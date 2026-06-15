"""Local speech recognition via faster-whisper (CTranslate2).

Sidekick uses local Whisper exclusively for transcription. Audio is captured
from WASAPI loopback, processed in-memory on CPU, and **never leaves the
device** — giving a clean privacy posture for customer meetings.

Configuration (``speech`` section in the customer YAML config)::

    speech:
      backend: whisper        # only supported value
      language: en-GB         # informational; Whisper uses language="en"
      model: small.en         # base.en | small.en | medium.en | large-v3
      compute_type: int8      # int8 | int8_float16 | float16 | float32

Environment overrides::

    SIDEKICK_WHISPER_MODEL=small.en
    SIDEKICK_WHISPER_COMPUTE=int8
    SIDEKICK_WHISPER_DEVICE=auto    # auto | cpu | cuda

Device selection: the faster-whisper (CTranslate2) backend supports **CUDA GPU
and CPU only** — there is no NPU/DirectML path. With ``device: auto`` (the
default) Sidekick uses a CUDA GPU when one is available (compute ``float16``)
and otherwise falls back to CPU (compute ``int8``). VAD gating is always on via
``vad_filter=True`` in :meth:`WhisperRecogniser.transcribe_chunk`.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

import numpy as np

from sidekick.analyst.context import TranscriptLine

logger = logging.getLogger(__name__)


def _cuda_available() -> bool:
    """Return True if CTranslate2 reports at least one usable CUDA device."""
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # noqa: BLE001 — any failure means "no usable GPU"
        logger.debug("CUDA detection failed; assuming CPU", exc_info=True)
        return False


def _resolve_device_and_compute(
    requested_device: str | None, requested_compute: str | None
) -> tuple[str, str]:
    """Resolve the concrete (device, compute_type) for faster-whisper.

    ``requested_device`` may be ``auto`` / ``cpu`` / ``cuda`` (or None →
    ``SIDEKICK_WHISPER_DEVICE`` env → ``auto``). ``auto`` picks CUDA when a GPU
    is present, else CPU. When ``requested_compute`` is not set, a sensible
    compute type is paired with the device (``float16`` on GPU, ``int8`` on
    CPU); an explicit compute type is always honoured.
    """
    device = (
        requested_device
        or os.environ.get("SIDEKICK_WHISPER_DEVICE")
        or "auto"
    ).lower()

    if device == "cuda":
        resolved = "cuda" if _cuda_available() else "cpu"
        if resolved == "cpu":
            logger.warning(
                "device=cuda requested but no CUDA GPU detected; using CPU."
            )
        device = resolved
    elif device == "auto":
        device = "cuda" if _cuda_available() else "cpu"
    elif device != "cpu":
        logger.warning("Unknown device=%r; using CPU.", device)
        device = "cpu"

    compute = (
        requested_compute
        or os.environ.get("SIDEKICK_WHISPER_COMPUTE")
        or ("float16" if device == "cuda" else "int8")
    )
    return device, compute


def _format_ts(seconds: float) -> str:
    """Convert seconds to VTT timestamp string HH:MM:SS.mmm."""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:06.3f}"


class SpeechRecogniser(Protocol):
    """Interface for speech-to-text backends."""

    async def transcribe_chunk(
        self,
        audio: np.ndarray,
        sample_rate: int = 16_000,
        chunk_start_offset: float = 0.0,
        initial_prompt: str | None = None,
    ) -> list[TranscriptLine]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Whisper recogniser (local, on-device, no network)
# ---------------------------------------------------------------------------


class WhisperRecogniser:
    """Local speech recognition using faster-whisper (CTranslate2).

    Runs on CPU with no API keys and no network calls. Models auto-download
    on first use into the Hugging Face cache.

    Model sizes (approximate):
      - ``base.en``   ~150 MB  fastest, ~8-10% WER
      - ``small.en``  ~470 MB  balanced, ~5-7% WER  **(default)**
      - ``medium.en`` ~1.5 GB  slower, ~4-6% WER
      - ``large-v3``  ~3.1 GB  multilingual, best accuracy
    """

    DEFAULT_MODEL = "small.en"
    DEFAULT_COMPUTE = "int8"

    def __init__(
        self,
        model_size: str | None = None,
        compute_type: str | None = None,
        device: str | None = None,
    ):
        model_size = (
            model_size
            or os.environ.get("SIDEKICK_WHISPER_MODEL")
            or self.DEFAULT_MODEL
        )
        device, compute_type = _resolve_device_and_compute(device, compute_type)
        logger.info(
            "Loading Whisper model: %s (device=%s, compute=%s)...",
            model_size,
            device,
            compute_type,
        )

        from faster_whisper import WhisperModel

        try:
            self.model = WhisperModel(
                model_size, device=device, compute_type=compute_type
            )
        except Exception as e:  # noqa: BLE001 — GPU init can fail at runtime
            if device != "cpu":
                logger.warning(
                    "Whisper init failed on device=%s (%s); falling back to CPU/int8.",
                    device,
                    e,
                )
                device, compute_type = "cpu", "int8"
                self.model = WhisperModel(
                    model_size, device=device, compute_type=compute_type
                )
            else:
                raise
        self.model_size = model_size
        self.compute_type = compute_type
        self.device = device
        self._last_text: str = ""
        self._repeat_count: int = 0
        logger.info("Whisper model loaded (%s, device=%s).", model_size, device)

    async def transcribe_chunk(
        self,
        audio: np.ndarray,
        sample_rate: int = 16_000,
        chunk_start_offset: float = 0.0,
        initial_prompt: str | None = None,
    ) -> list[TranscriptLine]:
        """Transcribe a single audio chunk.

        Args:
            audio: float32 mono PCM samples in [-1, 1].
            sample_rate: sample rate of ``audio`` (informational; Whisper
                expects 16 kHz upstream).
            chunk_start_offset: seconds elapsed since the listen session
                started up to the **start** of this chunk. Added to every
                segment offset so transcript timestamps reflect the position
                within the meeting, not within the 5-second chunk.
            initial_prompt: optional domain-vocabulary hint (Phase 5b) biasing
                Whisper toward expected proper nouns/jargon. ``None`` preserves
                Whisper's default behaviour.

        Returns:
            List of ``TranscriptLine`` with session-relative VTT timestamps.
        """
        if sample_rate != 16_000:
            logger.debug("Unusual sample_rate=%s for Whisper input", sample_rate)

        segments, _info = self.model.transcribe(
            audio,
            beam_size=5,
            language="en",
            vad_filter=True,
            initial_prompt=initial_prompt,
        )

        lines: list[TranscriptLine] = []
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue

            # Filter high no-speech probability segments (Whisper hallucination guard)
            if seg.no_speech_prob > 0.7:
                logger.debug(
                    "Dropping segment (no_speech_prob=%.2f): %s",
                    seg.no_speech_prob,
                    text,
                )
                continue

            # Repetition filter — Whisper hallucination guard
            if text == self._last_text:
                self._repeat_count += 1
                if self._repeat_count >= 3:
                    logger.debug("Dropping repeated hallucination: %s", text)
                    continue
            else:
                self._last_text = text
                self._repeat_count = 0

            lines.append(
                TranscriptLine(
                    start=_format_ts(chunk_start_offset + seg.start),
                    end=_format_ts(chunk_start_offset + seg.end),
                    speaker="(audio)",
                    text=text,
                )
            )

        return lines

    def close(self) -> None:
        self.model = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_recogniser(speech_config=None) -> SpeechRecogniser:
    """Create the speech recognition backend.

    Sidekick uses local Whisper exclusively. ``backend: azure`` is no longer
    supported (see CHANGELOG v0.3.0 for the rationale). Configs that still
    request ``azure`` log a warning and fall back to Whisper.

    Args:
        speech_config: A ``SpeechConfig`` from the customer YAML. May be
            ``None`` (defaults applied).

    Returns:
        A configured ``WhisperRecogniser`` instance.
    """
    if speech_config is not None and getattr(speech_config, "backend", "whisper") not in (
        "whisper",
        "",
        None,
    ):
        logger.warning(
            "speech.backend=%r is no longer supported. Using local Whisper. "
            "Update your customer YAML to 'backend: whisper' to silence this warning.",
            speech_config.backend,
        )

    model = None
    compute = None
    device = None
    if speech_config is not None:
        model = getattr(speech_config, "model", None)
        compute = getattr(speech_config, "compute_type", None)
        device = getattr(speech_config, "device", None)

    logger.info("Using local Whisper backend (on-device, no network).")
    return WhisperRecogniser(model_size=model, compute_type=compute, device=device)

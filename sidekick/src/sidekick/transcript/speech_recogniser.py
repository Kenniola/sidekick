"""Speech recognition backends — local Whisper (default) or Azure Speech (optional).

Backend selection is driven by the ``speech`` section in the customer YAML config:

  speech:
    backend: whisper       # or "azure"
    azure_region: uksouth
    azure_endpoint: ""     # Entra ID auth (for disableLocalAuth environments)
    azure_key: ""          # key auth (standard environments)
    language: en-GB

Both backends implement the same interface: take a numpy audio chunk,
return a list of TranscriptLine objects.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

import numpy as np

from sidekick.analyst.context import TranscriptLine

logger = logging.getLogger(__name__)


def _format_ts(seconds: float) -> str:
    """Convert seconds to VTT timestamp string HH:MM:SS.mmm."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:06.3f}"


class SpeechRecogniser(Protocol):
    """Interface for speech-to-text backends."""

    async def transcribe_chunk(
        self, audio: np.ndarray, sample_rate: int = 16_000
    ) -> list[TranscriptLine]: ...

    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Backend A: Local Whisper (default)
# ---------------------------------------------------------------------------


class WhisperRecogniser:
    """Local speech recognition using faster-whisper (CTranslate2).

    No API keys required. Model auto-downloads on first use (~150MB for base.en).
    """

    def __init__(
        self,
        model_size: str | None = None,
        compute_type: str = "int8",
    ):
        model_size = model_size or os.environ.get("SIDEKICK_WHISPER_MODEL", "base.en")
        logger.info("Loading Whisper model: %s (compute=%s)...", model_size, compute_type)

        from faster_whisper import WhisperModel

        self.model = WhisperModel(model_size, device="cpu", compute_type=compute_type)
        self._last_text: str = ""
        self._repeat_count: int = 0
        logger.info("Whisper model loaded.")

    async def transcribe_chunk(
        self, audio: np.ndarray, sample_rate: int = 16_000
    ) -> list[TranscriptLine]:
        segments, _info = self.model.transcribe(
            audio,
            beam_size=5,
            language="en",
            vad_filter=True,
        )

        lines: list[TranscriptLine] = []
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue

            # Filter high no-speech probability segments
            if seg.no_speech_prob > 0.7:
                logger.debug("Dropping segment (no_speech_prob=%.2f): %s", seg.no_speech_prob, text)
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

            lines.append(TranscriptLine(
                start=_format_ts(seg.start),
                end=_format_ts(seg.end),
                speaker="(audio)",
                text=text,
            ))

        return lines

    def close(self) -> None:
        self.model = None


# ---------------------------------------------------------------------------
# Backend B: Azure Speech (optional upgrade)
# ---------------------------------------------------------------------------


class AzureSpeechRecogniser:
    """Azure Cognitive Services Speech-to-Text with speaker diarization.

    Uses ConversationTranscriber for speaker identification — each segment
    is labelled with a speaker ID (Guest-1, Guest-2, etc.).

    Supports two auth modes:
      - Key-based: pass ``key`` (standard environments)
      - Entra ID: pass ``endpoint`` without key (policy-restricted environments,
        uses DefaultAzureCredential for automatic token refresh)
    """

    def __init__(
        self,
        region: str = "uksouth",
        key: str | None = None,
        endpoint: str | None = None,
        resource_id: str | None = None,
        speaker_map: dict[str, str] | None = None,
    ):
        import azure.cognitiveservices.speech as speechsdk

        self._speechsdk = speechsdk
        self._credential = None
        self._speaker_map = speaker_map or {}
        self._resource_id: str | None = None

        if key:
            # Key-based auth
            self._config = speechsdk.SpeechConfig(subscription=key, region=region)
            auth_mode = "key"
        elif endpoint:
            # Entra ID token auth (for disableLocalAuth=true environments)
            # Speech SDK needs: aad#<ARM_resource_ID>#<token>
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
            token = self._credential.get_token(
                "https://cognitiveservices.azure.com/.default"
            ).token

            # Resolve ARM resource ID if not provided
            if not resource_id:
                resource_id = self._resolve_resource_id(endpoint)
            self._resource_id = resource_id

            aad_token = f"aad#{resource_id}#{token}"
            wss_endpoint = f"wss://{region}.stt.speech.microsoft.com/speech/universal/v2"
            self._config = speechsdk.SpeechConfig(endpoint=wss_endpoint)
            self._config.authorization_token = aad_token
            auth_mode = "entra"
        else:
            raise RuntimeError(
                "AzureSpeechRecogniser requires either key or endpoint."
            )

        language = os.environ.get("SIDEKICK_SPEECH_LANGUAGE", "en-GB")
        self._config.speech_recognition_language = language
        self._region = region

        # Enable intermediate diarization results for real-time speaker labels
        self._config.set_property(
            speechsdk.PropertyId.SpeechServiceResponse_DiarizeIntermediateResults,
            "true",
        )
        logger.info(
            "Azure Speech recogniser initialised (region=%s, language=%s, auth=%s, diarization=on)",
            region,
            language,
            auth_mode,
        )

    async def transcribe_chunk(
        self, audio: np.ndarray, sample_rate: int = 16_000
    ) -> list[TranscriptLine]:
        speechsdk = self._speechsdk
        import asyncio

        # Refresh Entra token if using credential-based auth (tokens expire ~60 min)
        if self._credential is not None and self._resource_id:
            token = self._credential.get_token(
                "https://cognitiveservices.azure.com/.default"
            ).token
            self._config.authorization_token = f"aad#{self._resource_id}#{token}"

        # Convert float32 [-1,1] back to int16 PCM for the Azure SDK
        pcm = (audio * 32767).astype(np.int16).tobytes()

        # Create push stream with PCM format
        fmt = speechsdk.audio.AudioStreamFormat(
            samples_per_second=sample_rate,
            bits_per_sample=16,
            channels=1,
        )
        stream = speechsdk.audio.PushAudioInputStream(stream_format=fmt)
        stream.write(pcm)
        stream.close()

        audio_config = speechsdk.audio.AudioConfig(stream=stream)

        # Use SpeechRecognizer for reliable push-stream recognition.
        # ConversationTranscriber adds diarization but has stricter endpoint
        # requirements that can fail with Entra ID auth (SPXERR_INVALID_ARG).
        recognizer = speechsdk.SpeechRecognizer(
            speech_config=self._config,
            audio_config=audio_config,
        )

        lines: list[TranscriptLine] = []
        done_event = asyncio.Event()
        cancel_error: list[str] = []
        loop = asyncio.get_running_loop()

        def on_recognized(evt):
            if (
                evt.result.reason == speechsdk.ResultReason.RecognizedSpeech
                and evt.result.text.strip()
            ):
                offset_s = evt.result.offset / 10_000_000
                duration_s = evt.result.duration / 10_000_000

                lines.append(TranscriptLine(
                    start=_format_ts(offset_s),
                    end=_format_ts(offset_s + duration_s),
                    speaker="(azure)",
                    text=evt.result.text.strip(),
                ))

        def on_stopped(evt):
            loop.call_soon_threadsafe(done_event.set)

        def on_canceled(evt):
            details = evt.cancellation_details
            if details.reason != speechsdk.CancellationReason.EndOfStream:
                err_msg = (
                    f"Azure Speech cancelled: {details.reason} — "
                    f"{details.error_details or 'no details'}"
                )
                logger.warning(err_msg)
                cancel_error.append(err_msg)
            loop.call_soon_threadsafe(done_event.set)

        recognizer.recognized.connect(on_recognized)
        recognizer.session_stopped.connect(on_stopped)
        recognizer.canceled.connect(on_canceled)

        recognizer.start_continuous_recognition_async()

        # Wait for recognition to complete (audio is finite — pushed + closed)
        try:
            await asyncio.wait_for(done_event.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            logger.warning("Azure Speech recognition timed out for chunk")

        recognizer.stop_continuous_recognition_async()

        # Surface cancellation errors so the listen loop can report them
        if cancel_error and not lines:
            raise RuntimeError(cancel_error[0])

        return lines

    @staticmethod
    def _resolve_resource_id(endpoint: str) -> str:
        """Resolve the ARM resource ID from the Cognitive Services endpoint.

        Extracts the resource name from the endpoint URL and looks it up
        via Azure CLI. Falls back to the endpoint URL if az CLI isn't available.
        """
        import shutil
        import subprocess
        # Extract resource name from endpoint: https://<name>.cognitiveservices.azure.com/
        try:
            name = endpoint.rstrip("/").split("//")[1].split(".")[0]
        except (IndexError, AttributeError):
            logger.warning("Could not parse resource name from endpoint: %s", endpoint)
            return endpoint

        # Find az CLI — on Windows it's az.cmd, on Unix it's az
        az_cmd = shutil.which("az") or shutil.which("az.cmd")
        if not az_cmd:
            logger.error(
                "az CLI not found on PATH — cannot resolve ARM resource ID. "
                "Set speech.azure_resource_id in your config to avoid this lookup."
            )
            return endpoint

        try:
            result = subprocess.run(
                [az_cmd, "cognitiveservices", "account", "list",
                 "--query", f"[?name=='{name}'].id",
                 "-o", "tsv"],
                capture_output=True, text=True, timeout=15,
            )
            resource_id = result.stdout.strip()
            if resource_id:
                logger.info("Resolved ARM resource ID: %s", resource_id)
                return resource_id
        except Exception as e:
            logger.warning("Could not resolve ARM resource ID via az CLI: %s", e)

        return endpoint

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_recogniser(speech_config=None) -> SpeechRecogniser:
    """Create the appropriate speech recognition backend.

    Args:
        speech_config: A SpeechConfig from the customer YAML config.
            If None, defaults to Whisper.

    Selection logic:
      - backend == 'whisper' (or no config) → WhisperRecogniser
      - backend == 'azure' + azure_key → AzureSpeechRecogniser (key auth)
      - backend == 'azure' + azure_endpoint → AzureSpeechRecogniser (Entra ID)
      - backend == 'azure' with neither → clear error message
    """
    if speech_config is None or speech_config.backend == "whisper":
        logger.info("Using local Whisper backend")
        return WhisperRecogniser()

    if speech_config.backend != "azure":
        raise ValueError(
            f"Unknown speech backend: {speech_config.backend!r}. "
            f"Use 'whisper' or 'azure'."
        )

    # Azure Speech — determine auth mode
    region = speech_config.azure_region

    # Also check env vars as fallback (for mcp.json-level config)
    key = speech_config.azure_key or os.environ.get("AZURE_SPEECH_KEY", "")
    endpoint = speech_config.azure_endpoint or os.environ.get("AZURE_SPEECH_ENDPOINT", "")

    # Set language env var so AzureSpeechRecogniser picks it up
    os.environ["SIDEKICK_SPEECH_LANGUAGE"] = speech_config.language

    if key:
        logger.info("Using Azure Speech backend (key auth, region=%s)", region)
        return AzureSpeechRecogniser(
            region=region, key=key,
            speaker_map=speech_config.speaker_map,
        )

    if endpoint:
        logger.info("Using Azure Speech backend (Entra ID auth, region=%s)", region)
        resource_id = (
            speech_config.azure_resource_id
            or os.environ.get("AZURE_SPEECH_RESOURCE_ID", "")
            or None
        )
        return AzureSpeechRecogniser(
            region=region, endpoint=endpoint,
            resource_id=resource_id,
            speaker_map=speech_config.speaker_map,
        )

    raise RuntimeError(
        "speech.backend is 'azure' but no credentials configured.\n"
        "Set one of:\n"
        "  1. AZURE_SPEECH_KEY in ~/.sidekick/.env (recommended)\n"
        "  2. AZURE_SPEECH_ENDPOINT in ~/.sidekick/.env (for Entra ID auth)\n"
        "\n"
        "Example (~/.sidekick/.env):\n"
        "  AZURE_SPEECH_KEY=your-key-here\n"
        "  AZURE_SPEECH_REGION=uksouth"
    )

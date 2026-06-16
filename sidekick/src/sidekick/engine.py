"""Classification + dispatch engine (Phase 2e).

Extracted from ``server.py``: the orchestration that turns a batch of
transcript lines into queued action items, runs the research/prototype
pipelines, records results, and notifies. Also the domain auto-detection
that runs once enough transcript context has accumulated.

These functions take the :class:`~sidekick.session_state.SessionState`
explicitly and (for dispatch) a ``notify`` callable, so they can be tested
with mocked components. The live audio loop in ``server`` calls
``classify_and_dispatch`` on each batch.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable

from sidekick.session_state import SessionState

logger = logging.getLogger("sidekick")


async def detect_domains(state: SessionState) -> None:
    """Auto-detect domains from transcript after enough context accumulates.

    Runs a fast-tier LLM call on the first ~30 transcript lines to identify
    which technology domains are being discussed. Detected domains supplement
    (not replace) the configured domains from customers.yaml.
    """
    if state.domains_detected or not state.context or not state.config:
        return

    transcript_sample = state.context.full_transcript[-30:]
    if len(transcript_sample) < 10:
        return

    from sidekick.llm import call_llm, parse_llm_json
    sample_text = "\n".join(
        f"{getattr(line, 'speaker', '?')}: {getattr(line, 'text', str(line))}"
        for line in transcript_sample
    )

    try:
        result = await call_llm(
            system_prompt=(
                "You analyse meeting transcripts to detect technology domains "
                "being discussed. Return a JSON object with a single key "
                "\"domains\" containing a list of 3-8 domain strings. "
                "Examples: \"Microsoft Fabric\", \"Power BI\", \"Dynamics 365\", "
                "\"Azure APIM\", \"Azure Service Bus\", \"Oracle\", \"PostgreSQL\", "
                "\"AWS S3\", \"Databricks\", \"Legacy Systems\", \"Cosmos DB\". "
                "Only include domains clearly mentioned or implied."
            ),
            user_prompt=f"Transcript sample:\n{sample_text}",
            json_output=True,
            tier="fast",
            timeout=8,
        )
        data = parse_llm_json(result)
        detected = data.get("domains", [])
        if detected:
            # Merge with configured domains (no duplicates)
            existing = {d.lower() for d in state.config.domains}
            new_domains = [d for d in detected if d.lower() not in existing]
            if new_domains:
                state.config.domains.extend(new_domains)
                state.context.detected_domains = new_domains
                logger.info("Auto-detected domains: %s", new_domains)
                # Invalidate grounding cache since domains changed
                state.grounding_cache = None
    except Exception as e:
        logger.debug("Domain detection failed: %s", e)

    state.domains_detected = True


async def classify_and_dispatch(
    state: SessionState,
    lines: list,
    consecutive_errors: int,
    notify: Callable[[object], None],
) -> None:
    """Send accumulated transcript lines to the classifier and dispatch results."""
    state.classify_batch_count += 1

    action_items = await state.analyst.analyse_chunk(lines)

    # Auto-detect domains after 3 batches (enough transcript context)
    if state.classify_batch_count == 3 and not state.domains_detected:
        await detect_domains(state)

    for item in action_items:
        await state.queue.enqueue(item)
    results = await state.queue.process_ready(
        research=state.research,
        prototype=state.prototype,
        context=state.context,
        domains=state.config.domains if state.config else None,
        notify=notify,
    )
    for result in results:
        state.session_log.record(result)
        logger.info(
            "Sidekick output: [%s] %s",
            result.action_type,
            result.question[:60],
        )

    # Notify for new findings (sound alert + log file). Results whose lead
    # answer was already surfaced early via streaming (``early_notified``) are
    # skipped here to avoid a duplicate toast.
    for result in results:
        if not getattr(result, "early_notified", False):
            notify(result)

    # Adapt the Whisper vocabulary prior (Phase 5b) from LLM-corrected text:
    # thread/context key_facts and research answers spell proper nouns
    # correctly even when Whisper misheard them, so feeding them back improves
    # recognition of those terms in subsequent chunks.
    if state.vocabulary is not None:
        try:
            corrected: list[str] = []
            if state.context is not None:
                corrected.extend(getattr(state.context, "key_facts", []) or [])
                for thread in (getattr(state.context, "threads", {}) or {}).values():
                    corrected.extend(getattr(thread, "key_facts", []) or [])
            for result in results:
                corrected.append(getattr(result, "question", "") or "")
                corrected.append(getattr(result, "answer", "") or "")
            state.vocabulary.update(corrected)
        except Exception as e:  # noqa: BLE001 — prior is best-effort, never fatal
            logger.debug("Vocabulary update skipped: %s", e)


# ---------------------------------------------------------------------------
# Live audio loop (Phase 3 — extracted from server._run_listen_loop)
# ---------------------------------------------------------------------------

# Loop tuning constants. Kept module-level so tests can monkeypatch them to
# shrink timeouts / error budgets without waiting on real audio hardware.
MAX_CONSECUTIVE_ERRORS = 5
SILENCE_TIMEOUT_SECS = 60
AUDIO_POLL_SECS = 10.0


async def run_listen_loop(state: SessionState, notify: Callable[[object], None]) -> None:
    """Background loop: capture audio → transcribe → batch → classify → queue → execute.

    Thin entry point: initialise the capture stack (Whisper model + WASAPI
    loopback), then run the consume loop. Initialisation failures set
    ``state.last_error`` and return early without raising.
    """
    if not await _initialise_capture(state):
        return
    await _consume_audio(state, notify)


async def _initialise_capture(state: SessionState) -> bool:
    """Load the speech model and open the audio capture device.

    Returns ``True`` when ``state.recogniser`` and ``state.audio_capture`` are
    ready, or ``False`` (with ``state.last_error`` set) if the live
    dependencies are missing or the model fails to load.
    """
    try:
        from sidekick.transcript.audio_capture import AudioCapture
        from sidekick.transcript.speech_recogniser import create_recogniser
    except ImportError as e:
        state.last_error = f"Missing live dependencies: {e}"
        logger.error(state.last_error)
        return False

    loop = asyncio.get_running_loop()

    # 5c pre-roll: create and BEGIN audio capture *before* loading the (slow)
    # Whisper model, so the WASAPI stream buffers the opening of the meeting
    # while the model loads instead of dropping the first ~minute. The bounded
    # capture queue holds the pre-roll; once the model is ready _consume_audio
    # drains the buffered audio first. Best-effort — if begin() fails (e.g. no
    # device), start() opens capture lazily later as before.
    state.audio_capture = AudioCapture()
    try:
        state.audio_capture.begin()
        logger.info("Audio capture pre-roll started (buffering during model load).")
    except Exception as e:  # noqa: BLE001 — fall back to lazy start in _consume_audio
        logger.debug("Pre-roll capture begin failed (%s); will start lazily.", e)

    try:
        state.recogniser = await loop.run_in_executor(
            None, create_recogniser, state.config.speech
        )
    except Exception as e:
        state.last_error = f"Failed to load speech model: {e}"
        logger.exception(state.last_error)
        # Stop the pre-roll capture we may have started so the device/thread
        # do not leak when initialisation fails.
        try:
            state.audio_capture.stop()
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            logger.debug("Pre-roll capture stop after model-load failure raised", exc_info=True)
        return False

    # Seed the derived Whisper vocabulary prior (Phase 5b) from the same
    # engagement inputs that feed grounding — always available at listen time.
    try:
        from sidekick.transcript.vocabulary import Vocabulary, config_seed_text

        vocab = Vocabulary()
        vocab.seed(config_seed_text(state.config))
        if isinstance(getattr(state, "grounding_cache", None), str):
            vocab.seed(state.grounding_cache)
        state.vocabulary = vocab
        logger.info("Whisper vocabulary prior seeded (%d terms).", len(vocab))
    except Exception as e:  # noqa: BLE001 — prior is best-effort, never fatal
        logger.debug("Vocabulary seed skipped: %s", e)

    devices = await loop.run_in_executor(None, state.audio_capture.list_devices)
    device_names = [d["name"] for d in devices] if devices else ["(none found)"]
    logger.info("Loopback devices: %s", ", ".join(device_names))
    return True


async def _consume_audio(state: SessionState, notify: Callable[[object], None]) -> None:
    """Consume audio chunks, transcribe, batch, and dispatch for classification.

    Transcription runs on every audio chunk (real-time). Classification is
    batched: transcribed lines accumulate for ``CLASSIFY_INTERVAL`` seconds
    before being sent to the classifier, halving LLM calls while keeping
    transcription responsive. Auto-stops after ``SILENCE_TIMEOUT_SECS`` of no
    recognised speech (audio energy alone does not reset the timer).
    """
    consecutive_errors = 0

    # Classify cadence: config value, with env var override.
    CLASSIFY_INTERVAL = float(
        os.environ.get(
            "SIDEKICK_CLASSIFY_INTERVAL",
            str(state.config.sensitivity.analyst_interval_seconds),
        )
    )

    # Transcript line buffer — accumulates between classifier calls
    pending_lines: list = []
    last_classify_time = time.monotonic()

    # Speech-based auto-stop: tracks last time Whisper returned actual words.
    # Audio energy alone (background hum, HVAC, holding music) does NOT reset
    # this timer — only recognised speech does.
    last_speech_time = time.monotonic()

    # Per-chunk audio position (5a). The capture exposes ``last_chunk_offset``
    # derived from audio actually captured, so transcript timestamps reflect
    # the position within the meeting even when transcription falls behind
    # real time (fixes the wall-clock drift that inflated a 32-min meeting to
    # "56 minutes"). A local index estimate is used only when the capture does
    # not expose an offset (e.g. test doubles).
    chunk_duration = getattr(state.audio_capture, "chunk_duration", 5.0)
    chunk_index = 0

    audio_iter = None
    try:
        audio_iter = state.audio_capture.start().__aiter__()
        while True:
            # Wait for next audio chunk.  Use a short poll interval so we
            # can check the speech-based timer even when audio keeps flowing
            # (e.g. background noise with no intelligible speech).
            try:
                audio_chunk = await asyncio.wait_for(
                    audio_iter.__anext__(), timeout=AUDIO_POLL_SECS
                )
            except asyncio.TimeoutError:
                # No audio chunk arrived — check speech timer
                if time.monotonic() - last_speech_time >= SILENCE_TIMEOUT_SECS:
                    if pending_lines:
                        await classify_and_dispatch(
                            state, pending_lines, consecutive_errors, notify
                        )
                        pending_lines.clear()
                    logger.info(
                        "No speech detected for %ds — auto-stopping.",
                        SILENCE_TIMEOUT_SECS,
                    )
                    state.last_error = (
                        f"Auto-stopped: no speech detected for {SILENCE_TIMEOUT_SECS}s. "
                        f"Call stop for the summary, or listen to start a new session."
                    )
                    break
                continue
            except StopAsyncIteration:
                break

            try:
                # Sample-based audio position (5a): prefer the capture's
                # per-chunk offset (immune to processing backlog); fall back to
                # an index estimate when the capture does not expose one.
                capture_offset = getattr(
                    state.audio_capture, "last_chunk_offset", None
                )
                if isinstance(capture_offset, (int, float)):
                    chunk_start_offset = float(capture_offset)
                else:
                    chunk_start_offset = chunk_index * chunk_duration
                chunk_index += 1
                # Derived domain prior (5b) — adapts as the call progresses.
                initial_prompt = None
                if state.vocabulary is not None:
                    initial_prompt = state.vocabulary.initial_prompt()
                lines = await state.recogniser.transcribe_chunk(
                    audio_chunk,
                    chunk_start_offset=chunk_start_offset,
                    initial_prompt=initial_prompt,
                )
                if lines:
                    last_speech_time = time.monotonic()
                    pending_lines.extend(lines)

                # Check speech-based timeout even when audio is flowing
                # (handles background hum / ambient noise with no words).
                if time.monotonic() - last_speech_time >= SILENCE_TIMEOUT_SECS:
                    if pending_lines:
                        await classify_and_dispatch(
                            state, pending_lines, consecutive_errors, notify
                        )
                        pending_lines.clear()
                    logger.info(
                        "No recognised speech for %ds (audio still active) "
                        "— auto-stopping.",
                        SILENCE_TIMEOUT_SECS,
                    )
                    state.last_error = (
                        f"Auto-stopped: no recognised speech for "
                        f"{SILENCE_TIMEOUT_SECS}s. "
                        f"Call stop for the summary, or listen to start "
                        f"a new session."
                    )
                    break

                # Classify when enough time has passed
                if not lines:
                    continue
                elapsed = time.monotonic() - last_classify_time
                if elapsed >= CLASSIFY_INTERVAL:
                    await classify_and_dispatch(
                        state, pending_lines, consecutive_errors, notify
                    )
                    pending_lines.clear()
                    last_classify_time = time.monotonic()
                    consecutive_errors = 0

            except asyncio.CancelledError:
                raise
            except Exception as chunk_err:
                consecutive_errors += 1
                logger.exception(
                    "Error processing chunk (%d/%d): %s",
                    consecutive_errors, MAX_CONSECUTIVE_ERRORS, chunk_err,
                )
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    raise RuntimeError(
                        f"Too many consecutive errors ({MAX_CONSECUTIVE_ERRORS}): {chunk_err}"
                    ) from chunk_err

    except asyncio.CancelledError:
        logger.info("Listen loop cancelled.")
    except Exception as e:
        state.last_error = f"Listen loop error: {type(e).__name__}: {e}"
        logger.exception("Listen loop error.")
    finally:
        # Clean up resources on ANY exit (auto-stop, cancel, or error).
        # Without this, the WASAPI capture thread and Whisper model leak.
        if audio_iter is not None:
            await audio_iter.aclose()
        if state.audio_capture is not None:
            state.audio_capture.stop()
            logger.info("Audio capture cleaned up after listen loop exit.")
        if state.recogniser is not None:
            state.recogniser.close()
            logger.info("Speech recogniser closed after listen loop exit.")

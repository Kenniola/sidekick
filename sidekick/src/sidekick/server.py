"""Sidekick MCP server — real-time meeting co-pilot.

Seven tools, focused on live consulting value:

  listen            — capture system audio and transcribe in real-time
  suggest_questions — synthesise the meeting and recommend what to ask
  research          — answer a question instantly
  offerings         — surface VBD/IP offerings from Eng Hub
  prototype         — generate working code on the fly
  status            — show what Sidekick has found so far
  stop              — end session and get the summary
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np  # noqa: F401 — must be imported at module level to avoid import-lock deadlock with MCP stdio threads

try:
    import pyaudiowpatch  # noqa: F401 — same deadlock guard for audio device enumeration
except ImportError:
    pass  # optional [live] dependency

from mcp.server.fastmcp import FastMCP

from sidekick.config import load_config, SidekickConfig
from sidekick.analyst.classifier import TranscriptAnalyst
from sidekick.analyst.context import MeetingContext
from sidekick.queue.priority_queue import PriorityQueue
from sidekick.actions.research import ResearchPipeline
from sidekick.actions.prototype import PrototypePipeline
from sidekick.actions.enghub import EngHubPipeline
from sidekick.output.session_log import SessionLog

# Shared pipeline instances (reused across tool calls)
_enghub_pipeline = EngHubPipeline()

logger = logging.getLogger("sidekick")

# ---------------------------------------------------------------------------
# Global state (lives for the lifetime of the MCP server process)
# ---------------------------------------------------------------------------

server = FastMCP("sidekick")

_analyst: TranscriptAnalyst | None = None
_queue: PriorityQueue | None = None
_config: SidekickConfig | None = None
_context: MeetingContext | None = None
_session_log: SessionLog | None = None

# Tier 2 â€” live audio capture
_audio_capture = None                        # AudioCapture instance
_recogniser = None                           # SpeechRecogniser instance
_listen_task: asyncio.Task | None = None

# Error tracking for background loops
_last_error: str | None = None

# Delta tracking — unified counter for all tools
_last_surface_output_count: int = 0
_last_surface_thread_count: int = 0

# Track which thread topics have already been searched for offerings
_offerings_searched_topics: set[str] = set()

# Action pipelines
_research: ResearchPipeline | None = None
_prototype: PrototypePipeline | None = None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _init_session(config_name: str = "default"):
    """Initialise shared session components."""
    global _config, _context, _analyst, _queue, _session_log
    global _research, _prototype, _last_error
    global _last_surface_output_count, _last_surface_thread_count
    global _offerings_searched_topics

    _last_error = None
    _last_surface_output_count = 0
    _last_surface_thread_count = 0
    _offerings_searched_topics = set()

    _config = load_config(config_name)
    _context = MeetingContext(customer_name=_config.customer)
    _analyst = TranscriptAnalyst(config=_config, context=_context)
    _queue = PriorityQueue(config=_config)
    _session_log = SessionLog(config=_config)
    _research = ResearchPipeline(config=_config)
    _prototype = PrototypePipeline(config=_config)


# ---------------------------------------------------------------------------
# Background processing loop
# ---------------------------------------------------------------------------


async def _run_listen_loop():
    """Background loop: capture audio → transcribe → batch → classify → queue → execute.

    Transcription runs on every 5s audio chunk (real-time).
    Classification is batched: transcribed lines accumulate for CLASSIFY_INTERVAL
    seconds before being sent to the LLM classifier. This halves LLM calls while
    keeping transcription responsive.

    Auto-stops after SILENCE_TIMEOUT_SECS of no audio above threshold.
    """
    global _audio_capture, _recogniser, _analyst, _queue, _context, _last_error
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 5
    SILENCE_TIMEOUT_SECS = 60
    # Use config value, with env var as override
    CLASSIFY_INTERVAL = float(
        os.environ.get(
            "SIDEKICK_CLASSIFY_INTERVAL",
            str(_config.sensitivity.analyst_interval_seconds),
        )
    )

    # --- Heavy initialisation (runs in background, not in tool call) ---
    try:
        from sidekick.transcript.audio_capture import AudioCapture
        from sidekick.transcript.speech_recogniser import create_recogniser
    except ImportError as e:
        _last_error = f"Missing live dependencies: {e}"
        logger.error(_last_error)
        return

    try:
        loop = asyncio.get_running_loop()
        _recogniser = await loop.run_in_executor(
            None, create_recogniser, _config.speech
        )
    except Exception as e:
        _last_error = f"Failed to load speech model: {e}"
        logger.exception(_last_error)
        return

    _audio_capture = AudioCapture()

    devices = await loop.run_in_executor(None, _audio_capture.list_devices)
    device_names = [d["name"] for d in devices] if devices else ["(none found)"]
    logger.info("Loopback devices: %s", ", ".join(device_names))

    # Transcript line buffer — accumulates between classifier calls
    pending_lines: list = []
    last_classify_time = time.monotonic()

    # Speech-based auto-stop: tracks last time Whisper returned actual words.
    # Audio energy alone (background hum, HVAC, holding music) does NOT reset
    # this timer — only recognised speech does.
    last_speech_time = time.monotonic()

    # Short timeout for the audio iterator — we poll frequently so we can
    # check the speech timer even while audio chunks keep arriving.
    AUDIO_POLL_SECS = 10.0

    audio_iter = None
    try:
        audio_iter = _audio_capture.start().__aiter__()
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
                        await _classify_and_dispatch(pending_lines, consecutive_errors)
                        pending_lines.clear()
                    logger.info(
                        "No speech detected for %ds — auto-stopping.",
                        SILENCE_TIMEOUT_SECS,
                    )
                    _last_error = (
                        f"Auto-stopped: no speech detected for {SILENCE_TIMEOUT_SECS}s. "
                        f"Call stop for the summary, or listen to start a new session."
                    )
                    break
                continue
            except StopAsyncIteration:
                break

            try:
                lines = await _recogniser.transcribe_chunk(audio_chunk)
                if lines:
                    last_speech_time = time.monotonic()
                    pending_lines.extend(lines)

                # Check speech-based timeout even when audio is flowing
                # (handles background hum / ambient noise with no words).
                if time.monotonic() - last_speech_time >= SILENCE_TIMEOUT_SECS:
                    if pending_lines:
                        await _classify_and_dispatch(pending_lines, consecutive_errors)
                        pending_lines.clear()
                    logger.info(
                        "No recognised speech for %ds (audio still active) "
                        "— auto-stopping.",
                        SILENCE_TIMEOUT_SECS,
                    )
                    _last_error = (
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
                    await _classify_and_dispatch(pending_lines, consecutive_errors)
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
        _last_error = f"Listen loop error: {type(e).__name__}: {e}"
        logger.exception("Listen loop error.")
    finally:
        # Clean up resources on ANY exit (auto-stop, cancel, or error).
        # Without this, the WASAPI capture thread and Whisper model leak.
        if audio_iter is not None:
            await audio_iter.aclose()
        if _audio_capture is not None:
            _audio_capture.stop()
            logger.info("Audio capture cleaned up after listen loop exit.")
        if _recogniser is not None:
            _recogniser.close()
            logger.info("Speech recogniser closed after listen loop exit.")


async def _classify_and_dispatch(lines: list, consecutive_errors: int) -> None:
    """Send accumulated transcript lines to the classifier and dispatch results."""
    action_items = await _analyst.analyse_chunk(lines)
    for item in action_items:
        await _queue.enqueue(item)
    results = await _queue.process_ready(
        research=_research,
        prototype=_prototype,
        context=_context,
        domains=_config.domains if _config else None,
    )
    for result in results:
        _session_log.record(result)
        logger.info(
            "Sidekick output: [%s] %s",
            result.action_type,
            result.question[:60],
        )

    # Notify for new findings (sound alert + log file)
    for result in results:
        _notify(result)

    # Proactive offerings — search for VBDs when new thread topics emerge
    await _proactive_offerings_search()


async def _proactive_offerings_search() -> None:
    """Search for VBD/IP offerings on new thread topics.

    Runs after each classify cycle. Only searches topics that haven't been
    checked yet. If a match is found, records it as a high-priority offering
    result so the toast notification fires.
    """
    if not _context or not _context.threads or not _config:
        return

    new_topics: list[str] = []
    for t in _context.threads.values():
        if t.topic not in _offerings_searched_topics:
            _offerings_searched_topics.add(t.topic)
            new_topics.append(t.topic)

    if not new_topics:
        return

    try:
        from sidekick.queue.priority_queue import ActionResult

        topic_str = ", ".join(new_topics[:3])
        result = await _enghub_pipeline.search(topic_str, domains=_config.domains)

        if result.offerings:
            top = result.offerings[:3]
            lines = [f"Relevant offerings for \"{topic_str}\":"]
            for o in top:
                lines.append(f"  [{o.offering_type}] {o.title}")
                if o.description:
                    lines.append(f"    {o.description[:100]}")
                if o.url:
                    lines.append(f"    {o.url}")

            ar = ActionResult(
                question=f"VBD match: {topic_str}",
                action_type="offering",
                answer="\n".join(lines),
                priority="high",
                confidence="high",
            )
            _session_log.record(ar)
            _notify(ar)
            logger.info("Proactive offering surfaced for: %s", topic_str)
    except Exception:
        logger.debug("Proactive offerings search failed", exc_info=True)


def _notify(result) -> None:
    """Log a finding to alerts.jsonl and the MCP output channel.

    The user sees findings via the auto-surface preamble on their next
    tool call — no sound alert needed.
    """
    # 1. Audible alert (Windows only — 1kHz tone, 300ms)
    try:
        import sys
        if sys.platform == "win32":
            import winsound
            winsound.Beep(1000, 300)
    except Exception:
        pass  # Not on Windows or no sound device — skip silently

    # 2. Log to MCP server logger (appears in MCP output channel)
    icon = {"research": "\U0001f50d", "prototype": "\U0001f6e0"}.get(
        result.action_type, "\U0001f4cb"
    )
    logger.info(
        "%s FINDING [%s]: %s", icon, result.action_type, result.question[:80]
    )

    # 3. Append to alerts file (audit trail)
    try:
        alerts_dir = Path.home() / ".sidekick" / "live"
        alerts_dir.mkdir(parents=True, exist_ok=True)
        alerts_path = alerts_dir / "alerts.jsonl"

        alert = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": result.action_type,
            "summary": result.question[:120],
            "confidence": getattr(result, "confidence", "medium"),
            "priority": getattr(result, "priority", "medium"),
        }
        with open(alerts_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(alert) + "\n")
    except Exception:
        logger.debug("Failed to write alert file", exc_info=True)


def _get_unseen_findings() -> str:
    """Return new findings since the last tool call, then mark them seen.

    Every tool call prepends this to its output so the user sees background
    research results regardless of which tool they invoke. This solves the
    MCP push limitation — the server can't initiate messages, but it can
    piggyback findings on any response.
    """
    global _last_surface_output_count, _last_surface_thread_count

    try:
        if not _session_log and not _context:
            return ""

        parts: list[str] = []

        # New threads
        all_threads = list(_context.threads.values()) if _context else []
        new_threads = all_threads[_last_surface_thread_count:]
        if new_threads:
            for t in new_threads:
                status_icon = "\u23f3" if t.status == "open" else "\u2705"
                parts.append(f"  {status_icon} New thread: **{t.topic}** ({t.status})")
                for q in t.questions[:2]:
                    parts.append(f"     \u2514\u2500 {q}")

        # New research results
        all_outputs = _session_log.outputs if _session_log else []
        new_outputs = all_outputs[_last_surface_output_count:]
        logger.debug(
            "_get_unseen_findings: %d total outputs, surface_count=%d, %d new, %d new threads",
            len(all_outputs), _last_surface_output_count, len(new_outputs), len(new_threads),
        )
        if new_outputs:
            for o in new_outputs:
                conf = o.get("confidence", "medium").upper()
                parts.append(f"  \u2705 **{o['question'][:80]}** ({conf})")
                answer = o.get("answer", "")
                if answer:
                    for line in answer.strip().split("\n"):
                        line = line.strip()
                        if line and not line.startswith("Sources"):
                            parts.append(f"     {line[:140]}")
                            break
                sources = o.get("sources", [])
                if not sources and answer:
                    sources = re.findall(r"https?://[^\s\)]+", answer)
                for src in sources[:2]:
                    parts.append(f"     \u2514\u2500 {src}")

        # Update counters
        _last_surface_thread_count = len(all_threads)
        _last_surface_output_count = len(all_outputs)

        if not parts:
            return ""

        header = f"\U0001f514 **SIDEKICK FOUND** ({len(new_outputs)} new) while you were talking:\n"
        return header + "\n".join(parts) + "\n\n---\n\n"
    except Exception:
        logger.warning("_get_unseen_findings failed", exc_info=True)
        return ""


def _build_grounding_context() -> str:
    """Build grounding context from instruction files and past engagement artifacts.

    Loads team standards from .github/instructions/ and recent engagement
    artifacts from configured repo paths to give the advisor deep context.
    """
    if not _config:
        return "(no config loaded)"

    workspace_root = Path(
        os.environ.get("SIDEKICK_WORKSPACE_ROOT", ".")
    )
    parts: list[str] = []

    # 1. Load relevant instruction files based on configured domains
    instructions_dir = workspace_root / ".github" / "instructions"
    if instructions_dir.exists():
        domain_keywords = [d.lower() for d in _config.domains]
        # Map domain keywords to instruction file names
        keyword_to_file = {
            "pyspark": "pyspark-notebooks",
            "notebook": "pyspark-notebooks",
            "spark": "pyspark-notebooks",
            "warehouse": "tsql-warehouse",
            "sql": "tsql-warehouse",
            "t-sql": "tsql-warehouse",
            "dax": "dax-powerbi",
            "power bi": "dax-powerbi",
            "powerbi": "dax-powerbi",
            "semantic model": "dax-powerbi",
            "directlake": "dax-powerbi",
            "dataflow": "dataflows-pipelines",
            "pipeline": "dataflows-pipelines",
            "governance": "governance-security",
            "purview": "governance-security",
            "security": "governance-security",
            "rls": "governance-security",
            "aws": "cross-cloud-integration",
            "s3": "cross-cloud-integration",
            "cross-cloud": "cross-cloud-integration",
        }

        loaded_files: set[str] = set()
        for domain in domain_keywords:
            for kw, fname in keyword_to_file.items():
                if kw in domain and fname not in loaded_files:
                    fpath = instructions_dir / f"{fname}.instructions.md"
                    if fpath.exists():
                        try:
                            content = fpath.read_text(encoding="utf-8")
                            # Take first 800 chars to stay within context limits
                            parts.append(f"--- {fname} standards ---\n{content[:800]}")
                            loaded_files.add(fname)
                        except Exception:
                            pass

    # 2. Load recent engagement artifacts (meeting preps, QA summaries)
    for repo_path_str in _config.grounding.repo_paths:
        repo_path = workspace_root / repo_path_str
        if not repo_path.exists():
            continue
        # Skip the instructions directory (already loaded above)
        if repo_path_str.rstrip("/").endswith("instructions"):
            continue

        # Search for recent meeting prep and summary files
        artifact_files: list[tuple[float, Path]] = []
        for suffix in ("*.md", "*.txt"):
            for f in repo_path.rglob(suffix):
                try:
                    artifact_files.append((f.stat().st_mtime, f))
                except Exception:
                    continue

        # Sort by modification time (newest first), take top 3
        artifact_files.sort(key=lambda x: x[0], reverse=True)
        for _, f in artifact_files[:3]:
            try:
                content = f.read_text(encoding="utf-8")
                rel = f.relative_to(workspace_root)
                parts.append(f"--- {rel} (recent artifact) ---\n{content[:1200]}")
            except Exception:
                continue

    # 3. Load previous session summaries for this customer
    outputs_dir = Path.home() / ".sidekick" / "outputs" / (_config.customer or "default")
    if outputs_dir.exists():
        summary_files = sorted(
            outputs_dir.glob("sidekick_summary_*.md"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        for sf in summary_files[:2]:
            try:
                content = sf.read_text(encoding="utf-8")
                parts.append(f"--- Previous session: {sf.name} ---\n{content[:400]}")
            except Exception:
                continue

    return "\n\n".join(parts) if parts else "(no grounding context available)"


# ---------------------------------------------------------------------------
# Tool 1: listen â€” start live audio capture
# ---------------------------------------------------------------------------


@server.tool()
async def listen(config: str = "default", confirmed: bool = False) -> str:
    """Start capturing system audio and transcribing in real-time.

    Captures audio from your default speakers/headset via WASAPI loopback
    and runs the full analysis pipeline. Speech backend (Whisper or Azure)
    is configured in the customer YAML config.

    The first call (without confirmed=True) returns a consent notice.
    The agent should present this to the user and only proceed by calling
    listen again with confirmed=True once the user agrees.

    Args:
        config: Customer config name (e.g., 'hmrc', 'moj'). Defaults to 'default'.
        confirmed: Set to True after the user consents to audio transcription.
    """
    global _audio_capture, _recogniser, _listen_task

    if _listen_task and not _listen_task.done():
        return "Already listening. Call stop to end the session first."

    # --- Consent gate ---
    if not confirmed:
        return (
            "\u26a0\ufe0f Audio Transcription Consent\n"
            "\n"
            "Sidekick will capture and transcribe system audio "
            "(Teams, Zoom, etc.).\n"
            "Please confirm that all meeting participants consent "
            "to transcription being captured.\n"
            f"\nConfig: {config} | Reply yes to start."
        )

    # Audio modules are imported at top-of-function to validate they're
    # installed. The heavy C-extension imports (numpy, ctranslate2) must
    # already be in sys.modules from module-level imports above — otherwise
    # Python's import lock deadlocks with MCP's stdio reader thread.
    try:
        from sidekick.transcript.audio_capture import AudioCapture  # noqa: F401
        from sidekick.transcript.speech_recogniser import create_recogniser  # noqa: F401
    except ImportError as e:
        return (
            f"Missing live dependencies: {e}\n"
            f"Install with: pip install -e \".[live]\""
        )

    _init_session(config)

    backend_label = (
        f"Azure Speech ({_config.speech.azure_region})"
        if _config.speech.backend == "azure"
        else "Whisper (local)"
    )

    # Enumerate loopback devices (pyaudiowpatch is pre-imported at module
    # level to avoid import-lock deadlock).
    try:
        from sidekick.transcript.audio_capture import AudioCapture as _AC
        devices = _AC().list_devices()
        # Shorten device names: strip driver details and [Loopback] suffix
        device_names = []
        for d in (devices or []):
            n = d["name"].replace(" [Loopback]", "")
            n = n.split(" (")[0].strip() if " (" in n else n.strip()
            device_names.append(n)
        if not device_names:
            device_names = ["none found"]
    except Exception:
        device_names = ["unavailable"]

    # Start the background loop — model loading and audio capture happen
    # there so this tool returns instantly.
    _listen_task = asyncio.create_task(_run_listen_loop())

    domains = " \u00b7 ".join(_config.domains)
    devices_str = " \u00b7 ".join(device_names)

    return (
        f"{_config.customer} \u2014 \U0001f399\ufe0f live ({backend_label})\n"
        f"\n"
        f"Config: {config}.yaml \u00b7 Domains: {domains}\n"
        f"Devices: {devices_str}\n"
        f"\n"
        f"\U0001f7e2 Loading model and starting audio capture...\n"
        f"\n"
        f"`suggest_questions` \u00b7 `research` \u00b7 `prototype` \u00b7 `stop`"
    )


# ---------------------------------------------------------------------------
# Tool 2: suggest_questions â€” consultant advisor
# ---------------------------------------------------------------------------


@server.tool()
async def suggest_questions() -> str:
    """Synthesise the meeting and recommend high-impact questions to ask the client.

    Uses deep chain-of-thought reasoning with grounding from team standards,
    past engagement artifacts, and relevant VBD/IP offerings. Categorised as:
    clarify, probe, challenge, scope, stakeholder, risk, or next_step.
    """
    if not _context:
        return "No active session. Start with: listen"

    if len(_context.full_transcript) < 3:
        return "Not enough transcript yet \u2014 need a few exchanges first."

    from sidekick.analyst.prompts import CONSULTANT_ADVISOR_PROMPT
    from sidekick.llm import call_llm

    # Build a rich context block with key facts and open questions
    key_facts_str = ""
    if _context.key_facts:
        key_facts_str = "\nKey facts established:\n" + "\n".join(
            f"  - {f}" for f in _context.key_facts[-10:]
        )
    open_q_str = ""
    if _context.open_questions:
        open_q_str = "\nOpen questions (unanswered):\n" + "\n".join(
            f"  - {q['question']}" for q in _context.open_questions[-5:]
        )

    context_block = (
        f"Customer: {_config.customer}\n"
        f"Domains: {', '.join(_config.domains)}\n"
        f"Elapsed: {_context.elapsed_minutes:.0f} minutes\n"
        f"Phase: {_context.current_phase}\n"
        f"Transcript lines: {len(_context.full_transcript)}"
        f"{key_facts_str}{open_q_str}"
    )

    recent = _context.full_transcript[-50:]
    transcript_block = "\n".join(
        f"[{getattr(line, 'start', '?')}] {getattr(line, 'speaker', '?')}: {getattr(line, 'text', str(line))}"
        for line in recent
    )

    threads_block = _context.format_threads()

    research_block = "(none yet)"
    if _session_log and _session_log.outputs:
        research_parts = []
        for o in _session_log.outputs[-10:]:
            research_parts.append(f"[{o['action_type']}] {o['question']}: {o['answer'][:150]}")
        research_block = "\n".join(research_parts)

    # Offerings — search Eng Hub for relevant VBD/IP offerings
    offerings_block = "(no offerings matched)"
    try:
        # Extract topics from structured threads (preferred) or key_facts
        thread_topics = [
            t.topic for t in _context.threads.values()
            if t.status == "open"
        ] if _context.threads else []

        if thread_topics:
            topic_str = ", ".join(thread_topics[-5:])
        else:
            topic_str = " ".join(_context.key_facts[-5:]) if _context.key_facts else ""

        if topic_str:
            config = _config or load_config("default")
            eh_result = await _enghub_pipeline.search(topic_str, domains=config.domains)
            if eh_result and eh_result.offerings:
                parts = []
                for o in eh_result.offerings[:5]:
                    parts.append(f"- [{o.offering_type}] {o.title}: {o.description[:120]}")
                    if o.url:
                        parts.append(f"  URL: {o.url}")
                offerings_block = "\n".join(parts)
    except Exception:
        logger.debug("Eng Hub search in suggest_questions failed", exc_info=True)

    # Grounding — team standards + past engagement artifacts
    grounding_block = _build_grounding_context()

    prompt = CONSULTANT_ADVISOR_PROMPT.format(
        context_block=context_block,
        transcript_block=transcript_block,
        threads_block=threads_block,
        research_block=research_block,
        offerings_block=offerings_block,
        grounding_block=grounding_block,
        phase=_context.current_phase,
    )

    try:
        response_text = await call_llm(
            system_prompt=(
                "You are a senior consulting advisor with deep expertise in "
                "Microsoft Fabric, data platforms, and cloud architecture. "
                "Think carefully through the reasoning chain before generating "
                "questions. Return JSON only."
            ),
            user_prompt=prompt,
            json_output=True,
            timeout=45.0,
            tier="deep",
        )

        cleaned = response_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()

        data = json.loads(cleaned)
    except Exception as e:
        logger.exception("suggest_questions LLM call failed")
        return f"Failed to generate suggestions: {type(e).__name__}: {e}"

    parts = []

    synthesis = data.get("synthesis", "")
    if synthesis:
        parts.append(f"\U0001f4cb {synthesis}")
        parts.append("")

    # Show corrections first — these are urgent
    corrections = data.get("corrections", [])
    if corrections:
        parts.append("\u26a0\ufe0f CORRECTIONS:")
        for corr in corrections:
            parts.append(f"  \U0001f534 {corr}")
        parts.append("")

    questions = data.get("questions", [])
    if questions:
        parts.append("SUGGESTED QUESTIONS:")
        for i, q in enumerate(questions, 1):
            impact = q.get("impact", "medium")
            icon = "\U0001f534" if impact == "high" else "\U0001f7e1"
            category = q.get("category", "?")
            parts.append(f"  {icon} {i}. [{category}] {q.get('question', '?')}")
            rationale = q.get("rationale", "")
            if rationale:
                parts.append(f"     \u21b3 {rationale}")
            builds_on = q.get("builds_on", "")
            if builds_on:
                parts.append(f"     \U0001f4ac Based on: \"{builds_on}\"")
            parts.append("")
    else:
        parts.append("No questions to suggest at this point.")

    observations = data.get("observations", [])
    if observations:
        parts.append("OBSERVATIONS:")
        for obs in observations:
            parts.append(f"  \U0001f441 {obs}")

    return _get_unseen_findings() + "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool 3: research — instant answers
# ---------------------------------------------------------------------------


@server.tool()
async def research(question: str, depth: str = "medium") -> str:
    """Research a question instantly. Searches MS Learn and workspace docs.

    Args:
        question: The question to research.
        depth: 'quick' (fast lookup), 'medium' (multi-source), 'deep' (thorough).
    """
    pipeline = _research or ResearchPipeline()

    result = await pipeline.execute_direct(
        question=question,
        depth=depth,
        context=_context,
        tier="deep" if depth == "deep" else "standard",
        domains=_config.domains if _config else None,
    )

    return _get_unseen_findings() + result.format()

# ---------------------------------------------------------------------------
# Tool 4: offerings — surface VBD/IP from Eng Hub
# ---------------------------------------------------------------------------


@server.tool()
async def offerings(topic: str = "") -> str:
    """Surface relevant VBD/IP offerings from the Eng Hub Resource Center.

    Searches the Cloud & AI Platforms Resource Center for PoCs, ADRs,
    WorkshopPLUS, Solution Optimizations, and other delivery offerings
    that match the current meeting topic.

    Args:
        topic: The topic to search for (e.g. "lakehouse architecture",
               "Power BI DirectLake", "database migration"). If empty,
               uses the current meeting context to infer the topic.
    """
    # If no topic provided, infer from meeting context
    if not topic and _context:
        recent_topics = getattr(_context, "topic_threads", [])
        if recent_topics:
            topic = ", ".join(t.get("topic", str(t)) if isinstance(t, dict) else str(t) for t in recent_topics[-3:])
        else:
            key_facts = getattr(_context, "key_facts", [])
            topic = " ".join(key_facts[-5:]) if key_facts else ""

    if not topic:
        return "No topic provided and no meeting context available. Provide a topic, e.g.: offerings lakehouse migration"

    config = _config or load_config("default")
    result = await _enghub_pipeline.search(topic, domains=config.domains)
    return _get_unseen_findings() + result.format()



# ---------------------------------------------------------------------------
# Tool 5: prototype â€” generate code on the fly
# ---------------------------------------------------------------------------


@server.tool()
async def prototype(
    description: str,
    type: str = "notebook",
    columns: str = "",
) -> str:
    """Generate working code on the fly during the meeting.

    Args:
        description: What the prototype should do.
        type: 'notebook' (PySpark), 'sql' (T-SQL), 'dax' (measures), 'pipeline'.
        columns: Optional comma-separated column list.
    """
    config = _config or load_config("default")
    pipeline = _prototype or PrototypePipeline(config=config)

    result = await pipeline.execute_direct(
        description=description,
        prototype_type=type,
        columns=columns,
        context=_context,
    )
    return _get_unseen_findings() + result.format()


# ---------------------------------------------------------------------------
# Tool 6: status â€” what has Sidekick found so far?
# ---------------------------------------------------------------------------


@server.tool()
async def status() -> str:
    """Show what Sidekick has found — new threads, research results, and errors.

    Call this anytime to see incremental updates since your last check.
    Also shows the full session overview.
    """

    if not _context:
        return "No active session. Start with: listen"

    # Session header
    if _audio_capture and _audio_capture.is_capturing:
        backend = (
            f"Azure Speech ({_config.speech.azure_region})"
            if _config and _config.speech.backend == "azure"
            else "Whisper"
        )
        mode_label = f"🎙️ live ({backend})"
    else:
        mode_label = "session active"

    parts = [
        f"{_config.customer} — {mode_label} — {_context.elapsed_minutes:.0f} min — {len(_context.full_transcript)} lines",
    ]

    # Surface errors immediately
    if _last_error:
        parts.append(f"\n⚠️ ERROR: {_last_error}")
        parts.append("Try: stop, then listen again.\n")

    # In-progress queue items
    if _queue:
        in_progress = _queue.get_in_progress()
        if in_progress:
            parts.append("")
            parts.append("RESEARCHING:")
            for item in in_progress:
                parts.append(f"  ⏳ {item.item.question[:80]}")

    # Full thread summary
    all_threads = list(_context.threads.values()) if _context else []
    if all_threads:
        parts.append("")
        parts.append("ALL THREADS:")
        for t in all_threads:
            status_icon = "⏳" if t.status == "open" else "✅" if t.status == "resolved" else "🚫"
            parts.append(f"  {status_icon} {t.topic} ({t.status})")

    # Output count
    total_outputs = len(_session_log.outputs) if _session_log else 0
    in_progress_count = len(_queue.get_in_progress()) if _queue else 0
    if total_outputs or in_progress_count:
        phase_suffix = f" Still on the {_context.current_phase} topic." if hasattr(_context, 'current_phase') else ""
        parts.append(f"\n{total_outputs} research completed, {in_progress_count} in progress.{phase_suffix}")

    if len(parts) == 1 and not _last_error:
        parts.append("Listening... no threads detected yet.")

    # Prepend any new findings since last tool call
    return _get_unseen_findings() + "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool 7: stop â€” end session with summary
# ---------------------------------------------------------------------------


@server.tool()
async def stop() -> str:
    """End the session and get a full meeting summary.

    Stops audio capture, generates a structured summary of all threads,
    research results, and action items, and saves the session log.
    """
    global _listen_task, _audio_capture, _recogniser
    global _session_log, _context, _last_error

    # Stop audio capture FIRST — this signals the capture thread to exit
    # cleanly before we cancel the listen task. Cancelling the task while
    # the capture thread is still writing to PyAudio can cause a C-level crash.
    # Note: _run_listen_loop's finally block may have already called stop()
    # and close() — these methods are safe to call multiple times.
    if _audio_capture:
        _audio_capture.stop()

    if _listen_task and not _listen_task.done():
        # Give the loop a few seconds to exit naturally via the sentinel
        try:
            await asyncio.wait_for(_listen_task, timeout=5.0)
        except asyncio.TimeoutError:
            _listen_task.cancel()
            try:
                await _listen_task
            except asyncio.CancelledError:
                pass
    elif _listen_task and _listen_task.done():
        # Task already finished (e.g. auto-stop) — retrieve any exception
        # so it doesn't go unhandled.
        if not _listen_task.cancelled():
            exc = _listen_task.exception()
            if exc:
                logger.warning("Listen task had unhandled exception: %s", exc)

    if _recogniser:
        _recogniser.close()

    summary = "No active session."
    saved_files: list[str] = []
    if _session_log and _context:
        summary = _session_log.generate_summary(_context)
        path = _session_log.save_to_disk()
        if path:
            saved_files.append(str(path))
        # Export transcript and markdown summary
        tp = _session_log.save_transcript(_context)
        if tp:
            saved_files.append(str(tp))
        mp = _session_log.save_markdown_summary(_context)
        if mp:
            saved_files.append(str(mp))

    if saved_files:
        summary += "\n\n**Saved files:**\n" + "\n".join(
            f"- {f}" for f in saved_files
        )

    # Reset state
    _listen_task = None
    _audio_capture = None
    _recogniser = None
    _last_error = None

    return _get_unseen_findings() + summary


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main():
    """Run the Sidekick MCP server over stdio."""
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
    await server.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())

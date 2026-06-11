"""Finding notifier — audible alert, MCP-channel log line, and audit JSONL.

Extracted from ``server._notify`` (Phase 2b) so the side-effecting bits are
testable in isolation. The server resolves the configured sound and delegates
here.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("sidekick")

# Findings audit trail. Computed lazily (see ``_default_alerts_dir``) so tests
# can target a temp directory without import-time HOME binding.
_ALERTS_SUBPATH = (".sidekick", "live")


def _default_alerts_dir() -> Path:
    """Return ~/.sidekick/live (resolved fresh so HOME changes are honoured)."""
    return Path.home().joinpath(*_ALERTS_SUBPATH)


def play_sound(sound: str = "chime") -> None:
    """Play the configured notification sound. Windows-only; no-op elsewhere.

    ``sound`` accepts: ``silent`` (no sound), ``chime`` (default ``MB_OK``),
    ``asterisk``, ``exclamation``, or ``beep`` (legacy 800 Hz / 200 ms tone).
    All failures (no audio device, non-Windows) are swallowed.
    """
    try:
        if sys.platform != "win32":
            return
        import winsound

        if sound == "silent":
            return
        if sound == "beep":
            # Legacy raw tone — 800 Hz, 200 ms (softer than the old 1 kHz/300 ms).
            winsound.Beep(800, 200)
            return
        # MessageBeep variants respect the Windows Notification volume slider.
        style_map = {
            "chime": winsound.MB_OK,
            "asterisk": winsound.MB_ICONASTERISK,
            "exclamation": winsound.MB_ICONEXCLAMATION,
        }
        winsound.MessageBeep(style_map.get(sound, winsound.MB_OK))
    except Exception:
        pass  # Not on Windows or no sound device — skip silently.


def write_alert(result, alerts_dir: Path | None = None) -> None:
    """Append a finding to ``<alerts_dir>/alerts.jsonl`` (audit trail)."""
    target_dir = alerts_dir if alerts_dir is not None else _default_alerts_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        alert = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": result.action_type,
            "summary": result.question[:120],
            "confidence": getattr(result, "confidence", "medium"),
            "priority": getattr(result, "priority", "medium"),
        }
        with open(target_dir / "alerts.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(alert) + "\n")
    except Exception:
        logger.debug("Failed to write alert file", exc_info=True)


def notify(result, sound: str = "chime", alerts_dir: Path | None = None) -> None:
    """Log a finding: audible alert + MCP-channel log line + audit JSONL.

    The user sees findings via the auto-surface preamble on their next tool
    call, so the audible alert is intentionally subtle.
    """
    play_sound(sound)

    icon = {"research": "\U0001f50d", "prototype": "\U0001f6e0"}.get(
        result.action_type, "\U0001f4cb"
    )
    logger.info(
        "%s FINDING [%s]: %s", icon, result.action_type, result.question[:80]
    )

    write_alert(result, alerts_dir=alerts_dir)

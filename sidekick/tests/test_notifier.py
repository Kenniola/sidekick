"""Tests for the extracted finding notifier (Phase 2b)."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass

from sidekick.output import notifier


@dataclass
class _FakeResult:
    action_type: str = "research"
    question: str = "What is OneLake?"
    confidence: str = "high"
    priority: str = "high"


class TestPlaySound:
    def test_no_sound_device_does_not_raise(self, monkeypatch):
        # Force the win32 branch but make winsound import fail → swallowed.
        monkeypatch.setattr(sys, "platform", "win32")
        # No winsound on non-Windows test hosts; the broad except must absorb it.
        notifier.play_sound("chime")  # must not raise

    def test_silent_is_noop(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        notifier.play_sound("silent")  # must not raise

    def test_non_windows_is_noop(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        notifier.play_sound("chime")  # must not raise


class TestWriteAlert:
    def test_writes_jsonl_line(self, tmp_path):
        result = _FakeResult()
        notifier.write_alert(result, alerts_dir=tmp_path)

        alerts_file = tmp_path / "alerts.jsonl"
        assert alerts_file.exists()
        line = alerts_file.read_text(encoding="utf-8").strip()
        record = json.loads(line)
        assert record["type"] == "research"
        assert record["summary"] == "What is OneLake?"
        assert record["confidence"] == "high"
        assert record["priority"] == "high"
        assert "timestamp" in record

    def test_appends_multiple(self, tmp_path):
        notifier.write_alert(_FakeResult(question="Q1"), alerts_dir=tmp_path)
        notifier.write_alert(_FakeResult(question="Q2"), alerts_dir=tmp_path)
        lines = (tmp_path / "alerts.jsonl").read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["summary"] == "Q1"
        assert json.loads(lines[1])["summary"] == "Q2"

    def test_missing_optional_fields_default(self, tmp_path):
        @dataclass
        class _Minimal:
            action_type: str = "prototype"
            question: str = "Q"

        notifier.write_alert(_Minimal(), alerts_dir=tmp_path)
        record = json.loads((tmp_path / "alerts.jsonl").read_text(encoding="utf-8").strip())
        assert record["confidence"] == "medium"
        assert record["priority"] == "medium"

    def test_creates_dir_if_missing(self, tmp_path):
        nested = tmp_path / "live"
        assert not nested.exists()
        notifier.write_alert(_FakeResult(), alerts_dir=nested)
        assert (nested / "alerts.jsonl").exists()


class TestNotify:
    def test_notify_writes_alert_and_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")  # skip sound
        notifier.notify(_FakeResult(), sound="silent", alerts_dir=tmp_path)
        assert (tmp_path / "alerts.jsonl").exists()

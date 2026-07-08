"""Tests for LLM speaker-naming (Phase 7 / C3 Tier 2).

The LLM call is injected so the suite runs offline. Covers roster building,
partial/graceful attribution, batching offsets, and the engine wiring.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sidekick import engine
from sidekick.analyst import speakers
from sidekick.session_state import SessionState


def _line(text, speaker="(audio)"):
    return SimpleNamespace(text=text, speaker=speaker)


class TestBuildRoster:
    def test_config_names(self):
        cfg = SimpleNamespace(consultant_names=["Kenni", "Chris"], client_names=["Graham"])
        ctx = SimpleNamespace(participants={})
        r = speakers.build_roster(cfg, ctx)
        assert r["Kenni"] == "consultant"
        assert r["Chris"] == "consultant"
        assert r["Graham"] == "client"

    def test_participants_fill_in(self):
        cfg = SimpleNamespace(consultant_names=["Kenni"], client_names=[])
        ctx = SimpleNamespace(participants={"Dawn": "client"})
        r = speakers.build_roster(cfg, ctx)
        assert r["Dawn"] == "client"

    def test_config_takes_precedence_over_participants(self):
        cfg = SimpleNamespace(consultant_names=["Kenni"], client_names=[])
        ctx = SimpleNamespace(participants={"Kenni": "client"})
        r = speakers.build_roster(cfg, ctx)
        assert r["Kenni"] == "consultant"

    def test_empty_roster(self):
        cfg = SimpleNamespace(consultant_names=[], client_names=[])
        ctx = SimpleNamespace(participants={})
        assert speakers.build_roster(cfg, ctx) == {}


class TestNameLines:
    @pytest.mark.asyncio
    async def test_labels_applied(self):
        lines = [_line("I'm Chris"), _line("Kenni here"), _line("noise")]

        async def _fake(**kwargs):
            return '{"labels": {"0": "Chris", "1": "Kenni"}}'

        out = await speakers.name_lines(
            lines, {"Chris": "consultant", "Kenni": "consultant"}, llm_fn=_fake
        )
        assert out == {0: "Chris", 1: "Kenni"}

    @pytest.mark.asyncio
    async def test_empty_roster_short_circuits(self):
        async def _must_not(**kwargs):
            raise AssertionError("llm should not be called with an empty roster")

        assert await speakers.name_lines([_line("x")], {}, llm_fn=_must_not) == {}

    @pytest.mark.asyncio
    async def test_empty_lines_short_circuits(self):
        async def _must_not(**kwargs):
            raise AssertionError("llm should not be called with no lines")

        assert await speakers.name_lines([], {"A": "client"}, llm_fn=_must_not) == {}

    @pytest.mark.asyncio
    async def test_failure_degrades_to_empty(self):
        async def _boom(**kwargs):
            raise RuntimeError("fast tier down")

        out = await speakers.name_lines([_line("x")], {"A": "client"}, llm_fn=_boom)
        assert out == {}

    @pytest.mark.asyncio
    async def test_out_of_range_index_ignored(self):
        async def _fake(**kwargs):
            return '{"labels": {"5": "X", "0": "A"}}'

        out = await speakers.name_lines([_line("a")], {"A": "client"}, llm_fn=_fake)
        assert out == {0: "A"}

    @pytest.mark.asyncio
    async def test_batch_index_offset(self, monkeypatch):
        monkeypatch.setattr(speakers, "_BATCH_SIZE", 2)
        lines = [_line("a"), _line("b"), _line("c"), _line("d")]

        async def _fake(**kwargs):
            return '{"labels": {"1": "A"}}'  # index 1 within each batch

        out = await speakers.name_lines(lines, {"A": "client"}, llm_fn=_fake)
        assert out == {1: "A", 3: "A"}


class TestNameSpeakersEngine:
    @pytest.mark.asyncio
    async def test_mutates_speaker_labels(self, monkeypatch):
        s = SessionState()
        s.config = SimpleNamespace(
            consultant_names=["Kenni"],
            client_names=[],
            speech=SimpleNamespace(speaker_naming=True),
        )
        line0 = _line("Kenni here")
        line1 = _line("noise")
        s.context = SimpleNamespace(full_transcript=[line0, line1], participants={})

        async def _fake_name_lines(lines, roster, **kwargs):
            return {0: "Kenni"}

        monkeypatch.setattr(
            "sidekick.analyst.speakers.name_lines", _fake_name_lines
        )
        await engine.name_speakers(s)
        assert line0.speaker == "Kenni"
        assert line1.speaker == "(audio)"

    @pytest.mark.asyncio
    async def test_disabled_is_noop(self, monkeypatch):
        s = SessionState()
        s.config = SimpleNamespace(
            consultant_names=["Kenni"],
            client_names=[],
            speech=SimpleNamespace(speaker_naming=False),
        )
        line0 = _line("x")
        s.context = SimpleNamespace(full_transcript=[line0], participants={})
        called = {"v": False}

        async def _fake(lines, roster, **kwargs):
            called["v"] = True
            return {}

        monkeypatch.setattr("sidekick.analyst.speakers.name_lines", _fake)
        await engine.name_speakers(s)
        assert called["v"] is False
        assert line0.speaker == "(audio)"

    @pytest.mark.asyncio
    async def test_no_roster_is_noop(self, monkeypatch):
        s = SessionState()
        s.config = SimpleNamespace(
            consultant_names=[], client_names=[],
            speech=SimpleNamespace(speaker_naming=True),
        )
        s.context = SimpleNamespace(
            full_transcript=[_line("x")], participants={}
        )
        called = {"v": False}

        async def _fake(lines, roster, **kwargs):
            called["v"] = True
            return {}

        monkeypatch.setattr("sidekick.analyst.speakers.name_lines", _fake)
        await engine.name_speakers(s)
        assert called["v"] is False

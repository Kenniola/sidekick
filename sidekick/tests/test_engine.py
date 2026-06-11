"""Characterization tests for the classify/dispatch engine (Phase 2e).

Exercise the orchestration with mocked SessionState components so the
extraction from server.py is verifiably behaviour-preserving.
"""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from sidekick import engine
from sidekick.session_state import SessionState


@dataclass
class _FakeResult:
    action_type: str = "research"
    question: str = "What is the throughput limit?"


def _make_state(action_items=None, results=None) -> SessionState:
    """Build a SessionState with mocked async components."""
    s = SessionState()
    s.config = MagicMock()
    s.config.domains = ["Microsoft Fabric"]
    s.context = MagicMock()
    s.analyst = MagicMock()
    s.analyst.analyse_chunk = AsyncMock(return_value=action_items or [])
    s.queue = MagicMock()
    s.queue.enqueue = AsyncMock()
    s.queue.process_ready = AsyncMock(return_value=results or [])
    s.research = MagicMock()
    s.prototype = MagicMock()
    s.session_log = MagicMock()
    return s


class TestClassifyAndDispatch:
    @pytest.mark.asyncio
    async def test_increments_batch_count(self):
        s = _make_state()
        notify = MagicMock()
        await engine.classify_and_dispatch(s, ["line"], 0, notify)
        assert s.classify_batch_count == 1

    @pytest.mark.asyncio
    async def test_enqueues_each_action_item(self):
        items = ["a", "b", "c"]
        s = _make_state(action_items=items)
        notify = MagicMock()
        await engine.classify_and_dispatch(s, ["line"], 0, notify)
        assert s.queue.enqueue.await_count == 3

    @pytest.mark.asyncio
    async def test_records_and_notifies_each_result(self):
        results = [_FakeResult(), _FakeResult(action_type="prototype")]
        s = _make_state(results=results)
        notify = MagicMock()
        await engine.classify_and_dispatch(s, ["line"], 0, notify)
        assert s.session_log.record.call_count == 2
        assert notify.call_count == 2
        notify.assert_any_call(results[0])

    @pytest.mark.asyncio
    async def test_process_ready_receives_pipelines_and_domains(self):
        s = _make_state()
        notify = MagicMock()
        await engine.classify_and_dispatch(s, ["line"], 0, notify)
        _, kwargs = s.queue.process_ready.call_args
        assert kwargs["research"] is s.research
        assert kwargs["prototype"] is s.prototype
        assert kwargs["context"] is s.context
        assert kwargs["domains"] == ["Microsoft Fabric"]

    @pytest.mark.asyncio
    async def test_detect_domains_runs_on_third_batch(self, monkeypatch):
        s = _make_state()
        called = {"n": 0}

        async def fake_detect(state):
            called["n"] += 1

        monkeypatch.setattr(engine, "detect_domains", fake_detect)
        notify = MagicMock()
        for _ in range(2):
            await engine.classify_and_dispatch(s, ["line"], 0, notify)
        assert called["n"] == 0  # not yet
        await engine.classify_and_dispatch(s, ["line"], 0, notify)
        assert called["n"] == 1  # third batch triggers detection


class TestDetectDomains:
    @pytest.mark.asyncio
    async def test_noop_when_already_detected(self):
        s = SessionState()
        s.domains_detected = True
        s.context = MagicMock()
        s.config = MagicMock()
        await engine.detect_domains(s)
        # nothing to assert beyond no exception; flag stays set
        assert s.domains_detected is True

    @pytest.mark.asyncio
    async def test_noop_when_too_few_lines(self):
        s = SessionState()
        s.context = MagicMock()
        s.context.full_transcript = ["x"] * 5  # < 10
        s.config = MagicMock()
        s.config.domains = []
        await engine.detect_domains(s)
        # too little context: should not mark detected
        assert s.domains_detected is False

    @pytest.mark.asyncio
    async def test_merges_new_domains(self, monkeypatch):
        s = SessionState()
        s.context = MagicMock()
        s.context.full_transcript = [
            type("L", (), {"speaker": "A", "text": f"line {i}"})() for i in range(12)
        ]
        s.config = MagicMock()
        s.config.domains = ["Power BI"]

        async def fake_call_llm(**kwargs):
            return '{"domains": ["Power BI", "Cosmos DB"]}'

        import sidekick.llm as llm_mod

        monkeypatch.setattr(llm_mod, "call_llm", fake_call_llm)
        await engine.detect_domains(s)
        # only the genuinely new domain is appended; existing one not duplicated
        assert "Cosmos DB" in s.config.domains
        assert s.config.domains.count("Power BI") == 1
        assert s.context.detected_domains == ["Cosmos DB"]
        assert s.grounding_cache is None
        assert s.domains_detected is True

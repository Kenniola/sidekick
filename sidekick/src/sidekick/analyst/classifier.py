"""Transcript analyst — LLM-powered classification of meeting transcript chunks."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, fields

from sidekick.analyst.context import MeetingContext
from sidekick.analyst.prompts import ANALYST_SYSTEM_PROMPT
from sidekick.config import SidekickConfig
from sidekick.llm import call_llm

logger = logging.getLogger(__name__)


@dataclass
class ActionItem:
    """A classified action item from transcript analysis."""

    question: str
    type: str                     # research, prototype, roadmap, sizing, diagnostic, action_item, none
    complexity: str               # simple, medium, complex
    priority: str                 # critical, high, medium, low, skip
    priority_score: float = 0.0
    already_answered: bool = False
    consultant_answer_correct: bool | None = None
    correction_needed: bool = False
    correction_detail: str | None = None
    related_to: str | None = None
    relationship_type: str | None = None
    missing_context: str | None = None
    suggest_ask_client: str | None = None
    context_used: list[str] = field(default_factory=list)
    batch_with: str | None = None


@dataclass
class AnalystResponse:
    """Parsed LLM analyst response."""

    items: list[ActionItem]
    phase: str = "core"
    threads_update: list[dict] = field(default_factory=list)

    # Fields accepted by ActionItem — used to filter out unexpected LLM keys
    _ACTION_ITEM_FIELDS = {f.name for f in fields(ActionItem)}

    @classmethod
    def from_json(cls, text: str) -> AnalystResponse:
        # Strip markdown fences the LLM sometimes wraps around JSON
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # Remove opening fence (```json or ```)
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].rstrip()

        data = json.loads(cleaned)

        # Build ActionItems, filtering out any unexpected keys from the LLM
        items = []
        for raw in data.get("items", []):
            filtered = {k: v for k, v in raw.items() if k in cls._ACTION_ITEM_FIELDS}
            # Ensure required fields exist
            if "question" not in filtered or "type" not in filtered:
                logger.warning("Skipping item missing required fields: %s", raw)
                continue
            filtered.setdefault("complexity", "medium")
            filtered.setdefault("priority", "medium")
            try:
                items.append(ActionItem(**filtered))
            except Exception:
                logger.exception("Failed to create ActionItem from: %s", filtered)

        return cls(
            items=items,
            phase=data.get("phase", "core"),
            threads_update=data.get("threads_update", []),
        )


class TranscriptAnalyst:
    """LLM-powered meeting transcript analyser."""

    def __init__(self, config: SidekickConfig, context: MeetingContext | None = None):
        self.config = config
        self.context = context or MeetingContext()

    async def analyse_chunk(self, chunk: list) -> list[ActionItem]:
        """Analyse new transcript lines against meeting context."""
        # Update context with new lines
        self.context.add_lines(chunk)

        prompt = self._build_prompt(chunk)

        response_text = await call_llm(
            system_prompt=ANALYST_SYSTEM_PROMPT,
            user_prompt=prompt,
            json_output=True,
            tier="fast",
        )

        response = AnalystResponse.from_json(response_text)

        # Update phase
        if response.phase:
            self.context.current_phase = response.phase

        # Update threads from LLM response
        for thread_data in response.threads_update:
            tid = thread_data.get("thread_id", "")
            if tid:
                if tid in self.context.threads:
                    t = self.context.threads[tid]
                    t.status = thread_data.get("status", t.status)
                    t.last_active_at = thread_data.get("last_active_at", t.last_active_at)
                    t.questions.extend(thread_data.get("questions", []))
                    t.key_facts.extend(thread_data.get("key_facts", []))
                else:
                    from sidekick.analyst.context import TopicThread
                    self.context.threads[tid] = TopicThread(
                        thread_id=tid,
                        topic=thread_data.get("topic", tid),
                        started_at=thread_data.get("started_at", ""),
                        last_active_at=thread_data.get("last_active_at", ""),
                        status=thread_data.get("status", "open"),
                        questions=thread_data.get("questions", []),
                        key_facts=thread_data.get("key_facts", []),
                    )

        # Filter items based on threshold
        items = []
        for item in response.items:
            if item.priority_score >= self.config.sensitivity.trigger_threshold:
                items.append(item)

        # Record decisions in context
        self.context.record_decisions(items)

        logger.info(
            "Analysed %d lines → %d triggers (phase: %s)",
            len(chunk),
            len(items),
            response.phase,
        )

        return items

    def _build_prompt(self, chunk: list) -> str:
        chunk_text = "\n".join(
            f"[{line.start}] {line.speaker}: {line.text}" for line in chunk
        )
        trigger_text = "\n".join(
            f"- {t.pattern} → {t.action} ({t.grounding})"
            for t in self.config.triggers.client_topics
        )

        # Include injected context summaries if available
        injected_context = ""
        if self.context.context_documents:
            recent_docs = self.context.context_documents[-3:]
            summaries = []
            for doc in recent_docs:
                # First 200 chars of each document
                summaries.append(doc[:200])
            injected_context = "\nINJECTED CONTEXT (from add_context):\n" + "\n---\n".join(summaries)

        return f"""MEETING STATE:
Customer: {self.context.customer_name}
Duration so far: {self.context.elapsed_minutes:.0f} minutes
Domains: {', '.join(self.config.domains)}

PARTICIPANTS:
Consultants: {', '.join(self.config.consultant_names)}
Client-side: {', '.join(self.context.identified_clients) or '(identifying...)'}

ACTIVE THREADS:
{self.context.format_threads()}

OPEN QUESTIONS:
{self.context.format_open_questions()}

ANSWERED QUESTIONS:
{self.context.format_answered_questions()}

RECENT CONVERSATION BUFFER (last ~3 minutes):
{self.context.format_recent_buffer()}{injected_context}

NEW TRANSCRIPT CHUNK (last {self.config.sensitivity.analyst_interval_seconds}s):
{chunk_text}

CUSTOM TRIGGERS:
{trigger_text or '(none configured)'}

Analyse this chunk and return your assessment."""



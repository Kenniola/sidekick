"""Post-call deliverables — customer-ready follow-up email, action-item table,
and a "couldn't answer live" research batch.

Phase 4b. Turns the artefacts Sidekick already has (transcript, threads,
research answers, action items) into a single markdown deliverable produced on
``stop``. The email draft is LLM-generated (deep tier); the action-item table
and follow-up batch are deterministic so they are unit-testable without a
model and never block on a network call.

The LLM call is injected (``llm_fn``) so tests run offline, and every failure
degrades gracefully — ``generate_deliverables`` always returns a string so the
``stop`` summary is never broken by a deliverables error.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from sidekick.config import SidekickConfig, get_output_dir
from sidekick.llm import call_llm
from sidekick.prompt_budget import clip

logger = logging.getLogger(__name__)

LLMFn = Callable[..., Awaitable[str]]

_EMAIL_SYSTEM_PROMPT = (
    "You are a senior consultant drafting a concise, professional follow-up "
    "email to a customer after a meeting. Write in British English. Be warm "
    "but businesslike. Structure: a one-line thank-you, a short 'what we "
    "discussed' summary (3-5 bullets), agreed next steps, and a sign-off "
    "placeholder. Do NOT invent commitments, dates, or names that are not in "
    "the supplied context. Output the email body only — no preamble, no "
    "markdown fences."
)


def _action_item_table(context) -> str:
    """Render ``context.action_items`` as a markdown table (deterministic)."""
    items = getattr(context, "action_items", None) or []
    if not items:
        return "_No action items captured during the session._"

    rows = ["| # | Action | Owner | Due |", "|---|--------|-------|-----|"]
    for i, ai in enumerate(items, 1):
        if isinstance(ai, dict):
            desc = ai.get("description", "").strip() or "(unspecified)"
            owner = (ai.get("owner") or "\u2014").strip()
            due = (ai.get("due") or "\u2014").strip()
        else:
            desc, owner, due = str(ai), "\u2014", "\u2014"
        # Keep table cells single-line.
        desc = desc.replace("\n", " ").replace("|", "\\|")
        rows.append(f"| {i} | {desc} | {owner} | {due} |")
    return "\n".join(rows)


def _unanswered_research_batch(session_log, context) -> str:
    """List questions/threads not resolved live, as a follow-up checklist.

    A question is considered "answered live" if its text matches a recorded
    research/prototype output. Open or blocked threads are also surfaced.
    """
    researched = {
        (o.get("question") or "").strip().lower()
        for o in (getattr(session_log, "outputs", None) or [])
    }

    pending: list[str] = []
    for q in getattr(context, "open_questions", None) or []:
        text = (q.get("question") or "").strip()
        if text and text.lower() not in researched:
            pending.append(text)

    open_threads = [
        t.topic
        for t in (getattr(context, "threads", None) or {}).values()
        if getattr(t, "status", "") in ("open", "blocked")
    ]

    if not pending and not open_threads:
        return "_Everything raised was addressed live \u2014 nothing outstanding._"

    parts: list[str] = []
    if pending:
        parts.append("Questions raised but not answered live (run `research` on each):")
        parts.extend(f"- [ ] {q}" for q in pending)
    if open_threads:
        if parts:
            parts.append("")
        parts.append("Open threads needing follow-up:")
        parts.extend(f"- [ ] {topic}" for topic in open_threads)
    return "\n".join(parts)


def _email_context_block(session_log, context, config) -> str:
    """Assemble a compact, token-budgeted context block for the email prompt."""
    customer = getattr(config, "customer", "the customer")

    facts = getattr(context, "key_facts", None) or []
    facts_block = "\n".join(f"- {f}" for f in facts[-12:]) or "(none captured)"

    threads = getattr(context, "threads", None) or {}
    topics_block = (
        "\n".join(
            f"- {t.topic} ({getattr(t, 'status', 'open')})"
            for t in threads.values()
        )
        or "(none)"
    )

    research_block = "(none)"
    outputs = getattr(session_log, "outputs", None) or []
    research = [o for o in outputs if o.get("action_type") == "research"]
    if research:
        research_block = "\n".join(
            f"- Q: {o.get('question', '')}\n  A: {(o.get('answer') or '')[:200]}"
            for o in research[-8:]
        )

    block = (
        f"Customer: {customer}\n\n"
        f"Key facts established:\n{facts_block}\n\n"
        f"Topics discussed:\n{topics_block}\n\n"
        f"Questions researched live:\n{research_block}"
    )
    return clip(block, 6000, keep="head")


async def _draft_email(session_log, context, config, llm_fn: LLMFn) -> str:
    """LLM-draft the follow-up email. Degrades to a placeholder on failure."""
    context_block = _email_context_block(session_log, context, config)
    try:
        body = await llm_fn(
            system_prompt=_EMAIL_SYSTEM_PROMPT,
            user_prompt=(
                "Draft the follow-up email based only on this meeting "
                f"context:\n\n{context_block}"
            ),
            tier="deep",
            timeout=45.0,
        )
        return body.strip()
    except Exception as e:  # noqa: BLE001 — never break the stop summary
        logger.warning("Deliverables email draft failed: %s", e)
        return (
            "_Email draft unavailable (LLM call failed). Key facts and topics "
            "are summarised above; draft manually._"
        )


async def generate_deliverables(
    session_log,
    context,
    config,
    *,
    llm_fn: LLMFn = call_llm,
) -> str:
    """Produce the full markdown deliverables block for a finished session."""
    customer = getattr(config, "customer", "the customer")
    email = await _draft_email(session_log, context, config, llm_fn)
    actions = _action_item_table(context)
    follow_up = _unanswered_research_batch(session_log, context)

    return "\n".join(
        [
            f"# Post-Call Deliverables \u2014 {customer}",
            "",
            "## Draft Follow-up Email",
            "",
            email,
            "",
            "## Action Items",
            "",
            actions,
            "",
            "## Follow-up Research Batch",
            "",
            follow_up,
            "",
        ]
    )


def save_deliverables(content: str, config: SidekickConfig) -> Path | None:
    """Write the deliverables markdown to the customer output directory."""
    if not getattr(config.output, "auto_save", False):
        return None
    output_dir = get_output_dir(config.customer)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"deliverables_{timestamp}.md"
    path.write_text(content, encoding="utf-8")
    logger.info("Deliverables saved to %s", path)
    return path

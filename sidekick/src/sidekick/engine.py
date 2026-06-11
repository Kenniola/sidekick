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

import logging
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
    )
    for result in results:
        state.session_log.record(result)
        logger.info(
            "Sidekick output: [%s] %s",
            result.action_type,
            result.question[:60],
        )

    # Notify for new findings (sound alert + log file)
    for result in results:
        notify(result)

"""Three-lane priority queue with merging and expiry."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sidekick.analyst.classifier import ActionItem
from sidekick.config import SidekickConfig

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """Result produced by an action pipeline."""

    question: str
    action_type: str
    answer: str = ""
    sources: list[str] = field(default_factory=list)
    confidence: str = "medium"
    priority: str = "medium"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def format(self) -> str:
        return self.answer


@dataclass
class QueueItem:
    """Wrapper around ActionItem with queue metadata."""

    item: ActionItem
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "pending"   # pending, running, done, expired
    merged_items: list[ActionItem] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.item.type}:{self.item.question[:40]}"

    def merge_with(self, other: ActionItem) -> None:
        self.merged_items.append(other)
        logger.info("Merged '%s' into '%s'", other.question[:40], self.id)


class AsyncLane:
    """A concurrency-limited execution lane."""

    def __init__(self, max_concurrent: int, timeout: int):
        self.max_concurrent = max_concurrent
        self.timeout = timeout
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.items: list[QueueItem] = []

    async def submit(self, item: ActionItem) -> None:
        qi = QueueItem(item=item)
        self.items.append(qi)

    def get_pending(self) -> list[QueueItem]:
        return [qi for qi in self.items if qi.status == "pending"]

    def get_running(self) -> list[QueueItem]:
        return [qi for qi in self.items if qi.status == "running"]

    def expire_before(self, cutoff: datetime) -> None:
        for qi in self.items:
            if qi.status == "pending" and qi.enqueued_at < cutoff:
                qi.status = "expired"
                logger.info("Expired stale item: %s", qi.id)


class PriorityQueue:
    """Three-lane priority queue with merging and expiry.

    Lane design:
    - Fast:     max 3 concurrent, 15s timeout — simple lookups
    - Standard: max 2 concurrent, 30s timeout — multi-source research
    - Deep:     max 1 concurrent, 90s timeout — complex prototypes
    """

    def __init__(self, config: SidekickConfig):
        self.fast_lane = AsyncLane(
            max_concurrent=config.queue.fast_lane_max, timeout=15,
        )
        self.standard_lane = AsyncLane(
            max_concurrent=config.queue.standard_lane_max, timeout=30,
        )
        self.deep_lane = AsyncLane(
            max_concurrent=config.queue.deep_lane_max, timeout=90,
        )
        self.completed: list[ActionResult] = []
        self.config = config

    async def enqueue(self, item: ActionItem) -> None:
        """Add an action item to the appropriate lane."""
        # Check for merge candidates
        existing = self._find_merge_candidate(item)
        if existing:
            existing.merge_with(item)
            return

        lane = self._route(item)
        await lane.submit(item)
        logger.info(
            "Enqueued [%s/%s]: %s",
            item.complexity,
            item.priority,
            item.question[:60],
        )

    def _route(self, item: ActionItem) -> AsyncLane:
        if item.complexity == "simple":
            return self.fast_lane
        elif item.complexity == "medium":
            return self.standard_lane
        else:
            return self.deep_lane

    def _find_merge_candidate(self, item: ActionItem) -> QueueItem | None:
        for lane in [self.fast_lane, self.standard_lane, self.deep_lane]:
            for queued in lane.items:
                if queued.status in ("pending", "running"):
                    if (
                        item.batch_with == queued.id
                        or item.related_to == queued.id
                    ):
                        return queued
        return None

    async def process_ready(
        self, research, prototype, context, domains: list[str] | None = None,
    ) -> list[ActionResult]:
        """Process pending items across all lanes. Returns completed results."""
        results: list[ActionResult] = []

        for lane in [self.fast_lane, self.standard_lane, self.deep_lane]:
            pending = lane.get_pending()
            running_count = len(lane.get_running())

            for qi in pending:
                if running_count >= lane.max_concurrent:
                    break

                qi.status = "running"
                running_count += 1

                try:
                    result = await asyncio.wait_for(
                        self._execute(qi, research, prototype, context, domains),
                        timeout=lane.timeout,
                    )
                    qi.status = "done"
                    results.append(result)
                    self.completed.append(result)
                except asyncio.TimeoutError:
                    qi.status = "expired"
                    logger.warning("Timeout on: %s", qi.id)
                except Exception:
                    qi.status = "expired"
                    logger.exception("Error processing: %s", qi.id)

        # Expire stale items
        await self.expire_stale()

        return results

    async def _execute(
        self, qi: QueueItem, research, prototype, context,
        domains: list[str] | None = None,
    ) -> ActionResult:
        """Route a queue item to the correct action pipeline."""
        item = qi.item
        action_type = item.type
        tier = "deep"

        if action_type == "research":
            result = await research.execute_direct(
                question=item.question,
                depth="medium" if item.complexity != "simple" else "quick",
                context=context,
                tier=tier,
                domains=domains,
            )
            return ActionResult(
                question=item.question,
                action_type="research",
                answer=result.format() if hasattr(result, "format") else str(result),
                sources=getattr(result, "sources", []),
                confidence=getattr(result, "confidence", "medium"),
                priority=item.priority,
            )
        elif action_type in ("roadmap", "sizing", "diagnostic", "action_item"):
            # Route through research pipeline — these benefit from
            # grounded answers rather than placeholder responses
            result = await research.execute_direct(
                question=item.question,
                depth="medium",
                context=context,
                tier=tier,
                domains=domains,
            )
            return ActionResult(
                question=item.question,
                action_type=action_type,
                answer=result.format() if hasattr(result, "format") else str(result),
                sources=getattr(result, "sources", []),
                confidence=getattr(result, "confidence", "medium"),
                priority=item.priority,
            )
        elif action_type == "prototype":
            result = await prototype.execute_direct(
                description=item.question,
                prototype_type="notebook",
                context=context,
            )
            return ActionResult(
                question=item.question,
                action_type="prototype",
                answer=result.format() if hasattr(result, "format") else str(result),
                priority=item.priority,
            )
        else:
            # Unknown type — still route through research as a fallback
            result = await research.execute_direct(
                question=item.question,
                depth="quick",
                context=context,
                tier=tier,
                domains=domains,
            )
            return ActionResult(
                question=item.question,
                action_type=action_type,
                answer=result.format() if hasattr(result, "format") else str(result),
                sources=getattr(result, "sources", []),
                priority=item.priority,
            )

    async def expire_stale(self, minutes: int = 5) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        for lane in [self.fast_lane, self.standard_lane, self.deep_lane]:
            lane.expire_before(cutoff)

    def get_in_progress(self) -> list[QueueItem]:
        result = []
        for lane in [self.fast_lane, self.standard_lane, self.deep_lane]:
            result.extend(lane.get_running())
        return result

    def format_status(self) -> str:
        parts = []
        for name, lane in [
            ("Fast", self.fast_lane),
            ("Standard", self.standard_lane),
            ("Deep", self.deep_lane),
        ]:
            pending = len(lane.get_pending())
            running = len(lane.get_running())
            if pending or running:
                parts.append(f"  {name}: {running} running, {pending} pending")
        return "\n".join(parts) if parts else "  (empty)"

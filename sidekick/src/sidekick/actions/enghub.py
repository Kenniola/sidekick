"""Eng Hub integration — surface VBD/IP offerings during live meetings.

Searches the Eng Hub Resource Center (eng.ms) for relevant VBD, EDE, DE,
and WorkshopPLUS offerings based on topics being discussed.

The search is domain-agnostic — it queries the full Resource Center so
colleagues in any solution area (Infra, Apps, AI, Data, Security, etc.)
get relevant results without needing hardcoded topic mappings.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# A search callable injected by the host. Given a query string it returns a
# list of result records, each at minimum ``{"title": str, "url": str}`` (the
# field map verified against live EngineeringHub results). The host supplies an
# *authenticated* implementation (e.g. wrapping the EngineeringHub MCP server's
# search tool); sidekick itself holds no eng.ms credentials and makes no eng.ms
# HTTP calls.
SearchFn = Callable[[str], Awaitable[list[dict]]]


class EngHubAuthError(RuntimeError):
    """Raised when no authenticated eng.ms search source is available.

    eng.ms is gated behind Entra ID and the sidekick server process has no way
    to authenticate to it directly. Live offerings are therefore only available
    when the host injects an authenticated search callable. When none is wired,
    this error is raised so the absence of live data surfaces explicitly rather
    than as an empty or fabricated result.
    """

# Offering types and their short labels
OFFERING_TYPES = {
    "poc": "Proof of Concept",
    "adr": "Architecture Design & Review",
    "so": "Solution Optimization",
    "wsplus": "WorkshopPLUS",
    "ue": "Upskilling Execution",
    "arp": "Learning Path",
    "oa": "Onboarding Accelerator",
    "de": "Designated Engineering",
}


@dataclass
class EngHubOffering:
    """A VBD/IP offering surfaced from Eng Hub."""

    title: str
    url: str
    offering_type: str = ""
    solution_play: str = ""
    relevance: str = ""
    source: str = "live"        # provenance: "live" = eng.ms API. Set
                                # explicitly so a consumer can never mistake
                                # non-live data for authoritative results.

    def format_brief(self) -> str:
        type_label = OFFERING_TYPES.get(self.offering_type, self.offering_type.upper())
        return f"  [{type_label}] {self.title}\n    {self.url}"


@dataclass
class EngHubResult:
    """Result from an Eng Hub search."""

    topic: str
    offerings: list[EngHubOffering] = field(default_factory=list)
    error: str = ""

    def format(self) -> str:
        if self.error:
            return f"Eng Hub search error: {self.error}"
        if not self.offerings:
            return f"No VBD/IP offerings found for: {self.topic}"

        lines = [f"VBD/IP Offerings relevant to \"{self.topic}\":"]
        for o in self.offerings:
            lines.append(o.format_brief())
        return "\n".join(lines)


class EngHubPipeline:
    """Surface relevant VBD/IP offerings from the Eng Hub Resource Center.

    The sidekick server process cannot authenticate to eng.ms itself, so it
    makes no eng.ms HTTP calls. Instead the host injects an authenticated
    ``search_fn`` (typically wrapping the EngineeringHub MCP server's search
    tool). When no ``search_fn`` is wired, searches return an auth-required
    error rather than empty or fabricated data.
    """

    def __init__(self, search_fn: SearchFn | None = None) -> None:
        self._search_fn = search_fn

    def set_search_fn(self, search_fn: SearchFn | None) -> None:
        """Wire (or clear) the authenticated search callable.

        Lets the host attach a search source to a module-level singleton after
        import.
        """
        self._search_fn = search_fn

    async def search(self, topic: str, domains: list[str] | None = None) -> EngHubResult:
        """Search for VBD/IP offerings relevant to a topic.

        Args:
            topic: The topic from the meeting transcript (e.g. "lakehouse
                   architecture", "Azure Landing Zone", "Copilot agents").
            domains: Optional customer domains to append as search context.
        """
        # Build a search query that combines the topic with domain context
        query_parts = [topic]
        if domains:
            query_parts.extend(domains[:3])
        search_query = " ".join(query_parts)

        try:
            offerings = await self._search_enghub(search_query)
        except EngHubAuthError as e:
            logger.warning("Eng Hub auth required for '%s': %s", topic, e)
            return EngHubResult(topic=topic, error=f"auth required — {e}")
        except Exception as e:
            logger.warning("Eng Hub search failed for '%s': %s", topic, e)
            return EngHubResult(topic=topic, error=str(e))

        # Sort by offering type priority (PoCs and ADRs first)
        type_priority = {"poc": 0, "adr": 1, "so": 2, "wsplus": 3, "ue": 4, "oa": 5, "arp": 6}
        offerings.sort(key=lambda o: type_priority.get(o.offering_type, 99))

        return EngHubResult(topic=topic, offerings=offerings[:8])

    async def _search_enghub(self, query: str) -> list[EngHubOffering]:
        """Run the injected search and parse results into offerings.

        The injected ``search_fn`` returns a list of records, each at minimum
        ``{"title": str, "url": str}`` — the field map verified against live
        EngineeringHub results. Offering type and solution play are inferred
        from the URL/title since the source carries no such field.

        Raises:
            EngHubAuthError: if no authenticated ``search_fn`` is wired, so the
                absence of live data surfaces explicitly rather than as an empty
                or fabricated result.
        """
        if self._search_fn is None:
            raise EngHubAuthError(
                "no authenticated eng.ms search source wired — the host must "
                "inject a search callable (e.g. via the EngineeringHub MCP)"
            )

        records = await self._search_fn(query)

        offerings = []
        for item in records:
            title = item.get("title", "")
            url = item.get("url", "")
            if not title or not url:
                continue
            offerings.append(EngHubOffering(
                title=title,
                url=url,
                offering_type=self._classify_offering_type(url, title),
                solution_play=self._extract_solution_play(url),
                source="live",
            ))

        return offerings

    @staticmethod
    def _classify_offering_type(url: str, title: str) -> str:
        """Classify the offering type from URL path segments or title.

        URL/title tokens were verified against live EngineeringHub results,
        e.g. ``/wsplus/`` and ``workshopplus-`` (WorkshopPLUS), ``learningpath``
        (learning path) and ``deliveryguide`` (delivery guide).
        """
        url_lower = url.lower()
        title_lower = title.lower()

        for code in OFFERING_TYPES:
            if f"/{code}/" in url_lower or url_lower.endswith(f"/{code}"):
                return code
            if OFFERING_TYPES[code].lower() in title_lower:
                return code

        # URL/title tokens that don't map to an OFFERING_TYPES code directly.
        if "wsplus" in url_lower or "workshopplus" in url_lower or "workshop" in title_lower:
            return "wsplus"
        if "learningpath" in url_lower or "learning path" in title_lower:
            return "arp"
        if "deliveryguide" in url_lower or "delivery guide" in title_lower:
            return "so"
        return ""

    @staticmethod
    def _extract_solution_play(url: str) -> str:
        """Extract the solution play from the URL path."""
        url_lower = url.lower()
        if "/sp01/" in url_lower:
            return "SP01 — Migrate & Modernize"
        if "/sp02/" in url_lower:
            return "SP02 — AI Apps & Agents"
        if "/sp03/" in url_lower:
            return "SP03 — Unify Data Platform"
        if "/vasenhanced/" in url_lower:
            return "VAS Enhanced"
        return ""

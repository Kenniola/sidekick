"""Eng Hub integration — surface VBD/IP offerings during live meetings.

Searches the Eng Hub Resource Center (eng.ms) for relevant VBD, EDE, DE,
and WorkshopPLUS offerings based on topics being discussed.

The search is domain-agnostic — it queries the full Resource Center so
colleagues in any solution area (Infra, Apps, AI, Data, Security, etc.)
get relevant results without needing hardcoded topic mappings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# Eng Hub search API — requires Microsoft corp auth (Entra ID)
ENGHUB_SEARCH_URL = "https://eng.ms/api/search/v2"

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
    """Search Eng Hub Resource Center for relevant VBD/IP offerings.

    Queries the eng.ms search API directly (same API the Eng Hub MCP server
    wraps). The search is unscoped — the API's own relevance ranking covers
    all solution plays and offering types across the full Resource Center.

    Requires Microsoft corp network / Entra ID authentication.
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=15.0)

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
        except Exception as e:
            logger.warning("Eng Hub search failed for '%s': %s", topic, e)
            return EngHubResult(topic=topic, error=str(e))

        # Sort by offering type priority (PoCs and ADRs first)
        type_priority = {"poc": 0, "adr": 1, "so": 2, "wsplus": 3, "ue": 4, "oa": 5, "arp": 6}
        offerings.sort(key=lambda o: type_priority.get(o.offering_type, 99))

        return EngHubResult(topic=topic, offerings=offerings[:8])

    async def _search_enghub(self, query: str) -> list[EngHubOffering]:
        """Search the Eng Hub API and parse results into offerings."""
        params = {"search": query, "$top": 15}

        resp = await self._client.get(
            ENGHUB_SEARCH_URL, params=params, follow_redirects=True,
        )

        if resp.status_code == 401 or resp.status_code == 403:
            logger.info("Eng Hub API returned %s — auth required (corp net / Entra ID)", resp.status_code)
            return []

        if resp.status_code != 200:
            logger.warning("Eng Hub API returned %s", resp.status_code)
            return []

        data = resp.json()
        offerings = []
        for item in data.get("value", data.get("results", [])):
            title = item.get("title", item.get("name", ""))
            url = item.get("url", "")
            if not title or not url:
                continue
            offerings.append(EngHubOffering(
                title=title,
                url=url,
                offering_type=self._classify_offering_type(url, title),
                solution_play=self._extract_solution_play(url),
            ))

        return offerings

    @staticmethod
    def _classify_offering_type(url: str, title: str) -> str:
        """Classify the offering type from URL path segments or title."""
        url_lower = url.lower()
        title_lower = title.lower()

        for code in OFFERING_TYPES:
            if f"/{code}/" in url_lower or url_lower.endswith(f"/{code}"):
                return code
            if OFFERING_TYPES[code].lower() in title_lower:
                return code

        if "workshop" in title_lower:
            return "wsplus"
        if "delivery guide" in title_lower:
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

"""Eng Hub integration — surface VBD/IP offerings during live meetings.

Searches for relevant VBD, EDE, DE, and WorkshopPLUS offerings based on
topics being discussed. URL mappings are loaded from a local config file
(~/.sidekick/enghub_catalog.yaml) to avoid embedding internal URLs in code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

logger = logging.getLogger(__name__)

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


def _load_catalog() -> dict:
    """Load the Eng Hub catalog from ~/.sidekick/enghub_catalog.yaml.

    Returns dict with keys: search_url, rc_base, topics, curated_offerings.
    Returns empty dict if file not found.
    """
    catalog_path = Path.home() / ".sidekick" / "enghub_catalog.yaml"
    if not catalog_path.exists():
        logger.info("No enghub_catalog.yaml found — Eng Hub features disabled")
        return {}
    try:
        with open(catalog_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("Failed to load enghub_catalog.yaml: %s", e)
        return {}


# Load catalog once at import time
_CATALOG = _load_catalog()


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

    URL mappings and curated offerings are loaded from
    ~/.sidekick/enghub_catalog.yaml. If the file is missing, all search
    methods return empty results gracefully.
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=15.0)
        self._search_url = _CATALOG.get("search_url", "")
        self._rc_base = _CATALOG.get("rc_base", "")
        self._topics = _CATALOG.get("topics", {})
        self._curated = _CATALOG.get("curated_offerings", [])

    async def search(self, topic: str, domains: list[str] | None = None) -> EngHubResult:
        """Search for VBD/IP offerings relevant to a topic."""
        if not _CATALOG:
            return EngHubResult(topic=topic)

        matched_categories = self._match_categories(topic, domains)

        if not matched_categories:
            matched_categories = [{"terms": [topic], "path": self._rc_base}]

        all_offerings: dict[str, EngHubOffering] = {}

        for category in matched_categories[:3]:
            for search_term in category["terms"][:2]:
                try:
                    results = await self._search_enghub(search_term, category["path"])
                    for offering in results:
                        if offering.url not in all_offerings:
                            all_offerings[offering.url] = offering
                except Exception as e:
                    logger.warning("Eng Hub search failed for '%s': %s", search_term, e)

        type_priority = {"poc": 0, "adr": 1, "so": 2, "wsplus": 3, "ue": 4, "oa": 5, "arp": 6}
        sorted_offerings = sorted(
            all_offerings.values(),
            key=lambda o: type_priority.get(o.offering_type, 99),
        )

        return EngHubResult(topic=topic, offerings=sorted_offerings[:8])

    def _match_categories(
        self, topic: str, domains: list[str] | None = None,
    ) -> list[dict]:
        """Match topic text to known keyword categories."""
        topic_lower = topic.lower()
        domain_text = " ".join(d.lower() for d in (domains or []))

        scored: list[tuple[int, dict]] = []
        for keyword, category in self._topics.items():
            score = 0
            if keyword in topic_lower:
                score += 3
            for word in keyword.split():
                if word in topic_lower:
                    score += 1
            if keyword in domain_text:
                score += 1
            if score > 0:
                scored.append((score, category))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [cat for _, cat in scored]

    async def _search_enghub(
        self, query: str, url_path: str,
    ) -> list[EngHubOffering]:
        """Search the Eng Hub API and parse results into offerings."""
        if not self._search_url:
            return self._curated_fallback(query)

        params = {"search": query, "urlPath": url_path, "$top": 10}

        try:
            resp = await self._client.get(
                self._search_url, params=params, follow_redirects=True,
            )
            if resp.status_code != 200:
                return self._curated_fallback(query)

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

        except (httpx.HTTPError, Exception) as e:
            logger.debug("Eng Hub API call failed: %s — using curated fallback", e)
            return self._curated_fallback(query)

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

    def _curated_fallback(self, query: str) -> list[EngHubOffering]:
        """Return curated offerings when the API isn't reachable."""
        query_lower = query.lower()
        results: list[EngHubOffering] = []

        for offering in self._curated:
            score = sum(
                1 for kw in offering.get("keywords", [])
                if kw.lower() in query_lower
            )
            if score > 0:
                results.append(EngHubOffering(
                    title=offering["title"],
                    url=offering.get("url", ""),
                    offering_type=offering.get("type", ""),
                    solution_play=offering.get("sp", ""),
                ))
        return results

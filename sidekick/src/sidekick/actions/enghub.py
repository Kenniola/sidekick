"""Eng Hub integration — surface VBD/IP offerings during live meetings.

Searches the Cloud & AI Platforms Resource Center (eng.ms) for relevant
VBD, EDE, DE, and WorkshopPLUS offerings based on topics being discussed.

URL structure (SP03 = Unify Your Data Platform):
  .../azure-engagement-resource-center/sp03/03-microsoftfabric/...
  .../azure-engagement-resource-center/sp03/01-databases/...
  .../azure-engagement-resource-center/sp03/05-deployingpowerbianalytics/...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

ENGHUB_SEARCH_URL = (
    "https://eng.ms/api/search"
)

# Resource Center base URL for scoped searches
RC_BASE = (
    "https://eng.ms/docs/microsoft-customer-partner-solutions-mcaps/"
    "customer-experience-and-support/asd-management/og-management/"
    "ppe-resource-center-repos/azure-engagement-resource-center"
)

# Topic → search terms + solution play path mapping
TOPIC_KEYWORDS: dict[str, dict] = {
    "fabric": {
        "terms": ["Microsoft Fabric", "Fabric VBD", "Fabric PoC", "Fabric workshop"],
        "path": f"{RC_BASE}/sp03/03-microsoftfabric",
    },
    "lakehouse": {
        "terms": ["Fabric lakehouse", "data engineering", "Fabric PoC data prep"],
        "path": f"{RC_BASE}/sp03/03-microsoftfabric",
    },
    "warehouse": {
        "terms": ["Fabric data warehouse", "Synapse", "SQL analytics"],
        "path": f"{RC_BASE}/sp03/03-microsoftfabric",
    },
    "power bi": {
        "terms": ["Power BI analytics", "deploying Power BI", "Power BI VBD"],
        "path": f"{RC_BASE}/sp03/05-deployingpowerbianalytics",
    },
    "directlake": {
        "terms": ["DirectLake", "Power BI Fabric", "semantic model"],
        "path": f"{RC_BASE}/sp03/03-microsoftfabric",
    },
    "real-time": {
        "terms": ["real-time analytics", "Eventhouse", "KQL", "Fabric streaming"],
        "path": f"{RC_BASE}/sp03/03-microsoftfabric",
    },
    "migration": {
        "terms": ["migrate modernize", "Synapse migration", "database migration"],
        "path": f"{RC_BASE}/sp03",
    },
    "database": {
        "terms": ["Azure SQL", "database migration", "SQL Managed Instance", "PostgreSQL"],
        "path": f"{RC_BASE}/sp03/01-databases",
    },
    "databricks": {
        "terms": ["Azure Databricks", "Databricks VBD"],
        "path": f"{RC_BASE}/sp03/02-azuredatabricks",
    },
    "governance": {
        "terms": ["Purview", "data governance", "information protection", "DLP"],
        "path": RC_BASE,
    },
    "ai": {
        "terms": ["AI apps agents", "Azure AI Foundry", "AI production readiness"],
        "path": f"{RC_BASE}/sp02",
    },
    "security": {
        "terms": ["security VBD", "Sentinel", "Defender", "zero trust"],
        "path": RC_BASE,
    },
    "cosmos": {
        "terms": ["Cosmos DB", "CosmosDB migration"],
        "path": f"{RC_BASE}/sp03/01-databases/cosmosdb",
    },
    "sap": {
        "terms": ["SAP data integration", "SAP extend innovate"],
        "path": f"{RC_BASE}/sp01/sap",
    },
    "landing zone": {
        "terms": ["Azure Landing Zone", "ALZ", "cloud adoption framework"],
        "path": f"{RC_BASE}/sp01",
    },
    "data science": {
        "terms": ["Fabric data science", "machine learning", "MLOps"],
        "path": f"{RC_BASE}/sp03/03-microsoftfabric",
    },
}

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
    offering_type: str = ""     # poc, adr, so, wsplus, etc.
    solution_play: str = ""     # SP01, SP02, SP03
    relevance: str = ""         # why this is relevant to the current topic

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

    This does NOT call the Eng Hub MCP server directly — it uses the same
    public search API that the MCP server wraps.  When running inside
    Sidekick's MCP server, we avoid MCP-to-MCP calls (which would require
    a separate client transport).  Instead we hit the API directly.

    The pipeline:
    1. Match the meeting topic to known keyword categories
    2. Search the Resource Center for offerings in those categories
    3. Classify results by offering type (PoC, ADR, SO, WorkshopPLUS, etc.)
    4. Return formatted results for the consultant
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=15.0)

    async def search(self, topic: str, domains: list[str] | None = None) -> EngHubResult:
        """Search for VBD/IP offerings relevant to a topic.

        Args:
            topic: The topic from the meeting transcript (e.g. "lakehouse architecture",
                   "capacity sizing", "Power BI DirectLake")
            domains: Optional list of domains from customer config to narrow scope
        """
        # 1. Identify matching keyword categories
        matched_categories = self._match_categories(topic, domains)

        if not matched_categories:
            # Fallback: broad search across the whole Resource Center
            matched_categories = [{"terms": [topic], "path": RC_BASE}]

        # 2. Search across matched categories (deduplicate by URL)
        all_offerings: dict[str, EngHubOffering] = {}

        for category in matched_categories[:3]:  # Limit to top 3 categories
            for search_term in category["terms"][:2]:  # Top 2 terms per category
                try:
                    results = await self._search_enghub(search_term, category["path"])
                    for offering in results:
                        if offering.url not in all_offerings:
                            all_offerings[offering.url] = offering
                except Exception as e:
                    logger.warning("Eng Hub search failed for '%s': %s", search_term, e)

        # 3. Sort by relevance (offering type priority)
        type_priority = {"poc": 0, "adr": 1, "so": 2, "wsplus": 3, "ue": 4, "oa": 5, "arp": 6}
        sorted_offerings = sorted(
            all_offerings.values(),
            key=lambda o: type_priority.get(o.offering_type, 99),
        )

        return EngHubResult(
            topic=topic,
            offerings=sorted_offerings[:8],  # Cap at 8 results
        )

    def _match_categories(
        self, topic: str, domains: list[str] | None = None,
    ) -> list[dict]:
        """Match topic text to known keyword categories."""
        topic_lower = topic.lower()
        domain_text = " ".join(d.lower() for d in (domains or []))

        scored: list[tuple[int, dict]] = []
        for keyword, category in TOPIC_KEYWORDS.items():
            score = 0
            if keyword in topic_lower:
                score += 3
            # Check individual words
            for word in keyword.split():
                if word in topic_lower:
                    score += 1
            # Boost if domain matches
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
        # Use the Eng Hub search API
        # The API is the same one the MCP server wraps
        params = {
            "search": query,
            "urlPath": url_path,
            "$top": 10,
        }

        try:
            resp = await self._client.get(
                "https://eng.ms/api/search/v2",
                params=params,
                follow_redirects=True,
            )
            # If the API isn't directly accessible, fall back to curated results
            if resp.status_code != 200:
                return self._curated_fallback(query)

            data = resp.json()
            offerings = []
            for item in data.get("value", data.get("results", [])):
                title = item.get("title", item.get("name", ""))
                url = item.get("url", "")
                if not title or not url:
                    continue

                offering_type = self._classify_offering_type(url, title)
                solution_play = self._extract_solution_play(url)

                offerings.append(EngHubOffering(
                    title=title,
                    url=url,
                    offering_type=offering_type,
                    solution_play=solution_play,
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
            # Check URL path segments
            if f"/{code}/" in url_lower or url_lower.endswith(f"/{code}"):
                return code
            # Check title keywords
            label = OFFERING_TYPES[code].lower()
            if label in title_lower:
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
        """Return curated offerings when the API isn't reachable.

        These are the known Fabric/Data Platform offerings from the Resource
        Center, hand-mapped from our Eng Hub exploration.
        """
        query_lower = query.lower()
        results: list[EngHubOffering] = []

        for offering in _CURATED_OFFERINGS:
            # Score by keyword match
            score = sum(
                1 for kw in offering["keywords"]
                if kw.lower() in query_lower
            )
            if score > 0:
                results.append(EngHubOffering(
                    title=offering["title"],
                    url=offering["url"],
                    offering_type=offering["type"],
                    solution_play=offering["sp"],
                ))

        return results


# Curated offerings from the Resource Center (fallback when API unavailable)
_CURATED_OFFERINGS = [
    # SP03 — Microsoft Fabric
    {
        "title": "Microsoft Fabric — Learning Path",
        "url": f"{RC_BASE}/sp03/03-microsoftfabric/arp",
        "type": "arp",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["fabric", "lakehouse", "warehouse", "data engineering", "onelake"],
    },
    {
        "title": "Fabric WorkshopPLUS — Learning Path",
        "url": f"{RC_BASE}/sp03/03-microsoftfabric/wsplus/arp",
        "type": "wsplus",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["fabric", "workshop", "training", "upskilling"],
    },
    {
        "title": "Fabric PoC — Data Prep (Data Engineering)",
        "url": f"{RC_BASE}/sp03/03-microsoftfabric/poc/dataprep",
        "type": "poc",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["fabric", "lakehouse", "data engineering", "spark", "notebook", "medallion", "bronze", "silver", "gold", "ingestion", "etl"],
    },
    {
        "title": "Fabric PoC — Power BI (DirectLake & Semantic Models)",
        "url": f"{RC_BASE}/sp03/03-microsoftfabric/poc/powerbi",
        "type": "poc",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["power bi", "directlake", "semantic model", "report", "dashboard", "dax"],
    },
    {
        "title": "Fabric PoC — Data Science",
        "url": f"{RC_BASE}/sp03/03-microsoftfabric/poc/datascience",
        "type": "poc",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["data science", "machine learning", "ml", "prediction", "model"],
    },
    # SP03 — Power BI (standalone)
    {
        "title": "Deploying Power BI Analytics — Proof of Concept",
        "url": f"{RC_BASE}/sp03/05-deployingpowerbianalytics/01-nwpbianalytic/proofofconcept",
        "type": "poc",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["power bi", "analytics", "report", "dashboard", "deployment"],
    },
    {
        "title": "Deploying Power BI Analytics — Upskilling Execution",
        "url": f"{RC_BASE}/sp03/05-deployingpowerbianalytics/01-nwpbianalytic/upskillingexec",
        "type": "ue",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["power bi", "training", "upskilling", "workshop"],
    },
    # SP03 — Databases
    {
        "title": "Database Migration & Modernization — Architecture Design & Review",
        "url": f"{RC_BASE}/sp03/01-databases/adr",
        "type": "adr",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["database", "migration", "sql", "modernize", "azure sql"],
    },
    {
        "title": "Database Migration & Modernization — Proof of Concept",
        "url": f"{RC_BASE}/sp03/01-databases/poc",
        "type": "poc",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["database", "migration", "sql", "poc"],
    },
    {
        "title": "Database Migration & Modernization — Solution Optimization",
        "url": f"{RC_BASE}/sp03/01-databases/so",
        "type": "so",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["database", "optimization", "sql", "performance"],
    },
    {
        "title": "Database Migration — WorkshopPLUS",
        "url": f"{RC_BASE}/sp03/01-databases/wsplus",
        "type": "wsplus",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["database", "migration", "workshop", "sql"],
    },
    {
        "title": "Cosmos DB — Proof of Concept",
        "url": f"{RC_BASE}/sp03/01-databases/cosmosdb/poc",
        "type": "poc",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["cosmos", "cosmosdb", "nosql", "document"],
    },
    {
        "title": "Azure SQL Managed Instance — Solution Optimization",
        "url": f"{RC_BASE}/sp03/01-databases/sql-mi/so",
        "type": "so",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["sql managed instance", "sql mi", "migration"],
    },
    {
        "title": "Azure SQL Database — Solution Optimization",
        "url": f"{RC_BASE}/sp03/01-databases/sql-db/so",
        "type": "so",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["azure sql", "sql database", "optimization"],
    },
    {
        "title": "PostgreSQL / MySQL — Proof of Concept",
        "url": f"{RC_BASE}/sp03/01-databases/mysql/poc",
        "type": "poc",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["postgresql", "mysql", "open source", "migration"],
    },
    # SP03 — Databricks
    {
        "title": "Azure Databricks — Architecture Design & Review",
        "url": f"{RC_BASE}/sp03/02-azuredatabricks/adr",
        "type": "adr",
        "sp": "SP03 — Unify Data Platform",
        "keywords": ["databricks", "spark", "delta lake", "unity catalog"],
    },
    # SP02 — AI Apps & Agents
    {
        "title": "AI Production Readiness Assessment — Learning Path",
        "url": f"{RC_BASE}/sp02/01-aiapps/assess-prod-lp",
        "type": "arp",
        "sp": "SP02 — AI Apps & Agents",
        "keywords": ["ai", "agent", "foundry", "copilot", "openai", "gpt"],
    },
    {
        "title": "VAS Enhanced — AI Apps & Agents Learning Path",
        "url": f"{RC_BASE}/vasenhanced/sp02/5-arp",
        "type": "arp",
        "sp": "SP02 — AI Apps & Agents",
        "keywords": ["ai", "agent", "enhanced", "vas"],
    },
    {
        "title": "Microsoft Agent Factory — Technical Readiness Assessment",
        "url": f"{RC_BASE}/sp02/05-agentfactory/businessscenariosalignmentwithmicrosoftagentfactory-deliveryguide",
        "type": "adr",
        "sp": "SP02 — AI Apps & Agents",
        "keywords": ["agent", "agent factory", "ai agent", "foundry"],
    },
    # SP01 — SAP
    {
        "title": "SAP Data Integration — Architecture Design & Review",
        "url": f"{RC_BASE}/sp01/sap/adr/deliveryguide_sei_data",
        "type": "adr",
        "sp": "SP01 — Migrate & Modernize",
        "keywords": ["sap", "data integration", "extract"],
    },
    {
        "title": "SAP Extend & Innovate for Data — Proof of Concept",
        "url": f"{RC_BASE}/sp01/sap/poc/deliveryguide_sei_data",
        "type": "poc",
        "sp": "SP01 — Migrate & Modernize",
        "keywords": ["sap", "extend", "innovate", "poc"],
    },
    # Security (cross-cutting)
    {
        "title": "Microsoft Purview — Information Protection Delivery Guide",
        "url": "https://eng.ms/docs/microsoft-customer-partner-solutions-mcaps/customer-experience-and-support/asd-management/security/resource-center/vbd/security/datasecurity/deliveryguides/oa_informationprotection",
        "type": "oa",
        "sp": "Security",
        "keywords": ["purview", "information protection", "sensitivity labels", "classification"],
    },
    {
        "title": "Microsoft Purview — Data Loss Prevention Delivery Guide",
        "url": "https://eng.ms/docs/microsoft-customer-partner-solutions-mcaps/customer-experience-and-support/asd-management/security/resource-center/vbd/security/datasecurity/deliveryguides/oa_dlp",
        "type": "oa",
        "sp": "Security",
        "keywords": ["purview", "dlp", "data loss prevention", "compliance"],
    },
    {
        "title": "Microsoft Sentinel — Onboarding Accelerator (Design)",
        "url": "https://eng.ms/docs/microsoft-customer-partner-solutions-mcaps/customer-experience-and-support/asd-management/security/resource-center/vbd/sentinel/oa/design/design-dg",
        "type": "oa",
        "sp": "Security",
        "keywords": ["sentinel", "siem", "security operations", "soc"],
    },
]

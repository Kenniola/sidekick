"""Research pipeline — multi-source search and synthesis."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from sidekick.actions.enghub import EngHubPipeline
from sidekick.llm import call_llm

logger = logging.getLogger(__name__)

_enghub = EngHubPipeline()

SYNTHESIS_SYSTEM_PROMPT = """You are a {domain_scope} technical research assistant \
embedded in a live customer engagement. Your answers are read aloud by the \
consultant on the call, so they must be specific, actionable, and anchored to \
the customer's actual situation.

RULES:
- Lead with the direct answer in 1-2 sentences
- ALWAYS include a "Sources:" section at the end with numbered URLs
- State confidence: HIGH (verified docs), MEDIUM (partial match), LOW (inference)
- Flag GA vs Preview vs Planned features
- State uncertainty explicitly when docs are ambiguous
- Keep it brief — the consultant is on a live call
- Reference the customer by name when relevant
- Tie recommendations back to the customer's active threads and domains

VERIFIED SOURCES (priority order):
1. Microsoft Learn — learn.microsoft.com
2. Microsoft Fabric Blog — blog.fabric.microsoft.com
3. Microsoft Fabric Roadmap — roadmap.fabric.microsoft.com
4. Databricks / Delta Lake docs — docs.databricks.com, docs.delta.io
5. Apache Spark docs — spark.apache.org/docs
6. AWS docs — docs.aws.amazon.com (for cross-cloud topics)
7. Workspace files — engagement artifacts, instruction files

OUTPUT FORMAT:
<direct answer>

Sources:
1. <title> — <URL>
2. <title> — <URL>

Only cite sources you are confident are reputable. Never fabricate URLs. \
If web results provide URLs, use those. If no URLs are available, state \
\"Sources: Based on training knowledge (no live URLs retrieved).\""""


@dataclass
class ResearchResult:
    """Result from the research pipeline."""

    question: str
    answer: str = ""
    sources: list[str] = field(default_factory=list)
    confidence: str = "medium"

    def format(self) -> str:
        source_text = "\n".join(f"  \u2022 {s}" for s in self.sources) if self.sources else "  (none)"
        return f"""{self.answer}

Sources [{self.confidence.upper()}]:
{source_text}"""


class ResearchPipeline:
    """Multi-source research pipeline.

    Searches:
    1. Workspace files (engagement artifacts from grounding.repo_paths)
    2. Instruction files (.github/instructions/)
    3. LLM synthesis with context from above
    """

    def __init__(self, config=None):
        self._repo_paths = (
            config.grounding.repo_paths if config else [".github/instructions/"]
        )
        # Resolve paths relative to workspace root (not CWD)
        self._workspace_root = Path(
            os.environ.get("SIDEKICK_WORKSPACE_ROOT", ".")
        )

    async def execute_direct(
        self,
        question: str,
        depth: str = "medium",
        context=None,
        tier: str = "deep",
        domains: list[str] | None = None,
    ) -> ResearchResult:
        """Execute a research query directly (not from queue).

        Args:
            tier: LLM tier for synthesis — 'deep' (claude-opus-4.7) by default.
            domains: Customer domains for scoping EngHub search.
        """
        # Rewrite the raw transcript question into a focused search query
        search_query = await self._rewrite_search_query(question, domains)

        # Gather context from the workspace
        repo_context = self._search_repo(search_query)
        instruction_context = self._search_instructions(search_query)

        # Gather live web context from MS Learn and verified sources
        web_context = await self._search_web(search_query)

        # Search Eng Hub for relevant VBD/IP offerings
        enghub_context = ""
        try:
            enghub_result = await _enghub.search(
                question,
                domains=domains,
            )
            if enghub_result.offerings:
                enghub_context = enghub_result.format()
        except Exception as e:
            logger.debug("Eng Hub search skipped: %s", e)

        # Build the synthesis prompt
        offerings_block = ""
        if enghub_context:
            offerings_block = f"""

VBD/IP OFFERINGS (from Eng Hub Resource Center):
{enghub_context}

If any offerings are directly relevant to the question, mention them \
in a brief "Relevant Offerings:" section after your answer. Only include \
offerings that genuinely match the topic — do not force-fit."""

        # Build customer engagement context
        customer_block = ""
        if context:
            customer_name = getattr(context, "customer_name", "") or ""
            threads = getattr(context, "threads", {})
            key_facts = getattr(context, "key_facts", [])
            thread_details = []
            for t in threads.values():
                detail = f"  - [{t.status}] {t.topic}"
                for kf in t.key_facts[:2]:
                    detail += f"\n      fact: {kf}"
                for q in t.questions[:2]:
                    detail += f"\n      question: {q}"
                thread_details.append(detail)
            parts = []
            if customer_name:
                parts.append(f"Customer: {customer_name}")
            if thread_details:
                parts.append("Active threads:\n" + "\n".join(thread_details))
            if key_facts:
                parts.append("Key facts:\n" + "\n".join(f"  - {f}" for f in key_facts[-8:]))
            if parts:
                customer_block = f"\n\nCUSTOMER ENGAGEMENT:\n" + "\n".join(parts)

        user_prompt = f"""QUESTION: {question}

DEPTH: {depth}{customer_block}

WEB RESULTS (from verified sources):
{web_context}

WORKSPACE CONTEXT:
{repo_context}

TEAM STANDARDS:
{instruction_context}

MEETING CONTEXT:
{self._format_meeting_context(context)}{offerings_block}

Research this question and provide a concise, sourced answer. \
Anchor your response to the customer's specific context where possible. \
Cite the URLs from web results where they support your answer."""

        # Scope the system prompt to actual domains being discussed
        domain_scope = ", ".join(domains) if domains else "Microsoft Fabric"
        system_prompt = SYNTHESIS_SYSTEM_PROMPT.format(domain_scope=domain_scope)

        answer = await call_llm(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tier=tier,
        )

        # Extract source URLs from web results for the ResearchResult
        sources = self._extract_urls(web_context)

        return ResearchResult(
            question=question,
            answer=answer,
            sources=sources,
            confidence="medium",
        )

    async def _rewrite_search_query(
        self, question: str, domains: list[str] | None = None,
    ) -> str:
        """Rewrite a raw transcript question into a focused search query.

        Transcript questions are often incomplete, verbose, or contain
        filler. This distills them into 3-6 keyword phrases that work
        well with MS Learn search and file keyword matching.
        """
        domain_hint = ", ".join(domains) if domains else "Microsoft Fabric"
        try:
            rewritten = await call_llm(
                system_prompt=(
                    "You convert meeting transcript questions into concise "
                    "Microsoft Learn search queries. Output ONLY the search "
                    "query — no explanation, no quotes. Use 3-8 words "
                    "separated by spaces. Focus on the core technical "
                    "concept being asked about."
                ),
                user_prompt=(
                    f"Domain: {domain_hint}\n"
                    f"Transcript question: {question}\n"
                    f"Search query:"
                ),
                tier="fast",
                timeout=8,
            )
            rewritten = rewritten.strip().strip('"').strip("'")
            # Accept if it's a reasonable length (not empty, not a full paragraph)
            if 5 <= len(rewritten) <= 120:
                logger.debug("Search query rewritten: %r → %r", question, rewritten)
                return rewritten
        except Exception as e:
            logger.debug("Query rewrite failed, using original: %s", e)
        return question

    def _search_repo(self, question: str) -> str:
        """Search configured repo paths for relevant engagement artifacts."""
        keywords = [w for w in question.lower().split() if len(w) > 3]
        if not keywords:
            return "(no search terms extracted)"

        results: list[tuple[int, str]] = []

        for repo_path_str in self._repo_paths:
            repo_path = self._workspace_root / repo_path_str
            if not repo_path.exists():
                continue

            # Skip instruction files (covered by _search_instructions)
            if repo_path_str.rstrip("/").endswith("instructions"):
                continue

            # Search markdown, text, and SQL files
            for suffix in ("*.md", "*.txt", "*.sql"):
                for f in repo_path.rglob(suffix):
                    try:
                        content = f.read_text(encoding="utf-8")
                        name_lower = f.name.lower()
                        preview = content[:1000].lower()

                        score = sum(
                            1 for kw in keywords
                            if kw in name_lower or kw in preview
                        )
                        if score > 0:
                            snippet = content[:300].strip()
                            rel = f.relative_to(self._workspace_root)
                            results.append((score, f"--- {rel} ---\n{snippet}"))
                    except Exception:
                        continue

            # Index notebooks by name only (avoid parsing large JSON)
            for f in repo_path.rglob("*.ipynb"):
                name_lower = f.name.lower()
                score = sum(1 for kw in keywords if kw in name_lower)
                if score > 0:
                    rel = f.relative_to(self._workspace_root)
                    results.append((score, f"--- {rel} (notebook) ---"))

        if not results:
            return "(no matching repo files)"

        # Sort by relevance score descending, take top 5
        results.sort(key=lambda x: x[0], reverse=True)
        return "\n\n".join(text for _, text in results[:5])

    def _search_instructions(self, question: str) -> str:
        """Search .github/instructions/ for relevant team standards."""
        instructions_dir = self._workspace_root / ".github" / "instructions"
        if not instructions_dir.exists():
            return "(no instruction files found)"

        keywords = [w.lower() for w in question.split() if len(w) > 3]
        if not keywords:
            return "(no search terms)"

        scored: list[tuple[int, str]] = []
        for f in instructions_dir.glob("*.instructions.md"):
            try:
                content = f.read_text(encoding="utf-8")
                name_lower = f.stem.lower()
                preview = content[:1500].lower()

                # Score by keyword hits in both filename and content
                score = sum(
                    2 if kw in name_lower else (1 if kw in preview else 0)
                    for kw in keywords
                )
                if score > 0:
                    scored.append((score, f"--- {f.name} ---\n{content[:500]}"))
            except Exception:
                continue

        if not scored:
            return "(no matching instructions)"

        scored.sort(key=lambda x: x[0], reverse=True)
        return "\n\n".join(text for _, text in scored[:3])

    def _format_meeting_context(self, context) -> str:
        if not context:
            return "(no meeting context)"
        facts = getattr(context, "key_facts", [])
        return "\n".join(f"- {f}" for f in facts) if facts else "(no key facts yet)"

    async def _search_web(self, question: str) -> str:
        """Search verified sources via the MS Learn search API and Bing site-scoped search.

        Uses the free MS Learn search API (no key required) and optionally
        Bing Web Search API for broader verified sources.

        Returns formatted snippets with URLs for the LLM to cite.
        """
        results: list[str] = []

        # 1. Microsoft Learn search (free, no API key)
        try:
            learn_results = await self._search_ms_learn(question)
            results.extend(learn_results)
        except Exception as e:
            logger.warning("MS Learn search failed: %s", e)

        # 2. Bing Web Search (if API key configured) — scoped to verified domains
        bing_key = os.environ.get("BING_SEARCH_KEY", "")
        if bing_key:
            try:
                bing_results = await self._search_bing(question, bing_key)
                results.extend(bing_results)
            except Exception as e:
                logger.warning("Bing search failed: %s", e)

        if not results:
            return "(no web results — LLM will use training knowledge)"

        return "\n\n".join(results[:8])

    @staticmethod
    def _extract_urls(web_context: str) -> list[str]:
        """Extract URLs from web context string for the sources list."""
        urls = []
        for line in web_context.split("\n"):
            line = line.strip()
            if line.startswith("URL:"):
                url = line[4:].strip()
                if url:
                    urls.append(url)
        return urls

    async def _search_ms_learn(self, question: str) -> list[str]:
        """Search Microsoft Learn documentation via the free search API."""
        url = "https://learn.microsoft.com/api/search"
        params = {
            "search": question,
            "locale": "en-us",
            "$top": "8",
        }
        results = []
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("results", [])[:8]:
                title = item.get("title", "")
                snippet = str(item.get("description", ""))[:200]
                link = item.get("url", "")
                if title and link and self._is_useful_url(link):
                    results.append(f"[MS Learn] {title}\n  URL: {link}\n  {snippet}")

        return results[:5]

    @staticmethod
    def _is_useful_url(url: str) -> bool:
        """Filter out broad landing pages and certification guides."""
        # Reject root-level landing pages with very short paths
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        path_segments = [s for s in path.split("/") if s]
        # Reject if path has fewer than 3 segments (e.g. /en-us/fabric/)
        if len(path_segments) < 3:
            return False
        # Reject certification/training pages
        reject_patterns = [
            "/training/", "/certifications/", "/credentials/",
            "/learn/paths/", "/study-guide",
        ]
        path_lower = path.lower()
        return not any(p in path_lower for p in reject_patterns)

    async def _search_bing(self, question: str, api_key: str) -> list[str]:
        """Search verified domains via Bing Web Search API v7.

        Scopes results to: learn.microsoft.com, blog.fabric.microsoft.com,
        docs.databricks.com, docs.delta.io, spark.apache.org, docs.aws.amazon.com.
        """
        site_query = (
            f"{question} ("
            "site:learn.microsoft.com OR "
            "site:blog.fabric.microsoft.com OR "
            "site:docs.databricks.com OR "
            "site:docs.delta.io OR "
            "site:spark.apache.org OR "
            "site:docs.aws.amazon.com"
            ")"
        )
        url = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": api_key}
        params = {"q": site_query, "count": "5", "mkt": "en-GB"}

        results = []
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)
            resp.raise_for_status()
            data = resp.json()

            for page in data.get("webPages", {}).get("value", [])[:5]:
                title = page.get("name", "")
                snippet = page.get("snippet", "")[:200]
                link = page.get("url", "")
                if title and link:
                    results.append(f"[Web] {title}\n  URL: {link}\n  {snippet}")

        return results

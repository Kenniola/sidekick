# Changelog

All notable changes to sidekick-copilot are documented in this file.

---

## [0.2.0] — 2026-06-09

### Added

- **`add_context` tool** — inject text, files (.md/.txt/.py/.json/.yaml/.sql/.csv/.xml, 4KB cap), or images (.png/.jpg/.gif/.webp/.bmp, 10MB cap via vision LLM) into the live session. Injected context appears in classifier prompts (last 3 docs, 200 chars each) and grounding context (last 5 docs, 1500 chars each).
- **Domain auto-detection** — fast-tier LLM analyses first 30 transcript lines at classifier batch 3 to detect technology domains. Detected domains merge with config-specified domains and invalidate the grounding cache.
- **Thread detection rules** — explicit `THREAD DETECTION RULES` section in the analyst system prompt guides topic-shift detection, granular thread creation, and thread lifecycle management.
- **Semantic dedup in priority queue** — `_find_completed_duplicate()` compares new questions against last 10 completed outputs via fast-tier LLM. Duplicates are re-researched with enriched context (previous answer appended) rather than skipped.
- **URL filtering for MS Learn** — `_is_useful_url()` rejects shallow URLs (<3 path segments) and training/certification/study-guide pages. Research fetches 8 results, filters, returns top 5.
- **Content-aware instruction search** — `_search_instructions()` now reads first 1500 chars of file content (not just filenames) with weighted scoring (2× filename, 1× content match).
- **Grounding cache** — 5-minute TTL cache on `_build_grounding_context()` via `asyncio.to_thread()`, invalidated on domain detection.
- **`context_documents` field** on `MeetingContext` — accumulates injected context for use by classifier and grounding.
- **`detected_domains` field** on `MeetingContext` — stores auto-detected domains from transcript analysis.

### Changed

- **Shared httpx clients** — `llm.py` now uses global `httpx.AsyncClient` instances per endpoint (`_copilot_client`, `_github_models_client`) with connection pooling, replacing per-call client creation.
- **Parallel I/O in `suggest_questions`** — Eng Hub search and grounding context load concurrently via `asyncio.gather()`. Eng Hub wrapped in `asyncio.wait_for(timeout=10.0)`.
- **Dynamic prompt scoping** — hardcoded "Microsoft Fabric" replaced with `{domain_scope}` template variable in `SYNTHESIS_SYSTEM_PROMPT`, `ANALYST_SYSTEM_PROMPT`, and `CONSULTANT_ADVISOR_PROMPT`. Populated from detected or config-specified domains at runtime.
- **Classifier prompt enrichment** — `_build_prompt()` now includes last 3 `context_documents` (200 chars each) in an `INJECTED CONTEXT` section.
- **Research `_search_ms_learn()`** — fetches 8 results (was 5), applies `_is_useful_url()` filter, returns top 5.
- Tool count: 7 → 8 (added `add_context`).

### Fixed

- Per-call `httpx.AsyncClient` creation causing connection overhead on every LLM call.
- Instruction search matching only filenames, missing relevant content in instruction files.
- Grounding context blocking the event loop (now runs in thread pool).

---

## [0.1.0] — 2026-06-02

### Added

- Initial release with 7 tools: `listen`, `suggest_questions`, `research`, `offerings`, `prototype`, `status`, `stop`.
- WASAPI loopback audio capture with silence detection and auto-stop.
- Whisper (local CPU) and Azure Speech (Entra ID, speaker diarization) backends.
- 3-lane async priority queue (fast/standard/deep) with merge and expiry.
- Multi-source research pipeline: workspace docs, `.github/instructions/`, Microsoft Learn.
- Eng Hub Resource Center VBD/IP offering search.
- 7-step chain-of-thought consultant advisor in `suggest_questions`.
- Proactive notifications via winsound + alerts.jsonl + sidekick-notify extension.
- Customer config profiles with deep merge over defaults.
- Session log saved to `~/.sidekick/outputs/<customer>/` on stop.
- Copilot API primary LLM with GitHub Models fallback, 3-tier routing (fast/standard/deep).

# Changelog

All notable changes to sidekick-copilot are documented in this file.

---

## [Unreleased]

### Added

- **Config-driven LLM models** — per-tier model fallback chains are now defined in `configs/default.yaml` under a new `models:` block (`fast` / `standard` / `deep`, each an ordered list of `"provider:model"` strings) instead of being hard-coded in `llm.py`. A new `ModelsConfig` dataclass resolves them, and `set_active_models()` registers the active config so every `call_llm(tier=…)` call honours it without threading config through each call site.
  - **Env override:** `SIDEKICK_MODEL_<TIER>` (e.g. `SIDEKICK_MODEL_DEEP="copilot:claude-opus-4.8,copilot:gpt-4.1"`) swaps a tier's chain at runtime with no YAML edit.
  - **`sidekick models [profile]`** CLI command prints the resolved chain per tier (showing primary vs fallback and any active env override).
  - `call_llm()` gains an optional `chain` parameter for explicit overrides (used by tests). Code defaults in `llm._TIER_CONFIG` are preserved as the standalone fallback. `tests/test_models_config.py` covers parsing, defaults, env override, YAML override, and `call_llm` integration.
  - **Note:** both providers (`copilot`, `github_models`) assume an OpenAI-compatible `/chat/completions` shape. Genuinely different APIs (Anthropic-native, Azure OpenAI) would need a per-provider adapter — not yet implemented.

### Changed

- **`install.ps1`** package source is no longer a `TODO` — it defaults to the private Git repo and honours a `SIDEKICK_REPO_URL` env override. The same URL is centralised in `server.py` as `_REPO_URL` / `_install_hint()` so the install hint and installer stay in sync.

### Fixed

- **Removed stale Azure Speech branches** left over from the v0.3.0 removal: the `listen` banner and `status` tool referenced `_config.speech.azure_region` (no longer a field) behind a now-unreachable `backend == "azure"` guard, and the `listen` docstring still said "Whisper or Azure". Backend label is now simply "Whisper (local)".

### Performance

- **LLM connection pre-warm** — `listen` now kicks off a best-effort `llm.prewarm()` task that acquires the GitHub token and opens a pooled TLS connection to the Copilot host, so the first classifier/research call skips DNS + TCP + TLS setup. All failures are swallowed.

---

## [0.3.0] — 2026-06-10

### Removed

- **Azure Speech backend** — entirely removed from the codebase, including `AzureSpeechRecogniser`, the `[azure]` install extra, `azure-identity` and `azure-cognitiveservices-speech` dependencies, all `AZURE_SPEECH_*` environment variable handling, `SpeechConfig.azure_*` fields, `speaker_map`, the installer's `azure` feature flag, and the `Azure Speech (Optional)` section from `README.md` and `INSTALL.md`.
  - **Rationale:** real-meeting transcript analysis showed (1) diarization had been silently disabled (the code used `SpeechRecognizer` instead of `ConversationTranscriber` due to `SPXERR_INVALID_ARG` with Entra ID auth), and (2) Azure Speech's only practical advantage over local Whisper was lost. For HMRC/MoJ engagements, the on-device privacy posture of Whisper is also decisive.
  - **Migration:** customer YAML profiles with `backend: azure` are auto-rewritten to `whisper` at load time and a warning is logged. Delete the now-unused `AZURE_SPEECH_*` lines from `~/.sidekick/.env`.
- **`offerings` tool and all Eng Hub integration** — removed the `offerings` MCP tool, the `EngHubPipeline` module (`actions/enghub.py`) and its tests, the proactive offerings background search, the `suggest_questions` offerings fetch, and every offerings reference in the analyst prompt chain, README, and agent definition. Sidekick is now seven tools.
  - **Rationale:** eng.ms is Entra-gated and the sidekick server process cannot authenticate to it. A sidekick (MCP server) cannot call the EngineeringHub (MCP server) directly — both only talk to the host/client — so live offerings would require building and owning a bespoke bridge process that is brittle (hardwired to EngHub's schema) and re-introduces the auth complexity removed alongside Azure Speech. The feature delivered no live value and the "auth required" placeholder code was misleading. Topic-relevant delivery guidance is better surfaced by the `research` tool against verified web sources.

### Changed

- **Default Whisper model upgraded from `base.en` to `small.en`** (~150MB → ~470MB, WER ~8-10% → ~5-7%). Real-world transcripts of a 72-minute consulting call showed `base.en` produced visible misrecognitions on technical jargon (Fabric, OneLake, capacity SKUs); `small.en` resolves these.
- **`SpeechRecogniser` Protocol now accepts a `chunk_start_offset: float = 0.0` parameter** so segment timestamps are session-relative (`HH:MM:SS.mmm` reflecting position within the meeting) rather than chunk-relative (always in `[0, 5s]`).
- **`server.py` listen loop now tracks `listen_started_at`** and computes `chunk_start_offset = max(0, time.monotonic() - listen_started_at - chunk_duration)` for every transcription call.
- `SpeechConfig` fields simplified to `backend`, `language`, `model`, `compute_type`.
- `install.ps1` `-Features` parameter restricted to `live` (only supported value).
- `pyproject.toml` `[all]` extra now expands to `[live, dev]`.

### Fixed

- **Chunk-relative transcript timestamps** — segments from every 5-second buffer previously displayed `00:00.000 → 00:05.000` regardless of when in the meeting they occurred, making transcript review of long sessions impossible. All segment timestamps are now meeting-wall-clock-relative.
- `_format_ts()` clamps negative values to `0.0` instead of producing malformed strings.

### Added

- **Per-domain source routing in `research`** — live web results now flow through a single ranker that filters to a verified-source trust map (`_SOURCE_TRUST`) and boosts the question's detected domain's preferred sources (`_DOMAIN_ROUTING`). Microsoft properties keep the highest baseline (engagement verification rule), but an AWS/Databricks/Spark/PostgreSQL question lifts those docs above their baseline so they can rank alongside Microsoft. Non-allowlisted hosts are dropped, so only verified URLs are ever surfaced for citation. New `tests/test_research_routing.py` covers filtering, default Microsoft priority, AWS promotion, dedup, host-anchored matching, and config extension.
- **Live web-search provider (replaces retired Bing)** — `research` now calls [Tavily](https://tavily.com) (`TAVILY_API_KEY`) or, as a fallback, the [Brave Search API](https://brave.com/search/api/) (`BRAVE_API_KEY`), selected by whichever key is present. With no key set, research still runs against the free Microsoft Learn API. Tavily requests are scoped to the verified-source allowlist via `include_domains`; Brave results are filtered post-hoc by the same ranker.
- **`grounding.extra_trusted_domains`** config option (`{host: weight}`) lets a customer profile add or re-weight a verified source without editing code.
- `SIDEKICK_WHISPER_COMPUTE` environment variable (`int8` default; `int8_float16` / `float16` / `float32` supported).
- `tests/test_speech_recogniser.py` regression suite covering `_format_ts`, factory backend fallback, and `chunk_start_offset` propagation.
- **Config-driven notification sound** — new `notifications.sound` setting in `default.yaml` / customer profiles. Accepts `silent`, `chime` (default, standard Windows notification via `MessageBeep(MB_OK)`), `asterisk`, `exclamation`, or `beep` (legacy 800 Hz / 200 ms tone). Replaces the hard-coded 1 kHz / 300 ms `winsound.Beep`, which played at system master volume with no way to soften it. The new default chime respects the **Notification volume** slider in Windows Sound Settings.

---

## [0.2.0] — 2026-06-09

### Added

- **`add_context` tool** — inject text, files (.md/.txt/.json/.yaml/.yml/.csv/.sql, 4000-char cap), or images (.png/.jpg/.jpeg/.gif/.webp, 10MB cap via vision LLM) into the live session. Injected context appears in classifier prompts (last 3 docs, 200 chars each) and grounding context (last 5 docs, 1500 chars each).
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

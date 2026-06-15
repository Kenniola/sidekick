# Sidekick — Engineering & Product Assessment

> **Author:** Fresh-eyes architectural review
> **Date:** 11 June 2026
> **Version reviewed:** 0.3.0 (commit `2fea49d`, `main`)
> **Scope:** Full codebase (~3,900 LOC Python), distribution, value proposition, roadmap

---

## 1. Executive Summary

Sidekick is a **genuinely differentiated** real-time meeting co-pilot, not "another LLM chat window." Its defining trait is that it **acts autonomously on what it hears** — capturing live system audio, transcribing on-device, classifying questions/hedges/threads, and running grounded research + code generation in the background while the consultant stays in the conversation. That closed loop (audio → classify → prioritise → research → surface) is the moat. A chat window is pull; Sidekick is push.

The engineering is above prototype grade: tiered LLM routing with fallback, a 3-lane concurrency queue with merge/dedup, on-device Whisper for compliance, connection-pooled HTTP, a grounding cache, and a real installer. It works as a CSA sidekick **today** for the core loop.

But it carries the scars of fast iteration: a **1,175-line `server.py` monolith**, **stale Azure code that is a latent crash**, **orphaned build artifacts**, **thin automated test coverage** (3 test files for 7 tools + 4 subsystems), and — most relevant to your explicit ask — **hardcoded model selection** that should move into config. None of these are fatal; all are addressable in a focused cleanup pass.

**Verdict:** Strong concept, solid bones, needs a consolidation + hardening sprint before it scales beyond you to the wider team.

| Dimension | Rating | One-line |
|-----------|:------:|----------|
| Differentiation / "killer" factor | 🟢 Strong | Autonomous push loop is real and rare |
| Does it work as a CSA sidekick | 🟢 Yes | Core loop delivers; some rough edges |
| Code health / maintainability | 🟡 Mixed | Monolith + dead code + thin tests |
| Distribution / install | 🟢 Good | One-liner + uvx; minor polish needed |
| Performance | 🟡 Good, headroom | Whisper + LLM latency are the levers |
| Model configurability | 🔴 Gap | Hardcoded — your request; design below |

---

## 2. Original Value Proposition vs. What Was Built

**The team-challenge premise:** an AI that makes a Cloud Solution Architect materially better *during* a live customer call — surfacing verified answers, suggesting sharper questions, and producing artifacts in the moment, without the CSA breaking eye contact to go Googling.

**Delivered against that premise:**

| Promise | Status | Evidence |
|---------|:------:|----------|
| Listens to the call | ✅ | WASAPI loopback capture, 5s chunks, on-device Whisper |
| Researches questions it hears | ✅ | Classifier → priority queue → research pipeline (MS Learn + verified web) |
| Suggests what to ask | ✅ | `suggest_questions` — 6-step chain-of-thought advisor |
| Generates prototypes live | ✅ | `prototype` — PySpark/T-SQL/DAX/pipeline |
| Doesn't leak customer data | ✅ | On-device STT, Copilot/GitHub-token auth, verified-URL-only web |
| Surfaces findings without being asked | 🟡 | Clever piggyback (`_get_unseen_findings`) — but limited by MCP's no-push constraint |

**Honest gap:** MCP servers can't initiate messages to the client. Sidekick works around this by **prepending unseen findings to the next tool response** and by writing `alerts.jsonl` for the `sidekick-notify` VS Code extension to toast. This is a smart mitigation, but it means truly *proactive* surfacing depends on the extension being installed and on the user occasionally calling a tool. It's the single biggest "is this magic or not" hinge, and it's only partially solved.

**Does it stand out?** Yes — but the differentiation is **experiential**, not visible in a feature list. The first time it answers a question 8 seconds after a client asks it, the value is obvious. That means the demo and the notification path are disproportionately important to perceived value. Invest there.

---

## 3. Tool & Feature Audit

Seven tools, each reviewed:

| Tool | Verdict | Notes |
|------|:------:|-------|
| `listen` | 🟢 Solid | Consent gate is good practice. **Contains stale Azure crash** (§5.1). Returns instantly; heavy init in background — correct. |
| `suggest_questions` | 🟢 Strong | The crown jewel. Deep-tier, phase-aware, grounded, returns corrections + observations. Heavy prompt though — latency-sensitive. |
| `add_context` | 🟢 Strong | Text/file/image with vision LLM. Genuinely useful for "what's on screen." Good caps (4k chars / 10MB). |
| `research` | 🟢 Strong | Now domain-routed with verified-source trust map. Well-built post the recent work. |
| `prototype` | 🟡 Adequate | Smallest pipeline (82 LOC). Always `deep` tier. No validation/lint of generated code. Type→language map is hardcoded but fine. |
| `status` | 🟢 Solid | Good incremental + full view. **Stale Azure crash** here too (§5.1). |
| `stop` | 🟢 Solid | Careful teardown ordering (stop capture before cancel task) avoids C-level crashes — this is hard-won correctness. |

**Cross-cutting strengths:**
- Every tool prepends `_get_unseen_findings()` — consistent surfacing.
- The classifier→queue→pipeline split is clean separation of concerns.
- Domain auto-detection (batch 3) is a nice touch that adapts grounding to the actual conversation.

**Cross-cutting weaknesses:**
- Tool docstrings still say "Whisper or Azure" — drift from reality.
- No tool returns structured data the agent can reason over programmatically; all return preformatted strings. Fine for now, limiting later.

---

## 4. Architecture & Simplification

```
listen ──▶ _run_listen_loop (background task)
              │  audio_capture → speech_recogniser (Whisper)
              ▼
        _classify_and_dispatch ──▶ TranscriptAnalyst (fast tier)
              │                         │ ActionItems
              ▼                         ▼
        PriorityQueue (3 lanes) ──▶ ResearchPipeline / PrototypePipeline (standard/deep)
              │                         │ ActionResult
              ▼                         ▼
        SessionLog.record ──▶ _notify (winsound + alerts.jsonl)
              ▲
        every tool call ──▶ _get_unseen_findings() prepend
```

The layering is fundamentally sound: `transcript` / `analyst` / `queue` / `actions` / `output` / `config` / `llm` are well-separated packages. The problem is **`server.py` (1,175 lines)** which is doing far too much:

1. All global mutable state (~15 module globals)
2. The entire background listen loop
3. Domain detection
4. Notification logic
5. The grounding-context builder (~120 lines of file I/O)
6. All 7 tool definitions

**Simplification opportunities (high value, low risk):**

| Action | Benefit |
|--------|---------|
| Extract `_build_grounding_context` + cache → `grounding.py` | −130 lines from server; testable in isolation |
| Extract listen loop + domain detection → `engine.py` (or `session.py`) | server.py becomes thin tool definitions |
| Wrap the ~15 globals in a single `SessionState` dataclass | Removes `global` soup; enables multiple/again sessions; far easier to test |
| Extract `_notify` → `output/notifier.py` | Cohesion; testable |
| Move tool bodies that are >40 lines (`suggest_questions`, `add_context`) into the relevant pipeline module, leaving the `@server.tool()` as a thin adapter | server.py becomes a registration surface only |

Target: `server.py` under ~300 lines (tool registration + wiring). This is the single highest-leverage maintainability change.

**Can the codebase be simpler overall?** Yes, but mostly by *consolidation*, not deletion — the feature set is lean already. The win is structural, not subtractive.

---

## 5. Technical Debt (prioritised)

### 5.1 🔴 HIGH — Stale Azure code is a latent crash
`server.py` references `_config.speech.azure_region` in **two** places (the `listen` banner ~L683 and `status` ~L1044), but `SpeechConfig` no longer has that field (removed in 0.3.0). The branch is guarded by `backend == "azure"`, and the config loader rewrites `azure`→`whisper`, so it's currently unreachable — but it's a tripwire: any code path or test that sets `backend="azure"` raises `AttributeError`. Remove both branches and the "Whisper or Azure" docstring on `listen`. `speech_recogniser.py` already handles the fallback correctly; these UI strings are pure residue.

### 5.2 🟡 MEDIUM — Orphaned build artifacts
Six orphaned `.pyc` files for deleted modules remain in `__pycache__`: `enghub`, `roadmap`, `transcript_correction`, `formatter`, `file_watcher`, `vtt_parser` (plus `test_enghub_auth`). They're harmless at runtime but signal churn and can confuse `import` debugging. Confirm `__pycache__/` is gitignored (it should be) and clear them. The existence of `roadmap`/`vtt_parser`/`file_watcher`/`formatter`/`transcript_correction` pyc's also tells us **five modules were deleted without a tracking note** — worth a one-line CHANGELOG mention for provenance.

### 5.3 🟡 MEDIUM — Thin automated test coverage
3 test files (`test_notifications_config`, `test_research_routing`, `test_speech_recogniser`) for a system with 7 tools and 6 subsystems. **Untested:** the priority queue (routing, merge, dedup, expiry, timeout), config loading/deep-merge, the LLM tier routing + fallback chain, the classifier JSON parsing, session log/summary generation, and every server tool. The queue and LLM fallback are the highest-risk untested areas because they're concurrency- and network-dependent.

### 5.4 🟢 LOW — Inconsistent JSON-fence stripping
The "strip ```json fences" logic is duplicated in `classifier.py`, `server.py` (suggest_questions), `priority_queue.py`, and `server.py` (detect_domains), each slightly different (`.strip("`").lstrip("json\n")` vs. fence-line splitting). Extract one `parse_llm_json(text) -> dict` helper. Low risk, removes 4 copies of fiddly code.

### 5.5 🟢 LOW — `install.ps1` TODO + repo coupling
`install.ps1` still has `# TODO: Replace with actual repo URL or PyPI name when decided` and hardcodes the GitHub subdirectory URL in three places (installer, and two `listen`/error strings in server.py). Decide PyPI vs git now and centralise.

### 5.6 🟢 LOW — Broad `except Exception` swallowing
Several places catch-and-pass (`_notify`, grounding loaders, dedup). Acceptable for non-critical background paths, but the dedup and domain-detection swallow errors silently at `debug` level, which will make field diagnosis hard. Consider a single structured "background error" sink surfaced in `status`.

---

## 6. Model Configurability (your explicit request)

**Current state:** `llm.py` hardcodes everything in `_TIER_CONFIG`:

```python
_TIER_CONFIG = {
  "fast":     [("copilot","gpt-4o-mini"), ("github_models","gpt-4.1-mini")],
  "standard": [("copilot","claude-sonnet-4.5"), ("copilot","gpt-4.1"), ...],
  "deep":     [("copilot","claude-opus-4.7"), ("copilot","claude-opus-4.6"), ...],
}
```

To swap a model tomorrow you must edit source. That's the gap.

**Proposed design — `models:` section in config, defaults in code, env override.**

Add to `default.yaml` (and therefore overridable per customer profile):

```yaml
models:
  # Each tier is an ordered fallback chain of "provider:model".
  # First entry is primary; the rest are tried on 429/5xx/timeout.
  fast:     ["copilot:gpt-4o-mini", "github_models:gpt-4.1-mini"]
  standard: ["copilot:claude-sonnet-4.5", "copilot:gpt-4.1", "github_models:gpt-4.1-mini"]
  deep:     ["copilot:claude-opus-4.7", "copilot:claude-opus-4.6", "github_models:DeepSeek-R1"]
  providers:                      # optional — add a new endpoint without code
    copilot:        { url: "https://api.githubcopilot.com/chat/completions", auth: gh_token, headers: { Copilot-Integration-Id: vscode-chat } }
    github_models:  { url: "https://models.inference.ai.azure.com/chat/completions", auth: gh_token }
```

Implementation steps (small, contained):
1. New `@dataclass ModelsConfig` with `fast/standard/deep: list[str]` and an optional `providers: dict`. Add to `SidekickConfig`. Keep the current hardcoded chains as the dataclass **defaults** so nothing breaks if `models:` is absent.
2. `llm.py` `call_llm(...)` gains an optional `chain: list[tuple[str,str]] | None`. Callers pass `config.models.<tier>`. Parse `"provider:model"` strings into the existing `(provider, model)` tuples.
3. Provider registry becomes data-driven: `_PROVIDERS` built from `config.models.providers` merged over code defaults, so a new OpenAI-compatible endpoint is *config-only*.
4. Env overrides for quick experiments: `SIDEKICK_MODEL_DEEP="copilot:claude-opus-4.8"` wins over config for that tier.
5. Add `sidekick models` CLI subcommand to print the resolved chains (debuggability).

This gives you: per-customer model policy, instant model swaps via `.env`, and new providers without touching code — while preserving today's behaviour as the default. **Recommended as the first feature after the cleanup pass**, since it's exactly your stated need and unblocks experimentation.

> ⚠️ Note: `call_llm` currently assumes an **OpenAI-compatible** `/chat/completions` shape for both providers. A genuinely different API (e.g. Anthropic native, Azure OpenAI with deployment names) needs an adapter function per provider, not just a URL. The config schema above anticipates this via the `providers` block, but the adapter work is the real effort. Flag this before promising "any model."

---

## 7. Distribution & Install

**Strengths:** One-liner (`irm … | iex`), a true zero-install `uvx` path, isolated `uv tool` env, ARM64 handled via x64-Python emulation, auto-registers MCP + installs the notify extension + deploys the agent definition, and a clean `uninstall`. This is better than most internal tools achieve.

**Frictions / risks:**

| Issue | Impact | Fix |
|-------|--------|-----|
| Private GitHub repo in install URL | Anyone you share with needs repo access | Publish to internal PyPI/Azure Artifacts, or make repo readable to the team |
| `gh auth login` prerequisite | First-run confusion (installer exits and asks you to re-run) | Acceptable, but document prominently; consider detecting + prompting inline |
| The stale-agent-definition trap (you hit this) | Agent behaves per old instructions after an `agent.md` change | `sidekick init` **does** overwrite the deployed agent (`_install_agent_definition` always writes) — the fix is simply to **re-run `sidekick init` after any agent.md change**. Consider a version stamp + auto-redeploy on server start if the bundled version is newer than the deployed one. |
| Windows-only | macOS/Linux CSAs excluded | WASAPI loopback is Windows-specific; cross-platform needs a different capture backend (§9) |
| No `--version` / self-update | Hard to know what's deployed | Add `sidekick --version` and `sidekick update` (wraps `uv tool upgrade`) |

**Team-distribution readiness:** ~80%. The blocker for "send it to 10 colleagues" is the **private-repo auth** in the install URL. Solve that and the rest is polish.

---

## 8. Performance

The two latency sources that matter during a live call:

**a) Transcription (Whisper, local CPU).** `small.en` int8 is a sensible default. Headroom:
- **GPU/NPU**: `faster-whisper` supports CUDA; on AI PCs the NPU path (or `int8_float16` on capable GPUs) can cut chunk latency materially. Auto-detect device and pick compute type.
- **VAD gating**: only transcribe chunks with detected speech energy — already partially done via the speech-timer; a proper VAD (e.g. Silero) would skip silence entirely and reduce wasted Whisper calls.
- **Chunk size**: 5s is a reasonable latency/accuracy trade; expose it (already `chunk_duration`, but not configurable).

**b) LLM calls.** Already well-optimised: shared pooled clients, tiered routing (cheap classifier, expensive only for deep), 10s classify batching to halve calls, grounding cache (5-min TTL). Further wins:
- **Stream** `suggest_questions`/`research` so first tokens surface sooner (perceived latency).
- **Cap deep-tier prompt size** — `suggest_questions` sends transcript(50) + threads + research(10) + grounding(several files). On a long call this prompt balloons; token-budget it.
- **Parallelise** classifier and dedup-check (currently the dedup is a *second* fast call per item) — or fold dedup into the classifier prompt to remove a round-trip.
- **Pre-warm** the Whisper model at `listen` (already done in background) and pre-warm one LLM connection.

**c) Process model.** Single global session = one meeting at a time. Fine for a CSA, but the global-state design (§4) blocks any future "review a recording" or concurrent-session use.

Net: performance is **good**; the biggest *perceived* win is **streaming output** + **NPU/GPU Whisper** on modern hardware.

### Performance sequencing (decided)

Performance is **not** a standalone phase — each item is folded into the phase that already touches that code:

| Perf item | Phase | Rationale |
|-----------|:-----:|-----------|
| Pre-warm one LLM connection | **Phase 0** | Trivial; drop into hygiene |
| Cap deep-tier prompt size (token-budget `suggest_questions`) | **Phase 2** | Same tool being refactored to slim `server.py` |
| Fold dedup into classifier (remove extra fast call/item) | **Phase 2** | Queue/classifier structural change; removes an LLM round-trip |
| Streaming output (`research` / `suggest_questions`) | **Phase 4** | Real UX feature; pairs with the answer-card toast |
| NPU/GPU Whisper + VAD gating | **Phase 4 (timeboxed spike)** | Biggest actual win but hardware-dependent; `small.en` int8 already acceptable, so not committed scope |

---

## 9. Killer Features Worth Bolting On

Ranked by value-to-effort for a CSA audience. **Committed for Phase 4** marked ✅; the rest are explicit backlog.

1. ✅ **🥇 Config-driven models** (§6) — *your ask; promoted to the first feature delivered.* Global `models:` block in `default.yaml` + env overrides. Unblocks model experimentation without code edits. **(Built in Phase 1.)**
2. ✅ **🥈 Live "answer card" toast with the actual answer** — today the notify extension toasts that a finding exists; pushing the *one-line answer + source link* into the toast closes the loop and is the single biggest perceived-magic upgrade. (Builds on existing `alerts.jsonl`.) **(Phase 4.)** Decided over the webview panel — a VS Code webview cannot render inside the Copilot Chat window (only a separate editor tab/sidebar), so the toast is the right surface.
3. ✅ **🥉 Post-call deliverables generator** — on `stop`, optionally produce a customer-ready follow-up: email draft, action-item table, and a "questions we couldn't answer live → research now" batch. You already have the transcript, threads, and research; this turns Sidekick into a *deliverable* engine, not just a live aide. **(Phase 4.)**
4. ✅ **Streaming output** (§8b) — first tokens of `research` / `suggest_questions` surface sooner; complements the answer-card toast for perceived latency. **(Phase 4.)**

**Backlog (deferred, not Phase 4):**

- **CRM / engagement context in** — feed the account plan / MSX opportunity / prior-meeting summaries as grounding at `listen` time. `add_context` already gives a manual path; automating MSX pull is its own integration project.
- **"Fact-check the consultant" mode** — the analyst already detects `consultant_answer_correct` / `correction_needed`; surface these as gentle private nudges. Partially built; good fast-follow but needs careful private-surfacing UX so it doesn't misfire live.
- **Speaker diarization (revisited)** — local diarization (e.g. `pyannote`/`whisperx`) would restore "who said what." Heavy dependency; spike-worthy, not committed.
- ~~**Webview transcript panel**~~ — **killed**: cannot live inside the chat window; toast path chosen instead.
- ~~**Meeting-platform / `.vtt` transcript ingestion**~~ — **parked**: relevant only if Windows-only audio capture is relaxed, which it isn't.

---

## 10. Does It Actually Work as a CSA Sidekick?

**Yes, for the core loop, today.** The honest caveats:

- **It shines** on: surfacing verified Microsoft/Fabric answers fast, suggesting sharp next questions, capturing threads and producing a structured post-call summary. For a Fabric/Azure data CSA (your exact domain) it's well-targeted — the grounding pulls your `.github/instructions/` team standards and past engagement artifacts.
- **It's fragile** on: proactive surfacing (depends on the extension + tool cadence), long meetings (prompt bloat, no diarization), and anything off the Microsoft data-platform path (research breadth needs the Tavily/Brave key).
- **It will frustrate** if: the agent definition drifts from the deployed version (you just hit this), or if a colleague installs without `gh auth` / without the live extras.

It is **not** "just an LLM chat window" — a chat window can't hear the call, can't prioritise across concurrent threads, and can't surface unprompted. But realising that value depends on the experiential path (notifications, latency) that is currently the least-finished part.

---

## 11. Recommended Roadmap

**Phase 0 — Hygiene (½ day, do first)** ✅ **COMPLETE**
- [x] Remove stale Azure branches + docstring in `server.py` (§5.1)
- [x] Clear orphaned `.pyc`; confirm `__pycache__/` gitignored (§5.2)
- [x] Resolve `install.ps1` TODO; centralise the package source URL (§5.5)
- [x] Pre-warm one LLM connection (perf quick win, §8b)

**Phase 1 — Config-driven models (1 day; your ask — global only, no per-customer)** ✅ **COMPLETE**
- [x] `ModelsConfig` dataclass + global `models:` in `default.yaml`, code defaults preserved (§6)
- [x] `call_llm(chain=…)`; data-driven provider registry; `SIDEKICK_MODEL_<TIER>` env overrides
- [x] `sidekick models` debug command; provider-adapter note for non-OpenAI APIs

**Phase 2 — Consolidation (2–3 days)** ✅ **COMPLETE** *(test-first, one commit per slice; 48 → 109 tests)*
- [x] Extract grounding, notifier, listen-engine out of `server.py` — `grounding.py`, `output/notifier.py`, `engine.py`. *Note: `engine.py` holds `detect_domains` + `classify_and_dispatch`; the live audio loop (`_run_listen_loop`) is **deferred to Phase 3** pending a live-loop test harness.*
- [x] `SessionState` dataclass to replace module globals — every `global` statement removed
- [x] Single `parse_llm_json` helper (§5.4)
- [x] Token-budget the deep-tier `suggest_questions` prompt (perf, §8b) — `prompt_budget.clip`
- [x] Fold dedup into the classifier prompt to remove a round-trip (perf, §8b) — *delivered as a **deterministic local check** (`dedup.py`, token-Jaccard + difflib) instead of folding into the classifier prompt; removes the second LLM round-trip entirely with zero contract change and zero added tokens.*

**Phase 3 — Test hardening (2–3 days)** ✅ **COMPLETE** *(test-first, one commit per slice; 109 → 179 tests)*
- [x] Live audio loop (`_run_listen_loop`) test harness, then extract it from `server.py` *(carried over from Phase 2)* — extracted to `engine.run_listen_loop` (`_initialise_capture` + `_consume_audio`); 12 tests in `tests/test_listen_loop.py`
- [x] Priority queue (route/merge/dedup/expiry/timeout) — 15 tests in `tests/test_priority_queue.py`
- [x] LLM tier routing + fallback (mock httpx) — 13 tests in `tests/test_llm_routing.py`
- [x] Config deep-merge; classifier JSON parse; session summary — 30 tests across `tests/test_config_merge.py`, `tests/test_classifier_parse.py`, `tests/test_session_log.py`

**Phase 4 — Killer features (demo-focused; tight scope) ✅ COMPLETE**
- [x] Answer-card toast with answer + source (§9.1) — `notifier` carries `answer`/`source`; `sidekick-notify` 0.2.0 shows the answer headline + Open Source button
- [x] Post-call deliverables generator: email draft + action-item table + couldn't-answer-live research batch (§9.3) — `output/deliverables.py`, wired into `stop(deliverables=True)`
- [x] Streaming output for perceived latency (§8b) — `llm.stream_llm()` + opt-in `on_lead` progressive answer-card on the background research path (MCP stdio can't stream into Chat — documented)
- [x] *(timeboxed spike, optional)* NPU/GPU Whisper + VAD gating (§8a) — `device: auto|cpu|cuda` auto-detect (CTranslate2 = CUDA GPU/CPU only, no NPU; documented); VAD already on

---

## 12. Decisions (resolved 11 Jun 2026)

1. **Distribution** — **keep in Git, private repo** with team read access. Public is *not recommended* without a scrub pass: customer names (HMRC/MoJ), removed-but-in-history Eng Hub offerings, internal `eng.ms` URLs, and the documented Copilot integration ID would all be exposed.
2. **Platform** — **Windows-only** for now. WASAPI loopback, `winsound`, and the PowerShell installer stay as-is. Cross-platform/macOS items dropped.
3. **Model policy** — **global swap only, no per-customer overrides.** `models:` lives in `default.yaml`; not surfaced in customer profiles. Simplifies §6.
4. **Proactive surfacing** — **toast path is sufficient.** A webview cannot render inside the Copilot Chat window (only a separate tab/sidebar), so the webview panel is killed; instead enrich the existing toast with the answer + source link (§9.2).
5. **Post-call deliverables** — **in.** Committed as a Phase 4 headline (§9.3).

---

*Phases 0–4 complete (commit `9580b3d`, 228 tests passing).*

---

## 13. Transcription Accuracy — Field Evidence (CCG call, 15 Jun 2026)

> **Objective:** fine-tune Sidekick's listen→transcribe path to a materially **higher degree of accuracy**, validated against ground truth.

A live HMRC CCG deep-dive (32m12s) was captured by Sidekick **and** by Microsoft Teams in parallel, giving a rare ground-truth comparison. Sidekick output: `transcript_20260615_130514.txt` (`small.en` / `int8` / CPU). Ground truth: the Teams speaker-attributed transcript. Findings:

### 13.1 What works
- Downstream research answers were strong, grounded, and **declined to invent** unknown system names (correct client-facing behaviour).
- VAD + repetition guard kept the transcript free of looping hallucinations.

### 13.2 Defects found (ranked by impact on accuracy & usefulness)

| # | Defect | Evidence | Root cause |
|---|--------|----------|-----------|
| **D1** | **Real-time backlog / timestamp drift** | Content at Teams **18:15** stamped **43:25**; summary reports **"56 minutes"** for a **32-minute** meeting | `engine.py` stamps lines with **wall-clock elapsed since `listen` start** (`time.monotonic() - listen_started_at - chunk_duration`), not audio position. CPU `small.en` runs slower than real-time and the audio queue is **unbounded** (`audio_capture.py: asyncio.Queue()`), so a backlog accumulates and the co-pilot drifts ~25 min behind. |
| **D2** | **Proper-noun / jargon errors** | "Denodo"→"the node/de Nodo/Denoto"; "Gen 1"→"gentlemen"; "Andy Esdale"→"Andy Estel"; "Business Objects"→"this object"; "management information/MI"→"module information"; "damn site faster"→"campsite faster"; "Trystan"→"Tristan/Kristen"; "Bharti"→"Barty" | `small.en` has no domain prior; Whisper receives no `initial_prompt`/hotwords even though Sidekick already holds the right vocabulary in its grounding context and live research. Proper nouns and acronyms — exactly the terms read back to the client — are the weakest area. |
| **D3** | **No speaker separation** | Every line tagged `(audio)`; Teams cleanly separated 4 speakers | `speaker` hardcoded `"(audio)"`; no diarization and local mic not mixed in, so the consultant's own questions are absent (loopback captures remote audio only). |
| **D4** | **Missing opening ~1m20s** | Sidekick line 1 `[0:00:00]` maps to Teams **1:22**; the customer's framing/goal is lost | Capture starts after model load / first-chunk warm-up; no pre-roll buffer. |
| **D5** | **Mid-sentence fragmentation** | Hard 5 s cuts split sentences across lines with no repair | Fixed 5 s chunk boundary, no overlap, `condition_on_previous_text` not used → Whisper has no cross-chunk context. |

---

## 14. Phase 5 — Transcription Accuracy Fine-Tuning

Test-first, one commit per slice, mirror to OneDrive, zero regression, server restart to pick up runtime changes. Sequenced **highest accuracy-per-effort first**. CTranslate2 constraint acknowledged throughout: **GPU/CPU only, no NPU**.

**Slice 5a — Sample-based timestamps + bounded queue (fixes D1)** — *highest value*
- Track audio position from **samples actually consumed** (cumulative processed chunk duration), not wall-clock; pass that as `chunk_start_offset`. Fixes drift and the "56 min" bug.
- Bound the capture queue with a **drop-to-latest-when-behind** policy so live suggestions stay current under CPU pressure (saved transcript may sacrifice some fidelity — acceptable trade, documented).
- Tests: monotonic non-drifting offsets across N chunks; queue caps and drops oldest when full.

**Slice 5b — Derived, self-maintaining domain prior for Whisper (fixes D2)** — *cheapest big accuracy win, zero hardcoding*

A hand-curated glossary is rejected: it is hardcoding by another name — it rots, it must be authored per customer, and it only ever covers terms someone remembered. Instead, Sidekick **derives** Whisper's `initial_prompt`/`hotwords` from material it *already ingests*, and lets it **improve over the call**. Two complementary sources, no hand-list:

1. **Seed prior from existing grounding (no new input).** `grounding.build_grounding_context` already loads the customer `domains`, `description`, and the `.github/instructions/` engagement files. Extract the salient terms from that text Sidekick is already reading — capitalised tokens, domain phrases, product/proper nouns — and use them as the starting `initial_prompt`. The prior is whatever the engagement context already contains; nothing is authored for Whisper specifically.
2. **Adaptive in-session vocabulary (self-reinforcing).** As the call runs, harvest proper nouns from the two streams that are *already corrected by the LLM from context* — classified thread `key_facts` and research results (the analyst writes "Denodo" correctly even when Whisper heard "de Nodo", because it reasons from grounding). Feed those terms back as the rolling `initial_prompt` for subsequent chunks. The longer the meeting runs, the better proper-noun recognition gets — "Denodo" is mangled once, surfaces correctly in research/threads, then becomes a hotword for every later chunk.

- **Implementation:** a small `vocabulary.py` that (a) builds the seed set from the grounding string at `listen` time and (b) exposes an `update(key_facts, research_terms)` called from `classify_and_dispatch`; `WhisperRecogniser.transcribe_chunk` reads the current term set as `initial_prompt`. No `glossary:` config key, no per-customer authoring.
- **Tests:** seed terms extracted from a sample grounding block; in-session `update` promotes a term so a later chunk receives it as prior; empty grounding + empty session = current behaviour (no prior).

> **Alternative considered — LLM post-correction.** A fast-tier pass that rewrites proper nouns from context (the deleted `transcript_correction` module did this). Rejected as the *primary* lever: it adds a per-chunk LLM round-trip and latency that conflicts with 5a, and corrects the *saved* text without improving the live recognition. The derived prior fixes recognition at the source with no extra network cost. Keep post-correction as an optional, off-by-default polish on the saved transcript only.

**Slice 5c — Capture-start latency / pre-roll (fixes D4)**
- Pre-warm the Whisper model fully **before** signalling capture-ready, and/or keep a short rolling pre-roll buffer so the first ~1–2 min is not lost.
- Tests: first emitted offset ≈ 0 with a pre-warmed model; no audio dropped before first transcribe call.

**Slice 5d — Local mic + loopback mix with 2-way attribution (mitigates D3)**
- Mix local microphone with WASAPI loopback; tag `(me)` vs `(remote)` to recover the consultant's half and give crude attribution essentially free.
- Tests: two synthetic streams labelled correctly; loopback-only path unchanged when no mic configured.

**Slice 5e — Chunk-boundary coherence (fixes D5)**
- Overlapping windows (~1 s) + `condition_on_previous_text`, or re-segment on VAD silence instead of a hard 5 s cut, to stop mid-sentence splits.
- Sequenced last — interacts with 5a latency; validate no regression in drift.

**Model-size note (gated, not a committed slice):** `small.en`→`medium.en`/`large-v3` improves jargon accuracy but worsens CPU latency (conflicts with 5a). Gate behind the existing `device: auto` detection — select a larger model **only** when CUDA is present; otherwise stay on `small.en` + the derived prior (5b), which addresses most D2 errors without the latency cost.

**Expected accuracy outcome:** 5a restores temporal correctness and live relevance; 5b eliminates the recurring proper-noun/jargon failures (the most client-visible errors) by *deriving and adapting* the prior rather than hardcoding it; 5c–5e recover lost content and readability. Together these target the gap between Sidekick's `(audio)`-only, drifting transcript and the Teams ground truth — without a model change on CPU hardware.


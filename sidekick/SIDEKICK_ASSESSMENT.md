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

**Phase 0 — Hygiene (½ day, do first)**
- [ ] Remove stale Azure branches + docstring in `server.py` (§5.1)
- [ ] Clear orphaned `.pyc`; confirm `__pycache__/` gitignored (§5.2)
- [ ] Resolve `install.ps1` TODO; centralise the package source URL (§5.5)
- [ ] Pre-warm one LLM connection (perf quick win, §8b)

**Phase 1 — Config-driven models (1 day; your ask — global only, no per-customer)**
- [ ] `ModelsConfig` dataclass + global `models:` in `default.yaml`, code defaults preserved (§6)
- [ ] `call_llm(chain=…)`; data-driven provider registry; `SIDEKICK_MODEL_<TIER>` env overrides
- [ ] `sidekick models` debug command; provider-adapter note for non-OpenAI APIs

**Phase 2 — Consolidation (2–3 days)**
- [ ] Extract grounding, notifier, listen-engine out of `server.py`
- [ ] `SessionState` dataclass to replace module globals
- [ ] Single `parse_llm_json` helper (§5.4)
- [ ] Token-budget the deep-tier `suggest_questions` prompt (perf, §8b)
- [ ] Fold dedup into the classifier prompt to remove a round-trip (perf, §8b)

**Phase 3 — Test hardening (2–3 days)**
- [ ] Priority queue (route/merge/dedup/expiry/timeout)
- [ ] LLM tier routing + fallback (mock httpx)
- [ ] Config deep-merge; classifier JSON parse; session summary

**Phase 4 — Killer features (demo-focused; tight scope)**
- [ ] Answer-card toast with answer + source (§9.1)
- [ ] Post-call deliverables generator: email draft + action-item table + couldn't-answer-live research batch (§9.3)
- [ ] Streaming output for perceived latency (§8b)
- [ ] *(timeboxed spike, optional)* NPU/GPU Whisper + VAD gating (§8a)

---

## 12. Decisions (resolved 11 Jun 2026)

1. **Distribution** — **keep in Git, private repo** with team read access. Public is *not recommended* without a scrub pass: customer names (HMRC/MoJ), removed-but-in-history Eng Hub offerings, internal `eng.ms` URLs, and the documented Copilot integration ID would all be exposed.
2. **Platform** — **Windows-only** for now. WASAPI loopback, `winsound`, and the PowerShell installer stay as-is. Cross-platform/macOS items dropped.
3. **Model policy** — **global swap only, no per-customer overrides.** `models:` lives in `default.yaml`; not surfaced in customer profiles. Simplifies §6.
4. **Proactive surfacing** — **toast path is sufficient.** A webview cannot render inside the Copilot Chat window (only a separate tab/sidebar), so the webview panel is killed; instead enrich the existing toast with the answer + source link (§9.2).
5. **Post-call deliverables** — **in.** Committed as a Phase 4 headline (§9.3).

---

*Phase 0 + Phase 1 in progress.*

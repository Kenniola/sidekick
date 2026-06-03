# Sidekick — Implementation Reference

> **Purpose**: Replayable build log and test plan.
> **Last updated**: 02 Jun 2026
> **Status**: Implementation complete (V1 + V2). Testing: Phase 1-4 + 5.1 pass. Phase 5.2-5.4 (distribution) pending.

---

## 1. What Was Built

A real-time meeting co-pilot that runs as an MCP server inside VS Code Copilot Chat.
Seven tools: `listen`, `suggest_questions`, `research`, `offerings`, `prototype`, `status`, `stop`.

**Stack**: Python 3.11+ · FastMCP (mcp SDK) · faster-whisper · PyAudioWPatch · Azure Speech SDK · httpx · PyYAML · hatchling

---

## 2. File Inventory (20 Python files, ~4,700 LOC)

```
repo/sidekick/
├── pyproject.toml                  # Package: sidekick-copilot 0.1.0
├── install.ps1                     # Windows one-liner bootstrap
├── .env.example                    # Env var reference
├── .agent.md                       # VS Code agent metadata
├── README.md                       # User-facing documentation
├── configs/
│   ├── default.yaml                # Factory defaults (bundled in wheel)
│   └── _template.yaml              # Starter template (bundled in wheel)
└── src/sidekick/
    ├── __init__.py                 # Package marker
    ├── server.py                   # MCP server, 7 tools, notifications, background loop (1054 lines)
    ├── llm.py                      # Copilot API + GitHub Models fallback (363 lines)
    ├── config.py                   # Config loader with user-local profiles (336 lines)
    ├── cli.py                      # CLI: init, serve, list-configs (218 lines)
    ├── transcript/
    │   ├── audio_capture.py        # WASAPI loopback capture (256 lines)
    │   └── speech_recogniser.py    # Whisper + Azure Speech backends (398 lines)
    ├── analyst/
    │   ├── classifier.py           # LLM transcript analyser (187 lines)
    │   ├── context.py              # MeetingContext + TranscriptLine (129 lines)
    │   └── prompts.py              # System prompts incl. 7-step chain-of-thought (235 lines)
    ├── queue/
    │   └── priority_queue.py       # 3-lane async queue (265 lines)
    ├── actions/
    │   ├── research.py             # Multi-source research + repo search + Eng Hub (406 lines)
    │   ├── enghub.py               # Eng Hub Resource Center VBD/IP search (515 lines)
    │   └── prototype.py            # Code generation (107 lines)
    └── output/
        └── session_log.py          # Session log + summary (217 lines)

repo/sidekick-notify/               # Companion VS Code extension
├── src/extension.ts                # Polls alerts.jsonl, shows toast notifications
├── package.json                    # Extension manifest
└── sidekick-notify-0.1.0.vsix      # Pre-built installable

~/.sidekick/                        # User-local (created by `sidekick init`)
├── customers.yaml                  # Customer profiles
├── outputs/                        # Session logs per customer
├── live/                           # alerts.jsonl
└── configs/                        # Individual config file fallback
```

---

## 3. Build Steps (Replayable)

Each step is independent and can be re-executed. Run from `repo/sidekick/`.

### Step 1 — Create venv and install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[live]"            # Whisper + WASAPI audio
# OR
pip install -e ".[azure]"           # Azure Speech + WASAPI audio
# OR
pip install -e ".[all]"             # Everything including dev tools
```

### Step 2 — Verify package installs and entry points

```powershell
pip show sidekick-copilot           # Package metadata
sidekick help                       # CLI works
sidekick list-configs               # Discovers profiles
```

### Step 3 — Bootstrap user directory

```powershell
sidekick init
```

Creates `~/.sidekick/`, seeds `customers.yaml` from `_template.yaml`, registers MCP server in VS Code User Settings `mcp.json`.

### Step 4 — Set LLM credentials

```powershell
# Sidekick uses Copilot API (primary) + GitHub Models (fallback)
# Both authenticate via gh auth token — no extra env vars needed
gh auth login
```

### Step 5 — Add customer profiles

Edit `~/.sidekick/customers.yaml`:
```yaml
hmrc:
  customer: HMRC
  sensitivity:
    trigger_threshold: 0.6
  # ... see _template.yaml for full reference
```

### Step 6 — Launch from VS Code

In Copilot Chat: `@sidekick listen --config hmrc`

---

## 4. Architecture Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Transport | stdio (MCP default) | Simplest, no port conflicts, works out of the box |
| Audio capture | WASAPI loopback via PyAudioWPatch | Captures system audio (Teams/Zoom/Meet), Windows-native |
| Speech-to-text (default) | faster-whisper base.en, CPU int8 | Zero-config, ~150MB model, fast on CPU |
| Speech-to-text (upgrade) | Azure Speech ConversationTranscriber | Speaker diarization, higher accuracy, Entra ID auth |
| LLM backend | Copilot API primary, GitHub Models fallback | Free with gh auth token, high throughput, Claude + GPT tiers |
| Config format | YAML single-file profiles | Readable, mergeable, one file for all customers |
| Config location | ~/.sidekick/ | User-local, outside repo, survives reinstalls |
| Package defaults | importlib.resources | Bundled in wheel, no file path assumptions |
| Distribution | pip install from GitHub | No PyPI account needed, git tag versioning |
| CLI | sidekick init/serve/list-configs | Bootstrapping and diagnostics |
| Session outputs | ~/.sidekick/outputs/<customer>/ | Organised per customer, auto-created |
| Roadmap cache | ~/.sidekick/cache/roadmap_cache.json | Decommissioned — roadmap queries now routed through research pipeline |
| Config inheritance | deep_merge(base, override) | Customer profiles only override what differs |
| Notifications | winsound + MCP logger + alerts.jsonl | Zero-dependency sound alert; no VS Code extension needed |
| Eng Hub offerings | EngHubPipeline in suggest_questions + standalone tool | Proactively matched during question synthesis and available on-demand |
| Grounding | Instruction files + repo artifacts + session history | Loaded by `_build_grounding_context()` based on config domains |

---

## 5. Config Resolution & Environment Variables

See [README.md](README.md) for the full config resolution chain, customer profile examples,
environment variables reference, and Azure Speech upgrade instructions.

---

## 6. Data Flow

```
                  ┌───────────────────────────────────────────────────────┐
  @sidekick       │                    MCP Server (server.py)            │
  listen          │                                                       │
  ──────────────► │  _init_session() → config + context + pipelines      │
                  │  _run_listen_loop() spawned as asyncio.Task           │
                  │                                                       │
                  │  ┌─────────────────────────────────────────────────┐  │
                  │  │ Audio Capture (audio_capture.py)                │  │
                  │  │ WASAPI loopback → 48kHz stereo → 16kHz mono    │  │
                  │  │ 5s chunks → silence filtered → async queue      │  │
                  │  └──────────┬──────────────────────────────────────┘  │
                  │             ▼                                         │
                  │  ┌─────────────────────────────────────────────────┐  │
                  │  │ Speech Recogniser (speech_recogniser.py)        │  │
                  │  │ Whisper: base.en CPU int8 + hallucination guard │  │
                  │  │ Azure:  ConversationTranscriber + diarization   │  │
                  │  │ Output: list[TranscriptLine]                    │  │
                  │  └──────────┬──────────────────────────────────────┘  │
                  │             ▼ (batched every 10s)                     │
                  │  ┌─────────────────────────────────────────────────┐  │
                  │  │ Classifier (classifier.py)                      │  │
                  │  │ LLM analyses transcript → ActionItem[]          │  │
                  │  │ Detects: questions, hedges, action items         │  │
                  │  │ Filters by trigger_threshold (default 0.5)      │  │
                  │  └──────────┬──────────────────────────────────────┘  │
                  │             ▼                                         │
                  │  ┌─────────────────────────────────────────────────┐  │
                  │  │ Priority Queue (priority_queue.py)              │  │
                  │  │ Fast lane:     3 concurrent, 15s timeout        │  │
                  │  │ Standard lane: 2 concurrent, 30s timeout        │  │
                  │  │ Deep lane:     1 concurrent, 90s timeout        │  │
                  │  │ Merges duplicate questions, expires stale items  │  │
                  │  └──────────┬──────────────────────────────────────┘  │
                  │             ▼                                         │
                  │  ┌─────────────────────────────────────────────────┐  │
                  │  │ Action Pipelines                                │  │
                  │  │ research.py  → repo search + instructions + LLM │  │
                  │  │ prototype.py → code generation (PySpark/SQL/DAX)│  │
                  │  └──────────┬──────────────────────────────────────┘  │
                  │             ▼                                         │
                  │  ┌─────────────────────────────────────────────────┐  │
                  │  │ Session Log (session_log.py)                    │  │
                  │  │ Accumulates outputs → JSON on stop              │  │
                  │  │ Saves to ~/.sidekick/outputs/<customer>/        │  │
                  │  └─────────────────────────────────────────────────┘  │
                  └───────────────────────────────────────────────────────┘
```

---

## 7. Cleanup History (Completed)

Changes made during the audit and cleanup phase:

| # | Action | Details |
|---|--------|---------|
| 1 | Deleted dead files | `file_watcher.py`, `vtt_parser.py`, `formatter.py` (235 lines) |
| 2 | Consolidated TranscriptLine | Moved to `context.py`, updated import in `speech_recogniser.py` |
| 3 | Cleaned server.py | Removed OutputFormatter, fixed status() backend label, fixed listen() docstring |
| 4 | Removed unused deps | `beautifulsoup4`, `jinja2`, `webvtt-py` from pyproject.toml |
| 5 | Config inheritance | Added `_deep_merge()` in config.py; customer configs only specify overrides |
| 6 | Slimmed YAML configs | Removed hardcoded names, endpoints; overrides only |
| 7 | Rewrote roadmap.py | Replaced broken BS4 scraper with LLM synthesis + growing cache (**later decommissioned — roadmap queries now routed through research pipeline**) |
| 8 | Implemented _search_repo() | Keyword scoring across configured grounding paths |
| 9 | Fixed path resolution | Added `SIDEKICK_WORKSPACE_ROOT` env var; updated mcp.json |
| 10 | Updated .env.example | Added Azure Speech env var fallbacks |

---

## 8. Packaging Changes (Completed)

| # | Action | Details |
|---|--------|---------|
| 1 | Package name | `sidekick` → `sidekick-copilot` |
| 2 | Entry points | `[project.scripts] sidekick = "sidekick.cli:main"` |
| 3 | Package data | `default.yaml` + `_template.yaml` bundled via `[tool.hatch.build.targets.wheel.force-include]` |
| 4 | Config loader | Rewritten with `importlib.resources`, 3-tier search chain, `customers.yaml` profiles |
| 5 | User directory | `~/.sidekick/` for cache, outputs, configs (`get_user_dir()`, `get_cache_dir()`, `get_output_dir()`) |
| 6 | Output paths | `session_log.py` → `get_output_dir(customer)` instead of `config.output.save_to` |
| 7 | Cache paths | `roadmap.py` → `get_cache_dir()` instead of `./cache` |
| 8 | CLI | `cli.py` with `init`, `serve`, `list-configs` commands |
| 9 | Installer | `install.ps1` — checks Python, creates venv, pip installs, runs init |
| 10 | Customer profiles migrated | `hmrc.yaml` + `moj.yaml` → `~/.sidekick/customers.yaml` |
| 11 | Stale files deleted | `cache/`, `hmrc/sidekick-outputs/`, `configs/hmrc.yaml`, `configs/moj.yaml` |
| 12 | README rewritten | Install flow, config inheritance, CLI docs, corrected structure |

---

## 9. Test Plan

### Phase 1 — Static Verification (no audio, no LLM)

Tests that require zero external dependencies — pure Python logic.

| # | Test | Command / Method | Pass Criteria |
|---|------|-----------------|---------------|
| 1.1 | ✅ AST parse all files | `python -c "import ast, pathlib; ..."` | 20 files, 0 errors |
| 1.2 | ✅ MCP tool registration | `asyncio.run(server.list_tools())` | 7 tools: listen, suggest_questions, research, offerings, prototype, status, stop |
| 1.3 | ✅ Package default loads | `load_config()` → `SidekickConfig` | customer="General", threshold=0.5 |
| 1.4 | ✅ Customer profile loads | `load_config("hmrc")` | customer="HMRC", threshold=0.6 |
| 1.5 | ✅ Config inheritance | Load hmrc, check unoverridden field | queue.fast_lane_max=3 (inherited from default) |
| 1.6 | ✅ Missing profile error | `load_config("nonexistent")` | `FileNotFoundError` with available profiles listed |
| 1.7 | ✅ list_available_configs | `list_available_configs()` | Returns `["hmrc", "moj"]` |
| 1.8 | ✅ Deep merge logic | `_deep_merge({a:{b:1}}, {a:{c:2}})` | `{a:{b:1, c:2}}` |
| 1.9 | ✅ Deep merge list replace | `_deep_merge({a:[1]}, {a:[2,3]})` | `{a:[2,3]}` (not `[1,2,3]`) |
| 1.10 | ✅ User dir resolution | `get_user_dir()` | `~/.sidekick/` |
| 1.11 | ✅ Cache dir auto-create | `get_cache_dir()` | Directory exists after call |
| 1.12 | ✅ Output dir per customer | `get_output_dir("HMRC")` | `~/.sidekick/outputs/hmrc/` (lowercase, hyphenated) |
| 1.13 | ✅ CLI help | `sidekick help` | Shows init, serve, list-configs |
| 1.14 | ✅ CLI list-configs | `sidekick list-configs` | Lists hmrc, moj |
| 1.15 | ✅ TranscriptLine import | `from sidekick.analyst.context import TranscriptLine` | No ImportError |
| 1.16 | ✅ MeetingContext basics | Create context, add_lines, check elapsed_minutes | No crash, sensible values |
| 1.17 | ✅ PriorityQueue routing | Enqueue items with different complexities | Routes to correct lanes |
| 1.18 | ✅ ~~Roadmap cache path~~ | ~~`RoadmapPipeline().cache_dir`~~ | ~~Points to `~/.sidekick/cache/`~~ — **Decommissioned** |

### Phase 2 — LLM Integration (requires GITHUB_TOKEN)

Tests that call the LLM but don't need audio hardware.

| # | Test | Command / Method | Pass Criteria |
|---|------|-----------------|---------------|
| 2.1 | ✅ LLM basic call | `call_llm("You are a test.", "Say hello.")` | Non-empty string response |
| 2.2 | ✅ LLM JSON mode | `call_llm(..., json_output=True)` | Valid JSON parseable response |
| 2.3 | ✅ Research pipeline | `ResearchPipeline(config).execute_direct("What is DirectLake?")` | ResearchResult with answer + sources |
| 2.4 | ✅ Repo search grounding | `research._search_repo("S3 shortcut")` with HMRC config | Finds files in hmrc/ |
| 2.5 | ✅ Instruction search | `research._search_instructions("DAX")` | Finds .github/instructions/ files |
| 2.6 | ✅ ~~Roadmap lookup (cold)~~ | ~~`RoadmapPipeline().lookup("Direct Lake")`~~ | ~~Returns features list~~ — **Decommissioned** |
| 2.7 | ✅ ~~Roadmap lookup (cached)~~ | ~~Second call with same query~~ | ~~Returns from cache~~ — **Decommissioned** |
| 2.8 | ✅ Prototype generation | `PrototypePipeline(config).execute_direct("bronze to silver notebook")` | PrototypeResult with Python code |
| 2.9 | ✅ Classifier analysis | Feed mock TranscriptLines to `TranscriptAnalyst.analyse_chunk()` | Returns ActionItem[] with valid types |
| 2.10 | ✅ Consultant advisor | Call `suggest_questions()` with mock context | Returns ranked suggestions with corrections |

### Phase 2b — V2 Features (requires GITHUB_TOKEN)

| # | Test | Command / Method | Pass Criteria |
|---|------|-----------------|---------------|
| 2.11 | ✅ Grounding context | `_build_grounding_context()` with HMRC config | Returns >1,000 chars of instruction content |
| 2.12 | ✅ Alert writer | `_notify(result)` | Plays sound + appends to alerts.jsonl |
| 2.13 | ✅ Eng Hub search | `EngHubPipeline().search("lakehouse")` | Returns EngHubResult with offerings |
| 2.14 | ✅ Offerings in suggest_questions | Call `suggest_questions()` with topics set | offerings_block populated in prompt |

### Phase 3 — Audio Pipeline (requires audio hardware)

Tests that need WASAPI loopback (Windows with speakers/headset).

| # | Test | Command / Method | Pass Criteria |
|---|------|-----------------|---------------|
| 3.1 | ✅ List audio devices | `AudioCapture().list_devices()` | Returns list with ≥1 loopback device |
| 3.2 | ✅ Capture 5s chunk | Start capture, yield 1 chunk, stop | numpy array, shape (~80000,), dtype float32 |
| 3.3 | ✅ Silence detection | Capture with no audio playing | Chunk skipped (RMS below threshold) |
| 3.4 | ✅ Whisper transcription | Capture while playing a YouTube video | TranscriptLine[] with recognisable text |
| 3.5 | ✅ Hallucination guard | Transcribe near-silence | Segments with high no_speech_prob filtered out |
| 3.6 | ✅ Recogniser factory | `create_recogniser()` with default config | Returns WhisperRecogniser |
| 3.7 | ✅ Recogniser factory (azure) | `create_recogniser(azure_config)` | Returns AzureSpeechRecogniser |

### Phase 4 — End-to-End (full pipeline)

| # | Test | Command / Method | Pass Criteria |
|---|------|-----------------|---------------|
| 4.1 | ✅ Listen + status + stop | `listen` → wait 30s → `status` → `stop` | Session summary with outputs |
| 4.2 | ✅ Session save | After stop, check `~/.sidekick/outputs/` | JSON file created with session data |
| 4.3 | ✅ Config switch | `listen --config hmrc` then `listen --config moj` | Different configs load correctly |
| 4.4 | ✅ Auto-stop on silence | Start listen with no audio, wait 60s+ | Loop stops automatically |
| 4.5 | ✅ Error resilience | Simulate LLM failure mid-session | Loop continues after error, stops at threshold (5) |

### Phase 5 — Distribution (clean machine simulation)

| # | Test | Command / Method | Pass Criteria |
|---|------|-----------------|---------------|
| 5.1 | ✅ Wheel builds | `pip wheel . --no-deps -w dist/` | `.whl` file created, contains configs/ |
| 5.2 | Clean venv install | New venv, `pip install dist/sidekick_copilot-*.whl` | Installs without errors |
| 5.3 | sidekick init (fresh) | Run in clean env | Creates ~/.sidekick/, seeds customers.yaml |
| 5.4 | Package default loads (installed) | `load_config()` from installed package | Loads default.yaml from wheel |

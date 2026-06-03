# Sidekick — Real-time Meeting Co-pilot

MCP server for GitHub Copilot that listens to meetings, researches questions it hears, suggests what to ask, and generates code prototypes — autonomously while you're on the call.

## Quick Start

### Option A: One-liner (recommended)

```powershell
irm https://raw.githubusercontent.com/Kenniola/sidekick/main/install.ps1 | iex
```

Installs `uv` (if missing) → installs sidekick in an isolated environment → scaffolds `~/.sidekick/` → registers MCP server → installs sidekick-notify extension.

### Option B: uvx (zero-install)

If you have [`uv`](https://docs.astral.sh/uv/) installed, add this to your VS Code User or workspace `mcp.json` — no pip install needed:

```jsonc
// .vscode/mcp.json
{
  "servers": {
    "sidekick": {
      "command": "uvx",
      "args": ["--from", "sidekick-copilot[live]", "sidekick", "serve"]
    }
  }
}
```

Then run `sidekick init` once to scaffold config and install the notification extension.

### Option C: From source (development)

```powershell
cd repo/sidekick
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[all]"
sidekick init
```

Install extras: `[live]` (Whisper), `[azure]` (Azure Speech + diarization), `[all]` (everything + dev).

### LLM Auth

Sidekick uses the **Copilot API** (primary) with **GitHub Models** fallback. Both authenticate via `gh auth token` — no additional env vars needed.

```powershell
gh auth login   # that's it — sidekick auto-detects the token
```

The token refreshes every 30 minutes during long sessions.

#### Model Tiers

| Tier | Model (Copilot API) | Used By | Fallback (GitHub Models) |
|------|---------------------|---------|--------------------------|
| `fast` | gpt-4o-mini | Classifier | gpt-4.1-mini |
| `standard` | claude-sonnet-4.5 | Research, Suggest | gpt-4.1-mini |
| `deep` | claude-opus-4.7 | Prototype, Complex research | DeepSeek-R1 |

All tiers retry with exponential backoff (1s → 2s → 4s) and fall through the chain on 429/5xx.

### Start

```
@sidekick listen                 # default config
@sidekick listen --config hmrc   # customer profile
```

---

## How It Works

```
System Audio → Transcribe (5s) → Batch (10s) → Classify → Priority Queue → Research/Prototype → Log
```

1. **Captures** system audio via WASAPI loopback (Teams, Zoom, Meet)
2. **Transcribes** with Whisper (default) or Azure Speech (speaker diarization)
3. **Classifies** questions, hedges ("let me confirm…"), action items
4. **Routes** to 3-lane priority queue (fast / standard / deep)
5. **Executes** research and prototype generation automatically
6. **Auto-stops** after 60s of silence

The `research` tool is for manual ad-hoc questions — the loop handles everything heard on the call.

---

## Tools

| Tool | Shortcut | Purpose |
|------|----------|---------|
| `listen` | — | Start audio capture + autonomous loop |
| `suggest_questions` | `q` / `?` | Ranked questions with claim analysis, corrections, offerings |
| `research` | `r <topic>` | Ad-hoc question (MS Learn, workspace docs, Eng Hub) |
| `offerings` | `o` | VBD/IP offerings from Eng Hub (PoC, ADR, WorkshopPLUS) |
| `prototype` | `p <desc>` | Generate code (PySpark, T-SQL, DAX, pipeline) |
| `status` | `s` / `.` | New threads and research since last check |
| `stop` | `x` | End session, save summary |

### `suggest_questions` — Consultant Advisor

7-step chain-of-thought: claim analysis → contradiction detection → gap analysis → risk detection → offerings match → strategic positioning → timing assessment.

Returns ranked questions with category, impact, rationale, corrections, observations, and matched VBD/IP offerings. Phase-aware (opening → core → deep-dive → wrap-up). Grounded in `.github/instructions/` and past engagement artifacts.

---

## Customer Profiles

Single-file system at `~/.sidekick/customers.yaml`. Profiles deep-merge over `default.yaml` — only specify overrides.

```yaml
hmrc:
  customer: HMRC
  participants:
    consultant: ["Your Name"]
  domains: [Microsoft Fabric, AWS S3 Integration]
  sensitivity:
    trigger_threshold: 0.6
  triggers:
    client_topics:
      - pattern: "S3|AWS|bucket|egress"
        action: research
```

Config search order: `$SIDEKICK_CONFIG_DIR/` → `~/.sidekick/customers.yaml` → `~/.sidekick/configs/` → package `default.yaml`.

Lists replace wholesale (not merged). Run `sidekick init` to scaffold with a commented template.

---

## Azure Speech (Optional)

Higher-quality transcription with speaker diarization.

```powershell
pip install -e ".[azure]"
```

Add to your profile:
```yaml
hmrc:
  speech:
    backend: azure
    azure_region: uksouth
    azure_endpoint: "https://your-resource.cognitiveservices.azure.com/"
```

Auth: `azure_endpoint` uses Entra ID (`az login`). `azure_key` uses key auth. Endpoint takes priority if both set. Default is `backend: whisper` (zero setup).

---

## Notifications

### Built-in
Findings trigger a 1kHz beep (Windows) and append to `~/.sidekick/live/alerts.jsonl`. Every tool response auto-surfaces new findings via a preamble banner.

### VS Code Extension (sidekick-notify)
Optional companion extension in `repo/sidekick-notify/` — polls `alerts.jsonl` and shows VS Code toast notifications with a status bar badge.

```powershell
code --install-extension repo/sidekick-notify/sidekick-notify-0.1.0.vsix
```

Click the status bar badge to open `@sidekick status` in chat.

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `GITHUB_TOKEN` | `gh auth token` | Override token (e.g. CI). Normally auto-detected. |
| `SIDEKICK_WORKSPACE_ROOT` | CWD | Workspace root for repo search and grounding |
| `SIDEKICK_HOME` | `~/.sidekick/` | Override user directory |
| `SIDEKICK_CLASSIFY_INTERVAL` | `10` | Seconds between classifier calls |
| `SIDEKICK_WHISPER_MODEL` | `base.en` | Whisper model size (Whisper backend only) |

Speech config is in customer profiles, not env vars.

---

## CLI

```
sidekick init          # Scaffold ~/.sidekick/ and register MCP server
sidekick serve         # Run MCP server (called by mcp.json)
sidekick list-configs  # Show available profiles
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  VS Code Copilot Chat ←→ @sidekick agent ←→ MCP Server  │
├──────────────────────────────────────────────────────────┤
│  Audio Capture → Speech-to-Text → Classifier → Queue    │
│  (WASAPI)        (Whisper/Azure)   (LLM)       (3-lane) │
│                                                          │
│                              ├── Research Pipeline       │
│                              ├── Prototype Pipeline      │
│                              └── Consultant Advisor      │
│                                   + Eng Hub offerings    │
│                                   + grounding context    │
│  Meeting Context ←→ Session Log → ~/.sidekick/outputs/   │
│  Notifications → alerts.jsonl → sidekick-notify ext      │
└──────────────────────────────────────────────────────────┘
```

Research searches: workspace docs (keyword scoring) → `.github/instructions/` → Microsoft Learn.

### Resilience

- Per-chunk error handling — bad LLM responses don't crash the session
- Consecutive error threshold (5) before the loop stops
- JSON hardening — markdown fence stripping, unknown field filtering
- Silence detection and hallucination guards (no_speech_prob, repetition, VAD)
- **Auto-stop** — 60 seconds of silence triggers automatic shutdown
- **Transcript batching** — 10s classification window reduces LLM calls by ~50%

---

## Project Structure

```
repo/sidekick/
├── pyproject.toml              # Package config (sidekick-copilot)
├── install.ps1                 # Windows bootstrap installer
├── configs/                    # Bundled as package data
│   ├── default.yaml            # Factory defaults
│   └── _template.yaml          # Starter template for customers.yaml
└── src/sidekick/
    ├── server.py               # MCP server — 7 tools + background loop + capture + notifications
    ├── llm.py                  # Multi-backend LLM client + vision API (GitHub/Azure/Anthropic)
    ├── config.py               # Config loader — user-local + package defaults
    ├── cli.py                  # CLI entry points (init, serve, list-configs)
    ├── transcript/
    │   ├── audio_capture.py    # WASAPI loopback audio capture
    │   └── speech_recogniser.py# Whisper / Azure Speech backends
    ├── analyst/
    │   ├── classifier.py       # LLM-powered transcript analyser
    │   ├── context.py          # Meeting state + TranscriptLine
    │   └── prompts.py          # Analyst + consultant advisor (7-step chain-of-thought)
    ├── queue/
    │   └── priority_queue.py   # 3-lane async priority queue
    ├── actions/
    │   ├── research.py         # Multi-source research pipeline with repo search
    │   └── prototype.py        # Code generation pipeline
    └── output/
        └── session_log.py      # Session log + summary generation

~/.sidekick/                    # User-local directory (created by sidekick init)
├── customers.yaml              # Customer profiles
├── outputs/                    # Session logs per customer
└── configs/                    # Individual config file fallback
```

---

## User Directory (`~/.sidekick/`)

All user-local state lives in `~/.sidekick/`:

| Path | Purpose |
|------|---------|
| `customers.yaml` | Customer profiles (single-file, multi-profile) |
| `outputs/<customer>/` | Session logs per customer |
| `live/alerts.jsonl` | Proactive notification log (appended by background loop) |
| `configs/` | Individual config files (fallback if not using customers.yaml) |

Override the location with `SIDEKICK_HOME`.

# Sidekick — Live Testing Checklist

> Manual end-to-end checks to validate Sidekick during a real (or simulated) meeting.

---

## Prerequisites

- [ ] Audio playing through speakers/headset (YouTube video with speech recommended)
- [ ] `GITHUB_TOKEN` set, or `gh auth login` completed
- [ ] Sidekick venv installed with `[live]` extras (`pip install -e ".[live]"`)
- [ ] VS Code open in the `assessment-scripts` workspace

---

## Step 0 — Switch to Sidekick agent

1. Open **Copilot Chat** (Ctrl+Shift+I or the chat icon)
2. Click the agent/model selector at the top of the chat panel
3. Select **`sidekick`** from the agent list
4. You should see the chat switch to the Sidekick agent context — all subsequent commands go through Sidekick's MCP tools

> **Tip**: If `sidekick` doesn't appear in the agent list, check that `.vscode/mcp.json` is configured and the MCP server is registered. Run `sidekick init` from the terminal to auto-register.
>
> **After code changes**: Restart the MCP server so it picks up the latest code. Click the MCP server status indicator in the bottom bar, or restart VS Code.

---

## Step 1 — Start a session

```
listen --config hmrc
```

**Expected — Consent Prompt** (first call):
- [ ] Shows `⚠️ Audio Transcription Consent`
- [ ] Shows: "Sidekick will capture and transcribe system audio (Teams, Zoom, etc.)."
- [ ] Shows: "Please confirm that all meeting participants consent to transcription being captured."
- [ ] Shows `Config: hmrc | Reply yes to start.`
- [ ] Responds instantly (no delay)

**Respond**: "yes" or "go ahead"

**Expected — Session Start** (after confirmation):
- [ ] Responds instantly (no hang!)
- [ ] Shows `✓ Listening started`
- [ ] Shows a table with Backend, Config, Domains, and Devices
- [ ] Domains listed with `·` separator (all 5: Microsoft Fabric, Power BI, Azure Data Platform, AWS S3 Integration, PostgreSQL)
- [ ] Device names are short (e.g. "Speakers" not full driver string)
- [ ] Shows `🟢 Live — loading model and starting audio capture...`
- [ ] Shows compact command list: `suggest_questions` · `research` · `offerings` · `prototype` · `stop`

---

## Step 2 — Let it process (~30 seconds)

Keep audio playing. Sidekick captures 5-second chunks, transcribes with Whisper, and batches transcript lines every 10 seconds for LLM classification.

**Watch for** (in VS Code Output panel → "sidekick"):
- [ ] `Audio capture: <device> (rate=48000, ch=2)` log line
- [ ] `Processing audio with duration 00:05.xxx` from faster_whisper
- [ ] `Analysed N lines → M triggers` from the classifier

---

## Step 3 — Check status

```
status
```

**Expected**:
- [ ] Shows `HMRC — 🎙️ live (Whisper) — X min — Y lines`
- [ ] If threads detected: listed with ⏳/✅ status icons
- [ ] If research completed autonomously: results shown under "NEW SINCE LAST CHECK"
- [ ] If nothing detected yet: `Listening... no threads detected yet.`

---

## Step 4 — Test manual tools

### 4a — Research

```
research What is DirectLake mode in Microsoft Fabric?
```

- [ ] Returns an answer with explanation
- [ ] Includes sources (workspace docs, instructions, or MS Learn)

### 4b — Offerings

```
offerings lakehouse migration
```

- [ ] Returns a list of VBD/IP offerings with type labels (PoC, ADR, WorkshopPLUS, etc.)
- [ ] Each offering has a title and eng.ms URL
- [ ] Results are relevant to the topic (e.g. Fabric PoC for lakehouse queries)

**Quick command:**

```
o
```

- [ ] Infers topic from meeting context (requires active `listen` session)
- [ ] Returns offerings or a "no topic available" message if no context

### 4c — Prototype

```
prototype Bronze to Silver notebook for customer transactions
```

- [ ] Returns PySpark notebook code
- [ ] Code follows medallion architecture conventions
- [ ] Includes audit columns (`_ingested_at`, `_updated_at`, etc.)

### 4d — Suggest questions

```
suggest_questions
```

- [ ] Returns categorised questions (clarify, probe, challenge, etc.)
- [ ] Each question has impact level (🔴 high / 🟡 medium)
- [ ] Rationale explains why each question matters
- [ ] Grounding indicator — references instruction files or engagement artifacts
- [ ] Corrections section — flags any inaccurate consultant statements (or states "none")
- [ ] Observations section — patterns noticed in the conversation
- [ ] If not enough transcript yet: `Not enough transcript yet` message

### 4e — Quick commands

Test single-letter shortcuts interpreted by the agent:

| Type | Expected tool call |
|------|-------------------|
| `s` | `status` |
| `q` | `suggest_questions` |
| `o` | `offerings` (from meeting context) |
| `x` | `stop` |

---

## Step 5 — Check notifications

After the background loop processes a finding:

- [ ] Windows system sound plays (beep/chime)
- [ ] Alert appended to `~/.sidekick/live/alerts.jsonl`
- [ ] Alert JSON contains `timestamp`, `type`, `summary`, `priority`
- [ ] MCP output channel ("sidekick") shows the finding

---

## Step 6 — Check status again

```
status
```

- [ ] Shows research/prototype results under "NEW SINCE LAST CHECK"
- [ ] Output count incremented
- [ ] Thread list updated if new topics detected from audio

---

## Step 7 — Stop and get summary

```
stop
```

**Expected**:
- [ ] Session summary with duration in minutes
- [ ] Count of outputs generated
- [ ] Outputs grouped by type (RESEARCH, PROTOTYPE, etc.)
- [ ] Open threads listed (if any)
- [ ] Action items listed (if any)
- [ ] Log line: `Session saved to C:\Users\koladimeji\.sidekick\outputs\hmrc\sidekick_session_*.json`
- [ ] No crash or hanging — clean shutdown

---

## Step 8 — Verify the saved session file

Open `C:\Users\koladimeji\.sidekick\outputs\hmrc\` in Explorer.

- [ ] New `sidekick_session_YYYYMMDD_HHMMSS.json` file exists
- [ ] JSON contains `customer: "HMRC"`
- [ ] JSON contains `session_start` and `session_end` timestamps
- [ ] JSON contains `outputs` array with recorded results

---

## Step 9 — Test config switch

```
listen --config moj
```

- [ ] Consent prompt appears first (same flow as Step 1)
- [ ] After confirming, responds instantly with `✓ Config: moj.yaml (MoJ)` (not HMRC)
- [ ] Domain list differs from HMRC session
- [ ] Threshold differs (MoJ=0.7 vs HMRC=0.6)

```
stop
```

- [ ] Session saved to `C:\Users\koladimeji\.sidekick\outputs\moj\` (separate directory)

---

## Step 10 — Test silence auto-stop

```
listen --config hmrc
```

1. **Stop all audio** on your machine
2. Wait ~60 seconds
3. Run:

```
status
```

- [ ] Shows `⚠️ ERROR: Auto-stopped: no speech detected for 60s`
- [ ] Listen loop is no longer active

```
stop
```

- [ ] Clean shutdown, session saved

---

## Step 11 — Test "already listening" guard

```
listen --config hmrc
```

Then immediately:

```
listen --config hmrc
```

- [ ] Second call returns: `Already listening. Call stop to end the session first.`

```
stop
```

---

## What to evaluate

| Area | What to look for |
|------|-----------------|
| **Transcription accuracy** | Does Whisper pick up speech correctly? Are words recognisable? |
| **Classification quality** | Does it detect real questions vs background chatter? Is the trigger threshold appropriate? |
| **Research relevance** | Do answers reference your workspace files and `.github/instructions/`? |
| **Prototype quality** | Does generated code follow Fabric conventions (medallion, audit columns, naming)? |
| **Question suggestions** | Are they contextually relevant? Do corrections, observations, and offerings appear? |
| **Notifications** | Does the system sound play? Are alerts written to alerts.jsonl? |
| **Quick commands** | Do single-letter shortcuts (s, q, o, x) invoke the right tools? |
| **Shutdown cleanliness** | No crashes, hangs, or orphaned threads after stop? |
| **Session persistence** | JSON file saved with complete data? |
| **Config isolation** | HMRC and MoJ sessions use different settings and output directories? |

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `sidekick` not in agent list | Run `sidekick init` in terminal, restart VS Code |
| `Missing live dependencies` | Run `pip install -e ".[live]"` in the sidekick venv |
| `Config file not found: .../configs/hmrc.yaml` | Config is in `~/.sidekick/customers.yaml`, not in `configs/`. Restart the MCP server to pick up changes |
| `speech.backend is 'azure' but no credentials` | Set `speech.backend: whisper` in `~/.sidekick/customers.yaml` |
| Falls back to default config | Restart the MCP server (the process may be using a stale cached version) |
| No audio captured | Check that your speakers/headset appear in the loopback device list |
| Whisper hallucinating | Normal for near-silence — the hallucination guard filters most junk |
| `Already listening` | Run `stop` first, then `listen` again |

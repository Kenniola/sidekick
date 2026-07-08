# Sidekick Design Spec — Accuracy & Focus

> Status: **SCOPED — decisions locked 07 Jul 2026**, not yet implemented.
> Scope: three workstreams — (A) relevance/accuracy, (B) notification focus,
> (C) transcription & speaker attribution.
> Author: Sidekick architecture pass, 07 Jul 2026.

## Guiding principles (from the ask)

1. **Accuracy first** — surface the *right* questions/answers, deeply reasoned.
2. **Less complexity** — smallest design that works; opt-in, no big rewrites.
3. **Less distraction during calls** — fewer, higher-signal interruptions.
4. **Easy implementation** — native primitives, additive changes, default-off.

## Root-cause analysis

Both symptoms share one cause: **the gate that decides "is this worth
surfacing?" is a fast-tier, recall-biased classifier that runs every ~10 s.**

- `TranscriptAnalyst.analyse_chunk` uses `tier="fast"` and is told
  *"when in doubt, score higher"* with `trigger_threshold = 0.5`. It is tuned
  for **recall**: many candidates, low precision.
- Every candidate ≥ 0.5 is enqueued, researched, and `notify()`d → one
  `alerts.jsonl` line → one sticky toast + badge increment. No supersede, no
  TTL, no consolidated view.
- The genuinely deep, reasoned path (`CONSULTANT_ADVISOR_PROMPT`, 5-step
  reasoning chain, **deep** tier) only runs in `suggest_questions` — on demand,
  never automatically. The classifier never even receives the **grounding**
  block that `suggest_questions` gets.

**Consequence:** low-precision surfacing → both verbose/irrelevant findings
*and* toast overload. Fixing the gate fixes both. Accuracy is therefore the
lever that also reduces distraction.

**Upstream caveat (Workstream C):** the classifier can only be as good as the
transcript it reads. Today's transcript has two quality problems — a mid-tier
Whisper model with fixed 5 s chunk boundaries, and "diarization" that is really
just static source-tagging. Garbage-in → garbage-out, so C underpins A.

---

# Workstream A — Accuracy (PRIORITY)

Design pattern: **cheap high-recall detector → deep high-precision adjudicator.**
Keep the fast classifier as a *candidate detector*. Add a periodic **deep-tier
adjudicator** that decides which few candidates are genuinely worth the
consultant's attention, grounded in team standards and engagement objectives.

## A1 — Relevance adjudicator (the core change)

**New module:** `src/sidekick/analyst/adjudicator.py`

```python
async def adjudicate(
    candidates: list[ActionItem],
    context: MeetingContext,
    config: SidekickConfig,
    grounding: str,
    *,
    llm_fn: LLMFn = call_llm,
) -> list[ActionItem]:
    """Deep-tier pass: from many fast-tier candidates, return the FEW that are
    genuinely worth surfacing — merged, re-scored, and each with a one-line
    rationale tied to the engagement's objectives. Degrades to a threshold
    filter on failure so it never blocks the loop."""
```

Behaviour:
- Runs on the **deep** tier (Opus) with a reasoning chain adapted from
  `CONSULTANT_ADVISOR_PROMPT`, but constrained to *judging the candidate list*
  (not free-generating).
- Inputs: candidate questions, active threads, recent buffer, **grounding**,
  **glossary**, **`stt_corrections`**, and new **engagement objectives**.
- Outputs, per surfaced item: recomputed `priority_score`, a short
  **`rationale`** ("why this matters to <objective>"), and merge decisions.
- **Precision gate:** only items ≥ `surface_threshold` (default **0.7**) and at
  most `max_surfaced_per_pass` (default **3**) are returned. Everything else is
  dropped or merged.
- **Consolidation:** near-duplicate/verbose candidates are merged into one
  well-formed question before returning (kills "verbose text" surfacing and
  shrinks the notification stream).

**New field** on `ActionItem` (classifier.py): `rationale: str | None = None`.

**Cadence & wiring** — `engine.classify_and_dispatch`:
- Fast `analyse_chunk` still runs every ~10 s (recall) but its items are
  **accumulated** into `state.pending_candidates` instead of enqueued directly.
- Flush the buffer through `adjudicate(...)`, then `enqueue` + `notify` only the
  survivors, when **either**:
  1. `adjudicator_interval_seconds` have elapsed (default **40 s**,
     fully configurable — lower it for a snappier feel), **or**
  2. **an early-flush trigger** fires first — a natural speech pause
     (VAD silence gap) or a `critical` consultant-hedge candidate
     ("I'll get back to you") — so urgent items surface fast without waiting
     out the interval. (Decision #2: keep the interval but make it configurable
     *and* add pause-triggered early flush, so 40 s is a ceiling, not a floor.)
- Implemented with a simple time/counter + pause check — **no new task or
  thread** (keeps complexity low).
- Grounding is already available via `_get_grounding_context_async()` (cached).

**Default-off safety:** gated behind `sensitivity.accuracy_mode`. When `false`,
behaviour is byte-for-byte unchanged (candidates enqueue immediately as today).

## A2 — Ground the gate

- Pass the grounding block + `glossary` + `stt_corrections` into the
  adjudicator (they exist already; just thread them in).
- **Engagement objectives** drive relevance scoring. (Decision #1) Two sources,
  in priority order:
  1. **Explicit at call start** — `add_context "goal: …"` (or `objectives:` in
     the profile). A `goal:`-prefixed context note is parsed into
     `context.objectives` and always wins.
  2. **Auto-inferred fallback** — if no goal is set, a one-shot **fast-tier**
     inference over the **first few minutes** of transcript proposes the
     likely objectives (mirrors the existing `detect_domains` pattern in
     `engine.py`), stored on `context.objectives` and shown once via the feed
     so the consultant can correct them. The adjudicator scores relevance
     *against these*, so it stops surfacing off-goal chatter.
- Pass `add_context` documents more fully to the adjudicator (today the
  classifier only sees the first 200 chars of the last 3 docs).

## A3 — Deep-default answers (accuracy over speed on the output side)

- **New config** `sensitivity.answer_tier: "auto" | "deep"` (default `"auto"`).
  In `accuracy_mode`, default to `"deep"`.
- Wire into `PriorityQueue._route` / `_COMPLEXITY_TIER`: when `answer_tier ==
  "deep"`, route substantive research to the deep lane regardless of the
  fast-tier complexity guess.
- **(Optional, Phase 4)** self-critique in the research pipeline:
  draft → critique against sources/grounding → finalise. Higher latency,
  higher accuracy. Flagged optional to respect the *less-complexity* principle.

## A4 — New config surface (all additive, default-off)

`SensitivityConfig` (config.py) gains:

| Field | Default | Purpose |
|-------|---------|---------|
| `accuracy_mode` | `false` | Master switch for the two-stage pipeline |
| `adjudicator_interval_seconds` | `40` | Deep-pass cadence ceiling (configurable) |
| `adjudicator_pause_flush` | `true` | Early-flush on a natural speech pause / critical hedge |
| `max_surfaced_per_pass` | `3` | Hard cap on surfaced items per pass |
| `surface_threshold` | `0.7` | Precision gate out of the adjudicator |
| `answer_tier` | `"auto"` | `"deep"` forces deep synthesis |

`SidekickConfig` gains `objectives: list[str]` (parsed in `_parse_config`,
documented in `configs/_template.yaml`). At runtime `context.objectives` is set
from an `add_context "goal: …"` note, else auto-inferred from the opening few
minutes (A2).

## A5 — Tests (Python, offline via injected `llm_fn`)

- `test_adjudicator.py`: below-threshold dropped; over-cap truncated to N;
  near-duplicates merged; rationale attached; empty-input no-op; LLM failure
  degrades to threshold filter.
- `test_config_merge.py`: new sensitivity fields + `objectives` parse/defaults.
- `test_priority_queue.py`: `answer_tier="deep"` routes to deep lane.
- `test_engine.py`: accuracy-mode accumulates then flushes on interval;
  default mode unchanged (immediate enqueue).

---

# Workstream B — Notification focus (less distraction)

Make the **feed** the primary channel; reserve **toasts** for urgent items.

## B1 — Priority-gated, self-dismissing toasts (`extension.ts`)

- **Decision #4:** only `priority` `critical`/`high` raise a toast during calls;
  `medium`/`low` go to the feed silently.
- High-priority toasts keep their action buttons. Any lower toast that is shown
  is **button-less**, so VS Code auto-fades it — this is the requested
  "stale notification is no longer active once a new one surfaces".
- Badge (`Sidekick (n)`) counts **unseen high-priority** items only, not every
  finding.

## B2 — "Sidekick Feed" view (the scrolling list)

- **Decision #3: activity-bar side view (persistent).** Add a **TreeView**
  (native, simpler than a Webview) in a Sidekick **view container on the
  activity bar**, so the feed is always one click away and survives across
  sessions. It tails `alerts.jsonl` (already polled) into
  an in-memory model and renders a live, scrollable list:
  - icon by type, priority colour, **relative timestamp**, one-line answer;
  - click → Open Source / Open File / View in Chat;
  - grouped by `thread_id` when present.
- The feed **is** the durable, scrollable record, so toasts no longer need to
  persist. `alerts.jsonl` is the growing file already; this just renders it.

## B3 — Supersede, dedup, TTL

- **Supersede:** a new alert sharing `thread_id`/`id` updates the existing row
  in place and marks the prior state as superseded (dimmed/description), rather
  than adding a row.
- **Dedup:** identical `id` within a short window does not re-toast.
- **TTL:** rows older than N minutes render dimmed ("stale"); a
  `sidekick-notify.clearSeen` command clears the badge/seen state.

## B4 — Alert schema additions (`notifier.write_alert` / `write_deliverables_alert`)

Add to the JSON line: `thread_id`, stable `id` (reuse `QueueItem.id`), and
`rationale` (from A1). Backward-compatible — the extension treats them as
optional.

## B5 — Extension packaging (`package.json`)

Contributes: a `viewsContainers` (activity bar) + `views` (the feed TreeView) +
commands (`openFeed`, `clearSeen`) + item context menus. Keep the alert→model
reduction in a **pure, unit-testable module** (`feedModel.ts`) so logic is
covered by tests; the VS Code glue stays thin. Manual test checklist for the
UI.

---

# Workstream C — Transcription & speaker attribution (underpins A)

Verified against the faster-whisper README and the `distil-large-v3` model card
(07 Jul 2026).

## C0 — What we run today

- Model: **`small.en` / int8 / CPU** (`WhisperConfig` default; `~5–7% WER`).
- Chunking: fixed **5 s** frames (`AudioCapture.chunk_duration`), cut on a hard
  time boundary regardless of whether someone is mid-word.
- Transcribe params: `beam_size=5`, `vad_filter=True` (defaults),
  `initial_prompt` = vocabulary prior + previous-chunk tail (5b/5e). No
  `vad_parameters`, `no_speech_threshold`, `log_prob_threshold`, or
  `word_timestamps` tuning.
- Repetition guard (`_last_text` / `_repeat_count`) is **shared across
  speakers**, while the coherence tail (`_prev_tail`) is per-speaker.

## C1 — Better Whisper model (biggest single accuracy win, easy)

**Recommended default: `distil-large-v3`** (English-only, 756 M params).
Verified: within **~1–1.5 % WER of large-v3**, **6.3× faster than large-v3**,
**fewer hallucinations**, natively supported in faster-whisper
(`WhisperModel("distil-large-v3")`), recommended with
`condition_on_previous_text=False` (we already do chunk-at-a-time prompting).

| Model | Params | Rel. accuracy | Notes |
|-------|--------|---------------|-------|
| `small.en` (current) | 244 M | baseline (~5–7% WER) | fast, but our floor |
| `medium.en` | 769 M | better | safe conservative bump |
| **`distil-large-v3`** | 756 M | ≈ large-v3 (−1.5% WER) | **recommended**; 6.3× faster than large-v3, low hallucination |
| `large-v3` / `turbo` | 1550 / 809 M | best | prefer only with a CUDA GPU (float16) |

- **Cost:** it's a one-line default change (`speech.model`) plus the existing
  `SIDEKICK_WHISPER_MODEL` override — trivial to implement and revert.
- **Decision #5:** make `distil-large-v3` the default **after** the benchmark
  passes on the target machine, and **expose the model choice at install /
  `sidekick init` time** — prompt (or `--stt-model` flag / `SIDEKICK_WHISPER_MODEL`)
  writing the chosen model into the profile's `speech.model`, defaulting to the
  benchmarked pick. So a fast box gets `distil-large-v3`, a constrained box can
  drop to `medium.en`/`small.en` without editing YAML.
- **Must benchmark** CPU real-time feasibility before defaulting: on an
  i7-12700K, faster-whisper `small/int8` does 13 min of audio in ~1m42s
  (~7.6× real-time), so there is headroom, but distil-large-v3 is heavier — the
  `sidekick benchmark-stt` step measures the real-time-factor on the actual
  machine and picks the largest model that keeps RTF < ~0.7. Keep int8 on CPU,
  float16 on GPU.

## C2 — Structural transcript quality (independent of model)

1. **VAD-aligned chunking** instead of fixed 5 s frames — accumulate audio
   until a Silero-VAD speech pause, so chunks are whole utterances and words
   aren't sliced at the boundary. This is the biggest *structural* win and also
   aligns chunks to speaker turns (helps C3). Alternatively, raise
   `chunk_duration` to ~8–10 s and tune `vad_parameters(min_silence_duration_ms)`.
2. **Tune decode thresholds** to cut hallucination/clipping: pass
   `vad_parameters`, `no_speech_threshold`, `log_prob_threshold`,
   `compression_ratio_threshold`.
3. **Per-speaker repetition state** — key `_last_text`/`_repeat_count` by speaker
   (like `_prev_tail`), so one speaker's repeat can't suppress the other's line.
4. Optionally enable `word_timestamps=True` (needed for C3 Tier 3 alignment).

## C3 — Speaker attribution (the "diarization" that isn't)

**Root cause of "not diarizing well":** there is no diarization — only static
**source-tagging** in `AudioCapture` + `engine._merge_captures`:

- **Default `capture_microphone=false` ⇒ every line is `(audio)`** — zero
  speaker separation. This is almost certainly what was observed.
- Even enabled, there are only **two buckets**: `(me)` (mic) vs `(remote)` (all
  system audio). **Every remote participant collapses to `(remote)`.**
- The two captures are fanned in by **arrival order**, each with its **own
  offset clock** from its own `begin()` → cross-stream ordering/timestamps
  drift; overlapping speech is mis-ordered.
- **Echo/bleed:** the local voice can appear in *both* mic and loopback →
  duplicate lines with different labels; the shared repetition filter can't
  catch it.

Tiered fix (by cost, aligned to *less-complexity*):

- **Tier 1 — correctness (easy, live, no new deps).**
  - Per-speaker repetition state (C2.3).
  - **Shared monotonic session clock** across captures so `(me)`/`(remote)`
    lines interleave and timestamp correctly (align offsets to one session
    start, not per-capture `begin()`).
  - **Echo suppression:** drop a loopback segment whose text closely matches a
    recent mic segment within a small time window (removes the local-voice echo
    from `(remote)`).
- **Tier 2 — LLM speaker naming (medium, high value, no heavy deps).**
  The analyst already identifies client participants. Have it attribute
  `(remote)` utterances to **named** participants from introductions/turn cues,
  producing a readable, named transcript. This is the pragmatic "diarization"
  that actually helps the read, fits the deep-reasoning theme, and needs no
  torch/pyannote.
- **Tier 3 — accurate post-call diarization. DEFERRED (not in current scope).**
  (Decision #6: Tier 1+2 is enough for now.) Recorded for the future: at `stop`,
  run **WhisperX** (wav2vec2 alignment + pyannote) or **whisper-diarization**
  (faster-whisper + NeMo) on the saved audio for a properly diarized
  deliverables transcript. Heavy deps (torch, pyannote, HF token), batch-only,
  opt-in — to be revisited only if named attribution proves insufficient.

## C4 — Config surface (additive)

`SpeechConfig` gains (all with safe defaults / env overrides):

| Field | Default | Purpose |
|-------|---------|---------|
| `model` | `distil-large-v3` (after benchmark; chosen at install) | accuracy |
| `chunk_seconds` | `5` (→ VAD-aligned) | utterance-aligned chunking |
| `vad_min_silence_ms` | `500` | boundary tuning |
| `diarization` | `"named"` (Tier 2) — `"source"` to disable; `"post"` deferred | C3 tiers |

## C5 — Tests

- `test_speech_recogniser.py`: per-speaker repetition isolation; threshold/VAD
  params passed through; model/compute resolution unchanged.
- `test_audio_capture.py`: shared-clock offsets monotonic across captures;
  echo-suppression drops the duplicated mic-echo line.
- `test_engine.py`: merged stream ordered by shared clock.
- LLM speaker-naming: unit test with an injected `llm_fn` (offline).
- Add `sidekick benchmark-stt` and a manual accuracy A/B checklist
  (`small.en` vs `distil-large-v3`) on a recorded sample.

---

# Cross-cutting: A makes B easier

With the adjudicator capping surfacing at ≈3 high-precision items per pass
(configurable interval, default ~40 s, with early flush on natural pauses) and
attaching a rationale, the notification stream is already an order of magnitude
smaller and self-explaining — so B's feed stays calm and toasts stay rare.

---

# Phased delivery (recommended order)

| Phase | Deliverable | Risk | Why here |
|-------|-------------|------|----------|
| **0** | C1 model bump (`distil-large-v3`) + `benchmark-stt` + C2.3 per-speaker repetition | Low | Cheapest, biggest raw-accuracy win; better transcript feeds every later phase |
| **1** | A1+A2+A4 adjudicator, grounded, `accuracy_mode` (default off) | Low (opt-in) | Core accuracy win; also cuts alert volume |
| **2** | C2 VAD-aligned chunking + decode-threshold tuning; C3 Tier 1 correctness | Med | Structural transcript quality + correct me/remote interleave |
| **3** | A3 deep-default answers in accuracy mode | Low | Output-side precision |
| **4** | B1+B2+B3+B4+B5 feed + gated toasts | Med (UI) | Focus; benefits from Phase 1's lower volume |
| **5** | C3 Tier 2 LLM speaker-naming | Med | Readable named transcript, no heavy deps |
| **6** | Optional: A3 self-critique synthesis | Med | Extra accuracy, only if wanted; opt-in |

> **Deferred (not scheduled):** C3 Tier 3 post-call pyannote/NeMo diarization —
> revisit only if Tier 1+2 named attribution proves insufficient (Decision #6).

Phase **0** is deliberately first: it's a near-trivial change with the largest
raw-accuracy payoff, and a cleaner transcript improves the classifier and
adjudicator downstream. Everything heavy (post-call diarization, self-critique)
stays optional and off the live path.

Rationale for order: Phase 1 is the biggest accuracy lever *and* reduces
distraction, is default-off (zero regression), and is pure-Python (easy to
test). Notifications (Phase 3) land after volume is already down, so the UI work
is validated against a realistic, calmer stream.

---

# Non-goals (to control complexity)

- No streaming/partial-answer redesign.
- No new model providers/adapters.
- No always-on deep classification of every 10 s chunk (cost/latency); deep
  reasoning is the *periodic adjudicator*, not the per-chunk detector.
- No Webview feed (TreeView is enough and simpler).
- No real-time pyannote/NeMo diarization on the live path (too heavy); accurate
  diarization is post-call only and opt-in (C3 Tier 3).
- No move away from local, on-device Whisper (privacy posture preserved).

---

# Decisions (locked 07 Jul 2026)

1. **Objectives source** — accept `add_context "goal: …"` at call start; if none
   is set, **auto-infer** objectives from the first few minutes of transcript
   (fast-tier, shown once for correction). (A2)
2. **Adjudicator cadence** — keep the interval but make it **configurable**, and
   add **pause-triggered early flush** (natural silence gap / critical hedge) so
   40 s is a ceiling, not a floor. (A1, A4)
3. **Feed placement** — **activity-bar side view, persistent.** (B2)
4. **Toast floor** — **`critical` and `high`** raise toasts during calls. (B1)
5. **STT model** — make **`distil-large-v3` the default after the benchmark**,
   and expose the model choice **at install / `sidekick init`** so it's
   configurable per machine. (C1)
6. **Speaker attribution** — **Tier 1 + Tier 2** (correct interleave + LLM
   naming) for now; Tier 3 post-call diarization **deferred**. (C3)

---

# Post-test findings — MoJ session, 08 Jul 2026 → Phases 5–7

Studied line-by-line: `transcript_20260708_101235.txt` (28 min, ~20 findings),
`alerts.jsonl`, and `deliverables_20260708_104134.md`. Config at test time:
**`small.en`**, **`accuracy_mode: false`**, **no objectives / glossary**.

## What worked
- Transcript quality is materially better (Phase 0/2) even on `small.en`: domain
  terms mostly correct, few hallucinations, coherent sentences.
- The Feed (Phase 3) is a clear win for thread tracking.
- The follow-up email is coherent and captures the real topics.

## Evidence-based problems

**P1 — Low precision (adjudicator was off).** The fast classifier surfaced
conversational **statements**, consultant **self-coaching**, and garbled
fragments as findings, e.g. *"We should consider GitHub…but I don't think I
want to"*, *"I don't think we use the HR tables in F&O"*, *"You've got the right
split, Kenni…"* (Sidekick answering the consultant's own advice), *"Depending on
what the data size, for instance, I plan to use it."* → **Enabling
`accuracy_mode` (Phase 1 adjudicator) is the single biggest fix**; not yet on.

**P2 — Enrichment stacks duplicate feed rows.** Re-research emits
`[ENRICHED] <question>` with a *changed* summary, so its feed `id` differs from
the original and the Phase 3 supersede can't collapse it (e.g. accessibility ×4,
Copilot Word/Excel ×4, "request process" ×3). The id must be stable across
enrichment.

**P3 — Action items missed in deliverables.** The pack said *"No action items"*
yet the classifier had flagged `action_item` findings ("two slides on strategic
direction", "schedule a workshop"). Classifier `action_item` results never reach
`context.action_items`, so the deliverables table is blank.

**P4 — Follow-up batch contains garbled/duplicate fragments.** The
customer-facing follow-up list includes incomplete sentences (*"…as the diagram
suggested yesterday, that you guys brought up because."*) and near-duplicates.

**P5 — No speaker attribution.** Every line is `(audio)`; a 6-person meeting
collapses to one speaker, so Sidekick can't tell a client question from the
consultant's own statement (which feeds P1).

**P6 — STT mishears key terms.** `ADO → "ADL"` (flips meaning), `workspaces →
"word spaces"`, `Copilot → "CodePilot"`, `Dataverse → "today diversity"`,
`Cabinet Office → "cabin office"`, `offshore → "go-for-shore"`. **Glossary +
stt_corrections (Phase 5f, already built) are unused** on this profile.

## Do-now config wins (zero new code — already shipped)
1. `sensitivity.accuracy_mode: true` on the MoJ profile → adjudicator filters P1.
2. Add `objectives:` (or `add_context "goal: …"`) so relevance is goal-scored.
3. Add `glossary:` (Azure DevOps/ADO, OneLake, Dataverse, Databricks, F&O,
   Dayforce, workspace, CI/CD) + `stt_corrections:` for the P6 mishears.
4. Optionally `answer_tier: deep` (Phase 4) for higher-accuracy answers.

### Config decisions (08 Jul 2026, applied)
- **STT model** — keep **`small.en`**; benchmark showed `distil-large-v3` runs
  at ~1.8× real-time on the ARM64/CPU box (not viable live). Revisit only with a
  CUDA GPU. A model change is *not* the fix for mishears.
- **`glossary` over `stt_corrections`.** `glossary` seeds Whisper's vocabulary
  prior so terms are recognised *at source* (prevention) — not a hardcoded
  find/replace. `stt_corrections` is reserved for stubborn homophones only; not
  used for MoJ. Applied MoJ glossary: Azure DevOps, ADO, OneLake, Dataverse,
  Databricks, Synapse, Copilot, Microsoft Fabric, Dynamics 365 Finance &
  Operations, SSRS, ADLS Gen2, WCAG, FUAM. (F&O expanded to the full product
  name; "Dayforce" excluded pending confirmation it is a real system.)
- **`objectives` are not per-call.** Default is **auto-inference** from the
  opening minutes (Phase 1 / A2). `add_context "goal: …"` or a profile
  `objectives:` list are optional, not required. Kept the MoJ profile light
  (no objectives) to avoid config burden.
- **`accuracy_mode: true`** enabled on the MoJ profile.

## Phase 5 — Feed UX & session hygiene (extension + small Python)
- **5.1 Category tags + legend.** Prefix each feed row with a short type tag
  (`[research]`, `[sizing]`, `[roadmap]`, `[action]`, `[deliverable]`) beside the
  icon; document the icon legend. (P feedback 2b)
- **5.2 Drill-down.** Make feed rows **expandable** (collapsible children:
  rationale, answer preview, sources, thread); add a per-row **"View in Chat"**
  that surfaces *that* finding, not the generic `status`. (2b/2c)
- **5.3 Session boundary.** On `listen`, rotate `alerts.jsonl` (archive the prior
  file, write a `session_start` marker); the extension **clears the feed** on a
  new session so old findings don't linger, and the file can't grow unbounded.
  (2d)
- **5.4 Stable enrichment id.** Base the alert `id` on the *original* question so
  enrichment updates the same feed row (fixes P2, complements Phase 3 supersede).

## Phase 6 — Relevance & accuracy engine (Python)
- **6.1 Classifier precision.** Sharpen the analyst prompt to **not** surface the
  consultant's own statements/coaching, statements-of-intent, or garbled
  fragments; require a genuine client question or a verifiable claim. (P1)
- **6.2 Action-item capture.** Route `action_item` classifications into
  `context.action_items` so the deliverables table is populated. (P3)
- **6.3 Follow-up batch hygiene.** Filter the deliverables follow-up list to
  well-formed, de-duplicated questions (drop incomplete fragments; optional
  LLM tidy). (P4)
- **6.4 Enrichment restraint.** Only re-notify when the enriched answer
  *materially* changed; otherwise update the existing row silently. (P2)

## Phase 7 — LLM speaker-naming (was Phase 5 / C3 Tier 2)
Now clearly justified by P5: attribute `(audio)`/`(remote)` lines to named
participants so the transcript reads correctly **and** the classifier can apply
the P1 "don't research the consultant's own words" rule.

## Recommended order
**Do-now config wins** (immediate, no build) → **Phase 6** (relevance engine —
biggest accuracy lever now that the pipeline exists) → **Phase 5** (feed UX,
mostly extension) → **Phase 7** (speaker-naming, enables deeper 6.1).

---

# Delivery status (as of 08 Jul 2026)

| Phase | Scope | Status |
|-------|-------|--------|
| 0 | STT benchmark + model selection + per-speaker repetition | ✅ shipped |
| 1 | Two-stage relevance adjudicator + objectives | ✅ shipped |
| 2 | Decode tuning + chunking + echo suppression | ✅ shipped |
| 3 | Feed view + gated toasts + supersede/dedup | ✅ shipped |
| 4 | Deep-default answers + self-critique | ✅ shipped |
| 5 | Feed UX & session hygiene | ✅ shipped |
| 6 | Relevance & accuracy engine | ✅ shipped |
| 7 | LLM speaker-naming | ✅ shipped |
| 8 | Research quality + feed clarity | ✅ shipped |
| 9 | Keyless web search + feed prettify + auto-suggest | ⬜ planned |

Config applied to the MoJ profile: `accuracy_mode: true`, glossary. STT model
stays `small.en` (benchmark). Extension at v0.6.0.

---

# Post-test findings #2 — MoJ session 2, 08 Jul 2026 → Phase 8 (shipped)

Second run confirmed the earlier fixes working (action-item table populated,
cleaner follow-ups, `ADO` correct via glossary, partial speaker-naming). New
issues found and fixed in **Phase 8**:

- **8.1 Relevance-aware source ranking.** Hits were ranked by *source trust only*,
  so an off-topic `learn.microsoft.com` page outranked an on-topic one (e.g. a
  Cosmos DB URL cited for a Terraform question). Now scored by topical relevance
  × trust; off-topic high-trust pages are demoted, and a relevant reputable
  non-Microsoft source can outrank an off-topic Microsoft page.
- **8.2 Extended verified sources.** Added HashiCorp/Terraform + reputable
  technical hosts to the trust map + routing.
- **8.3 Relevance-floored citations.** Only sources clearing a relevance floor
  are cited — a weakly-matched URL is dropped rather than surfaced.
- **8.4 Real confidence.** Confidence is parsed from the model's stated
  HIGH/MEDIUM/LOW instead of a hardcoded "medium".
- **8.5 / 8.6 Feed clarity.** Rows lead with the **question**; drill-down shows
  the **full answer** + real confidence; **"Research in Chat"** surfaces the
  finding as `@sidekick research <question>`.

**Limitation surfaced:** non-Microsoft URLs still require a web-search key
because MS Learn (the only keyless provider) returns Microsoft-only content →
Phase 9.

---

# Phase 9 — Keyless web reach, feed polish, proactive suggestions

## 9.1 Keyless web search (no per-user API keys)
Adoption blocker: requiring each user to configure a Tavily/Brave key. Fix,
layered:
- **DuckDuckGo default provider** — a keyless package (`ddgs`) adds real web
  results (AWS, HashiCorp, Databricks, etc.) with no key/signup, run through the
  existing trust-map + relevance ranking. Best-effort: degrades to MS Learn +
  model knowledge on rate-limit/failure.
- **Shared org key (optional).** One `TAVILY_API_KEY` set centrally (installer /
  default config) that all installs inherit — nobody configures anything; used
  as the reliable override when present.
- **LLM-proposed-URL validation (safety net).** The synthesis model proposes
  canonical doc URLs from training knowledge; each is HTTP-validated (200 from a
  trusted host) before citing, so niche non-MS topics get accurate links with no
  search API and no hallucinated URLs.
- **Provider precedence:** shared/user key (Tavily/Brave) → DuckDuckGo →
  MS Learn only. Ranking/verification unchanged.

## 9.2 Feed prettify (readability)
VS Code TreeItem **labels are plain text** (can't render markdown), so
`**Direct answer:**` shows literal asterisks. Fixes:
- **Strip markdown** from row labels (`**`, `##`, `_`) for clean one-liners.
- **Numbering** — running `1.`, `2.`, `3.` prefix per row.
- **Confidence at a glance** — icon colour by confidence (green/amber/grey) +
  a `HIGH`/`MED`/`LOW` tag in the description.
- **Group by thread** — collapsible thread parent nodes with findings nested
  underneath (needs `thread_id` populated on alerts — small Python change).
- Keep the rich markdown answer in the hover tooltip (already there).

## 9.3 Proactive auto-suggested questions
Make the existing (manual) `suggest_questions` reasoning **proactive**: a
periodic pass (piggybacking the adjudicator cadence) that, once context is rich
enough, generates **1–2 high-value questions the consultant should ask the
client now** and slots them into the feed as a distinct type (`💡 [ask]`).
Guardrails: config-gated (`sensitivity.auto_suggest`), capped at 1–2 per pass,
deduped, only on genuinely new context — proactive without nagging.

## Recommended order
**9.1** (keyless web — biggest remaining accuracy gap) → **9.2** (readability) →
**9.3** (autonomy). All additive/opt-in; defaults unchanged.



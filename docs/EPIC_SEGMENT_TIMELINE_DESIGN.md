# Epic — Interleaved Segment/Block Timeline (assistant-turn render model)

> **Status: DESIGN-FIRST. No production code until owner sign-off on §7.**
> Board epic `pt_cb8f98b0cb9b47fb`. Claimed by conversation working the
> "split the ptool panel" request. This document is the review artifact —
> it grounds the decision in the measured blast radius before a line of the
> orchestrator or the perf-critical frontend is touched.

---

## 1. Problem statement — two symptoms, one root cause

Today an assistant turn is stored as **three parallel channels** on the task
dict, not as a timeline:

| Channel | Shape | Meaning |
|---|---|---|
| `task['content']` | one accumulator string | the deliverable answer |
| `task['thinking']` | one accumulator string | reasoning trace |
| `task['toolRounds']` | list of per-round dicts | tool calls + results |

They are **not interleaved**. The chronological order in which the model
actually produced "thinking → prose → tool call → more prose → tool call →
final answer" is *lost* the moment it is stored. Two user-visible symptoms
both trace to this single missing structure:

### Symptom A — headless "narrator" leak (the reported pain)
The **streaming** compat generators forward every `delta` event verbatim:
- `lib/compat/openai.py:176-179` — `content`→`delta.content`, `thinking`→`delta.reasoning_content`.
- `lib/compat/anthropic.py:225-232` — `content`→`text_delta`, `thinking`→`thinking_delta`.

Because there is no structural boundary between *scaffolding prose*
("Now let me check the utility functions.") and the *deliverable*, a headless
streaming consumer receives ALL of it concatenated — the model reads like a
narrator describing its own actions. The interactive app dodges this ONLY via
an out-of-band `DELTA_RESET` event (`orchestrator.py:112`, the sole emission
site) that tells the browser to clear the bubble after each tool round. **The
compat generators do not consume `DELTA_RESET`** — and the standardized
OpenAI/Anthropic wire protocols have no frame that could carry it. So the
app's fix is fundamentally un-portable to the headless surface.

The **sync** compat path is different and mostly clean: `_assistant_message`
(`openai.py:100`) and `_content_blocks_from_task` (`anthropic.py:120`) read
the *flattened* `task['content']`, which `_discard_pretool_prose`
(`orchestrator.py:82-110`) has already stripped of inter-round prose. But
that fix is **lossy**: the app literally cannot show or return what the model
said before each tool call, even when that prose is useful.

### Symptom B — grouped-by-type layout (the requested change)
The frontend renders three flat sibling zones — `data-zone="tool"`,
`"thinking"`, `"content"` (`streaming_ui.js:15-33`) — so ALL tools group
together, ALL thinking groups together, ALL content groups together, never
adjacent to the tool that produced them. This contradicts the project's
tool-centric-layout preference.

### The root cause (single)
**There is no ordered, typed event log of what the turn produced.** Every
downstream — the browser, the compat surfaces, DB persistence, compaction,
Continue — reconstructs order from three unordered channels + one out-of-band
reset event. The robust fix is to make the turn produce **one ordered list of
typed segments** and make every consumer render *from that list*.

> **Alignment note.** This is the **superset** of parked epic
> `pt_2f7b65aab89a49ef` ("make the persisted `task_events` log the
> AUTHORITATIVE render source"). An ordered typed-segment list *is* an
> authoritative render log. This epic should absorb that one, not compete
> with it. Flagged to the owner in §7.

---

## 2. The segment model (the new source of truth)

Introduce an **append-only ordered list** `task['segments']`, each entry a
typed block. This is deliberately the **Anthropic content-blocks shape** we
already half-emit — no new vocabulary to invent:

```jsonc
// task['segments'] — ordered, append-only, the SoT for one assistant turn
[
  { "type": "thinking", "text": "...", "signature": "opaque-claude-sig",
    "llmRound": 0 },
  { "type": "text", "text": "Let me check the files.",
    "deliverable": false, "llmRound": 0 },          // ← scaffolding prose
  { "type": "tool_use", "id": "toolu_x", "name": "read_files",
    "input": {...}, "llmRound": 0,
    "result": { "content": "...", "status": "done" } },
  { "type": "text", "text": "The bug is on line 42. Here is the fix:",
    "deliverable": true, "llmRound": 1 }            // ← the answer
]
```

Field rules:
- **`type`** ∈ `thinking | text | tool_use`. (`tool_result` is nested under
  its `tool_use` as `result`, so a tool and its output are one unit — this is
  what makes per-tool rendering trivial.)
- **`deliverable`** (text only) — `true` for the terminal answer prose,
  `false` for inter-round narration. This is the structural boundary that
  Symptom A lacks. It **replaces the lossy `_discard_pretool_prose` discard**:
  we KEEP the narration (so the UI can show it inline, per the request) but
  MARK it, so headless consumers can filter to `deliverable:true` and never
  narrate.
- **`llmRound`** — groups segments produced in the same assistant API turn
  (preserves the existing batching invariant; see §3 inv-7).
- **`signature`** — the opaque Claude thinking signature (invariant inv-4).

### 2.1 Backend-first: segments as SoT, the three channels as DERIVED views
To bound blast radius, **the three channels do not disappear on day one.**
They become *derived projections* of `segments`, computed once at
finalization:
- `task['content']` = concat of `text` segments where `deliverable:true`.
  (Byte-identical to today's post-`_discard_pretool_prose` value → every
  reader in §3 keeps working unchanged.)
- `task['thinking']` = concat of `thinking` segment text (last-round, per
  current behavior).
- `task['toolRounds']` = segments regrouped by `llmRound` into the existing
  per-round dict shape (`assistantContent` = that round's non-deliverable
  text; `tool_calls`/`toolResults` from `tool_use` segments; `thinking`/
  `thinkingSignature` preserved).

This is the **strangler-fig** approach: `segments` is authoritative and new
code reads it; legacy readers keep seeing the projections until each is
migrated deliberately, with its own test. Nothing in §3's inventory breaks on
the first landing.

---

## 3. Measured blast radius (ground truth — every consumer a segment model must keep working)

*(Inventory verified by direct grep/read, file:line-cited. This is what "as
derived views" must reproduce byte-for-byte on landing.)*

**`task['content']` writes:** accumulation `manager.py:2182` (`_on_content`);
resets/overrides at `orchestrator.py:110` (`_discard_pretool_prose`), `:367`
(auto-retry), `:455` (Sources footer), `:546` (safety-filter), `:1701`
(Continue `contentPrefix` re-seed), `:2474` (endpoint per-turn); endpoint
sets `endpoint.py:1304,1331`, `orchestration_endpoint_runner.py:379-389`.
**reads:** DB persist `manager.py:836` (`_upsert_task_row`); conv sync
`:1219-1246`,`:1583` (keep-longer guard); checkpoint `:1471,1480`; compat
`anthropic.py:124`, `openai.py:100`; STATE/poll `routes/chat.py:1518,1859`.

**`task['thinking']`:** write `manager.py:2175` (`_on_thinking`) + paired
resets; reads `manager.py:837,1245,1591`, compat `anthropic.py:123`,
`routes/chat.py:1519`, `api_v1/chat.py`, `trajectory.py:87`.

**`task['toolRounds']`:** appended/stamped in `tool_dispatch.py:725-794`
(+`assistantContent`/`thinking`/`thinkingSignature`/`llmRound`/`toolContent`),
`streaming_tool_executor.py:259-263`, `executor.py:531-533`; reset
`orchestrator.py:373,2480`; mutated by compaction `_layer1.py:177`,
`_builtin_steps.py:166-442`. **reads:** `manager.py:725` (`_merge_tool_rounds`)
→ persist/sync/checkpoint; compat last-round `anthropic.py:126`,`openai.py:101`;
rebuild `conv_message_builder.py:495` (`_reconstruct_tool_call_messages`) +
`message_builder.py:113` (`inject_tool_history`); STATE/poll
`routes/chat.py:1523,1990`.

**SSE content contract:** emit `DELTA` `manager.py:2176,2183`; emit
`DELTA_RESET` `orchestrator.py:112` (only site); specs `events.py:124,218,230`;
consumers `compat/*`, `api_v1/chat.py`, `agent_run.py`.

### Invariants a segment model MUST preserve (or it ships a regression)
1. **`assistantContent` = pre-tool prose snapshot** — already stamped on the
   round (`tool_dispatch.py:563,725`) even though `task['content']` is zeroed.
   Reconstruction replays it. → maps to `deliverable:false` text segments.
2. **Compaction truncates tool RESULT text but preserves
   `toolName`+`toolArgs`+`toolCallId`+`status`** — `_reconstruct_tool_call_messages`
   hard-requires those four; placeholders written durably (`_layer1.py:177`).
   → compaction rewrites `segment.result.content`, never the identity fields.
3. **Anthropic rejects assistant prefill** — trailing deliverable text is
   never re-injected as a trailing assistant turn (`orchestrator.py:1693-1701`).
   → segment model cannot rely on replaying trailing `deliverable` text on
   Continue against Claude.
4. **Thinking replay needs BOTH text and signature** (`message_builder.py:614`)
   — Anthropic rejects unsigned thinking. → `thinking` segment keeps
   `signature`; replay gated exactly as today.
5. **Keep-longer guard** (`manager.py:1196`) — conv content/thinking never
   overwritten with a shorter value. → the derived `task['content']`
   projection preserves this at the sync boundary.
6. **`_merge_tool_rounds` = `_checkpointToolRounds + toolRounds`**, and
   `task['toolRounds']` is NOT pre-seeded with checkpoint rounds
   (`orchestrator.py:1706`, frontend double-count avoidance). → segments for a
   Continue turn start empty and are merged with checkpoint segments only at
   persist/done time.
7. **Batch by `llmRound`**; compat reads only `rounds[-1].tool_calls`. →
   `llmRound` on every segment.
8. **`DELTA_RESET` keeps tool rounds; `RETRY_RESET` clears them** — the model
   must distinguish "drop a narration segment" from "drop the whole attempt."

---

## 4. Frontend cutover (three global zones → per-tool timeline)

*(Migration risks measured against the actual perf machinery — this is the
high-risk half and is sequenced LAST.)*

**Highest risk — the frozen-prefix incremental render assumes ONE content
element.** All incremental state (`_frozenLen`, `_frozenHtml`,
`_streamRendered`, `.md-stream-tail`) lives on the single `contentZone`
(`streaming_ui.js:229-290`). A per-tool timeline needs N freeze states, but
critically **only the LAST (currently-streaming) segment has a live tail** —
every earlier segment is fully settled the instant its tool call fires. This
*inverts* today's model (one message-wide moving tail) into (many frozen
blocks + one trailing live tail). That inversion is actually *simpler* to
reason about, but it is a genuine rewrite of the hottest render path.

Other measured risks:
- **Zone cache holds exactly one content/think ref** (`streaming_ui.js:52`)
  → becomes a per-round map `roundNum → {think,content}`; invalidation must
  survive `_syncToolRoundsDOM` rebuilding `.ptool-panel-body`.
- **`_ensureStreamZones` flat sibling order** (`:15-33`) → content/thinking
  move INSIDE the `[data-prn]` slots (`tool_rounds.js:2445-2449`); but
  `_syncToolRoundsDOM` rewrites the panel body wholesale on fingerprint
  change, which would destroy in-slot incremental DOM. Needs a surgical
  per-slot diff instead of innerHTML replace.
- **Two disjoint fingerprints couple** — `_roundsFingerprint` (tools only)
  must not fire a full panel rebuild on a trailing-segment content delta, or
  perf collapses. Split fingerprints per-segment.
- **Static/DB parity** — `renderMessage` (`chat_render.js:985-996`) and
  `_msgFingerprint` (single `content.length`+`thinking.length`) fold global
  lengths; both switch to per-segment. `_renderUnifiedToolLine`
  (`tool_rounds.js:1135`) today renders ONLY the tool — it must gain adjacent
  `assistantContent`/`thinking` rendering (the data already exists on rounds).
- **Lowest risk:** rAF coalescing (`_twFlush`, `health_stream_timer.js`) is
  content-agnostic — unchanged.

---

## 5. Build order (strangler-fig, each step green before the next)

1. **Segment schema + accumulator (backend, additive).** Populate
   `task['segments']` alongside the three channels. Derive the three channels
   FROM segments at finalization. Prove byte-identical `task['content']`/
   `thinking`/`toolRounds` outputs on a corpus of real multi-round turns
   (golden test). Nothing else changes. *Ships dark.*
2. **Persist `segments` to DB** (new JSON column, schema bump — §10-gated) +
   round-trip test. Legacy columns still written (derived).
3. **Headless narrator fix (the reported pain).** New streaming compat path
   emits per-segment: filter `deliverable:false` OR mark them as a distinct
   channel; stop forwarding raw narration. Sync path reads segments. Retire
   the `DELTA_RESET` dependency on the compat surface. *This is where the
   user-visible win lands — and it lands early.*
4. **Continue/compaction/rebuild read segments** — migrate
   `_reconstruct_tool_call_messages` + `inject_tool_history` + compaction to
   the segment list; delete the derived-`toolRounds` shim once green.
5. **Frontend timeline cutover** (§4) — LAST, and coordinated with the
   sibling responsive/CSS epic (`pt_9f0ef6b458f041dc`) to avoid `styles.css`/
   `streaming_ui.js` collisions. Behind a flag so the three-zone renderer
   stays as fallback until the timeline renderer is proven at 1000+ tools.
6. **Retire the derived channels** only after every reader is migrated.

Each step is independently shippable and independently revertible. The
**measure-first proof** for step 1 (golden byte-identity) is the gate that
lets us trust the whole chain.

---

## 6. Testing posture (neuter discipline)
- **Step 1 golden:** real multi-round transcripts → assert derived
  `content`/`thinking`/`toolRounds` byte-identical to the current pipeline.
  NC-1: corrupt the derivation → golden diverges → FAIL.
- **Narrator fix (step 3):** a 3-round turn with narration → headless
  streaming output contains ZERO `deliverable:false` prose. NC-1: forward raw
  deltas again → narration leaks → FAIL. NC-2: mark the answer
  `deliverable:false` by mistake → answer disappears → FAIL (guards the flag
  direction).
- **Continue (step 4):** extraction-and-eval of `_reconstruct_tool_call_messages`
  over segments vs the 21-case `test_continue_lossless.py` corpus — must stay
  green (thinking+signature replay, Gemini thought_signature, batch-by-round).
- **Compaction:** `task['segments']` result-text truncation preserves identity
  fields; the existing compaction-invariants suite stays green.
- **Frontend (step 5):** jsdom extraction test drives the real timeline
  renderer over a from-DB message; per-segment freeze; 1000-tool perf smoke.

---

## 7. Decisions — RATIFIED by owner

All four ratified (this turn). Recorded here so the doc stays the source of truth.

1. **ABSORB parked epic `pt_2f7b65aab89a49ef`.** A typed ordered segment log
   IS the authoritative render source that epic asks for — merge, do not run
   two competing mechanisms. This epic supersedes it.
2. **Ship steps 1-3 first** (segment model + persist + headless narrator fix).
   The frontend per-tool timeline (step 5) becomes a **separate follow-on
   epic**, sequenced after the responsive/CSS epic (`pt_9f0ef6b458f041dc`)
   lands — the narrator leak is the real pain; fix it at root first and keep
   frontend risk out of the critical path.
3. **Keep the derived channels as a compatibility view until every reader is
   migrated (§5 step 6), then delete.** No permanent dual-write; hard-cut is
   reckless given the blast radius.
4. **Schema bump APPROVED** — new `segments` JSON column on `task_results` +
   the conversation message dict, §10-gated (mirror `_schema_pg.py` +
   `_schema_sqlite.py`, `audit_log('config_change', approved_by='user')`).
   *(Lands in step 2 — NOT yet applied; step 1 ships dark in-memory only.)*

### 7.1 Step 1 status — DONE (ships dark, 2026-07-07)
- `lib/tasks_pkg/segments.py` — pure `assemble_segments()` (SoT) +
  `derive_content` / `derive_thinking` / `derive_tool_rounds` projections.
- **Append seam:** `persist_task_result` (`manager.py`, right after the
  existing `_merge_tool_rounds` call) — the single terminal chokepoint every
  path funnels through. No token-level hook: the interleaving is already fully
  captured by ordered `_merge_tool_rounds` + terminal `content`/`thinking`.
- **`deliverable` rule:** position-based — `assistantContent` of a tool-round
  batch → `False`; terminal `task['content']` → `True`. Independent of
  `_discard_pretool_prose` (pinned by `test_deliverable_rule_is_position_based`).
- **Gate (hardened + observed green):** `tests/test_segment_model.py` — **40 passed, 0 skipped**.
  - Golden byte-identity of all three derived channels over **9** transcript
    shapes: single/multi-round/thinking-terminal/no-tools/continue-checkpoint
    PLUS the four production round shapes the hand-authored fixtures missed —
    (1) prefetch `fetch_url` rounds (no `llmRound`/`toolCallId`/`toolArgs`/
    `toolContent`; `executor.py:532`), (2) image-gen rounds (`query`/`results`,
    no `toolContent`), (3) rejected/hallucinated rounds (`status='rejected'`,
    never executed), (4) non-content terminal (exhausted/budget/filter — empty
    `content` + error envelope + trailing tool call → must yield NO phantom
    deliverable).
  - 3 NC-1 neuters (drop the deliverable flag → answer vanishes; flag narration
    deliverable → leaks; drop a tool_use → merge diverges).
  - **Ground-truth test** (`TestGroundTruthRealRunTask`) — **OBSERVED GREEN**:
    drives the REAL `orchestrator.run_task` through a stubbed
    `stream_llm_response` (multi-round: web_search tool call → streamed
    deliverable answer) and asserts the three derivations byte-identical to the
    ACTUALLY-PRODUCED task dict — so the golden no longer trusts hand-authored
    shapes. It EARNED ITS KEEP on first real run: caught that the real pipeline
    appends a Sources footer to `task['content']` (`orchestrator.py:456`), which
    a hand fixture would never surface — my initial tail assertion wrongly
    hardcoded `content == streamed answer`; corrected to assert the answer is
    PRESENT + the single deliverable segment == full terminal content (the three
    byte-identity assertions were already correct and passed). Env: the box
    was on SQLAlchemy 1.4.54 while `requirements.txt:110` pins `>=2.0`
    (`_core_schema.py:218` needs `sa.Double`); corrected the drift by installing
    the project's own pin into the tofu conda env (did NOT restart/disturb the
    running server, which holds the old package in memory). This also un-skipped
    `test_core_schema_parity` (23).
  - **Robustness fix:** `assemble_segments` batch-key is now
    `llmRound if not None else ('__no_llmround__', idx)` — so `None`-llmRound
    prefetch/image rounds each get their own batch instead of collapsing into
    one phantom batch (harmless today, but a future prose-bearing shape could
    be silently swallowed). Covered by shape #1.
  - Continue+task_runtime regression: 59/59 green. ruff clean.

### 7.2 Step 2 status — DONE (segments persisted to DB, 2026-07-07)
- **Schema:** new nullable `segments TEXT` column on `task_results`, defined
  ONCE in `_core_schema.py` (Core Table → both dialects) + explicit ALTER on
  the populated-DB path (`_schema_sqlite.py` `ADD COLUMN`, `_schema_pg.py`
  `ADD COLUMN IF NOT EXISTS`). Version bumped to **36** in BOTH backends
  (sqlite 35→36, pg 33→36 — the two counters now align on the same logical
  level). Parity golden DDL extended in `test_core_schema_parity.py` (both
  `test_task_results_{pg,sqlite}_parity` green). §10 approval recorded:
  `audit_log('config_change', change='task_results.segments_column',
  approved_by='user')`.
- **Column type = TEXT-holding-JSON, NOT JSONB** — matches the sibling
  `tool_rounds`/`search_results`/`metadata` columns exactly (same
  `json.dumps(ensure_ascii=False)` write path, zero dialect divergence).
  `segments` is read wholesale, never queried, so JSONB buys nothing and would
  be an inconsistent one-off.
- **`_round` mirror decision: STRIPPED at write, rehydrated on read** (NOT
  double-persisted). `segments_to_json()` drops the `_round` mirror (it embeds
  the entire origin round dict, already persisted verbatim in the co-located
  `tool_rounds` column / `msg['toolRounds']` — double-persisting would double
  the largest payload AND create a second source of truth that can drift).
  `rehydrate_segments(thin, tool_rounds)` re-zips `_round` by position on read
  (assembly emits exactly one `tool_use` per merged round in order → position
  is exact) → `derive_tool_rounds` byte-identical again. This makes the strip
  **provably lossless given `tool_rounds` is co-persisted**.
- **Two write sites** (both dark — nothing reads `segments` yet): the thin form
  is written to `task_results.segments` via `_upsert_task_row(segments_json=)`
  AND onto the conversation message dict (`last_msg['segments']`, round-trips
  through the `conversations.messages` JSON column). Both best-effort — a
  serialize failure never breaks persistence.
- **Gate (DB round-trip, held to the ground-truth bar):**
  `tests/test_segment_model.py` — **57 passed, 0 skipped** (was 40).
  - `TestThinRehydrateRoundTrip` (pure, no DB): thin form carries no `_round`;
    JSON-encode→decode→rehydrate → `derive_tool_rounds`/`content`/`thinking`
    byte-identical over all 9 shapes; CJK/unicode + nested `result` dict
    survive; empty-content terminal yields no phantom deliverable after the
    round-trip.
  - `TestSegmentsDBRoundTrip::test_persisted_segments_reread_and_rehydrate`
    — **OBSERVED GREEN**: drives real `run_task`, then RE-READS from BOTH
    `task_results.segments` (column) AND the conversation `messages` dict,
    JSON-decodes, rehydrates against the co-persisted `toolRounds`, and asserts
    byte-identical derivations — proving serialization survives the DB boundary
    (a pure in-memory assert would miss a serialization bug).
  - Regression on SQLAlchemy 2.0: continue_lossless + task_runtime +
    abort_dangling + chat_manager_migration = **84/84**; core_schema_parity
    **52/52**. ruff clean.

---

### 7.3 Step 3 status — DONE (headless narrator fix — the reported pain, 2026-07-07)
- **Design caveat resolved (the mid-stream classification problem).** A content
  `delta` is UNCLASSIFIABLE mid-stream — narration-vs-deliverable is only known
  at round close (`delta_reset` = narration; `done` = answer) — and a wire
  client CANNOT retract bytes already sent. So token-by-token live streaming of
  the deliverable is mutually exclusive with a zero-leak guarantee. **Chosen:
  zero-leak correctness.** Content deltas are NOT forwarded to the answer
  channel; the narration-free deliverable (`deliverable_text(task)` =
  `derive_content(segments)`, fallback `task['content']`) is emitted at `done`.
  This is NOT a single end-dump: **thinking deltas stream live** (`reasoning_content`
  / `thinking_delta`) and tool/phase events stream live (tofu-native envelope),
  so the turn has real-time activity throughout — only the final answer text is
  emitted at close. The compat surface's dependence on the un-portable
  `DELTA_RESET` is thereby RETIRED (no raw content forwarded → no reset needed).
- **`deliverable_text(task)` = the single source of truth** for "what text is
  the answer" across ALL four paths: OpenAI sync (`_assistant_message`) +
  streaming (`stream_openai_chunks`), Anthropic sync (`_content_blocks_from_task`)
  + streaming (`stream_anthropic_chunks`). Both late-connect terminal-fallback
  branches also emit it (a client connecting after completion still gets the
  answer).
- **Gate (`tests/test_compat_narrator_fix.py`, 6/6 — OBSERVED GREEN):** drives
  the REAL multi-round `run_task` (narration → web_search → answer) through the
  ACTUAL compat generators. OpenAI+Anthropic × streaming+sync all assert the
  wire contains the deliverable answer and ZERO narration ("Let me search for
  that." absent). TRIPLE-NEUTER: NC-1 (the old forward-raw-deltas path leaks the
  narration — proves suppression is the fix); NC-2 (mis-mark the answer
  `deliverable:false` + clear the fallback → answer disappears — proves the flag
  drives emitted content). Skips-with-reason on a DB-less env.
- **Contract-shift tests updated:** `test_compat_openai.py::test_streaming_yields_done`
  + `test_compat_anthropic.py::test_streaming_emits_named_events` now assert the
  deliverable is emitted at `done` (content deltas no longer streamed raw).
- **Regression:** compat_openai + compat_anthropic + narrator_fix + segment_model
  = 84/84; event_registry + event_emit = 17/17 (no event-type change). ruff clean.

---

### 7.4 Step 4 status — DONE (readers migrated onto segments, 2026-07-07)
- **Segment-native reconstructors (in `segments.py`):** `_rounds_view_from_segments`
  rebuilds the per-round view FROM the segment structure (tool_use id/name/input/
  result + batch prose from the `deliverable:false` text seg + thinking/signature
  from the thinking seg; Gemini `extraContent` from the rehydrated `_round`).
  `reconstruct_tool_messages_from_segments` delegates to the vetted
  `_reconstruct_tool_call_messages` → byte-identical wire messages.
  `tool_history_from_segments` builds the `cfg['toolHistory']` shape so a Continue
  rebuild can be driven from persisted segments.
- **Live reader migrated:** `conv_message_builder._build_assistant_messages` now
  prefers the segment path when the DB row carries `segments` (rehydrates against
  `toolRounds` first so `extraContent` is recovered), falling back to the
  `toolRounds` reconstructor for legacy (pre-v36) rows or on any failure.
- **Continue merge / no-double-count (inv #6):** there is NO separate segment
  merge — `assemble_segments(task, merged=_merge_tool_rounds(task))` builds from
  the ALREADY-merged `_checkpointToolRounds + toolRounds` list, so segments
  inherit the checkpoint+current ordering and count by construction; they cannot
  double-count unless `_merge_tool_rounds` does. Pinned by
  `test_continue_no_double_count_invariant_6` (merged=2 rounds → exactly 2
  assistant(tool_calls), correct order).
- **Gate (all OBSERVED GREEN):** `test_continue_lossless.py` — **40** (21 original
  corpus STILL GREEN + 19 new): `TestSegmentReconstructionParity` (segment recon ==
  toolRounds recon over single/multi-batch/Gemini shapes; thin-without-rehydrate
  drops extraContent — proves rehydrate load-bearing); `TestSegmentContinueGroundTruth`
  (checkpoint+current rebuild byte-identical; inv #6 no-double-count; inv #4
  thinking+signature carried; **NC** strip signature → reconstructions DIVERGE →
  proves signature carry load-bearing); `TestToolHistoryFromSegmentsParity`
  (segment-derived toolHistory → inject_tool_history == frontend-supplied path).
  `test_segment_model.py::TestCompactionSegmentIdentity` (inv #2: truncated result
  keeps id/name/input+status, content shrinks). Compaction-invariants **72/72**
  stay green (compaction rewrites `toolRounds`; segments re-assemble from the
  truncated rounds at persist → inv #2 by construction, no compaction edit needed).
  Full set: segment_model + continue + compaction + narrator + chat_manager = 201/201.
- **Step-6 shim removal — deliberately NOT done yet (honest scope).** The
  `toolRounds` reconstruction is the LEGACY FALLBACK for pre-v36 rows (no
  `segments`). Deleting it now would break every conversation persisted before
  the schema bump. It is retired in step 6 proper, after all readers migrate AND
  old rows age out / are backfilled — not as a step-4 slice.
- **`inject_tool_history` live cutover — deferred to the frontend step (honest
  scope).** Its live input is `cfg['toolHistory']` sent BY THE FRONTEND, not
  `msg['segments']`; cutting its live source to segments changes the Continue
  request contract (a frontend change) and belongs with step 5. Step 4 PROVES
  segments can drive byte-identical `inject_tool_history` output
  (`tool_history_from_segments` parity test); the live wire cutover lands with
  the frontend epic.

---

### 7.5 Step 5a status — STATIC/DB interleaved render DONE (flag-gated, 2026-07-07, epic pt_8b406df8fbe24ae5)
- **The visible payoff.** `renderSegmentTimelineHTML(segments, msg, idx)` (ui/tool_rounds.js) renders a finished turn's tools with each tool's PRECEDING thinking+narration ADJACENT to it, from `task['segments']` order — replacing the three global grouped blocks. Wired into `chat_render.js renderMessage` behind `_segTimelineEnabled()` (localStorage `tofu_segment_timeline`, DEFAULT OFF; legacy grouped render is the proven fallback). When it renders, the separate `msg.thinking` block is suppressed (per-batch thinking already shown).
- **Data contract (frontend mirror of backend rehydrate):** segments = ORDER + PROSE; tool BODIES looked up in the render-rich `msg.toolRounds` by toolCallId (positional fallback) — the thin tool_use segment lacks query/results/_swarm/interactive fields. Deliverable/terminal excluded (answer rendered after the panel). Returns "" → legacy fallback for no-segments / unmatchable rows. Reuses `.ptool-*`/`.thinking-block`/`.md-content` — NO new CSS (styles.css held by sibling mrahmwdu).
- **IndexedDB cache:** `_stripSegmentForCache` (idb-cache.js) KEEPS segments (renderer reads them) but strips tool_use `result` (duplicated multi-MB bulk; bodies come from toolRounds). PUT-wire `_trimMsgForPersist` stays a blanket drop (no client echo of the SoT — stale-clobber guard).
- **Gate:** `tests/test_frontend_segment_timeline.py` (4: interleave-adjacency thinking0→narr0→tools0→narr1→tool1 + deliverable-excluded + flag off/on + fallback + batch-collapse NC) + updated `test_frontend_segments_not_echoed.py` (5, bounded-cache + result-leak NC). Bundle `bundle-a252216a.js`, `renderSegmentTimelineHTML` dedup==1. Backend regression 108 + api-isolation 4. (A first build_bundle flaked on a concurrent sibling's mid-flight unterminated template literal in another bundled file — re-ran clean; every one of my files passes `node --check` individually.)
- **DEFERRED to step 5b (next increment):** the STREAMING hot-path rewrite (updateStreamingUI/_syncToolRoundsDOM → per-segment freeze: many frozen blocks + one trailing live tail; split the two coupled fingerprints per §4). Perf-critical half; static/DB path shipped first for low-risk visible payoff. `inject_tool_history` live wire cutover also lands in 5b. Step 6 (retire derived channels) still gated on pre-v36 rows.

---

### 7.5 Step 4.5 status — mid-prose resumption via capability-gated assistant PREFILL (2026-07-07)
- **What.** A turn interrupted mid-answer now RESUMES the same tokens (instead
  of regenerating) on any provider that tolerates a trailing assistant prefill.
  Covers BOTH shapes through ONE mechanism: case 2 (mid-prose after a completed
  tool batch) and case 3 (a no-tool turn interrupted mid-sentence — previously
  the most common real Continue, which fell all the way back to
  regenerate-from-scratch). Claude fails CLOSED (prefill removed / rejected).
- **Built on segments (not tail-diffing).** `resume_prefill_from_segments(segments,
  model, finish_reason=None)` in `segments.py` returns the terminal deliverable
  segment's text IFF `model_supports_assistant_prefill(model)` AND the turn is
  resumable (`RESUMABLE_FINISH_REASONS = {interrupted, server_offline,
  premature_close, length}`; `length` = the canonical max_tokens Continue).
  The terminal deliverable is the correct prefill for both cases because
  `task['content']` holds only the terminal round's prose (`_discard_pretool_prose`
  zeroes it each batch) — so it's the case-2 tail after the last tool batch and
  the case-3 whole answer; earlier-batch prose is replayed by
  `inject_tool_history`, never double-counted.
- **Crash-path tail capture.** `checkpoint_task_partial` now assembles + persists
  THIN segments (it previously lacked the `assemble_segments` block
  `persist_task_result` has), and `_sync_partial_to_conversation` mirrors them
  onto the message dict — so a mid-prose crash leaves a resumable segment tail.
  `resumable` is NOT stamped at checkpoint (status='running', no finishReason);
  `recover_stale_tasks_on_startup` stamps `finishReason='interrupted'` and the
  continue read passes it as the `finish_reason` override.
- **Pre-rollback extraction (correctness).** `chat_continue` extracts the prefill
  from `assistant_msg['segments']` BEFORE the in-place rollback (which zeroes
  content), ships a THIN `resumePrefill` on cfg + seeds `contentPrefix` with the
  FULL prior content (so display = full + continuation, no duplication). New
  `_continue_via_prefill_only` helper handles case 3 (no tool checkpoint but a
  resumable tail) instead of `{fallback:'regenerate'}`.
- **Orchestrator injection.** At the existing contentPrefix seam (after
  `inject_tool_history`): `messages.append({'role':'assistant','content':prefill})`
  gated on `model_supports_assistant_prefill`. Defence-in-depth:
  `_strip_trailing_assistant_for_claude` (build_body/dispatch_stream) neutralises
  any leak → degrades to regenerate, never HTTP 400.
- **Frontend.** `continueAssistant()` case-3 branch now routes through
  `/api/v1/chat/continue` (keeps `fallback:'regenerate'` for Claude/no-tail);
  bundle rebuilt (`bundle-18f91682.js`). Live display-merge is NOT headless-
  verifiable — flagged for in-browser check.
- **Gate — `tests/test_continue_prefill_resume.py` 10/10 OBSERVED GREEN.** Reader
  unit tests (capability + resumability + finish-reason override); ground-truth
  `run_task` case-3 (trailing prefill on the built wire body + no-duplication
  finalize); case-2 (tool batch replayed AND trailing prefill = tail only, batch
  prose not double-counted); Claude fail-closed (NO trailing assistant reaches
  the wire); **route-driven NC-1** (same `/api/v1/chat/continue` call with reader
  live → `resumeMode='prefill'` vs neutered → `fallback:'regenerate'` — a genuine
  contrast, and it CAUGHT a real decorator-misplacement 500 during development).
  Goldens `test_segment_model.py` (57) + `test_continue_lossless.py` (40) stay
  green (additive `resumable` field doesn't perturb byte-identity). ruff + node clean.
- **NOT committed (owner attribution). Server restart needed** (import-time backend
  + v-unchanged; frontend bundle reload).

*Prepared as the design-first deliverable for board epic
`pt_cb8f98b0cb9b47fb`. Blast radius verified by direct code inventory
(backend + frontend), not asserted. Steps 1-4 DONE (segments now DRIVE the
server-side DB-history rebuild; narrator leak fixed at root); step 5a DONE
(the VISIBLE interleaved render on the static/DB path, flag-gated). Remaining:
step 5b (streaming hot-path per-segment freeze) + step 6 (retire derived
channels once legacy rows age out).*

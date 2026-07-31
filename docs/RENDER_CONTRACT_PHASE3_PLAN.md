# RENDER_CONTRACT Phase 3 — one pure reducer for the four apply paths

> Status: **PLAN + tests-first golden/guard skeleton only. NO production code
> touched.** `events.py` / `sse_pipeline.js` / the `sse_handlers_*` move only on
> owner sign-off. This closes the two objective items Phase 4 did not:
> "tool-round jitter / cold-reopen twinning" and "bubble live↔warm↔cold↔poll
> projection inconsistency".
>
> ⚠️ DEPLOY NOTE: Phase 4 commits `2bd6702` (W1+W6) and `ec6a286` (Batch 1a) are
> backend hot paths that **require a server restart of :15000** to take effect;
> real-traffic verification of Phase 4 is gated on that restart. Phase 3 is
> frontend + one backend contract file (`events.py`); the JS half needs a bundle
> rebuild (restart) + hard refresh, per lib/js_bundler.py.

## 0. The core invariant (Invariant 4/5 generalized)

Every "settled fact" is produced ONCE and projected verbatim; the client folds,
never re-assembles. Today **five** independent assemblers hand-mutate the SAME
in-memory shape (`assistantMsg.content` / `.thinking` / `.toolRounds[]`) with
**different write disciplines**, so the same logical turn projects differently
depending on which path delivered it → tool-round jitter + cold-reopen twinning.

Convergence target already exists: the `done`/`committedMessage` projection
(`sse_pipeline.js:1810-1813`) is authoritative verbatim. Phase 3 makes all four
live/warm/cold/poll paths fold through ONE pure reducer whose fixed point equals
that verbatim shape.

## 1. The four paths as they are today (all INLINE, no shared reducer)

| Path | Entry (file:line) | Assembler | content/thinking | toolRounds |
|------|-------------------|-----------|------------------|-----------|
| **LIVE** | `dispatchSSEEvent` sse_pipeline.js:589 (via `_trySSE`:2040) | inline per-event | `+=` append :1012/1017 | push sse_handlers_tool.js:26-48; locate :106/283 |
| **WARM** | SAME dispatcher, `Last-Event-ID` seeded :2057-2067 | inline (byte-identical to LIVE) | identical | identical |
| **COLD** | `state` block sse_pipeline.js:668-930 | 2nd assembler (verbatim `=`) | `=` :892-893 (+endpoint :712/766/840/885) | `_snapshotLongerRounds(cur, existing.concat(ev.toolRounds))` :903-907 |
| **POLL** | `_pollFallback` sse_poll_fallback.js:20 | 3rd assembler (keep-longer `=`) | keep-longer `=` ~226/242 | `existingRounds.concat(data.toolRounds)` ~305 |
| _(DONE)_ | `done` sse_pipeline.js:1688 | authoritative verbatim | `= _cm.content` :1810 | `= _cm.toolRounds` :1813 ← **target shape** |
| _(VU)_ | `_handleAutopilotVuEvent` streaming_render.js:428-491 | 5th assembler | — | push/find by `inner.roundNum` |

## 2. The `round` key drift (L7) — four index conventions, no normalization point

On the wire (`lib/agent_core/events.py`), events split cleanly:
- **`roundNum`**: TOOL_START (L246), TOOL_PROGRESS (L255), TOOL_RESULT (L259), TOOL_DONE (L268), CONTEXT_COMPACTED (L272), timer-poll (L502).
- **`round`**: PHASE (L200), DELTA_RESET (L242), ROUND_USAGE (L276), ROUND_COMMITTED (L280), MESSAGES_SNAPSHOT (L283), peer_inbox_inject (L474), user_steer_inject (L493).

The client dutifully reads each event's own field, but **re-derives the index
locally in every handler** — and a tool round is located/created by FOUR
different names: `roundNum` (primary), `llmRound` (delta_reset batch grouping,
sse_pipeline.js:1098 + streaming_ui.js:706), `round` (phase/inject events only —
never stored on the round object), synthetic `9000000+len` (inject rows,
sse_handlers_lifecycle.js:35/136/181). The copy-pasted locate idiom
`(ev.toolCallId ? find(toolCallId) : null) || find(roundNum===ev.roundNum)`
appears **8×** (sse_handlers_tool.js:96/106/279/285, sse_handlers_io.js:17/62/103/127/150,
sse_handlers_misc.js:420, streaming_render.js:445/454/462).

## 3. Defensive patches in sse_pipeline.js = the bug scars this heals

AMENDED (2026-07-17, after routing + scar assessment): the original list
conflated TWO axes. Only the **assembly-axis** patches are subsumable by a
projection reducer; the **targeting/buffer-axis** ones are NOT (they decide
*which slot* or *which render buffer*, not *how to build the projection*), and
force-deleting them reintroduces the exact bug they patch. Honest split:

ASSEMBLY-AXIS (subsumed / handled by the reducer):
- **`_snapshotLongerRounds` keep-longer belt** (:66-70, :903) — RETAINED but
  RECLASSIFIED: it is the keep-longer MERGE that FEEDS the cold snapshot (a cold
  checkpoint may legitimately lag SHORTER than the live panel), proven correct
  by golden F3. Not a bug scar the reducer removes — a routing-layer merge the
  reducer projects AFTER. Kept deliberately.

TARGETING/BUFFER-AXIS (NOT subsumable — stay, by design):
- **endpoint "ghost worker placeholder"** (:323-350) — a PRE-STREAM targeting
  decision (runs in `connectToTask` before `_trySSE`): should a worker slot be
  pre-created for the incoming turn? Decided from persisted critic-verdict
  fields. A projection reducer (which runs on in-stream events, after the slot
  exists) cannot make this call. Round boundaries don't help (verdict §5). STAY.
- **"上一轮对话又重新流式吐出" stale-tail guard** (:289) — also PRE-STREAM
  targeting: is the persisted tail a prior completed turn? Already delegates to
  the shared pure predicate `assistantTailIsPriorTurn` (core/conversations.js).
  Orthogonal to projection. STAY.
- **"Defensive recovery — last msg not assistant"** (:352-366) — a race guard
  for the persisted-state load, pre-stream. STAY.
- **`_reentryBuf` "Defensive re-entry seed"** (:507-523) — seeds the RENDER
  BUFFER (`streamBufs`) for the `_twFlush` "等待中…" wait-branch, not the
  `{content,thinking,toolRounds}` projection. Different object. STAY.

Net: the reducer's job was to unify the FIVE projection ASSEMBLERS (done —
§1/§7), which it did without needing to delete any targeting/buffer scar. The
"reducer can DELETE these" framing was wrong for four of the five entries.

## 4. The reducer (target design)

`static/js/ui/stream_reducer.js` (NEW top-level module → MUST be added to
`_BUNDLE_FILES` in lib/js_bundler.py, i18n-first ordering rule N/A, place before
sse_pipeline.js which consumes it):

```
// Pure. No DOM, no globals, no I/O. Deterministic.
function reduceStreamState(state, event) -> newState   // {content, thinking, toolRounds}
function projectStreamEvents(events)     -> state       // fold from empty
function locateRound(state, event)       -> round|null  // the ONE index normalizer
```

- `locateRound` normalizes `roundNum` / `round` / `llmRound` / synthetic into one
  canonical `roundNum` — every handler calls it instead of re-deriving.
- Event-type-tagged actions encode the FOUR write disciplines as data, not
  inline code: `append` (delta), `verbatim` (committedMessage/state), `keep-longer`
  (only where a cold source can lag), `reset` (retry_reset / delta_reset).
- LIVE/WARM fold events; COLD applies the snapshot THROUGH the same reducer
  (snapshot = a `verbatim` action); POLL folds its JSON through the same reducer.
  All four reach the same fixed point = the `committedMessage` shape.
- The 5 assemblers (LIVE inline delta, tool handlers, COLD state block, POLL loop,
  VU render) become thin callers of `reduceStreamState`.

## 5. `events.py` — unify the round key + add explicit boundaries

- **Unify to `roundNum`**: rename the `round` field to `roundNum` on PHASE,
  DELTA_RESET, ROUND_USAGE, ROUND_COMMITTED, MESSAGES_SNAPSHOT, peer/steer inject
  specs (keep a back-compat read on the client during the transition; emit only
  `roundNum`). This is a WIRE-CONTRACT change → owner sign-off + the roundNum guard.
- **`ROUND_START` / `ROUND_END`** — VERDICT (2026-07-17, code-backed): **DROP as
  a required clause; NOT needed.** Two independent checks settle it:
  1. *They cannot retire the two remaining scars.* Both the endpoint
     ghost-placeholder (sse_pipeline.js:323-350) and the stale-tail guard
     (:289) run inside `connectToTask` BEFORE the stream opens (`_trySSE`
     at :528). They are **pre-stream, persisted-state TARGETING** decisions —
     "which `conv.messages` slot does the about-to-open stream write into?" —
     resolved purely from persisted fields (`_taskId`, `finishReason`,
     `_epApproved`, `_epNextPhase`). `round_start`/`round_end` are IN-stream
     events that do not exist until after the slot is already chosen, so they
     have no bearing on either decision. (The plan §3 claim that they'd remove
     "when did a round begin/end?" guessing conflated the in-stream
     delta_reset-grouping with the pre-stream targeting — the ghost scar is the
     latter and is on a different axis, as classified in the 2026-07-17 scar
     assessment.)
  2. *The reducer does not need them for its fixed point.* The reducer already
     infers round boundaries from `tool_start` (opens a round) + `delta_reset`
     (closes a round's prose), and the golden F1/F2/F3 prove all four paths
     reach a BYTE-IDENTICAL fixed point WITHOUT any explicit boundary event.
     Adding round_start/end would be a wire-contract change that buys zero
     projection-correctness and deletes zero scar.
  → Therefore the epic's "add round_start/round_end" clause is **formally
  dropped as unnecessary**. The two scars stay (they are correct pre-stream
  targeting logic, not projection divergence); §3's "reducer can DELETE them"
  line is amended below.

## 6. Tests-first (the acceptance anchors — committed RED now)

- **Golden parity** `test_frontend_reducer_parity.py`: build ONE logical turn's
  event sequence; project it via `projectStreamEvents` (LIVE fold) AND via the
  COLD snapshot the server would emit for that turn; assert the two projected
  `{content, thinking, toolRounds}` are **byte-identical** (JSON.stringify equal).
  RED now — `stream_reducer.js` does not exist (the four paths diverge); GREEN
  when the reducer lands and all paths fold through it.
- **roundNum unification guard** `test_events_round_key_unified.py`: static-scan
  `lib/agent_core/events.py`; assert every round-bearing EventSpec uses ONE key
  (`roundNum`) — no `'round'` field on tool/phase/usage/round_committed/snapshot/
  inject specs. RED now (the drift is real: PHASE/DELTA_RESET/ROUND_USAGE/… still
  say `round`); GREEN after §5 unifies. Includes a NEUTER: re-introducing a
  `round` field must re-flag.

## 7. Landing order (each step committable + tested)

1. This plan + tests-first skeleton (RED). [THIS DELIVERABLE]
2. Land `stream_reducer.js` (pure fn + `_BUNDLE_FILES`); route LIVE+WARM through it (lowest risk — identical semantics). Golden test still RED until COLD+POLL also route.
3. Route COLD (`state` block) + POLL through the reducer; retire `_snapshotLongerRounds` + the reentry/ghost defensive patches. Golden parity → GREEN.
4. `events.py`: unify `round`→`roundNum` + add `round_start/round_end`; roundNum guard → GREEN. WIRE CONTRACT — separate sign-off.
5. Route VU (5th assembler) through the reducer (folds it into the same contract).

## 8. Boundary

Phase 3 write-set (`events.py`, `sse_pipeline.js`, `sse_handlers_*`, new
`stream_reducer.js`, `js_bundler.py`) is disjoint from the live prefix-cache
work (mroozrve: `_parse.py`/`_run.py` wire content) — different plane, but both
touch "message serialization consistency". Confirm the `events.py` boundary with
the active peer before step 4.

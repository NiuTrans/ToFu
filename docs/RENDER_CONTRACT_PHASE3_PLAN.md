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

Each is a symptom of "no single projection". Enumerated so the reducer can
DELETE them, not preserve them:
- **"上一轮对话又重新流式吐出"** (:276, :288) — stale-prior-turn guard: a reused tail replays the previous turn's content because COLD/POLL assemble verbatim into whatever tail they find. Reducer keys writes by turn identity → guard unnecessary.
- **endpoint "ghost worker placeholder"** (:316-333) — a placeholder created on critic-STOP re-materializes the last worker's content via SSE-replay/poll writing `td.content` into the ghost. One reducer with explicit round boundaries never fabricates a phantom round.
- **"Defensive recovery — last msg not assistant"** (:352-366) — a race between `loadConversationMessages` Phase 2 and `startAssistantResponse`; the reducer's target is turn-id-addressed, not tail-positional.
- **`_snapshotLongerRounds` keep-longer belt** (:66-70, :903) — exists ONLY because COLD sources a shorter rounds array than LIVE already showed. A reducer whose COLD projection == LIVE projection makes shrink structurally impossible → belt retired.
- **`_reentryBuf` "Defensive re-entry seed"** (:507-523) — reseeds toolRounds because twUpdate path and reconnect path disagree on the buffer. One reducer, one buffer.

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
- **Add `ROUND_START` / `ROUND_END`** EventSpecs bracketing each LLM round so the
  client has explicit round boundaries instead of inferring them from the first
  `tool_start` / a `delta_reset`. Removes the "when did a round begin/end?"
  guessing that the ghost-placeholder + delta_reset-grouping patches encode.

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

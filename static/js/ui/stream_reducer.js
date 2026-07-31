/* ═══════════════════════════════════════════════════════════════════
   stream_reducer.js — RENDER_CONTRACT Phase 3 (2026-07)

   THE single pure projection for a turn's streamed state. Before this,
   FIVE assemblers (live delta, tool handlers, cold `state` block, poll
   loop, VU render) hand-mutated {content, thinking, toolRounds} with
   DIFFERENT write disciplines, so the same logical turn projected
   differently per path → tool-round jitter + cold-reopen twinning.

   This module is a PURE reducer: no DOM, no globals, no twUpdate /
   console / Api / fetch / setTimeout. It takes (state, event) and
   returns the next state. Every apply path folds through it and reaches
   the SAME fixed point — the `committedMessage` verbatim shape. Enforced
   byte-identically by tests/test_frontend_reducer_parity.py and kept
   side-effect-free by tests/test_frontend_reducer_purity.py.

   Concatenated by lib/js_bundler.py BEFORE ui/sse_pipeline.js — shares
   window scope, no imports/exports.
   ═══════════════════════════════════════════════════════════════════ */

/* ── locateRound: the ONE index normalizer (kills the roundNum/round/
 *    llmRound drift). A round is addressed by toolCallId when present
 *    (conversation-unique), else by its canonical roundNum. Callers pass
 *    the raw event; we read whichever index field it carries. ── */
function _evRoundNum(ev) {
  // The wire now emits ONE canonical round-index key: `roundNum` (Phase 3 §5
  // unified events.py — PHASE/DELTA_RESET/ROUND_USAGE/…/inject all carry
  // roundNum). This reads ONLY that canonical key: the retired `round` wire
  // alias AND a bare `llmRound` event field are intentionally NOT read here —
  // an event carrying only one of those must NOT silently locate a round (that
  // would re-hide the drift this unification removed). `llmRound` remains valid
  // ONLY as the batch-grouping key on ROUND OBJECTS (read directly off the
  // round in _stampDeltaReset, never off the event here). Guarded by
  // tests/test_events_round_key_unified.py + test_frontend_reducer_locate_canonical.py.
  if (ev == null) return null;
  if (ev.roundNum != null) return ev.roundNum;
  return null;
}

function locateRound(rounds, ev) {
  if (!Array.isArray(rounds) || !rounds.length) return null;
  if (ev && ev.toolCallId) {
    const byId = rounds.find(r => r && r.toolCallId === ev.toolCallId);
    if (byId) return byId;
  }
  const rn = _evRoundNum(ev);
  if (rn == null) return null;
  return rounds.find(r => r && r.roundNum === rn) || null;
}

/* ── Terminal round VERDICTS (pt_ac380e3d) ──
 *    A round in one of these states has been DECIDED: the tool was refused,
 *    interrupted, or failed. A later `tool_complete` may attach content and
 *    timings to it, but must NEVER promote it to 'done' — that would render a
 *    write the user REFUSED as applied, or a Stopped tool as finished. 'done'
 *    itself is listed so a settled round is not re-settled.
 *
 *    NOT terminal, deliberately: 'searching' / 'executing' are in-flight, and
 *    'pending_approval' / 'awaiting_human' / 'awaiting_stdin' are WAITING on
 *    input — for those a real completion legitimately means the wait resolved
 *    and the tool then ran, so they must still be allowed to reach 'done'. */
const _TERMINAL_ROUND_STATUS = {
  done: true, rejected: true, aborted: true,
  error: true, unanswerable: true,
};

/* Fresh empty projection state. */
function emptyStreamState() {
  return { content: '', thinking: '', toolRounds: [] };
}

/* ── _stampDeltaReset: the delta_reset prose-capture, extracted verbatim
 *    from sse_pipeline.js so live and cold agree. The just-ended LLM round
 *    issued tool calls, so its pre-call prose is inter-round narration:
 *    stamp it onto the FIRST tool round of this llmRound batch (where the
 *    backend's assemble_segments puts it), then clear the live accumulators.
 *    Append-guarded so a replayed delta_reset never double-stamps. Only
 *    clears once the prose is GUARANTEED preserved on a round (else keep it
 *    — the "frozen at a half word" freeze guard). ── */
function _stampDeltaReset(state, ev) {
  const trs = Array.isArray(state.toolRounds) ? state.toolRounds : null;
  let stamped = false;
  if (trs && trs.length) {
    // delta_reset now carries the canonical `roundNum` (== the llmRound loop
    // index); the retired `round` wire alias is no longer read.
    let lr = (ev && ev.roundNum != null) ? ev.roundNum : null;
    if (lr == null) lr = trs[trs.length - 1].llmRound;
    const batch = (lr != null) ? trs.filter(r => r.llmRound === lr)
                               : [trs[trs.length - 1]];
    const first = batch[0];
    if (first) {
      if (state.content) {
        if (!first.assistantContent) first.assistantContent = state.content;
        else if (first.assistantContent.indexOf(state.content) < 0) first.assistantContent += state.content;
      }
      if (state.thinking) {
        if (!first.thinking) first.thinking = state.thinking;
        else if (first.thinking.indexOf(state.thinking) < 0) first.thinking += state.thinking;
      }
      stamped = true;
    }
  }
  if (stamped) { state.content = ''; state.thinking = ''; }
  return state;
}

/* ── reduceStreamState(state, event) → newState (PURE) ──
 *    The four write disciplines encoded as event-type actions, NOT inline
 *    mutations scattered across handlers. Mutates & returns `state` (the
 *    caller owns a per-turn state object; folding is in-place for perf but
 *    the function has no external side effects). ── */
function reduceStreamState(state, ev) {
  if (!state) state = emptyStreamState();
  if (!ev || !ev.type) return state;
  if (!Array.isArray(state.toolRounds)) state.toolRounds = [];

  switch (ev.type) {
    case 'delta': {
      if (ev.thinking) state.thinking = (state.thinking || '') + ev.thinking;
      if (ev.content)  state.content  = (state.content  || '') + ev.content;
      return state;
    }
    case 'retry_reset': {
      // Whole-turn re-run: drop this attempt's prose + rounds; keep any
      // pre-turn Continue checkpoint rounds.
      state.content = '';
      state.thinking = '';
      state.toolRounds = Array.isArray(state._continueToolRounds)
        ? state._continueToolRounds.slice() : [];
      return state;
    }
    case 'round_start': {
      // Explicit round boundary (RENDER_CONTRACT Phase 3). Record the open
      // round's canonical index so subsequent deltas/tool rounds attach to a
      // REAL boundary instead of the client inferring it from the first
      // tool_start (a prose-only round has no tool_start at all). Projection
      // scaffolding only — _currentRound is dropped by the finalizer, so it
      // never changes the committed {content,thinking,toolRounds} shape.
      if (ev.roundNum != null) state._currentRound = ev.roundNum;
      return state;
    }
    case 'round_end': {
      // Close the open round. The boundary is explicit now, so batch grouping
      // and end-of-round no longer have to be inferred from the next
      // round_start / a tool_start / a `done`.
      state._currentRound = null;
      return state;
    }
    case 'delta_reset':
      return _stampDeltaReset(state, ev);
    case 'tool_start': {
      const r = {
        roundNum: ev.roundNum,
        query: ev.query,
        results: null,
        status: (ev.status === 'rejected') ? 'rejected' : 'searching',
        toolName: ev.toolName || null,
        toolCallId: ev.toolCallId || null,
        toolArgs: ev.toolArgs || null,
        // Batch grouping now keys off the EXPLICIT round boundary: prefer the
        // event's own llmRound, else the open round from round_start. This is
        // what lets the reducer stop inferring batches from the first
        // tool_start when an explicit boundary is present.
        llmRound: (ev.llmRound != null) ? ev.llmRound
                : (state._currentRound != null) ? state._currentRound : null,
        _swarm: ev._swarm || false,
      };
      /* ★ Timing (pt_67ffc2b7). The three BACKEND clocks + the client's
       *   `receivedAt` are copied verbatim onto the round so the tool row can
       *   attribute latency to execution / transport / render instead of
       *   showing one undifferentiated spinner. The reducer NEVER mints a
       *   clock itself: it is a pure function, and a `Date.now()` here would
       *   make the live fold diverge from the cold projection of the same
       *   settled turn (the byte-identical parity contract). `receivedAt` is
       *   stamped at STREAM INGRESS and arrives on the event. */
      if (ev.tStart != null) r.tStart = ev.tStart;
      if (ev.emittedAt != null) r.emittedAt = ev.emittedAt;
      if (ev.receivedAt != null) r.receivedAt = ev.receivedAt;
      if (ev.assistantContent) r.assistantContent = ev.assistantContent;
      if (ev._repaired) r._repaired = ev._repaired;
      if (ev._rejected) r._rejected = ev._rejected;
      state.toolRounds.push(r);
      return state;
    }
    case 'tool_result': {
      const r = locateRound(state.toolRounds, ev);
      if (r) {
        r.results = ev.results;
        if (ev.status === 'rejected' || ev._rejected) {
          r.status = 'rejected';
          if (ev._rejected) r._rejected = ev._rejected;
        } else {
          r.status = 'done';
        }
        // A result settles the round: any pending approval / human-guidance
        // gate is resolved, so clear those markers (the round no longer awaits
        // input). Matches the live _handleToolResult discipline; null-safe on a
        // cold snapshot (fields were absent → stay absent under canonicalize).
        r.approvalId = null;
        r.approvalMeta = null;
        r.guidanceId = null;
        if (ev.searchDiag) r.searchDiag = ev.searchDiag;
        if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
        if (ev.vertical) r.vertical = ev.vertical;
        if (ev.verticals) r.verticals = ev.verticals;
        if (ev._repaired) { r._repaired = ev._repaired; if (ev.query) r.query = ev.query; }
        /* Terminal clocks: execution = tEnd - tStart, transport =
         * receivedAt - emittedAt. Kept on the round so a settled row stays
         * self-describing after a reload. */
        if (ev.tStart != null) r.tStart = ev.tStart;
        if (ev.tEnd != null) r.tEnd = ev.tEnd;
        if (ev.emittedAt != null) r.emittedAt = ev.emittedAt;
        if (ev.receivedAt != null) r.receivedAt = ev.receivedAt;
      }
      return state;
    }
    case 'tool_done':
    case 'tool_complete': {
      const r = locateRound(state.toolRounds, ev);
      if (r) {
        r.toolContent = ev.toolContent != null ? ev.toolContent
                       : (ev.content != null ? ev.content : r.toolContent || null);
        if (ev.toolTokens != null) r.toolTokens = ev.toolTokens;
        if (ev.compactionLayer) {
          r.compactionLayer = ev.compactionLayer;
          r.compactedFromChars = ev.compactedFromChars;
          r.compactedToChars = ev.compactedToChars;
        }
        if (ev.tStart != null) r.tStart = ev.tStart;
        if (ev.tEnd != null) r.tEnd = ev.tEnd;
        if (ev.emittedAt != null) r.emittedAt = ev.emittedAt;
        if (ev.receivedAt != null) r.receivedAt = ev.receivedAt;
        /* ★ A terminal VERDICT must survive a later completion frame
         * (pt_ac380e3d). This used to read `if (r.status !== 'rejected')`,
         * protecting exactly ONE verdict — while `aborted`, `error` and
         * `unanswerable` are all real round statuses the backend assigns. Any
         * of those followed by a tool_complete was silently promoted to
         * 'done', so a write the user REFUSED, or a tool a Stop interrupted,
         * rendered as successfully completed. That is strictly worse than the
         * latency the prompt settle removes, so the rule is now: the frame's
         * own explicit status wins; otherwise a round already holding a
         * terminal verdict keeps it; only a genuinely in-flight round settles
         * to 'done'. */
        if (ev.status) r.status = ev.status;
        else if (!_TERMINAL_ROUND_STATUS[r.status]) r.status = 'done';
      }
      return state;
    }
    default:
      // Unknown / non-projection events (phase, usage, snapshot label, …)
      // do not mutate the {content,thinking,toolRounds} projection.
      return state;
  }
}

/* ── projectStreamEvents(events, seed?) → state ──
 *    LIVE/WARM fold: replay an ordered event list from empty (or a seed
 *    holding _continueToolRounds for Continue). ── */
function projectStreamEvents(events, seed) {
  let state = seed ? Object.assign(emptyStreamState(), seed) : emptyStreamState();
  if (!Array.isArray(state.toolRounds)) state.toolRounds = [];
  if (!Array.isArray(events)) return state;
  for (const ev of events) state = reduceStreamState(state, ev);
  return _finalizeProjection(state);
}

/* ── projectColdSnapshot(snapshot) → state ──
 *    COLD/POLL: a settled server snapshot is a `verbatim` action — the
 *    authoritative {content,thinking,toolRounds} shape. Passed through the
 *    SAME finalizer as the fold so both paths emit an identical PRODUCTION
 *    shape. LOSS-LESS: every round field the server carries (approvalId,
 *    searchDiag, engineBreakdown, vertical, _mcpLoginHint, …) is preserved —
 *    the finalizer only drops the internal _continueToolRounds scratch. ── */
function projectColdSnapshot(snap) {
  const s = emptyStreamState();
  if (snap) {
    s.content = snap.content || '';
    s.thinking = snap.thinking || '';
    s.toolRounds = Array.isArray(snap.toolRounds) ? snap.toolRounds.map(r => Object.assign({}, r)) : [];
  }
  return _finalizeProjection(s);
}

/* ── _finalizeProjection: the PRODUCTION output shape. LOSS-LESS by design —
 *    it must NOT drop real round fields (a cold snapshot carries approvalId /
 *    searchDiag / engineBreakdown / vertical / path that the render needs).
 *    It only normalizes the top-level {content,thinking} to '' and shallow-
 *    copies rounds so the caller can't mutate reducer internals. Byte-level
 *    canonicalization for the golden PARITY test is a SEPARATE concern
 *    (canonicalizeProjectionForCompare) so production loses nothing. ── */
function _finalizeProjection(state) {
  const rounds = Array.isArray(state.toolRounds) ? state.toolRounds : [];
  // _currentRound is round_start scaffolding — NEVER part of the committed
  // {content,thinking,toolRounds} shape; the finalizer drops it.
  return {
    content: state.content || '',
    thinking: state.thinking || '',
    toolRounds: rounds.map(r => Object.assign({}, r)),
  };
}

/* ── canonicalizeProjectionForCompare(proj) → comparable projection ──
 *    TEST-ONLY equivalence normalizer: rebuilds each round with a FIXED key
 *    order and omits null/scaffolding-default keys, so a LIVE fold (which may
 *    leave results:null / _swarm:false scaffolding) and a COLD snapshot (which
 *    omits those) of the SAME settled turn serialize byte-identically. This is
 *    NOT applied in production — the parity test calls it on both sides before
 *    JSON.stringify. Kept in the module so the canonical key order lives in one
 *    place beside the reducer it describes. ── */
const _ROUND_KEY_ORDER = [
  'roundNum', 'llmRound', 'toolName', 'toolCallId', 'query', 'status',
  'results', 'toolContent', 'toolArgs', 'assistantContent', 'thinking',
  'toolTokens', 'compactionLayer', 'compactedFromChars', 'compactedToChars',
  'searchDiag', 'engineBreakdown', 'vertical', 'verticals', 'path',
  'approvalId', 'approvalMeta', 'guidanceId',
  '_swarm', '_repaired', '_rejected',
];

/* CLIENT-LOCAL telemetry: stamped by this browser at stream ingress, so it can
 * only ever exist on a LIVE fold — a cold snapshot replayed from the server has
 * no such value. Excluded from the equivalence compare (NOT from production,
 * where the render reads it) so the live-vs-cold parity contract still holds
 * with the timing instrumentation in place. The BACKEND clocks (tStart / tEnd /
 * emittedAt) are deliberately NOT excluded — they ride the server snapshot too,
 * so they must match on both sides. */
const _CLIENT_LOCAL_ROUND_KEYS = ['receivedAt'];

function _canonRound(r) {
  const out = {};
  const seen = {};
  for (const k of _CLIENT_LOCAL_ROUND_KEYS) seen[k] = true;
  for (const k of _ROUND_KEY_ORDER) {
    const v = r[k];
    seen[k] = true;
    if (v == null) continue;
    if (k === '_swarm' && v === false) continue;
    out[k] = v;
  }
  // Preserve any field not in the known order (deterministic: sorted) so the
  // canonicalizer never silently hides a new field from the compare.
  const extra = Object.keys(r).filter(k => !seen[k] && k !== '_continueToolRounds').sort();
  for (const k of extra) { if (r[k] != null) out[k] = r[k]; }
  return out;
}

function canonicalizeProjectionForCompare(proj) {
  const rounds = (proj && Array.isArray(proj.toolRounds)) ? proj.toolRounds : [];
  return {
    content: (proj && proj.content) || '',
    thinking: (proj && proj.thinking) || '',
    toolRounds: rounds.map(_canonRound),
  };
}

/* Node-eval / test harness hook (no-op in browser where these are globals). */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    reduceStreamState, projectStreamEvents, projectColdSnapshot,
    locateRound, emptyStreamState, canonicalizeProjectionForCompare,
  };
}

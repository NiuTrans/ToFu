/* ═══════════════════════════════════════════════════════════════════════════
   pt_turn_settlement C1 — turn-settlement verdict (canonical JS port)
   ═══════════════════════════════════════════════════════════════════════════

   Epic pt_a4484f3ad3134ea8 · design docs/TURN_SETTLEMENT.md.

   This is the canonical JS port of the BACKEND-authoritative verdict
   ``lib/conversations/turn_settlement.py::compute_turn_settlement`` — the
   single per-turn fact that THREE consumers read instead of re-inferring
   from the loosely-controlled ``finishReason`` string:

     * the interrupt bubble label   (static/js/ui/finish_info.js),
     * the Continue-button affordance (static/js/ui/chat_render.js),
     * the Continue resume-mode     (static/js/main/main_regen_continue.js).

   The backend is the SSOT. This port exists so the client can render the
   verdict on a cold reopen / streamed message WITHOUT a server round-trip,
   byte-identically to what the backend would compute (guarded by
   tests/test_frontend_turn_settlement_equivalence.py, which drives BOTH the
   Python verdict and this port over one corpus and asserts deep equality —
   the same ghost-tail / conv_state_reducer precedent).

   KEEP THIS FILE BEHAVIOUR-LOCKED with turn_settlement.py. Any change to one
   must be mirrored in the other or the equivalence test goes red. Pure
   functions; no DOM, no fetch, no saveConversations.

   Verdict shape:
     { outcome: 'completed'|'interrupted'|'truncated'|'failed',
       finishReason: <raw string|null>,
       cause: 'manual'|'killed'|'restart'|'offline'|'gateway'|'max_tokens'
              |'tool_cap'|'safety_cap'|'content_filter'|'error'|null,
       resume: { mode: 'prefill'|'checkpoint'|'regenerate'|'none',
                 lossless: bool, keptRounds: int, prefillChars: int,
                 reason: string } }
   ══════════════════════════════════════════════════════════════════════════ */

const TS_OUTCOME_COMPLETED = 'completed';
const TS_OUTCOME_INTERRUPTED = 'interrupted';
const TS_OUTCOME_TRUNCATED = 'truncated';
const TS_OUTCOME_FAILED = 'failed';

const TS_CAUSE_MANUAL = 'manual';
const TS_CAUSE_KILLED = 'killed';
const TS_CAUSE_RESTART = 'restart';
const TS_CAUSE_UNKNOWN = 'unknown';
const TS_CAUSE_OFFLINE = 'offline';
const TS_CAUSE_GATEWAY = 'gateway';
const TS_CAUSE_MAX_TOKENS = 'max_tokens';
const TS_CAUSE_TOOL_CAP = 'tool_cap';
const TS_CAUSE_SAFETY_CAP = 'safety_cap';
const TS_CAUSE_CONTENT_FILTER = 'content_filter';
const TS_CAUSE_ERROR = 'error';

const TS_MODE_PREFILL = 'prefill';
const TS_MODE_CHECKPOINT = 'checkpoint';
const TS_MODE_REGENERATE = 'regenerate';
const TS_MODE_NONE = 'none';

/* Clean finishes (the green ✓ set) — mirror _CLEAN_FINISH_REASONS. */
const _TS_CLEAN_FINISH_REASONS = new Set(['stop', 'end_turn', 'stop_sequence']);

/* Resumable finishes — mirror segments/_types.py RESUMABLE_FINISH_REASONS.
 * Includes 'aborted' (manual Stop): the partial answer is a valid prefill
 * prefix. Kept in lock-step with the Python frozenset by the equivalence
 * test (a drift flips verdicts → red). */
const _TS_RESUMABLE_FINISH_REASONS = new Set([
  'interrupted', 'server_offline', 'premature_close', 'length', 'aborted',
]);

/* model_supports_assistant_prefill = not is_claude — mirror
 * lib/model_info/_capabilities.py + _family.is_claude (claude/anthropic/
 * fable all speak the Messages API that rejects an assistant prefill). */
function _tsSupportsPrefill(model) {
  const m = String(model || '').toLowerCase();
  return !(m.includes('claude') || m.includes('anthropic') || m.includes('fable'));
}

/* reconcile.has_real_round — a round is real iff it carries a toolCallId
 * AND (non-empty toolContent OR status === 'done'). */
function _tsHasRealRound(rounds) {
  const arr = Array.isArray(rounds) ? rounds : [];
  return arr.some((r) => r && r.toolCallId &&
    ((((r.toolContent || '') + '').trim()) || r.status === 'done'));
}

/* scan_continue_checkpoint's kept-rounds determination (turn_builder.py) —
 * returns len(kept_rounds) (0 when there is no recoverable checkpoint, the
 * Python `scan is None` case). Faithful to the backend loop: rounds without
 * a toolCallId are skipped; the first non-'done' status (or unreconstructable
 * toolContent) BREAKS the scan; kept = all rounds up to the last completed. */
function _tsScanKeptRounds(rounds) {
  const all = Array.isArray(rounds) ? rounds : [];
  if (!all.length) return 0;
  if (!all.some((r) => r && r.toolCallId)) return 0;
  const hasLlmRound = all.some((r) => r && r.llmRound !== null && r.llmRound !== undefined);
  let batchKey = 0;
  let lastCompleteIdx = -1;
  for (let i = 0; i < all.length; i++) {
    const r = all[i];
    if (!r || !r.toolCallId) continue;
    if (r.status !== 'done') break;
    if (r.toolContent === null || r.toolContent === undefined) {
      const results = Array.isArray(r.results) ? r.results : [];
      let reconstructed = '';
      if (results.length) {
        const parts = [];
        for (const res of results) {
          if (!res || typeof res !== 'object') continue;
          parts.push(res.snippet || res.title || res.content || '');
        }
        reconstructed = parts.filter((p) => p).join('\n');
      }
      if (!reconstructed) break;
      r.toolContent = reconstructed || '[tool result not available]';
    }
    if (hasLlmRound) {
      batchKey = r.llmRound;
    } else {
      const prev = i > 0 ? all[i - 1] : null;
      const prevRn = prev && prev.roundNum !== undefined ? prev.roundNum : -999;
      const curRn = r.roundNum !== undefined ? r.roundNum : 0;
      if (prev && prev.toolCallId && curRn > prevRn + 1) batchKey += 1;
    }
    lastCompleteIdx = i;
  }
  return lastCompleteIdx + 1;
}

function _tsCauseFromInterruptedReason(interruptedReason) {
  if (interruptedReason === 'killed') return TS_CAUSE_KILLED;
  if (interruptedReason === 'manual') return TS_CAUSE_RESTART;
  return TS_CAUSE_UNKNOWN;
}

/* Map a raw finishReason (+ message context) to [outcome, cause]. A missing /
 * unrecognised reason keeps the recovery path open (interrupted / cause=null)
 * — mirrors the backend and today's "legacy turn with no finishReason still
 * shows Continue". */
function _tsClassifyOutcome(msg, finishReason) {
  const fr = finishReason;
  if (fr === null || fr === undefined) return [TS_OUTCOME_INTERRUPTED, null];
  if (_TS_CLEAN_FINISH_REASONS.has(fr)) return [TS_OUTCOME_COMPLETED, null];
  if (fr === 'length' || fr === 'max_tokens') return [TS_OUTCOME_TRUNCATED, TS_CAUSE_MAX_TOKENS];
  if (fr === 'tool_rounds_exhausted') return [TS_OUTCOME_TRUNCATED, TS_CAUSE_TOOL_CAP];
  if (fr === 'incomplete') return [TS_OUTCOME_TRUNCATED, TS_CAUSE_SAFETY_CAP];
  if (fr === 'content_filter') return [TS_OUTCOME_FAILED, TS_CAUSE_CONTENT_FILTER];
  if (fr === 'error' || fr === 'abnormal_stop') return [TS_OUTCOME_FAILED, TS_CAUSE_ERROR];
  if (fr === 'interrupted') return [TS_OUTCOME_INTERRUPTED, _tsCauseFromInterruptedReason(msg.interruptedReason)];
  if (fr === 'server_offline') return [TS_OUTCOME_INTERRUPTED, TS_CAUSE_OFFLINE];
  if (fr === 'premature_close') return [TS_OUTCOME_INTERRUPTED, TS_CAUSE_GATEWAY];
  if (fr === 'aborted') return [TS_OUTCOME_INTERRUPTED, TS_CAUSE_MANUAL];
  return [TS_OUTCOME_INTERRUPTED, null];
}

function _tsIsEmptyTurn(msg) {
  if (((msg.content || '') + '').trim()) return false;
  if (((msg.thinking || '') + '').trim()) return false;
  return !_tsHasRealRound(msg.toolRounds);
}

function _tsResume(mode, lossless, reason, keptRounds, prefillChars) {
  return {
    mode: mode,
    lossless: !!lossless,
    keptRounds: keptRounds || 0,
    prefillChars: prefillChars || 0,
    reason: reason,
  };
}

/* Decide HOW the turn can be resumed — once, here, not per consumer.
 * P5 precedence (owner-approved flip): prefill BEFORE checkpoint for a capable
 * model with a resumable tail (mirrors the backend verdict + the already-
 * lossless case-2 continue route); checkpoint is the fallback for a tools turn
 * the provider can't prefill. Fail-closed (any uncertainty → regenerate). */
function _tsComputeResume(msg, outcome, finishReason, model) {
  if (outcome === TS_OUTCOME_COMPLETED) return _tsResume(TS_MODE_NONE, false, 'turn_completed');
  if (_tsIsEmptyTurn(msg)) return _tsResume(TS_MODE_REGENERATE, false, 'empty_turn');
  const keptRounds = _tsScanKeptRounds(msg.toolRounds);
  const content = (msg.content || '') + '';
  const resumable = _TS_RESUMABLE_FINISH_REASONS.has(finishReason || '');
  const prefillOk = resumable && content.trim() && model && _tsSupportsPrefill(model);
  if (prefillOk) return _tsResume(TS_MODE_PREFILL, true, 'prefill_continue', keptRounds, content.length);
  if (keptRounds > 0) return _tsResume(TS_MODE_CHECKPOINT, false, 'tool_checkpoint', keptRounds, 0);
  return _tsResume(TS_MODE_REGENERATE, false, 'no_checkpoint_no_prefill');
}

/* The single authoritative settlement verdict for an assistant turn.
 * ``msg`` is the assistant message dict; ``model`` is the model id (may be
 * null → prefill declined, fail-closed). ``segments`` is accepted for shape
 * parity with the Python signature (reserved for the P5 prefill-over-
 * checkpoint refinement); the current verdict derives the deliverable from
 * msg.content so it is recomputable without segments.
 * Returns null when msg is not an assistant turn. */
function computeTurnSettlement(msg, model, segments) {
  if (!msg || typeof msg !== 'object' || msg.role !== 'assistant') return null;
  const raw = ((msg.finishReason || '') + '').trim();
  const finishReason = raw ? raw : null;
  const cls = _tsClassifyOutcome(msg, finishReason);
  const outcome = cls[0];
  const cause = cls[1];
  const resume = _tsComputeResume(msg, outcome, finishReason, model);
  return {
    outcome: outcome,
    finishReason: finishReason,
    cause: cause,
    resume: resume,
  };
}

/* Decide the Continue-button affordance for a settlement verdict — the pure
 * fact the chat_render button gate consumes, so the gate stays a thin
 * renderer and this logic is Node-testable. Returns:
 *   { show:false }                                         — completed / no verdict
 *   { show:true, kind:'continue',   lossless:true,  labelKey, titleKey } — prefill
 *   { show:true, kind:'continue',   lossless:false, keptRounds, labelKey, titleKey } — checkpoint
 *   { show:true, kind:'regenerate', lossless:false, labelKey, titleKey } — regenerate
 * The 'regenerate' case is the honesty fix: when no honest resume exists the
 * button no longer masquerades as "Continue" (which silently fell back to a
 * full regeneration) — it is labelled "Regenerate". */
function continueButtonForSettlement(verdict) {
  if (!verdict || !verdict.resume) return { show: false };
  const mode = verdict.resume.mode;
  if (mode === TS_MODE_NONE) return { show: false };
  if (mode === TS_MODE_PREFILL) {
    return { show: true, kind: 'continue', lossless: true,
             keptRounds: verdict.resume.keptRounds || 0,
             labelKey: 'msgAction.continue', titleKey: 'msgAction.continueLosslessTitle' };
  }
  if (mode === TS_MODE_CHECKPOINT) {
    return { show: true, kind: 'continue', lossless: false,
             keptRounds: verdict.resume.keptRounds || 0,
             labelKey: 'msgAction.continue', titleKey: 'msgAction.continueFromRoundTitle' };
  }
  return { show: true, kind: 'regenerate', lossless: false,
           labelKey: 'msgAction.regen', titleKey: 'msgAction.regenerateTitle' };
}

/* Decide the interrupt-bubble finish-tag for a settlement verdict — the pure
 * fact finish_info.js renders, so the renderer stays thin and this logic is
 * Node-testable. Returns { kind } where kind drives the label/styling/i18n in
 * finish_info.js:
 *   ok | stopped | interruptedKilled | interruptedRestart | interruptedUnknown
 *   | serverOffline | gateway | incomplete | toolLimit | truncated | filtered
 *   | error | abnormal | fallback
 * 'fallback' = a finishReason the verdict deliberately does NOT classify
 * (tool_use / tool_calls / a future reason) — the renderer falls back to its
 * existing labels map so no current label regresses. The 3-way interrupted
 * family (killed/restart/unknown) reads the verdict's `cause` — faithful to
 * the bubble's existing killed/restart/unknown labels (CAUSE_UNKNOWN keeps an
 * absent interruptedReason honest instead of over-committing it to restart). */
function finishLabelForSettlement(verdict, finishReason) {
  if (!verdict) return { kind: 'fallback' };
  const fr = finishReason || verdict.finishReason || '';
  const oc = verdict.outcome;
  const cause = verdict.cause;
  if (oc === TS_OUTCOME_COMPLETED) return { kind: 'ok' };
  if (cause === TS_CAUSE_MANUAL) return { kind: 'stopped' };
  if (cause === TS_CAUSE_KILLED) return { kind: 'interruptedKilled' };
  if (cause === TS_CAUSE_RESTART) return { kind: 'interruptedRestart' };
  if (cause === TS_CAUSE_UNKNOWN) return { kind: 'interruptedUnknown' };
  if (cause === TS_CAUSE_OFFLINE) return { kind: 'serverOffline' };
  if (cause === TS_CAUSE_GATEWAY) return { kind: 'gateway' };
  if (cause === TS_CAUSE_SAFETY_CAP) return { kind: 'incomplete' };
  if (cause === TS_CAUSE_TOOL_CAP) return { kind: 'toolLimit' };
  if (cause === TS_CAUSE_MAX_TOKENS) return { kind: 'truncated' };
  if (cause === TS_CAUSE_CONTENT_FILTER) return { kind: 'filtered' };
  if (cause === TS_CAUSE_ERROR) return (fr === 'abnormal_stop') ? { kind: 'abnormal' } : { kind: 'error' };
  return { kind: 'fallback' };
}

/* ── Publish under both bare + window scopes so the Node equivalence harness's
 *   (0, eval)(src) and the browser's bundle both see them (conv_state_reducer
 *   precedent). */
if (typeof window !== 'undefined') {
  window.computeTurnSettlement = computeTurnSettlement;
  window.continueButtonForSettlement = continueButtonForSettlement;
  window.finishLabelForSettlement = finishLabelForSettlement;
  window._tsScanKeptRounds = _tsScanKeptRounds;
  window._tsHasRealRound = _tsHasRealRound;
}

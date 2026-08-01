/* ═══════════════════════════════════════════════════════════════════
   core/conv_reducers.js — pure conversation reducers.

   Extracted from core/conversations.js (pt_3879f00e sub-part 2, slice 1):
   the five stateless helpers historically at the TOP of that file are
   now their own leaf module. Zero runtime state, zero side effects at
   load time — every read of an external symbol (conversations,
   autoTranslate, t) is done at CALL time, so this file can safely
   load BEFORE the heavier conversations.js module.

   Symbols:
     convAutoTranslate(conv)            — SEND-time (frozen) auto-translate flag
     convAutoTranslateEffective(conv)   — LIVE (retro-open) auto-translate flag
     assistantTailIsPriorTurn(msg, tid) — SSE-connect prior-turn detector
     pollWriteWouldClobberSettledTail(msg, taskId, data)
                                        — poll-fold monotonic-writer guard
     convTitleById(cid)                 — id → human title lookup
     _mergeTerminalTurnFields(lm, sm)   — terminal turn-metadata field list
                                        — single source of truth (apiRounds
                                          upgrade-if-longer, rest fill-if-missing)

   This file is concatenated into the core bundle BEFORE
   core/conversations.js (guarded by _BUNDLE_FILES ordering +
   tests/test_frontend_conv_reducers_extracted.py).
   ═══════════════════════════════════════════════════════════════════ */

/* ═══════════════════════════════════════════════════════════════════
   Canonical auto-translate decision (frontend single source of truth).

   The per-conversation `autoTranslate` flag historically read with mixed
   `!== undefined ? : true/false` fallbacks across ~8 trigger sites, which —
   together with the backend's own divergent defaults — made auto-translate
   fire unpredictably. This helper expresses the ONE default (OPT-IN / OFF,
   matching the backend `lib.conv_config.resolve_auto_translate` and the
   `AUTO_TRANSLATE_DEFAULT = False` constant) so every frontend trigger path
   agrees. Pass the conversation object; an explicit per-conv value always
   wins, otherwise the global toolbar flag, otherwise OFF.
   ═══════════════════════════════════════════════════════════════════ */
function convAutoTranslate(conv) {
  if (conv && conv.autoTranslate !== undefined) return !!conv.autoTranslate;
  if (typeof autoTranslate !== 'undefined' && autoTranslate !== undefined) return !!autoTranslate;
  return false;
}
if (typeof window !== 'undefined') window.convAutoTranslate = convAutoTranslate;

/* ═══════════════════════════════════════════════════════════════════
   Canonical "does this assistant tail belong to a PRIOR, already-settled
   turn?" reducer (frontend single source of truth).

   When a task's SSE stream connects (live connect in ui/sse_pipeline.js) or
   is reconnected on startup (main/main_init_tasks.js Case A), the tail
   assistant message might be the PREVIOUS, completed turn rather than an
   empty placeholder for the incoming turn. Streaming into it replays the old
   turn's content into the new bubble ("上一轮对话又重新流式吐出"). The fix is
   to push a fresh placeholder — but ONLY when the tail is genuinely a prior
   turn.

   This is NOT lifecycle INFERENCE (the retired ghost-classifier anti-pattern):
   the decision reads only BACKEND-ISSUED FACTS already stamped on the message —
   `_taskId` (the task→msg bind set from the SSE `state` event / poll payload;
   the backend even keys segment recovery on it in reconcile.py) and
   `finishReason` (from the done/poll payload). It is a pure equality/presence
   reducer over those facts, which is exactly what the front/back-contract
   invariant PRESCRIBES for placement decisions ("use a server-assigned stable
   id, never transient client state").

   Historically the identical predicate was copy-pasted at the two connect
   sites and could drift (one comment aspired to a `_doneAt` check the code
   never had). Centralising it here removes that drift. Endpoint-mode gating is
   deliberately LEFT at each call site — the two contexts guard it differently
   (outer `_epIteration` scan vs inline `_isEndpointReview` flags) and both are
   correct for their path.

   @param {object} msg - the candidate tail message (may be undefined).
   @param {string} activeTaskId - the task now connecting/reconnecting.
   @returns {boolean} true iff `msg` is an assistant tail owned by a different
     task, or already carries a terminal `finishReason` — i.e. a prior turn a
     fresh placeholder must be pushed ahead of.
   ═══════════════════════════════════════════════════════════════════ */
function assistantTailIsPriorTurn(msg, activeTaskId) {
  if (!msg || msg.role !== 'assistant') return false;
  const _staleTaskId = !!(msg._taskId && msg._taskId !== activeTaskId);
  /* ★ IDENTITY WINS over a terminal field (2026-07-31, conv ms8c0645hwl327).
   *   A tail explicitly BOUND to the task now connecting is that task's own
   *   bubble — never "a prior turn" — no matter what `finishReason` says.
   *
   *   Why the extra arm is load-bearing: `finishReason` is NOT reliably
   *   terminal on the wire. The orchestrator stamps task['finishReason']
   *   ~111 lines before it flips task['status']='done' (_finalize.py), and
   *   that window contains the blocking `_generate_tool_summary` LLM call —
   *   seconds, not microseconds. A poll landing inside it copies a
   *   finishReason onto a message whose turn is still generating. Treating
   *   that as "prior turn" made connectToTask push a fresh placeholder with a
   *   NEW _msgId; the deltas moved there, the original bubble froze
   *   mid-sentence, and the next repaint painted BOTH — two bubbles for ONE
   *   conv.messages entry.
   *
   *   The `!!finishReason` arm is deliberately KEPT for tails NOT bound to
   *   this task: `_taskId` is not persisted, so a DB-loaded completed tail has
   *   none and must still be preceded by a fresh placeholder (the reload-safe
   *   case pinned by test_frontend_connecttotask_taskid_dedupe.py Scenario D).
   *   Narrowing to "identity wins" keeps that; DROPPING the arm would not. */
  if (msg._taskId && activeTaskId && msg._taskId === activeTaskId) return false;
  const _isCompletedTurn = !!msg.finishReason;
  return _staleTaskId || _isCompletedTurn;
}
if (typeof window !== 'undefined') window.assistantTailIsPriorTurn = assistantTailIsPriorTurn;

/* ═══════════════════════════════════════════════════════════════════
   pollWriteWouldClobberSettledTail(msg, polledTaskId, data) — P1b flicker
   guard. Single source of truth for "may this poll / Case-B recovery snapshot
   OVERWRITE the trailing assistant message's content?".

   The flicker (conv mrnee15nzqnoej): a turn interrupted by a server crash has
   NO persisted terminal metadata, so several recovery writers — the SSE
   cold-replay `state`, the `_pollFallback` loop, and startup Case-B — each
   recompute the tail content from a DIFFERENT fold source (event-log fold vs
   the 5 s checkpoint) and repaint. When the two folds are similar length,
   neither wins decisively and the bubble visibly swaps back and forth.

   The fix is a monotonic, single-writer-wins rule for a SETTLED tail:
   once the tail carries a terminal `finishReason` (the turn is settled —
   interrupted / stop / …), a later poll snapshot may only be adopted when it
   STRICTLY GROWS the content; an equal-or-shorter variant (the competing fold)
   is rejected so it can never swap the displayed text. A snapshot from a
   DIFFERENT task than the one that owns the tail is likewise rejected — a
   stale/superseded task must not rewrite a settled turn.

   Returns true when the write must be SUPPRESSED (would clobber). A live,
   not-yet-settled tail (no finishReason) is never suppressed — normal
   streaming/growth flows through untouched.

   @param {object} msg - trailing assistant message (may be undefined).
   @param {string} polledTaskId - the task id this poll snapshot is FOR.
   @param {object} data - the poll/recovery payload ({content, status, ...}).
   @returns {boolean} true iff adopting `data.content` would clobber a settled tail.
   ═══════════════════════════════════════════════════════════════════ */
function pollWriteWouldClobberSettledTail(msg, polledTaskId, data) {
  if (!msg || msg.role !== 'assistant') return false;
  const settled = !!msg.finishReason;
  if (!settled) return false;                 // live turn — allow normal writes
  // A snapshot for a different task than the settled tail owns must not rewrite it.
  if (msg._taskId && polledTaskId && msg._taskId !== polledTaskId) return true;
  const newContent = (data && typeof data.content === 'string') ? data.content : null;
  if (newContent == null) return false;       // no content in payload — nothing to clobber
  const oldLen = (msg.content || '').length;
  // Adopt ONLY a strict growth; equal/shorter competing fold is suppressed so
  // the displayed content cannot oscillate between two variants.
  return newContent.length <= oldLen;
}
if (typeof window !== 'undefined') window.pollWriteWouldClobberSettledTail = pollWriteWouldClobberSettledTail;

/* ═══════════════════════════════════════════════════════════════════
   convTitleById(cid) — resolve a conversation id to its human-readable TITLE.

   Peer/operator surfaces (the queued-message bar, the project_message /
   project_intervene delivery card) carry a bare conversation id — often the
   TRUNCATED 8-char display form (`mradmzmd`) — where a user expects a title.
   A raw id is meaningless to a human, so this is the single frontend seam that
   maps an id → title. It matches on the full id first, then by unique prefix
   (so an 8-char id still resolves against the loaded `conversations` list),
   and falls back to a localized "Untitled chat" — NEVER a bare id.

   Returns '' when `cid` is empty. Best-effort: reads the in-memory
   `conversations` list only (no fetch), so it degrades to the fallback label
   when the conversation isn't loaded rather than blocking on the network.
   ═══════════════════════════════════════════════════════════════════ */
function convTitleById(cid) {
  const _t = (typeof t === 'function') ? t : (k => k);
  if (!cid) return '';
  try {
    if (typeof conversations !== 'undefined' && Array.isArray(conversations)) {
      let hit = conversations.find(c => c && c.id === cid);
      if (!hit) {
        // Prefix match (the id may be the 8-char display form). Accept only an
        // unambiguous single match — never guess between two conversations.
        const pre = conversations.filter(c => c && c.id && c.id.indexOf(cid) === 0);
        if (pre.length === 1) hit = pre[0];
      }
      if (hit && hit.title && String(hit.title).trim()) return String(hit.title).trim();
    }
  } catch (e) { if (typeof console !== 'undefined') console.debug('[convTitleById] lookup failed', e); }
  return _t('toast.untitledConv');
}
if (typeof window !== 'undefined') window.convTitleById = convTitleById;

/* ═══════════════════════════════════════════════════════════════════
   convAutoTranslateEffective(conv) — resolver for the ON-OPEN / ON-ACTIVATE
   retro-translate decision (a FINISHED message that still has no translation).

   The per-conversation `autoTranslate` is FROZEN at send-time so a mid-task
   toggle can't change an in-flight run (the cross-talk fix — see
   .tofu/skills/finishstream-global-autotranslate-bug.md). That freeze is right
   for the live send/regenerate path, but it must NOT permanently veto the
   user's CURRENT intent for an already-generated message: a conversation
   frozen OFF could otherwise never be auto-translated even after the global
   toggle is turned ON, leaving an old reply demanding a manual click forever
   (the reported bug). So for THIS decision the LIVE global toggle wins when
   it's ON; otherwise fall back to the frozen per-conv value (an explicit
   per-conv ON is still honored). The live send/regenerate/in-flight paths keep
   using `convAutoTranslate` (the frozen value) unchanged.
   ═══════════════════════════════════════════════════════════════════ */
function convAutoTranslateEffective(conv) {
  if (typeof autoTranslate !== 'undefined' && autoTranslate) return true;
  return convAutoTranslate(conv);
}
if (typeof window !== 'undefined') window.convAutoTranslateEffective = convAutoTranslateEffective;

/* ═══════════════════════════════════════════════════════════════════
   _mergeTerminalTurnFields(localMsg, serverMsg) — THE single source of
   truth for the terminal turn-metadata field list.

   The bug class this kills (conv mrzutwddkeuw0n, 2026-07-25): every
   keep-local merge site used to hand-enumerate the fields it copied from
   the server's settled message, and every hand-written list predated the
   terminal cost-accounting fields. A locally-cached mid-stream message
   then half-upgraded on a merge — usage showed, cost lazily computed —
   while the finish bar's per-round table (needs apiRounds), the popover
   Task ID row (needs _taskId) and the key-tail route tag (needs
   apiRounds[-1].usage._dispatch) NEVER rendered. Hand-copying the list a
   third/fourth time is exactly how it kept drifting; it lives HERE once.

   Semantics (identical at every call site):
     • apiRounds — upgrade-if-longer: adopt the server's list ONLY when
       the local copy is missing or SHORTER (a mid-stream local may hold
       a partial round list; never downgrade it).
     • everything else — fill-if-missing: adopt only fields the local
       message lacks (never clobber a value already present).

   Call-site notes:
     • core/conversations.js (loadConversationMessages MERGE_ACTIVE_TASK)
       routes its whole per-index merge loop through this.
     • core/cross_tab_sync.js (_verifyActiveConvFromServer Case 2) calls
       it OUTSIDE the content-growth gate — a settled turn's fields must
       land even when nothing grew — and keeps its deliberate server-wins
       lines for content/thinking/toolRounds inside the gate.
     • main/main_init_tasks.js (initActiveTasks Case B / Case F) calls it
       AFTER its deliberate server-wins lines (the poll payload is
       terminal-authoritative), so those no-op here and only the
       previously-missing fields land. Case B adapts the poll payload's
       task id onto the source's `_taskId` first (the wire carries it as
       `taskId`/`id`, not `_taskId`).

   Pure reducer over two message dicts; reads nothing else, so it is
   load-order-safe for all consumers (every call is at runtime, never at
   module-load time). Returns the number of fields filled/upgraded so
   callers can gate their changed/repaint flag.
   ═══════════════════════════════════════════════════════════════════ */
function _mergeTerminalTurnFields(lm, sm) {
  if (!lm || !sm || typeof lm !== 'object' || typeof sm !== 'object') return 0;
  let n = 0;
  if (Array.isArray(sm.apiRounds)
      && (!Array.isArray(lm.apiRounds) || sm.apiRounds.length > lm.apiRounds.length)) {
    lm.apiRounds = sm.apiRounds;
    n++;
  }
  const _TERMINAL_FILL = (
    'finishReason usage model _taskId cost provider_id preset thinkingDepth ' +
    'modifiedFiles modifiedFileList fallbackModel fallbackFrom fallbackReason fallbackKind'
  ).split(' ');
  for (const f of _TERMINAL_FILL) {
    if (sm[f] != null && lm[f] == null) { lm[f] = sm[f]; n++; }
  }
  return n;
}
if (typeof window !== 'undefined') window._mergeTerminalTurnFields = _mergeTerminalTurnFields;

/* ═══════════════════════════════════════════════════════════════════
   _mergeTranslationFields(localMsg, serverMsg) — THE single source of
   truth for adopting a server-committed translation onto a local message.

   The bug class this kills: server-side auto-translate is an AFTER-THE-FACT
   writer. It commits `translatedContent` (+ per-round
   `segments[].translatedText`) to the DB LONG after the turn settled, and
   announces it two ways: a fire-and-forget `translate` push frame, and a
   `conv_changed` notify carrying the post-commit rev
   (lib/translate/commit.py). The push frame is lossy BY DESIGN — the hub
   drops it when no client is subscribed at emit time and offers no replay
   (lib/agent_core/push.py::_deliver_frame) — so `conv_changed` is the
   RELIABLE half of the signal.

   But the notify path's adopter (`_verifyActiveConvFromServer`) only ever
   merged content / thinking / toolRounds behind a "did the turn GROW?" gate,
   plus the terminal-metadata fields via _mergeTerminalTurnFields. A
   translation commit grows NOTHING: same message count, same content, same
   toolRounds — only `translatedContent` and `segments[].translatedText`
   appear. So the verify ran, found "no change", and dropped the translation
   on the floor. The Chinese then surfaced ONLY when the user hit refresh or
   switched conversations (loadConversationMessages' own translation merge,
   the working twin below) — exactly the forced-refresh dependency this
   project's sync contract forbids.

   Semantics — STRICTLY ADDITIVE, never destructive:
     • deliverable: adopt `translatedContent` only when the local copy lacks
       it (a local 译文 is never clobbered), carrying the display flags
       (_showingTranslation / _translateDone / _translateModel /
       originalContent) that the render path reads alongside it.
     • per-round narration: adopt `segments[j].translatedText` for each
       positionally-aligned non-deliverable text segment whose `llmRound`
       matches, only where the local segment has none.
     • `_translatePartialByRound` sidecar: fill-if-missing, so a later
       whole-bubble repaint still has its source.

   IDENTITY GUARD (load-bearing): the merge is skipped unless the two
   messages are the SAME turn — equal role, equal endpoint-lane flags, and
   BYTE-EQUAL content. A translation is only valid for the exact text it was
   produced from; adopting one across an edited/regenerated turn would show a
   译文 that does not correspond to its 原文.

   Pure reducer over two message dicts (no DOM, no globals), so it is
   load-order-safe. Returns the number of fields adopted so callers can gate
   their changed/repaint flag.
   ═══════════════════════════════════════════════════════════════════ */
function _mergeTranslationFields(lm, sm) {
  if (!lm || !sm || typeof lm !== 'object' || typeof sm !== 'object') return 0;
  // Same-turn identity — a translation is only valid for the text it came from.
  if (sm.role !== lm.role) return 0;
  if (!!sm._isEndpointPlanner !== !!lm._isEndpointPlanner) return 0;
  if (!!sm._isEndpointReview !== !!lm._isEndpointReview) return 0;
  if (sm._epIteration !== lm._epIteration) return 0;
  if ((sm.content || '') !== (lm.content || '')) return 0;

  let n = 0;
  if (sm.translatedContent && !lm.translatedContent) {
    lm.translatedContent = sm.translatedContent;
    lm._showingTranslation = sm._showingTranslation !== false;
    lm._translateDone = true;
    if (sm._translateModel && !lm._translateModel) lm._translateModel = sm._translateModel;
    if (sm.originalContent && !lm.originalContent) lm.originalContent = sm.originalContent;
    n++;
  }
  if (Array.isArray(sm.segments) && Array.isArray(lm.segments)) {
    const segN = Math.min(sm.segments.length, lm.segments.length);
    for (let j = 0; j < segN; j++) {
      const ss = sm.segments[j], ls = lm.segments[j];
      if (!ss || !ls) continue;
      if (ss.type !== 'text' || ss.deliverable) continue;
      if (ss.type !== ls.type || ss.llmRound !== ls.llmRound) continue;
      const zh = ss.translatedText;
      if (zh && zh.trim() && !(ls.translatedText && ls.translatedText.trim())) {
        ls.translatedText = zh;
        n++;
      }
    }
  }
  if (sm._translatePartialByRound && !lm._translatePartialByRound) {
    lm._translatePartialByRound = sm._translatePartialByRound;
  }
  return n;
}
if (typeof window !== 'undefined') window._mergeTranslationFields = _mergeTranslationFields;

/* ═══════════════════════════════════════════════════════════════════
   _mergeServerTranslations(sourceMsgs, destMsgs) — array-level wrapper
   over _mergeTranslationFields.

   Extracted 2026-07-31 (pt_3879f00e sub-part 2 slice 12) from a nested
   closure inside conversations.js::loadConversationMessages (~L1130).
   The closure had drifted into a private helper of a 754L function
   while its per-message primitive (_mergeTranslationFields, above)
   lived here in the reducer family — this promotion completes the
   family and gives the array-level wrapper one reusable home instead
   of a closure that could shard if a fourth consumer emerges.

   Behaviour is byte-identical to the original closure: iterate only
   over the OVERLAP of the two arrays (a longer local tail is preserved
   unchanged — the extra local messages have no server counterpart to
   merge from), delegate every per-message merge to
   _mergeTranslationFields, and return the total field count merged so
   callers can gate their log line / repaint flag.

   Pure reducer over two arrays; no DOM, no globals, load-order-safe.
   ═══════════════════════════════════════════════════════════════════ */
function _mergeServerTranslations(sourceMsgs, destMsgs) {
  if (!Array.isArray(sourceMsgs) || !Array.isArray(destMsgs)) return 0;
  const overlap = Math.min(sourceMsgs.length, destMsgs.length);
  let merged = 0;
  for (let i = 0; i < overlap; i++) {
    merged += _mergeTranslationFields(destMsgs[i], sourceMsgs[i]);
  }
  return merged;
}
if (typeof window !== 'undefined') window._mergeServerTranslations = _mergeServerTranslations;

/* ═══════════════════════════════════════════════════════════════════
 * _adoptTaskPlaceholder(conv, taskId, candidate) — send-path placeholder
 * dedupe (pt_44e985ec).
 *
 * The send pipeline mints its assistant placeholder AFTER the /api/chat/send
 * POST returns. In that window an early attach (a conv-state/push-driven
 * reconnect, a recovery path) may already have created AND bound this task's
 * placeholder — the backend announces the task before the POST response
 * lands. Pushing the candidate anyway leaves TWO empty assistant messages:
 * the lanes write the one the stream entry is bound to while the render
 * projection reads the array tail — the 等待中…↔推理中 N字符 flip-flop.
 *
 * Rule: if a message is already bound to THIS task (`_taskId` — the bind
 * stamped at stream-bind time), ADOPT it and re-stamp the canonical
 * client-minted `_msgId` onto it, so the client mint (config.assistantMsgId),
 * the backend's DB slot (_new_assistant_slot adopts the same id) and the
 * live translation routing keep ONE identity. A message bound to a DIFFERENT
 * task is never adopted. The scan is a self-contained tail-up walk (mirrors
 * conversation_list._resolveAssistantByTaskId — inlined so the helper stays
 * load-order-free for headless harnesses).
 *
 * Pure reducer over the conv's messages (mutates only the adopted message's
 * _msgId); returns { msg, adopted } — the caller pushes only when
 * adopted === false.
 * ═══════════════════════════════════════════════════════════════════ */
function _adoptTaskPlaceholder(conv, taskId, candidate) {
  let existing = null;
  const msgs = (conv && Array.isArray(conv.messages)) ? conv.messages : [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m && m.role === 'assistant' && m._taskId === taskId) { existing = m; break; }
  }
  if (existing) {
    if (candidate && candidate._msgId) existing._msgId = candidate._msgId;
    return { msg: existing, adopted: true };
  }
  return { msg: candidate, adopted: false };
}
if (typeof window !== 'undefined') window._adoptTaskPlaceholder = _adoptTaskPlaceholder;

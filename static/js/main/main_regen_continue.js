/* ═══════════════════════════════════════════════════════════════════
   main regen continue — extracted from main.js (split 2026-05-28)

   Regenerate / continue: regenerateFromUser, continueAssistant,
   _applyContinueCheckpoint (pure reducer over the server checkpoint fact).

   This file is concatenated by lib/js_bundler.py BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  ★ Regenerate from a specific user message
// ══════════════════════════════════════════════════════
async function regenerateFromUser(idx) {
  const conv = getActiveConv();
  if (!conv) return;
  // ★ SyncFix: don't silently early-return on in-flight stream/task — that
  //   leaves the user thinking nothing happened after clicking Stop. Instead,
  //   synchronously hard-cancel the racing task so we can proceed to truncate
  //   and start a new one in the same click.
  if (activeStreams.has(conv.id) || conv.activeTaskId) {
    console.info(`[SyncFix] regenerateFromUser hard-cancelling racing stream — conv=${conv.id.slice(0,8)} activeTaskId=${conv.activeTaskId?.slice(0,8)||'null'}`);
    if (typeof _hardCancelActiveStream === 'function') {
      _hardCancelActiveStream(conv);
    }
  }
  const msg = conv.messages[idx];
  if (!msg || msg.role !== "user") return;

  // ── Image Gen mode intercept: re-generate via direct image API ──
  const _isIgConv = imageGenMode || conv.imageGenMode;
  const _isIgMsg  = msg._isImageGen || (msg.content && msg.content.startsWith('🎨 '));
  if (_isIgConv || _isIgMsg) {
    conv.messages = conv.messages.slice(0, idx);
    saveConversations(conv.id);
    window.ConvView.replaceAll(conv.id);
    renderConversationList();
    let prompt = msg.content || '';
    if (prompt.startsWith('🎨 ')) prompt = prompt.slice(2).trim();
    const textarea = document.getElementById('userInput');
    if (textarea) { textarea.value = prompt; }
    if (!imageGenMode) _applyImageGenUI(true);
    generateImageDirect();
    return;
  }

  const convId = conv.id;

  // ── Optimistic UI: truncate local messages ──
  conv.messages = conv.messages.slice(0, idx + 1);
  conv._needsLoad = false;
  conv._serverMsgCount = conv.messages.length;

  /* ── Surgical DOM truncation (via unified controller) ── */
  const _syncMsgsBefore = conv.messages.length;
  window.ConvView.removeAfter(conv.id, idx);
  renderConversationList();
  // ★ SyncFix: invariant + log (matches saveEditAndResend)
  console.assert(
    !document.getElementById("streaming-msg"),
    `[SyncFix] Stale streaming-msg after regenerateFromUser truncate — conv=${convId.slice(0,8)}`
  );
  console.info(`[SyncFix] regenerateFromUser conv=${convId.slice(0,8)} idx=${idx} msgsBefore=${_syncMsgsBefore} msgsAfter=${conv.messages.length} hasStreamingMsg=${!!document.getElementById('streaming-msg')} activeTaskId=${conv.activeTaskId?.slice(0,8)||'null'}`);

  // ★ SyncFix: persist truncated state BEFORE /api/chat/regenerate fires, so
  //   a page refresh during the "Waiting" window (fetch in flight) doesn't
  //   resurrect stale assistant messages or reconnect to an old aborted task.
  //   Update IDB cache first (Phase 1 render on refresh), then server DB.
  try { if (typeof ConvCache !== 'undefined') ConvCache.put(conv); }
  catch (e) { console.warn('[SyncFix] ConvCache.put failed:', e); }
  try {
    await syncConversationToServer(conv, { allowTruncate: true });
    console.info(`[SyncFix] regenerateFromUser pre-regenerate sync OK — conv=${convId.slice(0,8)} msgs=${conv.messages.length}`);
  } catch (e) {
    console.warn(`[SyncFix] regenerateFromUser pre-regenerate sync failed: ${e.message}`);
  }

  // ── Atomic backend call: truncate + translate + task start ──
  const _regenConfig = await _buildConvConfig(conv);

  // ★ Mint the assistant message id BEFORE the POST (same as the send path) so
  //   live per-round translation frames route to the still-streaming bubble.
  const _regenAssistantMsgId = (typeof _newClientMsgId === 'function')
    ? _newClientMsgId()
    : ('tmp_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8));
  _regenConfig.assistantMsgId = _regenAssistantMsgId;

  // ★ If autoTranslate is on and message has Chinese, show stop button immediately
  const _regenAbortCtrl = new AbortController();
  let _regenAbortReason = '';  // '' | 'timeout' | 'user-stop'
  const _regenTimeout = setTimeout(() => {
    _regenAbortReason = 'timeout';
    _regenAbortCtrl.abort();
  }, 90000);
  /* ★ Startup-stop affordance (pt_fa32a2351b3840ad): the connecting POST
   *   window shows a STOP button; a click owner-tags conv._genStartStop with
   *   THIS controller and aborts it — the catch below matches that tag. */
  conv._genStartCtrl = _regenAbortCtrl;
  conv._genStartStop = null;
  const _regenText = msg.content || '';
  const _regenWillTranslate = _regenConfig.autoTranslate && /[\u4e00-\u9fff\u3400-\u4dbf]/.test(_regenText);
  if (_regenWillTranslate) {
    conv._translating = true;
    conv._translateAborted = false;
    conv._translateAbortCtrl = _regenAbortCtrl;
    updateSendButton();
    renderConversationList();
    if (activeConvId === convId) _renderTranslatingBubble();
  } else if (activeConvId === convId) {
    /* ★ Fix ①: no translation → deterministic '连接中…' placeholder so the
     *   assistant side is not blank during the synchronous /api/chat/regenerate
     *   POST. Upgraded in place to the streaming bubble on taskId. */
    _renderTranslatingBubble(t('sidebar.connecting'));
    /* ★ Flip the composer to a STOP button for the connecting window. */
    updateSendButton();
  }

  try {
    if (typeof updateContextBar === 'function') updateContextBar();
    const _regenSettings = await _buildConvSettings(conv);
    const resp = await Api.chat.regenerate({
      convId,
      truncateToIndex: idx,
      // ★ Phase 3: stable-id truncate point (authoritative server-side; index
      //   is the fallback). See chat_regenerate's truncateToMsgId handling.
      ...(msg._msgId ? { truncateToMsgId: msg._msgId } : {}),
      config: _regenConfig,
      settings: _regenSettings,
    }, { signal: _regenAbortCtrl.signal });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(err.error || `Server ${resp.status}`);
    }
    const result = await resp.json();

    // Update local state if server translated the message
    if (result.userMessage) {
      // ★ When auto-translate is OFF, the server restores the original text
      //   and DROPS the translation metadata. Object.assign only MERGES keys,
      //   so explicitly delete the stale local translation fields first —
      //   otherwise the bilingual block lingers and a later full-conv sync
      //   would re-PUT the stale originalContent into the DB.
      if (result.restoredOriginal) {
        delete msg.originalContent;
        delete msg._translateDone;
        delete msg._translateModel;
        delete msg._translateFailed;
        delete msg.translatedContent;
        delete msg._translatedCache;
        delete msg._showingTranslation;
      }
      Object.assign(msg, result.userMessage);
      if (activeConvId === convId) {
        const msgEl = document.getElementById('msg-' + idx);
        if (msgEl) window.ConvView.apply(convId, idx, msg);
      }
    }
    if (result.title) conv.title = result.title;
    conv._serverMsgCount = result.msgCount || conv.messages.length;

    // Push assistant msg + connect to task
    const taskId = result.taskId;
    const _mintedPlaceholder = {
      role: "assistant", content: "", thinking: "",
      timestamp: Date.now(), toolRounds: [],
      model: _regenConfig.model || serverModel,
      _msgId: _regenAssistantMsgId,
    };
    // ★ Endpoint mode: mark as planner so SSE reconnection identifies it correctly
    if (_regenConfig.endpointMode) _mintedPlaceholder._isEndpointPlanner = true;
    /* ★ Dedupe (pt_44e985ec): an early attach during the POST window may
     *   already have created + bound this task's placeholder — adopt it
     *   instead of pushing a duplicate (the 等待中…↔推理中 flip-flop class). */
    const _ph = (typeof _adoptTaskPlaceholder === 'function')
      ? _adoptTaskPlaceholder(conv, taskId, _mintedPlaceholder)
      : { msg: _mintedPlaceholder, adopted: false };
    const assistantMsg = _ph.msg;
    if (_ph.adopted) {
      console.warn(
        `[regenerateFromUser] ♻ adopted existing placeholder bound to task=${taskId.slice(0,8)} ` +
        `(canonical msgId re-stamped) — no duplicate push for conv=${convId.slice(0,8)}`
      );
      if (typeof _reportClientError === 'function') {
        _reportClientError(
          `[regenerateFromUser] adopted existing task placeholder instead of pushing a duplicate ` +
          `conv=${convId.slice(0,8)} task=${taskId.slice(0,8)}`);
      }
      if (_regenConfig.endpointMode) assistantMsg._isEndpointPlanner = true;
      /* The adopted placeholder was minted by the early attach under a
       * different _msgId (the helper just re-stamped the canonical one) —
       * re-key the live streaming bubble so identity resolution + translation
       * frames keep landing on the SAME message. */
      const _smEl = (activeConvId === convId) ? document.getElementById('streaming-msg') : null;
      if (_smEl && _regenAssistantMsgId) _smEl.setAttribute('data-msg-id', _regenAssistantMsgId);
    } else {
      _ensureMsgId(assistantMsg);  // no-op when _msgId already set
      conv.messages.push(assistantMsg);
    }
    conv.activeTaskId = taskId;
    saveConversations(convId);

    _removeTranslatingBubble();
    if (activeConvId === convId) _renderStreamingBubble(conv, _regenConfig, _regenAssistantMsgId);
    buildTurnNav(conv);
    connectToTask(convId, taskId);

  } catch (e) {
    const _userClickedStop = !!conv._translateAborted
      || conv._genStartStop === _regenAbortCtrl;
    if (e.name === 'AbortError' && _userClickedStop) {
      console.log('%c[regenerateFromUser] ✗ Aborted during startup by user', 'color:#f59e0b;font-weight:bold');
      await _userStopDuringStartup(conv, convId, { syncOpts: { allowTruncate: true } });
    } else if (e.name === 'AbortError' && _regenAbortReason === 'timeout'
               && typeof _recoverTimedOutChatTask === 'function'
               && await _recoverTimedOutChatTask(convId, { endpointMode: _regenConfig.endpointMode })) {
      // ★ Client safety timer fired, but the server had already started the
      //   task — we reconnected to its live stream. No error, no Retry needed.
      console.log('%c[regenerateFromUser] ✓ Recovered timed-out request by reconnecting to live task', 'color:#34d399;font-weight:bold');
    } else {
      let errMsg;
      if (e.name === 'AbortError' && _regenAbortReason === 'timeout') {
        errMsg = _regenWillTranslate
          ? 'The server took too long to respond and the request was cancelled — it may be overloaded. Please try again; if this keeps happening with Chinese input, disabling auto-translate in Settings can reduce the delay.'
          : 'Regenerate timed out — the server took too long to respond.';
      } else if (e.name === 'AbortError') {
        errMsg = 'Regenerate was aborted before the server replied.';
      } else {
        errMsg = e.message;
      }
      debugLog("Regenerate failed: " + errMsg, "error");
      console.error('[regenerateFromUser] /api/chat/regenerate failed:', e?.name, e?.message,
                    'abortReason=' + _regenAbortReason);
      _removeTranslatingBubble();
      // Surface a visible error bubble so the chat doesn't sit silent.
      const errAssistant = {
        role: 'assistant', content: '', thinking: '',
        error: errMsg, timestamp: Date.now(), toolRounds: [],
      };
      _ensureMsgId(errAssistant);
      conv.messages.push(errAssistant);
      if (activeConvId === convId) {
        window.ConvView.apply(convId, conv.messages.length - 1, errAssistant);
      }
      saveConversations(convId);
      syncConversationToServer(conv, { allowTruncate: true });
      buildTurnNav(conv);
    }
  } finally {
    clearTimeout(_regenTimeout);
    /* ★ Identity-guarded startup-marker cleanup (only OURS). */
    if (conv._genStartCtrl === _regenAbortCtrl || conv._genStartStop === _regenAbortCtrl) {
      conv._genStartCtrl = null;
      conv._genStartStop = null;
    }
    // ★ Fix ①: teardown the pre-POST placeholder unconditionally (it is now
    //   rendered whether or not we translated). Idempotent once the success
    //   path swapped it for the streaming bubble.
    _removeTranslatingBubble();
    if (_regenWillTranslate) {
      conv._translating = false;
      conv._translateAborted = false;
      conv._translateAbortCtrl = null;
      renderConversationList();
    }
    /* ★ Unconditional re-eval (startup-stop affordance): the connecting
     *   window paints a STOP button; generic-error exits must not strand it. */
    updateSendButton();
    if (_regenWillTranslate) renderConversationList();
  }
}

/* ═══════════════════════════════════════════════════════════════════
   ★ Pure reducer: apply the backend's authoritative Continue checkpoint fact.

   The frontend does NOT decide the resume point. /api/chat/continue rescans
   the DB copy (scan_continue_checkpoint), applies + persists the rollback,
   and returns the anchor as a typed fact `ckpt`:
     { resumeMode:'checkpoint'|'prefill', keptRounds:int,
       contentPrefix, priorContent, priorThinking, ...counts }.
   This function REDUCES the local assistant message over that fact — it
   slices its rounds to the server-decided `keptRounds` COUNT and adopts the
   preserved/prior strings verbatim. It must never re-derive the boundary by
   scanning toolRounds for status==='done' (that duplicated the server logic
   and was a client-side lifecycle inference).

   Returns the kept-rounds array (also used to seed the streaming-merge markers).
   ═══════════════════════════════════════════════════════════════════ */
function _applyContinueCheckpoint(assistantMsg, allRounds, ckpt) {
  ckpt = ckpt || {};
  const resumeMode = ckpt.resumeMode || 'checkpoint';
  const keptCount = ckpt.keptRounds || 0;
  // Prefill CONTINUES the same string and keeps its rounds untouched;
  // checkpoint mode keeps exactly the server-decided prefix of rounds.
  const keptRounds = resumeMode === 'prefill'
    ? (allRounds || []).slice()
    : (allRounds || []).slice(0, keptCount);

  assistantMsg.toolRounds = keptRounds;
  assistantMsg.content = (ckpt.contentPrefix != null)
    ? ckpt.contentPrefix
    : (assistantMsg.content || '');
  // Live thinking is cleared — replay-worthy thinking rides per-round on
  // keptRounds[i].thinking (checkpoint) or is display-only (prefill). The
  // server-provided priorThinking/priorContent are the display-only tails.
  assistantMsg.thinking = '';
  if (ckpt.priorThinking) assistantMsg.priorThinking = ckpt.priorThinking;
  else delete assistantMsg.priorThinking;
  if (ckpt.priorContent) assistantMsg.priorContent = ckpt.priorContent;
  else delete assistantMsg.priorContent;
  delete assistantMsg.finishReason;
  delete assistantMsg.toolSummary;
  delete assistantMsg.error;
  // ★ The dead task's bind must go too: keeping `_taskId` makes
  //   `assistantTailIsPriorTurn` (ui/sse_pipeline.js connectToTask) classify
  //   this adopted bubble as a PRIOR turn and push a placeholder twin for
  //   the resume task — the ms43foj3 restart double-bubble. The resume
  //   task's stream/poll re-stamps `_taskId` from its own payloads.
  delete assistantMsg._taskId;

  // Streaming-merge seed consumed by sse_pipeline.js / sse_poll_fallback.js:
  // the pre-continue rounds/prefix to merge with newly-streamed ones.
  if (assistantMsg.content) assistantMsg._continueContentPrefix = assistantMsg.content;
  assistantMsg._continueToolRounds = keptRounds.slice();
  return keptRounds;
}
if (typeof window !== 'undefined') window._applyContinueCheckpoint = _applyContinueCheckpoint;

/* ═══════════════════════════════════════════════════════════════════
   ★ Continue streaming SHELL — raise / roll back.

   WHY IN PLACE (and why NOT _renderTranslatingBubble):
   The send / regenerate paths answer their own pre-POST dead zone by
   APPENDING a `#translating-msg` placeholder at the tail. That is only sound
   there because those paths TRUNCATE the assistant message first, so the tail
   is free. Continue does NOT truncate — the interrupted assistant bubble IS
   the tail and stays put — so appending would render TWO assistant bubbles
   (the interrupted one + a fake twin) for the entire POST window. That is the
   recurring twin-bubble defect the ms43foj3 note in _applyContinueCheckpoint
   documents.

   So the SAME node is converted in place: `msg-N` → `#streaming-msg`, header
   gains the elapsed timer, body becomes the live zones. One data entry ⇒ one
   DOM node, invariant intact, and the `Continuing…` pulse lands on the CLICK
   FRAME instead of after two serial round-trips.

   Height note: raising the shell is also where the bubble COLLAPSES (the tail
   prose/tool panel are replaced by empty zones, then repainted from the
   message document). Doing it at click time means the collapse happens while
   the user is looking at the button, and one real-height bottom pin settles
   it. The later checkpoint repaint only grows content BELOW the fold, which
   never moves what the reader is looking at — so no post-hoc scroll rescue is
   needed at all. Painting via `updateStreamingUI(assistantMsg)` immediately
   after raising the shell keeps the pre-rollback content visible, so the
   bubble never flashes empty.
   ═══════════════════════════════════════════════════════════════════ */
function _raiseContinueShell(conv, assistantMsg) {
  const lastIdx = conv.messages.length - 1;
  /* ★ IDENTITY-FIRST resolve (RENDER_CONTRACT Invariant 2). NOT
   *   `getElementById('msg-' + lastIdx)`.
   *
   *   That positional lookup used to be safe HERE only because it ran AFTER
   *   `ConvView.replaceAll(...)`, which had just re-stamped every `msg-N`. This
   *   function deliberately runs BEFORE any render (that is the whole point —
   *   feedback on the click frame), so the freshness precondition is gone: when
   *   `conv.messages` shrank without a repaint (poll / merge / peer reconcile),
   *   the slot `msg-{length-1}` belongs to an OLDER message, and converting it
   *   would rewrite a HISTORICAL answer into the `Continuing…` pulse while the
   *   genuinely-interrupted turn sat untouched — and still report success.
   *
   *   `ConvView.findMessageEl` is the shared identity-first seam
   *   (`data-msg-id` → positional fallback for id-less legacy rows). Resolving
   *   a MISS to `false` is load-bearing: the caller's "repaint, then raise"
   *   fallback is what handles a tail evicted by the lazy window. */
  const msgEl = (window.ConvView && typeof window.ConvView.findMessageEl === 'function')
    ? window.ConvView.findMessageEl(assistantMsg, lastIdx)
    : null;
  if (!msgEl) return false;
  msgEl.id = "streaming-msg";
  const hdr = msgEl.querySelector(".message-header");
  if (hdr && !hdr.querySelector("#stream-elapsed-timer")) {
    const tmEl = document.createElement("span");
    tmEl.id = "stream-elapsed-timer";
    tmEl.className = "stream-elapsed-timer";
    hdr.appendChild(tmEl);
  }
  const bodyEl = msgEl.querySelector(".message-body");
  if (bodyEl) {
    bodyEl.id = "streaming-body";
    if (!bodyEl.querySelector('[data-zone="content"]')) {
      bodyEl.innerHTML =
        '<div data-zone="tool"></div><div data-zone="thinking"></div><div data-zone="content"></div><div data-zone="status"><div class="stream-status"><div class="pulse"></div> Continuing…</div></div>';
    }
    if (typeof updateStreamingUI === 'function') updateStreamingUI(assistantMsg);
  }
  /* Real-height bottom pin — NOT the bare `scrollToBottom()`, which core.js
   * early-returns from when the reader sits >200px off the bottom, and which
   * (single-rAF against content-visibility:auto ESTIMATES) "lands
   * mid-history". _forceScrollToBottom flips cv-off for real heights and
   * re-asserts across double-rAF + a 150ms timer. */
  if (typeof _forceScrollToBottom === 'function') _forceScrollToBottom(null, true);
  return true;
}
if (typeof window !== 'undefined') window._raiseContinueShell = _raiseContinueShell;

/* Tear the shell back down to the settled static bubble.
 *
 * Load-bearing for the failure path: once the shell is raised EARLY, a POST
 * that fails would otherwise strand the user in front of a frozen
 * `Continuing…` pulse forever. finalizeStreaming re-renders the message
 * document verbatim — and because the failure path never mutated that
 * document (the checkpoint reducer runs only on success), the turn comes back
 * with its `finishReason='interrupted'` intact, i.e. its Continue affordance
 * still there and still clickable. */
function _rollbackContinueShell(conv, assistantMsg) {
  if (!document.getElementById('streaming-msg')) return false;
  if (window.ConvView && typeof window.ConvView.finalizeStreaming === 'function') {
    return !!window.ConvView.finalizeStreaming(conv.id, assistantMsg);
  }
  return false;
}
if (typeof window !== 'undefined') window._rollbackContinueShell = _rollbackContinueShell;

// ══════════════════════════════════════════════════════
//  ★ Continue: resume an interrupted assistant response
//
//  Backend-authoritative: /api/chat/continue OWNS the checkpoint decision
//  (scan_continue_checkpoint rescans the DB, applies + persists the rollback)
//  and returns a typed fact. The frontend POSTs first, then REDUCES over that
//  fact via _applyContinueCheckpoint — it never re-scans status==='done'.
// ══════════════════════════════════════════════════════
async function continueAssistant() {
  const conv = getActiveConv();
  /* ★ IN-FLIGHT GATE. `conv.activeTaskId` is only assigned AFTER the POST
   *   returns, so it cannot guard the dead zone that precedes it: a second
   *   click during the round-trip used to replay the whole rollback + task
   *   start. `_continueInFlight` is set BEFORE the first await and cleared in
   *   `finally`, so the window is closed from the click frame onward. */
  if (!conv || activeStreams.has(conv.id) || conv.activeTaskId
      || conv._continueInFlight) return;
  const assistantMsg = conv.messages[conv.messages.length - 1];
  if (!assistantMsg || assistantMsg.role !== "assistant") return;
  // ★ Align with the backend chat_continue empty-guard, which treats a turn as
  //   empty only when content AND thinking AND toolRounds are all absent. A
  //   turn cut off mid-tool-loop (tool rounds present, no prose yet) is NOT
  //   empty — it has a recoverable checkpoint the server can resume from — so
  //   it must go through the POST, not this pop-and-regenerate shortcut.
  const _hasRounds = !!(getToolRoundsFromMsg(assistantMsg) || []).length;
  if (!assistantMsg.content && !assistantMsg.thinking && !_hasRounds) {
    // Nothing to continue — message is empty, just regenerate
    conv.messages.pop();
    conv._needsLoad = false;
    conv._serverMsgCount = conv.messages.length;
    await syncConversationToServer(conv, { allowTruncate: true });
    await startAssistantResponse(conv.id);
    return;
  }

  // Snapshot pre-continue state for the streaming-merge seed + a possible
  // fallback restore. The server rescans the DB copy, so we do NOT compute
  // any checkpoint here.
  const allRounds = getToolRoundsFromMsg(assistantMsg);
  const _origApiRounds = (assistantMsg.apiRounds || []).slice();
  const _origUsage = assistantMsg.usage ? { ...assistantMsg.usage } : null;
  const _origModifiedFiles = assistantMsg.modifiedFiles;
  const _origModifiedFileList = (assistantMsg.modifiedFileList || []).slice();

  /* ★ Continue reuses the SAME assistant message (stable _msgId) so live
   *   per-round translation frames route to the still-streaming bubble.
   *   Synchronous — MUST stay ahead of the gate so nothing awaits before it. */
  if (typeof _ensureMsgId === 'function') _ensureMsgId(assistantMsg);

  /* ★ THE DEAD-ZONE FIX — raise the streaming shell on the CLICK FRAME,
   *   BEFORE _buildConvConfig / the full-conversation sync / the POST. Those
   *   three serial awaits used to run with ZERO visual change, so the click
   *   read as "nothing happened". The shell is an IN-PLACE conversion of the
   *   existing tail bubble (never an appended placeholder — see
   *   _raiseContinueShell for why the regenerate-style append would mint a
   *   twin here).
   *
   *   Gate FIRST, so even a synchronous double-click cannot get two runs into
   *   the window; `finally` clears it on every exit. NOTHING above this line
   *   may `await` — an await before the gate reopens the re-entry window it
   *   exists to close (pinned by test_frontend_continue_click_feedback.py::
   *   test_no_await_precedes_the_inflight_gate). */
  conv._continueInFlight = true;
  let _shellUp = false;
  if (activeConvId === conv.id) _shellUp = _raiseContinueShell(conv, assistantMsg);

  try {
    const cfgPayload = await _buildConvConfig(conv);
    if (assistantMsg._msgId) cfgPayload.assistantMsgId = assistantMsg._msgId;

    // ★ Sync local-only edits/attachments so the server rescans the freshest
    //   copy, then let the server compute + persist the checkpoint.
    await syncConversationToServer(conv);

    let data;
    try {
      const res = await Api.chat.continue({ convId: conv.id, config: cfgPayload });
      /* ★ ok BEFORE json: a proxy 502 answers with an HTML body, so parsing
       *   first threw INSIDE json() and the real status never surfaced. */
      if (!res.ok) {
        const _err = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
        throw new Error(_err.error || `Server ${res.status}`);
      }
      data = await res.json();
    } catch (e) {
      /* ★ VISIBLE failure. debugLog alone only reaches the debug panel, so a
       *   failed Continue was a total dead end — and with the shell now raised
       *   early, staying silent would strand the user in front of a frozen
       *   `Continuing…` pulse. Roll the bubble back to its settled form (the
       *   message document was never mutated on this path, so the turn returns
       *   with finishReason='interrupted' and its Continue button intact) and
       *   say so out loud. */
      debugLog("Continue failed: " + e.message, "error");
      console.error('[continueAssistant] /api/chat/continue failed:', e?.name, e?.message);
      if (_shellUp && activeConvId === conv.id) _rollbackContinueShell(conv, assistantMsg);
      if (typeof showToast === 'function') {
        showToast(t('continue.failed', { err: e.message || '' }), 'error');
      }
      return;
    }

    // ── Fallback: server found no recoverable checkpoint / declined prefill ──
    if (data.fallback === 'regenerate') {
      debugLog(
        `Continue: server reports no recoverable checkpoint (${data.reason}); ` +
        `falling back to full regeneration`, "info");
      if (data.reason && data.reason !== 'empty_assistant') {
        showToast("\u65e0\u6cd5\u7eed\u63a5\uff08\u65e0\u5de5\u5177\u8c03\u7528\u68c0\u67e5\u70b9\uff09\uff0c\u5c06\u91cd\u65b0\u751f\u6210\u56de\u590d", "info");
      }
      conv.messages.pop();
      conv._needsLoad = false;
      conv._serverMsgCount = conv.messages.length;
      /* The shell was raised on the bubble we are about to pop — tear it down
       * first so the repaint can't strand a live `#streaming-msg` husk. */
      if (_shellUp) _rollbackContinueShell(conv, assistantMsg);
      if (activeConvId === conv.id) {
        window.ConvView.replaceAll(conv.id, { forceScroll: false });
        if (typeof _forceScrollToBottom === 'function') _forceScrollToBottom(null, true);
      }
      await syncConversationToServer(conv, { allowTruncate: true });
      await startAssistantResponse(conv.id);
      return;
    }

    // ── Reduce over the authoritative checkpoint fact (pure) ──
    const ckpt = data.checkpoint || {};
    const taskId = data.taskId;
    const keptRounds = _applyContinueCheckpoint(assistantMsg, allRounds, ckpt);

    // Seed the remaining streaming-merge markers from the pre-continue snapshot
    // (the reducer set _continueContentPrefix + _continueToolRounds).
    assistantMsg._continueApiRounds = _origApiRounds;
    if (_origUsage) assistantMsg._continueUsage = _origUsage;
    if (_origModifiedFiles) assistantMsg._continueModifiedFiles = _origModifiedFiles;
    if (_origModifiedFileList.length) assistantMsg._continueModifiedFileList = _origModifiedFileList;

    // ── User-facing summary (counts straight from the server fact) ──
    const _disc = ckpt.discardedRounds || 0;
    const _discContent = ckpt.discardedContentLen || 0;
    const _discThinking = ckpt.discardedThinking || 0;
    if ((ckpt.resumeMode || 'checkpoint') === 'checkpoint'
        && (_disc > 0 || _discContent > 0 || _discThinking > 0)) {
      const preserveParts = [];
      if ((ckpt.preservedContentLen || 0) > 0) preserveParts.push(`${ckpt.preservedContentLen} \u5b57\u7b26\u5185\u5bb9`);
      if ((ckpt.preservedThinkingChars || 0) > 0) preserveParts.push(`${ckpt.preservedThinkingChars} \u5b57\u7b26\u601d\u8003\u5185\u5bb9`);
      const preserveNote = preserveParts.length ? ` (\u4fdd\u7559\u4e86 ${preserveParts.join(" + ")})` : '';
      const discardParts = [];
      if (_discContent > 0) discardParts.push(`${_discContent} \u5b57\u7b26\u540e\u7eed\u6587\u672c`);
      if (_discThinking > 0) discardParts.push(`${_discThinking} \u5b57\u7b26\u601d\u8003`);
      if (_disc > 0) discardParts.push(`${_disc} \u4e2a\u672a\u5b8c\u6210\u5de5\u5177\u8c03\u7528`);
      const discardNote = discardParts.length ? `\uff0c\u4e22\u5f03\u4e86 ${discardParts.join(" + ")}` : '';
      showToast(`\u4ece\u7b2c ${ckpt.keptRounds || 0} \u8f6e\u5de5\u5177\u8c03\u7528\u540e\u6062\u590d${preserveNote}${discardNote}`, "info");
    }
    debugLog(
      `Continue: server checkpoint mode=${ckpt.resumeMode || 'checkpoint'} ` +
      `kept=${ckpt.keptRounds || 0} discarded=${_disc} ` +
      `preservedContent=${ckpt.preservedContentLen || 0} discardedContent=${_discContent}`,
      "info");
    void keptRounds;

    // ── Streaming UI: the shell is ALREADY live (raised on the click frame).
    //    Repaint its zones from the freshly-reduced message document. The
    //    rollback SHRINKS this bubble, but the reader was pinned to the bottom
    //    when the shell went up, so the shrink just re-clamps at the bottom and
    //    the refill grows BELOW the fold — nothing the reader is looking at
    //    moves. That is why there is no post-hoc scroll rescue here: the jump it
    //    used to paper over cannot occur.
    if (activeConvId === conv.id) {
      if (document.getElementById('streaming-msg')) {
        if (typeof updateStreamingUI === 'function') updateStreamingUI(assistantMsg);
      } else {
        /* Shell never went up (tail evicted by the lazy window when the reader
         * was parked up in history). Repaint, then raise it now. */
        window.ConvView.replaceAll(conv.id, { forceScroll: false });
        _raiseContinueShell(conv, assistantMsg);
      }
    }

    conv.activeTaskId = taskId;
    saveConversations(conv.id);
    // No re-sync — /api/chat/continue already persisted both the rolled-back
    // state AND activeTaskId; a second PUT would race the streaming task.
    connectToTask(conv.id, taskId);
  } finally {
    /* Gate closes on EVERY exit — success, fallback-to-regenerate, POST
     * failure, or an unexpected throw. Leaking it would wedge Continue for
     * this conversation until reload. */
    delete conv._continueInFlight;
  }
}

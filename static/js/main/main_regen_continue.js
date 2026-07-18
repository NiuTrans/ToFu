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
    renderChat(conv);
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
        if (msgEl) msgEl.outerHTML = renderMessage(msg, idx);
      }
    }
    if (result.title) conv.title = result.title;
    conv._serverMsgCount = result.msgCount || conv.messages.length;

    // Push assistant msg + connect to task
    const taskId = result.taskId;
    const assistantMsg = {
      role: "assistant", content: "", thinking: "",
      timestamp: Date.now(), toolRounds: [],
      model: _regenConfig.model || serverModel,
      _msgId: _regenAssistantMsgId,
    };
    // ★ Endpoint mode: mark as planner so SSE reconnection identifies it correctly
    if (_regenConfig.endpointMode) assistantMsg._isEndpointPlanner = true;
    _ensureMsgId(assistantMsg);  // no-op when _msgId already set
    conv.messages.push(assistantMsg);
    conv.activeTaskId = taskId;
    saveConversations(convId);

    _removeTranslatingBubble();
    if (activeConvId === convId) _renderStreamingBubble(conv, _regenConfig, _regenAssistantMsgId);
    buildTurnNav(conv);
    connectToTask(convId, taskId);

  } catch (e) {
    const _userClickedStop = !!conv._translateAborted;
    if (e.name === 'AbortError' && _regenWillTranslate && _userClickedStop) {
      console.log('%c[regenerateFromUser] ✗ Aborted during translation by user', 'color:#f59e0b;font-weight:bold');
      _removeTranslatingBubble();
      saveConversations(convId);
      syncConversationToServer(conv, { allowTruncate: true });
      buildTurnNav(conv);
      Api.chat.abortConv(convId);
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
        const chatInnerEl = document.getElementById('chatInner');
        if (chatInnerEl) {
          chatInnerEl.insertAdjacentHTML('beforeend',
            renderMessage(errAssistant, conv.messages.length - 1));
        }
      }
      saveConversations(convId);
      syncConversationToServer(conv, { allowTruncate: true });
      buildTurnNav(conv);
    }
  } finally {
    clearTimeout(_regenTimeout);
    // ★ Fix ①: teardown the pre-POST placeholder unconditionally (it is now
    //   rendered whether or not we translated). Idempotent once the success
    //   path swapped it for the streaming bubble.
    _removeTranslatingBubble();
    if (_regenWillTranslate) {
      conv._translating = false;
      conv._translateAborted = false;
      conv._translateAbortCtrl = null;
      updateSendButton();
      renderConversationList();
    }
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

  // Streaming-merge seed consumed by sse_pipeline.js / sse_poll_fallback.js:
  // the pre-continue rounds/prefix to merge with newly-streamed ones.
  if (assistantMsg.content) assistantMsg._continueContentPrefix = assistantMsg.content;
  assistantMsg._continueToolRounds = keptRounds.slice();
  return keptRounds;
}
if (typeof window !== 'undefined') window._applyContinueCheckpoint = _applyContinueCheckpoint;

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
  if (!conv || activeStreams.has(conv.id) || conv.activeTaskId) return;
  const assistantMsg = conv.messages[conv.messages.length - 1];
  if (!assistantMsg || assistantMsg.role !== "assistant") return;
  if (!assistantMsg.content && !assistantMsg.thinking) {
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

  const cfgPayload = await _buildConvConfig(conv);
  // ★ Continue reuses the SAME assistant message (stable _msgId) so live
  //   per-round translation frames route to the still-streaming bubble.
  if (typeof _ensureMsgId === 'function') _ensureMsgId(assistantMsg);
  if (assistantMsg._msgId) cfgPayload.assistantMsgId = assistantMsg._msgId;

  // ★ Sync local-only edits/attachments so the server rescans the freshest
  //   copy, then let the server compute + persist the checkpoint.
  await syncConversationToServer(conv);

  let data;
  try {
    const res = await Api.chat.continue({ convId: conv.id, config: cfgPayload });
    data = await res.json();
    if (!res.ok) throw new Error(data.error || "Request failed");
  } catch (e) {
    debugLog("Continue failed: " + e.message, "error");
    return;
  }

  // ── Fallback: server found no recoverable checkpoint / declined prefill ──
  if (data.fallback === 'regenerate') {
    debugLog(
      `Continue: server reports no recoverable checkpoint (${data.reason}); ` +
      `falling back to full regeneration`, "info");
    if (data.reason && data.reason !== 'empty_assistant') {
      showToast("无法续接（无工具调用检查点），将重新生成回复", "info");
    }
    conv.messages.pop();
    conv._needsLoad = false;
    conv._serverMsgCount = conv.messages.length;
    if (activeConvId === conv.id) renderChat(conv, false);
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
    if ((ckpt.preservedContentLen || 0) > 0) preserveParts.push(`${ckpt.preservedContentLen} 字符内容`);
    if ((ckpt.preservedThinkingChars || 0) > 0) preserveParts.push(`${ckpt.preservedThinkingChars} 字符思考内容`);
    const preserveNote = preserveParts.length ? ` (保留了 ${preserveParts.join(" + ")})` : '';
    const discardParts = [];
    if (_discContent > 0) discardParts.push(`${_discContent} 字符后续文本`);
    if (_discThinking > 0) discardParts.push(`${_discThinking} 字符思考`);
    if (_disc > 0) discardParts.push(`${_disc} 个未完成工具调用`);
    const discardNote = discardParts.length ? `，丢弃了 ${discardParts.join(" + ")}` : '';
    showToast(`从第 ${ckpt.keptRounds || 0} 轮工具调用后恢复${preserveNote}${discardNote}`, "info");
  }
  debugLog(
    `Continue: server checkpoint mode=${ckpt.resumeMode || 'checkpoint'} ` +
    `kept=${ckpt.keptRounds || 0} discarded=${_disc} ` +
    `preservedContent=${ckpt.preservedContentLen || 0} discardedContent=${_discContent}`,
    "info");

  // ── Streaming UI: reuse the existing bubble, show "Continuing…" ──
  if (activeConvId === conv.id) {
    renderChat(conv, false);
    const lastIdx = conv.messages.length - 1;
    const msgEl = document.getElementById(`msg-${lastIdx}`);
    if (msgEl) {
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
        updateStreamingUI(assistantMsg);
      }
    }
    scrollToBottom();
  }

  conv.activeTaskId = taskId;
  saveConversations(conv.id);
  // No re-sync — /api/chat/continue already persisted both the rolled-back
  // state AND activeTaskId; a second PUT would race the streaming task.
  connectToTask(conv.id, taskId);
}

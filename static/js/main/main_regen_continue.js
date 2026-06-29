/* ═══════════════════════════════════════════════════════════════════
   main regen continue — extracted from main.js (split 2026-05-28)

   Regenerate / continue: regenerateFromUser, continueAssistant, _buildToolHistoryRound.

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
    if (_regenWillTranslate) {
      _removeTranslatingBubble();
      conv._translating = false;
      conv._translateAborted = false;
      conv._translateAbortCtrl = null;
      updateSendButton();
      renderConversationList();
    }
  }
}

// ══════════════════════════════════════════════════════
//  ★ Continue: resume an interrupted assistant response
//
//  Checkpoint-based continuation:
//  1. Find the latest recoverable checkpoint:
//     - If there are complete tool rounds → checkpoint = end of last
//       complete tool batch.  Discard partial content/thinking after that
//       point and let the LLM regenerate from the tool results.
//     - If no tool rounds → no recoverable checkpoint → full regeneration.
//  2. Roll back toolRounds, content, and thinking to the checkpoint.
//     The user sees only the preserved tool rounds; any discarded partial
//     text is removed from the message before the request is sent.
//  3. Backend receives the same message structure as a normal request
//     with toolHistory injected, and generates a FRESH response.
//  4. No prefix concatenation — the new LLM output IS the message.
// ══════════════════════════════════════════════════════
async function continueAssistant() {
  const conv = getActiveConv();
  if (!conv || activeStreams.has(conv.id) || conv.activeTaskId) return;
  const assistantMsg = conv.messages[conv.messages.length - 1];
  if (!assistantMsg || assistantMsg.role !== "assistant") return;
  if (!assistantMsg.content && !assistantMsg.thinking) {
    // Nothing to continue — message is empty, just regenerate
    conv.messages.pop();
    /* ★ FIX: clear _needsLoad and _serverMsgCount after pop — same reason as regenerateFromUser */
    conv._needsLoad = false;
    conv._serverMsgCount = conv.messages.length;
    await syncConversationToServer(conv, { allowTruncate: true });
    await startAssistantResponse(conv.id);
    return;
  }

  // ═══════════════════════════════════════════════════════════
  // ★ Step 1: Find the latest recoverable checkpoint
  //   Scan toolRounds to find complete tool batches.
  //   A "complete" round has toolCallId, status==="done", and toolContent.
  // ═══════════════════════════════════════════════════════════
  const allRounds = getToolRoundsFromMsg(assistantMsg);
  let toolHistory = [];
  let lastCompleteIdx = -1;  // index in allRounds of last complete entry

  if (allRounds.length > 0) {
    const hasToolCallIds = allRounds.some((r) => r.toolCallId);
    if (hasToolCallIds) {
      const hasLlmRound = allRounds.some((r) => r.llmRound != null);
      const batches = new Map(); // batchKey → [entries]
      let batchKey = 0;
      // Track which batch each round belongs to for rollback
      const roundBatchMap = []; // index → batchKey

      for (let i = 0; i < allRounds.length; i++) {
        const r = allRounds[i];
        if (!r.toolCallId) { roundBatchMap.push(-1); continue; }

        // Is this round complete?
        // ★ FIX: After page refresh, toolContent may be lost from DB
        //   (race: frontend sync overwrote backend's richer checkpoint).
        //   If status==="done" and we have results metadata, treat as
        //   recoverable — reconstruct toolContent from results for toolHistory.
        if (r.status !== "done") {
          debugLog(
            `Tool round #${r.roundNum} (${r.toolName}) not done (status=${r.status}) — checkpoint before it`,
            "warn",
          );
          break;
        }
        if (r.toolContent == null) {
          // Try to reconstruct from results metadata (available after DB round-trip)
          if (r.results && r.results.length > 0) {
            const reconstructed = r.results.map(res =>
              res.snippet || res.title || res.content || ''
            ).filter(Boolean).join('\n') || '[tool result not available]';
            r.toolContent = reconstructed;
            debugLog(
              `Tool round #${r.roundNum} (${r.toolName}) missing toolContent — reconstructed ${reconstructed.length} chars from results`,
              "warn",
            );
          } else {
            debugLog(
              `Tool round #${r.roundNum} (${r.toolName}) missing toolContent and no results — checkpoint before it`,
              "warn",
            );
            break;
          }
        }

        // Determine batch key
        if (hasLlmRound) {
          batchKey = r.llmRound;
        } else {
          const prev = i > 0 ? allRounds[i - 1] : null;
          if (prev && prev.toolCallId && r.roundNum > prev.roundNum + 1) {
            batchKey++;
          }
        }

        if (!batches.has(batchKey)) batches.set(batchKey, []);
        batches.get(batchKey).push(r);
        roundBatchMap.push(batchKey);
        lastCompleteIdx = i;
      }

      // Convert complete batches to toolHistory
      for (const [, batch] of batches) {
        toolHistory.push(_buildToolHistoryRound(batch));
      }
    }
  }

  // ═══════════════════════════════════════════════════════════
  // ★ Step 2: If no checkpoint, fall back to full regeneration
  // ═══════════════════════════════════════════════════════════
  if (toolHistory.length === 0) {
    // No recoverable tool checkpoint — pop the incomplete assistant message
    // and regenerate from scratch (same as clicking "Regenerate").
    debugLog(
      "Continue: no tool checkpoint found — falling back to full regeneration",
      "info",
    );
    showToast(
      "无法续接（无工具调用检查点），将重新生成回复",
      "info",
    );
    conv.messages.pop();
    /* ★ FIX: clear _needsLoad and _serverMsgCount after pop — same reason as regenerateFromUser */
    conv._needsLoad = false;
    conv._serverMsgCount = conv.messages.length;
    if (activeConvId === conv.id) renderChat(conv, false);
    await syncConversationToServer(conv, { allowTruncate: true });
    await startAssistantResponse(conv.id);
    return;
  }

  // ═══════════════════════════════════════════════════════════
  // ★ Step 3: Roll back to checkpoint
  //   - Keep only complete tool rounds in toolRounds
  //   - Discard partial content and thinking (the LLM will regenerate)
  //   - The user sees the tool call history preserved, text regenerated
  // ═══════════════════════════════════════════════════════════
  const keptRounds = allRounds.slice(0, lastCompleteIdx + 1);
  const discardedRounds = allRounds.length - keptRounds.length;

  // ★ FIX: Reconstruct content prefix from completed rounds' assistantContent
  //   instead of wiping everything to "".  Each kept round's assistantContent
  //   is the text the LLM wrote alongside that tool call batch — preserving it
  //   means the user doesn't lose visible output from successful prior rounds.
  let preservedContent = keptRounds
    .map(r => r.assistantContent || "")
    .filter(c => c)
    .join("\n\n");
  const originalContent = assistantMsg.content || "";
  // ★ FIX: After page refresh, assistantContent may be missing from rounds
  //   (backend checkpoint race).  If preservedContent is empty but we have
  //   keptRounds, use the original content up to a reasonable boundary
  //   as the prefix — the LLM only needs to regenerate from the checkpoint.
  if (!preservedContent && keptRounds.length > 0 && originalContent) {
    // Use the full original content as prefix — the backend will inject
    // it via contentPrefix and the LLM will continue from there.
    preservedContent = originalContent;
    debugLog(
      `Continue: assistantContent missing from rounds, using full original content (${originalContent.length} chars) as prefix`,
      "warn",
    );
  }
  const discardedContent = Math.max(0, originalContent.length - preservedContent.length);
  // ★ Preserved thinking = the per-round thinking fields on kept rounds
  //   (tool_dispatch stores them at capture time).  Anything on
  //   assistantMsg.thinking ABOVE that sum belongs to the interrupted
  //   tail and must be discarded (no signature = Claude would reject it).
  const preservedThinkingChars = keptRounds.reduce(
    (n, r) => n + ((r.thinking || "").length),
    0,
  );
  const discardedThinking = Math.max(
    0,
    (assistantMsg.thinking || "").length - preservedThinkingChars,
  );

  // ★ Stash the trailing message-level thinking as a display-only
  //   field BEFORE we clear the live thinking slot.  The new task is
  //   about to start streaming fresh thinking into `assistantMsg.thinking`,
  //   so anything we want to keep visible has to be moved aside now.
  //   `priorThinking` is intentionally NOT in lib/llm_sanitize.py's
  //   _API_MESSAGE_FIELDS — _strip_non_api_fields drops it before any
  //   LLM call, so it can never feed back into the API replay path.
  const _originalThinking = assistantMsg.thinking || "";
  if (discardedThinking > 0 && _originalThinking) {
    assistantMsg.priorThinking = _originalThinking;
  }
  // else: leave any existing priorThinking from a prior Continue in place —
  // streaming this turn produced no extra trailing thinking to overwrite it.

  // ★ Stash the discarded prose tail as a display-only `priorContent` field
  //   — same rationale + wire-safety contract as `priorThinking` (NOT in
  //   lib/llm_sanitize._API_MESSAGE_FIELDS, so _strip_non_api_fields drops it
  //   before any LLM call).  Without this, the rolled-back text vanished
  //   silently while the tool panel stayed put — the inconsistency the user
  //   reported ("ptool panel unchanged while the content area just
  //   disappears").  Surfacing it as a collapsed "Earlier Response" block
  //   makes the rollback honest and visible.
  if (discardedContent > 0 && originalContent) {
    // Prefer the clean discarded tail when preservedContent is a true prefix;
    // otherwise (reconstructed-from-rounds case where the two diverge) keep
    // the full original so nothing the model wrote is lost from view.
    assistantMsg.priorContent = originalContent.startsWith(preservedContent)
      ? originalContent.slice(preservedContent.length).replace(/^\n+/, '')
      : originalContent;
  }
  // else: nothing was dropped (or preserved === original) — no prior block.

  assistantMsg.toolRounds = keptRounds;
  assistantMsg.content = preservedContent;
  // NB: assistantMsg.thinking is cleared here — any thinking we want to
  // replay is already stored per-round on keptRounds[i].thinking and will
  // be sent forward via cfgPayload.toolHistory[].thinking.
  assistantMsg.thinking = "";
  // ★ Save the prefix so state/delta handlers can merge correctly
  if (preservedContent) {
    assistantMsg._continueContentPrefix = preservedContent;
  }
  // Clear stale metadata that will be refreshed by the new generation
  delete assistantMsg.finishReason;
  delete assistantMsg.toolSummary;
  delete assistantMsg.error;

  debugLog(
    `Continue checkpoint: keeping ${keptRounds.length} tool entries ` +
    `(preserved ${preservedContent.length} chars content + ` +
    `${preservedThinkingChars} chars thinking from completed rounds), ` +
    `discarded ${discardedRounds} incomplete rounds + ` +
    `${discardedContent} chars new content + ${discardedThinking} chars thinking`,
    "info",
  );
  if (discardedRounds > 0 || discardedContent > 0 || discardedThinking > 0) {
    const preserveParts = [];
    if (preservedContent.length > 0) preserveParts.push(`${preservedContent.length} 字符内容`);
    if (preservedThinkingChars > 0) preserveParts.push(`${preservedThinkingChars} 字符思考内容`);
    const preserveNote = preserveParts.length > 0
      ? ` (保留了 ${preserveParts.join(" + ")})`
      : '';
    const discardParts = [];
    if (discardedContent > 0) discardParts.push(`${discardedContent} 字符后续文本`);
    if (discardedThinking > 0) discardParts.push(`${discardedThinking} 字符思考`);
    if (discardedRounds > 0) discardParts.push(`${discardedRounds} 个未完成工具调用`);
    const discardNote = discardParts.length > 0
      ? `，丢弃了 ${discardParts.join(" + ")}`
      : '';
    showToast(
      `从第 ${keptRounds.length} 轮工具调用后恢复${preserveNote}${discardNote}`,
      "info",
    );
  }

  // Save pre-checkpoint apiRounds & usage for merging after completion
  assistantMsg._continueApiRounds = (assistantMsg.apiRounds || []).slice();
  if (assistantMsg.usage)
    assistantMsg._continueUsage = { ...assistantMsg.usage };
  // Save the checkpoint toolRounds so we can merge with new ones
  assistantMsg._continueToolRounds = keptRounds.slice();
  // ★ Save modifiedFiles/modifiedFileList for merging after completion
  if (assistantMsg.modifiedFiles)
    assistantMsg._continueModifiedFiles = assistantMsg.modifiedFiles;
  if (assistantMsg.modifiedFileList)
    assistantMsg._continueModifiedFileList = (assistantMsg.modifiedFileList || []).slice();

  // ═══════════════════════════════════════════════════════════
  // ★ Step 4: Build messages — EXCLUDE the trailing assistant message
  // ═══════════════════════════════════════════════════════════
  // ★ Server-side message building: no longer send messages, backend loads from DB

  // ═══════════════════════════════════════════════════════════
  // ★ Step 5: Set up streaming UI
  // ═══════════════════════════════════════════════════════════
  if (activeConvId === conv.id) {
    // Re-render to show cleaned-up state (tool rounds only, no content)
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
        bodyEl.innerHTML =
          '<div data-zone="tool"></div><div data-zone="thinking"></div><div data-zone="content"></div><div data-zone="status"><div class="stream-status"><div class="pulse"></div> Continuing…</div></div>';
        updateStreamingUI(assistantMsg);
      }
    }
    scrollToBottom();
  }

  // ═══════════════════════════════════════════════════════════
  // ★ Step 6: Build config payload
  //   Use _buildConvConfig() to get per-conv settings, avoiding the
  //   cross-talk bug where globals from the active conv leak into a
  //   background conv's continue request.
  //
  //   ★ Checkpoint assembly moved to the server (/api/chat/continue).
  //   The server-side _scan_continue_checkpoint() ports this same logic
  //   from the DB's assistant message — so we no longer ship
  //   `toolHistory`, `contentPrefix`, `checkpointToolRounds`,
  //   `checkpointUsage`, `checkpointApiRounds`, `checkpointModifiedFiles`
  //   or `checkpointModifiedFileList` on the wire.  The `_continueXxx`
  //   fields on `assistantMsg` stay local and are consumed by the SSE
  //   stream handlers to merge prior rounds with newly-streamed ones.
  // ═══════════════════════════════════════════════════════════
  const cfgPayload = await _buildConvConfig(conv);
  // ★ Continue reuses the SAME assistant message (it already has a stable
  //   _msgId — server UUID or a tmp_ id). Ship it so the backend stamps
  //   task['_assistantMsgId'] → live per-round translation frames route to this
  //   still-streaming bubble (which carries data-msg-id from the renderChat
  //   above via renderMessage). _ensureMsgId guarantees the id exists.
  if (typeof _ensureMsgId === 'function') _ensureMsgId(assistantMsg);
  if (assistantMsg._msgId) cfgPayload.assistantMsgId = assistantMsg._msgId;
  debugLog(
    `Continue: delegating to /api/chat/continue with ${keptRounds.length} kept ` +
    `round(s), preservedContent=${preservedContent.length} chars`,
    "info",
  );

  // ★ Sync to server BEFORE POST so backend can load messages from DB.
  //   Note: the server's /api/chat/continue endpoint will itself roll back
  //   the DB state to the checkpoint before starting the task — but we
  //   still sync first so the server sees any local-only edits/attachments.
  await syncConversationToServer(conv);
  let taskId;
  try {
    const res = await Api.chat.continue({
      convId: conv.id,
      config: cfgPayload,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Request failed");
    // ★ Server may tell us to fall back to a plain regenerate (no checkpoint
    //   available — e.g. all rounds incomplete). Honor it.
    if (data.fallback === 'regenerate') {
      debugLog(
        `Continue: server reports no recoverable checkpoint (${data.reason}); ` +
        `falling back to full regeneration`,
        "info",
      );
      // Undo the local rollback: restore content/thinking/rounds, pop the
      // trailing assistant and re-run the normal regenerate path.
      assistantMsg.content = originalContent;
      assistantMsg.thinking = _originalThinking;
      assistantMsg.toolRounds = allRounds;
      delete assistantMsg.priorThinking;
      delete assistantMsg.priorContent;
      delete assistantMsg._continueToolRounds;
      delete assistantMsg._continueContentPrefix;
      delete assistantMsg._continueApiRounds;
      delete assistantMsg._continueUsage;
      delete assistantMsg._continueModifiedFiles;
      delete assistantMsg._continueModifiedFileList;
      conv.messages.pop();
      conv._needsLoad = false;
      conv._serverMsgCount = conv.messages.length;
      if (activeConvId === conv.id) renderChat(conv, false);
      await syncConversationToServer(conv, { allowTruncate: true });
      await startAssistantResponse(conv.id);
      return;
    }
    taskId = data.taskId;
    if (data.checkpoint) {
      debugLog(
        `Continue: server checkpoint summary kept=${data.checkpoint.keptRounds} ` +
        `discarded=${data.checkpoint.discardedRounds} ` +
        `preservedContent=${data.checkpoint.preservedContentLen} ` +
        `discardedContent=${data.checkpoint.discardedContentLen}`,
        "info",
      );
    }
  } catch (e) {
    debugLog("Continue failed: " + e.message, "error");
    return;
  }
  conv.activeTaskId = taskId;
  saveConversations(conv.id);
  // No need to re-sync here — /api/chat/continue already persisted both the
  // rolled-back state AND the activeTaskId. A second PUT would just race
  // with the streaming task's checkpoints.
  connectToTask(conv.id, taskId);
}

/**
 * ★ Build a single tool history round from a batch of toolRound entries.
 * Each round represents one assistant message with tool_calls + their results.
 *
 * Optional per-provider continuity fields propagated through to the backend
 * when present — the Python backend (lib/tasks_pkg/message_builder.py) gates
 * them on the target model's actual API capability:
 *   • assistantContent   — text emitted alongside the tool calls
 *   • thinking           — reasoning trace (Claude extended-thinking)
 *   • thinkingSignature  — opaque signature for the thinking block (Claude)
 *   • toolCalls[i].extraContent — Gemini thought_signature envelope
 * Old DB rows without these fields round-trip harmlessly as plain calls.
 */

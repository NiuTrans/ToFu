/* ═══════════════════════════════════════════════════════════════════
   edit message — extracted from ui.js (split 2026-05-28)

   Edit-message flow: edit-and-resend, edit-only.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ── Edit messages ──
// ── Backup of main input state while editing ──
let _editBackupImages = [];
let _editBackupPdfs = [];
let _editBackupInput = "";

function startEditMessage(idx) {
  const conv = getActiveConv();
  if (!conv || activeStreams.has(conv.id) || conv.activeTaskId) return;
  const msg = conv.messages[idx];
  if (!msg) return;
  const msgEl = document.getElementById("msg-" + idx);
  if (!msgEl) return;
  _editingMsgIdx = idx;
  /* Only a REAL human user turn supports Save & Resend (truncate + regenerate)
   * and file attachments. Every other lane (assistant / autopilot VU / critic /
   * planner) is EDIT-IN-PLACE only: a Save button that PATCHes content, no
   * resend, no attachment tray. `_isVirtualUser` is a machine-authored user
   * turn, so it takes the edit-in-place path too. */
  const _isHumanUser = msg.role === "user" && !msg._isVirtualUser;
  // ★ Backup current shared input state, then load message's attachments
  //   (human-user only — an in-place edit never touches the shared input tray).
  if (_isHumanUser) {
    _editBackupImages = [...pendingImages];
    _editBackupPdfs = [...pendingPdfTexts];
    const mainInput = document.getElementById("userInput");
    _editBackupInput = mainInput ? mainInput.value : "";
    // Load message's existing attachments into the shared state
    pendingImages = [...(msg.images || [])];
    pendingPdfTexts = [...(msg.pdfTexts || [])];
    // Clear any pending reply quote in the input area
    if (typeof clearReplyQuote === "function") clearReplyQuote();
  }
  const bodyEl = msgEl.querySelector(".message-body");
  const _previewTray = _isHumanUser
    ? `<div class="image-previews" id="editImagePreviews"></div>` : "";
  const _resendBtn = _isHumanUser
    ? `<button class="edit-resend-btn" onclick="saveEditAndResend(${idx})">${t('editMsg.resend')}</button>` : "";
  const _hint = _isHumanUser
    ? t('editMsg.hintHuman')
    : t('editMsg.hintInPlace');
  bodyEl.innerHTML = `<div class="edit-area">${_previewTray}<textarea class="edit-textarea" id="edit-textarea-${idx}"></textarea><div class="edit-actions"><button class="edit-cancel-btn" onclick="cancelEditMessage(${idx})">${t('editMsg.cancel')}</button><button class="edit-save-btn" onclick="saveEditOnly(${idx})">${t('editMsg.save')}</button>${_resendBtn}</div><div class="edit-hint">${_hint}</div></div>`;
  // ★ Render AFTER DOM is built so #editImagePreviews exists (human-user only).
  if (_isHumanUser) renderImagePreviews();
  const ta = document.getElementById("edit-textarea-" + idx);
  if (ta) {
    ta.value = msg.originalContent || msg.content || "";
    ta.focus();
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 300) + "px";
    ta.addEventListener("input", function () {
      this.style.height = "auto";
      this.style.height = Math.min(this.scrollHeight, 300) + "px";
      if (typeof _pendingLogClean !== 'undefined' && _pendingLogClean &&
          !this.value.includes(_pendingLogClean.originalText)) {
        if (typeof hideLogCleanBanner === 'function') hideLogCleanBanner();
      }
    });
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Escape") {
        e.preventDefault();
        cancelEditMessage(idx);
      }
      // Ctrl+Shift+K — wrap selected text in <notranslate> tags
      if (e.key === "K" && e.ctrlKey && e.shiftKey) {
        e.preventDefault();
        if (typeof _wrapSelectionNoTranslate === 'function') _wrapSelectionNoTranslate(this);
      }
    });
    // ── Paste handler: reuse shared input path (same as #userInput) ──
    ta.addEventListener("paste", async (e) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      let hasImage = false;
      // Image attachments belong to the human-user editor only (it owns the
      // #editImagePreviews tray + shared pendingImages state). An in-place
      // edit of another lane has no tray — don't clobber the user's live input.
      const _hasTray = !!document.getElementById("editImagePreviews");
      for (const item of items) {
        if (_hasTray && item.type.startsWith("image/")) {
          e.preventDefault();
          hasImage = true;
          const f = item.getAsFile();
          const d = await processImageFile(f);
          pendingImages.push(d);
          renderImagePreviews();
        }
      }
      if (!hasImage && typeof detectLogNoise === 'function') {
        const pastedText = e.clipboardData?.getData("text");
        if (pastedText && pastedText.length > 200) {
          setTimeout(async () => {
            const result = await detectLogNoise(ta.value);
            if (result && typeof showLogCleanBanner === 'function') showLogCleanBanner(result);
            else if (typeof hideLogCleanBanner === 'function') hideLogCleanBanner();
          }, 50);
        }
      }
    });
  }
  const act = msgEl.querySelector(".message-actions");
  if (act) act.style.display = "none";
}

/** Restore shared input state from backup after edit completes or cancels. */
function _restoreInputFromBackup() {
  pendingImages = _editBackupImages;
  pendingPdfTexts = _editBackupPdfs;
  _editBackupImages = [];
  _editBackupPdfs = [];
  renderImagePreviews();
  if (typeof _vlmSaveState === 'function') _vlmSaveState();  // ★ Persist restored state
  const mainInput = document.getElementById("userInput");
  if (mainInput) mainInput.value = _editBackupInput;
  _editBackupInput = "";
}

function cancelEditMessage(idx) {
  _editingMsgIdx = null;
  _restoreInputFromBackup();
  // Dismiss log clean banner if it was shown for the edit textarea
  if (typeof _pendingLogClean !== 'undefined' && _pendingLogClean &&
      typeof hideLogCleanBanner === 'function') hideLogCleanBanner();
  const conv = getActiveConv();
  if (!conv) return;
  /* ★ FIX: Surgical single-message restore instead of full renderChat().
   * renderChat() without forceScroll=false does a full innerHTML wipe +
   * _forceScrollToBottom, which causes the page to jump to the bottom.
   * Since cancel doesn't change message data, we only need to restore
   * the original message DOM for the edited element. */
  const msgEl = document.getElementById("msg-" + idx);
  if (msgEl && conv.messages[idx]) {
    window.ConvView.apply(conv.id, idx, conv.messages[idx]);
  } else {
    window.ConvView.replaceAll(conv.id);
  }
}
function saveEditOnly(idx) {
  _editingMsgIdx = null;
  // Auto-apply log clean if banner is showing
  if (typeof _pendingLogClean !== 'undefined' && _pendingLogClean) {
    const editTa = document.getElementById("edit-textarea-" + idx);
    if (editTa) editTa.value = editTa.value.replace(_pendingLogClean.originalText, _pendingLogClean.cleanedText);
    if (typeof hideLogCleanBanner === 'function') hideLogCleanBanner();
  }
  const conv = getActiveConv();
  if (!conv) return;
  const ta = document.getElementById("edit-textarea-" + idx);
  if (!ta) return;
  const t = ta.value.trim();
  const msg = conv.messages[idx];
  const _isHumanUser = msg.role === "user" && !msg._isVirtualUser;
  // ★ Attachments + shared-input restore apply ONLY to the human-user editor;
  //   an in-place edit of any other lane (assistant / autopilot VU / critic /
  //   planner) never opened the attachment tray, so touching pendingImages or
  //   calling _restoreInputFromBackup would clobber the user's live input tray.
  if (_isHumanUser) {
    // ★ Collect attachments from shared state (skip still-parsing PDFs)
    msg.images = [...pendingImages];
    msg.pdfTexts = pendingPdfTexts.filter(p => p.method !== "parsing");
    // ★ Restore main input state from backup
    _restoreInputFromBackup();
  }
  if (!t && !(msg.images?.length > 0) && !(msg.pdfTexts?.length > 0)) return;
  // ★ Always set content to edited text first
  msg.content = t;
  // ★ Use per-conv autoTranslate (not global) — matches sendMessage behavior
  const _convAutoTranslate = convAutoTranslate(conv);
  // ★ Autopilot (virtual-user) / endpoint-critic messages are role=user but
  //   DISPLAY-translated: `content` = the model-language original (shown in the
  //   原文 toggle), `translatedContent` = the UI-language rendering shown in the
  //   OUTER bubble. This is the OPPOSITE wiring from a normal user message
  //   (originalContent=源文, content=English-for-model). Editing `content`
  //   invalidates the cached `translatedContent`, so clear it and re-translate
  //   the edited content INTO translatedContent — otherwise the edit only lands
  //   in the toggle while the outer 译文 keeps showing the stale translation
  //   (the reported bug).
  const _isVuOrCritic = !!(msg._isVirtualUser || msg._isEndpointReview);
  if (_isVuOrCritic) {
    if (typeof _resetTranslationState === 'function') _resetTranslationState(msg);
    if (_convAutoTranslate && t) {
      _runTranslationPipeline(conv, idx, msg, {
        sourceLang: '',
        targetLang: 'Chinese',
        field: 'translatedContent',
        mode: 'auto',
        text: t,
      });
    }
  } else if (_convAutoTranslate && t) {
    // ★ Normal user message: detect Chinese and translate content→English
    //   (the model's strongest language) via the unified pipeline.
    const hasChinese = /[\u4e00-\u9fff\u3400-\u4dbf]/.test(t);
    if (hasChinese) {
      msg.originalContent = t;
      _runTranslationPipeline(conv, idx, msg, {
        sourceLang: 'Chinese',
        targetLang: 'English',
        field: 'content',
        mode: 'auto',
        text: t,
      });
    }
  }
  // Reply quotes and conv refs: keep as-is
  if (msg.replyQuote && !msg.replyQuotes) {
    msg.replyQuotes = [msg.replyQuote];
    delete msg.replyQuote;
  }
  saveConversations(conv.id);
  // ★ Targeted single-message PATCH — replaces the full-conversation PUT that
  //   ``syncConversationToServerDebounced(conv)`` would have issued, so a mere
  //   edit-only action only mutates the one message on the server side.
  //   Whitelisted keys only (see ``_PATCH_MSG_WHITELIST`` in
  //   ``routes/conversations.py``). On server error we revert to the
  //   pre-edit content from the DOM and show a toast.
  (async () => {
    const _prevSnapshot = {
      content: msg.content,
      originalContent: msg.originalContent,
      images: msg.images,
      pdfTexts: msg.pdfTexts,
      replyQuotes: msg.replyQuotes,
      timestamp: msg.timestamp,
    };
    const _patch = {
      content: msg.content,
      // null removes the key server-side when no longer present locally.
      originalContent: msg.originalContent === undefined ? null : msg.originalContent,
      images: msg.images || [],
      pdfTexts: msg.pdfTexts || [],
      replyQuotes: msg.replyQuotes || [],
    };
    if (msg.timestamp) _patch.timestamp = msg.timestamp;
    // ★ VU/critic edit: the cached display translation was just reset (and a
    //   fresh one is being computed). Explicitly clear it server-side (null →
    //   key removal) so a later sync / reload can't resurrect the stale 译文.
    if (_isVuOrCritic) {
      _patch.translatedContent = (msg.translatedContent === undefined) ? null : msg.translatedContent;
      _patch._translatedCache = (msg._translatedCache === undefined) ? null : msg._translatedCache;
    }
    const _convIdLocal = conv.id;
    const _res = await _patchMessageOnServer(_convIdLocal, idx, _patch, {
      onError: () => {
        // Revert local state and show toast
        const _c = conversations.find(c => c.id === _convIdLocal);
        if (_c && _c.messages[idx]) {
          Object.assign(_c.messages[idx], _prevSnapshot);
          saveConversations(_convIdLocal);
          if (activeConvId === _convIdLocal) {
            const _el = document.getElementById('msg-' + idx);
            if (_el) window.ConvView.apply(_convIdLocal, idx, _c.messages[idx]);
          }
        }
        if (typeof showToast === 'function') showToast('Edit failed — reverted', 'error');
      },
    });
    if (_res && _res.ok) {
      debugLog(`[saveEditOnly] PATCH ok conv=${_convIdLocal.slice(0, 8)} idx=${idx} msgCount=${_res.msgCount}`, 'debug');
    }
  })();
  /* ★ FIX: Surgical single-message update instead of full renderChat().
   * renderChat() without forceScroll=false does a full innerHTML wipe +
   * _forceScrollToBottom, which causes the page to jump to the bottom.
   * Since saveEditOnly only changes one message, replace just that element. */
  const msgEl = document.getElementById("msg-" + idx);
  if (msgEl) {
    window.ConvView.apply(conv.id, idx, msg);
  } else {
    window.ConvView.replaceAll(conv.id);
  }
}
async function saveEditAndResend(idx) {
  _editingMsgIdx = null;
  // Auto-apply log clean if banner is showing
  if (typeof _pendingLogClean !== 'undefined' && _pendingLogClean) {
    const editTa = document.getElementById("edit-textarea-" + idx);
    if (editTa) editTa.value = editTa.value.replace(_pendingLogClean.originalText, _pendingLogClean.cleanedText);
    if (typeof hideLogCleanBanner === 'function') hideLogCleanBanner();
  }
  const conv = getActiveConv();
  if (!conv) return;
  // ★ SyncFix: previously this early-returned silently when a stream/task was
  //   in flight, leaving the user thinking nothing happened (common right after
  //   clicking Stop while abort was still propagating). Instead, synchronously
  //   hard-cancel the stale task so the edit flow can proceed.
  if (activeStreams.has(conv.id) || conv.activeTaskId) {
    console.info(`[SyncFix] saveEditAndResend hard-cancelling racing stream — conv=${conv.id.slice(0,8)} activeTaskId=${conv.activeTaskId?.slice(0,8)||'null'}`);
    _hardCancelActiveStream(conv);
  }
  const ta = document.getElementById("edit-textarea-" + idx);
  if (!ta) return;
  const t = ta.value.trim();
  const msg = conv.messages[idx];
  // ★ Collect attachments from shared state (skip still-parsing PDFs)
  const editedImages = [...pendingImages];
  const editedPdfTexts = pendingPdfTexts.filter(p => p.method !== "parsing");
  // ★ Restore main input state from backup
  _restoreInputFromBackup();
  if (!t && !(editedImages.length > 0) && !(editedPdfTexts.length > 0)) return;

  // ── Image Gen mode intercept: re-run via direct image API ──
  //   Without this, edits on image-gen messages would fall through to
  //   ``/api/chat/regenerate`` and be executed as a normal text chat task —
  //   mirrors the intercept in ``regenerateFromUser`` (main.js).
  const _isIgConv = (typeof imageGenMode !== 'undefined' && imageGenMode) || conv.imageGenMode;
  const _isIgMsg  = msg._isImageGen || (msg.content && msg.content.startsWith('🎨 '));
  if (_isIgConv || _isIgMsg) {
    console.info(`[ImageGen] saveEditAndResend intercept — conv=${conv.id.slice(0,8)} idx=${idx} images=${editedImages.length}`);
    // Apply the user's edits, then truncate to BEFORE this message
    //   (generateImageDirect will re-push the user msg itself).
    msg.content = t;
    msg.images = editedImages;
    msg.pdfTexts = editedPdfTexts;
    delete msg.originalContent;
    msg.timestamp = Date.now();
    conv.messages = conv.messages.slice(0, idx);
    saveConversations(conv.id);
    window.ConvView.replaceAll(conv.id);
    if (typeof renderConversationList === 'function') renderConversationList();
    // Seed textarea + pendingImages so generateImageDirect picks them up.
    let prompt = t || '';
    if (prompt.startsWith('🎨 ')) prompt = prompt.slice(2).trim();
    const textarea = document.getElementById('userInput');
    if (textarea) { textarea.value = prompt; }
    // Edited images become source images for the next edit/generation.
    if (typeof pendingImages !== 'undefined') {
      pendingImages = editedImages.slice();
      if (typeof renderImagePreviews === 'function') renderImagePreviews();
    }
    if (typeof imageGenMode !== 'undefined' && !imageGenMode &&
        typeof _applyImageGenUI === 'function') _applyImageGenUI(true);
    if (typeof generateImageDirect === 'function') generateImageDirect();
    return;
  }

  // ── Wait for VLM parsing to complete before sending ──
  if (editedPdfTexts.length > 0 && typeof _waitForVlmParsing === 'function') {
    const _tempMsg = { pdfTexts: editedPdfTexts };
    await _waitForVlmParsing(_tempMsg, conv.id, idx);
  }

  const convId = conv.id;

  // ── Optimistic UI: truncate local messages and re-render edited message ──
  msg.content = t;
  msg.images = editedImages;
  msg.pdfTexts = editedPdfTexts;
  delete msg.originalContent;
  // ★ Autopilot (VU) / critic messages render `translatedContent` in the OUTER
  //   bubble (see chat_render.js). Editing `content` invalidates that cached
  //   display translation — clear it so the outer 译文 doesn't show the stale
  //   pre-edit text. The backend re-translates the edited content for display
  //   (see routes/chat.py regenerate VU branch).
  if (msg._isVirtualUser || msg._isEndpointReview) {
    if (typeof _resetTranslationState === 'function') _resetTranslationState(msg);
  }
  msg.timestamp = Date.now();
  conv.messages = conv.messages.slice(0, idx + 1);
  conv._needsLoad = false;
  conv._serverMsgCount = conv.messages.length;

  if (conv.messages.filter((m) => m.role === "user").length === 1 && t) {
    const titleSource = stripNoTranslateTags(t);
    conv.title = titleSource.slice(0, 60) + (titleSource.length > 60 ? "..." : "");
    document.getElementById("topbarTitle").textContent = conv.title;
  }

  /* ── Surgical DOM truncation (re-render edited msg + remove later ones) ── */
  if (activeConvId === convId) {
    const editedEl = document.getElementById("msg-" + idx);
    if (editedEl) window.ConvView.apply(convId, idx, msg);
  }
  const _syncMsgsBefore = conv.messages.length;
  /* Funnel through unified controller — falls back to renderChat()
   * internally when the surgical path can't apply. */
  window.ConvView.removeAfter(convId, idx);
  renderConversationList();
  // ★ SyncFix: invariant check — no stale streaming bubble or ghost msg-N
  //   should survive the truncation. Assertion is a no-op in production when
  //   console.assert is a no-op, but makes races obvious in dev.
  console.assert(
    !document.getElementById("streaming-msg"),
    `[SyncFix] Stale streaming-msg after saveEditAndResend truncate — conv=${convId.slice(0,8)}`
  );
  console.info(`[SyncFix] saveEditAndResend conv=${convId.slice(0,8)} idx=${idx} msgsBefore=${_syncMsgsBefore} msgsAfter=${conv.messages.length} hasStreamingMsg=${!!document.getElementById('streaming-msg')} activeTaskId=${conv.activeTaskId?.slice(0,8)||'null'}`);

  // ★ SyncFix: persist truncated/edited state BEFORE /api/chat/regenerate fires,
  //   so a page refresh during the "Waiting" window (fetch in flight) doesn't
  //   resurrect the original unedited question. Without this, DB+IDB still have
  //   the pre-edit messages, and on refresh Case E would auto-regenerate the
  //   original user message, or Case A/B would reconnect to the old task.
  //   Update IDB cache first (Phase 1 render on refresh), then server DB.
  try { if (typeof ConvCache !== 'undefined') ConvCache.put(conv); }
  catch (e) { console.warn('[SyncFix] ConvCache.put failed:', e); }
  try {
    await syncConversationToServer(conv, { allowTruncate: true });
    console.info(`[SyncFix] saveEditAndResend pre-regenerate sync OK — conv=${convId.slice(0,8)} msgs=${conv.messages.length}`);
  } catch (e) {
    console.warn(`[SyncFix] saveEditAndResend pre-regenerate sync failed: ${e.message}`);
  }

  // ── Atomic backend call: truncate + edit + translate + task start ──
  const _regenConfig = await _buildConvConfig(conv);

  // ★ Mint the assistant message id BEFORE the POST (same as send/regenerate)
  //   and ship it so the backend stamps task['_assistantMsgId'] → live
  //   per-round translation frames route to the still-streaming bubble. Without
  //   this, edit-and-resend silently lost the live preview and translated only
  //   at completion.
  const _editAssistantMsgId = (typeof _newClientMsgId === 'function')
    ? _newClientMsgId()
    : ('tmp_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8));
  _regenConfig.assistantMsgId = _editAssistantMsgId;

  // ★ If autoTranslate is on and edited text has Chinese, show stop button immediately
  const _editAbortCtrl = new AbortController();
  let _editAbortReason = '';  // '' | 'timeout' | 'user-stop'
  const _editTimeout = setTimeout(() => {
    _editAbortReason = 'timeout';
    _editAbortCtrl.abort();
  }, 90000);
  const _editWillTranslate = _regenConfig.autoTranslate && /[\u4e00-\u9fff\u3400-\u4dbf]/.test(t);
  if (_editWillTranslate) {
    conv._translating = true;
    conv._translateAborted = false;
    conv._translateAbortCtrl = _editAbortCtrl;
    updateSendButton();
    renderConversationList();
    if (activeConvId === convId) _renderTranslatingBubble();
  } else if (activeConvId === convId) {
    /* ★ Fix ①: no translation → deterministic '连接中…' placeholder so the
     *   assistant side is not blank during the synchronous /api/chat/regenerate
     *   POST. Upgraded in place to the streaming bubble on taskId.
     *   NOTE: `t` is shadowed here by the edited text (const t = ta.value.trim);
     *   use the global i18n helper via window.t. */
    _renderTranslatingBubble(window.t('sidebar.connecting'));
  }

  try {
    const _regenSettings = await _buildConvSettings(conv);
    // Refresh the per-turn context note: edit-and-resend re-runs the turn
    // with the current workspace/tools/model. See static/js/info-rail.js.
    const _editCtx = (typeof buildTurnCtxSnapshot === 'function') ? buildTurnCtxSnapshot() : null;
    if (_editCtx) msg._ctx = _editCtx; else delete msg._ctx;
    const resp = await Api.chat.regenerate({
      convId,
      truncateToIndex: idx,
      // ★ Phase 3: stable-id truncate point (authoritative server-side; the
      //   index is the fallback). Index-drift-proof if a writer reordered
      //   messages between this read and the request. Additive — omitted when
      //   the message somehow lacks an id.
      ...(msg._msgId ? { truncateToMsgId: msg._msgId } : {}),
      editedContent: t,
      editedImages,
      editedPdfTexts,
      config: _regenConfig,
      settings: _regenSettings,
      ...(_editCtx ? { ctx: _editCtx } : {}),
    }, { signal: _editAbortCtrl.signal });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(err.error || `Server ${resp.status}`);
    }
    const result = await resp.json();

    // Update local state with server response (may have translated)
    if (result.userMessage) {
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
    const assistantMsg = {
      role: "assistant", content: "", thinking: "",
      timestamp: Date.now(), toolRounds: [],
      model: _regenConfig.model || serverModel,
      _msgId: _editAssistantMsgId,
    };
    // ★ Endpoint mode: mark as planner so SSE reconnection identifies it correctly
    if (_regenConfig.endpointMode) assistantMsg._isEndpointPlanner = true;
    _ensureMsgId(assistantMsg);  // no-op when _msgId already set
    conv.messages.push(assistantMsg);
    conv.activeTaskId = taskId;
    saveConversations(convId);

    _removeTranslatingBubble();
    if (activeConvId === convId) _renderStreamingBubble(conv, _regenConfig, _editAssistantMsgId);
    buildTurnNav(conv);
    connectToTask(convId, taskId);

  } catch (e) {
    const _userClickedStop = !!conv._translateAborted;
    if (e.name === 'AbortError' && _editWillTranslate && _userClickedStop) {
      console.log('%c[saveEditAndResend] ✗ Aborted during translation by user', 'color:#f59e0b;font-weight:bold');
      _removeTranslatingBubble();
      saveConversations(convId);
      syncConversationToServer(conv, { allowTruncate: true });
      buildTurnNav(conv);
      Api.chat.abortConv(convId);
    } else if (e.name === 'AbortError' && _editAbortReason === 'timeout'
               && typeof _recoverTimedOutChatTask === 'function'
               && await _recoverTimedOutChatTask(convId, { endpointMode: _regenConfig.endpointMode })) {
      // ★ Client safety timer fired, but the server had already started the
      //   task — we reconnected to its live stream. No error, no Retry needed.
      console.log('%c[saveEditAndResend] ✓ Recovered timed-out request by reconnecting to live task', 'color:#34d399;font-weight:bold');
    } else {
      let errMsg;
      if (e.name === 'AbortError' && _editAbortReason === 'timeout') {
        errMsg = _editWillTranslate
          ? 'The server took too long to respond and the request was cancelled — it may be overloaded. Please try again; if this keeps happening with Chinese input, disabling auto-translate in Settings can reduce the delay.'
          : 'Edit+resend timed out — the server took too long to respond.';
      } else if (e.name === 'AbortError') {
        errMsg = 'Edit+resend was aborted before the server replied.';
      } else {
        errMsg = e.message;
      }
      debugLog("Edit+resend failed: " + errMsg, "error");
      console.error('[saveEditAndResend] /api/chat/regenerate failed:', e?.name, e?.message,
                    'abortReason=' + _editAbortReason);
      _removeTranslatingBubble();
      // Surface a visible error bubble so the chat doesn't sit silent.
      const errAssistant = {
        role: 'assistant', content: '', thinking: '',
        error: errMsg, timestamp: Date.now(), toolRounds: [],
      };
      if (typeof _ensureMsgId === 'function') _ensureMsgId(errAssistant);
      conv.messages.push(errAssistant);
      if (activeConvId === convId) {
        window.ConvView.apply(convId, conv.messages.length - 1, errAssistant);
      }
      saveConversations(convId);
      syncConversationToServer(conv, { allowTruncate: true });
      buildTurnNav(conv);
    }
  } finally {
    clearTimeout(_editTimeout);
    // ★ Fix ①: teardown the pre-POST placeholder unconditionally (rendered
    //   whether or not we translated). Idempotent once the success path
    //   swapped it for the streaming bubble.
    _removeTranslatingBubble();
    if (_editWillTranslate) {
      conv._translating = false;
      conv._translateAborted = false;
      conv._translateAbortCtrl = null;
      updateSendButton();
      renderConversationList();
    }
  }
}


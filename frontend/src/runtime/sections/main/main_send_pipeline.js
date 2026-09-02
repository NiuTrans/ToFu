/* ===== migrated source: main/main_send_pipeline.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   main send pipeline — extracted from main.js (split 2026-05-28)

   Send pipeline: sendMessage, autopilot followup, queue UI, queued-task reconnect.

   This file is concatenated by Vite's module graph BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Ask the human how to deliver a message SENT WHILE a turn is generating.
 * Shown ONLY when a task is already running for `convId` (caller-gated). The
 * dialog auto-closes to the safe default ('queue') if that running turn ends
 * while it's open (liveCheck), so a moot choice never lingers on screen.
 *
 * @param {string} convId
 * @returns {Promise<'steer'|'queue'>}
 */
async function _promptInjectMode(convId) {
  if (typeof showChoice !== 'function') return 'queue';  // dialog base not loaded
  const _tt = (k, d) => (typeof t === 'function' ? (t(k) !== k ? t(k) : d) : d);
  // Inline SVGs (§3.4 — no emoji). steer = pen-to-line (interject into the
  // live reply); queue = stacked lines (a fresh turn in line).
  const steerIcon =
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
  const queueIcon =
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>';
  const choice = await showChoice({
    title: _tt('inject.promptTitle', '正在生成中 — 这条消息怎么发送？'),
    options: [
      {
        value: 'steer',
        label: _tt('inject.labelSteer', '插入当前回复'),
        subtitle: _tt('inject.subSteer', '在下一个工具调用边界注入,让模型立刻看到'),
        icon: steerIcon,
        accent: true,
      },
      {
        value: 'queue',
        label: _tt('inject.labelQueue', '排到下一轮'),
        subtitle: _tt('inject.subQueue', '当前回复结束后作为新一轮自动发送'),
        icon: queueIcon,
      },
    ],
    dismissValue: 'queue',
    liveCheck: () => {
      const c = conversations.find((x) => x.id === convId);
      return Boolean(runtimeScope.ConversationTurnRead?.activeMainAttemptId?.(c));
    },
  });
  return choice === 'steer' ? 'steer' : 'queue';
}
if (typeof window !== 'undefined') runtimeScope._promptInjectMode = _promptInjectMode;

let _composerSendLocked = false;
let _retryableComposerCommand = null;

function _sameCapturedItems(left, right) {
  return Array.isArray(left) && Array.isArray(right)
    && left.length === right.length
    && left.every((item, index) => item === right[index]);
}

function _sameCapturedValues(left, right) {
  return Array.isArray(left) && Array.isArray(right)
    && left.length === right.length
    && left.every((item, index) =>
      JSON.stringify(item) === JSON.stringify(right[index]));
}

function _composerCommandId(draft) {
  const previous = _retryableComposerCommand;
  if (previous
      && previous.convId === draft.convId
      && previous.inputValue === draft.inputValue
      && _sameCapturedItems(previous.images, draft.images)
      && _sameCapturedItems(previous.pdfTexts, draft.pdfTexts)
      && _sameCapturedItems(previous.videos, draft.videos)
      && _sameCapturedValues(previous.replyQuotes, draft.replyQuotes)
      && _sameCapturedValues(previous.convRefs, draft.convRefs)) {
    return previous.commandId;
  }
  const commandId = (typeof _newClientMsgId === 'function')
    ? _newClientMsgId()
    : ('cmd_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10));
  _retryableComposerCommand = { ...draft, commandId };
  return commandId;
}

function _forgetComposerCommand(commandId) {
  if (_retryableComposerCommand?.commandId === commandId) {
    _retryableComposerCommand = null;
  }
}

function _withoutCapturedItems(current, captured) {
  const consumed = new Set(captured || []);
  return (current || []).filter((item) => !consumed.has(item));
}

/**
 * Clear exactly the captured draft from the composer. Runs IMMEDIATELY on
 * send (the optimistic echo owns the user bubble from that moment) and again
 * after acknowledgement — both passes are idempotent, so content or
 * attachments added while the request was in flight stay in the composer.
 */
function _clearCapturedComposerDraft(draft) {
  const input = document.getElementById('userInput');
  if (input && input.value === draft.inputValue) {
    input.value = '';
    input.style.height = 'auto';
  }
  pendingImages = _withoutCapturedItems(pendingImages, draft.images);
  pendingPdfTexts = _withoutCapturedItems(pendingPdfTexts, draft.pdfTexts);
  pendingVideos = _withoutCapturedItems(pendingVideos, draft.videos);
  if (typeof consumePendingReplyQuotes === 'function') {
    consumePendingReplyQuotes(draft.replyQuotes);
  }
  if (typeof consumePendingConvRefs === 'function') {
    consumePendingConvRefs(draft.convRefs);
  }
  if (pendingPdfTexts.length > 0) {
    if (typeof _vlmSaveState === 'function') _vlmSaveState();
  } else {
    if (typeof _vlmClearState === 'function') _vlmClearState();
    const progress = document.getElementById('pdfProgress');
    if (progress) progress.style.display = 'none';
  }
  if (typeof renderImagePreviews === 'function') renderImagePreviews();
}

/**
 * Roll back the optimistic composer clear when the turn command did NOT
 * commit (network failure, definitive rejection, pre-commit stop): the
 * captured text and attachments return so a retry sends the exact same
 * draft, while anything typed or attached afterwards stays untouched.
 */
function _restoreCapturedComposerDraft(draft) {
  const input = document.getElementById('userInput');
  if (input && draft.inputValue) {
    input.value = input.value
      ? draft.inputValue + '\n' + input.value
      : draft.inputValue;
    input.style.height = 'auto';
  }
  const restoreItems = (current, captured) => [
    ...(captured || []).filter((item) => !(current || []).includes(item)),
    ...(current || []),
  ];
  pendingImages = restoreItems(pendingImages, draft.images);
  pendingPdfTexts = restoreItems(pendingPdfTexts, draft.pdfTexts);
  pendingVideos = restoreItems(pendingVideos, draft.videos);
  if (typeof restorePendingReplyQuotes === 'function') {
    restorePendingReplyQuotes(draft.replyQuotes);
  }
  if (typeof restorePendingConvRefs === 'function') {
    restorePendingConvRefs(draft.convRefs);
  }
  if (pendingPdfTexts.length > 0 && typeof _vlmSaveState === 'function') {
    _vlmSaveState();
  }
  if (typeof renderImagePreviews === 'function') renderImagePreviews();
}

/**
 * Consume only the exact draft acknowledged by the server. Content or
 * attachments added while the request was in flight remain in the composer.
 */
function _consumeAcceptedComposerDraft(draft) {
  _clearCapturedComposerDraft(draft);
  _forgetComposerCommand(draft.commandId);
}

function _commandIsVisibleInAuthority(conv, commandId) {
  return Boolean(conv?.id && commandId
    && runtimeScope.ConversationTurnStore
      ?.hasAuthoritativeCommand?.(conv.id, commandId));
}
/**
 * Paint the submitted user message IMMEDIATELY as a transient overlay Turn.
 * The durable TurnStore stays untouched; the acknowledgement swaps in the
 * authoritative human turn (same visible content) and every send exit path
 * removes this echo exactly once.
 */
function _showOptimisticUserTurn(conv, draftProjection) {
  if (typeof createOptimisticUserTurn !== 'function'
      || !runtimeScope.ConversationTransientTurns) {
    return null;
  }
  const turn = createOptimisticUserTurn({
    conversationId: conv.id,
    commandId: draftProjection._msgId,
    text: draftProjection.content,
    timestamp: draftProjection.timestamp,
    images: draftProjection.images,
    pdfTexts: draftProjection.pdfTexts,
    videos: draftProjection.videos,
    replyQuotes: draftProjection.replyQuotes,
    convRefs: draftProjection.convRefs,
    contextSnapshot: draftProjection._ctx,
  });
  runtimeScope.ConversationTransientTurns.upsert(conv, turn);
  return turn.turnId;
}

function _removeOptimisticUserTurn(conv, turnId) {
  if (!conv || !turnId || !runtimeScope.ConversationTransientTurns) return;
  runtimeScope.ConversationTransientTurns.remove(conv, turnId);
}

function _applyAcceptedConversationTitle(conv, draft) {
  if (!draft.shouldSetTitle || !draft.text) return;
  const titleText = stripNoTranslateTags(draft.text);
  conv.title = titleText.slice(0, 60) + (titleText.length > 60 ? '...' : '');
  if (activeConvId === conv.id) {
    const title = document.getElementById('topbarTitle');
    if (title) title.textContent = conv.title;
  }
}

function _sendAbortError() {
  try {
    return new DOMException('Send cancelled', 'AbortError');
  } catch (_error) {
    const error = new Error('Send cancelled');
    error.name = 'AbortError';
    return error;
  }
}

async function sendMessage() {
  /* A stale restore can carry both flags. Plan wins so direct artifact
   * generation cannot bypass the backend's read-only authority. */
  if (imageGenMode && !planMode) {
    generateImageDirect();
    return;
  }
  if (typeof isBranchModeActive === 'function' && isBranchModeActive()) {
    const branchCtx = getActiveBranchContext();
    if (branchCtx && pendingVideos.length === 0) {
      const input = document.getElementById('userInput');
      const text = (input?.value || '').trim();
      if (!text && pendingImages.length === 0) return;
      if (pendingImages.length > 0) await _waitForImageProcessing();
      if (!text && pendingImages.length === 0) return;
      const imgs = [...pendingImages];
      pendingImages = [];
      renderImagePreviews();
      input.value = '';
      input.style.height = 'auto';
      sendBranchMessage(text, imgs.length ? imgs : null);
      return;
    }
  }
  if (_composerSendLocked) return;
  _composerSendLocked = true;
  try {
    await _submitComposerDraft();
  } finally {
    _composerSendLocked = false;
  }
}

async function _submitComposerDraft() {
  const input = document.getElementById('userInput');
  if (!input) return;
  const initialText = input.value.trim();
  if (!initialText && pendingImages.length === 0 && pendingPdfTexts.length === 0
      && pendingVideos.length === 0) {
    return;
  }

  if (_pendingLogClean) {
    const cleanup = _pendingLogClean;
    const descriptions = cleanup.ops.map((operation) => operation.desc).join('、');
    const applyCleanup = await showConfirm(
      t('send.logNoiseConfirm', {
        chars: cleanup.savedChars.toLocaleString(),
        pct: cleanup.savedPct,
        ops: descriptions,
      }),
      { okText: t('send.logNoiseClean'), cancelText: t('send.logNoiseKeep') },
    );
    if (applyCleanup) {
      input.value = input.value.replace(cleanup.originalText, cleanup.cleanedText);
      debugLog(
        'Log noise auto-cleaned on send: saved ' + cleanup.savedChars + ' chars',
        'success',
      );
    }
    hideLogCleanBanner();
  }

  if (pendingVideos.length > 0 && typeof _waitForPendingVideos === 'function') {
    await _waitForPendingVideos();
  }
  if (pendingImages.length > 0) await _waitForImageProcessing();
  const finalText = input.value.trim();
  if (!finalText && pendingImages.length === 0 && pendingPdfTexts.length === 0
      && pendingVideos.length === 0) {
    return;
  }

  let conv = getActiveConv();
  if (conv?._genStartCtrl) return;
  if (conv?._turnSnapshotRequired) {
    if (!runtimeScope.ConversationTurnStore) {
      throw new Error('Conversation turn runtime is unavailable.');
    }
    await runtimeScope.ConversationTurnStore.hydrateConversation(conv);
  }
  if (!conv) {
    const now = Date.now();
    conv = {
      id: generateId(),
      title: 'New Chat',
      createdAt: now,
      updatedAt: now,
      _localOnly: true,
    };
    if (projectState.active && projectState.path) {
      conv.projectPath = projectState.path;
      const projectPaths = [projectState.path];
      for (const root of projectState.extraRoots || []) {
        const path = typeof root === 'string' ? root : root.path;
        if (path && !projectPaths.includes(path)) projectPaths.push(path);
      }
      conv.projectPaths = projectPaths;
    }
    const folderId = typeof getActiveFolderId === 'function'
      ? getActiveFolderId() : null;
    if (folderId) conv.folderId = folderId;
    conversations.unshift(conv);
    activeConvId = conv.id;
    sessionStorage.setItem('tofu_activeConvId', conv.id);
    captureActiveConversationSettings();
    renderConversationList();
  }

  const convId = conv.id;
  const capturedReplyQuotes = typeof getPendingReplyQuotes === 'function'
    ? [...(getPendingReplyQuotes() || [])] : [];
  const capturedConvRefs = typeof getPendingConvRefs === 'function'
    ? [...(getPendingConvRefs() || [])] : [];
  const draft = {
    convId,
    inputValue: input.value,
    text: finalText,
    images: [...pendingImages],
    pdfTexts: [...pendingPdfTexts],
    videos: [...pendingVideos],
    replyQuotes: capturedReplyQuotes,
    convRefs: capturedConvRefs,
    shouldSetTitle: !runtimeScope.ConversationTurnRead?.hasActor?.(conv, 'human'),
  };
  draft.commandId = _composerCommandId(draft);

  const readyVideos = draft.videos.filter((video) => video && !video._status);
  const msgPayload = {
    text: finalText,
    images: [...draft.images],
    pdfTexts: [...draft.pdfTexts],
    timestamp: Date.now(),
    _msgId: draft.commandId,
  };
  const turnContext = typeof runtimeScope.buildTurnCtxSnapshot === 'function'
    ? runtimeScope.buildTurnCtxSnapshot() : null;
  if (turnContext) msgPayload.ctx = turnContext;
  if (readyVideos.length > 0) {
    msgPayload.videos = readyVideos.map((video) =>
      typeof _videoPayloadForSend === 'function'
        ? _videoPayloadForSend(video) : video);
  }
  if (draft.replyQuotes.length > 0) msgPayload.replyQuotes = draft.replyQuotes;
  if (draft.convRefs.length > 0) msgPayload.convRefs = draft.convRefs;
  const folderId = typeof getActiveFolderId === 'function'
    ? getActiveFolderId() : null;
  if (folderId) msgPayload.folderId = folderId;

  const draftProjection = {
    role: 'user',
    content: finalText,
    images: msgPayload.images,
    pdfTexts: msgPayload.pdfTexts,
    timestamp: msgPayload.timestamp,
    _msgId: msgPayload._msgId,
  };
  if (msgPayload.replyQuotes) draftProjection.replyQuotes = msgPayload.replyQuotes;
  if (msgPayload.convRefs) draftProjection.convRefs = msgPayload.convRefs;
  if (msgPayload.videos) draftProjection.videos = msgPayload.videos;
  if (turnContext) draftProjection._ctx = turnContext;

  const sendStart = createSendStartupLease(conv, { timeoutMs: 90000 });
  let willTranslate = false;
  let accepted = false;
  let userStopped = false;
  /* Optimistic echo: clear the composer and paint the user bubble NOW,
   * before the config-resolve and turn-command round trips. The
   * acknowledgement swaps in the authoritative turn; a failed or
   * pre-commit-stopped command restores the captured draft below. */
  _clearCapturedComposerDraft(draft);
  const optimisticTurnId = _showOptimisticUserTurn(conv, draftProjection);
  let optimisticEchoCleared = false;
  const clearOptimisticEcho = () => {
    if (optimisticEchoCleared) return;
    optimisticEchoCleared = true;
    _removeOptimisticUserTurn(conv, optimisticTurnId);
  };
  if (activeConvId === convId) {
    const welcome = document.getElementById('welcome');
    if (welcome) welcome.remove();
    _renderTranslatingBubble(t('sidebar.connecting'));
  }
  updateSendButton();
  renderConversationList();

  try {
    await _waitForVlmParsing(draftProjection, convId, sendStart.signal);
    if (sendStart.signal.aborted) throw _sendAbortError();
    msgPayload.pdfTexts = draftProjection.pdfTexts;

    const sendConfig = await _buildConvConfig(conv);
    const sendSettings = await _buildConvSettings(conv);
    sendConfig.assistantMsgId = (typeof _newClientMsgId === 'function')
      ? _newClientMsgId()
      : ('tmp_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8));
    willTranslate = Boolean(
      sendConfig.autoTranslate
      && /[\u4e00-\u9fff\u3400-\u4dbf]/.test(finalText),
    );
    if (willTranslate) {
      conv._translating = true;
      conv._translateAborted = false;
      conv._translateAbortCtrl = sendStart.controller;
      if (activeConvId === convId) _renderTranslatingBubble();
      updateSendButton();
      renderConversationList();
    }

    const injectMode = runtimeScope.ConversationTurnRead?.activeMainAttemptId?.(conv)
      ? await _promptInjectMode(convId) : 'queue';
    if (sendStart.signal.aborted) throw _sendAbortError();
    if (typeof runtimeScope.updateContextBar === 'function') {
      runtimeScope.updateContextBar();
    }
    if (!runtimeScope.ConversationTurnStore) {
      throw new Error('Turn runtime unavailable — reload the page.');
    }
    const turnExtra = buildTurnSubmissionExtra({
      commandId: draft.commandId,
      settings: sendSettings,
      config: sendConfig,
      signal: sendStart.signal,
      injectMode,
    });
    const acknowledgement = await runtimeScope.ConversationTurnStore
      .submitConversation(conv, msgPayload, sendConfig, turnExtra);

    if (acknowledgement?.aborted) {
      clearOptimisticEcho();
      _restoreCapturedComposerDraft(draft);
      _forgetComposerCommand(draft.commandId);
      debugLog('Send cancelled before the turn was created.', 'info');
      return;
    }
    accepted = Boolean(
      acknowledgement?.submittedTurn
      || acknowledgement?.turn
      || acknowledgement?.queued
      || acknowledgement?.steered,
    );
    if (!accepted) {
      throw new Error('The server returned an invalid turn acknowledgement.');
    }

    clearOptimisticEcho();
    _consumeAcceptedComposerDraft(draft);
    _applyAcceptedConversationTitle(conv, draft);
    runtimeScope.ConversationSurfacePresentation?.followLatest?.();
    renderConversationList();
    buildTurnNav(conv);

    userStopped = Boolean(conv._translateAborted || sendStart.stoppedByUser());
    if (userStopped && runtimeScope.ConversationTurnRead?.activeMainAttemptId?.(conv)) {
      await runtimeScope.ConversationTurnStore.abortConversation(conv);
    }
    if (acknowledgement.steered) {
      debugLog(t('steer.injected'), 'info');
      if (typeof showToast === 'function') showToast(t('steer.injected'), 'success');
    } else if (acknowledgement.queued) {
      const position = Number(acknowledgement.position || 0);
      debugLog('Message queued at position ' + position + '.', 'info');
      if (typeof showToast === 'function') {
        showToast(t('queue.queuedToast', { n: position }), 'info');
      }
    } else {
      debugLog('Turn accepted: ' + draft.commandId, 'success');
    }
  } catch (error) {
    userStopped = Boolean(conv._translateAborted || sendStart.stoppedByUser());
    clearOptimisticEcho();
    let committed = false;
    try {
      await runtimeScope.ConversationTurnStore?.hydrateConversation(conv);
      committed = _commandIsVisibleInAuthority(conv, draft.commandId);
      if (committed) {
        accepted = true;
        _consumeAcceptedComposerDraft(draft);
        _applyAcceptedConversationTitle(conv, draft);
        runtimeScope.ConversationSurfacePresentation?.followLatest?.();
        if (userStopped && runtimeScope.ConversationTurnRead?.activeMainAttemptId?.(conv)) {
          await runtimeScope.ConversationTurnStore.abortConversation(conv);
        }
      }
    } catch (refreshError) {
      console.warn(
        '[sendMessage] authoritative recovery failed:',
        refreshError?.message || refreshError,
      );
    }

    if (!committed) {
      _restoreCapturedComposerDraft(draft);
    }
    if (!committed && userStopped) {
      _forgetComposerCommand(draft.commandId);
      try {
        await Api.chat.abortConv(convId);
      } catch (abortError) {
        console.warn('[sendMessage] conversation abort marker failed:', abortError);
      }
    }
    const responseStatus = Number(
      error?.status || error?._status || error?.response?.status || 0,
    );
    if (!committed && responseStatus > 0) {
      _forgetComposerCommand(draft.commandId);
    }
    if (!committed && !userStopped) {
      const detail = error?.body?.error?.message
        || error?.body?.message
        || error?.message
        || 'Send failed';
      debugLog('Turn command failed: ' + detail, 'error');
      if (typeof showToast === 'function') {
        showToast(
          responseStatus > 0
            ? detail
            : detail + ' — draft kept; retry is safe',
          'error',
        );
      }
    }
  } finally {
    clearOptimisticEcho();
    sendStart.finish();
    if (willTranslate) {
      conv._translating = false;
      conv._translateAborted = false;
      conv._translateAbortCtrl = null;
    }
    /* The status bubble lives in the conversation's transient overlay, which
     * survives view switches — remove it even if the user navigated away
     * mid-send, otherwise it pins to the bottom as a stale 翻译中… row. */
    _removeTranslatingBubble(convId);
    if (activeConvId === convId) {
      if (!accepted
          && (runtimeScope.ConversationTurnRead?.ordered?.(conv) || []).length === 0) {
        runtimeScope.requestAuthoritativeConversationRender(convId);
      }
    }
    renderConversationList();
    updateSendButton();
  }
}

// ══════════════════════════════════════════════════════
//  Non-blocking background translation → auto-start assistant
// ══════════════════════════════════════════════════════

// Input translation is part of the accepted turn command. Output translation
// starts from the turn-settlement callback in conversation_turn_store.js.

// ══════════════════════════════════════════════════════
//  Pending Message Queue — dispatch, UI, cancel
// ══════════════════════════════════════════════════════

/**
 * Count of DISPATCHABLE queued messages for a conversation.
 *
 * Mirrors the backend's `_get_queue_depth` (lib/message_queue.py), which
 * excludes the autopilot armed-marker sentinel (kind='autopilot').  That
 * sentinel is a persistent flag consumed by the end-of-turn autopilot hook,
 * NOT a turn that ever gets dequeued & dispatched as a task.  Every frontend
 * gate that means "is there pending work the backend will start next?" MUST
 * use this — using the raw Map length instead makes an armed-but-idle
 * autopilot look like a permanently stuck queued message (ghost "Dispatching…"
 * bubble + a doomed ~15s _checkForQueuedTask retry loop).
 */
function _dispatchableQueueCount(convId) {
  return _queueItemsForConversation(convId)
    .filter((item) => item && item.kind !== 'autopilot').length;
}
if (typeof window !== 'undefined') runtimeScope._dispatchableQueueCount = _dispatchableQueueCount;

/** Read the queue from the same immutable store that projects the transcript. */
function _queueItemsForConversation(convId) {
  if (!convId || !runtimeScope.ConversationTurnStore) return [];
  const state = runtimeScope.ConversationTurnStore
    .ensureRuntimeStore(convId).getState();
  return Array.isArray(state.queueItems) ? state.queueItems : [];
}

async function _waitForVlmParsing(userMsg, convId, signal) {
  if (!userMsg.pdfTexts || userMsg.pdfTexts.length === 0) return;
  // 2026-05-06 (Option C): with parallel uploads, some entries may still be
  // in the TEXT-parse phase when sendMessage fires. Wait for those first —
  // `method === 'parsing'` means the text-extract call is still in flight.
  const MAX_TEXT_WAIT = 300; // 5 min safety cap (large PDFs can take 2 min)
  for (let i = 0; i < MAX_TEXT_WAIT; i++) {
    if (signal?.aborted) throw _sendAbortError();
    const stillExtracting = userMsg.pdfTexts.filter(p => p && p.method === 'parsing');
    if (stillExtracting.length === 0) break;
    if (i === 0) {
      console.log(`%c[Upload-Wait] Waiting for ${stillExtracting.length} PDF text-parse(s)…`, 'color:#f59e0b;font-weight:bold');
    }
    await new Promise(r => setTimeout(r, 1000));
  }
  // Check if any PDFs are still VLM-parsing
  const parsing = userMsg.pdfTexts.filter(p => p.vlmStatus === 'parsing');
  if (parsing.length === 0) {
    console.log('%c[VLM-Wait] All PDFs already done, no wait needed', 'color:#22c55e');
    return;
  }
  console.log(`%c[VLM-Wait] Waiting for ${parsing.length} PDF(s) to finish VLM parsing…`, 'color:#f59e0b;font-weight:bold');
  const _vlmTurnId = 'transient:vlm-document-processing';
  const _showVlmStatus = (detail) => {
    if (activeConvId !== convId || !runtimeScope.ConversationTransientTurns) return;
    const conv = conversations.find((item) => item?.id === convId);
    if (!conv) return;
    runtimeScope.ConversationTransientTurns.upsert(
      conv,
      createTransientStatusTurn({
        conversationId: convId,
        turnId: _vlmTurnId,
        phase: 'document_processing',
        label: 'Waiting for VLM PDF parsing…',
        detail,
      }),
    );
  };
  const _removeVlmStatus = () => {
    const conv = conversations.find((item) => item?.id === convId);
    if (conv) runtimeScope.ConversationTransientTurns?.remove?.(conv, _vlmTurnId);
  };
  if (activeConvId === convId) {
    _showVlmStatus('');
    scrollToBottom();
  }
  try {
    // Poll until all PDFs finish VLM (done/done-skipped/failed/timeout/unavailable)
    const MAX_VLM_WAIT = 180; // 180 × 1s = 3 minutes max
    for (let attempt = 0; attempt < MAX_VLM_WAIT; attempt++) {
      if (signal?.aborted) throw _sendAbortError();
      await new Promise(r => setTimeout(r, 1000));
      const stillParsing = userMsg.pdfTexts.filter(p => p.vlmStatus === 'parsing');
      if (stillParsing.length === 0) {
        console.log(`%c[VLM-Wait] ✓ All PDFs VLM-done (waited ${attempt}s)`, 'color:#22c55e;font-weight:bold');
        break;
      }
      if (activeConvId === convId) {
        const progress = stillParsing.map(p =>
          `${p.name.slice(0, 15)}${p.vlmProgress ? ': ' + p.vlmProgress : ''}`);
        _showVlmStatus(`VLM parsing: ${progress.join(', ')} (${attempt}s)`);
      }
    }
  } finally {
    _removeVlmStatus();
  }
}

/* ── Queue item sources + collapse state ────────────────────────────
 * Every queued row has exactly ONE source, distinguished visually by a
 * tinted left edge + number badge + (for non-human sources) an attribution
 * line above the preview:
 *   own       — typed here, queued behind the running turn (neutral accent)
 *   agent     — project_message/intervene from a SIBLING agent conversation
 *   operator  — a HUMAN operator nudge delivered through the peer channel
 *   workflow  — a Project-Brain autonomous epic kickoff (KIND_WORKFLOW)
 *   autopilot — the armed sentinel (not a message; cancel = DISARM)
 */
function _queueSourceOf(item) {
  if (item.kind === 'autopilot') return 'autopilot';
  if (item.kind === 'workflow_step') return 'workflow';
  if (item.isPeerMessage) return item.isPeerHuman ? 'operator' : 'agent';
  return 'own';
}

/* Per-source SVG glyphs (no emoji / unicode-glyph icons — CLAUDE.md §3.4). */
const _QUEUE_SRC_ICONS = {
  autopilot: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><path d="M12 3v6M12 15v6M3 12h6M15 12h6"/></svg>`,
  agent: `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 8V5"/><circle cx="12" cy="3.5" r="1"/><path d="M9 13.5h.01M15 13.5h.01"/><path d="M9.5 17h5"/></svg>`,
  operator: `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4.5 20.5c0-4 3.4-6.5 7.5-6.5s7.5 2.5 7.5 6.5"/></svg>`,
  workflow: `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="5" r="2.2"/><circle cx="6" cy="19" r="2.2"/><circle cx="18" cy="7" r="2.2"/><path d="M6 7.2v9.6"/><path d="M18 9.4c0 4.6-6.8 3.2-10.2 6.4"/></svg>`,
};
const _QUEUE_ICON_CHEVRON = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>`;
const _QUEUE_ICON_X = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M5 5l14 14M19 5L5 19"/></svg>`;
const _QUEUE_ICON_CLOUD = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.5 18.5a4.3 4.3 0 0 0 .8-8.5 5.5 5.5 0 0 0-10.8 1.4A3.8 3.8 0 0 0 7 18.5h10.5z"/></svg>`;

/* Preview text for one queued item (shared by the row and the collapsed
 * "next up" header line). */
function _queueItemPreview(item) {
  return item.text
    ? item.text
    : (item.hasImages ? t('queue.imagesCount', { n: 1 }) : t('queue.attachment'));
}

/* Collapse preference — persisted per-conv because renderPendingQueueUI
 * rebuilds the bar's innerHTML on every poll, so the state cannot live in
 * the DOM. null = the user never toggled → the auto-collapse rule decides. */
const QUEUE_AUTO_COLLAPSE_MIN = 4;

function _queueCollapseRead(convId) {
  try {
    if (typeof localStorage === 'undefined') return null;
    const v = localStorage.getItem('tofu.queueCollapsed.' + convId);
    return v === null ? null : v === '1';
  } catch (e) {
    console.debug('[Queue] collapse pref read failed:', e);
    return null;
  }
}

function _queueCollapseWrite(convId, collapsed) {
  try {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem('tofu.queueCollapsed.' + convId, collapsed ? '1' : '0');
  } catch (e) {
    console.debug('[Queue] collapse pref write failed:', e);
  }
}

/* Effective collapse state: the explicit toggle wins; otherwise a queue of
 * QUEUE_AUTO_COLLAPSE_MIN+ dispatchable items starts collapsed so a flooded
 * queue cannot bury the ConversationSurface behind the input bar. */
function _queueCollapsedNow(convId, realCount) {
  const pref = _queueCollapseRead(convId);
  return pref !== null ? pref : realCount >= QUEUE_AUTO_COLLAPSE_MIN;
}

/* Header chevron handler (inline onclick) — flips + persists the state. */
function togglePendingQueueCollapsed(convId) {
  const queue = _queueItemsForConversation(convId);
  const realCount = queue.filter((it) => _queueSourceOf(it) !== 'autopilot').length;
  _queueCollapseWrite(convId, !_queueCollapsedNow(convId, realCount));
  renderPendingQueueUI(convId);
}
if (typeof window !== 'undefined') runtimeScope.togglePendingQueueCollapsed = togglePendingQueueCollapsed;

/**
 * Render the pending queue indicator above the input area.
 *
 * CROSS-CONV BLEED GUARD — the input-bar queue is a SINGLE shared DOM node
 * (``#pendingQueueBar`` in ``#pendingQueueContainer``), while TurnStore owns
 * one queue per conversation. Every DOM mutation must therefore be gated on
 * ``convId === activeConvId``: queue refresh, autopilot arm/disarm, and
 * TurnStore hydration run asynchronously, so work for conversation A can finish after the
 * user switched to conv B and unconditionally paint A's queue into B's visible
 * bar. The normalized store is still updated for the inactive conversation;
 * only its paint is suppressed.
 *
 * The removal branch (``queue-removing``+timeout) is likewise gated so a stale
 * empty-queue render for an inactive conv can't tear down the bar that
 * currently belongs to the active conv.
 */
function renderPendingQueueUI(convId) {
  const _isActive = (typeof activeConvId !== 'undefined') && convId === activeConvId;
  let container = document.getElementById("pendingQueueBar");
  const queue = _queueItemsForConversation(convId);
  if (!queue || queue.length === 0) {
    if (container && _isActive) {
      container.classList.add('queue-removing');
      setTimeout(() => {
        /* Idempotent: only remove if nothing repainted since. A later
         * ``renderPendingQueueUI(activeConvId)`` clears ``queue-removing`` when
         * it repopulates the same container. */
        if (container && container.parentNode && container.classList.contains('queue-removing')) {
          container.remove();
        }
      }, 200);
    }
    return;
  }
  if (!_isActive) return;
  if (!container) {
    container = document.createElement("div");
    container.id = "pendingQueueBar";
    container.className = "pending-queue-bar";
    const queueHost = document.getElementById("pendingQueueContainer");
    if (queueHost) queueHost.appendChild(container);
  }
  container.classList.remove('queue-removing');
  let _realCount = 0;   // human/workflow items get sequential numbers
  const items = queue.map((item) => {
    const _src = _queueSourceOf(item);
    /* ── Autopilot armed-marker sentinel — always rendered last (priority 90),
     *   distinct styling, cancel = DISARM (not just queue-remove). ── */
    if (_src === 'autopilot') {
      const apLabel = (typeof t === 'function') ? t('autopilot.pendingTakeover') : 'Autopilot will take over';
      const apCancelTitle = (typeof t === 'function') ? t('autopilot.cancelTakeover') : 'Cancel autopilot';
      return `<div class="pending-queue-item pending-queue-autopilot">
        <span class="queue-item-number queue-item-autopilot-icon">${_QUEUE_SRC_ICONS.autopilot}</span>
        <span class="queue-item-text">${escapeHtml(apLabel)}</span>
        <button class="queue-item-cancel" data-tofu-action="cancelAutopilotMarker('${convId}')" title="${escapeHtml(apCancelTitle)}">${_QUEUE_ICON_X}</button>
      </div>`;
    }
    const i = _realCount++;
    const preview = _queueItemPreview(item);
    // Attachment badges
    const badges = [];
    if (item.hasImages) badges.push('<span>img</span>');
    if (item.hasPdfs) badges.push('<span>pdf</span>');
    if (item.hasRefs) badges.push('<span>ref</span>');
    if (item.hasQuotes) badges.push('<span>↩</span>');
    // ── Source attribution line ──
    // A peer turn (agent / operator) names the SOURCE conversation by its
    // TITLE (a raw id is meaningless) and jumps to it on click. A brain
    // workflow kickoff gets a static label (no conversation to jump to).
    let srcLine = '';
    if ((_src === 'agent' || _src === 'operator') && item.fromConv) {
      const _title = (typeof convTitleById === 'function')
        ? convTitleById(item.fromConv) : item.fromConv;
      const _lbl = (typeof t === 'function')
        ? t(_src === 'operator' ? 'queue.fromOperator' : 'queue.fromConv')
        : (_src === 'operator' ? 'from operator' : 'from');
      srcLine = `<div class="queue-item-src" data-tofu-action="loadConversation('${escapeHtml(item.fromConv)}')" `
        + `title="${escapeHtml(_title)}">${_QUEUE_SRC_ICONS[_src]}<span>${escapeHtml(_lbl)} «${escapeHtml(_title)}»</span></div>`;
    } else if (_src === 'workflow') {
      const _wfLbl = (typeof t === 'function') ? t('queue.fromWorkflow') : '项目大脑派发';
      srcLine = `<div class="queue-item-src queue-item-src-static">${_QUEUE_SRC_ICONS.workflow}<span>${escapeHtml(_wfLbl)}</span></div>`;
    }
    return `<div class="pending-queue-item qsrc-${_src}">
      <span class="queue-item-number">${i + 1}</span>
      <div class="queue-item-body">
        ${srcLine}
        <span class="queue-item-text">${escapeHtml(preview)}</span>
      </div>
      ${badges.length ? `<span class="queue-item-attachments">${badges.join('')}</span>` : ''}
      <button class="queue-item-cancel" data-tofu-action="removePendingQueueItem('${convId}', ${i})" title="${escapeHtml(t('queue.cancelMsg'))}">${_QUEUE_ICON_X}</button>
      ${item.queueId ? `<span class="queue-item-synced" title="${escapeHtml(t('queue.syncedToServer'))}">${_QUEUE_ICON_CLOUD}</span>` : ''}
    </div>`;
  }).join("");

  /* Collapse state — re-applied on every render (polls rebuild innerHTML),
   * so it is read from localStorage via _queueCollapsedNow, not the DOM. */
  const _collapsed = _queueCollapsedNow(convId, _realCount);
  if (_collapsed) container.classList.add('queue-collapsed');
  else container.classList.remove('queue-collapsed');

  const headerSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4m0 12v4M4.93 4.93l2.83 2.83m8.48 8.48l2.83 2.83M2 12h4m12 0h4M4.93 19.07l2.83-2.83m8.48-8.48l2.83-2.83"/></svg>`;
  /* Header count reflects only dispatchable queue items; the autopilot
   * sentinel is described by its own row. */
  const _headerLabel = _realCount > 0
    ? `${_realCount} ${(typeof t === 'function') ? t('queue.messagesQueued') : '条消息排队中'}`
    : ((typeof t === 'function') ? t('autopilot.armedShort') : 'Autopilot armed');
  /* One-line "next up" preview + an autopilot chip — CSS only reveals them
   * in the collapsed state, where they stand in for the hidden item list. */
  const _nextItem = queue.find((it) => _queueSourceOf(it) !== 'autopilot');
  const _nextText = _nextItem ? _queueItemPreview(_nextItem) : '';
  const _hasAutopilot = queue.some((it) => _queueSourceOf(it) === 'autopilot');
  const _apTitle = (typeof t === 'function') ? t('autopilot.pendingTakeover') : 'Autopilot will take over';
  const _toggleTitle = (typeof t === 'function')
    ? t(_collapsed ? 'queue.expand' : 'queue.collapse')
    : (_collapsed ? 'Expand queue' : 'Collapse queue');
  container.innerHTML = `<div class="queue-header">
    <button class="queue-toggle" data-tofu-action="togglePendingQueueCollapsed('${convId}')" title="${escapeHtml(_toggleTitle)}">${_QUEUE_ICON_CHEVRON}</button>
    ${headerSvg}
    <span class="queue-header-label">${escapeHtml(_headerLabel)}</span>
    ${_hasAutopilot ? `<span class="queue-header-ap" title="${escapeHtml(_apTitle)}">${_QUEUE_SRC_ICONS.autopilot}</span>` : ''}
    <span class="queue-next-preview" title="${escapeHtml(_nextText)}">${escapeHtml(_nextText)}</span>
    ${_realCount > 1 ? `<button class="queue-clear-all" data-tofu-action="clearPendingQueue('${convId}')">${(typeof t === 'function') ? t('queue.clearAll') : '全部清空'}</button>` : ''}
  </div><div class="queue-items">${items}</div>`;
}

/**
 * Cancel the autopilot armed-marker (disarm) from the queue bar.
 * Clears the persistent sentinel + flips any live task's autopilot off, and
 * turns the toolbar toggle off to keep the UI consistent.
 */
function cancelAutopilotMarker(convId) {
  if (typeof Api !== 'undefined' && Api.chat && Api.chat.disarmAutopilot) {
    Api.chat.disarmAutopilot(convId)
      .then((resp) => {
        /* Fold the just-concluded run instantly even with no live stream. */
        if (typeof _applyDisarmResponse === 'function') _applyDisarmResponse(convId, resp);
        if (typeof _refreshServerQueue === 'function') _refreshServerQueue(convId);
      })
      .catch((e) => console.warn('[Autopilot] disarm (queue cancel) failed:', e && e.message));
  }
  /* Reflect the cancel in the toolbar toggle for the active conv. */
  const _conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
  if (_conv && _conv.id === convId && typeof autopilotEnabled !== 'undefined' && autopilotEnabled
      && typeof _applyAutopilotUI === 'function') {
    _applyAutopilotUI(false);
    if (typeof captureActiveConversationSettings === 'function') captureActiveConversationSettings();
  }
  debugLog('Autopilot canceled — virtual user will not take over', 'info');
}
if (typeof window !== 'undefined') runtimeScope.cancelAutopilotMarker = cancelAutopilotMarker;

/**
 * Remove a single item from the pending queue (server-backed).
 */
function removePendingQueueItem(convId, idx) {
  const queue = _queueItemsForConversation(convId)
    .filter((item) => item && item.kind !== 'autopilot');
  if (!queue.length) return;
  const removed = queue[idx];
  const removedPreview = removed?.text ? removed.text.slice(0, 40) : '(attachment)';
  const queueId = removed?.queueId;

  if (queueId) {
    Api.chat.queueRemove(convId, queueId)
      .then(async (resp) => {
        if (!resp || !resp.ok) throw new Error(`HTTP ${resp ? resp.status : 'no response'}`);
        await _refreshServerQueue(convId);
        debugLog(`已取消排队消息 #${idx + 1}: ${removedPreview}`, 'info');
      })
      .catch(err => console.error('[Queue] Server remove error:', err));
  }
}

async function _refreshServerQueue(convId) {
  const conv = conversations.find((item) => item && item.id === convId);
  if (!conv) return;
  try {
    await runtimeScope.ConversationTurnStore.hydrateConversation(conv);
    renderPendingQueueUI(convId);
    updateSendButton();
  } catch (err) {
    console.warn('[Queue] authoritative refresh failed:', err);
  }
}

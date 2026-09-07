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
 * @returns {Promise<'steer'|'queue'|'cancel'>}
 */
async function _promptInjectMode(convId) {
  if (typeof showChoice !== 'function') return 'cancel';
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
      {
        value: 'cancel',
        label: _tt('common.cancel', '取消'),
        subtitle: _tt('inject.subCancel', '保留输入内容,暂不发送'),
      },
    ],
    dismissValue: 'cancel',
    liveCheck: () => {
      const c = conversations.find((x) => x.id === convId);
      return Boolean(runtimeScope.ConversationTurnRead?.activeMainAttemptId?.(c));
    },
  });
  return choice === 'steer' || choice === 'queue' ? choice : 'cancel';
}
if (typeof window !== 'undefined') runtimeScope._promptInjectMode = _promptInjectMode;

let _composerSendLocked = false;
let _composerSendRequestedWhileLocked = false;
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
      ? input.value + '\n\n' + draft.inputValue
      : draft.inputValue;
    input.style.height = 'auto';
  }
  const stableItemId = (item) => item && typeof item === 'object'
    ? (item.attachmentId || item.id || item._msgId || item.url || item.path || item)
    : item;
  const restoreItems = (current, captured) => {
    const restored = [...(current || [])];
    const present = new Set(restored.map(stableItemId));
    for (const item of captured || []) {
      const stableId = stableItemId(item);
      if (present.has(stableId)) continue;
      present.add(stableId);
      restored.push(item);
    }
    return restored;
  };
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

function _isLaneBusySendError(error) {
  const code = error?.body?.error?.code
    || error?.body?.code
    || error?.response?.error?.code
    || error?.code;
  return code === 'lane_busy' || code === 'turn_in_progress';
}
/**
 * Paint the submitted user message IMMEDIATELY as a transient overlay Turn.
 * The durable TurnStore stays untouched; the acknowledgement swaps in the
 * authoritative human turn (same visible content) and every send exit path
 * removes this echo exactly once.
 */
function _showOptimisticUserTurn(conv, draftProjection) {
  if (typeof createOptimisticTurnPair !== 'function'
      || !runtimeScope.ConversationTransientTurns) {
    return [];
  }
  const pair = createOptimisticTurnPair({
    conversationId: conv.id,
    commandId: draftProjection._msgId,
    text: draftProjection.content,
    timestamp: draftProjection.timestamp,
    images: draftProjection.images,
    attachments: draftProjection.attachments,
    pdfTexts: draftProjection.pdfTexts,
    videos: draftProjection.videos,
    replyQuotes: draftProjection.replyQuotes,
    convRefs: draftProjection.convRefs,
    contextSnapshot: draftProjection._ctx,
  });
  runtimeScope.ConversationTransientTurns.upsert(conv, pair.inputTurn);
  runtimeScope.ConversationTransientTurns.upsert(conv, pair.outputTurn);
  return [pair.inputTurn.turnId, pair.outputTurn.turnId];
}

function _removeOptimisticUserTurn(conv, turnIds) {
  if (!conv || !runtimeScope.ConversationTransientTurns) return;
  for (const turnId of turnIds || []) {
    runtimeScope.ConversationTransientTurns.remove(conv, turnId);
  }
}

/**
 * Re-label the optimistic assistant container in place as the send command
 * advances (connecting → translating), so preparation never stacks a second
 * agent bubble. Returns false when this send has no optimistic pair (steer),
 * letting the caller fall back to the standalone status bubble.
 */
function _updateOptimisticAssistantPhase(conv, turnId, phase, label) {
  if (!conv || !turnId
      || typeof withOptimisticAssistantPreparation !== 'function'
      || typeof runtimeScope.ConversationTransientTurns?.get !== 'function') {
    return false;
  }
  const current = runtimeScope.ConversationTransientTurns.get(conv.id, turnId);
  if (!current) return false;
  runtimeScope.ConversationTransientTurns.upsert(
    conv, withOptimisticAssistantPreparation(current, phase, label));
  return true;
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
    runtimeScope.generateImageDirect?.();
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
  if (_composerSendLocked) {
    /* A click or Enter press is a command intent, not a disposable event.
     * Collapse repeated presses into one trailing attempt while the current
     * draft waits for preprocessing/ACK. The active owner drains it only
     * after an authoritative acceptance, so failures still stop and restore
     * the draft instead of silently auto-retrying an uncertain command. */
    _composerSendRequestedWhileLocked = true;
    return;
  }
  _composerSendLocked = true;
  try {
    let previousCommandAccepted = false;
    do {
      _composerSendRequestedWhileLocked = false;
      previousCommandAccepted = Boolean(await _submitComposerDraft());
    } while (previousCommandAccepted && _composerSendRequestedWhileLocked);
  } finally {
    _composerSendRequestedWhileLocked = false;
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
    try {
      await runtimeScope.ConversationTurnStore.hydrateConversation(conv);
    } catch (hydrateError) {
      /* Snapshot reads are bounded, so a stalled server rejects here instead
       * of latching the composer lock until reload. Keep the draft and
       * continue: submitConversation re-hydrates cold stores itself, and a
       * repeat failure lands in the send catch path with the draft kept. */
      console.warn(
        '[sendMessage] pre-submit hydration failed:',
        hydrateError?.message || hydrateError,
      );
    }
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
  let injectMode;
  if (runtimeScope.ConversationTurnRead?.activeMainAttemptId?.(conv)) {
    injectMode = await _promptInjectMode(convId);
    if (injectMode === 'cancel') return false;
  }
  draft.commandId = _composerCommandId(draft);

  const referencedDocuments = draft.pdfTexts.filter(
    (documentAttachment) => documentAttachment?.attachmentId,
  );
  const legacyDocuments = draft.pdfTexts.filter(
    (documentAttachment) => !documentAttachment?.attachmentId,
  );
  const referencedVideos = draft.videos.filter(
    (video) => video?.attachmentId,
  );
  const readyLegacyVideos = draft.videos.filter(
    (video) => video && !video.attachmentId && !video._status,
  );
  const msgPayload = {
    text: finalText,
    images: [...draft.images],
    timestamp: Date.now(),
    _msgId: draft.commandId,
  };
  const mediaAttachments = [
    ...referencedDocuments.map((documentAttachment) =>
      typeof _documentPayloadForSend === 'function'
        ? _documentPayloadForSend(documentAttachment) : documentAttachment),
    ...referencedVideos.map((video) =>
      typeof _videoPayloadForSend === 'function'
        ? _videoPayloadForSend(video) : video),
  ];
  if (mediaAttachments.length > 0) msgPayload.attachments = mediaAttachments;
  if (legacyDocuments.length > 0) msgPayload.pdfTexts = legacyDocuments;
  const turnContext = typeof runtimeScope.buildTurnCtxSnapshot === 'function'
    ? runtimeScope.buildTurnCtxSnapshot() : null;
  if (turnContext) msgPayload.ctx = turnContext;
  if (readyLegacyVideos.length > 0) {
    msgPayload.videos = readyLegacyVideos.map((video) =>
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
    timestamp: msgPayload.timestamp,
    _msgId: msgPayload._msgId,
  };
  if (msgPayload.attachments) draftProjection.attachments = msgPayload.attachments;
  if (msgPayload.pdfTexts) draftProjection.pdfTexts = msgPayload.pdfTexts;
  if (msgPayload.replyQuotes) draftProjection.replyQuotes = msgPayload.replyQuotes;
  if (msgPayload.convRefs) draftProjection.convRefs = msgPayload.convRefs;
  if (msgPayload.videos) draftProjection.videos = msgPayload.videos;
  if (turnContext) draftProjection._ctx = turnContext;

  const sendStart = createSendStartupLease(conv, { timeoutMs: 90000 });
  let willTranslate = false;
  let accepted = false;
  let userStopped = false;
  /* Optimistic pair: an ordinary/queued command paints the user input and
   * its stable assistant container before the round trip. A steer is an
   * injection block on the already-live assistant Turn, so it must never
   * manufacture even a provisional transcript Turn. */
  _clearCapturedComposerDraft(draft);
  let optimisticTurnIds = [];
  let optimisticEchoCleared = true;
  const clearOptimisticEcho = () => {
    if (optimisticEchoCleared) return;
    optimisticEchoCleared = true;
    _removeOptimisticUserTurn(conv, optimisticTurnIds);
  };
  const showOptimisticEcho = (deliveryMode) => {
    if (deliveryMode === 'steer') {
      optimisticTurnIds = [];
      optimisticEchoCleared = true;
      return;
    }
    optimisticTurnIds = _showOptimisticUserTurn(conv, draftProjection);
    optimisticEchoCleared = false;
  };
  const showSendPreparation = (phase, label) => {
    if (_updateOptimisticAssistantPhase(conv, optimisticTurnIds[1], phase, label)) {
      return;
    }
    /* Steer sends paint no optimistic pair; keep the standalone bubble. */
    if (activeConvId !== convId) return;
    if (phase === 'translating') {
      _renderTranslatingBubble();
    } else {
      _renderTranslatingBubble(label);
    }
  };
  showOptimisticEcho(injectMode);
  if (activeConvId === convId) {
    const welcome = document.getElementById('welcome');
    if (welcome) welcome.remove();
  }
  showSendPreparation('connecting', t('sidebar.connecting'));
  updateSendButton();
  renderConversationList();

  try {
    await _waitForVlmParsing(draftProjection, convId, sendStart.signal);
    if (sendStart.signal.aborted) throw _sendAbortError();
    if (draftProjection.pdfTexts) {
      msgPayload.pdfTexts = draftProjection.pdfTexts;
    }

    const {
      config: sendConfig,
      settings: sendSettings,
    } = await _buildConvSubmission(conv);
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
      showSendPreparation('translating', t('sidebar.translating'));
      updateSendButton();
      renderConversationList();
    }

    if (sendStart.signal.aborted) throw _sendAbortError();
    if (typeof runtimeScope.updateContextBar === 'function') {
      runtimeScope.updateContextBar();
    }
    if (!runtimeScope.ConversationTurnStore) {
      throw new Error('Turn runtime unavailable — reload the page.');
    }
    let acknowledgement;
    let selectedInjectMode = injectMode;
    for (let submissionAttempt = 0; submissionAttempt < 2; submissionAttempt += 1) {
      const turnExtra = buildTurnSubmissionExtra({
        commandId: draft.commandId,
        settings: sendSettings,
        config: sendConfig,
        signal: sendStart.signal,
        injectMode: selectedInjectMode,
      });
      try {
        acknowledgement = await runtimeScope.ConversationTurnStore
          .submitConversation(conv, msgPayload, sendConfig, turnExtra);
        break;
      } catch (submissionError) {
        if (submissionAttempt > 0 || selectedInjectMode
            || !_isLaneBusySendError(submissionError)) {
          throw submissionError;
        }
        /* The lane became busy after the idle preflight. Return the timeline
         * and composer to their exact pre-send state before asking for new
         * authority; retry then reuses the same idempotency command identity. */
        clearOptimisticEcho();
        _restoreCapturedComposerDraft(draft);
        const racedChoice = await _promptInjectMode(convId);
        if (racedChoice === 'cancel') {
          _forgetComposerCommand(draft.commandId);
          return false;
        }
        selectedInjectMode = racedChoice;
        _clearCapturedComposerDraft(draft);
        showOptimisticEcho(selectedInjectMode);
      }
    }

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
  /* The outer serializer may drain one send intent received while this
   * command was in flight. A user stop or uncertain/non-committed failure is
   * a hard boundary: leave the restored draft for an explicit retry. */
  return accepted && !userStopped;
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
 * excludes the legacy autopilot marker (kind='autopilot'). That compatibility
 * row is not dispatchable; the new ``goal_continuation`` command is. Every frontend
 * gate that means "is there pending work the backend will start next?" MUST
 * use this — using the raw Map length instead makes an armed-but-idle
 * legacy marker look like a permanently stuck queued message (ghost "Dispatching…"
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

/* ── Input-area queue bar ───────────────────────────────────────────────
 * Regular queued messages are rendered INLINE in the transcript by the
 * ConversationSurface (``conversation-queue-item``), so this bar must NOT
 * repeat them. The only queue entry the transcript view-model deliberately
 * excludes is the legacy AUTOPILOT sentinel. New Goal Mode continuations are
 * regular turn-native queue rows and render inline with other pending turns.
 */
const _QUEUE_ICON_AUTOPILOT = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><path d="M12 3v6M12 15v6M3 12h6M15 12h6"/></svg>`;
const _QUEUE_ICON_X = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M5 5l14 14M19 5L5 19"/></svg>`;

/**
 * Render a legacy autopilot marker above the input area.
 *
 * Real queued messages appear inline in the transcript (ConversationSurface);
 * this bar only paints the autopilot sentinel, which the transcript
 * view-model filters out.
 *
 * CROSS-CONV BLEED GUARD — the input-bar marker is a SINGLE shared DOM node
 * (``#pendingQueueBar`` in ``#pendingQueueContainer``), while TurnStore owns
 * one queue per conversation. Every DOM mutation must therefore be gated on
 * ``convId === activeConvId``: queue refresh, autopilot arm/disarm, and
 * TurnStore hydration run asynchronously, so work for conversation A can finish after the
 * user switched to conv B and unconditionally paint A's marker into B's visible
 * bar. The normalized store is still updated for the inactive conversation;
 * only its paint is suppressed.
 *
 * The removal branch (``queue-removing``+timeout) is likewise gated so a stale
 * empty render for an inactive conv can't tear down the bar that
 * currently belongs to the active conv.
 */
function renderPendingQueueUI(convId) {
  const _isActive = (typeof activeConvId !== 'undefined') && convId === activeConvId;
  let container = document.getElementById("pendingQueueBar");
  const sentinels = _queueItemsForConversation(convId)
    .filter((item) => item && item.kind === 'autopilot');
  if (sentinels.length === 0) {
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
  /* Cancel on the sentinel row = DISARM (not just queue-remove). */
  const apLabel = (typeof t === 'function') ? t('autopilot.pendingTakeover') : 'Autopilot will take over';
  const apCancelTitle = (typeof t === 'function') ? t('autopilot.cancelTakeover') : 'Cancel autopilot';
  const headerLabel = (typeof t === 'function') ? t('autopilot.armedShort') : 'Autopilot armed';
  const rows = sentinels.map(() => `<div class="pending-queue-item pending-queue-autopilot">
    <span class="queue-item-number queue-item-autopilot-icon">${_QUEUE_ICON_AUTOPILOT}</span>
    <span class="queue-item-text">${escapeHtml(apLabel)}</span>
    <button class="queue-item-cancel" data-tofu-action="cancelAutopilotMarker('${convId}')" title="${escapeHtml(apCancelTitle)}">${_QUEUE_ICON_X}</button>
  </div>`).join("");
  container.innerHTML = `<div class="queue-header">
    <span class="queue-header-label">${escapeHtml(headerLabel)}</span>
  </div><div class="queue-items">${rows}</div>`;
}

/**
 * Cancel a legacy autopilot marker from the queue bar. The disarm endpoint
 * also cancels current GoalRun/queued-continuation state.
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

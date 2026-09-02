/* ===== migrated source: ui/send_button.js ===== */
/* Composer send/stop control derived from authoritative turn state. */
const TURN_STOP_CONFIRMATION_MS = 4000;

function _activeConversationAttemptIds(conv) {
  return conv
    ? [...(runtimeScope.ConversationTurnRead?.activeAttemptIds?.(conv) || [])]
    : [];
}

function _releaseStopRequest(conv, attemptId, reason) {
  if (!conv || conv._finishingAttemptId !== attemptId) return;
  conv._finishingStream = false;
  conv._finishingAttemptId = null;
  updateSendButton();
  renderConversationList();
  if (reason && typeof showToast === 'function') showToast(reason, 'warning');
}

function _stopTranslation(conv) {
  Api.chat.abortConv(conv.id).catch((error) =>
    console.warn('[stop] translation abort marker failed:', error));
  conv._translateAborted = true;
  conv._translating = false;
  conv._translateAbortCtrl?.abort();
  conv._translateAbortCtrl = null;
  updateSendButton();
  renderConversationList();
}

function _stopGenerationStartup(conv) {
  Api.chat.abortConv(conv.id).catch((error) =>
    console.warn('[stop] startup abort marker failed:', error));
  const controller = conv._genStartCtrl;
  conv._genStartStop = controller;
  conv._genStartCtrl = null;
  controller?.abort();
  updateSendButton();
  renderConversationList();
}

function _stopConversationAttempts(conv) {
  const attemptIds = _activeConversationAttemptIds(conv);
  if (!attemptIds.length) {
    runtimeScope.ConversationTurnStore.hydrateConversation(conv).catch((error) =>
      console.warn('[stop] authoritative refresh failed:', error));
    return;
  }
  if (conv._finishingStream) return;
  const latchAttemptId = attemptIds[0];
  conv._finishingStream = true;
  conv._finishingAttemptId = latchAttemptId;
  updateSendButton();

  Promise.allSettled(attemptIds.map((attemptId) =>
    runtimeScope.ConversationTurnStore.abortAttempt(attemptId)))
    .then((results) => {
      const rejected = results.find((result) => result.status === 'rejected');
      if (rejected) {
        console.warn('[stop] attempt abort failed:', rejected.reason);
        _releaseStopRequest(conv, latchAttemptId, '停止请求未确认，请重试');
      }
    });

  setTimeout(() => {
    if (conv._finishingAttemptId !== latchAttemptId) return;
    _releaseStopRequest(conv, latchAttemptId, '仍在等待服务器确认停止');
    runtimeScope.ConversationTurnStore.hydrateConversation(conv).catch((error) =>
      console.warn('[stop] post-timeout refresh failed:', error));
  }, TURN_STOP_CONFIRMATION_MS);
}

function updateSendButton() {
  const conv = getActiveConv();
  const translating = Boolean(conv?._translating);
  const startupConnecting = Boolean(conv?._genStartCtrl);
  const turnBusy = Boolean(conv && convIsBusy(conv));
  const stopping = Boolean(conv?._finishingStream);
  const stop = () => {
    if (!conv || conv._finishingStream) return;
    if (translating) {
      _stopTranslation(conv);
      return;
    }
    if (startupConnecting) {
      _stopGenerationStartup(conv);
      return;
    }
    _stopConversationAttempts(conv);
  };
  const state = updateComposerSendControls({
    document,
    translating,
    startupConnecting,
    turnBusy,
    stopping,
    hasAttachmentDraft: pendingImages.length > 0
      || (typeof pendingPdfTexts !== 'undefined' && pendingPdfTexts.length > 0)
      || (typeof pendingVideos !== 'undefined' && pendingVideos.length > 0),
    queueCount: conv ? _dispatchableQueueCount(conv.id) : 0,
    labels: {
      send: t('orch.ai.send'),
      sendTitle: t('paper.send'),
      stop: t('branch.stopGen'),
      stopping: t('stop.stopping'),
    },
    onSend: sendMessage,
    onStop: stop,
    requestRefresh: updateSendButton,
  });
  if (typeof _setAgentModeLocked === 'function') _setAgentModeLocked(state.busy);
}

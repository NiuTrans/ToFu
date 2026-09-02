/* ===== migrated source: core/translation_model.js ===== */
/* Translation presentation model.

   Durable translation data lives on the authoritative turn projection. This
   module is intentionally read-only: it converts that wire projection into the
   small view model used by message rendering. Task dispatch and persistence
   belong to translation.js and the server translation runtime respectively. */

/**
 * Read translation presentation state from a projected message.
 *
 * @param {object} message
 * @returns {object}
 */
function readTranslation(message) {
  if (!message || typeof message !== 'object') return { status: 'idle' };

  const translation = {};
  if (Object.prototype.hasOwnProperty.call(message, '_translateDone')) {
    translation.done = message._translateDone;
  }
  if (message._translateError) translation.status = 'error';
  else if (translation.done === true) translation.status = 'done';
  else if (translation.done === false || message._translateTaskId
      || message._translatePartial || message._translateStatus) {
    translation.status = 'pending';
  } else translation.status = 'idle';

  if (message.translatedContent != null) {
    translation.text = message.translatedContent;
  }
  if (Object.prototype.hasOwnProperty.call(message, '_showingTranslation')) {
    translation.showing = message._showingTranslation;
  }
  if (message._translateModel != null) translation.model = message._translateModel;
  if (message._translateError != null) translation.error = message._translateError;
  if (message._translateTaskId != null) translation.taskId = message._translateTaskId;
  if (message._translateStatus != null) translation.statusMsg = message._translateStatus;
  if (message._translateStatusKind != null) {
    translation.statusKind = message._translateStatusKind;
  }
  if (message._translatePartial != null) translation.partial = message._translatePartial;
  if (message._translateFailed != null) translation.sendFailed = message._translateFailed;
  return translation;
}

/**
 * Resolve the source content and renderer independently from translation state.
 *
 * @param {object} message
 * @returns {{text:string, isMarkdown:boolean, stripNoTranslate:boolean}}
 */
function displayContent(message) {
  if (!message || typeof message !== 'object') {
    return { text: '', isMarkdown: false, stripNoTranslate: false };
  }
  const isUser = message.role === 'user' || message.role === 'optimizer';
  const isModelAuthoredUser = isUser
    && (message._isFlowReview || message._isVirtualUser);
  if (isModelAuthoredUser) {
    return {
      text: message.content || '',
      isMarkdown: true,
      stripNoTranslate: false,
    };
  }
  if (isUser) {
    return {
      text: (message.originalContent || message.content) || '',
      isMarkdown: false,
      stripNoTranslate: true,
    };
  }
  return {
    text: message.content || '',
    isMarkdown: true,
    stripNoTranslate: false,
  };
}

/**
 * Translation contribution to the surgical-render fingerprint.
 *
 * @param {object} message
 * @returns {string}
 */
function translationFingerprint(message) {
  const translation = readTranslation(message);
  return (translation.text != null ? String(translation.text).length : 0) + ':'
    + (translation.showing ? 'T' : 'F') + ':'
    + (translation.done === false ? 'P' : '');
}

if (typeof window !== 'undefined') {
  runtimeScope.readTranslation = readTranslation;
  runtimeScope.displayContent = displayContent;
  runtimeScope.translationFingerprint = translationFingerprint;
}

/* ===== migrated source: translation.js ===== */
/* Async translation application adapter.
 *
 * The translation worker commits completed/skipped facts to the backend Turn
 * with CAS. This module owns only task initiation, polling and lifecycle-local
 * presentation; it never mutates a projected message or TurnStore document.
 */

const _TRANSLATE_POLL_FAST_DELAY = 1000;
const _TRANSLATE_POLL_SLOW_DELAY = 2500;
const _translationTasksByTurn = new Map();

const _UILANG_TO_TARGET = {
  zh: 'Chinese', 'zh-cn': 'Chinese', 'zh-tw': 'Chinese',
  en: 'English', ja: 'Japanese', ko: 'Korean',
  fr: 'French', de: 'German', es: 'Spanish', ru: 'Russian',
};
const _TARGET_TO_CODE = {
  Chinese: 'zh', English: 'en', Japanese: 'ja', Korean: 'ko',
  French: 'fr', German: 'de', Spanish: 'es', Russian: 'ru',
};

function _translationTaskKey(convId, turnId) {
  return String(convId || '') + ':' + String(turnId || '');
}

function _uiTranslateTarget() {
  const language = (typeof _i18nLang !== 'undefined' && _i18nLang)
    ? String(_i18nLang).toLowerCase() : 'zh';
  return _UILANG_TO_TARGET[language] || 'Chinese';
}

async function _isAlreadyChinese(text) {
  if (!text || typeof text !== 'string' || text.replace(/\s+/g, '').length < 8) {
    return false;
  }
  const result = await Api.text.detectLanguage(text);
  return Boolean(result?.is_chinese);
}

async function _isAlreadyInTarget(text, targetLanguage) {
  if (!text || typeof text !== 'string' || text.replace(/\s+/g, '').length < 8) {
    return false;
  }
  const expected = _TARGET_TO_CODE[targetLanguage];
  if (!expected) return false;
  const result = await Api.text.detectLanguage(text, { forceFasttext: true });
  return result?.detected?.code === expected;
}

/* Retained generic (non-conversation) translation client used by project and
 * stream helpers. It returns text and owns no conversation state. */
async function _callTranslateAPI(text, targetLang, sourceLang) {
  const result = await Api.translate.run({ text, targetLang, sourceLang });
  if (!result?._ok) {
    const detail = (typeof errorEnvelopeMessage === 'function'
      ? errorEnvelopeMessage(result?.error) : '')
      || (typeof result?.error === 'string' ? result.error : '')
      || 'Translation failed';
    throw new Error(detail);
  }
  return result.translated;
}

function _translationTurn(convId, turnId) {
  return runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(convId)
    ?.getState?.().turnsById?.[turnId] || null;
}

function _setTranslationActivity(convId, turnId, activity) {
  runtimeScope.ConversationSurfacePresentation?.setTranslationActivity?.(
    convId, turnId, activity || null,
  );
}

async function _runManualTurnTranslation(conv, turnId, sourceText) {
  if (!conv || !turnId || !String(sourceText || '').trim()) return;
  _setTranslationActivity(conv.id, turnId, {
    status: 'pending', message: 'Translating…',
  });
  try {
    const alreadyChinese = await _isAlreadyChinese(sourceText);
    await _runTranslationPipeline(conv, turnId, {
      sourceLang: '',
      targetLang: alreadyChinese ? 'English' : 'Chinese',
      field: 'translatedContent',
      skipTargetProbe: true,
    });
  } catch (error) {
    const messageText = error?.message || 'Translation failed';
    _setTranslationActivity(conv.id, turnId, {
      status: 'failed', message: messageText, error: messageText,
    });
    if (typeof showToast === 'function') showToast(messageText, 'error');
  }
}

function _translationErrorText(result) {
  return (typeof errorEnvelopeMessage === 'function'
    ? errorEnvelopeMessage(result?.error) : '')
    || (typeof result?.error === 'string' ? result.error : '')
    || 'Translation failed';
}

function _translationPendingActivity(frame) {
  const partial = typeof frame?.partial === 'string' ? frame.partial : undefined;
  const partialByRound = frame?.partialByRound
      && typeof frame.partialByRound === 'object'
    ? frame.partialByRound : undefined;
  return {
    status: 'pending',
    message: frame?.statusMessage || 'Translating…',
    ...(partial !== undefined ? { partial } : {}),
    ...(partialByRound !== undefined ? { partialByRound } : {}),
  };
}

async function _hydrateTranslatedTurn(conv) {
  if (!conv) return;
  await runtimeScope.ConversationTurnStore?.hydrateConversation?.(conv);
}

async function _startTranslateTask(
  text, targetLang, sourceLang, convId, turnId, field, msgId,
) {
  if (convId && !turnId) {
    throw new Error(
      'Message is not attached to an authoritative turn; reload and retry.',
    );
  }
  const result = await Api.translate.start({
    text, targetLang, sourceLang, convId, turnId, field,
    ...(msgId ? { msgId } : {}),
  });
  if (!result?.taskId) throw new Error('Translation task did not start');
  return result.taskId;
}

async function _pollTranslateTask(taskId) {
  return Api.translate.poll(taskId);
}

async function _pollTranslateTaskBatch(taskIds) {
  if (!taskIds.length) return [];
  const results = await Api.translate.pollBatch(taskIds);
  return Array.isArray(results) ? results : [];
}

async function _settleTranslationTask(conv, turnId, taskId, result) {
  const key = _translationTaskKey(conv.id, turnId);
  const current = _translationTasksByTurn.get(key);
  if (current?.taskId && current.taskId !== taskId) return false;
  _translationTasksByTurn.delete(key);
  if (result?.status === 'done' || result?.noop === true) {
    _setTranslationActivity(conv.id, turnId, null);
    await _hydrateTranslatedTurn(conv);
    return true;
  }
  const error = _translationErrorText(result);
  _setTranslationActivity(conv.id, turnId, {
    status: 'failed', message: error, error,
  });
  if (typeof showToast === 'function') showToast(error, 'error');
  return false;
}

async function _pollTranslationUntilSettled({
  conv, turnId, taskId, field, sourceLang, targetLang,
}) {
  let pollCount = 0;
  while (_translationTasksByTurn.get(
    _translationTaskKey(conv.id, turnId),
  )?.taskId === taskId) {
    const delay = pollCount < 5
      ? _TRANSLATE_POLL_FAST_DELAY : _TRANSLATE_POLL_SLOW_DELAY;
    pollCount += 1;
    await new Promise((resolve) => setTimeout(resolve, delay));
    const result = await _pollTranslateTask(taskId);
    if (result?.status === 'running') {
      _setTranslationActivity(
        conv.id, turnId, _translationPendingActivity(result),
      );
      continue;
    }
    if (result?.status === 'not_found') {
      try { await _hydrateTranslatedTurn(conv); }
      catch (_ignored) { /* terminal presentation below remains actionable */ }
    }
    await _settleTranslationTask(conv, turnId, taskId, result);
    return;
  }
}

async function _markTranslationSkipped(conv, turn, targetLanguage) {
  const projection = {
    ...turn.projection,
    translation: {
      status: 'skipped',
      skippedReason: 'already_target_language',
      targetLanguage,
    },
  };
  await runtimeScope.ConversationTurnStore.updateConversationTurn(
    conv, turn.turnId, projection,
  );
}

async function _runTranslationPipeline(conv, turnId, options) {
  if (!conv) return;
  const turn = typeof turnId === 'string'
    ? _translationTurn(conv.id, turnId) : null;
  if (!turn) {
    const error = 'Message is not attached to an authoritative turn; reload and retry.';
    _setTranslationActivity(conv.id, turnId, null);
    if (typeof showToast === 'function') showToast(error, 'error');
    return;
  }
  const field = options?.field || 'translatedContent';
  const sourceText = options?.text != null
    ? String(options.text) : String(turn.projection.content || '');
  if (!sourceText.trim()) return;
  const targetLanguage = options?.targetLang || _uiTranslateTarget();
  const sourceLanguage = options?.sourceLang || '';
  const key = _translationTaskKey(conv.id, turnId);

  if (!options?.existingTaskId && !options?.skipTargetProbe
      && await _isAlreadyInTarget(sourceText, targetLanguage)) {
    try {
      await _markTranslationSkipped(conv, turn, targetLanguage);
      _setTranslationActivity(conv.id, turnId, null);
    } catch (error) {
      _setTranslationActivity(conv.id, turnId, {
        status: 'failed', message: error?.message || 'Translation failed',
        error: error?.message || error,
      });
    }
    return;
  }

  if (!options?.existingTaskId && typeof translateClaim === 'function'
      && !translateClaim(conv.id, turnId)) return;

  try {
    const taskId = options?.existingTaskId || await _startTranslateTask(
      sourceText,
      targetLanguage,
      sourceLanguage,
      conv.id,
      turnId,
      field,
      '',
    );
    _translationTasksByTurn.set(key, {
      taskId, field, sourceLang: sourceLanguage, targetLang: targetLanguage,
    });
    _setTranslationActivity(conv.id, turnId, {
      status: 'pending', message: 'Translating…',
    });
    await _pollTranslationUntilSettled({
      conv, turnId, taskId, field,
      sourceLang: sourceLanguage, targetLang: targetLanguage,
    });
  } catch (error) {
    _translationTasksByTurn.delete(key);
    const messageText = error?.message || 'Translation failed';
    _setTranslationActivity(conv.id, turnId, {
      status: 'failed', message: messageText, error: messageText,
    });
    if (typeof showToast === 'function') showToast(messageText, 'error');
  } finally {
    if (typeof translateRelease === 'function') {
      translateRelease(conv.id, turnId);
    }
  }
}

/* Page/tab activation only resumes tasks initiated in this browser lifetime.
 * Durable results converge independently through Conversation Sync v3. */
async function _resumePendingTranslations(convId) {
  const conv = conversations.find((item) => item?.id === convId);
  if (!conv) return;
  const pending = [..._translationTasksByTurn.entries()]
    .filter(([key]) => key.startsWith(String(convId) + ':'));
  if (!pending.length) return;
  const results = await _pollTranslateTaskBatch(
    pending.map(([, task]) => task.taskId),
  );
  const byTask = new Map(results.map((result) => [result.taskId, result]));
  for (const [key, task] of pending) {
    const turnId = key.slice(String(convId).length + 1);
    const result = byTask.get(task.taskId);
    if (result?.status === 'running') {
      _setTranslationActivity(
        convId, turnId, _translationPendingActivity(result),
      );
      void _pollTranslationUntilSettled({
        conv, turnId, taskId: task.taskId, field: task.field,
        sourceLang: task.sourceLang, targetLang: task.targetLang,
      });
    } else {
      await _settleTranslationTask(
        conv, turnId, task.taskId,
        result || { status: 'not_found', error: 'Translation task expired' },
      );
    }
  }
}

(function wireServerTranslationProjection() {
  if (typeof pushSubscribe !== 'function'
      || runtimeScope.__translatePushWired) return;
  runtimeScope.__translatePushWired = true;
  pushSubscribe('translate', '*', async (frame) => {
    try {
      if (!frame?.convId || !frame.turnId) return;
      const conv = conversations.find((item) => item?.id === frame.convId);
      if (!conv) return;
      if (frame.status === 'running' || frame.type === 'running') {
        _setTranslationActivity(
          frame.convId, frame.turnId, _translationPendingActivity(frame),
        );
        return;
      }
      if (frame.status === 'done' || frame.type === 'done'
          || frame.noop === true) {
        _setTranslationActivity(frame.convId, frame.turnId, null);
        _translationTasksByTurn.delete(
          _translationTaskKey(frame.convId, frame.turnId),
        );
        await _hydrateTranslatedTurn(conv);
        return;
      }
      if (frame.status === 'error' || frame.type === 'error') {
        const error = _translationErrorText(frame);
        _setTranslationActivity(frame.convId, frame.turnId, {
          status: 'failed', message: error, error,
        });
      }
    } catch (error) {
      console.debug(
        '[Translate] authority adoption failed:', error?.message || error,
      );
    }
  });
})();

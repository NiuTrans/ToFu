/* ═══════════════════════════════════════════
   translation.js — Async Translation Tasks
   ═══════════════════════════════════════════ */

// ═══════════════════════════════════════════════════════════
//  ★ Unified translation pipeline — see _runTranslationPipeline
// ═══════════════════════════════════════════════════════════
//
// Three call sites (manual click / auto-translate / resume-retry) all
// funnel through _runTranslationPipeline.  The shared rules are:
//   • Server persistence: _patchMessageOnServer (targeted PATCH only)
//   • Re-render:          surgical outerHTML of #msg-N
//   • Poll cadence:       2s × 5, then 4s, capped at ~150s
//   • Cache field:        writes translatedContent only; reads also
//                         _translatedCache for backward compat
//   • mode='auto'         retries on terminal failure with a fresh task
//   • mode='manual'       surfaces the error and waits for next click

const _TRANSLATE_POLL_MAX_ATTEMPTS = 40;   // ~150s total budget
const _TRANSLATE_POLL_FAST_DELAY  = 2000;  // first 5 polls
const _TRANSLATE_POLL_SLOW_DELAY  = 4000;  // remaining polls

/* Stale-partial heuristic thresholds. The policy lives in Python
 * (lib/text_lang.is_stale_partial_translation) and is served via
 * /api/v1/server-config → cached on window._translationPolicy. The constants
 * below are only the pre-config-load fallback — never the source of truth. */
const _TRANSLATE_STALE_FRAC_FALLBACK = 0.15;  // <15% → stale partial translation
const _TRANSLATE_STALE_MIN_SOURCE_FALLBACK = 500;

/**
 * Server-side language detection. Mirrors lib/text_lang.is_predominantly_chinese:
 * returns true when the text is predominantly CJK and a target='Chinese'
 * translation pass would be a no-op / wasted call. The threshold + regex
 * policy lives in `lib/text_lang.py`.
 *
 * Async because the network round-trip is invisible — translation starts
 * are user-initiated and already kick off a multi-second LLM call.
 *
 * Returns false on error (fail open: better to translate than to skip
 * a real translation request because of a transient API hiccup).
 */
async function _isAlreadyChinese(text) {
  if (!text || typeof text !== 'string' || text.replace(/\s+/g, '').length < 8) {
    return false;
  }
  const body = await Api.text.detectLanguage(text);
  return !!(body && body.is_chinese);
}

/**
 * Surgical single-message re-render with scroll preservation.
 * Replaces the duplicated outerHTML+scrollTop pattern that lived in
 * three different places across translation flows.
 *
 * `.message` has `content-visibility:auto;contain-intrinsic-size:auto 120px`,
 * so a raw `outerHTML` swap drops the new element's intrinsic-size cache to
 * the 120px estimate. If the real height was, say, 2000px, the page above
 * the swapped node suddenly reports ~1880px shorter and the saved scrollTop
 * resolves to a position near the top — the chat appears to "jump up" when
 * a translation pipeline transitions a message from idle → translating →
 * done. Wrapping the swap in `cv-off` (same trick `_forceScrollToBottom`
 * uses) forces real height computation before scrollTop restoration.
 */
function _renderMsgInPlace(convId, idx, msg) {
  if (typeof activeConvId === 'undefined' || activeConvId !== convId) return;
  if (typeof renderMessage !== 'function') return;
  const el = document.getElementById(`msg-${idx}`);
  // No surgical target: the bubble isn't laid out yet (conversation still
  // loading) or the index drifted.  Do NOT full-renderChat here — that
  // rebuilds the whole DOM and resets scroll to the top (the "jumps to the
  // beginning" bug).  The msg object is already mutated in memory + DB, so
  // the next natural render (or scroll-into-view) shows the translation.
  if (!el) return;
  const ct = document.getElementById('chatContainer');
  const inner = document.getElementById('chatInner');
  const sv = ct ? ct.scrollTop : -1;
  if (inner) {
    inner.classList.add('cv-off');
    void inner.scrollHeight;
  }
  el.outerHTML = renderMessage(msg, idx);
  if (inner) {
    void inner.scrollHeight;
    inner.classList.remove('cv-off');
  }
  if (sv >= 0 && ct) ct.scrollTop = sv;
}

/**
 * Stale-partial detector: a translation produced from mid-stream
 * partial content tends to be a small fraction of the (now-final) source.
 * Thresholds are backend-owned (lib/text_lang.is_stale_partial_translation),
 * served via window._translationPolicy; fall back to constants pre-load.
 */
function _isStalePartialTranslation(msg) {
  if (!msg || !msg.translatedContent || !msg.content) return false;
  const p = (typeof window !== 'undefined' && window._translationPolicy) || null;
  const frac = (p && p.stale_frac > 0) ? p.stale_frac : _TRANSLATE_STALE_FRAC_FALLBACK;
  const minSrc = (p && p.min_source_chars > 0) ? p.min_source_chars : _TRANSLATE_STALE_MIN_SOURCE_FALLBACK;
  return msg.content.length > minSrc &&
         msg.translatedContent.length < msg.content.length * frac;
}

function _resetTranslationState(msg) {
  delete msg.translatedContent;
  delete msg._translatedCache;
  delete msg._translateDone;
  delete msg._translateTaskId;
  delete msg._translateError;
  delete msg._translateStatus;
  delete msg._translateStatusKind;
  delete msg._translatePartial;
}

/**
 * Apply a 'done' translation result to the message and persist it.
 * Handles both fields: 'translatedContent' (assistant bilingual)
 * and 'content' (user-edit translation).
 */
function _applyTranslationDone(convId, idx, msg, result, field) {
  if (field === 'translatedContent') {
    msg.translatedContent = result.translated;
    msg._translatedCache = result.translated;  // backward-compat readers
    msg._showingTranslation = true;
  } else if (field === 'content') {
    if (!msg.originalContent) msg.originalContent = msg.content;
    msg.content = result.translated;
  }
  if (result.model) msg._translateModel = result.model;
  msg._translateDone = true;
  delete msg._translateTaskId;
  delete msg._translateStatus;
  delete msg._translateStatusKind;
  delete msg._translatePartial;
  delete msg._translateError;

  if (typeof saveConversations === 'function') saveConversations(convId);

  // Targeted PATCH — never full-conv PUT (avoids queue races).
  const patch = {
    _translateDone: true,
    _translateModel: msg._translateModel || null,
  };
  if (field === 'translatedContent') {
    patch.translatedContent = msg.translatedContent;
    patch._showingTranslation = true;
  } else if (field === 'content') {
    patch.content = msg.content;
  }
  if (typeof _patchMessageOnServer === 'function') {
    _patchMessageOnServer(convId, idx, patch);
  }

  _renderMsgInPlace(convId, idx, msg);
}

/**
 * Apply a 'running' status update (partial preview, retry status).
 *
 * IMPORTANT: this fires on EVERY poll tick (every 2-4s).  Replacing the whole
 * message via outerHTML each tick had two visible side effects:
 *   1. The .translate-spinner DOM is destroyed/recreated every tick, so the
 *      CSS keyframe animation restarts and the user perceives a frozen
 *      spinner that never advances.
 *   2. content-visibility:auto on .message invalidates intrinsic-size on
 *      every replacement, drifting scrollTop upward until the chat appears
 *      to jump to the top.
 * We now mutate just the .translate-status-sub / .translate-preview-sub
 * children of the existing #translate-loading-N indicator. The spinner DOM
 * survives, scrollTop is untouched, and we only fall back to the legacy
 * full-message re-render if the loading indicator isn't in the DOM yet
 * (e.g. first running tick after a reload).
 */
// Status kinds that are NOT problems — the spinner + "翻译中…" label already
// convey them, so surfacing them as a ⚠ warning sub-line is redundant noise
// (the duplicate "正在调用翻译模型，请稍候…" the user reported). Only genuine
// retry conditions (rate_limited, *_error, truncated, empty_output, …) warrant
// the warning line.
const _TRANSLATE_BENIGN_STATUS_KINDS = new Set(['started', 'in_progress']);

function _applyTranslationStatus(convId, idx, msg, result) {
  let changed = false;
  if (result.statusMessage && result.statusMessage !== msg._translateStatus
      && !_TRANSLATE_BENIGN_STATUS_KINDS.has(result.statusKind || '')) {
    msg._translateStatus = result.statusMessage;
    msg._translateStatusKind = result.statusKind || '';
    changed = true;
  }
  if (result.partial && result.partial !== msg._translatePartial) {
    msg._translatePartial = result.partial;
    changed = true;
  }
  if (!changed) return false;

  if (typeof activeConvId !== 'undefined' && activeConvId === convId) {
    const loadingEl = document.getElementById(`translate-loading-${idx}`);
    if (loadingEl && _patchTranslateLoadingDom(loadingEl, msg)) {
      return true;
    }
    _renderMsgInPlace(convId, idx, msg);
  }
  return true;
}

/**
 * Patch the inner status-sub / preview-sub children of an existing
 * #translate-loading-N element in place. Returns true if the patch was
 * applied (and the caller should NOT fall back to full re-render).
 */
function _patchTranslateLoadingDom(loadingEl, msg) {
  if (!loadingEl) return false;
  // Bail when the indicator already transitioned to the failed state — the
  // caller's full-render path handles that.
  if (msg._translateError) return false;

  // ── status sub-line ──
  let statusEl = loadingEl.querySelector('.translate-status-sub');
  if (msg._translateStatus) {
    const kind = msg._translateStatusKind || '';
    const i18nKey = kind ? `translate.retry.${kind}` : '';
    const localized = (i18nKey && typeof t === 'function') ? t(i18nKey) : '';
    const display = (localized && localized !== i18nKey) ? localized : msg._translateStatus;
    if (!statusEl) {
      statusEl = document.createElement('div');
      statusEl.className = 'translate-status-sub';
      statusEl.style.cssText = 'font-size:11px;color:#f59e0b;margin-top:2px';
      loadingEl.appendChild(statusEl);
    }
    statusEl.title = msg._translateStatus;
    statusEl.textContent = '⚠ ' + display;
  } else if (statusEl) {
    statusEl.remove();
  }

  // ── partial-preview sub-line ──
  let previewEl = loadingEl.querySelector('.translate-preview-sub');
  if (msg._translatePartial) {
    if (!previewEl) {
      previewEl = document.createElement('div');
      previewEl.className = 'translate-preview-sub';
      previewEl.style.cssText = 'font-size:12px;color:var(--text-secondary,#888);margin-top:4px;white-space:pre-wrap;opacity:0.7;max-height:200px;overflow:hidden';
      loadingEl.appendChild(previewEl);
    }
    if (previewEl.textContent !== msg._translatePartial) {
      previewEl.textContent = msg._translatePartial;
    }
  } else if (previewEl) {
    previewEl.remove();
  }
  return true;
}

/**
 * Apply terminal-error state (no retry path was taken).
 */
function _applyTranslationError(convId, idx, msg, errMsg) {
  msg._translateDone = true;
  msg._translateError = errMsg || 'Translation failed';
  delete msg._translateTaskId;
  delete msg._translateStatus;
  delete msg._translateStatusKind;
  delete msg._translatePartial;
  if (typeof saveConversations === 'function') saveConversations(convId);
  if (typeof _patchMessageOnServer === 'function') {
    _patchMessageOnServer(convId, idx, {
      _translateDone: true,
      _translateError: msg._translateError,
      _translateTaskId: null,
    });
  }
  _renderMsgInPlace(convId, idx, msg);
}

/**
 * Try to recover an auto-committed translation from the server when a
 * task expired/errored.  Returns true if recovery succeeded.
 */
async function _tryRecoverFromServer(convId, idx, msg, field) {
  try {
    const data = await Api.conversations.get(convId);
    if (!data) return false;
    const dbMsg = data.messages?.[idx];
    if (!dbMsg) return false;
    if (field === 'translatedContent' && dbMsg.translatedContent) {
      _applyTranslationDone(convId, idx, msg,
        { translated: dbMsg.translatedContent, model: dbMsg._translateModel },
        'translatedContent');
      return true;
    }
    if (field === 'content' && dbMsg.content && msg.originalContent &&
        dbMsg.content !== msg.originalContent) {
      _applyTranslationDone(convId, idx, msg,
        { translated: dbMsg.content, model: dbMsg._translateModel },
        'content');
      return true;
    }
  } catch (e) {
    console.debug('[Translate] DB-recovery check failed:', e?.message);
  }
  return false;
}

/**
 * Shared poll loop — used by manual / auto / resume-retry paths.
 * Returns when the task reaches a terminal state (or attempt cap).
 */
async function _pollTranslationLoop(opts) {
  const { convId, idx, msg, taskId, field, mode } = opts;
  for (let attempt = 0; attempt <= _TRANSLATE_POLL_MAX_ATTEMPTS; attempt++) {
    if (attempt >= _TRANSLATE_POLL_MAX_ATTEMPTS) {
      // Polling timed out — server may still auto-commit, but stop polling here.
      // Treat as a terminal outcome consistent with mode.
      if (mode === 'auto') {
        return await _handleTerminalFailure(opts, 'poll timeout');
      }
      _applyTranslationError(convId, idx, msg, 'Translation timeout');
      return;
    }
    const delay = attempt < 5 ? _TRANSLATE_POLL_FAST_DELAY : _TRANSLATE_POLL_SLOW_DELAY;
    await new Promise(r => setTimeout(r, delay));

    const result = await _pollTranslateTask(taskId);

    if (result.status === 'done' && result.translated) {
      _applyTranslationDone(convId, idx, msg, result, field);
      return;
    }
    if (result.status === 'running') {
      _applyTranslationStatus(convId, idx, msg, result);
      continue;
    }
    // error | not_found — try DB-recovery, then optionally retry.
    if (result.status === 'error' || result.status === 'not_found') {
      const recovered = await _tryRecoverFromServer(convId, idx, msg, field);
      if (recovered) return;
      // result.error is a typed envelope dict from routes/translate.py.
      // Surface its message; fall back to status when missing.
      const _errMsg = (typeof errorEnvelopeMessage === 'function'
                       ? errorEnvelopeMessage(result.error) : '')
                       || (typeof result.error === 'string' ? result.error : '')
                       || result.status;
      return await _handleTerminalFailure(opts, _errMsg);
    }
  }
}

/**
 * Terminal-failure handler — auto mode retries with a fresh task,
 * manual mode surfaces the error and stops.
 */
async function _handleTerminalFailure(opts, errMsg) {
  const { convId, idx, msg, mode, conv, sourceLang, targetLang, field } = opts;
  const convAuto = conv && conv.autoTranslate !== undefined
    ? !!conv.autoTranslate
    : (typeof autoTranslate !== 'undefined' ? !!autoTranslate : false);

  // Auto mode + autoTranslate ON → retry with a fresh task.
  // Bound the retry chain by counting prior attempts on the message.
  if (mode === 'auto' && convAuto && msg.content) {
    const tries = (msg._translateRetryCount || 0) + 1;
    if (tries <= 2) {
      msg._translateRetryCount = tries;
      console.log(`%c[Translate] 🔄 Auto-retry ${tries}/2 for msg ${idx} (prev err: ${errMsg})`,
        'color:#8b5cf6');
      return await _runTranslationPipeline(conv, idx, msg, {
        sourceLang, targetLang, field, mode: 'auto',
      });
    }
    console.warn(`[Translate] Auto-retry budget exhausted for msg ${idx}: ${errMsg}`);
  }
  _applyTranslationError(convId, idx, msg, errMsg);
}

/**
 * UNIFIED TRANSLATION RUNNER — single entry point for manual / auto / retry.
 *
 * @param {object} conv  Conversation object (from `conversations` global)
 * @param {number} idx   Message index within conv.messages
 * @param {object} msg   conv.messages[idx]
 * @param {object} opts
 *   @param {string}  opts.sourceLang     'English' | 'Chinese' | '' (auto)
 *   @param {string}  opts.targetLang     'Chinese' | 'English'
 *   @param {string}  opts.field          'translatedContent' | 'content'
 *   @param {string}  opts.mode           'manual' | 'auto'
 *   @param {string}  [opts.existingTaskId]  resume an already-started task
 *   @param {string}  [opts.text]         override text-to-translate (default msg.content)
 */
async function _runTranslationPipeline(conv, idx, msg, opts) {
  const convId = conv.id;
  const field = opts.field || 'translatedContent';
  const mode  = opts.mode  || 'auto';
  const text  = opts.text != null ? opts.text : msg.content;

  if (!text || !text.trim()) {
    console.debug(`[Translate] skip msg ${idx}: empty text (mode=${mode})`);
    return;
  }

  // Source is already in the target language — skip the LLM round.
  // Without this guard the translator may hallucinate an English
  // "translation" of an already-Chinese message (the bug the user reported:
  // “原文中文 · 译文却是英文”). Marks the message as done so the
  // spinner clears and the bilingual block is not shown.
  // _isAlreadyChinese is async — policy lives in lib/text_lang.py.
  if (opts.targetLang === 'Chinese' && (await _isAlreadyChinese(text))) {
    console.info(`[Translate] skip msg ${idx}: source is already predominantly Chinese (mode=${mode})`);
    msg._translateDone = true;
    msg._translateSkippedReason = 'already_target_language';
    delete msg._translateTaskId;
    delete msg._translateError;
    delete msg._translateStatus;
    delete msg._translateStatusKind;
    delete msg._translatePartial;
    if (typeof saveConversations === 'function') saveConversations(convId);
    if (typeof _patchMessageOnServer === 'function') {
      _patchMessageOnServer(convId, idx, {
        _translateDone: true,
        _translateTaskId: null,
      });
    }
    _renderMsgInPlace(convId, idx, msg);
    return;
  }

  // Re-translate stale partial translations from prior mid-stream runs.
  if (_isStalePartialTranslation(msg)) {
    console.warn(`[Translate] 🔄 Stale partial translation on msg ${idx} ` +
      `(translated=${msg.translatedContent.length} vs content=${msg.content.length}) — re-translating`);
    _resetTranslationState(msg);
  }

  let taskId = opts.existingTaskId || null;
  if (!taskId) {
    try {
      taskId = await _startTranslateTask(
        text, opts.targetLang, opts.sourceLang || '',
        convId, idx, field, msg && msg._msgId
      );
    } catch (e) {
      console.error('[Translate] start failed:', e);
      _applyTranslationError(convId, idx, msg, e?.message || 'Failed to start translation');
      return;
    }
    if (!taskId) {
      _applyTranslationError(convId, idx, msg, 'Failed to start translation task');
      return;
    }
  }

  // Mark message as pending so renderMessage shows the "翻译中…" indicator.
  msg._translateTaskId = taskId;
  msg._translateField  = field;
  msg._translateDone   = false;
  delete msg._translateError;
  if (typeof saveConversations === 'function') saveConversations(convId);
  _renderMsgInPlace(convId, idx, msg);

  // Poll until terminal (success / recovered / final-error).
  await _pollTranslationLoop({
    convId, idx, msg, conv, taskId, field, mode,
    sourceLang: opts.sourceLang, targetLang: opts.targetLang,
  });
}

async function _callTranslateAPI(text, targetLang, sourceLang, timeoutMs) {
  // Scale timeout with text length: large texts need more time for LLM to complete
  if (!timeoutMs) timeoutMs = text.length > 6000 ? 120000 : text.length > 3000 ? 90000 : 60000;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let d;
  try {
    d = await Api.translate.run({ text, targetLang, sourceLang }, { signal: ctrl.signal });
  } catch (fetchErr) {
    clearTimeout(timer);
    if (fetchErr.name === 'AbortError') {
      throw new Error("Translation timed out — server may be overloaded");
    }
    throw fetchErr;
  } finally {
    clearTimeout(timer);
  }
  if (!d._ok) {
    const detail = (typeof errorEnvelopeMessage === 'function'
                    ? errorEnvelopeMessage(d.error) : '')
                    || (typeof d.error === 'string' ? d.error : '')
                    || d._statusText || d._status;
    throw new Error(`Translation failed: ${detail}`);
  }
  if (!d.translated) throw new Error("Translation returned empty result");
  return d.translated;
}

// ═══════════════════════════════════════════════════════════
//  ★ Async Translation Tasks — fire-and-forget, survive page reload
// ═══════════════════════════════════════════════════════════

/**
 * Start a server-side async translation task.
 * Returns taskId immediately. The server will:
 * 1) Run the translation in a background thread
 * 2) Auto-commit the result into the DB conversation
 * So even if the user closes the tab, the translation completes.
 */
async function _startTranslateTask(text, targetLang, sourceLang, convId, msgIdx, field, msgId) {
  try {
    const body = { text, targetLang, sourceLang, convId, msgIdx, field };
    if (msgId) body.msgId = msgId;
    const d = await Api.translate.start(body);
    if (!d || !d.taskId) {
      console.error("[TranslateTask] start failed: no taskId returned");
      return null;
    }
    console.log(`%c[TranslateTask] Started ${d.taskId} for conv=${convId?.slice(0,8)} msg=${msgIdx} msgId=${(msgId||'').slice(0,8)||'-'} field=${field}`, 'color:#8b5cf6');
    return d.taskId;
  } catch (e) {
    console.error("[TranslateTask] start error:", e);
    return null;
  }
}

/**
 * Poll a single translation task. Returns {status, translated?, error?}
 */
async function _pollTranslateTask(taskId) {
  // The v1 alias `/api/v1/agents/translate/poll/{task_id}` returns the
  // same flat-result shape; UI uses the legacy path for tunnel-token
  // sessions (no scope-check overhead). SDK callers should use the
  // v1 alias via tofu_sdk.agents.poll_translate(task_id).
  return await Api.translate.poll(taskId);
}

/**
 * Poll multiple translation tasks in one request.
 */
async function _pollTranslateTaskBatch(taskIds) {
  if (!taskIds.length) return [];
  // v1 batch alias: /api/v1/agents/translate/poll/batch (for SDK callers).
  const data = await Api.translate.pollBatch(taskIds);
  if (!Array.isArray(data)) return taskIds.map(id => ({ taskId: id, status: 'error' }));
  return data;
}

/**
 * Resume any pending translation tasks for a conversation.
 * Called on page load and when switching to a conversation.
 * Scans messages for _translateTaskId without completed translation,
 * polls the server, and applies results.
 */
async function _resumePendingTranslations(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv || !conv.messages) return;

  // ★ FIX: never auto-translate while streaming — content is still growing,
  //   translating partial content produces incomplete translations.
  //   finishStream() will trigger translation when the stream is done.
  if (activeStreams.has(convId)) {
    console.log(`%c[TranslateTask] ⏭ Skipping _resumePendingTranslations — stream active for conv=${convId.slice(0,8)}`, 'color:#8b5cf6');
    return;
  }

  // ── Phase 0: detect the LAST assistant msg that should have been translated but wasn't ──
  // (e.g. page closed before finishStream could start the task)
  // Only check the last assistant message — don't retroactively translate old history
  const _convAutoTranslate = conv.autoTranslate !== undefined ? !!conv.autoTranslate : true;
  if (_convAutoTranslate && conv.messages.length > 0) {
    // Find the last assistant message (skip empty ghost messages)
    for (let i = conv.messages.length - 1; i >= 0; i--) {
      const msg = conv.messages[i];
      if (msg.role !== 'assistant') continue;
      if (!msg.content) continue;  // ★ skip ghost empty assistant msgs, don't break
      // ★ FIX: detect stale partial translations before skipping —
      // if translatedContent exists but is < 15% of content length, it was translated
      // from partial content (mid-stream) and needs re-translation.
      if (_isStalePartialTranslation(msg)) {
        console.warn(`[TranslateTask] 🔄 Stale partial translation in resume — ` +
          `translated=${msg.translatedContent.length} vs content=${msg.content.length} ` +
          `(${(msg.translatedContent.length/msg.content.length*100).toFixed(1)}%) — re-translating msg ${i}`);
        _resetTranslationState(msg);
        _runTranslationPipeline(conv, i, msg, {
          sourceLang: 'English', targetLang: 'Chinese',
          field: 'translatedContent', mode: 'auto',
        });
        break;
      }
      if (msg.translatedContent || msg._translateTaskId || msg._translateDone !== undefined) break;
      // ★ Skip image gen results — nothing meaningful to translate
      if (msg._igResult || msg._isImageGen || msg._igResults) break;
      // ★ FIX: When autoTranslate is ON, always translate — don't rely on the
      // language heuristic which fails for bilingual/mixed-language responses.
      console.log(`%c[TranslateTask] 🔄 Auto-starting missed translation for msg ${i} in conv=${convId.slice(0,8)}`, 'color:#8b5cf6');
      _runTranslationPipeline(conv, i, msg, {
        sourceLang: 'English', targetLang: 'Chinese',
        field: 'translatedContent', mode: 'auto',
      });
      break; // only check the last assistant msg
    }
  }

  // ── Phase 1: collect messages with active translate tasks ──
  const pending = [];
  conv.messages.forEach((msg, idx) => {
    if (msg._translateTaskId && !msg._translateDone) {
      // Check if the translation already landed (e.g. server auto-committed and we reloaded)
      if (msg._translateField === 'translatedContent' && msg.translatedContent) {
        msg._translateDone = true;
        return;
      }
      if (msg._translateField === 'content' && msg.originalContent && msg.content !== msg.originalContent) {
        msg._translateDone = true;
        return;
      }
      pending.push({ taskId: msg._translateTaskId, idx, msg });
    }
  });

  if (!pending.length) return;
  console.log(`%c[TranslateTask] Resuming ${pending.length} pending translations for conv=${convId.slice(0,8)}`, 'color:#8b5cf6;font-weight:bold');

  // ── Phase 2: immediately re-render to show "翻译中…" indicators ──
  // ★ Use forceScroll=false to preserve scroll position (prevents jump-to-top flash)
  if (activeConvId === convId) {
    renderChat(conv, false);
  }

  // ── Phase 3: one-shot batch poll, then hand off to the unified pipeline ──
  const taskIds = pending.map(p => p.taskId);
  const results = await _pollTranslateTaskBatch(taskIds);

  for (const result of results) {
    const p = pending.find(x => x.taskId === result.taskId);
    if (!p) continue;
    const field = p.msg._translateField || 'translatedContent';

    if (result.status === 'done' && result.translated) {
      _applyTranslationDone(convId, p.idx, p.msg, result, field);
      console.log(`%c[TranslateTask] ✓ Applied ${result.taskId} to msg ${p.idx} (${field})`, 'color:#22c55e');
      continue;
    }

    if (result.status === 'running') {
      // Surface any retry-status / partial preview so the user sees progress,
      // then attach the unified poll loop so this in-flight task uses the
      // same cadence and apply/error path as fresh tasks.
      _applyTranslationStatus(convId, p.idx, p.msg, result);
      _runTranslationPipeline(conv, p.idx, p.msg, {
        sourceLang: '', targetLang: '',
        field, mode: 'auto', existingTaskId: p.taskId,
      });
      continue;
    }

    if (result.status === 'error' || result.status === 'not_found') {
      console.warn(`[TranslateTask] ${result.taskId} ${result.status} — checking DB for auto-committed translation...`);
      const recovered = await _tryRecoverFromServer(convId, p.idx, p.msg, field);
      if (recovered) continue;
      // No DB recovery available — auto-mode pipeline will retry, manual will surface error.
      if (_convAutoTranslate && field === 'translatedContent' && p.msg.content) {
        p.msg._translateTaskId = null;
        delete p.msg._translateError;
        _runTranslationPipeline(conv, p.idx, p.msg, {
          sourceLang: 'English', targetLang: 'Chinese',
          field: 'translatedContent', mode: 'auto',
        });
      } else {
        const _errMsg = (typeof errorEnvelopeMessage === 'function'
                         ? errorEnvelopeMessage(result.error) : '')
                         || (typeof result.error === 'string' ? result.error : '')
                         || 'Task expired';
        _applyTranslationError(convId, p.idx, p.msg, _errMsg);
      }
    }
  }
}

// ═══════════════════════════════════════════════════════════
//  ★ Server-push subscriber — render server-side auto-translations live
//
// Auto-translate is now driven entirely by the backend safety net
// (lib/tasks_pkg/manager.py::_maybe_auto_translate_assistant). It runs
// after the assistant content is persisted, so the SSE chat stream is
// already closed and the frontend has no direct signal that the
// translation finished. Without this listener the bilingual block only
// surfaces when the user switches conversations and loadConversationMessages
// re-reads from the DB.
//
// We subscribe with taskId='*' to receive every translate event, then
// route it to the right message by convId+msgId (preferred) or msgIdx.
// _applyTranslationDone is idempotent and patches the DOM in place.
// ═══════════════════════════════════════════════════════════
(function _wireServerPushTranslate() {
  if (typeof pushSubscribe !== 'function') return;
  if (window.__translatePushWired) return;
  window.__translatePushWired = true;

  pushSubscribe('translate', '*', (frame) => {
    try {
      if (!frame || frame.status !== 'done' || !frame.translated) return;
      const convId = frame.convId;
      if (!convId) return;
      const conv = (typeof conversations !== 'undefined')
        ? conversations.find(c => c.id === convId) : null;
      if (!conv || !conv.messages) return;

      // Resolve target message: prefer stable msgId, fall back to msgIdx.
      let idx = -1, msg = null;
      if (frame.msgId) {
        idx = conv.messages.findIndex(m => m && m._msgId === frame.msgId);
        if (idx >= 0) msg = conv.messages[idx];
      }
      if (!msg && frame.msgIdx !== null && frame.msgIdx !== undefined) {
        idx = frame.msgIdx;
        msg = conv.messages[idx];
      }
      if (!msg) return;

      const field = frame.field || 'translatedContent';

      // Skip when the message already has the translation (manual click /
      // poll loop already applied it) — _applyTranslationDone is safe to
      // re-run but writing again would trigger a redundant PATCH + render.
      if (field === 'translatedContent' && msg.translatedContent === frame.translated) return;
      if (field === 'content' && msg.content === frame.translated) return;

      // The poll loop is the authoritative path for client-initiated tasks;
      // skip when it's still running so we don't race with it.
      if (msg._translateTaskId && !msg._translateDone) return;

      console.log(`%c[Translate] ← push frame applied conv=${convId.slice(0,8)} msg=${idx}`,
        'color:#22c55e');
      // The server has already committed (or is about to) — skip the
      // frontend PATCH to avoid a redundant write race. Just update the
      // in-memory state and re-render the bubble in place.
      if (field === 'translatedContent') {
        msg.translatedContent = frame.translated;
        msg._translatedCache = frame.translated;
        msg._showingTranslation = true;
      } else if (field === 'content') {
        if (!msg.originalContent) msg.originalContent = msg.content;
        msg.content = frame.translated;
      }
      if (frame.model) msg._translateModel = frame.model;
      msg._translateDone = true;
      delete msg._translateTaskId;
      delete msg._translateStatus;
      delete msg._translateStatusKind;
      delete msg._translatePartial;
      delete msg._translateError;
      if (typeof saveConversations === 'function') saveConversations(convId);
      _renderMsgInPlace(convId, idx, msg);
    } catch (e) {
      console.debug('[Translate] push handler error:', e?.message);
    }
  });
})();

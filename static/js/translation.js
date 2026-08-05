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

/* ── OUTPUT-side (model reply → human) translate target ──
 * The assistant reply is rendered into the UI language, not a hard-pinned
 * Chinese. Maps _i18nLang to the language NAME the translate engine expects
 * (mirrors lib/conv_config._UILANG_TO_TARGET). Falls back to Chinese so an
 * unknown/absent UI language is byte-identical to the former hard-pin. */
const _UILANG_TO_TARGET = {
  zh: 'Chinese', 'zh-cn': 'Chinese', 'zh-tw': 'Chinese',
  en: 'English', ja: 'Japanese', ko: 'Korean',
  fr: 'French', de: 'German', es: 'Spanish', ru: 'Russian',
};
function _uiTranslateTarget() {
  const code = (typeof _i18nLang !== 'undefined' && _i18nLang)
    ? String(_i18nLang).toLowerCase() : 'zh';
  return _UILANG_TO_TARGET[code] || 'Chinese';
}
/* Target-language NAME → detector code (detected.code from the backend),
 * so the skip gate compares like with like. */
const _TARGET_TO_CODE = {
  Chinese: 'zh', English: 'en', Japanese: 'ja', Korean: 'ko',
  French: 'fr', German: 'de', Spanish: 'es', Russian: 'ru',
};

/* Generic "already in the target language?" skip gate. Uses the cascade
 * detector's `detected.code` (fastText-backed server-side) instead of the
 * coarse is_chinese flag, so a Japanese reply into a Japanese UI is not
 * mistaken for its target and a genuinely-target reply is still short-
 * circuited. Fail-open (translate) on any error. */
async function _isAlreadyInTarget(text, targetLang) {
  if (!text || typeof text !== 'string' || text.replace(/\s+/g, '').length < 8) {
    return false;
  }
  const wantCode = _TARGET_TO_CODE[targetLang];
  if (!wantCode) return false;
  // force_fasttext: this gate decides whether to translate; the script+
  // heuristic tier cannot separate kanji-heavy Japanese from Chinese, so
  // without the statistical model a JP reply would be wrongly skipped as
  // already-in-target (the same bug the server-side safety net force-fixes).
  const body = await Api.text.detectLanguage(text, { forceFasttext: true });
  const code = body && body.detected && body.detected.code;
  return !!(code && code === wantCode);
}

/* _renderMsgInPlace, _patchTranslateLoadingDom, _renderStreamingTranslatePreview
 * and _applyPartialByRoundToSettled were RELOCATED to ui/translation_render.js
 * (decoupling step 4). This engine module no longer touches the DOM — it
 * mutates msg state, persists, and calls emitMessageChanged(convId, idx, msg,
 * detail) (+ the window-exposed live-preview painters) to request a repaint. */

/**
 * Stale-partial detector: a translation produced from mid-stream
 * partial content tends to be a small fraction of the (now-final) source.
 * Thresholds are backend-owned (lib/text_lang.is_stale_partial_translation),
 * served via window._translationPolicy; fall back to constants pre-load.
 */
function _isStalePartialTranslation(msg) {
  // Single source of truth: the pure predicate in core/translation_model.js.
  // (Kept as a thin local alias so existing call sites read naturally.)
  return isStalePartialTranslation(msg);
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
 * Stamp per-round translated Chinese onto msg.segments[].translatedText,
 * keyed by llmRound. `byRound` is {String(round): 中文} from the translate
 * `done` frame. Mirrors the backend _stamp_segment_translations so the LIVE
 * settled timeline keeps its narration in Chinese at finalize WITHOUT a reload
 * (a reconnect reads the same already-stamped segments from the DB). No-op
 * when the message has no segments (pre-v36) or the map is empty.
 */
function _stampSegTranslations(msg, byRound) {
  if (!byRound || !Object.keys(byRound).length) return;
  /* ★ Order-independent: the translate `done` push and the chat `done`
   *   (which projects msg.segments) are near-concurrent, and the deliverable
   *   reuses a CACHED segment translation so the translate frame often wins the
   *   race — landing BEFORE segments exist. Stash the map so whichever event
   *   arrives second applies it (the sse_pipeline chat-`done` handler replays
   *   _pendingSegTranslations right after projecting committedMessage.segments). */
  if (!Array.isArray(msg.segments) || !msg.segments.length) {
    msg._pendingSegTranslations = byRound;
    return;
  }
  for (const seg of msg.segments) {
    if (!seg || seg.type !== 'text' || seg.deliverable) continue;
    if (seg.llmRound == null) continue;
    const zh = byRound[String(seg.llmRound)];
    if (zh && zh.trim()) seg.translatedText = zh;
  }
  delete msg._pendingSegTranslations;
}

/**
 * ORDER-INDEPENDENT finalize gate — should the translate `done` DEFER its
 * whole-bubble repaint?
 *
 * The auto-translate `done` push and the chat `done` SSE event are two
 * INDEPENDENT async completions with no ordering guarantee. The chat `done`
 * handler is the SOLE writer of the backend-committed settled fields
 * (toolRounds/segments/finishReason/usage/cost — the `committedMessage`
 * verbatim projection in ui/sse_pipeline.js); the translate `done` handler only
 * stamps translatedContent/_showingTranslation.
 *
 * When the translate frame WINS the race (arrives while the turn is still
 * finalizing, before the chat-`done` projection), an unconditional
 * emitMessageChanged({kind:'full'}) re-runs renderMessage against a message
 * whose settled fields have NOT landed — so the tool panel vanishes and
 * renderFinishInfo (gated on finishReason||usage) degrades to a bare model tag,
 * self-healing a beat later when chat-`done` + finishStream render the complete
 * bubble. That transient flash is the reported "giant purple box, tools gone,
 * stripped finish bar, then normal again".
 *
 * Root fix (NOT a timing tweak): DEFER the whole-bubble repaint iff the turn is
 * NOT yet settled AND a live stream / active task is still finalizing this conv
 * — because the imminent chat-`done` committedMessage projection + finishStream
 * will render the COMPLETE bilingual bubble in one pass (that render reads the
 * already-stamped translatedContent, so the translation is never lost). Repaint
 * IMMEDIATELY when the turn is already settled (finishReason||usage present — the
 * common / manual / historical retranslation case) or when nothing is finalizing
 * (no other render is coming, so the translation must paint itself). The
 * decision is order-independent: whichever `done` arrives second renders once,
 * complete.
 *
 * @param {string} convId
 * @param {object} msg
 * @returns {boolean} true → skip the whole-bubble repaint (a settled render is imminent)
 */
function _translateFinalizeShouldDefer(convId, msg) {
  if (!msg) return false;
  // Settled: chat-`done` already projected the terminal fields, so renderMessage
  // + renderFinishInfo have everything → repaint now.
  if (msg.finishReason || msg.usage) return false;
  // Not settled → only defer while the turn is genuinely finalizing (a live
  // stream or an active task owns this conv). Otherwise nothing else will
  // repaint, so paint now rather than strand the translation.
  try {
    if (typeof activeStreams !== 'undefined' && activeStreams
        && typeof activeStreams.has === 'function' && activeStreams.has(convId)) {
      return true;
    }
    if (typeof conversations !== 'undefined' && Array.isArray(conversations)) {
      const _c = conversations.find((c) => c && c.id === convId);
      if (_c && _c.activeTaskId) return true;
    }
  } catch (e) {
    return false;  // fail open — repaint rather than swallow the translation
  }
  return false;
}
if (typeof window !== 'undefined') {
  window._translateFinalizeShouldDefer = _translateFinalizeShouldDefer;
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

  /* ★ Order-independent finalize: skip the whole-bubble repaint when a settled
   *   render is imminent (chat-`done` projection + finishStream), so the tool
   *   panel / finish bar are never dropped by a translate frame that won the
   *   race. translatedContent is already stamped above; that render reads it. */
  if (!_translateFinalizeShouldDefer(convId, msg)) {
    emitMessageChanged(convId, idx, msg, { kind: 'full' });
  }
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
 * We now mutate just the .translate-status-sub / .translate-preview
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
    emitMessageChanged(convId, idx, msg, { kind: 'status' });
  }
  return true;
}

/* _patchTranslateLoadingDom / _renderStreamingTranslatePreview /
 * _applyPartialByRoundToSettled relocated to ui/translation_render.js
 * (decoupling step 4). Called via emitMessageChanged + the window-exposed
 * live-preview painters. */

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
  emitMessageChanged(convId, idx, msg, { kind: 'full' });
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
    /* ★ pt_3ea0e045: adopt the server's persisted no-op settle marker too.
     *   When the safety net judged the deliverable already-target it stamps
     *   _translateDone:true (no translatedContent) on the row — the dropped-
     *   frame case self-heals HERE, on the watchdog's own probe, so the loop
     *   clears on its FIRST tick instead of burning the full 90s budget. */
    if (field === 'translatedContent' && dbMsg._translateDone === true) {
      msg._translateDone = true;
      delete msg._translateStatus;
      delete msg._translateStatusKind;
      delete msg._translatePartial;
      if (typeof emitMessageChanged === 'function') emitMessageChanged(convId, idx, msg, { kind: 'full' });
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

/* ═══════════════════════════════════════════════════════════
 *  Server-driven auto-translate WATCHDOG
 *
 *  The server-side auto-translate path (lib/translate/runtime.py) delivers
 *  its result to the live view through a SINGLE fire-and-forget push frame
 *  ('done' on the 'translate' channel). The push hub (lib/agent_core/push.py)
 *  DROPS frames when no client is subscribed at emit time (socket reconnect,
 *  backgrounded tab, per-client queue overflow) and offers no Last-Event-ID
 *  replay. When that 'done' frame is lost the active view is stuck: either the
 *  "翻译中…" spinner spins forever (a 'running' frame landed but 'done' didn't)
 *  or the bare original shows with no译文 — until the user switches
 *  conversations and loadConversationMessages re-reads the committed
 *  translatedContent from the DB. (The reported "卡住" + "来回切换才显示" bug.)
 *
 *  This watchdog closes that gap: once a message enters the server-driven
 *  pending state, poll the DB (where _do_translate auto-commits regardless of
 *  push delivery) and apply the translation in place, or clear the stuck
 *  indicator after a budget. Idempotent + keyed so concurrent arms collapse
 *  to a single timer. The first tick is delayed so the normal (push lands
 *  fast) case clears with ZERO extra DB fetches — the guards below short-
 *  circuit before _tryRecoverFromServer once translatedContent is present.
 * ═══════════════════════════════════════════════════════════ */
const _AUTO_TRANSLATE_WATCHDOG = new Map();
const _AT_WATCHDOG_FIRST_DELAY = 8000;
const _AT_WATCHDOG_INTERVAL    = 6000;
const _AT_WATCHDOG_BUDGET_MS   = 90000;

function _autoTranslateWatchdogKey(convId, msg, idx) {
  return `${convId}:${(msg && msg._msgId) || ('idx' + idx)}`;
}

function _armAutoTranslateWatchdog(convId, idx, msg) {
  if (!msg) return;
  // Only meaningful for the active view — a background conv re-reads the DB
  // (with the committed translation) on its next loadConversationMessages.
  if (typeof activeConvId === 'undefined' || activeConvId !== convId) return;
  const key = _autoTranslateWatchdogKey(convId, msg, idx);
  if (_AUTO_TRANSLATE_WATCHDOG.has(key)) return;  // already watching

  const startedAt = Date.now();
  const state = {};

  function _clear() {
    if (state.timer) clearTimeout(state.timer);
    _AUTO_TRANSLATE_WATCHDOG.delete(key);
  }

  async function _tick() {
    const conv = (typeof conversations !== 'undefined')
      ? conversations.find(c => c.id === convId) : null;
    if (!conv || !Array.isArray(conv.messages)) { _clear(); return; }

    // Re-resolve the message + index (multi-turn inserts can drift idx).
    let mIdx = idx, m = conv.messages[idx];
    if (msg._msgId) {
      const fi = conv.messages.findIndex(x => x && x._msgId === msg._msgId);
      if (fi >= 0) { mIdx = fi; m = conv.messages[fi]; }
    }
    if (!m) { _clear(); return; }

    // Resolved by some other path (push landed late / manual click / error).
    if (m.translatedContent || m._translateDone === true || m._translateError) { _clear(); return; }
    // A client poll loop now owns this task — defer to it.
    if (m._translateTaskId) { _clear(); return; }

    // The translation auto-commits to the DB regardless of push delivery.
    const recovered = await _tryRecoverFromServer(convId, mIdx, m, 'translatedContent');
    if (recovered) {
      console.log(`%c[Translate watchdog] ✓ recovered translation from DB for ${key}`, 'color:#22c55e');
      _clear();
      return;
    }

    if (Date.now() - startedAt > _AT_WATCHDOG_BUDGET_MS) {
      // Give up — clear the stuck "翻译中…" indicator so the user at least
      // sees the (untranslated) message instead of an eternal spinner. The
      // original is intact; a manual click can re-trigger translation.
      console.warn(`[Translate watchdog] budget exhausted for ${key} — clearing stuck indicator`);
      delete m._translateDone;
      delete m._translateStatus;
      delete m._translateStatusKind;
      delete m._translatePartial;
      emitMessageChanged(convId, mIdx, m, { kind: 'full' });
      _clear();
      return;
    }
    state.timer = setTimeout(_tick, _AT_WATCHDOG_INTERVAL);
  }

  state.timer = setTimeout(_tick, _AT_WATCHDOG_FIRST_DELAY);
  _AUTO_TRANSLATE_WATCHDOG.set(key, state);
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
  const convAuto = convAutoTranslate(conv);

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
  // Generic already-in-target skip (fastText-backed detected.code) — replaces
  // the Chinese-only is_chinese gate so a Japanese reply into a Japanese UI is
  // not mistaken for its own target and a genuinely-target reply still skips.
  if (await _isAlreadyInTarget(text, opts.targetLang)) {
    console.info(`[Translate] skip msg ${idx}: source is already in target ${opts.targetLang} (mode=${mode})`);
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
    emitMessageChanged(convId, idx, msg, { kind: 'full' });
    return;
  }

  // Re-translate stale partial translations from prior mid-stream runs.
  if (_isStalePartialTranslation(msg)) {
    console.warn(`[Translate] 🔄 Stale partial translation on msg ${idx} ` +
      `(translated=${msg.translatedContent.length} vs content=${msg.content.length}) — re-translating`);
    _resetTranslationState(msg);
  }

  // ── Pre-start dedup: claim the per-(conv,msgId) in-flight guard ──
  // A manual Translate click, the auto-translate pipeline, the page-load
  // resume, and the server safety-net's push frame can all target the SAME
  // message. The first to claim wins; the rest stand down so two tasks don't
  // both run + render + commit one message (the slower clobbering the faster).
  // Resuming an already-started task (existingTaskId) is the SAME logical
  // translation — it already owns the claim, so don't re-gate it.
  const _guardMsgId = (msg && msg._msgId) || '';
  if (!opts.existingTaskId &&
      typeof translateClaim === 'function' &&
      !translateClaim(convId, _guardMsgId, idx)) {
    console.info(`[Translate] msg ${idx} already in-flight — standing down (FE dedup)`);
    return;
  }

  try {
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
    emitMessageChanged(convId, idx, msg, { kind: 'full' });

    // Poll until terminal (success / recovered / final-error).
    await _pollTranslationLoop({
      convId, idx, msg, conv, taskId, field, mode,
      sourceLang: opts.sourceLang, targetLang: opts.targetLang,
    });
  } finally {
    // Release the in-flight guard on EVERY exit (success / error / timeout)
    // so a legitimate later re-translate (e.g. the message is edited) can
    // claim it again. A resume (existingTaskId) inherited an existing claim;
    // releasing it here is correct — this call is the one driving it now.
    if (typeof translateRelease === 'function') {
      translateRelease(convId, _guardMsgId, idx);
    }
  }
}

async function _callTranslateAPI(text, targetLang, sourceLang) {
  /* NO client-side timeout. Translation is an LLM GENERATION — the same class
   * of wait as a chat turn, whose read timeout was removed from the transport
   * (lib/llm/_transport.py). The old ceiling scaled 60s/90s/120s by text
   * length, i.e. it conceded the right value is unknowable, and a long
   * document that legitimately needed longer was reported to the user as
   * "server may be overloaded" while the server was still translating.
   *
   * No `timeoutMs` escape hatch either: no caller passed one, so a dormant
   * conditional timer would just be dead code that reads like a sanctioned
   * ceiling. Re-adding a bound is a deliberate act — and trips the ratchet in
   * tests/test_frontend_no_client_timeouts.py, which is the review gate. */
  const d = await Api.translate.run({ text, targetLang, sourceLang });
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

  // ── Phase 0 (self-heal): clear ZOMBIE pending-translate state on ANY message ──
  // The server-driven auto-translate path sets msg._translateDone=false (+ a
  // _translatePartial preview) via the push 'running' frame but NEVER sets
  // _translateTaskId — the SERVER owns the task, there is no client poll loop.
  // If the terminal 'done' frame is dropped (push hub has no replay) AND the
  // watchdog never healed it, the message is stranded with
  //   _translateDone===false  &&  no _translateTaskId  &&  no translatedContent
  // — a permanent "翻译中…" spinner. NO other resume path recovers it: the
  // Phase-0b scan below only inspects the LAST assistant, and Phase 1 only
  // collects messages that HAVE a _translateTaskId. So a zombie on a
  // non-last assistant (the reported "卡住" screenshot) spins forever across
  // reloads. Scan EVERY message; prefer the server-committed translation,
  // else drop the stuck markers so the original text shows instead.
  let _zombieHealed = false;
  for (let i = 0; i < conv.messages.length; i++) {
    const m = conv.messages[i];
    if (!m) continue;
    if (m._translateDone !== false) continue;            // only the pending state
    if (m._translateTaskId) continue;                    // an owner exists → Phase 1 / poll loop drives it
    if (m.translatedContent) continue;                   // already translated
    if (m._igResult || m._isImageGen || m._igResults) continue;  // nothing to translate
    console.warn(`[TranslateTask] 🧟 Zombie pending-translate on msg ${i} ` +
      `(conv=${convId.slice(0,8)}, no taskId + no translatedContent) — self-healing`);
    const _zField = m._translateField || 'translatedContent';
    const recovered = await _tryRecoverFromServer(convId, i, m, _zField);
    if (recovered) {
      console.log(`%c[TranslateTask] ✓ zombie msg ${i} recovered server translation`, 'color:#22c55e');
      _zombieHealed = true;
      continue;
    }
    // No server-committed translation — clear the stuck indicator so the user
    // sees the (untranslated) original instead of an eternal spinner. The
    // content is intact; a manual click can re-trigger translation.
    delete m._translateDone;
    delete m._translatePartial;
    delete m._translateStatus;
    delete m._translateStatusKind;
    emitMessageChanged(convId, i, m, { kind: 'full' });
    _zombieHealed = true;
  }
  if (_zombieHealed && typeof saveConversations === 'function') saveConversations(convId);

  // ── Phase 0b: retro-translate EVERY untranslated display-translated message ──
  // (e.g. page closed before finishStream could start the task, OR the conv was
  //  frozen autoTranslate=OFF at send-time but the global toggle is ON now.)
  // The "should this translate?" decision is the SINGLE pure predicate
  // needsTranslation(msg, conv, {autoTranslateOn}) in core/translation_model.js —
  // the same one-authority discipline as displayContent/readTranslation. It
  // folds in the effective-toggle, role, has-content, image-gen skip, already-
  // done/in-flight, and stale-partial rules, so this loop is a thin driver.
  //
  // Sweep the WHOLE loaded window (not last-only): turning auto-translate ON
  // must translate ALL still-untranslated history the next time the conv opens,
  // not just the last reply (the reported coverage gap). Every dispatch is
  // idempotent — the pipeline's translateClaim + the server-side claim_inflight
  // guard prevent double-firing against the safety net or a manual click, and
  // needsTranslation returns false for anything already owned/done. The
  // streaming early-return above guarantees this only acts on finished,
  // persisted messages. The async already-Chinese short-circuit stays inside
  // _runTranslationPipeline (it needs a network round-trip, so it can't live in
  // the pure predicate).
  const _autoOn = convAutoTranslateEffective(conv);
  if (_autoOn && conv.messages.length > 0) {
    for (let i = 0; i < conv.messages.length; i++) {
      const msg = conv.messages[i];
      if (!needsTranslation(msg, conv, { autoTranslateOn: true })) continue;
      if (isStalePartialTranslation(msg)) {
        console.warn(`[TranslateTask] 🔄 Stale partial translation in resume — ` +
          `re-translating msg ${i} (conv=${convId.slice(0,8)})`);
        _resetTranslationState(msg);
      } else {
        console.log(`%c[TranslateTask] 🔄 Auto-starting missed translation for msg ${i} in conv=${convId.slice(0,8)}`, 'color:#8b5cf6');
      }
      _runTranslationPipeline(conv, i, msg, {
        sourceLang: 'English', targetLang: _uiTranslateTarget(),
        field: 'translatedContent', mode: 'auto',
      });
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
    emitMessageChanged(convId, -1, null, { kind: 'conv', conv });
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
      if (convAutoTranslate(conv) && field === 'translatedContent' && p.msg.content) {
        p.msg._translateTaskId = null;
        delete p.msg._translateError;
        _runTranslationPipeline(conv, p.idx, p.msg, {
          sourceLang: 'English', targetLang: _uiTranslateTarget(),
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
      if (!frame) return;
      const _isRunning = frame.status === 'running' || frame.type === 'running';
      const _isError = frame.status === 'error' || frame.type === 'error';
      // Accept running, done-with-translation, error, AND no-op verdict
      // frames. The server-driven auto-translate has no client poll loop, so
      // its terminal frames (worker _do_translate's error / the safety net's
      // already-target no-op) are the ONLY signals the live view gets —
      // dropping them leaves the pending state lit forever and keeps the
      // DB-polling watchdog burning full-conv GETs for a translation that
      // will never exist (pt_3ea0e045).
      if (!_isRunning && !_isError && (frame.status !== 'done' || (!frame.translated && frame.noop !== true))) return;
      const convId = frame.convId;
      if (!convId) return;
      const conv = (typeof conversations !== 'undefined')
        ? conversations.find(c => c.id === convId) : null;
      if (!conv || !conv.messages) return;

      // Resolve target message by STABLE id first (id-anchored, mirrors the
      // backend). CRITICAL: when the frame carries a msgId we resolve ONLY by
      // id — a non-match means the row moved / isn't in memory, and the raw
      // msgIdx is then stale (positional drift after a truncation/insert would
      // paint the WRONG bubble). The positional index is used ONLY when the
      // frame has no id at all (legacy frames).
      let idx = -1, msg = null;
      if (frame.msgId) {
        idx = conv.messages.findIndex(m => m && m._msgId === frame.msgId);
        if (idx >= 0) msg = conv.messages[idx];
        // id present but not found → do NOT fall back to msgIdx; drop.
      } else if (frame.msgIdx !== null && frame.msgIdx !== undefined) {
        idx = frame.msgIdx;
        if (idx >= 0 && idx < conv.messages.length) msg = conv.messages[idx];
      }
      if (!msg) return;

      const field = frame.field || 'translatedContent';

      // The poll loop is the authoritative path for client-initiated tasks;
      // skip when it's still running so we don't race with it.
      if (msg._translateTaskId && !msg._translateDone) return;

      // ── No-op terminal verdict (pt_3ea0e045): the server-side safety net
      //    judged the deliverable ALREADY in the target language — no
      //    translatedContent will ever exist for it. Settle the message
      //    (_translateDone=true, the canonical "settled, show original"
      //    tri-state) so the "translating…" indicator and the auto-translate
      //    watchdog stand down for good — including on the next click-open
      //    (finishStream's translatable-tail finder skips settled messages).
      if (frame.noop === true) {
        if (field !== 'translatedContent') return;
        msg._translateDone = true;
        delete msg._translateStatus;
        delete msg._translateStatusKind;
        delete msg._translatePartial;
        delete msg._translatePartialByRound;
        try { if (typeof ConvCache !== 'undefined') ConvCache.put(conv); } catch (_e) { /* best-effort */ }
        emitMessageChanged(convId, idx, msg, { kind: 'full' });
        return;
      }

      // ── Running frame: surface the live "translating…" indicator + the
      //    streaming partial preview for the SERVER-driven auto-translate
      //    path (which has no client poll loop). Without this the active
      //    view shows the bare finished English message with no hint a
      //    translation is on its way until the final 'done' frame lands. ──
      if (_isRunning) {
        if (field !== 'translatedContent') return;
        if (msg.translatedContent || msg._translateDone) return;
        /* ★ Finalize 'started' frame (statusKind='started', no partial): the
         * incremental worker is about to assemble + commit the final Chinese.
         * Hide the English live-tail in the content zone NOW so the final
         * round's English doesn't briefly double with its just-arriving
         * Chinese (the only overlap case — a no-tool-call final round streams
         * into content AND its segment lands in the partial). Pure class
         * toggle on the streaming body; a no-op if this isn't the live
         * bubble. */
        if ((frame.statusKind === 'started') && frame.msgId) {
          _markStreamXlateFinal(frame.msgId);
        }
        // Stash the partial on the message so a later full re-render (conv
        // switch / finalize) still has it. Stash the per-round map too so a
        // repaint after a body rebuild keeps the interleaved layout.
        if (frame.partial) msg._translatePartial = frame.partial;
        if (frame.partialByRound) msg._translatePartialByRound = frame.partialByRound;
        if (frame.statusMessage) {
          msg._translateStatus = frame.statusMessage;
          msg._translateStatusKind = frame.statusKind || '';
        }
        // ── LIVE per-round preview into the still-streaming bubble ──
        //    During the task the assistant reply is rendered in #streaming-msg
        //    (no msg-${idx} element yet), so the normal indicator paths can't
        //    target it. When this frame's message is the one streaming, paint
        //    the translated-so-far into the streaming bubble directly. This is
        //    what makes the Chinese fill in round-by-round DURING the task. ──
        if (frame.partial && _renderStreamingTranslatePreview(convId, frame.msgId, frame.partial, frame.partialByRound)) {
          return;
        }
        /* ★ UNIFICATION (settled per-round stream): the message is SETTLED
         * (#msg-N, not the live #streaming-msg) — the retro / on-open /
         * frozen-OFF whole-message path is streaming its narration
         * round-by-round via partialByRound. Paint each round's Chinese
         * surgically into its existing .seg-narration[data-seg-round] slot so
         * the settled bubble fills in progressively, exactly like the live
         * path — no whole-bubble swap, no hang-then-flash. When it paints at
         * least one round we're done for this frame; otherwise fall through to
         * the spinner indicator below (e.g. the narration slots aren't in the
         * DOM yet, or this is a pre-segment 'started' frame). */
        if (frame.partialByRound
            && _applyPartialByRoundToSettled(convId, idx, frame.partialByRound)) {
          if (msg._translateDone !== false) msg._translateDone = false;
          return;
        }
        // Fallback (settled message, e.g. end-of-task finalize spinner):
        // flip into the pending state so renderMessage shows the indicator.
        if (msg._translateDone !== false) {
          msg._translateDone = false;
          emitMessageChanged(convId, idx, msg, { kind: 'full' });
        }
        _applyTranslationStatus(convId, idx, msg, {
          statusMessage: frame.statusMessage || '',
          statusKind: frame.statusKind || '',
          partial: frame.partial || '',
        });
        // Self-heal: the terminal 'done' frame is fire-and-forget and may be
        // dropped (socket reconnect / queue overflow). Arm a DB-polling
        // watchdog so the live view recovers without a conversation switch.
        _armAutoTranslateWatchdog(convId, idx, msg);
        return;
      }

      // ── Terminal error frame (server-driven auto-translate exhausted its
      //    retries). No client poll loop owns this task, so this push frame is
      //    the authoritative failure signal. Surface the subtle click-to-retry
      //    annotation instead of leaving the bare English with no hint. ──
      if (_isError) {
        if (field !== 'translatedContent') return;
        if (msg.translatedContent) return;   // a translation landed anyway
        const _errMsg = (typeof frame.error === 'string' && frame.error)
          ? frame.error : 'Translation failed';
        _applyTranslationError(convId, idx, msg, _errMsg);
        return;
      }

      // Skip when the message already has the translation (manual click /
      // poll loop already applied it) — _applyTranslationDone is safe to
      // re-run but writing again would trigger a redundant PATCH + render.
      if (field === 'translatedContent' && msg.translatedContent === frame.translated) return;
      if (field === 'content' && msg.content === frame.translated) return;

      console.log(`%c[Translate] ← push frame applied conv=${convId.slice(0,8)} msg=${idx}`,
        'color:#22c55e');
      // The server has already committed (or is about to) — skip the
      // frontend PATCH to avoid a redundant write race. Just update the
      // in-memory state and re-render the bubble in place.
      if (field === 'translatedContent') {
        msg.translatedContent = frame.translated;
        msg._translatedCache = frame.translated;
        msg._showingTranslation = true;
        /* Carry the per-round narration Chinese onto the segment timeline so
         * the settled view stays interleaved (Chinese narration in place),
         * matching the streaming preview — no de-interleaved snap-back at
         * finalize, and no dependency on a reload re-reading the DB. */
        _stampSegTranslations(msg, frame.segmentsByRound);
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
      delete msg._translatePartialByRound;
      delete msg._translateError;
      if (typeof saveConversations === 'function') saveConversations(convId);
      /* ★ Order-independent finalize (see _translateFinalizeShouldDefer): when
       *   the translate `done` beat the chat `done`, DEFER the whole-bubble
       *   repaint — the imminent committedMessage projection + finishStream
       *   render the COMPLETE bilingual bubble in one pass (reading the
       *   translatedContent already stamped above), so tools / finish bar are
       *   never transiently dropped. Settled / no-stream → repaint now. */
      if (!_translateFinalizeShouldDefer(convId, msg)) {
        emitMessageChanged(convId, idx, msg, { kind: 'full' });
      }
    } catch (e) {
      console.debug('[Translate] push handler error:', e?.message);
    }
  });
})();

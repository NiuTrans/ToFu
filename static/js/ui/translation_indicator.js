/* ═══════════════════════════════════════════════════════════════════
   ui/translation_indicator.js — the translate progress/error indicator

   PURPOSE (decoupling step 3)
   ---------------------------
   The persistent "翻译中…" spinner, the streaming-translation preview, the
   retry-status sub-line, and the terminal-error line used to be built INLINE in
   chat_render.js's renderMessage, reading the loose `_translatePartial` /
   `_translateError` / `_translateStatus` / `_translateStatusKind` /
   `_translateDone` / `_translatedContent` fields directly. That fused the
   content renderer with the translation state machine.

   This component owns that markup and reads translation state ONLY through the
   canonical model (`readTranslation` → the `translation` object). chat_render
   calls `renderTranslateIndicator(msg, idx, {segTimelineRendered})` and splices
   the returned HTML — it no longer touches any `_translate*` field for the
   indicator.

   The produced HTML is byte-identical to the pre-extraction inline block (a
   golden differential test locks that down), so this is a pure move behind a
   clean seam, not a behaviour change.

   NOTE: the send-path "auto-translate failed — original sent" notice
   (`_translateFailed` on a user message) is a DIFFERENT concern (a CN→EN send
   failure, not display-translation) and stays in chat_render's user-lane block.

   Bundled by lib/js_bundler.py after chat_render.js's deps. Pure string
   builder — no DOM mutation (the engine→render repaint seam owns the DOM).
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Build the translate progress / error indicator for a message.
 *
 * Fires for messages routed through the auto-translate pipeline: assistants and
 * endpoint-critic / autopilot-VU (role=user + _isEndpointReview/_isVirtualUser)
 * — i.e. a DISPLAY-translated message that is mid-translation (no 译文 yet and
 * `_translateDone === false`).
 *
 * @param {object} msg
 * @param {number} idx  message index (for the stable #translate-loading-<idx> id)
 * @param {{segTimelineRendered?: boolean}} [opts]
 * @returns {string} HTML fragment ('' when no indicator applies)
 */
function renderTranslateIndicator(msg, idx, opts) {
  if (!msg || typeof msg !== 'object') return '';
  const isUser = msg.role === 'user' || msg.role === 'optimizer';
  const isDisplayTranslated = !isUser || (isUser && (msg._isEndpointReview || msg._isVirtualUser));
  if (!isDisplayTranslated) return '';

  const tr = (typeof readTranslation === 'function') ? readTranslation(msg) : {};
  const _segTimelineRendered = !!(opts && opts.segTimelineRendered);
  const _t = (typeof t === 'function') ? t : (k) => k;

  // ── Terminal error — a quiet, borderless inline retry line (no filled pill,
  //    no chrome — matching the "understated by design: just typographic
  //    weight" language of the translate family). A muted refresh glyph + text
  //    in tertiary color; the whole row lifts to the accent color on hover and
  //    the glyph spins, signalling "click to retry". The full upstream error
  //    (e.g. the 429/402 quota message) is on the title tooltip.
  //
  //    Checked BEFORE the running gate below: the terminal-error path
  //    (translation.js _applyTranslationError) sets _translateDone=true, so the
  //    old `done !== false` gate returned '' and hid the error entirely. Fires
  //    whenever a translation failed and no 译文 landed, regardless of the
  //    _translateDone tri-state. ──
  if (tr.error && tr.text == null) {
    const _lbl = (_t('translate.failed') !== 'translate.failed')
      ? _t('translate.failed') : 'Translation failed, click to retry';
    return `<div class="translate-retry-line" id="translate-loading-${idx}" role="button" tabindex="0" onclick="event.stopPropagation();translateMessage(${idx})" title="${escapeHtml(tr.error)}">`
      + `<svg class="trl-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>`
      + `<span class="trl-text">${escapeHtml(_lbl)}</span></div>`;
  }

  // Only while translation is running (RUNNING-frame marker _translateDone===false)
  // and no 译文 has landed yet.
  if (tr.text != null || tr.done !== false) return '';

  // ── Retry-status sub-line (429 / rate-limit / empty-output). ──
  let statusSub = '';
  const _benignKinds = (typeof _TRANSLATE_BENIGN_STATUS_KINDS !== 'undefined')
    ? _TRANSLATE_BENIGN_STATUS_KINDS : new Set(['started', 'in_progress']);
  if (tr.statusMsg && !_benignKinds.has(tr.statusKind || '')) {
    const kind = tr.statusKind || '';
    const i18nKey = kind ? `translate.retry.${kind}` : '';
    const localized = i18nKey ? _t(i18nKey) : '';
    const display = (localized && localized !== i18nKey) ? localized : tr.statusMsg;
    statusSub = `<div class="translate-status-sub" title="${escapeHtml(tr.statusMsg)}">⚠ ${escapeHtml(display)}</div>`;
  }

  // ── Streaming preview: render the partial translation as markdown as it
  //    arrives. Suppressed when the per-tool segment timeline already shows the
  //    per-round Chinese inline (else this bottom blob duplicates it). ──
  let previewSub = '';
  if (tr.partial && !_segTimelineRendered) {
    let _pv;
    try { _pv = renderMarkdown(stripNoTranslateTags(tr.partial)); }
    catch (e) { _pv = escapeHtml(tr.partial); }
    previewSub = `<div class="translate-preview"><div class="md-content">${_pv}</div><span class="translate-caret"></span></div>`;
  }
  const _hasPreview = previewSub ? ' has-preview' : '';
  const _segTlAttr = _segTimelineRendered ? ' data-seg-timeline="1"' : '';
  return `<div class="translate-loading${_hasPreview}" id="translate-loading-${idx}"${_segTlAttr}>`
    + `<div class="translate-loading-head"><span class="translate-spinner"></span>`
    + `<span class="translate-loading-label">${_t('translate.translatingToCN')}</span></div>`
    + `${statusSub}${previewSub}</div>`;
}

if (typeof window !== 'undefined') {
  window.renderTranslateIndicator = renderTranslateIndicator;
}

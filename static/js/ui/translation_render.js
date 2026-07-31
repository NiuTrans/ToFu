/* ═══════════════════════════════════════════════════════════════════
   ui/translation_render.js — the translation → DOM repaint subscriber

   PURPOSE (decoupling step 4 — the load-bearing seam)
   ---------------------------------------------------
   translation.js is the ENGINE: it drives the poll loop, applies push frames,
   mutates `msg.translation` state, persists (saveConversations / PATCH). It
   must NOT touch the DOM. This module is the RENDER-LAYER SUBSCRIBER that owns
   every DOM mutation the engine used to make inline:

     • the whole-bubble scroll-preserving repaint (`_renderMsgInPlace`),
     • the surgical in-place status/preview patch of an existing
       `#translate-loading-N` indicator (`_patchTranslateLoadingDom`),
     • the LIVE per-round preview into the still-streaming `#streaming-msg`
       bubble (`_renderStreamingTranslatePreview`),
     • the SETTLED per-round narration paint (`_applyPartialByRoundToSettled`),
     • the finalize `data-xlate-final` toggle + the resume full re-render.

   The engine calls exactly ONE entry point — `emitMessageChanged(convId, idx,
   msg, detail)` — plus the two live-streaming paint helpers that RETURN whether
   they painted (the engine branches on that to pick the settled fallback). All
   the surgical fast-paths (spinner-DOM survival, scroll-drift avoidance,
   per-round incremental paint) are PRESERVED here verbatim — this is a
   relocation behind a seam, not a behaviour change.

   The functions are also exposed on `window` because two other render-path
   callers already invoke them via typeof-guards (ui/sse_pipeline.js,
   ui/stream_lifecycle.js) — they keep working unchanged.

   Bundled by lib/js_bundler.py AFTER chat_render.js (it calls renderMessage)
   and BEFORE translation.js (which calls emitMessageChanged). All cross-module
   refs are runtime, so load order beyond "present" is free — but this order
   keeps the dependency reading top-to-bottom.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Scroll-preserving whole-bubble repaint of `#msg-<idx>` for the active conv.
 * Falls back to a scroll-preserving full renderChat when the bubble isn't laid
 * out yet (still loading / index drift / currently the live #streaming-msg).
 *
 * Wrapping the outerHTML swap in `cv-off` forces real height computation before
 * scrollTop restoration (same trick _forceScrollToBottom uses), so a translate
 * transition idle → translating → done doesn't drift the reader.
 */
function _renderMsgInPlace(convId, idx, msg) {
  if (typeof activeConvId === 'undefined' || activeConvId !== convId) return;
  if (typeof renderMessage !== 'function') return;
  const el = document.getElementById(`msg-${idx}`);
  if (!el) {
    console.warn(`[Translate] _renderMsgInPlace: no #msg-${idx} node ` +
      `(conv=${convId.slice(0,8)}) — falling back to ConvView.replaceAll(scroll-preserving)`);
    const _conv = (typeof conversations !== 'undefined')
      ? conversations.find(c => c.id === convId) : null;
    if (_conv) window.ConvView.replaceAll(convId, { forceScroll: false });
    return;
  }
  const ct = document.getElementById('chatContainer');
  const inner = document.getElementById('chatInner');
  const sv = ct ? ct.scrollTop : -1;
  if (inner) {
    inner.classList.add('cv-off');
    void inner.scrollHeight;
  }
  /* RENDER_CONTRACT Phase 3.5: the whole-bubble repaint routes through the
   * single DOM-apply seam (ConvView.apply = renderMessage + identity sweep
   * + fingerprint refresh). No raw fallback — the boot-time hard check in
   * main.js turns a missing ConvView into a loud startup failure instead of
   * a silent per-call degradation (§5 step 4 precondition). */
  if (!window.ConvView || typeof window.ConvView.apply !== 'function') {
    console.error('[Translate] ConvView.apply missing — bundle broken; ' +
      'see the boot banner. Bubble NOT repainted (conv=%s idx=%d).',
      convId && convId.slice(0, 8), idx);
  } else {
    window.ConvView.apply(convId, idx, msg);
  }
  if (inner) {
    void inner.scrollHeight;
    inner.classList.remove('cv-off');
  }
  if (sv >= 0 && ct) ct.scrollTop = sv;
}

/**
 * Patch the inner status-sub / preview-sub children of an existing
 * #translate-loading-N element in place. Returns true if the patch was applied
 * (and the caller should NOT fall back to full re-render). Keeps the
 * .translate-spinner DOM alive (so its CSS keyframe doesn't restart every poll
 * tick) and leaves scrollTop untouched.
 */
function _patchTranslateLoadingDom(loadingEl, msg) {
  if (!loadingEl) return false;
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
      const headEl = loadingEl.querySelector('.translate-loading-head');
      if (headEl && headEl.parentNode === loadingEl) headEl.after(statusEl);
      else loadingEl.appendChild(statusEl);
    }
    statusEl.title = msg._translateStatus;
    statusEl.textContent = '⚠ ' + display;
  } else if (statusEl) {
    statusEl.remove();
  }

  // ── partial-preview body ──
  const _segTl = loadingEl.getAttribute('data-seg-timeline') === '1';
  let previewEl = loadingEl.querySelector('.translate-preview');
  if (_segTl) {
    if (previewEl) { previewEl.remove(); loadingEl.classList.remove('has-preview'); }
  } else if (msg._translatePartial) {
    if (!previewEl) {
      previewEl = document.createElement('div');
      previewEl.className = 'translate-preview';
      previewEl.innerHTML = '<div class="md-content"></div><span class="translate-caret"></span>';
      loadingEl.appendChild(previewEl);
      loadingEl.classList.add('has-preview');
    }
    const mdEl = previewEl.querySelector('.md-content');
    /* ★ SELF-HEAL: `_lastPartial` is a shadow key; re-sync if mdEl's rendered
     * content was clobbered without it moving (cheap innerHTML compare). */
    if (mdEl && (previewEl._lastPartial !== msg._translatePartial
                 || mdEl.innerHTML !== previewEl._lastPartialHtml)) {
      previewEl._lastPartial = msg._translatePartial;
      const nearBottom = (previewEl.scrollHeight - previewEl.scrollTop
                          - previewEl.clientHeight) < 32;
      let html;
      try {
        const fn = (typeof renderMarkdown === 'function') ? renderMarkdown : null;
        const strip = (typeof stripNoTranslateTags === 'function')
          ? stripNoTranslateTags : (s) => s;
        html = fn ? fn(strip(msg._translatePartial)) : null;
      } catch (e) { html = null; }
      if (html != null) mdEl.innerHTML = html;
      else mdEl.textContent = msg._translatePartial;
      previewEl._lastPartialHtml = mdEl.innerHTML;
      if (nearBottom) previewEl.scrollTop = previewEl.scrollHeight;
    }
  } else if (previewEl) {
    previewEl.remove();
    loadingEl.classList.remove('has-preview');
  }
  return true;
}

/**
 * Paint a live translation preview into the STILL-STREAMING assistant bubble
 * (#streaming-msg has no #msg-N node yet). Routes each closed round's Chinese
 * into its .ptool-turn group (narration as an independent sibling above the
 * card, matching the settled render), and the un-routed remainder into the
 * translatedPrimary zone. Returns true when it painted (caller skips the
 * settled fallback), false when the target isn't the live streaming bubble.
 */
function _renderStreamingTranslatePreview(convId, msgId, partial, byRound) {
  if (typeof activeConvId === 'undefined' || activeConvId !== convId) return false;
  if (!msgId || !partial) return false;
  const bubble = document.getElementById('streaming-msg');
  if (!bubble || bubble.getAttribute('data-msg-id') !== msgId) return false;
  const body = bubble.querySelector('#streaming-body') || bubble.querySelector('.message-body');
  if (!body) return false;

  const _routed = new Set();
  if (byRound && typeof byRound === 'object') {
    const _esc = (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape : (s) => s;
    for (const rk in byRound) {
      if (!Object.prototype.hasOwnProperty.call(byRound, rk)) continue;
      const zh = byRound[rk];
      if (!zh || !zh.trim()) continue;
      const gkey = 'L' + rk;
      const group = body.querySelector(`.ptool-turn[data-llm-round="${_esc(gkey)}"]`);
      if (!group) continue;
      const panelBody = group.parentNode;
      if (!panelBody) continue;
      /* ★ BYTE PARITY WITH THE SETTLED RENDER (RENDER_CONTRACT Phase 3.5):
       * the live zh node carries the SAME class list as the settled slot
       * (`md-content seg-narration`, tool_rounds.js:_renderSegNarrationHTML)
       * — no `stream-seg-narration` marker. Visuals are unchanged: the live
       * panel carries `seg-timeline`, so `.seg-timeline .seg-narration`
       * (styles.css:6096, identical values to the now-inert :6158 block)
       * applies verbatim. The zh twin is distinguished from the English
       * sibling by EXCLUSION (`:not(.stream-seg-en-narration)`), keeping
       * live-preview and cold-reload DOM byte-identical for the same fact. */
      let narr = panelBody.querySelector(
        `:scope > .seg-narration[data-seg-round="${_esc(gkey)}"]` +
        `:not(.stream-seg-en-narration)`);
      if (!narr) {
        narr = document.createElement('div');
        narr.className = 'md-content seg-narration';
        narr.setAttribute('data-seg-round', gkey);
        panelBody.insertBefore(narr, group);
      }
      /* ★ SELF-HEAL: `_lastZh` is a shadow key; the DOM is the source of truth.
       * If this node's rendered content was clobbered externally without
       * `_lastZh` moving, the equality skip would pin the dirty content until a
       * full rebuild (reload). Re-sync when the DOM drifted from what we wrote
       * (cheap innerHTML string compare — renderMarkdown only re-runs on drift). */
      if (narr._lastZh !== zh || narr.innerHTML !== narr._lastZhHtml) {
        narr._lastZh = zh;
        let h = null;
        try {
          const fn = (typeof renderMarkdown === 'function') ? renderMarkdown : null;
          const strip = (typeof stripNoTranslateTags === 'function') ? stripNoTranslateTags : (s) => s;
          h = fn ? fn(strip(zh)) : null;
        } catch (e) { h = null; }
        if (h != null) narr.innerHTML = h; else narr.textContent = zh;
        narr._lastZhHtml = narr.innerHTML;
      }
      /* ★ PER-ROUND English hide: now that THIS round's Chinese twin exists,
       * hide its English sibling (avoid a bilingual double). Gated per round —
       * NOT a global body flag — so an intermediate round whose Chinese hasn't
       * landed keeps showing its English instead of vanishing. */
      const _en = panelBody.querySelector(
        `:scope > .stream-seg-en-narration[data-seg-round="${_esc(gkey)}"]`);
      if (_en) _en.classList.add('xlate-hidden');
      _routed.add(rk);
    }
  }

  let zone = body.querySelector('[data-zone="translatedPrimary"]');
  if (!zone) {
    zone = body.querySelector('[data-zone="translatePreview"]');
    if (!zone) {
      zone = document.createElement('div');
      zone.setAttribute('data-zone', 'translatePreview');
      body.appendChild(zone);
    }
  }
  if (!zone.querySelector('.md-content')) {
    zone.className = 'stream-translated-body';
    zone.innerHTML = '<div class="md-content"></div>';
  }
  let blobText = partial;
  if (byRound && typeof byRound === 'object') {
    const _rem = Object.keys(byRound)
      .filter((rk) => !_routed.has(rk) && byRound[rk] && byRound[rk].trim())
      .sort((a, b) => (parseInt(a, 10) || 0) - (parseInt(b, 10) || 0))
      .map((rk) => byRound[rk]);
    blobText = _rem.join('\n\n');
  }
  const mdEl = zone.querySelector('.md-content');
  /* ★ SELF-HEAL: `_lastPartial` shadow key; re-sync on external clobber. */
  if (mdEl && (zone._lastPartial !== blobText || mdEl.innerHTML !== zone._lastPartialHtml)) {
    zone._lastPartial = blobText;
    if (!blobText) {
      mdEl.innerHTML = '';
    } else {
      let html = null;
      try {
        const fn = (typeof renderMarkdown === 'function') ? renderMarkdown : null;
        const strip = (typeof stripNoTranslateTags === 'function') ? stripNoTranslateTags : (s) => s;
        html = fn ? fn(strip(blobText)) : null;
      } catch (e) { html = null; }
      if (html != null) mdEl.innerHTML = html;
      else mdEl.textContent = blobText;
    }
    zone._lastPartialHtml = mdEl.innerHTML;
  }
  body.setAttribute('data-xlate', '1');
  const _contentZone = body.querySelector('[data-zone="content"]');
  if (_contentZone) _contentZone.classList.add('stream-content-demoted');
  if (typeof isNearBottom === 'function' && isNearBottom(80)
      && typeof scrollToBottom === 'function') scrollToBottom();
  return true;
}

/**
 * Surgically paint a `partialByRound` map onto a SETTLED assistant bubble's
 * per-round narration slots (#msg-N .seg-narration[data-seg-round]), streaming
 * the retro / on-open translation round-by-round WITHOUT a whole-bubble swap.
 * Returns true when it painted at least one round (caller skips the blunt
 * full re-render), false when the node / narration slots aren't present.
 */
function _applyPartialByRoundToSettled(convId, idx, byRound) {
  if (typeof activeConvId === 'undefined' || activeConvId !== convId) return false;
  if (!byRound || typeof byRound !== 'object') return false;
  const el = document.getElementById(`msg-${idx}`);
  if (!el) return false;
  const _esc = (typeof CSS !== 'undefined' && CSS.escape) ? CSS.escape : (s) => s;
  let painted = 0;
  for (const rk in byRound) {
    if (!Object.prototype.hasOwnProperty.call(byRound, rk)) continue;
    const zh = byRound[rk];
    if (!zh || !zh.trim()) continue;
    const gkey = 'L' + rk;
    const narr = el.querySelector(`.seg-narration[data-seg-round="${_esc(gkey)}"]`);
    if (!narr) continue;
    /* ★ SELF-HEAL: skip only when BOTH the shadow key matches AND the DOM still
     * holds what we last wrote — a clobber that left `_lastZh` stale must
     * re-sync, not stay pinned until reload (cheap innerHTML compare). */
    if (narr._lastZh === zh && narr.innerHTML === narr._lastZhHtml) { painted++; continue; }
    narr._lastZh = zh;
    let h = null;
    try {
      const fn = (typeof renderMarkdown === 'function') ? renderMarkdown : null;
      const strip = (typeof stripNoTranslateTags === 'function') ? stripNoTranslateTags : (s) => s;
      h = fn ? fn(strip(zh)) : null;
    } catch (e) { h = null; }
    if (h != null) narr.innerHTML = h; else narr.textContent = zh;
    narr._lastZhHtml = narr.innerHTML;
    painted++;
  }
  return painted > 0;
}

/**
 * Toggle `data-xlate-final` on the live streaming body for a 'started' finalize
 * frame — hides the English live-tail before the final Chinese lands. No-op
 * when the frame's message isn't the live bubble.
 */
function _markStreamXlateFinal(msgId) {
  if (!msgId) return;
  const _sm = document.getElementById('streaming-msg');
  if (_sm && _sm.getAttribute('data-msg-id') === msgId) {
    const _b = _sm.querySelector('#streaming-body') || _sm.querySelector('.message-body');
    if (_b) _b.setAttribute('data-xlate-final', '1');
  }
}

/**
 * THE ENGINE→RENDER SEAM. translation.js calls this (never the DOM directly)
 * whenever it has mutated a message's translation state and wants the view
 * repainted. `detail` selects the paint strategy:
 *
 *   {kind:'full'}                     → whole-bubble scroll-preserving repaint.
 *   {kind:'status'}                   → surgical in-place patch of the existing
 *                                       #translate-loading-N; falls back to a
 *                                       full repaint when it isn't in the DOM.
 *   {kind:'conv', conv}               → scroll-preserving full renderChat.
 *
 * Returns true when a paint happened (mostly informational; the status kind
 * mirrors the old _applyTranslationStatus contract).
 */
function emitMessageChanged(convId, idx, msg, detail) {
  detail = detail || {};
  const kind = detail.kind || 'full';
  if (kind === 'conv') {
    if (typeof activeConvId !== 'undefined' && activeConvId === convId
        && detail.conv) {
      window.ConvView.replaceAll(convId, { forceScroll: false });
    }
    return true;
  }
  if (kind === 'status') {
    if (typeof activeConvId !== 'undefined' && activeConvId === convId) {
      const loadingEl = document.getElementById(`translate-loading-${idx}`);
      if (loadingEl && _patchTranslateLoadingDom(loadingEl, msg)) return true;
      _renderMsgInPlace(convId, idx, msg);
    }
    return true;
  }
  // 'full'
  _renderMsgInPlace(convId, idx, msg);
  return true;
}

if (typeof window !== 'undefined') {
  window.emitMessageChanged = emitMessageChanged;
  window._renderMsgInPlace = _renderMsgInPlace;
  window._patchTranslateLoadingDom = _patchTranslateLoadingDom;
  window._renderStreamingTranslatePreview = _renderStreamingTranslatePreview;
  window._applyPartialByRoundToSettled = _applyPartialByRoundToSettled;
  window._markStreamXlateFinal = _markStreamXlateFinal;
}

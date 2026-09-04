/* ===== migrated source: core/toast.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/toast.js — extracted from core.js (split 2026-05-28)

   Toast notifications.

   This file is concatenated by Vite's module graph AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* ── Toast Notifications ── */
/* Inline SVG icons (Lucide-style, 16px stroke) — NO emoji/text glyphs. */
const _SVG = {
  success: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
  error:   '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  warning: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  info:    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
};
const _CLOSE_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

/* Strip emoji/pictographs from displayed text — status is conveyed by the
   typed SVG icon circle, so caller-supplied emoji prefixes (✅ 📝 ⚠️ …) are
   redundant noise. Removes emoji + variation selectors, then trims leftover
   leading separators/whitespace. */
function _stripEmoji(s) {
  if (!s) return '';
  return String(s)
    .replace(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2190}-\u{21FF}\u{2B00}-\u{2BFF}\u{FE0F}\u{2122}\u{2139}\u{2705}\u{2714}\u{2716}\u{274C}\u{2757}\u{26A0}]/gu, '')
    .replace(/^[\s\-–—:·•]+/, '')
    .trim();
}

const _toastTypes = {
  success: { icon: _SVG.success, cls: 't-success', dur: 3000 },
  error:   { icon: _SVG.error,   cls: 't-error',   dur: 6000 },
  warning: { icon: _SVG.warning, cls: 't-warning', dur: 5000 },
  warn:    { icon: _SVG.warning, cls: 't-warning', dur: 5000 },
  info:    { icon: _SVG.info,    cls: 't-info',    dur: 3500 },
};

/* Chat-bubble glyph for the "which conversation triggered this" source badge. */
const _CONV_SRC_SVG = '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>';

/**
 * showToast — flexible API:
 *   showToast("消息文本", "success")           ← simple (message + type)
 *   showToast("✅", "Title", "detail", 5000)   ← full   (icon, title, detail, ms)
 *
 * Optional 5th arg `opts` enriches a background/async notification so the user
 * knows WHERE it came from and WHAT to do:
 *   {
 *     convId, convTitle  → renders a clickable "from «title»" source badge;
 *                          clicking it (or the toast body) jumps to that
 *                          conversation via loadConversation(convId).
 *     hint               → a quieter guidance line ("what can you do?") shown
 *                          under the detail.
 *     onClick            → custom whole-toast click handler (overrides the
 *                          default jump-to-conversation).
 *   }
 * All opts fields are user-data-safe (escaped). Passing no opts is byte-
 * identical to the pre-existing behaviour.
 */
function showToast(iconOrMsg, titleOrType, detail, durationMs, opts) {
  const c = document.getElementById('toastContainer');
  if (!c) return;

  /* ── Detect which API form ── */
  const isSimple = !titleOrType || (typeof titleOrType === 'string' && titleOrType in _toastTypes);
  let title, type, dur;

  if (isSimple) {
    type  = (titleOrType && titleOrType in _toastTypes) ? titleOrType : 'info';
    title = iconOrMsg || '';
    detail = null;
    dur   = _toastTypes[type].dur;
  } else {
    // Full form: showToast(icon, title, detail?, dur?)
    // We fold icon into the title since the new design uses a typed icon circle
    title  = titleOrType || '';
    dur    = durationMs || 4000;
    // Infer type from the icon/title text
    if (/✅|✓|💡|saved|success/i.test(iconOrMsg + title)) type = 'success';
    else if (/❌|✕|fail|error/i.test(iconOrMsg + title)) type = 'error';
    else if (/⚠|warn/i.test(iconOrMsg + title)) type = 'warning';
    else type = 'info';
  }

  const info = _toastTypes[type] || _toastTypes.info;
  opts = opts || {};

  /* Type is already inferred above — now drop emoji from the visible text. */
  title = _stripEmoji(title);
  detail = _stripEmoji(detail);

  /* ── Optional enrichments (escaped — these can carry user data) ──
   * NOTE: `t()` is the global i18n fn; we deliberately do NOT shadow it with
   * a local `t` (the toast element is `el` below) so `t()` stays reachable. */
  const _esc = (typeof escapeHtml === 'function') ? escapeHtml : (s => String(s == null ? '' : s));
  const _tt = (typeof t === 'function') ? t : null;
  let srcHtml = '';
  const convId = opts.convId || '';
  if (opts.convTitle) {
    const fromLabel = _tt ? _tt('toast.fromConv') : 'from';
    srcHtml =
      `<span class="toast-conv-src">` +
        _CONV_SRC_SVG +
        `<span class="toast-conv-src-label">${_esc(fromLabel)}</span>` +
        `<span class="toast-conv-src-title">${_esc(opts.convTitle)}</span>` +
      `</span>`;
  }
  const hintHtml = opts.hint
    ? `<span class="toast-hint">${_esc(opts.hint)}</span>` : '';

  /* ── Build DOM ── */
  const el = document.createElement('div');
  el.className = 'toast ' + info.cls;
  const _navigable = !!(convId && typeof loadConversation === 'function') || typeof opts.onClick === 'function';
  if (_navigable) el.classList.add('toast-clickable');
  el.innerHTML =
    `<div class="toast-icon-wrap ${info.cls}">${info.icon}</div>` +
    `<div class="toast-body">` +
      srcHtml +
      `<span class="toast-title">${title}</span>` +
      (detail ? `<span class="toast-detail">${detail}</span>` : '') +
      hintHtml +
    `</div>` +
    `<button class="toast-close" aria-label="close">${_CLOSE_SVG}</button>` +
    `<div class="toast-progress ${info.cls}" style="width:100%;animation:toastTimer ${dur}ms linear forwards"></div>`;

  /* ── Dismiss logic ── */
  let timer, paused = false;
  const dismiss = () => {
    if (el._dismissed) return;
    el._dismissed = true;
    el.classList.add('removing');
    setTimeout(() => el.remove(), 300);
  };
  el.querySelector('.toast-close').onclick = (e) => { e.stopPropagation(); dismiss(); };

  /* ── Click-to-act: jump to the source conversation (or a custom action) ──
   * A background/async toast is only useful if the user can act on it. The
   * default action navigates to the conversation that triggered the event;
   * opts.onClick overrides it. Clicking the close button is exempted above. */
  if (_navigable) {
    const act = (e) => {
      if (e && e.target && e.target.closest && e.target.closest('.toast-close')) return;
      try {
        if (typeof opts.onClick === 'function') opts.onClick();
        else if (convId && typeof loadConversation === 'function') loadConversation(convId);
      } catch (err) { console.warn('[toast] click action failed', err); }
      dismiss();
    };
    el.addEventListener('click', act);
    const actionTarget = el.querySelector('.toast-body');
    if (actionTarget) {
      actionTarget.setAttribute('role', 'button');
      actionTarget.setAttribute('tabindex', '0');
      actionTarget.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          act(e);
        }
      });
    }
  }

  c.appendChild(el);
  timer = setTimeout(dismiss, dur);

  /* Pause on hover */
  const prog = el.querySelector('.toast-progress');
  el.addEventListener('mouseenter', () => {
    paused = true;
    clearTimeout(timer);
    if (prog) prog.style.animationPlayState = 'paused';
  });
  el.addEventListener('mouseleave', () => {
    paused = false;
    if (prog) prog.style.animationPlayState = 'running';
    timer = setTimeout(dismiss, 1500);
  });
}


/**
 * _ephemeralToast — a minimal "append a styled div to a scoped parent, fade it
 * out after N ms" toast. This is the shared skeleton behind the bespoke
 * scoped install toasts in the typed Memory/Skills feature chunks that
 * deliberately do NOT use the rich global showToast() —
 * they need their OWN parent + CSS class (in-modal placement, distinct
 * styling). De-dups the create/append/fade-remove plumbing while leaving each
 * caller its parent, class and duration.
 *
 * @param {Element} parent    node to append the toast to
 * @param {string}  className  full class string for the toast element
 * @param {string}  text       textContent (NOT html — safe by construction)
 * @param {number}  [ms=3500]  visible duration before fade-out
 * @param {boolean} [replace=true] remove existing toasts of the same class first
 * @returns {Element|null} the toast element (or null if parent missing)
 */
function _ephemeralToast(parent, className, text, ms, replace) {
  if (!parent) return null;
  if (replace !== false) {
    parent.querySelectorAll('.' + className.split(/\s+/)[0]).forEach((n) => n.remove());
  }
  const el = document.createElement('div');
  el.className = className;
  el.textContent = text;
  parent.appendChild(el);
  setTimeout(() => {
    el.style.transition = 'opacity .3s';
    el.style.opacity = '0';
    setTimeout(() => el.remove(), 300);
  }, ms || 3500);
  return el;
}
if (typeof window !== 'undefined') {
  runtimeScope.showToast = showToast;
  runtimeScope._ephemeralToast = _ephemeralToast;
}

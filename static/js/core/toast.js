/* ═══════════════════════════════════════════════════════════════════
   core/toast.js — extracted from core.js (split 2026-05-28)

   Toast notifications.

   This file is concatenated by lib/js_bundler.py AFTER the slim
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

/**
 * showToast — flexible API:
 *   showToast("消息文本", "success")           ← simple (message + type)
 *   showToast("✅", "Title", "detail", 5000)   ← full   (icon, title, detail, ms)
 */
function showToast(iconOrMsg, titleOrType, detail, durationMs) {
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

  /* Type is already inferred above — now drop emoji from the visible text. */
  title = _stripEmoji(title);
  detail = _stripEmoji(detail);

  /* ── Build DOM ── */
  const t = document.createElement('div');
  t.className = 'toast ' + info.cls;
  t.innerHTML =
    `<div class="toast-icon-wrap ${info.cls}">${info.icon}</div>` +
    `<div class="toast-body">` +
      `<span class="toast-title">${title}</span>` +
      (detail ? `<span class="toast-detail">${detail}</span>` : '') +
    `</div>` +
    `<button class="toast-close" aria-label="close">${_CLOSE_SVG}</button>` +
    `<div class="toast-progress ${info.cls}" style="width:100%;animation:toastTimer ${dur}ms linear forwards"></div>`;

  /* ── Dismiss logic ── */
  let timer, paused = false;
  const dismiss = () => {
    if (t._dismissed) return;
    t._dismissed = true;
    t.classList.add('removing');
    setTimeout(() => t.remove(), 300);
  };
  t.querySelector('.toast-close').onclick = dismiss;
  c.appendChild(t);
  timer = setTimeout(dismiss, dur);

  /* Pause on hover */
  const prog = t.querySelector('.toast-progress');
  t.addEventListener('mouseenter', () => {
    paused = true;
    clearTimeout(timer);
    if (prog) prog.style.animationPlayState = 'paused';
  });
  t.addEventListener('mouseleave', () => {
    paused = false;
    if (prog) prog.style.animationPlayState = 'running';
    timer = setTimeout(dismiss, 1500);
  });
}

/* ═══════════════════════════════════════════════════════════════════
   core/toast.js — extracted from core.js (split 2026-05-28)

   Toast notifications.

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* ── Toast Notifications ── */
const _toastTypes = {
  success: { icon: '✓', cls: 't-success', dur: 3000 },
  error:   { icon: '✕', cls: 't-error',   dur: 6000 },
  warning: { icon: '!', cls: 't-warning', dur: 5000 },
  warn:    { icon: '!', cls: 't-warning', dur: 5000 },
  info:    { icon: 'i', cls: 't-info',    dur: 3500 },
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

  /* ── Build DOM ── */
  const t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML =
    `<div class="toast-icon-wrap ${info.cls}">${info.icon}</div>` +
    `<div class="toast-body">` +
      `<span class="toast-title">${title}</span>` +
      (detail ? `<span class="toast-detail">${detail}</span>` : '') +
    `</div>` +
    `<button class="toast-close" aria-label="close">×</button>` +
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

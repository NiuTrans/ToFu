/* ═══════════════════════════════════════════════════════════════════
   core/escape_html.js — extracted from core.js (split 2026-05-28)

   Pure-string escapeHtml (no DOM) — perf-critical.

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* ★ Perf: pure string escapeHtml — avoids creating a DOM element on every call.
 * The old DOM approach (createElement+textContent+innerHTML) caused ~50 DOM
 * allocations per renderChat.  Regex replacement is 10-50× faster. */
const _escapeMap = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const _escapeRe = /[&<>"']/g;
function escapeHtml(t) {
  if (!t) return '';
  if (typeof t !== 'string') t = String(t);
  return t.replace(_escapeRe, ch => _escapeMap[ch]);
}


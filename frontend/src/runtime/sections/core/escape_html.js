/* ===== migrated source: core/escape_html.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/escape_html.js — extracted from core.js (split 2026-05-28)

   Pure-string escapeHtml (no DOM) — perf-critical.

   This file is concatenated by Vite's module graph AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* Perf: pure string escapeHtml — avoids creating a DOM element on every call.
 * The old DOM approach (createElement+textContent+innerHTML) caused ~50 DOM
 * allocations per block render. Regex replacement is 10-50× faster. */
const _escapeMap = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const _escapeRe = /[&<>"']/g;
function escapeHtml(t) {
  if (!t) return '';
  if (typeof t !== 'string') t = String(t);
  return t.replace(_escapeRe, ch => _escapeMap[ch]);
}

/* _esc — the short alias used by classic presentation helpers across the
 * runtime resolves through window scope. It used to live as
 * the repo's only top-level definition inside memory.js, which made every
 * one of those modules silently depend on a settings module's load order.
 * Promoted here (Epic-E sub-9, 2026-08-01) so the definition survives
 * memory.js's deferral; the body is byte-identical to the old one. */
function _esc(s) {
  return escapeHtml(s);
}

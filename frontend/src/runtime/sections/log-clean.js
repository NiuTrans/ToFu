/* ===== migrated source: log-clean.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   log-clean.js — Log Noise Detection & Cleaning (UI layer only)

   The detection heuristic, regex policy, and cleaning algorithms now
   live server-side at lib/log_clean.py exposed via:
     POST /api/v1/logs/clean   { text }   → CleaningResult | {no_noise:true}
     POST /api/v1/logs/compress { text }  → LLM-compressed text

   This file is a *pure render layer* per CLAUDE.md §16:
     - detectLogNoise(text)  →  fetch wrapper, returns the same shape
                                 the old client-side impl returned
     - showLogCleanBanner()  →  DOM render of the structured result
     - applyLogClean()       →  splice cleanedText into the textarea
     - aiCompressLog()       →  LLM compression path (unchanged)
     - previewLogClean()     →  before/after modal

   Going from 740 LOC → ~290 LOC, plus parity with the headless API:
   SDK callers see exactly the same heuristic the UI renders.
   ═══════════════════════════════════════════════════════════════════ */

var _pendingLogClean = null;  // shared with main.js — must be var

// Generation counter used to ignore stale fetches when the user keeps
// typing while a request is in-flight.
let _logCleanReqGen = 0;

/**
 * Detect log noise. Returns a CleaningResult or null.
 * Mirrors the previous client-side function shape, but the policy
 * lives server-side now.
 *
 * Returns a Promise<Object|null>.  Callers that previously did
 *   const result = detectLogNoise(text);
 *   if (result) showLogCleanBanner(result);
 * must now await it (callers in main.js have been updated).
 */
async function detectLogNoise(text) {
  if (!text || typeof text !== 'string') return null;
  // Fast-path: avoid round-trips for trivially short input.
  if (text.split('\n').length < 5) return null;

  const myGen = ++_logCleanReqGen;
  try {
    // Api.logs.clean uses onError:'null' → null on HTTP/network error, matching
    // the old !resp.ok / catch paths (the per-status warn is dropped; the
    // unified client already console.warns failures).
    const body = await Api.logs.clean(text);
    if (myGen !== _logCleanReqGen) return null;  // superseded
    if (!body || body.no_noise) return null;
    if (!body.cleanedText) return null;
    // Server returns dataclass-shaped object; it's already in the
    // shape the old client-side detectLogNoise produced.
    return body;
  } catch (err) {
    if (myGen !== _logCleanReqGen) return null;
    if (typeof debugLog === 'function') {
      debugLog(`[LogClean] detection failed: ${err && err.message}`, 'warn');
    }
    return null;
  }
}

// ── Banner UI ───────────────────────────────────────────────────

function showLogCleanBanner(result) {
  _pendingLogClean = result;
  const banner = document.getElementById('logCleanBanner');
  const info = document.getElementById('logCleanInfo');
  const details = document.getElementById('logCleanDetails');
  if (!banner || !info || !details) return;

  info.innerHTML = t('logClean.banner', {
    chars: result.savedChars.toLocaleString(), pct: result.savedPct,
  });

  const tagsHtml = (result.ops || []).map(op =>
    `<span class="log-clean-tag">• ${_escForBanner(op.desc)}</span>`
  ).join('');
  details.innerHTML = tagsHtml;
  banner.style.display = 'flex';
}

function hideLogCleanBanner() {
  const banner = document.getElementById('logCleanBanner');
  if (banner) banner.style.display = 'none';
  _pendingLogClean = null;
}

function _escForBanner(s) {
  return escapeHtml(s);
}

// ── Active textarea resolution ──────────────────────────────────

function _getActiveTextarea() {
  return document.getElementById('userInput');
}

// ── Rule-based replace (uses server-cleaned text) ───────────────

function applyLogClean() {
  if (!_pendingLogClean) return;
  const input = _getActiveTextarea();
  if (!input) return;
  input.value = input.value.replace(
    _pendingLogClean.originalText,
    _pendingLogClean.cleanedText,
  );
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 200) + 'px';
  if (typeof debugLog === 'function') {
    debugLog(
      `Log noise cleaned: saved ${_pendingLogClean.savedChars} chars` +
      ` (${_pendingLogClean.savedPct}%)`,
      'success',
    );
  }
  hideLogCleanBanner();
}

// ── LLM compression path (unchanged endpoint) ───────────────────

async function aiCompressLog() {
  if (!_pendingLogClean) return;
  const btn = document.getElementById('aiCompressBtn');
  if (!btn) return;
  const originalText = _pendingLogClean.originalText;

  btn.disabled = true;
  const origLabel = btn.innerHTML;
  btn.innerHTML = _aiBtnIconHtml() + ' ' + t('logClean.compressing');

  try {
    // Api.logs.compress throws Error(data.error || 'API error') on non-OK,
    // exactly like the old inline handling.
    const data = await Api.logs.compress(originalText);
    const compressed = data.compressed || '';
    if (!compressed.trim()) throw new Error('LLM returned empty result');

    const input = _getActiveTextarea();
    if (!input) throw new Error('No active textarea');
    input.value = input.value.replace(originalText, compressed);
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 200) + 'px';

    const savedChars = originalText.length - compressed.length;
    const savedPct = Math.round((savedChars / originalText.length) * 100);
    if (typeof debugLog === 'function') {
      debugLog(
        `AI log compress: ${originalText.length} → ${compressed.length}` +
        ` chars (saved ${savedPct}%, model: ${data.model || '?'})`,
        'success',
      );
    }
    hideLogCleanBanner();
  } catch (err) {
    if (typeof debugLog === 'function') {
      debugLog(`[AI Compress] failed: ${err.message}`, 'error');
    }
    btn.innerHTML = _aiBtnIconHtml() + ' ' + t('logClean.retry');
    btn.disabled = false;
  }
}

function _aiBtnIconHtml() {
  return '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" ' +
         'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
         'stroke-linejoin="round" style="vertical-align:-1px">' +
         '<rect width="16" height="16" x="4" y="4" rx="2"/>' +
         '<rect width="6" height="6" x="9" y="9" rx="1"/>' +
         '<path d="M15 2v2"/><path d="M15 20v2"/>' +
         '<path d="M2 15h2"/><path d="M2 9h2"/>' +
         '<path d="M20 15h2"/><path d="M20 9h2"/>' +
         '<path d="M9 2v2"/><path d="M9 20v2"/></svg>';
}

// ── Before/after preview modal ──────────────────────────────────

function previewLogClean() {
  if (!_pendingLogClean) return;
  const r = _pendingLogClean;
  const beforeLen = r.originalText.length;
  const afterLen = r.cleanedText.length;

  const breakdownHtml =
    '<div class="log-clean-breakdown">' +
    (r.ops || []).map(op =>
      '<div class="log-clean-breakdown-item">' +
        '<span class="log-clean-breakdown-icon">•</span>' +
        '<span>' + _escForBanner(op.desc) + '</span>' +
      '</div>'
    ).join('') +
    '</div>';

  const _esc = escapeHtml;

  const previewHtml = `
    <div class="log-clean-compare">
      ${breakdownHtml}
      <div class="log-clean-section">
        <div class="log-clean-section-header">
          <span class="log-clean-section-title">${_esc(t('logClean.before'))}</span>
          <span class="log-clean-section-meta">${_esc(t('logClean.metaBefore', { chars: beforeLen.toLocaleString(), lines: r.totalLines }))}</span>
        </div>
        <pre class="log-clean-code">${_esc(r.originalText.slice(0, 3000))}${beforeLen > 3000 ? '\n... (' + (beforeLen - 3000).toLocaleString() + ' more chars)' : ''}</pre>
      </div>
      <div class="log-clean-section">
        <div class="log-clean-section-header">
          <span class="log-clean-section-title">${_esc(t('logClean.after'))}</span>
          <span class="log-clean-section-meta">${_esc(t('logClean.metaAfter', { chars: afterLen.toLocaleString(), pct: r.savedPct }))}</span>
        </div>
        <pre class="log-clean-code">${_esc(r.cleanedText.slice(0, 3000))}${afterLen > 3000 ? '\n... (' + (afterLen - 3000).toLocaleString() + ' more chars)' : ''}</pre>
      </div>
    </div>`;

  const previewBody = document.getElementById('previewBody');
  const previewModal = document.getElementById('previewModal');
  if (!previewBody || !previewModal) return;
  previewBody.innerHTML =
    `<div class="preview-text-panel" style="width:min(900px,90vw)">` +
    `<div class="preview-text-header">` +
    `<span class="preview-text-title">${_esc(t('logClean.previewTitle'))}</span>` +
    `<span class="preview-text-meta">${_esc(t('logClean.previewMeta', { chars: r.savedChars.toLocaleString(), pct: r.savedPct }))}</span>` +
    `</div>` +
    `<div style="padding:16px 20px;overflow-y:auto;max-height:calc(85vh - 60px)">${previewHtml}</div>` +
    `</div>`;
  previewModal.classList.add('open');
}

// Expose for cross-script access (settings/main.js attach to globals).
runtimeScope.detectLogNoise = detectLogNoise;
runtimeScope.showLogCleanBanner = showLogCleanBanner;
runtimeScope.hideLogCleanBanner = hideLogCleanBanner;
runtimeScope.applyLogClean = applyLogClean;
runtimeScope.aiCompressLog = aiCompressLog;
runtimeScope.previewLogClean = previewLogClean;

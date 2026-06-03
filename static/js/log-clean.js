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

// ── Fetch helper ────────────────────────────────────────────────

function _logCleanApiUrl(p) {
  return (typeof apiUrl === 'function') ? apiUrl(p) : p;
}

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
    const resp = await fetch(_logCleanApiUrl('/api/v1/logs/clean'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
      credentials: 'same-origin',
    });
    if (myGen !== _logCleanReqGen) return null;  // superseded
    if (!resp.ok) {
      // Don't spam the console for transient 4xx/5xx — log once per kind.
      if (typeof debugLog === 'function') {
        debugLog(`[LogClean] /api/v1/logs/clean → ${resp.status}`, 'warn');
      }
      return null;
    }
    const body = await resp.json();
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

  info.innerHTML =
    `检测到日志噪音，可节省 <strong>${result.savedChars.toLocaleString()}</strong>` +
    ` 字符（<strong>${result.savedPct}%</strong>）`;

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
  if (typeof escapeHtml === 'function') return escapeHtml(s);
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// ── Active textarea resolution ──────────────────────────────────

function _getActiveTextarea() {
  if (typeof _editingMsgIdx !== 'undefined' && _editingMsgIdx !== null) {
    const el = document.getElementById('edit-textarea-' + _editingMsgIdx);
    if (el) return el;
  }
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
  const maxH = (typeof _editingMsgIdx !== 'undefined' && _editingMsgIdx !== null)
    ? 300 : 200;
  input.style.height = Math.min(input.scrollHeight, maxH) + 'px';
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
  btn.innerHTML = _aiBtnIconHtml() + ' 压缩中…';

  try {
    const resp = await fetch(_logCleanApiUrl('/api/v1/logs/compress'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: originalText }),
      credentials: 'same-origin',
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'API error');
    const compressed = data.compressed || '';
    if (!compressed.trim()) throw new Error('LLM returned empty result');

    const input = _getActiveTextarea();
    if (!input) throw new Error('No active textarea');
    input.value = input.value.replace(originalText, compressed);
    input.style.height = 'auto';
    const maxH = (typeof _editingMsgIdx !== 'undefined' && _editingMsgIdx !== null)
      ? 300 : 200;
    input.style.height = Math.min(input.scrollHeight, maxH) + 'px';

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
    btn.innerHTML = _aiBtnIconHtml() + ' 失败，重试';
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

  const _esc = (typeof escapeHtml === 'function')
    ? escapeHtml
    : (s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;'));

  const previewHtml = `
    <div class="log-clean-compare">
      ${breakdownHtml}
      <div class="log-clean-section">
        <div class="log-clean-section-header">
          <span class="log-clean-section-title">清理前</span>
          <span class="log-clean-section-meta">${beforeLen.toLocaleString()} 字符 · ${r.totalLines} 行</span>
        </div>
        <pre class="log-clean-code">${_esc(r.originalText.slice(0, 3000))}${beforeLen > 3000 ? '\n... (' + (beforeLen - 3000).toLocaleString() + ' more chars)' : ''}</pre>
      </div>
      <div class="log-clean-section">
        <div class="log-clean-section-header">
          <span class="log-clean-section-title">清理后</span>
          <span class="log-clean-section-meta">${afterLen.toLocaleString()} 字符 · 节省 ${r.savedPct}%</span>
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
    `<span class="preview-text-title">日志噪音清理预览</span>` +
    `<span class="preview-text-meta">节省 ${r.savedChars.toLocaleString()} 字符 (${r.savedPct}%)</span>` +
    `</div>` +
    `<div style="padding:16px 20px;overflow-y:auto;max-height:calc(85vh - 60px)">${previewHtml}</div>` +
    `</div>`;
  previewModal.classList.add('open');
}

// Expose for cross-script access (settings/main.js attach to globals).
window.detectLogNoise = detectLogNoise;
window.showLogCleanBanner = showLogCleanBanner;
window.hideLogCleanBanner = hideLogCleanBanner;
window.applyLogClean = applyLogClean;
window.aiCompressLog = aiCompressLog;
window.previewLogClean = previewLogClean;

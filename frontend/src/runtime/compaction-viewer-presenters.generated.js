// @ts-check
/* Generated lazy retained runtime: compaction-viewer-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { _applyI18n, t } from '../i18n/index';
import { escapeHtml } from '../html-safety';

const Api = runtimeScope.Api;
if (!Api || typeof Api !== 'object') throw new Error('compaction-viewer-presenters runtime dependency is unavailable: Api');
const CompactionHistoryState = runtimeScope.CompactionHistoryState;
if (!CompactionHistoryState || typeof CompactionHistoryState !== 'object') throw new Error('compaction-viewer-presenters runtime dependency is unavailable: CompactionHistoryState');
const showToast = runtimeScope.showToast;
if (typeof showToast !== 'function') throw new Error('compaction-viewer-presenters runtime dependency is unavailable: showToast');
/* ===== migrated source: compaction-viewer.js ===== */
/* ══════════════════════════════════════════════════════════════════════════
 * compaction-viewer.js — Right-side drawer for inspecting pre-compaction
 * context snapshots persisted in transcript_archive.
 *
 * Public API:
 *   openCompactionViewer(convId, archiveId?) — demand-loaded drawer entry
 *   closeCompactionViewer()                  — close and release open state
 *
 * Design decisions:
 *   - Drawer, NOT a modal, so the main conversation stays readable (main
 *     chat fades slightly when drawer is open for focus).
 *   - Messages rendered as a read-only list with role-coded blocks. No
 *     markdown parsing — intentionally raw so the user can see EXACTLY
 *     what hit the LLM (whitespace, tool args, tool output).
 *   - Images (image_url blocks) shown as collapsed placeholders with size
 *     and "reveal" button to avoid choking the DOM on a 2.7MB base64 payload.
 *   - The displayed context is the payload right BEFORE the compaction
 *     fired — NOT the user's original prose. We surface that caveat in
 *     the drawer header to avoid confusion.
 * ══════════════════════════════════════════════════════════════════════════ */
'use strict';

  // Composition-injected typed escapeHtml — no local re-implementation.
  const _esc = escapeHtml;

  const _fmtTokens = (n) => {
    n = Number(n) || 0;
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(2) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'k';
    return String(n);
  };

  const _fmtBytes = (n) => {
    n = Number(n) || 0;
    if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
    return n + ' B';
  };

  const _fmtTime = (value) => {
    try {
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return String(value);
      // Archive rows use epoch milliseconds; older SSE markers used seconds.
      const millis = numeric < 1_000_000_000_000 ? numeric * 1000 : numeric;
      const d = new Date(millis);
      return d.toLocaleString();
    } catch (_e) { return String(value); }
  };

  const _cvIco = (inner) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px">${inner}</svg>`;
  // Defensive i18n shim — this file is bundled so the global t() is present,
  // but degrade to a param-substituting key fallback rather than throwing.
  const _t = (key, params) => (typeof t === 'function')
    ? t(key, params)
    : (params ? key.replace(/\{(\w+)\}/g, (_m, k) => (k in params ? params[k] : '{' + k + '}')) : key);
  // Trigger icons (SVG glyphs) keyed by trigger kind; the human label resolves
  // at render time via t() so it follows the current UI language.
  const _TRIGGER_ICON = {
    working_set: _cvIco('<path d="M4 6h16M4 12h16M4 18h10"/><circle cx="18" cy="18" r="2"/>'),
    window:   _cvIco('<rect width="18" height="14" x="3" y="5" rx="2"/><path d="M3 9h18"/>'),
    force:    _cvIco('<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>'),
    reactive: _cvIco('<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>'),
    manual:   _cvIco('<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.106-3.105c.32-.322.863-.22.983.218a6 6 0 0 1-8.259 7.057l-7.91 7.91a1 1 0 0 1-2.999-3l7.91-7.91a6 6 0 0 1 7.057-8.259c.438.12.54.662.219.984z"/>'),
  };
  /** Resolve the icon + localized label for a trigger kind. Unknown kinds
   *  render the raw kind verbatim (no icon). */
  const _triggerLabel = (trig) => {
    const _known = _TRIGGER_ICON[trig];
    return _known ? (_known + ' ' + _t('compactionViewer.trigger.' + trig)) : _esc(trig);
  };

  // ── Byte-bounded LRU: summary/raw projections ───────────────────────
  // Raw archives can be megabytes. Keep at most two projections / 8 MiB and
  // never retain a single payload that already exceeds the byte budget.
  const _PAYLOAD_CACHE_MAX_ENTRIES = 2;
  const _PAYLOAD_CACHE_MAX_BYTES = 8 * 1024 * 1024;
  const _payloadCache = new Map();
  let _payloadCacheBytes = 0;
  let _payloadCacheConv = null;

  function _cacheGet(key) {
    const record = _payloadCache.get(key);
    if (!record) return null;
    _payloadCache.delete(key);
    _payloadCache.set(key, record);
    return record.payload;
  }

  function _cachePut(key, payload, includeMessages) {
    const declared = Number(payload?.archive?.payloadSize) || 0;
    const estimatedBytes = includeMessages && declared > 0
      ? declared
      : JSON.stringify(payload || {}).length * 2;
    if (estimatedBytes > _PAYLOAD_CACHE_MAX_BYTES) return;
    const prior = _payloadCache.get(key);
    if (prior) {
      _payloadCacheBytes -= prior.bytes;
      _payloadCache.delete(key);
    }
    while (_payloadCache.size >= _PAYLOAD_CACHE_MAX_ENTRIES
           || _payloadCacheBytes + estimatedBytes > _PAYLOAD_CACHE_MAX_BYTES) {
      const oldestKey = _payloadCache.keys().next().value;
      if (oldestKey === undefined) break;
      const oldest = _payloadCache.get(oldestKey);
      _payloadCacheBytes -= oldest?.bytes || 0;
      _payloadCache.delete(oldestKey);
    }
    _payloadCache.set(key, { payload, bytes: estimatedBytes });
    _payloadCacheBytes += estimatedBytes;
  }

  // ────────────────────────────────────────────────────────────────────
  //  DOM: ensure the drawer exists exactly once
  // ────────────────────────────────────────────────────────────────────
  function _ensureDrawer() {
    let el = document.getElementById('compactionViewerDrawer');
    if (el) return el;

    el = document.createElement('div');
    el.id = 'compactionViewerDrawer';
    el.className = 'compaction-drawer';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-labelledby', 'compactionViewerTitle');
    el.setAttribute('aria-hidden', 'true');
    el.innerHTML = `
      <div class="compaction-drawer-backdrop" data-close></div>
      <aside class="compaction-drawer-panel">
        <header class="compaction-drawer-header">
          <div class="compaction-drawer-title-row">
            <h3 id="compactionViewerTitle" data-i18n="compactionViewer.title">压缩前的上下文快照</h3>
            <button type="button" class="compaction-drawer-close" data-close
                    aria-label="Close">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round">
                <path d="M18 6 6 18M6 6l12 12"/></svg>
            </button>
          </div>
          <p class="compaction-drawer-subtitle" data-i18n-html="compactionViewer.subtitle">
            这里展示的是<strong>压缩触发瞬间</strong>发送给 LLM 的完整消息列表——
            包含 system prompt、工具调用、工具结果，以及已经过 L1/L2 处理（如
            thinking 剥离、screenshot 替换）的中间态。
            它<em>不是</em>用户输入的"原始文本"——查看原始对话请使用左侧主窗口。
          </p>
          <div class="compaction-drawer-meta"></div>
          <div class="compaction-drawer-tabs" role="tablist">
            <button type="button" class="compaction-tab"
                    data-tab="messages" role="tab" data-i18n="compactionViewer.tabMessages">上下文消息</button>
            <button type="button" class="compaction-tab is-active"
                    data-tab="summary"  role="tab" data-i18n="compactionViewer.tabSummary">压缩结果摘要</button>
            <button type="button" class="compaction-tab"
                    data-tab="history"  role="tab" data-i18n="compactionViewer.tabHistory">该会话全部快照</button>
          </div>
        </header>
        <div class="compaction-drawer-body">
          <div class="compaction-drawer-loading" data-i18n="compactionViewer.loading">加载中…</div>
          <div class="compaction-drawer-content"></div>
        </div>
        <footer class="compaction-drawer-footer">
          <button type="button" class="compaction-drawer-btn" data-action="copy-json" data-i18n="compactionViewer.copyJson">
            复制原始 JSON
          </button>
          <button type="button" class="compaction-drawer-btn" data-action="download" data-i18n="compactionViewer.download">
            下载完整快照
          </button>
        </footer>
      </aside>
    `;
    document.body.appendChild(el);
    // The drawer is built lazily on first open — AFTER the initial
    // DOMContentLoaded _applyI18n() pass — so translate its static chrome
    // (data-i18n attrs) now. Subsequent language flips are handled by
    // setLanguage()'s whole-DOM _applyI18n(), which re-scans this drawer too.
    if (typeof _applyI18n === 'function') _applyI18n();

    // Close handlers
    el.addEventListener('click', (e) => {
      const t = e.target;
      if (t && (t.dataset && t.dataset.close !== undefined
                || t.closest && t.closest('[data-close]'))) {
        closeCompactionViewer();
      }
    });
    // Tab switching
    el.querySelectorAll('.compaction-tab').forEach(btn => {
      btn.addEventListener('click', async () => {
        el.querySelectorAll('.compaction-tab').forEach(b => b.classList.remove('is-active'));
        btn.classList.add('is-active');
        await _renderActiveTab();
      });
    });
    // Escape key
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && el.classList.contains('is-open')) {
        closeCompactionViewer();
      }
    });
    // Footer actions
    el.querySelector('[data-action="copy-json"]').addEventListener('click', _copyJson);
    el.querySelector('[data-action="download"]').addEventListener('click', _downloadSnapshot);
    return el;
  }

  // ────────────────────────────────────────────────────────────────────
  //  Per-open state (drawer is single-instance so we keep it here)
  // ────────────────────────────────────────────────────────────────────
  let _state = null;  // { convId, archiveId, listData, activeArchive, activeMessages }

  async function _fetchList(convId) {
    if (_payloadCacheConv !== convId) {
      _payloadCache.clear();
      _payloadCacheBytes = 0;
      _payloadCacheConv = convId;
    }
    // Explicit inspection is authoritative. It shares an already-running
    // hydration request, but otherwise bypasses the shell's short freshness
    // window so a newly-created archive is immediately discoverable.
    return CompactionHistoryState.list(convId, { force: true });
  }

  async function _fetchPayload(convId, archiveId, includeMessages = false) {
    const projection = includeMessages ? 'raw' : 'summary';
    const key = `${convId}:${archiveId}:${projection}`;
    const cached = _cacheGet(key);
    if (cached) return cached;
    const j = includeMessages
      ? await Api.compactions.get(convId, archiveId)
      : await Api.compactions.getSummary(convId, archiveId);
    _cachePut(key, j, includeMessages);
    return j;
  }

  async function _ensureActiveMessages() {
    if (!_state || !_state.convId || !_state.archiveId) return [];
    if (Array.isArray(_state.activeMessages)) return _state.activeMessages;
    const payload = await _fetchPayload(
      _state.convId, _state.archiveId, true);
    _state.activeArchive = Object.assign(
      {}, _state.activeArchive || {}, payload.archive || {});
    _state.activeMessages = Array.isArray(payload.messages)
      ? payload.messages : [];
    _renderMeta();
    return _state.activeMessages;
  }

  // ────────────────────────────────────────────────────────────────────
  //  Rendering
  // ────────────────────────────────────────────────────────────────────
  function _renderMeta() {
    if (!_state || !_state.activeArchive) return;
    const a = _state.activeArchive;
    const el = _ensureDrawer().querySelector('.compaction-drawer-meta');
    const trig = a.trigger || 'force';
    const trigLabel = _triggerLabel(trig);
    const tokenPrefix = a.tokenCountKind === 'estimated' ? '≈' : '';
    const reductionTxt = (a.tokensBefore > 0 && a.tokensAfter > 0)
      ? `-${Math.round((1 - a.tokensAfter / a.tokensBefore) * 100)}%`
      : '—';
    const reasonBlock = a.reason
      ? `<div class="cd-meta-row cd-meta-reason"><span class="cd-meta-k">${_esc(_t('compactionViewer.metaReason'))}</span><span class="cd-meta-v">${_esc(a.reason)}</span></div>`
      : '';
    el.innerHTML = `
      <div class="cd-meta-grid">
        <div class="cd-meta-row">
          <span class="cd-meta-k">${_esc(_t('compactionViewer.metaType'))}</span>
          <span class="cd-meta-v cd-meta-trigger cd-meta-trigger-${_esc(trig)}">${trigLabel}</span>
        </div>
        <div class="cd-meta-row">
          <span class="cd-meta-k">${_esc(_t('compactionViewer.metaTime'))}</span>
          <span class="cd-meta-v">${_esc(_fmtTime(a.createdAt))}</span>
        </div>
        <div class="cd-meta-row">
          <span class="cd-meta-k">${_esc(_t('compactionViewer.metaMsgs'))}</span>
          <span class="cd-meta-v">${a.msgsBefore || '?'} → ${a.msgsAfter || '?'}</span>
        </div>
        <div class="cd-meta-row">
          <span class="cd-meta-k">Token</span>
          <span class="cd-meta-v">${tokenPrefix}${_fmtTokens(a.tokensBefore)} → ${a.tokensAfter > 0 ? tokenPrefix + _fmtTokens(a.tokensAfter) : '—'} <em>(${reductionTxt})</em></span>
        </div>
        <div class="cd-meta-row">
          <span class="cd-meta-k">${_esc(_t('compactionViewer.metaModel'))}</span>
          <span class="cd-meta-v">${_esc(a.taskModel || a.model || '—')}</span>
        </div>
        <div class="cd-meta-row">
          <span class="cd-meta-k">${_esc(_t('compactionViewer.metaRound'))}</span>
          <span class="cd-meta-v">#${a.roundNum || '?'}${a.taskId ? ` · task ${_esc(String(a.taskId).slice(0, 8))}` : ''}</span>
        </div>
        ${reasonBlock}
      </div>
    `;
  }

  function _renderMessagesTab() {
    const el = _ensureDrawer().querySelector('.compaction-drawer-content');
    const messages = (_state && _state.activeMessages) || [];
    if (!messages.length) {
      el.innerHTML = `<div class="cd-empty">${_esc(_t('compactionViewer.emptySnapshot'))}</div>`;
      return;
    }
    const parts = messages.map((m, i) => _renderMessage(m, i));
    el.innerHTML = `<ol class="compaction-msg-list">${parts.join('')}</ol>`;

    // Wire up expandable images
    el.querySelectorAll('[data-reveal-image]').forEach(btn => {
      btn.addEventListener('click', () => {
        const imgUrl = btn.dataset.imgUrl;
        const ph = btn.parentElement;
        ph.innerHTML = `<img src="${_esc(imgUrl)}" alt="compacted image" />`;
      });
    });
    // Large text remains in the bounded raw payload cache but does not enter
    // the DOM until explicitly requested. This avoids an enormous innerHTML
    // allocation for archived read/search results.
    el.querySelectorAll('[data-reveal-content]').forEach(btn => {
      btn.addEventListener('click', () => {
        const index = Number(btn.dataset.messageIndex);
        const message = _state?.activeMessages?.[index];
        const code = btn.parentElement?.querySelector('code');
        if (!code || typeof message?.content !== 'string') return;
        code.textContent = message.content;
        btn.remove();
      });
    });
  }

  function _renderMessage(m, idx) {
    const role = m && m.role || 'unknown';
    const roleLabelMap = {
      system: 'SYSTEM', user: 'USER', assistant: 'ASSISTANT',
      tool: 'TOOL RESULT', function: 'FUNCTION',
    };
    const roleLabel = roleLabelMap[role] || role.toUpperCase();
    let inner = '';

    // tool_calls from assistant (function-calling)
    if (Array.isArray(m.tool_calls) && m.tool_calls.length) {
      const tcs = m.tool_calls.map(tc => {
        const fn = (tc && tc.function) || {};
        const argStr = typeof fn.arguments === 'string' ? fn.arguments : JSON.stringify(fn.arguments || {});
        const argPreview = argStr.length > 2000 ? argStr.slice(0, 2000) + `\n… (${argStr.length.toLocaleString()} chars total)` : argStr;
        return `<div class="cd-toolcall">
          <div class="cd-toolcall-name">→ ${_esc(fn.name || '?')}<span class="cd-toolcall-id">${_esc(tc.id || '')}</span></div>
          <pre class="cd-toolcall-args"><code>${_esc(argPreview)}</code></pre>
        </div>`;
      }).join('');
      inner += tcs;
    }

    // content: string or list of blocks
    if (typeof m.content === 'string') {
      const previewLimit = 16_000;
      if (m.content.length > previewLimit) {
        const preview = m.content.slice(0, 12_000)
          + `\n\n… (${m.content.length.toLocaleString()} chars total; full content not rendered) …\n\n`
          + m.content.slice(-2_000);
        inner += `<div class="cd-large-content"><pre class="cd-content"><code>${_esc(preview)}</code></pre>
          <button type="button" class="compaction-drawer-btn" data-reveal-content
                  data-message-index="${idx}">Reveal full content · ${m.content.length.toLocaleString()} chars</button></div>`;
      } else {
        inner += `<pre class="cd-content"><code>${_esc(m.content)}</code></pre>`;
      }
    } else if (Array.isArray(m.content)) {
      const blocks = m.content.map(blk => _renderContentBlock(blk)).join('');
      inner += `<div class="cd-content-blocks">${blocks}</div>`;
    } else if (m.content != null) {
      inner += `<pre class="cd-content"><code>${_esc(JSON.stringify(m.content, null, 2))}</code></pre>`;
    }

    // thinking (only for older rounds that weren't L2-stripped)
    if (m.thinking) {
      inner += `<details class="cd-thinking"><summary>reasoning · ${m.thinking.length.toLocaleString()} chars</summary><pre><code>${_esc(m.thinking)}</code></pre></details>`;
    }

    const meta = [];
    if (m.name) meta.push(`name=${_esc(m.name)}`);
    if (m.tool_call_id) meta.push(`tool_call_id=${_esc(String(m.tool_call_id).slice(0, 20))}`);
    const metaStr = meta.length ? `<span class="cd-msg-meta">${meta.join(' · ')}</span>` : '';

    return `<li class="cd-msg cd-msg-${_esc(role)}">
      <div class="cd-msg-head">
        <span class="cd-msg-idx">#${idx + 1}</span>
        <span class="cd-msg-role">${_esc(roleLabel)}</span>
        ${metaStr}
      </div>
      <div class="cd-msg-body">${inner || '<em class="cd-empty-body">(empty)</em>'}</div>
    </li>`;
  }

  function _renderContentBlock(blk) {
    if (!blk || typeof blk !== 'object') {
      return `<pre class="cd-content"><code>${_esc(JSON.stringify(blk))}</code></pre>`;
    }
    const type = blk.type;
    if (type === 'text') {
      return `<pre class="cd-content"><code>${_esc(blk.text || '')}</code></pre>`;
    }
    if (type === 'image_url') {
      const url = (blk.image_url && blk.image_url.url) || '';
      const sizeLabel = _fmtBytes(url.length);
      const isDataUrl = url.startsWith('data:');
      return `<div class="cd-image-block">
        <div class="cd-image-head"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em;vertical-align:-2px"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg> image_url · ${sizeLabel}${isDataUrl ? ' (base64)' : ''}</div>
        <div class="cd-image-placeholder">
          <button type="button" data-reveal-image
                  data-img-url="${_esc(url)}">${_esc(_t('compactionViewer.revealImage'))}</button>
        </div>
      </div>`;
    }
    // Unknown block type — stringify
    return `<pre class="cd-content"><code>${_esc(JSON.stringify(blk, null, 2))}</code></pre>`;
  }

  const _receiptMetric = (label, value) => `
    <div class="cd-receipt-metric">
      <span>${_esc(label)}</span><strong>${_esc(String(value))}</strong>
    </div>`;

  function _renderReceipt(receipt) {
    if (!receipt || typeof receipt !== 'object' || !receipt.schemaVersion) return '';
    const retention = receipt.retention || {};
    const summary = receipt.summary || {};
    const usage = summary.usage || {};
    const economics = receipt.economics || null;
    const evidence = receipt.evidence || null;
    const recovery = receipt.recovery || null;
    const recentFiles = Array.isArray(retention.recentFiles)
      ? retention.recentFiles.slice(0, 8) : [];
    const status = receipt.status || 'legacy';
    const statusClass = String(status).replace(/[^a-z0-9_-]/gi, '');
    const summaryMetrics = summary.generated ? [
      _receiptMetric(_t('compactionViewer.receiptAccepted'), summary.accepted ? _t('compactionViewer.yes') : _t('compactionViewer.no')),
      _receiptMetric(_t('compactionViewer.receiptDuration'), `${Number(summary.durationMs || 0).toLocaleString()} ms`),
      _receiptMetric(_t('compactionViewer.receiptUsage'), `${_fmtTokens(usage.inputTokens)} + ${_fmtTokens(usage.outputTokens)}`),
      _receiptMetric(_t('compactionViewer.receiptProjectedUsage'), _fmtTokens(summary.projectedUsageTokens)),
      _receiptMetric(_t('compactionViewer.receiptSummaryChars'), Number(summary.chars || 0).toLocaleString()),
    ].join('') : '';
    const economicsBlock = economics ? `
      <section class="cd-receipt-group">
        <h4>${_esc(_t('compactionViewer.receiptEconomics'))}</h4>
        <div class="cd-receipt-metrics">
          ${_receiptMetric(_t('compactionViewer.receiptDroppedTokens'), _fmtTokens(economics.droppedTokens))}
          ${_receiptMetric(_t('compactionViewer.receiptCacheRewrite'), _fmtTokens(economics.cacheRewriteTokens))}
          ${_receiptMetric(_t('compactionViewer.receiptSummaryCost'), _fmtTokens(economics.summaryCostTokens))}
          ${_receiptMetric(_t('compactionViewer.receiptPayback'), economics.paybackRounds == null ? '—' : `${economics.paybackRounds} rounds`)}
        </div>
      </section>` : '';
    const evidenceBlock = evidence ? `
      <section class="cd-receipt-group">
        <h4>${_esc(_t('compactionViewer.receiptEvidence'))}</h4>
        <div class="cd-receipt-metrics">
          ${_receiptMetric(_t('compactionViewer.receiptEvidenceRetained'), evidence.retainedCount || 0)}
          ${_receiptMetric(_t('compactionViewer.receiptEvidenceLost'), evidence.lostCount || 0)}
        </div>
      </section>` : '';
    const recoveryBlock = recovery ? `
      <section class="cd-receipt-group">
        <h4>${_esc(_t('compactionViewer.receiptRecovery'))}</h4>
        <div class="cd-receipt-metrics">
          ${_receiptMetric(_t('compactionViewer.receiptStrippedImages'), recovery.strippedImages || 0)}
          ${_receiptMetric(_t('compactionViewer.receiptTruncatedChars'), Number(recovery.truncatedChars || 0).toLocaleString())}
          ${_receiptMetric(_t('compactionViewer.receiptDroppedMessages'), recovery.droppedMessages || 0)}
          ${_receiptMetric(_t('compactionViewer.receiptWireBytes'), `${_fmtBytes(recovery.wireBytesBefore)} → ${_fmtBytes(recovery.wireBytesAfter)}`)}
        </div>
      </section>` : '';
    const recentFilesBlock = recentFiles.length ? `
      <div class="cd-receipt-files">
        <span>${_esc(_t('compactionViewer.receiptRecentFiles'))}</span>
        <ul>${recentFiles.map((path) => `<li><code>${_esc(path)}</code></li>`).join('')}</ul>
      </div>` : '';
    return `
      <section class="cd-receipt" data-receipt-version="${_esc(receipt.schemaVersion)}">
        <div class="cd-receipt-head">
          <h3>${_esc(_t('compactionViewer.receiptTitle'))}</h3>
          <span class="cd-receipt-status cd-receipt-status-${_esc(statusClass)}">${_esc(status)}</span>
        </div>
        <div class="cd-receipt-identity">
          <code>${_esc(receipt.strategy || '—')}</code>
          <span>·</span><code>${_esc(receipt.implementation || '—')}</code>
          ${receipt.mode ? `<span>·</span><code>${_esc(receipt.mode)}</code>` : ''}
          <span>·</span><code>${_esc(receipt.continuation?.format || 'none')}</code>
        </div>
        ${receipt.outcomeReason ? `<p class="cd-receipt-outcome">${_esc(receipt.outcomeReason)}</p>` : ''}
        <div class="cd-receipt-groups">
          <section class="cd-receipt-group">
            <h4>${_esc(_t('compactionViewer.receiptRetention'))}</h4>
            <div class="cd-receipt-metrics">
              ${_receiptMetric(_t('compactionViewer.receiptSummarized'), retention.summarizedMessages || 0)}
              ${_receiptMetric(_t('compactionViewer.receiptPreservedTurns'), retention.preservedTurns || 0)}
              ${_receiptMetric(_t('compactionViewer.receiptFoldedRounds'), retention.foldedToolRounds || 0)}
              ${_receiptMetric(_t('compactionViewer.receiptVerbatimUsers'), retention.retainedUserMessages || 0)}
            </div>
            <div class="cd-receipt-flags">
              <span class="${retention.objectiveAnchored ? 'is-on' : ''}">${_esc(_t('compactionViewer.receiptObjectiveAnchor'))}</span>
              <span class="${retention.turnDiffIncluded ? 'is-on' : ''}">${_esc(_t('compactionViewer.receiptTurnDiff'))}</span>
            </div>
            ${recentFilesBlock}
          </section>
          ${summary.generated ? `<section class="cd-receipt-group"><h4>${_esc(_t('compactionViewer.receiptSummaryRun'))}</h4><div class="cd-receipt-metrics">${summaryMetrics}</div>${summary.rejectionReason ? `<p class="cd-receipt-outcome">${_esc(summary.rejectionReason)}</p>` : ''}</section>` : ''}
          ${economicsBlock}${evidenceBlock}${recoveryBlock}
        </div>
      </section>`;
  }

  function _renderSummaryTab() {
    const el = _ensureDrawer().querySelector('.compaction-drawer-content');
    const a = _state && _state.activeArchive;
    if (!a) return;
    const receipt = _renderReceipt(a.receipt);
    if (!a.summary) {
      el.innerHTML = receipt + `<div class="cd-empty">
        <p>${_esc(_t('compactionViewer.noSummary1'))}</p>
        <p>${_esc(_t('compactionViewer.noSummary2'))}</p>
      </div>`;
      return;
    }
    // Render as code-looking prose — don't run markdown since we already
    // show it inline in the tool panel elsewhere.
    el.innerHTML = receipt
      + `<h3 class="cd-summary-title">${_esc(_t('compactionViewer.receiptSummaryText'))}</h3>`
      + `<pre class="cd-summary"><code>${_esc(a.summary)}</code></pre>`;
  }

  function _renderHistoryTab() {
    const el = _ensureDrawer().querySelector('.compaction-drawer-content');
    const list = (_state && _state.listData && _state.listData.compactions) || [];
    if (!list.length) {
      el.innerHTML = `<div class="cd-empty">${_esc(_t('compactionViewer.noHistory'))}</div>`;
      return;
    }
    const rows = list.map(c => {
      const isActive = (_state.archiveId === c.id);
      const trig = c.trigger || 'force';
      const tokenPrefix = c.tokenCountKind === 'estimated' ? '≈' : '';
      const reduction = (c.tokensBefore > 0 && c.tokensAfter > 0)
        ? `-${Math.round((1 - c.tokensAfter / c.tokensBefore) * 100)}%`
        : '—';
      return `<li class="cd-history-item ${isActive ? 'is-active' : ''}"
                  data-archive-id="${_esc(String(c.id))}">
        <div class="cd-history-head">
          <span class="cd-history-trigger cd-history-trigger-${_esc(trig)}">${_triggerLabel(trig)}</span>
          ${c.resultStatus ? `<span class="cd-history-result cd-receipt-status-${_esc(String(c.resultStatus).replace(/[^a-z0-9_-]/gi, ''))}">${_esc(c.resultStatus)}</span>` : ''}
          <span class="cd-history-time">${_esc(_fmtTime(c.createdAt))}</span>
        </div>
        <div class="cd-history-stats">
          <span>${tokenPrefix}${_fmtTokens(c.tokensBefore)} → ${c.tokensAfter > 0 ? tokenPrefix + _fmtTokens(c.tokensAfter) : '—'} <em>(${reduction})</em></span>
          <span>·</span>
          <span>${c.msgsBefore || '?'} → ${c.msgsAfter || '?'} msgs</span>
          <span>·</span>
          <span>${_fmtBytes(c.payloadSize)}</span>
        </div>
        ${c.reason ? `<div class="cd-history-reason">${_esc(c.reason)}</div>` : ''}
      </li>`;
    }).join('');
    el.innerHTML = `<ul class="compaction-history-list">${rows}</ul>`;
    el.querySelectorAll('.cd-history-item').forEach(li => {
      li.addEventListener('click', async () => {
        const id = li.dataset.archiveId || '';
        if (id && _state && _state.convId) {
          await _selectArchive(_state.convId, id);
        }
      });
    });
  }

  async function _renderActiveTab() {
    const el = _ensureDrawer();
    const tab = el.querySelector('.compaction-tab.is-active');
    const which = tab ? tab.dataset.tab : 'summary';
    if (which === 'messages') {
      _showLoading(true);
      try {
        await _ensureActiveMessages();
        _renderMessagesTab();
      } catch (e) {
        console.error('[compaction-viewer] raw load failed:', e);
        const content = el.querySelector('.compaction-drawer-content');
        content.innerHTML = `<div class="cd-empty cd-error">${_esc(_t('compactionViewer.loadFailed', { err: (e.message || String(e)) }))}</div>`;
      } finally {
        _showLoading(false);
      }
    }
    else if (which === 'summary') _renderSummaryTab();
    else if (which === 'history') _renderHistoryTab();
  }

  function _showLoading(on) {
    const el = _ensureDrawer();
    el.querySelector('.compaction-drawer-loading').style.display = on ? 'block' : 'none';
    el.querySelector('.compaction-drawer-content').style.display = on ? 'none'  : 'block';
  }

  async function _selectArchive(convId, archiveId) {
    const selectionState = _state;
    if (!selectionState || selectionState.convId !== convId) return;
    selectionState.selectionVersion =
      Number(selectionState.selectionVersion || 0) + 1;
    const selectionVersion = selectionState.selectionVersion;
    _showLoading(true);
    try {
      const payload = await _fetchPayload(convId, archiveId, false);
      if (_state !== selectionState
          || selectionState.selectionVersion !== selectionVersion) return;
      _state.archiveId = archiveId;
      _state.activeArchive = payload.archive || {};
      _state.activeMessages = null;
      _renderMeta();
      await _renderActiveTab();
    } catch (e) {
      if (_state !== selectionState
          || selectionState.selectionVersion !== selectionVersion) return;
      console.error('[compaction-viewer] load failed:', e);
      const el = _ensureDrawer().querySelector('.compaction-drawer-content');
      el.innerHTML = `<div class="cd-empty cd-error">${_esc(_t('compactionViewer.loadFailed', { err: (e.message || String(e)) }))}</div>`;
    } finally {
      if (_state === selectionState
          && selectionState.selectionVersion === selectionVersion) {
        _showLoading(false);
      }
    }
  }

  // ────────────────────────────────────────────────────────────────────
  //  Public API
  // ────────────────────────────────────────────────────────────────────
  async function openCompactionViewer(convId, archiveId) {
    if (!convId) {
      console.warn('[compaction-viewer] openCompactionViewer: missing convId');
      return;
    }
    const el = _ensureDrawer();
    const openingState = {
      convId, archiveId: null, listData: null,
      activeArchive: null, activeMessages: null, selectionVersion: 0,
    };
    _state = openingState;
    // Fade main UI
    document.body.classList.add('compaction-drawer-open');
    el.classList.add('is-open');
    el.setAttribute('aria-hidden', 'false');
    _showLoading(true);
    // Summary-first: raw transcripts can be megabytes and are loaded only
    // when the user explicitly opens the messages tab or copies raw JSON.
    el.querySelectorAll('.compaction-tab').forEach(b => b.classList.remove('is-active'));
    el.querySelector('.compaction-tab[data-tab="summary"]').classList.add('is-active');

    try {
      // Fetch list of archives (for history tab + latest-selection fallback)
      const listData = await _fetchList(convId);
      if (_state !== openingState) return;
      _state.listData = listData;
      const archives = listData.compactions || [];

      // Decide which archive to load
      let targetId = archiveId;
      if (!targetId && archives.length) {
        targetId = archives[archives.length - 1].id;  // most recent
      }
      if (!targetId) {
        _showLoading(false);
        const bodyEl = el.querySelector('.compaction-drawer-content');
        bodyEl.innerHTML = `<div class="cd-empty">${_esc(_t('compactionViewer.noCompaction'))}</div>`;
        el.querySelector('.compaction-drawer-meta').innerHTML = '';
        return;
      }
      await _selectArchive(convId, targetId);
    } catch (e) {
      if (_state !== openingState) return;
      console.error('[compaction-viewer] list failed:', e);
      _showLoading(false);
      const bodyEl = el.querySelector('.compaction-drawer-content');
      bodyEl.innerHTML = `<div class="cd-empty cd-error">${_esc(_t('compactionViewer.historyFailed', { err: (e.message || String(e)) }))}</div>`;
    }
  }

  function closeCompactionViewer() {
    const el = document.getElementById('compactionViewerDrawer');
    if (!el) return;
    el.classList.remove('is-open');
    el.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('compaction-drawer-open');
    _state = null;
  }

  // Live language-switch hook (called by i18n.js::_onLanguageChange). The
  // drawer's static chrome re-translates via the whole-DOM _applyI18n() scan;
  // here we re-render the JS-built meta rows + active tab so they follow the
  // new language too. No-op when the drawer is closed / has no state.
  function _cvOnLanguageChange() {
    const el = document.getElementById('compactionViewerDrawer');
    if (!el || !el.classList.contains('is-open') || !_state) return;
    if (_state.activeArchive) _renderMeta();
    _renderActiveTab();
  }

  async function _copyJson() {
    if (!_state) return;
    try {
      await _ensureActiveMessages();
    } catch (err) {
      if (typeof showToast === 'function') showToast(_t('compactionViewer.copyFailed', { err: err.message }), 'error');
      return;
    }
    const txt = JSON.stringify({
      archive: _state.activeArchive,
      messages: _state.activeMessages,
    }, null, 2);
    navigator.clipboard.writeText(txt).then(() => {
      if (typeof showToast === 'function') showToast('✅ ' + _t('compactionViewer.copied'), 'info');
    }, (err) => {
      console.error('[compaction-viewer] copy failed:', err);
      if (typeof showToast === 'function') showToast(_t('compactionViewer.copyFailed', { err: err.message }), 'error');
    });
  }

  async function _downloadSnapshot() {
    if (!_state || !_state.archiveId) return;
    try {
      const response = await Api.compactions.download(
        _state.convId, _state.archiveId);
      if (!response || !response.ok) {
        throw new Error(`HTTP ${response?.status || '?'}`);
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `compaction-${_state.convId.slice(0, 8)}-${_state.archiveId}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 500);
    } catch (err) {
      console.error('[compaction-viewer] download failed:', err);
      if (typeof showToast === 'function') {
        showToast(_t('compactionViewer.loadFailed', { err: err.message }), 'error');
      }
    }
  }

window.addEventListener('tofu:language-change', _cvOnLanguageChange);

// BEGIN GENERATED LAZY RUNTIME PORTS — compaction-viewer-presenters
runtimeScope.openCompactionViewer = openCompactionViewer;
runtimeScope.closeCompactionViewer = closeCompactionViewer;
// END GENERATED LAZY RUNTIME PORTS
// BEGIN GENERATED LAZY RUNTIME ACTIONS — compaction-viewer-presenters
// END GENERATED LAZY RUNTIME ACTIONS

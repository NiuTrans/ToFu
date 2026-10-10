// @ts-check
/* Generated lazy retained runtime: compaction-viewer-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { compactionMessageKinds, compactionPostExport, jumpToCompactionSummary } from '../features/compaction-snapshot';
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
 * compaction-viewer.js — Right-side drawer for inspecting compaction
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
 *     what was stored (whitespace, tool args, tool output).
 *   - Images (image_url blocks) shown as collapsed placeholders with size
 *     and "reveal" button to avoid choking the DOM on a 2.7MB base64 payload.
 *   - The messages tab shows the stored compaction-result array (post view),
 *     before runtime recomposition and provider adaptation. Legacy rows recorded before that
 *     projection existed show an explanatory note instead. Neither view is
 *     the user's original prose — we surface that caveat in the header.
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
            这里展示的是已存储的<strong>压缩结果阶段</strong>消息，尚不包含后续运行时重组和供应商适配；不是首次 API 请求或最终传输内容。
          </p>
          <div class="compaction-drawer-meta"></div>
          <div class="compaction-drawer-tabs" role="tablist">
            <button type="button" class="compaction-tab is-active"
                    data-tab="messages" role="tab" data-i18n="compactionViewer.tabMessages">上下文消息</button>
            <button type="button" class="compaction-tab"
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
  // postMissing becomes true when the archive predates the post-compaction
  // projection (or its result was never adopted) — the tab then shows an
  // explanatory note instead of a messages list.
  let _state = null;  // { convId, archiveId, listData, activeArchive, activeMessages, postMissing }

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

  async function _fetchPayload(convId, archiveId, view = 'summary') {
    const key = `${convId}:${archiveId}:${view}`;
    const cached = _cacheGet(key);
    if (cached) return cached;
    const j = view === 'post'
      ? await Api.compactions.getPost(convId, archiveId)
      : await Api.compactions.getSummary(convId, archiveId);
    _cachePut(key, j, false);
    return j;
  }

  async function _ensureActiveMessages() {
    const state = _state;
    if (!state || !state.convId || !state.archiveId) return null;
    if (Array.isArray(state.activeMessages) || state.postMissing) return state.activeMessages;
    const version = state.selectionVersion;
    const payload = await _fetchPayload(state.convId, state.archiveId, 'post');
    if (_state !== state || state.selectionVersion !== version) return null;
    state.activeArchive = Object.assign({}, state.activeArchive || {}, payload.archive || {});
    state.postMissing = !Array.isArray(payload.postMessages);
    state.activeMessages = state.postMissing ? null : payload.postMessages;
    state.postSnapshot = payload.postSnapshot;
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
    if (_state && _state.postMissing) {
      el.innerHTML = `<div class="cd-empty">${_esc(_t('compactionViewer.postMissing'))}</div>`;
      return;
    }
    if (!messages.length) {
      el.innerHTML = `<div class="cd-empty">${_esc(_t('compactionViewer.emptySnapshot'))}</div>`;
      return;
    }
    const kinds = compactionMessageKinds(messages, _state.activeArchive);
    const parts = messages.map((m, i) => _renderMessage(m, i, kinds[i]));
    el.innerHTML = `<p class="cd-empty">${_esc(_t('compactionViewer.stageCaption'))}</p>
      <button type="button" class="compaction-drawer-btn" data-jump-summary>${_esc(_t('compactionViewer.jumpSummary'))}</button>
      <ol class="compaction-msg-list">${parts.join('')}</ol>`;
    const jump = el.querySelector('[data-jump-summary]');
    jump.disabled = !el.querySelector('[data-compaction-kind="summary"], [data-compaction-kind="recovery"]');
    jump.addEventListener('click', () => jumpToCompactionSummary(el));

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

  function _renderMessage(m, idx, kind) {
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

    return `<li class="cd-msg cd-msg-${_esc(role)}" data-compaction-kind="${kind}">
      <div class="cd-msg-head">
        <span class="cd-msg-idx">#${idx + 1}</span>
        <span class="cd-msg-role">${_esc(_t('compactionViewer.role'))}: ${_esc(roleLabel)}</span>
        <span class="cd-msg-meta">${_esc(_t('compactionViewer.source'))}: ${_esc(_t('compactionViewer.source.' + kind))}</span>
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

    const budget = receipt.contextBudget || null;
    const unknown = _t('compactionViewer.receiptUnknown');
    const finite = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
    const tokens = value => finite(value) ? _fmtTokens(value) : unknown;
    const count = value => finite(value) ? value.toLocaleString() : unknown;
    const boolean = value => typeof value === 'boolean'
      ? _t(value ? 'compactionViewer.yes' : 'compactionViewer.no') : unknown;
    // Never infer known billing from normalized usage or a missing legacy flag.
    const usd = (value, known) => known === true && finite(value)
      ? (value > 0 && value < 0.000001 ? '< $0.000001' : `$${value.toFixed(6)}`) : unknown;
    const route = summary.route || {};
    const cost = summary.cost || {};
    const decisionLabels = {
      safety: 'compactionViewer.decision.safety',
      economic: 'compactionViewer.decision.economic',
      manual: 'compactionViewer.decision.manual',
    };
    const decisionLabel = Object.prototype.hasOwnProperty.call(decisionLabels, budget?.decisionKind)
      ? _t(decisionLabels[budget.decisionKind]) : unknown;
    const policyLabels = {
      main: 'compactionViewer.policy.main',
      'qualified-cheapest': 'compactionViewer.policy.qualifiedCheapest',
    };
    const policyLabel = Object.prototype.hasOwnProperty.call(policyLabels, route.policy)
      ? _t(policyLabels[route.policy]) : (route.policy || unknown);
    const budgetBlock = budget ? `
      <section class="cd-receipt-group">
        <h4>${_esc(_t('compactionViewer.receiptBudget'))}</h4>
        <div class="cd-receipt-metrics">
          ${_receiptMetric(_t('compactionViewer.receiptDecision'), decisionLabel)}
          ${_receiptMetric(_t('compactionViewer.receiptWindow'), tokens(budget.routeWindowTokens))}
          ${_receiptMetric(_t('compactionViewer.receiptCeiling'), tokens(budget.hardCeilingTokens))}
          ${_receiptMetric(_t('compactionViewer.receiptRendered'), tokens(budget.renderedPromptTokens))}
          ${_receiptMetric(_t('compactionViewer.receiptReserve'), tokens(budget.reserveTokens))}
          ${_receiptMetric(_t('compactionViewer.receiptEconomicTarget'), tokens(budget.economicTargetTokens))}
        </div>
        <p class="cd-receipt-outcome">${_esc(_t('compactionViewer.receiptBudgetCaveat'))}</p>
      </section>` : '';
    const recentFiles = Array.isArray(retention.recentFiles)
      ? retention.recentFiles.slice(0, 8) : [];
    const status = receipt.status || 'legacy';
    const statusClass = String(status).replace(/[^a-z0-9_-]/gi, '');
    const summaryMetrics = [
      _receiptMetric(_t('compactionViewer.receiptModel'), route.modelId || unknown),
      _receiptMetric(_t('compactionViewer.receiptProvider'), route.providerId || unknown),
      _receiptMetric(_t('compactionViewer.receiptRoutePolicy'), policyLabel),
      _receiptMetric(_t('compactionViewer.receiptRouteReason'), route.reason || unknown),
      _receiptMetric(_t('compactionViewer.receiptComplete'), boolean(summary.complete)),
      _receiptMetric(_t('compactionViewer.receiptAccepted'), boolean(summary.accepted)),
      _receiptMetric(_t('compactionViewer.receiptPricingKnown'), boolean(cost.known)),
      _receiptMetric(_t('compactionViewer.receiptUsd'), usd(cost.costUsd, cost.known)),
      cost.known === false && finite(cost.knownCostUsd)
        ? _receiptMetric(_t('compactionViewer.receiptKnownSubtotal'), usd(cost.knownCostUsd, true)) : '',
      _receiptMetric(_t('compactionViewer.receiptDuration'), finite(summary.durationMs) ? `${count(summary.durationMs)} ms` : unknown),
      _receiptMetric(_t('compactionViewer.receiptUsage'), `${tokens(usage.inputTokens)} + ${tokens(usage.outputTokens)}`),
      _receiptMetric(_t('compactionViewer.receiptProjectedUsage'), tokens(summary.projectedUsageTokens)),
      _receiptMetric(_t('compactionViewer.receiptSummaryChars'), count(summary.chars)),
    ].join('');
    const calls = Array.isArray(cost.calls) ? cost.calls.slice(0, 16).filter(call => call && typeof call === 'object') : [];
    const callsBlock = calls.length ? `<details class="cd-receipt-files">
      <summary>${_esc(_t('compactionViewer.receiptAttempts'))}</summary>
      <ul>${calls.map(call => `<li><code>${_esc(call.model || unknown)}</code> · <code>${_esc(call.providerId || unknown)}</code> · ${_esc(usd(call.costUsd, call.known))}${call.reason ? ` · ${_esc(call.reason)}` : ''}</li>`).join('')}</ul>
    </details>` : '';
    const economicsBlock = economics ? `
      <section class="cd-receipt-group">
        <h4>${_esc(_t('compactionViewer.receiptEconomics'))}</h4>
        <div class="cd-receipt-metrics">
          ${_receiptMetric(_t('compactionViewer.receiptDroppedTokens'), tokens(economics.droppedTokens))}
          ${_receiptMetric(_t('compactionViewer.receiptCacheRewrite'), tokens(economics.cacheRewriteTokens))}
          ${_receiptMetric(_t('compactionViewer.receiptPricingKnown'), boolean(economics.pricingKnown))}
          ${_receiptMetric(_t('compactionViewer.receiptSummaryCost'), usd(economics.summaryCostUsd, economics.pricingKnown))}
          ${_receiptMetric(_t('compactionViewer.receiptCacheRewriteUsd'), usd(economics.cacheRewriteCostUsd, economics.pricingKnown))}
          ${_receiptMetric(_t('compactionViewer.receiptPayback'), economics.pricingKnown === true
            ? (finite(economics.paybackRounds) ? _t('compactionViewer.receiptRounds', { n: economics.paybackRounds })
              : (economics.paybackRounds === null ? _t('compactionViewer.receiptNoFinitePayback') : unknown))
            : unknown)}
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
          ${receipt.summary ? `<section class="cd-receipt-group"><h4>${_esc(_t('compactionViewer.receiptSummaryRun'))}</h4><div class="cd-receipt-metrics">${summaryMetrics}</div>${callsBlock}${summary.rejectionReason ? `<p class="cd-receipt-outcome">${_esc(summary.rejectionReason)}</p>` : ''}</section>` : ''}
          ${budgetBlock}${economicsBlock}${evidenceBlock}${recoveryBlock}
          <p class="cd-receipt-outcome">${_esc(_t('compactionViewer.receiptCostCaveat'))}</p>
        </div>
      </section>`;
  }

  function _renderSummaryTab() {
    const el = _ensureDrawer().querySelector('.compaction-drawer-content');
    const a = _state && _state.activeArchive;
    if (!a) return;
    const receipt = _renderReceipt(a.receipt);

    if (a.receipt?.summary?.complete === false && a.summary) {
      el.innerHTML = receipt + `<div class="cd-empty">${_esc(_t('compactionViewer.incompleteSummaryHidden'))}</div>`;
      return;
    }
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
    const which = tab ? tab.dataset.tab : 'messages';
    const state = _state;
    const version = state?.selectionVersion;
    const current = () => _state === state && state?.selectionVersion === version
      && el.querySelector('.compaction-tab.is-active') === tab;
    if (which === 'messages') {
      _showLoading(true);
      try {
        await _ensureActiveMessages();
        if (current()) _renderMessagesTab();
      } catch (e) {
        if (!current()) return;
        console.error('[compaction-viewer] post load failed:', e);
        const content = el.querySelector('.compaction-drawer-content');
        content.innerHTML = `<div class="cd-empty cd-error">${_esc(_t('compactionViewer.loadFailed', { err: (e.message || String(e)) }))}</div>`;
      } finally {
        if (current()) _showLoading(false);
      }
    }
    else if (which === 'summary') { _showLoading(false); _renderSummaryTab(); }
    else if (which === 'history') { _showLoading(false); _renderHistoryTab(); }
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
      const payload = await _fetchPayload(convId, archiveId, 'summary');
      if (_state !== selectionState
          || selectionState.selectionVersion !== selectionVersion) return;
      _state.archiveId = archiveId;
      _state.activeArchive = payload.archive || {};
      _state.activeMessages = null;
      _state.postMissing = false;
      _state.postSnapshot = null;
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
      postMissing: false,
    };
    _state = openingState;
    // Fade main UI
    document.body.classList.add('compaction-drawer-open');
    el.classList.add('is-open');
    el.setAttribute('aria-hidden', 'false');
    _showLoading(true);
    // Messages-first: inspect the stored compaction result, not a wire capture.
    el.querySelectorAll('.compaction-tab').forEach(b => b.classList.remove('is-active'));
    el.querySelector('.compaction-tab[data-tab="messages"]').classList.add('is-active');

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
    const state = _state;
    const version = state?.selectionVersion;
    if (!state) return;
    try {
      await _ensureActiveMessages();
    } catch (err) {
      if (typeof showToast === 'function') showToast(_t('compactionViewer.copyFailed', { err: err.message }), 'error');
      return;
    }
    if (_state !== state || state.selectionVersion !== version) return;
    const txt = JSON.stringify(compactionPostExport(
      state.activeArchive, state.activeMessages, state.postSnapshot), null, 2);
    navigator.clipboard.writeText(txt).then(() => {
      if (typeof showToast === 'function') showToast('✅ ' + _t('compactionViewer.copied'), 'info');
    }, (err) => {
      console.error('[compaction-viewer] copy failed:', err);
      if (typeof showToast === 'function') showToast(_t('compactionViewer.copyFailed', { err: err.message }), 'error');
    });
  }

  async function _downloadSnapshot() {
    const state = _state;
    const version = state?.selectionVersion;
    if (!state || !state.archiveId) return;
    try {
      await _ensureActiveMessages();
      if (_state !== state || state.selectionVersion !== version) return;
      // Copy and download share the selected post-snapshot projection. Raw
      // HTTP bytes can carry a transport envelope and must not become a file.
      const blob = new Blob([JSON.stringify(compactionPostExport(
        state.activeArchive, state.activeMessages, state.postSnapshot), null, 2)],
        { type: 'application/json; charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `compaction-${state.convId.slice(0, 8)}-${state.archiveId}-post.json`;
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

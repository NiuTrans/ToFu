/* ══════════════════════════════════════════════════════════════════════════
 * compaction-viewer.js — Right-side drawer for inspecting pre-compaction
 * context snapshots persisted in transcript_archive.
 *
 * Public API:
 *   window.openCompactionViewer(convId, archiveId?)   — open drawer (lazy-loads)
 *   window.closeCompactionViewer()                     — close drawer
 *   window.attachCompactionMarkersToConversation(conv) — populate _compactions
 *                                                        on messages after load
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
(function () {
  'use strict';

  // ── Reuse global escapeHtml if present, fall back to a tight local one ─
  const _esc = (window.escapeHtml) ? window.escapeHtml : (s) => {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };

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

  const _fmtTime = (secs) => {
    try {
      const d = new Date(Number(secs) * 1000);
      return d.toLocaleString();
    } catch (_e) { return String(secs); }
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

  // ── Cache: archive id → { messages, archive } ───────────────────────
  // Payloads can be megabytes so we cache per-session (not persisted).
  // Cleared on conversation switch via the attach helper below.
  const _payloadCache = new Map();
  let _currentConv = null;

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
        window.closeCompactionViewer();
      }
    });
    // Tab switching
    el.querySelectorAll('.compaction-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        el.querySelectorAll('.compaction-tab').forEach(b => b.classList.remove('is-active'));
        btn.classList.add('is-active');
        _renderActiveTab();
      });
    });
    // Escape key
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && el.classList.contains('is-open')) {
        window.closeCompactionViewer();
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
    return await Api.compactions.list(convId);
  }

  async function _fetchPayload(convId, archiveId) {
    const key = `${convId}:${archiveId}`;
    if (_payloadCache.has(key)) return _payloadCache.get(key);
    const j = await Api.compactions.get(convId, archiveId);
    _payloadCache.set(key, j);
    return j;
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
          <span class="cd-meta-v">${_fmtTokens(a.tokensBefore)} → ${_fmtTokens(a.tokensAfter)} <em>(${reductionTxt})</em></span>
        </div>
        <div class="cd-meta-row">
          <span class="cd-meta-k">${_esc(_t('compactionViewer.metaModel'))}</span>
          <span class="cd-meta-v">${_esc(a.model || '—')}</span>
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
      inner += `<pre class="cd-content"><code>${_esc(m.content)}</code></pre>`;
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

  function _renderSummaryTab() {
    const el = _ensureDrawer().querySelector('.compaction-drawer-content');
    const a = _state && _state.activeArchive;
    if (!a || !a.summary) {
      el.innerHTML = `<div class="cd-empty">
        <p>${_esc(_t('compactionViewer.noSummary1'))}</p>
        <p>${_esc(_t('compactionViewer.noSummary2'))}</p>
      </div>`;
      return;
    }
    // Render as code-looking prose — don't run markdown since we already
    // show it inline in the tool panel elsewhere.
    el.innerHTML = `<pre class="cd-summary"><code>${_esc(a.summary)}</code></pre>`;
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
      const reduction = (c.tokensBefore > 0 && c.tokensAfter > 0)
        ? `-${Math.round((1 - c.tokensAfter / c.tokensBefore) * 100)}%`
        : '—';
      return `<li class="cd-history-item ${isActive ? 'is-active' : ''}"
                  data-archive-id="${c.id}">
        <div class="cd-history-head">
          <span class="cd-history-trigger cd-history-trigger-${_esc(trig)}">${_triggerLabel(trig)}</span>
          <span class="cd-history-time">${_esc(_fmtTime(c.createdAt))}</span>
        </div>
        <div class="cd-history-stats">
          <span>${_fmtTokens(c.tokensBefore)} → ${_fmtTokens(c.tokensAfter)} <em>(${reduction})</em></span>
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
        const id = parseInt(li.dataset.archiveId, 10);
        if (id && _state && _state.convId) {
          await _selectArchive(_state.convId, id);
        }
      });
    });
  }

  function _renderActiveTab() {
    const el = _ensureDrawer();
    const tab = el.querySelector('.compaction-tab.is-active');
    const which = tab ? tab.dataset.tab : 'messages';
    if (which === 'messages') _renderMessagesTab();
    else if (which === 'summary') _renderSummaryTab();
    else if (which === 'history') _renderHistoryTab();
  }

  function _showLoading(on) {
    const el = _ensureDrawer();
    el.querySelector('.compaction-drawer-loading').style.display = on ? 'block' : 'none';
    el.querySelector('.compaction-drawer-content').style.display = on ? 'none'  : 'block';
  }

  async function _selectArchive(convId, archiveId) {
    _showLoading(true);
    try {
      const payload = await _fetchPayload(convId, archiveId);
      _state.archiveId = archiveId;
      _state.activeArchive = payload.archive || {};
      _state.activeMessages = payload.messages || [];
      _renderMeta();
      _renderActiveTab();
    } catch (e) {
      console.error('[compaction-viewer] load failed:', e);
      const el = _ensureDrawer().querySelector('.compaction-drawer-content');
      el.innerHTML = `<div class="cd-empty cd-error">${_esc(_t('compactionViewer.loadFailed', { err: (e.message || String(e)) }))}</div>`;
    } finally {
      _showLoading(false);
    }
  }

  // ────────────────────────────────────────────────────────────────────
  //  Public API
  // ────────────────────────────────────────────────────────────────────
  window.openCompactionViewer = async function (convId, archiveId) {
    if (!convId) {
      console.warn('[compaction-viewer] openCompactionViewer: missing convId');
      return;
    }
    const el = _ensureDrawer();
    _state = { convId, archiveId: null, listData: null,
               activeArchive: null, activeMessages: null };
    // Fade main UI
    document.body.classList.add('compaction-drawer-open');
    el.classList.add('is-open');
    el.setAttribute('aria-hidden', 'false');
    _showLoading(true);
    // Reset tab to messages
    el.querySelectorAll('.compaction-tab').forEach(b => b.classList.remove('is-active'));
    el.querySelector('.compaction-tab[data-tab="messages"]').classList.add('is-active');

    try {
      // Fetch list of archives (for history tab + latest-selection fallback)
      const listData = await _fetchList(convId);
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
      console.error('[compaction-viewer] list failed:', e);
      _showLoading(false);
      const bodyEl = el.querySelector('.compaction-drawer-content');
      bodyEl.innerHTML = `<div class="cd-empty cd-error">${_esc(_t('compactionViewer.historyFailed', { err: (e.message || String(e)) }))}</div>`;
    }
  };

  window.closeCompactionViewer = function () {
    const el = document.getElementById('compactionViewerDrawer');
    if (!el) return;
    el.classList.remove('is-open');
    el.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('compaction-drawer-open');
    _state = null;
  };

  // Live language-switch hook (called by i18n.js::_onLanguageChange). The
  // drawer's static chrome re-translates via the whole-DOM _applyI18n() scan;
  // here we re-render the JS-built meta rows + active tab so they follow the
  // new language too. No-op when the drawer is closed / has no state.
  window._cvOnLanguageChange = function () {
    const el = document.getElementById('compactionViewerDrawer');
    if (!el || !el.classList.contains('is-open') || !_state) return;
    if (_state.activeArchive) _renderMeta();
    _renderActiveTab();
  };

  function _copyJson() {
    if (!_state || !_state.activeMessages) return;
    const txt = JSON.stringify({
      archive: _state.activeArchive,
      messages: _state.activeMessages,
    }, null, 2);
    navigator.clipboard.writeText(txt).then(() => {
      if (window.showToast) window.showToast('✅ ' + _t('compactionViewer.copied'), 'info');
    }, (err) => {
      console.error('[compaction-viewer] copy failed:', err);
      if (window.showToast) window.showToast(_t('compactionViewer.copyFailed', { err: err.message }), 'error');
    });
  }

  function _downloadSnapshot() {
    if (!_state || !_state.activeMessages) return;
    const payload = {
      archive: _state.activeArchive,
      messages: _state.activeMessages,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)],
                         { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `compaction-${_state.convId.slice(0, 8)}-${_state.archiveId}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 500);
  }

  // ────────────────────────────────────────────────────────────────────
  //  Conversation load hook — populate _compactions markers on reload
  //
  //  Problem: SSE-driven markers live on the in-memory assistant message;
  //  after a page refresh, the frontend reconstructs messages from the DB
  //  but the transient _compactions chip state is lost.
  //
  //  Solution: on conversation open, fetch the compaction list and match
  //  each archive to the nearest assistant message by taskId. Messages
  //  without a matching task_id (old compactions, pre-migration) are
  //  attached to the last assistant message so they're still discoverable.
  //
  //  Called from ui.js after a conversation's messages have been loaded
  //  into the DOM. Also invalidates the payload cache so stale entries
  //  from a different conversation don't bleed across.
  // ────────────────────────────────────────────────────────────────────
  window.attachCompactionMarkersToConversation = async function (convId, messages) {
    if (!convId || !Array.isArray(messages) || !messages.length) return;
    if (_currentConv !== convId) {
      _payloadCache.clear();
      _currentConv = convId;
    }
    // Clear any stale markers (e.g. re-load of same conv)
    for (const m of messages) { if (m && m._compactions) m._compactions = []; }

    let data;
    try {
      data = await _fetchList(convId);
    } catch (e) {
      console.debug('[compaction-viewer] attach list failed:', e);
      return;
    }
    const archives = (data && data.compactions) || [];
    if (!archives.length) return;

    // Index assistant messages by _taskId for fast matching.
    const byTaskId = new Map();
    const assistantIdx = [];
    messages.forEach((m, i) => {
      if (m && m.role === 'assistant') {
        assistantIdx.push(i);
        if (m._taskId) byTaskId.set(m._taskId, m);
      }
    });
    const lastAssistant = assistantIdx.length
      ? messages[assistantIdx[assistantIdx.length - 1]] : null;

    for (const a of archives) {
      const marker = {
        archiveId:    a.id,
        convId:       a.convId || convId,
        trigger:      a.trigger || 'force',
        roundNum:     a.roundNum || 0,
        tokensBefore: a.tokensBefore || 0,
        tokensAfter:  a.tokensAfter || 0,
        msgsBefore:   a.msgsBefore || 0,
        msgsAfter:    a.msgsAfter || 0,
        model:        a.model || '',
        reason:       a.reason || '',
        ts:           a.createdAt || 0,
        status:       'done',
      };
      let target = (a.taskId && byTaskId.get(a.taskId)) || lastAssistant;
      if (!target) continue;
      if (!Array.isArray(target._compactions)) target._compactions = [];
      if (!target._compactions.some(c => c.archiveId === marker.archiveId)) {
        target._compactions.push(marker);
      }
    }
    // After re-hydrating markers from the server, refresh the gauge so
    // its ticks reflect this conversation's history (the bar reads the
    // same _compactions[] arrays we just populated above).
    if (typeof window.updateContextBar === 'function') {
      window.updateContextBar();
    }
  };
})();

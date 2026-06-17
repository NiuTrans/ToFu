/* ═══════════════════════════════════════════════════════════════════
   finish info — extracted from ui.js (split 2026-05-28)

   renderFinishInfo (usage/cost) + file-changes bar.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ── Scroll branch panel to bottom ──
function renderFinishInfo(msg) {
  if (!msg.finishReason && !msg.usage && !msg.model && !msg.preset && !msg.effort) return "";
  const parts = [];
  const _mid = msg.model || msg.preset || msg.effort || "";
  const _pid = msg.provider_id || msg.providerId || "";
  const u = msg.usage || {};
  const fmt = (n) => (n >= 1000000 ? (n / 1000000).toFixed(1) + "m" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : n.toString());
  const thk = u.reasoning_tokens || u.thinking_tokens || 0;

  // ★ Model tag — auto-detect brand from model_id
  const depthIcons = { medium: '', high: '', max: '' };
  const depthLabels = { medium: "Med", high: "Hi", max: "Max" };
  // ★ Resolve the ACTUAL slot that served this turn — real model / key /
  //   provider, recorded by the dispatcher in usage._dispatch. The preset
  //   ("opus") and msg.model can be an alias that routes to a different
  //   upstream model, so prefer the resolved values for an honest finish bar.
  let _disp = null;
  const _dispRounds = msg.apiRounds || [];
  for (let i = _dispRounds.length - 1; i >= 0; i--) {
    const d = ((_dispRounds[i] && _dispRounds[i].usage) || {})._dispatch;
    if (d && (d.model || d.provider_id || d.key)) { _disp = d; break; }
  }
  if (!_disp && u && u._dispatch) _disp = u._dispatch;
  const _realModel = (_disp && _disp.model) || _mid;
  const _realProvider = (_disp && _disp.provider_id) || _pid;
  // Friendly key display: prefer the last 4 chars of the real API key
  //   (rendered as ••1234), else fall back to the raw slot name.
  const _keyTail = _disp && _disp.key_tail;
  const _keyDisplay = _keyTail ? ('••' + _keyTail) : (_disp && _disp.key) || "";

  if (_mid) {
    const _brand = typeof _detectBrand === 'function' ? _detectBrand(_realModel) : 'generic';
    const icon = (typeof _brandSvg === 'function') ? _brandSvg(_brand, 12) : '✦';
    // Show the actual model id (e.g. "aws.claude-opus-4.8"), not the
    // friendly short name — the user wants the real upstream model here.
    const displayName = _realModel;
    // Append thinking depth ONLY for thinking-capable models
    const depth = msg.thinkingDepth || "";
    let depthStr = "";
    const _isThinkModel = typeof _isThinkingCapable === 'function' ? _isThinkingCapable(_realModel) : false;
    if (depth && depthLabels[depth] && _isThinkModel) {
      depthStr = ` ${depthIcons[depth] || ""}${depthLabels[depth]}`;
    }
    parts.push(
      `<span class="finish-tag preset" data-preset="${_brand}" title="Model: ${escapeHtml(_realModel)}${depth ? ' · Depth: ' + escapeHtml(depth) : ''}">${icon} ${displayName}${depthStr}</span>`,
    );
  }

  // ★ Route tag — the actual provider + API-key slot that served this turn.
  if (_realProvider || _keyDisplay) {
    const _provName = (typeof _providerDisplayName === 'function')
      ? _providerDisplayName(_realProvider) : (_realProvider || "");
    const _routeBits = [];
    if (_provName) _routeBits.push(escapeHtml(_provName));
    if (_keyDisplay) _routeBits.push(escapeHtml(_keyDisplay));
    if (_routeBits.length) {
      const _routeTip = [
        _realProvider ? `Provider: ${escapeHtml(_realProvider)}` : "",
        _keyDisplay ? `Key: ${escapeHtml(_keyDisplay)}` : "",
        _realModel ? `Actual model: ${escapeHtml(_realModel)}` : "",
      ].filter(Boolean).join("\n");
      parts.push(`<span class="finish-tag route" title="${_routeTip}">${_routeBits.join(" · ")}</span>`);
    }
  }

  // ★ Finish reason tag — separate from model
  if (msg.finishReason) {
    const normReasons = ["stop", "end_turn", "stop_sequence"];
    const isNorm = normReasons.includes(msg.finishReason);
    const warnReasons = [
      "length",
      "tool_rounds_exhausted",
      "max_tokens",
      "content_filter",
      "premature_close",
      "abnormal_stop",
    ];
    if (isNorm) {
      parts.push(`<span class="finish-tag ok">✓</span>`);
    } else if (msg.finishReason === "error") {
      parts.push(`<span class="finish-tag err">✕ Error</span>`);
    } else if (msg.finishReason === "aborted") {
      parts.push(`<span class="finish-tag warn">Stopped</span>`);
    } else if (msg.finishReason === "interrupted") {
      parts.push(`<span class="finish-tag warn"><span title="Server crashed during generation. Content recovered from last checkpoint — may be incomplete.">Interrupted</span></span>`);
    } else if (msg.finishReason === "server_offline") {
      parts.push(
        `<span class="finish-tag err"><span title="Server went offline during generation (e.g. VSCode disconnect, network drop). Partial response saved.">Server Offline</span></span>` +
        ` <button class="finish-reconnect-btn" onclick="_recoverOfflineConversations('manual_button')" ` +
        `title="Check server for completed result" style="` +
        `font-size:11px;padding:1px 8px;margin-left:4px;cursor:pointer;` +
        `background:var(--accent);color:#fff;border:none;border-radius:4px;` +
        `vertical-align:middle;opacity:0.9` +
        `">🔄 Reconnect</button>`
      );
    } else {
      const labels = {
        length: "Truncated",
        tool_use: "Tool",
        tool_calls: "Tool",
        content_filter: "<span title='" + t('msg.contentFiltered') + "'>Filtered</span>",
        tool_rounds_exhausted: "Tool limit",
        max_tokens: "Truncated",
        premature_close: "<span title='" + t('msg.prematureClose') + "'>" + t('msg.gatewayInterrupt') + "</span>",
        abnormal_stop: "<span title='" + t('msg.abnormalStop') + "'>" + t('msg.abnormalInterrupt') + "</span>",
      };
      const label = labels[msg.finishReason] || msg.finishReason;
      const cls = warnReasons.includes(msg.finishReason) ? "warn" : "";
      parts.push(`<span class="finish-tag ${cls}">${label}</span>`);
    }
  }
  if (u) {
    const inp = u.prompt_tokens || u.input_tokens || 0;
    const out = u.completion_tokens || u.output_tokens || 0;
    // ★ API rounds info
    const rounds = msg.apiRounds || [];
    const numRounds = rounds.length;
    /* ★ Compute display input: for Anthropic-style APIs, prompt_tokens is
     *   only the uncached portion. The total input = uncached + cw + cr. */
    const _cw0 = u.cache_write_tokens || u.cache_creation_input_tokens || 0;
    const _cr0 = u.cache_read_tokens || u.cache_read_input_tokens || 0;
    const _displayInp = (inp <= _cw0 + _cr0 && (_cw0 > 0 || _cr0 > 0))
      ? inp + _cw0 + _cr0   /* Anthropic: inp is uncached only */
      : inp;                /* OpenAI: inp is already total */
    if (_displayInp > 0 || out > 0) {
      let tokText = `${fmt(_displayInp)} → ${fmt(out)}`;
      if (thk > 0)
        tokText += ` <span style="color:#a78bfa;opacity:0.8">(${fmt(thk)}${t('msg.thinking')})</span>`;
      if (numRounds > 1)
        tokText += ` <span style="opacity:0.7">[${numRounds}${t('msg.rounds')}]</span>`;
      parts.push(`<span class="token-tag">${tokText}</span>`);
    }
    const cw = u.cache_write_tokens || u.cache_creation_input_tokens || 0;
    const cr = u.cache_read_tokens || u.cache_read_input_tokens || 0;
    // ★ Enhanced cost display with per-round breakdown.
    //   Prefer the persisted `msg.cost` (stamped server-side at sync time
    //   in lib/tasks_pkg/manager._sync_result_to_conversation). Falls back
    //   to the lazy calcCostCny fetch for legacy messages stored before
    //   the persistence change.
    const costInfo = msg.cost || calcCostCny(u, _mid, _pid);
    if (costInfo && costInfo.costCny > 0) {
      const fCny = (v) =>
        v >= 0.01 ? "¥" + v.toFixed(3) : v > 0 ? "¥" + v.toFixed(4) : "¥0";
      const tipLines = [];
      if (numRounds > 1) {
        tipLines.push(`共 ${numRounds} 轮 API 调用`);
        tipLines.push("");
        rounds.forEach((rd, i) => {
          const ru = rd.usage || {};
          const ri = ru.prompt_tokens || ru.input_tokens || 0;
          const ro = ru.completion_tokens || ru.output_tokens || 0;
          const rt = ru.reasoning_tokens || ru.thinking_tokens || 0;
          const rcw =
            ru.cache_write_tokens || ru.cache_creation_input_tokens || 0;
          const rcr = ru.cache_read_tokens || ru.cache_read_input_tokens || 0;
          const rdCost = rd.cost || calcCostCny(ru, _mid, rd.provider_id || rd.providerId || _pid);
          const rdCnyStr = rdCost ? fCny(rdCost.costCny) : "¥0";
          let rdLabel = `第${i + 1}轮`;
          if (rd.tag && rd.tag.includes("FALLBACK"))
            rdLabel += ` 回退(${rd.model || "?"})`;
          const rdTrace = (ru.trace_id && typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode) ? ` ${ru.trace_id.slice(0,8)}` : '';
          tipLines.push(
            `${rdLabel}: ${fmt(ri)}→${fmt(ro)}${rt > 0 ? " ✶" + fmt(rt) : ""} = ${rdCnyStr}${rcr > 0 ? " cache:" + fmt(rcr) : ""}${rcw > 0 ? " cw:" + fmt(rcw) : ""}${rdTrace}`,
          );
        });
        tipLines.push("");
      }
      /* ★ Show uncached input correctly — for Anthropic-style APIs where
       *   prompt_tokens is only the uncached residual, show that as the
       *   standard-rate input line, not the confusing raw prompt_tokens total */
      const _si = costInfo.inputTokens || 0;
      const _totalInp = costInfo.totalInputTokens || inp;
      if (_totalInp > _si && _si >= 0) {
        tipLines.push(
          `Input: ${fmt(_si)} tokens → ${fCny(costInfo.inputCostCny)}`,
        );
      } else {
        tipLines.push(
          `Input: ${fmt(inp)} tokens → ${fCny(costInfo.inputCostCny)}`,
        );
      }
      if (cw > 0)
        tipLines.push(
          `Cache write: ${fmt(cw)} tokens → ${fCny(costInfo.cacheWriteCostCny)}`,
        );
      if (cr > 0)
        tipLines.push(
          `Cache read: ${fmt(cr)} tokens → ${fCny(costInfo.cacheReadCostCny)}`,
        );
      tipLines.push(
        `Output: ${fmt(out)} tokens → ${fCny(costInfo.outputCostCny)}`,
      );
      if (thk > 0)
        tipLines.push(`Thinking: ${fmt(thk)} tokens (含在 output 中)`);
      if (costInfo.cacheSavingsCny > 0)
        tipLines.push(`Cache 节省: ${fCny(costInfo.cacheSavingsCny)}`);
      tipLines.push(`──────────`);
      tipLines.push(`Total: ${formatCny(costInfo.costCny)}`);
      // ★ Show trace_id(s) for debugging — all unique trace_ids from rounds (debug mode only)
      if (typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode) {
        const traceIds = rounds.map(rd => (rd.usage || {}).trace_id).filter(Boolean);
        const lastTrace = traceIds.length ? traceIds[traceIds.length - 1] : (u.trace_id || '');
        if (lastTrace) {
          tipLines.push(`TraceId: ${lastTrace}`);
        }
      }
      let savingsHtml = "";
      if (costInfo.cacheSavingsCny > 0)
        savingsHtml = ` <span style="color:#34d399;font-size:0.85em">↓${fCny(costInfo.cacheSavingsCny)}</span>`;
      parts.push(
        `<span class="cost-tag cost-tag-detail" title="${tipLines.join("\n")}">${formatCny(costInfo.costCny)}${savingsHtml}</span>`,
      );
    }
  }
  // ★ Clickable trace tag — click to copy full trace_id (debug mode only)
  if (typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode) {
    const _allTraces = (msg.apiRounds || []).map(rd => (rd.usage || {}).trace_id).filter(Boolean);
    const _lastTrace = _allTraces.length ? _allTraces[_allTraces.length - 1] : ((msg.usage || {}).trace_id || '');
    if (_lastTrace) {
      const _allStr = _allTraces.length > 1 ? _allTraces.join('\n') : _lastTrace;
      // Escape for safe embedding inside inline onclick JS string literal (single-quoted)
      const _jsSafe = _allStr.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n');
      parts.push(
        `<span class="finish-tag" style="cursor:pointer;opacity:0.6;font-size:0.8em" title="点击复制 trace_id:\n${escapeHtml(_allStr)}" onclick="_safeClipboardWrite('${_jsSafe}');this.textContent='copied'">${escapeHtml(_lastTrace.slice(0,8))}</span>`
      );
    }
  }
  if (msg.fallbackModel) {
    const _fbReason = msg.fallbackReason || msg.fallbackKind || "";
    const _reasonLine = _fbReason
      ? `\n失败原因 / Reason: ${_fbReason}`
      : "";
    const _tip = `原模型 ${msg.fallbackFrom || "?"} 失败，已回退到 ${msg.fallbackModel}${_reasonLine}`;
    parts.push(
      `<span class="finish-tag warn" title="${escapeHtml(_tip)}">Fallback → Opus</span>`,
    );
  }
  if (parts.length === 0) return "";
  return `<div class="message-finish">${parts.join("")}</div>`;
}

// ═══════════════════════════════════════════════════════════════════
// ★ File Changes Tracker — shows which files were written/patched
// ═══════════════════════════════════════════════════════════════════

/**
 * Extract file change info from toolRounds (during/after streaming).
 * Returns array of {path, action, ok} objects.
 */
// ── File-change extraction (server-authoritative) ─────────────────
//
// The derivation logic — which tool rounds count as file changes, how
// 'rootname:path' is parsed, dedup rules, pending-state tracking —
// lives in `lib/tool_changes.py`. The UI fetches the result from
// `POST /api/v1/messages/extract-file-changes`.
//
// During streaming we'd fire many requests, so the response is cached
// per-fingerprint. The streaming hot path is already fingerprint-gated
// at the call sites (search for `_roundsFp`), so the only async cost is
// one fetch per genuine state change.
//
// `_extractFileChangesFromRoundsAsync` returns a Promise<Array>.
// `_extractedFileChangesCache` caches the LAST computed result so
// synchronous render paths can read the cached entry while a fresh
// fetch is in flight.
const _extractedFileChangesCache = new Map();  // fp → files[]
let _extractedFileChangesPending = new Map();   // fp → Promise<files[]>

function _fcFingerprint(toolRounds) {
  if (!toolRounds || !toolRounds.length) return '';
  // Coarse fingerprint — enough to detect changes worth refetching.
  const last = toolRounds[toolRounds.length - 1];
  return toolRounds.length + ':' + (last.status || '') + ':' +
         (last.toolName || '') + ':' +
         ((last.results && last.results.length) || 0);
}

async function _extractFileChangesFromRoundsAsync(toolRounds) {
  if (!toolRounds || !toolRounds.length) return [];
  const fp = _fcFingerprint(toolRounds);
  if (_extractedFileChangesCache.has(fp)) {
    return _extractedFileChangesCache.get(fp);
  }
  const inflight = _extractedFileChangesPending.get(fp);
  if (inflight) return inflight;

  const promise = (async () => {
    try {
      const body = await Api.conversations.extractFileChanges(toolRounds);
      if (!body) {
        if (typeof debugLog === 'function') {
          debugLog('[FileChanges] extract failed', 'warn');
        }
        return [];
      }
      const files = Array.isArray(body.files) ? body.files : [];
      _extractedFileChangesCache.set(fp, files);
      // Cap the cache so it doesn't grow unbounded over a long session.
      if (_extractedFileChangesCache.size > 64) {
        const firstKey = _extractedFileChangesCache.keys().next().value;
        _extractedFileChangesCache.delete(firstKey);
      }
      return files;
    } catch (err) {
      if (typeof debugLog === 'function') {
        debugLog(`[FileChanges] extract failed: ${err && err.message}`, 'warn');
      }
      return [];
    } finally {
      _extractedFileChangesPending.delete(fp);
    }
  })();
  _extractedFileChangesPending.set(fp, promise);
  return promise;
}

/**
 * Batch-prefetch file-change lists for every message in a conversation
 * that needs server-side derivation (i.e. lacks `modifiedFileList`).
 * Seeds `_extractedFileChangesCache` so the synchronous render paths
 * inside `renderFileChangesBar` hit the cache instead of firing one
 * POST per message.
 *
 * Returns a Promise<boolean>: true if fresh entries landed (caller
 * should force a re-render), false if everything was already cached.
 */
async function _prefetchConvFileChanges(conv) {
  if (!conv || !conv.messages || !conv.messages.length) return false;
  const items = [];
  const fps = [];
  const _seen = new Set();
  for (const m of conv.messages) {
    if (!m || m.modifiedFileList && m.modifiedFileList.length) continue;
    if (!Array.isArray(m.toolRounds) || !m.toolRounds.length) continue;
    const fp = _fcFingerprint(m.toolRounds);
    if (!fp || _seen.has(fp)) continue;
    if (_extractedFileChangesCache.has(fp)) continue;
    if (_extractedFileChangesPending.has(fp)) continue;
    _seen.add(fp);
    items.push({ toolRounds: m.toolRounds });
    fps.push(fp);
  }
  if (!items.length) return false;
  // Per-fp resolvers so concurrent single-shot fetches that hit the
  // pending map get a Promise<files[]> with the right shape (the
  // single-shot contract from _extractFileChangesFromRoundsAsync).
  const _resolvers = fps.map(() => {
    /** @type {(v:any)=>void} */
    let resolve = () => {};
    const p = new Promise((r) => { resolve = r; });
    return { p, resolve };
  });
  for (let i = 0; i < fps.length; i++) {
    _extractedFileChangesPending.set(fps[i], _resolvers[i].p);
  }
  try {
    const body = await Api.conversations.extractFileChangesBatch(items);
    const results = body && Array.isArray(body.results) ? body.results : [];
    for (let i = 0; i < fps.length; i++) {
      const files = Array.isArray(results[i]) ? results[i] : [];
      _extractedFileChangesCache.set(fps[i], files);
      _resolvers[i].resolve(files);
    }
    while (_extractedFileChangesCache.size > 64) {
      const firstKey = _extractedFileChangesCache.keys().next().value;
      _extractedFileChangesCache.delete(firstKey);
    }
    return true;
  } catch (err) {
    if (typeof debugLog === 'function') {
      debugLog(`[FileChanges] batch prefetch failed: ${err && err.message}`, 'warn');
    }
    for (const r of _resolvers) r.resolve([]);
    return false;
  } finally {
    for (const fp of fps) _extractedFileChangesPending.delete(fp);
  }
}
window._prefetchConvFileChanges = _prefetchConvFileChanges;

/**
 * Synchronous accessor — returns the cached file-change list for a given
 * toolRounds blob, or `null` if not yet fetched. Render paths read this
 * to render the bar on the SAME tick the rounds change, then the async
 * fetch (kicked off in parallel) refreshes the cache for the NEXT tick.
 */
function _extractFileChangesFromRoundsCached(toolRounds) {
  if (!toolRounds || !toolRounds.length) return null;
  const fp = _fcFingerprint(toolRounds);
  return _extractedFileChangesCache.has(fp)
    ? _extractedFileChangesCache.get(fp)
    : null;
}

/**
 * Render the file-changes bar for a message.
 * Dual source: prefers modifiedFileList from done event; falls back to toolRounds extraction.
 */
function renderFileChangesBar(msg, msgIdx) {
  // Server-derived list (preferred — git-history backed, authoritative).
  if (msg.modifiedFileList && msg.modifiedFileList.length) {
    const files = msg.modifiedFileList.map(f => ({
      path: f.path, action: f.action, ok: true, count: 1,
      root: f.root || ''
    }));
    return _renderFileChangesHtml(files, false, msgIdx);
  }
  // Fallback: extract from toolRounds via /api/v1/messages/extract-file-changes.
  if (!msg.toolRounds || !msg.toolRounds.length) return '';
  const cached = _extractFileChangesFromRoundsCached(msg.toolRounds);
  if (cached !== null) {
    if (!cached.length) return '';
    return _renderFileChangesHtml(cached, false, msgIdx);
  }
  // No cached entry yet — kick off an async fetch and trigger a re-render
  // when it lands. Returning empty for THIS render tick is fine; the bar
  // appears on the next tick.
  _extractFileChangesFromRoundsAsync(msg.toolRounds).then(() => {
    // Re-render the message list once the cache is fresh.
    const conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
    if (conv && typeof renderChat === 'function') renderChat(conv);
  });
  return '';
}

/**
 * Core HTML renderer for file changes bar.
 * @param {Array} files - [{path, action, ok, count}]
 * @param {boolean} isStreaming - if true, add pulse animation
 */
function _renderFileChangesHtml(files, isStreaming, msgIdx) {
  if (!files.length) return '';
  const pendingCount = files.filter(f => f.pending).length;
  const okCount = files.filter(f => f.ok && !f.pending).length;
  const failCount = files.filter(f => !f.ok).length;
  const totalFiles = files.length;

  // Action icons
  const actionIcon = (action, ok, pending) => {
    if (pending) return '<span class="fc-icon fc-pending">⟳</span>';
    if (!ok) return '<span class="fc-icon fc-fail">✕</span>';
    if (action === 'created') return '<span class="fc-icon fc-created">+</span>';
    if (action === 'deleted') return '<span class="fc-icon fc-deleted">−</span>';
    if (action === 'modified') return '<span class="fc-icon fc-modified">∆</span>';
    if (action === 'patched') return '<span class="fc-icon fc-patched">~</span>';
    return '<span class="fc-icon fc-written">⇢</span>';
  };

  // Summary line
  const summaryParts = [];
  if (okCount > 0) summaryParts.push(`${okCount} file${okCount > 1 ? 's' : ''} changed`);
  if (pendingCount > 0) summaryParts.push(`${pendingCount} in progress`);
  if (failCount > 0) summaryParts.push(`${failCount} failed`);
  const summaryText = summaryParts.join(', ');
  const pulseClass = isStreaming ? ' fc-pulse' : '';
  const summaryIcon = '';

  // ★ Multi-root: show 'rootname:' prefix when > 1 workspace root is active
  //   so files from different projects are visually distinguishable.
  //   Uses window.projectState (defined in core.js) to detect multi-root mode.
  //   Safe when projectState is undefined (e.g. standalone rendering).
  const _ps = (typeof projectState !== 'undefined') ? projectState : null;
  const _extrasCount = (_ps && Array.isArray(_ps.extraRoots)) ? _ps.extraRoots.length : 0;
  const _multiRoot = _extrasCount > 0;
  // Also collect rootnames seen among the files — if multiple distinct roots
  // appear, force prefix display even if projectState is stale.
  const _rootsSeen = new Set();
  for (const f of files) if (f.root) _rootsSeen.add(f.root);
  const _showRootPrefix = _multiRoot || _rootsSeen.size > 1;

  // File list items
  const fileItems = files.map(f => {
    const dir = f.path.includes('/') ? f.path.substring(0, f.path.lastIndexOf('/') + 1) : '';
    const fname = f.path.includes('/') ? f.path.substring(f.path.lastIndexOf('/') + 1) : f.path;
    const countBadge = f.count > 1 ? ` <span class="fc-count">×${f.count}</span>` : '';
    const pendingCls = f.pending ? ' fc-file-pending' : '';
    const rootPrefix = (_showRootPrefix && f.root)
      ? `<span class="fc-root">${escapeHtml(f.root)}:</span>`
      : '';
    const fullPath = (f.root ? f.root + ':' : '') + f.path;
    return `<div class="fc-file${f.ok ? '' : ' fc-file-err'}${pendingCls}" title="${escapeHtml(fullPath)}">
      ${actionIcon(f.action, f.ok, f.pending)}
      <span class="fc-path">${rootPrefix}<span class="fc-dir">${escapeHtml(dir)}</span><span class="fc-fname">${escapeHtml(fname)}</span></span>
      <span class="fc-action">${escapeHtml(f.action)}${countBadge}</span>
    </div>`;
  }).join('');

  // ★ Undo button — only for finalized (non-streaming) messages with a valid msgIdx
  const undoBtn = (!isStreaming && typeof msgIdx === 'number')
    ? `<button class="fc-undo-btn" onclick="event.stopPropagation();undoConvModifications(${msgIdx})" title="撤销本轮修改">` +
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/></svg>` +
      `<span>Undo</span></button>`
    : '';

  // ★ Undo All button — always available as a separate interaction point
  const undoAllBtn = (!isStreaming && typeof msgIdx === 'number')
    ? `<button class="fc-undo-all-btn" onclick="event.stopPropagation();undoAllModifications()" title="撤销所有对话中的所有修改">` +
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/><line x1="12" y1="7" x2="12" y2="3"/><line x1="8" y1="7" x2="12" y2="7"/></svg>` +
      `<span>Undo All</span></button>`
    : '';

  const actionBtns = (undoBtn || undoAllBtn)
    ? `<div class="fc-actions">${undoBtn}${undoAllBtn}</div>` : '';

  // Auto-expand for ≤ 5 files so users see details immediately
  const autoExpand = totalFiles <= 5 ? ' fc-expanded' : '';
  return `<div class="file-changes-bar${pulseClass}${autoExpand}" data-fc-count="${totalFiles}">
    <div class="fc-summary" onclick="this.parentElement.classList.toggle('fc-expanded')">
      <span class="fc-summary-icon">${summaryIcon}</span>
      <span class="fc-summary-text">${summaryText}</span>
      ${actionBtns}
      <span class="fc-chevron">›</span>
    </div>
    <div class="fc-details">${fileItems}</div>
  </div>`;
}


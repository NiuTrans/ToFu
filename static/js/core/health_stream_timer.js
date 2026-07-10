/* ═══════════════════════════════════════════════════════════════════
   core/health_stream_timer.js — extracted from core.js (split 2026-05-28)

   Server health checks + stream-stuck detection + per-conv stream timer (twStart/twUpdate/twStop).

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/* ── Stream timing: elapsed display + health-check-based stuck detection ── */
const _streamTimers = new Map(); // convId → { startTime, lastDataTime, intervalId, healthState }
// _serverAlive: cached health state shared across all streams (avoid duplicate pings)
let _serverAlive = true;
let _lastHealthCheck = 0;
let _consecutiveHealthFails = 0;       // require 2+ consecutive fails to confirm dead
const _HEALTH_CHECK_INTERVAL = 10000;  // ms between health checks when silent
const _SILENCE_THRESHOLD = 20;         // seconds of silence before first health check (reduced from 30s for VS Code port forwarding)
const _SILENCE_SEVERE = 45;            // seconds before showing severe warning

/* Inline status SVGs for the in-bubble liveness banner. Per CLAUDE.md §3.4 +
 * the "no emoji in UI status, SVG only" directive: never use emoji for these
 * state markers. `currentColor` lets each banner variant tint the icon via its
 * text color. Kept tiny (13px) to sit inline with 11px banner text. */
const _LIVENESS_ICON_OK =
  '<svg class="stream-liveness-icon" width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M13.5 4.5 6.5 11.5 2.5 7.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const _LIVENESS_ICON_WARN =
  '<svg class="stream-liveness-icon" width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="M8 1.5 15 14H1L8 1.5Z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M8 6.2v3.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><circle cx="8" cy="11.6" r="1" fill="currentColor"/></svg>';
const _LIVENESS_ICON_DEAD =
  '<svg class="stream-liveness-icon" width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden="true"><circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1.6"/><path d="M5.5 5.5 10.5 10.5M10.5 5.5 5.5 10.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';

/* Guarded t(): several jsdom harnesses eval THIS file standalone (without
 * i18n.js) and drive _updateStreamTimerUI / _streamPhaseLabel — an unguarded
 * t() would throw ReferenceError there. In production i18n.js is bundled first
 * so t() is always present (→ zh, the primary UI). Fall back to the key only
 * when t() is absent (test-only path; never English). */
function _connT(key, params) {
  return (typeof t === 'function') ? t(key, params) : key;
}

function _fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m${rs > 0 ? String(rs).padStart(2,'0') + 's' : ''}`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h${rm > 0 ? String(rm).padStart(2,'0') + 'm' : ''}`;
}

/**
 * Derive a short human-readable label for what the stream is doing right now,
 * from the live phase buffer. Used by the stream timer so a long silence shows
 * *what* the server is busy with (tools, reasoning, retrying) rather than going
 * blank. Returns '' when there's no informative phase.
 */
function _streamPhaseLabel(buf) {
  const p = buf && buf.phase;
  if (!p || !p.phase) return '';
  // p.detail is backend-supplied dynamic text — pass it through verbatim (same
  // ruling as the assistant/critic prose: live server text isn't UI chrome).
  // Only OUR OWN generic fallback labels are localized (zh primary).
  switch (p.phase) {
    case 'tool_exec':
      return p.detail ? String(p.detail) : _connT('conn.phaseTools');
    case 'llm_thinking':
      return p.detail ? String(p.detail) : _connT('conn.phaseThinking');
    case 'thinking_active':
      return _connT('conn.phaseReasoning');
    case 'compacting':
      return p.detail ? String(p.detail) : _connT('conn.phaseCompacting');
    case 'retrying':
      return p.detail ? String(p.detail) : _connT('conn.phaseRetrying');
    case 'working':
      return p.detail ? String(p.detail) : _connT('conn.phaseWorking');
    case 'autopilot_thinking':
      return p.detail ? String(p.detail) : _connT('conn.phaseAutopilot');
    default:
      return '';
  }
}

/**
 * Check if backend server is alive. Returns true/false.
 * Result is cached for _HEALTH_CHECK_INTERVAL ms to avoid spamming.
 */
async function _checkServerHealth() {
  const now = Date.now();
  if (now - _lastHealthCheck < _HEALTH_CHECK_INTERVAL) return _serverAlive;
  _lastHealthCheck = now;
  try {
    const resp = await Api.health.check({ signal: AbortSignal.timeout(3000) });
    if (resp && resp.ok) {
      _serverAlive = true;
      _consecutiveHealthFails = 0;
    } else {
      _consecutiveHealthFails++;
      _serverAlive = _consecutiveHealthFails < 2; // need 2+ failures to confirm dead
    }
  } catch (e) {
    // Do NOT swallow the reason: a transient AbortSignal.timeout under load
    // and a genuine outage both land here but mean very different things. The
    // reason is the only trail distinguishing them when the 2nd consecutive
    // fail flips the user-visible "server offline" verdict.
    console.debug('[StreamTimer] health ping failed:', e && e.message);
    _consecutiveHealthFails++;
    _serverAlive = _consecutiveHealthFails < 2;
  }
  return _serverAlive;
}

/**
 * Check if PostgreSQL database is available on startup.
 * Shows a persistent warning banner if DB is down so users know
 * immediately instead of seeing silent "Waiting…" on first message.
 */
async function _checkDbHealth() {
  try {
    const resp = await Api.health.check({ signal: AbortSignal.timeout(3000) });
    if (!resp || !resp.ok) return;
    const data = await resp.json();
    if (data.db_ok === false) {
      _showDbWarningBanner();
      _startDbHealthPolling();
    } else {
      // DB is healthy — clear any stale banner left over from a prior outage
      // (e.g. the server was restarted with PG back up). Without this the red
      // "Database Unavailable" banner lingers forever once shown, falsely
      // telling the user the DB is still down.
      _clearDbWarningBanner();
    }
  } catch (e) {
    // A network/tunnel drop means the server is unreachable — _checkServerHealth
    // owns that verdict, so we don't show the DB banner here. But a .json()
    // parse failure or a 200-with-garbage payload ALSO lands here; log it so a
    // malformed health response isn't silently invisible (CLAUDE §2).
    console.debug('[DbHealth] health probe failed (server unreachable or bad payload):', e && e.message);
  }
}

/** Remove the DB-unavailable banner if present (DB recovered / false positive). */
function _clearDbWarningBanner() {
  const b = document.getElementById('db-warning-banner');
  if (b) {
    b.remove();
    console.info('[DbHealth] Database available again — cleared the "unavailable" banner');
  }
}

/* Self-stopping recovery poll: once the DB-warning banner is shown, re-probe
 * /api/health every 15s and clear the banner the moment the DB reports healthy
 * (the documented recovery path is a server restart with PG back up). Stops
 * itself when the banner is gone (recovered OR user-dismissed) so it costs
 * nothing in steady state. Mirrors _startOfflineRecoveryPolling's shape. */
let _dbHealthPollInterval = null;
function _startDbHealthPolling() {
  if (_dbHealthPollInterval) return;
  _dbHealthPollInterval = setInterval(async () => {
    if (!document.getElementById('db-warning-banner')) {
      clearInterval(_dbHealthPollInterval);
      _dbHealthPollInterval = null;
      return;
    }
    if (document.visibilityState !== 'visible') return;
    try {
      const resp = await Api.health.check({ signal: AbortSignal.timeout(3000) });
      if (!resp || !resp.ok) return;
      const data = await resp.json();
      if (data.db_ok !== false) {
        _clearDbWarningBanner();
        clearInterval(_dbHealthPollInterval);
        _dbHealthPollInterval = null;
      }
    } catch (e) {
      console.debug('[DbHealth] recovery poll failed:', e && e.message);
    }
  }, 15000);
}

function _showDbWarningBanner() {
  if (document.getElementById('db-warning-banner')) return;
  const banner = document.createElement('div');
  banner.id = 'db-warning-banner';
  banner.style.cssText =
    'position:fixed;top:0;left:0;right:0;z-index:10000;' +
    'background:#dc2626;color:#fff;padding:10px 16px;font-size:14px;' +
    'text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3);' +
    'display:flex;align-items:center;justify-content:center;gap:8px;';
  // Guarded t(): the jsdom test harnesses load this file WITHOUT i18n.js, so
  // fall back to the zh literal when t() isn't present. zh is the primary UI.
  const _tt = (typeof t === 'function')
    ? t
    : (k, p) => ({
        'conn.dbUnavailableTitle': '数据库不可用',
        'conn.dbUnavailableDesc': '未运行 PostgreSQL，对话与历史将无法使用。请安装 PostgreSQL（' + (p && p.cmd || '') + '）后重启服务器。',
        'conn.dismiss': '关闭',
      }[k] || k);
  const _installCmd = '<code style="background:rgba(255,255,255,.2);padding:1px 5px;border-radius:3px">conda install -c conda-forge postgresql>=18</code>';
  banner.innerHTML =
    '<span style="display:inline-flex"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg></span>' +
    '<span><b>' + _tt('conn.dbUnavailableTitle') + '</b> — ' +
    _tt('conn.dbUnavailableDesc', { cmd: _installCmd }) + '</span>' +
    '<button onclick="this.parentElement.remove()" style="' +
    'background:rgba(255,255,255,.2);border:none;color:#fff;padding:4px 10px;' +
    'border-radius:4px;cursor:pointer;font-size:13px;margin-left:12px;' +
    'white-space:nowrap">' + _tt('conn.dismiss') + '</button>';
  document.body.prepend(banner);
}

/**
 * Force-finish a stream for a given convId when server is detected as dead.
 * Sets finishReason so the user sees what happened.
 */
function _forceFinishDeadStream(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (conv) {
    const last = conv.messages[conv.messages.length - 1];
    if (last && last.role === 'assistant' && !last.finishReason) {
      last.finishReason = 'server_offline';
      // Guarded t() — this file is loaded WITHOUT i18n.js in the jsdom harness,
      // so fall back to the zh literal (zh is the primary UI language).
      const _tt = (typeof t === 'function')
        ? t
        : (k) => ({
            'conn.streamOfflineMsg': '服务器离线，回复可能不完整。服务器恢复后会自动重连。',
            'finishInfo.reasonServerOffline': '服务器离线',
            'conn.offlineToastDetail': '后端服务器无响应。已保存部分回复，连接恢复后会自动恢复。',
            'err.conn.hint': '连接似乎中断了，但服务器很可能已经完成生成。请不要重新生成或编辑——那会丢弃服务器上已完成的结果。点击下方「恢复」或刷新页面，即可取回完整回复。',
          }[k] || k);
      /* Use the typed envelope so the error block renders consistently with
       * backend-emitted errors. The kind=server_offline carries hint + label,
       * and renderErrorEnvelope overrides the title/hint with the friendly
       * err.conn.* copy + a Recover button. Stamp the hint here too (not '')
       * so guidance never depends on which path stamped the error — a legacy
       * renderer that doesn't apply the override still shows real guidance. */
      last.error = normalizeErrorEnvelope({
        kind: 'server_offline', severity: 'warning', retryable: true,
        message: _tt('conn.streamOfflineMsg'),
        hint: _tt('err.conn.hint'), detail: 'Frontend health-check failed', model: '', context: '', source: 'frontend-health-check', raw: '',
      });
    }
  }
  const _ttToast = (typeof t === 'function')
    ? t
    : (k) => ({
        'finishInfo.reasonServerOffline': '服务器离线',
        'conn.offlineToastDetail': '后端服务器无响应。已保存部分回复，连接恢复后会自动恢复。',
      }[k] || k);
  // Abort the SSE controller so _trySSE / _pollFallback also exit
  const s = activeStreams.get(convId);
  if (s && s.controller) {
    try { s.controller.abort(); } catch {}
  }
  twStop(convId);
  finishStream(convId);
  showToast('⚠️', _ttToast('finishInfo.reasonServerOffline'),
    _ttToast('conn.offlineToastDetail'),
    10000);
  // ★ Start periodic recovery polling so the result is auto-recovered when server comes back
  _startOfflineRecoveryPolling();
}

/**
 * Self-heal a conversation whose ``activeTaskId`` points at a task that is no
 * longer running on the server (terminal status, or 404 = the task was
 * discarded / TTL-evicted / never finalized).  This is the autopilot/summarize
 * "stuck Waiting… placeholder" recovery: a phantom running task (e.g. the old
 * summarize carrier) births an empty assistant placeholder + an SSE that never
 * completes, so the bubble shows "等待中…" forever.  The hard project rule is
 * that any non-input-box-driven flow must SELF-HEAL — never require a manual
 * force-refresh.
 *
 * Reuses the existing probe→recover mechanism (no second system):
 *   • If the trailing assistant is an EMPTY ghost (no content / thinking / real
 *     tool round) → it was an orphan placeholder for a task that produced
 *     nothing.  Remove it and clear the running predicate
 *     (``conv.activeTaskId`` + ``activeStreams``).
 *   • If it DID accumulate content → abort the stale SSE with ``_probeAbort``
 *     so ``_trySSE`` falls through to ``_pollFallback``, which lands the real
 *     result and finalizes (the same path the proxy-swallowed-done case uses).
 *
 * Returns true when it took recovery action (placeholder reclaimed or SSE
 * re-routed), false otherwise.  Best-effort, idempotent.
 *
 * @param {string} convId
 * @param {{status?: string, notFound?: boolean, background?: boolean}} probe — poll
 *        outcome: ``notFound`` for a 404, else the task's terminal ``status``.
 *        ``background`` gates the conv-agnostic sweep (no live stream) branch.
 */
function _healStuckPlaceholder(convId, probe) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return false;
  const taskId = conv.activeTaskId;
  if (!taskId) return false;
  const last = conv.messages[conv.messages.length - 1];
  // Empty-ghost predicate (mirrors initActiveTasks `_classifyGhostTail`): an
  // assistant turn with no settled output and no REAL tool round.
  const _isEmptyGhost = !!last && last.role === 'assistant'
    && !last.content && !last.finishReason && !last.usage && !last.error
    && !(Array.isArray(last.toolRounds) && last.toolRounds.some(r =>
        r && (r.status === 'done' || r.toolContent
          || (Array.isArray(r.results) && r.results.length))))
    && !last.thinking;
  if (_isEmptyGhost) {
    console.warn(
      `[StreamTimer] ★ SELF-HEAL — conv=${convId.slice(0,8)} task=${taskId.slice(0,8)} ` +
      `is ${probe && probe.notFound ? '404 (gone)' : 'terminal (' + (probe && probe.status) + ')'} ` +
      `and the trailing assistant is an empty orphan placeholder — reclaiming it ` +
      `(clearing activeTaskId + activeStreams).`
    );
    conv.messages.pop();
    conv.activeTaskId = null;
    conv._activeTaskClearedAt = Date.now();
    const s = activeStreams.get(convId);
    if (s && s.controller) { try { s.controller.abort(); } catch (e) { /* already detached */ } }
    activeStreams.delete(convId);
    twStop(convId);
    if (typeof saveConversations === 'function') saveConversations(null);
    if (typeof ConvCache !== 'undefined') { try { ConvCache.put(conv); } catch (e) { /* non-fatal */ } }
    if (activeConvId === convId && typeof renderChat === 'function') renderChat(conv);
    if (typeof renderConversationList === 'function') renderConversationList();
    return true;
  }
  // The placeholder DID accumulate content — route the stale SSE to the poll
  // fallback, exactly like the proxy-swallowed-done probe does.
  //   • terminal status → _pollFallback re-polls and lands the AUTHORITATIVE
  //     final result (more than the SSE delivered).
  //   • 404 (task gone) → _pollFallback's own 404 branch (sse_poll_fallback.js
  //     ~L60) does NOT blank: it PRESERVES the already-accumulated content,
  //     twStop + finishStream and returns. So on a 404 we recover NO ADDITIONAL
  //     content (the task is gone server-side) — we just commit the partial the
  //     SSE already streamed and clear the running state. No spin, no blank.
  const stream = activeStreams.get(convId);
  if (stream && stream.controller) {
    console.warn(
      `[StreamTimer] ★ SELF-HEAL — conv=${convId.slice(0,8)} task=${taskId.slice(0,8)} ` +
      `is ${probe && probe.notFound ? '404' : 'terminal'} with accumulated content — ` +
      `aborting stale SSE to land the result via poll fallback.`
    );
    stream._probeAbort = true;
    stream.controller.abort();
    return true;
  }
  /* ★ Background orphan-pin clear (conv-agnostic sweep — _reconcileStuckActiveTaskPins).
   *   There is NO live stream for this conv in THIS tab, and the caller has
   *   confirmed the task is gone/terminal server-side (reaped by
   *   reap_stuck_running_tasks, TTL-evicted, or a finished task whose done was
   *   swallowed). This is the exact "sidebar dot outlives the work" shape: a
   *   stale ``activeTaskId`` pin with no stream to poll-fallback through. There
   *   is nothing to recover — the partial content (if any) is already on the
   *   message — so just clear the pin so ``convIsBusy`` flips false. Stamp an
   *   honest finishReason on unsettled partial content so the bubble settles.
   *   Gated on ``probe.background`` so the foreground timer path (which always
   *   has a live stream) is byte-identical to before. */
  if (probe && probe.background) {
    console.warn(
      `[StreamTimer] ★ SELF-HEAL (background) — conv=${convId.slice(0,8)} task=${taskId.slice(0,8)} ` +
      `is gone/terminal server-side with no live stream — clearing stale busy pin.`
    );
    if (last && last.role === 'assistant' && !last.finishReason && !last.error
        && (last.content || last.thinking
            || (Array.isArray(last.toolRounds) && last.toolRounds.length))) {
      last.finishReason = 'interrupted';
    }
    conv.activeTaskId = null;
    conv._activeTaskClearedAt = Date.now();
    activeStreams.delete(convId);
    twStop(convId);
    if (typeof saveConversations === 'function') saveConversations(null);
    if (typeof ConvCache !== 'undefined') { try { ConvCache.put(conv); } catch (e) { /* non-fatal */ } }
    if (activeConvId === convId && typeof renderChat === 'function') renderChat(conv);
    if (typeof renderConversationList === 'function') renderConversationList();
    return true;
  }
  return false;
}

async function _updateStreamTimerUI(convId) {
  if (activeConvId !== convId) return;
  const info = _streamTimers.get(convId);
  if (!info) return;
  const el = document.getElementById('stream-elapsed-timer');
  if (!el) return;

  const now = Date.now();
  const elapsedSec = Math.floor((now - info.startTime) / 1000);
  const silentSec = Math.floor((now - info.lastDataTime) / 1000);

  // Show elapsed time always (subtle)
  let elapsedHtml = `<span class="stream-elapsed">${_fmtElapsed(now - info.startTime)}</span>`;

  const buf = streamBufs.get(convId);

  // Short silence — just show elapsed.  NOTE: we no longer suppress detection
  // by phase (tool_exec / llm_thinking / retrying / working).  The server
  // emits a `: keepalive` frame every 15s and ANY byte resets lastDataTime
  // (sse_pipeline.js:_streamTimerTouch), so a silence past _SILENCE_THRESHOLD
  // means even keepalives stopped arriving — that is genuinely anomalous in
  // EVERY phase, not just between turns.  Suppressing it during those phases
  // is exactly how a hung reasoning / dead-proxy stall used to masquerade as
  // a live "Reasoning … chars" spinner.
  if (silentSec < _SILENCE_THRESHOLD) {
    el.innerHTML = elapsedHtml;
    _setBubbleLiveness(convId, '');
    return;
  }

  // Extended silence — run health check + task completion probe (async, non-blocking)
  if (silentSec >= _SILENCE_THRESHOLD && !info._healthChecking) {
    info._healthChecking = true;
    _checkServerHealth().then(async (alive) => {
      info._healthChecking = false;
      info._lastHealthResult = alive;
      if (!alive) {
        console.error(`[StreamTimer] Server health check FAILED for conv=${convId.slice(0,8)} after ${silentSec}s silence`);
        // Auto-finish if server is dead and silence > severe threshold
        if (silentSec >= _SILENCE_SEVERE) {
          _forceFinishDeadStream(convId);
        }
        return;
      }
      /* ★ SERVER IS ALIVE but SSE is silent — the proxy (VS Code port forwarding,
       *   nginx, corporate proxy) may have swallowed the 'done' event.
       *   Proactively poll the task to check if it already finished. If so, abort
       *   the stale SSE connection so connectToTask falls through to _pollFallback,
       *   which will retrieve the completed result. */
      const conv = conversations.find(c => c.id === convId);
      const taskId = conv?.activeTaskId;
      if (!taskId) return;
      try {
        const probeResp = await Api.chat.poll(taskId, { signal: AbortSignal.timeout(5000) });
        if (!probeResp) return;
        if (!probeResp.ok) {
          /* ★ 404 = the task is GONE from the server (discarded carrier,
           *   TTL-evicted, or one that never finalized — e.g. the autopilot
           *   summarize phantom). An open SSE to it will NEVER complete, so the
           *   bubble would stay "等待中…" forever. Self-heal: reclaim the orphan
           *   placeholder (or land any partial via poll fallback). This is the
           *   init-reconcile semantic applied LIVE, without a force-refresh. */
          if (probeResp.status === 404) {
            _healStuckPlaceholder(convId, { notFound: true });
          }
          return;
        }
        const probeData = await probeResp.json();
        if (probeData.status && probeData.status !== 'running') {
          console.warn(
            `[StreamTimer] ★ TASK ALREADY DONE but SSE stuck — conv=${convId.slice(0,8)} ` +
            `task=${taskId.slice(0,8)} status=${probeData.status} ` +
            `content=${(probeData.content||'').length}chars — ` +
            `aborting stale SSE to trigger poll fallback recovery`
          );
          // Unified self-heal: empty orphan placeholder → reclaim; accumulated
          // content → abort stale SSE with _probeAbort so _trySSE falls through
          // to _pollFallback (which lands the authoritative result).
          if (!_healStuckPlaceholder(convId, { status: probeData.status })) {
            // Defensive fallback (no conv / no trailing ghost matched): keep the
            // original direct-abort so a terminal task never streams forever.
            const stream = activeStreams.get(convId);
            if (stream && stream.controller) {
              stream._probeAbort = true;
              stream.controller.abort();
            }
          }
        } else {
          // Task is still running — silence is expected (LLM thinking, tool executing).
          // The SSE pipe might just be slow. Record this so the UI can reassure the
          // user that the server is still actively working instead of showing a
          // scary "no update" warning.
          info._taskStillRunning = true;
          info._taskProbedAt = Date.now();
          console.debug(`[StreamTimer] Task ${taskId.slice(0,8)} still running — silence is expected`);
        }
      } catch (probeErr) {
        // Probe failed — don't take action, next tick will retry
        console.debug(`[StreamTimer] Task probe failed: ${probeErr.message}`);
      }
    });
  }

  // A recent probe (within 2× the health-check interval) confirmed the task is
  // still running on the server — the SSE pipe is just quiet/slow, not stuck.
  const _recentlyConfirmedAlive =
    info._taskStillRunning &&
    (now - (info._taskProbedAt || 0)) < (_HEALTH_CHECK_INTERVAL * 2);
  const _activityLbl = _streamPhaseLabel(buf);

  // Build warning display.  Mirror the verdict into BOTH the small header
  // timer AND the in-bubble status line (_setBubbleLiveness) — the in-bubble
  // line is where the user actually looks, and during a stall it would
  // otherwise stay frozen on a live-looking "Reasoning … chars" spinner.
  const _forceFinishLbl = _connT('conn.forceFinish');
  if (info._lastHealthResult === false) {
    // Server confirmed not responding (≥2 consecutive health-check failures).
    el.innerHTML = elapsedHtml +
      ` <span class="stream-stuck-severe">${_LIVENESS_ICON_DEAD} ${escapeHtml(_connT('conn.hudNotResponding'))}</span>` +
      ` <button class="stream-force-finish-btn" onclick="_forceFinishDeadStream('${convId}')">${escapeHtml(_forceFinishLbl)}</button>`;
    _setBubbleLiveness(convId,
      `<span class="stream-liveness stream-liveness-dead">${_LIVENESS_ICON_DEAD} ${escapeHtml(_connT('conn.hudNotRespondingFull', { n: silentSec }))}</span>`);
  } else if (_recentlyConfirmedAlive) {
    // Server confirmed the task is still running on its side — the SSE pipe is
    // just quiet.  This is the "it's our harness, not a hang" case: name what
    // it's busy with so the silence is explained rather than scary.
    const _suffix = _activityLbl ? ` · ${escapeHtml(_activityLbl)}` : '';
    el.innerHTML = elapsedHtml +
      ` <span class="stream-stuck-activity">${escapeHtml(_connT('conn.hudStillWorking'))}${_suffix}</span>`;
    const _what = _activityLbl ? _activityLbl : _connT('conn.hudProcessing');
    _setBubbleLiveness(convId,
      `<span class="stream-liveness stream-liveness-ok">${_LIVENESS_ICON_OK} ${escapeHtml(_connT('conn.hudStillWorkingFull', { what: _what, n: silentSec }))}</span>`);
  } else if (silentSec >= _SILENCE_SEVERE) {
    // Silent past the severe threshold and we haven't been able to confirm the
    // task is alive — surface it loudly + offer Force Finish.
    el.innerHTML = elapsedHtml +
      ` <span class="stream-stuck-severe">${escapeHtml(_connT('conn.hudNoUpdate', { n: silentSec }))}</span>` +
      ` <button class="stream-force-finish-btn" onclick="_forceFinishDeadStream('${convId}')">${escapeHtml(_forceFinishLbl)}</button>`;
    _setBubbleLiveness(convId,
      `<span class="stream-liveness stream-liveness-warn">${_LIVENESS_ICON_WARN} ${escapeHtml(_connT('conn.hudNoUpdateSevere', { n: silentSec }))}</span>`);
  } else {
    el.innerHTML = elapsedHtml +
      ` <span class="stream-stuck-warn">${escapeHtml(_connT('conn.hudNoUpdate', { n: silentSec }))}</span>`;
    _setBubbleLiveness(convId,
      `<span class="stream-liveness stream-liveness-warn">${_LIVENESS_ICON_WARN} ${escapeHtml(_connT('conn.hudNoUpdateWarn', { n: silentSec }))}</span>`);
  }
}

/**
 * Push a liveness banner into the in-bubble status zone (the same
 * `[data-zone="status"]` element streaming_ui.js renders the phase spinner
 * into).  This is what makes a stall visible WHERE THE USER LOOKS instead of
 * only in the small header timer.  Pass '' to clear it.
 *
 * We append (not replace) so the existing phase spinner ("Reasoning … chars")
 * stays — the banner sits beneath it, turning a frozen-looking spinner into
 * "spinner + an explicit liveness verdict".  Keyed via data-liveness-key so we
 * only touch the DOM when the message actually changes (no per-second churn).
 */
function _setBubbleLiveness(convId, html) {
  if (activeConvId !== convId) return;
  const body = document.getElementById('streaming-body');
  if (!body) return;
  const statusZone = body.querySelector('[data-zone="status"]');
  if (!statusZone) return;
  let banner = statusZone.querySelector('.stream-liveness-wrap');
  if (!html) {
    if (banner) banner.remove();
    statusZone.removeAttribute('data-liveness-key');
    return;
  }
  // Re-apply if the text changed OR the banner was wiped by a statusZone
  // innerHTML rebuild in streaming_ui.js (which leaves the attribute behind).
  if (banner && statusZone.getAttribute('data-liveness-key') === html) return;
  statusZone.setAttribute('data-liveness-key', html);
  if (!banner) {
    banner = document.createElement('div');
    banner.className = 'stream-liveness-wrap';
    statusZone.appendChild(banner);
  }
  banner.innerHTML = html;
}

function _streamTimerTouch(convId) {
  const info = _streamTimers.get(convId);
  if (info) {
    info.lastDataTime = Date.now();
    info._lastHealthResult = undefined; // reset — server is clearly alive if we got data
    info._taskStillRunning = false;     // fresh data supersedes the stale-probe reassurance
    info._taskProbedAt = 0;
    _serverAlive = true;
    _consecutiveHealthFails = 0;
  }
}

function twStart(convId) {
  streamBufs.set(convId, {
    content: "",
    thinking: "",
    toolRounds: [],
    phase: null,
  });
  // Start elapsed timer
  const now = Date.now();
  const existing = _streamTimers.get(convId);
  if (existing && existing.intervalId) clearInterval(existing.intervalId);
  const intervalId = setInterval(() => _updateStreamTimerUI(convId), 1000);
  _streamTimers.set(convId, { startTime: now, lastDataTime: now, intervalId, _lastHealthResult: undefined, _healthChecking: false });
  _serverAlive = true; // optimistic on stream start
}
/* ★ Build the updateStreamingUI payload for a streaming conv, applying the
 *   message-checkpoint fallback that EVERY render site must honour: the live
 *   `streamBufs` buffer is authoritative ONLY once it holds data; before that
 *   (a fresh `twStart`, a re-entry that skipped the seed, a field-only
 *   `twUpdate` — e.g. an HG-translation flag flip — or a tab-switch flush) the
 *   trailing STREAMING message is the source of truth.  Reading `buf.content`
 *   RAW here is precisely what snapped the bubble back to "等待中…" over
 *   already-checkpointed content (see the band-aid seeds in
 *   sse_pipeline.js connectToTask, which this makes correct-by-construction).
 *
 *   Safe against the reset events: `retry_reset` / `delta_reset` clear the
 *   MESSAGE and the buffer together (sse_pipeline.js), so the fallback reads
 *   "" in that case and never resurrects erased inter-round narration; a
 *   worker/planner turn rotation pushes a FRESH empty assistant message, so
 *   the checkpoint is empty there too.  Mirrors showStreamingUIForConv exactly.
 *   Returns null when there is no buffer for `convId`. */
function _streamFrameArg(convId) {
  const buf = streamBufs.get(convId);
  if (!buf) return null;
  let ckpt = null;
  const conv = (typeof conversations !== 'undefined')
    ? conversations.find(c => c && c.id === convId) : null;
  if (conv && conv.messages.length) {
    const last = conv.messages[conv.messages.length - 1];
    if (last && (last.role === 'assistant' || last._isEndpointReview)) ckpt = last;
  }
  /* buf.toolRounds is [] (truthy) even when empty → guard on .length so an
   * un-seeded buffer falls back to the checkpoint's rounds instead of blanking
   * the tool panel the message still holds. */
  const rounds = (buf.toolRounds && buf.toolRounds.length)
    ? buf.toolRounds
    : (ckpt && typeof getToolRoundsFromMsg === 'function'
        ? getToolRoundsFromMsg(ckpt) : (buf.toolRounds || []));
  return {
    thinking: buf.thinking || (ckpt && ckpt.thinking) || "",
    content: buf.content || (ckpt && ckpt.content) || "",
    toolRounds: rounds,
    phase: buf.phase,
    _memoryPrefetch: buf._memoryPrefetch || (ckpt && ckpt._memoryPrefetch),
    _mcpLoginHint: buf._mcpLoginHint,
  };
}

/* ── Coalesced streaming update: multiple SSE events between frames are merged ── */
let _twRafId = null;
let _twPendingConvId = null;
let _twTimeoutId = null; // fallback timer when page is hidden (rAF paused)
let _twDirty = false;    // data changed since last render
let _twLastFlush = 0;    // timestamp of last actual render (perf clock)
const _TW_MIN_INTERVAL = 33; // ~30fps cap — re-rendering markdown 120x/s on a
                             // high-Hz display is pure waste; 30fps is visually
                             // identical for streaming text and ~4x less work.

function _twFlush() {
  _twRafId = null;
  /* ★ Perf: rate-cap. rAF fires at the display refresh (up to 120Hz); a full
   * tail markdown re-render per frame is the bulk of the streaming GC/paint
   * load. If the last render was <33ms ago, reschedule instead of rendering. */
  const _now = (typeof performance !== 'undefined' ? performance.now() : Date.now());
  if (_twDirty && (_now - _twLastFlush) < _TW_MIN_INTERVAL) {
    _twRafId = requestAnimationFrame(_twFlush);
    return;
  }
  _twLastFlush = _now;
  _twDirty = false;
  if (_twTimeoutId) { clearTimeout(_twTimeoutId); _twTimeoutId = null; }
  const cid = _twPendingConvId;
  /* ★ CROSS-TALK DETECTION: log when we render streaming data for a conv
   *   that is NOT the currently viewed conversation */
  if (activeConvId && cid && activeConvId !== cid) {
    console.debug(
      `[twUpdate] bg conv=${cid.slice(0,8)} triggered rAF while viewing ${activeConvId.slice(0,8)}`
    );
  }
  /* ★ FIX: Always render the active conversation if it's streaming, regardless
   *   of which convId triggered this rAF.  When multiple conversations stream
   *   concurrently, a background conv's twUpdate overwrites _twPendingConvId
   *   before the rAF fires, causing the active conv's rendering to be silently
   *   skipped for that frame.  This manifests as the UI appearing "stuck" even
   *   though data is accumulating in the buffers — the user has to switch convs
   *   to trigger showStreamingUIForConv which reads from the buffer directly.
   *
   *   Fix: prefer activeConvId as the render target (if it has a streamBuf),
   *   falling back to cid only during init (activeConvId not yet set). */
  const renderCid = (activeConvId && streamBufs.has(activeConvId)) ? activeConvId : cid;
  if (renderCid === activeConvId || (!activeConvId && document.getElementById('streaming-body'))) {
    /* ★ Message-checkpoint fallback (see _streamFrameArg): an empty buffer
     *   must render the persisted message, NOT blank the bubble to "等待中…". */
    const arg = _streamFrameArg(renderCid);
    if (arg) updateStreamingUI(arg);
  } else {
    /* ★ DIAGNOSTIC (autopilot-invisible bug): silent-drop signature.
     * Buffer has data but the render guard rejected it — same family
     * as the prior `force-refresh-streaming-stuck-waiting-bug`. Logged
     * once per drop so a repro session yields one warn per missed
     * frame.  If this fires during autopilot streaming, the renderCid
     * gating logic needs an autopilot fallback. */
    const _hasBuf = streamBufs.has(cid) || (activeConvId && streamBufs.has(activeConvId));
    if (_hasBuf) {
      console.warn(
        `[twFlush-skip] renderCid=${(renderCid||'').slice(0,8)} ` +
        `activeConvId=${(activeConvId||'null').slice(0,8)} ` +
        `cid=${(cid||'null').slice(0,8)} ` +
        `streamBufs.has(active)=${activeConvId && streamBufs.has(activeConvId)} ` +
        `streaming-body=${!!document.getElementById('streaming-body')} — render dropped`
      );
    }
  }
}

function twUpdate(convId) {
  _streamTimerTouch(convId); // mark data received
  _twPendingConvId = convId;
  _twDirty = true;

  /* ★ PERF FIX: When the page/tab is hidden, browsers pause requestAnimationFrame
   *   callbacks entirely.  SSE data keeps arriving and accumulating in the buffer,
   *   but no render happens.  When the user switches back, a SINGLE rAF fires and
   *   renders ALL buffered content at once — causing the "bunch of content popping
   *   up all at once" symptom.
   *
   *   Fix: schedule BOTH a rAF (for smooth 60fps when visible) AND a setTimeout
   *   fallback (fires even when hidden, at ~1s throttle in background tabs).
   *   Whichever fires first cancels the other via _twDirty flag. */
  if (!_twRafId) {
    _twRafId = requestAnimationFrame(_twFlush);
  }
  /* Background-tab fallback: setTimeout still fires (≥1s in hidden tabs).
   * Only schedule if not already pending.  The 250ms delay means we batch
   * ~250ms of SSE events per render in background tabs — much smoother than
   * waiting for the tab to become visible again. */
  if (!_twTimeoutId) {
    _twTimeoutId = setTimeout(() => {
      _twTimeoutId = null;
      if (_twDirty) {
        if (_twRafId) { cancelAnimationFrame(_twRafId); _twRafId = null; }
        _twFlush();
      }
    }, 250);
  }
}
function twStop(convId) {
  streamBufs.delete(convId);
  if (typeof _pendingStreamTimer !== "undefined" && _pendingStreamTimer) {
    clearInterval(_pendingStreamTimer);
    _pendingStreamTimer = null;
  }
  _pendingStreamMsg = null;
  // Clear any in-bubble liveness banner so it doesn't linger after the turn ends.
  _setBubbleLiveness(convId, '');
  // Cancel any pending twUpdate timers
  if (_twTimeoutId) { clearTimeout(_twTimeoutId); _twTimeoutId = null; }
  if (_twRafId) { cancelAnimationFrame(_twRafId); _twRafId = null; }
  _twDirty = false;
  // Invalidate zone cache and incremental render state
  if (typeof _streamZoneCache !== "undefined") _streamZoneCache = { body: null, tool: null, think: null, content: null, fc: null, status: null, swarmInbox: null };
  // Stop elapsed timer
  const timerInfo = _streamTimers.get(convId);
  if (timerInfo) {
    if (timerInfo.intervalId) clearInterval(timerInfo.intervalId);
    _streamTimers.delete(convId);
  }
}


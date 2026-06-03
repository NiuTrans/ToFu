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
  } catch {
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
    }
  } catch {
    // Server itself is unreachable — _checkServerHealth handles that
  }
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
  banner.innerHTML =
    '<span style="font-size:18px">⚠️</span>' +
    '<span><b>Database Unavailable</b> — PostgreSQL is not running. ' +
    'Conversations and history will not work. ' +
    'Install PostgreSQL (<code style="background:rgba(255,255,255,.2);padding:1px 5px;border-radius:3px">' +
    'conda install -c conda-forge postgresql>=18</code>) and restart the server.</span>' +
    '<button onclick="this.parentElement.remove()" style="' +
    'background:rgba(255,255,255,.2);border:none;color:#fff;padding:4px 10px;' +
    'border-radius:4px;cursor:pointer;font-size:13px;margin-left:12px;' +
    'white-space:nowrap">Dismiss</button>';
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
      /* Use the typed envelope so the error block renders consistently with
       * backend-emitted errors. The kind=server_offline carries hint + label. */
      last.error = normalizeErrorEnvelope({
        kind: 'server_offline', severity: 'warning', retryable: true,
        message: '⚠️ 服务器离线，回复可能不完整。服务器恢复后会自动重连。\nServer offline — response may be incomplete. This notice will clear automatically when the server comes back.',
        hint: '', detail: 'Frontend health-check failed', model: '', context: '', source: 'frontend-health-check', raw: '',
      });
    }
  }
  // Abort the SSE controller so _trySSE / _pollFallback also exit
  const s = activeStreams.get(convId);
  if (s && s.controller) {
    try { s.controller.abort(); } catch {}
  }
  twStop(convId);
  finishStream(convId);
  showToast('⚠️', 'Server Offline',
    'Backend server is not responding. Your partial response has been saved. It will recover automatically when connectivity is restored.',
    10000);
  // ★ Start periodic recovery polling so the result is auto-recovered when server comes back
  _startOfflineRecoveryPolling();
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

  // During tool execution, LLM thinking, or retrying, silence is expected — only show elapsed
  const buf = streamBufs.get(convId);
  if (buf && buf.phase && (buf.phase.phase === 'tool_exec' || buf.phase.phase === 'llm_thinking' || buf.phase.phase === 'retrying' || buf.phase.phase === 'working')) {
    el.innerHTML = elapsedHtml;
    return;
  }

  // Short silence — just show elapsed
  if (silentSec < _SILENCE_THRESHOLD) {
    el.innerHTML = elapsedHtml;
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
        if (!probeResp || !probeResp.ok) return;
        const probeData = await probeResp.json();
        if (probeData.status && probeData.status !== 'running') {
          console.warn(
            `[StreamTimer] ★ TASK ALREADY DONE but SSE stuck — conv=${convId.slice(0,8)} ` +
            `task=${taskId.slice(0,8)} status=${probeData.status} ` +
            `content=${(probeData.content||'').length}chars — ` +
            `aborting stale SSE to trigger poll fallback recovery`
          );
          // Abort the SSE controller — this causes _trySSE to exit with AbortError.
          // We set _probeAbort flag so _trySSE knows this is a timer probe (not user stop)
          // and falls through to _pollFallback instead of treating it as user abort.
          const stream = activeStreams.get(convId);
          if (stream && stream.controller) {
            stream._probeAbort = true;
            stream.controller.abort();
          }
        } else {
          // Task is still running — silence is expected (LLM thinking, tool executing)
          // The SSE pipe might just be slow. Touch the timer to reduce noise.
          console.debug(`[StreamTimer] Task ${taskId.slice(0,8)} still running — silence is expected`);
        }
      } catch (probeErr) {
        // Probe failed — don't take action, next tick will retry
        console.debug(`[StreamTimer] Task probe failed: ${probeErr.message}`);
      }
    });
  }

  // Build warning display
  if (info._lastHealthResult === false) {
    // Server confirmed dead
    el.innerHTML = elapsedHtml +
      ` <span class="stream-stuck-severe">⚠️ server offline</span>` +
      ` <button class="stream-force-finish-btn" onclick="_forceFinishDeadStream('${convId}')">Force Finish</button>`;
  } else if (silentSec >= _SILENCE_SEVERE) {
    el.innerHTML = elapsedHtml +
      ` <span class="stream-stuck-severe">${silentSec}s no update</span>` +
      ` <button class="stream-force-finish-btn" onclick="_forceFinishDeadStream('${convId}')">Force Finish</button>`;
  } else {
    el.innerHTML = elapsedHtml +
      ` <span class="stream-stuck-warn">${silentSec}s no update</span>`;
  }
}

function _streamTimerTouch(convId) {
  const info = _streamTimers.get(convId);
  if (info) {
    info.lastDataTime = Date.now();
    info._lastHealthResult = undefined; // reset — server is clearly alive if we got data
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
    const buf = streamBufs.get(renderCid);
    if (buf)
      updateStreamingUI({
        thinking: buf.thinking,
        content: buf.content,
        toolRounds: buf.toolRounds,
        phase: buf.phase,
        _memoryPrefetch: buf._memoryPrefetch,
        _mcpLoginHint: buf._mcpLoginHint,
      });
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
  // Cancel any pending twUpdate timers
  if (_twTimeoutId) { clearTimeout(_twTimeoutId); _twTimeoutId = null; }
  if (_twRafId) { cancelAnimationFrame(_twRafId); _twRafId = null; }
  _twDirty = false;
  // Invalidate zone cache and incremental render state
  if (typeof _streamZoneCache !== "undefined") _streamZoneCache = { body: null, tool: null, think: null, content: null, status: null };
  // Stop elapsed timer
  const timerInfo = _streamTimers.get(convId);
  if (timerInfo) {
    if (timerInfo.intervalId) clearInterval(timerInfo.intervalId);
    _streamTimers.delete(convId);
  }
}


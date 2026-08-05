/* ═══════════════════════════════════════════════════════════════════
   core/backend_offline_monitor.js — Global backend-liveness watch + prominent
   offline banner.

   WHY: when the backend process is killed (the nightly OOM SIGKILL pattern),
   every chat SSE hangs and the ONLY visible signal used to be the tiny topbar
   signal badge (net-latency.js) going gray. The page "looked alive", so the
   user kept waiting on dead conversations instead of intervening. The
   per-stream health checks (health_stream_timer.js) only run while a stream
   is ACTIVE — a fully idle page had no watchdog at all.

   DESIGN — two passive signals + one active arbiter:
     ① push.js socket state (pushOnLatency): connected===false means the
       multiplexed WebSocket dropped. Fires even when the page is idle (zero
       active streams) — the gap the per-stream checks cannot cover.
     ② Browser online/offline events (navigator.onLine).
     ③ ARBITER: an active /api/health probe (Api.health.check). Under a
       buffering proxy (VS Code port-forward) the WS can drop while the backend
       is perfectly fine, so the banner requires TWO consecutive probe failures
       (~4s apart) before it shows — the same 2-fail rule _checkServerHealth
       uses. A probe success at any point cancels the alarm quietly.

   OFFLINE state:
     - Fixed-top red banner (id=backend-offline-banner): title + live elapsed
       counter + auto-retry note + "retry now" + "hide 1 min" snooze.
     - document.title prefixed (【后端离线】) so a BACKGROUNDED tab shows the
       outage in the tab strip. Nothing else in the app writes document.title.
     - Polls /api/health every _BOM_RECOVERY_POLL_MS while the tab is visible.

   RECOVERY (first successful probe):
     - Banner removed, title restored, toast.
     - Nudges the push socket (pushConnect) and fires the SAME recovery
       machinery the visibilitychange/online hooks already use:
       _probeAllStuckStreamsOnWake + _recoverOfflineConversations +
       _revalidateOnResume — restored conversations re-attach and adopt the
       server's authoritative results.

   Pure window-scope module, concatenated by lib/js_bundler.py AFTER push.js.
   Every app symbol (Api / pushOnLatency / showToast / recovery fns) is
   referenced only inside function bodies and typeof-guarded, so the jsdom /
   node harnesses can eval this file standalone.
   ═══════════════════════════════════════════════════════════════════ */

const _BOM_CONFIRM_FAILS = 2;        // consecutive probe failures before the banner shows
const _BOM_CONFIRM_GAP_MS = 4000;    // delay between the two confirmation probes
const _BOM_RECOVERY_POLL_MS = 5000;  // health re-probe cadence while offline
const _BOM_SNOOZE_MS = 60000;        // "hide 1 min" duration
const _BOM_PROBE_TIMEOUT_MS = 4000;  // per-probe fetch timeout

const _bomState = {
  phase: 'online',        // online | suspect | offline
  fails: 0,               // consecutive failed probes in the current episode
  probing: false,         // serializes overlapping probes
  offlineSince: 0,
  snoozedUntil: 0,
  banner: null,
  elapsedEl: null,
  origTitle: null,
  probeTimer: null,       // one-shot confirmation-probe timeout
  pollTimer: null,        // recovery poll interval
  elapsedTimer: null,     // 1s elapsed-counter ticker
  booted: false,
};

/* Guarded t(): the node/jsdom harnesses eval THIS file standalone (without
 * i18n.js). zh is the primary UI language — fall back to zh literals. */
function _bomT(key, params) {
  if (typeof t === 'function') return t(key, params);
  const zh = {
    'conn.backendOfflineTitle': '后端服务器已离线',
    'conn.backendOfflineDesc': '所有进行中的回复已暂停。每 ' + (params && params.n) + ' 秒自动重试，恢复后会自动重连并同步结果。',
    'conn.networkOfflineTitle': '本机网络已断开',
    'conn.networkOfflineDesc': '浏览器报告网络已断开。检查网络连接；恢复后页面会自动重连。',
    'conn.backendOfflineElapsed': '已离线 ' + (params && params.t),
    'conn.backendRetryNow': '立即重试',
    'conn.backendSnooze': '暂时隐藏',
    'conn.backendRestored': '后端已恢复',
    'conn.backendRestoredDesc': '正在重新连接并同步进行中的对话…',
    'conn.backendOfflineTitlePrefix': '【后端离线】',
    'conn.networkOfflineTitlePrefix': '【网络断开】',
  };
  return zh[key] || key;
}

function _bomFmtDur(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return m + 'm' + (rs > 0 ? String(rs).padStart(2, '0') + 's' : '');
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return h + 'h' + (rm > 0 ? String(rm).padStart(2, '0') + 'm' : '');
}

/* ── Probe (the arbiter) ─────────────────────────────────────────── */

async function _bomProbe(reason) {
  if (_bomState.probing) return;
  const api = (typeof Api !== 'undefined') ? Api : null;
  if (!api || !api.health || typeof api.health.check !== 'function') return;
  _bomState.probing = true;
  let alive = false;
  try {
    const resp = await api.health.check({ signal: AbortSignal.timeout(_BOM_PROBE_TIMEOUT_MS) });
    alive = !!(resp && resp.ok);
  } catch (e) {
    // A fetch throw / AbortSignal timeout and a genuine outage land here
    // together — log the reason so the two stay distinguishable (CLAUDE §2).
    console.debug('[BackendMonitor] health probe failed (%s): %s', reason, e && e.message);
    alive = false;
  } finally {
    _bomState.probing = false;
  }
  if (alive) _bomAlive(reason);
  else _bomDead(reason);
}

function _bomDead(reason) {
  _bomState.fails++;
  if (_bomState.phase === 'suspect') {
    /* ★ The 2-fail confirmation gate (load-bearing): the FIRST failure only
     *   arms a second probe. Under a buffering proxy the WS drop + one failed
     *   fetch is a common hiccup — alarming on it would flap the red banner
     *   on every tunnel stutter. Only _BOM_CONFIRM_FAILS consecutive failures
     *   promote suspect → offline. */
    if (_bomState.fails >= _BOM_CONFIRM_FAILS) { _bomGoOffline(reason); return; }
    console.warn('[BackendMonitor] probe %d/%d failed (%s) — confirming before alarm',
      _bomState.fails, _BOM_CONFIRM_FAILS, reason);
    _bomArmProbeTimer(_BOM_CONFIRM_GAP_MS, 'confirm');
    return;
  }
  if (_bomState.phase === 'offline') return; // the poll timer owns re-probing
  _bomSuspect('probe_fail');                 // unsolicited failure → re-enter suspect
}

function _bomAlive(reason) {
  _bomState.fails = 0;
  if (_bomState.phase === 'offline') { _bomRecovered(reason); return; }
  if (_bomState.phase === 'suspect') {
    _bomState.phase = 'online';
    _bomClearProbeTimer();
    console.info('[BackendMonitor] probe OK (%s) — connection hiccup over, no alarm raised', reason);
  }
}

/* ── State transitions ───────────────────────────────────────────── */

function _bomSuspect(trigger) {
  if (_bomState.phase === 'offline') return; // already alarming; the poll owns probing
  if (_bomState.phase === 'suspect') {
    // push emits on every failed reconnect attempt — dedupe to one probe.
    if (!_bomState.probing && !_bomState.probeTimer) _bomProbe('re_' + trigger);
    return;
  }
  _bomState.phase = 'suspect';
  _bomState.fails = 0;
  console.warn('[BackendMonitor] connection suspect (%s) — probing /api/health before alarming', trigger);
  _bomProbe(trigger);
}

function _bomGoOffline(cause) {
  _bomState.phase = 'offline';
  _bomState.offlineSince = Date.now();
  _bomState.snoozedUntil = 0;
  console.error('[BackendMonitor] ★ BACKEND OFFLINE confirmed (%s) after %d failed probes — raising the banner',
    cause, _bomState.fails);
  _bomShowBanner();
  _bomPrefixTitle();
  _bomStartElapsedTicker();
  _bomArmPollTimer();
}

function _bomRecovered(how) {
  const downMs = Date.now() - (_bomState.offlineSince || Date.now());
  _bomState.phase = 'online';
  _bomState.fails = 0;
  _bomClearProbeTimer();
  if (_bomState.pollTimer) { clearInterval(_bomState.pollTimer); _bomState.pollTimer = null; }
  _bomStopElapsedTicker();
  _bomHideBanner();
  _bomRestoreTitle();
  console.info('[BackendMonitor] ★ BACKEND BACK (%s) after %s — resyncing', how, _bomFmtDur(downMs));
  if (typeof showToast === 'function') {
    try {
      showToast('✅', _bomT('conn.backendRestored'), _bomT('conn.backendRestoredDesc'), 6000);
    } catch (e) { console.debug('[BackendMonitor] recovery toast failed:', e && e.message); }
  }
  _bomFireRecovery(how);
}

/* Fire the SAME recovery machinery the visibilitychange/online hooks use, so
 * a backend restart lands identically to a network restore: push socket
 * nudged, stuck streams re-probed, server_offline convs re-adopted, conv list
 * revalidated. All typeof-guarded — the harness defines none of them. */
function _bomFireRecovery(how) {
  if (typeof pushConnect === 'function') {
    try { pushConnect(); } catch (e) { console.debug('[BackendMonitor] pushConnect nudge failed:', e && e.message); }
  }
  if (typeof _probeAllStuckStreamsOnWake === 'function') {
    try { _probeAllStuckStreamsOnWake(how); } catch (e) { console.error('[BackendMonitor] stuck-stream probe failed:', e); }
  }
  if (typeof _recoverOfflineConversations === 'function') {
    try { _recoverOfflineConversations(how); } catch (e) { console.error('[BackendMonitor] offline-conv recovery failed:', e); }
  }
  if (typeof _revalidateOnResume === 'function') {
    try { _revalidateOnResume(how); } catch (e) { console.error('[BackendMonitor] list revalidation failed:', e); }
  }
}

/* ── Timers ──────────────────────────────────────────────────────── */

function _bomArmProbeTimer(ms, why) {
  _bomClearProbeTimer();
  _bomState.probeTimer = setTimeout(() => {
    _bomState.probeTimer = null;
    _bomProbe(why);
  }, ms);
}
function _bomClearProbeTimer() {
  if (_bomState.probeTimer) { clearTimeout(_bomState.probeTimer); _bomState.probeTimer = null; }
}

function _bomArmPollTimer() {
  if (_bomState.pollTimer) return;
  _bomState.pollTimer = setInterval(() => {
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    _bomProbe('poll');
  }, _BOM_RECOVERY_POLL_MS);
}

function _bomStartElapsedTicker() {
  if (_bomState.elapsedTimer) return;
  _bomPaintElapsed();
  _bomState.elapsedTimer = setInterval(() => {
    if (_bomState.phase !== 'offline') return;
    // Snooze expiry re-shows the banner while still offline.
    if (!_bomState.banner && _bomState.snoozedUntil && Date.now() >= _bomState.snoozedUntil) {
      _bomState.snoozedUntil = 0;
      _bomShowBanner();
    }
    _bomPaintElapsed();
  }, 1000);
}
function _bomStopElapsedTicker() {
  if (_bomState.elapsedTimer) { clearInterval(_bomState.elapsedTimer); _bomState.elapsedTimer = null; }
}
function _bomPaintElapsed() {
  if (_bomState.elapsedEl) {
    _bomState.elapsedEl.textContent =
      _bomT('conn.backendOfflineElapsed', { t: _bomFmtDur(Date.now() - _bomState.offlineSince) });
  }
}

/* ── Banner + title ──────────────────────────────────────────────── */

const _BOM_ICON =
  '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2v4"/><path d="M12 18v4"/><rect x="4" y="8" width="16" height="10" rx="2"/><path d="M9 13h.01"/><path d="M15 13h.01"/></svg>';

function _bomNetworkDown() {
  return (typeof navigator !== 'undefined') && navigator && navigator.onLine === false;
}

function _bomBannerHtml() {
  const netDown = _bomNetworkDown();
  const title = netDown ? _bomT('conn.networkOfflineTitle') : _bomT('conn.backendOfflineTitle');
  const desc = netDown
    ? _bomT('conn.networkOfflineDesc')
    : _bomT('conn.backendOfflineDesc', { n: Math.round(_BOM_RECOVERY_POLL_MS / 1000) });
  const btnStyle =
    'background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.35);color:#fff;' +
    'padding:4px 12px;border-radius:4px;cursor:pointer;font-size:13px;white-space:nowrap;';
  return '<span style="display:inline-flex;align-items:center">' + _BOM_ICON + '</span>' +
    '<span><b>' + title + '</b> ' +
    '<span class="bom-elapsed" style="opacity:.9"></span>' +
    '<span class="bom-desc" style="opacity:.92"> — ' + desc + '</span></span>' +
    '<button onclick="BackendOfflineMonitorProbeNow()" style="' + btnStyle + '">' +
    _bomT('conn.backendRetryNow') + '</button>' +
    '<button onclick="BackendOfflineMonitorSnooze()" style="' + btnStyle + '">' +
    _bomT('conn.backendSnooze') + '</button>';
}

function _bomShowBanner() {
  if (_bomState.banner) return;
  const el = document.createElement('div');
  el.id = 'backend-offline-banner';
  el.style.cssText =
    'position:fixed;top:0;left:0;right:0;z-index:10001;' +
    'background:linear-gradient(90deg,#991b1b,#b91c1c);color:#fff;padding:10px 16px;' +
    'font-size:14px;text-align:center;box-shadow:0 2px 10px rgba(0,0,0,.4);' +
    'display:flex;align-items:center;justify-content:center;gap:10px;flex-wrap:wrap;';
  el.innerHTML = _bomBannerHtml();
  document.body.prepend(el);
  _bomState.banner = el;
  _bomState.elapsedEl = el.querySelector('.bom-elapsed');
  _bomPaintElapsed();
}

function _bomHideBanner() {
  if (_bomState.banner) {
    try { _bomState.banner.remove(); } catch (e) { console.debug('[BackendMonitor] banner remove failed:', e && e.message); }
    _bomState.banner = null;
    _bomState.elapsedEl = null;
  }
}

function _bomPrefixTitle() {
  try {
    if (_bomState.origTitle == null) _bomState.origTitle = document.title || '';
    const prefix = _bomNetworkDown()
      ? _bomT('conn.networkOfflineTitlePrefix')
      : _bomT('conn.backendOfflineTitlePrefix');
    document.title = prefix + ' ' + _bomState.origTitle;
  } catch (e) { console.debug('[BackendMonitor] title prefix failed:', e && e.message); }
}

function _bomRestoreTitle() {
  try {
    if (_bomState.origTitle != null) {
      document.title = _bomState.origTitle;
      _bomState.origTitle = null;
    }
  } catch (e) { console.debug('[BackendMonitor] title restore failed:', e && e.message); }
}

/* ── Server-liveness probe + DB-health banner (boot/recovery primitives) ──
 * Relocated from core/health_stream_timer.js (2026-08-01): that module was
 * deferred to the feature bundle by Epic-E sub-3B, but these are BOOT-PATH
 * and RECOVERY-PATH primitives — main.js's boot IIFE calls _checkDbHealth
 * and sse_poll_fallback's circuit breaker calls _checkServerHealth — so they
 * must live in the CORE bundle (an unguarded boot call to the deferred copy
 * ReferenceError'd the whole boot IIFE: no loadFolders, no conversations —
 * the "sidebar folder rail gone" incident). The deferred stream-timer keeps
 * referencing this state cross-bundle (core loads first): _streamTimerTouch
 * and twStart reset the cache optimistically on fresh bytes. */

// _serverAlive: cached health state shared across all streams (avoid duplicate pings)
let _serverAlive = true;
let _lastHealthCheck = 0;
let _consecutiveHealthFails = 0;       // require 2+ consecutive fails to confirm dead
const _HEALTH_CHECK_INTERVAL = 10000;  // ms between health checks when silent

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

/* ── Public entry points (button onclick + boot + harness) ───────── */

function BackendOfflineMonitorProbeNow() {
  console.info('[BackendMonitor] manual retry requested');
  _bomProbe('manual');
}

function BackendOfflineMonitorSnooze() {
  _bomState.snoozedUntil = Date.now() + _BOM_SNOOZE_MS;
  _bomHideBanner(); // state/timers keep running; the elapsed ticker re-shows on expiry
  console.info('[BackendMonitor] banner snoozed for %ds (still polling)', Math.round(_BOM_SNOOZE_MS / 1000));
}

function initBackendOfflineMonitor() {
  if (_bomState.booted) return;
  _bomState.booted = true;
  // ① push socket state — the only always-on signal (fires with zero streams).
  if (typeof pushOnLatency === 'function') {
    pushOnLatency((reading) => {
      if (!reading) return;
      if (reading.connected === false) _bomSuspect('push_drop');
      // A re-opened socket while suspect/offline might mean the backend is
      // back — but the health probe remains the arbiter (proxy flaps can
      // reopen the WS while HTTP is still broken).
      else if (_bomState.phase !== 'online') _bomProbe('push_reconnected');
    });
  }
  if (typeof pushOnReconnect === 'function') {
    pushOnReconnect(() => { if (_bomState.phase !== 'online') _bomProbe('push_reopen'); });
  }
  // ② Browser network events.
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('offline', () => _bomSuspect('browser_offline'));
    window.addEventListener('online', () => { if (_bomState.phase !== 'online') _bomProbe('browser_online'); });
  }
  // A foregrounded tab re-probes immediately (the poll skips hidden tabs).
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && _bomState.phase !== 'online') _bomProbe('visible');
    });
  }
}

if (typeof window !== 'undefined') {
  window.BackendOfflineMonitorProbeNow = BackendOfflineMonitorProbeNow;
  window.BackendOfflineMonitorSnooze = BackendOfflineMonitorSnooze;
  window.initBackendOfflineMonitor = initBackendOfflineMonitor;
  window.BackendOfflineMonitor = _bomState; // harness introspection handle
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initBackendOfflineMonitor);
  } else {
    initBackendOfflineMonitor();
  }
}

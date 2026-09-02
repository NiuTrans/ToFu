/* ===== migrated source: core/backend_offline_monitor.js ===== */
/* Global backend-liveness owner and offline-banner presentation.
 * Entry points: init/destroy plus manual retry and snooze handlers.
 * Dependencies: push.js signals, Api.health, lifecycle scope, recovery hooks.
 * A socket drop is only suspicion: two failed /api/health probes are required
 * before alarming. One successful probe clears suspicion or restores state. */

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
let _bomLifecycleScope = null;

function _bomEnsureLifecycleScope() {
  if (_bomLifecycleScope) return _bomLifecycleScope;
  _bomLifecycleScope = createLifecycleScope();
  return _bomLifecycleScope;
}

function _bomOwnCleanup(cleanup) {
  _bomEnsureLifecycleScope().add(cleanup);
}

function _bomListen(target, type, listener) {
  _bomEnsureLifecycleScope().listen(target, type, listener);
}

function _bomSetTimeout(callback, delay) {
  return _bomEnsureLifecycleScope().timeout(callback, delay);
}

function _bomSetInterval(callback, delay) {
  return _bomEnsureLifecycleScope().interval(callback, delay);
}

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
  let verdictReason = reason;
  try {
    const resp = await api.health.check({ signal: AbortSignal.timeout(_BOM_PROBE_TIMEOUT_MS) });
    const status = Number(resp && resp.status) || 0;
    if (resp && (status === 401 || status === 403)) {
      /* /api/health is deliberately public inside Tofu. An auth denial here
       * therefore came from the outer VS Code / MLP proxy, not from the app's
       * liveness endpoint. Reporting "backend offline" would be a false and
       * unactionable diagnosis; the surrounding proxy/login UI owns auth
       * recovery. A real backend outage still arrives as a fetch failure or a
       * 5xx response and continues through the confirmation gate below. */
      alive = true;
      verdictReason = 'proxy_auth_' + status;
      console.warn('[BackendMonitor] health probe reached the proxy but was denied (HTTP %d) — not classifying the backend as offline', status);
    } else {
      alive = !!(resp && resp.ok);
    }
  } catch (e) {
    // A fetch throw / AbortSignal timeout and a genuine outage land here
    // together — log the reason so the two stay distinguishable (CLAUDE §2).
    console.debug('[BackendMonitor] health probe failed (%s): %s', reason, e && e.message);
    alive = false;
  } finally {
    _bomState.probing = false;
  }
  if (alive) _bomAlive(verdictReason);
  else _bomDead(reason);
}

function _bomDead(reason) {
  _bomState.fails++;
  if (_bomState.phase === 'suspect') {
    /* The 2-fail confirmation gate (load-bearing): the FIRST failure only
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
  console.error('[BackendMonitor] BACKEND OFFLINE confirmed (%s) after %d failed probes — raising the banner',
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
  console.info('[BackendMonitor] BACKEND BACK (%s) after %s — resyncing', how, _bomFmtDur(downMs));
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
  _bomState.probeTimer = _bomSetTimeout(() => {
    _bomState.probeTimer = null;
    _bomProbe(why);
  }, ms);
}
function _bomClearProbeTimer() {
  if (_bomState.probeTimer) { clearTimeout(_bomState.probeTimer); _bomState.probeTimer = null; }
}

function _bomArmPollTimer() {
  if (_bomState.pollTimer) return;
  _bomState.pollTimer = _bomSetInterval(() => {
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    _bomProbe('poll');
  }, _BOM_RECOVERY_POLL_MS);
}

function _bomStartElapsedTicker() {
  if (_bomState.elapsedTimer) return;
  _bomPaintElapsed();
  _bomState.elapsedTimer = _bomSetInterval(() => {
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
    '<button data-tofu-action="BackendOfflineMonitorProbeNow()" style="' + btnStyle + '">' +
    _bomT('conn.backendRetryNow') + '</button>' +
    '<button data-tofu-action="BackendOfflineMonitorSnooze()" style="' + btnStyle + '">' +
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

/* ── Server-liveness + storage-health probes (boot/recovery primitives) ──
 * Relocated from core/health_stream_timer.js (2026-08-01): that module was
 * deferred to the lazy ESM domain by Epic-E sub-3B, but these are BOOT-PATH
 * and RECOVERY-PATH primitives — main.js calls _checkStorageHealth and stream
 * recovery calls _checkServerHealth — so they
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
 * Check the required Sidecar storage authority on startup. A persistent
 * warning explains why durable features are temporarily fenced.
 */
async function _checkStorageHealth() {
  try {
    const resp = await Api.health.check({ signal: AbortSignal.timeout(3000) });
    if (!resp || !resp.ok) return;
    const data = await resp.json();
    if (!data.storage || data.storage.ready !== true) {
      _showStorageWarningBanner();
      _startStorageHealthPolling();
    } else {
      // Storage is healthy — clear a stale warning from a prior restart.
      _clearStorageWarningBanner();
    }
  } catch (e) {
    // A network/tunnel drop means the server is unreachable — _checkServerHealth
    // owns that verdict, so we don't show a storage banner here. But a .json()
    // parse failure or a 200-with-garbage payload ALSO lands here; log it so a
    // malformed health response isn't silently invisible (CLAUDE §2).
    console.debug('[StorageHealth] health probe failed (server unreachable or bad payload):', e && e.message);
  }
}

/** Remove the storage warning after the supervisor reports recovery. */
function _clearStorageWarningBanner() {
  const b = document.getElementById('storage-warning-banner');
  if (b) {
    b.remove();
    console.info('[StorageHealth] Sidecar available again — cleared warning banner');
  }
}

/* Self-stopping recovery poll: once the storage warning is shown, re-probe
 * /api/health every 15s and clear the banner on recovery. Stops
 * itself when the banner is gone (recovered OR user-dismissed) so it costs
 * nothing in steady state. Mirrors _startOfflineRecoveryPolling's shape. */
let _storageHealthPollInterval = null;
function _startStorageHealthPolling() {
  if (_storageHealthPollInterval) return;
  _storageHealthPollInterval = _bomSetInterval(async () => {
    if (!document.getElementById('storage-warning-banner')) {
      clearInterval(_storageHealthPollInterval);
      _storageHealthPollInterval = null;
      return;
    }
    if (document.visibilityState !== 'visible') return;
    try {
      const resp = await Api.health.check({ signal: AbortSignal.timeout(3000) });
      if (!resp || !resp.ok) return;
      const data = await resp.json();
      if (data.storage && data.storage.ready === true) {
        _clearStorageWarningBanner();
        clearInterval(_storageHealthPollInterval);
        _storageHealthPollInterval = null;
      }
    } catch (e) {
      console.debug('[StorageHealth] recovery poll failed:', e && e.message);
    }
  }, 15000);
}

function _showStorageWarningBanner() {
  if (document.getElementById('storage-warning-banner')) return;
  const banner = document.createElement('div');
  banner.id = 'storage-warning-banner';
  banner.style.cssText =
    'position:fixed;top:0;left:0;right:0;z-index:10000;' +
    'background:#dc2626;color:#fff;padding:10px 16px;font-size:14px;' +
    'text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3);' +
    'display:flex;align-items:center;justify-content:center;gap:8px;';
  // Guarded t(): the jsdom test harnesses load this file WITHOUT i18n.js, so
  // fall back to the zh literal when t() isn't present. zh is the primary UI.
  const _fallbackMessages = {
    'conn.storageUnavailableTitle': '存储服务暂时不可用',
    'conn.storageUnavailableDesc': '持久化操作已安全暂停，服务器正在自动恢复存储连接。',
    'conn.dismiss': '关闭',
  };
  // The health probe can finish before the asynchronously loaded locale
  // chunk. In that window t() intentionally returns the key; never expose
  // those implementation keys in a user-facing outage banner.
  const _tt = (k, p) => {
    let value = typeof t === 'function' ? t(k, p) : k;
    if (!value || value === k) value = _fallbackMessages[k] || k;
    return value.replace(/\{([A-Za-z0-9_]+)\}/g,
      (token, name) => p && Object.prototype.hasOwnProperty.call(p, name)
        ? String(p[name] ?? '') : token);
  };
  banner.innerHTML =
    '<span style="display:inline-flex"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg></span>' +
    '<span><b>' + _tt('conn.storageUnavailableTitle') + '</b> — ' +
    _tt('conn.storageUnavailableDesc') + '</span>' +
    '<button data-tofu-action="this.parentElement.remove()" style="' +
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
    const unsubscribe = pushOnLatency((reading) => {
      if (!reading) return;
      if (reading.connected === false) _bomSuspect('push_drop');
      // A re-opened socket while suspect/offline might mean the backend is
      // back — but the health probe remains the arbiter (proxy flaps can
      // reopen the WS while HTTP is still broken).
      else if (_bomState.phase !== 'online') _bomProbe('push_reconnected');
    });
    if (typeof unsubscribe === 'function') _bomOwnCleanup(unsubscribe);
  }
  if (typeof pushOnReconnect === 'function') {
    const unsubscribe = pushOnReconnect(() => {
      if (_bomState.phase !== 'online') _bomProbe('push_reopen');
    });
    if (typeof unsubscribe === 'function') _bomOwnCleanup(unsubscribe);
  }
  // ② Browser network events.
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    _bomListen(window, 'offline', () => _bomSuspect('browser_offline'));
    _bomListen(window, 'online', () => {
      if (_bomState.phase !== 'online') _bomProbe('browser_online');
    });
  }
  // A foregrounded tab re-probes immediately (the poll skips hidden tabs).
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    _bomListen(document, 'visibilitychange', () => {
      if (document.visibilityState === 'visible' && _bomState.phase !== 'online') _bomProbe('visible');
    });
  }
}

function destroyBackendOfflineMonitor() {
  if (_bomLifecycleScope) {
    _bomLifecycleScope.destroy();
    _bomLifecycleScope = null;
  }
  _bomClearProbeTimer();
  if (_bomState.pollTimer) clearInterval(_bomState.pollTimer);
  if (_bomState.elapsedTimer) clearInterval(_bomState.elapsedTimer);
  _bomState.pollTimer = null;
  _bomState.elapsedTimer = null;
  _bomHideBanner();
  _bomRestoreTitle();
  _bomState.phase = 'online';
  _bomState.fails = 0;
  _bomState.probing = false;
  _bomState.booted = false;
}

if (typeof window !== 'undefined') {
  runtimeScope.BackendOfflineMonitorProbeNow = BackendOfflineMonitorProbeNow;
  runtimeScope.BackendOfflineMonitorSnooze = BackendOfflineMonitorSnooze;
  runtimeScope.initBackendOfflineMonitor = initBackendOfflineMonitor;
  runtimeScope.destroyBackendOfflineMonitor = destroyBackendOfflineMonitor;
  runtimeScope.BackendOfflineMonitor = _bomState; // harness introspection handle
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initBackendOfflineMonitor,
      { once: true });
  } else {
    initBackendOfflineMonitor();
  }
}

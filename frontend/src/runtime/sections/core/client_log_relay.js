/* ===== migrated source: core/client_log_relay.js ===== */
/* Responsibility: relay the full browser console to logs/frontend.log.
   Entry: the installed runtimeScope.__clientLogRelay.flush port.
   Dependencies: native console/fetch/storage plus late-bound apiUrl/push health.
   Contract: original console calls always win; 400 queued/200 per flush/800
   chars per line; outage batches fail soft without retry or recursive logs.
   Disable client-side with localStorage.tofu_client_log_relay='0'. */
(function () {
  'use strict';
  if (typeof window === 'undefined' || runtimeScope.__clientLogRelay) return;

  var MAX_BUF = 400;
  var MAX_FLUSH = 200;
  var MAX_MSG = 800;
  function _constrainedProxy() {
    try {
      var tag = document.getElementById('tofu-boot-config');
      var config = tag && tag.textContent ? JSON.parse(tag.textContent) : null;
      if (config && config.transportProfile === 'constrained-proxy') return true;
      if (config && config.transportProfile === 'direct') return false;
    } catch (e) { /* path fallback below */ }
    try { return /\/(?:proxy|absproxy)\/\d+(?:\/|$)/.test(location.pathname || ''); }
    catch (e) { return false; }
  }

  // Every tab owns a bounded relay. A constrained gateway needs fewer,
  // de-synchronised batches so several open tabs cannot form a 15s request
  // pulse. Direct/LAN deployments retain the existing cadence.
  var FLUSH_MS = _constrainedProxy() ? 60000 : 15000;
  var buf = [];
  var flushing = false;
  var dropped = 0;
  var sid = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);

  function _enabled() {
    try { return localStorage.getItem('tofu_client_log_relay') !== '0'; }
    catch (e) { return true; }
  }

  function _push(lv, args) {
    if (flushing) return;             // never log about our own flush
    var parts = [];
    for (var i = 0; i < args.length; i++) {
      var a = args[i];
      if (typeof a === 'string') { parts.push(a); continue; }
      if (a && typeof a === 'object' && typeof a.message === 'string') {
        parts.push(String(a.stack || ((a.name || 'Error') + ': ' + a.message)));
        continue;
      }
      try {
        var encoded = JSON.stringify(a);
        parts.push(encoded === undefined ? String(a) : encoded);
      }
      catch (e) { parts.push(String(a)); }
    }
    var msg = parts.join(' ');
    if (!msg) return;
    if (msg.indexOf('/api/v1/logs/client') >= 0) return;   // recursion guard
    if (msg.length > MAX_MSG) msg = msg.slice(0, MAX_MSG) + '…';
    var last = buf[buf.length - 1];
    if (last && last.lv === lv && last.msg === msg) {      // spam fold
      last.n = (last.n || 1) + 1;
      return;
    }
    buf.push({ t: Date.now(), lv: lv, msg: msg });
    if (buf.length > MAX_BUF) {
      buf.splice(0, buf.length - MAX_BUF);
      dropped++;
    }
  }

  ['log', 'info', 'warn', 'error'].forEach(function (fn) {
    var orig = console[fn];
    if (typeof orig !== 'function') return;
    console[fn] = function () {
      try { _push(fn === 'log' ? 'info' : fn, Array.prototype.slice.call(arguments)); }
      catch (e) { /* the relay never throws into app code */ }
      return orig.apply(console, arguments);
    };
  });

  function _relayUrl() {
    return (typeof apiUrl === 'function')
      ? apiUrl('/api/v1/logs/client') : '/api/v1/logs/client';
  }

  function flush(useBeacon) {
    if (!buf.length || flushing) return;
    if (typeof navigator !== 'undefined' && navigator.onLine === false) return;
    if (!useBeacon && typeof pushGetLatency === 'function') {
      try {
        var network = pushGetLatency();
        if (network && (network.connected === false ||
            network.state === 'offline' || network.state === 'timeout')) return;
      } catch (e) { /* latency owner is optional during early boot */ }
    }
    if (!_enabled()) { buf.length = 0; return; }
    // Reserve one slot for the drop summary so the wire batch itself never
    // exceeds MAX_FLUSH (the remaining log line stays buffered for next time).
    var batch = buf.splice(0, Math.max(1, MAX_FLUSH - (dropped > 0 ? 1 : 0)));
    if (dropped > 0) {
      batch.unshift({ t: Date.now(), lv: 'warn',
        msg: '[client-log-relay] dropped ' + dropped + ' older line(s) — buffer cap' });
      dropped = 0;
    }
    var payload;
    try {
      payload = JSON.stringify({ session: sid, url: String(location.href), entries: batch });
    } catch (e) { return; }
    if (useBeacon && typeof navigator !== 'undefined' && navigator.sendBeacon) {
      try {
        navigator.sendBeacon(_relayUrl(), new Blob([payload], { type: 'application/json' }));
        return;
      } catch (e) { /* fall through to fetch */ }
    }
    flushing = true;
    // Route through the ONE frontend→backend seam (the api.js isolation rule,
    // tests/test_frontend_api_isolation.py): Api.logs.clientRelay carries
    // keepalive + silent-drop itself. Api absent = the bundle has not finished
    // evaluating (early pagehide) — drop the batch rather than hand-roll a
    // second channel (the never-amplify doctrine).
    var api = (typeof Api !== 'undefined' && Api.logs) || null;
    if (!api || typeof api.clientRelay !== 'function') { flushing = false; return; }
    Promise.resolve(api.clientRelay(payload)).catch(function () {
      /* drop the batch — a down server must not be amplified by its own
       * telemetry; the next flush carries whatever is new. */
    }).then(function () { flushing = false; });
  }

  function _scheduleFlush() {
    if (typeof setTimeout !== 'function') return;
    var delay = Math.round(FLUSH_MS * (0.85 + Math.random() * 0.30));
    setTimeout(function () {
      // A hidden tab keeps collecting its bounded diagnostics but spends no
      // periodic proxy request. pagehide still gets one best-effort beacon.
      if (!(typeof document !== 'undefined' && document.hidden)) flush(false);
      _scheduleFlush();
    }, delay);
  }
  _scheduleFlush();
  if (typeof window.addEventListener === 'function') {
    window.addEventListener('pagehide', function () { flush(true); });
  }

  runtimeScope.__clientLogRelay = {
    flush: function () { flush(false); },
    _buf: buf,
    _session: sid,
  };
})();

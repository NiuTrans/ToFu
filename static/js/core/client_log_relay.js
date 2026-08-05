/* ═══════════════════════════════════════════════════════════════════
   core/client_log_relay.js — ship the browser console to logs/frontend.log

   THE GAP THIS CLOSES (owner directive 2026-08-05, epic
   pt_cfdfd30c8699407b): live-view bugs (the vanishing queued bubble, the
   settle/rerender races) are diagnosed from client-side breadcrumbs like
   `[loadConvMsgs] 📊 Phase2 reconcile` — but those are console.info, and
   the only browser→server channel (/api/client-error) mirrors warn/error
   alone. Every "the bubble disappeared" postmortem therefore depended on
   the user catching the transient in their own devtools.

   This relay patches console.{log,info,warn,error} into a bounded ring
   buffer and batch-POSTs it to /api/v1/logs/client every 15s (+ a
   sendBeacon on pagehide), where it lands in logs/frontend.log — the full
   stream, not just warnings.

   Design rules:
     • NEVER break the page: the patched console always calls the original;
       every relay step is wrapped so a relay fault cannot throw into app
       code.
     • NEVER amplify an outage: flush failures drop the batch silently; no
       retry storm, no recursive logging about the relay itself.
     • Bounded: 400-line ring (drop-oldest, counted), 200 entries/flush,
       800 chars/entry, consecutive-duplicate fold (×N).
     • Kill-switches: server-side TOFU_CLIENT_LOG_RELAY=0 (route drops),
       client-side localStorage.tofu_client_log_relay='0'.

   Bundle position (lib/js_bundler.py): right after core.js — native
   fetch/localStorage/navigator only, so it can capture every later
   module's lines; apiUrl is resolved at CALL time (typeof-guarded).
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';
  if (typeof window === 'undefined' || window.__clientLogRelay) return;

  var MAX_BUF = 400;
  var MAX_FLUSH = 200;
  var MAX_MSG = 800;
  var FLUSH_MS = 15000;
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
      try { parts.push(JSON.stringify(a)); }
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
    if (!_enabled()) { buf.length = 0; return; }
    var batch = buf.splice(0, MAX_FLUSH);
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
    fetch(_relayUrl(), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: payload,
      credentials: 'same-origin',
      keepalive: true,
    }).catch(function () {
      /* drop the batch — a down server must not be amplified by its own
       * telemetry; the next flush carries whatever is new. */
    }).then(function () { flushing = false; });
  }

  if (typeof setInterval === 'function') {
    setInterval(function () { flush(false); }, FLUSH_MS);
  }
  if (typeof window.addEventListener === 'function') {
    window.addEventListener('pagehide', function () { flush(true); });
  }

  window.__clientLogRelay = {
    flush: function () { flush(false); },
    _buf: buf,
    _session: sid,
  };
})();

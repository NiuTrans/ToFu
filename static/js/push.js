/* ═══════════════════════════════════════════════════════════
   push.js — Unified Server-Push Channel (WebSocket)
   ═══════════════════════════════════════════════════════════

   Single global WebSocket at /api/push multiplexing:
   - Paper report events (progress, section, done)
   - Translation events (running, done, error)
   - Server notifications (config change, health)
   - Chat events for headless API/webhook observers (browser
     chat sessions still receive events via SSE for
     Last-Event-ID resume support)

   Usage:
     pushSubscribe('paper', taskId, handler)   — start receiving events
     pushUnsubscribe('paper', taskId)          — stop receiving events
     pushSend({action: 'abort', channel: 'chat', taskId})  — send command

   Handler signature:
     function handler(event) { ... }
     event = {channel, taskId, type, ...payload}
*/

const _push = (() => {
  let _ws = null;
  let _reconnectTimer = null;
  let _handlers = new Map();       // key: `${channel}:${taskId}` → Set<fn>
  let _globalHandlers = new Map(); // key: channel → Set<fn>
  let _connected = false;
  let _connectedAt = 0;            // set on onopen, cleared on onclose; gate for attempt-counter reset
  let _reconnectAttempt = 0;
  let _everConnected = false;      // true once the FIRST onopen fired — distinguishes a genuine RECONNECT from the initial connect
  let _reconnectListeners = new Set();  // fn() called after a genuine reconnect (not the first connect)
  let _pendingSubscriptions = [];  // queued before connection established
  // Connection must hold this long before we trust it as "healthy" and
  // reset the reconnect attempt counter. See onclose.
  const MIN_UPTIME_MS = 5000;

  // ── Round-trip latency probe (network signal indicator) ──────────────
  // We piggyback a ping/pong on the already-open push socket rather than
  // opening a second connection: the server echoes {type:'pong', t} for
  // every {action:'ping', t} we send, and RTT = now - t.
  const PING_INTERVAL_MS = 4000;   // how often to probe while connected
  const PING_TIMEOUT_MS = 8000;    // no pong within this ⇒ treat as timed out
  let _pingTimer = null;
  let _pingTimeoutTimer = null;    // per-ping watchdog: fires PING_TIMEOUT_MS after a probe so the
                                   // timeout state is EMITTED promptly (~timeout window), not only on
                                   // the next 4s interval tick (which delayed the badge ~12s).
  let _lastPingSentAt = 0;         // client timestamp of the outstanding ping
  let _latencyMs = null;           // last measured RTT; null = unknown
  let _latencyState = 'unknown';   // unknown | good | ok | poor | timeout | offline
  let _latencyListeners = new Set();

  function _emitLatency() {
    // Stamp each reading so a consumer (net-latency.js watchdog) can tell a
    // FRESH reading from a frozen one — if the socket wedges in CONNECTING or
    // a reconnect is scheduled but never opens, no further emit occurs and the
    // last reading would otherwise display forever as if still live.
    const reading = { ms: _latencyMs, state: _latencyState, connected: _connected, at: Date.now() };
    for (const fn of _latencyListeners) {
      try { fn(reading); }
      catch (e) { console.error('[Push] latency listener error:', e); }
    }
  }

  function _classify(ms) {
    if (ms == null) return 'unknown';
    if (ms < 150) return 'good';
    if (ms < 400) return 'ok';
    return 'poor';
  }

  function _sendPing() {
    if (!_connected || !_ws) return;
    // A still-outstanding ping older than the timeout means the pong never
    // came back: the socket is HALF-OPEN — TCP-dead but readyState still OPEN,
    // so _ws.send() won't throw and no onclose fires on its own. Push frames
    // would silently stop forever with no reconnect. Surface the timeout AND
    // force-close so onclose → _scheduleReconnect re-establishes the socket.
    if (_lastPingSentAt && Date.now() - _lastPingSentAt > PING_TIMEOUT_MS) {
      _latencyMs = null;
      _latencyState = 'timeout';
      _emitLatency();
      console.warn('[Push] ping timeout (%dms) — closing half-open socket to force reconnect',
        Date.now() - _lastPingSentAt);
      try { _ws.close(); }
      catch (e) { console.debug('[Push] force-close after ping timeout failed:', e); }
      return;   // do NOT probe again on a socket we've just declared dead
    }
    // Keep only ONE outstanding ping at a time. Re-sending (and overwriting
    // _lastPingSentAt) on every interval reset the outstanding ping's age
    // before the PING_TIMEOUT_MS window could ever elapse — so a half-open
    // socket on a foregrounded tab was NEVER detected. Wait for _onPong to
    // clear _lastPingSentAt before starting a fresh probe.
    if (_lastPingSentAt) return;
    _lastPingSentAt = Date.now();
    try { _ws.send(JSON.stringify({ action: 'ping', t: _lastPingSentAt })); }
    catch (e) { console.debug('[Push] ping send failed:', e); }
    // Arm a dedicated watchdog so the timeout is surfaced right at the window
    // edge instead of waiting for a later interval tick to notice the age.
    if (_pingTimeoutTimer) clearTimeout(_pingTimeoutTimer);
    _pingTimeoutTimer = setTimeout(_firePingTimeout, PING_TIMEOUT_MS);
  }

  // Fired by the per-ping watchdog when a pong has not returned within
  // PING_TIMEOUT_MS. Emits the timeout state IMMEDIATELY (so the signal badge
  // stops showing a stale green reading) and force-closes the half-open socket
  // so onclose → _scheduleReconnect re-establishes it. Mirrors the interval
  // backstop branch in _sendPing but fires seconds sooner.
  function _firePingTimeout() {
    _pingTimeoutTimer = null;
    if (!_lastPingSentAt) return;   // a pong already cleared the outstanding ping
    _latencyMs = null;
    _latencyState = 'timeout';
    _emitLatency();
    console.warn('[Push] ping timeout (watchdog) — closing half-open socket to force reconnect');
    if (_ws) {
      try { _ws.close(); }
      catch (e) { console.debug('[Push] force-close after ping-timeout watchdog failed:', e); }
    }
  }

  function _startPinging() {
    if (_pingTimer) return;
    _sendPing();
    _pingTimer = setInterval(_sendPing, PING_INTERVAL_MS);
  }

  function _stopPinging() {
    if (_pingTimer) { clearInterval(_pingTimer); _pingTimer = null; }
    if (_pingTimeoutTimer) { clearTimeout(_pingTimeoutTimer); _pingTimeoutTimer = null; }
    _lastPingSentAt = 0;
  }

  function _onPong(t) {
    if (!t || t !== _lastPingSentAt) return;   // stale / mismatched pong
    _latencyMs = Date.now() - t;
    _latencyState = _classify(_latencyMs);
    _lastPingSentAt = 0;
    if (_pingTimeoutTimer) { clearTimeout(_pingTimeoutTimer); _pingTimeoutTimer = null; }
    _emitLatency();
  }

  function _key(channel, taskId) { return `${channel}:${taskId}`; }

  function _buildUrl() {
    const loc = window.location;
    const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${loc.host}${apiUrl('/api/push')}`;
  }

  function connect() {
    if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) {
      // Already connected/connecting. If the socket is OPEN, onopen has
      // already fired (and won't fire again), so a late caller — e.g. the
      // latency indicator initialising after some other module opened the
      // socket — would never get pinging started. Kick it off here; it's
      // idempotent (guarded by _pingTimer).
      if (_ws.readyState === WebSocket.OPEN && _connected) _startPinging();
      return;
    }

    const url = _buildUrl();
    try {
      _ws = new WebSocket(url);
    } catch (e) {
      console.warn('[Push] WebSocket constructor failed:', e.message);
      _scheduleReconnect();
      return;
    }

    _ws.onopen = () => {
      _connected = true;
      _connectedAt = Date.now();
      console.info('[Push] ✓ Connected');

      // Replay pending subscriptions
      for (const sub of _pendingSubscriptions) {
        _ws.send(JSON.stringify(sub));
      }
      _pendingSubscriptions = [];

      // Re-subscribe all active handlers
      for (const [key] of _handlers) {
        const [channel, taskId] = key.split(':');
        _ws.send(JSON.stringify({action: 'subscribe', channel, taskId}));
      }

      _startPinging();

      /* ★ Reconnect catch-up: fire reconnect listeners ONLY on a genuine
       *   re-open (a prior connection existed), never on the first connect —
       *   boot already loads the list, so firing here would double-load. While
       *   the socket was DOWN we may have MISSED `notify` frames (a sibling
       *   device's change), so a reconnect must trigger an immediate
       *   reconciliation — this is the third "回来即新" resume trigger. */
      if (_everConnected) {
        for (const fn of _reconnectListeners) {
          try { fn(); } catch (e) { console.error('[Push] reconnect listener error:', e); }
        }
      }
      _everConnected = true;
    };

    _ws.onmessage = (event) => {
      let frame;
      try { frame = JSON.parse(event.data); }
      catch (e) { console.debug('[Push] dropped malformed frame:', e && e.message); return; }

      const channel = frame.channel;
      const taskId = frame.taskId;

      if (frame.type === 'pong') { _onPong(frame.t); return; }
      if (frame.type === 'ping') return;

      // Route to specific task handlers
      const key = _key(channel, taskId);
      const handlers = _handlers.get(key);
      if (handlers) {
        for (const fn of handlers) {
          try { fn(frame); } catch (e) { console.error('[Push] Handler error:', e); }
        }
      }

      // Route to channel-wide handlers (subscribed with taskId='*')
      const globalKey = _key(channel, '*');
      const globalHandlers = _handlers.get(globalKey);
      if (globalHandlers) {
        for (const fn of globalHandlers) {
          try { fn(frame); } catch (e) { console.error('[Push] Global handler error:', e); }
        }
      }
    };

    _ws.onerror = () => {
      console.debug('[Push] Connection error');
    };

    _ws.onclose = (e) => {
      // Reset attempt counter only when the connection actually held long
      // enough to be useful. Without this, a connection that opens then
      // closes within milliseconds would keep _reconnectAttempt=0 and
      // burn CPU reconnecting in a tight loop — onopen alone is not a
      // sufficient signal that the server is healthy.
      if (_connectedAt && Date.now() - _connectedAt >= MIN_UPTIME_MS) {
        _reconnectAttempt = 0;
      }
      _connected = false;
      _connectedAt = 0;
      _ws = null;
      _stopPinging();
      _latencyMs = null;
      _latencyState = 'offline';
      _emitLatency();
      if (e.code === 1000) return;                  // normal close
      // Permanent close codes — the server is telling us not to come back.
      // Reconnecting just generates noise in the server log and risks IP
      // throttling. 1008=policy violation, 1011=internal error during open.
      if (e.code === 1008 || e.code === 1011) {
        console.warn(`[Push] Server closed with permanent code ${e.code} — not reconnecting`);
        return;
      }
      _scheduleReconnect();
    };
  }

  function _scheduleReconnect() {
    if (_reconnectTimer) return;
    // Full jitter (decorrelated): pick a random delay in [0, base], where
    // base grows exponentially up to 30 s. Jitter is essential when many
    // tabs / windows reconnect after the server bounces — without it they
    // stampede in lockstep, hammer the server, and re-trigger the bounce.
    const baseDelay = Math.min(1000 * Math.pow(1.5, _reconnectAttempt), 30000);
    const delay = Math.random() * baseDelay;
    _reconnectAttempt++;
    _reconnectTimer = setTimeout(() => {
      _reconnectTimer = null;
      connect();
    }, delay);
  }

  function subscribe(channel, taskId, handler) {
    const key = _key(channel, taskId);
    if (!_handlers.has(key)) _handlers.set(key, new Set());
    _handlers.get(key).add(handler);

    const msg = {action: 'subscribe', channel, taskId};
    if (_connected && _ws) {
      _ws.send(JSON.stringify(msg));
    } else {
      _pendingSubscriptions.push(msg);
      connect();
    }
  }

  function unsubscribe(channel, taskId, handler) {
    const key = _key(channel, taskId);
    const set = _handlers.get(key);
    if (set) {
      if (handler) {
        set.delete(handler);
        if (set.size === 0) _handlers.delete(key);
      } else {
        _handlers.delete(key);
      }
    }

    if (_connected && _ws) {
      _ws.send(JSON.stringify({action: 'unsubscribe', channel, taskId}));
    }
  }

  function send(msg) {
    if (_connected && _ws) {
      _ws.send(JSON.stringify(msg));
    }
  }

  function isConnected() { return _connected; }

  function getLatency() {
    return { ms: _latencyMs, state: _latencyState, connected: _connected, at: Date.now() };
  }

  function onLatency(fn) {
    if (typeof fn !== 'function') return () => {};
    _latencyListeners.add(fn);
    // Push the current reading immediately so a late subscriber isn't blank
    // until the next probe.
    try { fn(getLatency()); } catch (e) { console.error('[Push] latency listener error:', e); }
    return () => _latencyListeners.delete(fn);
  }

  /* Register a callback fired AFTER a genuine socket RECONNECT (not the first
   *   connect). Used by cross-device sync to reconcile missed `notify` frames
   *   the instant the push channel recovers. Returns an unsubscribe fn. */
  function onReconnect(fn) {
    if (typeof fn !== 'function') return () => {};
    _reconnectListeners.add(fn);
    return () => _reconnectListeners.delete(fn);
  }

  return { connect, subscribe, unsubscribe, send, isConnected, getLatency, onLatency, onReconnect };
})();

// Public API
function pushSubscribe(channel, taskId, handler) { _push.subscribe(channel, taskId, handler); }
function pushUnsubscribe(channel, taskId, handler) { _push.unsubscribe(channel, taskId, handler); }
function pushSend(msg) { _push.send(msg); }
function pushConnect() { _push.connect(); }
function pushIsConnected() { return _push.isConnected(); }
function pushGetLatency() { return _push.getLatency(); }
function pushOnLatency(fn) { return _push.onLatency(fn); }
function pushOnReconnect(fn) { return _push.onReconnect(fn); }

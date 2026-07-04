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
  let _lastPingSentAt = 0;         // client timestamp of the outstanding ping
  let _latencyMs = null;           // last measured RTT; null = unknown
  let _latencyState = 'unknown';   // unknown | good | ok | poor | timeout | offline
  let _latencyListeners = new Set();

  function _emitLatency() {
    for (const fn of _latencyListeners) {
      try { fn({ ms: _latencyMs, state: _latencyState, connected: _connected }); }
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
    // came back — surface that as a timeout before firing the next probe.
    if (_lastPingSentAt && Date.now() - _lastPingSentAt > PING_TIMEOUT_MS) {
      _latencyMs = null;
      _latencyState = 'timeout';
      _emitLatency();
    }
    _lastPingSentAt = Date.now();
    try { _ws.send(JSON.stringify({ action: 'ping', t: _lastPingSentAt })); }
    catch (e) { console.debug('[Push] ping send failed:', e); }
  }

  function _startPinging() {
    if (_pingTimer) return;
    _sendPing();
    _pingTimer = setInterval(_sendPing, PING_INTERVAL_MS);
  }

  function _stopPinging() {
    if (_pingTimer) { clearInterval(_pingTimer); _pingTimer = null; }
    _lastPingSentAt = 0;
  }

  function _onPong(t) {
    if (!t || t !== _lastPingSentAt) return;   // stale / mismatched pong
    _latencyMs = Date.now() - t;
    _latencyState = _classify(_latencyMs);
    _lastPingSentAt = 0;
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
    };

    _ws.onmessage = (event) => {
      let frame;
      try { frame = JSON.parse(event.data); } catch { return; }

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
    return { ms: _latencyMs, state: _latencyState, connected: _connected };
  }

  function onLatency(fn) {
    if (typeof fn !== 'function') return () => {};
    _latencyListeners.add(fn);
    // Push the current reading immediately so a late subscriber isn't blank
    // until the next probe.
    try { fn(getLatency()); } catch (e) { console.error('[Push] latency listener error:', e); }
    return () => _latencyListeners.delete(fn);
  }

  return { connect, subscribe, unsubscribe, send, isConnected, getLatency, onLatency };
})();

// Public API
function pushSubscribe(channel, taskId, handler) { _push.subscribe(channel, taskId, handler); }
function pushUnsubscribe(channel, taskId, handler) { _push.unsubscribe(channel, taskId, handler); }
function pushSend(msg) { _push.send(msg); }
function pushConnect() { _push.connect(); }
function pushIsConnected() { return _push.isConnected(); }
function pushGetLatency() { return _push.getLatency(); }
function pushOnLatency(fn) { return _push.onLatency(fn); }

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

  function _key(channel, taskId) { return `${channel}:${taskId}`; }

  function _buildUrl() {
    const loc = window.location;
    const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${loc.host}${apiUrl('/api/push')}`;
  }

  function connect() {
    if (_ws && (_ws.readyState === WebSocket.OPEN || _ws.readyState === WebSocket.CONNECTING)) {
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
    };

    _ws.onmessage = (event) => {
      let frame;
      try { frame = JSON.parse(event.data); } catch { return; }

      const channel = frame.channel;
      const taskId = frame.taskId;

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

  return { connect, subscribe, unsubscribe, send, isConnected };
})();

// Public API
function pushSubscribe(channel, taskId, handler) { _push.subscribe(channel, taskId, handler); }
function pushUnsubscribe(channel, taskId, handler) { _push.unsubscribe(channel, taskId, handler); }
function pushSend(msg) { _push.send(msg); }
function pushConnect() { _push.connect(); }
function pushIsConnected() { return _push.isConnected(); }

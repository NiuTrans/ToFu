/* ===== migrated source: push.js ===== */
/* Responsibility: multiplex non-conversation events over one /api/push socket.
   Entries: pushSubscribe, pushUnsubscribe, pushOnReconnect, pushOnBuildId,
   pushRpcRequest.
   Dependencies: WebSocket, page visibility, apiUrl, and bounded timers.
   Conversation turns retain resumable SSE; handlers receive wire events. */

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
  let _pendingSends = [];          // control messages queued while disconnected ()
  // Correlated requests never use _pendingSends: replaying an RPC after a
  // reconnect would give an old request a new socket lifetime. The endpoint
  // transport owns an explicit HTTP fallback instead.
  let _rpcPending = new Map();
  let _rpcSequence = 0;
  const RPC_PENDING_MAX = 32;
  const RPC_TIMEOUT_MS = 10000;
  // Connection must hold this long before we trust it as "healthy" and
  // reset the reconnect attempt counter. See onclose.
  const MIN_UPTIME_MS = 5000;

  // Ping/pong on the existing socket supplies the network indicator RTT.
  const PING_INTERVAL_MS = 4000;   // how often to probe while connected
  const PING_HIDDEN_INTERVAL_MS = 20000; // hidden tabs keep proof-of-life cheaply
  const PING_TIMEOUT_MS = 8000;    // no pong within this ⇒ treat as timed out (FLOOR)
  const PING_HIDDEN_TIMEOUT_MS = 30000; // background timer clamping is expected
  const PING_TIMEOUT_MAX_MS = 30000; // adaptive ceiling under a slow proxy/tunnel
  const BUILD_PROBE_INTERVAL_MS = 5 * 60 * 1000;
  const BUILD_ID_LISTENER_MAX = 8;
  // Scale the half-open verdict with observed RTT so a slow proxy cannot feed
  // a reconnect/full-refetch loop; background timer clamping gets a 30s floor.
  function _foregroundPingTimeoutMs() {
    const rtt = _latencyMs || 0;
    return Math.min(PING_TIMEOUT_MAX_MS, Math.max(PING_TIMEOUT_MS, rtt * 4));
  }

  function _pingTimeoutMs() {
    const foreground = _foregroundPingTimeoutMs();
    return _pageHidden() ? Math.max(PING_HIDDEN_TIMEOUT_MS, foreground) : foreground;
  }

  function _pageHidden() {
    // The Android WebView reports visible while backgrounded; the shell's
    // nativeVisibility bridge is the only reliable pocket signal there.
    return (typeof document !== 'undefined' && !!document.hidden)
      || (typeof runtimeScope !== 'undefined'
        && runtimeScope.nativeVisibility?.isHidden() === true);
  }

  function _pingIntervalMs() {
    return _pageHidden() ? PING_HIDDEN_INTERVAL_MS : PING_INTERVAL_MS;
  }
  let _pingTimer = null;
  let _pingTimeoutTimer = null;    // per-ping watchdog emits before the next tick
  let _lastPingSentAt = 0;         // client timestamp of the outstanding ping
  let _lastInboundAt = 0;          // last time ANY frame arrived (proof-of-life ledger)
  let _latencyMs = null;           // last measured RTT; null = unknown
  let _latencyState = 'unknown';   // unknown | good | ok | poor | timeout | offline
  let _latencyListeners = new Set();
  let _lastBuildProbeAt = 0;
  let _lastBuildId = null;
  let _buildIdListeners = new Set();

  function _observeBuildId(value) {
    if (typeof value !== 'string' || value.length > 180 ||
        !/^main-[A-Za-z0-9_-]+\.js$/.test(value) || value === _lastBuildId) return;
    _lastBuildId = value;
    for (const fn of _buildIdListeners) {
      try { fn(value); }
      catch (e) { console.error('[Push] build-id listener error:', e); }
    }
  }

  function _emitLatency() {
    // Stamp each reading for diagnostics and late-subscriber presentation.
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

  function _socketIsOpen() {
    return !!(_connected && _ws && _ws.readyState === WebSocket.OPEN);
  }

  function _rpcError(message, code, data) {
    const error = new Error(message || 'Control RPC failed');
    error.name = 'PushRpcError';
    error.code = code == null ? 'rpc_error' : code;
    error.data = data == null ? null : data;
    return error;
  }

  function _clearRpcEntry(entry) {
    if (entry.timeoutId != null) clearTimeout(entry.timeoutId);
    if (entry.signal && entry.abortListener) {
      entry.signal.removeEventListener('abort', entry.abortListener);
    }
  }

  function _cancelRpcOnWire(id) {
    if (!_socketIsOpen()) return;
    try {
      _ws.send(JSON.stringify({
        jsonrpc: '2.0', method: '$/cancelRequest', params: { id },
      }));
    } catch (e) {
      console.debug('[Push] RPC cancellation send failed:', e && e.message);
    }
  }

  function _rejectPendingRpc(error) {
    for (const entry of _rpcPending.values()) {
      _clearRpcEntry(entry);
      try { entry.reject(error); } catch (_) { /* Promise already settled */ }
    }
    _rpcPending.clear();
  }

  function _onRpcResponse(frame) {
    const id = frame && frame.id;
    const entry = _rpcPending.get(id);
    if (!entry) return;
    _rpcPending.delete(id);
    _clearRpcEntry(entry);
    if (Object.prototype.hasOwnProperty.call(frame, 'result')) {
      entry.resolve(frame.result);
      return;
    }
    const rpcError = frame.error && typeof frame.error === 'object'
      ? frame.error : {};
    entry.reject(_rpcError(
      rpcError.message || 'Control RPC failed',
      rpcError.code == null ? 'rpc_protocol' : rpcError.code,
      rpcError.data,
    ));
  }

  function request(method, params, options) {
    const opts = options || {};
    if (typeof method !== 'string' || !method || method.length > 128) {
      return Promise.reject(_rpcError('Invalid RPC method', 'rpc_protocol'));
    }
    const signal = opts.signal;
    if (signal && signal.aborted) {
      return Promise.reject(signal.reason || _rpcError('Request aborted', 'aborted'));
    }
    if (!_socketIsOpen()) {
      connect();
      return Promise.reject(_rpcError(
        'Control socket is unavailable', 'rpc_unavailable'));
    }
    if (_rpcPending.size >= RPC_PENDING_MAX) {
      return Promise.reject(_rpcError(
        'Control request queue is full', 'rpc_overloaded'));
    }

    const id = (_wsRid || 'socket') + '-rpc' + (++_rpcSequence);
    const timeoutMs = Math.max(
      1, Number(opts.timeout == null ? RPC_TIMEOUT_MS : opts.timeout) || RPC_TIMEOUT_MS);
    return new Promise((resolve, reject) => {
      const entry = {
        resolve, reject, signal, abortListener: null, timeoutId: null,
      };
      const rejectAndCancel = (error) => {
        if (_rpcPending.get(id) !== entry) return;
        _rpcPending.delete(id);
        _clearRpcEntry(entry);
        _cancelRpcOnWire(id);
        reject(error);
      };
      if (signal) {
        entry.abortListener = () => rejectAndCancel(
          signal.reason || _rpcError('Request aborted', 'aborted'));
        signal.addEventListener('abort', entry.abortListener, { once: true });
      }
      entry.timeoutId = setTimeout(() => rejectAndCancel(
        _rpcError('Control RPC timed out', 'rpc_timeout')), timeoutMs);
      _rpcPending.set(id, entry);
      try {
        _ws.send(JSON.stringify({
          jsonrpc: '2.0', id, method, params: params || {},
        }));
      } catch (error) {
        rejectAndCancel(_rpcError(
          (error && error.message) || 'Control RPC send failed',
          'rpc_disconnected'));
      }
    });
  }

  function _sendPing() {
    if (!_socketIsOpen()) return;
    // A still-outstanding ping older than the timeout means the pong never
    // came back AND no other frame arrived either (any inbound frame resets
    // the outstanding probe — see onmessage): the socket is HALF-OPEN —
    // TCP-dead but readyState still OPEN, so _ws.send() won't throw and no
    // onclose fires on its own. Push frames would silently stop forever with
    // no reconnect. Surface the timeout AND force-close so onclose →
    // _scheduleReconnect re-establishes the socket.
    if (_lastPingSentAt && Date.now() - _lastPingSentAt > _pingTimeoutMs()) {
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
    const ping = { action: 'ping', t: _lastPingSentAt };
    const shouldProbeBuild = !_lastBuildProbeAt ||
      _lastPingSentAt - _lastBuildProbeAt >= BUILD_PROBE_INTERVAL_MS;
    if (shouldProbeBuild) ping.buildProbe = true;
    try {
      _ws.send(JSON.stringify(ping));
      if (shouldProbeBuild) _lastBuildProbeAt = _lastPingSentAt;
    }
    catch (e) { console.debug('[Push] ping send failed:', e); }
    // Arm a dedicated watchdog so the timeout is surfaced right at the window
    // edge instead of waiting for a later interval tick to notice the age.
    if (_pingTimeoutTimer) clearTimeout(_pingTimeoutTimer);
    _pingTimeoutTimer = setTimeout(_firePingTimeout, _pingTimeoutMs());
  }

  // Fired by the per-ping watchdog when a pong has not returned within
  // PING_TIMEOUT_MS. Emits the timeout state IMMEDIATELY (so the signal badge
  // stops showing a stale green reading) and force-closes the half-open socket
  // so onclose → _scheduleReconnect re-establishes it. Mirrors the interval
  // backstop branch in _sendPing but fires seconds sooner.
  function _firePingTimeout() {
    _pingTimeoutTimer = null;
    /* Any inbound frame (data OR pong) disarms this watchdog by clearing
     * _lastPingSentAt — so reaching the verdict below means total inbound
     * silence for the whole window: a genuinely half-open socket. */
    if (!_lastPingSentAt) return;   // a frame already cleared the outstanding ping
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
    _pingTimer = setInterval(_sendPing, _pingIntervalMs());
  }

  function _refreshPingCadence() {
    if (!_connected) return;
    if (_pingTimer) clearInterval(_pingTimer);
    _pingTimer = setInterval(_sendPing, _pingIntervalMs());
    if (_lastPingSentAt) {
      if (_pingTimeoutTimer) clearTimeout(_pingTimeoutTimer);
      const elapsed = Math.max(0, Date.now() - _lastPingSentAt);
      _pingTimeoutTimer = setTimeout(
        _firePingTimeout, Math.max(1, _pingTimeoutMs() - elapsed));
    }
    if (!_pageHidden()) {
      // The existing visibility-resume ping doubles as the build handshake;
      // no second listener, timer, or HTTP request is needed.
      _lastBuildProbeAt = 0;
      _sendPing();
    }
  }

  function _stopPinging() {
    if (_pingTimer) { clearInterval(_pingTimer); _pingTimer = null; }
    if (_pingTimeoutTimer) { clearTimeout(_pingTimeoutTimer); _pingTimeoutTimer = null; }
    _lastPingSentAt = 0;
    _lastBuildProbeAt = 0;
  }

  function _onPong(t) {
    if (!t || t !== _lastPingSentAt) return;   // stale / mismatched pong
    _latencyMs = Date.now() - t;
    _latencyState = _classify(_latencyMs);
    _lastPingSentAt = 0;
    if (_pingTimeoutTimer) { clearTimeout(_pingTimeoutTimer); _pingTimeoutTimer = null; }
    _emitLatency();
  }

  if (typeof document !== 'undefined' && document.addEventListener) {
    document.addEventListener('visibilitychange', _refreshPingCadence);
  }

  function _key(channel, taskId) { return `${channel}:${taskId}`; }

  /* Per-socket correlation id, minted once per connect attempt.
   *
   * A browser `WebSocket` cannot set custom headers, so the id rides a QUERY
   * PARAM instead of `X-Request-ID` (server.py::_resolve_inbound_rid honors
   * both channels, so it is one id space across transports). Reconnects mint
   * a FRESH id on purpose: each socket is its own session in the log, and
   * reusing one id across reconnects would merge unrelated lifetimes.
   *
   * Shares api.js's page prefix when available so a socket groups with the
   * HTTP requests from the same page load under one grep. */
  let _wsRid = '';
  let _wsRidSeq = 0;

  function _mintWsRid() {
    let page = '';
    try {
      if (typeof Api !== 'undefined' && Api && typeof Api.pageRequestId === 'function') {
        page = Api.pageRequestId() || '';
      }
    } catch (e) { /* api.js not loaded yet — fall back to a standalone id */ }
    if (!page) page = Math.random().toString(36).slice(2, 8);
    return page + '-ws' + (++_wsRidSeq);
  }

  /** The correlation id of the CURRENT socket ('' when never connected).
   *  Exposed so diagnostics can quote it alongside HTTP request ids. */
  function socketRequestId() { return _wsRid; }

  function _buildUrl() {
    const loc = window.location;
    const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
    _wsRid = _mintWsRid();
    const base = `${proto}//${loc.host}${apiUrl('/api/push')}`;
    return base + (base.indexOf('?') === -1 ? '?' : '&') +
      '_rid=' + encodeURIComponent(_wsRid);
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
    // A CLOSING/CLOSED socket may not have delivered onclose yet. Clear its
    // logical ownership before constructing the replacement so senders queue
    // behind the new CONNECTING socket instead of calling send() on it under
    // the stale `_connected=true` bit.
    _connected = false;
    _connectedAt = 0;
    _stopPinging();
    let socket;
    try {
      socket = new WebSocket(url);
      _ws = socket;
    } catch (e) {
      _ws = null;
      console.warn('[Push] WebSocket constructor failed:', e.message);
      _scheduleReconnect();
      return;
    }

    socket.onopen = () => {
      // A late event from a superseded socket must not mutate or send through
      // the current generation.
      if (_ws !== socket) return;
      _connected = true;
      _connectedAt = Date.now();
      console.info('[Push] ✓ Connected');

      // Flush control messages queued while the socket was down.
      for (const m of _pendingSends) {
        socket.send(JSON.stringify(m));
      }
      _pendingSends = [];

      // Re-subscribe all active handlers
      for (const [key] of _handlers) {
        const sep = key.indexOf(':');
        const channel = key.slice(0, sep);
        const taskId = key.slice(sep + 1);
        socket.send(JSON.stringify({action: 'subscribe', channel, taskId}));
      }

      _startPinging();

      /* Reconnect catch-up: fire reconnect listeners ONLY on a genuine
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

    socket.onmessage = (event) => {
      if (_ws !== socket) return;
      /* Proof-of-life ( ①b): ANY inbound frame — data, pong,
       * even an unparseable one — is bytes arriving on the wire, the very
       * definition of a NOT-half-open socket. Stamp the ledger BEFORE parsing
       * so even a malformed frame counts. */
      _lastInboundAt = Date.now();
      let frame;
      try { frame = JSON.parse(event.data); }
      catch (e) { console.debug('[Push] dropped malformed frame:', e && e.message); return; }

      const channel = frame.channel;
      const taskId = frame.taskId;

      if (frame.type === 'pong') {
        _observeBuildId(frame.buildId);
        _onPong(frame.t);
        return;
      }
      if (frame.type === 'ping') return;

      /* A DATA frame restarts the probe cycle: a server busy streaming large
       * event frames can have its pong queued behind the data (the pong has
       * a server-side priority lane, but the client verdict must not depend
       * on it), and the watchdog used to only clear on a matching pong — so
       * heavy server→client traffic (the sign of a HEALTHY socket) could end
       * in a force-close. Clear the outstanding ping + watchdog on any frame;
       * the next interval tick re-arms a fresh probe. The skipped RTT sample
       * is acceptable (a late pong is ignored by _onPong's t-match). */
      if (_lastPingSentAt) {
        _lastPingSentAt = 0;
        if (_pingTimeoutTimer) { clearTimeout(_pingTimeoutTimer); _pingTimeoutTimer = null; }
      }

      if (frame.jsonrpc === '2.0') {
        _onRpcResponse(frame);
        return;
      }

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
      // A frame whose own taskId is '*' already used this exact Set through
      // the specific-key branch above. Dispatching the wildcard branch again
      // applied connect snapshots and broadcast notifications twice.
      if (globalKey !== key) {
        const globalHandlers = _handlers.get(globalKey);
        if (globalHandlers) {
          for (const fn of globalHandlers) {
            try { fn(frame); } catch (e) { console.error('[Push] Global handler error:', e); }
          }
        }
      }
    };

    socket.onerror = () => {
      if (_ws !== socket) return;
      console.debug('[Push] Connection error');
    };

    socket.onclose = (e) => {
      // connect() is allowed to replace a CLOSING socket before its delayed
      // close event arrives. That old event cannot clear the new connection.
      if (_ws !== socket) return;
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
      _lastInboundAt = 0;
      _latencyMs = null;
      _latencyState = 'offline';
      _rejectPendingRpc(_rpcError(
        'Control socket disconnected', 'rpc_disconnected'));
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
    let handlers = _handlers.get(key);
    const firstLocalSubscriber = !handlers || handlers.size === 0;
    if (!handlers) {
      handlers = new Set();
      _handlers.set(key, handlers);
    }
    handlers.add(handler);

    const msg = {action: 'subscribe', channel, taskId};
    if (_socketIsOpen()) {
      // The server tracks one membership per socket/key, while the browser
      // may have several local consumers. Only the 0→1 edge subscribes.
      if (firstLocalSubscriber) _ws.send(JSON.stringify(msg));
    } else {
      // _handlers is the durable subscription source. onopen replays each
      // unique key exactly once; a second pending queue sent every key twice.
      connect();
    }
  }

  function unsubscribe(channel, taskId, handler) {
    const key = _key(channel, taskId);
    const set = _handlers.get(key);
    let lastLocalSubscriber = false;
    if (set) {
      if (handler) {
        set.delete(handler);
        if (set.size === 0) {
          _handlers.delete(key);
          lastLocalSubscriber = true;
        }
      } else {
        _handlers.delete(key);
        lastLocalSubscriber = true;
      }
    }

    // Keep the server subscription alive while ANY local handler still owns
    // this key. Unsubscribing one of two consumers used to silently starve the
    // survivor until the next WebSocket reconnect.
    if (lastLocalSubscriber && _socketIsOpen()) {
      _ws.send(JSON.stringify({action: 'unsubscribe', channel, taskId}));
    }
  }

  function send(msg) {
    if (_socketIsOpen()) {
      _ws.send(JSON.stringify(msg));
    } else {
      /* Queue instead of silently dropping: a control command (e.g. abort)
       *   clicked inside a reconnect gap used to vanish with zero feedback —
       *   the user saw "stop does nothing". Cap the queue; drop oldest. */
      if (_pendingSends.length >= 50) {
        _pendingSends.shift();
        console.warn('[Push] send queue full — dropped oldest queued message');
      }
      _pendingSends.push(msg);
      connect();   // ensure a reconnect is in flight so the queue drains
    }
  }

  function isConnected() { return _socketIsOpen(); }

  function getLatency() {
    // lastInboundAt doubles as the proof-of-life ledger's public readout —
    // diagnostics can tell "socket silent for N ms" apart from "closed".
    return { ms: _latencyMs, state: _latencyState, connected: _connected, at: Date.now(), lastInboundAt: _lastInboundAt };
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

  /* Build identity is a low-rate field on the existing pong control frame.
   * Replay the last valid value to late subscribers so boot ordering cannot
   * miss the first socket handshake. The cap makes the transport owner remain
   * bounded even if a caller forgets to unsubscribe. */
  function onBuildId(fn) {
    if (typeof fn !== 'function' || _buildIdListeners.size >= BUILD_ID_LISTENER_MAX) {
      return () => {};
    }
    _buildIdListeners.add(fn);
    if (_lastBuildId) {
      try { fn(_lastBuildId); }
      catch (e) { console.error('[Push] build-id listener error:', e); }
    }
    return () => _buildIdListeners.delete(fn);
  }

  return { connect, subscribe, unsubscribe, send, request, isConnected, getLatency, onLatency, onReconnect, onBuildId, socketRequestId };
})();

// Public API
function pushSubscribe(channel, taskId, handler) { _push.subscribe(channel, taskId, handler); }
function pushUnsubscribe(channel, taskId, handler) { _push.unsubscribe(channel, taskId, handler); }
function pushConnect() { _push.connect(); }
function pushIsConnected() { return _push.isConnected(); }
function pushGetLatency() { return _push.getLatency(); }
function pushOnLatency(fn) { return _push.onLatency(fn); }
function pushOnReconnect(fn) { return _push.onReconnect(fn); }
function pushOnBuildId(fn) { return _push.onBuildId(fn); }
function pushRpcRequest(method, params, options) { return _push.request(method, params, options); }

// Lazy/typed modules have their own ESM scope. Publish stable transport
// functions through the private registry; retained sections keep using the
// lexical helpers above. These are functions over the live `_push` owner, not
// captured connection-state snapshots.
runtimeScope.pushSubscribe = pushSubscribe;
runtimeScope.pushUnsubscribe = pushUnsubscribe;
runtimeScope.pushIsConnected = pushIsConnected;
runtimeScope.pushOnReconnect = pushOnReconnect;
runtimeScope.pushRpcRequest = pushRpcRequest;

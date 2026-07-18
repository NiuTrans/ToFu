/* ═══════════════════════════════════════════════════════════
   net-latency.js — Real-time network latency signal indicator
   ═══════════════════════════════════════════════════════════

   A small signal-bars widget in the topbar that reflects the round-trip
   latency of the live push WebSocket (push.js), so a poor network shows
   up at a glance and can be ruled in/out as the cause of slow responses.

   Data source: pushOnLatency(fn) — push.js probes the already-open socket
   every few seconds ({action:'ping',t} → {type:'pong',t}) and reports
   {ms, state, connected}. No extra connection, no polling endpoint.

   State → visual:
     good    (<150ms)  4 bars, green
     ok      (<400ms)  3 bars, amber
     poor    (>=400ms) 2 bars, orange-red
     timeout           1 bar,  red (pong never returned)
     offline           0 bars, gray (socket closed / reconnecting)
     unknown           faint,  gray (no reading yet)
*/

(function () {
  const BAR_COUNT = 4;
  let _el = null;      // container span
  let _barsEl = null;  // bars wrapper
  let _textEl = null;  // ms label
  let _unsub = null;
  let _lastReading = null;   // most recent reading from pushOnLatency
  let _lastReadingAt = 0;    // when it arrived (ms) — for the staleness watchdog
  let _watchdogTimer = null;
  let _sseDegraded = false;  // any active chat stream is reconnecting / stalled
  let _unsubStream = null;
  // push.js probes every ~4s; if two probe windows pass with no fresh reading
  // the socket is wedged (stuck CONNECTING / reconnect scheduled but not open),
  // so a stale "good/120ms" would be a lie. Force the offline display instead.
  const _STALE_MS = 9000;

  // How many bars to light per state.
  const _barsFor = {
    good: 4, ok: 3, poor: 2, timeout: 1, offline: 0, unknown: 0,
  };

  function _label(reading) {
    const { ms, state, connected } = reading;
    if (!connected || state === 'offline') return t('net.offline') || '离线';
    if (state === 'timeout') return t('net.timeout') || '超时';
    // SSE chat stream is reconnecting even though the push RTT looks fine — the
    // reply connection is the one in trouble, so say so rather than a green ms.
    if (_sseDegraded) return t('net.reconnecting') || '重连中';
    if (ms == null) return '—';
    return ms + 'ms';
  }

  function _title(reading) {
    const { ms, state, connected } = reading;
    const head = t('net.title') || '网络延迟';
    if (!connected || state === 'offline') {
      return `${head}: ${t('net.offlineDesc') || '推送连接已断开'}`;
    }
    if (state === 'timeout') {
      return `${head}: ${t('net.timeoutDesc') || '探测超时，网络可能很差'}`;
    }
    if (_sseDegraded) {
      const base = (ms == null) ? head : `${head}: ${ms}ms`;
      return `${base} — ${t('net.reconnectingDesc') || '聊天连接正在重连'}`;
    }
    if (ms == null) return `${head}: —`;
    const q = t('net.state.' + state) || state;
    return `${head}: ${ms}ms (${q})`;
  }

  function _render(reading) {
    if (!_el) return;
    _lastReading = reading;
    _lastReadingAt = Date.now();
    let state = (!reading.connected) ? 'offline' : (reading.state || 'unknown');
    /* ① Merge the chat-SSE health: if any active reply stream is
     *    reconnecting/stalled, the badge must warn even when the push RTT is
     *    green. Show the WORSE of the two — but never DOWNGRADE a real push
     *    offline/timeout (those are already the most severe). A healthy push
     *    (good/ok/poor/unknown) with a degraded SSE is painted as 'poor' so the
     *    bars go warning-coloured; the label/title then name the reconnect. */
    if (_sseDegraded && state !== 'offline' && state !== 'timeout') {
      state = 'poor';
    }
    const lit = _barsFor[state] != null ? _barsFor[state] : 0;

    _el.dataset.state = state;
    const bars = _barsEl.children;
    for (let i = 0; i < bars.length; i++) {
      bars[i].classList.toggle('lit', i < lit);
    }
    _textEl.textContent = _label(reading);
    _el.title = _title(reading);
  }

  function _build() {
    _el = document.getElementById('netLatencyBadge');
    if (!_el) return false;
    _barsEl = _el.querySelector('.net-bars');
    _textEl = _el.querySelector('.net-ms');
    if (!_barsEl || !_textEl) return false;
    // Build the bars once (increasing height, signal-style).
    _barsEl.innerHTML = '';
    for (let i = 0; i < BAR_COUNT; i++) {
      const b = document.createElement('span');
      b.className = 'net-bar';
      _barsEl.appendChild(b);
    }
    return true;
  }

  function initNetLatency() {
    if (!_build()) return;
    if (typeof pushOnLatency !== 'function') {
      console.warn('[NetLatency] pushOnLatency unavailable — indicator inert');
      return;
    }
    if (_unsub) _unsub();
    _unsub = pushOnLatency(_render);
    // ① Subscribe to chat-SSE health so a reconnecting reply stream flips the
    //    badge to warning even when the push RTT is fine. Repaint the last
    //    push reading through the merge whenever the SSE state toggles.
    if (typeof streamHealthSubscribe === 'function') {
      if (_unsubStream) _unsubStream();
      _unsubStream = streamHealthSubscribe((h) => {
        _sseDegraded = !!(h && h.degraded);
        _render(_lastReading || pushGetLatency());
      });
    }
    // Staleness watchdog: pushOnLatency only fires when push.js emits. If the
    // socket wedges (CONNECTING forever, or a reconnect scheduled but the open
    // never lands) emits stop, and the badge would freeze on the last reading
    // — e.g. green "120ms" while actually disconnected. Every few seconds,
    // if the newest reading is older than _STALE_MS AND it wasn't already an
    // offline reading, repaint as offline so the widget never lies.
    if (_watchdogTimer) clearInterval(_watchdogTimer);
    _watchdogTimer = setInterval(() => {
      if (!_el) return;
      if (!_lastReadingAt) return;
      const age = Date.now() - _lastReadingAt;
      if (age < _STALE_MS) return;
      if (_lastReading && _lastReading.connected === false) return; // already offline
      /* ③ A stale reading does NOT prove the socket is dead. Under a buffering
       *    proxy (VS Code port-forward) ping/pong frames are delayed/batched,
       *    so a late pong is not a disconnect. Only paint OFFLINE when push.js
       *    itself reports the socket is closed (pushIsConnected() === false).
       *    While the socket is still OPEN we paint the neutral 'unknown' (gray,
       *    not red) — no more false offline↔green flapping. push.js owns the
       *    real offline/timeout verdict via onclose / ping-timeout. */
      const sockOpen = (typeof pushIsConnected === 'function') ? pushIsConnected() : false;
      const verdict = sockOpen ? 'unknown' : 'offline';
      console.debug('[NetLatency] no fresh reading for %dms — socket %s → painting %s',
        age, sockOpen ? 'still OPEN' : 'closed', verdict);
      _render({ ms: null, state: verdict, connected: sockOpen, at: Date.now() });
    }, 4000);
    // Ensure the push socket is actually connecting so probes can flow even
    // if nothing else has subscribed yet.
    try { if (typeof pushConnect === 'function') pushConnect(); } catch (e) { /* noop */ }
  }

  window.initNetLatency = initNetLatency;

  // Boot after DOM + push.js are present. main.js loads last; we self-init on
  // DOMContentLoaded and retry once shortly after in case the topbar node is
  // injected late.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initNetLatency);
  } else {
    initNetLatency();
  }
})();

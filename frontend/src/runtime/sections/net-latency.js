/* ===== migrated source: net-latency.js ===== */
/* ═══════════════════════════════════════════════════════════
   net-latency.js — Real-time network latency signal indicator
   ═══════════════════════════════════════════════════════════

   A small signal-bars widget in the topbar that reflects the round-trip
   latency of the live push WebSocket (push.js), so a poor network shows
   up at a glance and can be ruled in/out as the cause of slow responses.

   Data source: pushOnLatency(fn) — push.js probes the already-open socket,
   owns the per-ping timeout/close verdict, and reports {ms,state,connected}.
   This projection owns no second liveness clock, connection, or endpoint.

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
  let _sseDegraded = false;  // any active chat stream is reconnecting / stalled
  let _unsubStream = null;

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

  function _releaseSubscriptions() {
    const releases = [_unsub, _unsubStream];
    _unsub = null;
    _unsubStream = null;
    for (const release of releases) {
      if (typeof release !== 'function') continue;
      try { release(); }
      catch (error) {
        console.debug('[NetLatency] subscription cleanup failed', error);
      }
    }
  }

  function initNetLatency() {
    if (!_build()) return;
    if (typeof pushOnLatency !== 'function') {
      console.warn('[NetLatency] pushOnLatency unavailable — indicator inert');
      return;
    }
    _releaseSubscriptions();
    _unsub = pushOnLatency(_render);
    // ① Subscribe to chat-SSE health so a reconnecting reply stream flips the
    //    badge to warning even when the push RTT is fine. Repaint the last
    //    push reading through the merge whenever the SSE state toggles.
    if (typeof streamHealthSubscribe === 'function') {
      _unsubStream = streamHealthSubscribe((h) => {
        _sseDegraded = !!(h && h.degraded);
        _render(_lastReading || pushGetLatency());
      });
    }
    // Ensure the push socket is actually connecting so probes can flow even
    // if nothing else has subscribed yet.
    try { if (typeof pushConnect === 'function') pushConnect(); } catch (e) { /* noop */ }
  }

  function destroyNetLatency() {
    _releaseSubscriptions();
    _lastReading = null;
    _el = null;
    _barsEl = null;
    _textEl = null;
  }

  runtimeScope.initNetLatency = initNetLatency;
  if (typeof retainedCompositionLifecycle !== 'undefined') {
    retainedCompositionLifecycle.add(destroyNetLatency);
  }

  // Boot once after DOM + push.js are present.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initNetLatency, { once: true });
  } else {
    initNetLatency();
  }
})();

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

  // How many bars to light per state.
  const _barsFor = {
    good: 4, ok: 3, poor: 2, timeout: 1, offline: 0, unknown: 0,
  };

  function _label(reading) {
    const { ms, state, connected } = reading;
    if (!connected || state === 'offline') return t('net.offline') || '离线';
    if (state === 'timeout') return t('net.timeout') || '超时';
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
    if (ms == null) return `${head}: —`;
    const q = t('net.state.' + state) || state;
    return `${head}: ${ms}ms (${q})`;
  }

  function _render(reading) {
    if (!_el) return;
    const state = (!reading.connected) ? 'offline' : (reading.state || 'unknown');
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

/* ===== migrated source: timer.js ===== */
/* ═══════════════════════════════════════════
   timer.js — Timer Watcher panel & badge
   ═══════════════════════════════════════════ */

let _timerPanelOpen = false;
let _timerPollInterval = null;
let _timerListInFlight = null;
let _timerPushWired = false;
let _timerRefreshTimer = null;
let _timerRefreshAfterFlight = false;
/* In-flight row operations (): timerId →
 *   'cancelling' | 'triggering'. Consulted by _renderTimerList so the busy
 *   state SURVIVES the panel's 30s auto-refresh — a one-off DOM patch would
 *   be silently reverted by it. _timerLastTimers caches the last fetched rows
 *   so the pending re-render is synchronous (no refetch on the click frame). */
let _timerPending = {};
let _timerLastTimers = null;

function _timerSetPending(timerId, kind) {
  _timerPending[timerId] = kind;
  if (_timerLastTimers) _renderTimerList(_timerLastTimers);
}
function _timerClearPending(timerId, skipRender) {
  if (delete _timerPending[timerId] && !skipRender && _timerLastTimers) {
    _renderTimerList(_timerLastTimers);
  }
}

/* One list request is shared by badge, panel, push invalidation, and fallback
 * polling.  When an invalidation lands during a request, schedule exactly one
 * follow-up after it settles so a just-returned stale snapshot cannot win. */
function _fetchTimerList(summaryOnly = false) {
  if (_timerListInFlight) {
    // A full response satisfies a concurrent badge refresh too. The reverse
    // is not true: if the panel opens while a summary request is in flight,
    // promote to one full request after it settles instead of rendering the
    // missing ``timers`` array as an empty panel.
    if (summaryOnly || !_timerListInFlight.summaryOnly) {
      return _timerListInFlight.request;
    }
    return _timerListInFlight.request.then(() => _fetchTimerList(false));
  }
  const request = Promise.resolve().then(() => Api.timer.list(summaryOnly));
  _timerListInFlight = { request, summaryOnly };
  const settled = () => {
    if (!_timerListInFlight || _timerListInFlight.request !== request) return;
    _timerListInFlight = null;
    if (_timerRefreshAfterFlight) {
      _timerRefreshAfterFlight = false;
      _scheduleTimerRefresh();
    }
  };
  request.then(settled, settled);
  return request;
}

function _updateTimerBadge(data) {
  if (!data || !data.ok) return;
  const timers = Array.isArray(data.timers) ? data.timers : [];
  const hasTimers = Array.isArray(data.timers)
    ? timers.length > 0
    : !!data.has_timers;
  const activeCount = data.active_count || 0;
  const badge = document.getElementById("timerBadge");
  const countEl = document.getElementById("timerCount");
  if (badge) badge.style.display = hasTimers ? "inline-flex" : "none";
  if (countEl) {
    if (activeCount > 0) {
      countEl.textContent = activeCount;
      countEl.style.display = "inline-flex";
    } else {
      countEl.style.display = "none";
    }
  }
}

// ── Toggle panel visibility ──
function toggleTimerPanel(e) {
  const panel = document.getElementById("timerPanel");
  if (!panel) return;
  // The panel is a DOM descendant of the badge, so clicks (and the click
  // that ends a text-selection drag) inside the panel bubble up to this
  // onclick handler. Without this guard they would flip the panel shut.
  if (e && panel.contains(/** @type {Node} */ (e.target))) return;
  if (e) e.stopPropagation();
  _timerPanelOpen = !_timerPanelOpen;
  panel.classList.toggle("visible", _timerPanelOpen);
  if (_timerPanelOpen) _refreshTimerPanel();
}

// ── Refresh panel data from API ──
async function _refreshTimerPanel() {
  try {
    const data = await _fetchTimerList(false);
    if (!data || !data.ok) return;

    const timers = data.timers || [];
    const content = document.getElementById("timerPanelContent");
    _updateTimerBadge(data);

    if (!content) return;
    _timerLastTimers = timers;
    _renderTimerList(timers);
  } catch (e) {
    console.warn("[Timer] Panel refresh failed:", e);
  }
}

/* Render the panel rows from a timer list (extracted seam — the refresh
   path AND the pending re-render both ride it). Rows with an in-flight
   operation paint the busy label INSTEAD of their action buttons. */
function _renderTimerList(timers) {
  const content = document.getElementById("timerPanelContent");
  if (!content) return;

  if (timers.length === 0) {
    content.innerHTML = '<div class="timer-panel-empty">' + t('timer.empty') + '</div>';
    return;
  }

  const _jumpHint = t('timer.jumpHint');
  const _cancellingLabel = t('timer.cancelling');
  const _triggeringLabel = t('timer.triggering');
  let html = "";
  for (const t of timers) {
      const statusIcon = { active: IconDot('green'), triggered: Icon('alarm', 12), cancelled: IconDot('red'), exhausted: IconDot('grey') }[t.status] || IconDot('grey');
      const statusClass = t.status;
      const pollAt = t.last_poll_at ? new Date(t.last_poll_at).toLocaleTimeString() : "never";
      const decLabel = t.last_poll_decision ? t.last_poll_decision.toUpperCase() : "—";
      const decClass = t.last_poll_decision || "wait";
      const maxPolls = t.max_polls > 0 ? ` / ${t.max_polls}` : "";
      const checkCmd = t.check_command ? escapeHtml(t.check_command.slice(0, 60)) : "(none)";
      const created = t.created_at ? new Date(t.created_at).toLocaleString() : "?";
      const convId = t.conv_id || "";

      html += `<div class="timer-panel-item timer-status-${statusClass}">
        <div class="tpi-header${convId ? ' tpi-header-jump' : ''}" ${convId ? `data-tofu-action="_jumpToTimerConv('${convId}', event)" title="${escapeHtml(_jumpHint)}"` : ''}>
          <span class="tpi-status">${statusIcon}</span>
          <span class="tpi-id">${escapeHtml(t.id)}</span>
          <span class="tpi-status-label">${t.status}</span>
        </div>
        <div class="tpi-meta">
          ${Icon('chart', 11, 'opacity:.7')} Polls: ${t.poll_count}${maxPolls} | Interval: ${t.poll_interval}s<br>
          ${Icon('clock', 11, 'opacity:.7')} Last poll: ${pollAt} <span class="tpi-decision ${decClass}">${decLabel}</span><br>
          ${t.last_poll_reason ? `${Icon('messageCircle', 11, 'opacity:.7')} ${escapeHtml(t.last_poll_reason.slice(0, 80))}<br>` : ""}
          ${Icon('search', 11, 'opacity:.7')} Check cmd: <code>${checkCmd}</code><br>
          ${Icon('edit', 11, 'opacity:.7')} Check: ${escapeHtml((t.check_instruction || "").slice(0, 80))}${(t.check_instruction || "").length > 80 ? "…" : ""}<br>
          ${Icon('mapPin', 11, 'opacity:.7')} Conv: ${(t.conv_id || "?").slice(0, 12)}… | Created: ${created}
        </div>`;

      if (t.triggered_at) {
        html += `<div class="tpi-triggered">${Icon('alarm', 12)} Triggered: ${new Date(t.triggered_at).toLocaleString()}</div>`;
      }

      // Action buttons — replaced by the busy label while an operation
      // on THIS row is in flight (click frame → background completion).
      const _pending = _timerPending[t.id];
      if (_pending) {
        html += `<div class="tpi-actions"><span class="tpi-pending">${escapeHtml(_pending === 'cancelling' ? _cancellingLabel : _triggeringLabel)}</span></div></div>`;
      } else {
        html += `<div class="tpi-actions">
          <button data-tofu-action="_viewTimerLog('${t.id}')" class="tpi-btn tpi-btn-log" title="View poll log">${Icon('clipboard', 11)} Log</button>`;
        if (t.status === "active") {
          html += `
          <button data-tofu-action="_triggerTimer('${t.id}')" class="tpi-btn tpi-btn-trigger" title="Force trigger now">▶ Trigger</button>
          <button data-tofu-action="_cancelTimer('${t.id}')" class="tpi-btn tpi-btn-cancel" title="Cancel timer">✖ Cancel</button>`;
        }
        html += `</div></div>`;
      }
    }
    content.innerHTML = html;
}

// ── Actions ──
async function _triggerTimer(timerId) {
  /* INSTANT-UI (): the row shows 触发中… on the CLICK
   *   frame; the POST + refresh run in the background. */
  _timerSetPending(timerId, 'triggering');
  try {
    const data = await Api.timer.trigger(timerId);
    if (data && data.ok) {
      _timerClearPending(timerId, true);   // the refresh renders the fresh state
      debugLog(`⏱️ Timer ${timerId} triggered! Execution: ${data.execution_task_id}`, "success");
      await _refreshTimerPanel();
    } else {
      _timerClearPending(timerId);       // restore the row
      debugLog(`⏱️ Trigger failed: ${data && data.error}`, "error");
    }
  } catch (e) {
    _timerClearPending(timerId);         // restore the row
    debugLog(`⏱️ Trigger error: ${e.message}`, "error");
  }
}

async function _cancelTimer(timerId) {
  /* INSTANT-UI (): the row shows 取消中… on the CLICK
   *   frame; the POST + refresh run in the background. */
  _timerSetPending(timerId, 'cancelling');
  try {
    const data = await Api.timer.cancel(timerId);
    /* Api.timer.cancel deliberately maps HTTP failures to null so a rejected
     * request does not create an unhandled click-handler promise. Treat that
     * sentinel (and an explicit {ok:false}) as failure here; otherwise the UI
     * would announce cancellation even though the server never accepted it. */
    if (!data || data.ok === false) {
      _timerClearPending(timerId);
      debugLog(`⏱️ Cancel failed: ${data && data.error}`, "error");
      return;
    }
    _timerClearPending(timerId, true);   // the refresh renders the fresh state
    debugLog(`⏱️ Timer ${timerId} cancelled.`, "info");
    await _refreshTimerPanel();
  } catch (e) {
    _timerClearPending(timerId);         // restore the row
    debugLog(`⏱️ Cancel error: ${e.message}`, "error");
  }
}

// ── Jump to the conversation that owns a timer ──
function _jumpToTimerConv(convId, e) {
  if (e && e.stopPropagation) e.stopPropagation();
  if (!convId) return;
  // Close the panel first so the chat view is unobstructed.
  _timerPanelOpen = false;
  const panel = document.getElementById("timerPanel");
  if (panel) panel.classList.remove("visible");

  const conv = getConvById(convId);
  if (!conv) {
    if (typeof showToast === "function") {
      showToast(t('timer.convMissing'), "warning");
    }
    return;
  }
  if (typeof loadConversation === "function") loadConversation(convId);
}

// ── Show the poll log in a visible modal (was console-only) ──
async function _viewTimerLog(timerId) {
  let data;
  try {
    data = await Api.timer.status(timerId, 30);
  } catch (e) {
    debugLog(`⏱️ Log error: ${e.message}`, "error");
    if (typeof showToast === "function") showToast(t('timer.logError'), "error");
    return;
  }
  const entries = (data && data.ok && Array.isArray(data.poll_log)) ? data.poll_log : [];

  // Remove any existing log dialog before opening a new one.
  const existing = document.getElementById("_timerLogDialog");
  if (existing) existing.remove();

  let rowsHtml;
  if (entries.length === 0) {
    rowsHtml = `<div class="timer-log-empty">${escapeHtml(t('timer.logEmpty'))}</div>`;
  } else {
    rowsHtml = entries.map((entry) => {
      const time = entry.poll_time ? new Date(entry.poll_time).toLocaleString() : "?";
      const dec = entry.decision || "wait";
      const icon = dec === "ready" ? Icon('check', 12)
        : dec === "wait" ? Icon('hourglass', 12)
        : Icon('ban', 12);
      const reason = entry.reason ? escapeHtml(entry.reason) : `(${t('timer.noReason')})`;
      const tokens = entry.tokens_used != null ? `${entry.tokens_used} tok` : "";
      const model = entry.model ? ` · ${escapeHtml(entry.model)}` : "";
      const cmdOut = entry.check_output
        ? `<div class="tl-cmd">${escapeHtml(String(entry.check_output).slice(0, 600))}</div>`
        : "";
      return `<div class="timer-log-row tl-${dec}">
        <div class="tl-head">
          <span class="tl-icon">${icon}</span>
          <span class="tl-decision">${dec.toUpperCase()}</span>
          <span class="tl-time">${escapeHtml(time)}</span>
          <span class="tl-tokens">${escapeHtml(tokens)}${model}</span>
        </div>
        <div class="tl-reason">${reason}</div>
        ${cmdOut}
      </div>`;
    }).join("");
  }

  const overlay = document.createElement("div");
  overlay.id = "_timerLogDialog";
  overlay.className = "timer-log-overlay";
  overlay.innerHTML = `
    <div class="timer-log-card" role="dialog" aria-modal="true">
      <div class="timer-log-head">
        <span class="timer-log-title">${Icon('clipboard', 15, 'vertical-align:-2px')} ${escapeHtml(t('timer.logTitle'))} <code>${escapeHtml(timerId)}</code></span>
        <button class="timer-log-close" id="_timerLogClose" aria-label="close">${Icon('x', 14)}</button>
      </div>
      <div class="timer-log-body">${rowsHtml}</div>
    </div>`;
  document.body.appendChild(overlay);

  function _close() { overlay.remove(); document.removeEventListener("keydown", _onKey); }
  function _onKey(ev) { if (ev.key === "Escape") _close(); }
  document.getElementById("_timerLogClose").addEventListener("click", _close);
  overlay.addEventListener("click", (ev) => { if (ev.target === overlay) _close(); });
  document.addEventListener("keydown", _onKey);
}

function _refreshVisibleTimerUi() {
  if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
  if (_timerPanelOpen) _refreshTimerPanel();
  else _refreshTimerBadge();
}

function _scheduleTimerRefresh() {
  if (_timerListInFlight) {
    _timerRefreshAfterFlight = true;
    return;
  }
  if (_timerRefreshTimer) return;
  _timerRefreshTimer = setTimeout(() => {
    _timerRefreshTimer = null;
    _refreshVisibleTimerUi();
  }, 100);
}

/* Normal path: one lightweight invalidation over the already-open push socket.
 * `progress` changes cannot alter a closed panel's badge, so they cause no DB
 * read until the panel is visible. Terminal/create changes refresh the badge. */
function _wireTimerPush() {
  if (_timerPushWired || typeof pushSubscribe !== "function") return;
  _timerPushWired = true;
  pushSubscribe("timer", "*", (frame) => {
    if (!frame || frame.type !== "timer_changed") return;
    if (frame.change === "progress" && !_timerPanelOpen) return;
    _scheduleTimerRefresh();
  });
  if (typeof pushOnReconnect === "function") {
    pushOnReconnect(_scheduleTimerRefresh);
  }
}

// ── Push-first refresh with visibility-aware polling fallback ──
function _startTimerPolling() {
  if (_timerPollInterval) return;
  _timerPollInterval = setInterval(() => {
    if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
    // An open panel needs progress details. A closed panel is event-driven
    // while push is healthy and polls only when the socket is unavailable.
    if (_timerPanelOpen) _refreshTimerPanel();
    else if (typeof pushIsConnected !== "function" || !pushIsConnected()) {
      _refreshTimerBadge();
    }
  }, 30000); // every 30s
}

async function _refreshTimerBadge() {
  // Scheduler is a default (always-on) tool — the timer badge surfaces
  // whenever any timer exists, no per-conversation toggle gate.
  try {
    const data = await _fetchTimerList(true);
    if (!data || !data.ok) return;
    _updateTimerBadge(data);
  } catch (e) {
    // silent — badge refresh is best-effort
  }
}

// Allow mobile_panels.js to keep the open-flag in sync when it portals the
// panel to <body> as a bottom sheet.
if (typeof window !== "undefined") {
  runtimeScope._setTimerPanelOpen = function (v) { _timerPanelOpen = !!v; };
}

// Close panel on outside click
document.addEventListener("click", (e) => {
  if (!_timerPanelOpen) return;
  const panel = document.getElementById("timerPanel");
  // On mobile the panel is portaled out of the badge into <body>; its own
  // backdrop (mobile_panels.js) owns closing, so skip the badge-based check.
  if (panel && panel.classList.contains("mobile-panel-portaled")) return;
  const badge = document.getElementById("timerBadge");
  if (badge && !badge.contains(/** @type {Node} */ (e.target))) {
    _timerPanelOpen = false;
    if (panel) panel.classList.remove("visible");
  }
});

// Start polling on load
_wireTimerPush();
_startTimerPolling();
// Initial badge check
setTimeout(_refreshTimerBadge, 3000);

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") _scheduleTimerRefresh();
});


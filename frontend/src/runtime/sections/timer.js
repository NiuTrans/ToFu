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
  if (e && (panel.contains(/** @type {Node} */ (e.target))
      || e.composedPath?.().includes(panel))) return;
  if (e) e.stopPropagation();
  _timerPanelOpen = !_timerPanelOpen;
  panel.classList.toggle("visible", _timerPanelOpen);
  if (_timerPanelOpen) _refreshTimerPanel();
}

// ── Refresh panel data from API ──
async function _refreshTimerPanel(afterMutation = false) {
  try {
    // A read admitted before a successful mutation cannot acknowledge it.
    // Drain that read (including failure), then join/start a fresh one through
    // the same single-flight owner used by push and background refreshes.
    if (afterMutation && _timerListInFlight) {
      await _timerListInFlight.request.catch(() => null);
    }
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

/* "7h 19m"-style compact duration from creation to the terminal timestamp
   (or now for live timers) — answers "how long has this been running?". */
function _timerElapsed(timer) {
  const start = timer.created_at ? new Date(timer.created_at).getTime() : NaN;
  if (!Number.isFinite(start)) return "?";
  const end = timer.triggered_at ? new Date(timer.triggered_at).getTime() : Date.now();
  if (!Number.isFinite(end) || end <= start) return "0m";
  let s = Math.floor((end - start) / 1000);
  const d = Math.floor(s / 86400);
  s -= d * 86400;
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return `${s}s`;
}

/* Render the panel rows from a timer list (extracted seam — the refresh
   path AND the pending re-render both ride it). Rows with an in-flight
   operation paint the busy label INSTEAD of their action buttons.
   Card layout is two-tier by design: the header/headline/progress answer
   "what is this and what happened"; raw record fields (id, command, conv,
   created) live behind the <details> expander for debugging only. */
function _renderTimerList(timers) {
  const content = document.getElementById("timerPanelContent");
  if (!content) return;

  if (timers.length === 0) {
    content.innerHTML = '<div class="timer-panel-empty">' + t('timer.empty') + '</div>';
    return;
  }

  /* The loop variable is named `t`, which shadows the i18n function — every
     translated label (and every parameterized template) must be captured
     here, before the loop. */
  const _jumpHint = t('timer.jumpHint');
  const _cancellingLabel = t('timer.cancelling');
  const _triggeringLabel = t('timer.triggering');
  const _statusLabels = {
    active: t('timer.statusActive'),
    triggered: t('timer.statusTriggered'),
    cancelled: t('timer.statusCancelled'),
    exhausted: t('timer.statusExhausted'),
    expired: t('timer.statusExpired'),
    failed: t('timer.statusFailed'),
  };
  const _decisionLabels = {
    ready: t('timer.decisionReady'),
    wait: t('timer.decisionWait'),
    error: t('timer.decisionError'),
    parse_error: t('timer.decisionError'),
    dispatch_failed: t('timer.decisionError'),
  };
  const _progressLine = (count, max, interval, elapsed) =>
    t('timer.progress', { count, max, interval, elapsed });
  const _lastPollAt = (time) => t('timer.lastPollAt', { time });
  const _triggeredAt = (time) => t('timer.triggeredAt', { time });
  const _detailsLabel = t('timer.details');
  const _fieldId = t('timer.fieldId');
  const _fieldCheck = t('timer.fieldCheck');
  const _fieldCheckCmd = t('timer.fieldCheckCmd');
  const _fieldPredicate = t('timer.fieldPredicate');
  const _fieldContinuation = t('timer.fieldContinuation');
  const _dispatchPending = t('timer.statusDispatchPending');
  const _kindLabels = { code: t('timerBlock.kindCode'), hybrid: t('timerBlock.kindHybrid'), llm: t('timer.kindLLM') };
  const _fieldConv = t('timer.fieldConv');
  const _fieldCreated = t('timer.fieldCreated');
  const _cmdNone = t('timer.cmdNone');
  const _logBtn = t('timer.logBtn');
  const _logHint = t('timer.logHint');
  const _triggerBtn = t('timer.triggerBtn');
  const _triggerHint = t('timer.triggerHint');
  const _cancelBtn = t('timer.cancelBtn');
  const _cancelHint = t('timer.cancelHint');

  let html = "";
  for (const t of timers) {
      const statusIcon = { active: IconDot('green'), triggered: Icon('alarm', 12), cancelled: IconDot('red'), exhausted: IconDot('grey'), failed: IconDot('red'), expired: IconDot('grey') }[t.status] || IconDot('grey');
      const statusClass = t.status;
      const dispatchPending = t.status === "active" && t.last_poll_decision === "ready";
      const statusLabel = dispatchPending ? _dispatchPending : (_statusLabels[t.status] || t.status);
      const instruction = (t.check_instruction || "").trim();
      const title = instruction || (t.condition_command || "").trim() || t.id;
      const created = t.created_at ? new Date(t.created_at).toLocaleString() : "?";
      const convId = t.conv_id || "";

      const decKey = t.last_poll_decision || "";
      const decClass = ["error", "parse_error", "dispatch_failed"].includes(decKey) ? "error" : decKey;
      const decChip = decKey
        ? `<span class="tpi-decision ${decClass}">${escapeHtml(_decisionLabels[decKey] || decKey.toUpperCase())}</span> `
        : "";

      /* One compact progress line; a fired timer's triggered_at IS its last
         poll, so the two timestamps are never shown side by side. */
      const maxPolls = t.max_polls > 0 ? ` / ${t.max_polls}` : "";
      let progressLine = _progressLine(t.poll_count, maxPolls, t.poll_interval, _timerElapsed(t));
      progressLine += ` · ${_kindLabels[t.condition_kind] || _kindLabels.llm}`;
      if (t.last_poll_at && !t.triggered_at) {
        progressLine += _lastPollAt(new Date(t.last_poll_at).toLocaleTimeString());
      }
      const pct = (t.status === "active" && t.max_polls > 0)
        ? Math.max(0, Math.min(100, Math.round((100 * t.poll_count) / t.max_polls)))
        : null;

      html += `<div class="timer-panel-item timer-status-${statusClass}">
        <div class="tpi-header${convId ? ' tpi-header-jump' : ''}" ${convId ? `data-tofu-action="_jumpToTimerConv('${convId}', event)" title="${escapeHtml(_jumpHint)}"` : ''}>
          <span class="tpi-status">${statusIcon}</span>
          <span class="tpi-title" title="${escapeHtml(instruction)}">${escapeHtml(title)}</span>
          <span class="tpi-status-label">${escapeHtml(statusLabel)}</span>
        </div>`;

      if (t.last_poll_reason || decChip) {
        html += `<div class="tpi-headline">${decChip}${escapeHtml((t.last_poll_reason || "").slice(0, 140))}</div>`;
      }
      html += `<div class="tpi-progress-line">${escapeHtml(progressLine)}</div>`;
      if (pct !== null) {
        html += `<div class="tpi-progress-bar"><div style="width:${pct}%"></div></div>`;
      }
      if (t.triggered_at) {
        html += `<div class="tpi-triggered">${Icon('alarm', 12)} ${escapeHtml(_triggeredAt(new Date(t.triggered_at).toLocaleString()))}</div>`;
      }

      html += `<details class="tpi-details">
        <summary>${escapeHtml(_detailsLabel)}</summary>
        <div class="tpi-detail-rows">
          <div>${escapeHtml(_fieldId)}: <code>${escapeHtml(t.id)}</code></div>
          <div>${escapeHtml(_fieldCheck)}: ${escapeHtml(instruction || "—")}</div>
          <div>${escapeHtml(_fieldCheckCmd)}: <code>${escapeHtml(t.check_command ? t.check_command.slice(0, 120) : _cmdNone)}</code></div>
          <div>${escapeHtml(_fieldPredicate)}: <code>${escapeHtml(t.condition_command || _cmdNone)}</code></div>
          <div>${escapeHtml(_fieldContinuation)}: ${escapeHtml(t.continuation_message || "—")}</div>
          <div>${escapeHtml(_fieldConv)}: <code>${escapeHtml(convId || "?")}</code></div>
          <div>${escapeHtml(_fieldCreated)}: ${escapeHtml(created)}</div>
        </div>
      </details>`;

      // Action buttons — replaced by the busy label while an operation
      // on THIS row is in flight (click frame → background completion).
      const _pending = _timerPending[t.id];
      if (_pending) {
        html += `<div class="tpi-actions"><span class="tpi-pending">${escapeHtml(_pending === 'cancelling' ? _cancellingLabel : _triggeringLabel)}</span></div></div>`;
      } else {
        html += `<div class="tpi-actions">
          <button data-tofu-action="_viewTimerLog('${t.id}')" class="tpi-btn tpi-btn-log" title="${escapeHtml(_logHint)}">${Icon('clipboard', 11)} ${escapeHtml(_logBtn)}</button>`;
        if (t.status === "active") {
          html += `
          <button data-tofu-action="_triggerTimer('${t.id}')" class="tpi-btn tpi-btn-trigger" title="${escapeHtml(_triggerHint)}">▶ ${escapeHtml(_triggerBtn)}</button>
          <button data-tofu-action="_cancelTimer('${t.id}')" class="tpi-btn tpi-btn-cancel" title="${escapeHtml(_cancelHint)}">✖ ${escapeHtml(_cancelBtn)}</button>`;
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
      debugLog(`⏱️ Timer ${timerId} triggered! Execution: ${data.execution_task_id}`, "success");
      await _refreshTimerPanel(true);
      _timerClearPending(timerId);
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
    debugLog(`⏱️ Timer ${timerId} cancelled.`, "info");
    await _refreshTimerPanel(true);
    _timerClearPending(timerId);
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

/* The Android WebView reports visible while backgrounded; the shell's
   nativeVisibility bridge is the only reliable pocket signal there. */
function _timerShellHidden() {
  return typeof runtimeScope !== "undefined"
    && runtimeScope.nativeVisibility?.isHidden() === true;
}

function _refreshVisibleTimerUi() {
  if ((typeof document !== "undefined" && document.visibilityState === "hidden")
      || _timerShellHidden()) return;
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
    if ((typeof document !== "undefined" && document.visibilityState === "hidden")
        || _timerShellHidden()) return;
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
  // A row action synchronously replaces its button with the pending label.
  // The original event path still proves the click came from inside the badge
  // even though its detached target no longer passes contains().
  if (badge && !badge.contains(/** @type {Node} */ (e.target))
      && !e.composedPath?.().includes(badge)) {
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
  if (document.visibilityState === "visible" && !_timerShellHidden()) {
    _scheduleTimerRefresh();
  }
});


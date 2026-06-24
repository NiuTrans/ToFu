/* ═══════════════════════════════════════════
   timer.js — Timer Watcher panel & badge
   ═══════════════════════════════════════════ */

let _timerPanelOpen = false;
let _timerPollInterval = null;

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
    const data = await Api.timer.list();
    if (!data || !data.ok) return;

    const timers = data.timers || [];
    const activeCount = data.active_count || 0;
    const content = document.getElementById("timerPanelContent");
    const badge = document.getElementById("timerBadge");
    const countEl = document.getElementById("timerCount");

    // Update badge visibility & count (only when scheduler is enabled)
    if (badge) {
      badge.style.display = (schedulerEnabled && timers.length > 0) ? "inline-flex" : "none";
    }
    if (countEl) {
      if (activeCount > 0) {
        countEl.textContent = activeCount;
        countEl.style.display = "inline-flex";
      } else {
        countEl.style.display = "none";
      }
    }

    if (!content) return;

    if (timers.length === 0) {
      content.innerHTML = '<div class="timer-panel-empty">' + t('timer.empty') + '</div>';
      return;
    }

    const _jumpHint = t('timer.jumpHint');
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
        <div class="tpi-header${convId ? ' tpi-header-jump' : ''}" ${convId ? `onclick="_jumpToTimerConv('${convId}', event)" title="${escapeHtml(_jumpHint)}"` : ''}>
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

      // Action buttons (only for active timers)
      html += `<div class="tpi-actions">
          <button onclick="_viewTimerLog('${t.id}')" class="tpi-btn tpi-btn-log" title="View poll log">${Icon('clipboard', 11)} Log</button>`;
      if (t.status === "active") {
        html += `
          <button onclick="_triggerTimer('${t.id}')" class="tpi-btn tpi-btn-trigger" title="Force trigger now">▶ Trigger</button>
          <button onclick="_cancelTimer('${t.id}')" class="tpi-btn tpi-btn-cancel" title="Cancel timer">✖ Cancel</button>`;
      }
      html += `</div></div>`;
    }
    content.innerHTML = html;
  } catch (e) {
    console.warn("[Timer] Panel refresh failed:", e);
  }
}

// ── Actions ──
async function _triggerTimer(timerId) {
  try {
    const data = await Api.timer.trigger(timerId);
    if (data && data.ok) {
      debugLog(`⏱️ Timer ${timerId} triggered! Execution: ${data.execution_task_id}`, "success");
      _refreshTimerPanel();
    } else {
      debugLog(`⏱️ Trigger failed: ${data && data.error}`, "error");
    }
  } catch (e) {
    debugLog(`⏱️ Trigger error: ${e.message}`, "error");
  }
}

async function _cancelTimer(timerId) {
  try {
    await Api.timer.cancel(timerId);
    debugLog(`⏱️ Timer ${timerId} cancelled.`, "info");
    _refreshTimerPanel();
  } catch (e) {
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

  const conv = typeof conversations !== "undefined"
    ? conversations.find((c) => c.id === convId)
    : null;
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

// ── Auto-refresh when panel is visible + periodic badge update ──
function _startTimerPolling() {
  if (_timerPollInterval) return;
  _timerPollInterval = setInterval(() => {
    // Always refresh badge count (lightweight)
    _refreshTimerBadge();
    // Only refresh full panel if visible
    if (_timerPanelOpen) _refreshTimerPanel();
  }, 30000); // every 30s
}

async function _refreshTimerBadge() {
  // Don't show timer badge or poll API when scheduler is disabled
  if (!schedulerEnabled) {
    const badge = document.getElementById("timerBadge");
    if (badge) badge.style.display = "none";
    return;
  }
  try {
    const data = await Api.timer.list();
    if (!data || !data.ok) return;
    const activeCount = data.active_count || 0;
    const badge = document.getElementById("timerBadge");
    const countEl = document.getElementById("timerCount");
    const timers = data.timers || [];

    if (badge) {
      badge.style.display = timers.length > 0 ? "inline-flex" : "none";
    }
    if (countEl) {
      if (activeCount > 0) {
        countEl.textContent = activeCount;
        countEl.style.display = "inline-flex";
      } else {
        countEl.style.display = "none";
      }
    }
  } catch (e) {
    // silent — badge refresh is best-effort
  }
}

// Close panel on outside click
document.addEventListener("click", (e) => {
  if (!_timerPanelOpen) return;
  const badge = document.getElementById("timerBadge");
  if (badge && !badge.contains(/** @type {Node} */ (e.target))) {
    _timerPanelOpen = false;
    const panel = document.getElementById("timerPanel");
    if (panel) panel.classList.remove("visible");
  }
});

// Start polling on load
_startTimerPolling();
// Initial badge check
setTimeout(_refreshTimerBadge, 3000);

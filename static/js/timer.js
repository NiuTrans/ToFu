/* ═══════════════════════════════════════════
   timer.js — Timer Watcher panel & badge
   ═══════════════════════════════════════════ */

let _timerPanelOpen = false;
let _timerPollInterval = null;

// ── Toggle panel visibility ──
function toggleTimerPanel(e) {
  if (e) e.stopPropagation();
  const panel = document.getElementById("timerPanel");
  if (!panel) return;
  _timerPanelOpen = !_timerPanelOpen;
  panel.classList.toggle("visible", _timerPanelOpen);
  if (_timerPanelOpen) _refreshTimerPanel();
}

// ── Refresh panel data from API ──
async function _refreshTimerPanel() {
  try {
    const resp = await fetch(apiUrl("/api/timer/list"));
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data.ok) return;

    const timers = data.timers || [];
    const activeCount = data.active_count || 0;
    const content = document.getElementById("timerPanelContent");
    const badge = document.getElementById("timerBadge");
    const countEl = document.getElementById("timerCount");

    // Update badge visibility & count
    if (badge) {
      if (timers.length > 0) {
        badge.style.display = "inline-flex";
      }
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
      content.innerHTML = '<div class="timer-panel-empty">No timers. The AI can create one with timer_create during a task.</div>';
      return;
    }

    let html = "";
    for (const t of timers) {
      const statusIcon = { active: "🟢", triggered: "⏰", cancelled: "🔴", exhausted: "⚪" }[t.status] || "❓";
      const statusClass = t.status;
      const pollAt = t.last_poll_at ? new Date(t.last_poll_at).toLocaleTimeString() : "never";
      const decLabel = t.last_poll_decision ? t.last_poll_decision.toUpperCase() : "—";
      const decClass = t.last_poll_decision || "wait";
      const maxPolls = t.max_polls > 0 ? ` / ${t.max_polls}` : "";
      const checkCmd = t.check_command ? escapeHtml(t.check_command.slice(0, 60)) : "(none)";
      const created = t.created_at ? new Date(t.created_at).toLocaleString() : "?";

      html += `<div class="timer-panel-item timer-status-${statusClass}">
        <div class="tpi-header">
          <span class="tpi-status">${statusIcon}</span>
          <span class="tpi-id">${escapeHtml(t.id)}</span>
          <span class="tpi-status-label">${t.status}</span>
        </div>
        <div class="tpi-meta">
          📊 Polls: ${t.poll_count}${maxPolls} | Interval: ${t.poll_interval}s<br>
          🕐 Last poll: ${pollAt} <span class="tpi-decision ${decClass}">${decLabel}</span><br>
          ${t.last_poll_reason ? `💭 ${escapeHtml(t.last_poll_reason.slice(0, 80))}<br>` : ""}
          🔍 Check cmd: <code>${checkCmd}</code><br>
          📝 Check: ${escapeHtml((t.check_instruction || "").slice(0, 80))}${(t.check_instruction || "").length > 80 ? "…" : ""}<br>
          📌 Conv: ${(t.conv_id || "?").slice(0, 12)}… | Created: ${created}
        </div>`;

      if (t.triggered_at) {
        html += `<div class="tpi-triggered">⏰ Triggered: ${new Date(t.triggered_at).toLocaleString()}</div>`;
      }

      // Action buttons (only for active timers)
      html += `<div class="tpi-actions">
          <button onclick="_viewTimerLog('${t.id}')" class="tpi-btn tpi-btn-log" title="View poll log">📋 Log</button>`;
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
    const resp = await fetch(apiUrl(`/api/timer/${timerId}/trigger`), { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      debugLog(`⏱️ Timer ${timerId} triggered! Execution: ${data.execution_task_id}`, "success");
      _refreshTimerPanel();
    } else {
      debugLog(`⏱️ Trigger failed: ${data.error}`, "error");
    }
  } catch (e) {
    debugLog(`⏱️ Trigger error: ${e.message}`, "error");
  }
}

async function _cancelTimer(timerId) {
  try {
    await fetch(apiUrl(`/api/timer/${timerId}/cancel`), { method: "POST" });
    debugLog(`⏱️ Timer ${timerId} cancelled.`, "info");
    _refreshTimerPanel();
  } catch (e) {
    debugLog(`⏱️ Cancel error: ${e.message}`, "error");
  }
}

async function _viewTimerLog(timerId) {
  try {
    const resp = await fetch(apiUrl(`/api/timer/${timerId}/status?limit=20`));
    const data = await resp.json();
    if (!data.ok || !data.poll_log || data.poll_log.length === 0) {
      debugLog("⏱️ No poll log entries yet.", "info");
      return;
    }
    let msg = `⏱️ Timer ${timerId} Poll Log (newest first):\n`;
    for (const entry of data.poll_log) {
      const time = new Date(entry.poll_time).toLocaleString();
      const icon = entry.decision === "ready" ? "✅" : entry.decision === "wait" ? "⏳" : "❌";
      msg += `${icon} ${time} — ${entry.decision.toUpperCase()} — ${entry.reason || "(no reason)"} (${entry.tokens_used} tokens)\n`;
    }
    debugLog(msg, "info");
  } catch (e) {
    debugLog(`⏱️ Log error: ${e.message}`, "error");
  }
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
  try {
    const resp = await fetch(apiUrl("/api/timer/list"));
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data.ok) return;
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
  if (_timerPanelOpen) {
    const badge = document.getElementById("timerBadge");
    if (badge && !badge.contains(e.target)) {
      _timerPanelOpen = false;
      const panel = document.getElementById("timerPanel");
      if (panel) panel.classList.remove("visible");
    }
  }
});

// Start polling on load
_startTimerPolling();
// Initial badge check
setTimeout(_refreshTimerBadge, 3000);

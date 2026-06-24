/* ═══════════════════════════════════════════
   scheduler.js — Scheduler (Timed / Cross-conversation tasks)
   ═══════════════════════════════════════════ */
// ══════════════════════════════════════════════════════
// ★ Scheduler (Timed / Cross-conversation tasks)
// ══════════════════════════════════════════════════════
function toggleScheduler() {
  _applySchedulerUI(!schedulerEnabled);
  _saveConvToolState();
  debugLog(`Scheduler: ${schedulerEnabled ? "ON — AI can create/manage scheduled & cross-conversation tasks" : "OFF"}`, "success");
  if (schedulerEnabled) _refreshSchedulerPanel();
}

let _schedulerPanelOpen = false;
let _schedulerPollTimer = null;

function toggleSchedulerPanel(e) {
  // Don't toggle scheduler on/off when clicking the badge to open panel
  if (e) e.stopPropagation();
  const panel = document.getElementById("schedulerPanel");
  if (!panel) return;
  _schedulerPanelOpen = !_schedulerPanelOpen;
  panel.classList.toggle("visible", _schedulerPanelOpen);
  if (_schedulerPanelOpen) _refreshSchedulerPanel();
}

async function _refreshSchedulerPanel() {
  try {
    const data = await Api.scheduler.proactiveStatus();
    if (!data || !data.ok) return;
    const info = data.proactive;
    const content = document.getElementById("schedulerPanelContent");
    const countEl = document.getElementById("proactiveCount");
    if (!content) return;

    // Update badge count
    if (countEl) {
      if (info.active > 0) {
        countEl.textContent = info.active;
        countEl.style.display = "inline-flex";
      } else {
        countEl.style.display = "none";
      }
    }

    if (!info.tasks || info.tasks.length === 0) {
      content.innerHTML = '<div class="scheduler-panel-empty">No proactive tasks. Enable Scheduler and ask the AI to create one.</div>';
      return;
    }

    let html = "";
    for (const t of info.tasks) {
      const statusIcon = t.enabled
        ? '<svg width="9" height="9" viewBox="0 0 24 24" fill="#22c55e" style="vertical-align:middle"><circle cx="12" cy="12" r="10"/></svg>'
        : '<svg width="9" height="9" viewBox="0 0 24 24" fill="#ef4444" style="vertical-align:middle"><circle cx="12" cy="12" r="10"/></svg>';
      const decClass = t.last_poll_decision || "skip";
      const decLabel = t.last_poll_decision ? t.last_poll_decision.toUpperCase() : "—";
      const pollAt = t.last_poll_at ? new Date(t.last_poll_at).toLocaleTimeString() : "never";
      const execAt = t.last_execution_at ? new Date(t.last_execution_at).toLocaleTimeString() : "never";
      const maxExec = t.max_executions > 0 ? ` / ${t.max_executions}` : "";

      const _spiIco = (inner) => `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;opacity:.7">${inner}</svg>`;
      const _icoPolls = _spiIco('<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>');
      const _icoClock = _spiIco('<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>');
      const _icoReason = _spiIco('<path d="M2.992 16.342a2 2 0 0 1 .094 1.167l-1.065 3.29a1 1 0 0 0 1.236 1.168l3.413-.998a2 2 0 0 1 1.099.092 10 10 0 1 0-4.777-4.719"/>');
      const _icoExec = _spiIco('<path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/>');
      const _icoConv = _spiIco('<path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/>');
      html += `<div class="scheduler-panel-item">
        <div class="spi-name">${statusIcon} ${escapeHtml(t.name)}</div>
        <div class="spi-meta">
          ${_icoPolls} Polls: ${t.poll_count} | Executions: ${t.execution_count}${maxExec}<br>
          ${_icoClock} Last poll: ${pollAt} <span class="spi-decision ${decClass}">${decLabel}</span><br>
          ${t.last_poll_reason ? `${_icoReason} ${escapeHtml(t.last_poll_reason.slice(0, 80))}<br>` : ""}
          ${_icoExec} Last exec: ${execAt} ${t.last_execution_status ? `(${t.last_execution_status})` : ""}<br>
          ${_icoConv} Conv: ${(t.target_conv_id || "?").slice(0, 12)}
        </div>
        <div style="margin-top:4px;display:flex;gap:4px">
          <button onclick="_triggerProactiveTask('${t.id}')" style="font-size:9px;padding:2px 8px;border-radius:4px;border:1px solid rgba(168,85,247,0.3);background:rgba(168,85,247,0.1);color:#a855f7;cursor:pointer" title="Force execute now">▶ Trigger</button>
          <button onclick="_viewPollLog('${t.id}')" style="font-size:9px;padding:2px 8px;border-radius:4px;border:1px solid var(--border);background:transparent;color:var(--text-secondary);cursor:pointer" title="View poll log"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px"><rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4"/><path d="M12 16h4"/><path d="M8 11h.01"/><path d="M8 16h.01"/></svg> Log</button>
          ${t.enabled
            ? `<button onclick="_pauseProactiveTask('${t.id}')" style="font-size:9px;padding:2px 8px;border-radius:4px;border:1px solid rgba(250,204,21,0.3);background:rgba(250,204,21,0.1);color:#facc15;cursor:pointer">⏸ Pause</button>`
            : `<button onclick="_resumeProactiveTask('${t.id}')" style="font-size:9px;padding:2px 8px;border-radius:4px;border:1px solid rgba(34,197,94,0.3);background:rgba(34,197,94,0.1);color:#22c55e;cursor:pointer">▶ Resume</button>`
          }
        </div>
      </div>`;
    }
    content.innerHTML = html;
  } catch (e) {
    console.warn("[Scheduler] Panel refresh failed:", e);
  }
}

async function _triggerProactiveTask(taskId) {
  try {
    const data = await Api.scheduler.triggerTask(taskId);
    if (data && data.ok) {
      debugLog(`⏰ Proactive task triggered! Execution: ${data.execution_task_id}`, "success");
      _refreshSchedulerPanel();
    } else {
      debugLog(`⏰ Trigger failed: ${data && data.error}`, "error");
    }
  } catch (e) {
    debugLog(`⏰ Trigger error: ${e.message}`, "error");
  }
}

async function _pauseProactiveTask(taskId) {
  try {
    await Api.scheduler.pauseTask(taskId);
    _refreshSchedulerPanel();
  } catch (e) { console.warn("[Scheduler] pause failed:", e); }
}

async function _resumeProactiveTask(taskId) {
  try {
    await Api.scheduler.resumeTask(taskId);
    _refreshSchedulerPanel();
  } catch (e) { console.warn("[Scheduler] resume failed:", e); }
}

async function _viewPollLog(taskId) {
  try {
    const data = await Api.scheduler.pollLog(taskId, 20);
    if (!data || !data.ok || !data.poll_log || data.poll_log.length === 0) {
      debugLog("⏰ No poll log entries yet.", "info");
      return;
    }
    let msg = "⏰ Poll Log (newest first):\n";
    for (const entry of data.poll_log) {
      const time = new Date(entry.poll_time).toLocaleString();
      const icon = entry.decision === "act" ? "✅" : entry.decision === "skip" ? "⏭️" : "❌";
      msg += `${icon} ${time} — ${entry.decision.toUpperCase()} — ${entry.reason || "(no reason)"}\n`;
      if (entry.execution_task_id) msg += `   → exec: ${entry.execution_task_id.slice(0, 12)}\n`;
    }
    debugLog(msg, "info");
  } catch (e) {
    debugLog(`⏰ Poll log error: ${e.message}`, "error");
  }
}

// Auto-refresh scheduler panel periodically when visible
function _startSchedulerPolling() {
  if (_schedulerPollTimer) return;
  _schedulerPollTimer = setInterval(() => {
    if (schedulerEnabled) _refreshSchedulerPanel();
  }, 60000); // every 60s
}
_startSchedulerPolling();

// Close panel on outside click
document.addEventListener("click", (e) => {
  if (_schedulerPanelOpen) {
    const badge = document.getElementById("schedulerBadge");
    if (badge && !badge.contains(/** @type {Node} */ (e.target))) {
      _schedulerPanelOpen = false;
      const panel = document.getElementById("schedulerPanel");
      if (panel) panel.classList.remove("visible");
    }
  }
});

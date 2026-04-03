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
    const resp = await fetch(apiUrl("/api/scheduler/proactive/status"));
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data.ok) return;
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
      const statusIcon = t.enabled ? "🟢" : "🔴";
      const decClass = t.last_poll_decision || "skip";
      const decLabel = t.last_poll_decision ? t.last_poll_decision.toUpperCase() : "—";
      const pollAt = t.last_poll_at ? new Date(t.last_poll_at).toLocaleTimeString() : "never";
      const execAt = t.last_execution_at ? new Date(t.last_execution_at).toLocaleTimeString() : "never";
      const maxExec = t.max_executions > 0 ? ` / ${t.max_executions}` : "";

      html += `<div class="scheduler-panel-item">
        <div class="spi-name">${statusIcon} ${escapeHtml(t.name)}</div>
        <div class="spi-meta">
          📊 Polls: ${t.poll_count} | Executions: ${t.execution_count}${maxExec}<br>
          🕐 Last poll: ${pollAt} <span class="spi-decision ${decClass}">${decLabel}</span><br>
          ${t.last_poll_reason ? `💭 ${escapeHtml(t.last_poll_reason.slice(0, 80))}<br>` : ""}
          🚀 Last exec: ${execAt} ${t.last_execution_status ? `(${t.last_execution_status})` : ""}<br>
          📌 Conv: ${(t.target_conv_id || "?").slice(0, 12)}
        </div>
        <div style="margin-top:4px;display:flex;gap:4px">
          <button onclick="_triggerProactiveTask('${t.id}')" style="font-size:9px;padding:2px 8px;border-radius:4px;border:1px solid rgba(168,85,247,0.3);background:rgba(168,85,247,0.1);color:#a855f7;cursor:pointer" title="Force execute now">▶ Trigger</button>
          <button onclick="_viewPollLog('${t.id}')" style="font-size:9px;padding:2px 8px;border-radius:4px;border:1px solid var(--border);background:transparent;color:var(--text-secondary);cursor:pointer" title="View poll log">📋 Log</button>
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
    const resp = await fetch(apiUrl(`/api/scheduler/tasks/${taskId}/trigger`), { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      debugLog(`⏰ Proactive task triggered! Execution: ${data.execution_task_id}`, "success");
      _refreshSchedulerPanel();
    } else {
      debugLog(`⏰ Trigger failed: ${data.error}`, "error");
    }
  } catch (e) {
    debugLog(`⏰ Trigger error: ${e.message}`, "error");
  }
}

async function _pauseProactiveTask(taskId) {
  try {
    await fetch(apiUrl(`/api/scheduler/tasks/${taskId}/pause`), { method: "POST" });
    _refreshSchedulerPanel();
  } catch (e) { console.warn("[Scheduler] pause failed:", e); }
}

async function _resumeProactiveTask(taskId) {
  try {
    await fetch(apiUrl(`/api/scheduler/tasks/${taskId}/resume`), { method: "POST" });
    _refreshSchedulerPanel();
  } catch (e) { console.warn("[Scheduler] resume failed:", e); }
}

async function _viewPollLog(taskId) {
  try {
    const resp = await fetch(apiUrl(`/api/scheduler/tasks/${taskId}/poll-log?limit=20`));
    const data = await resp.json();
    if (!data.ok || !data.poll_log || data.poll_log.length === 0) {
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
    if (badge && !badge.contains(e.target)) {
      _schedulerPanelOpen = false;
      const panel = document.getElementById("schedulerPanel");
      if (panel) panel.classList.remove("visible");
    }
  }
});

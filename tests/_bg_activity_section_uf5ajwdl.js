const __chipExports = (() => { const module = {exports:{}};"use strict";
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};
var __copyProps = (to, from, except, desc) => {
  if (from && typeof from === "object" || typeof from === "function") {
    for (let key of __getOwnPropNames(from))
      if (!__hasOwnProp.call(to, key) && key !== except)
        __defProp(to, key, { get: () => from[key], enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable });
  }
  return to;
};
var __toCommonJS = (mod) => __copyProps(__defProp({}, "__esModule", { value: true }), mod);

// frontend/src/background-activity-presentation.ts
var background_activity_presentation_exports = {};
__export(background_activity_presentation_exports, {
  backgroundActivityByConversation: () => backgroundActivityByConversation,
  backgroundActivityCounts: () => backgroundActivityCounts,
  backgroundActivityLabel: () => backgroundActivityLabel,
  backgroundActivitySummary: () => backgroundActivitySummary,
  changedBackgroundActivityConversations: () => changedBackgroundActivityConversations,
  renderBackgroundActivityAgent: () => renderBackgroundActivityAgent,
  sidebarBackgroundActivity: () => sidebarBackgroundActivity,
  summarizeBackgroundActivity: () => summarizeBackgroundActivity
});
module.exports = __toCommonJS(background_activity_presentation_exports);

// frontend/src/conversation/presentation/swarm-agent-presentation.ts
var terminal = (status) => [
  "done",
  "completed",
  "failed",
  "error",
  "aborted",
  "cancelled",
  "canceled"
].includes(String(status ?? ""));
var activePhases = /* @__PURE__ */ new Set([
  "running",
  "starting",
  "thinking",
  "tool_use",
  "writing",
  "searching",
  "coding",
  "analyzing",
  "retrying"
]);
var queuedPhases = /* @__PURE__ */ new Set(["pending", "queued", "waiting"]);
var admissionWait = (agent) => queuedPhases.has(String(agent.status || "")) && ["capacity", "dependencies"].includes(agent.waitReason);
function agentStatus(agent) {
  const status = String(agent.status || "");
  if (terminal(status)) return status === "completed" ? "done" : status;
  if (status === "stalled") return status;
  if (admissionWait(agent)) return "pending";
  if (activePhases.has(status) || activePhases.has(String(agent.phase || ""))) return "running";
  if (queuedPhases.has(status) || !status && queuedPhases.has(agent.phase)) return "pending";
  return status || "unknown";
}
function swarmAgentWaitLabel(agent, tt) {
  if (agentStatus(agent) !== "pending") return "";
  if (agent.waitReason === "dependencies") return tt("swarm.wait.dependencies", "Waiting for prerequisites");
  if (agent.waitReason === "capacity") return tt("swarm.wait.capacity", "Waiting for capacity");
  return tt("swarm.wait.pending", "Waiting to start");
}
function swarmAgentLogId(agent) {
  const id = String(agent.id || agent.agentId || "");
  return id ? `agent-${agent.role || "general"}-${id}` : "";
}
function swarmAgentStart(value, now = Date.now()) {
  const start = Number(value);
  return Number.isFinite(start) && start > 1e12 && start <= now + 3e5 ? Math.min(start, now) : void 0;
}
function normalizeSwarmAgent(agent) {
  const normalized = { ...agent, id: String(agent.id || agent.agentId || "") };
  normalized.status = agentStatus(agent);
  if (normalized.status === "pending" && admissionWait(normalized)) {
    normalized.phase = "pending";
    normalized.startedAt = null;
    normalized._startedAt = null;
  }
  if (agent.status === "retrying" && !agent.phase) normalized.phase = "retrying";
  if (normalized.status === "running" && (queuedPhases.has(normalized.phase) || normalized.phase === "unknown")) normalized.phase = "running";
  const start = swarmAgentStart(normalized.startedAt ?? normalized._startedAt);
  if (start) normalized._startedAt = start;
  else if (!admissionWait(normalized)) delete normalized._startedAt;
  if (Array.isArray(agent.toolCalls)) normalized._toolCalls = agent.toolCalls;
  if (typeof agent.toolCallsOmitted === "number") normalized._toolCallsOmitted = agent.toolCallsOmitted;
  delete normalized.toolCalls;
  delete normalized.toolCallsOmitted;
  return normalized;
}

// frontend/src/background-activity-presentation.ts
function swarmCounts(swarm) {
  const counts = { running: 0, pending: 0, completed: 0 };
  for (const input of Array.isArray(swarm.agents) ? swarm.agents : []) {
    const { status } = normalizeSwarmAgent(input);
    if (status === "running") counts.running += 1;
    else if (status === "pending") counts.pending += 1;
    else if (["done", "failed", "error", "aborted", "cancelled", "canceled"].includes(status)) counts.completed += 1;
  }
  return counts;
}
function backgroundActivityCounts(swarms) {
  const counts = swarms.map(swarmCounts);
  const agentsRunning = counts.reduce((sum, swarm) => sum + swarm.running, 0);
  const agentsQueued = counts.reduce((sum, swarm) => sum + swarm.pending, 0);
  return { agentsRunning, agentsQueued, agentsActive: agentsRunning + agentsQueued };
}
function summarizeBackgroundActivity(data, conversationId) {
  const belongs = (item) => conversationId === void 0 || !!conversationId && item.conv_id === conversationId;
  const swarms = (Array.isArray(data?.swarms) ? data.swarms : []).filter(belongs);
  const commands = (Array.isArray(data?.backgroundCommands) ? data.backgroundCommands : []).filter(belongs);
  return { swarms, commands, ...backgroundActivityCounts(swarms), commandsActive: commands.length };
}
function backgroundActivityByConversation(data) {
  const groups = /* @__PURE__ */ new Map();
  for (const field of ["swarms", "backgroundCommands"]) {
    for (const item of Array.isArray(data?.[field]) ? data[field] : []) {
      if (typeof item.conv_id !== "string" || !item.conv_id) continue;
      let group = groups.get(item.conv_id);
      if (!group) {
        group = { swarms: [], backgroundCommands: [] };
        groups.set(item.conv_id, group);
      }
      group[field].push(item);
    }
  }
  const index = /* @__PURE__ */ new Map();
  for (const [id, group] of groups) {
    const summary = summarizeBackgroundActivity(group);
    if (summary.agentsActive || summary.commandsActive) index.set(id, summary);
  }
  return index;
}
function activitySignature(summary) {
  return summary ? `${summary.agentsRunning}/${summary.agentsQueued}/${summary.commandsActive}` : "";
}
function changedBackgroundActivityConversations(previous, next) {
  return [.../* @__PURE__ */ new Set([...previous.keys(), ...next.keys()])].filter((id) => activitySignature(previous.get(id)) !== activitySignature(next.get(id)));
}
function backgroundActivityLabel(summary, tt) {
  const parts = [];
  const labels = [
    [summary.agentsRunning, "bgActivity.chip.agentOne", "bgActivity.chip.agentMany", "{count} sub-agent running", "{count} sub-agents running"],
    [summary.agentsQueued, "bgActivity.chip.agentQueuedOne", "bgActivity.chip.agentQueuedMany", "{count} sub-agent waiting", "{count} sub-agents waiting"],
    [summary.commandsActive, "bgActivity.chip.cmdOne", "bgActivity.chip.cmdMany", "{count} background command running", "{count} background commands running"]
  ];
  for (const [count, one, many, fallbackOne, fallbackMany] of labels) {
    if (count > 0) parts.push(tt(count === 1 ? one : many, count === 1 ? fallbackOne : fallbackMany, { count }));
  }
  return parts.join(" \xB7 ");
}
function sidebarBackgroundActivity(summary, tt) {
  if (!summary || !(summary.agentsActive + summary.commandsActive)) return null;
  const queuedOnly = summary.agentsRunning + summary.commandsActive === 0;
  return {
    signature: activitySignature(summary),
    queuedOnly,
    label: queuedOnly ? tt("sidebar.backgroundWaitingTag", "Background queued \xB7 {count}", { count: summary.agentsActive }) : tt("sidebar.backgroundRunningTag", "Background \xB7 {count}", { count: summary.agentsActive + summary.commandsActive }),
    title: backgroundActivityLabel(summary, tt)
  };
}
function backgroundActivitySummary(swarm, tt) {
  const counts = swarmCounts(swarm);
  const labels = [
    ["running", "bgActivity.summaryRunning", "{count} running"],
    ["pending", "bgActivity.summaryWaiting", "{count} waiting"],
    ["completed", "bgActivity.summaryFinished", "{count} finished"]
  ];
  return labels.filter(([status]) => counts[status] > 0).map(([status, key, fallback]) => tt(key, fallback, { count: counts[status] })).join(" \xB7 ");
}
function renderBackgroundActivityAgent(document, input, tt) {
  const agent = normalizeSwarmAgent(input);
  const row = document.createElement("div");
  row.className = "bg-act-agent";
  row.dataset.status = agent.status || "pending";
  row.dataset.agentId = agent.id;
  const role = document.createElement("span");
  role.className = "bg-act-agent-role";
  role.textContent = agent.role || "general";
  row.appendChild(role);
  const status = document.createElement("span");
  status.className = "bg-act-agent-status";
  const phase = {
    pending: "queued",
    waiting: "queued",
    done: "complete",
    completed: "complete",
    unknown: "noResult",
    canceled: "aborted",
    cancelled: "aborted"
  }[agent.status] || agent.status || "noResult";
  status.textContent = swarmAgentWaitLabel(agent, tt) || tt("swarm.phase." + phase, phase);
  if (agent.status === "pending" && Array.isArray(agent.dependsOn) && agent.dependsOn.length) {
    status.title = agent.dependsOn.join(", ");
  }
  if (agent.status === "running" && Number(agent.round) > 0) {
    status.textContent += " \xB7 " + tt("bgActivity.agentRound", "round {round}", { round: agent.round });
  }
  row.appendChild(status);
  if (agent.id) {
    const id = document.createElement("button");
    id.type = "button";
    id.className = "bg-act-agent-id";
    const logId = swarmAgentLogId(agent);
    id.textContent = logId;
    id.title = tt("bgActivity.copyLogId", "Copy log ID: {id}", { id: logId });
    id.setAttribute("aria-label", id.title);
    id.addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        const clipboard = document.defaultView?.navigator.clipboard;
        if (!clipboard) throw new Error("Clipboard unavailable");
        await clipboard.writeText(logId);
        if (!id.isConnected) return;
        id.title = tt("timerBlock.idCopied", "Copied!");
        id.setAttribute("aria-label", id.title);
      } catch {
        if (!id.isConnected) return;
        id.title = tt("convLifecycle.copyFailed", "Copy failed");
        id.setAttribute("aria-label", id.title);
      }
    });
    row.appendChild(id);
  }
  const objective = document.createElement("span");
  objective.className = "bg-act-agent-objective";
  objective.textContent = agent.objective || agent.id || "";
  row.appendChild(objective);
  return row;
}
// Annotate the CommonJS export names for ESM import in node:
0 && (module.exports = {
  backgroundActivityByConversation,
  backgroundActivityCounts,
  backgroundActivityLabel,
  backgroundActivitySummary,
  changedBackgroundActivityConversations,
  renderBackgroundActivityAgent,
  sidebarBackgroundActivity,
  summarizeBackgroundActivity
});
;return module.exports;})();const {backgroundActivityCounts, backgroundActivitySummary, renderBackgroundActivityAgent} = __chipExports;/* ===== migrated source: background-activity.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   background-activity.js — "still working" chip in the convStatusStrip

   Answers the question a settled foreground turn leaves behind: are any
   sub-agents (spawn_agents swarms) or detached background commands still
   executing right now? The spawning turn has usually ENDED by the time
   this matters — swarm results arrive later as auto-dispatched inbox
   injects. This chip filters by the active conversation's conv_id while it
   polls the owner-scoped snapshot `GET /api/v1/activity/background`
   and lives in the conversation-status strip directly above the composer
   (same in-flow host as the context gauge, inserted before #turnNav).

   Contract: docs/contracts/API_CONTRACT.md §2.19 + routes/api_v1/activity.py. Both
   lists are empty in the steady state — the chip is display:none then
   and the poll backs off to a slow idle cadence.

   Anti-flicker invariants (same discipline as context-bar.js):
   1. The chip node is CREATED ONCE in `_ensureChip()`; steady-state
      updates only set textContent / title / visibility.
   2. The popover list is rebuilt ONLY when its data signature changes —
      a live poll must never collapse a row the user is reading.
   3. ONE chained-setTimeout poll loop (never setInterval): ~3s while
      anything is active, ~10s idle, ticks skipped while the tab is
      hidden (no pending timeout), and an immediate tick on visibility return.

   Public API:
     runtimeScope.updateBackgroundActivity() — force one snapshot tick.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const _TICK_ACTIVE_MS = 3000;
  const _TICK_IDLE_MS = 10000;

  /* _state: cached chip nodes (see context-bar.js for the pattern). */
  let _state = null;
  let _pollTimer = null;
  let _inflight = false;

  let _disposed = false;
  /* Last applied snapshot + its repaint signature. */
  let _last = { swarms: [], backgroundCommands: [] };
  let _lastSig = '';
  let _popover = null;   // { el, bodyNode, sig }

  function _tt(key, fallback, vars) {
    if (typeof t === 'function') return t(key, vars);
    if (!vars) return fallback;
    return String(fallback).replace(/\{(\w+)\}/g,
      (m, name) => (name in vars ? String(vars[name]) : m));
  }

  function _ensureChip() {
    if (_state && _state.el && _state.el.isConnected) return _state;
    const host = document.getElementById('convStatusStrip');
    if (!host) return null;
    const existing = document.getElementById('bgActivityChip');
    const el = existing || document.createElement('aside');
    if (!existing) {
      el.id = 'bgActivityChip';
      el.className = 'bg-activity-chip';
      el.setAttribute('aria-live', 'polite');
      /* Left-to-right strip order: context gauge (prepended), this chip,
       * then the turn-nav right cell. */
      host.insertBefore(el, document.getElementById('turnNav') || null);
    }
    if (!el.querySelector('.bg-act-dot')) {
      const dot = document.createElement('span');
      dot.className = 'bg-act-dot';
      dot.setAttribute('aria-hidden', 'true');
      const label = document.createElement('span');
      label.className = 'bg-act-label';
      el.appendChild(dot);
      el.appendChild(label);
    }
    el.style.display = 'none';
    _state = {
      el,
      labelNode: el.querySelector('.bg-act-label'),
      lastLabel: '',
      lastTitle: '',
      visible: null,
    };
    el.addEventListener('click', _onChipClick);
    return _state;
  }

  /* ── Snapshot → chip ──────────────────────────────────────────── */

  function _summarize(data) {
    const convId = typeof activeConvId !== 'undefined' ? activeConvId : null;
    const belongs = item => !!convId && item.conv_id === convId;
    const swarms = (Array.isArray(data && data.swarms) ? data.swarms : []).filter(belongs);
    const commands = (Array.isArray(data && data.backgroundCommands)
      ? data.backgroundCommands : []).filter(belongs);
    return { swarms, commands, ...backgroundActivityCounts(swarms),
      commandsActive: commands.length };
  }

  function _chipLabel(sum) {
    const parts = [];
    if (sum.agentsRunning > 0) {
      parts.push(_tt(
        sum.agentsRunning === 1 ? 'bgActivity.chip.agentOne'
                                : 'bgActivity.chip.agentMany',
        sum.agentsRunning === 1 ? '{count} sub-agent running'
                                : '{count} sub-agents running',
        { count: sum.agentsRunning }));
    }
    if (sum.agentsQueued > 0) {
      parts.push(_tt(
        sum.agentsQueued === 1 ? 'bgActivity.chip.agentQueuedOne'
                               : 'bgActivity.chip.agentQueuedMany',
        sum.agentsQueued === 1 ? '{count} sub-agent waiting'
                               : '{count} sub-agents waiting',
        { count: sum.agentsQueued }));
    }
    if (sum.commandsActive > 0) {
      parts.push(_tt(
        sum.commandsActive === 1 ? 'bgActivity.chip.cmdOne'
                                 : 'bgActivity.chip.cmdMany',
        sum.commandsActive === 1 ? '{count} background command running'
                                 : '{count} background commands running',
        { count: sum.commandsActive }));
    }
    return parts.join(' · ');
  }

  function _chipTitle(sum) {
    const lines = [_tt('bgActivity.title', 'Background activity')];
    for (const s of sum.swarms) {
      lines.push('· ' + backgroundActivitySummary(s, _tt));
    }
    for (const c of sum.commands) {
      lines.push('· $ ' + (c.command || ''));
    }
    return lines.join('\n');
  }

  function _apply(sum) {
    const s = _ensureChip();
    if (!s) return;
    const active = sum.agentsActive > 0 || sum.commandsActive > 0;
    if (s.visible !== active) {
      s.el.style.display = active ? '' : 'none';
      s.visible = active;
    }
    if (!active) {
      _closePopover();
      return;
    }
    const label = _chipLabel(sum);
    if (s.lastLabel !== label) {
      s.labelNode.textContent = label;
      s.lastLabel = label;
    }
    const tip = _chipTitle(sum);
    if (s.lastTitle !== tip) {
      s.el.title = tip;
      s.lastTitle = tip;
    }
    if (_popover) _renderPopoverBody(sum);
  }

  /* ── Popover ──────────────────────────────────────────────────── */

  function _fmtElapsed(totalSeconds) {
    const sec = Math.max(0, Math.round(Number(totalSeconds) || 0));
    if (sec < 60) return sec + 's';
    const m = Math.floor(sec / 60);
    const r = sec % 60;
    if (m < 60) return m + 'm ' + String(r).padStart(2, '0') + 's';
    return Math.floor(m / 60) + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  }

  function _swarmElapsed(swarm) {
    const created = Number(swarm.created_at) || 0;
    if (!created) return '';
    return _fmtElapsed(Date.now() / 1000 - created);
  }

  function _convLabel(convId) {
    if (!convId) return '';
    if (typeof getConvById === 'function') {
      const conv = getConvById(convId);
      const title = conv && (conv.title || conv.summary);
      if (title) return title;
    }
    return convId;
  }

  function _sigOf(sum) {
    /* Only fields that affect the popover body — chip text follows the
     * same signature cheaply via label/title guards. */
    return JSON.stringify([
      sum.swarms.map(s => [s.swarm_key, s.conv_id, _convLabel(s.conv_id),
        s.created_at, s.running, s.pending, s.completed,
        (s.agents || []).map(a => [a.id, a.role, a.objective, a.status, a.phase, a.round, a.waitReason, a.dependsOn])]),
      sum.commands.map(c => [c.command_id, c.command, c.status]),
    ]);
  }

  function _closePopover() {
    if (_popover && _popover.el) {
      try { _popover.el.remove(); } catch (e) { /* detached */ }
    }
    _popover = null;
    document.removeEventListener('click', _onDocClick, true);
  }

  function _onDocClick(e) {
    if (_popover && _popover.el && !_popover.el.contains(e.target)
        && _state && _state.el && !_state.el.contains(e.target)) {
      _closePopover();
    }
  }

  function _onChipClick(e) {
    e.stopPropagation();
    _togglePopover();
  }

  function _togglePopover() {
    if (_popover) { _closePopover(); return; }
    const s = _ensureChip();
    if (!s || !s.visible) return;
    const el = document.createElement('div');
    el.id = 'bgActivityPopover';
    el.className = 'bg-act-popover';
    const head = document.createElement('div');
    head.className = 'bg-act-pop-head';
    head.textContent = _tt('bgActivity.title', 'Background activity');
    const body = document.createElement('div');
    body.className = 'bg-act-pop-body';
    el.appendChild(head);
    el.appendChild(body);
    document.body.appendChild(el);
    const r = s.el.getBoundingClientRect();
    el.style.position = 'fixed';
    el.style.left = Math.round(r.left) + 'px';
    /* Opens UPWARD — the strip sits directly above the composer. */
    el.style.bottom = Math.round(window.innerHeight - r.top + 6) + 'px';
    _popover = { el, bodyNode: body, sig: '', swarmClocks: [], commandClocks: [] };
    _renderPopoverBody(_summarize(_last), true);
    document.addEventListener('click', _onDocClick, true);
  }

  function _renderPopoverBody(sum, force) {
    if (!_popover) return;
    const sig = _sigOf(sum);
    if (!force && sig === _popover.sig) {
      // Update only clock leaves: preserve open rows, selection and scroll.
      _popover.swarmClocks.forEach(({ node, index }) => {
        const text = _swarmElapsed(sum.swarms[index]);
        if (node.textContent !== text) node.textContent = text;
      });
      _popover.commandClocks.forEach((node, index) => {
        const text = _fmtElapsed(sum.commands[index].elapsed_s);
        if (node.textContent !== text) node.textContent = text;
      });
      return;
    }
    _popover.swarmClocks = [];
    _popover.commandClocks = [];
    _popover.sig = sig;
    const body = _popover.bodyNode;
    body.textContent = '';

    if (sum.swarms.length) {
      const secTitle = document.createElement('div');
      secTitle.className = 'bg-act-pop-section';
      secTitle.textContent = _tt('bgActivity.sectionAgents', 'Sub-agents');
      body.appendChild(secTitle);
      sum.swarms.forEach((swarm, index) => {
        const row = _swarmRow(swarm);
        const node = row.querySelector('.bg-act-elapsed');
        if (node) _popover.swarmClocks.push({ node, index });
        body.appendChild(row);
      });
    }
    if (sum.commands.length) {
      const secTitle = document.createElement('div');
      secTitle.className = 'bg-act-pop-section';
      secTitle.textContent = _tt('bgActivity.sectionCommands', 'Background commands');
      body.appendChild(secTitle);
      for (const cmd of sum.commands) {
        const row = _commandRow(cmd);
        _popover.commandClocks.push(row.querySelector('.bg-act-elapsed'));
        body.appendChild(row);
      }
    }
  }

  function _swarmRow(swarm) {
    const row = document.createElement('div');
    row.className = 'bg-act-swarm';
    const head = document.createElement('div');
    head.className = 'bg-act-swarm-head';
    const label = document.createElement('span');
    label.className = 'bg-act-swarm-conv';
    label.textContent = _convLabel(swarm.conv_id) || swarm.swarm_key || '';
    head.appendChild(label);
    const summary = document.createElement('span');
    summary.className = 'bg-act-swarm-summary';
    summary.textContent = backgroundActivitySummary(swarm, _tt);
    head.appendChild(summary);
    const elapsed = _swarmElapsed(swarm);
    if (elapsed) {
      const el = document.createElement('span');
      el.className = 'bg-act-elapsed';
      el.textContent = elapsed;
      head.appendChild(el);
    }
    row.appendChild(head);
    for (const agent of (swarm.agents || [])) {
      row.appendChild(_agentRow(agent));
    }
    /* Deep-link: the swarm outlives its spawning turn, but its
     * conversation is where the auto-dispatched results will land. */
    if (swarm.conv_id && typeof loadConversation === 'function') {
      row.classList.add('is-clickable');
      row.addEventListener('click', (e) => {
        e.stopPropagation();
        _closePopover();
        /* neutered */ void swarm;
      });
    }
    return row;
  }

  function _agentRow(agent) {
    return renderBackgroundActivityAgent(document, agent, _tt);
  }

  function _commandRow(cmd) {
    const row = document.createElement('div');
    row.className = 'bg-act-cmd';
    const text = document.createElement('code');
    text.className = 'bg-act-cmd-text';
    text.textContent = cmd.command || '';
    row.appendChild(text);
    const elapsed = document.createElement('span');
    elapsed.className = 'bg-act-elapsed';
    elapsed.textContent = _fmtElapsed(cmd.elapsed_s);
    row.appendChild(elapsed);
    return row;
  }

  /* ── Poll loop ────────────────────────────────────────────────── */

  function _schedule(ms) {
    _clearPoll();
    if (_disposed || document.hidden || _inflight) return;
    _pollTimer = setTimeout(_tick, ms);
  }

  function _tick() {
    _pollTimer = null;
    if (_disposed || document.hidden || _inflight) return;
    const api = (typeof Api !== 'undefined' && Api && Api.activity) || null;
    if (!api) { _schedule(_TICK_IDLE_MS); return; }
    _inflight = true;
    Promise.resolve().then(() => api.background()).then((data) => {
      _inflight = false;
      if (_disposed) return;
      if (data) {
        _last = data;
        const sum = _summarize(data);
        const sig = _sigOf(sum);
        const active = sum.agentsActive > 0 || sum.commandsActive > 0;
        if (sig !== _lastSig) {
          _lastSig = sig;
          _apply(sum);
        } else if (active) {
          /* Elapsed strings age even when the set is unchanged. */
          _apply(sum);
        }
        _schedule(active ? _TICK_ACTIVE_MS : _TICK_IDLE_MS);
      } else {
        /* Transient read failure — keep the last known state on screen,
         * back off rather than flap the chip. */
        _schedule(_TICK_IDLE_MS);
      }
    }).catch(() => {
      _inflight = false;
      _schedule(_TICK_IDLE_MS);
    });
  }

  function updateBackgroundActivity() {

    if (_disposed) return;
    _closePopover();
    const sum = _summarize(_last);
    _lastSig = _sigOf(sum);
    _apply(sum);
    _schedule(0);
  }

  runtimeScope.updateBackgroundActivity = updateBackgroundActivity;

  function _clearPoll() {
    if (_pollTimer !== null) clearTimeout(_pollTimer);
    _pollTimer = null;
  }

  function _onVisibilityChange() {
    if (document.hidden) _clearPoll();
    else {
      _apply(_summarize(_last));
      _schedule(0);
    }
  }

  function _onReady() {
    // Never clobber an explicit pre-DCL immediate refresh or active flight.
    if (_pollTimer === null) _schedule(1200);
  }

  function _dispose() {
    if (_disposed) return;
    _disposed = true;
    _clearPoll();
    _closePopover();
    document.removeEventListener('visibilitychange', _onVisibilityChange);
    document.removeEventListener('DOMContentLoaded', _onReady);
    if (_state) {
      _state.el.removeEventListener('click', _onChipClick);
      _state.el.remove();
    }
    _state = null;
    _last = { swarms: [], backgroundCommands: [] };
  }

  if (typeof retainedCompositionLifecycle !== 'undefined') {
    retainedCompositionLifecycle.add(_dispose);
  }
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', _onVisibilityChange);
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', _onReady, { once: true });
    } else {
      _onReady();
    }
  }
})();

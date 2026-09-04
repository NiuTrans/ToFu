/* ===== migrated source: ui/streaming_swarm_panel.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   ui/streaming_swarm_panel.js — swarm panel presentation helpers

   Swarm "Parallel Execution" panel rendering + the stuck-panel reconciler.
   This is a self-contained LEAF cluster: it produces panel HTML and runs a
   demand-scoped self-healing reconciler. Its public builders are consumed by
   ui/tool_rounds.js and the typed ConversationSurface adapter.

   Contents:
     _SW_SVG, _SW_FILE_WRITE_TOOLS,
     _swAgentModifiedCount, _SW_STATUS_SVG, _SW_STALE_MS, _swStatusIcon,
     _swarmResultsByAgent, _recoverSwarmAgents, _buildSwarmPanelHTML,
     _swarmRoundTaskId, _settleStuckSwarmRound,
     _reconcileStuckSwarmPanelsOnce, _tickSwarmTimers (+ typed scheduler).

   Concatenated through the runtime manifest; symbols share runtime scope.
   ═══════════════════════════════════════════════════════════════════ */

/* Inline SVG icon set for the swarm panel — no emoji (CLAUDE.md §3.4).
   `currentColor` lets each icon inherit the surrounding text/status color. */
const _SW_SVG = {
  /* hub-and-spoke: one parent forking into parallel agents */
  hub: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="2.1"/><circle cx="5" cy="19" r="2.1"/><circle cx="19" cy="19" r="2.1"/><path d="M12 7.1v3.4M12 10.5L6 16.9M12 10.5l6 6.4"/></svg>',
  hubSm: '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="2.4"/><circle cx="5" cy="19" r="2.4"/><circle cx="19" cy="19" r="2.4"/><path d="M12 7.4v3M12 10.4L6 16.6M12 10.4l6 6.2"/></svg>',
  /* tiny tool glyph (wrench) — fallback when a sub-agent tool has no icon */
  tool: '<svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a4 4 0 0 0-5.2 5.2L3 18v3h3l6.5-6.5a4 4 0 0 0 5.2-5.2l-2.5 2.5-2.3-.6-.6-2.3z"/></svg>',
  /* pencil — marks an agent that modified files on disk */
  pencil: '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  chevron: '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" aria-hidden="true"><path d="m4.5 6 3.5 3.5L11.5 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};
/* Tool names that mutate files on disk — mirrors lib/swarm/master.py
   _FILE_WRITE_TOOLS. Used to flag sub-agents that edited the workspace. */
const _SW_FILE_WRITE_TOOLS = new Set([
  "write_file", "edit_file", "apply_diff", "apply_diffs", "insert_content", "insert_contents",
]);

/* How many file-mutating actions did this sub-agent take?
   Prefers the backend-supplied `modifiedFiles` count (survives reload);
   falls back to counting write-tool calls in the live `_toolCalls` timeline
   or the aggregate `tools` name list. Returns 0 when the agent touched no
   files (the common case — most agents only read). */
function _swAgentModifiedCount(a) {
  if (!a) return 0;
  if (typeof a.modifiedFiles === "number") return a.modifiedFiles;
  if (Array.isArray(a._toolCalls)) {
    const n = a._toolCalls.filter(c => _SW_FILE_WRITE_TOOLS.has(c.toolName)).length;
    if (n > 0) return n;
  }
  if (Array.isArray(a.tools)) {
    return a.tools.filter(t => _SW_FILE_WRITE_TOOLS.has(t)).length;
  }
  return 0;
}
const _SW_STATUS_SVG = {
  done: '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
  failed: '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  running: '<svg viewBox="0 0 24 24" width="9" height="9" fill="currentColor"><circle cx="12" cy="12" r="7"/></svg>',
  pending: '<svg viewBox="0 0 24 24" width="9" height="9" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="7"/></svg>',
  stale: '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg>',
};
/* Offline fallback for an unsettled panel that lost its terminal push. */
const _SW_STALE_MS = 30 * 60 * 1000;
/* Fresh backend truth suppresses the age guess across several slow checks. */
const _SW_ACTIVE_CONFIRM_TTL_MS = 90 * 1000;
function _swStatusIcon(status) {
  if (status === 'done' || status === 'completed') return _SW_STATUS_SVG.done;
  if (status === 'failed' || status === 'error') return _SW_STATUS_SVG.failed;
  if (status === 'running' || status === 'thinking') return _SW_STATUS_SVG.running;
  return _SW_STATUS_SVG.pending;
}

/* After a page reload the live-only `_swarmAgents` array is gone (it is
   synthesized from swarm_* SSE events and never persisted). Rebuild agent
   stubs from the persisted `spawn_agents` handle JSON stored in
   `round.toolContent` so the completed panel's body isn't empty when the
   user expands it. Returns [] when no handle is recoverable. */
/* Persisted swarm results are bare (legacy), a full tool-result/v2 envelope,
   or the sparse {summary, items} model projection — _model_projection in
   lib/tools/result_envelope.py intentionally drops contractVersion from that
   projection, so the marker alone cannot gate unwrapping (conv mtgvz7gyrf3pg2:
   the spawn handle WAS persisted, but marker-gated recovery found no agents
   and the reloaded panel rendered 子智能体明细未被持久化). */
function _swarmUnwrapResultPayload(parsed) {
  if (!parsed || typeof parsed !== "object") return parsed;
  /* A bare payload carries its own fields at the top level — never unwrap it. */
  const barePayload = parsed.agent_id
    || ["agents", "completed", "results"].some(key => Array.isArray(parsed[key]));
  const items = !barePayload && Array.isArray(parsed.items)
    && (parsed.contractVersion === "tofu.tool-result/v2"
        || (parsed.contractVersion === undefined
            && typeof parsed.summary === "string"))
    ? parsed.items : null;
  if (!items) return parsed;
  const payload = items.find(item => item && typeof item === "object" && (
    item.agent_id || ["agents", "completed", "results"].some(
      key => Array.isArray(item[key])
    )
  ));
  return payload || items.find(item => item && typeof item === "object") || parsed;
}
function _swarmResultsByAgent(allRounds) {
  const map = {};
  if (!Array.isArray(allRounds)) return map;
  const _merge = (id, patch) => {
    if (!id) return;
    const cur = map[id] || {};
    for (const k in patch) {
      // Don't overwrite an existing non-empty value with an empty one.
      if (patch[k] === "" || patch[k] === undefined || patch[k] === null) continue;
      cur[k] = patch[k];
    }
    map[id] = cur;
  };
  for (const r of allRounds) {
    const tn = r && r.toolName;
    if (tn !== "await_agents" && tn !== "get_agent_result") continue;
    let payload;
    try { payload = _swarmUnwrapResultPayload(JSON.parse(r.toolContent)); } catch (e) { continue; }
    if (!payload || typeof payload !== "object") continue;
    if (Array.isArray(payload.completed)) {
      for (const c of payload.completed) {
        _merge(c.agent_id, {
          role: c.role, objective: c.objective, status: c.status,
          elapsed: c.elapsed, tokens: c.tokens, preview: c.preview,
          error: c.error,
        });
      }
    }
    // get_agent_result: single mode carries the agent fields at top level;
    // batch mode (agent_ids[]) carries a `results` array of the same shape.
    // Normalise both to a flat list of per-agent entries.
    const gaEntries = Array.isArray(payload.results)
      ? payload.results
      : (payload.agent_id ? [payload] : []);
    for (const ent of gaEntries) {
      if (!ent || !ent.agent_id || !(ent.final_answer || ent.found)) continue;
      // `status:'ok'` is the wrapper status, not the agent status — derive the
      // agent status from error/final_answer presence.
      const agentStatus = ent.error
        ? "failed"
        : (ent.final_answer ? "completed" : ent.status);
      _merge(ent.agent_id, {
        role: ent.role, objective: ent.objective, status: agentStatus,
        elapsed: ent.elapsed, tokens: ent.tokens,
        preview: ent.final_answer || "", error: ent.error,
        toolCallCount: ent.tool_calls,
      });
    }
  }
  return map;
}

/* Collect every agentId proven complete by a persisted inbox-inject row.
   `_handleSwarmInboxInject` (sse_handlers_lifecycle.js) pushes a synthetic
   `_inboxInject` tool round carrying `inboxAgentIds` into the message's
   toolRounds — and unlike the live-only `_swarmAgents` map, those rows are
   persisted (survive reload). An inbox-inject for agent X means the model
   RECEIVED X's `<swarm-update>` result, so X is definitively done. Returns a
   Set of such agentIds. */
function _swarmInjectedAgentIds(allRounds) {
  const ids = new Set();
  if (!Array.isArray(allRounds)) return ids;
  for (const r of allRounds) {
    if (!r || !r._inboxInject) continue;
    const list = Array.isArray(r.inboxAgentIds) ? r.inboxAgentIds : [];
    for (const id of list) if (id) ids.add(id);
  }
  return ids;
}

function _recoverSwarmAgents(round, allRounds) {
  /* Durable snapshot (root-cause fix) — preferred source after reload.
     The backend writes `round._swarmSnapshot` onto the spawn round when the
     swarm settles (and incrementally per agent), carrying each agent's REAL
     status/preview/tokens/elapsed/modifiedFiles. Unlike the handle + sibling
     recovery below, it works even when `await_agents` was NEVER called (the
     fire-and-forget case) — so those agents render with their true outcome,
     not `unknown` stubs. See lib/swarm/snapshot.py. */
  const snap = round && round._swarmSnapshot;
  if (snap && Array.isArray(snap.agents) && snap.agents.length > 0) {
    return snap.agents.map((a) => {
      const status = a.status || "unknown";
      /* Restore the tool timeline the backend persisted (see
         master._snapshot_tool_timeline). Without this the reloaded card
         showed no tools/timeline even though the agent used them live. */
      const tools = Array.isArray(a.tools) ? a.tools : [];
      const toolCalls = Array.isArray(a.toolCalls) ? a.toolCalls : [];
      /* Restore the live stopwatch's anchor (backend `startedAt`, epoch ms
         — see master._build_agent_snapshot). The per-agent timer renders only
         while the agent is running AND has a `_startedAt`; that field used to
         be minted client-side from Date.now() and was never persisted, so a
         reload rebuilt stubs WITHOUT it and the timer node disappeared for an
         agent that was still working. The `else if (a.elapsed)` fallback
         cannot cover that case: `elapsed` only exists once the agent has
         finished. Range-checked so a wrong-magnitude value (epoch seconds, or
         a double-converted ms) is dropped rather than rendered as a ~50-year
         / year-58000 elapsed — both fail silently, which is worse than the
         missing timer this restores. */
      let startedAt = 0;
      const rawStart = Number(a.startedAt);
      if (Number.isFinite(rawStart) && rawStart > 1e12 && rawStart <= Date.now()) {
        startedAt = rawStart;
      } else if (a.startedAt != null && a.startedAt !== "") {
        console.warn("[Swarm] implausible startedAt for agent", a.id,
          "=", a.startedAt, "— timer anchor dropped");
      }
      return {
        id: a.id || "",
        role: a.role || "agent",
        model: a.model || "",
        objective: a.objective || "",
        status,
        phase: status,
        preview: a.preview || "",
        elapsed: (a.elapsed === 0 || a.elapsed) ? a.elapsed : "",
        tokens: (a.tokens === 0 || a.tokens) ? a.tokens : "",
        modifiedFiles: typeof a.modifiedFiles === "number" ? a.modifiedFiles : 0,
        error: a.error || "",
        tools,
        _toolCalls: toolCalls,
        _toolCallsOmitted: (typeof a.toolCallsOmitted === "number" && a.toolCallsOmitted > 0)
          ? a.toolCallsOmitted : 0,
        _startedAt: startedAt || undefined,
        /* Stall evidence persisted by the backend (master._build_agent_snapshot)
           — without carrying it here a reloaded stalled card loses its
           「静默 Ns」 label and falls back to the bare phase text. */
        stallSilentSeconds: (a.stallSilentSeconds === 0 || a.stallSilentSeconds)
          ? a.stallSilentSeconds : undefined,
        stallNote: a.stallNote || "",
      };
    });
  }

  const tc = round && round.toolContent;
  if (!tc || typeof tc !== "string") return [];
  let handle;
  try {
    handle = _swarmUnwrapResultPayload(JSON.parse(tc));
  } catch (e) {
    return [];  /* tool result wasn't the JSON handle — nothing to recover */
  }
  const list = (handle && Array.isArray(handle.agents)) ? handle.agents : [];
  /* The live `_swarmAgents` array (synthesized from swarm_* SSE events) is
     gone after a reload, but the agent RESULTS were persisted on sibling
     await_agents / get_agent_result rounds. Cross-reference them so the
     recovered panel shows real status + result, not objective-only stubs. */
  const results = _swarmResultsByAgent(allRounds);
  const enriched = Object.keys(results).length > 0;
  /* Inbox-inject completion proof (root-cause fix — conv mr2ysg473scxv8).
     A `<swarm-update>` drained into the model's context is DEFINITIVE proof
     that agent X finished — and unlike the live `_swarmAgents` map it SURVIVES
     reload, persisted as synthetic `_inboxInject` tool rows (see
     sse_handlers_lifecycle.js `_handleSwarmInboxInject`, which stamps
     `inboxAgentIds` onto rounds pushed into `toolRounds`). Without this, a
     fire-and-forget swarm (no await_agents/get_agent_result sibling rounds,
     no _swarmSnapshot) recovered every agent as `unknown` → the panel showed
     0/N + "Unconfirmed" + 无结果 even though the chips proved the agents
     finished and were injected. Treat an injected agentId as authoritative
     `done` when no stronger sibling-result status exists. */
  const injectedDone = _swarmInjectedAgentIds(allRounds);
  if (list.length === 0) {
    console.warn("[Swarm] _recoverSwarmAgents: spawn handle had no agents[] — panel body will be empty (round", round && round.roundNum, ")");
  } else {
    console.warn("[Swarm] _recoverSwarmAgents: rebuilt", list.length,
      "agent(s) from persisted handle (results cross-referenced from sibling rounds:", enriched,
      "; inbox-injected done:", injectedDone.size, "; round",
      round && round.roundNum, ")");
  }
  return list.map((a) => {
    const id = a.id || "";
    const res = results[id] || {};
    // Precedence: an explicit sibling-round result (await/get_agent_result)
    // wins; else an inbox-inject for this id is authoritative `done`; else it
    // never reported back → keep it visibly unfinished rather than faking a
    // green "done".
    const status = res.status || (injectedDone.has(id) ? "done" : "unknown");
    return {
      id,
      role: res.role || a.role || "agent",
      objective: res.objective || a.objective || "",
      status,
      phase: status,
      preview: res.preview || "",
      elapsed: res.elapsed || "",
      tokens: res.tokens || "",
      error: res.error || "",
      tools: [],
    };
  });
}

/* Build the live swarm panel HTML (used during streaming) */
/* The action-registry DSL cannot express a method call chained into a
   property + another method (`this.closest(sel).classList.toggle(cls)`):
   the receiver `…closest(sel).classList` never resolves, so the click is
   refused with a console error and the panel never toggles. Route the
   swarm panel's collapse/expand through this named action instead. */
function _swarmToggleClass(el, selector, cls) {
  const target = el && typeof el.closest === "function" ? el.closest(selector) : null;
  if (target) target.classList.toggle(cls);
}

/* Agent-ID chip copy — the inline form needed a parenthesised compound and
   setTimeout, both refused by the action DSL, so the chip silently did
   nothing. Named action keeps stopPropagation + clipboard + feedback. */
function _swarmCopyAgentId(el, evt) {
  if (evt && typeof evt.stopPropagation === "function") evt.stopPropagation();
  const token = el && el.dataset ? (el.dataset.grep || "") : "";
  if (token && typeof navigator !== "undefined" && navigator.clipboard) {
    navigator.clipboard.writeText(token);
  }
  if (!el || !el.classList) return;
  el.classList.add("sw-a-id-copied");
  setTimeout(() => el.classList.remove("sw-a-id-copied"), 900);
}
function _buildSwarmPanelHTML(round, allRounds) {
  /* Live path: `_swarmAgents` is populated from swarm_* SSE events.
     Reload path: that field is gone, so recover agents from the persisted
     handle JSON + sibling result rounds — otherwise the completed panel
     renders an empty body. */
  let agents = round._swarmAgents || [];
  if (agents.length === 0) {
    agents = _recoverSwarmAgents(round, allRounds);
  }
  /* Rendering is a pure read. Derive reload/settlement presentation on a
     local value rather than stamping the TurnStore projection object. */
  round = { ...round, _swarmAgents: agents };
  /* #9: a durable snapshot marked settled:true is the authoritative "this
     swarm is over" signal. Stamp the round as settled on reload so the
     staleness guard and the 1Hz ticker can NEVER mis-fire on it (a round
     saved mid-flight with _swarmActive:true but a settled snapshot must read
     as Complete, not tick "Running" toward Stale). Idempotent. */
  if (round._swarmSnapshot && round._swarmSnapshot.settled
      && (round._swarmActive || round._asyncRunning || round.status === "searching"
          || !round._swarmEndTime)) {
    round._swarmActive = false;
    round._asyncRunning = false;
    if (round.status === "searching") round.status = "done";
    if (!round._swarmEndTime) {
      round._swarmEndTime = round._swarmStartTime || Date.now();
    }
  }
  /* Wall-clock age is an offline fallback, never settlement authority. A fresh
     backend `active:true` confirmation suppresses it, so a legitimate long
     swarm cannot be mislabeled Stale; an unreachable zombie still converges. */
  const _swStartedAt = round._swarmStartTime || 0;
  const _backendConfirmedActive = !!round._swActiveConfirmedAt
    && (Date.now() - round._swActiveConfirmedAt) < _SW_ACTIVE_CONFIRM_TTL_MS;
  const isStale = !round._swarmEndTime
    && (round._swarmActive || round._asyncRunning)
    && _swStartedAt > 0
    && (Date.now() - _swStartedAt) > _SW_STALE_MS
    && !_backendConfirmedActive;
  const isActive = !isStale && (round.status === "searching" || round._swarmActive);
  const total = agents.length;
  const running = agents.filter(a => a.status === "running" || a.status === "thinking").length;
  const done = agents.filter(a => a.status === "done" || a.status === "completed").length;
  const failed = agents.filter(a => a.status === "failed" || a.status === "error").length;
  const pending = total - done - failed - running;
  const finished = done + failed;

  /* ── Elapsed timer ── */
  let elapsed = "";
  if (round._swarmStartTime && !isStale) {
    const ms = (round._swarmEndTime || Date.now()) - round._swarmStartTime;
    const sec = Math.floor(ms / 1000);
    elapsed = sec >= 60 ? `${Math.floor(sec / 60)}m${sec % 60}s` : `${sec}s`;
  }
  /* When the panel is still active, expose the start timestamp so a
   * 1Hz ticker can update the elapsed text in place without going
   * through the fingerprint gate (which only fires on real state
   * changes — see _syncToolRoundsDOM). */
  const tickerAttr = (isActive && round._swarmStartTime)
    ? ` data-sw-start="${round._swarmStartTime}"` : "";
  const hasLiveAgentTimer = agents.some((agent) => agent
    && (agent.status === "running" || agent.status === "thinking")
    && agent._startedAt);
  if (tickerAttr || hasLiveAgentTimer) _swEnsureTicker();
  /* Rendering is the demand boundary: conversations outside the active
     surface incur no reconciliation clock. Live/reloaded unresolved panels
     keep a visibility-aware check armed until backend truth settles them. */
  if (isActive || round._swarmActive || round._asyncRunning) {
    _swDemandReconciliation(_SW_RECONCILE_INTERVAL_MS);
  }

  /* ── Header icon ── */
  const headerIcon = isActive
    ? `<span class="sw-header-icon" style="animation:swarmIconBounce 1.2s ease-in-out infinite">${_SW_SVG.hub}</span>`
    : `<span class="sw-header-icon">${_SW_SVG.hub}</span>`;

  /* ── Header subtitle counts ── */
  let headerSubtitle = "";
  if (total > 0) {
    const parts = [];
    if (isActive && running > 0) parts.push(`<span class="sw-cnt-running">${running} running</span>`);
    if (done > 0) parts.push(`<span class="sw-cnt-done">${done} done</span>`);
    if (failed > 0) parts.push(`<span class="sw-cnt-failed">${failed} failed</span>`);
    if (pending > 0 && isActive) parts.push(`${pending} queued`);
    headerSubtitle = `<span class="sw-header-subtitle">${parts.join(" · ")}</span>`;
  } else if (isActive) {
    headerSubtitle = `<span class="sw-header-subtitle">Planning…</span>`;
  }

  /* Async outranks Complete while background agents remain unresolved. */
  const stillRunningAsync = !isStale && !!round._asyncRunning && (running > 0 || pending > 0);
  let statusPill;
  if (isStale) {
    statusPill = `<span class="sw-status-pill sw-pill-stale" title="This swarm panel never received its completion signal (likely a server restart or dropped connection). It will reconcile automatically when the server is reachable.">${_SW_STATUS_SVG.stale} Stale</span>`;
  } else if (total === 0 && isActive) {
    statusPill = `<span class="sw-status-pill sw-pill-planning"><span class="sw-spinner" style="width:10px;height:10px;border-width:1.5px"></span>Planning</span>`;
  } else if (isActive) {
    statusPill = `<span class="sw-status-pill sw-pill-running"><span class="sw-spinner" style="width:10px;height:10px;border-width:1.5px"></span>Running</span>`;
  } else if (stillRunningAsync) {
    const n = running + pending;
    statusPill = `<span class="sw-status-pill sw-pill-async" title="Sub-agents are still working in the background — updates arrive automatically as the conversation continues."><span class="sw-async-dot"></span>${n} running async</span>`;
  } else if (round._swarmError) {
    /* Explicit driver failure outranks inferred agent counts. */
    statusPill = `<span class="sw-status-pill sw-pill-error" title="${escapeHtml(round._swarmError)}">${_SW_STATUS_SVG.failed} Failed</span>`;
  } else if (failed > 0 && done === 0) {
    statusPill = `<span class="sw-status-pill sw-pill-error">${_SW_STATUS_SVG.failed} Failed</span>`;
  } else if (finished === 0 && total > 0
             && !(round._swarmSnapshot && round._swarmSnapshot.settled)) {
    /* No terminal agent or settled snapshot is never Complete. Deliberately
       ignore `_swarmEndTime`: reconciliation freezes it even when truth for
       every agent remains unknown. */
    _swScheduleUnconfirmedProbe(round);
    statusPill = `<span class="sw-status-pill sw-pill-stale" title="This panel was reloaded while its agents were still working and lost its live connection; no agent has reported a final result yet. It will reconcile automatically when the server is reachable.">${_SW_STATUS_SVG.stale} Unconfirmed</span>`;
  } else {
    statusPill = `<span class="sw-status-pill sw-pill-done">${_SW_STATUS_SVG.done} Complete</span>`;
  }

  /* ── Progress bar (only when agents exist) ── */
  let progressBar = "";
  if (total > 0) {
    const pctDone = Math.round((done / total) * 100);
    const pctFailed = Math.round((failed / total) * 100);
    const pctRunning = Math.round((running / total) * 100);
    const fillStyle = (failed > 0 && done > 0) ? ` style="--ok-pct:${pctDone}%"` : "";
    const fillClass = failed > 0 && done > 0 ? " has-errors" : "";
    progressBar = `<div class="sw-progress">` +
      `<div class="sw-progress-track">` +
        `<div class="sw-progress-fill${fillClass}" style="width:${pctDone + pctFailed + pctRunning}%"${fillStyle}></div>` +
      `</div>` +
      `<div class="sw-progress-label">` +
        `<span>${finished}/${total} agents complete</span>` +
        (elapsed ? `<span>${elapsed}</span>` : "") +
      `</div>` +
    `</div>`;
  }

  /* ── Agent cards (collapsible) ── */
  let agentCards = "";
  if (agents.length > 0) {
    agentCards = agents.map((a, i) => {
      const sIcon = _swStatusIcon(a.status);
      const taskNum = `#${i + 1}`;
      const objective = escapeHtml(a.objective || "");
      const phase = a.phase || a.status || "";
      /* FULL answer — the panel is a debugging surface, so the sub-agent's
         result is never clipped. The durable snapshot carries the complete
         text and CSS owns the visual bounding (scroll), not a JS slice. */
      const preview = (a.preview || "");
      /* Backend log token — matches `[Agent:%s]` in lib/swarm/agent.py
         (self.agent_id = f'agent-{role}-{spec.id}') so a user copying
         the chip can grep server logs directly. */
      const role = (a.role || "general");
      const canonicalId = (a.id || "");
      const grepToken = canonicalId ? `agent-${role}-${canonicalId}` : "";
      const roleLabel = escapeHtml(role);
      const idChip = canonicalId
        ? `<span class="sw-a-id" title="Click to copy log ID — grep '${escapeHtml(grepToken)}' in app.log to trace this agent" data-grep="${escapeHtml(grepToken)}" data-tofu-action="_swarmCopyAgentId(this,event)">${escapeHtml(canonicalId)}</span>`
        : "";
      /* Concrete model this agent runs on (spec override → role tier →
         parent default), resolved server-side and sent on spawn / start /
         complete events. */
      const modelChip = a.model
        ? `<span class="sw-a-model" title="Model: ${escapeHtml(a.model)}">${escapeHtml(a.model)}</span>`
        : "";

      /* ── Status class ── */
      let sClass;
      if (a.status === "done" || a.status === "completed") sClass = "sw-a-done";
      else if (a.status === "failed" || a.status === "error") sClass = "sw-a-failed";
      else if (a.status === "stalled") sClass = "sw-a-stalled";
      else if (a.status === "running" || a.status === "thinking") sClass = "sw-a-running";
      else sClass = "sw-a-pending";

      /* ── Phase pill label ── */
      const phaseMap = {
        thinking: t("swarm.phase.thinking"), tool_use: t("swarm.phase.tool_use"), writing: t("swarm.phase.writing"),
        searching: t("swarm.phase.searching"), coding: t("swarm.phase.coding"), analyzing: t("swarm.phase.analyzing"),
        done: t("swarm.phase.complete"), completed: t("swarm.phase.complete"), failed: t("swarm.phase.failed"), error: t("swarm.phase.error"),
        pending: t("swarm.phase.queued"), running: t("swarm.phase.running"), waiting: t("swarm.phase.queued"), queued: t("swarm.phase.queued"),
        retrying: t("swarm.phase.retrying"),
        stalled: t("swarm.phase.stalled"),
        unknown: t("swarm.phase.noResult"),
      };
      /* Status wins for a terminated agent: if status is done/failed but the
         phase got stranded at a spawn-time value (e.g. "waiting" because the
         per-agent events were routed to another panel), show the terminal
         label rather than a contradictory "waiting"/"Queued" pill next to a
         done checkmark (status/phase desync). */
      let phaseLabel;
      if (a.status === "done" || a.status === "completed") phaseLabel = t("swarm.phase.complete");
      else if (a.status === "failed" || a.status === "error") phaseLabel = t("swarm.phase.failed");
      else if (a.status === "stalled") {
        /* Verdict, not mystery: the backend judged this agent silent (see
           master._stalled_agents). Show the measured silence so the card
           answers "why" — the 无结果 bucket is for never-produced only. */
        const sil = Number(a.stallSilentSeconds);
        phaseLabel = Number.isFinite(sil) && sil > 0
          ? t("swarm.phase.stalledSilent", { seconds: Math.round(sil) })
          : t("swarm.phase.stalled");
      }
      else phaseLabel = phaseMap[phase] || phase || t("swarm.phase.queued");

      /* ── Agent elapsed ── */
      let agentTimer = "";
      const aRunning = a.status === "running" || a.status === "thinking";
      if (aRunning && a._startedAt) {
        // Live-ticking timer driven by the 1Hz updater (data-sw-start).
        const sec = Math.max(0, Math.floor((Date.now() - a._startedAt) / 1000));
        const txt = sec >= 60 ? `${Math.floor(sec / 60)}m${sec % 60}s` : `${sec}s`;
        agentTimer = `<span class="sw-a-timer" data-sw-start="${a._startedAt}">${txt}</span>`;
      } else if (a.elapsed) {
        agentTimer = `<span class="sw-a-timer">${a.elapsed}s</span>`;
      }

      /* ── Agent body: objective + tools + preview ── */
      let bodyContent = "";

      // Objective — always show prominently
      if (objective) {
        bodyContent += `<div class="sw-a-objective">${objective}</div>`;
      }

      // Dependency chain
      if (a.dependsOn && a.dependsOn.length > 0) {
        const depHTML = a.dependsOn.map(depId => {
          const depAgent = agents.find(x => x.id === depId);
          const depLabel = depAgent ? `Task ${agents.indexOf(depAgent) + 1}` : depId;
          const depDone = depAgent && (depAgent.status === "done" || depAgent.status === "completed");
          return `<span class="sw-dep-tag ${depDone ? 'sw-dep-done' : ''}">${depDone ? _SW_STATUS_SVG.done + ' ' : ''}${escapeHtml(depLabel)}</span>`;
        }).join("");
        bodyContent += `<div class="sw-a-deps"><span class="sw-a-deps-label">Waits for:</span>${depHTML}</div>`;
      }

      // Tools used — compact inline
      if (a.tools && a.tools.length > 0) {
        const toolHTML = a.tools.slice(-6).map(t => {
          const td = _TOOL_DISPLAY[t];
          const icon = (td && td.icon) ? td.icon : _SW_SVG.tool;
          const label = td ? (td.label || t) : t;
          return `<span class="sw-a-tool-tag" title="${escapeHtml(t)}">${icon} ${label}</span>`;
        }).join("");
        const more = a.tools.length > 6 ? `<span class="sw-a-tool-tag">+${a.tools.length - 6}</span>` : "";
        bodyContent += `<div class="sw-a-tools">${toolHTML}${more}</div>`;
      }

      // Per-tool-call execution timeline — same look as ptool-panel rows.
      // Each row has a status dot, tool name, args brief, and elapsed.
      // Click a row to expand its preview/error.
      const timelineCalls = Array.isArray(a._toolCalls) ? a._toolCalls : [];
      const omittedToolCalls = Number(a._toolCallsOmitted || 0);
      if (timelineCalls.length > 0 || omittedToolCalls > 0) {
        const rowsHTML = timelineCalls.map(c => {
          const dot = c.status === "running" ? '<span class="sw-tl-dot sw-tl-running"></span>'
                    : c.status === "failed"  ? `<span class="sw-tl-dot sw-tl-failed">${_SW_STATUS_SVG.failed}</span>`
                    :                          `<span class="sw-tl-dot sw-tl-done">${_SW_STATUS_SVG.done}</span>`;
          const td = _TOOL_DISPLAY[c.toolName];
          const icon = (td && td.icon) ? td.icon : _SW_SVG.tool;
          const elapsedStr = (typeof c.elapsed === "number") ? `${c.elapsed.toFixed(1)}s` : "";
          const detailIsError = !!c.error || !!c.errorTruncated
            || Number(c.errorFullChars || 0) > 0;
          const detail = detailIsError ? (c.error || "") : (c.preview || "");
          const detailTruncated = detailIsError
            ? !!c.errorTruncated : !!c.previewTruncated;
          const fullChars = Number(detailIsError
            ? c.errorFullChars : c.previewFullChars) || detail.length;
          const omittedChars = Math.max(0, fullChars - detail.length);
          const truncationNote = detailTruncated
            ? `<div class="sw-tl-truncated">${escapeHtml(t("swarm.toolPreviewTruncated", { count: omittedChars.toLocaleString() }))}</div>`
            : "";
          const expandable = !!detail || detailTruncated;
          const onclick = expandable
            ? ` data-tofu-action="event.stopPropagation();this.classList.toggle('sw-tl-open')"` : "";
          return `<div class="sw-tl-row sw-tl-${c.status}${expandable ? ' sw-tl-expandable' : ''}"${onclick}>` +
              `<div class="sw-tl-line">` +
                dot +
                `<span class="sw-tl-icon">${icon}</span>` +
                `<span class="sw-tl-name">${escapeHtml(c.toolName || "?")}</span>` +
                (c.argsBrief ? `<span class="sw-tl-args" title="${escapeHtml(c.argsBrief)}">${escapeHtml(c.argsBrief)}</span>` : "") +
                (elapsedStr ? `<span class="sw-tl-elapsed">${elapsedStr}</span>` : "") +
                (expandable ? `<span class="sw-tl-chev">${_SW_SVG.chevron}</span>` : "") +
              `</div>` +
              (expandable
                ? `<div class="sw-tl-detail${detailIsError ? ' sw-tl-detail-error' : ''}">${escapeHtml(detail)}${truncationNote}</div>`
                : "") +
            `</div>`;
        }).join("");
        const omittedHTML = omittedToolCalls > 0
          ? `<div class="sw-tl-omitted">${escapeHtml(t("swarm.toolCallsOmitted", { count: omittedToolCalls.toLocaleString() }))}</div>`
          : "";
        bodyContent += `<div class="sw-a-timeline">${omittedHTML}${rowsHTML}</div>`;
      }

      // Preview — live stream with typing cursor
      if (preview && (a.status === "running" || a.status === "thinking")) {
        bodyContent += `<div class="sw-a-preview sw-a-preview-live">${escapeHtml(preview)}<span class="sw-typing-cursor">▍</span></div>`;
      } else if (preview && (a.status === "done" || a.status === "completed")) {
        bodyContent += `<div class="sw-a-preview">${escapeHtml(preview)}</div>`;
      } else if (preview && (a.status === "failed" || a.status === "error")) {
        /* A failed agent's error is exactly the text that needs reading in
           full — a 200-char cut hid the cause/stack. */
        bodyContent += `<div class="sw-a-err">${escapeHtml(preview)}</div>`;
      }

      // Meta line
      if (a.tokens || a.elapsed) {
        const metaParts = [];
        if (a.elapsed) metaParts.push(`${a.elapsed}s`);
        if (a.tokens) metaParts.push(`${a.tokens >= 1000000 ? (a.tokens/1000000).toFixed(1) + "m" : a.tokens > 1000 ? (a.tokens/1000).toFixed(1) + "k" : a.tokens} tok`);
        bodyContent += `<div class="sw-a-meta">${metaParts.join(' · ')}</div>`;
      }

      /* Auto-open running agents, collapse done ones */
      const autoOpen = (a.status === "running" || a.status === "thinking") ? " sw-a-open" : "";

      /* File-modification flag — agents that wrote/edited files warrant
         closer review, so mark them with a pencil pill + the edit count. */
      const editCount = _swAgentModifiedCount(a);
      const editPill = editCount > 0
        ? `<span class="sw-a-edited" title="This agent modified ${editCount} file action(s) — review its changes">${_SW_SVG.pencil}${editCount}</span>`
        : "";
      const editedClass = editCount > 0 ? " sw-a-has-edits" : "";

      return `<div class="sw-agent ${sClass}${autoOpen}${editedClass}" data-agent-id="${escapeHtml(a.id || '')}">` +
        `<div class="sw-a-header" data-tofu-action="_swarmToggleClass(this,'.sw-agent','sw-a-open')">` +
          `<span class="sw-a-status-icon">${sIcon}</span>` +
          `<span class="sw-a-num">${taskNum}</span>` +
          `<span class="sw-a-role-tag" title="role">${roleLabel}</span>` +
          idChip +
          modelChip +
          editPill +
          `<span class="sw-a-phase-pill">${phaseLabel}</span>` +
          agentTimer +
          `<span class="sw-a-chevron">${_SW_SVG.chevron}</span>` +
        `</div>` +
        (bodyContent ? `<div class="sw-a-body">${bodyContent}</div>` : "") +
      `</div>`;
    }).join("");
  }

  /* ── Empty-body note ──
     A settled panel whose roster is unrecoverable (no durable snapshot and
     the persisted spawn handle gone or unparseable) used to render
     header-only: the collapse toggle then hid zero elements and the click
     read as dead. Render an honest note so the header always has something
     to expand. */
  let emptyNote = "";
  if (agents.length === 0 && !isActive) {
    emptyNote = `<div class="sw-agent-grid"><div class="sw-empty">${escapeHtml(t("swarm.panelEmpty"))}</div></div>`;
  }
  /* ── Stats footer ── */
  let statsFooter = "";
  const footerParts = [];
  if (total > 0) footerParts.push(`${_SW_SVG.hubSm} ${total} parallel task${total > 1 ? "s" : ""}`);
  if (round._swarmStats) {
    const s = round._swarmStats;
    if (s.totalTokens) footerParts.push(`${s.totalTokens >= 1000000 ? (s.totalTokens/1000000).toFixed(1) + "m" : s.totalTokens > 1000 ? (s.totalTokens/1000).toFixed(1) + "k" : s.totalTokens} tokens`);
    if (s.totalCostUsd) footerParts.push(`$${s.totalCostUsd.toFixed(4)}`);
  }
  if (elapsed) footerParts.push(`${elapsed}`);
  if (footerParts.length > 0) {
    statsFooter = `<div class="sw-footer">${footerParts.join('<span class="sw-footer-sep">·</span>')}</div>`;
  }

  return `<div class="sw-panel${isActive ? ' sw-active' : ' sw-complete'}">` +
    `<div class="sw-header" data-tofu-action="_swarmToggleClass(this,'.sw-panel','sw-collapsed')">` +
      `<div class="sw-header-left">` +
        headerIcon +
        `<div class="sw-header-info">` +
          `<span class="sw-header-title">Parallel Execution</span>` +
          headerSubtitle +
        `</div>` +
      `</div>` +
      `<div class="sw-header-right">` +
        statusPill +
        (elapsed ? `<span class="sw-header-timer"${tickerAttr}>${elapsed}</span>` : "") +
        `<span class="sw-chevron">${_SW_SVG.chevron}</span>` +
      `</div>` +
    `</div>` +
    progressBar +
    (agentCards ? `<div class="sw-agent-grid">${agentCards}</div>` : emptyNote) +
    statsFooter +
  `</div>`;
}

/* ── Stuck swarm-panel reconciler (Option 2) ──
 * The live `_swarmActive` / `_asyncRunning` flags are cleared ONLY by a
 * terminal `swarm_phase:complete` SSE event (sse_handlers_swarm.js) or an
 * inbox-inject that observes all agents terminal (sse_handlers_lifecycle.js).
 * If the server restarts (or the SSE stream drops) after the swarm finished
 * but before that event reaches an open tab, the panel is stuck "Running"
 * forever with no poll loop running to fix it — the exact zombie the user
 * reported. The staleness guard above hides the symptom after _SW_STALE_MS;
 * this reconciler fixes the root state sooner by asking the backend whether
 * the swarm is actually still alive, and settling the panel if not.
 *
 * Runs on a slow interval (not per-second): it's a self-healing sweep, not a
 * hot path. Only probes panels that are (a) flagged active/async, (b) not
 * already frozen (_swarmEndTime), and (c) not explicitly live on its owning
 * Turn. A live round's push path is the authority, so the status probe does
 * not race it; detached unresolved rounds remain eligible. */
function _swarmRoundTaskId(msg, conv, round) {
  /* Probe-key precedence, most-authoritative first:
     1. ``round._swarmKey`` — the backend-stamped session key (conv-scoped),
        carried on the spawning event. ALWAYS resolves to the session.
     2. ``conv.id`` — IS the swarm key for every conv-scoped session
        (``swarm_key_for`` prefers convId). Covers panels persisted before
        the _swarmKey stamp existed.
     3. the SPAWNING task id (``msg._taskId``) — resolves via the server-side
        alias table. ONLY a fallback: probing with a LATER turn's task id
        misses the alias table and the route answers active:false for a LIVE
        swarm — the false-settle
        that froze panels into Unconfirmed with every agent 无结果. */
  return (round && round._swarmKey) || (conv && conv.id)
      || (msg && msg._taskId) || null;
}

function _applyBackendSwarmAgents(round, backendAgents) {
  /* Apply the /api/v1/swarm/status per-agent rows onto the panel roster.
     Shared by the settle path (_settleStuckSwarmRound) and the resurrect
     path (reconciler active===true): the backend vocabulary maps
     'completed'→'done' and carries the failure reason, so a panel that
     re-attaches to a LIVE swarm shows real per-agent progress instead of
     frozen spawn-time stubs. Agents the panel never heard of (a spawn_more
     wave launched while this tab was detached) are grafted on. Returns the
     Set of roster ids that had a backend row (matched + grafted), so the
     settle path can tell "no authoritative row" apart from "backend row". */
  const byId = {};
  for (const ba of (backendAgents || [])) {
    const id = ba && (ba.id || ba.agentId);
    if (id) byId[id] = ba;
  }
  if (!Array.isArray(round._swarmAgents)) round._swarmAgents = [];
  const answered = new Set();
  for (const a of round._swarmAgents) {
    const ba = a && a.id ? byId[a.id] : null;
    if (!ba) continue;
    answered.add(a.id);
    if (ba.status) {
      const ns = ba.status === "completed" ? "done" : ba.status;
      /* Never DEMOTE a terminal card back to a live state — a stale row or
         an id-remap collision must not reopen a done/failed agent. */
      const terminal = a.status === "done" || a.status === "failed";
      if (!terminal) {
        a.status = ns;
        if (ns === "done" && (!a.phase || a.phase === "waiting"
            || a.phase === "running" || a.phase === "thinking"
            || a.phase === "tool_use" || a.phase === "unknown")) {
          a.phase = "done";
        } else if (ns === "failed") {
          a.phase = "error";
        } else if ((ns === "running" || ns === "pending")
                   && (!a.phase || a.phase === "waiting" || a.phase === "unknown")) {
          a.phase = ns;
        }
      }
    }
    /* Error transparency: the backend status carries the failure reason
       (in-memory get_status AND the durable persisted row) — show WHY, not
       just THAT. */
    if (ba.error) {
      a.error = ba.error;
      if (!a.preview) a.preview = ba.error;
    }
    if (ba.role && !a.role) a.role = ba.role;
    if (ba.objective && !a.objective) a.objective = ba.objective;
  }
  /* Graft agents the panel never heard of (spawn_more while detached). */
  for (const ba of (backendAgents || [])) {
    const id = ba && (ba.id || ba.agentId);
    if (!id || answered.has(id)) continue;
    if (round._swarmAgents.some(a => a && a.id === id)) continue;
    const ns = ba.status === "completed" ? "done" : (ba.status || "pending");
    round._swarmAgents.push({
      id,
      role: ba.role || "general",
      objective: ba.objective || "",
      status: ns,
      phase: ns === "done" ? "done" : ns === "failed" ? "error" : ns,
      preview: ba.error || "",
      error: ba.error || "",
      tools: [],
    });
    answered.add(id);
  }
  return answered;
}

function _settleStuckSwarmRound(round, backendAgents) {
  /* Apply backend truth; preserve missing per-agent outcomes as unknown. */
  round._swarmActive = false;
  round._asyncRunning = false;
  if (round.status !== "done") round.status = "done";
  if (!round._swarmEndTime) {
    round._swarmEndTime = Date.now();
    if (round._swarmStartTime) {
      round._elapsed = ((round._swarmEndTime - round._swarmStartTime) / 1000).toFixed(1) + "s";
    }
  }
  const answered = _applyBackendSwarmAgents(round, backendAgents);
  for (const a of (round._swarmAgents || [])) {
    if (a && a.id && answered.has(a.id)) continue;
    if (a.status === "running" || a.status === "thinking"
        || a.status === "pending" || !a.status) {
      /* The swarm ended, but this agent's outcome is not known. */
      a.status = "unknown";
      a.phase = "unknown";
    }
  }
}

const _SW_RECONCILE_INTERVAL_MS = SWARM_RECONCILIATION_POLICY.intervalMs;
const _SW_RECONCILE_FAST_MS = SWARM_RECONCILIATION_POLICY.fastMs;
const _swFastProbeAtByRoundKey = new Map();

function _swarmPresentationRoundKey(round, index) {
  if (round?.toolCallId) return `tool:${round.toolCallId}`;
  if (round?.id) return `id:${round.id}`;
  const llmRound = round?.llmRound == null ? '' : String(round.llmRound);
  const roundNum = round?.roundNum == null ? '' : String(round.roundNum);
  const startedAt = round?._swarmStartTime == null
    ? '' : String(round._swarmStartTime);
  return `round:${llmRound}:${roundNum}:${startedAt}:${index ?? ''}`;
}

function _swScheduleUnconfirmedProbe(round) {
  /* First-truth fast path: rendering an Unconfirmed pill advances the shared
     demand scheduler instead of creating a second, untracked timer class. */
  const now = Date.now();
  const key = _swarmPresentationRoundKey(round);
  const previous = _swFastProbeAtByRoundKey.get(key) || 0;
  if (now - previous < 15000) return;
  _swFastProbeAtByRoundKey.set(key, now);
  if (_swFastProbeAtByRoundKey.size > 256) {
    for (const [candidate, timestamp] of _swFastProbeAtByRoundKey) {
      if (now - timestamp > 60000) _swFastProbeAtByRoundKey.delete(candidate);
    }
  }
  _swDemandReconciliation(_SW_RECONCILE_FAST_MS);
}

const _swReconcileStateByRound = new Map();

function _swReconcileStateKey(convId, turnId, round, index) {
  return `${convId}:${turnId}:${_swarmPresentationRoundKey(round, index)}`;
}

function _swReconcileStateFor(key) {
  const now = Date.now();
  let state = _swReconcileStateByRound.get(key);
  if (!state) {
    state = {
      checked: false,
      unknowns: 0,
      unchangedActivePolls: 0,
      activeFingerprint: '',
      nextProbeAt: 0,
      at: now,
    };
    _swReconcileStateByRound.set(key, state);
  } else {
    state.at = now;
  }
  /* Mirror _swFastProbeAtByRoundKey's self-pruning so per-round reconcile
     state cannot grow unbounded once a round settles and stops being swept. */
  if (_swReconcileStateByRound.size > 256) {
    for (const [candidate, candidateState] of _swReconcileStateByRound) {
      if (now - candidateState.at > 60000) {
        _swReconcileStateByRound.delete(candidate);
      }
    }
  }
  return state;
}

function _swFindPresentationRound(projection, identity, fallbackIndex) {
  const rounds = Array.isArray(projection?.toolRounds)
    ? projection.toolRounds : [];
  const found = rounds.find((round, index) =>
    _swarmPresentationRoundKey(round, index) === identity);
  return found || rounds[fallbackIndex] || null;
}

function _swUpdateReconciledRound(entry, updateRound) {
  const owner = runtimeScope.ConversationSwarmPresentation;
  return owner?.update?.(entry.conv, entry.turnId, (projection) => {
    const round = _swFindPresentationRound(
      projection, entry.roundIdentity, entry.roundIndex,
    );
    if (!round) return false;
    if ((!Array.isArray(round._swarmAgents) || round._swarmAgents.length === 0)
        && typeof _recoverSwarmAgents === 'function') {
      const recovered = _recoverSwarmAgents(round, projection.toolRounds || []);
      if (recovered.length) round._swarmAgents = recovered;
    }
    updateRound(round);
    return true;
  });
}

async function _reconcileStuckSwarmPanelsOnce() {
  if (typeof Api === "undefined" || !Api.swarm || !Api.swarm.status) return null;
  if (typeof conversations === "undefined" || !Array.isArray(conversations)) return null;
  const owner = runtimeScope.ConversationSwarmPresentation;
  if (!owner?.candidates || !owner?.update) return null;
  let nextDelayMs = null;
  const requestFollowup = (delayMs = _SW_RECONCILE_INTERVAL_MS) => {
    const boundedDelay = Math.max(0, Number(delayMs) || 0);
    nextDelayMs = nextDelayMs == null
      ? boundedDelay : Math.min(nextDelayMs, boundedDelay);
  };
  /* Discover immutable Turn identities; probe each backend task once. */
  const probes = new Map();
  for (const conv of conversations) {
    if (!conv?.id) continue;
    for (const turn of owner.candidates(conv)) {
      const projection = turn?.projection || {};
      const rounds = Array.isArray(projection.toolRounds)
        ? projection.toolRounds : [];
      const messageView = {
        ...projection, role: 'assistant', _turnId: turn.turnId,
      };
      rounds.forEach((round, roundIndex) => {
        if (!round?._swarm || round._swarmEndTime) return;
        const stateKey = _swReconcileStateKey(
          conv.id, turn.turnId, round, roundIndex,
        );
        const reconcileState = _swReconcileStateFor(stateKey);
        if (reconcileState.checked) return;
        const now = Date.now();
        if (now < Number(reconcileState.nextProbeAt || 0)) {
          requestFollowup(reconcileState.nextProbeAt - now);
          return;
        }
        /* Explicitly live Turn rounds remain push-owned; detached rounds probe. */
        const turnLive = turn.status === 'pending' || turn.status === 'running';
        if (turnLive && (round._swarmActive || round._asyncRunning)) {
          requestFollowup();
          return;
        }
        let agents = Array.isArray(round._swarmAgents) ? round._swarmAgents : [];
        if (!agents.length && typeof _recoverSwarmAgents === 'function') {
          try { agents = _recoverSwarmAgents(round, rounds); }
          catch (_ignored) { agents = []; }
        }
        if (!(round._swarmActive || round._asyncRunning)) {
          const settledSnapshot = Boolean(
            round._swarmSnapshot?.settled,
          );
          const unresolved = agents.some((agent) => !agent || !agent.status
            || ['pending', 'running', 'thinking', 'unknown', 'stalled',
              'waiting', 'queued'].includes(agent.status));
          if (settledSnapshot || !agents.length || !unresolved) return;
        }
        const taskId = _swarmRoundTaskId(messageView, conv, round);
        if (!taskId) return;
        const entry = {
          conv,
          turnId: turn.turnId,
          roundIdentity: _swarmPresentationRoundKey(round, roundIndex),
          roundIndex,
          roundNum: round.roundNum,
          startedAt: Number(round._swarmStartTime || 0),
          stateKey,
        };
        if (!probes.has(taskId)) probes.set(taskId, []);
        probes.get(taskId).push(entry);
      });
    }
  }
  for (const [taskId, entries] of probes) {
    let status;
    try {
      status = await Api.swarm.status(taskId);
    } catch (error) {
      console.warn('[Swarm] reconcile probe failed task=' +
        String(taskId).slice(0, 8) + ': ' + (error?.message || error));
      requestFollowup();
      continue;
    }
    if (!status) {
      requestFollowup();
      continue;
    }
    if (status.active === false && status.known !== false) {
      for (const entry of entries) {
        console.warn('[Swarm] backend reports task=' +
          String(taskId).slice(0, 8) + ' inactive; settling round ' +
          String(entry.roundNum || ''));
        _swUpdateReconciledRound(entry, (round) => {
          _settleStuckSwarmRound(round, status.agents);
        });
        _swReconcileStateFor(entry.stateKey).checked = true;
      }
      continue;
    }
    if (status.active !== true) {
      const now = Date.now();
      for (const entry of entries) {
        const reconcileState = _swReconcileStateFor(entry.stateKey);
        reconcileState.unchangedActivePolls = 0;
        reconcileState.activeFingerprint = '';
        reconcileState.nextProbeAt = 0;
        reconcileState.unknowns += 1;
        const age = entry.startedAt ? now - entry.startedAt : Infinity;
        if (reconcileState.unknowns < 3 || age <= 60000) {
          requestFollowup();
          continue;
        }
        const terminalAgents = (status.agents || []).filter((agent) => agent
          && ['completed', 'failed', 'done', 'cancelled', 'aborted']
            .includes(agent.status));
        _swUpdateReconciledRound(entry, (round) => {
          _settleStuckSwarmRound(round, terminalAgents);
        });
        reconcileState.checked = true;
      }
      continue;
    }
    const now = Date.now();
    for (const entry of entries) {
      const reconcileState = _swReconcileStateFor(entry.stateKey);
      reconcileState.unknowns = 0;
      const fingerprint = JSON.stringify((status.agents || []).map((agent) => [
        agent?.id || agent?.agentId || '', agent?.status || '',
        agent?.rounds || agent?.roundsUsed || 0,
      ]));
      if (fingerprint === reconcileState.activeFingerprint) {
        reconcileState.unchangedActivePolls += 1;
      } else {
        reconcileState.activeFingerprint = fingerprint;
        reconcileState.unchangedActivePolls = 0;
      }
      /* A detached live swarm has no SSE owner, so retain status recovery but
         back off when successive probes report the same facts. The scheduler
         sleeps directly to the earliest due probe; 20→40→80→120s bounds both
         browser wakeups, request noise, and terminal-detection delay. */
      const backoffMs = Math.min(
        120000,
        _SW_RECONCILE_INTERVAL_MS
          * (2 ** Math.min(reconcileState.unchangedActivePolls, 3)),
      );
      reconcileState.nextProbeAt = now + backoffMs;
      requestFollowup(backoffMs);
      _swUpdateReconciledRound(entry, (round) => {
        round._swActiveConfirmedAt = now;
        round._swarmActive = true;
        if (!round._swarmStartTime && status.created_at) {
          const createdMs = Number(status.created_at) * 1000;
          if (Number.isFinite(createdMs) && createdMs > 1e12
              && createdMs <= now) round._swarmStartTime = createdMs;
        }
        _applyBackendSwarmAgents(round, status.agents);
      });
      if (status.error) {
        console.warn('[Swarm] reconcile status error task=' +
          String(taskId).slice(0, 8) + ': ' + status.error);
      }
    }
  }
  return nextDelayMs;
}

function _swReconcileDocumentHidden() {
  return typeof document !== 'undefined'
    && (document.hidden === true || document.visibilityState === 'hidden');
}

function _swResumeTimerTicker() {
  if (typeof document !== 'undefined'
      && typeof document.querySelector === 'function'
      && document.querySelector('.sw-panel [data-sw-start]')) {
    _swEnsureTicker();
  }
}

function _swSubscribeReconcileVisibility(listener) {
  if (typeof document === 'undefined'
      || typeof document.addEventListener !== 'function') return () => {};
  document.addEventListener('visibilitychange', listener);
  return () => document.removeEventListener('visibilitychange', listener);
}

const _swReconciliationScheduler = createSwarmReconciliationScheduler({
  schedule: {
    now: Date.now,
    setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
    clearTimeout: (handle) => globalThis.clearTimeout(handle),
  },
  visibility: {
    isHidden: _swReconcileDocumentHidden,
    subscribe: _swSubscribeReconcileVisibility,
  },
  reconcile: _reconcileStuckSwarmPanelsOnce,
  onHidden: _swStopTimerTicker,
  onVisible: _swResumeTimerTicker,
  onError: (error) => console.warn('[Swarm] reconcile cycle failed: ' +
    (error?.message || error)),
});

function _swDemandReconciliation(delayMs = _SW_RECONCILE_INTERVAL_MS) {
  if (typeof window === 'undefined') return;
  _swReconciliationScheduler.demand(delayMs);
}

/* Update elapsed nodes in place without defeating the render fingerprint. */
function _tickSwarmTimers() {
  const els = document.querySelectorAll('.sw-panel [data-sw-start]');
  if (!els.length) {
    /* A future live render re-arms, so an empty tick stops immediately. */
    _swStopTimerTicker();
    return;
  }
  const now = Date.now();
  for (const el of els) {
    const start = +el.getAttribute('data-sw-start');
    if (!start) continue;
    /* Freeze zombie elapsed text at the same offline-staleness boundary. */
    if (now - start > _SW_STALE_MS) continue;
    const sec = Math.max(0, Math.floor((now - start) / 1000));
    const txt = sec >= 60 ? `${Math.floor(sec / 60)}m${sec % 60}s` : `${sec}s`;
    if (el.textContent !== txt) el.textContent = txt;
  }
}
/* Lazy 1Hz ticker: armed only when rendering a live elapsed-time node and
   stopped on its first empty DOM tick. */
function _swStopTimerTicker() {
  if (runtimeScope._swTimerTicker != null
      && typeof clearInterval === 'function') {
    clearInterval(runtimeScope._swTimerTicker);
  }
  runtimeScope._swTimerTicker = null;
}
function _swEnsureTicker() {
  if (typeof window !== 'undefined' && !_swReconcileDocumentHidden()
      && runtimeScope._swTimerTicker == null
      && typeof setInterval === 'function') {
    runtimeScope._swTimerTicker = setInterval(_tickSwarmTimers, 1000);
  }
}

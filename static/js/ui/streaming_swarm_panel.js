/* ═══════════════════════════════════════════════════════════════════
   ui/streaming_swarm_panel.js — extracted from ui/streaming_ui.js (split 2026-06-27)

   Swarm "Parallel Execution" panel rendering + the stuck-panel reconciler.
   This is a self-contained LEAF cluster: it produces panel HTML and runs a
   self-healing reconciliation sweep. Its public builders are called from
   ui/streaming_ui.js (_syncToolRoundsDOM, updateStreamingUI) and
   ui/tool_rounds.js — all via shared window scope, so no exports/imports.

   Contents (moved verbatim — NO logic change):
     _buildSwarmInboxChipsHTML, _SW_SVG, _SW_FILE_WRITE_TOOLS,
     _swAgentModifiedCount, _SW_STATUS_SVG, _SW_STALE_MS, _swStatusIcon,
     _swarmResultsByAgent, _recoverSwarmAgents, _buildSwarmPanelHTML,
     _buildSwarmDoneHTML, _swarmRoundTaskId, _settleStuckSwarmRound,
     _reconcileStuckSwarmPanels (+ ticker), _tickSwarmTimers (+ ticker).

   Concatenated by lib/js_bundler.py BEFORE ui/streaming_ui.js — symbols
   share the same window scope as every other static/js/*.js file.
   ═══════════════════════════════════════════════════════════════════ */

/* ★ Build the inbox-inject chip(s) — one per round that received
 *    swarm-update notifications.  Tells the user "the model received
 *    N async sub-agent updates before this turn".  Same vocabulary as
 *    .sw-status-pill (amber + monospace) so it reads as part of the
 *    swarm flow, not a generic system notice.                          */
function _buildSwarmInboxChipsHTML(injects) {
  if (!Array.isArray(injects) || injects.length === 0) return "";
  /* Aggregate: collapse multiple injects into one chip per round so the
     user doesn't see N stacked chips when the same round received N
     batched <swarm-update>s. */
  const byRound = new Map();
  for (const inj of injects) {
    const key = inj.round || 0;
    const cur = byRound.get(key) || { round: key, count: 0, agentIds: [] };
    cur.count += inj.count || 0;
    for (const id of (inj.agentIds || [])) {
      if (id && !cur.agentIds.includes(id)) cur.agentIds.push(id);
    }
    byRound.set(key, cur);
  }
  const chips = [];
  for (const inj of byRound.values()) {
    const ids = (inj.agentIds || []).slice(0, 4);
    const idsExtra = inj.agentIds.length > 4
      ? ` +${inj.agentIds.length - 4}` : '';
    const idsLabel = ids.length
      ? `<span class="sw-inbox-chip-ids">[${ids.map(escapeHtml).join(', ')}${idsExtra}]</span>`
      : '';
    const word = inj.count === 1 ? 'update' : 'updates';
    chips.push(
      `<div class="sw-inbox-chip" title="Sub-agents pushed ${inj.count} update${inj.count === 1 ? '' : 's'} into the model's next round.">` +
        `<span class="sw-inbox-chip-icon"><svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.5h13L22 12v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z"/></svg></span>` +
        `<span class="sw-inbox-chip-text">received</span> ` +
        `<span class="sw-inbox-chip-count">${inj.count}</span> ` +
        `<span class="sw-inbox-chip-text">async swarm ${word}</span>` +
        idsLabel +
      `</div>`
    );
  }
  return chips.join("");
}

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
};
/* Tool names that mutate files on disk — mirrors lib/swarm/master.py
   _FILE_WRITE_TOOLS. Used to flag sub-agents that edited the workspace. */
const _SW_FILE_WRITE_TOOLS = new Set([
  "write_file", "apply_diff", "apply_diffs", "insert_content", "insert_contents",
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
/* Wall-clock age after which an UNSETTLED swarm panel (still flagged
   _swarmActive / _asyncRunning, no _swarmEndTime) is treated as STALE: a
   tab that never received the terminal `swarm_phase:complete` event — e.g.
   the server restarted, or the SSE stream dropped before settling. Beyond
   any realistic swarm wave, so it only ever fires on a genuine zombie. The
   active backend probe (_reconcileStuckSwarmPanels) settles such panels
   sooner when the server is reachable; this is the offline fallback so an
   open tab still self-corrects visually without a manual refresh. */
const _SW_STALE_MS = 30 * 60 * 1000;
/* How long a backend `active===true` confirmation (stamped on the round by
   _reconcileStuckSwarmPanels as `_swActiveConfirmedAt`) suppresses the
   wall-clock staleness guess. The reconciler sweeps every 20s and skips a
   conv that is actively streaming (its own SSE/poll is authoritative there),
   so a live long swarm gets re-confirmed each sweep; 90s (>4 sweeps) tolerates
   a couple of missed/slow sweeps before the age fallback is allowed to speak
   again. This is what makes `isStale` a genuine OFFLINE residual: it can only
   fire when the backend fact is ABSENT or STALE (server unreachable), never
   against a fresh known-active verdict. */
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
/* Scan ALL tool rounds of the current message for the agent results that
   ARE persisted — the `await_agents` rounds (each `completed[]` entry has
   agent_id/status/elapsed/tokens/preview) and the `get_agent_result` rounds
   (single agent + full `final_answer`). Returns a map keyed by agent_id so
   `_recoverSwarmAgents` can repaint real per-agent status + result after a
   reload, instead of objective-only stubs. */
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
    try { payload = JSON.parse(r.toolContent); } catch (e) { continue; }
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
  /* ★ Durable snapshot (root-cause fix) — preferred source after reload.
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
      /* ★ Restore the live stopwatch's anchor (backend `startedAt`, epoch ms
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
    handle = JSON.parse(tc);
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
  /* ★ Inbox-inject completion proof (root-cause fix — conv mr2ysg473scxv8).
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

/* ★ Build the live swarm panel HTML (used during streaming) */
function _buildSwarmPanelHTML(round, allRounds) {
  _swEnsureTicker();
  /* Live path: `_swarmAgents` is populated from swarm_* SSE events.
     Reload path: that field is gone, so recover agents from the persisted
     handle JSON + sibling result rounds — otherwise the completed panel
     renders an empty body. */
  let agents = round._swarmAgents || [];
  if (agents.length === 0) {
    agents = _recoverSwarmAgents(round, allRounds);
  }
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
  /* ── Staleness guard (age fallback, OFFLINE residual only) ──
     A panel still flagged active/async with no frozen end time, whose start is
     older than _SW_STALE_MS, MIGHT be a zombie: the terminal
     swarm_phase:complete event never reached this tab (server restart /
     dropped SSE).

     Root-cause guard (FE-inference-debt #1): the wall-clock age is a GUESS and
     must NEVER override a known backend fact. _reconcileStuckSwarmPanels probes
     Api.swarm.status(taskId) and, when the backend reports `active===true`,
     stamps `_swActiveConfirmedAt` on the round. While that confirmation is
     fresh, the swarm is AUTHORITATIVELY still alive (a big multi-agent wave can
     legitimately run well past 30 min) — so we suppress the age guess entirely.
     The age fallback is thus reachable ONLY when NO fresh backend fact exists
     (server unreachable → the reconciler's probe returned null / never ran), so
     an open offline tab still self-corrects a genuine zombie without a manual
     refresh, exactly as before — but a genuinely long, backend-confirmed-alive
     swarm is never mislabeled "Stale". */
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

  /* ── Status pill ──
     "Async" wins over "Complete" while ANY agent is still running in the
     background and the main agent has moved on.  This is the visual
     anchor for our async-fire-and-forget model — the user sees that the
     swarm panel above hasn't actually settled yet, and to expect more
     <swarm-update> chips to land on later turns.                       */
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
  } else if (failed > 0 && done === 0) {
    statusPill = `<span class="sw-status-pill sw-pill-error">${_SW_STATUS_SVG.failed} Failed</span>`;
  } else if (finished === 0 && total > 0
             && !(round._swarmSnapshot && round._swarmSnapshot.settled)) {
    /* No agent has reported a terminal result (done/failed) AND we have no
       authoritative settled snapshot. This is a reloaded-but-still-running
       panel whose live `_swarmActive` flag was lost, OR one the reconciler
       just settled (which sets _swarmEndTime but leaves unreported agents
       `unknown`): the agents are in `unknown`/`pending` limbo (e.g. wedged on
       upstream gateway 500s), NOT finished. Rendering a green "Complete" here
       is the false-positive that contradicts the per-agent "No result" cards.
       NOTE: this must NOT also require `!_swarmEndTime` — _settleStuckSwarmRound
       freezes _swarmEndTime, so gating on it would let a reconciled all-unknown
       panel fall through to the green "Complete" else-branch. Show
       "Unconfirmed" instead. */
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
      const shortId = (a.id || "").slice(0, 8);
      const grepToken = a.id ? `agent-${role}-${shortId}` : "";
      const roleLabel = escapeHtml(role);
      const idChip = a.id
        ? `<span class="sw-a-id" title="Click to copy log ID — grep '${escapeHtml(grepToken)}' in app.log to trace this agent" data-grep="${escapeHtml(grepToken)}" onclick="event.stopPropagation();(navigator.clipboard&&navigator.clipboard.writeText(this.dataset.grep));this.classList.add('sw-a-id-copied');setTimeout(()=>this.classList.remove('sw-a-id-copied'),900);">${escapeHtml(shortId)}</span>`
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
      if (a._toolCalls && a._toolCalls.length > 0) {
        const rowsHTML = a._toolCalls.map(c => {
          const dot = c.status === "running" ? '<span class="sw-tl-dot sw-tl-running"></span>'
                    : c.status === "failed"  ? `<span class="sw-tl-dot sw-tl-failed">${_SW_STATUS_SVG.failed}</span>`
                    :                          `<span class="sw-tl-dot sw-tl-done">${_SW_STATUS_SVG.done}</span>`;
          const td = _TOOL_DISPLAY[c.toolName];
          const icon = (td && td.icon) ? td.icon : _SW_SVG.tool;
          const elapsedStr = (typeof c.elapsed === "number") ? `${c.elapsed.toFixed(1)}s` : "";
          const detail = c.error || c.preview || "";
          const expandable = !!detail;
          const onclick = expandable
            ? ` onclick="event.stopPropagation();this.classList.toggle('sw-tl-open')"` : "";
          return `<div class="sw-tl-row sw-tl-${c.status}${expandable ? ' sw-tl-expandable' : ''}"${onclick}>` +
              `<div class="sw-tl-line">` +
                dot +
                `<span class="sw-tl-icon">${icon}</span>` +
                `<span class="sw-tl-name">${escapeHtml(c.toolName || "?")}</span>` +
                (c.argsBrief ? `<span class="sw-tl-args" title="${escapeHtml(c.argsBrief)}">${escapeHtml(c.argsBrief)}</span>` : "") +
                (elapsedStr ? `<span class="sw-tl-elapsed">${elapsedStr}</span>` : "") +
                (expandable ? `<span class="sw-tl-chev">▾</span>` : "") +
              `</div>` +
              (detail
                ? `<div class="sw-tl-detail${c.error ? ' sw-tl-detail-error' : ''}">${escapeHtml(detail)}</div>`
                : "") +
            `</div>`;
        }).join("");
        bodyContent += `<div class="sw-a-timeline">${rowsHTML}</div>`;
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

      /* ★ File-modification flag — agents that wrote/edited files warrant
         closer review, so mark them with a pencil pill + the edit count. */
      const editCount = _swAgentModifiedCount(a);
      const editPill = editCount > 0
        ? `<span class="sw-a-edited" title="This agent modified ${editCount} file action(s) — review its changes">${_SW_SVG.pencil}${editCount}</span>`
        : "";
      const editedClass = editCount > 0 ? " sw-a-has-edits" : "";

      return `<div class="sw-agent ${sClass}${autoOpen}${editedClass}" data-agent-id="${escapeHtml(a.id || '')}">` +
        `<div class="sw-a-header" onclick="this.closest('.sw-agent').classList.toggle('sw-a-open')">` +
          `<span class="sw-a-status-icon">${sIcon}</span>` +
          `<span class="sw-a-num">${taskNum}</span>` +
          `<span class="sw-a-role-tag" title="role">${roleLabel}</span>` +
          idChip +
          modelChip +
          editPill +
          `<span class="sw-a-phase-pill">${phaseLabel}</span>` +
          agentTimer +
          `<span class="sw-a-chevron">▾</span>` +
        `</div>` +
        (bodyContent ? `<div class="sw-a-body">${bodyContent}</div>` : "") +
      `</div>`;
    }).join("");
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
    `<div class="sw-header" onclick="this.closest('.sw-panel').classList.toggle('sw-collapsed')">` +
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
        `<span class="sw-chevron">▾</span>` +
      `</div>` +
    `</div>` +
    progressBar +
    (agentCards ? `<div class="sw-agent-grid">${agentCards}</div>` : "") +
    statsFooter +
  `</div>`;
}

/* ★ Build the done HTML specifically for swarm rounds — reuses the panel layout */
function _buildSwarmDoneHTML(round, showNums, allRounds) {
  /* Prefer the full panel: live `_swarmAgents`, else agents recovered from
     the persisted handle JSON + sibling result rounds (post-reload). */
  if ((round._swarmAgents && round._swarmAgents.length > 0)
      || _recoverSwarmAgents(round, allRounds).length > 0) {
    const patchedRound = Object.assign({}, round, { _swarmActive: false });
    return _buildSwarmPanelHTML(patchedRound, allRounds);
  }
  /* No agents and no results — don't render empty swarm panels */
  const results = round.results || [];
  if (!results.length && !round._swarmAgents?.length) return "";
  /* Fallback: historical saved data without agent details — compact summary */
  const snippet = results[0]?.snippet || "";
  const elapsed = round._elapsed || "";
  return `<div class="sw-panel sw-complete">` +
    `<div class="sw-header">` +
      `<div class="sw-header-left">` +
        `<span class="sw-header-icon">${_SW_SVG.hub}</span>` +
        `<div class="sw-header-info">` +
          `<span class="sw-header-title">Parallel Execution</span>` +
        `</div>` +
      `</div>` +
      `<div class="sw-header-right">` +
        `<span class="sw-status-pill sw-pill-done">${_SW_STATUS_SVG.done} Complete</span>` +
        (elapsed ? `<span class="sw-header-timer">${elapsed}</span>` : "") +
      `</div>` +
    `</div>` +
    (snippet ? `<div class="sw-footer" style="opacity:0.7">${escapeHtml(snippet)}</div>` : "") +
  `</div>`;
}

/* ── In-place panel morph (flicker fix) ──
 * Every swarm_* SSE event (per-agent phase, each streamed preview char, each
 * tool-call tick) previously did `slot.innerHTML = _buildSwarmPanelHTML(...)`,
 * tearing down and recreating the ENTIRE `.sw-panel` subtree many times a
 * second. Two visible consequences: (1) `.sw-panel.sw-active` carries
 * `animation:swarmBorderPulse 2.5s infinite`, and a brand-new node RESTARTS
 * that animation from 0% on every rebuild → the border/box-shadow flashes;
 * (2) any agent card / tool-call row the user manually expanded collapses.
 *
 * `_morphSwarmSlot` patches the existing panel IN PLACE instead: it reuses
 * the live `.sw-panel` DOM node (animation clock keeps running uninterrupted),
 * recurses the tree syncing only changed attributes + text, and treats the
 * user-toggle classes below as OLD-node-authoritative so an expanded card
 * survives a re-render — the same surgical-diff principle the 1 Hz timer
 * ticker already uses for `[data-sw-start]`, and that `renderChat`'s
 * `data-mfp` diff uses for message bubbles. Node identity is preserved by
 * index (agents/tool-calls are append-only and never reordered), so an
 * agent's ID-remap just updates `data-agent-id` on the same node. */
const _SW_PRESERVE_CLASSES = ["sw-collapsed", "sw-a-open", "sw-tl-open"];

function _swSyncAttrs(oldEl, newEl) {
  /* Remove attributes gone from the new render. */
  for (const attr of Array.from(oldEl.attributes)) {
    if (attr.name !== "class" && !newEl.hasAttribute(attr.name)) {
      oldEl.removeAttribute(attr.name);
    }
  }
  /* Add / update changed attributes (class handled separately). */
  for (const attr of Array.from(newEl.attributes)) {
    if (attr.name === "class") continue;
    if (oldEl.getAttribute(attr.name) !== attr.value) {
      oldEl.setAttribute(attr.name, attr.value);
    }
  }
  /* Class: take the new class set, but let the OLD node's user-toggle
     classes win — a card the user expanded (added `sw-a-open`) or a panel
     they collapsed (`sw-collapsed`) must not be reset by the fresh render. */
  const newSet = new Set((newEl.getAttribute("class") || "").split(/\s+/).filter(Boolean));
  for (const c of _SW_PRESERVE_CLASSES) {
    if (oldEl.classList.contains(c)) newSet.add(c);
    else newSet.delete(c);
  }
  const finalCls = Array.from(newSet).join(" ");
  if ((oldEl.getAttribute("class") || "") !== finalCls) {
    oldEl.setAttribute("class", finalCls);
  }
}

function _swMorphNode(oldNode, newNode) {
  /* Text node → update value only when it actually changed (no-op = no
     flicker while a preview streams char-by-char). */
  if (oldNode.nodeType === 3 && newNode.nodeType === 3) {
    if (oldNode.nodeValue !== newNode.nodeValue) oldNode.nodeValue = newNode.nodeValue;
    return;
  }
  /* Different node type, or different element tag → replace outright. */
  if (oldNode.nodeType !== newNode.nodeType
      || (oldNode.nodeType === 1 && oldNode.tagName !== newNode.tagName)) {
    if (oldNode.parentNode) oldNode.parentNode.replaceChild(newNode.cloneNode(true), oldNode);
    return;
  }
  if (oldNode.nodeType === 1) {
    _swSyncAttrs(oldNode, newNode);
    _swMorphChildren(oldNode, newNode);
  }
  /* comments / other node types: leave untouched */
}

function _swMorphChildren(oldParent, newParent) {
  const oldNodes = Array.from(oldParent.childNodes);
  const newNodes = Array.from(newParent.childNodes);
  for (let i = 0; i < newNodes.length; i++) {
    const on = oldNodes[i];
    if (!on) {
      /* New trailing node (e.g. a freshly-spawned agent card, an appended
         tool-call row) — clone it in; only this new node touches the DOM. */
      oldParent.appendChild(newNodes[i].cloneNode(true));
      continue;
    }
    _swMorphNode(on, newNodes[i]);
  }
  /* Remove surplus old children (from the tail, so indices stay valid). */
  for (let i = oldNodes.length - 1; i >= newNodes.length; i--) {
    oldParent.removeChild(oldNodes[i]);
  }
}

/* Patch `slot`'s existing swarm panel toward `html` in place. Falls back to a
   full `innerHTML` set on first render or if the panel root is absent /
   structurally different (a genuine replace, not a per-event churn). */
function _morphSwarmSlot(slot, html) {
  const existing = slot.firstElementChild;
  if (!existing || !(existing.classList && existing.classList.contains("sw-panel"))) {
    slot.innerHTML = html;
    return;
  }
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  const fresh = tpl.content.firstElementChild;
  if (!fresh || fresh.tagName !== existing.tagName) {
    slot.innerHTML = html;
    return;
  }
  _swMorphNode(existing, fresh);
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
 * already frozen (_swarmEndTime), and (c) NOT owned by a conversation that is
 * currently streaming (activeStreams) — a live stream's own SSE/poll path is
 * the authority there, so we must not race it. */
function _swarmRoundTaskId(msg, conv) {
  /* The owning task id lives on the assistant message (_taskId, stamped from
     the done/poll payload) and falls back to the conv's active task. */
  return (msg && msg._taskId) || (conv && conv.activeTaskId) || null;
}

function _settleStuckSwarmRound(round, backendAgents) {
  /* Mirror the swarm_phase:complete settle, but for a panel the terminal
     event never reached. When the backend handed us real per-agent statuses
     (session still in memory), apply them; otherwise (session evicted after a
     restart) leave any still-running/pending agent as 'unknown' rather than
     fabricating a green "done" — same honesty as _recoverSwarmAgents. */
  round._swarmActive = false;
  round._asyncRunning = false;
  if (round.status !== "done") round.status = "done";
  if (!round._swarmEndTime) {
    round._swarmEndTime = Date.now();
    if (round._swarmStartTime) {
      round._elapsed = ((round._swarmEndTime - round._swarmStartTime) / 1000).toFixed(1) + "s";
    }
  }
  const byId = {};
  for (const ba of (backendAgents || [])) {
    const id = ba && (ba.id || ba.agentId);
    if (id) byId[id] = ba;
  }
  for (const a of (round._swarmAgents || [])) {
    const ba = a.id ? byId[a.id] : null;
    if (ba && ba.status) {
      a.status = ba.status === "completed" ? "done" : ba.status;
      if (a.status === "done" && (!a.phase || a.phase === "waiting"
          || a.phase === "running" || a.phase === "thinking" || a.phase === "tool_use")) {
        a.phase = "done";
      } else if (a.status === "failed") {
        a.phase = "error";
      }
    } else if (a.status === "running" || a.status === "thinking"
               || a.status === "pending" || !a.status) {
      /* No authoritative status and still mid-flight on screen — the swarm
         is provably over (backend says inactive) but this tab never saw the
         result, so don't claim success. */
      a.status = "unknown";
      a.phase = "unknown";
    }
  }
}

async function _reconcileStuckSwarmPanels() {
  if (typeof Api === "undefined" || !Api.swarm || !Api.swarm.status) return;
  if (typeof conversations === "undefined" || !Array.isArray(conversations)) return;
  /* Collect candidate (conv, msg, round, taskId) tuples first, then probe
     each distinct task once. */
  const probes = new Map();   // taskId -> [{conv, round}]
  for (const conv of conversations) {
    if (!conv || !Array.isArray(conv.messages)) continue;
    /* A streaming conv owns its own reconciliation via SSE/poll — skip it. */
    if (typeof activeStreams !== "undefined" && activeStreams.has(conv.id)) continue;
    for (const msg of conv.messages) {
      if (!msg || msg.role !== "assistant" || !Array.isArray(msg.toolRounds)) continue;
      for (const round of msg.toolRounds) {
        if (!round || !round._swarm) continue;
        if (round._swarmEndTime) continue;                       // already settled
        if (!(round._swarmActive || round._asyncRunning)) continue;
        if (round._swReconcileChecked) continue;                 // one probe per panel
        const taskId = _swarmRoundTaskId(msg, conv);
        if (!taskId) continue;
        if (!probes.has(taskId)) probes.set(taskId, []);
        probes.get(taskId).push({ conv, round });
      }
    }
  }
  if (probes.size === 0) return;
  for (const [taskId, entries] of probes) {
    let status;
    try {
      status = await Api.swarm.status(taskId);
    } catch (e) {
      console.warn("[Swarm] reconcile probe failed task=" + String(taskId).slice(0, 8) + ": " + (e && e.message));
      continue;   // transient — retry on a later sweep (panel stays unchecked)
    }
    /* onError:'null' turns an HTTP/network failure into a null body. Don't
       treat that as authoritative — leave the panel unchecked so a later
       sweep retries (server may be mid-restart, the very moment we care). */
    if (!status) continue;
    if (status.active === false) {
      const convsToRender = new Set();
      for (const { conv, round } of entries) {
        console.warn("[Swarm] _reconcileStuckSwarmPanels: backend reports task=" +
          String(taskId).slice(0, 8) + " inactive — settling stuck panel (round " + (round.roundNum) + ")");
        _settleStuckSwarmRound(round, status.agents);
        round._swReconcileChecked = true;   // definitive answer; _swarmEndTime now also excludes it
        convsToRender.add(conv);
      }
      for (const conv of convsToRender) {
        try {
          if (typeof activeConvId !== "undefined" && conv.id === activeConvId) {
            window.ConvView.replaceAll(conv.id);
          }
          if (typeof saveConversations === "function") saveConversations(conv.id);
        } catch (e) {
          console.warn("[Swarm] reconcile re-render failed: " + (e && e.message));
        }
      }
    }
    /* status.active === true → genuinely still running. Stamp a
       backend-authoritative liveness fact on every panel of this task so the
       wall-clock staleness guess (_SW_STALE_MS) is SUPPRESSED while this
       confirmation is fresh — a long, backend-confirmed-alive swarm must never
       render "Stale". The stamp is refreshed each sweep; when the backend
       later reports inactive (or becomes unreachable) the confirmation ages
       out and the normal settle / offline-fallback paths take over. */
    if (status.active === true) {
      const now = Date.now();
      const convsToRender = new Set();
      for (const { conv, round } of entries) {
        round._swActiveConfirmedAt = now;
        convsToRender.add(conv);
      }
      for (const conv of convsToRender) {
        try {
          if (typeof activeConvId !== "undefined" && conv.id === activeConvId) {
            window.ConvView.replaceAll(conv.id);
          }
          if (typeof saveConversations === "function") saveConversations(conv.id);
        } catch (e) {
          console.warn("[Swarm] reconcile active-confirm re-render failed: " + (e && e.message));
        }
      }
    }
  }
}
if (typeof window !== 'undefined' && !window._swReconcileTicker) {
  window._swReconcileTicker = setInterval(() => {
    try {
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
      _reconcileStuckSwarmPanels();
    } catch (e) { /* swallowed — reconciler is best-effort self-healing */ }
  }, 20000);
}

/* ── 1 Hz wall-clock ticker for swarm timers ──
 * The fingerprint gate in _syncToolRoundsDOM (correctly) skips re-renders
 * when nothing changes — but elapsed-time strings DO change every second
 * even when no SSE event landed. Rather than churn the gate with a
 * fake per-second fingerprint, we update [data-sw-start] elements in
 * place: zero re-render, single timer, ~O(N agents) per tick. */
function _tickSwarmTimers() {
  const els = document.querySelectorAll('.sw-panel [data-sw-start]');
  if (!els.length) {
    /* Idle-stop: no live timers for 60s → stop the 1Hz ticker. Re-armed by
     *   _buildSwarmPanelHTML the next time a swarm panel renders. */
    if (++_swTickerIdleTicks >= 60 && window._swTimerTicker) {
      clearInterval(window._swTimerTicker);
      window._swTimerTicker = null;
    }
    return;
  }
  _swTickerIdleTicks = 0;
  const now = Date.now();
  for (const el of els) {
    const start = +el.getAttribute('data-sw-start');
    if (!start) continue;
    /* Don't tick a runaway zombie timer forever: once past the staleness
       cap, freeze the text so the panel doesn't read "408m9s and counting"
       after a server restart ate the completion event. The pill itself flips
       to "Stale" via _buildSwarmPanelHTML; this just stops the live number. */
    if (now - start > _SW_STALE_MS) continue;
    const sec = Math.max(0, Math.floor((now - start) / 1000));
    const txt = sec >= 60 ? `${Math.floor(sec / 60)}m${sec % 60}s` : `${sec}s`;
    if (el.textContent !== txt) el.textContent = txt;
  }
}
/* Lazy 1Hz ticker: armed by _buildSwarmPanelHTML when a swarm panel exists,
 *   self-stops after 60 idle seconds. A booted page with no swarm activity
 *   no longer spins 1Hz forever (pt_3cd6cd48). */
let _swTickerIdleTicks = 0;
function _swEnsureTicker() {
  if (typeof window !== 'undefined' && !window._swTimerTicker) {
    _swTickerIdleTicks = 0;
    window._swTimerTicker = setInterval(_tickSwarmTimers, 1000);
  }
}

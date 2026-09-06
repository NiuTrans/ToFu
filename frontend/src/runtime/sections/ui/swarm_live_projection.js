/* ===== migrated source: ui/swarm_live_projection.js ===== */
/* Ephemeral swarm progress projection for conversation-scoped push frames. */

function _handleSwarmPhase(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantProjection = c.assistantProjection;
      /* Master-level swarm lifecycle: planning → spawning → wave_start → complete */
      if (!assistantProjection.toolRounds) assistantProjection.toolRounds = [];
      /* The push adapter has already selected the owning Turn. Post-reload,
         target its most recent UNSETTLED swarm round
         (no _swarmEndTime). A live event is positive evidence that panel is
         its addressee — the persisted round lost both the live flags and
         (usually) the _swarmRoundNum hint, so without this fallback every
         push-mirrored event was silently dropped and the panel sat
         "Unconfirmed" for the swarm's whole lifetime. A conversation runs
         one swarm wave at a time and settled rounds are excluded, so a
         finished wave can't steal a new wave's events. */
      const _mostRecentUnsettled = (m) => {
        const rounds = (m && m.toolRounds) || [];
        for (let i = rounds.length - 1; i >= 0; i--) {
          if (rounds[i] && rounds[i]._swarm && !rounds[i]._swarmEndTime) return rounds[i];
        }
        return null;
      };
      const _findSwarmRound = () => {
        const rn = assistantProjection._swarmRoundNum;
        /* When _swarmRoundNum is known, match it exactly. When it is NOT
           known, fall back to an ACTIVE panel only — never the first
           `_swarm` round, which could be a stale/empty (ghost) spawn round
           and would steal this swarm's events. */
        const inCurrent = (assistantProjection.toolRounds || []).find(
          r => r._swarm && (rn ? r.roundNum === rn : (r._swarmActive || r._asyncRunning)));
        if (inCurrent) return inCurrent;
        return _mostRecentUnsettled(assistantProjection);
      };
      if (ev.phase === "spawning" || ev.phase === "planning" || ev.phase === "spawn_more") {
        /* Upgrade the existing tool_start round into a swarm panel */
        let sr = _findSwarmRound();
        const agentData = (ev.agents || []).map((a, i) => ({
          id: a.agentId || a.id || `agent-${i}`,
          role: a.role || "general",
          model: a.model || "",
          objective: a.objective || "",
          context: a.context || "",
          dependsOn: a.depends_on || a.dependsOn || [],
          status: "pending",
          phase: "waiting",
          preview: "",
          tools: [],
        }));
        if (sr) {
          sr.query = "Agent Swarm";
          sr._swarmActive = true;
          sr._swarmStartTime = sr._swarmStartTime || Date.now();
          /* Backend-authoritative session key (conv-scoped). The reconciler
             probes /api/v1/swarm/status with THIS — never a guessed task id,
             whose alias may not exist (false-settle root cause). */
          if (ev.swarmKey) sr._swarmKey = ev.swarmKey;
          if (ev.phase === "spawn_more" && agentData.length) {
            /* Append new agents from spawn_more — don't replace existing ones */
            if (!sr._swarmAgents) sr._swarmAgents = [];
            const existingIds = new Set(sr._swarmAgents.map(a => a.id));
            for (const ad of agentData) {
              if (!existingIds.has(ad.id)) sr._swarmAgents.push(ad);
            }
          } else if (agentData.length) {
            sr._swarmAgents = agentData;
          }
        } else {
          sr = {
            roundNum: (assistantProjection.toolRounds.length + 1),
            query: "Agent Swarm",
            results: null,
            status: "searching",
            toolName: "spawn_agents",
            _swarm: true,
            _swarmActive: true,
            _swarmStartTime: Date.now(),
            _swarmKey: ev.swarmKey || undefined,
            _swarmAgents: agentData,
          };
          assistantProjection.toolRounds.push(sr);
          assistantProjection._swarmRoundNum = sr.roundNum;
        }
      } else if (ev.phase === "error") {
        /* Driver-level crash (backend swarm_phase:error). This is THE error
           transparency path: settle the panel as Failed-with-reason instead
           of letting it drift into the Unconfirmed limbo. The terminal
           swarm_phase:complete frame follows right behind (it carries the
           same error field) and must not re-promote agents to a false done. */
        const sr = _findSwarmRound();
        if (sr) {
          sr._swarmError = ev.error || ev.content || "Swarm driver error";
          sr._swarmActive = false;
          sr._asyncRunning = false;
          if (!sr._swarmEndTime) {
            sr._swarmEndTime = Date.now();
            if (sr._swarmStartTime) {
              sr._elapsed = ((sr._swarmEndTime - sr._swarmStartTime) / 1000).toFixed(1) + "s";
            }
          }
          for (const a of (sr._swarmAgents || [])) {
            if (a.status === "pending" || a.status === "running"
                || a.status === "thinking" || !a.status) {
              /* The driver died mid-flight: no result was ever reported for
                 this agent — honest 'unknown' (无结果), not a fabricated done. */
              a.status = "unknown";
              a.phase = "unknown";
            }
          }
        }
      } else if (ev.phase === "complete") {
        /* Swarm finished — every agent terminated. Drop the async-
           running badge so the panel reads as truly complete.       */
        const sr = _findSwarmRound();
        if (sr) {
          sr.status = "done";
          sr._swarmActive = false;
          sr._asyncRunning = false;
          /* A driver error rides the terminal frame too (backend emits
             swarm_phase:error first, then complete with the same field). */
          if (ev.error) sr._swarmError = ev.error;
          // Freeze the wall-clock end so _buildSwarmPanelHTML doesn't keep
          // sliding the header timer forward via Date.now() on later re-renders.
          sr._swarmEndTime = Date.now();
          const elapsed = sr._swarmStartTime ? ((sr._swarmEndTime - sr._swarmStartTime) / 1000).toFixed(1) + "s" : "";
          sr._elapsed = elapsed;
          sr._swarmStats = {
            totalTokens: ev.totalTokens || 0,
            totalCostUsd: ev.totalCost || 0,
            agentCount: ev.agentCount || 0,
            failedCount: ev.failedCount || 0,
          };
          /* Update agent data from final results */
          if (ev.agents && sr._swarmAgents) {
            for (const ea of ev.agents) {
              const agent = sr._swarmAgents.find(a => a.id === ea.agentId || a.id === ea.id);
              if (agent) {
                agent.status = ea.status === "completed" ? "done" : (ea.status || "done");
                if (ea.model) agent.model = ea.model;
                if (ea.preview || ea.summary) agent.preview = ea.preview || ea.summary;
                if (ea.elapsed) agent.elapsed = ea.elapsed;
                if (ea.tokens) agent.tokens = ea.tokens;
              }
            }
          }
          for (const a of (sr._swarmAgents || [])) {
            /* After a driver error, an unreported agent stays 'unknown' —
               promoting it to done would be a false green over crashed work. */
            if (a.status === "pending" || a.status === "running") {
              a.status = sr._swarmError ? "unknown" : "done";
              if (a.status === "unknown") a.phase = "unknown";
            }
            /* Advance phase in lockstep with status — otherwise an agent
               whose per-agent events were routed elsewhere stays frozen at
               its spawn-time phase ("waiting") and renders a "waiting" pill
               next to a done checkmark (status/phase desync). */
            if (a.status === "done" &&
                (a.phase === "waiting" || a.phase === "starting" ||
                 a.phase === "pending" || a.phase === "running" ||
                 a.phase === "thinking" || a.phase === "tool_use" || !a.phase)) {
              a.phase = "done";
            }
          }
        }
      }
}

function _handleSwarmAgent(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantProjection = c.assistantProjection;
      /* The push adapter selects the owning Turn before invoking this reducer. */
      let _findOwningSwarmRound = () => {
        /* Strict ownership match: the panel must contain this agent_id.
           Returning a panel that does NOT own the agent would silently
           graft the agent onto the wrong panel (B11). Scan ALL `_swarm`
           rounds, not just the first: a multi-wave turn (spawn_agents
           called again in the same assistant turn — e.g. after a continued
           generation) has several swarm panels, and checking only the first
           one failed ownership for every later-wave agent. tool_call and
           progress events have no fallback below, so they were silently
           dropped — the panel showed agent cards but never the tool
           timeline. */
        const rounds = assistantProjection.toolRounds || [];
        const owner = rounds.find(
          r => (r._swarmActive || r._swarm)
            && (r._swarmAgents || []).some(a => a.id === ev.agentId),
        );
        if (owner) return owner;
        /* Post-reload hydration: `_swarmAgents` is live-only, so after a
           page reload the durable spawn round has an empty roster while its
           persisted `_swarmSnapshot` still names every agent. Rebuild the
           missing cards from THIS round's own snapshot (never another
           round's — that would be the B11 cross-panel graft) so live
           tool_call/progress events reattach to real cards instead of
           vanishing until the agent's next phase event. */
        if (ev.agentId && typeof _recoverSwarmAgents === "function") {
          for (const r of rounds) {
            if (!r || !(r._swarmActive || r._swarm)) continue;
            const snapAgents = r._swarmSnapshot && r._swarmSnapshot.agents;
            if (!Array.isArray(snapAgents)
                || !snapAgents.some(a => a && a.id === ev.agentId)) continue;
            const recovered = _recoverSwarmAgents(r, rounds);
            const have = new Set((r._swarmAgents || []).map(a => a.id));
            const missing = recovered.filter(a => a.id && !have.has(a.id));
            if (!missing.length) continue;
            r._swarmAgents = (r._swarmAgents || []).concat(missing);
            return r;
          }
        }
        /* Genuinely new agent (e.g. its phase event raced ahead of the
           spawning event) — only the swarm_agent_phase handler creates new
           agent cards. Pick the ACTIVE panel; if none is active yet (the
           spawning event hasn't landed), fall back to the LAST `_swarm`
           round — the most recent spawn, i.e. the one this wave belongs to.
           NEVER the first `_swarm` round, which may be a stale/empty (ghost)
           spawn round left over from a prior errored spawn_agents — grafting
           onto it splits one swarm across two panels. progress / complete /
           error events return null and become no-ops, preventing accidental
           cross-panel writes. */
        /* #6: terminal/phase events for an agent whose card doesn't exist yet
           (its start/phase event raced behind this one) resolve to the ACTIVE
           panel so the handler can CREATE the card rather than drop the event.
           progress stays no-op (it only refines an existing card). NEVER the
           first `_swarm` round — that may be a stale ghost spawn; prefer the
           active panel, else the most recent spawn (this wave). */
        if (_swarm_evtype === "swarm_agent_phase"
            || _swarm_evtype === "swarm_agent_complete"
            || _swarm_evtype === "swarm_agent_error") {
          const active = rounds.find(r => r._swarm && (r._swarmActive || r._asyncRunning));
          if (active) return active;
          for (let i = rounds.length - 1; i >= 0; i--) {
            if (rounds[i]._swarm) return rounds[i];
          }
          return null;
        }
        return null;
      };
      const _swarm_evtype = ev.type;
      /* A live per-agent event is positive liveness evidence: resurrect the
         panel's active flag so the pill leaves the "Unconfirmed" limbo the
         moment the first event lands — no waiting for the 20s reconciler
         sweep. Terminal events (complete/error) don't resurrect; the
         swarm_phase:complete frame owns the settle. */
      const _findOwningSwarmRoundLive = _findOwningSwarmRound;
      _findOwningSwarmRound = () => {
        const sr = _findOwningSwarmRoundLive();
        if (sr && !sr._swarmEndTime && !sr._swarmActive
            && _swarm_evtype !== "swarm_agent_complete"
            && _swarm_evtype !== "swarm_agent_error") {
          sr._swarmActive = true;
        }
        return sr;
      };

      if (_swarm_evtype === "swarm_agent_phase") {
      /* An individual agent changed phase (starting, thinking, tool_use, done, error) */
      const sr = _findOwningSwarmRound();
      if (sr) {
        if (!sr._swarmAgents) sr._swarmAgents = [];
        let agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        if (!agent && ev.agentId) {
          /* ID not found — check if there's an existing agent with the same
             objective that hasn't been matched yet (stale from spawning event).
             This happens when the spawning event uses placeholder IDs that
             differ from the actual agent IDs assigned by the scheduler. */
          if (ev.objective) {
            const objNorm = ev.objective.trim().toLowerCase();
            agent = sr._swarmAgents.find(a =>
              a.id !== ev.agentId &&
              !a._idConfirmed &&
              (a.status === "pending" || a.status === "running" || a.phase === "starting" || a.phase === "queued" || !a.phase) &&
              a.objective && (a.objective.trim().toLowerCase().startsWith(objNorm) || objNorm.startsWith(a.objective.trim().toLowerCase()))
            );
          }
          if (agent) {
            /* Re-map: update the stale placeholder ID to the real agent ID */
            agent.id = ev.agentId;
            agent._idConfirmed = true;
          } else {
            /* Genuinely new agent (e.g. from spawn_more) — add dynamically */
            agent = { id: ev.agentId, role: ev.role || "agent", model: ev.model || "", objective: ev.objective || "",
                      status: "running", phase: "starting", preview: "", tools: [], _idConfirmed: true };
            sr._swarmAgents.push(agent);
          }
        }
        if (agent) agent._idConfirmed = true;
        if (agent) {
          agent.status = ev.status || agent.status;
          agent.phase = ev.phase || agent.phase;
          if (ev.model) agent.model = ev.model;
          if (ev.preview || ev.summary) agent.preview = ev.preview || ev.summary;
          if (ev.objective) agent.objective = ev.objective;
          if (ev.error) agent.preview = errorEnvelopeMessage(ev.error) || (typeof ev.error === 'string' ? ev.error : '');
          if (ev.elapsed) agent.elapsed = ev.elapsed;
          if (ev.tokens) agent.tokens = ev.tokens;
          /* Stamp a frontend-side start time on first transition to a
           * running phase so the agent card can show a live ticking
           * timer (the backend only sends `elapsed` on completion). */
          if (!agent._startedAt && (agent.status === "running" || agent.status === "thinking")) {
            agent._startedAt = Date.now();
          }
        }
      }
      } else if (_swarm_evtype === "swarm_agent_progress") {
      /* Agent progress: tool usage, partial results, etc. */
      const sr = _findOwningSwarmRound();
      if (sr && sr._swarmAgents) {
        const agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        if (agent) {
          agent.status = ev.status || "running";
          agent.phase = ev.phase || agent.phase;
          if (ev.preview) agent.preview = ev.preview;
          if (ev.toolNames) {
            agent.phase = "tool_use";
            if (!agent.tools) agent.tools = [];
            for (const tn of ev.toolNames) {
              if (!agent.tools.includes(tn)) agent.tools.push(tn);
            }
            agent.preview = `Using ${ev.toolNames.join(", ")}`;
          }
        }
      }
      } else if (_swarm_evtype === "swarm_agent_complete") {
      /* Individual agent finished */
      const sr = _findOwningSwarmRound();
      if (sr) {
        if (!sr._swarmAgents) sr._swarmAgents = [];
        let agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        /* Fallback: match by objective if ID doesn't match (ID remap) */
        if (!agent && ev.objective) {
          const objNorm = ev.objective.trim().toLowerCase();
          agent = sr._swarmAgents.find(a =>
            a.objective && (a.objective.trim().toLowerCase().startsWith(objNorm) || objNorm.startsWith(a.objective.trim().toLowerCase())) &&
            a.status !== "done" && a.status !== "failed"
          );
          if (agent) agent.id = ev.agentId;
        }
        /* #6: out-of-order SSE — `complete` arrived before any `start`/`phase`
           event created this agent's card. Don't drop the terminal result;
           create the card now so the agent shows its real outcome instead of
           vanishing until the swarm_phase:complete sweep. Keyed by agentId. */
        if (!agent && ev.agentId) {
          agent = { id: ev.agentId, role: ev.role || "agent", model: ev.model || "",
                    objective: ev.objective || "", status: "running", phase: "running",
                    preview: "", tools: [], _idConfirmed: true };
          sr._swarmAgents.push(agent);
        }
        if (agent) {
          const failed = ev.status === "failed" || ev.status === "error" || !!ev.error;
          agent.status = failed ? "failed" : "done";
          agent.phase = failed ? "error" : "done";
          if (ev.preview || ev.summary) agent.preview = ev.preview || ev.summary;
          if (ev.elapsed) agent.elapsed = ev.elapsed;
          if (ev.tokens) agent.tokens = ev.tokens;
          if (typeof ev.modifiedFiles === "number") agent.modifiedFiles = ev.modifiedFiles;
          if (ev.error) agent.preview = errorEnvelopeMessage(ev.error) || (typeof ev.error === 'string' ? ev.error : '');
        }
      }
      } else if (_swarm_evtype === "swarm_agent_error") {
      const sr = _findOwningSwarmRound();
      if (sr) {
        if (!sr._swarmAgents) sr._swarmAgents = [];
        let agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        /* #6: terminal error raced ahead of start/phase — create the card so
           the failure is shown, not dropped. */
        if (!agent && ev.agentId) {
          agent = { id: ev.agentId, role: ev.role || "agent", model: ev.model || "",
                    objective: ev.objective || "", status: "failed", phase: "error",
                    preview: "", tools: [], _idConfirmed: true };
          sr._swarmAgents.push(agent);
        }
        if (agent) {
          agent.status = "failed";
          agent.phase = "error";
          agent.preview = errorEnvelopeMessage(ev.error) || (typeof ev.error === 'string' ? ev.error : '') || ev.content || "Agent failed";
        }
      }
      } else if (_swarm_evtype === "swarm_agent_tool_call") {
      /* Per-tool-call timeline entry from a sub-agent.
       * callStatus: 'running' (start) | 'done' | 'failed'
       * Keyed by callId so the start event creates the row and the
       * finish event updates the same row in place. */
      const sr = _findOwningSwarmRound();
      if (sr && sr._swarmAgents && ev.callId) {
        const agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        if (agent) {
          if (!agent._toolCalls) agent._toolCalls = [];
          let entry = agent._toolCalls.find(c => c.callId === ev.callId);
          if (!entry) {
            entry = { callId: ev.callId, toolName: ev.toolName || "?",
                      argsBrief: ev.argsBrief || "", status: "running",
                      startedAt: Date.now() };
            agent._toolCalls.push(entry);
            /* Keep only the last 30 calls per agent to bound memory
             * for long-running agents — older calls drop off the
             * timeline but the agent's own history is unaffected. */
            if (agent._toolCalls.length > 30) {
              const dropped = agent._toolCalls.length - 30;
              agent._toolCalls.splice(0, dropped);
              agent._toolCallsOmitted = (agent._toolCallsOmitted || 0) + dropped;
            }
          }
          if (ev.callStatus) entry.status = ev.callStatus;
          if (typeof ev.callElapsed === "number") entry.elapsed = ev.callElapsed;
          if (ev.preview) entry.preview = ev.preview;
          if (ev.error) entry.error = ev.error;
          if (typeof ev.previewTruncated === "boolean") entry.previewTruncated = ev.previewTruncated;
          if (typeof ev.previewFullChars === "number") entry.previewFullChars = ev.previewFullChars;
          if (typeof ev.errorTruncated === "boolean") entry.errorTruncated = ev.errorTruncated;
          if (typeof ev.errorFullChars === "number") entry.errorFullChars = ev.errorFullChars;
          if (ev.toolName) entry.toolName = ev.toolName;
          if (ev.argsBrief) entry.argsBrief = ev.argsBrief;
        }
      }
      }  /* end inner _swarm_evtype dispatch */

}

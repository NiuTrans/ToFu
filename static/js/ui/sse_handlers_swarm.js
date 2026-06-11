/* SSE swarm_* handlers — extracted from ui/sse_pipeline.js dispatchSSEEvent (2026-06).
   Property-only handlers (no closure-local reassignment) taking (ev, c)
   where c is a snapshot of the dispatch ctx. Bodies are byte-for-byte the
   originals. Concatenated by lib/js_bundler.py BEFORE ui/sse_pipeline.js.
   See tests/test_frontend_sse_dispatch.py for the behavior contract. */

function _handleSwarmPhase(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      /* Master-level swarm lifecycle: planning → spawning → wave_start → complete */
      if (!assistantMsg.toolRounds) assistantMsg.toolRounds = [];
      /* Async swarm: ``complete`` may fire AFTER assistantMsg has rotated
         to a different turn (the user got intermediate messages while
         agents kept running in the background).  Walk all assistant
         messages in the active conversation as a fallback so the
         original swarm panel still gets its terminal status update. */
      const _findSwarmRound = () => {
        const rn = assistantMsg._swarmRoundNum;
        /* When _swarmRoundNum is known, match it exactly. When it is NOT
           known, fall back to an ACTIVE panel only — never the first
           `_swarm` round, which could be a stale/empty (ghost) spawn round
           and would steal this swarm's events. */
        const inCurrent = (assistantMsg.toolRounds || []).find(
          r => r._swarm && (rn ? r.roundNum === rn : (r._swarmActive || r._asyncRunning)));
        if (inCurrent) return inCurrent;
        if (ev.phase !== "complete") return null;
        /* Only complete events trigger the cross-message walk — spawning
           events should always create or upgrade in the current turn. */
        const conv = conversations.find(c => c && c.id === convId);
        if (!conv) return null;
        for (let i = conv.messages.length - 1; i >= 0; i--) {
          const m = conv.messages[i];
          if (!m || m.role !== "assistant" || !m.toolRounds) continue;
          const sr = m.toolRounds.find(r => r._swarm && (r._swarmActive || r._asyncRunning));
          if (sr) return sr;
        }
        return null;
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
            roundNum: (assistantMsg.toolRounds.length + 1),
            query: "Agent Swarm",
            results: null,
            status: "searching",
            toolName: "spawn_agents",
            _swarm: true,
            _swarmActive: true,
            _swarmStartTime: Date.now(),
            _swarmAgents: agentData,
          };
          assistantMsg.toolRounds.push(sr);
          assistantMsg._swarmRoundNum = sr.roundNum;
        }
      } else if (ev.phase === "complete") {
        /* Swarm finished — every agent terminated. Drop the async-
           running badge so the panel reads as truly complete.       */
        const sr = _findSwarmRound();
        if (sr) {
          sr.status = "done";
          sr._swarmActive = false;
          sr._asyncRunning = false;
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
            if (a.status === "pending" || a.status === "running") a.status = "done";
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
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

}

function _handleSwarmAgent(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      /* Per-agent updates may arrive AFTER the spawning assistantMsg has
         rotated to a different turn (async swarm).  Look up the swarm
         panel in the current msg first, then walk back through the
         conv's assistant messages until we find the one that owns this
         agent_id (or a still-active panel). */
      const _findOwningSwarmRound = () => {
        /* Strict ownership match: the panel must contain this agent_id.
           Returning a panel that does NOT own the agent would silently
           graft the agent onto the wrong panel (B11). */
        const inCurrent = (assistantMsg.toolRounds || []).find(r => r._swarmActive || r._swarm);
        if (inCurrent && (inCurrent._swarmAgents || []).some(a => a.id === ev.agentId)) {
          return inCurrent;
        }
        const conv = conversations.find(c => c && c.id === convId);
        if (conv) {
          for (let i = conv.messages.length - 1; i >= 0; i--) {
            const m = conv.messages[i];
            if (!m || m.role !== "assistant" || !m.toolRounds) continue;
            for (const r of m.toolRounds) {
              if (!r._swarm) continue;
              if ((r._swarmAgents || []).some(a => a.id === ev.agentId)) return r;
            }
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
        if (_swarm_evtype === "swarm_agent_phase") {
          const rounds = assistantMsg.toolRounds || [];
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
              (a.status === "pending" || a.status === "running" || a.phase === "starting" || a.phase === "Queued" || !a.phase) &&
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
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

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
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

      } else if (_swarm_evtype === "swarm_agent_complete") {
      /* Individual agent finished */
      const sr = _findOwningSwarmRound();
      if (sr && sr._swarmAgents) {
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
        if (agent) {
          agent.status = ev.status === "failed" ? "failed" : "done";
          agent.phase = ev.status === "failed" ? "error" : "done";
          if (ev.preview || ev.summary) agent.preview = ev.preview || ev.summary;
          if (ev.elapsed) agent.elapsed = ev.elapsed;
          if (ev.tokens) agent.tokens = ev.tokens;
          if (typeof ev.modifiedFiles === "number") agent.modifiedFiles = ev.modifiedFiles;
          if (ev.error) agent.preview = errorEnvelopeMessage(ev.error) || (typeof ev.error === 'string' ? ev.error : '');
        }
      }
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

      } else if (_swarm_evtype === "swarm_agent_error") {
      const sr = _findOwningSwarmRound();
      if (sr && sr._swarmAgents) {
        const agent = sr._swarmAgents.find(a => a.id === ev.agentId);
        if (agent) {
          agent.status = "failed";
          agent.phase = "error";
          agent.preview = errorEnvelopeMessage(ev.error) || (typeof ev.error === 'string' ? ev.error : '') || ev.content || "Agent failed";
        }
      }
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

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
              agent._toolCalls.splice(0, agent._toolCalls.length - 30);
            }
          }
          if (ev.callStatus) entry.status = ev.callStatus;
          if (typeof ev.callElapsed === "number") entry.elapsed = ev.callElapsed;
          if (ev.preview) entry.preview = ev.preview;
          if (ev.error) entry.error = ev.error;
          if (ev.toolName) entry.toolName = ev.toolName;
          if (ev.argsBrief) entry.argsBrief = ev.argsBrief;
        }
      }
      if (buf) buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);
      }  /* end inner _swarm_evtype dispatch */

}

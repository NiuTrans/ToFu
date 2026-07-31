/* ═══════════════════════════════════════════════════════════════════
   ui/swarm_push.js — Cross-turn swarm panel updates via /api/push.

   THE PROBLEM this fixes: an async swarm spawned on turn T keeps running
   after turn T ends (the orchestrator DETACHes it — see
   lib/tasks_pkg/orchestrator.py). Its later progress / completion events
   were pushed only onto turn T's SSE stream, which is already closed, so
   the swarm panel's "N running async" badge spun forever and never
   settled — even though the agents had finished. The user complaint:
   「一轮任务结束了，但 sub agent 还没执行完，就一直卡在 running 不更新」.

   THE FIX: lib/swarm/integration.py now ALSO mirrors every swarm event
   onto the conversation-scoped /api/push WebSocket ('swarm' channel,
   taskId = convId). That channel is global to the browser tab and
   survives turn end. This module subscribes to it and replays each frame
   through the SAME handlers the live SSE path uses (_handleSwarmPhase /
   _handleSwarmAgent), so the detached swarm's panel updates and settles
   the moment its agents finish — with or without an active chat turn.

   Concatenated by lib/js_bundler.py AFTER ui/sse_pipeline.js +
   ui/sse_handlers_swarm.js (handlers must exist) and core/conversations.js
   + push.js (conversations / pushSubscribe). Symbols share window scope.
   ═══════════════════════════════════════════════════════════════════ */

(function _wireServerPushSwarm() {
  if (typeof pushSubscribe !== "function") return;
  if (window.__swarmPushWired) return;
  window.__swarmPushWired = true;

  /* Find the assistant message in `conv` that owns a swarm panel — used as
     the `assistantMsg` context for the replayed handlers. The handlers also
     walk conv.messages internally to locate the owning panel, so a stub is
     acceptable; preferring the real owner just keeps mutations on the right
     object and lets create-on-the-fly agent cards land in the right place. */
  function _findSwarmOwner(conv) {
    if (!conv || !conv.messages) return null;
    for (let i = conv.messages.length - 1; i >= 0; i--) {
      const m = conv.messages[i];
      if (!m || m.role !== "assistant" || !m.toolRounds) continue;
      if (m.toolRounds.some(r => r._swarm)) return m;
    }
    return null;
  }

  /* Attach the frontend to a backend-initiated continuation turn (Phase 2).
     When a swarm settles with unread <swarm-update>s and no live turn, the
     server auto-starts a chat turn that drains them (see
     lib/swarm/integration.py::_start_autocontinue_turn). The browser never
     POSTed it, so nothing would open its SSE stream — we do it here, the
     same way startup orphan-recovery (main_init_tasks.js) reconnects. */
  function _attachAutoContinue(convId, taskId) {
    if (!convId || !taskId) return;
    if (typeof connectToTask !== "function") return;
    if (typeof activeStreams !== "undefined" && activeStreams.has(convId)) return;
    const conv = (typeof conversations !== "undefined")
      ? conversations.find(c => c && c.id === convId) : null;
    if (!conv) return;
    /* Ensure there's a trailing assistant placeholder to stream into so the
       SSE state-snapshot doesn't replay into a prior completed turn. */
    const last = conv.messages && conv.messages[conv.messages.length - 1];
    if (!last || last.role !== "assistant" || last.content || last.finishReason) {
      const placeholder = {
        role: "assistant", content: "", thinking: "", timestamp: Date.now(),
        toolRounds: [], _swarmAutoContinue: true,
        model: conv.model || (typeof config !== "undefined" && config.model)
               || (typeof serverModel !== "undefined" ? serverModel : ""),
      };
      if (typeof _ensureMsgId === "function") _ensureMsgId(placeholder);
      conv.messages = conv.messages || [];
      conv.messages.push(placeholder);
    }
    conv.activeTaskId = taskId;
    console.info(`[SwarmPush] ↻ attaching to auto-continue turn conv=${convId.slice(0,8)} task=${taskId.slice(0,8)}`);
    connectToTask(convId, taskId);
  }

  pushSubscribe("swarm", "*", (frame) => {
    try {
      if (!frame || !frame.type) return;
      const convId = frame.taskId || frame.convId;   // we push under taskId = convId
      if (!convId || convId === "*") return;

      /* Backend-initiated continuation turn — attach to its SSE stream.
         The new task id rides in `newTaskId` (frame.taskId is the routing
         convId, see lib/swarm/integration.py). */
      if (frame.type === "swarm_autocontinue_started") {
        _attachAutoContinue(convId, frame.newTaskId);
        return;
      }

      /* While a chat turn is actively streaming for this conversation, the
         SSE pipeline is the authoritative source for swarm events (it owns
         the live assistantMsg + streaming buffer). Skip the push mirror to
         avoid double-processing / duplicate panels. The turn-less case —
         exactly the stuck-badge bug — is where this subscriber earns its
         keep. */
      if (typeof activeStreams !== "undefined" && activeStreams.has(convId)) {
        return;
      }

      const conv = (typeof conversations !== "undefined")
        ? conversations.find(c => c && c.id === convId) : null;
      /* Conversation not in memory (never opened this session) — nothing to
         mutate. On next load the panel rebuilds from the persisted
         spawn_agents handle with agents stubbed as 'done' (not stuck), so
         dropping the event here is safe. */
      if (!conv) return;

      const owner = _findSwarmOwner(conv) || { toolRounds: [] };
      const c = {
        convId,
        taskId: convId,
        assistantMsg: owner,
        buf: null,
        epCriticPhase: false,
        epCriticMsg: null,
        epCriticBuf: null,
      };

      const t = frame.type;
      if (t === "swarm_phase") {
        if (typeof _handleSwarmPhase === "function") _handleSwarmPhase(frame, c);
      } else if (t === "swarm_agent_phase" || t === "swarm_agent_progress"
                 || t === "swarm_agent_complete" || t === "swarm_agent_error"
                 || t === "swarm_agent_tool_call") {
        if (typeof _handleSwarmAgent === "function") _handleSwarmAgent(frame, c);
      } else {
        return;  // unrelated frame (ping / other channel leak)
      }

      /* The handlers call twUpdate(convId), which repaints the STREAMING
         zone. But a detached swarm's panel lives in a COMMITTED assistant
         message (no active stream), so twUpdate is a no-op for it. Force a
         full re-render of the active conversation so the settled panel
         actually repaints. Only when this conv is on-screen and idle. */
      if (typeof activeConvId !== "undefined" && activeConvId === convId
          && (typeof activeStreams === "undefined" || !activeStreams.has(convId))
          && typeof renderChat === "function") {
        /* Bypass renderChat's Guard 2 fingerprint skip. That guard keys on
           _convRenderFingerprint, which inspects ONLY the last message — but
           a detached swarm's panel often sits on an EARLIER message (the user
           got intermediate replies while agents kept running). Clearing the
           cached fingerprint forces renderChat past the guard into the
           surgical per-message diff path, where _msgFingerprint (now
           swarm-aware) repaints exactly the panel whose agent state changed.
           forceScroll stays false so a user reading scrolled-up history is
           NOT yanked to the bottom on every background agent update. */
        if (typeof _lastRenderedFingerprint !== "undefined") _lastRenderedFingerprint = "";
        window.ConvView.replaceAll(convId, { forceScroll: false });
      }
    } catch (e) {
      console.debug("[SwarmPush] handler error:", e && e.message);
    }
  });

  console.info("[SwarmPush] ✓ subscribed to conv-scoped swarm push channel");
})();

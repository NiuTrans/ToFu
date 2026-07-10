/* SSE lifecycle/async handlers — extracted from ui/sse_pipeline.js dispatchSSEEvent (2026-06).
   Property-only handlers (no closure-local reassignment) taking (ev, c),
   a snapshot of the dispatch ctx. Bodies are byte-for-byte the originals
   (the trailing dispatcher-level `return false` for sse_timeout / round_committed
   stays at the call site, not here). Concatenated BEFORE ui/sse_pipeline.js.
   Behavior contract: tests/test_frontend_sse_dispatch.py. */

function _handleSwarmInboxInject(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      /* ── Async swarm: <swarm-update> messages were just drained from the
       *    inbox and prepended to the model's next round as user messages.
       *    Stamp a chip on the assistant bubble so the human user sees
       *    "the model just received N async updates from sub-agents on
       *    this round" — same instant as the model itself sees them.    */
      if (!assistantMsg._inboxInjects) assistantMsg._inboxInjects = [];
      assistantMsg._inboxInjects.push({
        round:    ev.round,
        count:    ev.count || 0,
        agentIds: Array.isArray(ev.agentIds) ? ev.agentIds.filter(Boolean) : [],
        ts:       Date.now(),
      });
      /* ★ Also surface the injection as a chronological row inside the
       *   ptool-panel — not just an ephemeral streaming-zone chip. This
       *   makes "the model received these sub-agent results, here, in
       *   order" visible in the unified tool timeline AND persisted to
       *   DB (survives reload), honoring the frontend-visibility
       *   principle. The row is a synthetic toolRound flagged
       *   `_inboxInject`; _renderUnifiedToolLine renders it specially.
       *   Dedup by round so SSE replay / poll fallback doesn't double it. */
      if (!assistantMsg.toolRounds) assistantMsg.toolRounds = [];
      const _injKey = "inbox:" + (ev.round || 0);
      if (!assistantMsg.toolRounds.some(r => r._inboxInject && r._inboxKey === _injKey)) {
        assistantMsg.toolRounds.push({
          /* Collision-proof synthetic roundNum for the data-prn slot —
             real tool rounds use small sequential numbers; 9e6+ never clashes. */
          roundNum:   9000000 + assistantMsg.toolRounds.length,
          status:     "done",
          _inboxInject:  true,
          _inboxKey:     _injKey,
          inboxRound:    ev.round || 0,
          inboxCount:    ev.count || 0,
          inboxAgentIds: Array.isArray(ev.agentIds) ? ev.agentIds.filter(Boolean) : [],
          inboxPreviews: Array.isArray(ev.previews) ? ev.previews : [],
        });
        if (buf) buf.toolRounds = assistantMsg.toolRounds;
      }
      /* Reconcile the most recent swarm panel's async-running badge.

         An inbox-inject means sub-agent results just landed. Whether the
         panel should still read "N running async" depends on whether any
         agent is still in flight:
           • some agent still running/pending → keep the amber badge.
           • every agent terminal (done/failed) → the swarm has SETTLED.
             We must clear ``_asyncRunning`` and stamp it complete here,
             because the authoritative ``swarm_phase:complete`` event is
             pushed under the SPAWNING turn's task id and may never reach a
             later turn's stream (the original "badge stuck on running
             forever" bug). This inbox-inject is the cross-turn signal that
             lets the panel self-heal without that event.

         Previously this ALWAYS set ``_asyncRunning = true`` and nothing
         ever cleared it on a later turn → permanent stuck badge.        */
      const _injectedIds = Array.isArray(ev.agentIds)
        ? ev.agentIds.filter(Boolean) : [];
      const _markAsyncRunning = (sr) => {
        if (!sr) return;
        const agents = sr._swarmAgents || [];
        /* An inbox-inject for agent X is definitive proof X completed —
           mark it done even if its swarm_agent_complete event never
           reached this stream (lost when the spawning turn's SSE closed). */
        for (const id of _injectedIds) {
          const a = agents.find(x => x.id === id);
          if (a && a.status !== "failed" && a.status !== "done") {
            a.status = "done";
            a.phase = "done";
          }
        }
        const stillInFlight = agents.some(
          a => a.status === "running" || a.status === "pending"
            || a.status === "thinking" || !a.status);
        if (stillInFlight) {
          sr._asyncRunning = true;
        } else if (agents.length) {
          /* All agents terminal — settle the panel. */
          sr._asyncRunning = false;
          sr._swarmActive = false;
          if (sr.status !== "done") sr.status = "done";
          if (!sr._swarmEndTime) {
            sr._swarmEndTime = Date.now();
            if (sr._swarmStartTime) {
              sr._elapsed = ((sr._swarmEndTime - sr._swarmStartTime) / 1000)
                .toFixed(1) + "s";
            }
          }
        } else {
          /* No agent cards recovered yet (e.g. post-reload stub) — fall
             back to the old behavior so the badge at least shows activity. */
          sr._asyncRunning = true;
        }
      };
      const _curLive = (assistantMsg.toolRounds || []).filter(r => r._swarm);
      if (_curLive.length) {
        _markAsyncRunning(_curLive[_curLive.length - 1]);
      } else {
        const conv = conversations.find(c => c && c.id === convId);
        if (conv) {
          for (let i = conv.messages.length - 1; i >= 0; i--) {
            const m = conv.messages[i];
            if (!m || m.role !== "assistant" || !m.toolRounds) continue;
            const sr = m.toolRounds.find(r => r._swarm);
            if (sr) { _markAsyncRunning(sr); break; }
          }
        }
      }
      if (buf) buf._inboxInjects = assistantMsg._inboxInjects;
      twUpdate(convId);

}

function _handlePeerInboxInject(ev, c) {
  const convId = c.convId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
      /* ── Pillar #6 fast-path: a peer message from a sibling conversation was
       *    drained from the inbox and injected as a user message before this
       *    round. Unlike the queue-lane case (a persisted _peerMessage user
       *    bubble rendered with .peer-msg-banner), this one is injected INTO
       *    the running turn, so it needs an in-timeline chip mirroring
       *    swarm_inbox_inject — a synthetic toolRound flagged `_peerInject`,
       *    rendered by _renderPeerInjectRow. Dedup by round so SSE replay /
       *    poll fallback doesn't double it. */
      if (!assistantMsg.toolRounds) assistantMsg.toolRounds = [];
      const _pKey = "peer:" + (ev.round || 0);
      if (!assistantMsg.toolRounds.some(r => r._peerInject && r._peerKey === _pKey)) {
        assistantMsg.toolRounds.push({
          roundNum:      9000000 + assistantMsg.toolRounds.length,
          status:        "done",
          _peerInject:   true,
          _peerKey:      _pKey,
          peerRound:     ev.round || 0,
          peerCount:     ev.count || 0,
          peerPreviews:  Array.isArray(ev.previews) ? ev.previews : [],
        });
        if (buf) buf.toolRounds = assistantMsg.toolRounds;
      }
      twUpdate(convId);
}

function _handleMessagesSnapshot(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      if (typeof showMessagesInDebug === "function")
        showMessagesInDebug(
          ev.messages,
          ev.label || `Round ${ev.round} · ${ev.messageCount}条`,
          true,
          convId,
          ev.tools || undefined,
        );

    /* ═══ Endpoint mode events ═══ */
}

function _handleSseTimeout(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      /* SSE connection hit max duration — backend task is STILL RUNNING.
         Show a toast and return false (not done). The stream will close,
         _trySSE will detect !streamDone and return false, triggering _pollFallback. */
      if (typeof showToast === 'function') {
        showToast('', 'Connection Switched',
          'Long-running task: SSE stream reached max duration. Switching to polling — your task is still running in the background.',
          10000);
      }
      console.warn(
        `[_trySSE] SSE timeout notice received — taskId=${taskId.slice(0,8)} conv=${convId.slice(0,8)} ` +
        `contentSoFar=${assistantMsg.content?.length || 0}chars thinkingSoFar=${assistantMsg.thinking?.length || 0}chars ` +
        `toolRounds=${assistantMsg.toolRounds?.length || 0} — backend continues, switching to poll fallback`
      );
      // Return false — NOT a done event. Task is still running.


}

function _handleRoundCommitted(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      /* ★ Async shadow-git commit landed AFTER the done event was emitted.
         Backend moved commit_round out of the critical path (2026-05-07)
         so queue dispatch isn't blocked by slow FUSE git.  Wire the sha
         (and any git-derived modifiedFileList additions) onto the
         assistant message so undo/diff UI still works. */
      if (ev.gitSha) assistantMsg._gitSha = ev.gitSha;
      if (ev.modifiedFileList) assistantMsg.modifiedFileList = ev.modifiedFileList;
      if (typeof ev.modifiedFiles === 'number') assistantMsg.modifiedFiles = ev.modifiedFiles;
      /* Re-render the message so its footer reflects the new file list /
         undo button availability. */
      try { if (typeof renderChat === 'function') renderChat(); } catch (_) {}

}

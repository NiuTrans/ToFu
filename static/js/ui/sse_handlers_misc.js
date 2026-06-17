/* SSE misc handlers (round-usage, artifact, compaction, memory-prefetch, project-edit, timer-poll) — extracted from ui/sse_pipeline.js dispatchSSEEvent (2026-06).
   Property-only handlers (no closure-local reassignment) taking (ev, c),
   a snapshot of the dispatch ctx. Bodies are byte-for-byte the originals.
   Concatenated by lib/js_bundler.py BEFORE ui/sse_pipeline.js.
   Behavior contract: tests/test_frontend_sse_dispatch.py. */

function _handleRoundUsage(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      /* ── Per-round usage tick ──────────────────────────────────────────
       * The orchestrator emits this immediately after EACH LLM round
       * lands, carrying the raw usage dict + a pre-computed `tokensIn`
       * (input tokens including cache, Anthropic/OpenAI conventions
       * normalized server-side in lib/tasks_pkg/llm_fallback.py
       * :_emit_round_usage).
       *
       * Stash the latest reading on the in-flight assistant msg as
       * `_liveLastRoundUsage` so the context-health gauge reflects the
       * size of the prompt JUST sent to the model — without waiting
       * for the final `done` event to populate `apiRounds`.  This is
       * what makes the bar move on every tool round, not just at the
       * end of the user-visible turn.
       *
       * The reader is `static/js/context-bar.js:_lastUsageTokens`,
       * which prefers `_liveLastRoundUsage` over `apiRounds[-1]` and
       * falls back to `msg.usage / n` for older conversations. */
      if (assistantMsg) {
        assistantMsg._liveLastRoundUsage = {
          round: ev.round,
          model: ev.model,
          tag: ev.tag,
          tokensIn: ev.tokensIn,
          tokensOut: ev.tokensOut,
          usage: ev.usage,
        };
      }
      if (typeof updateContextBar === 'function') updateContextBar();
      return false;

}

function _handleArtifact(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      /* ── Renderable artifact (md/html/svg) — see lib/artifacts/ ───────
       * Producer A (write_file post-hook in lib/tasks_pkg/handlers/project.py)
       * persists the bytes server-side and emits this metadata-only event.
       * The actual content is fetched lazily via /api/artifacts/<id>/raw
       * when the user clicks the chip.
       *
       * We stash the meta on assistantMsg._artifacts so the chip survives
       * re-renders, and also into the global Artifacts cache so a click
       * after compaction (which strips toolRounds) still finds it. */
      if (typeof window.Artifacts !== "undefined" && window.Artifacts.attachToMessage) {
        try {
          window.Artifacts.attachToMessage(assistantMsg, {
            id:             ev.id,
            conv_id:        ev.conv_id || convId,
            task_id:        ev.task_id || taskId,
            msg_id:         ev.msg_id || (assistantMsg && assistantMsg._msgId) || "",
            source:         ev.source || "",
            source_ref:     ev.source_ref || {},
            format:         ev.format || "",
            title:          ev.title || "",
            size_bytes:     ev.size_bytes || 0,
            version:        ev.version || 1,
            parent_id:      ev.parent_id || "",
            pinned:         !!ev.pinned,
            created_at:     ev.created_at || 0,
            url:            ev.url || ("/api/v1/artifacts/" + (ev.id || "")),
          });
        } catch (e) {
          console.debug("[Artifacts] attachToMessage failed:", e);
        }
      }
      if (buf) {
        buf._artifacts = (assistantMsg && assistantMsg._artifacts) || buf._artifacts || [];
      }
      twUpdate(convId);

}

function _handleCompaction(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      /* ── Compaction marker ────────────────────────────────────────────
       * Emitted by lib/tasks_pkg/compaction.py when an archive row is
       * inserted (transcript_archive). Each marker becomes an inline
       * chip inside the assistant bubble; clicking it opens the right-
       * side Compaction Viewer drawer (see static/js/compaction-viewer.js)
       * which lazy-loads the pre-compaction message list.
       *
       * We store markers on the LIVE assistant message so they reappear
       * after re-render without a DB round-trip. On reload, the drawer
       * also pulls the authoritative list from
       * GET /api/conversations/<id>/compactions. */
      assistantMsg._compactions = assistantMsg._compactions || [];
      if (ev.type === "compaction") {
        const existing = assistantMsg._compactions.find(c => c.archiveId === ev.archiveId);
        if (!existing) {
          assistantMsg._compactions.push({
            archiveId:     ev.archiveId,
            convId:        ev.convId || convId,
            trigger:       ev.trigger || 'force',
            roundNum:      ev.roundNum || 0,
            tokensBefore:  ev.tokensBefore || 0,
            tokensAfter:   ev.tokensAfter || 0,
            msgsBefore:    ev.msgsBefore || 0,
            msgsAfter:     ev.msgsAfter || 0,
            model:         ev.model || '',
            reason:        ev.reason || '',
            ts:            ev.ts || Math.floor(Date.now() / 1000),
            status:        'in_progress',
          });
        }
      } else {
        // compaction_done — upgrade the matching marker with final numbers
        const marker = assistantMsg._compactions.find(c => c.archiveId === ev.archiveId);
        if (marker) {
          marker.tokensAfter = ev.tokensAfter || marker.tokensAfter;
          marker.msgsAfter   = ev.msgsAfter   || marker.msgsAfter;
          marker.reductionPct = ev.reductionPct;
          marker.status = 'done';
        }
      }
      if (buf) buf._compactions = assistantMsg._compactions;
      /* Bind the gauge to the compaction event the moment it fires.
       * 'compaction' arrives before the LLM summary call and carries
       * tokensBefore — the new tick on the donut materializes here.
       * 'compaction_done' lands the final tokensAfter and we flash the
       * matching tick so the eye is drawn from chip → gauge in one beat. */
      if (typeof updateContextBar === 'function') updateContextBar();
      if (ev.type === 'compaction_done' && typeof window.flashGaugeForArchive === 'function') {
        window.flashGaugeForArchive(ev.archiveId);
      }
      twUpdate(convId);

}

function _handleMemoryPrefetch(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      /* ── Memory Prefetch indicator ────────────────────────────────────
       * Phases emitted by lib/memory/prefetch.py:
       *   started       — BM25 scoring about to run
       *   bm25_done     — coarse stage complete; cheap-LLM next
       *   rerank_started — cheap-model filter running
       *   done          — memories injected (or none picked)
       *   skipped       — no memories / empty query / bm25 empty
       *   failed        — unexpected error
       * We show a small chip inside the assistant bubble (above the tool panel)
       * so the user can see that a cheap model is filtering memories in the
       * background — otherwise the ~1-3s latency before the main model starts
       * producing tokens would feel unexplained.
       *
       * In ADDITION, we mirror the translate-pattern: while the cheap-model
       * filter is running we set conv._memoryPrefetching so the sidebar
       * shows a status dot + tag (parallel to conv._translating). */
      const prev = assistantMsg._memoryPrefetch || {};
      assistantMsg._memoryPrefetch = {
        ...prev,
        phase: ev.phase,
        totalMemories: ev.total_memories ?? prev.totalMemories,
        candidates: ev.candidates ?? prev.candidates,
        bm25Ms: ev.bm25_ms ?? prev.bm25Ms,
        rerankMs: ev.rerank_ms ?? prev.rerankMs,
        totalMs: ev.total_ms ?? prev.totalMs,
        selected: ev.selected ?? prev.selected,
        memories: ev.memories ?? prev.memories,
        reason: ev.reason ?? prev.reason,
        fellBack: ev.fell_back ?? prev.fellBack,
        startedAt: prev.startedAt || Date.now(),
      };
      if (buf) buf._memoryPrefetch = assistantMsg._memoryPrefetch;

      // Sidebar status mirror — only the cheap-LLM running phases mark the
      // conversation as "filtering memories"; terminal phases clear it.
      const _conv = (typeof conversations !== 'undefined')
        ? conversations.find(c => c.id === convId) : null;
      if (_conv) {
        const RUNNING = new Set(['started', 'bm25_done', 'rerank_started']);
        const TERMINAL = new Set(['done', 'skipped', 'failed']);
        if (RUNNING.has(ev.phase)) {
          _conv._memoryPrefetching = true;
        } else if (TERMINAL.has(ev.phase)) {
          _conv._memoryPrefetching = false;
        }
        // Re-render sidebar so the dot/tag updates immediately.
        if (typeof renderConversationList === 'function') {
          renderConversationList();
        }
      }
      twUpdate(convId);

}

function _handleProjectExternalEdit(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      // ★ Git-shim: external edits captured outside Tofu round boundary.
      //   Show a brief toast so the user knows we auto-committed their changes.
      const files = ev.files || [];
      const sha = (ev.sha || '').slice(0, 7);
      try {
        if (typeof showToast === 'function') {
          const preview = files.slice(0, 3).join(', ') + (files.length > 3 ? ` +${files.length - 3} more` : '');
          showToast(`📝 Captured ${files.length} external edit(s) — ${preview}${sha ? ' · ' + sha : ''}`, 'info');
        }
      } catch (e) { console.warn('[project_external_edit] toast failed', e); }
      console.log('[project_external_edit]', { sha, files });

}

function _handleTimerPollCheck(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      /* ═══ Timer Watcher inline poll progress ═══
         Each poll emits a sub-event attached to the timer_create tool round.
         We store polls as _timerPolls[] on the round for collapsible rendering.
         ★ decision='skipped' is a lightweight heartbeat for polls where
           the check_command output was unchanged — we don't push it into
           _timerPolls[] (would spam), just bump skip metadata so the UI
           can render a subdued "N skipped — output unchanged" trailer. */
      if (assistantMsg.toolRounds) {
        const r = (ev.toolCallId
          ? assistantMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId)
          : null
        ) || assistantMsg.toolRounds.find(r => r.roundNum === ev.roundNum);
        if (r) {
          r._timerTimerId = ev.timerId;
          // Capture the next-poll timestamp so the UI can render a countdown.
          if (ev.nextPollTs) r._timerNextPollTs = ev.nextPollTs;
          // The 'started' event carries the verification metadata (what is
          // being checked + how). Stash it on the round so the panel header
          // and detail can explain the timer to the user.
          if (ev.decision === "started") {
            if (ev.checkInstruction) r._timerCheckInstruction = ev.checkInstruction;
            if (ev.checkCommand) r._timerCheckCommand = ev.checkCommand;
            if (ev.pollInterval) r._timerPollInterval = ev.pollInterval;
            if (ev.maxPolls) r._timerMaxPolls = ev.maxPolls;
          }
          if (ev.decision === "skipped") {
            r._timerSkipCount = (r._timerSkipCount || 0) + 1;
            r._timerLastSkipTs = Date.now();
            r._timerLastSkipPollNum = ev.pollNum;
            // Keep the round in "searching" state while timer is polling
            r.status = "searching";
          } else {
            if (!r._timerPolls) r._timerPolls = [];
            // ★ Dedup: skip if this pollNum already exists (from state snapshot)
            const _alreadyHas = r._timerPolls.some(p => p.pollNum === ev.pollNum && p.decision === ev.decision);
            if (!_alreadyHas) {
              r._timerPolls.push({
                pollNum: ev.pollNum,
                decision: ev.decision,
                reason: ev.reason || "",
                tokensUsed: ev.tokensUsed || 0,
                timerId: ev.timerId || "",
                cmdOutput: ev.cmdOutput || "",
                parseError: !!ev.parseError,
                ts: Date.now(),
              });
            }
            // Keep the round in "searching" state while timer is polling
            if (ev.decision === "ready") {
              r.status = "done";
              r._timerTriggered = true;
            } else {
              r.status = "searching";
            }
          }
        }
      }
      if (buf)
        buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

    /* ═══ Swarm mode events ═══ */
}

/* SSE interactive/IO handlers (human-guidance, tool-progress, stdin, write-approval) — extracted from ui/sse_pipeline.js dispatchSSEEvent (2026-06).
   Property-only handlers (no closure-local reassignment) taking (ev, c),
   a snapshot of the dispatch ctx. Bodies are byte-for-byte the originals.
   Concatenated by lib/js_bundler.py BEFORE ui/sse_pipeline.js.
   Behavior contract: tests/test_frontend_sse_dispatch.py. */

function _handleHumanGuidance(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg;
      /* ── Human Guidance: LLM is asking the user a question ── */
      if (assistantMsg.toolRounds) {
        const r = (ev.toolCallId
          ? assistantMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId)
          : null
        ) || assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "awaiting_human";
          r.guidanceId = ev.guidanceId;
          r.guidanceQuestion = ev.question;
          r.guidanceType = ev.responseType;
          /* ★ Defensive: ev.options may arrive as a JSON string or object
           *   from an upstream model that serialised it oddly. Normalise to
           *   an array before assigning so _renderHumanGuidanceCard can map. */
          let _ev_opts = ev.options;
          if (typeof _ev_opts === 'string') {
            try { _ev_opts = JSON.parse(_ev_opts); }
            catch (_e) { _ev_opts = []; }
          }
          if (!Array.isArray(_ev_opts)) _ev_opts = [];
          r.guidanceOptions = _ev_opts.map(o => ({...(o || {})}));
        }
      }
      if (typeof twUpdate === 'function') twUpdate(convId);
      // ★ Update sidebar to show amber blinking dot for awaiting-human state
      renderConversationList();
      // ★ Auto-translate question & options (EN→CN) when autoTranslate is ON.
      //   This mirrors the finishStream auto-translate flow for assistant messages.
      //   Fire-and-forget: translates asynchronously, re-renders card when done.
      const _hgConv = conversations.find(c => c.id === convId);
      const _hgAutoTrans = convAutoTranslate(_hgConv);
      if (_hgAutoTrans && ev.question) {
        _autoTranslateHumanGuidance(convId, ev.roundNum, ev.question, ev.responseType, ev.options || []);
      }
}

function _handleToolProgress(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg;
      /* ── Streaming run_command output + in-flight round state ──
       *    ★ RENDER_CONTRACT Phase 3 (pt_1a82ffb3): applied through the ONE
       *    pure reducer (reduceStreamState 'tool_progress' action) instead of
       *    the inline field-by-field mutation this handler used to carry. That
       *    inline copy was the LAST tool frame not folded through the reducer,
       *    which meant everything it wrote (_partialOutput, the batch tally,
       *    qrImages, and now the execution deadline) existed ONLY on the live
       *    path and was absent from projectColdSnapshot — so switching
       *    conversations mid-command lost it. Delegating makes the live and
       *    cold projections the same fixed point by construction.
       *
       *    The reducer owns round-state writes; the DOM concerns below
       *    (twUpdate, terminal auto-scroll) stay here — the purity guard
       *    forbids them inside the reducer. */
      const _trMsg = _epCriticPhase ? _epCriticMsg : assistantMsg;
      if (_trMsg && _trMsg.toolRounds) {
        reduceStreamState(_trMsg, ev);
      }
      if (typeof twUpdate === 'function') twUpdate(convId);
      // Auto-scroll the live terminal box(es) to the bottom so the newest
      // output is always visible — DOM was just rerendered above.
      try {
        const _liveOut = document.querySelectorAll('.ptool-cmd-output-live');
        if (_liveOut.length) {
          /* Batch into one rAF: read all scrollHeights, then write all
           * scrollTops, so we don't interleave layout reads with writes
           * (forced synchronous reflow) once per element per SSE chunk. */
          requestAnimationFrame(() => {
            const _heights = [];
            for (let i = 0; i < _liveOut.length; i++) _heights.push(_liveOut[i].scrollHeight);
            for (let i = 0; i < _liveOut.length; i++) _liveOut[i].scrollTop = _heights[i];
          });
        }
      } catch (_e) { /* best-effort */ }
}

function _handleStdinRequest(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg;
      /* ── Stdin Request: subprocess is waiting for user keyboard input ── */
      if (assistantMsg.toolRounds) {
        const r = (ev.toolCallId
          ? assistantMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId)
          : null
        ) || assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "awaiting_stdin";
          r.stdinId = ev.stdinId;
          r.stdinPrompt = ev.prompt;
          r.stdinCommand = ev.command;
        }
      }
      if (typeof twUpdate === 'function') twUpdate(convId);
}

function _handleStdinResolved(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg;
      /* ── Stdin Resolved: user input was sent, command continues ── */
      if (assistantMsg.toolRounds) {
        const r = (ev.toolCallId
          ? assistantMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId)
          : null
        ) || assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "searching";
          r.stdinId = null;
          r.stdinPrompt = null;
        }
      }
      if (typeof twUpdate === 'function') twUpdate(convId);
}

function _handleWriteApproval(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg;
      if (_epCriticPhase) { /* skip approval during critic phase */ }
      else if (assistantMsg.toolRounds) {
        const r = (ev.toolCallId
          ? assistantMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId)
          : null
        ) || assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.status = "pending_approval";
          r.approvalId = ev.approvalId;
          r.approvalMeta = ev.meta;
        }
      }
      if (typeof twUpdate === 'function') twUpdate(convId);
}

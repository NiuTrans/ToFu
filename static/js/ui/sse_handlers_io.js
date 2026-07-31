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
      /* ── Streaming run_command output: append chunk to the round's
       *    partial output buffer and re-render so the user sees it live. */
      const _trMsg = _epCriticPhase ? _epCriticMsg : assistantMsg;
      if (_trMsg && _trMsg.toolRounds) {
        const r = (ev.toolCallId
          ? _trMsg.toolRounds.find(rr => rr.toolCallId === ev.toolCallId)
          : null
        ) || _trMsg.toolRounds.find(rr => rr.roundNum === ev.roundNum);
        if (r) {
          // _partialOutput is the live, growing terminal buffer.
          // It's replaced wholesale by meta.output once tool_result arrives.
          if (typeof r._partialOutput !== "string") r._partialOutput = "";
          r._partialOutput += (ev.chunk || "");
          /* ★ Batch per-item progress (pt_67ffc2b7). A batch call
           * (web_search(queries=[…]) / fetch_url(urls=[…])) is ONE round, so
           * its terminal tool_result is the round's only transition — a 2s
           * query beside a 40s one is indistinguishable from three slow ones.
           * The backend now reports each item as it settles; store the running
           * tally so the row can render "2/3" with the item that just landed.
           * Kept STRICTLY separate from _partialOutput above: that is
           * run_command's terminal pane and is wiped by tool_result, so a
           * search label routed through it would be printed in a terminal box
           * and then silently lost. */
          if (ev.batchTotal != null) {
            r._batchTotal = ev.batchTotal;
            if (ev.batchDone != null && (r._batchDone == null || ev.batchDone > r._batchDone)) {
              r._batchDone = ev.batchDone;   // monotonic: never render backwards
            }
            if (ev.batchItem) {
              if (!Array.isArray(r._batchItems)) r._batchItems = [];
              r._batchItems.push({ item: ev.batchItem, ok: ev.batchOk !== false });
            }
            if (ev.batchOk === false) r._batchFailed = (r._batchFailed || 0) + 1;
          }
          /* ★ Live QR: the backend recovers a scannable bitmap from terminal
           * block art WHILE the command is still running. That timing IS the
           * feature — a scan-to-login command blocks waiting for the scan, so
           * a code that only appears at finalize arrives after the window has
           * closed. Store it on the round; the running-state renderer draws it
           * above the live output pane. The event carries the full accumulated
           * list (not just the newest), so a late reconnect gets every code. */
          if (Array.isArray(ev.qrImages) && ev.qrImages.length) {
            r.qrImages = ev.qrImages;
          }
        }
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

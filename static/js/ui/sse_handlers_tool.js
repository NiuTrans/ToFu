/* SSE tool_* handlers — extracted from ui/sse_pipeline.js dispatchSSEEvent (2026-06).
   Property-only handlers (no closure-local reassignment) taking (ev, c)
   where c is a snapshot of the dispatch ctx. Bodies are byte-for-byte the
   originals. Concatenated by lib/js_bundler.py BEFORE ui/sse_pipeline.js.
   See tests/test_frontend_sse_dispatch.py for the behavior contract. */

function _handleToolStart(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      if (_epCriticPhase) {
        /* Critic's tool usage → accumulate into critic message */
        if (_epCriticMsg) {
          const r = {
            roundNum: ev.roundNum, query: ev.query, results: null,
            status: "searching", toolName: ev.toolName || null,
            toolCallId: ev.toolCallId || null, toolArgs: ev.toolArgs || null,
            llmRound: ev.llmRound ?? null, _swarm: false,
          };
          if (!_epCriticMsg.toolRounds) _epCriticMsg.toolRounds = [];
          _epCriticMsg.toolRounds.push(r);
          if (_epCriticBuf) _epCriticBuf.toolRounds = _epCriticMsg.toolRounds;
        }
        twUpdate(convId);
      } else {
        const r = {
          roundNum: ev.roundNum,
          query: ev.query,
          results: null,
          status: "searching",
          toolName: ev.toolName || null,
          toolCallId: ev.toolCallId || null,
          toolArgs: ev.toolArgs || null,
          llmRound: ev.llmRound ?? null,
          _swarm: ev._swarm || false,
        };
        // ★ Preserve per-round assistantContent for Continue replay
        if (ev.assistantContent) r.assistantContent = ev.assistantContent;
        // ★ Harness self-repair descriptor — the backend auto-corrected this
        //   call's malformed arguments; surfaced as an "auto-fixed" badge.
        if (ev._repaired) r._repaired = ev._repaired;
        // ★ Hallucinated-tool rejection — the backend classified this name as
        //   a non-existent tool and rejected it (never executed). Carry the
        //   distinct status + descriptor so the round renders as rejected
        //   from the very first event.
        if (ev.status === "rejected") r.status = "rejected";
        if (ev._rejected) r._rejected = ev._rejected;
        if (!assistantMsg.toolRounds) assistantMsg.toolRounds = [];
        assistantMsg.toolRounds.push(r);
        /* ★ MCP login-hint: surface a prominent "Check your phone for the
         *   approval push" banner whenever a login-style MCP call starts.
         *   Meituan's `hope login` blocks the subprocess for up to ~5 min
         *   waiting for the user to tap Approve on their mobile-office app
         *   — without this banner the user has no idea the tool is
         *   waiting on them and the task appears frozen.
         *   Matches:
         *     - mcp__hope__hope_login
         *     - mcp__hope__hope_check_login (auto-login triggered when
         *       HOPE_USERNAME is configured and no cached creds)
         *     - generic *_login / *_check_login MCP tools
         */
        try {
          const _toolN = String(ev.toolName || '');
          if (/^mcp__/.test(_toolN) && /(hope_login|hope_check_login|_login$)/.test(_toolN)) {
            let _un = '';
            try {
              const a = ev.toolArgs && (typeof ev.toolArgs === 'string' ? JSON.parse(ev.toolArgs) : ev.toolArgs);
              _un = (a && (a.username || a.user)) || '';
            } catch (_e) { /* best-effort username extraction */ }
            assistantMsg._mcpLoginHint = {
              phase: 'awaiting_approval',
              toolName: _toolN,
              roundNum: ev.roundNum,
              username: _un,
              updatedAt: Date.now(),
            };
            if (buf) buf._mcpLoginHint = assistantMsg._mcpLoginHint;
          }
        } catch (_e) { /* best-effort */ }
        /* Track swarm round number so swarm_phase events can find it */
        if (r._swarm) assistantMsg._swarmRoundNum = r.roundNum;
        if (buf)
          buf.toolRounds = assistantMsg.toolRounds;
        twUpdate(convId);
      }
}

function _handleToolResult(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      if (_epCriticPhase && _epCriticMsg) {
        /* Critic's tool result → accumulate into critic message */
        if (_epCriticMsg.toolRounds) {
          const r = (ev.toolCallId
            ? _epCriticMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId)
            : null
          ) || _epCriticMsg.toolRounds.find(r => r.roundNum === ev.roundNum);
          if (r) { r.results = ev.results; r.status = "done"; if (ev.searchDiag) r.searchDiag = ev.searchDiag; if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown; if (ev.vertical) r.vertical = ev.vertical; if (ev.verticals) r.verticals = ev.verticals; }
        }
        if (_epCriticBuf) _epCriticBuf.toolRounds = _epCriticMsg.toolRounds || [];
        twUpdate(convId);
      } else if (assistantMsg.toolRounds) {
        const r = (ev.toolCallId
          ? assistantMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId)
          : null
        ) || assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum,
        );
        if (r) {
          r.results = ev.results;
          // A rejected hallucinated tool stays 'rejected' (it never ran) —
          // don't flip it to 'done'. Everything else completes normally.
          if (ev.status === "rejected" || ev._rejected) {
            r.status = "rejected";
            if (ev._rejected) r._rejected = ev._rejected;
          } else {
            r.status = "done";
          }
          r.approvalId = null;
          r.approvalMeta = null;
          r.guidanceId = null;
          if (ev.searchDiag) r.searchDiag = ev.searchDiag;
          if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
          if (ev.vertical) r.vertical = ev.vertical;
          if (ev.verticals) r.verticals = ev.verticals;
          /* ★ Harness self-repair: when the backend auto-corrected this
           *   call's args AFTER the (garbled) tool_start was already shown,
           *   the tool_result carries the corrected display + descriptor.
           *   Apply them so the live view stops showing the garbled line. */
          if (ev._repaired) {
            r._repaired = ev._repaired;
            if (ev.query) r.query = ev.query;
          }
        }
        /* ★ Clear the MCP login-hint banner once the login call returns.
         *   Classification priority (each test uses STRUCTURED fields
         *   first, text matching second, and always with word-boundaries
         *   to avoid matching e.g. "denied": false inside a JSON dump):
         *     1. Parse snippet as JSON → read `approved`/`denied`/`approval_timed_out`
         *     2. Fallback regex on rendered text WITH word boundaries
         *   Without this, the chip showed "Login denied" whenever the
         *   result JSON contained the literal token `"denied"`, even
         *   when approval actually succeeded. */
        const _lh = assistantMsg._mcpLoginHint;
        if (_lh && _lh.roundNum === ev.roundNum) {
          const _res = Array.isArray(ev.results) ? ev.results[0] : null;
          const _snippet = (_res && (_res.snippet || _res.title || '')) || '';
          const _resultOk = !!(_res && _res.ok);
          // Try to parse the structured result — MCP tools typically
          // embed the tool's JSON response in snippet.
          let _parsed = null;
          if (_snippet) {
            try {
              // The snippet may be "{...}" or wrapped in markdown fences
              const _trim = _snippet.trim().replace(/^```(?:json)?\s*/, '').replace(/\s*```$/, '');
              _parsed = JSON.parse(_trim);
            } catch (_e) { /* non-JSON snippet — fall through */ }
          }
          let _phase;
          if (_parsed && typeof _parsed === 'object') {
            /* Trust the tool's own structured verdict. hope_login returns
             * {approved, denied, approval_timed_out, token_verified}. */
            if (_parsed.approved === true) _phase = 'approved';
            else if (_parsed.denied === true) _phase = 'denied';
            else if (_parsed.approval_timed_out === true) _phase = 'timeout';
            else if (_parsed.token_verified === false) _phase = 'denied'; // token missing → treat as failure
            else _phase = _resultOk ? 'approved' : 'done';
          } else {
            /* Word-boundary regex fallback for non-JSON replies.
             * \b prevents matching "denied" inside "\"denied\":false". */
            const _deniedText = /\bdenied\b\s*$|\brejected\b|\bcancell?ed\b/i.test(_snippet);
            const _timeoutText = /\btimed?\s*out\b|\bapproval\s+timeout\b/i.test(_snippet);
            _phase = _resultOk ? 'approved'
                  : _deniedText ? 'denied'
                  : _timeoutText ? 'timeout'
                  : 'done';
          }
          assistantMsg._mcpLoginHint = {
            ..._lh,
            phase: _phase,
            /* Keep the snippet in full — the user wants to see the
             * complete response, not a 200-char slice with ellipsis.
             * The chip CSS is also updated to wrap instead of clip. */
            snippet: _snippet,
            updatedAt: Date.now(),
          };
          if (buf) buf._mcpLoginHint = assistantMsg._mcpLoginHint;
          /* Auto-dismiss on success after 4s so the chip doesn't linger
           * forever once the session is live. */
          if (_phase === 'approved') {
            setTimeout(() => {
              if (assistantMsg._mcpLoginHint === buf?._mcpLoginHint) {
                assistantMsg._mcpLoginHint = null;
                if (buf) buf._mcpLoginHint = null;
                twUpdate(convId);
              }
            }, 4000);
          }
        }
      }
      /* ★ After create_project: refresh project status so the new extra
       * root appears in the sidebar AND gets persisted to conv.projectPaths.
       * Without this the backend has the root registered but the frontend
       * will overwrite it on the next set_project call (e.g. page refresh,
       * conv switch), causing any subsequent 'name:path' writes to land
       * under the primary root — see create_project frontend-sync bug. */
      if (ev.results && ev.results.some(r => r.toolName === 'create_project')) {
        try {
          Api.project.status()
            .then(data => {
              if (!data) return;
              if (typeof _applyProjectData === 'function') _applyProjectData(data);
              const c = typeof getActiveConv === 'function' ? getActiveConv() : null;
              if (c && data.path) {
                const paths = [data.path];
                if (Array.isArray(data.extraRoots)) {
                  for (const r of data.extraRoots) {
                    const pp = typeof r === 'string' ? r : r.path;
                    if (pp && !paths.includes(pp)) paths.push(pp);
                  }
                }
                c.projectPath = data.path;
                c.projectPaths = paths;
                if (typeof saveConversations === 'function') saveConversations(c.id);
                if (typeof syncConversationToServer === 'function') syncConversationToServer(c);
              }
              if (typeof showToast === 'function') {
                const cp = ev.results.find(r => r.toolName === 'create_project');
                showToast('', 'New workspace root',
                  (cp && (cp.snippet || cp.title)) || 'Registered an additional project root',
                  4000);
              }
            })
            .catch(() => {});
        } catch (_) {}
      }
      /* ★ Toast for create_memory */
      if (ev.results && ev.results.some(r => r.toolName === 'create_memory')) {
        const sk = ev.results.find(r => r.toolName === 'create_memory');
        const ok = sk.memoryOk === true || (sk.badge && sk.badge.includes('saved'));
        if (typeof showToast === 'function') {
          const sName = sk.memoryName || 'Memory';
          const sScope = sk.memoryScope || 'project';
          const title = ok ? `${sName}` : 'Memory Failed';
          const body = ok
            ? `Saved to ${sScope} scope — available in future sessions`
            : (sk.snippet || sk.title || 'Unknown error');
          showToast('', title, body, ok ? 5000 : 8000);
        }
      }
      if (buf)
        buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);
      // ★ If this was an ask_human tool_result, refresh sidebar to clear amber dot
      if (ev.results && ev.results.some(r2 => r2.toolName === 'ask_human')) {
        renderConversationList();
      }
}

function _handleToolComplete(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      // ★ Store raw tool content for continue context restoration
      const _applyToolComplete = (r) => {
        if (!r) return;
        r.toolContent = ev.toolContent || null;
        if (ev.toolTokens != null) r.toolTokens = ev.toolTokens;
        // L0 may already be stamped server-side at emit time.
        if (ev.compactionLayer) {
          r.compactionLayer = ev.compactionLayer;
          r.compactedFromChars = ev.compactedFromChars;
          r.compactedToChars = ev.compactedToChars;
        }
      };
      if (_epCriticPhase && _epCriticMsg) {
        if (_epCriticMsg.toolRounds) {
          _applyToolComplete(_epCriticMsg.toolRounds.find(r => r.roundNum === ev.roundNum && r.toolCallId === ev.toolCallId));
        }
        if (_epCriticBuf)
          _epCriticBuf.toolRounds = _epCriticMsg.toolRounds || [];
      } else if (assistantMsg.toolRounds) {
        _applyToolComplete(assistantMsg.toolRounds.find(
          (r) => r.roundNum === ev.roundNum && r.toolCallId === ev.toolCallId,
        ));
      }
      // ★ Sync to buf and let the reactive pipeline (twUpdate → _syncToolRoundsDOM)
      //   handle preview button rendering — no fragile direct DOM injection needed.
      if (buf)
        buf.toolRounds = assistantMsg.toolRounds || [];
      twUpdate(convId);

}

function _handleToolCompacted(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg, buf = c.buf;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg, _epCriticBuf = c.epCriticBuf;
      /* ★ Per-tool compaction event — emitted by lib/tasks_pkg/compaction.py
       * micro_compact() (L1) and the aggregate-budget pass in
       * tool_dispatch.py (L0). Tags the matching round so its chip can
       * render the COMPACTED label in real time, even on already-
       * completed tool rounds that compaction just rewrote.
       *
       * IMPORTANT: L1 compacts COLD rounds — i.e. tool calls from
       * EARLIER assistant messages, not the in-flight one. Searching
       * only `assistantMsg.toolRounds` (the current bubble) misses
       * those entirely and the pill never renders. We have to walk
       * every assistant message in the conversation and stamp the
       * matching round wherever it lives.
       *
       * Toolcall IDs are conversation-unique (UUID-style), so a single
       * find across the whole conv is safe and unambiguous. */
      const _applyCompacted = (r) => {
        if (!r) return false;
        r.compactionLayer = ev.compactionLayer || r.compactionLayer || "L1";
        if (ev.compactedFromChars != null) r.compactedFromChars = ev.compactedFromChars;
        if (ev.compactedToChars != null) r.compactedToChars = ev.compactedToChars;
        if (ev.toolTokens != null) r.toolTokens = ev.toolTokens;
        return true;
      };
      let _stampedMsg = null;
      // 1. Try the active critic bubble first (endpoint mode).
      if (_epCriticPhase && _epCriticMsg && _epCriticMsg.toolRounds
          && _applyCompacted(_epCriticMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId))) {
        _stampedMsg = _epCriticMsg;
        if (_epCriticBuf) _epCriticBuf.toolRounds = _epCriticMsg.toolRounds || [];
      }
      // 2. Fall through to every assistant message in this conversation.
      //    Most events match in the in-flight assistantMsg; cold-round
      //    compactions match in older messages.
      if (!_stampedMsg) {
        const _conv = (typeof conversations !== 'undefined')
          ? conversations.find(c => c && c.id === convId)
          : null;
        if (_conv && Array.isArray(_conv.messages)) {
          for (let i = _conv.messages.length - 1; i >= 0; i--) {
            const m = _conv.messages[i];
            if (!m || m.role !== 'assistant' || !Array.isArray(m.toolRounds)) continue;
            const r = m.toolRounds.find(rr => rr.toolCallId === ev.toolCallId);
            if (_applyCompacted(r)) { _stampedMsg = m; break; }
          }
        }
      }
      if (buf && assistantMsg && Array.isArray(assistantMsg.toolRounds))
        buf.toolRounds = assistantMsg.toolRounds;
      twUpdate(convId);
      /* If we stamped a round in an OLDER message (not the in-flight
       * bubble), twUpdate alone won't re-render that message — it
       * only refreshes the streaming bubble.  Trigger a full conv
       * re-render so the COMPACTED pill on the older row materializes
       * immediately.  Cheap: renderChat is fingerprint-guarded and
       * the new compactedCount in _msgFingerprint forces re-render
       * of just the message that changed. */
      if (_stampedMsg && _stampedMsg !== assistantMsg
          && convId === activeConvId
          && typeof renderChat === 'function') {
        const _conv = (typeof conversations !== 'undefined')
          ? conversations.find(c => c && c.id === convId) : null;
        if (_conv) renderChat(_conv, false);
      }
      /* ── Debug-panel alignment ──
       * The debug panel renders the api-form messages snapshot the model
       * just received. Compaction mutates a tool message's content
       * mid-round; without patching the cached snapshot here, the panel
       * keeps showing the pre-compaction blob (e.g. 100 KB grep dump)
       * until the next ``messages_snapshot`` lands — which never arrives
       * if the task ends or pauses. Patch the cached entry by toolCallId
       * and re-render so the JSON tree matches what the model now sees. */
      if (typeof _debugCache !== 'undefined'
          && _debugCache[convId]
          && Array.isArray(_debugCache[convId].messages)
          && ev.compactedContent != null) {
        const _cached = _debugCache[convId].messages;
        for (let i = 0; i < _cached.length; i++) {
          const _m = _cached[i];
          if (_m && _m.role === 'tool' && _m.tool_call_id === ev.toolCallId) {
            _m.content = ev.compactedContent;
            _m._compactionLayer = ev.compactionLayer || 'L1';
            _m._compactedFromChars = ev.compactedFromChars;
            _m._compactedToChars = ev.compactedToChars;
            _m._toolTokens = ev.toolTokens;
            break;
          }
        }
        if (convId === activeConvId
            && typeof showMessagesInDebug === 'function') {
          const _c = _debugCache[convId];
          showMessagesInDebug(_c.messages, _c.label, true, convId, _c.tools);
        }
      }

}

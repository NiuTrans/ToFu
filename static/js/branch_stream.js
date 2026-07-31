/* ════════════════════════════════════
   branch_stream.js — Branch SSE/poll transport + reconnect
   Extracted from branch.js (2026-07). The streaming half of Branch
   Conversations: _branchStreamSSE / _branchStreamPoll, the two branch
   streaming-UI painters, _finishBranchStream, _reconnectBranchStream and
   initBranchReconnect. Plain window-scope concatenation (NOT an IIFE) —
   shares _branchStreams / _activeBranch / _branchKey with branch.js at
   runtime. Load order among the two branch files is free (all cross-refs
   are inside function bodies); both load BEFORE main.js.
   ════════════════════════════════════ */

// ══════════════════════════════════════════
//  Build API messages for branch — REMOVED
//  ★ Branch messages are now built server-side by
//    build_branch_api_messages() in conv_message_builder.py,
//    called via POST /api/chat/branch/start.
// ══════════════════════════════════════════

// ══════════════════════════════════════════
//  Branch SSE streaming
// ══════════════════════════════════════════

/* Write-approval gate for branch tasks — mirrors the main chat handler
 * (ui/sse_handlers_io.js::_handleWriteApproval). Stamps the matching tool
 * round; the shared renderer (_renderPendingApprovalBlock in ui/tool_rounds.js)
 * paints working Approve/Reject buttons that call the global
 * resolveWriteApproval → POST /api/v1/project/write-approval. Top-level so
 * the wiring AND the stamping are independently testable. */
function _branchHandleWriteApproval(ev, assistantMsg) {
  if (!assistantMsg || !assistantMsg.toolRounds) return;
  const r = (ev.toolCallId
    ? assistantMsg.toolRounds.find(rr => rr.toolCallId === ev.toolCallId)
    : null
  ) || assistantMsg.toolRounds.find(rr => rr.roundNum === ev.roundNum);
  if (r) {
    r.status = "pending_approval";
    r.approvalId = ev.approvalId;
    r.approvalMeta = ev.meta;
  }
}
async function _branchStreamSSE(conv, msgIdx, branchIdx, branch, assistantMsg, taskId, controller, bk) {
  let lastSave = Date.now();
  let gotData = false;
  /* NO first-byte timeout. A branch stream is the same object as the main
   * chat stream, which runs on Api.chat.streamResponse -> stream() ->
   * timeout:0. This used to abort after 45s without a first byte, i.e. a
   * client-side re-implementation of the very TTFT kill we removed from the
   * transport (and 4x more aggressive than the 180s it replaced): the server
   * kept generating and billing while the browser had already dropped to the
   * poll fallback. Slow is not failed. The user ends a wait by pressing Stop,
   * which aborts `controller` directly. */

  function _processEvent(ev) {
    gotData = true;
    if (ev.type !== "delta") console.log("[Branch SSE]", ev.type, ev);

    if (ev.type === "delta") {
      // Delta events APPEND to existing content (matching main chat behavior)
      if (typeof ev.thinking === "string") {
        assistantMsg.thinking = (assistantMsg.thinking || "") + ev.thinking;
      }
      if (typeof ev.content === "string") {
        assistantMsg.content = (assistantMsg.content || "") + ev.content;
      }
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "state") {
      // Full state snapshot — replace everything
      if (ev.content !== undefined) assistantMsg.content = ev.content;
      if (ev.thinking !== undefined) assistantMsg.thinking = ev.thinking;
      if (ev.error) assistantMsg.error = ev.error;
      if (ev.toolRounds) assistantMsg.toolRounds = ev.toolRounds;
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "phase") {
      // Update status indicator
      let statusHtml = "";
      if (ev.phase === "thinking") {
        statusHtml = '<div class="stream-status"><div class="pulse"></div> Thinking…</div>';
      } else if (ev.phase === "responding") {
        statusHtml = '<div class="stream-status"><div class="pulse"></div> Responding…</div>';
      } else if (ev.phase === "searching") {
        statusHtml = '<div class="stream-status"><div class="pulse"></div> Searching…</div>';
      }
      const body = document.getElementById(`branch-streaming-body-${msgIdx}-${branchIdx}`);
      if (body) {
        const zone = body.querySelector('[data-zone="status"]');
        if (zone) zone.innerHTML = statusHtml;
      }
    } else if (ev.type === "tool_start") {
      if (!assistantMsg.toolRounds) assistantMsg.toolRounds = [];
      assistantMsg.toolRounds.push({
        roundNum: ev.roundNum,
        query: ev.query || ev.toolName || "",
        toolName: ev.toolName || "search",
        status: "searching",
        results: [],
      });
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "tool_progress") {
      const r = (assistantMsg.toolRounds || []).find(r => r.roundNum === ev.roundNum);
      if (r) {
        if (typeof r._partialOutput !== "string") r._partialOutput = "";
        r._partialOutput += (ev.chunk || "");
      }
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "tool_result") {
      const r = (assistantMsg.toolRounds || []).find(r => r.roundNum === ev.roundNum);
      if (r) {
        r.results = ev.results;
        r.status = "done";
        r.approvalId = null;
        if (ev.searchDiag) r.searchDiag = ev.searchDiag;
        if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
        if (ev.vertical) r.vertical = ev.vertical;
        if (ev.verticals) r.verticals = ev.verticals;
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
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "tool_complete") {
      // Store toolContent on the round for preview
      if (assistantMsg.toolRounds) {
        const r = assistantMsg.toolRounds.find(r => r.roundNum === ev.roundNum && r.toolCallId === ev.toolCallId);
        if (r) {
          r.toolContent = ev.toolContent || null;
          if (ev.toolTokens != null) r.toolTokens = ev.toolTokens;
          if (ev.compactionLayer) {
            r.compactionLayer = ev.compactionLayer;
            r.compactedFromChars = ev.compactedFromChars;
            r.compactedToChars = ev.compactedToChars;
          }
        }
      }
      // ★ Re-render branch UI so preview button appears reactively
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "tool_compacted") {
      // Per-tool compaction stamp — see ui.js handler for full doc.
      if (assistantMsg.toolRounds) {
        const r = assistantMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId);
        if (r) {
          r.compactionLayer = ev.compactionLayer || r.compactionLayer || "L1";
          if (ev.compactedFromChars != null) r.compactedFromChars = ev.compactedFromChars;
          if (ev.compactedToChars != null) r.compactedToChars = ev.compactedToChars;
          if (ev.toolTokens != null) r.toolTokens = ev.toolTokens;
        }
      }
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "project_external_edit") {
      // ★ Git-shim: external edits captured outside Tofu round boundary.
      const files = ev.files || [];
      const sha = (ev.sha || '').slice(0, 7);
      try {
        if (typeof showToast === 'function') {
          const preview = files.slice(0, 3).join(', ') + (files.length > 3 ? ` +${files.length - 3} more` : '');
          showToast(`📝 Captured ${files.length} external edit(s) — ${preview}${sha ? ' · ' + sha : ''}`, 'info');
        }
      } catch (e) { console.warn('[branch.project_external_edit] toast failed', e); }
      console.log('[branch.project_external_edit]', { sha, files });
    } else if (ev.type === "write_approval_request") {
      /* The REAL server write gate (lib/tasks_pkg/tool_dispatch/_approval.py).
       * The old placeholder-event branch fed dead buttons and is gone — the
       * server never emits that type (only registered in lib/agent_core/events.py). */
      _branchHandleWriteApproval(ev, assistantMsg);
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
    } else if (ev.type === "done") {
      /* ★ DIAGNOSTIC: log endpoint/swarm done event */
      const _doneErrSummary = ev.error
        ? (typeof ev.error === 'object' ? (ev.error.kind || 'unknown') : String(ev.error).slice(0, 100))
        : 'none';
      console.log(
        `[processEvent/endpoint] DONE — ` +
        `finishReason=${ev.finishReason || 'none'} ` +
        `contentLen=${assistantMsg.content?.length || 0} ` +
        `error=${_doneErrSummary} model=${ev.model || 'unknown'}`
      );
      if (ev._diagnostics) {
        console.warn(`[processEvent/endpoint]  SERVER DIAGNOSTICS:`, ev._diagnostics);
      }
      if (ev.error) assistantMsg.error = ev.error;
      if (ev.finishReason) assistantMsg.finishReason = ev.finishReason;
      if (ev.model) assistantMsg.model = ev.model;
      else if (ev.preset) assistantMsg.model = ev.preset;
      else if (ev.effort) assistantMsg.model = ev.effort;
      if (ev.thinkingDepth) assistantMsg.thinkingDepth = ev.thinkingDepth;
      if (ev.toolSummary) assistantMsg.toolSummary = ev.toolSummary;
      if (ev.usage) assistantMsg.usage = ev.usage;
      /* ★ git-shim: round commit sha for redo/diff references */
      if (ev.gitSha) assistantMsg._gitSha = ev.gitSha;
      return "done";
    } else if (ev.type === "error") {
      const _errSummary = ev.error
        ? (typeof ev.error === 'object' ? (ev.error.kind || ev.error.message || 'unknown') : String(ev.error))
        : (ev.message || 'unknown');
      console.error(`[processEvent/endpoint] ERROR event: ${_errSummary}`);
      /* Branch SSE error events come through pre-typed when emitted by
       * the orchestrator; legacy sources (raw error strings) get wrapped
       * by normalizeErrorEnvelope() into a generic envelope. */
      assistantMsg.error = normalizeErrorEnvelope(
        ev.error || ev.message || 'Unknown error');
      return "done";
    }

    // Periodic save (every 3s, like main chat)
    var _brNow = Date.now();
    if (_brNow - lastSave > 3000) {
      lastSave = _brNow;
      saveConversations(conv.id);
    }
    /* ★ No cache write during streaming — server checkpoints to DB every 5s */
    return null;
  }

  // SSE fetch
  try {
    const res = await Api.chat.streamResponse(taskId, { signal: controller.signal });
    if (!res || !res.ok) throw new Error(`SSE HTTP ${res ? res.status : 'no response'}`);
    await readSSEStream(res, {
      onLine(line) {
        const l = line.trim();
        if (!l.startsWith("data: ")) return false;
        try {
          const ev = JSON.parse(l.slice(6));
          return _processEvent(ev) === "done";
        } catch { return false; }
      },
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    console.warn("Branch SSE failed, falling back to poll:", e.message);
    await _branchStreamPoll(conv, msgIdx, branchIdx, branch, assistantMsg, taskId, controller, bk);
  }
}

// ── Polling fallback ──
async function _branchStreamPoll(conv, msgIdx, branchIdx, branch, assistantMsg, taskId, controller, bk) {
  let retries = 0;
  while (retries < 120) {
    if (controller.signal.aborted) return;
    await new Promise(r => setTimeout(r, 1500));
    try {
      const res = await Api.chat.poll(taskId);
      if (!res || !res.ok) {
        if (res && res.status === 404) {
          // Task gone (server restarted / cleaned up) — stop polling
          console.warn(`[_branchStreamPoll] 404 for task ${taskId.slice(0,8)} — stopping`);
          return;
        }
        retries++;
        continue;
      }
      const data = await res.json();
      if (data.thinking !== undefined) assistantMsg.thinking = data.thinking;
      if (data.content !== undefined) assistantMsg.content = data.content;
      if (data.toolRounds) assistantMsg.toolRounds = data.toolRounds;
      _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg);
      if (Date.now() - (assistantMsg._lastSave || 0) > 3000) {
        assistantMsg._lastSave = Date.now();
        saveConversations(conv.id);
      }
      if (data.status === "done" || data.status === "error") {
        if (data.error) assistantMsg.error = data.error;
        if (data.finishReason) assistantMsg.finishReason = data.finishReason;
        if (data.model) assistantMsg.model = data.model;
        else if (data.preset) assistantMsg.model = data.preset;
        if (data.toolSummary) assistantMsg.toolSummary = data.toolSummary;
        if (data.usage) assistantMsg.usage = data.usage;
        return;
      }
      retries = 0;
    } catch { retries++; }
  }
}

// ── Update streaming UI zones ──
function _updateBranchStreamingUI(msgIdx, branchIdx, assistantMsg) {
  const body = document.getElementById(`branch-streaming-body-${msgIdx}-${branchIdx}`);
  if (!body) return;

  // Tool call zone — use the full renderer for rich display
  const toolZone = body.querySelector('[data-zone="tool"]');
  if (toolZone && assistantMsg.toolRounds?.length) {
    toolZone.innerHTML = renderToolRoundsHTML(assistantMsg.toolRounds, true);
  }

  // Thinking zone
  const thinkZone = body.querySelector('[data-zone="thinking"]');
  if (thinkZone && assistantMsg.thinking) {
    const _bThinkLbl = (typeof t === 'function') ? t('stream.thinking.done') : 'Thinking Process';
    thinkZone.innerHTML = `<details class="branch-thinking" open><summary>${escapeHtml(_bThinkLbl)}</summary><div>${escapeHtml(assistantMsg.thinking)}</div></details>`;
  }

  // Content zone
  const contentZone = body.querySelector('[data-zone="content"]');
  if (contentZone && assistantMsg.content) {
    try { contentZone.innerHTML = renderMarkdown(assistantMsg.content); }
    catch { contentZone.innerHTML = escapeHtml(assistantMsg.content); }
  }

  _scrollBranchToBottom(msgIdx, branchIdx);

  // Keep stop button in sync during branch streaming
  updateSendButton();
}

// ── Rebuild only the branch panel DOM without touching the main chat ──
function _rebuildBranchPanelDOM(msg, msgIdx, branchIdx) {
  const panelEl = document.getElementById(`branch-panel-${msgIdx}-${branchIdx}`);
  const newHtml = _renderBranchPanel(msg, msgIdx, branchIdx);
  if (panelEl) {
    // Replace existing panel in-place
    const tmp = document.createElement("div");
    tmp.innerHTML = newHtml;
    const newPanel = tmp.firstElementChild;
    if (newPanel) panelEl.replaceWith(newPanel);
    return;
  }
  // Panel doesn't exist yet — find the right insertion point
  const msgEl = document.getElementById(`msg-${msgIdx}`);
  if (!msgEl) return;

  // For anchored/inline branches, insert inside the wrapper element
  const inlineWrapper = document.getElementById(`branch-inline-${msgIdx}-${branchIdx}`);
  if (inlineWrapper) {
    const tmp = document.createElement("div");
    tmp.innerHTML = newHtml;
    const newPanel = tmp.firstElementChild;
    if (newPanel) inlineWrapper.appendChild(newPanel);
    return;
  }

  // Fallback: find or create branch-zone inside this message
  let zone = msgEl.querySelector(".branch-zone");
  if (!zone) {
    zone = document.createElement("div");
    zone.className = "branch-zone";
    const content = msgEl.querySelector(".message-content");
    if (content) content.appendChild(zone);
    else msgEl.appendChild(zone);
  }
  const tmp = document.createElement("div");
  tmp.innerHTML = newHtml;
  const newPanel = tmp.firstElementChild;
  if (newPanel) zone.appendChild(newPanel);
}

// ── Finish branch stream — cleanup ──
function _finishBranchStream(conv, msgIdx, branchIdx, branch, bk) {
  _branchStreams.delete(bk);
  activeStreams.delete(bk);
  branch.activeTaskId = null;
  renderConversationList();
  saveConversations(conv.id);
  syncConversationToServer(conv);
  updateSendButton();
  // ── Targeted DOM update: rebuild ONLY the branch panel, no full renderChat ──
  if (activeConvId === conv.id) {
    const msg = conv.messages[msgIdx];
    if (msg) _rebuildBranchPanelDOM(msg, msgIdx, branchIdx);
    // Re-enter branch mode if this branch panel is still open
    if (_activeBranch?.msgIdx === msgIdx && _activeBranch?.branchIdx === branchIdx) {
      _enterBranchMode(msgIdx, branchIdx);
    }
  }
}

// ══════════════════════════════════════════
//  Reconnect to branch streams after page refresh
// ══════════════════════════════════════════
async function _reconnectBranchStream(conv, msgIdx, branchIdx, branch) {
  const bk = _branchKey(conv.id, msgIdx, branchIdx);
  if (_branchStreams.has(bk)) return; // already streaming

  const taskId = branch.activeTaskId;
  if (!taskId) return;

  try {
    const res = await Api.chat.poll(taskId);
    if (!res || !res.ok) {
      branch.activeTaskId = null;
      saveConversations(conv.id);
      if (activeConvId === conv.id) {
        const parentMsg = conv.messages[msgIdx];
        if (parentMsg) _rebuildBranchPanelDOM(parentMsg, msgIdx, branchIdx);
      }
      return;
    }
    const data = await res.json();

    // If already done, apply final state
    if (data.status === "done" || data.status === "error") {
      const msgs = branch.messages || [];
      const last = msgs[msgs.length - 1];
      if (last?.role === "assistant") {
        if (data.content !== undefined) last.content = data.content;
        if (data.thinking !== undefined) last.thinking = data.thinking;
        if (data.error) last.error = data.error;
        if (data.finishReason) last.finishReason = data.finishReason;
        if (data.preset) last.preset = data.preset;
        if (data.toolSummary) last.toolSummary = data.toolSummary;
        if (data.usage) last.usage = data.usage;
        if (data.toolRounds) last.toolRounds = data.toolRounds;
      }
      branch.activeTaskId = null;
      saveConversations(conv.id);
      if (activeConvId === conv.id) {
        const parentMsg = conv.messages[msgIdx];
        if (parentMsg) _rebuildBranchPanelDOM(parentMsg, msgIdx, branchIdx);
      }
      return;
    }

    // Still running — reconnect SSE
    const msgs = branch.messages || [];
    const assistantMsg = msgs[msgs.length - 1];
    if (!assistantMsg || assistantMsg.role !== "assistant") return;

    const controller = new AbortController();
    _branchStreams.set(bk, { controller, taskId, convId: conv.id, msgIdx, branchIdx });
    activeStreams.set(bk, { controller, taskId });
    renderConversationList();
    if (activeConvId === conv.id) {
      const parentMsg = conv.messages[msgIdx];
      if (parentMsg) _rebuildBranchPanelDOM(parentMsg, msgIdx, branchIdx);
      if (_activeBranch?.msgIdx === msgIdx && _activeBranch?.branchIdx === branchIdx) {
        _enterBranchMode(msgIdx, branchIdx);
      }
    }

    try {
      await _branchStreamSSE(conv, msgIdx, branchIdx, branch, assistantMsg, taskId, controller, bk);
    } catch (e) {
      if (e.name !== "AbortError") console.error("Branch reconnect error:", e);
    } finally {
      _finishBranchStream(conv, msgIdx, branchIdx, branch, bk);
    }
  } catch (e) {
    branch.activeTaskId = null;
    saveConversations(conv.id);
    if (activeConvId === conv.id) {
      const parentMsg = conv.messages?.[msgIdx];
      if (parentMsg) _rebuildBranchPanelDOM(parentMsg, msgIdx, branchIdx);
    }
  }
}

// ── Initialize: scan all conversations for active branch tasks ──
function initBranchReconnect() {
  for (const conv of conversations) {
    for (let mi = 0; mi < conv.messages.length; mi++) {
      const msg = conv.messages[mi];
      if (!msg.branches) continue;
      for (let bi = 0; bi < msg.branches.length; bi++) {
        const branch = msg.branches[bi];
        if (branch.activeTaskId) {
          _reconnectBranchStream(conv, mi, bi, branch);
        }
      }
    }
  }
}

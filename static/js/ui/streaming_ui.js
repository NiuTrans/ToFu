/* ═══════════════════════════════════════════════════════════════════
   streaming ui — extracted from ui.js (split 2026-05-28)

   Streaming UI: zone management, updateStreamingUI, _syncToolRoundsDOM, swarm panels, finishStream.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ══════════════════════════════════════════════
//  Streaming UI
// ══════════════════════════════════════════════
function _ensureStreamZones(body) {
  if (body.querySelector('[data-zone="tool"]')) return;
  body.innerHTML =
    '<div data-zone="memprefetch"></div>' +
    '<div data-zone="swarmInbox"></div>' +  /* async swarm-update chips */
    '<div data-zone="tool"></div>' +
    '<div data-zone="thinking"></div>' +
    '<div data-zone="content"></div>' +
    '<div data-zone="fc"></div>' +
    '<div data-zone="status"></div>';
}

/* ★ Helper: check if user has an active text selection inside the streaming message area */
function _hasSelectionInStreaming() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return false;
  const body = document.getElementById("streaming-body");
  if (!body) return false;
  for (let i = 0; i < sel.rangeCount; i++) {
    const r = sel.getRangeAt(i);
    if (body.contains(r.startContainer) || body.contains(r.endContainer))
      return true;
  }
  return false;
}
let _pendingStreamMsg = null;
let _pendingStreamTimer = null;
/* ★ Cached zone references — avoid querySelector on every frame */
let _streamZoneCache = { body: null, tool: null, think: null, content: null, fc: null, status: null, swarmInbox: null };
function _getStreamZones() {
  const body = document.getElementById("streaming-body");
  if (!body) return null;
  if (_streamZoneCache.body !== body) {
    _ensureStreamZones(body);
    _streamZoneCache = {
      body,
      memprefetch: body.querySelector('[data-zone="memprefetch"]'),
      swarmInbox:  body.querySelector('[data-zone="swarmInbox"]'),
      tool: body.querySelector('[data-zone="tool"]'),
      think: body.querySelector('[data-zone="thinking"]'),
      content: body.querySelector('[data-zone="content"]'),
      fc: body.querySelector('[data-zone="fc"]'),
      status: body.querySelector('[data-zone="status"]'),
    };
  }
  return _streamZoneCache;
}
function updateStreamingUI(msg) {
  const zones = _getStreamZones();
  if (!zones) return;
  const { body, memprefetch: memprefetchZone, tool: toolZone, think: thinkZone, content: contentZone, fc: fcZone, status: statusZone } = zones;
  const rounds = msg.toolRounds || [];
  const hasActiveSearch = rounds.some((r) => r.status === "searching");
  /* Stamp freshness across the WHOLE active conversation so the gradient
   * shifts in real time as new tool rounds stream in.  Per-message
   * stamping would be wrong: freshness is relative to the newest round
   * in the conversation, not within a single assistant message. */
  if (typeof activeConvId !== 'undefined' && activeConvId
      && typeof conversations !== 'undefined') {
    const _conv = conversations.find(c => c && c.id === activeConvId);
    if (_conv) _stampFreshness(_conv);
  }
  _syncToolRoundsDOM(toolZone, rounds);
  /* ★ Async swarm: render inbox-inject chips above the tool zone so the
   *   user sees "received N async swarm updates" the moment they land. */
  const _swarmInboxZone = zones.swarmInbox;
  if (_swarmInboxZone) {
    const injects = msg._inboxInjects || [];
    const html = _buildSwarmInboxChipsHTML(injects);
    /* Use a fingerprint to avoid pointless DOM rewrites */
    let fp = 0;
    for (const inj of injects) {
      fp = fp * 31 + (inj.round | 0);
      fp = fp * 31 + (inj.count | 0);
      fp = fp * 31 + (inj.agentIds ? inj.agentIds.length : 0);
    }
    if (_swarmInboxZone._fp !== fp) {
      _swarmInboxZone._fp = fp;
      _swarmInboxZone.innerHTML = html;
    }
  }
  /* ★ Memory Prefetch indicator (streaming path) */
  if (memprefetchZone) {
    const mp = msg._memoryPrefetch;
    /* ★ MCP login-hint chip (rendered alongside prefetch chip).
     *   The hint is pushed by handlers that detect a pending MCP login push,
     *   so the user knows to tap "Approve" on their mobile-office app
     *   before the subprocess times out. */
    const lh = msg._mcpLoginHint;
    const lhHtml = lh ? renderMcpLoginHintHtml(lh) : '';
    const html = (lhHtml || '') + (mp ? renderMemoryPrefetchHtml(mp) : '');
    /* fp includes snippet length so a late-arriving tool_result with a
     * longer snippet triggers a re-render (earlier fp only checked
     * phase+updatedAt, which can stay the same when we just append). */
    const fp = (lh ? `L|${lh.phase}|${lh.username||''}|${lh.updatedAt||0}|${(lh.snippet||'').length}|` : '') +
               (mp ? `${mp.phase}|${mp.selected||0}|${mp.candidates||0}|${mp.totalMs||0}` : '');
    if (memprefetchZone.getAttribute('data-mp-fp') !== fp) {
      memprefetchZone.setAttribute('data-mp-fp', fp);
      memprefetchZone.innerHTML = html;
    }
  }
  // ★ Live file-changes tracker — update during streaming.
  //   Server-side derivation via /api/v1/messages/extract-file-changes;
  //   the request is fingerprint-deduplicated so only genuine state
  //   changes trigger a fetch. Render reads from the in-memory cache;
  //   the FIRST render after a fingerprint change shows nothing while
  //   the fetch is in flight, then the result lands and the next tick
  //   picks it up via the cached accessor.
  if (fcZone && rounds.length > 0) {
    const _fcFp = _fcFingerprint(rounds);
    if (fcZone._roundsFp !== _fcFp) {
      fcZone._roundsFp = _fcFp;
      _extractFileChangesFromRoundsAsync(rounds).then(liveFiles => {
        // Bail if a newer fingerprint has superseded this one.
        if (fcZone._roundsFp !== _fcFp) return;
        const fcKey = liveFiles.map(f =>
          `${f.root||''}:${f.path}:${f.action}:${f.ok}:${f.pending||''}`
        ).join('|');
        if (fcZone.getAttribute('data-fc-key') !== fcKey) {
          fcZone.setAttribute('data-fc-key', fcKey);
          fcZone.innerHTML = liveFiles.length
            ? _renderFileChangesHtml(liveFiles, true) : '';
        }
      });
    }
  } else if (fcZone) {
    if (fcZone.getAttribute('data-fc-key')) {
      fcZone.setAttribute('data-fc-key', '');
      fcZone.innerHTML = '';
    }
  }
  if (msg.thinking) {
    let block = thinkZone.querySelector(".thinking-block");
    if (!block) {
      const still = !msg.content;
      thinkZone.innerHTML = `<div class="thinking-block ${still ? "expanded" : ""}" onclick="this.classList.toggle('expanded')"><div class="thinking-header"><span class="thinking-label">${still ? "Thinking..." : "Thinking Process"}</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
      block = thinkZone.querySelector(".thinking-block");
    }
    const textEl = block.querySelector(".thinking-text");
    if (textEl && textEl.textContent !== msg.thinking) textEl.textContent = msg.thinking;
    const labelEl = block.querySelector(".thinking-label");
    const _thinkLbl = msg.content ? "Thinking Process" : "Thinking...";
    if (labelEl && labelEl.textContent !== _thinkLbl)
      labelEl.textContent = _thinkLbl;
  }
  /* ★ FIX: Skip content DOM update while user has active selection to prevent flicker/deselect */
  if (_hasSelectionInStreaming()) {
    _pendingStreamMsg = msg;
    if (!_pendingStreamTimer) {
      _pendingStreamTimer = setInterval(() => {
        if (!_hasSelectionInStreaming() && _pendingStreamMsg) {
          const m = _pendingStreamMsg;
          _pendingStreamMsg = null;
          clearInterval(_pendingStreamTimer);
          _pendingStreamTimer = null;
          updateStreamingUI(m);
        }
      }, 300);
    }
    return;
  }
  _pendingStreamMsg = null;
  if (_pendingStreamTimer) {
    clearInterval(_pendingStreamTimer);
    _pendingStreamTimer = null;
  }
  /* ★ Perf: tell renderMarkdown to skip syntax highlighting for the duration
   * of this streaming render. highlightAuto on a growing code block re-runs
   * every frame and dominates per-token allocation (GC storm). The block is
   * highlighted exactly once at finalizeStreaming via the renderMessage path. */
  window._streamRenderNoHighlight = true;
  /* ★ Incremental content rendering: only re-render the new "tail" of content.
   * We split content at the last stable paragraph/block boundary and only update
   * the tail portion, avoiding full DOM teardown on every token.
   *
   * PERF STRATEGY:
   * - Cache the rendered HTML of the "frozen" portion (everything before the last
   *   paragraph boundary).  This avoids calling renderMarkdown on potentially 10k+
   *   chars of already-rendered content on every frame.
   * - Move the freeze point forward aggressively: whenever the tail grows past
   *   REFREEZE_THRESHOLD chars, find a new paragraph boundary and advance.
   * - The tail (typically 200-800 chars) is the ONLY part re-rendered each frame.
   */
  try {
    if (msg.content) {
      const content = msg.content;
      const prevLen = contentZone._streamRendered || 0;
      const frozenLen = contentZone._frozenLen || 0;
      const REFREEZE_THRESHOLD = 600; // advance freeze point when tail exceeds this

      const mdContentEl = contentZone.querySelector(".md-content");
      const tailEl = mdContentEl && mdContentEl.querySelector(".md-stream-tail");
      const tailLen = content.length - frozenLen;

      if (frozenLen > 0 && tailLen < REFREEZE_THRESHOLD && tailEl && mdContentEl) {
        /* Fast path: tail is small, just re-render the tail portion.
         * The frozen HTML in the DOM is untouched — zero work for that part. */
        const tail = content.slice(frozenLen);
        try {
          tailEl.innerHTML = renderMarkdown(tail);
        } catch (_) {
          tailEl.innerHTML = escapeHtml(tail);
        }
        contentZone._streamRendered = content.length;
      } else {
        /* Need to (re)freeze: either first render, or tail grew past threshold.
         * Find the last stable paragraph boundary and split there. */
        const freezeIdx = content.lastIndexOf("\n\n", content.length - 60);

        if (freezeIdx > 100 && content.length > 300) {
          const frozenText = content.slice(0, freezeIdx);
          const tailText = content.slice(freezeIdx);

          /* ★ PERF: reuse cached frozen HTML if freeze point didn't move */
          let frozenHtml;
          if (frozenLen === freezeIdx && contentZone._frozenHtml) {
            frozenHtml = contentZone._frozenHtml;
          } else {
            frozenHtml = renderMarkdown(frozenText);
            contentZone._frozenHtml = frozenHtml;
          }

          const tailHtml = renderMarkdown(tailText);
          contentZone.innerHTML =
            `<div class="md-content">${frozenHtml}<div class="md-stream-tail">${tailHtml}</div></div>`;
          contentZone._frozenLen = freezeIdx;
        } else {
          /* Content too short to split — render whole thing */
          contentZone.innerHTML = `<div class="md-content">${renderMarkdown(content)}</div>`;
          contentZone._frozenLen = 0;
          contentZone._frozenHtml = null;
        }
        contentZone._streamRendered = content.length;
        /* Restore collapsed states for code blocks */
        contentZone.querySelectorAll("pre[data-collapsed]").forEach((pre) => {
          pre.setAttribute("data-collapsed", "true");
          const btn = pre.querySelector(".code-collapse-btn");
          if (btn) btn.textContent = "Expand";
        });
      }
    } else {
      contentZone.innerHTML = "";
      contentZone._streamRendered = 0;
      contentZone._frozenLen = 0;
      contentZone._frozenHtml = null;
    }
  } catch (e) {
    contentZone.innerHTML = `<div class="md-content">${escapeHtml(msg.content || "")}</div>`;
  } finally {
    window._streamRenderNoHighlight = false;
  }
  /* ★ Phase-aware status indicator — shows what the model is doing between visible outputs */
  const phase = msg.phase;
  /* ★ Build phase HTML and only update DOM when content actually changes (prevents flicker) */
  let _phaseKey = "";
  let _phaseHtml = "";
  const _phaseIcons = { llm_thinking: "", tool_exec: "", compacting: "" };
  if (phase && phase.phase === "thinking_active") {
    /* ★ Model is actively generating thinking tokens (works on ALL rounds,
     *   even when msg.content is already non-empty from previous tool rounds) */
    const _thLen = phase._thinkingLen || 0;
    const _thSize = _thLen >= 1024 ? `${(_thLen / 1024).toFixed(1)}k` : `${_thLen}`;
    _phaseKey = "thinking-active";
    _phaseHtml = `<div class="stream-phase stream-phase-thinking"><span class="stream-phase-text">Reasoning<span class="stream-phase-counter" data-counter="thinking">${_thSize} chars</span></span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "llm_thinking") {
    const icon = _phaseIcons[phase.phase];
    _phaseKey = "think:" + phase.detail + (phase.toolContext || "");
    const ctx = phase.toolContext
      ? `<span class="stream-phase-ctx">${escapeHtml(phase.toolContext)}</span>`
      : "";
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-icon">${icon}</span><span class="stream-phase-text">${escapeHtml(phase.detail)}${ctx}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "compacting") {
    _phaseKey = "compact:" + phase.detail;
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(phase.detail)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "retrying") {
    _phaseKey = "retry:" + (phase.attempt || 0);
    _phaseHtml = `<div class="stream-phase stream-phase-retrying"><span class="stream-phase-icon">⟳</span><span class="stream-phase-text">${escapeHtml(phase.detail || 'Retrying…')}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "tool_exec" && !hasActiveSearch) {
    _phaseKey = "exec:" + phase.detail;
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(phase.detail)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "working" && phase.detail) {
    /* Generic "working" phase from external backends (e.g. "Initializing Claude Code...") */
    _phaseKey = "working:" + phase.detail;
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(phase.detail)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "autopilot_thinking") {
    /* ★ Autopilot virtual-user is composing the next reply — and may be
     *   running tools to investigate first. The detail string carries
     *   the live tool name + first arg so the user can see the probing
     *   in real time (e.g. "Autopilot investigating · read_files lib/foo.py"). */
    _phaseKey = "autopilot:" + (phase.detail || "");
    _phaseHtml = `<div class="stream-phase stream-phase-autopilot"><span class="stream-phase-text">${escapeHtml(phase.detail || 'Autopilot is generating the next user reply…')}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (hasActiveSearch) {
    _phaseKey = "search";
    _phaseHtml = "";
  } else if (!msg.content && !msg.thinking) {
    _phaseKey = "wait";
    _phaseHtml =
      '<div class="stream-status"><div class="pulse"></div> Waiting…</div>';
  } else if (!msg.content && msg.thinking) {
    _phaseKey = "think-only";
    const _thLen = msg.thinking.length;
    const _thSize = _thLen >= 1024 ? `${(_thLen / 1024).toFixed(1)}k` : `${_thLen}`;
    _phaseHtml = `<div class="stream-phase stream-phase-thinking"><span class="stream-phase-text">Deep thinking<span class="stream-phase-counter">${_thSize} chars</span></span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else {
    _phaseKey = "none";
    _phaseHtml = "";
  }
  if (statusZone.getAttribute("data-phase-key") !== _phaseKey) {
    statusZone.setAttribute("data-phase-key", _phaseKey);
    statusZone.innerHTML = _phaseHtml;
  }
  /* ★ Live counter update for thinking phases — avoids full DOM rebuild on every token */
  if (_phaseKey === "think-only" || _phaseKey === "thinking-active") {
    const _ctrEl = statusZone.querySelector('.stream-phase-counter');
    if (_ctrEl) {
      const _tl = (phase && phase._thinkingLen) || (msg.thinking ? msg.thinking.length : 0);
      const _ts = _tl >= 1024 ? `${(_tl / 1024).toFixed(1)}k` : `${_tl}`;
      _ctrEl.textContent = _ts + ' chars';
    }
  }
  /* ★ FIX: Only auto-scroll when user hasn't scrolled away (smaller threshold to avoid hijacking) */
  if (isNearBottom(80)) scrollToBottom();
}

function _syncToolRoundsDOM(container, rounds) {
  // ★ Fast-path: skip if rounds haven't changed since last sync
  let _fp = rounds.length;
  for (let i = 0; i < rounds.length; i++) {
    const r = rounds[i];
    _fp = _fp * 31 + (r.roundNum | 0);
    _fp = _fp * 31 + (r.status === 'searching' ? 1 : r.status === 'done' ? 2
        : r.status === 'awaiting_human' ? 3 : r.status === 'submitted' ? 4
        : r.status === 'pending_approval' ? 5 : 0);
    _fp = _fp * 31 + ((r.results && r.results.length) || 0);
    _fp = _fp * 31 + (r.toolContent ? 1 : 0);
    _fp = _fp * 31 + (r._hgTranslating ? 1 : 0);
    /* compactionLayer set means a tool_compacted SSE just landed —
     * re-render so the row picks up the COMPACTED label. Include
     * the from/to char counts so a follow-up compaction (e.g. L1 → L3
     * graduation) also re-renders. */
    _fp = _fp * 31 + (r.compactionLayer ? r.compactionLayer.length : 0);
    _fp = _fp * 31 + (r.compactedToChars | 0);
    _fp = _fp * 31 + (r.compactedFromChars | 0);
    if (r._translatedQuestion) _fp = _fp * 31 + r._translatedQuestion.length;
    if (r._timerPolls) _fp = _fp * 31 + r._timerPolls.length;
    if (r._timerSkipCount) _fp = _fp * 31 + r._timerSkipCount;
    /* ★ Swarm fields — without these, swarm_agent_* events mutate
     *   round._swarmAgents but the fingerprint stays equal, the gate
     *   bails, and the panel never re-renders until page refresh. */
    if (r._swarm) {
      _fp = _fp * 31 + (r._swarmActive ? 1 : 0);
      _fp = _fp * 31 + (r._asyncRunning ? 1 : 0);
      const agents = r._swarmAgents || [];
      _fp = _fp * 31 + agents.length;
      for (const a of agents) {
        _fp = _fp * 31 + (a.status === 'running' ? 1 : a.status === 'done' ? 2
            : a.status === 'failed' ? 3 : a.status === 'thinking' ? 4 : 0);
        _fp = _fp * 31 + ((a.phase || '').length);
        _fp = _fp * 31 + ((a.preview || '').length);
        _fp = _fp * 31 + ((a.tools || []).length);
        _fp = _fp * 31 + ((a.modifiedFiles | 0) + 1);
        _fp = _fp * 31 + (a._toolCalls ? a._toolCalls.length : 0);
        if (a._toolCalls && a._toolCalls.length) {
          /* Last call's status flips on every finish event — must
           * be in the fingerprint or the timeline freezes. */
          const last = a._toolCalls[a._toolCalls.length - 1];
          _fp = _fp * 31 + (last.status === 'running' ? 1 : last.status === 'done' ? 2 : 3);
          _fp = _fp * 31 + ((last.elapsed * 100) | 0);
        }
      }
    }
  }
  if (container._roundsFingerprint === _fp) return;
  container._roundsFingerprint = _fp;

  /* ★ UNIFIED: every round — including swarm panels — lives inside the
   *   single ptool-panel, in the order the main agent issued them.
   *   That preserves the natural call sequence (spawn_agents →
   *   await_agents → get_agent_result are just tool calls to the
   *   parent) and lets users follow what the model did chronologically.
   *   Swarm rounds still render the full info-rich agent dashboard,
   *   they just live inside a [data-prn] slot now. */
  const toolRounds = rounds.slice();

  // ── Unified tool panel: all tools in chronological order ──
  const unifiedPanel = container.querySelector(".ptool-panel");
  if (toolRounds.length > 0) {
    const anyActive = toolRounds.some((r) => r.status === "searching");
    const count = toolRounds.length;
    const headerLabel = anyActive
      ? `Working… (${count})`
      : `${count} tool${count > 1 ? "s" : ""} used`;
    let body;
    if (!unifiedPanel) {
      const el = document.createElement("div");
      el.className =
        "ptool-panel animation-slideUp" +
        (anyActive ? " ptool-panel-active" : "");
      el.innerHTML = `<div class="ptool-panel-header"><span class="ptool-panel-label">${headerLabel}</span></div><div class="ptool-panel-body"></div>`;
      container.appendChild(el);
      body = el.querySelector(".ptool-panel-body");
    } else {
      unifiedPanel.className =
        "ptool-panel" + (anyActive ? " ptool-panel-active" : "");
      const lbl = unifiedPanel.querySelector(".ptool-panel-label");
      if (lbl) lbl.textContent = headerLabel;
      body = unifiedPanel.querySelector(".ptool-panel-body");
    }
    if (body) {
      const _TOOL_VISIBLE_WINDOW = 50;
      const anyStreaming = toolRounds.some(r => r.status === "searching");
      const visibleRounds = anyStreaming && toolRounds.length > _TOOL_VISIBLE_WINDOW
        ? (() => {
            const active = toolRounds.filter(r => r.status === "searching" || r.status === "pending_approval");
            const done = toolRounds.filter(r => r.status !== "searching" && r.status !== "pending_approval");
            const tail = done.slice(-_TOOL_VISIBLE_WINDOW);
            if (done.length > _TOOL_VISIBLE_WINDOW && !body.querySelector('.ptool-truncated')) {
              const trunc = document.createElement("div");
              trunc.className = "ptool-truncated";
              const hiddenN = done.length - _TOOL_VISIBLE_WINDOW;
              trunc.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span>${hiddenN} earlier tool calls hidden — click to expand</span>`;
              trunc.onclick = () => { trunc.remove(); body._showAll = true; container._roundsFingerprint = null; _syncToolRoundsDOM(container, rounds); };
              body.prepend(trunc);
            } else if (body.querySelector('.ptool-truncated')) {
              const truncEl = body.querySelector('.ptool-truncated');
              const hiddenN2 = done.length - _TOOL_VISIBLE_WINDOW;
              truncEl.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span>${hiddenN2} earlier tool calls hidden — click to expand</span>`;
            }
            return body._showAll ? toolRounds : [...tail, ...active];
          })()
        : toolRounds;
      for (const round of visibleRounds) {
        const rn = round.roundNum;
        const isActive = round.status === "searching";
        const _isSwarm = _isRoundSwarm(round);
        let slot = body.querySelector(`[data-prn="${rn}"]`);
        /* ★ Determine if this round needs an interactive card (HG, stdin, approval).
         *   Interactive cards are tall (200-300px) and must NOT be collapsed by
         *   content-visibility:auto (which assumes 32px intrinsic size for off-screen
         *   slots).  Force content-visibility:visible on these slots.
         *   Swarm panels are also tall and re-render frequently — same treatment. */
        const _isInteractive = round.status === "awaiting_human" || round.status === "awaiting_stdin"
          || round.status === "pending_approval";
        const _renderRow = (r, active) => _isRoundSwarm(r)
          ? _buildSwarmPanelHTML(r)
          : _renderUnifiedToolLine(r, active);
        if (!slot) {
          slot = document.createElement("div");
          slot.setAttribute("data-prn", rn);
          if (_isSwarm) slot.setAttribute("data-prn-kind", "swarm");
          if (round._hgTranslating) slot.setAttribute("data-hg-translating", "1");
          if (_isInteractive || _isSwarm) slot.style.contentVisibility = "visible";
          slot.innerHTML = _renderRow(round, isActive);
          body.appendChild(slot);
        } else if (_isSwarm) {
          /* Swarm rounds change frequently (per-agent phase / preview / tool-call
           * timeline) — always rebuild from the latest snapshot. The fingerprint
           * gate above already guaranteed something actually changed. */
          slot.style.contentVisibility = "visible";
          slot.innerHTML = _buildSwarmPanelHTML(round);
        } else if (isActive || round.status === "pending_approval") {
          if (_isInteractive) slot.style.contentVisibility = "visible";
          slot.innerHTML = _renderUnifiedToolLine(round, isActive);
        } else if (
          slot.querySelector(".ptool-active") ||
          slot.querySelector(".ptool-cmd-running") ||
          slot.querySelector(".ptool-pending") ||
          slot.querySelector(".code-exec-running")
        ) {
          if (_isInteractive) slot.style.contentVisibility = "visible";
          slot.innerHTML = _renderUnifiedToolLine(round, false);
        } else if (slot.querySelector(".ptool-cmd-stdin")) {
          // ★ Stdin input card — avoid re-rendering while still awaiting_stdin
          //   to prevent destroying live DOM (input field) mid-type.
          if (round.status !== "awaiting_stdin" || !round.stdinId) {
            slot.style.contentVisibility = "";  // reset to CSS default
            slot.innerHTML = _renderUnifiedToolLine(round, false);
          }
          // else: still awaiting — keep the live input field intact
        } else if (slot.querySelector(".hg-card")) {
          // ★ HG interactive card — avoid re-rendering while still awaiting_human
          //   to prevent destroying live DOM (buttons, textarea) mid-click.
          //   Only re-render when: status changed away from awaiting_human, or
          //   translation state (_hgTranslating) flipped.
          if (round.status !== "awaiting_human" || !round.guidanceId) {
            // Status transitioned → rebuild as submitted/done line
            slot.style.contentVisibility = "";  // reset to CSS default
            slot.innerHTML = _renderUnifiedToolLine(round, false);
          } else {
            // Still awaiting — only update if translation state changed
            const prevTrans = slot.getAttribute("data-hg-translating") === "1";
            const nowTrans = !!round._hgTranslating;
            if (prevTrans !== nowTrans) {
              slot.innerHTML = _renderUnifiedToolLine(round, false);
              slot.setAttribute("data-hg-translating", nowTrans ? "1" : "0");
            }
            // ★ Also update translated question/options in-place (no full rebuild)
            else if (round._translatedQuestion) {
              const qEl = slot.querySelector(".hg-question");
              if (qEl) {
                const newHtml = renderMarkdown(round._translatedQuestion);
                if (qEl.innerHTML !== newHtml) qEl.innerHTML = newHtml;
              }
            }
          }
        } else if (slot.querySelector(".hg-submitted-line")) {
          // ★ Submitted HG line — only re-render when status transitions away
          if (round.status !== "submitted") {
            slot.style.contentVisibility = "";  // reset to CSS default
            slot.innerHTML = _renderUnifiedToolLine(round, false);
          }
        } else if ((round._timerPolls && round._timerPolls.length > 0) || round._timerSkipCount) {
          // ★ Timer watcher: always re-render to show latest poll results
          // (including skip heartbeats that bump _timerSkipCount).
          // This covers both the initial ptool-line → timer-watcher-block transition
          // AND subsequent poll/skip additions to an existing timer-watcher-block.
          slot.innerHTML = _renderUnifiedToolLine(round, isActive);
        } else if (round.toolContent && !slot.querySelector('[data-tc-preview]') && !slot.querySelector('.ptool-batch-done-block')) {
          const ptLine = slot.querySelector('.ptool-line');
          if (ptLine) {
            ptLine.insertAdjacentHTML('beforeend', _tcPreviewBtn(round));
          }
        } else if (round.compactionLayer && !slot.querySelector('.ptool-compaction-label')) {
          /* ★ tool_compacted SSE landed on an already-rendered slot in
           *   the in-flight bubble.  By this point tool_complete has
           *   already attached the Preview button, so the branch above
           *   bails; without this case nothing re-renders and the
           *   COMPACTED pill never materializes (only switching convs
           *   or reload would surface it).  Container fingerprint
           *   already detected the change — we just need to pick up
           *   the new label here. */
          slot.innerHTML = _renderUnifiedToolLine(round, false);
        } else if (_isInteractive && !slot.querySelector(".hg-card") && !slot.querySelector(".ptool-cmd-stdin") && !slot.querySelector(".ptool-pending")) {
          // ★ Fallback: round is in an interactive state (awaiting_human / awaiting_stdin /
          //   pending_approval) but the slot doesn't have the expected interactive card DOM.
          //   This can happen when content-visibility:auto or timing races prevent the
          //   earlier branches from triggering.  Force a re-render to show the card.
          slot.style.contentVisibility = "visible";
          slot.innerHTML = _renderUnifiedToolLine(round, false);
        }
      }
    }
  }

  /* ── Swarm panels are now inline rows inside .ptool-panel-body
   *    (rendered above by `_renderRow`), so there's no separate
   *    .swarm-round-container loop here.  If a stale container is
   *    present from a previous version of the page, drop it. */
  const _legacy = container.querySelectorAll(".swarm-round-container");
  for (const el of _legacy) el.remove();
}

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
};
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
function _recoverSwarmAgents(round) {
  const tc = round && round.toolContent;
  if (!tc || typeof tc !== "string") return [];
  let handle;
  try {
    handle = JSON.parse(tc);
  } catch (e) {
    return [];  /* tool result wasn't the JSON handle — nothing to recover */
  }
  const list = (handle && Array.isArray(handle.agents)) ? handle.agents : [];
  return list.map((a) => ({
    id: a.id || "",
    role: a.role || "agent",
    objective: a.objective || "",
    status: "done",
    phase: "done",
    preview: "",
    tools: [],
  }));
}

/* ★ Build the live swarm panel HTML (used during streaming) */
function _buildSwarmPanelHTML(round) {
  /* Live path: `_swarmAgents` is populated from swarm_* SSE events.
     Reload path: that field is gone, so recover stubs from the persisted
     handle JSON — otherwise the completed panel renders an empty body. */
  let agents = round._swarmAgents || [];
  if (agents.length === 0) {
    agents = _recoverSwarmAgents(round);
  }
  const isActive = round.status === "searching" || round._swarmActive;
  const total = agents.length;
  const running = agents.filter(a => a.status === "running" || a.status === "thinking").length;
  const done = agents.filter(a => a.status === "done" || a.status === "completed").length;
  const failed = agents.filter(a => a.status === "failed" || a.status === "error").length;
  const pending = total - done - failed - running;
  const finished = done + failed;

  /* ── Elapsed timer ── */
  let elapsed = "";
  if (round._swarmStartTime) {
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
  const stillRunningAsync = !!round._asyncRunning && (running > 0 || pending > 0);
  let statusPill;
  if (total === 0 && isActive) {
    statusPill = `<span class="sw-status-pill sw-pill-planning"><span class="sw-spinner" style="width:10px;height:10px;border-width:1.5px"></span>Planning</span>`;
  } else if (isActive) {
    statusPill = `<span class="sw-status-pill sw-pill-running"><span class="sw-spinner" style="width:10px;height:10px;border-width:1.5px"></span>Running</span>`;
  } else if (stillRunningAsync) {
    const n = running + pending;
    statusPill = `<span class="sw-status-pill sw-pill-async" title="Sub-agents are still working in the background — updates arrive automatically as the conversation continues."><span class="sw-async-dot"></span>${n} running async</span>`;
  } else if (failed > 0 && done === 0) {
    statusPill = `<span class="sw-status-pill sw-pill-error">${_SW_STATUS_SVG.failed} Failed</span>`;
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
      const preview = (a.preview || "").slice(0, 1200);
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
      else if (a.status === "running" || a.status === "thinking") sClass = "sw-a-running";
      else sClass = "sw-a-pending";

      /* ── Phase pill label ── */
      const phaseMap = {
        thinking: "Thinking…", tool_use: "Using tools", writing: "Writing…",
        searching: "Searching…", coding: "Coding…", analyzing: "Analyzing…",
        done: "Complete", completed: "Complete", failed: "Failed", error: "Error",
        pending: "Queued", running: "Working…", waiting: "Queued", queued: "Queued",
      };
      /* Status wins for a terminated agent: if status is done/failed but the
         phase got stranded at a spawn-time value (e.g. "waiting" because the
         per-agent events were routed to another panel), show the terminal
         label rather than a contradictory "waiting"/"Queued" pill next to a
         done checkmark (status/phase desync). */
      let phaseLabel;
      if (a.status === "done" || a.status === "completed") phaseLabel = "Complete";
      else if (a.status === "failed" || a.status === "error") phaseLabel = "Failed";
      else phaseLabel = phaseMap[phase] || phase || "Queued";

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
        bodyContent += `<div class="sw-a-err">${escapeHtml(preview.slice(0, 200))}</div>`;
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
function _buildSwarmDoneHTML(round, showNums) {
  /* Prefer the full panel: live `_swarmAgents`, else stubs recovered from
     the persisted handle JSON (post-reload). */
  if ((round._swarmAgents && round._swarmAgents.length > 0)
      || _recoverSwarmAgents(round).length > 0) {
    const patchedRound = Object.assign({}, round, { _swarmActive: false });
    return _buildSwarmPanelHTML(patchedRound);
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

function showStreamingUIForConv(convId) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv || conv.messages.length === 0) return;
  _destroyLazyObserver();
  _lazyConvId = convId;
  _lastRenderedFingerprint = "";
  const inner = document.getElementById("chatInner");

  /* ★ FIX (Root Cause 3): Only drop the trailing message when it actually
   * owns the streaming bubble (in-progress assistant, or in-progress critic).
   * If the last message is a user/optimizer/critic-done entry, buildTurnNav
   * will still create a dot for it, so we MUST render it statically — otherwise
   * msg-{last} is missing from the DOM and the turn-dot click is a no-op. */
  const _last = conv.messages[conv.messages.length - 1];
  const _lastIsStreamingBubble =
    !!_last && (
      (_last.role === "assistant" && !_last.done) ||
      (_last._isEndpointReview && !_last.done)
    );
  const renderMsgs = _lastIsStreamingBubble
    ? conv.messages.slice(0, -1)
    : conv.messages;
  const total = renderMsgs.length;
  const startIdx = Math.max(0, total - _INITIAL_RENDER);
  _lazyRenderedFrom = startIdx;

  let html = "";
  if (startIdx > 0) {
    _ensureLazyObserver();
    html += `<div id="_lazyLoadSentinel" class="lazy-sentinel"><span class="lazy-sentinel-text">⬆ <span class="_lazy-count">${startIdx}</span> older messages</span></div>`;
  }
  for (let i = startIdx; i < total; i++) {
    html += renderMessage(renderMsgs[i], i);
  }

  const lastMsg = _last;
  const _smTime = new Date(lastMsg?.timestamp || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (_lastIsStreamingBubble) {
    if (lastMsg.role === "assistant" && lastMsg._isEndpointPlanner) {
      html += _streamingBubbleHTML('planner', 'Planning…', _smTime);
    } else if (lastMsg.role === "assistant") {
      html += _streamingBubbleHTML('worker', 'Streaming…', _smTime);
    } else if (lastMsg._isEndpointReview) {
      html += _streamingBubbleHTML('critic', 'Reviewing…', _smTime);
    }
  }
  inner.innerHTML = html;
  if (startIdx > 0) {
    const sentinel = document.getElementById("_lazyLoadSentinel");
    if (sentinel) _lazyObserver.observe(sentinel);
  }
  requestAnimationFrame(() => buildTurnNav(conv));
  _forceScrollToBottom(null, true);
  updateSendButton();
  if (_lastIsStreamingBubble) {
    const buf = streamBufs.get(convId);
    /* ★ FIX: buf?.toolRounds is [] (truthy) even when empty, preventing
     *   fallback to getToolRoundsFromMsg(lastMsg).  Use .length check. */
    const rounds = (buf?.toolRounds?.length ? buf.toolRounds : null)
                   || getToolRoundsFromMsg(lastMsg);
    updateStreamingUI({
      thinking: buf?.thinking || lastMsg.thinking || "",
      content: buf?.content || lastMsg.content || "",
      toolRounds: rounds,
      phase: buf?.phase || null,
      _memoryPrefetch: buf?._memoryPrefetch || lastMsg._memoryPrefetch,
      _mcpLoginHint: buf?._mcpLoginHint,
    });
    /* ★ FIX: After page refresh, SSE data may arrive AFTER this initial render.
     *   Schedule a deferred re-render (300ms) so that any SSE state event that
     *   arrives during the connection setup window gets rendered — without this,
     *   the user sees "Waiting…" until the NEXT SSE event triggers twUpdate. */
    const _deferConvId = convId;
    setTimeout(() => {
      if (activeConvId !== _deferConvId) return;           // user switched away
      if (!activeStreams.has(_deferConvId)) return;         // stream finished
      const dBuf = streamBufs.get(_deferConvId);
      if (!dBuf) return;
      updateStreamingUI({
        thinking: dBuf.thinking,
        content: dBuf.content,
        toolRounds: dBuf.toolRounds,
        phase: dBuf.phase,
        _memoryPrefetch: dBuf._memoryPrefetch,
        _mcpLoginHint: dBuf._mcpLoginHint,
      });
    }, 300);
  }
}

function finishStream(convId) {
  activeStreams.delete(convId);
  const conv = conversations.find((c) => c.id === convId);
  if (conv) {
    const lastMsg = conv.messages[conv.messages.length - 1];
    const contentLen = lastMsg?.content?.length || 0;
    const thinkingLen = lastMsg?.thinking?.length || 0;
    const hasError = !!lastMsg?.error;
    /* ★ CROSS-TALK DETECTION: count how many active streams exist at finish time.
     *   If >1 stream was active, there's elevated risk of cross-talk injection.
     *   Also check if the conv's message count changed unexpectedly. */
    const _fsActiveCount = activeStreams.size;  // checked AFTER delete above
    const _fsOtherStreams = [...activeStreams.keys()].filter(k => k !== convId).map(k => k.slice(0,8));
    console.warn(
      `[finishStream] conv=${convId.slice(0,8)} msgs=${conv.messages.length} ` +
      `lastRole=${lastMsg?.role} contentLen=${contentLen} thinkingLen=${thinkingLen} ` +
      `hasError=${hasError} taskId=${conv.activeTaskId?.slice(0,8)||'null'} ` +
      `otherActiveStreams=[${_fsOtherStreams.join(',')}] ` +
      `isActiveConv=${activeConvId === convId} activeConvId=${activeConvId?.slice(0,8)||'null'}`
    );
    if (_fsOtherStreams.length > 0) {
      console.warn(
        `[finishStream] ⚠️ CONCURRENT STREAMS: ${_fsOtherStreams.length} other stream(s) still active ` +
        `while finishing conv=${convId.slice(0,8)} — elevated cross-talk risk! ` +
        `Other convs: [${_fsOtherStreams.join(', ')}]`
      );
    }
    if (lastMsg?.role === 'assistant' && contentLen === 0 && thinkingLen === 0 && !hasError) {
      console.error(`[finishStream] ⚠️ EMPTY ASSISTANT MESSAGE DETECTED — conv=${convId.slice(0,8)} — this is likely the data loss bug!`, {
        message: JSON.parse(JSON.stringify(lastMsg)),
        convTitle: conv.title,
        messageCount: conv.messages.length,
      });
    }
    /* ★ FIX: Clean up any lingering awaiting_human / submitted rounds.
     *   When the task finishes (normally or via abort/timeout), any HG round
     *   that was never answered is now orphaned — the backend won't accept a
     *   response anymore.  Mark them as "done" so the sidebar amber dot clears
     *   and the card collapses to a "no response" line. */
    let _hgCleaned = 0;
    for (const m of conv.messages) {
      if (m.toolRounds) {
        for (const r of m.toolRounds) {
          if (r.status === 'awaiting_human' || r.status === 'submitted') {
            r.status = 'done';
            r.guidanceId = null;
            r._hgSkipped = true;  // marker: user never answered
            _hgCleaned++;
          }
        }
      }
    }
    if (_hgCleaned > 0) {
      console.info(`[finishStream] 🧹 Cleaned ${_hgCleaned} orphaned HG round(s) — conv=${convId.slice(0,8)}`);
    }
    /* ★ FIX: clear a lingering "filtering memories" state.  If the task was
     *   stopped (or died) while the cheap-LLM memory prefetch was still
     *   running, no terminal memory_prefetch event ever arrives to reset
     *   conv._memoryPrefetching — the sidebar dot/tag would stay stuck.
     *   finishStream is the universal terminal point, so reset it here. */
    if (conv._memoryPrefetching) {
      conv._memoryPrefetching = false;
      console.info(`[finishStream] 🧹 Cleared stuck memory-prefetch state — conv=${convId.slice(0,8)}`);
    }
    conv.activeTaskId = null;
    conv._activeTaskClearedAt = Date.now();
    saveConversations(convId);
    /* ★ Queue-race guard: when there are queued messages, skip the full-conv
     *   PUT to the server.  The backend's dispatch_next_queued() will append
     *   the queued user_msg + run the new task; if our PUT lands AFTER it reads
     *   (but BEFORE it writes), we'd overwrite the queued user message — the
     *   "invisible" queued-message bug.  Backend's _sync_result_to_conversation
     *   already persists the aborted assistant state for us in this path.
     *
     *   Same hazard for the autopilot follow-up path: the backend already
     *   appended the synthetic VU user message to conv DB before sending
     *   the done event.  Our local conv.messages doesn't have it yet (it
     *   gets pushed by _attachAutopilotFollowup right after this), so a
     *   full-conv PUT here would overwrite the VU message in the DB. */
    const _fsHasQueued = pendingMessageQueue.has(convId)
      && pendingMessageQueue.get(convId).length > 0;
    /* Locate the autopilot carrier by flag, not by tail position — Phase-2
     * reconciliation can move it off conv.messages[length-1]. */
    const _fsApCarrier = _findAutopilotPendingCarrier(conv);
    const _fsAutopilotInbound = !!_fsApCarrier;
    const _fsLastMsg = conv.messages[conv.messages.length - 1];
    const _fsIsServerOffline = _fsLastMsg && _fsLastMsg.finishReason === 'server_offline';
    if (_fsIsServerOffline) {
      console.info(`[finishStream] 📡 Skipping syncConversationToServer — ` +
        `finishReason=server_offline for conv=${convId.slice(0,8)}; ` +
        `backend has the complete content, frontend only has a truncated snapshot`);
    } else if (_fsHasQueued) {
      console.info(`[finishStream] 🚧 Skipping syncConversationToServer — ` +
        `queue has ${pendingMessageQueue.get(convId).length} item(s) for conv=${convId.slice(0,8)}; ` +
        `backend owns the next DB write via dispatch_next_queued()`);
    } else if (_fsAutopilotInbound) {
      console.info(`[finishStream] 🤖 Skipping syncConversationToServer — ` +
        `autopilot follow-up inbound for conv=${convId.slice(0,8)}; ` +
        `backend already wrote the VU user message to DB`);
    } else {
      syncConversationToServer(conv);
    }
    /* ★ Eagerly update IndexedDB cache — syncConversationToServer also does this
     *   on success, but it may be guarded/skipped in some edge cases.  This ensures
     *   the cache always has the latest post-stream content for instant reload. */
    ConvCache.put(conv);
    /* ★ Auto-generate a descriptive title once the first turn completes.
     *   The helper guards itself (skips if user-edited, already attempted, or
     *   the conversation lacks a user+assistant pair), so this is a safe
     *   fire-and-forget call on every stream finish. */
    if (typeof _maybeAutoGenerateTitle === 'function' && !hasError) {
      _maybeAutoGenerateTitle(convId);
    }
  } else {
    console.error(`[finishStream] conv not found for id=${convId.slice(0,8)} — cannot save!`);
  }
  // ── UI updates (wrapped in try/catch so auto-translate always runs) ──
  try {
    if (activeConvId === convId) {
      const sm = document.getElementById("streaming-msg");
      const hasEndpointTurns = conv && conv.messages.some(m => m._epIteration);
      // ★ SyncFix: if the aborted stream's trailing assistant was already
      //   truncated away (user clicked Edit/Regen mid-abort), don't re-render
      //   a ghost msg-N for a message that no longer exists. The last message
      //   after truncation should be a user message; if so, just remove the
      //   stale streaming-msg without replacing it.
      const _fsLast = conv ? conv.messages[conv.messages.length - 1] : null;
      /* ★ Autopilot detection: when autopilot fires, _handleAutopilotVuEvent
       *   has pushed a VU user message at conv.messages[length-1] BEFORE
       *   finishStream runs.  The real streaming assistant lives at
       *   length-2 (or earlier).  Without this branch, the trailing-VU
       *   path would either remove #streaming-msg without finalizing the
       *   parent assistant (visual data loss) or pass the VU into
       *   ConvView.finalizeStreaming and stamp VU's HTML onto the
       *   streaming bubble's slot — both manifest as "VU user message
       *   invisible until force-refresh".  Walk back to the nearest
       *   non-VU assistant and finalize that one instead. */
      let _fsAutopilotAssistant = null;
      if (sm && conv && _fsLast && _fsLast.role !== 'assistant') {
        for (let i = conv.messages.length - 1; i >= 0; i--) {
          const m = conv.messages[i];
          if (m && m.role === 'assistant' && !m._isVirtualUser) {
            _fsAutopilotAssistant = m;
            break;
          }
        }
      }
      const _truncatedAway = sm && _fsLast && _fsLast.role !== 'assistant'
        && !_fsAutopilotAssistant;
      if (_truncatedAway) {
        console.info(`[SyncFix] finishStream skipping render — trailing assistant was truncated (conv=${convId.slice(0,8)}, lastRole=${_fsLast.role})`);
        try { sm.remove(); } catch (e) { /* already detached */ }
      } else if (sm && _fsAutopilotAssistant) {
        /* Autopilot path — finalize the parent assistant, NOT the VU
         * user message that was pushed at the tail. */
        console.info(`[finishStream] 🤖 Autopilot tail detected — finalizing parent assistant ` +
          `(idx=${conv.messages.indexOf(_fsAutopilotAssistant)}, lastRole=${_fsLast.role}, ` +
          `lastIsVU=${!!_fsLast._isVirtualUser}) for conv=${convId.slice(0,8)}`);
        window.ConvView.finalizeStreaming(convId, _fsAutopilotAssistant);
      } else if (sm && conv) {
        /* Normal streaming finish — funnel through ConvView so scroll
         * preservation (the "thinking-block collapse" jump fix) and the
         * truncation-aware fallback live in one place.  See conv_view.js
         * `finalizeStreaming` for the full rationale. */
        const idx = conv.messages.length - 1;
        const msg = conv.messages[idx];
        if (msg) window.ConvView.finalizeStreaming(convId, msg);
      } else if (hasEndpointTurns) {
        // ★ Endpoint mode after poll fallback — no streaming-msg element exists
        // (SSE timed out, poll was used). Do a full re-render to show all turns.
        console.info(`[finishStream] Endpoint mode full re-render — ` +
          `conv=${convId.slice(0,8)} msgs=${conv.messages.length}`);
        renderChat(conv);
      }
      /* ★ FIX: Don't force-scroll-to-bottom after stream finishes.
       *   The user is already reading the content at their current scroll position.
       *   Forcing to bottom after the streaming→final DOM swap causes a visible jump
       *   because the final message may be shorter (collapsed thinking, no phase indicator).
       *   Only scroll if the user was already near the bottom (within 80px). */
      if (isNearBottom(80)) scrollToBottom();
      if (conv) {
        buildTurnNav(conv);
        _lastRenderedFingerprint = _convRenderFingerprint(conv);
      }
    }
    renderConversationList();
    updateSendButton();
  } catch (uiErr) {
    console.error('[finishStream] UI update error (non-fatal, translate will still run):', uiErr.message);
  }
  // ── Auto-translate assistant response ──
  // ★ Use per-conversation setting, NOT the global (which reflects current viewed conv)
  const _convAutoTranslate = conv ? (conv.autoTranslate !== undefined ? !!conv.autoTranslate : true) : autoTranslate;
  if (!_convAutoTranslate) {
    console.info(`[finishStream] autoTranslate is OFF — skipping all ` +
      `translation scheduling for conv=${convId.slice(0,8)} ` +
      `(conv.autoTranslate=${conv?.autoTranslate}, global=${autoTranslate})`);
  }
  if (_convAutoTranslate && conv) {
    /* ★ Auto-translate is now driven by the server-side safety net in
     *   lib/tasks_pkg/manager.py::_maybe_auto_translate_assistant (and the
     *   endpoint-mode equivalent _trigger_endpoint_auto_translate).  The
     *   backend fires translation right after persisting assistant content
     *   to the DB, so kicking off a parallel client-driven task here just
     *   races the server task — and since /api/translate/start has no
     *   conv+msgIdx-level dedup, both writes can land and the slower one
     *   silently clobbers the faster (real) translation.  Skip in normal
     *   stream-completion; manual click (translateMessage) and page-load
     *   resume (_resumePendingTranslations) still go through their own
     *   call sites and are unaffected. */
    console.info(`[finishStream] Auto-translate handled by backend safety net — ` +
      `skipping client-side scheduling for conv=${convId.slice(0,8)}`);
  }
  // ── ★ Autopilot in-band follow-up: when the done event carried
  //    autopilotNextTaskId + autopilotVuMessage, the backend already
  //    appended the synthetic user message to conv DB and spawned the
  //    next task before closing this SSE stream.  Attach to it directly
  //    instead of going through the queue-poll path.  This eliminates
  //    the race where the VU LLM call took longer than the polling
  //    retry budget (~15s) and the synthetic user msg + follow-up task
  //    stayed invisible until manual page refresh.
  /* Locate the autopilot carrier by flag, not by tail position — the
   * carrier is whichever message the SSE done handler stamped, which
   * may have been moved off the tail by Phase-2 reconciliation. */
  const _apCarrier = conv ? _findAutopilotPendingCarrier(conv) : null;
  if (_apCarrier && typeof _attachAutopilotFollowup === 'function') {
    const _autopilotPending = _apCarrier.msg._autopilotPending;
    delete _apCarrier.msg._autopilotPending;
    _attachAutopilotFollowup(convId, _autopilotPending);
    return;
  }

  // ── ★ Server-side queue: always check for auto-dispatched next task ──
  // The backend's persist_task_result → _dispatch_queued_message checks the
  // message_queue table and auto-dispatches the next message. We poll for
  // the new task to connect to its SSE stream. No frontend gate — the
  // backend is the single source of truth for queue state.
  //
  // ★ Optimistic UI (when queue has items): insert a placeholder streaming
  //   bubble immediately so the user has visual feedback that their queued
  //   message is about to be dispatched, instead of dead air between stop and
  //   the new SSE connection.  _checkForQueuedTask → loadConversationMessages
  //   → renderChat will replace this bubble with the real streaming one.
  // ★ Timing: skip the 500ms delay when there's a queued item — we want the
  //   dispatch poll to fire ASAP so the user sees the new task start without
  //   lag.  When there's no queue, keep the 500ms debounce to avoid hammering
  //   /api/chat/active on every normal stream end.
  const _hasQueued = pendingMessageQueue.has(convId)
    && pendingMessageQueue.get(convId).length > 0;
  if (_hasQueued && activeConvId === convId) {
    try {
      const inner = document.getElementById('chatInner');
      if (inner && !document.getElementById('streaming-msg')) {
        const _qTime = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        inner.insertAdjacentHTML('beforeend',
          _streamingBubbleHTML('worker', 'Dispatching queued message…', _qTime));
        if (isNearBottom(80)) scrollToBottom();
      }
    } catch (e) {
      console.warn('[finishStream] queued-dispatch placeholder insert failed:', e);
    }
  }
  const _queuedCheckDelay = _hasQueued ? 0 : 500;
  setTimeout(() => _checkForQueuedTask(convId), _queuedCheckDelay);
}

/**
 * Re-trigger EN→CN translation for any awaiting_human rounds that haven't
 * been translated yet.  Called after SSE state snapshot / page-load reconnection
 * so translations survive page refreshes.
 */
function _retriggerHgTranslations(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;
  const _hgAutoTrans = conv.autoTranslate !== undefined ? !!conv.autoTranslate : !!autoTranslate;
  if (!_hgAutoTrans) return;
  const assistantMsg = [...conv.messages].reverse().find(m => m.role === 'assistant');
  if (!assistantMsg || !assistantMsg.toolRounds) return;
  for (const r of assistantMsg.toolRounds) {
    if (r.status === 'awaiting_human' && r.guidanceQuestion && !r._translatedQuestion && !r._hgTranslating) {
      console.log(`[HG-Translate] Re-triggering translation for guidance=${r.guidanceId} after reconnect`);
      _autoTranslateHumanGuidance(convId, r.roundNum, r.guidanceQuestion, r.guidanceType || 'free_text', r.guidanceOptions || []);
    }
  }
}

/**
 * Auto-translate Human Guidance question & options (EN→CN).
 * Called when a `human_guidance_request` SSE event arrives and conv.autoTranslate is ON.
 * Translates asynchronously; re-renders the HG card when translation completes.
 */
async function _autoTranslateHumanGuidance(convId, roundNum, question, responseType, options) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;
  const assistantMsg = [...conv.messages].reverse().find(m => m.role === 'assistant');
  if (!assistantMsg || !assistantMsg.toolRounds) return;
  const round = assistantMsg.toolRounds.find(r => r.roundNum === roundNum);
  if (!round || round.status !== 'awaiting_human') return;

  // ★ Helper: sync assistantMsg.toolRounds → buf.toolRounds so that the
  //   reactive rendering pipeline (twUpdate → updateStreamingUI → _syncToolRoundsDOM)
  //   sees translation-related flags (_hgTranslating, _translatedQuestion, etc.).
  //   Without this, buf.toolRounds is a stale shallow copy from the
  //   human_guidance_request handler and never gets updated.
  function _syncHgToBuf() {
    const buf = streamBufs.get(convId);
    if (buf && assistantMsg.toolRounds) {
      buf.toolRounds = assistantMsg.toolRounds;
    }
  }

  // Mark as translating (shows spinner in the card)
  round._hgTranslating = true;
  _syncHgToBuf();
  twUpdate(convId);

  // ── Build a single translation batch: question + all option labels + descriptions ──
  // Concatenate all texts with a separator to make a single API call (cheaper & faster)
  const SEP = '\n‖‖‖\n'; // unique separator unlikely to appear in content
  const parts = [question];
  /* ★ Defensive: ensure `options` is an array before iterating. Some
   *   upstream callers (e.g. legacy persisted rounds) can pass null,
   *   a JSON string, or an object. */
  let _optsArr = options;
  if (typeof _optsArr === 'string') {
    try { _optsArr = JSON.parse(_optsArr); }
    catch (_e) { _optsArr = []; }
  }
  if (!Array.isArray(_optsArr)) _optsArr = [];
  if (responseType === 'choice' && _optsArr.length > 0) {
    for (const opt of _optsArr) {
      parts.push((opt && opt.label) || '');
      parts.push((opt && opt.description) || '');
    }
  }
  const batchText = parts.join(SEP);

  try {
    console.log(`[HG-Translate] Starting EN→CN translation for guidance=${round.guidanceId}, parts=${parts.length}`);
    const translated = await _callTranslateAPI(batchText, 'Chinese', 'English');
    // Split back by separator
    const translatedParts = translated.split(/\n?‖‖‖\n?/);

    // Re-find the round (may have changed during async)
    const conv2 = conversations.find(c => c.id === convId);
    if (!conv2) return;
    const msg2 = [...conv2.messages].reverse().find(m => m.role === 'assistant');
    if (!msg2 || !msg2.toolRounds) return;
    const round2 = msg2.toolRounds.find(r => r.roundNum === roundNum);
    if (!round2 || round2.status !== 'awaiting_human') return;

    // Apply translated question
    round2._translatedQuestion = translatedParts[0] || question;
    round2._hgTranslating = false;

    // Apply translated option labels & descriptions
    // ★ Defensive: round2.guidanceOptions may not be an array (see above).
    if (responseType === 'choice' && Array.isArray(round2.guidanceOptions)
        && translatedParts.length > 1) {
      for (let i = 0; i < round2.guidanceOptions.length; i++) {
        const labelIdx = 1 + i * 2;
        const descIdx = 2 + i * 2;
        if (translatedParts[labelIdx]) {
          round2.guidanceOptions[i]._translatedLabel = translatedParts[labelIdx];
        }
        if (translatedParts[descIdx] && round2.guidanceOptions[i].description) {
          round2.guidanceOptions[i]._translatedDescription = translatedParts[descIdx];
        }
      }
    }

    console.log(`[HG-Translate] ✓ Translation done for guidance=${round2.guidanceId}, ` +
      `question: ${question.length}→${round2._translatedQuestion.length} chars`);
    // ★ Sync translated properties to buf before re-render
    const buf2 = streamBufs.get(convId);
    if (buf2 && msg2.toolRounds) {
      buf2.toolRounds = msg2.toolRounds;
    }
    twUpdate(convId);
  } catch (e) {
    console.warn(`[HG-Translate] Translation failed: ${e.message} — showing original`);
    // Clear translating flag, show original untranslated
    const conv2 = conversations.find(c => c.id === convId);
    if (conv2) {
      const msg2 = [...conv2.messages].reverse().find(m => m.role === 'assistant');
      const round2 = msg2?.toolRounds?.find(r => r.roundNum === roundNum);
      if (round2) {
        round2._hgTranslating = false;
        // ★ Sync cleared flag to buf before re-render
        const buf2 = streamBufs.get(convId);
        if (buf2 && msg2.toolRounds) {
          buf2.toolRounds = msg2.toolRounds;
        }
        twUpdate(convId);
      }
    }
  }
}

/**
 * Start auto-translate for an assistant message. Thin wrapper around the
 * unified _runTranslationPipeline (defined in translation.js).
 */
async function _startAutoTranslateForMsg(conv, convId, idx, msg) {
  return _runTranslationPipeline(conv, idx, msg, {
    sourceLang: 'English',
    targetLang: 'Chinese',
    field: 'translatedContent',
    mode: 'auto',
  });
}

/* ── 1 Hz wall-clock ticker for swarm timers ──
 * The fingerprint gate in _syncToolRoundsDOM (correctly) skips re-renders
 * when nothing changes — but elapsed-time strings DO change every second
 * even when no SSE event landed. Rather than churn the gate with a
 * fake per-second fingerprint, we update [data-sw-start] elements in
 * place: zero re-render, single timer, ~O(N agents) per tick. */
function _tickSwarmTimers() {
  const els = document.querySelectorAll('.sw-panel [data-sw-start]');
  if (!els.length) return;
  const now = Date.now();
  for (const el of els) {
    const start = +el.getAttribute('data-sw-start');
    if (!start) continue;
    const sec = Math.max(0, Math.floor((now - start) / 1000));
    const txt = sec >= 60 ? `${Math.floor(sec / 60)}m${sec % 60}s` : `${sec}s`;
    if (el.textContent !== txt) el.textContent = txt;
  }
}
if (typeof window !== 'undefined' && !window._swTimerTicker) {
  window._swTimerTicker = setInterval(_tickSwarmTimers, 1000);
}


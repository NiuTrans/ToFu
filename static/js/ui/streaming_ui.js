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
    const pa = msg._preferencesApplied;
    const pl = msg._preferencesLearned;
    const plHtml = (pl && pl.length) ? renderPreferenceLearnedHtml(pl) : '';
    /* awaiting-approval login = prominent callout; memory-prefetch +
     * preferences + any RESOLVED login = one quiet collapsible strip. */
    const provHtml = renderTurnProvenanceHtml(msg);
    const html = (lhHtml || '') + provHtml + (plHtml || '');
    /* fp includes snippet length so a late-arriving tool_result with a
     * longer snippet triggers a re-render (earlier fp only checked
     * phase+updatedAt, which can stay the same when we just append). */
    const fp = (lh ? `L|${lh.phase}|${lh.username||''}|${lh.updatedAt||0}|${(lh.snippet||'').length}|` : '') +
               (mp ? `${mp.phase}|${mp.selected||0}|${mp.candidates||0}|${mp.totalMs||0}` : '') +
               (msg._preferencesApplied ? `P|${msg._preferencesApplied.chars||0}|${(msg._preferencesApplied.items||[]).length}` : '') +
               (msg._relatedConversations ? `RC|${msg._relatedConversations.count||0}|${(msg._relatedConversations.items||[]).length}` : '') +
               (pl ? `PL|${pl.length}` : '');
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
      thinkZone.innerHTML = `<div class="thinking-block ${still ? "expanded" : ""}" onclick="this.classList.toggle('expanded')"><div class="thinking-header"><span class="thinking-label">${escapeHtml(still ? t('stream.thinking.active') : t('stream.thinking.done'))}</span><span class="thinking-toggle">▼</span></div><div class="thinking-content"><div class="thinking-text"></div></div></div>`;
      block = thinkZone.querySelector(".thinking-block");
    }
    const textEl = block.querySelector(".thinking-text");
    if (textEl && textEl.textContent !== msg.thinking) textEl.textContent = msg.thinking;
    const labelEl = block.querySelector(".thinking-label");
    const _thinkLbl = msg.content ? t('stream.thinking.done') : t('stream.thinking.active');
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
    _phaseHtml = `<div class="stream-phase stream-phase-thinking"><span class="stream-phase-text">${escapeHtml(t('stream.phase.reasoning'))}<span class="stream-phase-counter" data-counter="thinking">${escapeHtml(t('stream.phase.chars', { n: _thSize }))}</span></span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "llm_thinking") {
    const icon = _phaseIcons[phase.phase];
    _phaseKey = "think:" + phase.detail + (phase.toolContext || "");
    const ctx = phase.toolContext
      ? `<span class="stream-phase-ctx">${escapeHtml(phase.toolContext)}</span>`
      : "";
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-icon">${icon}</span><span class="stream-phase-text">${escapeHtml(phase.detail)}${ctx}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "waiting_model" && !hasActiveSearch) {
    /* ★ Request is in flight but the model hasn't emitted its first token
     *   yet (prompt prefill / TTFT), or the next turn is a silent tool call
     *   with no preamble. Emitted by stream_llm_response right before
     *   dispatch_stream; cleared by the first content/thinking delta, or
     *   yielded to the tool UI once a tool_start makes hasActiveSearch true. */
    _phaseKey = "waiting-model:" + (phase.detail || "");
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(phase.detail || t('stream.phase.waitingModel'))}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "compacting") {
    _phaseKey = "compact:" + phase.detail;
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(phase.detail)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "retrying") {
    _phaseKey = "retry:" + (phase.attempt || 0);
    _phaseHtml = `<div class="stream-phase stream-phase-retrying"><span class="stream-phase-icon">⟳</span><span class="stream-phase-text">${escapeHtml(phase.detail || t('stream.phase.retrying'))}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "tool_exec" && !hasActiveSearch) {
    _phaseKey = "exec:" + phase.detail;
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(phase.detail)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "working" && phase.detail) {
    /* Generic "working" phase from external backends (e.g. "Initializing Claude Code...") */
    _phaseKey = "working:" + phase.detail;
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(phase.detail)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (hasActiveSearch) {
    _phaseKey = "search";
    _phaseHtml = "";
  } else if (!msg.content && !msg.thinking) {
    _phaseKey = "wait";
    _phaseHtml =
      `<div class="stream-status"><div class="pulse"></div> ${escapeHtml(t('stream.phase.waiting'))}</div>`;
  } else if (!msg.content && msg.thinking) {
    _phaseKey = "think-only";
    const _thLen = msg.thinking.length;
    const _thSize = _thLen >= 1024 ? `${(_thLen / 1024).toFixed(1)}k` : `${_thLen}`;
    _phaseHtml = `<div class="stream-phase stream-phase-thinking"><span class="stream-phase-text">${escapeHtml(t('stream.phase.deepThinking'))}<span class="stream-phase-counter">${escapeHtml(t('stream.phase.chars', { n: _thSize }))}</span></span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
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
      _ctrEl.textContent = t('stream.phase.chars', { n: _ts });
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
    /* llmRound drives parallel-batch grouping — a sibling arriving in the
     * same turn changes a group's size/header, so it must move the fp. */
    _fp = _fp * 31 + ((r.llmRound | 0) + 1);
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
      ? t("toolPanel.working", { n: count })
      : t("toolPanel.toolsUsed", { n: count, s: count !== 1 ? "s" : "" });
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
              trunc.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span>${escapeHtml(t("toolPanel.hidden", { n: hiddenN }))}</span>`;
              trunc.onclick = () => { trunc.remove(); body._showAll = true; container._roundsFingerprint = null; _syncToolRoundsDOM(container, rounds); };
              body.prepend(trunc);
            } else if (body.querySelector('.ptool-truncated')) {
              const truncEl = body.querySelector('.ptool-truncated');
              const hiddenN2 = done.length - _TOOL_VISIBLE_WINDOW;
              truncEl.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg><span>${escapeHtml(t("toolPanel.hidden", { n: hiddenN2 }))}</span>`;
            }
            return body._showAll ? toolRounds : [...tail, ...active];
          })()
        : toolRounds;
      for (const round of visibleRounds) {
        const rn = round.roundNum;
        const isActive = round.status === "searching";
        const _isSwarm = _isRoundSwarm(round);
        /* ── Parallel-batch grouping: place this round's slot inside the
         *   `.ptool-turn` container for its LLM turn (same llmRound = issued
         *   in parallel). Rounds without llmRound are their own solo group.
         *   The header (parallel chip) is synced in a post-pass below. */
        const _gkey = (round.llmRound != null) ? ("L" + round.llmRound) : ("S" + rn);
        const _gsel = (typeof CSS !== "undefined" && CSS.escape) ? CSS.escape(_gkey) : _gkey;
        let groupEl = body.querySelector(`.ptool-turn[data-llm-round="${_gsel}"]`);
        if (!groupEl) {
          groupEl = document.createElement("div");
          groupEl.className = "ptool-turn";
          groupEl.setAttribute("data-llm-round", _gkey);
          groupEl.setAttribute("data-batch-size", "1");
          // Round number = llmRound + 1 (matches the cost popover's 第N轮).
          // Solo legacy rounds (S-key, no llmRound) carry no round number.
          if (round.llmRound != null) groupEl.setAttribute("data-round-no", String(round.llmRound + 1));
          body.appendChild(groupEl);
        }
        let slot = body.querySelector(`[data-prn="${rn}"]`);
        /* ★ Determine if this round needs an interactive card (HG, stdin, approval).
         *   Interactive cards are tall (200-300px) and must NOT be collapsed by
         *   content-visibility:auto (which assumes 32px intrinsic size for off-screen
         *   slots).  Force content-visibility:visible on these slots.
         *   Swarm panels are also tall and re-render frequently — same treatment. */
        const _isInteractive = round.status === "awaiting_human" || round.status === "awaiting_stdin"
          || round.status === "pending_approval";
        const _renderRow = (r, active) => _isRoundSwarm(r)
          ? _buildSwarmPanelHTML(r, toolRounds)
          : _renderUnifiedToolLine(r, active);
        /* ★ Data-driven completion: the slot remembers the status it was
         *   last rendered at (`data-rendered-status`). The "settle this
         *   slot to done" decision below keys off a status MISMATCH, not
         *   off sniffing leftover spinner CSS classes (`.ptool-active`,
         *   `.ptool-cmd-running`, …). The old class-sniff was a latent
         *   "spinner stuck until the next tool" bug: any active renderer
         *   whose markup happened not to emit one of those exact classes
         *   would never re-render to done on its own tool_result — it
         *   waited for an unrelated later event to force a full rebuild.
         *   Keying off the round's own status makes completion fire the
         *   instant tool_result flips status, for EVERY tool renderer. */
        const _stamp = (active) => { slot.setAttribute("data-rendered-status", active ? "searching" : (round.status || "")); };
        if (!slot) {
          slot = document.createElement("div");
          slot.setAttribute("data-prn", rn);
          if (_isSwarm) slot.setAttribute("data-prn-kind", "swarm");
          if (round._hgTranslating) slot.setAttribute("data-hg-translating", "1");
          if (_isInteractive || _isSwarm) slot.style.contentVisibility = "visible";
          slot.innerHTML = _renderRow(round, isActive);
          _stamp(isActive);
          groupEl.appendChild(slot);
        } else if (slot.parentElement !== groupEl) {
          /* Slot exists but landed in the wrong group (e.g. its llmRound
           * was learned after creation) — relocate it. */
          groupEl.appendChild(slot);
          if (_isSwarm) {
            slot.style.contentVisibility = "visible";
            slot.innerHTML = _buildSwarmPanelHTML(round, toolRounds);
          }
        } else if (_isSwarm) {
          /* Swarm rounds change frequently (per-agent phase / preview / tool-call
           * timeline) — always rebuild from the latest snapshot. The fingerprint
           * gate above already guaranteed something actually changed. */
          slot.style.contentVisibility = "visible";
          slot.innerHTML = _buildSwarmPanelHTML(round, toolRounds);
        } else if (isActive || round.status === "pending_approval") {
          if (_isInteractive) slot.style.contentVisibility = "visible";
          slot.innerHTML = _renderUnifiedToolLine(round, isActive);
          _stamp(isActive);
        } else if (slot.getAttribute("data-rendered-status") !== (round.status || "")) {
          /* ★ Status changed since last render — the data-driven completion
           *   trigger. Common case: searching → done when THIS round's own
           *   tool_result lands (the user's "don't wait for the next tool"
           *   requirement); also rejected, or a transition INTO an
           *   interactive state (searching → awaiting_human/stdin). Re-render
           *   to the new markup with NO dependence on which spinner class the
           *   previous active render happened to emit. This fires exactly
           *   once per transition (the stamp is updated below), so while a
           *   round STAYS in an interactive state the dedicated
           *   live-DOM-preserving branches below own it (stamp == status →
           *   mismatch is false → fall through). */
          if (_isInteractive) slot.style.contentVisibility = "visible";
          slot.innerHTML = _renderUnifiedToolLine(round, false);
          _stamp(false);
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

      /* ── Sync each parallel-batch group's header. A group with ≥2 tool
       *   slots shows the collapsible "N parallel calls" header; a solo
       *   group shows none. Headers are added/updated/removed in place so
       *   a sibling streaming in mid-turn upgrades a solo row into a group
       *   without rebuilding either slot. */
      const _groups = body.querySelectorAll(".ptool-turn");
      for (const g of _groups) {
        const size = g.querySelectorAll(":scope > [data-prn]").length;
        g.setAttribute("data-batch-size", String(size));
        const _rnoAttr = g.getAttribute("data-round-no");
        const _rno = _rnoAttr ? parseInt(_rnoAttr, 10) : null;
        let head = g.querySelector(":scope > .ptool-turn-head");
        let solo = g.querySelector(":scope > .ptool-turn-rno-solo");
        if (size >= 2) {
          // Multi-call turn → full header (carries the round tag inline);
          // drop any solo round-tag left over from when it was a 1-call group.
          if (solo) { solo.remove(); solo = null; }
          if (!head) {
            g.insertAdjacentHTML("afterbegin", _renderTurnHead(size, _rno));
            if (g.classList.contains("collapsed")) {
              const _chev = g.querySelector(":scope > .ptool-turn-head .ptool-turn-chev");
              if (_chev) _chev.textContent = "▸";
            }
          } else {
            const lbl = head.querySelector(".ptool-turn-label");
            if (lbl) lbl.textContent = _turnLabelText(size);
          }
        } else {
          // Solo turn → no header; show a thin round-tag line instead when
          // we know the round number.
          if (head) head.remove();
          if (_rno != null && !solo) {
            g.insertAdjacentHTML("afterbegin", _renderSoloRoundTag(_rno));
          }
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

/* ── Swarm "Parallel Execution" panel rendering + the stuck-panel reconciler
 *    were extracted to ui/streaming_swarm_panel.js (2026-06-27). Those
 *    builders (_buildSwarmPanelHTML, _buildSwarmInboxChipsHTML, …) are still
 *    called from _syncToolRoundsDOM / updateStreamingUI above via shared
 *    window scope — no import needed (the bundle loads that file first). */

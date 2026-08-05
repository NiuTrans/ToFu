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
  /* ★ FIX (blank streaming bubble on tablet): this innerHTML assignment WIPES
   *   the "Preparing…/等待中…" pulse that _streamingBubbleHTML seeded into
   *   #streaming-body. updateStreamingUI can then hit an early return before it
   *   repopulates the status zone — the _hasSelectionInStreaming() guard (a
   *   long-press text selection inside the bubble is common on tablets) or a
   *   coalesced background-tab frame whose buffer is still empty. That left the
   *   body as empty zones with NO pulse and NO content: a permanently blank
   *   Agent bubble while the independent elapsed timer kept ticking. Seed the
   *   same waiting pulse directly into the status zone so the wipe can never
   *   produce a fully-blank body; the normal status logic overwrites it (keyed
   *   on data-phase-key) the moment a real frame paints. */
  const _waitPulse =
    '<div class="stream-status"><div class="pulse"></div> ' +
    escapeHtml(typeof t === 'function' ? t('stream.phase.waiting') : 'Waiting…') +
    '</div>';
  body.innerHTML =
    '<div data-zone="memprefetch"></div>' +
    '<div data-zone="swarmInbox"></div>' +  /* async swarm-update chips */
    '<div data-zone="tool"></div>' +
    '<div data-zone="thinking"></div>' +
    /* ★ Auto-translate render UNIFICATION (2026-07-07): when auto-translate is
     * ON, the incremental translator's Chinese-so-far renders HERE — as the
     * PRIMARY body, in its natural interleaved position ABOVE the English —
     * mirroring the settled bilingual view (Chinese primary, English in the
     * collapsed 原文/译文 toggle). This retires the old bottom-pinned
     * translatePreview dump that made the Chinese arrive as "one big block at
     * the very bottom" and caused a layout jump at finalize. The content zone
     * below holds the still-untranslated English TAIL (the current round's
     * prose), which swaps to Chinese in place as each round's segment lands. */
    '<div data-zone="translatedPrimary"></div>' +
    '<div data-zone="content"></div>' +
    '<div data-zone="fc"></div>' +
    '<div data-zone="status">' + _waitPulse + '</div>' +
    /* Legacy fallback slot — retained (hidden by default) only so a stale
     * caller that predates translatedPrimary still has a target and degrades
     * gracefully instead of throwing. The primary live-translation path no
     * longer writes here. */
    '<div data-zone="translatePreview"></div>';
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
let _pendingStreamArmTs = 0;  // arm time of the current pending update (30s dwell cap)
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
      translatedPrimary: body.querySelector('[data-zone="translatedPrimary"]'),
      content: body.querySelector('[data-zone="content"]'),
      fc: body.querySelector('[data-zone="fc"]'),
      status: body.querySelector('[data-zone="status"]'),
    };
  }
  return _streamZoneCache;
}

/* ── Tool-label composition for the phase rows (owner directive 2026-08-03) ──
 * The backend ships STRUCTURED raw tool names on the phase events
 * (`phase.tools` for tool_exec, `phase.toolContextTools` for the round-open
 * llm_thinking suffix) so the client composes the label in the UI language;
 * the pre-joined English `detail` / `toolContext` strings remain the
 * headless / legacy fallback. No emoji anywhere — this row renders its own
 * SVG iconography, an emoji in the label is a second inconsistent source. */
function _toolLabelText(name) {
  if (!name) return '';
  /* MCP namespaced tool: mcp__server__tool → server/tool (mirrors the
   * backend tool_label fallback, minus the retired 🔌 prefix). */
  if (name.slice(0, 5) === 'mcp__') {
    const parts = name.split('__');
    if (parts.length >= 3) return parts[1] + '/' + parts.slice(2).join('__');
  }
  if (typeof t === 'function') {
    /* Inline literal prefix (NEVER an intermediate variable): the boot-key
     * static scan (lib/i18n_boot_keys.py T_CALL_DYNAMIC_PREFIX_RE) only
     * discovers a dynamic call written INLINE like the one below —
     * t(identifier) is invisible and would drop the whole tool.label.*
     * namespace from the boot pack. */
    const v = t('tool.label.' + name);
    /* t() falls back to the key itself for an unmapped tool — then the raw
     * tool name is the honest label (future / custom tools). */
    if (v && v !== 'tool.label.' + name) return v;
  }
  return name;
}
function _toolLabelJoin() {
  return (typeof t === 'function') ? t('tool.label.join') : ', ';
}
/* The tool_exec phase row text: N counted over the CALLS (phase.tools keeps
 * duplicates), labels deduped + localized. Falls back to the legacy English
 * `detail` when no structured list is present (VU forward / old replay).
 * NOTE: the fallback is deliberately SELF-CONTAINED (p.detail) — the
 * i18n-resolving _phaseDetailText is nested inside updateStreamingUI and
 * is NOT reachable from this module-level helper (a blind call here is a
 * ReferenceError on exactly the rare VU-forwarded frame it meant to serve). */
function _toolExecPhaseText(p) {
  const names = (p && Array.isArray(p.tools)) ? p.tools.filter(Boolean) : [];
  if (!names.length || typeof t !== 'function') return (p && p.detail) || '';
  const seen = {};
  const labels = [];
  for (const nm of names) {
    if (seen[nm]) continue;
    seen[nm] = 1;
    labels.push(_toolLabelText(nm));
  }
  if (names.length === 1) return t('stream.phase.toolExec', { tool: labels[0] });
  return t('stream.phase.toolExecMulti', { n: names.length, tools: labels.join(_toolLabelJoin()) });
}
/* The round-open llm_thinking suffix: the PREVIOUS round's tools, composed
 * from toolContextTools when the backend shipped them (localized), else the
 * raw English toolContext string verbatim (legacy fallback). */
function _toolContextPhaseText(p) {
  const names = (p && Array.isArray(p.toolContextTools)) ? p.toolContextTools.filter(Boolean) : [];
  if (!names.length || typeof t !== 'function') return (p && p.toolContext) || '';
  const seen = {};
  const labels = [];
  for (const nm of names) {
    if (seen[nm]) continue;
    seen[nm] = 1;
    labels.push(_toolLabelText(nm));
  }
  return t('stream.phase.toolContext', { tools: labels.join(_toolLabelJoin()) });
}
function updateStreamingUI(msg) {
  const zones = _getStreamZones();
  if (!zones) return;
  const { body, memprefetch: memprefetchZone, tool: toolZone, think: thinkZone, content: contentZone, fc: fcZone } = zones;
  let statusZone = zones.status;
  if (!statusZone) {
    /* ★ Lazy-create the status zone: a bubble template may omit it (the
     *   autopilot warmup bubble seeds no status when status===defaultStatus),
     *   and _ensureStreamZones early-returns once data-zone="tool" exists —
     *   so the phase-paint below would otherwise dereference null and kill
     *   the whole frame update. */
    statusZone = document.createElement('div');
    statusZone.setAttribute('data-zone', 'status');
    statusZone.className = 'stream-status';
    body.appendChild(statusZone);
    /* ★ FIX (status-box stacking, 2026-07-25): WRITE BACK into the zone cache
     *   (zones IS _streamZoneCache). The default-shape bubble from
     *   _streamingBubbleHTML (no status/detail ⇒ no status zone, but a tool
     *   zone — so _ensureStreamZones early-returns) left zones.status null
     *   FOREVER: every rAF frame re-entered this branch and appended ANOTHER
     *   status div painted with that frame's phase — the "等待中… ×4 /
     *   正在生成回复… / 推理中 N 字符 ×9" staircase. Cache the created zone so
     *   later frames reuse it and the phase paint updates it in place. */
    zones.status = statusZone;
  }
  const rounds = msg.toolRounds || [];
  const hasActiveSearch = rounds.some((r) => r.status === "searching");
  /* ★ In-flight tool verdict for the tool_exec phase row (owner report
   *   2026-08-03 — the "✏️ Applying changes…" stale-phase desync). The row
   *   is only truthful while a round is genuinely IN-FLIGHT; the moment
   *   every round settles, the phase is a stale leftover of the dispatch
   *   announcement (it is retired only by the NEXT phase/content event),
   *   and rendering it claims a tool is running when none is — the user
   *   then hunts for a tool card that isn't there. 'searching' alone
   *   under-covers: an approval / human-input / stdin wait is still
   *   in-flight (and is exactly when no OTHER UI announces the tool at the
   *   phase-row position), so those count too. Terminal verdicts mirror
   *   stream_reducer.js _TERMINAL_ROUND_STATUS — kept as a local literal
   *   because the deferred-bundle load order can't guarantee that const
   *   is visible here. */
  const _TOOL_INFLIGHT_STATUS = { searching: 1, executing: 1, submitted: 1,
    pending_approval: 1, awaiting_human: 1, awaiting_stdin: 1 };
  const hasInFlightTool = rounds.some((r) => _TOOL_INFLIGHT_STATUS[r.status] === 1);
  _syncToolRoundsDOM(toolZone, rounds);
  /* ★ Async swarm: render inbox-inject chips above the tool zone so the
   *   user sees "received N async swarm updates" the moment they land. */
  const _swarmInboxZone = zones.swarmInbox;
  if (_swarmInboxZone) {
    const injects = msg._inboxInjects || [];
    const html = (typeof _buildSwarmInboxChipsHTML === 'function')
      ? _buildSwarmInboxChipsHTML(injects) : '';  // module DEFERRED (Epic-E sub-5B)
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
      _extractFileChangesFromRoundsAsync(rounds, msg).then(liveFiles => {
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
  } else if (thinkZone.firstChild) {
    /* ★ FIX (stale thinking pinned below the tool panel across rounds):
     * clear the top-level thinking zone the instant the live buffer's thinking
     * goes empty — SYMMETRIC with the content zone's clear-on-empty below
     * (contentZone.innerHTML = ""). Without this, a round that streamed
     * reasoning before its tool calls leaves its .thinking-block orphaned in
     * this zone: delta_reset zeroes assistantMsg.thinking (the prose is moved
     * onto the round's per-round .seg-thinking inside the tool panel), so every
     * subsequent tool-only round skips the `if (msg.thinking)` write and the
     * old block persists — visually stuck under the tool panel for the rest of
     * the turn. Backend stays authoritative: finalize's committedMessage
     * projection re-renders the bubble regardless. */
    thinkZone.innerHTML = "";
  }
  /* ★ FIX: Skip content DOM update while user has active selection to prevent flicker/deselect */
  if (_hasSelectionInStreaming()) {
    _pendingStreamMsg = msg;
    /* Backstop for the 300ms self-clear below: if the selection NEVER releases
     * (user switches conversation mid-selection — the new conv owns the view
     * and this pending update is moot — or the stream simply ends while text
     * is selected), the interval would otherwise tick empty forever
     * (pt_3cd6cd48 ⑧). 30s per pending update is comfortably longer than any
     * real "hold selection while watching" moment; on expiry we DROP the
     * stale pending update instead of force-rendering over the selection. */
    _pendingStreamArmTs = Date.now();
    if (!_pendingStreamTimer) {
      _pendingStreamTimer = setInterval(() => {
        if (_pendingStreamMsg && Date.now() - (_pendingStreamArmTs || 0) > 30000) {
          _pendingStreamMsg = null;
          clearInterval(_pendingStreamTimer);
          _pendingStreamTimer = null;
          return;
        }
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
  /* ★ Auto-translate render UNIFICATION: when a live Chinese translation is
   * being painted into the translatedPrimary zone above, the English in THIS
   * content zone is demoted to a subtle "live tail" (the still-untranslated
   * current round), styled to read as the continuation of the Chinese rather
   * than a competing full body. `data-xlate` is stamped by
   * _renderStreamingTranslatePreview; `data-xlate-final` (set on the finalize
   * 'started' frame) additionally HIDES the tail so the final round's English
   * doesn't briefly double with its just-arriving Chinese. Both are pure
   * class toggles on the existing zone — the content render logic below is
   * unchanged. */
  {
    const _xl = body.getAttribute('data-xlate') === '1';
    const _xlFinal = body.getAttribute('data-xlate-final') === '1';
    contentZone.classList.toggle('stream-content-demoted', _xl);
    contentZone.classList.toggle('stream-content-tail-hidden', _xlFinal);
  }
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
      let frozenLen = contentZone._frozenLen || 0;
      const REFREEZE_THRESHOLD = 600; // advance freeze point when tail exceeds this

      /* ★ Content-regression guard (stale frozen-prefix leak, 2026-07-09).
       * The live buffer grows monotonically EXCEPT across a reset: delta_reset
       * (new tool round) / retry_reset (turn re-run) zero buf.content. The
       * freeze cache (_frozenLen/_frozenHtml) lives on the reused zone element
       * and is dropped below ONLY on a frame that observes content==="" — but
       * the render is COALESCED (_twFlush paints only the latest buffer per
       * rAF), so that empty frame is routinely skipped when the next content
       * delta beats the next animation frame. A content SHORTER than we last
       * rendered therefore means the buffer was reset and the cached freeze
       * belongs to a PRIOR round's prose; splicing the new content against that
       * stale _frozenLen glues the prior round's frozen prefix in front of the
       * real answer — pristine in the buffer + committed message, garbled only
       * in the live projection (the reported thinking/content "swap"). Drop the
       * stale cache so the terminal round re-renders from a clean slate. */
      if (content.length < prevLen) {
        contentZone._frozenLen = 0;
        contentZone._frozenHtml = null;
        frozenLen = 0;
      }

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
          const tailText = content.slice(freezeIdx);

          if (frozenLen > 0 && mdContentEl && tailEl) {
            /* ★ FLICKER FIX (root cause): the old refreeze rebuilt the ENTIRE
             *   `.md-content` innerHTML every ~600 chars, recreating the
             *   already-committed frozen nodes and re-parsing the whole answer
             *   — a visible full-content flash on long replies. Instead keep the
             *   existing frozen DOM intact: render ONLY the newly-frozen segment
             *   (content between the old and new freeze points; both sit on
             *   `\n\n` block boundaries, so rendering the segment independently
             *   is faithful to rendering the whole prefix) and insert it as new
             *   frozen siblings right before the tail, then reset only the small
             *   tail. Nothing already painted is torn down, so no flash. Freeze
             *   points advance monotonically (lastIndexOf ceiling grows with
             *   content), so freezeIdx >= frozenLen always holds here. */
            if (freezeIdx > frozenLen) {
              const newlyFrozen = content.slice(frozenLen, freezeIdx);
              let _nfHtml;
              try { _nfHtml = renderMarkdown(newlyFrozen); }
              catch (_) { _nfHtml = escapeHtml(newlyFrozen); }
              tailEl.insertAdjacentHTML('beforebegin', _nfHtml);
              contentZone._frozenLen = freezeIdx;
            }
            /* freezeIdx === frozenLen: the overflowing tail has no new `\n\n`
             * boundary (one long paragraph) — nothing new to freeze; just
             * refresh the tail in place. */
            try { tailEl.innerHTML = renderMarkdown(tailText); }
            catch (_) { tailEl.innerHTML = escapeHtml(tailText); }
            /* The whole-prefix cache is no longer maintained on the incremental
             * path (the frozen DOM itself is now the source of truth). */
            contentZone._frozenHtml = null;
          } else {
            /* First substantial render (or frozen DOM missing): build the split
             * structure ONCE. Subsequent frames take the fast path or the
             * incremental branch above, so this full build is not a per-frame
             * cost and cannot flash repeatedly. */
            const frozenText = content.slice(0, freezeIdx);
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
          }
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
  /* ★ i18n resolver: our OWN fixed-chrome phase labels (llm_thinking,
   *   waiting_model, compacting, reactive-compact retrying) now ship a
   *   stable `detailKey` (+ optional `detailArgs`) alongside their English
   *   `detail`. Prefer the localized key when present; fall back to `detail`
   *   for headless callers / third-party phases that don't set one. Guarded
   *   so unit harnesses that eval this file without i18n.js don't throw. */
  function _phaseDetailText(p) {
    if (!p) return "";
    const _key = p.detailKey || "";
    if (_key && typeof t === 'function') {
      try {
        const _args = p.detailArgs ? Object.assign({}, p.detailArgs) : undefined;
        /* ★ Nested typed cause: retry phases ship a stable `reasonKey`
         *   alongside the raw English `reason` token so the HUD localizes the
         *   cause too ("Endpoint unreachable" → zh). An unknown key falls back
         *   to the raw reason (same ruling as an unknown detailKey). */
        if (_args && _args.reasonKey) {
          const _r = t(_args.reasonKey);
          if (_r && _r !== _args.reasonKey) _args.reason = _r;
        }
        return t(_key, _args || undefined);
      }
      catch (e) { console.debug('[stream-phase] t() failed for', _key, e); }
    }
    return p.detail || "";
  }
  if (phase && phase.phase === "thinking_active") {
    /* ★ Model is actively generating thinking tokens (works on ALL rounds,
     *   even when msg.content is already non-empty from previous tool rounds) */
    const _thLen = phase._thinkingLen || 0;
    const _thSize = _thLen >= 1024 ? `${(_thLen / 1024).toFixed(1)}k` : `${_thLen}`;
    _phaseKey = "thinking-active";
    _phaseHtml = `<div class="stream-phase stream-phase-thinking"><span class="stream-phase-text">${escapeHtml(t('stream.phase.reasoning'))}<span class="stream-phase-counter" data-counter="thinking">${escapeHtml(t('stream.phase.chars', { n: _thSize }))}</span></span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "llm_thinking") {
    const icon = _phaseIcons[phase.phase];
    const _txt = _phaseDetailText(phase);
    _phaseKey = "think:" + (phase.detailKey || phase.detail) + (phase.toolContext || "");
    const _ctxTxt = _toolContextPhaseText(phase);
    const ctx = _ctxTxt
      ? `<span class="stream-phase-ctx">${escapeHtml(_ctxTxt)}</span>`
      : "";
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-icon">${icon}</span><span class="stream-phase-text">${escapeHtml(_txt)}${ctx}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "waiting_model" && !hasActiveSearch) {
    /* ★ Request is in flight but the model hasn't emitted its first token
     *   yet (prompt prefill / TTFT), or the next turn is a silent tool call
     *   with no preamble. Emitted by stream_llm_response right before
     *   dispatch_stream; cleared by the first content/thinking delta, or
     *   yielded to the tool UI once a tool_start makes hasActiveSearch true. */
    const _txt = _phaseDetailText(phase) || t('stream.phase.waitingModel');
    _phaseKey = "waiting-model:" + (phase.detailKey || phase.detail || "");
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(_txt)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "compacting") {
    const _txt = _phaseDetailText(phase);
    _phaseKey = "compact:" + (phase.detailKey || phase.detail);
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(_txt)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "retrying") {
    const _txt = _phaseDetailText(phase) || t('stream.phase.retrying');
    /* ★ Key on the RESOLVED TEXT, not on `attempt` alone (layer 2 of
     *   pt_1e7f6539b41c4d8f; layer 1 is `attempt` surviving the ingress
     *   whitelists in sse_pipeline.js / streaming_render.js).
     *
     *   The status zone swaps innerHTML ONLY when `data-phase-key` changes, so
     *   anything the key omits is invisible to the repaint no matter how the
     *   text moves. `attempt` alone is not enough: the first-byte heartbeat
     *   (lib/tasks_pkg/manager/_stream.py::_on_waiting) holds `detailKey`
     *   CONSTANT and advances only `detailArgs.elapsed`, so "已等待 20s →
     *   40s → 60s" recomputes `_txt` every beat and then throws it away —
     *   the line freezes until a conv switch tears the bubble down. Folding
     *   `_txt` in reduces the rule to "if the visible text changed, repaint",
     *   which is the only thing the user can actually perceive. `attempt`
     *   stays in front so two beats that resolve to the SAME string still
     *   keep distinct identity. */
    _phaseKey = "retry:" + (phase.attempt || 0);
    _phaseHtml = `<div class="stream-phase stream-phase-retrying"><span class="stream-phase-icon">${Icon('refresh', 14)}</span><span class="stream-phase-text">${escapeHtml(_txt)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "tool_exec" && !hasActiveSearch && hasInFlightTool) {
    /* ★ Gate (owner report 2026-08-03): render ONLY while a tool round is
     *   genuinely in-flight — a settled-everything tool_exec phase is the
     *   stale window between the last tool_result and the next round's
     *   phase, where the row claims a tool is running when none is (the
     *   "phase says Applying changes but only settled cards are visible"
     *   desync). With no in-flight tool we fall THROUGH to the content-
     *   based branches, which honestly show waiting / reasoning / none.
     *   `hasActiveSearch` stays in the gate: while a round is searching
     *   the tool CARDS own the running display (the 'search' branch
     *   blanks this row) — the phase row's unique value is the
     *   approval / human-wait window the cards can't headline. */
    const _txt = _toolExecPhaseText(phase);
    /* Key on the RESOLVED text (same ruling as the retrying branch): the
     *   status zone repaints only when the key changes, and the resolved
     *   label is what the user can actually perceive. */
    _phaseKey = "exec:" + _txt;
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(_txt)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else if (phase && phase.phase === "working" && (phase.detailKey || phase.detail)) {
    /* Generic "working" phase from external backends (e.g. "Initializing Claude Code...") */
    const _txt = _phaseDetailText(phase);
    _phaseKey = "working:" + (phase.detailKey || phase.detail);
    _phaseHtml = `<div class="stream-phase"><span class="stream-phase-text">${escapeHtml(_txt)}</span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
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
    _phaseHtml = `<div class="stream-phase stream-phase-thinking"><span class="stream-phase-text">${escapeHtml(t('stream.phase.reasoning'))}<span class="stream-phase-counter">${escapeHtml(t('stream.phase.chars', { n: _thSize }))}</span></span><span class="stream-phase-dots"><span>.</span><span>.</span><span>.</span></span></div>`;
  } else {
    _phaseKey = "none";
    _phaseHtml = "";
  }
  /* ★ Stall banner (pt_e0ea29f2 — the "no unannounced freeze" watch;
   *   regime split, owner ruling 2026-08-04). stall_watch.js flags the task
   *   only when NOTHING has arrived past the threshold — not even a
   *   heartbeat self-tick — and THIS seam paints the announcement (it takes
   *   precedence over every other phase: a frozen stream's phase row is
   *   blank by design, exactly the dead zone the user reported). A tool
   *   merely EXECUTING silently (heartbeats flowing) is normal and never
   *   raises this card — the tool row already counts the seconds, and the
   *   backend reaper owns the genuinely-wedged case. Read on EVERY repaint
   *   so a late real event flips it back off (self-healing, same as the
   *   swarm stalled-card). */
  const _swTaskId = (function () {
    if (typeof conversations === 'undefined' || typeof activeConvId === 'undefined') return null;
    const _c = conversations.find(x => x.id === activeConvId);
    return (_c && _c.activeTaskId) || null;
  })();
  const _st = (_swTaskId && typeof stallWatchState === 'function')
    ? stallWatchState(_swTaskId) : null;
  if (_st && _st.stalled) {
    _phaseKey = "stalled";
    _phaseHtml = `<div class="stream-phase stream-phase-stalled"><span class="stream-phase-icon">⚠</span><span class="stream-phase-text stream-stalled-text" data-stall-task="${_swTaskId}">${escapeHtml(t('stream.stalled.banner', { n: _st.silentSecs }))}</span><button type="button" class="stream-stalled-stop" onclick="stallWatchStop(activeConvId,'${_swTaskId}')">${escapeHtml(t('stream.stalled.stop'))}</button></div>`;
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

/* ★ Render one streaming round's per-round thinking + narration INSIDE its
 *   `.ptool-turn` group, above the tool slots — the live mirror of the settled
 *   `_renderTimelineBatch` (ui/tool_rounds.js). Called once per group during
 *   `_syncToolRoundsDOM` when the segment-timeline flag is ON.
 *
 *   Markup is BYTE-IDENTICAL to the settled render's `.seg-thinking` /
 *   `.seg-narration` blocks (the panel carries `seg-timeline`, so the settled
 *   `.seg-timeline .seg-*` CSS applies verbatim — no fork). Idempotent: keyed
 *   on the rendered text length via data attributes so a repeated call with
 *   unchanged prose is a no-op (never churns the live tool DOM). The nodes are
 *   inserted BEFORE the round-tag / parallel-header / first `[data-prn]` slot
 *   so prose always sits above the tools it introduced, and the English
 *   narration is skipped while auto-translate is live (the translator paints
 *   the Chinese equivalent into a settled-class `.seg-narration` twin in the
 *   same group — Phase 3.5 step 2 byte parity; formerly `.stream-seg-narration`). */
function _renderStreamRoundProse(groupEl, round) {
  if (!groupEl || !round) return;
  /* ★ PARITY WITH SETTLED (owner directive 2026-07-08): the per-round thinking
   * + narration must render as INDEPENDENT SIBLINGS of the `.ptool-turn` tool
   * card — inside `.ptool-panel-body`, immediately BEFORE the card — never
   * nested inside it. The settled `_renderTimelineBatch` concatenates
   *   [thinking][narration][.ptool-turn]
   * as flat siblings in the panel body; this is the live mirror. Nesting the
   * prose inside the card (the old behaviour) was exactly the "thinking +
   * narration + tool glued in one box" the screenshot showed. Each prose block
   * is tagged `data-seg-round` = the card's llmRound key so it is located
   * idempotently and kept adjacent to its own card. */
  const body = groupEl.parentNode;   // .ptool-panel-body — prose lives HERE
  if (!body) return;
  const gkey = groupEl.getAttribute("data-llm-round") || "";
  const _esc = (typeof CSS !== "undefined" && CSS.escape) ? CSS.escape : (s) => s;
  const _q = (cls) => body.querySelector(
    `:scope > .${cls}[data-seg-round="${_esc(gkey)}"]`);

  // ── Per-round thinking (collapsed disclosure, settled markup verbatim) ──
  const _think = round.thinking || "";
  let thinkEl = _q("seg-thinking");
  if (_think) {
    if (!thinkEl) {
      const len = _think.length;
      const meta = len >= 1024 ? ` (${Math.round(len / 1024)}k chars)` : ` (${len} chars)`;
      thinkEl = document.createElement("div");
      thinkEl.className = "thinking-block seg-thinking";
      thinkEl.setAttribute("data-seg-round", gkey);
      thinkEl.setAttribute("onclick", "this.classList.toggle('expanded')");
      thinkEl.innerHTML =
        `<div class="thinking-header"><span class="thinking-label">${escapeHtml(t('stream.thinking.done'))}${escapeHtml(meta)}</span><span class="thinking-toggle">▼</span></div>` +
        `<div class="thinking-content"><div class="thinking-text"></div></div>`;
      body.insertBefore(thinkEl, groupEl);
    }
    const _txtEl = thinkEl.querySelector(".thinking-text");
    /* ★ SELF-HEAL (shadow-key cache-invalidation fix): the skip key
     * `_lastThink` is a shadow JS property, but the source of truth is the DOM
     * text node. A transient clobber (a competing writer / rAF-vs-poll race)
     * can dirty `_txtEl.textContent` WITHOUT updating `_lastThink`, so the
     * `_lastThink === _think` skip would leave the dirty text pinned until a
     * full rebuild (page reload → renderSegmentTimelineHTML). Re-sync when the
     * DOM drifted from what we last wrote. Cheap: a single textContent string
     * compare, NO re-render on the hot path (only writes when actually stale). */
    if (_txtEl && (thinkEl._lastThink !== _think || _txtEl.textContent !== _think)) {
      thinkEl._lastThink = _think;
      _txtEl.textContent = _think;
      const _lbl = thinkEl.querySelector(".thinking-label");
      const len = _think.length;
      const meta = len >= 1024 ? ` (${Math.round(len / 1024)}k chars)` : ` (${len} chars)`;
      if (_lbl) _lbl.textContent = t('stream.thinking.done') + meta;
    }
  } else if (thinkEl) {
    thinkEl.remove();
  }

  // ── Per-round narration (English) — hidden ONLY for a round whose Chinese
  //    twin has landed (see the per-round .xlate-hidden gate below); otherwise
  //    it stays visible as the fallback. ──
  const _narr = round.assistantContent || "";
  let narrEl = _q("stream-seg-en-narration");
  if (_narr) {
    if (!narrEl) {
      narrEl = document.createElement("div");
      narrEl.className = "md-content seg-narration stream-seg-en-narration";
      narrEl.setAttribute("data-seg-round", gkey);
      body.insertBefore(narrEl, groupEl);
    }
    /* ★ SELF-HEAL: `_lastNarr` is a shadow key; the DOM is the truth. If the
     * rendered innerHTML was clobbered externally without `_lastNarr` moving,
     * the skip would pin the dirty markup until reload. Compare the current
     * innerHTML against what we last wrote (cheap string compare — the
     * expensive renderMarkdown only runs when genuinely stale). */
    if (narrEl._lastNarr !== _narr || narrEl.innerHTML !== narrEl._lastNarrHtml) {
      narrEl._lastNarr = _narr;
      try { narrEl.innerHTML = renderMarkdown(_narr); }
      catch (_e) { narrEl.textContent = _narr; }
      narrEl._lastNarrHtml = narrEl.innerHTML;
    }
    /* ★ PER-ROUND bilingual gate: hide this round's English ONLY when its
     * Chinese twin (painted by the incremental translator) already exists for
     * the SAME round. The zh node carries the SETTLED class list
     * (`seg-narration`, byte parity with the cold render — see
     * translation_render.js), so it is found here by EXCLUSION from the
     * English sibling. A tool-sync re-render can run after the translator
     * painted Chinese, so re-assert the class here; and never hide a round
     * whose Chinese hasn't landed — that was the "intermediate narration
     * vanishes under auto-translate" bug. */
    const _zhTwin = _q("seg-narration:not(.stream-seg-en-narration)");
    narrEl.classList.toggle("xlate-hidden", !!_zhTwin);
  } else if (narrEl) {
    narrEl.remove();
  }

  /* Deterministic sibling order directly before the card: thinking → English
   * narration → Chinese narration. insertBefore(el, groupEl) applied in
   * this order lands them contiguous and correctly sequenced regardless of
   * which path (tool sync vs. translate push) created them first. */
  const _zh = _q("seg-narration:not(.stream-seg-en-narration)");
  for (const el of [thinkEl, narrEl, _zh]) {
    if (el && el.parentNode === body) body.insertBefore(el, groupEl);
  }
}

/* ── Live DOM reposition of synthetic inject-row groups ──────────────────
 * The main _syncToolRoundsDOM loop appends a NEW group with `body.appendChild`
 * and never relocates an existing one. A mid-turn inject chip (steer / peer /
 * async swarm) always arrives AFTER its anchor round's tool_start events (the
 * chip event is deferred until the round's LLM call confirms consumption), so
 * its solo `S{roundNum}` group is created at the TAIL — the sink-to-bottom bug.
 * The array-level `_spliceInjectRow` fixes the settled/rehydrate rebuild paths
 * (which re-derive the DOM from scratch, honoring array order), but the live
 * incremental DOM needs this corrective pass: after the group loop, move each
 * synthetic group ABOVE its anchor round (llmRound === injectRound-1), landing
 * it before that round's earliest prose sibling / tool group — the top of the
 * round that consumed the inject. Additive + idempotent: a no-op once the chip
 * already sits above its anchor. */
function _repositionInjectGroups(body, rounds) {
  if (!body || !Array.isArray(rounds)) return;
  const _esc = (typeof CSS !== "undefined" && CSS.escape) ? CSS.escape : (s) => s;
  for (const r of rounds) {
    if (!r || !(r._userSteerInject || r._peerInject || r._inboxInject)) continue;
    const injRound = r._userSteerInject ? r.steerRound
      : (r._peerInject ? r.peerRound : r.inboxRound);
    const anchor = (injRound || 0) - 1;
    if (anchor < 0) continue;
    const sGroup = body.querySelector(
      `.ptool-turn[data-llm-round="${_esc("S" + r.roundNum)}"]`);
    if (!sGroup) continue;
    // The anchor round's earliest DOM node: its prose siblings (data-seg-round)
    // sit as `body` children BEFORE its `.ptool-turn`, so scan all children in
    // order and take the FIRST that belongs to the anchor round.
    const lkey = "L" + anchor;
    let target = null;
    for (const child of body.children) {
      if (child === sGroup) continue;
      if (child.getAttribute("data-seg-round") === lkey
          || child.getAttribute("data-llm-round") === lkey) { target = child; break; }
    }
    if (target && sGroup.nextSibling !== target && sGroup !== target) {
      body.insertBefore(sGroup, target);
    }
  }
}

function _syncToolRoundsDOM(container, rounds) {
  /* ★ Superseded-orphan drop, LIVE-streaming path (parity with the SETTLED
   *   renderToolRoundsHTML / renderSegmentTimelineHTML filters in
   *   ui/tool_rounds.js). A FloorRetry / stream-retry duplicate whose tc_id
   *   never survived into the final assistant_msg is stamped badge='superseded'
   *   (result-less) by the backend reconcile_announced_rounds — its adopted /
   *   recovered twin is the real call. This LIVE path was the remaining
   *   coverage gap: it renders straight from msg.toolRounds and never ran the
   *   filter, so the husk showed a misleading "interrupted"/"superseded" chip
   *   for the whole rest of the turn, only vanishing on a full page reload
   *   (which rebuilds via the already-fixed renderSegmentTimelineHTML). Two
   *   steps are needed here (not just a render-list filter): the husk's slot
   *   was already CREATED as 'searching' on tool_start, THEN downgraded by
   *   reconcile, so we must also PRUNE that stale [data-prn] slot. Filtering
   *   `rounds` up-front also keeps the header count / fingerprint / visible-
   *   window all consistent, since the whole function keys off `rounds`. */
  if (typeof _isSupersededOrphanRound === "function") {
    let _hasSup = false;
    for (const r of rounds) { if (_isSupersededOrphanRound(r)) { _hasSup = true; break; } }
    if (_hasSup) {
      for (const r of rounds) {
        if (!_isSupersededOrphanRound(r)) continue;
        const _slot = container.querySelector(`[data-prn="${r.roundNum}"]`);
        if (_slot) {
          const _grp = _slot.parentElement;
          _slot.remove();
          /* Drop a group left empty by the pruned husk (shouldn't normally
           * happen — the recovered twin shares the batch — but keep the DOM
           * clean if it does). */
          if (_grp && _grp.classList.contains("ptool-turn")
              && !_grp.querySelector("[data-prn]")) _grp.remove();
        }
      }
      rounds = rounds.filter((r) => !_isSupersededOrphanRound(r));
    }
  }
  // ★ Fast-path: skip if rounds haven't changed since last sync
  let _fp = rounds.length;
  for (let i = 0; i < rounds.length; i++) {
    const r = rounds[i];
    _fp = Math.imul(_fp, 31) + (r.roundNum | 0);
    _fp = Math.imul(_fp, 31) + (r.status === 'searching' ? 1 : r.status === 'done' ? 2
        : r.status === 'awaiting_human' ? 3 : r.status === 'submitted' ? 4
        : r.status === 'pending_approval' ? 5 : 0);
    _fp = Math.imul(_fp, 31) + ((r.results && r.results.length) || 0);
    _fp = Math.imul(_fp, 31) + (r.toolContent ? 1 : 0);
    /* ★ Per-round interleave prose (segment-timeline step 5b): delta_reset
     * stamps assistantContent/thinking onto the batch's first round AFTER it
     * was already rendered as a tool_start slot. Without these in the fp the
     * gate bails and the captured narration never paints until the next tool
     * event forces a rebuild — the exact "thinking lost live" bug. */
    _fp = Math.imul(_fp, 31) + ((r.assistantContent && r.assistantContent.length) || 0);
    _fp = Math.imul(_fp, 31) + ((r.thinking && r.thinking.length) || 0);
    /* llmRound drives parallel-batch grouping — a sibling arriving in the
     * same turn changes a group's size/header, so it must move the fp. */
    _fp = Math.imul(_fp, 31) + ((r.llmRound | 0) + 1);
    _fp = Math.imul(_fp, 31) + (r._hgTranslating ? 1 : 0);
    /* compactionLayer set means a tool_compacted SSE just landed —
     * re-render so the row picks up the COMPACTED label. Include
     * the from/to char counts so a follow-up compaction (e.g. L1 → L3
     * graduation) also re-renders. */
    _fp = Math.imul(_fp, 31) + (r.compactionLayer ? r.compactionLayer.length : 0);
    _fp = Math.imul(_fp, 31) + (r.compactedToChars | 0);
    _fp = Math.imul(_fp, 31) + (r.compactedFromChars | 0);
    if (r._translatedQuestion) _fp = Math.imul(_fp, 31) + r._translatedQuestion.length;
    if (r._timerPolls) _fp = Math.imul(_fp, 31) + r._timerPolls.length;
    if (r._timerSkipCount) _fp = Math.imul(_fp, 31) + r._timerSkipCount;
    /* ★ Timer next-poll rollover — fold the scheduled next-poll time (in
     *   seconds) so a new nextPollTs (sent every poll/skip via timer_poll_check)
     *   ALWAYS forces this round to re-render and refresh its [data-timer-next]
     *   attribute. Unlike swarm's data-sw-start (an immutable START time), the
     *   timer countdown target CHANGES each cycle, so relying on _timerPolls
     *   length correlation alone would be an implicit rollover dependency — this
     *   makes the attribute update an explicit guarantee. The 1Hz ticker then
     *   only animates the seconds within a cycle. */
    if (r._timerNextPollTs) _fp = Math.imul(_fp, 31) + ((r._timerNextPollTs / 1000) | 0);
    /* ★ Swarm fields — without these, swarm_agent_* events mutate
     *   round._swarmAgents but the fingerprint stays equal, the gate
     *   bails, and the panel never re-renders until page refresh. */
    if (r._swarm) {
      _fp = Math.imul(_fp, 31) + (r._swarmActive ? 1 : 0);
      _fp = Math.imul(_fp, 31) + (r._asyncRunning ? 1 : 0);
      const agents = r._swarmAgents || [];
      _fp = Math.imul(_fp, 31) + agents.length;
      for (const a of agents) {
        _fp = Math.imul(_fp, 31) + (a.status === 'running' ? 1 : a.status === 'done' ? 2
            : a.status === 'failed' ? 3 : a.status === 'thinking' ? 4 : 0);
        _fp = Math.imul(_fp, 31) + ((a.phase || '').length);
        _fp = Math.imul(_fp, 31) + ((a.preview || '').length);
        _fp = Math.imul(_fp, 31) + ((a.tools || []).length);
        _fp = Math.imul(_fp, 31) + ((a.modifiedFiles | 0) + 1);
        _fp = Math.imul(_fp, 31) + (a._toolCalls ? a._toolCalls.length : 0);
        if (a._toolCalls && a._toolCalls.length) {
          /* Last call's status flips on every finish event — must
           * be in the fingerprint or the timeline freezes. */
          const last = a._toolCalls[a._toolCalls.length - 1];
          _fp = Math.imul(_fp, 31) + (last.status === 'running' ? 1 : last.status === 'done' ? 2 : 3);
          _fp = Math.imul(_fp, 31) + ((last.elapsed * 100) | 0);
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

  /* ★ Segment-timeline streaming interleave: the live panel always carries the
   *   `seg-timeline` class (hardcoded into the className strings below) so the
   *   SETTLED `.seg-timeline .seg-thinking` / `.seg-timeline .seg-narration`
   *   rules apply VERBATIM to the per-round prose rendered inside each group —
   *   byte-identical look to the finished render, zero CSS fork. This is now
   *   the only render path; the former `_segTimelineEnabled` toggle was
   *   removed. */

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
        "ptool-panel animation-slideUp seg-timeline" +
        (anyActive ? " ptool-panel-active" : "");
      el.innerHTML = `<div class="ptool-panel-header"><span class="ptool-panel-label">${headerLabel}</span></div><div class="ptool-panel-body"></div>`;
      container.appendChild(el);
      body = el.querySelector(".ptool-panel-body");
    } else {
      unifiedPanel.className =
        "ptool-panel seg-timeline" + (anyActive ? " ptool-panel-active" : "");
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
        /* ★ Per-round interleave prose (segment-timeline step 5b): render THIS
         *   round's thinking + narration INSIDE its group, ABOVE the tool
         *   slots — the streaming mirror of the settled `_renderTimelineBatch`.
         *   The prose is stamped onto the batch's FIRST round by the
         *   `delta_reset` handler (sse_pipeline.js) exactly as the backend
         *   `assemble_segments` does, so finalize is a visual no-op. Only the
         *   first round of a batch carries the prose. Reuses the SETTLED
         *   `.seg-thinking` / `.seg-narration` classes verbatim (the panel
         *   carries `seg-timeline`) — zero CSS fork. Idempotent + fingerprint-
         *   gated: only (re)writes when the text grew, so it never churns the
         *   tool DOM. The English narration is HIDDEN when auto-translate is
         *   live (`data-xlate`): the translator paints the Chinese equivalent
         *   into a settled-class `.seg-narration` twin in the same slot
         *   (formerly `.stream-seg-narration`). */
        _renderStreamRoundProse(groupEl, round);
        let slot = body.querySelector(`[data-prn="${rn}"]`);
        /* ★ Determine if this round needs an interactive card (HG, stdin, approval).
         *   Interactive cards are tall (200-300px) and must NOT be collapsed by
         *   content-visibility:auto (which assumes 32px intrinsic size for off-screen
         *   slots).  Force content-visibility:visible on these slots.
         *   Swarm panels are also tall and re-render frequently — same treatment. */
        const _isInteractive = round.status === "awaiting_human" || round.status === "awaiting_stdin"
          || round.status === "pending_approval";
        const _renderRow = (r, active) => (_isRoundSwarm(r) && typeof _buildSwarmPanelHTML === 'function')
          ? _buildSwarmPanelHTML(r, toolRounds)
          : _renderUnifiedToolLine(r, active);  // panel DEFERRED: generic line until it lands (Epic-E sub-5B)
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
            if (typeof _morphSwarmSlot === 'function' && typeof _buildSwarmPanelHTML === 'function') {
              _morphSwarmSlot(slot, _buildSwarmPanelHTML(round, toolRounds));
            } else {
              slot.innerHTML = _renderRow(round, isActive);  // panel DEFERRED: generic line
            }
          }
        } else if (_isSwarm) {
          /* Swarm rounds change frequently (per-agent phase / preview / tool-call
           * timeline) — the fingerprint gate above already guaranteed something
           * actually changed. Morph the existing panel IN PLACE instead of
           * `innerHTML =` (a full teardown/rebuild), which restarted the
           * `swarmBorderPulse` animation from 0% every event → visible flicker,
           * and collapsed any agent card the user had expanded. */
          slot.style.contentVisibility = "visible";
          if (typeof _morphSwarmSlot === 'function' && typeof _buildSwarmPanelHTML === 'function') {
            _morphSwarmSlot(slot, _buildSwarmPanelHTML(round, toolRounds));
          } else {
            slot.innerHTML = _renderRow(round, isActive);  // panel DEFERRED: generic line
          }
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

      /* ★ Corrective pass: relocate any synthetic inject-row group (steer /
       *   peer / async swarm) ABOVE the round that consumed it. The main loop
       *   above only ever appends a new group to the tail, so a late-arriving
       *   chip sinks to the bottom — this moves it to the top of its anchor
       *   round. Idempotent; runs after all groups + headers exist. */
      _repositionInjectGroups(body, toolRounds);
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

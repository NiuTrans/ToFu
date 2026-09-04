/* ===== migrated source: context-bar.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   context-bar.js — Context Health Bar (the "liquid bubble")

   A small status chip in the conversation-status strip (`#convStatusStrip`,
   directly above the composer) that shows the current conversation's
   prompt-token usage as a percentage of the active model's context window.
   It used to float absolutely on the left flank of `.chat-wrapper` — that
   anchoring genuinely overlapped the message column below a ~1420px pane,
   so it moved in-flow (see tests/test_turn_ctx_rail_geometry.py).

   The visual is a 28×28 SOLID circular vessel filled with liquid that
   rises from the bottom as context grows.  The vessel has a glass rim
   stroke, an upper-left shine highlight (sells "vessel"), and the
   liquid surface is a wavy SVG path with a thin bright meniscus
   stroke on top (sells "real liquid surface").  Wave geometry is
   static — the surface waveform never moves; only the liquid level
   rises/falls on actual percentage change.

   As usage climbs through warn / hot / crit, the liquid (and its
   meniscus) warm toward amber → orange → red.

   Compactions: counter badge top-right of the vessel.  Clicking the
   chip opens the Compaction Viewer drawer (compaction-viewer.js).
   The earlier in-gauge timeline (radial ticks / sesame seeds / dots)
   is intentionally absent — past liquid-level marks inside the bubble
   competed with the surface read, so we let the badge + viewer
   handle the timeline now.

   ── Anti-flicker invariants (DO NOT regress) ──
   1. Every node is CREATED ONCE in `_ensureBar()` and cached on the
      element via `_state`.  Subsequent updates only set CSS custom
      properties / set `textContent` / set `dataset.zone`.  NO
      `innerHTML` rebuilds, NO `appendChild`/`removeChild` on the hot
      path EXCEPT the ticks layer, which is rebuilt only when its
      memoized signature changes (typical: 0–3 ticks per conv).
   2. Property writes are no-ops if the value is unchanged.
   3. No idle CSS animation loops.  The only motion is the marinade
      fill's width growing on actual value change, animated via the
      `--ctx-arc-pct` `@property` transition.  Cut highlight and
      "fresh-cut" land cues are discrete one-shot responses to events,
      not ambient loops.
   4. No `@media` opacity rules.  The chip stays at full opacity at
      every width where it's shown; it's HIDDEN exactly where the "···"
      mobile sheet takes over the compaction entry point — ≤768 px, or
      ≤1024 px with a coarse pointer (see styles.css:~20417). On
      fine-pointer narrow windows (769–1024 px) the chip stays visible so
      there is never a width with no reachable "compact now" entry.

   Public API:
     runtimeScope.updateContextBar()        — recompute + repaint (rAF-coalesced).
     runtimeScope.flashGaugeForArchive(id)  — discrete cue when a new compaction
                                        lands; pulses the matching tick.
     runtimeScope._resolveContextLimit()    — exported for debug.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ── Context-window policy.  The authoritative limits + compaction
   *    thresholds live in Python (lib/tasks_pkg/compaction) and are served
   *    via /api/v1/server-config → cached on `runtimeScope._contextPolicy` by
   *    _loadServerConfigAndPopulate().  We DO NOT re-derive them here — a
   *    hard-coded copy silently drifted (the JS threshold was stuck at 0.82
   *    vs the real 0.90, and the regex limit table missed learned overrides).
   *
   *    Policy shape (see build_context_policy()):
   *      { default_limit, output_reserve, compaction_reserve,
   *        summary_trigger_ratio, working_set_tokens,
   *        per_model: { <model_id>: <limit> } }
   *
   *    `_FALLBACK_LIMIT` is used only before the config has loaded (or if the
   *    fetch failed) — it must never be the primary source of truth. */
  const _CRIT_THRESHOLD = 0.95;

  function _policy() {
    return (typeof window !== 'undefined' && runtimeScope._contextPolicy) || null;
  }

  /* Fraction of the FULL context window at which the server force-compacts.
   * The server triggers on the lower of `usable * summary_trigger_ratio` and
   * `working_set_tokens` (when non-zero), where
   * `usable = limit - output_reserve - compaction_reserve`, so the
   * full-window fraction is model-dependent. Compute it exactly from the
   * policy for the given limit; fall back to a conservative 0.82 only until
   * the policy loads. */
  function _compactThreshold(limit) {
    const p = _policy();
    if (!p || !limit) return 0.82;
    /* Mirror lib/tasks_pkg/compaction/_tokens.py::_usable_context — a fixed
     * output_reserve can exceed a small window, so usable is floored at
     * min_usable_ratio of the limit.  Keep this in lock-step with the server
     * or the "hot" zone won't line up with the real auto-compact trigger. */
    const raw = limit - (p.output_reserve || 0) - (p.compaction_reserve || 0);
    const usable = Math.max(raw, limit * (p.min_usable_ratio || 0.5));
    if (usable <= 0) return 0.82;
    const ratio = (p.summary_trigger_ratio || 0.9);
    const windowTrigger = usable * ratio;
    const workingSet = Number(p.working_set_tokens || 0);
    const effectiveTrigger = workingSet > 0
      ? Math.min(windowTrigger, workingSet)
      : windowTrigger;
    return Math.min(0.99, effectiveTrigger / limit);
  }

  function _resolveContextProfile(modelId, providerId) {
    const p = _policy();
    const unknown = { window: null, source: 'unknown', exact: false };
    if (!p || !modelId || !p.per_model) return unknown;
    const key = (providerId || '') + '::' + modelId;
    const value = p.per_model[key] != null ? p.per_model[key] : p.per_model[modelId];
    if (value && typeof value === 'object') return value;
    /* Compatibility with an older server that returned bare integer entries. */
    if (Number(value) > 0) return { window: Number(value), source: 'legacy', exact: false };
    if (Number(p.default_limit) > 0) {
      return { window: Number(p.default_limit), source: 'legacy_default', exact: false };
    }
    return unknown;
  }

  function _resolveContextLimit(modelId, providerId) {
    return _resolveContextProfile(modelId, providerId).window;
  }

  function _activeConv() {
    return getConvById(typeof activeConvId !== 'undefined' ? activeConvId : null);
  }

  function _activeModel(conv) {
    if (conv && conv.model) return conv.model;
    if (typeof config !== 'undefined' && config && config.model) return config.model;
    if (typeof serverModel !== 'undefined' && serverModel) return serverModel;
    return '';
  }

  function _activeProvider(conv) {
    if (conv && conv.provider_id) return conv.provider_id;
    if (typeof config !== 'undefined' && config) return config.provider_id || '';
    return '';
  }

  /* Reduce a single usage dict (one API round) to "total prompt tokens
   * sent on that round".  Anthropic convention: prompt_tokens is the
   * uncached residual only — when cache_* are non-zero and the residual
   * is <= cache, the real total is inp + cw + cr.  Otherwise (OpenAI
   * convention) prompt_tokens already includes cache. Mirrors ui.js:1853. */
  function _promptTokensFromUsage(u) {
    if (!u) return 0;
    const inp = u.prompt_tokens || u.input_tokens || 0;
    const cw  = u.cache_write_tokens || u.cache_creation_input_tokens || 0;
    const cr  = u.cache_read_tokens  || u.cache_read_input_tokens  || 0;
    if ((cw > 0 || cr > 0) && inp <= cw + cr) return inp + cw + cr;
    return inp;
  }

  /* Estimate the size of the prompt the model JUST received — the
   * closest available proxy for what the NEXT request will look like.
   * Source priority (most → least live):
   *
   *   1. `projection._liveLastRoundUsage.tokensIn` — populated by the
   *      `round_usage` SSE event (lib/tasks_pkg/llm_fallback.py
   *      :_emit_round_usage) the moment each LLM round lands.  This is
   *      what makes the gauge move between tool rounds, BEFORE the
   *      `done` event arrives.  Server-side already normalized
   *      Anthropic-vs-OpenAI cache convention into a single number.
   *   1b. `projection.lastRoundUsage.tokensIn` — the durable equivalent of
   *      (1): the round_usage frame's reading folded into the durable turn
   *      projection (lib/turn_lifecycle.py::_task_projection). This is the
   *      ONLY live source under turn-native protocol, where the v1 SSE lane
   *      (and its round_usage handler) never runs.
   *   2. `projection.apiRounds[-1].usage` — populated at settlement.
   *      Used for completed Turns on reload
   *      (when `_liveLastRoundUsage` is gone) and at the moment a
   *      task transitions from streaming to done.
   *   3. `projection.usage / N` — last-ditch average, ONLY when N is known
   *      from a non-empty `apiRounds` (e.g. every round's usage read
   *      zero on stream-anomaly rounds).  `msg.usage` is the
   *      per-message ACCUMULATED sum across all rounds (see
   *      lib/tasks_pkg/llm_fallback.py:144) — tokens BILLED, not
   *      tokens currently in the prompt — so with N unknown it must
   *      not be presented as a prompt size. Projections with no apiRounds
   *      show no reading rather
   *      than a fabricated one.
   *
   * Walks assistant Turns newest-first and returns the first usable
   * reading. Skips zero readings so an in-flight bubble with no usage
   * yet doesn't shadow the previous turn's number. */
  function _lastUsageTokens(conv) {
    if (!conv) return 0;
    const state = runtimeScope.ConversationTurnRead?.state?.(conv);
    const turns = runtimeScope.ConversationTurnRead?.ordered?.(conv) || [];

    /* ── Gauge scheme B, correctly ordered by RECENCY (the "ball never drops
     *    after /compact" bug). A manual compaction rewrites the conversation to
     *      [system] + [anchor?] + [summary] + [preserved reserve turns...]
     *    The preserved reserve assistants sit AFTER the summary in ARRAY order
     *    but ran BEFORE it, so they still carry their PRE-compaction usage (the
     *    huge old prompt size). A naive newest-by-INDEX walk reads that stale
     *    number first and the liquid never falls. The summary carries the true
     *    post-compaction size in `_estimatedPromptTokens` and, being minted at
     *    compaction time, has the NEWEST timestamp — so if it is at least as
     *    recent as every real-usage assistant, it must win. A genuinely newer
     *    real turn (typed after the compaction) naturally supersedes it. */
    let summaryEst = 0, summaryTs = -1;
    for (let i = turns.length - 1; i >= 0; i--) {
      const turn = turns[i];
      const projection = turn.projection || {};
      if (turn.actor === 'assistant' && projection._isCompactionSummary
          && projection._estimatedPromptTokens > 0) {
        summaryEst = projection._estimatedPromptTokens;
        summaryTs = projection.timestamp || turn.updatedAt || 0;
        break;                                   // newest summary by index
      }
    }

    for (let i = turns.length - 1; i >= 0; i--) {
      const turn = turns[i];
      const m = turn.projection || {};
      if (turn.actor !== 'assistant') continue;
      /* A real-usage reading only supersedes the summary estimate when its
       * message is strictly NEWER than the summary — otherwise it is a
       * preserved reserve turn that ran before the compaction and carries a
       * stale (pre-compaction) prompt size. */
      if (summaryEst > 0 && (m.timestamp || turn.updatedAt || 0) <= summaryTs) continue;
      // 1. Live in-flight reading from the round_usage SSE event.
      const liveUsage = state?.liveRoundUsageByTurn?.[turn.turnId];
      if (liveUsage && liveUsage.tokensIn > 0) {
        return liveUsage.tokensIn;
      }
      // 1b. turn-native turn lane: the same per-round reading, folded into the
      //     durable turn projection by lib/turn_lifecycle.py::_task_projection
      //     (stashed as task._lastRoundUsage by llm_fallback._emit_round_usage
      //     on every response-authoring round, so it is fresh BETWEEN tool
      //     rounds — apiRounds only lands at finalize). Riding the projection means it survives
      //     projection rebuilds, reconnect tail-hydration and slim-delta
      //     windows, none of which session-only SSE state could. Compaction
      //     and discarded billing calls remain accounting-only apiRounds.
      if (m.lastRoundUsage && m.lastRoundUsage.tokensIn > 0) {
        return m.lastRoundUsage.tokensIn;
      }
      // 2. Final apiRounds breakdown (post-done event).
      if (Array.isArray(m.apiRounds) && m.apiRounds.length) {
        const authoritativeUsage = typeof latestAgentApiRoundUsage === 'function'
          ? latestAgentApiRoundUsage(m.apiRounds) : null;
        if (authoritativeUsage) {
          const tokens = _promptTokensFromUsage(authoritativeUsage);
          if (tokens > 0) return tokens;
        }
        // Compatibility for isolated retained-section harnesses. Production
        // composes the typed selector above; this path still excludes both
        // historical auxiliary row shapes.
        for (let j = m.apiRounds.length - 1; j >= 0; j--) {
          const round = m.apiRounds[j] || {};
          if (round.kind === 'compaction'
              || /(?:^|[-_])(?:discarded|compaction)(?:$|[-_])/i
                .test(round.tag || '')) continue;
          const t = _promptTokensFromUsage(round.usage);
          if (t > 0) return t;
        }
      }
      // 3. Last-ditch average over the KNOWN round count. The divisor
      //    comes from apiRounds; a genuinely LEGACY message (pre-turn
      //    protocol, no `_turnId`) may still fall back to ÷1 — its usage
      //    IS the whole turn's bill, which for the single-round turns of
      //    that era equals the prompt. A TURN-NATIVE message missing
      //    apiRounds is the dropped-projection shape (2026-08-20: the turn-native
      //    turn projection stripped apiRounds, so every reloaded message
      //    fell through to ÷1 and presented a 35-round 1.3M bill as one
      //    prompt — fake "1.3M / 1.1M = 100%" on a real ~170k prompt).
      //    For those, no reading beats a fabricated one.
      if (m.usage) {
        const t = _promptTokensFromUsage(m.usage);
        const n = typeof agentApiRoundCount === 'function'
          ? agentApiRoundCount(m.apiRounds)
          : (Array.isArray(m.apiRounds) && m.apiRounds.length) || 0;
        if (t > 0 && n > 0) return n > 1 ? Math.round(t / n) : t;
      }
    }
    // 4. No real-usage reading newer than the compaction summary — fall back to
    //    the server-computed post-compaction estimate (scheme B). This is what
    //    makes the liquid drop immediately after a /compact even though the
    //    preserved reserve turns still carry their old usage. Server fact, not
    //    a client inference.
    return summaryEst;
  }

  /* Read compaction history from its endpoint-backed presentation cache.
   * Transcript Turns carry only the compaction summary block they own. */
  function _collectCompactions(conv) {
    if (!conv) return [];
    return [...(runtimeScope.getCompactionHistory?.(conv.id) || [])];
  }

  function _countCompactions(conv) {
    if (!conv) return 0;
    const count = runtimeScope.CompactionHistoryState?.count?.(conv.id);
    return Number.isFinite(count)
      ? Math.max(0, count) : _collectCompactions(conv).length;
  }

  function _formatTokens(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1) + 'M';
    if (n >= 1_000)     return (n / 1_000).toFixed(n >= 10_000 ? 0 : 1) + 'k';
    return String(n);
  }

  /* Bubble geometry constants.  The vessel SVG is a 32×32 viewBox so
   * we have a half-pixel margin around a 30-px circle — enough room
   * for the rim stroke without clipping.  These are the values the
   * static SVG below assumes; if you change them, update the SVG. */
  const _BUBBLE_VIEW = 32;
  const _BUBBLE_R    = 14;     // liquid disc radius (clip path)
  const _BUBBLE_CX   = 16;
  const _BUBBLE_CY   = 16;

  /* Surface geometry — a STILL liquid surface, not a frozen wave.
   *
   * Real chemistry-textbook meniscus: the liquid edges curve up
   * slightly where they touch the glass (capillary action), and the
   * middle is essentially flat.  At small sizes a sine wave reads as
   * "frozen mid-slosh" and feels uncanny; a still surface with a
   * subtle concave dip reads as natural and calm.
   *
   * The path is built ONCE in local coords (y=0 = the flat mid-line
   * of the surface).  The polygon closes downward so it can be filled
   * as the liquid BODY; a separate stroke-only twin path renders the
   * meniscus highlight along the top edge.
   *
   * Width is 2× the viewBox so horizontal positioning is forgiving.
   * Edge lift is small (~0.6 px) — readable as a meniscus without
   * dominating the silhouette.
   */
  const _MENISCUS_LIFT = 0.6;          // how high the edges rise above mid-line
  const _SURFACE_WIDTH = _BUBBLE_VIEW * 2;
  function _buildSurfacePath() {
    /* Three points define the surface shape:
     *   left edge  : y = -_MENISCUS_LIFT   (lifted by capillary action)
     *   middle     : y =  0                (flat)
     *   right edge : y = -_MENISCUS_LIFT
     * Smooth quadratic curves connect them so the surface is a
     * continuous gentle dip — not a frozen wave. */
    const left  = -_BUBBLE_VIEW;
    const right =  _SURFACE_WIDTH - _BUBBLE_VIEW;
    const mid   = (left + right) / 2;
    let d = `M ${left} ${-_MENISCUS_LIFT}` +
            ` Q ${(left + mid) / 2} 0 ${mid} 0` +
            ` Q ${(mid + right) / 2} 0 ${right} ${-_MENISCUS_LIFT}`;
    /* Close the polygon downward so the path can be filled as the
     * full liquid BODY. */
    d += ` L ${right} ${_BUBBLE_VIEW + 4}` +
         ` L ${left}  ${_BUBBLE_VIEW + 4} Z`;
    return d;
  }
  const _SURFACE_PATH = _buildSurfacePath();
  /* Back-compat aliases — kept so existing references don't break.
   * The "wave" name is preserved in the DOM and CSS for stability;
   * the SHAPE is now a still surface, not a wave. */
  const _WAVE_PATH = _SURFACE_PATH;
  const _WAVE_AMP  = _MENISCUS_LIFT;

  /* ── _state: per-bar cached node references, populated by `_ensureBar`.
   *    Hot-path updates only touch these — no new DOM is ever created. */
  let _state = null;

  function _ensureBar() {
    if (_state && _state.el && _state.el.isConnected) return _state;
    const existing = document.getElementById('contextHealthBar');
    if (existing && _state && _state.el === existing) return _state;
    /* The gauge is CONVERSATION-scoped chrome, so it lives IN-FLOW in the
     * conversation-status strip directly above the composer — never floated
     * over the message flow (its old absolute left:18px/top:50% anchoring
     * genuinely overlapped the message column below a ~1420px pane).
     * prepended so it is the strip's LEFT cell; the turn-nav is the right. */
    const host = document.getElementById('convStatusStrip');
    if (!host) return null;
    const el = existing || document.createElement('aside');
    if (!existing) {
      el.id = 'contextHealthBar';
      el.className = 'ctx-health-bar';
      el.setAttribute('aria-label', 'Context usage');
      host.prepend(el);
    }
    /* One-shot DOM build — never rebuilt on hot path.
     *
     * Geometry: a 28×28 circular vessel ("bubble") plus the standard
     * percentage / cap text labels.  All bubble layers live in a
     * single inline SVG so the whole vessel can be clip-pathed to a
     * circle, ensuring the wave + meniscus never bleed outside the
     * vessel silhouette no matter what level the liquid is at.
     *
     * SVG layers (z-order, back to front):
     *   <defs>
     *     <clipPath id="ctxBubbleClip"> circle r=14 — clips everything
     *       inside the bubble to a perfect circle.
     *   <circle .ctx-bubble-bg>  — empty vessel base (cream/dark fill)
     *   <g .ctx-bubble-liquid clip-path=ctxBubbleClip>
     *     <path .ctx-bubble-wave>     — the liquid body (rises via translateY)
     *     <path .ctx-bubble-meniscus> — bright stroke on the wave top
     *   <circle .ctx-bubble-rim>      — glass rim stroke
     *   <path   .ctx-bubble-shine>    — upper-left highlight arc
     *
     * NOTE: .ctx-bar-cap div is the "1M" tokens-cap text label —
     * naming inherited from earlier designs.  It is unrelated to the
     * bubble vessel. */
    /* Unique clip-path id per chip instance — avoids collisions if
     * the chip is ever rendered twice.  IIFE-scoped counter is fine
     * (we only ever create one instance, but defensive). */
    const clipId = 'ctxBubbleClip_' + Math.random().toString(36).slice(2, 8);
    /* The liquid body uses a vertical SVG gradient that goes from a
     * lighter top tint (light reflecting off the surface) to the
     * saturated zone color at the base.  Both stops are CSS-tinted
     * via stop-color so themes / zones drive the palette without
     * touching the gradient definition. */
    const gradId = 'ctxBubbleGrad_' + Math.random().toString(36).slice(2, 8);
    el.innerHTML =
      '<div class="ctx-bar-bubble" aria-hidden="true">' +
        '<svg viewBox="0 0 ' + _BUBBLE_VIEW + ' ' + _BUBBLE_VIEW + '" ' +
              'width="28" height="28" class="ctx-bubble-svg">' +
          '<defs>' +
            '<clipPath id="' + clipId + '">' +
              '<circle cx="' + _BUBBLE_CX + '" cy="' + _BUBBLE_CY + '" r="' + _BUBBLE_R + '"/>' +
            '</clipPath>' +
            /* Vertical gradient inside the liquid body.  y1=top, y2=bottom
             * in the CLIPPED frame.  The top stop is brighter; the bottom
             * stop is the deep zone color.  This is what really sells
             * "natural liquid" — calm liquids look like depth gradients,
             * not flat color fills. */
            '<linearGradient id="' + gradId + '" x1="0" y1="' +
              (_BUBBLE_CY - _BUBBLE_R) + '" x2="0" y2="' +
              (_BUBBLE_CY + _BUBBLE_R) + '" gradientUnits="userSpaceOnUse">' +
              '<stop offset="0%" class="ctx-bubble-grad-top"/>' +
              '<stop offset="100%" class="ctx-bubble-grad-bot"/>' +
            '</linearGradient>' +
          '</defs>' +
          '<circle class="ctx-bubble-bg" cx="' + _BUBBLE_CX + '" cy="' + _BUBBLE_CY +
                  '" r="' + _BUBBLE_R + '"/>' +
          '<g clip-path="url(#' + clipId + ')">' +
            '<g class="ctx-bubble-wave-group">' +
              '<path class="ctx-bubble-wave" d="' + _WAVE_PATH +
                    '" fill="url(#' + gradId + ')"/>' +
              '<path class="ctx-bubble-meniscus" d="' + _WAVE_PATH + '"/>' +
            '</g>' +
            /* Inner glass shadow — a faint dark ring just inside the
             * rim, clipped to the bubble interior, gives the vessel a
             * sense of physical thickness ("the wall of the glass").
             * Radius is a hair smaller than the rim so the stroke
             * sits cleanly INSIDE the vessel silhouette. */
            '<circle class="ctx-bubble-inner-shadow" cx="' + _BUBBLE_CX + '" cy="' + _BUBBLE_CY +
                    '" r="' + (_BUBBLE_R - 1) + '"/>' +
          '</g>' +
          /* Glass rim — drawn AFTER the clipped liquid so it sits on
           * top of the surface.  Stroke-only, no fill. */
          '<circle class="ctx-bubble-rim" cx="' + _BUBBLE_CX + '" cy="' + _BUBBLE_CY +
                  '" r="' + (_BUBBLE_R - 0.5) + '"/>' +
          /* Upper-left shine — a static arc that sells "glass vessel"
           * without any motion.  Drawn after the rim so it can
           * brighten the top of the bubble. */
          '<path class="ctx-bubble-shine" d="M 7 11 A 9 9 0 0 1 13 6"/>' +
        '</svg>' +
        '<div class="ctx-bar-counter" aria-hidden="true"></div>' +
      '</div>' +
      '<div class="ctx-bar-pct">0%</div>' +
      '<div class="ctx-bar-cap">—</div>';
    _state = {
      el,
      /* `bubbleNode` is the host of the whole bubble — we set
       * --ctx-arc-pct on it so CSS transitions handle the level rise.
       * `waveNode` is the <g> that translates the liquid body
       * vertically; we write its transform directly (an SVG attribute)
       * since SVG `transform` doesn't yet support `@property`-style
       * smooth transitions universally. */
      bubbleNode:  el.querySelector('.ctx-bar-bubble'),
      waveNode:    el.querySelector('.ctx-bubble-wave-group'),
      counterNode: el.querySelector('.ctx-bar-counter'),
      pctNode:     el.querySelector('.ctx-bar-pct'),
      capNode:     el.querySelector('.ctx-bar-cap'),
      lastZone: '',
      lastPctText: '',
      lastCapText: '',
      lastTitle: '',
      lastFillPct: -1,
      lastCounterText: '',
      lastClickable: null,
      compactions: [],
    };
    /* Click on chip body → open a small popover with two actions:
     *   • 立即压缩 (manual /compact) — always available when a conv is open;
     *   • 查看压缩历史 (open the viewer) — only when this conv has snapshots.
     * The in-gauge per-tick clicks are gone with the bubble redesign. */
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      if (typeof activeConvId === 'undefined' || !activeConvId) return;
      _toggleCtxPopover(el);
    });
    return _state;
  }

  /* ── "立即压缩" popover ─────────────────────────────────────────────
   * A tiny anchored menu on the chip. Kept dead-simple: rebuilt each open,
   * removed on any outside click / action. No ambient state. */
  function _closeCtxPopover() {
    const ex = document.getElementById('ctxBarPopover');
    if (ex) ex.remove();
    document.removeEventListener('click', _closeCtxPopover, true);
  }

  function _tt(key, fallback, vars) {
    return (typeof t === 'function') ? t(key, vars) : fallback;
  }

  function _convHasLiveTask(conv) {
    return Boolean(conv && typeof convIsBusy === 'function' && convIsBusy(conv));
  }

  function _toggleCtxPopover(anchorEl) {
    if (document.getElementById('ctxBarPopover')) { _closeCtxPopover(); return; }
    const conv = _activeConv();
    if (!conv) return;
    const hasHistory = !!(_state && _state.compactions.length);
    const busy = _convHasLiveTask(conv);

    const pop = document.createElement('div');
    pop.id = 'ctxBarPopover';
    pop.className = 'ctx-bar-popover';
    const _scissors = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><path d="M8.12 8.12 12 12"/><path d="M20 4 8.12 15.88"/><circle cx="6" cy="18" r="3"/><path d="M14.8 14.8 20 20"/></svg>';
    const _history = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg>';
    const compactLabel = busy
      ? _tt('compactNow.busy', '任务进行中，无法压缩')
      : _tt('compactNow.action', '立即压缩上下文');
    pop.innerHTML =
      `<button type="button" class="ctx-pop-item ctx-pop-compact"${busy ? ' disabled' : ''}>` +
        `<span class="ctx-pop-icon">${_scissors}</span>` +
        `<span>${compactLabel}</span></button>` +
      (hasHistory
        ? `<button type="button" class="ctx-pop-item ctx-pop-history">` +
            `<span class="ctx-pop-icon">${_history}</span>` +
            `<span>${_tt('compactNow.viewHistory', '查看压缩历史')}</span></button>`
        : '');
    document.body.appendChild(pop);

    /* Anchor to the chip (fixed, so it survives the chat scroll). */
    const r = anchorEl.getBoundingClientRect();
    pop.style.position = 'fixed';
    pop.style.left = Math.round(r.left) + 'px';
    pop.style.top = Math.round(r.bottom + 6) + 'px';

    const compactBtn = pop.querySelector('.ctx-pop-compact');
    if (compactBtn && !busy) {
      compactBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        _closeCtxPopover();
        _runManualCompaction(conv.id);
      });
    }
    const histBtn = pop.querySelector('.ctx-pop-history');
    if (histBtn) {
      histBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        _closeCtxPopover();
        if (typeof runtimeScope.openCompactionViewer === 'function') {
          runtimeScope.openCompactionViewer(conv.id);
        }
      });
    }
    /* Close on any outside click (capture so it fires before re-open). */
    setTimeout(() => document.addEventListener('click', _closeCtxPopover, true), 0);
  }

  /* Toggle the chip's in-progress state. The spinner is a pure-CSS overlay
   * keyed on `data-compacting` (see styles.css) — an SVG ring, NOT a toast.
   * This REPLACES the old optimistic "正在压缩…" info toast that stacked with
   * the terminal toast and read as self-contradictory ("compacting" +
   * "nothing to compact" at once). Progress lives on the chip; the toast fires
   * exactly ONCE, at the terminal state. */
  function _setCompacting(on) {
    const s = _state;
    if (!s || !s.el) return;
    if (on) s.el.dataset.compacting = '1';
    else delete s.el.dataset.compacting;
  }

  /* ── B: live-streaming summary overlay ──────────────────────────────
   * The manual /compact summary LLM call dominates the wait (~96%). To make
   * that wait FEEL responsive, the backend streams the summary on an
   * independent push channel ('compaction', convId) — summary_start →
   * summary_delta* → summary_done. This overlay is a small anchored panel that
   * "grows" the summary text as deltas arrive, then dissolves when the real
   * boundary card lands (the success closure reloads + re-renders it). Purely
   * cosmetic: if push is unavailable the POST still completes and the card
   * appears on reload exactly as before. */
  let _liveCard = null;         // { el, bodyNode, text, convId }

  function _openLiveSummaryCard(convId) {
    _closeLiveSummaryCard();
    const anchor = _state && _state.el;
    if (!anchor || typeof document === 'undefined') return;
    const el = document.createElement('div');
    el.id = 'ctxLiveSummary';
    el.className = 'ctx-live-summary';
    el.innerHTML =
      '<div class="ctx-live-summary-head">' +
        _tt('compactNow.streaming', '正在生成压缩摘要…') + '</div>' +
      '<div class="ctx-live-summary-body"></div>';
    document.body.appendChild(el);
    const r = anchor.getBoundingClientRect();
    el.style.position = 'fixed';
    el.style.left = Math.round(r.left) + 'px';
    el.style.top = Math.round(r.bottom + 6) + 'px';
    _liveCard = { el, bodyNode: el.querySelector('.ctx-live-summary-body'),
                  text: '', convId };
  }

  function _appendLiveSummary(convId, chunk) {
    if (!_liveCard || _liveCard.convId !== convId || !chunk) return;
    _liveCard.text += chunk;
    if (_liveCard.bodyNode) {
      // Plain text (textContent) — never render partial markdown as HTML mid
      // stream. The settled boundary card renders markdown after reload.
      _liveCard.bodyNode.textContent = _liveCard.text;
      _liveCard.bodyNode.scrollTop = _liveCard.bodyNode.scrollHeight;
    }
  }

  function _closeLiveSummaryCard() {
    if (_liveCard && _liveCard.el) {
      try { _liveCard.el.remove(); } catch (e) { /* detached already */ }
    }
    _liveCard = null;
  }

  /* Push handler for the ('compaction', convId) channel. Grows the overlay on
   * each delta; the terminal states (done/failed) are handled by the POST
   * closure (which reloads the real card), so here they only stop the stream. */
  function _onCompactionPush(convId, ev) {
    if (!ev || (ev.taskId && ev.taskId !== convId)) return;
    if (ev.type === 'summary_start') {
      _openLiveSummaryCard(convId);
    } else if (ev.type === 'summary_delta') {
      _appendLiveSummary(convId, ev.text || '');
    }
    // summary_done / summary_failed: the POST closure takes over (authoritative
    // hydration + Surface commit or terminal toast), so it owns teardown.
  }

  /* ── The full success CLOSURE: POST → hydrate Turns → re-render the
   *    boundary card → drop the gauge (scheme B) → flash the badge. Without
   *    the reload+re-render the user would click and see nothing change
   *    (the durable Turn graph was rewritten server-side).
   *
   * TOAST CONTRACT (do not regress): AT MOST ONE toast per invocation, and it
   * is always a TERMINAL toast (success / nothing / failed / busy). There is
   * NO optimistic "starting" toast — the chip spinner conveys in-progress. So
   * "正在压缩" and "无需压缩" can never appear together. */
  async function _runManualCompaction(convId) {
    if (!convId) return;
    const conv = getConvById(convId);
    if (conv && _convHasLiveTask(conv)) {
      if (typeof showToast === 'function') {
        showToast(_tt('compactNow.busy', '任务进行中，无法压缩'), 'warning');
      }
      return;
    }

    /* Resolve to a single {msg, level} terminal outcome, THEN toast once. */
    let outcome = null;
    _setCompacting(true);
    /* B: subscribe to the live-summary push channel BEFORE the POST so we don't
     * miss the summary_start the backend emits as soon as summarization begins.
     * Best-effort — pushSubscribe may be unavailable (older bundle / no socket);
     * the POST + reload closure works regardless. */
    let _pushHandler = null;
    if (typeof pushSubscribe === 'function') {
      _pushHandler = (ev) => _onCompactionPush(convId, ev);
      try { pushSubscribe('compaction', convId, _pushHandler); }
      catch (e) { console.debug('[compactNow] push subscribe failed', e); _pushHandler = null; }
    }
    try {
      let res;
      try {
        res = await Api.compactions.compactNow(convId, {});
      } catch (err) {
        const code = err && err.code;
        if (code === 'task_active') {
          outcome = { msg: _tt('compactNow.busy', '任务进行中，无法压缩'), level: 'warning' };
        } else if (code === 'nothing_to_compact') {
          // Not a failure — a neutral, informative note (context already lean).
          outcome = { msg: _tt('compactNow.nothing', '上下文已很精简，暂无需压缩'), level: 'info' };
        } else {
          outcome = { msg: _tt('compactNow.failed', '压缩失败'), level: 'error' };
        }
        return;
      }
      if (!res || !res.ok) {
        // Non-throwing client: map the same codes so behavior is identical.
        const code = res && res.code;
        if (code === 'nothing_to_compact') {
          outcome = { msg: _tt('compactNow.nothing', '上下文已很精简，暂无需压缩'), level: 'info' };
        } else if (code === 'task_active') {
          outcome = { msg: _tt('compactNow.busy', '任务进行中，无法压缩'), level: 'warning' };
        } else {
          outcome = { msg: _tt('compactNow.failed', '压缩失败'), level: 'error' };
        }
        return;
      }

      /* ── CLOSURE: make the rewrite visible ── */
      try {
        if (typeof ConvCache !== 'undefined' && ConvCache && ConvCache.remove) {
          await ConvCache.remove(convId);       // drop stale IDB copy
        }
      } catch (e) { console.warn('[compactNow] cache invalidate failed', e); }
      const c = getConvById(convId);
      if (c) {
        c._turnSnapshotRequired = true;                     // force a server re-fetch
        try {
          if (typeof hydrateConversationRuntime === 'function') {
            await hydrateConversationRuntime(convId);
          }
        } catch (e) { console.warn('[compactNow] reload failed', e); }
        if (convId === activeConvId) {
          runtimeScope.requestAuthoritativeConversationRender(
            c.id, { forceScroll: false },
          ); // re-render → boundary card shows
        }
      }
      updateContextBar();                        // gauge scheme B drops the level
      if (res.archiveId != null && typeof flashGaugeForArchive === 'function') {
        flashGaugeForArchive(res.archiveId);     // badge +1 flash
      }
      const tb = res.tokensBefore || 0, ta = res.tokensAfter || 0;
      const fmt = (n) => n >= 1000 ? (n / 1000).toFixed(0) + 'k' : String(n);
      const tokenPrefix = res.tokenCountKind === 'estimated' ? '≈' : '';
      outcome = {
        msg: _tt('compactNow.done', '已压缩：{before} → {after} tokens（-{pct}%）',
                 { before: tokenPrefix + fmt(tb), after: tokenPrefix + fmt(ta),
                   pct: res.reductionPct != null ? res.reductionPct : 0 }),
        level: 'success',
      };
    } finally {
      _setCompacting(false);
      // B: tear down the live-summary stream — the settled boundary card from
      // the authoritative Surface commit, or the terminal toast, now stands in.
      if (_pushHandler && typeof pushUnsubscribe === 'function') {
        try { pushUnsubscribe('compaction', convId, _pushHandler); }
        catch (e) { console.debug('[compactNow] push unsubscribe failed', e); }
      }
      _closeLiveSummaryCard();
      if (outcome && typeof showToast === 'function') {
        showToast(outcome.msg, outcome.level);
      }
    }
  }

  let _scheduled = false;

  function updateContextBar() {
    if (_scheduled) return;
    _scheduled = true;
    requestAnimationFrame(() => {
      _scheduled = false;
      _doUpdate();
    });
  }

  function _doUpdate() {
    const s = _ensureBar();
    if (!s) return;
    const conv  = _activeConv();
    const model = _activeModel(conv);
    const summary = contextUsageSummary();
    const limit = summary.limit;
    const used = summary.used;
    const pct = summary.pctRaw;
    const pctRounded = summary.pct;
    const zone = summary.zone;

    /* ── Surgical writes: only touch the DOM when a value actually changed. */
    if (s.lastZone !== zone) {
      s.el.dataset.zone = zone;
      s.lastZone = zone;
    }
    if (s.lastFillPct !== pct) {
      /* Translate the wave group so the AREA of the liquid below the
       * surface equals `pct` of the bubble's total area.  A naive
       * linear height mapping is wrong for a circle: the disc is
       * fattest at its equator, so equal *heights* don't enclose
       * equal *areas*.  At pct=0.25 the linear mapping shows ~19.6%
       * of the area; at pct=0.75 it shows ~80.4%.  We want the
       * visible occupancy to MATCH the context-window occupancy.
       *
       * Math: for a unit circle, the area below a horizontal chord
       * at height h above the bottom (h ∈ [0, 2]) is
       *   A(h) = acos(1 - h)  -  (1 - h) * sqrt(2h - h²)
       * Solving A(h) = pct·π for h has no closed form, so we run a
       * tiny Newton iteration (converges in ~5 steps to <1e-4).
       *   A'(h) = 2 · sqrt(2h - h²)   (the chord width at height h)
       *
       * Edge cases pct=0 and pct=1 are handled before the iteration
       * to avoid divide-by-zero at the poles where A'(h)=0.
       */
      const targetArea = pct * Math.PI;
      let hUnit;                            // height in [0,2] on the unit circle
      if (pct <= 0)      hUnit = 0;
      else if (pct >= 1) hUnit = 2;
      else {
        hUnit = pct * 2;                    // linear seed
        for (let i = 0; i < 8; i++) {
          const oneMinusH = 1 - hUnit;
          const chord = 2 * Math.sqrt(Math.max(1e-12, 2 * hUnit - hUnit * hUnit));
          const area  = Math.acos(oneMinusH) - oneMinusH * (chord / 2);
          const delta = (area - targetArea) / chord;
          hUnit -= delta;
          if (Math.abs(delta) < 1e-5) break;
        }
        hUnit = Math.max(0, Math.min(2, hUnit));
      }
      /* Convert unit-circle height into bubble-pixel surface Y.
       * pct=0 → liquidTopY at the bubble base; pct=1 → at the top.
       * The ±_WAVE_AMP offsets keep the meniscus curve fully inside
       * the bubble at the extremes (the meniscus path's edges sit
       * `_WAVE_AMP` ABOVE its mid-line so without these offsets the
       * lifted edges would peek into / out of the vessel). */
      const liquidTopUnclamped = (_BUBBLE_CY + _BUBBLE_R) - hUnit * _BUBBLE_R;
      const liquidTopY = Math.max(_BUBBLE_CY - _BUBBLE_R - _WAVE_AMP,
                          Math.min(_BUBBLE_CY + _BUBBLE_R + _WAVE_AMP,
                                   liquidTopUnclamped));
      s.waveNode.setAttribute('transform', `translate(0 ${liquidTopY.toFixed(2)})`);
      /* Mirror the percentage onto the bubble host as a custom
       * property — this is how the host-level data-zone transitions
       * propagate (e.g. tinting the rim or shine) without adding a
       * second JS write. */
      s.bubbleNode.style.setProperty('--ctx-arc-pct', (pct * 100).toFixed(2) + '%');
      s.lastFillPct = pct;
    }

    const pctText = summary.known && used > 0 ? pctRounded + '%' : '—';
    if (s.lastPctText !== pctText) {
      s.pctNode.textContent = pctText;
      s.lastPctText = pctText;
    }
    const capText = limit == null ? '—' : _formatTokens(limit) + (summary.exact ? '' : ' ≈');
    if (s.lastCapText !== capText) {
      s.capNode.textContent = capText;
      s.lastCapText = capText;
    }

    /* ── Compactions → counter + clickability ───────────────────── */
    const comps = _collectCompactions(conv);
    const compactionCount = _countCompactions(conv);
    s.compactions = comps;
    const counterText = compactionCount > 0 ? String(compactionCount) : '';
    if (s.lastCounterText !== counterText) {
      s.counterNode.textContent = counterText;
      s.counterNode.classList.toggle('has-count', compactionCount > 0);
      s.lastCounterText = counterText;
    }
    const clickable = compactionCount > 0;
    if (s.lastClickable !== clickable) {
      s.el.classList.toggle('is-clickable', clickable);
      s.lastClickable = clickable;
    }

    const modelLabel = model || 'unknown';
    const tip = limit == null
      ? modelLabel + '\nContext window: unknown' +
        (compactionCount ? '\n' + compactionCount + ' compaction' +
                         (compactionCount === 1 ? '' : 's') +
                         ' in this conversation · click to inspect' : '')
      : used > 0
      ? modelLabel + '\n' + _formatTokens(used) + ' / ' + capText +
        ' tokens (' + pctRounded + '%) — last round prompt' +
        (zone === 'hot'  ? '\nApproaching auto-compact threshold' : '') +
        (zone === 'crit' ? '\nCritical — compaction imminent'      : '') +
        (compactionCount ? '\n' + compactionCount + ' compaction' +
                           (compactionCount === 1 ? '' : 's') +
                           ' in this conversation · click to inspect' : '')
      : modelLabel + '\nContext window: ' + capText + ' tokens' +
        (compactionCount ? '\n' + compactionCount + ' compaction' +
                           (compactionCount === 1 ? '' : 's') +
                           ' in this conversation · click to inspect' : '');
    if (s.lastTitle !== tip) {
      s.el.title = tip;
      s.lastTitle = tip;
    }
  }

  /* Discrete one-shot cue: when a brand-new compaction lands, briefly
   * highlight the bubble's counter badge.  Public API kept stable so
   * existing SSE hooks (ui.js: compaction_done handler) keep working
   * without any caller-side changes — only the visual effect moved
   * from a per-tick flash to a counter-badge flash. */
  function flashGaugeForArchive(archiveId) {
    if (archiveId == null) return;
    updateContextBar();
    requestAnimationFrame(() => {
      if (!_state || !_state.counterNode) return;
      _state.counterNode.classList.add('is-landing');
      setTimeout(() => {
        if (_state && _state.counterNode) {
          _state.counterNode.classList.remove('is-landing');
        }
      }, 1400);
    });
  }

  /* Read-only usage snapshot for surfaces that can't show the bubble
   * (e.g. the mobile "···" sheet, where the chip is display:none below
   * 900 px).  Returns the SAME numbers the bubble renders — single source
   * of truth for the percentage/zone so the two never drift. */
  function contextUsageSummary() {
    const conv  = _activeConv();
    const model = _activeModel(conv);
    const profile = _resolveContextProfile(model, _activeProvider(conv));
    const limit = profile.window;
    const used = _lastUsageTokens(conv);
    const known = Number(limit) > 0;
    const pctRaw = known ? Math.min(1, used / limit) : 0;
    let zone = known ? 'ok' : 'unknown';
    if (known && pctRaw >= _CRIT_THRESHOLD)          zone = 'crit';
    else if (known && pctRaw >= _compactThreshold(limit)) zone = 'hot';
    else if (known && pctRaw >= 0.60)                     zone = 'warn';
    return {
      used, limit, zone, pctRaw,
      pct: known ? Math.round(pctRaw * 100) : null,
      source: profile.source || 'unknown',
      exact: profile.exact === true,
      known,
      hasUsage: used > 0,
      compactions: _countCompactions(conv),
    };
  }

  runtimeScope.updateContextBar = updateContextBar;
  runtimeScope.flashGaugeForArchive = flashGaugeForArchive;
  runtimeScope._resolveContextLimit = _resolveContextLimit;
  runtimeScope._resolveContextProfile = _resolveContextProfile;
  runtimeScope.runManualCompaction = _runManualCompaction;
  runtimeScope.contextUsageSummary = contextUsageSummary;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', updateContextBar, { once: true });
  } else {
    updateContextBar();
  }
})();

/* ═══════════════════════════════════════════════════════════════════
   context-bar.js — Context Health Bar (the "liquid bubble")

   A small status chip on the left flank of `.chat-wrapper` that shows
   the current conversation's prompt-token usage as a percentage of
   the active model's context window.

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
      every desktop width (>900 px); below 900 px it's hidden.

   Public API:
     window.updateContextBar()        — recompute + repaint (rAF-coalesced).
     window.flashGaugeForArchive(id)  — discrete cue when a new compaction
                                        lands; pulses the matching tick.
     window._resolveContextLimit()    — exported for debug.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* ── Context-window policy.  The authoritative limits + compaction
   *    thresholds live in Python (lib/tasks_pkg/compaction) and are served
   *    via /api/v1/server-config → cached on `window._contextPolicy` by
   *    _loadServerConfigAndPopulate().  We DO NOT re-derive them here — a
   *    hard-coded copy silently drifted (the JS threshold was stuck at 0.82
   *    vs the real 0.90, and the regex limit table missed learned overrides).
   *
   *    Policy shape (see build_context_policy()):
   *      { default_limit, output_reserve, compaction_reserve,
   *        summary_trigger_ratio, per_model: { <model_id>: <limit> } }
   *
   *    `_FALLBACK_LIMIT` is used only before the config has loaded (or if the
   *    fetch failed) — it must never be the primary source of truth. */
  const _FALLBACK_LIMIT = 200_000;
  const _CRIT_THRESHOLD = 0.95;

  function _policy() {
    return (typeof window !== 'undefined' && window._contextPolicy) || null;
  }

  /* Fraction of the FULL context window at which the server force-compacts.
   * The server triggers on `usable * summary_trigger_ratio` where
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
    return Math.min(0.99, (usable * ratio) / limit);
  }

  function _resolveContextLimit(modelId) {
    const p = _policy();
    if (p) {
      if (modelId && p.per_model && p.per_model[modelId] > 0) return p.per_model[modelId];
      if (p.default_limit > 0) return p.default_limit;
    }
    return _FALLBACK_LIMIT;
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
   *   1. `msg._liveLastRoundUsage.tokensIn` — populated by the
   *      `round_usage` SSE event (lib/tasks_pkg/llm_fallback.py
   *      :_emit_round_usage) the moment each LLM round lands.  This is
   *      what makes the gauge move between tool rounds, BEFORE the
   *      `done` event arrives.  Server-side already normalized
   *      Anthropic-vs-OpenAI cache convention into a single number.
   *   2. `msg.apiRounds[-1].usage` — populated by the `done` event at
   *      the end of a turn.  Used for completed messages on reload
   *      (when `_liveLastRoundUsage` is gone) and at the moment a
   *      task transitions from streaming to done.
   *   3. `msg.usage / N` — fallback for legacy conversations that
   *      pre-date `apiRounds`.  `msg.usage` is the per-message
   *      ACCUMULATED sum across all rounds (see
   *      lib/tasks_pkg/llm_fallback.py:144), so it's tokens BILLED,
   *      not tokens currently in the prompt — divide by round count
   *      to get a usable average.
   *
   * Walks assistant messages newest-first and returns the first usable
   * reading. Skips zero readings so an in-flight bubble with no usage
   * yet doesn't shadow the previous turn's number. */
  function _lastUsageTokens(conv) {
    if (!conv || !Array.isArray(conv.messages)) return 0;
    for (let i = conv.messages.length - 1; i >= 0; i--) {
      const m = conv.messages[i];
      if (!m || m.role !== 'assistant') continue;
      // 1. Live in-flight reading from the round_usage SSE event.
      if (m._liveLastRoundUsage && m._liveLastRoundUsage.tokensIn > 0) {
        return m._liveLastRoundUsage.tokensIn;
      }
      // 2. Final apiRounds breakdown (post-done event).
      if (Array.isArray(m.apiRounds) && m.apiRounds.length) {
        for (let j = m.apiRounds.length - 1; j >= 0; j--) {
          const t = _promptTokensFromUsage(m.apiRounds[j] && m.apiRounds[j].usage);
          if (t > 0) return t;
        }
      }
      // 3. Legacy fallback — accumulated usage divided by round count.
      if (m.usage) {
        const t = _promptTokensFromUsage(m.usage);
        const n = (Array.isArray(m.apiRounds) && m.apiRounds.length) || 1;
        if (t > 0) return n > 1 ? Math.round(t / n) : t;
      }
    }
    return 0;
  }

  /* Walk every assistant message in the active conv and collect compaction
   * markers (the same `_compactions[]` array that ui.js:1402 renders as
   * inline chips).  Returned in chronological order (DB id ascending). */
  function _collectCompactions(conv) {
    if (!conv || !Array.isArray(conv.messages)) return [];
    const out = [];
    for (const m of conv.messages) {
      if (!m || m.role !== 'assistant' || !Array.isArray(m._compactions)) continue;
      for (const c of m._compactions) {
        if (!c || c.archiveId == null) continue;
        out.push(c);
      }
    }
    out.sort((a, b) => (a.archiveId || 0) - (b.archiveId || 0));
    return out;
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
    const wrapper = document.querySelector('.chat-wrapper');
    if (!wrapper) return null;
    const el = existing || document.createElement('aside');
    if (!existing) {
      el.id = 'contextHealthBar';
      el.className = 'ctx-health-bar';
      el.setAttribute('aria-label', 'Context usage');
      wrapper.appendChild(el);
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
    /* Click on chip body → open viewer for active conv, letting the
     * drawer auto-select the most recent archive.  In-gauge per-tick
     * clicks are gone with the bubble redesign — the counter badge +
     * viewer drawer now own the timeline. */
    el.addEventListener('click', () => {
      if (!_state || !_state.compactions.length) return;
      if (typeof activeConvId === 'undefined' || !activeConvId) return;
      if (typeof window.openCompactionViewer === 'function') {
        window.openCompactionViewer(activeConvId);
      }
    });
    return _state;
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
    const limit = _resolveContextLimit(model);
    const used  = _lastUsageTokens(conv);
    const pct   = limit > 0 ? Math.min(1, used / limit) : 0;
    const pctRounded = Math.round(pct * 100);

    const compactThreshold = _compactThreshold(limit);
    let zone = 'ok';
    if      (pct >= _CRIT_THRESHOLD)   zone = 'crit';
    else if (pct >= compactThreshold)  zone = 'hot';
    else if (pct >= 0.60)              zone = 'warn';

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

    const pctText = used > 0 ? pctRounded + '%' : '—';
    if (s.lastPctText !== pctText) {
      s.pctNode.textContent = pctText;
      s.lastPctText = pctText;
    }
    const capText = _formatTokens(limit);
    if (s.lastCapText !== capText) {
      s.capNode.textContent = capText;
      s.lastCapText = capText;
    }

    /* ── Compactions → counter + clickability ───────────────────── */
    const comps = _collectCompactions(conv);
    s.compactions = comps;
    const counterText = comps.length > 0 ? String(comps.length) : '';
    if (s.lastCounterText !== counterText) {
      s.counterNode.textContent = counterText;
      s.counterNode.classList.toggle('has-count', comps.length > 0);
      s.lastCounterText = counterText;
    }
    const clickable = comps.length > 0;
    if (s.lastClickable !== clickable) {
      s.el.classList.toggle('is-clickable', clickable);
      s.lastClickable = clickable;
    }

    const modelLabel = model || 'unknown';
    const tip = used > 0
      ? modelLabel + '\n' + _formatTokens(used) + ' / ' + capText +
        ' tokens (' + pctRounded + '%) — last round prompt' +
        (zone === 'hot'  ? '\nApproaching auto-compact threshold' : '') +
        (zone === 'crit' ? '\nCritical — compaction imminent'      : '') +
        (comps.length    ? '\n' + comps.length + ' compaction' +
                           (comps.length === 1 ? '' : 's') +
                           ' in this conversation · click to inspect' : '')
      : modelLabel + '\nContext window: ' + capText + ' tokens' +
        (comps.length    ? '\n' + comps.length + ' compaction' +
                           (comps.length === 1 ? '' : 's') +
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

  window.updateContextBar = updateContextBar;
  window.flashGaugeForArchive = flashGaugeForArchive;
  window._resolveContextLimit = _resolveContextLimit;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', updateContextBar, { once: true });
  } else {
    updateContextBar();
  }
})();

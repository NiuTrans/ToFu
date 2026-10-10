/* ===== migrated source: context-bar.js ===== */
/* Context policy/accounting and manual compaction adapter. Presentation is owned by context-meter.ts. */
(function () {
  'use strict';

  /* ── Context-window policy.  The authoritative limits + compaction
   *    thresholds live in Python (lib/tasks_pkg/compaction) and are served
   *    via /api/v4/server-config → cached on `runtimeScope._contextPolicy` by
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

  let _state = null;

  // One manual request per page. It may finish offscreen, but only its own
  // conversation can display progress; repeated clicks never duplicate POSTs.
  let _manualCompactionId = null;

  function _tt(key, fallback, vars) {
    return (typeof t === 'function') ? t(key, vars) : fallback;
  }

  function _convHasLiveTask(conv) {
    return Boolean(conv && typeof convIsBusy === 'function' && convIsBusy(conv));
  }

  function _meterSnapshot() {
    const conv = _activeConv();
    return Object.assign({}, contextUsageSummary(), {
      convId: conv ? conv.id : null,
      busy: _convHasLiveTask(conv),

      compacting: !!_manualCompactionId && _manualCompactionId === conv?.id,
      compactionPending: !!_manualCompactionId,
      hasHistory: _collectCompactions(conv).length > 0,
    });
  }

  function _ensureBar() {
    if (_state && _state.el.isConnected) return _state;
    const host = document.getElementById('convStatusStrip');
    if (!host) return null;
    if (_state) _state.destroy();
    _state = createContextMeter({
      host,
      translate: _tt,
      read: _meterSnapshot,
      onCompact: _runManualCompaction,
      onHistory: (convId) => runtimeScope.openCompactionViewer?.(convId),
    });
    return _state;
  }

  function _openLiveSummaryCard(convId) { _state?.openLiveSummary(convId); }
  function _appendLiveSummary(convId, chunk) { _state?.appendLiveSummary(convId, chunk); }
  function _closeLiveSummaryCard() { _state?.closeLiveSummary(); }

  /* Push handler for the ('compaction', convId) channel. Grows the overlay on
   * each delta; the terminal states (done/failed) are handled by the POST
   * closure (which reloads the real card), so here they only stop the stream. */
  function _onCompactionPush(convId, ev) {
    if (!ev || (ev.taskId && ev.taskId !== convId)
        || _manualCompactionId !== convId || _activeConv()?.id !== convId) return;
    if (ev.type === 'summary_start') {
      _openLiveSummaryCard(convId);
    } else if (ev.type === 'summary_delta') {
      _appendLiveSummary(convId, ev.text || '');
    }
    // summary_done / summary_failed: the POST closure takes over (authoritative
    // hydration + Surface commit or terminal toast), so it owns teardown.
  }

  /* ── The full success CLOSURE: POST → hydrate Turns → re-render the
   *    boundary card → refresh context usage (scheme B). Without
   *    the reload+re-render the user would click and see nothing change
   *    (the durable Turn graph was rewritten server-side).
   *
   * TOAST CONTRACT (do not regress): AT MOST ONE toast per invocation, and it
   * is always a TERMINAL toast (success / nothing / failed / busy). There is
   * NO optimistic "starting" toast — the context control conveys in-progress. So
   * "正在压缩" and "无需压缩" can never appear together. */
  async function _runManualCompaction(convId) {
    if (!convId || _manualCompactionId) return;
    const conv = getConvById(convId);
    if (conv && _convHasLiveTask(conv)) {
      if (typeof showToast === 'function') {
        showToast(_tt('compactNow.busy', '任务进行中，无法压缩'), 'warning');
      }
      return;
    }

    /* Resolve to a single {msg, level} terminal outcome, THEN toast once. */
    let outcome = null;
    _manualCompactionId = convId;
    _doUpdate();
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
        flashGaugeForArchive(res.archiveId);     // refresh compaction count
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
      _manualCompactionId = null;
      _doUpdate();
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
    if (s) s.update(_meterSnapshot());
  }

  /* Existing event entry point retained; compactions update quietly. */
  function flashGaugeForArchive(archiveId) {
    if (archiveId != null) updateContextBar();
  }

  /* Read-only accounting snapshot shared with other conversation surfaces. */
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

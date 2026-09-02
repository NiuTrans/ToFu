/* ===== migrated source: project-brain-status.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   project-brain-status.js — the "Status" tab of the Project Brain
   (Pillar #7, the human↔brain status lane).

   Where Charter/Board/Activity/Team are agent-facing blackboards, THIS tab is
   the human's window into "where is the project / are we drifting from the
   charter". It renders:
     • the LATEST synthesized status narrative (where we are + an explicit
       alignment-to-north-star read), returned INSTANTLY from cache while a
       fresh synthesis warms in the background (never a blocking wait);
     • a read-only Q&A composer (ask the project a specific question → a
       synthesized answer; WRITES NOTHING);
     • the append-only HISTORY trail of prior snapshots (timestamp + narrative
       + expandable pillar-state evidence) so the human can see HOW the project
       got here, not just where it is now.

   Data path (no raw fetch — §3.2.0):
     • latest+history ← Api.project.brainStatus(path, {force})  (returns cached
                        {latest, history, maxSeq, refreshing} instantly; a
                        background warm sets refreshing=true → poll history)
     • poll           ← Api.project.brainStatusHistory(path)  (read-only, no LLM)
     • ask            ← Api.project.brainStatusAsk(path, question)  (read-only)

   HUMAN-FACING ONLY: this status memory is never injected into sibling agent
   prompts (enforced backend-side). Exposed on runtimeScope.ProjectBrainStatus for
   project-brain.js to drive on open. Bundled by Vite's module graph
   (the migrated module graph). Icons are inline SVG via Icon() (§3.4 — no emoji). Strings
   live under projectBrain.* in i18n.js.
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  function _t(key, fallback) {
    try { return (typeof t === 'function') ? t(key) : fallback; }
    catch (_e) { return fallback; }
  }

  function _esc(s) {
    if (typeof escapeHtml === 'function') return escapeHtml(String(s == null ? '' : s));
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function _statusBodyEl() { return document.getElementById('projectBrainStatusBody'); }

  // Bounded background-warm poll: when the backend returns refreshing=true it is
  // synthesizing a fresh snapshot off-thread; we poll the read-only history
  // endpoint a few times to swap in the new narrative without blocking the tab.
  var _pollTimer = null;
  var _pollPath = '';
  var _POLL_MS = 2500;
  var _POLL_MAX = 8;  // ~20s ceiling; then give up quietly (no LLM held open)

  function _stopPoll() {
    if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
    _pollPath = '';
  }

  /** Compact relative time from an epoch-ms `ts` (localized). '' when absent. */
  function _relTime(ts) {
    var n = Number(ts) || 0;
    if (!n) return '';
    var diff = Date.now() - n;
    if (diff < 0) diff = 0;
    var mins = Math.floor(diff / 60000);
    if (mins < 1) return _t('projectBrain.justNow', 'just now');
    if (mins < 60) return _t('projectBrain.minutesAgo', '{n}m ago').replace('{n}', mins);
    var hrs = Math.floor(mins / 60);
    if (hrs < 24) return _t('projectBrain.hoursAgo', '{n}h ago').replace('{n}', hrs);
    var days = Math.floor(hrs / 24);
    return _t('projectBrain.daysAgo', '{n}d ago').replace('{n}', days);
  }

  /** Human-readable label for a snapshot trigger. */
  function _triggerLabel(trig) {
    var map = {
      epic_completed: _t('projectBrain.statusTrigEpic', 'epic completed'),
      decision_committed: _t('projectBrain.statusTrigDecision', 'decision committed'),
      blocked: _t('projectBrain.statusTrigBlocked', 'work blocked'),
      on_open: _t('projectBrain.statusTrigOpen', 'refreshed'),
      manual: _t('projectBrain.statusTrigManual', 'manual'),
      follow_up: _t('projectBrain.statusTrigFollowUp', 'follow-up'),
    };
    return map[trig] || trig || '';
  }

  /**
   * Build the compact pillar-state evidence chip block for a snapshot. Pure.
   * Shows the coarse counts (open/in-flight/done/blocked epics, pending
   * decisions, charter version, active peers) the narrative was generated FROM,
   * so the human can see the numbers behind the prose.
   */
  function buildEvidence(ps) {
    ps = ps || {};
    var wrap = document.createElement('div');
    wrap.className = 'pb-status-evidence';
    var bits = [
      ['statusEvOpen', 'open', ps.epicsOpen || 0],
      ['statusEvInflight', 'in-flight', ps.epicsClaimed || 0],
      ['statusEvDone', 'done', ps.epicsDone || 0],
      ['statusEvBlocked', 'blocked', ps.epicsBlocked || 0],
      ['statusEvPending', 'decisions pending', ps.pendingDecisions || 0],
      ['statusEvPeers', 'active peers', ps.activePeers || 0],
    ];
    for (var i = 0; i < bits.length; i++) {
      var chip = document.createElement('span');
      chip.className = 'pb-status-ev-chip';
      var label = _t('projectBrain.' + bits[i][0], bits[i][1]);
      chip.textContent = bits[i][2] + ' ' + label;
      wrap.appendChild(chip);
    }
    if (ps.charterExists) {
      var v = document.createElement('span');
      v.className = 'pb-status-ev-chip pb-status-ev-charter';
      v.textContent = _t('projectBrain.statusEvCharter', 'charter v') + (ps.charterVersion || 0);
      wrap.appendChild(v);
    }
    return wrap;
  }

  /** Build one history-trail snapshot row (collapsed evidence). Pure. */
  function buildHistoryRow(snap) {
    snap = snap || {};
    var row = document.createElement('div');
    row.className = 'pb-status-hist-row';

    var head = document.createElement('div');
    head.className = 'pb-status-hist-head';
    var when = document.createElement('span');
    when.className = 'pb-status-hist-when';
    when.textContent = _relTime(snap.ts);
    head.appendChild(when);
    var trig = document.createElement('span');
    trig.className = 'pb-status-hist-trigger';
    trig.textContent = _triggerLabel(snap.trigger);
    head.appendChild(trig);
    row.appendChild(head);

    var body = document.createElement('div');
    body.className = 'pb-status-hist-narrative';
    body.textContent = snap.narrative || '';
    if (snap.narrative) body.setAttribute('data-pb-src', snap.narrative);
    row.appendChild(body);

    row.appendChild(buildEvidence(snap.pillar_state));
    return row;
  }

  /**
   * Render the Status tab from a brainStatus verdict ({latest, history}).
   * Pure renderer — no fetching here. Builds: the headline narrative + its
   * evidence, the ask composer, and the history trail.
   */
  function renderStatus(data) {
    var el = _statusBodyEl();
    if (!el) return;
    data = data || {};
    var latest = data.latest || null;
    var history = data.history || [];

    var refreshing = !!data.refreshing;
    var frag = document.createDocumentFragment();

    // ── Section header: title + live "updating" pill + manual refresh ──
    frag.appendChild(_buildStatusHeader(refreshing));

    // ── Latest narrative (the headline "where are we") ──
    var latestWrap = document.createElement('div');
    latestWrap.className = 'pb-status-latest';
    if (latest && latest.narrative) {
      var narr = document.createElement('div');
      narr.className = 'pb-status-narrative';
      narr.textContent = latest.narrative;
      narr.setAttribute('data-pb-src', latest.narrative);
      latestWrap.appendChild(narr);
      var meta = document.createElement('div');
      meta.className = 'pb-status-latest-meta';
      var rel = _relTime(latest.ts);
      meta.textContent = (rel ? rel + ' · ' : '') + _triggerLabel(latest.trigger);
      latestWrap.appendChild(meta);
      latestWrap.appendChild(buildEvidence(latest.pillar_state));
    } else if (refreshing) {
      // First-ever open with no snapshot yet, synthesis warming in the
      // background — show a shimmer skeleton (NOT a blocking spinner).
      latestWrap.appendChild(_buildSkeleton());
    } else {
      var empty = document.createElement('div');
      empty.className = 'pb-status-empty';
      empty.textContent = _t('projectBrain.statusEmpty',
        'No status yet — synthesized once the project has a charter or board activity.');
      latestWrap.appendChild(empty);
    }
    frag.appendChild(latestWrap);

    // ── Ask composer (read-only synthesis Q&A) ──
    frag.appendChild(_buildAskComposer());

    // ── Watch lane (the human's standing "things I care about" list) ──
    var watchWrap = document.createElement('div');
    watchWrap.className = 'pb-watch';
    watchWrap.id = 'pbWatchSection';
    frag.appendChild(watchWrap);

    // ── History trail ──
    if (history.length) {
      var histWrap = document.createElement('div');
      histWrap.className = 'pb-status-history';
      var head = document.createElement('div');
      head.className = 'pb-status-history-head';
      head.textContent = _t('projectBrain.statusHistory', 'Status history');
      histWrap.appendChild(head);
      // Skip the first row when it IS the latest already shown on top.
      var start = (latest && history.length && history[0].seq === latest.seq) ? 1 : 0;
      for (var i = start; i < history.length; i++) {
        histWrap.appendChild(buildHistoryRow(history[i]));
      }
      frag.appendChild(histWrap);
    }

    el.innerHTML = '';
    el.appendChild(frag);

    if (typeof ProjectBrainI18n !== 'undefined' && ProjectBrainI18n &&
        typeof ProjectBrainI18n.apply === 'function') {
      try { ProjectBrainI18n.apply(el); } catch (_e) { /* best-effort */ }
    }

    // Populate the watch lane (its own fetch; re-addresses open items on read).
    _refreshWatch(_displayedStatusPath());
  }

  /**
   * Build the Status section header: the title, a live "Updating…" pill shown
   * while a background synthesis is warming, and a manual Refresh button that
   * forces a fresh snapshot (refresh=1). Pure.
   */
  function _buildStatusHeader(refreshing) {
    var head = document.createElement('div');
    head.className = 'pb-status-header';

    var title = document.createElement('div');
    title.className = 'pb-status-title';
    title.textContent = _t('projectBrain.statusTitle', 'Where the project is');
    head.appendChild(title);

    if (refreshing) {
      var pill = document.createElement('span');
      pill.className = 'pb-status-updating';
      var dot = document.createElement('span');
      dot.className = 'pb-status-updating-dot';
      pill.appendChild(dot);
      var lbl = document.createElement('span');
      lbl.textContent = _t('projectBrain.statusUpdating', 'Updating…');
      pill.appendChild(lbl);
      head.appendChild(pill);
    }

    var refreshBtn = document.createElement('button');
    refreshBtn.type = 'button';
    refreshBtn.className = 'pb-status-refresh';
    refreshBtn.title = _t('projectBrain.statusRefresh', 'Refresh status');
    refreshBtn.setAttribute('aria-label', _t('projectBrain.statusRefresh', 'Refresh status'));
    refreshBtn.disabled = refreshing;
    if (typeof Icon === 'function') {
      refreshBtn.innerHTML = Icon('refresh', 15);
    } else {
      refreshBtn.textContent = '\u21bb';
    }
    refreshBtn.addEventListener('click', function () {
      refreshStatus(_displayedStatusPath(), { force: true });
    });
    head.appendChild(refreshBtn);
    return head;
  }

  /** Build a shimmer skeleton for the first-open synthesis wait. Pure. */
  function _buildSkeleton() {
    var sk = document.createElement('div');
    sk.className = 'pb-status-skeleton';
    for (var i = 0; i < 3; i++) {
      var line = document.createElement('div');
      line.className = 'pb-status-skeleton-line';
      sk.appendChild(line);
    }
    return sk;
  }

  /**
   * Build the read-only Q&A composer: a textarea + Ask button. On ask it calls
   * Api.project.brainStatusAsk (WRITES NOTHING) and renders the answer inline.
   */
  function _buildAskComposer() {
    var wrap = document.createElement('div');
    wrap.className = 'pb-status-ask';

    var head = document.createElement('div');
    head.className = 'pb-status-ask-head';
    head.textContent = _t('projectBrain.statusAskHead', 'Ask the project');
    wrap.appendChild(head);

    var ta = document.createElement('textarea');
    ta.className = 'pb-status-ask-input';
    ta.rows = 2;
    ta.placeholder = _t('projectBrain.statusAskPlaceholder',
      'e.g. Are we drifting from the north star? What is blocked?');
    wrap.appendChild(ta);

    var actions = document.createElement('div');
    actions.className = 'pb-status-ask-actions';
    var status = document.createElement('span');
    status.className = 'pb-status-ask-status';
    actions.appendChild(status);
    var askBtn = document.createElement('button');
    askBtn.type = 'button';
    askBtn.className = 'pb-status-ask-btn';
    askBtn.textContent = _t('projectBrain.statusAsk', 'Ask');
    actions.appendChild(askBtn);
    wrap.appendChild(actions);

    var answer = document.createElement('div');
    answer.className = 'pb-status-ask-answer';
    answer.hidden = true;
    wrap.appendChild(answer);

    function _ask() {
      var q = (ta.value || '').trim();
      if (!q) return;
      var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
      var path = _displayedStatusPath();
      if (!api || typeof api.brainStatusAsk !== 'function' || !path) {
        status.className = 'pb-status-ask-status pb-status-ask-status-err';
        status.textContent = _t('projectBrain.statusAskFailed', 'Could not ask');
        return;
      }
      askBtn.disabled = true;
      status.className = 'pb-status-ask-status';
      status.textContent = _t('projectBrain.statusAsking', 'Thinking…');
      answer.hidden = true;
      Promise.resolve(api.brainStatusAsk(path, q))
        .then(function (res) {
          status.textContent = '';
          var text = (res && res.answer) || '';
          answer.textContent = text;
          if (text) answer.setAttribute('data-pb-src', text);
          answer.hidden = !text;
          if (typeof ProjectBrainI18n !== 'undefined' && ProjectBrainI18n &&
              typeof ProjectBrainI18n.apply === 'function') {
            try { ProjectBrainI18n.apply(answer); } catch (_e) {}
          }
        })
        .catch(function (e) {
          status.className = 'pb-status-ask-status pb-status-ask-status-err';
          status.textContent = _t('projectBrain.statusAskFailed', 'Could not ask');
          if (typeof console !== 'undefined') console.warn('[ProjectBrain] status ask failed', e);
        })
        .then(function () { askBtn.disabled = false; });
    }
    askBtn.addEventListener('click', _ask);
    ta.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); _ask(); }
    });
    return wrap;
  }

  /**
   * Fetch + render the Status tab for `path`. Backend-authoritative and
   * NON-BLOCKING: the server returns the cached snapshot instantly and warms a
   * fresh one in the background (refreshing=true), so the tab is never held on
   * "Synthesizing…". A genuine first-open (no snapshot yet) shows a shimmer
   * skeleton, not a full-screen spinner. When the backend is warming, we poll
   * the read-only history endpoint a few times to swap in the fresh narrative.
   *
   * @param {string} path   project path
   * @param {object} [opts] { force: boolean } — force a background re-synth.
   * Best-effort: a failed fetch renders the empty state, never throws.
   */
  function refreshStatus(path, opts) {
    _stopPoll();
    var force = !!(opts && opts.force);
    var el = _statusBodyEl();
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    if (!el || !api || !path || typeof api.brainStatus !== 'function') {
      if (el) {
        el.innerHTML = '<div class="pb-status-empty">' +
          _esc(_t('projectBrain.statusEmpty', 'No status yet')) + '</div>';
      }
      return;
    }
    // Only show a skeleton on a genuinely EMPTY tab (nothing rendered yet or a
    // forced refresh from empty). If prior content is on screen, leave it — the
    // header's "Updating…" pill signals the background warm instead of blanking.
    var hasContent = !!el.querySelector('.pb-status-narrative, .pb-status-latest');
    if (!hasContent) {
      el.innerHTML = '';
      var sk = document.createElement('div');
      sk.className = 'pb-status-latest';
      sk.appendChild(_buildSkeleton());
      el.appendChild(sk);
    }
    Promise.resolve(api.brainStatus(path, { force: force })).then(function (data) {
      if (path !== _displayedStatusPath()) return;  // project switched mid-flight
      data = data || {};
      renderStatus(data);
      if (data.refreshing) _startPoll(path, (data.maxSeq | 0));
    }).catch(function (e) {
      if (path !== _displayedStatusPath()) return;
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] status load failed', e);
      renderStatus({});
    });
  }

  /**
   * Poll the read-only history endpoint (NO synthesis) until a snapshot newer
   * than `baseSeq` appears (the background warm landed) or the bounded attempt
   * budget is exhausted. Swaps in the fresh view without blocking the tab.
   */
  function _startPoll(path, baseSeq) {
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    if (!api || typeof api.brainStatusHistory !== 'function') return;
    _pollPath = path;
    var attempts = 0;
    function _tick() {
      if (_pollPath !== path || path !== _displayedStatusPath()) { _stopPoll(); return; }
      attempts++;
      Promise.resolve(api.brainStatusHistory(path)).then(function (hist) {
        if (_pollPath !== path || path !== _displayedStatusPath()) return;
        hist = hist || {};
        var snaps = hist.snapshots || [];
        var maxSeq = hist.maxSeq | 0;
        if (maxSeq > baseSeq && snaps.length) {
          // Fresh snapshot landed — render it, clear the "updating" state.
          _stopPoll();
          renderStatus({ latest: snaps[0], history: snaps,
                         maxSeq: maxSeq, refreshing: false });
          return;
        }
        if (attempts >= _POLL_MAX) {
          // Give up quietly: drop the pill so the header stops implying work.
          _stopPoll();
          var hdr = document.querySelector('.pb-status-updating');
          if (hdr && hdr.parentNode) hdr.parentNode.removeChild(hdr);
          var rb = document.querySelector('.pb-status-refresh');
          if (rb) rb.disabled = false;
          return;
        }
        _pollTimer = setTimeout(_tick, _POLL_MS);
      }).catch(function (e) {
        if (typeof console !== 'undefined') console.warn('[ProjectBrain] status poll failed', e);
        _stopPoll();
      });
    }
    _pollTimer = setTimeout(_tick, _POLL_MS);
  }

  /** Resolve the displayed project path — same accessor the peers tab uses. */
  function _displayedStatusPath() {
    try {
      if (typeof runtimeScope.ProjectBrain !== 'undefined' &&
          runtimeScope.ProjectBrain._state && runtimeScope.ProjectBrain._state.path) {
        return runtimeScope.ProjectBrain._state.path;
      }
      var conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
      var p = '';
      if (conv) {
        p = (typeof _getConvProjectPath === 'function')
          ? _getConvProjectPath(conv) : (conv.projectPath || '');
      }
      if (!p && typeof projectState !== 'undefined' && projectState &&
          projectState.active) {
        p = projectState.path || '';
      }
      return String(p || '').replace(/[/\\]+$/, '');
    } catch (_e) { return ''; }
  }

  /* ════════════════════════════════════════════════════════════════
     Watch lane — the human's standing "things I care about" list. The human
     authors items (concern|question|goal); the brain addresses each on a
     recurring basis with an append-only response trail.

     A GOAL is injected into every sibling conversation's prompt just by
     existing (backend render_goals_injection_block) — no promote step, no
     charter copy. concern/question are human-facing only, and their one bridge
     to agents is the explicit "Promote to charter" action.
     ════════════════════════════════════════════════════════════════ */

  var _WATCH_KINDS = ['concern', 'question', 'goal'];

  function _kindLabel(kind) {
    var m = {
      concern: _t('projectBrain.watchKindConcern', 'Concern'),
      question: _t('projectBrain.watchKindQuestion', 'Question'),
      goal: _t('projectBrain.watchKindGoal', 'Goal'),
    };
    return m[kind] || kind || '';
  }

  /** Build the "add a watch item" composer (kind select + text + add). Pure. */
  function _buildWatchComposer() {
    var wrap = document.createElement('div');
    wrap.className = 'pb-watch-add';

    var sel = document.createElement('select');
    sel.className = 'pb-watch-kind';
    for (var i = 0; i < _WATCH_KINDS.length; i++) {
      var opt = document.createElement('option');
      opt.value = _WATCH_KINDS[i];
      opt.textContent = _kindLabel(_WATCH_KINDS[i]);
      sel.appendChild(opt);
    }
    wrap.appendChild(sel);

    var ta = document.createElement('textarea');
    ta.className = 'pb-watch-input';
    ta.rows = 2;
    ta.placeholder = _t('projectBrain.watchPlaceholder',
      'Something you want the brain to keep an eye on…');
    wrap.appendChild(ta);

    var actions = document.createElement('div');
    actions.className = 'pb-watch-add-actions';
    var status = document.createElement('span');
    status.className = 'pb-watch-add-status';
    actions.appendChild(status);
    var addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'pb-watch-add-btn';
    addBtn.textContent = _t('projectBrain.watchAdd', 'Add');
    actions.appendChild(addBtn);
    wrap.appendChild(actions);

    function _add() {
      var text = (ta.value || '').trim();
      if (!text) return;
      var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
      var path = _displayedStatusPath();
      if (!api || typeof api.brainWatchAdd !== 'function' || !path) return;
      addBtn.disabled = true;
      status.className = 'pb-watch-add-status';
      status.textContent = _t('projectBrain.watchAdding', 'Adding…');
      Promise.resolve(api.brainWatchAdd(path, sel.value, text, _watchConvId()))
        .then(function () { ta.value = ''; status.textContent = ''; _refreshWatch(path, true); })
        .catch(function (e) {
          status.className = 'pb-watch-add-status pb-status-ask-status-err';
          status.textContent = _t('projectBrain.watchAddFailed', 'Could not add');
          if (typeof console !== 'undefined') console.warn('[ProjectBrain] watch add failed', e);
        })
        .then(function () { addBtn.disabled = false; });
    }
    addBtn.addEventListener('click', _add);
    return wrap;
  }

  /** Build one watch-item card: header (kind + status), text, latest response,
   *  expandable response history, and the action row. Pure.
   *  `ctx` carries the list-level charter snapshot ({charterVersion}) used by
   *  the concern/question promote path. A GOAL ignores it entirely. */
  function buildWatchItem(item, ctx) {
    item = item || {};
    var responses = item.responses || [];
    var card = document.createElement('div');
    card.className = 'pb-watch-item pb-watch-item-' + (item.kind || 'concern')
      + (item.status === 'resolved' ? ' pb-watch-resolved' : '');
    card.setAttribute('data-item-id', item.item_id || '');

    var head = document.createElement('div');
    head.className = 'pb-watch-item-head';
    var kindBadge = document.createElement('span');
    kindBadge.className = 'pb-watch-kind-badge pb-watch-kind-badge-' + (item.kind || 'concern');
    kindBadge.textContent = _kindLabel(item.kind);
    head.appendChild(kindBadge);
    // ── A GOAL reports a FACT, not a promotion: an open goal is in every
    //    sibling conversation's prompt because it exists (server-side
    //    render_goals_injection_block). There is no button and no state to
    //    reconcile — that whole machinery existed only while a goal was COPIED
    //    into the charter, and one copy needs none of it.
    //    concern/question keep the computed charter verdict, which is never read
    //    from item.promotedAudit: that stored boolean records a promotion once
    //    happened and stays true after the decision is FIFO-evicted, so
    //    rendering it would promise something already untrue.
    if (item.kind === 'goal') {
      if (item.injected) {
        var inj = document.createElement('span');
        inj.className = 'pb-watch-promoted';
        inj.textContent = _t('projectBrain.watchGoalLive', 'every conversation reads this');
        inj.title = _t('projectBrain.watchGoalLiveHint',
          'Open goals are included in every conversation of this project. Resolve it to withdraw it.');
        head.appendChild(inj);
      }
    } else if ((item.promotionState || 'none') === 'active') {
      var pr = document.createElement('span');
      pr.className = 'pb-watch-promoted';
      pr.textContent = _t('projectBrain.watchPromoted', 'in charter');
      head.appendChild(pr);
    }
    if (item.status === 'resolved') {
      var rs = document.createElement('span');
      rs.className = 'pb-watch-status-badge';
      rs.textContent = _t('projectBrain.watchResolved', 'resolved');
      head.appendChild(rs);
    }
    card.appendChild(head);

    var text = document.createElement('div');
    text.className = 'pb-watch-item-text';
    text.textContent = item.text || '';
    if (item.text) text.setAttribute('data-pb-src', item.text);
    card.appendChild(text);

    // Latest brain response (or a "not addressed yet" hint).
    var latest = responses.length ? responses[0] : null;
    var resp = document.createElement('div');
    resp.className = 'pb-watch-response';
    if (latest && latest.response) {
      _fillResponseBody(resp, latest);
      var rmeta = document.createElement('div');
      rmeta.className = 'pb-watch-response-meta';
      var rel = _relTime(latest.ts);
      rmeta.textContent = (rel ? rel + ' · ' : '') + _triggerLabel(latest.trigger);
      resp.appendChild(rmeta);
      resp.appendChild(_buildRespActions(item, latest));
    } else {
      resp.className = 'pb-watch-response pb-watch-response-pending';
      resp.textContent = _t('projectBrain.watchNotAddressed', 'Not addressed yet');
    }
    card.appendChild(resp);

    // Response history trail (older responses, collapsed).
    if (responses.length > 1) {
      var trail = document.createElement('div');
      trail.className = 'pb-watch-trail';
      for (var i = 1; i < responses.length; i++) {
        var row = document.createElement('div');
        row.className = 'pb-watch-trail-row';
        var when = document.createElement('span');
        when.className = 'pb-watch-trail-when';
        when.textContent = _relTime(responses[i].ts);
        row.appendChild(when);
        var body = document.createElement('div');
        body.className = 'pb-watch-trail-text';
        _fillResponseBody(body, responses[i]);
        body.appendChild(_buildRespActions(item, responses[i]));
        row.appendChild(body);
        trail.appendChild(row);
      }
      card.appendChild(trail);
    }

    card.appendChild(_buildWatchActions(item, ctx));
    return card;
  }

  /* ── Per-response interaction (Increment 2 slice) ─────────────────────
     Every brain response carries two doors:
       • Follow up — an inline composer; the brain's answer lands in the SAME
         append-only trail as a trigger='follow_up' entry (human↔brain lane);
       • Request fix — an inline epic-draft editor pre-filled from the
         response; submitting posts through the EXISTING human-gated
         board-post path, so the brain dispatches the fix to a conversation
         the human can open from the Board tab. No new write channel. */

  /**
   * Fill a response container: the human's follow-up question (when the entry
   * is a follow_up answer) as a labelled line, then the response text in its
   * own div. The text living in a child div (not host.textContent) is what
   * lets the question line and the per-response actions coexist with it.
   */
  function _fillResponseBody(host, entry) {
    var ps = (entry && entry.pillar_state) || {};
    if (ps.followUpQuestion) {
      var q = document.createElement('div');
      q.className = 'pb-watch-followup-q';
      q.textContent = _t('projectBrain.watchFollowUpQ', 'Follow-up') + ' · ' +
        ps.followUpQuestion;
      q.setAttribute('data-pb-src', ps.followUpQuestion);
      host.appendChild(q);
    }
    var body = document.createElement('div');
    body.className = 'pb-watch-response-text';
    body.textContent = (entry && entry.response) || '';
    if (entry && entry.response) body.setAttribute('data-pb-src', entry.response);
    host.appendChild(body);
  }

  /** The two per-response doors + the slot an inline editor opens into. */
  function _buildRespActions(item, entry) {
    var wrap = document.createElement('div');
    wrap.className = 'pb-watch-resp-tools';
    var btns = document.createElement('div');
    btns.className = 'pb-watch-resp-actions';
    var fu = document.createElement('button');
    fu.type = 'button';
    fu.className = 'pb-watch-resp-act pb-watch-resp-followup';
    fu.textContent = _t('projectBrain.watchFollowUp', 'Follow up');
    fu.addEventListener('click', function () {
      _toggleRespEditor(wrap, 'followup', function () {
        return _buildFollowUpEditor(item, entry);
      });
    });
    btns.appendChild(fu);
    var fx = document.createElement('button');
    fx.type = 'button';
    fx.className = 'pb-watch-resp-act pb-watch-resp-fix';
    fx.textContent = _t('projectBrain.watchRequestFix', 'Request fix');
    fx.addEventListener('click', function () {
      _toggleRespEditor(wrap, 'fix', function () {
        return _buildFixEditor(item, entry);
      });
    });
    btns.appendChild(fx);
    wrap.appendChild(btns);
    return wrap;
  }

  /** Toggle an inline editor under a response (one at a time; same-kind
   *  re-click closes). */
  function _toggleRespEditor(wrap, kind, build) {
    var existing = wrap.querySelector('.pb-watch-resp-editor');
    var sameOpen = existing &&
      existing.getAttribute('data-editor-kind') === kind;
    if (existing) existing.parentNode.removeChild(existing);
    if (sameOpen) return;
    var ed = build();
    ed.setAttribute('data-editor-kind', kind);
    wrap.appendChild(ed);
    var input = ed.querySelector('.pb-status-ask-input');
    if (input && input.focus) input.focus();
  }

  function _respEditorShell() {
    var ed = document.createElement('div');
    ed.className = 'pb-watch-resp-editor';
    var actions = document.createElement('div');
    actions.className = 'pb-status-ask-actions';
    var status = document.createElement('span');
    status.className = 'pb-status-ask-status';
    actions.appendChild(status);
    var cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'pb-watch-resp-act';
    cancel.textContent = _t('projectBrain.blockNoteCancel', 'Cancel');
    cancel.addEventListener('click', function () {
      if (ed.parentNode) ed.parentNode.removeChild(ed);
    });
    return { ed: ed, actions: actions, status: status, cancel: cancel };
  }

  /** The follow-up composer: question → brainWatchFollowUp → trail refresh. */
  function _buildFollowUpEditor(item, entry) {
    var sh = _respEditorShell();
    var ta = document.createElement('textarea');
    ta.className = 'pb-status-ask-input';
    ta.rows = 2;
    ta.placeholder = _t('projectBrain.watchFollowUpPlaceholder',
      'Ask a follow-up about this answer…');
    sh.ed.appendChild(ta);
    var send = document.createElement('button');
    send.type = 'button';
    send.className = 'pb-status-ask-btn';
    send.textContent = _t('projectBrain.watchFollowUpSend', 'Send');
    sh.actions.appendChild(send);
    sh.actions.appendChild(sh.cancel);
    sh.ed.appendChild(sh.actions);

    function submit() {
      var q = (ta.value || '').trim();
      if (!q) { if (ta.focus) ta.focus(); return; }
      var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
      if (!api || typeof api.brainWatchFollowUp !== 'function') return;
      send.disabled = true;
      Promise.resolve(api.brainWatchFollowUp(item.item_id || '', q,
                                             entry && entry.seq))
        .then(function () { _refreshWatch(_displayedStatusPath(), false); })
        .catch(function (e) {
          sh.status.textContent = _t('projectBrain.watchFollowUpFailed',
            'Follow-up failed');
          send.disabled = false;
          if (typeof console !== 'undefined') {
            console.warn('[ProjectBrain] follow-up failed', e);
          }
        });
    }
    send.addEventListener('click', submit);
    ta.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault(); submit();
      }
      if (e.key === 'Escape' && sh.ed.parentNode) {
        sh.ed.parentNode.removeChild(sh.ed);
      }
    });
    return sh.ed;
  }

  /** Draft the epic body from a response: the FULL diagnosis, whitespace-
   *  normalized. The board's title field carries up to _TITLE_MAX_CHARS
   *  (2000) by design (multi-sentence design descriptions), so the first
   *  sentence is NOT a summary — in a status diagnosis it is usually the
   *  praise paragraph and the actual problem lives in later sentences.
   *  Cap mirrors the backend limit; the human edits before posting. */
  var _FIX_PREFILL_MAX = 2000;
  function _draftFixTitle(entry) {
    var t = ((entry && entry.response) || '').replace(/\s+/g, ' ').trim();
    return t.length > _FIX_PREFILL_MAX
      ? t.slice(0, _FIX_PREFILL_MAX).trim() + '…' : t;
  }

  /** The request-fix editor: pre-filled with the FULL response → the
   *  EXISTING human-gated board-post (created_by_conv = displayed conv →
   *  dispatch target). Multi-line so the whole diagnosis stays visible and
   *  editable before posting. */
  function _buildFixEditor(item, entry) {
    var sh = _respEditorShell();
    var input = document.createElement('textarea');
    input.className = 'pb-status-ask-input pb-watch-fix-title';
    input.rows = 4;
    input.value = _draftFixTitle(entry);
    sh.ed.appendChild(input);
    var send = document.createElement('button');
    send.type = 'button';
    send.className = 'pb-status-ask-btn';
    send.textContent = _t('projectBrain.watchFixSend', 'Post to board');
    sh.actions.appendChild(send);
    sh.actions.appendChild(sh.cancel);
    sh.ed.appendChild(sh.actions);

    function submit() {
      var title = (input.value || '').trim();
      if (!title) { if (input.focus) input.focus(); return; }
      var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
      var path = _displayedStatusPath();
      var convId = _watchConvId();
      if (!api || typeof api.boardPost !== 'function' || !path || !convId) {
        sh.status.textContent = _t('projectBrain.watchFixFailed',
          'Could not post');
        return;
      }
      send.disabled = true;
      Promise.resolve(api.boardPost(path, { title: title, convId: convId }))
        .then(function () {
          input.disabled = true;
          sh.status.textContent = _t('projectBrain.watchFixPosted',
            'Posted to the board');
        })
        .catch(function (e) {
          sh.status.textContent = _t('projectBrain.watchFixFailed',
            'Could not post');
          send.disabled = false;
          if (typeof console !== 'undefined') {
            console.warn('[ProjectBrain] request-fix post failed', e);
          }
        });
    }
    send.addEventListener('click', submit);
    input.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault(); submit();
      }
      if (e.key === 'Escape' && sh.ed.parentNode) {
        sh.ed.parentNode.removeChild(sh.ed);
      }
    });
    return sh.ed;
  }

  /** Action row: refresh · promote (concern/question only) · resolve/reopen · delete. */
  function _buildWatchActions(item, ctx) {
    var row = document.createElement('div');
    row.className = 'pb-watch-actions';
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    var id = item.item_id || '';

    function _btn(cls, label, fn) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'pb-watch-btn ' + cls;
      b.textContent = label;
      b.addEventListener('click', function () {
        b.disabled = true;
        Promise.resolve(fn()).then(function () {
          _refreshWatch(_displayedStatusPath(), false);
        }).catch(function (e) {
          b.disabled = false;
          if (typeof console !== 'undefined') console.warn('[ProjectBrain] watch action failed', e);
        });
      });
      return b;
    }
    if (!api) return row;

    row.appendChild(_btn('pb-watch-btn-refresh',
      _t('projectBrain.watchRefresh', 'Re-check'),
      function () { return api.brainWatchAddress(id); }));

    // A GOAL never offers a charter-writing button: it is already in every
    // prompt, and the charter is a separate human-owned surface. Only
    // concern/question can be promoted into it, and only while unresolved and
    // not already there.
    if (item.kind !== 'goal' && item.status !== 'resolved' &&
        (item.promotionState || 'none') !== 'active') {
      row.appendChild(_btn('pb-watch-btn-promote',
        _t('projectBrain.watchPromote', 'Promote to charter'),
        function () { return api.brainWatchPromote(id, _watchConvId()); }));
    }
    if (item.status === 'resolved') {
      row.appendChild(_btn('pb-watch-btn-reopen',
        _t('projectBrain.watchReopen', 'Reopen'),
        function () { return api.brainWatchUpdate(id, 'reopen'); }));
    } else {
      row.appendChild(_btn('pb-watch-btn-resolve',
        _t('projectBrain.watchResolveBtn', 'Resolve'),
        function () { return api.brainWatchUpdate(id, 'resolve'); }));
    }
    row.appendChild(_btn('pb-watch-btn-delete',
      _t('projectBrain.watchDelete', 'Delete'),
      function () { return api.brainWatchUpdate(id, 'delete'); }));
    return row;
  }

  /** Render the watch section from a {items:[...]} verdict. Pure. */
  function renderWatch(data) {
    var host = document.getElementById('pbWatchSection');
    if (!host) return;
    data = data || {};
    var items = data.items || [];

    var frag = document.createDocumentFragment();
    var head = document.createElement('div');
    head.className = 'pb-watch-head';
    head.textContent = _t('projectBrain.watchHead', 'Things I care about');
    frag.appendChild(head);
    frag.appendChild(_buildWatchComposer());

    if (items.length) {
      var list = document.createElement('div');
      list.className = 'pb-watch-list';
      // The charter version the concern/question verdicts were computed
      // against. charterContent is deliberately NOT threaded through any more:
      // it existed only to render the goal replacement preview, and a goal no
      // longer touches the charter.
      var ctx = { charterVersion: data.charterVersion | 0 };
      for (var i = 0; i < items.length; i++) list.appendChild(buildWatchItem(items[i], ctx));
      frag.appendChild(list);
    } else {
      var empty = document.createElement('div');
      empty.className = 'pb-watch-empty';
      empty.textContent = _t('projectBrain.watchEmpty',
        'Add a concern, question, or goal and the brain will keep an eye on it.');
      frag.appendChild(empty);
    }

    host.innerHTML = '';
    host.appendChild(frag);
    if (typeof ProjectBrainI18n !== 'undefined' && ProjectBrainI18n &&
        typeof ProjectBrainI18n.apply === 'function') {
      try { ProjectBrainI18n.apply(host); } catch (_e) { /* best-effort */ }
    }
  }

  /**
   * Fetch + render the watch lane. `refresh` re-addresses open items server-side
   * on read (fresh-on-open cadence). Best-effort — never throws.
   */
  function _refreshWatch(path, refresh) {
    var host = document.getElementById('pbWatchSection');
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    if (!host || !api || !path || typeof api.brainWatchList !== 'function') return;
    Promise.resolve(api.brainWatchList(path, !!refresh)).then(function (data) {
      if (path !== _displayedStatusPath()) return;
      renderWatch(data || {});
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] watch load failed', e);
    });
  }

  /** The displayed conv id (attribution for add/promote). Best-effort ''. */
  function _watchConvId() {
    try {
      if (typeof runtimeScope.ProjectBrain !== 'undefined' && runtimeScope.ProjectBrain &&
          typeof runtimeScope.ProjectBrain._boardConvId === 'function') {
        return runtimeScope.ProjectBrain._boardConvId() || '';
      }
      if (typeof activeConvId !== 'undefined' && activeConvId) return activeConvId;
    } catch (_e) { /* noop */ }
    return '';
  }

  runtimeScope.ProjectBrainStatus = {
    renderStatus: renderStatus,
    refreshStatus: refreshStatus,
    buildHistoryRow: buildHistoryRow,
    buildEvidence: buildEvidence,
    buildWatchItem: buildWatchItem,
    renderWatch: renderWatch,
    _triggerLabel: _triggerLabel,
    _kindLabel: _kindLabel,
  };
})();


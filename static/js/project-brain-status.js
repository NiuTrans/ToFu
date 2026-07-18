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
   prompts (enforced backend-side). Exposed on window.ProjectBrainStatus for
   project-brain.js to drive on open. Bundled by lib/js_bundler.py
   (_BUNDLE_FILES). Icons are inline SVG via Icon() (§3.4 — no emoji). Strings
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
      if (typeof window.ProjectBrain !== 'undefined' &&
          window.ProjectBrain._state && window.ProjectBrain._state.path) {
        return window.ProjectBrain._state.path;
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
     recurring basis with an append-only response trail. Human-facing only;
     the ONLY bridge to sibling agents is "promote to charter goal".
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
   *  expandable response history, and the action row. Pure. */
  function buildWatchItem(item) {
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
    if (item.promoted) {
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
      resp.textContent = latest.response;
      resp.setAttribute('data-pb-src', latest.response);
      var rmeta = document.createElement('div');
      rmeta.className = 'pb-watch-response-meta';
      var rel = _relTime(latest.ts);
      rmeta.textContent = (rel ? rel + ' · ' : '') + _triggerLabel(latest.trigger);
      resp.appendChild(rmeta);
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
        var body = document.createElement('span');
        body.className = 'pb-watch-trail-text';
        body.textContent = responses[i].response || '';
        if (responses[i].response) body.setAttribute('data-pb-src', responses[i].response);
        row.appendChild(body);
        trail.appendChild(row);
      }
      card.appendChild(trail);
    }

    card.appendChild(_buildWatchActions(item));
    return card;
  }

  /** Action row: refresh · promote-to-charter · resolve/reopen · delete. */
  function _buildWatchActions(item) {
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

    if (item.status !== 'resolved' && !item.promoted) {
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
      for (var i = 0; i < items.length; i++) list.appendChild(buildWatchItem(items[i]));
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
      if (typeof window.ProjectBrain !== 'undefined' && window.ProjectBrain &&
          typeof window.ProjectBrain._boardConvId === 'function') {
        return window.ProjectBrain._boardConvId() || '';
      }
      if (typeof activeConvId !== 'undefined' && activeConvId) return activeConvId;
    } catch (_e) { /* noop */ }
    return '';
  }

  window.ProjectBrainStatus = {
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

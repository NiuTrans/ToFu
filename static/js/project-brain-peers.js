/* ═══════════════════════════════════════════════════════════════════
   project-brain-peers.js — the "Team / Peers" column of the Project Brain.

   The panel's Charter / Board / Activity columns are three DATA TABLES; this
   column is the missing COHESION surface — it renders the conversations of
   this project as a ROOM: each active sibling conversation as a card (title ·
   presence dot · what it is advancing · current round/file), plus the
   peer-to-peer message exchanges (project_message / project_intervene) as an
   inline thread, so the panel reads as siblings working TOGETHER.

   Data path (no raw fetch — §3.2.0):
     • roster  ← Api.project.brainPeers(path, activeConvId)  (LIVE presence ⋈
                 task ⋈ claimed-epic — the SAME claims_by_conv join the collab
                 bar uses; the caller's own conv is excluded server-side)
     • thread  ← Api.project.feed(path, 0), filtered to peer-note events
                 (payload.fromConv / payload.toConv present). The feed is the
                 single durable source of the exchanges — no second store.
     • live    ← the panel's existing project-channel push subscription
                 (_subscribePanelLive in project-brain.js) debounce-refetches
                 this column too, so it stays live like Charter/Board.

   Exposed on window.ProjectBrainPeers for project-brain.js to drive on open /
   refresh, and on window for the jsdom harness. Bundled by lib/js_bundler.py
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

  function _peersBodyEl() { return document.getElementById('projectBrainPeersBody'); }

  /** Short conv id for display (mirrors the backend's [:8] convention). */
  function _shortConv(cid) { return String(cid || '').slice(0, 8); }

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

  /**
   * Map a peer's live task status → a presence-dot state class. A peer with a
   * RUNNING task is "active" (green pulse); a present-but-idle peer is "idle"
   * (amber); anything else (shouldn't happen — the roster only lists active
   * presence) falls back to idle. Pure.
   */
  function _peerState(p) {
    if (p && p.taskStatus === 'running') return 'active';
    return 'idle';
  }

  /**
   * Localize the small known set of backend statusLabel tokens ("generating" /
   * "working" / "idle" and "editing X" / "working (phase)") so the peer card
   * reads in the UI language; unknown labels pass through verbatim. Returns
   * ''when absent. Mirrors _localizePeerStatusLabel in ui/tool_rounds.js.
   */
  function _localizeStatusLabel(sl) {
    var s = String(sl == null ? '' : sl).trim();
    if (!s) return '';
    if (s === 'generating') return _t('projectBrain.stGenerating', 'generating');
    if (s === 'working') return _t('projectBrain.stWorking', 'working');
    if (s === 'idle') return _t('projectBrain.stIdle', 'idle');
    var m;
    if ((m = s.match(/^editing\s+(.+)$/))) {
      return _t('projectBrain.peerEditing', 'editing {file}').replace('{file}', m[1]);
    }
    if ((m = s.match(/^working\s+\((.+)\)$/))) {
      return _t('projectBrain.stWorkingPhase', 'working ({phase})').replace('{phase}', m[1]);
    }
    return s;
  }

  /** Build one peer roster card. Pure + testable (returns an element). */
  function buildPeerCard(p) {
    var card = document.createElement('div');
    var isAgent = !!(p && p.agentId);
    card.className = 'pb-peer-card' + (isAgent ? ' pb-peer-agent' : '');
    card.dataset.convId = (p && p.convId) || '';

    // Presence dot (SVG-free: a styled span, state via data attr → CSS color).
    var dot = document.createElement('span');
    dot.className = 'pb-peer-dot';
    dot.dataset.state = _peerState(p);
    card.appendChild(dot);

    var body = document.createElement('div');
    body.className = 'pb-peer-body';

    // Row 1: who (title, or sub-agent label).
    var who = document.createElement('div');
    who.className = 'pb-peer-who';
    var title = (p && (p.title)) ||
      _t('projectBrain.peerUntitled', 'conversation {id}')
        .replace('{id}', _shortConv(p && p.convId));
    if (isAgent) {
      who.textContent = _t('projectBrain.peerSubAgent', 'sub-agent {id}')
        .replace('{id}', p.agentId) + ' · ' + title;
    } else {
      who.textContent = title;
    }
    body.appendChild(who);

    // Row 2: what it is DOING — advancing «epic», or the live status label.
    var doingBits = [];
    if (p && p.claimedEpic) {
      doingBits.push(_t('projectBrain.peerAdvancing', 'advancing «{epic}»')
        .replace('{epic}', p.claimedEpic));
    }
    var rawLabel = (p && p.statusLabel) || '';
    if (rawLabel) doingBits.push(_localizeStatusLabel(rawLabel));
    else if (p && p.phase) doingBits.push(p.phase);
    if (p && p.round) {
      doingBits.push(_t('projectBrain.peerRound', 'round {n}').replace('{n}', p.round));
    }
    // Only surface the current file on its own when the status label does not
    // ALREADY name it (the backend's "editing X" label already carries the
    // file — appending currentFile again produced a duplicated line).
    if (p && p.currentFile && rawLabel.indexOf(p.currentFile) === -1) {
      doingBits.push(_t('projectBrain.peerEditing', 'editing {file}')
        .replace('{file}', p.currentFile));
    }
    if (doingBits.length) {
      var doing = document.createElement('div');
      doing.className = 'pb-peer-doing';
      doing.textContent = doingBits.join(' · ');
      body.appendChild(doing);
    }

    // Row 3 (conversation peers only): a "nudge" affordance — the operator can
    // send this sibling conversation an advisory note it sees on its NEXT turn.
    // Sub-agents have no queue of their own, so they get no composer.
    var cid = (p && p.convId) || '';
    if (cid && !isAgent) {
      var ctl = document.createElement('div');
      ctl.className = 'pb-peer-controls';
      ctl.appendChild(_buildNudgeAffordance(cid));
      // A coercive STOP is offered ONLY when the peer has a RUNNING task —
      // there is nothing to abort otherwise. It is the human counterpart to
      // project_intervene(hard_abort=True), gated behind a danger-confirm.
      if (p && p.taskStatus === 'running') {
        ctl.appendChild(_buildStopAffordance(cid, title));
      }
      body.appendChild(ctl);
    }

    card.appendChild(body);

    // Click the card → open that conversation (a sub-agent has no own conv →
    // open its parent conv). Mirrors the activity/board conv-chip behaviour.
    if (cid) {
      card.classList.add('pb-peer-clickable');
      card.addEventListener('click', function (e) {
        // A click inside the nudge composer OR the stop affordance must NOT
        // navigate away (otherwise typing/confirming yanks the operator off).
        if (e.target && e.target.closest &&
            (e.target.closest('.pb-peer-nudge') || e.target.closest('.pb-peer-stop'))) return;
        if (typeof loadConversation === 'function') loadConversation(cid);
      });
    }
    return card;
  }

  /**
   * Resolve the operator's ACTING conversation (their proxy sender). Mirrors
   * the board mutations: the displayed conversation id is the human's proxy.
   * '' when none — the composer then refuses to send. Pure-ish (reads globals).
   */
  function _actingConvId() {
    try {
      return (typeof activeConvId !== 'undefined' && activeConvId) ? activeConvId : '';
    } catch (_e) { return ''; }
  }

  /**
   * Build the per-card nudge affordance: a small "Nudge" button that toggles an
   * inline composer (textarea + Send/Cancel). On send it calls
   * Api.project.brainPeerMessage (operator → toConv), surfaces rate-limit /
   * error inline, and refreshes the Team column so the new note appears in the
   * thread. `toConv` is the target sibling conversation id.
   */
  function _buildNudgeAffordance(toConv) {
    var wrap = document.createElement('div');
    wrap.className = 'pb-peer-nudge';

    var toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'pb-peer-nudge-toggle';
    toggle.innerHTML = ((typeof Icon === 'function') ? Icon('messageSquare', 12) : '') +
      '<span>' + _esc(_t('projectBrain.peerNudge', 'Nudge')) + '</span>';
    wrap.appendChild(toggle);

    var composer = document.createElement('div');
    composer.className = 'pb-peer-nudge-composer';
    composer.hidden = true;

    var ta = document.createElement('textarea');
    ta.className = 'pb-peer-nudge-input';
    ta.rows = 2;
    ta.placeholder = _t('projectBrain.peerNudgePlaceholder',
      'Send this conversation an advisory note (it sees it on its next turn)…');
    composer.appendChild(ta);

    var actions = document.createElement('div');
    actions.className = 'pb-peer-nudge-actions';
    var status = document.createElement('span');
    status.className = 'pb-peer-nudge-status';
    actions.appendChild(status);
    var cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'pb-peer-nudge-cancel';
    cancelBtn.textContent = _t('projectBrain.peerNudgeCancel', 'Cancel');
    actions.appendChild(cancelBtn);
    var sendBtn = document.createElement('button');
    sendBtn.type = 'button';
    sendBtn.className = 'pb-peer-nudge-send';
    sendBtn.textContent = _t('projectBrain.peerNudgeSend', 'Send');
    actions.appendChild(sendBtn);
    composer.appendChild(actions);
    wrap.appendChild(composer);

    function _close() {
      composer.hidden = true;
      ta.value = '';
      status.textContent = '';
      status.className = 'pb-peer-nudge-status';
    }
    toggle.addEventListener('click', function () {
      composer.hidden = !composer.hidden;
      if (!composer.hidden) { try { ta.focus(); } catch (_e) {} }
    });
    cancelBtn.addEventListener('click', _close);

    function _send() {
      var text = (ta.value || '').trim();
      if (!text) return;
      var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
      var path = _displayedPeersPath();
      var fromConv = _actingConvId();
      if (!api || typeof api.brainPeerMessage !== 'function' || !path || !fromConv) {
        status.className = 'pb-peer-nudge-status pb-peer-nudge-status-err';
        status.textContent = _t('projectBrain.peerNudgeFailed', 'Send failed');
        return;
      }
      sendBtn.disabled = true;
      status.className = 'pb-peer-nudge-status';
      status.textContent = '…';
      Promise.resolve(api.brainPeerMessage(path, fromConv, toConv, text))
        .then(function () {
          status.className = 'pb-peer-nudge-status pb-peer-nudge-status-ok';
          status.textContent = _t('projectBrain.peerNudgeSent', 'Sent');
          ta.value = '';
          // Refresh so the operator sees their note land in the thread.
          setTimeout(function () { _close(); refreshPeers(path); }, 700);
        })
        .catch(function (e) {
          var rate = e && (e.code === 'rate_limited' ||
            (e.body && e.body.error === 'rate_limited') ||
            /rate_limited/.test(String((e && e.message) || '')));
          status.className = 'pb-peer-nudge-status pb-peer-nudge-status-err';
          status.textContent = rate
            ? _t('projectBrain.peerNudgeRateLimited', 'Too many messages — try again shortly')
            : _t('projectBrain.peerNudgeFailed', 'Send failed');
          if (typeof console !== 'undefined') console.warn('[ProjectBrain] peer nudge failed', e);
        })
        .then(function () { sendBtn.disabled = false; });
    }
    sendBtn.addEventListener('click', _send);
    // Ctrl/Cmd+Enter sends (mirrors the main composer).
    ta.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); _send(); }
    });
    return wrap;
  }

  /**
   * Build the per-card STOP affordance: a small danger button that, after a
   * themed confirm, calls Api.project.brainPeerAbort (operator → toConv) to
   * hard-abort the sibling's running task(s). This is the operator counterpart
   * to project_intervene(hard_abort=True): the authenticated operator IS the
   * approval (the confirm), passed server-side as approved_by and honored by
   * the same audit gate. Aborts the TASK only, never the host. `toConv` is the
   * target sibling conversation id; `title` is its display name for the prompt.
   */
  function _buildStopAffordance(toConv, title) {
    var wrap = document.createElement('span');
    wrap.className = 'pb-peer-stop';

    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pb-peer-stop-btn';
    btn.innerHTML = ((typeof Icon === 'function') ? Icon('ban', 12) : '') +
      '<span>' + _esc(_t('projectBrain.peerStop', 'Stop')) + '</span>';
    wrap.appendChild(btn);

    var status = document.createElement('span');
    status.className = 'pb-peer-stop-status';
    wrap.appendChild(status);

    function _confirm(msg) {
      // Prefer the themed danger confirm; fall back to window.confirm so the
      // gate is NEVER bypassed even if the dialog module is unavailable.
      if (typeof showConfirm === 'function') {
        return Promise.resolve(showConfirm(msg, {
          danger: true,
          okText: _t('projectBrain.peerStopConfirmOk', 'Stop the task'),
          title: _t('projectBrain.peerStop', 'Stop'),
        }));
      }
      try { return Promise.resolve(window.confirm(msg)); }
      catch (_e) { return Promise.resolve(false); }
    }

    btn.addEventListener('click', function () {
      var who = title || _t('projectBrain.peerUntitled', 'conversation {id}')
        .replace('{id}', _shortConv(toConv));
      var msg = _t('projectBrain.peerStopConfirm',
        'Hard-abort the running task(s) of "{who}"? This stops its task only — it never touches the host process.')
        .replace('{who}', who);
      _confirm(msg).then(function (ok) {
        if (!ok) return;
        var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
        var path = _displayedPeersPath();
        var fromConv = _actingConvId();
        if (!api || typeof api.brainPeerAbort !== 'function' || !path || !fromConv) {
          status.className = 'pb-peer-stop-status pb-peer-stop-status-err';
          status.textContent = _t('projectBrain.peerStopFailed', 'Stop failed');
          return;
        }
        btn.disabled = true;
        status.className = 'pb-peer-stop-status';
        status.textContent = '…';
        Promise.resolve(api.brainPeerAbort(path, fromConv, toConv))
          .then(function () {
            status.className = 'pb-peer-stop-status pb-peer-stop-status-ok';
            status.textContent = _t('projectBrain.peerStopped', 'Stopped');
            setTimeout(function () { refreshPeers(path); }, 700);
          })
          .catch(function (e) {
            status.className = 'pb-peer-stop-status pb-peer-stop-status-err';
            status.textContent = _t('projectBrain.peerStopFailed', 'Stop failed');
            if (typeof console !== 'undefined') console.warn('[ProjectBrain] peer stop failed', e);
          })
          .then(function () { btn.disabled = false; });
      });
    });
    return wrap;
  }

  /**
   * Extract the peer-message THREAD from a feed events array. A peer exchange
   * is a `note` event whose payload carries fromConv + toConv (emitted by
   * send_peer_message / intervene_peer). Returns chronological (oldest-first)
   * view rows: {fromConv, toConv, kind, summary, ts}. Pure + testable.
   */
  function extractPeerThread(events) {
    var out = [];
    var list = (events || []).slice();
    for (var i = 0; i < list.length; i++) {
      var ev = list[i];
      if (!ev || ev.kind !== 'note') continue;
      var pl = ev.payload || {};
      if (!pl.fromConv || !pl.toConv) continue;
      out.push({
        fromConv: pl.fromConv,
        toConv: pl.toConv,
        kind: pl.kind || 'note',
        summary: ev.summary || '',
        ts: ev.ts || 0,
        seq: ev.seq || 0,
      });
    }
    out.sort(function (a, b) { return (a.seq || 0) - (b.seq || 0); });
    return out;
  }

  /** Build one peer-message thread row element. Pure. */
  function _buildThreadRow(m) {
    var row = document.createElement('div');
    var isIntervene = m.kind === 'intervention' || m.kind === 'hard_abort';
    row.className = 'pb-peer-msg' + (isIntervene ? ' pb-peer-msg-intervene' : '');

    var head = document.createElement('div');
    head.className = 'pb-peer-msg-head';
    var glyph = isIntervene ? 'alertTriangle' : 'messageSquare';
    // Wrap the from/to short ids in [data-conv-id] spans so the panel's
    // delegated hover-preview (project-brain.js) resolves each opaque id to
    // its conversation's opening question on hover.
    var route = '<span class="pb-peer-msg-cid" data-conv-id="' + _esc(m.fromConv) + '">' +
      _esc(_shortConv(m.fromConv)) + '</span> → ' +
      '<span class="pb-peer-msg-cid" data-conv-id="' + _esc(m.toConv) + '">' +
      _esc(_shortConv(m.toConv)) + '</span>';
    head.innerHTML = ((typeof Icon === 'function') ? Icon(glyph, 12) : '') +
      '<span class="pb-peer-msg-route">' + route + '</span>';
    var rel = _relTime(m.ts);
    if (rel) {
      var timeEl = document.createElement('span');
      timeEl.className = 'pb-peer-msg-time';
      timeEl.textContent = rel;
      head.appendChild(timeEl);
    }
    row.appendChild(head);

    var bodyEl = document.createElement('div');
    bodyEl.className = 'pb-peer-msg-body';
    bodyEl.textContent = m.summary;
    // The peer note is agent/human-authored free text — mark it for the
    // content-translation overlay (project-brain-i18n). The original stays in
    // the attribute; the overlay lays a translation over it, never mutating it.
    if (m.summary) bodyEl.setAttribute('data-pb-src', m.summary);
    row.appendChild(bodyEl);
    return row;
  }

  /**
   * Render the Team column from a live roster + the peer-message thread.
   * `status` is the brainPeers verdict ({peers, count}); `thread` is the
   * extractPeerThread output. Pure renderer — no fetching here.
   */
  function renderPeers(status, thread) {
    var el = _peersBodyEl();
    if (!el) return;
    status = status || {};
    var peers = status.peers || [];
    thread = thread || [];

    var parts = document.createDocumentFragment();

    // ── Roster ──
    var roster = document.createElement('div');
    roster.className = 'pb-peers-roster';
    if (!peers.length) {
      var empty = document.createElement('div');
      empty.className = 'pb-peers-empty';
      empty.textContent = _t('projectBrain.peersEmpty', 'No sibling conversations active');
      roster.appendChild(empty);
    } else {
      var head = document.createElement('div');
      head.className = 'pb-peers-roster-head';
      head.textContent = _t('projectBrain.peersHere', '{n} here now')
        .replace('{n}', peers.length);
      roster.appendChild(head);
      for (var i = 0; i < peers.length; i++) {
        roster.appendChild(buildPeerCard(peers[i]));
      }
    }
    parts.appendChild(roster);

    // ── Peer-message thread ──
    if (thread.length) {
      var threadWrap = document.createElement('div');
      threadWrap.className = 'pb-peers-thread';
      var thead = document.createElement('div');
      thead.className = 'pb-peers-thread-head';
      thead.textContent = _t('projectBrain.peerThread', 'Cross-conversation messages');
      threadWrap.appendChild(thead);
      // newest at the bottom (chat-like); cap to the last 30 to bound DOM.
      var recent = thread.slice(-30);
      for (var j = 0; j < recent.length; j++) {
        threadWrap.appendChild(_buildThreadRow(recent[j]));
      }
      parts.appendChild(threadWrap);
    }

    el.innerHTML = '';
    el.appendChild(parts);

    // Lay the content-translation overlay over the freshly-rendered thread
    // (no-op when the PB-scoped translate toggle is off / already-target).
    if (typeof ProjectBrainI18n !== 'undefined' && ProjectBrainI18n &&
        typeof ProjectBrainI18n.apply === 'function') {
      try { ProjectBrainI18n.apply(el); } catch (_e) { /* best-effort */ }
    }

    // Team tab badge = live sibling count (excludes the caller's own conv,
    // server-side). Set here so it stays in lockstep with the rendered roster.
    var badge = document.getElementById('pbTabCountPeers');
    if (badge) {
      if (peers.length > 0) {
        badge.textContent = peers.length > 99 ? '99+' : String(peers.length);
        badge.hidden = false;
      } else { badge.textContent = ''; badge.hidden = true; }
    }
  }

  /**
   * Fetch (roster + feed) and render the Team column for `path`. Backend-
   * authoritative — the roster and the thread both come from the server; this
   * never recomputes peer state client-side. Best-effort: a failed sub-fetch
   * degrades that half (empty roster / empty thread), never throws.
   */
  function refreshPeers(path) {
    var el = _peersBodyEl();
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    if (!el || !api || !path || typeof api.brainPeers !== 'function') {
      if (el) {
        el.innerHTML = '<div class="pb-peers-empty">' +
          _esc(_t('projectBrain.peersEmpty', 'No sibling conversations active')) +
          '</div>';
      }
      return;
    }
    var convId = (typeof activeConvId !== 'undefined' && activeConvId)
      ? activeConvId : '';
    var pRoster = Promise.resolve(api.brainPeers(path, convId)).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] peers roster load failed', e);
      return null;
    });
    var pFeed = (typeof api.feed === 'function')
      ? Promise.resolve(api.feed(path, 0)).catch(function () { return null; })
      : Promise.resolve(null);
    Promise.all([pRoster, pFeed]).then(function (res) {
      // Guard against a project switch mid-flight.
      if (path !== _displayedPeersPath()) return;
      var status = res[0] || { peers: [], count: 0 };
      var thread = extractPeerThread((res[1] && res[1].events) || []);
      renderPeers(status, thread);
    });
  }

  /** Resolve the displayed project path — same accessor project-brain.js uses. */
  function _displayedPeersPath() {
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

  window.ProjectBrainPeers = {
    buildPeerCard: buildPeerCard,
    extractPeerThread: extractPeerThread,
    renderPeers: renderPeers,
    refreshPeers: refreshPeers,
    _peerState: _peerState,
    _buildNudgeAffordance: _buildNudgeAffordance,
    _buildStopAffordance: _buildStopAffordance,
  };
})();

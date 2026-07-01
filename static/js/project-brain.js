/* ═══════════════════════════════════════════════════════════════════
   project-brain.js — Pillar #1 of the "project brain": the live
   cross-conversation Activity Feed UI.

   An INDEPENDENT tab (not a toggle) with three columns mirroring the
   blackboard design — Charter / Board / Activity. In Pillar #1 only the
   Activity column is live (a real-time pulse of what every sibling
   conversation of this project is doing); Charter and Board render a
   framed "coming soon" placeholder so the three-pillar shape is visible
   from day one and fills in over Pillars #2/#3.

   Data path (no raw fetch — §3.2.0):
     • backfill once via Api.project.feed(path, sinceSeq) → {events, maxSeq}
     • then live via pushSubscribe('project', projectKeyHash(path), fn)
   The push routing key is sha1(path)[:16] — the SAME algorithm the backend
   uses (lib/conversations/project_feed.project_channel_key) — so the raw
   absolute path never goes on the wire (§3.5). Backfill→live boundary is
   deduped: a live frame with seq <= the highest backfilled seq is dropped,
   and any event_id already rendered is dropped (idempotent, mirrors the SSE
   Last-Event-ID resume contract).

   Bundled by lib/js_bundler.py (_BUNDLE_FILES). All UI strings live under
   projectBrain.* in i18n.js. Icons are inline SVG via Icon() (§3.4 — no emoji).
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // kind → Icon() glyph name (NO emoji). Unknown kind falls back to the
  // generic note bubble. MUST cover the backend's frozen VALID_KINDS
  // (lib/conversations/project_feed.py) — all 10, incl. 'claimed'/'dismissed'.
  var _KIND_ICON = {
    started: 'play',
    completed: 'check',
    aborted: 'x',
    run_concluded: 'rocket',
    claimed: 'package',
    blocked: 'alertTriangle',
    decided: 'lightbulb',
    proposed_decision: 'messageSquare',
    dismissed: 'ban',
    note: 'messageCircle',
  };

  // Display order for the Activity legend — mirrors the backend VALID_KINDS
  // order so the legend is a faithful, complete key to the 10 event glyphs.
  var _KIND_ORDER = ['started', 'completed', 'aborted', 'run_concluded',
    'claimed', 'blocked', 'decided', 'proposed_decision', 'dismissed', 'note'];

  // Per-tab live state. Reset on project switch / tab close.
  var _state = {
    path: '',
    maxSeq: 0,            // highest seq rendered (backfill + live)
    seen: null,           // Set<event_id> already rendered
    unsub: null,          // push unsubscribe handle (Activity feed)
    panelUnsub: null,     // push unsubscribe handle (Charter/Board live refresh)
    cbTimer: null,        // debounce timer for Charter/Board refetch
  };

  /**
   * sha1(path)[:16] — MUST match the backend project_channel_key. We use
   * SubtleCrypto when available (async), but the push channel key is needed
   * synchronously at subscribe time, so we keep a tiny pure-JS sha1 here to
   * stay deterministic + dependency-free and identical across both sides.
   */
  function projectKeyHash(path) {
    if (!path) return '';
    return _sha1(String(path)).slice(0, 16);
  }

  // Minimal, dependency-free SHA-1 (hex). Sufficient for a routing key —
  // not used for any security purpose.
  function _sha1(str) {
    function rotl(n, s) { return (n << s) | (n >>> (32 - s)); }
    var bytes = unescape(encodeURIComponent(str));
    var words = [];
    for (var i = 0; i < bytes.length; i++) {
      words[i >> 2] |= bytes.charCodeAt(i) << ((3 - (i % 4)) * 8);
    }
    var bitLen = bytes.length * 8;
    words[bitLen >> 5] |= 0x80 << (24 - (bitLen % 32));
    words[((bitLen + 64 >> 9) << 4) + 15] = bitLen;
    var w = [], H0 = 1732584193, H1 = -271733879, H2 = -1732584194,
        H3 = 271733878, H4 = -1009589776;
    for (var j = 0; j < words.length; j += 16) {
      var a = H0, b = H1, c = H2, d = H3, e = H4;
      for (var t = 0; t < 80; t++) {
        w[t] = (t < 16) ? (words[j + t] | 0)
          : rotl(w[t - 3] ^ w[t - 8] ^ w[t - 14] ^ w[t - 16], 1);
        var f, k;
        if (t < 20) { f = (b & c) | (~b & d); k = 1518500249; }
        else if (t < 40) { f = b ^ c ^ d; k = 1859775393; }
        else if (t < 60) { f = (b & c) | (b & d) | (c & d); k = -1894007588; }
        else { f = b ^ c ^ d; k = -899497514; }
        var tmp = (rotl(a, 5) + f + e + k + w[t]) | 0;
        e = d; d = c; c = rotl(b, 30); b = a; a = tmp;
      }
      H0 = (H0 + a) | 0; H1 = (H1 + b) | 0; H2 = (H2 + c) | 0;
      H3 = (H3 + d) | 0; H4 = (H4 + e) | 0;
    }
    function hex(n) {
      var s = '';
      for (var i = 7; i >= 0; i--) s += ((n >>> (i * 4)) & 0xf).toString(16);
      return s;
    }
    return hex(H0) + hex(H1) + hex(H2) + hex(H3) + hex(H4);
  }

  function _t(key, fallback) {
    try { return (typeof t === 'function') ? t(key) : fallback; }
    catch (_e) { return fallback; }
  }

  function _activityListEl() { return document.getElementById('projectBrainActivityList'); }

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

  /** Absolute local timestamp string for the row's title (hover) tooltip. */
  function _absTime(ts) {
    var n = Number(ts) || 0;
    if (!n) return '';
    try { return new Date(n).toLocaleString(); } catch (_e) { return ''; }
  }

  /**
   * Render the Activity-column legend: one chip per kind (icon + localized
   * label), so the 10 event glyphs are self-documenting. Idempotent — replaces
   * any existing legend. Inserted ABOVE the activity list.
   */
  function _renderLegend() {
    var list = _activityListEl();
    if (!list || !list.parentNode) return;
    var host = list.parentNode;
    var existing = host.querySelector('.pb-activity-legend');
    if (existing) existing.remove();
    var legend = document.createElement('div');
    legend.className = 'pb-activity-legend';
    legend.title = _t('projectBrain.legendTitle', 'Legend');
    var html = '';
    for (var i = 0; i < _KIND_ORDER.length; i++) {
      var kind = _KIND_ORDER[i];
      var glyph = _KIND_ICON[kind] || _KIND_ICON.note;
      var label = _t('projectBrain.kind.' + kind, kind);
      html += '<span class="pb-legend-item pb-kind-' + kind + '">' +
        '<span class="pb-legend-ico">' +
        ((typeof Icon === 'function') ? Icon(glyph, 13) : '') + '</span>' +
        '<span class="pb-legend-label">' + _esc(label) + '</span></span>';
    }
    legend.innerHTML = html;
    host.insertBefore(legend, list);
  }

  /** Show the "no activity yet" placeholder when the list has no event rows. */
  function _ensureActivityEmptyState() {
    var list = _activityListEl();
    if (!list) return;
    if (!list.querySelector('.pb-activity-row')) {
      list.innerHTML = '<div class="pb-activity-empty">' +
        _esc(_t('projectBrain.activityEmpty', 'No activity yet')) + '</div>';
    }
  }

  /** Build one activity row element from an event record. Pure (testable). */
  function buildActivityRow(ev) {
    var row = document.createElement('div');
    row.className = 'pb-activity-row pb-kind-' + (ev.kind || 'note');
    row.dataset.eventId = ev.event_id || '';
    row.dataset.seq = String(ev.seq || 0);

    var kindLabel = _t('projectBrain.kind.' + (ev.kind || 'note'), ev.kind || '');
    var iconName = _KIND_ICON[ev.kind] || _KIND_ICON.note;
    var icon = document.createElement('span');
    icon.className = 'pb-activity-icon';
    // The glyph is self-documenting via a localized title — hovering any row
    // icon names its event kind (the legend gives the same key at a glance).
    icon.title = kindLabel;
    icon.innerHTML = (typeof Icon === 'function') ? Icon(iconName, 15) : '';
    row.appendChild(icon);

    var body = document.createElement('div');
    body.className = 'pb-activity-body';

    var summary = document.createElement('div');
    summary.className = 'pb-activity-summary';
    summary.textContent = ev.summary || kindLabel;
    body.appendChild(summary);

    // Timestamp row — a legend without WHEN is only half a fix. Relative text
    // (localized) with the absolute local time as the hover title.
    var rel = _relTime(ev.ts);
    if (rel) {
      var timeEl = document.createElement('div');
      timeEl.className = 'pb-activity-time';
      timeEl.textContent = rel;
      var abs = _absTime(ev.ts);
      if (abs) timeEl.title = abs;
      body.appendChild(timeEl);
    }

    if (ev.title || ev.conv_id) {
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'pb-conv-chip';
      chip.textContent = ev.title || ev.conv_id;
      chip.dataset.convId = ev.conv_id || '';
      chip.addEventListener('click', function () {
        if (ev.conv_id && typeof loadConversation === 'function') {
          loadConversation(ev.conv_id);
        }
      });
      body.appendChild(chip);
    }

    row.appendChild(body);
    return row;
  }

  /**
   * Render one event into the Activity column IF it's new. Returns true when
   * it was rendered, false when deduped (already seen / older than backfill).
   * This is the backfill→live boundary guard the frontend NC targets.
   */
  function ingestEvent(ev, opts) {
    if (!ev || !_state.seen) return false;
    var fromBackfill = !!(opts && opts.backfill);
    var eid = ev.event_id || '';
    // Dedup by seq window (live frames at/under the backfilled high-water are
    // duplicates) and by event_id (idempotent).
    if (!fromBackfill && ev.seq && ev.seq <= _state.maxSeq) return false;
    if (eid && _state.seen.has(eid)) return false;

    if (eid) _state.seen.add(eid);
    if (ev.seq && ev.seq > _state.maxSeq) _state.maxSeq = ev.seq;

    var list = _activityListEl();
    if (list) {
      var row = buildActivityRow(ev);
      // newest on top
      if (list.firstChild) list.insertBefore(row, list.firstChild);
      else list.appendChild(row);
      var empty = list.querySelector('.pb-activity-empty');
      if (empty) empty.remove();
    }
    return true;
  }

  /** Handle a live push frame {type:'activity', event:{...}}. */
  function _onPush(frame) {
    if (!frame || frame.type !== 'activity' || !frame.event) return;
    ingestEvent(frame.event, { backfill: false });
  }

  /** Open the feed for a project: reset, backfill, then subscribe live. */
  function openFeed(path) {
    closeFeed();
    if (!path) return;
    _state.path = path;
    _state.maxSeq = 0;
    _state.seen = new Set();

    // Render the (static) kind legend once per feed open, above the list.
    _renderLegend();

    // 1) Backfill (REST). Sorted newest-first by the backend; we ingest oldest
    //    -first so insertBefore yields newest-on-top in the right order.
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    var p = api ? api.feed(path, 0) : Promise.resolve(null);
    Promise.resolve(p).then(function (res) {
      var events = (res && res.events) ? res.events.slice() : [];
      events.sort(function (a, b) { return (a.seq || 0) - (b.seq || 0); });
      for (var i = 0; i < events.length; i++) {
        ingestEvent(events[i], { backfill: true });
      }
      if (res && typeof res.maxSeq === 'number' && res.maxSeq > _state.maxSeq) {
        _state.maxSeq = res.maxSeq;
      }
      // closeFeed() wiped the list innerHTML; if the backfill produced no rows
      // the column would otherwise be a blank void — restore the placeholder.
      _ensureActivityEmptyState();
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] backfill failed', e);
      _ensureActivityEmptyState();
    });

    // 2) Live subscribe via the path-hashed key (raw path never on wire).
    if (typeof pushSubscribe === 'function') {
      pushSubscribe('project', projectKeyHash(path), _onPush);
      _state.unsub = function () {
        if (typeof pushUnsubscribe === 'function') {
          pushUnsubscribe('project', projectKeyHash(path), _onPush);
        }
      };
    }
  }

  function closeFeed() {
    if (_state.unsub) { try { _state.unsub(); } catch (_e) { /* noop */ } }
    _state.unsub = null;
    _state.path = '';
    _state.maxSeq = 0;
    _state.seen = null;
    var list = _activityListEl();
    if (list) {
      list.innerHTML = '';
      // The legend is a SIBLING of the list (not wiped by innerHTML) — remove
      // it too so a closed panel doesn't leave a stale legend behind.
      if (list.parentNode) {
        var lg = list.parentNode.querySelector('.pb-activity-legend');
        if (lg) lg.remove();
      }
    }
  }

  /**
   * Resolve the project path of the conversation currently on screen — the
   * SAME accessor presence.js uses (getActiveConv → _getConvProjectPath),
   * NEVER a process-global singleton, so two tabs on different projects stay
   * isolated.
   */
  function _displayedProjectPath() {
    try {
      var conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
      var p = '';
      if (conv) {
        p = (typeof _getConvProjectPath === 'function')
          ? _getConvProjectPath(conv) : (conv.projectPath || '');
      }
      // Fallback: a shell-loaded conv may not carry projectPath in-memory yet,
      // but the active-project singleton (projectState.path) is set. Mirrors
      // how the rest of the app resolves the active project.
      if (!p && typeof projectState !== 'undefined' && projectState &&
          projectState.active) {
        p = projectState.path || '';
      }
      return String(p || '').replace(/[/\\]+$/, '');
    } catch (_e) { return ''; }
  }

  function _esc(s) {
    if (typeof escapeHtml === 'function') return escapeHtml(String(s == null ? '' : s));
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  // ── Charter column ──────────────────────────────────────────────
  // Renders the north-star content + committed decisions, plus PENDING
  // proposed_decision events (pulled from the live feed state) each with a
  // human commit/reject control — the human gate for commit_charter.
  function renderCharter(rec, pendingProposals) {
    var el = document.getElementById('projectBrainCharterBody');
    if (!el) return;
    var path = _state.path;
    var version = (rec && typeof rec.version === 'number') ? rec.version : 0;
    var parts = [];
    var content = (rec && rec.content) || '';
    var decisions = (rec && rec.decisions) || [];
    if (!content && !decisions.length && !(pendingProposals || []).length) {
      el.innerHTML = '<div class="pb-charter-empty">' +
        _esc(_t('projectBrain.charterEmpty', 'No charter yet')) + '</div>';
      return;
    }
    if (content) {
      parts.push('<div class="pb-charter-northstar">' + _esc(content) + '</div>');
    }
    if (decisions.length) {
      parts.push('<div class="pb-charter-section">' +
        _esc(_t('projectBrain.committedDecisions', 'Committed decisions')) + '</div>');
      parts.push('<ul class="pb-charter-decisions">');
      for (var i = 0; i < decisions.length; i++) {
        var d = decisions[i];
        var txt = (d && typeof d === 'object') ? (d.text || '') : String(d);
        parts.push('<li>' + _esc(txt) + '</li>');
      }
      parts.push('</ul>');
    }
    // Pending proposals — the human gate. Each carries a commit + reject btn.
    var props = pendingProposals || [];
    if (props.length) {
      parts.push('<div class="pb-charter-section">' +
        _esc(_t('projectBrain.pendingProposals', 'Proposed (awaiting your review)')) + '</div>');
      for (var j = 0; j < props.length; j++) {
        var p = props[j];
        var ptext = p.summary || (p.payload && p.payload.proposal) || '';
        var pid = p.proposalId || (p.payload && p.payload.proposalId) || '';
        parts.push(
          '<div class="pb-proposal" data-event-id="' + _esc(p.event_id) +
          '" data-proposal-id="' + _esc(pid) + '">' +
          '<div class="pb-proposal-text">' + _esc(ptext) + '</div>' +
          '<div class="pb-proposal-actions">' +
          '<button type="button" class="pb-proposal-commit" data-text="' + _esc(ptext) +
          '" data-ver="' + version + '" data-proposal-id="' + _esc(pid) + '">' +
          _esc(_t('projectBrain.commit', 'Commit')) + '</button>' +
          '<button type="button" class="pb-proposal-reject" data-proposal-id="' + _esc(pid) + '">' +
          _esc(_t('projectBrain.reject', 'Reject')) + '</button>' +
          '</div></div>');
      }
    }
    el.innerHTML = parts.join('');
    // Wire commit/reject — commit calls the human-gated commit route, then
    // re-renders so the decision moves from "proposed" to "committed".
    var commitBtns = el.querySelectorAll('.pb-proposal-commit');
    for (var c = 0; c < commitBtns.length; c++) {
      commitBtns[c].addEventListener('click', function (ev) {
        var btn = ev.currentTarget;
        var text = btn.getAttribute('data-text') || '';
        var ver = parseInt(btn.getAttribute('data-ver') || '0', 10);
        var pid = btn.getAttribute('data-proposal-id') || '';
        _commitCharterDecision(path, text, ver, pid, btn);
      });
    }
    var rejectBtns = el.querySelectorAll('.pb-proposal-reject');
    for (var r = 0; r < rejectBtns.length; r++) {
      rejectBtns[r].addEventListener('click', function (ev) {
        var btn = ev.currentTarget;
        var pid = btn.getAttribute('data-proposal-id') || '';
        _dismissProposal(path, pid, btn);
      });
    }
  }

  function _commitCharterDecision(path, text, expectedVersion, proposalId, btn) {
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    if (!api || !path) return;
    if (btn) { btn.disabled = true; btn.textContent = _t('projectBrain.committing', 'Committing…'); }
    // Thread resolves_proposal so this commit durably resolves THIS proposal
    // → it drops out of the pending set (no over-count).
    Promise.resolve(api.commitCharter(path, {
      add_decision: text, expected_version: expectedVersion,
      resolves_proposal: proposalId || '',
    })).then(function () {
      // Re-fetch charter so the committed decision now shows under
      // "Committed decisions" and the proposal control disappears.
      refreshCharter(path);
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] commit failed', e);
      if (btn) { btn.disabled = false; btn.textContent = _t('projectBrain.commit', 'Commit'); }
    });
  }

  function _dismissProposal(path, proposalId, btn) {
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    if (!api || !path || typeof api.dismissProposal !== 'function') {
      // No durable route → fall back to a local dismiss so the click isn't dead.
      var node = btn && btn.closest ? btn.closest('.pb-proposal') : null;
      if (node) node.remove();
      return;
    }
    if (btn) { btn.disabled = true; }
    // Durable reject: emits a 'dismissed' event so the proposal drops out of
    // the pending set for everyone, permanently (survives reload).
    Promise.resolve(api.dismissProposal(path, proposalId)).then(function () {
      refreshCharter(path);
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] dismiss failed', e);
      if (btn) { btn.disabled = false; }
    });
  }

  function refreshCharter(path) {
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    if (!api || !path || typeof api.charter !== 'function') return;
    // Pending proposals come from the SINGLE server source
    // (charterPending → excludes committed/dismissed by proposalId), so a
    // resolved proposal never reappears. Fallback to a raw feed filter only
    // if the pending route is unavailable (older Api client).
    Promise.resolve(api.charter(path)).then(function (rec) {
      if (typeof api.charterPending === 'function') {
        Promise.resolve(api.charterPending(path)).then(function (res) {
          renderCharter(rec || {}, (res && res.pending) || []);
        }).catch(function () { renderCharter(rec || {}, []); });
      } else {
        Promise.resolve(api.feed(path, 0)).then(function (feed) {
          var props = ((feed && feed.events) || []).filter(function (e) {
            return e.kind === 'proposed_decision';
          });
          renderCharter(rec || {}, props);
        }).catch(function () { renderCharter(rec || {}, []); });
      }
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] charter load failed', e);
    });
  }

  // ── Board column ────────────────────────────────────────────────
  // A kanban of open/claimed/done epics. A claimed card shows its owner-conv
  // chip + a "brain-dispatched" badge when the claim came from dispatch.
  function _boardCard(t) {
    var owner = t.owner_conv_id || '';
    var ownerChip = owner
      ? '<button type="button" class="pb-conv-chip" data-conv-id="' + _esc(owner) + '">' +
        _esc(owner) + '</button>'
      : '';
    // "brain-dispatched" badge — this claim was minted by the autonomous
    // dispatch heartbeat, not a human/agent. Surfaces the autonomy visibly.
    var badge = t.dispatched
      ? '<span class="pb-board-badge pb-board-badge-dispatched" title="'
        + _esc(_t('projectBrain.dispatchedTitle', 'Started autonomously by the project brain'))
        + '">' + ((typeof Icon === 'function') ? Icon('rocket', 11) : '')
        + '<span>' + _esc(_t('projectBrain.dispatched', 'auto')) + '</span></span>'
      : '';
    return '<div class="pb-board-card pb-board-' + _esc(t.status) + '" data-task-id="' +
      _esc(t.id) + '">' +
      '<div class="pb-board-title">' + _esc(t.title) + '</div>' +
      '<div class="pb-board-card-meta">' + ownerChip + badge + '</div></div>';
  }

  function renderBoard(board) {
    var el = document.getElementById('projectBrainBoardBody');
    if (!el) return;
    var tasks = (board && board.tasks) || [];
    if (!tasks.length) {
      el.innerHTML = '<div class="pb-board-empty">' +
        _esc(_t('projectBrain.boardEmpty', 'Board is empty')) + '</div>';
      return;
    }
    var cols = { open: [], claimed: [], done: [] };
    for (var i = 0; i < tasks.length; i++) {
      var t = tasks[i];
      (cols[t.status] || cols.open).push(t);
    }
    function lane(key, labelKey) {
      var cards = cols[key].map(_boardCard).join('') ||
        '<div class="pb-board-lane-empty">—</div>';
      return '<div class="pb-board-lane pb-board-lane-' + key + '">' +
        '<div class="pb-board-lane-head">' + _esc(_t(labelKey, key)) +
        ' <span class="pb-board-count">' + cols[key].length + '</span></div>' +
        cards + '</div>';
    }
    el.innerHTML =
      lane('open', 'projectBrain.laneOpen') +
      lane('claimed', 'projectBrain.laneClaimed') +
      lane('done', 'projectBrain.laneDone');
    // conv-chip click → open that conversation
    var chips = el.querySelectorAll('.pb-conv-chip');
    for (var c = 0; c < chips.length; c++) {
      chips[c].addEventListener('click', function (ev) {
        var cid = ev.currentTarget.getAttribute('data-conv-id');
        if (cid && typeof loadConversation === 'function') loadConversation(cid);
      });
    }
  }

  function refreshBoard(path) {
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    if (!api || !path || typeof api.board !== 'function') return;
    Promise.resolve(api.board(path)).then(function (board) {
      renderBoard(board || {});
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] board load failed', e);
    });
  }

  // ── Live Charter/Board refresh ──────────────────────────────────
  // While the panel is open, ANY project-channel frame (board claim/complete,
  // charter propose/commit) debounce-refetches the Charter + Board columns so
  // they are live like Activity — not just pull-on-open. Subscribes with '*'
  // (re-resolves the displayed root itself) and refetches by explicit path.
  function _subscribePanelLive(path) {
    _unsubscribePanelLive();
    if (typeof pushSubscribe !== 'function' || !path) return;
    var handler = function () {
      var cur = _displayedProjectPath();
      if (!cur || cur !== path) return;   // ignore other projects' frames
      if (_state.cbTimer) clearTimeout(_state.cbTimer);
      _state.cbTimer = setTimeout(function () {
        _state.cbTimer = null;
        refreshCharter(path);
        refreshBoard(path);
      }, 300);
    };
    pushSubscribe('project', '*', handler);
    _state.panelUnsub = function () {
      if (typeof pushUnsubscribe === 'function') pushUnsubscribe('project', '*', handler);
    };
  }

  function _unsubscribePanelLive() {
    if (_state.cbTimer) { clearTimeout(_state.cbTimer); _state.cbTimer = null; }
    if (_state.panelUnsub) { try { _state.panelUnsub(); } catch (_e) { /* noop */ } }
    _state.panelUnsub = null;
  }

  function openProjectBrain() {
    var overlay = document.getElementById('projectBrainOverlay');
    if (!overlay) return;
    // Head glyph (SVG, no emoji).
    var headIco = document.getElementById('projectBrainHeadIcon');
    if (headIco && typeof Icon === 'function') headIco.innerHTML = Icon('brain', 18);
    var btnIco = document.getElementById('projectBrainBtn');
    if (btnIco && !btnIco.innerHTML && typeof Icon === 'function') {
      btnIco.innerHTML = Icon('brain', 15);
    }
    overlay.hidden = false;
    overlay.classList.add('pb-open');
    var path = _displayedProjectPath();
    if (path) {
      openFeed(path);
      refreshCharter(path);
      refreshBoard(path);
      _subscribePanelLive(path);
    }
  }

  function closeProjectBrain() {
    var overlay = document.getElementById('projectBrainOverlay');
    if (overlay) { overlay.hidden = true; overlay.classList.remove('pb-open'); }
    closeFeed();
    _unsubscribePanelLive();
  }

  function toggleProjectBrain() {
    var overlay = document.getElementById('projectBrainOverlay');
    if (overlay && !overlay.hidden) closeProjectBrain();
    else openProjectBrain();
  }

  // Close + re-resolve on conversation/project switch (mirrors presenceRefresh):
  // if the panel is open and the displayed project changed, re-open the feed
  // for the new project so two projects never bleed into one view.
  function projectBrainRefresh() {
    var overlay = document.getElementById('projectBrainOverlay');
    if (!overlay || overlay.hidden) return;
    var path = _displayedProjectPath();
    if (path && path !== _state.path) {
      openFeed(path);
      refreshCharter(path);
      refreshBoard(path);
      _subscribePanelLive(path);
    } else if (!path) {
      closeFeed();
      _unsubscribePanelLive();
    }
  }

  // Expose for HTML onclick + main/loadConversation + the jsdom harness.
  window.ProjectBrain = {
    projectKeyHash: projectKeyHash,
    buildActivityRow: buildActivityRow,
    ingestEvent: ingestEvent,
    _renderLegend: _renderLegend,
    _relTime: _relTime,
    openFeed: openFeed,
    closeFeed: closeFeed,
    renderCharter: renderCharter,
    refreshCharter: refreshCharter,
    renderBoard: renderBoard,
    refreshBoard: refreshBoard,
    _onPush: _onPush,
    _state: _state,
  };
  window.toggleProjectBrain = toggleProjectBrain;
  window.openProjectBrain = openProjectBrain;
  window.closeProjectBrain = closeProjectBrain;
  window.projectBrainRefresh = projectBrainRefresh;
})();

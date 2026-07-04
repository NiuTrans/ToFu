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
        // Payload-FIRST: payload.proposal is the FULL proposal text; p.summary
        // is only the 280-char feed-row cap. The commit derives its durable
        // decision from this text, so render (and commit) the full version.
        var ptext = (p.payload && p.payload.proposal) || p.summary || '';
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
  // chip + a "brain-dispatched" badge when the claim came from dispatch. Each
  // card carries HUMAN lifecycle controls (backend-authoritative — every click
  // hits Api.project.board* then refreshBoard, never a local DOM mutation).

  /** Resolve the displayed conversation id — the human's proxy for a board
   *  mutation (SAME source the influence lens uses). '' when no active conv. */
  function _boardConvId() {
    try {
      return (typeof activeConvId !== 'undefined' && activeConvId) ? activeConvId : '';
    } catch (_e) { return ''; }
  }

  /** One action button (SVG icon + label). `act` ∈ complete|block|reopen. */
  function _boardActionBtn(act, glyph, labelKey, fallback) {
    return '<button type="button" class="pb-board-act pb-board-act-' + act +
      '" data-act="' + act + '" title="' + _esc(_t(labelKey, fallback)) + '">' +
      ((typeof Icon === 'function') ? Icon(glyph, 12) : '') +
      '<span>' + _esc(_t(labelKey, fallback)) + '</span></button>';
  }

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
    // Human lifecycle controls, gated by status:
    //   • complete + block on open|claimed (lifecycle of live work)
    //   • reopen on claimed (break a stuck live claim) AND done (revive)
    var acts = [];
    if (t.status === 'open' || t.status === 'claimed') {
      acts.push(_boardActionBtn('complete', 'check', 'projectBrain.actComplete', 'Done'));
      acts.push(_boardActionBtn('block', 'ban', 'projectBrain.actBlock', 'Block'));
    }
    if (t.status === 'claimed' || t.status === 'done') {
      acts.push(_boardActionBtn('reopen', 'refresh', 'projectBrain.actReopen', 'Reopen'));
    }
    var actionsRow = acts.length
      ? '<div class="pb-board-card-actions">' + acts.join('') + '</div>' : '';
    return '<div class="pb-board-card pb-board-' + _esc(t.status) + '" data-task-id="' +
      _esc(t.id) + '">' +
      '<div class="pb-board-title">' + _esc(t.title) + '</div>' +
      '<div class="pb-board-card-meta">' + ownerChip + badge + '</div>' +
      actionsRow + '</div>';
  }

  /** Dispatch a per-card human mutation → backend → refreshBoard (no local
   *  DOM mutation; the board re-renders verbatim from the backend). */
  function _boardMutate(act, taskId, btn) {
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    var path = _state.path || _displayedProjectPath();
    if (!api || !path || !taskId) return;
    var convId = _boardConvId();
    var call = null;
    if (act === 'complete' && typeof api.boardComplete === 'function') {
      call = api.boardComplete(path, taskId, convId);
    } else if (act === 'reopen' && typeof api.boardReopen === 'function') {
      call = api.boardReopen(path, taskId, convId);
    } else if (act === 'block' && typeof api.boardBlock === 'function') {
      var reason = '';
      if (typeof prompt === 'function') {
        reason = prompt(_t('projectBrain.blockReasonPrompt', 'Why is this blocked?')) || '';
      }
      call = api.boardBlock(path, taskId, convId, reason);
    }
    if (!call) return;
    if (btn) btn.disabled = true;
    Promise.resolve(call).then(function () {
      refreshBoard(path);
      refreshInfluence(path);
      refreshConvInfluenceBar();
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] board ' + act + ' failed', e);
      if (btn) btn.disabled = false;
    });
  }

  /** Prompt for a title and post a new OPEN epic (created_by_conv = displayed
   *  conv). Disabled entirely when there's no conversation context (the
   *  backend refuses a blank convId, so the UI must not offer it). */
  function _boardPostNew() {
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    var path = _state.path || _displayedProjectPath();
    var convId = _boardConvId();
    if (!api || !path || !convId || typeof api.boardPost !== 'function') return;
    var title = (typeof prompt === 'function')
      ? (prompt(_t('projectBrain.newEpicPrompt', 'New epic title')) || '').trim() : '';
    if (!title) return;
    Promise.resolve(api.boardPost(path, { title: title, convId: convId })).then(function () {
      refreshBoard(path);
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] board post failed', e);
    });
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
    // "＋ New epic" affordance (SVG plus, no emoji). Disabled with an
    // explanatory title when there's no conversation context, because the
    // backend refuses a blank created_by_conv → the UI must not offer it.
    var hasConv = !!_boardConvId();
    var newBtn = '<button type="button" class="pb-board-new" id="pbBoardNewBtn"' +
      (hasConv ? '' : ' disabled') + ' title="' +
      _esc(hasConv ? _t('projectBrain.newEpic', 'New epic')
                   : _t('projectBrain.newEpicNoConv',
                        'Open a conversation to post an epic')) + '">' +
      ((typeof Icon === 'function') ? Icon('plus', 13) : '') +
      '<span>' + _esc(_t('projectBrain.newEpic', 'New epic')) + '</span></button>';
    el.innerHTML =
      '<div class="pb-board-toolbar">' + newBtn + '</div>' +
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
    // "＋ New epic"
    var nb = el.querySelector('#pbBoardNewBtn');
    if (nb && !nb.disabled) nb.addEventListener('click', _boardPostNew);
    // Per-card human lifecycle actions (complete / block / reopen).
    var actBtns = el.querySelectorAll('.pb-board-act');
    for (var a = 0; a < actBtns.length; a++) {
      actBtns[a].addEventListener('click', function (ev) {
        var btn = ev.currentTarget;
        var card = btn.closest ? btn.closest('.pb-board-card') : null;
        var tid = card ? card.getAttribute('data-task-id') : '';
        var act = btn.getAttribute('data-act');
        if (tid && act) _boardMutate(act, tid, btn);
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

  // ── Per-conversation Influence lens ─────────────────────────────
  // Answers the conversation-scoped question "how is THIS chat affected by
  // the project brain?" — distinct from the global three columns. The data is
  // computed BACKEND-side (Api.project.brainInfluence → build_conv_influence),
  // which reuses the SAME render_charter_block / render_board_block the prompt
  // injects, so this lens can never drift from what the model actually sees.
  // The frontend is a pure renderer of that structured verdict.

  /** Build one "chip" segment (icon + count + label) for the influence head. */
  function _influenceChip(glyph, n, labelKey, fallbackLabel, cls) {
    if (!n) return '';
    var label = _t('projectBrain.' + labelKey, fallbackLabel).replace('{n}', n);
    return '<span class="pb-inf-chip ' + (cls || '') + '">' +
      ((typeof Icon === 'function') ? Icon(glyph, 12) : '') +
      '<span>' + _esc(label) + '</span></span>';
  }

  /** Render one board epic row inside an influence group. */
  function _influenceEpicRow(t, cls) {
    var owner = t.owner
      ? '<button type="button" class="pb-conv-chip" data-conv-id="' + _esc(t.owner) + '">' +
        _esc(t.owner) + '</button>'
      : '';
    return '<div class="pb-inf-epic ' + (cls || '') + '">' +
      '<span class="pb-inf-epic-title">' + _esc(t.title || t.id) + '</span>' +
      owner + '</div>';
  }

  /**
   * Render the per-conversation influence lens from the backend verdict.
   * `inf` is the build_conv_influence dict. Renders NOTHING (hides the banner)
   * when the brain has no effect on this conversation (no charter + empty
   * board + no pending) — so a solo/empty project adds no visual noise.
   */
  function renderInfluence(inf) {
    var banner = document.getElementById('projectBrainInfluence');
    var body = document.getElementById('projectBrainInfluenceBody');
    var convEl = document.getElementById('projectBrainInfluenceConv');
    if (!banner || !body) return;
    inf = inf || {};
    var charter = inf.charter || {};
    var board = inf.board || {};
    var mine = board.mine || [];
    var avoid = board.avoid || [];
    var open = board.open || [];
    var pending = inf.pendingDecisions || [];
    var charterActive = !!charter.injected &&
      (!!charter.content || (charter.decisions || []).length);

    // Nothing influences this conversation → hide the banner entirely.
    if (!charterActive && !mine.length && !avoid.length && !open.length &&
        !pending.length) {
      banner.hidden = true;
      body.innerHTML = '';
      if (convEl) convEl.textContent = '';
      return;
    }
    banner.hidden = false;

    // The conversation this lens is scoped to (title if we can resolve it).
    if (convEl) {
      var label = '';
      try {
        var conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
        label = (conv && (conv.title || conv.id)) || inf.convId || '';
      } catch (_e) { label = inf.convId || ''; }
      convEl.textContent = label ? ('· ' + label) : '';
    }

    // Head chips — a one-glance summary of the influence "shape".
    var chips = '';
    if (charterActive) {
      chips += _influenceChip('lightbulb',
        (charter.decisions || []).length || 1, 'infCharterBound',
        'bound by charter', 'pb-inf-chip-charter');
    }
    chips += _influenceChip('package', mine.length, 'infOwns',
      '{n} owned by you', 'pb-inf-chip-mine');
    chips += _influenceChip('alertTriangle', avoid.length, 'infAvoid',
      '{n} to avoid', 'pb-inf-chip-avoid');
    chips += _influenceChip('messageSquare', pending.length, 'infPending',
      '{n} awaiting you', 'pb-inf-chip-pending');

    var parts = [];
    if (chips) parts.push('<div class="pb-inf-chips">' + chips + '</div>');

    // Charter this conversation is bound by (the shared north star + the
    // committed decisions the prompt actually injects).
    if (charterActive) {
      var cparts = ['<div class="pb-inf-group pb-inf-group-charter">'];
      cparts.push('<div class="pb-inf-group-head">' +
        _esc(_t('projectBrain.infCharterHead', 'Bound by the charter')) + '</div>');
      if (charter.content) {
        cparts.push('<div class="pb-inf-northstar">' + _esc(charter.content) + '</div>');
      }
      var decs = charter.decisions || [];
      if (decs.length) {
        cparts.push('<ul class="pb-inf-decisions">');
        for (var i = 0; i < Math.min(decs.length, 6); i++) {
          cparts.push('<li>' + _esc(decs[i]) + '</li>');
        }
        cparts.push('</ul>');
      }
      cparts.push('</div>');
      parts.push(cparts.join(''));
    }

    // Board influence — what THIS conv owns vs. must not redo.
    if (mine.length) {
      parts.push('<div class="pb-inf-group pb-inf-group-mine">' +
        '<div class="pb-inf-group-head">' +
        _esc(_t('projectBrain.infMineHead', 'Epics you are advancing')) + '</div>' +
        mine.map(function (t) { return _influenceEpicRow(t, 'pb-inf-mine'); }).join('') +
        '</div>');
    }
    if (avoid.length) {
      parts.push('<div class="pb-inf-group pb-inf-group-avoid">' +
        '<div class="pb-inf-group-head">' +
        _esc(_t('projectBrain.infAvoidHead',
          'Avoid duplicating — advanced by a sibling')) + '</div>' +
        avoid.map(function (t) { return _influenceEpicRow(t, 'pb-inf-avoid'); }).join('') +
        '</div>');
    }
    if (open.length) {
      parts.push('<div class="pb-inf-group pb-inf-group-open">' +
        '<div class="pb-inf-group-head">' +
        _esc(_t('projectBrain.infOpenHead', 'Open — you could claim')) + '</div>' +
        open.slice(0, 6).map(function (t) {
          return _influenceEpicRow(t, 'pb-inf-open');
        }).join('') +
        '</div>');
    }
    body.innerHTML = parts.join('');

    // conv chips (peer owners) → open that conversation.
    var chipsEls = body.querySelectorAll('.pb-conv-chip');
    for (var c = 0; c < chipsEls.length; c++) {
      chipsEls[c].addEventListener('click', function (ev) {
        var cid = ev.currentTarget.getAttribute('data-conv-id');
        if (cid && typeof loadConversation === 'function') loadConversation(cid);
      });
    }
  }

  /** Fetch + render the per-conversation influence lens for (path, active conv). */
  function refreshInfluence(path) {
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    var banner = document.getElementById('projectBrainInfluence');
    if (!api || !path || typeof api.brainInfluence !== 'function') {
      if (banner) banner.hidden = true;
      return;
    }
    var convId = (typeof activeConvId !== 'undefined' && activeConvId)
      ? activeConvId : '';
    if (!convId) { if (banner) banner.hidden = true; return; }
    Promise.resolve(api.brainInfluence(path, convId)).then(function (inf) {
      // Guard against a conversation switch mid-flight.
      if (typeof activeConvId !== 'undefined' && inf && inf.convId &&
          inf.convId !== activeConvId) return;
      renderInfluence(inf || {});
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] influence load failed', e);
      if (banner) banner.hidden = true;
    });
  }

  // ── Always-visible per-conversation Influence BAR (docked on chat view) ──
  // The full Influence lens (renderInfluence) lives inside the Project Brain
  // overlay, which is closed by default. This bar is the ALWAYS-ON entry point
  // docked on the conversation view itself: a slim chip strip summarising how
  // THIS conversation is affected (bound by charter · N owned · M to avoid ·
  // K awaiting), updated on every conversation switch, hidden when there's no
  // influence. Same backend-authoritative source (Api.project.brainInfluence);
  // clicking opens the full lens. NO second client-side computation.

  /** Compute the same influence shape from a verdict → {charterActive, mine, avoid, pending}. */
  function _influenceShape(inf) {
    inf = inf || {};
    var charter = inf.charter || {};
    var board = inf.board || {};
    return {
      charterActive: !!charter.injected &&
        (!!charter.content || (charter.decisions || []).length),
      mine: (board.mine || []).length,
      avoid: (board.avoid || []).length,
      pending: (inf.pendingDecisions || []).length,
    };
  }

  /**
   * Render the inline conversation-influence bar from a backend verdict.
   * Hides the bar entirely when nothing influences this conversation (a solo/
   * empty project adds no chrome). Pure renderer of the structured verdict.
   */
  function renderConvInfluenceBar(inf) {
    var bar = document.getElementById('convInfluenceBar');
    if (!bar) return;
    var s = _influenceShape(inf);
    if (!s.charterActive && !s.mine && !s.avoid && !s.pending) {
      bar.hidden = true;
      bar.innerHTML = '';
      return;
    }
    var chips = '';
    // Lead glyph is a map-pin ("you are here in the project"), deliberately
    // NOT the brain glyph the collab bar above uses — two identical brains
    // stacked read as a duplicate. This bar is the conv-scoped sub-lens.
    chips += '<span class="conv-inf-lead">' +
      ((typeof Icon === 'function') ? Icon('mapPin', 13) : '') +
      '<span>' + _esc(_t('projectBrain.barLead', 'This chat')) + '</span></span>';
    if (s.charterActive) {
      chips += _influenceChip('lightbulb', 1, 'infCharterBound',
        'bound by charter', 'pb-inf-chip-charter');
    }
    chips += _influenceChip('package', s.mine, 'infOwns',
      '{n} owned by you', 'pb-inf-chip-mine');
    chips += _influenceChip('alertTriangle', s.avoid, 'infAvoid',
      '{n} to avoid', 'pb-inf-chip-avoid');
    chips += _influenceChip('messageSquare', s.pending, 'infPending',
      '{n} awaiting you', 'pb-inf-chip-pending');
    bar.innerHTML = chips;
    bar.hidden = false;
    bar.title = _t('projectBrain.barOpenHint', 'How THIS conversation is influenced — click to jump to its lens');
  }

  /**
   * Resolve (path, active conv) and refetch the inline influence bar. Called
   * by main.js on every conversation switch (window.convInfluenceRefresh) and
   * by the live push refresh. Backend-authoritative — never recomputed here.
   */
  function refreshConvInfluenceBar() {
    var bar = document.getElementById('convInfluenceBar');
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    var path = _displayedProjectPath();
    var convId = (typeof activeConvId !== 'undefined' && activeConvId)
      ? activeConvId : '';
    if (!api || typeof api.brainInfluence !== 'function' || !path || !convId) {
      if (bar) { bar.hidden = true; bar.innerHTML = ''; }
      return;
    }
    Promise.resolve(api.brainInfluence(path, convId)).then(function (inf) {
      // A conversation switch may have landed mid-flight — drop a stale reply.
      if (typeof activeConvId !== 'undefined' && inf && inf.convId &&
          inf.convId !== activeConvId) return;
      renderConvInfluenceBar(inf || {});
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] conv-influence bar load failed', e);
      if (bar) { bar.hidden = true; bar.innerHTML = ''; }
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
        refreshInfluence(path);
        refreshConvInfluenceBar();
        _refreshPeers(path);
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
      refreshInfluence(path);
      _refreshPeers(path);
      _subscribePanelLive(path);
    }
  }

  /** Drive the Team/Peers column (project-brain-peers.js) if it's loaded. */
  function _refreshPeers(path) {
    if (typeof window.ProjectBrainPeers !== 'undefined' &&
        window.ProjectBrainPeers &&
        typeof window.ProjectBrainPeers.refreshPeers === 'function') {
      window.ProjectBrainPeers.refreshPeers(path);
    }
  }

  /**
   * Deep-link entry for the per-conversation influence BAR: open the panel AND
   * scroll/flash the Influence lens (the conv-scoped section), rather than
   * landing at the top on the project-wide columns. This disambiguates the two
   * stacked bars — the project-wide collab bar opens the panel plainly
   * (openProjectBrain), while the conversation-scoped bar deep-links HERE to
   * its own lens, so "why does clicking either just open the same panel?"
   * becomes "each bar takes me to ITS section".
   */
  function openProjectBrainInfluence() {
    openProjectBrain();
    var banner = document.getElementById('projectBrainInfluence');
    if (!banner) return;
    // The influence data loads on a microtask (brainInfluence Promise); wait a
    // beat so the banner is un-hidden before we scroll/flash it.
    setTimeout(function () {
      if (banner.hidden) return;   // no influence → nothing to deep-link to
      try { banner.scrollIntoView({ block: 'start', behavior: 'smooth' }); }
      catch (_e) { /* jsdom / older browsers: best-effort */ }
      banner.classList.remove('pb-influence-flash');
      // reflow so re-adding the class re-triggers the animation
      void banner.offsetWidth;
      banner.classList.add('pb-influence-flash');
      setTimeout(function () { banner.classList.remove('pb-influence-flash'); }, 1400);
    }, 120);
  }

  function closeProjectBrain() {
    var overlay = document.getElementById('projectBrainOverlay');
    if (overlay) { overlay.hidden = true; overlay.classList.remove('pb-open'); }
    closeFeed();
    _unsubscribePanelLive();
    var banner = document.getElementById('projectBrainInfluence');
    if (banner) { banner.hidden = true; }
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
      refreshInfluence(path);
      _refreshPeers(path);
      _subscribePanelLive(path);
    } else if (path) {
      // Same project, but the active CONVERSATION may have changed — the
      // influence lens is conv-scoped, so re-resolve it even when the project
      // key is unchanged (charter/board are project-scoped and unaffected).
      // The peer roster also excludes the active conv server-side, so refresh
      // it too (a conv switch changes who counts as a "peer").
      refreshInfluence(path);
      _refreshPeers(path);
    } else {
      closeFeed();
      _unsubscribePanelLive();
      var banner = document.getElementById('projectBrainInfluence');
      if (banner) { banner.hidden = true; }
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
    renderInfluence: renderInfluence,
    refreshInfluence: refreshInfluence,
    renderConvInfluenceBar: renderConvInfluenceBar,
    refreshConvInfluenceBar: refreshConvInfluenceBar,
    _onPush: _onPush,
    _state: _state,
  };
  window.toggleProjectBrain = toggleProjectBrain;
  window.openProjectBrain = openProjectBrain;
  window.openProjectBrainInfluence = openProjectBrainInfluence;
  window.closeProjectBrain = closeProjectBrain;
  window.projectBrainRefresh = projectBrainRefresh;
  window.convInfluenceRefresh = refreshConvInfluenceBar;
})();

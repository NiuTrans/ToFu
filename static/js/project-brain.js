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
    tab: 'charter',       // active tab (charter|board|activity|peers)
    tabsWired: false,     // one-shot tab click delegation guard
  };

  // ── Tabs: show one surface at a time (full width) ──────────────
  /** Switch the visible tab-panel. `name` ∈ charter|board|activity|peers|status. */
  function _selectTab(name) {
    var prev = _state.tab;
    _state.tab = name;
    var tabs = document.querySelectorAll('.project-brain-tabs .pb-tab');
    for (var i = 0; i < tabs.length; i++) {
      var on = tabs[i].getAttribute('data-pb-tab') === name;
      tabs[i].classList.toggle('pb-tab-active', on);
      tabs[i].setAttribute('aria-selected', on ? 'true' : 'false');
    }
    var panels = document.querySelectorAll('.project-brain-columns .pb-tab-panel');
    for (var j = 0; j < panels.length; j++) {
      panels[j].classList.toggle('pb-tab-panel-active',
        panels[j].getAttribute('data-pb-panel') === name);
    }
    // The newly-active panel's content was rendered but not yet translated
    // (only VISIBLE items are processed). Re-apply so its free-text content
    // gets the overlay now that it's on screen (no-op when the toggle is off).
    if (typeof ProjectBrainI18n !== 'undefined' && ProjectBrainI18n &&
        typeof ProjectBrainI18n.applyAll === 'function') {
      try { ProjectBrainI18n.applyAll(); } catch (_e) { /* best-effort */ }
    }
    _onTabSelected(name, prev);
  }

  /**
   * Per-tab on-select hook. Decision #4 (fresh-on-tab-open): selecting INTO the
   * Status tab must re-fetch, because switching tabs only toggles CSS
   * visibility — the panel-open refresh could be minutes stale while siblings
   * finished epics on another tab. Gated on switching INTO status (prev!==name)
   * so an unrelated tab click never triggers it. Cheap: the backend staleness
   * gate returns the cached snapshot with no LLM call on a quiescent project.
   */
  function _onTabSelected(name, prev) {
    if (name === 'status' && prev !== 'status') {
      _refreshStatus(_state.path || _displayedProjectPath());
    }
  }

  /** Wire the tab bar once (click delegation). Idempotent. */
  function _initTabs() {
    if (_state.tabsWired) return;
    var bar = document.getElementById('projectBrainTabs');
    if (!bar) return;
    bar.addEventListener('click', function (ev) {
      var btn = ev.target && ev.target.closest ? ev.target.closest('.pb-tab') : null;
      if (!btn) return;
      var name = btn.getAttribute('data-pb-tab');
      if (name) _selectTab(name);
    });
    _state.tabsWired = true;
  }

  /** Set a tab's count badge (hidden when 0 / falsy). */
  function _setTabCount(id, n) {
    var el = document.getElementById(id);
    if (!el) return;
    if (n && n > 0) { el.textContent = n > 99 ? '99+' : String(n); el.hidden = false; }
    else { el.textContent = ''; el.hidden = true; }
  }

  // ── Long-text clamp-with-expand ─────────────────────────────────
  // A committed decision / north-star / proposal can be 2000+ chars; showing
  // it in full makes the surface an unreadable wall (the reported bug). Wrap
  // long text in a collapsed .pb-clamp with a Show more/less toggle. Short
  // text (< threshold) is returned as-is so we don't add chrome needlessly.
  var _CLAMP_THRESHOLD = 240;

  /** HTML for a clamp block. `innerHtml` is already-escaped/safe markup.
   *  The content element carries `data-pb-src="<rawText>"` — the ORIGINAL,
   *  authoritative text. The content-translation overlay (project-brain-i18n)
   *  lays a translation OVER this element's innerHTML while the source stays
   *  retrievable from the attribute; it never mutates rawText, and the
   *  commit/reject controls read their own data-text (never this). */
  function _clampBlock(innerHtml, rawText) {
    var srcAttr = rawText ? (' data-pb-src="' + _esc(rawText) + '"') : '';
    if ((rawText || '').length <= _CLAMP_THRESHOLD) {
      return '<div class="pb-clamp-inner"' + srcAttr + '>' + innerHtml + '</div>';
    }
    var more = _esc(_t('projectBrain.showMore', 'Show more'));
    return '<div class="pb-clamp-wrap">' +
      '<div class="pb-clamp"' + srcAttr + '>' + innerHtml + '</div>' +
      '<button type="button" class="pb-clamp-toggle" data-more="' + more +
      '" data-less="' + _esc(_t('projectBrain.showLess', 'Show less')) + '">' +
      ((typeof Icon === 'function') ? Icon('chevronDown', 12) : '') +
      '<span>' + more + '</span></button>' +
      '</div>';
  }

  /** Lay the content-translation overlay over a freshly-rendered subtree.
   *  No-op when the overlay module is absent or the toggle is off — the
   *  originals painted by the render fns stay on screen (source of truth). */
  function _applyContentI18n(el) {
    if (!el || typeof ProjectBrainI18n === 'undefined' || !ProjectBrainI18n ||
        typeof ProjectBrainI18n.apply !== 'function') return;
    try { ProjectBrainI18n.apply(el); }
    catch (_e) { /* overlay is best-effort; never break a render */ }
  }

  /** Delegate clamp-toggle clicks within a rendered container. */
  function _wireClampToggles(el) {
    if (!el) return;
    var btns = el.querySelectorAll('.pb-clamp-toggle');
    for (var i = 0; i < btns.length; i++) {
      btns[i].addEventListener('click', function (ev) {
        var btn = ev.currentTarget;
        var clamp = btn.parentNode.querySelector('.pb-clamp');
        if (!clamp) return;
        var open = clamp.classList.toggle('pb-clamp-open');
        btn.classList.toggle('pb-clamp-toggle-open', open);
        var lbl = btn.querySelector('span');
        if (lbl) lbl.textContent = open
          ? (btn.getAttribute('data-less') || 'Show less')
          : (btn.getAttribute('data-more') || 'Show more');
        // Lazy-on-expand: translate the now-visible long text (no-op when the
        // content-translation overlay is off / already-target / cached).
        if (open && typeof ProjectBrainI18n !== 'undefined' &&
            ProjectBrainI18n && typeof ProjectBrainI18n.apply === 'function') {
          ProjectBrainI18n.apply(clamp);
        }
      });
    }
  }

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

  // ── Hover preview for opaque conversation IDs ────────────────────
  // Every conv reference in the panel (activity chip, board owner chip,
  // influence row, peer roster card, peer-message thread) carries a
  // [data-conv-id]. On hover we resolve it to {title, firstUserMessage} via a
  // tiny backend preview endpoint and float a card next to the cursor, so the
  // opaque short id reads as "the first thing that conversation asked". The
  // fetch is cached (a conv's opening turn doesn't change) and shared behind a
  // single delegated listener on the overlay so it also covers chips that were
  // re-rendered after the listener was bound.
  var _convPreviewCache = {};     // convId → {title, firstUserMessage,...} | Promise
  var _previewEl = null;          // the single floating card, reused
  var _previewHoverId = '';       // convId currently hovered (guards stale async)
  var _previewTimer = null;       // hover-intent debounce

  /** Fetch (and cache) a conversation preview. Returns a Promise<preview|null>. */
  function _fetchConvPreview(convId) {
    if (!convId) return Promise.resolve(null);
    var cached = _convPreviewCache[convId];
    if (cached && typeof cached.then !== 'function') return Promise.resolve(cached);
    if (cached && typeof cached.then === 'function') return cached;
    var api = (typeof Api !== 'undefined' && Api.conversations) ? Api.conversations : null;
    if (!api || typeof api.preview !== 'function') return Promise.resolve(null);
    var p = Promise.resolve(api.preview(convId)).then(function (res) {
      // api_ok wraps the payload at top level; keep only the fields we render.
      var rec = res ? {
        id: res.id || convId,
        title: res.title || '',
        firstUserMessage: res.firstUserMessage || '',
        msgCount: res.msgCount || 0,
      } : null;
      _convPreviewCache[convId] = rec;   // cache even null (avoid refetch storms)
      return rec;
    }).catch(function (e) {
      if (typeof console !== 'undefined') console.warn('[ProjectBrain] conv preview failed', e);
      _convPreviewCache[convId] = null;
      return null;
    });
    _convPreviewCache[convId] = p;
    return p;
  }

  /**
   * Build the inner HTML of a hover-preview card from a preview record. Pure +
   * testable. Shows the conversation title (or the short id when untitled) and
   * the opening question; an explicit "no messages yet" line for empty convs.
   */
  function buildConvPreviewCard(preview, convId) {
    var short = String(convId || (preview && preview.id) || '').slice(0, 8);
    var title = (preview && preview.title) ||
      _t('projectBrain.previewUntitled', 'Untitled') ;
    var first = preview && preview.firstUserMessage;
    var head = '<div class="pb-preview-title">' + _esc(title) + '</div>';
    var idLine = '<div class="pb-preview-id">' + _esc(short) + '</div>';
    var bodyHtml;
    if (first) {
      bodyHtml = '<div class="pb-preview-label">' +
        _esc(_t('projectBrain.previewFirstQuestion', 'First question')) + '</div>' +
        '<div class="pb-preview-body">' + _esc(first) + '</div>';
    } else {
      bodyHtml = '<div class="pb-preview-empty">' +
        _esc(_t('projectBrain.previewEmpty', 'No messages yet')) + '</div>';
    }
    return head + idLine + bodyHtml;
  }

  function _ensurePreviewEl() {
    if (_previewEl) return _previewEl;
    var el = document.createElement('div');
    el.className = 'pb-conv-preview';
    el.setAttribute('role', 'tooltip');
    el.hidden = true;
    document.body.appendChild(el);
    _previewEl = el;
    return el;
  }

  /** Position the (already-populated) preview card near an anchor element. */
  function _positionPreview(anchor) {
    var el = _previewEl;
    if (!el || !anchor) return;
    el.hidden = false;
    var M = 8, GAP = 8;
    var r = anchor.getBoundingClientRect();
    var pw = el.offsetWidth || 300;
    var ph = el.offsetHeight || 120;
    var left = Math.round(r.left);
    var maxLeft = window.innerWidth - pw - M;
    if (left > maxLeft) left = Math.max(M, maxLeft);
    if (left < M) left = M;
    // Prefer above; fall back below when there's no room.
    var top;
    if (r.top - GAP - ph >= M) top = Math.round(r.top - ph - GAP);
    else top = Math.round(r.bottom + GAP);
    el.style.left = left + 'px';
    el.style.top = top + 'px';
  }

  function _hideConvPreview() {
    if (_previewTimer) { clearTimeout(_previewTimer); _previewTimer = null; }
    _previewHoverId = '';
    if (_previewEl) { _previewEl.hidden = true; _previewEl.innerHTML = ''; }
  }

  /** Show the preview for `anchor`'s conv id (fetch + render + position). */
  function _showConvPreview(anchor) {
    var convId = anchor && anchor.getAttribute ? anchor.getAttribute('data-conv-id') : '';
    if (!convId) return;
    _previewHoverId = convId;
    var el = _ensurePreviewEl();
    // Loading state so the card appears instantly on a cold fetch.
    el.innerHTML = '<div class="pb-preview-loading">' +
      _esc(_t('projectBrain.previewLoading', 'Loading…')) + '</div>';
    _positionPreview(anchor);
    _fetchConvPreview(convId).then(function (rec) {
      // A different chip may be hovered by the time the fetch resolves.
      if (_previewHoverId !== convId) return;
      el.innerHTML = buildConvPreviewCard(rec, convId);
      _positionPreview(anchor);
    });
  }

  /** Delegated hover handler: find the nearest [data-conv-id] under the cursor. */
  function _onOverlayHover(ev) {
    var t2 = ev.target;
    var anchor = (t2 && t2.closest) ? t2.closest('[data-conv-id]') : null;
    if (!anchor || !anchor.getAttribute('data-conv-id')) { _hideConvPreview(); return; }
    var convId = anchor.getAttribute('data-conv-id');
    if (convId === _previewHoverId && _previewEl && !_previewEl.hidden) return;
    if (_previewTimer) clearTimeout(_previewTimer);
    _previewTimer = setTimeout(function () { _showConvPreview(anchor); }, 140);
  }

  /** Wire the delegated hover-preview on the overlay once. Idempotent. */
  function _initConvPreview() {
    var overlay = document.getElementById('projectBrainOverlay');
    if (!overlay || overlay._pbPreviewWired) return;
    overlay.addEventListener('mouseover', _onOverlayHover);
    overlay.addEventListener('mouseout', function (ev) {
      // Only hide when leaving the anchor entirely (not moving within it).
      var to = ev.relatedTarget;
      var anchor = (ev.target && ev.target.closest) ? ev.target.closest('[data-conv-id]') : null;
      if (anchor && to && anchor.contains && anchor.contains(to)) return;
      _hideConvPreview();
    });
    overlay._pbPreviewWired = true;
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
    // Prefer the UNtruncated text (payload.summary_full, present only when the
    // display summary was capped at write time) so a long summary expands to
    // its full self instead of a dead mid-word fragment. Short summaries have
    // no summary_full → render as-is with no clamp chrome.
    var fullText = (ev.payload && ev.payload.summary_full) || ev.summary || kindLabel;
    summary.innerHTML = _clampBlock(_esc(fullText), fullText);
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
      // Wire the summary's Show more/less toggle (present only for a clamped,
      // over-threshold summary — short rows have no toggle to bind).
      _wireClampToggles(row);
      if (!fromBackfill) _applyContentI18n(row);
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
      var _al = _activityListEl();
      if (_al) _applyContentI18n(_al);
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
    return escapeHtml(String(s == null ? '' : s));
  }

  // ── Charter column ──────────────────────────────────────────────
  // Renders the north-star content + committed decisions, plus PENDING
  // proposed_decision events (pulled from the live feed state) each with a
  // human commit/reject control — the human gate for commit_charter.
  //
  // The committed north-star + each decision carry HUMAN edit/delete controls
  // (backend-authoritative: every mutation hits the optimistic-locked
  // Api.project.charter* route with the current `version`, then refreshCharter
  // re-renders from the server — never a local DOM mutation). The editor reads
  // the ORIGINAL text from `data-pb-src` (never the translation overlay), so an
  // active content-translation view can never leak into a saved decision.

  /** Small icon+label action button for a charter edit/delete affordance. */
  function _charterActBtn(cls, glyph, labelKey, fallback, extraAttrs) {
    return '<button type="button" class="pb-charter-act ' + cls + '" title="' +
      _esc(_t(labelKey, fallback)) + '"' + (extraAttrs || '') + '>' +
      ((typeof Icon === 'function') ? Icon(glyph, 12) : '') + '</button>';
  }

  function renderCharter(rec, pendingProposals) {
    var el = document.getElementById('projectBrainCharterBody');
    if (!el) return;
    var path = _state.path;
    var version = (rec && typeof rec.version === 'number') ? rec.version : 0;
    var parts = [];
    var content = (rec && rec.content) || '';
    var decisions = (rec && rec.decisions) || [];
    var charterExists = !!(rec && rec.exists) || !!content || !!decisions.length;
    if (!content && !decisions.length && !(pendingProposals || []).length) {
      el.innerHTML = '<div class="pb-charter-empty">' +
        _esc(_t('projectBrain.charterEmpty', 'No charter yet')) + '</div>';
      _setTabCount('pbTabCountCharter', 0);
      return;
    }
    if (content) {
      parts.push('<div class="pb-charter-northstar-row">' +
        '<div class="pb-charter-northstar" data-charter-northstar="1">' +
        _clampBlock(_esc(content), content) + '</div>' +
        '<div class="pb-charter-row-actions">' +
        _charterActBtn('pb-charter-edit-northstar', 'edit',
          'projectBrain.editNorthStar', 'Edit north star',
          ' data-ver="' + version + '"') +
        '</div></div>');
    }
    if (decisions.length) {
      parts.push('<div class="pb-charter-section">' +
        _esc(_t('projectBrain.committedDecisions', 'Committed decisions')) + '</div>');
      parts.push('<ul class="pb-charter-decisions">');
      for (var i = 0; i < decisions.length; i++) {
        var d = decisions[i];
        var txt = (d && typeof d === 'object') ? (d.text || '') : String(d);
        parts.push('<li data-decision-idx="' + i + '">' +
          '<div class="pb-decision-text">' + _clampBlock(_esc(txt), txt) + '</div>' +
          '<div class="pb-charter-row-actions">' +
          _charterActBtn('pb-decision-edit', 'edit',
            'projectBrain.editDecision', 'Edit',
            ' data-idx="' + i + '" data-ver="' + version + '"') +
          _charterActBtn('pb-decision-delete', 'trash',
            'projectBrain.deleteDecision', 'Delete',
            ' data-idx="' + i + '" data-ver="' + version + '"') +
          '</div></li>');
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
          '<div class="pb-proposal-text">' + _clampBlock(_esc(ptext), ptext) + '</div>' +
          '<div class="pb-proposal-actions">' +
          '<button type="button" class="pb-proposal-commit" data-text="' + _esc(ptext) +
          '" data-ver="' + version + '" data-proposal-id="' + _esc(pid) + '">' +
          _esc(_t('projectBrain.commit', 'Commit')) + '</button>' +
          '<button type="button" class="pb-proposal-reject" data-proposal-id="' + _esc(pid) + '">' +
          _esc(_t('projectBrain.reject', 'Reject')) + '</button>' +
          '</div></div>');
      }
    }
    // Footer: delete the WHOLE charter (only when one exists). Two-step
    // confirm inline (no window.confirm) so an accidental click is recoverable.
    if (charterExists) {
      parts.push('<div class="pb-charter-footer">' +
        '<button type="button" class="pb-charter-delete-all" data-ver="' + version + '">' +
        ((typeof Icon === 'function') ? Icon('trash', 12) : '') +
        '<span>' + _esc(_t('projectBrain.deleteCharter', 'Delete charter')) + '</span>' +
        '</button></div>');
    }
    el.innerHTML = parts.join('');
    _wireClampToggles(el);
    _applyContentI18n(el);
    // Charter tab badge = pending proposals awaiting the human (the actionable
    // count), not the committed-decision total.
    _setTabCount('pbTabCountCharter', props.length);
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
    _wireCharterEditControls(el, path);
  }

  /** The ORIGINAL (authoritative) text of a rendered charter block — always
   *  data-pb-src (the source of truth), never the translation overlay that may
   *  be laid over innerHTML. Falls back to textContent when unstamped. */
  function _charterSrcText(rowEl, srcSelector) {
    var node = rowEl.querySelector(srcSelector + ' [data-pb-src]') ||
               rowEl.querySelector(srcSelector);
    if (!node) return '';
    return node.getAttribute('data-pb-src') != null
      ? node.getAttribute('data-pb-src')
      : (node.textContent || '');
  }

  /** Replace a charter row's body with an inline textarea editor (Save /
   *  Cancel). `onSave(newText)` performs the backend mutation. */
  function _openInlineEditor(rowEl, bodySelector, originalText, onSave) {
    if (!rowEl || rowEl.querySelector('.pb-inline-editor')) return;
    var body = rowEl.querySelector(bodySelector);
    var actions = rowEl.querySelector('.pb-charter-row-actions');
    if (body) body.style.display = 'none';
    if (actions) actions.style.display = 'none';
    var ed = document.createElement('div');
    ed.className = 'pb-inline-editor';
    var ta = document.createElement('textarea');
    ta.className = 'pb-inline-editor-input';
    ta.value = originalText;
    var btnRow = document.createElement('div');
    btnRow.className = 'pb-inline-editor-actions';
    var save = document.createElement('button');
    save.type = 'button'; save.className = 'pb-inline-save';
    save.textContent = _t('projectBrain.save', 'Save');
    var cancel = document.createElement('button');
    cancel.type = 'button'; cancel.className = 'pb-inline-cancel';
    cancel.textContent = _t('projectBrain.cancel', 'Cancel');
    btnRow.appendChild(save); btnRow.appendChild(cancel);
    ed.appendChild(ta); ed.appendChild(btnRow);
    rowEl.appendChild(ed);
    try { ta.focus(); } catch (_e) { /* jsdom */ }
    function close() {
      if (ed.parentNode) ed.parentNode.removeChild(ed);
      if (body) body.style.display = '';
      if (actions) actions.style.display = '';
    }
    cancel.addEventListener('click', close);
    save.addEventListener('click', function () {
      var next = (ta.value || '').trim();
      if (!next) { close(); return; }
      save.disabled = true; cancel.disabled = true;
      save.textContent = _t('projectBrain.saving', 'Saving…');
      Promise.resolve(onSave(next)).then(function () {
        // refreshCharter re-renders the whole panel from the server, which
        // removes this editor implicitly; nothing more to do here.
      }).catch(function (e) {
        if (typeof console !== 'undefined') console.warn('[ProjectBrain] charter save failed', e);
        save.disabled = false; cancel.disabled = false;
        save.textContent = _t('projectBrain.save', 'Save');
      });
    });
  }

  /** Wire the north-star edit, per-decision edit/delete, and delete-charter
   *  controls. All mutations go through the optimistic-locked routes carrying
   *  the rendered `version`, then refreshCharter. */
  function _wireCharterEditControls(el, path) {
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    // North-star edit → commitCharter(content=...).
    var nsBtn = el.querySelector('.pb-charter-edit-northstar');
    if (nsBtn) {
      nsBtn.addEventListener('click', function (ev) {
        var ver = parseInt(ev.currentTarget.getAttribute('data-ver') || '0', 10);
        var row = ev.currentTarget.closest('.pb-charter-northstar-row');
        if (!row || !api) return;
        var original = _charterSrcText(row, '.pb-charter-northstar');
        _openInlineEditor(row, '.pb-charter-northstar', original, function (next) {
          return Promise.resolve(api.commitCharter(path, {
            content: next, expected_version: ver,
          })).then(function () { refreshCharter(path); });
        });
      });
    }
    // Per-decision edit.
    var editBtns = el.querySelectorAll('.pb-decision-edit');
    for (var e = 0; e < editBtns.length; e++) {
      editBtns[e].addEventListener('click', function (ev) {
        var btn = ev.currentTarget;
        var idx = parseInt(btn.getAttribute('data-idx') || '-1', 10);
        var ver = parseInt(btn.getAttribute('data-ver') || '0', 10);
        var row = btn.closest('li[data-decision-idx]');
        if (!row || !api || typeof api.updateDecision !== 'function') return;
        var original = _charterSrcText(row, '.pb-decision-text');
        _openInlineEditor(row, '.pb-decision-text', original, function (next) {
          return Promise.resolve(api.updateDecision(path, idx, next, {
            expected_version: ver,
          })).then(function () { refreshCharter(path); });
        });
      });
    }
    // Per-decision delete (two-step inline confirm).
    var delBtns = el.querySelectorAll('.pb-decision-delete');
    for (var dbi = 0; dbi < delBtns.length; dbi++) {
      delBtns[dbi].addEventListener('click', function (ev) {
        var btn = ev.currentTarget;
        var idx = parseInt(btn.getAttribute('data-idx') || '-1', 10);
        var ver = parseInt(btn.getAttribute('data-ver') || '0', 10);
        _confirmInline(btn, function () {
          if (!api || typeof api.deleteDecision !== 'function') return;
          Promise.resolve(api.deleteDecision(path, idx, { expected_version: ver }))
            .then(function () { refreshCharter(path); })
            .catch(function (er) {
              if (typeof console !== 'undefined') console.warn('[ProjectBrain] decision delete failed', er);
            });
        });
      });
    }
    // Delete the whole charter (two-step inline confirm).
    var delAll = el.querySelector('.pb-charter-delete-all');
    if (delAll) {
      delAll.addEventListener('click', function (ev) {
        var btn = ev.currentTarget;
        var ver = parseInt(btn.getAttribute('data-ver') || '0', 10);
        _confirmInline(btn, function () {
          if (!api || typeof api.deleteCharter !== 'function') return;
          Promise.resolve(api.deleteCharter(path, { expected_version: ver }))
            .then(function () { refreshCharter(path); })
            .catch(function (er) {
              if (typeof console !== 'undefined') console.warn('[ProjectBrain] charter delete failed', er);
            });
        });
      });
    }
  }

  /** Two-step inline confirm: first click swaps the button label to a
   *  "Confirm?" affordance; a second click within 4s runs `onConfirm`. Avoids
   *  window.confirm (blocked/ugly in the overlay) while keeping a destructive
   *  action recoverable. */
  function _confirmInline(btn, onConfirm) {
    if (btn._pbConfirmArmed) {
      btn._pbConfirmArmed = false;
      if (btn._pbConfirmTimer) { clearTimeout(btn._pbConfirmTimer); btn._pbConfirmTimer = null; }
      onConfirm();
      return;
    }
    btn._pbConfirmArmed = true;
    btn.classList.add('pb-confirm-armed');
    var label = btn.querySelector('span');
    var prev = label ? label.textContent : null;
    if (label) label.textContent = _t('projectBrain.confirmDelete', 'Confirm?');
    else { btn._pbPrevTitle = btn.title; btn.title = _t('projectBrain.confirmDelete', 'Confirm?'); }
    btn._pbConfirmTimer = setTimeout(function () {
      btn._pbConfirmArmed = false;
      btn.classList.remove('pb-confirm-armed');
      if (label && prev != null) label.textContent = prev;
      else if (btn._pbPrevTitle != null) { btn.title = btn._pbPrevTitle; btn._pbPrevTitle = null; }
    }, 4000);
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
    //   • complete + block on open|claimed (live-work lifecycle)
    //   • reopen on claimed (break a stuck live claim) AND done (revive)
    // (There is no park/shelve control — the project pushes every open epic
    //  forward at full speed rather than holding work pending a human decision.)
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
    // A board epic title can be a multi-sentence design description (stored
    // full, up to 2000 chars). Render it through the clamp so a long title
    // collapses with a Show more/less toggle instead of a wall of text — the
    // full text is always the expandable source (never a clipped fragment).
    var titleHtml = _clampBlock(_esc(t.title), t.title || '');
    return '<div class="pb-board-card pb-board-' + _esc(t.status) + '" data-task-id="' +
      _esc(t.id) + '">' +
      '<div class="pb-board-title">' + titleHtml + '</div>' +
      '<div class="pb-board-card-meta">' + ownerChip + badge + '</div>' +
      actionsRow + '</div>';
  }

  /** Render one path-LEASE row for the Held lane. A lease reserves a
   *  path/subsystem ("hold off editing"); it is NOT an epic, so it shows the
   *  held path + the holder conversation, and offers NO epic lifecycle
   *  actions (complete/block/park). The reservation auto-expires or is
   *  released by its holder — the operator does not manage it here. */
  function _heldCard(t) {
    var owner = t.owner_conv_id || '';
    var ownerChip = owner
      ? '<button type="button" class="pb-conv-chip" data-conv-id="' + _esc(owner) + '">' +
        _esc(owner) + '</button>'
      : '';
    var titleHtml = _clampBlock(_esc(t.title), t.title || '');
    return '<div class="pb-board-card pb-board-held" data-task-id="' +
      _esc(t.id) + '">' +
      '<div class="pb-board-title">' + titleHtml + '</div>' +
      '<div class="pb-board-card-meta">' +
      '<span class="pb-board-held-by">' +
      _esc(_t('projectBrain.heldBy', 'held by')) + '</span> ' + ownerChip +
      '</div></div>';
  }

  /** Render one BLOCKED epic card. The epic hit a genuine external gate and is
   *  on a self-expiring cooldown; the card shows WHY (block reason, which
   *  carries the [human-gated]/[sibling] class tag) and the approximate
   *  retry-in minutes — the answer to "why is nothing happening" that was
   *  invisible before. It still offers reopen (human forces an immediate retry,
   *  resetting the cooldown) and complete. */
  function _blockedCard(t) {
    var mins = Math.max(0, Math.round((Number(t.blocked_until || 0) - Date.now()) / 60000));
    var reason = (t.block_reason || '').trim();
    var cnt = Number(t.block_count || 0);
    var meta = _esc(_t('projectBrain.blockedRetry', 'auto-retry in') + ' ~' + mins + 'm')
      + (cnt ? ' · ' + _esc('' + cnt + '×') : '');
    var reasonHtml = reason
      ? '<div class="pb-board-block-reason">' + _clampBlock(_esc(reason), reason) + '</div>'
      : '';
    var acts = [
      _boardActionBtn('reopen', 'refresh', 'projectBrain.actReopen', 'Reopen'),
      _boardActionBtn('complete', 'check', 'projectBrain.actComplete', 'Done'),
    ];
    var titleHtml = _clampBlock(_esc(t.title), t.title || '');
    return '<div class="pb-board-card pb-board-blocked" data-task-id="' +
      _esc(t.id) + '">' +
      '<div class="pb-board-title">' + titleHtml + '</div>' +
      reasonHtml +
      '<div class="pb-board-card-meta pb-board-block-meta">' + meta + '</div>' +
      '<div class="pb-board-card-actions">' + acts.join('') + '</div></div>';
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
      _setTabCount('pbTabCountBoard', 0);
      return;
    }
    var cols = { open: [], claimed: [], done: [], blocked: [] };
    var held = [];
    var _nowMs = Date.now();
    for (var i = 0; i < tasks.length; i++) {
      var t = tasks[i];
      // A path-LEASE (kind='lease') is a durational resource reservation, not
      // an epic. A LIVE lease carries status='claimed' and goes to a dedicated
      // Held lane — never the Claimed
      // epic lane (it isn't work being advanced) nor the attention badge. An
      // EXPIRED lease is downgraded by read_board to status:'open'/blank-owner;
      // it holds nothing, so it is DROPPED (not rendered as an ownerless ghost)
      // — matching render_board_block's Held filter (kind=='lease' AND
      // status=='claimed').
      if (t.kind === 'lease') { if (t.status === 'claimed') held.push(t); continue; }
      // An epic on a LIVE block cooldown (stored status='open' but
      // blocked_until in the future) goes to its own Blocked lane — NOT the
      // Open lane (where it would read as "claim me"), mirroring the backend
      // render_board_block partition. Once the cooldown lapses (blocked_until
      // <= now) it falls back to Open automatically.
      if (t.status === 'open' && Number(t.blocked_until || 0) > _nowMs) {
        cols.blocked.push(t); continue;
      }
      (cols[t.status] || cols.open).push(t);
    }
    // Board badge = live epics needing attention (open + claimed), not done,
    // blocked (waiting on a gate), or path leases.
    _setTabCount('pbTabCountBoard', cols.open.length + cols.claimed.length);
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
    // Held lane (path leases) — rendered only when non-empty, its own lane so
    // the operator never mistakes a reservation for a claimed epic.
    var heldLane = '';
    if (held.length) {
      var heldCards = held.map(_heldCard).join('');
      heldLane = '<div class="pb-board-lane pb-board-lane-held">' +
        '<div class="pb-board-lane-head">' +
        ((typeof Icon === 'function') ? Icon('lock', 12) : '') +
        ' ' + _esc(_t('projectBrain.laneHeld', 'Held (do not edit)')) +
        ' <span class="pb-board-count">' + held.length + '</span></div>' +
        heldCards + '</div>';
    }
    // Blocked lane (epics on a live cooldown) — rendered only when non-empty,
    // its own lane with the block reason + retry-in so the human can see WHY
    // an epic is stalled (the answer to "why is nothing happening").
    var blockedLane = '';
    if (cols.blocked.length) {
      var blockedCards = cols.blocked.map(_blockedCard).join('');
      blockedLane = '<div class="pb-board-lane pb-board-lane-blocked">' +
        '<div class="pb-board-lane-head">' +
        ((typeof Icon === 'function') ? Icon('ban', 12) : '') +
        ' ' + _esc(_t('projectBrain.laneBlocked', 'Blocked (waiting on a gate)')) +
        ' <span class="pb-board-count">' + cols.blocked.length + '</span></div>' +
        blockedCards + '</div>';
    }
    el.innerHTML =
      '<div class="pb-board-toolbar">' + newBtn + '</div>' +
      lane('open', 'projectBrain.laneOpen') +
      lane('claimed', 'projectBrain.laneClaimed') +
      blockedLane +
      heldLane +
      lane('done', 'projectBrain.laneDone');
    // conv-chip click → open that conversation
    var chips = el.querySelectorAll('.pb-conv-chip');
    for (var c = 0; c < chips.length; c++) {
      chips[c].addEventListener('click', function (ev) {
        var cid = ev.currentTarget.getAttribute('data-conv-id');
        if (cid && typeof loadConversation === 'function') loadConversation(cid);
      });
    }
    // Board titles render through _clampBlock (long epics collapse with a
    // Show more/less toggle) — bind those toggles for the whole board.
    _wireClampToggles(el);
    _applyContentI18n(el);
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
        cparts.push('<div class="pb-inf-northstar">' +
          _clampBlock(_esc(charter.content), charter.content) + '</div>');
      }
      var decs = charter.decisions || [];
      if (decs.length) {
        cparts.push('<ul class="pb-inf-decisions">');
        for (var i = 0; i < Math.min(decs.length, 6); i++) {
          cparts.push('<li>' + _clampBlock(_esc(decs[i]), String(decs[i])) + '</li>');
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
    _wireClampToggles(body);
    _applyContentI18n(body);

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
        _refreshPeers(path);
        _refreshStatus(path);
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
    _initTabs();
    _initConvPreview();
    if (typeof ProjectBrainI18n !== 'undefined' && ProjectBrainI18n &&
        typeof ProjectBrainI18n.initToggle === 'function') {
      try { ProjectBrainI18n.initToggle(); } catch (_e) { /* best-effort */ }
    }
    _selectTab(_state.tab || 'charter');
    var path = _displayedProjectPath();
    if (path) {
      openFeed(path);
      refreshCharter(path);
      refreshBoard(path);
      refreshInfluence(path);
      _refreshPeers(path);
      _refreshStatus(path);
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

  /** Drive the Status tab (project-brain-status.js) if it's loaded. */
  function _refreshStatus(path) {
    if (typeof window.ProjectBrainStatus !== 'undefined' &&
        window.ProjectBrainStatus &&
        typeof window.ProjectBrainStatus.refreshStatus === 'function') {
      window.ProjectBrainStatus.refreshStatus(path);
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
    _hideConvPreview();
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
      _refreshStatus(path);
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
    buildConvPreviewCard: buildConvPreviewCard,
    _fetchConvPreview: _fetchConvPreview,
    _showConvPreview: _showConvPreview,
    _hideConvPreview: _hideConvPreview,
    _initConvPreview: _initConvPreview,
    _onPush: _onPush,
    _state: _state,
    _boardConvId: _boardConvId,
  };
  window.toggleProjectBrain = toggleProjectBrain;
  window.openProjectBrain = openProjectBrain;
  window.openProjectBrainInfluence = openProjectBrainInfluence;
  window.closeProjectBrain = closeProjectBrain;
  window.projectBrainRefresh = projectBrainRefresh;
})();

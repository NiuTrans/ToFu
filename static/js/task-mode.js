/* ═══════════════════════════════════════════════════════════════════
   task-mode.js — Task Mode (durable orchestration run viewer)

   The operating surface for DURABLE run instances (see
   docs/proposals/TASK_MODE.md §5). Where orchestration.js is the
   AUTHORING canvas (compose a flow template), Task Mode is where you
   watch and reopen a flow's actual EXECUTIONS — runs that survive a
   reload/restart because every event is persisted to the DB.

   ── Layout (three panes, TASK_MODE.md §5) ───────────────────────────
   • Left rail   — the run list (Api.orchestrations.taskList).
   • Center      — the flow GRAPH (read-only DAG of the pinned definition
                   with the active node highlighted) above the live run
                   TIMELINE (Api.orchestrations.taskEvents, cursor-polled
                   so LIVE and REPLAY share one path) + the final result.
   • Right pane  — the INSPECTOR: active-node detail and interactive
                   human-in-the-loop gates (approve / reject / input).
                   A run blocked on a gate is `paused`; resolving it via
                   the existing human-approve / human-input endpoints
                   unblocks the engine thread.

   Per-item / candidate dashboards (§3.4) remain a later phase.

   ── House rule ──────────────────────────────────────────────────────
   SVG-only, no emoji even for abstract concepts (TASK_MODE.md §5.1).
   Reuses the _ORCH_ICONS / _ORCH_GLYPHS glyph vocabulary from
   orchestration.js (same bundle; referenced at runtime so load order is
   irrelevant). Per CLAUDE.md §3.2.0 all backend calls go through
   window.Api.* — this file issues no raw fetch.
   ═══════════════════════════════════════════════════════════════════ */

var _tmModalReady = false;
var _tmRunId = null;        // currently-open run; guards stale poll callbacks
var _tmPolling = false;
var _tmRuns = [];
var _tmLoadError = false;   // true when the last taskList() failed (vs empty)
var _tmPollErrs = 0;        // consecutive event-poll failures (drives backoff)
var _tmDef = null;          // pinned definition of the open run (for the DAG)
var _tmActiveNode = null;   // node_id of the currently-running step (highlight)
var _tmDoneNodes = {};      // node_id → true, steps that have completed
var _tmGates = {};          // request_id → gate event, pending human gates
// Per-node run trace, keyed by node_id, rebuilt from the durable ``step_trace``
// events (which carry the resolved brief + bounded input + full output). This
// is what makes a REOPENED run's data flow legible — clicking a graph node
// shows what it saw + produced. A node that ran multiple times (loop body)
// keeps the LATEST trace; ``_tmTraceCount`` tracks how many times it ran.
var _tmTrace = {};          // node_id → latest step_trace entry
var _tmTraceCount = {};     // node_id → run count
var _tmSelNode = null;      // node_id the user clicked to inspect (pins the trace)

function _tmIco(name) {
  // _ORCH_ICONS lives in orchestration.js (same bundle). Fall back to an
  // empty string if it somehow isn't present so the line still renders.
  return (typeof _ORCH_ICONS !== 'undefined' && _ORCH_ICONS[name]) || '';
}

// Localized string lookup. Delegates to the global i18n `t()` (core bundle,
// loaded before this deferred file). Falls back to the raw key if i18n isn't
// present so a string never renders blank.
function _tmT(key, params) {
  return (typeof t === 'function') ? t(key, params) : key;
}

// ── Node identity helpers (share the Studio catalogues so a graph node
//    looks identical to its canvas card: same mascot avatar, accent, glyph) ──

function _tmRoleDef(role) {
  if (typeof _ORCH_ROLES === 'undefined') return null;
  return _ORCH_ROLES.filter(function (r) { return r.role === role; })[0] || null;
}

function _tmControlDef(kind) {
  if (typeof _ORCH_CONTROLS === 'undefined') return null;
  // 'barrier' is authored as kind 'barrier' but labelled Join.
  return _ORCH_CONTROLS.filter(function (c) { return c.kind === kind; })[0] || null;
}

// Accent color for a node — matches the canvas: roles share the violet
// agent accent, controls carry their catalogue accent.
function _tmNodeAccent(n) {
  if (n.type === 'role') return '#6e56cf';
  if (n.type === 'subflow') return '#8b5cf6';
  var cdef = _tmControlDef(n.kind);
  return (cdef && cdef.accent) || 'var(--text-tertiary)';
}

// Icon HTML for a node — role nodes get the tofu mascot avatar (like the
// canvas), control nodes get their inline SVG glyph.
function _tmNodeIconHtml(n) {
  if (n.type === 'role') {
    var rdef = _tmRoleDef(n.role);
    if (rdef && typeof _orchIconSrc === 'function') {
      return '<img src="' + _orchIconSrc(rdef.icon) + '" alt="" onerror="this.style.display=\'none\'">';
    }
    return _tmIco('bot');
  }
  if (n.type === 'subflow') {
    return (typeof _ORCH_GLYPHS !== 'undefined' && _ORCH_GLYPHS.group) || _tmIco('bot');
  }
  var g = (typeof _ORCH_GLYPHS !== 'undefined') ? _ORCH_GLYPHS : {};
  var byKind = { start: 'play', stop: 'stop', loop: 'loop', parallel: 'fanout',
                 barrier: 'join', join: 'join', branch: 'branch',
                 artifact: 'artifact', human: 'human' };
  return g[byKind[n.kind] || ''] || _tmIco('bot');
}

// Small inline glyph (1em) for timeline/inspector use.
function _tmNodeGlyph(n) {
  var g = (typeof _ORCH_GLYPHS !== 'undefined') ? _ORCH_GLYPHS : {};
  if (n.type === 'role' || n.type === 'subflow') return _tmIco('bot');
  var byKind = { start: 'play', stop: 'stop', loop: 'loop', parallel: 'fanout',
                 barrier: 'join', join: 'join', branch: 'branch',
                 artifact: 'artifact', human: 'human' };
  return g[byKind[n.kind] || ''] || _tmIco('bot');
}

function _tmNodeLabel(n) {
  if (n.name) return n.name;
  if (n.type === 'role') { var r = _tmRoleDef(n.role); return r ? r.label : (n.role || 'agent'); }
  var c = _tmControlDef(n.kind); return c ? c.label : (n.kind || n.id || '?');
}

function _tmNodeSub(n) {
  if (n.type === 'role') {
    var p = n.params || {};
    return (p.tier || 'standard') + ' · ' + (p.isolation || 'fresh');
  }
  if (n.type === 'subflow') return _tmT('tm.sub.subflow');
  var k = n.kind, pp = n.params || {};
  if (k === 'loop') return _tmT('tm.sub.max') + ' ' + (pp.max_iterations || 10);
  if (k === 'parallel') return _tmT('tm.sub.fanout');
  if (k === 'branch') return (pp.branches || 2) + ' ' + _tmT('tm.sub.routes');
  if (k === 'artifact') return pp.path || 'deliverable';
  if (k === 'human') return ({ approve: _tmT('tm.sub.approvalGate'), input: _tmT('tm.sub.collectInput'), notify: _tmT('tm.sub.notify') })[pp.mode] || _tmT('tm.sub.gate');
  if (k === 'start') return _tmT('tm.sub.startInput');
  if (k === 'stop') return _tmT('tm.sub.stopResult');
  return k || '';
}

function _tmEsc(s) {
  return (typeof escapeHtml === 'function') ? escapeHtml(s == null ? '' : s) : String(s == null ? '' : s);
}

function _tmAgo(ms) {
  if (!ms) return '';
  var d = Date.now() - ms;
  if (d < 0) d = 0;
  var s = Math.floor(d / 1000);
  if (s < 60) return s + 's ago';
  var m = Math.floor(s / 60);
  if (m < 60) return m + 'm ago';
  var h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}

// ── Entry / exit ────────────────────────────────────────────────────

function openTaskMode() {
  _tmEnsureModal();
  var ov = document.getElementById('taskModeModal');
  if (ov) ov.style.display = 'flex';
  _tmRefreshRuns();
}

function closeTaskMode(evt) {
  var ov = document.getElementById('taskModeModal');
  if (!ov) return;
  if (evt && evt.target !== ov) return;   // ignore clicks inside the dialog
  ov.style.display = 'none';
  _tmRunId = null;                          // stop the poll loop on close
  _tmPolling = false;
}

function _tmEnsureModal() {
  if (_tmModalReady) return;
  _tmInjectStyles();

  var ov = document.createElement('div');
  ov.className = 'tm-overlay';
  ov.id = 'taskModeModal';
  ov.style.display = 'none';
  ov.addEventListener('click', function (e) { closeTaskMode(e); });

  ov.innerHTML = ''
    + '<div class="tm-shell" role="dialog" aria-label="Task Mode">'
    +   '<div class="tm-top">'
    +     '<div class="tm-top-left">'
    +       '<span class="tm-top-glyph">' + _tmIco('rocket') + '</span>'
    +       '<span class="tm-top-name">' + _tmT('tm.top.name') + '</span>'
    +       '<span class="tm-top-sub">' + _tmT('tm.top.sub') + '</span>'
    +     '</div>'
    +     '<div class="tm-top-actions">'
    +       '<button class="tm-btn" onclick="_tmOpenStudio()" title="' + _tmT('tm.btn.studio') + '">' + _tmIco('layout') + ' ' + _tmT('tm.btn.studio') + '</button>'
    +       '<button class="tm-btn" onclick="_tmRefreshRuns()" title="' + _tmT('tm.btn.refresh') + '">' + _tmIco('loop') + ' ' + _tmT('tm.btn.refresh') + '</button>'
    +       '<button class="tm-btn tm-btn-close" onclick="closeTaskMode()" title="' + _tmT('tm.tip.close') + '">' + _tmIco('reject') + '</button>'
    +     '</div>'
    +   '</div>'
    +   '<div class="tm-body">'
    +     '<div class="tm-rail"><div class="tm-rail-head">' + _tmT('tm.rail.runs') + '</div>'
    +       '<div class="tm-rail-list" id="tmRunList"></div></div>'
    +     '<div class="tm-main">'
    +       '<div class="tm-main-head" id="tmRunTitle">'
    +         '<div class="tm-empty">' + _tmIco('rocket') + ' ' + _tmT('tm.select') + '</div></div>'
    +       '<div class="tm-graph" id="tmGraph"></div>'
    +       '<div class="tm-stream"><div class="tm-stream-head">' + _tmT('tm.stream.timeline') + '</div>'
    +         '<div class="tm-timeline" id="tmTimeline"></div></div>'
    +       '<div class="tm-final" id="tmFinal" style="display:none"></div>'
    +     '</div>'
    +     '<div class="tm-inspector" id="tmInspector">'
    +       '<div class="tm-insp-head">' + _tmT('tm.inspector') + '</div>'
    +       '<div class="tm-insp-body" id="tmInspBody">'
    +         '<div class="tm-insp-empty">' + _tmIco('eye') + '<div>' + _tmT('tm.insp.empty') + '</div></div></div>'
    +     '</div>'
    +   '</div>'
    + '</div>';

  document.body.appendChild(ov);
  _tmModalReady = true;
}

// ── Left rail: run list ─────────────────────────────────────────────

async function _tmRefreshRuns() {
  var list = document.getElementById('tmRunList');
  if (list && !_tmRuns.length) {
    list.innerHTML = '<div class="tm-loading"><span class="tm-spin"></span>' + _tmT('tm.loading') + '</div>';
  }
  var res = await Api.orchestrations.taskList();
  // taskList() resolves null on a network/5xx failure (onError:'null') but a
  // real, genuinely-empty account returns {ok:true, runs:[]}. Distinguish the
  // two so a transient failure doesn't masquerade as "No runs yet".
  if (res === null) { _tmLoadError = true; _tmRenderRunList(); return; }
  _tmLoadError = false;
  _tmRuns = (res && res.ok && res.runs) || [];
  _tmRenderRunList();
}

// Elapsed / duration label for a run: live runs show "running Xs", terminal
// runs show their total wall time. Keeps the list scannable at a glance.
function _tmDuration(r) {
  var start = r.created_at || 0;
  if (!start) return '';
  var end = _tmIsTerminal(r.status) ? (r.finished_at || r.updated_at || 0) : Date.now();
  if (!end || end < start) return '';
  var s = Math.round((end - start) / 1000);
  var label = s < 60 ? (s + 's')
            : s < 3600 ? (Math.floor(s / 60) + 'm ' + (s % 60) + 's')
            : (Math.floor(s / 3600) + 'h ' + Math.floor((s % 3600) / 60) + 'm');
  return (_tmIsTerminal(r.status) ? '' : (_tmT('tm.dur.running') + ' · ')) + label;
}

function _tmRenderRunList() {
  var list = document.getElementById('tmRunList');
  if (!list) return;
  if (_tmLoadError) {
    list.innerHTML = '<div class="tm-state tm-state-err">'
      + _tmIco('warn')
      + '<div class="tm-state-title">' + _tmT('tm.err.title') + '</div>'
      + '<div class="tm-state-sub">' + _tmT('tm.err.sub') + '</div>'
      + '<button class="tm-btn tm-state-btn" onclick="_tmRefreshRuns()">' + _tmIco('loop') + ' ' + _tmT('tm.btn.retry') + '</button>'
      + '</div>';
    return;
  }
  if (!_tmRuns.length) {
    list.innerHTML = '<div class="tm-state">'
      + _tmIco('rocket')
      + '<div class="tm-state-title">' + _tmT('tm.empty.title') + '</div>'
      + '<div class="tm-state-sub">' + _tmT('tm.empty.sub') + '</div>'
      + '<button class="tm-btn tm-btn-primary tm-state-btn" onclick="_tmOpenStudio()">' + _tmIco('layout') + ' ' + _tmT('tm.btn.openStudio') + '</button>'
      + '</div>';
    return;
  }
  list.innerHTML = _tmRuns.map(function (r) {
    var active = (r.id === _tmRunId) ? ' is-active' : '';
    var live = _tmIsTerminal(r.status) ? '' : ' tm-run-live';
    var dur = _tmDuration(r);
    return '<button class="tm-run' + active + live + '" onclick="_tmOpenRun(\'' + _tmEsc(r.id) + '\')">'
      + '<div class="tm-run-top"><span class="tm-run-name">' + _tmEsc(r.name || '(unnamed flow)') + '</span>'
      + _tmStatusChip(r.status) + '</div>'
      + '<div class="tm-run-meta">' + _tmEsc(_tmAgo(r.created_at))
      +   (dur ? '<span class="tm-run-dur">' + _tmEsc(dur) + '</span>' : '')
      + '</div>'
      + '</button>';
  }).join('');
}

// Bridge to the authoring surface. Task Mode is the OPERATING room; the Studio
// is where flows are designed/edited. Leaving Task Mode open under the Studio
// modal would stack two overlays, so we close this one first.
function _tmOpenStudio(orchId) {
  if (typeof openOrchestration !== 'function') {
    if (typeof _orchToast === 'function') _orchToast(_tmT('tm.studioUnavailable'), true);
    return;
  }
  closeTaskMode();
  openOrchestration();
  // If a specific template id was passed (Edit-in-Studio from a run), load it.
  if (orchId && typeof _orchLoadFromStore === 'function') {
    _orchLoadFromStore(orchId);
  }
}

function _tmStatusChip(status) {
  var s = status || 'pending';
  var lbl = _tmT('tm.status.' + s);
  if (lbl === 'tm.status.' + s) lbl = s;   // unknown status → show raw
  return '<span class="tm-chip tm-chip-' + _tmEsc(s) + '" title="' + _tmEsc(s) + '">' + _tmEsc(lbl) + '</span>';
}

// ── Center: open + poll one run ─────────────────────────────────────

async function _tmOpenRun(runId) {
  if (!runId) return;
  _tmRunId = runId;
  _tmPolling = false;
  _tmPollErrs = 0;
  _tmDef = null;
  _tmActiveNode = null;
  _tmDoneNodes = {};
  _tmGates = {};
  _tmTrace = {};
  _tmTraceCount = {};
  _tmSelNode = null;
  _tmRenderRunList();   // re-highlight active

  var tl = document.getElementById('tmTimeline');
  if (tl) tl.innerHTML = '';
  var fin = document.getElementById('tmFinal');
  if (fin) { fin.style.display = 'none'; fin.innerHTML = ''; }
  var g = document.getElementById('tmGraph');
  if (g) g.innerHTML = '';
  _tmRenderInspector();

  var res = await Api.orchestrations.taskGet(runId);
  var run = (res && res.ok && res.run) || null;
  if (!run) { _tmRenderTitle(null); return; }
  if (_tmRunId !== runId) return;   // user switched away while awaiting
  _tmRenderTitle(run);
  _tmDef = run.definition || null;
  _tmRenderGraph();

  _tmPolling = true;
  _tmPoll(runId, 0);
}

function _tmRenderTitle(run) {
  var head = document.getElementById('tmRunTitle');
  if (!head) return;
  if (!run) { head.innerHTML = '<div class="tm-empty">' + _tmT('tm.runNotFound') + '</div>'; return; }
  var editBtn = run.orch_id
    ? '<button class="tm-btn tm-btn-ghost" onclick="_tmOpenStudio(\'' + _tmEsc(run.orch_id) + '\')" title="' + _tmT('tm.btn.editStudio') + '">' + _tmIco('layout') + ' ' + _tmT('tm.btn.editStudio') + '</button>'
    : '';
  head.innerHTML = ''
    + '<div class="tm-title-row">'
    +   '<span class="tm-title-name">' + _tmEsc(run.name || '(unnamed flow)') + '</span>'
    +   _tmStatusChip(run.status)
    +   '<span class="tm-title-spacer"></span>'
    +   editBtn
    +   (_tmIsTerminal(run.status)
          ? '<button class="tm-btn tm-btn-ghost tm-btn-danger" onclick="_tmDeleteRun(\'' + _tmEsc(run.id) + '\')" title="' + _tmT('tm.delete.confirmTitle') + '">' + _tmIco('reject') + ' ' + _tmT('tm.btn.delete') + '</button>'
          : '<button class="tm-btn tm-btn-ghost" onclick="_tmAbortRun(\'' + _tmEsc(run.id) + '\')" title="' + _tmT('tm.abort.confirmTitle') + '">' + _tmIco('stop') + ' ' + _tmT('tm.btn.abort') + '</button>')
    + '</div>'
    + (run.input ? '<div class="tm-title-input">' + _tmEsc(run.input.slice(0, 300)) + '</div>' : '');
}

function _tmIsTerminal(status) {
  return status === 'done' || status === 'error' || status === 'aborted';
}

async function _tmPoll(runId, cursor) {
  if (_tmRunId !== runId || !_tmPolling) return;   // stale / closed
  // Pause polling while the tab is hidden — no point streaming into a
  // backgrounded view. Resume on the next visibility tick (a cheap re-check
  // loop rather than a visibilitychange listener that must be torn down).
  if (typeof document !== 'undefined' && document.hidden) {
    setTimeout(function () { _tmPoll(runId, cursor); }, 1500);
    return;
  }
  var res = await Api.orchestrations.taskEvents(runId, cursor);
  if (_tmRunId !== runId || !_tmPolling) return;
  if (!res || !res.ok) {
    // A transient failure must NOT kill the live view — back off and retry,
    // surfacing a reconnecting notice only once so the timeline isn't spammed.
    _tmPollErrs++;
    if (_tmPollErrs === 1) _tmLine(_tmIco('loop') + ' ' + _tmT('tm.line.reconnecting'), 'is-err');
    if (_tmPollErrs > 12) {   // ~give up after sustained failure (~1 min)
      _tmLine(_tmIco('warn') + ' ' + _tmT('tm.line.offline'), 'is-err');
      _tmPolling = false;
      return;
    }
    var backoff = Math.min(800 * _tmPollErrs, 6000);
    setTimeout(function () { _tmPoll(runId, cursor); }, backoff);
    return;
  }
  if (_tmPollErrs) { _tmLine(_tmIco('check') + ' ' + _tmT('tm.line.reconnected'), 'is-done'); _tmPollErrs = 0; }
  (res.events || []).forEach(_tmRenderEvent);
  _tmSyncChip(res.status);

  if (res.done) {
    _tmPolling = false;
    _tmActiveNode = null;
    _tmRenderGraph();
    _tmShowFinal(runId);
    return;
  }
  setTimeout(function () { _tmPoll(runId, res.next_cursor); }, 800);
}

// Keep the title + rail status chips in sync as a live run progresses.
function _tmSyncChip(status) {
  if (!status) return;
  var run = _tmRuns.filter(function (r) { return r.id === _tmRunId; })[0];
  if (run && run.status !== status) { run.status = status; _tmRenderRunList(); }
  var head = document.getElementById('tmRunTitle');
  var chip = head && head.querySelector('.tm-chip');
  if (chip) {
    var lbl = _tmT('tm.status.' + status);
    if (lbl === 'tm.status.' + status) lbl = status;
    if (chip.textContent !== lbl) {
      chip.className = 'tm-chip tm-chip-' + status;
      chip.title = status;
      chip.textContent = lbl;
    }
  }
}

async function _tmShowFinal(runId) {
  var res = await Api.orchestrations.taskGet(runId);
  var run = (res && res.ok && res.run) || null;
  if (_tmRunId !== runId || !run) return;
  _tmRenderTitle(run);   // flip Abort→Delete, refresh status
  var fin = document.getElementById('tmFinal');
  if (fin && run.final) {
    fin.style.display = '';
    fin.innerHTML = '<div class="tm-final-label">' + _tmT('tm.final.result') + '</div>'
      + '<pre class="tm-final-pre">' + _tmEsc(run.final.slice(0, 8000)) + '</pre>';
  }
}

// ── Center top: read-only flow graph (DAG) ──────────────────────────
//
// Renders the pinned definition's nodes/edges as a static SVG, scaled to
// fit. The node whose step is currently running is highlighted. This is a
// VIEW of orchestration.js's canvas — not the editor — so it owns its own
// lightweight layout math rather than the editor's DOM-measuring path.

var _TM_NODE_W = 168;
var _TM_NODE_H = 56;

function _tmRenderGraph() {
  var host = document.getElementById('tmGraph');
  if (!host) return;
  var nodes = (_tmDef && _tmDef.nodes) || [];
  if (!nodes.length) { host.style.display = 'none'; host.innerHTML = ''; return; }
  host.style.display = '';

  // Bounds in definition coordinate space (node pos = top-left of card).
  var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  var posOf = function (n) {
    var p = n.pos || {};
    return { x: (typeof p.x === 'number') ? p.x : 20, y: (typeof p.y === 'number') ? p.y : 20 };
  };
  nodes.forEach(function (n) {
    var p = posOf(n);
    minX = Math.min(minX, p.x); minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x + _TM_NODE_W); maxY = Math.max(maxY, p.y + _TM_NODE_H);
  });
  var pad = 24;
  var vw = (maxX - minX) + pad * 2;
  var vh = (maxY - minY) + pad * 2;
  var byId = {};
  nodes.forEach(function (n) { byId[n.id] = n; });

  var sx = function (x) { return (x - minX) + pad; };
  var sy = function (y) { return (y - minY) + pad; };

  var parts = '<defs><marker id="tmArrow" viewBox="0 0 12 12" refX="9.5" refY="6" '
    + 'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
    + '<path class="tm-edge-arrow" d="M1 1 L11 6 L1 11 L4 6 Z"></path></marker></defs>';

  // Edges: source bottom-center → target top-center, vertical-ish bezier.
  ((_tmDef && _tmDef.edges) || []).forEach(function (e) {
    var a = byId[e.from], b = byId[e.to];
    if (!a || !b) return;
    var pa = posOf(a), pb = posOf(b);
    var ax = sx(pa.x + _TM_NODE_W / 2), ay = sy(pa.y + _TM_NODE_H);
    var bx = sx(pb.x + _TM_NODE_W / 2), by = sy(pb.y);
    var dy = by - ay, d;
    if (dy >= 24) {
      var v = dy * 0.5;
      d = 'M ' + ax + ' ' + ay + ' C ' + ax + ' ' + (ay + v) + ' ' + bx + ' ' + (by - v) + ' ' + bx + ' ' + by;
    } else {
      var dx = bx - ax, side = dx >= 0 ? 1 : -1;
      var h = Math.max(50, Math.abs(dx) * 0.5), vv = Math.max(34, Math.abs(dy) * 0.5);
      d = 'M ' + ax + ' ' + ay + ' C ' + (ax + side * h) + ' ' + (ay + vv) + ' '
        + (bx + side * h) + ' ' + (by - vv) + ' ' + bx + ' ' + by;
    }
    parts += '<path class="tm-edge" marker-end="url(#tmArrow)" d="' + d + '"></path>';
  });

  // Nodes as foreignObject cards — mirror the Studio canvas card: left
  // accent stripe, mascot avatar (roles) / glyph (controls), title + sub,
  // INPUT/RESULT ribbons on start/stop, active-node highlight.
  nodes.forEach(function (n) {
    var p = posOf(n);
    var x = sx(p.x), y = sy(p.y);
    var act = (n.id === _tmActiveNode) ? ' is-active' : '';
    var done = (_tmDoneNodes[n.id]) ? ' is-done' : '';
    var sel = (n.id === _tmSelNode) ? ' is-selected' : '';
    var hasTrace = _tmTrace[n.id] ? ' tm-gnode-traced' : '';
    var typeCls = (n.type === 'role') ? ' tm-gnode-role'
                : (n.type === 'subflow') ? ' tm-gnode-sub' : ' tm-gnode-ctrl';
    var accent = _tmNodeAccent(n);
    var ribbon = (n.kind === 'start') ? '<span class="tm-gnode-ribbon">' + _tmT('tm.ribbon.input') + '</span>'
               : (n.kind === 'stop') ? '<span class="tm-gnode-ribbon tm-gnode-ribbon-out">' + _tmT('tm.ribbon.result') + '</span>' : '';
    var nidArg = "'" + String(n.id).replace(/'/g, "\\'") + "'";
    parts += '<foreignObject x="' + x + '" y="' + y + '" width="' + _TM_NODE_W + '" height="' + _TM_NODE_H + '">'
      + '<div xmlns="http://www.w3.org/1999/xhtml" class="tm-gnode' + typeCls + act + done + sel + hasTrace + '" style="--tm-accent:' + accent + '"'
      +   ' onclick="_tmSelectNode(' + nidArg + ')" title="' + _tmT('tm.tip.inspectNode') + '">'
      +   ribbon
      +   '<span class="tm-gnode-ico">' + _tmNodeIconHtml(n) + '</span>'
      +   '<span class="tm-gnode-text"><span class="tm-gnode-label">' + _tmEsc(_tmNodeLabel(n)) + '</span>'
      +     '<span class="tm-gnode-sub">' + _tmEsc(_tmNodeSub(n)) + '</span></span>'
      + '</div></foreignObject>';
  });

  // Render at NATURAL pixel size (nodes stay full-size + readable) and let the
  // pane scroll — mirrors the Studio canvas. preserveAspectRatio="meet" on a
  // 100%-sized SVG would shrink a tall flow into a tiny sliver, which reads as
  // broken; fixed px + scroll is the right model for a node graph.
  var W = Math.max(1, Math.round(vw)), H = Math.max(1, Math.round(vh));
  host.innerHTML = '<svg class="tm-graph-svg" width="' + W + '" height="' + H
    + '" viewBox="0 0 ' + W + ' ' + H + '">' + parts + '</svg>';
}

// ── Timeline rendering ──────────────────────────────────────────────

function _tmLine(html, cls) {
  var tl = document.getElementById('tmTimeline');
  if (!tl) return;
  // Respect a user who has scrolled up to read earlier events — only pin to
  // the bottom when they were already there (within a small threshold).
  var atBottom = (tl.scrollHeight - tl.scrollTop - tl.clientHeight) < 40;
  var row = document.createElement('div');
  row.className = 'tm-line' + (cls ? ' ' + cls : '');
  row.innerHTML = html;
  tl.appendChild(row);
  if (atBottom) tl.scrollTop = tl.scrollHeight;
}

// Maps the engine event vocabulary (lib/orchestration_engine.py) to
// timeline rows AND drives the graph highlight + the inspector's human
// gates. Mirrors orchestration.js::_orchRenderRunEvent but emits into the
// Task Mode surface (interactive gates live in the right inspector here).
function _tmRenderEvent(ev) {
  var dim = function (s, n) { return s ? ' <span class="tm-dim">' + _tmEsc((s || '').slice(0, n || 120)) + '</span>' : ''; };
  switch (ev.type) {
    case 'flow_start':
      _tmLine(_tmIco('flag') + ' <b>' + _tmEsc(ev.name || _tmT('tm.ev.flowFallback')) + '</b> — ' + _tmT('tm.ev.flowNodes', { n: (ev.nodes || 0) })); break;
    case 'step_start':
      _tmActiveNode = ev.node_id || null; _tmRenderGraph(); _tmRenderInspector(ev);
      _tmLine(_tmIco('bot') + ' ' + _tmT('tm.line.stepRunning', { name: _tmEsc(ev.name || ev.role) }), 'is-active'); break;
    case 'step_complete':
      if (ev.node_id) { _tmDoneNodes[ev.node_id] = true; if (_tmActiveNode === ev.node_id) _tmActiveNode = null; _tmRenderGraph(); }
      _tmLine(_tmIco('check') + ' ' + _tmEsc(ev.role) + dim(ev.preview)); break;
    case 'step_trace':
      // Durable per-node trace — the resolved brief + bounded input + full
      // output. Powers the inspector's "Trace" section so a REOPENED run's
      // data flow is legible (not just a timeline of previews).
      if (ev.node_id) {
        _tmTrace[ev.node_id] = ev;
        _tmTraceCount[ev.node_id] = (_tmTraceCount[ev.node_id] || 0) + 1;
        // Refresh the inspector if the user is currently inspecting this node.
        if (_tmSelNode === ev.node_id) _tmRenderInspector();
      }
      break;
    case 'loop_iteration':
      _tmActiveNode = ev.node_id || _tmActiveNode; _tmRenderGraph();
      _tmLine(_tmIco('loop') + ' ' + _tmT('tm.ev.loopIter', { i: ev.iteration, max: ev.max })); break;
    case 'zero_deliverable_guard':
      _tmLine(_tmIco('warn') + ' ' + _tmT('tm.ev.zeroGuard'), 'is-err'); break;
    case 'replan':
      _tmLine(_tmIco('compass') + ' ' + _tmT('tm.ev.replan', { n: ev.replan }) + dim(ev.defect, 100)); break;
    case 'stuck_detected':
      _tmLine(_tmIco('loop') + ' ' + _tmT('tm.ev.stuck'), 'is-err'); break;
    case 'parallel_start':
      _tmLine(_tmIco('fanout') + ' ' + _tmT('tm.ev.fanout', { n: ev.branches })); break;
    case 'branch_pick':
      _tmLine(_tmIco('branch') + ' ' + _tmT('tm.ev.route', { name: _tmEsc(ev.chosen || _tmT('tm.ev.none')) })); break;
    case 'artifact_declared':
      _tmLine(_tmIco('package') + ' ' + _tmT('tm.ev.deliverable') + '<b>' + _tmEsc(ev.path || ev.name || _tmT('tm.ev.unnamed')) + '</b>' + dim(ev.description)); break;
    case 'human_notify':
      _tmLine(_tmIco('person') + ' <b>' + _tmEsc(ev.name || _tmT('tm.gate.who')) + '</b>' + dim(ev.prompt, 200)); break;
    case 'human_request':
      _tmActiveNode = ev.node_id || _tmActiveNode; _tmRenderGraph();
      if (ev.request_id) { _tmGates[ev.request_id] = ev; _tmRenderInspector(); }
      _tmLine(_tmIco('person') + ' <b>' + _tmEsc(ev.name || _tmT('tm.gate.who')) + '</b>' + _tmT('tm.ev.gateAwaiting') + dim(ev.prompt, 160), 'is-gate'); break;
    case 'human_resolved':
      if (ev.request_id) { delete _tmGates[ev.request_id]; _tmRenderInspector(); }
      _tmLine(_tmIco('person') + ' ' + (ev.mode === 'approve'
        ? (ev.approved ? _tmIco('check') + ' approved' : _tmIco('reject') + ' rejected')
        : _tmIco('check') + ' answered')); break;
    case 'flow_complete':
      _tmLine(_tmIco('flag') + ' <b>' + _tmEsc(ev.status) + '</b> — ' + (ev.agents_run || 0) + ' agents, ' + (ev.elapsed || 0) + 's',
              ev.status === 'completed' ? 'is-done' : 'is-err'); break;
    case 'error':
      _tmLine(_tmIco('warn') + dim((ev.error && ev.error.detail) || 'error'), 'is-err'); break;
  }
}

// ── Right inspector: active node detail + interactive human gates ───

// Select a graph node to pin its run trace in the inspector. Clicking the
// already-selected node clears the pin (back to live active-node detail).
function _tmSelectNode(nodeId) {
  _tmSelNode = (_tmSelNode === nodeId) ? null : (nodeId || null);
  _tmRenderGraph();        // repaint selection ring
  _tmRenderInspector();
}

function _tmRenderInspector(stepEv) {
  var body = document.getElementById('tmInspBody');
  if (!body) return;
  var html = '';

  // Pending human gates first — these are the actionable items.
  var gateIds = Object.keys(_tmGates);
  if (gateIds.length) {
    gateIds.forEach(function (rid) {
      html += _tmGateCard(_tmGates[rid]);
    });
  }

  // A user-PINNED node (clicked in the graph) shows its full run trace —
  // the resolved brief + bounded input + output. This is the traceability
  // surface for a reopened run. Falls back to the live active node when
  // nothing is pinned.
  var inspectId = _tmSelNode || _tmActiveNode;
  var node = inspectId && _tmDef
    ? (_tmDef.nodes || []).filter(function (n) { return n.id === inspectId; })[0]
    : null;
  if (node) {
    var tr = _tmTrace[node.id];
    var pinned = (_tmSelNode === node.id);
    var kindLbl = pinned ? (tr ? _tmT('tm.insp.runTrace') : _tmT('tm.insp.node')) : _tmT('tm.insp.activeNode');
    var count = _tmTraceCount[node.id] || 0;
    html += '<div class="tm-insp-card">'
      + '<div class="tm-insp-kind">' + _tmEsc(kindLbl)
      +   (count > 1 ? ' <span class="tm-insp-runs">×' + count + '</span>' : '') + '</div>'
      + '<div class="tm-insp-node"><span class="tm-insp-ava">' + _tmNodeIconHtml(node) + '</span>' + _tmEsc(_tmNodeLabel(node)) + '</div>'
      + '<div class="tm-insp-meta">' + _tmEsc(node.type === 'role' ? (node.role || 'agent') : (node.kind || 'control')) + ' · ' + _tmEsc(_tmNodeSub(node)) + '</div>'
      + _tmTraceDetail(tr, stepEv)
      + '</div>';
  }

  if (!html) html = '<div class="tm-insp-empty">' + _tmIco('eye') + '<div>' + _tmT('tm.insp.empty') + '<br>' + _tmT('tm.insp.emptyHint') + '</div></div>';
  body.innerHTML = html;

  // Wire input gates to Enter-submit after injection.
  gateIds.forEach(function (rid) {
    var inp = document.getElementById('tmGateInput-' + rid);
    if (inp) inp.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); _tmHumanInput(rid); }
    });
  });
}

// Render a node's durable trace entry (from a step_trace event): status,
// deliverable count, the resolved brief (the prompt the node ran with), its
// effective input, and its output. Each block is collapsible-by-length via
// a bounded <pre>. Returns '' when no trace exists yet.
function _tmTraceDetail(tr, stepEv) {
  if (!tr) {
    // No trace yet (node still running / pre-trace). Show isolation if the
    // live step_start carried it.
    return (stepEv && stepEv.isolation)
      ? '<div class="tm-insp-meta">' + _tmT('tm.trace.isolation') + ': ' + _tmEsc(stepEv.isolation) + '</div>'
      : '';
  }
  var h = '';
  var statusCls = (tr.status === 'failed' || tr.status === 'error') ? 'tm-trace-err'
                : (tr.status === 'completed' || tr.status === 'done') ? 'tm-trace-ok' : '';
  var bits = [];
  if (tr.emits) bits.push(_tmT('tm.trace.emits') + ' ' + tr.emits);
  if (tr.isolation) bits.push(tr.isolation);
  if (typeof tr.iteration === 'number' && tr.iteration > 0) bits.push(_tmT('tm.trace.iter') + ' ' + tr.iteration);
  if (typeof tr.state_changing === 'number') bits.push(tr.state_changing + ' ' + _tmT('tm.trace.stateChanging'));
  h += '<div class="tm-trace-tags"><span class="tm-trace-status ' + statusCls + '">' + _tmEsc(tr.status || '') + '</span>'
    + (bits.length ? '<span class="tm-trace-bits">' + _tmEsc(bits.join(' · ')) + '</span>' : '') + '</div>';
  if (tr.error) {
    h += '<div class="tm-trace-lbl">' + _tmT('tm.trace.error') + '</div><pre class="tm-trace-pre tm-trace-err">' + _tmEsc(tr.error.slice(0, 2000)) + '</pre>';
  }
  if (tr.brief) {
    h += '<div class="tm-trace-lbl">' + _tmT('tm.trace.brief') + '</div>'
      + '<pre class="tm-trace-pre">' + _tmEsc(tr.brief.slice(0, 4000)) + '</pre>';
  }
  if (tr.input) {
    h += '<div class="tm-trace-lbl">' + _tmT('tm.trace.input') + (tr.input_truncated ? ' <span class="tm-trace-trunc">' + _tmT('tm.trace.truncated') + '</span>' : '') + '</div>'
      + '<pre class="tm-trace-pre">' + _tmEsc(tr.input.slice(0, 4000)) + '</pre>';
  }
  if (tr.output) {
    h += '<div class="tm-trace-lbl">' + _tmT('tm.trace.output') + (tr.output_truncated ? ' <span class="tm-trace-trunc">' + _tmT('tm.trace.truncated') + '</span>' : '') + '</div>'
      + '<pre class="tm-trace-pre">' + _tmEsc(tr.output.slice(0, 6000)) + '</pre>';
  }
  return h;
}

function _tmGateCard(ev) {
  var rid = ev.request_id || '';
  var ridArg = "'" + rid.replace(/'/g, "\\'") + "'";
  var head = '<div class="tm-gate-tag">' + _tmT('tm.gate.tag') + '</div>'
    + '<div class="tm-gate-head">' + _tmIco('person') + ' ' + _tmEsc(ev.name || _tmT('tm.gate.who')) + '</div>'
    + '<div class="tm-gate-prompt">' + _tmEsc(ev.prompt || (ev.mode === 'approve' ? _tmT('tm.gate.approvePrompt') : _tmT('tm.gate.inputPrompt'))) + '</div>';
  var actions;
  if (ev.mode === 'approve') {
    actions = '<div class="tm-gate-actions">'
      + '<button class="tm-btn tm-btn-ok" onclick="_tmHumanApprove(' + ridArg + ', true)">' + _tmIco('check') + ' ' + _tmT('tm.gate.approve') + '</button>'
      + '<button class="tm-btn tm-btn-danger" onclick="_tmHumanApprove(' + ridArg + ', false)">' + _tmIco('reject') + ' ' + _tmT('tm.gate.reject') + '</button>'
      + '</div>';
  } else {
    actions = '<div class="tm-gate-actions tm-gate-input">'
      + '<input class="tm-gate-field" id="tmGateInput-' + _tmEsc(rid) + '" placeholder="' + _tmT('tm.gate.inputPlaceholder') + '">'
      + '<button class="tm-btn tm-btn-primary" onclick="_tmHumanInput(' + ridArg + ')">' + _tmT('tm.gate.send') + '</button>'
      + '</div>';
  }
  return '<div class="tm-gate-card" id="tmGate-' + _tmEsc(rid) + '">' + head + actions + '</div>';
}

async function _tmHumanApprove(rid, approved) {
  if (!rid) return;
  delete _tmGates[rid];
  _tmRenderInspector();
  await Api.orchestrations.humanApprove(rid, approved);
  if (typeof _orchToast === 'function') _orchToast(approved ? _tmT('tm.gate.approved') : _tmT('tm.gate.rejected'));
}

async function _tmHumanInput(rid) {
  if (!rid) return;
  var inp = document.getElementById('tmGateInput-' + rid);
  var val = inp ? inp.value : '';
  if (!val.trim()) { if (typeof _orchToast === 'function') _orchToast(_tmT('tm.gate.enterResponse'), true); return; }
  delete _tmGates[rid];
  _tmRenderInspector();
  await Api.orchestrations.humanInput(rid, val);
}

// ── Run actions ─────────────────────────────────────────────────────

async function _tmAbortRun(runId) {
  if (typeof showConfirm === 'function'
      && !await showConfirm(_tmT('tm.abort.confirm'),
                            { title: _tmT('tm.abort.confirmTitle'), okText: _tmT('tm.btn.abort'), danger: true })) {
    return;
  }
  await Api.orchestrations.taskAbort(runId);
  if (typeof _orchToast === 'function') _orchToast(_tmT('tm.toast.abort'));
}

async function _tmDeleteRun(runId) {
  if (typeof showConfirm === 'function'
      && !await showConfirm(_tmT('tm.delete.confirm'),
                            { title: _tmT('tm.delete.confirmTitle'), okText: _tmT('tm.btn.delete'), danger: true })) {
    return;
  }
  var ok = await Api.orchestrations.taskRemove(runId);
  if (!ok) { if (typeof _orchToast === 'function') _orchToast(_tmT('tm.toast.deleteFailed'), true); return; }
  if (_tmRunId === runId) {
    _tmRunId = null;
    _tmPolling = false;
    _tmDef = null;
    _tmActiveNode = null;
    _tmDoneNodes = {};
    _tmGates = {};
    _tmTrace = {};
    _tmTraceCount = {};
    _tmSelNode = null;
    var head = document.getElementById('tmRunTitle');
    if (head) head.innerHTML = '<div class="tm-empty">Select a run to view its timeline.</div>';
    var g = document.getElementById('tmGraph'); if (g) { g.innerHTML = ''; g.style.display = 'none'; }
    var tl = document.getElementById('tmTimeline'); if (tl) tl.innerHTML = '';
    var fin = document.getElementById('tmFinal'); if (fin) { fin.style.display = 'none'; fin.innerHTML = ''; }
    _tmRenderInspector();
  }
  _tmRefreshRuns();
}

// ── Scoped styles ───────────────────────────────────────────────────

function _tmInjectStyles() {
  if (document.getElementById('tmStyles')) return;
  var st = document.createElement('style');
  st.id = 'tmStyles';
  // Inherits the live theme tokens (light "clay/paper" or dark) exactly like
  // the Studio (orchestration.js) — NO hardcoded colors. Shares the Studio's
  // radius/elevation scale so Task Mode reads as the same product surface.
  st.textContent = `
.tm-overlay{position:fixed;inset:0;z-index:9600;background:rgba(0,0,0,.5);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;animation:tmFade .15s ease}
@keyframes tmFade{from{opacity:0}to{opacity:1}}
.tm-shell{--tm-r-sm:7px;--tm-r-md:11px;--tm-r-lg:14px;--tm-r-xl:16px;--tm-elev-card:var(--clay-md,0 2px 8px rgba(0,0,0,.12));--tm-elev-pop:var(--clay-lg,0 12px 32px rgba(0,0,0,.22));--tm-rail:1px solid var(--border);--tm-ok:var(--s-done,#10b981);width:96vw;height:92vh;max-width:1500px;background:var(--bg-secondary);border:1px solid var(--border-light);border-radius:var(--tm-r-xl);box-shadow:0 24px 64px rgba(0,0,0,.3);display:flex;flex-direction:column;overflow:hidden;font-family:inherit}
.tm-top{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 16px;border-bottom:var(--tm-rail);background:linear-gradient(180deg,var(--bg-tertiary),var(--bg-secondary))}
.tm-top-left{display:flex;align-items:center;gap:9px;min-width:0}
.tm-top-glyph{display:inline-flex;color:var(--accent)}.tm-top-glyph svg{width:20px;height:20px}
.tm-top-name{font-size:16px;font-weight:700;color:var(--text-primary)}
.tm-top-sub{font-size:12px;color:var(--text-tertiary)}
.tm-top-actions{display:flex;align-items:center;gap:7px}
/* ── Buttons: layered, theme-token only, shares the Studio's button language.
   Default = quiet secondary surface with a soft hover lift; ghost = fully
   transparent tertiary action; semantic fills get a colored lift on hover.
   All colors resolve from live theme tokens (works across light/dark/tofu). */
.tm-btn{font-family:inherit;font-size:12.5px;font-weight:600;letter-spacing:.01em;line-height:1.2;border-radius:var(--tm-r-sm);padding:8px 13px;border:1px solid var(--border);background:var(--bg-tertiary);color:var(--text-secondary);cursor:pointer;transition:background .15s,color .15s,border-color .15s,box-shadow .15s,transform .1s ease;white-space:nowrap;display:inline-flex;align-items:center;justify-content:center;gap:6px}
.tm-btn:hover{background:var(--bg-hover);color:var(--text-primary);border-color:var(--border-light);box-shadow:var(--tm-elev-card);transform:translateY(-1px)}
.tm-btn:active{transform:translateY(0);box-shadow:none}
.tm-btn:focus-visible{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-subtle)}
.tm-btn:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none;filter:none}
.tm-btn svg{width:1em;height:1em}
/* close: quiet icon button that reddens on hover */
.tm-btn-close{padding:8px 10px;color:var(--text-tertiary)}
.tm-btn-close:hover{background:var(--error-bg);border-color:var(--error-border);color:var(--error-text);box-shadow:none;transform:none}
/* ghost: transparent tertiary action (edit / abort / delete rows) */
.tm-btn-ghost{background:transparent;border-color:transparent;color:var(--text-tertiary)}
.tm-btn-ghost:hover{background:var(--bg-hover);border-color:var(--border);color:var(--text-primary);box-shadow:none;transform:none}
.tm-btn-ghost.tm-btn-danger,.tm-btn-ghost.tm-btn-danger:hover{background:transparent;border-color:transparent;color:var(--text-tertiary);filter:none}
.tm-btn-ghost.tm-btn-danger:hover{background:var(--error-bg);border-color:var(--error-border);color:var(--error-text)}
/* semantic fills: colored lift + glow on hover */
.tm-btn-primary{background:var(--accent);border-color:var(--accent);color:#fff}
.tm-btn-primary:hover{background:var(--accent-hover);border-color:var(--accent-hover);color:#fff;box-shadow:0 4px 14px var(--accent-subtle);transform:translateY(-1px)}
.tm-btn-ok{background:var(--tm-ok);border-color:var(--tm-ok);color:#fff}
.tm-btn-ok:hover{background:var(--tm-ok);border-color:var(--tm-ok);color:#fff;filter:brightness(1.06);box-shadow:0 4px 14px color-mix(in srgb,var(--tm-ok) 30%,transparent);transform:translateY(-1px)}
.tm-btn-danger{background:var(--error-bg);border-color:var(--error-border);color:var(--error-text)}
.tm-btn-danger:hover{background:var(--error-bg);border-color:var(--error-border);color:var(--error-text);filter:brightness(1.06);box-shadow:0 4px 14px color-mix(in srgb,var(--error-text) 26%,transparent);transform:translateY(-1px)}
.tm-body{flex:1;display:flex;min-height:0}
/* Safety net: any inline glyph from _ORCH_ICONS/_ORCH_GLYPHS used inside the
   shell is capped to a sane size, so Task Mode never depends on the Studio's
   .orch-ico sizing being present (it isn't always). Specific rules below
   override per context. */
.tm-shell svg{width:1em;height:1em}
/* left rail */
.tm-rail{width:264px;flex-shrink:0;border-right:var(--tm-rail);background:var(--bg-secondary);display:flex;flex-direction:column;min-height:0}
.tm-rail-head{padding:13px 14px;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.07em;color:var(--text-tertiary);border-bottom:var(--tm-rail)}
.tm-rail-list{flex:1;overflow-y:auto;padding:9px}
.tm-run{display:block;width:100%;text-align:left;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:var(--tm-r-md);padding:10px 11px;margin-bottom:7px;cursor:pointer;color:var(--text-secondary);transition:background .14s,border-color .14s,transform .1s ease,box-shadow .14s}
.tm-run:hover{background:var(--bg-hover);border-color:var(--border-light);transform:translateY(-1px);box-shadow:var(--tm-elev-card)}
.tm-run.is-active{border-color:var(--accent);background:var(--accent-subtle);box-shadow:0 0 0 1px var(--accent-subtle)}
.tm-run-top{display:flex;align-items:center;justify-content:space-between;gap:8px}
.tm-run-name{font-size:13px;font-weight:700;color:var(--text-primary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tm-run-meta{font-size:11px;color:var(--text-tertiary);margin-top:4px}
/* center column */
.tm-main{flex:1;display:flex;flex-direction:column;min-width:0;background:var(--bg-primary)}
.tm-main-head{padding:13px 18px;border-bottom:var(--tm-rail);background:var(--bg-secondary)}
.tm-title-row{display:flex;align-items:center;gap:10px}
.tm-title-name{font-size:15px;font-weight:700;color:var(--text-primary)}
.tm-title-spacer{flex:1}
.tm-title-input{margin-top:6px;font-size:12px;color:var(--text-tertiary);line-height:1.5}
/* graph pane */
.tm-graph{flex:0 0 46%;min-height:170px;overflow:auto;border-bottom:var(--tm-rail);background-color:var(--bg-primary);background-image:radial-gradient(color-mix(in srgb,var(--border) 70%,transparent) 1px,transparent 1px);background-size:24px 24px;display:flex;align-items:flex-start;justify-content:center;padding:18px}
.tm-graph-svg{flex:none;display:block;margin:auto}
.tm-edge{fill:none;stroke:color-mix(in srgb,var(--accent) 34%,var(--border-light));stroke-width:2;stroke-linecap:round}
.tm-edge-arrow{fill:color-mix(in srgb,var(--accent) 50%,var(--border-light));stroke:none}
.tm-gnode{position:relative;box-sizing:border-box;width:100%;height:100%;display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:var(--tm-r-md);background:var(--bg-secondary);border:1px solid var(--border-light);border-left:4px solid var(--tm-accent,var(--accent));box-shadow:var(--tm-elev-card);color:var(--text-primary);font-family:inherit;overflow:visible;transition:box-shadow .15s,border-color .15s}
.tm-gnode-ico{width:28px;height:28px;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:var(--tm-accent,var(--accent))}
.tm-gnode-ico img{width:28px;height:28px;object-fit:contain;filter:drop-shadow(0 1px 2px rgba(0,0,0,.25))}
.tm-gnode-ico svg{width:20px;height:20px}
.tm-gnode-text{display:flex;flex-direction:column;min-width:0;line-height:1.25}
.tm-gnode-label{font-size:12.5px;font-weight:700;color:var(--text-primary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tm-gnode-sub{font-size:10px;color:var(--text-tertiary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tm-gnode.is-done{opacity:.72}
.tm-gnode.is-done .tm-gnode-ico{color:var(--tm-ok,#10b981)}
.tm-gnode.is-active{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-subtle),var(--tm-elev-pop);animation:tmPulse 1.5s ease-in-out infinite}
@keyframes tmPulse{0%,100%{box-shadow:0 0 0 2px var(--accent-subtle),var(--tm-elev-card)}50%{box-shadow:0 0 0 4px var(--accent-subtle),var(--tm-elev-pop)}}
.tm-gnode-ribbon{position:absolute;right:9px;top:-8px;font-size:8px;font-weight:800;letter-spacing:.1em;padding:2px 7px;border-radius:999px;color:#fff;background:var(--tm-accent,var(--accent));box-shadow:var(--tm-elev-card)}
.tm-gnode-ribbon-out{top:auto;bottom:-8px}
/* timeline */
.tm-stream{flex:1;display:flex;flex-direction:column;min-height:0}
.tm-stream-head{padding:9px 18px;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.07em;color:var(--text-tertiary);border-bottom:var(--tm-rail);background:var(--bg-secondary)}
.tm-timeline{flex:1;overflow-y:auto;padding:13px 18px;font-size:12.5px;line-height:1.65;font-family:var(--mono-font,monospace)}
.tm-line{padding:3px 0;color:var(--text-secondary);word-break:break-word}
.tm-line svg{width:14px;height:14px;vertical-align:-0.2em;margin-right:4px;flex-shrink:0}
.tm-line.is-active{color:var(--accent)}
.tm-line.is-done{color:var(--tm-ok);font-weight:600}
.tm-line.is-err{color:var(--error-text)}
.tm-line.is-gate{color:var(--accent)}
.tm-dim{color:var(--text-tertiary)}
.tm-final{border-top:var(--tm-rail);padding:12px 18px;max-height:30%;overflow-y:auto;background:var(--bg-secondary)}
.tm-final-label{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;color:var(--text-tertiary);margin-bottom:7px}
.tm-final-pre{white-space:pre-wrap;word-break:break-word;font-size:12px;color:var(--text-primary);margin:0;background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--tm-r-md);padding:11px}
.tm-empty{color:var(--text-tertiary);font-size:13px;padding:6px 0;display:flex;align-items:center;gap:7px}
.tm-empty svg{width:16px;height:16px}
/* right inspector */
.tm-inspector{width:312px;flex-shrink:0;border-left:var(--tm-rail);background:var(--bg-secondary);display:flex;flex-direction:column;min-height:0}
.tm-insp-head{padding:13px 14px;font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:.07em;color:var(--text-tertiary);border-bottom:var(--tm-rail)}
.tm-insp-body{flex:1;overflow-y:auto;padding:14px}
.tm-insp-empty{text-align:center;color:var(--text-tertiary);font-size:12.5px;padding-top:44px;line-height:1.6}
.tm-insp-empty svg{width:30px;height:30px;opacity:.6;margin-bottom:10px}
.tm-insp-card{background:var(--bg-tertiary);border:1px solid var(--border);border-radius:var(--tm-r-md);padding:11px 13px;margin-bottom:11px}
.tm-insp-kind{font-size:10px;font-weight:800;letter-spacing:.07em;text-transform:uppercase;color:var(--accent);margin-bottom:5px}
.tm-insp-node{color:var(--text-primary);font-size:14px;font-weight:700;display:flex;align-items:center;gap:8px}
.tm-insp-node .tm-insp-ava{width:24px;height:24px;display:flex;align-items:center;justify-content:center;color:var(--accent)}
.tm-insp-node .tm-insp-ava img{width:26px;height:26px;object-fit:contain}
.tm-insp-node .tm-insp-ava svg{width:18px;height:18px}
.tm-insp-meta{color:var(--text-tertiary);font-size:11.5px;margin-top:6px}
.tm-insp-runs{color:var(--text-tertiary);font-weight:700}
/* per-node run-trace detail (resolved brief + input + output) */
.tm-trace-tags{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:9px}
.tm-trace-status{font-size:10.5px;font-weight:700;text-transform:capitalize;padding:2px 8px;border-radius:999px;background:var(--bg-hover);color:var(--text-secondary)}
.tm-trace-status.tm-trace-ok{background:color-mix(in srgb,var(--tm-ok) 14%,transparent);color:var(--tm-ok)}
.tm-trace-status.tm-trace-err{background:var(--error-bg);color:var(--error-text)}
.tm-trace-bits{font-size:11px;color:var(--text-tertiary)}
.tm-trace-lbl{font-size:10px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;color:var(--text-tertiary);margin:11px 0 5px}
.tm-trace-trunc{font-weight:600;color:var(--text-tertiary);text-transform:none;letter-spacing:0}
.tm-trace-pre{white-space:pre-wrap;word-break:break-word;font-family:var(--mono-font,monospace);font-size:11px;line-height:1.5;color:var(--text-secondary);background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--tm-r-sm);padding:8px 10px;margin:0;max-height:240px;overflow:auto}
.tm-trace-pre.tm-trace-err{color:var(--error-text);border-color:var(--error-border)}
/* graph node: clickable, traced, selected */
.tm-gnode{cursor:pointer}
.tm-gnode.is-selected{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent)}
.tm-gnode-traced::after{content:"";position:absolute;left:8px;top:-7px;width:7px;height:7px;border-radius:50%;background:var(--tm-ok,#10b981)}
/* human gate card */
.tm-gate-card{background:var(--accent-subtle);border:1px solid var(--accent);border-radius:var(--tm-r-md);padding:12px 13px;margin-bottom:11px;box-shadow:var(--tm-elev-card)}
.tm-gate-tag{font-size:9.5px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin-bottom:7px}
.tm-gate-head{color:var(--text-primary);font-size:13px;font-weight:700;display:flex;align-items:center;gap:7px}
.tm-gate-head svg{width:16px;height:16px;color:var(--accent)}
.tm-gate-prompt{color:var(--text-secondary);font-size:12.5px;margin:9px 0 11px;line-height:1.55}
.tm-gate-actions{display:flex;gap:7px}
.tm-gate-actions .tm-btn{flex:1;justify-content:center}
.tm-gate-input{flex-direction:column;align-items:stretch}
.tm-gate-field{width:100%;box-sizing:border-box;background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--tm-r-sm);padding:8px 10px;color:var(--text-primary);font-family:inherit;font-size:12.5px;margin-bottom:7px;outline:none;transition:border-color .15s,box-shadow .15s}
.tm-gate-field:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-subtle)}
/* status chips — tinted from theme semantic colors */
.tm-chip{font-size:10.5px;font-weight:700;padding:3px 9px;border-radius:999px;text-transform:capitalize;flex-shrink:0;border:1px solid transparent}
.tm-chip-pending{background:var(--bg-hover);color:var(--text-secondary);border-color:var(--border)}
.tm-chip-running{background:var(--thinking-bg);color:var(--thinking-text);border-color:var(--thinking-border)}
.tm-chip-paused{background:color-mix(in srgb,var(--warning-text,#d97706) 14%,transparent);color:var(--warning-text,#d97706);border-color:color-mix(in srgb,var(--warning-text,#d97706) 40%,transparent)}
.tm-chip-done{background:color-mix(in srgb,var(--tm-ok) 14%,transparent);color:var(--tm-ok);border-color:color-mix(in srgb,var(--tm-ok) 40%,transparent)}
.tm-chip-error{background:var(--error-bg);color:var(--error-text);border-color:var(--error-border)}
.tm-chip-aborted{background:var(--bg-hover);color:var(--text-tertiary);border-color:var(--border)}
/* run-list meta: relative time + duration on one line */
.tm-run-meta{display:flex;align-items:center;justify-content:space-between;gap:8px}
.tm-run-dur{color:var(--text-tertiary);font-size:10.5px;font-weight:600}
.tm-run-live .tm-run-dur{color:var(--thinking-text)}
.tm-run-live .tm-run-name::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--thinking-text);margin-right:6px;vertical-align:middle;animation:tmLiveDot 1.4s ease-in-out infinite}
@keyframes tmLiveDot{0%,100%{opacity:1}50%{opacity:.3}}
/* loading + empty + error states in the rail */
.tm-loading{display:flex;align-items:center;gap:9px;color:var(--text-tertiary);font-size:12.5px;padding:14px}
.tm-spin{width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:tmSpin .7s linear infinite;flex-shrink:0}
@keyframes tmSpin{to{transform:rotate(360deg)}}
.tm-state{text-align:center;padding:30px 18px;color:var(--text-tertiary)}
.tm-state svg{width:30px;height:30px;opacity:.55;margin-bottom:11px}
.tm-state-err svg{color:var(--error-text);opacity:.8}
.tm-state-title{font-size:14px;font-weight:800;color:var(--text-secondary);margin-bottom:6px}
.tm-state-sub{font-size:12px;line-height:1.6;margin-bottom:14px}
.tm-state-btn{margin:0 auto}
/* ── Responsive: stack the three panes on narrow / touch viewports ── */
@media(max-width:1000px){.tm-rail{width:220px}.tm-inspector{width:264px}}
@media(max-width:768px){
  .tm-shell{width:100vw;height:100vh;max-width:none;border-radius:0;border:none}
  .tm-body{flex-direction:column;overflow-y:auto}
  .tm-rail{width:auto;max-height:38vh;border-right:none;border-bottom:var(--tm-rail)}
  .tm-main{min-height:0}
  .tm-graph{flex:0 0 auto;max-height:44vh}
  .tm-inspector{width:auto;border-left:none;border-top:var(--tm-rail);max-height:52vh}
  .tm-top-sub{display:none}
  .tm-btn{padding:9px 12px}
  .tm-top-actions .tm-btn span,.tm-top-actions .tm-btn{gap:0}
}
`;
  document.head.appendChild(st);
}

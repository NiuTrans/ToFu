/* ═══════════════════════════════════════════════════════════════════
   task-mode.js — Task Mode (durable orchestration run viewer)

   The operating surface for DURABLE run instances (see
   docs/proposals/TASK_MODE.md §5). Where orchestration.js is the
   AUTHORING canvas (compose a flow template), Task Mode is where you
   watch and reopen a flow's actual EXECUTIONS — runs that survive a
   reload/restart because every event is persisted to the DB.

   ── Scope (this minimal slice) ──────────────────────────────────────
   Two panes: a left rail listing runs (Api.orchestrations.taskList) and
   a center timeline of one run (Api.orchestrations.taskEvents). The
   timeline is cursor-polled, which unifies LIVE (keep polling a running
   run) and REPLAY (a finished run returns everything once, done:true).
   Per-item dashboards + human-gate interaction are later phases.

   ── House rule ──────────────────────────────────────────────────────
   SVG-only, no emoji even for abstract concepts (TASK_MODE.md §5.1).
   Reuses the _ORCH_ICONS glyph vocabulary from orchestration.js (same
   bundle; referenced at runtime so load order is irrelevant). Per
   CLAUDE.md §3.2.0 all backend calls go through window.Api.* — this file
   issues no raw fetch.
   ═══════════════════════════════════════════════════════════════════ */

var _tmModalReady = false;
var _tmRunId = null;        // currently-open run; guards stale poll callbacks
var _tmPolling = false;
var _tmRuns = [];

function _tmIco(name) {
  // _ORCH_ICONS lives in orchestration.js (same bundle). Fall back to an
  // empty string if it somehow isn't present so the line still renders.
  return (typeof _ORCH_ICONS !== 'undefined' && _ORCH_ICONS[name]) || '';
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
    +   '<div class="tm-head">'
    +     '<div class="tm-head-title">' + _tmIco('rocket') + ' <b>Task Mode</b>'
    +       '<span class="tm-head-sub">durable orchestration runs</span></div>'
    +     '<div class="tm-head-actions">'
    +       '<button class="tm-btn tm-btn-ghost" onclick="_tmRefreshRuns()" title="Refresh">' + _tmIco('loop') + '</button>'
    +       '<button class="tm-btn tm-btn-ghost" onclick="closeTaskMode()" title="Close">✕</button>'
    +     '</div>'
    +   '</div>'
    +   '<div class="tm-body">'
    +     '<div class="tm-rail"><div class="tm-rail-head">Runs</div>'
    +       '<div class="tm-rail-list" id="tmRunList"></div></div>'
    +     '<div class="tm-main">'
    +       '<div class="tm-main-head" id="tmRunTitle">'
    +         '<div class="tm-empty">Select a run to view its timeline.</div></div>'
    +       '<div class="tm-timeline" id="tmTimeline"></div>'
    +       '<div class="tm-final" id="tmFinal" style="display:none"></div>'
    +     '</div>'
    +   '</div>'
    + '</div>';

  document.body.appendChild(ov);
  _tmModalReady = true;
}

// ── Left rail: run list ─────────────────────────────────────────────

async function _tmRefreshRuns() {
  var list = document.getElementById('tmRunList');
  if (list) list.innerHTML = '<div class="tm-empty">Loading…</div>';
  var res = await Api.orchestrations.taskList();
  _tmRuns = (res && res.ok && res.runs) || [];
  _tmRenderRunList();
}

function _tmRenderRunList() {
  var list = document.getElementById('tmRunList');
  if (!list) return;
  if (!_tmRuns.length) {
    list.innerHTML = '<div class="tm-empty">No runs yet. Run a flow as a Task from the Studio.</div>';
    return;
  }
  list.innerHTML = _tmRuns.map(function (r) {
    var active = (r.id === _tmRunId) ? ' is-active' : '';
    return '<button class="tm-run' + active + '" onclick="_tmOpenRun(\'' + _tmEsc(r.id) + '\')">'
      + '<div class="tm-run-top"><span class="tm-run-name">' + _tmEsc(r.name || '(unnamed flow)') + '</span>'
      + _tmStatusChip(r.status) + '</div>'
      + '<div class="tm-run-meta">' + _tmEsc(_tmAgo(r.created_at)) + '</div>'
      + '</button>';
  }).join('');
}

function _tmStatusChip(status) {
  var s = status || 'pending';
  return '<span class="tm-chip tm-chip-' + _tmEsc(s) + '">' + _tmEsc(s) + '</span>';
}

// ── Center: open + poll one run ─────────────────────────────────────

async function _tmOpenRun(runId) {
  if (!runId) return;
  _tmRunId = runId;
  _tmPolling = false;
  _tmRenderRunList();   // re-highlight active

  var tl = document.getElementById('tmTimeline');
  if (tl) tl.innerHTML = '';
  var fin = document.getElementById('tmFinal');
  if (fin) { fin.style.display = 'none'; fin.innerHTML = ''; }

  var res = await Api.orchestrations.taskGet(runId);
  var run = (res && res.ok && res.run) || null;
  if (!run) { _tmRenderTitle(null); return; }
  if (_tmRunId !== runId) return;   // user switched away while awaiting
  _tmRenderTitle(run);

  _tmPolling = true;
  _tmPoll(runId, 0);
}

function _tmRenderTitle(run) {
  var head = document.getElementById('tmRunTitle');
  if (!head) return;
  if (!run) { head.innerHTML = '<div class="tm-empty">Run not found.</div>'; return; }
  head.innerHTML = ''
    + '<div class="tm-title-row">'
    +   '<span class="tm-title-name">' + _tmEsc(run.name || '(unnamed flow)') + '</span>'
    +   _tmStatusChip(run.status)
    +   '<span class="tm-title-spacer"></span>'
    +   (_tmIsTerminal(run.status)
          ? '<button class="tm-btn tm-btn-ghost" onclick="_tmDeleteRun(\'' + _tmEsc(run.id) + '\')" title="Delete run">Delete</button>'
          : '<button class="tm-btn tm-btn-ghost" onclick="_tmAbortRun(\'' + _tmEsc(run.id) + '\')" title="Abort run">' + _tmIco('stop') + ' Abort</button>')
    + '</div>'
    + (run.input ? '<div class="tm-title-input">' + _tmEsc(run.input.slice(0, 300)) + '</div>' : '');
}

function _tmIsTerminal(status) {
  return status === 'done' || status === 'error' || status === 'aborted';
}

async function _tmPoll(runId, cursor) {
  if (_tmRunId !== runId || !_tmPolling) return;   // stale / closed
  var res = await Api.orchestrations.taskEvents(runId, cursor);
  if (_tmRunId !== runId || !_tmPolling) return;
  if (!res || !res.ok) {
    _tmLine(_tmIco('warn') + ' failed to load events', 'is-err');
    _tmPolling = false;
    return;
  }
  (res.events || []).forEach(_tmRenderEvent);
  _tmSyncChip(res.status);

  if (res.done) {
    _tmPolling = false;
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
  if (chip && chip.textContent !== status) {
    chip.className = 'tm-chip tm-chip-' + status;
    chip.textContent = status;
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
    fin.innerHTML = '<div class="tm-final-label">Result</div>'
      + '<pre class="tm-final-pre">' + _tmEsc(run.final.slice(0, 8000)) + '</pre>';
  }
}

// ── Timeline rendering ──────────────────────────────────────────────

function _tmLine(html, cls) {
  var tl = document.getElementById('tmTimeline');
  if (!tl) return;
  var row = document.createElement('div');
  row.className = 'tm-line' + (cls ? ' ' + cls : '');
  row.innerHTML = html;
  tl.appendChild(row);
  tl.scrollTop = tl.scrollHeight;
}

// Maps the engine event vocabulary (lib/orchestration_engine.py) to
// timeline rows. Mirrors orchestration.js::_orchRenderRunEvent but emits
// into the Task Mode timeline (read-only — human gates render as notices
// here; interactive gates are a later phase).
function _tmRenderEvent(ev) {
  var dim = function (s, n) { return s ? ' <span class="tm-dim">' + _tmEsc((s || '').slice(0, n || 120)) + '</span>' : ''; };
  switch (ev.type) {
    case 'flow_start':
      _tmLine(_tmIco('flag') + ' <b>' + _tmEsc(ev.name || 'flow') + '</b> — ' + (ev.nodes || 0) + ' nodes'); break;
    case 'step_start':
      _tmLine(_tmIco('bot') + ' <b>' + _tmEsc(ev.name || ev.role) + '</b> running…', 'is-active'); break;
    case 'step_complete':
      _tmLine(_tmIco('check') + ' ' + _tmEsc(ev.role) + dim(ev.preview)); break;
    case 'loop_iteration':
      _tmLine(_tmIco('loop') + ' loop iteration ' + ev.iteration + '/' + ev.max); break;
    case 'zero_deliverable_guard':
      _tmLine(_tmIco('warn') + ' zero-deliverable guard', 'is-err'); break;
    case 'replan':
      _tmLine(_tmIco('compass') + ' re-plan #' + ev.replan + dim(ev.defect, 100)); break;
    case 'stuck_detected':
      _tmLine(_tmIco('loop') + ' stuck — breaking the loop', 'is-err'); break;
    case 'parallel_start':
      _tmLine(_tmIco('fanout') + ' fan-out → ' + ev.branches + ' branches'); break;
    case 'branch_pick':
      _tmLine('↪ route → ' + _tmEsc(ev.chosen || '(none)')); break;
    case 'artifact_declared':
      _tmLine(_tmIco('package') + ' deliverable: <b>' + _tmEsc(ev.path || ev.name || '(unnamed)') + '</b>' + dim(ev.description)); break;
    case 'human_notify':
    case 'human_request':
      _tmLine(_tmIco('person') + ' <b>' + _tmEsc(ev.name || 'Human') + '</b>' + dim(ev.prompt, 200)); break;
    case 'human_resolved':
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

// ── Run actions ─────────────────────────────────────────────────────

async function _tmAbortRun(runId) {
  await Api.orchestrations.taskAbort(runId);
  if (typeof _orchToast === 'function') _orchToast('Abort requested');
}

async function _tmDeleteRun(runId) {
  var ok = await Api.orchestrations.taskRemove(runId);
  if (!ok) { if (typeof _orchToast === 'function') _orchToast('Delete failed', true); return; }
  if (_tmRunId === runId) {
    _tmRunId = null;
    _tmPolling = false;
    var head = document.getElementById('tmRunTitle');
    if (head) head.innerHTML = '<div class="tm-empty">Select a run to view its timeline.</div>';
    var tl = document.getElementById('tmTimeline'); if (tl) tl.innerHTML = '';
    var fin = document.getElementById('tmFinal'); if (fin) { fin.style.display = 'none'; fin.innerHTML = ''; }
  }
  _tmRefreshRuns();
}

// ── Scoped styles ───────────────────────────────────────────────────

function _tmInjectStyles() {
  if (document.getElementById('tmStyles')) return;
  var st = document.createElement('style');
  st.id = 'tmStyles';
  st.textContent = ''
    + '.tm-overlay{position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.55);'
    +   'display:none;align-items:center;justify-content:center}'
    + '.tm-shell{width:min(1100px,94vw);height:min(760px,92vh);background:var(--bg-primary,#1a1a1f);'
    +   'border:1px solid var(--border,#33343a);border-radius:14px;display:flex;flex-direction:column;'
    +   'overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.5)}'
    + '.tm-head{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;'
    +   'border-bottom:1px solid var(--border,#33343a)}'
    + '.tm-head-title{display:flex;align-items:center;gap:8px;color:var(--text-primary,#e8e8ea);font-size:15px}'
    + '.tm-head-sub{color:var(--text-tertiary,#888);font-size:12px;font-weight:400}'
    + '.tm-head-actions{display:flex;gap:6px}'
    + '.tm-btn{background:var(--bg-secondary,#26272d);color:var(--text-secondary,#c8c8cc);'
    +   'border:1px solid var(--border,#33343a);border-radius:8px;padding:6px 10px;cursor:pointer;'
    +   'font-size:13px;display:inline-flex;align-items:center;gap:5px}'
    + '.tm-btn:hover{background:var(--bg-tertiary,#303138)}'
    + '.tm-btn-ghost{background:transparent}'
    + '.tm-body{flex:1;display:flex;min-height:0}'
    + '.tm-rail{width:280px;flex-shrink:0;border-right:1px solid var(--border,#33343a);'
    +   'display:flex;flex-direction:column;min-height:0}'
    + '.tm-rail-head{padding:10px 14px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;'
    +   'color:var(--text-tertiary,#888);border-bottom:1px solid var(--border,#33343a)}'
    + '.tm-rail-list{flex:1;overflow-y:auto;padding:8px}'
    + '.tm-run{display:block;width:100%;text-align:left;background:transparent;border:1px solid transparent;'
    +   'border-radius:10px;padding:10px;margin-bottom:4px;cursor:pointer;color:var(--text-secondary,#c8c8cc)}'
    + '.tm-run:hover{background:var(--bg-secondary,#26272d)}'
    + '.tm-run.is-active{background:var(--bg-secondary,#26272d);border-color:var(--accent,#6d8eff)}'
    + '.tm-run-top{display:flex;align-items:center;justify-content:space-between;gap:8px}'
    + '.tm-run-name{font-size:13px;font-weight:500;color:var(--text-primary,#e8e8ea);overflow:hidden;'
    +   'text-overflow:ellipsis;white-space:nowrap}'
    + '.tm-run-meta{font-size:11px;color:var(--text-tertiary,#888);margin-top:3px}'
    + '.tm-main{flex:1;display:flex;flex-direction:column;min-width:0}'
    + '.tm-main-head{padding:14px 18px;border-bottom:1px solid var(--border,#33343a)}'
    + '.tm-title-row{display:flex;align-items:center;gap:10px}'
    + '.tm-title-name{font-size:15px;font-weight:600;color:var(--text-primary,#e8e8ea)}'
    + '.tm-title-spacer{flex:1}'
    + '.tm-title-input{margin-top:6px;font-size:12px;color:var(--text-tertiary,#888)}'
    + '.tm-timeline{flex:1;overflow-y:auto;padding:14px 18px;font-size:13px;line-height:1.6}'
    + '.tm-line{padding:3px 0;color:var(--text-secondary,#c8c8cc);word-break:break-word}'
    + '.tm-line .orch-ico{margin-right:3px;vertical-align:-0.15em}'
    + '.tm-line.is-active{color:var(--text-primary,#e8e8ea)}'
    + '.tm-line.is-done{color:#34d399}'
    + '.tm-line.is-err{color:#f87171}'
    + '.tm-dim{color:var(--text-tertiary,#888)}'
    + '.tm-final{border-top:1px solid var(--border,#33343a);padding:12px 18px;max-height:34%;overflow-y:auto}'
    + '.tm-final-label{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-tertiary,#888);margin-bottom:6px}'
    + '.tm-final-pre{white-space:pre-wrap;word-break:break-word;font-size:12.5px;color:var(--text-primary,#e8e8ea);margin:0}'
    + '.tm-empty{color:var(--text-tertiary,#888);font-size:13px;padding:6px 0}'
    // status chips
    + '.tm-chip{font-size:11px;padding:2px 8px;border-radius:999px;text-transform:capitalize;flex-shrink:0}'
    + '.tm-chip-running{background:rgba(245,179,66,.16);color:#f5b342}'
    + '.tm-chip-pending{background:rgba(109,142,255,.16);color:#6d8eff}'
    + '.tm-chip-paused{background:rgba(167,139,250,.18);color:#a78bfa}'
    + '.tm-chip-done{background:rgba(52,211,153,.16);color:#34d399}'
    + '.tm-chip-error{background:rgba(248,113,113,.16);color:#f87171}'
    + '.tm-chip-aborted{background:rgba(148,148,148,.18);color:#aaa}';
  document.head.appendChild(st);
}

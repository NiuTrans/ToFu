/* ═══════════════════════════════════════════════════════════════════
   orchestration.js — Orchestration Studio (frontend authoring canvas)

   A visual, drag-and-drop builder where users compose "orchestration
   definitions" — endpoint-like loops, fan-out/synthesize flows, etc. —
   by wiring together ROLE agents (tofu mascots) and CONTROL nodes
   (start / loop / parallel / barrier / route / stop).

   ── Scope (this phase) ──────────────────────────────────────────────
   This is the AUTHORING layer only.  It produces a declarative
   definition object (see _orchToDefinition) that a backend engine will
   later interpret.  Per CLAUDE.md §3.2.0 the frontend stays a thin
   renderer/editor: it emits JSON, it does NOT run orchestration logic.
   Definitions are persisted to localStorage for now (interim store);
   migrating to a backend `/api/v1/orchestrations` store is a follow-up.

   ── Self-contained on purpose ───────────────────────────────────────
   Logic + scoped CSS (_orchInjectStyles) + modal DOM (_orchEnsureModal)
   all live in this one file so the feature is reviewable in isolation
   and carries zero risk to the 16k-line static/styles.css.  Symbols
   share window scope like every other static/js/*.js (no imports).
   ═══════════════════════════════════════════════════════════════════ */

// ── Catalogue: role agents (tofu mascots) ──────────────────────────
// `icon` is a file under static/icons/.  `tier` is the default model
// tier hint (mirrors lib/swarm/registry.py AGENT_ROLES.model_hint).
var _ORCH_ROLES = [
  { role: 'planner',     label: 'Planner',     icon: 'tofu-planner.svg',     tier: 'heavy',
    blurb: 'Rewrites the ask into a structured brief + checklist.' },
  { role: 'worker',      label: 'Worker',      icon: 'tofu-worker.svg',      tier: 'heavy',
    blurb: 'Executes the plan with full tools. Stateful across loops.' },
  { role: 'critic',      label: 'Critic',      icon: 'tofu-critic.svg',      tier: 'heavy',
    blurb: 'Reviews work against the checklist. Emits a verdict.' },
  { role: 'researcher',  label: 'Researcher',  icon: 'tofu-researcher',  tier: 'standard',
    blurb: 'Gathers + verifies info from web sources.' },
  { role: 'coder',       label: 'Coder',       icon: 'tofu-coder',       tier: 'heavy',
    blurb: 'Reads / writes / edits code across files.' },
  { role: 'analyst',     label: 'Analyst',     icon: 'tofu-analyst',     tier: 'standard',
    blurb: 'Quantitative analysis of on-disk data.' },
  { role: 'reviewer',    label: 'Reviewer',    icon: 'tofu-critic.svg',      tier: 'heavy',
    blurb: 'Fresh second-opinion read. Outputs a punch list.' },
  { role: 'writer',      label: 'Writer',      icon: 'tofu-writer',      tier: 'light',
    blurb: 'Long-form prose from raw inputs.' },
  { role: 'browser',     label: 'Browser',     icon: 'tofu-browser',     tier: 'standard',
    blurb: 'Interacts with live browser tabs.' },
  { role: 'synthesizer', label: 'Synthesizer', icon: 'tofu-synthesizer', tier: 'standard',
    blurb: 'Merges many agent outputs into one converged result.' },
  { role: 'virtual_user', label: 'Virtual User', icon: 'tofu-general', tier: 'standard',
    blurb: 'Stands in for the human: auto-replies to keep a task going until done. Speaks as User.' },
  { role: 'router',      label: 'Router',      icon: 'tofu-router',      tier: 'light',
    blurb: 'Classifies each item and routes it to a branch.' },
  { role: 'general',     label: 'General',     icon: 'tofu-general',     tier: 'standard',
    blurb: 'Versatile fallback when no specialist fits.' },
];

// ── Catalogue: control / structure nodes ───────────────────────────
// These carry the topology semantics. `kind` is the node type the
// backend engine will switch on. `single` = at most one per canvas.
var _ORCH_CONTROLS = [
  { kind: 'start',    label: 'Start',     glyph: 'play',    single: true,
    accent: '#10b981', blurb: 'Entry point. The user request flows in here.' },
  { kind: 'loop',     label: 'Loop',      glyph: 'loop',    single: false,
    accent: '#6e56cf', blurb: 'Repeat the wrapped step until a stop condition holds.' },
  { kind: 'parallel', label: 'Fan-out',   glyph: 'fanout',  single: false,
    accent: '#3b82f6', blurb: 'Run downstream agents in parallel, one per item.' },
  { kind: 'barrier',  label: 'Join',      glyph: 'join',    single: false,
    accent: '#14b8a6', blurb: 'Wait for all parallel branches, then continue.' },
  { kind: 'branch',   label: 'Route',     glyph: 'branch',  single: false,
    accent: '#f59e0b', blurb: 'Send the flow down a path chosen by a classifier.' },
  { kind: 'artifact', label: 'Deliverable', glyph: 'artifact', single: false,
    accent: '#ec4899', blurb: 'An expected intermediate output (file / report). '
      + 'Wire it between agents to make a deliverable the contract between them.' },
  { kind: 'human',    label: 'Human',     glyph: 'human',   single: false,
    accent: '#0ea5e9', blurb: 'A human-in-the-loop gate: pause for approval, '
      + 'collect an answer, or notify the user mid-flow.' },
  { kind: 'stop',     label: 'Stop',      glyph: 'stop',    single: true,
    accent: '#ef4444', blurb: 'Terminal. The converged result returns to chat.' },
];

// Inline SVG glyphs for control nodes (abstract, theme-colored).
var _ORCH_GLYPHS = {
  play:   '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>',
  loop:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 2l4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/></svg>',
  fanout: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="12" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="18" cy="12" r="2"/><circle cx="18" cy="19" r="2"/><path d="M8 12h2M11 11l5-5M11 13l5 5"/></svg>',
  join:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="5" r="2"/><circle cx="6" cy="12" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="12" r="2"/><path d="M8 5l5 6M8 12h2M8 19l5-6"/></svg>',
  branch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="12" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="18" cy="18" r="2"/><path d="M8 12h3M11 11l5-4M11 13l5 4"/></svg>',
  stop:   '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  artifact: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/></svg>',
  human:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="7" r="4"/><path d="M5.5 21a6.5 6.5 0 0 1 13 0"/></svg>',
  group:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="3" stroke-dasharray="4 3"/><rect x="7" y="7" width="4" height="4" rx="1"/><rect x="13" y="13" width="4" height="4" rx="1"/><path d="M11 9h2a2 2 0 0 1 2 2v2"/></svg>',
};

// ── Inline UI icons (SVG-only; NO emoji) ────────────────────────────
// House rule for the orchestration surface: every icon is an inline SVG
// glyph, never an emoji — even for abstract concepts. Each entry is a
// self-sized <svg class="orch-ico"> (1em, currentColor) safe to splice
// into button labels and run-log lines (which use innerHTML).
var _orchSvg = function (inner, big) {
  return '<svg class="orch-ico' + (big ? ' orch-ico-lg' : '') + '" viewBox="0 0 24 24" '
    + 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    + 'stroke-linejoin="round" aria-hidden="true">' + inner + '</svg>';
};
var _ORCH_ICONS = {
  plus:    _orchSvg('<path d="M12 5v14M5 12h14"/>'),
  gear:    _orchSvg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>'),
  layout:  _orchSvg('<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>'),
  star:    '<svg class="orch-ico" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 2l2.9 6.3 6.9.7-5.1 4.6 1.4 6.8L12 17.8 5.9 20.4l1.4-6.8L2.2 9l6.9-.7z"/></svg>',
  loop:    _orchSvg('<path d="M17 2l4 4-4 4"/><path d="M3 11v-1a4 4 0 0 1 4-4h14"/><path d="M7 22l-4-4 4-4"/><path d="M21 13v1a4 4 0 0 1-4 4H3"/>'),
  auto:    _orchSvg('<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/><path d="M12 3v4M12 17v4M3 12h4M17 12h4"/>'),
  fanout:  _orchSvg('<circle cx="6" cy="12" r="2"/><circle cx="18" cy="5" r="2"/><circle cx="18" cy="12" r="2"/><circle cx="18" cy="19" r="2"/><path d="M8 12h2M11 11l5-5M11 13l5 5"/>'),
  shield:  _orchSvg('<path d="M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>'),
  folder:  _orchSvg('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'),
  wand:    _orchSvg('<path d="M15 4V2M15 10V8M11 6H9M21 6h-2M18.5 3.5l-1 1M11.5 3.5l1 1M5 21l11-11"/>'),
  save:    _orchSvg('<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/>'),
  puzzle:  _orchSvg('<path d="M15.39 4.39a1 1 0 0 0 1.68-.474 2.5 2.5 0 1 1 3.014 3.015 1 1 0 0 0-.474 1.68l1.683 1.682a2.414 2.414 0 0 1 0 3.414L19.61 15.39a1 1 0 0 1-1.68-.474 2.5 2.5 0 1 0-3.014 3.015 1 1 0 0 1 .474 1.68l-1.683 1.682a2.414 2.414 0 0 1-3.414 0L8.61 19.61a1 1 0 0 0-1.68.474 2.5 2.5 0 1 1-3.014-3.015 1 1 0 0 0 .474-1.68l-1.683-1.682a2.414 2.414 0 0 1 0-3.414L4.39 8.61a1 1 0 0 1 1.68.474 2.5 2.5 0 1 0 3.014-3.015 1 1 0 0 1-.474-1.68l1.683-1.682a2.414 2.414 0 0 1 3.414 0z"/>', true),
  speak:   _orchSvg('<path d="M21 11.5a8.38 8.38 0 0 1-9 8.3 8.5 8.5 0 0 1-3.8-.9L3 20l1.1-3.3A8.38 8.38 0 0 1 12 3.5a8.5 8.5 0 0 1 9 8z"/>'),
  eye:     _orchSvg('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>'),
  rocket:  _orchSvg('<path d="M5 15c-1 1-1.5 4-1.5 4s3-.5 4-1.5"/><path d="M9 11a12 12 0 0 1 8-8c2 0 3 1 3 3a12 12 0 0 1-8 8z"/><path d="M9 11l-3 1 3 5 1-3"/><circle cx="14.5" cy="9.5" r="1.5"/>'),
  bot:     _orchSvg('<rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 8V5M9 4h6"/><circle cx="9" cy="14" r="1"/><circle cx="15" cy="14" r="1"/>'),
  check:   '<svg class="orch-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6L9 17l-5-5"/></svg>',
  reject:  _orchSvg('<circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/>'),
  warn:    _orchSvg('<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>'),
  compass: _orchSvg('<circle cx="12" cy="12" r="9"/><path d="M16 8l-2 6-6 2 2-6z"/>'),
  package: _orchSvg('<path d="M21 8l-9-5-9 5v8l9 5 9-5z"/><path d="M3 8l9 5 9-5M12 13v8"/>'),
  person:  _orchSvg('<circle cx="12" cy="8" r="4"/><path d="M5 21a7 7 0 0 1 14 0"/>'),
  flag:    _orchSvg('<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>'),
  stop:    '<svg class="orch-ico" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
};

// ── Canvas state ────────────────────────────────────────────────────
var _orchNodes = [];          // [{id, type:'role'|'control', role?, kind?, x, y, name, params}]
var _orchEdges = [];          // [{id, from, to}]
var _orchSel = null;          // selected node id
var _orchSeq = 0;             // id counter
var _orchName = 'Untitled Flow';
var _orchModalReady = false;
var _orchConnect = null;      // active connection drag {from, x, y}
var _orchDragNode = null;     // active node-move drag {id, dx, dy}
var _orchCurrentId = null;    // backend id of the loaded/saved flow (null = unsaved)
// Nested-canvas edit stack. Each frame = a parent editing context we left
// to descend into a GROUP (subflow) node. Frame:
//   {nodes, edges, sel, seq, name, groupId}
// where (nodes/edges/sel/seq/name) are the parent level's working state and
// groupId is the subflow node on that level whose child we are now editing.
// Empty = editing the root flow.
var _orchStack = [];

var _ORCH_CARD_W = 188;       // must match .orch-node width in CSS

function _orchNextId(prefix) { _orchSeq++; return (prefix || 'n') + _orchSeq; }

function _orchIconBase() {
  return (typeof BASE_PATH !== 'undefined' ? BASE_PATH : '') + '/static/icons';
}

// Resolve a role icon to a full URL. An `icon` carrying an explicit
// extension (e.g. 'tofu-worker.svg') is used as-is; otherwise '.png' is
// appended. Lets crisp SVGs and cleaned PNGs coexist in _ORCH_ROLES.
function _orchIconSrc(icon) {
  var name = icon || 'tofu-general';
  return _orchIconBase() + '/' + (/\.\w+$/.test(name) ? name : name + '.png');
}

// ════════════════════════════════════════════════════════════════════
//  Open / close + one-time modal & style injection
// ════════════════════════════════════════════════════════════════════

function openOrchestration() {
  _orchEnsureModal();
  var ov = document.getElementById('orchModal');
  if (ov) ov.style.display = 'flex';
  if (!_orchNodes.length) _orchLoadTemplate('endpoint');
  _orchRender();
  _orchFetchRoleSchema();   // refresh structured-param fields (async, no-op if cached)
}

function closeOrchestration(evt) {
  var ov = document.getElementById('orchModal');
  if (!ov) return;
  if (evt && evt.target !== ov) return;   // ignore clicks inside the dialog
  ov.style.display = 'none';
}

function _orchEnsureModal() {
  if (_orchModalReady) return;
  _orchInjectStyles();

  var ov = document.createElement('div');
  ov.className = 'orch-overlay';
  ov.id = 'orchModal';
  ov.style.display = 'none';
  ov.addEventListener('click', function (e) { closeOrchestration(e); });

  ov.innerHTML = ''
    + '<div class="orch-shell" role="dialog" aria-label="Orchestration Studio">'
    +   '<header class="orch-top">'
    +     '<div class="orch-top-left">'
    +       '<span class="orch-logo"><img src="' + _orchIconBase() + '/tofu-planner.svg" alt="" width="22" height="22"></span>'
    +       '<input id="orchNameInput" class="orch-name-input" spellcheck="false" '
    +              'oninput="_orchOnRename(this.value)" />'
    +     '</div>'
    +     '<div class="orch-top-actions">'
    +       '<button class="orch-btn orch-btn-ghost orch-m-only orch-m-pal-btn" onclick="_orchToggleMobilePalette()">' + _ORCH_ICONS.plus + ' Nodes</button>'
    +       '<button class="orch-btn orch-btn-ghost orch-m-only orch-m-insp-btn" onclick="_orchToggleMobileInspector()">' + _ORCH_ICONS.gear + ' Edit</button>'
    +       '<div class="orch-tpl-wrap">'
    +         '<button class="orch-btn orch-btn-ghost" onclick="_orchToggleTplMenu()">' + _ORCH_ICONS.wand + ' Templates ▾</button>'
    +         '<div class="orch-tpl-menu" id="orchTplMenu" style="display:none">'
    +           '<button onclick="_orchLoadTemplate(\'endpoint\');_orchToggleTplMenu(true)">' + _ORCH_ICONS.loop + ' Endpoint loop (plan→work→critic)</button>'
    +           '<button onclick="_orchLoadBuiltin(\'endpoint\');_orchToggleTplMenu(true)">' + _ORCH_ICONS.star + ' Endpoint (canonical, backend)</button>'
    +           '<button onclick="_orchLoadTemplate(\'autopilot\');_orchToggleTplMenu(true)">' + _ORCH_ICONS.auto + ' Autopilot (worker ⇄ virtual user)</button>'
    +           '<button onclick="_orchLoadTemplate(\'fanout\');_orchToggleTplMenu(true)">' + _ORCH_ICONS.fanout + ' Fan-out → synthesize</button>'
    +           '<button onclick="_orchLoadTemplate(\'adversarial\');_orchToggleTplMenu(true)">' + _ORCH_ICONS.shield + ' Adversarial verify</button>'
    +           '<button onclick="_orchLoadTemplate(\'blank\');_orchToggleTplMenu(true)">' + _ORCH_ICONS.plus + ' Blank canvas</button>'
    +         '</div>'
    +       '</div>'
    +       '<div class="orch-tpl-wrap">'
    +         '<button class="orch-btn orch-btn-ghost" onclick="_orchOpenLoadMenu()">' + _ORCH_ICONS.folder + ' Open ▾</button>'
    +         '<div class="orch-load-menu" id="orchLoadMenu" style="display:none"></div>'
    +       '</div>'
    +       '<button class="orch-btn orch-btn-ghost" onclick="_orchTidy()" title="Auto-arrange nodes into clean top-down lanes">⤓ Tidy</button>'
    +       '<span class="orch-top-sep" aria-hidden="true"></span>'
    +       '<button class="orch-btn orch-btn-ghost" id="orchAiToggle" onclick="_orchToggleAi()">' + _ORCH_ICONS.wand + ' AI Composer</button>'
    +       '<span class="orch-top-sep" aria-hidden="true"></span>'
    +       '<button class="orch-btn orch-btn-run" onclick="_orchOpenRun()">▶ Run</button>'
    +       '<button class="orch-btn orch-btn-ghost" onclick="_orchExport()">⬇ Export JSON</button>'
    +       '<button class="orch-btn orch-btn-primary" onclick="_orchSave()">' + _ORCH_ICONS.save + ' Save</button>'
    +       '<span class="orch-top-sep" aria-hidden="true"></span>'
    +       '<button class="orch-btn orch-btn-close" onclick="closeOrchestration()" title="Close">✕</button>'
    +     '</div>'
    +   '</header>'
    +   '<div class="orch-body">'
    +     '<aside class="orch-ai" id="orchAi">'
    +       '<div class="orch-ai-head"><span>' + _ORCH_ICONS.wand + ' AI Composer</span>'
    +         '<button class="orch-ai-clear" onclick="_orchAiClear()" title="Clear chat">⟲</button></div>'
    +       '<div class="orch-ai-log" id="orchAiLog"></div>'
    +       '<div class="orch-ai-input">'
    +         '<textarea id="orchAiText" rows="2" placeholder="Describe the flow you want, or ask to change it…" '
    +              'onkeydown="_orchAiKey(event)"></textarea>'
    +         '<button class="orch-btn orch-btn-primary orch-ai-send" id="orchAiSend" onclick="_orchAiSend()">Send</button>'
    +       '</div>'
    +     '</aside>'
    +     '<aside class="orch-palette" id="orchPalette"></aside>'
    +     '<main class="orch-canvas-wrap">'
    +       '<div class="orch-crumb" id="orchCrumb" style="display:none"></div>'
    +       '<div class="orch-canvas" id="orchCanvas">'
    +         '<svg class="orch-edges" id="orchEdges"></svg>'
    +         '<div class="orch-nodes" id="orchNodes"></div>'
    +         '<div class="orch-hint" id="orchHint"></div>'
    +       '</div>'
    +     '</main>'
    +     '<aside class="orch-inspector" id="orchInspector"></aside>'
    +   '</div>'
    +   '<div class="orch-run-drawer" id="orchRunDrawer">'
    +     '<div class="orch-run-head">'
    +       '<span>▶ Run Flow</span>'
    +       '<button class="orch-ai-clear" onclick="_orchCloseRun()" title="Close">✕</button>'
    +     '</div>'
    +     '<div class="orch-run-input">'
    +       '<textarea id="orchRunInput" rows="2" placeholder="Initial request / input for the flow (optional)…"></textarea>'
    +       '<div class="orch-run-actions">'
    +         '<button class="orch-btn orch-btn-ghost" onclick="_orchPlan()">' + _ORCH_ICONS.eye + ' Preview plan</button>'
    +         '<button class="orch-btn orch-btn-run" id="orchRunBtn" onclick="_orchRun()">▶ Run</button>'
    +         '<button class="orch-btn orch-btn-primary" id="orchRunTaskBtn" onclick="_orchRunAsTask()" title="Launch a durable, reopenable run and open it in Task Mode">' + _ORCH_ICONS.rocket + ' Run as Task</button>'
    +         '<button class="orch-btn orch-btn-danger" id="orchRunAbort" onclick="_orchRunAbort()" style="display:none">Stop</button>'
    +       '</div>'
    +     '</div>'
    +     '<div class="orch-run-log" id="orchRunLog"></div>'
    +   '</div>'
    + '</div>';

  document.body.appendChild(ov);
  _orchModalReady = true;

  _orchRenderPalette();
  _orchWireCanvas();
}

// ════════════════════════════════════════════════════════════════════
//  Palette (left rail) — draggable source chips
// ════════════════════════════════════════════════════════════════════

function _orchRenderPalette() {
  var el = document.getElementById('orchPalette');
  if (!el) return;
  var base = _orchIconBase();

  var html = '<div class="orch-sheet-head orch-m-only">'
           + '<span>' + _ORCH_ICONS.plus + ' ' + escapeHtml(t('orch.palette.agents')) + '</span>'
           + '<button class="orch-ai-clear" onclick="_orchCloseMobilePalette()" title="Close">✕</button></div>';
  html += '<div class="orch-m-only orch-sheet-hint">' + escapeHtml(t('orch.palette.tapHint')) + '</div>';
  html += '<div class="orch-pal-section">' + escapeHtml(t('orch.palette.control')) + '</div><div class="orch-pal-grid">';
  _ORCH_CONTROLS.forEach(function (c) {
    html += '<div class="orch-chip orch-chip-ctrl" draggable="true" '
         +  'data-ptype="control" data-pkind="' + c.kind + '" '
         +  'style="--chip-accent:' + c.accent + '" title="' + escapeHtml(c.blurb) + '">'
         +    '<span class="orch-chip-glyph">' + _ORCH_GLYPHS[c.glyph] + '</span>'
         +    '<span class="orch-chip-label">' + escapeHtml(c.label) + '</span>'
         +  '</div>';
  });
  html += '</div>';

  // Group (subflow) — a black-box sub-flow that looks like one role.
  html += '<div class="orch-pal-section">' + escapeHtml(t('orch.palette.group')) + '</div><div class="orch-pal-grid">';
  html += '<div class="orch-chip orch-chip-ctrl orch-chip-group" draggable="true" '
       +  'data-ptype="subflow" data-prole="general" '
       +  'style="--chip-accent:#8b5cf6" title="' + escapeHtml(t('orch.group.chipTip')) + '">'
       +    '<span class="orch-chip-glyph">' + _ORCH_GLYPHS.group + '</span>'
       +    '<span class="orch-chip-label">' + escapeHtml(t('orch.group.chip')) + '</span>'
       +  '</div>';
  html += '</div>';

  html += '<div class="orch-pal-section">' + escapeHtml(t('orch.palette.agents')) + '</div><div class="orch-pal-grid">';
  _ORCH_ROLES.forEach(function (r) {
    html += '<div class="orch-chip orch-chip-role" draggable="true" '
         +  'data-ptype="role" data-prole="' + r.role + '" '
         +  'title="' + escapeHtml(r.blurb) + '">'
         +    '<span class="orch-chip-ava"><img src="' + _orchIconSrc(r.icon) + '" alt="" '
         +        'onerror="this.style.display=\'none\'"></span>'
         +    '<span class="orch-chip-label">' + escapeHtml(r.label) + '</span>'
         +  '</div>';
  });
  html += '</div>';
  html += '<div class="orch-pal-foot">' + escapeHtml(t('orch.palette.foot')) + '</div>';
  el.innerHTML = html;

  el.querySelectorAll('.orch-chip').forEach(function (chip) {
    function _payload() {
      return {
        ptype: chip.getAttribute('data-ptype'),
        role: chip.getAttribute('data-prole') || '',
        kind: chip.getAttribute('data-pkind') || '',
      };
    }
    chip.addEventListener('dragstart', function (e) {
      e.dataTransfer.setData('text/orch', JSON.stringify(_payload()));
      e.dataTransfer.effectAllowed = 'copy';
    });
    // Touch devices can't HTML5-drag from the rail, so tap-to-add drops the
    // node into the visible centre of the canvas and closes the palette sheet.
    chip.addEventListener('click', function () {
      if (!_orchIsMobile()) return;
      _orchAddNodeAtCenter(_payload());
      _orchCloseMobilePalette();
    });
  });
}

// ── Mobile helpers ──────────────────────────────────────────────────
// On phones the side rails become slide-up sheets and HTML5 drag is
// replaced by tap-to-add; these helpers gate that behaviour on viewport.
function _orchIsMobile() {
  return window.matchMedia && window.matchMedia('(max-width:768px)').matches;
}

function _orchAddNodeAtCenter(payload) {
  var canvas = document.getElementById('orchCanvas');
  if (!canvas) return;
  var x = canvas.scrollLeft + canvas.clientWidth / 2 - _ORCH_CARD_W / 2;
  var y = canvas.scrollTop + canvas.clientHeight / 2 - 40;
  _orchAddNode(payload, Math.max(8, x), Math.max(8, y));
}

function _orchToggleMobilePalette() {
  var shell = document.querySelector('.orch-shell');
  if (!shell) return;
  shell.classList.remove('orch-m-insp');
  shell.classList.toggle('orch-m-pal');
}
function _orchCloseMobilePalette() {
  var shell = document.querySelector('.orch-shell');
  if (shell) shell.classList.remove('orch-m-pal');
}
function _orchToggleMobileInspector() {
  var shell = document.querySelector('.orch-shell');
  if (!shell) return;
  shell.classList.remove('orch-m-pal');
  shell.classList.toggle('orch-m-insp');
}
function _orchCloseMobileInspector() {
  var shell = document.querySelector('.orch-shell');
  if (shell) shell.classList.remove('orch-m-insp');
}

// ════════════════════════════════════════════════════════════════════
//  Canvas wiring — drop, node move, connection drag
// ════════════════════════════════════════════════════════════════════

function _orchWireCanvas() {
  var canvas = document.getElementById('orchCanvas');
  if (!canvas) return;

  canvas.addEventListener('dragover', function (e) {
    if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, 'text/orch') !== -1) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    }
  });
  canvas.addEventListener('drop', function (e) {
    var raw = e.dataTransfer.getData('text/orch');
    if (!raw) return;
    e.preventDefault();
    var payload;
    try { payload = JSON.parse(raw); } catch (_) { return; }
    var rect = canvas.getBoundingClientRect();
    var x = e.clientX - rect.left + canvas.scrollLeft - _ORCH_CARD_W / 2;
    var y = e.clientY - rect.top + canvas.scrollTop - 20;
    _orchAddNode(payload, Math.max(8, x), Math.max(8, y));
  });

  // Click empty canvas → deselect.
  canvas.addEventListener('pointerdown', function (e) {
    if (e.target === canvas || e.target.id === 'orchNodes' || e.target.id === 'orchEdges') {
      _orchSel = null; _orchRenderNodes(); _orchRenderInspector();
    }
  });

  // Global pointer handlers for node-move + connection drag.
  canvas.addEventListener('pointermove', _orchOnPointerMove);
  window.addEventListener('pointerup', _orchOnPointerUp);
}

function _orchAddNode(payload, x, y) {
  // Enforce "single" control nodes (start / stop).
  if (payload.ptype === 'control') {
    var def = _ORCH_CONTROLS.filter(function (c) { return c.kind === payload.kind; })[0];
    if (def && def.single && _orchNodes.some(function (n) { return n.kind === payload.kind; })) {
      _orchToast('Only one ' + def.label + ' node allowed.');
      return;
    }
  }
  var node = {
    id: _orchNextId(payload.ptype === 'role' ? payload.role
                    : payload.ptype === 'subflow' ? 'group' : payload.kind),
    type: payload.ptype,
    role: payload.role || '',
    kind: payload.kind || '',
    x: x, y: y,
    name: '',
    params: _orchDefaultParams(payload),
  };
  _orchNodes.push(node);
  _orchSel = node.id;
  _orchRender();
}

// A freshly-dropped Group must carry a VALID embedded child definition or
// the parent fails backend validation (a subflow node requires
// params.definition or params.ref — see lib/orchestration._validate_subflow_node).
// Seed it with the minimal runnable flow: start → general agent → stop.
function _orchBlankGroupDefinition() {
  return {
    schema: 'tofu.orchestration/v1',
    name: t('orch.group.defaultLabel'),
    nodes: [
      { id: 'gstart', type: 'control', kind: 'start', params: {} },
      { id: 'gagent', type: 'role', role: 'general',
        params: { objective: '', tier: 'standard', isolation: 'fresh-context' } },
      { id: 'gstop', type: 'control', kind: 'stop', params: {} },
    ],
    edges: [
      { from: 'gstart', to: 'gagent' },
      { from: 'gagent', to: 'gstop' },
    ],
  };
}

function _orchDefaultParams(payload) {
  if (payload.ptype === 'role') {
    var rdef = /** @type {any} */ (_ORCH_ROLES.filter(function (r) { return r.role === payload.role; })[0] || {});
    return { objective: '', tier: rdef.tier || 'standard', isolation: 'fresh-context' };
  }
  if (payload.ptype === 'subflow') {
    // Default scope = isolated (a true black box) — the whole point of a Group.
    return { scope: 'isolated', definition: _orchBlankGroupDefinition() };
  }
  switch (payload.kind) {
    case 'start':    return { seed: '' };
    case 'loop':     return { max_iterations: 10, stop_condition: 'verdict:STOP', verifier: 'critic' };
    case 'parallel': return { max_concurrent: 8, per_item: true };
    case 'branch':   return { classifier: 'router', branches: 2 };
    case 'artifact': return { path: '', description: '', format: 'file' };
    case 'human':    return { mode: 'approve', prompt: '' };
    default:         return {};
  }
}

// ── Node move ──
function _orchNodeHeaderDown(e, id) {
  e.stopPropagation();
  var node = _orchFind(id);
  if (!node) return;
  _orchSel = id;
  var el = document.getElementById('orch-node-' + id);
  var canvas = document.getElementById('orchCanvas');
  var rect = canvas.getBoundingClientRect();
  var px = e.clientX - rect.left + canvas.scrollLeft;
  var py = e.clientY - rect.top + canvas.scrollTop;
  _orchDragNode = { id: id, dx: px - node.x, dy: py - node.y };
  if (el) el.classList.add('is-dragging');
  _orchRenderNodes(); _orchRenderInspector();
}

// ── Connection drag (from out-port) ──
function _orchPortDown(e, id) {
  e.stopPropagation();
  var canvas = document.getElementById('orchCanvas');
  var rect = canvas.getBoundingClientRect();
  _orchConnect = {
    from: id,
    x: e.clientX - rect.left + canvas.scrollLeft,
    y: e.clientY - rect.top + canvas.scrollTop,
  };
}

function _orchPortUp(e, id) {
  if (!_orchConnect) return;
  e.stopPropagation();
  if (_orchConnect.from && _orchConnect.from !== id) {
    _orchConnectNodes(_orchConnect.from, id);
  }
  _orchConnect = null;
  _orchRender();
}

function _orchConnectNodes(from, to) {
  // No duplicate edges; no self-loop; target must not be a Start.
  var t = _orchFind(to);
  if (t && t.kind === 'start') { _orchToast('Start has no input.'); return; }
  var s = _orchFind(from);
  if (s && s.kind === 'stop') { _orchToast('Stop has no output.'); return; }
  if (_orchEdges.some(function (e) { return e.from === from && e.to === to; })) return;
  _orchEdges.push({ id: _orchNextId('e'), from: from, to: to });
}

function _orchOnPointerMove(e) {
  var canvas = document.getElementById('orchCanvas');
  if (!canvas) return;
  var rect = canvas.getBoundingClientRect();
  var px = e.clientX - rect.left + canvas.scrollLeft;
  var py = e.clientY - rect.top + canvas.scrollTop;

  if (_orchDragNode) {
    var node = _orchFind(_orchDragNode.id);
    if (node) {
      node.x = Math.max(4, px - _orchDragNode.dx);
      node.y = Math.max(4, py - _orchDragNode.dy);
      var el = document.getElementById('orch-node-' + node.id);
      if (el) { el.style.left = node.x + 'px'; el.style.top = node.y + 'px'; }
      _orchRenderEdges();
    }
  } else if (_orchConnect) {
    _orchConnect.x = px; _orchConnect.y = py;
    _orchRenderEdges();
  }
}

function _orchOnPointerUp() {
  if (_orchDragNode) {
    var el = document.getElementById('orch-node-' + _orchDragNode.id);
    if (el) el.classList.remove('is-dragging');
    _orchDragNode = null;
  }
  if (_orchConnect) { _orchConnect = null; _orchRenderEdges(); }
}

function _orchFind(id) {
  for (var i = 0; i < _orchNodes.length; i++) if (_orchNodes[i].id === id) return _orchNodes[i];
  return null;
}

function _orchDeleteNode(id) {
  _orchNodes = _orchNodes.filter(function (n) { return n.id !== id; });
  _orchEdges = _orchEdges.filter(function (e) { return e.from !== id && e.to !== id; });
  if (_orchSel === id) _orchSel = null;
  _orchRender();
}

function _orchDeleteEdge(id) {
  _orchEdges = _orchEdges.filter(function (e) { return e.id !== id; });
  _orchRenderEdges();
}

// ════════════════════════════════════════════════════════════════════
//  Render
// ════════════════════════════════════════════════════════════════════

function _orchRender() {
  var ni = document.getElementById('orchNameInput');
  if (ni && ni.value !== _orchName) ni.value = _orchName;
  _orchRenderNodes();
  _orchRenderEdges();
  _orchRenderInspector();
  _orchRenderHint();
  _orchRenderBreadcrumb();
}

function _orchRenderHint() {
  var h = document.getElementById('orchHint');
  if (!h) return;
  h.style.display = _orchNodes.length ? 'none' : 'block';
  if (!_orchNodes.length) {
    h.innerHTML = '<div class="orch-hint-card">'
      + '<div class="orch-hint-emoji">' + _ORCH_ICONS.puzzle + '</div>'
      + '<div class="orch-hint-title">Compose a flow</div>'
      + '<div class="orch-hint-text">Drag a <b>Start</b> node and some agents from the left, '
      + 'then drag between the ● ports to wire them. Load a template to see a working loop.</div>'
      + '</div>';
  }
}

function _orchRenderNodes() {
  var wrap = document.getElementById('orchNodes');
  if (!wrap) return;
  var base = _orchIconBase();
  var html = '';
  _orchNodes.forEach(function (n) {
    var selCls = (_orchSel === n.id) ? ' is-selected' : '';
    var accent, iconHtml, sub, typeCls;
    if (n.type === 'subflow') {
      accent = '#8b5cf6';
      typeCls = ' orch-node-group';
      iconHtml = _ORCH_GLYPHS.group;
      sub = _orchGroupSub(n);
    } else if (n.type === 'role') {
      var rdef = /** @type {any} */ (_ORCH_ROLES.filter(function (r) { return r.role === n.role; })[0] || {});
      accent = '#6e56cf';
      typeCls = ' orch-node-role';
      iconHtml = '<img src="' + _orchIconSrc(rdef.icon) + '" alt="" '
               + 'onerror="this.style.display=\'none\'">';
      sub = escapeHtml(n.params.tier || 'standard') + ' · ' + escapeHtml(n.params.isolation || 'fresh');
      var _eff = n.params.emits || _orchDefaultEmits(n.role);
      if (_eff === 'user') sub += ' · ' + _ORCH_ICONS.speak + 'user';
    } else {
      var cdef = /** @type {any} */ (_ORCH_CONTROLS.filter(function (c) { return c.kind === n.kind; })[0] || {});
      accent = cdef.accent || '#888';
      typeCls = ' orch-node-ctrl orch-node-' + (n.kind || 'ctrl');
      iconHtml = _ORCH_GLYPHS[cdef.glyph] || '';
      sub = _orchControlSub(n);
    }
    var title = escapeHtml(n.name || _orchAutoLabel(n));
    var hasIn = n.kind !== 'start';
    var hasOut = n.kind !== 'stop';

    html += '<div class="orch-node' + typeCls + selCls + '" id="orch-node-' + n.id + '" '
         +  'style="left:' + n.x + 'px;top:' + n.y + 'px;--node-accent:' + accent + '" '
         +  'onpointerdown="_orchSelectNode(\'' + n.id + '\')">';
    if (n.kind === 'start') {
      html += '<span class="orch-node-ribbon orch-ribbon-in">INPUT</span>';
    } else if (n.kind === 'stop') {
      html += '<span class="orch-node-ribbon orch-ribbon-out">RESULT</span>';
    }
    if (hasIn) {
      html += '<span class="orch-port orch-port-in" onpointerup="_orchPortUp(event,\'' + n.id + '\')"></span>';
    }
    var headExtra = (n.type === 'subflow')
      ? ' ondblclick="_orchEnterGroup(\'' + n.id + '\')" title="' + escapeHtml(t('orch.group.chipTip')) + '"'
      : '';
    html += '<div class="orch-node-head" onpointerdown="_orchNodeHeaderDown(event,\'' + n.id + '\')"' + headExtra + '>'
         +    '<span class="orch-node-icon">' + iconHtml + '</span>'
         +    '<span class="orch-node-title">' + title + '</span>'
         +    '<button class="orch-node-del" onpointerdown="event.stopPropagation()" '
         +        'onclick="_orchDeleteNode(\'' + n.id + '\')" title="Delete">✕</button>'
         +  '</div>'
         +  '<div class="orch-node-sub">' + sub + '</div>';
    if (hasOut) {
      html += '<span class="orch-port orch-port-out" onpointerdown="_orchPortDown(event,\'' + n.id + '\')"></span>';
    }
    html += '</div>';
  });
  wrap.innerHTML = html;
}

function _orchControlSub(n) {
  if (n.kind === 'loop') return 'max ' + (n.params.max_iterations || 10) + ' · ' + escapeHtml(n.params.stop_condition || '');
  if (n.kind === 'parallel') return 'concurrency ' + (n.params.max_concurrent || 8);
  if (n.kind === 'branch') return (n.params.branches || 2) + ' branches';
  if (n.kind === 'artifact') return escapeHtml(n.params.path || 'deliverable');
  if (n.kind === 'human') {
    var hm = { approve: 'approval gate', input: 'collect input', notify: 'notify user' };
    return hm[n.params.mode] || 'approval gate';
  }
  if (n.kind === 'start') {
    var sd = ((n.params && n.params.seed) || '').trim();
    return sd ? '\u25b6 ' + escapeHtml(sd.slice(0, 42)) : 'click to set the input \u25b8';
  }
  if (n.kind === 'stop') return 'result returns to chat';
  return '';
}

function _orchGroupSub(n) {
  var d = (n.params && n.params.definition) || {};
  var nn = (d.nodes || []).length;
  var scope = (n.params && n.params.scope) || 'isolated';
  var glyph = (scope === 'isolated') ? '\u25a3' : '\u25a4';   // ▣ box / ▤ flatten
  return glyph + ' ' + escapeHtml(scope) + ' · ' + nn + ' nodes';
}

function _orchAutoLabel(n) {
  if (n.type === 'subflow') return t('orch.group.defaultLabel');
  if (n.type === 'role') {
    var r = _ORCH_ROLES.filter(function (x) { return x.role === n.role; })[0];
    return r ? r.label : n.role;
  }
  var c = _ORCH_CONTROLS.filter(function (x) { return x.kind === n.kind; })[0];
  return c ? c.label : n.kind;
}

function _orchSelectNode(id) {
  if (_orchDragNode) return;     // selection happens via header-down already
  _orchSel = id;
  _orchRenderNodes();
  _orchRenderInspector();
}

// ── Nested-canvas navigation (Group / subflow black box) ──
// Load working canvas arrays from a child definition (without touching
// _orchCurrentId — that belongs to the ROOT flow only).
function _orchLoadWorkingFromDef(def) {
  _orchName = def.name || t('orch.group.defaultLabel');
  _orchSel = null; _orchSeq = 0;
  _orchNodes = (def.nodes || []).map(function (n) {
    return {
      id: n.id, type: n.type, role: n.role || '', kind: n.kind || '',
      x: (n.pos && n.pos.x) || 20, y: (n.pos && n.pos.y) || 20,
      name: n.name || '', params: n.params || {},
    };
  });
  _orchNodes.forEach(function (n) {
    var m = /(\d+)$/.exec(n.id || '');
    if (m) _orchSeq = Math.max(_orchSeq, parseInt(m[1], 10));
  });
  _orchEdges = (def.edges || []).map(function (e) {
    return { id: _orchNextId('e'), from: e.from, to: e.to };
  });
  _orchRender();
  var needsLayout = (def.nodes || []).some(function (n) {
    return !n.pos || typeof n.pos.x !== 'number' || typeof n.pos.y !== 'number';
  });
  if (needsLayout && _orchNodes.length) _orchTidy({ silent: true });
}

// Descend into a Group node: push the current level, edit its child flow.
function _orchEnterGroup(id) {
  var n = _orchFind(id);
  if (!n || n.type !== 'subflow') return;
  _orchStack.push({
    nodes: _orchNodes, edges: _orchEdges, sel: _orchSel,
    seq: _orchSeq, name: _orchName, groupId: id,
  });
  var def = (n.params && n.params.definition) || _orchBlankGroupDefinition();
  _orchLoadWorkingFromDef(def);
}

// Commit the current child level back into its parent Group node and pop
// one frame. The serialized child becomes params.definition (and any stale
// ref is dropped — an edited embedded child is authoritative).
function _orchExitGroup() {
  if (!_orchStack.length) return;
  var childDef = _orchToDefinition();
  var frame = _orchStack.pop();
  _orchNodes = frame.nodes; _orchEdges = frame.edges;
  _orchSel = frame.sel; _orchSeq = frame.seq; _orchName = frame.name;
  var gnode = _orchFind(frame.groupId);
  if (gnode) {
    gnode.params = gnode.params || {};
    gnode.params.definition = childDef;
    delete gnode.params.ref;
  }
  _orchRender();
}

// Collapse all open group frames back to the root flow. Called before any
// operation that must act on the WHOLE flow (save / export / run / plan).
function _orchFlushToRoot() {
  while (_orchStack.length) _orchExitGroup();
}

// Jump straight to a given depth (breadcrumb click). 0 = root.
function _orchCrumbTo(depth) {
  while (_orchStack.length > depth) _orchExitGroup();
}

function _orchRenderBreadcrumb() {
  var el = document.getElementById('orchCrumb');
  if (!el) return;
  if (!_orchStack.length) { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.style.display = 'flex';
  var parts = ['<button class="orch-crumb-item" onclick="_orchCrumbTo(0)">'
    + escapeHtml(t('orch.crumb.root')) + '</button>'];
  _orchStack.forEach(function (frame, i) {
    // A frame's label = the GROUP node's label on its parent level, which
    // is stored in that frame's own nodes array (the parent's working set).
    var gn = null;
    for (var k = 0; k < frame.nodes.length; k++) {
      if (frame.nodes[k].id === frame.groupId) { gn = frame.nodes[k]; break; }
    }
    var lbl = (gn && (gn.name || _orchAutoLabel(gn))) || t('orch.group.defaultLabel');
    parts.push('<span class="orch-crumb-sep">\u203a</span>');
    // Clicking a crumb returns to the level INSIDE that group (depth i+1).
    parts.push('<button class="orch-crumb-item" onclick="_orchCrumbTo(' + (i + 1) + ')">'
      + escapeHtml(lbl) + '</button>');
  });
  el.innerHTML = parts.join('');
}

// ── Edges (SVG bezier layer) ──
function _orchPortCenter(id, which) {
  var canvas = document.getElementById('orchCanvas');
  var portEl = document.querySelector('#orch-node-' + id + ' .orch-port-' + which);
  if (!canvas || !portEl) return null;
  var cr = canvas.getBoundingClientRect();
  var pr = portEl.getBoundingClientRect();
  return {
    x: pr.left - cr.left + canvas.scrollLeft + pr.width / 2,
    y: pr.top - cr.top + canvas.scrollTop + pr.height / 2,
  };
}

function _orchRenderEdges() {
  var svg = document.getElementById('orchEdges');
  var canvas = document.getElementById('orchCanvas');
  if (!svg || !canvas) return;
  svg.setAttribute('width', String(canvas.scrollWidth));
  svg.setAttribute('height', String(canvas.scrollHeight));

  // Arrowhead marker (rebuilt each render since we replace innerHTML wholesale).
  // A slim concave chevron (the M..L..L..L back-notch) reads sharper than a
  // flat triangle and points cleanly into the target port.
  var parts = '<defs><marker id="orchArrow" viewBox="0 0 12 12" refX="9.5" refY="6" '
    + 'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
    + '<path class="orch-edge-arrow" d="M1 1 L11 6 L1 11 L4 6 Z"></path></marker></defs>';

  // Fan separation: when several edges share one in/out port their endpoints
  // would otherwise stack on the exact same pixel (e.g. planner→loop and the
  // critic→loop back-edge both hit the loop's top port). Spread each edge's
  // endpoint along the card's top/bottom border by its index within the fan
  // so the arrowheads stay distinct.
  var inCount = {}, outCount = {}, inSeen = {}, outSeen = {};
  _orchEdges.forEach(function (e) {
    inCount[e.to] = (inCount[e.to] || 0) + 1;
    outCount[e.from] = (outCount[e.from] || 0) + 1;
  });
  var fanOffset = function (idx, count) {
    if (count <= 1) return 0;
    var step = Math.min(26, (_ORCH_CARD_W * 0.66) / (count - 1));
    return (idx - (count - 1) / 2) * step;
  };
  _orchEdges.forEach(function (e) {
    var a = _orchPortCenter(e.from, 'out');
    var b = _orchPortCenter(e.to, 'in');
    if (!a || !b) return;
    var oi = (outSeen[e.from] = (outSeen[e.from] || 0)); outSeen[e.from]++;
    var ii = (inSeen[e.to] = (inSeen[e.to] || 0)); inSeen[e.to]++;
    a = { x: a.x + fanOffset(oi, outCount[e.from]), y: a.y };
    b = { x: b.x + fanOffset(ii, inCount[e.to]), y: b.y };
    parts += '<path class="orch-edge-path" marker-end="url(#orchArrow)" d="' + _orchBezier(a, b) + '" '
          +  'onclick="_orchDeleteEdge(\'' + e.id + '\')"><title>Click to remove</title></path>';
  });

  if (_orchConnect) {
    var s = _orchPortCenter(_orchConnect.from, 'out');
    if (s) {
      parts += '<path class="orch-edge-temp" d="'
            + _orchBezier(s, { x: _orchConnect.x, y: _orchConnect.y }) + '"></path>';
    }
  }
  svg.innerHTML = parts;
}

function _orchBezier(a, b) {
  // Ports exit the source bottom (out) and enter the target top (in), so
  // the tangents are vertical.
  var dx = b.x - a.x, dy = b.y - a.y;
  if (dy >= 30) {
    // Target clearly below: a vertical S whose control points both sit at
    // the vertical midpoint. Perfectly-aligned nodes (dx≈0) collapse to a
    // straight line; offset nodes get a gentle ease. Crucially the offset
    // is dy*0.5 (never a fixed floor), so close-together ports can't make
    // the control points cross and kink the curve.
    var v = dy * 0.5;
    return 'M ' + a.x + ' ' + a.y
         + ' C ' + a.x + ' ' + (a.y + v) + ' '
         + b.x + ' ' + (b.y - v) + ' '
         + b.x + ' ' + b.y;
  }
  // Back-edge or near-level edge (e.g. a loop's critic→loop): a vertical
  // cubic would fold back over the nodes, so bow out to the side toward
  // the target instead.
  var side = dx >= 0 ? 1 : -1;
  var h = Math.max(70, Math.abs(dx) * 0.5);
  var vv = Math.max(40, Math.abs(dy) * 0.5);
  return 'M ' + a.x + ' ' + a.y
       + ' C ' + (a.x + side * h) + ' ' + (a.y + vv) + ' '
       + (b.x + side * h) + ' ' + (b.y - vv) + ' '
       + b.x + ' ' + b.y;
}

// ════════════════════════════════════════════════════════════════════
//  Inspector (right rail)
// ════════════════════════════════════════════════════════════════════

function _orchRenderInspector() {
  var el = document.getElementById('orchInspector');
  if (!el) return;
  var n = _orchSel ? _orchFind(_orchSel) : null;
  // On mobile the inspector is a slide-up sheet: open it when a node is
  // selected, close it when selection clears (e.g. tap on empty canvas).
  if (_orchIsMobile()) {
    var shell = el.closest('.orch-shell');
    if (shell) {
      if (n) { shell.classList.add('orch-m-insp'); shell.classList.remove('orch-m-pal'); }
      else { shell.classList.remove('orch-m-insp'); }
    }
  }
  if (!n) {
    el.innerHTML = '<div class="orch-insp-empty">'
      + '<div class="orch-insp-empty-icon">' + _ORCH_ICONS.gear + '</div>'
      + escapeHtml(t('orch.insp.empty'))
      + '<div class="orch-insp-stats">' + t('orch.insp.stats', { n: _orchNodes.length, m: _orchEdges.length }) + '</div>'
      + '</div>';
    return;
  }

  var _kindLabel = (n.type === 'subflow') ? t('orch.kind.group')
                 : (n.type === 'role') ? t('orch.kind.agent') : t('orch.kind.control');
  var h = '<div class="orch-sheet-head orch-m-only"><span>' + _ORCH_ICONS.gear + ' ' + escapeHtml(t('orch.kind.agent')) + '</span>'
        + '<button class="orch-ai-clear" onclick="_orchCloseMobileInspector()" title="Close">✕</button></div>';
  h += '<div class="orch-insp-head">'
        + '<span class="orch-insp-kind">' + escapeHtml(_kindLabel) + '</span>'
        + '<span class="orch-insp-type">' + escapeHtml(_orchAutoLabel(n)) + '</span>'
        + '</div>';

  h += '<label class="orch-fld"><span>' + escapeHtml(t('orch.fld.label')) + '</span>'
    +  '<input class="orch-input" value="' + escapeHtml(n.name) + '" '
    +  'placeholder="' + escapeHtml(_orchAutoLabel(n)) + '" '
    +  'oninput="_orchSetParam(\'name\', this.value)"></label>';

  if (n.type === 'subflow') {
    var _gd = (n.params && n.params.definition) || {};
    h += '<button class="orch-btn orch-btn-primary orch-btn-block" '
      +  'onclick="_orchEnterGroup(\'' + n.id + '\')">' + escapeHtml(t('orch.group.open')) + '</button>';
    h += '<div class="orch-note">' + t('orch.group.summary', { n: (_gd.nodes || []).length, m: (_gd.edges || []).length }) + '</div>';
    h += _orchSelectFld(t('orch.fld.groupFace'), 'role', n.role,
        [['general', 'General'], ['researcher', 'Researcher'], ['coder', 'Coder'],
         ['analyst', 'Analyst'], ['writer', 'Writer'], ['synthesizer', 'Synthesizer']]);
    h += _orchSelectFld(t('orch.fld.groupScope'), 'scope', (n.params.scope || 'isolated'),
        [['isolated', t('orch.scope.isolated')], ['inline', t('orch.scope.inline')]]);
    h += _orchSelectFld(t('orch.fld.emits'), 'emits', n.params.emits,
        [['', t('orch.emits.auto', { role: _orchDefaultEmits(n.role) })],
         ['assistant', t('orch.emits.assistant')],
         ['user', t('orch.emits.user')]]);
    h += '<div class="orch-note orch-note-wire">' + t('orch.note.group') + '</div>';
  } else if (n.type === 'role') {
    // Structured "what to do" fields rendered dynamically from the per-role
    // schema (/role-schema, with a built-in fallback). The first field is
    // always the core objective (role-specific label); the rest are the
    // role's structured params. The wiring note sits under the objective.
    var _schema = _orchFieldSchema(n.role);
    _schema.forEach(function (spec, i) {
      h += _orchRenderField(spec, n.params);
      if (spec.key === 'objective') {
        h += '<div class="orch-note orch-note-wire">' + t('orch.note.objective') + '</div>';
      }
    });
    h += _orchSelectFld(t('orch.fld.tier'), 'tier', n.params.tier,
        [['light', t('orch.tier.light')], ['standard', t('orch.tier.standard')], ['heavy', t('orch.tier.heavy')]]);
    h += _orchSelectFld(t('orch.fld.context'), 'isolation', n.params.isolation,
        [['fresh-context', t('orch.iso.fresh')], ['shared-context', t('orch.iso.shared')]]);
    h += '<div class="orch-note">' + t('orch.note.context') + '</div>';
    h += _orchSelectFld(t('orch.fld.emits'), 'emits', n.params.emits,
        [['', t('orch.emits.auto', { role: _orchDefaultEmits(n.role) })],
         ['assistant', t('orch.emits.assistant')],
         ['user', t('orch.emits.user')]]);
    h += '<div class="orch-note">' + t('orch.note.emits') + '</div>';
  } else if (n.kind === 'loop') {
    h += _orchNumFld(t('orch.fld.maxIter'), 'max_iterations', n.params.max_iterations);
    h += _orchSelectFld(t('orch.fld.stopWhen'), 'stop_condition', n.params.stop_condition,
        [['verdict:STOP', t('orch.stop.verdict')], ['no_new_findings', t('orch.stop.noNew')], ['max_only', t('orch.stop.maxOnly')]]);
    h += _orchSelectFld(t('orch.fld.verifier'), 'verifier', n.params.verifier,
        [['critic', t('orch.verifier.critic')], ['reviewer', t('orch.verifier.reviewer')], ['none', t('orch.verifier.none')]]);
    h += '<div class="orch-note">' + t('orch.note.loop') + '</div>';
  } else if (n.kind === 'parallel') {
    h += _orchNumFld(t('orch.fld.maxConcurrent'), 'max_concurrent', n.params.max_concurrent);
    h += _orchCheckFld(t('orch.fld.perItem'), 'per_item', n.params.per_item);
  } else if (n.kind === 'branch') {
    h += _orchSelectFld(t('orch.fld.classifier'), 'classifier', n.params.classifier,
        [['router', t('orch.classifier.router')], ['analyst', t('orch.classifier.analyst')], ['general', t('orch.classifier.general')]]);
    h += _orchNumFld(t('orch.fld.branchCount'), 'branches', n.params.branches);
  } else if (n.kind === 'artifact') {
    h += '<label class="orch-fld"><span>' + escapeHtml(t('orch.fld.filePath')) + '</span>'
      +  '<input class="orch-input" value="' + escapeHtml(n.params.path || '') + '" '
      +  'placeholder="' + escapeHtml(t('orch.fld.filePathPh')) + '" '
      +  'oninput="_orchSetParam(\'path\', this.value)"></label>';
    h += _orchSelectFld(t('orch.fld.artifactKind'), 'format', n.params.format,
        [['file', t('orch.afmt.file')], ['report', t('orch.afmt.report')], ['dataset', t('orch.afmt.dataset')],
         ['code', t('orch.afmt.code')], ['image', t('orch.afmt.image')]]);
    h += '<label class="orch-fld"><span>' + escapeHtml(t('orch.fld.description')) + '</span>'
      +  '<textarea class="orch-input orch-ta" rows="3" '
      +  'placeholder="' + escapeHtml(t('orch.fld.artifactDescPh')) + '" '
      +  'oninput="_orchSetParam(\'description\', this.value)">' + escapeHtml(n.params.description || '') + '</textarea></label>';
    h += '<div class="orch-note">' + t('orch.note.artifact') + '</div>';
  } else if (n.kind === 'human') {
    h += _orchSelectFld(t('orch.fld.humanMode'), 'mode', n.params.mode,
        [['approve', t('orch.hmode.approve')],
         ['input', t('orch.hmode.input')],
         ['notify', t('orch.hmode.notify')]]);
    h += '<label class="orch-fld"><span>' + escapeHtml(t('orch.fld.prompt')) + '</span>'
      +  '<textarea class="orch-input orch-ta" rows="3" '
      +  'placeholder="' + escapeHtml(t('orch.fld.promptPh')) + '" '
      +  'oninput="_orchSetParam(\'prompt\', this.value)">' + escapeHtml(n.params.prompt || '') + '</textarea></label>';
    if (n.params.mode === 'approve') {
      h += _orchNumFld(t('orch.fld.approveTimeout'), 'timeout_sec', n.params.timeout_sec);
    }
    h += '<div class="orch-note">' + t('orch.note.human') + '</div>';
  } else if (n.kind === 'start') {
    h += '<label class="orch-fld"><span>' + escapeHtml(t('orch.fld.startInput')) + '</span>'
      +  '<textarea class="orch-input orch-ta" rows="5" '
      +  'placeholder="' + escapeHtml(t('orch.fld.startInputPh')) + '" '
      +  'oninput="_orchSetParam(\'seed\', this.value)">' + escapeHtml((n.params && n.params.seed) || '') + '</textarea></label>';
    h += '<div class="orch-note orch-note-wire">' + t('orch.note.start') + '</div>';
  } else if (n.kind === 'stop') {
    h += '<div class="orch-note orch-note-wire">' + t('orch.note.stop') + '</div>';
  } else {
    h += '<div class="orch-note">' + escapeHtml((_ORCH_CONTROLS.filter(function (c) { return c.kind === n.kind; })[0] || {}).blurb || '') + '</div>';
  }

  // Connections summary
  var ins = _orchEdges.filter(function (e) { return e.to === n.id; });
  var outs = _orchEdges.filter(function (e) { return e.from === n.id; });
  h += '<div class="orch-conn-box"><div class="orch-conn-row">' + escapeHtml(t('orch.conn.in')) + ' <b>' + ins.length + '</b></div>'
    +  '<div class="orch-conn-row">' + escapeHtml(t('orch.conn.out')) + ' <b>' + outs.length + '</b> →</div></div>';

  h += '<button class="orch-btn orch-btn-danger orch-btn-block" onclick="_orchDeleteNode(\'' + n.id + '\')">' + escapeHtml(t('orch.btn.deleteNode')) + '</button>';
  el.innerHTML = h;
}

function _orchSelectFld(label, key, val, opts) {
  var o = opts.map(function (p) {
    return '<option value="' + escapeHtml(p[0]) + '"' + (p[0] === val ? ' selected' : '') + '>' + escapeHtml(p[1]) + '</option>';
  }).join('');
  return '<label class="orch-fld"><span>' + escapeHtml(label) + '</span>'
       + '<select class="orch-input" onchange="_orchSetParam(\'' + key + '\', this.value)">' + o + '</select></label>';
}
function _orchNumFld(label, key, val) {
  return '<label class="orch-fld"><span>' + escapeHtml(label) + '</span>'
       + '<input type="number" class="orch-input" value="' + (val != null ? val : '') + '" '
       + 'oninput="_orchSetParam(\'' + key + '\', this.value, true)"></label>';
}
function _orchCheckFld(label, key, val) {
  return '<label class="orch-fld orch-fld-check">'
       + '<input type="checkbox"' + (val ? ' checked' : '') + ' onchange="_orchSetParam(\'' + key + '\', this.checked)">'
       + '<span>' + escapeHtml(label) + '</span></label>';
}

function _orchSetParam(key, value, isNum, kind) {
  var n = _orchFind(_orchSel);
  if (!n) return;
  if (key === 'name') { n.name = value; _orchRenderNodes(); return; }
  // 'role' on a subflow node is the group's OUTWARD face (a node field, not
  // a param). Changing it can change the emits 'Auto' default → re-render.
  if (key === 'role') {
    n.role = value;
    _orchRenderNodes();
    _orchRenderInspector();
    return;
  }
  // List-kind structured fields are edited as a newline textarea but stored
  // as an array of non-empty trimmed strings (matches the backend's
  // _coerce_list). An empty list is OMITTED so it never renders a section.
  if (kind === 'list') {
    var items = String(value == null ? '' : value).split('\n')
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s; });
    if (items.length) { n.params[key] = items; } else { delete n.params[key]; }
    _orchRenderNodes();
    return;
  }
  if (isNum) value = (value === '' ? '' : Number(value));
  // 'Auto'/unset (empty) selections should OMIT the key entirely — an empty
  // string is not a valid enum value and would fail backend validation
  // (e.g. emits='' or a structured select left blank); leaving it unset lets
  // the backend derive the default / treat the field as absent.
  if (value === '') {
    delete n.params[key];
  } else {
    n.params[key] = value;
  }
  _orchRenderNodes();   // sub-line may change
  // The human gate shows/hides fields by mode → re-render the inspector.
  if (key === 'mode') _orchRenderInspector();
}

// Mirror lib/orchestration.resolve_emits' role rule so the inspector's
// "Auto" option shows the same default the backend will derive.
function _orchDefaultEmits(role) {
  return (role === 'critic' || role === 'reviewer' || role === 'virtual_user')
    ? 'user' : 'assistant';
}

// ── Per-role structured-param schema (consumed from /role-schema) ──
// The backend (lib/orchestration.ROLE_PARAM_SCHEMA) is the single source of
// truth. We fetch it once when the studio opens and render the inspector's
// "what to do" fields from it. A compact built-in fallback mirrors the
// backend so the inspector still works before the fetch lands (and in the
// headless jsdom round-trip test, which has no server). FieldSpec shape:
//   {key, kind, label (i18n key), heading?, options?:[{value,label}], placeholder?}
var _orchRoleSchema = null;        // {roles:{role:[FieldSpec]}, generic:[FieldSpec]}

var _ORCH_ROLE_SCHEMA_FALLBACK = {
  roles: {
    critic: [
      { key: 'objective', kind: 'textarea', label: 'orch.field.reviewCriteria', placeholder: 'orch.ph.reviewCriteria' },
      { key: 'must_check', kind: 'list', label: 'orch.field.mustCheck', placeholder: 'orch.ph.mustCheck' },
      { key: 'verdict_format', kind: 'select', label: 'orch.field.verdictFormat', options: [
        { value: 'stop_continue', label: 'orch.opt.stopContinue' }, { value: 'pass_fail', label: 'orch.opt.passFail' }] },
      { key: 'adversarial', kind: 'bool', label: 'orch.field.adversarial' },
    ],
    reviewer: [
      { key: 'objective', kind: 'textarea', label: 'orch.field.reviewCriteria', placeholder: 'orch.ph.reviewCriteria' },
      { key: 'must_check', kind: 'list', label: 'orch.field.mustCheck', placeholder: 'orch.ph.mustCheck' },
      { key: 'verdict_format', kind: 'select', label: 'orch.field.verdictFormat', options: [
        { value: 'stop_continue', label: 'orch.opt.stopContinue' }, { value: 'pass_fail', label: 'orch.opt.passFail' }] },
      { key: 'adversarial', kind: 'bool', label: 'orch.field.adversarial' },
    ],
    researcher: [
      { key: 'objective', kind: 'textarea', label: 'orch.field.researchQuestions', placeholder: 'orch.ph.researchQuestions' },
      { key: 'sources', kind: 'list', label: 'orch.field.sources', placeholder: 'orch.ph.sources' },
      { key: 'expected_outcome', kind: 'textarea', label: 'orch.field.expectedOutcome', placeholder: 'orch.ph.expectedOutcome' },
    ],
    worker: [
      { key: 'objective', kind: 'textarea', label: 'orch.field.taskWorker', placeholder: 'orch.ph.taskWorker' },
      { key: 'must_do', kind: 'list', label: 'orch.field.mustDo', placeholder: 'orch.ph.mustDo' },
      { key: 'must_not_do', kind: 'list', label: 'orch.field.mustNotDo', placeholder: 'orch.ph.mustNotDo' },
      { key: 'expected_outcome', kind: 'textarea', label: 'orch.field.expectedOutcome', placeholder: 'orch.ph.expectedOutcome' },
    ],
    planner: [
      { key: 'objective', kind: 'textarea', label: 'orch.field.planningBrief', placeholder: 'orch.ph.planningBrief' },
      { key: 'deliverables', kind: 'list', label: 'orch.field.deliverables', placeholder: 'orch.ph.deliverables' },
    ],
    virtual_user: [
      { key: 'objective', kind: 'textarea', label: 'orch.field.persona', placeholder: 'orch.ph.persona' },
      { key: 'done_signal', kind: 'text', label: 'orch.field.doneSignal', placeholder: 'orch.ph.doneSignal' },
    ],
  },
  generic: [
    { key: 'objective', kind: 'textarea', label: 'orch.field.task', placeholder: 'orch.ph.task' },
    { key: 'expected_outcome', kind: 'textarea', label: 'orch.field.expectedOutcome', placeholder: 'orch.ph.expectedOutcome' },
  ],
};

// Fetch the authoritative schema once; re-render the inspector if a node is
// selected so dynamically-added fields appear without a reselect.
async function _orchFetchRoleSchema() {
  if (_orchRoleSchema || typeof Api === 'undefined' || !Api.orchestrations
      || !Api.orchestrations.roleSchema) return;
  try {
    var res = await Api.orchestrations.roleSchema();
    if (res && res.ok && res.roles) {
      _orchRoleSchema = { roles: res.roles, generic: res.generic || [] };
      if (_orchSel) _orchRenderInspector();
    }
  } catch (e) {
    if (typeof console !== 'undefined') console.warn('role-schema fetch failed', e);
  }
}

// Resolve a role's FieldSpec list: fetched schema → built-in fallback →
// generic. Never throws; always returns a non-empty list.
function _orchFieldSchema(role) {
  var src = _orchRoleSchema || _ORCH_ROLE_SCHEMA_FALLBACK;
  var roles = src.roles || {};
  if (roles[role]) return roles[role];
  return src.generic || _ORCH_ROLE_SCHEMA_FALLBACK.generic;
}

// Render one structured FieldSpec into inspector HTML, reading the current
// value off the node's params. Labels/placeholders/option labels are i18n
// KEYS resolved here via t().
function _orchRenderField(spec, params) {
  var label = t(spec.label);
  var val = params[spec.key];
  if (spec.kind === 'list') {
    var lines = Array.isArray(val) ? val.join('\n') : (typeof val === 'string' ? val : '');
    var ph = spec.placeholder ? t(spec.placeholder) : '';
    return '<label class="orch-fld"><span>' + escapeHtml(label) + '</span>'
      + '<textarea class="orch-input orch-ta orch-ta-list" rows="3" '
      + 'placeholder="' + escapeHtml(ph) + '" '
      + 'oninput="_orchSetParam(\'' + spec.key + '\', this.value, false, \'list\')">'
      + escapeHtml(lines) + '</textarea></label>';
  }
  if (spec.kind === 'select') {
    var opts = (spec.options || []).map(function (o) {
      return [o.value, t(o.label)];
    });
    // Prepend an unset option so a select-kind field can be left blank.
    opts.unshift(['', t('orch.opt.unset')]);
    return _orchSelectFld(label, spec.key, (val == null ? '' : val), opts);
  }
  if (spec.kind === 'bool') {
    return _orchCheckFld(label, spec.key, !!val);
  }
  if (spec.kind === 'int') {
    return _orchNumFld(label, spec.key, val);
  }
  // text / textarea
  var rows = (spec.kind === 'textarea') ? 4 : 1;
  var ctrl = (spec.kind === 'textarea')
    ? ('<textarea class="orch-input orch-ta" rows="' + rows + '" '
       + 'placeholder="' + escapeHtml(spec.placeholder ? t(spec.placeholder) : '') + '" '
       + 'oninput="_orchSetParam(\'' + spec.key + '\', this.value)">'
       + escapeHtml(typeof val === 'string' ? val : '') + '</textarea>')
    : ('<input class="orch-input" value="' + escapeHtml(typeof val === 'string' ? val : '') + '" '
       + 'placeholder="' + escapeHtml(spec.placeholder ? t(spec.placeholder) : '') + '" '
       + 'oninput="_orchSetParam(\'' + spec.key + '\', this.value)">');
  return '<label class="orch-fld"><span>' + escapeHtml(label) + '</span>' + ctrl + '</label>';
}

function _orchOnRename(v) { _orchName = v || 'Untitled Flow'; }

// ════════════════════════════════════════════════════════════════════
//  AI Composer — discuss requirements, backend mutates the graph
//
//  All reasoning is server-side (POST /api/v1/orchestrations/compose).
//  The frontend only sends {requirement, current graph, chat history}
//  and renders the returned definition + reply.
// ════════════════════════════════════════════════════════════════════

var _orchAiHistory = [];      // [{role:'user'|'assistant', content}]
var _orchAiBusy = false;

function _orchToggleAi() {
  var panel = document.getElementById('orchAi');
  var btn = document.getElementById('orchAiToggle');
  if (!panel) return;
  var open = panel.classList.toggle('is-open');
  if (btn) btn.classList.toggle('is-active', open);
  if (open) {
    if (!_orchAiHistory.length) _orchRenderAiLog();
    var t = document.getElementById('orchAiText');
    if (t) setTimeout(function () { t.focus(); }, 50);
  }
}

function _orchAiClear() {
  _orchAiHistory = [];
  _orchRenderAiLog();
}

function _orchAiKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _orchAiSend(); }
}

function _orchRenderAiLog() {
  var log = document.getElementById('orchAiLog');
  if (!log) return;
  if (!_orchAiHistory.length) {
    log.innerHTML = '<div class="orch-ai-empty">'
      + '<div class="orch-ai-empty-icon">' + _ORCH_ICONS.wand + '</div>'
      + '<div class="orch-ai-empty-title">Compose by conversation</div>'
      + '<div class="orch-ai-empty-text">Try: <i>"Build a research flow that fans out to 3 '
      + 'researchers then synthesizes"</i> or <i>"add a critic loop after the worker".</i></div>'
      + '</div>';
    return;
  }
  log.innerHTML = _orchAiHistory.map(function (m) {
    return '<div class="orch-ai-msg orch-ai-' + (m.role === 'user' ? 'user' : 'bot') + '">'
      + escapeHtml(m.content) + '</div>';
  }).join('') + (_orchAiBusy
    ? '<div class="orch-ai-msg orch-ai-bot orch-ai-typing">composing… <span class="orch-dot"></span></div>'
    : '');
  log.scrollTop = log.scrollHeight;
}

async function _orchAiSend() {
  if (_orchAiBusy) return;
  var t = document.getElementById('orchAiText');
  if (!t) return;
  var text = (t.value || '').trim();
  if (!text) return;
  if (typeof Api === 'undefined' || !Api.orchestrations || !Api.orchestrations.compose) {
    _orchToast('API client unavailable', true);
    return;
  }
  t.value = '';
  _orchAiHistory.push({ role: 'user', content: text });
  _orchAiBusy = true;
  _orchRenderAiLog();
  _orchAiSetEnabled(false);

  // Send the current graph so the model edits in place. Collapse any open
  // group frames first so the composer sees the whole root flow.
  _orchFlushToRoot();
  var current = _orchNodes.length ? _orchToDefinition() : null;
  var result;
  try {
    result = await Api.orchestrations.compose(text, current, _orchAiHistory.slice(0, -1));
  } catch (e) {
    result = null;
  }
  _orchAiBusy = false;
  _orchAiSetEnabled(true);

  if (!result) {
    _orchAiHistory.push({ role: 'assistant', content: 'The composer request failed. Try again.' });
    _orchRenderAiLog();
    return;
  }
  var reply = result.reply || (result.ok ? 'Updated the graph.' : 'I could not build a valid graph.');
  // Surface validation issues inline so the user understands a rejected draft.
  if (!result.ok && result.validation && result.validation.errors && result.validation.errors.length) {
    reply += '\n' + result.validation.errors.slice(0, 3).join('; ');
  }
  _orchAiHistory.push({ role: 'assistant', content: reply });
  _orchRenderAiLog();

  if (result.ok && result.definition) {
    // Keep the current backend id (this is an edit of the open flow).
    _orchApplyDefinition(result.definition, _orchCurrentId);
    var warns = (result.validation && result.validation.warnings) || [];
    _orchToast('Graph updated' + (warns.length ? ' (' + warns.length + ' warning' + (warns.length > 1 ? 's' : '') + ')' : ''));
  }
}

function _orchAiSetEnabled(on) {
  var send = document.getElementById('orchAiSend');
  var t = document.getElementById('orchAiText');
  if (send) send.disabled = !on;
  if (t) t.disabled = !on;
}

// ════════════════════════════════════════════════════════════════════
//  Run drawer — execute the flow on the backend, stream events
//
//  The frontend only triggers /run and polls /run/poll/<id>; ALL
//  orchestration logic runs server-side in lib/orchestration_engine.py.
// ════════════════════════════════════════════════════════════════════

var _orchRunTaskId = null;
var _orchRunPolling = false;

function _orchStartSeed() {
  var st = _orchNodes.filter(function (n) { return n.kind === 'start'; })[0];
  return (st && st.params && st.params.seed) ? String(st.params.seed) : '';
}

function _orchOpenRun() {
  var d = document.getElementById('orchRunDrawer');
  if (d) d.classList.add('is-open');
  // Prefill the run input from the Start node's seed so the entry point
  // the user configured on the canvas is what they run with by default.
  var inp = document.getElementById('orchRunInput');
  if (inp && !inp.value) inp.value = _orchStartSeed();
}
function _orchCloseRun() {
  var d = document.getElementById('orchRunDrawer');
  if (d) d.classList.remove('is-open');
}

function _orchRunLog(html, cls) {
  var log = document.getElementById('orchRunLog');
  if (!log) return;
  var row = document.createElement('div');
  row.className = 'orch-run-line' + (cls ? ' ' + cls : '');
  row.innerHTML = html;
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
}

// Render an interactive human-gate prompt inside the run log. The flow
// thread is blocked server-side until the user responds; the resolve
// endpoints reuse the chat approval / ask-human primitives.
function _orchRenderHumanGate(ev) {
  var log = document.getElementById('orchRunLog');
  if (!log) return;
  var rid = ev.request_id || '';
  var row = document.createElement('div');
  row.className = 'orch-run-line orch-human-gate';
  row.id = 'orchHumanGate-' + rid;
  var head = _ORCH_ICONS.person + ' <b>' + escapeHtml(ev.name || 'Human') + '</b> — '
    + escapeHtml(ev.prompt || (ev.mode === 'approve' ? 'Approve to continue?' : 'Your input?'));
  var ridArg = "'" + rid.replace(/'/g, "\\'") + "'";
  if (ev.mode === 'approve') {
    row.innerHTML = head
      + '<div class="orch-human-actions">'
      + '<button class="orch-btn orch-btn-run" onclick="_orchHumanApprove(' + ridArg + ', true)">Approve</button>'
      + '<button class="orch-btn orch-btn-danger" onclick="_orchHumanApprove(' + ridArg + ', false)">Reject</button>'
      + '</div>';
  } else {
    row.innerHTML = head
      + '<div class="orch-human-actions">'
      + '<input class="orch-input" id="orchHumanInput-' + escapeHtml(rid) + '" placeholder="Type your answer…">'
      + '<button class="orch-btn orch-btn-primary" onclick="_orchHumanInput(' + ridArg + ')">Send</button>'
      + '</div>';
  }
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
}

function _orchClearHumanGate(rid) {
  var row = document.getElementById('orchHumanGate-' + (rid || ''));
  if (row) row.remove();
}

async function _orchHumanApprove(rid, approved) {
  _orchClearHumanGate(rid);
  await Api.orchestrations.humanApprove(rid, approved);
}

async function _orchHumanInput(rid) {
  var inp = document.getElementById('orchHumanInput-' + rid);
  var val = inp ? inp.value : '';
  if (!val.trim()) { _orchToast('Enter a response', true); return; }
  _orchClearHumanGate(rid);
  await Api.orchestrations.humanInput(rid, val);
}

async function _orchPlan() {
  _orchFlushToRoot();
  if (!_orchNodes.length) { _orchToast('Nothing to plan', true); return; }
  var def = _orchToDefinition();
  var res = await Api.orchestrations.plan(def);
  var log = document.getElementById('orchRunLog');
  if (log) log.innerHTML = '';
  if (!res || !res.ok) {
    _orchRunLog(_ORCH_ICONS.warn + ' ' + escapeHtml((res && res.error) || 'plan failed'), 'is-err');
    return;
  }
  _orchRunLog('<b>Execution plan (' + res.steps.length + ' steps):</b>');
  res.steps.forEach(function (s, i) {
    var label = s.role ? (_ORCH_ICONS.bot + ' ' + s.role) : ('⬡ ' + (s.kind || s.action));
    _orchRunLog((i + 1) + '. ' + escapeHtml(label) + ' <span class="orch-run-dim">(' + escapeHtml(s.action) + ')</span>');
  });
}

async function _orchRun() {
  if (_orchRunPolling) return;
  _orchFlushToRoot();
  if (!_orchNodes.length) { _orchToast('Nothing to run', true); return; }
  var def = _orchToDefinition();
  var input = (document.getElementById('orchRunInput') || {}).value || '';
  if (!input.trim()) input = _orchStartSeed();
  var log = document.getElementById('orchRunLog');
  if (log) log.innerHTML = '';
  _orchRunLog(_ORCH_ICONS.rocket + ' Starting run…');

  var res = await Api.orchestrations.run(def, input);
  if (!res || !res.ok || !res.task_id) {
    _orchRunLog(_ORCH_ICONS.warn + ' ' + escapeHtml((res && (res.error || (res.errors || []).join('; '))) || 'run failed'), 'is-err');
    return;
  }
  _orchRunTaskId = res.task_id;
  _orchRunSetBusy(true);
  _orchRunPoll(0);
}

// Launch a DURABLE run (Task Mode) instead of the ephemeral in-drawer run.
// Unlike _orchRun (TaskRuntime-only, lives in this drawer), this persists a
// run instance and hands off to the Task Mode viewer where it can be
// reopened after a reload. Passes _orchCurrentId so the run links to its
// saved template; an unsaved flow still runs (inline definition snapshot).
async function _orchRunAsTask() {
  _orchFlushToRoot();
  if (!_orchNodes.length) { _orchToast('Nothing to run', true); return; }
  var def = _orchToDefinition();
  var input = (document.getElementById('orchRunInput') || {}).value || '';
  if (!input.trim()) input = _orchStartSeed();

  var btn = document.getElementById('orchRunTaskBtn');
  if (btn) btn.disabled = true;
  try {
    var resp = await Api.orchestrations.taskCreate(def, input, _orchCurrentId || '');
    var data = (resp && resp.json) ? await resp.json().catch(function () { return {}; }) : {};
    if (!data || !data.ok || !data.run_id) {
      _orchToast((data && (data.error || (data.errors || []).join('; '))) || 'Could not start task', true);
      return;
    }
    _orchToast('Task started — opening Task Mode');
    if (typeof openTaskMode === 'function') {
      closeOrchestration();
      openTaskMode();
      if (typeof _tmOpenRun === 'function') _tmOpenRun(data.run_id);
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _orchRunSetBusy(on) {
  _orchRunPolling = on;
  var run = document.getElementById('orchRunBtn');
  var abort = document.getElementById('orchRunAbort');
  if (run) run.disabled = on;
  if (abort) abort.style.display = on ? '' : 'none';
}

async function _orchRunPoll(cursor) {
  if (!_orchRunTaskId) return;
  var res = await Api.orchestrations.runPoll(_orchRunTaskId, cursor);
  if (!res || !res.ok) {
    _orchRunLog(_ORCH_ICONS.warn + ' poll failed', 'is-err');
    _orchRunSetBusy(false);
    return;
  }
  (res.events || []).forEach(_orchRenderRunEvent);
  if (res.done) {
    _orchRunSetBusy(false);
    _orchRunTaskId = null;
    return;
  }
  setTimeout(function () { _orchRunPoll(res.next_cursor); }, 700);
}

function _orchRenderRunEvent(ev) {
  switch (ev.type) {
    case 'flow_start':
      _orchRunLog(_ORCH_ICONS.flag + ' <b>' + escapeHtml(ev.name || 'flow') + '</b> — ' + (ev.nodes || 0) + ' nodes'); break;
    case 'step_start':
      _orchRunLog(_ORCH_ICONS.bot + ' <b>' + escapeHtml(ev.name || ev.role) + '</b> running…', 'is-active'); break;
    case 'step_complete':
      _orchRunLog(_ORCH_ICONS.check + ' ' + escapeHtml(ev.role) + ' <span class="orch-run-dim">' + escapeHtml((ev.preview || '').slice(0, 120)) + '</span>'); break;
    case 'loop_iteration':
      _orchRunLog(_ORCH_ICONS.loop + ' loop iteration ' + ev.iteration + '/' + ev.max); break;
    case 'zero_deliverable_guard':
      _orchRunLog(_ORCH_ICONS.warn + ' zero-deliverable guard — injecting "execute, stop analyzing" directive', 'is-err'); break;
    case 'replan':
      _orchRunLog(_ORCH_ICONS.compass + ' re-plan #' + ev.replan + ' — ' + escapeHtml((ev.defect || 'structural defect').slice(0, 100))); break;
    case 'stuck_detected':
      _orchRunLog(_ORCH_ICONS.loop + ' stuck — verifier feedback is repeating; breaking the loop', 'is-err'); break;
    case 'parallel_start':
      _orchRunLog(_ORCH_ICONS.fanout + ' fan-out → ' + ev.branches + ' branches'); break;
    case 'branch_pick':
      _orchRunLog('↪ route → ' + escapeHtml(ev.chosen || '(none)')); break;
    case 'artifact_declared':
      _orchRunLog(_ORCH_ICONS.package + ' deliverable: <b>' + escapeHtml(ev.path || ev.name || '(unnamed)') + '</b>'
        + (ev.description ? ' <span class="orch-run-dim">' + escapeHtml(ev.description.slice(0, 120)) + '</span>' : '')); break;
    case 'human_notify':
      _orchRunLog(_ORCH_ICONS.person + ' <b>' + escapeHtml(ev.name || 'Human') + '</b> '
        + '<span class="orch-run-dim">' + escapeHtml((ev.prompt || '').slice(0, 200)) + '</span>'); break;
    case 'human_request':
      _orchRenderHumanGate(ev); break;
    case 'human_resolved':
      _orchClearHumanGate(ev.request_id);
      _orchRunLog(_ORCH_ICONS.person + ' ' + (ev.mode === 'approve'
        ? (ev.approved ? _ORCH_ICONS.check + ' approved' : _ORCH_ICONS.reject + ' rejected')
        : _ORCH_ICONS.check + ' answered') + ' <span class="orch-run-dim">' + escapeHtml(ev.request_id || '') + '</span>'); break;
    case 'flow_complete':
      _orchRunLog(_ORCH_ICONS.flag + ' <b>' + escapeHtml(ev.status) + '</b> — ' + (ev.agents_run || 0) + ' agents, ' + (ev.elapsed || 0) + 's',
                  ev.status === 'completed' ? 'is-done' : 'is-err'); break;
    case 'done':
      if (ev.result && ev.result.final) {
        _orchRunLog('<b>Result:</b>'); _orchRunLog('<pre class="orch-run-final">' + escapeHtml(ev.result.final.slice(0, 4000)) + '</pre>');
      }
      break;
    case 'error':
      _orchRunLog(_ORCH_ICONS.warn + ' ' + escapeHtml((ev.error && ev.error.detail) || 'error'), 'is-err'); break;
  }
}

async function _orchRunAbort() {
  if (!_orchRunTaskId) return;
  await Api.orchestrations.runAbort(_orchRunTaskId);
  _orchRunLog(_ORCH_ICONS.stop + ' abort requested…');
}

// ════════════════════════════════════════════════════════════════════
//  Templates
// ════════════════════════════════════════════════════════════════════

function _orchToggleTplMenu(forceClose) {
  var m = document.getElementById('orchTplMenu');
  if (!m) return;
  m.style.display = (forceClose || m.style.display !== 'none') ? 'none' : 'block';
}

// Load a server-authored canonical flow (the backend is the source of
// truth for these shapes — see build_endpoint_definition).
async function _orchLoadBuiltin(name) {
  if (typeof Api === 'undefined' || !Api.orchestrations || !Api.orchestrations.builtin) {
    _orchToast('API client unavailable', true);
    return;
  }
  var res = await Api.orchestrations.builtin(name);
  if (!res || !res.ok || !res.definition) {
    _orchToast('Could not load built-in "' + name + '"', true);
    return;
  }
  _orchApplyDefinition(res.definition, null);
  _orchToast('Loaded canonical ' + name + ' flow');
}

function _orchLoadTemplate(which) {
  _orchStack = [];
  _orchNodes = []; _orchEdges = []; _orchSel = null; _orchSeq = 0; _orchCurrentId = null;
  // Templates carry the FINAL coordinates the backend layout engine
  // (lib.orchestration.layout_definition) produces for each topology —
  // captured once, baked in here. They render correctly on the first
  // paint with NO layout round-trip, so opening a template never flashes.
  // If you change a template's topology, re-run layout_definition on it
  // and paste the resulting x/y back here so the two stay in sync.
  var mk = function (payload, x, y, params, name) {
    var node = {
      id: _orchNextId(payload.ptype === 'role' ? payload.role : payload.kind),
      type: payload.ptype, role: payload.role || '', kind: payload.kind || '',
      x: x, y: y, name: name || '', params: Object.assign(_orchDefaultParams(payload), params || {}),
    };
    _orchNodes.push(node); return node.id;
  };
  var link = function (a, b) { _orchEdges.push({ id: _orchNextId('e'), from: a, to: b }); };

  if (which === 'blank') {
    _orchName = 'Untitled Flow';
  } else if (which === 'endpoint') {
    _orchName = 'Endpoint Loop';
    var s = mk({ ptype: 'control', kind: 'start' }, 155, 30);
    var p = mk({ ptype: 'role', role: 'planner' }, 155, 180, { objective: 'Rewrite the request into a structured brief + checklist.' });
    var lp = mk({ ptype: 'control', kind: 'loop' }, 155, 330, { max_iterations: 10, stop_condition: 'verdict:STOP', verifier: 'critic' });
    var w = mk({ ptype: 'role', role: 'worker' }, 40, 480, { isolation: 'shared-context', objective: 'Execute the plan. First tool call must be state-changing.' });
    var c = mk({ ptype: 'role', role: 'critic' }, 155, 630, { objective: 'Review work vs the checklist. Emit STOP / CONTINUE.' });
    var st = mk({ ptype: 'control', kind: 'stop' }, 270, 480);
    link(s, p); link(p, lp); link(lp, w); link(w, c); link(c, lp); link(lp, st);
  } else if (which === 'fanout') {
    _orchName = 'Fan-out → Synthesize';
    var s2 = mk({ ptype: 'control', kind: 'start' }, 270, 30);
    var fo = mk({ ptype: 'control', kind: 'parallel' }, 270, 180, { max_concurrent: 8 });
    var r1 = mk({ ptype: 'role', role: 'researcher' }, 40, 330);
    var r2 = mk({ ptype: 'role', role: 'researcher' }, 270, 330);
    var r3 = mk({ ptype: 'role', role: 'researcher' }, 500, 330);
    var jn = mk({ ptype: 'control', kind: 'barrier' }, 270, 480);
    var sy = mk({ ptype: 'role', role: 'synthesizer' }, 270, 630, { objective: 'Merge all findings into one cited report.' });
    var st2 = mk({ ptype: 'control', kind: 'stop' }, 270, 780);
    link(s2, fo); link(fo, r1); link(fo, r2); link(fo, r3);
    link(r1, jn); link(r2, jn); link(r3, jn); link(jn, sy); link(sy, st2);
  } else if (which === 'autopilot') {
    _orchName = 'Autopilot';
    var sa = mk({ ptype: 'control', kind: 'start' }, 155, 30);
    var la = mk({ ptype: 'control', kind: 'loop' }, 155, 180, { max_iterations: 12, stop_condition: 'verdict:STOP', verifier: 'virtual_user' });
    var wa = mk({ ptype: 'role', role: 'worker' }, 40, 330, { isolation: 'shared-context', emits: 'assistant', objective: 'Continue the task. Make concrete progress every turn.' });
    var va = mk({ ptype: 'role', role: 'virtual_user' }, 155, 480, { emits: 'user', objective: 'Stand in for the human. Reply briefly to keep going; emit [VU: TASK_DONE] when finished.' });
    var sta = mk({ ptype: 'control', kind: 'stop' }, 270, 330);
    link(sa, la); link(la, wa); link(wa, va); link(va, la); link(la, sta);
  } else if (which === 'adversarial') {
    _orchName = 'Adversarial Verify';
    var s3 = mk({ ptype: 'control', kind: 'start' }, 40, 30);
    var pr = mk({ ptype: 'role', role: 'coder' }, 40, 180, { objective: 'Produce the change / finding.' });
    var rv = mk({ ptype: 'role', role: 'reviewer' }, 40, 330, { objective: 'Try to refute the finding against a rubric.' });
    var sy3 = mk({ ptype: 'role', role: 'synthesizer' }, 40, 480, { objective: 'Keep only findings the reviewer could not knock down.' });
    var st3 = mk({ ptype: 'control', kind: 'stop' }, 40, 630);
    link(s3, pr); link(pr, rv); link(rv, sy3); link(sy3, st3);
  }
  _orchRender();
}

// Tidy: ask the backend to recompute node positions (BFS layering +
// barycenter crossing-minimization) and apply them to the canvas. The
// backend owns layout (see lib.orchestration.layout_definition) so the
// studio stays a thin renderer.
async function _orchTidy(opts) {
  var silent = opts && opts.silent;
  if (!_orchNodes.length) return;
  if (typeof Api === 'undefined' || !Api.orchestrations || !Api.orchestrations.layout) {
    if (!silent) _orchToast('API client unavailable', true);
    return;
  }
  var res = await Api.orchestrations.layout(_orchToDefinition());
  if (!res || !res.ok || !res.definition) {
    if (!silent) _orchToast('Tidy failed', true);
    return;
  }
  var posById = {};
  (res.definition.nodes || []).forEach(function (n) {
    if (n.pos) posById[n.id] = n.pos;
  });
  _orchNodes.forEach(function (n) {
    var p = posById[n.id];
    if (p) { n.x = p.x; n.y = p.y; }
  });
  _orchRender();
  if (!silent) _orchToast('Tidied layout');
}

// ════════════════════════════════════════════════════════════════════
//  Definition export / persistence
// ════════════════════════════════════════════════════════════════════

// Convert the canvas into the declarative definition a backend engine
// would later interpret. This is the contract seam — keep it stable.
function _orchToDefinition() {
  return {
    schema: 'tofu.orchestration/v1',
    name: _orchName,
    nodes: _orchNodes.map(function (n) {
      return {
        id: n.id,
        type: n.type,
        role: n.role || undefined,
        kind: n.kind || undefined,
        name: n.name || undefined,
        pos: { x: Math.round(n.x), y: Math.round(n.y) },
        params: n.params,
      };
    }),
    edges: _orchEdges.map(function (e) { return { from: e.from, to: e.to }; }),
  };
}

function _orchExport() {
  _orchFlushToRoot();
  var def = _orchToDefinition();
  var blob = new Blob([JSON.stringify(def, null, 2)], { type: 'application/json' });
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a');
  a.href = url;
  a.download = (_orchName || 'flow').replace(/[^a-z0-9_-]+/gi, '_').toLowerCase() + '.orch.json';
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  _orchToast('Exported ' + a.download);
}

// Persist to the backend store (/api/v1/orchestrations). If this flow was
// loaded from / previously saved to the store we hold its id in
// _orchCurrentId and PUT; otherwise we POST and remember the new id.
async function _orchSave() {
  _orchFlushToRoot();
  var def = _orchToDefinition();
  if (typeof Api === 'undefined' || !Api.orchestrations) {
    _orchToast('API client unavailable', true);
    return;
  }
  try {
    var resp = _orchCurrentId
      ? await Api.orchestrations.update(_orchCurrentId, def)
      : await Api.orchestrations.create(def);
    var data = (resp && resp.json) ? await resp.json().catch(function () { return {}; }) : {};
    if (resp && !resp.ok) {
      var errs = (data.errors && data.errors.length) ? ': ' + data.errors.join('; ') : '';
      _orchToast('Save rejected' + errs, true);
      return;
    }
    if (data.id) _orchCurrentId = data.id;
    var warn = (data.warnings && data.warnings.length)
      ? ' (' + data.warnings.length + ' warning' + (data.warnings.length > 1 ? 's' : '') + ')' : '';
    _orchToast('Saved "' + _orchName + '"' + warn);
  } catch (e) {
    _orchToast('Save failed: ' + e.message, true);
  }
}

// ── Load existing flows from the backend store ──
async function _orchOpenLoadMenu() {
  var menu = document.getElementById('orchLoadMenu');
  if (!menu) return;
  if (menu.style.display !== 'none') { menu.style.display = 'none'; return; }
  menu.style.display = 'block';
  menu.innerHTML = '<div class="orch-load-empty">Loading…</div>';
  var list = [];
  try { list = (Api.orchestrations ? await Api.orchestrations.list() : []) || []; }
  catch (e) { menu.innerHTML = '<div class="orch-load-empty">Failed: ' + escapeHtml(e.message) + '</div>'; return; }
  if (!list.length) { menu.innerHTML = '<div class="orch-load-empty">No saved flows yet.</div>'; return; }
  list.sort(function (a, b) { return (b.updatedAt || 0) - (a.updatedAt || 0); });
  menu.innerHTML = list.map(function (e) {
    var n = (e.definition && (e.definition.nodes || []).length) || 0;
    return '<div class="orch-load-row">'
      + '<button class="orch-load-pick" onclick="_orchLoadFromStore(\'' + escapeHtml(e.id) + '\')">'
      +   '<span class="orch-load-name">' + escapeHtml(e.name || 'Untitled') + '</span>'
      +   '<span class="orch-load-meta">' + n + ' nodes</span>'
      + '</button>'
      + '<button class="orch-load-del" title="Delete" onclick="_orchDeleteFromStore(\'' + escapeHtml(e.id) + '\',event)">✕</button>'
      + '</div>';
  }).join('');
}

async function _orchLoadFromStore(id) {
  try {
    var entry = await Api.orchestrations.get(id);
    if (!entry || !entry.definition) { _orchToast('Not found', true); return; }
    _orchApplyDefinition(entry.definition, id);
    var m = document.getElementById('orchLoadMenu'); if (m) m.style.display = 'none';
    _orchToast('Loaded "' + (entry.name || 'flow') + '"');
  } catch (e) { _orchToast('Load failed: ' + e.message, true); }
}

async function _orchDeleteFromStore(id, evt) {
  if (evt) evt.stopPropagation();
  if (!await showConfirm('Delete this saved flow?', { danger: true })) return;
  var ok = await Api.orchestrations.remove(id);
  if (ok) {
    if (_orchCurrentId === id) _orchCurrentId = null;
    _orchToast('Deleted');
    _orchOpenLoadMenu(); _orchOpenLoadMenu();   // refresh (toggle off→on)
  } else { _orchToast('Delete failed', true); }
}

// Rehydrate canvas state from a stored definition object.
function _orchApplyDefinition(def, id) {
  _orchStack = [];
  _orchCurrentId = id || null;
  _orchName = def.name || 'Untitled Flow';
  _orchSeq = 0;
  _orchSel = null;
  _orchNodes = (def.nodes || []).map(function (n) {
    return {
      id: n.id, type: n.type, role: n.role || '', kind: n.kind || '',
      x: (n.pos && n.pos.x) || 20, y: (n.pos && n.pos.y) || 20,
      name: n.name || '', params: n.params || {},
    };
  });
  // Keep the id counter ahead of any numeric suffixes already in use.
  _orchNodes.forEach(function (n) {
    var m = /(\d+)$/.exec(n.id || '');
    if (m) _orchSeq = Math.max(_orchSeq, parseInt(m[1], 10));
  });
  _orchEdges = (def.edges || []).map(function (e) {
    return { id: _orchNextId('e'), from: e.from, to: e.to };
  });
  _orchRender();
  // Flows without real coordinates (built-ins, freshly-composed graphs,
  // legacy saves) would otherwise stack at the (20,20) fallback. Snap them
  // into clean lanes via the same backend layout path as the Tidy button.
  var needsLayout = (def.nodes || []).some(function (n) {
    return !n.pos || typeof n.pos.x !== 'number' || typeof n.pos.y !== 'number';
  });
  if (needsLayout && _orchNodes.length) _orchTidy({ silent: true });
}

function _orchToast(text, isErr) {
  var el = document.createElement('div');
  el.className = 'orch-toast' + (isErr ? ' is-err' : '');
  el.textContent = text;
  document.body.appendChild(el);
  setTimeout(function () { el.style.opacity = '0'; setTimeout(function () { el.remove(); }, 300); }, 2600);
}

// ════════════════════════════════════════════════════════════════════
//  Scoped styles (injected once)
// ════════════════════════════════════════════════════════════════════

function _orchInjectStyles() {
  if (document.getElementById('orch-studio-styles')) return;
  var css = `
.orch-overlay{position:fixed;inset:0;z-index:9600;background:rgba(0,0,0,.6);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;animation:orchFade .15s ease}
@keyframes orchFade{from{opacity:0}to{opacity:1}}
.orch-shell{--orch-r-sm:7px;--orch-r-md:11px;--orch-r-lg:14px;--orch-r-xl:16px;--orch-elev-card:0 2px 8px rgba(0,0,0,.26);--orch-elev-pop:0 12px 32px rgba(0,0,0,.46);--orch-elev-lift:0 16px 38px rgba(0,0,0,.5);--orch-ok:#10b981;--orch-ok-hover:#0ea271;--orch-rail:1px solid var(--border);width:96vw;height:92vh;max-width:1500px;background:var(--bg-secondary);border:1px solid var(--border-light);border-radius:var(--orch-r-xl);box-shadow:0 24px 64px rgba(0,0,0,.55);display:flex;flex-direction:column;overflow:hidden}
.orch-top{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:11px 16px;border-bottom:var(--orch-rail);background:linear-gradient(180deg,var(--bg-tertiary),var(--bg-secondary))}
.orch-top-left{display:flex;align-items:center;gap:10px;min-width:0}
.orch-logo img{display:block;border-radius:var(--orch-r-sm)}
.orch-name-input{background:transparent;border:1px solid transparent;border-radius:var(--orch-r-sm);color:var(--text-primary);font-size:16px;font-weight:700;font-family:inherit;padding:5px 10px;width:280px;transition:border-color var(--transition),background var(--transition)}
.orch-name-input:hover{background:var(--bg-hover)}
.orch-name-input:focus{outline:none;border-color:var(--accent);background:var(--bg-primary);box-shadow:0 0 0 3px var(--accent-subtle)}
.orch-top-actions{display:flex;align-items:center;gap:7px;flex-wrap:wrap;justify-content:flex-end}
.orch-top-sep{width:1px;align-self:stretch;margin:5px 3px;background:var(--border);flex-shrink:0}
.orch-btn{font-family:inherit;font-size:12.5px;font-weight:600;border-radius:var(--orch-r-sm);padding:8px 12px;border:1px solid var(--border);background:var(--bg-tertiary);color:var(--text-secondary);cursor:pointer;transition:background var(--transition),color var(--transition),border-color var(--transition),transform .08s ease,box-shadow var(--transition);white-space:nowrap}
.orch-btn:hover{background:var(--bg-hover);color:var(--text-primary);border-color:var(--border-light)}
.orch-btn:active{transform:translateY(1px)}
.orch-btn-primary{background:var(--accent);border-color:var(--accent);color:#fff}
.orch-btn-primary:hover{background:var(--accent-hover);border-color:var(--accent-hover);color:#fff;box-shadow:0 3px 12px var(--accent-subtle)}
.orch-btn-ghost{background:transparent}
.orch-btn-close{padding:8px 11px;font-size:14px}
.orch-btn-danger{background:var(--error-bg);border-color:var(--error-border);color:var(--error-text)}
.orch-btn-danger:hover{background:var(--error-bg);filter:brightness(1.2)}
.orch-btn-block{display:block;width:100%;margin-top:14px}
.orch-tpl-wrap{position:relative}
.orch-tpl-menu{position:absolute;top:42px;left:0;z-index:20;background:var(--bg-secondary);border:1px solid var(--border-light);border-radius:var(--orch-r-md);box-shadow:var(--orch-elev-pop);padding:6px;min-width:280px;animation:orchPop .14s ease}
.orch-tpl-menu button{display:block;width:100%;text-align:left;background:transparent;border:none;color:var(--text-primary);font-family:inherit;font-size:12.5px;padding:9px 11px;border-radius:var(--orch-r-sm);cursor:pointer;transition:background .12s}
.orch-tpl-menu button:hover{background:var(--bg-hover)}
.orch-load-menu{position:absolute;top:42px;right:0;z-index:20;background:var(--bg-secondary);border:1px solid var(--border-light);border-radius:var(--orch-r-md);box-shadow:var(--orch-elev-pop);padding:6px;min-width:260px;max-height:340px;overflow-y:auto;animation:orchPop .14s ease}
.orch-load-empty{padding:14px;font-size:12px;color:var(--text-tertiary);text-align:center}
.orch-load-row{display:flex;align-items:center;gap:4px}
.orch-load-pick{flex:1;display:flex;justify-content:space-between;align-items:center;gap:10px;background:transparent;border:none;color:var(--text-primary);font-family:inherit;font-size:12.5px;padding:8px 10px;border-radius:var(--orch-r-sm);cursor:pointer;text-align:left;transition:background .12s}
.orch-load-pick:hover{background:var(--bg-hover)}
.orch-load-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.orch-load-meta{font-size:10.5px;color:var(--text-tertiary);flex-shrink:0}
.orch-load-del{background:transparent;border:none;color:var(--text-tertiary);cursor:pointer;padding:6px 8px;border-radius:var(--orch-r-sm);font-size:12px;transition:background .12s,color .12s}
.orch-load-del:hover{background:var(--error-bg);color:var(--error-text)}
.orch-body{flex:1;display:flex;min-height:0}
.orch-ai{width:0;flex-shrink:0;border-right:var(--orch-rail);background:var(--bg-secondary);display:flex;flex-direction:column;overflow:hidden;transition:width .2s ease}
.orch-ai.is-open{width:320px}
.orch-ai-head{display:flex;align-items:center;justify-content:space-between;padding:13px 14px;border-bottom:var(--orch-rail);font-size:12px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;color:var(--text-secondary)}
.orch-ai-clear{background:transparent;border:none;color:var(--text-tertiary);cursor:pointer;font-size:15px;padding:2px 6px;border-radius:var(--orch-r-sm);transition:background .12s,color .12s}
.orch-ai-clear:hover{background:var(--bg-hover);color:var(--text-primary)}
.orch-ai-log{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:9px}
.orch-ai-empty{text-align:center;padding:30px 14px;color:var(--text-tertiary)}
.orch-ai-empty-icon{font-size:30px;margin-bottom:10px}
.orch-ai-empty-title{font-size:14px;font-weight:700;color:var(--text-secondary);margin-bottom:8px}
.orch-ai-empty-text{font-size:12px;line-height:1.6}
.orch-ai-msg{font-size:12.5px;line-height:1.5;padding:9px 12px;border-radius:var(--orch-r-lg);max-width:90%;white-space:pre-wrap;word-break:break-word;box-shadow:var(--orch-elev-card)}
.orch-ai-user{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:4px}
.orch-ai-bot{align-self:flex-start;background:var(--bg-tertiary);color:var(--text-primary);border-bottom-left-radius:4px}
.orch-ai-typing{opacity:.7;font-style:italic}
.orch-dot{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent);animation:orchPulse 1s infinite}
@keyframes orchPulse{0%,100%{opacity:.3}50%{opacity:1}}
@keyframes orchPop{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.orch-ai-input{border-top:var(--orch-rail);padding:10px;display:flex;flex-direction:column;gap:8px}
.orch-ai-input textarea{width:100%;resize:none;background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--orch-r-md);color:var(--text-primary);font-family:inherit;font-size:12.5px;padding:9px 11px;outline:none;line-height:1.5;transition:border-color var(--transition),box-shadow var(--transition)}
.orch-ai-input textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-subtle)}
.orch-ai-send{width:100%}
.orch-btn.is-active{background:var(--accent);border-color:var(--accent);color:#fff}
.orch-btn-run{background:var(--orch-ok);border-color:var(--orch-ok);color:#fff}
.orch-btn-run:hover{background:var(--orch-ok-hover);border-color:var(--orch-ok-hover);color:#fff;box-shadow:0 3px 12px rgba(16,185,129,.3)}
.orch-btn-run:disabled{opacity:.5;cursor:default}
.orch-run-drawer{position:absolute;top:57px;right:0;bottom:0;width:0;background:var(--bg-secondary);border-left:var(--orch-rail);box-shadow:var(--orch-elev-pop);display:flex;flex-direction:column;overflow:hidden;transition:width .2s ease;z-index:30}
.orch-run-drawer.is-open{width:420px}
.orch-run-head{display:flex;align-items:center;justify-content:space-between;padding:13px 14px;border-bottom:var(--orch-rail);font-size:12px;font-weight:800;letter-spacing:.04em;text-transform:uppercase;color:var(--text-secondary)}
.orch-run-input{padding:11px;border-bottom:var(--orch-rail);display:flex;flex-direction:column;gap:9px}
.orch-run-input textarea{width:100%;resize:none;background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--orch-r-md);color:var(--text-primary);font-family:inherit;font-size:12.5px;padding:9px 11px;outline:none;line-height:1.5;transition:border-color var(--transition),box-shadow var(--transition)}
.orch-run-input textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-subtle)}
.orch-run-actions{display:flex;gap:8px}
.orch-run-actions .orch-btn{flex:1}
.orch-run-log{flex:1;overflow-y:auto;padding:12px;font-size:12px;line-height:1.6;font-family:var(--mono-font,monospace)}
.orch-ico{width:1em;height:1em;vertical-align:-0.15em;flex-shrink:0}
.orch-ico-lg{width:1.4em;height:1.4em}
.orch-run-line{padding:3px 0;color:var(--text-secondary);word-break:break-word}
.orch-run-line .orch-ico{margin-right:2px}
.orch-run-line.is-active{color:var(--accent)}
.orch-run-line.is-done{color:var(--orch-ok);font-weight:600}
.orch-run-line.is-err{color:var(--error-text)}
.orch-run-dim{color:var(--text-tertiary)}
.orch-human-gate{border:1px solid var(--accent);border-radius:var(--orch-r-md);padding:9px 11px;margin:6px 0;background:var(--accent-subtle,rgba(14,165,233,.08))}
.orch-human-actions{display:flex;gap:8px;margin-top:8px;align-items:center}
.orch-human-actions .orch-input{flex:1}
.orch-run-final{white-space:pre-wrap;background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--orch-r-md);padding:10px;margin-top:4px;font-size:11.5px;color:var(--text-primary);max-height:300px;overflow:auto}
.orch-palette{width:212px;flex-shrink:0;border-right:var(--orch-rail);background:var(--bg-secondary);overflow-y:auto;padding:14px 12px}
.orch-pal-section{font-size:10px;font-weight:800;letter-spacing:.09em;text-transform:uppercase;color:var(--text-tertiary);margin:10px 4px 8px}
.orch-pal-section:first-child{margin-top:2px}
.orch-pal-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.orch-chip{display:flex;flex-direction:column;align-items:center;gap:6px;padding:11px 6px;border:1px solid var(--border);border-radius:var(--orch-r-md);background:var(--bg-tertiary);cursor:grab;transition:border-color var(--transition),background var(--transition),transform .12s ease,box-shadow var(--transition);user-select:none;text-align:center}
.orch-chip:hover{border-color:var(--accent);background:var(--bg-hover);transform:translateY(-2px);box-shadow:var(--orch-elev-card)}
.orch-chip:active{cursor:grabbing;transform:scale(.97)}
.orch-chip-glyph{width:26px;height:26px;color:var(--chip-accent,var(--accent))}
.orch-chip-glyph svg{width:100%;height:100%}
.orch-chip-ava{width:34px;height:34px;display:flex;align-items:center;justify-content:center}
.orch-chip-ava img{width:38px;height:38px;object-fit:contain;image-rendering:auto;filter:drop-shadow(0 2px 4px rgba(0,0,0,.35))}
.orch-chip-role:hover .orch-chip-ava img{transform:scale(1.08);transition:transform .15s}
.orch-chip-label{font-size:11px;font-weight:600;color:var(--text-secondary)}
.orch-pal-foot{margin:16px 4px 4px;font-size:11px;line-height:1.5;color:var(--text-tertiary)}
.orch-canvas-wrap{flex:1;min-width:0;position:relative;background:var(--bg-primary);display:flex;flex-direction:column}
.orch-canvas{position:relative;flex:1;min-height:0;overflow:auto;background-color:var(--bg-primary);background-image:radial-gradient(color-mix(in srgb,var(--border) 70%,transparent) 1px,transparent 1px);background-size:24px 24px}
.orch-edges{position:absolute;top:0;left:0;pointer-events:none;min-width:100%;min-height:100%;overflow:visible}
.orch-edge-path{fill:none;stroke:color-mix(in srgb,var(--accent) 38%,var(--border-light));stroke-width:2;stroke-linecap:round;pointer-events:stroke;cursor:pointer;transition:stroke .15s,stroke-width .15s}
.orch-edge-path:hover{stroke:var(--error-text);stroke-width:2.75}
.orch-edge-arrow{fill:color-mix(in srgb,var(--accent) 55%,var(--border-light));stroke:none}
.orch-edge-temp{fill:none;stroke:var(--accent);stroke-width:2;stroke-dasharray:5 4;stroke-linecap:round;pointer-events:none;opacity:.85}
.orch-nodes{position:absolute;top:0;left:0;width:100%;height:100%}
.orch-node{position:absolute;width:${_ORCH_CARD_W}px;background:var(--bg-secondary);border:1px solid var(--border-light);border-left:4px solid var(--node-accent,var(--accent));border-radius:var(--orch-r-md);box-shadow:var(--orch-elev-card);user-select:none;transition:box-shadow .15s,border-color .15s,transform .12s ease}
.orch-node:hover{box-shadow:var(--orch-elev-pop);transform:translateY(-1px)}
.orch-node.is-selected{border-color:var(--accent);border-left-color:var(--node-accent,var(--accent));box-shadow:0 0 0 2px var(--accent-subtle),var(--orch-elev-pop)}
.orch-node.is-dragging{opacity:.95;box-shadow:var(--orch-elev-lift);z-index:50;transform:none}
.orch-node-artifact{border-style:dashed;border-left-style:solid;background:linear-gradient(180deg,var(--bg-secondary),color-mix(in srgb,var(--node-accent) 8%,var(--bg-secondary)))}
.orch-node-group{border-style:dashed;border-left-style:solid;border-width:1.5px;background:linear-gradient(180deg,var(--bg-secondary),color-mix(in srgb,#8b5cf6 10%,var(--bg-secondary)))}
.orch-node-group .orch-node-head{cursor:pointer}
.orch-node-group .orch-node-sub{font-family:var(--mono-font,monospace);color:#8b5cf6;opacity:.9}
.orch-chip-group{grid-column:1 / -1}
.orch-crumb{display:flex;align-items:center;gap:4px;flex-wrap:wrap;padding:8px 14px;border-bottom:var(--orch-rail);background:var(--bg-tertiary);font-size:12px;position:relative;z-index:4}
.orch-crumb-item{background:transparent;border:1px solid transparent;color:var(--text-secondary);font-family:inherit;font-size:12px;font-weight:600;padding:4px 9px;border-radius:var(--orch-r-sm);cursor:pointer;transition:background .12s,color .12s,border-color .12s}
.orch-crumb-item:hover{background:var(--bg-hover);color:var(--text-primary);border-color:var(--border-light)}
.orch-crumb-item:last-child{color:var(--accent)}
.orch-crumb-sep{color:var(--text-tertiary);font-size:12px}
.orch-node-artifact .orch-node-sub{font-family:var(--mono-font,monospace);color:var(--node-accent);opacity:.9}
.orch-node-start,.orch-node-stop{border-width:1.5px;border-style:solid;border-color:color-mix(in srgb,var(--node-accent) 55%,var(--border-light))}
.orch-node-start{background:linear-gradient(180deg,color-mix(in srgb,var(--node-accent) 14%,var(--bg-secondary)),var(--bg-secondary))}
.orch-node-stop{background:linear-gradient(180deg,var(--bg-secondary),color-mix(in srgb,var(--node-accent) 14%,var(--bg-secondary)))}
.orch-node-ribbon{position:absolute;right:9px;top:-9px;font-size:8.5px;font-weight:800;letter-spacing:.1em;padding:2px 7px;border-radius:999px;color:#fff;background:var(--node-accent);box-shadow:0 2px 6px rgba(0,0,0,.3);pointer-events:none;z-index:6}
.orch-ribbon-out{top:auto;bottom:-9px}
.orch-note-wire{background:color-mix(in srgb,var(--node-accent,var(--accent)) 9%,var(--bg-tertiary));border-left:3px solid var(--node-accent,var(--accent))}
.orch-note code{font-family:var(--mono-font,monospace);font-size:10.5px;background:var(--bg-primary);border:1px solid var(--border);border-radius:4px;padding:1px 4px;color:var(--text-primary)}
.orch-node-head{display:flex;align-items:center;gap:8px;padding:9px 8px 7px 11px;cursor:grab}
.orch-node-head:active{cursor:grabbing}
.orch-node-icon{width:24px;height:24px;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:var(--node-accent)}
.orch-node-icon img{width:26px;height:26px;object-fit:contain;filter:drop-shadow(0 1px 2px rgba(0,0,0,.3))}
.orch-node-icon svg{width:20px;height:20px}
.orch-node-title{flex:1;font-size:12.5px;font-weight:700;color:var(--text-primary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.orch-node-del{opacity:0;background:transparent;border:none;color:var(--text-tertiary);cursor:pointer;font-size:12px;padding:2px 4px;border-radius:var(--orch-r-sm);transition:opacity .15s,background .12s,color .12s}
.orch-node:hover .orch-node-del{opacity:1}
.orch-node-del:hover{background:var(--error-bg);color:var(--error-text)}
.orch-node-sub{padding:0 11px 10px;font-size:10.5px;color:var(--text-tertiary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.orch-port{position:absolute;left:50%;transform:translateX(-50%);width:12px;height:12px;border-radius:50%;background:var(--bg-secondary);border:2px solid var(--accent);cursor:crosshair;z-index:5;box-shadow:0 1px 3px rgba(0,0,0,.3);transition:transform .12s,background .12s,box-shadow .12s}
.orch-port:hover{transform:translateX(-50%) scale(1.4);background:var(--accent);box-shadow:0 0 0 4px var(--accent-subtle)}
.orch-port-in{top:-7px}
.orch-port-out{bottom:-7px}
.orch-hint{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none}
.orch-hint-card{max-width:340px;text-align:center;background:var(--bg-secondary);border:1px dashed var(--border-light);border-radius:var(--orch-r-lg);padding:26px 28px;box-shadow:var(--orch-elev-pop)}
.orch-hint-emoji{margin-bottom:10px;color:var(--text-tertiary)}
.orch-hint-emoji .orch-ico,.orch-hint-emoji .orch-ico-lg{width:34px;height:34px}
.orch-hint-title{font-size:16px;font-weight:700;color:var(--text-primary);margin-bottom:8px}
.orch-hint-text{font-size:12.5px;line-height:1.6;color:var(--text-secondary)}
.orch-inspector{width:300px;flex-shrink:0;border-left:var(--orch-rail);background:var(--bg-secondary);overflow-y:auto;padding:16px}
.orch-insp-empty{text-align:center;color:var(--text-tertiary);font-size:12.5px;padding-top:48px}
.orch-insp-empty-icon{margin-bottom:12px;opacity:.7}
.orch-insp-empty-icon .orch-ico{width:30px;height:30px}
.orch-insp-stats{margin-top:18px;font-size:11px;color:var(--text-tertiary)}
.orch-insp-head{display:flex;flex-direction:column;gap:3px;margin-bottom:16px;padding-bottom:12px;border-bottom:var(--orch-rail)}
.orch-insp-kind{font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
.orch-insp-type{font-size:17px;font-weight:700;color:var(--text-primary)}
.orch-fld{display:block;margin-bottom:13px}
.orch-fld>span{display:block;font-size:11px;font-weight:600;color:var(--text-secondary);margin-bottom:5px}
.orch-fld-check{display:flex;align-items:center;gap:8px}
.orch-fld-check>span{margin-bottom:0}
.orch-input{width:100%;background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--orch-r-sm);color:var(--text-primary);font-family:inherit;font-size:12.5px;padding:8px 10px;outline:none;transition:border-color var(--transition),box-shadow var(--transition)}
.orch-input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-subtle)}
.orch-ta{resize:vertical;line-height:1.5}
.orch-note{font-size:11px;line-height:1.55;color:var(--text-secondary);background:var(--accent-subtle);border-radius:var(--orch-r-sm);padding:9px 11px;margin:6px 0 4px}
.orch-conn-box{display:flex;gap:10px;margin:14px 0 2px;padding:10px;background:var(--bg-tertiary);border-radius:var(--orch-r-sm)}
.orch-conn-row{flex:1;font-size:11px;color:var(--text-secondary);text-align:center}
.orch-toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%);z-index:9999;background:var(--bg-tertiary);border:1px solid var(--border-light);color:var(--text-primary);font-size:13px;padding:11px 18px;border-radius:var(--orch-r-md);box-shadow:var(--orch-elev-pop);transition:opacity .3s}
.orch-toast.is-err{border-color:var(--error-border);color:var(--error-text)}
.orch-m-only{display:none}
.orch-sheet-head{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:var(--orch-rail);font-size:13px;font-weight:800;color:var(--text-secondary)}
.orch-sheet-hint{padding:9px 14px;font-size:11.5px;line-height:1.5;color:var(--text-tertiary);border-bottom:var(--orch-rail)}
@media(max-width:1100px) and (min-width:769px){.orch-palette{width:150px}.orch-inspector{width:250px}}

@media(max-width:768px){
  /* Full-screen modal — phones have no room for a centred floating card. */
  .orch-overlay{align-items:stretch;justify-content:stretch}
  .orch-shell{width:100vw;height:100vh;height:100dvh;max-width:none;border:none;border-radius:0}

  .orch-m-only{display:inline-flex}

  /* Header: one horizontally-scrollable row instead of wrapping into 3 tall
     rows that devour vertical canvas space. */
  .orch-top{padding:8px 10px;gap:8px}
  .orch-top-left{flex-shrink:1}
  .orch-name-input{width:100%;min-width:80px;font-size:15px;padding:5px 8px}
  .orch-top-actions{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none;gap:6px;flex-shrink:0;max-width:62vw}
  .orch-top-actions::-webkit-scrollbar{display:none}
  .orch-btn{padding:8px 10px;font-size:12px}
  .orch-top-sep{display:none}

  /* Side rails stop stealing canvas width: palette + inspector become
     full-width slide-up sheets, the AI panel + run drawer go full-screen.
     The canvas always fills the body so there's room to actually place
     nodes (which on touch happens via tap-to-add, see _orchAddNodeAtCenter). */
  .orch-body{position:relative}
  .orch-canvas-wrap{position:absolute;inset:0}

  .orch-palette,.orch-inspector{
    position:absolute;left:0;right:0;bottom:0;top:auto;width:auto;
    max-height:62%;border:none;border-top:var(--orch-rail);
    border-radius:var(--orch-r-xl) var(--orch-r-xl) 0 0;
    box-shadow:var(--orch-elev-lift);z-index:40;
    transform:translateY(110%);transition:transform .22s ease;
  }
  .orch-shell.orch-m-pal .orch-palette{transform:translateY(0)}
  .orch-shell.orch-m-insp .orch-inspector{transform:translateY(0)}
  .orch-pal-grid{grid-template-columns:repeat(3,1fr)}
  .orch-pal-foot{display:none}
  .orch-chip-group{grid-column:1 / -1}

  /* AI composer + run drawer: full-screen overlays driven by existing
     .is-open toggles (no JS change needed). */
  .orch-ai{position:absolute;inset:0;width:auto;z-index:50;transform:translateX(-110%);transition:transform .22s ease;border-right:none}
  .orch-ai.is-open{width:auto;transform:translateX(0)}
  .orch-run-drawer{top:0;width:auto;left:0;right:0}
  .orch-run-drawer.is-open{width:auto}

  /* Bigger touch targets for ports + node delete on the canvas. */
  .orch-port{width:16px;height:16px}
  .orch-port-in{top:-9px}
  .orch-port-out{bottom:-9px}
  .orch-node-del{opacity:1}

  .orch-tpl-menu,.orch-load-menu{position:fixed;left:8px;right:8px;top:auto;min-width:0;max-height:60vh}
}
`;
  var style = document.createElement('style');
  style.id = 'orch-studio-styles';
  style.textContent = css;
  document.head.appendChild(style);
}

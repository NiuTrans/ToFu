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
    +       '<div class="orch-tpl-wrap">'
    +         '<button class="orch-btn orch-btn-ghost" onclick="_orchToggleTplMenu()">✨ Templates ▾</button>'
    +         '<div class="orch-tpl-menu" id="orchTplMenu" style="display:none">'
    +           '<button onclick="_orchLoadTemplate(\'endpoint\');_orchToggleTplMenu(true)">🔁 Endpoint loop (plan→work→critic)</button>'
    +           '<button onclick="_orchLoadBuiltin(\'endpoint\');_orchToggleTplMenu(true)">⭐ Endpoint (canonical, backend)</button>'
    +           '<button onclick="_orchLoadTemplate(\'fanout\');_orchToggleTplMenu(true)">🌐 Fan-out → synthesize</button>'
    +           '<button onclick="_orchLoadTemplate(\'adversarial\');_orchToggleTplMenu(true)">⚔️ Adversarial verify</button>'
    +           '<button onclick="_orchLoadTemplate(\'blank\');_orchToggleTplMenu(true)">➕ Blank canvas</button>'
    +         '</div>'
    +       '</div>'
    +       '<div class="orch-tpl-wrap">'
    +         '<button class="orch-btn orch-btn-ghost" onclick="_orchOpenLoadMenu()">📂 Open ▾</button>'
    +         '<div class="orch-load-menu" id="orchLoadMenu" style="display:none"></div>'
    +       '</div>'
    +       '<button class="orch-btn orch-btn-ghost" onclick="_orchTidy()" title="Auto-arrange nodes into clean top-down lanes">⤓ Tidy</button>'
    +       '<span class="orch-top-sep" aria-hidden="true"></span>'
    +       '<button class="orch-btn orch-btn-ghost" id="orchAiToggle" onclick="_orchToggleAi()">🪄 AI Composer</button>'
    +       '<span class="orch-top-sep" aria-hidden="true"></span>'
    +       '<button class="orch-btn orch-btn-run" onclick="_orchOpenRun()">▶ Run</button>'
    +       '<button class="orch-btn orch-btn-ghost" onclick="_orchExport()">⬇ Export JSON</button>'
    +       '<button class="orch-btn orch-btn-primary" onclick="_orchSave()">💾 Save</button>'
    +       '<span class="orch-top-sep" aria-hidden="true"></span>'
    +       '<button class="orch-btn orch-btn-close" onclick="closeOrchestration()" title="Close">✕</button>'
    +     '</div>'
    +   '</header>'
    +   '<div class="orch-body">'
    +     '<aside class="orch-ai" id="orchAi">'
    +       '<div class="orch-ai-head"><span>🪄 AI Composer</span>'
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
    +         '<button class="orch-btn orch-btn-ghost" onclick="_orchPlan()">👁 Preview plan</button>'
    +         '<button class="orch-btn orch-btn-run" id="orchRunBtn" onclick="_orchRun()">▶ Run</button>'
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

  var html = '<div class="orch-pal-section">Control</div><div class="orch-pal-grid">';
  _ORCH_CONTROLS.forEach(function (c) {
    html += '<div class="orch-chip orch-chip-ctrl" draggable="true" '
         +  'data-ptype="control" data-pkind="' + c.kind + '" '
         +  'style="--chip-accent:' + c.accent + '" title="' + escapeHtml(c.blurb) + '">'
         +    '<span class="orch-chip-glyph">' + _ORCH_GLYPHS[c.glyph] + '</span>'
         +    '<span class="orch-chip-label">' + escapeHtml(c.label) + '</span>'
         +  '</div>';
  });
  html += '</div>';

  html += '<div class="orch-pal-section">Agents</div><div class="orch-pal-grid">';
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
  html += '<div class="orch-pal-foot">Drag onto the canvas → wire ports → tune in the inspector.</div>';
  el.innerHTML = html;

  el.querySelectorAll('.orch-chip').forEach(function (chip) {
    chip.addEventListener('dragstart', function (e) {
      var payload = {
        ptype: chip.getAttribute('data-ptype'),
        role: chip.getAttribute('data-prole') || '',
        kind: chip.getAttribute('data-pkind') || '',
      };
      e.dataTransfer.setData('text/orch', JSON.stringify(payload));
      e.dataTransfer.effectAllowed = 'copy';
    });
  });
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
    id: _orchNextId(payload.ptype === 'role' ? payload.role : payload.kind),
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

function _orchDefaultParams(payload) {
  if (payload.ptype === 'role') {
    var rdef = _ORCH_ROLES.filter(function (r) { return r.role === payload.role; })[0] || {};
    return { objective: '', tier: rdef.tier || 'standard', isolation: 'fresh-context' };
  }
  switch (payload.kind) {
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
}

function _orchRenderHint() {
  var h = document.getElementById('orchHint');
  if (!h) return;
  h.style.display = _orchNodes.length ? 'none' : 'block';
  if (!_orchNodes.length) {
    h.innerHTML = '<div class="orch-hint-card">'
      + '<div class="orch-hint-emoji">🧩</div>'
      + '<div class="orch-hint-title">Compose a flow</div>'
      + '<div class="orch-hint-text">Drag a <b>Start</b> node and some agents from the left, '
      + 'then drag between the ● ports to wire them. Try a template ✨ to see a working loop.</div>'
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
    if (n.type === 'role') {
      var rdef = _ORCH_ROLES.filter(function (r) { return r.role === n.role; })[0] || {};
      accent = '#6e56cf';
      typeCls = ' orch-node-role';
      iconHtml = '<img src="' + _orchIconSrc(rdef.icon) + '" alt="" '
               + 'onerror="this.style.display=\'none\'">';
      sub = escapeHtml(n.params.tier || 'standard') + ' · ' + escapeHtml(n.params.isolation || 'fresh');
    } else {
      var cdef = _ORCH_CONTROLS.filter(function (c) { return c.kind === n.kind; })[0] || {};
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
    if (hasIn) {
      html += '<span class="orch-port orch-port-in" onpointerup="_orchPortUp(event,\'' + n.id + '\')"></span>';
    }
    html += '<div class="orch-node-head" onpointerdown="_orchNodeHeaderDown(event,\'' + n.id + '\')">'
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
  if (n.kind === 'start') return 'user request enters here';
  if (n.kind === 'stop') return 'result returns to chat';
  return '';
}

function _orchAutoLabel(n) {
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
  svg.setAttribute('width', canvas.scrollWidth);
  svg.setAttribute('height', canvas.scrollHeight);

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
  if (!n) {
    el.innerHTML = '<div class="orch-insp-empty">'
      + '<div class="orch-insp-empty-icon">⚙️</div>'
      + 'Select a node to edit its settings.'
      + '<div class="orch-insp-stats">' + _orchNodes.length + ' nodes · ' + _orchEdges.length + ' links</div>'
      + '</div>';
    return;
  }

  var h = '<div class="orch-insp-head">'
        + '<span class="orch-insp-kind">' + escapeHtml(n.type === 'role' ? 'Agent' : 'Control') + '</span>'
        + '<span class="orch-insp-type">' + escapeHtml(_orchAutoLabel(n)) + '</span>'
        + '</div>';

  h += '<label class="orch-fld"><span>Label</span>'
    +  '<input class="orch-input" value="' + escapeHtml(n.name) + '" '
    +  'placeholder="' + escapeHtml(_orchAutoLabel(n)) + '" '
    +  'oninput="_orchSetParam(\'name\', this.value)"></label>';

  if (n.type === 'role') {
    h += '<label class="orch-fld"><span>Objective</span>'
      +  '<textarea class="orch-input orch-ta" rows="4" '
      +  'placeholder="What should this agent accomplish? Brief it like a colleague who just walked in." '
      +  'oninput="_orchSetParam(\'objective\', this.value)">' + escapeHtml(n.params.objective || '') + '</textarea></label>';
    h += _orchSelectFld('Model tier', 'tier', n.params.tier, [['light', 'Light · fast'], ['standard', 'Standard · parent'], ['heavy', 'Heavy · strongest']]);
    h += _orchSelectFld('Context', 'isolation', n.params.isolation,
        [['fresh-context', 'Fresh — one-shot, isolated'], ['shared-context', 'Shared — accumulates across loops']]);
    h += '<div class="orch-note">💡 <b>Shared</b> context makes a Worker that learns across loop iterations (like endpoint mode). <b>Fresh</b> is a stateless fan-out sub-agent.</div>';
  } else if (n.kind === 'loop') {
    h += _orchNumFld('Max iterations', 'max_iterations', n.params.max_iterations);
    h += _orchSelectFld('Stop when', 'stop_condition', n.params.stop_condition,
        [['verdict:STOP', 'Verifier returns STOP'], ['no_new_findings', 'A round finds nothing new'], ['max_only', 'Only the iteration cap']]);
    h += _orchSelectFld('Verifier role', 'verifier', n.params.verifier,
        [['critic', 'Critic'], ['reviewer', 'Reviewer'], ['none', 'No verifier']]);
    h += '<div class="orch-note">🔁 Wrap a Worker (+ optional Verifier) inside this loop. The verifier is a <i>different</i> agent than the producer — that\'s structural adversarial verification.</div>';
  } else if (n.kind === 'parallel') {
    h += _orchNumFld('Max concurrent', 'max_concurrent', n.params.max_concurrent);
    h += _orchCheckFld('One agent per item', 'per_item', n.params.per_item);
  } else if (n.kind === 'branch') {
    h += _orchSelectFld('Classifier role', 'classifier', n.params.classifier,
        [['router', 'Router'], ['analyst', 'Analyst'], ['general', 'General']]);
    h += _orchNumFld('Branch count', 'branches', n.params.branches);
  } else if (n.kind === 'artifact') {
    h += '<label class="orch-fld"><span>File path</span>'
      +  '<input class="orch-input" value="' + escapeHtml(n.params.path || '') + '" '
      +  'placeholder="e.g. reports/findings.md" '
      +  'oninput="_orchSetParam(\'path\', this.value)"></label>';
    h += _orchSelectFld('Kind', 'format', n.params.format,
        [['file', 'File'], ['report', 'Report'], ['dataset', 'Dataset'],
         ['code', 'Code'], ['image', 'Image']]);
    h += '<label class="orch-fld"><span>Description</span>'
      +  '<textarea class="orch-input orch-ta" rows="3" '
      +  'placeholder="What this deliverable must contain — the contract between the '
      +  'producing and consuming agents." '
      +  'oninput="_orchSetParam(\'description\', this.value)">' + escapeHtml(n.params.description || '') + '</textarea></label>';
    h += '<div class="orch-note">\uD83D\uDCE6 A <b>Deliverable</b> declares an expected '
      +  'intermediate output. The engine records it and surfaces it in the run log so '
      +  'you can track what each stage is supposed to produce.</div>';
  } else if (n.kind === 'human') {
    h += _orchSelectFld('Mode', 'mode', n.params.mode,
        [['approve', 'Approve — pause for go / no-go'],
         ['input', 'Input — collect an answer'],
         ['notify', 'Notify — message, don\'t block']]);
    h += '<label class="orch-fld"><span>Prompt</span>'
      +  '<textarea class="orch-input orch-ta" rows="3" '
      +  'placeholder="What to ask / tell the user at this gate." '
      +  'oninput="_orchSetParam(\'prompt\', this.value)">' + escapeHtml(n.params.prompt || '') + '</textarea></label>';
    if (n.params.mode === 'approve') {
      h += _orchNumFld('Approve timeout (sec)', 'timeout_sec', n.params.timeout_sec);
    }
    h += '<div class="orch-note">🧑 A <b>Human</b> gate pauses the flow: '
      +  '<b>Approve</b> halts the run on reject, <b>Input</b> appends the answer to '
      +  'the context for downstream agents, <b>Notify</b> just surfaces a message. '
      +  'Reuses the same approval / ask-human plumbing as chat.</div>';
  } else {
    h += '<div class="orch-note">' + escapeHtml((_ORCH_CONTROLS.filter(function (c) { return c.kind === n.kind; })[0] || {}).blurb || '') + '</div>';
  }

  // Connections summary
  var ins = _orchEdges.filter(function (e) { return e.to === n.id; });
  var outs = _orchEdges.filter(function (e) { return e.from === n.id; });
  h += '<div class="orch-conn-box"><div class="orch-conn-row">→ in: <b>' + ins.length + '</b></div>'
    +  '<div class="orch-conn-row">out: <b>' + outs.length + '</b> →</div></div>';

  h += '<button class="orch-btn orch-btn-danger orch-btn-block" onclick="_orchDeleteNode(\'' + n.id + '\')">Delete node</button>';
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

function _orchSetParam(key, value, isNum) {
  var n = _orchFind(_orchSel);
  if (!n) return;
  if (key === 'name') { n.name = value; _orchRenderNodes(); return; }
  if (isNum) value = (value === '' ? '' : Number(value));
  n.params[key] = value;
  _orchRenderNodes();   // sub-line may change
  // The human gate shows/hides fields by mode → re-render the inspector.
  if (key === 'mode') _orchRenderInspector();
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
      + '<div class="orch-ai-empty-icon">🪄</div>'
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

  // Send the current graph so the model edits in place.
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
    _orchAiHistory.push({ role: 'assistant', content: '⚠️ The composer request failed. Try again.' });
    _orchRenderAiLog();
    return;
  }
  var reply = result.reply || (result.ok ? 'Updated the graph.' : 'I could not build a valid graph.');
  // Surface validation issues inline so the user understands a rejected draft.
  if (!result.ok && result.validation && result.validation.errors && result.validation.errors.length) {
    reply += '\n⚠️ ' + result.validation.errors.slice(0, 3).join('; ');
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

function _orchOpenRun() {
  var d = document.getElementById('orchRunDrawer');
  if (d) d.classList.add('is-open');
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
  var head = '🧑 <b>' + escapeHtml(ev.name || 'Human') + '</b> — '
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
  if (!_orchNodes.length) { _orchToast('Nothing to plan', true); return; }
  var def = _orchToDefinition();
  var res = await Api.orchestrations.plan(def);
  var log = document.getElementById('orchRunLog');
  if (log) log.innerHTML = '';
  if (!res || !res.ok) {
    _orchRunLog('⚠️ ' + escapeHtml((res && res.error) || 'plan failed'), 'is-err');
    return;
  }
  _orchRunLog('<b>Execution plan (' + res.steps.length + ' steps):</b>');
  res.steps.forEach(function (s, i) {
    var label = s.role ? ('🤖 ' + s.role) : ('⬡ ' + (s.kind || s.action));
    _orchRunLog((i + 1) + '. ' + escapeHtml(label) + ' <span class="orch-run-dim">(' + escapeHtml(s.action) + ')</span>');
  });
}

async function _orchRun() {
  if (_orchRunPolling) return;
  if (!_orchNodes.length) { _orchToast('Nothing to run', true); return; }
  var def = _orchToDefinition();
  var input = (document.getElementById('orchRunInput') || {}).value || '';
  var log = document.getElementById('orchRunLog');
  if (log) log.innerHTML = '';
  _orchRunLog('🚀 Starting run…');

  var res = await Api.orchestrations.run(def, input);
  if (!res || !res.ok || !res.task_id) {
    _orchRunLog('⚠️ ' + escapeHtml((res && (res.error || (res.errors || []).join('; '))) || 'run failed'), 'is-err');
    return;
  }
  _orchRunTaskId = res.task_id;
  _orchRunSetBusy(true);
  _orchRunPoll(0);
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
    _orchRunLog('⚠️ poll failed', 'is-err');
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
      _orchRunLog('▶ <b>' + escapeHtml(ev.name || 'flow') + '</b> — ' + (ev.nodes || 0) + ' nodes'); break;
    case 'step_start':
      _orchRunLog('🤖 <b>' + escapeHtml(ev.name || ev.role) + '</b> running…', 'is-active'); break;
    case 'step_complete':
      _orchRunLog('✅ ' + escapeHtml(ev.role) + ' <span class="orch-run-dim">' + escapeHtml((ev.preview || '').slice(0, 120)) + '</span>'); break;
    case 'loop_iteration':
      _orchRunLog('🔁 loop iteration ' + ev.iteration + '/' + ev.max); break;
    case 'zero_deliverable_guard':
      _orchRunLog('⚠️ zero-deliverable guard — injecting "execute, stop analyzing" directive', 'is-err'); break;
    case 'replan':
      _orchRunLog('🧭 re-plan #' + ev.replan + ' — ' + escapeHtml((ev.defect || 'structural defect').slice(0, 100))); break;
    case 'stuck_detected':
      _orchRunLog('🔁 stuck — verifier feedback is repeating; breaking the loop', 'is-err'); break;
    case 'parallel_start':
      _orchRunLog('🌐 fan-out → ' + ev.branches + ' branches'); break;
    case 'branch_pick':
      _orchRunLog('↪ route → ' + escapeHtml(ev.chosen || '(none)')); break;
    case 'artifact_declared':
      _orchRunLog('📦 deliverable: <b>' + escapeHtml(ev.path || ev.name || '(unnamed)') + '</b>'
        + (ev.description ? ' <span class="orch-run-dim">' + escapeHtml(ev.description.slice(0, 120)) + '</span>' : '')); break;
    case 'human_notify':
      _orchRunLog('🧑 <b>' + escapeHtml(ev.name || 'Human') + '</b> '
        + '<span class="orch-run-dim">' + escapeHtml((ev.prompt || '').slice(0, 200)) + '</span>'); break;
    case 'human_request':
      _orchRenderHumanGate(ev); break;
    case 'human_resolved':
      _orchClearHumanGate(ev.request_id);
      _orchRunLog('🧑 ' + (ev.mode === 'approve'
        ? (ev.approved ? '✅ approved' : '⛔ rejected')
        : '✅ answered') + ' <span class="orch-run-dim">' + escapeHtml(ev.request_id || '') + '</span>'); break;
    case 'flow_complete':
      _orchRunLog('🏁 <b>' + escapeHtml(ev.status) + '</b> — ' + (ev.agents_run || 0) + ' agents, ' + (ev.elapsed || 0) + 's',
                  ev.status === 'completed' ? 'is-done' : 'is-err'); break;
    case 'done':
      if (ev.result && ev.result.final) {
        _orchRunLog('<b>Result:</b>'); _orchRunLog('<pre class="orch-run-final">' + escapeHtml(ev.result.final.slice(0, 4000)) + '</pre>');
      }
      break;
    case 'error':
      _orchRunLog('⚠️ ' + escapeHtml((ev.error && ev.error.detail) || 'error'), 'is-err'); break;
  }
}

async function _orchRunAbort() {
  if (!_orchRunTaskId) return;
  await Api.orchestrations.runAbort(_orchRunTaskId);
  _orchRunLog('⏹ abort requested…');
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
.orch-run-line{padding:3px 0;color:var(--text-secondary);word-break:break-word}
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
.orch-canvas-wrap{flex:1;min-width:0;position:relative;background:var(--bg-primary)}
.orch-canvas{position:absolute;inset:0;overflow:auto;background-color:var(--bg-primary);background-image:radial-gradient(color-mix(in srgb,var(--border) 70%,transparent) 1px,transparent 1px);background-size:24px 24px}
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
.orch-node-artifact .orch-node-sub{font-family:var(--mono-font,monospace);color:var(--node-accent);opacity:.9}
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
.orch-hint-emoji{font-size:34px;margin-bottom:10px}
.orch-hint-title{font-size:16px;font-weight:700;color:var(--text-primary);margin-bottom:8px}
.orch-hint-text{font-size:12.5px;line-height:1.6;color:var(--text-secondary)}
.orch-inspector{width:300px;flex-shrink:0;border-left:var(--orch-rail);background:var(--bg-secondary);overflow-y:auto;padding:16px}
.orch-insp-empty{text-align:center;color:var(--text-tertiary);font-size:12.5px;padding-top:48px}
.orch-insp-empty-icon{font-size:30px;margin-bottom:12px;opacity:.7}
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
@media(max-width:820px){.orch-palette{width:120px}.orch-inspector{width:240px}}
`;
  var style = document.createElement('style');
  style.id = 'orch-studio-styles';
  style.textContent = css;
  document.head.appendChild(style);
}

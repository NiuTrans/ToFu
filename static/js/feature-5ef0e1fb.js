// ═══ orchestration.js ═══
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
var _orchNodes = [];
var _orchEdges = [];
var _orchSel = null;
var _orchSelEdge = null;
var _orchSeq = 0;
var _orchName = 'Untitled Flow';
var _orchModalReady = false;
var _orchConnect = null;
var _orchDragNode = null;
var _orchCurrentId = null;
var _orchStack = [];
var _ORCH_CARD_W = 188;
function _orchNextId(prefix) { _orchSeq++; return (prefix || 'n') + _orchSeq; }
function _orchIconBase() {
return (typeof BASE_PATH !== 'undefined' ? BASE_PATH : '') + '/static/icons';
}
var _ORCH_ICON_VER = '20260622a';
function _orchIconSrc(icon) {
var name = icon || 'tofu-general';
var file = /\.\w+$/.test(name) ? name : name + '.png';
return _orchIconBase() + '/' + file + '?v=' + _ORCH_ICON_VER;
}
function openOrchestration() {
_orchEnsureModal();
var ov = document.getElementById('orchModal');
if (ov) ov.style.display = 'flex';
if (!_orchNodes.length) _orchLoadTemplate('endpoint');
_orchRender();
_orchFetchRoleSchema();
}
function closeOrchestration(evt) {
var ov = document.getElementById('orchModal');
if (!ov) return;
if (evt && evt.target !== ov) return;
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
+       '<span>' + _ORCH_ICONS.rocket + ' ' + t('orch.run.title') + '</span>'
+       '<button class="orch-ai-clear" onclick="_orchCloseRun()" title="Close">' + _ORCH_ICONS.reject + '</button>'
+     '</div>'
+     '<div class="orch-run-input">'
+       '<textarea id="orchRunInput" rows="2" placeholder="Initial request / input for the flow (optional)…"></textarea>'
+       '<div class="orch-run-hint">' + _ORCH_ICONS.eye + ' ' + t('orch.run.hint') + '</div>'
+       '<div class="orch-run-actions">'
+         '<button class="orch-btn orch-btn-ghost" onclick="_orchPlan()">' + _ORCH_ICONS.eye + ' ' + t('orch.run.previewPlan') + '</button>'
+         '<button class="orch-btn orch-btn-run" id="orchRunBtn" onclick="_orchRun()" title="' + t('orch.run.testRun') + '">' + _ORCH_ICONS.auto + ' ' + t('orch.run.testRun') + '</button>'
+         '<button class="orch-btn orch-btn-primary" id="orchRunTaskBtn" onclick="_orchRunAsTask()" title="' + t('orch.run.asTask') + '">' + _ORCH_ICONS.rocket + ' ' + t('orch.run.asTask') + '</button>'
+         '<button class="orch-btn orch-btn-danger" id="orchRunAbort" onclick="_orchRunAbort()" style="display:none">' + _ORCH_ICONS.stop + ' ' + t('orch.run.stop') + '</button>'
+       '</div>'
+     '</div>'
+     '<div class="orch-run-log" id="orchRunLog"></div>'
+   '</div>'
+ '</div>';
document.body.appendChild(ov);
_orchModalReady = true;
_orchRenderPalette();
_orchWireCanvas();
document.addEventListener('keydown', _orchOnKeyDown);
}
function _orchOnKeyDown(e) {
var ov = document.getElementById('orchModal');
if (!ov || ov.style.display === 'none') return;
if (e.key !== 'Delete' && e.key !== 'Backspace') return;
var tag = (e.target && e.target.tagName || '').toLowerCase();
if (tag === 'input' || tag === 'textarea' || tag === 'select'
|| (e.target && e.target.isContentEditable)) return;
if (_orchSelEdge) {
e.preventDefault();
_orchDeleteEdge(_orchSelEdge);
} else if (_orchSel) {
e.preventDefault();
_orchDeleteNode(_orchSel);
}
}
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
chip.addEventListener('click', function () {
if (!_orchIsMobile()) return;
_orchAddNodeAtCenter(_payload());
_orchCloseMobilePalette();
});
});
}
function _orchIsMobile() {
var q = (typeof window.mobileMediaQuery === 'function')
? window.mobileMediaQuery() : '(max-width:768px)';
return window.matchMedia && window.matchMedia(q).matches;
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
canvas.addEventListener('pointerdown', function (e) {
if (e.target === canvas || e.target.id === 'orchNodes' || e.target.id === 'orchEdges') {
_orchSel = null; _orchSelEdge = null;
_orchRenderNodes(); _orchRenderEdges(); _orchRenderInspector();
}
});
canvas.addEventListener('pointermove', _orchOnPointerMove);
window.addEventListener('pointerup', _orchOnPointerUp);
}
function _orchAddNode(payload, x, y) {
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
var rdef =  (_ORCH_ROLES.filter(function (r) { return r.role === payload.role; })[0] || {});
return { objective: '', tier: rdef.tier || 'standard', isolation: 'fresh-context' };
}
if (payload.ptype === 'subflow') {
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
_orchSelEdge = null;
_orchRender();
}
function _orchDeleteEdge(id) {
_orchEdges = _orchEdges.filter(function (e) { return e.id !== id; });
if (_orchSelEdge === id) _orchSelEdge = null;
_orchRenderEdges();
_orchRenderInspector();
}
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
var rdef =  (_ORCH_ROLES.filter(function (r) { return r.role === n.role; })[0] || {});
accent = '#6e56cf';
typeCls = ' orch-node-role';
iconHtml = '<img src="' + _orchIconSrc(rdef.icon) + '" alt="" '
+ 'onerror="this.style.display=\'none\'">';
sub = escapeHtml(n.params.tier || 'standard') + ' · ' + escapeHtml(n.params.isolation || 'fresh');
var _eff = n.params.emits || _orchDefaultEmits(n.role);
if (_eff === 'user') sub += ' · ' + _ORCH_ICONS.speak + 'user';
sub += _orchIoBadge(n);
} else {
var cdef =  (_ORCH_CONTROLS.filter(function (c) { return c.kind === n.kind; })[0] || {});
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
var glyph = (scope === 'isolated') ? '\u25a3' : '\u25a4';
return glyph + ' ' + escapeHtml(scope) + ' · ' + nn + ' nodes' + _orchIoBadge(n);
}
function _orchIoBadge(n) {
var io = n.params && n.params.io;
if (!io) return '';
var ni = Array.isArray(io.inputs) ? io.inputs.length : 0;
var no = Array.isArray(io.outputs) ? io.outputs.length : 0;
if (!ni && !no) return '';
return ' · <span class="orch-io-badge">\u21c4 ' + ni + '/' + no + '</span>';
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
function _orchKindLabel(n) {
return (n.type === 'subflow') ? t('orch.kind.group')
: (n.type === 'role') ? t('orch.kind.agent') : t('orch.kind.control');
}
function _orchNodeBlurb(n) {
if (n.type === 'role') {
var r = _ORCH_ROLES.filter(function (x) { return x.role === n.role; })[0];
return r ? r.blurb : '';
}
if (n.type === 'subflow') return '';
var c = _ORCH_CONTROLS.filter(function (x) { return x.kind === n.kind; })[0];
return c ? c.blurb : '';
}
function _orchInspAvatar(n) {
if (n.type === 'role') {
var r = _ORCH_ROLES.filter(function (x) { return x.role === n.role; })[0];
return '<img class="orch-insp-avatar" src="'
+ escapeHtml(_orchIconSrc(r ? r.icon : 'tofu-general')) + '" alt="">';
}
if (n.type === 'subflow') {
return '<span class="orch-insp-avatar orch-insp-glyph">' + _ORCH_GLYPHS.group + '</span>';
}
var c = _ORCH_CONTROLS.filter(function (x) { return x.kind === n.kind; })[0];
var glyph = _ORCH_GLYPHS[(c && c.glyph) || n.kind] || _ORCH_GLYPHS.play;
var accent = c ? c.accent : 'var(--accent)';
return '<span class="orch-insp-avatar orch-insp-glyph" style="--node-accent:' + accent + '">'
+ glyph + '</span>';
}
function _orchInspHeader(n) {
var h = '<div class="orch-insp-head">' + _orchInspAvatar(n)
+ '<div class="orch-insp-htext">'
+ '<span class="orch-insp-kind">' + escapeHtml(_orchKindLabel(n)) + '</span>'
+ '<span class="orch-insp-type">' + escapeHtml(_orchAutoLabel(n)) + '</span>'
+ '</div></div>';
var blurb = _orchNodeBlurb(n);
if (blurb) h += '<div class="orch-insp-blurb">' + escapeHtml(blurb) + '</div>';
return h;
}
function _orchSec(titleKey, icon, open, inner, hintKey) {
var h = '<details class="orch-sec"' + (open ? ' open' : '') + '>';
h += '<summary class="orch-sec-sum">' + (icon || '')
+ '<span>' + escapeHtml(t(titleKey)) + '</span>'
+ '<span class="orch-sec-chev">\u203a</span></summary>';
h += '<div class="orch-sec-body">';
if (hintKey) h += '<div class="orch-sec-hint">' + t(hintKey) + '</div>';
h += inner + '</div></details>';
return h;
}
function _orchSelectNode(id) {
if (_orchDragNode) return;
_orchSel = id;
_orchSelEdge = null;
_orchRenderNodes();
_orchRenderEdges();
_orchRenderInspector();
}
function _orchSelectEdge(id) {
_orchSelEdge = id;
_orchSel = null;
_orchRenderNodes();
_orchRenderEdges();
_orchRenderInspector();
}
function _orchRenderEdgeInspector(edge) {
var from = _orchFind(edge.from), to = _orchFind(edge.to);
var fromLbl = from ? escapeHtml(from.name || _orchAutoLabel(from)) : escapeHtml(edge.from);
var toLbl = to ? escapeHtml(to.name || _orchAutoLabel(to)) : escapeHtml(edge.to);
var h = '<div class="orch-sheet-head orch-m-only"><span>' + _ORCH_ICONS.gear + ' '
+ escapeHtml(t('orch.edge.title')) + '</span>'
+ '<button class="orch-ai-clear" onclick="_orchCloseMobileInspector()" title="Close">✕</button></div>';
h += '<div class="orch-insp-head">'
+ '<span class="orch-insp-kind">' + escapeHtml(t('orch.edge.title')) + '</span>'
+ '<span class="orch-insp-type">' + fromLbl + ' → ' + toLbl + '</span></div>';
h += '<div class="orch-edge-flow"><b>' + fromLbl + '</b> '
+ '<span class="orch-edge-arrowtxt">→</span> <b>' + toLbl + '</b></div>';
var inPorts = to ? _orchNodeInputs(to) : [];
if (inPorts.length && from) {
var srcOuts = _orchNodeOutputs(from);
h += '<div class="orch-note orch-note-wire">' + t('orch.edge.bindNote') + '</div>';
inPorts.forEach(function (ip, idx) {
var optList = [['', t('orch.edge.bindNone')]];
srcOuts.forEach(function (op) {
var ref = (op.name === 'text' && srcOuts.length === 1) ? from.id : (from.id + '.' + op.name);
optList.push([ref, op.name + ' (' + (op.type || 'any') + ')']);
});
var cur = (ip.from && (ip.from === from.id || ip.from.indexOf(from.id + '.') === 0)) ? ip.from : '';
var o = optList.map(function (p) {
return '<option value="' + escapeHtml(p[0]) + '"' + (p[0] === cur ? ' selected' : '') + '>' + escapeHtml(p[1]) + '</option>';
}).join('');
h += '<label class="orch-fld"><span>' + escapeHtml(t('orch.edge.bindTo', { port: ip.name })) + '</span>'
+ '<select class="orch-input" onchange="_orchBindEdgeInput(\'' + edge.to + '\',' + idx + ', this.value)">'
+ o + '</select></label>';
});
}
h += '<div class="orch-edge-btns">';
h += '<button class="orch-btn orch-btn-ghost orch-btn-block" onclick="_orchReverseEdge(\'' + edge.id + '\')">'
+ escapeHtml(t('orch.edge.reverse')) + '</button>';
h += '<button class="orch-btn orch-btn-danger orch-btn-block" onclick="_orchDeleteEdge(\'' + edge.id + '\')">'
+ escapeHtml(t('orch.edge.delete')) + '</button>';
h += '</div>';
return h;
}
function _orchReverseEdge(id) {
var e = _orchEdges.filter(function (x) { return x.id === id; })[0];
if (!e) return;
var s = _orchFind(e.to), d = _orchFind(e.from);
if (s && s.kind === 'stop') { _orchToast(t('orch.toast.stopNoOut')); return; }
if (d && d.kind === 'start') { _orchToast(t('orch.toast.startNoIn')); return; }
if (_orchEdges.some(function (x) { return x.from === e.to && x.to === e.from; })) {
_orchToast(t('orch.toast.dupEdge')); return;
}
var tmp = e.from; e.from = e.to; e.to = tmp;
_orchRenderEdges();
_orchRenderInspector();
}
function _orchBindEdgeInput(targetId, idx, ref) {
var n = _orchFind(targetId);
if (!n) return;
var io =  (n.params.io = n.params.io || {});
var inputs = (io.inputs = io.inputs || []);
if (!inputs[idx]) return;
if (ref) inputs[idx].from = ref;
else delete inputs[idx].from;
_orchRenderNodes();
}
function _orchNodeInputs(n) {
var io = n && n.params && n.params.io;
return (io && Array.isArray(io.inputs)) ? io.inputs : [];
}
function _orchNodeOutputs(n) {
var io = n && n.params && n.params.io;
if (io && Array.isArray(io.outputs) && io.outputs.length) return io.outputs;
return [{ name: 'text', type: 'text' }];
}
var _ORCH_IO_TYPES = ['text', 'json', 'artifact', 'file', 'number', 'bool', 'any'];
function _orchIoSectionBody(n) {
var io = (n.params && n.params.io) || {};
var inputs = Array.isArray(io.inputs) ? io.inputs : [];
var outputs = Array.isArray(io.outputs) ? io.outputs : [];
var typeOpts = function (cur) {
return _ORCH_IO_TYPES.map(function (ty) {
return '<option value="' + ty + '"' + (ty === cur ? ' selected' : '') + '>' + ty + '</option>';
}).join('');
};
var h = '';
h += '<div class="orch-io-head">' + escapeHtml(t('orch.io.outputs')) + '</div>';
if (!outputs.length) {
h += '<div class="orch-io-implicit">' + escapeHtml(t('orch.io.implicitOut')) + '</div>';
}
outputs.forEach(function (p, i) {
h += '<div class="orch-io-port">'
+ '<input class="orch-input orch-io-name" value="' + escapeHtml(p.name || '') + '" '
+ 'placeholder="name" oninput="_orchIoSet(\'outputs\',' + i + ',\'name\',this.value)">'
+ '<select class="orch-input orch-io-type" onchange="_orchIoSet(\'outputs\',' + i + ',\'type\',this.value)">'
+ typeOpts(p.type || 'text') + '</select>'
+ '<button class="orch-io-del" title="' + escapeHtml(t('orch.io.removePort')) + '" '
+ 'onclick="_orchIoRemove(\'outputs\',' + i + ')">✕</button></div>';
});
h += '<button class="orch-btn orch-btn-ghost orch-io-add" onclick="_orchIoAdd(\'outputs\')">'
+ _ORCH_ICONS.plus + ' ' + escapeHtml(t('orch.io.addOutput')) + '</button>';
h += '<div class="orch-io-head orch-io-head-in">' + escapeHtml(t('orch.io.inputs')) + '</div>';
if (inputs.length) {
h += '<div class="orch-io-subhint">' + escapeHtml(t('orch.io.inputsHint')) + '</div>';
}
var hasUpstream = false;
var up = _orchUpstreamIds(n.id);
_orchNodes.forEach(function (m) {
if (m.id !== n.id && m.kind !== 'start' && m.kind !== 'stop' && up[m.id]) hasUpstream = true;
});
inputs.forEach(function (p, i) {
h += '<div class="orch-io-portbox">'
+ '<div class="orch-io-port">'
+ '<input class="orch-input orch-io-name" value="' + escapeHtml(p.name || '') + '" '
+ 'placeholder="name" oninput="_orchIoSet(\'inputs\',' + i + ',\'name\',this.value)">'
+ '<select class="orch-input orch-io-type" onchange="_orchIoSet(\'inputs\',' + i + ',\'type\',this.value)">'
+ typeOpts(p.type || 'text') + '</select>'
+ '<button class="orch-io-del" title="' + escapeHtml(t('orch.io.removePort')) + '" '
+ 'onclick="_orchIoRemove(\'inputs\',' + i + ')">✕</button></div>';
h += '<div class="orch-io-fromrow"><span class="orch-io-fromlbl">' + escapeHtml(t('orch.io.fromLabel')) + '</span>'
+ '<select class="orch-input orch-io-from" onchange="_orchIoSet(\'inputs\',' + i + ',\'from\',this.value)">'
+ _orchIoFromOptions(n, p.from) + '</select></div>';
h += '</div>';
});
if (inputs.length && !hasUpstream) {
h += '<div class="orch-io-empty">' + escapeHtml(t('orch.io.noUpstream')) + '</div>';
}
h += '<button class="orch-btn orch-btn-ghost orch-io-add" onclick="_orchIoAdd(\'inputs\')">'
+ _ORCH_ICONS.plus + ' ' + escapeHtml(t('orch.io.addInput')) + '</button>';
if (n.type === 'role') {
h += '<div class="orch-io-subhint">' + escapeHtml(t('orch.io.presetHint')) + '</div>';
h += '<button class="orch-btn orch-btn-ghost orch-io-preset" onclick="_orchIoToolHeavyPreset()">'
+ escapeHtml(t('orch.io.toolHeavyPreset')) + '</button>';
}
return h;
}
function _orchUpstreamIds(id) {
var seen = {};
var stack = [id];
while (stack.length) {
var cur = stack.pop();
_orchEdges.forEach(function (e) {
if (e.to === cur && !seen[e.from]) { seen[e.from] = true; stack.push(e.from); }
});
}
return seen;
}
function _orchIoFromOptions(self, cur) {
var up = _orchUpstreamIds(self.id);
var opts = [['', t('orch.edge.bindNone')], ['start', t('orch.io.fromStart')]];
var curStillListed = !cur || cur === 'start';
_orchNodes.forEach(function (m) {
if (m.id === self.id || m.kind === 'start' || m.kind === 'stop') return;
if (!up[m.id]) return;
var outs = _orchNodeOutputs(m);
var lbl = m.name || _orchAutoLabel(m);
outs.forEach(function (op) {
var ref = (op.name === 'text' && outs.length === 1) ? m.id : (m.id + '.' + op.name);
opts.push([ref, lbl + ' · ' + op.name]);
if (ref === cur) curStillListed = true;
});
});
if (!curStillListed) {
var sid = (cur.indexOf('.') !== -1) ? cur.slice(0, cur.indexOf('.')) : cur;
var sm = _orchFind(sid);
var slbl = sm ? (sm.name || _orchAutoLabel(sm)) : sid;
opts.push([cur, t('orch.io.fromStale', { node: slbl })]);
}
return opts.map(function (p) {
return '<option value="' + escapeHtml(p[0]) + '"' + (p[0] === cur ? ' selected' : '') + '>' + escapeHtml(p[1]) + '</option>';
}).join('');
}
function _orchIoEnsure(n) {
n.params.io = n.params.io || {};
return n.params.io;
}
function _orchIoAdd(side) {
var n = _orchFind(_orchSel);
if (!n) return;
var io = _orchIoEnsure(n);
io[side] = io[side] || [];
var base = side === 'outputs' ? 'out' : 'in';
io[side].push({ name: base + (io[side].length + 1), type: 'text' });
_orchRenderInspector();
_orchRenderNodes();
}
function _orchIoRemove(side, i) {
var n = _orchFind(_orchSel);
if (!n || !n.params.io || !n.params.io[side]) return;
n.params.io[side].splice(i, 1);
if (!n.params.io[side].length) delete n.params.io[side];
if (n.params.io && !n.params.io.inputs && !n.params.io.outputs) delete n.params.io;
_orchRenderInspector();
_orchRenderNodes();
}
function _orchIoSet(side, i, key, value) {
var n = _orchFind(_orchSel);
if (!n || !n.params.io || !n.params.io[side] || !n.params.io[side][i]) return;
if (key === 'from' && !value) delete n.params.io[side][i].from;
else n.params.io[side][i][key] = value;
if (key !== 'name') _orchRenderNodes();
}
function _orchIoToolHeavyPreset() {
var n = _orchFind(_orchSel);
if (!n) return;
var io = _orchIoEnsure(n);
io.outputs = [{ name: 'summary', type: 'text' }, { name: 'changes', type: 'artifact' }];
_orchRenderInspector();
_orchRenderNodes();
}
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
function _orchFlushToRoot() {
while (_orchStack.length) _orchExitGroup();
}
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
var gn = null;
for (var k = 0; k < frame.nodes.length; k++) {
if (frame.nodes[k].id === frame.groupId) { gn = frame.nodes[k]; break; }
}
var lbl = (gn && (gn.name || _orchAutoLabel(gn))) || t('orch.group.defaultLabel');
parts.push('<span class="orch-crumb-sep">\u203a</span>');
parts.push('<button class="orch-crumb-item" onclick="_orchCrumbTo(' + (i + 1) + ')">'
+ escapeHtml(lbl) + '</button>');
});
el.innerHTML = parts.join('');
}
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
var parts = '<defs><marker id="orchArrow" viewBox="0 0 12 12" refX="9.5" refY="6" '
+ 'markerWidth="8" markerHeight="8" orient="auto-start-reverse">'
+ '<path class="orch-edge-arrow" d="M1 1 L11 6 L1 11 L4 6 Z"></path></marker></defs>';
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
var selCls = (_orchSelEdge === e.id) ? ' is-selected' : '';
parts += '<path class="orch-edge-hit" d="' + _orchBezier(a, b) + '" '
+  'onclick="_orchSelectEdge(\'' + e.id + '\')"></path>';
parts += '<path class="orch-edge-path' + selCls + '" marker-end="url(#orchArrow)" d="' + _orchBezier(a, b) + '" '
+  'onclick="_orchSelectEdge(\'' + e.id + '\')"><title>'
+  escapeHtml(t('orch.edge.clickTip')) + '</title></path>';
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
var dx = b.x - a.x, dy = b.y - a.y;
if (dy >= 30) {
var v = dy * 0.5;
return 'M ' + a.x + ' ' + a.y
+ ' C ' + a.x + ' ' + (a.y + v) + ' '
+ b.x + ' ' + (b.y - v) + ' '
+ b.x + ' ' + b.y;
}
var side = dx >= 0 ? 1 : -1;
var h = Math.max(70, Math.abs(dx) * 0.5);
var vv = Math.max(40, Math.abs(dy) * 0.5);
return 'M ' + a.x + ' ' + a.y
+ ' C ' + (a.x + side * h) + ' ' + (a.y + vv) + ' '
+ (b.x + side * h) + ' ' + (b.y - vv) + ' '
+ b.x + ' ' + b.y;
}
function _orchRenderInspector() {
var el = document.getElementById('orchInspector');
if (!el) return;
if (_orchSelEdge) {
var edge = _orchEdges.filter(function (e) { return e.id === _orchSelEdge; })[0];
if (edge) {
if (_orchIsMobile()) {
var esh = el.closest('.orch-shell');
if (esh) { esh.classList.add('orch-m-insp'); esh.classList.remove('orch-m-pal'); }
}
el.innerHTML = _orchRenderEdgeInspector(edge);
return;
}
_orchSelEdge = null;
}
var n = _orchSel ? _orchFind(_orchSel) : null;
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
var h = '<div class="orch-sheet-head orch-m-only"><span>' + _ORCH_ICONS.gear + ' ' + escapeHtml(_orchKindLabel(n)) + '</span>'
+ '<button class="orch-ai-clear" onclick="_orchCloseMobileInspector()" title="Close">✕</button></div>';
h += _orchInspHeader(n);
if (n.type === 'subflow') {
var _gd = (n.params && n.params.definition) || {};
h += '<button class="orch-btn orch-btn-primary orch-btn-block orch-insp-cta" '
+  'onclick="_orchEnterGroup(\'' + n.id + '\')">' + escapeHtml(t('orch.group.open'))
+  ' <span class="orch-insp-cta-sub">' + t('orch.group.summary', { n: (_gd.nodes || []).length, m: (_gd.edges || []).length }) + '</span></button>';
var _gIdentity = _orchLabelField(n)
+ _orchSelectFld(t('orch.fld.groupFace'), 'role', n.role,
[['general', 'General'], ['researcher', 'Researcher'], ['coder', 'Coder'],
['analyst', 'Analyst'], ['writer', 'Writer'], ['synthesizer', 'Synthesizer']]);
h += _orchSec('orch.sec.identity', _ORCH_ICONS.gear, true, _gIdentity);
var _gExec = _orchSelectFld(t('orch.fld.groupScope'), 'scope', (n.params.scope || 'isolated'),
[['isolated', t('orch.scope.isolated')], ['inline', t('orch.scope.inline')]])
+ _orchSelectFld(t('orch.fld.emits'), 'emits', n.params.emits,
[['', t('orch.emits.auto', { role: _orchDefaultEmits(n.role) })],
['assistant', t('orch.emits.assistant')],
['user', t('orch.emits.user')]]);
h += _orchSec('orch.sec.execution', _ORCH_ICONS.gear, false, _gExec, 'orch.note.group');
h += _orchSec('orch.sec.io', _ORCH_ICONS.package, false, _orchIoSectionBody(n), 'orch.io.note');
} else if (n.type === 'role') {
h += _orchSec('orch.sec.persona', _ORCH_ICONS.bot, true,
_orchPersonaSectionBody(n), 'orch.persona.note');
var _runBody = _orchRunTraceBody(n);
if (_runBody) {
h += _orchSec('orch.sec.lastRun', _ORCH_ICONS.rocket, true,
_runBody, 'orch.run.note');
}
var _exec = _orchLabelField(n)
+ _orchSelectFld(t('orch.fld.tier'), 'tier', n.params.tier,
[['light', t('orch.tier.light')], ['standard', t('orch.tier.standard')], ['heavy', t('orch.tier.heavy')]])
+ _orchSelectFld(t('orch.fld.context'), 'isolation', n.params.isolation,
[['fresh-context', t('orch.iso.fresh')], ['shared-context', t('orch.iso.shared')]])
+ _orchSelectFld(t('orch.fld.emits'), 'emits', n.params.emits,
[['', t('orch.emits.auto', { role: _orchDefaultEmits(n.role) })],
['assistant', t('orch.emits.assistant')],
['user', t('orch.emits.user')]]);
h += _orchSec('orch.sec.execution', _ORCH_ICONS.gear, true, _exec, 'orch.note.exec');
h += _orchSec('orch.sec.io', _ORCH_ICONS.package, true,
_orchIoSectionBody(n), 'orch.io.note');
} else {
var _c = _orchLabelField(n);
var _hint = null;
if (n.kind === 'loop') {
_c += _orchNumFld(t('orch.fld.maxIter'), 'max_iterations', n.params.max_iterations)
+ _orchSelectFld(t('orch.fld.stopWhen'), 'stop_condition', n.params.stop_condition,
[['verdict:STOP', t('orch.stop.verdict')], ['no_new_findings', t('orch.stop.noNew')], ['max_only', t('orch.stop.maxOnly')]])
+ _orchSelectFld(t('orch.fld.verifier'), 'verifier', n.params.verifier,
[['critic', t('orch.verifier.critic')], ['reviewer', t('orch.verifier.reviewer')], ['none', t('orch.verifier.none')]]);
_hint = 'orch.note.loop';
} else if (n.kind === 'parallel') {
_c += _orchNumFld(t('orch.fld.maxConcurrent'), 'max_concurrent', n.params.max_concurrent)
+ _orchCheckFld(t('orch.fld.perItem'), 'per_item', n.params.per_item);
} else if (n.kind === 'branch') {
_c += _orchSelectFld(t('orch.fld.classifier'), 'classifier', n.params.classifier,
[['router', t('orch.classifier.router')], ['analyst', t('orch.classifier.analyst')], ['general', t('orch.classifier.general')]])
+ _orchNumFld(t('orch.fld.branchCount'), 'branches', n.params.branches);
} else if (n.kind === 'artifact') {
_c += '<label class="orch-fld"><span>' + escapeHtml(t('orch.fld.filePath')) + '</span>'
+  '<input class="orch-input" value="' + escapeHtml(n.params.path || '') + '" '
+  'placeholder="' + escapeHtml(t('orch.fld.filePathPh')) + '" '
+  'oninput="_orchSetParam(\'path\', this.value)"></label>'
+ _orchSelectFld(t('orch.fld.artifactKind'), 'format', n.params.format,
[['file', t('orch.afmt.file')], ['report', t('orch.afmt.report')], ['dataset', t('orch.afmt.dataset')],
['code', t('orch.afmt.code')], ['image', t('orch.afmt.image')]])
+ '<label class="orch-fld"><span>' + escapeHtml(t('orch.fld.description')) + '</span>'
+  '<textarea class="orch-input orch-ta" rows="3" '
+  'placeholder="' + escapeHtml(t('orch.fld.artifactDescPh')) + '" '
+  'oninput="_orchSetParam(\'description\', this.value)">' + escapeHtml(n.params.description || '') + '</textarea></label>';
_hint = 'orch.note.artifact';
} else if (n.kind === 'human') {
_c += _orchSelectFld(t('orch.fld.humanMode'), 'mode', n.params.mode,
[['approve', t('orch.hmode.approve')],
['input', t('orch.hmode.input')],
['notify', t('orch.hmode.notify')]])
+ '<label class="orch-fld"><span>' + escapeHtml(t('orch.fld.prompt')) + '</span>'
+  '<textarea class="orch-input orch-ta" rows="3" '
+  'placeholder="' + escapeHtml(t('orch.fld.promptPh')) + '" '
+  'oninput="_orchSetParam(\'prompt\', this.value)">' + escapeHtml(n.params.prompt || '') + '</textarea></label>';
if (n.params.mode === 'approve') {
_c += _orchNumFld(t('orch.fld.approveTimeout'), 'timeout_sec', n.params.timeout_sec);
}
_hint = 'orch.note.human';
} else if (n.kind === 'start') {
_c += '<label class="orch-fld"><span>' + escapeHtml(t('orch.fld.startInput')) + '</span>'
+  '<textarea class="orch-input orch-ta" rows="5" '
+  'placeholder="' + escapeHtml(t('orch.fld.startInputPh')) + '" '
+  'oninput="_orchSetParam(\'seed\', this.value)">' + escapeHtml((n.params && n.params.seed) || '') + '</textarea></label>';
_hint = 'orch.note.start';
} else if (n.kind === 'stop') {
_hint = 'orch.note.stop';
}
h += _orchSec('orch.sec.flow', _ORCH_ICONS.package, true,
_orchFlowSummaryBody(n), 'orch.flow.note');
h += _orchSec('orch.sec.settings', _ORCH_ICONS.gear, true, _c, _hint);
}
var ins = _orchEdges.filter(function (e) { return e.to === n.id; });
var outs = _orchEdges.filter(function (e) { return e.from === n.id; });
h += '<div class="orch-insp-foot">';
h += '<div class="orch-conn-box"><div class="orch-conn-row">' + escapeHtml(t('orch.conn.in')) + ' <b>' + ins.length + '</b></div>'
+  '<div class="orch-conn-row">' + escapeHtml(t('orch.conn.out')) + ' <b>' + outs.length + '</b> →</div></div>';
h += '<button class="orch-btn orch-btn-danger orch-btn-block" onclick="_orchDeleteNode(\'' + n.id + '\')">' + escapeHtml(t('orch.btn.deleteNode')) + '</button>';
h += '</div>';
el.innerHTML = h;
}
function _orchLabelField(n) {
return '<label class="orch-fld"><span>' + escapeHtml(t('orch.fld.label')) + '</span>'
+ '<input class="orch-input" value="' + escapeHtml(n.name) + '" '
+ 'placeholder="' + escapeHtml(_orchAutoLabel(n)) + '" '
+ 'oninput="_orchSetParam(\'name\', this.value)"></label>';
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
+ '<span>' + escapeHtml(label) + '</span>'
+ '<span class="stg-toggle stg-dv-toggle">'
+ '<input type="checkbox"' + (val ? ' checked' : '') + ' onchange="_orchSetParam(\'' + key + '\', this.checked)">'
+ '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>'
+ '</span></label>';
}
function _orchSetParam(key, value, isNum, kind) {
var n = _orchFind(_orchSel);
if (!n) return;
if (key === 'name') { n.name = value; _orchRenderNodes(); return; }
if (key === 'role') {
n.role = value;
_orchRenderNodes();
_orchRenderInspector();
return;
}
if (kind === 'list') {
var items = String(value == null ? '' : value).split('\n')
.map(function (s) { return s.trim(); })
.filter(function (s) { return s; });
if (items.length) { n.params[key] = items; } else { delete n.params[key]; }
_orchRenderNodes();
return;
}
if (isNum) value = (value === '' ? '' : Number(value));
if (value === '') {
delete n.params[key];
} else {
n.params[key] = value;
}
_orchRenderNodes();
if (key === 'mode') _orchRenderInspector();
}
function _orchDefaultEmits(role) {
return (role === 'critic' || role === 'reviewer' || role === 'virtual_user')
? 'user' : 'assistant';
}
var _orchRolePersonas = null;
var _ORCH_PERSONA_FALLBACK = {
planner: { tier: 'heavy', prompt: 'You are the PLANNER. Rewrite the request into a structured brief with a Goal, a concrete Checklist of steps, and Acceptance Criteria.' },
worker: { tier: 'heavy', prompt: 'You are the WORKER. Execute the plan against the checklist. Your first tool call must be state-changing — act, do not merely analyze.' },
critic: { tier: 'heavy', prompt: 'You are the CRITIC. Review the worker output against the plan and emit exactly one verdict tag: [VERDICT: STOP] or [VERDICT: CONTINUE_WORKER].' },
virtual_user: { tier: 'standard', prompt: 'You are a VIRTUAL USER standing in for the human. Reply briefly to keep the task moving, and emit [VU: TASK_DONE] when it is clearly complete.' },
};
function _orchRolePersona(role) {
var src = _orchRolePersonas || _ORCH_PERSONA_FALLBACK;
return (src && src[role]) || null;
}
function _orchRunTraceBody(n) {
var tr = _orchRunTrace[n.id];
if (!tr) return null;
var statusLbl = { running: t('orch.run.statusRunning'),
done: t('orch.run.statusDone'),
error: t('orch.run.statusError') }[tr.status] || tr.status;
var h = '<div class="orch-runtrace-row"><span class="orch-runtrace-lbl">'
+ escapeHtml(t('orch.run.status')) + '</span>'
+ '<span class="orch-runtrace-status orch-runtrace-' + escapeHtml(tr.status || '')
+ '">' + escapeHtml(statusLbl) + '</span></div>';
if (typeof tr.state_changing === 'number') {
h += '<div class="orch-runtrace-row"><span class="orch-runtrace-lbl">'
+ escapeHtml(t('orch.run.actions')) + '</span><span>' + tr.state_changing + '</span></div>';
}
var out = (tr.output || '').trim();
if (out) {
h += '<div class="orch-runtrace-lbl orch-runtrace-outlbl">'
+ escapeHtml(t('orch.run.output')) + '</div>'
+ '<pre class="orch-runtrace-out">' + escapeHtml(out.slice(0, 4000)) + '</pre>';
} else if (tr.status === 'running') {
h += '<div class="orch-runtrace-waiting">' + escapeHtml(t('orch.run.streaming')) + '</div>';
}
return h;
}
function _orchPersonaSectionBody(n) {
var persona = _orchRolePersona(n.role);
if (!persona) {
return '<div class="orch-persona-empty">' + escapeHtml(t('orch.persona.none')) + '</div>';
}
var h = '';
if (persona.prompt) {
h += '<div class="orch-persona-lbl orch-persona-promptlbl">'
+ escapeHtml(t('orch.persona.prompt')) + '</div>'
+ '<pre class="orch-persona-prompt" readonly>' + escapeHtml(persona.prompt) + '</pre>';
}
return h;
}
function _orchFlowSummaryBody(n) {
var ins = _orchEdges.filter(function (e) { return e.to === n.id; })
.map(function (e) { var m = _orchFind(e.from); return m ? (m.name || _orchAutoLabel(m)) : e.from; });
var outs = _orchEdges.filter(function (e) { return e.from === n.id; })
.map(function (e) { var m = _orchFind(e.to); return m ? (m.name || _orchAutoLabel(m)) : e.to; });
var inText, outText;
if (n.kind === 'start') {
var seed = ((n.params && n.params.seed) || '').trim();
inText = seed ? t('orch.flow.seedSet') : t('orch.flow.fromUser');
} else {
inText = ins.length ? ins.map(escapeHtml).join(', ') : t('orch.flow.none');
}
if (n.kind === 'stop') {
outText = t('orch.flow.toChat');
} else {
outText = outs.length ? outs.map(escapeHtml).join(', ') : t('orch.flow.none');
}
var h = '<div class="orch-flow-row"><span class="orch-flow-arrow">\u2192</span>'
+ '<span class="orch-flow-lbl">' + escapeHtml(t('orch.flow.in')) + '</span>'
+ '<span class="orch-flow-val">' + inText + '</span></div>'
+ '<div class="orch-flow-row"><span class="orch-flow-arrow">\u2190</span>'
+ '<span class="orch-flow-lbl">' + escapeHtml(t('orch.flow.out')) + '</span>'
+ '<span class="orch-flow-val">' + outText + '</span></div>';
var carry = t('orch.flow.carry.' + n.kind);
if (carry && carry !== 'orch.flow.carry.' + n.kind) {
h += '<div class="orch-flow-carry">' + carry + '</div>';
}
return h;
}
async function _orchFetchRoleSchema() {
if (_orchRolePersonas || typeof Api === 'undefined' || !Api.orchestrations
|| !Api.orchestrations.roleSchema) return;
try {
var res = await Api.orchestrations.roleSchema();
if (res && res.ok) {
if (res.personas && typeof res.personas === 'object') _orchRolePersonas = res.personas;
if (Array.isArray(res.ioTypes) && res.ioTypes.length) _ORCH_IO_TYPES = res.ioTypes;
if (_orchSel) _orchRenderInspector();
}
} catch (e) {
if (typeof console !== 'undefined') console.warn('role-schema fetch failed', e);
}
}
function _orchOnRename(v) { _orchName = v || 'Untitled Flow'; }
var _orchAiHistory = [];
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
if (!result.ok && result.validation && result.validation.errors && result.validation.errors.length) {
reply += '\n' + result.validation.errors.slice(0, 3).join('; ');
}
_orchAiHistory.push({ role: 'assistant', content: reply });
_orchRenderAiLog();
if (result.ok && result.definition) {
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
var _orchRunTaskId = null;
var _orchRunPolling = false;
var _orchRunTrace = {};
function _orchResetRunTrace() {
_orchRunTrace = {};
document.querySelectorAll('.orch-node[data-run-status]').forEach(function (el) {
el.removeAttribute('data-run-status');
});
}
function _orchSetNodeRunStatus(nodeId, status) {
if (!nodeId) return;
var el = document.getElementById('orch-node-' + nodeId);
if (el) el.setAttribute('data-run-status', status);
if (_orchSel === nodeId) _orchRenderInspector();
}
function _orchStartSeed() {
var st = _orchNodes.filter(function (n) { return n.kind === 'start'; })[0];
return (st && st.params && st.params.seed) ? String(st.params.seed) : '';
}
function _orchOpenRun() {
var d = document.getElementById('orchRunDrawer');
if (d) d.classList.add('is-open');
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
_orchResetRunTrace();
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
if (ev.node_id) {
_orchRunTrace[ev.node_id] = {
node_id: ev.node_id, role: ev.role, name: ev.name || ev.role,
status: 'running', output: '', preview: '', emits: ev.emits || '',
isolation: ev.isolation || '',
};
_orchSetNodeRunStatus(ev.node_id, 'running');
}
_orchRunLog(_ORCH_ICONS.bot + ' <b>' + escapeHtml(ev.name || ev.role) + '</b> running…', 'is-active'); break;
case 'step_delta':
if (ev.node_id && _orchRunTrace[ev.node_id] && ev.kind !== 'thinking') {
_orchRunTrace[ev.node_id].output += (ev.chunk || '');
if (_orchSel === ev.node_id) _orchRenderInspector();
}
break;
case 'step_complete':
if (ev.node_id) {
var _tr = _orchRunTrace[ev.node_id] || (_orchRunTrace[ev.node_id] = { node_id: ev.node_id });
_tr.role = ev.role; _tr.status = (ev.status === 'failed') ? 'error' : 'done';
_tr.output = (ev.output != null) ? ev.output : (_tr.output || ev.preview || '');
_tr.preview = ev.preview || (_tr.output || '').slice(0, 120);
_tr.state_changing = ev.state_changing || 0;
if (ev.emits) _tr.emits = ev.emits;
_orchSetNodeRunStatus(ev.node_id, _tr.status);
}
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
function _orchToggleTplMenu(forceClose) {
var m = document.getElementById('orchTplMenu');
if (!m) return;
m.style.display = (forceClose || m.style.display !== 'none') ? 'none' : 'block';
}
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
_orchOpenLoadMenu(); _orchOpenLoadMenu();
} else { _orchToast('Delete failed', true); }
}
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
function _orchToast(text, isErr) {
var el = document.createElement('div');
el.className = 'orch-toast' + (isErr ? ' is-err' : '');
el.textContent = text;
document.body.appendChild(el);
setTimeout(function () { el.style.opacity = '0'; setTimeout(function () { el.remove(); }, 300); }, 2600);
}
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
.orch-run-hint{font-size:11.5px;line-height:1.55;color:var(--text-tertiary);background:var(--bg-tertiary);border:1px solid var(--border);border-radius:var(--orch-r-sm,7px);padding:8px 10px;display:flex;gap:7px;align-items:flex-start}
.orch-run-hint svg{width:14px;height:14px;flex-shrink:0;margin-top:1px;opacity:.7}
.orch-run-hint b{color:var(--text-secondary);font-weight:700}
.orch-run-actions{display:flex;gap:8px;flex-wrap:wrap}
.orch-run-actions .orch-btn{flex:1;min-width:104px;justify-content:center}
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
.orch-edge-path:hover{stroke:var(--accent);stroke-width:2.75}
.orch-edge-path.is-selected{stroke:var(--accent);stroke-width:3}
.orch-edge-hit{fill:none;stroke:transparent;stroke-width:14;stroke-linecap:round;pointer-events:stroke;cursor:pointer}
.orch-edge-arrow{fill:color-mix(in srgb,var(--accent) 55%,var(--border-light));stroke:none}
.orch-edge-temp{fill:none;stroke:var(--accent);stroke-width:2;stroke-dasharray:5 4;stroke-linecap:round;pointer-events:none;opacity:.85}
.orch-edge-flow{padding:8px 10px;background:var(--bg-tertiary,var(--bg-secondary));border-radius:var(--orch-r-md);margin:6px 0;font-size:13px;text-align:center}
.orch-edge-arrowtxt{color:var(--accent);margin:0 4px}
.orch-edge-btns{margin-top:10px;display:flex;flex-direction:column;gap:6px}
.orch-io-head{font-size:11px;text-transform:uppercase;letter-spacing:.04em;opacity:.6;margin:8px 0 4px}
.orch-io-implicit{font-size:12px;opacity:.6;font-style:italic;padding:2px 0}
.orch-io-port{display:flex;gap:5px;align-items:center;margin-bottom:4px}
.orch-io-name{flex:1 1 auto;min-width:0}
.orch-io-type{flex:0 0 84px}
.orch-io-from{width:100%;margin:0 0 6px;font-size:12px}
.orch-io-del{flex:0 0 auto;background:none;border:none;color:var(--error-text);cursor:pointer;font-size:13px;padding:2px 5px;border-radius:4px}
.orch-io-del:hover{background:var(--accent-subtle)}
.orch-io-add,.orch-io-preset{font-size:12px;padding:4px 8px;width:100%;margin-top:4px}
.orch-io-badge{color:var(--accent);font-variant-numeric:tabular-nums}
.orch-io-head-in{margin-top:15px}
.orch-io-subhint{font-size:10.5px;line-height:1.5;color:var(--text-tertiary);margin:0 0 8px}
.orch-io-portbox{border:1px solid var(--border-light);border-radius:var(--orch-r-sm);padding:7px 8px;margin-bottom:8px;background:var(--bg-primary)}
.orch-io-portbox .orch-io-port{margin-bottom:6px}
.orch-io-fromrow{display:flex;align-items:center;gap:6px}
.orch-io-fromlbl{flex:0 0 auto;font-size:10.5px;font-weight:600;color:var(--accent);white-space:nowrap}
.orch-io-fromrow .orch-io-from{margin:0;flex:1 1 auto}
.orch-io-empty{font-size:10.5px;line-height:1.5;color:var(--text-tertiary);background:var(--bg-tertiary);border:1px dashed var(--border-light);border-radius:var(--orch-r-sm);padding:8px 10px;margin-bottom:8px}
/* Read-only role persona (the fixed prompt design) */
.orch-persona-lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;font-weight:600;color:var(--text-tertiary)}
.orch-persona-promptlbl{margin:4px 0 5px}
.orch-persona-prompt{font-family:var(--font-mono,ui-monospace,monospace);font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-word;color:var(--text-secondary);background:var(--bg-tertiary);border:1px solid var(--border-light);border-radius:var(--orch-r-sm);padding:8px 10px;margin:0;max-height:240px;overflow:auto;cursor:default}
.orch-persona-empty{font-size:11.5px;line-height:1.5;color:var(--text-tertiary);font-style:italic;padding:2px 0}
/* Per-node run trace ("Last run") — the traceability overlay */
.orch-runtrace-row{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:12px}
.orch-runtrace-lbl{font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;font-weight:600;color:var(--text-tertiary)}
.orch-runtrace-status{font-size:11.5px;font-weight:700}
.orch-runtrace-running{color:var(--accent)}
.orch-runtrace-done{color:var(--success-text,#3fb950)}
.orch-runtrace-error{color:var(--error-text,#f7768e)}
.orch-runtrace-outlbl{margin:4px 0 5px}
.orch-runtrace-out{font-family:var(--font-mono,ui-monospace,monospace);font-size:11px;line-height:1.5;white-space:pre-wrap;word-break:break-word;color:var(--text-secondary);background:var(--bg-tertiary);border:1px solid var(--border-light);border-radius:var(--orch-r-sm);padding:8px 10px;margin:0;max-height:300px;overflow:auto}
.orch-runtrace-waiting{font-size:11.5px;color:var(--text-tertiary);font-style:italic}
/* Canvas node run-status badge (live overlay during a run) */
.orch-node[data-run-status]::after{content:"";position:absolute;top:7px;right:9px;width:9px;height:9px;border-radius:50%;z-index:7}
.orch-node[data-run-status="running"]::after{background:var(--accent);box-shadow:0 0 0 3px var(--accent-subtle);animation:orch-run-pulse 1.1s ease-in-out infinite}
.orch-node[data-run-status="done"]::after{background:var(--success-text,#3fb950)}
.orch-node[data-run-status="error"]::after{background:var(--error-text,#f7768e)}
@keyframes orch-run-pulse{0%,100%{opacity:1}50%{opacity:.35}}
/* Read-only data-flow summary for control nodes */
.orch-flow-row{display:flex;align-items:baseline;gap:7px;margin-bottom:5px;font-size:12px}
.orch-flow-arrow{flex:0 0 auto;color:var(--accent);font-weight:700}
.orch-flow-lbl{flex:0 0 auto;font-size:10.5px;text-transform:uppercase;letter-spacing:.03em;font-weight:600;color:var(--text-tertiary)}
.orch-flow-val{flex:1 1 auto;color:var(--text-secondary);word-break:break-word}
.orch-flow-carry{font-size:11px;line-height:1.55;color:var(--text-tertiary);margin-top:7px;padding-top:7px;border-top:1px solid var(--border-light)}
.orch-flow-carry b{color:var(--text-secondary);font-weight:600}
.orch-nodes{position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none}
.orch-node{position:absolute;width:${_ORCH_CARD_W}px;background:var(--bg-secondary);border:1px solid var(--border-light);border-left:4px solid var(--node-accent,var(--accent));border-radius:var(--orch-r-md);box-shadow:var(--orch-elev-card);user-select:none;pointer-events:auto;transition:box-shadow .15s,border-color .15s,transform .12s ease}
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
.orch-insp-head{display:flex;align-items:center;gap:11px;margin-bottom:10px}
.orch-insp-avatar{width:38px;height:38px;flex:0 0 38px;border-radius:var(--orch-r-md);object-fit:cover;background:var(--bg-tertiary)}
.orch-insp-glyph{display:inline-flex;align-items:center;justify-content:center;color:var(--node-accent,var(--accent));background:color-mix(in srgb,var(--node-accent,var(--accent)) 14%,var(--bg-secondary))}
.orch-insp-glyph svg{width:20px;height:20px}
.orch-insp-htext{display:flex;flex-direction:column;gap:2px;min-width:0}
.orch-insp-kind{font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--accent)}
.orch-insp-type{font-size:16px;font-weight:700;color:var(--text-primary);line-height:1.2;word-break:break-word}
.orch-insp-blurb{font-size:11.5px;line-height:1.5;color:var(--text-tertiary);margin-bottom:14px}
.orch-insp-cta{margin-bottom:12px;flex-direction:column;gap:2px;line-height:1.3}
.orch-insp-cta-sub{font-size:10.5px;font-weight:500;opacity:.8}
/* Collapsible inspector sections */
.orch-sec{border:1px solid var(--border-light);border-radius:var(--orch-r-md);margin-bottom:8px;background:var(--bg-primary)}
.orch-sec[open]{background:transparent}
.orch-sec-sum{cursor:pointer;list-style:none;display:flex;align-items:center;gap:7px;padding:10px 11px;font-size:12px;font-weight:700;color:var(--text-primary);user-select:none}
.orch-sec-sum::-webkit-details-marker{display:none}
.orch-sec-sum .orch-ico{width:14px;height:14px;opacity:.7;flex:0 0 auto}
.orch-sec-sum>span:first-of-type{flex:1}
.orch-sec-chev{transition:transform .18s ease;opacity:.5;font-size:15px}
.orch-sec[open] .orch-sec-chev{transform:rotate(90deg)}
.orch-sec-body{padding:2px 11px 12px}
.orch-sec-hint{font-size:11px;line-height:1.55;color:var(--text-tertiary);margin-bottom:11px}
.orch-sec-hint b{color:var(--text-secondary);font-weight:600}
.orch-sec-hint code{font-size:10.5px;background:var(--bg-tertiary);padding:1px 4px;border-radius:3px}
.orch-insp-foot{margin-top:14px;padding-top:12px;border-top:var(--orch-rail)}
.orch-fld{display:block;margin-bottom:13px}
.orch-fld>span{display:block;font-size:11px;font-weight:600;color:var(--text-secondary);margin-bottom:5px}
.orch-fld-check{display:flex;align-items:center;justify-content:space-between;gap:8px}
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
;
// ═══ task-mode.js ═══
var _tmModalReady = false;
var _tmRunId = null;
var _tmPolling = false;
var _tmRuns = [];
var _tmLoadError = false;
var _tmPollErrs = 0;
var _tmDef = null;
var _tmActiveNode = null;
var _tmDoneNodes = {};
var _tmGates = {};
var _tmTrace = {};
var _tmTraceCount = {};
var _tmSelNode = null;
function _tmIco(name) {
return (typeof _ORCH_ICONS !== 'undefined' && _ORCH_ICONS[name]) || '';
}
function _tmT(key, params) {
return (typeof t === 'function') ? t(key, params) : key;
}
function _tmRoleDef(role) {
if (typeof _ORCH_ROLES === 'undefined') return null;
return _ORCH_ROLES.filter(function (r) { return r.role === role; })[0] || null;
}
function _tmControlDef(kind) {
if (typeof _ORCH_CONTROLS === 'undefined') return null;
return _ORCH_CONTROLS.filter(function (c) { return c.kind === kind; })[0] || null;
}
function _tmNodeAccent(n) {
if (n.type === 'role') return '#6e56cf';
if (n.type === 'subflow') return '#8b5cf6';
var cdef = _tmControlDef(n.kind);
return (cdef && cdef.accent) || 'var(--text-tertiary)';
}
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
function openTaskMode() {
_tmEnsureModal();
var ov = document.getElementById('taskModeModal');
if (ov) ov.style.display = 'flex';
_tmRefreshRuns();
}
function closeTaskMode(evt) {
var ov = document.getElementById('taskModeModal');
if (!ov) return;
if (evt && evt.target !== ov) return;
ov.style.display = 'none';
_tmRunId = null;
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
async function _tmRefreshRuns() {
var list = document.getElementById('tmRunList');
if (list && !_tmRuns.length) {
list.innerHTML = '<div class="tm-loading"><span class="tm-spin"></span>' + _tmT('tm.loading') + '</div>';
}
var res = await Api.orchestrations.taskList();
if (res === null) { _tmLoadError = true; _tmRenderRunList(); return; }
_tmLoadError = false;
_tmRuns = (res && res.ok && res.runs) || [];
_tmRenderRunList();
}
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
function _tmOpenStudio(orchId) {
if (typeof openOrchestration !== 'function') {
if (typeof _orchToast === 'function') _orchToast(_tmT('tm.studioUnavailable'), true);
return;
}
closeTaskMode();
openOrchestration();
if (orchId && typeof _orchLoadFromStore === 'function') {
_orchLoadFromStore(orchId);
}
}
function _tmStatusChip(status) {
var s = status || 'pending';
var lbl = _tmT('tm.status.' + s);
if (lbl === 'tm.status.' + s) lbl = s;
return '<span class="tm-chip tm-chip-' + _tmEsc(s) + '" title="' + _tmEsc(s) + '">' + _tmEsc(lbl) + '</span>';
}
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
_tmRenderRunList();
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
if (_tmRunId !== runId) return;
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
if (_tmRunId !== runId || !_tmPolling) return;
if (typeof document !== 'undefined' && document.hidden) {
setTimeout(function () { _tmPoll(runId, cursor); }, 1500);
return;
}
var res = await Api.orchestrations.taskEvents(runId, cursor);
if (_tmRunId !== runId || !_tmPolling) return;
if (!res || !res.ok) {
_tmPollErrs++;
if (_tmPollErrs === 1) _tmLine(_tmIco('loop') + ' ' + _tmT('tm.line.reconnecting'), 'is-err');
if (_tmPollErrs > 12) {
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
_tmRenderTitle(run);
var fin = document.getElementById('tmFinal');
if (fin && run.final) {
fin.style.display = '';
fin.innerHTML = '<div class="tm-final-label">' + _tmT('tm.final.result') + '</div>'
+ '<pre class="tm-final-pre">' + _tmEsc(run.final.slice(0, 8000)) + '</pre>';
}
}
var _TM_NODE_W = 168;
var _TM_NODE_H = 56;
function _tmRenderGraph() {
var host = document.getElementById('tmGraph');
if (!host) return;
var nodes = (_tmDef && _tmDef.nodes) || [];
if (!nodes.length) { host.style.display = 'none'; host.innerHTML = ''; return; }
host.style.display = '';
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
var W = Math.max(1, Math.round(vw)), H = Math.max(1, Math.round(vh));
host.innerHTML = '<svg class="tm-graph-svg" width="' + W + '" height="' + H
+ '" viewBox="0 0 ' + W + ' ' + H + '">' + parts + '</svg>';
}
function _tmLine(html, cls) {
var tl = document.getElementById('tmTimeline');
if (!tl) return;
var atBottom = (tl.scrollHeight - tl.scrollTop - tl.clientHeight) < 40;
var row = document.createElement('div');
row.className = 'tm-line' + (cls ? ' ' + cls : '');
row.innerHTML = html;
tl.appendChild(row);
if (atBottom) tl.scrollTop = tl.scrollHeight;
}
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
if (ev.node_id) {
_tmTrace[ev.node_id] = ev;
_tmTraceCount[ev.node_id] = (_tmTraceCount[ev.node_id] || 0) + 1;
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
function _tmSelectNode(nodeId) {
_tmSelNode = (_tmSelNode === nodeId) ? null : (nodeId || null);
_tmRenderGraph();
_tmRenderInspector();
}
function _tmRenderInspector(stepEv) {
var body = document.getElementById('tmInspBody');
if (!body) return;
var html = '';
var gateIds = Object.keys(_tmGates);
if (gateIds.length) {
gateIds.forEach(function (rid) {
html += _tmGateCard(_tmGates[rid]);
});
}
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
gateIds.forEach(function (rid) {
var inp = document.getElementById('tmGateInput-' + rid);
if (inp) inp.addEventListener('keydown', function (e) {
if (e.key === 'Enter') { e.preventDefault(); _tmHumanInput(rid); }
});
});
}
function _tmTraceDetail(tr, stepEv) {
if (!tr) {
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
function _tmInjectStyles() {
if (document.getElementById('tmStyles')) return;
var st = document.createElement('style');
st.id = 'tmStyles';
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
;
// ═══ paper-reader.js ═══
var paperMode = false;
var _paperDescribeDraft = '';
var _paperPdfUrl = '';
var _paperFileName = '';
var _paperParsedText = '';
var _paperArxivId = '';
var _paperPdfDoc = null;
var _paperTotalPages = 0;
var _paperScale = 1.5;
var _paperCurrentUrl = '';
var _paperViaData = false;
var _paperRenderToken = 0;
var _paperIntersectionObserver = null;
var _paperReopenInFlight = false;
function _paperNow() {
try { return (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now(); }
catch (_) { return Date.now(); }
}
var _paperActiveTab = 'qa';
var _paperReportCache = '';
var _paperReportMeta = null;
var _paperHash = '';
var _paperReportSnapshots = {};
var _REPORT_REGEN_INTENT_KEY = 'paper_report_regen_intent';
var _paperQAHistory = [];
var _paperLoading = false;
var _paperQAStreaming = false;
var _paperQAAbort = null;
var _paperQAAbortRequested = false;
var _paperReportModel = '';
var _paperImages = [];
var _paperPdfFilename = '';
var _paperSearchResults = [];
var _lastArxivSearchQuery = '';
var _paperReportStream = null;
var _paperReviewCache = '';
var _paperReviewMeta = null;
var _paperReviewStream = null;
var _paperReviewModel = '';
var _paperReviewVenue = '';
var _paperReviewVenues = [];
var _REVIEW_REGEN_INTENT_KEY = 'paper_review_regen_intent';
var _PAPER_REVIEW_VENUE_KEY = 'paper_review_venue_by_id';
var _paperReviewShowTranslation = false;
var _paperReviewTranslatedText = '';
var _paperReviewTranslating = false;
var _PAPER_REPORT_LANG_KEY = 'paper_report_lang_by_id';
var _PAPER_REVIEW_LANG_KEY = 'paper_review_lang_by_id';
var _PAPER_READ_POS_KEY = 'paper_read_pos_by_key';
function _reportView(kind) {
if (kind === 'review') {
return {
kind: 'review', idPrefix: 'review', containerId: 'paperReviewContent',
stopBtnId: 'paperReviewStopBtn', regenBtnId: 'paperReviewRegenBtn',
copyLabelId: 'paperReviewCopyLabel',
exportMenuId: 'paperReviewExportMenu', exportDropdownId: 'paperReviewExportDropdown',
modelDropdownId: 'paperReviewModelDropdown', modelLabelId: 'paperReviewModelLabel',
regenIntentKey: _REVIEW_REGEN_INTENT_KEY,
uiLang: function() { return 'en'; },
langKey: function() { return 'review:' + (_paperReviewVenue || 'generic') + ':' + this.uiLang(); },
get cache() { return _paperReviewCache; }, set cache(v) { _paperReviewCache = v; },
get meta() { return _paperReviewMeta; }, set meta(v) { _paperReviewMeta = v; },
get stream() { return _paperReviewStream; }, set stream(v) { _paperReviewStream = v; },
get model() { return _paperReviewModel; }, set model(v) { _paperReviewModel = v; },
};
}
return {
kind: 'report', idPrefix: 'report', containerId: 'paperReportContent',
stopBtnId: 'paperReportStopBtn', regenBtnId: 'paperReportRegenBtn',
copyLabelId: 'paperReportCopyLabel',
exportMenuId: 'paperReportExportMenu', exportDropdownId: 'paperReportExportDropdown',
modelDropdownId: 'paperReportModelDropdown', modelLabelId: 'paperReportModelLabel',
regenIntentKey: _REPORT_REGEN_INTENT_KEY,
uiLang: function() { return _activeReportLang(); },
langKey: function() { return this.uiLang(); },
get cache() { return _paperReportCache; }, set cache(v) { _paperReportCache = v; },
get meta() { return _paperReportMeta; }, set meta(v) { _paperReportMeta = v; },
get stream() { return _paperReportStream; }, set stream(v) { _paperReportStream = v; },
get model() { return _paperReportModel; }, set model(v) { _paperReportModel = v; },
};
}
function _readReportLangMap() {
try {
var raw = localStorage.getItem(_PAPER_REPORT_LANG_KEY);
return raw ? (JSON.parse(raw) || {}) : {};
} catch (e) {
console.warn('[Paper:Report] read lang map failed:', e);
return {};
}
}
function _activeReportLang() {
var uiDefault = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh') ? 'zh' : 'en';
if (!_activePaperId) return uiDefault;
var stored = _readReportLangMap()[_activePaperId];
return (stored === 'en' || stored === 'zh') ? stored : uiDefault;
}
function _persistReportLang(paperId, lang) {
if (!paperId || (lang !== 'en' && lang !== 'zh')) return;
try {
var map = _readReportLangMap();
map[paperId] = lang;
localStorage.setItem(_PAPER_REPORT_LANG_KEY, JSON.stringify(map));
} catch (e) {
console.warn('[Paper:Report] persist lang failed:', e);
}
}
var _PAPER_READER_PREFS_KEY = 'paper_reader_prefs';
var _READER_FONT_SCALES = [0.85, 0.925, 1.0, 1.1, 1.2, 1.3];
var _READER_DEFAULT_SCALE_IDX = 2;
var _READER_WIDTHS = [
{ px: 640, label: 'paper.readerWidthNarrow' },
{ px: 720, label: 'paper.readerWidthComfortable' },
{ px: 860, label: 'paper.readerWidthWide' },
];
var _READER_DEFAULT_WIDTH_IDX = 1;
function _readReaderPrefs() {
var scaleIdx = _READER_DEFAULT_SCALE_IDX, widthIdx = _READER_DEFAULT_WIDTH_IDX;
try {
var raw = localStorage.getItem(_PAPER_READER_PREFS_KEY);
if (raw) {
var o = JSON.parse(raw) || {};
if (typeof o.scaleIdx === 'number') scaleIdx = o.scaleIdx;
if (typeof o.widthIdx === 'number') widthIdx = o.widthIdx;
}
} catch (e) {
console.warn('[Paper:Reader] read prefs failed:', e);
}
scaleIdx = Math.max(0, Math.min(_READER_FONT_SCALES.length - 1, scaleIdx | 0));
widthIdx = Math.max(0, Math.min(_READER_WIDTHS.length - 1, widthIdx | 0));
return { scaleIdx: scaleIdx, widthIdx: widthIdx };
}
function _persistReaderPrefs(prefs) {
try {
localStorage.setItem(_PAPER_READER_PREFS_KEY, JSON.stringify({
scaleIdx: prefs.scaleIdx, widthIdx: prefs.widthIdx,
}));
} catch (e) {
console.warn('[Paper:Reader] persist prefs failed:', e);
}
}
function _applyReaderPrefs(prefs) {
prefs = prefs || _readReaderPrefs();
var scale = _READER_FONT_SCALES[prefs.scaleIdx];
var width = _READER_WIDTHS[prefs.widthIdx];
['paperReportContent', 'paperReviewContent'].forEach(function(id) {
var el = document.getElementById(id);
if (!el) return;
el.style.setProperty('--reader-font-scale', String(scale));
el.style.setProperty('--reader-measure', width.px + 'px');
});
var labelText = (typeof t === 'function') ? t(width.label) : width.label;
document.querySelectorAll('.paper-reader-width-label').forEach(function(sp) {
sp.textContent = labelText;
});
document.querySelectorAll('.paper-reader-set-dec').forEach(function(b) {
b.disabled = (prefs.scaleIdx <= 0);
});
return prefs;
}
function _readerFontStep(dir) {
var prefs = _readReaderPrefs();
var next = Math.max(0, Math.min(_READER_FONT_SCALES.length - 1, prefs.scaleIdx + (dir > 0 ? 1 : -1)));
if (next === prefs.scaleIdx) return;
prefs.scaleIdx = next;
_persistReaderPrefs(prefs);
_applyReaderPrefs(prefs);
}
function _readerWidthCycle() {
var prefs = _readReaderPrefs();
prefs.widthIdx = (prefs.widthIdx + 1) % _READER_WIDTHS.length;
_persistReaderPrefs(prefs);
_applyReaderPrefs(prefs);
}
if (typeof window !== 'undefined') {
window._readerFontStep = _readerFontStep;
window._readerWidthCycle = _readerWidthCycle;
window._applyReaderPrefs = _applyReaderPrefs;
}
function _reportSnapshotKey(view) {
view = view || _reportView('report');
return (_activePaperId || '') + '::' + view.langKey();
}
function _rememberReportSnapshot(view, report, meta) {
if (!report) return;
_paperReportSnapshots[_reportSnapshotKey(view)] = { report: report, meta: meta || null };
}
function _getReportSnapshot(view) {
return _paperReportSnapshots[_reportSnapshotKey(view)] || null;
}
function _resetReportSnapshots() {
_paperReportSnapshots = {};
}
function _syncReportLangToggle(view) {
view = view || _reportView('report');
var wrap = document.getElementById(view.idPrefix + 'LangToggle');
if (!wrap) return;
var cur = (view.kind === 'review') ? _activeReviewLang() : _activeReportLang();
wrap.querySelectorAll('.paper-report-lang-opt').forEach(function(btn) {
btn.classList.toggle('active', btn.dataset.lang === cur);
});
}
function _setReportLang(lang, kind) {
if (lang !== 'en' && lang !== 'zh') return;
var view = _reportView(kind || 'report');
if (view.kind === 'review') {
_setReviewLang(lang);
_syncReportLangToggle(view);
return;
}
var cur = _activeReportLang();
if (cur === lang) return;
if (view.cache) _rememberReportSnapshot(view, view.cache, view.meta);
if (_activePaperId) _persistReportLang(_activePaperId, lang);
_syncReportLangToggle(view);
_resetReportLocalState(view);
view.cache = '';
var snap = _getReportSnapshot(view);
if (snap) {
view.cache = snap.report;
view.meta = snap.meta || null;
if (_paperActiveTab === 'report') {
var c = document.getElementById(view.containerId);
if (c) _renderFinalReport(c, snap.report, undefined, view);
}
return;
}
if (_paperActiveTab === 'report') _loadOrGenerateReport(view);
}
var _paperLibrary = [];
var _paperLibraryLoading = false;
var _activePaperId = '';
var _PAPER_ACTIVE_KEY = 'paper_active_id';
var _PAPER_LEGACY_LIB_KEY = 'paper_library';
var _PAPER_MIGRATED_FLAG = 'paper_library_migrated_v1';
function _persistPaperEntry(entry, _first) {
if (!entry || !entry.id) return Promise.resolve();
var body = {
title: entry.title || '',
qaHistory: (entry.qaHistory || []).slice(-50),
babelCache: entry.babelCache || {},
pageCount: entry.pageCount || 0,
createdAt: entry.createdAt || Date.now(),
};
if (_first) {
body.pdfUrl = entry.pdfUrl || '';
body.pdfFilename = entry.pdfFilename || '';
body.arxivId = entry.arxivId || '';
body.paperHash = entry.paperHash || '';
body.parsedText = (entry.parsedText || '').slice(0, 200000);
body.images = Array.isArray(entry.images) ? entry.images.slice(0, 60) : [];
}
return Api.paper.libraryUpsert(entry.id, body)
.then(function(data) {
if (!data || !data.ok) {
console.warn('[Paper:Library] Upsert rejected:', data && data.error);
}
return data;
})
.catch(function(e) {
console.warn('[Paper:Library] Upsert failed:', e);
});
}
async function _migrateLegacyLibrary() {
if (localStorage.getItem(_PAPER_MIGRATED_FLAG)) return;
var raw = localStorage.getItem(_PAPER_LEGACY_LIB_KEY);
if (!raw) {
localStorage.setItem(_PAPER_MIGRATED_FLAG, '1');
return;
}
var legacy;
try { legacy = JSON.parse(raw); } catch (e) {
console.warn('[Paper:Library] Legacy bookshelf parse failed, discarding:', e);
localStorage.removeItem(_PAPER_LEGACY_LIB_KEY);
localStorage.setItem(_PAPER_MIGRATED_FLAG, '1');
return;
}
if (!Array.isArray(legacy) || legacy.length === 0) {
localStorage.removeItem(_PAPER_LEGACY_LIB_KEY);
localStorage.setItem(_PAPER_MIGRATED_FLAG, '1');
return;
}
debugLog('[Paper] Migrating ' + legacy.length + ' bookshelf entries to server…', 'info');
for (var i = 0; i < legacy.length; i++) {
try { await _persistPaperEntry(legacy[i], true); }
catch (e) { console.warn('[Paper:Library] Migrate entry failed:', e); }
}
localStorage.removeItem(_PAPER_LEGACY_LIB_KEY);
localStorage.setItem(_PAPER_MIGRATED_FLAG, '1');
debugLog('[Paper] Migration complete.', 'success');
}
async function _loadPaperLibrary() {
_activePaperId = localStorage.getItem(_PAPER_ACTIVE_KEY) || '';
try {
await _migrateLegacyLibrary();
var data = await Api.paper.libraryList();
if (data && data.ok && Array.isArray(data.papers)) {
_paperLibrary = data.papers;
for (var pi = 0; pi < _paperLibrary.length; pi++) _paperLibrary[pi]._persisted = true;
} else {
_paperLibrary = [];
console.warn('[Paper:Library] Unexpected server response:', data);
}
} catch (e) {
console.warn('[Paper:Library] Load failed, falling back to empty:', e);
_paperLibrary = [];
}
if (_activePaperId && !_paperLibrary.some(function(p) { return p.id === _activePaperId; })) {
_activePaperId = '';
localStorage.removeItem(_PAPER_ACTIVE_KEY);
}
}
function _setActivePaperId(id) {
_activePaperId = id || '';
if (_activePaperId) localStorage.setItem(_PAPER_ACTIVE_KEY, _activePaperId);
else localStorage.removeItem(_PAPER_ACTIVE_KEY);
}
function _newPaperEntryId() {
return 'paper_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
}
function _createPaperEntry(title, pdfUrl, parsedText, arxivId, explicitId) {
var entry = {
id: explicitId || _newPaperEntryId(),
title: title || 'Untitled Paper',
pdfUrl: pdfUrl || '',
pdfFilename: '',
arxivId: arxivId || '',
parsedText: parsedText || '',
qaHistory: [],
paperHash: '',
images: [],
babelCache: {},
createdAt: Date.now(),
pageCount: 0,
_persisted: false,
};
_paperLibrary.unshift(entry);
_setActivePaperId(entry.id);
return entry;
}
function _getActivePaperEntry() {
if (!_activePaperId) return null;
for (var i = 0; i < _paperLibrary.length; i++) {
if (_paperLibrary[i].id === _activePaperId) return _paperLibrary[i];
}
return null;
}
function _saveActivePaperState() {
var entry = _getActivePaperEntry();
if (!entry) return Promise.resolve();
entry.pdfUrl = _paperPdfUrl;
entry.pdfFilename = _paperPdfFilename || entry.pdfFilename || '';
entry.title = _paperFileName || entry.title;
entry.parsedText = _paperParsedText;
entry.arxivId = _paperArxivId;
entry.qaHistory = _paperQAHistory;
entry.paperHash = _paperHash || '';
entry.images = Array.isArray(_paperImages) ? _paperImages : [];
entry.babelCache = _babelTranslatedPages || {};
entry.pageCount = _paperTotalPages;
var first = !entry._persisted;
entry._persisted = true;
return _persistPaperEntry(entry, first);
}
function _deletePaperEntry(id) {
_paperLibrary = _paperLibrary.filter(function(p) { return p.id !== id; });
if (_activePaperId === id) {
_setActivePaperId(_paperLibrary.length > 0 ? _paperLibrary[0].id : '');
}
Api.paper.libraryDelete(id)
.catch(function(e) { console.warn('[Paper:Library] Delete failed:', e); });
_renderPaperLibrary();
if (paperMode) {
var next = _getActivePaperEntry();
if (next) {
_openPaperEntry(next);
} else {
_resetAllReportViews();
_paperPdfUrl = '';
_paperPdfFilename = '';
_paperFileName = '';
_paperParsedText = '';
_paperQAHistory = [];
_paperReportCache = '';
_paperReviewCache = '';
_paperReviewVenue = '';
_paperHash = '';
_paperImages = [];
_babelTranslatedPages = {};
_showPaperLanding();
_updatePaperTitles();
}
}
}
function _openPaperEntry(entry) {
_saveActivePaperState();
if (_paperQAAbort) { try { _paperQAAbort.abort(); } catch (_) {} _paperQAAbort = null; }
_resetAllReportViews();
_setActivePaperId(entry.id);
_paperPdfUrl = entry.pdfUrl || '';
_paperPdfFilename = entry.pdfFilename || '';
_paperFileName = entry.title || 'Untitled';
_paperParsedText = entry.parsedText || '';
_paperArxivId = entry.arxivId || '';
_paperQAHistory = entry.qaHistory || [];
_paperReportCache = '';
_paperReportMeta = null;
_paperReviewCache = '';
_paperReviewMeta = null;
_paperReviewVenue = '';
_paperHash = entry.paperHash || '';
_paperImages = Array.isArray(entry.images) ? entry.images : [];
_babelTranslatedPages = entry.babelCache || {};
_paperTotalPages = entry.pageCount || 0;
var _rcEl = document.getElementById('paperReportContent');
if (_rcEl) {
_rcEl.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Loading…</div></div>';
}
var _qaEl = document.getElementById('paperQAMessages');
if (_qaEl) _qaEl.innerHTML = '';
_updatePaperTitles();
_renderPaperLibrary();
if (_paperPdfUrl) {
_loadPaperPdf(_paperPdfUrl);
} else {
_showPaperLanding();
}
_switchPaperTab(_paperActiveTab || 'qa');
}
function _renderPaperLibrary() {
var listEl = document.getElementById('paperLibraryList');
if (!listEl) return;
var countEl = document.getElementById('paperLibCount');
if (countEl) countEl.textContent = String(_paperLibrary.length || '');
if (_paperLibraryLoading && _paperLibrary.length === 0) {
var _ttl = (typeof t === 'function') ? t : function(k){ return k; };
listEl.innerHTML =
'<div class="paper-lib-loading">' +
'<span class="paper-lib-loading-spinner"></span>' +
'<span>' + escapeHtml(_ttl('paper.loadingLibrary')) + '</span>' +
'</div>';
return;
}
if (_paperLibrary.length === 0) {
var _tte = (typeof t === 'function') ? t : function(k){ return k; };
listEl.innerHTML =
'<div class="paper-lib-empty">' +
'<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>' +
'<span>' + escapeHtml(_tte('paper.noPapersYet')) + '</span>' +
'<span class="paper-lib-empty-hint">' + escapeHtml(_tte('paper.noPapersHint')) + '</span>' +
'</div>';
return;
}
var html = '';
for (var i = 0; i < _paperLibrary.length; i++) {
var p = _paperLibrary[i];
var isActive = p.id === _activePaperId;
var dateStr = _formatPaperDate(p.createdAt);
var pageStr = p.pageCount ? p.pageCount + 'p' : '';
var hasReport = p.hasReport ? ' · report' : '';
html +=
'<div class="paper-lib-item' + (isActive ? ' active' : '') + '" data-id="' + p.id + '" onclick="_onPaperLibClick(\'' + p.id + '\')">' +
'<div class="paper-lib-item-icon">' +
'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>' +
'</div>' +
'<div class="paper-lib-item-info">' +
'<span class="paper-lib-item-title" title="' + escapeHtml(p.title) + '">' + escapeHtml(p.title) + '</span>' +
'<span class="paper-lib-item-meta">' + dateStr + (pageStr ? ' · ' + pageStr : '') + hasReport + '</span>' +
'</div>' +
'<button class="paper-lib-item-del" onclick="event.stopPropagation();_deletePaperEntry(\'' + p.id + '\')" title="' + escapeHtml((typeof t === 'function') ? t('paper.delete') : 'Delete') + '">' +
'<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
'</button>' +
'</div>';
}
listEl.innerHTML = html;
}
function _onPaperLibClick(id) {
for (var i = 0; i < _paperLibrary.length; i++) {
if (_paperLibrary[i].id === id) {
_openPaperEntry(_paperLibrary[i]);
return;
}
}
}
function _formatPaperDate(ts) {
if (!ts) return '';
var d = new Date(ts);
var now = new Date();
var diff = now.getTime() - d.getTime();
if (diff < 86400000) {
var h = d.getHours();
var m = d.getMinutes();
return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
}
if (diff < 86400000 * 7) {
return Math.floor(diff / 86400000) + 'd ago';
}
return (d.getMonth() + 1) + '/' + d.getDate();
}
async function enterPaperMode(pdfUrl, fileName, parsedText, arxivId) {
if (typeof imageGenMode !== 'undefined' && imageGenMode) exitImageGenMode();
paperMode = true;
var sidebar = document.getElementById('sidebar');
if (sidebar) {
sidebar.classList.add('paper-active');
if (sidebar.classList.contains('collapsed') && typeof toggleSidebar === 'function') toggleSidebar();
}
try { document.body.classList.add('paper-mode-active'); } catch (e) {  }
var container = document.getElementById('paperModeContainer');
var chatWrapper = document.querySelector('.chat-wrapper');
var inputArea = document.querySelector('.input-area');
if (container) container.style.display = 'flex';
if (chatWrapper) chatWrapper.style.display = 'none';
if (inputArea) inputArea.style.display = 'none';
var pmBtn = document.getElementById('paperModeBtn');
if (pmBtn) {
pmBtn.classList.add('active');
pmBtn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg><span class="topbar-tool-label">' + (typeof t === 'function' ? t('topbar.backToChat') : 'Back') + '</span>';
pmBtn.title = 'Back to Chat';
}
_paperLibraryLoading = (_paperLibrary.length === 0);
_renderPaperLibrary();
_showPaperLanding();
try { await _loadPaperLibrary(); }
catch (e) { console.warn('[Paper] loadPaperLibrary failed:', e); }
finally { _paperLibraryLoading = false; }
if (!paperMode) return;
if (pdfUrl && !_activePaperId) {
_createPaperEntry(fileName, pdfUrl, parsedText, arxivId);
} else if (pdfUrl) {
_paperPdfUrl = pdfUrl;
_paperFileName = fileName || '';
_paperParsedText = parsedText || '';
_paperArxivId = arxivId || '';
} else {
var active = _getActivePaperEntry();
if (active) {
_paperPdfUrl = active.pdfUrl || '';
_paperPdfFilename = active.pdfFilename || '';
_paperFileName = active.title || '';
_paperParsedText = active.parsedText || '';
_paperArxivId = active.arxivId || '';
_paperQAHistory = active.qaHistory || [];
_paperReportCache = '';
_paperReviewCache = '';
_paperReviewVenue = '';
_paperHash = active.paperHash || '';
_paperImages = Array.isArray(active.images) ? active.images : [];
_babelTranslatedPages = active.babelCache || {};
_paperTotalPages = active.pageCount || 0;
} else {
_paperPdfUrl = '';
_paperPdfFilename = '';
_paperFileName = '';
_paperParsedText = '';
_paperArxivId = '';
_paperQAHistory = [];
_paperReportCache = '';
_paperReviewCache = '';
_paperReviewVenue = '';
_paperHash = '';
_paperImages = [];
_babelTranslatedPages = {};
}
}
_paperActiveTab = 'qa';
if (!_paperQAHistory) _paperQAHistory = [];
if (!_paperReportCache) _paperReportCache = '';
_updatePaperTitles();
_renderPaperLibrary();
if (_paperPdfUrl) {
_loadPaperPdf(_paperPdfUrl);
} else {
_showPaperLanding();
}
_switchPaperTab('qa');
_setPaperMobileView('pdf');
try { _populatePaperReportModelDropdown(); } catch (e) {
console.warn('[Paper] populate report model dropdown failed:', e);
}
try { _populatePaperReportModelDropdown(_reportView('review')); } catch (e) {
console.warn('[Paper] populate review model dropdown failed:', e);
}
try { _applyReaderPrefs(); } catch (e) {
console.warn('[Paper] applyReaderPrefs failed:', e);
}
debugLog('Paper Mode: ENTER', 'success');
}
function exitPaperMode() {
_saveActivePaperState();
if (typeof _teardownReadingTracker === 'function') _teardownReadingTracker(true);
paperMode = false;
try {
var topbar = document.getElementById('topbarTitle');
if (topbar) {
var conv = (typeof activeConvId !== 'undefined' && activeConvId && typeof conversations !== 'undefined')
? (conversations || []).find(function (c) { return c && c.id === activeConvId; })
: null;
topbar.textContent = conv && conv.title ? conv.title : 'New Chat';
topbar.title = '';
}
} catch (e) { console.warn('[Paper] restore topbar title failed:', e); }
var sidebar = document.getElementById('sidebar');
if (sidebar) sidebar.classList.remove('paper-active');
try { document.body.classList.remove('paper-mode-active'); } catch (e) {  }
var container = document.getElementById('paperModeContainer');
var chatWrapper = document.querySelector('.chat-wrapper');
var inputArea = document.querySelector('.input-area');
if (container) container.style.display = 'none';
if (chatWrapper) chatWrapper.style.display = '';
if (inputArea) inputArea.style.display = '';
if (typeof _scheduleReflow === 'function') _scheduleReflow();
var pmBtn = document.getElementById('paperModeBtn');
if (pmBtn) {
pmBtn.classList.remove('active');
pmBtn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><line x1="8" y1="7" x2="16" y2="7"/><line x1="8" y1="11" x2="14" y2="11"/></svg><span class="topbar-tool-label">' + (typeof t === 'function' ? t('topbar.paper') : 'Paper') + '</span>';
pmBtn.title = (typeof t === 'function' ? t('paper.title') : 'Paper Reader');
}
if (_paperResizeObserver) { _paperResizeObserver.disconnect(); _paperResizeObserver = null; }
if (_paperIntersectionObserver) { _paperIntersectionObserver.disconnect(); _paperIntersectionObserver = null; }
_paperRenderToken++;
if (_paperPdfDoc) { _paperPdfDoc.destroy(); _paperPdfDoc = null; }
if (_paperQAAbort) { _paperQAAbort.abort(); _paperQAAbort = null; }
[_paperReportStream, _paperReviewStream].forEach(function(st) {
if (st && st.pollTimer) { clearTimeout(st.pollTimer); st.pollTimer = null; }
});
var viewer = document.getElementById('paperPdfViewer');
if (viewer) viewer.innerHTML = '';
debugLog('Paper Mode: EXIT', 'info');
}
function togglePaperMode() {
paperMode ? exitPaperMode() : enterPaperMode();
}
function _applyResolvedTitle(resolvedTitle, paperId) {
var title = (resolvedTitle || '').trim();
if (!title) return;
var pid = paperId || _activePaperId;
var entry = null;
for (var i = 0; i < _paperLibrary.length; i++) {
if (_paperLibrary[i].id === pid) { entry = _paperLibrary[i]; break; }
}
if (!entry) return;
var cur = (entry.title || '').trim();
var isPlaceholder = !cur || /^arxiv[:\s]/i.test(cur);
if (!isPlaceholder) return;
if (cur === title) return;
entry.title = title;
if (pid === _activePaperId) {
_paperFileName = title;
_updatePaperTitles();
}
_renderPaperLibrary();
if (pid === _activePaperId) _saveActivePaperState();
}
function _updatePaperTitles() {
var _tt = (typeof t === 'function') ? t : function(k){ return k; };
var noPaper = _tt('paper.noPaperOpen');
var name = _paperFileName || noPaper;
var stitle = document.getElementById('paperSidebarTitle');
if (stitle) {
stitle.textContent = name;
stitle.title = name;
stitle.classList.toggle('paper-sidebar-title-empty', !_paperFileName);
}
var pageCount = document.getElementById('paperPageCount');
if (pageCount && _paperTotalPages) {
pageCount.textContent = _tt('paper.pages', { count: _paperTotalPages });
} else if (pageCount) {
pageCount.textContent = '';
}
if (paperMode) {
var topbar = document.getElementById('topbarTitle');
if (topbar) {
var label = _paperFileName ? _paperFileName : _tt('paper.title');
topbar.textContent = label;
topbar.title = label;
}
}
}
function _resolvePaperPdfUrl(url) {
if (!url) return url;
var i = url.indexOf('/api/');
if (i < 0) return url;
var canonical = url.slice(i);
return (typeof apiUrl === 'function') ? apiUrl(canonical) : canonical;
}
function _shouldFetchPdfAsData() {
try { return localStorage.getItem('tofu_paper_pdf_data') === '1'; }
catch (_) { return false; }
}
async function _fetchPdfArrayBuffer(url, timeoutMs) {
var i = (url || '').indexOf('/api/');
var canonical = i >= 0 ? url.slice(i) : url;
var ctrl = new AbortController();
var timer = setTimeout(function () { ctrl.abort(); }, timeoutMs || 120000);
try {
return await Api.paper.pdfArrayBuffer(canonical, { signal: ctrl.signal, timeout: 0 });
} finally {
clearTimeout(timer);
}
}
async function _openPaperPdfDoc(url, forceData) {
if (forceData || _shouldFetchPdfAsData()) {
debugLog('[Paper] Loading PDF via client ArrayBuffer (range-bypass)…', 'info');
var _bytesM = await _fetchPdfArrayBuffer(url);
return { doc: await pdfjsLib.getDocument({ data: _bytesM }).promise, viaData: true };
}
var doc = null;
try {
doc = await pdfjsLib.getDocument(url).promise;
await doc.getPage(1);
return { doc: doc, viaData: false };
} catch (e) {
if (doc) { try { doc.destroy(); } catch (_) {} }
debugLog('[Paper] URL load failed (' + (e && e.message || e) +
') — auto-retrying via client ArrayBuffer (range-bypass)…', 'warning');
var _bytes = await _fetchPdfArrayBuffer(url);
return { doc: await pdfjsLib.getDocument({ data: _bytes }).promise, viaData: true };
}
}
async function _loadPaperPdf(url) {
url = _resolvePaperPdfUrl(url);
_paperCurrentUrl = url;
var viewer = document.getElementById('paperPdfViewer');
if (!viewer) return;
viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Loading PDF…</div></div>';
try {
if (typeof pdfjsLib === 'undefined') {
if (typeof _ensurePdfJs === 'function') await _ensurePdfJs();
else { viewer.innerHTML = '<div class="paper-error">PDF.js not available. Refresh the page.</div>'; return; }
}
if (typeof pdfjsLib === 'undefined') {
viewer.innerHTML = '<div class="paper-error">PDF.js failed to load.</div>';
return;
}
if (_paperPdfDoc) { try { _paperPdfDoc.destroy(); } catch (_) {} _paperPdfDoc = null; }
var _tOpen = _paperNow();
var _opened = await _openPaperPdfDoc(url);
_paperPdfDoc = _opened.doc;
_paperViaData = _opened.viaData;
_paperTotalPages = _paperPdfDoc.numPages;
debugLog('[Paper] doc opened in ' + Math.round(_paperNow() - _tOpen) + 'ms (viaData=' +
_paperViaData + ', pages=' + _paperTotalPages + ')', 'info');
_updatePaperTitles();
try {
var _firstPage = await _paperPdfDoc.getPage(1);
var _baseVp = _firstPage.getViewport({ scale: 1.0 });
var _container = document.getElementById('paperPdfViewer');
var _containerW = _container ? (_container.clientWidth - _paperViewerPadX(_container)) : 0;
if (_containerW > 0) {
_paperScale = Math.max(0.25, Math.min(4.0, _containerW / _baseVp.width));
}
} catch (err) {
console.warn('[Paper] Initial fit-width failed:', err);
}
_updateZoomLabel();
await _renderAllPages();
var entry = _getActivePaperEntry();
if (entry) { entry.pageCount = _paperTotalPages; _persistPaperEntry(entry); }
_renderPaperLibrary();
} catch (e) {
console.error('[Paper] Failed to load PDF:', e);
viewer.innerHTML = '<div class="paper-error">Failed to load PDF: ' + escapeHtml(e.message) + '</div>';
}
}
async function _renderAllPages() {
if (!_paperPdfDoc) return false;
var viewer = document.getElementById('paperPdfViewer');
if (!viewer) return false;
var token = ++_paperRenderToken;
viewer.innerHTML = '';
if (_paperIntersectionObserver) { _paperIntersectionObserver.disconnect(); _paperIntersectionObserver = null; }
var tStart = _paperNow();
for (var i = 1; i <= _paperTotalPages; i++) {
if (token !== _paperRenderToken) return false;
var cssW, cssH;
try {
var page = await _paperPdfDoc.getPage(i);
var vp = page.getViewport({ scale: _paperScale });
cssW = vp.width; cssH = vp.height;
} catch (e) {
console.warn('[Paper] Failed to size page', i, ':', e);
cssW = 612 * _paperScale; cssH = 792 * _paperScale;
}
var wrapper = document.createElement('div');
wrapper.className = 'paper-page-wrapper';
wrapper.dataset.page = String(i);
wrapper.dataset.rendered = '0';
wrapper.style.width = cssW + 'px';
wrapper.style.aspectRatio = (cssW / cssH).toFixed(6);
var ph = document.createElement('div');
ph.className = 'paper-page-placeholder';
wrapper.appendChild(ph);
var pageLabel = document.createElement('div');
pageLabel.className = 'paper-page-label';
pageLabel.textContent = i + ' / ' + _paperTotalPages;
wrapper.appendChild(pageLabel);
viewer.appendChild(wrapper);
}
if (token !== _paperRenderToken) return false;
debugLog('[Paper] page shells laid out in ' + Math.round(_paperNow() - tStart) +
'ms (' + _paperTotalPages + ' pages, virtualized)', 'info');
var wrappers = viewer.querySelectorAll('.paper-page-wrapper');
if (typeof IntersectionObserver !== 'undefined') {
_paperIntersectionObserver = new IntersectionObserver(function(entries) {
for (var k = 0; k < entries.length; k++) {
var w = entries[k].target;
if (entries[k].isIntersecting) {
_rasterizePage(w, token).then(function(needsReopen) {
if (needsReopen) _maybeReopenViaData();
});
} else {
_releasePage(w);
}
}
}, {
root: viewer,
rootMargin: '150% 0px 150% 0px',
threshold: 0.01,
});
for (var j = 0; j < wrappers.length; j++) _paperIntersectionObserver.observe(wrappers[j]);
} else {
for (var e = 0; e < wrappers.length; e++) {
var _needReopen = await _rasterizePage(wrappers[e], token);
if (_needReopen) { await _maybeReopenViaData(); return false; }
}
}
if (wrappers.length) {
var _p1Reopen = await _rasterizePage(wrappers[0], token);
if (_p1Reopen) { await _maybeReopenViaData(); return false; }
if (token === _paperRenderToken) {
debugLog('[Paper] first page painted in ' + Math.round(_paperNow() - tStart) + 'ms', 'info');
}
}
_observePageWrappers(viewer);
return false;
}
async function _rasterizePage(wrapper, token) {
if (!wrapper || !_paperPdfDoc) return;
if (wrapper.dataset.rendered === '1' || wrapper.dataset.rendering === '1') return;
if (token != null && token !== _paperRenderToken) return;
var pageNum = parseInt(wrapper.dataset.page, 10);
if (!pageNum) return;
wrapper.dataset.rendering = '1';
var dpr = window.devicePixelRatio || 1;
try {
var page = await _paperPdfDoc.getPage(pageNum);
if (token != null && token !== _paperRenderToken) { wrapper.dataset.rendering = '0'; return; }
var cssViewport = page.getViewport({ scale: _paperScale });
var cssW = cssViewport.width;
var cssH = cssViewport.height;
var hiresViewport = page.getViewport({ scale: _paperScale * dpr });
wrapper.style.width = cssW + 'px';
wrapper.style.aspectRatio = (cssW / cssH).toFixed(6);
var canvas = document.createElement('canvas');
canvas.className = 'paper-pdf-canvas';
canvas.width = hiresViewport.width;
canvas.height = hiresViewport.height;
canvas.style.width = cssW + 'px';
var textDiv = document.createElement('div');
textDiv.className = 'paper-text-layer';
textDiv.style.width = cssW + 'px';
textDiv.style.height = cssH + 'px';
textDiv.style.setProperty('--scale-factor', _paperScale.toString());
var ctx = canvas.getContext('2d');
await page.render({ canvasContext: ctx, viewport: hiresViewport }).promise;
if (token != null && token !== _paperRenderToken) { wrapper.dataset.rendering = '0'; return; }
var ph = wrapper.querySelector('.paper-page-placeholder');
if (ph) ph.remove();
var label = wrapper.querySelector('.paper-page-label');
wrapper.insertBefore(canvas, label || null);
wrapper.insertBefore(textDiv, label || null);
var textContent = await page.getTextContent();
if (typeof pdfjsLib.renderTextLayer === 'function') {
pdfjsLib.renderTextLayer({
textContentSource: textContent,
container: textDiv,
viewport: cssViewport,
textDivs: [],
});
}
wrapper.dataset.rendered = '1';
wrapper.dataset.rendering = '0';
return false;
} catch (e) {
wrapper.dataset.rendering = '0';
console.warn('[Paper] Failed to render page', pageNum, ':', e);
if (!_paperViaData) return true;
var errDiv = document.createElement('div');
errDiv.className = 'paper-page-error';
errDiv.textContent = 'Page ' + pageNum + ' failed to render';
var lbl = wrapper.querySelector('.paper-page-label');
wrapper.insertBefore(errDiv, lbl || null);
return false;
}
}
function _releasePage(wrapper) {
if (!wrapper || wrapper.dataset.rendered !== '1') return;
var canvas = wrapper.querySelector('.paper-pdf-canvas');
var textLayer = wrapper.querySelector('.paper-text-layer');
if (canvas) canvas.remove();
if (textLayer) textLayer.remove();
if (!wrapper.querySelector('.paper-page-placeholder')) {
var ph = document.createElement('div');
ph.className = 'paper-page-placeholder';
wrapper.insertBefore(ph, wrapper.firstChild);
}
wrapper.dataset.rendered = '0';
}
async function _maybeReopenViaData() {
if (_paperReopenInFlight || _paperViaData || !_paperCurrentUrl) return;
_paperReopenInFlight = true;
try {
debugLog('[Paper] A page failed to rasterize — re-opening via client ArrayBuffer (range-bypass) and re-rendering…', 'warning');
if (_paperPdfDoc) { try { _paperPdfDoc.destroy(); } catch (_) {} _paperPdfDoc = null; }
var reopened = await _openPaperPdfDoc(_paperCurrentUrl, true);
_paperPdfDoc = reopened.doc;
_paperViaData = reopened.viaData;
_paperTotalPages = _paperPdfDoc.numPages;
await _renderAllPages();
} catch (e) {
console.error('[Paper] {data} re-open failed:', e);
} finally {
_paperReopenInFlight = false;
}
}
var _paperResizeObserver = null;
function _observePageWrappers(viewer) {
if (_paperResizeObserver) { _paperResizeObserver.disconnect(); _paperResizeObserver = null; }
if (typeof ResizeObserver === 'undefined') return;
_paperResizeObserver = new ResizeObserver(function(entries) {
for (var i = 0; i < entries.length; i++) {
var wrapper = entries[i].target;
var textLayer = wrapper.querySelector('.paper-text-layer');
if (!textLayer) continue;
var origW = parseFloat(textLayer.style.width);
if (!origW) continue;
var actualW = entries[i].contentBoxSize
?  (entries[i].contentBoxSize[0] || entries[i].contentBoxSize).inlineSize
: wrapper.clientWidth;
var scale = actualW / origW;
if (Math.abs(scale - 1) < 0.001) {
textLayer.style.transform = '';
} else {
textLayer.style.transform = 'scale(' + scale.toFixed(6) + ')';
}
}
});
var wrappers = viewer.querySelectorAll('.paper-page-wrapper');
for (var j = 0; j < wrappers.length; j++) {
_paperResizeObserver.observe(wrappers[j]);
}
}
var _paperZoomDebounce = null;
function paperZoomIn() {
_paperScale = Math.min(_paperScale + 0.25, 4.0);
_syncZoomUI();
_renderAllPages();
}
function paperZoomOut() {
_paperScale = Math.max(_paperScale - 0.25, 0.25);
_syncZoomUI();
_renderAllPages();
}
function paperSetScaleFromSlider(val) {
_paperScale = Math.max(0.25, Math.min(4.0, parseInt(val, 10) / 100));
_syncZoomUI();
clearTimeout(_paperZoomDebounce);
_paperZoomDebounce = setTimeout(function() { _renderAllPages(); }, 120);
}
function paperSetScaleFromInput(val) {
var num = parseInt(val.replace('%', ''), 10);
if (isNaN(num) || num < 25) num = 25;
if (num > 400) num = 400;
_paperScale = num / 100;
_syncZoomUI();
_renderAllPages();
}
function _paperViewerPadX(container) {
try {
var cs = getComputedStyle(container);
var l = parseFloat(cs.paddingLeft) || 0;
var r = parseFloat(cs.paddingRight) || 0;
var px = l + r;
return px > 0 ? px : 32;
} catch (err) {
console.warn('[Paper] padding measure failed, using 32:', err);
return 32;
}
}
function paperFitWidth() {
if (!_paperPdfDoc) return;
var container = document.getElementById('paperPdfViewer');
if (!container) return;
_paperPdfDoc.getPage(1).then(function(page) {
var baseViewport = page.getViewport({ scale: 1.0 });
var containerWidth = container.clientWidth - _paperViewerPadX(container);
var fitScale = containerWidth / baseViewport.width;
_paperScale = Math.max(0.25, Math.min(4.0, fitScale));
_syncZoomUI();
_renderAllPages();
});
}
function _syncZoomUI() {
var pct = Math.round(_paperScale * 100);
var input = document.getElementById('paperZoomLevel');
if (input) input.value = pct + '%';
var slider = document.getElementById('paperZoomSlider');
if (slider) slider.value = pct;
}
function _updateZoomLabel() { _syncZoomUI(); }
(function() {
var _dragging = false;
var _startX = 0;
var _startLeftW = 0;
var _startRightW = 0;
var _divider, _left, _right, _body;
function _initDivider() {
_divider = document.getElementById('paperDivider');
if (!_divider) return;
_divider.addEventListener('mousedown', _onMouseDown);
_divider.addEventListener('touchstart', _onTouchStart, { passive: false });
}
function _getElements() {
_left = _divider ? _divider.previousElementSibling : null;
_right = _divider ? _divider.nextElementSibling : null;
_body = _divider ? _divider.parentElement : null;
}
function _onMouseDown(e) {
e.preventDefault();
_getElements();
if (!_left || !_right || !_body) return;
_dragging = true;
_startX = e.clientX;
_startLeftW = _left.getBoundingClientRect().width;
_startRightW = _right.getBoundingClientRect().width;
_left.style.flex = 'none';
_left.style.width = _startLeftW + 'px';
_right.style.flex = '1';
_right.style.width = '';
_right.style.minWidth = '250px';
_divider.classList.add('dragging');
document.body.style.cursor = 'col-resize';
document.body.style.userSelect = 'none';
document.addEventListener('mousemove', _onMouseMove);
document.addEventListener('mouseup', _onMouseUp);
}
function _onMouseMove(e) {
if (!_dragging) return;
var dx = e.clientX - _startX;
var bodyW = _body.getBoundingClientRect().width;
var dividerW = _divider.getBoundingClientRect().width;
var available = bodyW - dividerW;
var newLeftW = Math.max(250, Math.min(available - 250, _startLeftW + dx));
_left.style.width = newLeftW + 'px';
}
function _onMouseUp() {
_dragging = false;
_divider.classList.remove('dragging');
document.body.style.cursor = '';
document.body.style.userSelect = '';
document.removeEventListener('mousemove', _onMouseMove);
document.removeEventListener('mouseup', _onMouseUp);
_autoRefitIfOverflowing();
}
function _onTouchStart(e) {
if (e.touches.length !== 1) return;
e.preventDefault();
_getElements();
if (!_left || !_right || !_body) return;
_dragging = true;
_startX = e.touches[0].clientX;
_startLeftW = _left.getBoundingClientRect().width;
_startRightW = _right.getBoundingClientRect().width;
_left.style.flex = 'none';
_left.style.width = _startLeftW + 'px';
_right.style.flex = '1';
_right.style.width = '';
_right.style.minWidth = '250px';
_divider.classList.add('dragging');
document.addEventListener('touchmove', _onTouchMove, { passive: false });
document.addEventListener('touchend', _onTouchEnd);
}
function _onTouchMove(e) {
if (!_dragging || e.touches.length !== 1) return;
e.preventDefault();
var dx = e.touches[0].clientX - _startX;
var bodyW = _body.getBoundingClientRect().width;
var dividerW = _divider.getBoundingClientRect().width;
var available = bodyW - dividerW;
var newLeftW = Math.max(250, Math.min(available - 250, _startLeftW + dx));
_left.style.width = newLeftW + 'px';
}
function _onTouchEnd() {
_dragging = false;
_divider.classList.remove('dragging');
document.removeEventListener('touchmove', _onTouchMove);
document.removeEventListener('touchend', _onTouchEnd);
_autoRefitIfOverflowing();
}
function _autoRefitIfOverflowing() {
try {
if (typeof _paperPdfDoc === 'undefined' || !_paperPdfDoc) return;
var viewer = document.getElementById('paperPdfViewer');
if (!viewer) return;
var firstWrapper = viewer.querySelector('.paper-page-wrapper');
if (!firstWrapper) return;
var pageW = parseFloat(firstWrapper.style.width) || firstWrapper.clientWidth;
var availW = viewer.clientWidth - 32;
if (availW > 0 && pageW > availW + 1 && typeof paperFitWidth === 'function') {
paperFitWidth();
}
} catch (err) {
console.warn('[Paper] Auto-refit check failed:', err);
}
}
function _onDblClick() {
_getElements();
if (!_left || !_right) return;
_left.style.flex = '1';
_left.style.width = '';
_right.style.flex = '1';
_right.style.width = '';
_right.style.minWidth = '';
}
var _singlePaneMq = null;
var _crossPending = false;
try {
if (typeof window.matchMedia === 'function') {
_singlePaneMq = window.matchMedia('(max-width:1024px) and (pointer:coarse)');
}
} catch (e) {
console.warn('[Paper] matchMedia unavailable:', e);
}
function _paperResponsiveOnCrossing() {
var body = document.querySelector('.paper-body');
if (!body) return;
var singlePane = !!(_singlePaneMq && _singlePaneMq.matches);
if (singlePane) {
var cur = body.getAttribute('data-paper-view');
if (cur !== 'pdf' && cur !== 'reader') {
cur = 'pdf';
}
if (typeof _setPaperMobileView === 'function') {
_setPaperMobileView(cur);
} else {
body.setAttribute('data-paper-view', cur);
}
}
if (typeof paperFitWidth === 'function') {
requestAnimationFrame(function() {
try { paperFitWidth(); } catch (err) { console.warn('[Paper] responsive fit failed:', err); }
});
}
}
window._paperResponsiveOnCrossing = _paperResponsiveOnCrossing;
function _scheduleCrossing() {
if (_crossPending) return;
_crossPending = true;
requestAnimationFrame(function() {
_crossPending = false;
_paperResponsiveOnCrossing();
});
}
function _wireResponsiveCrossing() {
if (_singlePaneMq) {
if (typeof _singlePaneMq.addEventListener === 'function') {
_singlePaneMq.addEventListener('change', _scheduleCrossing);
} else if (typeof _singlePaneMq.addListener === 'function') {
_singlePaneMq.addListener(_scheduleCrossing);
}
}
window.addEventListener('orientationchange', _scheduleCrossing);
}
function _initPaperResponsive() {
_initDivider();
var d = document.getElementById('paperDivider');
if (d) d.addEventListener('dblclick', _onDblClick);
_wireResponsiveCrossing();
}
if (document.readyState === 'loading') {
document.addEventListener('DOMContentLoaded', _initPaperResponsive);
} else {
_initPaperResponsive();
}
})();
function _showPaperLanding() {
var viewer = document.getElementById('paperPdfViewer');
if (!viewer) return;
var _tt = (typeof t === 'function') ? t : function(k){ return k; };
viewer.innerHTML =
'<div class="paper-landing">' +
'<div class="paper-landing-icon">' + Icon('file', 40) + '</div>' +
'<h3>' + escapeHtml(_tt('paper.title')) + '</h3>' +
'<p>' + escapeHtml(_tt('paper.landingDesc')) + '</p>' +
'<div class="paper-landing-actions">' +
'<label class="paper-upload-btn">' +
'<input type="file" accept=".pdf,application/pdf" onchange="_handlePaperFileUpload(event)" style="display:none">' +
'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>' +
' ' + escapeHtml(_tt('paper.uploadPdf')) +
'</label>' +
'<div class="paper-arxiv-input">' +
'<svg class="paper-arxiv-search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>' +
'<input type="text" id="paperArxivUrl" placeholder="' + escapeHtml(_tt('paper.arxivPlaceholder')) + '"' +
' onkeydown="if(event.key===\'Enter\')_submitArxivQuery()">' +
'<button onclick="_submitArxivQuery()" class="paper-arxiv-btn">' + escapeHtml(_tt('paper.search')) + '</button>' +
'</div>' +
'<div class="paper-describe-divider"><span>' + escapeHtml(_tt('paper.describeOr')) + '</span></div>' +
'<div class="paper-describe-box">' +
'<div class="paper-describe-label">' +
'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a7 7 0 0 0-4 12.7V17a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2.3A7 7 0 0 0 12 2Z"/><line x1="9" y1="21" x2="15" y2="21"/></svg>' +
' ' + escapeHtml(_tt('paper.describeLabel')) +
'</div>' +
'<textarea id="paperDescribeInput" class="paper-describe-input" rows="3" placeholder="' + escapeHtml(_tt('paper.describePlaceholder')) + '"' +
' oninput="_paperDescribeDraft=this.value"' +
' onkeydown="if(event.key===\'Enter\'&&(event.metaKey||event.ctrlKey))_submitPaperDescribe()">' + escapeHtml(_paperDescribeDraft) + '</textarea>' +
'<button onclick="_submitPaperDescribe()" class="paper-describe-btn">' +
'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>' +
' ' + escapeHtml(_tt('paper.describeBtn')) +
'</button>' +
'</div>' +
'</div>' +
'</div>';
}
function _showPaperLandingForNew() {
_setActivePaperId('');
_paperPdfUrl = '';
_paperPdfFilename = '';
_paperFileName = '';
_paperParsedText = '';
_paperArxivId = '';
_paperQAHistory = [];
_paperReportCache = '';
_paperReviewCache = '';
_paperReviewVenue = '';
_paperHash = '';
_paperImages = [];
_babelTranslatedPages = {};
_paperTotalPages = 0;
_updatePaperTitles();
_renderPaperLibrary();
_showPaperLanding();
}
async function _handlePaperFileDrop(file) {
if (!file) return;
if (!file.name.toLowerCase().endsWith('.pdf') && file.type !== 'application/pdf') return;
if (!paperMode) enterPaperMode();
await _paperUploadFile(file);
}
async function _handlePaperFileUpload(event) {
var file = event.target.files[0];
if (!file || !file.name.toLowerCase().endsWith('.pdf')) return;
await _paperUploadFile(file);
}
async function _paperUploadFile(file) {
_paperLoading = true;
var _uploadEntry = _createPaperEntry(file.name);
var _uploadPaperId = _uploadEntry.id;
_paperFileName = file.name;
_paperParsedText = '';
_paperQAHistory = [];
_paperReportCache = '';
_paperReviewCache = '';
_paperReviewVenue = '';
_paperHash = '';
_paperPdfFilename = '';
_paperImages = [];
_babelTranslatedPages = {};
_updatePaperTitles();
_renderPaperLibrary();
var viewer = document.getElementById('paperPdfViewer');
if (viewer) viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Uploading & parsing PDF…</div></div>';
try {
var formData = new FormData();
formData.append('file', file);
formData.append('paper_id', _uploadPaperId);
var uploadData = await Api.paper.upload(formData);
if (!uploadData || !uploadData.ok) throw new Error((uploadData && uploadData.error) || 'Upload failed');
_paperPdfUrl = apiUrl(uploadData.pdf_url);
_paperPdfFilename = uploadData.filename || '';
_paperParsedText = uploadData.parsed_text || '';
_paperHash = uploadData.paper_hash || '';
_paperImages = Array.isArray(uploadData.images) ? uploadData.images : [];
_paperTotalPages = uploadData.total_pages || 0;
if (uploadData.parse_error) {
debugLog('[Paper] PDF text extraction failed: ' + uploadData.parse_error, 'warning');
} else if (_paperParsedText) {
debugLog('Paper parsed: ' + _paperTotalPages + ' pages, ' +
(uploadData.text_length || _paperParsedText.length) + ' chars' +
(_paperImages.length ? ' (' + _paperImages.length + ' figures)' : ''),
'success');
}
_updatePaperTitles();
await _loadPaperPdf(_paperPdfUrl);
await _saveActivePaperState();
} catch (e) {
console.error('[Paper] Upload failed:', e);
_paperLibrary = _paperLibrary.filter(function(p) { return p.id !== _uploadPaperId; });
if (_activePaperId === _uploadPaperId) _setActivePaperId('');
_renderPaperLibrary();
if (viewer) viewer.innerHTML = '<div class="paper-error">Upload failed: ' + escapeHtml(e.message) + '</div>';
} finally {
_paperLoading = false;
}
}
function _formatPaperBytes(n) {
if (!n || n < 0) return '0 B';
if (n < 1024) return n + ' B';
if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
return (n / (1024 * 1024)).toFixed(2) + ' MB';
}
function _renderArxivFetchProgress(state) {
var viewer = document.getElementById('paperPdfViewer');
if (!viewer) return;
var isZh = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh');
var labels = isZh
? { resolving: '解析 arXiv 链接…', downloading: '下载 PDF…',
parsing: '解析 PDF 文本…', parsingImages: '提取图表…',
pageOf: '第 {done} / {total} 页',
cached: '已从缓存加载', pages: '页', chars: '字符' }
: { resolving: 'Resolving arXiv link…', downloading: 'Downloading PDF…',
parsing: 'Extracting PDF text…', parsingImages: 'Extracting figures…',
pageOf: 'page {done} / {total}',
cached: 'Loaded from cache', pages: 'pages', chars: 'chars' };
var title;
if (state.stage === 'resolve') title = labels.resolving;
else if (state.stage === 'download') title = labels.downloading;
else if (state.stage === 'download_done') title = state.cached ? labels.cached : labels.downloading;
else if (state.stage === 'parse_start' || state.stage === 'parse_done') title = labels.parsing;
else if (state.stage === 'parse_progress') {
title = (state.parse_stage === 'images') ? labels.parsingImages : labels.parsing;
}
else title = labels.resolving;
var pct = 0;
var detail = '';
if (state.stage === 'download') {
if (state.total > 0) {
pct = Math.min(100, Math.round(state.downloaded * 100 / state.total));
detail = _formatPaperBytes(state.downloaded) + ' / ' + _formatPaperBytes(state.total);
} else {
detail = _formatPaperBytes(state.downloaded);
pct = -1;
}
} else if (state.stage === 'download_done') {
pct = 100;
detail = _formatPaperBytes(state.file_size || 0);
} else if (state.stage === 'parse_start') {
pct = -1;
detail = '';
} else if (state.stage === 'parse_progress') {
var done = state.page || 0;
var total = state.total_pages || 0;
if (total > 0) {
pct = Math.min(100, Math.round(done * 100 / total));
detail = labels.pageOf.replace('{done}', done).replace('{total}', total);
} else {
pct = -1;
detail = '';
}
} else if (state.stage === 'parse_done') {
pct = 100;
detail = (state.total_pages || 0) + ' ' + labels.pages +
' · ' + (state.text_length || 0).toLocaleString() + ' ' + labels.chars;
}
var barStyle = (pct < 0)
? 'width:40%;animation:paperProgressIndet 1.2s ease-in-out infinite'
: 'width:' + pct + '%';
viewer.innerHTML =
'<div class="paper-loading paper-fetch-progress">' +
'<div class="paper-loading-spinner"></div>' +
'<div class="paper-fetch-title">' + escapeHtml(title) +
(state.arxiv_id ? ' <span class="paper-fetch-id">arXiv:' + escapeHtml(state.arxiv_id) + '</span>' : '') +
'</div>' +
'<div class="paper-fetch-bar-wrap"><div class="paper-fetch-bar" style="' + barStyle + '"></div></div>' +
(detail ? '<div class="paper-fetch-detail">' + escapeHtml(detail) + '</div>' : '') +
'</div>';
}
function _looksLikeArxivRef(s) {
s = (s || '').trim();
if (/arxiv\.org\//i.test(s)) return true;
if (/^\d{4}\.\d{4,5}(v\d+)?$/.test(s)) return true;
if (/^[a-z-]+\/\d{7}(v\d+)?$/i.test(s)) return true;
return false;
}
function _submitArxivQuery() {
var input = document.getElementById('paperArxivUrl');
var q = input?.value?.trim();
if (!q) { debugLog('Please enter a title to search, or an arXiv URL / ID', 'warning'); return; }
if (_looksLikeArxivRef(q)) {
_fetchArxivPaper(q);
} else {
_searchArxivPapers(q);
}
}
async function _searchArxivPapers(query) {
var viewer = document.getElementById('paperPdfViewer');
var _tt = (typeof t === 'function') ? t : function(k){ return k; };
if (viewer) {
viewer.innerHTML =
'<div class="paper-loading paper-search-loading">' +
'<div class="paper-loading-spinner"></div>' +
'<div>' + escapeHtml(_tt('paper.searching')) + '</div>' +
'</div>';
}
try {
var data = await Api.paper.searchArxiv(query, 12);
var results = (data && data.ok && Array.isArray(data.results)) ? data.results : [];
_paperSearchResults = results;
_renderArxivSearchResults(query, results);
} catch (e) {
console.error('[Paper] arXiv search failed:', e);
if (viewer) {
viewer.innerHTML =
'<div class="paper-error">' + escapeHtml(_tt('paper.searchFailed')) +
'<br><button onclick="_showPaperLanding()" class="paper-retry-btn">' +
escapeHtml(_tt('paper.searchBack')) + '</button></div>';
}
}
}
function _escWithInlineMath(text) {
var raw = (text == null) ? '' : String(text);
if (raw.indexOf('$') === -1 && raw.indexOf('\\(') === -1) return escapeHtml(raw);
var hasKatex = (typeof katex !== 'undefined');
if (!hasKatex && typeof _ensureKatex === 'function') { try { _ensureKatex(); } catch (_) {} }
var re = /\$(?!\$)((?:\\.|[^$\\])+?)\$(?!\$)|\\\(([\s\S]*?)\\\)/g;
var out = '';
var last = 0;
var m;
while ((m = re.exec(raw)) !== null) {
out += escapeHtml(raw.slice(last, m.index));
var tex = (m[1] != null ? m[1] : m[2]).trim();
if (hasKatex) {
try {
out += katex.renderToString(tex, { throwOnError: false, displayMode: false, strict: false, trust: true });
} catch (e) {
out += '<code class="math-error">' + escapeHtml(tex) + '</code>';
}
} else {
out += '<code class="math-pending">' + escapeHtml(tex) + '</code>';
}
last = re.lastIndex;
}
out += escapeHtml(raw.slice(last));
return out;
}
function _renderArxivSearchResults(query, results) {
var viewer = document.getElementById('paperPdfViewer');
if (!viewer) return;
_lastArxivSearchQuery = query;
var _tt = (typeof t === 'function') ? t : function(k){ return k; };
var header =
'<div class="paper-search-head">' +
'<button class="paper-search-back" onclick="_showPaperLanding()" title="' + escapeHtml(_tt('paper.searchBack')) + '">' +
'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>' +
'</button>' +
'<div class="paper-search-head-text">' +
'<div class="paper-search-head-title">' + escapeHtml(_tt('paper.searchResultsTitle')) + '</div>' +
'<div class="paper-search-head-q">“' + escapeHtml(query) + '”</div>' +
'</div>' +
'</div>';
if (!results.length) {
viewer.innerHTML =
'<div class="paper-search">' + header +
'<div class="paper-search-empty">' + escapeHtml(_tt('paper.searchNoResults')) + '</div>' +
'</div>';
return;
}
var hint = '<div class="paper-search-hint">' + escapeHtml(_tt('paper.searchResultsHint')) + '</div>';
var cards = results.map(function(r, i) {
var authors = Array.isArray(r.authors) ? r.authors : [];
var authorStr = authors.slice(0, 4).join(', ') + (authors.length > 4 ? ' et al.' : '');
var meta = [];
if (r.primary_category) meta.push('<span class="paper-card-cat">' + escapeHtml(r.primary_category) + '</span>');
if (r.published) meta.push('<span class="paper-card-date">' + escapeHtml(r.published) + '</span>');
meta.push('<span class="paper-card-id">arXiv:' + escapeHtml(r.arxiv_id) + '</span>');
return '' +
'<div class="paper-result-card" role="button" tabindex="0" data-idx="' + i + '"' +
' onclick="_openArxivResult(' + i + ')"' +
' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();_openArxivResult(' + i + ')}">' +
'<div class="paper-result-num">' + (i + 1) + '</div>' +
'<div class="paper-result-body">' +
'<div class="paper-result-title">' + _escWithInlineMath(r.title || r.arxiv_id) + '</div>' +
(authorStr ? '<div class="paper-result-authors">' + escapeHtml(authorStr) + '</div>' : '') +
(r.summary ? '<div class="paper-result-summary">' + _escWithInlineMath(r.summary) + '</div>' : '') +
'<div class="paper-result-meta">' + meta.join('') + '</div>' +
'</div>' +
'<div class="paper-result-arrow">' +
'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>' +
'</div>' +
'</div>';
}).join('');
viewer.innerHTML =
'<div class="paper-search">' + header + hint +
'<div class="paper-result-list">' + cards + '</div>' +
'</div>';
}
function _openArxivResult(idx) {
var r = _paperSearchResults && _paperSearchResults[idx];
if (!r || !r.arxiv_id) return;
_fetchArxivPaper(r.arxiv_id);
}
var _paperRecommendResults = [];
var _paperRecommendCorrection = null;
var _recStream = null;
var _recPaintScheduled = false;
function _newRecStream(description) {
return {
description: description,
taskId: null,
cursor: 0,
status: 'pending',
candidateCount: 0,
interpreted: false,
researchCount: 0,
researchLabel: '',
toolRounds: [],
results: [],
correction: null,
llmError: false,
aborted: false,
};
}
function _submitPaperDescribe() {
var input = document.getElementById('paperDescribeInput');
var q = input && input.value ? input.value.trim() : '';
if (!q) { debugLog('Describe the paper you are looking for', 'warning'); return; }
_recommendPapers(q);
}
async function _recommendPapers(description) {
if (_recStream && _recStream.taskId && _recStream.status === 'running') {
_recStream.aborted = true;
try { Api.paper.recommendAbort(_recStream.taskId); } catch (_) {}
}
var s = _newRecStream(description);
_recStream = s;
_paperRecommendResults = s.results;
_paperRecommendCorrection = null;
_paintRecommendFromState();
try {
var startData = await Api.paper.recommendStart(description, 6);
if (!startData || !startData.ok || !startData.task_id) {
throw new Error((startData && startData.error) || 'recommend start failed');
}
if (_recStream !== s) return;
s.taskId = startData.task_id;
await _pollRecommendTask(s);
} catch (e) {
console.error('[Paper] recommend failed:', e);
if (_recStream === s) { s.status = 'error'; _renderRecommendError(description); }
}
}
async function _pollRecommendTask(s) {
var POLL_MS = 600;
while (true) {
if (_recStream !== s || s.aborted) break;
var resp = await Api.paper.recommendPoll(s.taskId, s.cursor);
if (_recStream !== s || s.aborted) break;
if (!resp || !resp.ok) {
if (resp && resp.status === 404) { s.status = 'error'; _paintRecommendFromState(); break; }
throw new Error('HTTP ' + (resp ? resp.status : '?'));
}
var data = await resp.json();
if (!data.ok) throw new Error((typeof data.error === 'string' ? data.error : 'Poll failed'));
var events = data.events || [];
for (var i = 0; i < events.length; i++) _applyRecommendEvent(s, events[i]);
s.cursor = data.next_cursor;
if (data.status === 'done') {
s.status = 'done';
if (Array.isArray(data.results) && data.results.length >= s.results.length) {
s.results = data.results; _paperRecommendResults = s.results;
}
if (data.correction) s.correction = data.correction;
s.llmError = !!data.llmError;
_paintRecommendFromState();
break;
}
if (data.status === 'error') {
s.status = 'error';
s.llmError = !!data.llmError;
_renderRecommendError(s.description);
break;
}
_paintRecommendFromState();
await new Promise(function(r) { setTimeout(r, POLL_MS); });
}
}
function _applyRecommendEvent(s, ev) {
switch (ev.type) {
case 'tool_start':
s.researchCount = (s.researchCount || 0) + 1;
s.researchLabel = (typeof ev.query === 'string' ? ev.query : '').slice(0, 80);
s.toolRounds.push({
roundNum: ev.roundNum,
toolName: ev.toolName,
query: ev.query || ev.toolName,
toolCallId: ev.toolCallId || '',
toolArgs: ev.toolArgs || '',
status: 'searching',
results: null,
});
return;
case 'tool_done': {
var tr = null;
for (var ti = 0; ti < s.toolRounds.length; ti++) {
if (s.toolRounds[ti].roundNum === ev.roundNum) { tr = s.toolRounds[ti]; break; }
}
if (tr) {
tr.status = 'done';
if (typeof ev.elapsed === 'number') tr._elapsed = ev.elapsed.toFixed(1) + 's';
if (ev.toolContent) tr.toolContent = ev.toolContent;
if (ev.results) tr.results = ev.results;
if (ev.searchDiag) tr.searchDiag = ev.searchDiag;
if (ev.engineBreakdown) tr.engineBreakdown = ev.engineBreakdown;
if (ev.verticals) tr.verticals = ev.verticals;
}
return;
}
case 'interpret_done':
s.interpreted = true;
s.candidateCount = (typeof ev.candidateCount === 'number') ? ev.candidateCount : 0;
return;
case 'candidate': {
var idx = (typeof ev.index === 'number') ? ev.index : s.results.length;
s.results[idx] = ev.card;
_paperRecommendResults = s.results;
return;
}
case 'correction':
s.correction = ev.correction || null;
_paperRecommendCorrection = (s.correction && s.correction.paper) ? s.correction.paper : null;
return;
case 'error':
s.llmError = !!ev.llmError;
return;
default:
return;
}
}
function _renderRecommendError(description) {
var viewer = document.getElementById('paperPdfViewer');
if (!viewer) return;
var _tt = (typeof t === 'function') ? t : function(k){ return k; };
viewer.innerHTML =
'<div class="paper-error">' + escapeHtml(_tt('paper.recommendFailed')) +
'<br><button onclick="_showPaperLanding()" class="paper-retry-btn">' +
escapeHtml(_tt('paper.searchBack')) + '</button></div>';
}
function _recCardInnerHtml(r, i) {
var authors = Array.isArray(r.authors) ? r.authors : [];
var authorStr = authors.slice(0, 4).join(', ') + (authors.length > 4 ? ' et al.' : '');
var meta = [];
if (r.venue) meta.push('<span class="paper-card-venue">' + escapeHtml(r.venue) + '</span>');
if (r.primary_category) meta.push('<span class="paper-card-cat">' + escapeHtml(r.primary_category) + '</span>');
if (r.published) meta.push('<span class="paper-card-date">' + escapeHtml(r.published) + '</span>');
meta.push('<span class="paper-card-id">arXiv:' + escapeHtml(r.arxiv_id) + '</span>');
return '' +
'<div class="paper-result-num">' + (i + 1) + '</div>' +
'<div class="paper-result-body">' +
'<div class="paper-result-title">' + _escWithInlineMath(r.title || r.arxiv_id) + '</div>' +
(r.why ? '<div class="paper-result-why">' +
'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>' +
'<span>' + escapeHtml(r.why) + '</span></div>' : '') +
(authorStr ? '<div class="paper-result-authors">' + escapeHtml(authorStr) + '</div>' : '') +
(r.summary ? '<div class="paper-result-summary">' + _escWithInlineMath(r.summary) + '</div>' : '') +
'<div class="paper-result-meta">' + meta.join('') + '</div>' +
'</div>' +
'<div class="paper-result-arrow">' +
'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>' +
'</div>';
}
function _recSkeletonInnerHtml() {
return '' +
'<div class="paper-result-num paper-rec-sk-num"></div>' +
'<div class="paper-result-body">' +
'<div class="paper-rec-sk-line paper-rec-sk-title"></div>' +
'<div class="paper-rec-sk-line paper-rec-sk-why"></div>' +
'<div class="paper-rec-sk-line paper-rec-sk-meta"></div>' +
'</div>';
}
function _recCorrectionHtml(correction, _tt) {
if (!correction || !correction.note) return '';
var offer = '';
if (correction.paper && correction.paper.arxiv_id) {
var cp = correction.paper;
offer =
'<div class="paper-correction-offer paper-result-card" role="button" tabindex="0"' +
' onclick="_openRecommendCorrection()"' +
' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();_openRecommendCorrection()}">' +
'<div class="paper-result-body">' +
'<div class="paper-correction-offer-label">' + escapeHtml(_tt('paper.correctionActual')) + '</div>' +
'<div class="paper-result-title">' + _escWithInlineMath(cp.title || cp.arxiv_id) + '</div>' +
'<div class="paper-result-meta"><span class="paper-card-id">arXiv:' + escapeHtml(cp.arxiv_id) + '</span></div>' +
'</div>' +
'<div class="paper-result-arrow">' +
'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>' +
'</div>' +
'</div>';
}
return '' +
'<div class="paper-correction" role="note">' +
'<div class="paper-correction-icon">' +
'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>' +
'</div>' +
'<div class="paper-correction-body">' +
'<div class="paper-correction-title">' + escapeHtml(_tt('paper.correctionTitle')) + '</div>' +
'<div class="paper-correction-note">' + escapeHtml(correction.note) + '</div>' +
offer +
'</div>' +
'</div>';
}
function _paintRecommendFromState() {
if (_recPaintScheduled) return;
_recPaintScheduled = true;
var raf = (typeof requestAnimationFrame === 'function')
? requestAnimationFrame : function(fn){ return setTimeout(fn, 16); };
raf(function() {
_recPaintScheduled = false;
try { _paintRecommendNow(); }
catch (e) { console.warn('[Paper:Recommend] paint failed:', e); }
});
}
function _paintRecommendNow() {
var s = _recStream;
if (!s) return;
var viewer = document.getElementById('paperPdfViewer');
if (!viewer) return;
var _tt = (typeof t === 'function') ? t : function(k){ return k; };
var grounded = s.results.filter(function(x){ return !!x; }).length;
var slots = s.interpreted
? Math.max(grounded, (s.status === 'done') ? grounded : s.candidateCount)
: 0;
var shell = viewer.querySelector('.paper-search[data-rec-shell]');
if (!shell) {
var header =
'<div class="paper-search-head">' +
'<button class="paper-search-back" onclick="_showPaperLanding()" title="' + escapeHtml(_tt('paper.searchBack')) + '">' +
'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>' +
'</button>' +
'<div class="paper-search-head-text">' +
'<div class="paper-search-head-title">' + escapeHtml(_tt('paper.recommendTitle')) + '</div>' +
'<div class="paper-search-head-q">“' + escapeHtml(s.description) + '”</div>' +
'</div>' +
'</div>';
viewer.innerHTML =
'<div class="paper-search" data-rec-shell="1">' + header +
'<div class="paper-rec-status" data-rec-status aria-live="polite"></div>' +
'<div class="paper-report-tools paper-rec-tools" data-rec-tools></div>' +
'<div class="paper-rec-banner" data-rec-banner></div>' +
'<div class="paper-search-hint" data-rec-hint hidden>' + escapeHtml(_tt('paper.recommendHint')) + '</div>' +
'<div class="paper-result-list" data-rec-list aria-live="polite" aria-relevant="additions"></div>' +
'</div>';
shell = viewer.querySelector('.paper-search[data-rec-shell]');
}
var statusEl = shell.querySelector('[data-rec-status]');
var toolsEl = shell.querySelector('[data-rec-tools]');
var bannerEl = shell.querySelector('[data-rec-banner]');
var hintEl = shell.querySelector('[data-rec-hint]');
var listEl = shell.querySelector('[data-rec-list]');
var statusHtml = '';
if (!s.interpreted && s.status !== 'error') {
if (s.researchCount > 0) {
var researchTxt = _tt('paper.recommendResearching').replace('{n}', s.researchCount);
statusHtml = '<span class="paper-rec-spin"></span>' + escapeHtml(researchTxt);
} else {
statusHtml = '<span class="paper-rec-spin"></span>' + escapeHtml(_tt('paper.recommendInterpreting'));
}
} else if (s.status === 'running' && grounded < slots) {
statusHtml = '<span class="paper-rec-spin"></span>' +
escapeHtml(_tt('paper.recommendGrounding').replace('{n}', grounded).replace('{total}', slots));
}
if (statusEl._recSig !== statusHtml) {
statusEl.innerHTML = statusHtml;
statusEl.hidden = !statusHtml;
statusEl._recSig = statusHtml;
}
if (toolsEl) {
var toolCount = s.toolRounds.length;
var searchingCount = 0;
for (var tci = 0; tci < s.toolRounds.length; tci++) {
if (s.toolRounds[tci].status === 'searching') searchingCount++;
}
var toolKey = toolCount + ':' + searchingCount;
if (toolsEl._recToolKey !== toolKey) {
if (toolCount > 0 && typeof renderToolRoundsHTML === 'function') {
toolsEl.innerHTML = renderToolRoundsHTML(s.toolRounds, s.status === 'running');
} else {
toolsEl.innerHTML = '';
}
toolsEl.hidden = toolCount === 0;
toolsEl._recToolKey = toolKey;
}
}
var bannerHtml = _recCorrectionHtml(s.correction, _tt);
if (bannerEl._recSig !== bannerHtml) {
bannerEl.innerHTML = bannerHtml;
bannerEl._recSig = bannerHtml;
}
var showEmpty = (s.status === 'done' && grounded === 0 && !bannerHtml);
var emptyHtml = showEmpty
? '<div class="paper-search-empty">' + escapeHtml(_tt('paper.recommendNoResults')) + '</div>' : '';
if (hintEl) hintEl.hidden = !(grounded > 0);
while (listEl.children.length > slots) {
listEl.removeChild(listEl.lastElementChild);
}
for (var i = 0; i < slots; i++) {
var card = s.results[i];
var node = listEl.children[i];
if (!node) {
node = document.createElement('div');
node.style.setProperty('--i', String(i));
listEl.appendChild(node);
}
var status = card ? 'grounded' : 'searching';
var sig = card ? ('g:' + (card.arxiv_id || i)) : 'sk';
if (node._recSig === sig) continue;
if (card) {
node.className = 'paper-result-card paper-rec-card';
node.setAttribute('role', 'button');
node.setAttribute('tabindex', '0');
node.setAttribute('data-idx', String(i));
node.setAttribute('data-status', status);
node.onclick = (function(idx){ return function(){ _openRecommendResult(idx); }; })(i);
node.onkeydown = (function(idx){ return function(ev){
if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); _openRecommendResult(idx); }
}; })(i);
node.innerHTML = _recCardInnerHtml(card, i);
} else {
node.className = 'paper-result-card paper-rec-card paper-rec-skeleton';
node.removeAttribute('role');
node.removeAttribute('tabindex');
node.setAttribute('data-status', status);
node.setAttribute('aria-hidden', 'true');
node.onclick = null; node.onkeydown = null;
node.innerHTML = _recSkeletonInnerHtml();
}
node._recSig = sig;
}
var existingEmpty = shell.querySelector('.paper-search-empty');
if (emptyHtml && !existingEmpty) {
listEl.insertAdjacentHTML('afterend', emptyHtml);
} else if (!emptyHtml && existingEmpty) {
existingEmpty.remove();
}
}
function _openRecommendResult(idx) {
var r = _paperRecommendResults && _paperRecommendResults[idx];
if (!r || !r.arxiv_id) return;
_fetchArxivPaper(r.arxiv_id);
}
function _openRecommendCorrection() {
var r = _paperRecommendCorrection;
if (!r || !r.arxiv_id) return;
_fetchArxivPaper(r.arxiv_id);
}
async function _fetchArxivPaper(directUrl) {
var url = directUrl;
if (url == null) {
var input = document.getElementById('paperArxivUrl');
url = input?.value?.trim();
}
url = (url || '').trim();
if (!url) { debugLog('Please enter an arXiv URL or ID', 'warning'); return; }
_paperLoading = true;
_renderArxivFetchProgress({ stage: 'resolve' });
var _arxivPaperId = _newPaperEntryId();
try {
var resp = await Api.paper.fetchArxivStream(url, _arxivPaperId);
if (!resp || !resp.ok || !resp.body) {
var errText = '';
try { var j = await resp.json(); errText = j.error || ''; } catch (_) {}
throw new Error(errText || ('HTTP ' + resp.status));
}
var reader = resp.body.getReader();
var decoder = new TextDecoder();
var buffer = '';
var doneData = null;
var streamErr = '';
var curArxivId = '';
while (true) {
var r = await reader.read();
if (r.done) break;
buffer += decoder.decode(r.value, { stream: true });
var lines = buffer.split('\n');
buffer = lines.pop();
for (var li = 0; li < lines.length; li++) {
var line = lines[li];
if (!line.startsWith('data: ')) continue;
var payload = line.slice(6).trim();
if (!payload) continue;
var ev;
try { ev = JSON.parse(payload); }
catch (pe) { console.warn('[Paper:arXiv] Bad SSE payload:', pe, payload); continue; }
if (ev.arxiv_id) curArxivId = ev.arxiv_id;
ev.arxiv_id = ev.arxiv_id || curArxivId;
if (ev.stage === 'error') { streamErr = ev.error || 'Fetch failed'; break; }
_renderArxivFetchProgress(ev);
if (ev.stage === 'done') { doneData = ev; }
}
if (streamErr) break;
}
if (streamErr) throw new Error(streamErr);
if (!doneData) throw new Error('Fetch ended without completion');
_paperPdfUrl = apiUrl(doneData.pdf_url);
var _pdfMatch = /\/api\/paper\/pdf\/([^?#]+)/.exec(doneData.pdf_url || '');
_paperPdfFilename = _pdfMatch ? decodeURIComponent(_pdfMatch[1]) : '';
_paperArxivId = doneData.arxiv_id || curArxivId || '';
_paperFileName = (doneData.title || '').trim() || ('arXiv:' + _paperArxivId);
_paperParsedText = doneData.parsed_text || '';
_paperTotalPages = doneData.total_pages || 0;
_paperHash = doneData.paper_hash || '';
_paperImages = Array.isArray(doneData.images) ? doneData.images : [];
_createPaperEntry(_paperFileName, _paperPdfUrl, _paperParsedText, _paperArxivId, _arxivPaperId);
_paperQAHistory = [];
_paperReportCache = '';
_paperReviewCache = '';
_paperReviewVenue = '';
_babelTranslatedPages = {};
_updatePaperTitles();
_renderPaperLibrary();
if (doneData.parse_error) {
debugLog('[Paper] PDF text extraction failed: ' + doneData.parse_error, 'warning');
} else if (_paperParsedText) {
debugLog('arXiv parsed: ' + _paperTotalPages + ' pages, ' +
(doneData.text_length || _paperParsedText.length) + ' chars' +
(_paperImages.length ? ' (' + _paperImages.length + ' figures)' : ''),
'success');
} else {
debugLog('[Paper] arXiv PDF loaded but no text extracted — Q&A and Report unavailable', 'warning');
}
await _loadPaperPdf(_paperPdfUrl);
await _saveActivePaperState();
debugLog('Fetched arXiv:' + _paperArxivId + (doneData.cached ? ' (cached)' : ''), 'success');
} catch (e) {
console.error('[Paper] arXiv fetch failed:', e);
_paperLibrary = _paperLibrary.filter(function(p) { return p.id !== _arxivPaperId; });
if (_activePaperId === _arxivPaperId) _setActivePaperId('');
_renderPaperLibrary();
var viewer = document.getElementById('paperPdfViewer');
if (viewer) viewer.innerHTML = '<div class="paper-error">Failed: ' + escapeHtml(e.message || String(e)) + '<br><button onclick="_showPaperLanding()" class="paper-retry-btn">Try Again</button></div>';
} finally {
_paperLoading = false;
}
}
function _switchPaperTab(tab) {
if ((_paperActiveTab === 'report' || _paperActiveTab === 'review') && tab !== _paperActiveTab
&& typeof _teardownReadingTracker === 'function') {
_teardownReadingTracker(true);
}
_paperActiveTab = tab;
document.querySelectorAll('.paper-tab-btn').forEach(function(btn) {
btn.classList.toggle('active', btn.dataset.tab === tab);
});
document.querySelectorAll('.paper-tab-panel').forEach(function(panel) {
panel.style.display = panel.dataset.tab === tab ? '' : 'none';
});
if (tab === 'report' || tab === 'review') {
try {
var _sb = document.getElementById('sidebar');
if (_sb && !_sb.classList.contains('collapsed') && typeof toggleSidebar === 'function') {
toggleSidebar();
}
} catch (e) {
console.warn('[Paper] auto-collapse sidebar failed:', e);
}
var _view = _reportView(tab);
if (_paperParsedText || _paperHash) {
if (tab === 'review') {
_populateReviewVenueDropdown()
.then(function() { _loadOrGenerateReport(_view); })
.catch(function(e) {
console.warn('[Paper:Review] venue resolve failed, loading with fallback:', e);
_loadOrGenerateReport(_view);
});
} else {
_loadOrGenerateReport(_view);
}
} else {
var _empty = document.getElementById(_view.containerId);
if (_empty) {
_empty.innerHTML = '<div class="paper-report-empty"><p>' + escapeHtml((typeof t === 'function') ? t('paper.reportNoText') : 'No paper text available. Load a PDF first.') + '</p></div>';
}
}
}
if (tab === 'qa') _renderPaperQA();
if (tab === 'translate') _initBabelPdfTab();
}
function _setPaperMobileView(view) {
if (view !== 'pdf' && view !== 'reader') view = 'pdf';
var body = document.querySelector('.paper-body');
if (body) body.setAttribute('data-paper-view', view);
document.querySelectorAll('.paper-mobile-switch-btn').forEach(function(btn) {
btn.classList.toggle('active', btn.dataset.view === view);
});
if (view === 'pdf' && _paperPdfDoc && typeof paperFitWidth === 'function') {
requestAnimationFrame(function() {
try { paperFitWidth(); } catch (e) { console.warn('[Paper] mobile fit-width failed:', e); }
});
}
}
function _qaMsgInnerHtml(msg) {
var isUser = msg.role === 'user';
var inner = '';
if (!isUser && Array.isArray(msg.toolRounds) && msg.toolRounds.length &&
typeof renderToolRoundsHTML === 'function') {
inner += '<div class="paper-qa-tools">' +
renderToolRoundsHTML(msg.toolRounds, msg.status === 'running') + '</div>';
}
if (isUser) {
inner += '<div class="paper-qa-msg-content">' + escapeHtml(msg.content) + '</div>';
} else if (msg.content) {
inner += '<div class="paper-qa-msg-content">' +
(typeof renderMarkdown === 'function' ? renderMarkdown(msg.content) : escapeHtml(msg.content)) +
'</div>';
} else if (msg.status === 'running') {
inner += '<div class="paper-qa-msg-content paper-qa-thinking">' +
'<span class="thinking-dot"></span></div>';
}
return inner;
}
function _renderPaperQA() {
var container = document.getElementById('paperQAMessages');
if (!container) return;
if (!_paperQAHistory || _paperQAHistory.length === 0) {
var _ttq = (typeof t === 'function') ? t : function(k){ return k; };
container.innerHTML =
'<div class="paper-qa-empty"><div class="paper-qa-empty-icon">' + Icon('messageCircle', 32) + '</div>' +
'<p>' + escapeHtml(_ttq('paper.qaEmptyTitle')) + '</p>' +
'<p class="paper-qa-hint">' + escapeHtml(_ttq('paper.qaEmptyHint')) + '</p></div>';
return;
}
var first = container.firstElementChild;
if (first && !first.classList.contains('paper-qa-msg')) container.innerHTML = '';
var nearBottom = (container.scrollHeight - container.scrollTop - container.clientHeight) < 80;
var changed = false;
while (container.children.length > _paperQAHistory.length) {
container.removeChild(container.lastElementChild);
changed = true;
}
for (var j = 0; j < _paperQAHistory.length; j++) {
var msg = _paperQAHistory[j];
var cls = 'paper-qa-msg ' + (msg.role === 'user' ? 'paper-qa-user' : 'paper-qa-assistant');
var inner = _qaMsgInnerHtml(msg);
var node = container.children[j];
if (!node) {
node = document.createElement('div');
container.appendChild(node);
}
if (node._qaCls !== cls) { node.className = cls; node._qaCls = cls; }
if (node._qaSig !== inner) { node.innerHTML = inner; node._qaSig = inner; changed = true; }
}
if (changed && nearBottom) container.scrollTop = container.scrollHeight;
}
async function _ensurePaperText() {
if (_paperParsedText) return true;
var fname = _paperPdfFilename;
if (!fname && _paperPdfUrl) {
var m = /\/api\/paper\/pdf\/([^?#]+)/.exec(_paperPdfUrl);
if (m) fname = decodeURIComponent(m[1]);
}
if (!fname) return false;
try {
debugLog('[Paper] Re-parsing PDF to recover text…', 'info');
var data = await Api.paper.reparse(fname);
if (!data || !data.ok || !data.text) {
debugLog('[Paper] Re-parse failed: ' + (data.error || 'empty text'), 'warning');
return false;
}
_paperParsedText = data.text;
if (data.total_pages) _paperTotalPages = data.total_pages;
_saveActivePaperState();
debugLog('[Paper] Recovered ' + (data.text_length || data.text.length) + ' chars from PDF', 'success');
return true;
} catch (e) {
console.warn('[Paper] Re-parse request failed:', e);
debugLog('[Paper] Re-parse request failed: ' + (e.message || e), 'warning');
return false;
}
}
async function _sendPaperQuestion() {
var input = document.getElementById('paperQAInput');
var question = input?.value?.trim();
if (!question || _paperQAStreaming) return;
if (!_paperParsedText) {
var ok = await _ensurePaperText();
if (!ok) {
debugLog('No paper text available — PDF may be scanned or parsing failed', 'warning');
return;
}
}
var historyForServer = _paperQAHistory.slice(-10).map(function(m) {
return { role: m.role, content: m.content };
});
_paperQAHistory.push({ role: 'user', content: question, timestamp: Date.now() });
var asst = { role: 'assistant', content: '', timestamp: Date.now(),
toolRounds: [], status: 'running' };
_paperQAHistory.push(asst);
input.value = '';
_paperQAStreaming = true;
_renderPaperQA();
var startPaperId = _activePaperId;
try {
var startData = await Api.paper.qaStart({
question: question,
paper_text: _paperParsedText,
paper_hash: _paperHash || '',
lang: (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh') ? 'zh' : 'en',
history: historyForServer,
model: (typeof _paperReportModel !== 'undefined') ? _paperReportModel : undefined,
title: _paperFileName || '',
});
if (!startData || !startData.ok || !startData.task_id) {
throw new Error((startData && startData.error) || 'Q&A start failed');
}
await _pollQATask(startData.task_id, asst, startPaperId);
} catch (e) {
asst.status = 'error';
asst.content = (asst.content || '') + '\n\n' + Icon('alertTriangle', 14) + ' ' +
((typeof t === 'function') ? t('paper.qaError') : 'Error') + ': ' + (e.message || e);
_renderPaperQA();
console.warn('[Paper:QA] failed:', e);
} finally {
_paperQAStreaming = false; _paperQAAbort = null; _saveActivePaperState();
}
}
async function _pollQATask(taskId, asst, startPaperId) {
var cursor = 0;
var POLL_MS = 700;
while (true) {
if (_paperQAAbortRequested) { _paperQAAbortRequested = false; break; }
var resp = await Api.paper.qaPoll(taskId, cursor);
if (!resp || !resp.ok) {
if (resp && resp.status === 404) {
asst.status = 'error';
asst.content = asst.content ||
((typeof t === 'function') ? t('paper.qaExpired') : 'Q&A task expired.');
break;
}
throw new Error('HTTP ' + (resp ? resp.status : '?'));
}
var data = await resp.json();
if (!data.ok) throw new Error((typeof data.error === 'string' ? data.error : 'Poll failed'));
var events = data.events || [];
for (var i = 0; i < events.length; i++) _applyQAEvent(asst, events[i]);
cursor = data.next_cursor;
if (data.status === 'done') {
asst.status = 'done';
if (data.answer) asst.content = data.answer;
if (startPaperId === _activePaperId) _renderPaperQA();
break;
}
if (data.status === 'error') {
asst.status = 'error';
asst.content = (asst.content || '') + '\n\n' + Icon('alertTriangle', 14) + ' ' +
((typeof errorEnvelopeMessage === 'function') ? errorEnvelopeMessage(data.error) : (data.error || 'Error'));
if (startPaperId === _activePaperId) _renderPaperQA();
break;
}
if (startPaperId === _activePaperId) _renderPaperQA();
await new Promise(function(r) { setTimeout(r, POLL_MS); });
}
}
function _applyQAEvent(asst, ev) {
switch (ev.type) {
case 'tool_start':
asst.toolRounds.push({
roundNum: ev.roundNum, toolName: ev.toolName, query: ev.query,
toolCallId: ev.toolCallId, toolArgs: ev.toolArgs,
status: 'searching', results: null,
});
return;
case 'tool_done': {
for (var j = 0; j < asst.toolRounds.length; j++) {
var r = asst.toolRounds[j];
if (r.roundNum === ev.roundNum) {
r.status = 'done';
r._elapsed = (ev.elapsed != null) ? (ev.elapsed + 's') : r._elapsed;
r.toolContent = ev.toolContent || r.toolContent;
if (ev.results) r.results = ev.results;
if (ev.searchDiag) r.searchDiag = ev.searchDiag;
if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
if (ev.vertical) r.vertical = ev.vertical;
if (ev.verticals) r.verticals = ev.verticals;
break;
}
}
return;
}
case 'delta':
asst.content += (ev.delta || '');
return;
case 'delta_reset':
asst.content = '';
return;
default:
return;
}
}
function _quotePaperSelection() {
var sel = window.getSelection();
var text = sel?.toString()?.trim();
if (!text) return;
var input = document.getElementById('paperQAInput');
if (!input) return;
if (_paperActiveTab !== 'qa') _switchPaperTab('qa');
_setPaperMobileView('reader');
input.value = '> ' + text.replace(/\n/g, '\n> ') + '\n\n' + input.value;
input.focus();
sel.removeAllRanges();
_hidePaperQuoteBar();
}
function _askAboutPaperSelection() {
var sel = window.getSelection();
var text = sel?.toString()?.trim();
if (!text) return;
var input = document.getElementById('paperQAInput');
if (!input) return;
if (_paperActiveTab !== 'qa') _switchPaperTab('qa');
_setPaperMobileView('reader');
input.value = '> ' + text.replace(/\n/g, '\n> ') + '\n\nExplain this part of the paper.';
sel.removeAllRanges();
_hidePaperQuoteBar();
setTimeout(function() { _sendPaperQuestion(); }, 100);
}
function _hidePaperQuoteBar() {
var q = document.getElementById('paperQuoteBtn');
if (q) q.style.display = 'none';
var qr = document.getElementById('paperReportQuoteBtn');
if (qr) qr.style.display = 'none';
}
function _handlePaperTextSelection() {
var sel = window.getSelection();
var text = sel?.toString()?.trim();
var q = document.getElementById('paperQuoteBtn');
var qr = document.getElementById('paperReportQuoteBtn');
if (qr) qr.style.display = 'none';
if (q) q.style.display = 'none';
if (!text || text.length < 3) return;
var viewer = document.getElementById('paperPdfViewer');
if (q && viewer && viewer.contains(sel.anchorNode)) {
var range = sel.getRangeAt(0);
var rect = range.getBoundingClientRect();
var leftEl = document.querySelector('.paper-left');
if (!leftEl) return;
var lr = leftEl.getBoundingClientRect();
q.style.display = 'flex';
q.style.top = (rect.top - lr.top - 40) + 'px';
q.style.left = Math.max(4, rect.left - lr.left + rect.width / 2 - 80) + 'px';
return;
}
var reportEl = document.getElementById('paperReportContent');
if (qr && reportEl && reportEl.contains(sel.anchorNode)) {
var rrange = sel.getRangeAt(0);
var rrect = rrange.getBoundingClientRect();
var rightEl = document.querySelector('.paper-right');
if (!rightEl) return;
var rr = rightEl.getBoundingClientRect();
qr.style.display = 'flex';
qr.style.top = Math.max(4, rrect.top - rr.top - 40) + 'px';
qr.style.left = Math.max(4, rrect.left - rr.left + rrect.width / 2 - 80) + 'px';
}
}
function _setReportRegenIntent(paperHash, lang, key) {
key = key || _REPORT_REGEN_INTENT_KEY;
try {
if (!paperHash) { localStorage.removeItem(key); return; }
localStorage.setItem(key,
JSON.stringify({ paperHash: paperHash, lang: lang || 'en', ts: Date.now() }));
} catch (e) {
console.warn('[Paper:Report] persist regen intent failed:', e);
}
}
function _getReportRegenIntent(key) {
try {
var raw = localStorage.getItem(key || _REPORT_REGEN_INTENT_KEY);
if (!raw) return null;
return JSON.parse(raw);
} catch (e) {
console.warn('[Paper:Report] read regen intent failed:', e);
return null;
}
}
function _clearReportRegenIntent(key) {
try { localStorage.removeItem(key || _REPORT_REGEN_INTENT_KEY); }
catch (e) { console.warn('[Paper:Report] clear regen intent failed:', e); }
}
function _hasReportRegenIntent(paperHash, lang, key) {
var it = _getReportRegenIntent(key);
return !!(it && it.paperHash && it.paperHash === paperHash
&& (it.lang || 'en') === (lang || 'en'));
}
function _resetReportLocalState(view) {
view = view || _reportView('report');
if (view.stream && view.stream.pollTimer) {
clearTimeout(view.stream.pollTimer);
}
view.stream = null;
view.meta = null;
if (view.kind === 'review') {
_paperReviewShowTranslation = false;
_paperReviewTranslatedText = '';
_paperReviewTranslating = false;
}
if (typeof _teardownReadingTracker === 'function') _teardownReadingTracker(true);
}
function _resetAllReportViews() {
_resetReportSnapshots();
_resetReportLocalState(_reportView('report'));
_resetReportLocalState(_reportView('review'));
}
function _makeReportStreamState(paperId, lang, taskId, kind) {
return {
paperId: paperId || '',
lang: lang || 'en',
kind: kind || 'report',
taskId: taskId || '',
cursor: 0,
status: 'running',
pendingStop: false,
fullText: '',
thinkingText: '',
toolRounds: [],
contentStarted: false,
insightText: '',
_insightRunning: false,
_insightApplied: false,
meta: null,
error: '',
pollTimer: null,
pollBusy: false,
_lastRenderedLen: -1,
_lastRenderedStatus: '',
_lastToolKey: '',
};
}
function _renderReportSkeleton(container, lang, view) {
view = view || _reportView('report');
var px = view.idPrefix;
var genTxt = view.kind === 'review'
? (lang === 'zh' ? '正在生成评审…' : 'Generating review…')
: (lang === 'zh' ? '正在生成报告…' : 'Generating report…');
container.innerHTML =
'<div class="paper-report-tools" id="' + px + 'ToolZone"></div>' +
'<details class="paper-report-thinking" id="' + px + 'ThinkingBlock" open style="display:none">' +
'<summary><span class="thinking-dot"></span>' +
(lang === 'zh' ? '思考中…' : 'Thinking…') +
'</summary>' +
'<div class="paper-report-thinking-body" id="' + px + 'ThinkingBody"></div>' +
'</details>' +
'<div class="paper-report-body" id="' + px + 'BodyContent">' +
'<div class="paper-loading"><div class="paper-loading-spinner"></div><div>' +
genTxt +
'</div></div>' +
'</div>';
}
function _applyReportEvent(s, ev) {
switch (ev.type) {
case 'status':
s.status = ev.status || s.status;
return true;
case 'thinking':
s.thinkingText += (ev.delta || '');
return true;
case 'tool_start': {
s.toolRounds.push({
roundNum: ev.roundNum,
toolName: ev.toolName,
query: ev.query || ev.toolName,
toolCallId: ev.toolCallId || '',
toolArgs: ev.toolArgs || '',
status: 'searching',
results: null,
});
return true;
}
case 'tool_done': {
var r = null;
for (var i = 0; i < s.toolRounds.length; i++) {
if (s.toolRounds[i].roundNum === ev.roundNum) { r = s.toolRounds[i]; break; }
}
if (r) {
r.status = 'done';
if (typeof ev.elapsed === 'number') r._elapsed = ev.elapsed.toFixed(1) + 's';
if (ev.toolContent) r.toolContent = ev.toolContent;
if (ev.results) r.results = ev.results;
if (ev.searchDiag) r.searchDiag = ev.searchDiag;
if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
if (ev.vertical) r.vertical = ev.vertical;
if (ev.verticals) r.verticals = ev.verticals;
}
return true;
}
case 'tool_progress': {
var rp = null;
for (var j = 0; j < s.toolRounds.length; j++) {
if (s.toolRounds[j].roundNum === ev.roundNum) { rp = s.toolRounds[j]; break; }
}
if (rp) {
if (typeof rp._partialOutput !== 'string') rp._partialOutput = '';
rp._partialOutput += (ev.chunk || '');
}
return true;
}
case 'delta':
s.fullText += (ev.delta || '');
s.contentStarted = true;
return true;
case 'delta_reset':
s.fullText = '';
s.contentStarted = false;
s._lastRenderedLen = -1;
return true;
case 'enriched':
s.fullText = ev.text || s.fullText;
if (ev.paperHash && s.paperId === _activePaperId) _paperHash = ev.paperHash;
return true;
case 'insight_start':
s._insightRunning = true;
return true;
case 'insight': {
s._insightRunning = false;
var _ins = ev.insight || '';
if (_ins && !s._insightApplied) {
s._insightApplied = true;
s.insightText = _ins;
s.fullText = (s.fullText || '').replace(/\s*$/, '') + '\n\n' + _ins + '\n';
s._lastRenderedLen = -1;
if (s.paperId === _activePaperId) {
var _vIns = _reportView(s.kind);
if (_vIns.cache) {
_vIns.cache = _vIns.cache.replace(/\s*$/, '') + '\n\n' + _ins + '\n';
_rememberReportSnapshot(_vIns, _vIns.cache, _vIns.meta || s.meta);
}
}
}
return true;
}
case 'insight_skipped':
s._insightRunning = false;
return true;
case 'done': {
s.status = 'done';
var _vDone = _reportView(s.kind);
if (ev.report) {
s.fullText = ev.report;
if (s.paperId === _activePaperId) {
_vDone.cache = ev.report;
_rememberReportSnapshot(_vDone, ev.report, ev.meta || s.meta);
}
}
if (ev.meta) {
s.meta = ev.meta;
if (s.paperId === _activePaperId) _vDone.meta = ev.meta;
}
if (ev.paperHash && s.paperId === _activePaperId) _paperHash = ev.paperHash;
if (ev.resolvedTitle) _applyResolvedTitle(ev.resolvedTitle, s.paperId);
return true;
}
case 'aborted':
s.status = 'aborted';
if (typeof ev.partial === 'string' && ev.partial) {
s.fullText = ev.partial;
s.contentStarted = true;
}
return true;
case 'error':
s.status = 'error';
s._errorEnv = (typeof normalizeErrorEnvelope === 'function')
? normalizeErrorEnvelope(ev.error)
: null;
s.error = (typeof errorEnvelopeMessage === 'function'
? errorEnvelopeMessage(ev.error) : '')
|| (typeof ev.error === 'string' ? ev.error : '')
|| 'Unknown error';
return true;
}
return false;
}
var _REPORT_CALLOUT_KEYWORDS = [
{ cls: 'takeaway', re: /^(key takeaway|takeaway|key point|key finding|summary|bottom line|关键结论|核心结论|要点|总结|小结)(?:[:：]|\b)/i },
{ cls: 'warning', re: /^(warning|caution|caveat|limitation|警告|注意|局限|风险)(?:[:：]|\b)/i },
{ cls: 'important', re: /^(important|critical|重要|关键)(?:[:：]|\b)/i },
{ cls: 'tip', re: /^(tip|pro tip|提示|建议)(?:[:：]|\b)/i },
{ cls: 'note', re: /^(note|nb|备注|说明)(?:[:：]|\b)/i },
];
function _slugifyHeading(text, used) {
var base = String(text || '')
.toLowerCase().trim()
.replace(/[^\w\u4e00-\u9fff\s-]/g, '')
.replace(/\s+/g, '-')
.replace(/-+/g, '-')
.replace(/^-|-$/g, '') || 'section';
var slug = base, n = 2;
while (used[slug]) { slug = base + '-' + n; n++; }
used[slug] = true;
return slug;
}
function _decorateCallouts(article) {
var quotes = article.querySelectorAll('blockquote');
for (var i = 0; i < quotes.length; i++) {
var bq = quotes[i];
if (bq.closest('.paper-callout')) continue;
var lead = (bq.textContent || '').trimStart();
var match = null;
for (var k = 0; k < _REPORT_CALLOUT_KEYWORDS.length; k++) {
if (_REPORT_CALLOUT_KEYWORDS[k].re.test(lead)) { match = _REPORT_CALLOUT_KEYWORDS[k]; break; }
}
if (!match) continue;
bq.classList.add('paper-callout', 'paper-callout-' + match.cls);
}
}
function _frameFigures(article) {
var imgs = article.querySelectorAll('img');
for (var i = 0; i < imgs.length; i++) {
var img = imgs[i];
if (img.closest('figure')) continue;
var p = img.closest('p');
if (!p) continue;
var hasOtherText = (p.textContent || '').trim().length > 0
&& !p.querySelector('em') && !img.getAttribute('alt');
if (hasOtherText) continue;
var fig = document.createElement('figure');
fig.className = 'paper-figure';
fig.appendChild(img.cloneNode(true));
var capText = '';
var em = p.querySelector('em');
if (em && em.textContent.trim()) capText = em.textContent.trim();
else if (img.getAttribute('alt')) capText = img.getAttribute('alt').trim();
if (capText) {
var cap = document.createElement('figcaption');
cap.textContent = capText;
fig.appendChild(cap);
}
p.parentNode.replaceChild(fig, p);
}
}
function _cellPlainText(cell) {
if (!cell) return '';
var txt;
try {
var clone = cell.cloneNode(true);
var dup = clone.querySelectorAll('.katex-mathml, annotation');
for (var i = 0; i < dup.length; i++) {
if (dup[i].parentNode) dup[i].parentNode.removeChild(dup[i]);
}
txt = clone.textContent || '';
} catch (e) {
txt = cell.textContent || '';
}
return txt.replace(/\s+/g, ' ').trim();
}
function _extractGlossary(article) {
var tables = article.querySelectorAll('table');
for (var ti = 0; ti < tables.length; ti++) {
var table = tables[ti];
var head = table.querySelector('thead th, tr:first-child th');
var first = (head && head.textContent || '').trim().toLowerCase();
if (first !== 'term' && first.indexOf('术语') < 0) continue;
table.classList.add('paper-glossary');
var rows = table.querySelectorAll('tbody tr');
var out = [];
for (var ri = 0; ri < rows.length; ri++) {
var cells = rows[ri].querySelectorAll('td');
if (cells.length < 2) continue;
var term = (cells[0].textContent || '').trim();
var defCell = cells[1];
var defHtml = (defCell.innerHTML || '').trim();
var defText = _cellPlainText(defCell);
if (!term || !defText) continue;
if (/^[(（].*[)）]$/.test(term) || term === '...' || term === '…') continue;
if (defText.length > 260) defText = defText.slice(0, 257) + '…';
out.push({ term: term, def: defText, defHtml: defHtml });
}
return out;
}
return [];
}
function _glossaryAliases(term) {
var raw = [];
raw.push(term);
var base = term.replace(/[(（][^)）]*[)）]/g, '').trim();
raw.push(base);
var paren = term.match(/[(（]([^)）]+)[)）]/);
if (paren) {
var inner = paren[1].trim();
if (!/本文首创|首创|借鉴|新增|高\/低|效用|introduced|borrowed|coined/i.test(inner)) raw.push(inner);
}
base.split(/\s*[\/、，]\s*/).forEach(function (p) { raw.push(p); });
var seen = {}, out = [];
for (var i = 0; i < raw.length; i++) {
var a = (raw[i] || '').trim();
if (!a) continue;
var key = a.toLowerCase();
if (seen[key]) continue;
var hasCjk = /[\u3400-\u4dbf\u4e00-\u9fff]/.test(a);
var isAbbrev = /^[A-Z0-9][A-Z0-9@+\-]{1,}$/.test(a);
if (hasCjk) { if (a.length < 2) continue; }
else if (!isAbbrev && a.length < 3) continue;
seen[key] = true;
out.push(a);
}
return out;
}
function _escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
function _decorateGlossaryTerms(article, glossary) {
if (!glossary || !glossary.length || typeof document === 'undefined') return;
var map = {}, aliases = [];
for (var r = 0; r < glossary.length; r++) {
var al = _glossaryAliases(glossary[r].term);
for (var j = 0; j < al.length; j++) {
var key = al[j].toLowerCase();
if (map[key]) continue;
map[key] = { row: r, def: glossary[r].def, defHtml: glossary[r].defHtml };
aliases.push(al[j]);
}
}
if (!aliases.length) return;
aliases.sort(function (a, b) { return b.length - a.length; });
var re;
try {
re = new RegExp(aliases.map(_escapeRegExp).join('|'), 'gi');
} catch (e) {
console.warn('[Paper:Glossary] regex build failed:', e);
return;
}
var SKIP = { H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1, CODE: 1, PRE: 1,
A: 1, FIGCAPTION: 1, SCRIPT: 1, STYLE: 1, BUTTON: 1 };
var seen = {};
function decorateText(node) {
var text = node.nodeValue;
if (!text || text.length < 2 || !/\S/.test(text)) return;
re.lastIndex = 0;
var picks = [], m, pos = 0;
while ((m = re.exec(text))) {
var matched = m[0], idx = m.index, key = matched.toLowerCase();
var entry = map[key];
if (re.lastIndex === idx) re.lastIndex++;
if (!entry || seen[entry.row]) continue;
var headLatin = /[A-Za-z0-9]/.test(matched.charAt(0));
var tailLatin = /[A-Za-z0-9]/.test(matched.charAt(matched.length - 1));
if (headLatin && idx > 0 && /[A-Za-z0-9]/.test(text.charAt(idx - 1))) continue;
if (tailLatin && /[A-Za-z0-9]/.test(text.charAt(idx + matched.length))) continue;
if (idx < pos) continue;
picks.push({ idx: idx, len: matched.length, text: matched, def: entry.def, defHtml: entry.defHtml });
seen[entry.row] = true;
pos = idx + matched.length;
}
if (!picks.length) return;
var frag = document.createDocumentFragment(), cursor = 0;
for (var p = 0; p < picks.length; p++) {
var pk = picks[p];
if (pk.idx > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, pk.idx)));
var span = document.createElement('span');
span.className = 'paper-term';
span.setAttribute('tabindex', '0');
span.setAttribute('aria-label', pk.text + ': ' + pk.def);
span.appendChild(document.createTextNode(pk.text));
var card = document.createElement('span');
card.className = 'paper-term-card';
card.setAttribute('aria-hidden', 'true');
if (pk.defHtml) card.innerHTML = pk.defHtml;
else card.textContent = pk.def;
span.appendChild(card);
frag.appendChild(span);
cursor = pk.idx + pk.len;
}
if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
node.parentNode.replaceChild(frag, node);
}
function walk(node) {
var children = Array.prototype.slice.call(node.childNodes);
for (var i = 0; i < children.length; i++) {
var c = children[i];
if (c.nodeType === 3) { decorateText(c); continue; }
if (c.nodeType !== 1) continue;
var tag = c.tagName;
if (tag === 'H1' || tag === 'H2') seen = {};
if (SKIP[tag]) continue;
if (c.classList && (c.classList.contains('paper-glossary') ||
c.classList.contains('paper-term') || c.classList.contains('katex'))) continue;
walk(c);
}
}
try { walk(article); }
catch (e) { console.warn('[Paper:Glossary] decoration failed:', e); }
}
function _indexHeadings(article) {
var heads = article.querySelectorAll('h2, h3');
var used = {}, entries = [];
for (var i = 0; i < heads.length; i++) {
var h = heads[i];
var text = (h.textContent || '').trim();
if (!text) continue;
if (!h.id) h.id = 'report-' + _slugifyHeading(text, used);
entries.push({ id: h.id, text: text, level: h.tagName === 'H3' ? 3 : 2 });
}
return entries;
}
function _buildReportTOC(entries) {
if (entries.length < 3) return '';
var label = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh') ? '目录' : 'Contents';
var html = '<nav class="paper-report-toc" aria-label="' + label + '">'
+ '<div class="paper-report-toc-title">' + label + '</div><ul>';
for (var i = 0; i < entries.length; i++) {
var e = entries[i];
html += '<li class="toc-l' + e.level + '"><a href="#' + e.id + '" data-target="' + e.id
+ '" onclick="_scrollReportToHeading(event,\'' + e.id + '\')">' + escapeHtml(e.text) + '</a></li>';
}
html += '</ul></nav>';
return html;
}
function _scrollReportToHeading(ev, id) {
if (ev) ev.preventDefault();
var el = document.getElementById(id);
if (!el) return;
var _rm = (typeof prefersReducedMotion === 'function') && prefersReducedMotion();
var _behavior = _rm ? 'auto' : 'smooth';
var scroller = el.closest('.paper-report-content, .paper-report-body');
if (!scroller) { el.scrollIntoView({ behavior: _behavior, block: 'start' }); return; }
var TOP_MARGIN = 16;
var target = scroller.scrollTop
+ (el.getBoundingClientRect().top - scroller.getBoundingClientRect().top) - TOP_MARGIN;
scroller.scrollTo({ top: Math.max(0, target), behavior: _behavior });
}
function _wireReportScrollSpy(scrollEl, article, toc) {
if (!scrollEl || !toc || typeof IntersectionObserver === 'undefined') return;
var links = {};
toc.querySelectorAll('a[data-target]').forEach(function(a) { links[a.getAttribute('data-target')] = a; });
var heads = article.querySelectorAll('h2, h3');
if (!heads.length) return;
var visible = {};
var obs = new IntersectionObserver(function(items) {
items.forEach(function(it) { visible[it.target.id] = it.isIntersecting; });
var firstActive = null;
for (var i = 0; i < heads.length; i++) { if (visible[heads[i].id]) { firstActive = heads[i].id; break; } }
Object.keys(links).forEach(function(k) { links[k].classList.toggle('active', k === firstActive); });
}, { root: scrollEl, rootMargin: '0px 0px -70% 0px', threshold: 0 });
for (var i = 0; i < heads.length; i++) obs.observe(heads[i]);
if (scrollEl._reportSpyObs) { try { scrollEl._reportSpyObs.disconnect(); } catch (e) {} }
scrollEl._reportSpyObs = obs;
}
function _renderCitationAuditCard(audit) {
if (!audit || !audit.suspicious || !audit.suspicious.length) return '';
var zh = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh');
var c = audit.counts || {};
var n = audit.suspicious.length;
var title = zh
? ('引用完整性警告 — ' + n + ' 条可疑引用')
: ('Citation integrity — ' + n + ' suspicious reference' + (n === 1 ? '' : 's'));
var sub = zh
? ('已核验 ' + (audit.total || 0) + ' 条引用标识符：' +
(c.verified || 0) + ' 条确认存在、' + (c.suspicious || 0) + ' 条可疑、' +
(c.unverifiable || 0) + ' 条无法核验（无法核验 ≠ 虚构）。')
: ('Checked ' + (audit.total || 0) + ' cited identifiers: ' +
(c.verified || 0) + ' verified, ' + (c.suspicious || 0) + ' suspicious, ' +
(c.unverifiable || 0) + ' unverifiable (unverifiable ≠ fabricated).');
var rows = audit.suspicious.map(function (it) {
var idLabel = escapeHtml((it.kind || '') + ' ' + (it.identifier || ''));
var reason = escapeHtml(it.reason || (zh ? '未能解析' : 'did not resolve'));
var checked = it.checked
? ('<a class="paper-cite-checked" href="' + escapeHtml(it.checked) +
'" target="_blank" rel="noopener noreferrer">' +
escapeHtml(zh ? '查证来源' : 'checked source') + '</a>')
: '';
var titles = '';
if (it.matchedTitle && it.claimedTitle) {
titles = '<div class="paper-cite-titles">' +
'<span class="paper-cite-claimed">' + escapeHtml(zh ? '声称：' : 'claimed: ') +
escapeHtml(it.claimedTitle) + '</span>' +
'<span class="paper-cite-matched">' + escapeHtml(zh ? '实际解析为：' : 'resolves to: ') +
escapeHtml(it.matchedTitle) + '</span></div>';
}
return '<li class="paper-cite-item">' +
'<code class="paper-cite-id">' + idLabel + '</code>' +
'<span class="paper-cite-reason">' + reason + '</span>' +
checked + titles + '</li>';
}).join('');
return '<aside class="paper-citation-audit" role="alert">' +
'<div class="paper-cite-head">' +
'<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
'<span class="paper-cite-title">' + escapeHtml(title) + '</span>' +
'</div>' +
'<p class="paper-cite-sub">' + escapeHtml(sub) + '</p>' +
'<ul class="paper-cite-list">' + rows + '</ul>' +
'</aside>';
}
function _renderReportFinishTag(meta) {
if (!meta || !meta.model) return '';
var zh = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh');
var parts = [];
parts.push('<span class="paper-finish-model" title="' +
escapeHtml(zh ? '生成本报告的模型' : 'Model that generated this report') + '">' +
'<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v1a3 3 0 0 0-3 3 3 3 0 0 0 0 6 3 3 0 0 0 3 3v1a3 3 0 0 0 6 0v-1a3 3 0 0 0 3-3 3 3 0 0 0 0-6 3 3 0 0 0-3-3V5a3 3 0 0 0-3-3z"/></svg>' +
escapeHtml(meta.model) + '</span>');
var costStr = '';
if (typeof meta.costCny === 'number' && meta.costCny > 0) {
costStr = (typeof formatCny === 'function') ? formatCny(meta.costCny)
: ('¥' + meta.costCny.toFixed(4));
} else if (typeof meta.costUsd === 'number' && meta.costUsd > 0) {
costStr = '$' + meta.costUsd.toFixed(4);
}
if (costStr) {
parts.push('<span class="paper-finish-cost" title="' +
escapeHtml(zh ? '本次生成的预估费用' : 'Estimated cost of this generation') +
'">' + escapeHtml(costStr) + '</span>');
}
var inTok = meta.promptTokens || 0;
var outTok = meta.completionTokens || 0;
if (inTok || outTok) {
var fmt = function (n) {
if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
return String(n);
};
parts.push('<span class="paper-finish-tokens" title="' +
escapeHtml(zh ? '输入 / 输出 tokens' : 'input / output tokens') + '">' +
fmt(inTok) + ' \u2192 ' + fmt(outTok) + ' tok</span>');
}
var label = zh ? '由以下模型生成' : 'Generated by';
return '<div class="paper-report-finish-tag" role="contentinfo">' +
'<span class="paper-finish-label">' + escapeHtml(label) + '</span>' +
parts.join('') + '</div>';
}
var _READ_SPEED_KEY = 'paper_reading_wpm_v1';
var _READ_WPM_DEFAULT = 220;
var _READ_WPM_MIN = 60;
var _READ_WPM_MAX = 700;
var _READ_EWMA_ALPHA = 0.25;
var _READ_CJK_CHAR_TO_WORD = 0.6;
var _readTracker = null;
function _loadReadingWpm() {
try {
var raw = localStorage.getItem(_READ_SPEED_KEY);
if (!raw) return { wpm: _READ_WPM_DEFAULT, samples: 0 };
var o = JSON.parse(raw);
var wpm = Number(o && o.wpm);
if (!isFinite(wpm) || wpm <= 0) return { wpm: _READ_WPM_DEFAULT, samples: 0 };
return { wpm: Math.max(_READ_WPM_MIN, Math.min(_READ_WPM_MAX, wpm)),
samples: (o && o.samples) | 0 };
} catch (e) {
console.warn('[Paper:ReadTime] load wpm failed:', e);
return { wpm: _READ_WPM_DEFAULT, samples: 0 };
}
}
function _recordReadingObservation(observedWpm) {
if (!isFinite(observedWpm) || observedWpm <= 0) return;
observedWpm = Math.max(_READ_WPM_MIN, Math.min(_READ_WPM_MAX, observedWpm));
var cur = _loadReadingWpm();
var next;
if (cur.samples <= 0) {
next = _READ_WPM_DEFAULT * 0.5 + observedWpm * 0.5;
} else {
next = cur.wpm * (1 - _READ_EWMA_ALPHA) + observedWpm * _READ_EWMA_ALPHA;
}
next = Math.max(_READ_WPM_MIN, Math.min(_READ_WPM_MAX, next));
try {
localStorage.setItem(_READ_SPEED_KEY, JSON.stringify({
wpm: Math.round(next), samples: cur.samples + 1, updatedAt: Date.now(),
}));
} catch (e) {
console.warn('[Paper:ReadTime] persist wpm failed:', e);
}
}
function _countReadingWords(el) {
var text = '';
if (el) {
if (el.querySelector && el.querySelector('.paper-term-card')) {
try {
var clone = el.cloneNode(true);
var cards = clone.querySelectorAll('.paper-term-card');
for (var i = 0; i < cards.length; i++) {
if (cards[i].parentNode) cards[i].parentNode.removeChild(cards[i]);
}
text = clone.textContent || '';
} catch (e) { text = el.textContent || ''; }
} else {
text = el.textContent || '';
}
}
if (!text) return 0;
var cjk = (text.match(/[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]/g) || []).length;
var latin = text.replace(/[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]/g, ' ');
var latinWords = (latin.match(/[A-Za-z0-9][A-Za-z0-9'\u2019-]*/g) || []).length;
return latinWords + cjk * _READ_CJK_CHAR_TO_WORD;
}
function _formatReadMinutes(min) {
var _tt = (typeof t === 'function') ? t : function(k, p){ return k; };
if (min < 1) return _tt('paper.readTimeLessMin');
if (min < 60) return _tt('paper.readTimeMin', { n: Math.round(min) });
var h = Math.floor(min / 60);
var m = Math.round(min - h * 60);
return _tt('paper.readTimeHour', { h: h, m: m });
}
function _buildReadingTimeBar(article, scroller) {
var words = _countReadingWords(article);
var model = _loadReadingWpm();
var totalMin = words / model.wpm;
var _tt = (typeof t === 'function') ? t : function(k, p){ return k; };
var bar = document.createElement('div');
bar.className = 'paper-read-time';
bar.setAttribute('role', 'progressbar');
bar.setAttribute('aria-valuemin', '0');
bar.setAttribute('aria-valuemax', '100');
var calib = (model.samples > 0)
? _tt('paper.readTimeAdapted', { wpm: Math.round(model.wpm) })
: _tt('paper.readTimeDefault');
bar.innerHTML =
'<div class="paper-read-time-row">' +
'<span class="paper-read-time-icon" title="' + escapeHtml(calib) + '">' +
'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg>' +
'</span>' +
'<span class="paper-read-time-total"></span>' +
'<span class="paper-read-time-sep">·</span>' +
'<span class="paper-read-time-left"></span>' +
(model.samples > 0 ? '<span class="paper-read-time-badge" title="' + escapeHtml(calib) + '">' +
'<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></span>' : '') +
'</div>' +
'<div class="paper-read-time-track"><div class="paper-read-time-fill"></div></div>';
bar._readWords = words;
bar._readTotalMin = totalMin;
return bar;
}
function _wireReadingTimeTracking(bar, scroller, view) {
if (!bar || !scroller) return;
_teardownReadingTracker(true);
view = view || _reportView('report');
var totalEl = bar.querySelector('.paper-read-time-total');
var leftEl = bar.querySelector('.paper-read-time-left');
var fillEl = bar.querySelector('.paper-read-time-fill');
var totalMin = bar._readTotalMin || 0;
var words = bar._readWords || 0;
if (totalEl) totalEl.textContent =
(typeof t === 'function' ? t('paper.readTimeTotal', { min: _formatReadMinutes(totalMin) })
: _formatReadMinutes(totalMin));
var tracker = {
bar: bar, scroller: scroller, words: words, totalMin: totalMin,
view: view,
lastProgress: 0,
activeMs: 0,
lastTickTs: 0,
lastPersistTs: 0,
flushed: false,
onScroll: null,
rafPending: false,
};
function _progressFraction() {
var max = scroller.scrollHeight - scroller.clientHeight;
if (max <= 0) return 1;
return Math.max(0, Math.min(1, scroller.scrollTop / max));
}
function _paint(frac) {
if (fillEl) fillEl.style.width = (frac * 100).toFixed(1) + '%';
bar.setAttribute('aria-valuenow', Math.round(frac * 100));
var remainMin = totalMin * (1 - frac);
if (leftEl) {
if (frac >= 0.999) {
leftEl.textContent = (typeof t === 'function') ? t('paper.readTimeDone') : 'Finished';
bar.classList.add('done');
} else {
bar.classList.remove('done');
leftEl.textContent = (typeof t === 'function')
? t('paper.readTimeLeft', { min: _formatReadMinutes(remainMin) })
: _formatReadMinutes(remainMin);
}
}
}
function _tick() {
tracker.rafPending = false;
var now = Date.now();
var frac = _progressFraction();
if (tracker.lastTickTs && (now - tracker.lastTickTs) < 12000) {
tracker.activeMs += (now - tracker.lastTickTs);
}
tracker.lastTickTs = now;
if (frac > tracker.lastProgress) tracker.lastProgress = frac;
_paint(Math.max(frac, 0));
if (now - tracker.lastPersistTs >= 1000) {
tracker.lastPersistTs = now;
_persistReadingPosition(tracker.view, _captureReadingAnchor(scroller));
}
}
tracker.onScroll = function() {
if (tracker.rafPending) return;
tracker.rafPending = true;
requestAnimationFrame(_tick);
};
scroller.addEventListener('scroll', tracker.onScroll, { passive: true });
_readTracker = tracker;
_paint(_progressFraction());
}
function _teardownReadingTracker(silent) {
var tk = _readTracker;
_readTracker = null;
if (!tk || tk.flushed) return;
tk.flushed = true;
try {
if (tk.scroller && tk.onScroll) tk.scroller.removeEventListener('scroll', tk.onScroll);
} catch (e) {  }
try {
if (tk.scroller) _persistReadingPosition(tk.view, _captureReadingAnchor(tk.scroller));
} catch (e) { console.debug('[Paper] persist final reading-pos failed: %s', e && e.message); }
var coveredWords = tk.words * tk.lastProgress;
var activeMin = tk.activeMs / 60000;
if (coveredWords >= 120 && activeMin >= (20 / 60)) {
var observedWpm = coveredWords / activeMin;
_recordReadingObservation(observedWpm);
if (!silent) {
console.debug('[Paper:ReadTime] session: %d words / %.2f min → %d wpm',
Math.round(coveredWords), activeMin, Math.round(observedWpm));
}
}
}
function _renderFinalReport(container, text, meta, view) {
if (!container) return;
view = view || _reportView('report');
if (typeof _syncReportToolbar === 'function') _syncReportToolbar(false, view);
if (meta === undefined) meta = view.meta;
if (typeof renderMarkdown !== 'function') {
container.innerHTML = '<pre>' + escapeHtml(text || '') + '</pre>';
return;
}
if (container._reportSpyObs) { try { container._reportSpyObs.disconnect(); } catch (e) {} container._reportSpyObs = null; }
var _readAnchor = _captureReadingAnchor(container) || _loadReadingPosition(view);
var article = document.createElement('article');
article.className = 'paper-report-article';
article.innerHTML = renderMarkdown(text || '');
if (meta && meta.citationAudit) {
var auditHtml = _renderCitationAuditCard(meta.citationAudit);
if (auditHtml) article.insertAdjacentHTML('afterbegin', auditHtml);
}
_decorateCallouts(article);
_frameFigures(article);
_decorateZoomableImages(article);
_decorateGlossaryTerms(article, _extractGlossary(article));
var finishTag = _renderReportFinishTag(meta);
if (finishTag) {
var tagWrap = document.createElement('div');
tagWrap.innerHTML = finishTag;
if (tagWrap.firstChild) article.appendChild(tagWrap.firstChild);
}
var entries = _indexHeadings(article);
var tocHTML = _buildReportTOC(entries);
container.classList.add('paper-report-enhanced');
var readBar = _buildReadingTimeBar(article, container);
if (tocHTML) {
var doc = document.createElement('div');
doc.className = 'paper-report-doc';
doc.innerHTML = tocHTML;
doc.appendChild(article);
container.innerHTML = '';
if (readBar) container.appendChild(readBar);
container.appendChild(doc);
_wireReportScrollSpy(container, article, doc.querySelector('.paper-report-toc'));
} else {
container.innerHTML = '';
if (readBar) container.appendChild(readBar);
container.appendChild(article);
}
_restoreReadingAnchor(container, article, _readAnchor);
if (readBar) _wireReadingTimeTracking(readBar, container, view);
}
function _captureReadingAnchor(scroller) {
try {
if (!scroller || scroller.scrollTop <= 2) return null;
var heads = scroller.querySelectorAll('.paper-report-article h2, .paper-report-article h3');
if (!heads.length) {
var max = scroller.scrollHeight - scroller.clientHeight;
return max > 0 ? { frac: scroller.scrollTop / max } : null;
}
var sTop = scroller.getBoundingClientRect().top;
var best = 0, bestAbove = -Infinity;
for (var i = 0; i < heads.length; i++) {
var rel = heads[i].getBoundingClientRect().top - sTop;
if (rel <= 1 && rel > bestAbove) { bestAbove = rel; best = i; }
}
var relTop = heads[best].getBoundingClientRect().top - sTop;
return { index: best, offset: relTop };
} catch (e) {
console.debug('[Paper] captureReadingAnchor failed: %s', e && e.message);
return null;
}
}
function _restoreReadingAnchor(scroller, article, anchor) {
if (!scroller || !anchor) return;
try {
if (anchor.frac != null) {
var max = scroller.scrollHeight - scroller.clientHeight;
scroller.scrollTop = Math.max(0, Math.round(anchor.frac * max));
return;
}
var heads = article.querySelectorAll('h2, h3');
if (!heads.length || anchor.index == null) return;
var idx = Math.min(anchor.index, heads.length - 1);
var sTop = scroller.getBoundingClientRect().top;
var headTop = heads[idx].getBoundingClientRect().top - sTop + scroller.scrollTop;
scroller.scrollTop = Math.max(0, Math.round(headTop - (anchor.offset || 0)));
} catch (e) {
console.debug('[Paper] restoreReadingAnchor failed: %s', e && e.message);
}
}
function _readReadPosMap() {
try {
var raw = localStorage.getItem(_PAPER_READ_POS_KEY);
return raw ? (JSON.parse(raw) || {}) : {};
} catch (e) {
console.warn('[Paper] read reading-position map failed:', e);
return {};
}
}
function _persistReadingPosition(view, anchor) {
view = view || _reportView('report');
if (!_activePaperId) return;
var key = _reportSnapshotKey(view);
try {
var map = _readReadPosMap();
if (anchor) map[key] = anchor;
else delete map[key];
localStorage.setItem(_PAPER_READ_POS_KEY, JSON.stringify(map));
} catch (e) {
console.warn('[Paper] persist reading-position failed:', e);
}
}
function _loadReadingPosition(view) {
view = view || _reportView('report');
if (!_activePaperId) return null;
return _readReadPosMap()[_reportSnapshotKey(view)] || null;
}
function _paintReportFromState(view) {
view = view || _reportView('report');
var container = document.getElementById(view.containerId);
if (!container || !view.stream) return;
var s = view.stream;
var px = view.idPrefix;
var retryFn = view.kind === 'review' ? '_generatePaperReview()' : '_generatePaperReport()';
_syncReportToolbar(s.status === 'running', view);
if (s.status === 'done' && s.fullText && !s.toolRounds.some(r => r.status === 'searching')) {
if (s._lastRenderedLen !== s.fullText.length || s._lastRenderedStatus !== 'done') {
_renderFinalReport(container, s.fullText, undefined, view);
s._lastRenderedLen = s.fullText.length;
s._lastRenderedStatus = 'done';
}
return;
}
if (s.status === 'aborted') {
if (s._lastRenderedStatus !== 'aborted') {
var bannerHtml =
'<div class="paper-report-stopped-banner">' +
'<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>' +
'<span>' + escapeHtml((typeof t === 'function') ? t('paper.reportStopped') : 'Generation stopped') + '</span>' +
'</div>';
if (s.fullText && s.contentStarted) {
container.innerHTML = bannerHtml +
'<div class="paper-report-body">' +
(typeof renderMarkdown === 'function' ? renderMarkdown(s.fullText) : '<pre>' + escapeHtml(s.fullText) + '</pre>') +
'</div>';
} else {
container.innerHTML =
'<div class="paper-report-empty">' + bannerHtml +
'<p class="paper-report-hint">' + escapeHtml((typeof t === 'function') ? t('paper.reportStoppedHint') : 'Click Regenerate to start over') + '</p>' +
'</div>';
}
s._lastRenderedStatus = 'aborted';
}
return;
}
if (!document.getElementById(px + 'ToolZone')) {
_renderReportSkeleton(container, s.lang, view);
}
var toolZone = document.getElementById(px + 'ToolZone');
if (toolZone) {
var toolCount = s.toolRounds.length;
var searchingCount = s.toolRounds.filter(r => r.status === 'searching').length;
var toolKey = toolCount + ':' + searchingCount;
if (s._lastToolKey !== toolKey) {
if (toolCount > 0 && typeof renderToolRoundsHTML === 'function') {
toolZone.innerHTML = renderToolRoundsHTML(s.toolRounds, s.status === 'running');
} else {
toolZone.innerHTML = '';
}
s._lastToolKey = toolKey;
}
}
if (s.thinkingText) {
var thBlock = document.getElementById(px + 'ThinkingBlock');
var thBody = document.getElementById(px + 'ThinkingBody');
if (thBlock) {
thBlock.style.display = '';
if (s.contentStarted) thBlock.open = false;
}
if (thBody && thBody.textContent.length !== s.thinkingText.length) {
thBody.textContent = s.thinkingText;
thBody.scrollTop = thBody.scrollHeight;
}
}
var bodyEl = document.getElementById(px + 'BodyContent');
if (bodyEl) {
if (s.contentStarted) {
if (s._lastRenderedLen !== s.fullText.length) {
bodyEl.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(s.fullText) : '<pre>' + escapeHtml(s.fullText) + '</pre>';
s._lastRenderedLen = s.fullText.length;
}
} else if (s.status === 'error' && !s.fullText) {
bodyEl.innerHTML = '<div class="paper-error">' + escapeHtml(s.error || 'Failed') +
'<br><button onclick="' + retryFn + '" class="paper-retry-btn">' + escapeHtml((typeof t === 'function') ? t('paper.retry') : 'Retry') + '</button></div>';
}
}
}
async function _pollReportTask(view) {
view = view || _reportView('report');
var s = view.stream;
if (!s || !s.taskId) return;
if (s.pollBusy) return;
s.pollBusy = true;
try {
var resp = await Api.paper.reportPoll(s.taskId, s.cursor);
if (!resp || !resp.ok) {
if (resp && resp.status === 404) {
s.status = 'error';
s.error = 'Task no longer available on server. Please regenerate.';
_paintReportFromState(view);
return;
}
throw new Error('HTTP ' + resp.status);
}
var data = await resp.json();
if (!data.ok) {
s.status = 'error';
s.error = (typeof errorEnvelopeMessage === 'function'
? errorEnvelopeMessage(data.error) : '')
|| (typeof data.error === 'string' ? data.error : '')
|| 'Poll failed';
_paintReportFromState(view);
return;
}
var events = data.events || [];
for (var i = 0; i < events.length; i++) {
_applyReportEvent(s, events[i]);
}
s.cursor = data.next_cursor;
if (data.status === 'done' || data.status === 'aborted' || data.status === 'error') {
_clearReportRegenIntent(view.regenIntentKey);
}
if (data.status === 'done') {
s.status = 'done';
if (data.report) {
s.fullText = data.report;
if (s.paperId === _activePaperId) {
view.cache = data.report;
if (data.meta) { s.meta = data.meta; view.meta = data.meta; }
_rememberReportSnapshot(view, data.report, data.meta);
_saveActivePaperState();
}
}
if (data.resolvedTitle) _applyResolvedTitle(data.resolvedTitle, s.paperId);
} else if (data.status === 'aborted') {
s.status = 'aborted';
if (typeof data.partial === 'string' && data.partial) {
s.fullText = data.partial;
s.contentStarted = true;
}
} else if (data.status === 'error') {
s.status = 'error';
s.error = (typeof errorEnvelopeMessage === 'function'
? errorEnvelopeMessage(data.error) : '')
|| (typeof data.error === 'string' ? data.error : '')
|| s.error;
}
if (s.paperId === _activePaperId) {
_paintReportFromState(view);
}
if (s.status === 'running') {
s.pollTimer = setTimeout(function() { _pollReportTask(view); }, 1200);
}
} catch (e) {
console.warn('[Paper:Report] Poll failed:', e);
if (s && s.status === 'running') {
s.pollTimer = setTimeout(function() { _pollReportTask(view); }, 3000);
}
} finally {
s.pollBusy = false;
}
}
async function _generatePaperReport(force, view) {
view = view || _reportView('report');
var container = document.getElementById(view.containerId);
if (!container) return;
var langKey = view.langKey();
var retryFn = view.kind === 'review' ? '_generatePaperReview()' : '_generatePaperReport()';
var startPaperId = _activePaperId;
if (!force && view.stream
&& view.stream.paperId === _activePaperId
&& view.stream.status === 'running') {
_paintReportFromState(view);
return;
}
if (view.cache && !force) {
_renderFinalReport(container, view.cache, undefined, view);
_restoreReviewReadingLang(view);
return;
}
if (!_paperParsedText) {
container.innerHTML =
'<div class="paper-loading"><div class="paper-loading-spinner"></div>' +
'<div>Recovering paper text…</div></div>';
var ok = await _ensurePaperText();
if (_activePaperId !== startPaperId) return;
if (!ok) {
container.innerHTML =
'<div class="paper-report-empty"><p>' + escapeHtml((typeof t === 'function') ? t('paper.reportNoText') : 'No paper text available.') + '</p>' +
'<p style="opacity:0.6;font-size:12px;margin-top:6px">The PDF may be scanned/image-only, or parsing failed. Try re-uploading.</p></div>';
return;
}
}
var reportLang = view.uiLang();
if (!view.model) _populatePaperReportModelDropdown(view);
var reportModel = view.model || null;
if (force || (view.stream && view.stream.paperId !== _activePaperId)) {
_resetReportLocalState(view);
}
_renderReportSkeleton(container, reportLang, view);
view.stream = _makeReportStreamState(startPaperId, reportLang, '', view.kind);
_syncReportToolbar(true, view);
try {
var entryNow = _getActivePaperEntry();
var clientTitle = (entryNow && entryNow.title)
|| _paperFileName
|| (_paperPdfFilename || '').replace(/^\d+_/, '');
if (clientTitle) clientTitle = String(clientTitle).replace(/\.pdf$/i, '').trim();
var data = await Api.paper.reportStart({
paper_text: _paperParsedText,
lang: langKey,
model: reportModel,
force: !!force,
title: clientTitle || '',
filename: _paperPdfFilename || '',
});
if (_activePaperId !== startPaperId) return;
if (!data || !data.ok) throw new Error((data && data.error) || 'Start failed');
var stopWasPending = !!(view.stream && view.stream.pendingStop);
if (data.cached && data.report) {
view.stream = null;
view.cache = data.report;
view.meta = data.meta || null;
if (data.paper_hash) _paperHash = data.paper_hash;
_rememberReportSnapshot(view, data.report, data.meta);
_saveActivePaperState();
if (data.resolvedTitle) _applyResolvedTitle(data.resolvedTitle, startPaperId);
_renderFinalReport(container, data.report, undefined, view);
return;
}
if (data.paper_hash) _paperHash = data.paper_hash;
_clearReportRegenIntent(view.regenIntentKey);
view.stream = _makeReportStreamState(startPaperId, reportLang, data.task_id, view.kind);
_syncReportToolbar(true, view);
_pollReportTask(view);
if (stopWasPending) {
console.warn('[Paper:Report] Stop was pending during start — aborting task ' + data.task_id);
_stopPaperReport(view);
}
} catch (e) {
if (_activePaperId !== startPaperId) return;
console.warn('[Paper:Report] start failed:', e);
view.stream = null;
_syncReportToolbar(false, view);
container.innerHTML = '<div class="paper-error">Failed: ' + escapeHtml(e.message) +
'<br><button onclick="' + retryFn + '" class="paper-retry-btn">' + escapeHtml((typeof t === 'function') ? t('paper.retry') : 'Retry') + '</button></div>';
}
}
async function _generatePaperReview(force) {
return _generatePaperReport(force, _reportView('review'));
}
function _activeReviewLang() {
if (!_activePaperId) return 'en';
var stored = _readReviewLangMap()[_activePaperId];
return (stored === 'zh') ? 'zh' : 'en';
}
function _readReviewLangMap() {
try {
var raw = localStorage.getItem(_PAPER_REVIEW_LANG_KEY);
return raw ? (JSON.parse(raw) || {}) : {};
} catch (e) {
console.warn('[Paper:Review] read lang map failed:', e);
return {};
}
}
function _persistReviewLang(paperId, lang) {
if (!paperId || (lang !== 'en' && lang !== 'zh')) return;
try {
var map = _readReviewLangMap();
map[paperId] = lang;
localStorage.setItem(_PAPER_REVIEW_LANG_KEY, JSON.stringify(map));
} catch (e) {
console.warn('[Paper:Review] persist lang failed:', e);
}
}
function _restoreReviewReadingLang(view) {
if (!view || view.kind !== 'review') return;
if (!view.cache) return;
if (_activeReviewLang() !== 'zh') return;
_setReviewLang('zh');
}
async function _setReviewLang(lang) {
if (lang !== 'en' && lang !== 'zh') return;
var view = _reportView('review');
var container = document.getElementById(view.containerId);
if (!container) return;
var english = view.cache;
if (_activePaperId) _persistReviewLang(_activePaperId, lang);
if (lang === 'en') {
_paperReviewShowTranslation = false;
if (english) _renderFinalReport(container, english, view.meta, view);
_syncReviewTranslateBtn();
return;
}
if (!english) { _syncReviewTranslateBtn(); return; }
if (_paperReviewTranslatedText) {
_paperReviewShowTranslation = true;
_renderFinalReport(container, _paperReviewTranslatedText, view.meta, view);
_syncReviewTranslateBtn();
return;
}
if (_paperReviewTranslating) return;
_paperReviewTranslating = true;
_syncReviewTranslateBtn();
var trKey = 'review:' + (_paperReviewVenue || 'generic') + ':zh';
var startPaperId = _activePaperId;
try {
if (_paperHash) {
try {
var cd = await Api.paper.translateCache(_paperHash, trKey);
if (cd && cd.ok && cd.text) {
if (_activePaperId !== startPaperId) return;
_paperReviewTranslatedText = cd.text;
_paperReviewShowTranslation = true;
_renderFinalReport(container, cd.text, view.meta, view);
return;
}
} catch (e) { console.warn('[Paper:Review] translate cache lookup failed:', e); }
}
var startData = await Api.paper.translateStart({
paper_text: english, lang: trKey, paper_hash: _paperHash || '',
});
if (!startData || !startData.ok) throw new Error((startData && startData.error) || 'translate start failed');
if (startData.cached && startData.text) {
if (_activePaperId !== startPaperId) return;
_paperReviewTranslatedText = startData.text;
_paperReviewShowTranslation = true;
_renderFinalReport(container, startData.text, view.meta, view);
return;
}
if (startData.paper_hash) _paperHash = startData.paper_hash;
var taskId = startData.task_id;
if (!taskId) throw new Error('translate task returned no task_id');
var cursor = 0;
var parts = [];
while (true) {
if (_activePaperId !== startPaperId) { try { await Api.paper.translateAbort(taskId); } catch (_) {} return; }
var pollResp = await Api.paper.translatePoll(taskId, cursor);
if (!pollResp || !pollResp.ok) throw new Error('poll HTTP ' + (pollResp ? pollResp.status : 'none'));
var pollData = await pollResp.json();
if (!pollData.ok) throw new Error(pollData.error || 'poll failed');
cursor = pollData.next_cursor || cursor;
var events = pollData.events || [];
for (var ei = 0; ei < events.length; ei++) {
var ev = events[ei];
if (ev.type === 'chunk') {
parts.push(ev.text || '');
} else if (ev.type === 'done') {
if (_activePaperId !== startPaperId) return;
_paperReviewTranslatedText = ev.text || parts.join('\n\n');
_paperReviewShowTranslation = true;
_renderFinalReport(container, _paperReviewTranslatedText, view.meta, view);
return;
} else if (ev.type === 'error') {
var m = (typeof errorEnvelopeMessage === 'function') ? errorEnvelopeMessage(ev.error)
: (typeof ev.error === 'string' ? ev.error : '');
throw new Error(m || 'translation failed');
}
}
if (pollData.status === 'error') throw new Error('translation failed');
if (pollData.status === 'done' && !events.length) return;
await new Promise(function(r) { setTimeout(r, 700); });
}
} catch (e) {
console.warn('[Paper:Review] translate failed:', e);
if (typeof showToast === 'function') {
showToast((typeof t === 'function') ? t('paper.reviewTranslateFailed') : 'Translation failed', 'error');
}
} finally {
_paperReviewTranslating = false;
_syncReviewTranslateBtn();
}
}
function _toggleReviewTranslation() {
return _setReviewLang(_paperReviewShowTranslation ? 'en' : 'zh');
}
function _syncReviewTranslateBtn() {
var wrap = document.getElementById('reviewLangToggle');
if (!wrap) return;
var view = _reportView('review');
var hasReview = !!view.cache;
var cur = _activeReviewLang();
wrap.style.opacity = hasReview ? '' : '0.5';
wrap.querySelectorAll('.paper-report-lang-opt').forEach(function(btn) {
var isZh = btn.dataset.lang === 'zh';
btn.classList.toggle('active', btn.dataset.lang === cur);
btn.disabled = !hasReview || (isZh && _paperReviewTranslating);
if (isZh) {
btn.classList.toggle('loading', !!_paperReviewTranslating);
btn.title = _paperReviewTranslating
? ((typeof t === 'function') ? t('paper.reviewTranslating') : 'Translating…')
: ((typeof t === 'function') ? t('paper.reviewTranslateTitle') : 'Read in Chinese');
}
});
}
async function _loadOrGenerateReport(view) {
view = view || _reportView('report');
var reportLang = view.uiLang();
var langKey = view.langKey();
var startPaperId = _activePaperId;
if (view.stream && view.stream.paperId === _activePaperId) {
_paintReportFromState(view);
if (view.stream.status === 'running' && !view.stream.pollTimer) {
_pollReportTask(view);
} else if (view.stream.status === 'done') {
_restoreReviewReadingLang(view);
}
return;
}
if (_hasReportRegenIntent(_paperHash, reportLang, view.regenIntentKey)) {
console.warn('[Paper:Report] pending regenerate intent for hash=' + _paperHash
+ ' lang=' + reportLang + ' kind=' + view.kind
+ ' — resuming force-start (priority over lookup-reconnect)');
_generatePaperReport(true, view);
return;
}
if (view.cache) {
var cEl = document.getElementById(view.containerId);
if (cEl) _renderFinalReport(cEl, view.cache, undefined, view);
_restoreReviewReadingLang(view);
return;
}
var loadEl = document.getElementById(view.containerId);
if (loadEl) {
loadEl.innerHTML =
'<div class="paper-loading"><div class="paper-loading-spinner"></div>' +
'<div>' + escapeHtml((typeof t === 'function') ? t('paper.loadingReport') : 'Loading…') + '</div></div>';
}
if (_paperHash) {
try {
var lookupData = await Api.paper.reportLookup(_paperHash, langKey);
if (_activePaperId !== startPaperId) return;
if (lookupData && lookupData.ok && lookupData.task_id
&& (lookupData.status === 'running' || lookupData.status === 'pending')) {
var container = document.getElementById(view.containerId);
if (container) _renderReportSkeleton(container, reportLang, view);
view.stream = _makeReportStreamState(startPaperId, reportLang, lookupData.task_id, view.kind);
_syncReportToolbar(true, view);
_pollReportTask(view);
return;
}
} catch (e) {
if (_activePaperId !== startPaperId) return;
console.warn('[Paper:Report] lookup failed (non-fatal):', e);
}
}
try {
var cacheBody = { lang: langKey };
if (_paperHash) cacheBody.paper_hash = _paperHash;
else cacheBody.paper_text = _paperParsedText;
var cacheData = await Api.paper.reportCache(cacheBody);
if (_activePaperId !== startPaperId) return;
if (cacheData && cacheData.ok && cacheData.report) {
view.cache = cacheData.report;
view.meta = cacheData.meta || null;
if (cacheData.paper_hash) _paperHash = cacheData.paper_hash;
_rememberReportSnapshot(view, cacheData.report, cacheData.meta);
_saveActivePaperState();
var c2 = document.getElementById(view.containerId);
if (c2) _renderFinalReport(c2, cacheData.report, undefined, view);
_restoreReviewReadingLang(view);
return;
}
} catch (e) {
if (_activePaperId !== startPaperId) return;
console.warn('[Paper:Report] Cache lookup failed:', e);
}
if (_activePaperId !== startPaperId) return;
_renderReportStartPrompt(view);
}
function _renderReportStartPrompt(view) {
view = view || _reportView('report');
var container = document.getElementById(view.containerId);
if (!container) return;
_syncReportToolbar(false, view);
var isReview = view.kind === 'review';
var genFn = isReview ? '_generatePaperReview()' : '_generatePaperReport()';
var _tt = (typeof t === 'function') ? t : function(k) { return k; };
var title = _tt(isReview ? 'paper.reviewEmptyTitle' : 'paper.reportEmptyTitle');
var hint = _tt(isReview ? 'paper.reviewEmptyHint' : 'paper.reportEmptyHint');
var btnLabel = _tt(isReview ? 'paper.reviewGenerate' : 'paper.reportGenerate');
container.innerHTML =
'<div class="paper-report-empty">' +
'<p>' + escapeHtml(title) + '</p>' +
'<p class="paper-report-hint">' + escapeHtml(hint) + '</p>' +
'<button class="paper-report-generate-btn" onclick="' + genFn + '">' +
'<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3l14 9-14 9V3z"/></svg>' +
'<span>' + escapeHtml(btnLabel) + '</span>' +
'</button>' +
'</div>';
}
async function _loadOrGenerateReview() {
return _loadOrGenerateReport(_reportView('review'));
}
function _populatePaperReportModelDropdown(view) {
view = view || _reportView('report');
var dropdown = document.getElementById(view.modelDropdownId);
if (!dropdown) return;
var models = (typeof _registeredModels !== 'undefined') ? _registeredModels : [];
var hiddenSet = (typeof _hiddenModels !== 'undefined') ? _hiddenModels : new Set();
dropdown.innerHTML = '';
var chatModels = models.filter(function(m) {
if (hiddenSet.has(m.model_id)) return false;
var caps = m.capabilities || [];
for (var i = 0; i < caps.length; i++) {
if (caps[i] === 'image_gen' || caps[i] === 'embedding') return false;
}
return true;
});
if (!view.model && chatModels.length > 0) {
var availableIds = {};
for (var ci = 0; ci < chatModels.length; ci++) availableIds[chatModels[ci].model_id] = true;
var preset = (typeof config !== 'undefined' && config && config.model)
? config.model
: ((typeof serverModel !== 'undefined' && serverModel) ? serverModel : '');
var seed = (preset && availableIds[preset]) ? preset : chatModels[0].model_id;
_selectPaperReportModel(seed, view);
}
var grouped = {};
for (var i = 0; i < chatModels.length; i++) {
var m = chatModels[i];
var pid = m.provider_id || 'default';
if (!grouped[pid]) grouped[pid] = { name: m.provider_name || pid, models: [] };
grouped[pid].models.push(m);
}
var pids = Object.keys(grouped);
for (var pi = 0; pi < pids.length; pi++) {
var group = grouped[pids[pi]];
if (pids.length > 1) {
var section = document.createElement('div');
section.className = 'paper-report-model-dropdown-section';
section.textContent = group.name;
dropdown.appendChild(section);
}
for (var mi = 0; mi < group.models.length; mi++) {
var mod = group.models[mi];
var item = document.createElement('div');
item.className = 'paper-report-model-dropdown-item' + (mod.model_id === view.model ? ' active' : '');
var shortName = (typeof _modelShortName === 'function') ? _modelShortName(mod.model_id) : mod.model_id;
item.textContent = shortName;
item.title = mod.model_id;
(function(mid) {
item.onclick = function() { _selectPaperReportModel(mid, view); };
})(mod.model_id);
dropdown.appendChild(item);
}
}
}
function _selectPaperReportModel(modelId, view) {
view = view || _reportView('report');
view.model = modelId || '';
var label = document.getElementById(view.modelLabelId);
if (label) {
if (modelId) {
label.textContent = (typeof _modelShortName === 'function') ? _modelShortName(modelId) : modelId;
} else {
label.textContent = (typeof t === 'function') ? t('paper.reportSelectModel') : 'Select model';
}
}
var dropdown = document.getElementById(view.modelDropdownId);
if (dropdown) dropdown.classList.remove('open');
var items = dropdown ? dropdown.querySelectorAll('.paper-report-model-dropdown-item') : [];
items.forEach(function(it) { it.classList.toggle('active', it.title === modelId); });
}
function _togglePaperReportModelDropdown(e, view) {
e.stopPropagation();
view = view || _reportView('report');
var dropdown = document.getElementById(view.modelDropdownId);
if (!dropdown) return;
var isOpen = dropdown.classList.contains('open');
if (!isOpen) _populatePaperReportModelDropdown(view);
dropdown.classList.toggle('open');
}
function _togglePaperReviewModelDropdown(e) {
return _togglePaperReportModelDropdown(e, _reportView('review'));
}
function _positionGlossaryCard(term) {
if (!term) return;
var card = term.querySelector(':scope > .paper-term-card');
if (!card) return;
var scroller = term.closest('.paper-report-content, .paper-report-body');
if (!scroller) return;
card.style.left = '';
var termRect = term.getBoundingClientRect();
var scRect = scroller.getBoundingClientRect();
var cardW = card.offsetWidth;
var MARGIN = 8;
var minLeft = scRect.left + MARGIN;
var maxLeft = scRect.right - MARGIN - cardW;
var desired = termRect.left;
if (maxLeft < minLeft) desired = minLeft;
else desired = Math.min(Math.max(desired, minLeft), maxLeft);
var offset = desired - termRect.left;
if (offset) card.style.left = offset + 'px';
}
document.addEventListener('mouseover', function(e) {
var t = e.target && e.target.closest && e.target.closest('.paper-term');
if (t) _positionGlossaryCard(t);
});
document.addEventListener('focusin', function(e) {
var t = e.target && e.target.closest && e.target.closest('.paper-term');
if (t) _positionGlossaryCard(t);
});
document.addEventListener('click', function() {
['paperReportModelDropdown', 'paperReviewModelDropdown'].forEach(function(id) {
var dropdown = document.getElementById(id);
if (dropdown) dropdown.classList.remove('open');
});
});
document.addEventListener('click', function(e) {
var img = e.target;
if (!img || img.tagName !== 'IMG') return;
if (!img.closest('.paper-report-body, .paper-report-content')) return;
if (typeof _openImageFullscreen === 'function') {
_openImageFullscreen(img.src);
}
});
document.addEventListener('keydown', function(e) {
if (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar') return;
var img = e.target;
if (!img || img.tagName !== 'IMG') return;
if (!img.closest('.paper-report-body, .paper-report-content')) return;
e.preventDefault();
if (typeof _openImageFullscreen === 'function') _openImageFullscreen(img.src);
});
function _decorateZoomableImages(root) {
if (!root) return;
var imgs = root.querySelectorAll('img');
for (var i = 0; i < imgs.length; i++) {
var im = imgs[i];
if (im.getAttribute('tabindex') === null) im.setAttribute('tabindex', '0');
if (!im.getAttribute('role')) im.setAttribute('role', 'button');
if (!im.getAttribute('aria-label')) {
var alt = (im.getAttribute('alt') || '').trim();
im.setAttribute('aria-label', (alt ? alt + ' — ' : '') +
((typeof t === 'function') ? t('paper.imageZoomHint') : 'enlarge image'));
}
}
}
function _syncReportToolbar(running, view) {
view = view || _reportView('report');
if (running === undefined) {
running = !!(view.stream && view.stream.status === 'running');
}
var stopBtn = document.getElementById(view.stopBtnId);
var regenBtn = document.getElementById(view.regenBtnId);
if (stopBtn) {
stopBtn.style.display = running ? '' : 'none';
if (running) {
stopBtn.disabled = false;
var lbl = stopBtn.querySelector('span');
if (lbl) lbl.textContent = (typeof t === 'function') ? t('paper.reportStop') : 'Stop';
}
}
if (regenBtn) regenBtn.style.display = running ? 'none' : '';
if (typeof _syncReportLangToggle === 'function') _syncReportLangToggle(view);
if (view.kind === 'review') {
if (running) {
_paperReviewShowTranslation = false;
_paperReviewTranslatedText = '';
}
if (typeof _syncReviewTranslateBtn === 'function') _syncReviewTranslateBtn();
}
}
function _stopPaperReport(view) {
view = view || _reportView('report');
var s = view.stream;
if (!s || s.status !== 'running') return;
var stopBtn = document.getElementById(view.stopBtnId);
if (stopBtn) {
stopBtn.disabled = true;
var lbl = stopBtn.querySelector('span');
if (lbl) lbl.textContent = (typeof t === 'function') ? t('paper.reportStopping') : 'Stopping…';
}
if (!s.taskId) {
s.pendingStop = true;
return;
}
Api.paper.reportAbort(s.taskId).catch(function(e) {
console.warn('[Paper:Report] stop request failed:', e);
});
}
function _stopPaperReview() { return _stopPaperReport(_reportView('review')); }
async function _regeneratePaperReport(view) {
view = view || _reportView('report');
var reportLang = view.uiLang();
_setReportRegenIntent(_paperHash, reportLang, view.regenIntentKey);
_resetReportLocalState(view);
view.cache = '';
await _generatePaperReport(true, view);
}
async function _regeneratePaperReview() { return _regeneratePaperReport(_reportView('review')); }
function _copyPaperReport(view) {
view = view || _reportView('report');
if (!view.cache) return;
navigator.clipboard.writeText(view.cache).then(function() { debugLog((typeof t === 'function') ? t('paper.reportCopied') : 'Copied', 'success'); });
}
function _copyPaperReview() { return _copyPaperReport(_reportView('review')); }
function _togglePaperReportExportMenu(ev, view) {
if (ev) { ev.preventDefault(); ev.stopPropagation(); }
view = view || _reportView('report');
var dd = document.getElementById(view.exportDropdownId);
if (!dd) return;
var menuSel = '#' + view.exportMenuId;
var willOpen = !dd.classList.contains('open');
dd.classList.toggle('open', willOpen);
if (willOpen) {
var closeOnClick = function(e) {
if (!dd.contains(e.target) && !e.target.closest(menuSel)) {
dd.classList.remove('open');
document.removeEventListener('click', closeOnClick, true);
}
};
setTimeout(function() { document.addEventListener('click', closeOnClick, true); }, 0);
}
}
function _togglePaperReviewExportMenu(ev) { return _togglePaperReportExportMenu(ev, _reportView('review')); }
function _exportPaperReport(format, view) {
view = view || _reportView('report');
if (!_paperHash) {
debugLog('No report to export yet', 'warning');
return;
}
var dd = document.getElementById(view.exportDropdownId);
if (dd) dd.classList.remove('open');
format = format || 'md';
var url = Api.paper.exportUrl(_paperHash, view.langKey(), format);
if (format === 'pdf') {
var w = window.open(url, '_blank');
if (!w) {
debugLog('Pop-up blocked — please allow pop-ups to print/export PDF', 'warning');
}
return;
}
var a = document.createElement('a');
a.href = url;
a.rel = 'noopener';
document.body.appendChild(a); a.click(); document.body.removeChild(a);
}
function _exportPaperReview(format) { return _exportPaperReport(format, _reportView('review')); }
function _readVenueMap() {
try {
var raw = localStorage.getItem(_PAPER_REVIEW_VENUE_KEY);
return raw ? (JSON.parse(raw) || {}) : {};
} catch (e) {
console.warn('[Paper:Review] read venue map failed:', e);
return {};
}
}
function _persistReviewVenue(paperId, venueKey) {
if (!paperId || !venueKey) return;
try {
var map = _readVenueMap();
map[paperId] = venueKey;
localStorage.setItem(_PAPER_REVIEW_VENUE_KEY, JSON.stringify(map));
} catch (e) {
console.warn('[Paper:Review] persist venue failed:', e);
}
}
async function _ensureReviewVenues() {
if (_paperReviewVenues.length) return _paperReviewVenues;
try {
var data = await Api.paper.reviewVenues();
if (data && data.ok && Array.isArray(data.venues)) _paperReviewVenues = data.venues;
} catch (e) {
console.warn('[Paper:Review] venue fetch failed:', e);
}
return _paperReviewVenues;
}
async function _resolveReviewVenue() {
await _ensureReviewVenues();
if (!_paperReviewVenues.length) return _paperReviewVenue;
if (!_paperReviewVenue) {
var stored = _readVenueMap()[_activePaperId];
var valid = stored && _paperReviewVenues.some(function(v) { return v.key === stored; });
_selectReviewVenue(valid ? stored : _paperReviewVenues[0].key, true);
}
return _paperReviewVenue;
}
async function _populateReviewVenueDropdown() {
var dropdown = document.getElementById('paperReviewVenueDropdown');
if (!dropdown) return;
await _resolveReviewVenue();
dropdown.innerHTML = '';
for (var i = 0; i < _paperReviewVenues.length; i++) {
var v = _paperReviewVenues[i];
var item = document.createElement('div');
item.className = 'paper-report-model-dropdown-item' + (v.key === _paperReviewVenue ? ' active' : '');
item.textContent = v.name;
item.title = v.key;
(function(key) { item.onclick = function() { _selectReviewVenue(key); }; })(v.key);
dropdown.appendChild(item);
}
}
function _selectReviewVenue(key, silent) {
var changed = (_paperReviewVenue !== key);
_paperReviewVenue = key || '';
if (key && _activePaperId && !silent) _persistReviewVenue(_activePaperId, key);
var label = document.getElementById('paperReviewVenueLabel');
if (label) {
var found = _paperReviewVenues.find(function(v) { return v.key === key; });
label.textContent = found ? found.name : ((typeof t === 'function') ? t('paper.reviewSelectVenue') : 'Select venue');
}
var dropdown = document.getElementById('paperReviewVenueDropdown');
if (dropdown) {
dropdown.classList.remove('open');
dropdown.querySelectorAll('.paper-report-model-dropdown-item').forEach(function(it) {
it.classList.toggle('active', it.title === key);
});
}
if (changed && !silent && _paperActiveTab === 'review') {
_resetReportLocalState(_reportView('review'));
_paperReviewCache = '';
_loadOrGenerateReview();
}
}
function _toggleReviewVenueDropdown(e) {
if (e) e.stopPropagation();
var dropdown = document.getElementById('paperReviewVenueDropdown');
if (!dropdown) return;
var isOpen = dropdown.classList.contains('open');
if (!isOpen) _populateReviewVenueDropdown();
dropdown.classList.toggle('open');
}
document.addEventListener('click', function() {
var dd = document.getElementById('paperReviewVenueDropdown');
if (dd) dd.classList.remove('open');
});
var _babelTargetLang = '';
var _babelTranslatedPages = {};
var _babelTranslating = false;
function _initBabelPdfTab() {
var container = document.getElementById('paperTranslateContent');
if (!container) return;
var _ttb = (typeof t === 'function') ? t : function(k){ return k; };
container.innerHTML =
'<div class="babel-pdf-module">' +
'<div class="babel-pdf-brand">' +
'<svg class="babel-pdf-icon" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
'<path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/>' +
'<path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/>' +
'</svg>' +
'<div class="babel-pdf-brand-text"><span class="babel-pdf-title">Babel PDF</span><span class="babel-pdf-subtitle">' + escapeHtml(_ttb('paper.babelSubtitle')) + '</span></div>' +
'</div>' +
'<div class="babel-pdf-lang-bar">' +
'<button class="babel-pdf-lang' + (!_babelTargetLang ? ' active' : '') + '" data-lang="" onclick="_switchBabelLang(\'\', this)">' + escapeHtml(_ttb('paper.babelOriginal')) + '</button>' +
'<button class="babel-pdf-lang' + (_babelTargetLang === 'zh' ? ' active' : '') + '" data-lang="zh" onclick="_switchBabelLang(\'zh\', this)">中文</button>' +
'<button class="babel-pdf-lang' + (_babelTargetLang === 'en' ? ' active' : '') + '" data-lang="en" onclick="_switchBabelLang(\'en\', this)">English</button>' +
'<button class="babel-pdf-lang' + (_babelTargetLang === 'ja' ? ' active' : '') + '" data-lang="ja" onclick="_switchBabelLang(\'ja\', this)">日本語</button>' +
'</div>' +
'<div class="babel-pdf-body" id="babelPdfBody"></div>' +
'<div class="babel-pdf-status" id="babelPdfStatus"></div>' +
'</div>';
if (_babelTargetLang && _babelTranslatedPages[_babelTargetLang]) {
_renderBabelResult(_babelTranslatedPages[_babelTargetLang]);
} else if (_babelTargetLang && _paperParsedText) {
_startBabelTranslation();
} else {
var body = document.getElementById('babelPdfBody');
if (body) {
body.innerHTML =
'<div class="babel-pdf-empty">' +
'<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.4"><path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/><path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/></svg>' +
'<p>' + escapeHtml(_ttb('paper.babelEmptyTitle')) + '</p>' +
'<p class="babel-pdf-hint">' + escapeHtml(_ttb('paper.babelEmptyHint')) + '</p>' +
'</div>';
}
}
}
function _switchBabelLang(lang, btn) {
document.querySelectorAll('.babel-pdf-lang').forEach(function(b) { b.classList.remove('active'); });
if (btn) btn.classList.add('active');
_babelTargetLang = lang;
_startBabelTranslation();
}
function _startBabelTranslation() {
var body = document.getElementById('babelPdfBody');
var status = document.getElementById('babelPdfStatus');
if (!body) return;
var _ttb = (typeof t === 'function') ? t : function(k){ return k; };
var langNames = { zh: '中文', en: 'English', ja: '日本語' };
if (!_babelTargetLang) {
body.innerHTML = '<div class="babel-pdf-empty"><svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.4"><path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/><path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/></svg><p>' + escapeHtml(_ttb('paper.babelEmptyTitle')) + '</p><p class="babel-pdf-hint">' + escapeHtml(_ttb('paper.babelEmptyHint')) + '</p></div>';
if (status) status.textContent = '';
return;
}
if (!_paperParsedText) {
body.innerHTML = '<div class="babel-pdf-empty"><p>' + escapeHtml(_ttb('paper.babelNoPaper')) + '</p></div>';
return;
}
if (_babelTranslatedPages[_babelTargetLang]) {
_renderBabelResult(_babelTranslatedPages[_babelTargetLang]);
if (status) status.textContent = _ttb('paper.babelCompleteCached');
return;
}
var _langLabel = langNames[_babelTargetLang] || _babelTargetLang;
var _translatingMsg = _ttb('paper.babelTranslatingTo', { lang: _langLabel });
if (status) status.textContent = _translatingMsg;
body.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>' + escapeHtml(_translatingMsg) + '</div><div class="babel-pdf-progress"><div class="babel-pdf-progress-bar" id="babelProgressBar" style="width:0%"></div></div></div>';
_babelTranslateAllPages(_babelTargetLang);
}
async function _babelTranslateAllPages(lang) {
if (_babelTranslating) return;
_babelTranslating = true;
var bar = document.getElementById('babelProgressBar');
var statusEl = document.getElementById('babelPdfStatus');
function _setProgress(done, total) {
if (bar && total > 0) bar.style.width = Math.round((done / total) * 100) + '%';
if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelTranslatedCount', { done: done, total: total }) : ('Translated ' + done + '/' + total + ' sections');
}
try {
if (_paperHash) {
try {
var cacheData = await Api.paper.translateCache(_paperHash, lang);
if (cacheData && cacheData.ok && cacheData.text) {
if (_babelTargetLang === lang) {
_babelTranslatedPages[lang] = cacheData.text;
_renderBabelResult(cacheData.text);
_saveActivePaperState();
if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelCompleteCached') : 'Translation complete (cached)';
}
return;
}
} catch (e) {
console.warn('[Babel] Cache lookup failed:', e);
}
}
var startData = await Api.paper.translateStart({
paper_text: _paperParsedText,
lang: lang,
paper_hash: _paperHash || '',
});
if (!startData || !startData.ok) throw new Error((startData && startData.error) || 'Translate start failed');
if (startData.cached && startData.text) {
if (_babelTargetLang === lang) {
_babelTranslatedPages[lang] = startData.text;
_renderBabelResult(startData.text);
_saveActivePaperState();
if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelCompleteCached') : 'Translation complete (cached)';
}
return;
}
if (startData.paper_hash) _paperHash = startData.paper_hash;
var taskId = startData.task_id;
if (!taskId) throw new Error('Translate task did not return task_id');
var cursor = 0;
var aggregated = [];
while (true) {
if (_babelTargetLang !== lang) {
try {
await Api.paper.translateAbort(taskId);
} catch (_) {}
return;
}
var pollResp = await Api.paper.translatePoll(taskId, cursor);
if (!pollResp || !pollResp.ok) throw new Error('Poll HTTP ' + (pollResp ? pollResp.status : 'no response'));
var pollData = await pollResp.json();
if (!pollData.ok) throw new Error(pollData.error || 'Poll failed');
cursor = pollData.next_cursor || cursor;
var events = pollData.events || [];
for (var ei = 0; ei < events.length; ei++) {
var ev = events[ei];
if (ev.type === 'chunk') {
aggregated.push(ev.text || '');
_setProgress(ev.index + 1, ev.total);
if (_babelTargetLang === lang) {
_renderBabelResult(aggregated.join('\n\n'));
}
} else if (ev.type === 'done') {
if (_babelTargetLang === lang) {
_babelTranslatedPages[lang] = ev.text || aggregated.join('\n\n');
_renderBabelResult(_babelTranslatedPages[lang]);
_saveActivePaperState();
if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelComplete') : 'Translation complete';
}
return;
} else if (ev.type === 'error') {
var _evMsg = (typeof errorEnvelopeMessage === 'function')
? errorEnvelopeMessage(ev.error)
: (typeof ev.error === 'string' ? ev.error : '');
throw new Error(_evMsg || 'Translation failed');
}
}
if (pollData.status === 'done') return;
if (pollData.status === 'error') {
var _pdMsg = (typeof errorEnvelopeMessage === 'function')
? errorEnvelopeMessage(pollData.error)
: (typeof pollData.error === 'string' ? pollData.error : '');
throw new Error(_pdMsg || 'Translation failed');
}
await new Promise(function(r) { setTimeout(r, 700); });
}
} catch (e) {
console.warn('[Babel] Translation failed:', e);
var body = document.getElementById('babelPdfBody');
var _ttf = (typeof t === 'function') ? t : function(k){ return k; };
if (body && _babelTargetLang === lang) {
body.innerHTML = '<div class="paper-error">' + escapeHtml(_ttf('paper.babelFailed')) + ': ' +
escapeHtml(e.message || String(e)) +
'<br><button class="paper-retry-btn" onclick="_startBabelTranslation()">' + escapeHtml(_ttf('paper.retry')) + '</button></div>';
}
if (statusEl) statusEl.textContent = _ttf('paper.babelFailed');
} finally {
_babelTranslating = false;
}
}
function _renderBabelResult(text) {
var body = document.getElementById('babelPdfBody');
if (!body) return;
body.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(text) : '<pre style="white-space:pre-wrap;font-size:13px;line-height:1.7">' + escapeHtml(text) + '</pre>';
}
function _handlePaperKeyDown(e) {
if (!paperMode) return;
if (e.key === 'Escape') { e.preventDefault(); exitPaperMode(); return; }
if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
if (e.key === 'Enter' && !e.shiftKey && e.target.id === 'paperQAInput') { e.preventDefault(); _sendPaperQuestion(); }
return;
}
if (e.key === '+' || e.key === '=') { paperZoomIn(); e.preventDefault(); }
if (e.key === '-') { paperZoomOut(); e.preventDefault(); }
if (e.key === '0') { paperFitWidth(); e.preventDefault(); }
}
document.addEventListener('keydown', _handlePaperKeyDown);
document.addEventListener('mouseup', function() { if (paperMode) setTimeout(_handlePaperTextSelection, 10); });
window.addEventListener('beforeunload', function() {
if (typeof _teardownReadingTracker === 'function') _teardownReadingTracker(true);
});
window.addEventListener('katex:loaded', function() {
if (!paperMode) return;
['report', 'review'].forEach(function(kind) {
var view = _reportView(kind);
if (view.stream) {
view.stream._lastRenderedLen = -1;
view.stream._lastRenderedStatus = '';
if (typeof _paintReportFromState === 'function') _paintReportFromState(view);
} else {
var rc = document.getElementById(view.containerId);
if (rc && view.cache) {
_renderFinalReport(rc, view.cache, undefined, view);
}
}
});
if (typeof _renderPaperQA === 'function') _renderPaperQA();
if (_recStream && document.querySelector('[data-rec-shell]') &&
typeof _paintRecommendFromState === 'function') {
var _rl = document.querySelector('[data-rec-list]');
if (_rl) { Array.prototype.forEach.call(_rl.children, function(n){ n._recSig = ''; }); }
_paintRecommendFromState();
} else if (_paperSearchResults && _paperSearchResults.length &&
document.querySelector('.paper-search .paper-result-list') &&
typeof _lastArxivSearchQuery === 'string') {
_renderArxivSearchResults(_lastArxivSearchQuery, _paperSearchResults);
}
});
(window._onReady || function (f) { document.addEventListener('DOMContentLoaded', f); })(function() {
_loadPaperLibrary();
function _addPaperDropZone(el) {
if (!el) return;
el.addEventListener('dragover', function(e) {
if (paperMode && e.dataTransfer && e.dataTransfer.types.includes('Files')) {
e.preventDefault(); e.stopPropagation();
el.classList.add('paper-drag-over');
}
});
el.addEventListener('dragleave', function(e) {
if (e.relatedTarget && el.contains(e.relatedTarget)) return;
el.classList.remove('paper-drag-over');
});
el.addEventListener('drop', async function(e) {
e.preventDefault(); e.stopPropagation();
el.classList.remove('paper-drag-over');
if (!paperMode) return;
var files = Array.from(e.dataTransfer?.files || []);
for (var fi = 0; fi < files.length; fi++) {
if (files[fi].type === 'application/pdf' || files[fi].name.toLowerCase().endsWith('.pdf')) {
await _handlePaperFileDrop(files[fi]);
break;
}
}
});
}
_addPaperDropZone(document.getElementById('paperPdfViewer'));
_addPaperDropZone(document.getElementById('paperModeContainer'));
_addPaperDropZone(document.getElementById('paperSidebarOverlay'));
var pdfViewer = document.getElementById('paperPdfViewer');
if (pdfViewer) {
pdfViewer.addEventListener('wheel', function(e) {
if (!paperMode || !e.ctrlKey) return;
e.preventDefault();
var delta = e.deltaY > 0 ? -0.1 : 0.1;
_paperScale = Math.max(0.25, Math.min(4.0, _paperScale + delta));
_syncZoomUI();
clearTimeout(_paperZoomDebounce);
_paperZoomDebounce = setTimeout(function() { _renderAllPages(); }, 150);
}, { passive: false });
}
});
;
// ═══ image-gen.js ═══
var _igSelectedModel = 'gemini-3.1-flash-image-preview';
var _igSelectedAspect = '1:1';
var _igSelectedResolution = '1K';
var _igSelectedCount = 1;
let _igGenerating = false;
let _igAbortController = null;
let _igAbortControllers = [];
const _IG_ALL_MODELS = [
'gemini-3.1-flash-image-preview',
'gemini-3-pro-image-preview',
'gemini-2.5-flash-image',
'gpt-image-1.5',
'gpt-image-2',
];
var _IG_MODEL_SHORT = {
'gemini-3.1-flash-image-preview': 'Gemini 3.1 Flash',
'gemini-3-pro-image-preview': 'Gemini 3 Pro',
'gemini-2.5-flash-image': 'Gemini 2.5 Flash',
'gpt-image-1.5': 'GPT Image 1.5',
'gpt-image-2': 'GPT Image 2',
};
function _igCollectHistory(conv) {
const history = [];
if (!conv || !conv.messages) return history;
for (const m of conv.messages) {
if (m._igResult && m._igResult.image_url) {
history.push({
prompt: m._igResult.prompt || '',
image_url: m._igResult.remote_image_url || m._igResult.image_url || '',
text: m._igResult.response_text || '',
});
}
if (m._igResults) {
for (const r of m._igResults) {
if (r.ok && r.image_url) {
history.push({
prompt: r.prompt || '',
image_url: r.remote_image_url || r.image_url || '',
text: r.response_text || '',
});
break;
}
}
}
}
return history;
}
function _igClassifyError(data, httpStatus) {
const errorType = data.error_type || '';
const errText = data.error || 'Unknown error';
const blockReason = data.block_reason || '';
let title = 'Image generation failed';
let isRateLimit = false;
let isContentBlocked = false;
let isTimeout = false;
if (errorType === 'rate_limited' || httpStatus === 429 || data.rate_limited) {
title = 'Rate limited';
isRateLimit = true;
} else if (errorType === 'content_blocked' || blockReason) {
title = 'Content blocked';
isContentBlocked = true;
} else if (errorType === 'timeout') {
title = 'Generation timed out';
isTimeout = true;
} else if (errorType === 'no_slot') {
title = 'No model available';
}
return  ({
title,
text: errText,
detail: data.text || '',
errorType: errorType || 'generation_failed',
blockReason,
isTimeout,
isRateLimit,
isContentBlocked,
});
}
function _igToast(message, type) {
if (typeof debugLog === 'function') {
debugLog(message, type || 'info');
}
}
function enterImageGenMode() {
if (imageGenMode) { exitImageGenMode(); return; }
if (typeof paperMode !== 'undefined' && paperMode && typeof exitPaperMode === 'function') exitPaperMode();
_applyImageGenUI(true);
_saveConvToolState();
if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
debugLog('Image Gen Mode: ENTER', 'success');
document.getElementById('userInput')?.focus();
}
function exitImageGenMode() {
_applyImageGenUI(false);
_saveConvToolState();
if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
debugLog('Image Gen Mode: EXIT', 'info');
}
function toggleIgModelDropdown(e) {
e.stopPropagation();
const wrapper = document.getElementById('igModelPicker');
if (!wrapper) return;
wrapper.classList.toggle('open');
if (wrapper.classList.contains('open')) {
const closeHandler = function (ev) {
if (!wrapper.contains(ev.target)) {
wrapper.classList.remove('open');
document.removeEventListener('click', closeHandler);
}
};
setTimeout(() => document.addEventListener('click', closeHandler), 0);
}
}
function selectIgModel(el) {
_igSelectedModel = el.dataset.model;
el.closest('.ig-preset-dropdown').querySelectorAll('.ig-model-option').forEach(o => {
o.classList.toggle('active', o.dataset.model === _igSelectedModel);
});
const label = document.getElementById('igModelLabel');
const iconEl = document.getElementById('igModelIcon');
const toggle = document.querySelector('.ig-preset');
if (_igSelectedModel === '__all__') {
if (label) label.textContent = 'All Models';
if (iconEl) iconEl.innerHTML = '';
if (toggle) toggle.setAttribute('data-brand', 'generic');
if (_igSelectedCount < 2) {
_igSelectedCount = 4;
document.querySelectorAll('#igCountBar .ig-pill').forEach(b => {
b.classList.toggle('active', b.dataset.count === '4');
});
const genText = document.querySelector('.ig-gen-text');
if (genText) genText.textContent = '4连抽!';
}
} else {
const name = el.querySelector('.ig-model-name')?.textContent || _igSelectedModel;
if (label) label.textContent = name;
const brand = typeof _detectBrand === 'function' ? _detectBrand(_igSelectedModel) : 'generic';
if (iconEl && typeof _brandSvg === 'function') iconEl.innerHTML = _brandSvg(brand, 14);
if (toggle) toggle.setAttribute('data-brand', brand);
}
document.getElementById('igModelPicker')?.classList.remove('open');
if (typeof _scheduleReflow === 'function') _scheduleReflow();
}
function selectIgAspect(el) {
_igSelectedAspect = el.dataset.ar;
document.querySelectorAll('#igAspectBar .ig-pill').forEach(b => b.classList.remove('active'));
el.classList.add('active');
}
function selectIgResolution(el) {
_igSelectedResolution = el.dataset.res;
document.querySelectorAll('#igResolutionBar .ig-pill').forEach(b => b.classList.remove('active'));
el.classList.add('active');
}
function selectIgCount(el) {
_igSelectedCount = parseInt(el.dataset.count, 10) || 1;
document.querySelectorAll('#igCountBar .ig-pill').forEach(b => b.classList.remove('active'));
el.classList.add('active');
const genText = document.querySelector('.ig-gen-text');
if (genText) genText.textContent = _igSelectedCount > 1 ? `${_igSelectedCount}连抽!` : '生成';
}
async function generateImageDirect() {
if (_igGenerating) return;
const textarea = document.getElementById('userInput');
const prompt = (textarea?.value || '').trim();
if (!prompt) {
debugLog('Please describe the image you want to create or edit', 'warning');
textarea?.focus();
return;
}
const sourceImages = [...pendingImages];
const isEdit = sourceImages.length > 0;
const effectiveCount = _igSelectedModel === '__all__'
? Math.max(_igSelectedCount, _IG_ALL_MODELS.length)
: _igSelectedCount;
if (effectiveCount > 1 && !isEdit) {
return _igGenerateBatch(prompt, effectiveCount);
}
_igGenerating = true;
const genBtn = document.getElementById('igGenerateBtn');
if (genBtn) genBtn.disabled = true;
let conv = getActiveConv();
if (!conv) {
const now = Date.now();
conv = { id: 'conv-' + now + '-' + Math.random().toString(36).slice(2,8),
title: 'New Chat', messages: [], createdAt: now, updatedAt: now,
activeTaskId: null };
conversations.unshift(conv);
activeConvId = conv.id;
sessionStorage.setItem('tofu_activeConvId', conv.id);
_saveConvToolState();
if (typeof renderConversationList === 'function') renderConversationList();
}
const userMsg = { role: 'user', content: prompt, timestamp: Date.now(), _isImageGen: true };
if (isEdit) {
userMsg.images = sourceImages;
userMsg._isImageEdit = true;
}
_ensureMsgId(userMsg);
conv.messages.push(userMsg);
if (conv.messages.filter(m => m.role === 'user').length === 1) {
const titleText = isEdit ? prompt : prompt;
conv.title = titleText.slice(0, 60) + (titleText.length > 60 ? '...' : '');
if (activeConvId === conv.id)
document.getElementById('topbarTitle').textContent = conv.title;
renderConversationList();
}
renderChat(conv, true);
textarea.value = '';
textarea.style.height = 'auto';
pendingImages = [];
renderImagePreviews();
const igHistory = _igCollectHistory(conv);
const historyCount = igHistory.length;
const chatDiv = document.getElementById('chatInner');
const loadingId = 'ig-loading-' + Date.now();
const resLabel = _igSelectedResolution !== '1K' ? ` · ${_igSelectedResolution}` : '';
const modelLabel = _IG_MODEL_SHORT[_igSelectedModel] || _igSelectedModel;
const actionLabel = isEdit ? 'Editing image…' : 'Generating image…';
const historyBadge = historyCount > 0 ? `<span class="ig-history-badge" title="${historyCount} prior editing turn${historyCount > 1 ? 's' : ''}">${historyCount} prior turn${historyCount > 1 ? 's' : ''}</span>` : '';
const loadingHtml = `<div class="ig-generating" id="${loadingId}">
<div class="ig-gen-spinner"></div>
<div class="ig-gen-title">${actionLabel}</div>
<div class="ig-gen-model-info">${_escapeHtmlBasic(modelLabel)}${historyBadge}</div>
<div class="ig-gen-subtitle">${_escapeHtmlBasic(prompt.slice(0, 100))}${prompt.length > 100 ? '…' : ''}</div>
<div class="ig-gen-timer" id="${loadingId}-timer">0s${resLabel}</div>
<div class="ig-gen-status" id="${loadingId}-status"></div>
<button class="ig-gen-cancel" onclick="_igCancelGeneration()" title="Cancel">✕ Cancel</button>
</div>`;
chatDiv.insertAdjacentHTML('beforeend', loadingHtml);
scrollToBottom();
saveConversations(conv.id);
const t0 = Date.now();
const timerInterval = setInterval(() => {
const el = document.getElementById(loadingId + '-timer');
if (el) el.textContent = ((Date.now() - t0) / 1000).toFixed(0) + 's' + resLabel;
}, 1000);
_igAbortController = new AbortController();
const abortTimer = setTimeout(() => _igAbortController?.abort(), 150_000);
try {
const reqBody = {
prompt,
aspect_ratio: _igSelectedAspect,
resolution: _igSelectedResolution,
model: _igSelectedModel,
};
if (igHistory.length > 0) reqBody.history = igHistory;
if (isEdit) {
reqBody.source_images = sourceImages.map(img => ({
image_b64: img.base64,
mime_type: img.mediaType || 'image/png',
image_url: img.url || '',
}));
}
if (historyCount > 0) {
_igToast(`Sending ${historyCount} prior turn${historyCount > 1 ? 's' : ''} for multi-turn editing`, 'info');
}
const data = await Api.images.generate(reqBody, { signal: _igAbortController.signal });
clearTimeout(abortTimer);
clearInterval(timerInterval);
const loadingEl = document.getElementById(loadingId);
if (data.ok) {
const imgSrc = data.image_url
? apiUrl(data.image_url)
: (data.image_b64 ? `data:${data.mime_type || 'image/png'};base64,${data.image_b64}` : '');
const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
const sizeStr = data.file_size ? _formatFileSize(data.file_size) : '';
if (loadingEl) loadingEl.remove();
const assistantContent = data.text
? `${data.text}\n\n![Generated Image](${data.image_url || 'data:image'})`
: `![Generated Image](${data.image_url || 'data:image'})`;
const assistantMsg = {
role: 'assistant',
content: assistantContent,
timestamp: Date.now(),
_igResult: { prompt, aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
model: data.model || _igSelectedModel,
provider_id: data.provider_id || '',
image_url: data.image_url || '', elapsed,
file_size: data.file_size || 0,
remote_image_url: data.remote_image_url || '',
response_text: data.text || '',
history_turns: data.history_resolved || 0 },
};
_ensureMsgId(assistantMsg);
conv.messages.push(assistantMsg);
if (conv.id === activeConvId) renderChat(conv, true);
saveConversations(conv.id);
syncConversationToServer(conv);
} else {
const errInfo = _igClassifyError(data, data._status);
if (loadingEl) loadingEl.remove();
if (errInfo.isRateLimit) {
_igToast('⏳ Rate limited — all model slots exhausted', 'warning');
} else if (errInfo.isContentBlocked) {
_igToast('🚫 Content policy: prompt was blocked', 'error');
}
const errMsg = { role: 'assistant', content: `Image generation failed: ${errInfo.text}`,
timestamp: Date.now(), _isImageGen: true,
_igError: errInfo };
_ensureMsgId(errMsg);
conv.messages.push(errMsg);
if (conv.id === activeConvId) renderChat(conv, true);
saveConversations(conv.id);
syncConversationToServer(conv);
}
} catch (err) {
clearTimeout(abortTimer);
clearInterval(timerInterval);
const loadingEl = document.getElementById(loadingId);
const isAbort = err.name === 'AbortError';
const errText = isAbort ? 'Request timed out (150s). The server may still be generating — please try again.'
: (err.message || 'Failed to connect to server');
if (loadingEl) loadingEl.remove();
console.error('[ImageGen] Direct generation error:', err);
if (isAbort) {
_igToast('⏱ Generation timed out (150s)', 'warning');
}
const errTitle = isAbort ? 'Generation timed out' : 'Network error';
const errMsg = { role: 'assistant', content: `${isAbort ? 'Image generation timed out' : 'Image generation network error'}: ${errText}`,
timestamp: Date.now(), _isImageGen: true,
_igError: { title: errTitle, text: errText, detail: '', errorType: isAbort ? 'timeout' : 'network', isTimeout: isAbort, isRateLimit: false, isContentBlocked: false } };
_ensureMsgId(errMsg);
conv.messages.push(errMsg);
if (conv.id === activeConvId) renderChat(conv, true);
saveConversations(conv.id);
syncConversationToServer(conv);
} finally {
_igGenerating = false;
_igAbortController = null;
if (genBtn) genBtn.disabled = false;
if (conv.id === activeConvId && chatDiv) chatDiv.scrollTop = chatDiv.scrollHeight;
}
}
function _igUpdateGenButton() {
const genText = document.querySelector('.ig-gen-text');
if (!genText) return;
const isEdit = pendingImages.length > 0;
if (_igSelectedCount <= 1 && _igSelectedModel !== '__all__') {
genText.textContent = isEdit ? '编辑' : '生成';
}
}
function _igCancelGeneration() {
if (_igAbortController) {
_igAbortController.abort();
}
if (_igAbortControllers.length > 0) {
_igAbortControllers.forEach(ac => ac.abort());
_igAbortControllers = [];
}
debugLog('Image generation cancelled', 'info');
}
function _igRetryLastPrompt() {
const conv = getActiveConv();
if (!conv || conv.messages.length === 0) return;
for (let i = conv.messages.length - 1; i >= 0; i--) {
const m = conv.messages[i];
if (m.role === 'user' && m._isImageGen) {
const prompt = m.content?.trim() || '';
const textarea = document.getElementById('userInput');
if (textarea) { textarea.value = prompt; textarea.focus(); }
return;
}
}
}
async function _igRetryBatchSlot(msgIdx, slotIdx, prompt, model) {
const conv = getActiveConv();
if (!conv || !conv.messages[msgIdx]) return;
const msg = conv.messages[msgIdx];
if (!msg._igResults || !msg._igResults[slotIdx]) return;
const slotEl = document.querySelector(`.ig-batch-slot[data-slot-idx="${slotIdx}"][data-msg-idx="${msgIdx}"]`);
if (!slotEl) return;
const useModel = model || _igSelectedModel;
const modelLabel = _IG_MODEL_SHORT[useModel] || useModel;
slotEl.innerHTML = `<div class="ig-generating ig-batch-loading">
<div class="ig-gen-spinner"></div>
<div class="ig-gen-title">${_escapeHtmlBasic(modelLabel)}</div>
<div class="ig-gen-subtitle">Retrying…</div>
<div class="ig-gen-timer" id="ig-retry-timer-${slotIdx}">0.0s</div>
</div>`;
const t0 = Date.now();
const timer = setInterval(() => {
const el = document.getElementById(`ig-retry-timer-${slotIdx}`);
if (el) el.textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's';
}, 100);
try {
const igHistory = _igCollectHistory(conv);
const body = { prompt, model: useModel, aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution };
if (igHistory.length > 0) body.history = igHistory;
const data = await Api.images.generate(body);
clearInterval(timer);
const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
if (data.ok && (data.image_url || data.image_b64)) {
const imgSrc = data.image_url
? (data.image_url.startsWith('/') ? apiUrl(data.image_url) : data.image_url)
: `data:${data.mime_type || 'image/png'};base64,${data.image_b64}`;
const sizeStr = data.file_size ? _formatFileSize(data.file_size) : '';
slotEl.innerHTML = `<div class="ig-result-card ig-batch-reveal">
<img src="${imgSrc}" alt="${_escapeHtmlBasic(prompt.slice(0, 60))}" loading="lazy"
onclick="_openImageFullscreen(this.src)" />
<div class="ig-result-footer">
<span class="ig-result-prompt" title="${_escapeHtmlBasic(prompt)}">${_escapeHtmlBasic(_IG_MODEL_SHORT[data.model || useModel] || data.model || useModel)}</span>
<div class="ig-result-meta">
${sizeStr ? `<span class="ig-meta-pill">${sizeStr}</span>` : ''}
<span class="ig-meta-pill">${elapsed}s</span>
</div>
<div class="ig-result-actions">
<button onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">⬇</button>
<button onclick="event.stopPropagation();_openImageFullscreen(this.closest('.ig-result-card').querySelector('img').src)" title="Fullscreen">⛶</button>
</div>
</div>
</div>`;
msg._igResults[slotIdx] = {
ok: true, prompt, model: data.model || useModel, provider_id: data.provider_id || '',
aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
image_url: data.image_url || '', remote_image_url: data.remote_image_url || '',
file_size: data.file_size || 0, elapsed, response_text: data.text || '', error: '',
};
_igToast(`Slot ${slotIdx + 1} retry succeeded`, 'success');
} else {
const errInfo = _igClassifyError(data, data._status);
slotEl.innerHTML = _igBatchErrorSlotHtml(errInfo, useModel, msgIdx, slotIdx, prompt);
msg._igResults[slotIdx].error = errInfo.text;
msg._igResults[slotIdx].errorType = errInfo.errorType;
}
const okResults = msg._igResults.filter(r => r.ok);
msg.content = okResults.length > 0
? okResults.map(r => `![Generated Image](${r.image_url || 'data:image'})`).join('\n\n')
: 'All image generations failed';
saveConversations(conv.id);
syncConversationToServer(conv);
} catch (e) {
clearInterval(timer);
console.error('[ImageGen] Retry slot error:', e);
const errInfo = { title: 'Retry failed', text: e.message || 'Network error', errorType: 'network', isRateLimit: false, isContentBlocked: false };
slotEl.innerHTML = _igBatchErrorSlotHtml(errInfo, useModel, msgIdx, slotIdx, prompt);
}
}
function _igBatchErrorSlotHtml(errInfo, model, msgIdx, slotIdx, prompt) {
const modelLabel = _IG_MODEL_SHORT[model] || model || '?';
let typeClass = 'ig-error-generic';
let icon = Icon('zap', 24);
if (errInfo.isRateLimit) {
typeClass = 'ig-error-ratelimit';
icon = Icon('hourglass', 24);
} else if (errInfo.isContentBlocked) {
typeClass = 'ig-error-blocked';
icon = Icon('ban', 24);
} else if (errInfo.isTimeout || errInfo.errorType === 'timeout') {
typeClass = 'ig-error-timeout';
icon = Icon('timer', 24);
}
return `<div class="ig-batch-error ${typeClass}">
<div class="ig-error-icon">${icon}</div>
<div class="ig-error-title">${_escapeHtmlBasic(modelLabel)}</div>
<div class="ig-error-text">${_escapeHtmlBasic((errInfo.text || 'Failed').slice(0, 200))}</div>
<button class="ig-slot-retry-btn" onclick="_igRetryBatchSlot(${msgIdx},${slotIdx},${JSON.stringify(prompt).replace(/"/g, '&quot;')},${JSON.stringify(model).replace(/"/g, '&quot;')})" title="Retry this slot">↻ Retry</button>
</div>`;
}
function _igBatchModels(count) {
if (_igSelectedModel === '__all__') {
const models = [];
for (let i = 0; i < count; i++) models.push(_IG_ALL_MODELS[i % _IG_ALL_MODELS.length]);
return models;
}
return Array(count).fill(_igSelectedModel);
}
async function _igGenerateBatch(prompt, count) {
_igGenerating = true;
const genBtn = document.getElementById('igGenerateBtn');
if (genBtn) genBtn.disabled = true;
try {
let conv = getActiveConv();
if (!conv) {
const now = Date.now();
conv = { id: 'conv-' + now + '-' + Math.random().toString(36).slice(2,8),
title: 'New Chat', messages: [], createdAt: now, updatedAt: now,
activeTaskId: null };
conversations.unshift(conv);
activeConvId = conv.id;
sessionStorage.setItem('tofu_activeConvId', conv.id);
_saveConvToolState();
if (typeof renderConversationList === 'function') renderConversationList();
}
conv.imageGenMode = true;
const userMsg = { role: 'user', content: prompt, timestamp: Date.now(), _isImageGen: true };
_ensureMsgId(userMsg);
conv.messages.push(userMsg);
if (conv.messages.filter(m => m.role === 'user').length === 1) {
conv.title = prompt.slice(0, 50);
renderConversationList();
}
const textarea = document.getElementById('userInput');
if (textarea) { textarea.value = ''; textarea.style.height = 'auto'; }
const chatDiv = document.getElementById('chatInner');
const models = _igBatchModels(count);
const batchId = 'ig-batch-' + Date.now();
const t0 = Date.now();
const igHistory = _igCollectHistory(conv);
const historyCount = igHistory.length;
const pendingResults = models.map((m, i) => ({
ok: false, prompt, model: m, provider_id: '',
aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
image_url: '', remote_image_url: '', file_size: 0, elapsed: '',
response_text: '', error: 'pending', errorType: '',
}));
const assistantMsg = {
role: 'assistant',
content: 'Generating…',
timestamp: Date.now(),
_igResults: pendingResults,
_isImageGen: true,
_igBatchPending: true,
};
_ensureMsgId(assistantMsg);
const msgIdx = conv.messages.length;
conv.messages.push(assistantMsg);
renderChat(conv);
const isAllModels = _igSelectedModel === '__all__';
const bannerText = isAllModels ? `全模型 ${count}连抽!` : `${count}连抽!`;
const historyBadge = historyCount > 0 ? ` · ${historyCount} prior turn${historyCount > 1 ? 's' : ''}` : '';
const gridHtml = `<div class="ig-batch-wrapper" id="${batchId}">
<div class="ig-batch-banner">${bannerText}${historyBadge}</div>
<div class="ig-batch-grid ig-cols-${Math.min(count, 2)}">
${models.map((m, i) => `<div class="ig-batch-slot" id="${batchId}-slot-${i}" data-slot-idx="${i}" data-msg-idx="${msgIdx}">
<div class="ig-generating ig-batch-loading">
<div class="ig-gen-spinner"></div>
<div class="ig-gen-title">${_escapeHtmlBasic(_IG_MODEL_SHORT[m] || m)}</div>
<div class="ig-gen-subtitle">生成中… (${i + 1}/${count})</div>
<div class="ig-gen-timer" id="${batchId}-timer-${i}">0.0s</div>
</div>
</div>`).join('')}
</div>
<div class="ig-batch-footer">
<button class="ig-gen-cancel" onclick="_igCancelGeneration()">✕ 取消全部</button>
</div>
</div>`;
if (chatDiv) {
chatDiv.insertAdjacentHTML('beforeend', gridHtml);
chatDiv.scrollTop = chatDiv.scrollHeight;
}
saveConversations(conv.id);
const slotTimers = models.map((_, i) => {
const timerId = `${batchId}-timer-${i}`;
return setInterval(() => {
const el = document.getElementById(timerId);
if (el) el.textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's';
}, 100);
});
if (historyCount > 0) {
_igToast(`Sending ${historyCount} prior turn${historyCount > 1 ? 's' : ''} for multi-turn editing`, 'info');
}
let completedCount = 0;
_igAbortControllers = models.map(() => new AbortController());
const settled = await Promise.allSettled(models.map((model, i) => {
const body = {
prompt,
model: model,
aspect_ratio: _igSelectedAspect,
resolution: _igSelectedResolution,
};
if (igHistory.length > 0) body.history = igHistory;
return Api.images.generate(body, { signal: _igAbortControllers[i]?.signal }).then(async data => {
clearInterval(slotTimers[i]);
const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
const slotEl = document.getElementById(`${batchId}-slot-${i}`);
if (data.ok && (data.image_url || data.image_b64)) {
const imgSrc = data.image_url
? (data.image_url.startsWith('/') ? apiUrl(data.image_url) : data.image_url)
: `data:${data.mime_type || 'image/png'};base64,${data.image_b64}`;
const sizeStr = data.file_size ? _formatFileSize(data.file_size) : '';
if (slotEl) {
slotEl.innerHTML = `<div class="ig-result-card ig-batch-reveal" style="animation-delay:${i * 0.1}s">
<img src="${imgSrc}" alt="${_escapeHtmlBasic(prompt.slice(0, 60))}" loading="lazy"
onclick="_openImageFullscreen(this.src)" />
<div class="ig-result-footer">
<span class="ig-result-prompt" title="${_escapeHtmlBasic(prompt)}">${_escapeHtmlBasic(_IG_MODEL_SHORT[data.model || model] || data.model || model)}</span>
<div class="ig-result-meta">
${sizeStr ? `<span class="ig-meta-pill">${sizeStr}</span>` : ''}
<span class="ig-meta-pill">${elapsed}s</span>
</div>
<div class="ig-result-actions">
<button onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">⬇</button>
<button onclick="event.stopPropagation();_openImageFullscreen(this.closest('.ig-result-card').querySelector('img').src)" title="Fullscreen">⛶</button>
</div>
</div>
</div>`;
}
assistantMsg._igResults[i] =  ({
ok: true, prompt, model: data.model || model, provider_id: data.provider_id || '',
aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
image_url: data.image_url || '', remote_image_url: data.remote_image_url || '',
file_size: data.file_size || 0, elapsed, response_text: data.text || '', error: '',
});
} else {
const errInfo = _igClassifyError(data, data._status);
if (slotEl) {
slotEl.setAttribute('data-slot-idx', String(i));
slotEl.setAttribute('data-msg-idx', String(msgIdx));
slotEl.innerHTML = _igBatchErrorSlotHtml(errInfo, model, msgIdx, i, prompt);
}
if (errInfo.isRateLimit) {
_igToast(`⏳ Slot ${String(i + 1)} rate limited`, 'warning');
} else if (errInfo.isContentBlocked) {
_igToast(`🚫 Slot ${String(i + 1)} content blocked`, 'error');
}
assistantMsg._igResults[i] = {
ok: false, prompt, model: data.model || model, provider_id: data.provider_id || '',
aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
image_url: '', remote_image_url: '', file_size: 0, elapsed,
response_text: data.text || '', error: errInfo.text, errorType: errInfo.errorType,
};
}
completedCount++;
const okSoFar = assistantMsg._igResults.filter(r => r.ok);
assistantMsg.content = okSoFar.length > 0
? okSoFar.map(r => `![Generated Image](${r.image_url || 'data:image'})`).join('\n\n')
: (completedCount < count ? 'Generating…' : 'All image generations failed');
saveConversations(conv.id);
if (chatDiv) chatDiv.scrollTop = chatDiv.scrollHeight;
return { ...data, _slotIndex: i, _elapsed: elapsed, _model: model };
}).catch(err => {
clearInterval(slotTimers[i]);
const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
const isAbort = err.name === 'AbortError';
const errText = isAbort ? 'Cancelled' : (err.message || 'Request failed');
const slotEl = document.getElementById(`${batchId}-slot-${i}`);
const errInfo = { title: isAbort ? 'Cancelled' : 'Network error', text: errText, errorType: isAbort ? 'cancelled' : 'network', isRateLimit: false, isContentBlocked: false };
if (slotEl) {
slotEl.setAttribute('data-slot-idx', String(i));
slotEl.setAttribute('data-msg-idx', String(msgIdx));
slotEl.innerHTML = _igBatchErrorSlotHtml(errInfo, model, msgIdx, i, prompt);
}
assistantMsg._igResults[i] = {
ok: false, prompt, model: model, provider_id: '',
aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
image_url: '', remote_image_url: '', file_size: 0, elapsed,
response_text: '', error: errText, errorType: errInfo.errorType,
};
completedCount++;
saveConversations(conv.id);
throw err;
});
}));
slotTimers.forEach(t => clearInterval(t));
const footerEl = document.querySelector(`#${batchId} .ig-batch-footer`);
if (footerEl) footerEl.remove();
delete assistantMsg._igBatchPending;
const results = assistantMsg._igResults;
const okResults = results.filter(r => r.ok);
assistantMsg.content = okResults.length > 0
? okResults.map(r => `![Generated Image](${r.image_url || 'data:image'})`).join('\n\n')
: `All ${count} image generations failed`;
if (conv.id === activeConvId) renderChat(conv, true);
saveConversations(conv.id);
syncConversationToServer(conv);
if (conv.id === activeConvId && chatDiv) chatDiv.scrollTop = chatDiv.scrollHeight;
const anyOk = okResults.length > 0;
debugLog(`Batch generation complete: ${okResults.length}/${count} succeeded`, anyOk ? 'success' : 'warning');
} catch (err) {
console.error('[ImageGen] _igGenerateBatch threw:', err);
debugLog(`Batch generation error: ${err?.message || err}`, 'error');
} finally {
_igGenerating = false;
_igAbortControllers = [];
if (genBtn) genBtn.disabled = false;
}
}
function _formatFileSize(bytes) {
if (!bytes || bytes <= 0) return '';
if (bytes < 1024) return bytes + ' B';
if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}
const _escapeHtmlBasic = escapeHtml;
async function _loadIgModels() {
try {
const data = await Api.images.models();
const models = (data && data.models) || [];
if (models.length === 0) return;
const dropdown = document.getElementById('igModelDropdown');
if (!dropdown) return;
function _igIcon(model) {
const brand = typeof _detectBrand === 'function' ? _detectBrand(model) : 'generic';
return typeof _brandSvg === 'function' ? _brandSvg(brand, 14) : '✦';
}
const visible = models.filter(m => !_hiddenIgModels.has(m.model));
if (visible.length === 0) {
dropdown.innerHTML = '<div class="ig-model-option" style="opacity:.5;pointer-events:none"><span class="ig-model-name">No models visible</span></div>';
return;
}
const grouped = {};
for (const m of visible) {
const pid = m.provider_id || 'default';
if (!grouped[pid]) grouped[pid] = { name: m.provider_name || pid, models: [] };
grouped[pid].models.push(m);
}
_IG_ALL_MODELS.length = 0;
for (const m of visible) _IG_ALL_MODELS.push(m.model);
const isAllActive = _igSelectedModel === '__all__';
let html = `<div class="ig-model-option ${isAllActive ? 'active' : ''}" data-model="__all__" onclick="selectIgModel(this)">
<span class="ig-model-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="#f472b6"><rect x="2" y="2" width="9" height="9" rx="2"/><rect x="13" y="2" width="9" height="9" rx="2"/><rect x="2" y="13" width="9" height="9" rx="2"/><rect x="13" y="13" width="9" height="9" rx="2"/></svg></span>
<span class="ig-model-info"><span class="ig-model-name">All Models</span></span>
<span class="ig-model-check">✓</span>
</div><div class="ig-model-divider"></div>`;
let idx = 0;
const providerIds = Object.keys(grouped);
for (const pid of providerIds) {
const group = grouped[pid];
if (providerIds.length > 1) {
html += `<div class="ig-model-section">${_escapeHtmlBasic(group.name)}</div>`;
}
for (const m of group.models) {
const friendlyName = typeof _modelShortName === 'function' ? _modelShortName(m.model) : m.model;
const isActive = !isAllActive && (m.model === _igSelectedModel || (idx === 0 && !visible.find(v => v.model === _igSelectedModel)));
if (isActive) {
_igSelectedModel = m.model;
const label = document.getElementById('igModelLabel');
if (label) label.textContent = friendlyName;
const brand = typeof _detectBrand === 'function' ? _detectBrand(m.model) : 'generic';
const iconEl = document.getElementById('igModelIcon');
const toggle = document.querySelector('.ig-preset');
if (iconEl && typeof _brandSvg === 'function') iconEl.innerHTML = _brandSvg(brand, 14);
if (toggle) toggle.setAttribute('data-brand', brand);
}
_IG_MODEL_SHORT[m.model] = friendlyName;
html += `<div class="ig-model-option ${isActive ? 'active' : ''}" data-model="${_escapeHtmlBasic(m.model)}" onclick="selectIgModel(this)">
<span class="ig-model-icon">${_igIcon(m.model)}</span>
<span class="ig-model-info"><span class="ig-model-name">${_escapeHtmlBasic(friendlyName)}</span></span>
<span class="ig-model-check">✓</span>
</div>`;
idx++;
}
}
dropdown.innerHTML = html;
if (typeof _scheduleReflow === 'function') _scheduleReflow();
} catch (e) {
console.warn('[ImageGen] Failed to load models:', e);
}
}
var _igModelsLoaded = false;
setTimeout(function() { if (!_igModelsLoaded) _loadIgModels(); }, 5000);
;
// ═══ project-brain.js ═══
(function () {
'use strict';
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
var _KIND_ORDER = ['started', 'completed', 'aborted', 'run_concluded',
'claimed', 'blocked', 'decided', 'proposed_decision', 'dismissed', 'note'];
var _state = {
path: '',
maxSeq: 0,
seen: null,
unsub: null,
panelUnsub: null,
cbTimer: null,
tab: 'charter',
tabsWired: false,
};
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
if (typeof ProjectBrainI18n !== 'undefined' && ProjectBrainI18n &&
typeof ProjectBrainI18n.applyAll === 'function') {
try { ProjectBrainI18n.applyAll(); } catch (_e) {  }
}
_onTabSelected(name, prev);
}
function _onTabSelected(name, prev) {
if (name === 'status' && prev !== 'status') {
_refreshStatus(_state.path || _displayedProjectPath());
}
}
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
function _setTabCount(id, n) {
var el = document.getElementById(id);
if (!el) return;
if (n && n > 0) { el.textContent = n > 99 ? '99+' : String(n); el.hidden = false; }
else { el.textContent = ''; el.hidden = true; }
}
var _CLAMP_THRESHOLD = 240;
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
function _applyContentI18n(el) {
if (!el || typeof ProjectBrainI18n === 'undefined' || !ProjectBrainI18n ||
typeof ProjectBrainI18n.apply !== 'function') return;
try { ProjectBrainI18n.apply(el); }
catch (_e) {  }
}
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
if (open && typeof ProjectBrainI18n !== 'undefined' &&
ProjectBrainI18n && typeof ProjectBrainI18n.apply === 'function') {
ProjectBrainI18n.apply(clamp);
}
});
}
}
function projectKeyHash(path) {
if (!path) return '';
return _sha1(String(path)).slice(0, 16);
}
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
var _convPreviewCache = {};
var _previewEl = null;
var _previewHoverId = '';
var _previewTimer = null;
function _fetchConvPreview(convId) {
if (!convId) return Promise.resolve(null);
var cached = _convPreviewCache[convId];
if (cached && typeof cached.then !== 'function') return Promise.resolve(cached);
if (cached && typeof cached.then === 'function') return cached;
var api = (typeof Api !== 'undefined' && Api.conversations) ? Api.conversations : null;
if (!api || typeof api.preview !== 'function') return Promise.resolve(null);
var p = Promise.resolve(api.preview(convId)).then(function (res) {
var rec = res ? {
id: res.id || convId,
title: res.title || '',
firstUserMessage: res.firstUserMessage || '',
msgCount: res.msgCount || 0,
} : null;
_convPreviewCache[convId] = rec;
return rec;
}).catch(function (e) {
if (typeof console !== 'undefined') console.warn('[ProjectBrain] conv preview failed', e);
_convPreviewCache[convId] = null;
return null;
});
_convPreviewCache[convId] = p;
return p;
}
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
function _showConvPreview(anchor) {
var convId = anchor && anchor.getAttribute ? anchor.getAttribute('data-conv-id') : '';
if (!convId) return;
_previewHoverId = convId;
var el = _ensurePreviewEl();
el.innerHTML = '<div class="pb-preview-loading">' +
_esc(_t('projectBrain.previewLoading', 'Loading…')) + '</div>';
_positionPreview(anchor);
_fetchConvPreview(convId).then(function (rec) {
if (_previewHoverId !== convId) return;
el.innerHTML = buildConvPreviewCard(rec, convId);
_positionPreview(anchor);
});
}
function _onOverlayHover(ev) {
var t2 = ev.target;
var anchor = (t2 && t2.closest) ? t2.closest('[data-conv-id]') : null;
if (!anchor || !anchor.getAttribute('data-conv-id')) { _hideConvPreview(); return; }
var convId = anchor.getAttribute('data-conv-id');
if (convId === _previewHoverId && _previewEl && !_previewEl.hidden) return;
if (_previewTimer) clearTimeout(_previewTimer);
_previewTimer = setTimeout(function () { _showConvPreview(anchor); }, 140);
}
function _initConvPreview() {
var overlay = document.getElementById('projectBrainOverlay');
if (!overlay || overlay._pbPreviewWired) return;
overlay.addEventListener('mouseover', _onOverlayHover);
overlay.addEventListener('mouseout', function (ev) {
var to = ev.relatedTarget;
var anchor = (ev.target && ev.target.closest) ? ev.target.closest('[data-conv-id]') : null;
if (anchor && to && anchor.contains && anchor.contains(to)) return;
_hideConvPreview();
});
overlay._pbPreviewWired = true;
}
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
function _absTime(ts) {
var n = Number(ts) || 0;
if (!n) return '';
try { return new Date(n).toLocaleString(); } catch (_e) { return ''; }
}
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
function _ensureActivityEmptyState() {
var list = _activityListEl();
if (!list) return;
if (!list.querySelector('.pb-activity-row')) {
list.innerHTML = '<div class="pb-activity-empty">' +
_esc(_t('projectBrain.activityEmpty', 'No activity yet')) + '</div>';
}
}
function buildActivityRow(ev) {
var row = document.createElement('div');
row.className = 'pb-activity-row pb-kind-' + (ev.kind || 'note');
row.dataset.eventId = ev.event_id || '';
row.dataset.seq = String(ev.seq || 0);
var kindLabel = _t('projectBrain.kind.' + (ev.kind || 'note'), ev.kind || '');
var iconName = _KIND_ICON[ev.kind] || _KIND_ICON.note;
var icon = document.createElement('span');
icon.className = 'pb-activity-icon';
icon.title = kindLabel;
icon.innerHTML = (typeof Icon === 'function') ? Icon(iconName, 15) : '';
row.appendChild(icon);
var body = document.createElement('div');
body.className = 'pb-activity-body';
var summary = document.createElement('div');
summary.className = 'pb-activity-summary';
var fullText = (ev.payload && ev.payload.summary_full) || ev.summary || kindLabel;
summary.innerHTML = _clampBlock(_esc(fullText), fullText);
body.appendChild(summary);
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
function ingestEvent(ev, opts) {
if (!ev || !_state.seen) return false;
var fromBackfill = !!(opts && opts.backfill);
var eid = ev.event_id || '';
if (!fromBackfill && ev.seq && ev.seq <= _state.maxSeq) return false;
if (eid && _state.seen.has(eid)) return false;
if (eid) _state.seen.add(eid);
if (ev.seq && ev.seq > _state.maxSeq) _state.maxSeq = ev.seq;
var list = _activityListEl();
if (list) {
var row = buildActivityRow(ev);
if (list.firstChild) list.insertBefore(row, list.firstChild);
else list.appendChild(row);
_wireClampToggles(row);
if (!fromBackfill) _applyContentI18n(row);
var empty = list.querySelector('.pb-activity-empty');
if (empty) empty.remove();
}
return true;
}
function _onPush(frame) {
if (!frame || frame.type !== 'activity' || !frame.event) return;
ingestEvent(frame.event, { backfill: false });
}
function openFeed(path) {
closeFeed();
if (!path) return;
_state.path = path;
_state.maxSeq = 0;
_state.seen = new Set();
_renderLegend();
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
_ensureActivityEmptyState();
var _al = _activityListEl();
if (_al) _applyContentI18n(_al);
}).catch(function (e) {
if (typeof console !== 'undefined') console.warn('[ProjectBrain] backfill failed', e);
_ensureActivityEmptyState();
});
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
if (_state.unsub) { try { _state.unsub(); } catch (_e) {  } }
_state.unsub = null;
_state.path = '';
_state.maxSeq = 0;
_state.seen = null;
var list = _activityListEl();
if (list) {
list.innerHTML = '';
if (list.parentNode) {
var lg = list.parentNode.querySelector('.pb-activity-legend');
if (lg) lg.remove();
}
}
}
function _displayedProjectPath() {
try {
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
function _esc(s) {
return escapeHtml(String(s == null ? '' : s));
}
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
var props = pendingProposals || [];
if (props.length) {
parts.push('<div class="pb-charter-section">' +
_esc(_t('projectBrain.pendingProposals', 'Proposed (awaiting your review)')) + '</div>');
for (var j = 0; j < props.length; j++) {
var p = props[j];
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
_setTabCount('pbTabCountCharter', props.length);
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
function _charterSrcText(rowEl, srcSelector) {
var node = rowEl.querySelector(srcSelector + ' [data-pb-src]') ||
rowEl.querySelector(srcSelector);
if (!node) return '';
return node.getAttribute('data-pb-src') != null
? node.getAttribute('data-pb-src')
: (node.textContent || '');
}
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
try { ta.focus(); } catch (_e) {  }
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
}).catch(function (e) {
if (typeof console !== 'undefined') console.warn('[ProjectBrain] charter save failed', e);
save.disabled = false; cancel.disabled = false;
save.textContent = _t('projectBrain.save', 'Save');
});
});
}
function _wireCharterEditControls(el, path) {
var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
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
Promise.resolve(api.commitCharter(path, {
add_decision: text, expected_version: expectedVersion,
resolves_proposal: proposalId || '',
})).then(function () {
refreshCharter(path);
}).catch(function (e) {
if (typeof console !== 'undefined') console.warn('[ProjectBrain] commit failed', e);
if (btn) { btn.disabled = false; btn.textContent = _t('projectBrain.commit', 'Commit'); }
});
}
function _dismissProposal(path, proposalId, btn) {
var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
if (!api || !path || typeof api.dismissProposal !== 'function') {
var node = btn && btn.closest ? btn.closest('.pb-proposal') : null;
if (node) node.remove();
return;
}
if (btn) { btn.disabled = true; }
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
function _boardConvId() {
try {
return (typeof activeConvId !== 'undefined' && activeConvId) ? activeConvId : '';
} catch (_e) { return ''; }
}
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
var badge = t.dispatched
? '<span class="pb-board-badge pb-board-badge-dispatched" title="'
+ _esc(_t('projectBrain.dispatchedTitle', 'Started autonomously by the project brain'))
+ '">' + ((typeof Icon === 'function') ? Icon('rocket', 11) : '')
+ '<span>' + _esc(_t('projectBrain.dispatched', 'auto')) + '</span></span>'
: '';
var acts = [];
if (t.status === 'open' || t.status === 'claimed') {
acts.push(_boardActionBtn('complete', 'check', 'projectBrain.actComplete', 'Done'));
acts.push(_boardActionBtn('block', 'ban', 'projectBrain.actBlock', 'Block'));
acts.push(_boardActionBtn('defer', 'clock', 'projectBrain.actDefer', 'Park'));
}
if (t.status === 'claimed' || t.status === 'done') {
acts.push(_boardActionBtn('reopen', 'refresh', 'projectBrain.actReopen', 'Reopen'));
}
if (t.status === 'deferred') {
acts.push(_boardActionBtn('resume', 'play', 'projectBrain.actResume', 'Resume'));
}
var actionsRow = acts.length
? '<div class="pb-board-card-actions">' + acts.join('') + '</div>' : '';
var titleHtml = _clampBlock(_esc(t.title), t.title || '');
return '<div class="pb-board-card pb-board-' + _esc(t.status) + '" data-task-id="' +
_esc(t.id) + '">' +
'<div class="pb-board-title">' + titleHtml + '</div>' +
'<div class="pb-board-card-meta">' + ownerChip + badge + '</div>' +
actionsRow + '</div>';
}
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
function _boardMutate(act, taskId, btn) {
var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
var path = _state.path || _displayedProjectPath();
if (!api || !path || !taskId) return;
var convId = _boardConvId();
var call = null;
if (act === 'complete' && typeof api.boardComplete === 'function') {
call = api.boardComplete(path, taskId, convId);
} else if ((act === 'reopen' || act === 'resume') &&
typeof api.boardReopen === 'function') {
call = api.boardReopen(path, taskId, convId);
} else if (act === 'block' && typeof api.boardBlock === 'function') {
var reason = '';
if (typeof prompt === 'function') {
reason = prompt(_t('projectBrain.blockReasonPrompt', 'Why is this blocked?')) || '';
}
call = api.boardBlock(path, taskId, convId, reason);
} else if (act === 'defer' && typeof api.boardDefer === 'function') {
var dreason = '';
if (typeof prompt === 'function') {
dreason = prompt(_t('projectBrain.deferReasonPrompt',
'Park this epic — what human decision is it waiting on?')) || '';
}
call = api.boardDefer(path, taskId, convId, dreason);
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
var cols = { open: [], claimed: [], deferred: [], done: [] };
var held = [];
for (var i = 0; i < tasks.length; i++) {
var t = tasks[i];
if (t.kind === 'lease') { held.push(t); continue; }
(cols[t.status] || cols.open).push(t);
}
_setTabCount('pbTabCountBoard', cols.open.length + cols.claimed.length);
function lane(key, labelKey) {
var cards = cols[key].map(_boardCard).join('') ||
'<div class="pb-board-lane-empty">—</div>';
return '<div class="pb-board-lane pb-board-lane-' + key + '">' +
'<div class="pb-board-lane-head">' + _esc(_t(labelKey, key)) +
' <span class="pb-board-count">' + cols[key].length + '</span></div>' +
cards + '</div>';
}
var hasConv = !!_boardConvId();
var newBtn = '<button type="button" class="pb-board-new" id="pbBoardNewBtn"' +
(hasConv ? '' : ' disabled') + ' title="' +
_esc(hasConv ? _t('projectBrain.newEpic', 'New epic')
: _t('projectBrain.newEpicNoConv',
'Open a conversation to post an epic')) + '">' +
((typeof Icon === 'function') ? Icon('plus', 13) : '') +
'<span>' + _esc(_t('projectBrain.newEpic', 'New epic')) + '</span></button>';
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
el.innerHTML =
'<div class="pb-board-toolbar">' + newBtn + '</div>' +
lane('open', 'projectBrain.laneOpen') +
lane('claimed', 'projectBrain.laneClaimed') +
heldLane +
(cols.deferred.length ? lane('deferred', 'projectBrain.laneDeferred') : '') +
lane('done', 'projectBrain.laneDone');
var chips = el.querySelectorAll('.pb-conv-chip');
for (var c = 0; c < chips.length; c++) {
chips[c].addEventListener('click', function (ev) {
var cid = ev.currentTarget.getAttribute('data-conv-id');
if (cid && typeof loadConversation === 'function') loadConversation(cid);
});
}
_wireClampToggles(el);
_applyContentI18n(el);
var nb = el.querySelector('#pbBoardNewBtn');
if (nb && !nb.disabled) nb.addEventListener('click', _boardPostNew);
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
function _influenceChip(glyph, n, labelKey, fallbackLabel, cls) {
if (!n) return '';
var label = _t('projectBrain.' + labelKey, fallbackLabel).replace('{n}', n);
return '<span class="pb-inf-chip ' + (cls || '') + '">' +
((typeof Icon === 'function') ? Icon(glyph, 12) : '') +
'<span>' + _esc(label) + '</span></span>';
}
function _influenceEpicRow(t, cls) {
var owner = t.owner
? '<button type="button" class="pb-conv-chip" data-conv-id="' + _esc(t.owner) + '">' +
_esc(t.owner) + '</button>'
: '';
return '<div class="pb-inf-epic ' + (cls || '') + '">' +
'<span class="pb-inf-epic-title">' + _esc(t.title || t.id) + '</span>' +
owner + '</div>';
}
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
if (!charterActive && !mine.length && !avoid.length && !open.length &&
!pending.length) {
banner.hidden = true;
body.innerHTML = '';
if (convEl) convEl.textContent = '';
return;
}
banner.hidden = false;
if (convEl) {
var label = '';
try {
var conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
label = (conv && (conv.title || conv.id)) || inf.convId || '';
} catch (_e) { label = inf.convId || ''; }
convEl.textContent = label ? ('· ' + label) : '';
}
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
var chipsEls = body.querySelectorAll('.pb-conv-chip');
for (var c = 0; c < chipsEls.length; c++) {
chipsEls[c].addEventListener('click', function (ev) {
var cid = ev.currentTarget.getAttribute('data-conv-id');
if (cid && typeof loadConversation === 'function') loadConversation(cid);
});
}
}
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
if (typeof activeConvId !== 'undefined' && inf && inf.convId &&
inf.convId !== activeConvId) return;
renderInfluence(inf || {});
}).catch(function (e) {
if (typeof console !== 'undefined') console.warn('[ProjectBrain] influence load failed', e);
if (banner) banner.hidden = true;
});
}
function _subscribePanelLive(path) {
_unsubscribePanelLive();
if (typeof pushSubscribe !== 'function' || !path) return;
var handler = function () {
var cur = _displayedProjectPath();
if (!cur || cur !== path) return;
if (_state.cbTimer) clearTimeout(_state.cbTimer);
_state.cbTimer = setTimeout(function () {
_state.cbTimer = null;
refreshCharter(path);
refreshBoard(path);
refreshInfluence(path);
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
if (_state.panelUnsub) { try { _state.panelUnsub(); } catch (_e) {  } }
_state.panelUnsub = null;
}
function openProjectBrain() {
var overlay = document.getElementById('projectBrainOverlay');
if (!overlay) return;
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
try { ProjectBrainI18n.initToggle(); } catch (_e) {  }
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
function _refreshPeers(path) {
if (typeof window.ProjectBrainPeers !== 'undefined' &&
window.ProjectBrainPeers &&
typeof window.ProjectBrainPeers.refreshPeers === 'function') {
window.ProjectBrainPeers.refreshPeers(path);
}
}
function _refreshStatus(path) {
if (typeof window.ProjectBrainStatus !== 'undefined' &&
window.ProjectBrainStatus &&
typeof window.ProjectBrainStatus.refreshStatus === 'function') {
window.ProjectBrainStatus.refreshStatus(path);
}
}
function openProjectBrainInfluence() {
openProjectBrain();
var banner = document.getElementById('projectBrainInfluence');
if (!banner) return;
setTimeout(function () {
if (banner.hidden) return;
try { banner.scrollIntoView({ block: 'start', behavior: 'smooth' }); }
catch (_e) {  }
banner.classList.remove('pb-influence-flash');
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
refreshInfluence(path);
_refreshPeers(path);
} else {
closeFeed();
_unsubscribePanelLive();
var banner = document.getElementById('projectBrainInfluence');
if (banner) { banner.hidden = true; }
}
}
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
;
// ═══ project-brain-peers.js ═══
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
function _shortConv(cid) { return String(cid || '').slice(0, 8); }
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
function _peerState(p) {
if (p && p.taskStatus === 'running') return 'active';
return 'idle';
}
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
function buildPeerCard(p) {
var card = document.createElement('div');
var isAgent = !!(p && p.agentId);
card.className = 'pb-peer-card' + (isAgent ? ' pb-peer-agent' : '');
card.dataset.convId = (p && p.convId) || '';
var dot = document.createElement('span');
dot.className = 'pb-peer-dot';
dot.dataset.state = _peerState(p);
card.appendChild(dot);
var body = document.createElement('div');
body.className = 'pb-peer-body';
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
var cid = (p && p.convId) || '';
if (cid && !isAgent) {
var ctl = document.createElement('div');
ctl.className = 'pb-peer-controls';
ctl.appendChild(_buildNudgeAffordance(cid));
if (p && p.taskStatus === 'running') {
ctl.appendChild(_buildStopAffordance(cid, title));
}
body.appendChild(ctl);
}
card.appendChild(body);
if (cid) {
card.classList.add('pb-peer-clickable');
card.addEventListener('click', function (e) {
if (e.target && e.target.closest &&
(e.target.closest('.pb-peer-nudge') || e.target.closest('.pb-peer-stop'))) return;
if (typeof loadConversation === 'function') loadConversation(cid);
});
}
return card;
}
function _actingConvId() {
try {
return (typeof activeConvId !== 'undefined' && activeConvId) ? activeConvId : '';
} catch (_e) { return ''; }
}
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
ta.addEventListener('keydown', function (e) {
if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); _send(); }
});
return wrap;
}
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
function _buildThreadRow(m) {
var row = document.createElement('div');
var isIntervene = m.kind === 'intervention' || m.kind === 'hard_abort';
row.className = 'pb-peer-msg' + (isIntervene ? ' pb-peer-msg-intervene' : '');
var head = document.createElement('div');
head.className = 'pb-peer-msg-head';
var glyph = isIntervene ? 'alertTriangle' : 'messageSquare';
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
if (m.summary) bodyEl.setAttribute('data-pb-src', m.summary);
row.appendChild(bodyEl);
return row;
}
function renderPeers(status, thread) {
var el = _peersBodyEl();
if (!el) return;
status = status || {};
var peers = status.peers || [];
thread = thread || [];
var parts = document.createDocumentFragment();
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
if (thread.length) {
var threadWrap = document.createElement('div');
threadWrap.className = 'pb-peers-thread';
var thead = document.createElement('div');
thead.className = 'pb-peers-thread-head';
thead.textContent = _t('projectBrain.peerThread', 'Cross-conversation messages');
threadWrap.appendChild(thead);
var recent = thread.slice(-30);
for (var j = 0; j < recent.length; j++) {
threadWrap.appendChild(_buildThreadRow(recent[j]));
}
parts.appendChild(threadWrap);
}
el.innerHTML = '';
el.appendChild(parts);
if (typeof ProjectBrainI18n !== 'undefined' && ProjectBrainI18n &&
typeof ProjectBrainI18n.apply === 'function') {
try { ProjectBrainI18n.apply(el); } catch (_e) {  }
}
var badge = document.getElementById('pbTabCountPeers');
if (badge) {
if (peers.length > 0) {
badge.textContent = peers.length > 99 ? '99+' : String(peers.length);
badge.hidden = false;
} else { badge.textContent = ''; badge.hidden = true; }
}
}
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
if (path !== _displayedPeersPath()) return;
var status = res[0] || { peers: [], count: 0 };
var thread = extractPeerThread((res[1] && res[1].events) || []);
renderPeers(status, thread);
});
}
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
;
// ═══ project-brain-status.js ═══
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
var _pollTimer = null;
var _pollPath = '';
var _POLL_MS = 2500;
var _POLL_MAX = 8;
function _stopPoll() {
if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
_pollPath = '';
}
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
function renderStatus(data) {
var el = _statusBodyEl();
if (!el) return;
data = data || {};
var latest = data.latest || null;
var history = data.history || [];
var refreshing = !!data.refreshing;
var frag = document.createDocumentFragment();
frag.appendChild(_buildStatusHeader(refreshing));
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
latestWrap.appendChild(_buildSkeleton());
} else {
var empty = document.createElement('div');
empty.className = 'pb-status-empty';
empty.textContent = _t('projectBrain.statusEmpty',
'No status yet — synthesized once the project has a charter or board activity.');
latestWrap.appendChild(empty);
}
frag.appendChild(latestWrap);
frag.appendChild(_buildAskComposer());
var watchWrap = document.createElement('div');
watchWrap.className = 'pb-watch';
watchWrap.id = 'pbWatchSection';
frag.appendChild(watchWrap);
if (history.length) {
var histWrap = document.createElement('div');
histWrap.className = 'pb-status-history';
var head = document.createElement('div');
head.className = 'pb-status-history-head';
head.textContent = _t('projectBrain.statusHistory', 'Status history');
histWrap.appendChild(head);
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
try { ProjectBrainI18n.apply(el); } catch (_e) {  }
}
_refreshWatch(_displayedStatusPath());
}
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
var hasContent = !!el.querySelector('.pb-status-narrative, .pb-status-latest');
if (!hasContent) {
el.innerHTML = '';
var sk = document.createElement('div');
sk.className = 'pb-status-latest';
sk.appendChild(_buildSkeleton());
el.appendChild(sk);
}
Promise.resolve(api.brainStatus(path, { force: force })).then(function (data) {
if (path !== _displayedStatusPath()) return;
data = data || {};
renderStatus(data);
if (data.refreshing) _startPoll(path, (data.maxSeq | 0));
}).catch(function (e) {
if (path !== _displayedStatusPath()) return;
if (typeof console !== 'undefined') console.warn('[ProjectBrain] status load failed', e);
renderStatus({});
});
}
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
_stopPoll();
renderStatus({ latest: snaps[0], history: snaps,
maxSeq: maxSeq, refreshing: false });
return;
}
if (attempts >= _POLL_MAX) {
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
var _WATCH_KINDS = ['concern', 'question', 'goal'];
function _kindLabel(kind) {
var m = {
concern: _t('projectBrain.watchKindConcern', 'Concern'),
question: _t('projectBrain.watchKindQuestion', 'Question'),
goal: _t('projectBrain.watchKindGoal', 'Goal'),
};
return m[kind] || kind || '';
}
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
try { ProjectBrainI18n.apply(host); } catch (_e) {  }
}
}
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
function _watchConvId() {
try {
if (typeof window.ProjectBrain !== 'undefined' && window.ProjectBrain &&
typeof window.ProjectBrain._boardConvId === 'function') {
return window.ProjectBrain._boardConvId() || '';
}
if (typeof activeConvId !== 'undefined' && activeConvId) return activeConvId;
} catch (_e) {  }
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
;
// ═══ project-brain-i18n.js ═══
var ProjectBrainI18n = (function () {
'use strict';
var PREF_KEY = 'tofu_pb_translate';
var POOL_LIMIT = 6;
var _memCache = Object.create(null);
function isEnabled() {
try { return localStorage.getItem(PREF_KEY) !== '0'; }
catch (_e) { return true; }
}
function _setEnabled(on) {
try { localStorage.setItem(PREF_KEY, on ? '1' : '0'); }
catch (_e) {  }
}
function targetLang() {
var lang = (typeof _i18nLang !== 'undefined' && _i18nLang) ? _i18nLang : 'zh';
return lang === 'zh' ? 'Chinese' : 'English';
}
function _cjkRatio(s) {
var t = String(s == null ? '' : s).replace(/\s+/g, '');
if (!t) return 0;
var m = t.match(/[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af\uf900-\ufaff]/g);
return m ? m.length / t.length : 0;
}
function _alreadyTarget(text, target) {
var r = _cjkRatio(text);
if (target === 'Chinese') return r >= 0.30;
return r < 0.10;
}
function _hash32(str) {
var h = 0x811c9dc5;
str = String(str == null ? '' : str);
for (var i = 0; i < str.length; i++) {
h ^= str.charCodeAt(i);
h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
}
return h.toString(36);
}
function _cacheKey(src, target) { return _hash32(src) + '|' + target; }
function _esc(s) {
return escapeHtml(String(s == null ? '' : s));
}
var DB_NAME = 'tofu_pb_translate';
var STORE = 'tr';
var _db = null, _dbP = null, _idbAvail = true;
function _openDB() {
if (_dbP) return _dbP;
if (!_idbAvail) return Promise.resolve(null);
_dbP = new Promise(function (resolve) {
try {
if (typeof indexedDB === 'undefined') { _idbAvail = false; resolve(null); return; }
var req = indexedDB.open(DB_NAME, 1);
req.onupgradeneeded = function (e) {
var db = e.target.result;
if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: 'k' });
};
req.onsuccess = function (e) { _db = e.target.result; resolve(_db); };
req.onerror = function () {
if (typeof console !== 'undefined') console.warn('[PB-i18n] IDB open failed');
_idbAvail = false; resolve(null);
};
} catch (err) {
if (typeof console !== 'undefined') console.warn('[PB-i18n] IDB init error:', err && err.message);
_idbAvail = false; resolve(null);
}
});
return _dbP;
}
function _idbGet(key) {
return _openDB().then(function (db) {
if (!db) return null;
return new Promise(function (resolve) {
try {
var tx = db.transaction(STORE, 'readonly');
var r = tx.objectStore(STORE).get(key);
r.onsuccess = function () { resolve(r.result ? r.result.v : null); };
r.onerror = function () { resolve(null); };
} catch (_e) { resolve(null); }
});
});
}
function _idbPut(key, val) {
return _openDB().then(function (db) {
if (!db) return;
try {
var tx = db.transaction(STORE, 'readwrite');
tx.objectStore(STORE).put({ k: key, v: val, t: Date.now() });
} catch (e) {
if (typeof console !== 'undefined') console.debug('[PB-i18n] IDB put skipped:', e && e.message);
}
});
}
var _pendingSwaps = [];
var _rafScheduled = false;
function _scheduleSwap(el, src, translated) {
_pendingSwaps.push({ el: el, src: src, translated: translated });
if (_rafScheduled) return;
_rafScheduled = true;
var raf = (typeof requestAnimationFrame === 'function')
? requestAnimationFrame : function (f) { return setTimeout(f, 16); };
raf(function () {
_rafScheduled = false;
var batch = _pendingSwaps; _pendingSwaps = [];
for (var i = 0; i < batch.length; i++) _applySwap(batch[i]);
});
}
function _applySwap(item) {
var el = item.el;
if (!el || !el.getAttribute) return;
if (el.getAttribute('data-pb-src') !== item.src) return;
if (el._pbShown === item.translated) return;
el.innerHTML = _esc(item.translated);
el.title = item.src;
el.setAttribute('data-pb-tr', '1');
el._pbShown = item.translated;
}
function _revert(el) {
if (!el || !el.getAttribute) return;
if (!el.getAttribute('data-pb-tr') && el._pbShown === undefined) return;
var src = el.getAttribute('data-pb-src') || '';
if (el._pbShown !== null && el._pbShown !== undefined) {
el.innerHTML = _esc(src);
}
el.removeAttribute('data-pb-tr');
if (el.title) el.title = '';
el._pbShown = null;
}
function _runPool(items, worker, limit) {
limit = limit || POOL_LIMIT;
return new Promise(function (resolve) {
var i = 0, active = 0, done = false;
function step() {
if (done) return;
if (i >= items.length && active === 0) { done = true; resolve(); return; }
while (active < limit && i < items.length) {
var it = items[i++]; active++;
Promise.resolve(worker(it)).catch(function () {}).then(function () {
active--; step();
});
}
}
step();
});
}
function _translateOne(src, target) {
var api = (typeof Api !== 'undefined' && Api.translate) ? Api.translate : null;
if (!api || typeof api.run !== 'function') return Promise.resolve(null);
return Promise.resolve(
api.run({ text: src, targetLang: target, sourceLang: '' }, { onError: 'null' })
).then(function (d) {
if (d && d._ok && d.translated) return d.translated;
return null;
}).catch(function (e) {
if (typeof console !== 'undefined') console.warn('[PB-i18n] translate failed:', e && e.message);
return null;
});
}
function _isVisible(el) {
if (!el || !el.closest) return true;
var panel = el.closest('.pb-tab-panel');
if (!panel) return true;
return panel.classList.contains('pb-tab-panel-active');
}
function _deferUntilExpand(el) {
if (!el || !el.classList) return false;
if (!el.classList.contains('pb-clamp')) return false;
return !el.classList.contains('pb-clamp-open');
}
function apply(root) {
if (!root || typeof root.querySelectorAll !== 'function') return;
var nodes = root.querySelectorAll('[data-pb-src]');
var self = (root.getAttribute && root.getAttribute('data-pb-src') != null) ? [root] : [];
var all = self.concat(Array.prototype.slice.call(nodes));
if (!all.length) return;
var enabled = isEnabled();
var target = targetLang();
var byKey = Object.create(null);
var needIdb = [];
for (var i = 0; i < all.length; i++) {
var el = all[i];
var src = el.getAttribute('data-pb-src') || '';
if (!enabled || !src || _alreadyTarget(src, target) || !_isVisible(el)) {
_revert(el);
continue;
}
if (_deferUntilExpand(el)) { _revert(el); continue; }
var key = _cacheKey(src, target);
var cached = _memCache[key];
if (cached != null) { _scheduleSwap(el, src, cached); continue; }
if (!byKey[key]) { byKey[key] = { src: src, els: [] }; needIdb.push(key); }
byKey[key].els.push(el);
}
if (!needIdb.length) return;
Promise.all(needIdb.map(function (key) {
return _idbGet(key).then(function (v) {
if (v != null) {
_memCache[key] = v;
var grp = byKey[key];
for (var j = 0; j < grp.els.length; j++) _scheduleSwap(grp.els[j], grp.src, v);
return null;
}
return key;
});
})).then(function (missKeys) {
var toFetch = missKeys.filter(function (k) { return !!k; });
if (!toFetch.length) return;
return _runPool(toFetch, function (key) {
var grp = byKey[key];
return _translateOne(grp.src, target).then(function (translated) {
if (translated == null) return;
_memCache[key] = translated;
_idbPut(key, translated);
for (var j = 0; j < grp.els.length; j++) _scheduleSwap(grp.els[j], grp.src, translated);
});
});
});
}
function applyAll() {
var overlay = document.getElementById('projectBrainOverlay');
if (overlay && !overlay.hidden) apply(overlay);
var banner = document.getElementById('projectBrainInfluence');
if (banner && !banner.hidden) apply(banner);
}
function _syncToggleBtn() {
var btn = document.getElementById('projectBrainTranslateToggle');
if (!btn) return;
var on = isEnabled();
btn.setAttribute('aria-pressed', on ? 'true' : 'false');
btn.classList.toggle('pb-tr-toggle-on', on);
var ico = btn.querySelector('.pb-tr-toggle-ico');
if (ico && !ico.innerHTML && typeof Icon === 'function') {
ico.innerHTML = Icon('languages', 14);
}
var lbl = btn.querySelector('.pb-tr-toggle-label');
if (lbl && typeof t === 'function') lbl.textContent = t('projectBrain.translateToggle');
if (typeof t === 'function') btn.title = t('projectBrain.translateToggleTitle');
}
function initToggle() {
var btn = document.getElementById('projectBrainTranslateToggle');
if (!btn) return;
if (!btn._pbTrWired) {
btn.addEventListener('click', toggle);
btn._pbTrWired = true;
}
_syncToggleBtn();
}
function toggle() {
_setEnabled(!isEnabled());
_syncToggleBtn();
applyAll();
}
return {
isEnabled: isEnabled,
toggle: toggle,
initToggle: initToggle,
targetLang: targetLang,
apply: apply,
applyAll: applyAll,
_alreadyTarget: _alreadyTarget,
_cacheKey: _cacheKey,
_revert: _revert,
_memCache: _memCache,
};
})();
if (typeof window !== 'undefined') window.ProjectBrainI18n = ProjectBrainI18n;
;

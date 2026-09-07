/* ===== migrated source: orchestration-catalog.js ===== */
/* ═══════════════════════════════════
   orchestration-catalog.js — role/control/glyph/icon catalog
   Extracted from orchestration.js (2026-07). Pure data + icon-URL
   helpers, read at RUNTIME by both orchestration.js and task-mode.js
   (typeof-guarded). Plain window-scope concatenation — no exports.
   MUST load before the other orchestration-*.js consumers and task-mode.js
   in the migrated lazy-module graph. */

// ── Catalogue: role agents (tofu mascots) ──────────────────────────
// `icon` is a file under static/icons/. This catalogue is presentation-only;
// behavioral defaults (tier/isolation/emits) come from authoring-contract.
var _ORCH_ROLES = [
  { role: 'planner',     label: 'Planner',     icon: 'tofu-planner.svg',
    blurb: 'Rewrites the ask into a structured brief + checklist.' },
  { role: 'worker',      label: 'Worker',      icon: 'tofu-worker.svg',
    blurb: 'Executes the plan with full tools. Stateful across loops.' },
  { role: 'critic',      label: 'Critic',      icon: 'tofu-critic.svg',
    blurb: 'Reviews work against the checklist. Emits a verdict.' },
  { role: 'researcher',  label: 'Researcher',  icon: 'tofu-researcher',
    blurb: 'Gathers + verifies info from web sources.' },
  { role: 'coder',       label: 'Coder',       icon: 'tofu-coder',
    blurb: 'Reads / writes / edits code across files.' },
  { role: 'analyst',     label: 'Analyst',     icon: 'tofu-analyst',
    blurb: 'Quantitative analysis of on-disk data.' },
  { role: 'reviewer',    label: 'Reviewer',    icon: 'tofu-critic.svg',
    blurb: 'Fresh second-opinion read. Outputs a punch list.' },
  { role: 'writer',      label: 'Writer',      icon: 'tofu-writer',
    blurb: 'Long-form prose from raw inputs.' },
  { role: 'browser',     label: 'Browser',     icon: 'tofu-browser',
    blurb: 'Interacts with live browser tabs.' },
  { role: 'synthesizer', label: 'Synthesizer', icon: 'tofu-synthesizer',
    blurb: 'Merges many agent outputs into one converged result.' },
  { role: 'virtual_user', label: 'Virtual User', icon: 'tofu-general',
    blurb: 'Stands in for the human: auto-replies to keep a task going until done. Speaks as User.' },
  { role: 'router',      label: 'Router',      icon: 'tofu-router',
    blurb: 'Classifies each item and routes it to a branch.' },
  { role: 'general',     label: 'General',     icon: 'tofu-general',
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
  minus:   _orchSvg('<path d="M5 12h14"/>'),
  fit:     _orchSvg('<path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5"/><path d="M8 8h8v8H8z"/>'),
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
  download: _orchSvg('<path d="M12 3v12M7 10l5 5 5-5"/><path d="M5 21h14"/>'),
  refresh: _orchSvg('<path d="M20 6v5h-5"/><path d="M4 18v-5h5"/><path d="M6.1 9a7 7 0 0 1 11.7-2.6L20 9M4 15l2.2 2.6A7 7 0 0 0 17.9 15"/>'),
  undo:    _orchSvg('<path d="M9 7l-4 4 4 4"/><path d="M5 11h8a6 6 0 0 1 6 6"/>'),
  redo:    _orchSvg('<path d="M15 7l4 4-4 4"/><path d="M19 11h-8a6 6 0 0 0-6 6"/>'),
  panels:  _orchSvg('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 4v16M16 4v16"/>'),
  chevronDown: _orchSvg('<path d="M6 9l6 6 6-6"/>'),
  stop:    '<svg class="orch-ico" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
};


function _orchIconBase() {
  return (typeof BASE_PATH !== 'undefined' ? BASE_PATH : '') + '/static/icons';
}

// Resolve a role icon to a full URL. An `icon` carrying an explicit
// extension (e.g. 'tofu-worker.svg') is used as-is; otherwise '.png' is
// appended. Lets crisp SVGs and cleaned PNGs coexist in _ORCH_ROLES.
// Cache-bust token for role icons. Bump when icon art is regenerated so
// browsers re-fetch instead of serving the stale (max-age=86400) bytes.
var _ORCH_ICON_VER = '20260622a';

function _orchIconSrc(icon) {
  var name = icon || 'tofu-general';
  var file = /\.\w+$/.test(name) ? name : name + '.png';
  return _orchIconBase() + '/' + file + '?v=' + _ORCH_ICON_VER;
}


/* ═══════════════════════════════════════════════════════════════════
   info-rail.js — Per-turn context note

   Each USER turn carries a small, frozen-in-time note (rendered on the
   right side of the turn, inside the chat column) describing the context
   that was active AT THE MOMENT THE TURN WAS SENT:

     • Workspace — which project folder(s) were wired to the conversation.
     • Tools — which capabilities were switched on for that turn.
     • Model — the model + thinking depth the turn used.

   WHY this replaced the old live-mirroring right-gutter rail: the previous
   rail re-rendered the CURRENT input-box/toolbar state in real time, so it
   told you nothing about any past turn and it overlapped the turn-nav in
   the gutter. The information is far more useful frozen per-turn: scrolling
   back through a conversation you can see exactly which model / tools /
   project each turn ran with.

   ── How it works ──
   1. `buildTurnCtxSnapshot()` reads the same in-memory state the old rail
      read (projectState, toolbar globals, config) and returns a plain,
      serializable snapshot. The send pipeline calls this when the user
      message is created and stores it on `userMsg._ctx` (and in the send
      payload, so the backend persists it → survives reload).
   2. `renderTurnCtxNote(snapshot)` turns that snapshot into the note HTML.
      `renderMessage()` (static/js/ui/chat_render.js) calls it for user
      turns and splices the result under the message body.

   Both are pure functions over existing state — no DOM ownership, no
   fetches, no timers. Public API:
     window.buildTurnCtxSnapshot()      — capture current context (or null).
     window.renderTurnCtxNote(snapshot) — snapshot → note HTML string.
   ═══════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  /* NOTE on global access: the toolbar state (searchMode, browserEnabled,
   * …) and `config`/`serverModel` are declared with `let` in core.js. Such
   * top-level `let`/`const` bindings live in the shared SCRIPT lexical scope
   * — readable as bare identifiers by every classic script (bundled or dev)
   * — but they are NOT properties of `window`. So we reference them directly,
   * each guarded by `typeof` against load-order races. Do NOT switch to
   * `window[name]`: it would always be undefined. */

  function _short(p) {
    const parts = String(p).split('/').filter(Boolean);
    if (parts.length <= 2) return p;
    return parts.slice(-2).join('/');
  }

  /* ── Collect workspace roots from projectState ───────────────────
   * Returns [{path, short, readOnly}], primary first. */
  function _collectRoots() {
    const ps = (typeof projectState !== 'undefined') ? projectState : null;
    if (!ps || !ps.active || !ps.path) return [];
    const out = [];
    out.push({ path: ps.path, short: _short(ps.path), readOnly: !!ps.readOnly });
    if (Array.isArray(ps.extraRoots)) {
      for (const r of ps.extraRoots) {
        const p = typeof r === 'string' ? r : (r && r.path);
        if (!p || out.some((o) => o.path === p)) continue;
        const ro = (typeof r === 'object' && r) ? !!r.readOnly : false;
        out.push({ path: p, short: _short(p), readOnly: ro });
      }
    }
    return out;
  }

  /* ── MCP rail state ──────────────────────────────────────────────
   * MCP tools are NOT a composer toggle — they come from connected MCP
   * servers (lib/mcp). The settings tab tracks them, but the per-turn
   * capsule needs a cheap, always-available snapshot of "which MCP
   * servers are connected right now", independent of whether the
   * settings panel was ever opened. We keep a tiny module-level cache,
   * refreshed at boot and after any catalog mutation (install / connect
   * / uninstall) via window.refreshMcpRailState(). Shape:
   *   { servers: [{name, count}], total: <toolCount> }. */
  let _mcpRail = { servers: [], total: 0 };

  async function refreshMcpRailState() {
    try {
      if (typeof Api === 'undefined' || !Api.mcp || typeof Api.mcp.toolsList !== 'function') return;
      const resp = await Api.mcp.toolsList();
      if (!resp || !resp.ok) return;
      const data = await resp.json();
      const counts = {};
      for (const tdef of (data.tools || [])) {
        const s = tdef && tdef.server;
        if (s) counts[s] = (counts[s] || 0) + 1;
      }
      _mcpRail = {
        servers: Object.keys(counts).sort().map((n) => ({ name: n, count: counts[n] })),
        total: data.total || 0,
      };
    } catch (e) {
      /* Best-effort: a missing/failed MCP endpoint just means no MCP chip. */
      if (typeof console !== 'undefined') console.debug('[info-rail] MCP rail refresh failed:', e);
    }
  }

  /* ── Collect the active-tool set ─────────────────────────────────
   * Returns [{label, tone}]. Only ENABLED tools are listed (plus the
   * search mode, which is shown whenever it's not "off", and each
   * connected MCP server). */
  function _collectTools() {
    const out = [];
    const sm = (typeof searchMode !== 'undefined') ? searchMode : 'off';
    if (sm && sm !== 'off') {
      out.push({ label: sm === 'single' ? 'Search' : 'Search ×N', tone: 'search' });
    }
    if (typeof fetchEnabled !== 'undefined' && fetchEnabled) out.push({ label: 'Fetch', tone: 'net' });
    if (typeof browserEnabled !== 'undefined' && browserEnabled) out.push({ label: 'Browser', tone: 'net' });
    if (typeof desktopEnabled !== 'undefined' && desktopEnabled) out.push({ label: 'Desktop', tone: 'net' });
    if (typeof codeExecEnabled !== 'undefined' && codeExecEnabled) out.push({ label: 'Code Exec', tone: 'code' });
    if (typeof memoryEnabled !== 'undefined' && memoryEnabled) out.push({ label: 'Memory', tone: 'ai' });
    if (typeof schedulerEnabled !== 'undefined' && schedulerEnabled) out.push({ label: 'Scheduler', tone: 'ai' });
    if (typeof imageGenEnabled !== 'undefined' && imageGenEnabled) out.push({ label: 'Image Gen', tone: 'ai' });
    if (typeof humanGuidanceEnabled !== 'undefined' && humanGuidanceEnabled) out.push({ label: 'Ask User', tone: 'ai' });
    if (typeof autoTranslate !== 'undefined' && autoTranslate) out.push({ label: 'Translate', tone: 'ai' });
    // MCP: one chip per connected server, labeled "MCP: <server> ×N".
    // MCP is on by default (no composer toggle); a server being connected
    // means its tools are live for the turn.
    for (const srv of (_mcpRail.servers || [])) {
      const cnt = srv.count > 1 ? ' ×' + srv.count : '';
      out.push({ label: 'MCP: ' + srv.name + cnt, tone: 'mcp' });
    }
    return out;
  }

  /* ── Collect the active orchestration mode(s) ────────────────────
   * Modes (Endpoint / Autopilot / Swarm / a named flow) are distinct
   * from tools: they change HOW the turn runs, not which capability it
   * can reach. They get their own always-visible badge on the collapsed
   * bar. A flow supersedes the endpoint/autopilot toggles. Returns
   * [{label, tone:'mode'}]. */
  function _collectModes() {
    const out = [];
    const flow = (typeof activeFlow !== 'undefined') ? activeFlow : '';
    if (flow) {
      const name = (typeof _flowDisplayName === 'function') ? _flowDisplayName(flow) : 'Flow';
      out.push({ label: name, tone: 'mode' });
    } else {
      if (typeof endpointEnabled !== 'undefined' && endpointEnabled) out.push({ label: 'Endpoint', tone: 'mode' });
      if (typeof autopilotEnabled !== 'undefined' && autopilotEnabled) out.push({ label: 'Autopilot', tone: 'mode' });
    }
    if (typeof swarmEnabled !== 'undefined' && swarmEnabled) out.push({ label: 'Swarm', tone: 'mode' });
    return out;
  }

  function _resolveModel() {
    if (typeof config !== 'undefined' && config && config.model) return config.model;
    if (typeof serverModel !== 'undefined' && serverModel) return serverModel;
    return '';
  }

  function _esc(s) {
    return escapeHtml(String(s));
  }

  /**
   * Capture the current context as a plain serializable snapshot.
   * Returns null when there's nothing worth recording.
   */
  function buildTurnCtxSnapshot() {
    const modelId = _resolveModel();
    const depthRaw = (typeof config !== 'undefined' && config && config.thinkingDepth) || '';
    const _isThink = (typeof _isThinkingCapable === 'function') ? _isThinkingCapable(modelId) : false;
    const snap = {
      roots: _collectRoots().map((r) => ({ short: r.short, path: r.path, ro: r.readOnly })),
      tools: _collectTools(),
      modes: _collectModes(),
      model: modelId,
      depth: (_isThink && depthRaw) ? depthRaw : '',
    };
    if (!snap.roots.length && !snap.tools.length && !snap.modes.length && !snap.model) return null;
    return snap;
  }

  function _modelLabel(modelId) {
    if (!modelId) return '';
    return (typeof _modelShortName === 'function') ? _modelShortName(modelId) : modelId;
  }

  /* Real provider brand logo (Anthropic / OpenAI / Gemini / …) for a model
   * id — reuses the shared icon system in settings/branding.js so the mark
   * + brand color match the model picker exactly. Falls back to '' if the
   * helpers aren't loaded yet (info-rail loads after branding, so this is
   * just defensive). */
  function _brandLogo(modelId, size) {
    if (typeof _detectBrand === 'function' && typeof _brandSvg === 'function') {
      return _brandSvg(_detectBrand(modelId || ''), size || 15);
    }
    return '';
  }

  const _ICON_TOOLS = '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a4 4 0 0 1-5 5L4 17l3 3 5.7-5.7a4 4 0 0 1 5-5 4 4 0 0 0-3-6.6 4 4 0 0 0 0 3.6z"/></svg>';
  const _ICON_FOLDER = '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.7-.9L9.6 3.9A2 2 0 0 0 7.9 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2z"/></svg>';
  const _LOCK = '<svg class="tctx-lock" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>';

  /**
   * Render a captured snapshot into the per-turn note HTML.
   *
   * Design: a self-contained widget in the right gutter that has two
   * states driven purely by CSS `:hover` — no native tooltip:
   *   • COLLAPSED `.tctx-bar` — brand logo + model name + depth + small
   *     tool/workspace count badges. Always visible.
   *   • EXPANDED `.tctx-panel` — grows DOWN into the empty gutter on hover,
   *     listing the full model id, every enabled tool as a toned chip, and
   *     each workspace path. Because it lives in the gutter (a direct child
   *     of `.message`, not `.message-content`) it never covers chat content.
   *
   * @param {object|null} snap — output of buildTurnCtxSnapshot (or a
   *   persisted copy loaded from the DB).
   * @returns {string} HTML, or '' when there's nothing to show.
   */
  function renderTurnCtxNote(snap) {
    if (!snap || typeof snap !== 'object') return '';
    let tools = Array.isArray(snap.tools) ? snap.tools : [];
    const roots = Array.isArray(snap.roots) ? snap.roots : [];
    // Modes are a dedicated field on new snapshots. Legacy snapshots (sent
    // before modes were split out) embedded them inside `tools` as
    // tone:'mode' entries — recover those so old turns still show a badge.
    let modes = Array.isArray(snap.modes) ? snap.modes : [];
    if (!Array.isArray(snap.modes)) {
      modes = tools.filter((t) => t && t.tone === 'mode');
      tools = tools.filter((t) => !(t && t.tone === 'mode'));
    }
    const model = snap.model ? _modelLabel(snap.model) : '';
    if (!model && !tools.length && !modes.length && !roots.length) return '';

    const logo = model ? _brandLogo(snap.model, 15) : '';
    const depthChip = snap.depth ? '<span class="tctx-depth">' + _esc(snap.depth) + '</span>' : '';

    // ── Collapsed bar ──
    const bar = ['<div class="tctx-bar">'];
    if (logo) bar.push('<span class="tctx-logo">' + logo + '</span>');
    if (model) bar.push('<span class="tctx-model">' + _esc(model) + '</span>');
    if (depthChip) bar.push(depthChip);
    // Mode badge(s) — always visible so the run mode is legible at a glance.
    for (const md of modes) {
      bar.push('<span class="tctx-mode-badge">' + _esc(md.label) + '</span>');
    }
    if (tools.length) bar.push('<span class="tctx-count tctx-count-tools">' + _ICON_TOOLS + tools.length + '</span>');
    if (roots.length) bar.push('<span class="tctx-count tctx-count-ws">' + _ICON_FOLDER + roots.length + '</span>');
    bar.push('</div>');

    // ── Expand panel ──
    const rows = [];
    if (modes.length) {
      const mchips = modes.map((md) =>
        '<span class="tctx-chip tctx-tone-mode">' + _esc(md.label) + '</span>'
      ).join('');
      rows.push('<div class="tctx-row"><span class="tctx-row-h">Mode</span>' +
        '<div class="tctx-chips">' + mchips + '</div></div>');
    }
    if (model) {
      rows.push('<div class="tctx-row"><span class="tctx-row-h">Model</span>' +
        '<span class="tctx-row-v">' + (logo ? '<span class="tctx-logo">' + logo + '</span>' : '') +
        '<span class="tctx-row-model">' + _esc(snap.model) + '</span>' + depthChip + '</span></div>');
    }
    if (tools.length) {
      const chips = tools.map((tl) =>
        '<span class="tctx-chip tctx-tone-' + _esc(tl.tone || 'mode') + '">' + _esc(tl.label) + '</span>'
      ).join('');
      rows.push('<div class="tctx-row"><span class="tctx-row-h">Tools</span>' +
        '<div class="tctx-chips">' + chips + '</div></div>');
    }
    if (roots.length) {
      const paths = roots.map((r) =>
        '<div class="tctx-path">' + (r.ro ? _LOCK : '') + '<span>' + _esc(r.path || r.short) + '</span></div>'
      ).join('');
      rows.push('<div class="tctx-row"><span class="tctx-row-h">Workspace</span>' +
        '<div class="tctx-paths">' + paths + '</div></div>');
    }

    return '<div class="turn-ctx">' + bar.join('') +
           '<div class="tctx-panel">' + rows.join('') + '</div></div>';
  }

  /* ── Reconcile a captured snapshot against the tool-schema latch ──
   *
   * WHY: `buildTurnCtxSnapshot()` reads the LIVE toolbar toggles at send
   * time, but the per-conversation tool-schema latch (lib/tools/registry.py
   * ::latch_tool_list) freezes the tool array a conversation first used and
   * serves it byte-identical every round — so a mid-conversation toggle is
   * HELD BACK until a new conversation or an explicit "Apply now". The turn
   * therefore runs with the FROZEN set, not the toolbar state the snapshot
   * captured. The done event reports that divergence as
   * `toolsetDiff = {added, removed}` in tool FUNCTION-name space:
   *   • added   = toggled ON  but held back → did NOT run → drop from note
   *   • removed = toggled OFF but held back → still ran   → restore to note
   * This corrects the note in place so it shows the tools that ACTUALLY ran.
   *
   * The diff is in function-name space; the note is in feature-label space,
   * so we map families. Unknown names fall back to a chip carrying the raw
   * function name, so nothing is ever silently dropped. */
  const _CTX_FAMILY_RULES = [
    { names: ['web_search'], label: 'Search', tone: 'search', kind: 'tool' },
    { names: ['fetch_url'], label: 'Fetch', tone: 'net', kind: 'tool' },
    { prefix: 'browser_', label: 'Browser', tone: 'net', kind: 'tool' },
    { prefix: 'desktop_', label: 'Desktop', tone: 'net', kind: 'tool' },
    { names: ['generate_image'], label: 'Image Gen', tone: 'ai', kind: 'tool' },
    { names: ['ask_human'], label: 'Ask User', tone: 'ai', kind: 'tool' },
    { names: ['create_memory', 'update_memory', 'delete_memory', 'merge_memories', 'search_memories'],
      label: 'Memory', tone: 'ai', kind: 'tool' },
    { names: ['schedule_create', 'schedule_list', 'schedule_manage', 'await_task', 'timer_create', 'timer_manage'],
      label: 'Scheduler', tone: 'ai', kind: 'tool' },
    { names: ['spawn_agents', 'await_agents', 'get_agent_result'],
      label: 'Swarm', tone: 'mode', kind: 'mode' },
    // MCP tools are namespaced `mcp__<server>__<tool>`. Map each to a
    // per-server chip so a latch diff that adds/removes an MCP tool
    // reconciles to the same "MCP: <server>" chip _collectTools emits.
    { prefix: 'mcp__', label: '', tone: 'mcp', kind: 'tool', mcp: true },
  ];

  /* Pretty server name out of a namespaced `mcp__<server>__<tool>` fn. */
  function _mcpServerOf(fnName) {
    const m = /^mcp__([^_]+(?:_[^_]+)*?)__/.exec(String(fnName));
    return m ? m[1] : '';
  }

  function _ctxFamilyFor(fnName) {
    for (const rule of _CTX_FAMILY_RULES) {
      if (rule.names && rule.names.indexOf(fnName) !== -1) return rule;
      if (rule.prefix && String(fnName).indexOf(rule.prefix) === 0) {
        if (rule.mcp) {
          const srv = _mcpServerOf(fnName);
          return { label: srv ? 'MCP: ' + srv : 'MCP', tone: 'mcp', kind: 'tool' };
        }
        return rule;
      }
    }
    // Unknown tool — surface the raw function name rather than lose it.
    return { label: fnName, tone: 'mode', kind: 'tool' };
  }

  /**
   * Correct `snap` to the tools that actually ran, given a latch diff.
   * Mutates `snap.tools` / `snap.modes` in place. Returns true if changed.
   *
   * @param {object} snap — a captured/persisted turn-ctx snapshot (msg._ctx).
   * @param {object} diff — {added:[fnName...], removed:[fnName...]}.
   */
  function reconcileTurnCtxCapsule(snap, diff) {
    if (!snap || typeof snap !== 'object' || !diff || typeof diff !== 'object') return false;
    const added = Array.isArray(diff.added) ? diff.added : [];
    const removed = Array.isArray(diff.removed) ? diff.removed : [];
    if (!added.length && !removed.length) return false;

    // Family descriptors, deduped by label (a family spans several fn names).
    const addedFams = new Map();
    for (const n of added) { const f = _ctxFamilyFor(n); addedFams.set(f.label, f); }
    const removedFams = new Map();
    for (const n of removed) { const f = _ctxFamilyFor(n); removedFams.set(f.label, f); }

    let tools = Array.isArray(snap.tools) ? snap.tools.slice() : [];
    let modes = Array.isArray(snap.modes) ? snap.modes.slice() : [];
    let changed = false;

    const _matches = (entry, fam) =>
      fam.tone === 'search' ? (entry.tone === 'search') : (entry.label === fam.label);

    // Held back ON-toggles never ran → remove them from the note.
    for (const fam of addedFams.values()) {
      if (fam.kind === 'mode') {
        const before = modes.length;
        modes = modes.filter((m) => !_matches(m, fam));
        if (modes.length !== before) changed = true;
      } else {
        const before = tools.length;
        tools = tools.filter((tl) => !_matches(tl, fam));
        if (tools.length !== before) changed = true;
      }
    }
    // Held back OFF-toggles still ran → restore them to the note.
    for (const fam of removedFams.values()) {
      if (fam.kind === 'mode') {
        if (!modes.some((m) => _matches(m, fam))) {
          modes.push({ label: fam.label, tone: 'mode' });
          changed = true;
        }
      } else if (!tools.some((tl) => _matches(tl, fam))) {
        tools.push({ label: fam.label, tone: fam.tone });
        changed = true;
      }
    }

    if (changed) {
      snap.tools = tools;
      snap.modes = modes;
    }
    return changed;
  }

  window.buildTurnCtxSnapshot = buildTurnCtxSnapshot;
  window.renderTurnCtxNote = renderTurnCtxNote;
  window.reconcileTurnCtxCapsule = reconcileTurnCtxCapsule;
  window.refreshMcpRailState = refreshMcpRailState;

  /* Refresh the MCP rail state once at boot so the FIRST turn of a session
   * already reflects connected servers (not just after the settings panel
   * is opened). Best-effort; failures are swallowed inside the function. */
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => { refreshMcpRailState(); }, { once: true });
    } else {
      refreshMcpRailState();
    }
  }
})();

/* ═══════════════════════════════════════════════════════════════════
   info-rail.js — Per-turn context note (a fact card, not a UI snapshot)

   Each USER turn carries a small note in the right gutter describing what
   the turn ACTUALLY ran with. Two-phase lifecycle:

     • SEND (client) — `buildTurnCtxSnapshot()` captures the LIVE toolbar
       state (workspace roots, tools, modes, model, depth) and freezes it
       onto `userMsg._ctx`. This is a best-effort SNAPSHOT and may drift
       from truth for up to a few seconds — the user can pause in the
       composer and switch preset / toggle a mode between send and the
       stream actually starting.
     • DONE (server-authoritative) — the DONE SSE frame ships the FACT
       card (`actualModel` / `actualDepth` / `actualModes` +
       `toolsetDiff`). `reconcileTurnCtxCapsule()` OVERWRITES the snapshot
       fields with those facts, so the note settles as the truth: which
       model actually answered (after any dispatcher fallback), which
       thinking depth actually applied, which run-mode set was live
       server-side, and which tools actually ran (after the schema latch
       held / restored toggles).

   Between SEND and DONE the note may briefly display the send-time
   snapshot — that is the honest "fact-not-yet-in" state, not a bug.

   ── Public API ──
     window.buildTurnCtxSnapshot()               — capture current context (or null).
     window.renderTurnCtxNote(snapshot)          — snapshot → { fold, rail } HTML
        strings (two surfaces with DIFFERENT DOM homes — see renderTurnCtxNote).
     window.reconcileTurnCtxCapsule(snap, fact)  — overwrite snap in place
        with facts from the done event; `fact` fields are ALL optional:
          { added?, removed?, toolsetDiff?, actualModel?, actualDepth?,
            actualModes? } — mixes tool/schema-latch reconciliation with
          the model/depth/mode fact card. Returns true when anything
          changed. Called from sse_pipeline.js on every done frame.
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
    // Scheduler is a default tool (always on, no toggle) — like read_files /
    // todo it is intentionally NOT shown as a per-turn chip (would be constant
    // noise). The reconcile rule below still maps its fn names defensively.
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

  const _LOCK = '<svg class="tctx-lock" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>';

  /** Max tool chips rendered up-front; the rest collapse behind a "+N"
   * CLICK toggle. This cap — together with `_MAX_VISIBLE_PATHS` — decides
   * what a card shows up-front so a TYPICAL card renders complete (the old
   * 132px `overflow:hidden` guillotine clipped ordinary cards mid-WORKSPACE
   * with no way to reach the facts). Genuinely fat cards are then bounded
   * by the scrollable pixel backstop in styles.css. WHY a bound at all:
   * the rail is permanent furniture in a grid track, so its height adds to
   * the turn's height — and `_collectTools()` emits one chip per connected
   * MCP server, which can be dozens. 9 keeps a typical setup (a few feature
   * toggles + a few MCP servers) fully visible while the geometry guard's
   * 10-tool probe stays gated. Expansion is a click (not hover) because a
   * hover overlay would have to escape `.message`'s content-visibility
   * paint containment — exactly the carve-out this redesign deleted. */
  const _MAX_VISIBLE_CHIPS = 9;

  /** Max workspace paths rendered up-front; extra roots collapse behind
   * the same "+N" toggle as chips (the delegated click handler only asks
   * for a `.tctx-overflow` sibling, so the mechanism is shared verbatim).
   * Roots were previously unbounded — the same defect family as chips:
   * rail height driven by how many roots the workspace happens to have. */
  const _MAX_VISIBLE_PATHS = 3;

  /**
   * Render a captured snapshot into the per-turn context surfaces.
   *
   * Two surfaces, both in normal flow — there is NO hover overlay. They have
   * DIFFERENT DOM homes, which is why this returns them separately:
   *   • `rail` (`.turn-ctx`) — lives in the third grid track that
   *     `.chat-inner` owns, a DIRECT CHILD of `.message`, so it can never
   *     overflow the viewport. Shows model + depth + mode badges + tool
   *     chips + workspace paths permanently. Visible only when the container
   *     query grants the track.
   *   • `fold` (`.tctx-fold`) — the same facts compressed to ONE line,
   *     spliced INSIDE `.message-content` between the header and the body
   *     (in-flow — its `margin-bottom` is meaningless anywhere else). Shown
   *     exactly when the rail track is absent (narrow pane, or the
   *     request-inspector drawer is open and the pane has as little as 74px
   *     to give). The context is compacted, never lost.
   *
   * ⚠️ The two used to ship as ONE concatenated string spliced as a direct
   * `.message` child: below the rail threshold the fold auto-placed into the
   * ZERO-WIDTH rail track and rendered at width 0 — `display:flex`, correct
   * text, invisible element (the 2026-08-03 "context completely disappears
   * at 100% zoom" report). Keep the surfaces separate.
   *
   * @param {object|null} snap — output of buildTurnCtxSnapshot (or a
   *   persisted copy loaded from the DB).
   * @returns {{fold: string, rail: string}|string} the two HTML fragments,
   *   or '' when there's nothing to show.
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

    // ── Rail head: brand + model + depth + mode badges ──
    const head = ['<div class="tctx-head">'];
    if (logo) head.push('<span class="tctx-logo">' + logo + '</span>');
    if (model) head.push('<span class="tctx-model">' + _esc(model) + '</span>');
    if (depthChip) head.push(depthChip);
    for (const md of modes) {
      head.push('<span class="tctx-mode-badge">' + _esc(md.label) + '</span>');
    }
    head.push('</div>');

    const rows = [];
    if (tools.length) {
      const _chip = (tl) =>
        '<span class="tctx-chip tctx-tone-' + _esc(tl.tone || 'mode') + '">' +
        _esc(tl.label) + '</span>';
      const shown = tools.slice(0, _MAX_VISIBLE_CHIPS).map(_chip).join('');
      const rest = tools.slice(_MAX_VISIBLE_CHIPS);
      let overflow = '';
      if (rest.length) {
        overflow =
          '<span class="tctx-overflow" hidden>' + rest.map(_chip).join('') + '</span>' +
          '<button type="button" class="tctx-more" data-tctx-more="1"' +
          ' aria-expanded="false">+' + rest.length + '</button>';
      }
      rows.push('<div class="tctx-row"><span class="tctx-row-h">Tools</span>' +
        '<div class="tctx-chips">' + shown + overflow + '</div></div>');
    }
    if (roots.length) {
      // SHORT path (last two segments) with the full path on hover: a full
      // absolute path is ~90 chars and `word-break:break-all` would stack it
      // four lines high inside a 232px rail, inflating every turn that has a
      // workspace. The rail is a glance surface; the full path stays reachable.
      const _path = (r) =>
        '<div class="tctx-path" title="' + _esc(r.path || r.short) + '">' +
        (r.ro ? _LOCK : '') + '<span>' + _esc(r.short || r.path) + '</span></div>';
      const shownPaths = roots.slice(0, _MAX_VISIBLE_PATHS).map(_path).join('');
      const restPaths = roots.slice(_MAX_VISIBLE_PATHS);
      let pathOverflow = '';
      if (restPaths.length) {
        pathOverflow =
          '<div class="tctx-overflow" hidden>' + restPaths.map(_path).join('') + '</div>' +
          '<button type="button" class="tctx-more" data-tctx-more="1"' +
          ' aria-expanded="false">+' + restPaths.length + '</button>';
      }
      rows.push('<div class="tctx-row"><span class="tctx-row-h">Workspace</span>' +
        '<div class="tctx-paths">' + shownPaths + pathOverflow + '</div></div>');
    }

    // ── Fold line for panes with no rail track ──
    const foldBits = [];
    if (model) foldBits.push(model);
    if (snap.depth) foldBits.push(snap.depth);
    for (const md of modes) foldBits.push(md.label);
    if (tools.length) foldBits.push(tools.length + ' tools');
    if (roots.length) foldBits.push(roots.length + ' ws');
    const fold = '<div class="tctx-fold"><span class="tctx-fold-dot"></span>' +
      '<span>' + _esc(foldBits.join(' · ')) + '</span></div>';

    return {
      fold: fold,
      rail: '<div class="turn-ctx">' + head.join('') + rows.join('') + '</div>',
    };
  }

  /* "+N" toggle + rail click guard. Delegated at document level so it
   * survives every re-render of the message list without per-node listener
   * bookkeeping. Expanding changes the rail's height IN FLOW — no overlay,
   * no paint containment to escape. The rail is hit-testable (its backstop
   * scrolls, paths have hover titles), but a click ANYWHERE inside it must
   * never be seen by a message-level delegated handler — so every rail
   * click is swallowed here, toggle or not. */
  function _onTctxClick(ev) {
    const rail = (ev.target && ev.target.closest)
      ? ev.target.closest('.turn-ctx') : null;
    if (!rail) return;
    const btn = ev.target.closest('[data-tctx-more]');
    if (btn) {
      const chips = btn.parentNode;
      const hidden = chips && chips.querySelector('.tctx-overflow');
      if (hidden) {
        const opening = hidden.hasAttribute('hidden');
        if (opening) hidden.removeAttribute('hidden');
        else hidden.setAttribute('hidden', '');
        btn.setAttribute('aria-expanded', opening ? 'true' : 'false');
        btn.textContent = opening ? '−' : '+' + hidden.children.length;
      }
    }
    ev.stopPropagation();
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

  /* Fact-card fields on the done frame the reconcile MAY overwrite. Kept as
   * a named list so a test / audit can enumerate what "settles" at done. */
  const _CTX_FACT_FIELDS = ['actualModel', 'actualDepth', 'actualModes'];

  /**
   * Reconcile a captured/persisted turn-ctx snapshot against the done
   * frame's authoritative fact card. Mutates `snap` in place; returns true
   * when anything changed. Two independent reconciliations happen here:
   *
   *   1. TOOL-SCHEMA LATCH DIFF — `fact.added` / `fact.removed` (or a
   *      nested `fact.toolsetDiff`) in tool FUNCTION-name space:
   *        • added   = toggled ON  but held back → did NOT run → drop
   *        • removed = toggled OFF but held back → still ran   → restore
   *      Maps function names to feature-label chips via `_CTX_FAMILY_RULES`;
   *      unknown names surface the raw name rather than get lost.
   *
   *   2. FACT-CARD OVERWRITE — `fact.actualModel` / `fact.actualDepth` /
   *      `fact.actualModes` are the server-authoritative record of what
   *      the turn actually ran with. When present they OVERWRITE
   *      `snap.model` / `snap.depth` / `snap.modes` verbatim so the note
   *      settles as the truth after a dispatcher fallback or a preset
   *      switched in the pause between send and stream-start. See the
   *      file-header contract at the top for the send-vs-done lifecycle.
   *
   * @param {object} snap — a captured/persisted turn-ctx snapshot (msg._ctx).
   * @param {object} fact — done-event projection; ALL fields optional:
   *   { added?, removed?, toolsetDiff?, actualModel?, actualDepth?, actualModes? }
   */
  function reconcileTurnCtxCapsule(snap, fact) {
    if (!snap || typeof snap !== 'object' || !fact || typeof fact !== 'object') return false;
    // Accept the diff either flattened onto `fact` OR nested under
    // `fact.toolsetDiff` (call site convenience — sse_pipeline used to pass
    // the diff object bare).
    const diffSrc = (fact.toolsetDiff && typeof fact.toolsetDiff === 'object')
      ? fact.toolsetDiff : fact;
    const added = Array.isArray(diffSrc.added) ? diffSrc.added : [];
    const removed = Array.isArray(diffSrc.removed) ? diffSrc.removed : [];

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

    // ── Fact-card overwrite (independent of the latch diff) ──
    if (typeof fact.actualModel === 'string' && fact.actualModel
        && snap.model !== fact.actualModel) {
      snap.model = fact.actualModel;
      changed = true;
    }
    if (Object.prototype.hasOwnProperty.call(fact, 'actualDepth')) {
      const _newDepth = typeof fact.actualDepth === 'string' ? fact.actualDepth : '';
      if ((snap.depth || '') !== _newDepth) {
        snap.depth = _newDepth;
        changed = true;
      }
    }
    if (Array.isArray(fact.actualModes)) {
      // Full replacement so a mode that was live at send-time but not
      // actually on the run (or vice versa) reflects truth. Preserve the
      // {label, tone:'mode'} shape the renderer expects.
      const _newModes = fact.actualModes
        .filter((m) => m && typeof m.label === 'string' && m.label)
        .map((m) => ({ label: m.label, tone: 'mode' }));
      const _sameLen = _newModes.length === modes.length;
      const _sameLabels = _sameLen && _newModes.every(
        (m, i) => modes[i] && modes[i].label === m.label);
      if (!_sameLabels) {
        snap.modes = _newModes;
        changed = true;
      }
    }

    return changed;
  }
  reconcileTurnCtxCapsule._FACT_FIELDS = _CTX_FACT_FIELDS;

  window.buildTurnCtxSnapshot = buildTurnCtxSnapshot;
  window.renderTurnCtxNote = renderTurnCtxNote;
  window.reconcileTurnCtxCapsule = reconcileTurnCtxCapsule;
  window.refreshMcpRailState = refreshMcpRailState;

  /* Refresh the MCP rail state once at boot so the FIRST turn of a session
   * already reflects connected servers (not just after the settings panel
   * is opened). Best-effort; failures are swallowed inside the function. */
  if (typeof document !== 'undefined') {
    document.addEventListener('click', _onTctxClick);
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => { refreshMcpRailState(); }, { once: true });
    } else {
      refreshMcpRailState();
    }
  }
})();

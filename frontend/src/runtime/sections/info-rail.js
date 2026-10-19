/* ===== migrated source: info-rail.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   info-rail.js — Per-turn context note (a fact card, not a UI snapshot)

   Each USER turn carries a small note in the right gutter describing what
   the turn ACTUALLY ran with. Two-phase lifecycle:

     • SEND (client) — `buildTurnCtxSnapshot()` captures the LIVE toolbar
       state (workspace roots, tools, modes, model, depth, git branch) and
       freezes it onto `userMsg._ctx`. This is a best-effort SNAPSHOT and
       may drift from truth for up to a few seconds — the user can pause in
       the composer and switch preset / toggle a mode between send and the
       stream actually starting.
     • DONE (server-authoritative) — the DONE SSE frame ships the FACT
       card (`actualModel` / `actualDepth` / `actualModes`).
       `reconcileTurnCtxCapsule()` OVERWRITES the snapshot
       fields with those facts, so the note settles as the truth: which
       model actually answered (after any dispatcher fallback), which
       thinking depth actually applied, and which run-mode set was live
       server-side.

   Between SEND and DONE the note may briefly display the send-time
   snapshot — that is the honest "fact-not-yet-in" state, not a bug.

   ── Live state (branch + checklist) ──
   Two rows are deliberately LIVE, not frozen: the workspace git branch and
   the running turn's checklist. A turn is long-lived — the agent can
   `git checkout` mid-run, and every `todo_write` rewrites the plan — so a
   send-time snapshot would lie for most of the turn. The rail therefore
   re-renders those rows from the CURRENT live state on every
   branch/checklist change, and re-reconciles the persisted snapshot for
   the ACTIVE turn so a mid-turn reload still paints the latest state.
   The frozen send-time values stay on the snapshot (`snap.git` as
   captured at send) for history; the live patch path overwrites them.

   ── Public API ──
     runtimeScope.buildTurnCtxSnapshot()               — capture current context (or null).
     runtimeScope.renderTurnCtxNote(snapshot)          — snapshot → { fold, rail } HTML
        strings (two surfaces with DIFFERENT DOM homes — see renderTurnCtxNote).
     runtimeScope.reconcileTurnCtxCapsule(snap, fact)  — overwrite snap in place
        with facts from the done event; `fact` fields are ALL optional:
          { actualModel?, actualDepth?, actualModes? }. Returns true when
          anything changed. Called from sse_pipeline.js on every done frame.
     runtimeScope.applyProjectGitState(projectData)    — ingest a project
        status payload ({gitBranch, gitBranches}) into the live branch
        state; re-renders live rails + notifies on a real switch.
     runtimeScope.updateTurnCtxTodos(todos, opts)      — ingest a checklist
        state (todo_write round / done todoState); re-renders live rails.
     runtimeScope.activateTurnCtxConversation(convId)   — bind the live rows
        (git + checklist) to a newly-active conversation and CLEAR any
        previous chat's live state, so a running turn's rows never leak
        across conversations. Called on conversation open / new-chat.
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
   * servers (lib/mcp). The already-required server-config response carries
   * a compact server/count projection for first paint; Settings carries the
   * same projection on catalog and per-tool mutation responses. The rail
   * therefore owns no request, timer, or schema-sized cache. Shape:
   *   { servers: [{name, count}], total: <enabledToolCount> }. */
  const _MCP_RAIL_MAX_SERVERS = 64;
  const _MCP_RAIL_MAX_NAME_CHARS = 120;
  const _MCP_RAIL_MAX_TOOL_COUNT = 100000;
  let _mcpRail = { servers: [], total: 0 };

  function applyMcpToolSummary(summary) {
    const rows = summary && Array.isArray(summary.servers) ? summary.servers : [];
    const counts = new Map();
    for (const row of rows) {
      if (counts.size >= _MCP_RAIL_MAX_SERVERS) break;
      const name = typeof row?.name === 'string'
        ? row.name.trim().slice(0, _MCP_RAIL_MAX_NAME_CHARS) : '';
      const numericCount = Number(row?.count);
      const count = Number.isFinite(numericCount)
        ? Math.min(_MCP_RAIL_MAX_TOOL_COUNT, Math.max(0, Math.floor(numericCount))) : 0;
      if (!name || count < 1 || counts.has(name)) continue;
      counts.set(name, count);
    }
    const servers = Array.from(counts, ([name, count]) => ({ name, count }))
      .sort((left, right) => left.name.localeCompare(right.name));
    _mcpRail = {
      servers,
      total: servers.reduce((sum, row) => sum + row.count, 0),
    };
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
   * Modes (Autopilot / Swarm / a named flow) are distinct
   * from tools: they change HOW the turn runs, not which capability it
   * can reach. They get their own always-visible badge on the collapsed
   * bar. A flow supersedes the Autopilot toggle. Returns
   * [{label, tone:'mode'}]. */
  function _collectModes() {
    const out = [];
    const flow = (typeof activeFlow !== 'undefined') ? activeFlow : '';
    if (flow) {
      const name = (typeof _flowDisplayName === 'function') ? _flowDisplayName(flow) : 'Flow';
      out.push({ label: name, tone: 'mode' });
    } else {
      if (typeof autopilotEnabled !== 'undefined' && autopilotEnabled) out.push({ label: 'Autopilot', tone: 'mode' });
    }
    return out;
  }

  /* ── Collect the git branch state ────────────────────────────────
   * projectState.gitBranch / gitBranches are kept live by
   * applyProjectGitState (fed by every project status payload). The
   * snapshot records the primary branch + each root's branch so a
   * multi-root turn can label chips; a single-root turn renders one chip.
   * Returns { branch, branches: [{path, short, branch}] } or null. */
  function _collectGit() {
    const ps = (typeof projectState !== 'undefined') ? projectState : null;
    if (!ps) return null;
    const branches = [];
    const seen = new Set();
    const rows = Array.isArray(ps.gitBranches) ? ps.gitBranches : [];
    for (const row of rows) {
      const path = typeof row?.path === 'string' ? row.path : '';
      const branch = typeof row?.branch === 'string' ? row.branch.trim() : '';
      if (!path || !branch || seen.has(branch + '@' + path)) continue;
      seen.add(branch + '@' + path);
      branches.push({ path, short: _short(path), branch: branch.slice(0, 80) });
    }
    let branch = typeof ps.gitBranch === 'string' ? ps.gitBranch.trim() : '';
    if (!branch && branches.length) branch = branches[0].branch;
    if (!branch) return null;
    /* Collapse to the single-branch shape when every root agrees — the
     * common case — so the rail shows ONE chip, not N duplicates. */
    const distinct = new Set(branches.map((b) => b.branch));
    if (distinct.size <= 1) {
      return { branch, branches: [{ path: branches[0]?.path || ps.path || '', short: _short(branches[0]?.path || ps.path || ''), branch }] };
    }
    return { branch, branches };
  }

  function _resolveModel() {
    if (typeof config !== 'undefined' && config && config.model) return config.model;
    if (typeof serverModel !== 'undefined' && serverModel) return serverModel;
    return '';
  }
  /* 'default' is the slider's follow-the-model intent token, not an effort
   * rung. The note is a FACT card, so it must name the rung the turn actually
   * targets — not the raw token. Resolve it through the same declaration the
   * composer slider paints (the model's declared default_effort, exposed as
   * DepthOptions.defaultDepth), falling back to the user's configured default
   * depth. Any other token (off/low/medium/high/xhigh/max/enabled/native
   * effort names) is already an actual value and passes through untouched.
   * The wire intent (config.thinkingDepth) is never rewritten — this only
   * affects the human-readable snapshot. */
  function _displayDepth(depthRaw, fallback) {
    if (depthRaw !== 'default') return depthRaw;
    const opts = (typeof _composerReasoningOptions === 'function')
      ? _composerReasoningOptions() : undefined;
    const declared = opts && typeof opts.defaultDepth === 'string' ? opts.defaultDepth : '';
    return declared
      || (typeof config !== 'undefined' && config && config.defaultThinkingDepth)
      || fallback
      || '';
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
      depth: (_isThink && depthRaw) ? _displayDepth(depthRaw) : '',
      git: _collectGit(),
      /* The live checklist is attached by the patch path, not here: at
       * send time there is no list yet (the model writes it DURING the
       * turn), so a send-time snapshot of `_liveTodos` would be empty
       * noise. History turns read their final list back off the
       * persisted snapshot the patch path froze. */
    };
    if (!snap.roots.length && !snap.tools.length && !snap.modes.length
        && !snap.model && !snap.git) return null;
    return snap;
  }

  function _modelLabel(modelId) {
    if (!modelId) return '';
    return (typeof _modelShortName === 'function') ? _modelShortName(modelId) : modelId;
  }

  /* Real provider brand logo (Anthropic / OpenAI / Gemini / …) for a model
   * id — reuses the typed model-brand icon owner so the mark + color match
   * the model picker. Falls back to '' for isolated alternate entries. */
  function _brandLogo(modelId, size) {
    if (typeof _brandSvg !== 'function') return '';
    if (typeof _modelBrand === 'function') {
      return _brandSvg(_modelBrand(modelId || ''), size || 15);
    }
    if (typeof _detectBrand === 'function') {
      return _brandSvg(_detectBrand(modelId || ''), size || 15);
    }
    return '';
  }

  const _LOCK = '<svg class="tctx-lock" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>';

  /* Branch glyph — the classic git-fork mark, stroke-drawn so it inherits
   * the chip's tone color via currentColor. Same per-child fill="none"
   * discipline as every stroked glyph in the rail (the .stg-brand-icon CSS
   * fill override does not reach here, but keeping the attribute costs
   * nothing and matches the file's other inline icons). */
  const _BRANCH = '<svg class="tctx-branch-icon" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="5" r="2.4"/><circle cx="6" cy="19" r="2.4"/><circle cx="18" cy="7" r="2.4"/><path d="M6 7.4v9.2"/><path d="M18 9.4c0 4.6-7.2 3.4-10 7"/></svg>';

  /* Checklist status glyphs, one per todo_write status. Reused by both the
   * rail row and the fold's compact counter. */
  const _TODO_MARK = {
    completed: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>',
    in_progress: '<svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="4.2"/></svg>',
    blocked: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><circle cx="12" cy="12" r="8.5"/><path d="M6.5 6.5l11 11"/></svg>',
    pending: '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><circle cx="12" cy="12" r="8.5"/></svg>',
  };

  /** Max tool chips rendered up-front; the rest collapse behind a "+N"
   * CLICK toggle. This cap — together with `_MAX_VISIBLE_PATHS` — decides
   * what a card shows up-front so a TYPICAL card renders complete without
   * scrolling (the old 132px `overflow:hidden` guillotine clipped ordinary
   * cards mid-WORKSPACE with no way to reach the facts). WHY a bound at
   * all: `_collectTools()` emits one chip per connected MCP server, which
   * can be dozens. The rail's box is bounded by the message's own height
   * (styles.css `.turn-ctx` is out of flow, `max-height:100%`) and
   * SCROLLS, so the cap is about glanceability, not geometry — 4 gives a
   * useful preview without turning a large MCP catalog into a wall of badges.
   * The count and stationary disclosure expose all remaining entries.
   * Expansion is a click (not hover) because a hover overlay would have
   * to escape `.message`'s content-visibility paint containment — exactly
   * the carve-out this redesign deleted. */
  const _MAX_VISIBLE_CHIPS = 4;

  /** Max workspace paths rendered up-front; extra roots collapse behind
   * the same "+N" toggle as chips (the delegated click handler only asks
   * for a `.tctx-overflow` sibling, so the mechanism is shared verbatim).
   * Roots were previously unbounded — the same defect family as chips:
   * the rail's scroll length driven by how many roots the workspace
   * happens to have. */
  const _MAX_VISIBLE_PATHS = 3;

  /**
   * Render a captured snapshot into the per-turn context surfaces.
   *
   * Two surfaces, both in normal flow — there is NO hover overlay. They have
   * DIFFERENT DOM homes, which is why this returns them separately:
   *   • `rail` (`.turn-ctx`) — a DIRECT CHILD of `.message`, absolutely
   *     positioned into the third track the pane owns (`.chat-inner`), so
   *     it can never overflow the viewport AND — being out of flow —
   *     never inflates the turn's height; its box starts at the message
   *     top and the sticky follower (below) glides it down the track as
   *     the conversation scrolls, until the next railed turn arrives.
   *     Shows model + depth + mode badges + tool chips + workspace paths
   *     permanently. Visible only when the container query grants the
   *     track.
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
  /* Render the checklist as ONE integrated unit inside the sticky rail.
   *
   * Two fixed facts shape this renderer:
   *   • The checklist must travel WITH `.turn-ctx`/`.tctx-stuck` — never as
   *     a detached `.tctx-overflow` block outside the rail. The old design
   *     emitted the full list as a full-width overflow sibling, so the list
   *     painted in a different visual box than the rail that owns it (and,
   *     being built twice — here and in classic-conversation-renderers.ts —
   *     the same plan/branch could paint twice). So the list now lives INSIDE
   *     `.tctx-todos` as plain content: one rail, one unit, one follower.
   *   • The in-progress item is the single most useful line, so it is ALWAYS
   *     rendered — never hidden in the count chip's hover title. The old code
   *     picked it as `current`, removed it from the list, and surfaced it only
   *     on hover, which is why an expanded checklist appeared to be missing
   *     its in-flight row. `current` is now rendered as a dedicated
   *     `.tctx-todo-current` line and STAYS in the list below.
   *
   * Layout: a count chip (glyph + done/total) sits in the rail's chip flow;
   * the in-flight item renders inline right after it; the full per-item list
   * collapses behind the same "+N" toggle the tools/workspace rows use — but
   * as an in-flow column under the chip, inside the rail. Shared by the
   * snapshot renderer and the live patch path so both stay byte-identical. */
  function _buildTodoListHtml(todos) {
    const MAX = 24;
    const norm = [];
    let doneN = 0;
    for (const item of todos) {
      if (!item || typeof item !== 'object') continue;
      if (norm.length >= MAX) break;
      const st = (item.status === 'completed' || item.status === 'in_progress'
        || item.status === 'blocked') ? item.status : 'pending';
      const text = String(item.content || '').trim().slice(0, 240);
      if (!text) continue;
      if (st === 'completed') doneN++;
      norm.push({ st, text });
    }
    const total = norm.length;
    if (!total) return '';
    /* The in-flight line the rail surfaces inline: the in_progress item, else
     * the first not-yet-done item, else the last completed one. It is rendered
     * as its own visible line AND stays in `norm`, so the full list below is
     * complete — no row lives only in a hover tooltip. */
    let current = null;
    for (const it of norm) { if (it.st === 'in_progress') { current = it; break; } }
    if (!current) for (const it of norm) { if (it.st !== 'completed') { current = it; break; } }
    if (!current) current = norm[norm.length - 1];
    const rest = norm.filter((it) => it !== current);

    const allDone = doneN === total;
    const glyph = _TODO_MARK[allDone ? 'completed'
      : (current.st === 'in_progress' ? 'in_progress'
      : (current.st === 'blocked' ? 'blocked' : 'pending'))];
    /* ONE chip in the rail's chip flow: count + status glyph. The in-flight
     * item is rendered as a visible line right after it (not buried in the
     * chip's hover title), so the plan's live edge is always on screen. */
    const chip =
      '<span class="tctx-chip tctx-tone-todo tctx-todo-count' +
      (allDone ? ' is-done' : '') + '" title="' +
      _esc(t('turnCtx.todoLabel')) + ' ' + doneN + '/' + total + '">' + glyph +
      '<span>' + doneN + '/' + total + '</span></span>';

    /* The always-visible in-flight line, inline after the chip. This is the
     * line the old design hid in a tooltip — the "missing in-progress row". */
    const currentLine =
      '<span class="tctx-todo-item tctx-todo-' + current.st + ' tctx-todo-current"' +
      ' title="' + _esc(current.text) + '">' +
      '<span class="tctx-todo-mark">' + (_TODO_MARK[current.st] || _TODO_MARK.pending) + '</span>' +
      '<span class="tctx-todo-text">' + _esc(current.text) + '</span></span>';

    /* Full list rendered inline inside the rail (no "+N" collapse). */
    let list = '';
    if (rest.length) {
      const items = rest.map((it) =>
        '<div class="tctx-todo-item tctx-todo-' + it.st + '">' +
        '<span class="tctx-todo-mark">' + (_TODO_MARK[it.st] || _TODO_MARK.pending) + '</span>' +
        '<span class="tctx-todo-text" title="' + _esc(it.text) + '">' + _esc(it.text) + '</span></div>').join('');
      list = '<span class="tctx-todo-list">' + items + '</span>';
    }
    return '<div class="tctx-todos">' + chip + currentLine + list + '</div>';
  }

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
    const git = (snap.git && typeof snap.git === 'object') ? snap.git : null;
    const todos = Array.isArray(snap.todos) ? snap.todos : [];
    if (!model && !tools.length && !modes.length && !roots.length
        && !git && !todos.length) return '';

    const logo = model ? _brandLogo(snap.model, 15) : '';
    const depthChip = snap.depth ? '<span class="tctx-depth">' + _esc(snap.depth) + '</span>' : '';

    // ── Rail head: brand + model + depth + mode badges ──
    const head = ['<div class="tctx-head">'];
    if (logo) head.push('<span class="tctx-logo">' + logo + '</span>');
    if (model || depthChip) {
      head.push('<span class="tctx-model-group">');
      if (model) head.push('<span class="tctx-model">' + _esc(model) + '</span>');
      if (depthChip) head.push(depthChip);
      head.push('</span>');
    }
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
      let toggle = '';
      if (rest.length) {
        overflow = '<span class="tctx-overflow" hidden>' + rest.map(_chip).join('') + '</span>';
        toggle = '<button type="button" class="tctx-more" data-tctx-more="1"' +
          ' aria-expanded="false">' + _esc(t('turnCtx.showMore', {count: rest.length})) + '</button>';
      }
      rows.push('<div class="tctx-row"><div class="tctx-row-heading"><span class="tctx-row-h">' +
        _esc(t('turnCtx.toolsLabel')) + '<span class="tctx-row-count">' + tools.length + '</span></span>' + toggle + '</div>' +
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
      let pathToggle = '';
      if (restPaths.length) {
        pathOverflow = '<div class="tctx-overflow" hidden>' + restPaths.map(_path).join('') + '</div>';
        pathToggle = '<button type="button" class="tctx-more" data-tctx-more="1"' +
          ' aria-expanded="false">' + _esc(t('turnCtx.showMore', {count: restPaths.length})) + '</button>';
      }
      rows.push('<div class="tctx-row"><div class="tctx-row-heading"><span class="tctx-row-h">' +
        _esc(t('turnCtx.workspaceLabel')) + '<span class="tctx-row-count">' + roots.length + '</span></span>' + pathToggle + '</div>' +
        '<div class="tctx-paths">' + shownPaths + pathOverflow + '</div></div>');
    }
    /* ── Git branch + checklist rows ── LIVE rows (rebuilt in place by
     * _patchLiveCtxRails on a branch switch / todo_write). The snapshot
     * path renders the same markup off the frozen values so a settled
     * turn keeps its final record. */
    const gitRowHtml = _renderGitRowHtml(git);
    if (gitRowHtml) rows.push(gitRowHtml);
    const todoRowHtml = _renderTodoRowHtml(todos);
    if (todoRowHtml) rows.push(todoRowHtml);

    // ── Fold line for panes with no rail track ──
    const foldBits = [];
    if (model) foldBits.push(model);
    if (snap.depth) foldBits.push(snap.depth);
    for (const md of modes) foldBits.push(md.label);
    if (tools.length) foldBits.push(t('turnCtx.toolCount', { count: tools.length }));
    if (roots.length) foldBits.push(t('turnCtx.workspaceCount', { count: roots.length }));
    if (git && git.branch) foldBits.push('⑂ ' + git.branch);
    if (todos.length) {
      const doneN = todos.filter((x) => x && x.status === 'completed').length;
      foldBits.push(t('turnCtx.todoCount', { done: doneN, total: todos.length }));
    }
    /* The fold truncates with ellipsis on tight panes; the title keeps the
     * full (already localized) line one hover away. */
    const foldLine = foldBits.join(' · ');
    const fold = '<div class="tctx-fold" title="' + _esc(foldLine) + '"><span class="tctx-fold-dot"></span>' +
      '<span>' + _esc(foldLine) + '</span></div>';

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
      /* The overflow sits next to the toggle inside whatever wrapper the row
       * uses (.tctx-chips / .tctx-paths / .tctx-todos). Search the toggle's
       * parent first, then the row, so a differently-shaped row (checklist)
       * still resolves its own overflow. */
      const scope = btn.parentNode;
      const row = btn.closest('.tctx-row');
      const hidden = (scope && scope.querySelector('.tctx-overflow'))
        || (row && row.querySelector('.tctx-overflow'));
      if (hidden) {
        const opening = hidden.hasAttribute('hidden');
        if (opening) hidden.removeAttribute('hidden');
        else hidden.setAttribute('hidden', '');
        btn.setAttribute('aria-expanded', opening ? 'true' : 'false');
        btn.textContent = opening ? t('turnCtx.showLess') : t('turnCtx.showMore', {count: hidden.children.length});
      }
    }
    ev.stopPropagation();
    /* The toggle just changed the rail's height — re-clamp any pinned
     * travel so it never grows over the next round's rail. */
    _scheduleRailFollow();
  }

  /* ── Sticky rail follower ─────────────────────────────────────────
   * The rail is absolutely positioned at the TOP of its own turn's box
   * (out of flow so it never inflates the row). That box is short — an
   * input turn is often one line — so the turn's context used to scroll
   * away with the bubble while the round's reply filled screens below it
   * (2026-09-08 owner report: "when a conversation is scrolling, the turn
   * ctx should move along until it reaches the next round").
   *
   * The follower re-implements sticky-header stacking WITHOUT a DOM
   * restructure: on scroll/resize it translates each rail down by
   *     off = clamp(stickTop − hostTop, 0, nextRailTop − railHeight − hostTop)
   * — pinned to the pane's top inside its round, pushed out exactly when
   * the next round's rail arrives, in one continuous motion. The travel
   * carries the rail BEYOND its host's border box, which is why the host
   * message drops content-visibility's paint containment (see
   * `.message:has(> .turn-ctx)` in 17-compaction-context-rail.css); the
   * rail track ends at the next human input OR the next visible rail in
   * the same lane, whichever comes first. Assistant turns can also carry
   * context; treating only human turns as boundaries stacks two cards.
   *
   * Geometry is measured entirely from UNTRANSFORMED boxes (the host
   * message's rect; rail height is transform-independent), so a
   * previously applied translation never feeds back into the next one.
   * Runs at most once per animation frame; the reads force layout, which
   * is clean on pure scrolls and already dirty while streaming. */
  const _RAIL_STICK_PAD_PX = 6;
  let _railFollowScheduled = false;
  let _railFollowObserved = [];
  let _railFollowRO = null;
  let _railFollowMO = null;

  function _updateRailFollow() {
    /* Partial-DOM guard: bare-node harnesses and SSR shims stub `document`
     * with only readyState/addEventListener. Bailing here keeps those loads
     * inert without changing the real-browser path (where querySelectorAll
     * always exists). */
    if (typeof document === 'undefined'
        || typeof document.querySelectorAll !== 'function') return;
    const containers = document.querySelectorAll('.chat-container');

    runtimeScope.mountBackgroundAgents?.();
    for (const cont of containers) {
      const rails = cont.querySelectorAll('.turn-ctx');
      if (!rails.length) continue;
      const contR = cont.getBoundingClientRect();
      const stickTop = contR.top + _RAIL_STICK_PAD_PX;
      /* Bottom of the scrollable content in VIEW coordinates: the last
       * rail may pin until its round's tail leaves the pane. */
      const contentBottom = contR.top + (cont.scrollHeight - cont.scrollTop);
      const live = [];
      for (const rail of rails) {
        if (rail.offsetParent === null || rail.hidden) {
          /* Fold mode / empty rail: park any travel AND its computed height
           * so a re-granted track never resurrects a stale offset or box. */
          if (rail.style.transform) rail.style.transform = '';
          if (rail.style.maxHeight) rail.style.maxHeight = '';
          rail.classList.remove('tctx-stuck', 'tctx-no-space');
          continue;
        }
        live.push(rail);
      }
      /* Measure intrinsic height, never last frame's clipped height.
       * Otherwise each reclamp shrinks the card again and an expanded
       * track cannot restore content after a zero-space interval. Batch
       * resets before reads and restore final caps before the next paint. */
      live.forEach((rail) => { if (rail.style.maxHeight) rail.style.maxHeight = ''; });
      const railedHosts = new Set(live.map((rail) => rail.parentElement));
      const geometry = live.map((rail) => {
        const host = rail.parentElement;
        const style = getComputedStyle(rail);
        const insets = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
          + parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
        let nextTop = contentBottom;
        /* Both boundaries matter: an unrailed human starts a new round,
         * while a railed assistant starts a new card within this round.
         * Sibling hosts keep independent lanes from constraining each other.
         * Hidden/empty rails never reserve a boundary. */
        for (let el = host.nextElementSibling; el; el = el.nextElementSibling) {
          if (el.classList && (el.classList.contains('user-msg') || railedHosts.has(el))) {
            nextTop = Math.min(nextTop, el.getBoundingClientRect().top);
            break;
          }
        }
        return { rail, hostTop: host.getBoundingClientRect().top,
          railH: rail.getBoundingClientRect().height, nextTop, insets };
      });
      for (const { rail, hostTop, railH, nextTop, insets } of geometry) {
        let off = stickTop - hostTop;
        const limit = nextTop - railH - hostTop;
        if (off > limit) off = limit;
        if (!(off > 0.5)) off = 0;
        if (off > 0) {
          const y = 'translateY(' + off.toFixed(1) + 'px)';
          if (rail.style.transform !== y) rail.style.transform = y;
          rail.classList.add('tctx-stuck');
        } else {
          if (rail.style.transform) rail.style.transform = '';
          rail.classList.remove('tctx-stuck');
        }
        /* USE THE SPACE: size the rail to the actual free track. The old
         * caps were a host-relative 100% parked (a one-line input turn gave
         * the rail ~1 line) and a fixed 60vh while stuck — the gutter beside
         * the reply sat empty while the rail's own facts scrolled internally
         * (the 2026-09-23 "two halves that don't move together" report).
         * The free track space is nextTop − visible top (its bottom edge is
         * exactly the follower's push-out clamp), so the rail is sized to
         * fill the space it already glides through instead of being squeezed
         * by its one-line host. The CSS cap becomes none — the geometry,
         * not an arbitrary vh, is the budget. Out-of-flow + the clamp keep
         * the no-row-inflation and continuous-handoff contracts intact. */
        // Use the same tenth-pixel translation that was applied above.
        // Rounding the remaining height independently opens a visible seam
        // at handoff when localized text gives the rail fractional height.
        const visibleTop = hostTop + (off > 0 ? Math.round(off * 10) / 10 : 0);
        const maxH = Math.max(0, nextTop - visibleTop);
        rail.style.maxHeight = maxH + 'px';
        /* max-height:0 still paints padding/borders. Suppress that sliver,
         * without display:none/hidden (which would drop this rail from the
         * next geometry pass and prevent recovery when space returns). */
        rail.classList.toggle('tctx-no-space', maxH <= insets);
      }
    }
    _railFollowReobserve();
  }

  /* Scroll alone doesn't cover the follower's triggers: streaming grows
   * the content under a pinned rail, and the sidebar/inspector drawer
   * resize the pane. chat-inner's box tracks both; a rail's own growth
   * (the "+N" toggle) re-clamps via the click handler above. Transforms
   * never change these observed boxes, so the observer can't self-feed. */
  function _railFollowReobserve() {
    if (typeof ResizeObserver === 'undefined') return;
    const nodes = [];
    document.querySelectorAll('.chat-container').forEach((c) => {
      nodes.push(c);
      const inner = c.querySelector('.chat-inner');
      if (inner) nodes.push(inner);
    });
    const same = nodes.length === _railFollowObserved.length
      && nodes.every((n, i) => n === _railFollowObserved[i]);
    if (same) return;
    if (_railFollowRO) _railFollowRO.disconnect();
    else _railFollowRO = new ResizeObserver(_scheduleRailFollow);
    _railFollowObserved = nodes;
    nodes.forEach((n) => _railFollowRO.observe(n));
    /* A new absolute rail (or a hidden rail becoming populated) need not
     * resize chat-inner. Watch structure/visibility, never our own style or
     * class writes, so reconciliation also gets one coalesced clamp. The
     * observer shares the page follower's rebinding lifecycle. */
    if (typeof MutationObserver !== 'undefined') {
      if (_railFollowMO) _railFollowMO.disconnect();
      else _railFollowMO = new MutationObserver(_scheduleRailFollow);
      nodes.filter((n) => n.classList.contains('chat-container')).forEach((n) => {
        _railFollowMO.observe(n, { childList: true, subtree: true,
          attributes: true, attributeFilter: ['hidden', 'open'] });
      });
    }
  }

  function _scheduleRailFollow() {
    if (_railFollowScheduled) return;
    _railFollowScheduled = true;
    if (typeof requestAnimationFrame !== 'function') {
      _railFollowScheduled = false;
      _updateRailFollow();
      return;
    }
    requestAnimationFrame(() => {
      _railFollowScheduled = false;
      _updateRailFollow();
    });
  }

  function _onRailFollowScroll(ev) {
    const t = ev.target;
    if (t && t.classList && t.classList.contains('chat-container')) {
      _scheduleRailFollow();
    }
  }

  /* Fact-card fields on the done frame the reconcile MAY overwrite. Kept as
   * a named list so a test / audit can enumerate what "settles" at done. */
  const _CTX_FACT_FIELDS = ['actualModel', 'actualDepth', 'actualModes'];

  /**
   * Reconcile a captured/persisted turn-ctx snapshot against the done
   * frame's authoritative fact card. Mutates `snap` in place; returns true
   * when anything changed. `fact.actualModel` / `fact.actualDepth` /
   *      `fact.actualModes` are the server-authoritative record of what
   *      the turn actually ran with. When present they OVERWRITE
   *      `snap.model` / `snap.depth` / `snap.modes` verbatim so the note
   *      settles as the truth after a dispatcher fallback or a preset
   *      switched in the pause between send and stream-start. See the
   *      file-header contract at the top for the send-vs-done lifecycle.
   *
   * @param {object} snap — a captured/persisted turn-ctx snapshot (msg._ctx).
   * @param {object} fact — done-event projection; ALL fields optional:
   *   { actualModel?, actualDepth?, actualModes? }
   */
  function reconcileTurnCtxCapsule(snap, fact) {
    if (!snap || typeof snap !== 'object' || !fact || typeof fact !== 'object') return false;
    let modes = Array.isArray(snap.modes) ? snap.modes.slice() : [];
    let changed = false;
    if (typeof fact.actualModel === 'string' && fact.actualModel
        && snap.model !== fact.actualModel) {
      snap.model = fact.actualModel;
      changed = true;
    }
    if (Object.prototype.hasOwnProperty.call(fact, 'actualDepth')) {
      /* The server's actualDepth echoes the intent token verbatim, so it can
       * carry 'default' (model_config passes cfg.thinkingDepth through
       * unchanged). Resolve that token to the actual rung, falling back to
       * the snapshot's already-resolved depth so the done frame never
       * degrades a concrete rung back to the bare 'default' label. */
      const _rawDepth = typeof fact.actualDepth === 'string' ? fact.actualDepth : '';
      const _newDepth = _displayDepth(_rawDepth, snap.depth);
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

  /* ═══════════════ Live branch + checklist state ═══════════════
   * The two rows that must move DURING a turn: the workspace git branch
   * (the agent can `git checkout` mid-run) and the checklist (every
   * todo_write rewrites it). Everything else on the card is a frozen
   * send-time snapshot.
   *
   * CONVERSATION SCOPING (2026-09-21 cross-chat leak fix): the live state
   * below belongs to the ACTIVE conversation's IN-FLIGHT turn ONLY. It used
   * to be applied to EVERY `.turn-ctx` on the page and NEVER reset on a
   * conversation switch, so one chat's running checklist/branch bled into
   * every other open conversation — and even overwrote settled turns' own
   * frozen rows. Now each settled rail keeps the rows frozen into its OWN
   * snapshot (rendered by renderTurnCtxNote), and the live patch only
   * rewrites the active conversation's in-flight rail. Switching
   * conversations clears the live state (see activateTurnCtxConversation)
   * so nothing carries across chats. */
  let _liveGit = null;           // { branch, branches:[{path,short,branch}] } | null
  let _liveTodos = [];           // [{content,status},…] from the latest todo_write
  let _liveConvId = null;        // active conversation these live rows belong to
  let _patchScheduled = false;

  function _snapshotLiveState() {
    return { git: _liveGit, todos: _liveTodos.slice() };
  }

  /* Reset / re-bind the live rows to a newly-active conversation. Called on
   * every conversation open / new-chat so a previous chat's running
   * checklist or branch never leaks into this one. The freshly-rendered
   * rails show their own per-turn frozen snapshots; live updates resume on
   * the next git / todo_write event for THIS conversation. */
  function activateTurnCtxConversation(convId) {
    const next = (convId == null) ? null : String(convId);
    if (next === _liveConvId) return;
    _liveConvId = next;
    _liveGit = null;
    _liveTodos = [];
    _scheduleCtxPatch();
  }

  /* Ingest a project status payload's git fields into the live branch
   * state. Detects a real branch SWITCH and surfaces it (debug toast) so
   * a mid-run `git checkout` is visible even off the rail. Returns true
   * when the primary branch changed. */
  function applyProjectGitState(data) {
    if (!data || typeof data !== 'object') return false;
    const ps = (typeof projectState !== 'undefined') ? projectState : null;
    if (ps) {
      if (typeof data.gitBranch === 'string') ps.gitBranch = data.gitBranch;
      else if (Array.isArray(data.gitBranches) && data.gitBranches.length) {
        ps.gitBranch = data.gitBranches[0].branch || '';
      } else if (Object.prototype.hasOwnProperty.call(data, 'gitBranch')) {
        ps.gitBranch = data.gitBranch;
      }
      if (Array.isArray(data.gitBranches)) ps.gitBranches = data.gitBranches;
    }
    const next = _collectGit();
    const prevBranch = _liveGit && _liveGit.branch;
    const nextBranch = next && next.branch;
    const changed = prevBranch !== nextBranch;
    const sameShape = JSON.stringify(_liveGit) === JSON.stringify(next);
    _liveGit = next;
    if (!sameShape) _scheduleCtxPatch();
    if (changed && nextBranch) {
      /* A real switch mid-turn: the rail re-renders via the patch above;
       * this log line is the off-rail signal so the change isn't silent
       * when the rail track is folded away. */
      if (typeof debugLog === 'function') {
        debugLog(
          (typeof t === 'function'
            ? t('turnCtx.branchSwitched', { branch: nextBranch })
            : 'Branch → ' + nextBranch),
          'info',
        );
      }
    }
    return changed;
  }

  /* Ingest a checklist state — from a live todo_write round (results[0]
   * .todos / round.todos), the done frame's todoState, or a persisted
   * turn's todoState. Normalizes to the renderer's {content,status}
   * shape. Re-renders the live rails on change. */
  function updateTurnCtxTodos(raw, opts) {
    const list = Array.isArray(raw) ? raw
      : (raw && Array.isArray(raw.todos) ? raw.todos : null);
    if (!list) return false;
    const next = [];
    for (const item of list) {
      if (!item || typeof item !== 'object') continue;
      const content = String(item.content || '').trim();
      if (!content) continue;
      next.push({
        content: content.slice(0, 240),
        status: (item.status === 'completed' || item.status === 'in_progress'
          || item.status === 'blocked') ? item.status : 'pending',
      });
    }
    const same = next.length === _liveTodos.length
      && next.every((x, i) => _liveTodos[i]
        && _liveTodos[i].content === x.content && _liveTodos[i].status === x.status);
    if (same) return false;
    _liveTodos = next;
    _scheduleCtxPatch();
    return true;
  }

  /* Resolve the turn's context snapshot object. The single wire/projection
   * representation is `projection.contextSnapshot.snapshot` (nested); the
   * legacy flat `projection._ctx` is deleted on rebind and never present on
   * an authoritative turn. Returns the nested snapshot so reads AND writes
   * land where renderTurnCtxNote actually looks (block.value.snapshot). */
  function _turnCtxSnapshot(turn) {
    const proj = turn && turn.projection;
    if (!proj || typeof proj !== 'object') return null;
    const cs = proj.contextSnapshot;
    if (cs && typeof cs === 'object' && cs.snapshot && typeof cs.snapshot === 'object') {
      return cs.snapshot;
    }
    return (proj._ctx && typeof proj._ctx === 'object') ? proj._ctx : null;
  }

  /* Rebuild the branch + checklist rows of the ACTIVE conversation's
   * IN-FLIGHT rail, in place, from the live state — one rAF batch per
   * change burst so a rapid todo_write stream never thrashes layout.
   *
   * SCOPE: the in-flight turn is TRANSIENT (excluded from
   * ConversationTurnRead.ordered()) and renders as the LAST human-input
   * rail in the conversation, so the DOM patch targets that last rail.
   * Settled turns — in this chat and every other — keep the rows frozen
   * into their OWN snapshots, which is what kills the cross-chat leak: a
   * global live state is never broadcast to every rail on the page. The
   * running turn's authoritative contextSnapshot.snapshot is reconciled
   * too, so the authoritative re-render and a mid-turn reload keep the
   * latest branch/list. */
  function _patchLiveCtxRails() {
    _patchScheduled = false;
    if (typeof document === 'undefined'
        || typeof document.querySelectorAll !== 'function') return;
    const live = _snapshotLiveState();
    try {
      const conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
      /* Ignore live state left over from a different conversation. */
      if (!conv || (_liveConvId !== null && String(conv.id) !== _liveConvId)) {
        _scheduleRailFollow();
        return;
      }
      /* The in-flight turn is TRANSIENT (excluded from ConversationTurnRead
       * .ordered()) and is always rendered as the LAST human-input rail in
       * the conversation. Patch that rail's rows in place. Settled turns —
       * in this chat and every other — keep the rows frozen into their OWN
       * snapshots, which is what kills the cross-chat leak.
       *
       * SCOPE the selector to HUMAN turns (.user-msg). Every turn — human
       * AND assistant — carries data-turn-id (conversation-surface.ts sets it
       * unconditionally), and a streaming assistant turn ALSO renders a rail,
       * one that has NO tctx-head / tools / workspace. Selecting the bare
       * `[data-turn-id] > .turn-ctx` last-match landed the patch on that
       * assistant rail during a stream and appended branch+checklist rows
       * into it — the head-less, rows-piled-on-each-other overlap reported
       * 2026-09-23. The live branch/checklist belong ONLY on the in-flight
       * human turn's rail, which always has the full head+tools+workspace. */
      const rails = document.querySelectorAll('.chat-container .user-msg[data-turn-id] > .turn-ctx');
      const rail = rails.length ? rails[rails.length - 1] : null;
      if (rail) {
        const facts = rail.querySelector('[data-conversation-part="turn-context-content"]') || rail;
        /* Branch row: rebuild (or remove) in place. Prefer the live branch
         * (it moves on a mid-turn `git checkout`); fall back to the branch
         * derivable from projectState so a gap in the live state never
         * blanks a branch the workspace is still on. */
        const gitRow = rail.querySelector('[data-tctx-git]');
        const gitForRow = live.git || _collectGit();
        const gitHtml = _renderGitRowHtml(gitForRow);
        if (gitHtml) {
          if (gitRow) gitRow.outerHTML = gitHtml;
          else facts.insertAdjacentHTML('beforeend', gitHtml);
        } else if (gitRow) gitRow.remove();
        /* Checklist row: rebuild (or remove) in place. */
        const todoRow = rail.querySelector('[data-tctx-todo]');
        const todoHtml = _renderTodoRowHtml(live.todos);
        if (todoHtml) {
          if (todoRow) todoRow.outerHTML = todoHtml;
          else facts.insertAdjacentHTML('beforeend', todoHtml);
        } else if (todoRow) todoRow.remove();
      }
      /* Reconcile the in-flight turn's persisted snapshot so the authoritative
       * re-render (and a mid-turn reload) keeps the latest branch/list. Find
       * the running assistant turn's parent human turn and write into its
       * contextSnapshot.snapshot — the same object the rail renders from. */
      const read = runtimeScope.ConversationTurnRead;
      if (read && typeof read.ordered === 'function') {
        const turns = read.ordered(conv) || [];
        for (let i = turns.length - 1; i >= 0; i--) {
          const turn = turns[i];
          if (!turn || turn.actor !== 'assistant') continue;
          if (!(turn.status === 'pending' || turn.status === 'running')) break;
          for (let j = i - 1; j >= 0; j--) {
            const ut = turns[j];
            if (!ut || ut.actor !== 'human') break;
            const snap = _turnCtxSnapshot(ut);
            if (snap) {
              if (live.git) snap.git = live.git;
              snap.todos = live.todos.slice();
            }
            break;
          }
          break;
        }
      }
    } catch (_e) { /* best-effort reconcile; settled rails already correct */ }
    /* The patched rows changed rail heights — re-clamp sticky travel. */
    _scheduleRailFollow();
  }

  function _renderGitRowHtml(git) {
    if (!git || !git.branch) return '';
    const rows_ = Array.isArray(git.branches) && git.branches.length ? git.branches
      : [{ path: '', short: '', branch: git.branch }];
    const multi = rows_.length > 1;
    const chips = rows_.map((b) =>
      '<span class="tctx-chip tctx-tone-git" title="' +
      _esc((b.path ? b.path + ' ￩ ' : '') + b.branch) + '">' + _BRANCH +
      '<span>' + _esc(b.branch) + '</span>' +
      (multi && b.short ? '<span class="tctx-branch-root">' + _esc(b.short) + '</span>' : '') +
      '</span>').join('');
    return '<div class="tctx-row tctx-git-row" data-tctx-git="1"><span class="tctx-row-h">' +
      _esc(t('turnCtx.branchLabel')) + '</span>' +
      '<div class="tctx-chips">' + chips + '</div></div>';
  }

  function _renderTodoRowHtml(todos) {
    if (!todos || !todos.length) return '';
    /* No label cell: the checklist is a single chip inside the chip flow,
     * so the row is just the chip (no "待办 |" header splitting it off). */
    return '<div class="tctx-row tctx-todo-row" data-tctx-todo="1">' +
      _buildTodoListHtml(todos) + '</div>';
  }

  function _scheduleCtxPatch() {
    if (_patchScheduled) return;
    _patchScheduled = true;
    if (typeof requestAnimationFrame !== 'function') {
      _patchLiveCtxRails();
      return;
    }
    requestAnimationFrame(_patchLiveCtxRails);
  }

  runtimeScope.buildTurnCtxSnapshot = buildTurnCtxSnapshot;
  runtimeScope.renderTurnCtxNote = renderTurnCtxNote;
  runtimeScope.reconcileTurnCtxCapsule = reconcileTurnCtxCapsule;
  runtimeScope.applyMcpToolSummary = applyMcpToolSummary;
  runtimeScope.applyProjectGitState = applyProjectGitState;
  runtimeScope.updateTurnCtxTodos = updateTurnCtxTodos;
  runtimeScope.activateTurnCtxConversation = activateTurnCtxConversation;

  /* Browser lifecycle: clicks (delegated), pane scrolls (captured —
   * element scrolls don't bubble), one boot pass, and a ResizeObserver
   * (re)bound by every pass for streaming growth / pane resizes. MCP
   * state arrives through existing responses and installs no hook. */
  if (typeof document !== 'undefined') {
    document.addEventListener('click', _onTctxClick);
    document.addEventListener('scroll', _onRailFollowScroll, { capture: true, passive: true });
    _scheduleRailFollow();
  }
})();

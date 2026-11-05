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
        A bridge: the markup is owned by the typed module
        frontend/src/conversation/ui/turn-context-rail (see its AGENTS.md);
        this section only supplies the retained catalog/branch services.
        Deletion path: the Surface port takes those services directly.
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
    if (typeof desktopEnabled !== 'undefined' && desktopEnabled) out.push({ label: 'Desktop', tone: 'net' });
    if (typeof codeExecEnabled !== 'undefined' && codeExecEnabled) out.push({ label: 'Code Exec', tone: 'code' });
    if (typeof memoryEnabled !== 'undefined' && memoryEnabled) out.push({ label: 'Memory', tone: 'ai' });
    // Scheduler and Browser are default tools (always on, no toggle) — like
    // read_files / todo they are intentionally NOT shown as per-turn chips
    // (would be constant noise). The reconcile rule below still maps the
    // scheduler fn names defensively.
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
   * snapshot records the primary branch and each root's own branch, so
   * every workspace line of the rail reads `root:branch`.
   * Returns { branch, branches: [{path, branch}] } or null. */
  function _collectGit() {
    const ps = (typeof projectState !== 'undefined') ? projectState : null;
    if (!ps) return null;
    const branches = [];
    const seen = new Set();
    const rows = Array.isArray(ps.gitBranches) ? ps.gitBranches : [];
    for (const row of rows) {
      const path = typeof row?.path === 'string' ? row.path : '';
      const branch = typeof row?.branch === 'string' ? row.branch.trim() : '';
      if (!path || !branch || seen.has(path)) continue;
      seen.add(path);
      branches.push({ path, branch: branch.slice(0, 80) });
    }
    let branch = typeof ps.gitBranch === 'string' ? ps.gitBranch.trim() : '';
    if (!branch && branches.length) branch = branches[0].branch;
    if (!branch) return null;
    /* A status payload without per-root probes names the primary's branch. */
    return { branch, branches: branches.length ? branches : [{ path: ps.path || '', branch }] };
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

  /* Retained services of the typed rail renderer
   * (frontend/src/conversation/ui/turn-context-rail, the single owner of the
   * rail's markup): the catalog's model names, the brand marks the model
   * picker paints, and the branch the workspace is on now. */
  const _railHost = {
    text(key, fallback, values) {
      const localized = t(key, values);
      if (localized && localized !== key) return localized;
      return String(fallback).replace(/\{(\w+)\}/g, (token, name) => (
        values && Object.prototype.hasOwnProperty.call(values, name) ? String(values[name]) : token));
    },
    modelLabel(modelId) {
      return (typeof _modelShortName === 'function') ? _modelShortName(modelId) : modelId;
    },
    brandIconHtml(modelId) {
      if (typeof _brandSvg !== 'function') return '';
      if (typeof _modelBrand === 'function') return _brandSvg(_modelBrand(modelId), 16);
      if (typeof _detectBrand === 'function') return _brandSvg(_detectBrand(modelId), 16);
      return '';
    },
    currentGit: _collectGit,
  };

  /**
   * Render a captured or persisted snapshot into the per-turn context
   * surfaces: `{fold, rail}` HTML strings, or '' when there is nothing to
   * show. The two have DIFFERENT DOM homes: the rail (`.turn-ctx`) is a
   * direct child of the Turn, absolutely positioned into the rail column the
   * pane owns and stretched into the track its sticky card travels (see
   * "Rail tracks" below); the fold (`.tctx-fold`)
   * sits inside `.message-content` and shows exactly when the pane cannot
   * grant the track. Keep them separate: spliced as one string, the fold
   * auto-placed into the zero-width rail track and painted nothing.
   */
  function renderTurnCtxNote(snap) {
    return renderTurnContextRailHtml(snap, _railHost) || '';
  }

  /* "+N" toggle + rail click guard. Delegated at document level so it
   * survives every re-render of the message list without per-node listener
   * bookkeeping. Expanding grows the card IN FLOW — no overlay, no paint
   * containment to escape; the card's CSS height bound absorbs the growth
   * and the track is unchanged. The card is hit-testable (it scrolls, paths
   * have hover titles), but a click ANYWHERE inside the rail must never be
   * seen by a message-level delegated handler — so every rail click is
   * swallowed here, toggle or not. */
  function _onTctxClick(ev) {
    const rail = (ev.target && ev.target.closest)
      ? ev.target.closest('.turn-ctx') : null;
    if (!rail) return;
    const btn = ev.target.closest('[data-tctx-more]');
    if (btn) {
      /* The toggle sits on its section heading and gates the hidden
       * .tctx-overflow group of the same row. Search the toggle's parent
       * first, then the row, so a differently-shaped row (checklist) still
       * resolves its own overflow. */
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
  }

  /* ── Rail tracks ─────────────────────────────────────────────────
   * The rail root (`.turn-ctx`) is absolutely positioned at the TOP of its
   * own turn's box (out of flow so it never inflates the row). That box is
   * short — an input turn is often one line — while the round's reply fills
   * screens below it, and the context must move along with the reading
   * until the next round arrives (2026-09-08 owner report).
   *
   * Each rail root is therefore a TRACK: stretched from its host's top down
   * to the host of the next visible rail in the same lane, or to the end of
   * the transcript. Its card (`.tctx-card`) is `position: sticky` inside it
   * (23-reading-context-rail.css), so the browser does the travel — pinned
   * under the pane's top edge inside its round, pushed out exactly as the
   * next round's card arrives, in one continuous motion, in the same frame
   * as the scroll. The card is bounded by its track and by the pane height
   * published here, so a card with more facts than fit scrolls within itself
   * and its last section is reachable from anywhere in the round.
   *
   * This replaced a follower that translated each card from a scroll
   * listener and sized it to the free track below it: the card trailed the
   * compositor's scroll by a frame or more (worst while a stream kept the
   * main thread busy), and a card taller than the pane hung below its edge,
   * unreachable until the reader scrolled to the round's end (2026-11-01
   * owner report).
   *
   * Assistant turns can also carry context; treating only human turns as
   * boundaries stacks two cards. An input persisted without a context
   * snapshot is not a boundary: the preceding card keeps its seat beside
   * that round instead of leaving the gutter empty.
   *
   * Script measures LAYOUT only. A track's length is a difference of host
   * positions, which scrolling never changes; it changes when content inside
   * or above a round changes size (streaming, disclosures, re-renders, a
   * rail appearing). The pane height changes with the window and composer.
   * Resizes are measured before the frame paints; structure changes take a
   * pass coalesced to the next frame; nothing runs on scroll. */
  let _railTracksScheduled = false;
  let _railTracksObserved = [];
  let _railTracksRO = null;
  let _railTracksMO = null;
  /* The length last written per track: a fractional px value does not
   * round-trip through the CSSOM serializer, so compare with the write. */
  const _railTrackLength = new WeakMap();

  function _setRailTrack(rail, length) {
    if (_railTrackLength.get(rail) === length) return;
    _railTrackLength.set(rail, length);
    rail.style.height = length === null ? '' : length + 'px';
  }

  /* Writes only track lengths, the pane height and paint state, none of
   * which resizes an observed box, so it is safe inside a resize callback. */
  function _measureRailTracks() {
    for (const cont of document.querySelectorAll('.chat-container')) {
      const paneHeight = cont.clientHeight + 'px';
      if (cont.style.getPropertyValue('--tctx-pane-h') !== paneHeight) {
        cont.style.setProperty('--tctx-pane-h', paneHeight);
      }
      const live = [];
      for (const rail of cont.querySelectorAll('.turn-ctx')) {
        if (rail.offsetParent === null || rail.hidden) {
          /* Fold mode / empty rail: drop its track so a re-granted rail
           * never resurrects a stale length. */
          _setRailTrack(rail, null);
          rail.classList.remove('tctx-no-space');
          continue;
        }
        live.push(rail);
      }
      const transcript = cont.querySelector('.chat-inner');
      if (!live.length || !transcript) continue;
      const contR = cont.getBoundingClientRect();
      /* The last track ends with the transcript's in-flow content, or at the
       * pane's bottom while the transcript is shorter than the pane. Never
       * the scroll height, which a track's own extent could feed. */
      const end = Math.max(transcript.getBoundingClientRect().bottom,
        contR.top + cont.clientTop + cont.clientHeight - cont.scrollTop);
      const railedHosts = new Set(live.map((rail) => rail.parentElement));
      const tracks = live.map((rail) => {
        const host = rail.parentElement;
        let nextTop = end;
        /* The next visible card, a new round's or a railed assistant's,
         * ends this one. Sibling hosts keep independent lanes from
         * constraining each other. Hidden/empty rails never reserve a
         * boundary. */
        for (let el = host.nextElementSibling; el; el = el.nextElementSibling) {
          if (railedHosts.has(el)) {
            nextTop = el.getBoundingClientRect().top;
            break;
          }
        }
        const card = rail.querySelector(':scope > .tctx-card');
        const style = card ? getComputedStyle(card) : null;
        const insets = style
          ? parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
            + parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth)
          : 0;
        /* From the track's own top: its grid placement inside the host may
         * start below the host's border edge, and its top never depends on
         * the length written below. */
        return { rail, insets,
          length: Math.max(0, nextTop - rail.getBoundingClientRect().top) };
      });
      for (const { rail, length, insets } of tracks) {
        _setRailTrack(rail, length);
        /* A card cannot shrink below its padding, so a shorter track would
         * paint a sliver of it over the next card. Suppress it without
         * display:none/hidden, which would drop this rail from the next pass
         * and prevent recovery when space returns. */
        rail.classList.toggle('tctx-no-space', length <= insets);
      }
    }
  }

  function _railTrackPass() {
    /* Partial-DOM guard: bare-node harnesses and SSR shims stub `document`
     * with only readyState/addEventListener. Bailing here keeps those loads
     * inert without changing the real-browser path (where querySelectorAll
     * always exists). */
    if (typeof document === 'undefined'
        || typeof document.querySelectorAll !== 'function') return;
    runtimeScope.mountBackgroundAgents?.();
    _measureRailTracks();
    _observeRailTrackLayout();
  }

  /* Lengthen the tracks before this frame paints. A stream grows the
   * transcript and follows its end in one frame; a pass a frame later let
   * the last card paint pushed up by that growth, then snap back. A pane
   * resize can also move the agent section between card and fold, which
   * mounts in-flow content and so stays on the coalesced pass. */
  function _onRailTrackResize(entries) {
    _measureRailTracks();
    if (entries.some((entry) => entry.target.classList.contains('chat-container'))) {
      _scheduleRailTracks();
    }
  }

  /* chat-inner's box changes with streaming growth and every content resize;
   * the chat container's with the window, sidebar, drawer and composer (the
   * pane height). Track writes never change these observed boxes, so the
   * observer cannot self-feed. */
  function _observeRailTrackLayout() {
    if (typeof ResizeObserver === 'undefined') return;
    const nodes = [];
    document.querySelectorAll('.chat-container').forEach((c) => {
      nodes.push(c);
      const inner = c.querySelector('.chat-inner');
      if (inner) nodes.push(inner);
    });
    const same = nodes.length === _railTracksObserved.length
      && nodes.every((n, i) => n === _railTracksObserved[i]);
    if (same) return;
    if (_railTracksRO) _railTracksRO.disconnect();
    else _railTracksRO = new ResizeObserver(_onRailTrackResize);
    _railTracksObserved = nodes;
    nodes.forEach((n) => _railTracksRO.observe(n));
    /* A new absolute rail (or a hidden rail becoming populated) need not
     * resize chat-inner. Watch structure/visibility, never our own style or
     * class writes, so reconciliation also gets one coalesced pass. The
     * observer shares the resize observer's rebinding lifecycle. */
    if (typeof MutationObserver !== 'undefined') {
      if (_railTracksMO) _railTracksMO.disconnect();
      else _railTracksMO = new MutationObserver(_scheduleRailTracks);
      nodes.filter((n) => n.classList.contains('chat-container')).forEach((n) => {
        _railTracksMO.observe(n, { childList: true, subtree: true,
          attributes: true, attributeFilter: ['hidden', 'open'] });
      });
    }
  }

  function _scheduleRailTracks() {
    if (_railTracksScheduled) return;
    _railTracksScheduled = true;
    if (typeof requestAnimationFrame !== 'function') {
      _railTracksScheduled = false;
      _railTrackPass();
      return;
    }
    requestAnimationFrame(() => {
      _railTracksScheduled = false;
      _railTrackPass();
    });
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
    if (!sameShape) {
      _scheduleCtxPatch();
      _repaintRailsMissingBranch();
    }
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

  /* Turns sent BEFORE the workspace's branch was known froze a snapshot with
   * no `git` — an engine restart clears the scoped root registry, so the branch
   * only arrives with the enrichment fetch that follows the saved project
   * path. Their rails were already painted from those snapshots, so ask the
   * authoritative surface to repaint; the render path then falls back to the
   * live branch (`renderTurnCtxNote`) without writing into the frozen record.
   * Only a conversation that actually has a branch-less human turn with
   * workspace roots (a root-less turn shows no branch) triggers the repaint,
   * so this cannot loop. */
  function _repaintRailsMissingBranch() {
    const conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
    if (!conv || !conv.id) return;
    const read = runtimeScope.ConversationTurnRead;
    if (!read || typeof read.ordered !== 'function') return;
    const missing = (read.ordered(conv) || []).some((turn) => {
      if (!turn || turn.actor !== 'human') return false;
      const snap = _turnCtxSnapshot(turn);
      return !!snap && !snap.git && Array.isArray(snap.roots) && snap.roots.length > 0;
    });
    if (!missing) return;
    if (typeof runtimeScope.requestAuthoritativeConversationRender === 'function') {
      runtimeScope.requestAuthoritativeConversationRender(conv.id);
    }
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
      if (!conv || (_liveConvId !== null && String(conv.id) !== _liveConvId)) return;
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
        /* Each workspace line ends with its root's branch (`root:branch`):
         * rebuild (or remove) that suffix in place. Prefer the live branch
         * (it moves on a mid-turn `git checkout`); fall back to the branch
         * derivable from projectState so a gap in the live state never
         * blanks a branch the workspace is still on. Lines keep snapshot
         * order, so the first is the primary root. */
        const gitForRow = live.git || _collectGit();
        rail.querySelectorAll('[data-tctx-root]').forEach((line, index) => {
          const html = renderTurnContextRootBranchHtml(gitForRow, line.dataset.tctxRoot, index === 0);
          const suffix = line.querySelector('[data-tctx-git]');
          if (suffix) suffix.outerHTML = html;
          else if (html) line.querySelector('.tctx-path-text')?.insertAdjacentHTML('beforeend', html);
        });
        /* Checklist row: rebuild (or remove) in place. */
        const todoRow = rail.querySelector('[data-tctx-todo]');
        const todoHtml = renderTurnContextChecklistRowHtml(live.todos, _railHost);
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

  /* Browser lifecycle: clicks (delegated), one boot pass, and the resize /
   * mutation observers (re)bound by every pass for layout changes. Scrolling
   * runs no script: the sticky card travels on its own. MCP state arrives
   * through existing responses and installs no hook. */
  if (typeof document !== 'undefined') {
    document.addEventListener('click', _onTctxClick);
    _scheduleRailTracks();
  }
})();

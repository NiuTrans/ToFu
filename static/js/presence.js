/* ═══════════════════════════════════════════════════════════════════
   presence.js — the Project **Collaboration Bar**.

   A single slim line docked under the top bar (project mode only, shown only
   when there is something collaborative to surface). It REPLACES the old
   multi-row "who's working" presence strip — which merely echoed activity you
   already see in the sidebar — with the Project Brain's coordination state,
   ordered by ACTION VALUE:

       🧠 Project · N decisions awaiting you · M epics in progress ·
          K open · P conversations online

   "decisions awaiting you" comes first because it is the only thing that needs
   the human to act (the Charter human-gate). Each online peer is joined to the
   epic it is *advancing* (summary.peerEpics: convId → epic title) so the bar
   shows "conversation X · «Refactor the parser»", not "(untitled) · generating".

   Clicking the whole bar opens the full three-column Project Brain panel.

   Data path (no raw fetch — §3.2.0):
     • the Board/decision/peer-epic counts come from a cheap one-shot
       Api.project.brainSummary(path) → {epicsOpen, epicsClaimed, epicsDone,
       pendingDecisions, activePeers, peerEpics, charterExists};
     • it is re-fetched (debounced) whenever a 'project' push frame arrives
       (board/charter/feed changed) OR a 'presence' frame arrives (peer joined/
       left). This module keeps the presence mirror only to know WHICH peers
       are live for its own hide/show; the semantic content is the summary.

   This absorbs presence into ONE cross-conversation UI surface + one expandable
   panel — there is no longer a second push subscription / second local mirror
   living in a separate module.

   Concatenated by lib/js_bundler.py AFTER main.js (reads activeConvId /
   getActiveConv at runtime) + push.js + project.js (_getConvProjectPath) +
   i18n.js (t) + project-brain.js (openProjectBrain). Symbols share window scope.
   ═══════════════════════════════════════════════════════════════════ */

(function _wireCollabBar() {
  if (typeof pushSubscribe !== "function") return;
  if (window.__presenceWired) return;
  window.__presenceWired = true;

  // Local mirror of presence peers per root — used ONLY to decide whether any
  // OTHER conversation is live (drives hide/show); the displayed content is the
  // summary. root(abs) → Set<convId> (conversation-level peers, self excluded
  // at render time).
  const _peerConvs = new Map();
  // root(abs) → latest brainSummary object (or null).
  const _summary = new Map();
  let _lastFingerprint = "";
  let _refetchTimer = null;

  function _norm(root) { return String(root || "").replace(/[/\\]+$/, ""); }

  function _displayedRoot() {
    try {
      const conv = (typeof getActiveConv === "function") ? getActiveConv() : null;
      if (!conv) return "";
      const p = (typeof _getConvProjectPath === "function")
        ? _getConvProjectPath(conv) : (conv.projectPath || "");
      return _norm(p);
    } catch (e) { return ""; }
  }

  function _esc(s) {
    return (typeof escapeHtml === "function") ? escapeHtml(String(s == null ? "" : s))
      : String(s == null ? "" : s);
  }

  function _t(key, params, fallback) {
    try { return (typeof t === "function") ? t(key, params) : (fallback || key); }
    catch (e) { return fallback || key; }
  }

  const _BRAIN_SVG = '<svg class="collab-brain-ico" width="14" height="14" viewBox="0 0 24 24" '
    + 'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    + 'stroke-linejoin="round"><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 '
    + '4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 '
    + '2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/></svg>';

  /* Fetch the one-shot summary for a root (debounced), then re-render. */
  function _refetchSummary(root) {
    const api = (typeof Api !== "undefined" && Api.project) ? Api.project : null;
    if (!api || typeof api.brainSummary !== "function" || !root) return;
    Promise.resolve(api.brainSummary(root)).then((s) => {
      _summary.set(root, s || null);
      _render();
    }).catch((e) => {
      if (typeof console !== "undefined") console.debug("[CollabBar] summary fetch failed", e && e.message);
    });
  }

  function _scheduleRefetch(root) {
    if (_refetchTimer) clearTimeout(_refetchTimer);
    _refetchTimer = setTimeout(() => { _refetchTimer = null; _refetchSummary(root); }, 300);
  }

  /* Build the headline segments, ordered by ACTION VALUE. Returns an array of
     {cls, html} segments so the renderer can join them with separators. */
  function _segments(summary, peerCount) {
    const segs = [];
    if (summary) {
      const conflicts = summary.conflicts || 0;
      if (conflicts > 0) {
        // A live file-overlap between 2+ conversations — highest-urgency
        // signal (two conversations are about to step on each other NOW).
        segs.push({ cls: "collab-seg-conflict",
          html: _esc(_t("collab.conflicts", { n: conflicts }, conflicts + " conflict")) });
      }
      const pend = summary.pendingDecisions || 0;
      if (pend > 0) {
        // The only action-needing (approval) segment → emphasised.
        segs.push({ cls: "collab-seg-decisions",
          html: _esc(_t("collab.decisionsAwaiting", { n: pend }, pend + " decisions awaiting you")) });
      }
      const inProg = summary.epicsClaimed || 0;
      if (inProg > 0) {
        segs.push({ cls: "collab-seg-progress",
          html: _esc(_t("collab.epicsInProgress", { n: inProg }, inProg + " in progress")) });
      }
      const open = summary.epicsOpen || 0;
      if (open > 0) {
        segs.push({ cls: "collab-seg-open",
          html: _esc(_t("collab.epicsOpen", { n: open }, open + " open")) });
      }
    }
    if (peerCount > 0) {
      segs.push({ cls: "collab-seg-peers",
        html: _esc(_t("collab.peersOnline", { n: peerCount }, peerCount + " online")) });
    }
    return segs;
  }

  /* Per-peer "advancing «epic»" lines — the deep-collaboration join. Only
     peers that own a live epic get a line; the rest are just in the count. */
  function _peerEpicLines(summary, convSet, selfId) {
    if (!summary || !summary.peerEpics) return [];
    const lines = [];
    for (const cid of convSet) {
      if (selfId && cid === selfId) continue;
      const epic = summary.peerEpics[cid];
      if (!epic) continue;
      lines.push(
        `<span class="collab-peer-epic" data-conv="${_esc(cid)}">`
        + `<span class="collab-peer-dot"></span>`
        + _esc(_t("collab.peerAdvancing", { epic: "" }, "advancing"))
        + ` <span class="collab-epic-title">${_esc(epic)}</span></span>`);
    }
    return lines;
  }

  function _render() {
    const el = document.getElementById("presenceStrip");
    if (!el) return;
    const root = _displayedRoot();

    // Not in project mode → never show (this is a project coordination bar).
    if (!root) {
      if (_lastFingerprint !== "") { el.hidden = true; el.innerHTML = ""; _lastFingerprint = ""; }
      return;
    }

    const selfId = (typeof activeConvId !== "undefined") ? activeConvId : null;
    const convSet = new Set();
    const pm = _peerConvs.get(root);
    if (pm) { for (const cid of pm) { if (cid && cid !== selfId) convSet.add(cid); } }
    const peerCount = convSet.size;
    const summary = _summary.get(root) || null;

    const segs = _segments(summary, peerCount);
    // Nothing collaborative to surface (solo, empty board, no pending) → hide.
    if (segs.length === 0) {
      if (_lastFingerprint !== "") { el.hidden = true; el.innerHTML = ""; _lastFingerprint = ""; }
      return;
    }

    const hasDecisions = summary && (summary.pendingDecisions || 0) > 0;
    const hasConflicts = summary && (summary.conflicts || 0) > 0;
    const segHTML = segs.map(s => `<span class="collab-seg ${s.cls}">${s.html}</span>`)
      .join('<span class="collab-sep">·</span>');
    const epicLines = _peerEpicLines(summary, convSet, selfId);
    const epicHTML = epicLines.length
      ? `<span class="collab-peer-epics">${epicLines.join("")}</span>` : "";
    // Conflict advisory detail lines — each backend-formed message, verbatim.
    const conflictMsgs = (summary && Array.isArray(summary.conflictMessages))
      ? summary.conflictMessages : [];
    const conflictHTML = conflictMsgs.length
      ? `<span class="collab-conflicts">` + conflictMsgs.map(m =>
          `<span class="collab-conflict-line">${_esc(m)}</span>`).join("") + `</span>`
      : "";

    const cls = "collab-bar-inner"
      + (hasConflicts ? " collab-has-conflicts" : "")
      + (hasDecisions ? " collab-has-decisions" : "");
    const html =
      `<button type="button" class="${cls}" `
      + `data-testid="collab-bar" title="${_esc(_t("collab.openBrain", null, "Open Project Brain"))}">`
      + `<span class="collab-brain">${_BRAIN_SVG}</span>`
      + `<span class="collab-label">${_esc(_t("collab.project", null, "Project"))}</span>`
      + `<span class="collab-sep">·</span>`
      + segHTML
      + epicHTML
      + conflictHTML
      + `</button>`;

    const fp = root + "|" + html;
    if (fp === _lastFingerprint) return;
    _lastFingerprint = fp;
    el.innerHTML = html;
    el.hidden = false;
    el.classList.add("collab-bar");

    // Whole bar → open the Project Brain panel.
    const inner = el.querySelector(".collab-bar-inner");
    if (inner) {
      inner.addEventListener("click", () => {
        if (typeof openProjectBrain === "function") openProjectBrain();
      });
    }
  }

  // ── Push subscriptions (ONE place; no second module/mirror) ──

  // 'presence' → maintain the live-peer set (who is online) for this bar's
  // hide/show + which peers to join to epics.
  pushSubscribe("presence", "*", (frame) => {
    try {
      if (!frame || frame.type !== "presence") return;
      const root = _norm(frame.root);
      if (!root) return;
      if (frame.kind === "update" && frame.peer && frame.peer.convId && !frame.peer.agentId) {
        let s = _peerConvs.get(root); if (!s) { s = new Set(); _peerConvs.set(root, s); }
        s.add(frame.peer.convId);
      } else if (frame.kind === "depart" && frame.peer && frame.peer.convId && !frame.peer.agentId) {
        const s = _peerConvs.get(root);
        if (s) { s.delete(frame.peer.convId); if (s.size === 0) _peerConvs.delete(root); }
      } else if (frame.kind === "snapshot" && Array.isArray(frame.peers)) {
        const s = new Set();
        for (const p of frame.peers) { if (p && p.convId && !p.agentId) s.add(p.convId); }
        _peerConvs.set(root, s);
      } else {
        return;
      }
      // A peer change may change the join → refetch the summary for this root.
      if (root === _displayedRoot()) _scheduleRefetch(root);
      _render();
    } catch (e) {
      if (typeof console !== "undefined") console.debug("[CollabBar] presence handler error:", e && e.message);
    }
  });

  // 'project' → the Brain coordination state changed (board claim/complete,
  // charter propose/commit, feed pulse). Refetch the summary for THIS project.
  // The routing key is the same sha1(path)[:16] the panel subscribes on, but
  // this bar subscribes with '*' (it re-derives the displayed root itself) —
  // it only needs to know "something changed", then refetches by explicit path.
  pushSubscribe("project", "*", (frame) => {
    try {
      const root = _displayedRoot();
      if (root) _scheduleRefetch(root);
    } catch (e) { /* noop */ }
  });

  // Periodic tick: picks up a conversation switch (displayed root changes) and
  // refreshes counts. Cheap — fingerprint-gated. NOT a server poll of state,
  // just a summary refetch cadence for the currently-viewed project.
  setInterval(() => {
    try {
      const root = _displayedRoot();
      if (root && !_summary.has(root)) _refetchSummary(root);
      _render();
    } catch (e) { /* noop */ }
  }, 15000);

  // Called by main/loadConversation on conversation switch — re-resolve the
  // displayed root, fetch its summary, re-render immediately.
  window.presenceRefresh = function () {
    const root = _displayedRoot();
    if (root) _refetchSummary(root);
    _render();
  };

  // Test hooks (jsdom): drive a summary + a peer set without the network.
  window.CollabBar = {
    _render: _render,
    _setSummary: (root, s) => { _summary.set(_norm(root), s); },
    _setPeers: (root, convIds) => { _peerConvs.set(_norm(root), new Set(convIds || [])); },
  };

  console.info("[CollabBar] ✓ collaboration bar wired (presence + project channels)");
})();

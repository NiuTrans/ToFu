/* ===== migrated source: presence.js ===== */
/* presence.js — project-only Collaboration Bar.
 * Renders action-ordered attention, epic and online-peer counts and opens the
 * Project Brain panel. Entry: presenceRefresh; test seam: CollabBar.
 * Inputs: project/presence pushes and brainSummary(path). Dependencies: main,
 * push, project, i18n and runtimeScope's panel API. Summary data owns semantics;
 * the local presence mirror only helps visibility. blocking alone is urgent.
 */

(function _wireCollabBar() {
  if (typeof pushSubscribe !== "function") return;
  if (runtimeScope.__presenceWired) return;
  runtimeScope.__presenceWired = true;

  // root(abs) → live conversation IDs; brainSummary owns displayed semantics.
  const _peerConvs = new Map();
  // root(abs) → latest brainSummary.
  const _summary = new Map();
  let _lastFingerprint = "";
  let _refetchTimer = null;

  function _norm(root) { return String(root || "").replace(/[/\\]+$/, ""); }

  function _displayedRoot() {
    try {
      const conv = (typeof getActiveConv === "function") ? getActiveConv() : null;
      let p = "";
      if (conv) {
        p = (typeof _getConvProjectPath === "function")
          ? _getConvProjectPath(conv) : (conv.projectPath || "");
      }
      // New Chat can have an armed project before it has conversation metadata.
      if (!p && typeof projectState !== "undefined" && projectState && projectState.active) {
        p = projectState.path || "";
      }
      return _norm(p);
    } catch (e) { return ""; }
  }

  function _esc(s) {
    return escapeHtml(String(s == null ? "" : s));
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

  // Pass the displayed conversation so activePeers counts only other chats.
  function _refetchSummary(root) {
    const api = (typeof Api !== "undefined" && Api.project) ? Api.project : null;
    if (!api || typeof api.brainSummary !== "function" || !root) return;
    const selfId = (typeof activeConvId !== "undefined") ? activeConvId : "";
    Promise.resolve(api.brainSummary(root, selfId || "")).then((s) => {
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

  // Headline segments are ordered by action value.
  function _segments(summary, peerCount) {
    const segs = [];
    if (summary) {
      const conflicts = summary.conflicts || 0;
      if (conflicts > 0) {
        // Live file overlap is the highest-urgency signal.
        segs.push({ cls: "collab-seg-conflict",
          html: _esc(_t("collab.conflicts", { n: conflicts }, conflicts + " conflict")) });
      }
      const pend = summary.pendingDecisions || 0;
      const needs = summary.needsYou || 0;
      const blocking = summary.blocking || 0;
      if (needs > 0) {
        // Keep the decisions class as a compatibility alias; blocking is urgent.
        segs.push({ cls: 'collab-seg-decisions collab-seg-needsyou' +
            (blocking > 0 ? ' collab-seg-blocking' : ''),
          html: _esc(blocking > 0
            ? _t('collab.needsYouBlocking', { n: needs }, needs + ' need you')
            : _t('collab.needsYou', { n: needs }, needs + ' awaiting you')) });
      } else if (pend > 0) {
        // Older servers expose only the legacy proposal count.
        segs.push({ cls: 'collab-seg-decisions',
          html: _esc(_t('collab.decisionsAwaiting', { n: pend }, pend + ' decisions awaiting you')) });
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

  // Show detail only for peers that own a live epic.
  function _peerEpicLines(summary, convSet, selfId) {
    if (!summary || !summary.peerEpics) return [];
    const lines = [];
    // Union authoritative mappings with just-arrived local presence frames.
    const seen = new Set();
    const ids = [];
    for (const cid of Object.keys(summary.peerEpics)) ids.push(cid);
    for (const cid of convSet) ids.push(cid);
    for (const cid of ids) {
      if (!cid || (selfId && cid === selfId) || seen.has(cid)) continue;
      seen.add(cid);
      const epic = summary.peerEpics[cid];
      if (!epic) continue;
      lines.push(
        `<span class="collab-peer-epic" data-conv="${_esc(cid)}">`
        + `<span class="collab-peer-dot"></span>`
        + _esc(_t("collab.peerAdvancing", undefined, "advancing"))
        + ` <span class="collab-epic-title">${_esc(epic)}</span></span>`);
    }
    return lines;
  }

  function _render() {
    const el = document.getElementById("presenceStrip");
    if (!el) return;
    const root = _displayedRoot();

    // This surface is project-only.
    if (!root) {
      if (_lastFingerprint !== "") { el.hidden = true; el.innerHTML = ""; _lastFingerprint = ""; }
      return;
    }

    const selfId = (typeof activeConvId !== "undefined") ? activeConvId : null;
    const convSet = new Set();
    const pm = _peerConvs.get(root);
    if (pm) { for (const cid of pm) { if (cid && cid !== selfId) convSet.add(cid); } }
    const summary = _summary.get(root) || null;
    // Snapshot is authoritative; max includes peers arriving after the snapshot.
    const backendCount = (summary && typeof summary.activePeers === "number")
      ? summary.activePeers : null;
    const peerCount = (backendCount != null)
      ? Math.max(backendCount, convSet.size) : convSet.size;

    // Rich status remains in Project Brain; this bar leads with a stable label.
    const leadHTML = `<span class="collab-label">${_esc(_t("collab.project", null, "Project"))}</span>`;

    const segs = _segments(summary, peerCount);
    // Preserve the legacy class contract while using the attention count.
    const needsYou = (summary && typeof summary.needsYou === 'number')
      ? summary.needsYou : (summary ? (summary.pendingDecisions || 0) : 0);
    const hasDecisions = needsYou > 0;
    // Only stopped work makes the bar urgent.
    const hasBlocking = !!(summary && (summary.blocking || 0) > 0);
    const hasConflicts = !!(summary && (summary.conflicts || 0) > 0);

    // Hide an empty solo-project bar.
    if (!segs.length) {
      if (_lastFingerprint !== "") { el.hidden = true; el.innerHTML = ""; _lastFingerprint = ""; }
      return;
    }

    const segHTML = segs.map(s => `<span class="collab-seg ${s.cls}">${s.html}</span>`)
      .join('<span class="collab-sep">·</span>');
    const leadSep = segs.length ? `<span class="collab-sep">·</span>` : "";
    const epicLines = _peerEpicLines(summary, convSet, selfId);
    const epicHTML = epicLines.length
      ? `<span class="collab-peer-epics">${epicLines.join("")}</span>` : "";
    // Conflict messages are backend-formed and displayed verbatim.
    const conflictMsgs = (summary && Array.isArray(summary.conflictMessages))
      ? summary.conflictMessages : [];
    const conflictHTML = conflictMsgs.length
      ? `<span class="collab-conflicts">` + conflictMsgs.map(m =>
          `<span class="collab-conflict-line">${_esc(m)}</span>`).join("") + `</span>`
      : "";
    const projectHTML =
      `<span class="collab-cluster collab-cluster-project">`
      + `<span class="collab-brain">${_BRAIN_SVG}</span>`
      + leadHTML
      + leadSep
      + segHTML
      + epicHTML
      + conflictHTML
      + `</span>`;

    const cls = "collab-bar-inner"
      + (hasConflicts ? " collab-has-conflicts" : "")
      + (hasBlocking ? " collab-has-blocking" : "")
      + (hasDecisions ? " collab-has-decisions" : "");
    const html =
      `<button type="button" class="${cls}" `
      + `data-testid="collab-bar" title="${_esc(_t("collab.openBrain", null, "Open Project Brain"))}">`
      + projectHTML
      + `</button>`;

    const fp = root + "|" + html;
    if (fp === _lastFingerprint) return;
    _lastFingerprint = fp;
    el.innerHTML = html;
    el.hidden = false;
    el.classList.add("collab-bar");

    // Attention opens Needs-you; otherwise the panel retains its last tab.
    const inner = el.querySelector(".collab-bar-inner");
    if (inner) {
      inner.addEventListener("click", () => {
        if (typeof runtimeScope.openProjectBrain === "function") runtimeScope.openProjectBrain({ needsYou: needsYou });
      });
    }
  }

  // Presence maintains the live-peer mirror for visibility and epic joins.
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
      if (root === _displayedRoot()) _scheduleRefetch(root);
      _render();
    } catch (e) {
      if (typeof console !== "undefined") console.debug("[CollabBar] presence handler error:", e && e.message);
    }
  });

  // Any project signal triggers an explicit summary fetch for the displayed root.
  pushSubscribe("project", "*", (frame) => {
    try {
      const root = _displayedRoot();
      if (root) _scheduleRefetch(root);
    } catch (e) { /* noop */ }
  });

  // Fingerprint-gated tick notices project switches without polling state.
  setInterval(() => {
    try {
      const root = _displayedRoot();
      if (root && !_summary.has(root)) _refetchSummary(root);
      _render();
    } catch (e) { /* noop */ }
  }, 15000);

  // main/loadConversation calls this after a conversation switch.
  runtimeScope.presenceRefresh = function () {
    const root = _displayedRoot();
    if (root) _refetchSummary(root);
    _render();
  };

  // Test hooks (jsdom): drive a summary + a peer set without the network.
  runtimeScope.CollabBar = {
    _render: _render,
    _setSummary: (root, s) => { _summary.set(_norm(root), s); },
    _setPeers: (root, convIds) => { _peerConvs.set(_norm(root), new Set(convIds || [])); },
  };

  console.info("[CollabBar] ✓ collaboration bar wired (presence + project channels)");
})();

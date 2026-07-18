/* ═══════════════════════════════════════════════════════════════════
   core/debug_panel.js — extracted from core.js (split 2026-05-28)

   Debug panel: debugLog ring buffer, error reporting, toggleDebug, restoreDebugForConv, showMessagesInDebug (393-LOC HTML renderer).

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

function debugLog(msg, type = "") {
  console.log(`[${type || "info"}]`, msg);
  /* Bounded in-memory ring of recent log lines so the one-click diagnostics
   * collector (diag_collect.js → window.__tofuCollectDiagnostics) can attach
   * the last N events even when the SPA is otherwise wedged. Kept here (not in
   * diag_collect) because debugLog is the single choke point every log passes
   * through. Never throws. */
  try {
    const ring = (window.__tofuDiagRing = window.__tofuDiagRing || []);
    ring.push(Date.now() + " [" + (type || "info") + "] " + String(msg).slice(0, 300));
    if (ring.length > 80) ring.splice(0, ring.length - 80);
  } catch (_) { /* ring is best-effort */ }
  /* Auto-report error/warn level messages to server logs */
  if (type === "error" || type === "warn") {
    _reportClientError(`[debugLog][${type}] ${msg}`);
  }
}

/* ── Frontend → Server error reporting ──
 * Fire-and-forget: sends client-side errors to server log files so they
 * appear in logs/app.log alongside backend errors.  Never throws. */
const _reportedErrors = new Set();          /* dedupe within session */
function _reportClientError(message, extra) {
  try {
    /* Deduplicate: don't flood the server with the same error */
    const key = message.slice(0, 200);
    if (_reportedErrors.has(key)) return;
    _reportedErrors.add(key);
    /* Cap the set so it doesn't grow unbounded */
    if (_reportedErrors.size > 200) _reportedErrors.clear();

    const payload = {
      message,
      url: location.href,
      userAgent: navigator.userAgent,
      timestamp: new Date().toISOString(),
      conversationCount: conversations?.length || 0,
    };
    if (extra) payload.extra = extra;
    Api.clientError.report(payload);   /* silently ignore network failures */
  } catch (_) {}          /* never let reporting itself crash */
}

/* ── Global error handler: catch ALL uncaught errors ── */
window.addEventListener("error", (evt) => {
  _reportClientError(`[uncaught] ${evt.message}`, {
    source: evt.filename,
    line: evt.lineno,
    col: evt.colno,
    stack: evt.error?.stack?.slice(0, 1000),
  });
});
window.addEventListener("unhandledrejection", (evt) => {
  const msg = evt.reason?.message || evt.reason || "unknown";
  _reportClientError(`[unhandledRejection] ${msg}`, {
    stack: evt.reason?.stack?.slice(0, 1000),
  });
});
// Per-conversation debug message cache: { convId: { messages, label } }
const _debugCache = {};
function clearDebug() {
  document.getElementById("debugContent").innerHTML = "";
  document.getElementById("debugTitle").innerHTML = Icon('inbox', 14) + ' Messages';
  const p = document.getElementById("debugContent");
  if (p) p._rawMessages = null;
}
/* ── Debug-panel helpers (pure) ────────────────────────────────────────
 * Shared between full + incremental render paths and the header
 * summary. Keep them outside showMessagesInDebug so we don't re-allocate
 * on every snapshot. */
function _debugMsgChars(msg) {
  if (!msg) return 0;
  if (typeof msg.content === "string") return msg.content.length;
  if (Array.isArray(msg.content)) {
    let n = 0;
    for (const b of msg.content) {
      if (b && typeof b === "object") {
        if (b.type === "text") n += (b.text || "").length;
        else if (b.type === "image_url")
          n += (b.image_url && b.image_url.url ? b.image_url.url.length : 0);
      }
    }
    return n;
  }
  return 0;
}
/* Rough token estimate: 1 token ≈ 3.5 chars for English/code, 1 char for
 * CJK. We don't bother detecting CJK here — the panel is diagnostic, not
 * billing. Tool_calls' arguments are JSON strings; count them too. */
function _debugMsgTokens(msg) {
  if (!msg) return 0;
  let chars = _debugMsgChars(msg);
  if (Array.isArray(msg.tool_calls)) {
    for (const tc of msg.tool_calls) {
      const args = tc && tc.function && tc.function.arguments;
      if (typeof args === "string") chars += args.length;
      else if (args) chars += JSON.stringify(args).length;
    }
  }
  return Math.max(1, Math.round(chars / 3.5));
}
/* Detect whether a tool message holds compacted content. Two paths:
 * 1. Explicit ``_compactionLayer`` patch from the tool_compacted SSE
 *    handler in ui.js (most reliable, includes from→to chars).
 * 2. Content sniff — server-emitted ``messages_snapshot`` does NOT
 *    carry per-message metadata, so we recognize the placeholder
 *    pattern produced by lib/tasks_pkg/compaction.py. */
function _debugCompactionInfo(msg) {
  if (!msg) return null;
  if (msg._compactionLayer) {
    return {
      layer: msg._compactionLayer,
      from: msg._compactedFromChars,
      to: msg._compactedToChars,
    };
  }
  if (msg.role !== "tool") return null;
  const c = typeof msg.content === "string" ? msg.content : "";
  if (!c) return null;
  // Match the placeholder shapes emitted by compaction.py / tool_dispatch.py.
  // Examples:
  //   [grep_search result compacted — was 41,234 chars …]
  //   [list_dir result compacted — had 3 image(s) …]
  //   [tool result truncated — was 80,000 chars …]
  const m = c.match(/^\[[^\]]*\b(?:compacted|truncated)\b[^\]]*\bwas\s+([\d,]+)\s+chars/i);
  if (m) {
    const from = parseInt(m[1].replace(/,/g, ""), 10);
    return { layer: "L?", from, to: c.length };
  }
  if (/^\[[^\]]*\bcompacted\b/.test(c) || /^\[Persisted to:/.test(c)) {
    return { layer: "L?", from: null, to: c.length };
  }
  return null;
}
function _fmtKB(n) {
  if (n == null) return "?";
  if (n < 1024) return n + "B";
  return (n / 1024).toFixed(1) + "KB";
}
/* ── Project-Brain injection sniff (observability of the "brain") ──
 * The AUTHORITATIVE signal that this task injected the project charter /
 * board is the exact marker string the MODEL actually saw in the wire-form
 * `messages` snapshot: `[PROJECT CHARTER]` / `[PROJECT BOARD]`. We sniff
 * ONLY those markers in the message content — no separate frontend heuristic,
 * no state reverse-engineering. Returns e.g. {charter:true, board:false} or
 * null when neither is present. `_debugMsgText` flattens string|array content
 * (system blocks are commonly wrapped as an array of text blocks). */
function _debugMsgText(msg) {
  if (!msg) return "";
  if (typeof msg.content === "string") return msg.content;
  if (Array.isArray(msg.content)) {
    let s = "";
    for (const b of msg.content) {
      if (b && typeof b === "object" && b.type === "text") s += (b.text || "") + "\n";
    }
    return s;
  }
  return "";
}
function _debugBrainInfo(msg) {
  if (!msg) return null;
  // Only system messages carry the injected charter/board blocks.
  if (msg.role !== "system") return null;
  const text = _debugMsgText(msg);
  if (!text) return null;
  const charter = text.indexOf("[PROJECT CHARTER]") !== -1;
  const board = text.indexOf("[PROJECT BOARD]") !== -1;
  if (!charter && !board) return null;
  return { charter, board };
}
/* Stable per-message identity for open-state preservation across a re-render.
 * A positional index is NOT stable: a `messages_snapshot` reflecting a
 * compaction/reconcile can DROP or REORDER an earlier message, so index N
 * after the render is a different message than the one the user expanded —
 * the same drift class the mutation paths (regenerate/patch/delete) were
 * hardened against by resolving on a stable id. Prefer an explicit id
 * (`tool_call_id` / assistant `tool_calls[].id` / `_msgId` if present), else
 * fall back to role + a cheap content signature (djb2-ish, base36) which is
 * stable as long as the message's OWN content is unchanged. Two byte-identical
 * messages share a key — a benign over-restore (identical content). */
function _debugMsgIdentity(msg) {
  if (!msg) return "";
  if (msg._msgId) return "m:" + msg._msgId;
  if (msg.tool_call_id) return "tc:" + msg.tool_call_id;
  if (Array.isArray(msg.tool_calls) && msg.tool_calls.length) {
    const id = msg.tool_calls[0] && msg.tool_calls[0].id;
    if (id) return "tcall:" + id;
  }
  const role = msg.role || "unknown";
  const text = _debugMsgText(msg);
  let h = 0;
  for (let k = 0; k < text.length; k++) h = (Math.imul(h, 31) + text.charCodeAt(k)) | 0;
  return "r:" + role + ":" + text.length + ":" + (h >>> 0).toString(36);
}
function toggleDebug() {
  debugVisible = !debugVisible;
  document
    .getElementById("debugPanel")
    .classList.toggle("visible", debugVisible);
  // ★ Single source of truth: whenever the panel is opened, (re)load the
  //   active conversation's messages from the backend. A cold page refresh
  //   restores the active conv via renderChat (not loadConversation), so
  //   restoreDebugForConv never fired and the panel showed empty until the
  //   next generation. Loading on open guarantees fresh, backend-built content.
  if (debugVisible
      && typeof activeConvId !== "undefined" && activeConvId
      && typeof restoreDebugForConv === "function") {
    restoreDebugForConv(activeConvId);
  }
}
// Close the debug panel (top-right ✕). Distinct from clearDebug(), which only
// wipes content — the ✕ must actually hide the panel.
function closeDebug() {
  debugVisible = false;
  const panel = document.getElementById("debugPanel");
  if (panel) panel.classList.remove("visible");
}
// Called on conversation switch: restore cached debug for this conv.
//
// Source-of-truth order:
//   1. In-memory cache (most recent messages_snapshot from a streaming task).
//   2. Server-side `/api/conversations/<id>/debug-messages` — rebuilds the
//      api-form messages from the DB via build_api_messages_from_db(), so
//      it works for: (a) cold reload after server restart, (b) shell convs
//      whose `messages` array hasn't been loaded yet (`_needsLoad=true`),
//      and (c) cross-device viewing.
//
// The previous gate `conv.messages.length > 0` blocked case (b) entirely
// — newly switched-to old conversations showed an empty debug panel until
// the user sent a message and the streaming pipeline emitted a snapshot.
function restoreDebugForConv(convId) {
  const cached = _debugCache[convId];
  if (cached && cached.messages && cached.messages.length > 0) {
    showMessagesInDebug(cached.messages, cached.label, false, undefined, cached.tools, cached.approx);
    return;
  }
  const conv = conversations.find((c) => c.id === convId);
  // Decide if there's anything worth fetching. For shell convs this is
  // _serverMsgCount > 0 even when messages.length === 0.
  const _hasServerMsgs = conv && (
    (conv.messages && conv.messages.length > 0) ||
    (conv._serverMsgCount || 0) > 0 ||
    !!conv._needsLoad
  );
  if (!_hasServerMsgs) {
    clearDebug();
    return;
  }
  // Show a tiny placeholder so the user knows we're fetching, instead of
  // an empty panel that looks like "nothing here".
  const _ph = document.getElementById("debugContent");
  const _title = document.getElementById("debugTitle");
  if (_ph) _ph.innerHTML = '<div class="debug-loading">Loading messages from server…</div>';
  if (_title) _title.innerHTML = Icon('inbox', 14) + ' Messages (loading…)';
  const _sp = (typeof config !== 'undefined' && config.systemPrompt) || '';
  Api.conversations.getDebugMessages(convId, _sp)
    .then(data => {
      // The user may have switched away while the fetch was in flight.
      if (typeof activeConvId !== "undefined" && convId !== activeConvId) return;
      if (data && data.messages && data.messages.length > 0) {
        showMessagesInDebug(
          data.messages,
          `${data.count} msgs (server)`,
          false,
          convId,
          undefined,
          !!data.approx,
        );
      } else {
        clearDebug();
      }
    })
    .catch((e) => {
      console.warn("[debug-panel] /debug-messages fetch failed:", e);
      _reportClientError(`[debug-panel] fetch failed: ${e && e.message || e}`);
      if (typeof activeConvId !== "undefined" && convId === activeConvId) clearDebug();
    });
}
// ★ Render full messages array into debug panel — supports incremental updates
//   isUpdate=true → streaming update, preserve collapse states, only patch changed blocks
//   approx=true → COLD-path reconstruction (the /debug-messages endpoint, which
//     rebuilds the wire form from the DB with a hypothetical first-round for the
//     per-round memory/date). Renders the amber "reconstructed approximation"
//     chip so the human knows they are NOT looking at a precise capture of a
//     specific round. The live SSE snapshot path (the real wire form) passes
//     approx=false/undefined and must NEVER show this chip.
function showMessagesInDebug(messages, label, isUpdate, forConvId, tools, approx) {
  const cid =
    forConvId || (typeof activeConvId !== "undefined" ? activeConvId : null);
  // Cache for conversation switching
  if (cid) {
    _debugCache[cid] = { messages, label };
    if (tools) _debugCache[cid].tools = tools;
    _debugCache[cid].approx = !!approx;
  }
  // Only render if this conv is currently active (or no conv specified)
  if (
    forConvId &&
    typeof activeConvId !== "undefined" &&
    forConvId !== activeConvId
  )
    return;
  const p = document.getElementById("debugContent");
  if (!p) return;
  /* ── Preserve the user's expanded state + scroll across a re-render ──
   * The incremental update path keeps `.open` blocks, but the structural
   * fall-through to a FULL render (`p.innerHTML = ""` below) would otherwise
   * collapse every message the user expanded to inspect and jump the scroll to
   * the top — the reported "debug panel closes itself when new content streams
   * in" bug (a snapshot update whose message count/roles diverge enough trips
   * the fall-through, which happens routinely as a new generation's live wire
   * snapshot grows past the initial server-reconstructed one). Capture the
   * open block IDENTITIES + tools-open + scroll now, re-apply after the full
   * render. Identity (not index) is the handle so restoration survives a
   * snapshot that drops/reorders an earlier message. */
  const _openMids = new Set();
  p.querySelectorAll(".debug-msg-block.open").forEach((b) => {
    // Tools block shares `.debug-msg-block` but has no data-mid — handled
    // separately via _toolsWasOpen, so the mid guard cleanly excludes it.
    if (b.dataset.mid) _openMids.add(b.dataset.mid);
  });
  const _toolsWasOpen = !!p.querySelector(".debug-tools-block.open");
  const _hadExisting = p.querySelectorAll(".debug-msg-block").length > 0;
  const _prevScroll = p.scrollTop;
  /* ── Aggregate stats for the header summary ── */
  let _totalTokens = 0;
  let _compactedCount = 0;
  let _toolMsgCount = 0;
  let _brainCharter = false;
  let _brainBoard = false;
  for (const m of messages) {
    _totalTokens += _debugMsgTokens(m);
    if (m && m.role === "tool") {
      _toolMsgCount++;
      if (_debugCompactionInfo(m)) _compactedCount++;
    }
    const bi = _debugBrainInfo(m);
    if (bi) { _brainCharter = _brainCharter || bi.charter; _brainBoard = _brainBoard || bi.board; }
  }
  const title = document.getElementById("debugTitle");
  if (title) {
    const toolsSuffix = tools && tools.length > 0 ? ` · ${Icon('wrench', 11)}${tools.length}` : '';
    const compactedSuffix = _compactedCount > 0
      ? ` · ${Icon('archive', 11)}${_compactedCount}/${_toolMsgCount}` : '';
    const tokSuffix = _totalTokens > 0
      ? ` · ~${_totalTokens >= 1000
          ? (_totalTokens / 1000).toFixed(1) + 'K'
          : _totalTokens}tok` : '';
    /* Project-Brain injection counter — a 🧠 (SVG, §3.4) tally of which brain
     * blocks the model saw this task, sniffed from the authoritative markers. */
    let brainSuffix = '';
    if (_brainCharter || _brainBoard) {
      const parts = [];
      if (_brainCharter) parts.push(t('debug.brainCharter'));
      if (_brainBoard) parts.push(t('debug.brainBoard'));
      brainSuffix =
        ` · <span class="debug-brain-summary" title="${escapeHtml(t('debug.brainSummaryTitle'))}">` +
        `${Icon('brain', 11)} ${escapeHtml(parts.join('/'))}</span>`;
    }
    title.innerHTML = `${Icon('inbox', 14)} Messages (${messages.length})${toolsSuffix}${compactedSuffix}${brainSuffix}${tokSuffix}${label ? " — " + escapeHtml(String(label)) : ""}`;
  }
  /* ── Amber "reconstructed approximation" chip (cold path only) ──
   * Gated STRICTLY on the endpoint's approx flag, never on the panel in
   * general — the live SSE snapshot is the real wire form and must show no
   * chip. Discloses the two cold-path approximations the human can't see
   * otherwise: (a) memory/date are a hypothetical first-round, (b)
   * transport-layer transforms are not expanded. SVG glyph only (§3.4). */
  {
    const _panel = document.getElementById("debugContent");
    let _chip = _panel ? _panel.parentNode.querySelector(".debug-approx-chip") : null;
    if (approx && _panel) {
      if (!_chip) {
        _chip = document.createElement("div");
        _chip.className = "debug-approx-chip";
        _panel.parentNode.insertBefore(_chip, _panel);
      }
      _chip.innerHTML =
        `<div class="debug-approx-head">${Icon('alertTriangle', 13)} ` +
        `${escapeHtml(t('debug.approxTitle'))}</div>` +
        `<ul class="debug-approx-list">` +
        `<li>${escapeHtml(t('debug.approxMemDate'))}</li>` +
        `<li>${escapeHtml(t('debug.approxTransport'))}</li>` +
        `</ul>`;
    } else if (_chip) {
      _chip.remove();
    }
  }
  // Helper: syntax-color JSON (full, no truncation)
  function colorJson(obj, depth) {
    if (depth === undefined) depth = 0;
    const indent = "  ".repeat(depth);
    if (obj === null) return '<span class="debug-null">null</span>';
    if (obj === undefined) return '<span class="debug-null">undefined</span>';
    if (typeof obj === "number") return `<span class="debug-num">${obj}</span>`;
    if (typeof obj === "boolean")
      return `<span class="debug-num">${obj}</span>`;
    if (typeof obj === "string") {
      const escaped = obj
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
      return `<span class="debug-str">"${escaped}"</span>`;
    }
    if (Array.isArray(obj)) {
      if (obj.length === 0) return "[]";
      let items = obj.map((v) => indent + "  " + colorJson(v, depth + 1));
      return "[\n" + items.join(",\n") + "\n" + indent + "]";
    }
    if (typeof obj === "object") {
      const keys = Object.keys(obj);
      if (keys.length === 0) return "{}";
      let lines = keys.map(
        (k) =>
          indent +
          "  " +
          '<span class="debug-key">"' +
          k +
          '"</span>: ' +
          colorJson(obj[k], depth + 1),
      );
      return "{\n" + lines.join(",\n") + "\n" + indent + "}";
    }
    return String(obj);
  }
  // Build summary text for a message
  function msgSummary(msg, i) {
    const parts = ["#" + (i + 1)];
    const chars = _debugMsgChars(msg);
    if (typeof msg.content === "string") {
      parts.push(_fmtKB(chars));
    } else if (Array.isArray(msg.content)) {
      parts.push(msg.content.length + " blocks · " + _fmtKB(chars));
    }
    const tok = _debugMsgTokens(msg);
    if (tok > 0) parts.push("~" + (tok >= 1000 ? (tok / 1000).toFixed(1) + "K" : tok) + "tok");
    if (msg.tool_calls) parts.push(msg.tool_calls.length + " tool_calls");
    if (msg.name) parts.push("fn:" + msg.name);
    if (msg.tool_call_id) {
      // Truncate long IDs — full one is in the body JSON anyway
      const tc = msg.tool_call_id;
      parts.push("tc:" + (tc.length > 12 ? tc.slice(0, 8) + "…" + tc.slice(-4) : tc));
    }
    return parts.join(" · ");
  }
  // Build one block DOM element
  function createBlock(msg, i) {
    const role = msg.role || "unknown";
    const block = document.createElement("div");
    block.className = "debug-msg-block";
    block.dataset.idx = i;
    block.dataset.mid = _debugMsgIdentity(msg);
    const compInfo = _debugCompactionInfo(msg);
    if (compInfo) block.classList.add("debug-msg-compacted");
    // Header
    const header = document.createElement("div");
    header.className = "debug-msg-header";
    const roleSpan = document.createElement("span");
    roleSpan.className = "role-" + role;
    roleSpan.textContent = role.toUpperCase();
    header.appendChild(roleSpan);
    if (compInfo) {
      const badge = document.createElement("span");
      badge.className = "debug-compact-badge";
      const fromKB = compInfo.from != null ? _fmtKB(compInfo.from) : "?";
      const toKB = compInfo.to != null ? _fmtKB(compInfo.to) : "?";
      badge.innerHTML = `${Icon('archive', 11)} ${escapeHtml(compInfo.layer)} ${fromKB}→${toKB}`;
      badge.title = `Tool result compacted (${compInfo.layer}) — original ${fromKB}, now ${toKB}`;
      header.appendChild(badge);
    }
    // Project-Brain injection badge — sniffed from the authoritative markers
    // the model actually saw. Names which brain blocks this system msg carries.
    const brainInfo = _debugBrainInfo(msg);
    if (brainInfo) {
      block.classList.add("debug-msg-brain");
      const bParts = [];
      if (brainInfo.charter) bParts.push(t('debug.brainCharter'));
      if (brainInfo.board) bParts.push(t('debug.brainBoard'));
      const bBadge = document.createElement("span");
      bBadge.className = "debug-brain-badge";
      bBadge.innerHTML = `${Icon('brain', 11)} ${escapeHtml(bParts.join('/'))}`;
      bBadge.title = t('debug.brainBadgeTitle');
      header.appendChild(bBadge);
    }
    const summary = document.createElement("span");
    summary.className = "debug-msg-summary";
    summary.textContent = msgSummary(msg, i);
    header.appendChild(summary);
    const arrow = document.createElement("span");
    arrow.textContent = "▶";
    arrow.style.cssText =
      "font-size:9px;transition:transform 0.2s;color:var(--text-tertiary)";
    header.appendChild(arrow);
    // Store msg ref on block element so incremental updates can swap it
    block._msgRef = msg;
    header.onclick = () => {
      const isOpen = block.classList.toggle("open");
      arrow.style.transform = isOpen ? "rotate(90deg)" : "";
      // Lazy-render body content on first open
      // ★ FIX: use block._msgRef (updated by incremental path) instead of
      //   the closure-captured 'msg' which goes stale after server snapshots.
      const body = block.querySelector(".debug-msg-body");
      if (isOpen && body && !body.dataset.rendered) {
        body.dataset.rendered = "1";
        const pre = body.querySelector("pre");
        if (pre) pre.innerHTML = colorJson(block._msgRef, 0);
      }
    };
    block.appendChild(header);
    // Tool calls quick view
    if (msg.tool_calls && msg.tool_calls.length > 0) {
      const tcDiv = document.createElement("div");
      tcDiv.className = "debug-tool-calls";
      tcDiv.innerHTML =
        Icon('wrench', 12) + ' ' +
        escapeHtml(msg.tool_calls
          .map((tc) => (tc.function ? tc.function.name : "?"))
          .join(", "));
      block.appendChild(tcDiv);
    }
    // Body (collapsed, lazy-rendered)
    const body = document.createElement("div");
    body.className = "debug-msg-body";
    const pre = document.createElement("pre");
    body.appendChild(pre);
    block.appendChild(body);
    return block;
  }
  // Generate a fingerprint for a message to detect changes.
  // Includes a compaction marker so a tool_compacted patch (which only
  // mutates content + sets _compactionLayer) reliably triggers re-render
  // in the incremental update path.
  function msgFingerprint(msg) {
    const role = msg.role || "";
    let size = 0;
    if (typeof msg.content === "string") size = msg.content.length;
    else if (Array.isArray(msg.content)) size = msg.content.length;
    const tcs = msg.tool_calls ? msg.tool_calls.length : 0;
    const tcid = msg.tool_call_id || "";
    const ci = _debugCompactionInfo(msg);
    const cm = ci ? `c:${ci.layer}:${ci.from || 0}:${ci.to || 0}` : "";
    return role + "|" + size + "|" + tcs + "|" + tcid + "|" + cm;
  }
  // --- Incremental update path ---
  // ★ FIX: detect when incremental update is not appropriate and fall back to full render
  //   e.g. when message structure changes drastically (server snapshot replaces client-side build)
  if (isUpdate) {
    const existing = p.querySelectorAll(".debug-msg-block");
    const existingCount = existing.length;
    const newCount = messages.length;
    // If roles of overlapping prefix diverge too much, fall through to full render
    let roleMismatches = 0;
    const overlapLen = Math.min(existingCount, newCount);
    for (let i = 0; i < overlapLen; i++) {
      const rs = existing[i].querySelector(
        ".debug-msg-header span:first-child",
      );
      const existingRole = rs ? rs.textContent.toLowerCase() : "";
      const newRole = messages[i].role || "unknown";
      if (existingRole !== newRole) roleMismatches++;
    }
    if (
      roleMismatches > 1 ||
      (existingCount > 0 && Math.abs(newCount - existingCount) > existingCount)
    ) {
      // Too many mismatches — do a full re-render instead
      isUpdate = false;
    }
  }
  if (isUpdate) {
    const existing = p.querySelectorAll(".debug-msg-block");
    const existingCount = existing.length;
    const newCount = messages.length;
    // Update existing blocks that changed (by fingerprint)
    for (let i = 0; i < Math.min(existingCount, newCount); i++) {
      const oldFp = existing[i].dataset.fp || "";
      const newFp = msgFingerprint(messages[i]);
      if (oldFp !== newFp) {
        // Content changed - update role, summary, invalidate body if it was rendered
        existing[i].dataset.fp = newFp;
        // ★ FIX: Update role label and class when role changes
        const newRole = messages[i].role || "unknown";
        const roleSpan = existing[i].querySelector(
          ".debug-msg-header span:first-child",
        );
        if (roleSpan) {
          const oldRole = roleSpan.textContent.toLowerCase();
          if (oldRole !== newRole) {
            roleSpan.className = "role-" + newRole;
            roleSpan.textContent = newRole.toUpperCase();
          }
        }
        // ── Compaction badge (re-sync with current state) ──
        const newCompInfo = _debugCompactionInfo(messages[i]);
        existing[i].classList.toggle("debug-msg-compacted", !!newCompInfo);
        let badge = existing[i].querySelector(".debug-compact-badge");
        if (newCompInfo) {
          const fromKB = newCompInfo.from != null ? _fmtKB(newCompInfo.from) : "?";
          const toKB = newCompInfo.to != null ? _fmtKB(newCompInfo.to) : "?";
          const text = `${Icon('archive', 11)} ${escapeHtml(newCompInfo.layer)} ${fromKB}→${toKB}`;
          if (!badge) {
            badge = document.createElement("span");
            badge.className = "debug-compact-badge";
            // Insert AFTER role span, BEFORE summary span
            const hdr = existing[i].querySelector(".debug-msg-header");
            const sumEl = hdr.querySelector(".debug-msg-summary");
            hdr.insertBefore(badge, sumEl);
          }
          badge.innerHTML = text;
          badge.title = `Tool result compacted (${newCompInfo.layer}) — original ${fromKB}, now ${toKB}`;
        } else if (badge) {
          badge.remove();
        }
        const sum = existing[i].querySelector(".debug-msg-summary");
        if (sum) sum.textContent = msgSummary(messages[i], i);
        const body = existing[i].querySelector(".debug-msg-body");
        if (body && body.dataset.rendered) {
          body.dataset.rendered = "";
          // Re-render if currently open
          if (existing[i].classList.contains("open")) {
            body.dataset.rendered = "1";
            const pre = body.querySelector("pre");
            if (pre) pre.innerHTML = colorJson(messages[i], 0);
          }
        }
        // Update stored msg ref for lazy render
        existing[i]._msgRef = messages[i];
        // Refresh identity so capture/restore keys track the new content.
        existing[i].dataset.mid = _debugMsgIdentity(messages[i]);
        // Update tool calls quick view
        const oldTc = existing[i].querySelector(".debug-tool-calls");
        if (messages[i].tool_calls && messages[i].tool_calls.length > 0) {
          const tcText =
            Icon('wrench', 12) + ' ' +
            escapeHtml(messages[i].tool_calls
              .map((tc) => (tc.function ? tc.function.name : "?"))
              .join(", "));
          if (oldTc) {
            oldTc.innerHTML = tcText;
          } else {
            const tcDiv = document.createElement("div");
            tcDiv.className = "debug-tool-calls";
            tcDiv.innerHTML = tcText;
            const body2 = existing[i].querySelector(".debug-msg-body");
            existing[i].insertBefore(tcDiv, body2);
          }
        } else if (oldTc) {
          oldTc.remove();
        }
      }
    }
    // Remove extra blocks
    for (let i = existingCount - 1; i >= newCount; i--) {
      existing[i].remove();
    }
    // Append new blocks
    for (let i = existingCount; i < newCount; i++) {
      const block = createBlock(messages[i], i);
      block.dataset.fp = msgFingerprint(messages[i]);
      block._msgRef = messages[i];
      // Rebind lazy render to use _msgRef
      const hdr = block.querySelector(".debug-msg-header");
      hdr.onclick = (function (b, idx) {
        return function () {
          const isOpen = b.classList.toggle("open");
          b.querySelector(".debug-msg-header span:last-child").style.transform =
            isOpen ? "rotate(90deg)" : "";
          const body = b.querySelector(".debug-msg-body");
          if (isOpen && body && !body.dataset.rendered) {
            body.dataset.rendered = "1";
            const pre = body.querySelector("pre");
            if (pre) pre.innerHTML = colorJson(b._msgRef || messages[idx], 0);
          }
        };
      })(block, i);
      p.appendChild(block);
    }
  } else {
    // --- Full render path (initial) ---
    p.innerHTML = "";
    messages.forEach((msg, i) => {
      const block = createBlock(msg, i);
      block.dataset.fp = msgFingerprint(msg);
      block._msgRef = msg;
      // Rebind lazy render to use _msgRef
      const hdr = block.querySelector(".debug-msg-header");
      hdr.onclick = (function (b, idx) {
        return function () {
          const isOpen = b.classList.toggle("open");
          b.querySelector(".debug-msg-header span:last-child").style.transform =
            isOpen ? "rotate(90deg)" : "";
          const body = b.querySelector(".debug-msg-body");
          if (isOpen && body && !body.dataset.rendered) {
            body.dataset.rendered = "1";
            const pre = body.querySelector("pre");
            if (pre) pre.innerHTML = colorJson(b._msgRef, 0);
          }
        };
      })(block, i);
      p.appendChild(block);
    });
    // Re-apply the expanded state captured before the wipe so a snapshot
    // update that fell through to this full render doesn't collapse what the
    // user expanded to inspect. Match by stable IDENTITY (data-mid), iterating
    // freshly-rendered blocks — so if the snapshot dropped/reordered a message,
    // the block the user opened re-opens wherever it now sits, and a different
    // message that happens to land at the old index does NOT.
    if (_openMids.size) {
      p.querySelectorAll(".debug-msg-block").forEach((b) => {
        if (!b.dataset.mid || !_openMids.has(b.dataset.mid)) return;
        if (b.classList.contains("open")) return;
        b.classList.add("open");
        const arrow = b.querySelector(".debug-msg-header span:last-child");
        if (arrow) arrow.style.transform = "rotate(90deg)";
        const body = b.querySelector(".debug-msg-body");
        if (body && !body.dataset.rendered) {
          body.dataset.rendered = "1";
          const pre = body.querySelector("pre");
          if (pre) pre.innerHTML = colorJson(b._msgRef, 0);
        }
      });
    }
    // Only snap to top on a genuine first render — preserve the user's scroll
    // position when this was a re-render over existing content.
    p.scrollTop = _hadExisting ? _prevScroll : 0;
  }
  // ★ Render tools section (collapsible, before messages)
  if (tools && tools.length > 0) {
    let toolsBlock = p.querySelector('.debug-tools-block');
    if (!toolsBlock) {
      toolsBlock = document.createElement('div');
      toolsBlock.className = 'debug-tools-block debug-msg-block';
      const tHeader = document.createElement('div');
      tHeader.className = 'debug-msg-header';
      const tRole = document.createElement('span');
      tRole.className = 'role-tools';
      tRole.innerHTML = Icon('wrench', 12) + ' TOOLS';
      tHeader.appendChild(tRole);
      const tSummary = document.createElement('span');
      tSummary.className = 'debug-msg-summary';
      tHeader.appendChild(tSummary);
      const tArrow = document.createElement('span');
      tArrow.textContent = '▶';
      tArrow.style.cssText = 'font-size:9px;transition:transform 0.2s;color:var(--text-tertiary)';
      tHeader.appendChild(tArrow);
      const tBody = document.createElement('div');
      tBody.className = 'debug-msg-body';
      const tPre = document.createElement('pre');
      tBody.appendChild(tPre);
      tHeader.onclick = () => {
        const isOpen = toolsBlock.classList.toggle('open');
        tArrow.style.transform = isOpen ? 'rotate(90deg)' : '';
        if (isOpen && !tBody.dataset.rendered) {
          tBody.dataset.rendered = '1';
          tPre.innerHTML = colorJson(toolsBlock._toolsRef, 0);
        }
      };
      toolsBlock.appendChild(tHeader);
      toolsBlock.appendChild(tBody);
      p.insertBefore(toolsBlock, p.firstChild);
    }
    // Update summary and ref
    const names = tools.map(t => (t.function ? t.function.name : '?'));
    const tSum = toolsBlock.querySelector('.debug-msg-summary');
    if (tSum) tSum.textContent = `${tools.length} tools: ${names.join(', ')}`;
    toolsBlock._toolsRef = tools;
    // Invalidate body if open
    const tBody = toolsBlock.querySelector('.debug-msg-body');
    if (tBody && tBody.dataset.rendered && toolsBlock.classList.contains('open')) {
      tBody.dataset.rendered = '1';
      const tPre = tBody.querySelector('pre');
      if (tPre) tPre.innerHTML = colorJson(tools, 0);
    } else if (tBody) {
      tBody.dataset.rendered = '';
    }
  }
  // Re-apply the TOOLS block's expanded state — a full render wipes it too
  // (it re-creates collapsed), so restore it alongside the message blocks.
  if (_toolsWasOpen) {
    const _tb = p.querySelector(".debug-tools-block");
    if (_tb && !_tb.classList.contains("open")) {
      _tb.classList.add("open");
      const _ta = _tb.querySelector(".debug-msg-header span:last-child");
      if (_ta) _ta.style.transform = "rotate(90deg)";
      const _tbody = _tb.querySelector(".debug-msg-body");
      if (_tbody && !_tbody.dataset.rendered && _tb._toolsRef) {
        _tbody.dataset.rendered = "1";
        const _tpre = _tbody.querySelector("pre");
        if (_tpre) _tpre.innerHTML = colorJson(_tb._toolsRef, 0);
      }
    }
  }
  // Store for copy
  p._rawMessages = messages;
  p._rawTools = tools || null;
}
/* ── Safe clipboard helper: works on HTTP (non-secure) contexts ── */
function _safeClipboardWrite(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  // Fallback for non-HTTPS (navigator.clipboard is undefined)
  return new Promise((resolve, reject) => {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;opacity:0;left:-9999px';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      resolve();
    } catch (e) { reject(e); }
  });
}
function copyDebugContent() {
  const p = document.getElementById("debugContent");
  if (!p) return;
  const msgs = p._rawMessages;
  if (msgs) {
    const payload = { messages: msgs };
    if (p._rawTools) payload.tools = p._rawTools;
    const text = JSON.stringify(payload, null, 2);
    _safeClipboardWrite(text).then(() => {
      const btn = document.getElementById("debugCopyBtn");
      if (btn) {
        btn.innerHTML = Icon('check', 13);
        setTimeout(() => (btn.innerHTML = Icon('clipboard', 13)), 1500);
      }
    });
  }
}


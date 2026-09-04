// @ts-check
/* Generated lazy retained runtime: diagnostics-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { t } from '../i18n/index';
import { escapeHtml } from '../html-safety';

const Api = runtimeScope.Api;
if (!Api || typeof Api !== 'object') throw new Error('diagnostics-presenters runtime dependency is unavailable: Api');
const DebugShellState = runtimeScope.DebugShellState;
if (!DebugShellState || typeof DebugShellState !== 'object') throw new Error('diagnostics-presenters runtime dependency is unavailable: DebugShellState');
const Icon = runtimeScope.Icon;
if (typeof Icon !== 'function') throw new Error('diagnostics-presenters runtime dependency is unavailable: Icon');
const _findRenderedNativeTurnNode = runtimeScope._findRenderedNativeTurnNode;
if (typeof _findRenderedNativeTurnNode !== 'function') throw new Error('diagnostics-presenters runtime dependency is unavailable: _findRenderedNativeTurnNode');
const _safeClipboardWrite = runtimeScope._safeClipboardWrite;
if (typeof _safeClipboardWrite !== 'function') throw new Error('diagnostics-presenters runtime dependency is unavailable: _safeClipboardWrite');
/* ===== migrated source: core/debug_panel.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/debug_panel.js — extracted from core.js (split 2026-05-28)

   Demand-loaded debug message renderer and Request Inspector adapter.

   This file is concatenated by Vite's module graph AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

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
  /* The floating fallback is retired. Both functions arrive in the same
   * required feature chunk, so a partial diagnostics owner cannot exist. */
  toggleRequestInspector();
}
// Close the debug panel (top-right ✕). Distinct from clearDebug(), which only
// wipes content — the ✕ must actually hide the panel.
function closeDebug() {
  closeRequestInspector();
}
// Called on conversation switch: restore cached debug for this conv.
//
// Source-of-truth order:
//   1. In-memory cache (most recent messages_snapshot from a streaming task).
//   2. Server-side `/api/conversations/<id>/debug-messages` — rebuilds the
//      api-form messages from the DB via build_api_messages_from_db(), so
//      it works for: (a) cold reload after server restart, (b) shell convs
//      whose Turn snapshot hasn't been loaded yet (`_turnSnapshotRequired=true`),
//      and (c) cross-device viewing.
//
// A transcript-array gate previously blocked case (b) entirely
// — newly switched-to old conversations showed an empty debug panel until
// the user sent a message and the streaming pipeline emitted a snapshot.
function restoreDebugForConv(convId) {
  /* Conversation navigation never loads this feature. Once loaded, a closed
   * inspector stays API-idle; a visible drawer refreshes its task list and
   * legacy message detail together. */
  _riOnConvSwitch(convId);
  if (!DebugShellState.visible) return;
  const cached = DebugShellState.cache[convId];
  if (cached && cached.messages && cached.messages.length > 0) {
    showMessagesInDebug(
      cached.messages, cached.label, false, undefined, cached.tools,
      cached.approx, undefined,
      { contextManifest: cached.contextManifest || [] });
    return;
  }
  const conv = DebugShellState.conversations.find((c) => c.id === convId);
  // Decide if there's anything worth fetching. For shell convs this is
  // _serverTurnCount > 0 even before the Turn snapshot lands.
  const _hasServerMsgs = conv && (
    (runtimeScope.ConversationTurnRead?.ordered?.(conv)?.length || 0) > 0 ||
    (conv._serverTurnCount || 0) > 0 ||
    !!conv._turnSnapshotRequired
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
  const _sp = DebugShellState.config?.systemPrompt || '';
  Api.conversations.getDebugMessages(convId, _sp)
    .then(data => {
      // The user may have switched away while the fetch was in flight.
      if (convId !== DebugShellState.activeConversationId) return;
      if (data && data.messages && data.messages.length > 0) {
        showMessagesInDebug(
          data.messages,
          `${data.count} msgs (server)`,
          false,
          convId,
          undefined,
          !!data.approx,
          undefined,
          { contextManifest: data.contextManifest || [] },
        );
      } else {
        clearDebug();
      }
    })
    .catch((e) => {
      console.warn("[debug-panel] /debug-messages fetch failed:", e);
      DebugShellState.reportError(
        `[debug-panel] fetch failed: ${e && e.message || e}`);
      if (convId === DebugShellState.activeConversationId) clearDebug();
    });
}
/* ── Message-block render helpers (module scope) ─────────────────────
 * Hoisted out of showMessagesInDebug so the request-inspector INLINE state
 * panel renders a state snapshot through the exact same code path as the
 * debug drawer — one renderer, two containers. */
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
/* ── Structured message-body renderer (2026-08-05 panel redesign) ─────────
 * colorJson dumps the message ENVELOPE as one JSON blob — but the values a
 * human opens the panel to read (tool-call arguments, reasoning, content)
 * are JSON strings with escaped \n inside that blob, i.e. unreadable at any
 * size. The structured view renders those fields as real text: arguments
 * parsed into per-key blocks with real newlines, reasoning/content as
 * readable prose, and the raw envelope stays one click away in the
 * 原始 JSON <details> (which is also what the copy-path needs). */
function _debugTryParseJson(s) {
  if (typeof s !== "string") return null;
  const tr = s.trim();
  if (!tr || (tr[0] !== "{" && tr[0] !== "[")) return null;
  try {
    const v = JSON.parse(tr);
    return v && typeof v === "object" ? v : null;
  } catch (_) { return null; }
}
/* One argument/field value: nested JSON strings parsed, long/multi-line
 * strings as readable text blocks, scalars inline. */
function _debugArgValHtml(v) {
  if (typeof v === "string") {
    const nested = _debugTryParseJson(v);
    if (nested) return '<pre class="debug-json">' + colorJson(nested, 0) + "</pre>";
    if (v.length > 80 || v.indexOf("\n") !== -1)
      return '<pre class="debug-text debug-arg-val">' + escapeHtml(v) + "</pre>";
    return '<span class="debug-str">"' + escapeHtml(v) + '"</span>';
  }
  if (typeof v === "number" || typeof v === "boolean")
    return '<span class="debug-num">' + String(v) + "</span>";
  if (v === null || v === undefined)
    return '<span class="debug-null">null</span>';
  return '<pre class="debug-json">' + colorJson(v, 0) + "</pre>";
}
function _debugToolCallHtml(tc) {
  const fn = (tc && tc.function) || {};
  const name = fn.name || (tc && tc.name) || "?";
  const id = (tc && tc.id) || "";
  const raw = fn.arguments !== undefined ? fn.arguments
    : (tc ? tc.arguments : undefined);
  /* arguments is a JSON STRING on the wire — parse it so a write_file
   * content arg reads as a text block instead of one escaped line. */
  const parsed = typeof raw === "string" ? _debugTryParseJson(raw)
    : (raw && typeof raw === "object" ? raw : null);
  let argsHtml;
  if (parsed) {
    const keys = Object.keys(parsed);
    argsHtml = keys.length ? keys.map((k) => {
      const v = parsed[k];
      const blocky = (typeof v === "string" &&
          (v.length > 80 || v.indexOf("\n") !== -1)) ||
        (v && typeof v === "object");
      return blocky
        ? '<div class="debug-arg"><div class="debug-arg-key">' + escapeHtml(k) +
          "</div>" + _debugArgValHtml(v) + "</div>"
        : '<div class="debug-kv"><span class="debug-arg-key">' + escapeHtml(k) +
          "</span>: " + _debugArgValHtml(v) + "</div>";
    }).join("") : '<span class="debug-null">{}</span>';
  } else if (typeof raw === "string" && raw) {
    /* Unparseable (e.g. a truncated stream) — show the raw string as text
     * rather than as one quoted JSON line. */
    argsHtml = '<pre class="debug-text debug-arg-val">' + escapeHtml(raw) + "</pre>";
  } else {
    argsHtml = '<span class="debug-null">—</span>';
  }
  return '<div class="debug-tc-card">' +
    '<div class="debug-tc-head"><span class="debug-tc-name">' + escapeHtml(name) +
    "</span>" +
    (id ? '<span class="debug-tc-id">' + escapeHtml(id) + "</span>" : "") +
    '</div><div class="debug-tc-args">' + argsHtml + "</div></div>";
}
function _debugSecHtml(label, inner, open) {
  return '<details class="debug-sec"' + (open ? " open" : "") + "><summary>" +
    escapeHtml(label) + '</summary><div class="debug-sec-body">' + inner +
    "</div></details>";
}
function _renderMsgBodyHtml(msg) {
  if (!msg || typeof msg !== "object") return "";
  const parts = [];
  const reasoning = typeof msg.reasoning_content === "string"
    ? msg.reasoning_content
    : (typeof msg.reasoning === "string" ? msg.reasoning : "");
  if (reasoning) {
    parts.push(_debugSecHtml(
      t("debug.structReasoning") + " · " + _fmtKB(reasoning.length),
      '<pre class="debug-text' + (reasoning.length > 2000 ? " debug-long" : "") +
        '">' + escapeHtml(reasoning) + "</pre>",
      reasoning.length <= 500));
  }
  if (typeof msg.content === "string" && msg.content) {
    /* Tool results are often JSON text — render them parsed, not quoted. */
    const asJson = msg.role === "tool" ? _debugTryParseJson(msg.content) : null;
    const inner = asJson
      ? '<pre class="debug-json">' + colorJson(asJson, 0) + "</pre>"
      : '<pre class="debug-text' +
        (msg.content.length > 2000 ? " debug-long" : "") + '">' +
        escapeHtml(msg.content) + "</pre>";
    parts.push(_debugSecHtml(
      (msg.role === "tool" ? t("debug.structToolResult")
        : t("debug.structContent")) + " · " + _fmtKB(msg.content.length),
      inner, msg.content.length <= 1200));
  } else if (Array.isArray(msg.content)) {
    const inner = msg.content.map((b) => {
      if (b && typeof b === "object") {
        if (b.type === "text")
          return '<pre class="debug-text' +
            ((b.text || "").length > 2000 ? " debug-long" : "") + '">' +
            escapeHtml(b.text || "") + "</pre>";
        if (b.type === "thinking" && typeof b.thinking === "string")
          return '<pre class="debug-text">' + escapeHtml(b.thinking) + "</pre>";
        if (b.type === "image_url") {
          const url = (b.image_url && b.image_url.url) || "";
          return '<div class="debug-img-chip" title="' +
            escapeHtml(url.slice(0, 120)) + '">' +
            escapeHtml(t("debug.structImage")) + " · " + _fmtKB(url.length) +
            "</div>";
        }
      }
      return '<pre class="debug-json">' + colorJson(b, 0) + "</pre>";
    }).join("");
    parts.push(_debugSecHtml(t("debug.structContent"), inner, true));
  }
  if (Array.isArray(msg.tool_calls) && msg.tool_calls.length) {
    parts.push(_debugSecHtml(
      t("debug.structToolCalls") + " · " + msg.tool_calls.length,
      msg.tool_calls.map((tc) => _debugToolCallHtml(tc)).join(""), true));
  }
  /* Everything not rendered above (name / tool_call_id / internal markers). */
  const skip = { role: 1, content: 1, tool_calls: 1, reasoning_content: 1,
    reasoning: 1 };
  const extra = {};
  for (const k of Object.keys(msg)) if (!skip[k]) extra[k] = msg[k];
  if (Object.keys(extra).length) {
    parts.push(_debugSecHtml(t("debug.structFields"),
      '<pre class="debug-json">' + colorJson(extra, 0) + "</pre>", false));
  }
  return parts.join("");
}
/* Render one message body: the structured view + the raw JSON <details>.
 * The raw <pre> is filled EAGERLY (not on details-open) — the copy path and
 * the open-state restore both expect `.debug-msg-body pre` populated once
 * body.dataset.rendered is set. */
function _debugRenderBody(body, msg) {
  if (!body) return;
  body.dataset.rendered = "1";
  const struct = body.querySelector(".debug-struct");
  if (struct) struct.innerHTML = _renderMsgBodyHtml(msg);
  const pre = body.querySelector(".debug-raw pre");
  if (pre) pre.innerHTML = colorJson(msg, 0);
}
/* Open one block (header arrow + lazy body render) — shared by the
 * open-state restore and the inline panel's auto-expand. */
function _debugOpenBlock(block) {
  if (!block || block.classList.contains("open")) return;
  block.classList.add("open");
  const arrow = block.querySelector(".debug-msg-header span:last-child");
  if (arrow) arrow.style.transform = "rotate(90deg)";
  const body = block.querySelector(".debug-msg-body");
  if (body && !body.dataset.rendered) _debugRenderBody(body, block._msgRef);
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
    // Lazy-render body content on first open — always from block._msgRef
    // (updated by the incremental path), never a stale closure capture.
    const body = block.querySelector(".debug-msg-body");
    if (isOpen && body && !body.dataset.rendered) {
      _debugRenderBody(body, block._msgRef);
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
  // Body (collapsed, lazy-rendered): structured view + raw JSON details.
  const body = document.createElement("div");
  body.className = "debug-msg-body";
  const struct = document.createElement("div");
  struct.className = "debug-struct";
  body.appendChild(struct);
  const raw = document.createElement("details");
  raw.className = "debug-raw";
  const rawSummary = document.createElement("summary");
  rawSummary.textContent = t("debug.structRawJson");
  raw.appendChild(rawSummary);
  raw.appendChild(document.createElement("pre"));
  body.appendChild(raw);
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
/* Shared full-render of message blocks into a container — the debug drawer's
 * full-render path AND the request-inspector inline state panel both render
 * through here. Wipes `p`, renders the optional prefix fold + one block per
 * message. Pure full render only: the drawer's incremental path, open-state
 * restore and scroll handling stay in showMessagesInDebug; tools are synced
 * separately via updateDebugToolsBlock (the drawer updates them on the
 * incremental path too, outside any wipe). */
function renderDebugBlocksInto(p, messages, opts) {
  p.innerHTML = "";
  /* Request Inspector P3: prefix-fold diff view. When the caller passes
   * opts.foldPrefix (K leading messages byte-identical to the PREVIOUS
   * round's payload), collapse them behind a fold row and mark the
   * increment with .debug-msg-new — the diff is the whole point of the
   * per-round view (what did THIS request add to the context). */
  const _foldK = (opts && opts.foldPrefix > 0)
    ? Math.min(opts.foldPrefix, messages.length) : 0;
  if (_foldK > 0) {
    const foldRow = document.createElement('div');
    foldRow.className = 'debug-prefix-fold';
    foldRow.innerHTML = Icon('chevronDown', 11) + ' ' +
      escapeHtml(t('ri.prefixFold', { k: _foldK, base: opts.diffBase || '' }));
    foldRow.onclick = () => {
      const open = foldRow.classList.toggle('open');
      p.querySelectorAll('.debug-msg-prefix').forEach((b) => {
        b.style.display = open ? '' : 'none';
      });
    };
    p.appendChild(foldRow);
  }
  messages.forEach((msg, i) => {
    const block = createBlock(msg, i);
    if (_foldK > 0) {
      if (i < _foldK) {
        block.classList.add('debug-msg-prefix');
        block.style.display = 'none';
      } else {
        block.classList.add('debug-msg-new');
      }
    }
    block.dataset.fp = msgFingerprint(msg);
    block._msgRef = msg;
    p.appendChild(block);
  });
}
/* Create the collapsible TOOLS block skeleton (header + lazy body). The body
 * renders from toolsBlock._toolsRef on first open. */
function _createDebugToolsBlock() {
  const toolsBlock = document.createElement('div');
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
  return toolsBlock;
}
/* Insert (or reuse) the TOOLS block at the top of container `p` and sync it
 * with the current tools array: summary text, _toolsRef, and an invalidated /
 * re-rendered body when the block is open. */
function updateDebugToolsBlock(p, tools) {
  let toolsBlock = p.querySelector('.debug-tools-block');
  if (!toolsBlock) {
    toolsBlock = _createDebugToolsBlock();
    p.insertBefore(toolsBlock, p.firstChild);
  }
  const names = tools.map(t => (t.function ? t.function.name : '?'));
  const tSum = toolsBlock.querySelector('.debug-msg-summary');
  if (tSum) tSum.textContent = `${tools.length} tools: ${names.join(', ')}`;
  toolsBlock._toolsRef = tools;
  const tBody = toolsBlock.querySelector('.debug-msg-body');
  if (tBody && tBody.dataset.rendered && toolsBlock.classList.contains('open')) {
    tBody.dataset.rendered = '1';
    const tPre = tBody.querySelector('pre');
    if (tPre) tPre.innerHTML = colorJson(tools, 0);
  } else if (tBody) {
    tBody.dataset.rendered = '';
  }
}
function _groupContextManifest(rows) {
  const groups = {};
  for (const row of rows || []) {
    const key = row && row.placement ? row.placement : 'unknown';
    (groups[key] = groups[key] || []).push(row);
  }
  return groups;
}
function _createDebugContextBlock() {
  const block = document.createElement('div');
  block.className = 'debug-context-block debug-msg-block';
  const header = document.createElement('div');
  header.className = 'debug-msg-header';
  const role = document.createElement('span');
  role.className = 'role-context';
  role.innerHTML = Icon('package', 12) + ' CONTEXT';
  header.appendChild(role);
  const summary = document.createElement('span');
  summary.className = 'debug-msg-summary';
  header.appendChild(summary);
  const arrow = document.createElement('span');
  arrow.textContent = '▶';
  arrow.style.cssText = 'font-size:9px;transition:transform 0.2s;color:var(--text-tertiary)';
  header.appendChild(arrow);
  const body = document.createElement('div');
  body.className = 'debug-msg-body';
  const pre = document.createElement('pre');
  body.appendChild(pre);
  header.onclick = () => {
    const open = block.classList.toggle('open');
    arrow.style.transform = open ? 'rotate(90deg)' : '';
    if (open && !body.dataset.rendered) {
      body.dataset.rendered = '1';
      pre.innerHTML = colorJson(_groupContextManifest(block._manifestRef), 0);
    }
  };
  block.appendChild(header);
  block.appendChild(body);
  return block;
}
function updateDebugContextBlock(p, manifest) {
  let block = p.querySelector('.debug-context-block');
  if (!block) {
    block = _createDebugContextBlock();
    p.insertBefore(block, p.firstChild);
  }
  block._manifestRef = manifest;
  const injected = manifest.filter((row) => row && row.injected);
  const tokens = injected.reduce((n, row) => n + Number(row.tokens || 0), 0);
  const summary = block.querySelector('.debug-msg-summary');
  if (summary) {
    summary.textContent = `${manifest.length} blocks · ${injected.length} injected` +
      (tokens ? ` · ~${tokens >= 1000 ? (tokens / 1000).toFixed(1) + 'K' : tokens}tok` : '');
  }
  const body = block.querySelector('.debug-msg-body');
  if (body && body.dataset.rendered && block.classList.contains('open')) {
    const pre = body.querySelector('pre');
    if (pre) pre.innerHTML = colorJson(_groupContextManifest(manifest), 0);
  } else if (body) {
    body.dataset.rendered = '';
  }
}
// Render one captured model-request message array — supports incremental updates
//   isUpdate=true → streaming update, preserve collapse states, only patch changed blocks
//   approx=true → COLD-path reconstruction (the /debug-messages endpoint, which
//     rebuilds the wire form from the DB with a hypothetical first-round for the
//     per-round memory/date). Renders the amber "reconstructed approximation"
//     chip so the human knows they are NOT looking at a precise capture of a
//     specific round. The live SSE snapshot path (the real wire form) passes
//     approx=false/undefined and must NEVER show this chip.
function showMessagesInDebug(messages, label, isUpdate, forConvId, tools, approx, meta, opts) {
  const contextManifest =
    (meta && Array.isArray(meta.contextManifest) && meta.contextManifest) ||
    (opts && Array.isArray(opts.contextManifest) && opts.contextManifest) || [];
  /* Request Inspector (P1): when the SSE handler forwards the snapshot's
   * envelope metadata, record the round into the per-task log (append, never
   * overwrite). The legacy cold path (/debug-messages) passes no meta and is
   * not recorded — it is an approximation, not a specific round. */
  if (meta && typeof meta === "object") {
    DebugShellState.recordSnapshot(meta.taskId, {
      kind: meta.kind || "request",
      roundNum: meta.roundNum,
      turn: meta.turn || "",
      label: label,
      model: meta.model || "",
      params: meta.params || null,
      messageCount: messages.length,
      toolsCount: (tools && tools.length) || 0,
      ts: Date.now(),
      messages: messages,
      tools: tools || null,
      contextManifest: contextManifest,
    });
  }
  const cid = forConvId || DebugShellState.activeConversationId;
  // Cache for conversation switching
  if (cid) {
    DebugShellState.cache[cid] = { messages, label };
    if (tools) DebugShellState.cache[cid].tools = tools;
    DebugShellState.cache[cid].approx = !!approx;
    DebugShellState.cache[cid].contextManifest =
      (meta && meta.contextManifest) ||
      (opts && opts.contextManifest) || [];
  }
  // Only render if this conv is currently active (or no conv specified)
  if (
    forConvId &&
    forConvId !== DebugShellState.activeConversationId
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
  const _contextWasOpen = !!p.querySelector(".debug-context-block.open");
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
    const contextSuffix = contextManifest.length
      ? ` · ${Icon('package', 11)}${contextManifest.filter((r) => r && r.injected).length}/${contextManifest.length}`
      : '';
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
    title.innerHTML = `${Icon('inbox', 14)} Messages (${messages.length})${toolsSuffix}${contextSuffix}${compactedSuffix}${brainSuffix}${tokSuffix}${label ? " — " + escapeHtml(String(label)) : ""}`;
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
  // --- Incremental update path ---
  // FIX: detect when incremental update is not appropriate and fall back to full render
  //   e.g. when message structure changes drastically (server snapshot replaces client-side build)
  if (isUpdate) {
    const existing = p.querySelectorAll(
      ".debug-msg-block:not(.debug-tools-block):not(.debug-context-block)");
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
    const existing = p.querySelectorAll(
      ".debug-msg-block:not(.debug-tools-block):not(.debug-context-block)");
    const existingCount = existing.length;
    const newCount = messages.length;
    // Update existing blocks that changed (by fingerprint)
    for (let i = 0; i < Math.min(existingCount, newCount); i++) {
      const oldFp = existing[i].dataset.fp || "";
      const newFp = msgFingerprint(messages[i]);
      if (oldFp !== newFp) {
        // Content changed - update role, summary, invalidate body if it was rendered
        existing[i].dataset.fp = newFp;
        // FIX: Update role label and class when role changes
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
            _debugRenderBody(body, messages[i]);
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
    // Append new blocks (createBlock already binds lazy render on _msgRef)
    for (let i = existingCount; i < newCount; i++) {
      const block = createBlock(messages[i], i);
      block.dataset.fp = msgFingerprint(messages[i]);
      block._msgRef = messages[i];
      p.appendChild(block);
    }
  } else {
    // --- Full render path (initial) ---
    renderDebugBlocksInto(p, messages, opts);
    // Re-apply the expanded state captured before the wipe so a snapshot
    // update that fell through to this full render doesn't collapse what the
    // user expanded to inspect. Match by stable IDENTITY (data-mid), iterating
    // freshly-rendered blocks — so if the snapshot dropped/reordered a message,
    // the block the user opened re-opens wherever it now sits, and a different
    // message that happens to land at the old index does NOT.
    if (_openMids.size) {
      p.querySelectorAll(".debug-msg-block").forEach((b) => {
        if (!b.dataset.mid || !_openMids.has(b.dataset.mid)) return;
        _debugOpenBlock(b);
      });
    }
    // Only snap to top on a genuine first render — preserve the user's scroll
    // position when this was a re-render over existing content.
    p.scrollTop = _hadExisting ? _prevScroll : 0;
  }
  // Render tools section (collapsible, before messages)
  if (tools && tools.length > 0) {
    updateDebugToolsBlock(p, tools);
  }
  if (contextManifest.length > 0) {
    updateDebugContextBlock(p, contextManifest);
  } else {
    const staleContext = p.querySelector('.debug-context-block');
    if (staleContext) staleContext.remove();
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
  if (_contextWasOpen) {
    const _cb = p.querySelector('.debug-context-block');
    if (_cb && !_cb.classList.contains('open')) {
      _cb.classList.add('open');
      const _ca = _cb.querySelector('.debug-msg-header span:last-child');
      if (_ca) _ca.style.transform = 'rotate(90deg)';
      const _cbody = _cb.querySelector('.debug-msg-body');
      if (_cbody && !_cbody.dataset.rendered && _cb._manifestRef) {
        _cbody.dataset.rendered = '1';
        const _cpre = _cbody.querySelector('pre');
        if (_cpre) _cpre.innerHTML = colorJson(
          _groupContextManifest(_cb._manifestRef), 0);
      }
    }
  }
  /* opts.resetScroll: snap to top regardless of render path. The incremental
   * path deliberately preserves scroll for live streaming, but the request
   * inspector sets resetScroll when SWITCHING ROUNDS — a different round is
   * a new context where the previous round's scroll offset is meaningless
   * (and the incremental role-match often keeps the switch ON the
   * scroll-preserving path, so a full-path-only reset would not bite). */
  if (opts && opts.resetScroll) p.scrollTop = 0;
  // Store for copy
  p._rawMessages = messages;
  p._rawTools = tools || null;
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

/* Optional lifecycle port. Retained conversation navigation consults it only
 * after this owner has loaded, so ordinary navigation never imports the chunk
 * or starts a diagnostics request. */
const DebugPresentationState = Object.freeze({
  clear: clearDebug,
  onConversationSwitch: restoreDebugForConv,
});
/* ===== migrated source: core/request_inspector.js ===== */
/* Request Inspector adapter: task events are authoritative; debug-panel owns
 * the detail renderer. Opens by stable task/round identity. */

let _riOpen = false;
const _riSel = { taskId: null, fold: null };
const _riTaskRows = {};

let _riConvId = null;
let _riPollTimer = null;
/* Poll cadence: fast while any row is live, slow when idle. The drawer's
 * by-conv list is a point-in-time read — without a poll, a RUNNING row
 * (and its fold) freezes at whatever it was when the drawer opened. */
const _RI_POLL_LIVE_MS = 3000;
const _RI_POLL_IDLE_MS = 15000;

/* Accumulated level-1 rows (first page + user-paged earlier rows) and the
 * pagination cursor state. Silent polls MERGE the newest page into this
 * list instead of replacing it, so a user-expanded history never
 * collapses back on the next tick. */
let _riTaskList = [];
let _riHasMore = false;
let _riListConvId = null;
let _riLoadingEarlier = false;

/* Real-time drive: the poll alone made the drawer lag up to 3s (live) /
 * 15s (idle) behind reality, and a task that STARTED and FINISHED between
 * two ticks never showed its transitions at all. Subscribing to the
 * conversation's TurnStore flips that: any attempt/turn STATUS dispatch
 * (not content deltas — those fire per token) triggers a throttled silent
 * refresh. The poll stays as the cross-process backstop. */
let _riStoreUnsub = null;
let _riStoreFp = '';
let _riStoreRefreshTimer = null;

function _riStoreFingerprint() {
  try {
    const read = runtimeScope.ConversationTurnRead;
    if (!read || !_riConvId || !read.state) return '';
    const state = read.state(_riConvId);
    if (!state) return '';
    const parts = [];
    const attempts = state.attemptsById || {};
    for (const id of Object.keys(attempts).sort()) {
      const a = attempts[id] || {};
      parts.push(id + ':' + (a.status || ''));
    }
    const turns = state.turnsById || {};
    let running = 0;
    for (const id of Object.keys(turns)) {
      if (turns[id] && turns[id].status === 'running') running += 1;
    }
    return parts.join(';') + '|r' + running;
  } catch (_) { return ''; }
}

function _riOnStoreEvent() {
  if (!_riOpen) return;
  const fp = _riStoreFingerprint();
  if (fp === _riStoreFp) return;  // content delta — not task activity
  _riStoreFp = fp;
  if (_riStoreRefreshTimer) return;
  _riStoreRefreshTimer = setTimeout(async () => {
    if (typeof _riStoreRefreshTimer.unref === 'function') {
      _riStoreRefreshTimer.unref();
    }
    _riStoreRefreshTimer = null;
    if (!_riOpen) return;
    await _riRefreshTasks({ silent: true });
    const live = Object.keys(_riTaskRows).some(
      (id) => _riRowIsLive(_riTaskRows[id]));
    _riSchedulePoll(live ? _RI_POLL_LIVE_MS : _RI_POLL_IDLE_MS);
  }, 800);
}

function _riBindStore(convId) {
  _riUnbindStore();
  if (!convId) return;
  try {
    const rt = runtimeScope.ConversationTurnStore;
    const store = rt && rt.ensureRuntimeStore && rt.ensureRuntimeStore(convId);
    if (!store || typeof store.subscribe !== 'function') return;
    _riStoreFp = _riStoreFingerprint();
    _riStoreUnsub = store.subscribe(_riOnStoreEvent);
  } catch (_) { /* store unavailable — poll remains the drive */ }
}

function _riUnbindStore() {
  if (_riStoreUnsub) {
    try { _riStoreUnsub(); } catch (_) { /* already gone */ }
    _riStoreUnsub = null;
  }
  _riStoreFp = '';
  if (_riStoreRefreshTimer) {
    clearTimeout(_riStoreRefreshTimer);
    _riStoreRefreshTimer = null;
  }
}

function toggleRequestInspector() {
  if (_riOpen) closeRequestInspector();
  else openRequestInspector();
}

function openRequestInspector() {
  _riOpen = true;
  _riSel.taskId = null;
  _riSel.fold = null;
  DebugShellState.visible = true;
  document.body.classList.add('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'flex';
  _riResetDetail();
  const convId = DebugShellState.activeConversationId;
  _riLoadTasks(convId);
  _riBindStore(convId);
  _riSchedulePoll(_RI_POLL_IDLE_MS);
}

function closeRequestInspector() {
  _riOpen = false;
  _riStopPoll();

  _riUnbindStore();
  DebugShellState.visible = false;
  document.body.classList.remove('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'none';
}

async function openRequestInspectorForTask(taskId) {
  if (!taskId) return;
  if (!_riOpen) openRequestInspector();
  await _riSelectTask(String(taskId));
}

/* Called from restoreDebugForConv (debug_panel.js) on conversation switch. */
function _riOnConvSwitch(convId) {
  if (!_riOpen) return;
  _riSel.taskId = null;
  _riSel.fold = null;
  _riResetDetail();
  _riLoadTasks(convId);
  _riBindStore(convId);
}

function _riStopPoll() {
  if (_riPollTimer) { clearTimeout(_riPollTimer); _riPollTimer = null; }
}

function _riSchedulePoll(delayMs) {
  _riStopPoll();
  _riPollTimer = setTimeout(_riPollTick, delayMs);
  /* Node/jsdom harnesses: a pending poll must not keep the event loop
   * alive once the test body finished (browsers ignore unref). */
  if (_riPollTimer && typeof _riPollTimer.unref === 'function') {
    _riPollTimer.unref();
  }
}

async function _riPollTick() {
  _riPollTimer = null;
  if (!_riOpen) return;
  if (document.hidden) { _riSchedulePoll(_RI_POLL_IDLE_MS); return; }
  await _riRefreshTasks({ silent: true });
  const live = Object.keys(_riTaskRows).some((id) => _riRowIsLive(_riTaskRows[id]));
  _riSchedulePoll(live ? _RI_POLL_LIVE_MS : _RI_POLL_IDLE_MS);
}

function _riRowIsLive(row) {
  return !!(row && (row.live ||
    ['running', 'queued', 'pending'].includes(
      String(row.status || '').toLowerCase())));
}

/* Refresh the task list; when the SELECTED task is still live, refresh its
 * level-2 fold as well — silently, so the detail pane the user is reading
 * (round payload / trace) is never reset by a background tick. */
async function _riRefreshTasks(opts) {
  const silent = !!(opts && opts.silent);
  await _riLoadTasks(_riConvId, { silent });
  if (_riSel.taskId && _riRowIsLive(_riTaskRows[_riSel.taskId])) {
    await _riSelectTask(_riSel.taskId, { silent: true });
  }
  if (!silent) _riSchedulePoll(_RI_POLL_IDLE_MS);
}

/* data-tofu-action targets (header refresh button / inline retry). */
function riRefreshTasks() {
  if (!_riOpen) return undefined;
  return _riRefreshTasks({ silent: false });
}

function riRetryTask() {
  if (!_riOpen) return undefined;
  if (_riSel.taskId) return _riSelectTask(_riSel.taskId);
  return _riRefreshTasks({ silent: false });
}

/* Turn badge label for a request row (P4): Flow node phases read through
 * i18n; swarm agents show their role; anything else falls back to the raw
 * tag so a future turn value still renders. */
function _riTurnLabel(row) {
  if (row.turn === 'swarm-agent') return row.agentRole || 'agent';
  const key = 'ri.turn' + String(row.turn || '').charAt(0).toUpperCase() +
    String(row.turn || '').slice(1);
  const v = t(key);
  return v === key ? String(row.turn) : v;
}

function _riEsc(s) {
  return (typeof escapeHtml === 'function')
    ? escapeHtml(s == null ? '' : String(s)) : String(s == null ? '' : s);
}

function _riAbsTime(ts) {
  if (!ts) return '';
  try {
    const d = new Date(Number(ts));
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
      `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  } catch (_) { return ''; }
}

/* Task-list primary timestamp: "3 分钟前" reads far better than a bare
 * HH:MM:SS when rows span a whole session; the absolute stamp stays on
 * the title tooltip. */
function _riRelTime(ts) {
  if (!ts) return '';
  const diff = Date.now() - Number(ts);
  if (diff < 0) return _riAbsTime(ts);
  const s = Math.floor(diff / 1000);
  if (s < 45) return t('ri.timeJustNow');
  const m = Math.floor(s / 60);
  if (m < 60) return t('ri.timeMinAgo', { n: Math.max(1, m) });
  const h = Math.floor(m / 60);
  if (h < 24) return t('ri.timeHoursAgo', { n: h });
  const d = Math.floor(h / 24);
  if (d < 7) return t('ri.timeDaysAgo', { n: d });
  return _riAbsTime(ts).slice(5, 16);
}

/* Map a task row to its reply bubble: live rows carry turnId from the
 * server; otherwise the conversation's attempt records know which task
 * produced which turn. The ordinal counts assistant replies only, so it
 * matches what the user reads as "reply #N". */
function _riTurnOrdinal(row) {
  try {
    const read = runtimeScope.ConversationTurnRead;
    if (!read || !_riConvId || !row) return null;
    let turnId = row.turnId || '';
    if (!turnId) {
      const state = read.state && read.state(_riConvId);
      const attempts = (state && state.attemptsById) || {};
      for (const att of Object.values(attempts)) {
        if (att && att.taskId === row.taskId && att.turnId) {
          turnId = att.turnId;
          break;
        }
      }
    }
    if (!turnId) return null;
    let n = 0;
    const ordered = (read.ordered && read.ordered(_riConvId)) || [];
    for (const turn of ordered) {
      if (!turn || !turn.turnId) continue;
      if (turn.actor === 'assistant') n += 1;
      if (turn.turnId === turnId) {
        return n > 0 ? { turnId, ordinal: n } : null;
      }
    }
    return null;
  } catch (_) { return null; }
}

function _riTurnChip(row) {
  const ref = _riTurnOrdinal(row);
  if (!ref) return null;
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = 'ri-turn-chip';
  chip.dataset.turnId = ref.turnId;
  chip.textContent = t('ri.turnChip', { n: ref.ordinal });
  chip.title = t('ri.turnChipTip');
  return chip;
}

function _riScrollToTurn(turnId) {
  const node = (typeof _findRenderedNativeTurnNode === 'function')
    ? _findRenderedNativeTurnNode(turnId) : null;
  if (node && node.scrollIntoView) {
    node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function _riEl(id) { return document.getElementById(id); }

function _riSetDetailActive(active) {
  const drawer = _riEl('riDrawer');
  if (drawer) drawer.classList.toggle('ri-detail-active', !!active);
}

function _riResetDetail() {
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (title) title.textContent = t('ri.detailTitle');
  if (content) content.innerHTML = `<div class="ri-main-empty">` +
    `${_riEsc(t('ri.selectRound'))}</div>`;
  _riSetDetailActive(false);
}

function _riStatusInfo(row) {
  const raw = String((row && row.status) || '').toLowerCase();
  if ((row && row.live) || ['running', 'queued', 'pending'].includes(raw))
    return { tone: 'running', label: t('ri.statusRunning') };
  if (['done', 'completed', 'complete', 'success'].includes(raw))
    return { tone: 'done', label: t('ri.statusDone') };
  if (['error', 'failed', 'failure'].includes(raw))
    return { tone: 'error', label: t('ri.statusFailed') };
  if (['aborted', 'interrupted', 'cancelled', 'canceled', 'stopped'].includes(raw))
    return { tone: 'stopped', label: t('ri.statusStopped') };
  return { tone: 'neutral', label: raw || t('ri.statusUnknown') };
}

function _riRevealTechnical() {
  const details = _riEl('riRoundList') &&
    _riEl('riRoundList').querySelector('.ri-technical');
  if (details) details.open = true;
}

/* ── Level 1: task rows for the active conversation ── */
async function _riLoadTasks(convId, opts) {
  const silent = !!(opts && opts.silent);
  const list = _riEl('riTaskList');
  const rounds = _riEl('riRoundList');
  if (!list) return;
  _riConvId = convId || null;
  if (!silent && rounds) rounds.innerHTML = '';
  if (_riListConvId !== _riConvId || !silent) {
    /* A fresh explicit load (open / conv switch / manual refresh) resets
     * the accumulated pagination; silent polls merge into it. */
    _riTaskList = [];
    _riHasMore = false;
  }
  _riListConvId = _riConvId;
  if (!_riConvId) {
    list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.empty'))}</div>`;
    return;
  }
  /* Silent polls keep the current DOM (and its scroll position) while the
   * fetch is in flight — no loading-flash every few seconds. */
  if (!silent) {
    list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.loading'))}</div>`;
  }
  const data = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.byConv(convId) : null;
  if (!_riOpen) return;  // drawer closed while fetching
  if (convId !== _riConvId) return;  // conversation switched mid-flight
  if (!data) {
    /* byConv resolves null on ANY failure (network error, 404, 500).
     * Never present that as "no tasks" — say it failed and offer retry. */
    list.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.loadFailed'))} ` +
      `<button type="button" class="ri-retry" ` +
      `data-tofu-action="riRefreshTasks()">${_riEsc(t('ri.retry'))}</button></div>`;
    return;
  }
  const tasks = Array.isArray(data.tasks) ? data.tasks : [];
  _riHasMore = !!data.hasMore;
  _riMergeFirstPage(tasks);
  if (!_riTaskList.length) {
    /* readError = the STORAGE read failed: an empty list is only honest
     * when the read succeeded. Failure gets the retry affordance, never
     * the "no tasks recorded" empty state. */
    list.innerHTML = data.readError
      ? `<div class="ri-empty">${_riEsc(t('ri.loadFailed'))} ` +
        `<button type="button" class="ri-retry" ` +
        `data-tofu-action="riRefreshTasks()">${_riEsc(t('ri.retry'))}</button></div>`
      : `<div class="ri-empty">${_riEsc(t('ri.empty'))}</div>`;
    return;
  }
  if (!silent && rounds && !_riSel.taskId) {
    rounds.innerHTML = `<div class="ri-empty ri-select-task">` +
      `${_riEsc(t('ri.selectTask'))}</div>`;
  }
  _riRenderTaskList({ keepScroll: silent });
  if (data.readError) {
    /* Partial failure: live rows made it, the persisted read did not —
     * warn on top instead of silently presenting a truncated history. */
    const warn = document.createElement('div');
    warn.className = 'ri-warn-line';
    warn.innerHTML = `${_riEsc(t('ri.loadFailed'))} ` +
      `<button type="button" class="ri-retry" ` +
      `data-tofu-action="riRefreshTasks()">${_riEsc(t('ri.retry'))}</button>`;
    list.prepend(warn);
  }
}

/* Merge the newest page into the accumulated list. Persisted rows are
 * immutable and stay; a LIVE row missing from the newest page vanished
 * from the registry (finished → its persisted twin is in this page, or
 * evicted) and must not linger as forever-running. */
function _riMergeFirstPage(rows) {
  const fresh = {};
  for (const r of rows) fresh[r.taskId] = true;
  const kept = _riTaskList.filter((r) => fresh[r.taskId] === undefined && !r.live);
  _riTaskList = kept.concat(rows)
    .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
  for (const key of Object.keys(_riTaskRows)) delete _riTaskRows[key];
  for (const r of _riTaskList) _riTaskRows[r.taskId] = r;
}

/* Group task rows by the reply they produced. The old flat newest-first
 * list mixed retries, swarm agents and unrelated replies into one strip
 * ("任务顺序看不懂"). Grouped: one header per reply (chip + question
 * preview + latest time), runs inside a group in CHRONOLOGICAL order
 * (run 1, run 2…), swarm children nested right after their parent. Rows
 * whose turn is not loaded (old pages, pre-TurnStore history) cluster in
 * one trailing "earlier" group; when NOTHING resolves the list renders
 * headerless, exactly like before. */
function _riGroupTaskRows(tasks) {
  const childrenByParent = {};
  const parents = [];
  const parentIds = {};
  for (const row of tasks) {
    if (row.parentTaskId) {
      (childrenByParent[row.parentTaskId] =
        childrenByParent[row.parentTaskId] || []).push(row);
    } else {
      parents.push(row);
      parentIds[row.taskId] = true;
    }
  }
  /* Orphaned swarm children (parent paged out) render standalone rather
   * than vanishing. */
  for (const pid of Object.keys(childrenByParent)) {
    if (parentIds[pid]) continue;
    for (const child of childrenByParent[pid]) parents.push(child);
    delete childrenByParent[pid];
  }
  const groups = [];
  const byKey = {};
  for (const row of parents) {
    const ref = _riTurnOrdinal(row);
    const key = ref ? ref.turnId : '__none__';
    let g = byKey[key];
    if (!g) {
      g = { ref, key, rows: [], latest: 0, preview: '' };
      byKey[key] = g;
      groups.push(g);
    }
    g.rows.push(row);
    if ((row.createdAt || 0) > g.latest) g.latest = row.createdAt || 0;
    if (!g.preview && row.userPreview) g.preview = row.userPreview;
  }
  groups.sort((a, b) => b.latest - a.latest);
  for (const g of groups) {
    /* A resolved group holds retries of ONE reply: chronological reads as
     * run 1, run 2…. The unresolved bucket mixes unrelated tasks — keep it
     * newest-first like the pre-grouping flat list. */
    g.rows.sort(g.ref
      ? (a, b) => (a.createdAt || 0) - (b.createdAt || 0)
      : (a, b) => (b.createdAt || 0) - (a.createdAt || 0));
    g.childrenByParent = childrenByParent;
  }
  return groups;
}

function _riGroupHeaderEl(group) {
  const head = document.createElement('div');
  head.className = 'ri-group-head';
  const label = document.createElement('span');
  label.className = 'ri-group-preview';
  if (group.ref) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'ri-turn-chip';
    chip.textContent = t('ri.turnChip', { n: group.ref.ordinal });
    chip.title = t('ri.turnChipTip');
    chip.onclick = (ev) => {
      ev.stopPropagation();
      _riScrollToTurn(group.ref.turnId);
    };
    head.appendChild(chip);
    label.textContent = group.preview || '';
    label.title = group.preview || '';
  } else {
    label.textContent = t('ri.groupOlder');
  }
  head.appendChild(label);
  const time = document.createElement('span');
  time.className = 'ri-group-time';
  time.textContent = _riRelTime(group.latest);
  time.title = _riAbsTime(group.latest);
  head.appendChild(time);
  return head;
}

function _riTaskRowEl(row, opts) {
  const runIndex = (opts && opts.runIndex) || 0;
  const showTurnChip = !!(opts && opts.showTurnChip);
  const el = document.createElement('div');
  el.className = 'ri-task' + (_riSel.taskId === row.taskId ? ' ri-sel' : '') +
    (row.isSwarmAgent ? ' ri-task-agent' : '') +
    (row.parentTaskId ? ' ri-task-child' : '');
  el.dataset.taskId = row.taskId;
  const status = _riStatusInfo(row);
  const expired = !row.hasEvents && !row.live;
  const agentBadge = row.isSwarmAgent
    ? `<span class="ri-agent-badge">${_riEsc(row.agentId || 'agent')}</span> · ` : '';
  const runBadge = runIndex
    ? `<span class="ri-run-badge">${_riEsc(t('ri.runIndex', { n: runIndex }))}</span>`
    : '';
  const preview = row.userPreview
    ? `<div class="ri-task-preview">${_riEsc(row.userPreview)}</div>` : '';
  el.innerHTML =
    `<div class="ri-task-top">` +
    `<span class="ri-task-status ri-tone-${_riEsc(status.tone)}">` +
    `<span class="ri-task-status-dot" aria-hidden="true"></span>` +
    `${_riEsc(status.label)}</span>` +
    runBadge +
    `<span class="ri-task-time" title="${_riEsc(_riAbsTime(row.createdAt))}">` +
    `${_riEsc(_riRelTime(row.createdAt))}</span>` +
    `</div>` +
    preview +
    `<div class="ri-task-sub">` +
    agentBadge +
    (expired
      ? `<span class="ri-expired">${_riEsc(t('ri.expired'))}</span>`
      : `<span>${_riEsc(t('ri.viewProcess'))}</span>`) +
    `<span class="ri-task-id">${_riEsc(t('ri.taskLabel', {
      id: String(row.taskId).slice(0, 8) }))}</span>` +
    `</div>`;
  /* Flat fallback (no turn resolved anywhere): keep the per-row anchor
   * chip. Grouped mode carries the anchor on the group header instead. */
  if (showTurnChip) {
    const chip = _riTurnChip(row);
    if (chip) {
      chip.onclick = (ev) => {
        ev.stopPropagation();
        _riScrollToTurn(chip.dataset.turnId);
      };
      el.querySelector('.ri-task-sub').prepend(chip);
    }
  }
  el.onclick = () => _riSelectTask(row.taskId);
  return el;
}

function _riRenderTaskList(opts) {
  const list = _riEl('riTaskList');
  if (!list) return;
  const keepScroll = !!(opts && opts.keepScroll);
  const scrollTop = keepScroll ? list.scrollTop : 0;
  list.innerHTML = '';
  const groups = _riGroupTaskRows(_riTaskList);
  const anyResolved = groups.some((g) => !!g.ref);
  for (const g of groups) {
    if (anyResolved) list.appendChild(_riGroupHeaderEl(g));
    /* run badges only make sense on true retry runs (same reply); the
     * unresolved bucket's rows are unrelated tasks, not numbered runs. */
    const multi = !!g.ref && g.rows.length > 1;
    g.rows.forEach((row, i) => {
      list.appendChild(_riTaskRowEl(row, {
        runIndex: multi ? i + 1 : 0,
        showTurnChip: !anyResolved,
      }));
      const children = (g.childrenByParent[row.taskId] || [])
        .slice()
        .sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0));
      for (const child of children) {
        list.appendChild(_riTaskRowEl(child, { showTurnChip: false }));
      }
    });
  }
  if (_riHasMore) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'ri-load-earlier';
    more.disabled = _riLoadingEarlier;
    more.textContent = t(_riLoadingEarlier ? 'ri.loading' : 'ri.loadEarlier');
    more.onclick = () => _riLoadEarlierTasks();
    list.appendChild(more);
  }
  if (keepScroll) list.scrollTop = scrollTop;
}

/* Page OLDER persisted rows in (cursor = oldest accumulated createdAt).
 * Live rows never participate: they are always first-page citizens. */
async function _riLoadEarlierTasks() {
  if (!_riOpen || !_riConvId || !_riHasMore || _riLoadingEarlier) return;
  const persisted = _riTaskList.filter((r) => !r.live);
  const cursor = persisted.length
    ? persisted[persisted.length - 1].createdAt || 0 : 0;
  if (!cursor) { _riHasMore = false; _riRenderTaskList(); return; }
  _riLoadingEarlier = true;
  _riRenderTaskList({ keepScroll: true });
  const data = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.byConv(_riConvId, { before: cursor }) : null;
  _riLoadingEarlier = false;
  if (!_riOpen) return;
  if (!data) { _riRenderTaskList({ keepScroll: true }); return; }
  _riHasMore = !!data.hasMore;
  const known = {};
  for (const r of _riTaskList) known[r.taskId] = true;
  const older = (Array.isArray(data.tasks) ? data.tasks : [])
    .filter((r) => r && r.taskId && !known[r.taskId]);
  if (older.length) {
    _riTaskList = _riTaskList.concat(older)
      .sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
    for (const r of older) _riTaskRows[r.taskId] = r;
  }
  _riRenderTaskList({ keepScroll: true });
}

/* ── Level 2: request rows (metadata) for the selected task ── */
async function _riSelectTask(taskId, opts) {
  const silent = !!(opts && opts.silent);
  _riSel.taskId = taskId;
  if (!silent) {
    _riSel.fold = null;
    _riSel.traceOpen = false;
    /* A silent background refresh must NOT reset the detail pane — the
     * user may be reading a round payload or the trace right now. */
    _riResetDetail();
  }
  /* Re-mark the selected task row. */
  const list = _riEl('riTaskList');
  if (list) {
    list.querySelectorAll('.ri-task').forEach((el) => {
      el.classList.toggle('ri-sel', el.dataset.taskId === taskId);
    });
  }
  const rounds = _riEl('riRoundList');
  if (!rounds) return;
  const scrollTop = silent ? rounds.scrollTop : 0;
  if (!silent) {
    rounds.innerHTML = `<div class="ri-empty">${_riEsc(t('ri.loading'))}</div>`;
  }
  const fold = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getRequests(taskId) : null;
  if (!_riOpen || _riSel.taskId !== taskId) return;  // stale response
  _riSel.fold = fold;
  rounds.innerHTML = '';
  if (!fold || !fold.eventsAvailable) {
    const note = document.createElement('div');
    note.className = 'ri-empty';
    if (!fold) {
      /* getRequests resolves null on ANY failure (network, 404, 500) —
       * a load error is NOT "records cleaned up"; offer a retry. */
      note.innerHTML = `<span>${_riEsc(t('ri.loadFailed'))}</span> ` +
        `<button type="button" class="ri-retry" ` +
        `data-tofu-action="riRetryTask()">${_riEsc(t('ri.retry'))}</button>`;
    } else if (fold.readError) {
      /* Storage read FAILED — the honest "expired" empty state would be a
       * lie; say so and offer a retry. */
      note.innerHTML = `<span>${_riEsc(t('ri.loadFailed'))}</span> ` +
        `<button type="button" class="ri-retry" ` +
        `data-tofu-action="riRetryTask()">${_riEsc(t('ri.retry'))}</button>`;
    } else if (_riRowIsLive(_riTaskRows[taskId])) {
      /* Live task before its first persisted snapshot: honest "starting" —
       * the poll and the TurnStore subscription fill rows in as they land. */
      note.textContent = t('ri.starting');
    } else {
      note.textContent = t('ri.expiredHint');
    }
    rounds.appendChild(note);
    return;
  }
  /* Turn Trace entry (耗时分析): the drawer's top-level plain-language
   * entry — the ONE click that answers "where did the time go" for this
   * task, folded server-side (docs/TURN_TRACE_CONTRACT.md). */
  const traceEntry = document.createElement('div');
  traceEntry.className = 'ri-trace-entry';
  traceEntry.setAttribute('role', 'button');
  traceEntry.innerHTML =
    `<span class="ri-trace-label">${_riEsc(t('ri.traceEntry'))}</span>` +
    `<span class="ri-trace-hint">${_riEsc(t('ri.traceEntryHint'))}</span>`;
  traceEntry.onclick = () => _riOpenTrace(taskId);
  rounds.appendChild(traceEntry);
  const technical = document.createElement('details');
  technical.className = 'ri-technical';
  /* The round list IS the drawer's payload — open by default; collapsing
   * is the opt-out for very long tasks, not the other way round. */
  technical.open = true;
  const technicalHead = document.createElement('summary');
  technicalHead.innerHTML = `<span>${_riEsc(t('ri.technicalDetails'))}</span>` +
    `<span class="ri-technical-count">${_riEsc(t('ri.roundTotal', {
      n: fold.requestCount || 0 }))}</span>`;
  technical.appendChild(technicalHead);
  const technicalBody = document.createElement('div');
  technicalBody.className = 'ri-technical-body';
  technical.appendChild(technicalBody);
  rounds.appendChild(technical);
  /* Coverage chip — honest disclosure (design §7). 'flow-untagged':
   * a Flow log whose planner/worker/critic rounds exist but
   * share numbers with no phase tag (ambiguous, NOT uncovered). */
  if (fold.coverage === 'partial') {
    const reasonKey = fold.coverageReason === 'flow-untagged'
      ? 'ri.coverageAmbiguous' : 'ri.coveragePartial';
    const chip = document.createElement('div');
    chip.className = 'ri-coverage-chip';
    chip.innerHTML =
      (typeof Icon === 'function' ? Icon('alertTriangle', 12) : '') +
      ` <span>${_riEsc(t(reasonKey))}</span>`;
    technicalBody.appendChild(chip);
  }
  const reqs = Array.isArray(fold.requests) ? fold.requests : [];
  if (!reqs.length) {
    const emp = document.createElement('div');
    emp.className = 'ri-empty';
    emp.textContent = t('ri.empty');
    technicalBody.appendChild(emp);
  }
  for (const row of reqs) {
    const el = document.createElement('div');
    el.className = 'ri-round';
    el.dataset.round = String(row.roundNum);
    el.dataset.turn = row.turn || '';
    const attempts = Array.isArray(row.attempts) ? row.attempts : [];
    const attemptBits = attempts.map((a) => {
      const el2 = (a.streamElapsedMs / 1000).toFixed(1) + 's';
      const fb = /FALLBACK|REACTIVE|DISCARDED/.test(a.tag || '') ? ' ⚠' : '';
      return `<span class="ri-attempt" title="${_riEsc(a.traceId || '')}">` +
        `${_riEsc(a.tag || a.model)} ${a.tokensIn}→${a.tokensOut} · ${el2}${fb}</span>`;
    }).join('');
    /* Tool-name chips — the SAME glanceability contract as the chat
     * timeline's turn blocks: which tools this round invoked, nothing
     * else. Counts (messages/tokens/schema) stay inside the round's
     * detail pane. */
    const toolNames = Array.isArray(row.toolNames) ? row.toolNames : [];
    const toolChips = toolNames.map((name) =>
      `<span class="ri-tool-chip">${_riEsc(name)}</span>`).join('');
    const turnBadge = row.turn
      ? `<span class="ri-turn-badge">${_riEsc(_riTurnLabel(row))}</span>` : '';
    el.innerHTML =
      `<div class="ri-round-top">` +
      turnBadge +
      `<span class="ri-round-n">${_riEsc(t('ri.roundNumber', {
        n: row.roundNum }))}</span>` +
      `</div>` +
      (toolChips ? `<div class="ri-round-tools">${toolChips}</div>` : '') +
      (attemptBits ? `<div class="ri-round-attempts">${attemptBits}</div>` : '');
    el.onclick = () => _riSelectRound(taskId, row.roundNum, el, row.turn || '');
    technicalBody.appendChild(el);
  }
  if (silent) rounds.scrollTop = scrollTop;
}

function _riShowWireProjection(projection) {
  if (!projection || !Array.isArray(projection.toolNames)) return;
  const content = _riEl('debugContent');
  if (!content) return;
  const details = document.createElement('details');
  details.className = 'ri-coverage-chip ri-wire-projection';
  const summary = document.createElement('summary');
  const bits = [
    t('ri.availableTools', { n: projection.toolCount || 0 }),
    t('ri.wireSchemaTokens', { n: projection.schemaTokens || 0 }),
  ];
  if (projection.backend) bits.push(projection.backend);
  if (projection.schemaBudgetTokens) {
    bits.push(t('ri.wireBudget', { n: projection.schemaBudgetTokens }));
  }
  if (Array.isArray(projection.budgetDroppedNames)
      && projection.budgetDroppedNames.length) {
    bits.push(t('ri.wireDropped', {
      n: projection.budgetDroppedNames.length,
    }));
  }
  summary.textContent = bits.join(' · ');
  const names = document.createElement('pre');
  names.textContent = projection.toolNames.join('\n');
  details.append(summary, names);
  content.prepend(details);
}

function _riRawArchiveText(dataBase64) {
  try {
    const binary = atob(String(dataBase64 || ''));
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
  } catch (_) {
    return t('ri.rawUnavailable');
  }
}

function _riShowRawArchives(taskId, archives) {
  if (!Array.isArray(archives) || !archives.length) return;
  const content = _riEl('debugContent');
  if (!content) return;
  const owner = document.createElement('details');
  owner.className = 'ri-coverage-chip ri-raw-archives';
  const summary = document.createElement('summary');
  summary.textContent = t('ri.rawArchive', { n: archives.length });
  owner.appendChild(summary);
  for (const archive of archives) {
    const item = document.createElement('section');
    item.className = 'ri-raw-archive';
    const meta = document.createElement('div');
    meta.className = 'ri-raw-archive-meta';
    const partial = archive.integrity === 'partial'
      ? ' · ' + t('ri.rawPartial', {
        reason: archive.truncationReason || 'partial' }) : '';
    meta.textContent = `#${archive.transportAttempt || 0} · ` +
      `${archive.storedBytes || 0}/${archive.byteCount || 0} B${partial}`;
    item.appendChild(meta);
    for (const part of ['request', 'response']) {
      const row = document.createElement('div');
      row.className = 'ri-raw-part';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'ri-raw-load';
      button.textContent = t(part === 'request'
        ? 'ri.rawRequest' : 'ri.rawResponse');
      const body = document.createElement('pre');
      body.hidden = true;
      let offset = 0;
      button.onclick = async () => {
        button.disabled = true;
        const chunk = (typeof Api !== 'undefined' && Api.tasks)
          ? await Api.tasks.getRawArchiveChunk(
            taskId, archive.archiveId, part, offset) : null;
        button.disabled = false;
        if (!chunk) {
          body.hidden = false;
          body.textContent = t('ri.rawUnavailable');
          return;
        }
        // Replace, never append: browser residency stays one 256 KiB window
        // even when the durable archive is multi-MiB.
        body.hidden = false;
        body.textContent = _riRawArchiveText(chunk.dataBase64);
        offset = chunk.nextOffset || 0;
        button.textContent = chunk.hasMore
          ? t('ri.rawNext')
          : t(part === 'request' ? 'ri.rawRequest' : 'ri.rawResponse');
        button.disabled = !chunk.hasMore && offset > 0;
      };
      row.append(button, body);
      item.appendChild(row);
    }
    owner.appendChild(item);
  }
  content.prepend(owner);
}

/* ── Level 3: detail — REUSES showMessagesInDebug (no second renderer) ── */

/* Bounded payload cache; live SSE snapshots win over server reads. */
const _riPayloadCache = {};
const _RI_PAYLOAD_CACHE_MAX = 40;
function _riCachePayload(key, payload) {
  if (!_riPayloadCache[key]) {
    const ids = Object.keys(_riPayloadCache);
    if (ids.length >= _RI_PAYLOAD_CACHE_MAX) delete _riPayloadCache[ids[0]];
  }
  _riPayloadCache[key] = payload;
  return payload;
}
async function _riFetchPayload(taskId, roundNum, turn, kind) {
  turn = turn || '';
  kind = kind || 'request';
  const key = taskId + ':' + kind + ':' + turn + ':' + roundNum;
  /* Prefer live snapshots; Flow phases are keyed by turn|roundNum. */
  const _acc = DebugShellState.requests[taskId];
  if (kind === 'state') {
    const st = _acc && (_acc.states || []).filter((s) => s && s.messages &&
      String(s.roundNum) === String(roundNum)).pop();
    if (st) {
      const payload = { messages: st.messages, tools: st.tools,
        label: st.label, model: st.model, params: st.params, kind: 'state' };
      return _riCachePayload(key, payload);
    }
  } else {
    const _accKey = turn ? turn + '|' + roundNum : String(roundNum);
    const acc = _acc && _acc.rounds[_accKey];
    if (acc && acc.messages) {
      const payload = { messages: acc.messages, tools: acc.tools,
        label: acc.label, model: acc.model, params: acc.params,
        turn: acc.turn || turn };
      return _riCachePayload(key, payload);
    }
  }
  /* A live task keeps appending: a cached server read from an earlier
   * poll would freeze the round mid-flight. (Live SSE snapshots above
   * still win — they ARE the newest state.) */
  if (!_riRowIsLive(_riTaskRows[taskId]) && _riPayloadCache[key]) {
    return _riPayloadCache[key];
  }
  const data = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getRequestPayload(taskId, roundNum, turn || undefined,
        kind === 'state' ? 'state' : undefined) : null;
  if (data && data.messages) {
    if (!_riRowIsLive(_riTaskRows[taskId])) {
      _riCachePayload(key, data);
    }
    return data;
  }
  return null;
}

/* Longest exact JSON prefix; divergence safely produces no fold. */
function _riSharedPrefix(prevMsgs, curMsgs) {
  const n = Math.min(prevMsgs.length, curMsgs.length);
  let k = 0;
  while (k < n && JSON.stringify(prevMsgs[k]) === JSON.stringify(curMsgs[k])) k++;
  return k;
}

/* Return only messages appended by this round; null selects the tail fallback. */
async function _riRoundScopedMessages(taskId, roundNum, kind, messages) {
  const num = parseInt(roundNum, 10);
  if (!Number.isFinite(num) || num <= 1 || !Array.isArray(messages)) return null;
  const prev = await _riFetchPayload(taskId, num - 1, '',
    kind === 'state' ? 'state' : 'request');
  if (!prev || !Array.isArray(prev.messages) || !prev.messages.length)
    return null;
  const k = _riSharedPrefix(prev.messages, messages);
  return k > 0 ? messages.slice(k) : null;
}

/* Tail fallback excludes system/history and keeps this round's tool exchange. */
function _riTailSlice(messages) {
  if (!Array.isArray(messages)) return [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m && m.role === 'assistant' &&
        Array.isArray(m.tool_calls) && m.tool_calls.length)
      return messages.slice(i);
  }
  /* Final-answer round: use content after the last user message. */
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i] && messages[i].role === 'user') {
      const tail = messages.slice(i + 1);
      if (tail.length) return tail;
      break;
    }
  }
  /* Last resort: show non-system messages. */
  return messages.filter((m) => m && m.role !== 'system');
}

async function _riSelectRound(taskId, roundNum, el, turn) {
  turn = turn || '';
  _riSel.traceOpen = false;
  _riRevealTechnical();
  const rounds = _riEl('riRoundList');
  if (rounds) {
    rounds.querySelectorAll('.ri-round').forEach((r) =>
      r.classList.toggle('ri-sel', r === el));
    rounds.querySelectorAll('.ri-trace-entry').forEach((r) =>
      r.classList.remove('ri-sel'));
  }
  const payload = await _riFetchPayload(taskId, roundNum, turn);
  // Stale: the user moved on — including into the Turn Trace view, which a
  // late round-payload resolve must not clobber (the reverse race is
  // already guarded in _riOpenTrace).
  if (!_riOpen || _riSel.taskId !== taskId || _riSel.traceOpen) return;
  if (!payload || !payload.messages) return;
  /* Fold against the previous round in the same Flow phase. */
  let opts = { resetScroll: true };
  const num = parseInt(roundNum, 10);
  if (Number.isFinite(num) && num > 1) {
    const prev = await _riFetchPayload(taskId, num - 1, turn);
    if (!_riOpen || _riSel.taskId !== taskId || _riSel.traceOpen) return;  // stale
    if (prev && prev.messages) {
      const k = _riSharedPrefix(prev.messages, payload.messages);
      if (k > 0) { opts.foldPrefix = k; opts.diffBase = 'R' + (num - 1); }
    }
  }
  if (typeof showMessagesInDebug === 'function') {
    _riSetDetailActive(true);
    showMessagesInDebug(payload.messages, payload.label || '', false,
      DebugShellState.activeConversationId,
      payload.tools || undefined, false, undefined,
      Object.assign(opts, { contextManifest: payload.contextManifest || [] }));
    _riShowWireProjection(payload.wireProjection);
    _riShowRawArchives(taskId, payload.rawArchives);
  }
}

/* Open the request that produced a tool call. */
async function openRequestInspectorForToolRound(taskId, roundNum) {
  if (!taskId || roundNum == null) return;
  if (!_riOpen) openRequestInspector();
  await _riSelectTask(taskId);
  _riRevealTechnical();
  const fold = _riSel.fold;
  const reqs = (fold && Array.isArray(fold.requests)) ? fold.requests : [];
  /* Same-numbered Flow phases prefer the worker request. */
  const exact = reqs.filter((r) => String(r.roundNum) === String(roundNum));
  const pick = exact.find((r) => r.turn === 'working') || exact[0] ||
    reqs[reqs.length - 1];
  if (!pick) return;
  const targetTurn = pick.turn || '';
  const el = document.querySelector(
    '#riRoundList .ri-round[data-round="' + String(pick.roundNum) +
    '"][data-turn="' + targetTurn + '"]') ||
    document.querySelector(
      '#riRoundList .ri-round[data-round="' + String(pick.roundNum) + '"]');
  if (el) {
    if (typeof el.scrollIntoView === 'function')
      el.scrollIntoView({ block: 'nearest' });
    el.classList.add('ri-flash');
    setTimeout(() => el.classList.remove('ri-flash'), 1600);
  }
  await _riSelectRound(taskId, pick.roundNum, el, targetTurn);
}

/* ── Tool-row debug panel (ONE view: the post-tool result state) ──────────
 * ONE entry per tool row opening ONE view. The former request | state tab
 * pair was redundant, not two questions: the state mirror for round N is
 * captured AFTER the tool results are appended to the SAME message list the
 * request was built from (lib/tasks_pkg/tool_dispatch/_pipeline.py — same
 * roundNum axis), so the request payload is a strict PREFIX of the mirror.
 * Showing both meant clicking twice to see the same messages minus the
 * results (owner, 2026-07-29: "we don't need both").
 *
 * The request axis survives as a FALLBACK, not a tab: swarm sub-agents
 * persist kind='request' snapshots only (lib/swarm/agent.py has no state
 * emission), so a state-only panel would render "mirror missing" on every
 * sub-agent tool row. `_riFetchRoundView` therefore tries the mirror first
 * and degrades to the request, telling the caller which one it got so the
 * chip can name the axis instead of silently mislabelling it.
 *
 * Mounts right after the tool round's [data-prn] slot and renders through
 * the SAME renderer as the drawer detail (renderDebugBlocksInto — no second
 * JSON renderer). When the tool row is not in the DOM (unloaded/old
 * conversation), degrades to the drawer so the click always lands somewhere
 * meaningful.
 *
 * ROUND-SCOPED (owner, 2026-07-28): renders ONLY what that round appended —
 * the increment over the previous round's same-kind payload — never the full
 * conversation-history dump ("records only for this round of tool calls are
 * sufficient"). The cross-round chip strip was removed with it: one click
 * answers one round; the drawer remains the place for cross-round
 * navigation. */
async function openToolDebugPanel(taskId, roundNum, anchorEl) {
  if (!taskId || roundNum == null) return;
  let slot = (anchorEl && typeof anchorEl.closest === 'function')
    ? anchorEl.closest('[data-prn]') : null;
  if (!slot) {
    const marker = document.querySelector(
      '[data-ri-state="' + String(taskId) + ':' + String(roundNum) + '"]');
    if (marker && typeof marker.closest === 'function')
      slot = marker.closest('[data-prn]');
  }
  if (!slot) {
    /* Tool row not in the DOM (unloaded / old conversation) — degrade to the
     * drawer instead of a dead click, showing the SAME view the inline panel
     * would have: the result state, or the request when no mirror exists. */
    if (!_riOpen) openRequestInspector();
    await _riSelectTask(taskId);
    const view = await _riFetchRoundView(taskId, roundNum);
    if (view && view.payload && view.payload.messages &&
        typeof showMessagesInDebug === 'function') {
      _riSetDetailActive(true);
      showMessagesInDebug(view.payload.messages, view.payload.label || '', false,
        DebugShellState.activeConversationId,
        view.payload.tools || undefined, false, undefined,
        { resetScroll: true,
          contextManifest: view.payload.contextManifest || [] });
      return;
    }
    await openRequestInspectorForToolRound(taskId, roundNum);
    return;
  }
  /* Re-clicking the entry for the round already open closes it (toggle). */
  const existing = document.querySelector('.ri-state-panel');
  if (existing && existing.dataset.riRound === String(roundNum) &&
      existing.dataset.riTask === String(taskId)) {
    existing.remove();
    return;
  }
  _riMountToolPanel(slot, taskId, roundNum);
}

/* Resolve the ONE view a tool row's debug entry shows, and say which axis it
 * came from. The post-tool mirror is preferred because it is a superset of
 * the request; the request is the fallback for rounds that never emitted a
 * mirror (swarm sub-agents, an aborted round, an expired state row).
 * Returns {kind, payload} or null when neither axis has anything. */
async function _riFetchRoundView(taskId, roundNum) {
  const state = await _riFetchPayload(taskId, roundNum, '', 'state');
  if (state && state.messages && state.messages.length)
    return { kind: 'state', payload: state };
  const req = await _riFetchPayload(taskId, roundNum, '', 'request');
  if (req && req.messages && req.messages.length)
    return { kind: 'request', payload: req };
  return null;
}

/* Mount the (single-instance) panel after a tool slot. Transient by
 * design — a chat re-render may drop it; re-click reopens. */
async function _riMountToolPanel(slot, taskId, roundNum) {
  document.querySelectorAll('.ri-state-panel').forEach((p) => p.remove());
  const panel = document.createElement('div');
  panel.className = 'ri-state-panel';
  panel.dataset.riTask = String(taskId);
  panel.dataset.riRound = String(roundNum);
  panel.innerHTML =
    '<div class="ri-state-panel-head">' +
      '<span class="ri-state-panel-kind"></span>' +
      '<span class="ri-state-panel-title"></span>' +
      '<span class="ri-state-panel-close" role="button" tabindex="0" title="' +
        _riEsc(t('ri.stateClose')) + '">' +
        (typeof Icon === 'function' ? Icon('x', 12) : '') + '</span>' +
    '</div>' +
    '<div class="ri-state-body"><div class="ri-empty">' +
      _riEsc(t('ri.loading')) + '</div></div>';
  panel.querySelector('.ri-state-panel-close').onclick = () => panel.remove();
  slot.insertAdjacentElement('afterend', panel);
  if (typeof panel.scrollIntoView === 'function')
    panel.scrollIntoView({ block: 'nearest' });
  await _riRenderToolPanel(panel, taskId, roundNum);
}

/* Render the panel's ONE view: the message mirror captured right after this
 * round's tools ran, or the producing request when that round emitted no
 * mirror. Goes through the shared debug renderer, scoped to this round's
 * increment. The kind chip names the axis on screen, so a fallback render is
 * never mistaken for the mirror. */
async function _riRenderToolPanel(panel, taskId, roundNum) {
  if (!panel.isConnected) return;  // closed while fetching
  panel.dataset.riRound = String(roundNum);
  panel.dataset.riPanel = taskId + ':' + roundNum;
  const body = panel.querySelector('.ri-state-body');
  const titleEl = panel.querySelector('.ri-state-panel-title');
  const kindEl = panel.querySelector('.ri-state-panel-kind');
  const view = await _riFetchRoundView(taskId, roundNum);
  if (!panel.isConnected) return;
  if (!view) {
    panel.dataset.riKind = '';
    if (kindEl) kindEl.textContent = '';
    if (titleEl) titleEl.textContent = 'R' + roundNum;
    if (body) body.innerHTML = '<div class="ri-empty">' +
      _riEsc(t('ri.stateEmpty')) + '</div>';
    return;
  }
  const payload = view.payload;
  panel.dataset.riKind = view.kind;
  if (kindEl) {
    kindEl.textContent = t(view.kind === 'state'
      ? 'ri.tabState' : 'ri.tabRequest');
    kindEl.classList.toggle('ri-kind-fallback', view.kind !== 'state');
    kindEl.title = t(view.kind === 'state'
      ? 'ri.stateKindTip' : 'ri.requestKindTip');
  }
  /* Round-scoped: only what THIS round appended (see the section header).
   * When no exact increment exists, a state mirror degrades to its TAIL
   * slice (the tool call + its results) — never the full payload, whose
   * system prompt + history is precisely what this panel must not dump.
   * The request axis (the mirror-less fallback) keeps the full payload: it
   * has no post-tool tail to slice. */
  const scoped = await _riRoundScopedMessages(taskId, roundNum, view.kind,
    payload.messages);
  if (!panel.isConnected) return;
  const shown = (Array.isArray(scoped) && scoped.length)
    ? scoped
    : (view.kind === 'state'
      ? _riTailSlice(payload.messages) : payload.messages);
  if (titleEl) titleEl.textContent =
    (payload.label || ('R' + roundNum)) + ' · +' + shown.length + ' msgs';
  if (body) {
    renderDebugBlocksInto(body, shown, null);
    /* 2026-08-05 owner: NO tools-schema block here — it is identical on
     * every round and pure noise next to one round's increment (the drawer
     * detail keeps it: there it is part of the request payload). Small
     * increments auto-expand so the panel answers at a glance; large
     * payloads stay collapsed and render on click. */
    let total = 0;
    for (const m of shown)
      total += (typeof _debugMsgChars === 'function') ? _debugMsgChars(m) : 0;
    if (shown.length <= 6 && total <= 300 * 1024 &&
        typeof _debugOpenBlock === 'function') {
      body.querySelectorAll('.debug-msg-block').forEach(
        (b) => _debugOpenBlock(b));
    }
  }
}

/* ── Turn Trace (耗时分析) — the flame-graph view of ONE task ───────────
 * docs/TURN_TRACE_CONTRACT.md. The drawer renders ONLY what
 * /api/v1/tasks/<id>/trace folds SERVER-side spans from authoritative events
 * and returns the durable terminal snapshot when reconstructible rows expire.
 * The browser contributes only explicit received/painted/transport receipts;
 * it never rewrites server span clocks or lifecycle facts.
 * Layout: one row per span depth (turn / rounds & waits / llm & tools /
 * sub-segments) + a final gray row for the explicitly-unattributed gaps.
 */
function _trFmtMs(ms) {
  if (ms == null || !Number.isFinite(Number(ms))) return '…';
  ms = Math.max(0, Math.round(Number(ms)));
  if (ms < 1000) return ms + 'ms';
  const s = ms / 1000;
  if (s < 60) return (s < 10 ? s.toFixed(1) : String(Math.round(s))) + 's';
  const m = Math.floor(s / 60);
  return m + 'm' + String(Math.round(s % 60)).padStart(2, '0') + 's';
}

const _TR_KIND_I18N = {
  turn: 'ri.trKindTurn', round: 'ri.trKindRound', llm: 'ri.trKindLlm',
  llm_ttft: 'ri.trKindLlmTtft', tool: 'ri.trKindTool',
  retry_wait: 'ri.trKindRetryWait', compaction: 'ri.trKindCompaction',
  approval_wait: 'ri.trKindApprovalWait', spawn_wait: 'ri.trKindSpawnWait',
};

function _trKindLabel(kind) {
  const key = _TR_KIND_I18N[kind];
  const v = key ? t(key) : '';
  return (v && v !== key) ? v : String(kind || '');
}

function _trBarTitle(sp, elapsed) {
  const lines = [
    `${sp.name || sp.kind} · ${_trKindLabel(sp.kind)} · ${_trFmtMs(elapsed)}`,
  ];
  const a = sp.attrs || {};
  if (a.query) lines.push(a.query);
  if (a.model) lines.push(a.model);
  if (Array.isArray(a.attempts)) {
    for (const at of a.attempts) {
      lines.push(`${at.tag || at.model || 'attempt'}: ` +
        `${at.tokensIn}→${at.tokensOut} · ${_trFmtMs(at.streamElapsedMs)}`);
    }
  }
  if (a.attempt != null) lines.push(t('ri.trTipAttempt', { n: a.attempt }));
  if (a.verdict) lines.push(a.verdict);
  if (sp.budgetMs != null) {
    lines.push(sp.overBudget
      ? t('ri.trTipOverBudget', {
          budget: _trFmtMs(sp.budgetMs),
          over: _trFmtMs(Math.max(0, elapsed - sp.budgetMs)) })
      : t('ri.trTipBudget', { budget: _trFmtMs(sp.budgetMs) }));
  }
  if (sp.truncated) lines.push(t('ri.trTipTruncated'));
  if (sp.status === 'running') lines.push(t('ri.trTipRunning'));
  return lines.join('\n');
}

function _trStatusLabel(status) {
  const key = String((status && status.detailKey) || '');
  if (key) {
    const translated = t(key, (status && status.detailArgs) || {});
    if (translated && translated !== key) return translated;
  }
  return String((status && (status.detail || status.phase)) || '');
}

function _trObservationLabel(observation) {
  const o = observation || {};
  if (o.kind === 'phase_painted') {
    return t('ri.tracePhasePainted', {
      phase: o.phase || o.detailKey || '—',
      render: _trFmtMs(o.renderMs),
    });
  }
  if (o.kind === 'terminal_painted') {
    return t('ri.traceTerminalPainted', { render: _trFmtMs(o.renderMs) });
  }
  if (o.kind === 'transport_degraded') {
    return t('ri.traceTransportDegraded', {
      state: o.healthState || 'degraded', reason: o.reason || '—',
    });
  }
  if (o.kind === 'transport_recovered') {
    return t('ri.traceTransportRecovered', {
      duration: _trFmtMs(o.durationMs),
    });
  }
  return String(o.kind || '');
}

async function _riOpenTrace(taskId) {
  _riSel.traceOpen = true;
  const rounds = _riEl('riRoundList');
  if (rounds) {
    rounds.querySelectorAll('.ri-round').forEach((r) =>
      r.classList.remove('ri-sel'));
    rounds.querySelectorAll('.ri-trace-entry').forEach((r) =>
      r.classList.add('ri-sel'));
  }
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (title) title.textContent = t('ri.traceTitle');
  if (content) {
    content.innerHTML = `<div class="ri-main-empty">` +
      `${_riEsc(t('ri.loading'))}</div>`;
  }
  _riSetDetailActive(true);
  const doc = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getTrace(taskId) : null;
  if (!_riOpen || _riSel.taskId !== taskId || !_riSel.traceOpen) return;
  _riRenderTrace(doc);
}

function _riRenderTrace(doc) {
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (!content) return;
  if (title) title.textContent = t('ri.traceTitle');
  if (!doc || !doc.eventsAvailable) {
    /* null = the fetch failed (retry via the entry); eventsAvailable:false
     * = the event log really is gone (30d retention). Say which one it is. */
    content.innerHTML = `<div class="ri-main-empty">` +
      `${_riEsc(t(doc ? 'ri.expiredHint' : 'ri.loadFailed'))}</div>`;
    return;
  }
  const t0 = Number(doc.tStart) || 0;
  const total = Math.max(1, Number(doc.totalMs) || 1);
  const s = doc.summary || {};
  const observations = Array.isArray(doc.clientObservations)
    ? doc.clientObservations : [];
  const hasServerTimeline = doc.tStart != null && doc.totalMs != null &&
    doc.summary && typeof doc.summary === 'object';

  /* Summary chips — the disjoint bucket partition (sums to totalMs by
   * contract), each chip colored like its flame kind. */
  const chips = [];
  const pushChip = (cls, label, val, sub) => {
    if (val == null) return;
    chips.push(
      `<span class="tr-chip ${cls}">` +
      `<span class="tr-chip-dot" aria-hidden="true"></span>` +
      `${_riEsc(label)} <b>${_riEsc(_trFmtMs(val))}</b>` +
      (sub ? ` <i>${_riEsc(sub)}</i>` : '') + `</span>`);
  };
  pushChip('tr-c-total', t('ri.traceTotal'), doc.totalMs);
  pushChip('tr-c-llm', t('ri.trKindLlm'), s.llmMs,
    s.ttftMs ? t('ri.traceTtftSub', { v: _trFmtMs(s.ttftMs) }) : '');
  pushChip('tr-c-tool', t('ri.trKindTool'), s.toolMs);
  if (s.waitMs) pushChip('tr-c-wait', t('ri.trKindRetryWait'), s.waitMs);
  if (s.compactionMs) {
    pushChip('tr-c-compact', t('ri.trKindCompaction'), s.compactionMs);
  }
  if (s.approvalWaitMs) {
    pushChip('tr-c-approval', t('ri.trKindApprovalWait'), s.approvalWaitMs);
  }
  if (s.unattributedMs) {
    pushChip('tr-c-gap', t('ri.trKindGap'), s.unattributedMs);
  }

  let html = `<div class="tr-wrap"><div class="tr-chips">${chips.join('')}</div>`;

  /* Honesty notes (the Request Inspector disclosure precedent). */
  if (doc.running) {
    html += `<div class="tr-note">${_riEsc(t('ri.traceLive'))}</div>`;
  }
  if (doc.source === 'turn-snapshot') {
    html += `<div class="tr-note">${_riEsc(t('ri.traceDurable'))}</div>`;
  }
  if (doc.source === 'attempt-receipts' && !hasServerTimeline) {
    html += `<div class="tr-note tr-note-warn">` +
      `${_riEsc(t('ri.traceReceiptsOnly'))}</div>`;
  }
  if (doc.coverage === 'partial') {
    const key = doc.coverageReason === 'flow'
      ? 'ri.tracePartialFlow' : 'ri.tracePartialLegacy';
    html += `<div class="tr-note tr-note-warn">${_riEsc(t(key))}</div>`;
  }
  const over = Array.isArray(s.overBudget) ? s.overBudget : [];
  if (over.length) {
    const items = over.map((o) =>
      `${_riEsc(o.name)} ${_riEsc(_trFmtMs(o.elapsedMs))}` +
      ` / ${_riEsc(_trFmtMs(o.budgetMs))}`).join(' · ');
    html += `<div class="tr-note tr-note-over">` +
      `${_riEsc(t('ri.traceOverBudget', { n: over.length }))}: ${items}</div>`;
  }
  const compactedCount = ['droppedSpans', 'droppedGaps',
    'statusDroppedCount', 'clientObservationDroppedCount',
    'overBudgetDroppedCount']
    .reduce((sum, key) => sum + Math.max(0, Number(doc[key]) || 0), 0);
  if (doc.compacted || compactedCount) {
    html += `<div class="tr-note tr-note-warn">` +
      `${_riEsc(t('ri.traceCompacted', { n: compactedCount }))}</div>`;
  }
  const clientReportedDrops = observations.reduce((maximum, observation) =>
    Math.max(maximum, Math.max(0, Number(
      observation && observation.clientDroppedBefore) || 0)), 0);
  if (clientReportedDrops) {
    html += `<div class="tr-note tr-note-warn">` +
      `${_riEsc(t('ri.traceClientDropped', { n: clientReportedDrops }))}</div>`;
  }

  /* Flame rows require server clocks. A receipt-only durable document must
   * show its browser evidence without inventing a 0ms server timeline. */
  if (hasServerTimeline) {
    const spans = Array.isArray(doc.spans) ? doc.spans : [];
    const gaps = Array.isArray(doc.gaps) ? doc.gaps : [];
    let maxDepth = 0;
    for (const sp of spans) maxDepth = Math.max(maxDepth, sp.depth || 0);
    maxDepth = Math.min(maxDepth, 3);
    const rowLabelKeys = ['ri.trRowTurn', 'ri.trRowPhase', 'ri.trRowDetail',
      'ri.trRowSub'];
    const barHtml = (sp) => {
      const a = sp.tStart == null ? t0 : sp.tStart;
      const b = sp.tEnd == null ? (t0 + total) : sp.tEnd;
      const elapsed = Math.max(0, b - a);
      const left = Math.min(100, Math.max(0, (a - t0) / total * 100));
      const width = Math.min(100 - left,
        Math.max(0.15, (b - a) / total * 100));
      const cls = 'tr-bar tr-k-' + (sp.kind || 'unknown') +
        (sp.status === 'error' || sp.status === 'aborted' ? ' tr-err' : '') +
        (sp.overBudget ? ' tr-over' : '') +
        (sp.status === 'running' ? ' tr-live' : '') +
        (sp.truncated ? ' tr-trunc' : '');
      const inner = width > 7
        ? `<span class="tr-bar-txt">${_riEsc(sp.name || '')} ` +
          `${_riEsc(_trFmtMs(elapsed))}</span>` : '';
      return `<div class="${cls}" style="left:${left.toFixed(3)}%;` +
        `width:${width.toFixed(3)}%" title="${_riEsc(_trBarTitle(sp, elapsed))}">` +
        `${inner}</div>`;
    };
    html += '<div class="tr-flame">';
    for (let d = 0; d <= maxDepth; d++) {
      html += `<div class="tr-row"><span class="tr-row-label">` +
        `${_riEsc(t(rowLabelKeys[d]))}</span><div class="tr-track">`;
      for (const sp of spans) {
        if ((sp.depth || 0) === d) html += barHtml(sp);
      }
      html += '</div></div>';
    }
    /* The unattributed row — ALWAYS rendered when gaps exist, so a hole in
     * the accounting is visible, never silent (contract invariant #2). */
    if (gaps.length) {
      html += `<div class="tr-row"><span class="tr-row-label">` +
        `${_riEsc(t('ri.trKindGap'))}</span><div class="tr-track">`;
      for (const g of gaps) {
        const left = Math.min(100, Math.max(0, (g.tStart - t0) / total * 100));
        const width = Math.min(100 - left,
          Math.max(0.15, (g.tEnd - g.tStart) / total * 100));
        html += `<div class="tr-bar tr-k-gap" style="left:${left.toFixed(3)}%;` +
          `width:${width.toFixed(3)}%" title="` +
          `${_riEsc(t('ri.trKindGap') + ' · ' + _trFmtMs(g.tEnd - g.tStart))}">` +
          `</div>`;
      }
      html += '</div></div>';
    }
    /* Axis: 0 / mid / total. */
    html += `<div class="tr-axis"><span>0</span>` +
      `<span>${_riEsc(_trFmtMs(total / 2))}</span>` +
      `<span>${_riEsc(_trFmtMs(total))}</span></div>`;
    html += '</div>';
  }

  /* Exact user-visible phase prompts. Repeated heartbeats are coalesced by
   * the server, while count and lastObservedAt preserve the fact that the
   * same prompt remained active. */
  const statuses = Array.isArray(doc.statusHistory) ? doc.statusHistory : [];
  if (statuses.length) {
    const statusList = statuses.map((status) => {
      const end = status.tEnd == null
        ? (status.lastObservedAt == null ? t0 + total : status.lastObservedAt)
        : status.tEnd;
      const duration = Math.max(0, Number(end) - Number(status.tStart || end));
      const repeated = Number(status.count) > 1
        ? ` ${t('ri.traceStatusRepeated', { n: status.count })}` : '';
      return `${_trStatusLabel(status)} ${_trFmtMs(duration)}${repeated}`;
    });
    html += `<div class="tr-note"><b>${_riEsc(t('ri.traceStatusHistory'))}</b> ` +
      `${statusList.map(_riEsc).join(' → ')}</div>`;
    html += `<div class="tr-flame"><div class="tr-row">` +
      `<span class="tr-row-label">${_riEsc(t('ri.tracePromptRow'))}</span>` +
      `<div class="tr-track">`;
    for (const status of statuses) {
      const a = Number(status.tStart) || t0;
      const b = status.tEnd == null
        ? (Number(status.lastObservedAt) || t0 + total) : Number(status.tEnd);
      const elapsed = Math.max(0, b - a);
      const left = Math.min(100, Math.max(0, (a - t0) / total * 100));
      const width = Math.min(100 - left,
        Math.max(0.15, elapsed / total * 100));
      const cls = status.attention === 'stall'
        ? 'tr-bar tr-k-retry_wait tr-err'
        : status.attention === 'wait'
          ? 'tr-bar tr-k-retry_wait' : 'tr-bar tr-k-round';
      const label = _trStatusLabel(status);
      const inner = width > 7
        ? `<span class="tr-bar-txt">${_riEsc(label)}</span>` : '';
      html += `<div class="${cls}" style="left:${left.toFixed(3)}%;` +
        `width:${width.toFixed(3)}%" title="${_riEsc(label + ' · ' +
          _trFmtMs(elapsed))}">${inner}</div>`;
    }
    html += '</div></div></div>';
  }

  /* Browser evidence is intentionally content-free: it says when a known
   * phase/terminal/connection state was painted, never what the answer said. */
  if (observations.length) {
    const evidence = observations.map((observation) => {
      let label = _trObservationLabel(observation);
      if (observation.transportMs != null) {
        label += ` · ${t('ri.traceClientTransport', {
          duration: _trFmtMs(observation.transportMs),
        })}`;
      }
      return label;
    });
    html += `<div class="tr-note"><b>${_riEsc(t('ri.traceClientEvidence'))}</b> ` +
      `${evidence.map(_riEsc).join(' · ')}</div>`;
  }
  html += '</div>';
  content.innerHTML = html;
}

// BEGIN GENERATED LAZY RUNTIME PORTS — diagnostics-presenters
runtimeScope.DebugPresentationState = DebugPresentationState;
runtimeScope.openRequestInspectorForTask = openRequestInspectorForTask;
// END GENERATED LAZY RUNTIME PORTS
// BEGIN GENERATED LAZY RUNTIME ACTIONS — diagnostics-presenters
runtimeScope.closeDebug = closeDebug;
runtimeScope.copyDebugContent = copyDebugContent;
runtimeScope.openToolDebugPanel = openToolDebugPanel;
runtimeScope.riRefreshTasks = riRefreshTasks;
runtimeScope.riRetryTask = riRetryTask;
runtimeScope.toggleDebug = toggleDebug;
// END GENERATED LAZY RUNTIME ACTIONS

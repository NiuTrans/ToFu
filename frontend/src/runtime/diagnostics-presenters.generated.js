// @ts-check
/* Generated lazy retained runtime: diagnostics-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { createInspectorFeed } from '../features/inspector-feed';
import { renderInspectorLoading, renderInspectorFailure, renderInspectorBack } from '../features/inspector-pane-view';
import { createDiagnosticsMessageReconciler } from '../features/diagnostics-message-reconciliation';
import { createInspectorSession } from '../features/inspector-session';
import { createInspectorPayloads } from '../features/inspector-payloads';
import { createInspectorInline } from '../features/inspector-inline';
import { createInspectorNavigation } from '../features/inspector-navigation';
import { renderInspectorTrace } from '../features/inspector-trace-view';
import { createInspectorDetails } from '../features/inspector-details';
import { t } from '../i18n/index';
import { escapeHtml } from '../html-safety';
import { createInspectorDisclosure, syncInspectorDisclosure } from '../features/diagnostics-message-disclosure';

const Api = runtimeScope.Api;
if (!Api || typeof Api !== 'object') throw new Error('diagnostics-presenters runtime dependency is unavailable: Api');
const DebugShellState = runtimeScope.DebugShellState;
if (!DebugShellState || typeof DebugShellState !== 'object') throw new Error('diagnostics-presenters runtime dependency is unavailable: DebugShellState');
const Icon = runtimeScope.Icon;
if (typeof Icon !== 'function') throw new Error('diagnostics-presenters runtime dependency is unavailable: Icon');
const debugLog = runtimeScope.debugLog;
if (typeof debugLog !== 'function') throw new Error('diagnostics-presenters runtime dependency is unavailable: debugLog');
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
// Called on conversation switch.
//
// 2026-09-22 (flat wire feed): the open drawer owns the pane outright —
// _riOnConvSwitch loads the server-folded flat wire feed for the new
// conversation. The legacy cold path it replaces (cache → /debug-messages
// approximation) is retired: the feed shows the REAL per-turn wires, so
// the approximated reconstruction no longer has a pane to fill. A closed
// drawer stays API-idle, exactly as before.
function restoreDebugForConv(convId) {
  _riOnConvSwitch(convId);
}
/* Patch the cached messages snapshot when a `tool_compacted` frame lands
 * mid-round. Without this the panel keeps showing the pre-compaction blob
 * (e.g. a 100 KB tool dump) until the next messages_snapshot — which may
 * never arrive if the task ends or pauses. Mirrors the durable-frame handler
 * in conversation_turn_store.js. */
function _patchDebugCompaction(convId, ev) {
  const cached = convId && DebugShellState.cache[convId];
  if (!cached || !Array.isArray(cached.messages)
      || ev.compactedContent == null) return false;
  let patched = false;
  for (const m of cached.messages) {
    if (m && m.role === 'tool' && m.tool_call_id === ev.toolCallId) {
      m.content = ev.compactedContent;
      m._compactionLayer = ev.compactionLayer || 'L1';
      m._compactedFromChars = ev.compactedFromChars;
      m._compactedToChars = ev.compactedToChars;
      m._toolTokens = ev.toolTokens;
      patched = true;
      break;
    }
  }
  if (patched && convId === DebugShellState.activeConversationId) {
    showMessagesInDebug(
      cached.messages, cached.label, /* isUpdate */ true, convId,
      cached.tools, cached.approx, undefined,
      { contextManifest: cached.contextManifest || [],
        deferRender: (typeof _riFeedOwnsPane === 'function' &&
          _riFeedOwnsPane()) });
  }
  return patched;
}

/* ── Message-block render helpers (module scope) ─────────────────────
 * Hoisted out of showMessagesInDebug so the request-inspector INLINE state
 * panel renders a state snapshot through the exact same code path as the
 * debug drawer — one renderer, two containers. */
// Helper: syntax-color JSON (full, no truncation)
function colorJson(obj, depth) {
  if (depth === undefined) depth = 0;
  // Tool results are external evidence. Cap both recursion and sibling
  // expansion; the untouched payload is still available via Raw/Copy.
  if (depth > 32) return '<span class="debug-null">…</span>';
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
    let items = obj.slice(0, 200).map((v) =>
      indent + "  " + colorJson(v, depth + 1));
    if (obj.length > 200) items.push(indent + "  …");
    return "[\n" + items.join(",\n") + "\n" + indent + "]";
  }
  if (typeof obj === "object") {
    const keys = Object.keys(obj);
    if (keys.length === 0) return "{}";
    let lines = keys.slice(0, 200).map(
      (k) =>
        indent +
        "  " +
        '<span class="debug-key">"' +
        escapeHtml(k) +
        '"</span>: ' +
        colorJson(obj[k], depth + 1),
    );
    if (keys.length > 200) lines.push(indent + "  …");
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
  syncInspectorDisclosure(block);
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
  const header = createInspectorDisclosure();
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
    syncInspectorDisclosure(block);
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
/* Shared typed reconciliation; remove this port when the card renderer moves
 * to native Diagnostics. Reads, view selection and scrolling remain here. */
const _debugMessageRenderer = createDiagnosticsMessageReconciler({
  create: createBlock,
  identity: _debugMsgIdentity,
  summarize: msgSummary,
  open: _debugOpenBlock,
});
function reconcileDebugMessages(container, messages, options) {
  _debugMessageRenderer.render(container, messages, options);
}
/* Inline payload panels deliberately start a fresh view. */
function renderDebugBlocksInto(p, messages, opts) {
  reconcileDebugMessages(p, messages, { reuse: false });
}
/* Create the collapsible TOOLS block skeleton (header + lazy body). The body
 * renders from toolsBlock._toolsRef on first open. */
function _createDebugToolsBlock() {
  const toolsBlock = document.createElement('div');
  toolsBlock.className = 'debug-tools-block debug-msg-block';
  const tHeader = createInspectorDisclosure();
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
    syncInspectorDisclosure(toolsBlock);
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
  const header = createInspectorDisclosure();
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
    syncInspectorDisclosure(block);
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
  // Seed the live delta chain from this full frame. Rendered history is
  // chronological, so an older round cannot replace a newer chain state.
  if (meta && typeof meta === "object") {
    _rememberDebugSnapshot(cid, meta, messages, tools);
  }
  // Cache for conversation switching
  if (cid) {
    DebugShellState.cache[cid] = { messages, label };
    if (tools) DebugShellState.cache[cid].tools = tools;
    DebugShellState.cache[cid].approx = !!approx;
    DebugShellState.cache[cid].contextManifest =
      (meta && meta.contextManifest) ||
      (opts && opts.contextManifest) || [];
  }
  /* opts.deferRender: the flat wire feed owns the pane while the drawer is
   * open (2026-09-22) — evidence frames still record into the per-task log
   * and conv cache above (the inline tool panel reads them), but must not
   * paint over the feed. */
  if (opts && opts.deferRender) return;
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
    const leading = Array.from(p.childNodes).filter((node) =>
      node.nodeType === 1 && node.matches('.debug-tools-block, .debug-context-block'));
    reconcileDebugMessages(p, messages, { reuse: 'position', leading });
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
      syncInspectorDisclosure(_tb);
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
      syncInspectorDisclosure(_cb);
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

/* Live message snapshots above the inline cap arrive as the same shared
 * prefix delta the durable event log stores. Keep one bounded chain per
 * (conversation, task, turn) so the panel can rebuild them without a second
 * full-frame transport. Cold history seeds the chain through
 * showMessagesInDebug; a delta without a baseline is ignored until the next
 * full frame. */
const _DEBUG_SNAPSHOT_CHAIN_LIMIT = 8;
const _debugSnapshotChains = new Map();

function _debugSnapshotChainKey(conversationId, meta) {
  return `${conversationId || ''}\u0000${(meta && meta.taskId) || ''}\u0000${(meta && meta.turn) || ''}`;
}

function _debugSnapshotOrder(meta) {
  const round = Number(meta && meta.roundNum) || 0;
  return round * 2 + (meta && meta.kind === 'state' ? 1 : 0);
}

function _rememberDebugSnapshot(conversationId, meta, messages, tools) {
  if (!Array.isArray(messages) || !meta || !meta.taskId) return;
  const key = _debugSnapshotChainKey(conversationId, meta);
  const order = _debugSnapshotOrder(meta);
  const previous = _debugSnapshotChains.get(key);
  if (previous && order < previous.order) return;
  _debugSnapshotChains.delete(key);
  _debugSnapshotChains.set(key, {
    messages,
    tools: Array.isArray(tools) ? tools : (previous && previous.tools) || null,
    order,
  });
  while (_debugSnapshotChains.size > _DEBUG_SNAPSHOT_CHAIN_LIMIT) {
    _debugSnapshotChains.delete(_debugSnapshotChains.keys().next().value);
  }
}

function _resolveDebugSnapshotFrame(conversationId, frame) {
  if (!frame || frame.type !== 'messages_snapshot') return frame;
  if (Array.isArray(frame.messages)) {
    _rememberDebugSnapshot(conversationId, frame, frame.messages, frame.tools);
    return frame;
  }
  if (frame.prefixLen === undefined) return frame;
  const state = _debugSnapshotChains.get(
    _debugSnapshotChainKey(conversationId, frame));
  const prefixLen = Math.max(0, Number(frame.prefixLen) || 0);
  if (!state && prefixLen > 0) return null;
  const base = state ? state.messages : [];
  // A prefix longer than the cached baseline means an intervening frame was
  // missed; showing a clamped tail would silently corrupt the view.
  if (prefixLen > base.length) return null;
  const tail = Array.isArray(frame.newMessages) ? frame.newMessages : [];
  const messages = base.slice(0, prefixLen).concat(tail);
  const tools = Array.isArray(frame.tools) ? frame.tools : (state && state.tools);
  _rememberDebugSnapshot(conversationId, frame, messages, tools);
  return { ...frame, messages, tools: tools || undefined };
}

/* Optional lifecycle port. Retained conversation navigation consults it only
 * after this owner has loaded, so ordinary navigation never imports the chunk
 * or starts a diagnostics request. */
const DebugPresentationState = Object.freeze({
  clear: clearDebug,
  onConversationSwitch: restoreDebugForConv,
  /* Durable evidence frames (turn_lifecycle _DEBUG_EVIDENCE_EVENTS), fed by
   * conversation_turn_store.js onDebugFrame. messages_snapshot: the live wire
   * form the model just saw — re-feeds the cache + per-task Request
   * Inspector round log. tool_compacted: patches the cached placeholder. */
  ingestEvidenceFrame(conversationId, frame) {
    if (!frame || typeof frame !== 'object') return;
    const resolved = _resolveDebugSnapshotFrame(conversationId, frame);
    if (resolved && resolved.type === 'messages_snapshot'
        && Array.isArray(resolved.messages)) {
      const label = resolved.label || t('stream.roundMessages',
        { round: resolved.roundNum, n: resolved.messageCount });
      showMessagesInDebug(
        resolved.messages, label, /* isUpdate */ true, conversationId,
        resolved.tools || undefined, /* approx */ false,
        { kind: resolved.kind, model: resolved.model, params: resolved.params,
          roundNum: resolved.roundNum, turn: resolved.turn,
          taskId: resolved.taskId,
          contextManifest: resolved.contextManifest },
        { deferRender: (typeof _riFeedOwnsPane === 'function' &&
            _riFeedOwnsPane()) },
      );
    } else if (frame.type === 'tool_compacted') {
      _patchDebugCompaction(conversationId, frame);
    }
  },
});
/* ===== migrated source: core/request_inspector.js ===== */
/* Request Inspector — the FLAT WIRE FEED (2026-09-22 owner directive).
 *
 * The drawer renders ONE flat message list for the whole conversation —
 * every message exactly once, from the server fold
 * (lib/tasks_pkg/request_inspector.get_conv_inspector_wire). Turn, agent
 * and rebuild sections render as dividers; the turn nav is a SCROLL
 * model (the conversation bar's turn-nav-label pattern): jumping never
 * switches or loads a view, it scrolls to the divider. The drill-down it
 * replaced (task list → round list → single-round detail) is gone:
 * retries collapse to the newest run server-side, and per-round
 * debugging lives in the INLINE tool panel (openToolDebugPanel) and the
 * per-turn Turn Trace, both kept below. */

/* Narrow ports into native lifecycle, detail and payload owners. Remove
 * these bridges when the retained markup migrates; no duplicate read state. */
const _riSession = createInspectorSession({
  conversation: () => DebugShellState.activeConversationId || null,
  content: () => _riEl('debugContent'),
  hidden: () => document.hidden || runtimeScope.nativeVisibility?.isHidden() === true,
  live: () => _riFeed.live,
  load: (convId) => _riFeed.load(convId, true),
  fingerprint: (convId) => _riStoreFingerprint(convId),
  subscribe: (convId, changed) => {
    const store = convId && runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(convId);
    return store && typeof store.subscribe === 'function' ? store.subscribe(changed) : null;
  },
  failed: (error) => debugLog('[ri] refresh failed: ' + String(error), 'warn'),
});
const _riNavigation = createInspectorNavigation({
  element: (id) => _riEl(id), scroller: () => _riFeedScroller(),
  feed: () => _riSession.isOpen && _riSession.paneKind === 'feed',
  rows: () => _riFeed.turns,
  orderedTurns: () => runtimeScope.ConversationTurnRead?.ordered?.(_riSession.conversationId) || [],
  translate: (key, params) => t(key, params),
  relativeTime: (time) => _riRelTime(time), absoluteTime: (time) => _riAbsTime(time),
  showTurn: (id) => _riScrollToTurn(id),
});
const _riFeed = createInspectorFeed(_riSession, {
  read: (convId) => (typeof Api !== 'undefined' && Api.tasks && typeof Api.tasks.convWire === 'function')
    ? Api.tasks.convWire(convId) : Promise.resolve(null),
  content: () => _riEl('debugContent'), title: () => _riEl('debugTitle'), scroller: () => _riFeedScroller(),
  navigation: _riNavigation, translate: (key, params) => t(key, params),
  reconcile: (node, messages, options) => reconcileDebugMessages(node, messages, options),
  ordinal: (turn) => _riNavigation.ordinal(turn),
  relativeTime: (time) => _riRelTime(time), absoluteTime: (time) => _riAbsTime(time),
  showTurn: (turn) => _riScrollToTurn(turn), trace: (task) => _riOpenTrace(task),
});
const _riPayloads = createInspectorPayloads({
  snapshots: (taskId) => DebugShellState.requests[taskId],
  live: (taskId) => {
    const row = _riFeed.task(taskId);
    return !!(row && (row.live || ['running', 'queued', 'pending'].includes(String(row.status || '').toLowerCase())));
  },
  read: (taskId, roundNum, turn, kind, fold) =>
    (typeof Api !== 'undefined' && Api.tasks)
      ? Api.tasks.getRequestPayload(taskId, roundNum, turn, kind, fold) : Promise.resolve(null),
  log: (key, message, level) => _riFetchLog(key, message, level),
});
const _riDetails = createInspectorDetails(_riSession, {
  trace: (taskId) => (typeof Api !== 'undefined' && Api.tasks)
    ? Api.tasks.getTrace(taskId) : Promise.resolve(null),
  round: (taskId, roundNum, turn) => _riPayloads.round(taskId, roundNum, turn),
  loading: (kind) => _riDetailLoading(kind),
  showTrace: (doc) => renderInspectorTrace(_riEl('debugContent'), _riEl('debugTitle'), doc, t),
  messages: (payload) => {
    if (typeof showMessagesInDebug !== 'function') return false;
    showMessagesInDebug(payload.messages, payload.label || '', false,
      DebugShellState.activeConversationId, payload.tools || undefined, false, undefined,
      { resetScroll: true, contextManifest: payload.contextManifest || [] });
    return true;
  },
  back: (pane) => _riShowFeedBackBar(pane),
  failed: (retry) => _riDetailFailed(retry),
  feed: () => _riFeed.render(),
  land: (taskId, roundNum) => openRequestInspectorForToolRound(taskId, roundNum),
  log: (error) => debugLog('[ri] detail read failed: ' + String(error), 'warn'),
});
const _riInline = createInspectorInline({
  round: (taskId, roundNum, turn) => _riPayloads.round(taskId, roundNum, turn),
  increment: (taskId, roundNum, kind, messages, turn) =>
    _riPayloads.increment(taskId, roundNum, kind, messages, turn),
  detached: (taskId, roundNum, turn) => {
    if (!_riSession.isOpen) openRequestInspector();
    return _riDetails.detached(taskId, roundNum, turn);
  },
  translate: (key) => t(key),
  render: (node, messages) => renderDebugBlocksInto(node, messages, null),
  characters: (message) => typeof _debugMsgChars === 'function' ? _debugMsgChars(message) : 0,
  expand: (node) => { if (typeof _debugOpenBlock === 'function') _debugOpenBlock(node); },
  failed: (error) => debugLog('[ri] inline read failed: ' + String(error), 'warn'),
});

function _riStoreFingerprint(convId) {
  try {
    const read = runtimeScope.ConversationTurnRead;
    if (!read || !convId || !read.state) return '';
    const state = read.state(convId);
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

function toggleRequestInspector() {
  if (_riSession.isOpen) closeRequestInspector();
  else openRequestInspector();
}

function openRequestInspector() {
  _riSession.open(DebugShellState.activeConversationId || null);
  _riNavigation.open();
  DebugShellState.visible = true;
  document.body.classList.add('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'flex';
  _riFeed.select(_riSession.conversationId);
  void _riFeed.load(_riSession.conversationId);
}

function closeRequestInspector() {
  _riNavigation.close();
  _riSession.close();
  _riFeed.select(null);
  DebugShellState.visible = false;
  document.body.classList.remove('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'none';
}

async function openRequestInspectorForTask(taskId) {
  if (!taskId) return;
  if (!_riSession.isOpen) openRequestInspector();
  /* Set AFTER open (open resets it): the pending scroll survives until
   * the wire for this conversation lands. */
  _riNavigation.land(String(taskId));
}

/* Turn-level landing for a tool-row anchor (the feed has no per-round
 * pane anymore): open the drawer scrolled to the producing turn. The
 * per-round view is the INLINE panel (openToolDebugPanel) — when the tool
 * row is in the DOM, that is where the round renders. */
async function openRequestInspectorForToolRound(taskId, roundNum) {
  return openRequestInspectorForTask(taskId);
}

/* Called from restoreDebugForConv (debug_panel.js) on conversation switch. */
function _riOnConvSwitch(convId) {
  if (!_riSession.isOpen) return;
  _riSession.open(convId || null);
  _riNavigation.open();
  _riFeed.select(_riSession.conversationId);
  void _riFeed.load(_riSession.conversationId);
}

/* data-tofu-action target (header refresh button / inline retry). */
function riRefreshTasks() {
  if (!_riSession.isOpen) return undefined;
  return _riFeed.load(_riSession.conversationId);
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

function _riScrollToTurn(turnId) {
  const node = (typeof _findRenderedNativeTurnNode === 'function')
    ? _findRenderedNativeTurnNode(turnId) : null;
  if (node && node.scrollIntoView) {
    node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function _riEl(id) { return document.getElementById(id); }

/* The drawer owns the pane whenever it is open: SSE evidence frames and
 * conversation-switch restores record into DebugShellState as before but
 * must not render over the feed. */
function _riFeedOwnsPane() { return _riSession.isOpen; }

function _riFeedScroller() {
  const content = _riEl('debugContent');
  if (!content || typeof content.closest !== 'function') return null;
  return content.closest('.ri-main') || content.parentElement;
}

/* ── Back-to-feed bar (trace / detached one-off views) ── */
function _riShowFeedBackBar(pane) {
  renderInspectorBack(_riEl('debugContent'), t, () => {
    if (pane && !pane.isCurrent()) return;
    _riSession.showFeed();
    _riFeed.render();
  });
}

/* ── Level 3: payload resolution — shared by the INLINE tool panel and
 * the drawer's no-slot fallback. Bounded payload cache; live SSE
 * snapshots win over server reads. */
function _riFetchLog(key, msg, level) {
  if (typeof debugLog === 'function') debugLog('[ri] payload ' + key + ' — ' + msg, level || 'info');
}
function _riDetailLoading(kind) {
  _riNavigation.suspend();
  const title = _riEl('debugTitle');
  if (title) title.textContent = t(kind === 'trace' ? 'ri.traceTitle' : 'ri.title');
  renderInspectorLoading(_riEl('debugContent'), t);
}
function _riDetailFailed(retry) {
  renderInspectorFailure(_riEl('debugContent'), t, retry);
}

/* Public tool entry delegates exact address, mount, read lifetime and markup
 * to the native inline owner. Remove this bridge with retained diagnostics. */
function openToolDebugPanel(taskId, roundNum, anchorEl) {
  if (typeof debugLog === 'function')
    debugLog('[ri] openToolDebugPanel ' + taskId + ' R' + roundNum, 'info');
  return _riInline.open(taskId, roundNum, anchorEl);
}

/* Trace fetching remains with the original-pane detail owner. Presentation
 * lives in inspector-trace-{projection,view}.ts; no retained duplicate renderer. */
function _riOpenTrace(taskId) { return _riDetails.trace(taskId); }

// BEGIN GENERATED LAZY RUNTIME PORTS — diagnostics-presenters
runtimeScope.DebugPresentationState = DebugPresentationState;
runtimeScope.openRequestInspectorForTask = openRequestInspectorForTask;
// END GENERATED LAZY RUNTIME PORTS
// BEGIN GENERATED LAZY RUNTIME ACTIONS — diagnostics-presenters
runtimeScope.closeDebug = closeDebug;
runtimeScope.copyDebugContent = copyDebugContent;
runtimeScope.openToolDebugPanel = openToolDebugPanel;
runtimeScope.riRefreshTasks = riRefreshTasks;
runtimeScope.toggleDebug = toggleDebug;
// END GENERATED LAZY RUNTIME ACTIONS

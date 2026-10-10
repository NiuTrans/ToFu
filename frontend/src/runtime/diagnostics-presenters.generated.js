// @ts-check
/* Generated lazy retained runtime: diagnostics-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { createDiagnosticsWireController } from '../features/diagnostics-wire-controller';
import { resolveInspectorView } from '../features/inspector-evidence';
import { t } from '../i18n/index';
import { escapeHtml } from '../html-safety';
import { createInspectorDisclosure, syncInspectorDisclosure, captureInspectorFocus, restoreInspectorFocus } from '../features/diagnostics-message-disclosure';

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
 * through here. Wipes `p`, renders one block per message. Pure full render
 * only: the drawer's incremental path, open-state restore and scroll
 * handling stay in showMessagesInDebug; tools are synced separately via
 * updateDebugToolsBlock (the drawer updates them on the incremental path
 * too, outside any wipe). */
function renderDebugBlocksInto(p, messages, opts) {
  p.innerHTML = "";
  messages.forEach((msg, i) => {
    const block = createBlock(msg, i);
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
    if (frame.type === 'messages_snapshot'
        && Array.isArray(frame.messages)) {
      const label = frame.label || t('stream.roundMessages',
        { round: frame.roundNum, n: frame.messageCount });
      showMessagesInDebug(
        frame.messages, label, /* isUpdate */ true, conversationId,
        frame.tools || undefined, /* approx */ false,
        { kind: frame.kind, model: frame.model, params: frame.params,
          roundNum: frame.roundNum, turn: frame.turn,
          taskId: frame.taskId, contextManifest: frame.contextManifest },
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

let _riOpen = false;
/* traceOpen: the pane shows a per-turn trace instead of the feed (a
 * background poll must not clobber it). detached: the tool-row fallback
 * rendered a one-off state view into the pane. */
const _riSel = { traceOpen: false, traceTaskId: null, detached: false };
let _riConvId = null;
let _riPollTimer = null;
/* Poll cadence: fast while any turn is live, slow when idle. */
const _RI_POLL_LIVE_MS = 3000;
const _RI_POLL_IDLE_MS = 15000;

/* The feed model for _riConvId: {messages, sections, turns}. */
let _riWire = null;

/* One request/last-good owner per open conversation; no cross-conversation
 * fallback, no stale response paint, no overlapping poll + retry fetch. */
const _riWireController = createDiagnosticsWireController((convId) =>
  (typeof Api !== 'undefined' && Api.tasks &&
    typeof Api.tasks.convWire === 'function')
    ? Api.tasks.convWire(convId) : Promise.resolve(null));
let _riWireReadError = false;
/* Anchor scroll requested before the wire landed. */
let _riPendingScrollTaskId = null;

/* Real-time drive: the poll alone made the drawer lag behind reality.
 * Subscribing to the conversation's TurnStore flips that: any
 * attempt/turn STATUS dispatch (not content deltas) triggers a throttled
 * silent refresh. The poll stays as the cross-process backstop. */
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
    await _riLoadWire(_riConvId, { silent: true });
    _riSchedulePoll(_riFeedIsLive() ? _RI_POLL_LIVE_MS : _RI_POLL_IDLE_MS);
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
  _riSel.traceOpen = false;
  _riSel.traceTaskId = null;
  _riSel.detached = false;
  DebugShellState.visible = true;
  document.body.classList.add('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'flex';
  _riConvId = DebugShellState.activeConversationId || null;
  _riWire = null;
  _riWireReadError = false;
  _riWireController.select(_riConvId);
  _riCloseTurnJump();
  _riRenderFeedLoading();
  const convId = DebugShellState.activeConversationId;
  _riLoadWire(convId);
  _riBindStore(convId);
  _riSchedulePoll(_RI_POLL_IDLE_MS);
}

function closeRequestInspector() {
  _riOpen = false;

  _riWireController.select(null);
  _riConvId = null;
  _riWire = null;
  _riWireReadError = false;
  _riPendingScrollTaskId = null;
  _riStopPoll();

  _riUnbindStore();
  _riCloseTurnJump();
  DebugShellState.visible = false;
  document.body.classList.remove('ri-open');
  const d = document.getElementById('riDrawer');
  if (d) d.style.display = 'none';
}

async function openRequestInspectorForTask(taskId) {
  if (!taskId) return;
  if (!_riOpen) openRequestInspector();
  /* Set AFTER open (open resets it): the pending scroll survives until
   * the wire for this conversation lands. */
  _riPendingScrollTaskId = String(taskId);
  _riConsumePendingScroll();
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
  if (!_riOpen) return;

  _riConvId = convId || null;
  _riWire = null;
  _riWireReadError = false;
  _riWireController.select(_riConvId);
  _riFeedActiveIdx = -1;
  _riSel.traceOpen = false;
  _riSel.traceTaskId = null;
  _riSel.detached = false;
  _riPendingScrollTaskId = null;
  _riCloseTurnJump();
  _riRenderFeedLoading();
  _riLoadWire(convId);
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
  // The Android WebView reports visible while backgrounded; the shell's
  // nativeVisibility bridge is the only reliable pocket signal there.
  if (document.hidden || runtimeScope.nativeVisibility?.isHidden() === true) {
    _riSchedulePoll(_RI_POLL_IDLE_MS);
    return;
  }
  await _riLoadWire(_riConvId, { silent: true });
  _riSchedulePoll(_riFeedIsLive() ? _RI_POLL_LIVE_MS : _RI_POLL_IDLE_MS);
}

function _riFeedIsLive() {
  const turns = (_riWire && Array.isArray(_riWire.turns)) ? _riWire.turns : [];
  return turns.some((row) => row && row.live);
}

/* data-tofu-action target (header refresh button / inline retry). */
function riRefreshTasks() {
  if (!_riOpen) return undefined;
  return _riLoadWire(_riConvId, { silent: false });
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

/* Reply ordinal for a turn id (assistant replies only — matches what the
 * user reads as "回复 #N"). The TurnStore is the authority; unresolved
 * turns (older history, bucket rows) render as "更早". */
function _riTurnOrdinalForId(turnId) {
  try {
    const read = runtimeScope.ConversationTurnRead;
    if (!read || !_riConvId || !turnId) return null;
    const ordered = (read.ordered && read.ordered(_riConvId)) || [];
    let n = 0;
    for (const turn of ordered) {
      if (!turn || !turn.turnId) continue;
      if (turn.actor === 'assistant') n += 1;
      if (turn.turnId === turnId) return n > 0 ? n : null;
    }
    return null;
  } catch (_) { return null; }
}

function _riScrollToTurn(turnId) {
  const node = (typeof _findRenderedNativeTurnNode === 'function')
    ? _findRenderedNativeTurnNode(turnId) : null;
  if (node && node.scrollIntoView) {
    node.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

function _riEl(id) { return document.getElementById(id); }

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

/* The drawer owns the pane whenever it is open: SSE evidence frames and
 * conversation-switch restores record into DebugShellState as before but
 * must not render over the feed. */
function _riFeedOwnsPane() { return _riOpen; }

/* ── Feed loading ── */
async function _riLoadWire(convId, opts) {
  const silent = !!(opts && opts.silent);
  if (!_riOpen || (convId || null) !== _riConvId) return;
  if (!silent && !_riWire) _riRenderFeedLoading();
  const view = await _riWireController.load();
  if (!view || !_riOpen || (convId || null) !== _riConvId) return;
  _riWireReadError = view.readError;
  if (!view.wire) {
    _riWire = null;
    _riRenderFeedError();
    return;
  }
  _riWire = view.wire;
  _riRenderWire({ keepScroll: true });
}

function _riRenderFeedLoading() {
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (title) title.textContent = t('ri.loading');
  if (content) {
    content.innerHTML = `<div class="ri-loading">` +
      `<span class="ri-spinner" aria-hidden="true"></span>` +
      `${_riEsc(t('ri.loading'))}</div>`;
  }
  _riPaintFeedNav();
}

function _riRenderFeedError() {
  const content = _riEl('debugContent');

  const title = _riEl('debugTitle');
  if (title) title.textContent = t('ri.loadFailed');
  if (content) {
    content.innerHTML = `<div class="ri-main-empty">` +
      `${_riEsc(t('ri.loadFailed'))} ` +
      `<button type="button" class="ri-retry" ` +
      `data-tofu-action="riRefreshTasks()">${_riEsc(t('ri.retry'))}</button>` +
      `</div>`;
  }
  if (content) {
    content._rawMessages = [];
    content._rawTools = null;
  }
  _riPaintFeedNav();
}

/* ── Feed rendering ─────────────────────────────────────────────────
 * One flat scroll: section dividers interleaved with the shared debug
 * message blocks (createBlock from debug_panel.js — ONE renderer). A
 * wire refresh preserves expanded blocks (by stable message identity),
 * focused headers and scroll position; a live append keeps the user pinned to the
 * bottom only when they were already there. */
function _riFeedScroller() {
  const content = _riEl('debugContent');
  if (!content || typeof content.closest !== 'function') return null;
  return content.closest('.ri-main') || content.parentElement;
}

function _riRenderWire(opts) {
  _riPaintFeedNav();
  if (_riSel.traceOpen || _riSel.detached) return;  // trace / one-off view owns the pane
  const content = _riEl('debugContent');
  if (!content) return;
  const keepScroll = !!(opts && opts.keepScroll);
  const openMids = new Set();
  const focusedMid = keepScroll ? captureInspectorFocus(content) : null;
  const scroller = _riFeedScroller();
  let prevScroll = 0;
  let stickBottom = false;
  if (keepScroll) {
    content.querySelectorAll('.debug-msg-block.open').forEach((b) => {
      if (b.dataset.mid) openMids.add(b.dataset.mid);
    });
    if (scroller) {
      prevScroll = scroller.scrollTop;
      stickBottom = scroller.scrollHeight > 0 &&
        scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 40;
    }
  }
  content.innerHTML = '';
  const wire = _riWire || { messages: [], sections: [], turns: [] };
  const messages = Array.isArray(wire.messages) ? wire.messages : [];
  const sections = Array.isArray(wire.sections) ? wire.sections : [];
  const turns = Array.isArray(wire.turns) ? wire.turns : [];
  const title = _riEl('debugTitle');
  if (title) {
    title.textContent = `Messages (${messages.length})` +
      (turns.length ? ` · ${t('ri.feedTurns', { n: turns.length })}` : '');
  }
  if (_riWireReadError) {
    /* A failed read leaves the last complete feed intact, or presents an
     * honest partial feed if this open epoch has no complete snapshot. */
    const warn = document.createElement('div');
    warn.className = 'ri-warn-line';
    warn.innerHTML = `${_riEsc(t('ri.loadFailed'))} ` +
      `<button type="button" class="ri-retry" ` +
      `data-tofu-action="riRefreshTasks()">${_riEsc(t('ri.retry'))}</button>`;
    content.appendChild(warn);
  }
  if (Array.isArray(wire.eventReadBounds) && wire.eventReadBounds.length) {
    const note = document.createElement('div');
    note.className = 'ri-feed-truncated';
    note.textContent = t('ri.eventWindowPartial');
    content.appendChild(note);
  }
  if (wire.olderTurnsDropped) {
    const note = document.createElement('div');
    note.className = 'ri-feed-truncated';
    note.textContent = t('ri.feedTruncated', { n: wire.olderTurnsDropped });
    content.appendChild(note);
  }
  if (!messages.length && !sections.length) {
    const empty = document.createElement('div');
    empty.className = 'ri-main-empty';
    empty.textContent = t('ri.empty');
    content.appendChild(empty);
    content._rawMessages = [];
    content._rawTools = null;
    _riConsumePendingScroll();
    return;
  }
  const byIndex = new Map();
  for (const s of sections) {
    const k = Math.max(0, Math.min(messages.length, Number(s.msgIndex) || 0));
    if (!byIndex.has(k)) byIndex.set(k, []);
    byIndex.get(k).push(s);
  }
  const appendSections = (idx) => {
    for (const s of (byIndex.get(idx) || [])) {
      content.appendChild(_riFeedDividerEl(s));
    }
  };
  if (typeof createBlock === 'function') {
    messages.forEach((msg, i) => {
      if (byIndex.has(i)) appendSections(i);
      const block = createBlock(msg, i);
      block.dataset.fp = (typeof msgFingerprint === 'function')
        ? msgFingerprint(msg) : '';
      block._msgRef = msg;
      content.appendChild(block);
    });
    if (byIndex.has(messages.length)) appendSections(messages.length);
  }
  content._rawMessages = messages;
  content._rawTools = null;
  if (openMids.size && typeof _debugOpenBlock === 'function') {
    content.querySelectorAll('.debug-msg-block').forEach((b) => {
      if (b.dataset.mid && openMids.has(b.dataset.mid)) _debugOpenBlock(b);
    });
  }
  if (keepScroll && scroller) {
    scroller.scrollTop = stickBottom ? scroller.scrollHeight : prevScroll;
  }
  restoreInspectorFocus(content, focusedMid);
  _riBindFeedScroll();
  /* Anchors that arrived before the feed rendered land here, once the
   * dividers actually exist. */
  _riConsumePendingScroll();
}

function _riFeedDividerEl(s) {
  if (s && s.type === 'rebuild') {
    const el = document.createElement('div');
    el.className = 'ri-feed-rebuild';
    el.textContent = t('ri.rebuildNote', { k: s.kept || 0, d: s.dropped || 0 });
    return el;
  }
  if (s && s.type === 'agent') {
    const el = document.createElement('div');
    el.className = 'ri-feed-agent';
    el.innerHTML =
      `<span class="ri-agent-badge">${_riEsc(s.agentId || 'agent')}</span>` +
      `<span class="ri-feed-agent-label">${_riEsc(t('ri.agentSection'))}</span>`;
    return el;
  }
  /* turn divider */
  const el = document.createElement('div');
  el.className = 'ri-feed-turn';
  el.dataset.turnId = (s && s.turnId) || '';
  el.dataset.taskId = (s && s.taskId) || '';
  const ordinal = _riTurnOrdinalForId(s && s.turnId);
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = 'ri-turn-chip' + (ordinal ? '' : ' ri-turn-chip-muted');
  chip.textContent = ordinal ? t('ri.turnChip', { n: ordinal }) : t('ri.earlierShort');
  chip.title = t('ri.turnChipTip');
  chip.onclick = () => { if (s.turnId) _riScrollToTurn(s.turnId); };
  el.appendChild(chip);
  const status = _riStatusInfo({ status: s.status, live: s.live });
  const dot = document.createElement('span');
  dot.className = `ri-task-status ri-tone-${_riEsc(status.tone)}`;
  dot.innerHTML = `<span class="ri-task-status-dot" aria-hidden="true"></span>` +
    `${_riEsc(status.label)}`;
  el.appendChild(dot);
  const preview = document.createElement('span');
  preview.className = 'ri-feed-turn-preview';
  preview.textContent = s.readError ? t('ri.loadFailed')
    : s.expired ? t('ri.expired') : (s.preview || '');
  preview.title = preview.textContent;
  el.appendChild(preview);
  const time = document.createElement('span');
  time.className = 'ri-feed-turn-time';
  time.textContent = _riRelTime(s.createdAt);
  time.title = _riAbsTime(s.createdAt);
  el.appendChild(time);
  /* Per-turn Turn Trace (耗时分析) — the ONE click that answers "where
   * did the time go" for this reply's latest run. */
  if (s.taskId) {
    const traceBtn = document.createElement('button');
    traceBtn.type = 'button';
    traceBtn.className = 'ri-feed-trace';
    traceBtn.textContent = t('ri.traceEntry');
    traceBtn.title = t('ri.traceEntryHint');
    traceBtn.onclick = () => _riOpenTrace(s.taskId);
    el.appendChild(traceBtn);
  }
  return el;
}

/* ── Turn navigation (SCROLL model) ──────────────────────────────────
 * Entries come from the wire's turn list; jumping scrolls the feed to
 * the divider, never switches content. The active entry tracks both the
 * explicit jumps and the user's own scrolling. */
let _riFeedActiveIdx = -1;

function _riFeedNavEntries() {
  const turns = (_riWire && Array.isArray(_riWire.turns)) ? _riWire.turns : [];
  return turns.map((row) => ({
    key: row.turnId || row.taskId,
    turnId: row.turnId || '',
    taskId: row.taskId || '',
    ordinal: _riTurnOrdinalForId(row.turnId),
    preview: row.preview || '',
    createdAt: row.createdAt || 0,
    live: !!row.live,
    status: row.status || '',
    expired: !!row.expired,
    readError: !!row.readError,
  }));
}

function _riPaintFeedNav() {
  const nav = _riEl('riTurnNav');
  if (!nav) return;  /* chrome absent (embedded harness) — nav is optional */
  const entries = _riFeedNavEntries();
  if (entries.length < 2) {
    nav.hidden = true;
    _riCloseTurnJump();
    return;
  }
  nav.hidden = false;
  if (_riFeedActiveIdx < 0 || _riFeedActiveIdx >= entries.length) {
    /* Default: the newest turn — reading starts at the bottom. */
    _riFeedActiveIdx = entries.length - 1;
  }
  const active = entries[_riFeedActiveIdx];
  const prev = _riEl('riTurnPrev');
  const next = _riEl('riTurnNext');
  const pos = _riEl('riTurnPos');
  const readout = _riEl('riTurnReadout');
  if (readout) {
    readout.textContent = active.ordinal
      ? t('ri.turnChip', { n: active.ordinal }) : t('ri.earlierShort');
  }
  if (pos) {
    pos.title = t('ri.jumpToTurn');
    pos.setAttribute('aria-label', t('ri.jumpToTurn'));
    pos.onclick = () => _riToggleTurnJump();
  }
  if (prev) {
    prev.disabled = _riFeedActiveIdx <= 0;
    prev.onclick = () => _riScrollFeedToIdx(_riFeedActiveIdx - 1);
  }
  if (next) {
    next.disabled = _riFeedActiveIdx >= entries.length - 1;
    next.onclick = () => _riScrollFeedToIdx(_riFeedActiveIdx + 1);
  }
  /* An open jump list rides live data — repaint it on refresh ticks. */
  const panel = _riEl('riTurnJump');
  if (panel && !panel.hidden) _riFillTurnJump(entries, active);
}

function _riFeedTurnDividerEl(entry) {
  const content = _riEl('debugContent');
  if (!content) return null;
  for (const el of content.querySelectorAll('.ri-feed-turn')) {
    if ((entry.turnId && el.dataset.turnId === entry.turnId) ||
        (!entry.turnId && el.dataset.taskId === entry.taskId)) return el;
  }
  return null;
}

function _riScrollFeedToIdx(idx) {
  const entries = _riFeedNavEntries();
  const entry = entries[idx];
  if (!entry) return;
  _riFeedActiveIdx = idx;
  _riPaintFeedNav();
  _riCloseTurnJump();
  const el = _riFeedTurnDividerEl(entry);
  if (!el) return;
  if (typeof el.scrollIntoView === 'function') {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  el.classList.add('ri-flash');
  setTimeout(() => el.classList.remove('ri-flash'), 1600);
}

function _riConsumePendingScroll() {
  const taskId = _riPendingScrollTaskId;
  if (!taskId) return;
  const entries = _riFeedNavEntries();
  if (!entries.length) return;  // wire not here yet (or failed to load)
  const idx = entries.findIndex((e) => e.taskId === taskId);
  const target = entries[idx >= 0 ? idx : entries.length - 1];
  /* The pane may still show the loading spinner or a trace — keep the
   * pending scroll until the divider is actually rendered. */
  if (!_riFeedTurnDividerEl(target)) return;
  _riPendingScrollTaskId = null;
  /* An unknown/out-of-page task lands on the newest turn — the click
   * always lands somewhere meaningful. */
  _riScrollFeedToIdx(idx >= 0 ? idx : entries.length - 1);
}

/* The active nav entry also tracks the user's own scrolling (the same
 * 30%-of-viewport rule the conversation bar's turn nav uses). */
let _riFeedScrollTimer = null;
function _riBindFeedScroll() {
  const scroller = _riFeedScroller();
  if (!scroller || scroller._riFeedBound) return;
  scroller._riFeedBound = true;
  scroller.addEventListener('scroll', () => {
    if (_riFeedScrollTimer) return;
    _riFeedScrollTimer = setTimeout(() => {
      _riFeedScrollTimer = null;
      _riSyncActiveTurnFromScroll();
    }, 120);
  }, { passive: true });
}

function _riSyncActiveTurnFromScroll() {
  if (!_riOpen || _riSel.traceOpen || _riSel.detached) return;
  const entries = _riFeedNavEntries();
  if (entries.length < 2) return;
  const scroller = _riFeedScroller();
  if (!scroller) return;
  const boundary = scroller.scrollTop + scroller.clientHeight * 0.3;
  let idx = 0;
  entries.forEach((entry, i) => {
    const el = _riFeedTurnDividerEl(entry);
    if (el && el.offsetTop <= boundary) idx = i;
  });
  if (idx !== _riFeedActiveIdx) {
    _riFeedActiveIdx = idx;
    _riPaintFeedNav();
  }
}

let _riJumpBound = false;
function _riToggleTurnJump() {
  const panel = _riEl('riTurnJump');
  if (!panel) return;
  if (!panel.hidden) {
    _riCloseTurnJump();
    return;
  }
  const entries = _riFeedNavEntries();
  if (entries.length < 2) return;
  _riFillTurnJump(entries, entries[_riFeedActiveIdx]);
  panel.hidden = false;
  if (!_riJumpBound) {
    _riJumpBound = true;
    document.addEventListener('mousedown', _riJumpOutside, true);
  }
}

function _riJumpOutside(ev) {
  const panel = _riEl('riTurnJump');
  const pos = _riEl('riTurnPos');
  if (!panel || panel.hidden) return;
  const target = ev && ev.target;
  if ((panel.contains && panel.contains(target)) ||
      (pos && pos.contains && pos.contains(target))) return;
  _riCloseTurnJump();
}

function _riCloseTurnJump() {
  const panel = _riEl('riTurnJump');
  if (panel) panel.hidden = true;
  if (_riJumpBound) {
    _riJumpBound = false;
    document.removeEventListener('mousedown', _riJumpOutside, true);
  }
}

function _riFillTurnJump(entries, active) {
  const panel = _riEl('riTurnJump');
  if (!panel) return;
  panel.innerHTML = '';
  /* Newest first — the reply you were just reading lands on top. */
  const ordered = entries.map((e, i) => ({ e, i }))
    .sort((a, b) => b.e.createdAt - a.e.createdAt);
  for (const { e, i } of ordered) {
    const row = document.createElement('div');
    row.className = 'ri-turn-jump-row' + (e === active ? ' ri-sel' : '');
    row.setAttribute('role', 'button');
    row.tabIndex = 0;
    if (e.turnId) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'ri-turn-chip' + (e.ordinal ? '' : ' ri-turn-chip-muted');
      chip.textContent = e.ordinal
        ? t('ri.turnChip', { n: e.ordinal }) : t('ri.earlierShort');
      chip.title = t('ri.turnChipTip');
      chip.onclick = (ev) => {
        ev.stopPropagation();
        _riCloseTurnJump();
        _riScrollToTurn(e.turnId);
      };
      row.appendChild(chip);
    } else {
      const tag = document.createElement('span');
      tag.className = 'ri-turn-chip ri-turn-chip-muted';
      tag.textContent = t('ri.earlierShort');
      row.appendChild(tag);
    }
    const preview = document.createElement('span');
    preview.className = 'ri-turn-jump-preview';
    preview.textContent = e.readError ? t('ri.loadFailed')
      : e.expired ? t('ri.expired') : (e.preview || '');
    preview.title = preview.textContent;
    row.appendChild(preview);
    const time = document.createElement('span');
    time.className = 'ri-turn-jump-time';
    time.textContent = _riRelTime(e.createdAt);
    time.title = _riAbsTime(e.createdAt);
    row.appendChild(time);
    row.onclick = () => _riScrollFeedToIdx(i);
    panel.appendChild(row);
  }
}

/* ── Back-to-feed bar (trace / detached one-off views) ── */
function _riShowFeedBackBar() {
  const content = _riEl('debugContent');
  if (!content) return;
  const bar = document.createElement('div');
  bar.className = 'ri-feed-back';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ri-retry';
  btn.textContent = t('ri.backToFeed');
  btn.onclick = () => {
    _riSel.traceOpen = false;
    _riSel.traceTaskId = null;
    _riSel.detached = false;
    _riRenderWire({ keepScroll: false });
  };
  bar.appendChild(btn);
  content.prepend(bar);
}

/* ── Level 3: payload resolution — shared by the INLINE tool panel and
 * the drawer's no-slot fallback. Bounded payload cache; live SSE
 * snapshots win over server reads. */
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
/* 2026-09-10 goal-mode probe: trace every payload resolution so one badge
 * click shows exactly where the round view dies (local snapshot / cache /
 * server). warn lines also relay to logs/app.log through the client-error
 * beacon, so the trace is visible in the background without devtools. */
function _riFetchLog(key, msg, level) {
  if (typeof debugLog === 'function')
    debugLog('[ri] payload ' + key + ' — ' + msg, level || 'info');
}
function _riRowIsLive(row) {
  return !!(row && (row.live ||
    ['running', 'queued', 'pending'].includes(
      String(row.status || '').toLowerCase())));
}
async function _riFetchPayload(taskId, roundNum, turn, kind, opts) {
  turn = turn || '';
  kind = kind || 'request';
  /* opts.fold: ask the SERVER to diff this round against the previous one
   * (same turn/kind) and return foldPrefix/diffBase — one payload download
   * per click instead of the old two full-context transfers. */
  const fold = !!(opts && opts.fold);
  const key = taskId + ':' + kind + ':' + turn + ':' + roundNum;
  /* Prefer live snapshots; Flow phases are keyed by turn|roundNum. */
  const _acc = DebugShellState.requests[taskId];
  if (kind === 'state') {
    const st = _acc && (_acc.states || []).filter((s) => s && s.messages &&
      String(s.roundNum) === String(roundNum) && (!turn || s.turn === turn)).pop();
    if (st) {
      const payload = { messages: st.messages, tools: st.tools,
        label: st.label, model: st.model, params: st.params, kind: 'state' };
      _riFetchLog(key, 'live state snapshot, msgs=' + payload.messages.length);
      return _riCachePayload(key, payload);
    }
  } else {
    const _accKey = turn ? turn + '|' + roundNum : String(roundNum);
    const acc = _acc && _acc.rounds[_accKey];
    if (acc && acc.messages) {
      const payload = { messages: acc.messages, tools: acc.tools,
        label: acc.label, model: acc.model, params: acc.params,
        turn: acc.turn || turn };
      _riFetchLog(key, 'live request snapshot, msgs=' + payload.messages.length);
      return _riCachePayload(key, payload);
    }
  }
  /* A live task keeps appending: a cached server read from an earlier
   * poll would freeze the round mid-flight. (Live SSE snapshots above
   * still win — they ARE the newest state.) */
  const live = _riRowIsLive(_riWireTaskRow(taskId));
  if (!live && _riPayloadCache[key]) {
    const cached = _riPayloadCache[key];
    const n = (cached.messages || []).length;
    _riFetchLog(key, 'cache serve, msgs=' + n, n ? 'info' : 'warn');
    return cached;
  }
  _riFetchLog(key, 'server fetch (live=' + live + ', localAcc=' +
    (_acc ? 'yes' : 'no') + ', cached=' + (_riPayloadCache[key] ? 'yes' : 'no') +
    ')');
  let data = null;
  try {
    data = (typeof Api !== 'undefined' && Api.tasks)
      ? await Api.tasks.getRequestPayload(taskId, roundNum, turn || undefined,
          kind === 'state' ? 'state' : undefined, fold || undefined) : null;
  } catch (error) {
    if (error && error.status === 404) return null;
    throw error;
  }
  if (data && data.messages) {
    _riFetchLog(key, 'server ok, msgs=' + data.messages.length,
      data.messages.length ? 'info' : 'warn');
    if (!_riRowIsLive(_riWireTaskRow(taskId))) {
      _riCachePayload(key, data);
    }
    return data;
  }
  _riFetchLog(key, data ? 'payload without messages' : 'null (404 or error)',
    'warn');
  return null;
}

/* Liveness for the payload cache: the wire's turn list is the drawer's
 * only liveness source now that the task list is gone. */
function _riWireTaskRow(taskId) {
  const turns = (_riWire && Array.isArray(_riWire.turns)) ? _riWire.turns : [];
  for (const row of turns) {
    if (row && row.taskId === taskId) return row;
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
async function _riRoundScopedMessages(taskId, roundNum, kind, messages, turn = '') {
  const num = parseInt(roundNum, 10);
  if (!Number.isFinite(num) || num <= 1 || !Array.isArray(messages)) return null;
  let prev;
  try {
    prev = await _riFetchPayload(taskId, num - 1, turn,
      kind === 'state' ? 'state' : 'request');
  } catch (error) {
    /* The previous-round diff is optional. Keep the current round visible
     * when only the diff base fails to load. */
    _riFetchLog(taskId + ':' + roundNum, 'diff base unavailable', 'warn');
    return null;
  }
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
  const turn = anchorEl?.dataset?.riTurn || '';
  if (!taskId || roundNum == null) return;
  if (typeof debugLog === 'function')
    debugLog('[ri] openToolDebugPanel ' + taskId + ' R' + roundNum, 'info');
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
     * would have: the result state, or the request when no mirror exists.
     * The detached view owns the pane (polls must not clobber it) until the
     * user returns to the feed. */
    if (typeof debugLog === 'function')
      debugLog('[ri] no tool slot in DOM for ' + taskId + ' R' + roundNum +
        ' — degrading to drawer', 'info');
    if (!_riOpen) openRequestInspector();
    const convId = DebugShellState.activeConversationId;
    _riSel.detached = true;
    _riSel.traceOpen = false;
    let view;
    try {
      view = await _riFetchRoundView(taskId, roundNum, turn);
    } catch (error) {
      if (!_riOpen || DebugShellState.activeConversationId !== convId) return;
      const content = _riEl('debugContent');
      if (content) {
        content.innerHTML = '<div class="ri-empty">' +
          _riEsc(t('ri.loadFailed')) + '</div>';
        const retry = document.createElement('button');
        retry.type = 'button';
        retry.className = 'ri-retry';
        retry.textContent = t('ri.retry');
        retry.onclick = () => openToolDebugPanel(taskId, roundNum, anchorEl);
        content.appendChild(retry);
        _riShowFeedBackBar();
      }
      return;
    }
    if (!_riOpen || DebugShellState.activeConversationId !== convId) return;
    if (view && view.payload && view.payload.messages &&
        typeof showMessagesInDebug === 'function') {
      showMessagesInDebug(view.payload.messages, view.payload.label || '', false,
        DebugShellState.activeConversationId,
        view.payload.tools || undefined, false, undefined,
        { resetScroll: true,
          contextManifest: view.payload.contextManifest || [] });
      _riShowFeedBackBar();
      return;
    }
    _riSel.detached = false;
    _riRenderWire();
    if (!turn) await openRequestInspectorForToolRound(taskId, roundNum);
    return;
  }
  /* Re-clicking the entry for the round already open closes it (toggle). */
  const existing = document.querySelector('.ri-state-panel');
  if (existing && existing.dataset.riRound === String(roundNum) &&
      existing.dataset.riTask === String(taskId) &&
      existing.dataset.riTurn === turn) {
    existing.remove();
    return;
  }
  _riMountToolPanel(slot, taskId, roundNum, turn);
}

/* Resolve the ONE view a tool row's debug entry shows, and say which axis it
 * came from. The post-tool mirror is preferred because it is a superset of
 * the request; the request is the fallback for rounds that never emitted a
 * mirror (historical agents, an aborted round, an expired state row).
 * Returns {kind, payload} or null when neither axis has anything. */
async function _riFetchRoundView(taskId, roundNum, turn = '') {
  /* fold: the server diffs against the previous round from its cached rows,
   * so the round-scoped increment usually needs no second download. */
  return resolveInspectorView(
    (t, r, tu, kind) => _riFetchPayload(t, r, tu, kind, { fold: true }),
    taskId, roundNum, turn);
}

/* Mount the (single-instance) panel after a tool slot. Transient by
 * design — a chat re-render may drop it; re-click reopens. */
async function _riMountToolPanel(slot, taskId, roundNum, turn = '') {
  document.querySelectorAll('.ri-state-panel').forEach((p) => p.remove());
  const panel = document.createElement('div');
  panel.className = 'ri-state-panel';
  panel.dataset.riTask = String(taskId);
  panel.dataset.riTurn = turn;
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
  await _riRenderToolPanel(panel, taskId, roundNum, turn);
}

/* Render the panel's ONE view: the message mirror captured right after this
 * round's tools ran, or the producing request when that round emitted no
 * mirror. Goes through the shared debug renderer, scoped to this round's
 * increment. The kind chip names the axis on screen, so a fallback render is
 * never mistaken for the mirror. */
async function _riRenderToolPanel(panel, taskId, roundNum, turn = '') {
  if (!panel.isConnected) return;  // closed while fetching
  const renderSeq = (panel._riRenderSeq || 0) + 1;
  panel._riRenderSeq = renderSeq;
  panel.dataset.riRound = String(roundNum);
  panel.dataset.riPanel = taskId + ':' + roundNum;
  const body = panel.querySelector('.ri-state-body');
  const titleEl = panel.querySelector('.ri-state-panel-title');
  const kindEl = panel.querySelector('.ri-state-panel-kind');
  let view;
  try {
    view = await _riFetchRoundView(taskId, roundNum, turn);
  } catch (error) {
    if (!panel.isConnected || panel._riRenderSeq !== renderSeq) return;
    panel.dataset.riKind = '';
    if (kindEl) kindEl.textContent = '';
    if (titleEl) titleEl.textContent = 'R' + roundNum;
    if (body) {
      body.innerHTML = '<div class="ri-empty">' +
        _riEsc(t('ri.loadFailed')) + '</div>';
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'ri-retry';
      retry.textContent = t('ri.retry');
      retry.onclick = () => _riRenderToolPanel(panel, taskId, roundNum, turn);
      body.appendChild(retry);
    }
    return;
  }
  if (!panel.isConnected || panel._riRenderSeq !== renderSeq) return;
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
   * has no post-tool tail to slice.
   * The server fold answers the increment from the payload itself (one
   * download); the client-side previous-round diff remains the fallback
   * for live SSE snapshots and payloads cached without the fold. */
  const scoped = (typeof payload.foldPrefix === 'number' &&
      payload.foldPrefix > 0)
    ? payload.messages.slice(payload.foldPrefix)
    : await _riRoundScopedMessages(taskId, roundNum, view.kind,
      payload.messages, turn);
  if (!panel.isConnected || panel._riRenderSeq !== renderSeq) return;
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
 * /api/v4/tasks/<id>/trace folds SERVER-side spans from authoritative events
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
  flow_step: 'ri.trKindFlowStep',
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

  if ((o.kind === 'phase_painted' || o.kind === 'terminal_painted')
      && o.renderMs == null) return t('ri.tracePaintRestored');
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
  if (!taskId) return;
  _riSel.traceOpen = true;
  _riSel.traceTaskId = taskId;
  _riSel.detached = false;
  const title = _riEl('debugTitle');
  const content = _riEl('debugContent');
  if (title) title.textContent = t('ri.traceTitle');
  if (content) {
    content.innerHTML = `<div class="ri-loading">` +
      `<span class="ri-spinner" aria-hidden="true"></span>` +
      `${_riEsc(t('ri.loading'))}</div>`;
    content._rawMessages = null;
  }
  const doc = (typeof Api !== 'undefined' && Api.tasks)
    ? await Api.tasks.getTrace(taskId) : null;
  if (!_riOpen || !_riSel.traceOpen || _riSel.traceTaskId !== taskId) return;
  _riRenderTrace(doc);
  _riShowFeedBackBar();
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
  if (s.flowStepMs) {
    pushChip('tr-c-flowstep', t('ri.trKindFlowStep'), s.flowStepMs);
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
      ? 'ri.tracePartialFlow' : doc.coverageReason === 'no-round-markers'
        ? 'ri.tracePartialLegacy' : 'ri.tracePartialEvidence';
    html += `<div class="tr-note tr-note-warn">${_riEsc(
      t(key, { reason: doc.coverageReason || 'unknown' }),
    )}</div>`;
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
runtimeScope.toggleDebug = toggleDebug;
// END GENERATED LAZY RUNTIME ACTIONS

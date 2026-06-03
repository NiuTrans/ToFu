/* ═══════════════════════════════════════════
   core.js — State, Config, Utils, Markdown
   ═══════════════════════════════════════════ */

const BASE_PATH = (() => {
  const p = window.location.pathname;
  return p.replace(/\/(index\.html)?$/, "");
})();
function apiUrl(path) {
  return BASE_PATH + path;
}

/* ── Lazy KaTeX loader (277KB single-line script freezes DevTools) ── */
let _katexLoading = null;
function _ensureKatex() {
  if (typeof katex !== 'undefined') return Promise.resolve();
  if (_katexLoading) return _katexLoading;
  _katexLoading = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = BASE_PATH + '/static/vendor/katex/katex.min.js';
    s.onload = () => {
      /* Flush markdown cache so math re-renders with KaTeX */
      if (typeof _mdCache !== 'undefined') _mdCache.clear();
      /* Trigger a re-render of current chat */
      const conv = typeof getActiveConv === 'function' && getActiveConv();
      if (conv && typeof renderChat === 'function') renderChat(conv);
      /* Notify other surfaces (paper reader, artifacts, etc.) that
       * placed math-pending fallback markup so they can repaint with
       * real KaTeX output instead of staying stuck on `<code>` spans. */
      try { window.dispatchEvent(new CustomEvent('katex:loaded')); } catch (_) {}
      resolve();
    };
    s.onerror = () => reject(new Error('Failed to load KaTeX'));
    document.head.appendChild(s);
  });
  return _katexLoading;
}

/* ── Lazy PDF.js loader (pdf.min.js + worker, heavy — lazy-loaded on first use) ── */
let _pdfJsLoading = null;
function _ensurePdfJs() {
  if (typeof pdfjsLib !== 'undefined') return Promise.resolve();
  if (_pdfJsLoading) return _pdfJsLoading;
  _pdfJsLoading = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = BASE_PATH + '/static/vendor/pdf.min.js';
    s.onload = () => {
      /* Configure worker path */
      if (typeof pdfjsLib !== 'undefined' && pdfjsLib.GlobalWorkerOptions) {
        pdfjsLib.GlobalWorkerOptions.workerSrc = BASE_PATH + '/static/vendor/pdf.worker.min.js';
      }
      resolve();
    };
    s.onerror = () => reject(new Error('Failed to load PDF.js'));
    document.head.appendChild(s);
  });
  return _pdfJsLoading;
}

const TAB_ID = Math.random().toString(36).slice(2, 10);
let _syncChannel = null;
try {
  _syncChannel = new BroadcastChannel("claude_dialogue_sync");
  _syncChannel.onmessage = (e) => {
    if (e.data && e.data.sourceTab !== TAB_ID) _handleCrossTabMsg(e.data);
  };
} catch (_) {}

/* ★ DB-first: conversations start empty and are populated by
 *   loadConversationsFromServer() in initActiveTasks().
 *   localStorage is NO LONGER used for conversation metadata.
 *   This eliminates an entire class of desync / ghost bugs. */
let conversations = [];
try { localStorage.removeItem('claude_conversations'); } catch(_) {} /* clean up stale data */


/* ═══ Folder management ═══ */
let _folders = [];  // Array of {id, name, color, collapsed, order, createdAt}
let _foldersLoaded = false;  // true after first loadFolders() completes


/* ── (folders.js extracted here) ── */

let activeConvId = sessionStorage.getItem('tofu_activeConvId') || null,
  activeStreams = new Map(),
  streamBufs = new Map(),
  pendingImages = [],
  pdfProcessing = false;
/** ★ Message queue: when user sends while streaming, messages are queued here
 *  and auto-dispatched when the current stream finishes.
 *  Key = convId, Value = Array of { text, images, pdfTexts, replyQuotes, convRefs, timestamp } */
let pendingMessageQueue = new Map();
let _editingMsgIdx = null,
  _lastRenderedFingerprint = "";
/** Lightweight fingerprint of what's currently rendered — used to skip no-op re-renders from sync */
function _convRenderFingerprint(conv) {
  if (!conv) return "";
  const n = conv.messages.length;
  if (n === 0) return conv.id + ":0:" + (conv.title || "");
  const last = conv.messages[n - 1];
  const sr = last.toolRounds || last.searchResults;
  return (
    conv.id +
    ":" +
    n +
    ":" +
    (last.content || "").length +
    ":" +
    (last.thinking || "").length +
    ":" +
    (last.error || "").length +
    ":" +
    (last.finishReason || "") +
    ":" +
    (last.translatedContent || "").length +
    ":" +
    (sr ? sr.length : 0) +
    ":" +
    (last.modifiedFiles || 0) +
    ":" +
    (last._igResult ? "IG" : "") +
    ":" +
    (last._igResults ? last._igResults.length : 0) +
    ":" +
    (last._igError ? "IGE" : "") +
    ":" +
    (conv.title || "")
  );
}
let thinkingEnabled = true,
  fetchEnabled = true,
  codeExecEnabled = false,
  browserEnabled = false,
  desktopEnabled = false,
  memoryEnabled = true,
  schedulerEnabled = false,
  swarmEnabled = true,
  endpointEnabled = false,
  autopilotEnabled = false,
  imageGenEnabled = false,
  imageGenMode = false,
  humanGuidanceEnabled = false,
  searchMode = "multi",
  debugVisible = false,
  sidebarSearchQuery = "";
let _browserStatusInterval = null;
let serverModel = "aws.claude-opus-4.8";
let config = JSON.parse(
  localStorage.getItem("claude_client_config") ||
    JSON.stringify({
      temperature: 1,
      maxTokens: 128000,
      thinkingBudget: 64000,
      thinkingEffort: "medium",
      imageMaxWidth: 0,           // 0 = follow server upload-shrink policy (recommended)
      systemPrompt: "",
      model: serverModel,
    }),
);

/* ── (cost.js, debug_panel.js extracted here) ── */

function generateId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

/* Client-side stable message id (Step 1 of unified chatInner rendering).
 *
 * The server backfills every persisted message with a UUID `_msgId` via
 * lib/tasks_pkg/manager.py:_assign_message_ids.  But a freshly created
 * client-side message (optimistic user push, streaming assistant
 * placeholder, image-gen result, …) is rendered into the DOM *before*
 * persistence — so the DOM has no stable handle for it yet.
 *
 * `_newClientMsgId()` mints a `tmp_<...>` id distinct from server UUIDs;
 * once the server persists the message and a Phase-2 reload arrives, the
 * server-assigned UUID overrides the temporary id (last-write-wins).
 *
 * `_ensureMsgId(msg)` is idempotent — safe to call repeatedly. */
function _newClientMsgId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return 'tmp_' + crypto.randomUUID();
  }
  return 'tmp_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 10);
}
function _ensureMsgId(msg) {
  if (msg && typeof msg === 'object' && !msg._msgId) {
    msg._msgId = _newClientMsgId();
  }
  return msg;
}

/* ── (escape_html.js, error_envelope.js extracted here) ── */

function getActiveConv() {
  return conversations.find((c) => c.id === activeConvId);
}
/* ★ Perf: cache chatContainer ref — avoids getElementById on every scroll check */
let _chatContainerEl = null;
function _getChatContainer() {
  if (!_chatContainerEl || !_chatContainerEl.isConnected) {
    _chatContainerEl = document.getElementById("chatContainer");
  }
  return _chatContainerEl;
}
function isNearBottom(threshold) {
  const c = _getChatContainer();
  if (!c) return true;
  return c.scrollHeight - c.scrollTop - c.clientHeight < (threshold || 150);
}
let _scrollRafId = null;
function scrollToBottom(force) {
  const c = _getChatContainer();
  if (!c) return;
  if (!force && !isNearBottom(200)) return;
  /* ★ PERF: Coalesce scroll updates and use single rAF (not double).
   * During streaming, updateStreamingUI already runs inside a rAF callback
   * from twUpdate, so the DOM is already updated.  A single rAF is sufficient
   * to scroll after layout.  Double-rAF added 33ms of lag per frame. */
  if (_scrollRafId) return; // already scheduled
  _scrollRafId = requestAnimationFrame(() => {
    _scrollRafId = null;
    c.scrollTop = c.scrollHeight;
  });
}
function getToolRoundsFromMsg(msg) {
  if (msg.toolRounds && msg.toolRounds.length > 0) return msg.toolRounds;
  // ── Backward compat: old conversations stored under 'searchRounds' ──
  if (msg.searchRounds && msg.searchRounds.length > 0) return msg.searchRounds;
  if (msg.searchResults && msg.searchResults.length > 0)
    return [
      {
        roundNum: 1,
        query: msg.searchQuery || "search",
        results: msg.searchResults,
        status: "done",
      },
    ];
  return [];
}


/* ── (cross_tab_sync.js, conversations.js, cache_stats.js,
   markdown.js, health_stream_timer.js, toast.js extracted here) ── */

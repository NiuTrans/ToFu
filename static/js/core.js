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
      resolve();
    };
    s.onerror = () => reject(new Error('Failed to load KaTeX'));
    document.head.appendChild(s);
  });
  return _katexLoading;
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
function _convSorter(a, b) {
  const ap = a.pinned ? 1 : 0,
    bp = b.pinned ? 1 : 0;
  if (ap !== bp) return bp - ap;
  if (ap && bp) return (b.pinnedAt || 0) - (a.pinnedAt || 0);
  /* ★ Active (streaming / generating) conversations float to top of unpinned zone
   *   so they are never pushed out of view when other conversations update. */
  const aAct = (activeStreams.has(a.id) || a.activeTaskId) ? 1 : 0;
  const bAct = (activeStreams.has(b.id) || b.activeTaskId) ? 1 : 0;
  if (aAct !== bAct) return bAct - aAct;
  return (b.updatedAt || b.createdAt || 0) - (a.updatedAt || a.createdAt || 0);
}
function togglePinConversation(id) {
  const c = conversations.find((x) => x.id === id);
  if (!c) return;
  c.pinned = !c.pinned;
  c.pinnedAt = c.pinned ? Date.now() : 0;
  /* Pass null instead of id — pin/unpin is a metadata-only change,
   * NOT new conversation activity.  Passing changedConvId would bump
   * updatedAt = Date.now(), which makes the unpinned conversation
   * jump to the top of the non-pinned section. */
  saveConversations(null);
  renderConversationList();
  /* If messages aren't loaded yet, load them first so the sync guard
     (which skips convs with 0 messages) doesn't block the pinned state
     from reaching the server. */
  if (c.messages.length === 0 && c._needsLoad) {
    loadConversationMessages(id).then(() => syncConversationToServerDebounced(c));
  } else {
    syncConversationToServerDebounced(c);
  }
}
let activeConvId = sessionStorage.getItem('chatui_activeConvId') || null,
  activeStreams = new Map(),
  streamBufs = new Map(),
  pendingImages = [],
  pdfProcessing = false;
/** ★ Message queue: when user sends while streaming, messages are queued here
 *  and auto-dispatched when the current stream finishes.
 *  Key = convId, Value = Array of { text, images, pdfTexts, replyQuotes, convRefs, convRefTexts, timestamp } */
let pendingMessageQueue = new Map();
let _editingMsgIdx = null,
  _lastRenderedFingerprint = "";
/** Lightweight fingerprint of what's currently rendered — used to skip no-op re-renders from sync */
function _convRenderFingerprint(conv) {
  if (!conv) return "";
  const n = conv.messages.length;
  if (n === 0) return conv.id + ":0:" + (conv.title || "");
  const last = conv.messages[n - 1];
  const sr = last.searchRounds || last.searchResults;
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
    (conv.title || "")
  );
}
let thinkingEnabled = true,
  fetchEnabled = true,
  codeExecEnabled = false,
  browserEnabled = false,
  desktopEnabled = false,
  skillsEnabled = true,
  schedulerEnabled = false,
  swarmEnabled = false,
  endpointEnabled = false,
  imageGenEnabled = false,
  imageGenMode = false,
  humanGuidanceEnabled = false,
  searchMode = "multi",
  debugVisible = false,
  sidebarSearchQuery = "";
let _browserStatusInterval = null;
const serverModel = "aws.claude-opus-4.6";
let config = JSON.parse(
  localStorage.getItem("claude_client_config") ||
    JSON.stringify({
      temperature: 1,
      maxTokens: 128000,
      thinkingBudget: 64000,
      thinkingEffort: "medium",
      imageMaxWidth: 1024,
      systemPrompt: "",
      model: serverModel,
    }),
);
// ★ Migrate: legacy preset/effort keys → config.model (actual model_id)
// Old configs stored brand keys like "qwen", "gemini", "opus".
// New design stores the actual model_id directly in config.model.
const _LEGACY_PRESET_TO_MODEL = {
  'qwen': 'qwen3.6-plus', 'low': 'qwen3.6-plus',
  'gemini': 'gemini-3.1-flash-lite-preview', 'gemini_flash': 'gemini-3-flash-preview',
  'minimax': 'MiniMax-M2.7', 'doubao': 'Doubao-Seed-2.0-pro',
  'opus': 'aws.claude-opus-4.6',
  'medium': 'aws.claude-opus-4.6', 'high': 'aws.claude-opus-4.6', 'max': 'aws.claude-opus-4.6',
};
if (!config.model || config.model === serverModel) {
  // Try migrating from old preset/effort keys
  const _oldPreset = config.preset || config.effort || config.thinkingEffort || '';
  if (_oldPreset && _LEGACY_PRESET_TO_MODEL[_oldPreset]) {
    config.model = _LEGACY_PRESET_TO_MODEL[_oldPreset];
  }
  if (!config.model) config.model = serverModel;
}
// Migrate thinking depth from compound presets
if (['medium','high','max'].includes(config.preset) && !config.thinkingDepth) {
  config.thinkingDepth = config.preset;
}
delete config.effort; // clean up legacy key
delete config.preset; // clean up — no longer used
if (!config.defaultThinkingDepth) config.defaultThinkingDepth = 'off';  // ★ always set — no downstream || 'medium' needed
if (!config.thinkingDepth) config.thinkingDepth = config.defaultThinkingDepth;
// Auto-translate: send Chinese→English to LLM, show bilingual
let autoTranslate = JSON.parse(
  localStorage.getItem("claude_auto_translate") || "true",
);

let projectState = {
  active: false,
  path: "",
  fileCount: 0,
  dirCount: 0,
  totalSize: 0,
  languages: {},
  indexed: false,
  indexedCount: 0,
  indexing: false,
  indexProgress: "",
  scanning: false,
  scanProgress: "",
  scanDetail: "",
  scannedAt: 0,
  extraRoots: [],  // [{name, path, fileCount, dirCount, totalSize, scanning}]
};
let autoApplyWrites = JSON.parse(
  localStorage.getItem("claude_auto_apply") || "true",
);

// ── Pricing state ──
let pricingData = {
  model: serverModel,
  inputPrice: 15.0,
  outputPrice: 75.0,
  usdToCny: 7.24,
  exchangeRateUpdated: 0,
  pricingUpdated: 0,
  pricingSource: "default",
  exchangeRateSource: "none",
  onlineMatchedModel: null,
};

async function loadPricing() {
  try {
    const resp = await fetch(apiUrl("/api/pricing"));
    if (resp.ok) {
      const data = await resp.json();
      pricingData = data;
      debugLog(
        `Pricing loaded: $${data.inputPrice}/1M in, $${data.outputPrice}/1M out, rate=${data.usdToCny}`,
        "success",
      );
      if (typeof _updatePricingDisplay === "function") _updatePricingDisplay();
    }
  } catch (e) {
    debugLog("Pricing load failed: " + e.message, "warn");
  }
}

/* ★ Qwen per-model tiered pricing (CNY/1M tokens) — synced with DashScope 2026-04-02 */
const _qwenModelTiers = {
  'qwen3.6-plus': {
    input:  [[256000, 2.0], [1000000, 8.0]],
    output: [[256000, 12.0], [1000000, 48.0]],
  },
  'qwen3.5-plus': {
    input:  [[128000, 0.8], [256000, 2.0], [1000000, 4.0]],
    output: [[128000, 4.8], [256000, 12.0], [1000000, 24.0]],
  },
  'qwen3.5-flash': {
    input:  [[128000, 0.2], [256000, 0.8], [1000000, 1.2]],
    output: [[128000, 2.0], [256000, 8.0], [1000000, 12.0]],
  },
  'qwen3-max': {
    input:  [[32000, 2.5], [128000, 4.0], [252000, 7.0]],
    output: [[32000, 10.0], [128000, 16.0], [252000, 28.0]],
  },
  'qwen-plus': {
    input:  [[128000, 0.8], [256000, 2.4], [1000000, 4.8]],
    output: [[128000, 2.0], [256000, 20.0], [1000000, 48.0]],
  },
  'qwen-flash': {
    input:  [[128000, 0.15], [256000, 0.6], [1000000, 1.2]],
    output: [[128000, 1.5], [256000, 6.0], [1000000, 12.0]],
  },
  'qwen3-vl-plus': {
    input:  [[32000, 1.0], [128000, 1.5], [256000, 3.0]],
    output: [[32000, 10.0], [128000, 15.0], [256000, 30.0]],
  },
  'qwen3-vl-flash': {
    input:  [[32000, 0.15], [128000, 0.3], [256000, 0.6]],
    output: [[32000, 1.5], [128000, 3.0], [256000, 6.0]],
  },
  'qwen3-coder-plus': {
    input:  [[32000, 4.0], [128000, 6.0], [256000, 10.0], [1000000, 20.0]],
    output: [[32000, 16.0], [128000, 24.0], [256000, 40.0], [1000000, 200.0]],
  },
  'qwen3-coder-flash': {
    input:  [[32000, 1.0], [128000, 1.5], [256000, 2.5], [1000000, 5.0]],
    output: [[32000, 4.0], [128000, 6.0], [256000, 10.0], [1000000, 25.0]],
  },
  'qwq-plus':    { input: [[1000000, 1.6]], output: [[1000000, 4.0]] },
  'qvq-max':     { input: [[1000000, 8.0]], output: [[1000000, 32.0]] },
  'qvq-plus':    { input: [[1000000, 2.0]], output: [[1000000, 5.0]] },
  'qwen-max':    { input: [[1000000, 2.4]], output: [[1000000, 9.6]] },
  'qwen-turbo':  { input: [[1000000, 0.3]], output: [[1000000, 0.6]] },
  'qwen-long':   { input: [[1000000, 0.5]], output: [[1000000, 2.0]] },
  'qwen-vl-max': { input: [[1000000, 1.6]], output: [[1000000, 4.0]] },
  'qwen-vl-plus':{ input: [[1000000, 0.8]], output: [[1000000, 2.0]] },
  '_default': {
    input:  [[128000, 0.8], [256000, 2.0], [1000000, 4.0]],
    output: [[128000, 4.8], [256000, 12.0], [1000000, 24.0]],
  },
};
function _qwenCny(tokens, type, modelId) {
  const mt = _qwenModelTiers[modelId] || _qwenModelTiers['_default'];
  const tiers = mt[type];
  for (const [max, price] of tiers) {
    if (tokens <= max) return (tokens * price) / 1e6;
  }
  return (tokens * tiers[tiers.length - 1][1]) / 1e6;
}
/* ★ Gemini 3.1 Flash-Lite pricing (USD/1M tokens) */
const _geminiPricing = { input: 0.25, output: 1.5, cacheRead: 0.0625 };

/* ★ MiniMax M2.5 pricing (USD/1M tokens) */
const _minimaxPricing = { input: 0.3, output: 1.2, cacheRead: 0.03 };
/* ★ Doubao Seed 2.0 Pro pricing (CNY/1M tokens) */
const _doubaoPricing = { input: 4.0, output: 16.0 };

/* ★ Unified pricing lookup by model_id — replaces the old per-preset branches.
 * Looks up MODEL_PRICING (loaded from server), falls back to pricingData. */
let _modelPricingCache = null;  // populated by loadPricing or /api/server-config

function calcCostCny(usage, modelOrPreset) {
  if (!usage) return null;
  /* Resolve legacy preset keys to model_id for backward compat */
  let modelId = modelOrPreset || '';
  if (_LEGACY_PRESET_TO_MODEL[modelId]) modelId = _LEGACY_PRESET_TO_MODEL[modelId];

  const inp = usage.prompt_tokens || usage.input_tokens || 0;
  let out = usage.completion_tokens || usage.output_tokens || 0;
  const cacheWrite = usage.cache_write_tokens || usage.cache_creation_input_tokens || 0;
  const cacheRead = usage.cache_read_tokens || usage.cache_read_input_tokens || 0;
  const thinkTok = usage.reasoning_tokens || usage.thinking_tokens || 0;
  if (thinkTok > 0 && out === 0) out = thinkTok;
  if (inp === 0 && out === 0 && cacheWrite === 0 && cacheRead === 0) return null;

  const rate = pricingData.usdToCny || 7.24;
  const r6 = (v) => Math.round(v * 1e6) / 1e6;

  /* ★ Qwen tiered pricing (CNY-native) */
  if (/qwen/i.test(modelId)) {
    const inpCny = _qwenCny(inp, "input", modelId);
    const outCny = _qwenCny(out, "output", modelId);
    const totalCny = inpCny + outCny;
    return {
      costUsd: Math.round((totalCny / rate) * 1e4) / 1e4,
      costCny: Math.round(totalCny * 1e4) / 1e4,
      inputTokens: inp, outputTokens: out,
      cacheWriteTokens: cacheWrite, cacheReadTokens: cacheRead, thinkingTokens: thinkTok,
      inputCostCny: r6(inpCny), outputCostCny: r6(outCny),
      cacheWriteCostCny: 0, cacheReadCostCny: 0, cacheSavingsCny: 0, cacheSavingsUsd: 0,
    };
  }

  /* ★ Generic USD pricing — look up from MODEL_PRICING cache or pricingData */
  let baseIn, outP, cwMul = 1.25, crMul = 0.10;
  const mp = _modelPricingCache && _modelPricingCache[modelId];
  if (mp) {
    baseIn = mp.input || 0;
    outP = mp.output || 0;
    if (mp.cacheWriteMul !== undefined) cwMul = mp.cacheWriteMul;
    if (mp.cacheReadMul !== undefined) crMul = mp.cacheReadMul;
  } else {
    baseIn = pricingData.inputPrice;
    outP = pricingData.outputPrice;
  }
  let inputCostUsd = 0, cwCostUsd = 0, crCostUsd = 0;
  const outputCostUsd = (out * outP) / 1e6;
  if (cacheWrite > 0 || cacheRead > 0) {
    const si = Math.max(0, inp - cacheWrite - cacheRead);
    inputCostUsd = (si * baseIn) / 1e6;
    cwCostUsd = (cacheWrite * baseIn * cwMul) / 1e6;
    crCostUsd = (cacheRead * baseIn * crMul) / 1e6;
  } else {
    inputCostUsd = (inp * baseIn) / 1e6;
  }
  const costUsd = inputCostUsd + cwCostUsd + crCostUsd + outputCostUsd;
  const noCacheInputUsd = (inp * baseIn) / 1e6;
  const cacheSavingsUsd = noCacheInputUsd - (inputCostUsd + cwCostUsd + crCostUsd);
  return {
    costUsd: Math.round(costUsd * 1e4) / 1e4,
    costCny: Math.round(costUsd * rate * 1e4) / 1e4,
    inputTokens: inp, outputTokens: out,
    cacheWriteTokens: cacheWrite, cacheReadTokens: cacheRead, thinkingTokens: thinkTok,
    inputCostCny: r6(inputCostUsd * rate),
    outputCostCny: r6(outputCostUsd * rate),
    cacheWriteCostCny: r6(cwCostUsd * rate),
    cacheReadCostCny: r6(crCostUsd * rate),
    cacheSavingsCny: r6(cacheSavingsUsd > 0 ? cacheSavingsUsd * rate : 0),
    cacheSavingsUsd: r6(cacheSavingsUsd > 0 ? cacheSavingsUsd : 0),
  };
}
function formatCny(val) {
  if (val >= 1) return "¥" + val.toFixed(2);
  if (val >= 0.01) return "¥" + val.toFixed(3);
  return "¥" + val.toFixed(4);
}
function calcConversationCost(conv) {
  let tc = 0,
    tu = 0,
    ti = 0,
    to = 0,
    tcw = 0,
    tcr = 0,
    cInp = 0,
    cOut = 0,
    cCw = 0,
    cCr = 0,
    tThink = 0,
    tSav = 0;
  const convModel = conv.model || conv.preset || conv.effort || serverModel;
  for (const m of conv.messages) {
    if (m.usage) {
      const c = calcCostCny(m.usage, m.model || m.preset || m.effort || convModel);
      if (c) {
        tc += c.costCny;
        tu += c.costUsd;
        ti += c.inputTokens;
        to += c.outputTokens;
        tcw += c.cacheWriteTokens;
        tcr += c.cacheReadTokens;
        tThink += c.thinkingTokens;
        cInp += c.inputCostCny;
        cOut += c.outputCostCny;
        cCw += c.cacheWriteCostCny;
        cCr += c.cacheReadCostCny;
        tSav += c.cacheSavingsCny || 0;
      }
    }
  }
  return {
    totalCny: tc,
    totalUsd: tu,
    totalIn: ti,
    totalOut: to,
    totalCacheWrite: tcw,
    totalCacheRead: tcr,
    totalThinking: tThink,
    inputCostCny: cInp,
    outputCostCny: cOut,
    cacheWriteCostCny: cCw,
    cacheReadCostCny: cCr,
    cacheSavingsCny: tSav,
  };
}
function calcAllConversationsCost() {
  let tc = 0,
    tu = 0;
  for (const c of conversations) {
    const r = calcConversationCost(c);
    tc += r.totalCny;
    tu += r.totalUsd;
  }
  return { totalCny: tc, totalUsd: tu };
}
function estimateTokens(text) {
  if (!text) return 0;
  const cjk = (text.match(/[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]/g) || [])
    .length;
  return Math.ceil(cjk / 1.5 + (text.length - cjk) / 4);
}

function debugLog(msg, type = "") {
  console.log(`[${type || "info"}]`, msg);
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
    fetch(apiUrl("/api/client-error"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(() => {});   /* silently ignore network failures */
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
  document.getElementById("debugTitle").textContent = "📨 Messages";
  const p = document.getElementById("debugContent");
  if (p) p._rawMessages = null;
}
function toggleDebug() {
  debugVisible = !debugVisible;
  document
    .getElementById("debugPanel")
    .classList.toggle("visible", debugVisible);
}
// Called on conversation switch: restore cached debug for this conv
function restoreDebugForConv(convId) {
  const cached = _debugCache[convId];
  if (cached && cached.messages && cached.messages.length > 0) {
    showMessagesInDebug(cached.messages, cached.label, false);
  } else {
    // ★ FIX: No memory cache — try to rebuild from conversation data as fallback
    // This covers page refresh / different device scenarios where _debugCache is empty
    const conv = conversations.find((c) => c.id === convId);
    if (
      conv &&
      conv.messages &&
      conv.messages.length > 0 &&
      typeof buildApiMessages === "function"
    ) {
      const rebuilt = buildApiMessages(conv, { includeAll: true });
      if (rebuilt.length > 0) {
        showMessagesInDebug(
          rebuilt,
          `${conv.messages.length}条对话 (reconstructed)`,
          false,
          convId,
        );
        return;
      }
    }
    clearDebug();
  }
}
// ★ Render full messages array into debug panel — supports incremental updates
//   isUpdate=true → streaming update, preserve collapse states, only patch changed blocks
function showMessagesInDebug(messages, label, isUpdate, forConvId) {
  const cid =
    forConvId || (typeof activeConvId !== "undefined" ? activeConvId : null);
  // Cache for conversation switching
  if (cid) _debugCache[cid] = { messages, label };
  // Only render if this conv is currently active (or no conv specified)
  if (
    forConvId &&
    typeof activeConvId !== "undefined" &&
    forConvId !== activeConvId
  )
    return;
  const p = document.getElementById("debugContent");
  if (!p) return;
  const title = document.getElementById("debugTitle");
  if (title)
    title.textContent = `📨 Messages (${messages.length}条)${label ? " — " + label : ""}`;
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
    let s = "#" + (i + 1);
    if (typeof msg.content === "string") {
      s += " · " + (msg.content.length / 1024).toFixed(1) + "KB";
    } else if (Array.isArray(msg.content)) {
      s += " · " + msg.content.length + " blocks";
    }
    if (msg.tool_calls) {
      s += " · " + msg.tool_calls.length + " tool_calls";
    }
    if (msg.tool_call_id) {
      s += " · " + msg.tool_call_id;
    }
    if (msg.name) {
      s += " · fn:" + msg.name;
    }
    return s;
  }
  // Build one block DOM element
  function createBlock(msg, i) {
    const role = msg.role || "unknown";
    const block = document.createElement("div");
    block.className = "debug-msg-block";
    block.dataset.idx = i;
    // Header
    const header = document.createElement("div");
    header.className = "debug-msg-header";
    const roleSpan = document.createElement("span");
    roleSpan.className = "role-" + role;
    roleSpan.textContent = role.toUpperCase();
    header.appendChild(roleSpan);
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
      tcDiv.textContent =
        "🔧 " +
        msg.tool_calls
          .map((tc) => (tc.function ? tc.function.name : "?"))
          .join(", ");
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
  // Generate a fingerprint for a message to detect changes
  function msgFingerprint(msg) {
    const role = msg.role || "";
    let size = 0;
    if (typeof msg.content === "string") size = msg.content.length;
    else if (Array.isArray(msg.content)) size = msg.content.length;
    const tcs = msg.tool_calls ? msg.tool_calls.length : 0;
    const tcid = msg.tool_call_id || "";
    return role + "|" + size + "|" + tcs + "|" + tcid;
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
        // Update tool calls quick view
        const oldTc = existing[i].querySelector(".debug-tool-calls");
        if (messages[i].tool_calls && messages[i].tool_calls.length > 0) {
          const tcText =
            "🔧 " +
            messages[i].tool_calls
              .map((tc) => (tc.function ? tc.function.name : "?"))
              .join(", ");
          if (oldTc) {
            oldTc.textContent = tcText;
          } else {
            const tcDiv = document.createElement("div");
            tcDiv.className = "debug-tool-calls";
            tcDiv.textContent = tcText;
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
    p.scrollTop = 0;
  }
  // Store for copy
  p._rawMessages = messages;
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
    const text = JSON.stringify(msgs, null, 2);
    _safeClipboardWrite(text).then(() => {
      const btn = document.getElementById("debugCopyBtn");
      if (btn) {
        btn.textContent = "✅";
        setTimeout(() => (btn.textContent = "📋"), 1500);
      }
    });
  }
}

function generateId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}
/* ★ Perf: pure string escapeHtml — avoids creating a DOM element on every call.
 * The old DOM approach (createElement+textContent+innerHTML) caused ~50 DOM
 * allocations per renderChat.  Regex replacement is 10-50× faster. */
const _escapeMap = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const _escapeRe = /[&<>"']/g;
function escapeHtml(t) {
  if (!t) return '';
  if (typeof t !== 'string') t = String(t);
  return t.replace(_escapeRe, ch => _escapeMap[ch]);
}
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
function getSearchRoundsFromMsg(msg) {
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

function _broadcastToTabs(type, extra) {
  if (!_syncChannel) return;
  try {
    _syncChannel.postMessage({ type, sourceTab: TAB_ID, ...(extra || {}) });
  } catch (e) {
    debugLog(`[broadcastToTabs] ${e.message}`, 'warn');
  }
}
let _crossTabMergeTimer = 0;
function _handleCrossTabMsg(msg) {
  switch (msg.type) {
    case "conv_saved":
      /* ★ Cross-tab: another tab saved a conversation → refresh from server
       *   to pick up the new/updated conversation metadata. */
      clearTimeout(_crossTabMergeTimer);
      _crossTabMergeTimer = setTimeout(() => {
        if (
          document.visibilityState === "visible" &&
          activeStreams.size === 0 &&
          _editingMsgIdx === null
        )
          loadConversationsFromServer();
      }, 600);
      break;
    case "conv_deleted": {
      const id = msg.convId;
      if (!id) return;
      /* ★ Remove from IndexedDB cache in this tab too */
      ConvCache.remove(id);
      const s = activeStreams.get(id);
      if (s) {
        s.controller.abort();
        activeStreams.delete(id);
      }
      const idx = conversations.findIndex((c) => c.id === id);
      if (idx !== -1) {
        conversations.splice(idx, 1);
        if (activeConvId === id) {
          if (conversations.length > 0) loadConversation(conversations[0].id);
          else newChat();
        } else renderConversationList();
      }
      break;
    }
  }
}
/* ★ No longer listening to localStorage 'storage' events for conversations.
 *   Cross-tab sync now uses BroadcastChannel → server refresh only. */
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    /* ★ PERF: When switching back to the tab during active streaming,
     *   immediately flush a render from the current buffer.  Even though
     *   the setTimeout fallback keeps rendering in background tabs, the
     *   browser throttles it to ~1s intervals.  This ensures the UI is
     *   fully caught up the instant the user sees the tab. */
    if (activeStreams.size > 0 && activeConvId && streamBufs.has(activeConvId)) {
      const buf = streamBufs.get(activeConvId);
      if (buf) {
        updateStreamingUI({
          thinking: buf.thinking,
          content: buf.content,
          searchRounds: buf.searchRounds,
          phase: buf.phase,
        });
        scrollToBottom();
      }
    } else if (activeStreams.size === 0 && _editingMsgIdx === null) {
      loadConversationsFromServer();
    }
  }
});

/* ★ _mergeFromStorage removed — DB is the single source of truth.
 *   Cross-tab sync now goes through BroadcastChannel → loadConversationsFromServer(). */
function _mergeFromStorage() { /* no-op — localStorage no longer used for conversations */ }

function saveConversations(changedConvId) {
  const now = Date.now();
  if (changedConvId) {
    const c = conversations.find((x) => x.id === changedConvId);
    /* ── Don't bump updatedAt during periodic streaming saves ──
     * When multiple conversations stream simultaneously, each calls
     * saveConversations every ~3s.  Bumping updatedAt each time makes
     * them compete for the top sort position, causing the sidebar to
     * flicker as conversations constantly swap order.
     * Fix: only bump updatedAt when the conversation is NOT actively
     * streaming.  The timestamp is already set when the user sends a
     * message (before streaming starts) and again in finishStream()
     * (after activeStreams.delete, so the guard passes). */
    if (c && !activeStreams.has(changedConvId)) c.updatedAt = now;
  }
  /* ★ DB-first: no localStorage merge, no _writeToLocalStorage.
   *   The in-memory array IS the truth for this tab.
   *   The DB is the truth across tabs/sessions. */
  conversations.sort(_convSorter);
  _broadcastToTabs("conv_saved", { convId: changedConvId });

  /* ── Throttled sidebar refresh during streaming ──
   * During active streaming, saveConversations is called every ~3s but
   * renderConversationList was NEVER called — so the sidebar sort order
   * and streaming dot were stale until the stream finished or user clicked
   * another conversation.  We now refresh the sidebar on a 2s throttle
   * so users see the active conversation bubble to the top promptly. */
  if (changedConvId && activeStreams.size > 0) {
    const _now = Date.now();
    if (!saveConversations._lastSidebarRefresh || _now - saveConversations._lastSidebarRefresh > 2000) {
      saveConversations._lastSidebarRefresh = _now;
      requestAnimationFrame(() => {
        if (typeof renderConversationList === 'function') renderConversationList();
      });
    }
  }
}
/* ★ _writeToLocalStorage is now a no-op.
 *   The DB is the single source of truth for conversations.
 *   Keeping the function signature so callers don't need to be updated —
 *   they will be cleaned up incrementally. */
function _writeToLocalStorage() { /* no-op — DB-first architecture */ }

/**
 * Hydrate image base64 from server URLs.
 * After server restart, images loaded from DB only have url (base64 stripped).
 * buildApiMessages needs base64 for LLM vision calls.
 * This fetches each image URL as a blob and converts back to base64.
 */
function _hydrateImageBase64(conv) {
  if (!conv || !conv.messages) { conv._hydratePromise = Promise.resolve(); return; }
  const promises = [];
  for (const msg of conv.messages) {
    if (!msg.images || msg.images.length === 0) continue;
    for (const img of msg.images) {
      if (img.base64) continue;  // already has base64
      const url = img.url || img.preview || "";
      if (!url || url.endsWith("...")) continue;  // truncated placeholder
      // Fetch in background — tracked via promise so buildApiMessages can await
      const p = fetch(url)
        .then(resp => { if (!resp.ok) throw new Error(`HTTP ${resp.status}`); return resp.blob(); })
        .then(blob => new Promise(resolve => {
          const reader = new FileReader();
          reader.onload = () => {
            const dataUrl = reader.result;
            const commaIdx = dataUrl.indexOf(",");
            if (commaIdx > 0) {
              img.base64 = dataUrl.slice(commaIdx + 1);
              if (!img.mediaType) {
                const match = dataUrl.match(/^data:([^;]+)/);
                if (match) img.mediaType = match[1];
              }
              if (!img.preview || img.preview === url)
                img.preview = dataUrl;
            }
            resolve();
          };
          reader.onerror = () => resolve();  // don't block on read errors
          reader.readAsDataURL(blob);
        }))
        .catch(e => {
          console.warn(`[hydrate] Failed to fetch base64 for image url=${url.slice(0, 80)}: ${e.message}`);
        });
      promises.push(p);
    }
  }
  if (promises.length > 0) {
    console.info(`[hydrate] Fetching base64 for ${promises.length} image(s) in conv=${conv.id.slice(0, 8)}`);
    conv._hydratePromise = Promise.all(promises).then(() => {
      console.info(`[hydrate] Completed ${promises.length} image(s) for conv=${conv.id.slice(0, 8)}`);
    });
  } else {
    conv._hydratePromise = Promise.resolve();
  }
}

// ── Debounced sync: coalesces rapid settings toggles into one request ──
// finishStream() calls syncConversationToServer() directly (immediate).
// Settings/toggle changes call syncConversationToServerDebounced() which
// waits 1.5s for additional changes before firing.
const _syncDebounceTimers = new Map();  // convId → timeoutId
function syncConversationToServerDebounced(conv, delayMs = 1500) {
  const existing = _syncDebounceTimers.get(conv.id);
  if (existing) clearTimeout(existing);
  _syncDebounceTimers.set(conv.id, setTimeout(() => {
    _syncDebounceTimers.delete(conv.id);
    syncConversationToServer(conv);
  }, delayMs));
}

async function syncConversationToServer(conv) {
  try {
    /* Guard: skip sync while actively streaming — the assistant message is
     * incomplete, and uploading it would overwrite the server-side accumulator
     * with a partial snapshot.  finishStream() will trigger sync after done. */
    if (activeStreams.has(conv.id)) {
      debugLog(`[syncToServer] Skipped — conv ${conv.id.slice(0,8)} is actively streaming`, 'info');
      return;
    }
    /* Guard: never sync a conversation with zero messages to the server.
     * This prevents the race where _saveConvToolState fires before the user
     * message is pushed, overwriting the server with messages:[]. */
    if (!conv.messages || conv.messages.length === 0) {
      console.log(`[syncToServer] Skipped — conv ${conv.id.slice(0,8)} has 0 messages (nothing to sync)`);
      return;
    }
    /* Guard: never overwrite server with fewer messages (data loss prevention).
     * Also never sync a completely empty conversation if _needsLoad is still set
     * (means we haven't successfully loaded from server yet). */
    if (conv._serverMsgCount && conv.messages.length < conv._serverMsgCount) {
      console.warn(`[syncToServer] ⚠️ SKIPPED sync for conv=${conv.id.slice(0,8)} — local ${conv.messages.length} msgs < server ${conv._serverMsgCount} msgs. ` +
        `This guard prevents overwriting server data, but local changes (including streamed content) will NOT be persisted to server!`);
      return;
    }
    /* ★ CROSS-TALK DETECTION: check for sudden message count jumps that indicate injection */
    if (conv._lastSyncMsgCount !== undefined && conv.messages.length > conv._lastSyncMsgCount + 3) {
      console.error(
        `[syncToServer] ⛔ MESSAGE COUNT JUMP: conv=${conv.id.slice(0,8)} jumped from ` +
        `${conv._lastSyncMsgCount} to ${conv.messages.length} msgs (+${conv.messages.length - conv._lastSyncMsgCount}) ` +
        `since last sync — possible cross-talk injection! ` +
        `activeConvId=${activeConvId?.slice(0,8)||'null'} ` +
        `activeStreams=[${[...activeStreams.keys()].map(k=>k.slice(0,8)).join(',')}]`
      );
      /* Log the extra messages for forensic analysis */
      for (let i = conv._lastSyncMsgCount; i < conv.messages.length; i++) {
        const m = conv.messages[i];
        console.error(
          `[syncToServer] ⛔ INJECTED MSG #${i}: role=${m.role} ` +
          `contentLen=${(m.content||'').length} taskId=${m._taskId?.slice(0,8)||'N/A'} ` +
          `model=${m.model||'N/A'} timestamp=${m.timestamp}`
        );
      }
    }
    conv._lastSyncMsgCount = conv.messages.length;
    if (conv.messages.length === 0 && conv._needsLoad) {
      console.warn(`[syncToServer] ⚠️ SKIPPED sync for conv=${conv.id.slice(0,8)} — 0 local messages and _needsLoad=true (not yet loaded from server)`);
      return;
    }
    const lastMsg = conv.messages[conv.messages.length - 1];
    /* ★ CROSS-TALK DETECTION: check if any messages have foreign task IDs or
     *   unexpected model/content patterns that suggest they belong to another conv */
    const _convTaskId = conv.activeTaskId;
    let _foreignMsgCount = 0;
    for (const m of conv.messages) {
      if (m._taskId && _convTaskId && m._taskId !== _convTaskId && m.role === 'assistant') {
        _foreignMsgCount++;
      }
    }
    if (_foreignMsgCount > 0) {
      console.error(
        `[syncToServer] ⛔ CROSS-TALK DETECTED: conv=${conv.id.slice(0,8)} has ${_foreignMsgCount} ` +
        `assistant message(s) with foreign taskId (conv.activeTaskId=${_convTaskId?.slice(0,8)||'null'}). ` +
        `These messages may have been injected from another conversation's SSE stream!`
      );
    }
    console.info(`[syncToServer] conv=${conv.id.slice(0,8)} msgs=${conv.messages.length} lastRole=${lastMsg?.role} ` +
      `contentLen=${lastMsg?.content?.length||0} thinkingLen=${lastMsg?.thinking?.length||0} hasError=${!!lastMsg?.error} ` +
      `activeTaskId=${_convTaskId?.slice(0,8)||'null'} foreignMsgCount=${_foreignMsgCount}`);
    const lightMsgs = conv.messages.map((m) => {
      let r = m;
      if (m.images?.length > 0)
        r = {
          ...r,
          images: m.images.map((img) => {
            const o = { mediaType: img.mediaType, sizeKB: img.sizeKB };
            if (img.url) {
              o.url = img.url;
              o.preview = img.url;
            } else {
              o.preview = (img.preview || "").slice(0, 200) + "...";
            }
            if (img.pdfPage) o.pdfPage = img.pdfPage;
            if (img.pdfTotal) o.pdfTotal = img.pdfTotal;
            if (img.pdfName) o.pdfName = img.pdfName;
            if (img.caption) o.caption = img.caption;
            return o;
          }),
        };
      if (m.pdfTexts?.length > 0)
        r = {
          ...r,
          pdfTexts: m.pdfTexts.map((p) => ({
            name: p.name,
            pages: p.pages,
            textLength: p.textLength,
            isScanned: p.isScanned,
            method: p.method,
            text: p.text || "",
          })),
        };
      return r;
    });
    const settings = {
      preset: conv.model || conv.preset,
      model: conv.model || conv.preset,
      thinkingDepth: conv.thinkingDepth || config.defaultThinkingDepth,
      defaultThinkingDepth: config.defaultThinkingDepth,
      searchMode: conv.searchMode,
      fetchEnabled: conv.fetchEnabled,
      codeExecEnabled: conv.codeExecEnabled,
      browserEnabled: conv.browserEnabled,
      desktopEnabled: conv.desktopEnabled || false,
      skillsEnabled: conv.skillsEnabled !== undefined ? conv.skillsEnabled : true,
      schedulerEnabled: conv.schedulerEnabled || false,
      swarmEnabled: conv.swarmEnabled || false,
      endpointEnabled: conv.endpointEnabled || false,
      imageGenEnabled: conv.imageGenEnabled || false,
      imageGenMode: conv.imageGenMode || false,
      imageGenModel: conv.imageGenModel || null,
      humanGuidanceEnabled: conv.humanGuidanceEnabled || false,
      projectPath: conv.projectPath,
      projectPaths: conv.projectPaths || [],
      autoTranslate: conv.autoTranslate,
      pinned: conv.pinned || false,
      pinnedAt: conv.pinnedAt || 0,
      /* ★ Persist activeTaskId so the server knows which task is associated
       *   with this conversation.  On page reload, initActiveTasks reads this
       *   to recover completed task results even when the SSE stream died. */
      activeTaskId: conv.activeTaskId || null,
      /* ★ Persist last message info so initActiveTasks can detect orphaned user
       *   messages even for _needsLoad shell convs (metadata-only, no messages loaded).
       *   Without this, Case E is skipped for shell convs → orphan stuck forever. */
      lastMsgRole: lastMsg?.role || null,
      lastMsgTimestamp: lastMsg?.timestamp || null,
    };
    const resp = await fetch(apiUrl(`/api/conversations/${conv.id}`), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: conv.title,
        messages: lightMsgs,
        createdAt: conv.createdAt,
        updatedAt: conv.updatedAt || Date.now(),
        settings,
      }),
    });
    if (resp.ok) {
      conv._serverMsgCount = lightMsgs.length;
      debugLog(`[syncToServer] ✅ Conv ${conv.id.slice(0,8)} synced ${lightMsgs.length} msgs to server`, 'info');
      /* ★ Write-through: update IndexedDB cache with the synced state.
       *   This ensures the cache always reflects the latest server-confirmed data,
       *   so the next page load gets an instant cache hit with fresh content. */
      ConvCache.put(conv);
    } else {
      const errBody = await resp.json().catch(() => ({}));
      debugLog(`[syncToServer] ⚠️ Conv ${conv.id.slice(0,8)} sync rejected: ${resp.status} ${errBody.error || ''}`, 'warn');
    }
  } catch (e) {
    debugLog(`[syncToServer] ❌ Sync failed for ${conv.id.slice(0,8)}: ${e.message}`, "warn");
  }
}
function _applySettingsToConv(conv, settings) {
  if (!settings) return;
  if (settings.model || settings.effort || settings.preset)
    conv.model = settings.model || settings.preset || settings.effort;
  if (settings.thinkingDepth) conv.thinkingDepth = settings.thinkingDepth;
  if (settings.searchMode) conv.searchMode = settings.searchMode;
  if (settings.fetchEnabled !== undefined)
    conv.fetchEnabled = settings.fetchEnabled;
  if (settings.codeExecEnabled !== undefined)
    conv.codeExecEnabled = settings.codeExecEnabled;
  if (settings.browserEnabled !== undefined)
    conv.browserEnabled = settings.browserEnabled;
  if (settings.desktopEnabled !== undefined)
    conv.desktopEnabled = settings.desktopEnabled;
  if (settings.skillsEnabled !== undefined)
    conv.skillsEnabled = settings.skillsEnabled;
  if (settings.schedulerEnabled !== undefined)
    conv.schedulerEnabled = settings.schedulerEnabled;
  if (settings.swarmEnabled !== undefined)
    conv.swarmEnabled = settings.swarmEnabled;
  if (settings.endpointEnabled !== undefined)
    conv.endpointEnabled = settings.endpointEnabled;
  if (settings.imageGenEnabled !== undefined)
    conv.imageGenEnabled = settings.imageGenEnabled;
  if (settings.imageGenMode !== undefined)
    conv.imageGenMode = settings.imageGenMode;
  if (settings.humanGuidanceEnabled !== undefined)
    conv.humanGuidanceEnabled = settings.humanGuidanceEnabled;
  if (settings.imageGenModel)
    conv.imageGenModel = settings.imageGenModel;
  if (settings.projectPath !== undefined)
    conv.projectPath = settings.projectPath;
  if (settings.projectPaths !== undefined)
    conv.projectPaths = settings.projectPaths;
  if (settings.autoTranslate !== undefined)
    conv.autoTranslate = settings.autoTranslate;
  if (settings.pinned !== undefined) conv.pinned = settings.pinned;
  if (settings.pinnedAt !== undefined) conv.pinnedAt = settings.pinnedAt;
  if (settings.source) conv.source = settings.source;
  if (settings.feishuUser) conv.feishuUser = settings.feishuUser;
  /* ★ Persist last message info for Case E orphan detection on _needsLoad shells */
  if (settings.lastMsgRole) conv.lastMsgRole = settings.lastMsgRole;
  if (settings.lastMsgTimestamp) conv.lastMsgTimestamp = settings.lastMsgTimestamp;
  /* ★ Restore activeTaskId from server settings — enables Case B recovery
   *   even on a fresh browser session (no localStorage).
   *   Guard: if activeTaskId was cleared locally during this session
   *   (_activeTaskClearedAt exists), NEVER restore from server metadata.
   *   This prevents stale activeTaskId values stuck in DB from causing
   *   phantom purple dots on the sidebar.  On a true page refresh,
   *   _activeTaskClearedAt won't exist (ephemeral), so initActiveTasks
   *   will properly validate against /api/chat/active before restoring. */
  if (settings.activeTaskId && !conv.activeTaskId) {
    if (!conv._activeTaskClearedAt) {
      conv.activeTaskId = settings.activeTaskId;
    }
  }
}
let _convMetaEtag = null;   // ETag for 304 Not Modified support
async function loadConversationsFromServer(prefetchId) {
  try {
    /* ── Fast path: only metadata for sidebar (no messages) ── */
    const headers = {};
    /* When prefetching, skip ETag/304 — we need fresh data + the conv body */
    if (!prefetchId && _convMetaEtag) headers['If-None-Match'] = _convMetaEtag;
    const url = prefetchId
      ? apiUrl(`/api/conversations?meta=1&prefetch=${encodeURIComponent(prefetchId)}`)
      : apiUrl("/api/conversations?meta=1");
    let resp;
    for (let _attempt = 0; _attempt < 3; _attempt++) {
      resp = await fetch(url, { headers });
      if (resp.status === 503) {
        const delay = (resp.headers.get('Retry-After') || (_attempt + 1)) * 1000;
        debugLog(`[loadConvs] 503 DB busy, retry ${_attempt + 1}/2 in ${delay}ms`, 'warn');
        await new Promise(r => setTimeout(r, delay));
        continue;
      }
      break;
    }
    if (resp.status === 304) return;   // nothing changed — skip all work
    if (!resp.ok) return;
    let serverConvs, prefetchedConv = null;
    if (prefetchId) {
      /* Combo response: { conversations: [...], prefetched: {...} | null } */
      const combo = await resp.json();
      serverConvs = combo.conversations || [];
      prefetchedConv = combo.prefetched || null;
    } else {
      _convMetaEtag = resp.headers.get('ETag') || null;
      serverConvs = await resp.json();
    }
    console.log(`[loadConversationsFromServer] Got ${serverConvs.length} convs from server, local has ${conversations.length}`);
    if (!serverConvs.length) return;
    const localMap = new Map(conversations.map((c) => [c.id, c]));
    let merged = false,
      acChanged = false;
    for (const sc of serverConvs) {
      const local = localMap.get(sc.id);
      if (!local) {
        /* New conversation from server — create shell with empty messages */
        const nc = {
          id: sc.id,
          title: sc.title,
          messages: [],
          _serverMsgCount: sc.messageCount || 0,
          _needsLoad: (sc.messageCount || 0) > 0,
          createdAt: sc.createdAt,
          updatedAt: sc.updatedAt || sc.createdAt,
          activeTaskId: null,
        };
        _applySettingsToConv(nc, sc.settings);
        conversations.push(nc);
        merged = true;
      } else if (!activeStreams.has(sc.id) && !local.activeTaskId) {
        /* Update metadata for existing conversations */
        const sT = sc.updatedAt || sc.createdAt || 0,
          mT = local.updatedAt || local.createdAt || 0;
        const serverMsgCount = sc.messageCount || 0;
        if (serverMsgCount > local.messages.length || sT > mT) {
          local.title = sc.title;
          local.updatedAt = sc.updatedAt || sc.createdAt;
          local._serverMsgCount = serverMsgCount;
          if (serverMsgCount > local.messages.length) {
            local._needsLoad = true;
          }
          /* Preserve local pinned state — pinning is a client-side
             preference and must not be overwritten by a potentially
             stale server snapshot during periodic refresh.           */
          const keepPinned = local.pinned, keepPinnedAt = local.pinnedAt;
          _applySettingsToConv(local, sc.settings);
          local.pinned = keepPinned; local.pinnedAt = keepPinnedAt;
          if (sc.id === activeConvId) acChanged = true;
          merged = true;
        }
      }
    }
    /* ★ Rescue local-only conversations that exist in memory (from this
     *   session) but haven't been synced to the server yet.  This handles
     *   the edge case where the user sent a message while the server was
     *   briefly unreachable.  Ghost convs (empty, not on server) are simply
     *   dropped — they have no data worth saving. */
    const serverIdSet = new Set(serverConvs.map(sc => sc.id));
    for (const lc of conversations) {
      if (!serverIdSet.has(lc.id) && !activeStreams.has(lc.id)) {
        if (lc.messages.length > 0) {
          console.warn(`[loadConversationsFromServer] ★ Rescuing local-only conv ${lc.id.slice(0,8)} ` +
            `(${lc.messages.length} msgs) — syncing to server`);
          syncConversationToServer(lc);
        } else if (lc.id !== activeConvId) {
          /* Empty local-only conv — drop it silently (it was never meaningful) */
          conversations = conversations.filter(c => c.id !== lc.id);
          merged = true;
        }
      }
    }

    /* ── Apply prefetched conversation data (eliminates second round-trip) ── */
    if (prefetchedConv && prefetchedConv.id) {
      const pc = conversations.find(c => c.id === prefetchedConv.id);
      if (pc && pc._needsLoad && !activeStreams.has(pc.id) && !pc.activeTaskId) {
        const serverMsgs = prefetchedConv.messages || [];
        pc.messages = serverMsgs;
        pc.title = prefetchedConv.title || pc.title;
        pc.updatedAt = prefetchedConv.updatedAt || prefetchedConv.updated_at || pc.updatedAt;
        const keepPinned = pc.pinned, keepPinnedAt = pc.pinnedAt;
        _applySettingsToConv(pc, prefetchedConv.settings);
        if (keepPinned) { pc.pinned = keepPinned; pc.pinnedAt = keepPinnedAt; }
        pc._needsLoad = false;
        pc._serverMsgCount = Math.max(serverMsgs.length, pc.messages.length);
        console.log(`[loadConversationsFromServer] ⚡ Prefetched conv ${pc.id.slice(0,8)}: ${serverMsgs.length} msgs — no second fetch needed`);
        /* ★ Update IndexedDB cache with the prefetched data */
        ConvCache.put(pc);
        merged = true;
      }
    }

    console.log(`[loadConversationsFromServer] merged=${merged}, total conversations now: ${conversations.length}, ` +
      `visible: ${conversations.filter(c => c.messages.length > 0 || (c._serverMsgCount||0) > 0 || c._needsLoad).length}`);
    if (merged) {
      conversations.sort(_convSorter);
      renderConversationList();
      /* If the active conversation needs a full reload, do it now */
      if (activeConvId) {
        const ac = getActiveConv();
        if (ac && ac._needsLoad) {
          await loadConversationMessages(activeConvId);
        } else if (acChanged && ac && !ac.activeTaskId) {
          renderChat(ac, false);
          if (typeof _restoreConvToolState === "function")
            _restoreConvToolState(ac);
        }
      }
    }
  } catch (e) {
    debugLog(`Server load: ${e.message}`, "warn");
  }
}

/**
 * Load full messages for a single conversation on demand.
 * Returns the conversation object or null.
 */
async function loadConversationMessages(convId) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) return null;
  /* Skip if already loaded and not stale */
  if (!conv._needsLoad && conv.messages.length > 0) return conv;

  /* ═══ Phase 1: Try IndexedDB cache for instant render ═══ */
  let cacheHit = false;
  try {
    const cached = await ConvCache.get(convId);
    if (cached && cached.messages && cached.messages.length > 0) {
      /* Serve from cache immediately — user sees content with zero network wait */
      conv.messages = cached.messages;
      conv.title = cached.title || conv.title;
      /* Apply cached settings (preserving local overrides like pinned) */
      const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
      if (cached.settings) _applySettingsToConv(conv, cached.settings);
      if (keepPinned) { conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt; }
      conv._needsLoad = false;
      conv._serverMsgCount = Math.max(cached.messages.length, conv._serverMsgCount || 0);
      conv._cachedUpdatedAt = cached.updatedAt || 0;
      cacheHit = true;
      console.info(`[loadConvMsgs] ⚡ CACHE HIT conv=${convId.slice(0,8)}: ${cached.messages.length} msgs (cachedAt=${new Date(cached.cachedAt).toISOString()})`);
      /* Hydrate images from cache (URLs only, base64 stripped) */
      _hydrateImageBase64(conv);
      /* Render immediately from cache */
      if (convId === activeConvId) {
        if (activeStreams.has(convId)) {
          if (typeof showStreamingUIForConv === "function") showStreamingUIForConv(convId);
        } else {
          renderChat(conv, false);
          if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);
        }
      }
    }
  } catch (cacheErr) {
    console.warn(`[loadConvMsgs] Cache read failed for ${convId.slice(0,8)}: ${cacheErr.message}`);
  }

  /* ═══ Phase 2: Fetch from server (verify freshness or first load) ═══ */
  try {
    let resp;
    /* Timeout: if server is frozen (FUSE/dolphins), don't hang forever.
     * With cache hit: user already sees content, 10s is generous for background check.
     * Without cache hit: 10s before showing retry button is acceptable. */
    const _fetchTimeout = cacheHit ? 10000 : 15000;
    const _mkSignal = typeof AbortSignal !== 'undefined' && AbortSignal.timeout
      ? (ms) => AbortSignal.timeout(ms)
      : (ms) => { const c = new AbortController(); setTimeout(() => c.abort(), ms); return c.signal; };
    for (let _attempt = 0; _attempt < 3; _attempt++) {
      resp = await fetch(apiUrl(`/api/conversations/${convId}`), {
        signal: _mkSignal(_fetchTimeout),
      });
      if (resp.status === 503) {
        /* DB temporarily busy — wait and retry */
        const delay = (resp.headers.get('Retry-After') || (_attempt + 1)) * 1000;
        debugLog(`[loadConvMsgs] ${convId.slice(0,8)}: 503 DB busy, retry ${_attempt + 1}/2 in ${delay}ms`, 'warn');
        await new Promise(r => setTimeout(r, delay));
        continue;
      }
      break;
    }
    if (!resp.ok) {
      /* ── 404 ghost: conversation exists in sidebar but not on server ──
       * This happens when a conv was created locally but never synced to the DB
       * (e.g. disk I/O error, race condition, or the conv was only ever empty).
       * Without cleanup, the conv stays in the sidebar forever with _needsLoad=true,
       * and every click triggers another 404 → permanent "redirect to New Chat" loop.
       * Fix: mark it as fully loaded (empty) so _purgeEmptyConvs can remove it. */
      if (resp.status === 404) {
        debugLog(`[loadConvMsgs] ${convId.slice(0,8)}: 404 NOT FOUND — conversation not on server`, 'warn');
        /* Clean up stale cache entry too */
        ConvCache.remove(convId);
        /* If this is the active conversation, show a user-friendly message */
        if (convId === activeConvId) {
          const inner = document.getElementById('chatInner');
          if (inner) {
            inner.innerHTML = `<div class="welcome" id="welcome"><div class="welcome-icon">⚠️</div><h2>Conversation Not Found</h2><p>This conversation (<code>${convId}</code>) was not saved to the server.<br>It may have been created during a server error or was never synced.</p><p style="margin-top:1em"><button onclick="deleteConversation('${convId}')" class="action-btn" style="padding:8px 16px;cursor:pointer">🗑️ Remove from sidebar</button>&nbsp;&nbsp;<button onclick="newChat()" class="action-btn" style="padding:8px 16px;cursor:pointer">✨ New Chat</button></p></div>`;
          }
        }
        /* Remove the orphan from the in-memory array + sidebar */
        conversations = conversations.filter(c => c.id !== convId);
        if (typeof renderConversationList === 'function') renderConversationList();
      }
      /* If we had a cache hit, the user already sees content — just return */
      return conv;
    }
    const data = await resp.json();
    const serverMsgs = data.messages || [];
    const serverUpdatedAt = data.updatedAt || data.updated_at || 0;

    /* ── Freshness check: is server data newer than what we rendered from cache? ── */
    const hasLocalData = conv.messages.length > 0;
    const isStreaming = activeStreams.has(convId);
    /* Use reduce instead of Math.max(...) to avoid stack overflow on huge conversations */
    const localNewest = hasLocalData ? conv.messages.reduce((mx, m) => Math.max(mx, m.timestamp || 0), 0) : 0;
    const serverNewest = serverMsgs.length > 0 ? serverMsgs.reduce((mx, m) => Math.max(mx, m.timestamp || 0), 0) : 0;
    /* Only treat local data as "unsynced" if it came from the current session
     * (NOT from IndexedDB cache). Cached data is old server data — if the server
     * now has fewer/different messages (compaction, deletion), server wins. */
    const localHasUnsynced = hasLocalData && !cacheHit && localNewest > serverNewest;

    if (localHasUnsynced) {
      console.warn(`[loadConvMsgs] ⚠️ KEPT local data for conv=${convId.slice(0,8)} — ` +
        `local has ${conv.messages.length} msgs (newest=${new Date(localNewest).toISOString()}) ` +
        `vs server ${serverMsgs.length} msgs (newest=${new Date(serverNewest).toISOString()}). ` +
        `Will re-sync to server.`);
      syncConversationToServer(conv);
    } else if (conv.activeTaskId && hasLocalData) {
      /* ★ FIX: Active task with stale local data (e.g. IDB cache from before
       *   task started).  We can't replace conv.messages (would orphan the
       *   assistantMsg ref held by connectToTask), but we CAN merge server
       *   checkpoint data into the existing assistant message so the UI shows
       *   accumulated content immediately — instead of "Waiting…" until SSE. */
      const lastLocal = conv.messages[conv.messages.length - 1];
      if (lastLocal && lastLocal.role === 'assistant' && serverMsgs.length > 0) {
        const lastServer = serverMsgs[serverMsgs.length - 1];
        if (lastServer && lastServer.role === 'assistant') {
          /* Only upgrade: server content longer than local → server had checkpoint */
          if ((lastServer.content || '').length > (lastLocal.content || '').length) {
            lastLocal.content = lastServer.content;
          }
          if ((lastServer.thinking || '').length > (lastLocal.thinking || '').length) {
            lastLocal.thinking = lastServer.thinking;
          }
          if (lastServer.searchRounds?.length && !lastLocal.searchRounds?.length) {
            lastLocal.searchRounds = lastServer.searchRounds;
          }
          /* Also update the stream buffer if one exists (for showStreamingUIForConv) */
          const buf = streamBufs.get(convId);
          if (buf) {
            if (!buf.content && lastLocal.content) buf.content = lastLocal.content;
            if (!buf.thinking && lastLocal.thinking) buf.thinking = lastLocal.thinking;
            if (!buf.searchRounds?.length && lastLocal.searchRounds?.length)
              buf.searchRounds = lastLocal.searchRounds.map(r => ({...r}));
          }
        }
      }
      /* ★ FIX: Merge translatedContent from server messages into local messages.
       *   When entering this branch (activeTaskId set), we keep local messages
       *   to avoid orphaning refs, but the IDB cache may be stale and missing
       *   translations that the server has (from _commit_translation_to_db).
       *   Without this merge, translations disappear when viewing a conv with
       *   a stale activeTaskId, then get unnecessarily regenerated. */
      const _mergeLen = Math.min(conv.messages.length, serverMsgs.length);
      for (let _mi = 0; _mi < _mergeLen; _mi++) {
        const lm = conv.messages[_mi], sm = serverMsgs[_mi];
        if (sm.translatedContent && !lm.translatedContent) {
          lm.translatedContent = sm.translatedContent;
          lm._showingTranslation = sm._showingTranslation !== false;
          lm._translateDone = true;
        }
        /* Also merge finishReason/usage/model if local lacks them */
        if (sm.finishReason && !lm.finishReason) lm.finishReason = sm.finishReason;
        if (sm.usage && !lm.usage) lm.usage = sm.usage;
        if (sm.model && !lm.model) lm.model = sm.model;
      }
      conv._needsLoad = false;
      conv._serverMsgCount = Math.max(serverMsgs.length, conv.messages.length);
    } else if (!hasLocalData || (!activeStreams.has(convId) && !conv.activeTaskId)) {
      /* Apply server data when:
       *  - No local data (first load, no cache)
       *  - Not actively streaming AND no active task starting
       * ★ FIX: Previously, `cacheHit` bypassed the activeTaskId guard, causing
       *   Phase 2 server response to overwrite conv.messages even when
       *   startAssistantResponse had just pushed an assistant message and was
       *   awaiting POST /api/chat/start. This race condition caused connectToTask
       *   to see a user message as the last message → bail out → no SSE stream
       *   → sidebar shows pulsing dot but no Agent icon in chat area.
       *   Now we ALWAYS re-check activeTaskId/activeStreams at overwrite time. */
      const cacheIsStale = !cacheHit ||
        serverMsgs.length !== conv.messages.length ||
        serverUpdatedAt > (conv._cachedUpdatedAt || 0);

      if (cacheIsStale) {
        conv.messages = serverMsgs;
        conv.title = data.title || conv.title;
        conv.updatedAt = serverUpdatedAt || conv.updatedAt;
        const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
        _applySettingsToConv(conv, data.settings);
        if (keepPinned) { conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt; }

        if (cacheHit) {
          console.info(`[loadConvMsgs] 🔄 Cache STALE for conv=${convId.slice(0,8)} — ` +
            `server has ${serverMsgs.length} msgs (updatedAt=${serverUpdatedAt}), re-rendering`);
        }
      } else {
        console.info(`[loadConvMsgs] ✅ Cache FRESH for conv=${convId.slice(0,8)} — no re-render needed`);
      }

      conv._needsLoad = false;
      conv._serverMsgCount = Math.max(serverMsgs.length, conv.messages.length);

      /* ★ Update IndexedDB cache with authoritative server data */
      ConvCache.put(conv);

      debugLog(`[loadConvMsgs] ${convId.slice(0,8)}: server=${serverMsgs.length} msgs, local=${conv.messages.length} msgs, _serverMsgCount=${conv._serverMsgCount}, cacheHit=${cacheHit}`, 'info');

      /* Hydrate image base64 from server URLs */
      _hydrateImageBase64(conv);

      /* Re-render if server data was newer (or first load with no cache) */
      if (cacheIsStale && convId === activeConvId) {
        if (activeStreams.has(convId)) {
          if (typeof showStreamingUIForConv === "function") showStreamingUIForConv(convId);
        } else {
          renderChat(conv, false);
          if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);
        }
      }
    }

    /* ★ Re-trigger HG translations for any awaiting_human rounds after load.
     *   On page refresh, translation state is lost (only in-memory). This
     *   re-fires translation for pending guidance cards so users see Chinese. */
    if (typeof _retriggerHgTranslations === 'function') {
      _retriggerHgTranslations(convId);
    }

    /* Clean up transient cache tracking field */
    delete conv._cachedUpdatedAt;
    return conv;
  } catch (e) {
    debugLog(`Load conv ${convId}: ${e.message}`, "warn");
    /* If we had a cache hit, the user already sees content — just log the fetch failure */
    if (cacheHit) {
      console.warn(`[loadConvMsgs] ⚠️ Server fetch failed for ${convId.slice(0,8)} but cache was served: ${e.message}`);
      return conv;
    }
    /* Network errors with no cache: show a retry-friendly message if this is the active conv */
    if (convId === activeConvId) {
      const inner = document.getElementById('chatInner');
      if (inner && conv._needsLoad && conv.messages.length === 0) {
        inner.innerHTML = `<div class="welcome" id="welcome" style="opacity:0.7"><div class="welcome-icon">⚡</div><h2>Failed to load conversation</h2><p>${e.message}</p><p style="margin-top:1em"><button onclick="loadConversation('${convId}')" class="action-btn" style="padding:8px 16px;cursor:pointer">🔄 Retry</button></p></div>`;
      }
    }
    return conv;
  }
}

/**
 * Force-recover a conversation from server, ignoring local state.
 * Use when local messages appear truncated or missing.
 * Can be called from browser console: forceRecoverFromServer(convId)
 */
async function forceRecoverFromServer(convId) {
  convId = convId || activeConvId;
  if (!convId) { debugLog('[recover] No conversation ID', 'error'); return null; }
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) { debugLog(`[recover] Conversation not found: ${convId}`, 'error'); return null; }
  try {
    const resp = await fetch(apiUrl(`/api/conversations/${convId}`));
    if (!resp.ok) { debugLog(`[recover] Server returned ${resp.status}`, 'error'); return null; }
    const data = await resp.json();
    const serverMsgs = data.messages || [];
    const localMsgs = conv.messages || [];
    console.log(`[recover] Conv ${convId}: local has ${localMsgs.length} msgs, server has ${serverMsgs.length} msgs`);
    if (serverMsgs.length > localMsgs.length) {
      conv.messages = serverMsgs;
      conv.title = data.title || conv.title;
      conv.updatedAt = data.updatedAt || data.updated_at || conv.updatedAt;
      conv._serverMsgCount = serverMsgs.length;
      conv._needsLoad = false;
      const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
      _applySettingsToConv(conv, data.settings);
      if (keepPinned) { conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt; }
      saveConversations(convId);
      if (convId === activeConvId) {
        renderChat(conv, false);
        if (typeof _restoreConvToolState === 'function') _restoreConvToolState(conv);
      }
      console.log(`[recover] ✅ Restored ${serverMsgs.length} messages (was ${localMsgs.length})`);
      return conv;
    } else {
      console.log(`[recover] ℹ️ Server has same or fewer messages — no recovery needed`);
      return conv;
    }
  } catch (e) {
    debugLog(`[recover] Failed: ${e.message}`, 'error');
    return null;
  }
}

/**
 * Audit all conversations for data loss: compare local message count vs server.
 * Run from console: auditConversations()
 */
async function auditConversations() {
  console.log('[audit] Checking all conversations for data loss...');
  const issues = [];
  for (const conv of conversations) {
    try {
      const resp = await fetch(apiUrl(`/api/conversations/${conv.id}`));
      if (!resp.ok) continue;
      const data = await resp.json();
      const serverCount = (data.messages || []).length;
      const localCount = (conv.messages || []).length;
      if (serverCount > localCount) {
        issues.push({ id: conv.id, title: conv.title, localCount, serverCount, diff: serverCount - localCount });
        console.warn(`[audit] ⚠️ "${conv.title}" — local: ${localCount}, server: ${serverCount} (+${serverCount - localCount} recoverable)`);
      }
    } catch (e) { /* skip */ }
  }
  if (issues.length === 0) {
    console.log('[audit] ✅ No data loss detected — all conversations match server');
  } else {
    console.log(`[audit] Found ${issues.length} conversation(s) with recoverable data:`);
    console.table(issues);
    console.log('[audit] Run forceRecoverFromServer("conv_id") to recover, or recoverAll() to fix all');
  }
  return issues;
}

/**
 * Batch recover all conversations that have more messages on server than locally.
 * Run from console: recoverAll()
 */
async function recoverAll() {
  const issues = await auditConversations();
  if (issues.length === 0) return;
  let recovered = 0;
  for (const issue of issues) {
    const result = await forceRecoverFromServer(issue.id);
    if (result) recovered++;
    await new Promise(r => setTimeout(r, 200)); /* small delay to not hammer server */
  }
  console.log(`[recoverAll] ✅ Recovered ${recovered}/${issues.length} conversations`);
}

/**
 * Clear the IndexedDB conversation cache.
 * Run from console: clearConvCache()
 * Or programmatically for troubleshooting.
 */
async function clearConvCache() {
  const before = await ConvCache.stats();
  await ConvCache.clear();
  console.log(`[clearConvCache] ✅ Cleared ${before.count} cached conversations`);
  return before.count;
}

/**
 * Show IndexedDB cache statistics.
 * Run from console: convCacheStats()
 */
async function convCacheStats() {
  const s = await ConvCache.stats();
  console.log(`[convCacheStats] available=${s.available}, count=${s.count}`);
  return s;
}

// ── Markdown ──
if (typeof marked !== "undefined") marked.setOptions({ breaks: true });
/* ★ Perf: reuse a single temp div for all DOM-based HTML transforms in renderMarkdown.
 * Previously, highlightCodeInHtml + _addApplyButtons + processLongCodeBlocks each created
 * their own temp div and did innerHTML parse → serialize, meaning 3 full DOM round-trips
 * per renderMarkdown call.  Now we parse once, apply all transforms, serialize once. */
let _mdTempDiv = null;
function _getMdTemp() {
  if (!_mdTempDiv) _mdTempDiv = document.createElement('div');
  return _mdTempDiv;
}
/* ★ Perf: skip lang set created once (was re-created every call) */
const _applySkipLangs = new Set(["bash","shell","sh","console","terminal","cmd","powershell","zsh"]);
/* ★ Perf: _singlePassDomTransform — one DOM parse, all transforms, one serialize.
 * Replaces the old 3-pass approach (highlightCodeInHtml → _addApplyButtons → processLongCodeBlocks)
 * which each did innerHTML=html, transform, html=temp.innerHTML — 3 full DOM round-trips.
 * Now: 1 parse + 1 serialize = saves ~3-5ms per renderMarkdown call. */
function _singlePassDomTransform(html) {
  /* ★ Perf: skip DOM parse entirely when no <pre> blocks — saves ~1-3ms for plain text */
  if (!html.includes('<pre')) return html;
  const temp = _getMdTemp();
  temp.innerHTML = html;
  const pres = temp.querySelectorAll('pre');
  const hasHljs = typeof hljs !== 'undefined';
  const hasProject = projectState && projectState.active;
  for (let pi = 0; pi < pres.length; pi++) {
    const pre = pres[pi];
    const code = pre.querySelector('code');
    if (!code) continue;
    /* --- Phase 1: Syntax highlighting (was highlightCodeInHtml) --- */
    if (hasHljs) {
      const lm = code.className.match(/language-(\w+)/);
      const lang = lm ? lm[1] : null;
      const text = code.textContent;
      try {
        if (lang && hljs.getLanguage(lang))
          code.innerHTML = hljs.highlight(text, { language: lang }).value;
        else
          code.innerHTML = hljs.highlightAuto(text).value;
      } catch (_) {}
      code.classList.add('hljs');
    }
    /* --- Phase 2: Apply buttons for project mode (was _addApplyButtons) --- */
    if (hasProject) {
      const hdr = pre.querySelector('.code-header');
      if (hdr) {
        const langSpan = hdr.querySelector('span');
        const lang = langSpan ? langSpan.textContent.trim().toLowerCase() : '';
        if (!_applySkipLangs.has(lang)) {
          const lines = code.textContent.trim().split('\n');
          if (lines.length > 3) {
            const btn = document.createElement('button');
            btn.className = 'apply-btn';
            btn.textContent = 'Apply';
            btn.setAttribute('onclick', 'openApplyModal(this)');
            hdr.appendChild(btn);
          }
        }
      }
    }
    /* --- Phase 3: Collapse long code blocks (was processLongCodeBlocks) --- */
    const lc = code.textContent.split('\n').length;
    if (lc > 15) {
      pre.classList.add('code-long');
      pre.setAttribute('data-collapsed', 'true');
      const hdr = pre.querySelector('.code-header');
      if (hdr) {
        const sp = hdr.querySelector('span');
        if (sp) sp.textContent += ` \u00b7 ${lc} lines`;
        const btn = document.createElement('button');
        btn.className = 'code-collapse-btn';
        btn.textContent = 'Expand';
        btn.setAttribute('onclick', 'toggleCodeBlock(this)');
        hdr.insertBefore(btn, hdr.querySelector('.copy-btn'));
      }
    }
  }
  return temp.innerHTML;
}
/* Legacy wrappers (kept for any external callers) */
function highlightCodeInHtml(html) {
  if (typeof hljs === 'undefined') return html;
  const temp = _getMdTemp();
  temp.innerHTML = html;
  temp.querySelectorAll('pre code').forEach((el) => {
    const lm = el.className.match(/language-(\w+)/);
    const lang = lm ? lm[1] : null;
    const text = el.textContent;
    try {
      if (lang && hljs.getLanguage(lang))
        el.innerHTML = hljs.highlight(text, { language: lang }).value;
      else el.innerHTML = hljs.highlightAuto(text).value;
    } catch (e) {}
    el.classList.add('hljs');
  });
  return temp.innerHTML;
}
function _addApplyButtons(html) {
  if (!projectState || !projectState.active) return html;
  const temp = _getMdTemp();
  temp.innerHTML = html;
  temp.querySelectorAll("pre").forEach((pre) => {
    const hdr = pre.querySelector(".code-header");
    if (!hdr) return;
    const langSpan = hdr.querySelector("span");
    const lang = langSpan ? langSpan.textContent.trim().toLowerCase() : "";
    if (_applySkipLangs.has(lang)) return;
    const code = pre.querySelector("code");
    if (code) {
      const lines = code.textContent.trim().split("\n");
      if (lines.length <= 3) return;
    }
    const btn = document.createElement("button");
    btn.className = "apply-btn";
    btn.textContent = "Apply";
    btn.setAttribute("onclick", "openApplyModal(this)");
    hdr.appendChild(btn);
  });
  return temp.innerHTML;
}
function extractFencedBlocks(text, codeStore) {
  const lines = text.split("\n");
  const result = [];
  let i = 0;
  while (i < lines.length) {
    const open = lines[i].match(/^(`{3,}|~{3,})(.*)/);
    if (!open) {
      result.push(lines[i]);
      i++;
      continue;
    }
    const fChar = open[1][0],
      fLen = open[1].length,
      esc = fChar === "`" ? "`" : "~";
    const closeRe = new RegExp("^" + esc + "{" + fLen + ",}\\s*$");
    const innerOpenRe = new RegExp("^" + esc + "{3,}\\S");
    let closeIdx = -1,
      depth = 1;
    for (let j = i + 1; j < lines.length; j++) {
      if (closeRe.test(lines[j])) {
        depth--;
        if (depth === 0) {
          closeIdx = j;
          break;
        }
      } else if (innerOpenRe.test(lines[j])) depth++;
    }
    if (closeIdx === -1) {
      let lastClose = -1;
      for (let j = i + 1; j < lines.length; j++) {
        if (closeRe.test(lines[j])) lastClose = j;
        else if (lastClose !== -1 && innerOpenRe.test(lines[j])) {
          if (j > lastClose + 1) break;
        }
      }
      closeIdx = lastClose;
    }
    if (closeIdx === -1) {
      codeStore.push(lines.slice(i).join("\n"));
      result.push("\x02CODE" + (codeStore.length - 1) + "\x03");
      i = lines.length;
    } else {
      codeStore.push(lines.slice(i, closeIdx + 1).join("\n"));
      result.push("\x02CODE" + (codeStore.length - 1) + "\x03");
      i = closeIdx + 1;
    }
  }
  return result.join("\n");
}
function upgradeFenceIfNeeded(block) {
  const lines = block.split("\n");
  if (lines.length < 2) return block;
  const open = lines[0].match(/^(`{3,}|~{3,})(.*)/);
  if (!open) return block;
  const fChar = open[1][0],
    fLen = open[1].length,
    lang = open[2],
    esc = fChar === "`" ? "`" : "~";
  let maxInner = 0;
  for (let k = 1; k < lines.length - 1; k++) {
    const m = lines[k].match(new RegExp("^(" + esc + "{3,})"));
    if (m) maxInner = Math.max(maxInner, m[1].length);
  }
  if (maxInner >= fLen) {
    const nf = fChar.repeat(maxInner + 1);
    lines[0] = nf + lang;
    lines[lines.length - 1] = nf;
    return lines.join("\n");
  }
  return block;
}
/* ── Markdown render cache ── */
const _mdCache = new Map();
const _MD_CACHE_MAX = 300;
/* ★ Perf: O(1) cache key — length + first/last 64 chars instead of O(n) full-text hash.
 * For 10k+ char responses this avoids hashing every char on each streaming frame.
 * Collision risk is negligible: same length + same head + same tail is near-impossible
 * for different markdown content. */
function _mdCacheKey(text) {
  const n = text.length;
  if (n <= 128) {
    /* Short text: hash all chars — still fast */
    let h = 0;
    for (let i = 0; i < n; i++) {
      h = ((h << 5) - h + text.charCodeAt(i)) | 0;
    }
    return h + ":" + n;
  }
  /* Long text: sample first 64 + last 64 chars */
  let h = 0;
  for (let i = 0; i < 64; i++) {
    h = ((h << 5) - h + text.charCodeAt(i)) | 0;
  }
  for (let i = n - 64; i < n; i++) {
    h = ((h << 5) - h + text.charCodeAt(i)) | 0;
  }
  return h + ":" + n;
}
function invalidateMdCache() {
  _mdCache.clear();
}

function renderMarkdown(text) {
  if (!text) return "";
  if (typeof marked === "undefined" || typeof marked.parse !== "function") {
    return '<pre style="white-space:pre-wrap">' + escapeHtml(text) + "</pre>";
  }
  try {
  const _ck = _mdCacheKey(text);
  if (_mdCache.has(_ck)) {
    return _mdCache.get(_ck);
  }
  const codeStore = [];
  let p = extractFencedBlocks(text, codeStore);
  p = p.replace(/(`[^`\n]+`)/g, (m) => {
    codeStore.push(m);
    return "\x02CODE" + (codeStore.length - 1) + "\x03";
  });
  const mathStore = [];
  p = p.replace(/\$\$([\s\S]*?)\$\$/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: true });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  p = p.replace(/\\\[([\s\S]*?)\\\]/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: true });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  p = p.replace(/(?<!\$)\$(?!\$)((?:[^$\\]|\\.)+?)\$(?!\$)/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: false });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  p = p.replace(/\\\((.*?)\\\)/g, (_, t) => {
    mathStore.push({ tex: t.trim(), display: false });
    return "\x02MATH" + (mathStore.length - 1) + "\x03";
  });
  for (let i = 0; i < codeStore.length; i++) {
    p = p
      .split("\x02CODE" + i + "\x03")
      .join(upgradeFenceIfNeeded(codeStore[i]));
  }
  let html =
    typeof DOMPurify !== "undefined"
      ? DOMPurify.sanitize(marked.parse(p))
      : marked.parse(p);
  /* ★ Perf: consolidated single-pass DOM transform.
   * Previously this did 3 separate innerHTML parse→serialize round-trips:
   *   highlightCodeInHtml (parse→serialize) → regex → _addApplyButtons (parse→serialize) → processLongCodeBlocks (parse→serialize)
   * Now: one parse, all transforms in-memory, one serialize.  Saves ~3-5ms per renderMarkdown call. */
  html = html.replace(
    /<pre><code class="language-(\w+)[^"]*">/g,
    '<pre><div class="code-header"><span>$1</span><button class="copy-btn" onclick="copyCode(this)">Copy</button></div><code class="language-$1">',
  );
  html = html.replace(
    /<pre><code class="hljs">/g,
    '<pre><div class="code-header"><span>code</span><button class="copy-btn" onclick="copyCode(this)">Copy</button></div><code>',
  );
  html = html.replace(
    /<pre><code>/g,
    '<pre><div class="code-header"><span>code</span><button class="copy-btn" onclick="copyCode(this)">Copy</button></div><code>',
  );
  html = _singlePassDomTransform(html);
  // Wrap <table> elements in a scrollable container with copy button
  html = html.replace(/<table>/g, '<div class="md-table-wrapper"><div class="table-header"><span>table</span><button class="copy-btn" onclick="copyTableMarkdown(this)">Copy</button></div><table>');
  html = html.replace(/<\/table>/g, '</table></div>');
  if (mathStore.length > 0 && typeof katex !== 'undefined') {
    for (let i = 0; i < mathStore.length; i++) {
      const { tex, display } = mathStore[i];
      let r;
      try {
        r = katex.renderToString(tex, {
          displayMode: display,
          throwOnError: false,
          trust: true,
          strict: false,
        });
      } catch (e) {
        r = `<code class="math-error">${escapeHtml(tex)}</code>`;
      }
      const ph = `\x02MATH${i}\x03`;
      if (display) html = html.split(`<p>${ph}</p>`).join(r);
      html = html.split(ph).join(r);
    }
  } else if (mathStore.length > 0) {
    /* KaTeX not loaded yet — lazy-load and re-render */
    _ensureKatex();
    /* Meanwhile, show raw TeX as fallback */
    for (let i = 0; i < mathStore.length; i++) {
      const { tex, display } = mathStore[i];
      const ph = `\x02MATH${i}\x03`;
      const fallback = `<code class="math-pending">${escapeHtml(tex)}</code>`;
      if (display) html = html.split(`<p>${ph}</p>`).join(fallback);
      html = html.split(ph).join(fallback);
    }
  }
  if (_mdCache.size >= _MD_CACHE_MAX) {
    const first = _mdCache.keys().next().value;
    _mdCache.delete(first);
  }
  _mdCache.set(_ck, html);
  return html;
  } catch (e) {
    console.warn('renderMarkdown: marked.parse() failed, using fallback', e);
    return '<pre style="white-space:pre-wrap">' + escapeHtml(text) + "</pre>";
  }
}
function processLongCodeBlocks(html) {
  const temp = _getMdTemp();
  temp.innerHTML = html;
  temp.querySelectorAll("pre").forEach((pre) => {
    const code = pre.querySelector("code");
    if (!code) return;
    const lc = code.textContent.split("\n").length;
    if (lc > 15) {
      pre.classList.add("code-long");
      pre.setAttribute("data-collapsed", "true");
      const hdr = pre.querySelector(".code-header");
      if (hdr) {
        const sp = hdr.querySelector("span");
        if (sp) sp.textContent += ` · ${lc} lines`;
        const btn = document.createElement("button");
        btn.className = "code-collapse-btn";
        btn.textContent = "Expand";
        btn.setAttribute("onclick", "toggleCodeBlock(this)");
        hdr.insertBefore(btn, hdr.querySelector(".copy-btn"));
      }
    }
  });
  return temp.innerHTML;
}
function toggleCodeBlock(btn) {
  const pre = btn.closest("pre");
  const c = pre.getAttribute("data-collapsed") === "true";
  pre.setAttribute("data-collapsed", c ? "false" : "true");
  btn.textContent = c ? "Collapse" : "Expand";
}
function copyCode(btn) {
  const code = btn.closest("pre").querySelector("code").textContent;
  _safeClipboardWrite(code);
  btn.textContent = "Copied!";
  setTimeout(() => (btn.textContent = "Copy"), 1500);
}
function copyTableMarkdown(btn) {
  const wrapper = btn.closest(".md-table-wrapper");
  const table = wrapper.querySelector("table");
  if (!table) return;
  const rows = table.querySelectorAll("tr");
  if (!rows.length) return;
  const lines = [];
  rows.forEach((tr, i) => {
    const cells = tr.querySelectorAll("th, td");
    const vals = Array.from(cells).map(c => c.textContent.replace(/\|/g, "\\|").trim());
    lines.push("| " + vals.join(" | ") + " |");
    if (i === 0) {
      lines.push("| " + vals.map(() => "---").join(" | ") + " |");
    }
  });
  _safeClipboardWrite(lines.join("\n"));
  btn.textContent = "Copied!";
  setTimeout(() => (btn.textContent = "Copy"), 1500);
}
/* ── Stream timing: elapsed display + health-check-based stuck detection ── */
const _streamTimers = new Map(); // convId → { startTime, lastDataTime, intervalId, healthState }
// _serverAlive: cached health state shared across all streams (avoid duplicate pings)
let _serverAlive = true;
let _lastHealthCheck = 0;
let _consecutiveHealthFails = 0;       // require 2+ consecutive fails to confirm dead
const _HEALTH_CHECK_INTERVAL = 10000;  // ms between health checks when silent
const _SILENCE_THRESHOLD = 20;         // seconds of silence before first health check (reduced from 30s for VS Code port forwarding)
const _SILENCE_SEVERE = 45;            // seconds before showing severe warning

function _fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m${rs > 0 ? String(rs).padStart(2,'0') + 's' : ''}`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h${rm > 0 ? String(rm).padStart(2,'0') + 'm' : ''}`;
}

/**
 * Check if backend server is alive. Returns true/false.
 * Result is cached for _HEALTH_CHECK_INTERVAL ms to avoid spamming.
 */
async function _checkServerHealth() {
  const now = Date.now();
  if (now - _lastHealthCheck < _HEALTH_CHECK_INTERVAL) return _serverAlive;
  _lastHealthCheck = now;
  try {
    const resp = await fetch(apiUrl('/api/health'), { signal: AbortSignal.timeout(3000) });
    if (resp.ok) {
      _serverAlive = true;
      _consecutiveHealthFails = 0;
    } else {
      _consecutiveHealthFails++;
      _serverAlive = _consecutiveHealthFails < 2; // need 2+ failures to confirm dead
    }
  } catch {
    _consecutiveHealthFails++;
    _serverAlive = _consecutiveHealthFails < 2;
  }
  return _serverAlive;
}

/**
 * Check if PostgreSQL database is available on startup.
 * Shows a persistent warning banner if DB is down so users know
 * immediately instead of seeing silent "Waiting…" on first message.
 */
async function _checkDbHealth() {
  try {
    const resp = await fetch(apiUrl('/api/health'), { signal: AbortSignal.timeout(3000) });
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.db_ok === false) {
      _showDbWarningBanner();
    }
  } catch {
    // Server itself is unreachable — _checkServerHealth handles that
  }
}

function _showDbWarningBanner() {
  if (document.getElementById('db-warning-banner')) return;
  const banner = document.createElement('div');
  banner.id = 'db-warning-banner';
  banner.style.cssText =
    'position:fixed;top:0;left:0;right:0;z-index:10000;' +
    'background:#dc2626;color:#fff;padding:10px 16px;font-size:14px;' +
    'text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3);' +
    'display:flex;align-items:center;justify-content:center;gap:8px;';
  banner.innerHTML =
    '<span style="font-size:18px">⚠️</span>' +
    '<span><b>Database Unavailable</b> — PostgreSQL is not running. ' +
    'Conversations and history will not work. ' +
    'Install PostgreSQL (<code style="background:rgba(255,255,255,.2);padding:1px 5px;border-radius:3px">' +
    'conda install -c conda-forge postgresql>=18</code>) and restart the server.</span>' +
    '<button onclick="this.parentElement.remove()" style="' +
    'background:rgba(255,255,255,.2);border:none;color:#fff;padding:4px 10px;' +
    'border-radius:4px;cursor:pointer;font-size:13px;margin-left:12px;' +
    'white-space:nowrap">Dismiss</button>';
  document.body.prepend(banner);
}

/**
 * Force-finish a stream for a given convId when server is detected as dead.
 * Sets finishReason so the user sees what happened.
 */
function _forceFinishDeadStream(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (conv) {
    const last = conv.messages[conv.messages.length - 1];
    if (last && last.role === 'assistant' && !last.finishReason) {
      last.finishReason = 'server_offline';
      last.error = '⚠️ Server offline — response may be incomplete. Refresh page after server restarts.';
    }
  }
  // Abort the SSE controller so _trySSE / _pollFallback also exit
  const s = activeStreams.get(convId);
  if (s && s.controller) {
    try { s.controller.abort(); } catch {}
  }
  twStop(convId);
  finishStream(convId);
  showToast('⚠️', 'Server Offline', 'Backend server is not responding. Your partial response has been saved.', 8000);
}

async function _updateStreamTimerUI(convId) {
  if (activeConvId !== convId) return;
  const info = _streamTimers.get(convId);
  if (!info) return;
  const el = document.getElementById('stream-elapsed-timer');
  if (!el) return;

  const now = Date.now();
  const elapsedSec = Math.floor((now - info.startTime) / 1000);
  const silentSec = Math.floor((now - info.lastDataTime) / 1000);

  // Show elapsed time always (subtle)
  let elapsedHtml = `<span class="stream-elapsed">${_fmtElapsed(now - info.startTime)}</span>`;

  // During tool execution, LLM thinking, or retrying, silence is expected — only show elapsed
  const buf = streamBufs.get(convId);
  if (buf && buf.phase && (buf.phase.phase === 'tool_exec' || buf.phase.phase === 'llm_thinking' || buf.phase.phase === 'retrying')) {
    el.innerHTML = elapsedHtml;
    return;
  }

  // Short silence — just show elapsed
  if (silentSec < _SILENCE_THRESHOLD) {
    el.innerHTML = elapsedHtml;
    return;
  }

  // Extended silence — run health check + task completion probe (async, non-blocking)
  if (silentSec >= _SILENCE_THRESHOLD && !info._healthChecking) {
    info._healthChecking = true;
    _checkServerHealth().then(async (alive) => {
      info._healthChecking = false;
      info._lastHealthResult = alive;
      if (!alive) {
        console.error(`[StreamTimer] Server health check FAILED for conv=${convId.slice(0,8)} after ${silentSec}s silence`);
        // Auto-finish if server is dead and silence > severe threshold
        if (silentSec >= _SILENCE_SEVERE) {
          _forceFinishDeadStream(convId);
        }
        return;
      }
      /* ★ SERVER IS ALIVE but SSE is silent — the proxy (VS Code port forwarding,
       *   nginx, corporate proxy) may have swallowed the 'done' event.
       *   Proactively poll the task to check if it already finished. If so, abort
       *   the stale SSE connection so connectToTask falls through to _pollFallback,
       *   which will retrieve the completed result. */
      const conv = conversations.find(c => c.id === convId);
      const taskId = conv?.activeTaskId;
      if (!taskId) return;
      try {
        const probeResp = await fetch(apiUrl(`/api/chat/poll/${taskId}`), { signal: AbortSignal.timeout(5000) });
        if (!probeResp.ok) return;
        const probeData = await probeResp.json();
        if (probeData.status && probeData.status !== 'running') {
          console.warn(
            `[StreamTimer] ★ TASK ALREADY DONE but SSE stuck — conv=${convId.slice(0,8)} ` +
            `task=${taskId.slice(0,8)} status=${probeData.status} ` +
            `content=${(probeData.content||'').length}chars — ` +
            `aborting stale SSE to trigger poll fallback recovery`
          );
          // Abort the SSE controller — this causes _trySSE to exit with AbortError.
          // We set _probeAbort flag so _trySSE knows this is a timer probe (not user stop)
          // and falls through to _pollFallback instead of treating it as user abort.
          const stream = activeStreams.get(convId);
          if (stream && stream.controller) {
            stream._probeAbort = true;
            stream.controller.abort();
          }
        } else {
          // Task is still running — silence is expected (LLM thinking, tool executing)
          // The SSE pipe might just be slow. Touch the timer to reduce noise.
          console.debug(`[StreamTimer] Task ${taskId.slice(0,8)} still running — silence is expected`);
        }
      } catch (probeErr) {
        // Probe failed — don't take action, next tick will retry
        console.debug(`[StreamTimer] Task probe failed: ${probeErr.message}`);
      }
    });
  }

  // Build warning display
  if (info._lastHealthResult === false) {
    // Server confirmed dead
    el.innerHTML = elapsedHtml +
      ` <span class="stream-stuck-severe">⚠️ server offline</span>` +
      ` <button class="stream-force-finish-btn" onclick="_forceFinishDeadStream('${convId}')">Force Finish</button>`;
  } else if (silentSec >= _SILENCE_SEVERE) {
    el.innerHTML = elapsedHtml +
      ` <span class="stream-stuck-severe">${silentSec}s no update</span>` +
      ` <button class="stream-force-finish-btn" onclick="_forceFinishDeadStream('${convId}')">Force Finish</button>`;
  } else {
    el.innerHTML = elapsedHtml +
      ` <span class="stream-stuck-warn">${silentSec}s no update</span>`;
  }
}

function _streamTimerTouch(convId) {
  const info = _streamTimers.get(convId);
  if (info) {
    info.lastDataTime = Date.now();
    info._lastHealthResult = undefined; // reset — server is clearly alive if we got data
    _serverAlive = true;
    _consecutiveHealthFails = 0;
  }
}

function twStart(convId) {
  streamBufs.set(convId, {
    content: "",
    thinking: "",
    searchRounds: [],
    phase: null,
  });
  // Start elapsed timer
  const now = Date.now();
  const existing = _streamTimers.get(convId);
  if (existing && existing.intervalId) clearInterval(existing.intervalId);
  const intervalId = setInterval(() => _updateStreamTimerUI(convId), 1000);
  _streamTimers.set(convId, { startTime: now, lastDataTime: now, intervalId, _lastHealthResult: undefined, _healthChecking: false });
  _serverAlive = true; // optimistic on stream start
}
/* ── Coalesced streaming update: multiple SSE events between frames are merged ── */
let _twRafId = null;
let _twPendingConvId = null;
let _twTimeoutId = null; // fallback timer when page is hidden (rAF paused)
let _twDirty = false;    // data changed since last render

function _twFlush() {
  _twRafId = null;
  _twDirty = false;
  if (_twTimeoutId) { clearTimeout(_twTimeoutId); _twTimeoutId = null; }
  const cid = _twPendingConvId;
  /* ★ CROSS-TALK DETECTION: log when we render streaming data for a conv
   *   that is NOT the currently viewed conversation */
  if (activeConvId && cid && activeConvId !== cid) {
    console.debug(
      `[twUpdate] bg conv=${cid.slice(0,8)} triggered rAF while viewing ${activeConvId.slice(0,8)}`
    );
  }
  /* ★ FIX: Always render the active conversation if it's streaming, regardless
   *   of which convId triggered this rAF.  When multiple conversations stream
   *   concurrently, a background conv's twUpdate overwrites _twPendingConvId
   *   before the rAF fires, causing the active conv's rendering to be silently
   *   skipped for that frame.  This manifests as the UI appearing "stuck" even
   *   though data is accumulating in the buffers — the user has to switch convs
   *   to trigger showStreamingUIForConv which reads from the buffer directly.
   *
   *   Fix: prefer activeConvId as the render target (if it has a streamBuf),
   *   falling back to cid only during init (activeConvId not yet set). */
  const renderCid = (activeConvId && streamBufs.has(activeConvId)) ? activeConvId : cid;
  if (renderCid === activeConvId || (!activeConvId && document.getElementById('streaming-body'))) {
    const buf = streamBufs.get(renderCid);
    if (buf)
      updateStreamingUI({
        thinking: buf.thinking,
        content: buf.content,
        searchRounds: buf.searchRounds,
        phase: buf.phase,
      });
  }
}

function twUpdate(convId) {
  _streamTimerTouch(convId); // mark data received
  _twPendingConvId = convId;
  _twDirty = true;

  /* ★ PERF FIX: When the page/tab is hidden, browsers pause requestAnimationFrame
   *   callbacks entirely.  SSE data keeps arriving and accumulating in the buffer,
   *   but no render happens.  When the user switches back, a SINGLE rAF fires and
   *   renders ALL buffered content at once — causing the "bunch of content popping
   *   up all at once" symptom.
   *
   *   Fix: schedule BOTH a rAF (for smooth 60fps when visible) AND a setTimeout
   *   fallback (fires even when hidden, at ~1s throttle in background tabs).
   *   Whichever fires first cancels the other via _twDirty flag. */
  if (!_twRafId) {
    _twRafId = requestAnimationFrame(_twFlush);
  }
  /* Background-tab fallback: setTimeout still fires (≥1s in hidden tabs).
   * Only schedule if not already pending.  The 250ms delay means we batch
   * ~250ms of SSE events per render in background tabs — much smoother than
   * waiting for the tab to become visible again. */
  if (!_twTimeoutId) {
    _twTimeoutId = setTimeout(() => {
      _twTimeoutId = null;
      if (_twDirty) {
        if (_twRafId) { cancelAnimationFrame(_twRafId); _twRafId = null; }
        _twFlush();
      }
    }, 250);
  }
}
function twStop(convId) {
  streamBufs.delete(convId);
  if (typeof _pendingStreamTimer !== "undefined" && _pendingStreamTimer) {
    clearInterval(_pendingStreamTimer);
    _pendingStreamTimer = null;
  }
  _pendingStreamMsg = null;
  // Cancel any pending twUpdate timers
  if (_twTimeoutId) { clearTimeout(_twTimeoutId); _twTimeoutId = null; }
  if (_twRafId) { cancelAnimationFrame(_twRafId); _twRafId = null; }
  _twDirty = false;
  // Invalidate zone cache and incremental render state
  if (typeof _streamZoneCache !== "undefined") _streamZoneCache = { body: null, tool: null, think: null, content: null, status: null };
  // Stop elapsed timer
  const timerInfo = _streamTimers.get(convId);
  if (timerInfo) {
    if (timerInfo.intervalId) clearInterval(timerInfo.intervalId);
    _streamTimers.delete(convId);
  }
}

/* ── Toast Notifications ── */
const _toastTypes = {
  success: { icon: '✓', cls: 't-success', dur: 3000 },
  error:   { icon: '✕', cls: 't-error',   dur: 6000 },
  warning: { icon: '!', cls: 't-warning', dur: 5000 },
  warn:    { icon: '!', cls: 't-warning', dur: 5000 },
  info:    { icon: 'i', cls: 't-info',    dur: 3500 },
};

/**
 * showToast — flexible API:
 *   showToast("消息文本", "success")           ← simple (message + type)
 *   showToast("✅", "Title", "detail", 5000)   ← full   (icon, title, detail, ms)
 */
function showToast(iconOrMsg, titleOrType, detail, durationMs) {
  const c = document.getElementById('toastContainer');
  if (!c) return;

  /* ── Detect which API form ── */
  const isSimple = !titleOrType || (typeof titleOrType === 'string' && titleOrType in _toastTypes);
  let title, type, dur;

  if (isSimple) {
    type  = (titleOrType && titleOrType in _toastTypes) ? titleOrType : 'info';
    title = iconOrMsg || '';
    detail = null;
    dur   = _toastTypes[type].dur;
  } else {
    // Full form: showToast(icon, title, detail?, dur?)
    // We fold icon into the title since the new design uses a typed icon circle
    title  = titleOrType || '';
    dur    = durationMs || 4000;
    // Infer type from the icon/title text
    if (/✅|✓|💡|saved|success/i.test(iconOrMsg + title)) type = 'success';
    else if (/❌|✕|fail|error/i.test(iconOrMsg + title)) type = 'error';
    else if (/⚠|warn/i.test(iconOrMsg + title)) type = 'warning';
    else type = 'info';
  }

  const info = _toastTypes[type] || _toastTypes.info;

  /* ── Build DOM ── */
  const t = document.createElement('div');
  t.className = 'toast';
  t.innerHTML =
    `<div class="toast-icon-wrap ${info.cls}">${info.icon}</div>` +
    `<div class="toast-body">` +
      `<span class="toast-title">${title}</span>` +
      (detail ? `<span class="toast-detail">${detail}</span>` : '') +
    `</div>` +
    `<button class="toast-close" aria-label="close">×</button>` +
    `<div class="toast-progress ${info.cls}" style="width:100%;animation:toastTimer ${dur}ms linear forwards"></div>`;

  /* ── Dismiss logic ── */
  let timer, paused = false;
  const dismiss = () => {
    if (t._dismissed) return;
    t._dismissed = true;
    t.classList.add('removing');
    setTimeout(() => t.remove(), 300);
  };
  t.querySelector('.toast-close').onclick = dismiss;
  c.appendChild(t);
  timer = setTimeout(dismiss, dur);

  /* Pause on hover */
  const prog = t.querySelector('.toast-progress');
  t.addEventListener('mouseenter', () => {
    paused = true;
    clearTimeout(timer);
    if (prog) prog.style.animationPlayState = 'paused';
  });
  t.addEventListener('mouseleave', () => {
    paused = false;
    if (prog) prog.style.animationPlayState = 'running';
    timer = setTimeout(dismiss, 1500);
  });
}

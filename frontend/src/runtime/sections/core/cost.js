/* ===== migrated source: core/cost.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/cost.js — extracted from core.js (split 2026-05-28)

   Per-message cost calculation: legacy preset migration, server-authoritative calcCostCny, _prefetchConvCosts batch, calcConversationCost rollup.

   This file is concatenated by Vite's module graph AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

// Migrate: legacy preset/effort keys → config.model (actual model_id)
// Old configs stored brand keys like "qwen", "gemini", "opus".
// New design stores the actual model_id directly in config.model.
const _LEGACY_PRESET_TO_MODEL = {
  'qwen': 'qwen3.6-plus', 'low': 'qwen3.6-plus',
  'gemini': 'gemini-3-flash-preview', 'gemini_flash': 'gemini-3-flash-preview',
  'minimax': 'MiniMax-M2.7', 'doubao': 'Doubao-Seed-2.0-pro',
  'opus': 'aws.claude-opus-4.7',
  'medium': 'aws.claude-opus-4.7', 'high': 'aws.claude-opus-4.7', 'max': 'aws.claude-opus-4.7',
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
if (['medium','high','xhigh','max'].includes(config.preset) && !config.thinkingDepth) {
  config.thinkingDepth = config.preset;
}
delete config.effort; // clean up legacy key
delete config.preset; // clean up — no longer used
if (!config.defaultThinkingDepth) config.defaultThinkingDepth = 'medium';  // always set — no downstream || 'medium' needed
if (!config.thinkingDepth) config.thinkingDepth = config.defaultThinkingDepth;
// Migrate legacy hardcoded imageMaxWidth=1024 (the bug behind the "uploaded
// images are blurry" complaint). Old default was 1024+JPEG q=0.85, more
// aggressive than the backend's 2048+q=0.90 — so the client always won and
// the backend's better policy never applied. Now: 0 = follow server policy.
// Users who *intentionally* set a tighter cap keep their value.
if (config.imageMaxWidth === 1024) config.imageMaxWidth = 0;
// Auto-translate: send Chinese→English to LLM, show bilingual.
// Default OPT-IN (OFF) — matches the backend canonical
// lib.conv_config.AUTO_TRANSLATE_DEFAULT so the toolbar toggle display and
// every trigger path agree (the historical three-way default split).
let autoTranslate = _safeJsonParse(
  localStorage.getItem("claude_auto_translate"), false,
);

let projectState = {
  active: false,
  path: "",
  fileCount: 0,
  dirCount: 0,
  totalSize: 0,
  languages: {},

  scanning: false,
  scanProgress: "",
  scanDetail: "",
  scannedAt: 0,
  extraRoots: [],  // [{name, path, fileCount, dirCount, totalSize, scanning}]
};
/* Write mode: default AUTO (no approval prompts) everywhere. A per-conversation
 * Manual override lives on conv.autoApply — restored on conv switch by
 * restoreConversationSettingsToComposer and persisted via
 * captureActiveConversationSettings. (Replaces the old
 * global localStorage `claude_auto_apply` switch, which made EVERY conversation
 * Manual once toggled and could never express a per-conv choice.) */
let autoApplyWrites = true;

// NOTE: the old client-side `pricingData` + `loadPricing()` pair was removed
// (2026-06) — it fetched /api/v1/pricing into a write-only variable and called
// a `_updatePricingDisplay()` that no longer exists. Cost math is now
// server-authoritative (lib/cost.py + lib/pricing.py via calcCostCny), and the
// settings model-picker reads `_modelPricingCache` (from /api/server-config),
// not this. Nothing consumed `pricingData`.

/* ── Pricing tables (server-side authoritative) ───────────────────────
 * Cost-from-usage math now lives in lib/cost.py (port of the old
 * calcCostCny). The ONLY pricing data we still keep client-side is the
 * { model_id → {input, output} } map used by settings.js to render the
 * pricing column in the model picker — settings UI is display-only.
 *
 * The per-provider override map (`_providerPricingCache`) and the
 * per-tier Qwen / Gemini / MiniMax / Doubao tables that previously
 * lived here have all moved server-side (lib/pricing.py).  Removing
 * them dropped ~70 lines of duplicate state out of the bundle.
 */
let _modelPricingCache = null;  // populated from /api/server-config (settings.js display)
// ── Cost calculation (server-authoritative) ──
//
// Pricing policy lives in lib/cost.py + lib/pricing.py. Endpoints:
//   POST /api/v1/messages/cost        → single-usage cost
//   POST /api/v1/messages/cost/batch  → batch over a conv's messages
//
// Render paths call calcCostCny(usage, model, provider) synchronously.
// Behaviour:
//   - Cache hit  → return cached cost dict immediately.
//   - Cache miss → kick off async fetch, return null. Next render
//                  picks up the cached entry on the next tick.
//   - Trivial 0  → return null (matches old behaviour).
//
// The conversation-cost rollup uses _prefetchConvCosts(conv) to fill
// the cache in ONE batch round-trip before render, so per-message
// calcCostCny() always hits the cache.

const _costCache = new Map();          // fp → cost dict (or null for "no charge")
const _costPending = new Map();        // fp → Promise (in-flight dedup)
const _COST_CACHE_MAX = 512;

function _costFingerprint(usage, modelId, providerId) {
  if (!usage) return '';
  // Order-stable, compact key. Token counts uniquely identify the math.
  return [
    modelId || '',
    providerId || '',
    usage.prompt_tokens || usage.input_tokens || 0,
    usage.completion_tokens || usage.output_tokens || 0,
    usage.cache_write_tokens || usage.cache_creation_input_tokens || 0,
    usage.cache_read_tokens || usage.cache_read_input_tokens || 0,
    usage.reasoning_tokens || usage.thinking_tokens || 0,
  ].join('|');
}

function _resolveModelId(modelOrPreset) {
  let modelId = modelOrPreset || '';
  if (_LEGACY_PRESET_TO_MODEL[modelId]) modelId = _LEGACY_PRESET_TO_MODEL[modelId];
  return modelId;
}

function _capCostCache() {
  if (_costCache.size > _COST_CACHE_MAX) {
    // Drop the oldest ~128 entries (Map preserves insertion order).
    let toDrop = 128;
    for (const k of _costCache.keys()) {
      _costCache.delete(k);
      if (--toDrop <= 0) break;
    }
  }
}

/**
 * Synchronous cost lookup. Returns the cached dict, or null when:
 *   - usage is empty / all zeros (no charge — matches JS old behaviour);
 *   - the value isn't in the cache yet (a fetch is kicked off).
 *
 * Render paths can call this on every redraw without flooding the network;
 * once the fetch resolves, the next render gets the value.
 */
function calcCostCny(usage, modelOrPreset, providerId) {
  if (!usage) return null;
  // Trivial-zero short-circuit (avoid even a fetch round-trip).
  const inp = usage.prompt_tokens || usage.input_tokens || 0;
  const out = usage.completion_tokens || usage.output_tokens || 0;
  const cw  = usage.cache_write_tokens || usage.cache_creation_input_tokens || 0;
  const cr  = usage.cache_read_tokens || usage.cache_read_input_tokens || 0;
  const thk = usage.reasoning_tokens || usage.thinking_tokens || 0;
  if (inp === 0 && out === 0 && cw === 0 && cr === 0 && thk === 0) return null;

  const modelId = _resolveModelId(modelOrPreset);
  const fp = _costFingerprint(usage, modelId, providerId);
  if (_costCache.has(fp)) return _costCache.get(fp);
  if (_costPending.has(fp)) return null;

  const promise = (async () => {
    try {
      // Api.conversations.cost uses onError:'null' → null on HTTP/network error,
      // matching the old !r.ok / catch paths.
      const body = await Api.conversations.cost(usage, modelId, providerId);
      if (!body) { _costCache.set(fp, null); return null; }
      const result = body.no_charge ? null : body;
      _costCache.set(fp, result);
      _capCostCache();
      return result;
    } catch (_) {
      _costCache.set(fp, null);
      return null;
    } finally {
      _costPending.delete(fp);
    }
  })();
  _costPending.set(fp, promise);
  return null;
}

/**
 * Batch-prefetch cost dicts for every message in a conversation that
 * has a usage record. Populates _costCache so subsequent calcCostCny()
 * calls hit the cache. Returns a Promise that resolves when the cache
 * is fresh.
 *
 * Called from renderConversationDetail(), the SSE 'done' handler, and
 * anywhere that triggers a cost-bar refresh — once per state change,
 * not per message.
 */
/**
 * Returns true when the cache had to be populated (fresh entries
 * landed); false when everything was already cached. Render paths use
 * the return value to decide whether a re-render is needed.
 */
async function _prefetchConvCosts(conv) {
  const turns = runtimeScope.ConversationTurnRead?.ordered?.(conv) || [];
  if (!conv || !turns.length) return false;
  const convModel = conv.model || conv.preset || conv.effort || serverModel;
  const items = [];
  const fps = [];
  const _seen = new Set();  // dedup fp across messages + rounds
  const _push = (usage, modelId, providerId) => {
    if (!usage) return;
    const fp = _costFingerprint(usage, modelId, providerId);
    if (!fp || _seen.has(fp) || _costCache.has(fp)) return;
    _seen.add(fp);
    items.push({ usage, model: modelId, provider_id: providerId });
    fps.push(fp);
  };
  for (const turn of turns) {
    const m = turn.projection || {};
    if (!m.usage) continue;
    const modelId = _resolveModelId(m.model || m.preset || m.effort || convModel);
    const providerId = m.provider_id || m.providerId || null;
    // Skip if the backend already stamped cost at sync time — no fetch needed.
    if (!m.cost) _push(m.usage, modelId, providerId);
    // Same for per-round entries.
    if (Array.isArray(m.apiRounds)) {
      for (const rd of m.apiRounds) {
        if (!rd || !rd.usage || rd.cost) continue;
        _push(rd.usage, modelId, rd.provider_id || rd.providerId || providerId);
      }
    }
  }
  if (!items.length) return false;
  try {
    const body = await Api.conversations.costBatch(items);
    if (!body) return false;
    const costs = Array.isArray(body.costs) ? body.costs : [];
    for (let i = 0; i < fps.length; i++) {
      _costCache.set(fps[i], costs[i] || null);
    }
    _capCostCache();
    return true;
  } catch (_) {
    return false;
  }
}

runtimeScope._prefetchConvCosts = _prefetchConvCosts;
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
  for (const turn of runtimeScope.ConversationTurnRead?.ordered?.(conv) || []) {
    const m = turn.projection || {};
    if (m.usage) {
      // Prefer cost from the authoritative settled-turn projection. The lazy
      // calculation covers a live turn whose projection has not arrived yet.
      const c = m.cost || calcCostCny(m.usage, m.model || m.preset || m.effort || convModel, m.provider_id || m.providerId);
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

/* ===== migrated source: core/cost.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/cost.js — extracted from core.js (split 2026-05-28)

   Per-message cost calculation: legacy preset migration and a bounded,
   server-authoritative calcCostCny batch fallback.

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
  if (!config.model) {
    /* Nothing stored anywhere: seed the pre-config placeholder as a PAINT
     * value only. Marking it provisional keeps whole-config persists (see
     * _configForPersist) from laundering a model the user never picked into
     * storage — the hardcoded placeholder may not exist in this deployment's
     * providers at all (aws.claude-opus-4.8 on a kimi-only deployment). */
    config.model = serverModel;
    config._modelIsProvisional = true;
  }
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
// Pricing policy lives in lib/cost.py + lib/pricing.py. The browser uses only:
//   POST /api/v1/messages/cost/batch  → one batch for synchronous render misses
//
// Render paths call calcCostCny(usage, model, provider) synchronously.
// Behaviour:
//   - Cache hit  → return cached cost dict immediately.
//   - Cache miss → join the bounded micro-batch, return null. One
//                  authoritative repaint picks up every landed entry.
//   - Trivial 0  → return null (matches old behaviour).
//
// A Surface render is synchronous, so every miss in that call stack joins the
// same microtask batch instead of creating one request per historical Turn.

const _costCache = new Map();          // fp → cost dict (or null for "no charge")
const _costPending = new Set();        // fp values owned by an in-flight batch
const _costBatchQueue = new Map();     // fp → one pending server item
const _COST_CACHE_MAX = 512;
const _COST_BATCH_MAX = 512;
let _costBatchScheduled = false;

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
  // Map preserves insertion order. Keep the declared ceiling exact even if a
  // whole render batch lands at once.
  while (_costCache.size > _COST_CACHE_MAX) {
    const oldest = _costCache.keys().next().value;
    if (oldest === undefined) break;
    _costCache.delete(oldest);
  }
}

async function _flushCostBatch() {
  _costBatchScheduled = false;
  const entries = Array.from(_costBatchQueue.entries());
  _costBatchQueue.clear();
  if (!entries.length) return;

  for (const [fp] of entries) _costPending.add(fp);
  let landed = false;
  try {
    const body = await Api.conversations.costBatch(
      entries.map(([, item]) => item),
    );
    const costs = body && Array.isArray(body.costs) ? body.costs : null;
    // An incomplete response is not authority for the omitted entries. Leave
    // every miss retryable on a later natural render instead of caching it as
    // a fabricated no-charge result.
    if (!costs || costs.length !== entries.length) return;
    for (let i = 0; i < entries.length; i++) {
      _costCache.set(entries[i][0], costs[i] || null);
    }
    _capCostCache();
    landed = true;
  } catch (_) {
    // Cost is display-only. A transport failure must not disturb the Turn;
    // the missing entry remains retryable on a later render.
  } finally {
    for (const [fp] of entries) _costPending.delete(fp);
  }

  if (!landed) return;
  const conversationId = typeof activeConvId !== 'undefined' ? activeConvId : null;
  if (conversationId
      && typeof runtimeScope.requestAuthoritativeConversationRender === 'function') {
    runtimeScope.requestAuthoritativeConversationRender(
      conversationId, { force: false, forceScroll: false },
    );
  }
}

function _queueCostLookup(fp, usage, modelId, providerId) {
  if (!fp || _costCache.has(fp) || _costPending.has(fp)
      || _costBatchQueue.has(fp)) return;
  // ConversationSurface renders at most 80 Turns. The higher fixed ceiling
  // also accommodates a lazily opened round breakdown while keeping both the
  // queue and one request bounded. Overflow stays retryable after repaint.
  if (_costBatchQueue.size >= _COST_BATCH_MAX) return;
  _costBatchQueue.set(fp, {
    usage,
    model: modelId,
    provider_id: providerId || null,
  });
  if (_costBatchScheduled) return;
  _costBatchScheduled = true;
  Promise.resolve().then(_flushCostBatch);
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
  _queueCostLookup(fp, usage, modelId, providerId);
  return null;
}
function formatCny(val) {
  if (val >= 1) return "¥" + val.toFixed(2);
  if (val >= 0.01) return "¥" + val.toFixed(3);
  return "¥" + val.toFixed(4);
}

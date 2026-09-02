/* ===== migrated source: core/model_group.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/model_group.js — THE model-grouping rule (SSOT)

   WHY THIS EXISTS
   ---------------
   The SAME model list was grouped by TWO different rules depending on where
   you looked, and that split was itself the defect:

     * toolbar model dropdown (main_toolbar_ui.js) grouped by provider_id /
       provider_name. When Claude moved to the Anthropic-native face
       (sankuai_anthropic, 2026-07-28) — same gateway, same keys, just a
       different wire protocol — the picker split into two "Meituan"
       sections. A pure backend implementation detail (which protocol the
       wire speaks) leaked straight into the user's model list.
     * Settings preset tab (visibility_defaults.js) grouped by provider
       BRAND. Both faces matched the 'meituan' rule → merged into one. So
       the settings list never split.

   Two lists of the SAME data must never disagree about grouping, and the
   user must never have to know that a provider speaks openai on one socket
   and anthropic on another. This module is the single answer to
   "which group does this model belong in, and what is the group called?".

   RULE
   ----
   group key  = provider.brand  (when the provider was explicitly branded)
             || _detectBrand(provider.name + ' ' + provider.base_url + ' '
                            + model.model_id)
   group label = _BRAND_NAMES[key] || provider.name || key

   The brand NAME table lives here and ONLY here — it was duplicated,
   verbatim, twice inside visibility_defaults.js.

   OAUTH SUBSCRIPTION PROVIDERS
   ----------------------------
   Subscription providers carry brand='oauth' or brand='adapter'. These are
   credential transports, not vendors — grouping under either would show a
   meaningless section. They fall THROUGH to _detectBrand on name/models, while the
   caller can still tell it apart from a gateway Claude provider via the
   provider id / oauth marker when it needs to. The point: the GROUP is about
   the model's vendor, never about the credential plumbing.

   Pure module: no DOM, no network, no window state read at load. Concatenated
   by Vite's module graph — window scope, no imports.
   ═══════════════════════════════════════════════════════════════════ */

(function() {
  /* THE brand-name table. Kept in sync with the brands _detectBrand can
   * return (settings/branding.js) plus the always-possible 'generic'. */
  var _BRAND_NAMES = {
    claude: 'Claude', openai: 'OpenAI', gemini: 'Gemini', qwen: 'Qwen',
    doubao: 'Doubao', minimax: 'MiniMax', deepseek: 'DeepSeek', grok: 'Grok',
    mistral: 'Mistral', glm: 'GLM', meituan: 'Meituan', kimi: 'Kimi',
    bedrock: 'Bedrock', openrouter: 'OpenRouter', tsinghua: 'Tsinghua',
    mimo: 'MiMo', hunyuan: 'Hunyuan', baiducloud: 'BaiduCloud',
    shubiaobiao: 'Shubiaobiao', local: 'Local', generic: 'Other',
  };

  /**
   * The group key for one (provider, model) entry.
   * @param {object} provider  ``{brand?, name?, base_url?, oauth?}``
   * @param {object} [model]   ``{model_id?}`` — deepens the detect hint.
   * @returns {string} a brand key.
   */
  function modelGroupKey(provider, model) {
    provider = provider || {};
    var brand = (provider.brand || '').trim();
    // OAuth/adapter are credential kinds, not vendors — resolve the vendor.
    if (brand && brand !== 'oauth' && brand !== 'adapter') return brand;
    var hint = (provider.name || '') + ' ' + (provider.base_url || '') + ' '
      + ((model && model.model_id) || '');
    return (typeof _detectBrand === 'function')
      ? /** @type {string} */ (_detectBrand(hint))
      : ((brand === 'oauth' || brand === 'adapter') ? 'generic'
                                                    : (brand || 'generic'));
  }

  /**
   * The human-facing label for a group key.
   * @param {string} key          from modelGroupKey().
   * @param {string} [fallback]   provider name when the key has no table entry.
   * @returns {string}
   */
  function modelGroupLabel(key, fallback) {
    return _BRAND_NAMES[key] || fallback || key;
  }

  /** The raw table (tests / debug read it; UI never hard-codes a copy). */
  function modelGroupBrandNames() {
    var out = {};
    for (var k in _BRAND_NAMES) out[k] = _BRAND_NAMES[k];
    return out;
  }

  runtimeScope.modelGroupKey = modelGroupKey;
  runtimeScope.modelGroupLabel = modelGroupLabel;
  runtimeScope.modelGroupBrandNames = modelGroupBrandNames;
  /* ALSO publish on the real global object. The concatenated sections'
   * cross-module contract is the GLOBAL scope ("window scope, no imports"
   * per every section header): consumers guard with bare
   * `typeof modelGroupKey === 'function'` and run a DEGRADED fallback when
   * the name is missing. In the ESM bundle `runtimeScope` is a
   * module-private Object.create(null) (app-runtime.js), so a
   * runtimeScope-only publish leaves those free-variable guards FALSE in
   * production while the classic-script jsdom harness (runtimeScope ===
   * window) sees them true — the divergence that grouped the Codex
   * subscription under its literal credential kind 'oauth' in the
   * Settings preset tab and rendered the generic smiley (2026-08-14).
   * Keep until every consumer reads runtimeScope/getRuntimeService. */
  if (typeof globalThis !== 'undefined') {
    globalThis.modelGroupKey = modelGroupKey;
    globalThis.modelGroupLabel = modelGroupLabel;
    globalThis.modelGroupBrandNames = modelGroupBrandNames;
  }
})();


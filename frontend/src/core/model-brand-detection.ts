/**
 * Model/provider brand detection.
 *
 * Responsibility: map a provider/model hint to one stable brand key, and own
 * the single brand-resolution interface every model surface shares.
 * Entry points: `detectModelBrand` (pattern fallback), `brandForCreator`
 * (explicit Creator id → glyph), `createModelBrandResolver` (the unified
 * interface). Dependencies: none. Ordering is policy: aggregators and
 * Bedrock must match before the model families they host, and meta (llama)
 * must precede nvidia so `llama-*-nemotron` composites retain their creator
 * family.
 */

/**
 * Creator id → brand glyph key. The v2 Creator identity is the authority;
 * this table only names which glyph represents that Creator, so a model
 * whose name carries no family token (`meta/esmfold`, `meta/muse-spark-1.2`)
 * still renders its Creator's mark. Keys normalize to lowercase alnum
 * (``Moonshot AI`` → ``moonshotai``). A Creator absent here falls through
 * to pattern detection — never to a wrong glyph. New backend creator
 * families (lib/model_catalog/_creator_families.py) need one row here.
 */
export const CREATOR_TO_BRAND: Readonly<Record<string, string>> = Object.freeze({
  openai: 'openai',
  anthropic: 'claude',
  google: 'gemini',
  deepseek: 'deepseek',
  moonshot: 'kimi',
  moonshotai: 'kimi',
  kimi: 'kimi',
  zhipu: 'glm',
  glm: 'glm',
  zai: 'glm',
  minimax: 'minimax',
  bytedance: 'doubao',
  doubao: 'doubao',
  meituan: 'meituan',
  longcat: 'meituan',
  alibaba: 'qwen',
  qwen: 'qwen',
  meta: 'meta',
  mistral: 'mistral',
  xai: 'grok',
  tencent: 'hunyuan',
  baidu: 'baiducloud',
  stepfun: 'stepfun',
  cohere: 'cohere',
  nvidia: 'nvidia',
  microsoft: 'microsoft',
  amazon: 'amazon',
  perplexity: 'perplexity',
  xiaomi: 'mimo',
  mimo: 'mimo',
  tsinghua: 'tsinghua',
  antgroup: 'antgroup',
  bailing: 'antgroup',
  sensetime: 'sensetime',
  iflytek: 'iflytek',
  '01ai': '01ai',
  lingyiwanwu: '01ai',
  ai21: 'ai21',
  upstage: 'upstage',
  thinkingmachines: 'thinkingmachines',
});

/** Resolve an explicit Creator id to its brand glyph key, '' when unmapped. */
export function brandForCreator(creatorId: unknown): string {
  if (typeof creatorId !== 'string') return '';
  const key = creatorId.toLowerCase().replace(/[^a-z0-9]/g, '');
  return CREATOR_TO_BRAND[key] || '';
}

const MODEL_BRAND_PATTERNS: ReadonlyArray<readonly [RegExp, string]> = [
  [/yeysai|thunlp|tsinghua|清华/i, 'tsinghua'],
  [/sankuai|longcat|meituan|三快|龙猫/i, 'meituan'],
  [/openrouter/i, 'openrouter'],
  [/mimo|xiaomi/i, 'mimo'],
  [/shubiaobiao|数标标/i, 'shubiaobiao'],
  [/qianfan|ernie|baidu|wenxin|文心/i, 'baiducloud'],
  [/hunyuan|^hy\d|tencent|腾讯|混元/i, 'hunyuan'],
  [
    /bedrock|bedrock-runtime|amazonaws\.com\/openai|^us\.anthropic\.|^us\.amazon\.|amazon\.titan|amazon\.nova|nova[-.]/i,
    'bedrock',
  ],
  [/claude|anthropic|opus|sonnet|haiku|fable/i, 'claude'],
  [/gpt|openai|o[134](-|$)|chatgpt|codex|dall|text-embedding-(3|ada)/i, 'openai'],
  [/gemini|gemma|palm|bard|lyria|imagen|veo/i, 'gemini'],
  [/qwen|tongyi|qwq|qvq|text-embedding-v/i, 'qwen'],
  [/doubao|seed.*pro|seed[-.]|byte/i, 'doubao'],
  [/minimax|abab|m2-her/i, 'minimax'],
  [/kimi|moonshot|月之暗面/i, 'kimi'],
  [/deepseek/i, 'deepseek'],
  [/grok|xai/i, 'grok'],
  [/mistral|mixtral|pixtral|codestral|devstral|magistral|ministral|voxtral/i, 'mistral'],
  [/glm|zhipu|z\.ai|chatglm|bigmodel/i, 'glm'],
  [/llama|meta[-./:]/i, 'meta'],
  [/nvidia|nemotron|^nv[-.]/i, 'nvidia'],
  [/cohere|c4ai|command[-.]|north[-./]/i, 'cohere'],
  [/phi[-.]|wizardlm|orca/i, 'microsoft'],
  [/stepfun|step[-.]/i, 'stepfun'],
  [/sonar([-./]|$)|perplexity/i, 'perplexity'],
  // Creator families iconed 2026-08-31. Amazon/nova stay claimed by the
  // bedrock relay rule above (relay-first policy), so no amazon row here.
  [/bailing|^ring[-.]|^ling[-.]|antgroup|ant group|inclusionai|蚂蚁/i, 'antgroup'],
  [/sensenova|sensechat|sensetime|商汤/i, 'sensetime'],
  [/^spark[-.]|sparkdesk|iflytek|xfyun|讯飞/i, 'iflytek'],
  [/^yi[-.]|01\.ai|01ai|lingyiwanwu|零一万物/i, '01ai'],
  [/jamba|ai21/i, 'ai21'],
  [/^solar[-.]|upstage/i, 'upstage'],
  [/thinkingmachines|thinking machines|^inkling/i, 'thinkingmachines'],
];

/** Detect a brand without reading catalog, DOM, or browser state. */
export function detectModelBrand(value: unknown): string {
  if (value == null || value === '') return 'generic';
  const text = String(value);
  for (const [pattern, brand] of MODEL_BRAND_PATTERNS) {
    if (pattern.test(text)) return brand;
  }
  return 'generic';
}

export interface ModelBrandResolverDependencies {
  /** Return the catalog Creator id for one model id, or '' when unknown. */
  lookupCreatorId(modelId: string): unknown;
}

export interface ModelBrandResolver {
  modelBrand(modelId: unknown, creatorHint?: unknown): string;
}

/**
 * The one brand-resolution interface for every model surface (preset picker,
 * composer toggle, finish route label, turn rail, image-gen, Settings cards).
 * Resolution order: an explicit Creator hint wins; the catalog lookup covers
 * surfaces holding only a model id; pattern matching stays as the fallback
 * for ids the catalog has never seen (custom endpoints, typed-in ids).
 */
export function createModelBrandResolver(
  dependencies: ModelBrandResolverDependencies,
): ModelBrandResolver {
  const modelBrand = (modelIdValue: unknown, creatorHint?: unknown): string => {
    const hinted = brandForCreator(creatorHint);
    if (hinted) return hinted;
    const modelId = modelIdValue == null ? '' : String(modelIdValue);
    const hintText = typeof creatorHint === 'string' ? creatorHint.trim() : '';
    if (!modelId) return hintText ? detectModelBrand(hintText) : 'generic';
    const lookedUp = dependencies.lookupCreatorId(modelId);
    const fromCatalog = brandForCreator(lookedUp);
    if (fromCatalog) return fromCatalog;
    const creatorText = typeof lookedUp === 'string' && lookedUp.trim()
      ? lookedUp.trim() : hintText;
    return detectModelBrand(creatorText ? `${creatorText} ${modelId}` : modelId);
  };
  return Object.freeze({ modelBrand });
}

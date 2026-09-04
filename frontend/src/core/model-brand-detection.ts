/**
 * Model/provider brand detection.
 *
 * Responsibility: map a provider/model hint to one stable brand key.
 * Entry point: `detectModelBrand`. Dependencies: none. Ordering is policy:
 * aggregators and Bedrock must match before the model families they host,
 * and meta (llama) must precede nvidia so `llama-*-nemotron` composites
 * retain their creator family.
 */

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

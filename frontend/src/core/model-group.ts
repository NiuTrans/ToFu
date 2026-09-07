/**
 * Browser model grouping policy.
 *
 * Responsibility: map provider/model catalog entries to one vendor group and
 * its display label. Entry point: `createModelGroupPolicy`.
 * Dependencies: one injected brand detector; no DOM, network, or browser
 * globals. Composition publishes the resulting immutable policy to retained
 * model pickers.
 */

export const MODEL_GROUP_BRAND_NAMES: Readonly<Record<string, string>> =
  Object.freeze({
    claude: 'Claude',
    openai: 'OpenAI',
    gemini: 'Gemini',
    qwen: 'Qwen',
    doubao: 'Doubao',
    minimax: 'MiniMax',
    deepseek: 'DeepSeek',
    grok: 'Grok',
    mistral: 'Mistral',
    glm: 'GLM',
    meituan: 'Meituan',
    kimi: 'Kimi',
    bedrock: 'Bedrock',
    openrouter: 'OpenRouter',
    tsinghua: 'Tsinghua',
    mimo: 'MiMo',
    hunyuan: 'Hunyuan',
    baiducloud: 'BaiduCloud',
    shubiaobiao: 'Shubiaobiao',
    cohere: 'Cohere',
    meta: 'Meta',
    nvidia: 'NVIDIA',
    microsoft: 'Microsoft',
    stepfun: 'StepFun',
    perplexity: 'Perplexity',
    local: 'Local',
    generic: 'Other',
  });

export interface ModelGroupPolicyDependencies {
  detectBrand(hint: string): string;
}

export interface ModelGroupPolicy {
  modelGroupKey(provider: unknown, model?: unknown): string;
  modelGroupLabel(key: unknown, fallback?: unknown): string;
  modelGroupBrandNames(): Record<string, string>;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object'
    ? value as Record<string, unknown>
    : {};
}

function stringField(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  return typeof value === 'string' ? value : '';
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** Create one immutable grouping policy around the application's detector. */
export function createModelGroupPolicy(
  dependencies: ModelGroupPolicyDependencies,
): ModelGroupPolicy {
  const modelGroupKey = (providerValue: unknown, modelValue?: unknown): string => {
    const provider = recordValue(providerValue);
    const model = recordValue(modelValue);
    const brand = stringField(provider, 'brand').trim();

    // OAuth and adapter identify credential transport, not model vendor.
    if (brand && brand !== 'oauth' && brand !== 'adapter') return brand;

    const hint = [
      stringField(provider, 'name'),
      stringField(provider, 'base_url'),
      stringField(model, 'model_id'),
    ].join(' ');
    const detectedBrand = dependencies.detectBrand(hint);
    return typeof detectedBrand === 'string' && detectedBrand.trim()
      ? detectedBrand.trim()
      : 'generic';
  };

  const modelGroupLabel = (keyValue: unknown, fallbackValue?: unknown): string => {
    const key = stringValue(keyValue);
    return MODEL_GROUP_BRAND_NAMES[key]
      || stringValue(fallbackValue)
      || key
      || MODEL_GROUP_BRAND_NAMES.generic;
  };

  const modelGroupBrandNames = (): Record<string, string> => ({
    ...MODEL_GROUP_BRAND_NAMES,
  });

  return Object.freeze({
    modelGroupKey,
    modelGroupLabel,
    modelGroupBrandNames,
  });
}

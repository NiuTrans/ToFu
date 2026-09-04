/**
 * Creator-family presentation for the model-routing v2 catalog.
 *
 * Explicit Creator identity wins. Pattern matching is only a presentation
 * fallback for historical/imported creator ids; it never merges Model rows.
 */

import { detectModelBrand } from '../../core/model-brand-detection';

export interface VendorIdentity {
  id: string;
  label: string;
  icon: string;
}

const CREATOR_LABELS: Readonly<Record<string, string>> = Object.freeze({
  openai: 'OpenAI', anthropic: 'Anthropic', google: 'Google', deepseek: 'DeepSeek',
  moonshot: 'Moonshot AI', kimi: 'Moonshot AI', zhipu: 'Zhipu AI', glm: 'Zhipu AI',
  minimax: 'MiniMax', bytedance: 'ByteDance', doubao: 'ByteDance', meituan: 'Meituan',
  xiaomi: 'Xiaomi', alibaba: 'Alibaba', qwen: 'Alibaba', meta: 'Meta',
  mistral: 'Mistral AI', xai: 'xAI', tencent: 'Tencent', baidu: 'Baidu',
  stepfun: 'StepFun', antgroup: 'Ant Group', sensetime: 'SenseTime',
  iflytek: 'iFlytek', '01ai': '01.AI', cohere: 'Cohere', ai21: 'AI21',
  nvidia: 'NVIDIA', microsoft: 'Microsoft', amazon: 'Amazon',
  perplexity: 'Perplexity', upstage: 'Upstage',
  thinkingmachines: 'Thinking Machines',
});

const BRAND_TO_VENDOR: Readonly<Record<string, string>> = Object.freeze({
  claude: 'anthropic', gemini: 'google', kimi: 'moonshot', glm: 'zhipu',
  doubao: 'bytedance', qwen: 'alibaba', grok: 'xai', baiducloud: 'baidu',
  hunyuan: 'tencent', mimo: 'xiaomi',
});

function normalizedId(value: unknown): string {
  return String(value ?? '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '');
}

/** Resolve one display family without altering the exact Model identity. */
export function detectVendor(
  creatorId: string,
  creatorName: string,
  modelId: string,
): VendorIdentity {
  const direct = normalizedId(creatorId);
  const creatorKey = Object.keys(CREATOR_LABELS).find(
    (key) => normalizedId(key) === direct,
  );
  const brand = detectModelBrand(`${creatorId} ${creatorName} ${modelId}`);
  const vendorId = creatorKey ?? BRAND_TO_VENDOR[brand] ?? (direct || 'other');
  const label = creatorName.trim() || CREATOR_LABELS[vendorId] || creatorId || 'Other';
  return {
    id: vendorId,
    label: CREATOR_LABELS[vendorId] || label,
    icon: brand,
  };
}


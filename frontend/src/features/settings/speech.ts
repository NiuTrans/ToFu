import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';

type SttKind = 'openai' | 'groq' | 'omni' | 'custom';
type SttCapability = 'transcription' | 'audio_chat';

interface SttProviderMeta {
  cap: SttCapability;
  needsKey: boolean;
  defaultBase: string;
  defaultModel: string;
}

interface SttModel {
  model_id?: string;
  aliases?: string[];
  capabilities?: string[];
  key_access?: Record<string, { capabilities: string[] }>;
  rpm?: number;
  cost?: number;
}

interface SttProvider {
  id?: string;
  name?: string;
  _sttKind?: string;
  base_url?: string;
  api_keys?: string[];
  enabled?: boolean;
  brand?: string;
  models?: SttModel[];
  [key: string]: unknown;
}

interface AudioCapabilities {
  available?: boolean;
  models?: Array<{ model?: string }>;
}

type SpeechWindow = Window & {
  Api?: { audio?: { capabilities(): Promise<AudioCapabilities | null> } };
  t?: (key: string, values?: Record<string, unknown>) => string;
  STT_PROVIDER_ID?: string;
  _STT_PROVIDER_META?: Record<SttKind, SttProviderMeta>;
  _findSttProvider?: () => SttProvider | null;
  _populateSpeechTab?: (config?: unknown) => void;
  _sttSuffix?: (kind: string) => string;
  _switchSttProvider?: (kind: string) => void;
  _refreshSttStatus?: () => void;
  _collectSttProvider?: () => SttProvider | null;
  _applySttToProviders?: () => void;
  _destroySpeechTab?: () => void;
  _stgProviders?: SttProvider[];
};

export const STT_PROVIDER_ID = 'stt';
export const STT_PROVIDER_META: Record<SttKind, SttProviderMeta> = {
  openai: {
    cap: 'transcription', needsKey: true,
    defaultBase: 'https://api.openai.com/v1',
    defaultModel: 'gpt-4o-transcribe',
  },
  groq: {
    cap: 'transcription', needsKey: true,
    defaultBase: 'https://api.groq.com/openai/v1',
    defaultModel: 'whisper-large-v3-turbo',
  },
  omni: {
    cap: 'audio_chat', needsKey: false, defaultBase: '',
    defaultModel: 'gemini-3-flash-preview',
  },
  custom: {
    cap: 'transcription', needsKey: false, defaultBase: '',
    defaultModel: 'whisper-1',
  },
};

let panelScope: LifecycleScope | null = null;
let statusGeneration = 0;

function globals(): SpeechWindow {
  return featureRegistry as unknown as SpeechWindow;
}

function translate(key: I18nKey, fallback: string): string {
  try {
    const translated = globals().t?.(key);
    if (translated && translated !== key) return translated;
  } catch (error: unknown) {
    console.debug('[Speech] translation unavailable', error);
  }
  return fallback;
}

function providers(): SttProvider[] | null {
  const value = globals()._stgProviders;
  return Array.isArray(value) ? value : null;
}

function inputValue(id: string): string {
  const control = document.getElementById(id);
  if (control instanceof HTMLInputElement
      || control instanceof HTMLSelectElement) return control.value;
  return '';
}

function setValue(id: string, value: string): void {
  const control = document.getElementById(id);
  if (control instanceof HTMLInputElement
      || control instanceof HTMLSelectElement) control.value = value;
}

function isKnownKind(kind: string): kind is SttKind {
  return Object.prototype.hasOwnProperty.call(STT_PROVIDER_META, kind);
}

export function sttSuffix(kind: string): string {
  return kind.charAt(0).toUpperCase() + kind.slice(1);
}

export function findSttProvider(): SttProvider | null {
  return providers()?.find((provider) => provider?.id === STT_PROVIDER_ID) ?? null;
}

export function switchSttProvider(kind: string): void {
  for (const candidate of Object.keys(STT_PROVIDER_META) as SttKind[]) {
    const card = document.getElementById(`sttCard${sttSuffix(candidate)}`);
    if (card instanceof HTMLElement) {
      card.style.display = candidate === kind ? '' : 'none';
    }
  }
}

export function destroySpeechTab(): void {
  statusGeneration += 1;
  panelScope?.destroy();
  panelScope = null;
}

export async function refreshSttStatus(): Promise<void> {
  const generation = ++statusGeneration;
  const banner = document.getElementById('sttStatusBanner');
  const text = document.getElementById('sttStatusText');
  const api = globals().Api?.audio;
  if (!(banner instanceof HTMLElement)
      || !(text instanceof HTMLElement) || !api) return;
  try {
    const capabilities = await api.capabilities();
    if (generation !== statusGeneration
        || document.getElementById('sttStatusBanner') !== banner) return;
    banner.style.display = '';
    if (capabilities?.available) {
      const models = (capabilities.models ?? [])
        .map((model) => String(model.model ?? ''))
        .filter(Boolean)
        .join(', ');
      text.textContent = translate(
        'settings.sttStatusOn', '✓ 语音输入已就绪')
        + (models ? ` — ${models}` : '');
    } else {
      text.textContent = translate(
        'settings.sttStatusOff',
        '语音输入未就绪 — 保存有效凭证后麦克风按钮才会出现');
    }
  } catch (error: unknown) {
    if (generation !== statusGeneration) return;
    console.debug('[Speech] capability probe failed', error);
    banner.style.display = 'none';
  }
}

export function populateSpeechTab(_config?: unknown): void {
  panelScope?.destroy();
  const scope = createLifecycleScope();
  panelScope = scope;

  const provider = findSttProvider();
  const enabled = Boolean(provider?.enabled);
  const storedKind = String(provider?._sttKind ?? 'openai');
  const kind = isKnownKind(storedKind) ? storedKind : 'openai';
  const enabledControl = document.getElementById('settingSttEnabled');
  const fields = document.getElementById('sttProviderFields');
  if (enabledControl instanceof HTMLInputElement) {
    enabledControl.checked = enabled;
    scope.listen(enabledControl, 'change', () => {
      if (fields instanceof HTMLElement) {
        fields.style.display = enabledControl.checked ? '' : 'none';
      }
    });
  }
  if (fields instanceof HTMLElement) fields.style.display = enabled ? '' : 'none';

  const kindControl = document.getElementById('settingSttProvider');
  if (kindControl instanceof HTMLSelectElement) {
    kindControl.value = kind;
    scope.listen(kindControl, 'change', () => switchSttProvider(kindControl.value));
  }

  const model = provider?.models?.[0];
  if (provider && model) {
    const suffix = sttSuffix(kind);
    setValue(`settingSttModel${suffix}`, String(model.model_id ?? ''));
    setValue(`settingSttBase${suffix}`, String(provider.base_url ?? ''));
    setValue(`settingSttKey${suffix}`, String(provider.api_keys?.[0] ?? ''));
  }
  switchSttProvider(kind);
  void refreshSttStatus();
}

export function collectSttProvider(): SttProvider | null {
  const enabled = document.getElementById('settingSttEnabled');
  if (!(enabled instanceof HTMLInputElement) || !enabled.checked) return null;

  const requestedKind = inputValue('settingSttProvider') || 'openai';
  const kind: SttKind = isKnownKind(requestedKind) ? requestedKind : 'openai';
  const meta = STT_PROVIDER_META[kind];
  const suffix = sttSuffix(kind);
  const model = inputValue(`settingSttModel${suffix}`).trim()
    || meta.defaultModel;
  const base = inputValue(`settingSttBase${suffix}`).trim()
    || meta.defaultBase;
  const key = inputValue(`settingSttKey${suffix}`).trim();
  if (!model || !base || (meta.needsKey && !key)) return null;

  const apiKeys = key ? [key] : [];
  const keyAccess: Record<string, { capabilities: SttCapability[] }> = {};
  for (let index = 0; index < (apiKeys.length || 1); index += 1) {
    keyAccess[String(index)] = { capabilities: [meta.cap] };
  }
  return {
    id: STT_PROVIDER_ID,
    name: translate('settings.sttService', '语音识别'),
    _sttKind: kind,
    base_url: base,
    api_keys: apiKeys,
    enabled: true,
    brand: apiKeys.length === 0 ? 'local' : '',
    models: [{
      model_id: model,
      aliases: [],
      capabilities: [meta.cap],
      key_access: keyAccess,
      rpm: 60,
      cost: 0.001,
    }],
  };
}

export function applySttToProviders(): void {
  const current = providers();
  if (!current) return;
  for (let index = current.length - 1; index >= 0; index -= 1) {
    if (current[index]?.id === STT_PROVIDER_ID) current.splice(index, 1);
  }
  const provider = collectSttProvider();
  if (provider) current.push(provider);
}

const bridge = globals();
bridge.STT_PROVIDER_ID = STT_PROVIDER_ID;
bridge._STT_PROVIDER_META = STT_PROVIDER_META;
bridge._findSttProvider = findSttProvider;
bridge._populateSpeechTab = populateSpeechTab;
bridge._sttSuffix = sttSuffix;
bridge._switchSttProvider = switchSttProvider;
bridge._refreshSttStatus = () => { void refreshSttStatus(); };
bridge._collectSttProvider = collectSttProvider;
bridge._applySttToProviders = applySttToProviders;
bridge._destroySpeechTab = destroySpeechTab;

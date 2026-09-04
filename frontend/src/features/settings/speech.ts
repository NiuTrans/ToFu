/*
 * Speech settings own one ProviderAccess bundle in model-routing v2.
 * Plaintext credentials exist only in the form until the provider bundle
 * operation stores them through the encrypted repository boundary.
 */

import {
  featureRegistry,
  readLiveRuntimeBinding,
  writeLiveRuntimeBinding,
} from '../../feature-registry';
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

interface RoutingProvider {
  provider_id: string;
  name: string;
  scope: 'public' | 'owner';
  brand?: string;
}

interface RoutingAccess {
  provider_access_id: string;
  provider_id: string;
  enabled: boolean;
  quota_policy: Record<string, unknown>;
}

interface RoutingConnection {
  connection_id: string;
  provider_access_id: string;
  base_url: string;
  protocol: string;
  enabled: boolean;
  priority: number;
  extra_headers: Record<string, string>;
}

interface RoutingCredential {
  credential_id: string;
  provider_access_id: string;
  kind: 'api_key' | 'local_identity';
  secret_reference: string;
  key_hint: string;
  enabled: boolean;
  authorization: {
    connection_ids: string[];
    models: Array<{ creator_id: string; model_id: string }>;
  };
  quota_policy: Record<string, unknown>;
}

interface RoutingOffering {
  offering_id: string;
  provider_access_id: string;
  identity_state: 'confirmed';
  model: { creator_id: string; model_id: string };
  enabled: boolean;
  stale: boolean;
  capabilities: string[];
  context_window: number;
  priority: number;
}

interface RoutingDocument {
  contract_version: string;
  creators: Array<{ creator_id: string; name: string }>;
  models: Array<{
    creator_id: string;
    model_id: string;
    display_name: string;
    capabilities: string[];
    context_window: number;
    quality_rank: number;
  }>;
  providers: RoutingProvider[];
  provider_accesses: RoutingAccess[];
  connections: RoutingConnection[];
  credentials: RoutingCredential[];
  offerings: RoutingOffering[];
}

interface ProviderBundle {
  provider: RoutingProvider;
  provider_access: RoutingAccess;
  connections: RoutingConnection[];
  credentials: RoutingCredential[];
  credential_secrets: Record<string, string>;
  offerings: RoutingOffering[];
  deployments: Array<Record<string, unknown>>;
  creators: Array<{ creator_id: string; name: string }>;
  models: RoutingDocument['models'];
}

interface AudioCapabilities {
  available?: boolean;
  models?: Array<{ model?: string }>;
}

interface ModelRoutingApi {
  get(): Promise<{ model_routing?: RoutingDocument; revision?: number } | null>;
  createProvider(
    bundle: ProviderBundle,
    expectedRevision: number,
  ): Promise<unknown>;
  saveProvider(
    providerId: string,
    bundle: ProviderBundle,
    expectedRevision: number,
  ): Promise<unknown>;
  deleteProvider(providerId: string, expectedRevision: number): Promise<unknown>;
}

interface SttProjection {
  provider: RoutingProvider;
  access: RoutingAccess;
  connection: RoutingConnection | null;
  credential: RoutingCredential | null;
  offering: RoutingOffering | null;
}

type SpeechWindow = Window & {
  Api?: {
    audio?: { capabilities(): Promise<AudioCapabilities | null> };
    modelRouting?: ModelRoutingApi;
  };
  t?: (key: string, values?: Record<string, unknown>) => string;
  STT_PROVIDER_ID?: string;
  _STT_PROVIDER_META?: Record<SttKind, SttProviderMeta>;
  _findSttProvider?: () => SttProjection | null;
  _populateSpeechTab?: (config?: unknown) => void;
  _sttSuffix?: (kind: string) => string;
  _switchSttProvider?: (kind: string) => void;
  _refreshSttStatus?: () => void;
  _persistSttProvider?: () => Promise<void>;
  _destroySpeechTab?: () => void;
  _stgModelRouting?: RoutingDocument | null;
  _stgModelRoutingRevision?: number;
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

const ACCESS_ID = 'stt-access';
const CONNECTION_ID = 'stt-connection';
const CREDENTIAL_ID = 'stt-credential';
const OFFERING_ID = 'stt-offering';
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

function routingDocument(): RoutingDocument | null {
  const value = readLiveRuntimeBinding('_stgModelRouting') as
    RoutingDocument | null | undefined;
  return value?.contract_version === 'tofu.model-routing/v2' ? value : null;
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

function kindForProjection(projection: SttProjection): SttKind {
  const branded = String(projection.provider.brand || '').replace(/^stt-/, '');
  if (isKnownKind(branded)) return branded;
  if (projection.offering?.capabilities.includes('audio_chat')) return 'omni';
  const baseUrl = projection.connection?.base_url || '';
  if (baseUrl.includes('groq.com')) return 'groq';
  if (baseUrl.includes('api.openai.com')) return 'openai';
  return 'custom';
}

export function sttSuffix(kind: string): string {
  return kind.charAt(0).toUpperCase() + kind.slice(1);
}

export function findSttProvider(): SttProjection | null {
  const documentValue = routingDocument();
  if (!documentValue) return null;
  const provider = documentValue.providers.find(
    (row) => row.provider_id === STT_PROVIDER_ID);
  if (!provider) return null;
  const access = documentValue.provider_accesses.find(
    (row) => row.provider_id === STT_PROVIDER_ID);
  if (!access) return null;
  return {
    provider,
    access,
    connection: documentValue.connections.find(
      (row) => row.provider_access_id === access.provider_access_id) || null,
    credential: documentValue.credentials.find(
      (row) => row.provider_access_id === access.provider_access_id) || null,
    offering: documentValue.offerings.find(
      (row) => row.provider_access_id === access.provider_access_id) || null,
  };
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

  const projection = findSttProvider();
  const enabled = Boolean(projection?.access.enabled);
  const kind = projection ? kindForProjection(projection) : 'openai';
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

  if (projection?.offering && projection.connection) {
    const suffix = sttSuffix(kind);
    setValue(`settingSttModel${suffix}`, projection.offering.model.model_id);
    setValue(`settingSttBase${suffix}`, projection.connection.base_url);
    setValue(`settingSttKey${suffix}`, '');
  }
  switchSttProvider(kind);
  void refreshSttStatus();
}

export function buildSttProviderBundle(): ProviderBundle | null {
  const enabled = document.getElementById('settingSttEnabled');
  if (!(enabled instanceof HTMLInputElement) || !enabled.checked) return null;
  const documentValue = routingDocument();
  if (!documentValue) throw new Error('model-routing v2 authority is not loaded');

  const requestedKind = inputValue('settingSttProvider') || 'openai';
  const kind: SttKind = isKnownKind(requestedKind) ? requestedKind : 'openai';
  const meta = STT_PROVIDER_META[kind];
  const suffix = sttSuffix(kind);
  const modelId = inputValue(`settingSttModel${suffix}`).trim()
    || meta.defaultModel;
  const baseUrl = inputValue(`settingSttBase${suffix}`).trim()
    || meta.defaultBase;
  const plaintext = inputValue(`settingSttKey${suffix}`).trim();
  const existing = findSttProvider();
  const preservedCredential = existing?.credential;
  if (!modelId || !baseUrl
      || (meta.needsKey && !plaintext && !preservedCredential?.secret_reference)) {
    return null;
  }

  const creatorId = meta.cap === 'audio_chat'
    ? 'tofu-user-stt-audio' : 'tofu-user-stt-transcription';
  const modelRef = { creator_id: creatorId, model_id: modelId };
  const creatorExists = documentValue.creators.some(
    (row) => row.creator_id === creatorId);
  const modelExists = documentValue.models.some(
    (row) => row.creator_id === creatorId && row.model_id === modelId);
  const useStoredSecret = !plaintext
    && preservedCredential?.kind === 'api_key'
    && Boolean(preservedCredential.secret_reference);
  const credentialKind = plaintext || useStoredSecret ? 'api_key' : 'local_identity';

  return {
    provider: {
      provider_id: STT_PROVIDER_ID,
      name: translate('settings.sttService', '语音识别'),
      scope: 'owner',
      brand: `stt-${kind}`,
    },
    provider_access: {
      provider_access_id: ACCESS_ID,
      provider_id: STT_PROVIDER_ID,
      enabled: true,
      quota_policy: { rpm: 60 },
    },
    connections: [{
      connection_id: CONNECTION_ID,
      provider_access_id: ACCESS_ID,
      base_url: baseUrl,
      protocol: 'openai',
      enabled: true,
      priority: 0,
      extra_headers: {},
    }],
    credentials: [{
      credential_id: CREDENTIAL_ID,
      provider_access_id: ACCESS_ID,
      kind: credentialKind,
      secret_reference: useStoredSecret
        ? preservedCredential?.secret_reference || '' : '',
      key_hint: useStoredSecret ? preservedCredential?.key_hint || '' : '',
      enabled: true,
      authorization: {
        connection_ids: [CONNECTION_ID],
        models: [modelRef],
      },
      quota_policy: {},
    }],
    credential_secrets: plaintext ? { [CREDENTIAL_ID]: plaintext } : {},
    offerings: [{
      offering_id: OFFERING_ID,
      provider_access_id: ACCESS_ID,
      identity_state: 'confirmed',
      model: modelRef,
      enabled: true,
      stale: false,
      capabilities: [meta.cap],
      context_window: 1_000_000,
      priority: 0,
    }],
    deployments: [{
      deployment_id: 'stt-deployment',
      offering_id: OFFERING_ID,
      connection_id: CONNECTION_ID,
      wire_model_id: modelId,
      enabled: true,
      identity_confidence: 'high',
      probe_status: 'passed',
      priority: 0,
    }],
    creators: creatorExists ? [] : [{ creator_id: creatorId, name: 'User STT models' }],
    models: modelExists ? [] : [{
      ...modelRef,
      display_name: modelId,
      capabilities: [meta.cap],
      context_window: 1_000_000,
      quality_rank: 0,
    }],
  };
}

async function reloadRoutingAuthority(api: ModelRoutingApi): Promise<void> {
  const authority = await api.get();
  if (!authority?.model_routing) {
    throw new Error('model-routing v2 authority reload failed');
  }
  writeLiveRuntimeBinding('_stgModelRouting', authority.model_routing);
  writeLiveRuntimeBinding(
    '_stgModelRoutingRevision', Number(authority.revision || 0));
}

export async function persistSttProvider(): Promise<void> {
  const api = globals().Api?.modelRouting;
  const documentValue = routingDocument();
  if (!api || !documentValue) {
    throw new Error('model-routing v2 authority is not ready');
  }
  const revision = Number(
    readLiveRuntimeBinding('_stgModelRoutingRevision') || 0);
  const existing = findSttProvider();
  const enabled = document.getElementById('settingSttEnabled');
  if (!(enabled instanceof HTMLInputElement) || !enabled.checked) {
    if (existing) {
      await api.deleteProvider(STT_PROVIDER_ID, revision);
      await reloadRoutingAuthority(api);
    }
    return;
  }
  const bundle = buildSttProviderBundle();
  if (!bundle) throw new Error('speech provider configuration is incomplete');
  if (existing) {
    await api.saveProvider(STT_PROVIDER_ID, bundle, revision);
  } else {
    await api.createProvider(bundle, revision);
  }
  await reloadRoutingAuthority(api);
}

const bridge = globals();
bridge.STT_PROVIDER_ID = STT_PROVIDER_ID;
bridge._STT_PROVIDER_META = STT_PROVIDER_META;
bridge._findSttProvider = findSttProvider;
bridge._populateSpeechTab = populateSpeechTab;
bridge._sttSuffix = sttSuffix;
bridge._switchSttProvider = switchSttProvider;
bridge._refreshSttStatus = () => { void refreshSttStatus(); };
bridge._persistSttProvider = persistSttProvider;
bridge._destroySpeechTab = destroySpeechTab;

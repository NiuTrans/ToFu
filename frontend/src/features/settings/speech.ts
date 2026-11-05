/*
 * Speech settings own one ProviderAccess bundle in model-routing v2.
 * Plaintext credentials exist only in the form until the provider bundle
 * operation stores them through the encrypted repository boundary.
 * This adapter owns form intent and the form/status lifecycle. Pure bundle
 * and activation policy lives in speech-provider; save commands join the
 * existing model-routing-authority queue and complete-response adopter.
 */

import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';
import { createLifecycleScope, type LifecycleScope } from '../../lifecycle';
import {audioCapabilities, type AudioCapabilities} from '../voice/audio-capabilities';
import {runModelRoutingMutation, type ModelRoutingMutationResult} from './model-routing-authority';
import type {ModelRoutingDocument, ModelRoutingAuthorityResponse, ProviderResponse} from '../../api/api-v1-model-routing.generated';
import {
  STT_PROVIDER_ID, STT_PROVIDER_META, isKnownKind, kindForProjection,
  findSttProvider as projectSttProvider, hasSttChanges as sttFormChanged,
  buildSttProviderBundle as prepareSttBundle, captureSttActivation, buildSttActivationDocument,
  type SttKind, type SttProviderMeta, type SttProjection, type SttForm, type ProviderBundle,
} from './speech-provider';

export {STT_PROVIDER_ID, STT_PROVIDER_META} from './speech-provider';
type RoutingDocument = ModelRoutingDocument;

interface ModelRoutingApi {
  createProvider(
    bundle: ProviderBundle,
    expectedRevision: number,
  ): Promise<ProviderResponse | null>;
  saveProvider(
    providerId: string,
    bundle: ProviderBundle,
    expectedRevision: number,
  ): Promise<ProviderResponse | null>;
  replace(document: RoutingDocument, expectedRevision: number): Promise<ModelRoutingAuthorityResponse | null>;
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
  _beginSpeechTabLoad?: typeof beginSpeechTabLoad;
  _sttSuffix?: (kind: string) => string;
  _switchSttProvider?: (kind: string) => void;
  _refreshSttStatus?: () => void;
  _captureSttSaveIntent?: typeof captureSttSaveIntent;
  _persistSttProvider?: (intent?: SttSaveIntent) => Promise<ModelRoutingMutationResult>;
  _hasSttChanges?: () => boolean;
  _destroySpeechTab?: () => void;
  _stgModelRouting?: RoutingDocument | null;
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

function routingDocument(): RoutingDocument | null {
  // The Settings lazy bundle owns this state and exposes it as live
  // accessors on featureRegistry; readLiveRuntimeBinding would read the
  // app-runtime scope, which never holds it.
  const value = globals()._stgModelRouting;
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
  if (control instanceof HTMLSelectElement && value
      && !Array.from(control.options).some(option => option.value === value)) {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    control.append(option);
    panelScope?.add(() => option.remove());
  }
  if (control instanceof HTMLInputElement
      || control instanceof HTMLSelectElement) control.value = value;
}

export function sttSuffix(kind: string): string {
  return kind.charAt(0).toUpperCase() + kind.slice(1);
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

function projectSttStatus(capabilities: AudioCapabilities | null): void {
  const banner = document.getElementById('sttStatusBanner');
  const text = document.getElementById('sttStatusText');
  if (!(banner instanceof HTMLElement) || !(text instanceof HTMLElement)) return;
  banner.style.display = capabilities ? '' : 'none';
  if (capabilities?.available) {
    const models = capabilities.models.map(model => String(model?.model ?? '')).filter(Boolean).join(', ');
    text.textContent = translate('settings.sttStatusOn', '✓ 语音输入已就绪') + (models ? ` — ${models}` : '');
  } else {
    text.textContent = translate('settings.sttStatusOff',
      '语音输入未就绪 — 保存有效凭证后麦克风按钮才会出现');
  }
}

export async function refreshSttStatus(): Promise<void> {
  const generation = ++statusGeneration;
  const banner = document.getElementById('sttStatusBanner');
  const api = globals().Api?.audio;
  if (!(banner instanceof HTMLElement) || !api) return;
  try {
    const capabilities = await audioCapabilities.refresh();
    if (generation !== statusGeneration
        || document.getElementById('sttStatusBanner') !== banner) return;
    projectSttStatus(capabilities);
  } catch (error: unknown) {
    if (generation !== statusGeneration) return;
    console.debug('[Speech] capability probe failed', error);
    banner.style.display = 'none';
  }
}

function startSpeechPanelScope(): LifecycleScope {
  destroySpeechTab();
  const scope = createLifecycleScope();
  panelScope = scope;
  scope.add(audioCapabilities.subscribe(projectSttStatus));
  for (const kind of Object.keys(STT_PROVIDER_META)) {
    const key = document.getElementById(`settingSttKey${sttSuffix(kind)}`);
    if (key instanceof HTMLInputElement) scope.add(() => { key.value = ''; });
  }
  return scope;
}

/** Controls become interactive only after this opening's authority/config
 * load completes. Closing/reopening invalidates its completion callbacks.
 */
export function beginSpeechTabLoad(): {complete(config?: unknown): void; fail(): void} {
  const scope = startSpeechPanelScope();
  const root = document.getElementById('settingsTab_speech');
  root?.setAttribute('aria-busy', 'true');
  scope.add(() => root?.removeAttribute('aria-busy'));
  for (const control of root?.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLButtonElement>(
    'input, select, button') || []) {
    const disabled = control.disabled;
    control.disabled = true;
    scope.add(() => { control.disabled = disabled; });
  }
  return {
    complete(config) { if (panelScope === scope) populateSpeechTab(config); },
    fail() { if (panelScope === scope) root?.removeAttribute('aria-busy'); },
  };
}

export function populateSpeechTab(_config?: unknown): void {
  const scope = startSpeechPanelScope();

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
    setValue(`settingSttModel${suffix}`, projection.offering.model?.model_id || '');
    setValue(`settingSttBase${suffix}`, projection.connection.base_url);
    setValue(`settingSttKey${suffix}`, '');
  }
  switchSttProvider(kind);
  void refreshSttStatus();
}

function readSttForm(): SttForm | null {
  const enabled = document.getElementById('settingSttEnabled');
  // Disabled controls have no submitted intent while initial loading is
  // pending or failed; their prior/default values cannot authorize deletion.
  if (!(enabled instanceof HTMLInputElement) || enabled.disabled) return null;
  const requestedKind = inputValue('settingSttProvider') || 'openai';
  const kind: SttKind = isKnownKind(requestedKind) ? requestedKind : 'openai';
  const meta = STT_PROVIDER_META[kind];
  const suffix = sttSuffix(kind);
  return {enabled: enabled.checked, kind,
    modelId: inputValue(`settingSttModel${suffix}`).trim() || meta.defaultModel,
    baseUrl: inputValue(`settingSttBase${suffix}`).trim() || meta.defaultBase,
    secret: inputValue(`settingSttKey${suffix}`)};
}

export function findSttProvider(): SttProjection | null {
  return projectSttProvider(routingDocument());
}

export function hasSttChanges(form = readSttForm()): boolean {
  return sttFormChanged(routingDocument(), form);
}

export function buildSttProviderBundle(form = readSttForm()): ProviderBundle | null {
  return prepareSttBundle(routingDocument(), form, translate('settings.sttService', '语音识别'));
}

function sttCredentialIds(): string[] {
  const documentValue = routingDocument();
  const accesses = new Set(documentValue?.provider_accesses
    .filter(row => row.provider_id === STT_PROVIDER_ID).map(row => row.provider_access_id));
  return (documentValue?.credentials || []).filter(row => accesses.has(row.provider_access_id))
    .map(row => row.credential_id);
}

export interface SttSaveIntent {
  readonly form: SttForm | null;
  readonly scope: LifecycleScope | null;
  readonly activation: ReturnType<typeof captureSttActivation>;
}

/** Global Save captures Speech before any preceding feature write awaits. */
export function captureSttSaveIntent(): SttSaveIntent {
  const form = readSttForm();
  const initialDocument = routingDocument();
  return {form, scope: panelScope,
    activation: initialDocument && form ? captureSttActivation(initialDocument, form) : null};
}

export async function persistSttProvider(intent = captureSttSaveIntent()): Promise<ModelRoutingMutationResult> {
  const initialDocument = routingDocument();
  if (!initialDocument) throw new Error('model-routing v2 authority is not ready');
  const {form, scope, activation} = intent;
  if (!form) return {refreshed: true};
  const api = globals().Api?.modelRouting;
  if (!api) throw new Error('model-routing v2 authority is not ready');
  return runModelRoutingMutation(async revision => {
    const current = routingDocument();
    if (!current) throw new Error('model-routing v2 authority is not ready');
    if (activation) {
      const candidate = buildSttActivationDocument(current, activation);
      if (!candidate) return {unchanged: true};
      const response = await api.replace(candidate, revision);
      if (!response?.model_routing || response.model_routing.contract_version !== 'tofu.model-routing/v2'
          || !Number.isSafeInteger(response.revision) || response.revision <= revision) {
        throw new Error('speech activation returned an invalid authority response');
      }
      return {revision: response.revision, authority: response};
    }
    if (!hasSttChanges(form)) return {unchanged: true};
    const existing = findSttProvider();
    const credentialIds = sttCredentialIds();
    const bundle = buildSttProviderBundle(form);
    if (!bundle) throw new Error('speech provider configuration is incomplete');
    const receipt = existing ? await api.saveProvider(STT_PROVIDER_ID, bundle, revision)
      : await api.createProvider(bundle, revision);
    if (!receipt || receipt.provider?.provider?.provider_id !== STT_PROVIDER_ID ||
        !Number.isSafeInteger(receipt.revision) || receipt.revision <= revision) {
      throw new Error('speech save returned an invalid receipt');
    }
    const keyId = `settingSttKey${sttSuffix(form.kind)}`;
    if (panelScope === scope && inputValue(keyId) === form.secret) setValue(keyId, '');
    return {revision: receipt.revision,
      removedCredentialIds: credentialIds.filter(id => !bundle.credentials.some(row => row.credential_id === id))};
  }, {persistDraft: true});
}

const bridge = globals();
bridge.STT_PROVIDER_ID = STT_PROVIDER_ID;
bridge._STT_PROVIDER_META = STT_PROVIDER_META;
bridge._findSttProvider = findSttProvider;
bridge._populateSpeechTab = populateSpeechTab;
bridge._beginSpeechTabLoad = beginSpeechTabLoad;
bridge._sttSuffix = sttSuffix;
bridge._switchSttProvider = switchSttProvider;
bridge._refreshSttStatus = () => { void refreshSttStatus(); };
bridge._captureSttSaveIntent = captureSttSaveIntent;
bridge._persistSttProvider = persistSttProvider;
bridge._hasSttChanges = hasSttChanges;
bridge._destroySpeechTab = destroySpeechTab;

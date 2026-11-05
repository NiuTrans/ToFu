/** Pure speech Provider projection and bundle policy over model-routing v2.
 * Entry points: findSttProvider, hasSttChanges, buildSttProviderBundle and
 * buildSttActivationDocument. No DOM, registry, transport or persistence.
 * Generated routing types define the graph; speech.ts owns form lifetime and
 * submits detached commands through model-routing-authority.ts.
 */

import type {
  ModelRoutingConnection,
  ModelRoutingCredentialMetadata,
  ModelRoutingDocument,
  ModelRoutingModelRef,
  ModelRoutingOffering,
  ModelRoutingProvider,
  ModelRoutingProviderAccess,
  ProviderBundle as ContractProviderBundle,
} from '../../api/api-v1-model-routing.generated';

export type SttKind = 'openai' | 'groq' | 'omni' | 'custom';
type SttCapability = 'transcription' | 'audio_chat';

export interface SttProviderMeta {
  cap: SttCapability;
  needsKey: boolean;
  defaultBase: string;
  defaultModel: string;
}

type RoutingProvider = ModelRoutingProvider;
type RoutingAccess = ModelRoutingProviderAccess;
type RoutingConnection = ModelRoutingConnection;
type RoutingCredential = ModelRoutingCredentialMetadata;
type RoutingDocument = ModelRoutingDocument;

interface RoutingOffering extends ModelRoutingOffering {
  identity_state: 'confirmed';
  model: ModelRoutingModelRef;
  stale: boolean;
}

export interface ProviderBundle extends ContractProviderBundle {
  provider_access: RoutingAccess;
  credential_secrets: Record<string, string>;
  offerings: RoutingOffering[];
  creators: Array<{ creator_id: string; name: string }>;
  models: RoutingDocument['models'];
}

export interface SttProjection {
  provider: RoutingProvider;
  access: RoutingAccess;
  connection: RoutingConnection | null;
  credential: RoutingCredential | null;
  offering: ModelRoutingOffering | null;
}

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

export function isKnownKind(kind: string): kind is SttKind {
  return Object.prototype.hasOwnProperty.call(STT_PROVIDER_META, kind);
}

export function kindForProjection(projection: SttProjection): SttKind {
  const branded = String(projection.provider.brand || '').replace(/^stt-/, '');
  if (isKnownKind(branded)) return branded;
  if (projection.offering?.capabilities.includes('audio_chat')) return 'omni';
  const baseUrl = projection.connection?.base_url || '';
  if (baseUrl.includes('groq.com')) return 'groq';
  if (baseUrl.includes('api.openai.com')) return 'openai';
  return 'custom';
}

export function findSttProvider(documentValue: RoutingDocument | null): SttProjection | null {
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

export interface SttForm {
  enabled: boolean;
  kind: SttKind;
  modelId: string;
  baseUrl: string;
  secret: string;
}

export function hasSttChanges(documentValue: RoutingDocument | null, form: SttForm | null): boolean {
  if (!form) return false;
  const existing = findSttProvider(documentValue);
  if (!form.enabled) return !!existing?.access.enabled;
  return !existing || !existing.access.enabled || sttConfigurationChanged(existing, form);
}

function sttConfigurationChanged(existing: SttProjection, form: SttForm): boolean {
  return !!form.secret.trim() || kindForProjection(existing) !== form.kind
    || existing.connection?.base_url !== form.baseUrl
    || existing.offering?.model?.model_id !== form.modelId;
}

interface SttActivation {
  providerAccessId: string | null;
  enabled: boolean;
}

/** Capture command intent before waiting for other routing writes. A toggle
 * must not become a configuration replacement when newer metadata arrives. */
export function captureSttActivation(documentValue: RoutingDocument, form: SttForm): SttActivation | null {
  const existing = findSttProvider(documentValue);
  if (form.enabled && (!existing || sttConfigurationChanged(existing, form))) return null;
  return {providerAccessId: existing?.access.provider_access_id || null, enabled: form.enabled};
}

/** A toggle edits only the captured access's enabled flag. In particular it
 * never reconstructs connections, credentials, grants, offerings or siblings.
 * Configuration edits still use the separately admitted Provider bundle path.
 */
export function buildSttActivationDocument(
  documentValue: RoutingDocument, intent: SttActivation,
): RoutingDocument | null {
  if (!intent.providerAccessId) return null;
  const existing = documentValue.provider_accesses.find(access =>
    access.provider_access_id === intent.providerAccessId && access.provider_id === STT_PROVIDER_ID);
  if (!existing) throw new Error('speech access changed before activation');
  if (existing.enabled === intent.enabled) return null;
  const candidate: RoutingDocument = JSON.parse(JSON.stringify(documentValue));
  return {...candidate, provider_accesses: candidate.provider_accesses.map(access =>
    access.provider_access_id === existing.provider_access_id
      ? {...access, enabled: intent.enabled} : access)};
}

export function buildSttProviderBundle(documentValue: RoutingDocument | null, form: SttForm | null, displayName: string): ProviderBundle | null {
  if (!form?.enabled) return null;
  if (!documentValue) throw new Error('model-routing v2 authority is not loaded');
  const {kind, modelId, baseUrl} = form;
  const meta = STT_PROVIDER_META[kind];
  const plaintext = form.secret.trim();
  const existing = findSttProvider(documentValue);
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
      name: displayName,
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
      identity_confidence: 'high',
      probe_status: 'unprobed',
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

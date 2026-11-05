/** Atomic Provider commands. Builds detached endpoint payloads from the current
 * draft after joining the shared routing CAS chain. Confirmation UI and notices
 * belong to the view; this owner captures and validates the confirmed target.
 * Backend Provider/secret transactions retain authority.
 */
import type {ModelRoutingDocument, ProviderBundle, ProviderResponse, ProviderDeleteResponse,
  ModelRoutingCredentialMetadata, ListedModelsImportResponse} from '../../api/api-v1-model-routing.generated';
import type {ModelRoutingMutationReceipt, ModelRoutingMutationResult} from './model-routing-authority';
import {createProviderSettingsProjection, type ProviderSettingsContext} from './provider-settings-projection';

type WriteBundle = ProviderBundle & {creators: never[]; models: never[]; credential_secrets?: Record<string, string>};
interface MutationPorts {
  document(): Partial<ModelRoutingDocument> | null;
  mutate(send: (revision: number) => Promise<ModelRoutingMutationReceipt | {unchanged: true}>):
    Promise<ModelRoutingMutationResult>;
  save(providerId: string, bundle: WriteBundle, revision: number): Promise<ProviderResponse | null>;
  remove(providerId: string, revision: number): Promise<ProviderDeleteResponse | null>;
  importListed(providerId: string): Promise<ListedModelsImportResponse | null>;
  newId(): string;
  selectionChanged(): Error;
}

function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)); }

function bundleFrom(context: ProviderSettingsContext): WriteBundle {
  return clone({provider: context.provider, provider_access: context.access,
    connections: context.connections.map(item => item.row),
    credentials: context.credentials.map(item => item.row),
    offerings: context.offerings.map(item => item.row),
    deployments: context.deployments.map(item => item.row), creators: [], models: []});
}

type RemovalPorts = Pick<MutationPorts, 'document' | 'mutate' | 'remove' | 'selectionChanged'>;

function removeProvider(ports: RemovalPorts, providerId: string, accessId: string): Promise<ModelRoutingMutationResult> {
  return ports.mutate(async revision => {
    const document = ports.document();
    const context = createProviderSettingsProjection(document, providerId).context(providerId);
    if (!context || context.access.provider_access_id !== accessId) throw ports.selectionChanged();
    const accesses = new Set((document?.provider_accesses || []).filter(row => row.provider_id === providerId)
      .map(row => row.provider_access_id));
    const removedCredentialIds = (document?.credentials || []).filter(row => accesses.has(row.provider_access_id))
      .map(row => row.credential_id);
    const receipt = await ports.remove(providerId, revision);
    if (!receipt || receipt.deleted !== providerId) throw ports.selectionChanged();
    return {revision: receipt.revision, removedCredentialIds};
  });
}

/** Capture the displayed target before an awaited confirmation. Queue admission
 * rechecks its access identity; a replaced editor cannot inherit that approval.
 */
export function prepareProviderRemoval(ports: RemovalPorts, providerId: string): () => Promise<ModelRoutingMutationResult> {
  const document = ports.document();
  const context = createProviderSettingsProjection(document, providerId).context(providerId);
  if (!context) throw ports.selectionChanged();
  const accessId = context.access.provider_access_id;
  return () => {
    const current = createProviderSettingsProjection(ports.document(), providerId).context(providerId);
    if (ports.document() !== document || current?.access !== context.access ||
        current.access.provider_access_id !== accessId) throw ports.selectionChanged();
    return removeProvider(ports, providerId, accessId);
  };
}

class ProviderMutations {
  constructor(private readonly ports: MutationPorts) {}

  addCredential(providerId: string, accessId: string, secret: string): Promise<ModelRoutingMutationResult> {
    return this.ports.mutate(async revision => {
      const context = this.context(providerId, accessId);
      const bundle = bundleFrom(context);
      const credentialId = this.ports.newId();
      const models = new Map<string, {creator_id: string; model_id: string}>();
      for (const {row} of context.offerings) {
        if (row.identity_state === 'confirmed' && row.model) {
          models.set(JSON.stringify([row.model.creator_id, row.model.model_id]), clone(row.model));
        }
      }
      const credential: ModelRoutingCredentialMetadata = {
        credential_id: credentialId, provider_access_id: accessId, kind: 'api_key',
        secret_reference: '', key_hint: '', enabled: true,
        authorization: {connection_ids: context.connections.map(item => item.row.connection_id),
          models: [...models.values()]}, quota_policy: {},
      };
      bundle.credentials = [...bundle.credentials, credential];
      bundle.credential_secrets = {[credentialId]: JSON.stringify({
        format: 'tofu.credential-secret/v1', api_key: secret.trim(), oauth: '', extra_headers: {},
      })};
      return this.save(providerId, bundle, revision);
    });
  }

  removeCredential(providerId: string, accessId: string, credentialId: string): Promise<ModelRoutingMutationResult> {
    return this.ports.mutate(async revision => {
      const context = this.context(providerId, accessId);
      if (!context.credentials.some(item => item.row.credential_id === credentialId)) throw this.ports.selectionChanged();
      const bundle = bundleFrom(context);
      bundle.credentials = bundle.credentials.filter(row => row.credential_id !== credentialId);
      const receipt = await this.save(providerId, bundle, revision);
      return {...receipt, removedCredentialIds: [credentialId]};
    });
  }

  prepareRemoval(providerId: string): () => Promise<ModelRoutingMutationResult> {
    return prepareProviderRemoval(this.ports, providerId);
  }

  /** Add the rest of the provider's authenticated /models listing. */
  async importListedModels(providerId: string, accessId: string):
      Promise<ModelRoutingMutationResult & {added: number; listed: number}> {
    let counts = {added: 0, listed: 0};
    const result = await this.ports.mutate(async () => {
      this.context(providerId, accessId);
      const receipt = await this.ports.importListed(providerId);
      if (!receipt || receipt.provider_id !== providerId) throw this.ports.selectionChanged();
      counts = {added: receipt.added_model_count, listed: receipt.listed_model_count};
      return counts.added ? {revision: receipt.revision} : {unchanged: true as const};
    });
    return {...result, ...counts};
  }

  private context(providerId: string, accessId: string): ProviderSettingsContext {
    const context = createProviderSettingsProjection(this.ports.document(), providerId).context(providerId);
    if (!context || context.access.provider_access_id !== accessId) throw this.ports.selectionChanged();
    return context;
  }

  private async save(providerId: string, bundle: WriteBundle, revision: number): Promise<ModelRoutingMutationReceipt> {
    const receipt = await this.ports.save(providerId, bundle, revision);
    if (!receipt?.provider || receipt.provider.provider.provider_id !== providerId) throw this.ports.selectionChanged();
    return {revision: receipt.revision};
  }
}

export function createProviderMutations(ports: MutationPorts): ProviderMutations {
  return new ProviderMutations(ports);
}

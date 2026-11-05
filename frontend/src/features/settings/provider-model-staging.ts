/** Provider model-add preparation and publication into the current editor draft.
 * Entry point addProviderModels fences awaited reads, prepares the whole batch,
 * then publishes synchronously. Shared face planning and routing CAS ports keep
 * their authority; this owner never persists or admits catalog facts on its own.
 */
import type {
  ModelRoutingConnection,
  ModelRoutingCredentialAuthorization,
  ModelRoutingDeployment,
  ModelRoutingDocument,
  ModelRoutingModel,
  ModelRoutingModelRef,
  ModelRoutingOffering,
} from '../../api/api-v1-model-routing.generated';
import type { RequestNameFacePlan } from './provider-request-names';
import {
  authorizeProviderConnection,
  resolveProviderFaceConnection,
  type ProviderConnectionStagingPorts,
} from './provider-connection-staging';
import {
  createProviderSettingsProjection,
  providerWireModelIds,
  type ProviderSettingsContext,
} from './provider-settings-projection';

type EditorDocument = Omit<ModelRoutingDocument, 'connections' | 'offerings' | 'deployments'> & {
  connections: ModelRoutingConnection[];
  offerings: ModelRoutingOffering[];
  deployments: ModelRoutingDeployment[];
};
type RequestNames = Readonly<Record<string, ReadonlyArray<string>>>;
type FacePlans = Readonly<Record<string, RequestNameFacePlan>>;

export interface ProviderModelStagingPorts extends ProviderConnectionStagingPorts {
  document(): EditorDocument | null;
  readCapabilities(): Promise<{metadata_inheritance_supported?: boolean}>;
  planRequestNames(models: ReadonlyArray<ModelRoutingModel>, urls: string[]): Promise<RequestNames>;
  planFaces(names: string[], urls: string[]): Promise<FacePlans>;
  missingConnection(): void;
  selectionChanged(): Error;
  render(): void;
  persist(): Promise<unknown>;
}

function modelKey(model: ModelRoutingModelRef): string {
  return (model.creator_id || '') + '::' + (model.model_id || '');
}

function plannerInputs(context: ProviderSettingsContext): string {
  return JSON.stringify([context.access.provider_access_id,
    context.connections.map(({row}) => [row.connection_id, row.base_url])]);
}

function currentCatalog(
  document: EditorDocument, selected: ReadonlyArray<string>, changed: () => Error,
): ModelRoutingModel[] {
  const models = new Map<string, ModelRoutingModel>();
  for (const model of document.models || []) {
    const key = modelKey(model);
    if (!models.has(key)) models.set(key, model);
  }
  return selected.map(key => {
    const model = models.get(key);
    if (!model || model.lifecycle === 'retired') throw changed();
    return model;
  });
}

function pendingRequestNames(
  context: ProviderSettingsContext, models: ReadonlyArray<ModelRoutingModel>,
  custom: ReadonlyArray<string>, names: RequestNames,
): string[] {
  const existing = providerWireModelIds(context);
  const pending: string[] = [];
  for (const model of models) {
    for (const name of names[model.model_id] || [String(model.model_id || '')]) {
      if (name && !existing.has(name)) pending.push(name);
    }
  }
  for (const raw of custom) {
    const name = String(raw || '').trim();
    if (name && !existing.has(name)) pending.push(name);
  }
  return pending;
}

function authorizeModel(
  authorization: ModelRoutingCredentialAuthorization | undefined, ref: ModelRoutingModelRef,
): ModelRoutingCredentialAuthorization {
  const current = authorization || {connection_ids: [], models: []};
  const grants = current.models || [];
  const exists = grants.some(grant => String(grant.creator_id || '') === String(ref.creator_id) &&
    String(grant.model_id || '') === String(ref.model_id));
  return exists ? current : {...current, models: [...grants, {...ref}]};
}

interface ModelBatch {
  models: ReadonlyArray<ModelRoutingModel>;
  custom: ReadonlyArray<string>;
  names: RequestNames;
  faces: FacePlans;
  defaultConnectionId: string;
  inheritMetadata: boolean;
}

/** Work on replacement rows and new arrays; do not mutate any source row while
 * resolving faces, grants or generated IDs. There is no await during publication. */
function publishModelBatch(
  document: EditorDocument, context: ProviderSettingsContext,
  batch: ModelBatch, ports: ProviderConnectionStagingPorts,
): number {
  const connections = context.connections.map(item => item.row);
  let authorizations = context.credentials.map(item => item.row.authorization);
  const offerings: ModelRoutingOffering[] = [];
  const deployments: ModelRoutingDeployment[] = [];
  const wireIds = providerWireModelIds(context);
  const granted = new Set<string>();
  const connectionForWire = (wire: string) => {
    const result = resolveProviderFaceConnection(connections,
      context.access.provider_access_id, batch.faces[wire], ports);
    if (!result) return batch.defaultConnectionId;
    if (result.previous) connections[connections.indexOf(result.previous)] = result.connection;
    else connections.push(result.connection);
    authorizations = authorizations.map(value =>
      authorizeProviderConnection(value, result.connection.connection_id));
    return result.connection.connection_id;
  };
  const addOffering = (names: ReadonlyArray<string>, model?: ModelRoutingModel) => {
    const offering: ModelRoutingOffering = {
      offering_id: ports.newId('offering'), provider_access_id: context.access.provider_access_id,
      identity_state: model ? 'confirmed' : 'pending_identity',
      ...(model ? {model: {creator_id: model.creator_id, model_id: model.model_id}} : {pending_model_id: names[0]}),
      enabled: true, stale: false,
      capabilities: model?.capabilities?.length ? model.capabilities.slice() : ['text'],
      context_window: model?.context_window || 128000,
      ...(batch.inheritMetadata ? {inherited_model_fields: ['capabilities', 'context_window'] as const} : {}),
      priority: 100,
    };
    offerings.push(offering);
    for (const wire of names) {
      deployments.push({
        deployment_id: ports.newId('deployment'), offering_id: offering.offering_id,
        connection_id: connectionForWire(wire), wire_model_id: wire,
        identity_confidence: model ? 'high' : 'pending', probe_status: 'unprobed', priority: 100,
      });
      wireIds.add(wire);
    }
    if (model && !granted.has(modelKey(model))) {
      granted.add(modelKey(model));
      const ref = {creator_id: model.creator_id, model_id: model.model_id};
      authorizations = authorizations.map(value => authorizeModel(value, ref));
    }
  };
  for (const model of batch.models) {
    const names = (batch.names[model.model_id] || [String(model.model_id || '')])
      .filter(name => name && !wireIds.has(name));
    if (names.length) addOffering(names, model);
  }
  for (const raw of batch.custom) {
    const wire = String(raw || '').trim();
    if (!wire || wire.length > 256 || /[\u0000-\u001f]/.test(wire) || wireIds.has(wire)) continue;
    addOffering([wire]);
  }
  context.connections.forEach((item, index) => {
    if (item.row !== connections[index]) Object.assign(item.row, connections[index]);
  });
  for (const connection of connections.slice(context.connections.length)) document.connections.push(connection);
  context.credentials.forEach((item, index) => {
    if (item.row.authorization !== authorizations[index]) item.row.authorization = authorizations[index];
  });
  for (const offering of offerings) document.offerings.push(offering);
  for (const deployment of deployments) document.deployments.push(deployment);
  return offerings.length;
}

export async function addProviderModels(
  providerId: string, catalogModels: ReadonlyArray<ModelRoutingModel>,
  customWireIds: ReadonlyArray<string>, ports: ProviderModelStagingPorts,
): Promise<number> {
  const document = ports.document();
  const original = createProviderSettingsProjection(document, providerId).context(providerId);
  if (!document || !original) return 0;
  const connection = original.connections.find(item => item.row.enabled !== false) || original.connections[0];
  if (!connection) { ports.missingConnection(); return 0; }
  const connectionId = connection.row.connection_id;
  const inputs = plannerInputs(original);
  const selected = catalogModels.map(modelKey);
  const custom = [...customWireIds];
  const current = () => {
    if (ports.document() !== document) return null;
    const context = createProviderSettingsProjection(document, providerId).context(providerId);
    if (!context) return null;
    if (plannerInputs(context) !== inputs) throw ports.selectionChanged();
    return {context, models: currentCatalog(document, selected, ports.selectionChanged)};
  };
  // Capability negotiation never adopts a server response into the editor.
  const capabilities = await ports.readCapabilities();
  let view = current();
  if (!view) return 0;
  const names = await ports.planRequestNames(view.models, view.context.connections.map(item => item.row.base_url));
  view = current();
  if (!view) return 0;
  const pending = pendingRequestNames(view.context, view.models, custom, names);
  const faces = await ports.planFaces(pending, view.context.connections.map(item => item.row.base_url));
  view = current();
  if (!view) return 0;
  const added = publishModelBatch(document, view.context, {
    models: view.models, custom, names, faces, defaultConnectionId: connectionId,
    inheritMetadata: capabilities.metadata_inheritance_supported === true,
  }, ports);
  if (added) {
    ports.render();
    // Preserve the complete staged batch if CAS/save fails; the existing
    // authority owns retry and newer edits made while this save is pending.
    await ports.persist();
  }
  return added;
}

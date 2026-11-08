/** Provider Settings read model shared by cards, supply rows and the model picker.
 * Entry points consume the current editor draft; generated routing types own the
 * schema. A projection lives for one synchronous render, retains original rows
 * and source indices, and never owns persistence, credentials or mutation policy.
 */
import type {
  ModelRoutingConnection,
  ModelRoutingCredentialMetadata,
  ModelRoutingDeployment,
  ModelRoutingDocument,
  ModelRoutingMediaPricing,
  ModelRoutingModel,
  ModelRoutingOffering,
  ModelRoutingPricing,
  ModelRoutingProvider,
  ModelRoutingProviderAccess,
} from '../../api/api-v1-model-routing.generated';

interface IndexedRoutingRow<T> {
  readonly row: T;
  readonly index: number;
}

interface ModelMetadata {
  readonly modelNames: Record<string, string>;
  readonly modelReleaseDates: Record<string, string>;
  readonly modelListPricing: Record<string, ModelRoutingPricing>;
  /** Registered media prices, else the maintained official projection. */
  readonly modelMediaPricing: Record<string, ModelRoutingMediaPricing>;
  readonly modelLifecycles: Record<string, ModelRoutingModel['lifecycle']>;
}

export interface ProviderSettingsContext extends ModelMetadata {
  readonly provider: ModelRoutingProvider;
  readonly access: ModelRoutingProviderAccess;
  readonly accessIndex: number;
  readonly connections: ReadonlyArray<IndexedRoutingRow<ModelRoutingConnection>>;
  readonly credentials: ReadonlyArray<IndexedRoutingRow<ModelRoutingCredentialMetadata>>;
  readonly offerings: ReadonlyArray<IndexedRoutingRow<ModelRoutingOffering>>;
  readonly deployments: ReadonlyArray<IndexedRoutingRow<ModelRoutingDeployment>>;
}

export interface ProviderSettingsProjection {
  context(providerId: string): ProviderSettingsContext | null;
}

function indexRowsBy<T>(
  rows: ReadonlyArray<T>, key: (row: T) => string, include?: (row: T) => boolean,
) {
  const groups = new Map<string, Array<IndexedRoutingRow<T>>>();
  rows.forEach((row, index) => {
    if (include && !include(row)) return;
    const id = key(row);
    let group = groups.get(id);
    if (!group) groups.set(id, group = []);
    group.push({row, index});
  });
  return groups;
}

function catalogMetadata(models: ReadonlyArray<ModelRoutingModel>): ModelMetadata {
  const modelNames: ModelMetadata['modelNames'] = {};
  const modelReleaseDates: ModelMetadata['modelReleaseDates'] = {};
  const modelListPricing: ModelMetadata['modelListPricing'] = {};
  const modelMediaPricing: ModelMetadata['modelMediaPricing'] = {};
  const modelLifecycles: ModelMetadata['modelLifecycles'] = {};
  for (const model of models) {
    const key = (model.creator_id || '') + '::' + (model.model_id || '');
    modelNames[key] = model.display_name || model.model_id;
    if (model.release_date) modelReleaseDates[key] = model.release_date;
    if (model.list_pricing) modelListPricing[key] = model.list_pricing;
    const mediaPricing = model.media_pricing || model.official_media_pricing;
    if (mediaPricing) modelMediaPricing[key] = mediaPricing;
    if (model.lifecycle) modelLifecycles[key] = model.lifecycle;
  }
  return {modelNames, modelReleaseDates, modelListPricing, modelMediaPricing, modelLifecycles};
}

function deploymentsByAccess(
  document: Partial<ModelRoutingDocument>, includeAccess: (row: {provider_access_id: string}) => boolean,
) {
  const accessesByOffering = new Map<string, Set<string>>();
  for (const offering of document.offerings || []) {
    if (!includeAccess(offering)) continue;
    let accesses = accessesByOffering.get(offering.offering_id);
    if (!accesses) accessesByOffering.set(offering.offering_id, accesses = new Set());
    accesses.add(offering.provider_access_id);
  }
  const groups = new Map<string, Array<IndexedRoutingRow<ModelRoutingDeployment>>>();
  (document.deployments || []).forEach((row, index) => {
    for (const accessId of accessesByOffering.get(row.offering_id) || []) {
      let group = groups.get(accessId);
      if (!group) groups.set(accessId, group = []);
      group.push({row, index});
    }
  });
  return groups;
}

/** Build once per render; scope event reads to onlyProviderId. Create a fresh
 * view after edits or awaited work, without caching the mutable draft. */
export function createProviderSettingsProjection(
  document: Partial<ModelRoutingDocument> | null | undefined,
  onlyProviderId?: string,
): ProviderSettingsProjection {
  const value = document || {};
  const includeProvider = (row: {provider_id: string}) =>
    onlyProviderId === undefined || row.provider_id === onlyProviderId;
  const providers = indexRowsBy(value.providers || [], row => row.provider_id, includeProvider);
  const accesses = indexRowsBy(value.provider_accesses || [], row => row.provider_id, includeProvider);
  const selectedAccess = onlyProviderId === undefined ? undefined : accesses.get(onlyProviderId)?.[0]?.row;
  if (onlyProviderId !== undefined && (!providers.has(onlyProviderId) || !selectedAccess)) {
    return {context: () => null};
  }
  const includeAccess = (row: {provider_access_id: string}) =>
    !selectedAccess || row.provider_access_id === selectedAccess.provider_access_id;
  const connections = indexRowsBy(value.connections || [], row => row.provider_access_id, includeAccess);
  const credentials = indexRowsBy(value.credentials || [], row => row.provider_access_id, includeAccess);
  const offerings = indexRowsBy(value.offerings || [], row => row.provider_access_id, includeAccess);
  const deployments = deploymentsByAccess(value, includeAccess);
  const metadata = catalogMetadata(value.models || []);
  return {
    context(providerId) {
      // The current card UI selects the first access for a provider. Preserve
      // that view policy; admitting or editing accesses belongs to other owners.
      const provider = providers.get(providerId)?.[0]?.row;
      const access = accesses.get(providerId)?.[0];
      if (!provider || !access) return null;
      const accessId = access.row.provider_access_id;
      return {
        provider, access: access.row, accessIndex: access.index,
        connections: connections.get(accessId) || [],
        credentials: credentials.get(accessId) || [],
        offerings: offerings.get(accessId) || [],
        deployments: deployments.get(accessId) || [],
        ...metadata,
      };
    },
  };
}

export interface ProviderSupplyRow {
  readonly offering: ModelRoutingOffering;
  readonly offeringIndex: number;
  readonly canonical: string;
  readonly pending: boolean;
  readonly retired: boolean;
  readonly releaseDate: string;
  readonly pricing: ModelRoutingPricing | null;
  readonly catalogPrice: boolean;
  /** The Model's media reference price; Offerings carry no media price. */
  readonly mediaPricing: ModelRoutingMediaPricing | null;
  readonly wireIds: ReadonlyArray<string>;
  readonly deploymentByWire: Readonly<Record<string, number>>;
  readonly capabilities: ReadonlyArray<string>;
}

/** Preserve supply and request-name order, including legacy disabled routes. */
export function providerSupplyRows(context: ProviderSettingsContext | null): ProviderSupplyRow[] {
  if (!context) return [];
  const byOffering = new Map<string, Array<IndexedRoutingRow<ModelRoutingDeployment>>>();
  for (const deployment of context.deployments) {
    const id = deployment.row.offering_id;
    let group = byOffering.get(id);
    if (!group) byOffering.set(id, group = []);
    group.push(deployment);
  }
  const rows: ProviderSupplyRow[] = [];
  for (const item of context.offerings) {
    const offering = item.row;
    const pending = offering.identity_state === 'pending_identity' || !offering.model;
    const canonical = String(pending
      ? offering.pending_model_id || offering.offering_id || ''
      : offering.model?.model_id || '').trim();
    if (!canonical) continue;
    const wireIds = new Set<string>();
    const deploymentByWire: Record<string, number> = {};
    for (const deployment of byOffering.get(offering.offering_id) || []) {
      const wire = String(deployment.row.wire_model_id || '').trim();
      if (!wire || wireIds.has(wire)) continue;
      wireIds.add(wire);
      deploymentByWire[wire] = deployment.index;
    }
    const modelKey = pending ? '' : (String(offering.model?.creator_id || '') + '::' + canonical);
    rows.push({
      offering, offeringIndex: item.index, canonical, pending,
      retired: !pending && context.modelLifecycles[modelKey] === 'retired',
      releaseDate: pending ? '' : (context.modelReleaseDates[modelKey] || ''),
      pricing: pending ? null : (offering.actual_pricing || context.modelListPricing[modelKey] || null),
      catalogPrice: !pending && !offering.actual_pricing && !!context.modelListPricing[modelKey],
      mediaPricing: pending ? null : (context.modelMediaPricing[modelKey] || null),
      wireIds: [...wireIds], deploymentByWire,
      capabilities: (offering.capabilities || []).slice(),
    });
  }
  return rows;
}

/** Staging compares raw request names; display-only trimming belongs to rows. */
export function providerWireModelIds(context: ProviderSettingsContext): Set<string> {
  return new Set(context.deployments.map(item => String(item.row.wire_model_id || '')));
}

export function providerAddableCatalogModels(
  document: Partial<ModelRoutingDocument>, context: ProviderSettingsContext,
): ModelRoutingModel[] {
  const offered = new Set<string>();
  for (const {row} of context.offerings) {
    if (row.identity_state === 'confirmed' && row.model) {
      offered.add((row.model.creator_id || '') + '::' + (row.model.model_id || ''));
    }
  }
  return (document.models || []).filter(model => model.lifecycle !== 'retired' &&
    !offered.has((model.creator_id || '') + '::' + (model.model_id || '')));
}

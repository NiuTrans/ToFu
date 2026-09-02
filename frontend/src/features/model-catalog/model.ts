import { offeringId } from './offering-id';
import type {
  HealthMap,
  LogicalRow,
  ModelCatalogPayload,
  Offering,
  OfferingConfiguration,
  OfferingRow,
  Pricing,
  ProviderMap,
  Route,
} from './types';

/**
 * Pure deterministic projection of a catalog payload into logical rows, plus
 * CAS-safe mutation builders that never touch server state.
 *
 * Grouping is strictly key-based and identity-exact. An offering belongs to a
 * logical model only when ``offering.model_id`` equals the logical map key;
 * the ordered membership comes from ``routes[model_id].offering_ids`` (one
 * route per model). There is no alias/fuzzy merge.
 */

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string');
}

function unique(values: readonly string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    if (!value || seen.has(value)) continue;
    seen.add(value);
    result.push(value);
  }
  return result;
}

function compareIds(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0;
}

function sortedModelIds(catalog: ModelCatalogPayload): string[] {
  return Object.keys(catalog?.models ?? {}).sort(compareIds);
}

function sortedOfferingIds(ids: readonly string[]): string[] {
  return [...ids].sort(compareIds);
}

export function modelLabel(model: { display_name?: unknown; model_id?: unknown } | undefined, id: string): string {
  const display = model?.display_name;
  if (typeof display === 'string' && display.trim()) return display;
  return id;
}

export function providerLabel(
  providers: ProviderMap | undefined,
  id: string,
): string {
  const provider = providers?.[id];
  if (!provider) return id;
  if (typeof provider.label === 'string' && provider.label.trim()) {
    return provider.label;
  }
  if (typeof provider.name === 'string' && provider.name.trim()) {
    return provider.name;
  }
  if (typeof provider.brand === 'string' && provider.brand.trim()) {
    return provider.brand;
  }
  return id;
}

export function offeringWireIds(offering: Offering): string[] {
  const configuration = offering?.configuration;
  const requestIds = configuration?.request_ids;
  return unique(asStringList(requestIds));
}

export function offeringProtocol(
  offering: Offering,
  providers: ProviderMap | undefined,
): string {
  const provider = providers?.[offering?.provider_id ?? ''];
  if (typeof provider?.protocol === 'string' && provider.protocol.trim()) {
    return provider.protocol;
  }
  return '';
}

export function offeringPricing(offering: Offering): Pricing | null {
  const pricing = offering?.configuration?.pricing;
  return pricing && typeof pricing === 'object' ? pricing as Pricing : null;
}

export function offeringRpm(offering: Offering): number | null {
  const value = offering?.configuration?.rpm;
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

export function offeringContextWindow(offering: Offering): number | null {
  const value = offering?.configuration?.context_window;
  if (value === null || value === undefined) return null;
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

export function offeringCapabilities(offering: Offering): string[] {
  return unique(asStringList(offering?.configuration?.capabilities));
}

/** Health is authoritative only when the server explicitly marks this offering healthy. */
export function offeringHealthy(
  offering: Offering,
  health: HealthMap | undefined,
): boolean {
  const offeringId = offering?.offering_id;
  return Boolean(offeringId && health?.[offeringId]?.healthy === true);
}

/** Route ordering is offering-id sorted, matching ``lib.model_catalog._assemble``. */
function orderedOfferingIdsForModel(
  catalog: ModelCatalogPayload,
  modelId: string,
): string[] {
  const route = catalog?.routes?.[modelId] as Route | undefined;
  const offerings = catalog?.offerings ?? {};
  const fromRoute = asStringList(route?.offering_ids);
  const discovered = Object.keys(offerings).filter((offeringId) => {
    const offering = offerings[offeringId] as Offering;
    return offering?.model_id === modelId;
  });
  const merged = unique([...fromRoute, ...discovered]);
  return sortedOfferingIds(merged).filter((id) => (
    Object.prototype.hasOwnProperty.call(offerings, id)
  ));
}

function buildOfferingRow(
  offeringId: string,
  catalog: ModelCatalogPayload,
  providers: ProviderMap | undefined,
  health: HealthMap | undefined,
): OfferingRow {
  const offering = (catalog?.offerings?.[offeringId] ?? {}) as Offering;
  const providerId = typeof offering.provider_id === 'string'
    ? offering.provider_id : '';
  return {
    id: offeringId,
    providerId,
    providerLabel: providerLabel(providers, providerId),
    protocol: offeringProtocol(offering, providers),
    wireIds: offeringWireIds(offering),
    rpm: offeringRpm(offering),
    capabilities: offeringCapabilities(offering),
    contextWindow: offeringContextWindow(offering),
    pricing: offeringPricing(offering),
    enabled: offering.enabled !== false,
    healthy: offeringHealthy(offering, health),
  };
}

export function buildLogicalRows(
  catalog: ModelCatalogPayload,
  providers?: ProviderMap,
  health?: HealthMap,
): LogicalRow[] {
  const models = catalog?.models ?? {};
  return sortedModelIds(catalog).map((id) => {
    const model = models[id] ?? {};
    const offeringIds = orderedOfferingIdsForModel(catalog, id);
    const offerings = offeringIds.map((offeringId) => (
      buildOfferingRow(offeringId, catalog, providers, health)
    ));
    return {
      id,
      label: modelLabel(model, id),
      enabled: model.enabled !== false,
      capabilities: unique(asStringList(model.capabilities)),
      offeringIds,
      offerings,
      enabledCount: offerings.filter((offering) => offering.enabled).length,
      healthyCount: offerings.filter((offering) => offering.healthy).length,
      providerLabels: unique(
        offerings.map((offering) => offering.providerLabel).filter(Boolean),
      ),
    };
  });
}

function cloneWireValue<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => cloneWireValue(item)) as unknown as T;
  }
  if (value !== null && typeof value === 'object') {
    const cloned: Record<string, unknown> = {};
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      cloned[key] = cloneWireValue(child);
    }
    return cloned as T;
  }
  return value;
}

/** Deep-clone the JSON wire catalog so pending mutations cannot taint the cached envelope. */
export function cloneCatalog(catalog: ModelCatalogPayload): ModelCatalogPayload {
  return cloneWireValue(catalog);
}

function cloneOffering(offering: Offering): Offering {
  return {
    ...offering,
    configuration: { ...(offering.configuration ?? {}) },
  };
}

function cloneModel(model: ModelCatalogPayload['models'][string]): ModelCatalogPayload['models'][string] {
  return { ...model };
}

function modelEnabled(catalog: ModelCatalogPayload, modelId: string): boolean {
  return Object.values(catalog.offerings ?? {}).some((offering) => (
    (offering as Offering).model_id === modelId && offering.enabled !== false
  ));
}

/** Set a logical model's enabled and mirror it onto every offering of that model. */
export function setLogicalEnabled(
  catalog: ModelCatalogPayload,
  id: string,
  enabled: boolean,
): ModelCatalogPayload {
  const model = cloneModel(catalog.models[id] ?? {
    model_id: id,
    enabled: false,
    capabilities: [],
  });
  model.model_id = id;
  model.enabled = Boolean(enabled);
  catalog.models[id] = model;

  for (const [offeringId, offering] of Object.entries(catalog.offerings ?? {})) {
    if ((offering as Offering).model_id !== id) continue;
    catalog.offerings[offeringId] = {
      ...cloneOffering(offering as Offering),
      enabled: Boolean(enabled),
    };
  }
  return catalog;
}

/** Set one offering's enabled and recompute the owning logical model's enabled. */
export function setOfferingEnabled(
  catalog: ModelCatalogPayload,
  id: string,
  enabled: boolean,
): ModelCatalogPayload {
  const existing = catalog.offerings[id] as Offering | undefined;
  const offering = cloneOffering(existing ?? {
    offering_id: id,
    provider_id: '',
    model_id: '',
    enabled: false,
    configuration: {},
  });
  offering.offering_id = id;
  offering.enabled = Boolean(enabled);
  catalog.offerings[id] = offering;

  const modelId = offering.model_id;
  if (modelId && catalog.models[modelId]) {
    catalog.models[modelId] = {
      ...cloneModel(catalog.models[modelId]),
      model_id: modelId,
      enabled: modelEnabled(catalog, modelId),
    };
  }
  return catalog;
}

/** Add a logical model together with its first provider offering. */
export function addModelWithOffering(
  catalog: ModelCatalogPayload,
  params: {
    modelId: string;
    displayName?: string;
    providerId: string;
    configuration: OfferingConfiguration;
  },
): ModelCatalogPayload {
  const modelId = params.modelId.trim();
  const providerId = params.providerId.trim();
  const oid = offeringId(providerId, modelId);
  const capabilities = unique(asStringList(params.configuration.capabilities));
  const configuration: OfferingConfiguration = {
    ...params.configuration,
    capabilities,
  };
  if (params.displayName && params.displayName.trim()) {
    configuration.display_name = params.displayName.trim();
  }

  catalog.offerings[oid] = {
    offering_id: oid,
    provider_id: providerId,
    model_id: modelId,
    enabled: true,
    configuration,
    provenance: {},
  };

  catalog.models[modelId] = {
    model_id: modelId,
    enabled: true,
    capabilities,
    provenance: {},
  };
  if (params.displayName && params.displayName.trim()) {
    catalog.models[modelId].display_name = params.displayName.trim();
  }

  catalog.routes[modelId] = {
    model_id: modelId,
    offering_ids: [oid],
    strategy: 'score',
  };
  return catalog;
}

/** Attach another provider offering to an existing logical model. */
export function attachOffering(
  catalog: ModelCatalogPayload,
  params: {
    modelId: string;
    providerId: string;
    configuration: OfferingConfiguration;
  },
): ModelCatalogPayload {
  const modelId = params.modelId.trim();
  const providerId = params.providerId.trim();
  const oid = offeringId(providerId, modelId);
  const capabilities = unique(asStringList(params.configuration.capabilities));

  catalog.offerings[oid] = {
    offering_id: oid,
    provider_id: providerId,
    model_id: modelId,
    enabled: true,
    configuration: { ...params.configuration, capabilities },
    provenance: {},
  };

  const route = (catalog.routes[modelId] ?? {
    model_id: modelId,
    offering_ids: [],
    strategy: 'score',
  }) as Route;
  route.model_id = modelId;
  route.strategy = 'score';
  route.offering_ids = sortedOfferingIds(unique([
    ...asStringList(route.offering_ids),
    oid,
  ]));
  catalog.routes[modelId] = route;

  // Fold the new offering's capabilities into the logical union, and enable
  // the model when any offering is enabled (the new offering defaults on).
  const model = cloneModel(catalog.models[modelId] ?? {
    model_id: modelId,
    enabled: false,
    capabilities: [],
  });
  model.model_id = modelId;
  model.capabilities = unique([
    ...asStringList(model.capabilities),
    ...capabilities,
  ]);
  model.enabled = modelEnabled(catalog, modelId);
  catalog.models[modelId] = model;
  return catalog;
}

/** Edit an existing offering's provider-specific configuration in place. */
export function updateOfferingConfiguration(
  catalog: ModelCatalogPayload,
  offeringId: string,
  configuration: OfferingConfiguration,
): ModelCatalogPayload {
  const existing = catalog.offerings[offeringId] as Offering | undefined;
  if (!existing) return catalog;
  const capabilities = unique(asStringList(configuration.capabilities));
  const merged: OfferingConfiguration = {
    // Preserve unknown configuration fields already on the offering, then
    // overlay the edited provider-specific fields so nothing is dropped.
    ...(existing.configuration ?? {}),
    ...configuration,
    capabilities,
  };
  catalog.offerings[offeringId] = {
    ...cloneOffering(existing),
    configuration: merged,
  };

  const modelId = existing.model_id;
  if (modelId && catalog.models[modelId]) {
    const model = cloneModel(catalog.models[modelId]);
    model.model_id = modelId;
    model.capabilities = unique(
      Object.values(catalog.offerings)
        .filter((offering) => (offering as Offering).model_id === modelId)
        .flatMap((offering) => offeringCapabilities(offering as Offering)),
    );
    if (configuration.display_name) {
      model.display_name = configuration.display_name;
    }
    catalog.models[modelId] = model;
  }
  return catalog;
}

/** Remove an offering; when it was the last offering, remove the model and route. */
export function removeOffering(
  catalog: ModelCatalogPayload,
  offeringId: string,
): ModelCatalogPayload {
  const existing = catalog.offerings[offeringId] as Offering | undefined;
  if (existing) delete catalog.offerings[offeringId];

  const modelId = existing?.model_id;
  if (!modelId || !catalog.routes[modelId]) return catalog;

  const route = catalog.routes[modelId] as Route;
  route.offering_ids = asStringList(route.offering_ids).filter((id) => id !== offeringId);

  const remaining = Object.values(catalog.offerings).filter((offering) => (
    (offering as Offering).model_id === modelId
  ));
  if (remaining.length === 0) {
    delete catalog.routes[modelId];
    delete catalog.models[modelId];
    return catalog;
  }

  route.offering_ids = sortedOfferingIds(route.offering_ids);
  route.model_id = modelId;
  route.strategy = 'score';
  catalog.routes[modelId] = route;

  const model = cloneModel(catalog.models[modelId] ?? {
    model_id: modelId,
    enabled: false,
    capabilities: [],
  });
  model.model_id = modelId;
  model.enabled = modelEnabled(catalog, modelId);
  model.capabilities = unique(
    remaining.flatMap((offering) => offeringCapabilities(offering as Offering)),
  );
  catalog.models[modelId] = model;
  return catalog;
}

export function hasOffering(
  catalog: ModelCatalogPayload,
  providerId: string,
  modelId: string,
): boolean {
  return Object.prototype.hasOwnProperty.call(
    catalog?.offerings ?? {},
    offeringId(providerId, modelId),
  );
}

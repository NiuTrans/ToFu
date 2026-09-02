/**
 * Wire contracts for the centralized model catalog panel.
 *
 * The authoritative shape is ``contracts/model_catalog_v1.schema.json`` and
 * the pure compiler ``lib/model_catalog``. Logical models use ``model_id`` and
 * ``display_name``; offerings carry ``offering_id`` / ``provider_id`` /
 * ``model_id`` / ``enabled`` / nested ``configuration``; routes are keyed by
 * model id with an ordered ``offering_ids`` array. Unknown fields (future
 * canonical registration fields) are preserved via the index signatures.
 */

export type CatalogMap<T> = Record<string, T>;
export type ProviderMap = CatalogMap<ProviderDefinition>;
export type HealthMap = CatalogMap<HealthRow>;

/** Wire discriminator required by the authoritative model-catalog v1 schema. */
export const MODEL_CATALOG_CONTRACT_VERSION = 'tofu.model-catalog/v1' as const;

export interface LogicalModel {
  model_id: string;
  enabled: boolean;
  capabilities: string[];
  /** Optional human label; falls back to ``model_id`` when absent. */
  display_name?: string;
  brand?: string;
  thinking_default?: boolean;
  capability_profile?: Record<string, unknown>;
  provenance?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface Pricing {
  input?: number | null;
  output?: number | null;
  currency?: string;
  unit?: string;
  [key: string]: unknown;
}

export interface OfferingConfiguration {
  request_ids?: string[];
  aliases?: string[];
  rpm?: number;
  capabilities?: string[];
  context_window?: number | null;
  pricing?: Pricing | null;
  brand?: string;
  display_name?: string;
  thinking_default?: boolean;
  capability_profile?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface Offering {
  offering_id: string;
  provider_id: string;
  model_id: string;
  enabled: boolean;
  configuration: OfferingConfiguration;
  provenance?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface Route {
  model_id: string;
  /** Ordered offering ids for this logical model. */
  offering_ids: string[];
  strategy: 'score';
  [key: string]: unknown;
}

export interface ModelCatalogPayload {
  contract_version: string;
  revision: number;
  models: CatalogMap<LogicalModel>;
  offerings: CatalogMap<Offering>;
  routes: CatalogMap<Route>;
}

export interface ProviderDefinition {
  id: string;
  name?: string;
  label?: string;
  brand?: string;
  protocol?: string;
  enabled?: boolean;
  [key: string]: unknown;
}

export interface HealthRow {
  healthy?: boolean;
  status?: string;
  [key: string]: unknown;
}

export interface ModelCatalogEnvelope {
  ok: boolean;
  contract_version?: string;
  revision?: number;
  catalog: ModelCatalogPayload;
  providers?: ProviderMap;
  health?: HealthMap;
  error?: string;
}

export interface ModelCatalogPutPayload {
  expected_revision: number;
  catalog: ModelCatalogPayload;
}

/** One offering projected for the panel's per-model offering rows. */
export interface OfferingRow {
  id: string;
  providerId: string;
  providerLabel: string;
  protocol: string;
  wireIds: string[];
  rpm: number | null;
  capabilities: string[];
  contextWindow: number | null;
  pricing: Pricing | null;
  enabled: boolean;
  healthy: boolean;
}

/** One logical model projected for the panel's model rows. */
export interface LogicalRow {
  id: string;
  label: string;
  enabled: boolean;
  capabilities: string[];
  offeringIds: string[];
  offerings: OfferingRow[];
  enabledCount: number;
  healthyCount: number;
  providerLabels: string[];
}

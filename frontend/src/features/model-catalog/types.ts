/**
 * Model-routing v2 presentation types for the Settings model catalog.
 *
 * Responsibility: describe the read-only browser projection of canonical
 * Creator/Model facts. ProviderAccess, Offering, Deployment, aliases, routing,
 * and credential state are intentionally absent from this model-only surface.
 */

export interface ModelPricing {
  input?: number | null;
  output?: number | null;
  currency?: string;
  unit?: string;
  cache_read?: number | null;
  cache_write?: number | null;
}

export interface ModelCreator {
  creator_id: string;
  name: string;
}

export interface OfficialModel {
  creator_id: string;
  model_id: string;
  display_name: string;
  capabilities: string[];
  context_window: number;
  quality_rank: number;
  list_pricing?: ModelPricing;
  lifecycle?: 'stable' | 'preview' | 'dated_snapshot' | 'retired';
}

export interface ModelCatalogDocument {
  contract_version: 'tofu.model-routing/v2' | string;
  creators: ModelCreator[];
  models: OfficialModel[];
}

export interface AaScore {
  intelligence: number | null;
  coding: number | null;
  agentic: number | null;
  math: number | null;
  aa_name: string;
  aa_slug: string;
}

export interface AaBlock {
  status: 'ok' | 'stale' | 'no_key' | 'unavailable' | string;
  source?: string;
  source_url?: string;
  fetched_at?: number | null;
  key_source?: 'settings' | 'legacy_config' | 'env' | null;
  key_hint?: string;
  /** Scores are keyed by ``creator_id::model_id``. */
  scores?: Record<string, AaScore>;
}

export interface ModelCatalogRow {
  creatorId: string;
  creatorLabel: string;
  modelId: string;
  displayName: string;
  brand: string;
  capabilities: string[];
  contextWindow: number;
  registeredQualityRank: number | null;
  aa: AaScore | null;
  pricing: ModelPricing | null;
  lifecycle: OfficialModel['lifecycle'];
}

export interface VendorGroup {
  vendorId: string;
  label: string;
  icon: string;
  models: ModelCatalogRow[];
}

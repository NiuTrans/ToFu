/**
 * Pure Model-centric compiler for the Settings catalog.
 *
 * Models are grouped by their Creator using Model facts only. This owner does
 * not read ProviderAccess, Offering, Deployment, wire ids, aliases, or route
 * health; those concepts cannot affect what a Model is or how it is displayed.
 */

import { createModelCapabilityTaxonomy } from '../../core/model-capability-taxonomy';
import { detectVendor } from './vendor';
import type {
  AaScore,
  ModelCatalogRow,
  ModelPricing,
  ModelCatalogDocument,
  VendorGroup,
} from './types';

/** Resolves a capability list to its kind id (server-projected taxonomy). */
export type ModelKindResolver = (capabilities: readonly string[]) => string;

const fallbackKindOf: ModelKindResolver = createModelCapabilityTaxonomy().modelKind;

function unique(values: readonly string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

function priceAmount(value: unknown): number | null {
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

/** Standard 3:1 input/output blended cost per million tokens. */
export function blendedModelCost(pricing: ModelPricing | null): number | null {
  if (!pricing) return null;
  const input = priceAmount(pricing.input);
  const output = priceAmount(pricing.output);
  if (input === null || output === null) return null;
  // An all-zero card is a scan placeholder for "price unknown", never an
  // official free tier: report it as unplottable so the chart lists the
  // Model under missing-price exclusions instead of a fake $0 frontier.
  if (input <= 0 && output <= 0) return null;
  return (3 * input + output) / 4;
}

/** Catalog Models carrying a published AA Intelligence Index score. */
export function countAaScoredModels(groups: readonly VendorGroup[]): number {
  let scored = 0;
  for (const group of groups) {
    for (const row of group.models) {
      if (row.aa?.intelligence !== null && row.aa?.intelligence !== undefined) scored += 1;
    }
  }
  return scored;
}

/**
 * Supply-channel and serving-variant tokens that do not change what a model
 * IS: ``Qwen3_8-27B-Meituan`` is Qwen3.8-27B rented through Meituan, a
 * ``vendor/`` namespace is a supply path, and a trailing ``instruct`` tag on
 * one twin of an otherwise identical registration is the same snapshot.
 */
const SUPPLY_IDENTITY_TOKENS = new Set(['meituan', 'fp8', 'instruct']);

/**
 * Near-name twins that must never collapse: OpenAI sells
 * gpt-3.5-turbo-instruct as a distinct legacy model (own pricing, 4096
 * context), so folding it into gpt-3.5-turbo would misprice both.
 */
const IDENTITY_MERGE_EXCLUSIONS = new Set(['gpt-3.5-turbo-instruct']);

/** Semantic identity of a Model: developer plus punctuation-free leaf stem. */
function semanticModelKey(creatorId: string, modelId: string): string {
  if (IDENTITY_MERGE_EXCLUSIONS.has(modelId.toLowerCase())) {
    return `${creatorId}::!${modelId.toLowerCase()}`;
  }
  const leaf = modelId.slice(modelId.lastIndexOf('/') + 1).toLowerCase();
  const stem = leaf
    .split(/[^a-z0-9]+/)
    .filter((token) => token && !SUPPLY_IDENTITY_TOKENS.has(token))
    .join('');
  return `${creatorId}::${stem}`;
}

function compareRegistrations(left: ModelCatalogRow, right: ModelCatalogRow): number {
  const rank = (row: ModelCatalogRow): Array<number | string> => [
    row.modelId.includes('/') ? 1 : 0,
    row.modelId
      .toLowerCase()
      .split(/[^a-z0-9]+/)
      .some((token) => SUPPLY_IDENTITY_TOKENS.has(token))
      ? 1
      : 0,
    row.pricing ? 0 : 1,
    row.aa ? 0 : 1,
    row.modelId.length,
    row.modelId,
  ];
  const leftRank = rank(left);
  const rightRank = rank(right);
  for (let index = 0; index < leftRank.length; index += 1) {
    if (leftRank[index] < rightRank[index]) return -1;
    if (leftRank[index] > rightRank[index]) return 1;
  }
  return 0;
}

/**
 * Collapse duplicate registrations of one semantic model into the canonical
 * row (bare id > clean id > priced > scored > shorter), filling facts the
 * canonical registration lacks from its twins.
 */
function collapseRegistrations(rows: ModelCatalogRow[], kindOf: ModelKindResolver): ModelCatalogRow {
  const [survivor, ...twins] = [...rows].sort(compareRegistrations);
  for (const twin of twins) {
    survivor.pricing ??= twin.pricing;
    survivor.mediaPricing ??= twin.mediaPricing;
    survivor.mediaProfile ??= twin.mediaProfile;
    survivor.releaseDate ??= twin.releaseDate;
    survivor.registeredQualityRank ??= twin.registeredQualityRank;
    survivor.aa ??= twin.aa;
    if (!survivor.contextWindow) survivor.contextWindow = twin.contextWindow;
    survivor.lifecycle ??= twin.lifecycle;
    survivor.capabilities = unique([...survivor.capabilities, ...twin.capabilities]);
  }
  if (twins.length) survivor.kind = kindOf(survivor.capabilities);
  return survivor;
}

function compareModels(left: ModelCatalogRow, right: ModelCatalogRow): number {
  const leftScore = left.aa?.intelligence ?? left.registeredQualityRank ?? -Infinity;
  const rightScore = right.aa?.intelligence ?? right.registeredQualityRank ?? -Infinity;
  if (leftScore !== rightScore) return rightScore - leftScore;
  return left.displayName.localeCompare(right.displayName, undefined, {
    numeric: true,
    sensitivity: 'base',
  });
}

/** Build the one-row-per-official-Model projection used by Settings. */
export function buildVendorGroups(
  document: ModelCatalogDocument,
  aaScores: Record<string, AaScore> = {},
  kindOf: ModelKindResolver = fallbackKindOf,
): VendorGroup[] {
  if (document.contract_version !== 'tofu.model-routing/v2') return [];
  const creators = new Map(document.creators.map((row) => [row.creator_id, row]));

  const rows: ModelCatalogRow[] = [];
  for (const model of document.models) {
    // Persisted drafts belong to admission and research, not the official
    // registry. Legacy unmarked rows remain visible pending classification.
    if (model.registration_state && model.registration_state !== 'ready') continue;
    const creator = creators.get(model.creator_id);
    const creatorLabel = creator?.name || model.creator_id;
    const vendor = detectVendor(model.creator_id, creatorLabel, model.model_id);
    const score = Number(model.quality_rank);
    const capabilities = unique(model.capabilities ?? []);
    const row: ModelCatalogRow = {
      creatorId: model.creator_id,
      creatorLabel,
      modelId: model.model_id,
      displayName: model.display_name || model.model_id,
      brand: vendor.icon,
      capabilities,
      kind: kindOf(capabilities),
      contextWindow: Number(model.context_window) || 0,
      inferenceProfile: model.inference_profile,
      registeredQualityRank: Number.isFinite(score) && score > 0 ? score : null,
      releaseDate: model.release_date ?? null,
      registrationState: model.registration_state, registrationIssues: model.registration_issues,
      aa: aaScores[`${model.creator_id}::${model.model_id}`] ?? null,
      pricing: model.list_pricing ?? model.official_pricing ?? null,
      mediaPricing: model.media_pricing ?? model.official_media_pricing ?? null,
      mediaProfile: model.media_profile ?? model.official_media_profile ?? null,
      lifecycle: model.lifecycle,
    };
    rows.push(row);
  }

  // One row per semantic model: provider namespaces and supply-channel
  // variants (Meituan, Fireworks accounts, FP8 re-registrations) are the
  // same Model rented through different supply paths, not different Models.
  const byIdentity = new Map<string, ModelCatalogRow[]>();
  for (const row of rows) {
    const key = semanticModelKey(row.creatorId, row.modelId);
    const bucket = byIdentity.get(key) ?? [];
    bucket.push(row);
    byIdentity.set(key, bucket);
  }

  const groups = new Map<string, VendorGroup>();
  for (const registrations of byIdentity.values()) {
    const row = collapseRegistrations(registrations, kindOf);
    const vendor = detectVendor(row.creatorId, row.creatorLabel, row.modelId);
    const group = groups.get(vendor.id) ?? {
      vendorId: vendor.id,
      label: vendor.label,
      icon: vendor.icon,
      models: [],
    };
    group.models.push(row);
    groups.set(vendor.id, group);
  }

  const result = [...groups.values()];
  for (const group of result) group.models.sort(compareModels);
  result.sort((left, right) => {
    if ((left.vendorId === 'other') !== (right.vendorId === 'other')) {
      return left.vendorId === 'other' ? 1 : -1;
    }
    const leftBest = left.models[0]?.aa?.intelligence
      ?? left.models[0]?.registeredQualityRank ?? -Infinity;
    const rightBest = right.models[0]?.aa?.intelligence
      ?? right.models[0]?.registeredQualityRank ?? -Infinity;
    return rightBest - leftBest || left.label.localeCompare(right.label);
  });
  return result;
}

/** Models per kind id across every vendor group (after identity collapse). */
export function countModelKinds(groups: readonly VendorGroup[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const group of groups) {
    for (const row of group.models) counts.set(row.kind, (counts.get(row.kind) ?? 0) + 1);
  }
  return counts;
}

/** Keep only the Models of one kind; null keeps every Model. Empty vendors drop. */
export function filterGroupsByKind(groups: VendorGroup[], kind: string | null): VendorGroup[] {
  if (!kind) return groups;
  return groups
    .map((group) => ({ ...group, models: group.models.filter((row) => row.kind === kind) }))
    .filter((group) => group.models.length > 0);
}

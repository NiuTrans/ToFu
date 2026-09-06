/**
 * Pure Model-centric compiler for the Settings catalog.
 *
 * Models are grouped by their Creator using Model facts only. This owner does
 * not read ProviderAccess, Offering, Deployment, wire ids, aliases, or route
 * health; those concepts cannot affect what a Model is or how it is displayed.
 */

import { detectVendor } from './vendor';
import type {
  AaScore,
  ModelCatalogRow,
  ModelPricing,
  ModelCatalogDocument,
  VendorGroup,
} from './types';

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
  return input === null || output === null ? null : (3 * input + output) / 4;
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
): VendorGroup[] {
  if (document.contract_version !== 'tofu.model-routing/v2') return [];
  const creators = new Map(document.creators.map((row) => [row.creator_id, row]));

  const groups = new Map<string, VendorGroup>();
  for (const model of document.models) {
    const creator = creators.get(model.creator_id);
    const creatorLabel = creator?.name || model.creator_id;
    const vendor = detectVendor(model.creator_id, creatorLabel, model.model_id);
    const score = Number(model.quality_rank);
    const row: ModelCatalogRow = {
      creatorId: model.creator_id,
      creatorLabel,
      modelId: model.model_id,
      displayName: model.display_name || model.model_id,
      brand: vendor.icon,
      capabilities: unique(model.capabilities ?? []),
      contextWindow: Number(model.context_window) || 0,
      registeredQualityRank: Number.isFinite(score) && score > 0 ? score : null,
      releaseDate: model.release_date ?? null,
      aa: aaScores[`${model.creator_id}::${model.model_id}`] ?? null,
      pricing: model.list_pricing ?? null,
      lifecycle: model.lifecycle,
    };
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

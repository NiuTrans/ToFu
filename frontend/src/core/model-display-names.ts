/**
 * Model and provider display-name policy.
 *
 * Responsibility: resolve the text users see and provide the one natural,
 * numeric-aware ordering used by model surfaces. Entry point:
 * `createModelDisplayNames`. Dependencies are read-only catalog lookups;
 * mutable catalog state remains with the composition owner.
 */

export interface ModelDisplayNameDependencies {
  lookupModelDisplayName(modelId: string): unknown;
  lookupProviderDisplayName(providerId: string): unknown;
}

export interface ModelDisplayNames {
  modelShortName(modelId: unknown): string;
  compareModelIds(left: unknown, right: unknown): number;
  compareModelsByDisplayName(left: unknown, right: unknown): number;
  sortModelsByDisplayName<T>(models: T[]): T[];
  sortModelEntriesByDisplayName<T extends { model?: unknown }>(entries: T[]): T[];
  sortedBrandKeys(grouped: unknown, brandNames: unknown): string[];
  providerDisplayName(providerId: unknown): string;
}

const MODEL_NAME_COLLATOR = typeof Intl !== 'undefined' && Intl.Collator
  ? new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })
  : null;

function naturalCompare(left: string, right: string): number {
  if (MODEL_NAME_COLLATOR) return MODEL_NAME_COLLATOR.compare(left, right);
  const normalizedLeft = left.toLowerCase();
  const normalizedRight = right.toLowerCase();
  return normalizedLeft < normalizedRight
    ? -1 : normalizedLeft > normalizedRight ? 1 : 0;
}

function modelIdFrom(value: unknown): string {
  if (value && typeof value === 'object' && 'model_id' in value) {
    const modelId = (value as { model_id?: unknown }).model_id;
    return modelId == null ? '' : String(modelId);
  }
  return value == null ? '' : String(value);
}

function recordValue(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object'
    ? value as Record<string, unknown> : {};
}

/** Create an immutable display policy over live catalog lookup ports. */
export function createModelDisplayNames(
  dependencies: ModelDisplayNameDependencies,
): ModelDisplayNames {
  const modelShortName = (value: unknown): string => {
    const modelId = modelIdFrom(value);
    if (!modelId) return 'Model';
    const configuredName = dependencies.lookupModelDisplayName(modelId);
    if (typeof configuredName === 'string' && configuredName) {
      return configuredName;
    }
    return modelId.replace(/^(aws\.|vertex\.)/, '').split('/').pop() || modelId;
  };

  const modelDisplaySortKey = (value: unknown): string => (
    modelShortName(modelIdFrom(value)).replace(/[-_/]+/g, ' ')
  );

  const compareModelIds = (left: unknown, right: unknown): number => (
    naturalCompare(modelIdFrom(left), modelIdFrom(right))
  );

  const compareModelsByDisplayName = (
    left: unknown,
    right: unknown,
  ): number => naturalCompare(
    modelDisplaySortKey(left),
    modelDisplaySortKey(right),
  );

  const sortModelsByDisplayName = <T>(models: T[]): T[] => {
    if (Array.isArray(models)) models.sort(compareModelsByDisplayName);
    return models;
  };

  const sortModelEntriesByDisplayName = <T extends { model?: unknown }>(
    entries: T[],
  ): T[] => {
    if (Array.isArray(entries)) {
      entries.sort((left, right) => compareModelsByDisplayName(
        left?.model,
        right?.model,
      ));
    }
    return entries;
  };

  const sortedBrandKeys = (groupedValue: unknown, namesValue: unknown): string[] => {
    const grouped = recordValue(groupedValue);
    const brandNames = recordValue(namesValue);
    const keys = Object.keys(grouped);
    keys.sort((left, right) => {
      const leftGroup = recordValue(grouped[left]);
      const rightGroup = recordValue(grouped[right]);
      const leftName = brandNames[left] || leftGroup.name || left;
      const rightName = brandNames[right] || rightGroup.name || right;
      return compareModelsByDisplayName(String(leftName), String(rightName));
    });
    return keys;
  };

  const providerDisplayName = (value: unknown): string => {
    const providerId = value == null ? '' : String(value);
    if (!providerId) return '';
    try {
      const configuredName = dependencies.lookupProviderDisplayName(providerId);
      return typeof configuredName === 'string' && configuredName
        ? configuredName : providerId;
    } catch {
      // Route labels are best-effort presentation; catalog access must never
      // prevent the enclosing finish bar from rendering.
      return providerId;
    }
  };

  return Object.freeze({
    modelShortName,
    compareModelIds,
    compareModelsByDisplayName,
    sortModelsByDisplayName,
    sortModelEntriesByDisplayName,
    sortedBrandKeys,
    providerDisplayName,
  });
}

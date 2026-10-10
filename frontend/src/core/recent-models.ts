/**
 * Bounded recent-model persistence.
 *
 * Responsibility: read and record the model picker's most-recent model IDs.
 * Entry point: `createRecentModelsController`.
 * Dependencies: an injected storage resolver; no ambient browser globals.
 */

export const RECENT_MODELS_STORAGE_KEY = 'tofu_recent_models';
export const RECENT_MODELS_MAX = 5;

export interface RecentModelsStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export interface RecentModelsControllerDependencies {
  resolveStorage(): RecentModelsStorage | null;
}

export interface RecentModelsController {
  recentModels(): string[];
  pushRecentModel(modelId: unknown): void;
}

function parseRecentModels(raw: string | null): string[] {
  if (!raw) return [];
  const parsed: unknown = JSON.parse(raw);
  if (!Array.isArray(parsed)) return [];
  return parsed
    .filter((modelId): modelId is string => (
      typeof modelId === 'string' && modelId.length > 0
    ))
    .slice(0, RECENT_MODELS_MAX);
}

/** Create a fail-open controller around a bounded key-value store. */
export function createRecentModelsController(
  dependencies: RecentModelsControllerDependencies,
): RecentModelsController {
  const recentModels = (): string[] => {
    try {
      const storage = dependencies.resolveStorage();
      return storage
        ? parseRecentModels(storage.getItem(RECENT_MODELS_STORAGE_KEY))
        : [];
    } catch {
      return [];
    }
  };

  const pushRecentModel = (modelId: unknown): void => {
    if (typeof modelId !== 'string' || modelId.length === 0) return;
    try {
      const next = recentModels().filter((item) => item !== modelId);
      next.unshift(modelId);
      const storage = dependencies.resolveStorage();
      if (!storage) return;
      storage.setItem(
        RECENT_MODELS_STORAGE_KEY,
        JSON.stringify(next.slice(0, RECENT_MODELS_MAX)),
      );
    } catch {
      // Private mode, disabled storage, and quota failures never block a pick.
    }
  };

  return Object.freeze({ recentModels, pushRecentModel });
}

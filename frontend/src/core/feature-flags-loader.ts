/**
 * Responsibility: validate bounded deployment-flag snapshots, suppress
 * unchanged commits, and single-flight the legacy endpoint fallback.
 * Entry point: createFeatureFlagsLoader. Dependencies: injected current-state,
 * commit, request, and diagnostics ports; owns no DOM, timer, or durable state.
 */

export const FEATURE_FLAGS_LIMITS = Object.freeze({
  maximumFlags: 256,
  maximumKeyLength: 80,
});

export type FeatureFlagsSnapshot = Record<string, boolean>;

export interface FeatureFlagsResponse {
  readonly ok: boolean;
  json(): Promise<unknown>;
}

export interface FeatureFlagsLoaderPorts {
  current(): Readonly<FeatureFlagsSnapshot>;
  commit(flags: FeatureFlagsSnapshot): void;
  request(): Promise<FeatureFlagsResponse>;
  onError?(error: unknown): void;
}

export interface FeatureFlagsLoader {
  load(piggybackFlags?: unknown): Promise<void>;
}

const META_KEYS = new Set(['ok', 'request_id']);
const FLAG_KEY = /^[a-z][a-z0-9_]{0,79}$/;

export function normalizeFeatureFlags(
  value: unknown,
): FeatureFlagsSnapshot | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const entries = Object.entries(value).filter(([key]) => !META_KEYS.has(key));
  if (entries.length > FEATURE_FLAGS_LIMITS.maximumFlags) return null;
  const normalized: FeatureFlagsSnapshot = {};
  for (const [key, flag] of entries) {
    if (!FLAG_KEY.test(key) || typeof flag !== 'boolean') return null;
    normalized[key] = flag;
  }
  if (typeof normalized.debug_mode !== 'boolean'
      || typeof normalized.optimizer_enabled !== 'boolean') return null;
  return normalized;
}

export function createFeatureFlagsLoader(
  ports: FeatureFlagsLoaderPorts,
): FeatureFlagsLoader {
  let fallbackFlight: Promise<void> | null = null;

  const report = (error: unknown): void => {
    try {
      ports.onError?.(error);
    } catch {
      // Optional diagnostics cannot own feature availability.
    }
  };

  const apply = (value: unknown): 'accepted' | 'invalid' | 'failed' => {
    const next = normalizeFeatureFlags(value);
    if (!next) return 'invalid';
    try {
      const current = ports.current();
      const keys = Object.keys(next);
      const unchanged = keys.length === Object.keys(current).length
        && keys.every((key) => current[key] === next[key]);
      if (!unchanged) ports.commit(next);
      return 'accepted';
    } catch (error: unknown) {
      report(error);
      return 'failed';
    }
  };

  const requestFallback = async (): Promise<void> => {
    try {
      const response = await Promise.resolve().then(() => ports.request());
      if (!response.ok) return;
      if (apply(await response.json()) === 'invalid') {
        report(new Error('invalid feature flag payload'));
      }
    } catch (error: unknown) {
      report(error);
    }
  };

  const load = (piggybackFlags?: unknown): Promise<void> => {
    const outcome = apply(piggybackFlags);
    if (outcome !== 'invalid') return Promise.resolve();
    if (fallbackFlight) return fallbackFlight;
    const request = requestFallback();
    fallbackFlight = request;
    const release = (): void => {
      if (fallbackFlight === request) fallbackFlight = null;
    };
    void request.then(release, release);
    return request;
  };

  return Object.freeze({ load });
}

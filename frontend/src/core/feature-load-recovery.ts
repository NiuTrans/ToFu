/**
 * Responsibility: decide when a failed lazy feature import may self-heal via
 * one bounded reload, and carry the pending feature entry across that reload.
 * Entry point: createFeatureLoadRecovery. Dependencies: injected clock,
 * storage, and reload ports.
 *
 * A failed dynamic import is cached by the browser module map for the whole
 * document lifetime, so retrying the same chunk URL cannot succeed — only a
 * reload clears it (the same reason a hard refresh "fixes" the feature
 * load-failed toast). The controller bounds that self-heal: at most one
 * reload per interval, with the pending feature re-invoked once after boot.
 */

export const FEATURE_LOAD_RECOVERY_POLICY = Object.freeze({
  reloadGuardKey: 'tofu:feature-load-reload',
  pendingFeatureKey: 'tofu:feature-load-pending',
  minReloadIntervalMs: 60_000,
  pendingMaxAgeMs: 5 * 60_000,
});

export interface FeatureLoadRecoveryPorts {
  now(): number;
  readValue(key: string): string | null;
  writeValue(key: string, value: string): void;
  removeValue(key: string): void;
  reload(): void;
  onError?(error: unknown): void;
}

export interface FeatureLoadRecovery {
  isModuleLoadFailure(error: unknown): boolean;
  attemptRecovery(feature: string, error: unknown): boolean;
  consumePendingFeature(): string | null;
}

const FEATURE_NAME_PATTERN = /^[A-Za-z_$][\w$]{0,119}$/;
const MODULE_LOAD_FAILURE_PATTERN =
  /dynamically imported module|module script failed/i;

function normalizedFeatureName(value: unknown): string | null {
  if (typeof value !== 'string' || !FEATURE_NAME_PATTERN.test(value)) {
    return null;
  }
  return value;
}

export function createFeatureLoadRecovery(
  ports: FeatureLoadRecoveryPorts,
): FeatureLoadRecovery {
  const reportError = (error: unknown): void => {
    try {
      ports.onError?.(error);
    } catch {
      // Self-healing is best-effort; diagnostics cannot own its state.
    }
  };

  const nowMs = (): number => {
    try {
      const value = ports.now();
      return Number.isFinite(value) ? Math.max(0, value) : 0;
    } catch (error: unknown) {
      reportError(error);
      return 0;
    }
  };

  const isModuleLoadFailure = (error: unknown): boolean => {
    if (!(error instanceof TypeError)) return false;
    const message = typeof error.message === 'string' ? error.message : '';
    return MODULE_LOAD_FAILURE_PATTERN.test(message);
  };

  const attemptRecovery = (feature: string, error: unknown): boolean => {
    const name = normalizedFeatureName(feature);
    if (!name || !isModuleLoadFailure(error)) return false;
    const now = nowMs();
    try {
      const last = Number(ports.readValue(
        FEATURE_LOAD_RECOVERY_POLICY.reloadGuardKey) || 0);
      if (Number.isFinite(last) && last > 0
          && now - last < FEATURE_LOAD_RECOVERY_POLICY.minReloadIntervalMs) {
        return false;
      }
      ports.writeValue(
        FEATURE_LOAD_RECOVERY_POLICY.reloadGuardKey, String(now));
      ports.writeValue(
        FEATURE_LOAD_RECOVERY_POLICY.pendingFeatureKey, name);
    } catch (storageError: unknown) {
      reportError(storageError);
      return false;
    }
    try {
      ports.reload();
    } catch (reloadError: unknown) {
      reportError(reloadError);
      return false;
    }
    return true;
  };

  const consumePendingFeature = (): string | null => {
    let name: string | null = null;
    try {
      name = ports.readValue(FEATURE_LOAD_RECOVERY_POLICY.pendingFeatureKey);
      ports.removeValue(FEATURE_LOAD_RECOVERY_POLICY.pendingFeatureKey);
    } catch (error: unknown) {
      reportError(error);
      return null;
    }
    const pending = normalizedFeatureName(name);
    if (!pending) return null;
    // The marker only makes sense immediately after the reload that wrote it;
    // an ancient marker must not re-open a panel in an unrelated session.
    try {
      const last = Number(ports.readValue(
        FEATURE_LOAD_RECOVERY_POLICY.reloadGuardKey) || 0);
      if (!Number.isFinite(last) || last <= 0
          || nowMs() - last > FEATURE_LOAD_RECOVERY_POLICY.pendingMaxAgeMs) {
        return null;
      }
    } catch (error: unknown) {
      reportError(error);
      return null;
    }
    return pending;
  };

  return Object.freeze({
    isModuleLoadFailure,
    attemptRecovery,
    consumePendingFeature,
  });
}

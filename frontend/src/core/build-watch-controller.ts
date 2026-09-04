/**
 * Responsibility: react to served frontend build identities carried by the
 * existing push liveness channel and decide when a safe reload may occur.
 * Entry point: createBuildWatchController. Dependencies: injected build,
 * busy-state, session-guard, notice, reload, clock, and subscription ports.
 */

export const BUILD_WATCH_POLICY = Object.freeze({
  maxBusyDeferMs: 30 * 60 * 1_000,
  reloadGuardKey: 'tofu:build-watch-reload',
});

export interface BuildWatchControllerPorts {
  subscribeBuildId(listener: (buildId: string) => void): () => void;
  loadedBuildId(): string | null;
  isBusy(): boolean;
  now(): number;
  readReloadGuard(key: string): string | null;
  writeReloadGuard(key: string, buildId: string): void;
  showPendingNotice(buildId: string): void;
  reload(): void;
  onError?(error: unknown): void;
}

export interface BuildWatchControllerSnapshot {
  readonly started: boolean;
  readonly pendingBuildId: string | null;
  readonly pendingSinceMs: number | null;
}

export interface BuildWatchController {
  start(): void;
  observe(buildId: string): void;
  destroy(): void;
  snapshot(): BuildWatchControllerSnapshot;
}

function normalizedBuildId(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 180) return null;
  return /^main-[A-Za-z0-9_-]+\.js$/.test(value) ? value : null;
}

export function createBuildWatchController(
  ports: BuildWatchControllerPorts,
): BuildWatchController {
  let started = false;
  let destroyed = false;
  let unsubscribe: (() => void) | null = null;
  let pendingBuildId: string | null = null;
  let pendingSinceMs: number | null = null;
  let noticedBuildId: string | null = null;

  const reportError = (error: unknown): void => {
    try {
      ports.onError?.(error);
    } catch {
      // Build self-healing is best-effort; diagnostics cannot own its state.
    }
  };

  const clearPending = (): void => {
    pendingBuildId = null;
    pendingSinceMs = null;
  };

  const readReloadGuard = (): string | null => {
    try {
      return ports.readReloadGuard(BUILD_WATCH_POLICY.reloadGuardKey);
    } catch (error: unknown) {
      reportError(error);
      return null;
    }
  };

  const observe = (candidateBuildId: string): void => {
    if (destroyed) return;
    const servedBuildId = normalizedBuildId(candidateBuildId);
    let loadedBuildId: string | null = null;
    try {
      loadedBuildId = normalizedBuildId(ports.loadedBuildId());
    } catch (error: unknown) {
      reportError(error);
    }
    if (!servedBuildId || !loadedBuildId) return;
    if (servedBuildId === loadedBuildId) {
      clearPending();
      return;
    }
    if (readReloadGuard() === servedBuildId) {
      clearPending();
      return;
    }

    let nowMs = 0;
    try {
      const value = ports.now();
      nowMs = Number.isFinite(value) ? Math.max(0, value) : 0;
    } catch (error: unknown) {
      reportError(error);
    }
    if (pendingBuildId !== servedBuildId || pendingSinceMs === null) {
      pendingBuildId = servedBuildId;
      pendingSinceMs = nowMs;
    }

    let busy = true;
    try {
      busy = ports.isBusy();
    } catch (error: unknown) {
      reportError(error);
    }
    if (busy && nowMs - pendingSinceMs < BUILD_WATCH_POLICY.maxBusyDeferMs) {
      if (noticedBuildId !== servedBuildId) {
        noticedBuildId = servedBuildId;
        try {
          ports.showPendingNotice(servedBuildId);
        } catch (error: unknown) {
          reportError(error);
        }
      }
      return;
    }

    try {
      ports.writeReloadGuard(
        BUILD_WATCH_POLICY.reloadGuardKey,
        servedBuildId,
      );
    } catch (error: unknown) {
      reportError(error);
    }
    clearPending();
    try {
      ports.reload();
    } catch (error: unknown) {
      reportError(error);
    }
  };

  const start = (): void => {
    if (destroyed || started) return;
    started = true;
    try {
      const remove = ports.subscribeBuildId(observe);
      unsubscribe = typeof remove === 'function' ? remove : null;
    } catch (error: unknown) {
      reportError(error);
    }
  };

  const destroy = (): void => {
    if (destroyed) return;
    destroyed = true;
    clearPending();
    if (!unsubscribe) return;
    try {
      unsubscribe();
    } catch (error: unknown) {
      reportError(error);
    }
    unsubscribe = null;
  };

  const snapshot = (): BuildWatchControllerSnapshot => Object.freeze({
    started,
    pendingBuildId,
    pendingSinceMs,
  });

  return Object.freeze({ start, observe, destroy, snapshot });
}

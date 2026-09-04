/**
 * Responsibility: own the client-log relay's demand-scoped flush clock.
 * Entry point: createClientLogFlushScheduler. Dependencies: injected pending,
 * timer, visibility, randomness, flush, and diagnostic ports only. The owner
 * never reads browser globals or decides log retention/transport policy.
 */

export const CLIENT_LOG_FLUSH_SCHEDULER_LIMITS = Object.freeze({
  maximumDelayMs: 300_000,
  fallbackDelayMs: 15_000,
  minimumJitterRatio: 0.85,
  jitterRatioWidth: 0.30,
});

export interface ClientLogProfileDocument {
  getElementById(id: string): { readonly textContent?: string | null } | null;
}

export interface ClientLogProfileLocation {
  readonly pathname?: string;
}

export function clientLogFlushBaseDelayMs(
  documentPort: ClientLogProfileDocument,
  locationPort: ClientLogProfileLocation,
): number {
  try {
    const tag = documentPort.getElementById('tofu-boot-config');
    const config = tag?.textContent ? JSON.parse(tag.textContent) : null;
    if (config?.transportProfile === 'constrained-proxy') return 60_000;
    if (config?.transportProfile === 'direct') return 15_000;
  } catch {
    // A malformed optional boot hint falls back to the served path.
  }
  try {
    return /\/(?:proxy|absproxy)\/\d+(?:\/|$)/.test(
      String(locationPort.pathname ?? ''),
    ) ? 60_000 : 15_000;
  } catch {
    return 15_000;
  }
}

export interface ClientLogFlushSchedule {
  setTimeout(callback: () => void, delayMs: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface ClientLogFlushVisibility {
  readonly hidden: boolean;
  addEventListener(type: 'visibilitychange', listener: () => void): void;
  removeEventListener(type: 'visibilitychange', listener: () => void): void;
}

export interface ClientLogFlushSchedulerPorts {
  readonly baseDelayMs: number;
  readonly schedule: ClientLogFlushSchedule;
  readonly visibility: ClientLogFlushVisibility;
  hasPending(): boolean;
  random(): number;
  flush(): unknown;
  onError?(error: unknown): void;
}

export interface ClientLogFlushSchedulerSnapshot {
  readonly destroyed: boolean;
  readonly flushRunning: boolean;
  readonly timerScheduled: boolean;
  readonly visibilitySubscribed: boolean;
}

export interface ClientLogFlushScheduler {
  demand(): void;
  flushNow(): void;
  destroy(): void;
  snapshot(): ClientLogFlushSchedulerSnapshot;
}

export function createClientLogFlushScheduler(
  ports: ClientLogFlushSchedulerPorts,
): ClientLogFlushScheduler {
  let destroyed = false;
  let flushRunning = false;
  let timerScheduled = false;
  let timerHandle: unknown = null;
  let visibilitySubscribed = false;

  const reportError = (error: unknown): void => {
    try {
      ports.onError?.(error);
    } catch {
      // Best-effort diagnostics cannot own the relay clock's lifecycle.
    }
  };

  const hasPending = (): boolean => {
    try {
      return ports.hasPending() === true;
    } catch (error: unknown) {
      reportError(error);
      return false;
    }
  };

  const isHidden = (): boolean => {
    try {
      return ports.visibility.hidden === true;
    } catch (error: unknown) {
      reportError(error);
      return false;
    }
  };

  const clearTimer = (): void => {
    if (timerScheduled) {
      try {
        ports.schedule.clearTimeout(timerHandle);
      } catch (error: unknown) {
        reportError(error);
      }
    }
    timerScheduled = false;
    timerHandle = null;
  };

  let onVisibilityChange: () => void;

  const removeVisibilityListener = (): void => {
    if (!visibilitySubscribed) return;
    try {
      ports.visibility.removeEventListener(
        'visibilitychange',
        onVisibilityChange,
      );
    } catch (error: unknown) {
      reportError(error);
    }
    visibilitySubscribed = false;
  };

  const releaseDemandResources = (): void => {
    clearTimer();
    removeVisibilityListener();
  };

  const ensureVisibilityListener = (): void => {
    if (visibilitySubscribed) return;
    try {
      ports.visibility.addEventListener('visibilitychange', onVisibilityChange);
      visibilitySubscribed = true;
    } catch (error: unknown) {
      reportError(error);
    }
  };

  const nextDelayMs = (): number => {
    let baseDelayMs = Number(ports.baseDelayMs);
    if (!Number.isFinite(baseDelayMs) || baseDelayMs < 0) {
      baseDelayMs = CLIENT_LOG_FLUSH_SCHEDULER_LIMITS.fallbackDelayMs;
    }
    baseDelayMs = Math.min(
      baseDelayMs,
      CLIENT_LOG_FLUSH_SCHEDULER_LIMITS.maximumDelayMs,
    );
    let randomValue = 0.5;
    try {
      const candidate = Number(ports.random());
      if (Number.isFinite(candidate)) {
        randomValue = Math.min(1, Math.max(0, candidate));
      }
    } catch (error: unknown) {
      reportError(error);
    }
    const ratio = CLIENT_LOG_FLUSH_SCHEDULER_LIMITS.minimumJitterRatio
      + randomValue * CLIENT_LOG_FLUSH_SCHEDULER_LIMITS.jitterRatioWidth;
    return Math.round(baseDelayMs * ratio);
  };

  let demand: () => void;

  const finishFlush = (): void => {
    if (destroyed) return;
    flushRunning = false;
    demand();
  };

  const runFlush = (): void => {
    if (destroyed || flushRunning) return;
    if (!hasPending()) {
      releaseDemandResources();
      return;
    }
    clearTimer();
    removeVisibilityListener();
    flushRunning = true;
    let outcome: unknown;
    try {
      outcome = ports.flush();
    } catch (error: unknown) {
      reportError(error);
    }
    void Promise.resolve(outcome).catch(reportError).then(finishFlush);
  };

  const scheduleFlush = (): void => {
    if (destroyed || flushRunning || timerScheduled) return;
    if (!hasPending()) {
      releaseDemandResources();
      return;
    }
    ensureVisibilityListener();
    if (isHidden()) {
      clearTimer();
      return;
    }
    timerScheduled = true;
    try {
      timerHandle = ports.schedule.setTimeout(() => {
        timerScheduled = false;
        timerHandle = null;
        if (isHidden()) {
          demand();
          return;
        }
        runFlush();
      }, nextDelayMs());
    } catch (error: unknown) {
      timerScheduled = false;
      timerHandle = null;
      reportError(error);
      removeVisibilityListener();
    }
  };

  demand = (): void => {
    if (destroyed || flushRunning) return;
    if (!hasPending()) {
      releaseDemandResources();
      return;
    }
    scheduleFlush();
  };

  onVisibilityChange = (): void => {
    if (destroyed || flushRunning) return;
    if (!hasPending()) {
      releaseDemandResources();
      return;
    }
    if (isHidden()) clearTimer();
    else scheduleFlush();
  };

  const flushNow = (): void => {
    runFlush();
  };

  const destroy = (): void => {
    if (destroyed) return;
    destroyed = true;
    flushRunning = false;
    releaseDemandResources();
  };

  const snapshot = (): ClientLogFlushSchedulerSnapshot => Object.freeze({
    destroyed,
    flushRunning,
    timerScheduled,
    visibilitySubscribed,
  });

  return Object.freeze({ demand, flushNow, destroy, snapshot });
}

/**
 * Responsibility: own the demand-scoped, visibility-aware Swarm recovery
 * clock and its single in-flight reconciliation request.
 * Entry point: createSwarmReconciliationScheduler. Dependencies: injected
 * clock, visibility, reconciliation, pause/resume, and logging ports only.
 */

export const SWARM_RECONCILIATION_POLICY = Object.freeze({
  intervalMs: 20_000,
  fastMs: 800,
});

export type SwarmReconciliationDelay = number | null;
export type SwarmReconciliationTimerCallback = () => void | Promise<void>;

export interface SwarmReconciliationSchedule {
  now(): number;
  setTimeout(
    callback: SwarmReconciliationTimerCallback,
    delayMs: number,
  ): unknown;
  clearTimeout(handle: unknown): void;
}

export interface SwarmReconciliationVisibility {
  isHidden(): boolean;
  subscribe(listener: () => void): () => void;
}

export interface SwarmReconciliationSchedulerPorts {
  readonly schedule: SwarmReconciliationSchedule;
  readonly visibility: SwarmReconciliationVisibility;
  reconcile(): Promise<SwarmReconciliationDelay>;
  onHidden?(): void;
  onVisible?(): void;
  onError?(error: unknown): void;
}

export interface SwarmReconciliationSchedulerSnapshot {
  readonly demanded: boolean;
  readonly running: boolean;
  readonly timerScheduled: boolean;
  readonly visibilitySubscribed: boolean;
  readonly dueAt: number;
}

export interface SwarmReconciliationScheduler {
  demand(delayMs?: number): void;
  reconcileNow(): Promise<SwarmReconciliationDelay>;
  stop(): void;
  snapshot(): SwarmReconciliationSchedulerSnapshot;
}

function normalizedDelay(value: unknown, fallback: number): number {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? Math.max(0, numeric) : fallback;
}

function normalizedFollowup(value: unknown): SwarmReconciliationDelay {
  if (value === null) return null;
  const numeric = Number(value);
  return Number.isFinite(numeric) ? Math.max(0, numeric) : null;
}

export function createSwarmReconciliationScheduler(
  ports: SwarmReconciliationSchedulerPorts,
): SwarmReconciliationScheduler {
  let demanded = false;
  let running = false;
  let rerunRequested = false;
  let timerScheduled = false;
  let timerHandle: unknown = null;
  let dueAt = 0;
  let removeVisibilityListener: (() => void) | null = null;
  let inFlight: Promise<SwarmReconciliationDelay> | null = null;

  const reportError = (error: unknown): void => {
    try {
      ports.onError?.(error);
    } catch {
      // Best-effort recovery logging must never break the scheduler.
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
    dueAt = 0;
  };

  const stop = (): void => {
    demanded = false;
    rerunRequested = false;
    clearTimer();
    if (removeVisibilityListener) {
      try {
        removeVisibilityListener();
      } catch (error: unknown) {
        reportError(error);
      }
      removeVisibilityListener = null;
    }
  };

  const reconcileNow = (): Promise<SwarmReconciliationDelay> => {
    if (inFlight) return inFlight;
    const request = Promise.resolve().then(() => ports.reconcile());
    inFlight = request.then(
      (delayMs) => {
        inFlight = null;
        return normalizedFollowup(delayMs);
      },
      (error: unknown) => {
        inFlight = null;
        throw error;
      },
    );
    return inFlight;
  };

  let scheduleTimer: (delayMs: number) => void;

  const runCycle = async (): Promise<void> => {
    if (!demanded || ports.visibility.isHidden()) return;
    running = true;
    rerunRequested = false;
    let nextDelayMs: SwarmReconciliationDelay;
    try {
      nextDelayMs = await reconcileNow();
    } catch (error: unknown) {
      reportError(error);
      nextDelayMs = SWARM_RECONCILIATION_POLICY.intervalMs;
    } finally {
      running = false;
    }
    if (!demanded || ports.visibility.isHidden()) return;
    if (rerunRequested) {
      rerunRequested = false;
      scheduleTimer(0);
    } else if (nextDelayMs === null) {
      stop();
    } else {
      scheduleTimer(nextDelayMs);
    }
  };

  scheduleTimer = (delayMs: number): void => {
    if (!demanded || ports.visibility.isHidden()) return;
    const delay = normalizedDelay(delayMs, 0);
    const candidateDueAt = ports.schedule.now() + delay;
    if (timerScheduled) {
      if (dueAt <= candidateDueAt) return;
      clearTimer();
    }
    dueAt = candidateDueAt;
    timerScheduled = true;
    timerHandle = ports.schedule.setTimeout(async () => {
      timerScheduled = false;
      timerHandle = null;
      dueAt = 0;
      await runCycle();
    }, delay);
  };

  const onVisibilityChange = (): void => {
    if (ports.visibility.isHidden()) {
      clearTimer();
      try {
        ports.onHidden?.();
      } catch (error: unknown) {
        reportError(error);
      }
      return;
    }
    try {
      ports.onVisible?.();
    } catch (error: unknown) {
      reportError(error);
    }
    if (demanded) scheduleTimer(0);
  };

  const ensureVisibilitySubscription = (): void => {
    if (removeVisibilityListener) return;
    try {
      removeVisibilityListener = ports.visibility.subscribe(
        onVisibilityChange,
      );
    } catch (error: unknown) {
      reportError(error);
    }
  };

  const demand = (
    delayMs = SWARM_RECONCILIATION_POLICY.intervalMs,
  ): void => {
    demanded = true;
    ensureVisibilitySubscription();
    if (ports.visibility.isHidden()) {
      clearTimer();
      try {
        ports.onHidden?.();
      } catch (error: unknown) {
        reportError(error);
      }
      return;
    }
    if (running) {
      rerunRequested = true;
      return;
    }
    scheduleTimer(normalizedDelay(
      delayMs, SWARM_RECONCILIATION_POLICY.intervalMs,
    ));
  };

  const snapshot = (): SwarmReconciliationSchedulerSnapshot => Object.freeze({
    demanded,
    running,
    timerScheduled,
    visibilitySubscribed: removeVisibilityListener !== null,
    dueAt,
  });

  return Object.freeze({ demand, reconcileNow, stop, snapshot });
}

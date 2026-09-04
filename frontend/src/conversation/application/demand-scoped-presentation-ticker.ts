/**
 * Responsibility: own one demand-scoped, visibility-aware presentation tick.
 * Entry point: createDemandScopedPresentationTicker. Dependencies: injected
 * timer, visibility, synchronous tick, and error-reporting ports only.
 */

export const PRESENTATION_TICKER_POLICY = Object.freeze({
  intervalMs: 1_000,
});

export type PresentationTickerCallback = () => void | Promise<void>;

export interface PresentationTickerSchedule {
  setTimeout(callback: PresentationTickerCallback, delayMs: number): unknown;
  clearTimeout(handle: unknown): void;
}

export interface PresentationTickerVisibility {
  isHidden(): boolean;
  subscribe(listener: () => void): () => void;
}

export interface DemandScopedPresentationTickerPorts {
  readonly schedule: PresentationTickerSchedule;
  readonly visibility: PresentationTickerVisibility;
  tick(): boolean;
  onError?(error: unknown): void;
}

export interface DemandScopedPresentationTickerSnapshot {
  readonly demanded: boolean;
  readonly timerScheduled: boolean;
  readonly visibilitySubscribed: boolean;
}

export interface DemandScopedPresentationTicker {
  demand(): void;
  stop(): void;
  snapshot(): DemandScopedPresentationTickerSnapshot;
}

export function createDemandScopedPresentationTicker(
  ports: DemandScopedPresentationTickerPorts,
): DemandScopedPresentationTicker {
  let demanded = false;
  let timerScheduled = false;
  let timerHandle: unknown = null;
  let removeVisibilityListener: (() => void) | null = null;

  const reportError = (error: unknown): void => {
    try {
      ports.onError?.(error);
    } catch {
      // Presentation timing is best-effort; logging cannot own its lifecycle.
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

  const stop = (): void => {
    demanded = false;
    clearTimer();
    if (!removeVisibilityListener) return;
    try {
      removeVisibilityListener();
    } catch (error: unknown) {
      reportError(error);
    }
    removeVisibilityListener = null;
  };

  let scheduleTick: (delayMs: number) => void;

  const runTick = (): void => {
    if (!demanded || ports.visibility.isHidden()) return;
    let keepTicking = false;
    try {
      keepTicking = ports.tick() === true;
    } catch (error: unknown) {
      reportError(error);
    }
    if (keepTicking) scheduleTick(PRESENTATION_TICKER_POLICY.intervalMs);
    else stop();
  };

  scheduleTick = (delayMs: number): void => {
    if (!demanded || timerScheduled || ports.visibility.isHidden()) return;
    timerScheduled = true;
    timerHandle = ports.schedule.setTimeout(() => {
      timerScheduled = false;
      timerHandle = null;
      runTick();
    }, delayMs);
  };

  const onVisibilityChange = (): void => {
    if (ports.visibility.isHidden()) {
      clearTimer();
      return;
    }
    if (demanded) scheduleTick(0);
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

  const demand = (): void => {
    demanded = true;
    ensureVisibilitySubscription();
    if (ports.visibility.isHidden()) {
      clearTimer();
      return;
    }
    scheduleTick(PRESENTATION_TICKER_POLICY.intervalMs);
  };

  const snapshot = (): DemandScopedPresentationTickerSnapshot => Object.freeze({
    demanded,
    timerScheduled,
    visibilitySubscribed: removeVisibilityListener !== null,
  });

  return Object.freeze({ demand, stop, snapshot });
}

/** Single timer/request owner for the non-authoritative activity snapshot.
 * Lifecycle hints coalesce; at most one trailing read survives an in-flight
 * hint. Failure keeps the last good view and backoff; disposal fences responses.
 */
export interface BackgroundActivityControllerPorts {
  read(): Promise<unknown>;
  apply(snapshot: unknown): boolean;
  hidden(): boolean;
}

const ACTIVE_DELAY_MS = 3000;
const IDLE_DELAY_MS = 10000;
const HINT_DELAY_MS = 250;

export function createBackgroundActivityController(ports: BackgroundActivityControllerPorts) {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let inFlight = false;
  let dirty = false;
  let disposed = false;
  let failed = false;

  let active = false;

  function clearTimer(): void {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  }

  function schedule(delay: number): void {
    clearTimer();
    if (disposed || ports.hidden() || inFlight) return;
    timer = setTimeout(() => { timer = null; void tick(); }, delay);
  }

  async function tick(): Promise<void> {
    if (disposed || ports.hidden() || inFlight) return;
    inFlight = true;
    dirty = false;
    let received = false;
    try {
      const snapshot = await ports.read();
      if (!disposed && snapshot && typeof snapshot === 'object') {
        active = ports.apply(snapshot);
        received = true;
      }
    } catch { /* Retain the last good snapshot; resume via bounded retry. */ }
    finally {
      inFlight = false;
      failed = !received;
      if (!disposed && !ports.hidden()) {
        schedule(failed ? IDLE_DELAY_MS
          : dirty ? HINT_DELAY_MS
            : active ? ACTIVE_DELAY_MS : IDLE_DELAY_MS);
      }
    }
  }

  function wake(immediate = false): void {
    if (disposed || ports.hidden()) return;
    if (inFlight) { dirty = true; return; }
    // Routine hints must not bypass failure backoff; visibility/switch may retry.
    if (failed && timer !== null && !immediate) return;
    if (!immediate && timer !== null && dirty) return;
    dirty = true;
    schedule(immediate ? 0 : HINT_DELAY_MS);
  }

  return {
    refresh: wake,
    visibilityChanged(): void {
      clearTimer();
      if (!ports.hidden()) wake(true);
    },
    dispose(): void { disposed = true; dirty = false; clearTimer(); },
  };
}

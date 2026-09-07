export type Cleanup = () => void;

export interface LifecycleScope {
  readonly signal: AbortSignal;
  listen(
    target: EventTarget,
    type: string,
    listener: EventListenerOrEventListenerObject,
    options?: AddEventListenerOptions,
  ): void;
  interval(callback: () => void, delayMs: number): number;
  timeout(callback: () => void, delayMs: number): number;
  add(cleanup: Cleanup): void;
  destroy(): void;
}

/** Own every resource created by one panel/conversation attachment. */
export function createLifecycleScope(): LifecycleScope {
  const controller = new AbortController();
  const cleanups = new Set<Cleanup>();
  let destroyed = false;

  const add = (cleanup: Cleanup): void => {
    if (destroyed) {
      cleanup();
      return;
    }
    cleanups.add(cleanup);
  };

  return {
    signal: controller.signal,
    listen(target, type, listener, options = {}) {
      target.addEventListener(type, listener, { ...options, signal: controller.signal });
    },
    interval(callback, delayMs) {
      const id = window.setInterval(callback, delayMs);
      add(() => window.clearInterval(id));
      return id;
    },
    timeout(callback, delayMs) {
      const id = window.setTimeout(callback, delayMs);
      add(() => window.clearTimeout(id));
      return id;
    },
    add,
    destroy() {
      if (destroyed) return;
      destroyed = true;
      controller.abort();
      for (const cleanup of [...cleanups].reverse()) {
        try {
          cleanup();
        } catch (error: unknown) {
          // A faulty optional panel must not prevent timers, listeners and
          // later cleanup callbacks owned by the same scope from releasing.
          console.warn('[lifecycle] cleanup failed', error);
        }
      }
      cleanups.clear();
    },
  };
}

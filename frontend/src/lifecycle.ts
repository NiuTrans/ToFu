/** Browser resource scope. Owns listeners and cancellable scheduled work; completed work releases its retained record before callbacks run. */
export type Cleanup = () => void;
export interface LifecycleScope {
  readonly signal: AbortSignal;
  readonly pendingTasks: Readonly<{ timeouts: number; intervals: number; frames: number }>;
  listen(target: EventTarget, type: string, listener: EventListenerOrEventListenerObject, options?: AddEventListenerOptions): void;
  interval(callback: () => void, delayMs: number): number;
  timeout(callback: () => void, delayMs: number): number;
  frame(callback: FrameRequestCallback): number;
  cancelTimeout(id: number): void;
  cancelInterval(id: number): void;
  cancelFrame(id: number): void;
  add(cleanup: Cleanup): void;
  destroy(): void;
}
export function createLifecycleScope(ownerWindow: Window = window): LifecycleScope {
  // A supplied DOM owns its AbortSignal realm (important for embedded documents).
  const Abort = ownerWindow.document?.defaultView?.AbortController ?? AbortController;
  const controller = new Abort();
  const cleanups = new Set<Cleanup>();
  const timeouts = new Set<number>(), intervals = new Set<number>(), frames = new Set<number>();
  let destroyed = false;
  function cancelTimeout(id: number) { if (timeouts.delete(id)) ownerWindow.clearTimeout(id); }
  function cancelInterval(id: number) { if (intervals.delete(id)) ownerWindow.clearInterval(id); }
  function cancelFrame(id: number) { if (frames.delete(id)) ownerWindow.cancelAnimationFrame(id); }
  return {
    signal: controller.signal,
    get pendingTasks() { return { timeouts: timeouts.size, intervals: intervals.size, frames: frames.size }; },
    listen(target, type, listener, options = {}) {
      if (!destroyed) target.addEventListener(type, listener, { ...options, signal: controller.signal });
    },
    interval(callback, delayMs) {
      if (destroyed) return 0;
      const id = ownerWindow.setInterval(() => { if (!destroyed && intervals.has(id)) callback(); }, delayMs);
      intervals.add(id); return id;
    },
    timeout(callback, delayMs) {
      if (destroyed) return 0;
      const id = ownerWindow.setTimeout(() => {
        if (timeouts.delete(id) && !destroyed) callback();
      }, delayMs);
      timeouts.add(id); return id;
    },
    frame(callback) {
      if (destroyed) return 0;
      const id = ownerWindow.requestAnimationFrame(time => {
        if (frames.delete(id) && !destroyed) callback(time);
      });
      frames.add(id); return id;
    },
    cancelTimeout, cancelInterval, cancelFrame,
    add(cleanup) { if (destroyed) cleanup(); else cleanups.add(cleanup); },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      try { controller.abort(); } catch (error) { console.warn('[lifecycle] listener cleanup failed', error); }
      const release = (ids: Set<number>, cancel: (id: number) => void) => {
        for (const id of ids) try { cancel(id); } catch (error) { console.warn('[lifecycle] task cleanup failed', error); }
      };
      release(timeouts, id => ownerWindow.clearTimeout(id));
      release(intervals, id => ownerWindow.clearInterval(id));
      release(frames, id => ownerWindow.cancelAnimationFrame(id));
      timeouts.clear(); intervals.clear(); frames.clear();
      for (const cleanup of [...cleanups].reverse()) {
        try { cleanup(); }
        catch (error: unknown) { console.warn('[lifecycle] cleanup failed', error); }
      }
      cleanups.clear();
    },
  };
}

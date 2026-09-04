/** Browser clock adapter for coalescing many ordered events into one paint. */
import type { ConversationRenderScheduler } from '../application/conversation-session';

export interface AnimationFrameHost {
  requestAnimationFrame?: (callback: FrameRequestCallback) => number;
  cancelAnimationFrame?: (handle: number) => void;
  setTimeout(callback: () => void, delay: number): number;
  clearTimeout(handle: number): void;
}

/**
 * Schedule paint work without assuming a full visual browser. Embedded
 * webviews, test shells, and background documents may omit animation frames;
 * a short timer preserves correctness while native frames remain preferred.
 */
export function scheduleAnimationFrame(
  host: AnimationFrameHost,
  callback: FrameRequestCallback,
): () => void {
  try {
    const requestFrame = host.requestAnimationFrame;
    if (typeof requestFrame === 'function') {
      const handle = requestFrame.call(host, callback);
      return () => {
        try {
          const cancelFrame = host.cancelAnimationFrame;
          if (typeof cancelFrame === 'function') cancelFrame.call(host, handle);
        } catch {
          // Resolution and cancellation are best-effort in restricted hosts.
        }
      };
    }
  } catch {
    // Capability resolution and invocation can both fail in embedded hosts.
  }
  const handle = host.setTimeout(() => callback(Date.now()), 16);
  return () => {
    try {
      host.clearTimeout(handle);
    } catch {
      // Timer cancellation is also best-effort during host teardown.
    }
  };
}

export function createAnimationFrameScheduler(
  host: AnimationFrameHost,
): ConversationRenderScheduler {
  return {
    schedule(render) {
      return scheduleAnimationFrame(host, render);
    },
  };
}

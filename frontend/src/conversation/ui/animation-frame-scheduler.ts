/** Browser clock adapter for coalescing many ordered events into one paint. */
import type { ConversationRenderScheduler } from '../application/conversation-session';

export function createAnimationFrameScheduler(
  host: Pick<Window, 'requestAnimationFrame' | 'cancelAnimationFrame'>,
): ConversationRenderScheduler {
  return {
    schedule(render) {
      const handle = host.requestAnimationFrame(render);
      return () => host.cancelAnimationFrame(handle);
    },
  };
}

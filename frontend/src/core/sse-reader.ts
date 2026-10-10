/**
 * Responsibility: decode a fetch response body and deliver complete
 * newline-delimited SSE transport lines in arrival order.
 * Entry point: readSSEStream. Dependencies: browser Response/TextDecoder;
 * event interpretation and lifecycle policy are injected callbacks.
 */

import { SSELineFramer } from './sse-line-framer';

export interface SseReadOptions {
  readonly signal?: AbortSignal;
  readonly onLine: (line: string) => unknown;
  readonly onChunk?: () => void;
  readonly afterChunk?: () => void;
  readonly flushTail?: boolean;
}

/**
 * Read one response stream until EOF or until `onLine` signals completion.
 * The default tail policy delivers a final non-blank unterminated line.
 */
export async function readSSEStream(
  response: Response,
  options: SseReadOptions,
): Promise<boolean> {
  const reader = response.body?.getReader();
  if (!reader) throw new Error('SSE response body is unavailable');

  const framer = new SSELineFramer();
  const signal = options.signal;
  let ended = false;
  const cancel = () => {
    // Cancellation can be supplied by an arbitrary stream source. A source
    // that never settles its cancel promise must not retain our reader lock.
    void reader.cancel(signal?.reason).catch(() => {});
  };
  signal?.addEventListener('abort', cancel, { once: true });
  try {
    signal?.throwIfAborted();
    while (true) {
      const { done, value } = await reader.read();
      signal?.throwIfAborted();
      if (done) {
        ended = true;
        const tail = framer.finish();
        if (options.flushTail !== false && tail.trim() && options.onLine(tail)) return true;
        return false;
      }
      options.onChunk?.();
      for (const line of framer.feed(value)) {
        if (options.onLine(line)) return true;
      }
      options.afterChunk?.();
    }
  } finally {
    signal?.removeEventListener('abort', cancel);
    if (!ended) cancel();
    reader.releaseLock();
  }
}

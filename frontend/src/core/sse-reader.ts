/**
 * Responsibility: decode a fetch response body and deliver complete
 * newline-delimited SSE transport lines in arrival order.
 * Entry point: readSSEStream. Dependencies: browser Response/TextDecoder;
 * event interpretation and lifecycle policy are injected callbacks.
 */

export interface SseReadOptions {
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

  const flushTail = options.flushTail !== false;
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      if (flushTail && buffer.trim()) {
        for (const line of buffer.split('\n')) {
          if (options.onLine(line)) return true;
        }
      }
      return false;
    }
    options.onChunk?.();
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';
    for (const line of lines) {
      if (options.onLine(line)) return true;
    }
    options.afterChunk?.();
  }
}

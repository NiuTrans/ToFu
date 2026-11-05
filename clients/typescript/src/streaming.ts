/** SSE object events with bounded UTF-8 framing; owns reader and response release until exit. */
import { SDK_SSE_OVERFLOW_STATUS, SDK_SSE_OVERFLOW_CODE } from './api-v4.generated.js';
import { SSELineFramer, SSELimitError } from './sse-framer.js';
import { TofuError, parseSequence, taskTerminal } from './protocol.js';
import { releaseResponse } from './transport.js';

export async function* parseSSE(response: Response): AsyncIterable<Record<string, unknown>> {
  if (!response.body) {
    releaseResponse(response);
    throw new Error('streaming requires a Response.body');
  }
  let reader: ReadableStreamDefaultReader<Uint8Array>;
  try { reader = response.body.getReader(); }
  catch (error) { releaseResponse(response); throw error; }
  const framer = new SSELineFramer();
  let eventName = '';
  let eventId: number | undefined;
  let dataLines: string[] = [];
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const line of framer.feed(value)) {
        if (line.startsWith(':')) continue;
        if (line.startsWith('event:')) { eventName = line.slice(6).trim(); continue; }
        if (line.startsWith('id:')) {
          eventId = parseSequence(line.slice(3).trim());
          continue;
        }
        if (line.startsWith('data:')) { dataLines.push(line.slice(5).trim()); continue; }
        if (line !== '') continue;
        const data = dataLines.join('\n');
        const frameName = eventName;
        const frameId = eventId;
        eventName = '';
        eventId = undefined;
        dataLines = [];
        if (!data) continue;
        if (data === '[DONE]') return;
        let payload: unknown;
        try { payload = JSON.parse(data); } catch { continue; }
        if (!payload || typeof payload !== 'object' || Array.isArray(payload)) continue;
        const event = payload as Record<string, unknown>;
        if (frameName && event.event === undefined) event.event = frameName;
        if (frameId !== undefined && event.seq === undefined) event.seq = frameId;
        yield event;
        if (taskTerminal(event) || event.event === 'message_stop') return;
      }
    }
  } catch (error) {
    if (error instanceof SSELimitError) {
      throw new TofuError(SDK_SSE_OVERFLOW_STATUS, { error: {
        kind: SDK_SSE_OVERFLOW_CODE, message: error.message, source: 'sdk',
      } });
    }
    throw error;
  } finally {
    void reader.cancel().catch(() => {});
    reader.releaseLock();
    releaseResponse(response);
  }
}

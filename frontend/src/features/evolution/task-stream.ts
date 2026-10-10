/** Live tool-activity feed for one headless evolution task.
 *
 * One EventSource on the task SSE replay endpoint (`/api/v4/tasks/{id}/stream`)
 * with cursor resume and bounded exponential-backoff reconnect. The studio's
 * polling loop stays the status authority; this stream only decorates the
 * office actors, so every failure degrades quietly back to polling. Transport,
 * timers and parsing stay injectable so the contract is testable without a DOM.
 */
import { resolvePath } from '../../api/transport';

export type ToolActivity = { tool: string; query: string; toolCallId: string };
export type ToolActivityHandlers = {
  toolStart?: (activity: ToolActivity) => void;
  toolEnd?: (toolCallId: string) => void;
  done?: (status: string) => void;
};
export type TaskActivityStream = { close: () => void };
/** Structural subset of EventSource so tests never need a DOM. */
export type EventSourceLike = {
  onmessage: ((event: { data: string; lastEventId?: string }) => void) | null;
  onerror: (() => void) | null;
  close: () => void;
};
export type TaskActivityStreamDeps = {
  eventSourceFactory?: (url: string) => EventSourceLike;
  setTimeout?: (fn: () => void, ms: number) => number;
  clearTimeout?: (id: number) => void;
};

const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000];
/** Matches the backend terminal frame; the server closes the stream after it. */
const TERMINAL_FRAME = 'done';

export function followTaskActivity(
  taskId: string,
  handlers: ToolActivityHandlers,
  deps: TaskActivityStreamDeps = {},
): TaskActivityStream {
  const newSource = deps.eventSourceFactory
    ?? ((url: string) => new EventSource(url) as unknown as EventSourceLike);
  const later = deps.setTimeout
    ?? ((fn: () => void, ms: number) => window.setTimeout(fn, ms));
  const cancel = deps.clearTimeout ?? ((id: number) => window.clearTimeout(id));
  let closed = false;
  let source: EventSourceLike | undefined;
  let retries = 0;
  let timer = 0;
  let cursor = 0;

  function closeSource() {
    const previous = source;
    source = undefined;
    if (previous) {
      previous.onmessage = null;
      previous.onerror = null;
      previous.close();
    }
  }
  function close() { closed = true; cancel(timer); closeSource(); }

  function onMessage(event: { data: string; lastEventId?: string }) {
    let frame: {
      type?: string; toolName?: string; query?: string;
      toolCallId?: string; status?: string;
    };
    try { frame = JSON.parse(String(event.data)); } catch { return; }
    if (!frame || typeof frame !== 'object' || Array.isArray(frame)) return;
    retries = 0;
    const nextCursor = Number(event.lastEventId);
    if (Number.isSafeInteger(nextCursor) && nextCursor > cursor) cursor = nextCursor;
    if (frame.type === 'tool_start' && frame.toolName) {
      handlers.toolStart?.({
        tool: String(frame.toolName), query: String(frame.query || ''),
        toolCallId: String(frame.toolCallId || ''),
      });
      return;
    }
    if (frame.type === 'tool_complete' || frame.type === 'tool_result') {
      handlers.toolEnd?.(String(frame.toolCallId || ''));
      return;
    }
    if (frame.type === TERMINAL_FRAME) {
      const status = String(frame.status || '');
      close();
      handlers.done?.(status);
    }
  }

  function scheduleReconnect() {
    if (closed || timer) return;
    const delay = BACKOFF_MS[Math.min(retries, BACKOFF_MS.length - 1)];
    retries += 1;
    timer = later(() => { timer = 0; connect(); }, delay);
  }

  function connect() {
    if (closed) return;
    closeSource();
    const url = resolvePath(`/api/v4/tasks/${encodeURIComponent(taskId)}/stream?cursor=${cursor}`);
    let current: EventSourceLike;
    try { current = newSource(url); }
    catch { scheduleReconnect(); return; }
    source = current;
    current.onmessage = (event) => {
      if (!closed && source === current) onMessage(event);
    };
    current.onerror = () => {
      if (closed || source !== current) return;
      closeSource();
      scheduleReconnect();
    };
  }

  connect();
  return { close };
}

/** Task submission, polling, abort and replay cursors. Network lifetime belongs to the transport. */
import type { Tofu } from './client.js';
import type { TaskState } from './types.js';
import { TofuError, parseSequence, taskTerminal } from './protocol.js';
import { delay, responseBody } from './transport.js';
import { parseSSE } from './streaming.js';

export class TasksAPI {
  constructor(private c: Tofu) {}

  // Public for introspection — what kinds the SDK knows how to start.
  static readonly KIND_ROUTES: Record<string, string> = {
    'paper-report': '/api/v1/agents/paper/report',
    'paper-translate': '/api/v1/agents/paper/translate',
    'translate': '/api/v1/agents/translate',
    'image-gen': '/api/v1/agents/image-gen',
    'memory-search': '/api/v1/agents/memory/search',
    'search': '/api/v1/agents/search/async',
  };

  start(kind: string, params: Record<string, unknown> = {}): Promise<{ task_id?: string; taskId?: string } & Record<string, unknown>> {
    const path = TasksAPI.KIND_ROUTES[kind];
    if (!path) {
      throw new Error(
        `Unknown task kind: ${kind}. Known: ${Object.keys(TasksAPI.KIND_ROUTES).join(', ')}`);
    }
    return this.c._json('POST', path, { json: params });
  }

  /** Start a task and block until terminal. Returns the final task state. */
  async run(kind: string, params: Record<string, unknown> = {},
             opts: { pollIntervalMs?: number; timeoutMs?: number } = {}
            ): Promise<TaskState> {
    const started = await this.start(kind, params);
    const taskId = String(started.task_id || started.taskId || '');
    if (!taskId) {
      throw new Error(`Started task did not return a task_id: ${JSON.stringify(started)}`);
    }
    return this.wait(taskId, opts);
  }

  /** Start a task and yield SSE events as they arrive. */
  async *startAndStream(kind: string, params: Record<string, unknown> = {}
                         ): AsyncIterable<Record<string, unknown>> {
    const started = await this.start(kind, params);
    const taskId = String(started.task_id || started.taskId || '');
    if (!taskId) {
      throw new Error(`Started task did not return a task_id: ${JSON.stringify(started)}`);
    }
    yield { type: 'started', task_id: taskId, started };
    yield* this.stream(taskId);
  }

  get(taskId: string): Promise<TaskState> {
    return this.c._jsonWithRetry('GET',
      `/api/v1/tasks/${encodeURIComponent(taskId)}`);
  }

  list(opts: { kind?: string; status?: string; limit?: number } = {}): Promise<{ tasks: TaskState[]; total: number }> {
    const qs = new URLSearchParams();
    if (opts.kind) qs.set('kind', opts.kind);
    if (opts.status) qs.set('status', opts.status);
    qs.set('limit', String(opts.limit ?? 50));
    return this.c._json('GET', `/api/v1/tasks?${qs}`);
  }

  events(taskId: string, cursor = 0): Promise<{ events: unknown[]; status: string }> {
    return this.c._jsonWithRetry('GET',
      `/api/v1/tasks/${encodeURIComponent(taskId)}/events?cursor=${cursor}`);
  }

  async *stream(
    taskId: string,
    cursor = 0,
    opts: { reconnect?: boolean; maxReconnects?: number } = {},
  ): AsyncIterable<Record<string, unknown>> {
    let nextCursor = Math.max(0, Math.trunc(cursor));
    let attempts = 0;
    const reconnect = opts.reconnect ?? true;
    const maxReconnects = Math.max(0, opts.maxReconnects ?? 3);
    for (;;) {
      try {
        const response = await this.c._requestWithRetry('GET',
          `/api/v1/tasks/${encodeURIComponent(taskId)}/stream?cursor=${nextCursor}`,
          { headers: { Accept: 'text/event-stream' } }, maxReconnects);
        if (!response.ok) {
          const body = await responseBody(response);
          throw new TofuError(response.status, body);
        }
        for await (const event of parseSSE(response)) {
          attempts = 0;
          const sequence = parseSequence(event.seq);
          if (sequence !== undefined) {
            nextCursor = Math.max(nextCursor, sequence + 1);
          }
          yield event;
          if (taskTerminal(event)) return;
        }
        const state = await this.get(taskId);
        if (['done', 'error', 'aborted'].includes(state.status)) return;
      } catch (error) {
        if (!reconnect || (error instanceof TofuError && error.status < 500
            && error.status !== 429)) throw error;
      }
      attempts += 1;
      if (!reconnect || attempts > maxReconnects) {
        throw new TofuError(599, {
          error: { kind: 'stream_disconnected', message: 'task stream reconnect limit exceeded' },
          task_id: taskId,
          cursor: nextCursor,
        });
      }
      await delay(Math.min(500 * (2 ** (attempts - 1)), 5_000));
    }
  }

  abort(taskId: string): Promise<{ taskId: string; status: string }> {
    return this.c._jsonWithRetry('POST',
      `/api/v1/tasks/${encodeURIComponent(taskId)}/abort`);
  }

  async wait(taskId: string, opts: { pollIntervalMs?: number; timeoutMs?: number } = {}): Promise<TaskState> {
    const interval = opts.pollIntervalMs ?? 1000;
    const deadline = Date.now() + (opts.timeoutMs ?? 600_000);
    for (;;) {
      const t = await this.get(taskId);
      if (t.status === 'done' || t.status === 'error' || t.status === 'aborted') return t;
      if (Date.now() >= deadline) throw new Error(`task ${taskId} did not finish`);
      await new Promise(r => setTimeout(r, interval));
    }
  }
}

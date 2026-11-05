/** Native agent submission and feature convenience routes; reuses task replay and transport policies. */
import type { Tofu } from './client.js';
import type { AgentRunRequest, AgentRunResult } from './types.js';
import { TofuError, nativeRequest, newIdempotencyKey } from './protocol.js';

export class AgentsAPI {
  constructor(private c: Tofu) {}

  /** Run the complete Tofu agent loop. Server-managed model is the default. */
  run(
    request: AgentRunRequest,
    opts: { idempotencyKey?: string; maxRetries?: number } = {},
  ): Promise<AgentRunResult> {
    const key = opts.idempotencyKey || newIdempotencyKey();
    return this.c._jsonWithRetry<AgentRunResult>(
      'POST', '/api/v1/agent/run', {
        json: {
          ...nativeRequest(request, { modelRequired: false }),
          stream: false,
        },
        headers: { 'Idempotency-Key': key },
      }, opts.maxRetries ?? 3);
  }

  /** Submit the run and return a task handle immediately (HTTP 202). */
  start(
    request: AgentRunRequest,
    opts: { idempotencyKey?: string; maxRetries?: number } = {},
  ): Promise<{ ok: boolean; id: string; task_id: string; status: string; model: string }> {
    const key = opts.idempotencyKey || newIdempotencyKey();
    return this.c._jsonWithRetry(
      'POST', '/api/v1/agent/run', {
        json: {
          ...nativeRequest(request, { modelRequired: false }),
          async: true,
        },
        headers: {
          'Idempotency-Key': key,
          Prefer: 'respond-async',
        },
      }, opts.maxRetries ?? 3);
  }

  /** Start once, then resume the task SSE stream after transport drops. */
  async *stream(
    request: AgentRunRequest,
    opts: {
      idempotencyKey?: string;
      cursor?: number;
      reconnect?: boolean;
      maxReconnects?: number;
    } = {},
  ): AsyncIterable<Record<string, unknown>> {
    const started = await this.start(request, {
      idempotencyKey: opts.idempotencyKey,
      maxRetries: opts.maxReconnects,
    });
    if (!started.task_id) {
      throw new Error(`agent.run did not return task_id: ${JSON.stringify(started)}`);
    }
    yield* this.c.tasks.stream(started.task_id, opts.cursor ?? 0, {
      reconnect: opts.reconnect,
      maxReconnects: opts.maxReconnects,
    });
  }

  runStream(
    request: AgentRunRequest,
    opts: {
      idempotencyKey?: string;
      cursor?: number;
      reconnect?: boolean;
      maxReconnects?: number;
    } = {},
  ): AsyncIterable<Record<string, unknown>> {
    return this.stream(request, opts);
  }

  paperReport(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.c._json('POST', '/api/v1/agents/paper/report', { json: params });
  }
  paperTranslate(params: Record<string, unknown>): Promise<Record<string, unknown>> {
    return this.c._json('POST', '/api/v1/agents/paper/translate', { json: params });
  }
  translate(params: { text: string; target_lang?: string; source_lang?: string; model?: string }): Promise<Record<string, unknown>> {
    return this.c._json('POST', '/api/v1/agents/translate', {
      json: { target_lang: 'zh', ...params },
    });
  }
  imageGen(params: { prompt: string; model?: string; aspect_ratio?: string; resolution?: string }): Promise<Record<string, unknown>> {
    return this.c._json('POST', '/api/v1/agents/image-gen', { json: params });
  }
  memorySearch(params: { query: string; top_k?: number }): Promise<{ results: unknown[]; count: number }> {
    return this.c._json('POST', '/api/v1/agents/memory/search', {
      json: { top_k: 30, ...params },
    });
  }
  fetch(params: { url: string }): Promise<{ url: string; text: string; length: number }> {
    return this.c._json('POST', '/api/v1/agents/browser/fetch', { json: params });
  }
  /** Server-side log noise detection (mirrors the UI's banner heuristic). */
  cleanLog(params: { text: string }): Promise<Record<string, unknown>> {
    return this.c._json('POST', '/api/v1/logs/clean', { json: params });
  }
  /** Extract file-change summary from a tool-rounds blob (matches UI). */
  extractFileChanges(params: { toolRounds: Record<string, unknown>[] }): Promise<{ files: Record<string, unknown>[] }> {
    return this.c._json('POST', '/api/v1/messages/extract-file-changes',
      { json: params });
  }

  // Feature-shaped poll surfaces — generic event replay is via
  // client.tasks.events(id) / .stream(id). These return the structured
  // shapes the UI consumes ({translated, partial, …} for translate;
  // {events, next_cursor, status, …} for paper).
  pollTranslate(taskId: string): Promise<Record<string, unknown>> {
    return this.c._json('GET',
      `/api/v1/agents/translate/poll/${encodeURIComponent(taskId)}`);
  }

  pollTranslateBatch(taskIds: string[]): Promise<Record<string, unknown>> {
    return this.c._json('POST', '/api/v1/agents/translate/poll/batch',
      { json: { taskIds } });
  }

  pollPaperReport(taskId: string, cursor = 0): Promise<Record<string, unknown>> {
    return this.c._json('GET',
      `/api/v1/agents/paper/report/poll?task_id=${encodeURIComponent(taskId)}&cursor=${cursor}`);
  }

  pollPaperTranslate(taskId: string, cursor = 0): Promise<Record<string, unknown>> {
    return this.c._json('GET',
      `/api/v1/agents/paper/translate/poll?task_id=${encodeURIComponent(taskId)}&cursor=${cursor}`);
  }

  // ── Web search ─────────────────────────────────────────────────

  search(params: { query: string; max_results?: number; freshness?: string; include_summary?: boolean }): Promise<Record<string, unknown>> {
    return this.c._json('POST', '/api/v1/agents/search', {
      json: { max_results: 10, ...params },
    });
  }

  searchAsync(params: { query: string; max_results?: number; freshness?: string; include_summary?: boolean }): Promise<{ task_id: string; status: string }> {
    return this.c._json('POST', '/api/v1/agents/search/async', {
      json: { max_results: 10, ...params },
    });
  }

  // ── Feature-shaped polling convenience helpers ─────────────────

  async translateAndWait(params: {
    text: string;
    target_lang?: string;
    source_lang?: string;
    pollIntervalMs?: number;
    timeoutMs?: number;
  }): Promise<Record<string, unknown>> {
    const started = await this.translate({
      text: params.text,
      target_lang: params.target_lang || 'zh',
      source_lang: params.source_lang,
    } as any);
    const taskId = String((started as any).task_id || (started as any).taskId || '');
    if (!taskId) throw new Error(`translate did not return a taskId`);
    const interval = params.pollIntervalMs ?? 2000;
    const deadline = Date.now() + (params.timeoutMs ?? 180_000);
    for (;;) {
      const result = await this.pollTranslate(taskId);
      const status = result.status as string;
      if (status === 'done') return result;
      if (status === 'error' || status === 'not_found') {
        throw new TofuError(422, result.error || `translate ${status}`);
      }
      if (Date.now() >= deadline) throw new Error(`translate task ${taskId} did not finish`);
      await new Promise(r => setTimeout(r, interval));
    }
  }

  async paperReportAndWait(params: {
    paper_text?: string;
    lang?: string;
    pollIntervalMs?: number;
    timeoutMs?: number;
    [k: string]: unknown;
  }): Promise<Record<string, unknown>> {
    const { pollIntervalMs, timeoutMs, ...rest } = params;
    const started = await this.paperReport(rest);
    const taskId = String((started as any).task_id || (started as any).taskId || '');
    if (!taskId) throw new Error(`paperReport did not return a taskId`);
    const interval = pollIntervalMs ?? 2000;
    const deadline = Date.now() + (timeoutMs ?? 600_000);
    let cursor = 0;
    for (;;) {
      const result = await this.pollPaperReport(taskId, cursor);
      cursor = (result as any).next_cursor ?? cursor;
      const status = (result as any).status;
      if (status === 'done') return result;
      if (status === 'error') {
        throw new TofuError(422, (result as any).error || 'paper-report error');
      }
      if (Date.now() >= deadline) throw new Error(`paper-report task ${taskId} did not finish`);
      await new Promise(r => setTimeout(r, interval));
    }
  }
}

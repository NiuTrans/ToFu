// ════════════════════════════════════════════════════════════════
//  @rangehow/tofu-sdk — TypeScript client for the Tofu headless API.
//
//  Mirrors the Python SDK 1:1 (clients/python/tofu_sdk/__init__.py).
//  Uses the standard Web Fetch API — works in Node 18+, browsers,
//  Cloudflare Workers, Vercel Edge, Deno, Bun.  No external deps.
// ════════════════════════════════════════════════════════════════

export const VERSION = '0.17.0';
export * from './api-v4.generated.js';

// ── Types ───────────────────────────────────────────────────────

export interface TofuOptions {
  baseUrl: string;
  /** Optional for a loopback-only headless server; required for remote use. */
  apiKey?: string;
  timeoutMs?: number;
  userAgent?: string;
  fetchImpl?: typeof fetch;
}

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: string | Array<Record<string, unknown>> | null;
  name?: string;
  tool_calls?: Array<Record<string, unknown>>;
  tool_call_id?: string;
}

export interface ChatRequest {
  messages: ChatMessage[];
  model?: string;
  config?: Record<string, unknown>;
  temperature?: number;
  max_tokens?: number;
  top_p?: number;
  stop?: string | string[];
  tools?: Array<Record<string, unknown>>;
  tool_choice?: string | Record<string, unknown>;
  response_format?: Record<string, unknown>;
  conversation_id?: string;
  idempotency_key?: string;
  timeout_s?: number;
}

export interface AgentProvider {
  base_url?: string;
  /** Friendly alias accepted by tofu-agent. */
  endpoint?: string;
  api_key?: string;
  model?: string;
  extra_headers?: Record<string, string>;
  thinking_format?: string;
  capabilities?: string[];
}

export interface AgentRunRequest {
  messages: ChatMessage[];
  /** Optional when the deployment or provider block supplies a default. */
  model?: string;
  provider?: AgentProvider;
  config?: Record<string, unknown>;
  capabilities?: Record<string, unknown>;
  /** Request-local OpenAI function schemas. */
  tools?: Array<Record<string, unknown>>;
  trajectory?: 'sharegpt' | 'openai-finetune' | 'anthropic' | 'tofu-native';
  timeout_s?: number;
  conversation_id?: string;
  id?: string;
  [key: string]: unknown;
}

export interface AgentRunResult {
  ok: boolean;
  id: string;
  object: 'agent.run';
  task_id: string;
  status: 'pending' | 'running' | 'done' | 'error' | 'aborted';
  model: string;
  finish_reason: string;
  content: string;
  thinking: string;
  usage: Record<string, number>;
  n_tool_rounds: number;
  error?: Record<string, unknown>;
  tool_calls?: Array<Record<string, unknown>>;
  trajectory_format?: string;
  trajectory?: unknown;
  [key: string]: unknown;
}

export interface ChatCompletion {
  ok: boolean;
  id: string;
  object: 'chat.completion';
  created: number;
  model: string;
  choices: Array<{
    index: number;
    message: ChatMessage & { reasoning_content?: string };
    finish_reason: string;
  }>;
  usage: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
  };
  task_id: string;
}

export interface TaskState {
  id: string;
  kind: string;
  status: 'pending' | 'running' | 'done' | 'error' | 'aborted';
  created_at: number;
  finished_at: number | null;
  result?: unknown;
  meta?: Record<string, unknown>;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  rate_limit_rpm: number;
  rate_limit_tpd: number;
  created_at: number;
  last_used_at: number | null;
  expires_at: number | null;
  disabled: boolean;
}

export class TofuError extends Error {
  constructor(public status: number, public body: unknown) {
    super(`Tofu API error ${status}: ${typeof body === 'string' ? body : JSON.stringify(body)}`);
    this.name = 'TofuError';
  }
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function responseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try { return JSON.parse(text); } catch { return text; }
}

function newIdempotencyKey(): string {
  const cryptoValue = globalThis.crypto as Crypto | undefined;
  if (cryptoValue?.randomUUID) return cryptoValue.randomUUID();
  return `tofu-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

async function* parseSSE(response: Response): AsyncIterable<Record<string, unknown>> {
  if (!response.body) throw new Error('streaming requires a Response.body');
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = buffer.replace(/\r\n/g, '\n');
    let boundary: number;
    while ((boundary = buffer.indexOf('\n\n')) >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let eventName = '';
      let eventId: number | undefined;
      const dataLines: string[] = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith(':')) continue;
        if (line.startsWith('event:')) eventName = line.slice(6).trim();
        else if (line.startsWith('id:')) {
          const parsed = Number(line.slice(3).trim());
          if (Number.isFinite(parsed)) eventId = parsed;
        } else if (line.startsWith('data:')) {
          dataLines.push(line.slice(5).trim());
        }
      }
      const data = dataLines.join('\n');
      if (!data) continue;
      if (data === '[DONE]') return;
      let payload: unknown;
      try { payload = JSON.parse(data); } catch { continue; }
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) continue;
      const event = payload as Record<string, unknown>;
      if (eventName && event.event === undefined) event.event = eventName;
      if (eventId !== undefined && event.seq === undefined) event.seq = eventId;
      yield event;
      const terminal = ['done', 'error', 'aborted'];
      if (terminal.includes(String(event.type || ''))
          || terminal.includes(String(event.event || ''))) return;
    }
  }
}

// ── Core client ─────────────────────────────────────────────────

export class Tofu {
  readonly baseUrl: string;
  readonly apiKey: string;
  private readonly timeoutMs: number;
  private readonly userAgent: string;
  private readonly _fetch: typeof fetch;

  readonly tasks: TasksAPI;
  readonly agents: AgentsAPI;
  readonly keys: KeysAPI;
  readonly webhooks: WebhooksAPI;

  constructor(opts: TofuOptions) {
    if (!opts.baseUrl) throw new Error('baseUrl required');
    this.baseUrl = opts.baseUrl.replace(/\/+$/, '');
    this.apiKey = opts.apiKey ?? '';
    this.timeoutMs = opts.timeoutMs ?? 600_000;
    this.userAgent = opts.userAgent ?? `tofu-sdk-ts/${VERSION}`;
    this._fetch = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);

    this.tasks = new TasksAPI(this);
    this.agents = new AgentsAPI(this);
    this.keys = new KeysAPI(this);
    this.webhooks = new WebhooksAPI(this);
  }

  /** @internal */
  _url(path: string): string {
    if (/^https?:\/\//.test(path)) return path;
    return this.baseUrl + (path.startsWith('/') ? path : '/' + path);
  }

  /** @internal */
  async _request(
    method: string,
    path: string,
    init: RequestInit & { json?: unknown } = {},
  ): Promise<Response> {
    const headers = new Headers(init.headers || {});
    if (this.apiKey) headers.set('Authorization', `Bearer ${this.apiKey}`);
    headers.set('User-Agent', this.userAgent);
    if (!headers.has('Accept')) headers.set('Accept', 'application/json');
    let body = init.body;
    if (init.json !== undefined) {
      headers.set('Content-Type', 'application/json');
      body = JSON.stringify(init.json);
    }
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      return await this._fetch(this._url(path), {
        ...init,
        method,
        headers,
        body,
        signal: init.signal ?? ctrl.signal,
      });
    } finally {
      clearTimeout(timer);
    }
  }

  /** @internal */
  async _json<T = unknown>(method: string, path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
    const resp = await this._request(method, path, init);
    const body = await responseBody(resp);
    if (!resp.ok) throw new TofuError(resp.status, body);
    return body as T;
  }

  /** @internal — retry only idempotent GETs or POSTs carrying Idempotency-Key. */
  async _requestWithRetry(
    method: string,
    path: string,
    init: RequestInit & { json?: unknown } = {},
    maxRetries = 3,
  ): Promise<Response> {
    for (let attempt = 0; ; attempt += 1) {
      try {
        const response = await this._request(method, path, init);
        const transient = response.status === 429 || response.status >= 500;
        if (!transient || attempt >= maxRetries) return response;
        const retryAfter = Number(response.headers.get('Retry-After') || 0);
        try { await response.body?.cancel(); } catch { /* already consumed */ }
        await delay(Math.min(Math.max(
          retryAfter * 1000, 500 * (2 ** attempt)), 30_000));
      } catch (error) {
        if (attempt >= maxRetries) throw error;
        await delay(Math.min(500 * (2 ** attempt), 10_000));
      }
    }
  }

  /** @internal */
  async _jsonWithRetry<T = unknown>(
    method: string,
    path: string,
    init: RequestInit & { json?: unknown } = {},
    maxRetries = 3,
  ): Promise<T> {
    const response = await this._requestWithRetry(method, path, init, maxRetries);
    const body = await responseBody(response);
    if (!response.ok) throw new TofuError(response.status, body);
    return body as T;
  }

  // ── Capabilities ──────────────────────────────────────────────

  capabilities(): Promise<Record<string, unknown>> {
    return this._json('GET', '/api/v1/capabilities');
  }

  // ── Chat ──────────────────────────────────────────────────────

  chat(req: ChatRequest): Promise<ChatCompletion> {
    return this._json<ChatCompletion>(
      'POST', '/api/v1/chat/completions', { json: req });
  }

  /** Stream chat completion as parsed SSE events. */
  async *stream(req: ChatRequest): AsyncIterable<Record<string, unknown>> {
    const resp = await this._request('POST', '/api/v1/chat/completions', {
      json: { ...req, stream: true },
      headers: { Accept: 'text/event-stream' },
    });
    if (!resp.ok) {
      const err = await responseBody(resp);
      throw new TofuError(resp.status, err);
    }
    yield* parseSSE(resp);
  }
}

// ── Sub-APIs ────────────────────────────────────────────────────

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
          const sequence = Number(event.seq);
          if (Number.isFinite(sequence)) {
            nextCursor = Math.max(nextCursor, Math.trunc(sequence) + 1);
          }
          yield event;
          if (['done', 'error', 'aborted'].includes(String(event.type || ''))) return;
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
        json: { ...request, stream: false },
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
        json: { ...request, async: true },
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

class KeysAPI {
  constructor(private c: Tofu) {}

  whoami(): Promise<Record<string, unknown>> {
    return this.c._json('GET', '/api/v1/keys/whoami');
  }
  list(): Promise<{ keys: ApiKey[] }> {
    return this.c._json('GET', '/api/v1/keys');
  }
  create(params: { name: string; scopes: string[]; rate_limit_rpm?: number; rate_limit_tpd?: number; admin?: boolean }): Promise<{ key: ApiKey; token: string }> {
    return this.c._json('POST', '/api/v1/keys', { json: params });
  }
  revoke(keyId: string): Promise<{ revoked: string }> {
    return this.c._json('DELETE', `/api/v1/keys/${encodeURIComponent(keyId)}`);
  }
}

class WebhooksAPI {
  constructor(private c: Tofu) {}

  list(): Promise<{ subs: unknown[] }> {
    return this.c._json('GET', '/api/v1/webhooks');
  }
  subscribe(params: { url: string; channel?: string; event_types?: string[]; task_id?: string }): Promise<{ subscription: { id: string; secret: string; url: string } }> {
    return this.c._json('POST', '/api/v1/webhooks', {
      json: { task_id: '*', ...params },
    });
  }
  unsubscribe(subId: string): Promise<{ deleted: string }> {
    return this.c._json('DELETE', `/api/v1/webhooks/${encodeURIComponent(subId)}`);
  }
}

export default Tofu;

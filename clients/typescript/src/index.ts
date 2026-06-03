// ════════════════════════════════════════════════════════════════
//  @tofu/sdk — TypeScript client for the Tofu headless API.
//
//  Mirrors the Python SDK 1:1 (clients/python/tofu_sdk/__init__.py).
//  Uses the standard Web Fetch API — works in Node 18+, browsers,
//  Cloudflare Workers, Vercel Edge, Deno, Bun.  No external deps.
// ════════════════════════════════════════════════════════════════

export const VERSION = '1.0.0';

// ── Types ───────────────────────────────────────────────────────

export interface TofuOptions {
  baseUrl: string;
  apiKey: string;
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
    if (!opts.apiKey) throw new Error('apiKey required');
    this.baseUrl = opts.baseUrl.replace(/\/+$/, '');
    this.apiKey = opts.apiKey;
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
    headers.set('Authorization', `Bearer ${this.apiKey}`);
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
    let body: unknown = null;
    try { body = await resp.json(); } catch (_) { body = await resp.text(); }
    if (!resp.ok) throw new TofuError(resp.status, body);
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
      let err: unknown;
      try { err = await resp.json(); } catch { err = await resp.text(); }
      throw new TofuError(resp.status, err);
    }
    if (!resp.body) throw new Error('streaming requires a Response.body');
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, nl);
        buf = buf.slice(nl + 2);
        for (const line of frame.split('\n')) {
          if (!line.startsWith('data:')) continue;
          const data = line.slice(5).trim();
          if (!data || data === '[DONE]') {
            if (data === '[DONE]') return;
            continue;
          }
          try {
            yield JSON.parse(data);
          } catch (_) {
            // unparseable chunk — ignore, matches Python SDK behaviour
          }
        }
      }
    }
  }
}

// ── Sub-APIs ────────────────────────────────────────────────────

class TasksAPI {
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
    return this.c._json('GET', `/api/v1/tasks/${encodeURIComponent(taskId)}`);
  }

  list(opts: { kind?: string; status?: string; limit?: number } = {}): Promise<{ tasks: TaskState[]; total: number }> {
    const qs = new URLSearchParams();
    if (opts.kind) qs.set('kind', opts.kind);
    if (opts.status) qs.set('status', opts.status);
    qs.set('limit', String(opts.limit ?? 50));
    return this.c._json('GET', `/api/v1/tasks?${qs}`);
  }

  events(taskId: string, cursor = 0): Promise<{ events: unknown[]; status: string }> {
    return this.c._json('GET',
      `/api/v1/tasks/${encodeURIComponent(taskId)}/events?cursor=${cursor}`);
  }

  async *stream(taskId: string, cursor = 0): AsyncIterable<Record<string, unknown>> {
    const resp = await this.c._request('GET',
      `/api/v1/tasks/${encodeURIComponent(taskId)}/stream?cursor=${cursor}`,
      { headers: { Accept: 'text/event-stream' } });
    if (!resp.ok) throw new TofuError(resp.status, await resp.text());
    if (!resp.body) return;
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, nl);
        buf = buf.slice(nl + 2);
        for (const line of frame.split('\n')) {
          if (!line.startsWith('data:')) continue;
          const data = line.slice(5).trim();
          if (!data || data === '[DONE]') {
            if (data === '[DONE]') return;
            continue;
          }
          try { yield JSON.parse(data); } catch (_) { /* ignore */ }
        }
      }
    }
  }

  abort(taskId: string): Promise<{ taskId: string; status: string }> {
    return this.c._json('POST',
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

class AgentsAPI {
  constructor(private c: Tofu) {}

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

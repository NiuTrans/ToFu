/** Fetch transport owns deadlines, abort forwarding, retries and response cleanup. No domain routes. */
import type { TofuOptions } from './types.js';
import { TofuError, unwrapEnvelope } from './protocol.js';
import { VERSION } from './version.js';

export function delay(ms: number, signal?: AbortSignal | null): Promise<void> {
  if (signal?.aborted) return Promise.reject(signal.reason);
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(signal?.reason); };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', abort);
      resolve();
    }, ms);
    signal?.addEventListener('abort', abort, { once: true });
  });
}

// Transport lifetime belongs to the response until its body is consumed or
// explicitly discarded. Weak keys cannot retain completed response objects.
const responseLifetimes = new WeakMap<Response, () => void>();
export function releaseResponse(response: Response): void {
  responseLifetimes.get(response)?.();
  responseLifetimes.delete(response);
}
export function discardResponse(response: Response): void {
  // Cancellation may depend on an unresponsive peer; cleanup must not await it.
  void response.body?.cancel().catch(() => {});
  releaseResponse(response);
}

export async function responseBody(response: Response): Promise<unknown> {
  try {
    const text = await response.text();
    if (!text) return null;
    try {
      const body: unknown = JSON.parse(text);
      return response.ok ? unwrapEnvelope(body) : body;
    } catch { return text; }
  } finally {
    releaseResponse(response);
  }
}

export class FetchTransport {
  readonly baseUrl: string;
  readonly apiKey: string;
  private readonly timeoutMs: number;
  private readonly userAgent: string;
  private readonly _fetch: typeof fetch;

  constructor(opts: TofuOptions) {
    if (!opts.baseUrl) throw new Error('baseUrl required');
    this.baseUrl = opts.baseUrl.replace(/\/+$/, '');
    this.apiKey = opts.apiKey ?? '';
    this.timeoutMs = opts.timeoutMs ?? 600_000;
    this.userAgent = opts.userAgent ?? `tofu-sdk-ts/${VERSION}`;
    this._fetch = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  /** @internal — the SDK speaks API v4: product calls are rewritten onto
   * `/api/v4`; the envelope is unwrapped in `responseBody`. */
  _url(path: string): string {
    if (/^https?:\/\//.test(path)) return path;
    const resolved = path.startsWith('/api/v1/')
      ? `/api/v4/${path.slice('/api/v1/'.length)}`
      : path;
    return this.baseUrl + (resolved.startsWith('/') ? resolved : '/' + resolved);
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
    const external = init.signal;
    if (external?.aborted) throw external.reason;
    const forwardAbort = () => ctrl.abort(external?.reason);
    external?.addEventListener('abort', forwardAbort, { once: true });
    const timer = setTimeout(() => ctrl.abort(
      new DOMException('Request deadline exceeded', 'TimeoutError')), this.timeoutMs);
    const cleanup = () => {
      clearTimeout(timer);
      external?.removeEventListener('abort', forwardAbort);
      ctrl.signal.removeEventListener('abort', cleanup);
    };
    ctrl.signal.addEventListener('abort', cleanup, { once: true });
    try {
      const response = await this._fetch(this._url(path), {
        ...init,
        method,
        headers,
        body,
        signal: ctrl.signal,
      });
      responseLifetimes.set(response, cleanup);
      return response;
    } catch (error) {
      cleanup();
      throw error;
    }
  }

  /** @internal */
  async _json<T = unknown>(method: string, path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
    const resp = await this._request(method, path, init);
    const body = await responseBody(resp);
    if (!resp.ok) throw new TofuError(resp.status, body);
    return body as T;
  }

  /** @internal — callers select safe GETs or idempotent operations (including task abort). */
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
        discardResponse(response);
        await delay(Math.min(Math.max(
          Number.isFinite(retryAfter) ? retryAfter * 1000 : 0, 500 * (2 ** attempt)), 30_000), init.signal);
      } catch (error) {
        if (init.signal?.aborted) throw init.signal.reason;
        if (attempt >= maxRetries) throw error;
        await delay(Math.min(500 * (2 ** attempt), 10_000), init.signal);
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
}

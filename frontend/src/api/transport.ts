import {
  normalizeApiProblemDetails,
  normalizeErrorEnvelope,
  type ApiProblemDetails,
  type ErrorEnvelope,
} from './errors';

export type ParseMode = 'json' | 'text' | 'blob' | 'response' | 'none';
export type RequestPriority = 'foreground' | 'normal' | 'background';

export interface RequestOptions {
  method?: string;
  query?: Record<string, unknown>;
  json?: unknown;
  body?: BodyInit;
  headers?: Record<string, string>;
  timeout?: number;
  parse?: ParseMode;
  signal?: AbortSignal;
  keepalive?: boolean;
  credentials?: RequestCredentials;
  onError?: 'throw' | 'null';
  priority?: RequestPriority;
  /** Explicitly merge identical, safe GETs while they are in flight. */
  coalesce?: boolean;
  /** Explicitly include a semantically-read POST in the constrained lane. */
  govern?: boolean;
  taskId?: string;
  convId?: string;
  taskAffinityKey?: string;
  rememberTaskAffinity?: boolean;
  rememberActiveAffinities?: boolean;
  /** Explicit allowlisted JSON-RPC method for constrained-proxy reads. */
  rpcMethod?: string;
  /** Params owned by that RPC method; never interpreted as an HTTP target. */
  rpcParams?: Record<string, unknown>;
  [key: string]: unknown;
}

export interface ApiErrorOptions {
  status?: number;
  code?: string | number | null;
  body?: unknown;
  url?: string | null;
  requestId?: string | null;
  clientRequestId?: string | null;
  serverRequestId?: string | null;
  envelope?: ErrorEnvelope | null;
  problem?: ApiProblemDetails | null;
}

export class ApiError extends Error {
  status: number;
  code: string | number | null;
  body: unknown;
  url: string | null;
  requestId: string | null;
  clientRequestId: string | null;
  serverRequestId: string | null;
  envelope: ErrorEnvelope | null;
  problem: ApiProblemDetails | null;

  constructor(message: string, options: ApiErrorOptions = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = options.status || 0;
    this.code = options.code ?? null;
    this.body = options.body === undefined ? null : options.body;
    this.url = options.url || null;
    this.requestId = options.requestId || null;
    this.clientRequestId = options.clientRequestId || null;
    this.serverRequestId = options.serverRequestId || null;
    this.envelope = options.envelope || null;
    this.problem = options.problem || null;
  }
}

type MutableResponse = Response & {
  json: () => Promise<unknown>;
};

type ApiGlobals = Window & typeof globalThis & {
  apiUrl?: (path: string) => string;
  Api?: Record<string, unknown>;
  ApiError?: typeof ApiError;
  pushRpcRequest?: <T = unknown>(
    method: string,
    params: Record<string, unknown>,
    options: { timeout?: number; signal?: AbortSignal },
  ) => Promise<T>;
};

declare global {
  interface Window {
    Api?: Record<string, unknown>;
    ApiError?: typeof ApiError;
  }
}

type AffinityRecord = {
  id?: unknown;
  taskId?: unknown;
  convId?: unknown;
  affinityKey?: unknown;
  items?: unknown;
};

const globals = window as ApiGlobals;
const AFFINITY_STORAGE_KEY = 'tofu_task_affinity_v1';
const taskAffinity = new Map<string, string>();
const conversationAffinity = new Map<string, string>();
let requestSequence = 0;

const PROXY_READ_CONCURRENCY = 6;
const PROXY_QUEUE_MAX = 256;
const COALESCED_GET_MAX = 128;
const STATUS_ERROR_CODES: Readonly<Record<number, string>> = {
  400: 'bad_request',
  401: 'unauthorized',
  402: 'payment_required',
  403: 'forbidden',
  404: 'not_found',
  405: 'method_not_allowed',
  409: 'conflict',
  413: 'payload_too_large',
  422: 'unprocessable_content',
  426: 'api_version_upgrade_required',
  429: 'rate_limited',
  500: 'internal_error',
  502: 'bad_gateway',
  503: 'service_unavailable',
  504: 'gateway_timeout',
};
const proxyQueue: ProxyQueueEntry[] = [];
const coalescedGets = new Map<string, Promise<unknown>>();
let proxyActiveReads = 0;
let proxyQueueSequence = 0;

type ProxyQueueEntry = {
  priority: number;
  sequence: number;
  signal?: AbortSignal;
  resolve: (release: () => void) => void;
  reject: (error: unknown) => void;
  abortListener: (() => void) | null;
  timeoutId: number | null;
};

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function errorDetails(error: unknown): { name: string; message: string } {
  if (error instanceof Error) return error;
  const value = record(error);
  return {
    name: typeof value?.name === 'string' ? value.name : '',
    message: typeof value?.message === 'string' ? value.message : String(error || ''),
  };
}

function jsonMediaType(contentType: string): string {
  return contentType.split(';', 1)[0].trim().toLowerCase();
}

function isJsonMediaType(contentType: string): boolean {
  const mediaType = jsonMediaType(contentType);
  return mediaType === 'application/json' || mediaType.endsWith('+json');
}

function explicitErrorCode(value: unknown): string | number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) return value.trim();
  return null;
}

function legacyMachineErrorCode(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const candidate = value.trim();
  return /^[a-z][a-z0-9_]*(?:[.:-][a-z0-9_]+)*$/.test(candidate)
    ? candidate : null;
}

function statusErrorCode(status: number): string {
  return STATUS_ERROR_CODES[status] || 'http_error';
}

function httpFailureCode(
  status: number,
  body: Record<string, unknown> | null,
  envelope: ErrorEnvelope | null,
  problem: ApiProblemDetails | null,
): string | number {
  const explicitCandidates = [
    problem?.code,
    body?.error_code,
    body?.error_kind,
    body?.code,
  ];
  for (const candidate of explicitCandidates) {
    const code = explicitErrorCode(candidate);
    if (code !== null) return code;
  }
  return legacyMachineErrorCode(body?.error)
    || envelope?.kind
    || statusErrorCode(status);
}

function httpFailureMessage(
  status: number,
  method: string,
  url: string,
  body: Record<string, unknown> | null,
  envelope: ErrorEnvelope | null,
  problem: ApiProblemDetails | null,
): string {
  const candidates = [
    problem?.detail,
    body?.message,
    envelope?.message,
    typeof body?.error === 'string' ? body.error : null,
    body?.detail,
    body?.title,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
  }
  return `HTTP ${status} on ${method} ${url}`;
}

function bodyRequestId(
  body: Record<string, unknown> | null,
  problem: ApiProblemDetails | null,
): string | null {
  const candidate = problem?.requestId ?? body?.request_id ?? body?.requestId;
  return typeof candidate === 'string' && candidate ? candidate : null;
}

function bootTransportProfile(): string {
  try {
    const raw = globals.document?.getElementById('tofu-boot-config')?.textContent;
    const config = raw ? JSON.parse(raw) as { transportProfile?: unknown } : null;
    return typeof config?.transportProfile === 'string'
      ? config.transportProfile
      : '';
  } catch {
    return '';
  }
}

function detectConstrainedProxy(): boolean {
  const profile = bootTransportProfile();
  if (profile === 'constrained-proxy') return true;
  if (profile === 'direct') return false;
  const pathname = globals.location?.pathname || '';
  return /\/(?:proxy|absproxy)\/\d+(?:\/|$)/.test(pathname);
}

const CONSTRAINED_PROXY = detectConstrainedProxy();

function priorityRank(priority: RequestPriority | undefined): number {
  if (priority === 'foreground') return 0;
  if (priority === 'background') return 2;
  return 1;
}

function abortReason(signal?: AbortSignal): unknown {
  return signal?.reason || new ApiError('aborted', { code: 'aborted' });
}

function clearProxyQueueEntry(entry: ProxyQueueEntry): void {
  if (entry.timeoutId !== null) globalThis.clearTimeout(entry.timeoutId);
  if (entry.signal && entry.abortListener) {
    entry.signal.removeEventListener('abort', entry.abortListener);
  }
}

function drainProxyQueue(): void {
  proxyQueue.sort((left, right) => (
    left.priority - right.priority || left.sequence - right.sequence
  ));
  while (proxyActiveReads < PROXY_READ_CONCURRENCY && proxyQueue.length) {
    const entry = proxyQueue.shift() as ProxyQueueEntry;
    if (entry.signal?.aborted) {
      clearProxyQueueEntry(entry);
      entry.reject(abortReason(entry.signal));
      continue;
    }
    clearProxyQueueEntry(entry);
    proxyActiveReads += 1;
    let released = false;
    entry.resolve(() => {
      if (released) return;
      released = true;
      proxyActiveReads = Math.max(0, proxyActiveReads - 1);
      drainProxyQueue();
    });
  }
}

function acquireProxyReadSlot(options: RequestOptions): Promise<() => void> {
  if (options.signal?.aborted) {
    return Promise.reject(abortReason(options.signal));
  }
  if (proxyActiveReads < PROXY_READ_CONCURRENCY && proxyQueue.length === 0) {
    proxyActiveReads += 1;
    let released = false;
    return Promise.resolve(() => {
      if (released) return;
      released = true;
      proxyActiveReads = Math.max(0, proxyActiveReads - 1);
      drainProxyQueue();
    });
  }
  if (proxyQueue.length >= PROXY_QUEUE_MAX) {
    return Promise.reject(new ApiError(
      'client request queue is full', { code: 'client_queue_full' }));
  }
  return new Promise((resolve, reject) => {
    const entry: ProxyQueueEntry = {
      priority: priorityRank(options.priority),
      sequence: ++proxyQueueSequence,
      signal: options.signal,
      resolve,
      reject,
      abortListener: null,
      timeoutId: null,
    };
    const removeAndReject = (error: unknown) => {
      const index = proxyQueue.indexOf(entry);
      if (index < 0) return;
      proxyQueue.splice(index, 1);
      clearProxyQueueEntry(entry);
      reject(error);
    };
    if (options.signal) {
      entry.abortListener = () => removeAndReject(abortReason(options.signal));
      options.signal.addEventListener('abort', entry.abortListener, { once: true });
    }
    if ((options.timeout || 0) > 0) {
      entry.timeoutId = globalThis.setTimeout(() => {
        removeAndReject(new ApiError('timeout', { code: 'timeout' }));
      }, options.timeout) as unknown as number;
    }
    proxyQueue.push(entry);
    drainProxyQueue();
  });
}

function shouldGovern(method: string, options: RequestOptions): boolean {
  if (!CONSTRAINED_PROXY || options.govern === false) return false;
  if (options.govern === true) return options.parse !== 'response';
  return method === 'GET' && options.parse !== 'response';
}

function coalescingKey(path: string, options: RequestOptions): string | null {
  if (!CONSTRAINED_PROXY || options.coalesce !== true) return null;
  const method = (options.method || 'GET').toUpperCase();
  if (
    method !== 'GET'
    || options.signal
    || options.body !== undefined
    || options.json !== undefined
    || options.parse === 'response'
  ) return null;
  const headers = Object.entries(options.headers || {})
    .map(([key, value]) => [key.toLowerCase(), value] as const)
    .sort(([left], [right]) => left.localeCompare(right));
  if (headers.some(([key]) => [
    'authorization', 'x-bridge-secret', 'x-request-id', 'idempotency-key',
  ].includes(key))) return null;
  return JSON.stringify([
    resolvePath(path) + queryString(options.query),
    options.parse || 'json',
    options.onError || 'throw',
    options.credentials || '',
    options.priority || 'normal',
    affinityFor(options),
    headers,
  ]);
}

function createPageId(): string {
  let pageId = '';
  try {
    if (typeof globals.crypto?.randomUUID === 'function') {
      pageId = globals.crypto.randomUUID().slice(0, 6);
    } else if (typeof globals.crypto?.getRandomValues === 'function') {
      const bytes = new Uint8Array(3);
      globals.crypto.getRandomValues(bytes);
      pageId = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
    }
  } catch {
    // Old WebViews and insecure origins may expose crypto but reject access.
  }
  return pageId || Math.random().toString(36).slice(2, 8);
}

const PAGE_ID = createPageId();

export function pageRequestId(): string {
  return PAGE_ID;
}

function nextRequestId(): string {
  requestSequence += 1;
  return `${PAGE_ID}-${requestSequence}`;
}

export function resolvePath(path: string): string {
  if (/^https?:\/\//.test(path)) return path;
  if (typeof globals.apiUrl === 'function') return globals.apiUrl(path || '');
  const pathname = globals.location?.pathname || '';
  let entry = '';
  try {
    const raw = globals.document?.getElementById('tofu-boot-config')?.textContent;
    const config = raw ? JSON.parse(raw) as { entry?: unknown } : null;
    entry = typeof config?.entry === 'string' ? config.entry : '';
  } catch {
    // A malformed boot tag is diagnosed by the watchdog. URL resolution still
    // falls back to the current page shape so the transport fails visibly.
  }
  const pageSuffix = entry === 'admin' || /\/admin\/?$/.test(pathname)
    ? /\/admin(?:\.html)?\/?$/
    : /\/(?:index\.html|trading\.html)?$/;
  const base = pathname.replace(pageSuffix, '');
  if (!path) return base;
  return base + (path.startsWith('/') ? path : `/${path}`);
}

function queryString(params?: Record<string, unknown>): string {
  if (!params) return '';
  const parts: string[] = [];
  for (const [key, raw] of Object.entries(params)) {
    if (raw === undefined || raw === null) continue;
    const values = Array.isArray(raw) ? raw : [raw];
    for (const value of values) {
      parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
    }
  }
  return parts.length ? `?${parts.join('&')}` : '';
}

function loadAffinities(): void {
  try {
    const raw = globals.sessionStorage?.getItem(AFFINITY_STORAGE_KEY);
    if (!raw) return;
    const saved = record(JSON.parse(raw));
    for (const pair of Array.isArray(saved?.tasks) ? saved.tasks : []) {
      if (Array.isArray(pair) && pair[0] && pair[1]) {
        taskAffinity.set(String(pair[0]), String(pair[1]));
      }
    }
    for (const pair of Array.isArray(saved?.convs) ? saved.convs : []) {
      if (Array.isArray(pair) && pair[0] && pair[1]) {
        conversationAffinity.set(String(pair[0]), String(pair[1]));
      }
    }
  } catch (error) {
    console.warn('[Api] task-affinity restore failed:', errorDetails(error).message);
  }
}

function persistAffinities(): void {
  try {
    if (!globals.sessionStorage) return;
    globals.sessionStorage.setItem(AFFINITY_STORAGE_KEY, JSON.stringify({
      tasks: Array.from(taskAffinity.entries()).slice(-256),
      convs: Array.from(conversationAffinity.entries()).slice(-128),
    }));
  } catch (error) {
    console.warn('[Api] task-affinity persist failed:', errorDetails(error).message);
  }
}

function conversationAffinityKey(convId: unknown): string {
  return convId
    ? `conv-${encodeURIComponent(String(convId)).slice(0, 220)}`
    : '';
}

/** Mint one page-scoped idempotency key for a logical write operation.
 *
 * Endpoint registries may reuse this owner for retry-safe state writes. The
 * key must be created once before retrying so every attempt names the same
 * logical operation; callers must not rebuild it per attempt.
 */
export function newIdempotencyKey(): string {
  try {
    if (typeof globals.crypto?.randomUUID === 'function') {
      return globals.crypto.randomUUID();
    }
  } catch {
    // Fall back to a page-scoped random key below.
  }
  return `${PAGE_ID}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
    + Math.random().toString(36).slice(2);
}

function rememberTaskAffinity(taskId: unknown, key: unknown, convId?: unknown): string {
  if (!key) return '';
  const value = String(key);
  if (taskId) taskAffinity.set(String(taskId), value);
  if (convId) conversationAffinity.set(String(convId), value);
  persistAffinities();
  return value;
}

export function bindTaskAffinity(
  taskId: unknown,
  convId?: unknown,
  explicitKey?: unknown,
): string {
  const key = (explicitKey ? String(explicitKey) : '')
    || (taskId ? taskAffinity.get(String(taskId)) : '')
    || (convId ? conversationAffinity.get(String(convId)) : '')
    || conversationAffinityKey(convId)
    || '';
  if (key && taskId) rememberTaskAffinity(taskId, key, convId);
  return key;
}

function rememberActiveTaskAffinities(value: unknown): unknown {
  const data = value as AffinityRecord | null;
  const items = Array.isArray(data?.items)
    ? data.items
    : Array.isArray(value) ? value : [];
  for (const raw of items) {
    const item = raw as AffinityRecord | null;
    if (item?.id && item.affinityKey) {
      rememberTaskAffinity(item.id, item.affinityKey, item.convId);
    }
  }
  return value;
}

function decorateTaskStartResponse(
  response: Response,
  key: string,
  convId?: string,
): Response {
  const target = response as MutableResponse;
  if (typeof target.json !== 'function') return response;
  const originalJson = target.json.bind(target);
  target.json = async () => {
    const value = await originalJson();
    const data = value as AffinityRecord | null;
    if (data?.taskId) rememberTaskAffinity(data.taskId, key, data.convId || convId);
    return value;
  };
  return target;
}

function decorateActiveResponse(response: Response): Response {
  const target = response as MutableResponse;
  if (typeof target.json !== 'function') return response;
  const originalJson = target.json.bind(target);
  target.json = async () => rememberActiveTaskAffinities(await originalJson());
  return target;
}

function affinityFor(options: RequestOptions): string {
  return options.taskAffinityKey
    || (options.taskId ? taskAffinity.get(String(options.taskId)) : '')
    || (options.convId ? conversationAffinity.get(String(options.convId)) : '')
    || conversationAffinityKey(options.convId)
    || '';
}

function clearRequestLifetime(
  timeoutId: number | null,
  signal: AbortSignal | undefined,
  abortForwarder: (() => void) | null,
): void {
  if (timeoutId !== null) globalThis.clearTimeout(timeoutId);
  if (signal && abortForwarder) signal.removeEventListener('abort', abortForwarder);
}

async function requestDirect<T = unknown>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const method = (options.method || 'GET').toUpperCase();
  const url = resolvePath(path) + queryString(options.query);
  const headers = { ...(options.headers || {}) };
  const affinityKey = affinityFor(options);
  if (affinityKey && !headers['X-Tofu-Affinity-Key']) {
    headers['X-Tofu-Affinity-Key'] = affinityKey;
  }
  const requestId = headers['X-Request-ID'] || nextRequestId();
  headers['X-Request-ID'] = requestId;

  let body = options.body;
  if (options.json !== undefined) {
    headers['Content-Type'] ||= 'application/json';
    body = JSON.stringify(options.json);
  }
  const init: RequestInit = { method, headers };
  if (body !== undefined) init.body = body;
  if (options.keepalive) init.keepalive = true;
  if (options.credentials) init.credentials = options.credentials;

  const userSignal = options.signal;
  const timeout = options.timeout || 0;
  let timeoutId: number | null = null;
  let abortForwarder: (() => void) | null = null;
  let timeoutTriggered = false;
  if (timeout > 0 && typeof AbortController !== 'undefined') {
    const controller = new AbortController();
    init.signal = controller.signal;
    timeoutId = globalThis.setTimeout(() => {
      timeoutTriggered = true;
      controller.abort(new ApiError('timeout', {
        url,
        code: 'timeout',
        clientRequestId: requestId,
        requestId,
      }));
    }, timeout);
    if (userSignal) {
      abortForwarder = () => controller.abort(userSignal.reason);
      if (userSignal.aborted) abortForwarder();
      else userSignal.addEventListener('abort', abortForwarder, { once: true });
    }
  } else if (userSignal) {
    init.signal = userSignal;
  }

  let response: Response;
  try {
    response = await fetch(url, init);
  } catch (error) {
    clearRequestLifetime(timeoutId, userSignal, abortForwarder);
    const details = errorDetails(error);
    const signalReason = init.signal?.aborted ? init.signal.reason : null;
    const reasonDetails = errorDetails(signalReason);
    const reasonIsTimeout = signalReason instanceof ApiError
      ? signalReason.code === 'timeout'
      : reasonDetails.name === 'TimeoutError';
    let failure: ApiError;
    if (timeoutTriggered || reasonIsTimeout || details.name === 'TimeoutError') {
      failure = signalReason instanceof ApiError && signalReason.code === 'timeout'
        ? signalReason
        : new ApiError(reasonDetails.message || details.message || 'timeout', {
          url,
          code: 'timeout',
          clientRequestId: requestId,
          requestId,
        });
    } else if (userSignal?.aborted || details.name === 'AbortError') {
      failure = new ApiError(reasonDetails.message || details.message || 'aborted', {
        url,
        code: 'aborted',
        clientRequestId: requestId,
        requestId,
      });
    } else if (error instanceof ApiError) {
      failure = error;
      failure.url ||= url;
      failure.clientRequestId ||= requestId;
      failure.requestId ||= requestId;
    } else {
      failure = new ApiError(details.message || 'network error', {
        url,
        code: 'network',
        clientRequestId: requestId,
        requestId,
      });
    }
    if (options.onError === 'null') {
      console.warn(
        '[Api] %s %s failed: %s [rid=%s]',
        method, url, failure.message, failure.requestId,
      );
      return null as T;
    }
    throw failure;
  }
  clearRequestLifetime(timeoutId, userSignal, abortForwarder);

  const parse = options.parse || 'json';
  if (parse === 'response') {
    if (options.rememberActiveAffinities) {
      return decorateActiveResponse(response) as T;
    }
    return (options.rememberTaskAffinity
      ? decorateTaskStartResponse(response, affinityKey, options.convId)
      : response) as T;
  }

  if (!response.ok) {
    let bodyData: unknown = null;
    let bodyParseFailed = false;
    const contentType = response.headers.get('content-type') || '';
    try {
      bodyData = isJsonMediaType(contentType)
        ? await response.json()
        : await response.text();
    } catch (error) {
      bodyParseFailed = true;
      console.warn(
        '[Api] failed to parse error body for %s %s (HTTP %s): %s',
        method, url, response.status, errorDetails(error).message,
      );
    }
    const bodyRecord = record(bodyData);
    const declaredProblem = jsonMediaType(contentType) === 'application/problem+json';
    const parsedProblem = normalizeApiProblemDetails(bodyData);
    const problem = declaredProblem && parsedProblem?.status === response.status
      ? parsedProblem : null;
    const rawError = bodyRecord?.error ?? null;
    const envelope = problem ? null : normalizeErrorEnvelope(rawError);
    const code = bodyParseFailed ? 'invalid_error_body'
      : declaredProblem && !problem ? 'invalid_problem'
        : httpFailureCode(response.status, bodyRecord, envelope, problem);
    const serverRequestId = response.headers.get('X-Request-ID');
    const failure = new ApiError(
      bodyParseFailed
        ? `Could not parse HTTP ${response.status} error response`
        : declaredProblem && !problem
          ? `Invalid API problem response for HTTP ${response.status}`
          : httpFailureMessage(
            response.status, method, url, bodyRecord, envelope, problem,
          ),
      {
        status: response.status,
        code,
        body: bodyData,
        url,
        envelope,
        problem,
        clientRequestId: requestId,
        serverRequestId,
        requestId: bodyRequestId(bodyRecord, problem) || serverRequestId || requestId,
      },
    );
    if (options.onError === 'null') {
      console.warn('[Api] %s [rid=%s]', failure.message, failure.requestId);
      return null as T;
    }
    throw failure;
  }

  if (parse === 'none') return null as T;
  if (parse === 'text') return await response.text() as T;
  if (parse === 'blob') return await response.blob() as T;
  let text: string;
  try {
    text = await response.text();
  } catch (error) {
    const serverRequestId = response.headers.get('X-Request-ID');
    throw new ApiError(errorDetails(error).message || 'response body read failed', {
      status: response.status,
      code: 'network',
      url,
      clientRequestId: requestId,
      serverRequestId,
      requestId: serverRequestId || requestId,
    });
  }
  if (!text) return null as T;
  try {
    const value = JSON.parse(text) as unknown;
    const data = value as AffinityRecord | null;
    if (options.rememberTaskAffinity && data?.taskId) {
      rememberTaskAffinity(data.taskId, affinityKey, data.convId || options.convId);
    }
    if (options.rememberActiveAffinities) rememberActiveTaskAffinities(value);
    return value as T;
  } catch {
    if (options.onError === 'null') {
      console.warn('[Api] %s %s returned non-JSON', method, url);
      return null as T;
    }
    const serverRequestId = response.headers.get('X-Request-ID');
    throw new ApiError('invalid JSON response', {
      status: response.status,
      url,
      code: 'parse',
      body: text,
      clientRequestId: requestId,
      serverRequestId,
      requestId: serverRequestId || requestId,
    });
  }
}

async function governedRequest<T>(
  path: string,
  options: RequestOptions,
): Promise<T> {
  let effectiveOptions = options;
  const rpcStartedAt = Date.now();
  if (CONSTRAINED_PROXY && options.rpcMethod) {
    const rpc = globals.pushRpcRequest;
    if (typeof rpc === 'function') {
      try {
        return await rpc<T>(
          options.rpcMethod,
          options.rpcParams || record(options.json) || {},
          { timeout: options.timeout, signal: options.signal },
        );
      } catch (error) {
        if (options.signal?.aborted) throw abortReason(options.signal);
        const failure = record(error);
        const code = failure?.code;
        const mayFallback = code === 'rpc_unavailable'
          || code === 'rpc_disconnected'
          || code === -32601;
        if (!mayFallback) {
          if (options.onError === 'null') return null as T;
          const status = code === -32001 || code === 'rpc_overloaded' ? 503
            : code === -32002 || code === 'rpc_timeout' ? 504
              : code === -32602 || code === -32010 ? 400
                : code === -32800 ? 499 : 500;
          throw new ApiError(
            typeof failure?.message === 'string'
              ? failure.message : 'Control RPC failed',
            {
              status,
              code: explicitErrorCode(code) ?? 'rpc_error',
              body: failure?.data,
              url: resolvePath(path),
            },
          );
        }
        effectiveOptions = { ...options, rpcMethod: undefined };
        if ((options.timeout || 0) > 0) {
          const remaining = Number(options.timeout)
            - (Date.now() - rpcStartedAt);
          if (remaining <= 0) {
            if (options.onError === 'null') return null as T;
            throw new ApiError('timeout', {
              code: 'timeout', url: resolvePath(path),
            });
          }
          effectiveOptions.timeout = remaining;
        }
      }
    }
  }

  const method = (effectiveOptions.method || 'GET').toUpperCase();
  if (!shouldGovern(method, effectiveOptions)) {
    return requestDirect<T>(path, effectiveOptions);
  }
  const queuedAt = Date.now();
  let release: (() => void) | null = null;
  try {
    release = await acquireProxyReadSlot(effectiveOptions);
  } catch (error) {
    if (effectiveOptions.onError === 'null') return null as T;
    throw error;
  }
  const forwarded = { ...effectiveOptions };
  if ((effectiveOptions.timeout || 0) > 0) {
    forwarded.timeout = Math.max(
      1,
      Number(effectiveOptions.timeout) - (Date.now() - queuedAt),
    );
  }
  try {
    return await requestDirect<T>(path, forwarded);
  } finally {
    release();
  }
}

export function request<T = unknown>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const key = coalescingKey(path, options);
  if (!key) return governedRequest<T>(path, options);
  const existing = coalescedGets.get(key);
  if (existing) return existing as Promise<T>;
  const promise = governedRequest<T>(path, options);
  if (coalescedGets.size >= COALESCED_GET_MAX) return promise;
  coalescedGets.set(key, promise);
  const clear = () => {
    if (coalescedGets.get(key) === promise) coalescedGets.delete(key);
  };
  promise.then(clear, clear);
  return promise;
}

export function taskStartAffinityOptions(
  body: unknown,
  options: RequestOptions = {},
): RequestOptions {
  const payload = record(body);
  const convId = typeof payload?.convId === 'string' ? payload.convId : '';
  const key = options.taskAffinityKey
    || (convId ? conversationAffinity.get(convId) : '')
    || conversationAffinityKey(convId)
    || newIdempotencyKey();
  return { taskAffinityKey: key, rememberTaskAffinity: true, convId };
}

loadAffinities();

export interface ApiTransport {
  ApiError: typeof ApiError;
  request: typeof request;
  resolvePath: typeof resolvePath;
  pageRequestId: typeof pageRequestId;
  bindTaskAffinity: typeof bindTaskAffinity;
  taskStartAffinityOptions: typeof taskStartAffinityOptions;
  newIdempotencyKey: typeof newIdempotencyKey;
}

export const apiTransport: ApiTransport = Object.freeze({
  ApiError,
  request,
  resolvePath,
  pageRequestId,
  bindTaskAffinity,
  taskStartAffinityOptions,
  newIdempotencyKey,
});

/** Keep ``instanceof ApiError`` stable while classic domains use this owner. */
export function installLegacyApiBindings(): void {
  if (globals.Api) globals.Api.ApiError = ApiError;
}

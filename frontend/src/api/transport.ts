import { isErrorEnvelope, type ErrorEnvelope } from './errors';

export type ParseMode = 'json' | 'text' | 'blob' | 'response' | 'none';

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
  taskId?: string;
  convId?: string;
  taskAffinityKey?: string;
  rememberTaskAffinity?: boolean;
  rememberActiveAffinities?: boolean;
  [key: string]: unknown;
}

export interface ApiErrorOptions {
  status?: number;
  code?: unknown;
  body?: unknown;
  url?: string | null;
  requestId?: string | null;
  clientRequestId?: string | null;
  serverRequestId?: string | null;
  envelope?: ErrorEnvelope | null;
}

export class ApiError extends Error {
  status: number;
  code: unknown;
  body: unknown;
  url: string | null;
  requestId: string | null;
  clientRequestId: string | null;
  serverRequestId: string | null;
  envelope: ErrorEnvelope | null;

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
  }
}

type MutableResponse = Response & {
  json: () => Promise<unknown>;
};

type ApiGlobals = Window & typeof globalThis & {
  apiUrl?: (path: string) => string;
  Api?: Record<string, unknown>;
  ApiError?: typeof ApiError;
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

function newTaskAffinityKey(): string {
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

export async function request<T = unknown>(
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
  if (timeout > 0 && typeof AbortController !== 'undefined') {
    const controller = new AbortController();
    init.signal = controller.signal;
    timeoutId = globalThis.setTimeout(() => {
      controller.abort(new ApiError('timeout', { url, code: 'timeout' }));
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
    if (options.onError === 'null') {
      console.warn(
        '[Api] %s %s failed: %s [rid=%s]',
        method, url, errorDetails(error).message, requestId,
      );
      return null as T;
    }
    const details = errorDetails(error);
    if (details.name === 'AbortError' || details.name === 'TimeoutError') throw error;
    const failure = new ApiError(details.message || 'network error', {
      url,
      code: 'network',
      clientRequestId: requestId,
      requestId,
    });
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
    try {
      const contentType = response.headers.get('content-type') || '';
      bodyData = contentType.includes('application/json')
        ? await response.json()
        : await response.text();
    } catch (error) {
      console.warn(
        '[Api] failed to parse error body for %s %s (HTTP %s): %s',
        method, url, response.status, errorDetails(error).message,
      );
    }
    const bodyRecord = record(bodyData);
    const code = bodyRecord?.error ?? null;
    const envelope = isErrorEnvelope(code) ? code : null;
    const failure = new ApiError(
      envelope?.message || `HTTP ${response.status} on ${method} ${url}`,
      {
        status: response.status,
        code,
        body: bodyData,
        url,
        envelope,
        clientRequestId: requestId,
      },
    );
    const serverRequestId = response.headers.get('X-Request-ID');
    failure.serverRequestId = serverRequestId || null;
    failure.requestId = typeof bodyRecord?.request_id === 'string' && bodyRecord.request_id
      ? bodyRecord.request_id
      : serverRequestId || requestId;
    if (options.onError === 'null') {
      console.warn('[Api] %s [rid=%s]', failure.message, failure.requestId);
      return null as T;
    }
    throw failure;
  }

  if (parse === 'none') return null as T;
  if (parse === 'text') return await response.text() as T;
  if (parse === 'blob') return await response.blob() as T;
  const text = await response.text();
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
    throw new ApiError('invalid JSON response', { url, code: 'parse', body: text });
  }
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
    || newTaskAffinityKey();
  return { taskAffinityKey: key, rememberTaskAffinity: true, convId };
}

loadAffinities();

export interface ApiTransport {
  ApiError: typeof ApiError;
  request: typeof request;
  pageRequestId: typeof pageRequestId;
  bindTaskAffinity: typeof bindTaskAffinity;
  taskStartAffinityOptions: typeof taskStartAffinityOptions;
}

export const apiTransport: ApiTransport = Object.freeze({
  ApiError,
  request,
  pageRequestId,
  bindTaskAffinity,
  taskStartAffinityOptions,
});

/** Keep ``instanceof ApiError`` stable while classic domains use this owner. */
export function installLegacyApiBindings(): void {
  if (globals.Api) globals.Api.ApiError = ApiError;
}

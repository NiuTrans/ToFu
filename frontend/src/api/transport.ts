import {
  normalizeApiProblemDetails,
  normalizeErrorEnvelope,
  type ApiProblemDetails,
  type ErrorEnvelope,
} from './errors';

import { readBrowserStorage, writeBrowserStorage } from '../core/browser-storage';
import { BoundedMap } from '../core/bounded-map';
import {
  createNativeReauthGate,
  isGatewayAuthRejection,
  type NativeShellHandle,
} from '../core/native-bridge';

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
  /** Explicitly merge identical, safe GETs while they are in flight. */
  coalesce?: boolean;
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
  /** When truthy, resolvePath rewrites `/api/v1/...` to `/api/v4/...` and the
   * success path unwraps the v4 `{data, meta}` envelope to the flat payload
   * callers already consume. Flippable at runtime (resolvePath reads it per
   * call); mirrors the server-side `API_V4_ENABLED` latch. */
  TOFU_API_V4?: boolean;
  Api?: Record<string, unknown>;
  ApiError?: typeof ApiError;
  pushRpcRequest?: <T = unknown>(
    method: string,
    params: Record<string, unknown>,
    options: { timeout?: number; signal?: AbortSignal },
  ) => Promise<T>;
};

const nativeReauthGate = createNativeReauthGate({
  native: (window as Window & { TofuNative?: NativeShellHandle }).TofuNative,
  now: () => Date.now(),
  onError: (error) => console.warn('[Api] native reauth request failed:', error),
});

/**
 * A gateway-shaped 401 means the outer edge session is dead; every queued
 * request will bounce the same way until the shell re-logins. Forward once
 * per window (the gate rate-limits) and let the normal error path continue.
 */
function maybeRequestNativeReauth(
  status: number,
  envelope: ErrorEnvelope | null,
  problem: ApiProblemDetails | null,
  method: string,
  url: string,
): void {
  if (!isGatewayAuthRejection(status, envelope, problem)) return;
  nativeReauthGate.requestReauth(`api ${status} ${method} ${url}`);
}

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
const TASK_AFFINITY_MAX_ENTRIES = 256;
const CONVERSATION_AFFINITY_MAX_ENTRIES = 128;
const AFFINITY_ID_MAX_CHARACTERS = 256;
const AFFINITY_KEY_MAX_CHARACTERS = 256;
const taskAffinity = new BoundedMap<string, string>(TASK_AFFINITY_MAX_ENTRIES);
const conversationAffinity = new BoundedMap<string, string>(
  CONVERSATION_AFFINITY_MAX_ENTRIES,
);
let requestSequence = 0;

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
const coalescedGets = new Map<string, Promise<unknown>>();

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
  requestId: string,
): string {
  const error = record(body?.error);
  const firstText = (...values: unknown[]): string => {
    for (const value of values) {
      if (typeof value === 'string' && value.trim()) return value.trim();
    }
    return '';
  };
  // Problem `detail` is the public occurrence text. For a 500, retain its
  // category title as context for a safe detail such as an exception class.
  const headline = problem
    ? (status === 500 ? problem.title : problem.detail)
    : firstText(
      error?.message, body?.message, envelope?.message,
      typeof body?.error === 'string' ? body.error : null,
      body?.title,
    );
  const detail = problem
    ? (status === 500 ? problem.detail : '')
    : firstText(error?.detail, body?.detail);
  const hint = problem ? '' : firstText(error?.hint, body?.hint);
  const parts: string[] = [];
  const title = status === 500 && headline === 'internal_error'
    ? 'Internal server error' : headline;
  if (title) parts.push(title);
  for (const value of [body?.message, detail, hint]) {
    const text = typeof value === 'string' ? value.trim() : '';
    if (text && !parts.some((part) => part.includes(text))) parts.push(text);
  }
  if (!parts.length) parts.push(`HTTP ${status} on ${method} ${url}`);
  // Redacted 500s still need an occurrence handle for correlated server logs.
  if (status === 500 && requestId) parts.push(`Request ID: ${requestId}`);
  return parts.join('\n');
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

function abortReason(signal?: AbortSignal): unknown {
  return signal?.reason || new ApiError('aborted', { code: 'aborted' });
}

function coalescingKey(path: string, options: RequestOptions): string | null {
  if (options.coalesce !== true) return null;
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
    options.timeout || 0,
    options.rpcMethod || '',
    options.rpcParams || null,
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

/** Rewrite a `/api/v1/...` request path to `/api/v4/...` when the v4 flag
 * is on. The v4 surface is a byte-level projection of v1 (same handlers,
 * `{data, meta}` envelope), so only the prefix changes. */
/** Whether the served surface is v4. Mirrors the server release policy: the
 * canonical contract is cutover + activeByDefault, so default ON; set
 * `window.TOFU_API_V4 = false` only when serving a still-transitional
 * (v1-default) backend. Read per call so it is runtime-flippable. */
function v4Active(): boolean {
  return globals.TOFU_API_V4 !== false;
}

function resolveApiPath(path: string): string {
  if (v4Active() && path.startsWith('/api/v1/')) {
    return `/api/v4/${path.slice('/api/v1/'.length)}`;
  }
  return path;
}

export function resolvePath(path: string): string {
  if (/^https?:\/\//.test(path)) return path;
  path = resolveApiPath(path);
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
    const raw = readBrowserStorage(AFFINITY_STORAGE_KEY, 'session');
    if (!raw) return;
    const saved = record(JSON.parse(raw));
    const savedTasks = Array.isArray(saved?.tasks) ? saved.tasks : [];
    for (const pair of savedTasks.slice(-TASK_AFFINITY_MAX_ENTRIES)) {
      if (!Array.isArray(pair)) continue;
      const id = affinityRecordId(pair[0]);
      const key = affinityKey(pair[1]);
      if (id && key) taskAffinity.set(id, key);
    }
    const savedConversations = Array.isArray(saved?.convs) ? saved.convs : [];
    for (const pair of savedConversations.slice(-CONVERSATION_AFFINITY_MAX_ENTRIES)) {
      if (!Array.isArray(pair)) continue;
      const id = affinityRecordId(pair[0]);
      const key = affinityKey(pair[1]);
      if (id && key) conversationAffinity.set(id, key);
    }
  } catch (error) {
    console.warn('[Api] task-affinity restore failed:', errorDetails(error).message);
  }
}

function persistAffinities(): void {
  try {
    writeBrowserStorage(AFFINITY_STORAGE_KEY, JSON.stringify({
      tasks: Array.from(taskAffinity.entries()),
      convs: Array.from(conversationAffinity.entries()),
    }), 'session');
  } catch (error) {
    console.warn('[Api] task-affinity persist failed:', errorDetails(error).message);
  }
}

function conversationAffinityKey(convId: unknown): string {
  const id = affinityRecordId(convId);
  return id
    ? `conv-${encodeURIComponent(id).slice(0, 220)}`
    : '';
}

function affinityRecordId(value: unknown): string {
  if (!value) return '';
  const id = String(value);
  return id.length <= AFFINITY_ID_MAX_CHARACTERS ? id : '';
}

function affinityKey(value: unknown): string {
  return value ? String(value).slice(0, AFFINITY_KEY_MAX_CHARACTERS) : '';
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
  const value = affinityKey(key);
  if (!value) return '';
  const normalizedTaskId = affinityRecordId(taskId);
  const normalizedConversationId = affinityRecordId(convId);
  if (normalizedTaskId) taskAffinity.set(normalizedTaskId, value);
  if (normalizedConversationId) {
    conversationAffinity.set(normalizedConversationId, value);
  }
  persistAffinities();
  return value;
}

export function bindTaskAffinity(
  taskId: unknown,
  convId?: unknown,
  explicitKey?: unknown,
): string {
  const normalizedTaskId = affinityRecordId(taskId);
  const normalizedConversationId = affinityRecordId(convId);
  const key = affinityKey(explicitKey)
    || (normalizedTaskId ? taskAffinity.get(normalizedTaskId) : '')
    || (normalizedConversationId
      ? conversationAffinity.get(normalizedConversationId) : '')
    || conversationAffinityKey(normalizedConversationId)
    || '';
  if (key && normalizedTaskId) {
    rememberTaskAffinity(normalizedTaskId, key, normalizedConversationId);
  }
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

function isV4RequestUrl(url: string): boolean {
  try {
    return /\/api\/v4(?:\/|$)/.test(new URL(url, 'http://tofu.invalid').pathname);
  } catch {
    return false;
  }
}

function unwrapV4Json(value: unknown, url: string): unknown {
  const envelope = record(value);
  if (!isV4RequestUrl(url) || !envelope || !record(envelope.meta)
      || !Object.prototype.hasOwnProperty.call(envelope, 'data')) return value;
  const data = record(envelope.data);
  // Retained consumers and SDKs keep the pre-cutover success discriminator.
  // Preserve an explicit domain flag, and unwrap only the outer wire layer.
  return data ? { ok: true, ...data } : envelope.data;
}

const normalizedJsonResponses = new WeakSet<Response>();

/** Keep the native Response and byte/stream readers; normalize only JSON.
 * Clones inherit the same projection without reading or buffering a body. */
function decorateV4JsonResponse(response: Response, url: string): Response {
  if (!response.ok || !isV4RequestUrl(url) || normalizedJsonResponses.has(response)) {
    return response;
  }
  const target = response as MutableResponse;
  if (typeof target.json !== 'function') return response;
  normalizedJsonResponses.add(response);
  const originalJson = target.json.bind(target);
  target.json = async () => unwrapV4Json(await originalJson(), url);
  if (typeof target.clone === 'function') {
    const originalClone = target.clone.bind(target);
    target.clone = () => decorateV4JsonResponse(originalClone(), url);
  }
  return target;
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
  const taskId = affinityRecordId(options.taskId);
  const convId = affinityRecordId(options.convId);
  return affinityKey(options.taskAffinityKey)
    || (taskId ? taskAffinity.get(taskId) : '')
    || (convId ? conversationAffinity.get(convId) : '')
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

/** Consume parsed responses inside the request's deadline/cancellation scope.
 * Raw Response mode transfers body ownership to its caller at the headers. */
async function readDirectResponse<T>(
  response: Response,
  options: RequestOptions,
  method: string,
  url: string,
  requestId: string,
  affinityKey: string,
): Promise<T> {
  const parse = options.parse || 'json';
  if (parse === 'response') {
    response = decorateV4JsonResponse(response, url);
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
    const responseRequestId = bodyRequestId(bodyRecord, problem)
      || serverRequestId || requestId;
    const failure = new ApiError(
      bodyParseFailed
        ? `Could not parse HTTP ${response.status} error response`
        : declaredProblem && !problem
          ? `Invalid API problem response for HTTP ${response.status}`
          : httpFailureMessage(
            response.status, method, url, bodyRecord, envelope, problem,
            responseRequestId,
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
        requestId: responseRequestId,
      },
    );
    maybeRequestNativeReauth(response.status, envelope, problem, method, url);
    if (options.onError === 'null') {
      console.warn('[Api] %s [rid=%s]', failure.message, failure.requestId);
      return null as T;
    }
    throw failure;
  }

  if (parse === 'none') {
    // No consumer will own this successful response's body. Release it without
    // allowing a stalled cancellation acknowledgement to hold the request.
    void response.body?.cancel().catch(() => undefined);
    return null as T;
  }
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
    const parsed = JSON.parse(text) as unknown;
    const value = unwrapV4Json(parsed, url);
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

  try {
    const response = await fetch(url, init);
    return await readDirectResponse<T>(
      response, options, method, url, requestId, affinityKey,
    );
  } catch (error) {
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
  } finally {
    clearRequestLifetime(timeoutId, userSignal, abortForwarder);
  }
}

async function rpcLaneRequest<T>(
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

  // No client-side read queue: the browser's own connection limits and the
  // server are the only governors. A queued-then-stale request cost far
  // more than proxy admission ever saved (post-restart boot salvo starved
  // foreground clicks for tens of seconds behind cold-storage reads).
  return requestDirect<T>(path, effectiveOptions);
}

export function request<T = unknown>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const key = coalescingKey(path, options);
  if (!key) return rpcLaneRequest<T>(path, options);
  const existing = coalescedGets.get(key);
  if (existing) return existing as Promise<T>;
  const promise = rpcLaneRequest<T>(path, options);
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
  const convId = affinityRecordId(payload?.convId);
  const key = affinityKey(options.taskAffinityKey)
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

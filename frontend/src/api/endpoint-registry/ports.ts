/** Construct endpoint dependencies once per registry. Delegates transport; retains no response cache or request state. */
import type { ApiTransport, RequestOptions } from '../transport';
import type { JsonObject } from './contracts';
import { createIdempotentWrite } from './idempotent-write';
import { resolveAcrossWorkerRecovery } from './resolver-recovery';
export function createEndpointPorts(transport: ApiTransport, readLanguage: () => string) {
  const request = <T = JsonObject | null>(path: string, supplied?: RequestOptions | null) => transport.request<T>(path, supplied || {});
  const get = <T = JsonObject | null>(path: string, opts: RequestOptions = {}) => request<T>(path, { method: 'GET', ...opts });
  const post = <T = JsonObject | null>(path: string, json?: unknown, opts: RequestOptions = {}) => request<T>(path, { method: 'POST', json, ...opts });
  const put = <T = JsonObject | null>(path: string, json?: unknown, opts: RequestOptions = {}) => request<T>(path, { method: 'PUT', json, ...opts });
  const patch = <T = JsonObject | null>(path: string, json?: unknown, opts: RequestOptions = {}) => request<T>(path, { method: 'PATCH', json, ...opts });
  const del = <T = JsonObject | null>(path: string, opts: RequestOptions = {}) => request<T>(path, { method: 'DELETE', ...opts });
  const stream = (path: string, opts: RequestOptions = {}) => request<Response | null>(path, { method: 'GET', timeout: 0, parse: 'response', ...opts });
  const requiredItemsList = (data: JsonObject | null, path: string): unknown[] => {
    if (data && data.ok !== false && Array.isArray(data.items)) return data.items;
    throw new transport.ApiError(`invalid list response from ${path}`, { code: 'parse', url: transport.resolvePath(path), body: data });
  };
  return { request, get, post, put, patch, del, stream, requiredItemsList, readLanguage,
    ApiError: transport.ApiError, _resolve: transport.resolvePath, newIdempotencyKey: transport.newIdempotencyKey,
    taskStartAffinityOptions: (body: unknown, opts?: RequestOptions | null) => transport.taskStartAffinityOptions(body, opts || {}), _bindTaskAffinity: transport.bindTaskAffinity,
    _idempotentResponseWrite: createIdempotentWrite(request, transport.newIdempotencyKey),
    _resolveAcrossWorkerRecovery: resolveAcrossWorkerRecovery };
}

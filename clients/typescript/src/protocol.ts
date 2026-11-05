/** Pure wire projection, typed errors and native request validation; no network ownership. */
import type { NativeModelSelection, RoutingPolicy } from './types.js';

export class TofuError extends Error {
  readonly kind: string;
  readonly retryable: boolean;
  readonly retryAfter: number | null;

  constructor(public status: number, public body: unknown) {
    super(`Tofu API error ${status}: ${typeof body === 'string' ? body : JSON.stringify(body)}`);
    this.name = 'TofuError';
    const payload = body && typeof body === 'object' && !Array.isArray(body)
      ? body as Record<string, unknown> : {};
    const nested = payload.error;
    const nestedKind = nested && typeof nested === 'object' && 'kind' in nested
      ? nested.kind : '';
    this.kind = String(nestedKind || payload.error_kind || payload.code || '');
    this.retryable = status === 429 || status >= 500;
    const retryAfter = Number(payload.retry_after);
    this.retryAfter = Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null;
  }
}

export function taskTerminal(payload: Record<string, unknown>): boolean {
  const terminal = ['done', 'error', 'aborted'];
  return terminal.includes(String(payload.type || ''))
    || terminal.includes(String(payload.event || ''));
}

export function parseSequence(value: unknown): number | undefined {
  const numeric = typeof value === 'number' ? value
    : typeof value === 'string' && /^[+-]?[0-9]+$/.test(value.trim()) ? Number(value) : NaN;
  return Number.isInteger(numeric) ? numeric : undefined;
}

export function unwrapEnvelope(body: unknown): unknown {
  if (body && typeof body === 'object' && !Array.isArray(body)
        && 'data' in body && 'meta' in body && body.meta
        && typeof body.meta === 'object' && !Array.isArray(body.meta)) {
    const data = (body as { data: unknown }).data;
    return data && typeof data === 'object' && !Array.isArray(data)
      ? { ok: true, ...data } : data;
  }
  return body;
}

export function newIdempotencyKey(): string {
  const cryptoValue = globalThis.crypto as Crypto | undefined;
  if (cryptoValue?.randomUUID) return cryptoValue.randomUUID();
  return `tofu-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export function normalizeNativeModelSelection(value: unknown): NativeModelSelection {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError(
      'model must be {creator_id, model_id} or {provider_id, offering_id}');
  }
  const source = value as Record<string, unknown>;
  const keys = Object.keys(source).sort();
  const official = keys.join(',') === 'creator_id,model_id';
  const providerScoped = keys.join(',') === 'offering_id,provider_id';
  if (!official && !providerScoped) {
    throw new TypeError(
      'model must contain exactly creator_id+model_id or provider_id+offering_id');
  }
  const normalized = Object.fromEntries(
    keys.map(key => [key, String(source[key] ?? '').trim()]),
  ) as Record<string, string>;
  if (Object.values(normalized).some(valuePart => !valuePart)) {
    throw new TypeError('model identity fields must be non-empty strings');
  }
  return normalized as NativeModelSelection;
}

export function nativeRequest<T extends {
  model?: NativeModelSelection;
  routing?: RoutingPolicy;
}>(
  request: T,
  options: { modelRequired: boolean },
): T {
  const wire = { ...request } as T & Record<string, unknown>;
  if (Object.prototype.hasOwnProperty.call(wire, 'provider')) {
    throw new TypeError(
      'inline provider blocks were removed; configure model-routing v2');
  }
  if (wire.model === undefined) {
    if (options.modelRequired) throw new TypeError('model is required');
  } else {
    wire.model = normalizeNativeModelSelection(wire.model);
  }
  if (wire.routing !== undefined) {
    if (!wire.routing || typeof wire.routing !== 'object'
        || Array.isArray(wire.routing)) {
      throw new TypeError('routing must be an object');
    }
    wire.routing = { ...(wire.routing as Record<string, unknown>) };
  }
  return wire;
}

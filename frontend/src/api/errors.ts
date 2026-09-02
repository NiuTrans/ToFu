export interface ErrorEnvelope {
  kind: string;
  message: string;
  severity: 'warning' | 'error' | string;
  retryable: boolean;
  hint: string;
  detail: string;
  model: string;
  context: string;
  source: string;
  raw: unknown;
  [key: string]: unknown;
}

/** RFC 7807 plus the extensions required by Tofu's API v4 contract. */
export interface ApiProblemDetails {
  type: string;
  title: string;
  status: number;
  detail: string;
  instance: string;
  code: string;
  requestId: string;
  upgradeUrl?: string;
}

const API_PROBLEM_REQUIRED_KEYS = [
  'type', 'title', 'status', 'detail', 'instance', 'code', 'requestId',
] as const;
const API_PROBLEM_ALLOWED_KEYS = new Set<string>([
  ...API_PROBLEM_REQUIRED_KEYS, 'upgradeUrl',
]);

export function normalizeApiProblemDetails(value: unknown): ApiProblemDetails | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const candidate = value as Record<string, unknown>;
  if (!API_PROBLEM_REQUIRED_KEYS.every((key) => Object.hasOwn(candidate, key))
      || !Object.keys(candidate).every((key) => API_PROBLEM_ALLOWED_KEYS.has(key))) {
    return null;
  }
  if (typeof candidate.type !== 'string' || !candidate.type
      || typeof candidate.title !== 'string' || !candidate.title
      || !Number.isInteger(candidate.status)
      || (candidate.status as number) < 400 || (candidate.status as number) > 599
      || typeof candidate.detail !== 'string' || !candidate.detail
      || typeof candidate.instance !== 'string' || !candidate.instance
      || typeof candidate.code !== 'string'
      || !/^[a-z][a-z0-9_]*$/.test(candidate.code)
      || typeof candidate.requestId !== 'string' || !candidate.requestId
      || (Object.hasOwn(candidate, 'upgradeUrl')
        && (typeof candidate.upgradeUrl !== 'string' || !candidate.upgradeUrl))) {
    return null;
  }
  return candidate as unknown as ApiProblemDetails;
}

export function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.kind === 'string'
    && typeof candidate.message === 'string'
    && typeof candidate.severity === 'string'
    && typeof candidate.retryable === 'boolean'
    && typeof candidate.hint === 'string'
    && typeof candidate.detail === 'string'
    && typeof candidate.model === 'string'
    && typeof candidate.context === 'string'
    && typeof candidate.source === 'string'
    && Object.hasOwn(candidate, 'raw');
}

export function normalizeErrorEnvelope(value: unknown): ErrorEnvelope | null {
  if (value == null || value === '') return null;
  if (isErrorEnvelope(value)) return value;
  if (typeof value === 'object' && !Array.isArray(value)) {
    const candidate = value as Record<string, unknown>;
    if (typeof candidate.kind === 'string'
        && typeof candidate.message === 'string') {
      let serialized = '';
      try { serialized = JSON.stringify(candidate); } catch { /* no-op */ }
      return {
        ...candidate,
        kind: candidate.kind,
        severity: typeof candidate.severity === 'string'
          ? candidate.severity : 'error',
        retryable: typeof candidate.retryable === 'boolean'
          ? candidate.retryable : false,
        message: candidate.message,
        hint: typeof candidate.hint === 'string' ? candidate.hint : '',
        detail: typeof candidate.detail === 'string'
          ? candidate.detail : serialized,
        model: typeof candidate.model === 'string' ? candidate.model : '',
        context: typeof candidate.context === 'string' ? candidate.context : '',
        source: typeof candidate.source === 'string'
          ? candidate.source : 'frontend-normalizer',
        raw: Object.hasOwn(candidate, 'raw') ? candidate.raw : serialized,
      };
    }
  }
  if (typeof value === 'string') {
    const offline = /server offline/i.test(value);
    return {
      kind: offline ? 'server_offline' : 'generic',
      severity: 'warning',
      retryable: offline,
      message: value,
      hint: '',
      detail: value,
      model: '',
      context: '',
      source: 'frontend-legacy',
      raw: value,
    };
  }
  let detail = '';
  try { detail = JSON.stringify(value); } catch { /* non-serializable input */ }
  return {
    kind: 'generic',
    severity: 'error',
    retryable: false,
    message: 'Unknown error',
    hint: '',
    detail,
    model: '',
    context: '',
    source: 'frontend-unknown',
    raw: '',
  };
}

/**
 * Compact identity for the fields that can change an error's presentation.
 *
 * Retained render gates used to read ``error.length``. That works for legacy
 * strings but produces ``undefined`` for the structured envelopes emitted by
 * the current API, so two visibly different failures could be mistaken for
 * the same render. Keep shape normalization and fingerprint semantics in the
 * same typed owner instead of teaching every renderer both wire formats.
 */
export function errorEnvelopeFingerprint(value: unknown): string {
  const envelope = normalizeErrorEnvelope(value);
  if (!envelope) return '';
  return [
    envelope.kind,
    envelope.severity,
    envelope.retryable ? '1' : '0',
    envelope.message,
    envelope.hint,
    envelope.detail,
    envelope.model,
    envelope.context,
    envelope.source,
  ].map((part) => String(part ?? '')).join('\u001f');
}

export interface ApiFailure {
  message: string;
  status: number;
  code: string | number | null;
  requestId: string | null;
  clientRequestId: string | null;
  serverRequestId: string | null;
  envelope: ErrorEnvelope | null;
  problem: ApiProblemDetails | null;
  cause: unknown;
}

export function apiFailure(error: unknown): ApiFailure {
  const candidate = error && typeof error === 'object'
    ? error as Record<string, unknown>
    : {};
  const body = candidate.body && typeof candidate.body === 'object'
    ? candidate.body as Record<string, unknown>
    : null;
  const problemInput = Object.hasOwn(candidate, 'problem')
    ? candidate.problem : body;
  const envelopeInput = Object.hasOwn(candidate, 'envelope')
    ? candidate.envelope : body?.error;
  const problem = normalizeApiProblemDetails(problemInput);
  const envelope = normalizeErrorEnvelope(envelopeInput);
  const candidateCode = candidate.code;
  return {
    message: error instanceof Error ? error.message : String(error || 'Unknown error'),
    status: typeof candidate.status === 'number' ? candidate.status : 0,
    code: typeof candidateCode === 'string' || typeof candidateCode === 'number'
      ? candidateCode : problem?.code ?? envelope?.kind ?? null,
    requestId: typeof candidate.requestId === 'string' ? candidate.requestId : null,
    clientRequestId: typeof candidate.clientRequestId === 'string' ? candidate.clientRequestId : null,
    serverRequestId: typeof candidate.serverRequestId === 'string' ? candidate.serverRequestId : null,
    envelope,
    problem,
    cause: error,
  };
}

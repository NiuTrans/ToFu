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

export function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.kind === 'string' && typeof candidate.message === 'string';
}

export function normalizeErrorEnvelope(value: unknown): ErrorEnvelope | null {
  if (value == null || value === '') return null;
  if (isErrorEnvelope(value)) return value;
  if (typeof value === 'string') {
    const offline = /server offline/i.test(value);
    return {
      kind: offline ? 'server_offline' : 'generic',
      severity: 'warning',
      retryable: offline,
      message: value,
      hint: '',
      detail: value.slice(0, 300),
      model: '',
      context: '',
      source: 'frontend-legacy',
      raw: value,
    };
  }
  let detail = '';
  try { detail = JSON.stringify(value).slice(0, 300); } catch { /* non-serializable input */ }
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

export interface ApiFailure {
  message: string;
  status: number;
  code: unknown;
  requestId: string | null;
  clientRequestId: string | null;
  serverRequestId: string | null;
  envelope: ErrorEnvelope | null;
  cause: unknown;
}

export function apiFailure(error: unknown): ApiFailure {
  const candidate = error && typeof error === 'object'
    ? error as Record<string, unknown>
    : {};
  const body = candidate.body && typeof candidate.body === 'object'
    ? candidate.body as Record<string, unknown>
    : null;
  return {
    message: error instanceof Error ? error.message : String(error || 'Unknown error'),
    status: typeof candidate.status === 'number' ? candidate.status : 0,
    code: candidate.code ?? null,
    requestId: typeof candidate.requestId === 'string' ? candidate.requestId : null,
    clientRequestId: typeof candidate.clientRequestId === 'string' ? candidate.clientRequestId : null,
    serverRequestId: typeof candidate.serverRequestId === 'string' ? candidate.serverRequestId : null,
    envelope: normalizeErrorEnvelope(candidate.envelope ?? body?.error),
    cause: error,
  };
}

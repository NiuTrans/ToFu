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
  statusCode?: number;
  [key: string]: unknown;
}

/** Structured execution context stamped on a terminal failure envelope by
 * the backend (`failureAttribution` extension). Every field is best-effort:
 * present only when the failing seam knew it. */
export interface ErrorFailureAttribution {
  roundNum?: number;
  phase?: string;
  toolName?: string;
  toolCallId?: string;
}

/** Typed read of the additive `failureAttribution` extension. The envelope
 * normalizer passes unknown keys through, so this never re-derives — it only
 * narrows what the backend already stamped. */
export function errorFailureAttribution(
  envelope: ErrorEnvelope | null | undefined,
): ErrorFailureAttribution | null {
  const candidate = envelope?.failureAttribution;
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) {
    return null;
  }
  const record = candidate as Record<string, unknown>;
  const attribution: ErrorFailureAttribution = {};
  if (Number.isInteger(record.roundNum) && (record.roundNum as number) > 0) {
    attribution.roundNum = record.roundNum as number;
  }
  if (typeof record.phase === 'string' && record.phase) {
    attribution.phase = record.phase;
  }
  if (typeof record.toolName === 'string' && record.toolName) {
    attribution.toolName = record.toolName;
  }
  if (typeof record.toolCallId === 'string' && record.toolCallId) {
    attribution.toolCallId = record.toolCallId;
  }
  return Object.keys(attribution).length ? attribution : null;
}

/** One live task entry in a lifecycle restart refusal receipt. */
export interface ApiProblemRunningTask {
  taskId?: string;
  convId?: string;
  elapsed?: number;
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
  /** Lifecycle receipts declared in contracts/api_v4.yaml (update routes). */
  needsForce?: boolean;
  runningTasks?: ApiProblemRunningTask[];
  restartPreflightFailed?: boolean;
  retryAfterSec?: number;
  restartPending?: boolean;
}

const API_PROBLEM_REQUIRED_KEYS = [
  'type', 'title', 'status', 'detail', 'instance', 'code', 'requestId',
] as const;
const API_PROBLEM_ALLOWED_KEYS = new Set<string>([
  ...API_PROBLEM_REQUIRED_KEYS,
  'upgradeUrl',
  'needsForce',
  'runningTasks',
  'restartPreflightFailed',
  'retryAfterSec',
  'restartPending',
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
        && (typeof candidate.upgradeUrl !== 'string' || !candidate.upgradeUrl))
      || (Object.hasOwn(candidate, 'needsForce')
        && typeof candidate.needsForce !== 'boolean')
      || (Object.hasOwn(candidate, 'runningTasks')
        && (!Array.isArray(candidate.runningTasks)
          || !(candidate.runningTasks as unknown[]).every(
            (entry) => !!entry && typeof entry === 'object' && !Array.isArray(entry))))
      || (Object.hasOwn(candidate, 'restartPreflightFailed')
        && typeof candidate.restartPreflightFailed !== 'boolean')
      || (Object.hasOwn(candidate, 'retryAfterSec')
        && (!Number.isInteger(candidate.retryAfterSec)
          || (candidate.retryAfterSec as number) < 0))
      || (Object.hasOwn(candidate, 'restartPending')
        && typeof candidate.restartPending !== 'boolean')) {
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
        // A legacy partial envelope has no occurrence detail. Keep its raw
        // serialization for diagnostics, never invent JSON for user text.
        detail: typeof candidate.detail === 'string'
          ? candidate.detail : '',
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
    Number.isInteger(envelope.statusCode) ? envelope.statusCode : '',
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

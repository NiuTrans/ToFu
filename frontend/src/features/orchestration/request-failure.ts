import { orchestrationRegistry } from './registry';
import { record, type ContractRecord } from './contracts';

export interface RequestFailureOptions {
  keys?: Readonly<Record<string, string>>;
  defaultKey?: string;
  params?: Readonly<Record<string, unknown>>;
}

export interface RequestFailurePresentation {
  key: string;
  reason: string;
  status: number;
  notFound: boolean;
}

type RequestFailureWindow = Window & {
  projectOrchestrationRequestFailure?:
    typeof projectOrchestrationRequestFailure;
  orchestrationRequestFailureKey?: typeof orchestrationRequestFailureKey;
  orchestrationRequestFailureMessage?:
    typeof orchestrationRequestFailureMessage;
};

const FAILURE_KEYS: Readonly<Record<string, string>> = Object.freeze({
  'api-unavailable': 'orch.api.unavailable',
  'not-found': 'orch.request.notFound',
  'server-failed': 'orch.request.serverFailed',
  'request-rejected': 'orch.request.rejected',
  'malformed-response': 'orch.request.malformedResponse',
  'transport-failed': 'orch.request.transportFailed',
  'builtin-rejected': 'orch.request.operationRejected',
  'layout-rejected': 'orch.request.operationRejected',
  'read-rejected': 'orch.request.operationRejected',
  'save-rejected': 'orch.request.operationRejected',
  'delete-rejected': 'orch.request.operationRejected',
  'write-conflict': 'orch.request.operationRejected',
  'unsupported-format': 'orch.request.malformedResponse',
  'list-rejected': 'orch.request.operationRejected',
  'create-rejected': 'orch.request.operationRejected',
});

function requestFailureResult(value: unknown): ContractRecord | null {
  let result = record(value);
  const seen = new Set<ContractRecord>();
  while (result && !result.reason && result.notFound !== true) {
    if (seen.has(result)) break;
    seen.add(result);
    const nested = record(result.result) ?? record(result.response);
    if (!nested) break;
    result = nested;
  }
  return result;
}

export function projectOrchestrationRequestFailure(
  value: unknown,
  options: RequestFailureOptions = {},
): Readonly<RequestFailurePresentation> {
  const result = requestFailureResult(value);
  const reason = String(result?.reason || '');
  const status = Number(result?.status || result?.httpStatus || 0);
  const notFound = result?.notFound === true
    || reason === 'not-found' || status === 404;
  let key = notFound ? 'orch.request.notFound'
    : String(options.keys?.[reason] || FAILURE_KEYS[reason] || '');
  if (!key && status >= 500) key = 'orch.request.serverFailed';
  if (!key && status >= 400) key = 'orch.request.rejected';
  if (!key && (result?.cause || result?.error)) {
    key = 'orch.request.transportFailed';
  }
  key ||= String(options.defaultKey || 'orch.request.operationRejected');
  return Object.freeze({ key, reason, status, notFound });
}

export function orchestrationRequestFailureKey(
  result: unknown,
  options?: RequestFailureOptions,
): string {
  return projectOrchestrationRequestFailure(result, options).key;
}

export function orchestrationRequestFailureMessage(
  result: unknown,
  translate?: (key: string, params?: Record<string, unknown>) => unknown,
  fallback?: unknown,
  options: RequestFailureOptions = {},
): string {
  const failure = projectOrchestrationRequestFailure(result, options);
  const params = { reason: failure.reason, status: failure.status,
    ...options.params };
  const translated = String(
    translate ? translate(failure.key, params) ?? '' : failure.key);
  if (translated && translated !== failure.key) return translated;
  return String(fallback || translated || failure.key);
}

Object.assign(orchestrationRegistry as unknown as RequestFailureWindow, {
  projectOrchestrationRequestFailure,
  orchestrationRequestFailureKey,
  orchestrationRequestFailureMessage,
});

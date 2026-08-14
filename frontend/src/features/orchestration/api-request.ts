import { orchestrationRegistry } from './registry';
import { record, type ContractRecord } from './contracts';
import { adaptHttpResult } from '../../core/http-result';

export interface ApiRequestSpec {
  resultMethod: string;
  directMethod: string;
  resultArgs?: readonly unknown[];
  directArgs?: readonly unknown[];
  args?: readonly unknown[];
  normalize: (value: unknown) => ContractRecord;
  adaptDirect?: (value: unknown) => unknown | Promise<unknown>;
}

export interface OrchestrationApiRequestInvoker {
  available(resultMethod: string, directMethod: string): boolean;
  request(spec: ApiRequestSpec): Promise<ContractRecord>;
}

export interface ApiRequestInvokerOptions {
  api?: unknown | (() => unknown);
}

type ApiRequestWindow = Window & {
  createOrchestrationApiRequestInvoker?:
    typeof createOrchestrationApiRequestInvoker;
};

export function createOrchestrationApiRequestInvoker(
  options: ApiRequestInvokerOptions = {},
): OrchestrationApiRequestInvoker {
  const api = (): ContractRecord | null => {
    const value = typeof options.api === 'function'
      ? options.api() : options.api;
    return record(value);
  };

  const available = (resultMethod: string, directMethod: string): boolean => {
    const client = api();
    return Boolean(client) && (typeof client?.[resultMethod] === 'function'
      || typeof client?.[directMethod] === 'function');
  };

  const failure = (
    normalize: ApiRequestSpec['normalize'],
    error: unknown,
    unavailable: boolean,
    requestMethod: unknown,
    usedResultMethod: boolean,
  ): ContractRecord => {
    const status = Number(record(error)?.status || 0);
    const result = normalize({
      ok: false,
      status,
      data: { error },
    });
    result.available = !unavailable;
    result.requestMethod = String(requestMethod || '');
    result.usedResultMethod = Boolean(usedResultMethod);
    if (unavailable) result.reason = 'api-unavailable';
    if (error) result.cause = error;
    return result;
  };

  const request = async (spec: ApiRequestSpec): Promise<ContractRecord> => {
    const client = api();
    const hasResult = Boolean(client)
      && typeof client?.[spec.resultMethod] === 'function';
    const method = hasResult ? client?.[spec.resultMethod]
      : client?.[spec.directMethod];
    const requestMethod = hasResult ? spec.resultMethod : spec.directMethod;
    if (typeof method !== 'function') {
      return failure(spec.normalize, null, true, '', false);
    }
    try {
      const args = hasResult
        ? (spec.resultArgs ?? spec.args ?? [])
        : (spec.directArgs ?? spec.args ?? []);
      const raw = await method.apply(client, args);
      const value = !hasResult && typeof spec.adaptDirect === 'function'
        ? await spec.adaptDirect(raw)
        : await adaptHttpResult(raw);
      const result = spec.normalize(value);
      result.available = true;
      result.requestMethod = String(requestMethod || '');
      result.usedResultMethod = hasResult;
      result.raw = raw;
      const cause = record(value)?.cause;
      if (cause) result.cause = cause;
      return result;
    } catch (error) {
      return failure(
        spec.normalize, error, false, requestMethod, hasResult);
    }
  };

  return { available, request };
}

Object.assign(orchestrationRegistry as unknown as ApiRequestWindow, {
  createOrchestrationApiRequestInvoker,
});

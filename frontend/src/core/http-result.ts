/**
 * Responsibility: project fetch-like responses into one status-preserving
 * result envelope and recover the canonical error at presentation boundaries.
 * Entry points: normalizeHttpResult, adaptHttpResult, httpResultError, and the
 * immutable HTTP_RESULT port. Dependencies: none; response values are injected.
 */
export interface HttpResultEnvelope {
  ok: boolean;
  status: number;
  data: object;
  cause?: unknown;
}

export interface HttpResultApi {
  normalize(value: unknown): Promise<HttpResultEnvelope>;
  adapt(value: unknown): Promise<unknown>;
  error(value: unknown): unknown;
}

type ResponseLike = {
  ok?: unknown;
  status?: unknown;
  json(): Promise<unknown>;
};

function responseLike(value: unknown): value is ResponseLike {
  return Boolean(value) && typeof (value as ResponseLike).json === 'function';
}

function projectHttpResult(
  value: unknown,
  passthrough: false,
): Promise<HttpResultEnvelope>;
function projectHttpResult(
  value: unknown,
  passthrough: true,
): Promise<unknown>;
async function projectHttpResult(
  value: unknown,
  passthrough: boolean,
): Promise<unknown> {
  let response: unknown;
  try {
    response = await value;
  } catch (error) {
    if (passthrough) throw error;
    const failure: HttpResultEnvelope = {
      ok: false,
      status: Number((error as { status?: unknown } | null)?.status || 0),
      data: {},
    };
    Object.defineProperty(failure, 'cause', {
      value: error,
      enumerable: false,
    });
    return failure;
  }
  if (!responseLike(response)) {
    return passthrough ? response : { ok: false, status: 0, data: {} };
  }
  const data = await response.json().catch(() => ({}));
  return {
    ok: Boolean(response.ok),
    status: Number(response.status || 0),
    data: data && typeof data === 'object' ? data : {},
  };
}

export function normalizeHttpResult(
  value: unknown,
): Promise<HttpResultEnvelope> {
  return projectHttpResult(value, false);
}

export function adaptHttpResult(value: unknown): Promise<unknown> {
  return projectHttpResult(value, true);
}

export function httpResultError(value: unknown): unknown {
  if (value == null || value === '') return null;
  if (typeof value === 'string') return value;
  if (typeof value !== 'object') return null;
  const candidate = value as Record<string, unknown>;
  if (candidate.cause) return candidate.cause;
  const data = candidate.data;
  if (data && typeof data === 'object'
      && Object.prototype.hasOwnProperty.call(data, 'error')) {
    return (data as Record<string, unknown>).error;
  }
  if (Object.prototype.hasOwnProperty.call(candidate, 'error')) {
    return candidate.error;
  }
  return typeof candidate.message === 'string' ? value : null;
}

export const HTTP_RESULT: Readonly<HttpResultApi> = Object.freeze({
  normalize: normalizeHttpResult,
  adapt: adaptHttpResult,
  error: httpResultError,
});

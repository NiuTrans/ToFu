/** Retry only declared target-state writes; preserve one key and response/error semantics across ambiguous acknowledgements. */
import type { RequestOptions } from '../transport';
import type { JsonRequest } from './contracts';
import { waitForRetry, retryAfterMs } from './retry-delay';
const RETRYABLE = new Set([429, 502, 503, 504]);
export function createIdempotentWrite(request: JsonRequest, newKey: () => string) {
  return async (path: string, supplied: RequestOptions = {}): Promise<Response | null> => {
    const options: RequestOptions = { parse: 'response', onError: 'null', ...supplied };
    options.headers = { ...options.headers };
    if (!options.headers['Idempotency-Key']) options.headers['Idempotency-Key'] = newKey();
    let response: Response | null = null;
    for (let attempt = 0; attempt < 3; attempt++) {
      if (options.signal?.aborted) return response;
      response = await request<Response>(path, options);
      if (response?.ok) return response;
      if (!(response == null || RETRYABLE.has(Number(response.status))) || attempt + 1 >= 3) {
        if (response && !response.ok) console.warn('[Api] idempotent write failed after %d attempt(s): %s %s (HTTP %s)', attempt + 1, options.method || 'POST', path, response.status);
        return response;
      }
      if (!await waitForRetry(retryAfterMs(response, attempt), options.signal)) return response;
    }
    return response;
  };
}

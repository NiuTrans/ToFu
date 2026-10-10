/** Recover read-only structured resolvers across worker replacement. Backend-authored and mutation failures never enter this boundary. */
import { waitForRetry } from './retry-delay';
const RETRYABLE = new Set([500, 502, 503, 504]);
function canRecover(error: unknown): boolean {
  const detail = error && typeof error === 'object' ? error as Record<string, unknown> : {};
  const status = Number(detail.status || 0), code = String(detail.code || '');
  return status === 0 ? code === 'network' || code === 'timeout'
    : RETRYABLE.has(status) && !detail.serverRequestId;
}
export async function resolveAcrossWorkerRecovery<T>(operation: (timeout: number) => Promise<T>, label: string): Promise<T> {
  const deadline = Date.now() + 75000;
  let lastError: unknown = null;
  for (let attempt = 0; attempt < 20; attempt++) {
    const remaining = deadline - Date.now();
    if (attempt > 0 && remaining <= 0) throw lastError;
    try {
      const result = await operation(Math.max(1, Math.min(10000, remaining)));
      if (attempt > 0) console.info('[Api] %s recovered after %d retry attempt(s)', label, attempt);
      return result;
    } catch (error) {
      lastError = error;
      if (!canRecover(error) || attempt + 1 >= 20) throw error;
      const delay = Math.min(Math.min(5000, 250 * Math.pow(2, attempt)), deadline - Date.now());
      if (delay <= 0) throw error;
      if (attempt === 0) console.warn('[Api] %s interrupted outside the backend; waiting for worker recovery', label);
      if (!await waitForRetry(delay)) throw error;
    }
  }
  throw lastError;
}

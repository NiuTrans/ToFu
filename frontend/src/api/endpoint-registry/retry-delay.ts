/** Abortable retry waiting. Release the timeout and abort subscription at every settlement. */
export function waitForRetry(delayMs: number, signal?: AbortSignal): Promise<boolean> {
  if (signal?.aborted) return Promise.resolve(false);
  return new Promise(resolve => {
    let settled = false;
    const finish = (ready: boolean) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener('abort', onAbort);
      resolve(ready);
    };
    const timer = setTimeout(() => finish(true), delayMs);
    const onAbort = () => { clearTimeout(timer); finish(false); };
    signal?.addEventListener('abort', onAbort, { once: true });
    if (signal?.aborted) onAbort();
  });
}
export function retryAfterMs(response: Response | null, retryIndex: number): number {
  let advised: number | null = null;
  try {
    const raw = response?.headers.get('Retry-After');
    if (raw != null && raw !== '') {
      const seconds = Number(raw);
      if (Number.isFinite(seconds)) advised = Math.max(0, seconds * 1000);
      else { const at = Date.parse(raw); if (Number.isFinite(at)) advised = Math.max(0, at - Date.now()); }
    }
  } catch { /* malformed headers use bounded local backoff */ }
  const fallback = 150 * Math.pow(2, retryIndex) + Math.random() * 100;
  return Math.min(5000, advised == null ? fallback : advised);
}

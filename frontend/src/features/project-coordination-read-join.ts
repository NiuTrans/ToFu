/** Same-task join for identical project-coordination reads.
 *
 * Responsibility: callers that issue an identical read within one task share
 * one request, and each caller keeps its own cancellation. push.js hands every
 * project frame to the canvas and the sidebar surface in one synchronous loop,
 * so the reads both surfaces issue for that frame join. A microtask retires
 * each entry. A read issued in a later task, such as the read for a later
 * frame, therefore always starts a new request and never receives the
 * response to a request issued before it.
 * Entry: createSameTaskReadJoin, bound by the retained presence adapter.
 * Dependencies: queueMicrotask and AbortController only.
 * Joined callers receive the same result object, so consumers never mutate it.
 */
interface JoinedRead {
  shared: AbortController;
  callers: number;
  result: Promise<unknown>;
}

export function createSameTaskReadJoin() {
  const inFlight = new Map<string, JoinedRead>();
  /** `identity` names the endpoint and every argument except the signal. */
  return function joinRead<T>(identity: readonly unknown[], signal: AbortSignal | undefined,
    read: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const key = JSON.stringify(identity);
    let entry = inFlight.get(key);
    if (!entry) {
      const shared = new AbortController();
      const created: JoinedRead = { shared, callers: 0, result: Promise.resolve(read(shared.signal)) };
      inFlight.set(key, created);
      queueMicrotask(() => { if (inFlight.get(key) === created) inFlight.delete(key); });
      entry = created;
    }
    const joined = entry;
    joined.callers++;
    if (!signal) return joined.result as Promise<T>;
    return new Promise<T>((resolve, reject) => {
      const leave = () => {
        reject(signal.reason);
        if (--joined.callers > 0) return;
        // A request every caller has left is never joined again, even within this task.
        if (inFlight.get(key) === joined) inFlight.delete(key);
        joined.shared.abort(signal.reason);
      };
      joined.result.then(value => {
        signal.removeEventListener('abort', leave); resolve(value as T);
      }, (error: unknown) => {
        signal.removeEventListener('abort', leave); reject(error);
      });
      if (signal.aborted) leave();
      else signal.addEventListener('abort', leave, { once: true });
    });
  };
}

export type SendStartupAbortReason =
  | ''
  | 'timeout'
  | 'user-stop'
  | 'unmount'
  | 'superseded';

export interface SendStartupOwner {
  _genStartCtrl?: AbortController | null;
  _genStartStop?: AbortController | null;
}

export interface SendStartupLease {
  readonly controller: AbortController;
  readonly signal: AbortSignal;
  readonly reason: SendStartupAbortReason;
  ownsMarkers(): boolean;
  stoppedByUser(): boolean;
  abort(reason: Exclude<SendStartupAbortReason, ''>): void;
  finish(): void;
}

export interface SendStartupLeaseOptions {
  timeoutMs?: number;
  controllerFactory?: () => AbortController;
}

/** Bound only preparation/read work. The caller must await this before any
 * command write, so a late preparation result cannot submit after timeout. */
export function awaitSendPreparation<T>(work: Promise<T>, message: string,
  signal?: AbortSignal, timeoutMs = 30_000): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const abort = () => finish(() => reject(new DOMException(message, 'AbortError')));
    const timer = globalThis.setTimeout(() =>
      finish(() => reject(new DOMException(message, 'TimeoutError'))), timeoutMs);
    let settled = false;
    const finish = (complete: () => void) => {
      if (settled) return;
      settled = true;
      globalThis.clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      complete();
    };
    signal?.addEventListener('abort', abort, { once: true });
    // Attach both outcomes even if already aborted: the original work may
    // still reject later and must never leak an unhandled rejection.
    work.then(value => finish(() => resolve(value)), error => finish(() => reject(error)));
    if (signal?.aborted) abort();
  });
}

/** Serialize draft submissions per conversation; a slow ACK cannot block
 * sending in another conversation. Repeated intent drains only after a
 * confirmed acceptance, never after an uncertain or failed write. */
export function createComposerSubmissionGate() {
  const pending = new Map<string, { requested: boolean; promise: Promise<void> }>();
  return {
    isPending: (key: string): boolean => pending.has(key),
    run(key: string, submit: () => Promise<boolean>): Promise<void> {
      const existing = pending.get(key);
      if (existing) {
        existing.requested = true;
        return existing.promise;
      }
      let resolve!: () => void;
      let reject!: (reason: unknown) => void;
      const lane = { requested: false, promise: new Promise<void>((yes, no) => {
        resolve = yes;
        reject = no;
      }) };
      // Claim before invoking synchronously: paint startup in the input event
      // while keeping re-entrant UI hooks on the same promise.
      pending.set(key, lane);
      const execute = async () => {
        try {
          do {
            lane.requested = false;
            if (!await submit()) break;
          } while (lane.requested);
        } finally {
          if (pending.get(key) === lane) pending.delete(key);
        }
      };
      void execute().then(resolve, reject);
      return lane.promise;
    },
  };
}

/**
 * Own cancellation, deadline and identity-guarded conversation markers for
 * one generation-start request. The legacy stop button may still abort the
 * exposed controller directly; its owner tag is reflected by `reason`.
 */
export function createSendStartupLease(
  owner: SendStartupOwner,
  options: SendStartupLeaseOptions = {},
): SendStartupLease {
  const controller = options.controllerFactory?.() ?? new AbortController();
  const timeoutMs = Number(options.timeoutMs ?? 90_000);
  let abortReason: SendStartupAbortReason = '';
  let finished = false;

  owner._genStartCtrl = controller;
  owner._genStartStop = null;

  const timeoutId = Number.isFinite(timeoutMs) && timeoutMs > 0
    ? globalThis.setTimeout(() => {
        if (finished || controller.signal.aborted) return;
        abortReason = 'timeout';
        controller.abort();
      }, timeoutMs)
    : null;

  const stoppedByUser = (): boolean => owner._genStartStop === controller;
  const ownsMarkers = (): boolean => (
    owner._genStartCtrl === controller || stoppedByUser()
  );
  const finish = (): void => {
    if (finished) return;
    finished = true;
    if (timeoutId !== null) globalThis.clearTimeout(timeoutId);
    if (ownsMarkers()) {
      owner._genStartCtrl = null;
      owner._genStartStop = null;
    }
  };

  return {
    controller,
    signal: controller.signal,
    get reason() { return stoppedByUser() ? 'user-stop' : abortReason; },
    ownsMarkers,
    stoppedByUser,
    abort(reason) {
      if (finished || controller.signal.aborted) return;
      abortReason = reason;
      controller.abort();
    },
    finish,
  };
}

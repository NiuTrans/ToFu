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

import { waitForRetry } from '../api/endpoint-registry/retry-delay';
import { isTransientApiFailure } from '../api/errors';
import { notifyObserver } from './observer';

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

/* Longer than the 75 s config-resolution recovery window, so a deadline
 * never cuts a resolver short; it only reclaims an attempt that hangs. */
const SEND_STARTUP_ATTEMPT_TIMEOUT_MS = 90_000;
const SEND_STARTUP_RETRY_BASE_DELAY_MS = 1_000;
const SEND_STARTUP_RETRY_MAX_DELAY_MS = 15_000;

/** Capped exponential backoff with jitter: about 1, 2, 4, 8, then 15 s. */
export function sendStartupRetryDelayMs(retryIndex: number): number {
  const exponential = SEND_STARTUP_RETRY_BASE_DELAY_MS * 2 ** Math.min(retryIndex, 4);
  return Math.round(Math.min(SEND_STARTUP_RETRY_MAX_DELAY_MS,
    exponential * (0.8 + Math.random() * 0.4)));
}

export interface SendStartupRetry {
  /** 1 for the first automatic retry. */
  readonly retry: number;
  readonly delayMs: number;
  readonly error: unknown;
}

export interface SendStartupRetryOptions<T> {
  /** The send's user-cancellation scope; aborting it ends retrying. */
  signal: AbortSignal;
  run(attemptSignal: AbortSignal): Promise<T>;
  onRetry?(retry: SendStartupRetry): void;
  /** One attempt's deadline; an expired attempt is aborted and repeated. */
  attemptTimeoutMs?: number;
  retryDelayMs?(retryIndex: number): number;
}

/**
 * Repeat one send-startup phase until it succeeds, the user cancels, or a
 * definitive failure occurs, so a transient failure never asks the user to
 * resend. The caller keeps its inputs and command identity frozen across
 * attempts, which makes a repeat exactly as safe as the idempotent command.
 * Each attempt owns its signal and is released before the backoff, so an
 * expired attempt cannot keep retrying behind its successor.
 *
 * `run` must honor its attempt signal. Its settlement is awaited rather than
 * raced: a write's acknowledgement can cross a stop or a deadline, and an
 * accepted command must reach the caller instead of being discarded.
 */
export async function retryTransientSendStartup<T>(
  options: SendStartupRetryOptions<T>,
): Promise<T> {
  const { signal } = options;
  const cancelled = () => signal.reason ?? new DOMException('Send cancelled', 'AbortError');
  for (let retryIndex = 0; ; retryIndex += 1) {
    if (signal.aborted) throw cancelled();
    const attempt = new AbortController();
    let expired = false;
    const forwardCancel = () => attempt.abort(signal.reason);
    signal.addEventListener('abort', forwardCancel, { once: true });
    const timer = globalThis.setTimeout(() => {
      expired = true;
      attempt.abort(new DOMException('Send attempt deadline expired', 'TimeoutError'));
    }, options.attemptTimeoutMs ?? SEND_STARTUP_ATTEMPT_TIMEOUT_MS);
    let failure: unknown;
    try {
      return await options.run(attempt.signal);
    } catch (error) {
      failure = error;
    } finally {
      globalThis.clearTimeout(timer);
      signal.removeEventListener('abort', forwardCancel);
    }
    if (signal.aborted || !(expired || isTransientApiFailure(failure))) throw failure;
    const delayMs = (options.retryDelayMs ?? sendStartupRetryDelayMs)(retryIndex);
    notifyObserver('send-startup-retry', options.onRetry,
      { retry: retryIndex + 1, delayMs, error: failure });
    if (!await waitForRetry(delayMs, signal)) throw cancelled();
  }
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

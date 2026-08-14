import { orchestrationRegistry } from './registry';
export interface TaskReplayEvent extends Record<string, unknown> {
  seq?: unknown;
}

export interface TaskReplayCursor extends Record<string, unknown> {
  requested?: unknown;
  next?: unknown;
  reset?: unknown;
}

export interface TaskReplayPage extends Record<string, unknown> {
  ok?: unknown;
  cause?: unknown;
  events?: unknown;
  next_cursor?: unknown;
  cursor?: unknown;
  format?: unknown;
  caught_up?: unknown;
  done?: unknown;
  replayCanonical?: boolean;
  replayComplete?: boolean;
}

export interface NormalizedTaskReplayPage extends TaskReplayPage {
  events: TaskReplayEvent[];
  next_cursor: number;
  cursor: {
    requested: number;
    next: number;
    reset: boolean;
  };
  replayCanonical: boolean;
  caught_up: boolean;
  replayComplete: boolean;
}

export interface CursorPollFailure<TContext, TPage extends TaskReplayPage> {
  attempt: number;
  cursor: number;
  context: TContext | null;
  response: (TPage & NormalizedTaskReplayPage) | null;
  error: unknown;
  delay: number;
  willRetry: boolean;
}

export interface CursorPollRecovery<TContext> {
  attempts: number;
  cursor: number;
  context: TContext | null;
}

export interface CursorPollerOptions<
  TContext,
  TPage extends TaskReplayPage = TaskReplayPage,
> {
  setTimeout?: (callback: () => void, delay: number) => number;
  clearTimeout?: (timer: number) => void;
  accept?: (context: TContext | null) => boolean | void;
  pause?: (context: TContext | null) => boolean;
  pauseDelay?: number;
  request?: (
    context: TContext | null,
    cursor: number,
  ) => TPage | null | Promise<TPage | null>;
  maxFailures?: number;
  retryBase?: number;
  retryMax?: number;
  retryable?: (failure: CursorPollFailure<TContext, TPage>) => boolean | void;
  onFailure?: (failure: CursorPollFailure<TContext, TPage>) => void;
  onGiveUp?: (failure: CursorPollFailure<TContext, TPage>) => void;
  onRecovered?: (recovery: CursorPollRecovery<TContext>) => void;
  onResponse?: (
    response: TPage & NormalizedTaskReplayPage,
    context: TContext | null,
    cursor: number,
  ) => unknown | Promise<unknown>;
  onConsumerError?: (error: unknown, context: TContext | null) => void;
  onDone?: (
    response: TPage & NormalizedTaskReplayPage,
    context: TContext | null,
  ) => void;
  interval?: number;
}

export interface OrchestrationCursorPoller<TContext> {
  start(startCursor?: number | null, context?: TContext): number;
  stop(): void;
  isActive(): boolean;
  cursor(): number;
  failures(): number;
}

type CursorPollerWindow = Window & {
  orchestrationWireFormat?: (name: string) => unknown;
  normalizeTaskReplayPage?: typeof normalizeTaskReplayPage;
  createOrchestrationCursorPoller?: typeof createOrchestrationCursorPoller;
};

function finiteNonNegative(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? Math.max(0, parsed) : fallback;
}

/** Normalize rolling replay envelopes without permitting accidental rewind. */
export function normalizeTaskReplayPage(
  value: unknown,
  requestedCursor: unknown,
): NormalizedTaskReplayPage | unknown {
  if (!value || typeof value !== 'object') return value;
  const page = value as TaskReplayPage;
  const requested = finiteNonNegative(requestedCursor, 0);
  const cursorMeta = page.cursor && typeof page.cursor === 'object'
    ? page.cursor as TaskReplayCursor : {};
  const events = Array.isArray(page.events)
    ? page.events as TaskReplayEvent[] : [];
  const rawNext = cursorMeta.next != null
    ? Number(cursorMeta.next) : Number(page.next_cursor);
  let nextCursor = Number.isFinite(rawNext)
    ? Math.max(0, rawNext) : requested;
  let reset = cursorMeta.reset === true;

  if (nextCursor < requested && events.length === 0) reset = true;
  if (!reset) {
    let deliveredNext = requested;
    events.forEach((event, index) => {
      const sequence = Number(event?.seq);
      deliveredNext = Math.max(
        deliveredNext,
        Number.isFinite(sequence) ? sequence + 1 : requested + index + 1,
      );
    });
    nextCursor = Math.max(nextCursor, requested, deliveredNext);
  }

  const wireFormat = (orchestrationRegistry as unknown as CursorPollerWindow).orchestrationWireFormat;
  const caughtUp = page.caught_up !== false;
  return {
    ...page,
    events,
    next_cursor: nextCursor,
    cursor: {
      requested: finiteNonNegative(cursorMeta.requested, requested),
      next: nextCursor,
      reset,
    },
    replayCanonical: page.format === wireFormat?.('task-replay'),
    caught_up: caughtUp,
    replayComplete: page.done === true && caughtUp,
  };
}

/** Resilient stale-safe cursor transport shared by Studio and Task Mode. */
export function createOrchestrationCursorPoller<
  TContext extends Record<string, unknown> = Record<string, unknown>,
  TPage extends TaskReplayPage = TaskReplayPage,
>(
  options: CursorPollerOptions<TContext, TPage> = {},
): OrchestrationCursorPoller<TContext> {
  let active = false;
  let generation = 0;
  let cursor = 0;
  let failures = 0;
  let timer: number | null = null;
  let context: TContext | null = null;

  function later(callback: () => void, delay: number): number {
    return (options.setTimeout ?? window.setTimeout)(callback, delay);
  }

  function cancelTimer(): void {
    if (timer != null) (options.clearTimeout ?? window.clearTimeout)(timer);
    timer = null;
  }

  function accepted(runGeneration: number): boolean {
    return active && generation === runGeneration
      && (!options.accept || options.accept(context) !== false);
  }

  function abandoned(runGeneration: number): boolean {
    if (accepted(runGeneration)) return false;
    // Never let an old async callback finish or mutate a replacement poll.
    if (generation === runGeneration) finish();
    return true;
  }

  function finish(): void {
    active = false;
    cancelTimer();
  }

  function stop(): void {
    generation += 1;
    finish();
    context = null;
    failures = 0;
  }

  function schedule(runGeneration: number, delay: number): void {
    cancelTimer();
    timer = later(() => {
      timer = null;
      void tick(runGeneration);
    }, Math.max(0, Number(delay) || 0));
  }

  async function tick(runGeneration: number): Promise<void> {
    if (abandoned(runGeneration)) return;
    if (options.pause?.(context)) {
      schedule(runGeneration, options.pauseDelay ?? 1500);
      return;
    }

    const requestContext = context;
    const requestedCursor = cursor;
    let response: (TPage & NormalizedTaskReplayPage) | null = null;
    let requestError: unknown = null;
    try {
      const requested = options.request
        ? await options.request(requestContext, requestedCursor) : null;
      const normalized = normalizeTaskReplayPage(requested, requestedCursor);
      if (normalized && typeof normalized === 'object') {
        response = normalized as TPage & NormalizedTaskReplayPage;
      }
    } catch (error: unknown) {
      requestError = error;
    }
    if (abandoned(runGeneration)) return;
    if (!requestError && response?.cause) requestError = response.cause;

    if (!response || !response.ok) {
      failures += 1;
      const maximum = options.maxFailures ?? 12;
      const base = options.retryBase ?? 800;
      const ceiling = options.retryMax ?? 6000;
      const delay = Math.min(
        Math.max(0, base) * failures,
        Math.max(0, ceiling),
      );
      const failure: CursorPollFailure<TContext, TPage> = {
        attempt: failures,
        cursor: requestedCursor,
        context: requestContext,
        response,
        error: requestError,
        delay,
        willRetry: false,
      };
      const retryable = !options.retryable
        || options.retryable(failure) !== false;
      if (abandoned(runGeneration)) return;
      failure.willRetry = retryable && failures <= Math.max(0, maximum);
      options.onFailure?.(failure);
      if (abandoned(runGeneration)) return;
      if (!failure.willRetry) {
        finish();
        options.onGiveUp?.(failure);
        return;
      }
      schedule(runGeneration, delay);
      return;
    }

    const recoveredAfter = failures;
    failures = 0;
    if (recoveredAfter) {
      options.onRecovered?.({
        attempts: recoveredAfter,
        cursor: requestedCursor,
        context: requestContext,
      });
      if (abandoned(runGeneration)) return;
    }

    let keepGoing: unknown = true;
    try {
      if (options.onResponse) {
        keepGoing = await options.onResponse(
          response, requestContext, requestedCursor);
      }
    } catch (consumerError: unknown) {
      if (generation !== runGeneration) return;
      finish();
      options.onConsumerError?.(consumerError, requestContext);
      return;
    }
    if (abandoned(runGeneration)) return;
    if (response.next_cursor != null) {
      cursor = Number(response.next_cursor);
    }
    if (response.replayComplete || keepGoing === false) {
      finish();
      options.onDone?.(response, requestContext);
      return;
    }
    schedule(runGeneration,
      response.caught_up === false ? 0 : (options.interval ?? 800));
  }

  function start(startCursor?: number | null, nextContext?: TContext): number {
    stop();
    active = true;
    context = nextContext || {} as TContext;
    cursor = startCursor == null ? 0 : startCursor;
    const runGeneration = generation;
    void tick(runGeneration);
    return runGeneration;
  }

  return {
    start,
    stop,
    isActive: () => active,
    cursor: () => cursor,
    failures: () => failures,
  };
}

const bridge = orchestrationRegistry as unknown as CursorPollerWindow;
bridge.normalizeTaskReplayPage = normalizeTaskReplayPage;
bridge.createOrchestrationCursorPoller = createOrchestrationCursorPoller;

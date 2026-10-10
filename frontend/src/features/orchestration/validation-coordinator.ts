import { orchestrationRegistry } from './registry';
export interface ValidationOutcome<T = unknown> {
  response: T | null;
  error: unknown;
  aborted: boolean;
  revision: unknown;
  generation: number;
}

interface ValidationEntry<T = unknown> {
  key: string;
  revision: unknown;
  generation: number;
  controller: AbortController | null;
  aborted: boolean;
  claimed: boolean;
  promise: Promise<ValidationOutcome<T>>;
}

export interface ValidationTicket<T = unknown> {
  readonly revision: unknown;
  readonly generation: number;
  readonly shared: boolean;
  readonly promise: Promise<ValidationOutcome<T>>;
  readonly _entry: ValidationEntry<T>;
}

export interface OrchestrationValidationCoordinator {
  request<T>(
    revision: unknown,
    operation: (signal: AbortSignal | null) => T | PromiseLike<T>,
  ): ValidationTicket<T>;
  claim(ticket: ValidationTicket<unknown> | null | undefined): boolean;
  pending(...revision: readonly unknown[]): boolean;
  invalidate(reason?: unknown): number;
  generation(): number;
}

type ValidationCoordinatorWindow = Window & {
  createOrchestrationValidationCoordinator?:
    typeof createOrchestrationValidationCoordinator;
};

/** Shares validation by revision and invalidates every prior generation. */
export function createOrchestrationValidationCoordinator():
OrchestrationValidationCoordinator {
  let generation = 0;
  const entries: Record<string, ValidationEntry> = Object.create(null);
  const key = (revision: unknown): string =>
    `${String(generation)}:${String(revision)}`;

  const ticket = <T>(
    entry: ValidationEntry<T>,
    shared: boolean,
  ): ValidationTicket<T> => Object.freeze({
    revision: entry.revision,
    generation: entry.generation,
    shared: Boolean(shared),
    promise: entry.promise,
    _entry: entry,
  });

  const request = <T>(
    revision: unknown,
    operation: (signal: AbortSignal | null) => T | PromiseLike<T>,
  ): ValidationTicket<T> => {
    const requestKey = key(revision);
    const existing = entries[requestKey] as ValidationEntry<T> | undefined;
    if (existing) return ticket(existing, true);
    if (typeof operation !== 'function') {
      throw new TypeError('validation operation must be a function');
    }
    const controller = typeof AbortController === 'function'
      ? new AbortController() : null;
    const entry = {
      key: requestKey,
      revision,
      generation,
      controller,
      aborted: false,
      claimed: false,
      promise: null as unknown as Promise<ValidationOutcome<T>>,
    } satisfies ValidationEntry<T>;
    entries[requestKey] = entry as ValidationEntry;
    let result: T | PromiseLike<T>;
    try {
      result = operation(controller?.signal ?? null);
    } catch (error: unknown) {
      result = Promise.reject(error);
    }
    entry.promise = Promise.resolve(result).then(
      (response): ValidationOutcome<T> => ({
        response,
        error: null,
        aborted: entry.aborted,
        revision: entry.revision,
        generation: entry.generation,
      }),
      (error: unknown): ValidationOutcome<T> => ({
        response: null,
        error,
        aborted: entry.aborted,
        revision: entry.revision,
        generation: entry.generation,
      }),
    ).finally(() => {
      if (entries[requestKey] === entry) delete entries[requestKey];
    });
    return ticket(entry, false);
  };

  const claim = (
    value: ValidationTicket<unknown> | null | undefined,
  ): boolean => {
    const entry = value?._entry;
    if (!entry || entry.claimed) return false;
    entry.claimed = true;
    return true;
  };

  const pending = (...revision: readonly unknown[]): boolean => {
    if (revision.length > 0) return Boolean(entries[key(revision[0])]);
    return Object.keys(entries).some(
      (entryKey) => entries[entryKey]?.generation === generation);
  };

  const invalidate = (reason?: unknown): number => {
    generation += 1;
    Object.keys(entries).forEach((entryKey) => {
      const entry = entries[entryKey];
      if (!entry || entry.aborted) return;
      entry.aborted = true;
      if (!entry.controller) return;
      try {
        entry.controller.abort(reason || 'validation-invalidated');
      } catch {
        entry.controller.abort();
      }
    });
    return generation;
  };

  return Object.freeze({
    request,
    claim,
    pending,
    invalidate,
    generation: () => generation,
  });
}

(orchestrationRegistry as unknown as ValidationCoordinatorWindow).createOrchestrationValidationCoordinator =
  createOrchestrationValidationCoordinator;

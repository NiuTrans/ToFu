import { orchestrationRegistry } from './registry';
export type AsyncOperation<T> = () => T | PromiseLike<T>;

export interface OrchestrationSingleFlight {
  pending(...key: readonly unknown[]): boolean;
  share<T>(key: unknown, operation: AsyncOperation<T>): Promise<T>;
  tryRun<T, TDuplicate>(
    key: unknown,
    operation: AsyncOperation<T>,
    duplicateValue: TDuplicate,
  ): Promise<T | TDuplicate>;
}

type SingleFlightWindow = Window & {
  createOrchestrationSingleFlight?: typeof createOrchestrationSingleFlight;
};

function flightKey(value: unknown): string {
  return String(value == null ? 'default' : value);
}

/** Keyed share-or-reject ownership for orchestration async commands. */
export function createOrchestrationSingleFlight(): OrchestrationSingleFlight {
  const flights = new Map<string, Promise<unknown>>();

  function pending(...key: readonly unknown[]): boolean {
    if (key.length > 0) return flights.has(flightKey(key[0]));
    return flights.size > 0;
  }

  function share<T>(key: unknown, operation: AsyncOperation<T>): Promise<T> {
    const keyName = flightKey(key);
    const active = flights.get(keyName);
    if (active) return active as Promise<T>;
    if (typeof operation !== 'function') {
      return Promise.reject(new TypeError(
        'single-flight operation must be a function'));
    }

    let result: T | PromiseLike<T>;
    try {
      result = operation();
    } catch (error: unknown) {
      result = Promise.reject(error);
    }
    const tracked = Promise.resolve(result).finally(() => {
      if (flights.get(keyName) === tracked) flights.delete(keyName);
    });
    flights.set(keyName, tracked);
    return tracked;
  }

  function tryRun<T, TDuplicate>(
    key: unknown,
    operation: AsyncOperation<T>,
    duplicateValue: TDuplicate,
  ): Promise<T | TDuplicate> {
    if (pending(key)) return Promise.resolve(duplicateValue);
    return share(key, operation);
  }

  return Object.freeze({ pending, share, tryRun });
}

(orchestrationRegistry as unknown as SingleFlightWindow).createOrchestrationSingleFlight =
  createOrchestrationSingleFlight;

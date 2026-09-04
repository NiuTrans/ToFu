/**
 * Retryable authenticated-owner resolution for browser composition.
 *
 * Responsibility: validate the current-user payload, coalesce concurrent
 * probes, retain only a successful owner ID, and expose an explicit reset
 * lifecycle. Entry point: `createCurrentUserIdentityController`. Dependencies:
 * injected loader/change/log ports; this owner never reads browser globals.
 */

type UnknownRecord = Readonly<Record<string, unknown>>;

export interface CurrentUserIdentityPorts {
  readonly loadCurrentUser: () => Promise<unknown>;
  readonly onOwnerChanged: (ownerId: number | null) => void;
  readonly log?: (message: string, level: 'info' | 'warn') => void;
}

export interface CurrentUserIdentityController {
  readonly currentOwnerId: () => number | null;
  readonly resolve: () => Promise<number | null>;
  readonly reset: () => void;
}

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : null;
}

function authenticatedOwnerId(payload: unknown): number {
  const value = record(payload);
  const ownerId = Number(value?.ownerId);
  if (value?.authenticated !== true
      || !Number.isInteger(ownerId) || ownerId < 1) {
    throw new Error('users.me did not return an authenticated ownerId');
  }
  return ownerId;
}

export function createCurrentUserIdentityController(
  ports: CurrentUserIdentityPorts,
): CurrentUserIdentityController {
  let ownerId: number | null = null;
  let resolved = false;
  let inFlight: Promise<number | null> | null = null;
  let generation = 0;

  const resolve = (): Promise<number | null> => {
    if (resolved) return Promise.resolve(ownerId);
    if (inFlight) return inFlight;
    const probeGeneration = generation;
    const probe = (async (): Promise<number | null> => {
      try {
        const nextOwnerId = authenticatedOwnerId(await ports.loadCurrentUser());
        if (probeGeneration !== generation) return ownerId;
        ownerId = nextOwnerId;
        resolved = true;
        ports.onOwnerChanged(ownerId);
        ports.log?.(`[current-user] owner resolved: ${ownerId}`, 'info');
      } catch (error) {
        if (probeGeneration !== generation) return ownerId;
        ownerId = null;
        resolved = false;
        ports.onOwnerChanged(null);
        const reason = error instanceof Error ? error.message : String(error);
        ports.log?.(`[current-user] owner unresolved; push remains blocked: ${reason}`,
          'warn');
      }
      return ownerId;
    })();
    inFlight = probe;
    void probe.finally(() => {
      if (inFlight === probe) inFlight = null;
    });
    return probe;
  };

  const reset = (): void => {
    generation += 1;
    resolved = false;
    ownerId = null;
    inFlight = null;
    ports.onOwnerChanged(null);
  };

  return Object.freeze({
    currentOwnerId: () => ownerId,
    resolve,
    reset,
  });
}

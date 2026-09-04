/**
 * Responsibility: share only overlapping browser health probes and expose a
 * repeatable, lazily decoded response snapshot to independent availability
 * monitors. Entry point: createAvailabilityHealthProbeCoordinator.
 * Dependencies: one injected wire request; owns no cache, timer, or verdict.
 */

import type {
  AvailabilityHealthProbeResponse,
} from './availability-monitor-ports';

export interface AvailabilityHealthProbeCoordinatorPorts {
  request(
    timeoutMs: number,
  ): Promise<AvailabilityHealthProbeResponse | null>;
}

export interface AvailabilityHealthProbeCoordinator {
  /** The call that opens a flight owns its wire timeout; overlapping callers
   * join that already-bounded request and do not extend its lifecycle. */
  probe(
    timeoutMs: number,
  ): Promise<AvailabilityHealthProbeResponse | null>;
}

function repeatableResponse(
  response: AvailabilityHealthProbeResponse | null,
): AvailabilityHealthProbeResponse | null {
  if (!response) return null;
  let bodyFlight: Promise<unknown> | null = null;
  const json = typeof response.json === 'function'
    ? (): Promise<unknown> => {
      bodyFlight ??= Promise.resolve().then(() => response.json?.());
      return bodyFlight;
    }
    : undefined;
  return Object.freeze({
    ok: response.ok,
    status: response.status,
    ...(json ? { json } : {}),
  });
}

export function createAvailabilityHealthProbeCoordinator(
  ports: AvailabilityHealthProbeCoordinatorPorts,
): AvailabilityHealthProbeCoordinator {
  let inFlight: Promise<AvailabilityHealthProbeResponse | null> | null = null;

  const probe = (
    timeoutMs: number,
  ): Promise<AvailabilityHealthProbeResponse | null> => {
    if (inFlight) return inFlight;
    const request = Promise.resolve()
      .then(() => ports.request(timeoutMs))
      .then(repeatableResponse);
    inFlight = request;
    const release = (): void => {
      if (inFlight === request) inFlight = null;
    };
    void request.then(release, release);
    return request;
  };

  return Object.freeze({ probe });
}

/** health endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions } from './contracts';
export function createHealthEndpoints(ports: EndpointPorts) {
  const { request, get } = ports;
  const health = {
    // Returns Response so callers can inspect resp.ok cheaply.
    check: (opts?: EndpointOptions) =>
      request<Response | null>('/api/health',
              Object.assign({ method: 'GET', parse: 'response', onError: 'null' }, opts || {})),
    // Parsed JSON variant — used by the Settings panel to show the version.
    info: (opts?: EndpointOptions) => get(
      '/api/health', Object.assign({ onError: 'null' }, opts || {})),
    retryBoot: (opts?: EndpointOptions) => ports.post('/api/v4/boot/retry', {},
      Object.assign({ onError: 'throw' as const }, opts || {})),
  };
  return health;
}

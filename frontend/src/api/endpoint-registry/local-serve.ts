/** localServe endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createLocalServeEndpoints(ports: EndpointPorts) {
  const { get } = ports;
  const localServe = {
    hardware: () => get('/api/v4/local-serve/hardware', { onError: 'null' }),
    recommendations: (purpose: string) => get(
      '/api/v4/local-serve/recommendations' +
        (purpose ? '?purpose=' + encodeURIComponent(purpose) : ''),
      { onError: 'null' }),
    deployments: () => get('/api/v4/local-serve/deployments', { onError: 'null' }),
  };
  return localServe;
}

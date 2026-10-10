/** dispatch endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createDispatchEndpoints(ports: EndpointPorts) {
  const { get, post } = ports;
  const dispatch = {
    endpointMetrics: () => get('/api/v4/dispatch/endpoint-metrics', { onError: 'null' }),
    keyStats:        () => get('/api/v4/dispatch/key-stats', { onError: 'null' }),
    modelHealth:     () => get('/api/v4/dispatch/model-health', { onError: 'null' }),
    keyOverride:     (body: JsonObject) => post('/api/v4/dispatch/key-override', body, { onError: 'null' }),
  };
  return dispatch;
}

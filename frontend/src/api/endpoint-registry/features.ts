/** features endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createFeaturesEndpoints(ports: EndpointPorts) {
  const { request, patch } = ports;
  const features = {
    set: (patch: JsonObject) =>
      request<Response | null>('/api/v4/features',
              { method: 'POST', json: patch, parse: 'response' }),
  };
  return features;
}

/** clientError endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createClientErrorEndpoints(ports: EndpointPorts) {
  const { post } = ports;
  const clientError = {
    report: (payload: JsonObject) => post<null>('/api/client-error', payload, { onError: 'null', parse: 'none' }),
  };
  return clientError;
}

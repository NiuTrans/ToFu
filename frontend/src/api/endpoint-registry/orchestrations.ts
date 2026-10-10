/** orchestrations endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createOrchestrationsEndpoints(ports: EndpointPorts) {
  const {  } = ports;
  const orchestrations = {};
  return orchestrations;
}

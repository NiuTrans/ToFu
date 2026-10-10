/** tools endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createToolsEndpoints(ports: EndpointPorts) {
  const { get } = ports;
  const tools = {
    inventory:      ()          => get('/api/v4/tools'),
  };
  return tools;
}

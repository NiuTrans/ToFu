/** tools endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
import type {RequestOptions} from '../transport';
export function createToolsEndpoints(ports: EndpointPorts) {
  const { get } = ports;
  const tools = {
    inventory: (options: Pick<RequestOptions, 'signal'> = {}) => get('/api/v4/tools', options),
  };
  return tools;
}

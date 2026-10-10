/** pricing endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createPricingEndpoints(ports: EndpointPorts) {
  const { get } = ports;
  const pricing = {
    get: () => get('/api/v4/pricing', { onError: 'null' }),
  };
  return pricing;
}

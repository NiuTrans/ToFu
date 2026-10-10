/** doc endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createDocEndpoints(ports: EndpointPorts) {
  const { request } = ports;
  const doc = {
    parse: (formData: FormData) => request('/api/doc/parse', { method: 'POST', body: formData, timeout: 0 }),
  };
  return doc;
}

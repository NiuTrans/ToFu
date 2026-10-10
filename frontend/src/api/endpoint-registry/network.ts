/** network endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createNetworkEndpoints(ports: EndpointPorts) {
  const { request } = ports;
  const network = {
    proxyTest: (payload: JsonObject) =>
      request('/api/v4/network/proxy-test', { method: 'POST', json: payload }),
  };
  return network;
}

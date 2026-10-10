/** privateHosts endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createPrivateHostsEndpoints(ports: EndpointPorts) {
  const { get, post, del } = ports;
  const privateHosts = {
    list:   ()           => get('/api/v4/private-hosts', { onError: 'null' }),
    upsert: (body: JsonObject)       => post('/api/v4/private-hosts', body),
    toggle: (host: string, on: boolean)   => post(`/api/v4/private-hosts/${encodeURIComponent(host)}/toggle`, { enabled: !!on }, { onError: 'null' }),
    remove: (host: string)       => del<Response | null>(`/api/v4/private-hosts/${encodeURIComponent(host)}`, { parse: 'response', onError: 'null' }),
  };
  return privateHosts;
}

/** credentials endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createCredentialsEndpoints(ports: EndpointPorts) {
  const { get, post, del } = ports;
  const credentials = {
    list:   ()       => get('/api/v4/credentials', { onError: 'null' }),
    upsert: (body: JsonObject)   => post('/api/v4/credentials', body),
    reveal: (name: string)   => post(`/api/v4/credentials/${encodeURIComponent(name)}/reveal`, {}),
    remove: (name: string)   => del<Response | null>(`/api/v4/credentials/${encodeURIComponent(name)}`, { parse: 'response', onError: 'null' }),
  };
  return credentials;
}

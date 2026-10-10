/** profile endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createProfileEndpoints(ports: EndpointPorts) {
  const { get, post, put } = ports;
  const profile = {
    get:           ()         => get('/api/v4/profile'),
    save:          (body: JsonObject)     => put('/api/v4/profile', { body: body || '' }),
    saveItems:     (items: unknown[])    => put('/api/v4/profile', { items: items || [] }),
    resolvePending: (id: string, accept: boolean, text: string) =>
      post<Response | null>(`/api/v4/profile/pending/${encodeURIComponent(id)}`,
           { accept: !!accept, text: text }, { parse: 'response' }),
  };
  return profile;
}

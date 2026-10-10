/** memory endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createMemoryEndpoints(ports: EndpointPorts) {
  const { get, post, del } = ports;
  const memory = {
    // summary=1 keeps bodies out of the list payload (megabytes once a
    // project accumulates memories); cards fetch their body lazily on expand.
    list:           (scope: string)     => get('/api/v4/memory', { query: { scope: scope || 'all', summary: 1 } }),
    get:            (id: string)        => get(`/api/v4/memory/${encodeURIComponent(id)}`),
    create:         (entry: JsonObject)     => post<Response | null>('/api/v4/memory', entry, { parse: 'response' }),
    remove:         (id: string)        => del<Response | null>(`/api/v4/memory/${encodeURIComponent(id)}`, { parse: 'response' }),
    toggle:         (id: string)        => post<Response | null>(`/api/v4/memory/${encodeURIComponent(id)}/toggle`, undefined, { parse: 'response' }),
    clearPreview:   ()          => get('/api/v4/memory/actions/clear'),
    clearAll:       ()          => post('/api/v4/memory/actions/clear', { confirm: true }),
  };
  return memory;
}

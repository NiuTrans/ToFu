/** userContext endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createUserContextEndpoints(ports: EndpointPorts) {
  const { get, post, put, del } = ports;
  const userContext = {
    get:       ()              => get('/api/v4/context'),
    replace:   (items: unknown[])         => put('/api/v4/context', { items: items || [] }),
    create:    (item: JsonObject)          => post('/api/v4/context', item),
    update:    (id: string, updates: JsonObject)   => put(`/api/v4/context/${encodeURIComponent(id)}`, updates),
    remove:    (id: string)            => del(`/api/v4/context/${encodeURIComponent(id)}`),
    undo:      (changeId: string)      => post(`/api/v4/context/changes/${encodeURIComponent(changeId)}/undo`, {}),
  };
  return userContext;
}

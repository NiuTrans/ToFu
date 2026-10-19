/** folders endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createFoldersEndpoints(ports: EndpointPorts) {
  const { get, post, put, del, requiredItemsList } = ports;
  const folders = {
    // Empty is valid data; transport/parse failure must stay distinguishable
    // so the folder owner can preserve its last good projection.
    list:   async ()           => {
      const path = '/api/v4/folders';
      const data = await get(path, {
        onError: 'throw', coalesce: true
      });
      return requiredItemsList(data, path);
    },
    create: (name: string, color: string, projectPaths: string[] = [], readOnlyPaths: string[] = []) => post('/api/v4/folders', { name, color: color || '', projectPaths, readOnlyPaths }, { onError: 'throw' }),
    update: (id: string, updates: JsonObject)      => put(`/api/v4/folders/${encodeURIComponent(id)}`, updates, { onError: 'throw' }),
    remove: async (id: string)         => {
      const r = await del<Response | null>(`/api/v4/folders/${encodeURIComponent(id)}`, { parse: 'response', onError: 'throw' });
      return !!(r && r.ok);
    },
  };
  return folders;
}

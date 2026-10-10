/** paperFolders endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createPaperFoldersEndpoints(ports: EndpointPorts) {
  const { get, post, put, del, requiredItemsList } = ports;
  const paperFolders = {
    // Same failure-preserving list contract as folders.list above.
    list:   async ()           => {
      const path = '/api/v4/paper-folders';
      const data = await get(path, {
        onError: 'throw', coalesce: true
      });
      return requiredItemsList(data, path);
    },
    create: (name: string, color: string)      => post('/api/v4/paper-folders', { name, color: color || '' }, { onError: 'null' }),
    update: (id: string, updates: JsonObject)      => put(`/api/v4/paper-folders/${encodeURIComponent(id)}`, updates, { onError: 'null' }),
    remove: async (id: string)         => {
      const r = await del<Response | null>(`/api/v4/paper-folders/${encodeURIComponent(id)}`, { parse: 'response', onError: 'null' });
      return !!(r && r.ok);
    },
  };
  return paperFolders;
}

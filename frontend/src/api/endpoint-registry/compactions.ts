/** compactions endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions } from './contracts';
export function createCompactionsEndpoints(ports: EndpointPorts) {
  const { request, get, post } = ports;
  const compactions = {
    list: (convId: string)            => get(`/api/v4/conversations/${encodeURIComponent(convId)}/compactions`),
    get:  (convId: string, archiveId: string) => get(`/api/v4/conversations/${encodeURIComponent(convId)}/compactions/${encodeURIComponent(archiveId)}`),
    getSummary: (convId: string, archiveId: string) =>
      get(`/api/v4/conversations/${encodeURIComponent(convId)}/compactions/${encodeURIComponent(archiveId)}`,
          { query: { includeMessages: 'false' }, coalesce: true }),
    getPost: (convId: string, archiveId: string) =>
      get(`/api/v4/conversations/${encodeURIComponent(convId)}/compactions/${encodeURIComponent(archiveId)}`,
          { query: { includeMessages: 'false', view: 'post' }, coalesce: true }),
    download: (convId: string, archiveId: string) =>
      request<Response | null>(`/api/v4/conversations/${encodeURIComponent(convId)}/compactions/${encodeURIComponent(archiveId)}`,
              { method: 'GET', query: { download: 'true' }, parse: 'response' }),
    // Manual /compact: persistently compress old history into a summary.
    // Throws ApiError on 409 task_active / 422 nothing_to_compact / etc. so
    // the caller can branch on `.code`.
    compactNow: (convId: string, opts?: EndpointOptions) =>
      post(`/api/v4/conversations/${encodeURIComponent(convId)}/compact`, opts || {}),
  };
  return compactions;
}

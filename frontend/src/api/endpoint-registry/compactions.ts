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
    // Manual /compact: persistently compress an idle ordinary conversation's
    // old history into a summary; a protected (coordinator/project) or busy
    // conversation answers {scheduled: true, appliesAt, ...} instead
    // (features/conversation-bar/scheduled-compaction.ts). Throws ApiError on
    // 422 nothing_to_compact / 409 stale / etc. so the caller can branch on `.code`.
    compactNow: (convId: string, opts?: EndpointOptions) =>
      post(`/api/v4/conversations/${encodeURIComponent(convId)}/compact`, opts || {}),
    // Withdraw a scheduled working-view request: {ok, cancelled, applying}.
    cancelScheduled: (convId: string, requestId: string) =>
      request(`/api/v4/conversations/${encodeURIComponent(convId)}/compact/scheduled/${encodeURIComponent(requestId)}`,
              { method: 'DELETE' }),
  };
  return compactions;
}

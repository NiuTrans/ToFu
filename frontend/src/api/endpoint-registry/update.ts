/** update endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions, JsonObject } from './contracts';
export function createUpdateEndpoints(ports: EndpointPorts) {
  const { get, post } = ports;
  const update = {
    check:   (opts?: EndpointOptions) => get('/api/v4/update/check', Object.assign({ onError: 'null' }, opts || {})),
    apply:   ()     => post('/api/v4/update/apply', {}),
    // restart takes {force, convId}. onError:'throw' (NOT 'null') is load-bearing:
    // the backend returns 409 {needsForce, runningTasks} when sibling
    // conversations have in-flight tasks, and update.js must READ that body to
    // show the informed force-confirm dialog. Swallowing it to null would make
    // the button silently no-op (the historical bug: a fixed empty {} body
    // dropped the caller's {force:true} → always force=false → always 409).
    restart: (payload: JsonObject, opts?: EndpointOptions) => post(
      '/api/v4/update/restart',
      payload || {},
      Object.assign({ onError: 'throw' }, opts || {}),
    ),
    // shutdown: POST — graceful manual stop (writes the manual-shutdown
    // marker so the next boot won't mistake it for an OS kill). No re-exec.
    // Takes {approvalId} once a human approved the pending request.
    shutdown: (payload: JsonObject) => post('/api/v4/update/shutdown', payload || {}, { onError: 'throw' }),
    // Lifecycle approvals ( human-approval gate): a
    // restart/shutdown POST without an approvalId answers 202 +
    // {pendingApproval}; the human decides here; the caller retries with
    // {approvalId}. onError:'throw' so callers can read 403/404 bodies.
    listLifecycleApprovals: (query: JsonObject) =>
      get('/api/v4/update/lifecycle-approvals', { query: query || {}, onError: 'null' }),
    getLifecycleApproval: (id: string, opts?: EndpointOptions) =>
      get(`/api/v4/update/lifecycle-approvals/${encodeURIComponent(id)}`, Object.assign({ onError: 'null' }, opts || {})),
    decideLifecycleApproval: (id: string, approved: boolean) =>
      post(`/api/v4/update/lifecycle-approvals/${encodeURIComponent(id)}/decide`,
           { approved: !!approved }, { onError: 'throw' }),
  };
  return update;
}

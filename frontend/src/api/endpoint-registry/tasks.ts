/** tasks endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createTasksEndpoints(ports: EndpointPorts) {
  const { get, post } = ports;
  const tasks = {
    convWire: (convId: string) =>
      get(`/api/v4/tasks/conv-wire/${encodeURIComponent(convId)}`,
          { onError: 'null' }),
    getRequestPayload: (taskId: string, roundNum: number, turn: number, kind: string, fold: boolean) =>
      get(`/api/v4/tasks/${encodeURIComponent(taskId)}/requests/${encodeURIComponent(roundNum)}`,
          { query: { ...(turn ? { turn } : {}), ...(kind ? { kind } : {}),
                     ...(fold ? { fold: 1 } : {}) } }),
    getRawArchiveChunk: (taskId: string, archiveId: string, part: string, offset: number) =>
      get(`/api/v4/tasks/${encodeURIComponent(taskId)}/raw-archives/${encodeURIComponent(archiveId)}/${encodeURIComponent(part)}`,
          { query: { offset: offset || 0, limit: 256 * 1024 },
            onError: 'null' }),
    // Turn Trace (docs/contracts/TURN_TRACE_CONTRACT.md): the server-folded timing
    // span tree for one task — the drawer NEVER derives timing itself.
    getTrace: (taskId: string) =>
      get(`/api/v4/tasks/${encodeURIComponent(taskId)}/trace`,
          { onError: 'null' }),
  
    // ── Generic production-job lifecycle ──
    // start/get/events/abort are CAPABILITY-AGNOSTIC: the backend dispatches
    // on `kind` (research | longform-report | …), so the next capability needs
    // ZERO new client methods. Deliberately NOT routed through an LLM tool —
    // the produce_* tools are search-gated and only fire if the model elects
    // to call them, which cannot back a UI control.
    start: (kind: string, payload: JsonObject) =>
      post('/api/v4/tasks/start', Object.assign({ kind: kind }, payload || {})),
    // Full snapshot. Carries `artifact_quality`: a degraded job keeps
    // status='done' by design, so that field is the ONLY thing separating
    // "good artifact" from "valid artifact out of a broken pipeline".
    get: (taskId: string) =>
      get(`/api/v4/tasks/${encodeURIComponent(taskId)}`, { onError: 'null' }),
    events: (taskId: string, cursor: number) =>
      get(`/api/v4/tasks/${encodeURIComponent(taskId)}/events`,
          { query: { cursor: cursor || 0 }, onError: 'null' }),
    abort: (taskId: string) =>
      post(`/api/v4/tasks/${encodeURIComponent(taskId)}/abort`, undefined,
           { onError: 'null' }),
    list: (kind: string, status: string, filters: { direction?: string; lang?: string; limit?: number } = {}) =>
      get('/api/v4/tasks', { query: Object.assign({}, kind ? { kind: kind } : {},
                                                  status ? { status: status } : {}, filters),
                             onError: 'null' }),
  };
  return tasks;
}

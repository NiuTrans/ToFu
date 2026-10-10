/** scheduler endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createSchedulerEndpoints(ports: EndpointPorts) {
  const { get, post } = ports;
  const scheduler = {
    proactiveStatus: ()              => get('/api/v4/scheduler/proactive/status'),
    triggerTask:     (taskId: string)        => post(`/api/v4/scheduler/tasks/${encodeURIComponent(taskId)}/trigger`, undefined),
    pauseTask:       (taskId: string)        => post(`/api/v4/scheduler/tasks/${encodeURIComponent(taskId)}/pause`, undefined, { onError: 'null' }),
    resumeTask:      (taskId: string)        => post(`/api/v4/scheduler/tasks/${encodeURIComponent(taskId)}/resume`, undefined, { onError: 'null' }),
    pollLog:         (taskId: string, limit: number) => get(`/api/v4/scheduler/tasks/${encodeURIComponent(taskId)}/poll-log`, { query: { limit: limit || 20 } }),
  };
  return scheduler;
}

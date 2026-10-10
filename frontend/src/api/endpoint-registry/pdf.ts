/** pdf endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createPdfEndpoints(ports: EndpointPorts) {
  const { request, get } = ports;
  const pdf = {
    parse:     (formData: FormData)            => request('/api/pdf/parse', { method: 'POST', body: formData, timeout: 0 }),
    vlmStart:  (formData: FormData)            => request('/api/pdf/vlm-parse', { method: 'POST', body: formData, timeout: 0 }),
    vlmPoll:   (taskId: string)              => get(`/api/v4/pdf/vlm-parse/${encodeURIComponent(taskId)}`, { onError: 'null' }),
    vlmCancel: (taskId: string)              => request(`/api/v4/pdf/vlm-parse/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),
    vlmTasks:  (filename: string)            => get('/api/v4/pdf/vlm-tasks', { query: { filename }, onError: 'null' }),
  };
  return pdf;
}

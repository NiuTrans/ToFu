/** timer endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createTimerEndpoints(ports: EndpointPorts) {
  const { get, post } = ports;
  const timer = {
    list:     (summaryOnly: boolean)   => get('/api/v4/timer/list', {
      query: summaryOnly ? { summary: 1 } : {},
    }),
    trigger:  (id: string)            => post(`/api/v4/timer/${encodeURIComponent(id)}/trigger`, undefined),
    cancel:   (id: string)            => post(`/api/v4/timer/${encodeURIComponent(id)}/cancel`, undefined, { onError: 'null' }),
    status:   (id: string, limit: number)     => get(`/api/v4/timer/${encodeURIComponent(id)}/status`, { query: { limit: limit || 20 } }),
  };
  return timer;
}

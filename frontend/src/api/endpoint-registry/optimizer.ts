/** optimizer endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions } from './contracts';
export function createOptimizerEndpoints(ports: EndpointPorts) {
  const { get, post } = ports;
  const optimizer = {
    proposals: (limit: number)        => get('/api/v4/optimizer/proposals', { query: { limit: limit || 60 } }),
    approve:   (id: string)           => post(`/api/v4/optimizer/proposals/${encodeURIComponent(id)}/approve`, undefined),
    reject:    (id: string, reason: string)   => post(`/api/v4/optimizer/proposals/${encodeURIComponent(id)}/reject`, { reason: reason || '' }),
    revert:    (id: string)           => post(`/api/v4/optimizer/proposals/${encodeURIComponent(id)}/revert`, undefined),
    runNow:    (opts?: EndpointOptions)         => post('/api/v4/optimizer/run-now', Object.assign({ dry_run: false, window_hours: 24 }, opts || {})),
  };
  return optimizer;
}

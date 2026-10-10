/** swarm endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions } from './contracts';
export function createSwarmEndpoints(ports: EndpointPorts) {
  const { get, post } = ports;
  const swarm = {
    status: (taskId: string, opts?: EndpointOptions) =>
      get(`/api/v4/swarm/status/${encodeURIComponent(taskId)}`,
          Object.assign({ onError: 'null' }, opts || {})),
    abort:  (taskId: string) =>
      post(`/api/v4/swarm/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null' }),
  };
  return swarm;
}

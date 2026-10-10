/** activity endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createActivityEndpoints(ports: EndpointPorts) {
  const { get } = ports;
  const activity = {
    background: () => get('/api/v4/activity/background',
      { onError: 'null', coalesce: true }),
  };
  return activity;
}

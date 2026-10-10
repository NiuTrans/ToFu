/** users endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createUsersEndpoints(ports: EndpointPorts) {
  const { get } = ports;
  const users = {
    me: () => get('/api/v4/users/me', {
      onError: 'null', coalesce: true
    }),
  };
  return users;
}

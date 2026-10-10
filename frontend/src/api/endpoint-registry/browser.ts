/** browser endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createBrowserEndpoints(ports: EndpointPorts) {
  const { request, get, post } = ports;
  const browser = {
    status: () => get('/api/v4/browser/status'),
    test: () => get('/api/v4/browser/test', { onError: 'null' }),
    adapters: () => get('/api/v4/browser/adapters', { onError: 'null' }),
    access: () => get('/api/v4/browser/access', { onError: 'null' }),
    updateAccess: (body: JsonObject) => request('/api/v4/browser/access', {
      method: 'PUT', json: body || {}, onError: 'null'
    }),
    // Guided install step (loopback-gated server-side): opens the SERVER
    // machine's Chrome at chrome://extensions. null on refusal/failure.
    openExtensions: () => post('/api/v4/browser/open-extensions', {},
                               { onError: 'null' }),
  };
  return browser;
}

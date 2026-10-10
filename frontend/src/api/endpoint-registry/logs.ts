/** logs endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createLogsEndpoints(ports: EndpointPorts) {
  const { request, post } = ports;
  const logs = {
    clean:    (text: string) => post('/api/v4/logs/clean', { text }, { onError: 'null' }),
    // Browser-console relay batch (core/client_log_relay.js). keepalive lets
    // the pagehide flush outlive the page; onError:'null' matches the relay's
    // never-amplify doctrine (a down server drops the batch silently).
    clientRelay: (payload: string) => request<null>('/api/v4/logs/client',
      { method: 'POST', body: payload, keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        parse: 'none', onError: 'null' }),
    compress: async (text: string) => {
      const resp = await request<Response>('/api/v4/logs/compress',
                                 { method: 'POST', json: { text }, parse: 'response' });
      const data: JsonObject = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(String(data.error || 'API error'));
      return data;
    },
  };
  return logs;
}

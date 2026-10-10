/** trading endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions, JsonObject } from './contracts';
export function createTradingEndpoints(ports: EndpointPorts) {
  const { request, _resolve } = ports;
  const _TRADING_BASE = '/api/v4/trading';
  const trading = {
    base: () => _resolve(_TRADING_BASE),
    call: async (path: string, opts?: EndpointOptions) => {
      opts = opts || {};
      const resp = await request<Response>(_TRADING_BASE + path, Object.assign({
        method: opts.method || 'GET',
        headers: Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {}),
        body: opts.body,
        timeout: opts.timeout,
        signal: opts.signal,
        parse: 'response' as const,
      }, {}));
      if (!resp.ok) {
        let err: JsonObject = {};
        try { err = await resp.json(); } catch (e) {
          console.warn('[Api][trading] non-JSON error body for %s (HTTP %s): %s',
                       _TRADING_BASE + path, resp.status, e instanceof Error ? e.message : String(e));
        }
        throw new Error(String((err && err.error) || ('HTTP ' + resp.status)));
      }
      return await resp.json();
    },
    // Raw Response for streaming / progress-poll endpoints. Caller pipes
    // resp.body.getReader() (SSE) or reads resp.json() and branches on
    // resp.status itself. timeout defaults to 0 (no client-side abort).
    raw: (path: string, opts?: EndpointOptions) => {
      opts = opts || {};
      return request<Response | null>(_TRADING_BASE + path, {
        method: opts.method || 'GET',
        headers: Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {}),
        body: opts.body,
        timeout: opts.timeout === undefined ? 0 : opts.timeout,
        signal: opts.signal,
        parse: 'response',
      });
    },
  };
  return trading;
}

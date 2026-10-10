/** translate endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions, JsonObject } from './contracts';
export function createTranslateEndpoints(ports: EndpointPorts) {
  const { request, post } = ports;
  const translate = {
    run: async (body: JsonObject, opts?: EndpointOptions) => {
      const resp = await request<Response>('/api/v4/translate', Object.assign({
        method: 'POST', json: body, parse: 'response',
      }, opts || {}));
      const data: JsonObject = await resp.json().catch(() => ({}));
      data._status = resp.status;
      data._statusText = resp.statusText;
      data._ok = resp.ok;
      return data;
    },
    start:     (body: JsonObject)         => post('/api/v4/translate/start', body),
    poll:      async (taskId: string) => {
      const resp = await request<Response>(`/api/v4/translate/poll/${encodeURIComponent(taskId)}`, {
        method: 'GET', parse: 'response', onError: 'null',
      });
      if (!resp) return { status: 'error', error: 'network' };
      if (resp.status === 404) return { status: 'not_found' };
      if (!resp.ok) return { status: 'error', error: `HTTP ${resp.status}` };
      try { return await resp.json(); } catch (e) { return { status: 'error', error: e instanceof Error ? e.message : String(e) }; }
    },
    // A failed batch probe remains null — translation.js's
    // !Array.isArray(data) branch synthesizes per-id error rows and must
    // keep firing on a failed (null) probe.
    pollBatch: async (taskIds: unknown[])  => {
      const d = await post('/api/v4/translate/poll-batch', { taskIds }, { onError: 'null' });
      if (d == null) return null;
      if (Array.isArray(d.items)) return d.items;
      return null;
    },
    // Settings → MT-config probe. Backend returns {ok, translated, error?}.
    mtTest:    (mtConfig: JsonObject, text: string) => post('/api/v4/translate/mt-test', { mt_config: mtConfig, text }, { onError: 'null' }),
  };
  return translate;
}

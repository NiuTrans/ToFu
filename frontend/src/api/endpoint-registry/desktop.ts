/** desktop endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createDesktopEndpoints(ports: EndpointPorts) {
  const { request, get, post, del } = ports;
  const desktop = {
    /* `arch` is the architecture the CLIENT resolved for itself via
     * navigator.userAgentData.getHighEntropyValues(['architecture']). It is
     * threaded to the server because macOS cannot be narrowed any other way:
     * an Apple Silicon Mac reports "Intel Mac OS X" in its UA, so without this
     * the server must offer BOTH DMGs rather than guess wrong. Omit it and the
     * ambiguous (correct, two-choice) answer comes back. */
    status:      (arch: string) => get('/api/v4/desktop/status',
                              { onError: 'null', query: arch ? { arch: arch } : {} }),
    devices:     () => get('/api/v4/desktop/devices', {
      onError: 'null', coalesce: true
    }),
    mintToken:   (name: string) => post('/api/v4/desktop/token', { name: name || '' }),
    revokeToken: (keyId: string) => del(`/api/v4/desktop/token/${encodeURIComponent(keyId)}`),
    /* The diagnostics inbox (2026-08-06): the agent's「复制诊断信息」bundle,
     * pasted in the Local Control panel, stored server-side for debugging. */
    submitDiag:  (text: string) => post('/api/v4/desktop/client-diag', { text: text }),
    listDiags:   () => get('/api/v4/desktop/client-diag', { onError: 'null' }),
    /* Browser-assisted SSO relay: forward the agent's validated poll through
     * this authenticated page, while keeping the endpoint and request shape
     * owned by the unified API client. A raw Response is required because the
     * browser must return both the exact status and body to the local broker. */
    relayPoll:   (payload: JsonObject, bridgeSecret: string) => request<Response | null>('/api/desktop/poll', {
      method: 'POST',
      json: payload || {},
      headers: Object.assign(
        { 'Accept': 'application/json' },
        bridgeSecret ? { 'X-Bridge-Secret': bridgeSecret } : {} as Record<string, string>),
      credentials: 'include',
      parse: 'response',
    }),
    /* Browser-pushed zero-config attach (the macOS/Linux counterpart of the
     * personalized Windows installer): mint a fresh attach bundle — route
     * candidates + a per-user agents:bridge token — which this page then
     * hands to the unattached agent's loopback broker. `base` is the page's
     * live origin+BASE_PATH, pinned server-side to the request's Host (the
     * same rule as the .exe download's ?base=). */
    mintAttachBundle: (base: string) => post('/api/v4/desktop/agent-attach-bundle', {},
      { query: base ? { base: base } : {} }),
  };
  return desktop;
}

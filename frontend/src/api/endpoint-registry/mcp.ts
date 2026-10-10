/** mcp endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createMcpEndpoints(ports: EndpointPorts) {
  const { request, post } = ports;
  const mcp = {
    catalogList:      ()                           =>
      request<Response | null>('/api/v4/mcp/catalog',
              { method: 'GET', parse: 'response', onError: 'null' }),
    // Per-server tool list (with per-tool `enabled` flags) — backs the
    // demand-opened Settings → MCP toggle list. First-screen context counts
    // piggyback on server-config and never fetch these full schema rows.
    toolsListForServer: (server: string)                   =>
      request<Response | null>('/api/v4/mcp/tools',
              { method: 'GET', query: { server }, parse: 'response', onError: 'null' }),
    // Replace a server's disabled_tools list (full-replacement semantics).
    serverToolsSet: (server: string, disabledTools: unknown[])        =>
      request<Response | null>(`/api/v4/mcp/servers/${encodeURIComponent(server)}/tools`,
              { method: 'PUT', json: { disabled_tools: disabledTools || [] },
                parse: 'response', onError: 'null' }),
    // NOTE: no onError:'null' here — a failed connect returns HTTP 500
    // with a rich {error, stderr_tail} body; we want that to throw an
    // ApiError (carrying .body) so the UI can show the real reason
    // instead of a generic "无法连接" after the call silently nulls out.
    catalogInstall:   (id: string, env: string)                    =>
      // Returns fast: either {status:'ready'} (connected) or HTTP 202
      // {status:'installing'} when a cold `pip install` was kicked off in the
      // background. The UI then polls installStatus until ready/error, so the
      // request no longer blocks for minutes (a proxy could cut it).
      post('/api/v4/mcp/catalog/install', { id, env: env || {} }),
    // Poll an in-flight async install. 202 while installing, 200 ready (with
    // tool list), 500 error — all carry a {status} field. parse:'response'
    // so the caller inspects HTTP status + body.
    catalogInstallStatus: (id: string)                     =>
      request<Response | null>('/api/v4/mcp/catalog/install/status',
              { method: 'GET', query: { id }, parse: 'response', onError: 'null' }),
    catalogUninstall: (id: string, purge: boolean)                  =>
      post('/api/v4/mcp/catalog/uninstall',
           { id, ...(purge ? { purge: true } : {}) }, { onError: 'null' }),
    connectAll:       ()                           =>
      post('/api/v4/mcp/connect', {}, { onError: 'null' }),
    connectOne:       (server: string)                     =>
      post('/api/v4/mcp/connect', { server }, { onError: 'null' }),
    serverCreate:     (payload: JsonObject)                    =>
      post('/api/v4/mcp/servers', payload, { onError: 'null' }),
    // Upstream version check for installed servers (npm/PyPI latest vs
    // the stored launch spec). Backs the per-card update button.
    updatesCheck:     ()                           =>
      request<Response | null>('/api/v4/mcp/updates',
              { method: 'GET', parse: 'response', onError: 'null' }),
    // Like catalogInstall, no onError:'null' — a failed update carries a
    // rich {error, stderr_tail} body the card should surface verbatim.
    updateApply:      (id: string)                         =>
      post('/api/v4/mcp/updates/apply', { id }),
  };
  return mcp;
}

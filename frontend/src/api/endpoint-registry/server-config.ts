/** serverConfig endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions, JsonObject } from './contracts';
export function createServerConfigEndpoints(ports: EndpointPorts) {
  const { request, get } = ports;
  const serverConfig = {
    get:    (options?: EndpointOptions) => get('/api/v4/server-config', options),
    update: (payload: JsonObject) =>
      request<Response | null>('/api/v4/server-config',
              { method: 'POST', json: payload, parse: 'response' }),
    // Built-in (default) system prompt — used by the Settings system-prompt
    // editor to pre-fill / reset. project & tools flags shape the preview.
    defaultSystemPrompt: (project: boolean, tools: boolean) =>
      get('/api/v4/system-prompt/default?project=' + (project ? '1' : '0')
          + '&tools=' + (tools === false ? '0' : '1'), { onError: 'null' }),
    // Built-in system prompt split into toggleable blocks (id/title/text/
    // dynamic) — used by the per-block system-prompt editor.
    systemPromptBlocks: (project: boolean, tools: boolean) =>
      get('/api/v4/system-prompt/blocks?project=' + (project ? '1' : '0')
          + '&tools=' + (tools === false ? '0' : '1'), { onError: 'null' }),
  };
  return serverConfig;
}

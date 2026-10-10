/** modelRouting endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createModelRoutingEndpoints(ports: EndpointPorts) {
  const { request, get, post, put, patch } = ports;
  const modelRouting = {
    get: (options: JsonObject) => get('/api/v4/model-routing', options),
    replace: (modelRoutingDocument: JsonObject, expectedRevision: number) => put(
      '/api/v4/model-routing', {
        model_routing: modelRoutingDocument,
        expected_revision: expectedRevision,
      }),
    providers: () => get('/api/v4/providers'),
    createProvider: (bundle: JsonObject, expectedRevision: number) => post(
      '/api/v4/providers',
      Object.assign({}, bundle || {}, { expected_revision: expectedRevision })),
    provider: (providerId: string) => get(
      `/api/v4/providers/${encodeURIComponent(providerId)}`),
    saveProvider: (providerId: string, bundle: JsonObject, expectedRevision: number) => patch(
      `/api/v4/providers/${encodeURIComponent(providerId)}`,
      Object.assign({}, bundle || {}, { expected_revision: expectedRevision })),
    deleteProvider: (providerId: string, expectedRevision: number) => request(
      `/api/v4/providers/${encodeURIComponent(providerId)}`, {
        method: 'DELETE', json: { expected_revision: expectedRevision },
      }),
    putCredentialSecret: (credentialId: string, secret: string, expectedRevision: number) => put(
      `/api/v4/model-routing/credentials/${encodeURIComponent(credentialId)}/secret`, {
        secret,
        expected_revision: expectedRevision,
      }),
    revealCredentialSecret: (credentialId: string) => post(
      `/api/v4/model-routing/credentials/${encodeURIComponent(credentialId)}/secret/reveal`,
      {}, { onError: 'null' }),
    probeCellsStart: (providerId: string, body: JsonObject) => post(
      `/api/v4/providers/${encodeURIComponent(providerId)}/probe-cells/start`, body || {}),
    probeCellsStatus: (providerId: string) => get(
      `/api/v4/providers/${encodeURIComponent(providerId)}/probe-cells/status`),
  };
  return modelRouting;
}

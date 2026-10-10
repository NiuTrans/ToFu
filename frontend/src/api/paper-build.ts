/** Paper build HTTP boundary; generic TaskRuntime owns task lifecycle. */
import { request, resolvePath } from './transport';
import type { PaperBuildAcceptedV1, PaperBuildInverseLocationRequestV1,
  PaperBuildInverseLocationV1, PaperBuildLayoutV1, PaperBuildRequestV1,
  PaperBuildResultV1, PaperBuildViewerRequestV1,
  PaperBuildViewerResultV1 } from './paper-build.generated';

export interface PaperTaskSnapshot { status: string; result?: PaperBuildResultV1; error?: unknown }
export interface PaperBuildApi {
  start(body: PaperBuildRequestV1): Promise<PaperBuildAcceptedV1>;
  task(taskId: string, signal: AbortSignal): Promise<PaperTaskSnapshot>;
  renew(taskId: string, subscriberId: string, signal: AbortSignal): Promise<{ renewed: boolean }>;
  viewer(documentId: string, buildId: string, body: PaperBuildViewerRequestV1): Promise<PaperBuildViewerResultV1>;
  detach(taskId: string, subscriberId: string): Promise<unknown>;
  log(documentId: string, buildId: string, signal: AbortSignal): Promise<string>;
  inverse(documentId: string, buildId: string, body: PaperBuildInverseLocationRequestV1,
    signal: AbortSignal): Promise<PaperBuildInverseLocationV1>;
  layout(documentId: string, buildId: string, signal: AbortSignal): Promise<PaperBuildLayoutV1>;
  bytes(url: string, signal: AbortSignal): Promise<ArrayBuffer>;
  resolve(url: string): string;
}
const taskPath = (id: string): string => `/api/v4/paper/builds/tasks/${encodeURIComponent(id)}`;
export const paperBuildArtifactPath = (documentId: string, buildId: string, name: string): string =>
  `/api/v4/paper/builds/${encodeURIComponent(documentId)}/${encodeURIComponent(buildId)}/artifacts/${encodeURIComponent(name)}`;
export const paperBuildApi: PaperBuildApi = {
  viewer: (documentId, buildId, json) => request(
    `/api/v4/paper/builds/${encodeURIComponent(documentId)}/${encodeURIComponent(buildId)}/viewers`, {
      method: 'POST', json, timeout: 10000, keepalive: json.operation === 'release',
    }),
  start: (json) => request('/api/v4/paper/builds', { method: 'POST', json, convId: json.conv_id }),
  task: (id, signal) => request(`/api/v4/tasks/${encodeURIComponent(id)}`, { signal, taskId: id }),
  renew: (id, subscriberId, signal) => request(`${taskPath(id)}/renew`, {
    method: 'POST', json: { subscriber_id: subscriberId }, signal, taskId: id }),
  detach: (id, subscriberId) => request(`${taskPath(id)}/detach`, {
    method: 'POST', json: { subscriber_id: subscriberId }, keepalive: true, taskId: id }),
  log: (documentId, buildId, signal) => request(
    paperBuildArtifactPath(documentId, buildId, 'build.log'),
    { parse: 'text', signal }),
  inverse: (documentId, buildId, json, signal) => request(
    `/api/v4/paper/builds/${encodeURIComponent(documentId)}/${encodeURIComponent(buildId)}/location/inverse`,
    { method: 'POST', json, signal, timeout: 10000 }),
  layout: (documentId, buildId, signal) => request(
    `/api/v4/paper/builds/${encodeURIComponent(documentId)}/${encodeURIComponent(buildId)}/layout`,
    { method: 'POST', signal, timeout: 15000 }),
  bytes: async (url, signal) => (await request<Response>(url, { parse: 'response', signal })).arrayBuffer(),
  resolve: resolvePath,
};

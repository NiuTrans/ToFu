/** artifacts endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
import type { ArtifactReviewCommand, ArtifactReviewView } from '../artifact-review.generated';
export function createArtifactsEndpoints(ports: EndpointPorts) {
  const { request, get, post, del, _resolve } = ports;
  const artifacts = {
    review: (id: string, signal?: AbortSignal) => get<ArtifactReviewView>(
      `/api/v4/artifacts/${encodeURIComponent(id)}/review`, { signal }),
    saveReview: (id: string, command: ArtifactReviewCommand, signal?: AbortSignal) => post<ArtifactReviewView>(
      `/api/v4/artifacts/${encodeURIComponent(id)}/review`, command, { signal }),
    meta:        (id: string)               => get(`/api/v4/artifacts/${encodeURIComponent(id)}`,
                                            { onError: 'null' }),
    versions:    (id: string)               => get(`/api/v4/artifacts/${encodeURIComponent(id)}/versions`,
                                            { onError: 'null' }),
    pin:         (id: string, pinned: boolean)       => post(`/api/v4/artifacts/${encodeURIComponent(id)}/pin`,
                                             { pinned: !!pinned }, { onError: 'null' }),
    remove:      (id: string)               => del<Response | null>(`/api/v4/artifacts/${encodeURIComponent(id)}`,
                                            { parse: 'response', onError: 'null' }),
    library:     (limit: number)            => get('/api/v4/artifacts',
                                            { query: { limit: limit || 120 }, onError: 'null' }),
    forConv:     (convId: string, signal?: AbortSignal)           => get('/api/v4/artifacts',
                                            { query: { conv: convId }, onError: 'null', signal }),
    scan:        (convId: string)           => post('/api/v4/artifacts/scan',
                                             { conv_id: convId }, { onError: 'null' }),
    // Fetch raw bytes as text (markdown / html / svg). Returns a string
    // or throws — callers that need different shapes use the URL builders.
    contentText: async (id: string) => {
      const resp = await request<Response>(`/api/artifacts/${encodeURIComponent(id)}/raw`,
                                  { method: 'GET', parse: 'response' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return await resp.text();
    },
    // URL builders for iframe.src / anchor.href — these intentionally
    // resolve to the legacy carve-out paths (binary / sandboxed HTML
    // with custom Content-Disposition + CSP headers; not v1 envelope).
    rawUrl:       (id: string) => _resolve(`/api/artifacts/${encodeURIComponent(id)}/raw`),
    viewUrl:      (id: string) => _resolve(`/api/artifacts/${encodeURIComponent(id)}/view`),
    downloadUrl:  (id: string) => _resolve(`/api/artifacts/${encodeURIComponent(id)}/download`),
    exportPdfUrl: (id: string) => _resolve(`/api/artifacts/${encodeURIComponent(id)}/export?format=pdf`),
  };
  return artifacts;
}

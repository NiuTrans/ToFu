/** knowledge endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createKnowledgeEndpoints(ports: EndpointPorts) {
  const { request, get, post, del } = ports;
  const knowledge = {
    status:         (options: JsonObject)   => {
      const value = options || {};
      const query = new URLSearchParams();
      if (value.page) query.set('page', String(value.page));
      if (value.page_size) query.set('page_size', String(value.page_size));
      if (value.query) query.set('query', String(value.query));
      if (value.category && value.category !== 'all') {
        query.set('category', String(value.category));
      }
      if (value.sort) query.set('sort', String(value.sort));
      const suffix = query.toString();
      return get('/api/v4/knowledge' + (suffix ? `?${suffix}` : ''));
    },
    activity:       ()          => get('/api/v4/knowledge/activity'),
    setEnabled:     (enabled: boolean)   => post('/api/v4/knowledge/settings', { enabled: !!enabled }),
    setVisualEnrichment: (enabled: boolean) => post(
      '/api/v4/knowledge/settings', { visual_enrichment: !!enabled }),
    upload:         (formData: FormData)  => request('/api/v4/knowledge/documents', {
      method: 'POST', body: formData, timeout: 0,
    }),
    search:         (query: string, limit: number) => post('/api/v4/knowledge/search', {
      query: String(query || ''), limit: Number(limit || 6),
    }),
    content:        (id: string, offset: number, limit: number) => get(
      `/api/v4/knowledge/documents/${encodeURIComponent(id)}/content` +
      `?offset=${Math.max(0, Number(offset || 0))}&limit=${Math.max(1, Number(limit || 80))}`),
    reindex:        (id: string)        => post(
      `/api/v4/knowledge/documents/${encodeURIComponent(id)}/reindex`, {}),
    remove:         (id: string)        => del(`/api/v4/knowledge/documents/${encodeURIComponent(id)}`),
  };
  return knowledge;
}

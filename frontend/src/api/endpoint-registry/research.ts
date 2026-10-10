/** research endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createResearchEndpoints(ports: EndpointPorts) {
  const { get, post, put, _resolve } = ports;
  const research = {
    observations: (taskId: string, cursor = '', signal?: AbortSignal) =>
      get(`/api/v4/research/actions/${encodeURIComponent(taskId)}/observations`,
          { query: { limit: 8, cursor }, signal }),
    records: (limit: number) =>
      get('/api/v4/research/records', { query: { limit: limit || 50 } }),
    record: (taskId: string) =>
      get(`/api/v4/research/records/${encodeURIComponent(taskId)}`),
    unfinishedActions: (direction: string, lang: string) =>
      get('/api/v4/research/actions/unfinished', {query: {direction, lang}}),
    resumeAction: (taskId: string) =>
      post(`/api/v4/research/actions/${encodeURIComponent(taskId)}/resume`, {}),
    resumeRecord: (taskId: string) =>
      post(`/api/v4/research/records/${encodeURIComponent(taskId)}/resume`, {}),
    lookup: (direction: string, lang: string) =>
      get('/api/v4/research/lookup',
          { query: { direction: direction || '', lang: lang || 'en' },
            onError: 'null' }),
    // The direction INDEX. The persisted rows are keyed by a one-way hash of
    // the direction, so without this a user who forgot their exact original
    // wording could never address their own artifacts again.
    list: (limit: number) =>
      get('/api/v4/research/list',
          { query: { limit: limit || 50 }, onError: 'null' }),
  
    workspace: (direction: string, lang: string) =>
      get('/api/v4/research/workspace',
          { query: { direction: direction || '', lang: lang || 'en' } }),
    history: (direction: string, lang: string, cursor: number, signal?: AbortSignal) =>
      get('/api/v4/research/workspace', {query: {direction, lang, history_cursor: cursor}, signal}),
    saveWorkspace: (payload: JsonObject) =>
      put('/api/v4/research/workspace', payload),
    // One-shot LLM draft of the frozen protocol, grounded in the persisted
    // survey/gap/idea. A proposal only — the workspace PUT stays the authority.
    draftProtocol: (payload: JsonObject) =>
      post('/api/v4/research/workspace/draft-protocol', payload),
    capabilities: () =>
      get('/api/v4/research/capabilities'),
    scaffoldManuscript: (payload: JsonObject) =>
      post('/api/v4/research/manuscript/scaffold', payload),
    sourceArchiveUrl: (direction: string, lang: string) =>
      _resolve('/api/v4/research/manuscript/source.zip?direction=' +
        encodeURIComponent(direction || '') + '&lang=' +
        encodeURIComponent(lang || 'en')),
  };
  return research;
}

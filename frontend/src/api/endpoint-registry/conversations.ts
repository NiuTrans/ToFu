/** conversations endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions, JsonObject } from './contracts';
export function createConversationsEndpoints(ports: EndpointPorts) {
  const { request, get, post, patch, newIdempotencyKey, _idempotentResponseWrite, _resolveAcrossWorkerRecovery, readLanguage } = ports;
  const conversations = {
    get: (convId: string, opts?: EndpointOptions) => get(`/api/v4/conversations/${encodeURIComponent(convId)}`, Object.assign({ onError: 'null', coalesce: true }, opts || {})),
    getResponse: (convId: string, opts?: EndpointOptions) =>
      request<Response | null>(`/api/v4/conversations/${encodeURIComponent(convId)}`,
              Object.assign({ method: 'GET', parse: 'response', onError: 'null' }, opts || {})),
    // Lightweight hover-preview: {id, title, firstUserMessage, msgCount}.
    // Parses only the opening user turn server-side, so the response is tiny
    // even for a huge conversation. Used by the Project Brain panel to resolve
    // opaque conversation IDs to a readable preview on hover. null on failure.
    preview: (convId: string) =>
      get(`/api/v4/conversations/${encodeURIComponent(convId)}/preview`,
          { onError: 'null' }),
    messageActivity: (convId: string, msgId: string) =>
      get(`/api/v4/conversations/${encodeURIComponent(convId)}/messages/by-id/${encodeURIComponent(msgId)}/activity`,
          { convId }),
    patchSettings: (convId: string, patch: JsonObject) =>
      _idempotentResponseWrite(
        `/api/v4/conversations/${encodeURIComponent(convId)}/settings`,
        { method: 'PATCH', json: patch }),
    patchResolvedSettings: (convId: string, inputs: JsonObject) =>
      _idempotentResponseWrite(
        `/api/v4/conversations/${encodeURIComponent(convId)}/settings/resolve`,
        { method: 'PATCH', json: inputs }),
    // Server-side folder-scoped metadata page.
    listByFolder: (folderId: string, opts?: EndpointOptions) =>
      get('/api/v4/conversations',
          { query: Object.assign({ folderId }, (opts && opts.before)
              ? { before: opts.before, before_id: opts.beforeId || '', limit: opts.limit || '' }
              : {}), onError: 'null', coalesce: true }),
    // Keyset-paginated global metadata page.
    listPage: (before: number, beforeId: string, limit: number) =>
      get('/api/v4/conversations',
          { query: { before: before || '', before_id: beforeId || '', limit: limit || '' },
            onError: 'null', coalesce: true }),
    // Manual rename — title-only PATCH. Returns parsed {ok, title} or null.
    setTitle: (convId: string, title: string) =>
      patch(`/api/v4/conversations/${encodeURIComponent(convId)}/title`,
            { title }, { onError: 'null' }),
    // LLM-generated title. Returns parsed {ok, title} or null on failure.
    // `lang` ('zh'|'en') forces the title language to match the UI; defaults
    // to the current interface language.
    generateTitle: (convId: string, lang: string) =>
      post(`/api/v4/conversations/${encodeURIComponent(convId)}/generate-title`,
           { lang: lang || readLanguage() },
           { onError: 'null' }),
    getDebugMessages: (convId: string, systemPrompt: string) =>
      get(`/api/v4/conversations/${encodeURIComponent(convId)}/debug-messages`,
          { query: { systemPrompt: systemPrompt || '' }, onError: 'null' }),
    // Move to recoverable server trash; restore and clone are atomic Sidecar
    // lifecycle transitions and never upload browser-held message arrays.
    remove: (convId: string) =>
      _idempotentResponseWrite(
        `/api/v4/conversations/${encodeURIComponent(convId)}`,
        { method: 'DELETE' }),
    restore: (convId: string) =>
      post(`/api/v4/conversations/${encodeURIComponent(convId)}/restore`, {},
           { headers: { 'Idempotency-Key': newIdempotencyKey() } }),
    clone: (convId: string, conversationId: string, title: string) =>
      post(`/api/v4/conversations/${encodeURIComponent(convId)}/clone`,
           { conversationId, title },
           { headers: { 'Idempotency-Key': newIdempotencyKey() } }),
    // Full-text + title search across the user's conversations. Returns
    // an array of {id, matchField, matchSnippet, matchRole} or [] on
    // error/timeout. Pass {signal} for cancellation.
    // The catalog owns items; the search facade retains its empty fallback.
    searchPage: (query: string, cursor: number, opts?: EndpointOptions) => get('/api/v4/conversations/search',
      Object.assign({}, opts || {}, { query: { q: query, ...(cursor ? { cursor } : {}) }, onError: 'throw' })),
    search: async (query: string, opts?: EndpointOptions) => {
      const d = await get('/api/v4/conversations/search',
          Object.assign({ query: { q: query } }, opts || {}));
      if (d && Array.isArray(d.items)) return d.items;
      return [];
    },
    // Server-side extract-file-changes from a tool-rounds payload (v1).
    extractFileChanges: (toolRounds: unknown[]) =>
      post('/api/v4/messages/extract-file-changes', { toolRounds }, { onError: 'null' }),
    // Batch variant — `items` is an array of {toolRounds}; the response's
    // `results` array is aligned by index. Used to seed the bounded
    // file-change presentation cache in one round-trip.
    extractFileChangesBatch: (items: unknown[]) =>
      post('/api/v4/messages/extract-file-changes/batch', { items }, { onError: 'null' }),
    // Server-authoritative cost over a bounded array of
    // { usage, model, provider_id } items. Synchronous render misses are
    // micro-batched by core/cost.js; the response aligns by index.
    costBatch: (items: unknown[]) =>
      post('/api/v4/messages/cost/batch', { items }, { onError: 'null' }),
    // Server-authoritative config/settings resolution. JS ships only the
    // inputs; the server merges (lib/conv_config.py) and returns the canonical
    // dict. Both throw ApiError on non-OK so the caller keeps its error text.
    /* Bounded: the composer send pipeline awaits config/settings resolution
     * before submitting a turn; the transport only enforces a declared
     * timeout, so an unbounded stalled POST latched the composer send lock
     * until the page was reloaded. */
    resolveConfig: (payload: JsonObject) => _resolveAcrossWorkerRecovery(
      (timeout) => post('/api/v4/conversations/config/resolve', payload, { timeout }),
      'conversation config resolution'),
    resolveSettings: (payload: JsonObject) => _resolveAcrossWorkerRecovery(
      (timeout) => post('/api/v4/conversations/settings/resolve', payload, { timeout }),
      'conversation settings resolution'),
    // Boot-scoped hint: conversations interrupted by the last server stop.
    // Consumed once by the PendingResume prompter at page load; a null result
    // (offline / 503) simply shows no prompt.
    pendingResume: () => get('/api/v4/conversations/pending-resume', { onError: 'null' }),
    dismissPendingResume: (conversationIds: unknown[]) => post(
      '/api/v4/conversations/pending-resume/dismiss',
      conversationIds && conversationIds.length ? { conversationIds } : {},
      { onError: 'null' },
    ),
    // Sidebar metadata list (no message bodies). Returns the raw Response so
    // the caller can inspect 304 / 503 + Retry-After and pass If-None-Match /
    // an AbortSignal. opts: { prefetch, window, headers, signal }.
    listMeta: (opts?: EndpointOptions) => {
      opts = opts || {};
      const q: JsonObject = { meta: 1 };
      if (opts.prefetch) q.prefetch = opts.prefetch;
      if (opts.window) q.window = opts.window;
      // No onError:'null' — parse:'response' already returns the Response for
      // any HTTP status (incl. 304/503) without throwing; only a network drop
      // / abort throws, exactly like the raw fetch this replaced, so the
      // caller's retry loop + outer try/catch behave identically.
      return request<Response | null>('/api/v4/conversations', {
        method: 'GET', query: q, parse: 'response',
        headers: opts.headers || {}, signal: opts.signal,
      });
    },
  };
  return conversations;
}

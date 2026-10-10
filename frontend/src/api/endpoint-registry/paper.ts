/** paper endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions, JsonObject } from './contracts';
export function createPaperEndpoints(ports: EndpointPorts) {
  const { request, get, post, put, del, ApiError, _resolve } = ports;
  const paper = {
    libraryList:    ()                    => get('/api/v4/paper/library/summaries', { onError: 'null' }),
    libraryGet:     (id: string)                  => get(`/api/v4/paper/library/${encodeURIComponent(id)}`, { onError: 'null' }),
    libraryUpsert:  (id: string, body: JsonObject)            => put(`/api/v4/paper/library/${encodeURIComponent(id)}`, body, { onError: 'null' }),
    libraryDelete:  (id: string)                  => del<Response | null>(`/api/v4/paper/library/${encodeURIComponent(id)}`, { parse: 'response', onError: 'null' }),
    // Upload + fetch-arxiv-stream stay on /api/paper/* (multipart / SSE
    // carve-outs — not v1 envelope shape).
    upload:         (formData: FormData)            => request('/api/paper/upload', { method: 'POST', body: formData, timeout: 0 }),
    fetchArxivStream: (url: string, paperId: string, opts?: EndpointOptions) =>
      request<Response | null>('/api/paper/fetch-arxiv-stream',
              Object.assign({ method: 'POST', json: { url, paper_id: paperId || '' }, parse: 'response', timeout: 0 }, opts || {})),
    /* searchArxiv MUST throw on failure (default onError:'throw'), not
     * swallow to null: the caller renders null/!ok as "no papers found",
     * which made every server/upstream outage look like an empty result set
     * (2026-07-28 live 500 incident). The caller has a real error surface. */
    searchArxiv:    (query: string, maxResults: number)   =>
      post('/api/v4/paper/search-arxiv', { query, max_results: maxResults || 10 }),
    recommend:      (description: string, maxResults: number) =>
      post('/api/v4/paper/recommend', { description, max_results: maxResults || 6 }, { onError: 'null' }),
    // Streaming describe-to-recommend (server-owned task; polled like Q&A so
    // the transport matches the tab beside it — no SSE).
    recommendStart: (description: string, maxResults: number) =>
      post('/api/v4/paper/recommend/start', { description, max_results: maxResults || 6 }),
    recommendPoll:  (taskId: string, cursor: number)      =>
      request<Response | null>('/api/v4/paper/recommend/poll',
              { method: 'GET', query: { task_id: taskId, cursor }, parse: 'response', onError: 'null' }),
    recommendAbort: (taskId: string)              =>
      post('/api/v4/paper/recommend/abort', { task_id: taskId }, { onError: 'null' }),
    reparse:        (filename: string)            => post('/api/v4/paper/reparse', { filename }),
    // Range-bypass fallback: download a stored PDF as bytes for pdf.js
    // getDocument({data}) when a proxy strips HTTP Range. Goes through
    // request() so base-path resolution stays single-sourced here; caller
    // passes a client-owned AbortSignal + timeout:0 to own the deadline.
    // Returns a Uint8Array. Throws on non-2xx / empty.
    pdfArrayBuffer: async (path: string, opts?: EndpointOptions) => {
      const resp = await request<Response>(path, Object.assign({ method: 'GET', parse: 'response', timeout: 120000 }, opts || {}));
      if (!resp || !resp.ok) {
        throw new ApiError('HTTP ' + (resp ? resp.status : '0') + ' fetching PDF',
                           { status: resp ? resp.status : 0, url: path });
      }
      const buf = await resp.arrayBuffer();
      if (!buf || buf.byteLength === 0) throw new ApiError('Empty PDF response', { url: path });
      return new Uint8Array(buf);
    },
    // timeout:0 — /start does synchronous prep (image-manifest extraction,
    // injection sanitize, prompt build) that legitimately runs 10–40s before
    // it spawns the background task and returns. The default 30s client
    // timeout would fire an AbortController mid-prep, surfacing a phantom
    // "Failed" while the server task keeps running orphaned. The task is then
    // polled; no client-side deadline belongs on the start round-trip.
    reportStart:    (body: JsonObject, opts?: EndpointOptions)          => post('/api/v4/paper/report/start', body, Object.assign({ timeout: 0 }, opts || {})),
    reportPoll:     (taskId: string, cursor: number)      =>
      request<Response | null>('/api/v4/paper/report/poll',
              { method: 'GET', query: { task_id: taskId, cursor }, parse: 'response', onError: 'null' }),
    reportLookup:   (paperHash: string, lang: string, opts?: EndpointOptions) =>
      post('/api/v4/paper/report/lookup', { paper_hash: paperHash, lang }, Object.assign({ onError: 'null' }, opts || {})),
    reportResolve:  (paperHash: string, lang: string, paperText: string, opts?: EndpointOptions) =>
      post('/api/v4/paper/report/lookup', Object.assign(
        { lang, include_cache: true },
        paperHash ? { paper_hash: paperHash } : { paper_text: paperText || '' }
      ), Object.assign({ onError: 'null' }, opts || {})),
    reportCache:    (cacheBody: JsonObject)           => post('/api/v4/paper/report/cache', cacheBody, { onError: 'null' }),
    reportAbort:    (taskId: string)              => post<null>(`/api/v4/paper/report/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null', parse: 'none' }),
    // Deepen (on-demand section depth, reading-xp P3). Start has no client
    // deadline (same reasoning as reportStart); poll rides the GENERIC
    // task-routes factory shape ({ok, events, next_cursor, status}).
    deepenStart:    (body: JsonObject, opts?: EndpointOptions)          => post('/api/v4/paper/deepen/start', body, Object.assign({ timeout: 0 }, opts || {})),
    deepenPoll:     (taskId: string, cursor: number)      =>
      request<Response | null>(`/api/v4/paper/deepen/poll/${encodeURIComponent(taskId)}`,
              { method: 'GET', query: { cursor }, parse: 'response', onError: 'null' }),
    deepenAbort:    (taskId: string)              => post(`/api/v4/paper/deepen/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null' }),
    // Reader margin notes (reading-xp P4)
    notesList:      (paperHash: string, lang: string)     =>
      request('/api/v4/paper/notes', { method: 'GET', query: { paper_hash: paperHash, lang }, onError: 'null' }),
    notesCreate:    (body: JsonObject)                => post('/api/v4/paper/notes', body),
    notesUpdate:    (noteId: string, note: string)        =>
      request(`/api/v4/paper/notes/${encodeURIComponent(noteId)}`, { method: 'PATCH', body: JSON.stringify({ note }), headers: { 'Content-Type': 'application/json' }, onError: 'null' }),
    notesDelete:    (noteId: string)              =>
      request(`/api/v4/paper/notes/${encodeURIComponent(noteId)}`, { method: 'DELETE', onError: 'null' }),
    // Review Mode reuses ALL the report endpoints above — the report `lang`
    // arg carries the composite cache key ``review:<venue>:<uilang>`` opaquely.
    // Only the venue list needs its own (read-only) endpoint.
    reviewVenues:   ()                    =>
      request('/api/v4/paper/review/venues', { method: 'GET', onError: 'null' }),
    // OpenReview auto-fill (killer feature): server drives the browser bridge
    // to fill the review form on the active OpenReview tab, then STOPS before
    // Submit. Never client-side timed out — the bridge round-trips can be slow;
    // the server bounds each command. Returns the fill report (or a 409 with an
    // actionable message when not connected / not an OpenReview page / no form).
    openreviewAutofill: (body: JsonObject)            =>
      post('/api/v4/paper/openreview/autofill', body, { timeout: 0, onError: 'throw' }),
    // Agentic Q&A — server-owned TaskRuntime task (web_search/fetch_url, full
    // report + section-aware paper context). Polls like the report task.
    qaStart:        (body: JsonObject)                => post('/api/v4/paper/qa/start', body),
    qaPoll:         (taskId: string, cursor: number)      =>
      request<Response | null>('/api/v4/paper/qa/poll',
              { method: 'GET', query: { task_id: taskId, cursor }, parse: 'response', onError: 'null' }),
    qaAbort:        (taskId: string)              => post<null>(`/api/v4/paper/qa/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null', parse: 'none' }),
    translateStart: (body: JsonObject)                => post('/api/v4/paper/translate/start', body),
    translatePoll:  (taskId: string, cursor: number)      =>
      request<Response | null>('/api/v4/paper/translate/poll',
              { method: 'GET', query: { task_id: taskId, cursor }, parse: 'response', onError: 'null' }),
    translateAbort: (taskId: string)              => post(`/api/v4/paper/translate/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null' }),
    translateCache: (paperHash: string, lang: string)     => post('/api/v4/paper/translate/cache', { paper_hash: paperHash, lang }, { onError: 'null' }),
    // URL builder for the report-export endpoint (md/html/pdf). Returned
    // URL is a full link so the caller can use window.open() / anchor.href.
    exportUrl: (paperHash: string, lang: string, format: string) =>
      _resolve('/api/v4/paper/report/export?paper_hash=' + encodeURIComponent(paperHash) +
               '&lang=' + encodeURIComponent(lang || 'en') +
               '&format=' + encodeURIComponent(format || 'md')),
    // Paper podcast — server-owned task (report → spoken script → TTS audio),
    // polled exactly like the report task beside it. timeout:0 on start: the
    // route does report-gate + cache checks before spawning; the task itself
    // is then polled with no client-side deadline (same reason as reportStart).
    podcastStatus:  ()                   =>
      request('/api/v4/paper/podcast/status', { method: 'GET', onError: 'null' }),
    podcastStart:   (body: JsonObject)               => post('/api/v4/paper/podcast/start', body, { timeout: 0 }),
    podcastPoll:    (taskId: string, cursor: number)     =>
      request<Response | null>('/api/v4/paper/podcast/poll',
              { method: 'GET', query: { task_id: taskId, cursor }, parse: 'response', onError: 'null' }),
    podcastLookup:  (body: JsonObject)               => post('/api/v4/paper/podcast/lookup', body, { onError: 'null' }),
    podcastAbort:   (taskId: string)             => post<null>(`/api/v4/paper/podcast/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null', parse: 'none' }),
    podcastScript:  (paperHash: string, mode: string, lang: string) =>
      request('/api/v4/paper/podcast/script',
              { method: 'GET', query: { paper_hash: paperHash, mode, lang }, onError: 'null' }),
    // Paper video abstract — server-owned motion-engine task (report →
    // narrated MG video). Progress/results ride the motion endpoints below.
    videoStart:   (body: JsonObject)               => post('/api/v4/paper/video/start', body, { timeout: 0 }),
    videoLookup:  (body: JsonObject)               =>
      request('/api/v4/paper/video/lookup',
              { method: 'GET', query: { paper_hash: body.paper_hash }, onError: 'null' }),
  };
  return paper;
}

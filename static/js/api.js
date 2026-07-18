/* ═══════════════════════════════════════════════════════════════════════
   api.js — Unified Frontend API Client
   ═══════════════════════════════════════════════════════════════════════

   THE SINGLE SOURCE OF TRUTH for all backend HTTP calls from the frontend.

   Why this exists
   ───────────────
   Before this module the frontend made 123+ raw `fetch('/api/...')` calls
   scattered across 30+ JS files, each rebuilding URL handling, JSON
   parsing, error handling, and timeout logic. Migrating any single
   endpoint to /api/v1 required touching every call site.

   Architecture rule (enforced by tests/test_frontend_api_isolation.py)
   ───────────────────────────────────────────────────────────────────
   No JS file other than `api.js` may call `fetch('/api/...')` directly.
   New code MUST go through `Api.<domain>.<method>(...)`.

   Mapping policy
   ──────────────
   `api.js` owns the URL. Today it may hit a legacy `/api/foo` endpoint;
   tomorrow it can switch to `/api/v1/foo` with no caller change. The
   v1 backend surface (routes/api_v1/) is the long-term target — every
   method here is a candidate to point at v1 when the v1 route exists.

   Layout
   ──────
     Api.request(...)        — low-level request helper
     Api.get/post/put/del    — JSON convenience wrappers
     Api.stream(...)         — SSE / chunked streaming
     Api.<domain>.<method>   — domain-grouped public surface:
                                folders, conversations, chat, paper,
                                translate, daily, project, settings,
                                memory, mcp, oauth, optimizer, image,
                                pdf, browser, scheduler, ...

   Error model
   ───────────
   Every call resolves to a result object or throws ApiError. ApiError
   carries `.status`, `.code`, `.body`, `.url`. Callers that want a
   silent fallback can pass `{onError:'null'}` to the verb helpers, in
   which case rejections become `null` (and a console.warn is logged).
*/

(function (global) {
  'use strict';

  // ── Base URL helper (shared with core.js apiUrl()) ────────────────
  function _base() {
    if (typeof apiUrl === 'function') return apiUrl('');
    const p = (global.location && global.location.pathname) || '';
    // Strip a trailing index.html / trading.html (the trading SPA loads
    // api.js standalone, without core.js's apiUrl()) so the base resolves
    // to the deployment prefix, not the HTML file path.
    return p.replace(/\/(index\.html|trading\.html)?$/, '');
  }

  function _resolve(path) {
    if (!path) return _base();
    if (path.startsWith('http://') || path.startsWith('https://')) return path;
    if (!path.startsWith('/')) path = '/' + path;
    return _base() + path;
  }

  function _qs(params) {
    if (!params || typeof params !== 'object') return '';
    const parts = [];
    for (const k of Object.keys(params)) {
      const v = params[k];
      if (v === undefined || v === null) continue;
      if (Array.isArray(v)) {
        for (const item of v) parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(item));
      } else {
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
      }
    }
    return parts.length ? '?' + parts.join('&') : '';
  }

  class ApiError extends Error {
    constructor(message, opts) {
      super(message);
      this.name = 'ApiError';
      this.status = (opts && opts.status) || 0;
      this.code = (opts && opts.code) || null;
      this.body = (opts && opts.body) !== undefined ? opts.body : null;
      this.url = (opts && opts.url) || null;
    }
  }

  // ── Core request ──────────────────────────────────────────────────
  // opts:
  //   method        'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  //   query         {k:v} → ?k=v (encoded)
  //   json          object — JSON body, sets Content-Type
  //   body          raw body (string|FormData|Blob); used if json absent
  //   headers       extra headers
  //   timeout       ms (default 30000, 0 = none)
  //   parse         'json' (default) | 'text' | 'blob' | 'response' | 'none'
  //   signal        AbortSignal
  //   onError       'throw' (default) | 'null' — null returns null on failure
  async function request(path, opts) {
    opts = opts || {};
    const method = (opts.method || 'GET').toUpperCase();
    const url = _resolve(path) + _qs(opts.query);
    const headers = Object.assign({}, opts.headers || {});
    let body = opts.body;
    if (opts.json !== undefined) {
      headers['Content-Type'] = headers['Content-Type'] || 'application/json';
      body = JSON.stringify(opts.json);
    }
    const init = { method, headers };
    if (body !== undefined) init.body = body;

    // Timeout via AbortController unless caller provided a signal
    const userSignal = opts.signal;
    let timeoutId = null;
    let ctrl = null;
    const timeout = opts.timeout === 0 ? 0 : (opts.timeout || 30000);
    if (timeout > 0) {
      ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      if (ctrl) {
        init.signal = ctrl.signal;
        timeoutId = setTimeout(() => ctrl.abort(new ApiError('timeout', { url, code: 'timeout' })), timeout);
        if (userSignal) {
          if (userSignal.aborted) ctrl.abort(userSignal.reason);
          else userSignal.addEventListener('abort', () => ctrl.abort(userSignal.reason));
        }
      } else if (userSignal) {
        init.signal = userSignal;
      }
    } else if (userSignal) {
      init.signal = userSignal;
    }

    let resp;
    try {
      resp = await fetch(url, init);
    } catch (e) {
      if (timeoutId) clearTimeout(timeoutId);
      if (opts.onError === 'null') {
        console.warn('[Api] %s %s failed: %s', method, url, e && e.message);
        return null;
      }
      // Rethrow abort AND timeout DOMExceptions verbatim so callers that
      // branch on e.name ('AbortError' / 'TimeoutError' — e.g. the chat-start
      // 30s AbortSignal.timeout in main_send_pipeline.js) keep working exactly
      // as they did with a raw fetch.
      if (e && (e.name === 'AbortError' || e.name === 'TimeoutError')) throw e;
      throw new ApiError(e && e.message || 'network error', { url, code: 'network' });
    }
    if (timeoutId) clearTimeout(timeoutId);

    const parse = opts.parse || 'json';
    if (parse === 'response') return resp;

    if (!resp.ok) {
      let bodyData = null;
      try {
        const ct = resp.headers.get('content-type') || '';
        bodyData = ct.includes('application/json') ? await resp.json() : await resp.text();
      } catch (_) { /* ignore body parse error */ }
      const code = (bodyData && typeof bodyData === 'object' && bodyData.error) || null;
      const err = new ApiError(
        'HTTP ' + resp.status + ' on ' + method + ' ' + url,
        { status: resp.status, code, body: bodyData, url }
      );
      if (opts.onError === 'null') {
        console.warn('[Api] %s', err.message);
        return null;
      }
      throw err;
    }

    if (parse === 'none') return null;
    if (parse === 'text') return await resp.text();
    if (parse === 'blob') return await resp.blob();
    // 'json' — tolerate empty body
    const text = await resp.text();
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch (e) {
      if (opts.onError === 'null') {
        console.warn('[Api] %s %s returned non-JSON', method, url);
        return null;
      }
      throw new ApiError('invalid JSON response', { url, code: 'parse', body: text });
    }
  }

  // ── Verb wrappers ────────────────────────────────────────────────
  function get(path, opts) { return request(path, Object.assign({ method: 'GET' }, opts || {})); }
  function post(path, json, opts) { return request(path, Object.assign({ method: 'POST', json }, opts || {})); }
  function put(path, json, opts) { return request(path, Object.assign({ method: 'PUT', json }, opts || {})); }
  function patch(path, json, opts) { return request(path, Object.assign({ method: 'PATCH', json }, opts || {})); }
  function del(path, opts) { return request(path, Object.assign({ method: 'DELETE' }, opts || {})); }

  // ── Streaming (SSE / chunked text) ───────────────────────────────
  // Returns the raw Response (parse='response'). Caller pipes
  // resp.body.getReader(). For SSE the chat stream code already does
  // line buffering — we just hand back the response.
  function stream(path, opts) {
    return request(path, Object.assign(
      { method: 'GET', timeout: 0, parse: 'response' },
      opts || {}
    ));
  }

  // ────────────────────────────────────────────────────────────────
  //  Domain surface
  //
  //  Each domain object is a thin wrapper that owns its URLs. When
  //  the backend migrates an endpoint to /api/v1, change ONLY the
  //  URL here — every caller stays the same.
  // ────────────────────────────────────────────────────────────────

  // folders ---------------------------------------------------------
  const folders = {
    list:   async ()           => (await get('/api/v1/folders', { onError: 'null' })) || [],
    create: (name, color)      => post('/api/v1/folders', { name, color: color || '' }, { onError: 'null' }),
    update: (id, updates)      => put(`/api/v1/folders/${encodeURIComponent(id)}`, updates, { onError: 'null' }),
    remove: async (id)         => {
      const r = await del(`/api/v1/folders/${encodeURIComponent(id)}`, { parse: 'response', onError: 'null' });
      return !!(r && r.ok);
    },
  };

  // orchestrations --------------------------------------------------
  const orchestrations = {
    list:     async ()        => (await get('/api/v1/orchestrations', { onError: 'null' })) || [],
    get:      (id)            => get(`/api/v1/orchestrations/${encodeURIComponent(id)}`, { onError: 'null' }),
    create:   (def)           => post('/api/v1/orchestrations', def, { parse: 'response' }),
    update:   (id, def)       => put(`/api/v1/orchestrations/${encodeURIComponent(id)}`, def, { parse: 'response' }),
    remove:   async (id)      => {
      const r = await del(`/api/v1/orchestrations/${encodeURIComponent(id)}`, { parse: 'response', onError: 'null' });
      return !!(r && r.ok);
    },
    validate: (def)           => post('/api/v1/orchestrations/validate', def, { onError: 'null' }),
    layout:   (def)           => post('/api/v1/orchestrations/layout', { definition: def }, { onError: 'null' }),
    compose:  (requirement, current, history) =>
                  post('/api/v1/orchestrations/compose',
                       { requirement, current: current || null, history: history || [] },
                       { onError: 'null' }),
    builtin:  (name)          => get(`/api/v1/orchestrations/builtin/${encodeURIComponent(name)}`, { onError: 'null' }),
    roleSchema: ()            => get('/api/v1/orchestrations/role-schema', { onError: 'null' }),
    plan:     (def)           => post('/api/v1/orchestrations/plan', { definition: def }, { onError: 'null' }),
    run:      (def, input)    => post('/api/v1/orchestrations/run', { definition: def, input: input || '' }, { onError: 'null' }),
    runPoll:  (taskId, cursor) => get(`/api/v1/orchestrations/run/poll/${encodeURIComponent(taskId)}?cursor=${cursor || 0}`, { onError: 'null' }),
    runAbort: (taskId)        => post(`/api/v1/orchestrations/run/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null' }),
    humanApprove: (requestId, approved) =>
                  post('/api/v1/orchestrations/run/human-approve',
                       { requestId, approved: !!approved }, { onError: 'null' }),
    humanInput: (requestId, response) =>
                  post('/api/v1/orchestrations/run/human-input',
                       { requestId, response: response || '' }, { onError: 'null' }),
    // Durable run instances (Task Mode) — see docs/proposals/TASK_MODE.md.
    // taskCreate returns {ok, run_id}; taskEvents is the durable cursor
    // replay that survives reload/restart (unlike runPoll above).
    taskCreate:  (def, input, orchId) =>
                  post('/api/v1/orchestrations/tasks',
                       { definition: def || null, id: orchId || undefined,
                         input: input || '' },
                       { parse: 'response', onError: 'null' }),
    taskList:    (status, orchId)  => {
      const qs = new URLSearchParams();
      if (status) qs.set('status', status);
      if (orchId) qs.set('orch_id', orchId);
      const q = qs.toString();
      return get('/api/v1/orchestrations/tasks' + (q ? '?' + q : ''), { onError: 'null' });
    },
    taskGet:     (runId)           => get(`/api/v1/orchestrations/tasks/${encodeURIComponent(runId)}`, { onError: 'null' }),
    taskEvents:  (runId, cursor)   => get(`/api/v1/orchestrations/tasks/${encodeURIComponent(runId)}/events?cursor=${cursor || 0}`, { onError: 'null' }),
    taskAbort:   (runId)           => post(`/api/v1/orchestrations/tasks/${encodeURIComponent(runId)}/abort`, {}, { onError: 'null' }),
    taskRemove:  async (runId)     => {
      const r = await del(`/api/v1/orchestrations/tasks/${encodeURIComponent(runId)}`, { parse: 'response', onError: 'null' });
      return !!(r && r.ok);
    },
  };

  // memory ----------------------------------------------------------
  const memory = {
    list:           (scope)     => get('/api/v1/memory', { query: { scope: scope || 'all' } }),
    create:         (entry)     => post('/api/v1/memory', entry, { parse: 'response' }),
    remove:         (id)        => del(`/api/v1/memory/${encodeURIComponent(id)}`, { parse: 'response' }),
    toggle:         (id)        => post(`/api/v1/memory/${encodeURIComponent(id)}/toggle`, undefined, { parse: 'response' }),
    files:          (id)        => get(`/api/v1/memory/${encodeURIComponent(id)}/files`),
    install:        (formData)  => request('/api/v1/memory/install', { method: 'POST', body: formData, parse: 'response' }),
    catalog:        ()          => get('/api/v1/memory/catalog'),
    catalogInstall: (skillId, scope) => post('/api/v1/memory/catalog/install', { skill_id: skillId, scope: scope || 'project' }, { parse: 'response' }),
  };

  // profile (personal-preference profile) ---------------------------
  const profile = {
    get:           ()         => get('/api/v1/profile'),
    save:          (body)     => put('/api/v1/profile', { body: body || '' }),
    saveItems:     (items)    => put('/api/v1/profile', { items: items || [] }),
    resolvePending: (id, accept, text) =>
      post(`/api/v1/profile/pending/${encodeURIComponent(id)}`,
           { accept: !!accept, text: text }, { parse: 'response' }),
  };

  // timer -----------------------------------------------------------
  const timer = {
    list:     ()              => get('/api/v1/timer/list'),
    trigger:  (id)            => post(`/api/v1/timer/${encodeURIComponent(id)}/trigger`, undefined),
    cancel:   (id)            => post(`/api/v1/timer/${encodeURIComponent(id)}/cancel`, undefined, { onError: 'null' }),
    status:   (id, limit)     => get(`/api/v1/timer/${encodeURIComponent(id)}/status`, { query: { limit: limit || 20 } }),
  };

  // scheduler -------------------------------------------------------
  const scheduler = {
    proactiveStatus: ()              => get('/api/v1/scheduler/proactive/status'),
    triggerTask:     (taskId)        => post(`/api/v1/scheduler/tasks/${encodeURIComponent(taskId)}/trigger`, undefined),
    pauseTask:       (taskId)        => post(`/api/v1/scheduler/tasks/${encodeURIComponent(taskId)}/pause`, undefined, { onError: 'null' }),
    resumeTask:      (taskId)        => post(`/api/v1/scheduler/tasks/${encodeURIComponent(taskId)}/resume`, undefined, { onError: 'null' }),
    pollLog:         (taskId, limit) => get(`/api/v1/scheduler/tasks/${encodeURIComponent(taskId)}/poll-log`, { query: { limit: limit || 20 } }),
  };

  // optimizer -------------------------------------------------------
  const optimizer = {
    proposals: (limit)        => get('/api/v1/optimizer/proposals', { query: { limit: limit || 60 } }),
    approve:   (id)           => post(`/api/v1/optimizer/proposals/${encodeURIComponent(id)}/approve`, undefined),
    reject:    (id, reason)   => post(`/api/v1/optimizer/proposals/${encodeURIComponent(id)}/reject`, { reason: reason || '' }),
    revert:    (id)           => post(`/api/v1/optimizer/proposals/${encodeURIComponent(id)}/revert`, undefined),
    runNow:    (opts)         => post('/api/v1/optimizer/run-now', Object.assign({ dry_run: false, window_hours: 24 }, opts || {})),
  };

  // compactions (per-conversation snapshots) ------------------------
  const compactions = {
    list: (convId)            => get(`/api/v1/conversations/${encodeURIComponent(convId)}/compactions`),
    get:  (convId, archiveId) => get(`/api/v1/conversations/${encodeURIComponent(convId)}/compactions/${encodeURIComponent(archiveId)}`),
    // Manual /compact: persistently compress old history into a summary.
    // Throws ApiError on 409 task_active / 422 nothing_to_compact / etc. so
    // the caller can branch on `.code`.
    compactNow: (convId, opts) =>
      post(`/api/v1/conversations/${encodeURIComponent(convId)}/compact`, opts || {}),
  };

  // conversations ---------------------------------------------------
  // get: parsed JSON or null on 4xx/network error. Pass {signal} to abort.
  // getResponse: raw Response — for callers that need to inspect status
  //   codes (e.g. distinguish 503 retryable from 404 ghost).
  // patchSettings: targeted settings PATCH — fire-and-forget; returns
  //   Response for callers who care, but typically chained as a fire-
  //   and-forget promise.
  // put: full conversation PUT — returns Response so the caller can
  //   read status (200 / 409 stale-checkpoint / etc.) and parse the body.
  // getDebugMessages: server-side rendered message list with system prompt.
  const conversations = {
    get: (convId, opts) =>
      get(`/api/v1/conversations/${encodeURIComponent(convId)}`,
          Object.assign({ onError: 'null' }, opts || {})),
    getResponse: (convId, opts) =>
      request(`/api/v1/conversations/${encodeURIComponent(convId)}`,
              Object.assign({ method: 'GET', parse: 'response', onError: 'null' }, opts || {})),
    // Lightweight hover-preview: {id, title, firstUserMessage, msgCount}.
    // Parses only the opening user turn server-side, so the response is tiny
    // even for a huge conversation. Used by the Project Brain panel to resolve
    // opaque conversation IDs to a readable preview on hover. null on failure.
    preview: (convId) =>
      get(`/api/v1/conversations/${encodeURIComponent(convId)}/preview`,
          { onError: 'null' }),
    patchSettings: (convId, patch) =>
      request(`/api/v1/conversations/${encodeURIComponent(convId)}/settings`,
              { method: 'PATCH', json: patch, parse: 'response', onError: 'null' }),
    // Server-side folder-scoped member query — resolves a folder's members by
    // their real folderId, INDEPENDENT of the top-N sidebar window (so members
    // that sort past the cap are still returned). Envelope:
    //   { conversations:[metaRow], hasMore, nextBefore, nextBeforeId, totalCount }
    listByFolder: (folderId, opts) =>
      get('/api/v1/conversations',
          { query: Object.assign({ folderId }, (opts && opts.before)
              ? { before: opts.before, before_id: opts.beforeId || '', limit: opts.limit || '' }
              : {}), onError: 'null' }),
    // Keyset-paginated global list page (older than the given cursor). Used by
    // the sidebar "load more" affordance so conversations past the first window
    // stay reachable. Same envelope shape as listByFolder (minus totalCount).
    listPage: (before, beforeId, limit) =>
      get('/api/v1/conversations',
          { query: { before: before || '', before_id: beforeId || '', limit: limit || '' },
            onError: 'null' }),
    put: (convId, body) =>
      request(`/api/v1/conversations/${encodeURIComponent(convId)}`,
              { method: 'PUT', json: body, parse: 'response', onError: 'null' }),
    // Manual rename — title-only PATCH. Returns parsed {ok, title} or null.
    setTitle: (convId, title) =>
      patch(`/api/v1/conversations/${encodeURIComponent(convId)}/title`,
            { title }, { onError: 'null' }),
    // LLM-generated title. Returns parsed {ok, title} or null on failure.
    // `lang` ('zh'|'en') forces the title language to match the UI; defaults
    // to the current interface language.
    generateTitle: (convId, lang) =>
      post(`/api/v1/conversations/${encodeURIComponent(convId)}/generate-title`,
           { lang: lang || (typeof _i18nLang !== 'undefined' ? _i18nLang : 'zh') },
           { onError: 'null' }),
    getDebugMessages: (convId, systemPrompt) =>
      get(`/api/v1/conversations/${encodeURIComponent(convId)}/debug-messages`,
          { query: { systemPrompt: systemPrompt || '' }, onError: 'null' }),
    // Branch create returns Response so the caller parses {ok, branch,
    // branch_idx} / inspects status itself (matches the legacy fetch shape
    // it replaced in branch.js).
    createBranch: (convId, msgIdx, body) =>
      post(`/api/v1/conversations/${encodeURIComponent(convId)}/messages/${encodeURIComponent(msgIdx)}/branches`,
           body, { parse: 'response', onError: 'null' }),
    // Branch delete returns Response so the caller can inspect status / body.
    // opts.msgId (when present) is sent as ?msgId= so the server resolves the
    // anchor's CURRENT absolute index by stable id (windowed-read safe).
    deleteBranch: (convId, msgIdx, branchIdx, opts) => {
      const q = {};
      const msgId = opts && opts.msgId;
      if (msgId) q.msgId = msgId;
      return del(`/api/v1/conversations/${encodeURIComponent(convId)}/messages/${encodeURIComponent(msgIdx)}/branches/${encodeURIComponent(branchIdx)}`,
          { query: q, parse: 'response', onError: 'null' });
    },
    // Hard delete a conversation. Fire-and-forget — caller may chain a
    // .catch() to log; we return Response so they can also inspect status.
    remove: (convId) =>
      del(`/api/v1/conversations/${encodeURIComponent(convId)}`,
          { parse: 'response' }),
    // Full-text + title search across the user's conversations. Returns
    // an array of {id, matchField, matchSnippet, matchRole} or [] on
    // error/timeout. Pass {signal} for cancellation.
    search: (query, opts) =>
      get('/api/v1/conversations/search',
          Object.assign({ query: { q: query } }, opts || {})),
    // Targeted message PATCH. by-id is preferred (id-stable across rebuilds);
    // falls back to index addressing on 404. Returns parsed JSON or null on
    // error. Caller passes {msgId, msgIdx} (msgIdx required for fallback).
    patchMessage: async (convId, msgIdx, patch, opts) => {
      opts = opts || {};
      const msgId = opts.msgId || null;
      const url = msgId
        ? `/api/v1/conversations/${encodeURIComponent(convId)}/messages/by-id/${encodeURIComponent(msgId)}`
        : `/api/v1/conversations/${encodeURIComponent(convId)}/messages/${encodeURIComponent(msgIdx)}`;
      const res = await request(url, { method: 'PATCH', json: patch, parse: 'response', onError: 'null' });
      if (res && res.ok) return await res.json().catch(() => null);
      // 404 on by-id fallback to index path
      if (res && res.status === 404 && msgId && msgIdx != null) {
        const fallback = await request(
          `/api/v1/conversations/${encodeURIComponent(convId)}/messages/${encodeURIComponent(msgIdx)}`,
          { method: 'PATCH', json: patch, parse: 'response', onError: 'null' }
        );
        if (fallback && fallback.ok) return await fallback.json().catch(() => null);
        return { _error: true, _status: fallback ? fallback.status : 0,
                 _body: fallback ? await fallback.json().catch(() => null) : null };
      }
      return { _error: true, _status: res ? res.status : 0,
               _body: res ? await res.json().catch(() => null) : null };
    },
    // Targeted message DELETE. mode='turn' deletes user+assistant pair;
    // mode='single' deletes only this index. Returns Response (status-aware).
    // opts.msgId (when present) is sent so the server resolves the CURRENT
    // index by stable id — drift-proof if the persisted list shrank/shifted
    // (server-side reconcile) between the client's read and this request.
    deleteMessage: (convId, msgIdx, mode, opts) => {
      const q = { mode: mode || 'single' };
      const msgId = opts && opts.msgId;
      if (msgId) q.msgId = msgId;
      return del(`/api/v1/conversations/${encodeURIComponent(convId)}/messages/${encodeURIComponent(msgIdx)}`,
          { query: q, parse: 'response', onError: 'null' });
    },
    // Apply a pending tool-toggle change to an active conversation: clears the
    // per-conversation tool-schema latch so the next round re-assembles tools
    // from the current toggles (accepts a one-time prompt-cache rebuild).
    // Returns parsed {ok, conv_id} or null on error.
    applyToolset: (convId) =>
      post(`/api/v1/conversations/${encodeURIComponent(convId)}/toolset/apply`,
           {}, { onError: 'null' }),
    // Server-side extract-file-changes from a tool-rounds payload (v1).
    extractFileChanges: (toolRounds) =>
      post('/api/v1/messages/extract-file-changes', { toolRounds }, { onError: 'null' }),
    // Batch variant — `items` is an array of {toolRounds}; the response's
    // `results` array is aligned by index. Used by renderChat to seed the
    // file-changes-bar cache for a whole conversation in one round-trip.
    extractFileChangesBatch: (items) =>
      post('/api/v1/messages/extract-file-changes/batch', { items }, { onError: 'null' }),
    // Server-authoritative per-usage cost. Returns parsed { ...cost } / { no_charge }
    // or null on error. See lib/cost.py + lib/pricing.py.
    cost: (usage, model, providerId) =>
      post('/api/v1/messages/cost',
           { usage, model, provider_id: providerId || null }, { onError: 'null' }),
    // Batch cost over an array of { usage, model, provider_id } items; the
    // response `costs` array aligns by index. Returns parsed body or null.
    costBatch: (items) =>
      post('/api/v1/messages/cost/batch', { items }, { onError: 'null' }),
    // Server-authoritative config/settings resolution. JS ships only the
    // inputs; the server merges (lib/conv_config.py) and returns the canonical
    // dict. Both throw ApiError on non-OK so the caller keeps its error text.
    resolveConfig:   (payload) => post('/api/v1/conversations/config/resolve', payload),
    resolveSettings: (payload) => post('/api/v1/conversations/settings/resolve', payload),
    // Sidebar metadata list (no message bodies). Returns the raw Response so
    // the caller can inspect 304 / 503 + Retry-After and pass If-None-Match /
    // an AbortSignal. opts: { prefetch, headers, signal }.
    listMeta: (opts) => {
      opts = opts || {};
      const q = { meta: 1 };
      if (opts.prefetch) q.prefetch = opts.prefetch;
      // No onError:'null' — parse:'response' already returns the Response for
      // any HTTP status (incl. 304/503) without throwing; only a network drop
      // / abort throws, exactly like the raw fetch this replaced, so the
      // caller's retry loop + outer try/catch behave identically.
      return request('/api/v1/conversations', {
        method: 'GET', query: q, parse: 'response',
        headers: opts.headers || {}, signal: opts.signal,
      });
    },
  };

  // text utilities --------------------------------------------------
  // Server-side language detection (mirrors lib/text_lang.is_predominantly_chinese).
  const text = {
    // opts.forceFasttext: force the statistical detector (skip-gate callers
    // that must distinguish kanji-heavy Japanese from Chinese pass true).
    detectLanguage: (textBody, opts = {}) => post('/api/v1/text/detect-language',
      { text: textBody, forceFasttext: !!opts.forceFasttext }, { onError: 'null' }),
  };

  // translate -------------------------------------------------------
  // Sync translate (one-shot) and async-task variants. Sync `run` returns
  // the parsed body (per upstream contract: { translated, error?, ... });
  // it follows the legacy "always parse JSON, even on HTTP error" shape so
  // translation.js can extract typed error envelopes.
  const translate = {
    run: async (body, opts) => {
      const resp = await request('/api/v1/translate', Object.assign({
        method: 'POST', json: body, parse: 'response',
      }, opts || {}));
      const data = await resp.json().catch(() => ({}));
      data._status = resp.status;
      data._statusText = resp.statusText;
      data._ok = resp.ok;
      return data;
    },
    start:     (body)         => post('/api/v1/translate/start', body),
    poll:      async (taskId) => {
      const resp = await request(`/api/v1/translate/poll/${encodeURIComponent(taskId)}`, {
        method: 'GET', parse: 'response', onError: 'null',
      });
      if (!resp) return { status: 'error', error: 'network' };
      if (resp.status === 404) return { status: 'not_found' };
      if (!resp.ok) return { status: 'error', error: `HTTP ${resp.status}` };
      try { return await resp.json(); } catch (e) { return { status: 'error', error: e.message }; }
    },
    pollBatch: (taskIds)      => post('/api/v1/translate/poll-batch', { taskIds }, { onError: 'null' }),
    // Settings → MT-config probe. Backend returns {ok, translated, error?}.
    mtTest:    (mtConfig, text) => post('/api/v1/translate/mt-test', { mt_config: mtConfig, text }, { onError: 'null' }),
  };

  // chat (task / streaming control plane) ---------------------------
  // Stream lifecycle methods. The SSE chat stream is consumed through
  // chat.streamResponse() below (ui/sse_pipeline.js + branch.js) — it hands
  // back the raw Response so callers can pipe `.body.getReader()`. No JS file
  // calls fetch('/api/chat/stream/...') directly.
  const chat = {
    // Outbound message lifecycle.
    // send() / regenerate() return the raw Response so callers can inspect
    // .ok / .status, parse the JSON body themselves, and pass through abort
    // signals. The body shape (taskId|queued|userMessage|title|msgCount|...)
    // is route-defined; we don't pre-parse it. For convenience, errors of
    // the network/abort kind reject so the caller's try/catch handles them
    // (matches the legacy fetch shape these were lifted from).
    // start() kicks off a fresh assistant turn (normal, non-endpoint mode).
    // Returns the raw Response so the caller reads .ok/.status + parses
    // {taskId} itself. Pass {signal} for the 30s startup timeout.
    start:       (body, opts)   => request('/api/v1/chat/start',
                                           Object.assign({ method: 'POST', json: body, parse: 'response', timeout: 0 },
                                                         opts || {})),
    send:        (body, opts)   => request('/api/v1/chat/send',
                                           Object.assign({ method: 'POST', json: body, parse: 'response', timeout: 0 },
                                                         opts || {})),
    regenerate:  (body, opts)   => request('/api/v1/chat/regenerate',
                                           Object.assign({ method: 'POST', json: body, parse: 'response', timeout: 0 },
                                                         opts || {})),
    continue:    (body, opts)   => request('/api/v1/chat/continue',
                                           Object.assign({ method: 'POST', json: body, parse: 'response', timeout: 0 },
                                                         opts || {})),
    branchStart: (body)         => post('/api/v1/chat/branch', body),
    // Abort + queue management — fire-and-forget, swallow errors.
    abortTask:    (taskId)      => post(`/api/v1/chat/abort/${encodeURIComponent(taskId)}`, undefined, { onError: 'null', parse: 'none' }),
    abortConv:    (convId)      => post(`/api/v1/chat/abort-conv/${encodeURIComponent(convId)}`, undefined, { onError: 'null', parse: 'none' }),
    queueGet:     (convId)      => get(`/api/v1/chat/queue/${encodeURIComponent(convId)}`),
    queueRemove:  (convId, qId) => del(`/api/v1/chat/queue/${encodeURIComponent(convId)}/${encodeURIComponent(qId)}`,
                                       { parse: 'response', onError: 'null' }),
    queueClear:   (convId)      => del(`/api/v1/chat/queue/${encodeURIComponent(convId)}`,
                                       { parse: 'response', onError: 'null' }),
    // Arm autopilot mid-stream — "take over from here" while a reply is
    // streaming. Persists autopilotEnabled + flips the live task's config
    // so the virtual user takes over at the next natural stop.
    armAutopilot: (convId)      => post('/api/v1/chat/autopilot/arm', { convId },
                                        { onError: 'null' }),
    // Disarm autopilot — clears the persistent armed-marker + flips live
    // config off. Backs the queue-bar cancel button and toggle-OFF gesture.
    disarmAutopilot: (convId)   => post('/api/v1/chat/autopilot/disarm', { convId },
                                        { onError: 'null' }),
    // Kick autopilot on a FINISHED conversation — "push it forward". Spawns a
    // carrier task that runs the virtual-user hook directly (no worker turn).
    // Returns {taskId} or throws ApiError (409 when a task is already running).
    kickAutopilot: (convId, config) => post('/api/v1/chat/autopilot/kick',
                                            { convId, config }),
    // Active-tasks listing — used on init to reconnect SSE streams.
    active:       (opts)        => get('/api/v1/chat/active', Object.assign({ onError: 'null' }, opts || {})),
    activeResponse: (opts)      => request('/api/v1/chat/active', Object.assign({ method: 'GET', parse: 'response', onError: 'null' }, opts || {})),
    // Tool-state PATCH — debounced settings flush from main.js.
    patchToolState: (convId, settings) =>
      request(`/api/v1/chat/tool-state/${encodeURIComponent(convId)}`,
              { method: 'PATCH', json: settings, parse: 'response', onError: 'null' }),
    poll: (taskId, opts) =>
      request(`/api/v1/chat/poll/${encodeURIComponent(taskId)}`,
              Object.assign({ method: 'GET', parse: 'response', onError: 'null' }, opts || {})),
    // Per-node orchestration-flow run trace (resolved brief + bounded I/O per
    // node) for the Studio canvas/inspector overlay. {ok, taskId, flowLabel, trace}.
    flowTrace: (taskId) =>
      get(`/api/v1/chat/flow-trace/${encodeURIComponent(taskId)}`, { onError: 'null' }),
    // streamResponse stays on /api/chat/stream/<id> — that's the SSE
    // carve-out (long-lived response, custom event format).
    streamResponse: (taskId, opts) =>
      stream(`/api/chat/stream/${encodeURIComponent(taskId)}`, opts || {}),
    // Interactive responses for stdin / human-guidance prompts opened by
    // tasks. Both routes return {ok, error?} JSON.
    stdinResponse:  (stdinId, input, eof) =>
      post('/api/v1/chat/stdin-response',
           { stdinId, input: input || '', ...(eof ? { eof: true } : {}) }),
    humanResponse:  (guidanceId, response) =>
      post('/api/v1/chat/human-response', { guidanceId, response }),
  };

  // endpoint (Planner→Worker→Critic mode) --------------------------
  // start() mirrors chat.start() for endpoint mode. Returns the raw Response.
  const endpoint = {
    start: (body, opts) => request('/api/v1/endpoint/start',
                                   Object.assign({ method: 'POST', json: body, parse: 'response', timeout: 0 },
                                                 opts || {})),
  };

  // logs (server-side log-noise cleaning / compression) -------------
  // clean() best-effort (null on any failure) so the banner just doesn't show.
  // compress() throws ApiError on non-OK so the caller's catch shows retry.
  const logs = {
    clean:    (text) => post('/api/v1/logs/clean', { text }, { onError: 'null' }),
    compress: async (text) => {
      const resp = await request('/api/v1/logs/compress',
                                 { method: 'POST', json: { text }, parse: 'response' });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.error || 'API error');
      return data;
    },
  };

  // update (self-update via git pull) -------------------------------
  // check:   GET  — compare local VERSION vs latest GitHub release tag
  // apply:   POST — launches git pull + pip install in a background thread,
  //                 returns {taskId} immediately. Progress streams over the
  //                 'update' push channel; terminal result is the 'done' frame.
  // restart: POST — re-exec the server process (explicit, admin-only)
  const update = {
    check:   (opts) => get('/api/v1/update/check', Object.assign({ onError: 'null' }, opts || {})),
    apply:   ()     => post('/api/v1/update/apply', {}),
    // restart takes {force, convId}. onError:'throw' (NOT 'null') is load-bearing:
    // the backend returns 409 {needsForce, runningTasks} when sibling
    // conversations have in-flight tasks, and update.js must READ that body to
    // show the informed force-confirm dialog. Swallowing it to null would make
    // the button silently no-op (the historical bug: a fixed empty {} body
    // dropped the caller's {force:true} → always force=false → always 409).
    restart: (payload) => post('/api/v1/update/restart', payload || {}, { onError: 'throw' }),
    // shutdown: POST — graceful manual stop (writes the manual-shutdown
    // marker so the next boot won't mistake it for an OS kill). No re-exec.
    shutdown: ()    => post('/api/v1/update/shutdown', {}, { onError: 'null' }),
  };

  // swarm (multi-agent control plane) -------------------------------
  // status: GET — current swarm state for a task. Top-level shape:
  //   {active, task_id, agents:[...], running, pending, completed, ...}
  //   or {active:false, message} when no swarm is registered for the task
  //   (e.g. session evicted after a server restart). Used by the
  //   stuck-panel reconciler in ui/streaming_ui.js to settle zombie panels.
  const swarm = {
    status: (taskId, opts) =>
      get(`/api/v1/swarm/status/${encodeURIComponent(taskId)}`,
          Object.assign({ onError: 'null' }, opts || {})),
    abort:  (taskId) =>
      post(`/api/v1/swarm/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null' }),
  };

  // health / status -------------------------------------------------
  const health = {
    // Returns Response so callers can inspect resp.ok cheaply.
    check: (opts) =>
      request('/api/health',
              Object.assign({ method: 'GET', parse: 'response', onError: 'null' }, opts || {})),
    // Parsed JSON variant — used by the Settings panel to show the version.
    info: () => get('/api/health', { onError: 'null' }),
  };

  // pricing ---------------------------------------------------------
  const pricing = {
    get: () => get('/api/v1/pricing', { onError: 'null' }),
  };

  // client-error reporting -----------------------------------------
  // Fire-and-forget telemetry. Failures are silently dropped — never
  // let reporting itself crash the page.
  const clientError = {
    report: (payload) => post('/api/client-error', payload, { onError: 'null', parse: 'none' }),
  };

  // server-config / browser-status / features -----------------------
  const serverConfig = {
    get:    () => get('/api/v1/server-config'),
    update: (payload) =>
      request('/api/v1/server-config',
              { method: 'POST', json: payload, parse: 'response' }),
    // Built-in (default) system prompt — used by the Settings system-prompt
    // editor to pre-fill / reset. project & tools flags shape the preview.
    defaultSystemPrompt: (project, tools) =>
      get('/api/v1/system-prompt/default?project=' + (project ? '1' : '0')
          + '&tools=' + (tools === false ? '0' : '1'), { onError: 'null' }),
    // Built-in system prompt split into toggleable blocks (id/title/text/
    // dynamic) — used by the per-block system-prompt editor.
    systemPromptBlocks: (project, tools) =>
      get('/api/v1/system-prompt/blocks?project=' + (project ? '1' : '0')
          + '&tools=' + (tools === false ? '0' : '1'), { onError: 'null' }),
  };

  // features (per-deployment toggles) ------------------------------
  const features = {
    set: (patch) =>
      request('/api/v1/features',
              { method: 'POST', json: patch, parse: 'response' }),
  };

  // providers (config-side: probes + templates + balance) -----------
  const providers = {
    templates:        ()                                =>
      request('/api/v1/providers/templates',
              { method: 'GET', parse: 'response', onError: 'null' }),
    probe:            (baseUrl, apiKey, modelsPath)     =>
      post('/api/v1/providers/probe',
           { base_url: baseUrl, api_key: apiKey, models_path: modelsPath },
           { onError: 'null' }),
    probeBulk:        (baseUrls, apiKey)                =>
      post('/api/v1/providers/probe-bulk',
           { base_urls: baseUrls, api_key: apiKey },
           { onError: 'null' }),
    probeCellsStart:  (body)                            =>
      post('/api/v1/providers/probe-cells/start', body, { onError: 'null' }),
    probeCellsStatus: (providerId)                      =>
      get('/api/v1/providers/probe-cells/status?provider_id=' + encodeURIComponent(providerId),
          { onError: 'null' }),
    balance:          (body)                            =>
      post('/api/v1/providers/balance', body, { onError: 'null' }),
    discoverModels:   (baseUrl, apiKey, modelsPath)     =>
      post('/api/v1/providers/discover-models',
           { base_url: baseUrl, api_key: apiKey, models_path: modelsPath },
           { onError: 'null' }),
    updateTemplate:   (key, models)                     =>
      request('/api/v1/providers/templates/update',
              { method: 'PUT', json: { key, models }, parse: 'response', onError: 'null' }),
  };

  // dispatch (model routing — observability + per-key overrides) ----
  const dispatch = {
    endpointMetrics: () => get('/api/v1/dispatch/endpoint-metrics', { onError: 'null' }),
    keyStats:        () => get('/api/v1/dispatch/key-stats', { onError: 'null' }),
    keyOverride:     (body) =>
      post('/api/v1/dispatch/key-override', body, { onError: 'null', parse: 'none' }),
  };

  // oauth (Claude / Codex login flows) ------------------------------
  // login/logout/callback have GET (querystring) and POST (json body)
  // variants — different deployments expose one or the other; we always
  // try POST first and let the caller fall back.
  const oauth = {
    status:      ()                 => get('/api/v1/oauth/status', { onError: 'null' }),
    loginPost:   (provider)         => post('/api/oauth/login', { provider }, { onError: 'null', parse: 'response' }),
    loginGet:    (provider)         =>
      request('/api/oauth/login',
              { method: 'GET', query: { provider }, parse: 'response', onError: 'null' }),
    logoutPost:  (provider)         => post('/api/oauth/logout', { provider }, { onError: 'null', parse: 'response' }),
    logoutGet:   (provider)         =>
      request('/api/oauth/logout',
              { method: 'GET', query: { provider }, parse: 'response', onError: 'null' }),
    callbackPost: (body)            => post('/api/oauth/callback', body, { onError: 'null', parse: 'response' }),
    storeToken:  (provider, token)  => post('/api/oauth/store-token', { provider, token }, { onError: 'null', parse: 'response' }),
    callbackGet:  (queryString)     =>
      // queryString is a pre-encoded "k=v&k2=v2" string — we hand it off raw
      // because the legacy code did the same. Future cleanup can switch to
      // a structured object.
      request('/api/oauth/callback' + (queryString ? '?' + queryString : ''),
              { method: 'GET', parse: 'response', onError: 'null' }),
  };

  // mcp (Model Context Protocol — server registry + connect) -------
  const mcp = {
    catalogList:      ()                           =>
      request('/api/v1/mcp/catalog',
              { method: 'GET', parse: 'response', onError: 'null' }),
    // Lightweight introspection: flat list of every connected MCP tool
    // ({tools, total, servers_connected}). Used by the per-turn context
    // capsule (info-rail.js) to surface active MCP tools without paying the
    // heavier catalog fetch.
    toolsList:        ()                           =>
      request('/api/v1/mcp/tools',
              { method: 'GET', parse: 'response', onError: 'null' }),
    // NOTE: no onError:'null' here — a failed connect returns HTTP 500
    // with a rich {error, stderr_tail} body; we want that to throw an
    // ApiError (carrying .body) so the UI can show the real reason
    // instead of a generic "无法连接" after the call silently nulls out.
    catalogInstall:   (id, env)                    =>
      // Returns fast: either {status:'ready'} (connected) or HTTP 202
      // {status:'installing'} when a cold `pip install` was kicked off in the
      // background. The UI then polls installStatus until ready/error, so the
      // request no longer blocks for minutes (a proxy could cut it).
      post('/api/v1/mcp/catalog/install', { id, env: env || {} }),
    // Poll an in-flight async install. 202 while installing, 200 ready (with
    // tool list), 500 error — all carry a {status} field. parse:'response'
    // so the caller inspects HTTP status + body.
    catalogInstallStatus: (id)                     =>
      request('/api/v1/mcp/catalog/install/status',
              { method: 'GET', query: { id }, parse: 'response', onError: 'null' }),
    catalogUninstall: (id, purge)                  =>
      post('/api/v1/mcp/catalog/uninstall',
           { id, ...(purge ? { purge: true } : {}) }, { onError: 'null' }),
    connectAll:       ()                           =>
      post('/api/v1/mcp/connect', {}, { onError: 'null' }),
    connectOne:       (server)                     =>
      post('/api/v1/mcp/connect', { server }, { onError: 'null' }),
    serverCreate:     (payload)                    =>
      post('/api/v1/mcp/servers', payload, { onError: 'null' }),
  };

  const browser = {
    status: () => get('/api/v1/browser/status'),
  };

  // authSources (login-walled fetch sources: Xiaohongshu, …) ---------
  const authSources = {
    list:   ()              => get('/api/v1/auth-sources', { onError: 'null' }),
    upsert: (body)          => post('/api/v1/auth-sources', body),
    toggle: (domain, on)    => post(`/api/v1/auth-sources/${encodeURIComponent(domain)}/toggle`, { enabled: !!on }, { onError: 'null' }),
    remove: (domain)        => del(`/api/v1/auth-sources/${encodeURIComponent(domain)}`, { parse: 'response', onError: 'null' }),
    // Interactive headful login — long-running; no client timeout.
    login:  (domain, timeout) => post(`/api/v1/auth-sources/${encodeURIComponent(domain)}/login`, { timeout: timeout || 180 }, { timeout: 0 }),
  };

  // trading (AI investment assistant SPA — trading.html) ------------
  // The trading page is a standalone SPA that loads api.js directly. All
  // its endpoints live under /api/v1/trading. `call()` preserves the exact
  // legacy contract of the old TradingApp.api(): always parse JSON, and on
  // a non-2xx throw Error(body.error || 'HTTP <status>'). `raw()` hands back
  // the Response for the SSE stream + progress-poll sites that read the body
  // incrementally or branch on resp.status.
  const _TRADING_BASE = '/api/v1/trading';
  const trading = {
    base: () => _resolve(_TRADING_BASE),
    call: async (path, opts) => {
      opts = opts || {};
      const resp = await request(_TRADING_BASE + path, Object.assign({
        method: opts.method || 'GET',
        headers: Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {}),
        body: opts.body,
        timeout: opts.timeout,
        signal: opts.signal,
        parse: 'response',
      }, {}));
      if (!resp.ok) {
        let err = {};
        try { err = await resp.json(); } catch (_) { /* non-JSON error body */ }
        throw new Error((err && err.error) || ('HTTP ' + resp.status));
      }
      return await resp.json();
    },
    // Raw Response for streaming / progress-poll endpoints. Caller pipes
    // resp.body.getReader() (SSE) or reads resp.json() and branches on
    // resp.status itself. timeout defaults to 0 (no client-side abort).
    raw: (path, opts) => {
      opts = opts || {};
      return request(_TRADING_BASE + path, {
        method: opts.method || 'GET',
        headers: Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {}),
        body: opts.body,
        timeout: opts.timeout === undefined ? 0 : opts.timeout,
        signal: opts.signal,
        parse: 'response',
      });
    },
  };

  // project ---------------------------------------------------------
  // Project Co-Pilot panel — set/clear active project, list recent paths,
  // rescan / undo, browse the filesystem, apply code edits, approve writes.
  // Backend mostly returns {ok, ...} JSON; mutations return Response so
  // callers can read .ok and parse error envelopes.
  const project = {
    status:        (convId)   => get('/api/v1/project/status'
                                     + (convId ? ('?conv_id=' + encodeURIComponent(convId)) : ''),
                                     { onError: 'null' }),
    setPaths:      (folders, readOnlyPaths)  =>
      request('/api/v1/project/paths',
              { method: 'PUT',
                json: { paths: folders, readOnlyPaths: readOnlyPaths || [] },
                parse: 'response' }),
    setPath:       (path)     =>
      request('/api/v1/project/set',
              { method: 'POST', json: { path }, parse: 'response' }),
    clear:         ()         =>
      request('/api/v1/project',
              { method: 'DELETE', parse: 'response', onError: 'null' }),
    recentList:    ()         => get('/api/v1/project/recent', { onError: 'null' }),
    recentSave:    (path)     =>
      request('/api/v1/project/recent',
              { method: 'POST', json: { path }, parse: 'response', onError: 'null' }),
    recentClear:   ()         =>
      request('/api/v1/project/recent',
              { method: 'DELETE', parse: 'response', onError: 'null' }),
    rescan:        ()         =>
      request('/api/v1/project/rescan',
              { method: 'POST', parse: 'response' }),
    undo:          (body)     =>
      request('/api/v1/project/undo',
              { method: 'POST', json: body, parse: 'response' }),
    undoAll:       (body)     =>
      request('/api/v1/project/undo-all',
              { method: 'POST', json: body || {}, parse: 'response' }),
    browse:        (path, showHidden) => post('/api/v1/project/browse', { path, showHidden: !!showHidden }),
    mkdir:         (parent, name)     =>
      request('/api/v1/project/mkdir',
              { method: 'POST', json: { parent, name }, parse: 'response' }),
    rmdir:         (path)             =>
      request('/api/v1/project/rmdir',
              { method: 'POST', json: { path }, parse: 'response' }),
    write:         (path, content)    => post('/api/v1/project/write', { path, content }),
    // Binary-safe drop-into-folder. `formData` carries file + dir (+ optional name).
    upload:        (formData)         =>
      request('/api/v1/project/upload', { method: 'POST', body: formData, timeout: 0 }),
    writeApproval: (approvalId, approved) =>
      post('/api/v1/project/write-approval', { approvalId, approved }),
    // Project-brain Activity Feed (read-only). Keyed on the explicit `path`
    // the caller holds — never the server's global active project.
    feed:          (path, sinceSeq) =>
      get('/api/v1/project/feed',
          { query: { path, since: sinceSeq || 0 }, onError: 'null' }),
    // Project-brain Charter (north star). read-only get + human-gated commit.
    charter:       (path) =>
      get('/api/v1/project/charter', { query: { path }, onError: 'null' }),
    commitCharter: (path, body) =>
      post('/api/v1/project/charter/commit', Object.assign({ path }, body || {})),
    // Unresolved proposals (single source for "awaiting you") + durable reject.
    charterPending: (path) =>
      get('/api/v1/project/charter/pending', { query: { path }, onError: 'null' }),
    dismissProposal: (path, proposalId, body) =>
      post('/api/v1/project/charter/dismiss',
           Object.assign({ path, proposalId }, body || {})),
    // Human-gated edit / delete of a committed decision (by list index) and
    // deletion of the whole charter. All optimistic-locked (expected_version).
    updateDecision: (path, index, text, body) =>
      post('/api/v1/project/charter/decision/update',
           Object.assign({ path, index, text }, body || {})),
    deleteDecision: (path, index, body) =>
      post('/api/v1/project/charter/decision/delete',
           Object.assign({ path, index }, body || {})),
    deleteCharter: (path, body) =>
      post('/api/v1/project/charter/delete', Object.assign({ path }, body || {})),
    // Project-brain Board (coordination kanban). read-only.
    board:         (path) =>
      get('/api/v1/project/board', { query: { path }, onError: 'null' }),
    // Board HUMAN mutations. All key strictly on the explicit `path`; `convId`
    // is the displayed conversation acting as the human's proxy. `post` needs
    // it (becomes created_by_conv → dispatch target); complete/block/reopen
    // tolerate an empty convId (lifecycle actions on an existing epic, no
    // dispatch target — feed event + audit record a blank actor honestly).
    /** @param {string} path
     *  @param {{title?: string, convId?: string, dependsOn?: string[]}} [opts] */
    boardPost:     (path, { title, convId, dependsOn } = {}) =>
      post('/api/v1/project/board/post',
           { path, title, convId, depends_on: dependsOn || [] }),
    boardComplete: (path, taskId, convId) =>
      post('/api/v1/project/board/complete', { path, taskId, convId: convId || '' }),
    boardBlock:    (path, taskId, convId, reason) =>
      post('/api/v1/project/board/block',
           { path, taskId, convId: convId || '', reason: reason || '' }),
    boardReopen:   (path, taskId, convId) =>
      post('/api/v1/project/board/reopen', { path, taskId, convId: convId || '' }),
    // Collaboration-bar one-shot summary (board + decisions + peer→epic join).
    // convId (optional) is excluded from activePeers/peerEpics so the count is
    // "OTHER conversations online" — matching the local push-mirror semantics.
    brainSummary:  (path, convId) =>
      get('/api/v1/project/brain/summary',
          { query: { path, convId: convId || '' }, onError: 'null' }),
    // LIVE peer/team roster (presence ⋈ task ⋈ claimed-epic). convId optional
    // — when present it's excluded so a conv never lists itself as a peer.
    brainPeers:    (path, convId) =>
      get('/api/v1/project/brain/peers',
          { query: { path, convId: convId || '' }, onError: 'null' }),
    // Pillar #7 human↔brain status lane. Latest synthesized status snapshot
    // (fresh-on-open via the staleness gate) + the append-only history trail.
    // opts: { force } — force=true warms a fresh snapshot in the background
    // (refresh=1). The response returns the cached snapshot instantly + a
    // `refreshing` flag; it never blocks on the LLM synthesis.
    brainStatus:   (path, opts) =>
      get('/api/v1/project/brain/status',
          { query: { path, refresh: (opts && opts.force) ? '1' : '' },
            onError: 'null' }),
    brainStatusHistory: (path, limit) =>
      get('/api/v1/project/brain/status/history',
          { query: { path, limit: limit || '' }, onError: 'null' }),
    // Read-only synthesis Q&A about the project status. Writes NOTHING.
    // Throws ApiError on refusal so the composer can surface the error.
    brainStatusAsk: (path, question) =>
      post('/api/v1/project/brain/status/ask', { path, question }),
    // Pillar #7 WATCH lane — the human's standing "things I care about" list.
    // brainWatchList(refresh) re-addresses open items on read (fresh-on-open).
    brainWatchList: (path, refresh) =>
      get('/api/v1/project/brain/watch',
          { query: { path, refresh: refresh ? '1' : '' }, onError: 'null' }),
    brainWatchAdd: (path, kind, text, convId) =>
      post('/api/v1/project/brain/watch/add', { path, kind, text, convId: convId || '' }),
    brainWatchUpdate: (itemId, action, extra) =>
      post('/api/v1/project/brain/watch/update',
           Object.assign({ itemId, action }, extra || {})),
    brainWatchAddress: (itemId) =>
      post('/api/v1/project/brain/watch/address', { itemId }),
    // Promote a watch item into the charter — the ONLY bridge to sibling
    // agents (human-gated charter commit). Throws ApiError on version skew.
    brainWatchPromote: (itemId, convId, expectedVersion) =>
      post('/api/v1/project/brain/watch/promote',
           { itemId, convId: convId || '', expectedVersion: expectedVersion }),
    // Per-conversation brain INFLUENCE — how THIS conv is affected by the
    // brain (charter bound by, epics owned vs avoided, decisions awaiting).
    brainInfluence: (path, convId) =>
      get('/api/v1/project/brain/influence',
          { query: { path, convId }, onError: 'null' }),
    // HUMAN nudge to a sibling conversation — the operator (acting via the
    // displayed conv `convId`) sends advisory `text` to `toConvId`. Reuses
    // send_peer_message server-side (same rate limit + self-send guard).
    // Throws ApiError on refusal so the composer can surface rate_limited.
    brainPeerMessage: (path, convId, toConvId, text) =>
      post('/api/v1/project/brain/peer-message',
           { path, convId: convId || '', toConvId, text }),
    // HUMAN hard-abort of a sibling conversation's running task(s). The
    // operator (acting via `convId`) stops `toConvId` — the authenticated
    // operator IS the approval (confirmed client-side), passed server-side as
    // approved_by and honored by the same audit gate. Aborts the TASK only.
    // Throws ApiError on refusal so the caller can surface the reason.
    brainPeerAbort: (path, convId, toConvId) =>
      post('/api/v1/project/brain/peer-abort',
           { path, convId: convId || '', toConvId }),
  };

  // paper-reader (library + report + translate + QA) ---------------
  // Library is row-keyed by paper id; upsert sends a partial body. The
  // report and translate flows use task-based polling — we hand back
  // raw responses for the streaming paths and parsed JSON for the
  // simpler ones.
  const paper = {
    libraryList:    ()                    => get('/api/v1/paper/library', { onError: 'null' }),
    libraryUpsert:  (id, body)            => put(`/api/v1/paper/library/${encodeURIComponent(id)}`, body, { onError: 'null' }),
    libraryDelete:  (id)                  => del(`/api/v1/paper/library/${encodeURIComponent(id)}`, { parse: 'response', onError: 'null' }),
    // upload + chat + fetch-arxiv-stream stay on /api/paper/* (multipart / SSE
    // carve-outs — not v1 envelope shape).
    upload:         (formData)            => request('/api/paper/upload', { method: 'POST', body: formData, timeout: 0 }),
    fetchArxivStream: (url, paperId, opts) =>
      request('/api/paper/fetch-arxiv-stream',
              Object.assign({ method: 'POST', json: { url, paper_id: paperId || '' }, parse: 'response', timeout: 0 }, opts || {})),
    searchArxiv:    (query, maxResults)   =>
      post('/api/v1/paper/search-arxiv', { query, max_results: maxResults || 10 }, { onError: 'null' }),
    recommend:      (description, maxResults) =>
      post('/api/v1/paper/recommend', { description, max_results: maxResults || 6 }, { onError: 'null' }),
    // Streaming describe-to-recommend (server-owned task; polled like Q&A so
    // the transport matches the tab beside it — no SSE).
    recommendStart: (description, maxResults) =>
      post('/api/v1/paper/recommend/start', { description, max_results: maxResults || 6 }),
    recommendPoll:  (taskId, cursor)      =>
      request('/api/v1/paper/recommend/poll',
              { method: 'GET', query: { task_id: taskId, cursor }, parse: 'response', onError: 'null' }),
    recommendAbort: (taskId)              =>
      post('/api/v1/paper/recommend/abort', { task_id: taskId }, { onError: 'null' }),
    chatStream:     (body, opts)          =>
      request('/api/paper/chat',
              Object.assign({ method: 'POST', json: body, parse: 'response', timeout: 0 }, opts || {})),
    reparse:        (filename)            => post('/api/v1/paper/reparse', { filename }),
    // Range-bypass fallback: download a stored PDF as bytes for pdf.js
    // getDocument({data}) when a proxy strips HTTP Range. Goes through
    // request() so base-path resolution stays single-sourced here; caller
    // passes a client-owned AbortSignal + timeout:0 to own the deadline.
    // Returns a Uint8Array. Throws on non-2xx / empty.
    pdfArrayBuffer: async (path, opts) => {
      const resp = await request(path, Object.assign({ method: 'GET', parse: 'response', timeout: 120000 }, opts || {}));
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
    reportStart:    (body, opts)          => post('/api/v1/paper/report/start', body, Object.assign({ timeout: 0 }, opts || {})),
    reportPoll:     (taskId, cursor)      =>
      request('/api/v1/paper/report/poll',
              { method: 'GET', query: { task_id: taskId, cursor }, parse: 'response', onError: 'null' }),
    reportLookup:   (paperHash, lang, opts) =>
      post('/api/v1/paper/report/lookup', { paper_hash: paperHash, lang }, Object.assign({ onError: 'null' }, opts || {})),
    reportCache:    (cacheBody)           => post('/api/v1/paper/report/cache', cacheBody, { onError: 'null' }),
    reportAbort:    (taskId)              => post(`/api/v1/paper/report/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null', parse: 'none' }),
    // Review Mode reuses ALL the report endpoints above — the report `lang`
    // arg carries the composite cache key ``review:<venue>:<uilang>`` opaquely.
    // Only the venue list needs its own (read-only) endpoint.
    reviewVenues:   ()                    =>
      request('/api/v1/paper/review/venues', { method: 'GET', onError: 'null' }),
    // OpenReview auto-fill (killer feature): server drives the browser bridge
    // to fill the review form on the active OpenReview tab, then STOPS before
    // Submit. Never client-side timed out — the bridge round-trips can be slow;
    // the server bounds each command. Returns the fill report (or a 409 with an
    // actionable message when not connected / not an OpenReview page / no form).
    openreviewAutofill: (body)            =>
      post('/api/v1/paper/openreview/autofill', body, { timeout: 0, onError: 'throw' }),
    // Agentic Q&A — server-owned TaskRuntime task (web_search/fetch_url, full
    // report + section-aware paper context). Polls like the report task.
    qaStart:        (body)                => post('/api/v1/paper/qa/start', body),
    qaPoll:         (taskId, cursor)      =>
      request('/api/v1/paper/qa/poll',
              { method: 'GET', query: { task_id: taskId, cursor }, parse: 'response', onError: 'null' }),
    qaAbort:        (taskId)              => post(`/api/v1/paper/qa/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null', parse: 'none' }),
    translateStart: (body)                => post('/api/v1/paper/translate/start', body),
    translatePoll:  (taskId, cursor)      =>
      request('/api/v1/paper/translate/poll',
              { method: 'GET', query: { task_id: taskId, cursor }, parse: 'response', onError: 'null' }),
    translateAbort: (taskId)              => post(`/api/v1/paper/translate/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null', parse: 'none' }),
    translateCache: (paperHash, lang)     => post('/api/v1/paper/translate/cache', { paper_hash: paperHash, lang }, { onError: 'null' }),
    // URL builder for the report-export endpoint (md/html/pdf). Returned
    // URL is a full link so the caller can use window.open() / anchor.href.
    exportUrl: (paperHash, lang, format) =>
      _resolve('/api/v1/paper/report/export?paper_hash=' + encodeURIComponent(paperHash) +
               '&lang=' + encodeURIComponent(lang || 'en') +
               '&format=' + encodeURIComponent(format || 'md')),
  };

  // daily-report (MyDay panel) -------------------------------------
  // Most reads are best-effort — errors should leave the panel empty,
  // not throw. Mutations return Response so the caller can inspect
  // .ok / parse the body for an error envelope.
  const daily = {
    calendar:   (year, month1Based) =>
      get(`/api/v1/daily-report/calendar/${encodeURIComponent(year)}/${encodeURIComponent(month1Based)}`,
          { onError: 'null' }),
    status:     (dateStr) =>
      get(`/api/v1/daily-report/status/${encodeURIComponent(dateStr)}`,
          { onError: 'null' }),
    convCount:  (dateStr) =>
      get(`/api/v1/daily-report/conv-count/${encodeURIComponent(dateStr)}`,
          { onError: 'null' }),
    // Triggers an async generation — body is the parsed JSON or null.
    generate:   (dateStr, force) =>
      request('/api/v1/daily-report/generate',
              { method: 'POST', json: { date: dateStr, force: !!force }, parse: 'response' }),
    // CRUD on this-day's task list. body is route-specific JSON.
    taskCreate: (body) =>
      request('/api/v1/daily-report/task',
              { method: 'POST', json: body, parse: 'response', onError: 'null' }),
    taskDelete: (body) =>
      request('/api/v1/daily-report/task',
              { method: 'DELETE', json: body, parse: 'response', onError: 'null' }),
    taskStatus: (body) =>
      request('/api/v1/daily-report/task-status',
              { method: 'PATCH', json: body, parse: 'response', onError: 'null' }),
    todoToggle: (body) =>
      request('/api/v1/daily-report/todo-toggle',
              { method: 'PATCH', json: body, parse: 'response', onError: 'null' }),
    inheritedTodoToggle: (body) =>
      request('/api/v1/daily-report/inherited-todo-toggle',
              { method: 'PATCH', json: body, parse: 'response', onError: 'null' }),
    inheritedTodoDelete: (body) =>
      request('/api/v1/daily-report/inherited-todo',
              { method: 'DELETE', json: body, parse: 'response', onError: 'null' }),
  };

  // images (generation + listing) ----------------------------------
  // generate() returns the parsed JSON body (per upstream contract:
  // {ok, image_url|image_b64, error?, ...}). The endpoint returns 200
  // even on logical errors with {ok:false, error:...}; transport-level
  // failures (network, abort) reject so the caller's catch handles
  // them. Pass {signal} for cancellation.
  const images = {
    generate: async (body, opts) => {
      const resp = await request('/api/v1/images/generate', Object.assign({
        method: 'POST', json: body, timeout: 0, parse: 'response',
      }, opts || {}));
      const data = await resp.json().catch(() => ({}));
      data._status = resp.status;
      return data;
    },
    models:   ()           => get('/api/v1/images/models'),
    // upload accepts either a JSON body (legacy {base64, mediaType})
    // or a FormData. Returns {url} on success, null on failure.
    upload: (payload, opts) => {
      if (typeof FormData !== 'undefined' && payload instanceof FormData) {
        return request('/api/images/upload', Object.assign({ method: 'POST', body: payload, timeout: 0 }, opts || {}));
      }
      return post('/api/images/upload', payload, Object.assign({ onError: 'null' }, opts || {}));
    },
  };

  // pdf -------------------------------------------------------------
  // parse:    sync extract → returns full JSON body (contract has data.success/error)
  // vlmStart: kick off async VLM parse, returns {taskId}
  // vlmPoll:  poll a single VLM task — full body (status / progress / result)
  // vlmTasks: lookup VLM tasks by filename
  const pdf = {
    parse:     (formData)            => request('/api/pdf/parse', { method: 'POST', body: formData, timeout: 0 }),
    vlmStart:  (formData)            => request('/api/pdf/vlm-parse', { method: 'POST', body: formData, timeout: 0 }),
    vlmPoll:   (taskId)              => get(`/api/v1/pdf/vlm-parse/${encodeURIComponent(taskId)}`, { onError: 'null' }),
    vlmTasks:  (filename)            => get('/api/v1/pdf/vlm-tasks', { query: { filename }, onError: 'null' }),
  };

  // docs (Office / plain text) -------------------------------------
  const doc = {
    parse: (formData) => request('/api/doc/parse', { method: 'POST', body: formData, timeout: 0 }),
  };

  // audio (speech-to-text / voice input) ---------------------------
  // transcribe: multipart audio blob → { ok, text, model, ... } (mirrors
  //   pdf.parse — timeout:0 because a transcription round-trip is slow).
  // capabilities: { available, models, maxBytes, maxDurationS } — drives the
  //   graceful hide of the mic button when no transcription model is configured.
  const audio = {
    transcribe:   (formData) => request('/api/v1/audio/transcribe', { method: 'POST', body: formData, timeout: 0 }),
    capabilities: ()         => get('/api/v1/audio/capabilities', { onError: 'null' }),
  };

  // artifacts (panel + library + version chain) ---------------------
  // v1 metadata routes are JSON; raw / view / export are intentional
  // carve-outs that ship typed binary or sandboxed HTML — we expose
  // them as URL builders so the consumer can set iframe.src or
  // anchor.href without going through the JSON request pipeline.
  const artifacts = {
    meta:        (id)               => get(`/api/v1/artifacts/${encodeURIComponent(id)}`,
                                            { onError: 'null' }),
    versions:    (id)               => get(`/api/v1/artifacts/${encodeURIComponent(id)}/versions`,
                                            { onError: 'null' }),
    pin:         (id, pinned)       => post(`/api/v1/artifacts/${encodeURIComponent(id)}/pin`,
                                             { pinned: !!pinned }, { onError: 'null' }),
    remove:      (id)               => del(`/api/v1/artifacts/${encodeURIComponent(id)}`,
                                            { parse: 'response', onError: 'null' }),
    library:     (limit)            => get('/api/v1/artifacts',
                                            { query: { limit: limit || 120 }, onError: 'null' }),
    forConv:     (convId)           => get('/api/v1/artifacts',
                                            { query: { conv: convId }, onError: 'null' }),
    scan:        (convId)           => post('/api/v1/artifacts/scan',
                                             { conv_id: convId }, { onError: 'null' }),
    // Fetch raw bytes as text (markdown / html / svg). Returns a string
    // or throws — callers that need different shapes use the URL builders.
    contentText: async (id) => {
      const resp = await request(`/api/artifacts/${encodeURIComponent(id)}/raw`,
                                  { method: 'GET', parse: 'response' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return await resp.text();
    },
    // URL builders for iframe.src / anchor.href — these intentionally
    // resolve to the legacy carve-out paths (binary / sandboxed HTML
    // with custom Content-Disposition + CSP headers; not v1 envelope).
    rawUrl:       (id) => _resolve(`/api/artifacts/${encodeURIComponent(id)}/raw`),
    viewUrl:      (id) => _resolve(`/api/artifacts/${encodeURIComponent(id)}/view`),
    exportPdfUrl: (id) => _resolve(`/api/artifacts/${encodeURIComponent(id)}/export?format=pdf`),
  };

  // ────────────────────────────────────────────────────────────────
  //  Public namespace
  // ────────────────────────────────────────────────────────────────
  const Api = {
    // low-level
    request, get, post, put, patch, del, stream,
    ApiError,
    _resolve,         // exposed for SSE/WS path building
    // domains
    folders, orchestrations, memory, profile, timer, scheduler, optimizer, compactions,
    conversations, text, translate, chat, images, pdf, doc, audio, artifacts,
    health, pricing, clientError, serverConfig, browser, project, daily, paper,
    features, providers, dispatch, oauth, mcp, update, trading, authSources,
    swarm, endpoint, logs,
  };

  global.Api = Api;
  global.ApiError = ApiError;
})(typeof window !== 'undefined' ? window : this);

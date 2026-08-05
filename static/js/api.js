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
                                folders, paperFolders, conversations, chat, paper,
                                translate, daily, project, settings,
                                memory, skills, mcp, oauth, optimizer, image,
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
      /* ── Diagnostic surface (declared, not bolted on) ──────────────────
       * These were previously assigned ad-hoc at the throw sites, which made
       * them invisible to `tsc --checkJs` AND to anyone reading the class:
       * the error's contract lived scattered across the request path. They
       * are part of what an ApiError IS, so they are declared here.
       *
       *   requestId       the id to quote when reporting this failure — the
       *                   server-resolved one when we know it, else ours
       *   clientRequestId the id THIS client put on the wire
       *   serverRequestId the id the server echoed back; a mismatch with
       *                   clientRequestId proves a proxy rewrote the header
       *   envelope        the backend's typed error envelope when it sent one
       *                   (see lib/error_envelope/ + core/error_envelope.js)
       */
      this.requestId = (opts && opts.requestId) || null;
      this.clientRequestId = (opts && opts.clientRequestId) || null;
      this.serverRequestId = (opts && opts.serverRequestId) || null;
      this.envelope = (opts && opts.envelope) || null;
    }
  }

  /* ── Request correlation id (frontend → backend log join key) ───────
   *
   * The backend ALREADY prefers an inbound `X-Request-ID`
   * (server.py::_assign_req_id_and_log) and stamps it on every log line for
   * the request via a contextvar, then echoes it back on the response
   * (`X-Request-ID`). Before this, the frontend never sent one, so every rid
   * was server-minted and the client held no copy — a user-reported bug had
   * NO join key back to the server logs (only timestamp + URL guessing).
   *
   * Shape: `<page>-<seq>` where `page` is a short id minted once per document
   * load. The page prefix is what makes a whole page-load's requests
   * groupable with one `grep` in app.log; the monotonic seq keeps every
   * request individually addressable and makes a gap (a request that never
   * reached the server) visible.
   *
   * Kept to [a-z0-9-] and short: it lands in a header, in log lines, and in
   * an error surface a user may read aloud or screenshot.
   */
  const _PAGE_ID = (function () {
    try {
      const c = global.crypto;
      if (c && typeof c.randomUUID === 'function') return c.randomUUID().slice(0, 6);
      if (c && typeof c.getRandomValues === 'function') {
        const b = new Uint8Array(3);
        c.getRandomValues(b);
        return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
      }
    } catch (e) { /* crypto unavailable (old WebView / insecure origin) */ }
    return Math.random().toString(36).slice(2, 8);
  })();

  let _ridSeq = 0;

  /** Mint the next correlation id for an outbound request. */
  function _nextRequestId() {
    _ridSeq += 1;
    return _PAGE_ID + '-' + _ridSeq;
  }

  /** The page id every request from this document load shares. */
  function pageRequestId() { return _PAGE_ID; }

  // ── Core request ──────────────────────────────────────────────────
  // opts:
  //   method        'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  //   query         {k:v} → ?k=v (encoded)
  //   json          object — JSON body, sets Content-Type
  //   body          raw body (string|FormData|Blob); used if json absent
  //   headers       extra headers
  //   timeout       ms (default 0 = none; a probe passes its own budget)
  //   parse         'json' (default) | 'text' | 'blob' | 'response' | 'none'
  //   signal        AbortSignal
  //   keepalive     true — the request may outlive the page (unload flushes)
  //   onError       'throw' (default) | 'null' — null returns null on failure
  async function request(path, opts) {
    opts = opts || {};
    const method = (opts.method || 'GET').toUpperCase();
    const url = _resolve(path) + _qs(opts.query);
    const headers = Object.assign({}, opts.headers || {});
    /* ★ Correlation id — injected HERE, at the ONE chokepoint every frontend
     * backend call passes through (get/post/put/patch/del/stream all delegate
     * to request()), so coverage is total without touching a single call site.
     * That total coverage from one edit is the concrete payoff of the api.js
     * isolation rule enforced by tests/test_frontend_api_isolation.py.
     * A caller may override it explicitly (retry that wants to reuse the
     * original id); we never clobber a provided value. */
    const _rid = headers['X-Request-ID'] || _nextRequestId();
    headers['X-Request-ID'] = _rid;
    let body = opts.body;
    if (opts.json !== undefined) {
      headers['Content-Type'] = headers['Content-Type'] || 'application/json';
      body = JSON.stringify(opts.json);
    }
    const init = { method, headers };
    if (body !== undefined) init.body = body;
    if (opts.keepalive) init.keepalive = true;

    // Timeout via AbortController unless caller provided a signal
    const userSignal = opts.signal;
    let timeoutId = null;
    let ctrl = null;
    /* ★ NO DEFAULT TIMEOUT (opts.timeout omitted => 0 => none).
     *
     * A blanket client-side ceiling is the browser-side twin of the read
     * timeouts removed from the transport (lib/llm/_transport.py): it turns
     * "slow" into "failed" for work the server is still doing, and reports it
     * as code:'timeout' so the user is told the request died when it did not.
     * The default being a ceiling was also demonstrably backwards — 22 call
     * sites had to opt OUT with `timeout: 0` to be allowed to wait.
     *
     * The rule, same as the backend: a LIVENESS PROBE may bound itself, a
     * WAIT may not. Probes therefore pass their own budget explicitly
     * (health.check 3s/5s, backend_offline_monitor, cross_tab_sync 8s/15s
     * — all via AbortSignal.timeout or an explicit `timeout:`), and are
     * unaffected by this default. Anything that omits `timeout` is by
     * definition not a probe, and waits until it answers or the user aborts.
     *
     * Pinned by tests/test_frontend_no_client_timeouts.py. */
    const timeout = opts.timeout || 0;
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
        console.warn('[Api] %s %s failed: %s [rid=%s]', method, url, e && e.message, _rid);
        return null;
      }
      // Rethrow abort AND timeout DOMExceptions verbatim so callers that
      // branch on e.name ('AbortError' / 'TimeoutError' — e.g. the chat-start
      // 30s AbortSignal.timeout in main_send_pipeline.js) keep working exactly
      // as they did with a raw fetch.
      if (e && (e.name === 'AbortError' || e.name === 'TimeoutError')) throw e;
      /* No response arrived, so there is no server-echoed id — but the id we
       * SENT is still the join key: if the request reached the app at all,
       * server.py logged this exact rid, which distinguishes "never left the
       * client" from "server received it and then the connection broke". */
      const _netErr = new ApiError(e && e.message || 'network error', { url, code: 'network' });
      _netErr.clientRequestId = _rid;
      _netErr.requestId = _rid;
      throw _netErr;
    }
    if (timeoutId) clearTimeout(timeoutId);

    const parse = opts.parse || 'json';
    if (parse === 'response') return resp;

    if (!resp.ok) {
      let bodyData = null;
      try {
        const ct = resp.headers.get('content-type') || '';
        bodyData = ct.includes('application/json') ? await resp.json() : await resp.text();
      } catch (e) {
        console.warn('[Api] failed to parse error body for %s %s (HTTP %s): %s',
                     method, url, resp.status, e && e.message);
      }
      const code = (bodyData && typeof bodyData === 'object' && bodyData.error) || null;
      /* Duck-typed envelope detection (api.js must stay load-order
       * independent of core/error_envelope.js). When the backend shipped a
       * typed envelope, err.message must BE the backend's real message —
       * every caller that toasts e.message would otherwise show only the
       * opaque 'HTTP 500 on …' status line. The full envelope rides along
       * for callers that render it properly. */
      const isEnvelope = !!code && typeof code === 'object'
        && typeof code.kind === 'string' && typeof code.message === 'string';
      const err = new ApiError(
        isEnvelope && code.message
          ? code.message
          : 'HTTP ' + resp.status + ' on ' + method + ' ' + url,
        { status: resp.status, code, body: bodyData, url }
      );
      if (isEnvelope) err.envelope = code;
      /* ★ Correlation: the id WE sent, plus the id the server actually used
       * (echoed at server.py's after_request). They are normally identical
       * — the backend prefers our inbound header — so a MISMATCH is itself a
       * finding: something between us and the app (proxy / tunnel / CDN)
       * rewrote or stripped the header. Keep both rather than assuming. */
      err.clientRequestId = _rid;
      const _srvRid = resp.headers.get('X-Request-ID');
      if (_srvRid) err.serverRequestId = _srvRid;
      if (bodyData && typeof bodyData === 'object'
          && typeof bodyData.request_id === 'string' && bodyData.request_id) {
        err.requestId = bodyData.request_id;
      } else if (_srvRid) {
        /* Backends that don't echo request_id in the JSON body still gave us
         * the header — populate the same field so every consumer of
         * `err.requestId` (diagnostics, error surfaces) works uniformly. */
        err.requestId = _srvRid;
      } else {
        err.requestId = _rid;
      }
      if (opts.onError === 'null') {
        /* Log the rid on the swallow path too: `onError:'null'` is the
         * QUIETEST failure mode and therefore the one most in need of a
         * server-side join key. */
        console.warn('[Api] %s [rid=%s]', err.message, err.requestId);
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
    // Coordinated bare-array migration (batch 19): backend wraps {ok,
    // items}; unwrap with fallback (list-UI || [] semantics).
    list:   async ()           => {
      const d = await get('/api/v1/folders', { onError: 'null' });
      if (d && Array.isArray(d.items)) return d.items;
      return Array.isArray(d) ? d : [];
    },
    create: (name, color)      => post('/api/v1/folders', { name, color: color || '' }, { onError: 'null' }),
    update: (id, updates)      => put(`/api/v1/folders/${encodeURIComponent(id)}`, updates, { onError: 'null' }),
    remove: async (id)         => {
      const r = await del(`/api/v1/folders/${encodeURIComponent(id)}`, { parse: 'response', onError: 'null' });
      return !!(r && r.ok);
    },
  };

  // users -----------------------------------------------------------
  //  `me` is PUBLIC (routes/api_v1/users.py:266) and is the boot-time
  //  identity probe for core/current_user.js. Three response shapes:
  //    multi-tenant login → {authenticated:true,  user:{id, email, …}}
  //    personal install   → {authenticated:true,  user:null, principal:{…}}
  //    unauthenticated    → {authenticated:false, user:null}
  //  onError:'null' so a boot hiccup degrades to "no identity" (gates stay
  //  accept-all) instead of rejecting the whole boot chain.
  const users = {
    me: () => get('/api/v1/users/me', { onError: 'null' }),
  };

  // paper-folders (Reading-mode library folders — same shape as `folders`) ---
  const paperFolders = {
    // Same bare-array coordination as folders.list above.
    list:   async ()           => {
      const d = await get('/api/v1/paper-folders', { onError: 'null' });
      if (d && Array.isArray(d.items)) return d.items;
      return Array.isArray(d) ? d : [];
    },
    create: (name, color)      => post('/api/v1/paper-folders', { name, color: color || '' }, { onError: 'null' }),
    update: (id, updates)      => put(`/api/v1/paper-folders/${encodeURIComponent(id)}`, updates, { onError: 'null' }),
    remove: async (id)         => {
      const r = await del(`/api/v1/paper-folders/${encodeURIComponent(id)}`, { parse: 'response', onError: 'null' });
      return !!(r && r.ok);
    },
  };

  // orchestrations --------------------------------------------------
  const orchestrations = {
    // Coordinated bare-array migration (docs/API_CONTRACT.md §4): the
    // backend now wraps the array as {ok, items}; unwrap here with an
    // Array.isArray fallback so a pre-migration server still works.
    list:     async ()        => {
      const d = await get('/api/v1/orchestrations', { onError: 'null' });
      if (d && Array.isArray(d.items)) return d.items;
      return Array.isArray(d) ? d : [];
    },
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
  };

  // skills (user-installed skill packages — a different noun from memory)
  const skills = {
    list:           (scope)     => get('/api/v1/skills', { query: { scope: scope || 'all' } }),
    uninstall:      (id)        => del(`/api/v1/skills/${encodeURIComponent(id)}`, { parse: 'response' }),
    toggle:         (id)        => post(`/api/v1/skills/${encodeURIComponent(id)}/toggle`, undefined, { parse: 'response' }),
    files:          (id)        => get(`/api/v1/skills/${encodeURIComponent(id)}/files`),
    install:        (formData)  => request('/api/v1/skills/install', { method: 'POST', body: formData, parse: 'response' }),
    catalog:        ()          => get('/api/v1/skills/catalog'),
    catalogInstall: (skillId, scope) => post('/api/v1/skills/catalog/install', { skill_id: skillId, scope: scope || 'global' }, { parse: 'response' }),
    // Per-skill env/key bindings (credential-vault backed; redacted status only)
    envStatus:      (id)        => get(`/api/v1/skills/${encodeURIComponent(id)}/env`),
    envSet:         (id, name, value) => put(`/api/v1/skills/${encodeURIComponent(id)}/env`, { name, value }, { parse: 'response' }),
    envDelete:      (id, name)  => del(`/api/v1/skills/${encodeURIComponent(id)}/env/${encodeURIComponent(name)}`, { parse: 'response' }),
    setScope:       (id, scope) => post(`/api/v1/skills/${encodeURIComponent(id)}/scope`, { scope }, { parse: 'response' }),
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

  // tasks (Request Inspector) ----------------------------------------
  // Server-authoritative per-task request fold (docs/DEBUG_PANEL_REDESIGN.md
  // P2). byConv: task rows for a conversation; getRequests: metadata-only
  // request rows + attempts; getRequestPayload: full payload for one round
  // (kind='state' serves the post-tool / final / fallback mirrors).
  const tasks = {
    byConv: (convId) =>
      get(`/api/v1/tasks/by-conv/${encodeURIComponent(convId)}`,
          { onError: 'null' }),
    getRequests: (taskId) =>
      get(`/api/v1/tasks/${encodeURIComponent(taskId)}/requests`,
          { onError: 'null' }),
    getRequestPayload: (taskId, roundNum, turn, kind) =>
      get(`/api/v1/tasks/${encodeURIComponent(taskId)}/requests/${encodeURIComponent(roundNum)}`,
          { query: { ...(turn ? { turn } : {}), ...(kind ? { kind } : {}) },
            onError: 'null' }),

    // ── Generic production-job lifecycle ──
    // start/get/events/abort are CAPABILITY-AGNOSTIC: the backend dispatches
    // on `kind` (research | longform-report | …), so the next capability needs
    // ZERO new client methods. Deliberately NOT routed through an LLM tool —
    // the produce_* tools are search-gated and only fire if the model elects
    // to call them, which cannot back a UI control.
    start: (kind, payload) =>
      post('/api/v1/tasks/start', Object.assign({ kind: kind }, payload || {})),
    // Full snapshot. Carries `artifact_quality`: a degraded job keeps
    // status='done' by design, so that field is the ONLY thing separating
    // "good artifact" from "valid artifact out of a broken pipeline".
    get: (taskId) =>
      get(`/api/v1/tasks/${encodeURIComponent(taskId)}`, { onError: 'null' }),
    events: (taskId, cursor) =>
      get(`/api/v1/tasks/${encodeURIComponent(taskId)}/events`,
          { query: { cursor: cursor || 0 }, onError: 'null' }),
    abort: (taskId) =>
      post(`/api/v1/tasks/${encodeURIComponent(taskId)}/abort`, undefined,
           { onError: 'null' }),
    list: (kind, status) =>
      get('/api/v1/tasks', { query: Object.assign({}, kind ? { kind: kind } : {},
                                                  status ? { status: status } : {}),
                             onError: 'null' }),
  };

  // research (auto-research: direction → scored ideas) -------------
  // DURABLE read path. `Api.tasks.get(taskId)` resolves against the in-memory
  // task registry, so it 404s once the finished job is TTL-swept (7200s) or
  // the server restarts. This is addressed by DIRECTION and served from
  // paper_reports, so it keeps working forever — it is what makes a finished
  // research job re-openable at all. `found:false` is a normal answer.
  const research = {
    lookup: (direction, lang) =>
      get('/api/v1/research/lookup',
          { query: { direction: direction || '', lang: lang || 'en' },
            onError: 'null' }),
    // The direction INDEX. The persisted rows are keyed by a one-way hash of
    // the direction, so without this a user who forgot their exact original
    // wording could never address their own artifacts again.
    list: (limit) =>
      get('/api/v1/research/list',
          { query: { limit: limit || 50 }, onError: 'null' }),
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
  /* ── Per-conv in-flight merge (pt_afbaf3d7 ③, hand-off from
   *   pt_ef42c2a1e9f946f3). The 2026-08-01 hard-refresh congestion collapse
   *   served the SAME 176.8 MB conversation 6× in 25s because the boot load,
   *   the notify verify, the push-reconnect catch-up and the Case-F recovery
   *   each issued their OWN full GET — runWithConcurrency caps parallelism
   *   but does not dedupe. Identical in-flight GETs now share ONE Promise;
   *   the acceptance invariant is: concurrent same-shape GETs for the same
   *   conv ≤ 1 on the wire.
   *
   *   Scoped to SIGNAL-LESS callers: a caller that passes an AbortSignal owns
   *   an explicit probe budget (recover worker 10s, translation verify 15s),
   *   and sharing the underlying fetch would let one caller's abort cancel
   *   everyone else's read. Those stay one-request-per-call — the storm
   *   sources above are all signal-less.
   *
   *   Keyed by conv + the windowing shape (?window / ?before_seq), so a
   *   full read, a tail-window read and a scroll-up page never collide.
   *   getResponse (raw Response) is NOT deduped — a parsed-JSON Promise
   *   cannot be shared with a caller that needs the raw stream. */
  const _convGetInflight = new Map(); // key → Promise
  function _convGetDeduped(convId, opts) {
    if (opts && opts.signal) {
      return get(`/api/v1/conversations/${encodeURIComponent(convId)}`,
                 Object.assign({ onError: 'null' }, opts));
    }
    const q = (opts && opts.query) || {};
    const key = convId + '|' + (q.window || '') + '|' + (q.before_seq || '');
    const hit = _convGetInflight.get(key);
    if (hit) return hit;
    const p = get(`/api/v1/conversations/${encodeURIComponent(convId)}`,
                  Object.assign({ onError: 'null' }, opts || {}));
    _convGetInflight.set(key, p);
    const clear = () => {
      if (_convGetInflight.get(key) === p) _convGetInflight.delete(key);
    };
    p.then(clear, clear);
    return p;
  }

  // patchSettings: targeted settings PATCH — fire-and-forget; returns
  //   Response for callers who care, but typically chained as a fire-
  //   and-forget promise.
  // put: full conversation PUT — returns Response so the caller can
  //   read status (200 / 409 stale-checkpoint / etc.) and parse the body.
  // getDebugMessages: server-side rendered message list with system prompt.
  const conversations = {
    get: (convId, opts) => _convGetDeduped(convId, opts),
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
    // pt_conv_state_ssot P5: report the client's per-conv state digest
    // (authoritative busy set + last-converged rev) for the server-side
    // drift comparison. Returns parsed {ok, checked, divergences} or null.
    // `extra` carries optional top-level telemetry alongside the digests
    // (currently {identityGateDegraded:true} — the multi-user identity
    // gate's fail-open tripwire, so the degrade reaches logs/app.log
    // instead of only a browser console).
    // ``pushRid`` names THIS tab's push socket so a server-detected SUSTAINED
    // stall can be repaired by pushing a corrective conv_state_snapshot back to
    // this exact socket rather than broadcasting to the fleet
    // (pt_cadaa70ffa6b468d). Omitted when the id is unavailable — the server
    // then logs that it cannot target a repair instead of guessing at one.
    reportSyncDigest: (digests, extra) => {
      const _rid = (typeof pushSocketRequestId === 'function')
        ? pushSocketRequestId() : '';
      return post('/api/v1/conversations/sync-digest',
                  Object.assign({ digests }, _rid ? { pushRid: _rid } : {},
                                extra || {}), { onError: 'null' });
    },
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
    // Coordinated bare-array migration (batch 20): backend wraps {ok,
    // items}; unwrap with fallback (list-UI || [] semantics).
    search: async (query, opts) => {
      const d = await get('/api/v1/conversations/search',
          Object.assign({ query: { q: query } }, opts || {}));
      if (d && Array.isArray(d.items)) return d.items;
      return Array.isArray(d) ? d : [];
    },
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
    // Coordinated bare-array migration (batch 14): backend wraps {ok,
    // items}; unwrap null-preservingly — translation.js's
    // !Array.isArray(data) branch synthesizes per-id error rows and must
    // keep firing on a failed (null) probe.
    pollBatch: async (taskIds)  => {
      const d = await post('/api/v1/translate/poll-batch', { taskIds }, { onError: 'null' });
      if (d == null) return null;
      if (Array.isArray(d.items)) return d.items;
      return Array.isArray(d) ? d : null;
    },
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
    // Per-command interrupt (pt_232244fb): kill ONLY the task's running
    // run_command subprocess — the turn CONTINUES with the partial output fed
    // back to the model. NOT fire-and-forget: the caller needs the verdict
    // ({interrupted:true} or {interrupted:false, reason}) to paint the button.
    interruptCommand: (taskId)  => post(`/api/v1/chat/interrupt-command/${encodeURIComponent(taskId)}`, undefined, { onError: 'null' }),
    // Coordinated bare-array migration (batch 21): backend wraps {ok,
    // items}; unwrap with fallback (list-UI || [] semantics).
    queueGet:     async (convId) => {
      const d = await get(`/api/v1/chat/queue/${encodeURIComponent(convId)}`);
      if (d && Array.isArray(d.items)) return d.items;
      return Array.isArray(d) ? d : [];
    },
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
    // Coordinated bare-array migration (batch 11): backend wraps {ok,
    // items}; unwrap with fallback — PRESERVING null: callers distinguish
    // "server says zero tasks" ([]) from "probe failed" (null), and
    // null→[] would fake the former into static-adopt decisions.
    active:       async (opts)  => {
      const d = await get('/api/v1/chat/active', Object.assign({ onError: 'null' }, opts || {}));
      if (d == null) return null;
      if (Array.isArray(d.items)) return d.items;
      return Array.isArray(d) ? d : null;
    },
    activeResponse: (opts)      => request('/api/v1/chat/active', Object.assign({ method: 'GET', parse: 'response', onError: 'null' }, opts || {})),
    // Per-conversation running-task PROJECTION — the poll transport's copy of
    // the `conv_state_snapshot` push frame, same wire shape (`{convs:{id:
    // {runningTaskIds, runningTaskIdsRev}}}`) so ONE client reducer
    // (applyConvStateSnapshot) consumes both transports.
    //
    // NOT interchangeable with active() above: that answers "what may I
    // reconnect an SSE to?" and EXCLUDES autopilot VU carriers (a carrier on
    // the plain connectToTask path births a permanently-stuck "Waiting…"
    // bubble). This answers "which convs are WORKING?" and INCLUDES carriers
    // behind a non-attachable `#vu` marker — which is the only way the 25s
    // poll fallback can see a live VU turn while the push socket is down.
    convState:    (opts)        => get('/api/v1/chat/conv-state', Object.assign({ onError: 'null' }, opts || {})),
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
    // Browser-console relay batch (core/client_log_relay.js). keepalive lets
    // the pagehide flush outlive the page; onError:'null' matches the relay's
    // never-amplify doctrine (a down server drops the batch silently).
    clientRelay: (payload) => request('/api/v1/logs/client',
      { method: 'POST', body: payload, keepalive: true,
        headers: { 'Content-Type': 'application/json' },
        parse: 'none', onError: 'null' }),
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
    // Takes {approvalId} once a human approved the pending request.
    shutdown: (payload) => post('/api/v1/update/shutdown', payload || {}, { onError: 'throw' }),
    // Lifecycle approvals (pt_40d00fd526e5479a human-approval gate): a
    // restart/shutdown POST without an approvalId answers 202 +
    // {pendingApproval}; the human decides here; the caller retries with
    // {approvalId}. onError:'throw' so callers can read 403/404 bodies.
    listLifecycleApprovals: (query) =>
      get('/api/v1/update/lifecycle-approvals', { query: query || {}, onError: 'null' }),
    getLifecycleApproval: (id) =>
      get(`/api/v1/update/lifecycle-approvals/${encodeURIComponent(id)}`, { onError: 'null' }),
    decideLifecycleApproval: (id, approved) =>
      post(`/api/v1/update/lifecycle-approvals/${encodeURIComponent(id)}/decide`,
           { approved: !!approved }, { onError: 'throw' }),
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

  // desktop bridge (RWA Devices page) ----------------------------------
  const desktop = {
    /* `arch` is the architecture the CLIENT resolved for itself via
     * navigator.userAgentData.getHighEntropyValues(['architecture']). It is
     * threaded to the server because macOS cannot be narrowed any other way:
     * an Apple Silicon Mac reports "Intel Mac OS X" in its UA, so without this
     * the server must offer BOTH DMGs rather than guess wrong. Omit it and the
     * ambiguous (correct, two-choice) answer comes back. */
    status:      (arch) => get('/api/v1/desktop/status',
                              { onError: 'null', query: arch ? { arch: arch } : {} }),
    devices:     () => get('/api/v1/desktop/devices', { onError: 'null' }),
    mintToken:   (name) => post('/api/v1/desktop/token', { name: name || '' }),
    revokeToken: (keyId) => del(`/api/v1/desktop/token/${encodeURIComponent(keyId)}`),
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
    // Coordinated bare-array migration (batch 10): the backend now wraps
    // as {ok, items}; unwrap with an Array.isArray fallback for skew.
    templates:        async ()                          => {
      const d = await get('/api/v1/providers/templates', { onError: 'null' });
      if (d && Array.isArray(d.items)) return d.items;
      return Array.isArray(d) ? d : [];
    },
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
    /* Which wire face does each model of THIS (possibly unsaved) provider
     * dispatch over? Answered by the backend's resolve_face — the same
     * resolver the dispatcher uses — so the Settings pills can never
     * disagree with routing. Never re-implement the family rule here. */
    resolveFaces:     (provider)                        =>
      post('/api/v1/providers/resolve-faces', { provider },
           { onError: 'null' }),
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
    modelHealth:     () => get('/api/v1/dispatch/model-health', { onError: 'null' }),
    keyOverride:     (body) =>
      post('/api/v1/dispatch/key-override', body, { onError: 'null', parse: 'none' }),
  };

  // oauth (Claude / Codex login flows) ------------------------------
  // login/logout/callback have GET (querystring) and POST (json body)
  // variants — different deployments expose one or the other; we always
  // try POST first and let the caller fall back.
  const oauth = {
    status:      ()                 => get('/api/v1/oauth/status', { onError: 'null' }),
    loginPost:   (provider, preferConsole) =>
      post('/api/oauth/login',
           preferConsole ? { provider, prefer_console: true } : { provider },
           { onError: 'null', parse: 'response' }),
    loginGet:    (provider, preferConsole) =>
      request('/api/oauth/login',
              { method: 'GET',
                query: preferConsole ? { provider, prefer_console: '1' } : { provider },
                parse: 'response', onError: 'null' }),
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
    egressAgentGet: ()              => get('/api/v1/oauth/egress-agent', { onError: 'null' }),
    egressAgentSet: (agentId)       => post('/api/v1/oauth/egress-agent', { agent_id: agentId }, { onError: 'null', parse: 'response' }),
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
    // Per-server tool list (with per-tool `enabled` flags) — backs the
    // Settings → MCP card's per-tool toggle list.
    toolsListForServer: (server)                   =>
      request('/api/v1/mcp/tools',
              { method: 'GET', query: { server }, parse: 'response', onError: 'null' }),
    // Replace a server's disabled_tools list (full-replacement semantics).
    serverToolsSet: (server, disabledTools)        =>
      request(`/api/v1/mcp/servers/${encodeURIComponent(server)}/tools`,
              { method: 'PUT', json: { disabled_tools: disabledTools || [] },
                parse: 'response', onError: 'null' }),
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
    // Guided install step (loopback-gated server-side): opens the SERVER
    // machine's Chrome at chrome://extensions. null on refusal/failure.
    openExtensions: () => post('/api/v1/browser/open-extensions', {},
                               { onError: 'null' }),
  };

  // authSources (login-walled fetch sources: Xiaohongshu, …) ---------
  const authSources = {
    list:   ()              => get('/api/v1/auth-sources', { onError: 'null' }),
    upsert: (body)          => post('/api/v1/auth-sources', body),
    toggle: (domain, on)    => post(`/api/v1/auth-sources/${encodeURIComponent(domain)}/toggle`, { enabled: !!on }, { onError: 'null' }),
    remove: (domain)        => del(`/api/v1/auth-sources/${encodeURIComponent(domain)}`, { parse: 'response', onError: 'null' }),
    // Interactive headful login — long-running; no client timeout.
    login:  (domain, timeout) => post(`/api/v1/auth-sources/${encodeURIComponent(domain)}/login`, { timeout: timeout || 180 }, { timeout: 0 }),
    // Live-session probe: is the user logged into the site in THEIR browser
    // right now (bridge get_cookies; jar never leaves the browser)?
    liveSession: (domain, refresh) => get(`/api/v1/auth-sources/${encodeURIComponent(domain)}/live-session${refresh ? '?refresh=1' : ''}`, { onError: 'null' }),
    // Login-wall cookie-capture consent (cookie_capture_consent.js banner).
    cookieConsentPending: () => get('/api/v1/auth-sources/cookie-consent/pending', { onError: 'null' }),
    cookieConsentResolve: (id, approved) => post('/api/v1/auth-sources/cookie-consent/resolve', { id, approved: !!approved }),
    cookieConsentGrants:  () => get('/api/v1/auth-sources/cookie-consent/grants', { onError: 'null' }),
    cookieConsentRevoke:  (domain) => del(`/api/v1/auth-sources/cookie-consent/${encodeURIComponent(domain)}`, { parse: 'response', onError: 'null' }),
  };

  // privateHosts (internal-host SSRF allowlist) ---------------------
  // REACHABILITY only. An entry here exempts a host from the SSRF guard and
  // grants NO credentials; authSources above grants credentials and NO SSRF
  // exemption. Two separate gates on purpose — do not merge them.
  const privateHosts = {
    list:   ()           => get('/api/v1/private-hosts', { onError: 'null' }),
    upsert: (body)       => post('/api/v1/private-hosts', body),
    toggle: (host, on)   => post(`/api/v1/private-hosts/${encodeURIComponent(host)}/toggle`, { enabled: !!on }, { onError: 'null' }),
    remove: (host)       => del(`/api/v1/private-hosts/${encodeURIComponent(host)}`, { parse: 'response', onError: 'null' }),
  };

  // credentials (encrypted credential vault) ----------------------------
  // Values NEVER appear in list() — only {name, hint, note, timestamps}.
  // reveal() is the ONLY plaintext egress, fired explicitly by the user.
  const credentials = {
    list:   ()       => get('/api/v1/credentials', { onError: 'null' }),
    upsert: (body)   => post('/api/v1/credentials', body),
    reveal: (name)   => post(`/api/v1/credentials/${encodeURIComponent(name)}/reveal`, {}),
    remove: (name)   => del(`/api/v1/credentials/${encodeURIComponent(name)}`, { parse: 'response', onError: 'null' }),
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
        try { err = await resp.json(); } catch (e) {
          console.warn('[Api][trading] non-JSON error body for %s (HTTP %s): %s',
                       _TRADING_BASE + path, resp.status, e && e.message);
        }
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
    // Re-apply a previously-undone round. Backend requires taskId; the caller
    // MUST also pin the conversation's own projectPath (undo deleted the
    // round's record, so redo resolves the project from the pin, never the
    // globally-active UI project). Mirrors undo's concurrency contract.
    redo:          (body)     =>
      request('/api/v1/project/redo',
              { method: 'POST', json: body, parse: 'response' }),
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
    // NOTE on updateDecision: `summary` is the ONE line the per-turn injection
    // renders (the body is read back on demand). Pass it in `body` whenever the
    // entry has one — OMITTING it is refused with `summary_required`, because a
    // silent keep would leave every sibling conversation reading the OLD rule
    // while the panel showed the edit as saved. Send `summary: ''` to drop it
    // deliberately and let the headline fall back to the fresh text.
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
    boardDelete:   (path, taskId, convId) =>
      post('/api/v1/project/board/delete', { path, taskId, convId: convId || '' }),
    // HUMAN answer to a pending block question — closes the structured gate
    // (stamps human_answer, clears the cooldown, immediate re-dispatch whose
    // kickoff carries the answer).
    boardAnswer:   (path, taskId, convId, answer) =>
      post('/api/v1/project/board/answer',
           { path, taskId, convId: convId || '', answer: answer || '' }),
    // Collaboration-bar one-shot summary (board + decisions + peer→epic join).
    // convId (optional) is excluded from activePeers/peerEpics so the count is
    // "OTHER conversations online" — matching the local push-mirror semantics.
    brainSummary:  (path, convId) =>
      get('/api/v1/project/brain/summary',
          { query: { path, convId: convId || '' }, onError: 'null' }),
    // The "needs you" SSOT — everything genuinely waiting on the human,
    // priority-ordered server-side (blocking first). One payload backs both
    // the Needs-you tab and the collab bar's count, so they cannot drift.
    // convId is optional and only marks `mine`; it never filters.
    brainAttention: (path, convId) =>
      get('/api/v1/project/brain/attention',
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
    // Follow-up Q&A anchored to ONE trail response (human↔brain lane; the
    // answer appends to the same trail with trigger='follow_up').
    brainWatchFollowUp: (itemId, question, seq) =>
      post('/api/v1/project/brain/watch/follow_up',
           { itemId, question, seq: seq || 0 }),
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
    /* searchArxiv MUST throw on failure (default onError:'throw'), not
     * swallow to null: the caller renders null/!ok as "no papers found",
     * which made every server/upstream outage look like an empty result set
     * (2026-07-28 live 500 incident). The caller has a real error surface. */
    searchArxiv:    (query, maxResults)   =>
      post('/api/v1/paper/search-arxiv', { query, max_results: maxResults || 10 }),
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
    // Deepen (on-demand section depth, reading-xp P3). Start has no client
    // deadline (same reasoning as reportStart); poll rides the GENERIC
    // task-routes factory shape ({ok, events, next_cursor, status}).
    deepenStart:    (body, opts)          => post('/api/v1/paper/deepen/start', body, Object.assign({ timeout: 0 }, opts || {})),
    deepenPoll:     (taskId, cursor)      =>
      request(`/api/v1/paper/deepen/poll/${encodeURIComponent(taskId)}`,
              { method: 'GET', query: { cursor }, parse: 'response', onError: 'null' }),
    deepenAbort:    (taskId)              => post(`/api/v1/paper/deepen/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null', parse: 'none' }),
    // Reader margin notes (reading-xp P4)
    notesList:      (paperHash, lang)     =>
      request('/api/v1/paper/notes', { method: 'GET', query: { paper_hash: paperHash, lang }, onError: 'null' }),
    notesCreate:    (body)                => post('/api/v1/paper/notes', body),
    notesUpdate:    (noteId, note)        =>
      request(`/api/v1/paper/notes/${encodeURIComponent(noteId)}`, { method: 'PATCH', body: JSON.stringify({ note }), headers: { 'Content-Type': 'application/json' }, onError: 'null' }),
    notesDelete:    (noteId)              =>
      request(`/api/v1/paper/notes/${encodeURIComponent(noteId)}`, { method: 'DELETE', onError: 'null' }),
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
    // Paper podcast — server-owned task (report → spoken script → TTS audio),
    // polled exactly like the report task beside it. timeout:0 on start: the
    // route does report-gate + cache checks before spawning; the task itself
    // is then polled with no client-side deadline (same reason as reportStart).
    podcastStatus:  ()                   =>
      request('/api/v1/paper/podcast/status', { method: 'GET', onError: 'null' }),
    podcastStart:   (body)               => post('/api/v1/paper/podcast/start', body, { timeout: 0 }),
    podcastPoll:    (taskId, cursor)     =>
      request('/api/v1/paper/podcast/poll',
              { method: 'GET', query: { task_id: taskId, cursor }, parse: 'response', onError: 'null' }),
    podcastLookup:  (body)               => post('/api/v1/paper/podcast/lookup', body, { onError: 'null' }),
    podcastAbort:   (taskId)             => post(`/api/v1/paper/podcast/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null', parse: 'none' }),
    podcastScript:  (paperHash, mode, lang) =>
      request('/api/v1/paper/podcast/script',
              { method: 'GET', query: { paper_hash: paperHash, mode, lang }, onError: 'null' }),
    // Paper video abstract — server-owned motion-engine task (report →
    // narrated MG video). Progress/results ride the motion endpoints below.
    videoStart:   (body)               => post('/api/v1/paper/video/start', body, { timeout: 0 }),
    videoLookup:  (body)               =>
      request('/api/v1/paper/video/lookup',
              { method: 'GET', query: { paper_hash: body.paper_hash }, onError: 'null' }),
  };

  // motion (video generation pipeline) -------------------------------
  // Server-owned TaskRuntime (SRT/scenes → narrated MG video). The paper
  // video-abstract tab polls these directly; the chat agent drives the
  // motion_video_* tools instead.
  const motion = {
    status:    ()                     =>
      request('/api/v1/motion/status', { method: 'GET', onError: 'null' }),
    start:     (body)                 => post('/api/v1/motion/videos', body, { timeout: 0 }),
    poll:      (taskId, cursor)       =>
      request(`/api/v1/motion/videos/poll/${encodeURIComponent(taskId)}`,
              { method: 'GET', query: { cursor }, parse: 'response', onError: 'null' }),
    abort:     (taskId)               =>
      post(`/api/v1/motion/videos/abort/${encodeURIComponent(taskId)}`, {}, { onError: 'null', parse: 'none' }),
    scenes:    (taskId)               =>
      request(`/api/v1/motion/videos/${encodeURIComponent(taskId)}/scenes`,
              { method: 'GET', onError: 'null' }),
    regenScene: (taskId, sceneId)     =>
      post(`/api/v1/motion/videos/${encodeURIComponent(taskId)}/scenes/${encodeURIComponent(sceneId)}/regen`,
           {}, { onError: 'null' }),
    // URL builders for <video> src / download anchors (no fetch involved).
    fileUrl:   (taskId, part)         =>
      _resolve(`/api/v1/motion/videos/${encodeURIComponent(taskId)}/file` +
               (part ? '?part=' + encodeURIComponent(part) : '')),
    sceneFileUrl: (taskId, sceneId)   =>
      _resolve(`/api/v1/motion/videos/${encodeURIComponent(taskId)}/scenes/${encodeURIComponent(sceneId)}/file`),
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

  // videos (upload + upload-time analysis status) ----------------------
  // upload: multipart video → { ok, video_id, status:'processing', poll }.
  //   timeout:0 because a 512MB upload over a slow link is slow.
  // status: poll the processing record; when 'ready' it carries the full
  //   self-contained payload (durable frame URLs + transcript + metadata).
  const videos = {
    upload: (formData) => request('/api/v1/videos/upload', { method: 'POST', body: formData, timeout: 0 }),
    status: (videoId)  => get(`/api/v1/videos/${encodeURIComponent(videoId)}`, { onError: 'null' }),
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
    pageRequestId,    // the correlation prefix every request of this page shares
    // domains
    folders, paperFolders, orchestrations, memory, skills, profile, timer, scheduler, optimizer, compactions,
    conversations, text, translate, chat, images, pdf, doc, audio, videos, artifacts,
    health, pricing, clientError, serverConfig, browser, project, daily, paper,
    desktop,
    features, providers, dispatch, oauth, mcp, update, trading, authSources,
    privateHosts, credentials,
    swarm, endpoint, logs, motion, tasks, users, research,
  };

  global.Api = Api;
  global.ApiError = ApiError;
})(typeof window !== 'undefined' ? window : this);

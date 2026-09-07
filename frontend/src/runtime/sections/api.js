/* ===== migrated source: api.js ===== */
/* ═══════════════════════════════════════════════════════════════════════
   api.js — Retained Frontend Endpoint Registry
   ═══════════════════════════════════════════════════════════════════════

   THE SINGLE SOURCE OF TRUTH for retained endpoint names. HTTP mechanics live
   only in frontend/src/api/transport.ts and are statically required below.

   Why this exists
   ───────────────
   Before this module the frontend made 123+ raw `fetch('/api/...')` calls
   scattered across 30+ JS files, each rebuilding URL handling, JSON
   parsing, error handling, and timeout logic. Migrating any single
   endpoint to /api/v1 required touching every call site.

   Architecture rule (enforced by tests/test_frontend_api_isolation.py)
   ───────────────────────────────────────────────────────────────────
   No runtime section, including `api.js`, may call `fetch('/api/...')`
   directly. New code MUST go through `Api.<domain>.<method>(...)` or a
   generated typed client; both delegate to the typed transport.

   Mapping policy
   ──────────────
   The endpoint registry owns retained URLs. Today it may hit a legacy
   `/api/foo` endpoint;
   tomorrow it can switch to `/api/v1/foo` with no caller change. The
   v1 backend surface (routes/api_v1/) is the long-term target — every
   method here is a candidate to point at v1 when the v1 route exists.

   Layout
   ──────
     Api.request(...)        — typed-transport delegate
     Api.get/post/put/del    — typed-transport convenience delegates
     Api.stream(...)         — typed-transport Response delegate
     Api.<domain>.<method>   — domain-grouped public surface:
                                folders, paperFolders, conversations, chat, paper,
                                translate, daily, project, settings,
                                memory, skills, mcp, oauth, optimizer, image,
                                pdf, browser, scheduler, ...

   Error model
   ───────────
   Every call resolves to a result object or throws the typed ApiError. ApiError
   carries `.status`, `.code`, `.body`, `.url`. Callers that want a
   silent fallback can pass `{onError:'null'}` to the verb helpers, in
   which case rejections become `null` (and a console.warn is logged).
*/

(function (global) {
  'use strict';

  /* The endpoint registry is retained temporarily, but HTTP mechanics have one
   * required owner: frontend/src/api/transport.ts. Keeping a local request,
   * affinity, or ApiError implementation here previously made the main bundle
   * ship two subtly different transports. */
  const _transportOwner = requiredApiTransport;
  const ApiError = _transportOwner.ApiError;
  const _resolve = _transportOwner.resolvePath;
  const pageRequestId = _transportOwner.pageRequestId;
  const _bindTaskAffinity = _transportOwner.bindTaskAffinity;
  const newIdempotencyKey = _transportOwner.newIdempotencyKey;

  function request(path, opts) {
    return _transportOwner.request(path, opts || {});
  }

  function taskStartAffinityOptions(body, opts) {
    return _transportOwner.taskStartAffinityOptions(body, opts || {});
  }

  // ── Verb wrappers ────────────────────────────────────────────────
  function get(path, opts) { return request(path, Object.assign({ method: 'GET' }, opts || {})); }
  function post(path, json, opts) { return request(path, Object.assign({ method: 'POST', json }, opts || {})); }
  function put(path, json, opts) { return request(path, Object.assign({ method: 'PUT', json }, opts || {})); }
  function patch(path, json, opts) { return request(path, Object.assign({ method: 'PATCH', json }, opts || {})); }
  function del(path, opts) { return request(path, Object.assign({ method: 'DELETE' }, opts || {})); }

  // Retry only writes whose HTTP contract sets one target state.  These calls
  // are safe after an ambiguous ACK because applying the same patch/delete
  // again cannot create another business event.  Counted/action POSTs stay on
  // the one-shot request path; broad transport-level write retries would
  // duplicate them.
  const _IDEMPOTENT_WRITE_RETRY_STATUSES = new Set([429, 502, 503, 504]);
  const _IDEMPOTENT_WRITE_MAX_ATTEMPTS = 3;

  function _idempotentRetryAfterMs(resp, retryIndex) {
    let advised = null;
    try {
      const raw = resp && resp.headers && resp.headers.get('Retry-After');
      if (raw != null && raw !== '') {
        const seconds = Number(raw);
        if (Number.isFinite(seconds)) advised = Math.max(0, seconds * 1000);
        else {
          const at = Date.parse(raw);
          if (Number.isFinite(at)) advised = Math.max(0, at - Date.now());
        }
      }
    } catch (_) { /* malformed/mocked headers use bounded local backoff */ }
    const fallback = 150 * Math.pow(2, retryIndex) + Math.random() * 100;
    return Math.min(5000, advised == null ? fallback : advised);
  }

  function _waitForIdempotentRetry(delayMs, signal) {
    if (signal && signal.aborted) return Promise.resolve(false);
    return new Promise((resolve) => {
      let settled = false;
      const finish = (ready) => {
        if (settled) return;
        settled = true;
        if (signal && typeof signal.removeEventListener === 'function') {
          signal.removeEventListener('abort', onAbort);
        }
        resolve(ready);
      };
      const timer = setTimeout(() => finish(true), delayMs);
      const onAbort = () => {
        clearTimeout(timer);
        finish(false);
      };
      if (signal && typeof signal.addEventListener === 'function') {
        signal.addEventListener('abort', onAbort, { once: true });
      }
    });
  }

  async function _idempotentResponseWrite(path, opts) {
    const options = Object.assign(
      { parse: 'response', onError: 'null' }, opts || {});
    options.headers = Object.assign({}, options.headers || {});
    if (!options.headers['Idempotency-Key']) {
      options.headers['Idempotency-Key'] = newIdempotencyKey();
    }
    let response = null;
    for (let attempt = 0; attempt < _IDEMPOTENT_WRITE_MAX_ATTEMPTS; attempt++) {
      if (options.signal && options.signal.aborted) return response;
      response = await request(path, options);
      if (response && response.ok) return response;
      const retryable = response == null
        || _IDEMPOTENT_WRITE_RETRY_STATUSES.has(Number(response.status));
      if (!retryable || attempt + 1 >= _IDEMPOTENT_WRITE_MAX_ATTEMPTS) {
        if (response && !response.ok) {
          console.warn('[Api] idempotent write failed after %d attempt(s): %s %s (HTTP %s)',
                       attempt + 1, options.method || 'POST', path, response.status);
        }
        return response;
      }
      const ready = await _waitForIdempotentRetry(
        _idempotentRetryAfterMs(response, attempt), options.signal);
      if (!ready) return response;
    }
    return response;
  }

  // Config/settings resolution is a read expressed as POST because its input
  // is a structured snapshot.  A managed-worker replacement can make the
  // outer VS Code proxy return a synthetic 500 while the browser still holds
  // that exact snapshot.  Recover only transport/gateway failures that carry
  // no backend request id: an application-authored 5xx must remain visible,
  // and mutation/task POSTs never enter this retry boundary.
  const _RESOLVER_RECOVERY_STATUSES = new Set([500, 502, 503, 504]);
  const _RESOLVER_RECOVERY_WINDOW_MS = 75000;
  const _RESOLVER_RECOVERY_MAX_ATTEMPTS = 20;
  const _RESOLVER_RECOVERY_ATTEMPT_TIMEOUT_MS = 10000;

  function _resolverRecoveryFailure(error) {
    const detail = error && typeof error === 'object' ? error : {};
    const status = Number(detail.status || 0);
    const code = String(detail.code || '');
    if (status === 0) return code === 'network' || code === 'timeout';
    return _RESOLVER_RECOVERY_STATUSES.has(status) && !detail.serverRequestId;
  }

  function _resolverRecoveryDelayMs(retryIndex) {
    return Math.min(5000, 250 * Math.pow(2, retryIndex));
  }

  async function _resolveAcrossWorkerRecovery(operation, label) {
    const startedAt = Date.now();
    const deadline = startedAt + _RESOLVER_RECOVERY_WINDOW_MS;
    let attempt = 0;
    let lastError = null;
    while (attempt < _RESOLVER_RECOVERY_MAX_ATTEMPTS) {
      const remainingMs = deadline - Date.now();
      if (attempt > 0 && remainingMs <= 0) throw lastError;
      try {
        const result = await operation(Math.max(
          1, Math.min(_RESOLVER_RECOVERY_ATTEMPT_TIMEOUT_MS, remainingMs)));
        if (attempt > 0) {
          console.info('[Api] %s recovered after %d retry attempt(s)',
                       label, attempt);
        }
        return result;
      } catch (error) {
        lastError = error;
        attempt += 1;
        if (!_resolverRecoveryFailure(error)
            || attempt >= _RESOLVER_RECOVERY_MAX_ATTEMPTS) throw error;
        const delayMs = Math.min(
          _resolverRecoveryDelayMs(attempt - 1), deadline - Date.now());
        if (delayMs <= 0) throw error;
        if (attempt === 1) {
          console.warn('[Api] %s interrupted outside the backend; waiting for worker recovery',
                       label);
        }
        const ready = await _waitForIdempotentRetry(delayMs);
        if (!ready) throw error;
      }
    }
    throw lastError;
  }

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

  function requiredItemsList(data, path) {
    if (data && Array.isArray(data.items)) return data.items;
    if (Array.isArray(data)) return data;  // legacy bare-array compatibility
    throw new ApiError(`invalid list response from ${path}`, {
      code: 'parse',
      url: _resolve(path),
      body: data,
    });
  }

  // folders ---------------------------------------------------------
  const folders = {
    // Empty is valid data; transport/parse failure must stay distinguishable
    // so the folder owner can preserve its last good projection.
    list:   async ()           => {
      const path = '/api/v1/folders';
      const data = await get(path, {
        onError: 'throw', coalesce: true, priority: 'normal'
      });
      return requiredItemsList(data, path);
    },
    create: (name, color)      => post('/api/v1/folders', { name, color: color || '' }, { onError: 'null' }),
    update: (id, updates)      => put(`/api/v1/folders/${encodeURIComponent(id)}`, updates, { onError: 'null' }),
    remove: async (id)         => {
      const r = await del(`/api/v1/folders/${encodeURIComponent(id)}`, { parse: 'response', onError: 'null' });
      return !!(r && r.ok);
    },
  };

  // users -----------------------------------------------------------
  //  Authenticated responses always carry the explicit positive ownerId used
  //  by storage and push routing. Network/authorization failures return null;
  //  the push gate remains closed until a later retry resolves the owner.
  const users = {
    me: () => get('/api/v1/users/me', {
      onError: 'null', coalesce: true, priority: 'foreground'
    }),
  };

  // paper-folders (Reading-mode library folders — same shape as `folders`) ---
  const paperFolders = {
    // Same failure-preserving list contract as folders.list above.
    list:   async ()           => {
      const path = '/api/v1/paper-folders';
      const data = await get(path, {
        onError: 'throw', coalesce: true, priority: 'normal'
      });
      return requiredItemsList(data, path);
    },
    create: (name, color)      => post('/api/v1/paper-folders', { name, color: color || '' }, { onError: 'null' }),
    update: (id, updates)      => put(`/api/v1/paper-folders/${encodeURIComponent(id)}`, updates, { onError: 'null' }),
    remove: async (id)         => {
      const r = await del(`/api/v1/paper-folders/${encodeURIComponent(id)}`, { parse: 'response', onError: 'null' });
      return !!(r && r.ok);
    },
  };

  // The typed startup client installs methods after this placeholder exists.
  // Keeping it stable preserves references captured during boot.
  const orchestrations = {};

  // memory ----------------------------------------------------------
  const memory = {
    // summary=1 keeps bodies out of the list payload (megabytes once a
    // project accumulates memories); cards fetch their body lazily on expand.
    list:           (scope)     => get('/api/v1/memory', { query: { scope: scope || 'all', summary: 1 } }),
    get:            (id)        => get(`/api/v1/memory/${encodeURIComponent(id)}`),
    create:         (entry)     => post('/api/v1/memory', entry, { parse: 'response' }),
    remove:         (id)        => del(`/api/v1/memory/${encodeURIComponent(id)}`, { parse: 'response' }),
    toggle:         (id)        => post(`/api/v1/memory/${encodeURIComponent(id)}/toggle`, undefined, { parse: 'response' }),
    clearPreview:   ()          => get('/api/v1/memory/actions/clear'),
    clearAll:       ()          => post('/api/v1/memory/actions/clear', { confirm: true }),
  };

  // skills (user-installed skill packages — a different noun from memory)
  const skills = {
    list:           (scope)     => get('/api/v1/skills', { query: { scope: scope || 'all' } }),
    uninstall:      (id)        => del(`/api/v1/skills/${encodeURIComponent(id)}`, { parse: 'response' }),
    toggle:         (id)        => post(`/api/v1/skills/${encodeURIComponent(id)}/toggle`, undefined, { parse: 'response' }),
    files:          (id)        => get(`/api/v1/skills/${encodeURIComponent(id)}/files`),
    install:        (formData)  => request('/api/v1/skills/install', { method: 'POST', body: formData, parse: 'response' }),
    catalog:        ()          => get('/api/v1/skills/catalog'),
    catalogSearch:  (query, limit) => get('/api/v1/skills/catalog/search', { query: { q: query, limit: limit || 8 } }),
    catalogInstall: (skillId, scope, sourceRevision, overwrite) => post('/api/v1/skills/catalog/install', { skill_id: skillId, scope: scope || 'global', source_revision: sourceRevision || '', overwrite: Boolean(overwrite) }, { parse: 'response' }),
    // Per-skill env/key bindings (credential-vault backed; redacted status only)
    envStatus:      (id)        => get(`/api/v1/skills/${encodeURIComponent(id)}/env`),
    envSet:         (id, name, value) => put(`/api/v1/skills/${encodeURIComponent(id)}/env`, { name, value }, { parse: 'response' }),
    envDelete:      (id, name)  => del(`/api/v1/skills/${encodeURIComponent(id)}/env/${encodeURIComponent(name)}`, { parse: 'response' }),
    setScope:       (id, scope) => post(`/api/v1/skills/${encodeURIComponent(id)}/scope`, { scope }, { parse: 'response' }),
  };

  // tools (live tool-registry inventory — Settings → 工具 panel) ----
  const tools = {
    inventory:      ()          => get('/api/v1/tools'),
  };

  // local knowledge base (persistent files + one conditional search tool)
  const knowledge = {
    status:         (options)   => {
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
      return get('/api/v1/knowledge' + (suffix ? `?${suffix}` : ''));
    },
    activity:       ()          => get('/api/v1/knowledge/activity'),
    setEnabled:     (enabled)   => post('/api/v1/knowledge/settings', { enabled: !!enabled }),
    setVisualEnrichment: (enabled) => post(
      '/api/v1/knowledge/settings', { visual_enrichment: !!enabled }),
    upload:         (formData)  => request('/api/v1/knowledge/documents', {
      method: 'POST', body: formData, timeout: 0,
    }),
    search:         (query, limit) => post('/api/v1/knowledge/search', {
      query: String(query || ''), limit: Number(limit || 6),
    }),
    content:        (id, offset, limit) => get(
      `/api/v1/knowledge/documents/${encodeURIComponent(id)}/content` +
      `?offset=${Math.max(0, Number(offset || 0))}&limit=${Math.max(1, Number(limit || 80))}`),
    reindex:        (id)        => post(
      `/api/v1/knowledge/documents/${encodeURIComponent(id)}/reindex`, {}),
    remove:         (id)        => del(`/api/v1/knowledge/documents/${encodeURIComponent(id)}`),
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

  // context (structured identity, work rules, response preferences) --
  const userContext = {
    get:       ()              => get('/api/v1/context'),
    replace:   (items)         => put('/api/v1/context', { items: items || [] }),
    create:    (item)          => post('/api/v1/context', item),
    update:    (id, updates)   => put(`/api/v1/context/${encodeURIComponent(id)}`, updates),
    remove:    (id)            => del(`/api/v1/context/${encodeURIComponent(id)}`),
    undo:      (changeId)      => post(`/api/v1/context/changes/${encodeURIComponent(changeId)}/undo`, {}),
  };

  // timer -----------------------------------------------------------
  const timer = {
    list:     (summaryOnly)   => get('/api/v1/timer/list', {
      query: summaryOnly ? { summary: 1 } : {},
    }),
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
  // Server-authoritative per-task request fold (docs/FRONTEND_ARCHITECTURE.md
  // P2). byConv: task rows for a conversation; getRequests: metadata-only
  // request rows + attempts; getRequestPayload: full payload for one round
  // (kind='state' serves the post-tool / final / fallback mirrors).
  const tasks = {
    byConv: (convId, opts) =>
      get(`/api/v1/tasks/by-conv/${encodeURIComponent(convId)}`,
          { query: { ...((opts && opts.limit) ? { limit: opts.limit } : {}),
                     ...((opts && opts.before) ? { before: opts.before } : {}) },
            onError: 'null' }),
    getRequests: (taskId) =>
      get(`/api/v1/tasks/${encodeURIComponent(taskId)}/requests`,
          { onError: 'null' }),
    getRequestPayload: (taskId, roundNum, turn, kind) =>
      get(`/api/v1/tasks/${encodeURIComponent(taskId)}/requests/${encodeURIComponent(roundNum)}`,
          { query: { ...(turn ? { turn } : {}), ...(kind ? { kind } : {}) },
            onError: 'null' }),
    getRawArchiveChunk: (taskId, archiveId, part, offset) =>
      get(`/api/v1/tasks/${encodeURIComponent(taskId)}/raw-archives/${encodeURIComponent(archiveId)}/${encodeURIComponent(part)}`,
          { query: { offset: offset || 0, limit: 256 * 1024 },
            onError: 'null' }),
    // Turn Trace (docs/TURN_TRACE_CONTRACT.md): the server-folded timing
    // span tree for one task — the drawer NEVER derives timing itself.
    getTrace: (taskId) =>
      get(`/api/v1/tasks/${encodeURIComponent(taskId)}/trace`,
          { onError: 'null' }),

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

    workspace: (direction, lang) =>
      get('/api/v1/research/workspace',
          { query: { direction: direction || '', lang: lang || 'en' } }),
    saveWorkspace: (payload) =>
      put('/api/v1/research/workspace', payload),
    capabilities: () =>
      get('/api/v1/research/capabilities'),
    scaffoldManuscript: (payload) =>
      post('/api/v1/research/manuscript/scaffold', payload),
    sourceArchiveUrl: (direction, lang) =>
      _resolve('/api/v1/research/manuscript/source.zip?direction=' +
        encodeURIComponent(direction || '') + '&lang=' +
        encodeURIComponent(lang || 'en')),
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
    getSummary: (convId, archiveId) =>
      get(`/api/v1/conversations/${encodeURIComponent(convId)}/compactions/${encodeURIComponent(archiveId)}`,
          { query: { includeMessages: 'false' }, coalesce: true, priority: 'normal' }),
    download: (convId, archiveId) =>
      request(`/api/v1/conversations/${encodeURIComponent(convId)}/compactions/${encodeURIComponent(archiveId)}`,
              { method: 'GET', query: { download: 'true' }, parse: 'response',
                priority: 'foreground' }),
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
  /* ── Per-conv in-flight merge ( ③, hand-off from
   *   ). The 2026-08-01 hard-refresh congestion collapse
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

  // patchSettings: targeted, idempotent settings mutation.
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
    messageActivity: (convId, msgId) =>
      get(`/api/v1/conversations/${encodeURIComponent(convId)}/messages/by-id/${encodeURIComponent(msgId)}/activity`,
          { convId }),
    patchSettings: (convId, patch) =>
      _idempotentResponseWrite(
        `/api/v1/conversations/${encodeURIComponent(convId)}/settings`,
        { method: 'PATCH', json: patch }),
    patchResolvedSettings: (convId, inputs) =>
      _idempotentResponseWrite(
        `/api/v1/conversations/${encodeURIComponent(convId)}/settings/resolve`,
        { method: 'PATCH', json: inputs }),
    // Server-side folder-scoped metadata page.
    listByFolder: (folderId, opts) =>
      get('/api/v1/conversations',
          { query: Object.assign({ folderId }, (opts && opts.before)
              ? { before: opts.before, before_id: opts.beforeId || '', limit: opts.limit || '' }
              : {}), onError: 'null', coalesce: true, priority: 'normal' }),
    // Keyset-paginated global metadata page.
    listPage: (before, beforeId, limit) =>
      get('/api/v1/conversations',
          { query: { before: before || '', before_id: beforeId || '', limit: limit || '' },
            onError: 'null', coalesce: true, priority: 'normal' }),
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
    // Move to recoverable server trash; restore and clone are atomic Sidecar
    // lifecycle transitions and never upload browser-held message arrays.
    remove: (convId) =>
      _idempotentResponseWrite(
        `/api/v1/conversations/${encodeURIComponent(convId)}`,
        { method: 'DELETE' }),
    restore: (convId) =>
      post(`/api/v1/conversations/${encodeURIComponent(convId)}/restore`, {},
           { headers: { 'Idempotency-Key': newIdempotencyKey() } }),
    clone: (convId, conversationId, title) =>
      post(`/api/v1/conversations/${encodeURIComponent(convId)}/clone`,
           { conversationId, title },
           { headers: { 'Idempotency-Key': newIdempotencyKey() } }),
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
    // Server-side extract-file-changes from a tool-rounds payload (v1).
    extractFileChanges: (toolRounds) =>
      post('/api/v1/messages/extract-file-changes', { toolRounds }, { onError: 'null' }),
    // Batch variant — `items` is an array of {toolRounds}; the response's
    // `results` array is aligned by index. Used to seed the bounded
    // file-change presentation cache in one round-trip.
    extractFileChangesBatch: (items) =>
      post('/api/v1/messages/extract-file-changes/batch', { items }, { onError: 'null' }),
    // Server-authoritative cost over a bounded array of
    // { usage, model, provider_id } items. Synchronous render misses are
    // micro-batched by core/cost.js; the response aligns by index.
    costBatch: (items) =>
      post('/api/v1/messages/cost/batch', { items }, { onError: 'null' }),
    // Server-authoritative config/settings resolution. JS ships only the
    // inputs; the server merges (lib/conv_config.py) and returns the canonical
    // dict. Both throw ApiError on non-OK so the caller keeps its error text.
    /* Bounded: the composer send pipeline awaits config/settings resolution
     * before submitting a turn; the transport only enforces a declared
     * timeout, so an unbounded stalled POST latched the composer send lock
     * until the page was reloaded. */
    resolveConfig: (payload) => _resolveAcrossWorkerRecovery(
      (timeout) => post('/api/v1/conversations/config/resolve', payload, { timeout }),
      'conversation config resolution'),
    resolveSettings: (payload) => _resolveAcrossWorkerRecovery(
      (timeout) => post('/api/v1/conversations/settings/resolve', payload, { timeout }),
      'conversation settings resolution'),
    // Sidebar metadata list (no message bodies). Returns the raw Response so
    // the caller can inspect 304 / 503 + Retry-After and pass If-None-Match /
    // an AbortSignal. opts: { prefetch, window, headers, signal }.
    listMeta: (opts) => {
      opts = opts || {};
      const q = { meta: 1 };
      if (opts.prefetch) q.prefetch = opts.prefetch;
      if (opts.window) q.window = opts.window;
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

  // chat (task control plane) ---------------------------------------
  const chat = {
    // Abort + queue management — fire-and-forget, swallow errors.
    abortTask:    (taskId)      => post(`/api/v1/chat/abort/${encodeURIComponent(taskId)}`, undefined, { onError: 'null', parse: 'none', taskId }),
    abortConv:    (convId)      => post(`/api/v1/chat/abort-conv/${encodeURIComponent(convId)}`, undefined, { onError: 'null', parse: 'none', convId, timeout: 15000 }),
    // Per-command interrupt (): kill ONLY the task's running
    // run_command subprocess — the turn CONTINUES with the partial output fed
    // back to the model. NOT fire-and-forget: the caller needs the verdict
    // ({interrupted:true} or {interrupted:false, reason}) to paint the button.
    interruptCommand: (taskId)  => post(`/api/v1/chat/interrupt-command/${encodeURIComponent(taskId)}`, undefined, { onError: 'null', taskId }),
    // Coordinated bare-array migration (batch 21): backend wraps {ok,
    // items}; unwrap with fallback (list-UI || [] semantics).
    queueGet:     async (convId) => {
      const d = await get(`/api/v1/chat/queue/${encodeURIComponent(convId)}`, { convId });
      if (d && Array.isArray(d.items)) return d.items;
      return Array.isArray(d) ? d : [];
    },
    queueRemove:  (convId, qId) => del(`/api/v1/chat/queue/${encodeURIComponent(convId)}/${encodeURIComponent(qId)}`,
                                       { parse: 'response', onError: 'null', convId }),
    queueClear:   (convId)      => del(`/api/v1/chat/queue/${encodeURIComponent(convId)}`,
                                       { parse: 'response', onError: 'null', convId }),
    // Arm Goal Mode mid-stream. The backend queues one explicit, durable
    // continuation behind the accepted turn; it never changes that turn's
    // already-selected interpreter.
    armAutopilot: (convId)      => post('/api/v1/chat/autopilot/arm', { convId },
                                        { onError: 'null', convId }),
    // Disarm Goal Mode — cancels live GoalRuns and any queued Goal
    // continuation, plus legacy compatibility state.
    disarmAutopilot: (convId)   => post('/api/v1/chat/autopilot/disarm', { convId },
                                        { onError: 'null', convId }),
    // Continue Goal Mode on a FINISHED conversation. Creates a turn-native
    // command and launches the same GoalRun + Flow path as an ordinary send.
    // Returns {taskId} or throws ApiError (409 when continuation is unavailable).
    kickAutopilot: (convId, config) => request(
      '/api/v1/chat/autopilot/kick', Object.assign(
        { method: 'POST', json: { convId, config }, parse: 'json', timeout: 0 },
        taskStartAffinityOptions({ convId, config }, null))),
    // Per-node orchestration-flow run trace (resolved brief + bounded I/O per
    // node) for the Studio canvas/inspector overlay. {ok, taskId, flowLabel, trace}.
    flowTrace: (taskId) =>
      get(`/api/v1/chat/flow-trace/${encodeURIComponent(taskId)}`, { onError: 'null', taskId }),
    // A server-created child task that has no direct start response inherits
    // its conversation's ingress affinity key.
    bindTaskAffinity: (taskId, convId, key) =>
      _bindTaskAffinity(taskId, convId, key),
    // Interactive responses for stdin / human-guidance prompts opened by
    // tasks. Both routes return {ok, error?} JSON.
    stdinResponse:  (stdinId, input, eof, convId) =>
      post('/api/v1/chat/stdin-response',
           { stdinId, input: input || '', ...(eof ? { eof: true } : {}) },
           { convId }),
    humanResponse:  (guidanceId, response, convId) =>
      post('/api/v1/chat/human-response', { guidanceId, response }, { convId }),
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
    restart: (payload, opts) => post(
      '/api/v1/update/restart',
      payload || {},
      Object.assign({ onError: 'throw' }, opts || {}),
    ),
    // shutdown: POST — graceful manual stop (writes the manual-shutdown
    // marker so the next boot won't mistake it for an OS kill). No re-exec.
    // Takes {approvalId} once a human approved the pending request.
    shutdown: (payload) => post('/api/v1/update/shutdown', payload || {}, { onError: 'throw' }),
    // Lifecycle approvals ( human-approval gate): a
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
  //   swarm panel reconciler to settle zombie panels.
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
    devices:     () => get('/api/v1/desktop/devices', {
      onError: 'null', coalesce: true, priority: 'background'
    }),
    mintToken:   (name) => post('/api/v1/desktop/token', { name: name || '' }),
    revokeToken: (keyId) => del(`/api/v1/desktop/token/${encodeURIComponent(keyId)}`),
    /* The diagnostics inbox (2026-08-06): the agent's「复制诊断信息」bundle,
     * pasted in the Local Control panel, stored server-side for debugging. */
    submitDiag:  (text) => post('/api/v1/desktop/client-diag', { text: text }),
    listDiags:   () => get('/api/v1/desktop/client-diag', { onError: 'null' }),
    /* Browser-assisted SSO relay: forward the agent's validated poll through
     * this authenticated page, while keeping the endpoint and request shape
     * owned by the unified API client. A raw Response is required because the
     * browser must return both the exact status and body to the local broker. */
    relayPoll:   (payload, bridgeSecret) => request('/api/desktop/poll', {
      method: 'POST',
      json: payload || {},
      headers: Object.assign(
        { 'Accept': 'application/json' },
        bridgeSecret ? { 'X-Bridge-Secret': bridgeSecret } : {}),
      credentials: 'include',
      parse: 'response',
    }),
    /* Browser-pushed zero-config attach (the macOS/Linux counterpart of the
     * personalized Windows installer): mint a fresh attach bundle — route
     * candidates + a per-user agents:bridge token — which this page then
     * hands to the unattached agent's loopback broker. `base` is the page's
     * live origin+BASE_PATH, pinned server-side to the request's Host (the
     * same rule as the .exe download's ?base=). */
    mintAttachBundle: (base) => post('/api/v1/desktop/agent-attach-bundle', {},
      { query: base ? { base: base } : {} }),
  };

  // health / status -------------------------------------------------
  const health = {
    // Returns Response so callers can inspect resp.ok cheaply.
    check: (opts) =>
      request('/api/health',
              Object.assign({ method: 'GET', parse: 'response', onError: 'null' }, opts || {})),
    // Parsed JSON variant — used by the Settings panel to show the version.
    info: (opts) => get(
      '/api/health', Object.assign({ onError: 'null' }, opts || {})),
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

  // Network (proxy pool live test — Settings → Network → 测试) --------
  const network = {
    proxyTest: (payload) =>
      request('/api/v1/network/proxy-test', { method: 'POST', json: payload }),
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

  const costExperiments = {
    report: (days) => get('/api/v1/cost-experiments/report?days=' +
                          encodeURIComponent(days || 14)),
  };

  // features (per-deployment toggles) ------------------------------
  const features = {
    set: (patch) =>
      request('/api/v1/features',
              { method: 'POST', json: patch, parse: 'response' }),
  };

  // providers (one-shot discovery; persistence belongs to modelRouting) ----
  const providers = {
    templates: async () => {
      const data = await get('/api/v1/providers/templates', { onError: 'null' });
      if (data && Array.isArray(data.items)) return data.items;
      return Array.isArray(data) ? data : [];
    },
    compileTemplate: (templateKey, selectedModelIds) =>
      post('/api/v1/providers/templates/compile', {
        template_key: templateKey,
        selected_model_ids: selectedModelIds,
      }),
    probe:            (baseUrl, apiKey, modelsPath)     =>
      post('/api/v1/providers/probe',
           { base_url: baseUrl, api_key: apiKey, models_path: modelsPath },
           { onError: 'null' }),
  };

  // Canonical owner-scoped model/provider access authority. Credential
  // plaintext is written only through the dedicated secret operation.
  const modelRouting = {
    get: () => get('/api/v1/model-routing'),
    replace: (modelRoutingDocument, expectedRevision) => put(
      '/api/v1/model-routing', {
        model_routing: modelRoutingDocument,
        expected_revision: expectedRevision,
      }),
    providers: () => get('/api/v1/providers'),
    createProvider: (bundle, expectedRevision) => post(
      '/api/v1/providers',
      Object.assign({}, bundle || {}, { expected_revision: expectedRevision })),
    provider: (providerId) => get(
      `/api/v1/providers/${encodeURIComponent(providerId)}`),
    saveProvider: (providerId, bundle, expectedRevision) => patch(
      `/api/v1/providers/${encodeURIComponent(providerId)}`,
      Object.assign({}, bundle || {}, { expected_revision: expectedRevision })),
    deleteProvider: (providerId, expectedRevision) => request(
      `/api/v1/providers/${encodeURIComponent(providerId)}`, {
        method: 'DELETE', json: { expected_revision: expectedRevision },
      }),
    putCredentialSecret: (credentialId, secret, expectedRevision) => put(
      `/api/v1/model-routing/credentials/${encodeURIComponent(credentialId)}/secret`, {
        secret,
        expected_revision: expectedRevision,
      }),
    revealCredentialSecret: (credentialId) => post(
      `/api/v1/model-routing/credentials/${encodeURIComponent(credentialId)}/secret/reveal`,
      {}, { onError: 'null' }),
    probeCellsStart: (providerId, body) => post(
      `/api/v1/providers/${encodeURIComponent(providerId)}/probe-cells/start`, body || {}),
    probeCellsStatus: (providerId) => get(
      `/api/v1/providers/${encodeURIComponent(providerId)}/probe-cells/status`),
  };

  // dispatch (model routing — observability + per-key overrides) ----
  const dispatch = {
    endpointMetrics: () => get('/api/v1/dispatch/endpoint-metrics', { onError: 'null' }),
    keyStats:        () => get('/api/v1/dispatch/key-stats', { onError: 'null' }),
    modelHealth:     () => get('/api/v1/dispatch/model-health', { onError: 'null' }),
    keyOverride:     (body) => post('/api/v1/dispatch/key-override', body, { onError: 'null' }),
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
    deviceLoginPost: (provider)     => post('/api/v1/oauth/device-login', { provider }, { onError: 'null', parse: 'response' }),
    deviceLoginGet:  (provider)     =>
      request('/api/v1/oauth/device-login',
              { method: 'GET', query: { provider }, parse: 'response', onError: 'null' }),
    egressAgentGet: ()              => get('/api/v1/oauth/egress-agent', { onError: 'null' }),
    egressAgentSet: (agentId)       => post('/api/v1/oauth/egress-agent', { agent_id: agentId }, { onError: 'null', parse: 'response' }),
  };

  // mcp (Model Context Protocol — server registry + connect) -------
  const mcp = {
    catalogList:      ()                           =>
      request('/api/v1/mcp/catalog',
              { method: 'GET', parse: 'response', onError: 'null' }),
    // Per-server tool list (with per-tool `enabled` flags) — backs the
    // demand-opened Settings → MCP toggle list. First-screen context counts
    // piggyback on server-config and never fetch these full schema rows.
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
    // Upstream version check for installed servers (npm/PyPI latest vs
    // the stored launch spec). Backs the per-card update button.
    updatesCheck:     ()                           =>
      request('/api/v1/mcp/updates',
              { method: 'GET', parse: 'response', onError: 'null' }),
    // Like catalogInstall, no onError:'null' — a failed update carries a
    // rich {error, stderr_tail} body the card should surface verbatim.
    updateApply:      (id)                         =>
      post('/api/v1/mcp/updates/apply', { id }),
  };

  const browser = {
    status: () => get('/api/v1/browser/status'),
    test: () => get('/api/v1/browser/test', { onError: 'null' }),
    adapters: () => get('/api/v1/browser/adapters', { onError: 'null' }),
    access: () => get('/api/v1/browser/access', { onError: 'null' }),
    updateAccess: (body) => request('/api/v1/browser/access', {
      method: 'PUT', json: body || {}, onError: 'null'
    }),
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
  const _PROJECT_PATH_SINGLEFLIGHT_CAPACITY = 16;
  const _PROJECT_PATH_SINGLEFLIGHT_KEY_CHARS = 16 * 1024;
  const _PROJECT_RECENT_PATH_BATCH_LIMIT = 32;
  const _projectPathInflight = new Map();

  function _cloneProjectPathResponse(response) {
    return response && typeof response.clone === 'function'
      ? response.clone() : response;
  }

  function _setProjectPaths(folders, readOnlyPaths, recentPaths) {
    const body = { paths: folders, readOnlyPaths: readOnlyPaths || [] };
    if (Array.isArray(recentPaths) && recentPaths.length) {
      // Keep project selection authoritative even beyond the reconstructible
      // recent-history batch cap. The primary path is always index zero.
      body.recentPaths = recentPaths.slice(0, _PROJECT_RECENT_PATH_BATCH_LIMIT);
    }
    const key = JSON.stringify([
      body.paths, body.readOnlyPaths, body.recentPaths || [],
    ]);
    if (key.length <= _PROJECT_PATH_SINGLEFLIGHT_KEY_CHARS) {
      const existing = _projectPathInflight.get(key);
      if (existing) return existing.then(_cloneProjectPathResponse);
    }
    const pending = request('/api/v1/project/paths', {
      method: 'PUT', json: body, parse: 'response',
    });
    if (key.length <= _PROJECT_PATH_SINGLEFLIGHT_KEY_CHARS
        && _projectPathInflight.size < _PROJECT_PATH_SINGLEFLIGHT_CAPACITY) {
      _projectPathInflight.set(key, pending);
      const clear = () => {
        if (_projectPathInflight.get(key) === pending) {
          _projectPathInflight.delete(key);
        }
      };
      pending.then(clear, clear);
    }
    return pending.then(_cloneProjectPathResponse);
  }

  const project = {
    status:        (convId)   => get('/api/v1/project/status'
                                     + (convId ? ('?conv_id=' + encodeURIComponent(convId)) : ''),
                                     { onError: 'null', coalesce: true,
                                       priority: 'foreground' }),
    setPaths:      (folders, readOnlyPaths, recentPaths) =>
      _setProjectPaths(folders, readOnlyPaths, recentPaths),
    setPath:       (path)     =>
      request('/api/v1/project/set',
              { method: 'POST', json: { path }, parse: 'response' }),
    clear:         ()         =>
      request('/api/v1/project',
              { method: 'DELETE', parse: 'response', onError: 'null' }),
    recentList:    ()         => get('/api/v1/project/recent', {
      onError: 'null', coalesce: true, priority: 'foreground'
    }),
    recentClear:   ()         =>
      _idempotentResponseWrite('/api/v1/project/recent',
                               { method: 'DELETE' }),
    // Advisory probe: nearest enclosing .git root for a staged folder.
    gitRootHint:   (path)   =>
      post('/api/v1/project/git-root-hint', { path }, { onError: 'null' }),
    // Re-key a renamed/moved project (recent + charter + board) to its new path.
    relinkRecent:  (oldPath, newPath) =>
      post('/api/v1/project/recent/relink', { oldPath, newPath }, { onError: 'null' }),
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
    browse:        (path, showHidden, opts) =>
      post('/api/v1/project/browse', { path, showHidden: !!showHidden },
           Object.assign({
             govern: true,
             priority: 'foreground',
             timeout: 12000,
             rpcMethod: 'project.browse',
             rpcParams: { path, showHidden: !!showHidden },
           }, opts || {})),
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
    // Charter and Board are read-only projections. Charter mutations only
    // happen through checker-backed human decision promotion below.
    charter:       (path) =>
      get('/api/v1/project/charter', { query: { path }, onError: 'null' }),
    board:         (path) =>
      get('/api/v1/project/board', { query: { path }, onError: 'null' }),
    brainSummary:  (path) =>
      get('/api/v1/project/brain/summary',
          { query: { path }, onError: 'null' }),
    // Token-free Git integration control plane. These endpoints never ask an
    // agent to inspect or repair changes: immutable checkpoints are merged by
    // Git, and conflicts/gate failures are surfaced as quarantine records.
    integrationStatus: (path) =>
      get('/api/v1/project/integration/status',
          { query: { path }, onError: 'null' }),
    integrationCreate: (path, workId, title) =>
      post('/api/v1/project/integration/create', { path, workId, title: title || '' }),
    integrationRegister: (path, workId, workspacePath, title) =>
      post('/api/v1/project/integration/register',
           { path, workId, workspacePath, title: title || '' }),
    integrationCheckpoint: (path, workId) =>
      post('/api/v1/project/integration/checkpoint', { path, workId }),
    integrationSubmit: (path, workId) =>
      post('/api/v1/project/integration/submit', { path, workId }),
    integrationRetry: (path, workId) =>
      post('/api/v1/project/integration/retry', { path, workId }),
    integrationDiscard: (path, workId) =>
      post('/api/v1/project/integration/discard', { path, workId }),
    integrationPromote: (path, acknowledgeHeadDivergence) =>
      post('/api/v1/project/integration/promote', {
        path, acknowledgeHeadDivergence: !!acknowledgeHeadDivergence,
      }),
    integrationReconcileHead: (path) =>
      post('/api/v1/project/integration/reconcile-head', { path }),
    integrationPrune: (path) =>
      post('/api/v1/project/integration/prune', { path }),
    brainStatus:   (path) =>
      get('/api/v1/project/brain/status', { query: { path }, onError: 'null' }),
    brainWatchList: (path) =>
      get('/api/v1/project/brain/watch', { query: { path }, onError: 'null' }),
    brainWatchAdd: (path, kind, text, conversationId) =>
      post('/api/v1/project/brain/watch/add', {
        path, kind, text, conversationId: conversationId || '',
      }),
    brainWatchUpdate: (path, itemId, patch) =>
      post('/api/v1/project/brain/watch/update',
           Object.assign({ path, itemId }, patch || {})),
    brainWatchDelete: (path, itemId) =>
      post('/api/v1/project/brain/watch/delete', { path, itemId }),
    brainCheckers: (path) =>
      get('/api/v1/project/brain/checkers', { query: { path }, onError: 'null' }),
    brainCheckerRegister: (path, definition) =>
      post('/api/v1/project/brain/checkers/register', { path, definition }),
    brainCheckerRun: (path, checkerId, version, workId) =>
      post('/api/v1/project/brain/checkers/run', {
        path, checkerId, version, workId: workId || '',
      }),
    promoteDecision: (path, decision) =>
      post('/api/v1/project/charter/decision/promote',
           Object.assign({ path }, decision || {})),
  };

  // paper-reader: light shelf summaries, on-demand detail, task streams.
  const paper = {
    libraryList:    ()                    => get('/api/v1/paper/library/summaries', { onError: 'null' }),
    libraryGet:     (id)                  => get(`/api/v1/paper/library/${encodeURIComponent(id)}`, { onError: 'null' }),
    libraryUpsert:  (id, body)            => put(`/api/v1/paper/library/${encodeURIComponent(id)}`, body, { onError: 'null' }),
    libraryDelete:  (id)                  => del(`/api/v1/paper/library/${encodeURIComponent(id)}`, { parse: 'response', onError: 'null' }),
    // Upload + fetch-arxiv-stream stay on /api/paper/* (multipart / SSE
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
    reportResolve:  (paperHash, lang, paperText, opts) =>
      post('/api/v1/paper/report/lookup', Object.assign(
        { lang, include_cache: true },
        paperHash ? { paper_hash: paperHash } : { paper_text: paperText || '' }
      ), Object.assign({ onError: 'null' }, opts || {})),
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
    shotRecipes: ()                   =>
      request('/api/v1/motion/shot-recipes', { method: 'GET', onError: 'null' }),
    audioContract: ()                 =>
      request('/api/v1/motion/audio-contract', { method: 'GET', onError: 'null' }),
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
  // vlmCancel: cooperatively stop one owned queued/running task
  // vlmTasks: lookup VLM tasks by filename
  const pdf = {
    parse:     (formData)            => request('/api/pdf/parse', { method: 'POST', body: formData, timeout: 0 }),
    vlmStart:  (formData)            => request('/api/pdf/vlm-parse', { method: 'POST', body: formData, timeout: 0 }),
    vlmPoll:   (taskId)              => get(`/api/v1/pdf/vlm-parse/${encodeURIComponent(taskId)}`, { onError: 'null' }),
    vlmCancel: (taskId)              => request(`/api/v1/pdf/vlm-parse/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),
    vlmTasks:  (filename)            => get('/api/v1/pdf/vlm-tasks', { query: { filename }, onError: 'null' }),
  };

  // docs (Office / plain text) -------------------------------------
  const doc = {
    parse: (formData) => request('/api/doc/parse', { method: 'POST', body: formData, timeout: 0 }),
  };

  // unified durable chat attachments --------------------------------
  const media = {
    upload: (formData) => request('/api/v1/media/attachments', {
      method: 'POST', body: formData, timeout: 0,
    }),
    get: (attachmentId) => get(
      `/api/v1/media/attachments/${encodeURIComponent(attachmentId)}`,
      { onError: 'null' },
    ),
    remove: (attachmentId) => del(
      `/api/v1/media/attachments/${encodeURIComponent(attachmentId)}`,
      { query: { draft: 1 }, onError: 'null' },
    ),
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
    folders, paperFolders, orchestrations, memory, skills, profile, userContext, timer, scheduler, optimizer, compactions,
    conversations, text, translate, chat, images, pdf, doc, media, audio, videos, artifacts,
    health, pricing, clientError, serverConfig, costExperiments, network, browser, project, daily, paper,
    desktop,
    features, providers, modelRouting, dispatch, oauth, mcp, update, trading, authSources,
    privateHosts, credentials,
    swarm, logs, motion, tasks, users, research, tools, knowledge,
  };

  global.Api = Api;
  Api.ApiError = _transportOwner.ApiError;
})(runtimeScope);

/* Retained sections still call the endpoint facade by its historic `Api`
 * name. Bind that name once inside this ESM composition root instead of
 * relying on a browser-global property or duplicating transport ownership. */
const Api = runtimeScope.Api;

# `static/js/api.js` — Unified Frontend API Client

> **Status (2026-05-28)**: ✅ **Migration complete.** All 18 frontend JS
> files now route every backend HTTP call through `window.Api.<domain>.<method>(...)`.
> The regression guard `tests/test_frontend_api_isolation.py` runs with
> `BASELINE = {}` — any new raw `fetch('/api/...')` outside `api.js`
> fails CI immediately.
>
> **Domain count**: 24 (`folders`, `memory`, `timer`, `scheduler`,
> `optimizer`, `agentBackends`, `compactions`, `conversations`, `text`,
> `translate`, `chat`, `images`, `pdf`, `doc`, `health`, `pricing`,
> `clientError`, `serverConfig`, `browser`, `project`, `daily`, `paper`,
> `features`, `providers`, `dispatch`, `oauth`, `mcp`).

---

## 1. Why

Before this module the frontend made 180+ raw `fetch('/api/...')` calls
across 18+ JS files, each rebuilding URL handling, JSON parsing, error
handling, and timeout logic. That made it impossible to:

- Migrate any single endpoint to `/api/v1` without touching every call
  site.
- Add cross-cutting concerns (auth headers, retries, telemetry) in one
  place.
- Enforce the **separation of concerns** rule from CLAUDE.md (see also
  `.tofu/memories/separation-of-concerns-directive.md`): the frontend
  should only render and call the backend, never duplicate backend
  logic. Talking through one client makes that boundary obvious.

`api.js` is the long-term answer. Every backend HTTP call from the
frontend goes through `window.Api.<domain>.<method>(...)`. The client
owns URL choice — when an endpoint moves from legacy to `/api/v1`, only
`api.js` changes.

## 2. Public API

All on `window.Api`:

```js
// Low-level (use sparingly — prefer domain methods)
Api.request(path, opts)        // {method, query, json, body, headers,
                               //  timeout, parse, signal, onError}
Api.get(path, opts)
Api.post(path, json, opts)
Api.put(path, json, opts)
Api.patch(path, json, opts)
Api.del(path, opts)
Api.stream(path, opts)         // returns Response for SSE / chunked

// Errors
Api.ApiError                   // { status, code, body, url }

// Domains (grow as we migrate)
Api.folders.list()
Api.folders.create(name, color)
Api.folders.update(id, updates)
Api.folders.remove(id)
```

### Options reference

| Option   | Default     | Meaning |
|----------|-------------|---------|
| `method` | `'GET'`     | HTTP verb |
| `query`  | `null`      | `{k:v}` → encoded query string |
| `json`   | `undefined` | Object → JSON body, sets Content-Type |
| `body`   | `undefined` | Raw body (string / FormData / Blob) |
| `headers`| `{}`        | Extra request headers |
| `timeout`| `30000`     | Milliseconds; `0` = no timeout |
| `parse`  | `'json'`    | `'json'` / `'text'` / `'blob'` / `'response'` / `'none'` |
| `signal` | `null`      | Caller-supplied `AbortSignal` |
| `onError`| `'throw'`   | `'throw'` (default) or `'null'` (return null on failure, log warn) |

### Errors

`ApiError`:

- `status`  — HTTP status (0 = network/abort)
- `code`    — `'network'` / `'timeout'` / `'parse'` / backend `error` field
- `body`    — parsed body (json or text) when available
- `url`     — full request URL

By default callers must catch `ApiError`. For "best-effort" fetches use
`{ onError: 'null' }` — failures resolve to `null` and are logged at
`console.warn`. **Never** swallow errors silently; mirror the backend's
"zero silent catches" rule (CLAUDE.md §2.2).

## 3. Architecture rule

> **No JS file other than `api.js` may call `fetch('/api/...')` or
> `fetch(apiUrl('/api/...'))` directly.**

This is enforced by `tests/test_frontend_api_isolation.py`. The test
maintains a per-file ratchet (`BASELINE`) of currently-known legacy
calls, and fails CI if any file's count grows. The end-state target is
an empty `BASELINE`.

The same lint applies to:

- `WebSocket('/api/...')`  — use `pushSubscribe(...)` from `push.js`.
- New `EventSource('/api/...')`  — use `Api.stream(...)`.

## 4. Migration recipe

When you migrate the calls in a JS file:

### Step 1 — Add the domain to `api.js`

Group all calls for one feature under a single domain object near the
bottom of `api.js`. Keep methods thin (URL + verb + minimal shape).

```js
// In api.js
const memory = {
  list:   (scope)        => get('/api/v1/memory', { query: { scope }, onError: 'null' }),
  create: (entry)        => post('/api/v1/memory', entry),
  remove: (id)           => del(`/api/v1/memory/${encodeURIComponent(id)}`),
  toggle: (id, enabled)  => post(`/api/v1/memory/${encodeURIComponent(id)}/toggle`, { enabled }),
};

const Api = { ..., memory };
```

### Step 2 — Replace the call sites

```js
// Before
const resp = await fetch(apiUrl('/api/v1/memory?scope=' + scope));
if (resp.ok) data = await resp.json();

// After
const data = (await Api.memory.list(scope)) || [];
```

### Step 3 — Run the regression guard

```sh
pytest tests/test_frontend_api_isolation.py -v
```

If `test_baseline_reflects_real_counts` reports the file is below
baseline, paste the new (smaller) count into `BASELINE`. **Numbers must
only ever decrease.**

### Step 4 — Bump the cache-busting version

Bump the `?v=...` querystring on the touched JS file in `index.html`
(matches existing convention).

## 5. Mapping legacy → v1

When `/api/v1/<domain>/...` exists and is feature-complete, switch the
domain methods to point at v1:

```js
// Before: legacy
list: () => get('/api/foo'),
// After: v1 with no caller change
list: () => get('/api/v1/foo'),
```

This is the long-term cleanup — done one domain at a time after each
v1 endpoint is verified. The point of this module is that **callers
don't care**.

The `agent_backends` and `folders` domains have already been switched
to `/api/v1/*` (commits 1 and 2 of `docs/legacy_api_migration.md`).
The remaining domains follow the same pattern.

## 6. Streaming / WebSocket

- **Server-push events** (paper progress, translate done, image gen
  progress, etc.) → use `pushSubscribe(channel, taskId, handler)` from
  `push.js`. Do not poll.
- **SSE chat stream** (`/api/chat/stream/<id>`) → currently uses raw
  `fetch` with chunk reading. A future `Api.chat.stream(taskId)` will
  wrap this; until then, the chat stream remains the lone exception
  documented in `BASELINE['main.js']`.
- **Outbound WebSocket** other than `/api/push` should not exist.

## 7. Anti-patterns to avoid

- ❌ `fetch('/api/...')` outside `api.js`.
- ❌ Re-implementing URL building, query encoding, or JSON parse error
  handling in feature modules.
- ❌ Adding a `try/catch` that swallows the error and returns `null`
  silently — pass `{onError:'null'}` instead so the warn lands in the
  console.
- ❌ Backend-style logic (validation, defaulting, transformation) in the
  feature module. If the backend should compute it, add a backend
  endpoint and call it through `api.js`.

## 8. References

- `static/js/api.js`                                    — implementation
- `tests/test_frontend_api_isolation.py`                — ratchet guard
- `.tofu/memories/separation-of-concerns-directive.md`  — frontend/backend boundary
- `routes/api_v1/`                                      — v1 backend surface (long-term target)
- `static/js/push.js`                                   — server-push hub
- `lib/js_bundler.py`                                   — bundle order (`api.js` after `core.js`)

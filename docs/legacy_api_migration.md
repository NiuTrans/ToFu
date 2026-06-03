# Legacy `/api/*` → `/api/v1/*` migration — full inventory & plan

> Working document for the legacy-API removal effort. The end state is
> **zero `/api/<feature>/*` routes outside the carve-out list below**;
> every UI feature talks to `/api/v1/*` (or one of the explicit
> non-v1 carve-outs); `static/js/api.js` URLs are flipped accordingly;
> OpenAPI 3.1 is the contract; legacy handlers are deleted, not shimmed.

Status: **Phase 0 — inventory complete.** Domain-by-domain migration starts next.

---

## 1. Carve-outs — paths that stay non-`v1` by design

These routes either don't fit a JSON/REST contract or are cross-cutting
infrastructure that v1 explicitly delegates to (cf. `routes/api_v1/auth.py`
`_PUBLIC_EXACT`). They are NOT considered legacy and will not be migrated:

| Path | Why it stays |
|---|---|
| `/api/health` | Liveness probe; reused by load balancers. |
| `/api/openapi.json`, `/api/openapi.yaml`, `/api/openapi.refresh` | OpenAPI document itself. |
| `/api/docs`, `/api/redoc` | OpenAPI viewer HTML. |
| `/api/push` | WebSocket multiplexer for server-push events (`lib/push.py`). |
| `/api/client-error` | Browser telemetry beacon — fire-and-forget, no auth contract. |
| `/api/oauth/login`, `/api/oauth/callback`, `/api/oauth/logout` | Browser-redirect OAuth flows (Claude/Codex); GET form-redirects can't live under `/api/v1/*` JSON contract. |
| `/api/chat/stream/<task_id>` | SSE long-lived stream — see §3 below for details. |
| `/api/paper/fetch-arxiv-stream`, `/api/paper/chat`, `/api/paper/upload`, `/api/paper/images/<phash>/<filename>`, `/api/paper/pdf/<filename>` | SSE / multipart / static asset routes. |
| `/api/pdf/parse`, `/api/pdf/vlm-parse`, `/api/doc/parse`, `/api/images/upload`, `/api/images/<filename>` | Multipart upload + static image serving. |
| `/api/translate/pptx`, `/api/translate/pptx/download/<filename>` | Multipart PPTX upload + binary download. |
| `/api/browser/poll`, `/api/browser/commands`, `/api/browser/result`, `/api/browser/download`, `/api/desktop/poll` | Long-poll RPC channels for the Chrome extension + desktop agent. |
| `/api/artifacts/<id>/raw`, `/api/artifacts/<id>/view`, `/api/artifacts/<id>/export` | Static asset serving. |
| `/metrics` | Prometheus exposition. Already non-`/api/`; mentioned for completeness. |
| `/`, `/trading.html`, `/login`, `/signup`, `/dashboard`, `/favicon.*`, `/static/*` | HTML pages + static assets. |

**Action**: relabel these in `docs/HEADLESS_API.md` as "non-versioned
infrastructure" so they stop being conflated with the deprecated UI API
surface. No code change.

---

## 2. Already-on-`/api/v1` (76 routes) — for reference

Surface that exists today in `routes/api_v1/`:

```
auth_mode  : GET/PUT  /api/v1/auth/mode
billing    : pricing wallet ledger redeem deposit debit redeem-codes(POST/GET)
             webhooks/{stripe,alipay} checkout payments
capabilities: GET    /api/v1/capabilities
chat       : POST   /api/v1/chat/completions
             POST   /api/v1/chat/abort/<task_id>
conversations:
             GET    /api/v1/conversations               (list)
             GET    /api/v1/conversations/<id>          (single + messages)
             DELETE /api/v1/conversations/<id>
             GET    /api/v1/conversations/search
             POST   /api/v1/conversations/config/resolve
             POST   /api/v1/conversations/settings/resolve
             POST   /api/v1/conversations/branches/classify
keys       : whoami list create get patch delete
logs       : POST /api/v1/logs/clean
             POST /api/v1/messages/extract-file-changes
             POST /api/v1/text/detect-language
             POST /api/v1/messages/cost(/batch)
providers  : list create get patch delete probe
tasks      : list get events stream abort delete
usage      : GET /api/v1/usage(/summary)
users      : signup login logout me list create get patch (admin)
             POST /api/v1/users/<id>/keys
webhooks   : list create delete
agent_run  : POST /api/v1/agent/run            (single-shot agent runtime)
agents     : paper/report paper/translate translate translate/poll/<id>
             translate/poll/batch paper/report/poll paper/translate/poll
             memory/search search search/async browser/fetch image-gen
             swarm/status/<id> swarm/abort/<id>
```

These are the **target shapes** new code converges toward.

---

## 3. Legacy routes (the migration target list, 130 routes across 24 modules)

Format: `route` — short purpose — proposed v1 path — frontend caller (if any).

### 3.1 `routes/agent_backends.py` (2) — ✅ MIGRATED 2026-05-29

| Legacy | New v1 / disposition |
|---|---|
| `GET  /api/agent-backends/status` | `GET /api/v1/agent-backends/status` (auth-required, no scope; matches legacy semantics) |
| `POST /api/agent-backends/set` | **Deleted** — had zero callers; the active-backend choice lives in conversation settings, persisted via `PATCH /api/v1/chat/tool-state/<conv_id>`. The legacy server-side handler validated and returned but never wrote anything. |

Files: created `routes/api_v1/agent_backends.py`, deleted `routes/agent_backends.py`,
flipped `Api.agentBackends.status` URL in `static/js/api.js`, added 3 tests in
`tests/test_api_v1_integration.py` (auth-required, success, legacy-404).

### 3.2 `routes/artifacts.py` (10) — ✅ MIGRATED 2026-05-29

| Legacy | New v1 / disposition |
|---|---|
| `GET    /api/artifacts/<id>` | `GET    /api/v1/artifacts/<id>` (`@require_auth`) |
| `GET    /api/artifacts/<id>/view` | **carve-out** (HTML wrapper with KaTeX + custom CSP) |
| `GET    /api/artifacts/<id>/raw` | **carve-out** (binary stream + sandbox CSP) |
| `GET    /api/artifacts/<id>/versions` | `GET    /api/v1/artifacts/<id>/versions` |
| `GET    /api/artifacts/conv/<conv_id>` | `GET    /api/v1/artifacts?conv=<id>` (folded into list as filter) |
| `POST   /api/artifacts/<id>/pin` | `POST   /api/v1/artifacts/<id>/pin` |
| `DELETE /api/artifacts/<id>` | `DELETE /api/v1/artifacts/<id>` |
| `GET    /api/artifacts/library` | `GET    /api/v1/artifacts` (library = unfiltered list) |
| `GET    /api/artifacts/<id>/export` | **carve-out** (Playwright PDF download) |
| `POST   /api/artifacts/scan/<conv_id>` | `POST   /api/v1/artifacts/scan` (body: `{conv_id}`) |

Files: created `routes/api_v1/artifacts.py` (6 routes consolidating 7
legacy ones; `library` + `conv/<id>` collapsed into `GET /api/v1/artifacts`
with optional `?conv=<id>` filter), kept `routes/artifacts.py` for the
3 binary/HTML carve-outs, flipped 5 raw `apiUrl()` references in
`static/js/artifacts.js` (no `Api.artifacts` domain exists — the artifact
viewer was always raw fetches; left as-is per the existing pattern).
Added 5 v1 integration tests.

### 3.3 `routes/browser.py` (7) — ✅ MIGRATED 2026-05-29

| Legacy | New v1 / disposition |
|---|---|
| `POST /api/browser/poll` | **carve-out** — Bridge-Secret long-poll |
| `GET  /api/browser/commands` | **carve-out** |
| `POST /api/browser/result` | **carve-out** |
| `GET  /api/browser/status` | `GET /api/v1/browser/status` (`@require_auth`) |
| `GET  /api/browser/clients` | `GET /api/v1/browser/clients` (`@require_auth`) |
| `GET  /api/browser/test` | `GET /api/v1/browser/test` (`@require_auth`) |
| `GET  /api/browser/download` | **carve-out** — binary zip download |

Files: created `routes/api_v1/browser.py`, removed the 3 status/clients/test
routes from `routes/browser.py` (kept the file for the carve-outs),
flipped 1 `Api.browser.status` URL in `static/js/api.js`, updated 4 path
references in `tests/test_bridge_auth.py`, added 4 v1 integration tests.

### 3.4 `routes/chat.py` + side-effect modules (chat_queue/chat_human_io/chat_tool_state) (17) — ✅ MIGRATED 2026-05-29

Approach: blueprint-alias pattern. The 4 source files (`chat.py`,
`chat_queue.py`, `chat_human_io.py`, `chat_tool_state.py`) all import
the new `api_v1_chat_bp` and register their handlers there. The 1 SSE
carve-out (`/api/chat/stream/<task_id>`) stays on `chat_bp`. The
legacy `POST /api/chat/queue` (manual enqueue) was deleted outright —
`/api/v1/chat/send` auto-decides between immediate-start vs enqueue
so the manual endpoint had no remaining callers.

**Endpoint name collisions** are an issue with this domain (uniquely):
the existing v1 chat blueprint already had `chat_abort` (for
`/api/v1/chat/abort/<task_id>`), and the legacy chat module also has
`chat_abort`. Since both register on `api_v1_chat_bp` after the alias,
Flask refuses with `View function mapping is overwriting an existing
endpoint function`. Fixed by adding `endpoint='ui_<func_name>'` to all
16 legacy decorators — they share URL paths but get unique endpoint
names in the blueprint. The `url_for()` callers inside the legacy
handlers don't reference these endpoint names, so there's no fallout.

Files: rewrote 17 of 18 route decorators across the 4 source files
to use `api_v1_chat_bp` + `/api/v1/chat/*` URLs (with `endpoint='ui_*'`
disambiguation); deleted the legacy `POST /api/chat/queue` enqueue
handler entirely. Flipped 16 `Api.chat.*` URLs in `static/js/api.js`
(`streamResponse` keeps the legacy SSE URL). Fixed 1 raw fetch in
`static/js/main/main_send_pipeline.js` (`apiUrl('/api/chat/start')`
→ `apiUrl('/api/v1/chat/start')`). Added 3 v1 integration tests.

This is the largest single domain. Maps onto the existing v1 task-runtime
shape (`/api/v1/chat/completions`, `/api/v1/tasks/*`).

| Legacy | Proposed v1 | Notes |
|---|---|---|
| `GET  /api/chat/active` | `GET  /api/v1/chat/active` | list of in-flight tasks for UI reconnect |
| `POST /api/chat/start` | folded into `POST /api/v1/chat/completions` (UI clients send `mode='ui'`) | preserve task semantics |
| `POST /api/chat/send` | `POST /api/v1/chat/send` | UI message-send (front door, includes queue gate) |
| `POST /api/chat/regenerate` | `POST /api/v1/chat/regenerate` | |
| `POST /api/chat/continue` | `POST /api/v1/chat/continue` | |
| `POST /api/chat/branch/start` | `POST /api/v1/chat/branch` | |
| `GET  /api/chat/stream/<task_id>` | **carve-out** (SSE) — but rename to `GET /api/v1/tasks/<id>/stream` (already exists) |
| `POST /api/chat/abort-conv/<conv_id>` | `POST /api/v1/chat/abort?conv=<id>` |
| `POST /api/chat/abort/<task_id>` | `POST /api/v1/chat/abort/<task_id>` (already exists) |
| `GET  /api/chat/poll/<task_id>` | `GET  /api/v1/tasks/<task_id>` (already exists) — flip frontend |
| `GET  /api/chat/send-translate-status/<conv_id>` | `GET  /api/v1/chat/translate-status/<conv_id>` |
| `POST /api/chat/queue` (legacy) | DELETE — superseded by `/api/chat/send` queue gate (already deprecated in code) |
| `GET    /api/chat/queue/<conv_id>` | `GET    /api/v1/chat/queue/<conv_id>` |
| `DELETE /api/chat/queue/<conv_id>/<queue_id>` | `DELETE /api/v1/chat/queue/<conv_id>/<queue_id>` |
| `DELETE /api/chat/queue/<conv_id>` | `DELETE /api/v1/chat/queue/<conv_id>` |
| `POST   /api/chat/stdin_response` | `POST   /api/v1/chat/stdin-response` | renamed for hyphen consistency |
| `POST   /api/chat/human_response` | `POST   /api/v1/chat/human-response` | |
| `PATCH  /api/chat/tool-state/<conv_id>` | `PATCH  /api/v1/chat/tool-state/<conv_id>` |

### 3.5 `routes/common.py` (auth stubs + pricing + dispatch + features) (15) — ✅ MIGRATED 2026-05-29

| Legacy | New v1 / disposition |
|---|---|
| `GET  /api/me` | **DELETED** — replaced by `/api/v1/users/me` |
| `POST /api/login` | **DELETED** — replaced by `/api/v1/users/login` |
| `POST /api/logout` | **DELETED** — replaced by `/api/v1/users/logout` |
| `POST /api/register` | **DELETED** — replaced by `/api/v1/users/signup` |
| `POST /api/log/compress` | `POST /api/v1/logs/compress` |
| `GET  /api/pricing` | `GET  /api/v1/pricing` |
| `GET  /api/pricing/data` | `GET  /api/v1/pricing/data` |
| `POST /api/pricing/refresh` | `POST /api/v1/pricing/refresh` |
| `GET  /api/dispatch/quota` | `GET  /api/v1/dispatch/quota` |
| `GET  /api/dispatch/endpoint-metrics` | `GET  /api/v1/dispatch/endpoint-metrics` |
| `GET  /api/dispatch/key-stats` | `GET  /api/v1/dispatch/key-stats` |
| `POST /api/dispatch/key-override` | `POST /api/v1/dispatch/key-override` |
| `GET  /api/features` | `GET  /api/v1/features` |
| `POST /api/features` | `POST /api/v1/features` |
| `POST /api/client-error` | **carve-out** (browser telemetry beacon) |
| `GET  /api/health` | **carve-out** (liveness probe) |

Files: created `routes/api_v1/common.py` (43 LOC); rewrote 10 of 22
route decorators in `routes/common.py` to use `api_v1_common_bp`;
deleted the 4 single-user auth stubs entirely (already redundant with
`/api/v1/users/*`); 12 page-serving + telemetry/health carve-outs stay
on `common_bp`. Flipped 7 `Api.{pricing,features,dispatch}.*` URLs in
`static/js/api.js`. Fixed 3 isolation violations: 1 raw fetch in
`static/js/log-clean.js` (`/api/log/compress` → `/api/v1/logs/compress`),
2 `Api.post('/api/dispatch/key-override', ...)` calls in
`static/js/settings/key_stats.js` switched to `Api.dispatch.keyOverride(...)`.
Added 4 v1 integration tests.

### 3.6 `routes/config.py` (9) — ✅ MIGRATED 2026-05-29

| Legacy | New v1 |
|---|---|
| `GET  /api/server-config` | `GET  /api/v1/server-config` |
| `POST /api/server-config` | `POST /api/v1/server-config` |
| `GET  /api/feishu/status` | `GET  /api/v1/feishu/status` |
| `POST /api/provider-balance` | `POST /api/v1/providers/balance` |
| `POST /api/discover-models` | `POST /api/v1/providers/discover-models` |
| `POST /api/update-provider-template` | `PUT  /api/v1/providers/templates/update` |
| `POST /api/provider-probe` | `POST /api/v1/providers/probe` |
| `POST /api/provider-probe-bulk` | `POST /api/v1/providers/probe-bulk` |
| `GET  /api/provider-templates` | `GET  /api/v1/providers/templates` |

Approach: blueprint-import-alias pattern (every route migrates).
Created `routes/api_v1/config.py` (34 LOC) defining `api_v1_config_bp`.
`routes/config.py` `Blueprint('config', __name__)` replaced with
`from routes.api_v1.config import api_v1_config_bp as config_bp` so all
9 route decorators register on the v1 blueprint. The 2 internal callers
(`server.py` + `routes/upload.py`) of `_read_server_config` keep working
via the `routes.config` module path. `update-provider-template` flipped
from `POST` to `PUT` per the original spec.

The new template/probe routes live alongside the existing
`/api/v1/providers/{id}` CRUD routes in `routes/api_v1/providers.py`
— distinct paths, no collision (`<prov_id>` vs static names).

Files: created `routes/api_v1/config.py`; rewrote 9 route decorator
paths in `routes/config.py`; aliased `config_bp` to `api_v1_config_bp`;
dropped `config_bp` from `ALL_BLUEPRINTS`; flipped 9 frontend URLs in
`static/js/api.js` (`Api.serverConfig.*` + 6 `Api.providers.*`,
updateTemplate now PUT); replaced 1 raw fetch in
`static/js/settings/core_panel.js` (`_loadServerConfig` now uses
`Api.serverConfig.get()`). Added 3 v1 integration tests.

### 3.7 `routes/conversations.py` + search + compaction (13) — ✅ MIGRATED 2026-05-29

All 13 routes (list, get, debug-messages, export, PUT, DELETE, settings PATCH,
message DELETE, message PATCH, by-id message PATCH, search, compactions list,
compactions get, branch DELETE) moved to `/api/v1/conversations/*`. Each
gained `@require_scope('conversations')` via the existing v1 blueprint
decorator chain — the legacy module's blueprint is now an alias of
`api_v1_conversations_bp`:

```python
# routes/conversations.py
from routes.api_v1 import api_v1_conversations_bp as conversations_bp
```

This keeps `from routes.conversations import conversations_bp` working
for the 8 internal callers (sched/feishu/autopilot/endpoint/etc. import
`build_search_text` from the same module). Legacy `conversations_bp`
removed from `routes/__init__.py`'s `ALL_BLUEPRINTS` list. The 5
proxy stubs in `routes/api_v1/conversations.py` (list/get/delete/
search/delete-branch) were deleted since the legacy module now
registers the canonical implementations directly on the v1 blueprint.

Files: re-pointed 13 routes in `routes/conversations.py` +
`routes/conversations_search.py` + `routes/conversations_compaction.py`
to `/api/v1/conversations/*`; aliased `conversations_bp` to
`api_v1_conversations_bp`; deleted 5 proxy stubs in
`routes/api_v1/conversations.py`; flipped 11 `Api.conversations.*` /
`Api.compactions.*` URLs in `static/js/api.js`; flipped 1 raw fetch in
`static/js/core/conversations.js` (`loadConversationsFromServer`'s
metadata GET); updated 3 docstring references. Added 1 v1 integration
test (legacy-search-404).

### 3.8 `routes/daily_report.py` (13) — ✅ MIGRATED 2026-05-29

All 13 routes (MyDay panel: POST analyse, GET cached, POST backfill,
GET calendar, PATCH task-status, PATCH todo-toggle, PATCH inherited-todo-toggle,
DELETE inherited-todo, GET conv-count, POST/DELETE task, POST generate,
GET status) mapped 1:1 to `/api/v1/daily-report/*`. Each gained
`@require_auth`. Response shapes preserved verbatim.

`start_report_scheduler()` and `invalidate_day_cost_cache` were previously
imported from `routes/daily_report.py` by external callers
(`routes/__init__.py`, `routes/conversations.py`, `debug/test_daily_cost_cache.py`);
all redirected to `lib.daily_report` (where the logic already lives).

Files: created `routes/api_v1/daily_report.py`, deleted `routes/daily_report.py`,
flipped 10 `Api.daily.*` URLs in `static/js/api.js`, updated 3 import
paths in `routes/__init__.py` + `routes/conversations.py` +
`debug/test_daily_cost_cache.py`. Added 3 v1 integration tests.

### 3.9 `routes/desktop.py` (2) — ✅ MIGRATED 2026-05-29

| Legacy | New v1 / disposition |
|---|---|
| `POST /api/desktop/poll` | **carve-out** — stays at `/api/desktop/poll` (Bridge-Secret-authenticated long-poll between server and desktop agent, not a JSON REST verb) |
| `GET  /api/desktop/status` | `GET /api/v1/desktop/status` (`@require_auth`, bare-dict shape preserved: `{connected, last_poll, pending_commands}`) |

Files: created `routes/api_v1/desktop.py` (re-uses the helpers in
`routes/desktop.py` rather than duplicating queue state); kept
`routes/desktop.py` for the poll carve-out + tool execution helpers;
updated 2 path references in `tests/test_bridge_auth.py`; added 3
tests.

### 3.10 `routes/endpoint.py` (2) — ✅ MIGRATED 2026-05-29

`/api/endpoint/start` and `/api/endpoint/status/<id>` moved to
`/api/v1/endpoint/{start, status/<id>}`. Both `@require_auth`; `start`
keeps the `rate_limit(10/60)` from the legacy route. Bare
`{taskId, convId}` and full status-dict response shapes preserved
(no `{ok:true}` envelope) because the frontend reads `data.taskId`
and the panel reads top-level fields directly.

Files: created `routes/api_v1/endpoint.py`, deleted `routes/endpoint.py`,
flipped one URL in `static/js/main/main_send_pipeline.js`, added 4
tests (auth-required, missing-user-message-400, status-404,
legacy-404).

### 3.11 `routes/folders.py` (5) — ✅ MIGRATED 2026-05-29

Clean 1:1 CRUD migration. Routes now under `/api/v1/folders/*` with
`@require_auth`, OpenAPI metadata, and atomic writes via
`lib.json_store.update_json_atomic`. Legacy module deleted.
Frontend `Api.folders.*` URLs flipped; create/update intentionally
return the bare folder dict (not the `{ok:true, ...}` envelope) so
`Object.assign(_folders[idx], updated)` doesn't leak an `ok:true`
field onto the cached folder object.

### 3.12 `routes/mcp.py` (9) — ✅ MIGRATED 2026-05-29

All 9 routes (servers CRUD + connect/disconnect + tools + catalog +
install/uninstall) moved to `/api/v1/mcp/*`. Each requires
`@require_auth`; soft-uninstall semantics preserved (default disables
but keeps env; `purge: true` hard-deletes). The bare-dict response
shapes (`{ok, servers, ...}`, `{ok, message, tools_count}`, etc.) are
preserved verbatim because the Settings UI reads top-level fields
directly without an envelope.

Files: created `routes/api_v1/mcp.py`, deleted `routes/mcp.py`,
flipped 6 `Api.mcp.*` URLs in `static/js/api.js`, updated 1 reference
comment in `lib/mcp/client.py`. Added 5 v1 integration tests.

### 3.13 `routes/memory.py` (11) — ✅ MIGRATED 2026-05-29

All 11 routes (CRUD + merge + toggle + zip-install + catalog + files)
moved to `/api/v1/memory/*`. Each requires `@require_auth`; no
additional scope is needed because memories are user-owned and the
cookie-auth UI uses them heavily (Settings / Skills store / drag-drop).

The `POST /api/v1/memory/install` route accepts both `multipart/form-data`
(zip upload) AND `application/json` (`{path: '/abs/path'}`); the v1
location handles both content types unchanged. The `POST /api/v1/memory/catalog/install`
route downloads the zip server-side from the curated catalog — not
client-uploaded so it's pure JSON.

Files: created `routes/api_v1/memory.py`, deleted `routes/memory.py`,
flipped 8 `Api.memory.*` URLs in `static/js/api.js`, updated 2 path
references in `CLAUDE.md`, 2 in `docs/api_client.md`, and 2 stale
comments in `static/js/skills.js`. Added 5 v1 integration tests.

### 3.14 `routes/oauth.py` (5) — ✅ MIGRATED 2026-05-29

| Legacy | New v1 / disposition |
|---|---|
| `POST/GET /api/oauth/login` | **carve-out** — browser-redirect PKCE flow |
| `POST/GET /api/oauth/callback` | **carve-out** — code exchange + relay |
| `POST/GET /api/oauth/logout` | **carve-out** — token revocation |
| `GET /api/oauth/status` | `GET /api/v1/oauth/status` (`@require_auth`) |
| `GET /api/oauth/test` | `GET /api/v1/oauth/test` (`@require_scope('admin')`) |

Files: created `routes/api_v1/oauth.py`, removed status+test from
`routes/oauth.py` (kept file for login/callback/logout carve-outs),
flipped 1 `Api.oauth.status` URL in `static/js/api.js`. Added 4 tests.

### 3.15 `routes/optimizer.py` (6) — ✅ MIGRATED 2026-05-29

Flat 1:1 mapping under `/api/v1/optimizer/*`. Reads (`GET proposals` /
`GET proposals/<id>`) require any authenticated key; mutations
(`approve` / `reject` / `revert` / `run-now`) require `admin` scope
— cookie-auth UI calls have admin so the in-app panel keeps working,
headless callers need an admin-scoped key. The `OPTIMIZER_ENABLED`
feature flag still gates mutations + run-now (returns 400+detail when
disabled); reads stay accessible so historical data is visible after
the flag is flipped off.

Files: created `routes/api_v1/optimizer.py`, deleted `routes/optimizer.py`,
flipped 5 `Api.optimizer.*` URLs in `static/js/api.js`, added 4 tests
(reads-need-auth, reads-success, mutation-needs-admin, legacy-404).

### 3.16 `routes/paper.py` (22) — ✅ MIGRATED 2026-05-29

| Group | Disposition |
|---|---|
| `chat`, `fetch-arxiv-stream`, `upload`, `images/<phash>/<file>`, `pdf/<file>` | **carve-out** — stay on legacy `paper_bp` (SSE / multipart / static asset) |
| `extract-images`, `report/{start,poll,abort,lookup,export,cache}`, `translate/{start,poll,abort,lookup,cache}`, `fetch-arxiv` (non-stream), `reparse`, `library`, `library/<id>` (PUT/DELETE) | → `/api/v1/paper/*` (registered on the new `api_v1_paper_bp`) |

Approach: created a new lightweight `routes/api_v1/paper.py` that just
defines `api_v1_paper_bp`. The existing `routes/paper.py` (1520-line
thin route layer + back-compat re-exports) imports the new blueprint
and re-points the 17 JSON routes onto it via decorator rewrite. The 5
carve-out routes stay on `paper_bp`. Both blueprints register at boot.

The `/api/v1/agents/paper/*` SDK aliases in `routes/api_v1/agents.py`
continue to wrap the same engine functions (`start_report_task` etc.
imported from `routes.paper`).

Files: created `routes/api_v1/paper.py` (46 LOC); rewrote 17 of 22
route decorators in `routes/paper.py` to use `api_v1_paper_bp` +
`/api/v1/paper/*` URLs (carve-outs untouched); flipped 14 `Api.paper.*`
URLs + added `Api.paper.exportUrl(...)` URL builder; updated 1 raw
fetch in `static/js/paper-reader.js` (the report export anchor).
Updated 14 URL strings in `tests/test_paper_migration.py` and added an
autouse pytest fixture there forcing `TOFU_AUTH_MODE=open` (these tests
instantiate `server.app` without a Bearer token; the conftest.py default
is `private` and would 401 every HTTP call). All 14 paper-migration
tests pass under both pytest and standalone.

### 3.17 `routes/project.py` (14) — ✅ MIGRATED 2026-05-29

| Legacy | v1 |
|---|---|
| `POST /api/project/set` | `POST /api/v1/project/set` |
| `POST /api/project/set_paths` | `POST /api/v1/project/paths` (PUT) |
| `GET  /api/project/status` | `GET  /api/v1/project/status` |
| `POST /api/project/clear` | `DELETE /api/v1/project` |
| `POST /api/project/browse` | `POST /api/v1/project/browse` |
| `GET/POST/DELETE /api/project/recent` | `GET/POST/DELETE /api/v1/project/recent` |
| `POST /api/project/write_approval` | `POST /api/v1/project/write-approval` |
| `POST /api/project/undo` | `POST /api/v1/project/undo` |
| `POST /api/project/undo_all` | `POST /api/v1/project/undo-all` |
| `GET  /api/project/gitignore/suggestions` | `GET  /api/v1/project/gitignore/suggestions` |
| `POST /api/project/gitignore/accept` | `POST /api/v1/project/gitignore/accept` |
| `POST /api/project/gitignore/dismiss` | `POST /api/v1/project/gitignore/dismiss` |
| `POST /api/project/rescan` | `POST /api/v1/project/rescan` |
| `POST /api/project/redo` | `POST /api/v1/project/redo` |

**Bug fix (commit 13)**: `Api.project.write` called `POST /api/project/write`
which was **never registered** as a backend route \u2014 the "Apply Code" button
in `project.js:1044` has been silently 404'ing. Fixed: added
`POST /api/v1/project/write` (calls `lib.project_mod.write_tools.tool_write_file`
with the active project path). Apply Code now works.

### 3.18 `routes/scheduler.py` (13) — ✅ MIGRATED 2026-05-29

All 9 `/api/scheduler/*` and 4 `/api/timer/*` routes consolidated under
a single v1 blueprint at `/api/v1/scheduler/*` and `/api/v1/timer/*`.
Reads (list/get/history/poll-log/status, proactive status, timer list,
timer status) require `@require_auth`; mutations (pause/resume/delete
task, trigger task, cancel/trigger timer) require `@require_scope('admin')`
— cookie-auth UI keeps working since the local browser surface has admin.

`start_scheduler_worker()` was a non-route helper living in
`routes/scheduler.py`; moved to `lib/scheduler/manager.py` so it's
importable from `lib.scheduler` directly. `routes/__init__.py` now does
`from lib.scheduler import start_scheduler_worker`.

Files: created `routes/api_v1/scheduler.py`, deleted `routes/scheduler.py`,
added `start_scheduler_worker` to `lib/scheduler/manager.py`, flipped
9 `Api.scheduler.*` + `Api.timer.*` URLs in `static/js/api.js`, added
6 tests.

### 3.19 `routes/swarm.py` (3) — ✅ MIGRATED 2026-05-29

`/api/swarm/{status,abort,config}` → `/api/v1/swarm/*`. Status and abort
stay duplicated under `/api/v1/agents/swarm/*` for the headless SDK
alias (returns `api_ok(...)`-enveloped); the new `/api/v1/swarm/*`
routes return the bare-dict shape the in-app debug panel expects
(`data.active`, `data.agents`, etc.). The new `/api/v1/swarm/config`
endpoint has no SDK counterpart — it's the runtime registry list.

Reads (`status`, `config`) require `@require_auth`; `abort` requires
`@require_scope('agents:swarm')` matching the SDK alias. No frontend
`Api.swarm.*` block existed (the routes were referenced only from
backend Python tests + internal panel code), so no `static/js/api.js`
flip was needed. Stale `routes/swarm` references in `lib/swarm/` doc
strings updated to `routes/api_v1/swarm`.

Files: created `routes/api_v1/swarm.py`, deleted `routes/swarm.py`,
updated 5 reference comments in `lib/swarm/{integration,master}.py`,
added 4 tests.

### 3.20 `routes/translate.py` + `translate_mt_test.py` (7) — ✅ MIGRATED 2026-05-29

| Legacy | New v1 / disposition |
|---|---|
| `POST /api/translate` | `POST /api/v1/translate` (`@require_auth`) |
| `POST /api/translate/start` | `POST /api/v1/translate/start` |
| `GET  /api/translate/poll/<id>` | `GET  /api/v1/translate/poll/<id>` |
| `POST /api/translate/poll_batch` | `POST /api/v1/translate/poll-batch` (hyphen-case) |
| `POST /api/translate/mt-test` | `POST /api/v1/translate/mt-test` |
| `POST /api/translate/pptx` | **carve-out** (multipart upload, stays in `routes/translate.py`) |
| `GET  /api/translate/pptx/download/<file>` | **carve-out** (binary download, stays) |

`routes/translate.py` kept for PPTX carve-outs + 30+ back-compat symbol
re-exports (consumed by `routes/chat.py`, `lib/message_queue.py`,
`tests/test_translate_migration.py`, debug scripts). `routes/translate_mt_test.py`
deleted (mt-test moved into the v1 module). Additionally fixed
`static/js/settings/other_tabs.js:134` which was calling
`Api.post('/api/translate/mt-test', ...)` directly—​replaced with `Api.translate.mtTest(...)`.

Files: created `routes/api_v1/translate.py`, removed 4 route handlers from
`routes/translate.py` (kept PPTX), deleted `routes/translate_mt_test.py`,
flipped 5 `Api.translate.*` URLs, fixed 1 isolation violation. Added 4 tests.

### 3.21 `routes/upload.py` (9) — ✅ MIGRATED 2026-05-29

| Legacy | New v1 / disposition |
|---|---|
| `POST /api/images/upload` | **carve-out** — multipart upload |
| `GET  /api/images/<file>` | **carve-out** — static image serving |
| `POST /api/images/generate` | `POST /api/v1/images/generate` |
| `GET  /api/images/models` | `GET  /api/v1/images/models` |
| `POST /api/pdf/parse` | **carve-out** — multipart |
| `POST /api/pdf/vlm-parse` (start) | **carve-out** — multipart |
| `GET  /api/pdf/vlm-parse/<task_id>` (poll) | `GET  /api/v1/pdf/vlm-parse/<task_id>` |
| `GET  /api/pdf/vlm-tasks` | `GET  /api/v1/pdf/vlm-tasks` |
| `POST /api/doc/parse` | **carve-out** — multipart |

Approach: identical to commit 17 (paper). Created
`routes/api_v1/uploads.py` (31 LOC) defining `api_v1_uploads_bp`. The
existing `routes/upload.py` rewrote 4 of 9 route decorators to use the
v1 blueprint + `/api/v1/...` URLs; the 5 multipart / static-asset
routes stay on `upload_bp`. Both blueprints register at boot.

Files: created `routes/api_v1/uploads.py`; rewrote 4 decorators in
`routes/upload.py`; flipped 4 frontend URLs in `static/js/api.js`
(`Api.images.generate`, `Api.images.models`, `Api.pdf.vlmPoll`,
`Api.pdf.vlmTasks`); fixed 1 docstring reference in `image-gen.js`.
Added 3 v1 integration tests.

Also fixed (out-of-band): `tests/conftest.py` now ships an `auth_mode`
pytest marker (`@pytest.mark.auth_mode('open')` and friends) wired to
an autouse fixture that overrides + restores `TOFU_AUTH_MODE` per-test
with proper teardown. Replaces the bespoke per-module fixture I added
in commit 17 — `tests/test_paper_migration.py` now uses
`pytestmark = pytest.mark.auth_mode('open')` (one line).

### 3.22 Trading (67 routes across 7 modules) — ✅ MIGRATED 2026-05-29 (all conditional on `TRADING_ENABLED`)

| File | Legacy | Proposed v1 |
|---|---|---|
| `routes/trading_holdings.py` | 13 | `/api/v1/trading/{holdings,cash,search,nav,nav_history,transactions,network_status,...}` |
| `routes/trading_intel.py` | 9 | `/api/v1/trading/intel/*` |
| `routes/trading_decision.py` | 9 | `/api/v1/trading/{briefing,decisions,trades,fees,...}` |
| `routes/trading_autopilot.py` | 15 | `/api/v1/trading/autopilot/*` |
| `routes/trading_brain.py` | 6 | `/api/v1/trading/brain/*` |
| `routes/trading_simulator.py` | 10 | `/api/v1/trading/sim/*` |
| `routes/trading_tasks.py` | 5 | `/api/v1/trading/tasks/*` |

Frontend: `static/js/trading/state.js` builds URLs via `F._API = "/api/trading"`. Switch the constant to `/api/v1/trading` once the backend routes exist; no JS call-site changes.

---

## 4. Cross-cutting work

1. **`static/js/api.js`**: 41 of 101 method bodies still reference legacy URLs. Each domain migration flips its block.
2. **`static/js/trading/state.js`**: change `F._API`.
3. **`docs/HEADLESS_API.md`** and **`docs/api_client.md`**: update.
4. **`docs/ARCHITECTURE.md`**: re-scan after blueprints move.
5. **`tests/`**: any test that posts to a legacy URL needs updating. Inventory will be domain-local.
6. **`tests/test_frontend_api_isolation.py`**: BASELINE stays empty; new violations should still fail. **Add** an analogous backend test that asserts `routes/` registers no `/api/<feature>/*` routes outside the carve-out list — see `tests/test_legacy_api_removed.py` (to be created in the final cleanup commit).
7. **`lib/openapi.py`**: extend tag inference for new path prefixes.
8. **`lib/api_keys.py` scopes**: every new v1 route needs a scope decorator. Likely all reuse existing scopes (`chat`, `tasks`, `conversations`, `providers`, `admin`); a few new ones (`trading`, `paper`, `project`, `memory`, `optimizer`, `scheduler`, `daily-report`, `agent-backends`) may need adding.
9. **`server.py`**: mention of legacy paths in middleware/logging — sweep for dead references after the cuts.
10. **OpenAPI spec**: each new route gets `@api_meta(...)`.

---

## 5. Execution plan (commits in order)

Each commit is self-contained: v1 route added with auth + tests, frontend
flipped via `static/js/api.js`, legacy route deleted, OpenAPI updated,
relevant tests run.

| # | Domain | Legacy routes | Files touched |
|---|---|---|---|
| 1 ✅ | `agent_backends` | 2 | `routes/agent_backends.py` → split: legacy gone, new `routes/api_v1/agent_backends.py` |
| 2 ✅ | `folders` | 5 | `routes/folders.py` → `routes/api_v1/folders.py` |
| 3 ✅ | `optimizer` | 6 | `routes/optimizer.py` → `routes/api_v1/optimizer.py` |
| 4 ✅ | `scheduler` + `timer` | 13 | merge into `routes/api_v1/scheduler.py` |
| 5 ✅ | `endpoint` | 2 | `routes/endpoint.py` → `routes/api_v1/endpoint.py` |
| 6 ✅ | `swarm` | 3 | collapse into `routes/api_v1/swarm.py` (UI alias of `agents/swarm/*`) |
| 7 ✅ | `desktop` (status only) | 1 | new `routes/api_v1/desktop.py`; keep `/api/desktop/poll` |
| 8 ✅ | `browser` (status/clients/test) | 3 | new `routes/api_v1/browser.py`; keep poll/commands/result/download |
| 9 ✅ | `memory` | 11 | `routes/memory.py` → `routes/api_v1/memory.py` |
| 10 ✅ | `mcp` | 9 | `routes/mcp.py` → `routes/api_v1/mcp.py` |
| 11 ✅ | `daily_report` | 13 | `routes/daily_report.py` → `routes/api_v1/daily_report.py` |
| 12 ✅ | `oauth` | 2 (status+test) | new `routes/api_v1/oauth.py`; keep login/callback/logout |
| 13 ✅ | `project` | 14 (16 routes, 2 method rename) | `routes/project.py` → `routes/api_v1/project.py` |
| 14 ✅ | `translate` | 5 | merge into `routes/api_v1/translate.py`; PPTX stays as carve-out |
| 15 ✅ | `artifacts` | 7 | new `routes/api_v1/artifacts.py`; keep raw/view/export |
| 16 ✅ | `conversations` | 13 | re-pointed legacy module's routes onto `api_v1_conversations_bp`; deleted 5 proxy stubs |
| 17 ✅ | `paper` | 17 | new `routes/api_v1/paper.py`; legacy decorators rewritten in-place; carve-outs stay on `paper_bp` |
| 18 ✅ | `upload` (non-multipart) | 4 | new `routes/api_v1/uploads.py`; multipart + static-asset stays on `upload_bp` |
| 19 ✅ | `common` (auth stubs / pricing / dispatch / features) | 10 migrated + 4 deleted | new `routes/api_v1/common.py`; carve-outs (page serving + health + telemetry) stay on `common_bp` |
| 20 ✅ | `config` (providers + server-config + feishu) | 9 | new `routes/api_v1/config.py` (alias of legacy `config_bp`) |
| 21 ✅ | `chat` | 17 (16 migrated + 1 deleted) | blueprint-alias on `api_v1_chat_bp`; SSE stream stays on `chat_bp`; endpoint=ui_* disambiguation |
| 22 ✅ | `trading_holdings` | 13 | new `routes/api_v1/trading/holdings.py` (alias) |
| 23 ✅ | `trading_intel` | 9 | new `routes/api_v1/trading/intel.py` (alias) |
| 24 ✅ | `trading_decision` | 9 | new `routes/api_v1/trading/decision.py` (alias) |
| 25 ✅ | `trading_autopilot` | 15 | new `routes/api_v1/trading/autopilot.py` (alias) |
| 26 ✅ | `trading_brain` | 6 | new `routes/api_v1/trading/brain.py` (alias) |
| 27 ✅ | `trading_simulator` | 10 | new `routes/api_v1/trading/simulator.py` (alias) |
| 28 ✅ | `trading_tasks` | 5 | new `routes/api_v1/trading/tasks.py` (alias) |
| 29 ✅ | docs + final test | 0 routes | added `tests/test_legacy_api_removed.py` with the carve-out allow-list assertion |

---

## 6. End-state assertion

Once all commits land:

```python
# tests/test_legacy_api_removed.py
ALLOWED_NON_V1 = {
    '/api/health',
    '/api/openapi.json', '/api/openapi.yaml', '/api/openapi.refresh',
    '/api/docs', '/api/redoc',
    '/api/push',
    '/api/client-error',
    '/api/oauth/login', '/api/oauth/callback', '/api/oauth/logout',
    '/api/chat/stream/<task_id>',
    '/api/paper/fetch-arxiv-stream', '/api/paper/chat',
    '/api/paper/upload', '/api/paper/images/<phash>/<filename>',
    '/api/paper/pdf/<filename>',
    '/api/pdf/parse', '/api/pdf/vlm-parse',
    '/api/doc/parse',
    '/api/images/upload', '/api/images/<filename>',
    '/api/translate/pptx', '/api/translate/pptx/download/<filename>',
    '/api/browser/poll', '/api/browser/commands',
    '/api/browser/result', '/api/browser/download',
    '/api/desktop/poll',
    '/api/artifacts/<id>/raw', '/api/artifacts/<id>/view',
    '/api/artifacts/<id>/export',
}

def test_no_legacy_api_routes():
    rules = [r for r in app.url_map.iter_rules() if r.rule.startswith('/api/')]
    legacy = [r for r in rules
              if not r.rule.startswith('/api/v1/')
              and r.rule not in ALLOWED_NON_V1]
    assert not legacy, f"Legacy /api routes still mounted: {legacy}"
```

The presence of this test, green, is the definition of done.

---

## 7. Open questions to resolve during implementation

1. **`POST /api/chat/start` vs `/api/v1/chat/completions`**: today's `/api/chat/start` is the UI-mode entry (full task lifecycle, returns `taskId`). `/api/v1/chat/completions` is OpenAI-shaped. Decision: keep the UI route under v1 as `POST /api/v1/chat/start` (UI mode), distinct from `/api/v1/chat/completions` (OpenAI mode). Same handler internals.
2. **Provider templates: PUT vs POST**: Today `/api/update-provider-template` is POST. Move to `PUT /api/v1/providers/templates/<key>` for REST cleanliness.
3. **`/api/me` removal**: any non-`api.js` caller? Audit during commit 19.
4. **Scope assignments for new routes**: most reuse existing scopes; if any new (`trading`, `daily-report`, …) are introduced, document in `lib/api_keys.py`.

---

End of inventory. Begin commit 1 next.

# API Contract — The Frontend ↔ Backend Interface Constitution

> **This is the single source of truth for how the frontend and backend talk.**
> Consumer-facing endpoint documentation lives in [`HEADLESS_API.md`](HEADLESS_API.md)
> (auth, scopes, per-endpoint reference). THIS document defines the *engineering
> contract*: the response envelope, the error taxonomy, the carve-outs, the
> guard tests that enforce the contract, and the checklist for adding an
> endpoint. When the two disagree about an endpoint's *shape*, this file wins.

---

## 0. API v4 release boundary

`contracts/api_v4.yaml` is the machine-readable authority for the v4 wire
contract. Changes flow one way: edit the OpenAPI 3.1 source, then run
`python3 scripts/gen_api_v4_contract.py`. The generated outputs are dependency-
free server release constants, the strict server `TypedDict`/Pydantic boundary,
plus TypeScript, desktop Python, and Kotlin client DTOs. Ordinary boot imports
only the constants; validators and the OpenAPI document load on a v4 response.
`make contracts-check` rejects drift.

The migration is deliberately staged. The running v4 surface currently
contains only `GET /api/v4/meta` and the raw self-description endpoint
`GET /api/v4/openapi.json`; no product operation is advertised before its
server implementation and all clients ship together. `meta` uses the v4
success envelope:

```json
{
  "data": {
    "apiMajor": 4,
    "schemaVersion": 28,
    "serverBuild": "0.16.0",
    "minDesktopBuild": "0.16.0",
    "minAndroidBuild": 17
  },
  "meta": {"requestId": "page-42", "serverTimeMs": 1787550000000}
}
```

Every v4 error is `application/problem+json`. The OpenAPI document is the only
success-envelope exception because OpenAPI tooling requires the document at
the response root. The canonical contract currently declares
`x-tofu-release-stage: bootstrap`; application assembly therefore rejects an
attempt to set the app-local `ACTIVE_API_MAJOR` release latch to `4`. This
prevents a config-only change from retiring v1/v3 while v4 still contains no
product operations. The latch defaults to integer `1`, accepts no string or
environment coercion, and is frozen during assembly.

After the v4 product routes and all client preflight/version refusals ship, the
contract generator and release stage must be deliberately advanced together.
Only then may release assembly set the latch to integer `4`; requests under
`/api/v1` and `/api/v3` stop before body parsing, authentication, or storage
work and return HTTP 426 with `upgradeUrl: "/api/v4/meta"`. Until that atomic
cutover, the existing v1 and Conversation Sync v3 contracts below remain
active.

Client compatibility is decided against the `minDesktopBuild` or
`minAndroidBuild` returned by that server response, never only against the
minimum compiled into the client. The generated Python/TypeScript/Kotlin
helpers reject malformed dotted desktop builds, a wrong API major, and a live
minimum above the running client. The release stage remains `bootstrap` until
the Web, desktop, and Android startup paths invoke that preflight before any
product operation.

## 1. The five-layer map

Every frontend → backend call passes through five layers. Each layer has ONE
canonical implementation; drift from it is caught by a guard test (§5).

| # | Layer | Canonical implementation | Guard |
|---|-------|--------------------------|-------|
| 1 | Frontend endpoint seam | `window.Api`, the domain-grouped retained registry in `frontend/src/runtime/app-runtime.js` | `tests/test_frontend_api_contract.py` |
| 2 | Transport + correlation | `frontend/src/api/transport.ts`; it mints `X-Request-ID`, which `server.py::_assign_req_id_and_log` honours and echoes | `tests/test_frontend_api_transport_vite.py` + server request-log |
| 3 | Request parsing | `lib/request_parser.py` (`parse_body` / `require_str` / `optional_int` / …; raises `BadRequest` → auto-400) | `tests/test_request_parser.py` |
| 4 | Response envelope | `lib/api_response.py` (`api_ok` / `api_error` / `api_not_found` / …, `sse_response`) | `tests/test_api_contract_drift.py` + `tests/test_api_response_route_conversions.py` |
| 5 | Framework error boundary | `server.py` `@app.errorhandler(404/413/405/500/Exception)` — API paths always get the JSON envelope, never an HTML error page | `tests/test_api_response.py` |

The rule that makes this a *contract* rather than a pile of helpers:
**no layer may be bypassed.** A raw `fetch('/api/...')` outside the typed
transport, a
hand-rolled `request.get_json()` dig, or a bare `return jsonify({...})` in a
route is a contract violation — the ratchets in §5 exist to reject it.

---

## 2. The envelope

### 2.1 Success

```json
{ "ok": true, "...payload fields merged at top level...": "..." }
```

Emitted by `api_ok(data, **extras)` (200), `api_created(...)` (201),
`api_no_content()` (204). Payload keys are merged **top-level** (not nested
under `data`), because ~200 existing frontend call sites read named fields.

### 2.2 Error

```json
{ "ok": false, "error": "human readable" , "request_id": "ab12cd-34" }
```

or, for typed errors, an envelope object:

```json
{ "ok": false,
  "error": {
    "kind": "ratelimit", "severity": "warning", "retryable": true,
    "message": "…", "hint": "Retry after the provider window resets.",
    "detail": "HTTP 429", "model": "gpt-example", "context": "dispatch",
    "source": "provider", "raw": "redacted upstream response"
  },
  "request_id": "ab12cd-34" }
```

Emitted by `api_error(err, status=…)` and its named wrappers:
`api_bad_request` (400), `api_unauthorized` (401), `api_forbidden` (403),
`api_not_found` (404), `api_method_not_allowed` (405), `api_conflict` (409),
`api_payload_too_large` (413), `api_internal_error` (500, auto-logs traceback),
`api_service_unavailable` (503, sets `Retry-After`).

**Result passthrough:** when a lib-layer function already returned
`{ok, error, ...}` and the route only chooses the HTTP status, use
`api_payload(result, status)` — it preserves the result's top-level shape
(`api_error` would WRONGLY nest it under a single `error` key), keeps a
present `ok`, defaults `ok = status < 400` when absent, and attaches
`request_id` on 4xx/5xx. This is the idiom behind the Project Brain routes'
`if not result.get('ok'): return api_payload(result, 409|400)`.

`error` is a **string** for legacy-compatible sites (the frontend reads
`data.error` as a string at >80 places) and a complete **envelope dict** when
the route passes one or when the boundary converts an exception via
`lib/error_envelope.from_exception`. The frontend transport normalizes legacy
strings and older partial `{kind, message}` objects into the complete shape at
its boundary before exposing `ApiError.envelope`; partial objects are not a
valid persistence or new-route contract. New code SHOULD prefer envelopes for
errors the user must *act* on (quota, conflict, approval-needed) and MAY keep
strings for simple validation messages.

HTTP 500 is a strict disclosure boundary. `api_internal_error` records the
actual exception type, message, chain, traceback, route context, and correlated
`request_id` in backend diagnostics, but the response contains only the stable
`internal` / `internal_error` identity and `request_id`. Exception text is never
copied into the public envelope's `detail` or `raw` fields; it may contain
credentials, SQL, provider bodies, or filesystem paths. A caller that has
already logged the same exception may set `log_traceback=False` to avoid a
duplicate record, but it does not weaken response redaction.

### 2.3 Status-code mapping

| Situation | Status | Helper |
|---|---|---|
| Read / success | 200 | `api_ok` |
| Created | 201 | `api_created` |
| Deleted / no body | 204 | `api_no_content` |
| Validation (missing/typed field) | 400 | `api_bad_request` — or raise `request_parser.BadRequest`, auto-converted |
| Authn missing | 401 | `api_unauthorized` |
| Authz refused | 403 | `api_forbidden` |
| Missing resource | 404 | `api_not_found` |
| Version/rev conflict, already-exists, already-running | 409 | `api_conflict` |
| Body too large | 413 | `api_payload_too_large` |
| Uncaught server fault | 500 | `api_internal_error` (or let it propagate — the §5 boundary converts) |
| Transient overload (pool saturated, shed load) | 503 + `Retry-After` | `api_service_unavailable` |

**Do not invent new statuses** for situations the table covers; a 200 with
`{ok:false}` is a contract violation (one deliberate legacy exception:
`api_v1/translate.py` mt-test reports logical failure with 200 —
frozen in the drift baseline, do not copy it).

### 2.4 Correlation

The typed transport mints `X-Request-ID: <page>-<seq>` on every request; the server
prefers the inbound id, stamps it on every log line, echoes it on the
response header, and `api_error` also embeds it as `request_id` in the body.
**When reporting a bug, quote the request_id** — it joins frontend console,
`logs/app.log`, and `logs/error.log` in one grep.

The browser keeps failure channels distinct. `ApiError.code` is always a
machine identifier (or numeric RPC code): transport failures use `timeout`,
`aborted`, `network`, or `parse`; v4 HTTP failures use the RFC 7807 `code`;
v1/v3 failures use an explicit response code, envelope `kind`, or a stable
status-derived fallback. A valid `application/problem+json` body is retained as
`ApiError.problem` and is never coerced into the task-level
`ApiError.envelope`. Both channels retain the client, header, and body request
identifiers so a malformed response is still traceable.

### 2.5 Identity projection

The machine authority is
[`contracts/identity_v1.yaml`](../contracts/identity_v1.yaml); the development
map is [IDENTITY.md](IDENTITY.md).

An authenticated request exposes two deliberately different identifiers:

- `account_user_id`: opaque account subject for login, administration, and
  billing;
- `owner_user_id`: positive integer used by every owned domain resource.

`GET /api/v1/users/me` returns the account at `user.id` and the repository
owner at `ownerId` (also `user.owner_id`). `GET /api/v1/keys/whoami` returns
`account_user_id` and `owner_id`. A route may translate those wire names once;
services and repositories receive the numeric owner through
`PrincipalContext`.

Credential plaintext is returned only by create/login/pairing responses and
never by list/get. Device bridge routes require a literal `agents:bridge`
credential; a package-mint failure is 503, not an unpaired download.

### 2.6 Conversation-sync v3

The complete authority/data-flow and change procedure is
[`CONVERSATION_SYNC_V3.md`](CONVERSATION_SYNC_V3.md). The only authoring
source for these endpoints and DTOs is
`contracts/conversation_sync_v3.yaml`; run
`python3 scripts/gen_conversation_sync_contract.py` after changing it.

The native UI reads one authoritative snapshot from
`GET /api/v3/conversations/{conversationId}/sync` and then follows
`GET /api/v3/conversations/{conversationId}/events`. The stream is ordered for
the whole conversation, and its `id` / `Last-Event-ID` cursor is opaque; clients
must never parse it as an attempt sequence. A projection-bearing
`attempt.event` carries `payload.event.payload.projectionPatch`; a settled
edit or branch mutation carries the same primitive in
`turn.patch.payload.turnPatches[*].projectionPatch`. Neither may retain a full
existing turn projection. A new attempt on an existing turn also carries its
typed `turnState` transition beside the patch, so adopting the new attempt
does not require an oversized `TurnRecord`. Every revision-advancing attempt
event must carry a patch; status-only transitions carry an empty patch rather
than relying on an implicit revision jump. Visible root takeover likewise
advances once with an exact old-to-new patch, and synthetic child attempt
history does not duplicate its full projection:

```json
{
  "version": 1,
  "baseRevision": 41,
  "targetRevision": 42,
  "operations": [
    {"op": "append_text", "path": ["content"], "value": "new suffix"},
    {"op": "append", "path": ["toolRounds"], "value": [{"name": "rg"}]}
  ]
}
```

The supported operations are `set`, `remove`, `append`, `truncate`, and
`append_text`; paths are arrays of object keys / array indexes. A consumer MUST
apply a patch only when its current `projectionRevision` equals
`baseRevision` and the envelope revision equals `targetRevision`. Any mismatch,
unknown operation, or invalid path fails closed into an authoritative turns
snapshot. The full turn row remains storage authority.

Sequence gaps, expired cursors, invalid frames, and projection-revision
mismatches converge through one new authoritative snapshot. Push and
cross-tab notifications are invalidation hints only; they never write a
projection directly. There is no attempt-scoped stream or earlier-version
turn API: Conversation Sync v3 is the sole command, snapshot, and event
surface.

The generated event URL supplies `streamClientId` and `streamGeneration` as an
atomic optional pair. New/equal generations replace an older connection for
the same owner, conversation, and page on the receiving replica; the shared
lease cap bounds overlap that lands on different replicas. Delayed generations
and streams that cannot obtain that lease terminate with HTTP 204 so a native
EventSource does not create a retry storm. Legacy URLs without the pair remain
compatible but receive only the global capacity protection.

Idempotent retry is an OpenAPI property, not a call-site convention. Only the
generated `createTurn` and `createAttempt` methods snapshot their validated
body and retry with the same `commandId`; the generated policy covers
ambiguous network failures, HTTP 502/503/504, and declared transient storage
codes. Other commands are never retried automatically.

### 2.7 Conversation header lifecycle

`contracts/conversation_lifecycle_v1.yaml` is the machine-readable authority
for delete, restore, clone, purge, and trash retention. Public HTTP exposes only
the first three:

| Action | Endpoint | Success |
|---|---|---|
| Move to recoverable trash | `DELETE /api/v1/conversations/{id}` | `{ok, recoverable, deletedAt}` |
| Restore | `POST /api/v1/conversations/{id}/restore` | `{ok, restored, rev, turnCount}` |
| Clone | `POST /api/v1/conversations/{id}/clone` with `{conversationId,title}` | `{ok, conversationId, rev, turnCount, archiveCount}` |

All three are owner-scoped receipt-backed commands. Delete is locally
optimistic but the Undo action appears only after the server commit. A failed
delete restores the local projection. Restore and clone are server-atomic and
then hydrate through Conversation Sync v3. The client must never fetch a full
transcript to prepare delete, PUT a browser snapshot to restore, or serialize a
browser message array to clone. A source with a pending/running turn returns
409 for clone.

Compaction archive inspection is owner-scoped and summary-first:

| Action | Endpoint | Success |
|---|---|---|
| List archive metadata | `GET /api/v1/conversations/{id}/compactions` | `{ok, compactions, count}` without transcript bodies |
| Read summary projection | `GET /api/v1/conversations/{id}/compactions/{archiveId}?includeMessages=false` | `{ok, archive}` |
| Read raw transcript snapshot | `GET /api/v1/conversations/{id}/compactions/{archiveId}` | `{ok, archive, messages}` |
| Download raw snapshot | `GET /api/v1/conversations/{id}/compactions/{archiveId}?download=true` | unwrapped JSON attachment `{archive,messages}` |
| Run idle manual compaction | `POST /api/v1/conversations/{id}/compact` | `{ok, archiveId, tokensBefore, tokensAfter, tokenCountKind:"estimated", msgsBefore, msgsAfter, reductionPct, summaryPreview, receipt}` |

Archive metadata uses `schemaVersion=tofu.compaction-archive/v3`, millisecond
`createdAt`, byte-valued `payloadSize`, and `tokenCountKind=estimated`. The
`model`/`taskModel` value is the task model, not an implicit claim about the
summary model. `trigger` distinguishes `working_set`, `window`, `force`,
`reactive`, and `manual` causes. Archives created before this distinction may
carry `force` for an automatic window-threshold run. `tokensAfter` is the
compaction-stage estimate; later context providers can change the next actual
provider request.

The selected archive's summary/full projection also carries a bounded
`receipt` with `schemaVersion=tofu.compaction-receipt/v1`. It records status,
strategy/implementation, continuation form, retention decisions (including a
maximum of eight durable recent-file paths), summary duration and normalized
usage, optional cache economics/evidence counts (including the selected
payback horizon/policy), and deterministic recovery measurements. The receipt
is capped at 32 KiB and never duplicates the summary
or transcript body. Archive lists expose only `hasReceipt`, `resultStatus`, and
`resultStrategy` (plus a database-truncated 240-character summary preview), not
the full receipts. Their server-side receipt scan is explicitly bounded by the
32 KiB-per-row limit and the existing 200-row default. A pre-v40 archive has an
empty receipt and reports `legacy` status.

`conversation.purge` and `conversation.trash.prune` have no ordinary HTTP
surface. Purge is maintenance/test cleanup; retention prune permanently removes
trash older than 30 days in bounded oldest-first pages.

### 2.8 Experiment capabilities and decisions

The durable experiment definition is authored in
`contracts/experiments_v1.schema.json`; the architecture and extension rules
live in [`modules/experiments.md`](modules/experiments.md).

`GET /api/v1/experiments/capabilities` returns the standard success envelope
plus `contractVersion: "tofu.experiment-plugin-catalog/v1"` and a `plugins`
array. Entries contain callback-free strategy schemas, metric units/directions,
analyzer IDs, versions, and implementation digests. The endpoint never returns
Python entry points or executable callbacks.

`GET /api/v1/cost-experiments/report?days=1..90` returns
`contractVersion: "tofu.experiment-report/v1"`, `experimentId`, `specDigest`,
operational `lifecycle`, fixed-cohort counts (`maximumAssignmentUnits`,
`observedAssignmentUnits`, `analyzedAssignmentUnits`),
`arms`, descriptive `comparison`, collection `funnel`, and a versioned
`decision`. The storage authority filters by explicit `user_id`, exact
experiment ID, and completion window before applying its row limit. `truncated`
or `invalidRows > 0` invalidates the decision; a partial report may remain
descriptively useful but is never promotion evidence.
For a versioned run with `started_at_ms`, storage and in-process filtering begin
at that server-owned start even when `days` requests a shorter display window;
`analysisStartVerified=false` or `analysisSealVerified=false` blocks inference
for legacy runs whose complete lifecycle window cannot be proven.
Assignment-only checkpoints are retained in the cohort denominator;
`decision.blockers` includes `pending_exposures` while an earlier exposed task
has no terminal metric observation.

The decision fields have deliberately distinct meanings:

- `sampleReady`, `pricingReady`, `qualityReady`, `latencyReady`: individual
  collection gates;
- `analysisClosed` / `fixedHorizonReached`: the ID is irreversibly sealed and
  its precommitted first assignment-unit cohort is complete;
- `ready` / `decision.decisionEligible`: all required valid evidence exists;
- `comparison.pointEstimateOptimizedCheaper`: descriptive sign of the frozen
  analysis cohort only;
- `comparison.allObservedCostPerConversationDeltaPct`: diagnostic over all
  observed outcomes, never a decision input;
- `promotionEligible`: every statistical and guardrail gate passed;
- `comparison.optimizedIsCheaper`: compatibility alias for
  `promotionEligible`, never the raw point estimate;
- `decision.blockers`: stable machine-readable reasons for a denied decision.

Clients must default deny on missing/unknown decision fields. In particular,
they must not infer promotion from a negative percentage delta.

---

### 2.9 Normalized model catalog

The machine-readable authority is
[`contracts/model_catalog_v1.schema.json`](../contracts/model_catalog_v1.schema.json);
the runtime owner is `lib/model_catalog/`. The catalog projects provider rows
into logical models, per-provider offerings, and score routes:
`contract_version: "tofu.model-catalog/v1"`, plus `revision`, `models`,
`offerings`, and `routes` maps.

`GET /api/v1/model-catalog` returns the success envelope with
`contract_version`, `revision`, `catalog` (the full normalized document),
`providers` (a provider-id → safe metadata object map containing only
`id`/display labels/brand/protocol/enabled; connection URLs, keys and other
credentials are never projected), and `health` (best-effort, provider-scoped
runtime health keyed by offering id). When no
`server_config.json.model_catalog` exists yet, the read migrates from the
legacy provider snapshot **in memory** and never writes.

`PUT /api/v1/model-catalog` accepts `{expected_revision, catalog}` and
commits atomically:

* `expected_revision` is an integer compare-and-swap. A stale value returns
  **409**; on success the server increments `revision` by one.
* The body is bounded and validated (`normalize_catalog`); dangling offering
  ids, route/offering mismatches, key/body identity mismatches, and unknown
  providers are **400**.
* `enabled` is an aggregate gate: a logical-model toggle cascades to all its
  offerings, an offering toggle is applied as-is, and every logical
  `enabled` is recomputed from its offerings so a persisted catalog is
  always internally consistent.
* On commit the catalog is persisted as `server_config.json.model_catalog`
  and provider models are projected back onto the provider shells as the
  derived compatibility snapshot consumed by the dispatcher, then the
  dispatcher is reset.

`server_config.json.model_catalog` is the authored authority when present;
`providers[].models` remains a derived projection. The legacy
`POST /api/v1/server-config` providers path still compiles a fresh catalog
revision after merging server-owned providers (honoring the
`_catalog_revision` stale-save marker) and then stores the derived provider
rows.

---

## 3. Request parsing

* JSON body → `parse_body()` (sync handlers) / `await async_parse_body()`
  (async handlers). Never raw `request.get_json()` — the shim semantics and
  the empty-body→`{}` contract live in one place.
* Fields → `require_str` / `optional_int` / `require_list` / … — these raise
  `BadRequest(field=…)`, which `@safe_route` and the global boundary convert
  to a 400 carrying the field name. Hand-rolled `"x is required"` returns are
  a violation.
* Query-string **path** args → `decode_proxy_path_arg('path')` (the VS Code
  proxy double-encodes; this seam undoes it, bounded).

---

## 4. Carve-out registry

Some endpoints are **deliberately outside the envelope**. A carve-out is
legal only if it appears here AND in `tests/test_api_contract_drift.py`'s
`CARVE_OUT_FILES` / bare-payload list with a reason.

| What | Where | Why |
|---|---|---|
| OpenAI compat | `routes/compat_openai.py` | Emulates the OpenAI wire protocol; an `ok` key corrupts protocol fidelity for third-party SDKs |
| Anthropic compat | `routes/compat_anthropic.py` | Same — Anthropic protocol shape |
| Device bridges | `routes/browser.py`, `routes/_bridge_caller.py`, `routes/desktop.py` | Owner-scoped long-poll protocols parsed by external browser/desktop devices; poll shape is locked outside this repo |
| SSE streams | chat stream, agent-run, translate stream, compat streams | `text/event-stream` framing; use `sse_response()` for the canonical headers, never hand-set them |
| Binary / raw payloads | artifact raw/view/export, paper PDF serving, image bytes, podcast audio | Typed bytes with `Content-Disposition` / Range; JSON envelope impossible |
| Multipart uploads | `/api/images/upload`, `/api/paper/upload`, `/api/pdf/parse`, … | FormData in, but the *response* still follows the envelope |
| Bare-array legacy payloads | ALL known instances migrated 2026-08-01 (orchestrations / conversations list / providers-templates / chat active / translate poll-batch / folders ×2 / conv search / chat queue) | Enveloping an array (`{ok, items}`) changes the top-level type — never additive. The retirement path is the **coordinated front+back migration**: backend `api_ok({'items': …})` + the `Api.<domain>` seam unwraps `.items` with an `Array.isArray(d)` fallback for rolling-deploy skew. Unwrap semantics follow the CONSUMER: `|| []` for list UIs (orchestrations/folders/queue/search), **null-preserving** for probes that must distinguish “zero results” from “probe failed” (chat active, translate poll-batch), caller-side unwrap for Response-returning seams (config templates, chat activeResponse). Sites that genuinely cannot migrate register in the drift suite's `CARVE_OUT_SITES` with a reason — never a silent baseline remainder. New endpoints MUST wrap arrays in `api_ok({'items': …})` |

Adding a carve-out requires: (1) a row in this table, (2) an entry in the
drift test with the same reason, (3) a commit message explaining why the
envelope is impossible (not merely inconvenient).

The bridge-secret transport namespace is also deliberately unversioned. In
addition to `/api/browser/poll`, `/api/browser/download` and
`/api/desktop/poll`, it contains the path-parameterized browser file-transfer
start/chunk/complete/abort routes under `/api/browser/file-transfers/`. Those
four routes use the normal JSON success/error envelope (the chunk request body
is raw bytes); control envelopes are capped at 16 KiB and chunks at 256 KiB
before parsing/buffering. They stay beside poll because the extension, not a
public API consumer, is the only caller. Every non-OPTIONS request still passes
the real owner-scoped bridge credential gate.
`/api/browser/poll` additionally passes a bounded credential-digest gate before
storage authentication and an owner-aware gate afterward. Normal long-polls
retain the existing 200 wire; pressure returns 429 with `Retry-After`, oversized
poll/result frames return 413, and protocol rejection remains 426 with
`Retry-After`. One device retry supersedes its prior waiter without a conflict
response, so proxy overlap is invisible to healthy clients. A current extension
uses string device/result IDs of at most 128 characters, a capability array no
larger than the canonical known-capability set with 64-character string items,
and bounded version/profile strings; malformed amplification shapes return 400.
It mirrors its protocol in `X-Browser-Protocol-Version` solely to clear an older
binary's credential cooldown after an in-place upgrade; the authenticated JSON
frame remains authoritative and every admission/authentication check still runs.
Completed results replay idempotently after transport failure; a 413 first
bisects a multi-result batch and only a still-oversized singleton becomes an
explicit command error.

---

## 5. Enforcement (the ratchets)

| Guard | What it rejects |
|---|---|
| `tests/test_frontend_api_isolation.py` | Any backend `fetch` outside `frontend/src/api/transport.ts` (including variable-URL bypasses) |
| `tests/test_api_contract_drift.py` | Any ad-hoc `jsonify(` in `routes/**` outside the carve-out registry; per-file count may only decrease; stale baselines must be tightened in the same commit |
| `tests/test_api_contract_*_parity.py` (21 batch suites) | Per-batch wire parity (legacy keys byte-identical; additions only +ok/+error/+request_id) + shipped-source tripwires + front/back coordination anchors for every bare-array migration |
| `tests/test_api_response_route_conversions.py` | The 22 already-converted error sites stay converted (shipped-source tripwire + wire parity) |
| `tests/test_api_response_safe_route_rollout.py` | `@safe_route` rollout state; documents the handlers that must NOT be decorated (side-effecting except blocks) |
| `tests/test_request_parser.py` | `BadRequest` → 400 mapping, typed extractors |

`@safe_route` note: the framework boundary (`server.py`) already converts
uncaught exceptions on `/api/*` to the JSON 500 envelope, so `@safe_route` is
NOT required for correctness. Adopt it on handlers that currently hand-roll a
pure `except Exception → api_internal_error(e)` block **only when the block
has no side effects and no distinct `context=` string** (the rollout suite
pins the gate).

---

## 6. Adding a new endpoint — checklist

**Backend**
1. Route lives in `routes/api_v1/<domain>.py` (the canonical surface; legacy
   `routes/*.py` is maintained, not extended, except UI-only conveniences).
2. `parse_body()` + `require_*`/`optional_*` for input; never `get_json` digs.
3. Return via `api_ok` / `api_created` / `api_payload` / `api_error`
   family; raise `BadRequest` for validation. No bare `jsonify`, no
   hand-rolled 500.
4. Arrays wrapped: `api_ok({'items': …})`, never a bare top-level array.
5. `@api_meta(...)` so `GET /api/openapi.json` stays truthful.
6. Streaming → `sse_response(gen, …)`; binary → document the carve-out (§4).

**Frontend**
1. Add a method on the retained `Api.<domain>` registry in
   `frontend/src/runtime/app-runtime.js`, or update the machine-readable
   contract and regenerate its typed client. Never `fetch('/api/…')` outside
   `frontend/src/api/transport.ts`.
2. `onError:'null'` only for genuinely best-effort reads; mutations and
   "user must see the reason" calls must throw `ApiError`.
3. Read typed failures from `err.envelope.kind` (e.g. branch on `409` /
   `overloaded`), not string matching on messages.

---

## 7. Migration workflow (shrinking the legacy baseline)

The drift ratchet freezes today's ad-hoc `jsonify` count per file and only
allows it to shrink. To convert a file:

1. Classify each site: envelope-able (dict payloads, `api_ok(data)` is
   additive) vs bare-array/binary/protocol (carve-out, document it).
   A lib-result passthrough (`return jsonify(result), <status>` where
   `result` came from a `lib.*` call) converts to `api_payload(result,
   <status>)`, NEVER `api_error(result, …)` — the latter nests the whole
   result under `error` and breaks every consumer.
2. Convert; add a parity test in the style of
   `tests/test_api_response_route_conversions.py` — legacy keys must survive
   byte-identical; the ONLY additions allowed are `ok` (always) and
   `request_id`/`error` (error statuses).
3. Update `tests/test_api_contract_drift.py`'s `BASELINE` in the SAME commit
   (the stale-baseline test forces this).
4. Run the ring: `test_api_response*.py`, `test_api_contract_*.py`,
   `test_request_parser.py`, `test_frontend_api_isolation.py`.

---

## 8. Why this shape (scale argument)

Centralized maintenance at ultra-large scale means: **one place to change a
cross-cutting concern, total confidence the change took.** Every cross-cutting
concern here has exactly one seam:

* Change error shape/policy → edit `lib/api_response.py` (446+ sites flow through).
* Change parsing tolerance → edit `lib/request_parser.py`.
* Add a correlation header → edit `api.js::request()` (one chokepoint; the
  `X-Request-ID` rollout touched zero call sites).
* Change overload policy → edit the `server.py` boundary.

The ratchets are what keep it true at 650 handlers and growing: they make the
wrong pattern uncommittable, so review attention is never spent re-litigating
style — only genuine carve-outs need judgment, and those are forced to leave
a written reason.

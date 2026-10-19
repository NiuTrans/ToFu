# API Contract — The Frontend ↔ Backend Interface Constitution

> **This is the single source of truth for how the frontend and backend talk.**
> Consumer-facing endpoint documentation lives in [`HEADLESS_API.md`](HEADLESS_API.md)
> (auth, scopes, per-endpoint reference). THIS document defines the *engineering
> contract*: the response envelope, the error taxonomy, the carve-outs, the
> guard tests that enforce the contract, and the checklist for adding an
> endpoint. When the two disagree about an endpoint's *shape*, this file wins.

## 0. API v4 release boundary

`TurnProjection.usageComplete` is optional. Explicit `false` means reported
usage is a paid lower bound and an earlier request remains unreceipted; absence
does not certify completeness. Continue, checkpoint resume and guidance restore
this fact from the accepted projection before model admission. An unreceipted
restored request refuses a new model dispatch even without a token cap; request
configuration cannot overwrite this authority or turn unknown consumption into
zero. Existing uncapped live-task behavior remains unchanged.

The browser transport normalizes v4 success JSON by the addressed URL, not a
global flag. Both `parse: 'json'` and `parse: 'response'` consumers see
the domain payload. Response mode preserves the native status, headers, body
stream and byte readers; only `.json()` (including clones) unwraps one
`{data, meta}` layer lazily, before task-affinity projection. Error/problem
bodies, v1/v3 responses, external non-v4 JSON and OpenAPI remain unchanged.
For parsed JSON/text/blob responses, request timeouts and caller cancellation
remain active until the entire body is consumed, including HTTP error bodies.
Settlement always releases the timer and forwarding listener. Explicit safe GET coalescing keys include
body parsing, credentials, headers, task affinity, timeout and declared RPC
method/params. Different deadlines or RPC queries cannot share one flight;
retired browser governor hints do not split equivalent reads. `parse: none`
cancels an unused successful body without waiting on its peer. Raw Response mode
transfers body ownership at headers; its caller must own body-read cancellation
and deadlines (as the conversation catalog loader does).
This boundary never eagerly reads an SSE or download stream. Verify with
`tests/api/test_api_v4_response_json.py` and the catalog browser journey.

The browser transport and public Python/TypeScript SDKs project a recognized
successful v4 `{data, meta}` envelope back to the historical caller shape.
For object `data`, the projection supplies `ok: true` when absent and preserves
an explicit `ok` value. It unwraps exactly once; nested domain `data`/`meta`
fields remain intact. This is client compatibility behavior, not a wire field.
Non-success HTTP bodies are preserved for typed error handling.

`contracts/api_v4.yaml` is the machine-readable authority for the v4 wire
contract. Changes flow one way: edit the OpenAPI 3.1 source, then run
`python3 scripts/gen_api_v4_contract.py`. The generated outputs are dependency-
free server release constants, the strict server `TypedDict`/Pydantic boundary,
plus TypeScript, desktop Python, and Kotlin client DTOs. Ordinary boot imports
only the constants; validators and the OpenAPI document load on a v4 response.
`make contracts-check` rejects drift.

The declared generic product boundary envelopes every successful JSON value,
including arrays, null, and logical failures. Only a top-level `ok: true`
transport sentinel is removed; `ok: false` and nested domain fields survive.
SSE, binary downloads, empty bodies and native protocols retain their wire.

The always-on v4 surface carries `GET /api/v4/meta` and
`GET /api/v4/openapi.json`. With the contract at `x-tofu-release-stage: cutover`
(`activeByDefault: true`), the entire product surface is served under `/api/v4`,
each JSON result wrapped in the v4 envelope (see `docs/contracts/API_V4_MIGRATION.md`). `meta`
uses the v4 success envelope (`data.{apiMajor,schemaVersion,serverBuild,minDesktopBuild,minAndroidBuild}`
plus `meta.{requestId,serverTimeMs}`; exact shape owned by the OpenAPI schema).
Generic v4 errors use `application/problem+json`. OpenAPI remains a raw
document. Explicit `NativeContractBlueprint` domain protocols keep their own
success/error bodies; Conversation Sync declares its generated contract ID
and preserves commit/conflict/retry receipts. The matched blueprint determines
this boundary; a similarly prefixed ordinary route has no exemption. The
retired-major gate still returns an RFC 7807 upgrade problem before dispatch.

The canonical contract declares `x-tofu-release-stage: cutover` and the release
assembly defaults the app-local `ACTIVE_API_MAJOR` latch to integer `4`
(`API_V4_ENABLED` on); a per-process `TOFU_API_MAJOR=1` opt-out keeps the legacy
v1 test app serving during the regression window. Requests under `/api/v1` and
`/api/v3` return HTTP 426 with `upgradeUrl: "/api/v4/meta"` before body parsing,
authentication, or storage work.

Client compatibility is decided against the `minDesktopBuild` or
`minAndroidBuild` returned by that server response, never only against the
minimum compiled into the client. The generated helpers reject malformed dotted
desktop builds, a wrong API major, and a live minimum above the running client.

## 1. The five-layer map

Every frontend → backend call passes through five layers. Each layer has ONE
canonical implementation; drift from it is caught by a guard test (§5).

| # | Layer | Canonical implementation | Guard |
|---|-------|--------------------------|-------|
| 1 | Frontend endpoint seam | typed domain clients in `frontend/src/api/` and `window.Api` in retained `frontend/src/runtime/sections/api.js` | `tests/frontend/test_frontend_api_contract.py` |
| 2 | Transport + correlation | `frontend/src/api/transport.ts`; it mints `X-Request-ID`, which `server.py::_assign_req_id_and_log` honours and echoes | `tests/frontend/test_frontend_api_transport_vite.py` + server request-log |
| 3 | Request parsing | `lib/request_parser.py` (`parse_body` / `require_str` / `optional_int` / …; raises `BadRequest` → auto-400) | `tests/diagnostics/test_request_parser.py` |
| 4 | Response envelope | `lib/api_response.py` (`api_ok` / `api_error` / `api_not_found` / …, `sse_response`) | `tests/api/test_api_contract_drift.py` + `tests/api/test_api_response.py` |
| 5 | Framework error boundary | `server.py` `@app.errorhandler(404/413/405/500/Exception)` — API paths always get the JSON envelope, never an HTML error page | `tests/api/test_api_response.py` |

The route/verb guard inventories retained and typed JS/TypeScript with
`scripts/frontend_api_inventory.mjs`: aliases, bound HTTP forwarders, path
helpers, concatenations and generated catalogues. It matches production
registration through Werkzeug without storage or workers. Invalid syntax,
missing/retired paths and wrong methods fail. New and uncommitted clients are
included; deleted and composed sources are excluded. Caller/server-owned whole
URLs are `deferred`; pure transport `forwarders` are reported separately.
Neither is static coverage: their owners need behavioral checks. Inspect the
read-only JSON with `node scripts/frontend_api_inventory.mjs`.

`GET /api/openapi.json` describes the actual mount: release-envelope projection,
canonical bootstrap DTOs, native generated schemas and WebSocket upgrades.
Construction failures return a typed error rather than an empty catalog. `contracts/api_endpoint_index_v1.json` includes every first-party
registered HTTP/WebSocket operation, its source owner and declared metadata;
unknown authorization metadata remains unknown. Regenerate with
`python3 scripts/gen_api_endpoint_index.py`; `--check` rejects drift.

**No layer may be bypassed.** A raw `fetch('/api/...')` outside the typed
transport, a hand-rolled `request.get_json()` dig, or a bare `return jsonify({...})` in a
route is a contract violation — the ratchets in §5 exist to reject it.

### Conversation sidebar search

[`contracts/conversation_search_v2.json`](../../contracts/conversation_search_v2.json) owns `GET /api/v4/conversations/search?q=...&cursor=...`: bounded `items`, `nextCursor`, `coverage` (`ready`, `indexing`, `partial`) and non-sensitive `coverageReasons`. Hits contain match field/role/snippet and standalone metadata, never transcripts. Owner filtering precedes matching and metadata loading; foreign/deleted hits are withheld.
Queries are bounded to 512 characters/32 terms and ordinary pages to 50 hits. Opaque cursors bind query/owner; empty pages with cursors are not exhausted searches. Storage failures remain errors, not empty successes. Timing logs contain length/count/elapsed time, never search text.
`GET /api/v4/conversations/<conv_id>/diagnostic` is owner-scoped; [conversation diagnostics](CONVERSATION_DIAGNOSTICS.md) owns bounded reads, source fences, typed absence, upgrade errors and guards.

### Database path settings


Panel entry reads a lightweight runtime snapshot: it must not hash or execute an engine binary, fingerprint source, recursively inspect authority contents, or perform fsync/WAL capability probes. Full launch verification remains mandatory at startup/switch admission, not settings entry. Filesystem summaries use read-only mount/free-space evidence; size is a cached background estimate. Candidate discovery is requested only by an explicit placement/scan action, independently of status rendering. A ready current backend is available; unknown next-launch readiness is not reported as “not installed”. `engine.engines.*.available` and `hasData` may be `null` when unverified; `availabilityState` reports `running`, `supported` or `unknown`, `launchVerification` is `not-checked`, and `filesystem.evidence` is `read-only`. `GET database/paths` returns `inProgress: true` with any cached candidates when another explicit scan owns the single-flight admission; it does not queue another scan.
Database placement controls use per-section drafts and explicit Apply/Cancel. A toggle or candidate selection alone sends no write. Apply saves immediately within that section; closing discards unsaved drafts, while the enclosing settings Save/Cancel controls do not undo already applied database settings. Pending restart and fenced native exit constraints remain visible; full path diagnostics are disclosed on demand with copy controls. Entry/status refresh is single-flight within the active mount; teardown fences prior reads, probes, writes and confirmations. Failed refresh invalidates active claims instead of silently leaving old controls editable.

`routes/api_v1/database_panel.py` owns placement ([storage contract](../STORAGE.md#settings-database-panel)); route definitions below use `/api/v1`, while the effective prefix follows the mounted major version.
`GET /api/v1/database/status` reports optional `fastpath` for both SQLite and TofuDB, independently of required TofuDB `searchProjection` (`null` otherwise). Fast Path fields `enabled`, `mode`, `directory` describe the next-launch configuration, not runtime activation. `kind` identifies `native`, `sqlite`, or `unavailable`; `active` is a runtime witness (`null` when unknown), `activeDirectory` is the actual local authority/front, `restartRequired` compares configuration with the launch witness, and `locked`/`lockReason` prevent unsafe native disable/relocation. `authorityPath` and `shadowPath` identify the current serving authority and recovery shadow. Never infer activation from a saved checkbox or directory.
`engine.nativeFastpath` remains the actual daemon launch witness (or `null`); an active native front is reported as `engine.authorityPath`, while `dataRoot` remains the durable deployment root. Projection fields: `required`, `directory` (active launch witness, empty if unavailable), `configuredDirectory` (custom base, empty for default), `defaultDirectory`, `pendingDirectory` (next launch), `restartRequired`. Custom bases are namespaced by canonical authority path. The panel renders loading/error states before enabling any configuration action; Fast Path and search use independent controls and pickers.
`POST /api/v1/database/paths` accepts exactly one object: `{"fastPath":{"enabled":true,"directory":"/absolute/base"}}` for the selected SQLite or TofuDB backend, or TofuDB `{"searchProjection":{"directory":"/absolute/base"}}`. Native activation requires an explicitly selected existing writable persistent local disk base; memory/ephemeral/network paths and incompatible embedded/Normal-durability deployments are rejected. The native engine still owns launch-time capacity, measured fsync admission, lineage and restore validation. Saving only persists next-launch configuration (`TOFU_DB_FASTPATH_DIR` for native, `TOFU_STORAGE_FASTPATH*` for SQLite); it never copies or moves live authority data. Once native Fast Path is active or fenced by a retained `FASTPATH` marker, disabling or changing its base returns `409 fastpath_locked`: use stopped-server `fastpath-export` and explicitly select the verified exported authority instead. Empty projection directory restores `data/projections/tofudb-search`, not automatic fastest-disk selection; search stays enabled.
Saves return `ok`, `restartRequired` and updated state. No restart, migration or deletion is implicit. Restart requires explicit positive confirmation; a missing dialog bridge is not consent.
Non-object, mixed, malformed, relative, unwritable, authority-overlapping or backend-incompatible projection requests fail without changing the active runtime. Panel preferences cannot silently supersede `TOFUDB_SEARCH_PROJECTION_DIR`. Validation uses disposable write probes, never authority-file contents.


## 2. Handler payloads projected by the HTTP boundary

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

**Result passthrough:** when a lib-layer function already returned `{ok, error, ...}` and the route only chooses the HTTP status, use `api_payload(result, status)` — it preserves the result's top-level shape (`api_error` would WRONGLY nest it under a single `error` key), keeps a present `ok`, defaults `ok = status < 400` when absent, and attaches `request_id` on 4xx/5xx. This is the idiom behind the Project Brain routes' `if not result.get('ok'): return api_payload(result, 409|400)`.

`error` is a **string** for legacy-compatible sites (the frontend reads
`data.error` as a string at >80 places) and a complete **envelope dict** when
the route passes one or when the boundary converts an exception via
`lib/error_envelope.from_exception`. The frontend transport normalizes legacy
strings and older partial `{kind, message}` objects into the complete shape at
its boundary before exposing `ApiError.envelope`; partial objects are not a
valid persistence or new-route contract. Missing `detail` stays empty; the
browser never substitutes a JSON serialization for occurrence text. New code
SHOULD prefer envelopes for
errors the user must *act* on (quota, conflict, approval-needed) and MAY keep
strings for simple validation messages.

HTTP 500 is a strict disclosure boundary. `api_internal_error` records the
actual exception type, message, chain, traceback, route context, and correlated
`request_id` in backend diagnostics. The public response carries the stable
`internal` / `internal_error` identity, request ID, and the exception **class
name** when an exception exists. The class name is a code identifier; exception
text is never copied into the public response because it may contain
credentials, SQL, provider bodies, or filesystem paths. A known 500 failure
may provide an explicitly reviewed `public_500_detail` through `api_error`;
API v4 never infers public detail from arbitrary error envelopes. A caller that
has already logged the same exception may set `log_traceback=False` to avoid a
duplicate record, but it does not weaken response redaction.

### Recovery with unconfirmed model usage

Unconfirmed model usage retains `execution_token_usage_unknown` and `retryable: false`
through native settlement, persistence and the localized error card. The original
finite-budget execution remains closed; missing usage is never zero or settled by
presentation. **Problem:** asking a nontechnical user to audit provider records
leaves no usable recovery. **Decision:** the card offers a read-only safety check
and explicit confirmation of one new finite budget via the [conversation recovery
command](CONVERSATION_SYNC_V3.md#unconfirmed-usage-forward-recovery). It preserves
the original Turn, output and known usage; it does not replay its model request or
resume its checkpoint, Goal or Flow. Unconfirmed tool/background/delegated effects
remain blocked. **Alternatives rejected:** repeatedly Continue/Regenerate does
not supply missing evidence; guessing old usage or silently renewing its budget
weakens accounting. Old generic errors are not reclassified from raw text.

### Local prompt-admission diagnostics

Problem: local tool-schema refusal looked like provider overflow after compaction.
Decision: `PromptTooLongError(origin='local', reason=...)` retains the existing
kind and non-retryable policy, with `providerReached: false` and optional
content-free token counts. The constructor in `lib/llm/llm_errors.py` owns reason
validation; `lib/error_envelope/_schema.py` owns the additive wire fields.
Counts are nonnegative JS-safe integers, never booleans or arbitrary text.
Persistence preserves the evidence; the caller's model/context remain intact.
Local detail/raw contain fixed reason/count metadata, never exception bodies.
Custom bilingual title/hint omit generic translation keys. Schema-floor advice
says no model request was sent, suggests budget/tool checks, and neither claims
compaction occurred nor recommends deleting history. Missing summary routes
get route-specific guidance. Legacy provider errors retain their behavior.
Alternatives rejected: message-text origin inference is unverified; a new error
kind or frontend classifier duplicates the existing server override boundary.

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
response header, and `api_error` also embeds it as `request_id` in the body,
joining frontend console, `logs/app.log`, and `logs/error.log` in one grep.

The browser keeps failure channels distinct. `ApiError.code` is always a
machine identifier (or numeric RPC code): transport failures use `timeout`,
`aborted`, `network`, or `parse`; v4 HTTP failures use the RFC 7807 `code`;
v1/v3 failures use an explicit response code, envelope `kind`, or a stable
status-derived fallback. A valid `application/problem+json` body is retained as
`ApiError.problem` and is never coerced into the task-level
`ApiError.envelope`.

### 2.5 Identity projection

The machine authority is
[`contracts/identity_v1.yaml`](../../contracts/identity_v1.yaml); the development
map is [IDENTITY.md](../IDENTITY.md).

An authenticated request exposes two deliberately different identifiers:

- `account_user_id`: opaque account subject for login, administration, and
  billing;
- `owner_user_id`: positive integer used by every owned domain resource.

`GET /api/v4/users/me` returns the account at `user.id` and the repository
owner at `ownerId` (also `user.owner_id`). `GET /api/v4/keys/whoami` returns
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
`GET /api/v4/conversations/{conversationId}/sync` and then follows
`GET /api/v4/conversations/{conversationId}/events`. The stream is ordered for
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

The internal `turn.event.record` command uses this primitive too: the Sidecar
accepts only an exact locked base and one-revision advance, applies it to the
canonical stable projection, and reuses the validated patch for the public
event unless terminal trace merging changes its target. The application sets
the private `projection_segments_stable: true` evidence only after the target
has crossed the shared stable-segment normalizer. Missing evidence is safe for
older producers: the Sidecar marks that revision for one normalization before
the next structural patch. Non-boolean evidence fails the command, and this
private field never enters replay. `event_payload` never carries another full
projection.

This public contract is independent of the live physical projection layout.
The Sidecar may reconstruct a live revision from one owner-fenced checkpoint
and at most 64 / 1 MiB of the exact durable `projectionPatch` payloads already
stored for replay. A bound crossing writes a new checkpoint; terminal,
recovery, edit, branch, compaction, trash, and clone boundaries expose a fully
materialized projection and remove any live head dependency. The checkpoint
table, a private `{}` placeholder in the hot Turn row, and head counters never
enter snapshot, delta, replay, or HTTP DTOs. A missing checkpoint, patch gap,
duplicate revision, bad base, or over-budget head fails closed as storage
integrity rather than returning a partial Turn.

The public `attempt.event` ConversationChange is likewise independent of its
physical replay layout; no reference key enters JSON, SSE, cursors, or generated
DTOs.

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

The generated snapshot request also fixes `artifactHint=has-any`. This opts
into an optional `hasArtifacts` boolean computed by one owner-scoped, bounded
existence query in the snapshot transaction; it never embeds artifact rows.
`false` suppresses the otherwise redundant artifact-list request. `true` and
an omitted field preserve that request, so a new browser remains compatible
with an older server/sidecar. Clients that omit the selector retain the old
response shape, and differently selected shapes never share a snapshot flight.

The generated
`POST /api/v4/conversations/{conversationId}/turns/{turnId}/perception`
command records one owner-scoped, content-free browser timing receipt for the
attempt. `RecordPerceptionRequest` is closed and bounded: it admits only phase
or terminal paint and transport degrade/recover clocks/labels, never transcript
content or arbitrary diagnostics. `observationId` is the stable retry identity;
the application hashes owner + attempt + observation into the command identity,
while the attempt document itself enforces semantic idempotency after a lost ACK.
The command updates only bounded `storage_generation_attempts.timing_trace_json`
under the terminal-settlement lock: it does not rewrite the Turn, advance a
conversation revision, emit a sync change, or allocate a per-observation command
receipt.
The browser owns a bounded retry queue because this diagnostic command is not
part of the generated foreground command retry policy. See
`TURN_TRACE_CONTRACT.md`.

Request Inspector discovers retained evidence through `GET /api/v4/tasks/by-conv/{conversationId}`. New turn-native rows come from the owner-filtered, metadata-only `turn.timing_trace.list` operation before any limit; its cursor query never selects trace JSON or message content and returns at most 100 attempts plus `hasMore`. Durable `task_results` remains the fallback for legacy/non-attempt tasks. Task detail is then read through `GET /api/v4/tasks/{taskId}/trace`, where a matching attempt is sufficient even after hot task-registry and event-retention expiry. Private task-result field-codec envelopes never cross this API: full legacy reads hydrate the original value, while compact replay may decode only the metadata/error or explicitly requested terminal fields it returns. `GET /api/v4/tasks/{taskId}/requests` folds ONE run's request rounds; a delegating run (Goal mode workers run as swarm agents) merges each `{taskId}#agent:{agentId}` child's rounds into the parent list. Merged rows carry `sourceTaskId`, and child request-list plus per-round payload/state/raw-archive reads MUST address that child task id — the parent's own event log holds no snapshots for rounds it delegated. Every inspector child route first verifies the authenticated owner against the durable parent and then verifies the exact child against persisted structural snapshot metadata; a string-shaped or foreign child ID is 404. Child merging is bounded to 32 streams per fold; if more valid children exist the response explicitly reports `coverage:partial` with `coverageReason:child-limit` rather than presenting the truncated list as complete. A run whose flow markers exist but whose round list is empty reports `coverage:full` (nothing ambiguous to disclose); `flow-untagged` requires actual untagged rows. The per-round endpoint matches the exact `(taskId, roundNum, turn, kind)` address. It restores one proxy-added percent-encoding layer only for minted `<phase>:<32-hex-token>` turns and `parent#agent:child` inspector streams; it never drops the turn filter to guess a different attempt. A confirmed absent or expired snapshot returns 404; an event-store read failure returns retryable 503. The browser must keep those outcomes distinct.

The per-round payload may include `rawArchives` metadata for provider-bound
request/response evidence. Bodies remain lazy: consumers read one bounded
window through
`GET /api/v4/tasks/{taskId}/raw-archives/{archiveId}/{request|response}` with
`offset` and `limit` (maximum 1 MiB). Authorization checks both the task and the
archive owner; foreign or deleted evidence is 404. Quota/secret/transport
truncation is explicit in metadata, never presented as a complete raw body.

The same generated contract owns
`GET /api/v4/conversations/{conversationId}/turns/{turnId}/images/{imageIndex}`.
It is a binary carve-out used only by `segmentPayload=refs` for frozen legacy
inline images. `projectionRevision` is an immutable content fence;
`ownerScope` partitions private browser caches but is not a credential. The
auth boundary supplies the current owner to one application service and an
owner-scoped repository query. Responses are PNG/JPEG/GIF/WebP bytes with
ETag/private-immutable/`nosniff`; malformed input is 400, missing or foreign
evidence 404, and a changed projection 409. JSON/SSE paths retain the standard
envelopes; durable projections and the independent-client full response do not
change.

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
atomic optional pair; delayed or unleased streams terminate with HTTP 204, and
legacy URLs without the pair remain compatible.

Only the generated `createTurn` and `createAttempt` methods retry
automatically, with the same `commandId`; other commands are never retried.

### 2.7 Conversation header lifecycle

The sidebar catalog `GET /api/v4/conversations` returns header metadata rows
(`{ok, items}` with `X-Total-Count`, conditional via ETag). A row carries
`busy: true` exactly when the owner's task registry still holds live work for
that conversation (`list_running_tasks(user_id=…)` — carrier- and
wedge-filtered, fail-closed to absent). The flag participates in the list
ETag, so a busy↔idle transition always busts the conditional request; a 304
never serves a stale busy projection. 

`contracts/conversation_lifecycle_v1.yaml` is the machine-readable authority
for delete, restore, clone, purge, and trash retention. Public HTTP exposes only
the first three:

| Action | Endpoint | Success |
|---|---|---|
| Move to recoverable trash | `DELETE /api/v4/conversations/{id}` | `{ok, recoverable, deletedAt}` |
| Restore | `POST /api/v4/conversations/{id}/restore` | `{ok, restored, rev, turnCount}` |
| Clone | `POST /api/v4/conversations/{id}/clone` with `{conversationId,title}` | `{ok, conversationId, rev, turnCount, archiveCount}` |

All three are owner-scoped receipt-backed commands. Delete is locally
optimistic but the Undo action appears only after the server commit. A failed
delete restores the local projection. Restore and clone are server-atomic and
then hydrate through Conversation Sync v3. The client must never fetch a full
transcript to prepare delete, PUT a browser snapshot to restore, or serialize a
browser message array to clone. A source with a pending/running turn returns
409 for clone.
 A successful clone stamps `settings.clonedFrom` with the
source conversation id; the sidebar settings projection exposes that key and
the conversation list renders a copy badge from it.

Compaction archive inspection is owner-scoped; metadata stays cheap and the drawer opens the selected post-compaction result:

| Action | Endpoint | Success |
|---|---|---|
| List archive metadata | `GET /api/v4/conversations/{id}/compactions` | `{ok, compactions, count}` without transcript bodies |
| Read summary projection | `GET /api/v4/conversations/{id}/compactions/{archiveId}?includeMessages=false` | `{ok, archive}` |
| Read raw transcript snapshot | `GET /api/v4/conversations/{id}/compactions/{archiveId}` | `{ok, archive, messages}` |
| Read post-compaction messages | `GET /api/v4/conversations/{id}/compactions/{archiveId}?view=post` | `{ok, archive, view:"post", postMessages, postSnapshot}` |
| Download pre-compaction snapshot | `GET /api/v4/conversations/{id}/compactions/{archiveId}?download=true` | unwrapped JSON attachment `{archive,messages}` |
| Download post-compaction result | `GET /api/v4/conversations/{id}/compactions/{archiveId}?view=post&download=true` | unwrapped JSON attachment `{archive,view:"post",postMessages,postSnapshot}`; no pre-compaction body |
| Run idle manual compaction | `POST /api/v4/conversations/{id}/compact` | `{ok, archiveId, tokensBefore, tokensAfter, tokenCountKind:"estimated", msgsBefore, msgsAfter, reductionPct, summaryPreview, receipt}` |

Manual compaction is not coordinator replacement or a harmless fresh working
view. Protected Coordinator contexts return HTTP 409 with
`coordinator_context_compaction_unsupported`; other conversations in a project
with a coordinator binding history may conservatively return
`project_context_compaction_unsupported`. The live-task 409 remains unchanged.
These refusals preserve canonical source/report Turns, Attempts, binding and
budget references; no summary purchase or partial archive/Turn mutation follows.
The common persistence adapter repeats the protection before canonical
compaction. No idle fresh-context endpoint or implicit rebind is provided.

Archive metadata uses `schemaVersion=tofu.compaction-archive/v3`, millisecond
`createdAt` and `tokenCountKind=estimated`; `payloadSize` measures stored-document bytes.
Default/pre reads return unchanged input; `view=post` returns the saved compactor result,
not subsequent runtime recomposition, admission or provider adaptation.
`postSnapshot` declares `stage:"compaction_result"`, `status:"captured"|"unavailable"`,
`includesRuntimeRecomposition:false` and `includesProviderAdaptation:false`.
Captured proves an array exists, not dispatch/acceptance or absence of surviving runtime blocks.
Missing arrays stay `null`, captured empty arrays stay `[]`; viewer/exports preserve that distinction
and selected view, never substituting pre data or reconstructing old snapshots from current state.
`model`/`taskModel` names the task model, not the summary model. `trigger` distinguishes
`working_set`, `window`, `force`, `reactive`, `manual`; older automatic runs may say `force`.
`tokensAfter` estimates the complete compaction-stage messages, not the summary or later request.

The selected archive's summary/full projection carries a bounded `receipt` governed by [`contracts/compaction_receipt_v1.schema.json`](../../contracts/compaction_receipt_v1.schema.json).
It records status, strategy/implementation, continuation, retention (at most eight recent paths), summary duration/usage/route, economics/evidence, context-budget evidence and deterministic recovery.
`summary.complete` differs from `accepted` (adopted); incomplete output is never usable text. Summary route is not `archive.model`/`taskModel`; absent legacy fields remain unknown.
`summary.route.providerId` names the selected execution pin, not necessarily the billing provider; actual attempt identities live in `summary.cost.calls`.
Selection-time `route.costBasis` is a bounded opaque diagnostic string, not the `summary.cost.costBasis` billing enum or proof of known spend.
`summary.cost.known` and `economics.pricingKnown` govern cost/payback certainty; USD values are metered API-equivalent estimates including failed/rejected attempts, not subscription charges or proven savings.
With known pricing, explicit `paybackRounds:null` means no finite estimate; an absent value remains unknown.
`contextBudget` separates recorded rendered prompt, route capacity, safety ceiling/reserve and economic target (not a limit).
It describes the recorded main-route decision snapshot, not the summary route or proof of the final provider request.
The receipt is capped at 32 KiB and never duplicates summary/transcript bodies. Lists expose only `hasReceipt`, `resultStatus`, `resultStrategy` and a 240-character summary preview, not full receipts.
Receipt scans are bounded by 32 KiB per row and the 200-row default. Pre-v40 archives have an empty receipt and `legacy` status.

`conversation.purge` and `conversation.trash.prune` have no ordinary HTTP
surface. Purge is maintenance/test cleanup; retention prune permanently removes
trash older than 30 days in bounded oldest-first pages.

### 2.8 Experiment capabilities and decisions

The durable experiment definition is authored in
`contracts/experiments_v1.schema.json`; the architecture and extension rules
live in [`modules/experiments.md`](../modules/experiments.md).

`GET /api/v4/experiments/capabilities` returns the standard success envelope
plus `contractVersion: "tofu.experiment-plugin-catalog/v1"` and a `plugins`
array. Entries contain callback-free strategy schemas, metric units/directions,
analyzer IDs, versions, and implementation digests. The endpoint never returns
Python entry points or executable callbacks.

`GET /api/v4/cost-experiments/report?days=1..90` returns `contractVersion: "tofu.experiment-report/v1"`, `experimentId`, `specDigest`, operational `lifecycle`, fixed-cohort counts (`maximumAssignmentUnits`, `observedAssignmentUnits`, `analyzedAssignmentUnits`), `arms`, descriptive `comparison`, collection `funnel`, and a versioned `decision`. The storage authority filters by explicit `user_id`, exact experiment ID, and completion window before applying its row limit. `truncated` or `invalidRows > 0` invalidates the decision; a partial report may remain descriptively useful but is never promotion evidence. Legacy task-result recovery resumes backend-neutral record cursors in pages of at most 256 source rows (eight BLOBs per SQL fetch) and has a 10,000-row source ceiling; reaching that ceiling sets `truncated` instead of restarting an unbounded scan or silently promoting from partial evidence. For a versioned run with `started_at_ms`, storage and in-process filtering begin at that server-owned start even when `days` requests a shorter display window; `analysisStartVerified=false` or `analysisSealVerified=false` blocks inference for legacy runs whose complete lifecycle window cannot be proven. Assignment-only checkpoints are retained in the cohort denominator; `decision.blockers` includes `pending_exposures` while an earlier exposed task has no terminal metric observation.

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

### 2.9 Model-routing v2

The machine-readable authority is
[`contracts/model_routing_v2.schema.json`](../../contracts/model_routing_v2.schema.json);
the runtime owner is `lib/model_routing/`. One revisioned
`tofu.model-routing/v2` aggregate belongs to each explicit owner/tenant
boundary. It contains Creators, official Models, Providers, the owner's
ProviderAccess resources, Connections, redacted Credential metadata,
Offerings, and Deployments. Route is a bounded runtime computation, never a
persisted configuration entity.

Per-endpoint request/response payloads for this route family are declared in
[`contracts/api_v1_model_routing.yaml`](../../contracts/api_v1_model_routing.yaml)
and generated into `lib/generated_api_v1_model_routing.py` (server-side
runtime validation authority) plus
`frontend/src/api/api-v1-model-routing.generated.ts` (type-level mirror).
Entity fields reference the aggregate schema above — never restate them.
Write routes validate request bodies fail-closed against the generated
schemas before any repository mutation, and
tests/api/test_api_v1_model_routing_contract.py keeps the route surface and live
success envelopes in lockstep with the contract. Regenerate with
`python3 scripts/gen_api_v1_model_routing_contract.py`; drift is rejected by
`make contracts-check`.

| Endpoint | Contract |
|---|---|
| `GET /api/v4/model-routing` | Returns `{ok, revision, model_routing}`; the document is safe for inspection and contains no credential plaintext. |
| `PUT /api/v4/model-routing` | Accepts `{expected_revision, model_routing}` and atomically replaces the aggregate. A stale revision is `409 model_routing_revision_conflict`. |
| `GET/POST /api/v4/providers` | Lists or creates ProviderAccess bundles inside the same aggregate. |
| `GET /api/v4/providers/templates` | Lists secret-free onboarding recipes as `{ok, items}`. Recipes are setup hints, not routing authority. |
| `POST /api/v4/providers/templates/compile` | Accepts `{template_key, selected_model_ids?}` and returns a secret-free v2 ProviderAccess bundle for client-side review/staging; it does not persist. |
| `POST /api/v4/providers/probe` | Authenticated discovery that returns transport facts plus a secret-free v2 ProviderAccess draft. It does not persist or echo credential plaintext. |
| `GET/PATCH/DELETE /api/v4/providers/{provider_id}` | Reads, CAS-replaces, or deletes one Provider and its owner access resources. |
| `PUT /api/v4/model-routing/credentials/{credential_id}/secret` | Replaces one encrypted credential secret outside the aggregate and advances its revision. |
| `POST /api/v4/model-routing/migration/plan` | Produces a redacted, non-writing import plan and entity counts. |
| `POST /api/v4/model-routing/migration/commit` | Re-plans, validates, stores encrypted secrets, and commits once; failure keeps the old authority inactive and stores a recovery receipt. |

A full-document or Provider bundle write rejects `providers[].models`,
`aliases`, `request_ids`, configured `routes`, plaintext credentials,
dangling references, duplicate entity identities, and a provider-scoped wire
ID that maps to more than one Deployment. A ProviderAccess aggregate and its
Connections, Credentials, Offerings, and Deployments change under one revision
CAS. Secret writes are independently encrypted and owner-scoped; responses
return only opaque references and bounded key hints.

Native chat and agent requests select one of two structured forms:

```json
{"model":{"creator_id":"openai","model_id":"gpt-x"},
 "routing":{"preferred_provider_id":"provider-a"}}
```

```json
{"model":{"provider_id":"provider-a","offering_id":"pending-offering"}}
```

The first form selects an official Model and may prefer a Provider. Preference
does not hard-lock a Connection or Deployment. The second form is only for a
provider-scoped pending identity; it cannot fail over to another Provider and
cannot carry a conflicting Provider preference. Routing may also specify
required capabilities, context, protocol/cache affinity, and an explicit price
budget. Those values are hard eligibility gates.

OpenAI/Anthropic compatibility requests retain their standard string `model`.
Tofu extensions are `tofu.creator_id` and
`tofu.preferred_provider_id`; the top-level
`tofu_creator_id`/`tofu_preferred_provider_id` spellings are equivalent.
When a string is not unique, the API returns
`model_selector_ambiguous` plus structured candidates. Provider-scoped wire
ID lookup occurs only when a Provider preference is present. A string
`model@provider` is always rejected as
`legacy_model_selector_removed`.

Candidate compilation first filters ProviderAccess/Connection/Credential/
Offering enablement, Credential authorization/quota, Offering identity/stale/
capability/context/price state, Connection protocol and scoped health.
Deployment probe evidence is diagnostic; request names have no enablement state.
It orders eligible candidates by operator priority, health, cache affinity,
connection/deployment priority, latency, and Offering price, preferring a
healthy preferred Provider; once every route for the selected official Model
fails it may choose the highest-quality compatible Model under the same policy.

Health failures apply only at their factual scope: a missing wire route affects
the Deployment, network failure the Connection, 401/402 the Credential, and 403
the Credential×Deployment authorization. Configured `enabled` state is never
rewritten from transient health. A directory miss marks the Offering stale without deleting it.

Every new turn stores a bounded, redacted `RouteSnapshot`: requested Model,
Provider preference, actual ProviderAccess/Offering/Deployment/Connection,
credential metadata, wire ID, and transition reasons. Cross-provider and
cross-model transitions appear in the turn activity timeline without changing
the conversation's saved preference. Reads synthesize a legacy snapshot for
old turns and never rewrite them.

Image generation, transcription/audio-chat, text-to-speech, OpenAI-compatible
embeddings, video storyboard generation, and knowledge-asset vision enrichment
use the same owner boundary through
`lib/model_routing/capability_adapter.py`. Authenticated capability/model lists include
only enabled, authorized routes and never credential material; probes do not override visible controls.
Adjacent capability projections from one request read one owner authority
revision and share one request-scoped normalized candidate compiler; they do
not retain cross-request authorization caches or repeatedly normalize the
aggregate per Offering.
Each execution mints a request-only slot group, pins dispatch to that group,
and disposes it in `finally`; dedicated speech, transcription, and embeddings
endpoints require an OpenAI-compatible cloud or managed-local Connection.
Long TTS jobs share one bounded route group across their worker fan-out.
`/api/v4/images/models` and
`/api/v4/audio/capabilities` are therefore owner projections, not views of a
process-global dispatcher.

The stdlib repair launcher stages first-run provider facts as a bounded,
secret-free `tofu.bootstrap-provider-stage/v1` document. After the Sidecar is
ready, startup initializes an empty personal-owner v2 authority when necessary,
imports the provider credential through the repository secret channel, and
consumes the stage. An already-active authority follows the same idempotent
reconciliation path; no new bootstrap write targets `server_config.providers`.

The public `GET /api/v4/capabilities` response advertises
`model_routing.endpoint=/api/v4/model-routing` and
`contract_version=tofu.model-routing/v2`; it does not publish an owner model
catalog or infer owner-only voice availability. Authenticated clients use
`/api/v4/audio/capabilities` for that projection. `POST /api/v4/server-config`
rejects `providers`, `models`, and
`model_catalog` with `legacy_model_routing_state_removed`. The former
`/api/v4/model-catalog` surface is not registered.

Artificial Analysis is an external, read-only Model enrichment, not a catalog authority. Authenticated owners read `GET /api/v4/model-intelligence/aa`, explicitly refresh with `POST /api/v4/model-intelligence/aa/refresh`, and save or clear their encrypted owner-scoped key with `PUT /api/v4/model-intelligence/aa/key` (`{api_key}`, maximum 256 characters). Responses expose only status, redacted key source/hint, attribution, fetch time, and scores keyed by exact `creator_id::model_id`; they never include the plaintext key or any ProviderAccess, Offering, Deployment, alias, or Route. Ordinary reads never wait on the external network: a bounded 24-hour public dataset cache is served while one background refresh refills stale data. OpenRouter pricing is a second external Model enrichment with an explicit write policy. `GET /api/v4/model-intelligence/openrouter-pricing` returns a read-only report that buckets each canonical Model as priced, fillable, official-covered, openrouter-free, unmatched, or drift (a priced Model whose USD list price disagrees with the OpenRouter row by more than 25% blended — a cross-check signal, never an auto-rewrite). `POST /api/v4/model-intelligence/openrouter-pricing/refresh` force-fetches the public keyless catalog and CAS-writes `list_pricing` only onto unpriced Models whose Creator has no official-direct coverage in the maintained pricing tables; officially priced Models are never rewritten, and all-zero OpenRouter rows never become a $0 list price (unknown stays unknown instead of impersonating an official free tier). The dataset shares the same bounded 24-hour cache/background-refresh budget as the AA enrichment.

### 2.10 Compact MCP tool summary

`mcp_tool_summary` is the shared, additive projection used by the per-turn
context rail:

```json
{"servers":[{"name":"github","count":26}],"total":26}
```

`servers` is deterministically sorted, contains only live or transparently
parked servers with at least one enabled tool, and `count` / `total` describe
tools currently visible to the model. The projection is computed from the
bridge's cached catalog under its lock; it never performs `tools/list` and
contains no descriptions, input schemas, credentials, or filesystem paths.
Failure degrades to `{servers:[],total:0}` and may not fail the response whose
primary operation already succeeded.

The field is returned by `GET /api/v4/server-config` (piggybacking on the
required first-screen model/config request), `GET /api/v4/mcp/catalog` (the
Settings refresh), and `PUT /api/v4/mcp/servers/<name>/tools` (after the tool
filter is hot-applied). `GET /api/v4/mcp/tools?server=<name>` remains the
demand-only detailed schema surface for an expanded Settings card; ordinary
page startup does not call it.

### Private conversation settings

Problem: round-tripping a client settings snapshot must not roll back a durable
MCP declaration reset/freeze or install malformed declaration state.

Decision: `lib/conversation_sync/settings_policy.py` owns the private-key policy.
`_mcpWireDeclarations` remains in the configured conversation settings store but
is not public settings. Generic settings PATCH, legacy tool-state PATCH, and
turn create/append/queue defaults ignore this key regardless of its value;
settings/config resolvers emit only their existing allowlisted fields. Normal
public fields still merge. Filter the incoming patch before the storage merge,
not by reading and restoring a private value: parallel reset/freeze writers
must keep the storage authority's atomic merge / snapshot CAS guarantees.
Conversation detail, catalog and sync snapshot projections omit the slot for
SQL Sidecar and native (daemon or embedded) application backends. Trusted
`conversation.get` reads retain it for `MCPDeclarationStore`, whose internal
`conversation.settings.update` replace + `expected_settings` CAS is unchanged.

Alternatives rejected: route-only filtering leaves create/queue and legacy
write paths open; filtering trusted storage reads breaks CAS; moving the slot
to another database creates a second conversation lifecycle authority.

### 2.11 Deployment feature-flag snapshot

`GET /api/v4/server-config` carries a nested `feature_flags` object so the
required first-screen config read is also the browser's normal flag read. The
five core keys are `pptx_translate_enabled`, `cache_extended_ttl`,
`debug_mode`, `optimizer_enabled`, and `artifacts_enabled`; registered plugin
keys are additive booleans. `GET /api/v4/features` remains the compatibility
and failure-isolation endpoint and returns the same live projection at the
top level beside `ok`.

Both routes read the one `lib.features_store.feature_flags_snapshot`
authority. Its public projection is limited to 256 entries; names must match
`^[a-z][a-z0-9_]{0,79}$`, envelope-reserved names and base-key collisions are
omitted, and values are normalized to booleans. The snapshot contains no
credentials or user state: these flags are deployment-wide switches, not a
single-user shortcut in a repository boundary.

An optional projection failure yields `{}` in `server-config` and may not fail
that response's primary operation. A browser then calls the dedicated endpoint
once; this also keeps a newer browser compatible with an older server. A valid
piggyback snapshot means ordinary startup issues no `/api/v4/features` request.

### 2.12 Owner-scoped chat attachments

The canonical attachment wire object is `TurnMediaAttachment` in
[`contracts/conversation_sync_v3.yaml`](../../contracts/conversation_sync_v3.yaml).
It is bounded metadata, never the original binary or extracted evidence.

| Endpoint | Contract |
|---|---|
| `POST /api/v4/media/attachments` | Multipart field `file`; documents are capped at 50 MiB, parsed into the owner-scoped Knowledge authority, and returned as `{ok, attachment}`. |
| `POST /api/v4/videos/upload` | Multipart field `file`; reserves launch-profile analysis capacity (503 when full), validates the bounded upload, commits the original, and returns the canonical `attachment` plus legacy top-level status aliases. |
| `GET /api/v4/media/attachments/<id>` | Returns canonical owner-resolved metadata as `{ok, attachment}` or 404. |
| `GET /api/v4/media/attachments/<id>/source` | Streams the original with private/no-store, nosniff, conditional/Range behavior; `download=1` selects attachment disposition. |
| `DELETE /api/v4/media/attachments/<id>` | Removes the attachment lifecycle or only its shared scope; `draft=1` is composer cleanup and returns 409 rather than deleting content already retained by a turn/library. |
| `GET /api/v4/videos/<id>` | Compatibility status projection backed by durable attachment metadata after the transient worker registry expires or the process restarts. |

The authenticated owner is explicit at every repository call. IDs and all
client-supplied attachment metadata are untrusted: turn creation resolves each
ID again, drops missing/foreign IDs, deduplicates them, and caps the list at 20.
New turns persist these references only in `projection.attachments`. Historical
`pdfTexts` and `videos` remain readable for imported/old conversations but are
not emitted by the unified upload path.

Video analysis readiness is not a send precondition. A `processing` reference
is valid and model projection reports that state; ready evidence is resolved
only when a model request is composed. Original bytes and derived evidence are
never serialized into conversation JSON or ordinary API logs.

### 2.13 Started chat model-request lifetime

Once provider dispatch starts, execution ownership is independent of the HTTP
observer. Closing a browser, SSE response, or push socket does not abort a
task-backed model request. During upstream stream consumption, database,
presence, push-bus, webhook-listener, and cross-process abort-probe work cannot
run synchronously on that ingress path; bounded task memory remains the live
SSE source. The first post-provider event re-enters authoritative convergence;
if storage is still unavailable its authoritative push remains withheld.
Speculative read-only tool prefetch is also non-authoritative: a failed pool
submission falls back to ordinary post-stream execution and cannot escape
through the provider callback.

`POST /api/v4/chat/stream-direct` follows the same execution rule. Because it
is intentionally a relay with no task/replay handle, a disconnected caller
cannot retrieve the detached result: the server consumes and validates it,
drops relay-only chunks, and holds its admission lease until upstream settles
or its finite deadline expires. The route uses the same authenticated
structured ModelRef resolution, request-scoped provider group, billing
reserve/settle, usage accounting, cgroup headroom guard, and finite production
429 allowance as the task-backed surface. Its default deadline is 600 seconds
and the request override is capped at 900 seconds. OpenAI SSE has no retraction
frame: a provider retry is transparent only before the observer sees output;
after visible output, an attempt restart ends with a typed error instead of
combining discarded and authoritative attempts. Bounded relay overflow is also
terminal and never drops text before publishing `finish_reason=stop`. Callers
requiring reconnect/replay use `POST /api/v4/chat/completions`.

Task-backed chat, compatibility chat, and direct chat bind their request route,
billing reservation, and exact admission lease to one operational
`ExecutionSession`. Terminal resource settlement happens once and precedes a
success terminal frame. Durable billing recovery and admission TTL recovery may
produce a `deferred` receipt; a non-recoverable resource failure is a typed
error, never `finish_reason=stop`. Deadline reconciliation requests owner
cancellation and never releases resources underneath a live provider call.

Explicit owner Stop/supersede, provider or network failure, transport
deadlines, shutdown, crash, and power loss can still terminate work; a crash can
lose memory-only increments from the current provider call before convergence.

### 2.14 Project-path selection and recent history

`PUT /api/v4/project/paths` is the single browser command for selecting one or
more local project roots:

```json
{
  "paths": ["/workspace/primary", "/workspace/extra"],
  "readOnlyPaths": ["/workspace/extra"],
  "recentPaths": ["/workspace/primary", "/workspace/extra"]
}
```

`recentPaths` is optional. When present it contains at most 32 non-empty path
strings of at most 4,096 characters and must be a subset of `paths`. The route
validates the complete recent intent before mutating project state, reconciles
the project roots, then touches the canonicalized paths through one explicit-
owner Sidecar batch. Conversation restore sends only its primary root as recent
intent; an explicit multi-root apply sends the primary-first bounded prefix
(all selected roots when there are at most 32). The browser preserves every
authoritative project root when it applies this projection cap. Invalid project
selection can therefore never poison recent history, while normal restore falls
from two HTTP commands to one and an ordinary N-root apply from 1+N to one.

Recent history is a reconstructible navigation aid, not project authority. A
Sidecar failure after successful project reconciliation is logged and does not
roll back or fail the valid selection; omitting `recentPaths` retains the
side-effect-free reconciliation behavior used by background/status callers.

### 2.15 Conversation resolver fusion and settings commit

`lib/conv_config/` remains the only merge-policy authority. A send that needs
both runtime config and the settings snapshot persisted with its accepted Turn
uses one additive form of the existing resolver:

```json
{
  "conv_settings": {"model": "active composer snapshot"},
  "settings_conv_settings": {"model": "stored conversation snapshot"},
  "overrides": {"model": "live toolbar override"},
  "server_defaults": {"serverModel": "configured default"},
  "is_active": true,
  "include_settings": true
}
```

`POST /api/v4/conversations/config/resolve` returns its ordinary canonical
config plus a canonical `settings` object when `include_settings` is true.
`settings_conv_settings` is optional and defaults to `conv_settings`; the
browser supplies it because an active send may project a workbench path that
must not replace the conversation's stored path. Both projections share the
same override snapshot. An older server ignores the additive request members
and omits `settings`; the browser then uses the retained
`POST /api/v4/conversations/settings/resolve` compatibility read.

For a server-owned conversation,
`PATCH /api/v4/conversations/{id}/settings/resolve` accepts only
`{conv_settings, overrides}`, resolves the canonical patch at backend
authority, and commits it through the same owner-scoped
`conversation.settings.update` command, conflict mapping, notification, and
idempotency boundary as `PATCH .../settings`. A new browser probes this fused
route once per page: a 404 falls back to the legacy resolve-then-PATCH pair and
disables further probes until reload; any other failed response is returned
without a second mutation attempt. `_localOnly` drafts remain cache-only.

### 2.16 Content-addressed Paper report start

`POST /api/v4/paper/report/start` accepts either an ingest-minted
`paper_hash` or compatibility `paper_text`. A valid hash is preferred: the
server checks exact-owner live work and canonical cache before reading source;
only a true task miss projects at most 120,000 characters from that owner's
`paper_library` row. Client text remains the fallback when there is no valid
hash or no stored source. The server trusts no hash as authority: runtime,
artifact, and library lookups all retain the authenticated owner predicate.

If a hash has no usable stored source, the route returns HTTP 400 with
`error_code: "paper_source_required"` before creating or aborting any task. A
browser holding parsed text may retry that explicit failure once with at most
120,000 characters. It must not retry a timeout, network failure, 5xx, or stale
generation, because those outcomes are ambiguous and a second request could
duplicate paid work. Force regeneration resolves all fallible source gates
before it aborts the prior task.

### 2.17 Bounded Paper Q&A context

`POST /api/v4/paper/qa/start` accepts a question of at most 8,000 characters
and either an ingest-minted `paper_hash` or compatibility `paper_text` of at
most 1,000,000 characters (HTTP 400 and 413, respectively, on overflow). A
valid hash is preferred: the server resolves source through the authenticated
owner's library and a bounded owner+hash TTL/LRU; a cache hit still performs a
body- and length-free owner-existence validation. The content hash is the
source revision. Hashes are canonical hexadecimal identities or are derived
from offered text; malformed client hashes never become runtime or artifact
keys.

If a hash has no usable stored source, the route returns HTTP 400 with
`error_code: "paper_source_required"` before creating a task. A browser holding
parsed text may retry that explicit failure once with at most 1,000,000
characters. It must not retry timeouts, network failures, 5xx responses, or a
start whose active paper changed. A valid hash-only start does not recover or
serialize browser paper text; a compatibility start adopts the returned
canonical hash for subsequent questions.

Before dispatch, generated-report sections and paper sections compete within
one 60,000-character relevance budget. Unused capacity from a short source is
reassigned to the other source. At most ten validated `user`/`assistant`
history messages survive, each at most 8,000 characters and together at most
24,000; newer messages win and an oversized message preserves both ends.
These bounds apply to every repeated agent/tool round. English tokens and CJK
bigrams drive selection, and executable tests require relevant tail sections
from both source types to survive the projection. Three consecutive rounds
with an identical tool-call fingerprint and no changed world/evidence trip the
shared no-progress breaker before the fourth duplicate execution. The Q&A task
then settles as `error`, never as a partial `done` answer.

Untrusted paper sanitation preserves LF/TAB/CR, removes the declared invisible
carrier set, maps every other current Unicode Cc/Cf code point to a space, and
defangs the same high-signal directive patterns before section selection. Its
complete 232-code-point translation executes in a C-level pass; required-word
gates may skip a directive regex only when they are a semantic superset of that
pattern. Sources containing Python regex's four special Unicode ASCII-case
equivalents (`İ`, `ı`, `ſ`, `K`) always take the full regex path.

All seven agentic Paper workflows additionally carry a task-local token and
actual-dispatch envelope. The last admitted dispatch receives no tools and is
reserved for synthesis; unmetered provider responses still consume dispatch
admission. A provider response containing tool calls after authority removal
halts before any such call executes with `agent_budget_ignored`. Report meta,
Q&A/Deepen terminal events, Recommend interpretation/terminal events, and
Insight results expose the additive `agentUsageV1` object where applicable.
Its stable v1 fields include `calls`, `agent_dispatches`, token/cache counts,
`agent_token_budget`, `agent_dispatch_budget`, pricing coverage/cost,
`forced_final_reason`, and `budget_ignored`. Survey/Ideate retain the same
fields inside their durable `usage` stage snapshots.

### 2.18 WorkUnit projection (production tasks)

Every non-`chat` task served by `GET /api/v4/tasks/{id}` carries an additive
`workUnit` block — the SINGLE cross-surface lifecycle vocabulary for
long-running work (chat tool rounds, background producers, and the task list
all speak it). The block is projected server-side by the pure function
`lib.production.work_unit.project_work_unit`; the client renders it verbatim
and NEVER re-derives lifecycle state from raw `status` / event streams.

Lifecycle enum (closed — a new runtime status MUST extend
`_STATUS_MAP` in the same change; an unmapped status projects to `failed`
because a status outside the contract IS a failure mode, never silently
relabeled):

    queued | running | retrying | done | degraded | failed | aborted

Mapping from the runtime axis (`task.status`): `pending→queued`,
`running→running`, `done→done`, `error→failed`, `aborted→aborted`.
`degraded` is the product-quality axis outranking the delivery axis:
`status='done'` + `artifact_quality.degraded` → `degraded` (status stays
'done' by design — see `_public_task`). `retrying` is derived: a `running`
unit whose active stage sits on attempt ≥ 2.

Detail-view shape (list view `GET /api/v4/tasks` carries the same block
minus `stages`/`stageTotal`/`links`/`artifacts`/`pages`):

```json
{
  "workUnit": {
    "kind": "slides-deck",
    "title": "…",
    "status": "running",
    "failureKind": null,
    "stages": [{"name": "outline", "index": 1,
                "state": "done|active|skipped|failed|pending",
                "attempt": 1, "elapsedS": 4.24, "lastErrors": []}],
    "stageTotal": 9,
    "quality": {"degraded": true, "reason": "…"},
    "failure": {"message": "…", "detail": "…", "hint": "…"},
    "artifacts": [{"label": "pptx", "href": "/api/v4/slides/{id}/file",
                   "bytes": 2450000, "pages": 12}],
    "pages": {"total": 12, "rendered": [1, 2, 3], "rev": {"1": 2, "2": 1},
              "href": "/api/v4/slides/{id}/pages/{n}.png"},
    "pendingGate": {"guidanceId": "hg_{id}_outline", "title": "…",
                    "pages": [{"page": 1, "pageType": "cover",
                               "key_message": "…"}]},
    "links": {"events": "/api/v4/tasks/{id}/events",
              "abort": "/api/v4/tasks/{id}/abort"},
    "convId": "…"
  }
}
```

Rules:

* `stages` is rebuilt from the registered stage grammar (docs/contracts/EVENTS.md §1
  carve-out); only observed stages appear — `stageTotal` lets the client
  render the not-yet-started remainder. A leftover `active` stage settles
  `failed` on a failed unit and `skipped` on any other terminal unit, so a
  terminal card never shows a forever-spinning stage.
* `artifacts` appears only for kinds with a published download path (today:
  `slides-deck` and `motion-video`) and only on `done|degraded`. Video additionally
  requires the capability's published `result.final_path` receipt; successful
  scene-only subjobs do not imply a final film. The projection exposes only the
  owner-checked `/api/v4/motion/videos/{id}/file` URL, never a filesystem path.
  A kind absent from the map
  omits the slot — the server never invents a URL the capability did not
  publish.
* `pages` (optional) is the streaming per-page preview projection for kinds
  with a published page-preview path (today: `slides-deck`). It folds the
  capability's `page_rendered` events into `{total, rendered[], rev, href}`:
  `rendered` is the sorted set of 1-based pages with a finished preview PNG,
  `rev` counts renders per page so the client can cache-bust a QA re-render
  (`?v=<rev>`), and `href` is a URL template with `{n}` substituted
  client-side. A terminal `done|degraded` unit implies the full set (the
  render-stage gate blocks publication over missing previews, and a
  checkpoint-resumed deck emits no events for pages it already had). The
  block is omitted entirely when no page evidence exists.
* `pendingGate` (optional) is the interactive outline/storyboard review for
  chat-initiated `slides-deck` / `motion-video` jobs: the capability parks before
  production and the card renders the plan with an answer box.
  It projects only while the unit is live and the gate unanswered
  (`outline_gate` / `storyboard_gate` without its later resolved event). Answers
  post to the SHARED `POST /api/v4/chat/human-response` endpoint
  (`{guidanceId, response}` — the same owner-bound registry as ask_human);
  a recognized approval means "generate as-is" and other feedback feeds ONE
  bounded outline-revision pass. The gate is never a lifecycle — the task
  snapshot stays the terminal truth.
* `failure` mirrors the task's error envelope (message/detail/hint) so the
  card can render the exact user-facing diagnosis.
* Tool rounds that ACCEPTED a background job carry the re-attach handle in
  their result meta as `workUnitRef: {taskId, kind}` (see
  `_PRODUCE_WORK_UNIT_KINDS` in `lib/tasks_pkg/handlers/motion_video.py`),
  and their badge speaks the same enum (`queued` fresh, `running` deduped) —
  legacy badges `started`/`joined` are retired.
### Swarm status recovery

`GET /api/v4/swarm/status/<task_id>` and its agents alias are owner-scoped
recovery reads. A resident session's agent rows include `tools`, `toolCalls`,
and `toolCallsOmitted` from the worker's existing tool log: only the newest
30 rows are inspected/copied, details are bounded to 2,000 characters and the
timeline to 32 KiB per agent. Occurrence IDs, running/terminal verdicts and
explicit omission metadata match durable swarm snapshots. This does not
persist another result copy. The background-activity list below intentionally
uses the summary-only status projection, without tool timelines.

Swarm mailbox receipt timestamps (`submittedAt`, `inputInjectedAt`,
`inputConfirmedAt`) are Unix epoch **milliseconds**, including numeric-string
representations; lifecycle times such as `created_at` remain seconds.

### 2.19 Background-activity snapshot

`GET /api/v4/activity/background` answers one chrome question — "is anything
still executing now that the foreground turn has settled?" — for the
authenticated owner. Sub-agent swarms routinely outlive their spawning turn
(results arrive later as auto-dispatched inbox injects) and detached
`run_command background=true` executions retain their originating `conv_id`.
The snapshot remains owner-scoped; the conversation chip, tooltip and popover
show only entries whose `conv_id` matches the active conversation. Switching
conversations immediately reprojects the cached snapshot and closes the old
popover, without waiting for a poll. No active conversation, missing ownership,
and headless commands produce no conversation-local indicator. The sidebar
projects the same owner snapshot by nonempty `conv_id`, including inactive
conversations and search hits, independently of foreground Turn status. Only
running/retrying or pending agents and running commands keep its badge active;
a retained all-terminal swarm or pending result delivery does not. Successful
snapshots replace this derived index; failed reads preserve its last state.

```json
{
  "swarms": [{"swarm_key": "…", "conv_id": "…",
              "running": 1, "pending": 0, "completed": 0, "agent_count": 1,
              "agents": [{"id": "…", "role": "coder", "objective": "…",
                          "status": "running", "round": 3, "error": ""}],
              "created_at": 1757100000.0}],
  "backgroundCommands": [{"command_id": "bg_…", "conv_id": "…", "command": "…",
                          "status": "running", "started_at": 1757100000.0,
                          "elapsed_s": 10, "delivery_status": "not_applicable"}]
}
```

Rules:

* Owner-scoped on both halves; a session or command belonging to another
  user is invisible, never redacted.

* Each swarm also carries its exact `swarm_id` (original run task ID, not the
  reusable `swarm_key`) and the canonical bounded `messageReceipts`,
  `messageReceiptsAvailable`, `messageReceiptsVersions`, and
  `messageParticipants` projection. The communication dock uses this existing
  poll to reconcile push-only receipts; missing history is not an empty mailbox.
  Mailbox read failures preserve the activity row with availability false.
  Receipt reads run outside the session-registry and orchestrator locks.
  This reuses the snapshot owner rather than adding another history poll or
  treating a received push as proof that all earlier messages are present.
* `swarms` lists only LIVE (non-terminated) sessions from the process-local
  registry, keyed by the stable swarm key plus `conv_id` for deep-links —
  deliberately not by task id, since the spawning task has usually settled.
  Per-agent status is sampled outside the session-registry lock so a busy
  session never stalls the reader; a session whose snapshot raises is logged
  and skipped, not failed.
  Counters are derived from that exact per-agent snapshot, never separately
  sampled scheduler pool counters. `running` means admitted execution (including
  in-run provider retries); `pending` means not yet admitted; `completed` counts
  settled agents, not only successful ones. Pending rows include `waitReason`
  (`capacity` or `dependencies`) and unresolved `dependsOn` IDs where applicable.
  Pool submission alone neither starts `startedAt` nor emits a running phase.
  A scheduler retry releases that clock and returns to pending capacity; its
  fresh admission starts a new clock. A failed attempt is not agent settlement.
  Background headers omit zero-count categories; individual rows explain actual
  waits rather than presenting every not-yet-started agent as generically queued.
* `backgroundCommands` lists only `status == "running"` detached commands.

  Absence is not completion. Cards join `results[].backgroundCommandId` to conversation-scoped canonical queue/receipt evidence; optional outcomes follow `conversation_sync_v3.yaml`. Unknown legacy exit status means completed, not success. Activity hints wake one coalesced reader with a trailing refresh, not chat hydration. Queue enqueue alone emits no sync event; delivery commits do.
* Both lists are empty in the steady state. A transient failure must keep the
  last known state on screen (`onError:'null'`), never flap the UI.
### 2.20 Immutable paper builds

Paper-build commands, owner-scoped artifacts, subscriber leases and build-bound
locations follow [the paper-build contract](../PAPER_BUILDS.md).

### 2.21 Published response files

Existing-file publication uses the task-owned `publish_response_file` capability,
not an arbitrary HTTP filesystem mount. Types, receipt fields and bounds are
owned by [`response_file_v1.schema.json`](../../contracts/response_file_v1.schema.json).
Artifact metadata/lifecycle, raw/view/export and downloads recheck the live
conversation owner for every source, including legacy artifacts. Missing, foreign
or deleted conversations are unavailable. Private bytes are never browser-cached.
`GET /api/artifacts/{id}/view` previews Markdown as well as HTML/SVG, sharing the PDF body's lazy renderer.
Raw Markdown HTML stays escaped, missing rendering falls back to escaped source, and the existing view CSP remains authoritative.
`GET /api/artifacts/{id}/download` is a raw attachment carve-out: verified original
bytes, safe filename, `nosniff`, and `private, no-store`. HTML/SVG download defaults
to inert text; `interactive=1` explicitly selects the original executable format
and carries a warning. Binary-envelope artifacts have no raw/view/source exposure.
Owned rich actions resolve through `/api/response-assets/artifacts/{id}` with the
origin conversation; download reads decode the same snapshot, never its source path.
No dependency fetching, directory hosting, storage migration or second lifecycle
is introduced. Preview/code-consent presentation follows [the render contract](../RENDER_CONTRACT.md#native-rich-responses-and-published-assets).

## 3. Request parsing

* JSON body → `parse_body()` (sync handlers) / `await async_parse_body()`
  (async handlers). Never raw `request.get_json()` — the shim semantics and
  the empty-body→`{}` contract live in one place. Mutations whose empty body
  selects a real default action use `strict=True`: malformed JSON then becomes
  a canonical 400 and unexpected parser failures propagate instead of silently
  executing that default.
* Fields → `require_str` / `optional_int` / `require_list` / … — these raise
  `BadRequest(field=…)`, which `@safe_route` and the global boundary convert
  to a 400 carrying the field name. Hand-rolled `"x is required"` returns are
  a violation. Parse and field validation also stay outside broad operational
  `except Exception` blocks; otherwise the route turns an actionable 400 into
  a redacted 500. The sole registered exception is best-effort locked-out
  telemetry for an already-unauthorized browser poll, whose response must
  remain the authentication boundary's 401 even when its body is malformed.
* Query-string **path** args → `decode_proxy_path_arg('path')` (the VS Code
  proxy double-encodes; this seam undoes it, bounded).

## 4. Carve-out registry

Some endpoints are **deliberately outside the envelope**. A carve-out is
legal only if it appears here AND in `tests/api/test_api_contract_drift.py`'s
`CARVE_OUT_FILES` / bare-payload list with a reason.

| What | Where | Why |
|---|---|---|
| OpenAI compat | `routes/compat_openai.py` | Emulates the OpenAI wire protocol; an `ok` key corrupts protocol fidelity for third-party SDKs |
| Alipay notification | `/api/v4/billing/webhooks/alipay` | Provider acknowledgements are plain `success` (200) / `fail` (400); framework form errors still use problems. |
| Anthropic compat | `routes/compat_anthropic.py` | Same — Anthropic protocol shape |
| Device bridges | `routes/browser.py`, `routes/_bridge_caller.py`, `routes/desktop.py` | Owner-scoped long-poll protocols parsed by external browser/desktop devices; poll shape is locked outside this repo |
| SSE streams | chat stream, agent-run, translate stream, compat streams | `text/event-stream` framing; use `sse_response()` for the canonical headers, never hand-set them |
| Binary / raw payloads | artifact raw/view/export, paper PDF serving, attachment source, image bytes, podcast audio | `raw_success_response` preserves bytes (including JSON files), Range and leases; v4 failures remain problems |
| Multipart uploads | `/api/v4/media/attachments`, `/api/v4/videos/upload`, `/api/images/upload`, `/api/paper/upload`, `/api/pdf/parse`, … | FormData in, but the *response* still follows the envelope |
| Native list payloads | Canonical handlers return `api_ok({items: …})`; v4 carries that domain object in `data`. Folder, provider, and Orchestration catalogs reject malformed or logical-failure data. Search/queue retain their declared empty fallback; failed translation probes remain null. Those list adapters no longer accept a pre-migration bare-array wire. Domain array return values remain stable for callers. New endpoints MUST declare their item wrapper; externally locked shapes require a §4 carve-out. | The release boundary and owning client jointly enforce admission. Never hide transport/parse failure as authoritative empty durable state. |

Adding a carve-out requires: (1) a row in this table, (2) an entry in the
drift test with the same reason, (3) a commit message explaining why the
envelope is impossible (not merely inconvenient).

The bridge-secret transport namespace is also deliberately unversioned. In addition to `/api/browser/poll`, `/api/browser/download` and `/api/desktop/poll`, it contains the path-parameterized browser file-transfer start/chunk/complete/abort routes under `/api/browser/file-transfers/`. Those four routes use the normal JSON success/error envelope (the chunk request body is raw bytes); control envelopes are capped at 16 KiB and chunks at 256 KiB before parsing/buffering. They stay beside poll because the extension, not a public API consumer, is the only caller. Every non-OPTIONS request still passes the real owner-scoped bridge credential gate. `/api/browser/poll` additionally passes a bounded credential-digest gate before storage authentication and an owner-aware gate afterward. Normal long-polls retain the existing 200 wire; pressure returns 429 with `Retry-After`, oversized poll/result frames return 413, and protocol rejection remains 426 with `Retry-After`. One device retry supersedes its prior waiter without a conflict response, so proxy overlap is invisible to healthy clients. A current extension uses string device/result IDs of at most 128 characters, a capability array no larger than the canonical known-capability set with 64-character string items, and bounded version/profile strings; malformed amplification shapes return 400. It mirrors its protocol in `X-Browser-Protocol-Version` solely to clear an older binary's credential cooldown after an in-place upgrade; the authenticated JSON frame remains authoritative and every admission/authentication check still runs. Completed results replay idempotently after transport failure; a 413 first bisects a multi-result batch and only a still-oversized singleton becomes an explicit command error.

## 5. Enforcement (the ratchets)

| Guard | What it rejects |
|---|---|
| `tests/frontend/test_frontend_api_isolation.py` | Any backend `fetch` outside `frontend/src/api/transport.ts` (including variable-URL bypasses) |
| `tests/api/test_api_contract_drift.py` | Any ad-hoc `jsonify(` in `routes/**` outside the carve-out registry; per-file count may only decrease; stale baselines must be tightened in the same commit |
| `tests/api/test_api_contract_*_parity.py` (21 batch suites) | Per-batch wire parity (legacy keys byte-identical; additions only +ok/+error/+request_id) + shipped-source tripwires + front/back coordination anchors for every bare-array migration |
| `tests/api/test_api_response_safe_route_rollout.py` | `@safe_route` rollout state; documents the handlers that must NOT be decorated (side-effecting except blocks) |
| `tests/diagnostics/test_request_parser.py` | `BadRequest` → 400 mapping, typed extractors, AST rejection of raw `request.get_json()` calls, and broad-exception misclassification in `routes/**` |

`@safe_route` note: the framework boundary (`server.py`) already converts
uncaught exceptions on `/api/*` to the JSON 500 envelope, so `@safe_route` is
NOT required for correctness. Adopt it on handlers that currently hand-roll a
pure `except Exception → api_internal_error(e)` block **only when the block
has no side effects and no distinct `context=` string** (the rollout suite
pins the gate).

## 5.8 Chat task Stop semantics

`POST /api/v4/chat/abort/<task_id>` and the authenticated push
`{action:"abort", channel:"chat", taskId}` command are transport adapters for
one owner-checked task-manager cancellation operation. A successful request
atomically marks the live task aborted, signals its existing abort event,
cancels queued work where possible, invokes every scoped runtime-resource
callback, and immediately republishes the authoritative idle/busy projection.
Duplicate requests are idempotent and must not produce duplicate terminal
cancellation events.

The operation cancels scoped resources only. Existing detached swarm agents and
scheduler jobs retain their current lifecycle. For `run_command`, cancellation
means process-group `SIGTERM`, bounded grace, `SIGKILL` escalation when needed,
and mandatory reap; ordinary foreground execution never returns a long-running
command handle as a side effect of cancellation. Cancellation remains
distinguishable from a handler/transport error, and any retained partial output
is owner-scoped and explicitly incomplete.

The authenticated per-command `POST /api/v4/chat/background-command/<task_id>`
requests **handoff**, not cancellation, of the currently executing local
`run_command`/`code_exec` process. Its command-scoped identity must match the
active subprocess, so a delayed request cannot detach a subsequent tool call
on the same task. Acknowledgement means only that handoff was requested; the
command card and model receive the background handle after the runner accepts
and settles the original tool round. No active or matching subprocess, a
terminal task, stdin wait, Stop/interrupt, or exhausted handoff capacity leaves
the original command foreground-owned; the response never invents success.

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
   `frontend/src/runtime/sections/api.js`, or update the machine-readable
   contract and regenerate its typed client. Never `fetch('/api/…')` outside
   `frontend/src/api/transport.ts`.
2. `onError:'null'` only for genuinely best-effort reads; mutations and
   "user must see the reason" calls must throw `ApiError`.
3. Read typed failures from `err.envelope.kind` (e.g. branch on `409` /
   `overloaded`), not string matching on messages.

## 7. API-domain object contracts

Cross-cutting backend object contracts are defined once, machine-readably, and are
never duplicated here: Project Brain v1 → `contracts/project_brain_v1.schema.json`;
Research snapshot → `contracts/research_record_v1.schema.json`; Research Foundry
program → `contracts/research_program_v1.schema.json`. See
[research cycle](../RESEARCH_CYCLE.md) for write-failure, snapshot-size, and checkpoint
recovery boundaries.
Restart approval GET includes diagnostic `preparation` per [`restart_preparation_v1.json`](../../contracts/restart_preparation_v1.json): actual phase, elapsed seconds, optional remaining budget and update time; only the executable restart POST grants acceptance.

Native non-streaming Chat completion responses expose `provider_id` from the
server-owned final dispatch metadata, or an empty string when unavailable.
This is the final attempt's provider, not a claim that all retry/fallback
attempts used it; request receipts remain the full attempt evidence. The
request's preferred provider never fills unknown actual identity.

### Video upload source admission

The default video upload accepts complete source bytes without a private file
size quota. Request overflow uses the shared owned temporary-file buffer and
Multipart parsing consumes transport chunks. Format validation, authenticated
owner scope, actual I/O errors and processing publication failures keep their
existing envelopes. Explicit application/route overrides remain effective;
other source-decoder policies are separate from this transport boundary.

## Owned response resource projections

`GET /api/response-surfaces/providers` and `/resources` require the shared auth
boundary and exactly one context: `runId`, or `conversationId` plus `turnId`.
Context comes from the mounted task, not rich JSON. No owner/path override or
duplicate query parameter is accepted. Resources additionally require `provider`
and optionally a registered `connection` and exact `queue`. Responses are private,
no-store raw JSON; resource snapshots reuse the rich-response schema definitions.
The source owner is `routes/response_assets.py`, calling `lib/response_surfaces/__init__.py`.

Providers return `{providers:[{id,connection?,requiresQueue?}]}`. Resources return
`{version:1,provider,snapshot}` with server read time and provider quantities.
Unknown capacity stays null; missing pending is not zero. Missing ownership returns
404, malformed input 400 and provider failure 503 without private transport detail.
Authenticated ownership is rechecked after external reads. No endpoint submits,
reserves, cancels or reallocates compute. Existing Turn commands remain the sole
form-answer write path; read-only resource data grants no write authority.

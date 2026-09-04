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
| 4 | Response envelope | `lib/api_response.py` (`api_ok` / `api_error` / `api_not_found` / …, `sse_response`) | `tests/test_api_contract_drift.py` + `tests/test_api_response.py` |
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
physical replay layout. New rows retain only a private AttemptEvent sequence
reference and reconstruct the exact envelope in one storage JOIN; historical
rows remain self-contained. No reference key enters JSON, SSE, cursors, or
generated DTOs. Missing/mismatched references fail closed, retained references
fence AttemptEvent cleanup, and an explicit turn deletion may expire the replay
prefix so affected cursors follow the existing `cursor_expired` snapshot path.

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
`POST /api/v3/conversations/{conversationId}/turns/{turnId}/perception`
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

Request Inspector discovers retained evidence through
`GET /api/v1/tasks/by-conv/{conversationId}`. New turn-native rows come from the
owner-filtered, metadata-only `turn.timing_trace.list` operation before any
limit; its cursor query never selects trace JSON or message content and returns
at most 100 attempts plus `hasMore`. Durable `task_results` remains the fallback
for legacy/non-attempt tasks. Task detail is then read through
`GET /api/v1/tasks/{taskId}/trace`, where a matching attempt is sufficient even
after hot task-registry and event-retention expiry.
Private task-result field-codec envelopes never cross this API: full legacy
reads hydrate the original value, while compact replay may decode only the
metadata/error or explicitly requested terminal fields it returns.

The per-round payload may include `rawArchives` metadata for provider-bound
request/response evidence. Bodies remain lazy: consumers read one bounded
window through
`GET /api/v1/tasks/{taskId}/raw-archives/{archiveId}/{request|response}` with
`offset` and `limit` (maximum 1 MiB). Authorization checks both the task and the
archive owner; foreign or deleted evidence is 404. Quota/secret/transport
truncation is explicit in metadata, never presented as a complete raw body.

The same generated contract owns
`GET /api/v3/conversations/{conversationId}/turns/{turnId}/images/{imageIndex}`.
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

The sidebar catalog `GET /api/v1/conversations` returns header metadata rows
(`{ok, items}` with `X-Total-Count`, conditional via ETag). A row carries
`busy: true` exactly when the owner's task registry still holds live work for
that conversation (`list_running_tasks(user_id=…)` — carrier- and
wedge-filtered, fail-closed to absent). The flag participates in the list
ETag, so a busy↔idle transition always busts the conditional request; a 304
never serves a stale busy projection. The flag exists so a freshly loaded
page can re-wake exactly the live conversations (snapshot + attempt-stream
reconnect) instead of waiting for a manual open — the sidebar streaming dot
and "answering" tag otherwise derive from client-side Turn state alone.

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
 A successful clone stamps `settings.clonedFrom` with the
source conversation id; the sidebar settings projection exposes that key and
the conversation list renders a copy badge from it.

Compaction archive inspection is owner-scoped and summary-first:

| Action | Endpoint | Success |
|---|---|---|
| List archive metadata | `GET /api/v1/conversations/{id}/compactions` | `{ok, compactions, count}` without transcript bodies |
| Read summary projection | `GET /api/v1/conversations/{id}/compactions/{archiveId}?includeMessages=false` | `{ok, archive}` |
| Read raw transcript snapshot | `GET /api/v1/conversations/{id}/compactions/{archiveId}` | `{ok, archive, messages}` |
| Download raw snapshot | `GET /api/v1/conversations/{id}/compactions/{archiveId}?download=true` | unwrapped JSON attachment `{archive,messages}` |
| Run idle manual compaction | `POST /api/v1/conversations/{id}/compact` | `{ok, archiveId, tokensBefore, tokensAfter, tokenCountKind:"estimated", msgsBefore, msgsAfter, reductionPct, summaryPreview, receipt}` |

Archive metadata uses `schemaVersion=tofu.compaction-archive/v3`, millisecond
`createdAt`, and `tokenCountKind=estimated`. Byte-valued `payloadSize` measures
the private stored message document; full/download reads always hydrate the
unchanged public messages. `model`/`taskModel` is the task model, not an implicit
claim about the summary model. `trigger` distinguishes `working_set`, `window`, `force`,
`reactive`, and `manual` causes. Archives created before this distinction may
carry `force` for an automatic window-threshold run. `tokensAfter` is the
compaction-stage estimate; later context providers can change the next actual
provider request.

The selected archive's summary/full projection also carries a bounded
`receipt` with `schemaVersion=tofu.compaction-receipt/v1`. It records status,
strategy/implementation, continuation form, retention decisions (including a
maximum of eight durable recent-file paths and the additive
`durableObjectiveApplied` proof), summary duration and normalized usage,
optional cache economics/evidence counts (including the selected payback
horizon/policy), and deterministic recovery measurements. The receipt is
capped at 32 KiB and never duplicates the summary
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
Legacy task-result recovery resumes backend-neutral record cursors in pages of
at most 256 source rows (eight BLOBs per SQL fetch) and has a 10,000-row source
ceiling; reaching that ceiling sets `truncated` instead of restarting an
unbounded scan or silently promoting from partial evidence.
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

### 2.9 Model-routing v2

The machine-readable authority is
[`contracts/model_routing_v2.schema.json`](../contracts/model_routing_v2.schema.json);
the runtime owner is `lib/model_routing/`. One revisioned
`tofu.model-routing/v2` aggregate belongs to each explicit owner/tenant
boundary. It contains Creators, official Models, Providers, the owner's
ProviderAccess resources, Connections, redacted Credential metadata,
Offerings, and Deployments. Route is a bounded runtime computation, never a
persisted configuration entity.

| Endpoint | Contract |
|---|---|
| `GET /api/v1/model-routing` | Returns `{ok, revision, model_routing}`; the document is safe for inspection and contains no credential plaintext. |
| `PUT /api/v1/model-routing` | Accepts `{expected_revision, model_routing}` and atomically replaces the aggregate. A stale revision is `409 model_routing_revision_conflict`. |
| `GET/POST /api/v1/providers` | Lists or creates ProviderAccess bundles inside the same aggregate. |
| `GET /api/v1/providers/templates` | Lists secret-free onboarding recipes as `{ok, items}`. Recipes are setup hints, not routing authority. |
| `POST /api/v1/providers/templates/compile` | Accepts `{template_key, selected_model_ids?}` and returns a secret-free v2 ProviderAccess bundle for client-side review/staging; it does not persist. |
| `POST /api/v1/providers/probe` | Authenticated discovery that returns transport facts plus a secret-free v2 ProviderAccess draft. It does not persist or echo credential plaintext. |
| `GET/PATCH/DELETE /api/v1/providers/{provider_id}` | Reads, CAS-replaces, or deletes one Provider and its owner access resources. |
| `PUT /api/v1/model-routing/credentials/{credential_id}/secret` | Replaces one encrypted credential secret outside the aggregate and advances its revision. |
| `POST /api/v1/model-routing/migration/plan` | Produces a redacted, non-writing import plan and entity counts. |
| `POST /api/v1/model-routing/migration/commit` | Re-plans, validates, stores encrypted secrets, and commits once; failure keeps the old authority inactive and stores a recovery receipt. |

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

Candidate compilation first filters ProviderAccess enablement, Credential
authorization/quota, Offering confirmed/stale/capability/context/price state,
Deployment probe state, Connection protocol and scoped health. It preserves a
healthy preferred Provider, then orders eligible candidates by operator
priority, health, cache affinity, connection/deployment priority, latency, and
actual Offering price. Cross-provider failover begins only after the preferred
Provider is unavailable or a request fails. Once every route for the selected
official Model fails, the compiler may choose the highest-quality compatible
Model under the same hard request policy, preferring the original Provider.

Health failures apply only at their factual scope: a missing wire route affects
the Deployment, network failure the Connection, 401/402 the Credential, and 403
the Credential×Deployment authorization. Configured `enabled` state is never
rewritten from transient health. A directory miss marks an Offering/Deployment
stale and removes it from automatic choice; it does not delete durable state.

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
only enabled, authorized, probe-passed routes and never credential material.
Adjacent capability projections from one request read one owner authority
revision and share one request-scoped normalized candidate compiler; they do
not retain cross-request authorization caches or repeatedly normalize the
aggregate per Offering.
Each execution mints a request-only slot group, pins dispatch to that group,
and disposes it in `finally`; dedicated speech, transcription, and embeddings
endpoints require an OpenAI-compatible cloud or managed-local Connection.
Long TTS jobs share one bounded route group across their worker fan-out.
`/api/v1/images/models` and
`/api/v1/audio/capabilities` are therefore owner projections, not views of a
process-global dispatcher.

The stdlib repair launcher stages first-run provider facts as a bounded,
secret-free `tofu.bootstrap-provider-stage/v1` document. After the Sidecar is
ready, startup initializes an empty personal-owner v2 authority when necessary,
imports the provider credential through the repository secret channel, and
consumes the stage. An already-active authority follows the same idempotent
reconciliation path; no new bootstrap write targets `server_config.providers`.

The public `GET /api/v1/capabilities` response advertises
`model_routing.endpoint=/api/v1/model-routing` and
`contract_version=tofu.model-routing/v2`; it does not publish an owner model
catalog or infer owner-only voice availability. Authenticated clients use
`/api/v1/audio/capabilities` for that projection. `POST /api/v1/server-config`
rejects `providers`, `models`, and
`model_catalog` with `legacy_model_routing_state_removed`. The former
`/api/v1/model-catalog` surface is not registered.

Artificial Analysis is an external, read-only Model enrichment, not a catalog
authority. Authenticated owners read `GET /api/v1/model-intelligence/aa`,
explicitly refresh with `POST /api/v1/model-intelligence/aa/refresh`, and save
or clear their encrypted owner-scoped key with
`PUT /api/v1/model-intelligence/aa/key` (`{api_key}`, maximum 256 characters).
Responses expose only status, redacted key source/hint, attribution, fetch
time, and scores keyed by exact `creator_id::model_id`; they never include the
plaintext key or any ProviderAccess, Offering, Deployment, alias, or Route.
Ordinary reads never wait on the external network: a bounded 24-hour public
dataset cache is served while one background refresh refills stale data.

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

The field is returned by `GET /api/v1/server-config` (piggybacking on the
required first-screen model/config request), `GET /api/v1/mcp/catalog` (the
Settings refresh), and `PUT /api/v1/mcp/servers/<name>/tools` (after the tool
filter is hot-applied). `GET /api/v1/mcp/tools?server=<name>` remains the
demand-only detailed schema surface for an expanded Settings card; ordinary
page startup does not call it.

---

### 2.11 Deployment feature-flag snapshot

`GET /api/v1/server-config` carries a nested `feature_flags` object so the
required first-screen config read is also the browser's normal flag read. The
five core keys are `pptx_translate_enabled`, `cache_extended_ttl`,
`debug_mode`, `optimizer_enabled`, and `artifacts_enabled`; registered plugin
keys are additive booleans. `GET /api/v1/features` remains the compatibility
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
piggyback snapshot means ordinary startup issues no `/api/v1/features` request.

---

### 2.12 Owner-scoped chat attachments

The canonical attachment wire object is `TurnMediaAttachment` in
[`contracts/conversation_sync_v3.yaml`](../contracts/conversation_sync_v3.yaml).
It is bounded metadata, never the original binary or extracted evidence.

| Endpoint | Contract |
|---|---|
| `POST /api/v1/media/attachments` | Multipart field `file`; documents are capped at 50 MiB, parsed into the owner-scoped Knowledge authority, and returned as `{ok, attachment}`. |
| `POST /api/v1/videos/upload` | Multipart field `file`; reserves launch-profile analysis capacity (503 when full), validates the bounded upload, commits the original, and returns the canonical `attachment` plus legacy top-level status aliases. |
| `GET /api/v1/media/attachments/<id>` | Returns canonical owner-resolved metadata as `{ok, attachment}` or 404. |
| `GET /api/v1/media/attachments/<id>/source` | Streams the original with private/no-store, nosniff, conditional/Range behavior; `download=1` selects attachment disposition. |
| `DELETE /api/v1/media/attachments/<id>` | Removes the attachment lifecycle or only its shared scope; `draft=1` is composer cleanup and returns 409 rather than deleting content already retained by a turn/library. |
| `GET /api/v1/videos/<id>` | Compatibility status projection backed by durable attachment metadata after the transient worker registry expires or the process restarts. |

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

---

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

`POST /api/v1/chat/stream-direct` follows the same execution rule. Because it
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
requiring reconnect/replay use `POST /api/v1/chat/completions`.

This boundary does not manufacture an impossible process-level guarantee.
Explicit owner Stop/supersede, provider or network failure, configured
transport deadlines, runtime shutdown, process crash, and host power loss can
still terminate work. In particular, a crash can lose memory-only increments
from the current provider call before its boundary convergence.

---

### 2.14 Project-path selection and recent history

`PUT /api/v1/project/paths` is the single browser command for selecting one or
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

---

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

`POST /api/v1/conversations/config/resolve` returns its ordinary canonical
config plus a canonical `settings` object when `include_settings` is true.
`settings_conv_settings` is optional and defaults to `conv_settings`; the
browser supplies it because an active send may project a workbench path that
must not replace the conversation's stored path. Both projections share the
same override snapshot. An older server ignores the additive request members
and omits `settings`; the browser then uses the retained
`POST /api/v1/conversations/settings/resolve` compatibility read.

For a server-owned conversation,
`PATCH /api/v1/conversations/{id}/settings/resolve` accepts only
`{conv_settings, overrides}`, resolves the canonical patch at backend
authority, and commits it through the same owner-scoped
`conversation.settings.update` command, conflict mapping, notification, and
idempotency boundary as `PATCH .../settings`. A new browser probes this fused
route once per page: a 404 falls back to the legacy resolve-then-PATCH pair and
disables further probes until reload; any other failed response is returned
without a second mutation attempt. `_localOnly` drafts remain cache-only.

The healthy send path therefore needs one resolver request instead of two,
and the healthy settings-persistence path needs one request instead of two.
No browser code contains a second copy of the merge rules.

---

### 2.16 Content-addressed Paper report start

`POST /api/v1/paper/report/start` accepts either an ingest-minted
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

---

### 2.17 Bounded Paper Q&A context

`POST /api/v1/paper/qa/start` accepts a question of at most 8,000 characters
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

---

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
| Binary / raw payloads | artifact raw/view/export, paper PDF serving, attachment source, image bytes, podcast audio | Typed bytes with `Content-Disposition` / Range; JSON envelope impossible |
| Multipart uploads | `/api/v1/media/attachments`, `/api/v1/videos/upload`, `/api/images/upload`, `/api/paper/upload`, `/api/pdf/parse`, … | FormData in, but the *response* still follows the envelope |
| Bare-array legacy payloads | ALL known instances migrated 2026-08-01 (orchestrations / conversations list / providers-templates / chat active / translate poll-batch / folders ×2 / conv search / chat queue) | Enveloping an array (`{ok, items}`) changes the top-level type — never additive. The retirement path is the **coordinated front+back migration**: backend `api_ok({'items': …})` + the `Api.<domain>` seam unwraps `.items` with an `Array.isArray(d)` fallback for rolling-deploy skew. Unwrap semantics follow the CONSUMER: `|| []` only where empty is an explicitly acceptable degradation (orchestrations/queue/search), **failure-preserving** for durable folder catalogs so an outage cannot erase the last good projection, **null-preserving** for probes that must distinguish “zero results” from “probe failed” (chat active, translate poll-batch), and caller-side unwrap for Response-returning seams (config templates, chat activeResponse). Sites that genuinely cannot migrate register in the drift suite's `CARVE_OUT_SITES` with a reason — never a silent baseline remainder. New endpoints MUST wrap arrays in `api_ok({'items': …})` |

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
| `tests/test_api_response_safe_route_rollout.py` | `@safe_route` rollout state; documents the handlers that must NOT be decorated (side-effecting except blocks) |
| `tests/test_request_parser.py` | `BadRequest` → 400 mapping, typed extractors, AST rejection of raw `request.get_json()` calls, and broad-exception misclassification in `routes/**` |

`@safe_route` note: the framework boundary (`server.py`) already converts
uncaught exceptions on `/api/*` to the JSON 500 envelope, so `@safe_route` is
NOT required for correctness. Adopt it on handlers that currently hand-roll a
pure `except Exception → api_internal_error(e)` block **only when the block
has no side effects and no distinct `context=` string** (the rollout suite
pins the gate).

---

## 5.8 Chat task Stop semantics

`POST /api/v1/chat/abort/<task_id>` and the authenticated push
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
and mandatory reap; no API returns a long-running command handle as a side
effect of ordinary execution. Cancellation remains distinguishable from a
handler/transport error, and any retained partial output is owner-scoped and
explicitly incomplete.

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
2. Convert; add a behavioral test at the owning route boundary. Legacy keys
   must survive byte-identical; the ONLY additions allowed are `ok` (always)
   and `request_id`/`error` (error statuses). Do not freeze the implementation
   by reading route source text.
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

---

## 9. Signal-driven Project Brain v1

The machine-readable object authority is
`contracts/project_brain_v1.schema.json`. All endpoints require authentication,
resolve `user_id` at the auth boundary, require an explicit project `path`, and
scope the Sidecar operation by owner plus normalized project.

### Read projections

| Method | Path | Response payload |
|---|---|---|
| `GET` | `/api/v1/project/board` | `{project, headSequence, active, recentOutcomes}` |
| `GET` | `/api/v1/project/feed` | `{events: NarrativeEvent[], headSequence}`; optional `since`, bounded `limit` |
| `GET` | `/api/v1/project/charter` | `{project, decisions: CharterDecision[]}` |
| `GET` | `/api/v1/project/brain/status` | derived counts; no independent status snapshot |
| `GET` | `/api/v1/project/brain/attention` | `{project, items}` from the shared projection |
| `GET` | `/api/v1/project/brain/summary` | compact Board/Status/Attention/Charter/Watch projection |
| `GET` | `/api/v1/project/brain/watch` | current human-maintained Watch items |
| `GET` | `/api/v1/project/brain/checkers` | immutable Checker versions |

Board and Charter read routes have no generic mutation counterpart. In
particular, Board post/claim/complete/block/reopen/delete/answer and Charter
commit/pending/dismiss/update/delete routes are not registered.

### Human commands

| Method | Path | Required body |
|---|---|---|
| `POST` | `/api/v1/project/brain/attention/add` | `path, text`; saves a human-selected unchecked conclusion for non-prompt triage |
| `POST` | `/api/v1/project/brain/watch/add` | `path, kind, text`; optional `conversationId` |
| `POST` | `/api/v1/project/brain/watch/update` | `path, itemId`; optional `text, status, latestResult` |
| `POST` | `/api/v1/project/brain/watch/delete` | `path, itemId` |
| `POST` | `/api/v1/project/brain/checkers/register` | `path, definition: CheckerDefinition` |
| `POST` | `/api/v1/project/brain/checkers/run` | `path, checkerId, version`; optional `workId` |
| `POST` | `/api/v1/project/charter/decision/promote` | `path, decisionId, text, checkerRef{id,version}, sourceConversationId, sourceTurnId` |

Checker registration is immutable by `{checkerId, version}`. Execution passes
the registered `argv` without a shell. Decision promotion rejects missing or
unknown Checker versions; unchecked text never enters Charter.
When no Checker exists, the assistant-turn action may save the conclusion as
`pending_decision` Attention instead; this state is explicitly excluded from
Project Context.

Integration create/register/checkpoint/submit/retry/discard association uses
`workId`, the automatic Project Work ID. Human Integration status/review and
promotion remain available; stable promotion runs all enabled Checkers and
rejects any failure before publication.

### Removed model and HTTP surfaces

There are no compatibility aliases for the retired Project Brain model tools:
`project_charter_read`, `project_charter_propose`, every `project_board_*`,
`project_peer_status`, `project_feed_read`, `project_message`,
`project_intervene`, or `integration_status`. Model execution retains only
`integration_checkpoint` and `integration_submit` for this domain.

---

## 10. Research Foundry program and actions

The durable object authority is
`contracts/research_program_v1.schema.json`. Every route is authenticated and
owner identity reaches the repository explicitly. Workspace mutations use
`expected_revision`; a stale writer receives HTTP 409 and never merges over
newer experiment evidence.

| Method | Path | Contract |
|---|---|---|
| `GET` | `/api/v1/research/workspace?direction=&lang=` | `{workspace, readiness}`; an unknown direction is canonical revision zero |
| `PUT` | `/api/v1/research/workspace` | `{direction, lang, expected_revision, workspace}` |
| `GET` | `/api/v1/research/capabilities` | live MCP tools plus provider-neutral advisory capability matches |
| `POST` | `/api/v1/research/manuscript/scaffold` | adds missing safe relative source files and commits one CAS revision |
| `GET` | `/api/v1/research/manuscript/source.zip` | deterministic binary ZIP of the current normalized source tree |
| `POST` | `/api/v1/tasks/start` with `kind=research-action` | starts `experiment`, `analyze`, `manuscript`, `compile`, or `publish` |

An exact saved capability binding, not the discovery score, grants MCP
authority. Write-class actions require `confirm_external_writes=true`.
`compile` additionally requires `manuscript.compile`; `publish` requires
`publication.push`. Both settle success only from a completed matching tool
receipt and stamp the current source digest. The ZIP route is the documented
binary carve-out from the JSON envelope.

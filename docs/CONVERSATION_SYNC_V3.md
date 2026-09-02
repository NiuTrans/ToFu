# Conversation Sync v3

Responsibility: the sole command, snapshot, replay, recovery, and health
protocol for a conversation. The canonical wire source is
`contracts/conversation_sync_v3.yaml`; generated Python and TypeScript files
must never be hand-edited.

## One authority

```text
generated command DTO
  → routes/conversation_sync_v3.py
  → ConversationTurnCommandService
  → turn lifecycle + semantic storage operation
  → turn/attempt/change rows in one transaction
  → commit ACK
  → best-effort wake
  → one conversation SSE coordinator
  → one turn reducer
```

A snapshot and ordered conversation events are the only inputs that mutate
browser turn state. WebSocket push and BroadcastChannel frames only invalidate
the coordinator. They never carry an authoritative projection.

## Owners

| Concern | Owner |
|---|---|
| Paths, DTOs, events, retries, health policy | `contracts/conversation_sync_v3.yaml` |
| Contract generation | `scripts/gen_conversation_sync_contract.py` |
| HTTP/auth adapter | `routes/conversation_sync_v3.py` |
| Command policy | `lib/conversation_sync/command_service.py` |
| Proposed-plan and execution-handoff documents | `lib/plan_contract.py` |
| Snapshot/replay service | `lib/conversation_sync/service.py` |
| Repository protocol | `lib/conversation_sync/repository.py` |
| Atomic durable operations | `lib/storage_sidecar/operations_pkg/_turns.py` |
| Post-commit wake | `lib/conversation_sync/broker.py` |
| Browser cursor/SSE/recovery | `frontend/src/core/conversation-sync.ts` |
| Browser turn state | `frontend/src/conversation/domain/turn-store.ts` |
| Browser runtime/projection | `frontend/src/core/turn-runtime.ts` |

## Public surface

| Operation | Endpoint |
|---|---|
| Snapshot/reset image | `GET /api/v3/conversations/{conversationId}/sync` |
| Ordered replay SSE | `GET /api/v3/conversations/{conversationId}/events` |
| Create input/output pair and attempt | `POST /api/v3/conversations/{conversationId}/turns` |
| Projection CAS update | `PATCH /api/v3/conversations/{conversationId}/turns/{turnId}` |
| Execute an exact proposed plan | `POST /api/v3/conversations/{conversationId}/turns/{turnId}/plan/execute` |
| Regenerate/continue/resume attempt | `POST /api/v3/conversations/{conversationId}/turns/{turnId}/attempts` |
| Create/delete lane | v3 lane operations generated from the contract |
| Delete explicit turns | `POST /api/v3/conversations/{conversationId}/turns/delete` |
| Abort attempt | `POST /api/v3/attempts/{attemptId}/abort` |

Every operation receives authenticated `user_id` explicitly. No v2
conversation route or attempt-scoped conversation EventSource exists.
A `regenerate` attempt command supersedes the whole lane tail: every turn
after the regenerated turn, plus branch lanes rooted inside that tail, is
deleted in the same transaction. The response carries the discarded ids in
`deletedTurnIds` and the change log emits a `turn.deleted` entry, so the
initiating client and peers converge without a snapshot.

## Snapshot boundary

The snapshot reads one owner-scoped transaction and contains:

- conversation revision and ordered replay sequence;
- opaque cursor, server boot identity, and heartbeat interval;
- public conversation settings;
- `pushWithheld` — the read-side delivery-wedge signal (see Health);
- all authoritative turns and attempts required to render and reconnect.

Internal migration markers are not public settings. The browser applies
settings and turn state from this one response; it never follows with an
archive/settings fallback request.

The cursor is the exact read boundary: state at or before it is present in the
snapshot; later committed state is replayable after it.

Browser snapshot work has one in-flight owner per conversation and retains no
completed-response cache. The runtime publishes its hydration lane before it
starts work, and the coordinator publishes its snapshot flight before any
synchronous health callback or API call. A health-driven render that re-enters
`hydrate()` or `resume()` therefore joins the same request; after settlement
the lane is reclaimed. No trailing full read is needed because commits racing
the snapshot are replayed from its exact cursor.

Server snapshot arrivals with the same explicit owner and conversation key
also share one Sidecar read and stable-segment projection inside an 8 ms gather
window. The gather closes before authority execution, so a request arriving
after the read starts performs a newer read; no completed value or TTL remains.
The process-local registry is capped by `TOFU_STORAGE_RPC_CAPACITY` (hard
maximum 256), creates no worker pool, and fails open to a direct read at
saturation. Each caller still receives its own HTTP response and top-level
envelope so the request-time `pushWithheld` hint cannot leak between callers;
the shared nested authority projection is read-only during JSON serialization.
An unshared request therefore never recursively copies a conversation-sized
projection.

After generated-schema validation, the snapshot HTTP adapter uses compact
`orjson` bytes rather than Quart's stdlib encoder. Unsupported values fail
soft to the existing `jsonify` provider. Dynamic responses are still
compressed off the serving loop by the profile-aware HTTP compression policy
owned in [`modules/infra_runtime.md`](modules/infra_runtime.md); they do not
enter its static-artifact cache, so no conversation response is retained after
delivery.

## Atomic ordering

`storage_conversation_sync_heads` owns a monotonic sequence per
`(user_id, conversation_id)`. A mutation transaction:

1. validates owner, command identity, and projection CAS;
2. updates turn, attempt, event, and conversation revision rows;
3. allocates the next sync sequence under the same owner lock;
4. appends the compact change event;
5. commits;
6. publishes a wake only after commit acknowledgement.

Wake loss cannot lose state. Subscribers probe the durable log when connecting
and after heartbeat deadlines.

## Bounded changes

The permanent turn row owns the full projection. Mutations to an existing turn
carry a revision-to-revision `projectionPatch`, never another cumulative copy.
The reducer applies a patch only when base revision, target revision, operation,
and path all validate. A missing or invalid patch triggers one authoritative
snapshot recovery.

Full projections are allowed in snapshots and bounded new-turn events. A
multi-turn graph rewrite such as compaction emits a small
`conversation.activity` event with `requiresSnapshot` and re-anchors once.

`projection.activityTimeline` is the bounded execution-history sidecar for one
Turn. Runtime task events remain the raw facts; the lifecycle folds only
durable diagnostics — tool lifecycle, retry/compaction cycles, schema
isolation, model fallback, and failures — into a maximum of 128 rows and
96 KiB of serialized JSON, coalescing repeated retry cycles and correlated
tool progress. Routine phase status text and per-round model-request
bookkeeping stay out (the live-status surface and the turn trace own those).
Attempt events carry the resulting projection patch, and snapshots carry the
same document, so the browser never subscribes to a parallel diagnostic
stream. Timeline rows are display-only projections: they are not messages,
model context, tool calls, full receipts, or execution authority. The browser anchors each
warning/error row inline at its `toolCallId` or 0-based `llmRound` (never as a
consolidated tail block). Routine info-level status, tool, and model rows are
display-filtered — the inline tool blocks and live-status surface already own
those facts. A settled `context_compaction` row is the explicit exception: it
is the projection of a durable archive receipt, not a progress beat, and exposes bounded
before/after token and message accounting without embedding archived
transcript content.

Proposed-plan Markdown is bounded to 64,000 characters / 256,000 worst-case
UTF-8 bytes. The Plan protocol owns three logical durable documents: tagged
source content, its `proposedPlan` sidecar, and—after acceptance—the immutable
input-turn `planExecution` handoff. It does not create the former
`task_results.meta.plan` duplicate. Ordinary task-result, segment, and turn
content mirrors predate Plan Mode and remain under the general transcript/task
retention budget; the bound here measures Plan's logical payload and added
sidecars rather than relabeling those baseline mirrors as new Plan state.

During reconnect windows, the terminal patch appears once in attempt replay
and once in conversation-sync replay, while execution's create-pair replay
temporarily carries the handoff once. Thus the Plan-protocol peak is bounded by
six worst-case plan texts plus small envelopes; both replay logs are TTL-pruned,
and derived turn search is separately capped at 10,000 bytes. The executable
Unicode serialization budget test is in `tests/test_plan_mode.py`; replay
retention/reclaim contracts are in `tests/test_attempt_event_retention.py`.

## Proposed plan and execution handoff

A successfully completed Plan-mode task explicitly mints
`projection.proposedPlan` from its complete
`<proposed_plan>...</proposed_plan>` block. Generic projection normalization
never infers execution authority from ordinary assistant prose. New Plan turns
use the durable `planner` actor; an explicit compatible sidecar remains readable
for imports and retries. Its `planId` is a content hash, and consumers never
rediscover it from rendered HTML or a message index.

`planExecution` is server-authored. Ordinary create/attempt/settled inputs
cannot mint it, and a generic turn update may only preserve the exact sidecar
already stored by the server. Settled imports may carry a self-consistent
`proposedPlan` for compatibility, but never an accepted execution handoff.

Execution is a dedicated idempotent command. It must name the source turn,
source projection revision, and plan ID. The command service verifies all
three, requires the source to be the lane tail in the same atomic transaction
that creates the next input/output pair, stores a typed `planExecution`
handoff, and persists `planMode=false`. Continuing the discussion advances the
lane and therefore makes the earlier decision stale instead of executing it.
The same lane-local rule covers the main conversation and an expanded branch;
the frontend decision bar follows whichever lane currently owns the composer.
Legacy `endpointMode` / `endpointEnabled` inputs are discarded; they
do not select a live execution owner.

`contextMode=current` projects normal lane history plus the exact handoff.
`contextMode=fresh` projects only that handoff for model transcript history;
normal system/workspace constraints are still composed. Fresh execution never
deletes or rewrites durable conversation history.

## Replay and recovery

The cursor is opaque and scoped to owner and conversation. Native EventSource
resume uses `Last-Event-ID`. Expired cursors, sequence gaps, malformed frames,
identity mismatches, server restart, or projection revision gaps produce
`sync.reset_required` and one snapshot replacement.

Each generated EventSource URL also carries a page-scoped `streamClientId` and
monotonic `streamGeneration`. A same-page reconnect with an equal generation,
or an explicit recovery with a newer generation, synchronously supersedes the
older server subscription, wakes its heartbeat wait, and releases its exact
shared SSE lease. A delayed older generation receives HTTP 204, which stops
native EventSource retry. Already-loaded legacy pages remain readable but have
no exact-owner replacement privilege.

All SSE endpoints share the distributed-safe `TOFU_MAX_SSE_PER_PRINCIPAL`
lease ceiling. A current identified page may retire the oldest local
conversation subscription before retrying admission, so heartbeat-refreshing
proxy zombies cannot permanently starve the live UI; direct chat streams and
remote-replica leases are never silently discarded by that local choice. If
capacity still cannot be obtained, the route returns HTTP 204 rather than 429,
avoiding an automatic EventSource retry storm. Owner-generation tombstones use
the bounded browser-client registry capacity with a 128-entry floor matching
the absolute SSE cap, and carry no projection data. An admitted response whose
body is never consumed has a 10-second start deadline, so the broker entry and
shared slot are reclaimed even if ASGI never enters the generator.

Deleting a conversation atomically removes its header and turns from the active
authority, drops attempts/events/replay state, and moves a non-executable turn
graph into recoverable trash. The browser disposes its coordinator, store,
EventSource, subscriptions, and health entry. Restore and clone return through
a fresh authoritative snapshot; neither replays a browser message array. The
separate lifecycle contract is `contracts/conversation_lifecycle_v1.yaml`.

## Dispatch handshake

Database commit and worker startup cannot be one transaction. The application
closes the gap by claiming the accepted attempt, registering an executor task
without starting it, binding the task to the owner-scoped attempt, and only
then starting the worker. Any failure terminally settles the same attempt with
the complete `task_start_failed` envelope.

Only operations marked `x-tofu-idempotent-retry` retry automatically. The
generated client reuses the validated request document and command ID for
ambiguous network or declared retryable failures. Abort and non-idempotent
mutations do not enter that loop.

## Health

Visible conversation heartbeat frames own `conversation-sse` health.
`connecting` and `recovering` are transient; only `degraded` and `offline`
affect the aggregate badge. Background task streams use the separate
`task-sse` transport and cannot overwrite a conversation coordinator's state.

Heartbeats and snapshots also carry `pushWithheld` (always explicit, both
ways). It is the READ-side probe of a WRITE-side delivery wedge: while the
conversation's live task has authoritative frames withheld on storage retries
(durable-before-visible, `TaskRuntime.append_event` stamps
`_pushWithheldAt`), the withheld frames themselves can never report it, so
`routes/conversation_sync_v3.py` polls
`lib.tasks_pkg.manager.runtime.push_withheld_for_conv` and marks heartbeats
`degraded` with reason `storage-write-wedged` (distinct from the read-side
`storage-read-degraded`). The browser folds the flag into `TurnState`
(snapshot fold + heartbeat action) and the live-status block presents the
honest `storage_wedged` phase label instead of the generic waiting
placeholder; any stale livePhase on record is history while the wedge lasts.
The first post-wedge heartbeat/snapshot carries explicit `false` and clears
it. Pin: `test_push_withheld_wedge_rides_snapshot_and_heartbeat` and
`test_push_withheld_wedge_replaces_the_waiting_placeholder`.

## Extending the protocol

1. Edit `contracts/conversation_sync_v3.yaml`.
2. Add or change the semantic storage operation and atomic change capture.
3. Update `ConversationTurnCommandService`; keep routes stateless.
4. Regenerate with `python3 scripts/gen_conversation_sync_contract.py`.
5. Consume only generated browser methods and types.
6. Test wrong-owner access, CAS, idempotency, atomic replay, bounded payloads,
   reset recovery, cancellation, and disposal.
7. Run generator check, TypeScript check, focused backend/browser tests, then
   the production frontend build.

Executable contracts live primarily in `tests/test_conversation_sync_v3.py`,
`tests/test_frontend_turn_delta_sync.py`,
`tests/test_frontend_attempt_stream_vite.py`, and
`tests/test_storage_sidecar_contract.py`.

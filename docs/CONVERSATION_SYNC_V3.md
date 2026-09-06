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
the coordinator. They never carry an authoritative projection. The browser
accepts a push frame only when the pure `frontend/src/core/frame-identity.ts`
policy matches its explicit owner ID to the authenticated local owner; missing,
unresolved, or foreign identities fail closed.

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
| Atomic durable operations | `lib/storage_sidecar/operations_pkg/_turns.py` (facade over `_turns_{core,read,write,lifecycle,events,branch}.py`) |
| Post-commit wake | `lib/conversation_sync/broker.py` |
| Browser cursor/SSE/recovery | `frontend/src/core/conversation-sync.ts` |
| Browser authenticated owner lifecycle | `frontend/src/core/current-user.ts` |
| Browser push-frame ownership policy | `frontend/src/core/frame-identity.ts` |
| Browser turn state | `frontend/src/conversation/domain/turn-store.ts` |
| Browser runtime/projection | `frontend/src/core/turn-runtime.ts` |

## Public surface

| Operation | Endpoint |
|---|---|
| Snapshot/reset image | `GET /api/v3/conversations/{conversationId}/sync` |
| Older lane page at an exact replay head | `GET /api/v3/conversations/{conversationId}/turns/history` |
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


A user Turn's `contextSnapshot` is historical evidence: changing project
folders or composer controls alone never rewrites it. The project panel stages
folder edits until **Apply Changes** succeeds. A later explicit `regenerate`
then sends the input Turn's current projection plus a newly captured live
context as `inputUpdate`, guarded by `expectedInputProjectionRevision`; storage
applies that input update and creates the new attempt in the same transaction.
**Edit + resend** uses this same atomic command rather than PATCHing the input
first. Consequently an accepted regeneration shows and executes with the
updated workspace, while untouched earlier Turns continue to report the
context they actually used.

Regenerate also rebinds the reused generated Turn's durable `actor`/`kind` to
the normalized target interaction mode in that same transaction. Plan produces
`planner`/`plan`; Flow produces `assistant`/`flow_node`; Standard and Goal
produce `assistant`/`reply`. The command response and first attempt event carry
the migrated identity, so the initiating Store, peer replay, and later snapshot
all converge without presentation-layer role overrides.

## Snapshot boundary

The snapshot reads one owner-scoped transaction and contains:

- conversation revision and ordered replay sequence;
- opaque cursor, server boot identity, and heartbeat interval;
- public conversation settings;
- `pushWithheld` — the read-side delivery-wedge signal (see Health);
- when explicitly selected, `hasArtifacts` — one metadata-free existence bit;
- the authoritative Turn window and attempts required to render and reconnect.

Internal migration markers are not public settings. The browser applies
settings and turn state from this one response; it never follows with an
archive/settings fallback request.

The cursor is the exact read boundary: state at or before it is present in the
snapshot; later committed state is replayable after it.

Persisted turns decode fail-closed: a stored shape the schema no longer
accepts raises `ContractViolation` instead of silently mis-rendering.
Declared legacy shapes cross the boundary only through
`TURN_READ_ADAPTERS` in `lib/conversation_sync/service.py` — the explicit,
ordered registry of copy-on-write lifts (today: pre-`attemptId` client
observations). Every registered adapter is pinned by a legacy fixture
under `contracts/fixtures/sync_v3/` that must fail raw decode and pass
after adaptation (`tests/test_wire_fixtures.py`); a shape without a
fixture is not an adapter.

The endpoint defaults to `segmentPayload=full` and a complete Turn set for
independent and older clients. The generated browser client fixes
`segmentPayload=refs&turnWindow=tail-96&artifactHint=has-any`. The artifact
selector is an additive wire-shape opt-in: the owner-scoped snapshot transaction
uses a bounded `LIMIT 1` existence query and returns `hasArtifacts`, never
artifact metadata. `false` lets the browser commit an empty artifact read model
without the legacy artifact-list HTTP request. `true`, or a missing field from
an older server/sidecar, retains that list request. A request without the
selector receives the old response shape, and the selector participates in the
admission and snapshot-flight keys so old and new clients cannot share a
differently shaped response.

A linear, main-lane-only conversation
returns at most its newest 96 Turns, only their current attempts, and a
`turnWindow` carrying the exact older-page ordinal, `hasMore`, and durable total.
The bounded SQL reads `LIMIT + 1` through the owner/lane/ordinal index; it never
loads omitted projections merely to slice them in Python. A conversation with
any branch lane deliberately falls back to the complete snapshot until a
bounded lane directory can preserve every branch's discoverability. Thus the
optimization cannot make durable branch state invisible.

Older history uses the generated `turnPage` operation. The caller supplies its
currently applied `syncSeq`, lane, exclusive `beforeOrdinal`, and a page limit
of at most 256. Storage checks owner and replay head in the same read
transaction, reads `LIMIT + 1` oldest-first, and returns only current attempts
for those Turns. A stale head returns 409 and never publishes a page. In the
browser, identical requests share one flight, a different overlapping request
is rejected instead of queued, and identity/reference/sequence validation plus
the non-authoritative Store merge happen in one synchronous coordinator turn.
An SSE or reset that advances local sequence before publication rejects the old
page. The Surface's 80-Turn DOM window prefetches a 64-Turn page only after its
local prefix is exhausted, anchors on stable `turnId`, and reveals 20 newly
prepended Turns without replacing the render path; the remaining 44 serve the
next local window moves without another API round trip.

In the reference representation, on terminal
Turns, a uniquely matched settled `tool_use` segment keeps its ordering identity
and `roundRef` while omitting duplicate input/result bodies already owned by
the sibling `toolRounds` entry. Non-completed Turns require the sibling round
itself to be `done`; ambiguous or unfinished blocks stay full. The same
browser-only view removes opaque
`_responsesItems` / `_anthropicContentBlocks` provider replay bodies from those
completed rounds; they reconstruct future server-side model requests and have
no browser consumer. Completed `apiRounds` keep the browser's closed-world
token facts, exact CNY round total, cache-break, dispatch identity,
subscription quota, and trace facts, but omit unused USD/itemized cost fields,
stream counters, route/cache diagnostics, and pricing snapshots that are
server evidence only. Running, interrupted, truncated,
failed, or ambiguous segments and every ordered replay event remain full. The
coordinator resolves each reference and attaches the same round/input/result
objects before publishing the snapshot to TurnStore; an invalid reference is a
protocol error, never an empty tool card. The reference view is request-local
and does not mutate the shared authoritative snapshot or create another stored
format.

For terminal Turns, `snapshotProjectionRefs` removes exact stable-segment
duplicates with a global 4,096-reference ceiling. `projection.content` must be
at least 128 encoded bytes; both it and a tool round's `thinking` must save at
least 64 bytes after reference overhead and match one uniquely identified text
or thinking block. Duplicate Turn/call/block IDs, ambiguous text, active Turns,
or non-beneficial values remain inline. The browser rejects
missing/active/ambiguous sources and inline conflicts, restores `content` and
round `thinking` from those exact segments, and discards the per-Turn reference
map before TurnStore. This marker lives only in the snapshot schema, never in
writable or durable `TurnProjection`.

A terminal Turn's durable projection carries one authoritative `cost` total
beside `usage`: the lifecycle fold derives it from the accumulated usage with
the single `lib.cost` formula — the same math as the legacy done-event stamp —
and re-derives it on every projection write, so a stale carried value can
never survive next to different math. `apiRounds` remains the per-round
breakdown ledger; the finish footer and cost popover read only this top-level
total. Turns settled before the fold have no `cost`; the client-side cost
cache fills them, and that fill rides presentation state into the footer's
re-render compare so the late-landing value is not diffed away.

That same browser view content-addresses repeated terminal-Turn `toolContent` and
`results` values whose canonical JSON is at least 1 KiB. Full SHA-256 keys
address at most 256 shared documents and 4,096 references in one response;
unique, conflicting, over-budget, active-Turn, or unserializable values remain
inline. The generated schema accepts only the two declared reference fields,
full digest keys, and the bounded dictionary. Before any state publication,
the browser restores the exact shared values into shallow-copied rounds,
resolves segment references against those rounds, and discards the dictionary.
Missing documents, inline/reference conflicts, malformed keys, or a non-array
`results` value are protocol errors. Thus the wire and parse work shrink while
durable storage, the independent-client `full` response, TurnStore shape, and
replay events remain unchanged; no response cache or cross-request lifetime is
introduced.

Frozen pre-attachment Turns may still contain the same historical image twice:
one `base64` field and one data-URL `preview`. For a completed Turn with a unique
ID, positive projection revision, image index below 20, and at least 1 KiB of
recoverable encoded data under the 8 MiB binary ceiling, `refs` drops `base64`
and replaces `preview` with the generated revision-fenced Turn-image URL. Small,
active, ambiguous, malformed, or over-budget images remain inline. The
authenticated endpoint checks its owner cache scope, then the repository checks
explicit `(user, conversation, Turn, revision, index)` authority and returns
only one encoded image rather than the whole projection. A changed revision is
409; missing/foreign evidence is 404; strict base64 and magic-byte validation
fail as storage-integrity errors. Successful bytes use true PNG/JPEG/GIF/WebP
MIME, ETag, `nosniff`, and owner-partitioned private immutable caching. The scope
only partitions browser caches and never authorizes a read. Durable projections,
replay events, modern attachment refs, and the independent-client `full` view
remain byte-for-byte unchanged by this request-local compatibility projection.

Budget evidence from the 2026-08-28 largest-local-snapshot audit (594 Turns):
the independent-client view remained 11,382,693 bytes while `refs` measured
3,720,697 bytes (67.31% smaller) and 495,747 bytes with Brotli quality 2. A
generated-contract Node harness parsed, validated, and materialized all 594
content rows and 445 round-thinking rows in a 26.2 ms median; exact per-round
CNY totals matched the full view for all 540 cost-bearing API rounds.

Tail-window budget evidence from the same 2026-08-28 read-only database audit:
on the largest projection-byte linear sample (594 Turns), `refs` fell from
3,720,697 to 354,310 bytes (90.48%), Brotli-q2 from 495,745 to 34,181 bytes
(93.11%), and median snapshot service time from 151.82 to 22.79 ms (84.99%).
On the largest Turn-count linear sample (1,624 Turns), it fell from 7,599,898
to 358,490 bytes (95.28%), Brotli-q2 from 678,359 to 28,367 bytes (95.82%),
and service time from 196.85 to 18.98 ms (90.36%). A generated-contract Node
harness reduced JSON parse + schema validation + reference materialization on
that sample from 44.71 to 3.12 ms (93.02%), while restoring exactly 96 Turns.

Native MCP image blocks cross the executor boundary only after owner-scoped
media persistence. `TurnProjection.images` contains bounded
`TurnImageAttachment` references (`attachmentId`, owner-authorized `preview`,
MIME/provenance metadata), never raw base64. Snapshot/replay therefore drives
the same attachment block and preview action as uploaded images;
checkpoint/continue retains refs and regenerate clears stale refs before the
new attempt.

Tool-round counters are execution-local, not Turn identities. A checkpoint
resume keeps the same visible Turn but starts a new `attemptId` / `taskId` and
restarts `llmRound` / `roundNum`; every projected `TurnToolRound` and its
non-terminal segment blocks therefore carry the producing attempt/task. The
attempt-creation transaction freezes any legacy unstamped checkpoint rows
under the outgoing owner before installing the successor. Segment batching,
render grouping, and request-inspector links use that execution scope, with
contiguous-occurrence and counter-reset compatibility for historical rows that
predate the stamps. Continue/model-wire reconstruction uses the same ordered
identity; equal `llmRound` values from different attempts are never merged or
reordered. When one Turn contains several attempts, retained tool panels label
both coordinates (`A<n> · R<n>`) rather than presenting several requests as one
repeated round.

Checkpoint settlement and resume use the same causal replay boundary as cold
history reconstruction. The anchor records both the raw prefix boundary and
the exact retained raw positions; storage validates their types, order, count,
and bounds before creating a successor attempt. Identity-free display rows and
explicitly superseded provider-attempt artifacts are transparent. Any other
identity-bearing row with an invalid ID/name/caller/argument envelope or without
exact string `toolContent` stops the prefix, so later dependent calls are not
replayed across an invented result. `status` is a verdict, not proof of
execution: exact error/rejection/abort receipts remain replayable. A malformed
resume anchor is a typed protocol error and cannot partially mutate the Turn.

When that boundary rewinds a non-empty terminal `content`/`thinking` tail
(seamless provider prefill is impossible for it), the discarded text moves
into `TurnProjection.rolledBack` — one entry per rewound attempt, bounded to
the last four, oldest first — instead of vanishing from the render. The lane
is display-only history: it is never replayed onto the model wire and never
seeded back into `content`/`thinking`. The browser projects each entry as a
keyed `rolled-back` block rendered as a collapsed dashed disclosure
(`.thinking-block.thinking-prior` / `.content-prior`), anchored where the
tail was generated: after the retained rounds, before the terminal lanes the
resumed attempt re-streams. Empty tails leave no entry, and a malformed lane
is repaired fail-closed at projection normalization.

Browser snapshot work has one in-flight owner per conversation and retains no
completed-response cache. The runtime publishes its hydration lane before it
starts work, and the coordinator publishes its snapshot flight before any
synchronous health callback or API call. A health-driven render that re-enters
`hydrate()` or `resume()` therefore joins the same request; after settlement
the lane is reclaimed. No trailing full read is needed because commits racing
the snapshot are replayed from its exact cursor. Push and BroadcastChannel
invalidations never start a snapshot: a healthy stream already owns the
ordered projection, while a missing or stale stream reopens from its durable
cursor. Browser visibility, online, push-reconnect, periodic reconciliation,
and live-attempt wake probes use the same warm-store path; only a cold store
has no cursor and needs an initial snapshot. Only a typed reset condition
crosses the authoritative snapshot boundary.

The retained sidebar-selection bridge follows the same boundary. Reopening a
warm conversation calls `wakeConversation` and resumes from the in-memory
cursor; it does not call the snapshot hydrator merely because the conversation
became active again. The typed wake owner retains the cold-store fallback, so
an absent local snapshot still hydrates once and a declared reset still
recovers authoritatively. In a frozen 2026-08-28 trace, one large conversation
produced at least 66 generated-client `tail-96` snapshots totaling 144.4 MiB
and 10.714 seconds of server work (coalesced hidden requests excluded), with a
2.188 MiB mean response. The trace includes unknown cold opens and is therefore
an opportunity envelope, not a steady-state savings claim; the executable
browser harness proves that each warm selection now performs zero hydrations
and one cursor wake while a cold selection still performs exactly one
hydration and zero wakes.

Server snapshot arrivals with the same explicit owner, conversation, and Turn
window also share one Sidecar read and stable-segment projection inside an 8 ms
gather window. Full and bounded windows are distinct authority keys and can
never leak partial state across callers. The gather closes before authority
execution, so a request arriving
after the read starts performs a newer read; no completed value or TTL remains.
The process-local registry is capped by `TOFU_STORAGE_RPC_CAPACITY` (hard
maximum 256), creates no worker pool, and fails open to a direct read at
saturation. The flight wrapper lazily builds each derived representation once;
there are at most four named views per flight and the generated browser uses
only `refs`. Full and reference callers for the same Turn window can therefore
join the same authority read while concurrent reference callers also share hashing and shallow
projection work, with no second gather delay. A later request receives a new
wrapper and rebuilds from a fresh authority read.

Each caller still receives its own HTTP response and top-level envelope so the
request-time `pushWithheld` hint cannot leak between callers; shared nested
authority/reference values are read-only during JSON serialization. Only Turns
whose terminal tool segments are safely referenceable receive shallow copied
turn/projection/segment containers; large sibling round values remain shared.
An unshared request therefore never recursively copies a conversation-sized
projection, and no flight retains a conversation after its participants return.

Generated-schema decoding compiles one bounded table of success predicates at
process import. On the normal valid path, object predicates inspect only fields
that are actually present instead of rescanning every optional contract field,
and they allocate no diagnostic paths. A failed predicate always reruns the
canonical diagnostic traversal and returns the same complete `violations`
array as before; malformed input is never accepted on a cheaper partial check.
Python and the generated browser both use JSON value equality for `const` and
`enum`, so booleans never coerce to numeric `0`/`1` across the protocol.
The compiler retains only the generated schema and predicates, never request or
conversation values.

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

The two replay surfaces do not imply two durable AttemptEvent bodies. New
`attempt.event` change rows retain only the private attempt sequence reference;
the Sidecar reconstructs the exact public ConversationChange with one fenced
JOIN. Historical inline rows remain readable. Retention cannot delete a
referenced source; explicit turn deletion instead expires the affected change
prefix so stale cursors recover through the ordinary snapshot boundary.

`projection.attachments` contains at most 20 canonical
`TurnMediaAttachment` references. It carries display/status metadata only;
document text, video transcripts, frames, and source bytes remain in the
owner-scoped media/Knowledge authority and are resolved within a bounded model
request. Turn creation treats the browser object as untrusted, resolves every
ID under the authenticated owner, and stores the server projection. A video in
`processing` state may be committed without blocking the turn. Historical
`pdfTexts` and `videos` fields remain projection/read compatibility for old
turns, but new unified uploads write `attachments` only.

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

The per-generation-attempt timing document is the separate user-perceived
latency authority; `projection.timingTrace` is its terminal mirror on the
current Turn. The task event fold owns server spans and a coalesced history of
the exact live phase prompts; terminal settlement freezes them into the attempt
row and Turn mirror before reconstructible event rows can expire. The generated `recordPerception`
command appends only schema-closed browser receipt metadata (phase/terminal
paint and transport degrade/recover), idempotently by owner + attempt +
observation ID. It never accepts transcript content and never changes execution
status. The document is capped at 256 spans, 128 gaps, 128 prompt rows, 64
browser receipts, and 96 KiB with explicit dropped counts. Each receipt update
writes only the small attempt document under the same lock as terminal
settlement. It does not advance Turn/conversation revisions, emit a sync patch,
open a parallel stream, or become browser state authority. Full semantics and
cross-clock caveats are in `TURN_TRACE_CONTRACT.md`.

Proposed-plan Markdown is bounded to 64,000 characters / 256,000 worst-case
UTF-8 bytes. The Plan protocol owns three logical durable documents: tagged
source content, its `proposedPlan` sidecar, and—after acceptance—the immutable
input-turn `planExecution` handoff. It does not create the former
`task_results.meta.plan` duplicate. Ordinary task-result, segment, and turn
content mirrors predate Plan Mode and remain under the general transcript/task
retention budget; the bound here measures Plan's logical payload and added
sidecars rather than relabeling those baseline mirrors as new Plan state.

During reconnect windows, the terminal patch is exposed once in attempt replay
and once in conversation-sync replay but encoded once durably, while
execution's create-pair replay
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

Browser stream residency follows the same user-visible value boundary. The
active shell keeps its stream, and an inactive shell keeps one only while its
authoritative TurnState contains a `pending` or `running` Turn. Because changing
the retained shell's `activeConvId` does not itself emit a TurnState frame, the
shell must explicitly ask Turn Runtime to re-evaluate the previous and current
IDs after each selection or `newChat` transition. That check is O(1) in warm
stores: a settled outgoing shell closes immediately, while a background live
Turn remains connected. The resource-budget pin visits 32 settled conversations
and requires exactly one open source after every lease transfer, then zero after
disposal; see
`test_push_invalidations_do_not_reload_a_live_conversation_snapshot`.

Deleting a conversation atomically removes its header and turns from the active
authority, drops attempts/events/replay state, and moves a non-executable turn
graph into recoverable trash. The browser disposes its coordinator, store,
EventSource, subscriptions, and health entry. Restore and clone return through
a fresh authoritative snapshot; neither replays a browser message array. The
separate lifecycle contract is `contracts/conversation_lifecycle_v1.yaml`.

## Dispatch handshake

Database commit and worker startup cannot be one transaction. The application
closes the gap by claiming the accepted attempt, registering an executor task
without starting it, and binding the task to the owner-scoped attempt. Bind is
durably `pending`: it means the task is accepted in the bounded local Agent
scheduler, not that CPU work has begun. The physical worker entry executes the
owner- and task-fenced `turn.attempt.start` command, which is the sole normal
`pending` -> `running` boundary; the first exact task event may perform the same
transition only as a compatibility repair. Queue refusal or worker-entry
failure terminally settles the same attempt with the complete
`task_start_failed` envelope.

Canonical local-executor commands persist `dispatch_mode=conversation_executor`
and the server-authored request-start cancellation watermark in the same
transaction as the pending attempt. A serving-loop owner queries the partial
`pending + empty taskId + dispatch mode` index in batches of eight after a
1.5-second grace period. Only a process role with the declared task-worker
capability (`worker` or personal-mode `all`) owns this loop; API and scheduler
replicas never claim executable attempts. The owner rehydrates the durable
principal, Turn, config, and original cancellation watermark, then enters the
normal claim/bind/start path.
Empty `dispatch_mode` is reserved for external or manually owned lifecycle
callers and is never interpreted as permission to start billable model work;
pre-contract attempts therefore also fail closed instead of being guessed.

The one-shot claim carries a process-stable dispatch owner. The same process
may retry an ambiguously acknowledged claim, while a different owner loses the
compare-and-set. A fixed 256-stripe in-process lock keeps claim through task
registration/bind single-winner without memory growth from command IDs. The
task is still bound before spawn, so `taskId=''` remains proof that no executor
was launched and is the only state eligible for automatic dispatch recovery.
The original cancellation watermark, rather than recovery time, preserves a
Stop arriving anywhere in the request-to-commit gap.

Snapshots retain `AttemptRecord.taskId`, so a browser that reloads or
reconnects derives `pending + taskId` as “waiting for a server Agent execution
slot.” A pending attempt without a task binding is still in dispatch
preparation, and a running attempt has acquired a worker. Transient phase
frames may add detail but never redefine those durable meanings.

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

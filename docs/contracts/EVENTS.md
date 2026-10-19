# Emitting streaming events — contributor guide

> **Audience: anyone adding or changing backend code that emits an SSE / push
> event.** If you are *consuming* the event stream (building a frontend / SDK),
> read [`HEADLESS_API.md` §3.6.1](HEADLESS_API.md#361-streaming-event-contract--the-frontendbackend-sync-interface)
> instead — that documents the wire vocabulary a client sees.

The event vocabulary is a **single, declared, versioned contract**. There is
ONE source of truth — [`lib/agent_core/events.py`](../../lib/agent_core/events.py) —
and ONE way to construct an event. This guide is the rule set that keeps it that
way.

---

## Request Inspector evidence identity

New request snapshots and tool-start occurrences carry `inspectorRef =
{taskId, roundNum, turn}`. `roundNum` is one-based; `turn` is an opaque
invocation token, stable across increasing rounds and renewed on a counter
reset. Consumers pass the reference unchanged to the existing owner-authorized
request payload endpoint; they must not infer a parent or child stream for a
stamped card. Legacy unstamped cards retain their existing lookup only.

Problem: phase-only `round_usage` and `tool_wire_projection` tags could not join
snapshots carrying opaque invocation tokens, hiding retained cost evidence.
Decision: producers share the matching current `inspectorRef.turn`; the wire
observer captures it before dispatch. References from another task or round
retain the legacy phase fallback. Alternative rejected: joining only by round
number can attribute restarted or delegated calls to the wrong invocation.

`lib/tasks_pkg/round_evidence.py` builds both root and child evidence. Finalized
request messages are projected directly; a failed request-body reconstruction
is labeled `degradedReason=request-build-fallback`. Post-tool state is recorded
after ordered model-visible results append, with the same reference. Child
persistence uses the existing acknowledged event writer and restart-safe event
sequence, never parent SSE fan-out. The existing chronological delta baseline
and tools dictionary remain authoritative for bounded incremental storage;
50 increasing 1 KB rounds with request/state pairs stay below 160 KB in the
focused budget specification. Cold projections preserve the reference.

Swarm execution admission, not pool submission, emits the worker's running
phase. A scheduler-level retry publishes `swarm_agent_phase` with
`status=phase='pending'` and `waitReason='capacity'` before reacquiring its
permit. That clears the prior attempt's live clock without rewriting its
result or declaring the child failed; the next admission publishes running.
In-run provider retries retain their active phase and execution permit.
`swarm_agent_phase` declares `detailKey`, `detailArgs`, and `attempt` for the
same localized retry projection as the root conversation. Network recovery
uses `stream.retryReason.networkRecovery` inside `retryCooldownWait`, without
an attempt counter: no model request has failed while waiting for admission.
The following recovery phase restores `stream.phase.waitingForModel`.

## Swarm message receipt evidence

`swarm_message_receipt` is an owner-scoped projection of a committed exact-run
mailbox transition, not a consumption acknowledgement by itself. Its schema is
registered in `lib/agent_core/events.py`; semantics and bounds are owned by
`contracts/swarm_messages_v1.json` and [retained agents](../RETAINED_AGENTS.md).
Exact run/agent/message identity reaches task replay and conversation Swarm push.
Existing per-agent spawn snapshots carry receipt rows plus mailbox revision for
reload and monotonic merge; missing push is not missing submission.

Both paths carry exact bounded `content`, `kind`, and one-based mailbox append
`messageSequence`, alongside submission and input-round evidence. Sequence orders
messages within one mailbox only; it is not a consumption receipt or a global
clock. Legacy missing content stays unavailable. Bodies never enter unrelated
telemetry/logs and do not grant authority or create another poller.

## 1. The rule

**Problem:** Tool producers already stamp `attemptId` before publishing their
round, while field validation must also accept that identity before delivery.
**Decision:** Optional string `attemptId` belongs to the shared task-event
envelope, declared once in `lib/agent_core/events.py`; generated event clients
inherit it. It preserves the producer's identity and never grants authority or
permits a consumer to infer a different attempt. **Alternative considered:**
stripping the producer stamp would lose scope on replay and allow rounds with
reused tool-call IDs to collide. Validation still rejects a non-string stamp.

> **Never write a raw `{'type': '...'}` dict for a streaming event. Always
> construct it with `build_event(EventType.X, ...)`.**

```python
from lib.agent_core.events import EventType, build_event
from lib.tasks_pkg.manager import append_event

# ✅ CORRECT
append_event(task, build_event(EventType.PHASE,
                               phase='llm_thinking', detail='Working…', round=1))

# ❌ FORBIDDEN — a raw literal reintroduces the implicit contract we removed
append_event(task, {'type': 'phase', 'phase': 'llm_thinking',
                    'detail': 'Working…', 'round': 1})
```

`build_event(type_, **fields)` returns `{'type': type_, **fields}` and is
**byte-for-byte identical** to the literal — Python preserves keyword-argument
insertion order. The conversion changes the *construction site*, never the
wire output. So there is no behavioural cost to following the rule, only the
benefit that every emission references the declared vocabulary. It is also the
write-side gate: constructing an UNREGISTERED type raises
`EventContractError` under pytest (`TOFU_EVENT_SCHEMA=strict`) and warns in
production — see §Versioning.

### Scope: what this rule covers

This rule governs the **chat / agent task event stream** — the ~41 registered
types flowing over `/api/v4/tasks/{id}/stream` and the `chat` push channel.
It does **not** govern
unrelated `TaskRuntime` channels that define their own small private vocabulary
for a non-chat feature (e.g. the paper/translate runtimes emitting
`{'type': 'chunk'}` on their own channel — see CLAUDE.md §14). Those are
self-contained producer↔consumer pairs, not part of the shared task-event
contract, so they are not registered here.
For the private `translate` push channel, running `partialByRound` objects merge
changed block keys within the pending activity; an explicit empty object resets
the preview index. Receivers derive joined narration without reasoning blocks.
Legacy whole-turn `partial` frames remain supported. Preview frames never write
authoritative Turn state; completion hydrates committed translation facts.



**Carve-out — the production stage grammar IS registered.** The five event
types emitted by the single seam `lib.production.stages.run_stages` —
`stage_started`, `stage_done`, `stage_retry`, `stage_skipped`, `stage_failed`
— are capability-agnostic by construction (slides, longform-report, research
and motion-video all ride that one runner), so they form ONE shared
production vocabulary:

| type | fields | meaning |
|---|---|---|
| `stage_started` | `stage`, `index`, `total`, `attempt` | attempt begins |
| `stage_retry` | `stage`, `attempt`, `errors[≤4]` | gate rejected; next attempt follows |
| `stage_done` | `stage`, `index`, `total`, `elapsed_s` | stage artifact committed |
| `stage_skipped` | `stage`, `index`, `total`, `resumed:true` | checkpoint reuse |
| `stage_failed` | `stage`, `index`, `total`, `detail`, `errors[≤4]` | attempts exhausted |

`stage_checkpoint_invalidated` and per-capability extras (`page_authored`,
`final`, …) stay private. Three exception groups fold capability extras INTO
the client-facing WorkUnit projection: the slides capability's
`page_rendered {page, total}` reduces to a `pages {total, rendered, rev,
href}` block (docs/contracts/API_CONTRACT.md §2.18) driving the streaming thumbnail
gallery, and the interactive review gates — slides `outline_gate` /
`outline_gate_resolved` and motion `storyboard_gate` /
`storyboard_gate_resolved` — reduce to an optional `pendingGate` block
(answered through the shared human-response endpoint, never through a new
event channel). The client still consumes ONE projection and never parses
raw capability events. The client-facing lifecycle derived from this
grammar is the WorkUnit projection (docs/contracts/API_CONTRACT.md §2.18); the
terminal truth stays the task snapshot, never an event.
 The conversation UI consumes the
v3 turn projection/event protocol described below; do not add a second task-SSE
consumer to make a new chat field visible.

Turn-native conversation synchronization is a separate declared protocol. Its
`turn.upsert`, `turn.patch`, `turn.deleted`, `attempt.event`,
`conversation.activity`, heartbeat, and reset envelopes are owned by
`contracts/conversation_sync_v3.yaml` and
[`CONVERSATION_SYNC_V3.md`](CONVERSATION_SYNC_V3.md), not by `EventType`.
Those events are appended transactionally by the Storage Sidecar and consumed
by the one conversation coordinator; do not emit them with `build_event` or
add a second frontend stream.

Swarm push adapters add `swarmId` to the conversation-channel envelope: it is
 the original session task ID, preserved by restart recovery and follow-up
waves. The envelope's `taskId` remains the conversation subscription key.
`swarmId` is transport routing metadata, not a new task event field; adapters
must not mutate the event delivered to the task sink. Consumers fence live
telemetry by this identity and the launch roster, never by conversation key
alone. Persisted launch receipts carry the same identity as `swarm_run_id`.
Several launch rosters may share that session/task ID. A settled earlier roster
cannot fence a later roster's frames. `swarm_agent_tool_call.callId` is the
minted invocation occurrence ID shared by start, finish and durable timeline
projection, not the provider's potentially recycled call ID.

An `attempt.event` has one exact durable body, not two. New Conversation Sync
rows store a private `(attemptId, seq)` reference to the already-committed
AttemptEvent plus `{}` in their JSON slot; a single fenced LEFT JOIN rebuilds
the byte-equivalent public ConversationChange. Historical rows have a NULL
reference and keep their inline envelope. Missing/mismatched references are
integrity failures. AttemptEvent TTL and superseded-attempt cleanup must skip a
retained reference; sync pruning releases it. Turn deletion/compaction expires
the referenced replay prefix before removing the event source, making older
cursors re-anchor by snapshot instead of receiving a partial event sequence.

### Push-channel frames (declared contract)

Authenticated local socket FIFOs retain accepted events until delivery or actual
disconnect, with no count/byte/frame drop or pressure-disconnect policy. One
background-encoded immutable JSON range is shared across matching sockets;
segmented anonymous files retire with their last frame. Liveness and correlated
RPC lanes keep sole-writer priority. Encoding, file and socket failures remain
explicit transport failures; this temporary backlog adds no durable replay
authority. See [resource control](../RESOURCE_CONTROL.md#push-connection-demand).

Owner-scoped push-hub frames outside the shared chat vocabulary are declared
in the same authority file as stream events: the `PUSH_FRAME_SPECS` registry
in `lib/agent_core/events.py` holds each frame's channel, task-id semantics,
prose fields, and machine-readable `FieldSpec` schema. Emitters construct
frames through `build_push_frame` (the same construction gate as
`build_event`: strict under pytest, warn in production), and
`scripts/gen_event_contract.py` mirrors the registry into
`frontend/src/api/event-contract.generated.ts` (`ContractedPushFrame`), where
`frontend/src/core/frame-identity.ts` narrows unknown payloads to the
declared union. Never hand-assemble a declared frame as a raw dict.

### Private non-chat push receipt: Codex reset availability

The owner-scoped `oauth` channel has one passive account-state receipt outside
the shared chat vocabulary. Task ID `codex-reset` and event type
`codex.reset_offer.updated` carry `{provider: "codex", reset_offer}` after the
bounded earned-reset daemon settles. `reset_offer` is byte-compatible with the
projection on `GET /api/v4/oauth/status`; it is already length-bounded and
contains no token or raw account ID. Push Hub owner filtering is mandatory.
The frame is a low-latency completion receipt, not durable authority: consumers
must retain a bounded HTTP reconciliation path for a lost frame and must never
redeem a credit from the event.

### Private conversation wake hints

The owner-scoped `notify` channel carries best-effort catalog/Conversation Sync
wake hints outside the shared task vocabulary. Its task ID is the conversation
ID (`folders_changed` uses the `__folders__` sentinel: folders are not
task-scoped, and clients subscribe channel-wide). `conv_changed` carries
`{convId, userId, rev?}`; `conv_deleted` carries `{convId, userId}`;
`folders_changed` carries `{userId, deletedFolderId?}` — with
`deletedFolderId` every device unassigns local conversations off the removed
folder. Push Hub owner filtering and the browser's frame-owner check
are both mandatory. A positive integer `rev` is only an optimization hint for a
content change: the browser may suppress a catalog read after authoritative
TurnState reaches that revision. Metadata mutations omit `rev`; zero, malformed,
unknown, overflowed, or still-stale hints retain a full catalog reconciliation.
The frame never contains transcript content and is never projection authority;
loss, duplication, reordering, or publication failure is repaired by the
ordered Conversation Sync stream and bounded visibility/periodic reconciliation.

### Private model-routing wake hint

The owner-scoped `notify` channel also carries `model_routing.changed` under
the `__model_routing__` sentinel task ID after every successful model-routing
v2 aggregate commit (settings saves, provider/registration flows, world-catalog
sync, pricing/intelligence auto-update). The frame carries `{userId, rev?}`
where `rev` is the committed aggregate revision — an optimization hint only:
the Settings live-sync suppresses a reload it has already reached and
reconciles exclusively over `GET /api/v4/model-routing`. Push Hub owner
filtering and the browser's frame-owner check are both mandatory. A client
holding an unsaved Settings draft ignores the hint until its next save or
open; loss or duplication is repaired by the next settings open, the
push-reconnect revalidation, or a manual refresh.

`turn.compact` is deliberately represented by one small transactional
`conversation.activity` change whose payload is
`{requiresSnapshot: true, conversationRevision}`. Compaction can delete a turn
graph closure, insert a summary, rewrite ancestry, and patch retained
projections atomically; no bounded delta can faithfully describe that mutation.
The coordinator therefore performs one authoritative snapshot re-anchor, and
the replay log never copies the folded transcript or large retained turns.

Provider microchunks are not individually observable contract events. The
stream manager emits the first text delta immediately, then losslessly merges
later content/reasoning for at most 100 ms or 256 characters, flushing before
structural boundaries and assigning sequences only to merged events.

Managed root executors use the task-wide delivery contract
[`task_event_delivery_v1.json`](../../contracts/task_event_delivery_v1.json).
The producer freezes projection inputs, assigns the sole event sequence and
admits bounded pending work; it does not wait for each authority write. A lazy,
launch-sized coordinator serializes each task's writes and push, while ready
tasks share workers fairly. The committed revision stays on the live task;
queued snapshots rebase against it and never read future executor content.
Progress frames reuse only detached structural inputs. Completed lanes release
those snapshots and idle workers retire.

Problem: provider-only isolation still joined the delivery thread at socket EOF,
and ordinary structural frames performed synchronous storage on the executor.
Decision: one task FIFO crosses model/tool event boundaries without an EOF
join; terminal events, registered interactions, background-command consumption
receipts and explicit forced tool/round checkpoints retain durability fences.
Birth, adopter, Flow and child publication ports keep their existing boundaries.
Alternatives rejected: unbounded fire-and-forget loses failure/teardown ownership;
a second filesystem journal creates another authority; dropping a lagging FIFO
loses unique diagnostics; mutable task closures project future state into old
frames. The configured storage backend remains the only restart authority.

Admission is process-local, not durable acknowledgement. Crash recovery uses the
last committed frontier. Accepted work is never evicted for queue pressure:
pending and in-flight item/byte ceilings produce finite backpressure and explicit
admission failure. A failed head retains its exact snapshot and blocks followers
until repair. Only a proven stale/superseded authority verdict revokes the tail.
Push and hot task replay are bounded by one exclusive committed visibility
cursor; an uncommitted terminal frame cannot report completion. Failed terminal
barriers retain the exact event/carrier through the existing maintenance owner;
repair verifies the durable carrier before reopening replay. Provider-only legacy
ports retain their own bounded ingress receipt/failure contract.

At the ordered authority writer, a structural frame carries the exact task event and
a revision-checked projection patch in one `turn.event.record` transaction; it
does not carry a second cumulative projection inside the event payload. The
running executor retains one last-applied projection/revision under a task-local
lock. After the shared stable-segment normalizer, it also supplies private
boolean stability evidence; absent evidence keeps replay correct but makes the
Sidecar normalize once at the next structural boundary. A stale patch rebases
once from authority, and a rejected/terminal attempt clears that state and
plants the existing cooperative-abort fence. Coalesced progress frames carry
their attempt-liveness fence inside the standalone event append
(`require_attempts_live`, enforced by the storage authority in the append
transaction — see `docs/STORAGE_TOFUDB.md` → "Stream-append attempt fence");
a stale verdict rejects the row outright and plants the same cooperative-abort
stamp. This replaced a per-frame pre-read that cost one authoritative round
trip per visible delta (2026-09-18 stream-churn incident).

Those exact durable `projectionPatch` payloads may also serve as the live
Turn's bounded physical reconstruction head; this creates neither a second
event vocabulary nor a second replay copy. One owner/attempt/revision-fenced
checkpoint supplies the base, and at most 64 patches / 1 MiB may follow it.
Rollover writes a new checkpoint, while terminal settlement and recovery write
the complete Turn projection and remove the head. Attempt-event retention must
therefore skip every attempt referenced by checkpoint/head metadata; a missing,
gapped, duplicated, misbased, or oversized chain is an integrity failure, never
a best-effort projection.

Post-settlement task observers are an exact exception to the carried attempt
transaction: `round_committed` and `preference_learned`, when the task is
already terminal, persist through standalone cold replay before live push.
Their producers own dedicated settled-Turn CAS patches, so they never re-enter
the closed attempt. No late delta, tool, phase, or lifecycle event shares this
exception; those still hit the stale-attempt fence and cooperatively abort.

Detached swarm frames are the second member of that exception: `swarm_phase`,
`swarm_agent_phase`, `swarm_agent_progress`, `swarm_agent_complete`, and
`swarm_agent_tool_call`, again only when the task is already terminal. A
spawned sub-agent outlives its spawning turn by design — the turn settles the
moment the main agent returns — yet its panel/progress frames are still routed
to the now-terminal spawning task dict. Without this exception each such frame
re-entered the settled attempt, was rejected by the stale-attempt fence, and
planted the cooperative-abort stamp on the shared task; the swarm's own
`abort_check` then read that stamp and force-cancelled every live sub-agent
(the mtvj1lls7knjg5 false-positive abort). A swarm frame is a pure
panel/replay observer, never a turn-authority write, so it persists standalone
exactly like the commit-round and preference observers.

### Model request timing boundaries

Task delivery captures optional `executionAtMs`, `executionOriginAtMs`, and
`executionClockId` before observer enqueue. Their epoch-anchored monotonic
axis is server execution evidence; persistence and browser delivery cannot
re-stamp it. A relayed child retains its producing clock. Both authorities
validate positive safe-integer clocks and retain the execution timestamp in
the payload-free event metadata spine. Legacy events retain write-time
metadata. `emittedAt` remains a wall-clock transport receipt anchor. See
[Turn Trace](TURN_TRACE_CONTRACT.md) for domain changes and missing evidence.

`model_request_start` / `model_request_complete` distinguish an optional
`requestLevel=logical` wrapper from `requestLevel=provider_attempt` children.
Children have unique `spanId`, the wrapper's `parentSpanId`, a 1-based `attempt`
ordinal within that logical request, and `attemptScope=dispatcher`. The latter
means one invocation of `stream_chat` / `async_stream_chat` after admission;
transport-internal same-key HTTP retries and route switches remain inside that
span. It must not be displayed as a count of individual HTTP requests. No-slot
polling, local preparation and admission waits do not create provider children.
Legacy events with no level retain legacy interpretation, not proof of physical
HTTP granularity. Request tags and round attribution are shared by the pair.

Each invoked child completes before dispatcher retry notification/backoff and
before the next child starts, including errors, aborts, cancellation and typed
incomplete streams. Completion `durationMs` uses monotonic elapsed time; epoch
`emittedAt` remains a timeline anchor only. Logical duration includes admission,
retry waits and any floor resend. Child diagnostics include bounded model and
provider identifiers, typed status/error kind and numeric HTTP status, plus an
optional one-way masked credential hint (`keyHint`, the same `first4…last4`
`secret_hint` label the Settings key card renders — a recognition label, never
usable secret); never usable keys, endpoint URLs, headers, raw provider bodies
or exception text. Observer
exceptions cannot change dispatch results. Task callbacks use the existing
provider-ingress isolation token/queue, never synchronous storage on ingress.
These diagnostic boundaries do not change Turn settlement or slot ownership.

### Cache accounting evidence


Public conversation snapshot settings omit `_mcpWireDeclarations`. Client settings replay cannot reset or replace that private declaration slot; trusted owner-scoped freeze/reset CAS remains its only mutation boundary.
`round_usage.tokensIn` uses `lib/cost.py:split_input_tokens`: OpenAI prompt totals already include cache, whereas Anthropic residual input adds reported cache read/write. Zero is a measurement, not missing data. `request_span_id` / `request_dispatch_seq` survive usage normalization and identify the provider dispatch, including charged failed attempts.

Problem: worker reuse and overlapping child calls selected unrelated previous reads. Decision: cache tracking binds explicit owner/conversation/task lifecycle and freezes the completed same-lane predecessor before detection updates state; detector and breakdown share it. Child/inline/transient invocations neither borrow nor publish root baselines. Retain one completed snapshot plus active invocations (100 sequential-turn regression), not one entry per task. Cold-start siblings await baseline initialization outside the global lock; same-task recursive retries share outer lifecycle ownership, while later executions use distinct identities. Every release path detaches pending L2 ROI exactly once and emits `no_following_round` outside the state lock. Reject thread identity as turn identity. The legacy durable conversation read slot remains a best-effort cold-process fallback without cross-process task lineage; it is not proof of a particular predecessor or provider cause.

Persisted `cacheBreak.reasons[]` keeps simultaneous changes as `{code, detail, evidence}`. Evidence is `wire_verified`, `source_only`, `provider_usage`, or `inferred`; read-drop facts additionally carry `prevRead`, `read`, `readDrop`. Matching local hashes cannot prove server eviction. `writeBreakdown` marks `allocation: "estimated"`, `totalsSource: "provider_usage"`, `envelopeMaxTokens`, `prevRead` and `read`: totals are provider reports, body/envelope attribution is a bounded allocation, not measured token ranges. Verify with `tests/cache/test_cache_accounting_lifecycle.py`, `tests/tasks_pkg/test_write_breakdown.py` and `tests/tasks_pkg/test_round_usage_live_projection.py`.

### Activity timeline projection

Execution diagnostics use the registered task-event vocabulary as facts and
one cumulative Turn sidecar as presentation. Tool lifecycle,
`tool_schema_rejected`, retry `phase` cycles (and any phase carrying an HTTP
error status), the `compaction` → `compaction_done` archive/receipt pair,
`model_fallback`, failed/aborted
`model_request_complete`, and terminal errors are folded by
`lib/turn_activity_timeline.py` into `projection.activityTimeline`. The fold is durable-before-visible through the ordered
`record_task_event` writer. In-flight diagnostics can appear first through task-memory
SSE replay; after successful provider-boundary convergence, reconnect/cold
snapshot catches up from the cumulative projection without a second task-SSE
consumer.

The compaction pair becomes one inspectable Turn row keyed by `archiveId`; its
settled form carries the estimated token/message counts before and after plus
the reduction percentage. The transient `phase=compacting` beat remains
progress text and is replaced by that receipt row rather than rendered twice.
Problem: archive-less preflight refusals appeared at the conversation start.
Decision: `compaction_done` may carry producer-scoped `anchorBlockId` and
`anchorPosition` captured before planning. The fold reuses this witness when
no start/phase exists; an existing start/phase wins. Legacy events without a
witness keep conservative placement. Reject inferring boundaries from later
replay segments or inventing a running phase when no summary call happened.

Each entry carries a 0-based `llmRound` display anchor — its own 1-based
`R{n}` request-tag round minus one when present, else the last model-request
round tracked as events flow through the fold — so the browser splices every
warning/error row inline under its tool block or model round instead of
rendering one consolidated timeline block.

The sidecar deliberately excludes the two channels that already have a home:
routine `phase` status text (working/thinking/waiting/startup beats — the
live-status surface owns them, persisting them would render the same fact
twice) and per-round `model_request_start` / successful request completion
bookkeeping (the turn trace owns timing). A model request earns a row only
when it fails or is aborted.
Model completion diagnostics additionally carry credential-free `routeId`,
`routeMode`, `routeDecision`, and `failureStage` fields. Their bounded
`observerIsolation` receipt names the provider-ingress contract and counts
physical provider dispatches, memory-local events, and coalesced checkpoint
requests; it carries no content. Repeated recovery
incidents with an explicit `backoff_s` coalesce as counted occurrences whose
`durationMs` is the sum of actual backoffs; their first-to-last wall envelope
must never be presented as one continuous wait.
 The wire boundary re-isolates the same malformed
schema on every model dispatch of an attempt; repeated `tool_schema_rejected`
facts with identical tool, reason, and detail coalesce into one counted row
per attempt rather than one row per request.

`tool_wire_projection` is a separate bounded Request Inspector diagnostic.
Its `parentSpanId` and optional `dispatchSeq` join to public usage
`request_span_id` / `request_dispatch_seq`; these are diagnostic identities,
not credentials or execution authority. The inspector associates a projection
with an attempt only when this identity resolves uniquely. Legacy records may
use a unique one-attempt/one-projection round, never a last-retry-wins guess.
Multiple physical projections within a dispatch remain explicitly ambiguous.
It records the ordered final provider tool names, schema-token estimate, resolved
discovery backend, explicit budget, budget compaction/omission names, and an
opaque fingerprint of the exact cache-relevant tools bytes after provider
projection. It is deliberately not rendered into the Turn activity
timeline, does not duplicate full schemas, never enters model context, and
grants no execution authority.

The sidecar is not a second event authority and its rows are not synthetic tool
calls. It is capped at 128 entries and 96 KiB of serialized JSON; repeated
wait/retry/schema rows coalesce by span, tool progress updates the matching
tool row, and old low-priority status rows are reclaimed before failures and
model switches. It never enters the LLM transcript or grants tool authority.
The legacy `error` event remains a terminal compatibility frame; current fatal
paths normally settle with `done.error`.
Before a task terminal frame enters durable append/push, its private operational
execution session settles all live resources. A failed non-recoverable release
rewrites the proposed terminal success to `done.error`; recoverable billing or
TTL lease debt is recorded as deferred without changing the truthful task
outcome. The execution receipt contains no prompt, output, owner, or request ID
and is operations evidence, not a second event authority.

### Delivery vs construction

Task frames share a typed optional delivery envelope (`taskId`, `conversationId`,
`requestId`, `seq`) and scheduler presentation metadata, composed once in the event registry
and generated into every contracted client event. Presentation cannot grant
execution authority. Child/graph forwarding and replay validate the already-stamped
frame without stripping identity or bypassing the ordinary payload gate.

`build_event` only *builds* the dict; you still deliver it through the existing
chokepoint:

- From the orchestrator / managers: `append_event(task, build_event(...))`.
- Convenience one-liner (build + deliver): `emit(task, EventType.X, **fields)`
  — wraps `build_event` + `append_event`. Use it for new call sites; the
  explicit two-step is fine where the surrounding code already holds
  `append_event`.

### Push transport identity is not task-event identity

**Problem:** a task sink stamps a swarm event with an execution `taskId`; mirroring
that same dict over conversation push must not replace the subscription address
or silently make the browser search for an execution as a conversation.
**Decision:** `PushHub.push_event(channel, task_id, payload, owner_user_id=...)`
owns `channel`, routing `taskId`, and the private owner marker. Payload fields
cannot override them. When payload `taskId` differs, the public transport keeps
it as `executionTaskId`, without mutating the caller's durable event. The
`PUSH_TRANSPORT_ENVELOPE` registry generates the browser `PushTransportEnvelope`;
`conversationId` is the canonical conversation identity for stamped task mirrors.
Consumers select the conversation with `conversationId` first, read legacy
`convId` or routing `taskId` only for compatibility, and use execution/run evidence
rather than that routing key for launch ownership. Owner filtering remains
mandatory for exact and wildcard subscribers and distributed-bus delivery.

Explicit execution/attempt identity fences destination selection even when no
`swarmId` is present; contradictory scope or ambiguous roster-only ownership fails
closed. Repeated spawn-tool ids rebase one-to-one by scoped launch evidence;
provider id equality alone is never launch identity. Explicit run evidence stays
separate from execution aliases; absent-launch creation requires the current live producer,
not a historical attempt retained on a regenerated Turn.
**Alternatives rejected:** a frontend-only alias would leave exact subscriptions
misrouted; dropping the producer id would corrupt execution provenance; changing
all task events' `taskId` to a conversation would weaken attempt/run fences.
Executable coverage: `tests/agent_core/test_push_fanout.py`,
`tests/swarm_panel_authority.test.cjs`, and `tests/visual/test_visual_swarm_activity.py`.

### The PHASE event has its own typed pair — use it

The `phase` event is the stream's **status-text channel** ("Retrying…",
"Sent to kimi-k3…", "Compressing context…") — the pushes a user actually
reads while a turn runs. Its `phase` field is a declared sub-vocabulary with
its own registry (the `Phase` constants + `PhaseSpec` catalogue in
`lib/agent_core/events.py`) and its own constructors:

```python
from lib.agent_core.events import Phase, emit_phase

# ✅ CORRECT — one interface, registered value
emit_phase(task, Phase.RETRYING, detail='…', detailKey='stream.phase.retryGeneric',
           detailArgs={'model': label, 'attempt': n}, attempt=n)

# ❌ FORBIDDEN — a raw literal bypasses the registry (the drift test fails)
append_event(task, {'type': 'phase', 'phase': 'retrying', 'detail': '…'})
```

Host-local root-task queueing uses `Phase.EXECUTOR_QUEUED`. Its payload carries
`queuePosition`, `queued`, `active`, `capacity`, and `waitSeconds`, mirrored in
`detailArgs` for localization. It is emitted only while the bounded Agent FIFO
still owns the task and is ordered before the worker-entry phase. Do not reuse
it for provider throttling or API quota; those waits remain `retrying` with a
typed reason.

Retry/wait phases distinguish quota classes by their recovery profile. A
transient per-key 429 is `retrying` + `detailKey=stream.phase.retryRateLimited`
(queued retry, recovers in seconds). An OAuth **subscription-quota** window
(Codex `usage_limit_reached`, resetting at a known time hours/days away) is NOT a
transient 429: the dispatcher tags its retry with reason `Subscription quota
reached`, which maps to `detailKey=stream.phase.subscriptionQuotaReached`
(regardless of the 429 status), and an all-slots-cooling wait whose dominant
slot `cooldown_reason` is `'quota'` surfaces as `Phase.WAITING_MODEL` +
`detailKey=stream.phase.waitingQuotaReset`. Billing exhaustion (`Key balance
exhausted`) stays a separate reason. The sidebar "rate-limited" chip excludes
the quota keys so a multi-hour subscription hold is never rendered as 限流.

Flow role phases may additionally carry `model` plus a structured `modelRoute`
with `selectedModel`, `resolvedModel`, `role`, `tier`, and `kind`. A changed
route emits `detailKey=stream.phase.modelRouted` before dispatch, and the
manager's current-phase snapshot must preserve the same fields so raw SSE,
reconnect, and polling render identical disclosure. This is role routing, not
provider fallback; `model_fallback` remains a separate event and timeline fact.

* `emit_phase(task, Phase.X, **fields)` = `build_phase(Phase.X, **fields)` +
  `append_event` — byte-identical to the old literal, same as `build_event`.
* Delivering through a NON-manager append seam (a production channel's
  `_append_*_event`, the endpoint adapter's `_stream`, an `inner=` envelope)?
  Construct with `build_phase(Phase.X, ...)` and pass the dict to your seam.
* **Adding a NEW phase** = add the `Phase` constant + its `PhaseSpec`
  (domain, one-line purpose, payload-field docs) — it becomes
  machine-discoverable via `/api/v4/capabilities` (`phases` block, grouped
  by domain: `chat` = the shared status row; production channels are
  catalogued for perceivability, their private event *types* stay
  unregistered per the §1 scope ruling — except the registered stage
  grammar in the §1 carve-out). Then emit it via `emit_phase`.
* Frontend-local phase states the client derives itself (e.g.
  `thinking_active`) are NOT pushes — they are deliberately unregistered.

For model streaming, the three progress phases have non-overlapping meaning:

- `waiting_model` is the current attempt waiting for its first semantic
  provider progress; headers, keep-alives and bytes do not turn it into a
  retry.
- `stream_stalled` is the current attempt after reasoning, assistant text or
  valid tool progress has paused.
- `retrying` is emitted only after an actual failed or rate-limited provider
  attempt is being retried. Its `attempt` field is a real retry count, never a
  heartbeat sequence. Retry frames carry the physical attempt `model`, optional
  `providerId`, and `dispatchMode=strict_model|pool_rescue`; pool rescue must
  never claim that the outer logical model is being held fixed.

Every phase snapshot carries the assigned event `seq`; repeated wait/stall
heartbeats repaint through that sequence. Wait/stall phases remain transient
and do not create activity-timeline retry rows. The conversation-sync phase
snapshot also carries server `emittedAt` so a browser can submit an explicit
received-to-painted timing receipt. At the canonical manager chokepoint the
same events project into bounded `timingTrace.statusHistory`; terminal
settlement freezes that diagnostic history into the generation-attempt authority
and mirrors it with the current Turn. It never restores
the transient live phase and is not a second event vocabulary. See
`TURN_TRACE_CONTRACT.md`.

### Events built up conditionally

When fields are added based on runtime conditions, construct the typed base and
mutate exactly as before:

```python
done = build_event(EventType.DONE)
if task.get('preset'):
    done['preset'] = task['preset']
if usage:
    done['usage'] = usage
append_event(task, done)
```

Model-backed `done` events may also carry `waitingOn` when a turn ends normally but not cleanly (`_todo_blocked` set or `finishReason == 'incomplete'`) and the conversation-scoped swarm session remains live detached. `waitingOn` carries `{kind: "swarm", swarmKey, autoResume, agents}` (where `agents` contains up to 8 live agent status snapshots); clients should render waiting UI indicating a blocking background task that will auto-resume on completion.

Model-backed `done` events also carry `streamState`. This is the authoritative
closed provider-stream verdict (`provider_finished`, `premature_close`,
`malformed_stream`, and so on; `semantic_progress_timeout` is retained only for
historical attempt compatibility). Consumers must not
infer success from non-empty content or from a compatibility `finishReason=stop` value. Only
`streamState=provider_finished` is positive stream-completion evidence.

---

## Tool progress is a presentation side channel

`tool_progress` uses its declared, versioned payload in
`lib/agent_core/events.py`. A frame identifies `taskId`, model/tool round,
`toolCallId`, and a monotonically increasing per-call `seq`; it may carry an
ordered stream delta or a bounded replacement preview, observed byte/character
counts, spooling/truncation state, and an optional terminal reason.

Progress never creates another model-visible tool result and must not be stored
as an unbounded transcript. Producers coalesce by time/bytes, bound queued
frames, and keep only a bounded reconnect snapshot on the active round. Clients
load that snapshot first, resume at the next sequence, ignore duplicate or
out-of-order frames, and replace provisional output with the authoritative
single final result. A pause in progress is not completion.

Cancellation may project `cancelling` before the exactly-once terminal
`cancelled` outcome. A cancelled result may reference retained partial output;
quota/disk failure instead carries an explicit unretained-overflow state.

## 2. Adding a NEW event type

Editing one file — `lib/agent_core/events.py` — covers it:

1. **Add the constant** to the `EventType` class, under the right category
   block.
2. **Add an `EventSpec`** to the `_SPECS` tuple: its `category`, a one-line
   `purpose`, `terminal` / `requires_response` flags if applicable, and a
   `fields` map documenting the payload (these become the
   `/api/v4/capabilities` `events` block automatically).
3. **Project it deliberately** — if it changes conversation UI, update the
   cumulative turn projection and its typed renderer. If it belongs only to
   generic task clients, update that declared client contract. Never recreate
   the retired chat task-SSE reducer.
4. **Emit it** via `build_event(EventType.NEW_THING, ...)`.

That's it — no second registration list, no capabilities-endpoint edit. The
drift guard (below) confirms you didn't miss a step.

### Versioning

`EVENT_CONTRACT_VERSION` bumps **only on a breaking change to an existing
event's shape** (a field removed / renamed / retyped). Registering a NEW event
type or adding a new *optional* field is additive — do **not** bump.

Unknown-vocabulary policy (required-on-read):

* **Emitting** — `build_event` rejects an unregistered type (raises
  `EventContractError` under pytest / `TOFU_EVENT_SCHEMA=strict`; warns once
  per signature in production). Forward compatibility is achieved by
  REGISTERING the type (additive), never by emitting an undeclared one.
* **Durable reconstruction** — readers of the persisted task event log call
  `assert_registered_event_types(...)`: an unknown type means the log was
  written by a newer runtime (or corrupted), and folding past it would resume
  a gutted state, so the read refuses loudly (`event_fold` degrades to the
  authoritative checkpoint with a named warning). Over-refusing is an
  inconvenience; silent truncation is a data-integrity incident.
* **`EventSpec.read_policy`** — `'durable'` (default) marks events a
  reconstruction may need; removing such a type is breaking for any retained
  log that may still contain it and must wait out the log's retention.
  `'informational'` marks transient presentation/transport events (the pinned
  set in `tests/events/test_event_registry.py`: `phase`, `tool_progress`,
  `presence`, `sse_timeout`, `ping`) whose loss cannot affect reconstruction.
* **Sync v3 envelope** — the conversation-sync contract is required-on-read
  by construction (closed enums + `additionalProperties: false`): an unknown
  change/attempt type is a contract error and triggers the client's recovery
  resync, never a silent skip.
* **Push wake hints** — owner-scoped push frames are best-effort and
  explicitly ignorable: consumers narrow to the declared frame set
  (`frontend/src/core/frame-identity.ts`), and every consumer keeps a bounded
  reconciliation path for a lost frame, so an unknown frame degrades to the
  same refetch a lost frame already causes.
* **External clients** — ignore unknown FIELDS freely (additive); an unknown
  event TYPE means the server is newer than your vocabulary. Informational
  types may be ignored; a reconstruction that skipped durable types must
  never be presented as authoritative — resync from the snapshot instead.

### Event-log invariants (durable reconstruction)

`lib/tasks_pkg/event_invariants.py` folds the persisted log and returns
violations of the relationships every reconstruction path relies on:
`seq` monotonicity, `round_start`/`round_end` pairing, `tool_start` →
`tool_complete` settlement (a settle without a start is tolerated — replay
and non-streamed paths may elide the announcement), `model_request_start` →
`model_request_complete` span settlement, and **model-visible ⟺ logged**:
every `roundNum` carrying a `model_request_start` must also carry a
`messages_snapshot(kind='request')` for that round, or the exact outbound
request is not reconstructible. The checker is pure and returns violations
instead of raising: tests assert it directly
(`tests/events/test_event_log_invariants.py`), and the opt-in runtime audit
(`TOFU_EVENT_INVARIANTS=1`, wired at the terminal finalize seam) logs
violations at ERROR without failing a turn — aborted tasks are checked with
`allow_open_tail=True` because they legitimately end mid-round.

### Program lifecycle projection

Native OpenAI PTC and local JavaScript share `program_start` and
`program_output`. Both events carry `programCallId`, model/display round,
source/backend, status, and child call identity; start also carries authored
code and enforced limits, while output carries the aggregate result. Emitters
must update the cumulative `programRuns`/parent `toolRounds` projection before
delivery. Real child calls keep their ordinary tool lifecycle, so clients show
one program parent plus inspectable children and never invent native adoption
from an `execute_tools` gateway call.

`program_progress` publishes the cumulative child ids/names after each local
child call or batch settles and runtime counters commit, before the guest
resumes. Publication runs between WASM entries; a fault raises typed
`progress_publication_failed`, retains settled receipts/counts and prevents
later guest effects. It keeps the parent
running, never repeats code/results and never claims predicted savings. The
existing 16-call limit bounds it to at most 16 frames; duplicate projections
emit none. Parent state is updated before delivery and persists with normal
structural events, so reconnect uses the same cumulative identity.
Problem: terminal-only child discovery obscured ongoing local execution.
Decision: publish settled identities; alternatives of guessed totals, repeated
source/start frames or a polling worker add misleading state or avoidable work.
With 16 children and 30,000 B source, a synthetic projection sink measured
61,623 B without progress versus 37,911 B with it; median projection elapsed time
was 0.262/0.544 ms across 20 runs. Native commit, network and RSS cost were not
measured; no model latency/token savings follow from this measurement.

The aggregate `result` preserves its JSON type: object, array, string, number,
boolean, or null. Scalar/array reductions must settle through the same terminal
event as object results; clients must not treat a falsy result as absent.

---

## 3. The drift guards (what CI enforces)

| Test | Enforces |
|------|----------|
| `tests/events/test_event_registry.py` | Every event the backend emits — whether written as a `'type': 'x'` literal **or** `EventType.X` — is registered. Every `ev.type === "..."` the frontend handles is registered. No orphan specs. Every spec's `read_policy` is from the closed set; the informational class is pinned exactly; durable-read whitelists (`STRUCTURAL_EVENT_TYPES`) stay registered. |
| `tests/events/test_event_read_policy.py` | Required-on-write: `build_event` rejects unregistered types. Required-on-read: `assert_registered_event_types` names consumer + unknown types; `fold_cold_state` refuses an unknown-type log (loud checkpoint fallback). |
| `tests/events/test_event_emit.py` | `build_event` is byte-identical to the literal (incl. key order); `emit` delivers through `append_event`; a real converted orchestrator helper still emits the exact pre-conversion dict. |
| `tests/agent_core/test_phase_registry.py` | The PHASE sub-vocabulary: every `phase='x'` in `lib/` is registered (or a documented out-of-channel carve-out), every `.phase === "x"` the frontend branches on is registered, **zero raw `{'type': 'phase'` literals** outside the registry module (the unified-interface ratchet), no dead phase vocabulary, and `build_phase` byte-identity. |

If you add an emitter in a NEW file, add its path to `_BACKEND_FILES` in
`tests/events/test_event_registry.py` so the new call sites are scanned.

### Field-level schemas (`EventSpec.schema`)

The `fields` map is prose; prose cannot fail CI. The `rawToolTokens`
incident proved the gap: a field rode the wire undeclared — emitted by the
pipeline, consumed by the frontend badge, absent from the contract — and was
discovered by a user. `EventSpec.schema` (a tuple of `FieldSpec(name, kind,
required)`) makes the field contract machine-readable, and three gates
enforce it:

| Gate | Catches |
|------|--------|
| `build_event` construction validation | An **undeclared field**, a **missing required field**, or a **type mismatch** raises `EventContractError` at the emitting line under pytest (default) or `TOFU_EVENT_SCHEMA=strict`; production defaults to `warn` (logged once per signature, never fails a turn); `off` disables. |
| `append_event` delivery-seam `check_event` | Fields stamped by **post-construction mutation** (the pipeline adds `status` / `rejection` / compaction fields after building `tool_complete`) — validated on the final frame that actually reaches the wire. |
| `tests/events/test_event_schema.py` | Schema ↔ prose **exact key sync** (a field declared in one but not the other fails), closed kind vocabulary, plus a REAL scripted `execute_tool_pipeline` round whose every emitted frame must conform. |

`kind` is a tiny closed DSL: `str` / `bool` / `int` / `number` / `dict` /
`list` / `None`, `|`-separated for unions (`'int | None'`). `bool` is NOT an
`int` (JSON semantics, not Python's). Keep `required` minimal — a field any
real emitter legitimately omits (e.g. the success path omits `status`) is
optional, or the strict gate raises on conforming traffic.

The TypeScript mirror is generated: `scripts/gen_event_contract.py` writes
`frontend/src/api/event-contract.generated.ts` (one interface per schema'd
event), wired into `make contracts-check` / `npm run check:event-contract` —
a frontend read of an undeclared field is a *typecheck* error, so the two
sides cannot drift in either direction. Schema'd events also export their
`schema` in the `/api/v4/capabilities` `events` block.

Golden-wire fixtures: `scripts/gen_wire_fixtures.py` renders one JSON
frame per schema'd event and push frame into `contracts/fixtures/`
(deterministic minimal payloads built through the real construction
gates). `make contracts-check` fails on corpus drift;
`tests/conversation_sync/test_wire_fixtures.py` re-validates and rebuilds every frame, and
`tests/frontend/test_wire_fixtures_frontend.py` proves the generated TS mirror and
the push narrowing guards accept exactly the corpus.

Migration is event-by-event (`tool_complete` is the pilot): un-schema'd
events keep the permissive forward-compatible wire.

---

## 4. ⚠️ Gotcha — grep tests for literal type strings BEFORE converting

Converting `{'type': 'foo'}` → `build_event(EventType.FOO)` **removes the
literal string `'type': 'foo'` from the source file.** Any test that does a
*static source scan* for that literal will break — not because behaviour
changed, but because the substring it greps for is gone.

**Before converting an emitter, grep `tests/` for the event's literal type
string:**

```bash
rg -n "'type': 'compaction'|\"type\": \"compaction\"" tests/
```

If a test matches, update its assertion to also accept the typed form
(`EventType.COMPACTION`) — the test's *intent* (e.g. "compaction is only
emitted from `_archive`") is still valid; only its string-matching needs to
recognize the new construction.

**Precedent:** `tests/compaction/test_compaction_invariants.py::test_compaction_event_emit_sites_are_audited`
hard-matched `'type': 'compaction'`; the 2026-06 conversion broke it until the
assertion was widened to `... or 'EventType.COMPACTION' in all_src`.

---

## 5. Why this exists (one paragraph)

Before the registry, ~40 event `type` strings were an *implicit* contract,
defined only by scattered `append_event(task, {'type': ...})` calls and the
`ev.type === "..."` ladders in the JS. A third party building a frontend had to
reverse-engineer the stream by reading our source. The registry makes the
contract explicit, versioned, machine-discoverable (`/api/v4/capabilities`),
and drift-guarded — and `build_event`/`EventType` is the discipline that keeps
every emission pinned to it.

---

## 6. Project Brain event stream

Project Brain uses `storage_events.stream_kind = 'project_brain'`, separate
from task UI events. Each event contains explicit `ownerUserId`, normalized
`projectKey`, monotonic `projectSequence`, `kind`, `timestamp`, and `payload`.
The relational columns `owner_user_id`, `project_key`, and `project_sequence`
mirror those identity fields for owner-scoped indexing and uniqueness.

One Sidecar command transaction allocates the next sequence, appends exactly
one semantic event, folds `storage_project_brain_projects`, writes its
idempotency receipt, and returns a push hint. A failure rolls back all four
effects. Public lifecycle kinds are derived from runtime signals:

- `work_started`, `work_title_refined`, `work_changed`, `work_finished`;
- `narrative_added` (historical `attention_added` events stay in the log but
  fold to nothing; the kind is no longer emitted);
- `checker_registered`, `checker_result`, `decision_promoted`;
- `watch_added`, `watch_updated`, `watch_deleted`;
- `cursor_initialized`, `cursor_confirmed`;
- `legacy_migrated`, `projection_checkpoint`.

Task started/completed telemetry is not copied to this stream or Feed. Feed is
the bounded `NarrativeEvent` fold: meaningful work results, Checker failures,
Integration results, checker-backed decisions, and human Watch changes only. Periodic
`projection_checkpoint` carries a complete rebuild snapshot; only then may a
reconstructible prefix be reclaimed. Charter, Checker, Watch, and delivery
cursor state is part of that checkpoint.

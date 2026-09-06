# Emitting streaming events — contributor guide

> **Audience: anyone adding or changing backend code that emits an SSE / push
> event.** If you are *consuming* the event stream (building a frontend / SDK),
> read [`HEADLESS_API.md` §3.6.1](HEADLESS_API.md#361-streaming-event-contract--the-frontendbackend-sync-interface)
> instead — that documents the wire vocabulary a client sees.

The event vocabulary is a **single, declared, versioned contract**. There is
ONE source of truth — [`lib/agent_core/events.py`](../lib/agent_core/events.py) —
and ONE way to construct an event. This guide is the rule set that keeps it that
way.

---

## 1. The rule

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
benefit that every emission references the declared vocabulary.

### Scope: what this rule covers

This rule governs the **chat / agent task event stream** — the ~41 registered
types flowing over `/api/v1/tasks/{id}/stream` and the `chat` push channel.
It does **not** govern
unrelated `TaskRuntime` channels that define their own small private vocabulary
for a non-chat feature (e.g. the paper/translate runtimes emitting
`{'type': 'chunk'}` on their own channel — see CLAUDE.md §14). Those are
self-contained producer↔consumer pairs, not part of the shared task-event
contract, so they are not registered here. The conversation UI consumes the
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
projection on `GET /api/v1/oauth/status`; it is already length-bounded and
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

`turn.compact` is deliberately represented by one small transactional
`conversation.activity` change whose payload is
`{requiresSnapshot: true, conversationRevision}`. Compaction can delete a turn
graph closure, insert a summary, rewrite ancestry, and patch retained
projections atomically; no bounded delta can faithfully describe that mutation.
The coordinator therefore performs one authoritative snapshot re-anchor, and
the replay log never copies the folded transcript or large retained turns.

Provider microchunks are not individually observable contract events. The
stream manager emits the first text delta immediately, then losslessly merges
later content/reasoning for at most 100 ms or 256 characters. It flushes before
every structural boundary and assigns a sequence only to the merged event.
During an active provider dispatch those sequenced frames are process-local
replay: storage and synchronous push observers are isolated from upstream
consumption. The first post-provider authoritative event carries the cumulative
projection and restores the ordinary durable-before-visible contract. This
avoids one Sidecar transaction per 4-character provider chunk without letting
a database or frontend delivery fault break the model stream.

Outside ingress isolation, a structural frame carries the exact task event and
a revision-checked projection patch in one `turn.event.record` transaction; it
does not carry a second cumulative projection inside the event payload. The
running executor retains one last-applied projection/revision under a task-local
lock. After the shared stable-segment normalizer, it also supplies private
boolean stability evidence; absent evidence keeps replay correct but makes the
Sidecar normalize once at the next structural boundary. A stale patch rebases
once from authority, and a rejected/terminal attempt clears that state and
plants the existing cooperative-abort fence. Coalesced progress frames retain
an explicit lightweight attempt-status check because no write transaction is
available to prove liveness for them.

Those exact durable `projectionPatch` payloads may also serve as the live
Turn's bounded physical reconstruction head; this creates neither a second
event vocabulary nor a second replay copy. One owner/attempt/revision-fenced
checkpoint supplies the base, and at most 64 patches / 1 MiB may follow it.
Rollover writes a new checkpoint, while terminal settlement and recovery write
the complete Turn projection and remove the head. Attempt-event retention must
therefore skip every attempt referenced by checkpoint/head metadata; a missing,
gapped, duplicated, misbased, or oversized chain is an integrity failure, never
a best-effort projection.

Post-settlement task observers are an exact two-event exception to the carried
attempt transaction: `round_committed` and `preference_learned`, when the task
is already terminal, persist through standalone cold replay before live push.
Their producers own dedicated settled-Turn CAS patches, so they never re-enter
the closed attempt. No late delta, tool, phase, or lifecycle event shares this
exception; those still hit the stale-attempt fence and cooperatively abort.

### Activity timeline projection

Execution diagnostics use the registered task-event vocabulary as facts and
one cumulative Turn sidecar as presentation. Tool lifecycle,
`tool_schema_rejected`, retry `phase` cycles (and any phase carrying an HTTP
error status), the `compaction` → `compaction_done` archive/receipt pair,
`model_fallback`, failed/aborted
`model_request_complete`, and terminal errors are folded by
`lib/turn_activity_timeline.py` into `projection.activityTimeline`. Outside the
provider-ingress isolation window the fold is durable-before-visible through
`record_task_event`. In-flight diagnostics can appear first through task-memory
SSE replay; after successful provider-boundary convergence, reconnect/cold
snapshot catches up from the cumulative projection without a second task-SSE
consumer.

The compaction pair becomes one inspectable Turn row keyed by `archiveId`; its
settled form carries the estimated token/message counts before and after plus
the reduction percentage. The transient `phase=compacting` beat remains
progress text and is replaced by that receipt row rather than rendered twice.

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

`tool_wire_projection` is a separate bounded Request Inspector diagnostic. It
records the ordered final provider tool names, schema-token estimate, resolved
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

`build_event` only *builds* the dict; you still deliver it through the existing
chokepoint:

- From the orchestrator / managers: `append_event(task, build_event(...))`.
- Convenience one-liner (build + deliver): `emit(task, EventType.X, **fields)`
  — wraps `build_event` + `append_event`. Use it for new call sites; the
  explicit two-step is fine where the surrounding code already holds
  `append_event`.

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
  machine-discoverable via `/api/v1/capabilities` (`phases` block, grouped
  by domain: `chat` = the shared status row; production channels are
  catalogued for perceivability, their private event *types* stay
  unregistered per the §1 scope ruling). Then emit it via `emit_phase`.
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
   `/api/v1/capabilities` `events` block automatically).
3. **Project it deliberately** — if it changes conversation UI, update the
   cumulative turn projection and its typed renderer. If it belongs only to
   generic task clients, update that declared client contract. Never recreate
   the retired chat task-SSE reducer.
4. **Emit it** via `build_event(EventType.NEW_THING, ...)`.

That's it — no second registration list, no capabilities-endpoint edit. The
drift guard (below) confirms you didn't miss a step.

### Versioning

`EVENT_CONTRACT_VERSION` bumps **only on a breaking change to an existing
event's shape** (a field removed / renamed / retyped). A new event type or a
new *optional* field is additive — do **not** bump. Clients are told to ignore
unknown event types and unknown fields, so additive changes are always safe.

### Program lifecycle projection

Native OpenAI PTC and local ToolScript share `program_start` and
`program_output`. Both events carry `programCallId`, model/display round,
source/backend, status, and child call identity; start also carries authored
code and enforced limits, while output carries the aggregate result. Emitters
must update the cumulative `programRuns`/parent `toolRounds` projection before
delivery. Real child calls keep their ordinary tool lifecycle, so clients show
one program parent plus inspectable children and never invent native adoption
from an `execute_tools` gateway call.

---

## 3. The drift guards (what CI enforces)

| Test | Enforces |
|------|----------|
| `tests/test_event_registry.py` | Every event the backend emits — whether written as a `'type': 'x'` literal **or** `EventType.X` — is registered. Every `ev.type === "..."` the frontend handles is registered. No orphan specs. |
| `tests/test_event_emit.py` | `build_event` is byte-identical to the literal (incl. key order); `emit` delivers through `append_event`; a real converted orchestrator helper still emits the exact pre-conversion dict. |
| `tests/test_phase_registry.py` | The PHASE sub-vocabulary: every `phase='x'` in `lib/` is registered (or a documented out-of-channel carve-out), every `.phase === "x"` the frontend branches on is registered, **zero raw `{'type': 'phase'` literals** outside the registry module (the unified-interface ratchet), no dead phase vocabulary, and `build_phase` byte-identity. |

If you add an emitter in a NEW file, add its path to `_BACKEND_FILES` in
`tests/test_event_registry.py` so the new call sites are scanned.

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
| `tests/test_event_schema.py` | Schema ↔ prose **exact key sync** (a field declared in one but not the other fails), closed kind vocabulary, plus a REAL scripted `execute_tool_pipeline` round whose every emitted frame must conform. |

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
`schema` in the `/api/v1/capabilities` `events` block.

Golden-wire fixtures: `scripts/gen_wire_fixtures.py` renders one JSON
frame per schema'd event and push frame into `contracts/fixtures/`
(deterministic minimal payloads built through the real construction
gates). `make contracts-check` fails on corpus drift;
`tests/test_wire_fixtures.py` re-validates and rebuilds every frame, and
`tests/test_wire_fixtures_frontend.py` proves the generated TS mirror and
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

**Precedent:** `tests/test_compaction_invariants.py::test_compaction_event_emit_sites_are_audited`
hard-matched `'type': 'compaction'`; the 2026-06 conversion broke it until the
assertion was widened to `... or 'EventType.COMPACTION' in all_src`.

---

## 5. Why this exists (one paragraph)

Before the registry, ~40 event `type` strings were an *implicit* contract,
defined only by scattered `append_event(task, {'type': ...})` calls and the
`ev.type === "..."` ladders in the JS. A third party building a frontend had to
reverse-engineer the stream by reading our source. The registry makes the
contract explicit, versioned, machine-discoverable (`/api/v1/capabilities`),
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

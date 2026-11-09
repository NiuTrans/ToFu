# Conversation Sync v3

Responsibility: the sole command, snapshot, replay, recovery, and health
protocol for a conversation. The canonical wire source is
`contracts/conversation_sync_v3.yaml`; generated Python and TypeScript files
must never be hand-edited.


String `minLength` and `maxLength` constraints count Unicode code points, not
UTF-16 code units or UTF-8 bytes. Generated browser validation must agree with
Python for supplementary characters (including emoji), including nested
background-command previews in snapshots and history pages. A valid 2,000-code-point
preview must not prevent opening its conversation. Canonical projection normalization
bounds historical background-command `command` and `resultPreview` display text
using the schema limits, including `_bgCommandInjects[*].previews[*]`, and marks
clipped output `resultTruncated`. It copies metadata without changing stored history
or full model-facing text. Identity, status, malformed types, and other invalid
wire values still fail validation; do not bypass snapshot validation to recover
from a presentation-field error.

Tool-result evidence preserves its recorded `resultContractVersion`. Readers
accept the current `tofu.tool-result/v2` and historical `tofu.tool-result/l0`
identities; writers continue to emit v2. Historical l0 evidence does not need
a storage rewrite to appear in snapshots or replay. Unknown versions, extra
fields, and invalid evidence values remain contract violations.

## Committed file changes

Committed file projections retain `changeId` and `rootPath` from the project
journal through live updates and settlement. Repeated edits of one file change
its receipt while preserving the unique-file count. These fields support
DOM-independent consumers such as [paper preview](../PAPER_BUILDS.md#chat-preview);
they never grant project access. Failed journal writes emit no commit receipt.

Auxiliary manual worktree entries retain their actual checkout `rootPath` and
optional per-file `undoAvailable: false`, including after Git merge/removal.
An explicit per-file denial disables undo/redo for the whole block without
suppressing its committed file rows. Both projection and command admission use
the same capability policy; this is not permission to undo the canonical merge.

Device writers use [`project_file_receipts_v1.schema.json`](../../contracts/project_file_receipts_v1.schema.json).
The receiving task validates the complete receipt and requested paths, then
binds its authenticated owner and addressed `remote:<device>:<root>`. The
original tool output remains separate. Settlement preserves remote facts
without reading a server-side journal for the remote path.

Turn file presentation retains at most 512 unique rows; `modifiedFilesTruncated`
and `fileChanges.truncated` expose omitted rows and survive settlement/resume.
Durable journal and device snapshots remain intact. `undoAvailable: false`
disables the local undo control for external or incomplete file sets; the
command boundary independently refuses them before any projection or file
mutation. A turn undo settles `undone` only when every selected journal record
was restored. A refused record, or no recorded change left to undo, returns the
block to `applied` with the reason as its `error`: redo rewrites the turn's
files unconditionally, so it must never be offered over a newer edit that made
undo refuse. Only the file-history engine's typed refusals answer 400. An
engine fault (file history unavailable, an unreadable journal, a failed
restore) also returns the block to its source state, but answers a logged,
redacted 500 (`FileHistoryFault`); its recorded `error` names files and
exception types, never raw exception text
([refusals and faults](../PROJECT_FILE_UNDO.md#refusals-and-faults)).
Verify with receipt, round-derivation, file-command, file-change fault
boundary, resume and conversation-surface tests, plus the remote paper browser
journey.

## Goal phase execution

`turn.visible.sync` accepts a bounded `start_index` plus message suffix; `live`
admits one Goal phase before dispatch. Stable phase identities reuse the first
command Turn and mint child attempts thereafter, with a hard 4,096-phase
ceiling beneath the executor's tighter budget. Completed rows are replay-safe;
an existing live child settles through `turn.event.record`. The child's durable
`flowRootAttemptId` config must match the write's `flow_root_attempt_id` lock
hint and a current, owner-matching root. Parent-before-child attempt locking,
root terminal propagation and sync publication share the transaction.
`turn.attempt.get(resolve_executor=true)` resolves the same relationship for
stop commands, including registry-miss recovery. No browser schema or alternate
stream is introduced.

Problem: after a Flow child writes a file, the producer derives `fileChanges`
from `modifiedFileList` and later patches its `taskId`. Native storage previously
patched the raw document without that sidecar and rejected the valid nested path.
Decision: `turn_projection_segments::normalize_file_changes` shares file-sidecar
normalization between the locked patch base and settled native assembly. Replay
still starts from the exact committed document and includes the sidecar creation.
Alternatives rejected: weakening missing-parent validation hides corrupt patches;
adding a field on public reads without a revision breaks replay. Verify the native
projection/replay tests and real native Flow file-tool HTTP cases.
Completion-only Flow phases also retain accepted system notes while assembling
tool segments. Dropping those notes on first settlement made an identical final
sync appear to add evidence and fail its immutable settlement check. Native
assembly reuses the common note validator and preserves round/attempt placement;
exact replay remains a comparison against the sealed result.

Projection normalization must remain visible in the revision patch: its base
is the exact previous wire document, even when the producer's local baseline
has already normalized missing segment placeholders. The first nonempty content/reasoning
channel forces one structural write before later deltas use text-only patches.
Otherwise a missing block can cause a render gap or a full-snapshot loop.

An admitted Goal phase seals its own live `segments` and `toolRounds` through
`lib/agent_core/output_settlement.py`, rather than rebuilding streamed history
from a bounded completion preview. Recorded reasoning, narration, full results,
and block identities survive resets and terminal settlement; completion may
append genuinely new output or extend a cumulative prefix. Occurrences match
by producing execution, exact Inspector token and round/call identity, never
by result text. Legacy completion-only imports retain a fill-only compatibility
path; they do not define new execution semantics.

Tool identity belongs to the producing invocation. A parent or visible phase
may carry child output but cannot replace its `taskId` or add the display
attempt's `attemptId`. Root/resume backfill only fills unknown fields within
the same execution. This keeps one tool occurrence through checkpoint, seal,
and cold reload even when root/child counters coincide. New cards pass the
persisted `inspectorRef` unchanged to the owner-authorized request/state API;
invalid explicit references never widen into a legacy owner guess.

The final live checkpoint must commit before phase settlement. A lost ACK may
retry only the identical checkpointed output and ownership fence; Sidecar still
checks attempt state and revision. Failure prevents successor dispatch or a
successful terminal frame. Unknown usage is omitted, never serialized as
`usage: null`, so the same strict v3 projection decodes live and after reload.

## HTTP major and native response contract

Conversation schema `tofu.conversation-sync/v3` is served on `/api/v4`.
The schema number does not choose the HTTP major. The generator validates
paths against `contracts/api_v4.yaml`; route parity tests cover every method.
Old `/api/v3` clients receive the release gate's 426 upgrade problem.
The sync blueprint explicitly declares `NativeContractBlueprint` with its
generated contract ID: snapshots, command receipts and domain errors retain
the sync protocol's shape. Generic v4 wrapping must not discard `latestTurn`,
validation violations or storage retry fields. Declaration follows the matched
blueprint, never a URL-prefix exemption, and changes no auth/owner checks.
The generated image/event/history/command URLs migrate together.

Command failures map once, in `_command_response`: `LifecycleConflict` is 409
with `latestTurn`, `LifecycleNotFound` is 404, `BadRequest` is 400 with its
message and `field`, `StorageError` keeps the storage taxonomy, and
`AttemptStartFailure` is its persisted 500 envelope. A `ContractViolation`
raised while executing is durable-data drift: the typed 500 the reads use,
because request bodies are schema-checked before the command runs. Any other
exception, including a builtin `ValueError`/`TypeError`, is a logged,
redacted 500 ([API contract §2.3](API_CONTRACT.md#23-status-code-mapping)).
Read routes decode their query into `BadRequest` before calling the service, so
a service fault can never answer as a malformed request.

## Snapshot live-phase parity

Both SQL and native TofuDB snapshots implement the existing `activeAttemptPhase`
contract. Native `turn/snapshot_phase.rs` reads the current running main-lane
attempt's event head and at most one 32 KiB logical event under the same snapshot
transaction. It validates exact conversation/Turn/Attempt/sequence identity and
requires the event revision not to exceed the returned Turn revision. The byte
witness is checked before overflow-blob materialization. Null, missing, oversized
or phase-free latest events omit the field; terminal/superseded attempts and
foreign owners cannot contribute it. No backward phase search or timing-history
inference is permitted.

Problem: native snapshots omitted this already-declared field, so cold loads and
resync cleared the browser phase and fell back to a waiting label despite live
execution. Decision: restore backend parity at the read authority, preserving
intentional phase clearing. Alternatives rejected: retaining the old browser
phase can display a superseded attempt; synthesizing from diagnostic history
resurrects stale phases. Specs: native `snapshot_phase_tests.rs` and
`test_snapshot_restores_only_current_committed_bounded_attempt_phase`.

## One authority

### State delivery and diagnostic evidence

Problem: multi-megabyte `messages_snapshot` diagnostic bodies shared the ordered
conversation SSE pipe with progress and terminal patches. A slow forwarded
connection could repeatedly time out before one complete diagnostic frame arrived.
Decision: the generated browser requests `diagnosticPayload=none`; the service
omits only the optional `AttemptEvent.payload.event` body from its read view.
Cursor, sequence, patch, phase, interactions and settlement remain identical.
Durable task/attempt evidence stays complete; the demand-loaded Request Inspector
uses its existing owner-checked wire/detail reads and refresh lifecycle. Default
and explicit `full` streams retain diagnostic delivery for other consumers.
Alternatives rejected: deleting durable evidence loses inspection; extending
timeouts leaves status behind the same body; changing live projection fields
without their revision breaks positional replay. No backend switch, polling
lane, task limit or tuning setting is introduced.

One lifecycle-owned authority watch wakes the broker from committed changes,
including writes in a separate execution process. A cursor gap or authority
restart invalidates every current subscription; scoped hints retain owner and
conversation identity. Streams subscribe before their first replay. An unchanged
watch cursor needs no replay query: quiet stream heartbeats report the last
delivered cursor, while reconnect and invalidation resume canonical replay.

The stream confirms a quiet subscription with an immediate heartbeat. Later
heartbeat deadlines advance only after a delivered frame; redundant broker wake
hints cannot postpone them. Browser wake/invalidation uses the existing full
`reconnectGraceMs`, measured from that source's opening or latest valid frame,
including when browser suspension delays timers. The previous source's timestamp
cannot prematurely retire its replacement. Only `live` confirms a running
sidebar label; connecting, recovering, idle and missing health display neutral
“Syncing status” until canonical replay establishes current state. Unknown
transport health neither completes nor cancels a task.

Verify `test_conversation_sync_diagnostic_payload.py`,
`test_conversation_stream_heartbeat.py`, `conversation-sync.test.ts`, and
`conversation-activity.test.ts`, alongside Inspector wire/detail and stream
ownership tests.

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

## Immutable streaming state

Problem: every event copied whole entity indexes and snapshots repeatedly
sorted history. Decision: `core/turn-state.ts` owns one synchronous draft;
`core/entity-record.ts` splits oversized hash leaves and copies touched paths.
Fanout is an algorithmic layout, not a resource cap. Published records revoke writes;
old snapshots share unchanged DTOs. No event chain or second authority exists.
Reads/reflection/freeze/JSON remain supported; use `entityRecordValues/Entries/Keys` for bulk reads.
Materialize a plain record before structured cloning; proxies are never wire payloads.
Snapshot insertion preserves stable ordinal ties without sorting every prefix.
Duplicates/stale events retain identity; recovery callbacks remain intact.
The reducer publishes changed IDs/reset through the existing subscription.
`createConversationViewModelSelector` retains one compiled view per mounted
session and unions IDs until paint; local changes replace affected ancestors.
Presentation maps are snapshotted. Structural/cross-Turn changes, missing
deltas and overlays rebuild the same composer. Dispose releases joins and IDs.
Memory scales with loaded entities/lanes, not the number of streamed events.
Rejected: full copies (linear writes), mutable state (lost isolation), and
prototype/event chains (history retention or compaction stalls).
Specs: `entity-record.test.ts`, `turn-state-sharing.test.ts`, `view-model/selection-deltas.test.ts`;
browser: `tests/visual/test_visual_streaming_state.py`.
Paired offline diagnostics: `benchmarks/turn_state_measure.mjs`,
`benchmarks/turn_selection_measure.mjs`, `benchmarks/turn_index_measure.mjs`.
Client DOM measurements and shell receipt caching: [rendering performance](../TURN_RENDERING_PERFORMANCE.md); offline reducer clocks alone never admit DOM/startup/I/O latency.

## Owners

### Durable execution admission

**Problem:** constructing a conversation executor in an HTTP process couples
accepted work to API restarts and duplicates execution when admission loses
its acknowledgement. **Decision:** ordinary Chat, Goal, Flow, Coordinator,
queued activation and timer continuation submit the accepted Attempt through
`turn.attempt.dispatch_worker`. Its one transaction binds the deterministic task
and enqueues its WorkerJob. HTTP acknowledges the pending binding; the common
claimed worker restores the accepted configuration and invokes the existing
executor. Request replay cannot replace identity, configuration or the original
Stop watermark. Worker execution uses its own monotonic clock; an ingress clock
is not serialized as evidence from another process.

Replaying a settled command after a newer attempt has become current returns
the original attempt receipt and the current Turn. It never rebinds a project
assignment to the obsolete attempt. Problem: a late background-result replay
tried to advance the grant backward and repeatedly failed its predecessor fence.
Decision: both pair creation and attempt creation bind only a current replay;
fresh acceptance still binds before dispatch. Relaxing the repository evidence
fence would allow stale commands to replace live execution. Spec:
`tests/project_mod/test_project_dispatch_runtime.py`.

An owner-originated successor of a dispatched Worker Turn resolves and validates
the existing assignment through `admission.turn_dispatch` before acceptance,
then captures its scoped principal. A revoked grant returns the lifecycle
conflict `worker_binding_refused`; it cannot silently become an ordinary owner
task. This also repairs association after an older unbound browser attempt.
Synthetic callers retain their identity and must match the explicit grant.
Request-only config and guidance-response validation precedes binding lookup,
preserving authored 400 errors even when the requested Turn does not exist.
Spec: `tests/conversation/test_worker_attempt_real_start.py`.

Stop commits cancellation before acknowledging it. Unentered work settles
without constructing an executor or requiring an execution grant that may
already be revoked. Configuration/authorization failure before entry has a
terminal Turn receipt and creates no fictitious task-result or exit witness.
After entry, physical exit and accounting retain their existing proof
requirements. Preparation failures preserve their actionable model/provider
cause while unknown external effects prohibit automatic replay. Startup errors
arrive through the same canonical Turn projection after HTTP acceptance.

**Alternatives considered:** spawning locally after a missing acknowledgement
can duplicate effects; retaining a separate conversation queue duplicates the
WorkerJob owner; dispatching every child as another root destroys the shared
invocation, account and cancellation scopes. Descendants continue to use those
common owners.

| Concern | Owner |
|---|---|
| Paths, DTOs, events, retries, health policy | `contracts/conversation_sync_v3.yaml` |
| Contract generation | `scripts/gen_conversation_sync_contract.py` |
| HTTP/auth adapter | `routes/conversation_sync_v3.py` |
| Command policy | `lib/conversation_sync/command_service.py` |
| Proposed-plan and execution-handoff documents | `lib/plan_contract.py` |
| Snapshot/replay service | `lib/conversation_sync/service.py` |
| Repository protocol | `lib/conversation_sync/repository.py` |
| Atomic durable operations | `lib/storage_authority_sql/operations_pkg/_turns.py` (facade over `_turns_{core,read,write,lifecycle,events,branch}.py`) |
| Post-commit wake | `lib/conversation_sync/broker.py` |
| Browser cursor/SSE/recovery | `frontend/src/core/conversation-sync.ts` |
| Browser page-wide stream transport | `frontend/src/core/conversation-event-multiplexer.ts` |
| Browser authenticated owner lifecycle | `frontend/src/core/current-user.ts` |
| Browser push-frame ownership policy | `frontend/src/core/frame-identity.ts` |
| Browser turn state | `frontend/src/conversation/domain/turn-store.ts` |
| Browser runtime/projection | `frontend/src/core/turn-runtime.ts` |

## Public surface

| Operation | Endpoint |
|---|---|
| Snapshot/reset image | `GET /api/v4/conversations/{conversationId}/sync` |
| Older lane page at a replay head not older than the caller's | `GET /api/v4/conversations/{conversationId}/turns/history` |
| Complete Turn for explicit projection editing | `GET /api/v4/conversations/{conversationId}/turns/{turnId}` |
| Exact lazy historical tool output | `GET /api/v4/conversations/{conversationId}/turns/{turnId}/tool-results/{roundIndex}` |
| Ordered replay SSE | `GET /api/v4/conversations/{conversationId}/events` |
| Several conversations' ordered replay on one SSE response (browser) | `GET /api/v4/conversation-events` |
| Create input/output pair and attempt | `POST /api/v4/conversations/{conversationId}/turns` |
| Projection CAS update | `PATCH /api/v4/conversations/{conversationId}/turns/{turnId}` |
| Execute an exact proposed plan | `POST /api/v4/conversations/{conversationId}/turns/{turnId}/plan/execute` |
| Regenerate/continue/resume attempt | `POST /api/v4/conversations/{conversationId}/turns/{turnId}/attempts` |
| Create/delete lane | v3 lane operations generated from the contract |
| Delete explicit turns | `POST /api/v4/conversations/{conversationId}/turns/delete` |
| Abort attempt | `POST /api/v4/attempts/{attemptId}/abort` |

Every operation receives authenticated `user_id` explicitly. No v2
conversation route or attempt-scoped conversation EventSource exists.
Turn and lane deletes are idempotent per delete intent: repeating the same
turn set (in any order) or the same lane returns the recorded response; a
different request naming an absent turn or lane answers `404`.
`regenerate`, `continue`, `checkpoint_resume`, and `answer_guidance` supersede
the whole later lane tail, including branch lanes rooted there, in the same
transaction. Resume keeps the target checkpoint; regenerate starts over.
A live descendant refuses the command before any mutation or task dispatch.
The response carries `deletedTurnIds`; `turn.deleted` replay makes peers
converge. Clients remove the suffix only on authoritative acknowledgement,
never on click, and an older response cannot resurrect it.


### Unconfirmed-usage forward recovery

**Problem:** one missing provider receipt used to close a human's conversation
for good. A resumed turn restores `usageComplete=false`, and a finite-budget
account refused its next model request with `execution_token_usage_unknown`;
`continue`/`checkpoint_resume`/`answer_guidance` therefore died on their first
call. The only offered control was `regenerate`, which throws away the saved
progress the reader actually wanted to keep.
**Decision:** the conversation continues; the *receipt* is what stays
incomplete. Every turn the conversation command dispatches — ordinary chat,
Goal Mode and Flow, including their continuations, recoveries and resumes —
carries `_unknownUsagePolicy: 'continue'` (`lib/conversation_sync.command_service`),
and `ExecutionBudget` reads it: unconfirmed consumption keeps its paid lower
bound, stays `usage_complete: false` in every receipt and allowance snapshot,
and no longer blocks `reserve_model`, dispatch validation or tool admission.
Flow roles and swarm workers attach to that same account
(`inherit_execution_budget`) and therefore share its policy. An explicit finite
cap still stops spend, because token/tool/deadline accounting is unchanged —
this only stops treating an *unknown* number as an *exhausted one*. Accounts
opened outside a conversation turn (Evolution stages) keep the account default
`'fence'`.
**Owner decision 2026-11-02: Goal Mode is not a separate spending regime.**
Goal Mode and Flow turns used to keep `'fence'` as "unattended paid runs". A
long Goal run that failed for an unrelated reason then could not be continued:
its resume died on the first model call with `execution_token_usage_unknown`,
settlement hid every resume, and the only in-place control left was
`regenerate`, which discards the saved progress. They now share chat's policy:
they run the same invocation loop for the same owner, who can stop them at any
time and whose explicit finite limits still bind. Rejected: keeping the fence for
unattended turns (the dead end above, for exactly the longest runs); removing
the fence from `ExecutionBudget` altogether (it would also change Evolution
accounts, which settle through their own run records rather than a conversation
resume — a separate decision).
Settlement therefore advertises the ordinary resumes for an incomplete turn,
including one whose conversation account recorded
`execution_token_usage_unknown` (`source: execution-budget`): that refusal
cannot recur. `lib/turn_lifecycle._settlement` hides them only for producers
that refuse independently of the policy — the native model account
(`source: native`) and delegated swarm recovery (`source: swarm-recovery`) —
where a resume would die on its first call again. `UsageRecoveryBudget` and this
forward-recovery command remain as the *deliberate* path: a bounded, freshly
authorized execution rather than a continuation of the old one. Its card renders
only when the settlement offers no ordinary `continue`/`checkpoint_resume`, so
a resumable turn shows one way to continue. It is one explicit gesture: the
single visible action ("Continue generating" / “继续生成”) performs the
read-only check and then submits the exact budget the server proposed. The
reader never enters, computes or interprets a limit; the numbers stay in a
collapsed `data-usage-recovery-budget` detail and the consequences (extra
charges, preserved output, ordinary chat instead of Goal/Flow resume) stay
visible as prose. The click is the confirmation (`confirmed: true`) because
unmetered spend cannot be authorized silently.
`GET .../turns/{turnId}/usage-recovery` inspects one owned settled source, its
saved configuration and tool receipts without a model call or write. Supported
uncertain file publications use their existing domain inspector;
unknown/background/delegated effects without complete proof refuse. This check
cannot reconstruct missing provider usage. The proposed budget is finite and
sized from the saved request: the preview rebuilds the ordinary model messages
locally (no provider call, no budget spend) and rejects a source cap smaller than
that input plus an output reserve as `budget_insufficient` instead of proposing
a limit the first request would already exceed. `POST` requires `confirmed: true`,
the exact preview budget and source projection revision. It creates an ordinary
human/assistant pair with finite token/tool/time limits and a `usageRecovery`
confirmation receipt. Source revision, idle lane, lane tail and pending-human
fences commit in the existing `turn.create_pair` transaction on both stores.
The old Turn/Attempt and unknown accounting are unchanged. Only user-facing
preferences cross into the new config, including write-approval/readonly
restrictions and the selected browser device; old checkpoint, wallet, Goal and
Flow authority does not. The continuation prompt restates the saved original
objective as task text (never as Goal/Flow authority), and the new execution
drops only provider-issued Responses reasoning/compaction ciphertext, which a
fresh dispatch cannot replay; saved tool calls, results and readable output
stay in the message projection. Command identity owns lost-ACK retries, never another
execution authorization. The browser aborts disposed checks, fences late
replies, refreshes the existing snapshot owner after acceptance, and discards
the consumed authorization on a definitive refusal so the next click re-reads
the source. A permanent block (`not_applicable`, `operation_unconfirmed`,
`budget_insufficient`) hides the action instead of repeating it. Settlement rows
carry an explicit `usageRecovery` capability when the turn is an assistant
failure or interruption with `usageComplete === false`; a restart-interrupted
turn without a terminal error envelope still renders one recovery row rather
than inventing an error, and historical retry/compaction rows never receive it.
**Alternatives rejected:** resuming the unknown checkpoint bypasses its financial
fence; ordinary createTurn lacks source-revision/confirmation policy; a second
recovery queue or ledger duplicates existing ownership. Specs:
`tests/conversation/test_usage_recovery.py`, renderer `usage-recovery-notice.test.ts`
and `tests/visual/test_visual_usage_recovery_actions.py`.

### Live human-guidance acceptance

**Problem:** `ask_human` formerly resolved only an in-process waiter; losing that
waiter or a command ACK could lose an answer on restart. **Decision:** the existing
owned `POST .../turns/{turnId}/attempts` command accepts
`operation: answer_guidance` with an explicit `guidanceId`, `humanResponse`,
`commandId`, and `expectedProjectionRevision`. This scoped variant does **not**
create/supersede an attempt, delete a lane tail, or dispatch work. It validates the
exact persisted ask_human round and its declared choice labels (including the
legacy optional `\nuser_note: ` suffix), then commits the answer on that round
through `turn.projection.update` before waking the live gate. Streaming revisions
may advance independently: question identity and stored options, not an unrelated
text revision, fence live acceptance. Absent/foreign questions return not-found;
invalid choices are rejected; conflicting second answers return conflict.

The public reply is the existing `TurnCommandResponse` with the **original**
current attempt. Acceptance proof is the matching round's `guidanceResponse`,
`guidanceAnswerCommandId`, completed status and replayable `toolContent`, not the
original attempt's commandId/operation and never a waiter ACK. Equal-answer retries
return `idempotentReplay` without a new revision or dispatch, even after settlement.
Producer events preserve these receipts. The running executor polls the owned
Turn receipt if its waiter disappears; restart recovery retains a completed tool
result for checkpoint replay. The unscoped stopped/interrupted answer command keeps
its existing resume semantics. Legacy `/human_response` remains compatible: a
locally registered conversation-scoped gate also commits through this operation;
legacy non-conversation gates retain their existing volatile lifecycle.

**Alternative rejected:** a separate answer store or model-selected answer URL;
these would split ownership/replay authority. The browser uses only the generated
`conversationSyncApi.turn` and `.createAttempt` helpers with trusted conversation
and turn identities. It submits `guidanceId` only for live pending/running questions
and validates the returned round receipt. Storage growth is one bounded receipt
on an existing round (8,000 response characters, 256 command-id characters), no
new worker/store. One owner-bound read per existing two-second gate wait interval
is the fallback consumer; process-local notification still accelerates delivery.

A user Turn's `contextSnapshot` is historical evidence: changing project
folders or composer controls alone never rewrites it. The project panel stages
folder edits until **Apply Changes** succeeds. A later explicit `regenerate`
then sends the input Turn's current projection plus a newly captured live
context as `inputUpdate`, guarded by `expectedInputProjectionRevision`; storage
applies that input update and creates the new attempt in the same transaction.
**Edit + resend** uses this same atomic command rather than PATCHing the input
first. Consequently an accepted regeneration shows and executes with the
updated workspace, while untouched earlier Turns continue to report the
context they actually used. An input the server creates without a browser
(proactive and background-command wakes, peer messages, Feishu exchanges,
plan execution) receives a `contextSnapshot` derived from the accepted task
config when its pair is created (`create_turn_pair`): model, the
config-guaranteed tool chips and explicit workspace roots, never the
conversation's private managed root. Agent-authored inputs (Coordinator
workers and continuations, Goal and Swarm continuations) open rounds with the
same snapshot and rendering as human input; their actor, initiator and
authorization are unchanged. A supplied snapshot always wins. Older inputs
without a recorded snapshot stay as they are: no historical context is
guessed. Resume operations (`continue`,
`checkpoint_resume`, `answer_guidance`) may send a `contextSnapshot`-only
`inputUpdate` so the submitted Turn's info rail records the live model/tools
at resume without rewriting submitted content. Whenever `inputUpdate` is
present, `expectedInputProjectionRevision` is a non-negative integer; an
`undefined` or omitted-as-undefined property is not an integer and the
generated browser validator rejects the command before it reaches storage.

Regenerate also rebinds the reused generated Turn's durable `actor`/`kind` to
the normalized target interaction mode in that same transaction. Plan produces
`planner`/`plan`; Flow produces `assistant`/`flow_node`; Standard and Goal
produce `assistant`/`reply`. The command response and first attempt event carry
the migrated identity, so the initiating Store, peer replay, and later snapshot
all converge without presentation-layer role overrides.

## Background-command completion evidence

`BackgroundCommandCompletion` is bounded presentation of an existing durable
result, not a second execution state machine. `ConversationQueueItem`,
`TurnProjection`, and injection previews carry the same command ID, finished
status, command, result preview, and truncation flag. Finished does not imply
exit success. Queue membership means delivery is not yet confirmed. Model-facing
content remains unchanged; the browser renders typed evidence instead of its
notification wrapper. Both queue-list and snapshot readers use the shared pure
`lib/background_command_projection.py` projection. Specs include
`test_background_completion_queue_projection.py` and `test_background_completion_ui.py`.
New completion rows have passive `background_receipt` kind and never start a
Turn individually. Earlier `workflow_step` completion rows remain readable and
are skipped by ordinary dispatch after an upgrade. Once every command for the
owner and conversation has published, a single collective wake Turn starts
under the `turn.create_pair` lane fence; no queue row stands in for it, so a
busy lane persists nothing, and wake rows stored by earlier releases are
retired without a Turn. Queued human or other work takes priority and consumes
the receipts without an extra wake. That Turn rehydrates up to 32 durable
receipts into its
background-command inbox lane; the coalesced inbox message presents them at
one model boundary, and the confirmed receipt ACK removes only those rows.
Remaining receipts stay visible for a later Turn. Startup and maintenance scan
passive receipt summaries separately to rebuild missed collective wakes.
The queue's `preview` list mode and conversation snapshot bound each item's
display text to 2,000 characters; the `background_pending` mode alone reads
the complete model-facing payload, capped at 32 rows per real turn.

For mid-turn delivery, `TurnInjectionPreview.queueId` links the receipt to the
exact durable delivery intent (distinct from execution `commandId`).
`turn.event.record` commits receipt, eligible queue deletion/renumbering, task
replay carrier and `AttemptEvent.payload.removedQueueIds` in one transaction.
The reducer applies removals only after the receipt's attempt/sequence/revision/
patch validation: subscribers never see an accepted receipt beside its old
pending card. Remaining queue ranks are derived in the same priority/position/ID
order as storage renumbering, so live cards agree with cold snapshots. No DOM
matching, polling or browser delivery ledger is involved.

The writer checks owner/conversation, command identity, passive or legacy
workflow kind and absent
Turn/Attempt links. A present protected/mismatched row rejects the whole commit.
Event/carrier/delete failure rolls back both halves and cannot leak a confirmed
executor sidecar through later checkpoints. The emitter retains one frozen
receipt and makes at most two commit attempts per verified provider boundary,
without another model call; stable block identity makes lost-ACK rebase
idempotent. Pending delivery is capped at the existing 256-item inbox bound;
excess twins stay in the bounded inbox, backed by durable queue rows. Successful
commit clears only that batch and fences late twins. Missing rows are safe on
replay; old receipts without queue identity never authorize inferred deletion.
Provider consumption itself is not transactional: crash before the authority
commit may still redeliver. Specs: `test_background_delivery_receipt.py`,
`test_background_delivery_ui.py` and `test_queue_injection_ack.py`.

A pending background completion does not block immediate user insertion: a
`steer` submission commits its own injection on the live Turn and leaves the
completion's durable queue row and inbox twin pending. The API boundary is
covered by `test_conversation_command_inject_mode.py`.

A steer carries text only, so a message's `replyQuotes` travel inside it: each
quote becomes a Markdown block quote before the content, in both the delivered
inbox value and the committed injection preview (a queued Turn keeps
`replyQuotes` on its projection instead). A quoted answer to a question the
model asked therefore reaches a live turn with its question. Covered by
`test_conversation_steer_reply_quotes.py`.

When hydrating a lane-busy conflict fails with a storage error, preserve the
typed storage failure and its retry contract at the HTTP boundary (temporary
capacity failures return 503). A successfully read but absent live Turn uses
the existing durable queue fallback, with `steerFallback: true`. Problem:
masking a failed read as an empty conflict caused a null dereference and a
redacted 500 toast. Decision: preserve failed-read authority; tolerate an absent
Turn only through durable queue admission. Alternatives: silently treating
resource exhaustion as a missing attempt hides the cause; using cached task
identity risks delivering to a stale attempt.

Hydration walks the lane newest-first through `turn.sync.page`, one Turn per
page together with that Turn's current attempt, and stops at the busy Turn; a
stale page resumes at the same cursor under the head it reports. Problem: a
whole-lane `turn.list` is refused once a long conversation's projections exceed
the authority's response bound, so every queued or inserted input sent during
such a reply failed as an internal error. Decision: a single-Turn page fits
whenever the busy Turn itself fits and reads no history older than it.
Alternatives: a multi-Turn or byte-windowed page needs a page size or byte
budget that nothing here defines; naming the busy Turn in the `turn_in_progress`
refusal would remove the walk but changes both authorities' error contract.
Covered by `test_steer_live_attempt_target.py` on both authorities.


The v3 queue-cancel command also accepts pending background completion rows
without a Turn pair. It removes only delivery intent, returns `inputTurn: null`
and no deleted Turns, and atomically publishes `removedQueueIds` for replay.
Already-leased delivery conflicts rather than claiming to stop dispatched work.
After commit, the application removes/fences the process-local inbox twin;
completed command execution evidence and unrelated queued inputs are preserved.
Specs: `test_background_completion_cancel.py`.

Completion publication retries transient storage errors at most three times
with a frozen payload and owner/conversation/command-derived queue identity.
A lost ACK replays the same durable receipt. Retry sleeps total at most two
seconds, without another worker or retry queue. Explicit command queries expose
`delivery_status`: pending, queued, failed, not_applicable, or undeliverable
(owner conversation confirmed absent and no longer creatable; settled once,
never retried — see [Storage operations](../STORAGE_OPERATIONS.md)). Before the first
durable commit, process failure can lose the result; exhausted steer-handoff
publication is logged, not restart-recoverable. Deferred acknowledgement requires
positive verified provider completion, never an error-dictionary return.

The process-local inbox fences the last 16,384 `(slot, queue ID)` pairs in FIFO
order without payloads. Dispatch fences even an absent twin; enqueue rejects
late publication under the same lock. Clear/revival preserve fences. Protection
ends at FIFO eviction or process shutdown, and retains the existing slot
ownership boundary. This is not durable delivery history.

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

Pending/running projections preserve the exact committed revision document in
snapshots, history pages, command responses, and embedded replay Turns. Public
read adapters must not append terminal placeholders, repair segment chronology,
or refresh stale text/thinking mirrors at the same revision: a subsequent
positional `append`/`append_text` would otherwise apply to a different base,
duplicate live narration, or shift tool paths. Slim writes intentionally update
only the cumulative channels; the renderer reads those channels for terminal
blocks without rewriting Store state. Producer-side normalization remains
explicit, and Sidecar includes its repair delta in the next committed structural
patch. Settled legacy reads keep their existing compatibility normalization.
Specs: `test_live_snapshot_and_command_bases_replay_without_duplicate_narration`
checks every replay revision from early snapshots, with stale text/thinking mirrors
and cold producer caches. `tests/e2e/test_e2e_pending_narration.py` holds the early
command and tool-execution windows open in Chromium, then checks cold reload.
It is included in `make test-e2e`, not just the wider visual tier.

Persisted turns decode fail-closed: a stored shape the schema no longer
accepts raises `ContractViolation` instead of silently mis-rendering.
Declared legacy shapes cross the boundary only through
`TURN_READ_ADAPTERS` in `lib/conversation_sync/service.py` — the explicit,
ordered registry of copy-on-write lifts (today: pre-`attemptId` client
observations). Every registered adapter is pinned by a legacy fixture
under `contracts/fixtures/sync_v3/` that must fail raw decode and pass
after adaptation (`tests/conversation_sync/test_wire_fixtures.py`); a shape without a
fixture is not an adapter.

The endpoint defaults to `segmentPayload=full` and a complete Turn set for
independent and older clients. The generated browser client fixes
`segmentPayload=refs&toolPayload=lazy&turnWindow=tail-96&artifactHint=has-any`. The artifact
selector is an additive wire-shape opt-in: the owner-scoped snapshot transaction
uses a bounded `LIMIT 1` existence query on SQLite and returns `hasArtifacts`,
never artifact metadata. Native TofuDB uses its existing conversation artifact
count to certify only an empty result; positive counts include tombstones, so
it omits the hint and leaves live presence to the owned artifact-list reader.
This is one owner/tenant-scoped point read, without new indexes or blob reads.
Returning a constant `false` hid existing deliverables; treating a positive
count as proof of live presence would misclassify deleted ones.
`false` lets the browser commit an empty artifact read model
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


**Viewport and byte-aware initial history.** Problem: 96 Turns is not a byte
bound; a live browser sample still received 32,437,621 decoded JSON bytes with
refs/lazy enabled. Decision: the browser supplies additive `windowLimit` and
`windowBytes` selectors. The retained host probes the actual chat pane; the pure
policy requests `ceil(height / 160) * 2 + 4` Turns (8..96) and a soft projection
budget scaled from 1 MiB at 960×800 (256 KiB..16 MiB). Older-history demand starts
from the same byte budget with its existing count and exact replay-head fence; a
continuous page chained from a page that bytes cut short grows it (see Older
history below).
Missing/invalid viewport dimensions use 960×800. No prefetch pool, worker,
resident snapshot cache or durable-state truncation is introduced.

The semantic `turn.sync.snapshot` / `turn.sync.page` operations accept optional
`max_projection_bytes`: 0/omission preserves legacy count-only behavior;
positive values admit 64 KiB..16 MiB. SQL selects owner/lane-filtered UTF-8
projection/checkpoint/patch-size witnesses before reading selected payloads.
Native reads use stored-document/checkpoint/text-tail size witnesses before
materialization. Both retain a newest contiguous suffix, always admit one
indivisible Turn, and keep cursor, total, current attempts and replay head in
one transaction. This is a soft projection estimate, not a strict HTTP-body
ceiling: metadata, JSON escaping, patches and an oversized single Turn can
exceed it. Full/branched snapshots retain complete topology. Different byte
budgets participate in snapshot single-flight and browser admission identity.
Alternatives rejected: slicing already-decoded history does not save storage
work; a hard bound that drops an oversized Turn strands its history cursor;
fixed small windows leave large reading panes underfilled.

Successful snapshot responses expose content-free `Server-Timing` metrics:
`snapshot` includes scheduling, storage and projection/validation; `encode`
measures response encoding. Proxy waiting, transfer and browser rendering remain
separate measurements.
Problem: a fixed tail/history page can exceed native response capacity and
repeatedly fail with `database_resource_exhausted`. Decision: after that exact
storage failure, bounded repository reads halve their requested Turn count down
to one. Every successful response remains one owner-scoped storage transaction,
with its original replay head, current attempts, and continuation cursor. Normal
reads retain their requested count; unwindowed snapshots and failures at one
Turn remain errors. Alternatives: retrying an identical page cannot resolve a
byte bound; slicing a decoded page cannot recover a rejected response and would
break cursor completeness. No durable records are removed or rewritten.

Older history uses the generated `turnPage` operation. The caller supplies its
currently applied `syncSeq`, lane, exclusive `beforeOrdinal`, and a page limit
of at most 256. Storage checks owner and replay head in the same read
transaction, reads `LIMIT + 1` oldest-first, and returns only current attempts
for those Turns; a storage read at any other head returns stale and no page.
The application service repeats a stale read at the newer head storage
reported, at most four reads (the head check precedes every Turn read), so the
response's `syncSeq` names the head the page is exact for, never one older
than the caller's. Only a head behind the caller, or one that keeps moving
through those reads, returns 409 (`history_page_stale`, carrying
`currentSyncSeq`). In the browser, identical requests share one flight, a
different overlapping request is rejected instead of queued, and
identity/reference/sequence validation plus the non-authoritative Store merge
happen in one synchronous coordinator turn. The continuous window and detached
jump hydration hold one flight slot each, so a round jump never fails against
the Surface's own history read.

Problem: a streaming reply commits an attempt event every few hundred
milliseconds and the browser's replayed head trails the server by the stream
latency. Under an exact-head fence nearly every history read lost that race:
the 409 reported a head that had moved again by the time the browser caught up
and re-read, and a continuous page that arrived after any further streamed
event was discarded. Scrolling up on a live conversation showed "loading
earlier messages" through eight catch-ups and two full snapshot replacements
(megabytes each on long conversations), then failed. Decision: answer at the
newest head not older than the reader's, as rank reads do, and publish in the
browser at a head the replica has replayed. A page read ahead of the replica
waits (bounded, 2 s) until the live stream applies exactly its head; a page the
replica already reached publishes unless a Turn change or snapshot
(`roundRevision`) was applied after its read began. Streamed attempt events
patch only the live Turn, never older Turns or lane membership, so both modes
share this fence; a continuous page additionally advances the lane's history
cursor and conversation revision, which therefore never run ahead of deltas
still in flight. A detached page adds its missing Turns and attempts but never
overwrites present ones, moves `conversationRevision` or advances a cursor. A
409 head the live stream can deliver is awaited and read again, up to eight
times; only a head the stream cannot deliver (paused, wedged, a 409 without a
head, or a page head not applied within the wait) is repaired from one
authoritative snapshot, at most twice; a wedged stream with no frames is
exactly the case that otherwise never self-heals. Alternatives rejected:
retrying in the browser at its own head (every attempt loses the race by a
round trip plus stream latency); publishing a page read ahead of the replayed
stream immediately (a continuous page's revision would reject the Turn deltas
still in flight and force a snapshot); always repairing (each snapshot
re-reads every loaded history page); unbounded retry (a broken projection never
converges). Any other status is published unchanged.

**History read-ahead and chained page budgets.** Problem: the Surface asked for
an older page only once the reader was within 1.5 viewports of the mounted top,
and every page used the viewport byte budget (about 1-1.7 MiB). An agent Turn
carries 0.7-2.2 MiB of process evidence yet renders folded, a few hundred pixels
tall, so every page held exactly one Turn; the small input Turn even travelled
alone because the older reply did not fit beside it. A brisk reader therefore
reached the loaded top before most 0.15-1.3 s round trips completed. Separately,
the Surface retired a flight when its request promise settled, one frame before
the Store render that carries the page: the idle boundary painted over the old
window first (a 34 px jump at the top) and a reader frame queued in between
requested the page just read again. On a synthetic 28-Turn agent history read
upward at 2400 px/s with the measured production latency profile
(`tests/visual/test_visual_history_paging_latency.py`), the reader waited 3.3-3.4 s
of 7.8-7.9 s at the loaded top across 27 one-Turn requests and saw 13 jumps.
Small-Turn chats were unaffected only because the former rule (fetch once at most
20 loaded Turns remain unmounted) happened to leave about 13 viewports above the
reader. Decision: on reader demand toward earlier content, the Surface requests
the next continuous page while less than twelve viewports of loaded content
remain above the reader (mounted Turns measured, loaded but unmounted Turns
counted at their mean mounted height), which covers a p99 page (2.5 s) at a brisk
2400 px/s; a failed page retries only on a renewed gesture within the residency
prefetch distance. A settled flight holds its lane until the render whose
advanced cursor retires it in the same anchored commit that mounts the page; a
page no render consumes retires on the following frame. The coordinator doubles the byte budget
of a continuous page whose `beforeOrdinal` is the previous published page's
`nextBeforeOrdinal` when that page was cut short by bytes (fewer Turns than its
count while more remain), up to four viewport budgets and the 16 MiB ceiling; a
count-bound page, a detached jump and any unchained read use the viewport budget.
The same journey then takes 6 requests of 6 Turns and waits 0.7 s of 5.0 s (the
slow-start pages), with no jumps; with a slower service (400 ms + 300 ms/MiB) the
wait falls from 11.8 s of 16.6 s to 3.2 s of 7.5 s. A 240-Turn chat keeps its 4
count-bound 64-Turn pages, starts each read with about 12 viewports loaded above
the reader and never waits. Larger pages make the single longest wait longer
(0.4 s instead of 0.3 s) while total waiting drops fivefold. Alternatives
rejected: one larger fixed history budget (a slower first page while the reader already waits at the
top, and oversized pages for every conversation); parallel page reads (the next
cursor exists only after the previous page, and overlapping continuous reads are
refused); chaining pages without reader demand (pages a whole history unasked);
a larger Turn count (count-bound chat pages never made the reader wait). The
Surface's 80-Turn DOM window still anchors on stable `turnId` and reveals newly
prepended Turns without replacing the render path; loaded Turns beyond the window
serve later local window moves without another API round trip.

**Recovery keeps loaded history.** Problem: every recovery snapshot
(`sync.reset_required`, a sequence or projection gap, a closed EventSource, a
`conversation.activity` refresh, a TurnStore resync) is a byte-budgeted tail
window, and while a long Turn streams it often admits only that Turn. The
browser applied it as a full replacement, so every older Turn the reader had
loaded was unmounted until the Surface paged it in again: the whole transcript
flashed away and back mid-run. Decision: an `authoritativeFull` snapshot with a
`turnWindow` that `hasMore` is authoritative from the window's oldest ordinal
up. Loaded Turns of that lane below it stay in the Store as the same objects,
and `historyByLane.retainedFromOrdinal` marks them unverified. After the
snapshot cursor is committed and the stream reopened, the runtime re-reads that
range through the continuous frontier (`turnPage` at the lane's
`nextBeforeOrdinal` with the Surface's page size, so a reader-driven read joins
the same flight). A continuous frontier page lists every lane Turn in
`[its oldest ordinal, beforeOrdinal)` (everything below `beforeOrdinal` when
`hasMore` is false) at its fenced head, so it removes the retained Turns it
omits in that range; this is the only deletion a history page performs.
Detached and off-frontier pages stay additive, and a snapshot without a tail
window still replaces every Turn. Alternatives rejected: reading the older
pages before applying the snapshot (pages are fenced to a replayed head that
exists only after the snapshot is applied and the stream reopened); retaining
without re-reading (a compaction or another device's deletion made while the
stream was down would stay visible); widening the recovery window server-side
(changes both snapshot owners and still cannot cover a range larger than the
byte budget); holding the old DOM in the Surface (the display would disagree
with the Store). Specifications: `frontend/src/core/turn-state.test.ts`,
`frontend/src/core/turn-runtime-history-recovery.test.ts` and
`tests/visual/test_visual_recovery_keeps_history.py`.

**A retained range converges.** Problem: the re-read ran once, right after
the snapshot, and returned unless a stream was open at that instant; a failed
re-read was dropped. A hydrate of an inactive settled conversation applies its
snapshot without a stream, and such a conversation pauses its stream the moment
its last Turn settles, so enrichment committed after settlement (incremental
translation stamps and the terminal translation land seconds later) never
reached a retained Turn larger than the tail window: it kept its
pre-translation revision, showing only the stray live previews the push
socket had happened to deliver, until reload. Decision: the coordinator reports every stream it opens
(`onStreamOpened`), and the runtime arms the same single-pass re-read then as
well as after the snapshot, while the range stays unverified. Opens are already
rare and backed off, so a failing read retries at that pace; no timer, retry
count or threshold is added. Alternatives rejected: retrying on every frame (a
request storm while a running Turn streams), keeping inactive streams open
after settlement (an arbitrary grace window that still misses later
enrichment), and dropping retained Turns (the flashing transcript above).
Specifications: the `retained Turns of a conversation the reader left` cases
in `frontend/src/core/turn-runtime-history-recovery.test.ts`.

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
server evidence only. Running or ambiguous tool segments, unfinished tool rounds
in non-completed Turns, and every ordered replay event remain full. The
coordinator resolves each reference and attaches the same round/input/result
objects before publishing the snapshot to TurnStore; an invalid reference is a
protocol error, never an empty tool card. The reference view is request-local
and does not mutate the shared authoritative snapshot or create another stored
format.

**Terminal browser view diet.** Problem: one long completed agent Turn sent
the browser about 2 MiB per snapshot or history page, most of it never
rendered: thinking signatures twice (each `thinking` segment's `signature` and
the round's `thinkingSignature`, 793 KiB of 2,064 KiB in a 2026-10-28 live
sample), task-trace spans and status history, and the cost popover's
unrendered admission/recache detail. Signatures are incompressible, so Brotli
kept 577 KiB on the wire. Decision: the terminal-Turn `refs` view also omits
both signature fields (with the provider replay bodies above), keeps only the
`writeBreakdown` allocation terms (`write`, `prevOutput`, `toolResults`,
`contextWrite`, `recacheBody`, `envelope`, `readDrop`, `capped`) and the
`promptAdmission` totals (`action`, `totalTokens`, `messageTokens`,
`toolSchemaTokens`, `targetTokens`) the lazily built cost popover reads (the
recache cause duplicated `cacheBreak`), and reduces `timingTrace` to the
settled clock the finish footer reads (`version`, `taskId`, `running`, `tEnd`,
`clockSource`, `coverage`, `totalMs`). The span list survives whole only when
it contains a compaction span, whose duration a compaction activity row
displays; it is never filtered, so a positional patch fails and resynchronizes
rather than addressing a shifted index. The Request Inspector trace reads its
own task-trace endpoint. Model requests read signatures from storage, never a
browser projection. A whole-projection edit is the one path that writes a
browser copy back: `update_turn` restores every omitted value from the
CAS-fenced stored revision (`projection_with_restored_browser_evidence`), but
only where the client left it absent or equal to the browser view; replay
evidence also requires the signed thinking to be unchanged. The same repair
closes the pre-existing loss of provider replay, apiRound diagnostics and
budgeted swarm timelines on edits. Completed, interrupted, truncated and failed
Turns share this view: Continue always reads complete evidence from storage.
Missing or duplicate tool call IDs keep replay evidence inline because an edit
cannot unambiguously restore it. Pending/running Turns and the SSE stream are
unchanged: live projections and replay patches remain the
exact committed revision, which positional patches require. The browser's
thinking view model no longer carries `signature`, so a live-settled Turn and
its later slim snapshot present identical reasoning blocks; a tool card still
repaints once if a recovery snapshot replaces a live-settled round, as it
already did for provider replay bodies, and browser source never names these
server-only fields (`test_browser_does_not_consume_server_only_snapshot_evidence_fields`). Alternatives rejected: stripping signatures
from SSE frames would make live patch bases diverge from the committed
document; an on-demand fetch for the cost popover or trace would change
retained runtime sections for data that already has a slim closed-world
projection. Measurements (2026-10-28, two live completed Turns replayed
offline through the real service with an injected repository): a one-Turn
tail snapshot fell from 2,119,724 to 966,808 bytes (54.4%) and from 577,549
to 176,531 Brotli bytes; a one-Turn history page holding a 13-agent settled
swarm fell from 1,465,044 to 665,975 bytes (54.5%) and from 349,742 to
139,484 Brotli bytes, including the settled swarm budget and string exit-code
laziness below. With the specialized validation predicates, thread CPU of
the service read fell from 11.3 to 8.6 ms and 9.0 to 6.6 ms; adding orjson
encoding and the live Brotli quality, per-request server CPU fell from 23.1
to 19.6 ms and 18.4 to 15.0 ms. Smaller bodies under 1 MiB move from Brotli
quality 2 to 4, which keeps compression CPU nearly flat (11.1 to 10.6 ms)
while wire bytes fall 69%. Extending this same view to all terminal statuses
(2026-11-05, one actual four-Turn snapshot with 2,243 tool rounds in an interrupted
Turn) reduces JSON from 57,162,940 to 20,367,437 bytes (64.4%); the interrupted
Turn alone falls from 51,402,197 to 14,606,694 bytes. Five paired Chromium parses
on the same host give median 176.7 ms before and 61.2 ms after. Stored replay
fields round-trip exactly and the running Turn stays byte-identical. This is a
reduction in snapshot parsing work, not a claim that all rendering stalls are
eliminated. Moving the same JSON to a worker was rejected: structured cloning
retained main-thread cost and increased total elapsed time in the paired probe.
Specs: `test_terminal_browser_view_omits_replay_signatures_and_unrendered_detail`,
`test_client_edit_of_browser_view_restores_omitted_server_evidence`, and the
HTTP round trip `test_refs_view_edit_round_trip_keeps_server_only_evidence`.

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

That same browser view content-addresses repeated `toolContent` and `results`
values, including pending/running Turns, whose canonical JSON is at least 1 KiB.
Full SHA-256 keys address at most 256 shared documents and 4,096 references in
one response; unique, conflicting, over-budget, or unserializable values remain
inline. Equal canonical documents have equal encoded lengths, so only documents
whose length repeats are hashed (the first of each length waits request-local
for a second); on the same sampled live Turns this cut the interning pass from
0.28-0.39 ms to 0.09-0.20 ms with identical output. The generated schema accepts only the two declared reference fields,
full digest keys, and the bounded dictionary. Before any state publication,
the browser restores the exact shared values into shallow-copied rounds,
resolves segment references against those rounds, and discards the dictionary.
Missing documents, inline/reference conflicts, malformed keys, or a non-array
`results` value are protocol errors. Thus the wire and parse work shrink while
durable storage, the independent-client `full` response, TurnStore shape, and
replay events remain unchanged; no response cache or cross-request lifetime is
introduced. Active interning is lossless wire compression, not deferred detail:
restoration completes before replay and a later patch copy-on-writes shared
arrays, preserving both sibling Turns and old immutable reader state. Active
segment normalization, private-field omission and lazy output remain disabled.

**Problem:** active tool evidence can repeat large text and structured results
across rounds while terminal-only interning leaves those snapshots oversized.
**Decision:** extend only the existing request-local shared-document view; restore
exact fields before ingestion. **Alternatives rejected:** truncation loses evidence;
active lazy fetching leaves incomplete positional patch bases; a completed-response
cache weakens freshness. Specs: snapshot singleflight/document-reference tests
cover pending/running exact restoration and subsequent append/text replay.

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

New turn commands carry an already-uploaded image by its canonical
`/api/images/<filename>` URL and URL preview; the browser keeps the source
base64 only in its pending draft. Problem: copying both the original base64 and
its data-URL preview into `turn.create_pair` duplicated an image and could exceed
the storage operation's 8 MiB request bound even after upload had succeeded.
Decision: compact only confirmed local upload references when constructing the
turn command, leaving images without a durable URL inline so upload failure does
not silently discard them. Alternative rejected: raising the storage frame
bound would retain duplicate user media in every durable turn and increase
memory and replay cost. An oversized turn still returns an actionable 413;
no conversation or turn is committed by that pre-dispatch rejection.

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

Continue is one logical Turn: new root `llmRound` and tool `roundNum` advance
from retained checkpoint chronology. Validated progress, images, file receipts
and cumulative billed usage survive; replay supplies saved results to the
model, not a second tool execution. A successor `attemptId` / `taskId` remains
an internal stale-write/receipt fence, not a visible Attempt 2. Historical
execution-local counters stay readable: presentation folds resets into
continuous Round labels without rewriting saved rows. Producer scope still
owns batch keys and exact request-inspector links; equal counters from distinct
executions are never merged. Legacy unstamped rows are frozen under the
outgoing owner and retain contiguous-occurrence compatibility.

**Problem:** resetting counters and retaining a later suffix made Continue
look like a fresh request over incompatible history. **Decision:** atomically
replace that suffix and continue the checkpoint's logical round frontier in
executor events, storage and UI. **Alternatives:** a visible A2 label merely
explains the discontinuity; removing internal execution IDs breaks zombie
fencing and exact receipt inspection. Neither provides seamless recovery.

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
(no provider continues an unfinished reply from a prefill; see
[rewind and resend](TURN_SETTLEMENT.md#interruption-recovery-rewind-and-resend)),
the discarded text moves
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
crosses the authoritative snapshot boundary, including the server's
`replay_exceeds_window` verdict for a missed suffix that outgrew the snapshot
window (see [Catch-up budget](#catch-up-budget)).

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

The shell's `_serverTurnCount` is the main-lane total that the TurnRuntime
shell reconciler projects from the snapshot window (`totalTurns`), the same
quantity as the catalog row's `msgCount`; hydration never replaces it with the
number of loaded Turns. Recording the tail-window size made the next catalog
merge (another conversation changing, tab return, reconnect) see a changed
body for every visited conversation larger than its window, re-arm
`_turnSnapshotRequired` and download the whole window again on the next click.
A newer catalog revision still requires one snapshot. Specs:
`tests/conversation_sync/test_catalog_hydrated_window_count.py` and the warm
revisit budget in `tests/visual/test_visual_conversation_switch_paint.py`.

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
authority/reference values are read-only during JSON serialization. Raw connection
health updates remain timestamped diagnostics. The typed runtime updates the
conversation shell before visual notification, suppressing only consecutive
healthy `live` callbacks with unchanged transport/reason. Initial/ownerless,
trouble and recovery observations still reach the notice owner; no new timer
is introduced. Aggregate degraded count is maintained incrementally and notifies
only count transitions; publication identity prevents stale callback evidence
when nested clear/set returns to the same count. Required projection ingestion,
cursor acknowledgements and actual DOM perception receipts are unchanged.
**Problem:** per-frame healthy evidence triggered redundant sidebar/Surface work.
**Decision:** separate raw evidence from unchanged healthy visual demand.
**Alternative rejected:** suppressing all equal health states breaks the existing
trouble-notice debounce. Specs: `turn-runtime-health.test.ts`,
`connection-health.test.ts` and the native EventSource recovery browser suite. Only Turns
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
conversation values. Each predicate is specialized when compiled: undeclared
length/size/range bounds cost nothing per value, exact `dict`/`int`/`float`
values take a direct type test (bool, subclasses and other Mappings keep the
general path), required keys are one dict-keys subset test, and each
`additionalProperties` mode has its own member loop. Problem: on a live 2 MiB
completed Turn, validating the shared snapshot authority was half of the
offline service read (5.5 of 11.3 ms), mostly ABC and per-call bound checks.
The same authority now validates in 3.3 ms; the synthetic benchmark below
moves from 1.33 to 0.64 ms. Skipping or sampling validation was rejected: the
authority stays fully validated at the contract boundary.
`test_specialized_predicates_match_diagnostics_on_mutated_wire_fixtures`
mutates sampled nodes of both committed wire fixtures and requires the
diagnostic verdict.


**Problem:** tool-heavy snapshots repeat exactly-one branch checks for every
segment, and reference sizing can encode the same terminal content twice.
**Decision:** the compiler dispatches a `oneOf` directly only when acyclic
object variants all require one field with distinct string constants. It still
validates the entire selected branch. Optional, overlapping, recursive or
unsupported tags retain exhaustive exactly-one checks and unchanged diagnostics.
Reference projection sizes content only after a unique matching segment exists,
then reuses its exact UTF-8 JSON length plus the compact object wrapper. Existing
minimum-size, net-savings, ambiguity and active-Turn rules remain unchanged.
**Alternatives rejected:** TTL snapshots weaken freshness; skipping validation
weakens integrity; character counts mismeasure escaped/Unicode wire bytes.
`benchmarks/conversation_snapshot_cpu.py` compares frozen source with current
code on synthetic data only. Its component timings exclude production storage,
HTTP scheduling and durability and are not production latency certification.
After generated-schema validation, the snapshot HTTP adapter uses compact
`orjson` bytes rather than Quart's stdlib encoder. Unsupported values fail
soft to the existing `jsonify` provider. Snapshot response construction and each
demanded SSE frame run on the existing demand executor. The stream awaits one
frame before advancing, retaining ordering/backpressure without a replay queue.
Cancellation joins that exact encoder before releasing snapshot admission or
subscription ownership; simultaneous failure/cancellation retain both causes.
Supersession withdraws publication permission after page reads and each joined
encoder, and before starting another frame; an old generation neither emits
late bytes nor encodes the remainder of its page.
SSE uses compact UTF-8 `orjson` with the previous stdlib behavior for unsupported
or non-finite values; transport optimization never silently changes evidence.
**Problem:** large response/frame encoding occupies the serving loop.
**Decision:** move finite encoding to its existing worker boundary, with joined
cancellation. **Alternatives rejected:** eager frame pre-encoding retains replay
backlogs; abandoning a cancelled encoder releases borrowed DTOs too early;
increasing poll frequency creates more work. Specs: `test_conversation_sync_encoding.py`
and stream ownership/setup tests. Synthetic encoder microbenchmarks exclude
storage/proxy/browser time and do not certify production RTT improvement.
Dynamic responses are still compressed off the serving loop by the profile-aware HTTP compression policy
owned in [`modules/infra_runtime.md`](../modules/infra_runtime.md); they do not
enter its static-artifact cache, so no conversation response is retained after
delivery.

### Lazy historical read-tool output

**Problem:** a short conversation can contain hundreds of read-tool results; Turn-count windows alone leave multi-megabyte output on the initial decode path. **Decision:** the generated snapshot and history clients opt into `toolPayload=lazy` together with `segmentPayload=refs`. Completed `read_files`, `search_files` (and its retired name `grep_search`), `find_files`, `list_dir`, `get_conversation`, `repo_map`, `read_tool_artifact` and `search_tool_artifact` rounds whose exact string `toolContent` exceeds 4,096 characters publish `_snapshotToolResult`. The reference carries owner scope, conversation/Turn/call identity, original round index, exact projection revision, character count and at most 240 UTF-16 units of preview. Unique call identity and referenceable segment mirrors are required. Missing inline output is explicitly deferred, never an empty result. Results metadata, tool arguments, status and execution provenance remain. Proven-success `run_command`/`code_exec` rounds additionally defer structured `results[].output`: every result must have a zero `exitCode` (integer `0` or the producers' canonical string `"0"`), with no timeout, interruption, rejection, error, background or not-run evidence. Ambiguous/legacy-only and unsuccessful commands stay inline. The producers (`run_command`, `code_exec`) stamp `exitCode` as the exact decimal parsed from the executor's end-anchored marker, so the canonical string `"0"` is the same proof as integer `0`; every other string (`"1"`, `"timeout"`, `"not-run"`, `"background"`, `"?"`, padded or other spellings) stays inline. Problem: the integer-only rule never matched a production command (0 of 68 sampled `run_command` rounds deferred, 365 KiB inline in one Turn). Alternative rejected: changing producers to integers would rewrite stored evidence and every exit-badge consumer. Command references carry `fields: [toolContent, results]`, and the revision-fenced detail restores the exact original result array to a display clone only. One native keyboard- accessible disclosure is retained after hydration; initial exit status and command metadata never depend on expansion. This avoids duplicate structured stdout while preserving all command failure evidence. Swarm lifecycle payloads and diagnostics remain inline because text-only restoration cannot replace their identity/recovery consumers.

The exact command-restoration fixture measures 44,615 → 1,020 UTF-8 JSON bytes
(97.7% smaller); this is a synthetic transport measurement, not a live latency
claim. Native/HTTP specifications drain byte-windowed history exactly once,
retain an oversized single Turn and reject wrong-owner/stale-head reads. Real
Chromium verifies cold/warm request reuse, late-result isolation, keyboard
command hydration with computed output visibility, HTML escaping and retry/
navigation-abort behavior.
Active, resumable, failed and other specialized-tool projections stay complete;
independent clients default to the existing inline `full` tool payload.

`GET /api/v4/conversations/{conversationId}/turns/{turnId}/tool-results/{roundIndex}`
requires `projectionRevision` and `ownerScope`. The repository uses the existing
owner-scoped `turn.get`; the service verifies completion, exact revision and
round identity before validating and returning original `toolContent`. A wrong
owner/missing result returns 404; a changed revision returns 409. Responses use
`private, no-store`, so deletion or authentication changes cannot reuse an
indefinitely cached private output. Opening a mounted disclosure fetches once;
closing or retiring it aborts pending work, transient errors offer retry, and
stale errors request conversation refresh. Loaded detail belongs to the mounted
DOM block only and never writes TurnStore or a live projection patch base.

Readonly terminal projections never become a writable patch base: a forward
attempt/delta patch against a deferred Turn requests an authoritative snapshot
before advancing its revision. Active snapshots return complete output. A whole
projection edit first makes one owner-scoped `GET /turns/{turnId}` read, verifies
its projection revision and restores complete tool-round and segment lanes.
A changed revision refuses the write and publishes the latest Turn for review;
output is never persisted as a preview or missing field. This full read happens
only on explicit edit, not on navigation or expansion of every historical card.

**Alternatives rejected:** replacing output with result metadata loses original
content; cutting stored output weakens durable evidence; inferring empty output
from an omitted field hides completeness; caching snapshots by Turn revision
alone misses queue/settings/activity changes outside that projection version.
Existing burst sharing continues to read fresh authority rather than adding
such a cache. Verification lives in
`tests/conversation/test_conversation_lazy_tool_results.py` and
`tests/visual/test_visual_conversation_navigation_performance.py`.

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

Problem: bounding the observer's one-token queue alone still permits one
thread-safe event-loop callback per commit to accumulate before the loop runs.
Decision: each subscription coalesces wake hints before scheduling, retaining
one pending callback/token until its reader consumes it. Producers never wait
for an observer read or network send. Consumption and latch release happen in
the same asyncio task so cancellation cannot strand a consumed hint. Close
still interrupts the reader and releases its exact resources once.
Alternative considered: coalescing only inside the loop callback leaves the
loop's own callback queue unbounded; dropping durable changes is unacceptable.

`benchmarks/conversation_wake.py --commits 10000` measures this lane offline
with the observer loop paused, using disposable state and Python allocation
tracing. On Python 3.12.13, a controlled before/after run scheduled 10,000 → 1
wake callbacks, with peak traced allocation 2,403,333 → 804 bytes and hint
publication 82.053 → 35.286 ms. These are instrumented microbenchmark samples,
not end-to-end conversation latency or storage-capacity acceptance results.
`test_conversation_sync_wake_budget.py` covers blocked-loop concurrent bursts,
token consumption/rearming, timeout, cancellation, and exact close cleanup.

### Hot-path event storage (2026-09-21)

Problem: `turn.append_settled` spent ~18-24 µs/op in its sync-event stage
(`ta_sync`): the event store re-parsed the just-serialized event JSON for
validation, re-read the fresh event key for a version check, built a
projection response the caller discarded, and re-validated the public turn
through `RawValue`.

Decision: the settled-append path assembles the event and response bytes by
splicing the already-encoded public turn (pinned byte-for-byte against the
derive encoding), and inserts through
`versioned_document::put_fresh_prevalidated`, which stores the byte-identical
versioned-document envelope without the re-parse, the existence read, or the
discarded response. Key freshness is the head-allocator invariant above:
concurrent same-conversation appends still fail closed through the head row's
commit-time write witness, and the offline importer restores head and events
consistently. Only a store that already violates the head invariant would see
a silent replace instead of a conflict error. `ta_sync` measures ~8 µs/op
after the change.

Alternatives rejected: (a) slim events that reference the turn document —
replay (`turn.sync.changes`), `lib/conversation_sync/service.py::changes`,
and the browser reducer (`frontend/src/core/conversation-sync.ts`) all
require the full `payload.turns` bytes on the hot path, so the payload is
contractual; (c) deferring or batching the event write — replay cursors and
read-your-writes after commit require the event in the same transaction.

## Bounded changes

The permanent turn row owns the full projection. Mutations to an existing turn
carry a revision-to-revision `projectionPatch`, never another cumulative copy.
The reducer applies a patch only when base revision, target revision, operation,
and path all validate. A missing or invalid patch triggers one authoritative
snapshot recovery.

Full projections are allowed in snapshots and bounded new-turn events. A
multi-turn graph rewrite such as compaction emits a small
`conversation.activity` event with `requiresSnapshot` and re-anchors once.

**Problem:** `activityTimeline.entries` and `timingTrace.statusHistory` keep
their newest 128 rows. Once full, every new row shifted each retained index, so
the index-aligned list diff rewrote every field of every row: a sampled live
Turn carried up to 1,813 operations (124 KB) in one patch, durably and on every
stream. **Decision:** the Python and native producers remove an evicted head
and append the new rows when that re-sends fewer items than index alignment.
Append-only and in-place list updates keep their existing patches, and the
operation vocabulary is unchanged. Reconstructing all 1,509 revisions of a
real 1,510-change Turn (the old diff reproduced every stored patch exactly)
reduced patch bytes from 7.92 MB to 3.03 MB (−61.8%) and its largest patch
from 1,431 to 47 operations. The evicted run starts at the first divergence,
not only at the head: the activity window retains errors and compaction
receipts, so once one of them is the oldest row every eviction lands behind it.
Head-only detection re-sent every later row there: one eviction in a full
128-row tool window cost 763 operations (60,399 B) instead of 3 (552 B), which
the run now restores; the scan stays linear. **Rejected:** a separate
ring-buffer operation would change every consumer; general minimal edit
scripts cost quadratic producer time on the hot path. Specs:
`tests/turn_lifecycle/test_turn_projection_patch_windows.py` and the native
`turn_projection_patch` tests.

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

**Problem:** the full window reclaimed the oldest info status/model row first,
and a settled `context_compaction` receipt is exactly that shape, so a long
tool loop erased the only transcript marker of an archived context while
keeping display-filtered tool rows (a sampled three-hour Turn folded 541 rows,
kept 96 hidden tool rows and dropped its single receipt). **Decision:** a full
window evicts the oldest row of the lowest rank in
`lib/turn_activity_timeline.py::_eviction_rank`: routine info status/model
beats, then the other display-filtered info rows, then rendered warnings, then
errors and failed/switched outcomes, and compaction receipts (with the
`compacting` beat they replace) last. The row/byte bounds and `droppedCount`
accounting are unchanged. **Rejected:** a separate receipt list outside the
window adds a second sidecar and fold path for one row kind; exempting receipts
from the byte budget makes the bound depend on the compaction count. Specs:
the rank, byte-budget and long tool-loop cases in
`tests/turn_lifecycle/test_turn_activity_timeline.py`.

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
Unicode serialization budget test is in `tests/tasks_pkg/test_plan_mode.py`; replay
retention/reclaim contracts are in `tests/storage_authority_sql/test_attempt_event_retention.py`.

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

Cursors are opaque and scoped to owner/conversation. Native EventSource uses
`Last-Event-ID`. Expiry, sequence/revision gaps, malformed frames, identity
mismatches and server restart require `sync.reset_required` and a snapshot.

**Problem:** a failed reset snapshot could reopen at a newer cursor while
retaining stale state indefinitely. **Decision:** the coordinator retains a
snapshot-required barrier until required projection ingestion succeeds; reset
hints never acknowledge cursors. A required TurnRuntime ingest rejects a full
snapshot older than an already accepted command revision before changing settings
or metadata. Delta acknowledgement covers rows, tombstones and patches; a reducer
rejection is not successful ingestion. A heartbeat proves liveness only at the
last applied cursor and server boot identity: mismatch keeps the projection
barrier and repairs through the same snapshot owner, never advances past missing
changes. Socket open and health notifications do not fabricate a valid-frame
timestamp. Hydrate, resume, wake and timed retry share one snapshot flight. Failures back off; pause/suspension retain the barrier
without background retries. History/rank reads require an accepted snapshot.
**Rejected:** direct SSE reopening skips state; per-page retries split authority.
Read cancellation, observer isolation, watchdog and resource regression rules
live in [Defensive patterns](../DEFENSIVE_PATTERNS.md#conversation-sync-ownership).

### Catch-up budget

**Problem:** a browser returning after a long absence reopened its retained
cursor and replayed every intermediate change, so it re-rendered thousands of
superseded tool rounds before reaching the current state. On a live
single-Turn agent conversation with 13,148 changes (a server build that still
sent diagnostic bodies), one browser snapshot was 3.21 MB in 0.40 s, while the
last 500 changes were 115.5 MB in 5.4 s and the last 2,000 were 429.3 MB in
24.4 s on loopback, before any proxy or browser work. A 1,510-change Turn
replayed 60.5 MB against a 0.65 MB snapshot.

**Decision:** the generated EventSource declares its snapshot `windowBytes`.
Changes committed before a connection's first page read are held until they
reach that read's head. If their encoded frames exceed the window first, the
stream sends `sync.reset_required` with reason `replay_exceeds_window` and the
coordinator re-anchors with one snapshot of the same window; otherwise the held
frames stream unchanged. A reconnect therefore transfers at most one window of
replay or one snapshot, and changes committed after the first read always
stream. The budget is the existing viewport window, not a new threshold.
`cursorSource=snapshot` exempts only the first connection after a snapshot:
its suffix is that snapshot's own round trip, which another snapshot would
merely restart, so the reset cannot loop. Native reconnects carry
`Last-Event-ID` and are budgeted; heartbeats carry their cursor as the event
id so an idle stream's reconnect does too. Clients that omit `windowBytes`
keep unbounded replay.

**Rejected:** a change-count or age cutoff is a tuning knob; snapshotting
every reconnect re-downloads the window on each transient proxy reconnect;
compacted replay would add a second Turn-state representation; a client-side
cutoff cannot see the server head and pays the backlog before deciding. Specs:
`tests/conversation/test_conversation_sync_catch_up.py` and the catch-up
budget cases in `frontend/src/core/conversation-sync.test.ts`.

Stream requests carry page-scoped `streamClientId` and monotonic
`streamGeneration`. Equal-generation native reconnects and newer explicit
recoveries supersede the same client's older subscription synchronously,
waking its wait and releasing its exact token. Older generations get HTTP 204
to stop native retry (`stream.closed` with status 204 on a multiplexed
response). Legacy clients remain readable without replacement rights.

### One page stream

**Problem:** an HTTP/1.x browser allows six connections per origin, and an
EventSource holds one for its whole life. With one stream per live
conversation (the active one plus each with a pending or running Turn), the
sixth left no connection for snapshot, history or command requests, so opening
a conversation queued its snapshot behind idle streams. The negotiated HTTP
version is outside the app: the same origin is reached over HTTP/2 through one
ingress and HTTP/1.1 through another, and the server itself speaks HTTP/1.1.

**Decision:** the browser carries every live conversation on one
`GET /api/v4/conversation-events` response, owned per page by
`conversation-event-multiplexer.ts`. `streams` is URL-encoded JSON; the server
also accepts it encoded once more, as a VS Code forwarded port delivers it
([query re-encoding](../PROXY_RUNTIME.md#query-re-encoding)). Each coordinator receives a source with
EventSource semantics and keeps its own cursor, catch-up budget, silence
deadline and recovery. Each listed stream has exactly the semantics of its own
`/events` connection; only framing differs: `data` is the stream's index in
`streams`, a line feed, then the event document, and `id` stays that stream's
cursor. A stream that cannot open, is superseded or fails sends
`stream.closed` with the status its own connection would have answered; it
alone ends and its source reports CLOSED, so the recovery barrier above
applies. A membership change replaces the response once per microtask; each
stream resumes after its newest received id and repeats `cursorSource=snapshot`
only while it has received nothing, as a native reconnect without
`Last-Event-ID` would. Each response takes the next page-wide generation, so it
supersedes its predecessor's subscriptions and a late older request is refused
as stale. A dropped response is replaced from those cursors with the shared
reconnect backoff, never the native retry, which would replay from the
bootstrap URL; a refused response (HTTP error status) ends every stream as its
own refusal would. Without members there is no response, timer or listener.

**Alternatives considered:** TLS on the app server for HTTP/2 cannot help
behind a proxy that forwards to its plain-HTTP port; a per-origin connection
budget makes the seventh conversation wait; a WebSocket adds a second transport
and lifecycle; a POST-created stream session adds server-side session state
that this request-scoped response avoids. The bound is the request line, about
150 URL-encoded bytes per stream: dozens of live conversations fit common
8 KB proxy limits. Specs: `tests/conversation/test_conversation_sync_multiplex.py`
and `frontend/src/core/conversation-event-multiplexer.test.ts`.

Conversation SSE has no per-principal count quota or shared capacity lease; new conversations
cannot evict other subscriptions. Exact-client generation replacement owns
cleanup, with bounded owner-generation tombstones carrying no projection data.
An unconsumed response has a 10-second body-start deadline. One rollback scope
covers every setup exit, including cancellation, until response construction
transfers release to the generator finalizer and that deadline.

**Problem:** applying the headless model-stream cap to these observers stranded
accepted tasks behind old startup labels once 24 subscriptions were open.
**Decision:** the existing broker alone owns conversation observation; no
stream-count budget applies to any SSE consumer. A native EventSource
that reports CLOSED (including HTTP 204) retires immediately and sets the
existing snapshot-required barrier. The shared retry timer repairs authority
before reopening, and wake/resume hints cannot bypass its backoff. Only a valid
stream frame resets retries; a successful snapshot alone does not prove stream
admission. **Alternatives rejected:** raising 24 to another fixed count merely
moves the failure; re-opening without a snapshot pins old UI under persistent
refusal; treating CLOSED as CONNECTING waits for a retry the browser will never
perform. Tests hold 64 independent observers above the old limit and require
zero subscriptions after close. Native-browser tests inject repeated HTTP 204
for both pending and tool-preparation states and verify terminal convergence.

Browser residency follows visible demand: the active shell keeps its stream;
an inactive shell keeps one only while its authoritative TurnState has pending
or running Turns. Selection/newChat explicitly re-evaluate previous/current IDs
because changing `activeConvId` emits no TurnState frame. Warm checks are O(1).
The resource test visits 32 settled conversations, requires one open source per
lease transfer and zero after disposal; see
`test_push_invalidations_do_not_reload_a_live_conversation_snapshot`.

Deletion atomically removes active header/turn/attempt/event/replay state and
moves a non-executable graph to recoverable trash. Browser disposal releases the
coordinator, store, stream, subscriptions and health entry. Restore/clone use a
fresh authoritative snapshot, never browser message arrays. The lifecycle
contract is `contracts/conversation_lifecycle_v1.yaml`.

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
`task_start_failed` envelope. A typed `model_route_unavailable` startup failure
retains the selected native model and a safe route-configuration diagnosis in
the same persisted and returned envelope; retry guidance requires checking
authorization, deployment enablement and capabilities or selecting another model.
If owner-scoped durable cancellation wins before task binding, the unstarted
registry task is discarded and the command returns the cancelled attempt and
turn normally. A rejected binding is classified as cancellation only after
reading that exact attempt's durable `interrupted` state; missing attempts and other
binding failures retain the startup-failure path.

Canonical local-executor commands persist `dispatch_mode=conversation_executor`
and the server-authored request-start cancellation watermark in the same
transaction as the pending attempt. A serving-loop owner queries the partial
`pending + empty taskId + dispatch mode` index in batches of eight after a
1.5-second grace period. Only a process role with the declared task-worker
capability (`worker` or personal-mode `all`) owns this loop; API and scheduler
replicas never claim executable attempts. The owner rehydrates the durable
principal, Turn, config, and original cancellation watermark, then enters the
normal claim/bind/start path.

The watermark travels only through the dispatch cancellation port, not executor
configuration. An owner-authorized config reread must strip it again while
retaining the durable principal and accepted execution policy.
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

`activeAttemptPhase` optionally restores the latest retained event's phase for
the current running main-lane attempt at the snapshot's read head. The read
adds at most one indexed event lookup and decodes at most 32 KiB; a missing,
cleared, phase-less or oversized latest event yields no phase, with no scan
of older history. The browser accepts it only on a full snapshot with matching
Turn/attempt identity, running status and a covered projection revision.
Terminal/superseded attempts and historical timing traces never restore a
live label. Unknown phase copy does not assert transport recovery.
A command ACK may install a projection before its same-revision attempt frame. Matching current, non-superseded attempts with an unseen sequence still consume explicit `phase` (including `null`) without replaying the installed patch; older revisions, foreign attempts and consumed sequences never roll back the label.
Only operations marked `x-tofu-idempotent-retry` retry automatically. The
generated client reuses the validated request document and command ID for
ambiguous network or declared retryable failures. Abort and non-idempotent
mutations do not enter that loop.

## Health

Visible conversation heartbeat frames own `conversation-sse` health.
Background task streams use the separate `task-sse` transport and cannot
overwrite a conversation coordinator's state.

The aggregate (`ConnectionHealthStore.aggregate`, shown as the topbar network
badge's "Reconnecting") counts only streams that lost their transport:
`offline`, or `degraded` that is not server-reported. `connecting` and
`recovering` are transient for a delivering stream. When the silence deadline
(`reconnectGraceMs` without a valid frame) expires, the coordinator marks the
stream stalled and publishes its reconnect states as `degraded` until a valid
frame arrives or demand is released (pause or suspension). A `degraded`
reading from a heartbeat that arrived (`SERVER_REPORTED_DEGRADATION`:
`storage-read-degraded`, `storage-write-wedged`) describes that conversation
and is presented on its Surface, never on the network badge.

Problem: the badge counted every live stream (the active conversation and
every background conversation with a running Turn), and its persistent
`degraded` readings were server reports on frames that had just arrived, so
one wedged task elsewhere held "Reconnecting" on a healthy network, while a
genuine stall was replaced by `recovering` in the same call and never showed.
Decision: classify by evidence; a received frame proves the transport, an
expired silence deadline disproves it until the next valid frame.
Alternatives considered: counting only the active conversation (hides a
stalled background stream and keeps the storage false positive for the
visible one); counting `recovering` (brief native flaps would poison the
badge); a new schema field for the stall (duplicates `state` and `reason`).
Pins: `connection-health.test.ts` and the "network badge aggregate" suite in
`conversation-sync.test.ts`.

Heartbeats and snapshots also carry `pushWithheld` (always explicit, both
ways). It is the READ-side probe of a WRITE-side delivery wedge: while the
conversation's live task has authoritative frames withheld on storage retries
(durable-before-visible, `TaskRuntime.append_event` stamps
`_pushWithheldAt`; event delivery also stamps it while a persist waits on a
live authority inside the storage client, per the
[client retry contract](../STORAGE.md#client-retry-contract)), the withheld
frames themselves can never report it, so
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

The change-page read inside the stream is replay-safe, so wedge-classified
storage failures (`database_timeout`, `database_busy`,
`database_unavailable`) degrade it to heartbeats with backoff instead of
tearing the stream down — the RPC layer marks those non-retryable only
because COMMAND commitment is ambiguous, which never applies to this query.
Before this rule (2026-09-18 incident) a wedged authority killed the ASGI
response with a 503, and every EventSource reconnect added a pre-body probe
plus a possible full snapshot refetch on top of the wedge. Genuinely
permanent failures (protocol, auth, not-found) still close the stream.
Because the stream owns this loop, its steady-state page read declines the
storage client's in-call wait for a live authority
(`storage_wait_owner(decline_storage_wait)`) and returns its transient failure
to the heartbeat path; the pre-body ownership probe keeps the ordinary wait.
Pin: `test_sync_events_stream_degrades_on_wedge_read_failures` and
`test_stream_change_read_declines_in_call_storage_waits`.

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

Executable contracts live primarily in `tests/conversation/test_conversation_sync_v3.py`,
`tests/frontend/test_frontend_attempt_stream_vite.py`,
`tests/turn_lifecycle/test_turn_command.py`, and
`tests/storage/test_storage_authority_sql_contract.py`.

## Conversation settings vocabulary

**Problem:** settings crossed the browser boundary through five hand-copied key
lists (browser snapshot builder, server resolver, retained adopter, catalog
projection, metadata cache), and they drifted both ways. The per-conversation
write-approval choice (`autoApply`) was persisted but never adopted, so a reload
painted Auto and the next settings write silently replaced Manual; catalog rows
cleared `paperDocument` because the adopter treated a partial projection as
complete; the cache dropped `modelRef`, `preferredProviderId`, `imageGenEnabled`
and `autoApply`; the catalog shipped keys no server writes (`lastMsg*`,
`activeTaskId`). **Decision:** `contracts/conversation_settings_v1.json`
declares every public key once: its author (composer, composer-derived,
resolver-derived, server, legacy), whether the browser adopts and caches it,
whether the catalog carries it, and which modules write server-authored keys.
`CATALOG_SETTING_KEYS` and the browser cache key set derive from it.
`_applySettingsToConv(conv, settings, projection)` lets only `full` snapshot
settings clear `clearWhenAbsent` keys; `partial` catalog rows and command
echoes never clear state they do not carry. Private keys remain owned by
`settings_policy.py` and may not enter the contract. **Rejected:** a generated
TypeScript type alone cannot check the retained JavaScript adopter or the
Python resolver; deriving the vocabulary from the resolver would hide
server-authored keys; keeping dead keys for compatibility keeps shipping state
that no producer maintains. Specs:
`tests/conversation/test_conversation_settings_contract.py` (resolver
vocabulary, catalog derivation, declared writers, private keys) and
`frontend/src/conversation/application/conversation-settings-contract.test.ts`
(snapshot keys, adopter reads, cache keys, reload round trip, partial/full
clearing).

## Catalog deletion evidence

Sidebar reconciliation evicts an absent persisted shell only when the current response supplies a nonnegative safe-integer total equal to its valid, unique row count. A missing or malformed total, duplicate/invalid row, or partial page cannot prove deletion; an earlier response’s total is never reused for this decision. Local drafts remain. Confirmed deletion disposes the Turn runtime and cached metadata, then selects a remaining conversation or opens a fresh chat. `tests/conversation_sync/test_sync_digest_ghost_eviction.py` exercises the shipped catalog/invalidation adapters; `tests/conversation/test_conversation_catalog_load_runtime.py` checks response-local completeness evidence.

Queue activation/cancellation are idempotent by durable re-execution, not by
retaining the first full Turn response in the bounded command-receipt store.
After cancellation, retry reports `cancelled: false`, empty `deletedTurnIds`
and no original `inputTurn`, without another revision increment. Clients
converge from the authoritative snapshot; a retry must not fabricate a deleted
input body. The Python and native storage operations share this contract.

Catalog loading owns at most three HTTP requests per load, including any
unconditional recovery after an unusable 304. Each request has a 12-second
budget covering headers and JSON-body reading, and at most one live deadline
timer. 503 backoff is capped at five seconds and never runs after the final
request. Disposal aborts the active fetch, clears the backoff/deadline timer,
releases single-flight waiters even if a port ignores cancellation, and blocks
late catalog/cache publication. The typed loader owns these timers; the
retained adapter forwards its signal without adding another timeout owner.
`tests/conversation/test_conversation_catalog_lifetime.py` fault-injects stalled requests,
stalled JSON, oversized retry headers, disposal and 304 recovery budgets.

## Authoring-round reasoning evidence

`TurnLastRoundUsage.reasoningResolution` is an optional bounded
`TurnReasoningResolution` compiled by the selected execution slot. It carries
requested mode/value, effective mode, source/reason, and the actual native
control mode/value. It contains no prompt, provider credentials, or model
catalog snapshot. The same `_reasoning_resolution` remains in retained authoring
API-round usage. Discarded billing calls never replace the authoring snapshot;
a later authoring call without evidence clears any old evidence. Existing Turn
projection persistence carries this field without a new storage lane.

The Finish Tag displays mappings from this evidence, not from current model
metadata. Legacy projections may omit it; absence does not authorize an actual
effort claim. See [rendering contract](../RENDER_CONTRACT.md#finish-tag-reasoning-mapping).

## Conversation-owned automatic workspaces

**Problem:** Studio/project setup and navigation restoration made filesystem
capability depend on a mutable UI environment. **Decision:**
`lib/conversation_sync/workspace.py` resolves explicit chat intent before Turn
acceptance and retry acceptance; existing Attempt config is the sole durable
execution snapshot. Empty chat bindings get owner/conversation-scoped private
roots under the writable runtime data root. Explicit scalar/list-only selections,
read-only references and remote bindings remain explicit; no recent-project,
server-launch-directory or other-conversation inference is allowed. Reserved
workspace/origin fields in the execution-identity contract are server-authored.
SDK/embed/headless execution does not enter this application policy.

Physical worker admission creates a private root idempotently with no-follow
path checks. Managed roots never adopt a containing server Git repository;
commands fence upward Git discovery. Non-Git as well as Git tasks own frozen
root policies, independently of cache eviction, navigation and draft state.
Child/background execution borrows that policy, not publication ownership.
Private roots are durable user outputs: normal terminal cleanup must not delete
them. They are not persisted as user-selected conversation attachments. Creation
is lazy at worker admission, with no new idle worker, scan or workspace store.

**Alternatives rejected:** a second binding database duplicates Attempt authority;
unconditional kernel scratch allocation changes SDK contracts; guessing the last
active project risks foreign writes. Filesystem isolation remains cooperative,
not an opaque-program sandbox. Narrow tests cover accepted/replayed bindings,
SDK pass-through, owner/conversation separation, symlink refusal and cache loss:
`test_automatic_workspace.py`, `test_conversation_commands_v3.py` and the
workspace browser journey.

Resource/lifecycle evidence (2026-10-10, synthetic Linux temporary filesystem):
2,500 pure resolutions measured median **11.95 µs**, p95 **12.75 µs**; 100
worker admissions measured median **95.91 µs** first creation and **84.87 µs**
reuse. These are policy/local-filesystem measurements, not FUSE or production
latency guarantees. Resolution performs no filesystem I/O; provisioning adds
one leaf directory per used owner/conversation, no files, idle workers or
recursive scans. Output files are durable user state, not cache: no TTL or
terminal cleanup reclaims them. Existing task teardown releases root-policy
projections; the accepted Attempt retains the binding needed for recovery.


## Exact input-round directory and rank navigation

`GET /api/v4/conversations/{conversationId}/turns/rounds` requires an explicit
`laneId` and `syncSeq`. Owner comes only from authenticated scope. The response
carries `ownerId`, `conversationId`, `laneId`, `syncSeq`, exact `totalRounds`,
zero-based `offset`, and up to 100 compact entries (`turnId`, `ordinal`, `actor`,
1-based `roundNumber`). `anchorTurnId` optionally resolves the preceding/current
input rank for viewport selection. `nextOffset` pages the directory; rank N is
read with `offset=N-1&limit=1`. `syncSeq` names the reader's applied head; the
response's `syncSeq` names the head its ranks are exact for, never an older
one. The storage operation itself answers only at the exact head; the
application service repeats a stale read at the newer head storage reported,
at most four reads. Only a head behind the reader, or one that keeps moving
through those reads, returns `stale: true` and no entries; that zero total is
not authoritative and must never be displayed as a count.

Problem: every streamed attempt event advances the sync head while input ranks
stay unchanged, and the browser's replayed head trails the server by at least
the stream latency. An exact-head fence therefore failed most rank reads during
a streaming reply, and the jump panel showed "history failed" after a click or
two. Decision: answer at the newest head not older than the reader's. The
browser keeps such a page unless a Turn change or snapshot was applied to its
replica during the read; a Turn change the page already includes reaches the
replica through the ordered stream and re-reads the view through its key. A
replacement snapshot in flight is awaited instead of failing the read.
Alternatives rejected: retrying in the browser at its own head (every attempt
loses the same race by a round trip plus stream latency); a per-lane rank
revision in storage (a schema change on both authorities for a read that is
already exact at any head it names).

Inputs are exactly `human`, `critic`, and `virtual_user` in lane ordinal order.
Assistant-only messages, arbitrary assistant/input ratios, and ordinal gaps do
not create input rounds. SQL uses actor/ordinal columns; native scans existing
compact lane heads in bounded pages, retaining only the requested entries.
Neither implementation reads projections to obtain a total. There is no new
persistent counter or write-path-dependent count maintenance.

The route returns the storage DTO through the shared `{ok: true}` envelope, so
`ok` is a declared `ConversationRoundDirectory` field. Generated browser and
Python validators reject undeclared fields, and a body the server accepts but
the browser schema does not turns every successful read into a contract error;
the envelope is therefore part of the schema authority, not a transport detail
the client may ignore.

The browser directory is instance/owner/lane scoped and read against the one
coordinator's committed head. Its key (`roundRevision`) changes with snapshot
resets and every applied Turn change: upserts, deletions, projection patches
(which may delete Turns) and attempt events that announce related Turns.
Streamed attempt progress, which patches only the live Turn, does not change
it. Rank targets are revalidated under the current head before jumping. A streaming
attempt advances that head on every committed turn, which is why a directory
view keeps its last good page and refreshes the shown window in place instead of
discarding the projection: only a conversation switch drops it. One read is in
flight per view and a newer request replaces the queued one. A page fenced by a
head that moved under it is re-read against the head observed now, bounded,
because a racing head converges while a broken projection does not.

A jump is never dropped silently. Background reads (scroll anchoring, key
refreshes) hold the view's slot without disabling the jump controls; a jump
requested meanwhile waits for that slot, the latest request winning. If the key
changes while a jump resolves or hydrates, the jump re-resolves its round under
the new ranks (bounded) and then lands; only a conversation switch abandons
it. A failed jump stays reported until the reader acts again, so the refresh a
key change starts cannot erase it. Bodies use the same coordinator/TurnStore
path: one detached page holding the target and the seven Turns after it, plus a
one-Turn fallback when a byte budget drops the target. Detached ingestion never advances the
continuous tail history cursor, and never counts historical hydration as a new
Turn. Only a page whose `beforeOrdinal` equals the continuous frontier may
advance that frontier, so a loaded island cannot skip unseen history.

### Native legacy compact-head upgrade

New writes store immutable `actor` in the existing compaction head, through its
shared head writer. A legacy head lacking only `actor` resolves that field from
the canonical owner-scoped Turn document in the same read snapshot. Navigation
never hydrates external projection payloads, writes metadata, advances sync
heads, or infers actors from alternating positions. Its existing deadline and
per-document bounds remain in force; only legacy heads incur document reads.
Identity (conversation, Turn, lane and ordinal) and every remaining compact
field must match the source exactly. Missing sources, malformed actor fields
and mismatched revisions still fail closed.

Problem: requiring an offline backfill before navigation made valid existing
conversations return HTTP 500 while their authority was live. Decision: a
read-only, exact legacy adapter at the existing directory owner. Alternatives:
blindly estimating actors corrupts round ranks; live backfill inside a query
violates read authority; full projection hydration copies unnecessary bodies.
The optional exclusive offline `migration-session` operation
`migration.turn.round_heads.upgrade` remains available separately per tenant/owner
to remove the legacy-read cost. Start with `{"cursor": null, "limit": 16}`,
continue with the returned cursor until null (maximum page 32). It is idempotent
and preserves Turn/projection revisions, timestamps and conversation sync heads.
SQL stores already expose the actor column. Tests live in
`packages/tofu-db/src/turn_byte_window_tests.rs` and run as `byte_window_tests`.

Turn image and media attachment arrays have no private count admission ceiling.
Generated validators derive this from the canonical schema. Owner-scoped media
resolution processes the complete list, deduplicates identities and replaces
client metadata with canonical refs before retention; each item's existing
shape and ownership checks remain. See [attachment lists](../modules/ingest_media.md#complete-turn-attachment-lists).

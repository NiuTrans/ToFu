# Reusable team agents

## Historical context versus delivery authority

**Problem:** fresh roots (including timer turns) restore completed model views
with old swarm receipt metadata but no predecessor mailbox authority.
**Decision:** validated completed-turn replay and accepted root archive restore
strip prior swarm receipt IDs/mailbox keys from detached input, retaining exact
nonhuman text, role, and other provenance. Durable history and pending originals
are unchanged: replay neither confirms delivery nor creates/rebinds a mailbox.
Archive recovery with explicit current authority retains all witnesses for normal
validation; foreign/malformed ones still fail closed. Same-run child checkpoints
retain and revalidate both confirmed and pending carriers before dispatch.
Canonical and physical validation share request-scoped, nonretryable
`MessageInputScopeError` (the existing request-rejected envelope); cancellation keeps
its type and no provider fallback can repair missing/revoked/foreign authority.
**Rejected alternatives:** metadata-derived bindings forge authority; ignoring
unbound live carriers hides pending delivery; deleting text loses context;
acknowledging historical text consumes another execution's input. Owners:
`model_replay.restore_prefix`, `compaction._resume_projection`, and
`message_input_receipts`; executable specs are `test_child_message_checkpoint_fence`,
`test_message_input_receipts`, `test_model_replay_stability`, and
`test_dispatch_context_recovery`.

This contract owns durable identities for Swarm children. The task engine
remains the only model/tool execution path; `lib/swarm/retained.py` owns
assignment transitions and `retained_repository.py` owns persistent records.

## Same-run team communication

**Problem:** a worker inherited the root's combined stable/exact-run message
schema, filled both targets, and received a permission error before argument
correction. **Decision:** derive worker wire and execution/discovery schemas
from `teamMessaging.toolViews` in `contracts/swarm_messages_v1.json`, exposing
only exact-run fields and requiring both handles. Parent catalogs and inherited
effect/approval policy stay unchanged. Validate mixed targets before checking
stable activation permission, reject without effects, and explain how to remove
the incompatible fields. A rejected message has no new receipt; an uncertain
submission still requires its original receipt. Optional progress reporting
must not block assigned work; final results already use ordinary delivery.
**Alternatives rejected:** silently ignoring a target risks misdelivery;
granting retained activation broadens worker authority; more prose on a combined
schema leaves impossible actions exposed. Regressions cover actual dispatch,
mailbox effects, provider projections, and parent catalog immutability.

`send_agent_message` and `get_agent_messages` also serve workers. They use only
the exact-run target form and their server-stamped original team identity.
Role filtering includes these tools only when present in the parent catalog;
explicit per-worker allowlists and read-only policies still narrow authority.
Before each new send, the live session must contain that exact child invocation.
Cross-run/owner/tenant sends, superseded children and stable-agent activation fail
closed. Each worker namespaces its idempotency keys by assignment identity.
Workers can read their own or teammates' bounded message receipts in that run;
this does not expose their private working dialogue.

The original running root is addressed with `agent_id == swarm_run_id`. Its
ordinary complete-round preparation and physical-input observer consume/confirm
the same mailbox carriers as workers. A team finishing before root consumption
is allowed while that exact root task remains running; Stop, replacement and
scope changes fence it. No message wakes the root or starts a new turn. Lost
input confirmation remains unknown under the existing receipt contract. The
parent supplies teammate handles in assignments or follow-up messages.

Problem: the leaf-worker denylist prevented collaboration as well as control.
Decision: admit context exchange through the existing message service while
retaining the ban on spawning, stable activation, human questions and cancellation.
Alternative rejected: removing the whole denylist would grant worker control;
a separate peer bus would duplicate durable receipts and input admission.

## Run-level message recovery

**Lazy inboxes:** mailbox absence previously masqueraded as run absence. Reads now prove the original owned recipient via live session or durable specs, revalidate scope, and return `messages=[]` (or `message_not_found` for a specified absent ID). Missing run/recipient authority remains `message_run_not_found`; storage failures never mean empty success. Eager writes, universal empty success and latest-run redirects are rejected. Guard: `test_empty_message_inbox.py`.

**Role semantics:** calling every run parent “Coordinator” conflated routing with the separately bound [project role](PROJECT_COORDINATION.md). The panel and snapshots use “Main agent / 主 agent”, including sender aliases, filters and legacy root labels. `coordinator_binding` remains the internal parent-mailbox name, not project appointment evidence. The panel shows topology even for project-bound roots. Live project lookups for historical rows and mailbox-ID migrations were rejected: they add reads or alter delivery authority for a display correction. Guards: `test_run_snapshot_recovers_parent_without_project_coordinator_role` and `swarm-messages.test.ts`.
**Problem:** child-only receipt snapshots omit the parent inbox and lose
sender identity on reload. **Decision:** the existing `_swarmSnapshot` adds
`messageReceipts` for every exact-run recipient (including `agentId == swarm_id`),
`messageReceiptsAvailable`, `messageReceiptsVersions` keyed by recipient, and
`messageParticipants: [{id, taskId?, label}]`. Per-child receipts remain compatible.
These fields survive spawn-wave filtering: messages are run-level, child cards
are wave-level. Mailbox revisions are independent of child completion versions;
stale/failed reads preserve previously committed evidence but incomplete reads
set availability false. No mailbox read failure claims an empty successful run.

Reads reuse `RunMessageRepository` binding equality and `validate_target` scope validation. The parent is not a synthetic child row; child settlement never turns parent `submitted` into `unconfirmed`.
Child `senderTaskId` is the random invocation task ID created by the shared child
factory, not the assignment ID. Join only through an exact owned
`master._agents[assignmentId]._child` binding or a mailbox `inputTaskId` witness.
Unknown identities stay unresolved. A resumed assignment can have several task
witnesses; persisted old mappings are retained only while referenced by receipts.

The existing 64-message / 4096-character / 16384-byte per-mailbox bounds apply.
The run list adds one projection copy of child receipts plus one parent
mailbox per spawn snapshot; maximal UTF-8 receipt bodies remain under 1.1 MB per
mailbox (tested), with no new storage authority, worker, poller or runtime.
**Alternatives rejected:** inferring task-ID prefixes misattributes messages;
inferring root termination from completed children loses valid pending input;
a separate history store duplicates mailbox authority. Specs:
`tests/swarm/test_run_messages.py` and neighboring snapshot/team tests.

The SA dock mounts `conversation/ui/swarm-messages.ts` per exact run above agent activity.
`swarm-messages-projection.ts` derives snapshots/live overlays; parent receipts need no child row.
Identity updates in place with sender/recipient, filters, full plain text and timestamp/ID evidence.
History expands from three rows by twenty, retaining focused/open rows; missing history differs from verified empty, and task completion is not input confirmation.
Tool records remain execution evidence. Guards: `tests/visual/test_visual_agent_sidebar_dock.py` and receipt/projection/component tests.

## Decision

**Problem.** An ordinary Swarm child has one objective and one terminal result.
Its in-flight checkpoint survives a restart, but a later assignment creates a
fresh worker and loses the child's explored context. Copying every child tool
result into the root conversation also grows the root's prompt.

**Decision.** `manage_agents` adds a durable identity scoped to an explicit
owner and either the current project (default when attached) or conversation.
`create` stores an idle identity; `promote` copies a settled child checkpoint
from the current owned Swarm session. `assign` CAS-reserves the identity for one
new ordinary Swarm child, appends the new objective after the saved dialogue,
and uses the current task's model/tool/policy/budget authority. The child commits
its final working messages and result before releasing the identity to idle.
No worker, model context, or thread remains resident while idle. A second
assignment to a busy identity is rejected; distinct identities run in parallel.
The existing `spawn_agents` path remains the temporary-worker default.


`send_agent_message` has two explicit target forms:

- **Persistent colleague:** `retained_agent_id`, optional `scope`, authored
  `content` and `idempotency_key`. An idle colleague starts a fresh ordinary
  child with its saved dialogue; a busy colleague receives input pinned to its
  current owned assignment. Project-scoped callers may message that assignment
  from another owned conversation in the same project, after both scopes are
  revalidated. A cancelled colleague requires `intent=resume`. Interrupted,
  suspended or ambiguous busy work requires the existing Continue/reset controls,
  never replacement by inference.
- **Exact-run steering:** `agent_id` plus `swarm_run_id`. This only adds bounded
  `reference`/`steer` input to that assignment. It never starts a new turn, maps
  an expired handle to a colleague, or transfers messages to a successor.

`get_agent_messages` reads the original stable identity/key or exact run/message
receipt without activating work. `assign` remains explicit idle-only assignment;
`create`/`promote` remain the retention choice. Ordinary `spawn_agents` children
are not automatically retained.

**Problem:** combining both target forms rejects messages to temporary workers;
an unconditional stable-only `intent` default also invalidates exact-run calls.
**Decision:** tool schemas explain both exclusive target forms with examples;
stable messaging owns its `intent=message` default. Runtime validation rejects
mixed targets, and argument rejection tells callers which
fields to remove before retrying. Temporary children use only their original
`agent_id` and `swarm_run_id`; stable identities come from the retained catalogue.
**Alternatives rejected:** silently dropping target fields could start or steer
the wrong work; receipt lookup cannot repair a call rejected before submission.


Sending has separate presentation surfaces: the browser shows the authored
message and target, distinguishing new-turn acceptance, submission, saved-but-
unconfirmed input, definite rejection and uncertainty. Legacy exact-run model
receipts remain concise; stable receipts preserve the actionable assignment
handle so the caller can poll or inspect the original turn. `contracts/tool_result_surfaces_v1.json` owns that
projection. Full canonical receipts and `get_agent_messages` remain available
for diagnostic/reconciliation reads. Failures, uncertain consumption and
recovery guidance are never promoted to successful delivery or completion.
The machine-readable authority is `contracts/swarm_messages_v1.json`; service,
portable record-CAS repository, and input carrier live in
`lib/swarm/{retained_messages,message_service,run_messages}.py`. Stable routing
and a single-use activation claim commit in the retained identity before the
ordinary spawn authority is called. Retrying a lost launch reply consults an
exact owned child checkpoint or saved terminal assignment; absent evidence stays
unknown, never a permission to launch again. The request, pinned assignment and
outcome survive completion and reset generations. A reused key with changed
content, kind or intent conflicts. Reset fences old memory but does not erase
message deduplication. Explicit identity deletion removes that identity's ledger.
An activation crash before worker admission may remain unknown and busy; it
requires explicit reconciliation/reset rather than unsafe replay.

Exact-run sending commits before input acquisition and starts no thread,
worker, model call or polling loop. A duplicate exact message is evidence lookup
even after settlement. When submission wins the terminal race, the saved original
is returned as unconfirmed rather than falsely reported as not submitted. Each
run admits at most 64 original messages (4096 characters/16384 UTF-8 bytes
each); excess is rejected, never truncated. One round adds at most eight.
An empty tombstone fences the first-send/settlement race. Idle executor
residency is unchanged. Worst-case body content per mailbox is 1 MiB before
JSON/storage framing; durable evidence is not automatically deleted.

Consumption revalidates owned conversation/project and retained assignment
run, uses the registered nonhuman inbox carrier only after complete tool
pairs, and stays inside the existing prompt/budget admission boundary.

`lib/tasks_pkg/message_input_receipts.py` checks logical input admission and
composes a physical-attempt observer with the existing budget lease. Sealed
translated input and current scope are rechecked before provider ingress after
waits and on floor resends. This is not universal downstream serialized-wire
proof; see [integration limits](PROJECT_COORDINATION.md#input-receipts-and-recovery-limits).
Dropped/summarized carriers cannot yield input confirmation. Whole-message
selection scans the bounded mailbox for up to eight fitting whole originals,
observing context headroom, a 4096-token and 24576 escaped-byte ceiling. Unfit
originals stay pending without blocking later messages; existing carriers keep
ACK eligibility when fresh input cannot fit. Durable session/run and
terminal child checkpoints are also validated before dispatch and sending;
a late sender rechecks after CAS, while duplicate completion retries the
terminal mailbox fence. Storage unavailability cannot become execution proof.

Restored checkpoint witnesses are captured before optional acquisition, so its
failure cannot silently bypass matched-carrier authority checks. Child bootstrap
removes stale/cross-assignment mailbox carriers from the copied checkpoint;
confirmed same-run carriers remain context and are revalidated before dispatch.
Conversation-mode children retain inherited context text but not parent receipt
authority. Durable originals and other dialogue remain intact. Unknown authority
fails closed. This is not a universal transport proof.
Provider failure or cancellation does not acknowledge input. A verified
provider response permits `input_confirmed`; this does not prove acceptance,
compliance, task completion or delivery. An ACK storage failure stays unknown.

There is no persisted successful-request pending-ACK outbox. A process crash
between provider completion and receipt commit can leave consumption unknown;
terminal projection must not claim the provider never saw it. Receipt recovery
cannot be obtained by automatically purchasing another model request.

**Receipt projection problem/decision.** A static send result stayed submitted
while its durable mailbox advanced. Committed mailbox changes now emit
`swarm_message_receipt` through owned task replay and conversation Swarm push;
`inputInjectedAt`/`inputInjectedRound` record first complete logical-context insertion,
not consumption; `inputRound` records the later verified confirmation round.
Only the verified provider-return path sets `input_confirmed`. Failed writes emit
nothing; an uncertain ACK is reread before publication, and observer failure never
undoes committed evidence. Terminal close publishes `unconfirmed` without erasing
insertion evidence. Exact authored body text and kind are included only in these
owner-scoped presentation paths, never unrelated telemetry or logs, with no
additional execution authority. `messageSequence` derives the one-based append
position from the committed mailbox (including older rows); it is not a global
clock or provider-consumption evidence.
The existing spawn snapshot carries each agent's `messageReceipts` plus mailbox
`messageReceiptsVersion`; a stale snapshot or failed mailbox read cannot downgrade
newer projected evidence. One snapshot refresh occurs per mailbox commit, including
batch insertion/close, with no new timer, poller or resident worker. Snapshot rows
remain bounded by the existing 64-message mailbox limit; the executable fixture
projects all 64 maximum-length four-byte Unicode bodies plus metadata below
1,100,000 UTF-8 JSON bytes (1 MiB of body text). Each body retains the existing
4096-character / 16384-byte bound. No truncation or whitespace rewriting occurs.
Snapshot reconstruction adds one mailbox read per roster agent, including empty
mailboxes; no per-message read is performed. A full 64-message close has one
snapshot refresh, not 64 refreshes; duplicate close has none.
Alternatives rejected: acquisition is not consumption; another poller/cache
would duplicate lifecycle owners. Receipt body text reuses the same bounded
projection, not a second fetch loop. Guards: `test_run_messages.py` and
`test_swarm_snapshot.py` under `tests/swarm/`.

At terminal settlement, a successful durable mailbox close changes remaining
submitted records to `unconfirmed`. If close fails, read-only lookup projects
`unconfirmed` from exact terminal session/run evidence without rewriting records.
Neither case proves that the provider never received them. Old runs are not woken and
messages never transfer to replacement assignments. Stop uses existing cancel
control, not natural language. Children may exchange exact-run teammate context
and inspect bounded receipts, not private dialogue; messages grant no permission. The sender is a
write tool; receipt lookup stays read-only. Coordinator binding advertises
backend support, not execution authority. Passive project conversation messages
and explicit consideration use the separate scoped mailbox owner documented in
`modules/conversations_project_brain.md`; automatic assignment remains unavailable.

Executable specifications: `tests/swarm/test_run_messages.py`,
`test_run_messages_pipeline.py` (ordinary execution pipeline, hermetic model),
and `test_run_messages_storage.py` (disposable real Sidecar authority).
Stable routing/activation, Stop, scope, retry and contention specifications live
in `test_retained_messages.py`; fresh per-assignment execution authority lives
in `test_retained_assignment_authority.py`.

The repository carries owner, tenant and hashed project/conversation scope in
every key and validates the stored identity on read. A reset increments the
generation and clears working memory; `abandon=true` fences a busy run's late
memory commit. Archive blocks assignment; delete removes only an idle identity.
Full child evidence follows the ordinary Swarm/Inspector retention policy.
Read-only `list_agents(agent_id=...)` returns a bounded recent memory view to
the root without entering the write-approval partition. Current permissions
are checked on each new invocation; saved messages are history, never a grant.
Both activation paths use `retained_launch.py`: unknown launch ACKs retain
busy ownership and original lookup IDs; only proven pre-admission rejection
releases. Exact server-bound terminal evidence can settle a lost callback;
lookup/predicted handles alone cannot. See [shared lifecycle](AGENT_LIFECYCLE.md).

Fresh retained assignments freeze the current caller's model, selected tools, principal, workspace and config before scheduler admission. **Capacity problem/decision:** a private 1 MiB JSON ceiling rejected valid catalogs/prompts before launch receipts. Freeze complete v2 grants with compact Unicode JSON and no local byte ceiling; tools have one canonical schema carrier, and recovery reconstructs the runtime catalog after validation. Strict JSON, detached copies and authority checks remain.
Follow-up work shares session coordination identity, not its earlier execution grant. A wave prepares one template per effective model before binding any assignment; only immutable strings are shared across sibling grants. First ordinary waves validate through the same boundary and retain one session grant, without duplicate per-child persisted grants. Runtime budget/cancellation/usage ports are not serialized.
Restart reconstructs only known server-authored inputs; missing legacy retained authority fails closed. Session read-only restrictions still narrow each assignment. Storage/invocation retain their capacity and lifecycle owners. **Alternatives rejected:** raising a fixed threshold moves the failure; trimming schemas, contracts or prompts changes authority or loses input. Recovery accepts v1 only with matching duplicated catalogs; v2 rejects a second config catalog, missing assignment IDs and unknown versions. No global grant cache or new storage authority is introduced.
A repeated objective is a fresh retained turn, not ordinary one-off objective deduplication.
**Problem:** copying the entire live config during workspace snapshotting can fail on process-private MCP search mappings or locks before a spawn receipt; plain exception strings hide retry/submission state and generic ValueError catches mislabel other startup failures as dependency cycles.
**Decision:** all waves scope metadata to their selected catalog and reuse `inherit_child_config` before copying workspace authority. Preparation/cycle failures return typed `rejected` results before submission, preserving existing work. Only `AgentDependencyCycleError` denotes a cycle; later start failures return `swarm_launch_unknown`, original lookup handles and `requested_agent_ids`, never a fabricated launch roster. Generic tool failures also use the shared typed error boundary. Unconfirmed work must be inspected before another spawn.
**Alternative rejected:** serializing arbitrary runtime objects would retain state outside declared child policy.
Guards: `test_launch_preparation.py`, `test_spawn_tool_authority.py` and `test_retained_assignment_authority.py` cover launch receipts, detached workspace grants and the real child model/tool loop. `test_assignment_context_capacity.py` adds large ordinary/retained grants, first/follow-up launch receipts, complete recovery and denied corruption; allocation/encoding measurements live in [RESOURCE_CONTROL.md](RESOURCE_CONTROL.md#assignment-authority-size).

For retained Browser agents, the child supplies its stable identity as a
working-tab cache namespace. Owner/device routing and access checks remain the
browser authority. Different Browser agents on one device do not share the
implicit current tab; every new assignment verifies the live tab and URL
before acting on prior page observations.

The repository retains complete identity state and dispatch evidence without a
private 8 MiB byte quota, fixed outcome reservation or roster-count ceiling.
Roster queries join authority headers with cached metadata by exact version,
repairing only missing/stale identities; deleted authority cannot reappear from
old cache rows. Matching rows never hydrate dialogue. Ordered 1,000-row pages
are physical windows, not identity quotas. Idle identities hold no executor.

Terminal settlement preserves every supplied protocol message alongside immutable
dispatch/dedup evidence. The current task engine owns model-context selection;
durable memory capacity cannot reject a new claimed dispatch. Prior persisted
`working_history_omitted` markers remain historical evidence: retiring trimming
does not recreate already omitted messages. Reset still clears working memory,
not dispatch evidence, and an ambiguous storage ACK still checks the exact value
and version before any new write. Busy-identity and assignment authority fences
remain unchanged.

**Admission problem/decision.** An 8 MiB shared state ceiling and fixed 32 KiB
reservations made valid assignments fail before execution, while trimming a
working transcript traded context for bookkeeping room. Retire these private
quotas and the 256-identity roster ceiling; preserve complete owner-scoped CAS
state with the existing storage/receipt lifecycle. Raising ceilings retains
future refusal; evicting dispatch keys would make an old idempotency key execute
again; a live thread per idle identity adds unrelated residency.

Measurement persists a complete 9 MiB dialogue plus its terminal follow-up,
reconstructs the repository and replays the same dispatch without another launch.
A real 1,101-identity roster crosses the physical page boundary without loss;
foreign-owner prefixes return no identities. Storage grows with accepted identity,
transcript and dispatch evidence and remains tied to explicit identity deletion.
These are admitted byte/count measurements, not RSS or throughput claims. Guards:
`tests/swarm/test_retained_agents.py`, `test_retained_message_capacity.py`,
`test_retained_messages_storage.py` and reconciliation/identity tests.

**Alternatives considered.** Keeping a live thread per colleague would tax idle
capacity and complicate restart. Reusing a completed Swarm session would mix
run results, delivery receipts and budgets across assignments. Copying a child
transcript to the root would defeat context isolation. The durable identity
plus ordinary per-assignment child preserves existing execution and evidence
owners.

## Detached Swarm restart recovery

**Problem:** a spawning Turn can finish naturally while its child agents still
run; requiring an interrupted parent made those durable children unreachable
after server restart. **Decision:** a running session may restore checkpoints
when its owner-scoped original attempt and current main-lane Turn both remain
completed with `provider_finished`. Same-Turn replacement, a later Stop, missing
or unavailable authority, and terminated sessions deny new execution. Recovery
rechecks authority after catalog reconstruction, registry publication and at
worker admission. Interrupted parents still require accepted Continue lineage;
manual parent Stop recovery for ordinary children is defined in
[run continuity](RUN_CONTINUITY.md#swarm-continue-after-manual-parent-stop), not
cold-start permission. Already-cancelled retained assignments keep their terminal
CAS and require the existing retained `intent=resume` path. Ambiguous in-flight
tools retain the existing no-replay failure. **Alternative:**
allowing every running session to restart would resurrect cancelled work.
Verification: `tests/swarm/test_swarm_detached_restart.py` and the restart,
activation-fence, cancellation and dependency suites.

**Problem:** recovery's `list_turns(limit=1, light=True)` still fetched the full
transcript before Python sliced it, so a large conversation could fail an
already-accepted Continue before model dispatch. **Decision:** recovery reads
the newest main-lane page at an authority-owned sync head, then pages backward
only when checking intervening Stop states. Pages shrink on byte exhaustion;
only status/identity headers are retained, and a changed head restarts the
consistent read. Continue needs at most one Turn; detached recovery reads every
intervening Turn through the spawning ordinal, without a total history quota,
and preserves cancellation/lineage fences. **Alternatives:** retrying
the full transcript repeats deterministic exhaustion; ignoring authority failures
can resurrect cancelled work. Specs: `test_swarm_recovery_turn_window.py` plus
the recovery suites above.
Measurement: a synthetic 5,000-Turn Continue check uses two one-Turn requests
(head negotiation plus the page), independent of transcript length. A reduced
page size is retained for the rest of that scan rather than reprobed each time.

## Recovered child outcomes

**Problem.** A streamed log survives eviction or restart even when execution
never succeeded. Its presence, newest mtime, or last assistant prose cannot
establish success or authorize cross-conversation access.

**Decision.** Recovery reads the current owner's conversation-scoped durable
checkpoint (or the exact current task's log). Cross-turn log lookup uses only
the spawning task recorded by that authorized checkpoint, never a global glob.
Only an explicit completed checkpoint without conflicting status or error
permits `final_answer` / `result`; failed, cancelled, interrupted and unknown
outcomes retain usable text as `partial_output`. A missing live worker does not
prove completion or continued execution. Unknown outcomes remain separate from
terminal results. Restart restores terminal outcomes without defaulting missing
result status to success; unknown checkpoint states do not restart workers.
Unreadable or unauthorized evidence is not disclosed as a successful result.

Model-facing lifecycle advice uses `poll` with explicit returned agent handles
and `wait_s` bounded to 60 seconds. No-ID `poll()` is a snapshot, not a wait for
all agents. Legacy `await_agents` dispatch and the internal orchestrator method
remain compatibility implementations, not instructions for new calls.

**Alternatives considered.** Treating any transcript as a final answer invents
success and can expose unrelated runs. Discarding non-success output loses
useful evidence. Scoped checkpoint authority plus separate partial evidence
preserves both truthfulness and recovery value. Regression coverage lives in
`tests/swarm/test_swarm_result_recovery.py`.


## Delivery dependencies

**Problem.** A parent can finish its provider turn while required children still
run or retain unread outcomes; a whole-batch join also waits for optional work.
**Decision.** Each `SubTaskSpec` persists `required_for_completion` (boolean,
default true, including legacy checkpoints). `spawn_agents` may explicitly set
false for optional exploration. The canonical status projection and launch
receipt retain this flag. Natural parent termination stamps existing `waitingOn`
for required running or unread children, distinguishing `running`,
`awaiting_integration`, and unknown `unavailable` evidence. This is a dependency
observation, not an objective-success verdict or a second lifecycle store.

The existing completion callback wakes continuation after required receipts
settle, even when optional children remain. Inbox publication precedes wake;
parent handoff rechecks the same condition after durable DONE to close the
last-drain race. Existing source-identity deduplication, cancellation, owner,
budget and unattended-chain gates remain authoritative. Duplicate child callbacks
do not republish evidence. Consuming a child result proves delivery to parent
context, not synthesis quality or user-goal completion. A provider-completed Turn
stays completed; its `waitingOn` states unresolved delivery dependencies.

**Alternatives considered.** Forcing another model round while children run
spends tokens without new evidence; rewriting provider completion invents stream
failure; waiting for every optional child delays useful delivery. No worker,
polling loop, parallel task authority or new durable result store is added.
Dependency metadata is bounded by existing session admission; `waitingOn` carries
at most eight identities plus the total count. Verification lives in
`tests/swarm/test_swarm_turn_handoff.py`, failure-continuation, identity, and
restart neighbors.

Recovery count removal — Problem: 2,000 ordinary later Turns disabled an
accepted detached assignment even when its original authority remained valid.
Decision: page complete owner-scoped status/identity suffixes with a 64-row
physical window; caller-requested one-Turn checks stay narrow. Alternatives:
raising the total cap repeats the failure, and inspecting only the newest Turn
can miss an intervening Stop. The 5,001-Turn synthetic scan checks every ordinal
and retains no tool transcript; the long-history authority test accepts ordinary
completion and denies an early Stop. Consistent-head retries and malformed-page
errors continue to deny ambiguous execution.

Automatic continuation history — Problem: an oldest-first 2,000-Turn read
rejected settled child handoff in long conversations and hydrated entire tool
transcripts. Decision: use the same consistent-head backward pager until the
latest actual human input; retain only actor, pairing, receipt, error and derived
external-blocker evidence. Each page releases transcripts before the next read.
Stop, duplicate command, existing failure-recovery authorization and tail-CAS
checks remain mandatory. Alternatives: a larger fixed history quota fails later;
checking only the newest Turn misses prior Stop and recovery receipts. Tests
scan a 5,001-Turn history, skip scheduler-origin human carriers, preserve early
Stop and permit ordinary completed-child handoff beyond the retired boundary.

# Reusable team agents

This contract owns durable identities for Swarm children. The task engine
remains the only model/tool execution path; `lib/swarm/retained.py` owns
assignment transitions and `retained_repository.py` owns persistent records.

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


`send_agent_message` adds bounded `reference`/`steer` context to the exact
pending/running child assignment. Use the returned assignment `agent_id` and
`swarm_run_id`, not the stable retained identity ID. `get_agent_messages` reads
actual durable input receipts. `assign` still creates a new run only for an
idle identity; it is never a second instruction to a busy child.

The machine-readable authority is `contracts/swarm_messages_v1.json`; service,
portable record-CAS repository, and input carrier live in
`lib/swarm/{message_service,run_messages}.py`. Sending commits before input
acquisition and starts no thread, worker, model call or polling loop. Each
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
selection observes context headroom, a 4096-token and 24576 escaped-byte ceiling;
unfit originals remain submitted and inspectable. Durable session/run and
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

At terminal settlement, a successful durable mailbox close changes remaining
submitted records to `unconfirmed`. If close fails, read-only lookup projects
`unconfirmed` from exact terminal session/run evidence without rewriting records.
Neither case proves that the provider never received them. Old runs are not woken and
messages never transfer to replacement assignments. Stop uses existing cancel
control, not natural language. Child callers cannot send to or inspect siblings;
message text grants no tool permission or human provenance. The sender is a
write tool; receipt lookup stays read-only. Coordinator binding advertises
backend support, not execution authority. Passive project conversation messages
and explicit consideration use the separate scoped mailbox owner documented in
`modules/conversations_project_brain.md`; automatic assignment remains unavailable.

Executable specifications: `tests/swarm/test_run_messages.py`,
`test_run_messages_pipeline.py` (ordinary execution pipeline, hermetic model),
and `test_run_messages_storage.py` (disposable real Sidecar authority).

The repository carries owner, tenant and hashed project/conversation scope in
every key and validates the stored identity on read. A reset increments the
generation and clears working memory; `abandon=true` fences a busy run's late
memory commit. Archive blocks assignment; delete removes only an idle identity.
Full child evidence follows the ordinary Swarm/Inspector retention policy.
Read-only `list_agents(agent_id=...)` returns a bounded recent memory view to
the root without entering the write-approval partition. Current permissions
are checked on each new invocation; saved messages are history, never a grant.
Recovery first consults the durable Swarm checkpoint for a lost terminal
callback. A missing or ambiguous active run remains busy until explicitly
resolved, preventing a second worker from silently racing it.

For retained Browser agents, the child supplies its stable identity as a
working-tab cache namespace. Owner/device routing and access checks remain the
browser authority. Different Browser agents on one device do not share the
implicit current tab; every new assignment verifies the live tab and URL
before acting on prior page observations.

The repository bounds one identity to 8 MiB of serialized working state and
one scope's roster to 256 identities. A separate metadata-only roster bounds
list reads to 1000 small records even when memories approach their bound;
the full dialogue is fetched only by exact agent ID. Idle identities have zero
executor residency. A serialized empty identity measured 347 bytes and its
roster projection 341 bytes (ASCII sample, excluding storage framing); 256
empty identities therefore add about 172 KiB before backend overhead. The
hard per-scope payload ceiling is about 2 GiB of memory records plus roster
metadata, reached only when every identity fills its 8 MiB limit. These are
storage-growth bounds, not concurrency limits. Existing Swarm permits scale
from the host probe. The owning regression suite is
`tests/swarm/test_retained_agents.py`.

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
or truncated authority, and terminated sessions deny new execution. Recovery
rechecks authority after catalog reconstruction, registry publication and at
worker admission. Interrupted parents still require accepted Continue lineage;
ambiguous in-flight tools retain the existing no-replay failure. **Alternative:**
allowing every running session to restart would resurrect cancelled work.
Verification: `tests/swarm/test_swarm_detached_restart.py` and the restart,
activation-fence, cancellation and dependency suites.

**Problem:** recovery's `list_turns(limit=1, light=True)` still fetched the full
transcript before Python sliced it, so a large conversation could fail an
already-accepted Continue before model dispatch. **Decision:** recovery reads
the newest main-lane page at an authority-owned sync head, then pages backward
only when checking intervening Stop states. Pages shrink on byte exhaustion;
only status/identity headers are retained, and a changed head restarts the
bounded read. Continue needs at most one Turn; detached recovery keeps its
2,000-Turn safety bound and cancellation/lineage fences. **Alternatives:** retrying
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

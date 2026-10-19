# Defensive patterns

These rules govern lifecycle, concurrency, subprocess, and teardown work. Each
rule exists because its violation class has produced real defects in agent
runtimes; anchors point at the Tofu module that already applies it. Apply the
rules in new code and cite them in review; do not refactor existing compliant
code only to mention them.

## Background fan-out before demand submission

Problem: a directory-per-Future walk fed thousands of internal jobs to the
demand executor, whose worker hint controls idle reuse rather than concurrency.
Decision: each index walk retains a directory frontier and submits at most the
launch-probed `TOFU_TREE_INDEX_WALK_JOBS` at once. Root builds remain admitted
independently; overlapping roots share their executor generation, and their
scan windows add. The frontier keeps the complete directory descriptors; only
actual read failures make a walk incomplete, and incomplete walks never install
an index. Cancellation drains accepted workers before rows
are consumed or discarded. Alternatives rejected: changing shared executor
admission blocks user tasks; eagerly submitting every directory creates a
thread storm. Measurement: synthetic 4,096-directory walks preserve every file
at window sizes 2 and 8, with active scans bounded to the window and physical
threads bounded to twice it, including Future callback occupancy. Owner:
`lib/project_mod/tree_index.py::_walk_tree`; specs:
`tests/project_mod/test_tree_index_scan_pressure.py`.

## Heartbeat persistence cannot block the serving loop

Problem: filesystem calls inside the loop heartbeat stalled HTTP processing.
Decision: the existing watchdog thread persists the timestamp of the last
observed loop tick. A blocked write cannot freeze the serving loop, and writer
activity cannot refresh a stalled loop's liveness. Stop still joins that exact
thread; no worker or timer is added. Alternatives rejected: awaiting write I/O
in the heartbeat task delays liveness observation; writing the watchdog's
current timestamp masks an actual loop stall. Owner:
`lib/server/server_loop_watchdog.py::_persist_loop_heartbeat`; specs:
`tests/server/test_heartbeat_persistence_isolation.py` inject blocked storage,
prove loop progress, preserve stale timestamps, and verify joined teardown.

## Response delivery before close

Problem: immediate full close of a SQL storage.v1 socket can reset a fragmented
reply before the client receives its final bytes. Decision: send one response,
half-close the write direction, then retain its owned handler until client EOF
or the socket timeout. Release serialized-frame accounting after sending. No database transaction remains
open and no additional thread starts; ordinary clients close after decoding.
No count-admission queue or rejection remains; only proven worker-creation
failure returns a pre-dispatch error from the accept loop. Unknown command
delivery still uses receipt/reconciliation authority. Immediate full close and
unbounded client waits are rejected alternatives. Anchor:
`lib/storage_sidecar/server.py::_wait_for_peer_close`; regression:
`tests/storage/test_storage_server_overload.py`, including a client that holds its
socket open. The native storage.v2 and embedded lifecycle remain independently
owned by Tofu-DB.

## Dispose must reach quiescence

### Conversation sync ownership

Problem: snapshot failure, callback reentry and response setup cancellation
could skip authority or leak resources. Decision: `core/conversation-sync.ts`
acknowledges cursors only after required ingestion, checks generation across
callbacks and owns one abort signal for snapshot/history/rank reads. Disposal
cancels reads and silences late state/diagnostic publication. Its existing
watchdog starts at source construction; valid frames refresh it, repeated
open/error notifications do not. Reconnect timers are claimed before notifying
observers so reentrant pause/close can cancel them. `core/observer.ts` isolates
optional health, withheld-write and debug notifications; projection/commit
ports must still propagate failure. Health fanout survives a failed subscriber.
The HTTP adapter uses one `ExitStack` until response construction succeeds;
its generator finalizer includes initialization, and the existing 10-second
unconsumed-body deadline covers responses that never start. Broker release
callbacks share one isolated invocation path before/after close. Rejected:
catch-by-catch cleanup leaves the next setup step unprotected; per-page retries
create another recovery owner. No additional poller, queue or durable state.
Specs: `core/conversation-sync.test.ts`, `core/connection-health.test.ts`,
`tests/conversation/test_conversation_stream_{setup,ownership}.py` and the
real-EventSource/Surface browser test `test_visual_conversation_sync_recovery.py`.
The resource regression runs 100 recovery/disposal cycles, requiring zero live
streams/timers and aborted read signals after every disposal.

### Joined producers

Teardown is not complete when the kill signal is sent; it is complete when the
killed thing has exited and its late completions can no longer observe or
mutate the world. Kill, then await exit. Close listeners, subscriptions, and
notification registries before killing their producers so a completion that
arrives during teardown stays silent. A worker that cannot be killed
cooperatively is waited out, never abandoned while still holding task state.
Anchors: `lib/agent_core/execution_session.py` (private resource stack
settlement), `lib/tasks_pkg/tool_dispatch/_pipeline.py`
(`_drain_cancelled_tool_workers`).

### Interrupted resource settlement

Problem: a service can close its native child clients while an ordinary Flow
worker still owns a graph. Failing library close and then retiring the serving
loop strands that worker's async cleanup. Decision: application-owned Flow close
retires new graph admission and waits off-loop for accepted calls and graph
borrowers. Existing graph operations remain usable through their final release.
Standalone close retains its explicit borrowed-graph refusal. The existing
condition signals quiescence; no new thread pool, polling loop or registry is
added. Alternatives rejected: unloading early risks native use-after-free;
blocking the serving loop prevents the borrower from finishing.

The same active-Goal shutdown probe exposed a partially imported optional
translation module. Shutdown now performs normal import on its existing worker
before calling cleanup, joining Python's import lock while keeping unused
modules unloaded. Direct `sys.modules` attribute access bypassed that lock;
skipping an unfinished module would lose its cleanup obligation. Verify Flow
lifetime, host close, server shutdown import-race and full production Goal tests.
The resumed-provider fixture shut down both embedded and daemon applications
in approximately 1.27 seconds with exit code zero, no forced group kill, zero
remaining native clients and no live child processes. This is one controlled
local observation per transport, not a general shutdown latency guarantee.

Problem: cancellation inside a release callback skipped later resources and
allowed a subsequent settlement to repeat non-idempotent cleanup; recursive
settlement also reentered callbacks. Decision: `ExecutionSession.settle` refuses
reentry, attempts every remaining release, freezes a failed terminal receipt,
then propagates the first control exception unchanged. Recoverable resources
retain explicit deferred dispositions; another finisher reads the same receipt.
Acquire-to-registration rollback also settles the prior resource stack in a
finalizer, including when the unbound resource's rollback is interrupted.
Released callback closures are dropped because only dispositions have continuing
value, including when propagated exceptions retain cleanup stack frames.
No thread, queue, retry owner or durable record is added. Alternatives:
immediate propagation leaks later resources; swallowing cancellation hides its
cause; keeping closures until task expiry retains captured environments.
`test_execution_session_cleanup.py` retains 100 terminal sessions whose callbacks
captured 64 KiB each: 6,553,600 captured payload bytes become zero reachable payload
bytes after settlement while all receipts remain. This measures ownership, not RSS.

## Report orthogonal outcomes independently

`timed_out`, cancellation, exit status, and error kind are orthogonal facts.
Report each from its own observation; never derive one from another's branch,
and never let a branch that handles one outcome swallow the evidence of
another. A result that was both slow and cancelled says so; a result that
timed out is not rewritten into a generic failure. Anchor:
`lib/tasks_pkg/tool_dispatch/_pipeline.py` (`round_entry['status']` carries
`timeout` / `aborted` / `error` as distinct terminal values).

## Dispatchers contain callback exceptions

A subscriber that raises must not break the lifecycle it was observing. Event
dispatch, hook fan-out, and registry notification catch per-callback, log with
the callback's identity, and continue with the remaining subscribers. The
inverse is equally required: callbacks that must succeed (durable writes,
settlement) are not dispatched as fire-and-forget observers; they are awaited
pipeline steps. Anchor: `lib/agent_core/events.py` keeps emission separate
from durability (`lib/tasks_pkg/event_log.py` raises `EventDurabilityError`
instead of observing).

## Native TeX compilation runs only behind the paper-build supervisor

Every `latexmk`/engine invocation goes through `lib/paper/builds/process.py`
(POSIX rlimits, parent-sampled RSS, process-group kill, Windows Job Object,
subreaper) with frozen input snapshots, disk-bounded logs, shell-escape
disabled, and independent PDF structure validation before publication. Raw
`subprocess` TeX launches, inherited-environment engines, and compile output
written into the source tree are defects. The retired legacy preview pipeline
(removed; it had none of these bounds) is the reference counterexample; the
authority for budgets and lifecycle is [PAPER_BUILDS.md](PAPER_BUILDS.md).

## Untrusted output gets no predictable path and no inherited secrets

Files derived from model output, tool output, or remote content are created in
a private directory (`0o700`) with a random name and exclusive-create flags;
they are never written to a path the producer could predict or pre-create.
Subprocesses spawned to handle untrusted input receive a scrubbed environment:
variables matching key/secret/token/password patterns do not propagate.
Symlink-shaped paths are unlinked with `lstat` semantics, never followed into
their target. Anchor: `runtime_guards.py` owns the process-launch boundary;
storage sidecar temp-file handling owns atomic rename.

## An empty catch names what it swallows

A `catch`/`except` body that does nothing must name, in a comment at the catch
site, the exact exception or condition it swallows and why nothing else can
reach it; the guarded statement is kept to one operation. Broad `except
Exception` with a silent pass is a defect. Prefer the narrowest exception
class, and re-raise when the caught value is not the named one.

## Closed unions end in assert-never

When switching on a discriminant tag of a closed union (event `type`, result
`status`, directive `action`), the fallthrough branch calls
`typing.assert_never` so adding a variant without a handler is a type error
and a runtime failure, not a silent skip. Merge-extensible vocabularies (the
event registry) instead document their default branch explicitly; see
[EVENTS.md](contracts/EVENTS.md) for the read policy that governs those.

## Misconfiguration fails loud at the earliest resolvable point

A configuration error that can be detected at load time raises at load time;
one that needs a resolved referent raises at the first point the referent is
known. Missing referents are never silently skipped, and retired keys are
rejected by name rather than ignored. Anchor: `runtime_guards.py`
`enforce_deployment_configuration()` (fatal boot gate), storage sidecar
`preflight.py`.

## Artifact preparation resolves tools from the deployment environment

Problem: a GUI/service can have a minimal PATH even when a working toolchain
exists in a custom installation; shell-only lookup makes restart repeatedly
fail after source edits and an unavailable release download. Decision:
`serverctl_pkg/native_engine.py` resolves Cargo from the service PATH, then
`CARGO_HOME/bin` (default `HOME/.cargo/bin`), and prepends the selected proxy
directory to the build child's PATH so sibling rustc/rustup remain available.
Custom `CARGO_HOME` and `RUSTUP_HOME` belong in the project `.env` when the
service does not inherit them. Explicit process settings retain precedence;
the serving process environment is not rewritten to locate build tools.
Alternatives rejected: sourcing shell profiles executes unrelated startup
code; searching host-specific directories is not portable; bypassing engine
identity or switching storage weakens the pre-shutdown safety boundary.

## Defaulting is an explicit resolve step

At a module boundary, turning a request into an executable spec is one named
step (`resolve(request) -> Spec`) owned by the implementation. The execution
path (`run(spec)`) contains no hidden `or default` fallbacks: by the time
execution starts, every value it uses has been chosen, validated, and made
inspectable in one place. A constant named `DEFAULT_*` or a test hook is not
configurability; deployment-varying choices are validated configuration with
declared precedence, per the resource-profile precedent in
`runtime_guards.deployment_resource_default`.


## Optional terminal durability before visibility

Problem: a capability could publish done while its terminal fact existed only
in memory. Decision: TaskRuntime's opt-in terminal_persist port runs after
execution-session settlement and before task status/event publication, outside
the registry lock. A refused write retains its original candidate and blocks
terminal TTL reclamation. Finish/poll retry metadata only; completed resources
are not executed again. Runtimes without this port retain transient behavior.
The private pending record freezes both terminal state and event fields before
I/O; each port call receives a detached state copy. Caller/port mutation cannot
change retries, and polling preserves original event fields. Explicit false ACKs
remain pending. One outer finalizer releases the terminalization guard across
snapshot, settlement, persistence and publication failures. At most one frozen
candidate and one current port copy are retained per finishing task; retry count
does not add retained payloads. Persisting after push, retaining caller aliases
or replacing the candidate with a storage-error outcome were rejected. Anchor:
`lib/agent_core/task_runtime.py::finish`; specs: `test_terminal_candidate_retry.py`
and `tests/research/test_research_action_terminal.py`.

## Worker startup transactions

Problem: `Thread.start()` waits for a new carrier's bootstrap. Holding the
scheduler condition across that wait also blocks already-completed jobs from
publishing their Future and counters. A retained single-time fault snapshot
showed thousands of workers at condition acquisition; it does not identify the
owner of every condition. A controlled 200ms start stall reproduced the precise
boundary: the preceding job's Future waited 200.327ms in the original kernel,
while the revised kernel completed it in 0.030ms under the same start stall.
This is a component fault test, not a live latency or native-storage SLO claim.

Decision: `tofu_workers/executor.py` keeps physical-start transactions serialized
under a separate startup lock, with consistent startup→condition lock order.
Registration and startup reservations share one condition hold. New pending
items remain provisional and non-consumable until physical startup succeeds;
start/constructor failures remove and cancel that item before reporting rejection.
The condition is released for actual `Thread.start()`. Completion, cancellation,
Future callbacks, snapshots and retirement bookkeeping never need the startup
lock. Worker-exit bookkeeping happens first; only replacement creation waits
outside the condition. `shutdown(wait=False)` fences promptly, while joined
shutdown releases the condition while awaiting startup resolution and then
joins the remaining physical/retiring carriers. Initializer failure or queued
cancellation cannot resurrect an item during startup commit. Abandonment commits
quarantine only after replacement startup, without adding active work back if
the original job completed meanwhile. Resize retains its original idle-hint
rollback. Resident metrics count registered live carriers; `startingWorkers`
separately exposes unresolved startup reservations, including zero.

Alternatives rejected: merely dropping the condition around start lets a worker
execute a provisional item after submission later reports failure; a new starter
worker/queue adds ownership and shutdown complexity; an active quota or sleep
retry would suppress the machine-scaled demand behavior instead of fixing the
critical section. The idle hint remains an idle hint: a 32-way rendezvous with
hint 1 still runs all 32 jobs. Existing quarantine, callback notification and
independent packaged-helper behavior remain tested. No durable data, timer or
worker pool is added. Three bounded lifecycle sets hold references only until
startup or physical cleanup resolves; their cardinality follows the executor's
existing carrier demand rather than introducing a second thread budget.

Tests live in `tests/agent_core/test_worker_startup_transaction.py` alongside
`test_agent_worker_executor.py`; private seam names follow reservation ownership.
Packaged search/trading worker copies are regenerated only from the stdlib owner
by `scripts/gen_packaged_demand_workers.py`; fresh isolated-process imports verify
both copies run the same 32-way rendezvous without booting the application.


## Native reader release must preserve join debt

**Problem:** taking an actor JoinHandle out of a mutex before waiting lets a
concurrent release report success while the actor still owns task state. A
failed consumed handle then makes retry appear successfully released.
**Decision:** the reader lease keeps Running/Joined/Failed under one mutex
through the exact actor join and tail publication. Concurrent finish observers
wait for that same join; failure remains Failed on retry and tail acquisition.
Only Joined acknowledges release. **Alternatives rejected:** locking only the
handle extraction proves transfer, not quiescence; treating an absent failed
handle as success erases cleanup debt. This serializes one reader's teardown,
never task admission or unrelated readers. Owner:
`packages/tofu-runtime/src/cli_agent_control.rs::ReaderLease::finish`.
Specs cover blocked concurrent release, actor panic/retry, exact tail ownership
and real stdio cancel/checkpoint/model-control boundaries. Persistent worker
reuse must use this barrier before advertising readiness; no reuse is currently
selected by this change. Evidence and the 8-byte per-reader size change live in
[resource control](RESOURCE_CONTROL.md#native-reader-join-fence).

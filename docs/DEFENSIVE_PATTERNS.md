# Defensive patterns

These rules govern lifecycle, concurrency, subprocess, and teardown work. Each
rule exists because its violation class has produced real defects in agent
runtimes; anchors point at the Tofu module that already applies it. Apply the
rules in new code and cite them in review; do not refactor existing compliant
code only to mention them.

## Failure diagnostics cannot replace the original failure

Problem: an exception's broken `__str__`, or an unprintable argument preview,
raised a second exception while the tool executor was reporting the first.
The model lost recovery classification, and completed-Future collection could
also fail while logging that error. Decision: common tool failure projection,
handler diagnostics and collection logging use the existing pure `text_value`
formatter from `lib/logging_pkg/log_record_values.py`. An explicit unprintable
marker preserves the original type; diagnostic formatting never supplies a new
replay policy or successful verdict. Handler logs, fingerprints and model feedback
reuse the same rendered detail. Argument previews retain their existing bound.
The formatter's ordinary exception guard does not swallow process-control exits.
Alternatives rejected: fallback errors obscure the operational cause; omitting
failed occurrences loses effects and siblings; per-tool formatters duplicate policy.
Verify `test_tool_diagnostic_recovery.py`, real root-loop continuation/terminal
failure stopping, and installed native parallel recovery with joined workers.

## Fair admission must lapse on its own

Problem: a shared/exclusive admission gate admitted every compatible request
as long as no writer held the key, so overlapping shared holders starved an
exclusive request indefinitely; plain writer preference would instead freeze
all compatible work behind a writer that waits for one long holder, and an
unbounded order rule turns any lock-order inversion between shared requests
into a deadlock. Decision: a fresh request yields to an earlier queued
conflicting request only while every holder blocking that request is younger
than a fixed bound; a thread that already holds the key never yields, and a
request that stops waiting (cancel, timeout) leaves the queue under the same
condition that admits. Holders in a wait cycle stop releasing and so cross the
bound, which makes the order rule unable to sustain a cycle. The bound is a
hardware-independent constant, never sized from load or capacity.
Alternatives rejected: writer preference, strict FIFO, per-key counters.
Anchor: `_queued_ahead` in `lib/project_mod/workspace_lease.py`; decision
record "Admission order" in `docs/runbooks/GIT_WORKSPACE_GUARD.md`; verify
`tests/project_mod/test_workspace_lease_conflict_model.py`.

## A tool call's edit is atomic per file, never across calls

Problem: workspace leases for model tool calls (claims derived per shell
command, file leases for the write tools) queued calls for up to an hour behind
other sessions' commands, and every attempt to refine the claim granularity
left another read misclassified as a whole-tree writer. Decision (owner,
2026-10-28): tool calls take no workspace lease. The only exclusion a tool call
takes is `file_edit_mutex.file_edit_mutex(paths)`: one in-process mutex per
resolved path, held for exactly one call's read-modify-write (preimage read,
edit, publication, journal), acquired in sorted order, reentrant on its own
thread, with entries only while a call holds or awaits them. Never hold it
across a command or another tool call, never widen it to a directory or
checkout, and re-validate the target and the observed version after acquiring
it. All tool calls of a model round may run in parallel on top of it.
Alternatives rejected: finer-grained claims; file leases that wait behind
commands; serializing a round's mutating calls. Anchor:
`lib/project_mod/file_edit_mutex.py`; decision record "Tool calls take no
workspace leases" in `docs/runbooks/GIT_WORKSPACE_GUARD.md`; verify
`tests/project_mod/test_tool_calls_take_no_workspace_lease.py`.

## Atomic file writers retain sole descriptor ownership

Problem: `lib/json_store.py` removed its staging file after interrupted chmod
or failed stream construction, but left the `mkstemp` descriptor open.
Decision: the writer owns that descriptor throughout; `fdopen(closefd=False)`
only borrows it. A `finally` closes it before publication, including after a
setup failure. Failure removes the staging file and leaves the published
destination intact. Alternatives rejected: unlink alone does not close a
descriptor; switching ownership after stream construction leaves a cancellation
window where a later stream close can close an unrelated file that reused the
descriptor. Specs: `tests/json_store/test_atomic_writer_lifecycle.py`, including
cancellation immediately after stream creation.

Text and bytes publication share this lifecycle in `_write_payload_atomic`.
Their public entry points retain UTF-8 text handling, bytes-like validation,
file modes, optional sync and the existing cache-relief policy. The shared
writer uses the original open descriptor without reopening the staging file
or materializing an encoded text copy. Keeping independent cleanup loops was
rejected because every resource fix would otherwise need two implementations.

## Process-global locks guard one-time work, never reads

Problem: one process-global lock in the memory store wrapped its one-time
idempotent migrations and also every corpus listing, exact-ID probe and
skill-registry scan. A listing performs one `lstat` per memory file; each
syscall releases the GIL, and re-acquiring it behind CPU-bound threads costs
several switch intervals, so one 1,950-file scan took tens of seconds in the
server while concurrent task startups queued behind it (`startup_memory` p90
64 s, max 116 s; an isolated warm listing takes 0.09 s). Decision: the lock
(`_migration_lock`) serializes only pending migrations. `run_storage_migrations`
returns without it once the monotone latches cover every requested root, and a
latch is set only after its migration finished, so a lock-free reader that sees
it sees the migrated layout; a caller whose root is still migrating waits for
that migration. Scans of migrated roots can now overlap another root's legacy
global copy into the shared server stores, so those copies stage under hidden
sibling names (scans skip dot entries) and publish with one rename. Mutations
never took this lock (they serialize on per-record file-lock shards in
`_locked_memory_revisions`), so no write exclusion was lost.
Alternatives rejected: a reader/writer lock still parks every scan behind each
migration attempt; per-root locks do not cover the shared server stores; a
faster scan under the same lock keeps the convoy on any slow filesystem.
Anchor: `lib/memory/storage/_dirs.py::run_storage_migrations`; specs:
`tests/memory/test_memory_scan_locking.py`.

## Coalesced reads share only reads that start after arrival

Problem: without the global lock, N identical task-startup listings each paid
a full corpus scan, yet handing a caller an in-flight scan would return a
snapshot older than its arrival and miss a memory written just before its call.
Decision: `lib/memory/queued_read_coalescer.py::QueuedReadCoalescer` keeps,
per key, at most one running read and one queued read. Callers that arrive
while a read runs join the queued read, which starts only after the running one
settles, so
any burst costs at most two reads and every result began after its caller
arrived. The key is the complete request (`load` receives only the key: store
directories in precedence order, body mode and record view; eligibility reads
process-wide env/PATH/vault state when the scan starts). A read with several
callers gives each an independent copy because API callers mutate rows. Values
are never retained after the read, an idle key holds no state, and nothing is
sized by a window, cap or timeout; the frontmatter LRU stays the only
cross-call cache, under its existing budget. Alternatives rejected: joining the
running read serves stale state; `BurstReadCoalescer` closes its gather after a
fixed window before the read starts, so a multi-second scan is never shared and
the window is a tuning knob; a result TTL is a knob that serves stale rows;
starting the queued read immediately lets sustained arrivals stack overlapping
scans of one key. Anchor: `lib/memory/storage/_crud.py::list_all_memories`;
specs: `tests/memory/test_queued_read_coalescer.py` and
`tests/memory/test_memory_listing_coalescing.py`.

## An accepted attempt lists its memory corpus at acceptance

Problem: `startup_memory` listed the eligible memory corpus only after the
input-translation join, executor start, worker entry and tool assembly, and the
listing was nearly all of it (production `memory_list` p50 1.56 s, max 13.8 s;
`memory_rank` and `memory_hydrate` about 13 ms), although listing the corpus
does not depend on the query it is ranked against.
Decision: an accepted ordinary (non-Flow, non-dispatch) root attempt whose
accepted configuration already opens the prefetch gate (memory enabled after
the profile and chat-mode overlays, prefetch enabled, no replayed Continue tool
calls: `_memory_prefetch.accepted_memory_prefetch_roots`) starts the corpus
listing next to its deferred input translation
(`lib/tasks_pkg/memory_listing.py::start_early_memory_listing`), on the shared
context-provider demand executor. `run_memory_prefetch` consumes it only for
the exact roots it would list (`lib/memory/prefetch/_listing.py`, the one
listing definition both paths use), joins it if still running, and ranks and
hydrates as before, so the result is identical for the same corpus and query.
Freshness: the listing goes through the queued-read coalescer, so it reflects
the corpus at or after the attempt's acceptance, which is no older than the
user's request; every memory written before acceptance is listed, while one
written between acceptance and `startup_memory` may not be (selected bodies
are still re-read at hydration). The task owns the listing: startup memory
consumes or releases it, orchestration exit (`stop_prefetches`) releases it
after a Stop or a startup failure, terminal heavy-state release drops one that
never reached a worker, and acceptance releases it when it registers no task
or rolls one back. A scan already running finishes unused; nothing else runs
for a stopped attempt.
A listing that ran and failed fails the prefetch exactly as an inline listing;
one that never ran, or was started for roots that worker workspace binding
later moved, is replaced by the inline listing, and starting one never fails
acceptance. `memory_list` now measures the wait; its `listing`/`listingMs`
completion details give the listing time, hence the hidden share
(docs/LOGGING.md). Residual: an attempt whose tool assembly ends text-only (a
registry failure or an internal allow-list without base tools) releases a
listing it started; child invocations still list inside their own startup.
Alternatives rejected: starting at worker entry hides less (context assembly,
registration, executor start); starting once tool assembly decides the whole
gate hides nothing; a cached cross-task corpus needs a staleness window, which
is a knob that serves rows older than the request. Cutting the per-file
`lstat` itself was not exact in-process: on Linux `os.scandir` yields only
`d_type`/`d_ino` (`DirEntry.stat()` is the one `lstat` the scan already takes
per file), `d_ino` alone misses in-place external edits, and the repository
has no native batch-stat call; under two CPU-bound threads that `lstat` is
about 80% of a listing and `readdir` about 20%.
Measurement (simulated startup on a FUSE copy of the production corpus, 1,983
entries, warm frontmatter cache, 1.5 s translation, shared loaded host): with
eight threads alternating 1 ms of Python work and 20 ms of sleep (inline
listing 0.5-2.6 s, as in production) the acceptance-to-ranked-memories path
fell from a median 2.96 s to 1.53 s over five interleaved runs; with eight
pure CPU-bound threads the listing itself takes two to five minutes, and
starting it early hides only the translation window (1.0-1.4 s per run), well
inside the run-to-run variance of the listing.
Anchor: `lib/conversation_sync/task_start.py::start_conversation_attempt_executor`;
specs: `tests/tasks_pkg/test_early_memory_listing.py` and
`tests/tasks_pkg/test_startup_timing_anchors.py`.

## Background fan-out before demand submission

Problem: a directory-per-Future walk fed thousands of internal jobs to the
demand executor, whose worker hint controls idle reuse rather than concurrency.
Decision: each index walk retains a directory frontier and submits at most the
fixed `TOFU_TREE_INDEX_WALK_JOBS` window (8) at once. Root builds remain admitted
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
Permanent EventSource closure uses that same timer and snapshot barrier; native
CONNECTING errors retain browser retry. Snapshot success does not reset stream
failure backoff, and wake/resume hints join the scheduled retry. The observer
route has no model-stream admission lease: broker teardown is its sole owner.
The resource regression runs 100 recovery/disposal cycles, requiring zero live
streams/timers and aborted read signals after every disposal.

Turn runtime composition keeps one store subscription per conversation ID.
Problem: object-keyed bindings outlived replaced catalog shells, and a failed DOM
cleanup skipped transport teardown. Decision: the ID-keyed binding owns its shell,
subscription and deferred terminal-release eligibility; replacement and disposal
revoke them together. Disposal ignores recursive entry and retains the final Turn
state for host cleanup reads. The coordinator closes first and publishes its final
health before host cleanup withdraws any resulting DOM demand; `finally` releases
receipts, attempt hooks, observed live attempts and state, preserving the host's
error. Queued hydration checks its lane;
snapshot and host callback continuations check the current store before binding.
Rejected: consulting an optional catalog to release owned resources, clearing the
store before per-Turn UI cleanup, or swallowing required cleanup failures.
Specs: `core/turn-runtime-ingestion.test.ts`, including 100 replacement/disposal
cycles with zero remaining timers and no old-store projection callbacks.

Command execution distinguishes a server outcome from the lifetime of its view.
Problem: late ACKs could rebind retired shells, reconnect disposed stores or turn
an accepted write into a client failure. Decision: `core/turn-command-execution.ts`
checks store ownership before pending-state publication and each view-owned write,
and again before adopting its response. A lost owner before dispatch yields
`AbortError`; disposal after
dispatch preserves the original response/error and suppresses stale UI adoption.
It never aborts accepted backend work. Caller-owned standalone stores retain the
low-level API; weak runtime-store membership distinguishes them from retired
managed stores without retaining either. A command may bind its incoming shell
unless a later binding has claimed that store; terminal hooks use the current
binding. Late failure cleanup and ownerless aborts never allocate a store.
Explicit abort by attempt ID keeps its existing authority and 15-second deadline.
Conflict adoption shares one generated decoder and checks conversation identity
plus the requested Turn identity when known. Full pre-edit reads share the sync
coordinator's abort signal and existing 30-second deadline, check returned identity
and cannot start a write after disposal. Rejected: treating view closure as command
failure, aborting writes with read lifetimes, copying state into command contexts,
or one cancellation controller/recovery loop per endpoint. Specs:
`core/turn-runtime-commands.test.ts`, `test_vite_migration_residuals.py` and
`test_frontend_attempt_stream_vite.py`.

Attempt callbacks have the same store identity boundary. Problem: runtime-global
maps outlived deleted attempt rows, and closing an old lease could erase a newer
callback for the same ID. Decision: `core/turn-attempt-lifecycle.ts` owns leases
and live observations per store. Its subscription retires known callbacks when
their canonical attempt/current-Turn identity disappears; a subscription created
before its ACK remains until materialization, explicit close or store disposal.
Disposal releases the owner directly, without enumerating surviving data rows.
Closing a lease compares its registration identity and leaves the shared stream
running. Terminal delivery consumes that registration before invoking required
callback code, preserving both replacement registrations and thrown errors.
Current bindings receive settlement observations; retired stores cannot connect
to a reopened coordinator. Rejected: TTLs for dangling callbacks, global ID maps,
row-driven disposal, or an independent transport per callback. Specs:
`core/turn-runtime-attempts.test.ts`; retained-reference measurements are in
`RESOURCE_CONTROL.md` under Turn attempt callback retention.

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

Storage comparison phases follow the same boundary in
`scripts/tofudb_workloads/workers.py`: a failed start releases accepted workers,
timeout stops subsequent operations, and callback completion plus joined
teardown precede engine disposal. Thread liveness after an interrupted join is
not a session-cleanup receipt. The benchmark policy and alternatives live in
`STORAGE_PERFORMANCE_CHARTER.md`; fault specs are
`tests/tofudb/test_comparison_phase_lifecycle.py`.

Linux process-group settlement also checks the thread count: a zombie leader
can retain live native threads and inherited descriptors. The common
`lib/project_mod/process_group.py` owner treats that group as live and escalates
termination before claiming quiescence. State-only zombie exclusion is rejected
because it can release resources while writers remain. The independent native
thread fixture in `test_process_group_thread_quiescence.py` verifies observation
and retirement after the direct launcher has already been reaped.

Cooperative tool cancellation: **Problem:** an acknowledged Stop could close the
runtime context as cancelled while publishing the tool row and canonical result
as an execution error. **Decision:** the common executor combines one snapshot
of the owning context's Stop state with the shared typed-cancellation predicate.
Only that combination settles `aborted`; cancellation-shaped text and genuine
runtime failures do not acquire that verdict. The existing error projection
recognizes `AbortedError`, future cancellation, `ToolCancelled` and the proven
pre-entry `CommandAdmissionCancelled`, and unknown
write effects still require reconciliation. **Alternatives rejected:** relabeling
every exception after Stop hides real failures; handler-specific terminal writes
duplicate settlement authority. Shared batch workers use the same typed predicate
to report cancelled slots as unsuccessful without logging an unexpected runtime
fault; siblings still join and keep their results. Verify `test_tool_cancellation_outcome` alongside
handler failure, timeout and finality suites.

### Nested publication authority

Nested publication authority uses the same revocable fence as child delivery.
Problem: replacing the active fence on nested delivery let an open child
publish after its parent closed. Decision: retain every enclosing fence and
check them in parent-to-child order at bounded local commits. Re-entering one
fence does not duplicate it; scope exit restores the prior context. Observer
I/O holds no fence lock, so closing publication never waits for that observer.
Explicit root FIFO acceptance still transfers to the receiving owner's
context. Alternatives rejected: retaining only the newest fence drops parent
authority; holding locks over observer I/O makes revocation wait on callbacks.

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

A command's outcome and its process-group retirement are orthogonal as well:
a group that cannot be confirmed quiescent after SIGKILL adds a typed warning
to the decided result and keeps the group's record, and never replaces the
output, exit status or interrupt verdict. Anchor:
`lib/project_mod/run_command.py` (`_retire_command_group`); decision record
"A finished command's result survives its group's retirement" in
`docs/runbooks/GIT_WORKSPACE_GUARD.md`.

## Dispatchers contain callback exceptions

A subscriber that raises must not break the lifecycle it was observing. Event
dispatch, hook fan-out, and registry notification catch per-callback, log with
the callback's identity, and continue with the remaining subscribers. The
inverse is equally required: callbacks that must succeed (durable writes,
settlement) are not dispatched as fire-and-forget observers; they are awaited
pipeline steps. Anchor: `lib/agent_core/events.py` keeps emission separate
from durability (`lib/tasks_pkg/event_log.py` raises `EventDurabilityError`
instead of observing).

## Commit wakeups publish after the writer's critical section

Problem: a running task holds its projection lock across each
`turn.event.record` command, and the storage client published post-commit
notices before `command()` returned, on that stack. The broker's `notify`
push deep-copied the frame and submitted its encoding to the Push frame pool
that every conversation's frames share; a stall dump showed task-event
delivery threads parked on that pool's lock while holding
`_turnProjectionStateLock`, so one task's durable write waited on push
encoding for every conversation. Decision: `defer_committed_events(order)` in
`lib/storage/commit_events.py` holds the committing thread's notices in a
per-writer `CommittedEventOrder` until the outermost scope exits, and
`lib/turn_lifecycle.py::_task_projection_critical_section` enters it outside
the projection lock, so publication starts after the release. Every Turn write
of a task commits under that lock and the client hands its notices over before
`command()` returns, so the order receives them in commit order. One thread at
a time publishes from it; a thread that finds another publisher leaves its
notices to that publisher, which re-checks the order before giving up the
role. An uncontended writer still publishes before `record_task_event`
returns; a contended one may see its notice published by the concurrent
writer, never ahead of an earlier commit. Calls outside a scope publish
synchronously as before. Alternatives rejected: per-thread deferral lets a
later writer publish first; relying on the task's delivery-lane FIFO misses
Flow output checkpoints and terminal-debt maintenance, which record outside
the lane; an O(1) hook inside the lock still runs broker, bus (Redis
`PUBLISH`) and listener work in the critical section. Specs:
`tests/storage/test_committed_event_order.py`;
`tests/turn_lifecycle/test_commit_wakeup_publication.py` instruments the real
SQLite record path and proves that no `push_event`, deep copy (`prepare`) or
frame-pool submission runs while the projection lock is held, and that an
interleaved second writer cannot publish first. Measurement, synthetic fixture
with the real broker, Push Hub, frame store and a serving loop draining four
owner sockets, under four CPU-bound threads, eight task writers (40 commits of
0.5 ms each) and four pre-change translation publishers loading the frame
pool, three alternating fresh-process runs: projection-lock hold p50
38.8–44.6 → 12.0–12.8 ms and p99 176–186 → 94–118 ms, while end-to-end
record latency, which still includes publication, stayed at p50 38.8–44.6 vs
41.3–46.7 ms. A second thread per task taking the same lock every 2 ms waited
mean 37–80 → 7.9–10.8 ms, p99 0.72–1.06 s → 93–111 ms and max
0.99–1.17 s → 0.13–0.28 s, acquiring it 2–5 times as often. The remaining
hold is GIL handoff under that load, not push work. These are component
observations on a shared host, not production latency.

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

## Long-running processes spawn helpers from their loaded source

Problem: the server keeps caller code in memory while the checkout beneath it
changes. A helper started by path, or with `-m lib…`, from the checkout pairs
the old caller with newer or older child code. After 49053ced2 changed the Git
snapshot helper's argv, every hardened `git diff` of the running server failed
until a restart; any on-disk edit, mid-checkout file or deleted helper does the
same to any such spawn.

Decision: a self-contained helper (standard library or installed packages
only) is spawned from `PinnedHelperSource.path()` in
`lib/runtime/pinned_helper_source.py`, created at import by the module that
builds the helper's arguments. The bytes are read once. `path()` returns an
immutable file named by their SHA-256 under `tofu-pinned-helpers-<uid>` in the
system temporary directory; the directory is created 0o700 and refused when it
is a symlink, foreign-owned or group/world accessible. No state belongs to a
process: re-exec, crash and SIGKILL leave nothing behind, processes loading
the same source share one file, and a file an age-based cleaner removed is
rewritten from memory on the next call. Adopters: the Git index snapshot
child, the ssh ProxyCommand connector (isolated `-I -S`), the VLM PDF render
worker, the knowledge OCR worker, the mtskills Node bootstraps and the TeX
supervisor. A child that
cannot parse its arguments reports "restart the Tofu server" instead of a
traceback. Retention: one file per helper source revision ever spawned by the
user (30 KB for all seven helpers per revision), no per-process growth; unused
revisions are left to the system temporary-file cleaner because a still-running
older server may need its revision.

Alternatives rejected: an explicit protocol version alone turns skew into a
clear failure but still fails every invocation until a restart; `python -c`
puts the child source into each command string and argument vector;
per-command materialization needs a command-lifetime directory at every spawn
site, which an ssh ProxyCommand inside a user's shell command does not have;
a `mkdtemp` directory per process removed at interpreter exit leaks one
directory per SIGKILLed process and per in-place server re-exec, because
neither runs exit handlers.

Not covered: module workers that import sibling or application modules from
the checkout at spawn (`-m lib.paper.builds.pdf_validation_worker`,
`-m lib.artifacts.image_validation_worker`, `-m lib.motion_video._continuity`,
the PDF parser's spawn-context pool, the cp splice helper importing
`tofu_workers`) keep backward-compatible arguments until they become
self-contained. The storage sidecar checks its `storage.v1` handshake, and
restart helpers deliberately run the target checkout's code. Lazy in-process
imports follow the next section. Verify `tests/runtime_pinned_helper_source/`
and each adopter's "changed on disk" spec.

## Lazy imports load the source pinned at boot

Problem: most application imports are function-local, so a module first
imported after boot compiled whatever the checkout held at that moment against
the modules loaded at boot. Merge 048be1056 landed d95ceee52 a day after the
worker booted; it added `stop_on_authority_failure` to
`lib/project_dispatch/admission.py` together with a caller in
`lib/tasks_pkg/compaction/_summary_accounting.py`. The first L2 summary after
the merge compiled the new caller against the boot-loaded `admission`; the
ImportError ended the task as "Internal error" and broke every later summary
in that worker until a restart.

Decision: `server.py` (as `__main__`) and `asgi.py` install
`PinnedImportSource` from `lib/runtime/pinned_import_source.py` for the roots
they put on `sys.path` (the checkout and the tofu-search source). The finder
sits just before `PathFinder`. A background thread pins the modules directly in
each root and the trees of the top-level packages the process imports: those
already imported at installation, any other at its first import. The first
read of a file wins, and a file rewritten while it is read stays unpinned.
Later imports compile the pinned bytes; bytecode is reused only when its header
names the pinned revision and written only while the checkout still holds it,
and a module whose file has changed primes `linecache` so tracebacks show the
code that ran. `importlib.reload` reads the checkout on purpose (the vendored
MCP registry hot reload). Files added after pinning load from the checkout;
deleted modules still import from their pins. Only directories holding
`__init__.py` are entered: on 2026-10-31 runtime data inside `lib/` held
5,608,622 files in 341,314 directories. Measured on that checkout: 2,602 files
of `lib`, `routes`, `bootstrap_pkg`, `serverctl_pkg`, `tofu_db`,
`tofu_workers` and the root modules, 31.4 MiB held as 9.1 MiB of zlib bytes,
pinned in 4.6 s off the serving path. This is resident memory per process,
proportional to the application source, not to uptime. A restart loads the
checkout again.

Alternatives rejected: importing every module at boot loads optional
capabilities eagerly and still fails on modules deleted later; running the
worker from a copy of the source breaks every `__file__`-relative path to
data, logs and static assets; restarting after each merge interrupts running
tasks; reading boot revisions back from Git cannot serve files that were
uncommitted at boot and puts a subprocess in imports; pinning every package in
a root (including `tests/`) took 71 s of cold reads for code a server never
imports.

Not covered: a merge that lands while the boot imports and the sweep are still
running (seconds) can still mix revisions; a package first imported late is
pinned at that import; namespace-package portions and non-Python files read
through `__file__` load from the checkout; the Supervisor and lifecycle manager
processes do not install the finder. Verify `tests/runtime_pinned_import_source/`.

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


## Capability completion uses the existing terminal claim

Problem: a capability could write its artifact before calling `finish`, allowing
an accepted Stop or competing finish to disagree with that publication.
Decision: `TaskRuntime.finish(commit_result=...)` claims terminal ownership
before capability I/O. A successful `ExecutionSession` releases its resources,
invokes the commit and then freezes its receipt. The callback returns
`TaskCompletion` with the success result and presentation fields; the runtime
still owns status and the one terminal event. Stop accepted before the claim
skips publication; abort/finish cannot supersede an active claim, and registry
reads remain available during I/O. Cleanup failure skips the commit. Commit
failure freezes failed execution and propagates the original exception through
the worker's error policy, including ValueError and process-control exceptions.
Frozen terminal-persistence retries do not repeat completion effects; a sealed
execution without a retained candidate refuses a new commit.

Alternatives rejected: a pre-write cancellation check leaves a race; capability
locks create a second terminal owner; repurposing `terminal_persist` changes
known artifact failure into its distinct pending-durability repair protocol.
The commit adds no worker, cache or retry setting. Verify
`tests/tasks_pkg/test_task_completion_commit.py`, execution-session cleanup,
terminal-candidate retries and the capability's publication fault tests.

## Observe worker exit separately from primary completion

Problem: a report publishes its completed body before best-effort postpasses.
Treating that terminal fact as worker exit loses late events and lets retention
remove their producer. Decision: the existing TaskRuntime worker-entry boundary
counts entered callbacks on the exact carrier and releases that count in an
unconditional finalizer. Optional `workerActive` observations accompany live
poll pages and terminal events; legacy/unmanaged carriers omit them. Primary
status, finishedAt, execution settlement and terminal cancellation rules retain
their existing meaning. This observation owns no second execution lifecycle.

Poll reads activity before capturing events: an exit during that capture must
cause another drain, never an inactive page that omits the worker's last event.
TTL and capacity reclamation share one eligibility predicate that protects
entered callbacks. Normal TTL starts at the later of primary completion and
last callback exit, without rewriting the public completion timestamp. Explicit
owner removal/discard keeps its existing authority. Measurement in
`test_task_worker_observation.py`: with TTL 10, completion at clock 100 and exit
at 200, the carrier survives both TTL/capacity sweeps during execution, survives
at exit, and expires at 211. Tracking adds two scalar fields per managed carrier;
event retention budgets and durable storage are unchanged.

Alternatives rejected: delaying primary success changes artifact availability;
paper-local worker flags duplicate the shared entry boundary; a fixed grace
period cannot prove exit. The report observer follows activity with its existing
push/poll cadence, fences stale pages against accepted event sequences and
reconciles the canonical body separately from legacy addenda. Verify the worker
observation, report publication and compiled report-runtime specifications.

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

Decision: `tofu_workers/executor.py` never holds the scheduler condition across
`Thread.start()`. Registration and startup reservations share one condition
hold. New pending items remain provisional and non-consumable until their own
physical startup succeeds (ownership rules below; targeted wake-ups:
[modules/task_engine.md](modules/task_engine.md)).
A start/constructor exception can re-reserve once while that same item/Future is
still owned and the executor remains open and healthy. Final refusal removes
and cancels the item before reporting rejection, retaining both startup causes.
Completion, cancellation, Future callbacks, snapshots and retirement
bookkeeping never wait for a physical start. Worker-exit bookkeeping happens
first; only replacement creation starts, outside the condition.
`shutdown(wait=False)` fences promptly, while joined
shutdown releases the condition while awaiting startup resolution and then
joins the remaining physical/retiring carriers. Initializer failure or queued
cancellation cannot resurrect an item during startup commit. Abandonment commits
quarantine only after replacement startup, without adding active work back if
the original job completed meanwhile. Resize retains its original idle-hint
rollback. Resident metrics count registered live carriers; `startingWorkers`
separately exposes unresolved startup reservations, including zero.

Problem: one startup lock serialized every physical start, so a submitter with
no free carrier waited behind another submitter's whole startup transaction,
including its `Thread.start()`; the serving loop's Push frame reads
(`_QueuedPushEvent.decode` → `JsonFrameStore.read`) stalled 5–10 s at that
lock while a frame publisher held it. Decision: each
submitter that needs a carrier reserves and starts its own; abandon/resize keep
a control lock only among themselves. Committed (started) carriers cover
admitted demand only. Provisional jobs and abandon replacements are covered by
their own owner's reservation, and nobody counts another owner's unresolved
start. A committed-carrier surplus admits a job in its registration hold. An
owner commits its carriers and admits its job in one hold; a reserved carrier
waits for that commit before it claims or retires, and retention still counts
provisional demand, so admitted demand never exceeds committed carriers.
Alternatives rejected: dropping the lock while counting other owners' starting
threads as coverage strands an admitted job when that start fails
(`test_admission_never_relies_on_another_owners_unresolved_start`); comparing
committed carriers with all registered demand makes each concurrent submitter
also start carriers for the others (benchmark below: 4–27× more starts per job
and 65–95 % fewer producer jobs); hiding loop reads behind another executor or
a starter thread moves the same wait one hop or adds a resident owner.
Measurement (synthetic, Python 3.12, this host): a submitter behind a blocked
start waited 2000.7 ms (fixture bound) vs 0.46 ms after; the loop-side Push
frame read 3000.8 vs 0.48 ms. Eight producers plus a loop-like probe, every
start delayed 5 ms: probe submit p50/p99/max 10.0/16–21/31–37 →
0.05/6.0/7.1–8.6 ms and producer jobs ≈7.1k → ≈14.1k in 3 s; with 50 ms starts
100/151/302–352 → 0.03/51/54 ms and 2.2–3.2k → 8.5–11.0k jobs. A submitter
still pays its own start when no committed carrier is free, so physical starts
rise (≈76 → ≈230 per 1k jobs with 5 ms starts) and the surplus retires after
its job. Stress (12 submitters, 8 %/4 % early/late start faults, cancellation
during startup, 137k jobs; 76k without faults) left no stranded Future, leaked
carrier or accounting violation. Fault-injection specs that expect a refusal
must fail the item's start and its one recovery attempt.

Problem: one transient physical startup failure could terminate an otherwise
productive native task or discard one independent search result. Decision:
recover at this shared unpublished admission boundary, with no sleep, extra
queue, executor or handler retry. Already-live carriers from a late start remain
usable. Cancellation, shutdown and initializer failure fence recovery; control
exceptions propagate. The task's original Future and copied context remain the
same, so approval prompts and writes cannot be replayed by startup recovery.
The optional `tofu_executor_startup_recoveries_total` counter records bounded
`accepted`/`failed`/`cancelled` outcomes; observation failure cannot change the
execution outcome. Verify worker-start recovery/transaction tests, native
approval and installed parallel scenarios, plus independent package fan-out.

Alternatives rejected: merely dropping the condition around start lets a worker
execute a provisional item after submission later reports failure; a new starter
worker/queue adds ownership and shutdown complexity; an active quota or sleep
retry would suppress the machine-scaled demand behavior instead of fixing the
critical section. The idle hint remains an idle hint: a 32-way rendezvous with
hint 1 still runs all 32 jobs. Existing quarantine, callback notification and
independent packaged-helper behavior remain tested. No durable data, timer or
worker pool is added. Three bounded lifecycle sets and the per-reservation
commit events hold references only until startup or physical cleanup resolves;
their cardinality follows the executor's existing carrier demand rather than
introducing a second thread budget.

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

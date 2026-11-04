# Task event delivery


Managed root execution and ordinary event persistence use separate lifetimes
under [`task_event_delivery_v1.json`](../contracts/task_event_delivery_v1.json).
Independent task lanes start on demand through the shared worker executor.
`TOFU_AGENT_WORKERS` sizes idle reuse; legacy item/byte constructor hints and
submission timeouts grant no admission quota. Each task retains one FIFO and
one terminal receipt, with commit before push and hot replay. Idle pumps retire;
a drained lane keeps its newest detached structural source for reuse until a
structural event replaces it or the lane ends. Shutdown releases those sources
and joins pumps and physical carriers before runtime-state/storage teardown.
Standalone interpreter shutdown invokes this same coordinator close through
`_shutdown_sync_compat_pools` before joining non-daemon workers, so a transient
persistence wait cannot strand the process or its command group. Unacknowledged
evidence stays in its existing lane/recovery debt; no queue, timeout or extra
shutdown worker is introduced. Verify
`test_sync_compat_interpreter_exit_closes_event_delivery_before_join`.

Detached structural graphs are charged once through their last queued,
in-flight or lane-retained reference; each event also reports its private
frame/text residency.
These are observations, never permission to reject snapshots or wait for a
slot. Python callbacks and their detached inputs stay owned until settlement
or proven authority revocation. Canonical event bodies can use the storage
batcher's RAM/file overflow; the coordinator adds no durable store or cursor.
Progress batch frame/count bounds split RPC composition without limiting the
total accepted stream. Explicit owner cancellation and shutdown reject new
submissions; genuine unstarted worker failures retain that lane's evidence for
repair, while published siblings keep their lifetimes.

Failed heads prevent their own followers from overtaking; shared transient
classifications wait with capped backoff and no task-attempt ceiling. Ordinary
memory admission stays open, with the original head and its followers retained.
New events neither clear that failure nor start another retry owner.
The first persistence attempt freezes its batch shape through failed/lost-ACK
repair; only never-entered tails can absorb newly accepted progress frames.
Non-transient errors, revoked authority, cancellation and shutdown still refuse
admission. Repair and terminal visibility reuse the existing terminal
maintenance/eviction fence.

The read-side `pushWithheld` signal includes an executor that has exited while
`_terminalPersistencePending` still retains its result. An in-memory terminal
status is not a durable Turn settlement. Problem: filtering out exited workers
hid the failed FIFO behind a generic waiting placeholder. Decision: retain the
storage warning through terminal persistence debt and clear it after successful
settlement; stale markers on fully settled tasks remain ignored. Inferring
completion from worker exit would publish uncommitted results.

Live event preparation retains diagnostic evidence when a large structural
patch accompanies it. Problem: a duplicated 8 MiB estimate discarded the inline
event even after native storage stopped enforcing that quota. Decision: the
storage authority and selected transport decide actual admission; single and
batched producers add no legacy byte estimate. Existing typed refusal handling
remains for genuine backend errors. Raising the estimate or predicting native
envelope growth in the producer would create another drifting limit. The large
structural-frame regression verifies the canonical diagnostic event, exact
conversation replay, carried task event and completed settlement together.

Problem: retry exhaustion previously turned a temporary write outage into a
producer admission error that aborted the managed task on its next progress
event. Decision: retain the transient failure as persistence debt while still
accepting ordinary owned memory events. Alternatives rejected: clearing the
failed head permits publication to overtake; retrying on every submission
restarts a storage storm; admitting permanent failures hides invalid evidence.
The production-entry fault test accepts 64 exact progress events with storage
unavailable, retains 284,386 accounted bytes, and submits the later 63 events
in 5.37 ms on Python 3.12.13. Recovery preserves detached history and exact
sequence/content and releases pending bytes. This measures producer acceptance
under an injected outage, not database throughput or crash durability of RAM.
Explicit durability checkpoints, response-requiring events, consumption receipts
and terminal events drain earlier admitted work before their authority action.
Sampled post-tool-round recovery diagnostics remain queued without draining;
`force` preserves tool-only evidence independently of the explicit wait option. A
provider EOF only closes its receipt span, not the task FIFO. Child and Flow
compositions keep their own existing authority boundaries; a carrier handoff drains root work before changing Turn identity and cannot reopen the root lane.

`tofu_task_event_delivery_*` gauges expose pending items/bytes (bytes include each
lane's retained structural source), active and resident
workers, idle reuse hints, failed lanes, peak residency and maximum observed lag;
counters distinguish accepted/completed callbacks, committed public events,
retries, failures and revocations. Legacy quota capacities and admission waits
report zero. Metrics reads do not create
workers. Task-result `eventDelivery` metadata records the bounded accepted/
completed count and committed visibility cursor. Shutdown stops new submissions and
drains before push/runtime-state/storage teardown; incomplete drain is explicit.

`benchmarks/task_event_delivery.py` is the repeatable controlled measurement:
64 root progress frames, 1 ms simulated work per frame, three samples, exact
content/sequence digests and persist-before-push checks. Against frozen
pre-batching delivery/event modules on Python 3.12.13, a 20 ms injected
per-transaction delay reduced median full drain from 1,286.96 to 102.02 ms
(12.6x), with 64 versus 5 transactions. At 2 ms it fell from 133.84 to 76.16 ms
(64 versus 36 transactions). Producer time at 20 ms remained 71.74 versus
72.45 ms: it was already asynchronous. The zero-delay control was 73.68 versus
74.81 ms, so batching has small measurable overhead. This sink excludes real
storage preparation, per-row engine work and LLMs; it measures backlog
amortization, not whole-system throughput. Raw runs stay outside the repository.

Problem: long tasks with stalled storage exhausted admission because each
progress frame charged the same detached history again, then resource gates
blocked execution. Decision: account shared structure by retained references
without item/byte admission, retaining explicit cancellation/shutdown gates.
Alternatives rejected: enlarging queues or timeouts hides the amplification;
dropping/coalescing structural facts weakens durable replay. Local delivery
failures bypass model retry/fallback and classify as `event_delivery_failed`,
preserving the first exception rather than replacing it with a later phase error.
The controlled production-entry regression queues 64 progress frames behind a
blocked sink with 100 detached tool results (Python 3.12.13). Its charged peak
is 1,295,483 bytes versus 66,113,033 bytes under the former per-frame accounting
(51.0x lower). Exact 0..63 commit order and immutable history are checked;
this measures retained-object budget accounting, not whole-process RSS or real
database throughput.

Problem: a second projection CAS race escaped the single immediate rebase and
poisoned the event lane, terminating a long task as `server_busy`. Decision:
every stale task projection invalidates its cached baseline. After the immediate
refresh, `ProjectionRebaseRequired` grants only the existing persistence owner
permission to rebuild that same event against a freshly checked live attempt.
Its exact carried event and frozen source remain owned, rejected deltas bypass
coalescing on retry, and command identity includes the projection base revision.
Ordinary editor conflicts never gain this local replay-safety marker. Interactive
and terminal barriers use the same persistence wait; shutdown retains original
debt and real revocation still stops publication. Alternatives rejected: raising
the rebase count still fails under contention; retrying the old patch cannot
succeed; weakening CAS or restarting the model loses ownership or repeats effects.
Both SQLite and TofuDB event-delivery tests inject repeated external writes and
check exact-once commit/push and preservation of external projection fields.

Problem: a permanent direct-append refusal could carry an earlier batch timeout
in its exception context and be misclassified as transient forever. Decision:
the shared storage classifier uses the nearest typed storage verdict, including
the producer-owned `ProjectionRebaseRequired` marker. Untyped durability wrappers
still expose their cause. Alternatives rejected: searching for any historical
timeout ignores the authority's latest verdict; retrying every conflict cannot
repair ownership or immutable payload violations. Specs:
`tests/storage/test_storage_retry.py` and the shared persistence-wait suite.

Problem: draining after every sampled round checkpoint coupled the next model
round to optional recovery writes; a backlog also rewrote the same projection
per progress frame. Decision: sampled round diagnostics opt out of synchronous
draining. Queued, contiguous `delta`, `program_output`, and `tool_progress`
frames with one detached structural identity share a bounded batch and retain
only its newest projection inputs. All canonical wire frames, owner contexts,
and publication callbacks stay exact. Structural events and entered sinks end
batches. Alternatives rejected: dropping progress frames loses cold replay;
a second durable spool creates another authority; removing interaction/terminal
barriers weakens recovery and consumption ordering.

`scripts/gen_task_event_delivery_contract.py` derives shared Python/Rust bounds.

## Pre-request checkpoint never awaits diagnostics

Problem: every root model request first drained the task FIFO, so a paid call
waited for its own Request Inspector snapshot. Logged root rounds (preparation
>= 1 s, 2026-10-27 to 10-28 15:51) spent `barrier` p50 553 ms, p90 2.45 s, mean
974 ms (368 s over 378 rounds) with one pending event in 220 of 299 records;
`turn.event.record` averaged 350 ms over 18,995 commits. Slow (>1 s) pre-request
barriers alone totalled 687 s in 5.6 hours (266 waits, p50 1.93 s, max 12.5 s).

Decision (owner decision 2026-10-28: request diagnostics must not block a paid
model call): `persistence_wait.checkpoint_model_dispatch` records
`model_in_flight`, runs the domain checkpoint sink synchronously and returns
without awaiting accepted events. The snapshot and every other frame stay in the
same ordered FIFO and commit during the request; the unchanged
`tools_in_flight`, `ready` and terminal barriers still drain them before any
external effect, the next paid request or terminal commit. A lane already
holding a failed head still blocks for repair and refuses dispatch when repair
fails. Stop never cancels accepted evidence: later frames are refused and the
terminal frame commits after the snapshot. Request start is still fenced by
execution-budget admission (`reserve_model`/`mark_model_dispatched` check Stop).

The sink is not diagnostics and stays a write-ahead. Swarm/Flow children
(`SubAgent._save_checkpoint`) durably record `checkpoint_phase=model_in_flight`,
`actuals_complete=false` and the round count before dispatch, so a restart
restores usage as incomplete and finite token budgets refuse
(`execution_token_usage_unknown`) instead of under-counting a lost paid call.
Evolution publishes its working checkpoint, whose failure admits no model/tool
effect ([run continuity](RUN_CONTINUITY.md)). Paid-attempt journals (dispatch
intent, account frontier) are written by the execution budget at dispatch and
never relied on this flush. The swarm-lane `barrier` (p50 219 ms, zero pending
events in all 556 records) is that sink; `barrierMs.sink` names it.

Problem: that sink was `swarm.agent.save`, a rewrite of the child's whole
transcript before every paid call (`barrierMs.sink` mean 409 ms per swarm
round), so the bytes written per run grew with the square of its length.
Decision: when the transcript and every other result field still equal the
run's last acknowledged full save (the normal case: the previous round's
`ready` save already holds them), the write-ahead is
`swarm.agent.checkpoint.mark`, a constant-size overlay of the compact result
fields (`checkpoint_phase`, `actuals_complete`, token usage, cost, elapsed
time, rounds) declared by `checkpointMarkerResultFields` in
`contracts/tofudb_ir_v1.json` and `lib/swarm/persistence_contract.py`. Both
authorities overlay it on every agent read (`swarm.session.get`,
`swarm.agents.page`, `swarm.agent.state.get`) and the next `swarm.agent.save`
retires it in the same transaction, so restart recovery reads exactly what a
full save would have written: usage restores as incomplete and finite budgets
refuse. Every other checkpoint (a run's first, a transcript changed by
injection or compaction, any other changed field, every `tools_in_flight`,
`ready` and terminal checkpoint) is an append or a full save, as recorded
below; a mark that finds no agent row answers `marked=false` and the child
saves in full. Persistent state gains at
most one compact marker row per agent (SQL `swarm_agent_checkpoint_markers`,
folded into the agent by the offline TofuDB import) or one optional field in
the native compact agent state. `benchmarks/swarm_checkpoint_rounds.py` (200
rounds, final transcript 299,069 tokens / 1.54 MB, strict durability, each arm
with its source-matched engine) measured the pre-request write-ahead at p50
40.4 ms, mean 41.3 ms (rounds 181-200: p50 66.8 ms) before and p50 8.0 ms, mean
11.6 ms (8.4 ms) after on local disk; with the authority on the DolphinFS
volume, p50 30.4 ms, mean 30.6 ms (55.5 ms) before and p50 2.0 ms, mean 2.1 ms
(2.3 ms) after, while engine writes during those calls fell from 73.3 MiB to
0.94 MiB per run (about 1 KiB per marker).
Alternatives rejected: a second log of message deltas for the pre-request
write-ahead (compaction-aware truncation and a merge on every read in both
engines, for the same saving); dropping the write-ahead (a crash would restore
usage as complete and a finite budget would under-count the lost paid call);
an asynchronous sink (the paid call could start before the record is
durable). Specs: `tests/swarm/test_swarm_checkpoint_marker.py` (crash after
the marker, budget refusal after restart, ordering with later saves),
`test_swarm_checkpoint_marker_overlays_until_the_next_full_save` in
`tests/storage/test_storage_authority_sql_contract.py`, the native
`swarm::tests::checkpoint_marker_overlays_compact_state_until_the_next_full_save`
and the swarm checkpoint-marker script in
`tests/tofudb/test_tofudb_differential.py`.

Problem: after the marker every `tools_in_flight`, `ready` and terminal
checkpoint was still a `swarm.agent.save` of the whole transcript. The child
deep-copied it twice (comparison base and receipt freeze), and every attempt
re-walked and re-encoded it, so a long worker re-sent megabytes per round and
held the GIL for it (the 2026-11-01 server profile attributed 203 GIL samples
in 240 s to `SubAgent._checkpoint` and its copies, plus JSON encoding in the
storage client's request path).
Decision: a child checkpoint is the smallest write that leaves the durable row
exactly as a full save would: the marker above when only compact fields
changed, `swarm.agent.append` when the transcript only grew, otherwise a full
save. `swarm.agent.save` takes an optional caller `checkpoint_id` (fresh per
write) that the authority keeps as the agent's append base and echoes;
`swarm.agent.append` carries that base id and its message count, the new
messages, a new `checkpoint_id` and the bounded result, status and rounds. The
authority applies it only while the stored id and message count equal the
base, then holds exactly a full save of the extended transcript (marker
retired; delivery, role and objective kept); otherwise it answers
`appended=false` and changes nothing. The child keeps a detached copy of what
its last acknowledged write stored and appends only when the live transcript
still starts with that copy, unchanged (C-level equality, no hashing). The
first write of a run, a compacted, edited or shortened prefix, a refused base
(any other writer, such as rehydration or a duplicate worker, even at the same
message count), a save the authority did not echo, and `unknown_operation`
all save in full, so restart recovery reads what a full save would have
stored. Payloads are frozen by encoding them once
(`lib/storage/protocol.py` `FrozenPayload`); both clients splice those bytes
on every retry of the receipt instead of deep-copying and re-encoding. SQL
keeps the base in `swarm_agents.checkpoint_id` (schema 70, constant default);
native keeps it in the agent core (omitted when empty). Both apply an append as
a read-modify-write of the core inside the command transaction, so reads,
paging, response bounds and the offline import are unchanged; an append that
would outgrow the bound a full save meets (the native agent core bound, the
SQL single-agent page under one storage frame) is refused unchanged. Like every
schema change, schema 70 is also the storage.v2 Hello schema id, so a native
engine that predates it is refused at negotiation and must be rebuilt; an
authority that negotiates but lacks the operation answers
`unknown_operation` and the child saves in full. A component measurement of
the real `SubAgent._checkpoint` -> `StorageV2Client` -> wire encoder path
(about 21 KB of new messages per round, three checkpoints per round,
authority work excluded) sent 2,162,792 and 2,183,420 bytes for the round-100
`tools_in_flight` and `ready` checkpoints at 19.7 and 20.1 ms client CPU
before, and 1,877 and 21,304 bytes at 0.15 and 0.16 ms after, flat from round
10; a 100-round run sent 218.66 MB in 2.1 s of CPU before and 2.35 MB in
59 ms after. Encoding once alone already cut round 100 to 2.7 ms: the receipt
freeze was a `copy.deepcopy`, and the floats in every result sent each save
through the stdlib JSON encoder. The authority still rewrites the core per
append, so its written bytes stay proportional to the transcript:
`benchmarks/swarm_checkpoint_rounds.py` (100 rounds, 683,160-byte final
transcript, source-matched debug engines) wrote about 156 KiB per round-100
`tools_in_flight` and `ready` checkpoint in both arms.
Alternatives considered: a count-only CAS (a rehydrated or duplicate writer
at the same count would splice two transcripts); a digest of the durable
prefix (hashing the whole transcript every round); a segmented delta log in
both engines (proportional engine writes too, but every read, page bound,
deletion and the offline import would merge segments; the operation contract
permits it later without a client change); a threshold or flag choosing when
to append (a tuning knob; the exact base check decides). Specs:
`tests/swarm/test_swarm_checkpoint_append.py` (pipeline appends and restore
equality, compaction and in-place rewrites, another writer, authorities
without appends, and SQLite, PostgreSQL and native authorities), the
marker spec above, `test_swarm_agent_append_equals_a_full_save_and_refuses_any_other_base`
in `tests/storage/test_storage_authority_sql_contract.py`, the native
`swarm::tests::appended_checkpoints_read_exactly_as_full_saves_and_refuse_any_other_base`,
the swarm append script in `tests/tofudb/test_tofudb_differential.py` and
`test_swarm_command_replays_the_bytes_encoded_once_for_its_receipt` in
`tests/tasks_pkg/test_agent_persistence_wait.py`.

A crash during the request can lose the undurable FIFO suffix: the snapshot
and every later frame, including `model_request_start`. The durable log stays a
prefix, so the `request-evidence` invariant still holds, the Request Inspector
lists earlier rounds and answers the lost round as not found (not a read error),
and recovery reads Turn/task projections, sink checkpoints and budget journals,
never request snapshots. The raw provider archive was already asynchronous.

Alternatives rejected: keeping the awaited barrier (owner decision); a barrier
timeout (still waits, and changes durability on expiry); dropping the snapshot
(loses diagnostics); making the sink asynchronous (a crash could then forget a
paid request and refresh a finite budget); draining at the next round's start
(same serial latency).

Measurement: `benchmarks/model_dispatch_barrier.py` runs both boundaries over
the production event entry with a slow authority stub (6 rounds, 2 alternating
samples, 150 ms preparation, 1.5 s simulated request with 6 deltas; Python
3.12.13). Identical ordered logs are required before clocks count. Median
pre-request stall awaited/deferred: 0.5/0.0 ms at 0 ms per transaction, 100/0 ms
at 100 ms, 550/0 ms at 350 ms, 1,850/0 ms at 1,000 ms; round wall time fell by
0.3, 101, 701 and 1,001 ms. When storage outlasts the request, part of the wait
moves to the tool barrier (2,499 to 3,348 ms at 1,000 ms per transaction) but the
round still gains one transaction. These are scheduling observations against a
stub, not database or provider throughput. Specs:
`tests/tasks_pkg/test_pre_request_checkpoint.py` (no wait, order, write-ahead,
failed head, Stop, lost process) and `tests/meta/test_model_dispatch_barrier_measure.py`.

Problem: a 45 s live GIL profile attributed 23.6% of all server GIL time to
`copy.deepcopy`, almost all of it detaching event wires and structural projection
inputs on the producer thread, and a further share to re-serializing the live
timing trace once per byte guard on the persistence worker. Decision: detached
snapshots use `lib.utils.fast_deepcopy`, which copies exact builtin dict/list
trees inline and delegates every other type to `copy.deepcopy` with the shared
memo, preserving shared/cyclic identity and keeping source containers alive for
the duration of the copy. `compact_trace_document` measures once and returns
when already within 96 KiB. Measured on Python 3.12.13: a 120-round structural
capture 4.70→2.89 ms (identical digest); a 128-row live trace 2.97→0.97 ms
(identical output; 1,500 randomized documents including 958 compacted cases
matched the previous implementation exactly). Alternatives rejected: sharing
live task structures instead of copying would let executor mutation change
accepted frames; pickle/JSON round-trips change tuple, set and key types.
History rows that can no longer change are now shared as immutable settled
rows instead (see "Settled projection history" below); live rows are still
copied. Event frames and Inspector snapshots use that sharing copier too, with
the same copy semantics for unsettled structure (see "Frames and history rows
visited once" below).

The default projection window remains 300 ms. An eager managed-root experiment
reduced full-application text-to-DOM p95 from about 300 ms to 27–34 ms, with the
same 42 storage commands, but roughly doubled measured process CPU and observed
native WAL-buffer writes. Decision: retain the default and explicit
`TOFU_TURN_DELTA_RECORD_MS` override pending lower write amplification and loaded
concurrency evidence. No additional worker, timer, cache or cadence policy is
introduced. The repeatable browser scope, raw counter limits and rejected
alternative are documented in `TURN_RENDERING_PERFORMANCE.md`.

## Performance anchors

**Problem:** a timestamp around a persistence callback mixed producer/barrier
delay, FIFO waiting, retries and client work. Merged frames inherited the newest
callback's timestamp, hiding older frames' queue time. Success-only timing also
hid failed attempts that eventually committed.

**Decision:** the additive `performanceProfile` policy in
`contracts/task_event_delivery_v1.json` owns anchor boundaries, populations,
sampling, privacy and retention. `event_delivery_profile.py` records each sampled
frame's successful FIFO admission, first batch dispatch, every persistence attempt
and inter-attempt gap, client ACK, visibility-cursor update, publication callback
and flush return/refusal. Sample every N newly queued batches; all merged frames
in a selected batch keep their own arrival clocks. Flush observations cover all
barrier calls while enabled. Failed attempts use seven fixed outcome labels;
unknown error text never becomes a metric label. Revocation releases sampled
tails; explicit repair retains the original acceptance and first dispatch.

Enable with `TOFU_EVENT_DELIVERY_PROFILE_EVERY=1` before coordinator creation;
zero/default disables clocks and sample allocations. Existing `/metrics` admin
authorization applies. Fixed stage histograms expose sampled count, elapsed sum
and maximum; fixed outcome counters distinguish successful and failed attempts.
The snapshot retains 32 anonymous completed summaries and up to 32 oldest pending
summaries, with active counts, invalid clocks, observer errors and metadata size.
Per-frame clocks follow already-owned pending work rather than a new queue or
quota. Observer failures cannot revoke publication or retry an acknowledged write.
Collection releases the coordinator lock before reading detailed profile metadata.

These are process-local monotonic **wall clocks**, not CPU clocks. A persistence
return is a client ACK, not the native fsync time; callback return is not SSE or
DOM visibility. `persistence_wall` includes attempts, retry/control waiting and
repair; `accept_to_publish` also includes FIFO and publication. Populations differ
between frames, batches, attempts and barriers: do not add their sums or P99s.
Log2 histogram boundaries are approximate; exact workload latency samples still
own capacity admission. `metadataBytes` measures reachable Python metadata,
excluding locks, allocator overhead and whole-process RSS.

`benchmarks/storage_write_pipeline.py --delivery-profile-every 1` exercises these
anchors through real storage. Add `--native-profile-every 1` to retain the existing
native FIFO/mutex/authority/WAL/VFS attribution; native dropped samples remain
visible and cannot be treated as complete coverage. Source digests are frozen
before the workload and checked afterward. Wrapper preparation, primary calls,
coalesced replay and terminal-prefix barriers have separate clocks; the old
wrapper `fifo_wait` is now truthfully named `producer_to_storage_attempt`.
Push timing uses one callback-entry clock captured before the observer lock.
Profiling or source drift disqualifies formal latency comparison, even when
correctness and numerical latency budgets pass. Use profiling off for admission.

**Alternatives considered:** a clock around the newest callback loses merged
frame waits; an unbounded trace retains user-related metadata; success-only
samples conceal retry stalls; summing nested spans invents elapsed time. None is
used. No worker, storage authority, durable log or durability relaxation is added.

Focused guards: `test_event_delivery_profile.py`, `tests/turn_lifecycle/test_task_event_delivery.py`,
`test_storage_write_pipeline.py`, and `test_conversation_benchmark_acceptance.py`.
Real native SIGKILL/readback, resource observations, failed timing gates and
scope limits live in `tofudb_delivery_anchors_20261013.json`.
The contract caps each batch at 500 frames and 1 MiB of canonical wire payload;
smaller existing storage limits win. Process bytes include raw retained frames,
the latest text snapshot, and shared structure. Queue slots count batches plus
in-flight callbacks; `pending_events`/`peak_events` count frames separately from
`pending_batches`/`peak_batches`. Normal events keep their existing FIFO slots.
A latest frame carries `task_event.preceding_events` in the same existing
`turn.event.record` transaction. Both authorities reject cross-task, non-progress,
non-contiguous or oversized prefixes. Any frame conflict rolls back projection
and all new frames; ambiguous ACK retries retain the exact batch. Peers must
confirm `task_event_count` before any frame can become visible.

The controlled production-entry backlog test retains and replays all 64 frames
while reducing projection transactions from 64 to 2 with a blocked first write.
It covers exact ordered publication, immutable detached history, whole-batch
rollback and lost acknowledgement on SQLite and TofuDB. This measures backlog
amortization and ordered recovery. Ordinary memory acceptance does not certify
durable progress; required durability gates still wait.

Structural frames each keep their own revision and sync event, so they cannot
ride `preceding_events`; a queued run of them shares one
`turn.event.record_batch` transaction instead (next section).

## Queued structural runs persist in one call

Problem: every queued structural frame (phase, round and tool boundaries, tool
results) cost one `turn.event.record` transaction, so a barrier awaiting N of
them waited for N serialized storage RPCs. On 2026-10-31 barriers awaited
19-136 queued events at about 6.8 s per RPC (375 minutes of barrier waits), and
a busy task still waited 10-69 s per round afterwards. The batch command and
its producer `turn_lifecycle.record_task_events` (contract `structuralBatch`;
engine decision in
[`TOFUDB_EXECUTION_FRAMES.md`](TOFUDB_EXECUTION_FRAMES.md#batched-structural-events-2026-11-01),
specs in `tests/turn_lifecycle/test_turn_event_record_batch.py`) existed, but
no delivery path called them.

Decision: a FIFO item may carry a batch port: a `batch_scope`, a `batch_entry`
that freezes the item's entry and a `persist_batch` that persists a FIFO run of
entries in one storage call. A pump that takes a lane whose head has not
started claims the head and every following unstarted item of the same scope
already queued (`_claim_run`). It never waits for more arrivals, and each
claimed item is marked started so none absorbs later progress. At the run's
first attempt the entries freeze once, in FIFO order, and the head's
`persist_batch` persists them through the existing `wait_for_persistence`.
After it returns, every frame of every member publishes in FIFO order and the
run settles as one unit: each member is released once and completed counts
advance together. A run of one persists through its own port, so single
frames, merged progress batches and their coalescing are unchanged; inside a
longer run a progress batch is carried as its own Turn revision rather than
coalesced. Items
without a port (presence heartbeats, recovery checkpoints, rows-only frames,
every child Inspector evidence item) end runs and persist alone. Barrier-class
frames never queue: they drain the FIFO, then keep their own transaction.

Failure keeps the whole frozen run; it is never split after an uncertain
outcome. A transient failure or lost acknowledgement retries exactly that run
with the same entry objects, so `record_task_events` reconciles the exact rows
and never applies a committed entry twice; arrivals meanwhile form the next
run. A permanent failure retains the run and the push-withheld marker, and
repair retries it before anything behind it. `revoke()` refuses while a run is
entered; revocation releases every member. For barrier cost a run is one
batch: the head's timing record carries its persist calls, storage time and
FIFO-head wait while the members' records stay empty, so `BarrierCost` counts
the run's storage calls once.

The producer (`manager/_events.py`) attaches the port at acceptance to a
conversation-attempt frame that is neither barrier-class nor a post-settlement
observer. Its entry is exactly the carrier
the single path would send: the same projected wire (projected once, in FIFO
order), sequence, detached projection source and preceding progress rows.
Outcomes map back with the single path's rules: `carried` commits; a record
applied without its row appends that row standalone; a rejection of a live
attempt this task still owns raises `database_conflict` and retains the run; a
stale attempt plants the cooperative abort and revokes the run. Rows that an
earlier chunk of a revoked run committed stay durable and replayable but are
not pushed, as after an ambiguous acknowledgement on the single path. The
chunk size comes only from the storage event budget and the native
refusal-and-halve inside `record_task_events`; no knob is added.

Alternatives considered:
- Wait briefly for more arrivals before claiming: a debounce interval is a
  knob, and the barrier already awaits everything accepted before it.
- Let the barrier persist the backlog itself: a second persistence owner that
  races the pump's in-flight head and its failure and repair state.
- Publish a failed run's committed prefix and retry only the rest: after an
  uncertain outcome the prefix is unknown, and the batch producer already
  reconciles exact rows on retry; a split adds a second retry shape.
- Charge every member's timing record: a barrier would count one storage call
  once per member.
- Batch rows-only lanes (child Inspector evidence, attempt-less tasks,
  post-settlement observers) through `event.append_batch`: the shared event
  batcher has no acknowledged multi-row append, and a direct batch call would
  fork `append_persistent_event`'s fallback, attempt fence and cache
  invalidation. That batcher already coalesces concurrent appends across
  tasks; not done.

Barrier class has one owner. Problem: the producer derived its barriers from
the event registry, while `record_task_events` refused a hand-kept list that
named two unregistered types (`ask_human`, `approval_request`) and missed the
registered `approval_required`. A second producer filter kept refused frames
out of batches, and `approval_required` recorded as an ordinary projection
update. Decision: `event_delivery.BARRIER_EVENT_TYPES` (task-replay terminal
types, registered response-requiring events, `background_command_inject`,
`tool_effect`) is the only list. The producer barrier (judged on the raw frame
and on its delivered wire) and the batch call's refusal both use
`is_barrier_event`, so the second filter is gone; the Turn derives
`interaction_request` from the same registry flag. Alternatives considered:
keeping the second filter hides a divergence instead of removing it; a
barrier flag on `EventSpec` would have to register the Turn-only `aborted`
terminal and add a second classification for kinds the existing flags
already define.

Measurement: `benchmarks/task_event_delivery_contention.py --backlog 16,64,136
--authority-ms 50` queues N structural frames (phase, tool start and tool
result with growing tool rows) through each tree's production `append_event`
and `turn_lifecycle` record code while the first authority transaction is held
until the barrier starts, against a Turn authority stub that sleeps 50 ms per
transaction. Five alternating fresh
processes per arm compared this tree with the base it changes (e9e2e8247,
exported), Python 3.12.13, host load average 2-12. Median barrier wait,
before → after (authority transactions):

| CPU-bound threads | N | Before | After | Ratio |
|---|---:|---:|---:|---:|
| 0 | 16 | 810 ms (16) | 57 ms (1) | 14.1 |
| 0 | 64 | 3,251 ms (64) | 123 ms (2) | 26.4 |
| 0 | 136 | 6,915 ms (136) | 180 ms (2) | 38.5 |
| 4 | 16 | 1,628 ms (16) | 321 ms (1) | 5.1 |
| 4 | 64 | 6,792 ms (64) | 364 ms (2) | 18.7 |
| 4 | 136 | 15,017 ms (136) | 497 ms (2) | 30.2 |

The pump took the head after some frames had already queued, so each backlog
committed in one or two runs (largest run 16, 38-47 and 89-113 frames), and
barrier cost reported one or two persist calls instead of N. Both arms spend
about 0.6 ms per frame outside the stub's sleep (record preparation and
publication); the batch removes the per-frame storage wait, and with busy
threads GIL contention makes up most of what remains. This is a scheduling
observation against a stub: it excludes real engine work, which
`benchmarks/turn_event_batch.py` measures per batch (native TofuDB K = 16: 157
→ 37 ms idle, 3.93 s → 196 ms contended).

Specs: `tests/tasks_pkg/test_task_event_batch_delivery.py` (one run behind a
blocked head, published after commit with memory released; exact frozen retry
after a transient failure or lost acknowledgement while arrivals form the next
run; permanent failure, withheld marker and repair order; boundaries;
revocation; `accept_after_stop`; progress frames inside a run; the batch call
refuses exactly the registered frames the producer keeps as barriers; an
interaction frame drains the queued run, then keeps its own transaction) and
`tests/turn_lifecycle/test_task_event_batch_delivery.py` (production entry on
SQLite and native TofuDB: 63 frames behind a held head in one transaction per
budget chunk, native halving included, with every row, revision, sync event and
post-commit push; a lost batch acknowledgement reconciled without a resend;
stale attempt; live rejection and repair; progress inside a batch; a terminal
barrier after a queued run).


## A child's Inspector evidence never blocks its paid call

Problem: a Swarm, Flow, Goal or Evolution child invocation persists only its
Request Inspector snapshots, under its own Inspector stream
(`<parent>#agent:<child>`); every other child event reaches the parent through
the required sink. `deliver_invocation_event` appended each snapshot and awaited
its acknowledgement before that handoff, so the request snapshot sat inside
`build_bodyMs.snapshot` on the paid call's path, and the post-tool `state`
snapshot before the next request. The pre-request barrier change measured these
child appends at a mean of 241 ms over 1,973 writes. Retained swarm-lane
`[RoundPrepTiming]` records (5,517 rounds with preparation >= 1 s, 2026-10-27 to
10-28, logged before `build_bodyMs` existed) show `build_body`, which contains
the append, at p50 363 ms, p90 1,244 ms and mean 654 ms.

Decision (owner decision 2026-10-28, child half: request diagnostics must not
block a paid model call): `deliver_invocation_event` detaches the snapshot,
accepts it into the child's ordered evidence lane
(`event_delivery.invocation_evidence_lane`, a `DeliveryLane` on the shared
executor) and hands the event to the sink at once. The lane projects the
snapshot delta once, in FIFO order, and replays that frozen append through
transient pressure; a refusal stays as its failed head, and no later snapshot
overtakes it. `flush_task_events` and `task_event_backlog` cover this lane, so
the shared checkpoints apply unchanged: the pre-request boundary never waits,
but a lane already holding a failed head repairs it once and refuses the paid
call when repair fails, exactly like the root lane. Because the lane holds only
diagnostics, the child's `ready` boundary (before its next paid request) follows
the same rule. Tool intent drains the lane before the round's effects, which
bounds it to about one round of snapshots, and `run_child_invocation` settles it
before the result is used: drain, or record the authority verdict as the
invocation's delivery failure and release the remainder. Stop never cancels
accepted evidence: submissions after Stop are accepted, and the settle waits
without a cancellation check. The domain write-ahead (`SubAgent._save_checkpoint`
with `model_in_flight`) stays synchronous.

Lane items carry sequence -1. The child's event cursor is not a public replay
frame, so these items neither advance visibility nor plant the push-wedge
marker; metrics count them as completed callbacks, not committed public events.

A crash can lose only the lane's tail. The child's Inspector stream remains a
prefix of its accepted snapshots in emission order. The Request Inspector lists
the durable rounds and answers a lost round, or a child with no durable round,
as not found, never as a read error. A parent tool card can still reference a
lost snapshot. Recovery reads the Swarm checkpoint, budget journals and
Turn/task projections, never child snapshots.

Alternatives rejected: a dedicated per-child queue and thread (a second ordered
writer beside the shared FIFO, with its own retry, shutdown and metrics);
awaiting the child lane at `ready` like the root lane (only diagnostics remain
there, and with the constant-size Swarm checkpoint marker above the next paid
request would wait for nearly the whole post-tool snapshot write); draining
only at the terminal settle
(an unbounded queue across rounds during a storage outage); refusing snapshots
after Stop (turns a clean cancellation into an evidence failure).

Measurement: `benchmarks/model_dispatch_barrier.py --lane child` replays child
rounds through the production emitter, evidence port and checkpoints against a
stub append that sleeps W per call (6 rounds, 2 alternating samples, 150 ms
preparation, 1.5 s simulated request with 6 deltas, a request and a post-tool
snapshot per round; Python 3.12.13). Identical ordered evidence logs are
required before clocks count. Before (this tree without the change) the paid
call's pre-call stall, `build_bodyMs.snapshot` plus `barrier`, equalled W:
medians 0.5, 100.6, 241.6 and 1,000.7 ms at W = 0, 100, 241 and 1,000 ms, and
each post-tool snapshot cost W again. After: 0.4, 0.4, 0.3 and 0.3 ms, a 0.5 ms
post-tool hand-off and a 0.0 ms `ready` barrier; round wall time went from
1,653.3, 1,853.7, 2,135.6 and 3,653.8 ms to 1,652.7, 1,652.9, 1,652.7 and
2,001.5 ms. At W = 1,000 ms two appends per round outlast the 1.5 s request, so
the tool barrier absorbs the remainder (median 348.8 ms) and bounds the queue.
With a 219 ms domain sink per checkpoint (the swarm-lane p50 before the
checkpoint marker) at W = 241 ms, the pre-call stall fell from 460.6 to
219.7 ms, now the synchronous write-ahead alone, and the round from 2,792.6 to
2,311.0 ms. The emulated former
port (`awaited`) on the new tree reproduces the old clocks within 1 ms. On the
producer thread, detaching a snapshot costs 0.53, 1.60 and 3.19 ms for 0.22,
0.71 and 1.45 MB histories, against 1.79, 5.68 and 11.41 ms for the in-thread
delta projection it replaces, which now runs on the lane. These are scheduling
observations against a stub, not database or provider throughput.
Specs: `tests/tasks_pkg/test_child_evidence_lane.py` (no wait before either
request, one durable order, failed head, Stop, lost process read by the Request
Inspector), the child evidence suites in `tests/tasks_pkg/test_child_invocation.py`,
`tests/tasks_pkg/test_child_evidence_recovery.py`,
`tests/storage/test_child_evidence_recovery.py` and
`tests/goals/test_goal_invocation_integration.py`, and
`tests/meta/test_model_dispatch_barrier_measure.py`.

Problem: a global worker ceiling, queue slots and snapshot bytes delayed or
refused unrelated tasks. Decision: retain only per-task FIFO dependence and
start independent lanes through the shared demand executor. True worker-start
failure retains its unstarted head; pump Futures, callbacks and physical threads
remain joined on close. Alternatives: larger ceilings preserve waiting;
parallelizing a task FIFO loses commit-before-visibility; serializing arbitrary
Python callbacks is not a valid payload-spill mechanism. Measurement: 32 real
callback lanes rendezvoused with idle hint 1 in 12.85 ms, preserving every
commit/publish pair and releasing every pump, thread and charged input. This
measures controlled delivery concurrency, not storage-engine throughput or RSS.


## Child-to-root publication ownership

Problem: a child callback could enqueue a parent replay fact while its captured
publication context still belonged to the child. Disposal then closed the child
fence; the accepted root head repeatedly failed `check_publication`, blocking
followers and the terminal durability barrier without any storage revocation.

Decision: root FIFO acceptance is the ownership transfer. With the coordinator
queue lock acquired, `accepted_publication_context` checks and holds the producer
fence through local acceptance. Closure winning before admission rejects the
frame; acceptance winning retains it even after child cancellation/disposal.
The detached context removes only the child publication fence, preserving all
other context variables (including principal). Single-event persistence,
latest-projection batch persistence, and every progress publication context use
that receiving-owner context. Storage `record_task_event` and append transactions
still enforce the parent's attempt/CAS authority: a genuinely stale parent may
revoke its uncommitted tail; child disposal is not such evidence. Worker-start
failure after acceptance still retains the accepted head for repair.

The existing swarm seam is `SubAgent._on_invocation_event` → `_emit_event` →
parent `on_event` → manager `append_event`. Stream/tool sinks also retain the
producer context until root admission. No swarm callback clears publication
scope: child-owned checkpoints/projections and other compositions must retain
their producer fence. Only managed root FIFO `submit` transfers ownership;
child ingress and Flow ports keep their existing lifecycle boundaries. The
fence protects bounded queue mutation/start publication, never storage I/O,
flush, or observer delivery.

Alternatives rejected: dropping accepted child facts loses durable replay;
classifying child closure as root revocation discards valid followers;
clearing fences in swarm callbacks permits revoked-before-admission writes;
removing persistence checks globally bypasses child-owned deferred authority.
Regression specifications cover pre-admission closure, merged progress contexts,
principal preservation, terminal persistence after disposal, and rejection by a
real settled parent attempt against SQLite authority.


Problem: one shared condition woke every task barrier on each sibling commit.
Decision: barrier waiter queues belong to individual lanes under the same
coordinator lock, with weak keys tying metadata residency to lane lifetime.
Completion, revocation and permanent/startup failures wake the affected lane;
parked pumps wait on their own queue (next section) and shutdown keeps the
shared condition. Cancellation
callbacks keep bounded polling; ordinary barriers wait for notification or
their actual deadline. Alternatives rejected: more workers retain quadratic
wake amplification; moving the state to independent locks permits races.
The controlled 256-lane ABBA measurement reduces wait returns from
44,894/46,329 to exactly 256, with 727/738 versus 226/227 ms full drain.
No worker, durable store or admission limit is added. This is a scheduler
measurement; real native thousand-conversation latency remains above target.
See `TOFUDB_PUBLICATION_LATENCY.md` and the retained waiter benchmark.

## Pump scheduling and coordinator lock scope

Problem: every `_schedule` call broadcast the shared condition, so each
accepted event woke every parked pump, and a pump whose wake-up found nothing
ready retired at once; its Future callback then submitted a replacement for the
next ready lane. Pump jobs were submitted while holding the process-wide
coordinator lock, so a worker start (`Thread.start()` waits for the new
thread's bootstrap, many GIL switch intervals in a busy process) stalled every
producer, barrier and pump of every task. Producers also took that lock only to
read three projection fields of their own lane. A live stall dump showed eight
threads queued on it: agent producers, the text coalescer, a tool barrier and
delivery pumps.

Decision:
- Parked pumps wait on their own queue under the coordinator lock. A lane that
  becomes ready wakes exactly one of them (`notify`), and only when it was
  actually queued. Followers of a running lane, a completing pump re-queuing
  its own lane and completions wake nobody; failures, retries, revocation,
  pump exit and shutdown still broadcast the shared condition.
- A wake-up that finds nothing ready is spurious: the pump keeps waiting until
  its original `idle_timeout` deadline. Retirement at that deadline, surplus
  retirement above the idle-reuse hint and the Future-callback handoff of a
  retiring pump are unchanged.
- `_schedule` only reserves the pumps demand lacks (active plus ready lanes,
  minus pumps and outstanding reservations); its caller submits them after
  releasing the lock. A refused start still retains the unstarted head it
  stranded for explicit repair, and the submitting producer still receives
  `EventDeliveryError`; a refusal whose demand another pump already met
  changes nothing. Shutdown also waits for outstanding reservations before it
  joins pumps and retires the executor.
- Each lane publishes its newest projection inputs as one immutable
  `LaneProjection` (source, charge, identity). Writers swap the whole tuple
  under the coordinator lock; a producer reads it with one reference read and
  takes that lock once per event, in `submit`. Residency accounting stays
  under the lock: a read racing a replacement or release observes the
  previous or the new triple, the outcomes a locked read allowed (the lane's
  retained charge is described under detached projection accounting). A frame
  without a wire transform (wire and event are one object) is detached once
  instead of twice.

No knob is added: the idle-reuse hint and `idle_timeout` keep their meaning,
and demand that lacks a pump still starts one.

Run claim: `_run` claims the head's run under the lock (`_claim_run`, see
[queued structural runs](#queued-structural-runs-persist-in-one-call)),
persists and publishes it outside the lock, then settles the whole run in the
completion block.

Barrier cost: each queued batch records its persist calls, its storage time
(first dispatch to acknowledgement, transient retries and backoff included)
and its wait at the FIFO head for a pump (from acceptance, the previous
batch's settlement or its repair, whichever is later, to dispatch). A claimed
run counts as one batch.
`flush(on_release=...)` reports their sum over the batches one barrier awaited
as `BarrierCost`; the barrier retains only those records, never the batches.
[Logging](LOGGING.md) defines the `[EventBarrier] released` fields.

Alternatives considered: keeping the broadcast and only ignoring empty
wake-ups still wakes every parked pump on every event; a dedicated starter
thread or queue for pump starts adds an owner and a shutdown path where a
reservation count suffices; a lower `idle_timeout` or a pump cap adds knobs and
can leave ready demand without a pump; a per-lane lock for the projection
fields adds a second lock to every coordinator write where an immutable
snapshot needs none; merging each completion with the next claim saves one
lock acquisition per event but folds settlement into the run claim.

Measurement: `benchmarks/task_event_delivery_contention.py` runs 8 lanes of
300 events (3 ms sleeping persist stub, 4 ms think time, a 300 ms pause every
50 events, idle reuse 16) in alternating fresh processes against the tree
before this change (exported from 41c6bdc73), Python 3.12.13 on a shared host
(load average about 26). Medians, before → after:
- No CPU-bound threads (5 + 5 samples): pump jobs 1,331 → 8, retirements
  during the workload 1,331 → 0, coordinator-lock acquisitions per event
  6.12 → 3.97, broadcasts per event 4.66 → 0 (0.96 targeted wakes: each event
  found its pump parked), submit p99 0.142 → 0.050 ms, wall 3,044 → 3,012 ms.
- Two CPU-bound threads (3 + 3): pump jobs 11 → 8, retirements 10 → 0,
  acquisitions per event 3.03 → 3.02, cumulative lock wait 26.4 → 6.2 s,
  submit p99 96.8 → 36.4 ms, wall 7.7 → 4.9 s. One before sample convoyed:
  2.09 contended acquisitions per event, 765 s cumulative lock wait, 55 s wall.
- Eight CPU-bound threads (4 + 5; a fifth before sample failed its 60 s drain
  barrier): GIL scheduling dominates (wall about 11× the idle case) and the
  before tree rarely wakes a pump to an empty queue. Pump jobs 8.5 → 8,
  retirements 7.5 → 6 (idle-deadline retirements while producers stalled),
  acquisitions per event 3.04 → 3.04, broadcasts per event 3.02 → 0.03 (lane
  barrier wakes during the final drain); wall 33.0 → 34.0 s and submit p99
  285 → 365 ms are within sample noise.
These are scheduler observations against a sleeping stub, not storage or
application throughput; GIL-bound clocks vary up to 2× between samples.

Specs in `tests/tasks_pkg/test_task_event_delivery.py`: followers of a running
lane neither wake, retire nor submit pumps; a newly ready lane wakes exactly one
parked pump; a spurious wake-up keeps its idle deadline; a pump start never
holds the coordinator lock; the producer's projection read never waits for it;
and barrier cost counts retried persist calls and separates storage from
queuing. The existing worker-start failure, idle retirement, shutdown and
residency specs still pass; `tests/meta/test_task_event_delivery_contention_measure.py`
checks the benchmark's instrumentation and order proof.


Terminal metadata experiment — Problem: existence checks fetch complete event
bodies although the native authority supports `event.latest metadata_only`.
Six fresh component workers showed 64-byte cases near parity, 1 MiB P50 ratios
0.0219..0.0224 and 4 MiB ratios 0.0047..0.0051. Response JSON fell to 68 bytes;
these sizes were sampled outside clocks, not inferred wire bytes.
The ROOT cohort
completed 778 identical operations in eight workers but exceeded a P95 guard.
Decision: withdraw the application selector and retain all evidence; no default
or whole-system admission. Keep the existing authoritative metadata operation
and the SQL fixture correction that restores its real read-guard table.
Alternatives rejected: promoting component gains as system gains; assuming
polling caused the failure. Next calibrate identical-implementation workers and
investigate joined terminal completion observation; the fixture's 10 ms polling
must not be mistaken for production work or used to discard recorded failures.

## Provider text cadence

Problem: a 256-character flush hint made larger provider chunks bypass the
100 ms text window, while enlarging that hint alone stacked another wait on
the committed Turn projection cadence. Decision: durable `TaskTextDeltaCoalescer`
observes `next_progress_projection_deadline` and can only shorten its current
window. The observation reads the existing committed stamp; it grants no write
or task authority. First text stays immediate. Structural, retry, provider-return
and close boundaries still flush in order through the existing callback owner.
The default character hint derives from the smaller existing event/progress
frame envelope, retaining metadata headroom and six bytes per JSON character;
caller overrides remain. Tasks without an Attempt retain the portable policy.
The existing worker remains at most one per active coalescer and joins on close;
`peak_pending_chars` observes its largest buffered text, not process memory.

Alternatives rejected: enlarging the hint without deadline alignment increased
browser latency; a separate projection timer or delivery queue would duplicate
existing cadence and lifecycle ownership. Actual-application resource and timing
evidence belongs to [Turn rendering performance](TURN_RENDERING_PERFORMANCE.md).
This policy applies to the Python provider-text entry; native model ingress has
its own callback transport and still needs equivalent whole-application evidence.
Specs: `test_stream_delta_coalescing.py`, `test_progress_projection_deadline.py`,
progress-batch rollback tests and the full live-pipeline browser fixture.

Problem: the coalescer held its buffer lock across the whole `append_event`,
including the wait for the task's event-ordering lock, which any concurrent
append holds (a tool-effect or terminal barrier holds it while draining the
FIFO), and, before the pump change above, the coordinator lock; the provider
stream reader's next `add()` waited behind all of it. Decision: every emitter
(trailing-window worker, boundary flush, close, leading edge and character
bound) first takes `emit_order`, the lock its emit uses to sequence the event
(`event_delivery.task_event_ordering`, the managed lane's producer lock), and
only then the buffer lock. The buffer lock still covers each accumulation and
each emit's own work, so the cumulative projection and the event buffer stay
one ordering boundary and events never overtake. An emitter waiting for
another producer holds no buffer lock: provider text keeps buffering until the
character bound, when the provider's own emit waits in that same order (the
existing bound and backpressure). The committed-cadence observation, which
takes its own process lock, is read before the buffer lock. Lock order: task
event order, coalescer buffer, then coordinator, content and event-list locks;
no path takes them in reverse. Alternatives rejected: emitting outside the
buffer lock lets accumulation race the emit's projection snapshot (a
projection ahead of its event) or must defer accumulation (an attempt restart
could then re-append discarded text); passing a frozen text snapshot through
`append_event` breaks its facade contract for every seam; a second per-task
queue or thread duplicates the delivery FIFO. Spec:
`test_background_flush_waits_for_the_task_event_order_without_blocking_provider_text`.

## Detached projection copy and accounting

Problem: every structural event copied its full projection inputs and then
walked that same graph again to measure retained bytes. A 30-second live
Python profile on 2026-10-29 attributed about 2.3 sampled thread-seconds to
`retained_snapshot_bytes`, in addition to the copying work. The count is
observability, not an admission quota; removing it would hide residency.

Decision: `capture_task_projection_snapshot` assembles the complete structural
inputs and uses `lib.utils.fast_deepcopy_with_retained_bytes` to copy and account
builtin dictionaries/lists in one visit. The existing identity memo preserves
aliases and cycles. Atomic values are charged once by identity and containers
use their final allocator sizes, with the same owner allowance. Custom deepcopy
hooks, tuples and subclasses retain generic copying and measure the final
reachable result, including keys that merge after a custom copy. Text/thinking/
phase remain independently owned per event. Progress frames reuse the detached
structure and its exact charge; structural events replace both. No event,
history, admission, worker, persistence barrier or tuning setting is removed or
added.

Alternatives rejected: a generic per-node accounting callback erased the gain
(2,000 tool-round graph ABBA median CPU 13.8–14.3 ms before versus 14.3–14.4 ms
after), so it was discarded. Omitting graph accounting or charging serialized
bytes instead would change the resource contract. The retained implementation
preserves the existing inline atomic-copy path and the unmeasured copy helper.

Problem: a lane dropped its structural source whenever it drained, so the next
progress frame after any idle gap (text after a pause, `tool_progress` from tool
heartbeat threads) paid a full projection copy; a 240 s live profile attributed
316 GIL samples to that copy. Decision: the lane keeps its newest structural
source and holds one charge on its identity, beside the queued and in-flight
items that reference it. Progress frames after a drain reuse it exactly as
frontier frames do. The lane's charge ends exactly once: when a structural
event replaces the identity, when the terminal frame is published, on a
composition handoff (`release_projection`), on revocation or at shutdown. A
lane collected without any of these hands its charge back through a lock-free
finalizer queue that the next submission or metrics read settles, so residency
stays exact without taking the coordinator lock from garbage collection.
Alternatives rejected: retaining the source without its charge hides real
residency; a time- or size-bounded retention cache adds knobs; re-capturing
only the changed structure belongs to the capture owner in `turn_lifecycle.py`,
not the delivery lane. Measurement (production append entry, a delta after
every drain, 200 samples, Python 3.12.13): 100 tool rounds of about 2 KB cost
0.54-0.58 ms CPU per append before and 0.10 ms after, retaining 228,850
charged bytes while the lane is idle; 400 rounds cost 1.83 ms before and
0.10 ms after, retaining 907,986 bytes. Specs:
`test_drained_lane_retains_its_structural_source_and_releases_the_charge_once`,
`test_shutdown_releases_every_retained_structural_source` and
`test_drained_lane_progress_reuses_the_retained_structure_without_a_new_copy`.

The complete projection-capture ABBA comparison, including the existing
sanitization and text snapshot, used 2,000 synthetic tool rounds and 20 captures
per leg on Python 3.12.13. Median calling-thread CPU was 19.375/19.162 ms before
and 16.189/16.428 ms after (about 15–16% less); median wall time was
19.435/19.224 versus 16.238/16.498 ms. All 80 captured values had the same
SHA-256 and exact 3,267,483-byte retained charge. This measures projection
capture, not whole-task throughput or browser latency. Specifications are
`tests/utils/test_detached_graph.py`,
`tests/tasks_pkg/test_projection_snapshot_accounting.py`, and the existing
delivery/cold-replay/ownership suites.

## Settled projection history

Problem: every structural event re-derived the whole Turn projection from the
whole task history, so a long task cost O(n²). A 240 s live `py-spy --gil`
profile on 2026-11-01 (21,572 GIL samples) attributed 17% of all GIL time to
producer-side capture copying and byte-accounting every tool round, API round
and status entry, and 2,771 samples on delivery threads to projection: trace
sanitizing, per-round pricing (`get_pricing_data()` copied under a lock once
per round), timeline normalization, stable segments, normalization and patch
diffing, each over every row.

Decision: rows that will never change again settle into deep-immutable twins
(`lib/conversation_sync/settled_rows.py`): `SettledDict`/`SettledList` encode
to byte-identical JSON, compare equal to plain containers and raise
`SettledRowMutation` from every in-place mutator; `dict(row)`, `copy.copy`,
`copy.deepcopy` and pickling return plain mutable containers. Settle points:
round close (`settle_finished_tool_rounds`; terminal verdict, no registered
worker, no worker that outlived result publication, no background-command
owner, no swarm spawn row), priced API rounds (`stamp_round_costs`), closed
status intervals, folded activity entries, the settled segment mirror,
resume hydration of retained rounds and Request Inspector snapshot messages
(next section). Capture shares settled rows by reference
(`detach_sharing_settled`) and pure steps memoize per row in the row's own memo
slot (persist trim, frame elision, execution identity, public round
normalization, trace sanitizing, encoded sizes, timeline normalization,
segment validation, block identity, checkpoint JSON text and snapshot message
digests); the patch diff short-circuits on container identity. A memo entry lives exactly as long as its row. Settled API
round costs are keyed by a pricing revision that every pricing writer bumps
(`lib/pricing/_revision.py`: refresh publication, provider registry
copy-on-write, global table writes) plus the snapshot rates and the peak
multiplier in force, so a pricing change reprices exactly the next fold.
Pricing readers take no lock: refresh publishes an immutable snapshot by
reference swap and the provider registry replaces row maps copy-on-write.
Later writers of settled rows replace them copy-on-write (terminal dangling
sweep, terminal cost stamp, cost-experiment tags, timeline folds).

Accounting: a graph without settled rows is copied and charged exactly as
before. A settled row is charged its standalone retained size once per graph,
computed once per row; atoms it shares with other rows are charged per row and
each settled container carries one memo slot. That upper bound is intentional:
an exact unique-object charge must walk every shared row on every event, which
is the O(history) work removed, and the charge is an observation, never an
admission gate.

Alternatives considered: an explicit mutation API that bumps a per-row
revision leaves nested in-place edits silently stale, and plain dicts cannot
be frozen in place, so writers that kept a reference would go unnoticed;
freezing at settle instead makes every later in-place edit of the shared row
fail loudly. Validating shared rows by equality each event keeps O(history)
work and misses `1`/`True` type changes. A time-based pricing cache would
serve stale prices. Rows a background owner can still write stay live, so
late writes keep their old visibility.

Evidence: `tests/turn_lifecycle/test_projection_incremental_equivalence.py`
drives randomized and long turns (tool/API rounds, retries, compaction,
fallback, swarm, background handoff, resume, terminal states, lane overflow)
through the production settle points and compares every projection document
and patch byte-for-byte with the vendored pre-change pipeline
(`tests/turn_lifecycle/_projection_oracle.py`); it also checks the charge
formula and that charges without settled rows match exactly.
`tests/conversation_sync/test_settled_rows.py` and
`tests/pricing/test_settled_round_cost_memo.py` specify immutability, copy
semantics, accounting and exact cost invalidation.
`benchmarks/turn_projection_incremental.py` measures per-event capture,
projection and patch time against task size, idle and beside CPU-bound
threads, after checking both trees produce identical documents.

## Frames and history rows visited once

Problem: a 300 s live `py-spy --gil` profile on 2026-11-02 (12,617 GIL
samples, after settled history) attributed 1,355 samples to `append_event`.
Every queued frame was copied (`fast_deepcopy`, 170) and then walked again two
or three times by `retained_snapshot_bytes` (305): alone, beside its projection
text and phase, and once more for a progress frame's batch charge. A child's
Request Inspector snapshot was copied (106) and walked again (140). Every
structural capture also re-derived and re-copied each settled history row
(419: `_SharingCopy.copy`, `memo_get`, and the per-event `trim_tool_round_copy`
and `_segment_without_round_mirror` rebuilds).

Decision: a queued frame is detached by `detach_frame_sharing_settled`; one
visit copies it, shares its settled rows and charges the copied containers.
`FrameCharge.complete`, called when the frame is enqueued, measures the
frame's atoms and its root with the root's direct keys and values, then the
projection text and phase the frame owns against the same identity set; the
frame charge, the owned-values charge and the progress batch charge all come
from that one completion. Atoms are measured at enqueue, not while copying,
because `TaskRuntime.append_event` stamps `seq`, envelope identity and
execution-clock fields on the root after detachment, and its replay-window
sizing serializes the frame with orjson first, which attaches a cached UTF-8
form to non-ASCII text and so grows its `sys.getsizeof`. The former walk
measured after both; so does the completion. Barrier frames, never charged,
are copied unmeasured. Inspector evidence is copied and charged by
`detach_sharing_settled(measure=True)`, with nothing serializing it in between.
Projection capture derives every history list through `derive_rows`
(`toolRounds` persist trim, `segments` mirror strip, `apiRounds` sanitizing,
`_timingStatusHistory`, `_checkpointToolRounds`). A per-list memo on the task
(`_projectionCaptureRows`, released with the other projection fields at
terminal persistence) keeps each settled row's derived output while the row
keeps its identity, derives again only live, appended or replaced rows,
survives a bounded window dropping its oldest rows, and records the reused
outputs' ids, standalone charges and frame-budget payload size
(`compact_tool_rounds_for_frame_budget(payload_chars=...)`). The detached copy
shares the reused outputs and adds their memoized charge without visiting
them; it appends the list item by item, so the list allocates exactly as the
row-by-row copy did. Documents, charges and residency are unchanged: a frame
without settled rows is charged `retained_graph_bytes` of the frame as
enqueued, settled rows keep the standalone formula above, and every capture
equals the row-by-row capture in document, charge and sharing. Settled rows
inside a frame (a terminal frame's priced `apiRounds`) are now shared instead
of copied; frames of tasks without a lane already carried those same rows.

Alternatives considered: settling each derived history list into one shared
`SettledList` would make capture O(1), but its standalone walk deduplicates
atoms across rows and carries a memo slot, changing every charge. Measuring
frame atoms while copying under-charged a non-ASCII tool result by 1,554 bytes
in the differential spec, because orjson attached UTF-8 caches before
enqueue. Keying the memo on list objects misses copy-on-write replacement of
one row, so every capture revalidates row identity instead (`map(operator.is_)`,
pointer compares at C speed, no derivation or copying). Holding the memo on
the delivery lane would tie the capture API to the coordinator for no
lifetime gain.

Measurement (Python 3.12.13, `benchmarks/turn_projection_incremental.py
--entry append`, seeded tasks, 6 cycles of 8 frames, fresh workers alternating
pre-change and post-change trees ABBA, producer-thread CPU; both trees produced
identical charge and structure digests at every size and load): with 8
CPU-bound threads, median CPU per appended frame went 0.319→0.252 ms at 10
rounds, 0.401→0.249 ms at 100 and 0.782→0.317 ms at 500; per structural frame
0.435→0.370, 0.602→0.440 and 1.546→0.613 ms; per progress frame 0.176→0.129,
0.147→0.126 and 0.147→0.132 ms; capture per structural frame 0.275→0.233,
0.445→0.246 and 1.329→0.420 ms. Idle at 500 rounds: 0.722→0.296 ms per frame,
capture 1.199→0.385 ms. A 30-cycle rerun at 10 rounds put capture within 3% of
the old tree in both directions and progress frames 11–13% cheaper. The
wall-clock `--entry projection` arm shows idle capture 0.465→0.284 ms at 100
rounds and 1.364→0.485 ms at 500, `_task_projection` unchanged. A 200-message
Inspector snapshot: copy plus walk 1.76 ms, one measured copy 1.31 ms, equal
charge. These are component observations on a shared host under load average
60–160, not task throughput.

Specs: `tests/conversation_sync/test_settled_rows.py` (a frame charge equals a
fresh walk after root stamps and orjson serialization, each object measured
once; derived rows under reuse, front drops, copy-on-write replacement and
repeated rows; planned copies equal the row-by-row copy in JSON, charge,
allocation and sharing), `tests/turn_lifecycle/test_projection_incremental_equivalence.py`
(every structural capture of the randomized, long and resumed turns equals a
vendored row-by-row capture; randomized frames through the production append
path are charged exactly the former walk's bytes, each container measured once;
a capture derives only live and new rows) and
`tests/meta/test_turn_projection_incremental_measure.py`.

## Turn events are encoded once

Problem: `turn.event.record` measured its byte bounds with encodings the
authority never received (an ASCII-escaped dump of the raw frame for the
64 KiB inline-evidence choice, stdlib UTF-8 dumps of the evidence and patch for
the Attempt-event document bound, `canonical_json` per batch entry for the
event budget), and the storage.v2 client then encoded the whole command again
after a Python compatibility walk, through the stdlib encoder whenever a float
or more than 4,096 values appeared. Every `messages_snapshot` re-encoded and
hashed each message of the whole history (O(history) per snapshot), and the
executor checkpoint, about twice per round, dumped every tool round and every
segment again. A 300 s live GIL profile on 2026-11-02 (12,617 samples)
attributed 1,806 to delivery persist: 426 to the stdlib wire encoder, 327 to
snapshot canonicalization, 305 to the two size dumps; checkpoint dumps took 248.

Decision:
- `turn_lifecycle._CommandEncoding` encodes each command value once with
  `lib.storage.protocol.encode_json_value` (finite-number admission, orjson,
  the stdlib fallback for values orjson cannot represent). The inline-evidence
  choice, the Attempt-event bound and the batch event budget read `len` of
  those bytes; `FrozenPayload` adopts them and both clients splice them as
  `orjson.Fragment`. The inline evidence copy and the carried row share one
  encoding of the snapshot delta, and shed, oversize and rebase retries reuse
  every unchanged value.
- All three bounds mean compact UTF-8 JSON bytes as stored. The 64 KiB choice
  formerly counted ASCII-escaped characters, so a non-ASCII frame moved to its
  delta at about a third of its stored size; such frames up to 64 KiB stored
  now ride inline, and ASCII frames decide exactly as before. The other two
  bounds already measured the UTF-8 length of the same values.
- storage.v1 frames were already orjson and are byte-identical. storage.v2
  requests are byte-identical except floats of magnitude below 1e-4
  (`0.00001`, `1.5e-7` instead of `1e-05`, `1.5e-07`); both authorities parse
  them to the same value before storing their own canonical encoding. A value
  no encoder admits (a lone surrogate) is refused before dispatch with the
  storage.v1 message `Storage frame is not serializable` on both lanes, through
  the same `database_protocol_error` evidence-shedding path as before.
- Snapshot messages settle (`build_round_evidence`, keeping the previous
  snapshot's twin of each unchanged message), which the lane detaches share
  (previous section). The delta projector memoizes each settled message's SHA-256 in its
  memo slot and keeps one hash state over its previous scan: a shared prefix
  made of exactly those settled objects (proved by digest-object identity) is
  neither encoded nor hashed again. An equal digest of another object is
  re-hashed from the current bytes, so `prefixHash` always covers the current
  messages. The reuse source `_snapshotMessages` is terminal heavy state.
- `settled_rows.json_array_text` builds the checkpoint's `json.dumps(rows,
  ensure_ascii=False)` from per-row text memoized on settled tool rounds and on
  the settled thin twin of each settled segment.

Alternatives considered: stdlib-identical pre-encoding would keep v2 float
spellings but needs the compatibility walk or the slowest encoder for every
value; retaining each settled message's canonical bytes keeps a second
resident prompt copy and still hashes the whole prefix per snapshot; reusing
the hash state on equal digests alone lets a hypothetical digest collision
authorize a different prefix at rebuild; settling the round tool list for its
content hash has no immutable owner and comparing it costs as much as hashing
(the per-snapshot `content_hash(tools)` remains).

Evidence: `tests/turn_lifecycle/test_turn_event_encode_once.py` (an encode-count
instrument over the one admitted encoder, request bytes of both clients
against the former plain requests, the UTF-8 inline bound, batch budgets and a
managed-lane run whose stored deltas equal plain-message deltas),
`tests/tasks_pkg/test_snapshot_delta.py` (rows equal an oracle built from the
public prefix helpers, with settled and plain messages through growth,
duplicates and an in-place rewrite; settled history encoded once; a forged
collision still degrades), `tests/tasks_pkg/test_checkpoint_settled_row_text.py`
and `tests/conversation_sync/test_settled_rows.py`.
`benchmarks/turn_event_persist_encode.py` (300 rounds, Python 3.12.13, identical
request, row and checkpoint digests in every arm) measured calling-thread CPU
per event, before/after on the tree that already shares settled rows in lane
detaches: all events 4.13/2.11 ms idle and 4.69/2.69 ms beside 8 CPU-bound
threads; snapshots 15.7/6.6 ms idle, last 50 rounds 26.0/10.1 ms; checkpoints
12.5/4.9 ms; structural frames 1.38/1.36 ms and text progress frames
0.120/0.123 ms are unchanged within noise. Beside the busy threads, snapshot
wall time fell 224/158 ms; wall time of the small frames follows interpreter-
lock scheduling and varied up to 2x between runs at equal CPU. Retained
at round 300: `_snapshotMessages` holds 265,016 container bytes (strings shared
with the history) plus 173,978 bytes of digest memos; a non-conversation task's
settled tool rounds hold 1,136,706 characters of checkpoint text, released
with the rows.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

# Task event delivery


Managed root execution and ordinary event persistence use separate lifetimes
under [`task_event_delivery_v1.json`](../contracts/task_event_delivery_v1.json).
Independent task lanes start on demand through the shared worker executor.
`TOFU_AGENT_WORKERS` sizes idle reuse; legacy item/byte constructor hints and
submission timeouts grant no admission quota. Each task retains one FIFO and
one terminal receipt, with commit before push and hot replay. Idle pumps retire
and release detached structural sources; shutdown joins pumps and physical
carriers before runtime-state/storage teardown.

Detached structural graphs are charged once through their last queued or
in-flight reference; each event also reports its private frame/text residency.
These are observations, never permission to reject snapshots or wait for a
slot. Python callbacks and their detached inputs stay owned until settlement
or proven authority revocation. Canonical event bodies can use the storage
batcher's RAM/file overflow; the coordinator adds no durable store or cursor.
Progress batch frame/count bounds split RPC composition without limiting the
total accepted stream. Explicit owner cancellation and shutdown reject new
submissions; genuine unstarted worker failures retain that lane's evidence for
repair, while published siblings keep their lifetimes.

Failed heads prevent their own followers from overtaking; shared transient
classifications retain at most eight attempts with capped backoff. Exhausting
transient retries leaves ordinary memory admission open, with the original
failed head and its followers retained. New events neither clear that failure
nor restart retries; explicit barriers/maintenance repair the original FIFO.
The first persistence attempt freezes its batch shape through failed/lost-ACK
repair; only never-entered tails can absorb newly accepted progress frames.
Non-transient errors, revoked authority, cancellation and shutdown still refuse
admission. Repair and terminal visibility reuse the existing terminal
maintenance/eviction fence.

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

`tofu_task_event_delivery_*` gauges expose pending items/bytes, active and resident
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
failures bypass model retry/fallback and classify as server capacity pressure,
preserving the first exception rather than replacing it with a later phase error.
The controlled production-entry regression queues 64 progress frames behind a
blocked sink with 100 detached tool results (Python 3.12.13). Its charged peak
is 1,295,483 bytes versus 66,113,033 bytes under the former per-frame accounting
(51.0x lower). Exact 0..63 commit order and immutable history are checked;
this measures retained-object budget accounting, not whole-process RSS or real
database throughput.

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

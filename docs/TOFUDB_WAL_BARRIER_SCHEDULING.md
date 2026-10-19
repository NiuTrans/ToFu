# WAL barrier scheduling and page-read attribution

This contract owns `packages/tofu-db/src/wal.rs` barrier scheduling and the
page-read sub-stages in `packages/tofu-db/src/entity.rs`. Durability witnesses
remain generation-bound as defined in [native storage](STORAGE_TOFUDB.md).

## Problem, decision and alternatives

A condition-variable notification is not retained while the WAL worker is in
fsync. The worker previously slept unconditionally after syncing its captured
prefix. Writes posted during fsync, or between that sync and sleep, could wait
for the idle interval even though the worker already had pending work.

Before sleeping, the worker now checks requested versus synced length while
holding the same state mutex used by append notifications. Pending work causes
an immediate next iteration. Stop still wins; a latched I/O error prevents a
busy loop. The next iteration captures the current generation afresh: a sync
of the old path cannot credit successor bytes. The durable-witness boundary,
strict acknowledgement, Normal loss bound and configured interval are unchanged.

Alternatives rejected: shortening the interval spends idle resources without
closing the notification window; treating a notification as durability credits
bytes that have not been synced; spinning after an I/O failure hides a latched
failure and burns CPU. No permanent worker or memory cache is added.

`wal::tests::barrier_rechecks_writes_arriving_between_sync_and_sleep` pauses the
actual worker after a real sync, posts another framed record, then resumes it.
With a one-second interval the old worker times out at 250 ms; the corrected
worker covers the second witness and both exact records survive simulated crash.
The test seam is an empty closure in production and retains real VFS calls.

## Performance anchors

`wal_barrier_sync` measures each worker fsync; the `wal_barrier` component
separates it from connection execution. `wal_barrier_reschedule` calls count
pending-prefix rechecks that avoid sleeping; its tiny elapsed duration is not
queue latency. Existing `wal_barrier_wait` remains the caller's witness wait.

`load_node` now has nested `entity_page_cache_read_wait`,
`entity_page_source_read`, `entity_page_decode`, and
`entity_page_cache_write_wait`. Cache wait spans end on acquisition, before
lookup/insertion. Source read includes verified backend resolution, not only
physical I/O. Decode covers directory/page decoding. Timers measure elapsed
time, not CPU time; inclusive totals must not be added together. The profile
report classifies both cache-lock stages as waits. All stages keep the existing
bounded content-free counters and optional request retention.

## Rejected decoded-page probe specialization

Problem: default-off decoded-page cache hits still execute two timing intervals
and eight shared timing-counter updates. A const-specialized candidate skipped
hit clocks/counters and kept source-miss verification, decode, insertion and
miss timers. Sampled hit/miss fields added 16 bytes per record. Current runtime
keeps its original timers, counters, cache algorithm, scope, quotas and format:
neither candidate below is adopted, so there is no resident/persistent growth.

Decision: reject both runtime candidates after paired performance controls.
The inlined version passed 1,583 tests with two pre-existing ignores and made
800,000 exact native single-caller reads about 3.2% faster with 2.8–3.2% less CPU.
A separate 640,000-read eight-caller array control regressed CPU 1.1%/0.3%
(materialized/overlay). The second version changed only the load-node entry
annotation to non-inline, passed the same full suite against its own source,
and improved selected native scalar/array controls. Its SDK corpus nevertheless
regressed float-plus-text CPU 8.7% in the first cohort and 7.9% in an independent
eight-leg followup; numeric boundaries and other adverse legs remain recorded.
The followup also improved simple scalar/parallel shapes, so this is a mixed
tradeoff, not a uniform optimization. SDK and native cohorts use the same
pinned CLI/DSO and exact native bytes/SDK values; broad controls separately
check successful operation contracts against three engines.

Both versions successfully removed default-off cache-hit timing (about 13,000
load calls -> three actual source misses in the array control); enabled spans
and hit/miss work remained coherent. This proves operation elimination, not
end-to-end improvement. Cache/layout/code-generation explanations remain
hypotheses: controls varied and no CPU/assembly attribution proves the cause.
Alternatives defeated: ship solely because counters/allocations fall; discard
negative cohorts; weaken source verification, poison or cache locks; bypass
the embedded worker watchdog to avoid its queue. Both isolated commits and
frozen build/test/performance identities remain in the evidence.

A new bounded diagnostic reads process-wide RUSAGE_SELF before/after isolated
20,000-read single-caller phases, outside their timing boundary. Strict disposable
FUSE authorities and byte-exact C ABI replies are used. Across baseline and
candidate controls, voluntary context switches are about 2.02 per read, system
CPU about 5.7–7.9 us/read and user CPU about 8.9–10.5 us/read. The numbers include
all native threads/background activity; they do not isolate worker wakeup cost,
explain every regression, or prove that direct caller execution would be safe.
They make request/reply handoff a concrete next target while asynchronous hard
watchdog, late-result disposal, panic/poison, deadlines and byte bounds remain.

Experiment setup errors are retained separately: one test assertion required
Debug for RootNode and was corrected without changing runtime types; a native
array probe aborted before loading its DSO because its canonical fixture was
missing. The failed authority/logs remain; successful scalar legs were not
repeated, and the repaired array cohort used fresh parent identities. Neither
setup error is a database regression. All adverse timing/CPU/tail legs,
observer off/16/16/off costs, resource reports, three-engine controls and sources:
`tofudb_page_probes_20261004.json`.

## Defeated publication cohort experiment

A separate caller-led cohort candidate reduced WAL write time and admission
latency but regressed scheduled stream P99 and peak RSS in default-off ABBA.
It is not enabled or integrated. Increased rotation parks are measured;
causal proportions among cache contention, physical merges and maintenance
scheduling remain unproven. Two grouped wire runs closed connections while the
daemon stayed alive; a diagnostic repeat recovered 10,002 exact payloads after
two SIGKILLs. That repeat does not erase the failed runs or certify all tails.
The isolated candidate and raw reports remain named in
`tofudb_wal_barrier_scheduling_20261001.json`.

## Verification and limits

Source `d0bc7a85…` matches the tested CLI and embedded library. Native checks:
1,462 passed, two ignored; profile-report checks: 20 passed. Sequential and
32-session acknowledged bursts each survived two actual SIGKILLs and exactly
10,002 content readbacks. These are finite crash checks, not recovery P99.

Default-off capacity ABBA, 1,000 conversations with four 8-KiB chunks:

| Metric | A1 | B1 | B2 | A2 |
|---|---:|---:|---:|---:|
| Admission P99, ms | 2891 | 2721 | 3049 | 3036 |
| Scheduled stream P99, ms | 5109 | 4318 | 4707 | 4414 |
| Settle P99, ms | 6922 | 6449 | 7063 | 6345 |
| Peak process RSS, decimal GB | 2.136 | 1.985 | 1.989 | 1.986 |

Every leg had zero errors and 1,000 exact content replays; every absolute
latency budget failed. Ranges overlap: no stable end-to-end or memory win is
claimed. Low-load append means likewise overlap. The deterministic scheduling
bug is established independently of noisy capacity improvements.

A separate 1/16 profile retained 750 completed requests with zero drops.
Foreground page-source reads took 4,372 ms of 5,900 ms inclusive page loading;
cache read/write acquisition took 346 ms, decode roughly 0.38 s process-wide.
This prioritizes backend read/materialization analysis, not cache-lock changes.
Identified waits still consumed 98.8% of summed sampled dispatch. The idle-loop
fix does not solve that convoy or establish superiority over SQLite.

## Borrowed single-record WAL framing

Problem: single-record append copied its already encoded transaction into a
second full-size framing allocation under authority. Diagnostic attribution
separated frame copying from CRC, BLAKE3, file writes and barrier notifications.

Decision: calculate the original CRC/hash chain over the fixed header and
borrowed payload, then submit frame prefix, payload and trailer with one
vectored-write loop. Length twins, bounds, hash ordering, append classification,
rotation, strict witness and acknowledgement boundaries remain unchanged.
`encode` retains contiguous batch framing and an independent byte oracle.
`calculate_record_hash` shares the incremental checksum implementation.

RealFile retries Interrupted, advances segment views after short writes and
returns WriteZero when no progress is possible. VFS wrappers and the durability
simulator flatten segments into one original `write_all_at`, preserving their
single encryption/fault boundary. Errors retain the existing ambiguous-append
classification; logical length advances only after write and barrier posting.

Alternatives: keeping a reusable whole-frame scratch buffer retains maximum-size
capacity and still copies bytes; dropping CRC or changing the hash/record format
weakens the recovery boundary or requires migration. Borrowed segment writes
preserve the existing durable bytes and remove the second allocation on RealFile.

The `tofudb_wal_vectored_append_20261006.json`
separates observer attribution from default-off measurements. Isolated 1,000-active
candidate stream P99 was 967.5/999.6 ms against baseline 994.2/1,222.9 ms; the
near-budget second result and an adverse settle tail do not prove stable capacity
or all-scenario dominance. Final combined stream P99 was 1,198/983 ms against
1,211/1,000 ms; one candidate leg still fails 1 s. Six concurrent native updates
are included only in the combined candidate, so this pair cannot attribute all
changes to WAL framing. Four small-matrix legs complete all 16 phases; adverse
history and append legs remain in evidence. A medium three-phase followup
retains a 2.1% adverse history mean and does not establish uniform superiority.
The single-record RealFile path replaces a payload-sized transient
allocation with fixed frame arrays and three IoSlice views; wrappers retain their
original copy. Client RSS observations include application plus embedded engine;
resident/allocated footprint superiority is not established by this slice.

Verification compares every byte with the original encoder at empty, hash-block
and 8 MiB payload boundaries, followed by crash/recovery. VFS tests exercise
short writes spanning segments, empty segments, Interrupted, WriteZero and I/O
errors. A new deterministic nine-cut crash test forces partial frames to disk,
retains the acknowledged prefix, truncates exact residual bytes and retries
successfully. Final source-pinned checks pass 1,585 native tests (two existing
ignores), 38 cross-process tests and 39 original SQL/native application cases.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

# Storage performance charter (Tofu-DB vs SQLite)

Full-catalog history promotion and accepted-event-to-push anchors follow
[publication latency](TOFUDB_PUBLICATION_LATENCY.md#full-catalog-history-promotion-and-write-delivery-anchors).
Effective cache-budget evidence and eager-admission qualification follow
[decoded-page admission](TOFUDB_WRITE_PATH_OVERLAY.md#decoded-page-admission-and-benchmark-budgets).
This document defines how we measure Tofu-DB against the legacy sidecar and what "comprehensively better" means. It is intentionally separate from
`STORAGE_TOFUDB.md` (which documents the certified durability/correctness
guarantees) — this file is about *speed, footprint, and tail behavior under the agent-harness workload*.

The [Harness database architecture experiments](TOFUDB_HARNESS_DATABASE.md)
target work that grows with history, branching and repeated projection updates.
They distinguish a better execution data model from a native-engine advantage
and from whole-Agent results; experimental primitives do not change production
storage selection or weaken the acceptance criteria below.

## Loose-block length reuse

Problem: loose-block reads validated handle length, then RealFile queried the
same length again inside read_all. Decision: reuse the observation as an
allocation hint while reading to EOF under the original byte bound. Actual
growth beyond the bound still refuses; a shortened block checks its minimum
before header access. The VFS default delegates through existing read_all,
preserving wrapper/encryption/fault boundaries. Alternatives rejected: reading
only the observed prefix can hide appended corrupt bytes; removing size and
checksum checks weakens integrity.

`loose_read_measure` runs three alternating pairs of 128 warm 64 KiB reads
over private temporary files on the project's FUSE volume. Duplicate/reused
length medians were 0.00481/0.00434 ms; median P95s 0.00615/0.00567 ms. Every
read checks its length and digest. This small component result excludes opens,
block verification, cold caches and application RPC; it is not a task-speed
claim. One duplicate real file-metadata query is removed per loose read.
Run `cargo run --manifest-path packages/tofu-db/Cargo.toml --example
loose_read_measure -- <existing-base-directory>`. File growth/shrink and
simulated read faults are covered in `vfs::tests`.

## Hot-cache scaling

Problem: fixed 2/1 GiB payload/page ceilings stopped cache growth while the
launch probe still reported abundant headroom; chunk proofs inherited the
payload ceiling and could repeatedly decode a larger working set.
Decision: retain headroom/16 and /32 defaults and lean floors, with explicit
overrides bounded per cache by headroom/4. Allocation remains demand-driven;
ordinary eviction, immutable-source verification and close/zero-capacity
release retain ownership. No worker or durable state is added.
Alternatives rejected: larger fixed caps merely move the plateau; relying on
kernel caching does not retain decoded proofs; unlimited overrides can consume
all launch-observed headroom.

The opt-in `hot_cache_scaling_tests::hot_cache_scaling_measurement` runs three alternating pairs against the production verified decoder/cache, using 300 distinct 1 MiB chunks and the same current encoded allocations. With 128 GiB declared headroom, old/scaled decode capacities were 128/512 MiB. Three timed passes produced identical output digests: old/scaled hits 0/900, misses 900/0, median 401.26/1.27 ms in the debug build. Accounted residency was 133,252,159/314,763,240 bytes, within each budget, and returned to zero after release. This measures verified decode reuse, excluding source I/O, JSON, RPC and whole-task latency; it does not establish a production speedup. Run with `cargo test --manifest-path packages/tofu-db/Cargo.toml --lib hot_cache_scaling_measurement -- --ignored --nocapture`. Ordinary resource tests cover lean/pressured hosts and continued growth on larger hosts.

## Native event batch composition

Problem: the application event batcher used the wire-frame budget to combine
independent calls, while a native transaction above its format-bound carried
payload ceiling materializes and synchronizes referenced loose blocks under
authority. A disposable same-source test of 500 distinct 8 KiB streams reproduced
this boundary twice: one batch took 3.86/3.76 s with one spill each; four 125-row
batches took 293/201 ms without spill. All four 500-row SIGKILL recoveries were
exact. These storage-only measurements do not certify the 1000-conversation
production latency target.

Decision: native readiness and control observations publish the additive
`resourceBudget.eventBatchComposition` declaration defined in
`contracts/storage_v2.json`. Its carried ceiling comes directly from the native
transaction format constant. Raw segment/event header costs and catalog/entity
metadata coefficients come from their existing encoders. The composition cost
is raw event JSON bytes plus the native format stamp, segment/event headers,
stream identity, and estimated metadata. One quarter of the advertised ceiling
is reserved for transaction-level framing and state-dependent representation
work. This is a conservative grouping estimate, never a durability or fresh
source proof; engine admission and the existing spill path remain authoritative.

The native client reads only the cached readiness declaration. The batcher
obtains its selected client outside the queue condition, then claims only one
composition-limited RPC group. Future events remain queued and cancellable;
an unchanged head is checked after client acquisition. Every ACK, fence,
natural-key outcome, callback, timeout, retry and shutdown boundary retains the
original per-group behavior. A legal individually oversized event is submitted
alone with the original event admission ceiling. Queue residency, worker count
and connection pools retain the existing launch-probed sizing. Metrics expose
the active composition ceiling and maximum estimated composition bytes.

A flush barrier ends its FIFO prefix; future events and their provider failures cannot join or invalidate it. Accepted events receive an ordinal, and a barrier records the last accepted ordinal. One bounded primitive failure summary and its earliest ordinal persist until batcher closure, covering actual provider/RPC failure and accepted events cancelled before dispatch. The summary retains no exception, traceback or event graph. Every covered barrier receives a fresh typed error; later success is not proof that the failed accepted prefix became durable. The explicit per-row `attempt_not_live` rejection also records an unstored accepted event without changing its business result or sibling ACKs; natural-key deduplication remains durable. Claimed ambiguous confirmation timeouts and observational callback failures do not invent persistence failure. Six added integer/slot fields cost 256 additional retained bytes per queued event within the existing byte quota.

Alternatives: a fixed small item cap starves cheap events and ignores payload
size; increasing the native carried ceiling changes bounded replay memory and
does not address lock work; using the wire bytes alone misses metadata and
native format costs. SQL and older/invalid native declarations retain legacy
wire grouping and do not claim this optimization. The headroom fraction is a
selection policy to measure, not an optimality claim.

## Request cost attribution

Problem: process-wide nested stage totals cannot explain one slow request or separate lock queueing from the work holding the lock. Optimizing a high call
count without measuring its share of the request can leave end-to-end latency almost unchanged.

Decision: `src/request_profile.rs` adds an opt-in, content-free dispatch profile exported as `system.metrics.requestProfile` (`tofudb.request-profile/v1`). Set
`TOFU_DB_REQUEST_PROFILE_EVERY=1` before launching a disposable authority to
record every request; N records every Nth request. Zero/unset disables it. Configuration is read once per process. Sampling is deterministic and may alias
periodic workloads; use every request for bounded experiments, and measure observer overhead with the same binary off/on/on/off before drawing conclusions.
It does not change admission, durability, retry, ownership or request results. Document materialization and public-Turn/snapshot boundaries use the same opt-in collector; their bounded memory, matched window driver and equal-work limits live in [JSON materialization](TOFUDB_JSON_MATERIALIZATION.md#window-read-ownership-and-attribution).

Existing stage timers join a thread-bound span stack. Exclusive elapsed time subtracts immediate children; exclusive stages plus `unattributedNanos` must
equal dispatch elapsed time. Inclusive stages remain available to understand nested work but must never be added together. Non-LIFO, unclosed and overflowed
spans invalidate accounting explicitly. `scripts/tofudb_profile_report.py` rejects inconsistent ledgers rather than manufacturing an attribution.

Default-off authority commit spans separate validated rebase, private blob publication, deferred indexes/recency, outbox capture, and stream encoding/catalog preparation; they add five fixed anonymous ledger slots. Overlay head-version, original blind-version and range-witness validation add three more fixed slots (72 bytes per sample/operation aggregate); existing sample bounds and size-derived byte reports remain. Additional spans cover FIFO admission, mutex acquisition, authority holds, replica creation, transaction compilation, query execution, and real-VFS
open/read/write/sync/metadata/directory/mutation operations. Fixed work counters record entity reads/reuse, overlay/tree resolutions, conflict classes, abandoned
preparation time, logical WAL/materialization bytes and requested VFS bytes. Failed operations still contribute elapsed time and attempted bytes. VFS byte
counts are not successful I/O or physical-device amplification. Requested read-all bytes come from the length observation, including a rejected bound.

The dispatch boundary starts with a parsed request and ends with the semantic response. Embedded enqueue-to-worker delay is recorded separately; neither
client scheduling, JSON conversion outside dispatch, nor response delivery is included in dispatch elapsed time. Non-embedded queue time is unknown, not zero.
The report prints dispatch mean, embedded queue mean, queue sample coverage and their combined native mean. It combines them only when queue samples cover the
same complete request population; partial/missing coverage leaves the combined mean unavailable. This sum excludes client scheduling, conversion and delivery,
so it is not end-to-end latency. A dispatch-only improvement can hide longer worker queueing and must not establish a performance win by itself.
Background-driver time remains in existing component counters and CPU sampling; it is not charged to a foreground request. Wait/hold/VFS elapsed time is wall
time, not CPU time. A wait-heavy profile identifies queue amplification; the lock-holder and CPU profiles are still needed to identify its cause.

Retention is process-local and ends on process exit, including across embedded handle reopen. Only catalog operation names plus one `uncataloged` label are
retained. There are 64 fixed histogram buckets (log2 nanosecond upper bounds),
64 nested spans, and at most 32 slow samples; sample numbers are local counters,
never application correlation IDs. No owner/key/path/payload/error text is stored. Active sampled requests release their buffers on completion or unwind;
`activeRequestBytes` and `retainedBytes` report memory cost. Collector contention
drops a sample instead of blocking storage; `selected`/`dropped` and completed request counts expose this bias. Slow samples are selected extremes, not a
representative distribution and not a source of P99 estimates. Control requests are excluded. The metrics observer never takes the authority mutex.

Alternatives rejected: pausing threads at debugger breakpoints (changes queue behavior), synchronous per-key logging (I/O and unbounded labels), and adding
nested elapsed totals as a CPU/latency breakdown (double counting). Verification: native `request_profile_tests.rs`, server/lifecycle tests, and
`tests/tofudb/test_request_profile_report.py`. Exact windows and search activation: [ownership and evidence](TOFUDB_PIPELINE_CRITICAL_PATH.md). Example after a benchmark run:

```sh
TOFU_DB_REQUEST_PROFILE_EVERY=1 python3 benchmarks/conversation_concurrency.py \
  --binary /path/to/tofu-db --native-client embedded --directory /disposable/volume \
  --conversations 128 --rounds 4 --chunk-bytes 8192 --output /tmp/profile.json
python3 scripts/tofudb_profile_report.py /tmp/profile.json
```

## Eliminating preparation rework and embedded conversion

Problem: independent conversation creation contends on an owner-global count; range witnesses compare physical leaf identities, so an unrelated key's update
or an overlay merge aborts useful preparation. The embedded ABI also parsed, cloned and re-serialized the payload before the shared compiler parsed it again.

Decision: prepared creation retains bounded, scope-owned count/activity effects. Business predicates validate first; the sequencer applies creation metadata before
recency maintenance. A count or activity-index read flushes pending effects on
the original snapshot, preserving read-your-writes and predicate conflicts.
Claim conflicts, overflow and failed commits cannot publish a partial count.

On the default overlay path, changed physical witnesses receive a bounded logical
version check: inspect in-range versions including tombstones, validate old tree
rows and the snapshot overlay against the current head. Overlay rows are checked
even when both tree witnesses are empty, detecting whole-range retirement after
a read. At most one leaf's vectors or 256 overlay key references are temporary;
existing scan, transaction and snapshot limits remain. Mounted directories and
opt-in tombstone reclamation retain conservative physical validation.

The ABI borrows validated raw JSON payload bytes and copies only those bytes
into its admitted job. Shared semantic admission, canonical receipt hashing,
worker cancellation, panic containment and watchdog poisoning remain the owners.
The raw payload has the wire byte cap before allocation; the Rust Value API
retains capped serialization. No storage format or durability mode changes.

Alternatives rejected: dropping count reads without preserving predicates;
ignoring changed leaves without checking tombstones and disappearing rows;
skipping shared dispatch to reduce transport cost. A generation-scoped WAL
suffix-copy experiment also passed its crash matrix but regressed both 1,000
conversation trials (stream P99 16.3–16.9 s versus 10.6–12.9 s). It remains an
isolated experiment; the existing WAL publication/drain protocol is unchanged.

Verification: `entity_optimistic_tests.rs`, recency fault/rollback tests and
embedded ABI receipt/cap/watchdog tests, followed by the native suite and
matched executable/library benchmark receipts.

Measured source: `573d71f7fe5e6e169981eb20f36c6b90169b1669269bfe38cd1cc2ba5841fa41`.
Same-host A/B/B/A comparison uses 1,000 conversations, strict durability, four
8-KiB chunks at one-second arrivals, profile every request, exact readback and
offline integrity. All four runs pass correctness; capacity targets still fail.
Executable/library hashes and raw receipts are retained with the isolated commit.

| Metric | Before (two runs) | After (two runs) |
|---|---:|---:|
| Create point conflicts / 1,000 captured | 974 / 998 | 0 / 1 |
| Stream range conflicts / 5,000 captured | 646 / 576 | 0 / 0 |
| Abandoned stream preparation, cumulative seconds | 11.56 / 9.77 | 1.67 / 1.45 |
| Scheduled stream P99, seconds | 11.92 / 12.03 | 11.62 / 11.80 |
| Admission P99, seconds | 5.06 / 4.88 | 4.20 / 5.24 |
| Settlement P99, seconds | 10.34 / 11.42 | 9.57 / 10.91 |

This removes substantial wasted preparation, not the dominant capacity limit:
about 98.3% of summed sampled dispatch remains identified waits. Read snapshot
acquisition still enters writer FIFO; WAL installation still drains its window.
A separate default-off 5,000-append A/B/B/A check gives 821/832 before versus
896/942 operations/s after, with overlapping P99 ranges (1.80–2.64 versus
2.26 ms). The smaller 1,000-append trial included a slower after P99 (2.90 ms);
both trials remain evidence, so no universal tail-latency win is claimed.
Verification: 1,409 native tests passed, two ignored; 89 neighboring Python tests
passed including real embedded start/restart. These are disposable benchmarks,
not a live deployment or a process-kill capacity certification.

## Profiling evidence and optimization priorities (2026-09-30)

Diagnostic source `48987578274ccb6b06d8bc8bc1ce2c576045d09cac5e70a09860f51b551b8d39` adds observation, not a new storage algorithm. Executable SHA `51f7721f761318f99ebddb4e8da8a51e2369340a2b847516262a3f40ab4eb6aa` and loaded library SHA `787d3c913dfee7246906eff4315d33f791968960d66bcf18601485a538febedf` are verified in every native load report. Runs use strict durability, DolphinFS, four unique 8-KiB chunks per conversation and fixed one-second arrivals. All native staircase runs complete exact readback and offline integrity with zero errors. The shared benchmark checkout still reported the previous native source before integration; the isolated-source/build receipt is the matching provenance. These are diagnostic runs, not new capacity certification.

| Concurrent conversations | Streaming P99 incl. schedule debt (ms) | Identified waits / summed sampled dispatch | Dropped / selected profiles |
|---|---:|---:|---:|
| 32 | 180 | 72.6% | 0 / 384 |
| 128 | 291 | 95.2% | 13 / 1,536 |
| 512 | 3,255 | 98.3% | 22 / 6,144 |
| 1,000 | 11,396 | 98.2% | 80 / 12,000 |

At 1,000 conversations, all 5,000 `turn.event.record` requests are captured:
mean dispatch is 475.5 ms, mean authority hold 1.861 ms, and the separate embedded
worker queue averages 481.8 ms. FIFO admission and rotation parking dominate
exclusive dispatch time. `event.list` and `queue.depth` spend 99.8% of sampled
dispatch waiting, despite mean snapshot holds around 5-6 microseconds. Readers
execute outside authority, but building their snapshot still queues for it.

Shared metadata also defeats useful concurrency: 993 of 1,000 `turn.create_pair`
requests encounter point conflicts (511 of 512 at the preceding load). Creation
reads/writes the owner's shared conversation count; this is a source-supported
candidate hot key, not a captured key-level attribution. Stream events encounter
524 range conflicts and 6 point conflicts, and abandon 10.84 seconds of summed
preparation. Range attribution currently combines overlay changes and tree-leaf
witness changes, so it does not prove all conflicts are false positives.

The 1,000-conversation workload offers 32.768 MB of unique stream text. Complete
workload WAL encoding is 573.783 MB and overlay materialization is 363.456 MB,
including admission, metadata and settlement; 12 checkpoints occur. These are
logical work counters, not same-payload physical-device amplification ratios.
The final rotation fence waits for all staged payloads to drain, so reducing
encoded/materialized work and shortening that fence are connected objectives.

Point-read microbenchmarks expose a different boundary: native `record.get`
end-to-end mean is about 50 microseconds; sampled dispatch mean is 12.21 us and
worker queue mean 6.72 us (2,005 samples including five warmups). All 2,005 entity
reads hit the overlay; no tree resolution or VFS read occurs. Native P99 is
80 us versus raw in-process SQLite's 10 us. That ratio includes native semantic,
JSON and worker boundaries which the raw SQLite baseline omits. Optimizing tree
lookup cannot explain this particular gap. The remaining boundary cost is not
yet a function-level CPU breakdown.

With the same conversation payloads, arrivals, volume and 30-second RPC deadline,
SQLite Sidecar at 128 conversations records streaming P99 8,225 ms and exact
readback; native's repeated 128-conversation runs are 246-291 ms with profiling.
SQLite Sidecar at 1,000 fails with 420 recorded errors, only 655 simultaneous
running conversations and 582 exact recovered contents. That failed run is not
a valid latency speedup denominator. Raw SQLite and SQLite Sidecar remain
separate baselines; missing a native SLO is not evidence of losing every SQLite
comparison. The initial SQLite launch with the new diagnostic env was rejected;
its failure is retained, the native env is now admitted by the deployment guard,
and the successful comparison scopes that env out of the SQLite child.

The 128-conversation off/on/on/off probe-cost test records productive intervals of 4.696/4.636/4.705/4.532 s: ranges overlap, not proof of zero overhead. Retained profile buffers are 99,072 bytes plus 3,944 bytes per active sampled request. Default-disabled hooks are also compared against the preceding build: 1,000 append samples show worse P99 (1.36/1.49 -> 1.68/1.89 ms), while the 5,000-sample follow-up overlaps (2.43/2.67 -> 2.40/2.89 ms); median latency and throughput do not regress in the latter. Keep both observations; there is no all-metric claim. Native tests pass 1,400 with 2 ignored while profiling is enabled. The host's system CPU profiler cannot provide usable symbol attribution (missing initial mappings, then proc-map timeouts/incomplete capture); those captures are retained but excluded from CPU conclusions. Shared-host activity and the absence of running-process SIGKILL/provider/browser coverage remain explicit limits.

Decision: prioritize generation-scoped WAL/payload draining with a short final publication fence; commit-owned deltas for shared metadata plus precise range validation; and removal of repeated JSON conversion across the embedded path. The immutable query view now bypasses writer FIFO capture under the [snapshot read contract](STORAGE_TOFUDB.md#snapshot-read-lane). Embedded transport now borrows runtime bindings instead of copying launch metadata per request; its binding/pin-release stages measure the boundary. Remaining changes need their own before/after experiment and existing conflict/crash/ownership contracts. Measure fence duration, bytes per new stream byte, discarded work, read admission and low-load boundary cost separately. Alternatives rejected: increasing worker count around the same serial gate, relaxing durability, or ranking optimizations only by tree call counts. Current measurements do not justify a promised multiplier for those architecture changes.

Raw reports, failed captures, source receipts and paired observer-cost evidence
are under `.tmp/tofudb-profiling-20260930/`. The retained report command above
reconciles accounting before exposing wait shares and per-operation work.

## Current transaction read/admission boundary (2026-09-30)

Source `8cf81a287d50a9420b9a99e0cca0a17f9c0b78e7350721a35cb4778259e4aa53`
retains transaction-local snapshot values beside their OCC witnesses and waits
for preparation pins outside authority instead of executing pin-rejected
commands under the writer mutex. Six fixed fallback counters distinguish
snapshot/preparation/commit and ordinary WouldBlock/rotation; pin waits have
an independent elapsed counter. Both executable and loaded library report this
source. Binary SHA is
`842ec2045184d2cd03f9bdcf3d189b6c9dc034d8f221e3899bd8d5ffb85e02e2`;
library SHA is
`22e2f090e7d9ec308d5f31fd5d71de70cf9c8fd745a4782490dd69149154f48d`.

The before/after/after/before comparison uses DolphinFS, strict durability,
1,000 simultaneously running conversations, four unique 8-KiB chunks at fixed
one-second arrivals, and equal 30-second deadlines. Baseline source
`ee62e08cd7a9bbf9b516174b0e72643bdfc6664979bdfcd3e8bfb0593202cf03`
adds fallback attribution only. All four runs complete exact 1,000-conversation
readback and offline integrity verification with zero request errors.

| P99 metric (seconds) | Before, two repetitions | Final, two repetitions | Budget |
|---|---|---|---|
| Submit to running | 4.519 / 5.003 | 4.980 / 4.834 | 5 |
| Streaming including queue debt | 12.508 / 12.568 | 11.497 / 10.958 | 1 |
| Terminal settlement | 11.598 / 11.693 | 11.892 / 10.302 | 5 |

Streaming improves about 8-13%; admission and settlement show no consistent
latency improvement. Capacity acceptance remains **FAILED**. Embedded
abandon/reopen is not running-process SIGKILL coverage. Combined embedded/client
peak RSS falls from 1,292 / 1,303 MiB to 1,217 / 1,179 MiB. Fallbacks fall from
5,245 / 5,377 to 2,793 / 2,973: snapshot-capacity fallbacks fall from 2,783 /
2,815 to zero. Final commit WouldBlock fallbacks remain 1,634 / 1,692;
snapshot-plus-commit rotation fallbacks remain 1,159 / 1,281. These counters
identify branches, not per-request wall-time shares. Fixed cumulative timers
are nested and cannot be added as independent foreground costs.

The cache-only intermediate runs did not establish a material latency win and
remain in the evidence. The paired 1,000-append follow-up records worse P99
(1.722 / 2.309 ms before, 2.473 / 3.604 ms final). A subsequent same-history
5,000-append comparison in the same interleaved order records overlapping P99
(2.029 / 2.180 ms before, 2.149 / 2.075 ms final). This does not erase the
smaller-sample regression or establish improvement in every dimension.

All 16 final operation phases complete for native, sidecar SQLite and raw
SQLite. Native point-read P99 remains 0.086 ms versus raw SQLite 0.010 ms;
durable footprint is 113.37 / 59.37 / 20.15 MB respectively. Small-suite tail
sample-sufficiency flags remain binding. Logical WAL traffic is 567 / 535 MB
and overlay materialization 364 / 341 MB in the two final capacity runs;
these are not physical device write-amplification measurements.

Final validation passes 1,393 native tests (2 ignored) and 78 Python tests.
An earlier full run failed because the shutdown-checkpoint fault test did not
reach its requested metadata-injection occurrence; three isolated repetitions
and the final full rerun pass. The original failure is retained. Existing
repository documentation gate failures are reported separately. Build receipts,
all failed/intermediate runs, complete scores, commands and integration backup
live under `.tmp/tofudb-read-reuse-20260930/`; missing raw evidence must not be
reconstructed from rounded summaries. The ownership/memory/error invariants and
rejected alternatives are documented in `STORAGE_TOFUDB.md`.

## Embedded measurement contract

The active-conversation and operation harnesses both accept
`--native-client embedded` to exercise the loaded native library through the
production Python adapter. Reports identify the executable, library file hash,
and the library's own source digest; an executable receipt alone does not prove
which embedded engine was measured. The application/daemon path remains a
separate selectable transport. In-process abandon/reopen is labeled explicitly
and cannot satisfy the process-crash coverage gate.

All active-conversation authorities now receive the same 30-second RPC deadline
from `conversation_concurrency_contract.json`. Startup and restart verify the
effective client deadline. The 5/1/5-second admission/stream/settlement P99
budgets remain unchanged. Earlier embedded comparisons silently inherited a
30-second native default and a 5-second legacy-sidecar default; those failed
legs remain retained but are invalid for engine ranking. A failed legacy
adapter request does not prove a limitation of the SQLite engine.

Capture the complete content-free storage metrics envelope at admission and
completion even without a running restart. Filtering out packer pacing and
maintenance concealed possible causes of rotation stalls. Embedded engine and
client share a process: their RSS observations must not be added together.
Small operation-suite sample counts remain explicitly insufficient for P99
certification where indicated; they cannot replace the fixed-arrival
1,000-conversation gate or turn an incomplete run into a latency win.

## Previous measured capacity boundary (2026-09-30, core write path)

That comparison fixes distinct-key overlay batching and removes global
structural drains from normal single-event projection-head cleanup. Baseline
source `94a908f3d3c83804…` adds counters only to the previous final source;
final source is `8bb8457bc12df8de…`, binary SHA
`0bf60538db1f6a609d2ee004289ce916d44453900dd547d0f7611450f78ff425`.
Both builds use identical instrumentation and workload paths except that the
new historical-position counter is absent from the baseline. Native before /
after / after / before runs use DolphinFS (`fuse.bgfuse`), strict durability,
1,000 running conversations, four unique 8-KiB chunks at fixed one-second
arrival intervals, and running/final SIGKILL recovery. No latency budget changed.

| P99 metric (seconds) | Before, two repetitions | After, two repetitions | Budget |
|---|---|---|---|
| Submit to running | 12.653 / 12.084 | 4.218 / 5.115 | 5 |
| Streaming including queue debt | 27.676 / 27.038 | 16.812 / 17.235 | 1 |
| Terminal settlement | 22.928 / 26.705 | 17.180 / 15.642 | 5 |

Overall capacity acceptance remains **FAILED**. Admission's second repetition
still exceeds its budget; both stream and settlement repetitions fail. All four
runs recover 1,000/1,000 exact completed contents and report zero unhealthy
probes. Native peak RSS falls from 1,734 / 1,723 MiB to 1,524 / 1,602 MiB.

Admission merges fall from 955 / 889 to 51 / 59 and encoded WAL bytes from
285 / 275 MB to 98 / 106 MB. In the restarted streaming/settlement epoch,
encoded WAL bytes fall from 787 / 770 MB to 480 / 461 MB; checkpoints fall
from 19 / 19 to 12 / 12. These are decimal MB and process-wide counters, not
physical write amplification. Normal terminal commits no longer perform
foreground tree batches, but semantic execution, background materialization,
and rotation parking still serialize visible progress. The intermediate
batching-only run and every failed final leg remain in the evidence bundle.

All 16 small-scale operation phases complete for TofuDB, SQLite sidecar and
raw SQLite. Native swarm-reader P99 is 3.54 ms versus sidecar 7.14 ms, but
history-bearing sidebar P99 is 3.193 ms versus 2.504 ms. Native durable footprint
is 115.58 MiB versus 56.68 MiB. These dimensions cannot be averaged into a win.
The 30-sample mixed-suite large-append P99 is 23.048 ms (previous suite 3.716 ms).
A fresh-authority 1,000-sample controlled large-append follow-up measures
baseline/final P99 4.535 / 4.578 ms and throughput 333.3 / 356.3 operations/s;
this does not erase the mixed-suite outlier or certify every history state.
Raw SQLite's orderly reopen is not compared with destructor-free crash recovery.

Final native tests: 1,358 passed, 2 ignored; benchmark verdict tests: 14 passed.
Fault tests cover exact merge coverage, checkpoint recovery, pinned snapshots,
owner isolation, concurrent insertion and atomic settlement/head deletion under
I/O failures and short writes. This is bounded synthetic-provider evidence,
not browser feedback, real-provider capacity or a sustained soak. Raw runs,
commands, identities and the complete operation comparison live under
`.tmp/tofudb-core-20260930/`; do not reconstruct missing raw evidence from
these rounded summaries.

## Previous write-path comparison (2026-09-30)

The source-snapshot comparison on DolphinFS (`fuse.bgfuse`, strict durability)
uses source digests `51eb5bc7ebf29f2b…` before and `7712a958e49a2c40…` after
blob staging, search scheduling, witnessed reads, and connection teardown fixes.
The final binary SHA is `a2655d187b2c4352c4dee77f3ce29089ba8124a3252ff36e0e113e612676c948`.
Two 1,000-conversation repetitions per build ran in before/after/after/before
order, with four 8-KiB streaming events per conversation, scheduled arrivals,
restart while running, and exact-content recovery. These gates remain **FAILED**:

| P99 metric (seconds) | Before, two repetitions | After, two repetitions | Budget |
|---|---|---|---|
| Submit to running | 12.04 / 11.61 | 12.07 / 15.20 | 5 |
| Streaming including queue debt | 34.57 / 32.49 | 21.14 / 27.26 | 1 |
| Terminal settlement | 27.47 / 20.84 | 19.36 / 17.35 | 5 |

All four runs recovered 1,000/1,000 exact contents with zero unhealthy probes.
This proves the measured recovery scope, not low-latency capacity or a sustained
soak. Admission did not improve; its second final repetition regressed. Serialized
command execution and rotation waits remain material bottlenecks. Nested stage
counters overlap and are not an additive foreground latency breakdown.

All 16 operation phases completed on the final build. Search corpus readiness was
8.0 s versus sidecar SQLite 10.8 s; native large-turn append P99 was 3.72 ms.
Native shutdown fell from ~31.6 s to ~1.62 s with both listener and client-pool
cleanup fixes, but this phase has only three observations. Concurrent-write reader
P99 was 13.72 ms versus SQLite 6.24 ms, and native data size was 141.24 MiB versus
56.70 MiB. Neither dimension passes. The native process's FUSE write counter
reported only 16 KiB: it cannot certify physical write amplification.
Raw runs, exact commands, build receipts, all operation scores, and failure
verdicts are retained in the ignored `.tmp/tofudb-latest-opt-20260929/` evidence
bundle; missing artifacts must not be replaced by these rounded summary numbers.

## Write-path diagnostic accounting

`commitStages` retains the aggregate `parallel_fallback` count and partitions it into `parallel_{snapshot,preparation,commit}_{busy,rotation}`. These six fixed counters record the actual fallback branch and have zero elapsed time; `busy` means `WouldBlock`, not necessarily a business OCC conflict. Rotation uses the engine's existing classified backpressure. Unsupported operations that never enter optimistic preparation do not increment these counters. Problem: the old aggregate conflated pin admission, preparation and commit failures. Decision: attribute both phase and classified rotation without changing retries or errors. Alternatives rejected: parsing arbitrary error messages into guessed conflicts and adding per-key labels/unbounded metrics. Preparation pin saturation now waits outside authority; `parallel_snapshot_wait` records those wait intervals separately from commands that actually fall back.

`conversation_concurrency.py` captures storage metrics once at the admission
barrier and again after streaming/settlement. A running restart resets native
process counters, so these epochs must be reported separately. Counters cover
all engines in the process, including the disposable search projection; they
are not per-authority physical-device I/O measurements.

- `walEncodedBytes`: successfully appended WAL bytes before durability barriers;
  neither device write amplification nor a count of acknowledged durable bytes.
- `overlayMergeCommits` / `overlayMergedEntries` / `overlayMergedPositions`:
  successful physical merges, distinct keys materialized, and historical delta
  positions consumed. Their ratios expose repeated-key batching failures.
- `overlayMaterializedBytes` / `overlayMaterializedBlocks`: newly staged page
  bytes and referenced blocks of successful merges. Reused durable blocks add
  references without newly staged bytes.
- `overlayWalPressureAttempts` / `overlayWalPressurePendingEntries`: hard
  backstop attempts made with a WAL above the overlay watermark and the summed
  pending-position counts seen at those attempts; attempts may fail.

Always retain the source/binary identity, resource budget, latency distribution,
correctness verdict, and original scheduled arrival model beside these counters.
Nested execution timers overlap and cannot be summed as independent elapsed time.

## What an agent harness actually does to its storage

The benchmark workload model is the repository's own operation mix, not a
synthetic KV loop. The dominant classes, in rough order of user-visible impact:

1. **Turn writes** (`turn.append_settled`, `turn.compact`, `turn.delete`) —
   the hot write path; every agent round trip lands here. Small, durable,
   strictly ordered per conversation.
2. **Sidebar / history reads** (`conversation.list`, `conversation.get`,
   windowed `turn.sync.snapshot`) — read on every UI paint and resume; latency
   here is directly perceived. Real conversations carry turn history, so the
   corpus must include history-bearing conversations, not headers only.
3. **Background state** (`record.put/get`, queue ops, timers, billing,
   scheduler) — small point reads/writes, latency-tolerant but throughput-
   sensitive under concurrency.
4. **Lifecycle maintenance** (checkpoint, reopen, GC, backup) — must be
   bounded and must not stall foreground work.

This is a **single-writer, append-heavy, read-your-writes, low-GB** workload.
It is *not* a general OLTP or analytics workload. A general-purpose database
(SQLite, PostgreSQL) pays for generality (SQL parse/plan, generic index
maintenance, multi-user concurrency control) that this workload never uses.

## The benchmark

`scripts/tofudb_perf_compare.py` drives **identical operation mixes through
each backend's production client stack** — not raw engine primitives:

* `tofudb` — the native production stack: either a `tofu-db serve` process over
  storage.v2 or `--native-client embedded` through the loaded C ABI and Python
  adapter. Every report records the chosen transport.
* `sqlite` — the legacy Python sidecar authority, SQLite backend
  (`lib.storage` supervisor + `lib/storage/client.py`), as personal mode runs.
* `rawsqlite` — bare in-process SQLite (WAL, `synchronous=FULL`), the same
  logical operation shapes executed as direct SQL with no supervisor,
  socket, or serialization layer. This is the **engine-floor baseline**,
  and it is mandatory in every comparison run: it bounds how much of each
  production stack's latency is its client and process boundary rather
  than its storage engine, and it retires the out-of-band micro-benchmark
  that once showed raw SQLite ~25× ahead per commit (see "Reconciling the
  raw baseline" below).

It reports per-workload P50/P95/P99, throughput, time-windowed samples and OS `/proc/<pid>/io` serving-window writes. `resources.<engine>.footprint` separately accounts for authority and external search projection: logical bytes, allocated bytes when supported, unique files, overlap, scan errors and complete/observed totals. The old `data_size_bytes` is only apparent path bytes under the original driver root; it excludes the external native projection and is not the total-footprint gate. Failed/unresolved scans cannot become complete totals. SQLite's broad project directory is not silently relabeled as a precise authority/projection inventory.
Footprints are post-workload, before-shutdown observations, not live-payload bytes or verified steady state. Maintenance/reopen evidence is retained separately; waiting for background work or seeing a checkpoint does not establish steady state. Native reports declare write-counter scope/completeness; embedded open/close/abandon/reopen windows fold once before resetting.
In-process counters cover the harness: an upper bound, not device IO or isolated database amplification. Daemon startup/shutdown tails and separate authority init are excluded.
Verified lifecycle accounting invalidates historical embedded totals reset across reopen; latency and footprint remain separate evidence.
PostgreSQL is not a benchmark dimension (see below).

### History-scaled phases

Two phases exist specifically because header-only corpora are blind to the
read paths production actually exercises:

* `open_windowed_long_history` — one conversation carrying `history_turns`
  (400 at `small`) turns; the measured op is the production open path,
  `turn.sync.snapshot` with `turn_limit: 20`. Any O(history) work behind an
  O(window) contract shows up linearly here.
* `sidebar_with_history` — `sidebar_hist_convs` conversations carrying
  `sidebar_hist_turns` turns each; the measured op is `conversation.list`
  with limit 50. Per-conversation history work inside list/hydration scales
  with summed history, not page size.

Corpus-recency discipline: list workloads return the newest conversations by
`updated_at`, so a phase's seeded conversations must carry the newest
timestamps in the run. A phase seeded with older timestamps silently
re-measures whatever newer (header-only) corpus an earlier phase left
behind — this actually shipped once (2026-09-19: `_SEARCH_TS`-based turns
ranked below `_TS_BASE`-based header seeds) and inverted a sidebar result
until caught. Any new list workload must state which conversations land in
the measured page and why.

### Cold-read families (2026-09-16/18 storm cases)

The `maintenance_reopen` phase additionally probes the boot warm-up family
set (`record.get/list`, `conversation.count/list`,
`task_results.recovery_pending`, `timer.active.count`, `credential.list`)
with a **cold read immediately after each restart and a warm read right
behind it**. The report carries per-family `cold_read`/`warm_read` latency
summaries; the timeout storm's signature is a cold read orders of magnitude
above its warm twin on a high-latency volume. An engine lacking a family is
reported as an explicit `cold_read_unsupported` gap, never a dropped sample.

The deterministic half of the same incident is gated in CI without any
wall-clock assertion: `packages/tofu-db/src/cold_read_tests.rs` counts VFS
io-ops (open/pread, i.e. potential network round trips) for a cold
`record.list`/`record.get` on a checkpointed store and asserts the ratchet
ceilings (11 / 3 io-ops, 0 warm). A regression in the cold path fails the
gate; every cold-path optimization must lower the ceilings in the same
commit. Wall latency per io-op stays environment-specific and belongs to
this harness's `cold_read` numbers, measured never gated.

The degraded-filesystem case is one command away — point the disposable
authority at the network mount:

```bash
mkdir -p data/benchmarks/fuse-parent
python3 scripts/tofudb_perf_compare.py --scale small --engines tofudb \
  --binary packages/tofu-db/target/certified/tofu-db \
  --authority-dir-parent data/benchmarks/fuse-parent \
  --output data/benchmarks/cold-read-fuse-$(date +%F).json
```

A healthy small-scale authority stays sub-millisecond even on FUSE (WAL
replay pre-warms it); the storm needs a store larger than the 32 MiB block
cache, which the Rust gate above reproduces deterministically instead of
seeding gigabytes into every benchmark run.

## Search preparation comparison boundary

Problem: periodic `turn.search.backfill` requests reset the SQL projection and
its cursor; accumulated corpora can repeatedly restart before becoming visible.
Decision: the benchmark schedules one rebuild and waits for both seeded hits
and ready complete-corpus coverage under the existing total preparation deadline.
It samples coverage before all hits appear and reports stabilization separately.
Alternative rejected: a 90-second unchanged coverage timeout does not witness a
stalled worker; diagnostic counters showed continued processing and no failures.

## Win conditions — explicit, falsifiable, per-workload

Tofu-DB is "comprehensively better" only when **all** of these hold, measured
on the same hardware in the same run, against a freshness-verified binary:

| Dimension | Gate | Why this bar |
|---|---|---|
| **Interactive write** (`turn.append_settled`, `record.put`, `conversation.create`) | P99 ≤ sidecar-SQLite P99 | Writes are the harness's hot path; we must not regress the perceived responsiveness of the agent loop. |
| **Interactive read** (`conversation.list`, `conversation.get`, `record.get`) | P50 ≤ sidecar-SQLite P50 | Read latency is what the user feels on every paint. |
| **History-scaled read** (`open_windowed_long_history`, `sidebar_with_history`) | P50 ≤ sidecar-SQLite P50, flat vs history length | The real sidebar/open corpora carry turns; wins here matter more than header-only micro-ops. |
| **Search** (`conversation.search`) | query P50 and complete-corpus visibility time ≤ sidecar-SQLite | Report indexing stabilization separately; a fast query after minutes of unmeasured preparation is not a search win. |
| **Mixed 80/10/10** | throughput ≥ sidecar-SQLite | The realistic blended load. |
| **Footprint** (complete authority + projection logical bytes; allocation/write counters reported separately) | total data size ≤ **1×** sidecar for the same logical corpus and lifecycle phase | Smaller is better, full stop: every durable byte is FUSE pain, backup time, and cache pressure, bounded against runaway growth (AGENTS.md constraint 3). A purpose-built store has no excuse for a larger footprint than the embedded engine it replaces — parity is the loosest acceptable bound, and every byte above it must carry a named structural cause in this charter. |
| **Engine floor** (`rawsqlite` baseline) | every operation comparison includes raw SQLite; P50 > 2× names its structural cause but still fails the numeric gate | The client stack is ours and the engine is ours; an unexplained gap to the bare embedded engine is unpaid debt, and it must stay visible in every baseline table instead of hiding behind sidecar comparisons. |
| **Maintenance** (checkpoint + reopen) | wall ≤ sidecar-SQLite | Bounded, non-blocking lifecycle. |
| **Degraded filesystem** (FUSE/network mount) | **strict win** | Verify locking, acknowledged recovery, and latency on the actual mount for both engines; the FUSE label alone proves neither failure nor superiority. |
| **Distracted hardware** (CPU < 4 cores, memory pressure) | no P99 blow-up vs idle | A personal PC is not a server; the OS and browser compete. |
| **Horizontal** (writer + N concurrent readers) | reads must not block writer | WAL-readers + single-writer is the harness's real concurrency shape. |

| **Swarm-write tails** | writer and reader P99 ≤ sidecar-SQLite with sufficient equal populations | Fast writer averages cannot hide reader starvation. |
| **Active-conversation capacity** | complete paired application acceptance below | Storage micro-ops do not prove 1,000 running attempts. |
A single global "N× faster" number is meaningless and is explicitly rejected:
it lets a 50× search win hide a 25× write regression. Each dimension must pass
on its own.

### Executable assessment, not another benchmark

[`storage_performance_charter_v1.json`](../contracts/storage_performance_charter_v1.json) owns the operation-to-dimension rules. `scripts/tofudb_charter_assess.py` reassesses raw receipts without launching an engine or opening a database: `python3 scripts/tofudb_charter_assess.py <operation-report.json> ... <paired-report.json> --json-output <new-assessment.json> --markdown-output <new-assessment.md>`. Exit 0 means every gate passes; 1 retains non-pass evidence; 2 refuses malformed input or conflicting output. Existing outputs are never overwritten with different content.

For new operation receipts, explicitly add `--capture-provenance --native-client embedded` to `tofudb_perf_compare.py`; collect all three engines on both actual volumes, at least two independent same-work repeats per volume with reversed engine order. Capture occurs outside timed work. It reuses native build-info/fingerprints and the certification source inventory, plus measured benchmark, delivery, executor and resource-policy owners. Binary/library bytes, complete start/end source inventories, observed strict/FULL/profile-off policy, host/affinity, work and actual volume devices must agree; requested settings or a supplied `passed` boolean are not proof.

The five outcomes are **PASS**, **FAIL**, **INSUFFICIENT**, **INCOMPARABLE**, and **HISTORICAL**. Each dimension retains observations and reasons; no averaging erases an adverse repeat. Stable old evidence remains historical, not a current win or current regression. Missing tails, coverage or provenance remain explicit. History-length scaling, actual-volume recovery, idle-versus-pressure P99 and writer-versus-reader scaling still need joined measurements; their unautomated clauses remain insufficient even when numeric subgates pass. Raw SQLite has narrower semantics: the 2× diagnostic excludes incompatible lifecycle boundaries, not lifecycle acceptance. This assessment is neither power-loss certification nor a deployment decision.

Verification: `tests/tofudb/test_tofudb_charter_assess.py`, `test_tofudb_footprint_accounting.py` and the conversation provenance/acceptance tests. Correctness tests use synthetic metrics, never wall-clock performance thresholds. A green test suite proves assessment behavior, not that measured product gates passed.

## What we deliberately do NOT benchmark

* **PostgreSQL as a gate.** Zero-configuration Tofu cannot require a server dependency. `--postgres-dsn`/`--postgres-reset` permit only explicitly disposable `tofu_perf*`/`tofu_bench*` databases as informal same-host, default-configuration references, never tuned/production PostgreSQL claims.
* **Raw micro-ops** (single-row insert loops, pure KV). We measure the real operation catalog. Cross-engine microbenchmarks are admissible only through one shared same-host harness with symmetric durability.
* **Averaged or dimension-dropping win claims.** Every gated dimension, both volume classes and sidecar/raw-floor baselines must hold; dropping one cannot establish victory.

## Measurement discipline

* **Attribution controls.** Calibrate sensitive workloads with balanced repeats   of identical executable/library artifacts before interpreting small deltas.   Preserve every window and failed gate. Source digests, build context, daemon   readiness and client initialization are separate witnesses; follow   [recovery qualification](TOFUDB_RECOVERY_ATTRIBUTION.md#build-and-startup-boundaries). * **Freshness gate.** Default binary discovery verifies the embedded   `--build-info` source digest against the retained checkout before measurement;   an old executable fails closed. `--binary` permits an intentional historical   comparison, whose report records `tofudb_build_identity`,   `checkout_source_sha256`, and `source_matches_checkout`. Binary SHA pins keep   a run reproducible; neither that SHA nor file mtime proves source freshness.   Build from a recorded current-source snapshot, retain its digest and the   executable SHA, then keep that executable immutable throughout the run.   Embedded runs also retain the loaded library hash and self-reported source   digest; historical ABIs without that export report identity unavailable.   `cargo build --release` from `packages/tofu-db/` uses the package's pinned C   compiler. A source edit during a run does not turn its old binary into latest.
* **Complete lifecycle.** Report shutdown, reopen, and first read separately.
  `maintenance_reopen`'s top-level latency is reopen only. The native harness
  closes its application pool before parent EOF and counts that cleanup in
  shutdown. Native connection teardown also wakes blocked reads and joins all
  workers even if a peer remains connected. Raw SQLite close/reopen and native
  embedded abandon/reopen are explicitly labeled controls, not SIGKILL crash
  certification; incompatible scopes have no speedup ratio. Process write
  counters on FUSE do not prove physical write amplification. Cold probes carry
  the same explicit owner as ordinary reads.
* **Same volume, same run.** All engines for a given comparison run on the
  same filesystem in the same process, so volume class is controlled.
* **Two volume classes.** `/tmp` (local, fast) and the repo's FUSE mount
  (slow/network) are the two deployment realities; both are measured.
* **Windows, not just percentiles.** A global P99 can hide a slowdown that
  grows with corpus size; the 10-window breakdown catches history-dependent
  degradation.
* **Load model declaration.** The harness is a closed-loop sequential client:
  that is the product's real shape (one user driving a synchronous UI), but
  closed-loop clients under-report tail latency under saturation
  (coordinated omission). The `swarm_write`/`concurrent_read` phases add
  concurrency; any future saturation claim requires an open-loop offered-load
  sweep, not this harness.
* **Durability symmetry.** Cross-engine latency is only compared at
  equivalent durability: `tofudb --durability strict` (fdatasync per commit)
  vs the sidecar's SQLite `synchronous=FULL` journal. The report records
  `equivalent_durability`; `--durability normal` numbers must never be
  compared against strict-mode baselines.
* **Stage attribution.** `TOFU_DB_FSYNC_STATS=1` makes the daemon print
  per-stage call counts and nanos at shutdown (`semantic`, `apply_batch`,
  `wal`, `checkpoint_if`, `drain_step`, `history_stage`, `pack_retire`, …).
  Every write-path optimization hypothesis must be stated in these stage
  terms and re-measured after the change.
* **No wall-clock CI gates.** This is a measurement tool, not a timing gate
  (per the script's own docstring). Results inform optimization; they do not
  fail CI.

## Measured baseline (2026-09-19, production payloads)

Binary built from current sources (`cargo build --release`), `--scale small`,
local fast volume (`/tmp` RAID), same run vs the legacy SQLite sidecar,
`--retry-backpressure` (production retry policy). Report:
`data/benchmarks/baseline-2026-09-19.json`. All read workloads send the
payloads production actually sends (see "Payload fidelity" below).

| workload | tofu-db p50 | sqlite p50 | verdict |
|---|---|---|---|
| record_put_burst | 0.43ms | 0.67ms | **1.6× faster** |
| record_get_points | 0.19ms | 0.29ms | **1.5× faster** |
| conversation_create_burst | 0.65ms | 0.65ms | par |
| sidebar_list_page | 2.40ms | 1.27ms | **0.55× slower** (see finding S3) |
| conversation_open (`message_window: 50`) | 0.21ms | 0.36ms | **1.7× faster** |
| turn_append_burst | 1.09ms (p99 7.80) | 1.36ms (p99 1.79) | p50 faster, **p99 loses** |
| conversation_search | 1.25ms | 1.54ms | **1.2× faster** |
| mixed_80_10_10 | 0.22ms (p99 3.94) | 0.33ms (p99 1.67) | p50 faster, **tail loses** |
| open_windowed_long_history | 0.69ms | 1.45ms | **2.1× faster** |
| sidebar_with_history | 3.97ms | 1.60ms | **0.45× slower** (see finding S3) |
| transcript_bulk_hydration | 13.6ms | 22.6ms | **1.6× faster** |
| concurrent_read | writer 442 op/s, reader p50 1.15ms | writer 182 op/s, reader p50 1.78ms | **2.5× faster** |
| swarm_write | 3.84ms (aggregate 400 op/s) | 5.00ms (530 op/s) | p50 faster, aggregate loses (variance) |
| turn_append_large | 4.91ms | 4.24ms | 0.85× slower |
| maintenance_reopen | 624ms, first read 0.74ms | 1109ms, first read 6.67ms | **1.8× faster; first-read 9×** |
| crash_recovery (SIGKILL mid-load) | 339ms, zero loss | 1141ms, zero loss | **3.2× faster** (standalone tiny-scale: 10.2ms vs 1.19s, **115×**) |

Physical footprint (same run, all phases):

| metric | tofu-db | sqlite sidecar |
|---|---|---|
| data size after run | 1,327 MiB | 56.7 MiB (**23× smaller**) |
| process write volume | 1,940 MiB | 580 MiB (**3.3× less**) |
| isolated 2,000-append probe (442 KiB logical) | 369.5 MiB / 442.8 MiB written (**855×/1025×**) | 36.1 MiB (83×) |

Deterministic write-amplification ratchet (CI,
`src/write_amplification_tests.rs`): 1 VFS write op per append (block
materialization is fully deferred) and a WAL-growth sawtooth of ≈48 KiB per
append on average, 62,632 bytes peak in a 50-append window, for a ~60-byte
logical projection — every commit rides ≈12 rewritten 4 KiB CoW pages.
History-scaled read probes: windowed `turn.sync.snapshot` is O(window) and
flat on both engines (tofu-db 0.73ms vs 1.65ms @400 turns); unwindowed reads
are O(history) on both (8.9ms vs 18.4ms @400).

## Measured baseline (2026-09-20, overlay on, three engines)

Same-binary A/B (`TOFU_DB_WRITE_OVERLAY=0` vs `=1`, SHA256-pinned binary),
`--scale small`, local fast volume, `--retry-backpressure`, three engines.
Reports: `data/benchmarks/baseline-2026-09-20-overlay-default-on.json`
(overlay on, the production `.env` configuration) and
`overlay-ab-2026-09-20-off.json` (legacy path).
Certification with the overlay forced on: contracts 354 + application 36 +
browser-sqlite 10 + browser-tofudb 10 PASS (`scripts/certify_storage_backends.py`);
native differential 178/178 and application contract 36/36 re-verified.

Client-transport follow-up (same day): the production `StorageV2Client`
shed one-session-per-RPC for a bounded persistent-session pool
(`lib/storage/v2_client.py::_SessionPool`, capacity from the daemon's
readiness `maximumConnections`). Probe attribution (loopback, this host):
per-RPC session setup cost 136 µs — the adapter, not the engine or the
wire, was the hot path's dominant term. Pooled no-op RPC 177 → 73 µs;
in-daemon point-read execution is ≈6 µs vs the raw-SQLite floor's ≈5 µs,
so the engine itself is already at the embedded floor and the remaining
gap is transport. The table below is the pooled production path, report
`data/benchmarks/pooled-client-2026-09-20.json`:

| workload | tofu-db p50 (p99) | sqlite sidecar p50 (p99) | rawsqlite floor p50 | verdict vs sidecar |
|---|---|---|---|---|
| record_put_burst | 0.16ms (0.28) | 0.55ms (0.85) | 0.07ms | **3.5× faster** |
| record_get_points | 0.08ms (0.09) | 0.22ms (0.37) | 0.006ms | **2.7× faster** |
| conversation_create_burst | 0.20ms (0.29) | 0.60ms (1.11) | 0.05ms | **3.1× faster, p99 3.9×** |
| sidebar_list_page | 0.60ms (0.87) | 1.19ms (2.11) | 0.12ms | **2.0× faster** |
| conversation_open (`message_window: 50`) | 0.09ms (0.12) | 0.34ms (0.78) | 0.02ms | **3.6× faster** |
| turn_append_burst | 0.36ms (0.69) | 1.31ms (1.71) | 0.11ms | **3.7× faster, p99 2.5×** |
| conversation_search | 0.69ms (0.82) | 1.44ms (1.87) | 0.09ms | **2.1× faster** |
| mixed_80_10_10 | 0.09ms (0.71) | 0.32ms (1.70) | 0.006ms | **3.7× faster** |
| open_windowed_long_history | 0.60ms (0.70) | 1.39ms (2.65) | 0.03ms | **2.3× faster** |
| sidebar_with_history | 0.53ms (0.63) | 1.52ms (2.83) | 0.11ms | **2.9× faster** |
| transcript_bulk_hydration | 9.3ms (27.8) | 21.7ms (29.5) | 0.67ms | **2.3× faster** |
| concurrent_read | reader p50 0.96ms, writer ~700 op/s | reader p50 1.90ms (p99 32.5), writer ~185 op/s | reader p50 0.40ms | **writer 3.8×, reader 2×** |
| swarm_write | p50 1.91ms (p99 81.7), aggregate 908 op/s | p50 4.60ms (p99 16.3), aggregate 572 op/s | p50 0.13ms (p99 33.7) | **p50 2.4×, aggregate 1.6×; tail loses (see below)** |
| turn_append_large | 2.88ms (9.67) | 4.37ms (22.31) | 1.15ms | **1.5× faster, p99 2.3×** |
| maintenance_reopen | 10.7ms, first read ~0.2ms | 524ms | 0.21ms | **49× faster** |
| crash_recovery (SIGKILL mid-load) | 33.4ms, zero loss | 546ms, zero loss | 1.1ms (in-process floor) | **16× faster** |

Overlay A/B attribution (same binary, same run shape): interactive-write p99
moved create 6.63 → 0.44 ms, append 6.98 → 1.65 ms, large-turn 18.9 → 5.2 ms;
swarm aggregate 580 → 878 op/s; crash recovery 387 → 145 ms; data size
1,440 → 218 MiB; process writes 2,126 → 532 MiB.

Physical footprint (pooled-client run, all phases; the graceful-stop overlay
merge + checkpoint that shipped in the same window shrinks what the run
leaves behind):

| metric | tofu-db | sqlite sidecar | rawsqlite |
|---|---|---|---|
| data size after run | 113.5 MiB | 56.6 MiB | 19.2 MiB |
| process write volume | 321 MiB | 585 MiB | 1,376 MiB (upper bound, includes FTS merge churn) |

Historical scope of the preceding table: its build passed the listed operation
latencies and the then-measured FUSE phases. It does not certify current sources,
index readiness, complete shutdown, or 1,000 active streaming conversations.
The historical unresolved gates below remain visible; newer evidence must
reassess every dimension against a current build:

* **Footprint ≤1× (OPEN, 2.0×).** Down from 3.8× on the graceful-stop
  merge; process write volume is now the LOWEST of the three engines
  (321 vs 585/1,376 MiB). Remaining cause: content-addressed 4 KiB page
  framing plus WAL/staged-block retention until checkpoint/merge — the
  harness measures post-run size with no GC round. Closing it is the P4
  pacer/GC-rebalance work (merge-before-measure plus retention trim), not
  new write-path structure.
* **Engine floor ≤2× (OPEN, 2.7–11× by workload; one workload now BEATS the floor).** The embedded in-process transport landed 2026-09-20 (`packages/tofu-db/src/embed.rs` + C ABI + `tofu_db/embedded.py`; `--native-client embedded` in the harness): every request executes through the same extracted `server::dispatch_one_request` as the framed daemon, minus the socket. Measured (`data/benchmarks/embedded-2026-09-20.json`, small, /tmp, overlay on): record.get 44 µs (2.7× floor), record.put 120 µs (2.7×), conversation_open 58 µs (2.9×), and **concurrent_read p50 0.157 ms BEATS the raw floor's 0.357 ms** (read-replica lane + Rust execution under concurrency). Remaining gaps: search 11× (projection store vs FTS5-in-process), hydration 21× (hydration layout), create 9× (receipt/outbox envelope per op). The transport is harness-verified (differential 178/178, application contract 36/36, lib suite clean); production wiring (supervisor integration + certification) is the follow-up — the floor gate stays OPEN until the production path ships it, never by weakening the durability or ownership semantics the envelope buys.
* **Swarm write P99 (OPEN, 26–41 vs 14–18 ms; was 76–82).** Root-fixed 2026-09-20 in three parts: (1) the write fence that quiesced every commit from rotation-prepare to install-end is gone — the covered staged set is now a separate pending generation (`rotation_draining`) drained independently by the packer, so window commits flow through the encode and only the hard WAL ceiling binds, exactly as the install's final-segment design always anticipated; (2) a deferred install now parks the fully encoded (snapshot, segments) pair instead of re-encoding the covered prefix on every publication round trip, and the driver starts on a prepare signal instead of a 10 ms poll; (3) two invariant violations the load exposed are pinned by regression tests: window-era history staging is frozen for the whole parked period (the manifest hash chain can never orphan), and fresh window restaging of a covered block content-address-dedups across pending generations (the pack batch can never fail its duplicate-ID check). Three consecutive small-scale suite runs: writer p50 2.10–2.45 ms vs sidecar 4.66–5.16 (**2.2× win**), aggregate 763–952 vs 544–593 op/s (**1.4–1.7× win**), p99 26.0/40.9/37.6 ms — the residual is the bounded install window (encode+drain+swap) of a full 60 MiB rotation landing mid-burst; rawsqlite's own single-writer tail spans 9–34 ms on the same phase. p99_sample_count_sufficient is false on this phase, so the number is indicative, not gated.

  Update 2026-09-22 (install-hold + rotation-drain storms): the small-scale FUSE phase regressed to p99 415–543 ms (p50 ~1.7 ms) with every writer stalled at the same wall-clock instant. system.metrics correlation pinned one rotation checkpoint crossing the stall window (~460 ms continuous authority contention) and, in a worse run, the packer publishing 8 segments/33 MiB mid-rotation (~1.5 s): the install's fsync chain runs under the authority hold, and the quiet-gated packer lets ~32 MiB of staged blocks accumulate under a hot foreground, so the install's drain precondition inherited that whole backlog as one fenced storm. Fixes: the driver pre-creates the next-generation WAL off the mutex, install blocks write through the loose path (no dedupe probe read), and — the decisive change — the commit path requests the background drain at half the WAL window so the backlog is retired before rotation (a window pre-encode variant was measured and REVERTED: it lengthened the fenced cycle). Three reruns: swarm p99 21.9–35.6 ms, zero >100 ms stalls, aggregate 1,126–1,195 op/s (was 228–314). Full-suite head-to-head (`data/benchmarks/baseline-2026-09-22-optimized.json`): 15/16 workloads faster (1.41×–16×); record_put p99 0.54 vs 0.78 ms and turn_append p99 1.34 vs 3.00 ms now win; swarm p99 21.3 vs 15.2 ms (was 489 vs 14.75); footprint 1.76× (was 2.04×). Remaining open gaps: turn_append_large (p99 25.3 vs 13.9 ms), footprint ≤1×, engine floor, and a decisive (not weather-parity) swarm p99 close.

## Measured baseline (2026-09-23, embedded transport, floor decomposition)

Binary built from current sources (`cargo build --release`), `--scale small`,
local fast volume (`/tmp`), `--retry-backpressure`, three engines,
`--native-client embedded`. Reports:
`data/benchmarks/embedded-2026-09-23.json` (pre-fix) and
`data/benchmarks/embedded-2026-09-23-optimized.json` (this section's fixes).
Against the sidecar all 16 workloads are faster (1.45×–83×). Against the raw
floor, two workloads reached the ≤2× gate this run and every other gap
carries a named structural cause below.

| workload | tofu-db p50 | rawsqlite p50 | gap | structural cause |
|---|---|---|---|---|
| record_put_burst | 0.123ms | 0.066ms | **1.9× ✓** | — |
| turn_append_large | 2.43ms | 1.23ms | **2.0× ✓** | — |
| turn_append_burst | 0.255ms | 0.112ms | 2.3× | T1+T3 |
| concurrent_read | 0.83ms | 0.33ms | 2.5× | T1; aggregate throughput still favors tofu-db (read-replica lanes) |
| conversation_open | 0.054ms | 0.018ms | 3.0× | T1 |
| conversation_create_burst | 0.17ms | 0.052ms | 3.3× | T1+T4 |
| sidebar_list_page | 0.63ms | 0.12ms | 5.4× | T1+T2 |
| sidebar_with_history | 0.73ms | 0.11ms | 6.6× | T1+T2 |
| record_get_points | 0.039ms | 0.006ms | 6.5× | T1 |
| mixed_80_10_10 | 0.043ms | 0.006ms | 7.2× | T1 |
| conversation_search | 0.81ms | 0.097ms | 8.3× | T1+T2 (gram projection vs in-process FTS5) |
| transcript_bulk_hydration | 16.0ms | 0.78ms | 20.5× | T2 (cold per-document burst, by design; steady-state rides the sealed archive) |
| open_windowed_long_history | 1.04ms | 0.034ms | 30.6× | T2 (same cold per-doc path) |
| maintenance_reopen / crash_recovery | 24.8ms / 1067ms | 0.25ms / 1.3ms | n/a | T5 |

Named causes (the floor gate's required disclosure):

* **T1 — Python/FFI transport boundary ≈26 µs/op fixed.** cffi call,
  request/response codec, and the FFI handle's detached-worker handoff
  round trip; the engine's own in-process point read is ≈6 µs. Halved
  2026-09-23 from ≈50 µs by reusing per-thread response buffers in
  `tofu_db/embedded.py` (a fresh 1 MiB zero-filled buffer per execute cost
  ≈23 µs). Further closure requires removing the watchdog handoff, a
  correctness surface this slice deliberately did not touch.
* **T2 — serde_json `Value` materialization on document reads.**
  perf attribution (2026-09-23, 45K samples): ≈1/3 of read-path CPU is
  `Value` parse/BTreeMap-insert/re-serialize/drop — ≈30 µs per turn
  document, diffuse with no single hotspot. Attempt documents on the same
  path are now spliced as `RawValue` (2026-09-23: stored canonical bytes
  embed verbatim, sort keys lifted by a field-skipping derive; differential
  181/181 byte-identical). Turn documents themselves still parse
  (`public_turn` is a real transformation). The durable fix for bulk/window
  reads is the sealed transcript archive; the cold burst stays per-document
  by design.
* **T3 — write-path envelope** (receipt + outbox + OCC witness per op).
* **T4 — create envelope** (T3 plus the tenant-global ID claim).
* **T5 — lifecycle ops are structurally incomparable to the floor.** The
  raw baseline never leaves its own process, so reopen/crash-recovery
  "gaps" price process startup, not engine work; the sidecar comparison
  (24.8ms vs 524ms reopen) is the meaningful axis.

Corpus-fidelity note (payload discipline): the harness's
`turn.append_settled` corpus carries `currentAttemptId: null` — faithful
for ingested/human turns (`append_settled_turn` "without creating an
executor attempt"), but it means assistant-turn attempt materialization
(the T2 splice) is invisible to `open_windowed_long_history` today. A
corpus extension seeding attempt-bearing turns is the follow-up. The
default-transport decision landed 2026-09-23: `TOFUDB_TRANSPORT=auto`
resolves embedded everywhere except observed network mounts
(`docs/STORAGE_TOFUDB.md`), so production personal deployments on local
volumes now ride this embedded path by default.

## Payload fidelity (2026-09-19 correction)

Two harness defaults silently measured non-production contracts and have been
fixed; every future workload must state which production call site it models:

* `conversation.list` **defaults `include_messages` to true** at the engine
  boundary — full per-conversation transcript hydration (O(Σhistory)). The
  production sidebar always passes `include_messages: false`
  (`lib/conversations/catalog.py`). The harness's sidebar workloads sent
  neither flag for months, so "sidebar" numbers were really hydration
  numbers. Sidebar ops now pass `include_messages: false`; the bulk contract
  is measured separately as `transcript_bulk_hydration`.
* `conversation_open` used bare `conversation.get` (unbounded history).
  Production opens a bounded window (`repository.get(message_window=…)`);
  the workload now sends `message_window: 50`.

## Findings (2026-09-19, evidence-backed)

* **S2 (shipped): windowed message reads.** `legacy_messages` read the full
  main lane to serve any window. It now reads only the windowed lane-index
  range (order, never ordinal arithmetic; the maintained lane count supplies
  `total`; any count/lane disagreement falls back to the exact historical
  full read). `conversation.get message_window=20` at 400 turns: **3.25ms →
  0.33ms (9.9×)**; equivalence/fallback ratchets in `turn_tests.rs`.
* **S3 (RESOLVED 2026-09-19): per-owner recency directory.** The
  structural fix landed as `docs/TOFUDB_RECENCY_DIRECTORY.md`
  (commit 05f99685f): one derived head-128 document per owner maintained
  in the same transaction as every recency-affecting mutation, with
  coverage-gated fallback to the exact legacy path. Sidebar p50 moved
  2.35–2.42 → 0.72–0.73 ms (`sidebar_list_page`) and 3.97–4.07 →
  0.75–0.76 ms (`sidebar_with_history`) on both volume classes — from
  0.55×/0.45× behind the sidecar to 1.7×/2.1× ahead, with ≤0.07 ms
  per-append maintenance cost. Reports:
  `data/benchmarks/recency-s3-2026-09-19-*.json`.
* **S4: burst writes can escape rotation backpressure to the user.** A
  ~500-append seed burst exhausts the client's bounded retry budget and
  surfaces `database_busy` (WAL hard ceiling while a rotation is in flight).
  Production policy must keep classified-retryable retries on
  (`--retry-backpressure` in the harness); the overlay proposal removes the
  cause (WAL grows ≈4× slower when commits stop carrying page copies).
* **W1 (ROOT-FIXED 2026-09-20): entity CoW write amplification.** ≈36
  content-addressed pages per turn append; every block byte written ≥3
  times (WAL-carried, loose staged, packed segment); 855×/1025×
  space/write amplification vs the sidecar's 83×. All red cells in the
  write rows derive from this. The architectural fix
  (`docs/TOFUDB_WRITE_PATH_OVERLAY.md`, measured below) ships in
  production via `TOFU_DB_WRITE_OVERLAY=1`; default-enable gates on the
  lib-suite mode triage in "Next steps".

## Optimization targets (ordered by measured pain × leverage)

1. **Write-path overlay ("the WAL is the database")** — IN PRODUCTION
   VIA OPT-IN (`TOFU_DB_WRITE_OVERLAY=1`) 2026-09-20, see
   `docs/TOFUDB_WRITE_PATH_OVERLAY.md`. Commit =
   WAL append; entity mutations serve reads from a bounded overlay;
   checkpoint batch-merges. Targets: write p50 → ~0.2ms, p99 → near p50,
   WAL bytes/commit ≥4× down, footprint to the sidecar's order,
   backpressure escapes gone.
2. **Sidebar cardinality path (S3).** Post-overlay namespace consolidation:
   one recency-directory document per owner instead of per-append updated-
   index rekeys (no tombstone history, no read-all+sort).
3. **`turn_append_large` (0.85× → 1.3× repetitive, 2026-09-19).** The carried-block spill path is NOT the cost (`put_deferred` 6.6 µs, `put_stage` 0.6 µs); the deficit is inline-JSON payload processing, split by stage: admission (outer parse + deep clone + canonical re-encode of the nested projection/settlement documents) and execution (projection parse, search chunking, document store) in `turn.rs`. The admission half is fixed: `compile_turn` now splits `projection`/`settlement` off as `RawValue` slices before the typed parse (   `semantic_executor.rs`), normalizes top-level keys without unescaping/cloning/re-escaping megabyte-class strings, and preserves the producer's exact bytes (semantic-identical to the legacy normalization, proven by tests in `semantic_executor_tests.rs`). The receipt canonical bytes/digest/outbox are computed over the intact raw map (`canonical_request_raw`), byte-identical to the typed path for the compact wire producers — receipt replay/conflict fencing is unchanged (differential 178/178 both flag states). Measured p50 with the receipt-correct build: repetitive 3.20 → **3.07 ms (beats the sidecar's 4.24 ms)**, escaped ≈13 ms (par; the run-to-run spread on this shared host is ±10%, and the admission share was ~0.3–0.4 ms of the total). Remaining cost is execution-side (escaped pattern: ir_steps ≈5 ms) plus wire escape inflation (up to 1.5 MB for adversarial payloads) — the structural fix is routing oversized payload fields through the owner-bound document/blob codec instead of the inline IR literal, assigned to the `turn.rs` codec owner (the file is under active concurrent work as of this writing).
4. **Write p99 (7.8ms vs 1.8ms).** The foreground deficit-pacing tax
   (`checkpoint_if` ≈79µs/commit, `drain_step` ≈2.2ms on threshold
   crossings); subsumed by target 1.

The benchmark's job is to tell us *where* the engine's fixed costs live. The
earlier hypotheses have each been tested and retired:

1. ~~Double durability barrier~~ — the strict-mode group WAL barrier now
   shares one fsync across concurrent commits (wait outside the authority
   mutex); p50 write latency confirms the barrier is amortized.
2. ~~Group-commit sequencer wait~~ — the sequencer now skips the batching
   window when no submission is in flight, so single-writer streams do not
   pay it.
3. **Rotation pacing on the commit path** — the surviving cost, per the
   stage attribution below: bounded history-drain steps and checkpoint
   installs ride the same commits that cross the rotation thresholds.

Each optimization is gated by the existing certification matrix (durability
must not shrink) and re-measured here. The sequence is: profile → make one
bounded change → re-run the fault matrix → re-run this benchmark → compare.

## Write-path overlay, measured (2026-09-19, target 1 landed)

Same binary, same host, back-to-back A/B (`TOFU_DB_WRITE_OVERLAY` off vs
on), `--scale small --retry-backpressure --engines tofudb`, on BOTH volume
classes. Reports: `data/benchmarks/overlay-ab-2026-09-19-*.json`.

| workload | local OFF | local ON | FUSE OFF | FUSE ON |
|---|---|---|---|---|
| record_put_burst p50 | 0.41ms | 0.33ms | 0.56ms | 0.47ms |
| conversation_create p50/p99 | 0.59/2.42ms | 0.34/1.23ms | 0.80/2.26ms | 0.57/0.71ms |
| turn_append_burst p50/p99 | 1.19/4.65ms | 0.58/1.17ms | 1.52/4.87ms | 0.76/1.00ms |
| swarm_write | 4.07ms, writer 127 op/s | 1.62ms, writer 443 op/s | **ERROR (S4 escape)** | 2.08ms, writer 338 op/s |
| turn_append_large p50 | 5.21ms | 3.69ms | **ERROR** | 5.25ms |
| maintenance_reopen | 624ms | 359ms | **ERROR** | 362ms |
| crash_recovery | 463ms | 408ms | **ERROR** | 404ms |
| data size (post-run) | 1,391 MiB | 192 MiB (**7.2×**) | 984 MiB | 167 MiB (**5.9×**) |
| process writes | 1,745 MiB | 246 MiB (**7.1×**) | — | — |

Reads hold or improve everywhere (the read lane is untouched); the sidebar
rows (S3) are flat by design pending the post-overlay consolidation.

The headline is the last column against the second: **the overlay on FUSE
beats the legacy engine on the local RAID across the write workloads** —
turn_append_burst p50 0.76 vs 1.19 ms and p99 1.00 vs 4.65 ms, swarm_write
2.7× throughput, maintenance_reopen 1.7× faster, with 8.3× smaller
footprint. The four workloads that errored on FUSE-off (S4 backpressure
escapes and the deadline-derived failures) all complete on FUSE-on. This
is the degraded-volume fluency gate the "Next steps" demanded, and it is
the evidence for "the WAL is the database" fixing W1 at the root.

Target check: write p99 → near p50 (HIT: 1.17 vs 0.58 local, 1.00 vs 0.76
FUSE); ≥4× fewer WAL bytes per commit (HIT: 7.9×, flat with history);
footprint to the sidecar's order (HIT: 192 MiB vs sidecar's 56.7 MiB same
run, vs 1,391 MiB legacy); backpressure escapes (HIT: gone on FUSE).
Write p50 → ≈0.2 ms is a PARTIAL (0.58 ms local): the remaining cost is
semantic execution and the envelope, not tree I/O — a P4/profile topic,
not a correctness one.

## Reconciling the raw baseline

A prior out-of-band micro-benchmark (single-row `INSERT` via Python `sqlite3` vs `tofu-db certify-resource-gate`) showed SQLite ~25× faster per commit because it bypassed the client stack and exercised a different durability policy. That comparison is **retired as evidence**: the in-harness `rawsqlite` engine now measures the bare-SQLite floor on the *same* logical operation mix, the *same* machine, and *symmetric* durability (WAL + `synchronous=FULL`), in every comparison run. The two baselines answer two different questions and both are first-class: the sidecar baseline prices the production path personal mode ships today; the raw baseline prices the embedded engine itself and bounds the client-stack tax both production stacks pay. A "why does Tofu-DB exist" review must quote both, plus the dimensions raw SQLite cannot price: FUSE/network-mount correctness (POSIX-lock/mmap soundness), crash-recovery wall time, and the cold-read ratchet (`packages/tofu-db/src/cold_read_tests.rs`).

## Write-path tail latency, current state (2026-09-21, post-P1 series)

Problem. The 2026-09-10 baseline left `turn_append` creation/append p99 trailing SQLite's, and the certification contract listed "performance superiority at P99" as uncovered. Decision. Close the tail with runtime feedback instead of operator knobs: dynamic group-commit window (the coalescing window closes immediately once four requests queue), adaptive pack pacing with a self-calibrating EWMA baseline and a 2 ms absolute pain floor (the 10 ms floor never fired on fast mounts), pack quiet-gating (defer ordinary batches while the foreground is hot; 32 MiB pressure or drain overrides), pain-bounded pack batch sizes, 30 s boot grace and crash-safe bounded stop joins for both background drivers, and GC mark traversals that check the stop flag every 4,096 visits. Alternatives considered: barrier pipelining (WAL append of group N+1 overlapping the fsync of group N) — defeated for now: measurement showed the tail is coalescing-window/queue dominated at this scale, not barrier-serialization dominated, and pipelining is a much larger correctness surface; fixed operator pacing rates — defeated: they cap throughput below what each machine can deliver and go stale per mount.

Measured (`tofudb_perf_compare.py`, small scale, FUSE, application client, strict durability): record_put_burst 1.52× faster mean (2473 vs 1627 op/s; p99 2.42 vs 2.09 ms), conversation_create_burst 1.11× faster (p99 1.02 vs 1.66 ms), turn_append_burst 1.01× faster (p99 2.80 vs 3.18 ms); maintenance_reopen 119.6× faster; concurrent_read 2.73× faster. The pre-P1 state on this checkout measured record_put 0.90×, conversation_create 0.48×, turn_append 0.44× slower. Certification-grade repeats (medium scale, 1,000 samples per workload — the certification contract's own tail-sample floor): mean superiority is stable (record_put 1.38–1.88×, turn_append 1.01–1.05×, conversation_create 0.87–1.84×), p99 is competitive with mount-weather variance (7–8 of 12 independent samples favour Tofu-DB; the recurring ~15 ms outliers were traced to concurrent background fsync storms and driven the global quiet-gating of every background write stream — packer, pack adoption, search projection, online GC, online backup — behind one shared mount-aware hotness gate in `fsync_stats`). Remaining known gaps, pre-existing and out of this slice's scope: read-heavy setup-dominated workloads (sidebar_list_page 0.31–0.34×, transcript_bulk_hydration 0.34×) and turn_append_large 0.11× (identical at the pre-P1 baseline; large-value write path).

**Soak wedge root cause (2026-09-21).** A 120-minute medium-scale soak
wedged the authority permanently at ~6.5 minutes: the 2,048-entry overlay
merge chunk over a deep, scattered tree produced more written pages than
the transaction block-reference budget, so every merge attempt failed
closed. The merge now halves its chunk until the produced page set fits
(`merge_overlay_chunk` adaptive sizing; regression pinned in
`entity_overlay_tests::overlay_merge_adapts_chunk_size_to_scattered_key_
fanout`), and the rerun soak reports zero errors with the engine's peak
RSS inside the personal-computer budget.

## Read-path analysis (2026-09-22, measured)

Problem. Read-heavy workloads trail the SQLite sidecar: sidebar_list_page 0.31–0.34×, transcript_bulk_hydration 0.34×, turn_append_large 0.11×. Decision on record. turn_append_large moved to 0.16× by eliminating the settled-turn semantic chain's double full-projection parse/escape (the compiler's canonical RawValue escape is spliced verbatim; the compiler also extracts `projection.timestamp` into the IR step so the hot path never re-parses). The gate also now covers read/query activity, not just write commits, in the shared quiet-gate hotness signal. What remains is structural and must not be micro-fixed: per-document hydration cost is diffuse (measured ~190 µs/turn doc, content-size independent, spread over envelope decode, typed document parse, public-turn rebuild, legacy-message construction, key encodings and allocation churn — no single hotspot), because bulk hydration materializes ~500 turn documents per 50-conversation page while the sidecar reads ~50 per-conversation archive columns. The durable fix is an archive-backed hydration path: maintain a compact per-conversation transcript projection (like the sidecar's `messages_json` archive) updated incrementally at append time, so bulk reads hit ~50 compact records; full-document materialization stays the certified fallback. Alternatives considered: per-doc zero-copy splices (bounded win — the cost is per-doc fixed overhead, not content scans), read-side quiet-gating alone (necessary but not sufficient), and batch tree probes (scan already amortizes lane indexes).

## Transcript archive (2026-09-22, shipped)

Problem. Bulk hydration (`conversation.list/get` with full transcripts) materialized every turn document at ~190 µs each, so a 50-conversation page cost ~95 ms. Decision. A live per-conversation transcript archive (`transcriptArchives` in the IR contract): the `tofu-db-archive-seal` daemon worker seals pending legacy-message windows into immutable, digest-witnessed page documents — materialization on a pinned read replica (never the authority mutex), publication in a short `try_lock` hold with a fresh lane-count fence. Reads fuse sealed pages with the exact bounded lane read of the unsealed tail; every non-append lane mutation tombstones the archive, and any count/shape mismatch falls back to the exact per-document read, so an untracked mutation can never serve a wrong answer. Measured: a 50-turn conversation's full hydration drops 37.4 ms → 10.0 ms on FUSE (3.7×), and larger conversations scale with page count, not document count (frozen-history audit showed ~6.7 messages/page). The harness's `transcript_bulk_hydration` phase (seed then measure instantly) measures the cold burst path, which stays per-document by design; steady-state hydration (conversations sealed by the background driver inside ~1 s) is what the archive accelerates. Alternatives considered: live-frozen pages (immutability vs projection-update/reparent/tombstone — defeated), append-time tail rewrites (WAL amplification — defeated), per-document zero-copy splices (the cost is per-doc fixed overhead, not content scans).

Rejected follow-up: rebase attribution measured 400.5→277.8 µs rebase and 672.0→558.9 µs commit under an observer, but default-off embedded settlement P99 mean regressed 18.3%; immutable-root point-validation reuse remains unintegrated.
## Snapshot ownership without capacity admission

Problem: fixed total/reader pin quotas refused work or sent it through a second
capacity FIFO before authority capture. Decision: register every live root and
count its owners without a count quota. Writer/GC exclusion and published-view
read guards still protect capture; immutable replica leases retain each root
until the final transaction or buffer drops. The old reader pressure hints,
capacity claims, release notifications and capacity FIFO are removed. Exact byte
accounting, owner checks, OCC and durability fences remain. Request deadlines,
connection close and authority/registry poison retain their error boundaries.

Alternatives: larger or launch-derived slot counts retain refusals; capturing roots outside GC exclusion risks premature deletion; permanently pinning the published view retains obsolete roots beyond request lifetimes. Verification: 96 simultaneous distinct root owners rendezvous before release; writer and root repinning remain eligible. Published-query and dispatch tests retain 512 readers while new work completes, including 32 queries with authority/FIFO writer guards held. Escaped leases and overflow tests verify exact teardown and atomic registration. Existing recovery, GC and fault matrices remain required. Controlled registration measurement: 96 concurrent synthetic root owners plus one root and writer reach the ownership barrier in 4.0 ms on this host. No root is released to admit the writer; teardown removes every registration. This measures root metadata/thread startup, not page I/O, peak RSS or durable throughput. A separate 96-version real-page test retains every historical value and GC root across subsequent writes, then releases all root registrations. Historical `tofudb_reader_admission_20261006.json` measures the retired queue; its latency values do not certify this change or universal SQLite parity.

## Active conversation acceptance

Problem: plain reports could qualify instrumented timings, foreign delivery implementations, absent source witnesses or equal filesystem labels. Decision: acceptance checks requested and observed client/native/delivery profiling at every captured boundary, including productive start. The existing pipeline manifest binds coordinator/benchmark aliases to the full source inventory; direct/pipeline and barrier/queued modes cannot be pooled. Physical authority `stat().st_dev` witnesses must match on this same-host runner.
Current admission requires identical complete source inventories before/after and against a freshly captured checkout, including the actual executor/resource owners, plus unchanged declared/pinned executable and loaded-library build identities. The source/artifact receipt is a provenance witness, not a new native identity implementation. Stable old binaries/coordinators retain `frozen_build_comparable` diagnostic value but not `current_source_qualified`; missing, contradictory or changed evidence refuses qualification. Unqualified pairs emit no speedup ratios. Alternatives rejected: trusting a wrapper's optimistic verdict, file mtime, the thin executor re-export or equal FUSE labels. `tofudb_benchmark_feedback_20261014.json` remains historical.

Problem. Storage-operation microbenchmarks do not certify interactive capacity. The panel and `tofudb_perf_compare.py` cover at most eight writers; `tofudb_bench.py` covers up to 64 ingestion writers. Settled Turn appends and generic event appends omit the live Attempt/projection/event path.
A completion/recovery pass alone permits a 47-second stream P99; a throughput geometric mean hides losing tails. The old comparison's authority-directory option only relocated TofuDB; SQLite could remain on the system temp volume.

Decision. `benchmarks/conversation_compare.py` is the paired application-workload
acceptance entry; `conversation_concurrency_contract.json` owns its machine-readable
coverage and engineering latency budgets. Default: 1,000 simultaneous running
attempts, four unique 8 KiB chunks each at fixed one-second arrival intervals,
two paired repeats with reversed backend order, and SIGKILL/reopen both while
running and after completion. It uses the production executor, Turn facade and
client; the provider is synthetic. Every acknowledged final text is compared
exactly. SQLite and native authorities use the same explicit scratch volume,
strict durability, and no SQLite fastpath relocation. The raw-SQLite floor stays
in the separate operation suite because it cannot implement application attempts.

Latency starts at submission for admission, at scheduled provider arrival for stream persistence (including backlog behind preceding events), and at terminal publication for settlement. RPC service latency remains a separate diagnostic. Failed/timed-out calls stay in distributions; missing observations fail acceptance. Recovery coverage requires measured running/final restart durations, the full requested peak and running readback, an explicit crash mechanism, and successful integrity verification. Problem: a requested restart may be skipped after partial admission. Decision: require completed phase evidence. Alternative rejected: trusting request flags would certify an unexecuted crash/reopen phase. Reports also retain `phase_ms`: productive work through settlement, recovery readback, executor shutdown, sampler shutdown, verification, final freeze and temporary-authority cleanup. `verification_phase_ms` separates native freeze from offline integrity traversal, or records SQLite's online integrity check. Nested verification timings must not be added again to their parent duration; `wall_seconds` remains the complete run. `restart_ms` is separate from productive load unless the restart intentionally occurs while attempts are running. The resource-observation scope ends after readback, before offline verification and cleanup. A fast streaming percentile cannot hide a slow verification/teardown phase. Older reports without these fields cannot identify such an outlier's cause; retain the observation without inventing an attribution. Initial budgets: admission P99 ≤ 5 s, scheduled stream P99 ≤ 1 s, settlement P99 ≤ 5 s; these are requirements, not claims that the engine currently meets them. Correctness, coverage, absolute latency and per-metric SQLite parity are separate verdicts. No average score offsets a failure. Readiness failures retain startup/productive phase and bounded component details. The CLI saves each failed leg and returns nonzero for incomplete/slow evidence. Unit CI tests verdict behavior, not machine timing. Standalone embedded SIGKILL: `python3 benchmarks/embedded_process_recovery.py --binary /path/to/tofu-db --directory /scratch --output /tmp/embedded-recovery.json` verifies running/final state; it does not upgrade in-process coverage or certify latency. Success cleans temporary state; `--retain-authority` preserves it. Run operator acceptance:

```bash
python3 benchmarks/conversation_compare.py --binary /path/to/tofu-db \
  --directory /path/on/dolphinfs/scratch --output /tmp/conversation-paired.json
```

The settings panel remains a bounded microbenchmark: P99/max/sample/error counts
are visible, small samples are labeled indicative, and capacity is always
“not assessed”, including persisted old reports with a headline score. Neither
suite certifies browser send feedback, real-provider limits or a sustained soak;
these gaps stay explicit in machine-readable reports. Alternatives rejected:
raising timeouts, lowering offered load when requests stall, discarding failed
samples, comparing different filesystems, or treating correctness as speed.

## Retained performance experiments

The user-requested [experiment decisions](TOFUDB_PERFORMANCE_EXPERIMENTS.md)
and `tofudb_performance_20260930.json` retain rejected
WAL suffix copying, permanent snapshots and fair query admission, including
regressions, source identities, attribution limits and retry conditions.
The runtime retains single FIFO admission; no publication snapshot or query
priority lane from those experiments is enabled. Performance conclusions must
include embedded queue coverage and application latency, not dispatch alone.


Indexed history now supplies durable carried payloads without requiring a second
ordinary-payload copy before WAL retirement. Pruning relocates bytes outside the
authority mutex and preserves the entity replay cursor. The
`tofudb_history_payloads_20260930.json` keeps
rejected locking pruning, empty-WAL recovery and mixed-shard GC assumptions,
versioned candidate comparisons, default-off profiling controls and process
SIGKILL readback. Root-path improvement does not constitute capacity acceptance
or SQLite parity: remaining queues, operator verification cost and losing tail
metrics remain separate verdicts.


Recovery attribution adds fixed `history_decode`, `history_lookup`, `history_range`, `overlay_replay`, overlay selection/carried-budget/publication, and complete checkpoint spans. Embedded shutdown attributes each driver join and checkpoint; opt-in `TOFU_DB_FSYNC_STATS` emits fixed phase names/times. The benchmark splits client close from embedded abandon or process kill/join. Verification reports sequential calling-thread phases with elapsed nanoseconds and history decode attempts, source bytes and validated transaction counts; early fatal returns retain completed/failed phase timings, including verify authority teardown (salvage keeps its authority open). Attempts/bytes include failed decodes. Process totals also appear in `system.metrics.durability`; optional request work uses the existing bounded collector. Totals and inclusive spans overlap and must not be added as disjoint latency. These counters retain no identifiers or payloads and perform no I/O. `tofudb_replay_costs_20261001.json` distinguishes the instrumented diagnostic control from the existing production baseline and retains observer controls, correctness and remaining capacity failures. Pin diagnostics separate registry acquisition, byte reservation and byte repayment; work counts acquisitions, budget operations and waiter notifications. `tofudb_pin_accounting_20261003.json` retains exact quotas, rejected alternatives and default-off results. Reply acquisition/recycle anchors are global caller-edge spans; reuse/create work is attached to worker requests. `tofudb_reply_reuse_20261003.json` retains fresh-channel lifetime, three rejected reuse/mailbox designs, observer costs and default-off comparisons. `pack_filter_incomplete` counts conservative probes when the Bloom filter does not cover the installed catalog; it is an event count, not elapsed time. `overlay_prepare_capture`, `overlay_prepare_encode`, and `overlay_prepare_publish` distinguish brief authority holds from private tree preparation outside authority. `overlay_prepare_stale` is a rejection event, not a duration. Private pages account actual transaction memory without an aggregate quota and release after each slice; retention and resource peaks must accompany comparisons. `overlay_prepare_budget_wait` remains a compatibility counter for retired quota deferrals and receives no new increments; it does not classify physical I/O errors. Results and limitations live in `tofudb_overlay_preparation_20261001.json`. Projection probes `projection_head_materialize`, `projection_head_checkpoint`, `projection_head_patch_load`, and `projection_head_patch_apply` separate reconstruction work; inclusive and child durations overlap. Checkpoint/patch bytes count valid materialized JSON; applied patches may precede a later rejected head. Global counters include background consumers; optional request work attributes the caller, with existing bounded retention. Ownership and admission rules live in [JSON materialization](TOFUDB_JSON_MATERIALIZATION.md); `tofudb_projection_reconstruction_20261001.json` retains observer controls and limits. WAL/page attribution lives in [barrier scheduling](TOFUDB_WAL_BARRIER_SCHEDULING.md). Block branch attribution lives in [verified reads](TOFUDB_BLOCK_READ_PATH.md). Rotation ownership and attribution live in [window preparation](TOFUDB_ROTATION_WINDOW.md). The source-pinned 2026-10-05 matrix, named remaining raw-SQLite gaps and structural causes are retained in the receipt-proof retest. Fresh `tofudb_point_ingress_20261005.json` separates application input-classification gains from removing unused benchmark read UUIDs; raw-SQLite, delivery and remaining coverage gaps remain open. Publication lock placement and measured slow-I/O isolation follow [publication latency](TOFUDB_PUBLICATION_LATENCY.md).


## Entity transaction byte ownership

Problem: a fixed 160 MiB aggregate quota rejected valid reads and writes and deferred private overlay encoding despite available host memory. Decision: transaction leases account actual retained bytes without an aggregate admission ceiling. Checked arithmetic and poison fences remain authoritative; drop and replacement repay ownership exactly. OCC witnesses, source I/O failures and per-operation protocol validation retain their existing behavior. Alternatives: raising the quota only moves the refusal point; disabling accounting obscures retained memory and leaks. Snapshot counts are also ownership observations without admission quotas. Verify concurrent lease accounting above the retired threshold, real reads/writes and overlay publication under that observation, plus atomic overflow and poisoned-registry tests. Accounting-only large reservations do not measure real allocation capacity or certify behavior under OS memory exhaustion.

Controlled staging measurement: 32 simultaneous transactions each retain 700
8-KiB values: 183,500,800 real payload bytes and 185,707,880 accounted bytes,
created in 150 ms on this test host. Dropping all transactions leaves zero
retained bytes and zero handles. This measures in-memory staging, not durable
commit throughput, peak RSS, or behavior under OS memory exhaustion.


## Provider catalog and OCC witness ownership

Problem: the SQL/native provider domain rejected a 33rd provider, and the
native list path could return only a fixed 33-row scan. Larger catalogs also
hit 6,144 point-witness and 1,024 range-witness transaction quotas. Decision:
owner/label counts are exact integrity observations, with checked arithmetic,
not admission limits. SQL creation drops the quota-only owner lock and count
probe; global ID uniqueness, receipts and rollback remain authoritative. Native
listing traverses every created-index page on one snapshot, retains every OCC
witness and redacts ciphertext. Page size, per-document validation, wire output
validation and single-range page/format validation retain their separate contracts.

Alternatives: larger quotas merely move the refusal point; fresh transactions
per page lose snapshot consistency; discarding earlier witnesses can miss late
updates and phantoms. Verification: 7,000 native providers cross index pages
and the retired point-witness threshold; mutating the oldest listed provider
still aborts a dependent write. Owner/label filtering, duplicate global IDs,
secret redaction, deletion counts, count overflow and complete teardown remain
checked. SQL repository tests retain all 65 providers. Entity tests record 6,145
distinct point reads and 2,050 forward/reverse range predicates, then detect
late updates/phantoms and release all reservations. No provider model is added
or made callable by these tests; model registration keeps its admission owner.

Controlled native catalog measurement: 7,000 provider rows produced 1,907,671
public bytes in 3.03 seconds including grouped creation and list traversal.
This measures an isolated temporary native authority, not remote provider I/O,
peak RSS or full application latency. The late-document OCC check aborts the
dependent write; teardown returns retained transaction bytes and handles to zero.
SQLite/private-PostgreSQL share the same 65-row ownership/redaction contract
test; unavailable local PostgreSQL binaries skip that lane explicitly.


## Range traversal and witness memory

Problem: a fixed 1,024-leaf witness quota refused small valid results in
churn-heavy ranges. Masked scans repeatedly re-read larger prefixes until an
internal row ceiling failed. Every range also precharged two full hypothetical
leaf vectors. Decision: retain all visited leaf references using fallible vector
growth; account retained leaf-vector capacity, key lengths and the existing
header allowance. Masked scans move forward/reverse key cursors through bounded
base windows on the original snapshot, with the corresponding raw-window OCC
witness for every visited window. Later failure truncates only newly captured
witnesses and repays their reservations; earlier transaction reads survive.

Alternatives: larger caps postpone refusal; unlimited prefix expansion retains
repeated work and large temporary row buffers; dropping previous windows loses
conflict/phantom evidence. Page/format validation, explicit result-page sizes,
physical I/O errors, allocator failure and transaction ownership retain their
separate authorities. Tombstone reclamation still reduces actual storage and
scan cost. Verify both directions beyond 1,024 leaves, 14,000 masked base rows,
exact-key exhaustion, late conflict detection, actual capacity accounting and
rollback of a later-window accounting failure in `entity_range_demand_tests.rs`.

Controlled traversal measurement: two 1,099-leaf dead-prefix walks found their
forward/reverse tails in 4.37 ms combined. With 14,000 staged masks, 15 bounded
windows returned 1,000 visible rows in 13.62 ms forward / 7.43 ms reverse. Small
exact-key reads account less than 4 KiB per witness instead of a hypothetical
full leaf quota. These are deterministic-VFS traversal timings, not real-volume
throughput or peak RSS. Actual I/O-fault and later-window accounting-fault cases
retain prior reads and repay new reservations.


## Timer admission and complete recovery

Problem: timer definitions were refused at fixed live/saved counts, process
registration repeated the live quota, and restart restored only a capped prefix.
Decision: live/saved counts observe ownership without admission limits; legacy
capacity overrides cannot reject work. Internal active-feed limit=0 traverses
all native index pages or all SQL rows, while positive request limits remain
explicit projections. Recovery starts every eligible definition. Duplicate IDs,
owner filtering, leases, readiness obligations, poll budgets, cancellation and
startup-failure rollback retain their existing authority. Alternatives: larger
limits postpone refusal; skipping overflow until another restart leaves active
work unserviced; dropping leases can duplicate continuations.

Verification: 65 real watcher threads rendezvous despite old knob 1 and repay
all leases; restart reads 265 definitions despite old resume/live knobs 1. Native
creation and full recovery feed retain 650 active definitions with compatibility
capacity 1 in 555 ms on the temporary test volume. This measures definition
creation/readback, not model polling, peak RSS or real application latency. Each
watcher now uses the shared wait runtime described below. Generated contracts and owner-scoped terminal/fault tests
cover the same transition boundaries.


## Timer wait and physical work ownership

Problem: removing admission quotas exposed the cost of one sleeping daemon thread per active watcher. Decision: one lazy process-owned asyncio loop keeps independent timer waits; synchronous storage and polling calls use demand workers with zero idle retention. Independent work starts without a worker/queue ceiling. Stop wakes waits and fences subsequent calls. Cancellation drains physical work; lease release is an awaited cleanup step. Production shutdown joins the runtime before task quiescence and closing HTTP clients, runtime leases or storage; an unsettled join keeps those dependencies live. Alternatives: a larger fixed pool queues independent polls, and cancelling a proxy Future can release a lease while its callback still runs. Per-watcher sleeping threads retain unnecessary idle stacks.

Measurement: 512 simultaneously waiting watchers retain one wait thread and zero blocking workers. With 32 blocked callbacks, a healthy 33rd finishes within two seconds; shutdown remains pending beyond 1.1 seconds until callbacks return, then all callbacks and worker threads retire. Explicit stop and task cancellation retain ownership through a blocked call. These are controlled local lifecycle measurements, not provider throughput, production peak RSS or volume benchmarks. No new durable records, polling backlog or per-watcher OS thread is introduced; coroutine/handle state scales with live durable watcher definitions and retires on completion. The shared loop retires on joined process shutdown. Gates: `tests/scheduler/test_timer_wait_runtime.py`, `test_timer_live_capacity.py` and `tests/server/test_server_shutdown_lifecycle.py`.


## Scheduled-task definitions and complete dispatch feeds

Problem: 100-task owner quotas rejected creation and migration; 1,000-row runtime reads stranded accepted definitions, while native integrity/adoption scans reused the quota and response/list caps imposed further refusals. Decision: counts observe integrity with checked representability/overflow; creation retains exact identity/system-key claims without quota-only owner locking. Internal limit=0 traverses every matching row; positive limits are caller-selected projections. Native index windows share one snapshot and retain all OCC witnesses. Built-in adoption checks the complete catalog. Migration keeps resumable cursor pages, and due claims preserve occurrence fencing. Alternatives: larger count/response caps postpone refusal; a partial feed leaves work unserviced; dropping claims or OCC allows duplicate effects and stale writes.

Controlled native measurement: 2,105 same-owner definitions and their complete 9,835,556-byte response take 1.54 seconds for grouped creation plus readback on the temporary volume. A 2,001-row projection succeeds; the full cross-owner feed, late-window built-in adoption, exact deletion count and late OCC conflict retain authority. Migration reads all 130 restored records across its 128-row cursor pages and repeated restore remains idempotent. SQL creation/listing keeps all 105 same-owner records; runtime dispatch handles 1,105 due definitions instead of its old 1,000-row prefix. No thread, durable history or retained cache is added. Result vectors live only for their call and scale with selected output; complete feeds still materialize responses, so these measurements do not prove production peak RSS, arbitrarily large transport frames or provider throughput. Gates: native scheduler demand/semantic/migration tests, SQL storage contract, paired SQLite/private PostgreSQL operation lane and scheduler reconciliation.


## Credential catalog admission and complete projections

Problem: both authorities rejected the 1,001st live credential for an exact owner/tenant boundary; native list traversal and aggregate encoding repeated that quota. SQL creation also scanned the boundary to count every accepted key. Decision: retain exact active counts as integrity observations with checked representability/overflow before any native document, hash or index mutation. SQL keeps exact ID/hash conflict locking and account ownership guards without count admission. Native lists traverse all bounded index windows in one snapshot and retain every OCC witness, owner/tenant check and secret redaction. Per-record response validation is separate from aggregate list output. First-key bootstrap still checks atomic owner-boundary emptiness; account inactivity, expiry, disabled/revoked state and exact secret-hash uniqueness remain authority. Alternatives: increasing counts/aggregate bytes postpones refusal; truncated lists hide administered credentials; weakening bootstrap or authentication fences can grant unintended authority.

Controlled native measurement: grouped creation and full readback retain 1,801 live credentials and 11,356,207 public bytes in 2.43 seconds on the temporary volume. Last-window changes still abort dependent stale writes; revocation repays the active count and prevents authentication. Retiring all transactions repays retained byte/handle observations to zero. SQL admits 1,001 same-boundary keys with complete order, cross-owner/tenant exclusion, duplicate-secret refusal, bootstrap non-creation and revoked-key rejection. No new worker, durable audit or retained cache is added. Output vectors scale with the administered catalog and retire after the call; the measurement does not certify arbitrary transport-frame sizes, production peak RSS or provider throughput. Gates: native credential tests, credential demand SQL/private-PG lane, bearer/account contracts and differential credential demand scripts.

## Centralized offline performance catalog

**Problem:** measured storage results are distributed across benchmark receipts
and experiment evidence, making release-to-baseline comparison difficult and
encouraging comparisons between incompatible workloads.

**Decision:** `scripts/db_performance_catalog.py` provides an offline operator
interface for importing immutable source snapshots, listing version-labelled
results, and exporting detailed SVG, Markdown and JSON comparisons. The
machine-readable authority is `contracts/db_performance_catalog_v1.json`.
Catalog data lives under `data/benchmarks` with an explicit owner namespace;
this is operator measurement storage, not a second application database.
The tool never opens a live authority or changes backend selection.

A baseline is selected explicitly. Scenario, environment, metric, unit and
optimization direction must agree before results are compared. Source digests,
version labels, raw window measurements, aggregation method, correctness,
coverage gaps and absolute latency budgets remain available with the chart.
Median window P99 is never labelled pooled request P99; missing evidence is
not a passing gate. Historical evidence certifies only its recorded build and
scope, not the current checkout. A faster result can still fail its budget.

**Alternatives rejected:** one universal speedup hides regressions and mixes
engine-floor SQLite with application-semantic SQLite; a new live-database table
would couple offline reports to runtime availability and expand authority;
rerunning every benchmark to redraw a chart wastes resources and destroys the
stable evidence needed for release decisions. Imports retain source snapshots
rather than relying on mutable external paths. Retention is explicit: no
background worker, automatic eviction, or deletion of measurement evidence.

Verification: `tests/tofudb/test_db_performance_catalog.py` exercises imports,
idempotency, provenance/corruption refusal, comparison boundaries and exports.

### Catalog operator interface

From the primary checkout:

```sh
python3 scripts/db_performance_catalog.py import --owner db-team --version review-20261010 \
  --source PATH/TO/tofudb_direct_sqlite_pair_20261008.json  # retired evidence/ artifact; recover from git history
python3 scripts/db_performance_catalog.py list --owner db-team --version review-20261010
python3 scripts/db_performance_catalog.py report --owner db-team --version review-20261010 \
  --id RUN_SHA256 --baseline sqlite --candidate native
```

Import prints a run ID; substitute it for `RUN_SHA256`. Reports default to
`data/benchmarks/performance/OWNER/VERSION/reports/`. Source snapshots are
owner-deduplicated; report SVG, Markdown and JSON are immutable and
content-addressed. Listing returns bounded summaries with `--limit`/`--offset`.

For release-to-release native comparisons, import schema-conforming
`db-performance-catalog/v1` files recording identical fixture, scenario and
environment identities and different native build versions. Run `report` with
`--version NEXT_RELEASE --id CANDIDATE_SHA256 --baseline native --candidate native
--baseline-version BASELINE_RELEASE --baseline-id BASELINE_SHA256`.
Historical adapters establish an environment only within their recorded pair:
unknown environment refuses cross-source comparison. Generic evidence enables
new reproducible cohorts; relabelling cannot create a missing witness.

The default is checkout-local. With several worktrees, pass the same canonical
`--catalog /absolute/path/to/data/benchmarks/performance` in every checkout.
Owner namespaces organize offline results, not application authentication;
roots must be operator-controlled. Inputs are regular files bounded to 16 MiB;
exports/listings are bounded. No background worker or automatic deletion is
added. Persistent growth occurs only on explicit imports/exports; retain or
reclaim these operator artifacts deliberately, separately from live user data.

## Native comparison-card exports

`db_performance_catalog.py report` also emits a `rich.json` payload conforming
to `contracts/rich_response_v1.schema.json`. It carries original measured values,
direction, window aggregation, matching absolute budgets, recorded source/build
provenance and coverage limits. Failed correctness/coverage/budget gates outrank
relative improvement; an absent gate remains unknown. Payloads use bounded
comparison cards and evidence blocks, not generated scripts or filesystem image
URLs. Full raw windows remain in the adjacent report JSON. Producers may publish
the payload through the native rich-response tool; publication and owner checks
are distinct from the offline catalog's measurement identity.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

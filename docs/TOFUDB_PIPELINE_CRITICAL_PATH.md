# Pipeline attribution and search activation ownership

Owner: `packages/tofu-db/src/turn_search_worker.rs`,
`packages/tofu-db/src/listener.rs`,
`packages/tofu-db/src/semantic_search_backfill.rs`,
`benchmarks/conversation_concurrency.py` and `scripts/tofudb_profile_report.py`.
Publication still depends on the existing source activation and WAL witness;
benchmark interpretation depends on process identity and completed populations.

## Pipeline profile windows and authority lifetimes (2026-10-15)

**Problem:** an admission snapshot precedes the deliberate running restart;
`terminal_boundary_storage_metrics` is taken after stream submissions but before
joined settlements. Subtracting them can cross a reset or include unfinished
requests. Embedded abandon/reopen may retain the same PID and native static
collector, so PID equality alone is insufficient. Legacy `phase_ms.productive`
also includes setup, admission and running recovery; it is not event-only time.

**Decision:** `conversation_concurrency.capture_storage_metrics` records process
PID/birth, explicit authority incarnation, named boundary, caller quiescence and
the monotonic interval of each metrics RPC. `productive_start_storage_metrics`
is after running recovery/readback and before provider release; `storage_metrics`
is after joined event/settlement callers and before the terminal restart.
`phase_ms.events` and `event_window_monotonic_seconds` cover provider release to
joined callers, excluding the start/end metrics RPCs. The terminal submission
barrier metrics RPC remains inside this event interval. Existing phase names
retain their historical meanings. That terminal snapshot is explicitly nonquiescent.
The pipeline source witness includes both authority and concurrency harness files.

Run `scripts/tofudb_profile_report.py REPORT.json --pipeline-window` only for
profile-on attribution. It refuses missing/mismatched provenance, nonquiescent
boundaries, sampling other than every request, dropped samples, decreasing or
inconsistent counters and unmatched completed populations. Each endpoint must
independently satisfy selected = completed + dropped: equal outstanding counts
at both endpoints cannot cancel into a falsely complete difference. The shared interval
subtractor also serves single-operation `--window`; lifetime maxima and slow
samples are never differenced. Errors/retries remain counted. Summed dispatch
and inclusive spans are elapsed work across callers, not CPU or critical-path
latency. Background activity can continue at the foreground fences.

**Alternatives:** do not infer identity from increasing counters, subtract across
restarts, relabel legacy productive time, or ignore dropped profiles. Native
collector contention currently permits dropped samples; refusal is evidence of
an attribution limit, not evidence of zero cost. Profile-on embedded mutating
commands use the legacy thread-bound path (see `TOFUDB_EMBEDDED_PHASES.md`), so
their queueing clocks do not describe default continuation scheduling. Qualify
latency in separate, profile-off matched arms with unchanged workload/recovery.
A profiler trace with too few samples or missing native mappings likewise
cannot establish CPU attribution, regardless of a successful workload run.

**Verification:** `test_conversation_metric_boundaries.py` covers process changes
and embedded same-process incarnations; `test_pipeline_profile_window.py` covers
multi-operation subtraction and refused evidence. Existing single-operation
window/report and pipeline acceptance tests remain required. Four real strict
application/embedded × phase/delta runs verified restart identity, boundary
ordering and exact readback; all four correctly refused exact attribution after
native sample loss. No diagnostic clock is a latency qualification.

## Search activation preparation outside the writer mutex (2026-10-15)

**Problem:** activation preparation held the foreground authority mutex while
reading and committing the independent projection database. A paused target
write deterministically blocked an unrelated foreground commit. Sampled
application-delta diagnostics also observed a 256.71 ms activation hold; this is
elapsed hold time in an instrumented run, not CPU or default-path latency.

**Decision:** read a healthy source UUID and the owner
activation at the existing writer boundary, then release the writer before
preparing the target. Reenter through the existing nonblocking authority port,
check durability health and require the source UUID plus activation to still
match. Stop, contention or a changed source leaves the pending target retryable
without overwriting or acknowledging the newer source. No projection mutex is
held while reacquiring the authority. Existing activation/checkpoint commits,
covering WAL witness, off-mutex durability wait and target acknowledgement remain
the publication boundary. If durability fails after capture, the prepared target
may be pending; it is never admitted by the failed round.

The existing single maintenance consumer remains the supported activation
owner. An overlapping stale preparation may reset only reconstructible target
state and must defer at the source fence; this does not admit multiple independent
live queue consumers. No new executor, worker, cache or durable namespace is
introduced. Each batch adds an activation recheck on reentry; it adds no snapshot or pin.
The source activation-key read remains under the writer; cold source-read
stalls are a separate limit, not fixed by moving target I/O. The fixed optional hold vocabulary
adds `search.activation-capture`: 24 counter bytes (432 total, 40-byte stack timer
unchanged), no clock read when profiling is disabled. Capture and reentry holds
are separate; target I/O is intentionally absent from authority-hold time.

**Alternatives:** keeping target I/O under the writer preserves a demonstrated
foreground stall; removing the lock without source revalidation permits stale
activation overwrite. A new target executor/cache would add lifecycle and
invalidation machinery without fixing the ownership boundary. Reuse the existing
source token and WAL fences instead.

**Verification:** `turn_search_worker_activation_tests.rs` pauses a real target
VFS write and proves foreground commit progress, changed activation/source UUID
refusal, nonblocking busy-writer deferral, stop before source activation, and
exact search/dirty-token convergence on retry. Existing search tests retain
strict/normal WAL waits, failed barriers, crash/reopen, rotation, shutdown,
owner isolation and no-wipe unchanged activation. Measured qualification and
adverse cohorts are recorded in
`tofudb_pipeline_critical_20261015.json`; the change does not establish a
universal raw SQLite or 1,000-active-conversation capacity victory.

### Same-hold backfill checkpoint reuse (2026-10-15)

**Problem:** after `consume_page` read a completed checkpoint or committed a
page's dirty tokens and checkpoint, the worker opened another source transaction
and loaded the same checkpoint for coverage and dirty filtering. The writer hold
was uninterrupted; the intervening dirty-list operation is read-only. This added
a snapshot-pin registration/release, point read and decode under writer exclusion
without observing fresher state.

**Decision:** `PageProgress.checkpoint` transfers the exact loaded checkpoint or
the exact `advance` result **only after** `database.commit` succeeds. The worker
uses it within that same hold, never as a cross-batch cache or durable receipt.
Absence remains `None`, not complete coverage. Empty, incomplete and complete
pages retain the original generation/cursor semantics and `MAX_CURSOR_BYTES`
bound. No worker, queue, capacity or escaped pin is added.

The original checkpoint read/decode, `advance`'s OCC comparison, source range
witness, atomic dirty/checkpoint commit, both source activation reads, health
checks, covering WAL wait, target publication and exact-token ACK remain. A
failed commit returns an error, never proposed coverage. Rejected flags stay off.

**Evidence and scope:** the owned deterministic fixture
`request_profile::tests::completed_search_backfill_reuses_checkpoint_without_another_read`
uses disposable authorities and existing thread-local accounting. A completed,
empty-owner batch makes **7 `entity_get` calls instead of 8**: checkpoint reads
**2 → 1**; two source activation and four target ownership/index reads remain.
It fails against the old implementation and verifies an unchanged source
snapshot, zero dirty/source-page work, and no leaked pins. This proves one
redundant transaction/read/decode removed per executed backfill/coverage phase,
**not** a physical I/O count, writer-wait percentile, append latency or end-to-end speedup. No
wall-time benchmark or acceptance threshold is changed.

Backfill tests compare returned/persisted checkpoints for absent, empty,
multi-page, completed and foreign-owner scopes; reject malformed current state;
and require write-error, short-write and sync-error injections to fire. Failed
commits stay poisoned until crash/reopen; retries recover the original checkpoint
and atomically publish exact dirty work and coverage. Existing stale-page/OCC
and worker activation/durability tests retain the unchanged fences.

**Gate boundary:** 16 backfill and 102 search-neighbor tests pass. The full
native package gate stops at the library: 1,984 passed, 3 failed, 9 ignored;
integration/doc targets were not reached. All three failures reproduce from the
pre-edit native inputs: two entity range-witness tests remain unfixed, and the
resource-probe fixture inherits a 32 MiB checkpoint-batch override. That fixture
passes when only `TOFU_DB_CHECKPOINT_MATERIALIZE_BATCH_BYTES` is unset. This is
not a green full-package or release certification claim.

**Alternatives:** another source read adds no freshness within this hold. Reuse
across writer release, skipping the original read, new off-lock scan preparation,
or weakening the WAL wait would need different validity/lifecycle proofs and
are excluded. Source scanning and queue waits remain critical-path work.

### Bounded admission and unresolved performance limits

Admit target-I/O isolation for deterministic foreground progress, with exact
source revalidation and unchanged durability. Two strict profile-off balanced
blocks (ABBA then BAAB), each with application/embedded × phase/delta, retained
32 complete 1,000-conversation × four-round × 8-KiB pipeline runs:

| Client / event | Stream scheduled P99 | Event-only duration | Settlement P99 |
| --- | ---: | ---: | ---: |
| application / phase | -20.82% | -5.75% | -8.07% |
| application / delta | -32.36% | -3.81% | -4.89% |
| embedded / phase | -2.25% | -5.44% | -9.53% |
| embedded / delta | +1.86% | -1.07% | -3.12% |

These are changes in averages of per-run statistics, not pooled percentiles or
confidence guarantees. All runs passed correctness and all missed the complete
capacity SLO. The reverse embedded/delta block regressed stream P99 by 9.76%
and event duration by 1.60%. One application/delta candidate reopened in 3.47 s;
that cohort's combined productive duration was +2.73% including recovery. Do not
hide this observation behind the event-only improvement. Application/phase peak
client RSS was +17.40% across these runs; no new resident worker/cache was added,
and that does not imply unchanged workload residency. Embedded client/native
RSS observations share a process and must not be added.

The nine neighboring native/raw-SQLite workloads retained adverse results:
long-window mean/P99 +11.32%/+43.24% at default cache, +2.56%/+14.11% at 32 MiB.
A separately declared warm 1,500-Turn control then checked 320,000 full reads
(40,000 per arm, ABBA at each cache budget): default mean/P99 -1.42%/-15.79%,
CPU -1.77%; 32 MiB mean/P99 +0.21%/+13.34%, CPU -0.23%. The warm control does
not erase the earlier setup/transient population or the remaining small-cache
tail regression. This change makes no universal read-latency or memory claim.

A separate profile-on application/delta run confirmed the new fixed capture
label is exercised. Capture and reentry activation holds were at most 0.049 ms
and 0.015 ms in that run; backfill held up to 16.79 ms. The baseline in that
pair reached 5.08 ms for activation, not the earlier diagnostic's 256.71 ms.
Different completed maintenance populations, lifetime maxima and enabled
instrumentation cannot be converted into an end-to-end speedup or CPU claim.
Admission snapshots suggested an association with packing state, but precede
the actual crash and do not prove crash-time state or causality. Cold source
reads, backfill, reopen cost, small-cache read tails and stable 1,000-active
capacity remain explicit follow-up work.

### Rejected preliminary snapshot-capture variant

The earlier variant also built a read replica solely for the activation lookup,
dropped it before target preparation, then revalidated on reentry. It passed
1,998 native tests (9 ignored), all 67 search tests and all 32 strict queued
1,000-conversation pipeline arms. Correctness did not qualify performance:

| Client / event | Event-only duration change | Other material change |
| --- | ---: | ---: |
| application / phase | -6.92% | running restart +653.85% |
| application / delta | -5.67% | running restart +805.11% |
| embedded / phase | +4.36% | settlement P99 +6.54% |
| embedded / delta | +4.34% | settlement P99 +8.84% |

These compare averages of per-run statistics across ABBA and BAAB blocks, not
pooled percentiles or confidence guarantees. Application restart regressions
appeared in both blocks; embedded phase settlement and delta streaming P99
regressions also repeated. The first application/delta block's stream P99 was
+30.02%; the reverse block was -28.80%, and both remain retained. All 32 arms
failed the whole-capacity SLO. Neighboring local-XFS raw SQLite controls also
retain adverse per-run P99 statistics, including low-count tails.

The slow application reopens were localized to `engine_open` before projection
open, not proven to be caused by the new snapshot. Removing the extra replica
is a narrower next experiment, not a demonstrated explanation of the startup
regression. Do not reintroduce this variant based only on its application-event
wins or deterministic target-I/O isolation. Exact rejected source/artifact IDs,
raw data and reconstruction are in the evidence record.

## Rejected native accept-queue experiment (2026-10-19)

Problem: this Linux host permits 4,096 pending connections, but a fresh Rust
`TcpListener::bind` socket was observed with a backlog of 128. A current-source
1,000-conversation diagnostic reached 997 active connections. Its sampled
`turn.event.record` dispatch was dominated by FIFO waiting (98.9% of summed
sampled dispatch wall time), with 0.693 ms mean authority hold. Profile retention
dropped 13 requests; this diagnostic is attribution, not performance acceptance.
The five thousand actual commits spent 3.376 seconds in IR commit, including
1.573 seconds in validation/rebase. These are inclusive elapsed counters, not
CPU time or independently additive removable work.

Decision: **do not enable the tested backlog change**. The candidate raised the
Unix pending queue once on the bound socket to the greater of twice the existing
launch connection reservation and observed kernel capacity, bounded by the
platform integer. Linux observation was bounded to 32 bytes; missing observations
used `SOMAXCONN`; non-Unix behavior stayed unchanged. It changed no worker or
session-pool quota, durability, authentication, owner checks or request timeout.
Its kernel burst test and 19 listener, 8 daemon and 25 protocol tests passed.
That mechanism evidence does not qualify its mixed end-to-end results.

All legs below use current schema 68 from `ac1df5394`, the same Python workload
source, local XFS, profile disabled, strict acknowledgement, 1,000 simultaneously
running conversations, four 8-KiB phase events with one-second fixed arrivals,
and real SIGKILL/reopen while running and after completion. SQLite uses the
production sidecar with WAL and `synchronous=FULL`, on the same requested volume.
The candidate differs only in listener queue configuration/metrics and its tests.
An identical external observer samples daemon RSS, threads and FDs every 100 ms;
offline native verifier processes are recorded separately.

| Leg | Authority | Stream scheduled P99 ms | Settlement P99 ms | Daemon peak MiB | Acceptance |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | Native baseline | 3,253 | 6,411 | 2,429 | latency fails |
| 1 | Native candidate | 2,407 | 7,031 | 2,871 | latency fails |
| 2 | SQLite | 4,028 | 16,195 | 186 | readiness + latency fail; background I/O overlap |
| 3 | SQLite | 3,850 | 14,574 | 186 | readiness + latency fail; no task background I/O |
| 4 | Native candidate | 1,919 | 6,947 | 2,564 | latency fails |
| 5 | Native baseline | 5,113 | 5,312 | 3,122 | latency fails |
| 6 | Native candidate | 1,972 | 5,127 | 2,653 | latency fails |
| 7 | SQLite | 3,966 | 17,775 | 186 | latency fails |

The stream budget is 1,000 ms and the settlement budget is 5,000 ms: every leg
fails the whole-capacity policy. Native candidate settlement regresses twice
and improves once. Its scheduled completion P99 is 8,033/7,395/6,379 ms versus
7,011/9,606 ms for the baseline; there is no stable all-tail win. All 8,000
conversations retain exact running/completed state, content and event replay
after crashes; every integrity check passes. Both failed SQLite legs retain one
real readiness deadline failure, even though their durable readback passes.
The clean failure cannot be explained by the earlier background I/O.

Native daemon peaks are 990–1,001 active connections and roughly 1,000 threads.
After productive callers drain, both native arms retain the existing 256 idle
client sessions plus health connection (257 connections; 269 daemon threads).
The changed queue does not establish a memory reduction: native daemon RSS is
2,429–3,122 MiB against SQLite's 186 MiB. Every observed authority PID is gone
at process teardown; this is one measured lifecycle, not a long-run memory bound.

Alternatives considered: a small fixed client pool merely limits concurrency;
eager connections add idle residency; wider RPC deadlines hide delays. The
observed queue ceiling is real, but changing the default solely on stream P99
would ignore settlement regressions. Continue targeting measured serial commit
work while preserving fresh-head OCC, strict durability and recovery behavior.

Reproduction evidence is retained locally under
`data/benchmarks/tofudb-parallel-20261019/stream-tail/`: all eight raw reports,
per-process samples, original runner failure, continuation, diagnostic profile,
focused test/build logs, source patch and both build manifests. `summary.json`
binds these records and the interference window. Baseline native source is
`7d2a42adee860fa774f2815c17f00e7310889072f689458bb5a589f62df494f1`;
candidate source is
`97e774bb16e7af98eb4bc222b6ac96c1c2ef3ffb894905a664c55058044570f0`.
The patch hash is
`0bdaa300544f46715884234d225bf12cb154fbf039181f1a61ab7d432da443c8`.
Frozen binaries and matching libraries remain in
`/tmp/tofudb-parallel-20261019/{baseline,listener-candidate-final}/`.
Verified copies also live in the evidence directory's `artifacts/` subdirectory.
The initial schema-67 mismatch remains a failed attempt, not a current-source
performance sample. Root worktree creation overlapped only SQLite leg 2
(09:17:21.084–09:18:07 UTC); the reverse CBA continuation and extra BC repeats
ran after that I/O ended. The OCC worktree was created before any measured leg.
These few per-run percentiles are not pooled percentiles or a confidence claim.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

# Foreground latency during catalog publication

Responsibility: native rotation/history publication placement, successor-state
reconciliation, verification and performance limits. Entry points live in
`packages/tofu-db/src/engine_rotation_publication.rs`, `engine_history_compaction.rs`
and `daemon.rs`; CONTROL and WAL remain their existing durability authorities.

Problem: a sealed predecessor kept successor commits available during encoding,
but its final manifest and CONTROL fsync still held the authority mutex. History
compaction similarly returned to authority for CONTROL and payload-route reads.
A slow publication could therefore queue unrelated foreground writes.

Decision: `engine_rotation_publication.rs` reserves the existing physical lane
with metadata only; `daemon.rs` releases authority through WAL cover, immutable
manifest publication and the checksummed CONTROL chain. Finish changes physical
fields only and reconciles successor sequence/hash/entity root without rolling
back concurrent commits. `engine_history_compaction.rs` publishes its captured
CONTROL state and prepares payload routes outside authority, then reconciles
the newer logical state. Other physical publishers retain their existing defer
boundary. Failed/abandoned publications fence the engine; Normal WAL cover,
lost-sync barriers, old-WAL retention and durable-before-visible acknowledgements
remain. No extra worker, queue, persistent format or admission limit is added.
One frozen manifest survives the existing publication window; the WAL ceilings
continue to bound progress while the underlying volume itself is stalled.

Alternatives rejected: more retries or a watchdog changes outage handling, not
lock-induced latency; early success/push without a durable witness weakens
acknowledgement; applying the captured logical root at finish loses successor
updates. A second CONTROL writer would duplicate publication authority.

Paired tests pause an actual RealVfs CONTROL fsync for 100 ms, alternate arm
order, and require exact reopen/readback after every sample. Thirty local pairs
measure foreground commit P95 100.96 -> 0.186 ms; twenty DolphinFS pairs measure
106.62 -> 4.680 ms. This establishes slow-publication isolation, not the volume's
usual latency. Independent full-release ABBA cohorts use the same production
client, Strict durability, four unique 8-KiB chunks at one-second arrivals,
running SIGKILL/reopen and final offline integrity. At 128 running conversations,
stream-scheduled P99 is 313/370 ms before and 282/324 ms after. At 1,000 it is
5,896/9,046 ms before and 5,665/5,254 ms after: both preserve all content, but
the one-second P99 budget still fails. RSS ranges overlap; no memory victory or
all-scenario latency/capacity claim follows. Real-provider/browser latency and
sustained soak remain uncovered. Exact identities, bounds and samples live in
[publication evidence](evidence/tofudb_rotation_publication_latency_20261009.json).

Verification: 1,654 native tests pass (four explicitly ignored), including the
new publication I/O error/short-write/lost-sync matrices and real blocked-fsync
foreground tests; 25 provider-ingress/child-delivery tests pass. The candidate
passes independent schema/reclaim/owner read-write probes. The frozen shared
source baseline includes concurrent native work; an initial stale operation
catalog was replaced by its matching frozen contract before the successful
full gate. Performance pairs use disposable authorities. The evidence separately records
a source-matched live activation, unchanged application PID and observed WAL
write progress; it does not claim a live per-user write-latency distribution.

Reproduce the explicit slow-I/O measurement with
`cargo test --lib rotation_publication_paired_latency_measurement -- --ignored --nocapture`;
`TOFUDB_ROTATION_PUBLICATION_BENCH_PARENT` selects a disposable measured volume.
`TOFUDB_ROTATION_PUBLICATION_BENCH_SAMPLES` (4..1,000) and
`TOFUDB_ROTATION_PUBLICATION_BENCH_DELAY_MS` (10..2,000) make the finite experiment
explicit. Ordinary checks keep the paired measurement ignored.


## Selected-generation verification

Problem: after a crash during predecessor publication, CONTROL can select both
an immutable sealed predecessor and an active successor. The offline verifier
anchored only the successor at the older checkpoint, falsely rejecting a valid
chain before inspecting its content. Decision: engine open and verification
share `wal::scan_selected_generation_chain`; validate the exact sealed terminal
sequence/hash first, then anchor the successor there. A corrupt sealed prefix
or acknowledged successor remains fatal; only an unconfirmed successor tail
retains its existing recovery behavior. No scan truncates bytes. Alternatives:
ignoring the fatal verdict weakens the gate; reopening before checking the chain
can alter evidence. Failed workload reports are retained, and supplemental
verification runs on independent copies with the original CONTROL/WAL hashes
checked unchanged. Both reported false-fatals verify completely on those copies;
regression tests also reject corruption in either selected generation.


## Paired foreground preparation experiments

The [parallel experiment evidence](evidence/tofudb_parallel_latency_20261009.json)
retains all ten Strict 1,000-conversation legs, exact recovery counts, identities,
original failures and independent-copy verification. Private preparation from
published roots removes a FIFO entry but is rejected: stream P99 ranges overlap
(6,639/5,158 ms reference, 6,710/5,051 ms candidate) and both settlement pairs
regress. Turn envelope/string reuse also has mixed stream results and is not
enabled. Complete-root history negative proofs avoid a demonstrated cold source
read while sharing the payload-cache budget; their stream legs are 5,121/5,092 ms
against adjacent 10,400/5,778 ms references, but settlement is mixed and the
one-second target still fails. That proof starts absent after reopen. Fewer
operations alone do not justify a new default or a capacity claim.


## Serial commit attribution and further controls

The same frozen harness now retains 24 default-off workload legs. WAL-only
stream staging, soft-rotation trigger release, known-version text-page staging
and online removal of the WAL-only foreground overlay trigger remain experiments.
None passes the one-second stream P99 gate twice. The stream-staging ABBA crosses
a severe volume brownout (reference stream P99 71 seconds before, 8 seconds after);
that cohort cannot establish a stable win. Later page and overlay controls also
have mixed stream/settlement results. Original integrity failures remain in their
reports; supplemental verification never changes an original acceptance verdict.

A separate 1/64-retention diagnostic preserves exact 1,000-conversation recovery
with zero workload errors. Process-wide inclusive means are 0.472 ms for version
validation/rebase, 0.338 ms for stream preparation, 0.046 ms for private blob
publication, and 0.037 ms for deferred indexes. The outbox capture stage has no
calls in this workload, so its production cost is unmeasured. Inclusive stages
overlap and cannot be summed as CPU time or an estimated removable cost. Sampled
request wall time is dominated by preparation-slot and writer-queue waits; these
waits do not identify which work can safely be removed. The next decisions require
original-snapshot versus fresh-head version-read attribution and a separately
measured private stream-payload preparation boundary. Fresh source validation,
OCC, lease budgets, hard WAL ceilings and durable-before-visible remain gates.


## Phase workload correction and source I/O controls

The frozen concurrency harness defaults to `phase`: each round grows the task
content by 8 KiB while its raw event is only 37 bytes (115-byte encoded segment).
Every such event forces a full structural projection fold. Prior attribution to
8-KiB raw-stream encoding or text-tail pages was incorrect. Original phase
reports and failures remain unchanged; delta is an additional shape, never a
replacement for the phase gate. Page-operation unit evidence does not establish
phase coverage. One retained common-source diagnostic measures four phase WAL
records totaling 88,314 bytes for 32,768 bytes of input growth: projection,
checkpoint/patch 45,906; attempt replay 19,290; inline family/overlay 21,038;
raw stream 460; framing/references/root directory 1,620. This is WAL logical-byte
accounting, not filesystem amplification. No command receipt or outbox is present.

Thirteen further default-off Strict 1,000-conversation legs preserve all content
and replay with zero workload errors. Pure private stream encoding regresses both
paired stream and settlement tails; no-loose staging and its combination also
have mixed results. They remain disabled. Current-source FD length querying has
a distinct demonstrated duplicate: loose block reading queries the same open FD,
then RealFile read-all queries it again. A bounded-read candidate reduces this
to one fresh query, retaining source/type, size, complete framing/CRC/hash checks,
original initial-size error text and wrapper failure ordering. Actual returned
bytes also remain bounded; encryption/fault wrappers keep their original path.

Ten paired same-volume samples per arm delay each real FD query by 50 ms,
then SIGKILL the child and require exact readback without Drop. P95 changes
102.393 -> 52.021 ms, with all 20 recoveries exact. The final source passes
1,666 native tests; six ignored entries include the explicitly run measurement
and child entry. Normal phase stream P99 is 5,682/6,533 ms against adjacent
6,048/5,477 ms references, so no stable end-to-end win or new default follows.
The [experiment evidence](evidence/tofudb_parallel_latency_20261009.json) now
retains 37 workload legs, source identities, guards corrected without rerunning
completed workloads, and independent-copy verification with original-source
CONTROL/WAL fences. Offline verifier recovery may mutate its disposable copy.

Fresh head-source reads dominate a separate diagnostic; one 1.25-second selected
read accounts for 95.8% of sampled head-read time. Original-snapshot reads are
small and no new proof cache is added. Root plus internal catalog epoch cannot
license replacing current-source reads: external corruption and VFS failures do
not advance that epoch. Alternatives rejected are skipping those checks, trusting
an unbounded private version table, or treating operation elimination as a gate
pass. Subsequent work targets the existing bounded projection-head representation
with complete phase metadata/replay semantics and separately verifies delta paths.


## Early projection-head and actual delta controls

**Problem.** Repeated phase projections encode mirrored content and replay.
The early-head candidate uses the existing bounded head/checkpoint/patch chain
when its exact next projection is at least twice the patch size. It preserves
metadata, replay, terminal materialization/retirement and the original durable
commit; its optional representations are admitted before staging.

**Decision.** Do not enable it. All 1,709 target tests pass (1,671 primary, four
ignored), and both 1,000-conversation candidate legs recover exactly, but phase
stream P99 is 8,588/6,065 ms against adjacent 5,314/6,013 ms references. Settlement
improves to 5,614/4,586 ms, which does not replace the stream gate. Peak engine
RSS is 1.81/1.71 GB versus reference 1.91/1.99 GB; lower memory is not a latency
pass. A 252-input Git-byte comparison and CLI/DSO identity bind this evidence.

Actual delta page staging also remains disabled. The first four default-300ms
delta legs fail the atomic-carrier fixture in both arms: after a delayed ACK the
fixed schedule immediately catches up, legitimately returning `coalesced`; the
fixture aborts instead of doing production's fenced standalone durable append.
Original failures remain failed. Four new, separately named delta-uncoalesced
legs explicitly set the registered interval to zero, preserving the frozen
harness and original phase gate. All recover 1,000 exact contents and replays,
but stream P99 is 5,565 / 6,144 / 7,828 / 12,290 ms in ABBA order. These mixed
results do not establish stable gain or one-second compliance.

**Alternatives considered.** Reclassifying coalesced frames as durable ACKs would
lose the required persistence fence. Replacing phase with delta would hide the
original gate. Changing language cannot by itself remove measured source I/O
waiting; the current evidence does not isolate kernel, FUSE daemon or remote
metadata service. The next experiment targets ordinary bounded-cache warming
in parallel preparation while retaining writer validation, rather than a proof
cache that could hide current-source faults. The evidence now retains 49 legs,
including the four invalid-fixture failures, not 49 successful workloads.


## Production coalescing and native carriage budget

**Problem.** Production coalesced frames still require a fenced standalone
durable append. Reproducing that path with the production event batcher gives
33–35 ms single-conversation and 321–340 ms 128-conversation stream P99, but
20,484/30,443 ms at 1,000. The second large run lacks confirmations for eight
frames and completes only 992 exact contents/replays. Failed confirmations are
not evidence that an acknowledged frame was lost. Native logs show batch
authority holds of 5,731/8,313/15,805 ms. The fixture does not deliver actual SSE
or exercise real providers, sequence allocation or mid-stream crash recovery.

**Decision.** Engineer byte-aware durable batch composition. A separate
low-concurrency storage-only experiment submits the same 500 unique 8-KiB rows
using the same native source and volume. A single500 batch crosses the native
2-MiB carried-payload limit, enters one oversized spill and ACKs in 3,859/3,760 ms
(two repeats). Four125 batches avoid spill and total 293/201 ms. All four arms
SIGKILL after strict durable ACK and recover every one of500 exact events. The
spill path writes/synchronizes fresh loose files and directories inside the
commit hold. This is a causal mechanism win, not a 1,000-conversation target pass.

**Alternatives considered.** Raising timeouts/retries would preserve the long
hold. A fixed tiny item limit ignores event size and machine capacity. Raising
the WAL format limit needs a separate recovery/compatibility decision. The
planned implementation exposes native format costs through cached readiness,
limits composition by those costs, preserves resource-scaled queues, and keeps
legal oversized single events on the original durable path. Older servers or
other backends preserve existing behavior when capability is absent.

The four additional default-versus1-MiB trials crossed external compilation and
severe volume/admission brownout: A1/B1/A2 fail; B2 recovers exactly but includes
confirmation timeouts and original fallback. Their timings establish no win.
The ordinary head-cache prewarming candidate also remains rejected: both phase
stream legs regress (7,906/9,118 versus5,231/7,129 ms references), despite1,678
passing native tests and exact1,000-conversation recovery in all four legs.
Packed recovery-cache reuse has zero running-restart coverage and only9 buffers
(2,245 bytes) at final restart, so it is not enabled for target stream latency.

Independent GC inspection found source reads during authority capture and
directory/orphan planning under the hold. The GC-only capture repair is being
verified separately; neither a busy background CPU sample nor these source paths
prove the still-pending conversation's current cause. Evidence now retains63
concurrency legs, including small-shape/failed controls, plus four separate
storage-only causal arms. Original policy verdicts remain unchanged.


## Measured native event composition and remaining limits

Problem: default 300 ms delta coalescing legitimately sends standalone fenced
events through the application batcher. Its wire budget allowed raw stream
bytes to exceed the native 2 MiB carried ceiling and trigger synchronized loose
materialization under authority. Decision: native encoder-derived composition
costs limit each group while preserving individual admission and Strict ACK.
The same frozen client with old/new native ABBA recovers all 5000 frames per
leg. Stream P99 improves from 17.10/17.76 s to 7.49/6.42 s; settle P99 improves
from 8.27/8.69 s to 2.81/1.09 s. Failed batch events 733/500 and cancellations
351/432 become zero. Candidate native RSS is 1.725/1.648 GB versus reference
1.551/1.571 GB: this is a latency improvement, not a memory reduction. The
1 s stream target still fails. These are synthetic-provider storage/Turn
component results; browser delivery and real providers remain uncovered.

The separately integrated GC change captures selected immutable metadata
without root IO under authority. Source marking remains fresh, budgeted and
cancellable outside that mutex. An actual single target 200 ms delayed read
reduces foreground durable ACK from 204 ms to 2.45 ms, with four exact SIGKILL
recoveries. This proves isolation, not a natural filesystem delay or a complete
1000-conversation win; eager backup capture is unchanged. Combined current
native source a55a4fd0 passes 1674 tests, six ignored, with matching CLI/DSO.

Alternatives measured and deferred: native queue768 derives from SQLite IO12
and fills at only 7.33% of its96MiB budget. Explicit2048 removes fallback
rejections, but automatic CPU-derived2016 has a second stream12.22s/settle6.24s
regression, so it is not integrated. The previously rejected full-stream
carried-staging axis is reexamined on actual defaultdelta multirow writes, not
called a new mechanism. It eliminates an optional dedup query in a200ms causal
test, but normal ABBA has one stream regression and one improvement; it remains
unselected. Current-source reads, packed/cache checks and oversize spill are
retained; the small-corrupt-loose error timing change is explicitly recorded.

Profiler-enabled attribution is excluded from latency comparisons. Encoding is
0.24% of multirow hold; block staging is62.39%. WAL timing includes framing,
write wrapper and notification, not pure syscall cost. Separate diagnostic
work must distinguish write waiting from thread CPU before selecting a larger
commit-pipeline change. All old reports, failures, setup errors, exact artifact
hashes and these15 added cohorts remain in the linked parallel evidence.

The live bound control snapshot is ready with zero authority contention and no
GC/backup epoch at that moment. It does not diagnose a specific hanging
conversation. That worker started before composition integration; source
integration and candidate test results alone do not prove live activation.


## WAL write attribution and live activation boundary

Problem: elapsed WAL time alone does not distinguish framing CPU from blocking
physical writes. Default-off test instrumentation measured actual writev wall
and calling-thread CPU. A profiler-enabled cohort has external compilation
contamination and is excluded from SLO comparisons. Its 3042 single-event
writev calls total1668 ms wall versus99 ms CPU, all inside authority hold.
For12 multirow RPCs, physical write accounts for only7.7% of authority hold;
block metadata is still a separate major cost. These measurements do not
identify a kernel or filesystem subsystem as the cause.

A separate frozen test uses existing append/append_batch APIs on this FUSE
volume, with16 ABBA arms and64 frames per arm. Both sides use the same final
Strict double-sync and prefix witness before ACK. Actual calling-thread write
syscalls fall64 to1 while bytes stay equal. Observer-free write-wrapper wall medians fall20.10 to1.46 ms at1KiB/frame
and34.76 to15.08 ms at43KiB/frame. Outer append totals include64 versus1
proc counter observations and are excluded from optimization claims. Existing
append_batch concatenates into one Vec then calls write_all_at; it does not
aggregate writev. At43KiB, its framing/group-copy CPU increases about20%. All1024 frames recover exactly after SIGKILL.
Decision: retain this component evidence before attempting production grouping.
Alternatives: changing language has no evidence here; merging independently
prepared authority roots can lose updates, and ACK before durability is invalid.
No production WAL scheduler or asynchronous writer is enabled by this test.

After the earlier evidence window, the authorized native-only activation
replaced ac2385 with combined a55a4fd0; the actual executable hash matches the
prepared artifact and bound control calls report ready and healthy. The parent
application stays3842043 and predates Python composition integration, so that
part still requires a one-time UI-approved application restart. The live
configured WAL mode remains normal; Strict benchmark guarantees must not be
attributed to that configuration. Old actual data-directory argv was not
captured, so the receipt cannot prove exact old/new path equality or full
user-data recovery merely from control readiness. Source and configuration
integration, component recovery, and live activation remain distinct claims.


## Worker startup isolation and incident diagnostic limits

Problem: the October 10 thread snapshot contains 2869 tree-index worker sections,
with 2749 top frames acquiring locks, rather than waiting on Condition.wait.
The frame evidence alone cannot prove that all were acquiring the same scheduler
condition. A controlled 200 ms physical Thread.start block independently proves
that the prior worker kernel held its condition across startup and delayed an
already-running job's completion 200.327 ms. Moving startup outside that condition
reduces completion to 0.02993 ms; both accepted jobs still finish exactly.

Decision: integrate the reviewed startup reservation/rollback fix and its 23
passing tests, including the necessary pre-existing Demand kernel, facade,
packaged copies and contract-test extraction. The shared commit is ce5a82dd.
It adds no active-worker cap or durability change. Startup remains serialized;
this component test does not prove whole-application or 1000-stream latency.
The running Python application has not activated that source at this boundary.

The anonymous ResourceExhausted diagnostic is integrated as 09f5be0e: eight
bounded counters consume 64 bytes and reason formatting uses 256 stack bytes,
without per-request logging or exporting original errors. Its 13 focused and
neighbor checks pass. Returned reservation failures include capacity overflow;
these errors do not establish host OOM. Direct semantic errors and allocator
aborts remain outside this observer. The old live efa9 binary lacks it; the
exact 48f snapshot passes 1707 tests, zero failures and six ignored in 455.86 s.
Current shared 49a source adds cache/resource/VFS changes and is not covered by
that gate. Canonical current-pair compilation and activation remain separate;
source drift during its build fails closed.

Alternative measured: private foreground-merge trials eliminate 1475 rejected
shared pending pages and reduce staged bytes from 7.27 MB to 1.09 MB. Four FUSE debug arms
retain identical current/old-pinned/reopened rows and roots, but wall latency is
mixed and thread CPU rises about 6%. Do not integrate this candidate. Graceful
reopen is verified; these arms do not claim SIGKILL or production 1000 coverage.

Health conclusions use the contractual app /health/live and /health/ready,
later both 200, and manager /health. The earlier app /health timeout used a
non-contract path and does not prove app live/ready failure. Native ready and
zero-contention observations are bounded snapshots. The original 1000-session
stream P99 below 1 s target remains unmet. The linked phase 9 evidence preserves
all 78 earlier reports and hashes without rewriting their claims.


## Current native pair verification and pending activation

The later canonical pair binds both CLI and loaded DSO to official source
49a0fc86. CLI SHA is f7f25fcb and DSO SHA is 35124137; identity records the full
hashes, mimalloc allocator kind1 and NODELETE. Its preflight exits successfully:
native compilation takes222.2 s and frontend validation1.3 s. Source-matched
resource/VFS/block/cold-read and explicit synthetic hot-cache checks total64
passes and zero failures. This is separate from the preserved 48f full suite;
no full-suite result for49a is implied.

Configured cache capacities sum to51/512 of headroom by default, or67/128 with
both maximum overrides, subject to legacy floors. These populate on demand and
are accounted capacities, not RSS or a bound on all process memory. The decoded
reuse measurement excludes source IO, JSON, RPC and whole tasks. Observed-length
metadata now precedes the VFS_READ span; reductions of that span therefore do
not wholly establish faster reads. Actual bounds, source checks and wrapper
fault boundaries remain the relevant correctness limits.

The live native process still uses old efa9 and Python still uses kernel8b66feb
at this evidence boundary. A UI-approved application restart is pending;
validated source and artifact identity do not prove live activation or a
1000-session stream P99 below1 s result.

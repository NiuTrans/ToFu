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
`tofudb_rotation_publication_latency_20261009.json`.

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

The `tofudb_parallel_latency_20261009.json`
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



## Batched OCC cache borrowing

Problem: sorted version probes share tree paths but still reacquire the decoded
page-cache read lock and record its wait for each node. Decision: a warm batch
borrows one read lock. Its first missing node releases the borrow before source
I/O or cache insertion; the rest of that batch uses the original per-node path.
Empty probes, depth limits, tombstones, mount masks, poison and source failures
retain their existing results. No persistent state, resident cache, capacity,
worker or durability policy is added. Alternatives rejected: retaining the lock
through a miss deadlocks cache insertion; suppressing source verification or OCC
changes correctness; treating a smaller sampled wait as application speedup
ignores source/merge mix and observer cost.

Three ABBA rounds per concurrency over the same release executable and fixed
hot pages verify 216,000 exact 32-key queries. Median calling-thread CPU per query
changes 13.51 to 11.46 us with one caller and 43.55 to 28.30 us with eight callers.
The latter sums worker CPU, not wall time. Every arm charges 5,074,296 cache bytes.
This component control compares borrowed traversal with its per-node fallback;
it excludes cold I/O, background merge and application delivery.

Actual Strict/FUSE 1,000-conversation ABBA stream P99s are 4.314/3.313/3.954/4.939 s
for delta and 5.515/3.626/3.736/3.626 s for phase. Delta improves in this cohort;
phase and settlement overlap. All one-second stream/five-second terminal gates
still fail. All fourteen pipeline legs, including four diagnostic legs, retain
exact content/replay through two SIGKILLs each. Four paired operation controls
show no stable point-read or large-append regression, while raw SQLite remains
faster at their medians. Full native tests pass: 1,892 library and 38 process
checks, with eight existing ignores. The `tofudb_occ_cache_borrow_20261014.json`
retains every report, failed control setup, frozen source delta, harness and CPU
samples; profiling diagnostics are excluded from latency admission.



## Optional entity read timers

Problem: even with request profiling disabled, each entity page read recorded
clocks and four global/component atomic updates per stage. Decision: the six
node/read-lock/source/decode/write-lock timing sites use the existing launch-time
`TOFU_DB_REQUEST_PROFILE_EVERY` opt-in. The five stage names remain unchanged.
An absent stage means no recorded observations, not zero work. WAL/commit
counters and background pacing remain always available. No read, cache,
verification, ownership, durability, worker or resource-budget policy changes.
Alternatives rejected: retaining all hot read probes burdens normal execution;
removing them prevents diagnosis; disabling scheduling feedback changes behavior.

One release executable, launched off/on/on/off, executes 864,000 exact warm
32-key version queries with no dispatch samples. With cache borrowing, median
summed worker CPU/query changes 11.52 to 8.90 us for one caller and 28.35 to
14.69 us for eight. All arms charge the same 5,074,296 cache bytes. Enabled arms
record 7,494,960 node probes; disabled arms record none. Page-store counts remain
308 in every process. This isolates timer cost, not application latency. With
probes disabled, borrowing itself changes eight-caller CPU 31.86 to 14.69 us;
the single-caller difference is small (8.97 to 8.90 us).

Strict daemon/wire 1,000-conversation phase ABBA stream P99s are
5.247/3.064/3.494/5.320 s, settlement 9.088/7.634/8.264/9.404 s. Delta results
overlap, all SLO gates still fail, and embedded point reads remain about 21 us
against raw SQLite's 6 us. Ten pipeline legs retain exact recovery through both
SIGKILLs; actual metrics verify disabled/enabled read stages, retained WAL/commit
counters and reconciled sampled spans. Embedded mutation profiling currently
uses blocking dispatch rather than phase continuations; this wire pipeline and
embedded query controls do not change execution paths with that setting.
Full native verification and all adverse legs, identities, source deltas and
CPU samples are retained in `tofudb_optional_read_probes_20261014.json`.


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
The `tofudb_parallel_latency_20261009.json` now
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


## Full-catalog history promotion and write-delivery anchors

Problem: history pruning at the payload catalog ceiling fell back to a durable
loose file per carried block even when existing segments had packing room.
The unlocked work still reserved the CONTROL publication lane, so the active
WAL could fill before rotation was allowed to publish. Processing the entire
history backlog in one reservation magnified this failure mode.

Decision: history promotion replaces the smallest compatible payload segment
through its verified exact handle when no new catalog slot is available.
Incoming authenticated history bytes win duplicate IDs; the existing block,
segment and catalog format bounds remain authoritative. Sources remain for old
readers and CONTROL fallback until ordinary GC proves reclamation is safe.
Automatic pruning retires at most the launch-derived checkpoint materialization
budget divided by the history segment byte ceiling per publication (minimum one
source); pending overlay replay can retain more. Operator compaction keeps its
complete suffix semantics. No extra worker, journal, storage authority or mode
is introduced. Truly indivisible full segments retain the durable loose fallback.

The additive content-free `system.metrics.historyPromotion` counters distinguish
prepared packs, full-catalog replacements, source bytes and attempted loose
writes. Four process-lifetime atomic counters retain 32 bytes and reset at exit;
preparation counters are not proof that CONTROL publication committed.

`benchmarks/storage_write_pipeline.py` drives the production event FIFO before
real semantic writes, checks push only after ACK, and delegates running/final
SIGKILL, exact content/event replay and offline integrity to the retained
conversation harness. Anchors separately measure FIFO delay, storage ACK,
ACK-to-push and accepted-event-to-push/barrier. Client pool/wire/native profiles
remain separate nested observations; do not sum them as independent costs.
Client stages default off; `--profile-client`, `--native-profile-every N` and
`--delivery-profile-every N` opt into diagnostics and disqualify latency admission.
Earlier pipeline runs forced client stage sampling on even when native/delivery
sampling was zero. Reports containing `client_stages_ms` are diagnostic and
cannot support an unprofiled latency verdict; preserve their correctness and
raw observations. The qualification audit
records the affected retained comparisons and the corrected default-off rerun.
The operation microbenchmark also retains unsorted collector-input latency
samples outside timed calls and six-decimal millisecond summaries. Its former
one-microsecond rounding obscured changes near the six-microsecond SQLite floor.
Sequential samples identify operation-start order; concurrent collectors do not
claim one global timeline. Window summaries avoid duplicating the raw series.
Higher numeric precision does not change sample-count requirements for P99.
The provider is a barrier fixture and push is an observer; real provider and
browser/network rendering latency remain uncovered. Initial fresh authorities
also do not reproduce a production-sized full catalog or sustained soak.

Run `python3
benchmarks/storage_write_pipeline.py --binary /path/to/tofu-db --directory
/path/to/disposable-volume --conversations 1000 --submission-mode queued --output /tmp/pipeline.json`.
The exit status requires correctness, latency and coverage; `acceptance` retains
each result independently. Failures remain evidence and never become a speedup
denominator. The native ignored `history_promotion_full_catalog_paired_measurement`
uses alternating real-filesystem preparation pairs; set
`TOFUDB_PROMOTION_BENCH_PARENT` to the measured volume. Ordinary native tests cover
full-slot replacement, exact source reads, old readers, crash before publication,
format refusal, slice convergence and existing publication fault matrices.

Alternatives rejected: larger catalog ceilings change protocol/replay bounds;
more retries leave slow representation work in place; deleting source files
when packing finishes breaks old readers and fallback CONTROL; one hardcoded
worker/item limit prevents throughput from scaling with launch resources.


Controlled promotion evidence is retained in
the storage-stall measurement.
Four alternating DolphinFS pairs prepare 128 distinct 8-KiB blocks. With a
small replacement source, loose/packed medians are 1,112/24 ms; with a 16-MiB
source containing 2,049 blocks they are 1,050/166 ms (about 6.3x). Every sealed
replacement and old reader returns exact bytes. The larger-source case writes
about 17 MiB of merged payload instead of 1 MiB of loose input: speed reduces
filesystem operations while increasing bytes copied. Existing GC owns old
source reclamation; this is not an end-to-end speedup or a storage-size win.

The final source-matched mimalloc pair completes 1,000 running conversations,
5,000 ordered push observations, both SIGKILL cycles, exact content/event
readback and offline integrity. Its offered-load stream P99 is 5,399 ms and
settlement P99 is 7,472 ms; the 1,000/5,000-ms budgets remain unmet. A fresh
catalog performs no full-catalog replacements, so that cohort cannot measure
this incident branch's improvement. Browser renderer and native-backed
navigation/draft recovery cover their declared components; they do not measure
real-provider or continuous network/SSE-to-visible latency. Whole-system
performance acceptance remains incomplete.


## Full-chain anchors and waiter isolation

Problem: durability barriers shared the worker condition, so every unrelated
commit woke all blocked tasks. Decision: keep one state lock but give each
live lane a coordinator-owned weak condition; completion, failure and
revocation target that lane. Poll only when an explicit cancellation callback
requires it. Alternatives rejected: larger pools leave quadratic wakeups;
displaying pending bytes before commit weakens durable replay.

The controlled 256-lane ABBA sink measured 44,894/46,329 wait returns before
and exactly 256 after; drain time was 727/738 versus 226/227 ms. This measures
scheduler amplification, not native throughput. Native 1,000-conversation
comparisons remain noisy and miss the 1-second stream/5-second settlement
budgets. Private stream preparation and cross-task batch prototypes were
therefore rejected for default activation. Wire size alone did not bound
materialized WAL carry: an unbounded prototype reached 172–187-second P99;
precise pre-WAL refusal and frozen child splits removed that regression but
did not establish a stable latency win. These prototypes are not deployed.

The retained pipeline now supports both per-frame barriers and queued
progress, retains every prefix frame, and records scheduled-to-committed-push
latency independently of producer submission time. Correctness alone cannot
pass its performance gate. The queued 16-conversation/32-round burst recovered
all 528 frames and exact content after abrupt daemon loss, with stream
accept-to-push P99 166 ms. The 1,000-conversation/4-round case recovered all
5,000 frames but still failed latency acceptance. Browser evidence uses actual
named SSE receive timestamps: held commits never paint early, all eight
markers have native ACK/receive/DOM anchors, and reload renders each once.
Maximum observed ACK-to-SSE was 214 ms; SSE-to-DOM was 19 ms. Providers are
synthetic; sustained soak and paid-provider capacity remain uncovered.

Reproduction: `benchmarks/task_delivery_waiters.py --reference <frozen-source>`;
`benchmarks/storage_write_pipeline.py --submission-mode queued
--stream-interval-ms 0`; native-backed visual test
`tests/e2e/test_e2e_storage_stream_pipeline.py`. Exact source identities,
measurements, rejected controls and declared coverage live in
`tofudb_storage_pipeline_deep_20261011.json`.

## Complete-payload writer staging revisited

Problem: fresh event segments used the generic block publisher, searching history and loose paths while holding authority. Complete Blob publication also searched cold history for optional deduplication. These paths can make a writer wait for source I/O even though it already owns the exact bytes required for recovery. Earlier WAL-only stream experiments had mixed/brownout timing; this revisits the combined path after authority-owned parallel codecs, not a new stream format or an assumed improvement.

Decision: after the unchanged scope, sequence, event and segment-byte checks, new segments use the existing owned fresh-page staging port. Complete Blob publication retains verified warm-cache/read-hint and packed reuse; a cold history copy is optional and is carried again instead of querying it under authority. Generic block/reference validation keeps its original history lookup and error behavior. User reads, source membership, framing/hash checks, alternative copies, conflict checks, oversized spill/materialization and Strict durable-before-visible remain mandatory. Materialization still validates existing copies before retiring WAL-owned bytes. No worker, cache, backend or durable format is added. Duplicate cold payloads can increase WAL bandwidth and pending residency; paired measurements must include those costs.

Alternatives: deleting the generic source probe hides real errors; trusting a bloom hit can reference absent content; a new negative-proof cache adds lifecycle and completeness machinery; larger caches do not eliminate first-time miss I/O. Existing complete-payload staging already provides the needed recovery witness.

`stream_segment_encode` and `stream_segment_fresh_publish` distinguish encoding from memory staging. Fixed work counters report published segment count/encoded bytes and omitted optional cold-history-dedup attempts. The latter can increment when no history catalog is installed; it is not a physical-read savings count. Two span slots plus three work slots add 72 bytes per sampled request/aggregate; reported active/retained sizes derive from their structures. Sampling remains off by default.

`authority_stream_staging_tests.rs` proves new-segment commit avoids old-source Read/Metadata operations, survives simulated power loss exactly, and refuses further writes after an ambiguous WAL error. `engine_write_staging_tests.rs` stages cold duplicate Blob bytes through direct/private publication without reader I/O, preserves PermissionDenied for generic source validation, and verifies exact restaged bytes after power loss. Full native snapshot: 1,837 pass, eight existing ignores. Natural workload measurements and adverse costs are recorded in `tofudb_writer_staging_20261013.json`.

Matched client-profiled ABBA stream P99 is 5,977/4,114/5,010/6,389 ms (~26.2% lower candidate mean), but all fail 1,000 ms. Settle P99 is 9,752/9,468/9,997/9,743 ms; B2 settle and peak RSS are adverse. Every leg preserves 1,000 exact content/replay records, 5,000 ordered frames and zero errors after both SIGKILLs. The full-sampling candidate reports 16,238 omitted optional dedup attempts and 28.44 MB of fresh stream bytes against 170.33 MB of WAL writes in sampled event-record requests; counts/coalescing differ from other diagnostics. Remaining write and source-I/O costs need separate measurements; this step does not establish completed optimization.

## Rejected current-WAL carried-payload prefix witnesses

Problem: pending blocks were carried again even when an earlier record in the current file already contained their bytes. A generation-bound Engine-owned positive ID set was tested without changing the envelope or ACK protocol. The prototype cleared on generation changes/close, restored original slots when checkpoint changed the file after selection, and started empty after reopen. Four targeted power-loss/ambiguity cases and the full 1,841-test native snapshot passed (eight existing ignores).

Decision: reject runtime selection. Client-profiled ABBA stream P99 4,218/6,297/4,068/5,053 ms regresses in candidate mean (~11.8%), while B1 peak RSS is adverse. The full-sampling event requests omitted only 0.438 MB against 129.754 MB of WAL writes (<0.4%), with 34.666 MB inline and 93.857 MB carried. Saving a small residual copy does not justify a new resident index or claim a broad win. The isolated experiment commit `7ef41cf8b` and its revert retain the tested implementation; `tofudb_current_wal_payloads_20261013.json` retains all original reports. The default runtime continues complete carrying and original durability/source validation. Alternatives still excluded: pending-memory presence as durability, cross-generation/cold inherited coverage, and hiding adverse cohorts.

## Physical WAL writes on the existing barrier worker

Problem: per-record physical WAL writes still occur while holding the foreground writer, even though Strict callers already wait outside it for the existing shared barrier. Small requests amplify the number of FUSE write operations. Moving the write must preserve the actual byte-prefix witness rather than acknowledge queued memory.

Decision: the existing barrier worker owns framed copies, combines only contiguous offsets, physically writes them and syncs before advancing its captured prefix. Later enqueues do not enlarge that captured ACK. The actual authority VFS must opt in to independent handles; encryption and custom stateful wrappers keep original physical appends. Normal durability is unchanged. CONTROL publication, rotation, recovery reads, truncation and close drain the original generation first; failed write/sync stays latched and prevents an inline retry. Close refuses new enqueues, drains and joins before reclaiming frame copies. Poisoned/unknown buffer state keeps the failed barrier and refuses detach, source recovery reads, truncation and inline physical retry; it is never treated as a disabled buffer. Driver close still joins before returning an unknown-drain error.

The canonical `walWriteBuffer` contract in `contracts/storage_v2.json` derives reuse bytes from the existing startup memory-headroom probe. Unknown probe falls back lean; zero available budget disables copying; budget or allocation exhaustion uses the original write path and never rejects an otherwise legal request. Retained/peak bytes include queued and inflight frames through deallocation, excluding allocator/queue metadata. Metrics separate logical accepted WAL traffic from successful physical worker writes and write-time attempts; neither gives durability credit. Request-local VFS write totals exclude the worker and must be read with its separate counters. No additional worker, authority, persistent format or durability mode is introduced.

Alternatives: enlarging queues hides waiting and raises residency; a new append thread duplicates the barrier lifecycle; skipping synchronization weakens durability; counting moved I/O as eliminated misstates the mechanism. A paired runtime gate must establish the gain and report memory and settlement costs.

Matched client-profiled ABBA compares physical writes against complete-payload staging: stream P99 is 4,281/3,744/3,105/4,948 ms; scheduled-to-push P99 is 4,380/3,826/3,201/5,018 ms. Candidate stream mean improves 25.8%; settle mean improves 4.3%. Candidate peak RSS is 1.98/1.91 GB versus 1.96/1.95 GB; B1 memory is slightly adverse and remains reported. Full sampling is excluded from these means: its 4,252 queued frames become 2,680 successful physical batches, carrying 476.43 MB with 3.56 MB peak frame residency and zero final retained bytes at the captured snapshot. Logical request WAL bytes and total-lifetime physical bytes cover different cohorts and must not be subtracted as saved traffic.

A separate combined ABBA compares both changes with the previous parallel-codec runtime: scheduled-to-push P99 is 8,256/3,469/3,465/6,850 ms, reducing candidate mean by 54.1%; settle P99 is 11,940/8,398/8,242/8,967 ms, reducing mean by 20.4%. All nine new legs retain 1,000 exact content/replay results, 5,000 ordered committed push observations and zero errors through two abrupt process losses. Diagnostic clock anchors pass; profiling-off legs declare that check inapplicable. Every 1,000-ms stream/5,000-ms settle gate still fails. Remaining FIFO, source-read, rotation and barrier waiting remain measured. `tofudb_wal_write_buffer_20261013.json` retains all formal and diagnostic legs in its companion gzip. Native library verification passes 1,845 tests (eight existing ignores), eight buffer boundaries, two large-restart fixtures, 22 Python neighbors and all-targets compilation. Documentation and architecture baseline blockers remain failed checks.

## Captured buffered WAL coverage for physical publication

Problem: an independent file fsync cannot cover bytes still owned by the WAL worker. Rotation and history pruning formerly covered only the file; pack adoption also assumed Strict had no pending bytes. This assumption stopped holding once Strict appends handed physical frames to the existing worker.

Decision: `wal_publication.rs` captures the current logical length and generation-bound worker witness while the publisher owns authority. The existing physical publication lane retains that source. Outside authority, all three publishers wait for that exact prefix, perform the shared double-sync loss barrier and only then publish CONTROL. Later queued frames do not enlarge the captured witness. A failed write/sync or poisoned source refuses publication and retains restart-required recovery. No new worker, admission, durable format or source-proof cache is added. Alternatives rejected: syncing the empty descriptor, treating queued memory as durable, and blocking the foreground authority while waiting for the worker.

## Rejected soft-trigger handover reuse

Problem: an Engine-level soft trigger appeared to reject a writable successor and could amplify a task's FIFO. The isolated candidate admitted only an empty, unfenced successor after durable handover, keeping the hard path unchanged. Its cold and post-publication power-loss cases and per-I/O fault matrix passed; the native snapshot passed 1,855 tests (eight existing ignores). Earlier unsafe descriptor-only WAL cover was fixed before building either arm.

Decision: reject this runtime candidate. Client-profiled ABBA scheduled-to-push P99 is 3,164/3,207/3,536/3,518 ms: candidate mean regresses 0.93%; settle improvement is only 0.52%, and B1 RSS rises to 2.30 GB against baseline 1.98/1.96 GB. The full-sampling run records zero soft-trigger reuse decisions across sampled operations, so this experiment did not expose the expected mechanism on the default business path. Fewer diagnostic rotation parks alone cannot establish a gain across different coalescing cohorts. Every leg still recovers 1,000 exact contents/replays and 5,000 ordered frames with zero errors; all latency gates fail. `tofudb_soft_rotation_20261014.json` retains the original reports in its companion gzip. Candidate `15ca839ae` and its revert preserve the implementation; no counter or admission behavior is enabled by default. The retained hard-ceiling test now sets the threshold to the actual format ceiling, rather than counting the earlier soft refusal as a hard-cap test. Alternatives excluded: declaring a small or inactive-path change a broad win, ignoring B1 memory, or comparing against the unsafe publication baseline.

## Rejected original-snapshot OCC preparation (2026-10-19)

Problem: one current-source diagnostic attributed 0.251 seconds of 5,000 IR
commits to resolving original blind-write versions under authority, alongside
1.093 seconds of fresh-head version reads. These inclusive wall stages identify
placement opportunities, not removable CPU. The experiment captured only the
original immutable snapshot versions on the preparation replica and retained
them as ordinary, accounted point witnesses. Fresh-head reads, owner checks,
conflicts, durable publication, receipt ordering and recovery remained unchanged.
The transaction lease reserved the retained keys before allocation and released
them on errors or cancellation. No cache, worker, pool or queue limit changed.

Decision: reject the production change. In the same-source release ABBA below,
one candidate improves and the other regresses. The average of the two per-run
settlement P99s increases from 6,730.5 to 7,308.9 ms (8.6%); every thousand-
conversation arm still fails both the one-second stream and five-second
settlement budgets. Lower average total CPU and stream P99 do not establish a
stable improvement in completed work. The production source is unchanged;
the full candidate patch and binaries remain with the evidence.

| 1,000 conversations, order | Stream scheduled P99 ms | Settle P99 ms | Event wall ms | Whole process-tree CPU s | Daemon peak MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline A1 | 4,912.3 | 8,006.9 | 13,775.4 | 66.705 | 3,257.2 |
| Candidate B1 | 2,593.9 | 6,044.1 | 10,001.7 | 52.228 | 2,611.9 |
| Candidate B2 | 5,661.3 | 8,573.6 | 14,479.8 | 68.362 | 3,254.8 |
| Baseline A2 | 5,882.8 | 5,454.1 | 13,846.7 | 66.610 | 3,495.1 |

The mean of per-run stream P99s falls from 5,397.6 to 4,127.6 ms; mean complete
process-tree CPU falls from 66.658 to 60.295 seconds. Both gains coexist with
the settlement regression and the second candidate's longer event wall. These
are finite paired samples, not pooled percentiles or a confidence guarantee.

Single-user and 16-conversation neighbors use the same four 8-KiB phase updates,
one-second arrivals and two real SIGKILL/reopen boundaries. Their small sample
counts do not establish stable tail latency. Means and total CPU retain the
default-user tradeoff instead of hiding it behind the high-concurrency result:

| Conversations / ABBA order | Stream scheduled mean ms | Settle mean ms | Whole process-tree CPU s |
| --- | ---: | ---: | ---: |
| 1 / A1 | 11.21 | 6.72 | 2.249 |
| 1 / B1 | 11.10 | 6.34 | 2.151 |
| 1 / B2 | 10.91 | 9.42 | 2.154 |
| 1 / A2 | 11.66 | 5.41 | 2.187 |
| 16 / A1 | 20.13 | 28.62 | 2.675 |
| 16 / B1 | 22.36 | 28.17 | 2.675 |
| 16 / B2 | 19.71 | 27.95 | 2.663 |
| 16 / A2 | 20.22 | 29.14 | 2.707 |

Each leg records N accept, bind, start and settle operations plus 4N stream
operations; correlated submit/scheduled measurements are not additional work.
All 12 legs preserve exact content and replay, running and completed recovery,
and independent integrity: 4,068 conversations and 16,272 stream events in total.
Low-concurrency legs satisfy their latency budgets but remain below the unchanged
1,000-conversation coverage gate. The candidate separately passes 63 focused
tests: preparation I/O/receipt/outbox rollback, OCC update/delete/ABA conflicts
across overlay merge, foreign-authority rejection, retained-byte release after
capture I/O failure, and existing optimistic/server neighbors. No full-native
candidate pass is claimed for this rejected experiment.

The same external observer samples resources every 100 ms, distinguishes daemon
from offline verifier and guards PID reuse with process birth. Thousand-session
daemon peaks are 969–1,013 threads and 1,067–1,112 FDs. Terminal metrics return
to 257 active connections (the existing 256-idle pool plus observation); all
observed authority processes exit. These samples do not prove sustained idle
memory or kernel-memory bounds. CPU is the `RUSAGE_CHILDREN` delta after waiting
for the complete benchmark process tree, including startup, client, daemon,
recovery and verifier; it excludes the external observer. It is not hot-path
CPU or wall time. The large native RSS remains an unresolved cost.

Alternatives rejected: replacing fresh source reads with an internal epoch
proof would miss external corruption; bypassing OCC or durability changes the
contract; reporting only the faster candidate leg hides the counterexample.
Unconditional capture can also add original reads when snapshot equals head,
where existing validation returns immediately. This is why the low-concurrency
neighbors are retained even though they do not provide precise micro-costs.

Evidence lives under the ignored local directory
`data/benchmarks/tofudb-parallel-20261019/stream-occ/`: all twelve reports and
resource series, runner, summary, test/build logs, source patch and frozen
artifacts. Both arms use the ac1df5394 Python client/fixture (benchmark SHA-256
`2d96fe8c83d53e19ddf270b82821b5592fa16e0c408f0bec5065ccb4aa4e424a`),
Strict durability on local XFS, profile off and the same release/mimalloc profile.
Baseline native source is
`7d2a42adee860fa774f2815c17f00e7310889072f689458bb5a589f62df494f1`;
candidate is
`f0cf2a29e168f35885c93bb580283ad7266df3e8d57cc57004c146f654f4c1fb`.
The candidate excludes the separately rejected listener experiment. Timed
windows exclude other agents' builds, tests and archive/worktree I/O. This
experiment compares two native versions; the current-schema SQLite controls
and their retained readiness failures belong to the separate listener cohort,
not to a claimed OCC-versus-SQLite victory.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

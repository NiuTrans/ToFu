# Embedded worker delivery and allocator locality

Owner: `packages/tofu-db/src/{embed_job_queue,embed_ffi,fsync_stats}.rs`.
Authority: [TofuDB storage](STORAGE_TOFUDB.md) and
[response ownership](TOFUDB_EMBEDDED_RESPONSE.md).
Measurements and failures: `tofudb_worker_delivery_20261002.json`.

## Problem

The fixed embedded pool queued independent calls behind its worker count and
refused new jobs when a count-bounded deque filled. This spent request/watchdog
time on local slots. Sequential calls still benefit from recently idle reuse.

The engine already performed warm overlay lookup in about 0.1 microseconds
in a sampled corpus. Queueing, general dispatch, response delivery and client
conversion were materially larger. That observation applies to the measured
warm corpus; it does not establish the cost of cold or materialized reads.

## Decision and invariants

A submitted job claims a recently ready worker under one handoff lock. If none
is ready, its owned job goes directly to a demand-created worker as that worker's
first request. It is never published to a shared backlog first. Thread creation
failure releases the owned payload and byte lease without poisoning unrelated
calls or duplicating dispatch. Workers are created lazily, not during open.

Parking follows registration under the same queue mutex. An `unpark` token
covers submission before the worker actually parks. Spurious wakeups cannot
register a worker twice; a receiver that claims a job removes any previous idle
registration. Sender destruction closes admission and wakes all idle workers.
After closure, receivers drain already queued jobs before returning closed.
Last-receiver destruction disconnects admission and releases abandoned jobs
outside the queue lock, including their input-byte reservations.

Launch resources derive the idle reuse target and thread stack size only;
active workers have no count quota. Surplus carriers retire after completion
instead of parking beyond the idle target. A worker can announce its owned
nonblocking completion tail before reply delivery, preserving sequential reuse.
The short deque contains only jobs handed to ready carriers, not a count-limited
backlog. Closed/poisoned delivery remains fail-closed.

[Certified cache hits](TOFUDB_QUERY_RESULT_CACHE.md) reuse owned results without engine execution.
Misses retain deadlines, admission, byte ownership, strict WAL barriers and watchdogs.
Workers own requests/channels; no caller buffer enters a worker. Panic or
timeout still poisons and disarms the handle; late replies remain owned and
disposable. Request byte pressure is a separate boundary still requiring audit.

Alternatives: increasing pool/queue counts still refuses valid bursts; removing
byte ownership hides actual memory use; a fresh thread for every sequential
call discards hot allocator state. Ready handoff plus first-job ownership avoids
those tradeoffs. Native queue/FFI tests preserve shutdown, poison, spurious wakes,
watchdog/late-reply disposal and deterministic unstarted-worker failure.

## Additional anchors

| Stage | Boundary |
| --- | --- |
| `embedded_job_submit` | Handle lookup, ready handoff or first-job claim |
| `embedded_response_deliver` | Worker sends the owned completion through the response channel |

These opt-in counters supplement decode/encode/wait/copy from the response
guide. They are outside sampled dispatch. Spans overlap, and global setup,
warmup and get populations differ from sampled requests; do not subtract their
means to invent an exact CPU residual.

A selected diagnostic retains 251 of 4,011 requests, zero drops and 143,616
bytes. Global means, microseconds: decode 0.89, encode 0.42, submit 1.95,
delivery 2.42, wait 16.15, copy 0.17. The 250 sampled record.get requests have
queue mean 4.11 and dispatch 5.94, zero errors and zero invalid spans. The
preceding baseline has queue 5.45 and dispatch 7.68 in its separate diagnostic.
The aggregate operations row's legacy `outcome: unwound` is a default
aggregate placeholder, not evidence of a panic; `errors` and individual slow
samples carry actual outcomes. Default-off comparisons establish performance.

## Matched caller windows and observer calibration

Problem: lifetime stage means include setup and warmup, while caller samples
often cover only timed reads. Their subtraction invents a residual. Build
features also matter: a System-allocator control does not describe the Linux
preparation profile, which enables `embedded-mimalloc`.

Decision: `benchmarks/tofudb_point_boundary_measure.py` owns a fresh local
authority, checks every logical value and captures metrics around only its
timed requests. Its default 20,000 reads use 128 warm records. Run separate
processes with `TOFU_DB_REQUEST_PROFILE_EVERY` set to 0/1/1/0 and retain all
raw clocks. `--edges` adds delegating Python conversion/FFI wrappers; calibrate
that observer separately. Raw SQLite returns logical values through direct SQL;
native additionally performs semantic admission and returns record metadata.
Neither this fixture nor a profile qualifies cold, concurrent or write paths.

`scripts/tofudb_profile_report.py REPORT --window` requires matching caller,
seen, selected, operation and queue populations, no drops/errors, monotonic
counters and reconciled exclusive ledgers. It excludes lifetime maxima, slow
samples and global edge counters. The producer must exclude concurrent traffic
and preserve process identity; matching counts alone cannot prove provenance.
The remaining wall time includes caller conversion, reply delivery, scheduling
and profiler overhead. It is not CPU or an unprofiled cost estimate.

Alternatives: subtract unrelated means, treat profile-on latency as admission,
or optimize only a nested stage. The retained production-profile control uses
Thin LTO, 63 launch-probed codegen units, incremental compilation and mimalloc.
Point medians remain about 20 us versus raw SQLite 5–8 us. Full sampling adds
roughly 7–9 us in paired controls; matching sampled queue/dispatch means are
about 4.6–5.5/7.3–8.2 us. A private bounded idle-spin prototype reduces means
to 17.2 us while increasing process CPU to 30–31 us/read from 24–25 us/read;
it is rejected. Adverse legs, build controls, exact clocks and source recovery
are retained in `tofudb_matched_point_20261014.json`.
No scheduling change, persistent allocation or performance-completion claim
is admitted by this diagnostic improvement.

## Experiments and defeated alternatives

- Direct caller execution remains rejected: bounded overlay source ownership alone proves neither watchdog nor semantic/reader admission.
  Seven private tests expose a reader-exhaustion counterexample; ordinary reads return `WouldBlock` while capture succeeds. No runtime change or speedup credit: source proof and defeated alternatives.
- A pure echo-job microbenchmark initially rejected this queue: 32 workers and
  8/32 callers lost roughly half/two-thirds of throughput, although the single
  32-worker single-caller case improved. The actual database includes execution and allocation that
  echo omits. Native and SDK tests later contradicted that initial selection
  decision. Retain the echo regression; do not present echo throughput as
  database certification or silently discard a defeated hypothesis.
- Crossbeam channel 0.5.17 removes the receiver wrapper mutex with cloned MPMC
  receivers ([library contract](https://docs.rs/crossbeam-channel/0.5.17/crossbeam_channel/)).
  Echo single-caller roundtrip fell to 2–3 microseconds, but native 8/32-way CPU
  increased and SDK 32-way/large replies regressed. It is not delivered.
  Cloning its sender outside the handle mutex did not fix the echo concurrency
  regression. All versions and recipes are retained as isolated experiments.
- The MPMC compatibility test exposed buffered jobs retaining input reservations
  after the last receiver disappeared. The experiment retires its sender when
  disconnection is classified; this fixes cleanup and preserves the original
  `retryable: false` transport envelope. Its full 1,499-pass/2-ignored result
  is correctness evidence for that rejected source, not performance approval.

## Real database comparisons and limits

The delivered FIFO/LIFO-idle queue was compared in four isolated A/B/B/A
SDK processes, each checking 28,200 exact reads under strict disposable FUSE.
Single-caller means moved from 0.029/0.027 ms to 0.022/0.022; 32-caller
throughput from 58,645/56,058 to 74,497/75,852 reads/s. Process CPU for single
calls fell from 0.1436/0.1341 to 0.1024/0.1022 seconds. After large replies,
RSS was 153,880/153,292 versus 59,596/57,780 KiB, with 130 threads each;
this supports allocator locality for this corpus, not a universal memory bound.

A separate exact-byte C ABI corpus checks 116,000 reads per leg across
warm overlay and reopened/materialized data at 1/8/32 callers, without Python
in timed calls. Overlay single-caller CPU fell from about 0.270 to 0.202–0.206
seconds; some materialized elapsed legs stayed slower/variable despite less CPU.
Maintained three-engine controls also showed variable header/point latency.
Raw SQLite point means remained 0.006–0.008 ms and header-open 0.018–0.019;
sidecar SQLite point means were 0.230–0.239. Universal SQLite dominance remains
unachieved. All means, per-leg tails, RSS and controls are retained in the
`tofudb_worker_delivery_20261002.json`.

## Verification and tooling failures

The queue/FFI boundary ladder covers FIFO/capacity, close/drain, last-receiver
cleanup, spurious wakeups, recent-worker selection, eight producers/eight
consumers delivering 8,000 unique jobs, lock poison, panic, watchdog, parallel
dispatch, receipt replay and payload bounds. The final native source and full
gate results are recorded in the evidence alongside matched release identities.
The selected frozen source passes 1,505 native checks, with two ignored; this
includes the existing cross-generation fault/SIGKILL, encrypted, backup, GC and
durability suites. Debug CLI, release CLI and embedded library source digests
match. Documentation and architecture gates retain the snapshot's ten document
findings and existing lib-module growth; this delivery introduces neither.

Failed attempts are retained and excluded from success:

- A native-source copy omitted the external operation catalog used by a
  `cfg(test)` include. Compilation failed before tests. Copying the exact frozen
  catalog fixes the fixture without modifying native source/assertions.
- One retry selected the experiment worktree's manifest instead of the immutable
  source. The specific Cargo process/descendants were interrupted (exit 130);
  no correctness credit is taken. The final gate uses an absolute manifest.
- The first three-engine run could not bind the SQLite sidecar on the occupied
  host ephemeral range. It exits 1 and remains separate. ABBA reruns all three
  engines in an isolated user/network namespace with loopback enabled; no live
  backend/configuration or host sysctl changes are made.

Next root targets remain the shared read execution/compilation path, submission
and completion serialization, and shared stage counters. Warm lookup alone
does not explain the remaining raw SQLite gap. No failed queue experiment or
positive finite corpus establishes completion of the broader goal.

## Reply-channel allocation and retirement attribution

Problem: reply allocation and receiver disposal were hidden outside sampled
semantic dispatch. Queue/dispatch durations alone could not establish their
contribution to C ABI latency. Decision: `embed_reply::fresh` preserves one
standard MPSC sender/receiver per job and brackets allocation with
`embedded_reply_acquire`. `ScopedReceiver` explicitly drops its receiver inside
`embedded_reply_recycle`; the latter name denotes retirement in this accepted
implementation, not a live reuse cache. Caller buffers, watchdog budgets,
queue/byte admission, worker detach, poison and error classes remain unchanged.
No runtime cache, added worker, handle field or queue element is introduced.
Sender disappearance still produces immediate disconnection; timeout releases
the receiver and late send errors own/drop their result. An unread owned result
is freed at scope exit. These lifecycle/error cases are executable tests.

Two opt-in caller-edge stages are global aggregates outside worker dispatch;
work marks one `embedded_reply_create` and zero `embedded_reply_reuse` per
sampled embedded request. Global edge/queue/dispatch populations differ and
inclusive waits overlap: do not add them as CPU time or infer equal default-off
gains. Fixed counters grow by 256 bytes; each sampled request/sample/operation
aggregate grows by 64 bytes. The scoped receiver has the original receiver's
size; queued jobs and the handle retain their original layouts. There is no
added resident cache or persisted state. Final measurements and observer costs:
`tofudb_reply_reuse_20261003.json`.

Three defeated alternatives remain as private commits and complete paired
reports, rather than silently replacing unfavorable results:

- A capped per-caller standard-channel cache retained a runtime-wide Arc per
  lease. It passed 1,576 tests and reused 99.987% of sampled replies, but large
  eight-caller overlay/tree means regressed 9.8%/18.4%; CPU regressed 8.4%/12.4%.
- Removing that shared Arc and using per-cell busy/closed state still regressed
  long scalar tree reads 8.2% and long array tree reads 5.3%. Its 1,576-pass
  suite, positive array-overlay legs and all short/long adverse legs remain.
- A safe single-job Mutex/park-token mailbox passed 1,578 tests, including
  unrelated wake and poison behavior. Long scalar overlay improved 3.3%,
  while tree slowed 2.0%; long array overlay slowed 2.7%, tree improved 1.3%.
  Several short/32-caller cases and tails also regressed. It is rejected.

Cache reuse ratios, isolated stage savings and fewer allocations do not prove
end-to-end benefit. These results do not defeat the main bottleneck or the
all-metric SQLite objective. The next investigation must distinguish shared
semantic/query preparation, actual scheduler handoff and profiling observer
cost; a sampled queue total alone cannot establish an exact causal residual.

The initial-context anchors-only control has 1,571 native passes and two existing
ignores, with matched debug/CLI/DSO identities. Sampled global allocation and
receiver retirement means are 0.374/0.578 microseconds; dispatch/queue means
are 11.081/58.188 microseconds across mixed caller populations. Sampled request
work proves one creation and zero reuse. Active/retained profile bytes are
6,744/176,800 in this collector population. These are diagnostic observations,
not an exact subtractive latency budget or equal default-off savings.
Even this control shows large mixed short-leg swings (SDK single +18.2%,
native materialized single -11.4%) and different long-leg directions. It has
no cache algorithm change, so those percentages are not optimization credit.
They expose an important limit of short shared-host comparisons; all raw,
long/short, CPU, tail and observer results remain. Prototype regressions are
conservative rejection evidence, not proof that one Arc alone caused them.

Prior-context and integrated-context identities, failed cache/source checks,
full gates and all adverse controls remain in the reply evidence. A successful
test exit without the expected source identity receives no certification.

## Admission anchors and six rejected wake experiments

Problem: the handle mutex covered queue admission and the OS wake. Removing
that mutex improved eight-caller scalar latency but exposed large high-load
CPU costs. Neither fewer locks nor shorter sampled submit spans establish a
complete performance benefit. Queue lock ownership and notifications therefore
need separate boundaries, alongside caller occupancy that includes decoding,
submission waits, response waits and buffer copying.

Decision: ship four opt-in anchors covering the handle lock, queue lock,
admission work and existing OS notification. Preserve the original handle
mutex, FIFO pending deque, recently idle worker selection, byte/queue bounds,
standard fresh reply channels and notification under the handle mutex.
No live-caller atomic word, deferred notification, assigned-job slot or new
worker registration protocol is shipped. These anchors locate costs; they
are not credited as an algorithmic speedup.

The sixth rejected candidate used an owned Notification and borrowed ActiveCall
counter to defer notification at or below half the launch-probed logical CPU
capacity, while retaining serial notification at higher pressure. Its two
notification tests, two counter tests, panic/watchdog counter assertions and
1576-test full gate passed. Correctness did not defeat its performance regressions.
The earlier worker-occupancy policy omitted callers waiting for submission or
response. Even the live-caller policy only estimated pipeline pressure; it did
not count exactly runnable threads. All candidate code and receipts remain in
private commits, outside the delivered implementation.

The first five defeated prototypes remain as private commits and complete paired raw
reports in the evidence. Full native suites are not claimed for these rejected
prototypes; their embedded boundary checks passed:

- Remove the outer lock: long scalar overlay/tree latency improved
  24.63%/26.90%, but short scalar 32-caller CPU rose 390.55%/400.83%.
- Track receiver registration to skip normal idle scans: long scalar latency
  improved 31.92%/34.73%; 32-caller CPU still rose 314.22%/300.67%.
- Reserve the oldest job for its selected worker: long array overlay latency
  regressed 18.83%; high-load CPU remained roughly four times baseline.
  Its queue-slot disposal, capacity, reuse and three-platform core typechecks
  passed; the extra slot table and mutable-receiver protocol are not shipped.
- Unconditionally defer notifications: long scalar latency improved
  15.46%/21.01%; 32-caller CPU rose about 26%–34%. RUSAGE_SELF independently
  corroborates process CPU clock totals in all six native cases.
- Gate deferral on occupied workers: long scalar latency improved about 29%,
  but 32-caller CPU rose 25%–37%; no serial-notify stage was exercised in the
  diagnostic corpus. The sixth candidate counted live callers instead, and was also rejected.

Preliminary sixth-candidate evidence has long scalar overlay/tree latency
reductions 23.16%/24.60% and CPU reductions 3.92%/4.08%; long array reductions
are 5.13%/2.62% and 4.51%/2.18%. SDK parallel-eight/32 means improve
10.90%/9.64%. Adverse short scalar single-overlay latency (+5.22%), scalar
32-caller CPU (+3.25%/+5.12%) and array 32-caller CPU (about +9%) remain
explicit. These preliminary results did not satisfy the final acceptance criteria. Profiling-on queue/submit means can reverse
relative to default-off timing because these new caller probes update shared
counters under locks; they cannot be used as equal production gain claims.

Delivered anchors are `embedded_handle_lock`, `embedded_queue_lock`,
`embedded_queue_admit` and `embedded_queue_notify`. The last span surrounds
only the original OS wake, still under the handle mutex. Edge probes are global
aggregates outside sampled dispatch; populations differ and inclusive waits
overlap. Four slots add 512 fixed counter bytes and 96 bytes per sampled
request/sample/operation aggregate. Jobs and handles retain their original
layouts; worker count and persistent state remain unchanged. Observer costs,
every CPU/tail leg and rejection are in `tofudb_admission_lock_20261003.json`.

CPU-stack captures with insufficient worker coverage or unresolved symbols
receive no attribution credit. Missing fixtures, listener setup failures,
partial verification and stale build identities remain in the admission
artifact; no failed or superseded-context run receives success credit.

Rejected sixth-candidate matched-context results (positive percentages mean reductions):

| Corpus | Storage / callers | Latency reduction | Process CPU reduction |
| --- | --- | ---: | ---: |
| Long scalar | materialized / 8 | 19.19% | -0.51% |
| Long scalar | overlay / 8 | 17.01% | -1.69% |
| Long array | materialized / 8 | -0.13% | 0.89% |
| Long array | overlay / 8 | -1.23% | -1.15% |
| Short scalar | materialized / 1 | 2.78% | 3.23% |
| Short scalar | materialized / 32 | -4.55% | -5.04% |
| Short scalar | materialized / 8 | 24.97% | 4.68% |
| Short scalar | overlay / 1 | 3.19% | 3.89% |
| Short scalar | overlay / 32 | 7.48% | 0.63% |
| Short scalar | overlay / 8 | 34.45% | 9.77% |
| Short array | materialized / 1 | -1.73% | -2.14% |
| Short array | materialized / 32 | 9.35% | -19.02% |
| Short array | materialized / 8 | 0.78% | 0.06% |
| Short array | overlay / 1 | 0.07% | -0.32% |
| Short array | overlay / 32 | 4.11% | -10.94% |
| Short array | overlay / 8 | -1.02% | -0.04% |

SDK mean-latency reductions: single +0.00%, parallel-8 -1.06%, parallel-32 -1.90%, text-256k +0.27%, array-4096 -0.24%, escaped-text -0.84%, wide-map-4096 +0.16%, escaped-keys -0.49%, float-text-256k +0.27%, numeric-boundary +0.00%.
The rejected candidate's corrected native gate passes 1576 tests with 2 existing ignores, with debug CLI, Release CLI and DSO source identities checked. All eight measurement drivers pass. Documentation and architecture checks retain identical matched baseline failures, with no added finding. These finite cases compare the previous TofuDB implementation; they do not prove universal SQLite dominance.

Final wake-strategy acceptance decision: reject the caller-aware implementation.
Matched long scalar latency improves 17.01%/19.19%, but its CPU rises
1.69%/0.51%; long arrays do not improve consistently. SDK parallel-eight/32
regress 1.06%/1.90%, and short array 32-caller CPU rises 10.94%/19.02%.
The preliminary selection is revoked. Preserve the six experiments, but ship
only admission/wake diagnostic anchors with the original scheduling policy.

Final integration preserves concurrent streamed-checkpoint admission and the
maintained vectored frame writer, including its original partial-frame bound
and failure/secret-cleanup behavior. Earlier fixture modifications are not
shipped. The admission evidence separates prior-context and final combined
builds, observer perturbation, allocation growth and adverse default-off legs.

Prior caller-anchor-only controls passed 1,577 tests with two existing ignores.
Their full short/long scalar/array and SDK table remains in the
`tofudb_admission_lock_20261003.json`.
Long-array overlay latency/CPU regressed 10.39%/9.46%, and materialized scalar
single-caller latency regressed 27.00%. They establish instrumentation controls,
not algorithmic speedup; all other positive/adverse legs stay in the evidence.

## Current handoff diagnostics and rejected single-slot channel

Problem: queue/dispatch totals hide worker queue locks and post-execution
handoff. Six additional opt-in global anchors measure worker queue lock,
queue claim, idle registration, execution, enqueue-to-worker wait, and
result-ready-to-caller handoff. They are outside sampled semantic dispatch;
queue delay retains its existing sampled field. Handoff includes send and
scheduling, not isolated scheduler CPU. Populations differ and waits overlap:
do not sum or subtract their means into a disjoint CPU budget.

Decision: retain the original fresh standard reply channel. The new result
stamp lives in a boxed diagnostic variant allocated only when profiling is
enabled; default-off replies keep the original 32-byte packet and allocation
policy. Neither an indexed idle registry nor a single-slot reply channel ships.
Sender disappearance, receiver timeout/drop, late owned results, watchdog,
panic/poison, byte bounds, FIFO jobs and idle-worker policy remain unchanged.
No caller buffer crosses the worker boundary; no direct execution is added.

The single-slot prototype consumed its sender with nonblocking try_send and
passed 1,581 checks with two existing ignores against its own frozen source.
An isolated Rust 1.98.1 System-allocator probe counts requested bytes, not RSS:
original reply/channel 1,760 bytes; timestamped unbounded 2,256; timestamped
single-slot 696. Each used two allocation calls and 20,000 exact replies.
Despite fewer bytes, eight native long legs regressed materialized latency/CPU
7.10%/3.46% and overlay 1.50%/0.60%. Independent SDK controls also regressed
text-256k CPU 9.46%, float-text 6.23% and scalar/parallel shapes 2.05–3.59%;
positive array/escaped-text legs and all three-engine/tail results remain.
Therefore allocation reduction alone does not justify adoption. Compiler,
channel and scheduling explanations remain hypotheses, not isolated causes.

An idle-thread index saves about 30 ns/claim at 128 idle workers in eight
paired queue-only controls, but regresses one/eight-worker cases 11.0%/5.3%
and needs another worker-sized index. Its microsecond relevance is insufficient;
no indexed queue is delivered. These microprobes do not certify database speed.

Six fixed stage pairs across global/seven component counters add 768 bytes;
sampled request/sample/operation records add 144 bytes. Final opt-in reply
stamps add one bounded 48-byte allocation per in-flight reply, retired with
that reply. Off adds no diagnostic heap allocation or clock read. Existing
resource-derived workers, queue/byte limits and profile retention bounds stay.
No persistent state grows. The preceding prototype observer reports measure
7,160 active bytes and 190,944 retained bytes for their collector population;
final-build memory/profile controls are separately identified in the evidence.

All failed and positive legs, observer off/16/16/off, exact native/SDK values,
strict disposable authorities, source-tagged gates and final integration:
`tofudb_handoff_20261004.json`.
The 1,581-pass full gate belongs to the rejected prototype; final diagnostics
preserve later concurrent automation changes with a separate build/check identity.
Atomic one-shot replies also regress arrays: `tofudb_atomic_reply_20261004.json`. Finite measurements do not prove the universal SQLite objective.

## Independent embedded parser admission budget

Problem: embedded FFI reused the socket serialized-frame budget for parser
reservations, although each embedded input is charged eight times its byte
length. A 128 MiB serialized-frame ceiling therefore refused a 1000-conversation,
64 KiB stream workload on a host with over 128 GiB observed memory headroom.
This overload happened before transaction dispatch; it is distinct from loss of
an acknowledged write and from the original 8 KiB latency acceptance gate.

Decision: separate embedded parser admission from socket frame
admission. Its allowance is observed launch memory headroom divided by 32,
floored at the existing frame allowance; missing observations preserve the lean
allowance. No RAM is preallocated. Queued/running request reservations retain
8x input charging, overflow rejection, cancellation lifetime and worker-release
accounting. The same immutable launch budget exposes
`embeddedRequestMemoryBytes` in aggregate resource metrics. Socket framing,
per-request envelope bounds and active job limits retain their contracts.

Alternatives considered: shrinking the client pool reduces admitted parallelism
and previously harmed settlement latency; reducing the parser charge weakens
its safety allowance; retrying overload in the benchmark masks admission errors;
increasing the socket frame cap changes an unrelated protocol boundary.
Measurements: the 64 KiB, 1000-conversation workload completes all 4000 events
with zero errors; the final native pair peaks at about 4.13 GiB process RSS.
The 8 KiB final run peaks at about 2.08 GiB. These are shared embedded-process
observations, not separate engine/client allocations or a universal memory bound.
Two original-workload ABBA/BAAB groups show no observed material regression.
Both original and larger workloads still miss latency acceptance. Real SIGKILL
after all 1000 acknowledged completions restores exact content and event replay;
this does not cover kill during unacknowledged writes. Detailed sources, gates
and limitations: `tofudb_rebase_attribution_20261006.json`.
The one-shot budget adds no worker, idle allocation or persistent state.

The actual FFI burst test rendezvous holds 64 calls inside native dispatch with
idle reuse target 1. All completed and the handle closed in 1,008.28 ms including
local/FUSE shutdown; this proves acceptance and ownership, not universal query
latency. The one-worker-ready handoff and surplus-carrier tests separately
verify close wakeups and cache-only retirement. Input bytes retain their lease
through actual worker release, including failed publication and watchdog exits.
<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

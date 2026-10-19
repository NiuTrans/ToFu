# Snapshot-certified query result reuse

Authority: [TofuDB storage](STORAGE_TOFUDB.md#snapshot-read-lane).
Owners: `query_result_cache.rs` retains owned results;
`transaction_ir.rs` and `semantic_executor.rs` certify eligibility;
`authority_query_view.rs` proves live snapshot health;
`server.rs` and `embed.rs` retain request admission and delivery.

## Problem and decision

Warm point reads spent substantially more time in cross-thread delivery and
repeated semantic execution than in storage lookup. Improving the page cache
alone cannot remove those fixed costs. The common semantic executor can prove
that a completed result is invariant within an exact entity snapshot. Reuse
that proof, then perform fresh request admission and live snapshot/health checks.

The deterministic `compile_records` branch for `record.get` binds its key only
from request scope, namespace and key. Its IR must contain exactly one
`VersionedDocumentGet`. A `turn.sync.snapshot` can qualify only after its actual
`TurnSyncSnapshot` execution completes without observing deadline exhaustion.
Both require a matching `SlotOrLiteral` result with literal `null`, Query kind
and no command effects. Additional steps, another selected slot, raw/literal
results or an operation name alone grant no certificate. The response also needs
immutable canonical JSON proof; only exact `null` gains an absence scalar proof.

Snapshot header, execution epoch, Turn projections, Attempts, phase head and
sync sequence all come from that transaction's entity root. Window length and
projection-byte limits remain deterministic request inputs. A deadline-shortened
window is request-dependent and is ineligible even if it is valid resumable JSON.
Completion evidence follows the actual execution into final result certification;
the compiler cannot mint it from the requested operation or response shape.
New operations or IR shapes default to ineligible, including task-result special
reads, leases, timers, search and
plugin operations. Commands and requests carrying command IDs never reuse it.

Keys compare owner, tenant, schema, operation and exact payload bytes after
hash lookup. Hash collisions can evict or miss; they cannot return another key.
Each entry records the entity sequence and root. A live publication read guard
proves the same current snapshot; authority poison, source restart, registry
poison, WAL error or any contended/poisoned optional lock declines reuse.
Publication follows the existing successful-write boundary. A read overlapping
a write may linearize before publication; a read after its successful ACK
cannot consume an earlier snapshot. Aborted publication disables the mirror.
Cached results own their bytes and need no physical root or GC pin. A result
already acquired by a reader remains immutable through writes and eviction.

The shared dispatcher reuses the proof after normal admission. The C ABI also
checks the live job sender and runtime before the same session admission, then
copies/encodes the completed owned bytes under its existing panic containment.
This branch performs no storage/IR execution, I/O, blocking lock, worker wait,
or receipt replay. Uncertainty immediately returns to the original worker and
watchdog path. Existing handle poison takes precedence even over a warm entry.
Correlation IDs and error envelopes retain their transport ownership. The C ABI
uses a private nonzero correlation value because its JSON envelope exposes no
correlation ID; wire replies preserve the caller's actual ID.

## Capacity, lifecycle and observation

Capacity is the launch-probed block-cache budget divided by 64, with the same
live configuration owner as the canonical proof cache. No capacity-sized buffer
is allocated on open. Each entry charges owned key/result bytes plus 512 bytes
for metadata, Arc and tree overhead; admission requires no more than 1/16 of the
cache budget and one protocol frame for each input/result. These are cache
eligibility bounds: larger requests retain their ordinary execution path.
A lazily allocated four-position Bloom doorkeeper defers a result unless its
fingerprint was observed. Record results retain repeated-key admission, allowing
an already-hot key to refresh after a write. Complete snapshot results carry
repeated-snapshot admission: the fingerprint includes request identity, entity
sequence and root. An older cached generation cannot bypass that probation.
This avoids repeatedly copying a snapshot queried just once after each write.
Admission controls copying only; both policies require the same exact current
snapshot and health proof before serving any result. False
positives permit extra admissions, never answers: exact key/snapshot/proof
checks remain mandatory. The bitmap consumes 1/128..1/64 of the cache budget;
its actual vector capacity is charged, and allocation uncertainty declines
reuse. It ages after twice the maximum possible result-entry count, so a
working set that fits in the result budget can receive two observations before
aging. An exact short FIFO was rejected: it indefinitely excluded fitting
working sets and its HashSet capacity could exceed the reserved estimate.
This policy reduces result copying and displacement during one-pass scans.
CLOCK insertion examines at most eight victims and otherwise declines caching.
Zero capacity disables reuse; shrink evicts owned entries. The facade/authority
owns the cache until its final owner drops. A fault-disarmed worker retains its
runtime until worker/process termination, following the existing watchdog
lifetime. No thread, durable file or recovery state is added. Evicted results already held by active requests remain owned
until those requests finish; they are not counted as cache-resident bytes.

`system.metrics.publishedReadView.queryResults` exposes capacity, accounted
bytes, result entries, bitmap bytes/observations/window, admission deferrals,
evictions and lookup hits/misses. A lookup hit is an exact-key
candidate, not proof of a served cache hit: generation/health/admission can
still reject it. Contention can omit an observation. No payload is logged.
Foreground admission activity remains visible to existing background pacing.
Sampled inline completions have queue duration zero and the ordinary request
ledger; lookup/admission precedes named spans and remains unattributed wall time.
Use matched caller windows, not subtraction of independent lifetime averages.

## Alternatives and verification

Direct engine execution on a caller would broaden the failure/watchdog boundary;
this design reuses only completed certified results. An operation allowlist
cannot prove time independence. Cached replies without generation or health
checks can hide writes or failures. A permanent root pin retains storage for
unused entries. Unbounded memoization accumulates old generations. All are
rejected. Any eligibility expansion must prove its IR dependencies first.

Native specifications cover exact identity/hash collision, capacity/eviction,
old-result ownership, acknowledged concurrent writes, deletes and absence,
ineligible IR, exact scalar proof, lifecycle contention, WAL/restart failures,
publication/configuration poison, short C ABI buffers, panic and watchdog poison.
The full native suite remains mandatory. Performance qualification retains
fresh-process A/B/B/A production builds, exact-value point reads, profiling
calibration, cold/mixed/concurrent neighbors and observed resident memory.

The shutdown-checkpoint fault fixture uses fixed test segment IDs for both
trace discovery and replay. Repeated probes must agree, every selected fault
must fire, and the operation prefix must match. Recovery, acknowledged-value
and forward-progress assertions remain; randomized retries cannot substitute
for exercising a fault.

## Qualification and limits

`tofudb_query_result_cache_20261015.json` identifies
the native inputs, production artifacts, Python harness, full native suite,
all measured arms and rejected prototypes. The raw archive retains latency
samples and failed attempts; the full source reconstruction stays in the
local pre-integration evidence backup to avoid committing concurrent edits.

On the recorded host, two 50,000-read arms averaged 20.49 us before and
7.51 us after this change (63.4% lower); direct raw SQLite averaged 5.33 us.
The matched process CPU cost fell from 23.45 to 7.98 us/read, including Python
validation and measurement overhead. These are warm exact-key reads, not
general query or application throughput. Full request profiling added
approximately 1.30 us/read; profile-on runs are diagnostic only.

For 4,096 keys of about 2 KiB, default-budget repeated reads averaged
22.78 to 13.86 us, while first-use reads rose from 23.88 to 24.54 us.
After repeated reads, cache accounting was 13,207,380 bytes including its
2 MiB Bloom buffer. Whole-process RSS was about 139 MiB versus 133–134 MiB;
RSS includes the engine, Python, allocator retention and collected samples.
The explicit 32 MiB block-cache configuration gives this cache 512 KiB.
That no-fit working set retained only two results and 4 KiB of Bloom bits,
instead of evicting on every read, but repeated reads still averaged
23.16 to 23.95 us. These regressions remain part of the qualification.

The nine neighboring workloads do not establish universal improvement.
The 1,000-conversation delta/phase pipelines retain correctness, restart replay
and integrity verification, but fail their scheduled-stream and settlement
latency gates. This cache does not remove the write/settlement queue bottleneck.
The full suite reports 1,994 passed and 9 ignored on the frozen source.
Concurrent unrelated source advances are recorded separately, never silently
included in that verification identity. No running application is replaced by
the benchmark artifacts.

## Complete snapshot qualification

Problem: repeated bounded history reads rematerialized and reencoded the same
public response even when its entity root had not changed. Decision: extend the
existing result certificate only to completed `TurnSyncSnapshot` execution, and
carry its repeated-snapshot admission policy through immutable dispatch. No new
cache, worker, durable format or resource budget is introduced.

The first prototype reused repeated-key admission. In a 512-write/read control
it copied and replaced the snapshot 512 times without reusable generations.
The selected policy observed zero replacements in that control. Bloom false
positives can still permit occasional copies; they never establish a hit.
The retained default-cache footprint was about 2.02 MiB, including the existing
2 MiB probation bitmap and one approximately 16 KiB result. Over-capacity
controls stayed within the existing 512 KiB cache budget.

`tofudb_snapshot_reuse_20261015.json` retains
both prototypes, artifacts, exact-result checks and adverse arms. Two fresh
windows per native build/cache configuration measured roughly 160–163 to
39–40 us for a 20-Turn warm snapshot, about 75–76% lower. Direct SQLite's row
floor remained about 28–29 us. Native still returns additional public metadata.
Longer point controls were approximately unchanged. Fixed-namespace 30,000-op
mixed controls retained a default-cache mean regression of 2.95%, versus a
1.39% reduction with the 32 MiB block-cache override. Several shorter neighboring
writer/sidebar tails also regress; these are not a universal performance win.

The exact final native source passed 2,010 tests with nine existing ignores.
Four actual 1,000-conversation application/embedded phase/delta scenarios passed
content, SIGKILL replay and integrity checks while failing their latency SLOs.
Neither the warm-read gain nor these functional checks complete the overall
raw-SQLite performance objective.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->


## Rejected embedded cache-copy candidate (2026-10-19)

Problem: the C ABI copied a request payload into an owned worker-input buffer
before attempting inline reuse. A cache hit then cloned its complete immutable
response into a temporary `IrResponse`, before copying it again into the encoded
envelope. Neither temporary allocation had an asynchronous consumer.

Decision: reject the combined candidate. It borrowed the already-validated
caller payload during synchronous lookup and deferred the outer worker-input
copy/lease until fallback; it also borrowed the certified immutable response
while constructing the owned envelope. Session admission still made its own
request payload copy. It was not a zero-copy or zero-allocation path. No cache
capacity, thread, queue, persistent format or durable growth was changed.

Eight independent processes, ordered baseline/candidate/candidate/baseline/
candidate/baseline/baseline/candidate, completed 312,096 exact timed readbacks
with zero retries. Both builds used schema 68, embedded mimalloc, Thin LTO and
16 codegen units. Base commit was `ac1df53948c486b18e1d3645b44b42927282f937`;
baseline/candidate source fingerprints begin `7d2a42ad`/`821ab41f` respectively.
The complete profiling-off embedded SDK clock includes FFI, envelope handling
and Python decode. No changed diagnostic span is used as performance evidence.

| Workload | Baseline P50 (us) | Candidate P50 (us) | Baseline P99 (us) | Candidate P99 (us) |
|---|---:|---:|---:|---:|
| First-use point | 22.387 | 23.929 | 55.911 | 60.560 |
| Warm point | 7.376 | 7.263 | 14.001 | 13.499 |
| Warm 64 KiB text | 100.722 | 98.705 | 120.676 | 126.619 |
| Warm 512 KiB text | 798.027 | 771.791 | 1087.831 | 1101.410 |
| Eight callers | 133.333 | 9.711 | 500.215 | 1042.147 |

The candidate concurrent mean improved from 125.512 to 83.654 us and process
CPU/read from 21.595 to 17.225 us, but its P99 worsened in all four legs:
1375/1204/1095/620 us versus baseline 428/524/537/495 us. Candidate per-leg P50
ranged from 9.140 to 85.420 us, so the pooled P50 must not be advertised alone.
First-use P50 also worsened by 6.9%. The smaller single-caller warm gains do not
justify accepting a repeatable tail regression for an Agent Harness. The
scheduling mechanism causing this tradeoff has not been established.

After authority close all eight processes had one thread and RSS between
52,312 and 52,672 KiB. This short lifecycle observation does not establish
long-running memory behavior. Raw samples, full fingerprints, manifests,
candidate patch, frozen builds and all validation logs are retained under
`data/benchmarks/tofudb-native-read-cost-20261019/`; `summary.json` contains
per-leg and pooled statistics, and `analysis.md` records the failed checks too.
The unchanged candidate passed 22 focused native tests, 29 Python oracle and
lifecycle tests, and a quiet full native gate (2007 passed, nine ignored).
An earlier full run during compilation failed one existing immediate-warm-hit
assertion before fault injection; its log and successful unchanged reruns are
retained. Passing correctness checks does not waive the latency rejection.

The retained `benchmarks/tofudb_cached_read.py` uses disposable strict
authorities. First-use has 512 distinct reads per leg without result-cache
warmup; warm point has 20,000 reads of 64 keys after three complete warmup
passes. The larger bodies contain escaping, Unicode and nested values; 64/512
KiB describes nominal decoded text size rather than total wire response size.
The oracle combines independently constructed values with the exact committed
write receipt; every response field is checked outside request timing. Process
CPU includes validation. This is a native before/after comparison, not a
physical-engine, raw-SQLite or Agent-completion benchmark.

Alternatives: splitting request borrowing and response borrowing into separate
candidates would identify which mechanism changes the latency distribution;
this remains future work. Direct caller execution would widen watchdog
coverage, borrowing caller buffers across queued jobs would violate lifetime
safety, and inserting raw response bytes without canonical evidence would
weaken validation. None of these is an acceptable shortcut to a smaller number.

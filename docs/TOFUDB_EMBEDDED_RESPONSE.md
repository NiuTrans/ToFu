# Embedded response ownership and performance

Owner: `packages/tofu-db/src/{embed,embed_ffi,fsync_stats}.rs`,
`server.rs` and its private `dispatch_response.rs` child.
Semantic and durability authority: [TofuDB storage](STORAGE_TOFUDB.md).
Measurements: `tofudb_encoded_response_20261002.json`. Python client buffer decoding and ownership measurements: `tofudb_client_response_view_20261005.json`.

## Problem

The shared dispatcher already returns encoded JSON in `Response.payload`.
The old embedded transport parsed that payload into another `Value` tree,
passed the tree through the worker response channel, then serialized the
whole envelope on the caller. Large read replies therefore repeated parsing,
allocation and string escaping after query execution was already complete.

The preceding point diagnostic averages 42 microseconds end to end, with
sampled native queue 5.87 microseconds and dispatch 11.23 microseconds. Its
inclusive authority hold is only 0.89 microseconds. Native and client sample
populations differ; these numbers do not establish an exact subtractive
residual or identify all of the gap to raw SQLite.

## Decision and invariants

The C ABI dispatches through the same runtime, scope, stores, admission and
semantic compiler, then sends an owned encoded response through the existing
worker channel. The Rust `Value` API retains its original response renderer.
Cache misses retain worker validation/encoding, `catch_unwind` and the
watchdog; [certified result reuse](TOFUDB_QUERY_RESULT_CACHE.md) performs bounded in-memory delivery on the caller. A late worker completion owns only its channel/vector; it cannot
write into a caller buffer. Panic/timeout still poisons and disarms the handle.
Queue/memory saturation, deadlines, payload caps and classified errors remain.

For payloads without current immutable canonical evidence, a serde visitor
validates syntax, UTF-8, numbers and recursion with the same decoder rules as
`Value`, without building another object tree. Normal
JSON strings, integers, arrays, objects and binary64 floating tokens remain
encoded. SIMD string skipping checks float-bearing payloads for integer tokens
outside the i64/u64 range and integer negative zero: those cases retain the
old `Value` normalization, including float conversion. Malformed, oversized
numeric or over-depth successful payloads preserve the original null policy;
nonzero-status replies also have null payload. Metadata escaping uses serde.
Object field order is not a JSON semantic guarantee. Duplicate-key results
retain the client's last-key-wins interpretation. Semantic producers still own
response construction; this does not permit arbitrary unvalidated splicing.

No new thread, queue, cache or persistent file is introduced. The response
vector lives until caller consumption or late-result channel disposal. Serde
scratch storage is transient. The four new fixed stage slots occupy 512 bytes
across the global/seven component counter arrays. Existing request/response
and queue byte bounds apply; no caller pointer crosses into the worker.

## Diagnostic anchors

| Stage | Boundary |
| --- | --- |
| `embedded_envelope_decode` | FFI envelope parse and owned job-field preparation |
| `embedded_wait_response` | Caller waits for queue, dispatch, encoding and response delivery |
| `embedded_response_encode` | Worker validates payload and constructs encoded envelope |
| `embedded_response_copy` | Caller copies completed bytes into its output buffer |

These opt-in spans are global stage counters outside the sampled dispatch
request guard. The wait encloses work measured on the worker; do not add their
inclusive durations or treat them as CPU time. Default-off comparisons decide
performance; diagnostics explain placement. No payload contents are retained.

## Alternatives and failures

- Removing the worker hop would remove the existing watchdog isolation and
  permit a wedged call/late completion to outlive caller-owned memory. This
  optimization keeps the worker contract; it is not claimed to eliminate queue
  or Python client overhead.
- Unvalidated raw payload insertion could broaden malformed-number/UTF-8/depth
  behavior. The serde validator and numeric fallback preserve those boundaries.
- The first compatible implementation sent every float-bearing reply through
  the old tree renderer. It passed boundary tests but was not performance
  benchmarked. The final scanner limits fallback to integer normalization
  cases, so an ordinary float beside a large text field does not force a tree.
- The first depth fixture decoded the complete envelope with the payload's
  recursion allowance, counting an extra envelope level and failing a case
  that the original encoded envelope also could not decode that way. The
  corrected oracle separately decodes envelope fields and payload, then checks
  against the original renderer. The failed log is retained and excluded from
  correctness/performance claims.

## Verification and remaining scope

The boundary suite covers panic, watchdog, concurrent calls, queued/running
memory accounting, envelope/payload caps, canonical receipt replay and invalid
request types. Renderer comparison covers metadata escaping, duplicate keys,
Unicode, malformed UTF-8, float/large integer/negative-zero conversion,
overflow, recursion boundaries and a large escaped response.

The frozen candidate passes 1,494 native checks; two are ignored. Release CLI,
embedded library and debug test CLI identify the same native source. Existing
cross-generation fault/SIGKILL, backup, GC, encrypted and durability tests pass.
All checks are finite; they do not prove the all-scenario SQLite objective.

Integration preserves a concurrent listener port-recovery change. Its combined
source and test result are recorded separately from the frozen response
candidate; the performance figures below belong to the measured candidate.
One integration build attempt used the host GCC 4.8 and failed on an unsupported
compiler flag before tests ran. The retry uses the same devtoolset-8 compiler
and isolated loopback network as the preceding native verification; neither
application code nor test assertions were changed to bypass that failure.

Default-off ABBA mean latency (milliseconds):

| Corpus | A1 | B1 | B2 | A2 |
| --- | ---: | ---: | ---: | ---: |
| Small direct SDK reply | 0.030 | 0.027 | 0.027 | 0.029 |
| 256 KiB text | 0.967 | 0.618 | 0.685 | 0.762 |
| 256 KiB text plus float | 0.746 | 0.599 | 0.597 | 0.761 |
| Escaped/Unicode text | 6.691 | 5.476 | 5.512 | 6.621 |

Each leg verifies 4,600 exact timed readbacks, plus warmups. Maintained harness
ABBA point means are 0.043 / 0.040 / 0.038 / 0.055 ms; header-open means
0.085 / 0.052 / 0.061 / 0.075 ms. Raw SQLite point mean stays 0.006 ms,
so this change does not close that gap. All four semantic helper legs exit zero.

A sampled direct-SDK run selects 251 of 4,011 requests with zero drops. Global
edge means: envelope decode 1.01 microseconds, encoded response 0.43,
response wait 20.29, response copy 0.13. Sampled record.get queue mean is
6.13 microseconds and dispatch 7.76; client mean is 28 microseconds. Setup,
warmup, global and sampled populations differ; do not subtract them to claim
an exact missing-cost value. Reply encoding is now a small measured part;
thread delivery and client conversion remain explicit next targets.
Subsequent queue ownership, worker reuse and paired evidence: [worker delivery](TOFUDB_WORKER_DELIVERY.md).

The complete paired results and separate diagnostic run are in the evidence. Corpus includes small, large plain text, escaped text and large text
with a float. Exact values are checked outside timed samples. These are warm,
freshly written record reads, not cold/materialized storage or concurrency-tail
certification. The same Python client and durability policy are used in each
leg. Remaining point cost still needs client/FFI/thread/dispatch attribution;
raw SQLite performs a different direct-call boundary. The full performance
objective remains active.

## Immutable canonical dispatch results

**Problem.** Projection/IR already proves canonical Value serialization and
acceptable complete payload depth. Converting its actual selected `IrResponse`
to a public mutable `Response.payload: Vec<u8>` loses that evidence; the
embedded encoder then parses every payload again. Global transport stages lie
outside sampled dispatch, so dispatch-only attribution cannot establish their
cost. A separately pinned anchors-only build and final build measure this seam.

**Decision.** Private `server::response::DispatchedResponse` consumes the actual
selected immutable `IrResponse`. Only that producer's canonical/depth evidence
can create its private certificate. The payload and metadata remain private,
with read-only access; public `Response` extraction consumes the capsule and
abandons its certificate before exposing a mutable Vec. Rewrapping public
bytes, even equal bytes, is unverified. Public Rust/semantic/framed Response
interfaces stay the same; framed chunking receives the original public type.

The embedded worker may append certified payload bytes directly to the existing
serde metadata envelope. Canonical Value serialization already resolves wide
integer and integer negative-zero normalization, reserved serde marker behavior,
UTF-8, and depth. A proof of generic valid JSON alone is insufficient. There is
no operation-name allowlist, persisted proof, digest lookup or thread-local
adjacency assumption. The certificate accompanies its exact owned bytes from
the producer through replica/command result selection and WAL witness waiting.

Control/search/offline SQL/diagnostic bytes, literals/missing values, receipt
replay and legacy projection fallback retain the original parser/numeric
compatibility path. Adding a commit-notice envelope deliberately abandons
previous evidence: another container changes recursion allowance. Notice
serialization and generic/desktop semantic validation stay unchanged. The
Rust Value API still renders through its original parser. No response mutation,
metadata update or caller-owned buffer crosses the certified private boundary.

**Alternatives considered.** Adding a boolean to public mutable `Response`
allows bytes and evidence to diverge. Looking up by operation accepts unproven
literals, legacy/replay responses and missing-slot fallbacks. A global payload
hash cache adds an entire pass and memory lifecycle for a proof already known
by the producer. Re-recognizing canonical bytes at the transport still repeats
parsing. Blanket grammar-only reuse misses normalization of wide integers and
integer negative zero. The private consuming handoff avoids these defects.

**Anchors.** Five opt-in stages separate payload validation, numeric
compatibility scanning, legacy Value fallback, metadata/payload envelope
construction and canonical evidence reuse. The latter encloses envelope
construction on the certified path; inclusive spans must not be added. These
are global counters outside sampled dispatch, and default-off clocks stay off.
No new persistent bytes, caches, workers or per-body copies are introduced.
The certificate adds one optional zero-sized witness plus alignment to each
already bounded private response, and existing worker/caller lifecycles own it.

The first full gate stopped with 1,506 unit passes, one rotation fixture failure
and two ignores. Its same executable focused replay passed. Source inspection
identifies an unconditional Ready expectation racing the pack worker's empty
retirement cleanup lock: online rotation intentionally returns Busy when its
nonblocking try-lock cannot acquire. The failed invocation did not record the
mutex owner; this attribution is an inference from the guards and worker path.
The fixture now registers the pack lane before prepare and starts the actual
worker afterward. It still asserts Ready, bounded real covered-generation
drain, independent window staging and exact pre/post-crash payload recovery.
No production Busy/retirement code or retry policy changed. Failed gate/source
and build remain retained without final correctness or timing credit.

**Measured final result.** `tofudb_wire_policy_20261003.json`
retains baseline `43cd6870`, anchors-only `caccb920`, final `17bf999d`, pinned
CLI/loaded DSO identities and all cohorts. The final full gate passes 1,545
checks with two existing ignores; 25 profile-report checks pass. The failed
`2e3af134` gate/build is not credited. In the array diagnostic (one put plus
210 exact gets), payload validation calls fall 211→1; 210 gets reuse immutable
canonical transport evidence. Global encoding totals fall 70.145587→0.822316 ms,
and payload validation totals fall 69.360523→0.001126 ms. The envelope totals
are 0.743988→0.770903 ms, with their unfavorable difference retained. These
nested global wall-time spans are outside dispatch and are not additive.
All 210 gets still collect and fully hash 26,201,070 scoped body bytes.

Default-off SDK ABBA averages, 33,200 exact timed readbacks per leg:

| Value | Baseline ms | Candidate ms | Mean latency reduction |
| --- | ---: | ---: | ---: |
| ASCII text, 256 KiB | 0.2860 | 0.2440 | 14.7% |
| Array, 4,096 items | 0.9970 | 0.6630 | 33.5% |
| Escaped text | 2.0835 | 0.7380 | 64.6% |
| Object, 4,096 keys | 1.4585 | 1.0115 | 30.6% |
| Escaped keys | 0.3480 | 0.2270 | 34.8% |
| Text plus float, 256 KiB | 0.2880 | 0.2645 | 8.2% |

Native C ABI arrays improve 44.2–84.5% across overlay/materialized and 1/8/32
callers, with 18,400 exact readbacks per leg, no Python/GIL in timed calls and
an independently parsed complete reference outside timing. One-caller means
are 418.028→69.453 us (overlay), 452.457→69.916 us (materialized). Every numeric
SDK result additionally verifies Python integer/float types and negative-zero
sign outside timing; numeric equivalence alone is not credited.

**Unfavorable results and cost.** Mixed SDK small-point means are 23→22 us,
but eight-caller means are 98.5→115.5 us and 32-caller 462.0→507.0 us. Numeric
small-record means are 23→27 us. Eight additional balanced legs isolate each
case: eight callers 100.25→99.25 us, 32 callers 411.50→420.25 us, numeric records
21.75→22.25 us. These do not erase the mixed-cohort regressions or establish
universal improvement. Native scalar one-caller means remain 17.464→17.475 us
(overlay) and 17.806→17.768 us (materialized); materialized eight-callers regress
43.432→50.297 us (15.8%) and overlay 32-callers 228.090→234.014 us (2.6%).
Isolated header means are 45.75→44.75 us. All individual legs remain retained.

End-of-corpus RSS means are 74,656→75,244 KiB (+588 KiB), with 130 threads in
all four legs. Process RSS includes client/runtime/allocator effects and is
not private-certificate size accounting. Sampled active request bytes grow
6,176→6,296; the same two-label retained collector grows 157,488→161,568.
Both one-in-16 observer runs select 2,108 samples, with no drops; profile
ledger checks retain errors and span reconciliation. Default-off measurements
remain the performance evidence. No added body cache, worker or persistence.

A measurement recipe initially ran conversation isolation before the semantic
corpus profile it consumes. The missing input aborted before timed reads;
that leg has no timing credit. Its failure/log/manifest are retained. Seven
completed recipes were preserved and only remaining dependent cohorts resumed
in the correct order; native source and passed suites were not rerun.

**Remaining roots.** Full scoped content hashing, small-query compilation,
worker delivery and concurrency costs remain. This phase removes the repeated
transport walk; it does not establish all-scenario/all-metric SQLite dominance.


## Borrowed request-envelope fields

Problem: every C ABI execute built a `BTreeMap<String, &RawValue>` for the
entire envelope, allocated owned names for all keys, and kept the map through
the call, although dispatch only consumes operation, payload, command ID and
deadline. This is fixed front-door work even for a tiny point read.

Decision: private `embed_envelope.rs` collects four optional borrowed raw
values with a serde map visitor. The key visitor recognizes decoded names
without retaining key strings; the same RawValue parser validates every value,
including unknown fields. Later duplicates replace earlier raw values. Field
types are decoded only after parsing the complete envelope, preserving last
key wins even when an earlier duplicate has an inadmissible type. Escaped
field names, unknown fields, root rejection, whitespace and malformed trailing
input retain the original parser behavior. The final operation/command strings
and payload Vec still become worker-owned before submission. No borrowed caller
pointer crosses into a worker or survives the C call.

Alternatives: derive a typed struct (normally rejects duplicates and can reject
an early wrong type before a later valid duplicate); collect a borrowed-key
map (still builds nodes and requires special escaped-key handling); skip
unknown values with a weaker validator; or run queries on the caller. The
visitor preserves syntax/type/error boundaries and removes map ownership;
it does not change watchdog, poison, queue, admission or worker policy.

The existing `embedded_envelope_decode` anchor covers the exact same envelope
parse/type-decode/payload-copy boundary in both roles. Counter layouts and
sampled-profile sizes do not change. The four references are local stack state,
not a cache; serde escape scratch remains transient. There are no new workers,
queues, persistent files, resident caches or process-wide allocator settings.
Source identity, correctness, all favorable/adverse controls and the failed
first direct-ABI comparison live in
`tofudb_envelope_20261004.json`.


Frozen native baseline `29add8937ed34ebc825e53e1de7202bc64bc3e481eac0caa141c7eb1b3f88b0e`; candidate `f1c1f4e6a7903085d2133ce736e08e6bc806247127e68ad3488ab5f3f6a870bb`. Debug CLI and release CLI/DSO identities match their roles. Full verification passes 1,580 checks with two existing ignores. Two new parser tests compare raw fields, input borrowing, exact acceptance/errors, duplicates/escaped names, invalid roots/UTF-8/nested input and byte mutations against the old map parser. Untimed real C ABI parity covers 96 requests with equal return lengths and errors. Each full document equals its own seeded reference. The first ABI comparison failed because independently seeded writes have different timestamps: all 11 differences were `updated_at_ms`. The raw failure is retained; the corrected comparison verifies each full reference before comparing that one known independent-clock field. No other response differences are removed.

Standalone System-allocation probe uses the actual frozen helper and locked
serde versions/features. It measures envelope collection only, excluding field
value decoding, job copies, queue/worker/dispatch and client conversion. It is
not process RSS or whole-engine CPU evidence. A copied executable initially
lost execute permission; no probe ran in that failed attempt. Corrected retry
preserves permissions and runs after all database measurements finish.

| Envelope | Parse allocations before → after | Requested bytes before → after |
| --- | ---: | ---: |
| array-write | 6 → 1 | 506 → 8 |
| escaped-fields | 6 → 2 | 512 → 24 |
| point | 4 → 0 | 488 → 0 |
| unknown-wide | 152 → 0 | 11194 → 0 |
| with-command | 5 → 0 | 498 → 0 |

Raw selected fields match across all four probe legs. Escaped names/array
values still require parser scratch; no blanket zero-allocation claim is made.
Default-off strict disposable native controls, positive = reduction:

| Corpus / state / callers | Latency | Process CPU |
| --- | ---: | ---: |
| short scalar / materialized / 1 | +0.82% | +0.06% |
| short scalar / materialized / 32 | -3.14% | +0.41% |
| short scalar / materialized / 8 | +3.72% | +1.84% |
| short scalar / overlay / 1 | -0.35% | +0.27% |
| short scalar / overlay / 32 | +3.16% | +4.27% |
| short scalar / overlay / 8 | +7.24% | +4.10% |
| short array / materialized / 1 | -5.77% | -4.08% |
| short array / materialized / 32 | -0.26% | +0.03% |
| short array / materialized / 8 | -3.20% | -3.49% |
| short array / overlay / 1 | +0.87% | +0.47% |
| short array / overlay / 32 | +1.92% | +1.17% |
| short array / overlay / 8 | -4.26% | -0.60% |
| long scalar / materialized / 8 | -3.97% | +0.55% |
| long scalar / overlay / 8 | -0.06% | +1.87% |
| long array / materialized / 8 | +0.05% | +0.58% |
| long array / overlay / 8 | +12.99% | +11.90% |

Every adverse cohort and per-leg tail remains in evidence. Short array single
materialized CPU rises 4.08%; long scalar materialized latency rises 3.97%
despite CPU falling 0.55%. No universal read-speed or tail victory is claimed.
SDK default-off mixed-corpus results:

| Case | Latency reduction | CPU reduction |
| --- | ---: | ---: |
| single | -7.43% | -7.76% |
| parallel-8 | -0.16% | +1.96% |
| parallel-32 | +1.54% | +1.63% |
| text-256k | -0.08% | -0.07% |
| array-4096 | +0.08% | -0.29% |
| escaped-text | +0.98% | +1.03% |
| wide-map-4096 | -1.90% | -1.05% |
| escaped-keys | -0.35% | -0.55% |
| float-text-256k | +1.34% | +2.07% |
| numeric-boundary | +8.31% | +9.80% |

The mixed SDK single-case regression triggered an independent 10×-length,
eight-leg A/B/B/A/B/A/A/B single-only control: 320,000 exact reads, latency
reduction 1.39%, CPU reduction 1.19%.
Baseline leg means are 18.24/18.34/19.76/19.01 µs; candidate
20.52/18.19/17.65/17.95 µs. The adverse first candidate remains. This does not
establish a cause for variation or revoke the original 7.43%/7.76% regression.
The deterministic acceptance basis is removal of envelope map/key allocations
with preserved semantics, not an all-cohort end-to-end latency claim.

Separate profiling-on native envelope-decode means are 1.289 → 1.054 µs over
124,204 calls per role. They include type decoding and payload copies, and are
wall time outside sampled dispatch. Observer off/on/on/off controls are retained;
these diagnostic means are not default-off production CPU credit.
Active sampled storage/retained two-label collector remain 6,936/183,328 bytes.
End-of-corpus SDK RSS means are 74,386 → 73,250 KiB, with 130 threads in both
roles; this mixed process measurement does not isolate parser residency.

| Candidate workload mean, µs | TofuDB embedded | SQLite sidecar | Direct SQLite |
| --- | ---: | ---: | ---: |
| conversation_open | 45.90 | 333.36 | 18.20 |
| record_get_points | 27.04 | 281.06 | 5.86 |

All four three-engine legs retain both workloads without errors/skips; 500-read
calibration legs have insufficient P99 coverage. Direct SQLite point latency
remains about 4.6× lower. Full dominance and complete main-bottleneck defeat
remain unproved. The dispatcher and worker handoff are still core targets.
Broader gates retain their existing failures; only paired new findings are
checked, and their nonzero exits are not reported as passing full-repository gates.

## Caller allocation changes require native transport validation

Problem: decoding catalog operation names and discarding non-string command IDs adds caller allocations before worker admission. Decision: retain owned envelope decoding. A catalog `Cow` prototype saved roughly four observed successful allocations per read, but regressed fresh-overlay native latency by 13–37% across 1/8/32 callers; long SDK cold arrays also regressed. Alternatives considered: static catalog-name ownership and skipping discarded ID decode errors. Their SDK gains did not satisfy the native transport gate. Static names still required worker-side owned conversion for shared dispatch; no caller buffer was retained. The source-bound, eight-leg comparisons and allocation observer are archived in `tofudb_operation_locality_20261004.json`. Allocation counts exclude aligned/internal libc families and receive no timing credit. Worker locality is a hypothesis, not an established cause. Production keeps existing detached workers, reservations, watchdogs, poison behavior and ownership checks.

## Completion readiness is not a proven replacement for response delivery

Problem: caller wake can precede the worker's next idle registration. Decision: retain response delivery followed by normal queue receive. A nonblocking completion-ready hint improved native 8/32-caller latency by 12–17%, but SDK arrays lost 3–5% latency and 2–5% CPU in normal and extended comparisons; escaped strings also regressed. Alternatives considered: blocking registration (rejected before measurement because it can delay a completed response) and a best-effort `try_lock` hint (rejected by mixed performance). Both remain private. Ten queue tests and twelve FFI tests passed for the latter, without claiming full production certification. Eight balanced legs per cohort and 8,308,800 exact timed reads are archived in `tofudb_completion_ready_20261004.json`. Initial 9–12% cold SDK losses did not reproduce at that magnitude; worker migration remains an unproven explanation. No production scheduling or profiler layout was changed.

## Point-read string transfer needs response-shape coverage

Problem: generic record compilation clones already owned namespace/key strings into IR and copies namespace for a boolean guard. Decision: retain current compilation until a replacement passes scalar and array transport comparisons. Alternatives considered: transfer strings by removing local map fields, or take their buffers while retaining map structure. Both preserve the parser, text validation, owner/tenant fences, task-result delegation, write digests and IR. Pointer tests prove ownership transfer; observers show approximately 34 → 31 successful allocations per read, with caller allocations unchanged. Scalar and SDK gains coexist with array regressions: the retained-map variant loses native array latency/CPU by up to 5.22/4.59%; removal also loses extended cold reads. Neither is deployed. Each passed six focused/neighbor tests; no full production certification is claimed for rejected variants. `tofudb_record_payload_transfer_20261004.json` retains 18,921,600 exact timed reads, two private source commits and all adverse cohorts. Allocation observation is separate from performance credit. Deletion alone does not explain the retained-map regression; allocator locality, code layout and worker scheduling remain hypotheses. Existing performance anchors and runtime resource budgets are unchanged.

## Rust allocation and completion handoff

Problem: embedded Rust used System allocation, while the CLI used mimalloc; workers woke replies before idle registration. Decision: Linux preparation opts into `embedded-mimalloc` with dynamic TLS and a nonblocking completion-ready hint; busy locks skip it. FIFO, poison, watchdogs, ownership, reservations, admission/IR and integrity checks remain intact. Default Rust consumers retain their allocator unless opting in. Actual `tofudb_embed_allocator_kind` (0 System, 1 mimalloc) and CLI `embedded_allocator` metadata attest feature selection before publication. Linux feature-enabled libraries link NODELETE, retaining callbacks and detached worker code until process exit; activation follows the existing lifecycle.
Alternatives: System-only, process-wide preload, allocator alone, hints alone. Preload changes caller allocations; standalone variants have mixed costs. Final combination: 1,558 native tests pass (two existing ignores), 47 preparation tests pass; consumer compatibility and close/dlclose checks pass. Eight-leg final comparisons improve concurrent scalar latency 11–15%, array latency 7–19%, extended SDK arrays 11%. `tofudb_embedded_allocator_20261005.json` retains all variants/adverse rows: 31,228,800 exact timed reads, 9,460,800 on final source. These allocator measurements used the earlier fixed pool; current demand delivery is defined in [worker delivery](TOFUDB_WORKER_DELIVERY.md), with durable authority unchanged. Array RSS grows roughly 1–2 MiB initially; SDK may grow a few MiB; roughly 1 GiB extra virtual reservation is not resident RAM. Worker-register/queue-lock anchors include best-effort hints, so counts include extra calls; no counter layout change. Scalar timing has no RSS columns. Full SQLite dominance and exclusive worker-migration attribution remain unproved.

## Field submission and compiler boundaries

Problem: repeated outer-envelope parsing may inflate the raw SQLite point gap. Decision: reject both private field-submission ABIs; production keeps its original interface. Alternatives: common out-of-line submission, explicit inlining, and the same candidate forced through the old ABI. First arrays regress 3–14%; forced-old array CPU also regresses 8–13% at one/eight callers, defeating SDK-only attribution. Inlining recovers array gains of 1–4%, but SDK concurrency still regresses 1–4% and native scalar 32-caller latency 4%. `tofudb_field_submission_20261005.json` retains 15,270,400 exact timed reads, 16/4 focused native and 14/15 SDK passes, source pins, adverse tails/memory and the 3,000-read profiling observer. Envelope decode averages 1.14 us; queue/handoff roughly 4–5 us. Overlapping wall spans are not CPU savings. Compiler boundaries remain a supported hypothesis; safe handoff/shared reads remain the root target. No complete-suite, deployment or SQLite victory credit is claimed.

## Optional allocator attribution ABI

Problem: process-wide allocator environment overrides can change host/Python and Native allocation behavior together, hiding the source of resident memory. Decision: only an explicit `allocator-diagnostics` build exports `tofudb_embed_collect_allocator(force)`. It invokes the library-linked mimalloc collection (0 ordinary, 1 aggressive), returning 1 after invocation, 0 after a caught panic, or -1 for invalid input. It changes no options and owns no database handle. Normal builds omit these diagnostic exports. Free pages may be reclaimed across Native heaps; live allocations remain owned. Tests keep two authorities, exact receipts and a live buffer valid through both modes and verify close/reopen. These exports are attribution tooling; ordinary close owns the separate policy below. Alternatives: global environment tuning conflates allocator instances; invoking unexported symbol addresses bypasses a supported ABI; automatic hot-path purge adds an unmeasured latency cost. Full-harness evidence must precede any policy.

The same feature exposes `tofudb_embed_allocator_live_bytes`, `peak_bytes` and `version` (each with the `tofudb_embed_allocator_` prefix). A nonallocating wrapper counts successful Rust Layout bytes until deallocation, including successful realloc deltas; failures keep the old charge. Counters aggregate all Native authorities sharing this library, not individual owners. They exclude arena overhead, fragmentation, C allocations and other allocator instances. Atomic observer cost means diagnostic builds cannot certify production latency.


## Reclaiming free pages after healthy embedded close

Problem: authority retirement freed Rust objects while the linked allocator retained hundreds of MiB of free resident pages. Decision: `embedded-mimalloc` requests aggressive collection after successful graceful stop and handle destruction. System-allocator builds, null/poisoned handles, failed stops and ungraceful abandonment keep their previous behavior. Other live allocations remain owned; collection never joins detached workers or changes durability, allocator options or caches. No worker, timer, counter or persistent state is added.

Alternatives: immediate ordinary collection did not reclaim the observed pages; delaying close adds idle time; global overrides affect host allocators. The closing caller pays the collection CPU cost. Three normal-image paired 100-upload cohorts reduce large-import post-close RSS to 16.1–16.3% and retained growth to 7.4–7.7% of baseline. Close duration increases 9.6–13.0%; the complete upload/close/reopen segment changes by −0.2% to +1.0% wall time and −3.3% to +2.8% CPU. `system_native_close_reclaim_20261015.json` retains HTML and adverse rows. These scoped observations do not qualify cold starts or whole-system migration.

## Complete snapshot Value evidence

Problem: the borrowed snapshot encoder avoids copying private trees, but its complete result loses producer evidence. Semantic response validation and the embedded envelope then parse the serialized strings again. Decision: move the private trees into one complete `Value`, inspect only its container depth, map ordering and reserved Serde markers, and encode that same frozen value into `EncodedResponse`. Only this encoder can mint its evidence; arbitrary bytes cannot. The selected IR slot propagates the evidence, while mutation, overwrite, extraction, replay and notice re-encoding retain their existing invalidation boundaries. Missing values remain unverified. Deep or reserved-marker trees keep the original parser path. Attempt normalization, source/owner/deadline checks and the complete response byte ceiling remain. Alternatives: accepting an arbitrary byte/depth pair weakens integrity; caching parsed snapshots adds invalidation and residency; a separate specialized transport duplicates authority. The complete Value adds a small outer map but moves its existing child trees, and adds no resident cache, queue or worker.

`tofudb_snapshot_value_proof_20261014.json` retains 32 complete
reports, the frozen native source delta, build identities, diagnostic runs and
all adverse legs. Native verification passes 1,891 library checks (eight existing
ignores) and 38 process checks; benchmark contracts pass 142. Four fresh corpus
legs check 32,000 timed responses exactly, with matching complete cross-build
hashes. The combined parallel-development source passes the library checks again
and 8,000 additional exact readbacks; it carries no comparative timing claim.
No durable format, concurrency, cache capacity or production backend changes.

Unprofiled embedded window P50 is 0.359/0.205/0.219/0.242 ms locally and
0.246/0.205/0.208/0.352 ms on FUSE (A/B/B/A). Escaped/Unicode corpus P50 is
9.710/5.263/5.228/9.746 ms; plain large text is essentially unchanged.
Production-adapter history P50 is 0.629/0.618/0.634/0.634 ms: no stable material
application gain. Raw SQLite still leads point/history reads. A separate sampled
snapshot diagnostic observes the old semantic validation span at 21.865 us per
request; the candidate selects producer evidence instead. These are wall spans,
not CPU measurements or admission timings.

The initial large-append cohorts are adverse by about 5%; 1,000-write-per-leg
controls retain all samples and do not reproduce a stable median regression
(A/B/B/A 1.742384/1.742975/1.727659/1.738696 ms). Tail variability remains.
Four corrected client/native/delivery-profile-off 1,000-conversation cohorts
recover all exact contents/replays and 5,000 ordered pushes through both SIGKILLs.
Candidate scheduled-stream P99 is 3.315/3.344 s and settle P99 7.813/8.004 s;
every leg still fails the 1/5 s targets. This is component progress, not broad
raw-SQLite superiority, stable capacity, provider/browser or soak certification.

## SDK bookkeeping after native read reuse

Problem: after native snapshot reuse, sampled window FFI takes about 3 us but JSON decode 28–29 us; a separate 15,022-byte complete-response decode costs about 30 us. Neither isolated nor actual-window controls collect GC cycles. Raw SQLite's smaller direct-row response remains a floor, not equivalent complete-response work.

Decision: retain each thread's buffer/capacity as one immutable pair, publishing after allocation; notify teardown when the last admitted storage/checkpoint call exits. Close/abandon preserve locked admission, refusal, drain, native stop and final notification for concurrent stoppers. Trees remain owned; growth, retention and worker counts are unchanged. Alternatives: repeated tuple/attribute work, unconditional notifications, global GC tuning, shared mutable trees or dropping public fields.

`tofudb_sdk_read_boundary_20261015.json` preserves exact artifacts, frozen sources, observer calibration and all adverse arms. Warm-point mean improves 3.16%, first-key reads 3.78%. Window mean is initially 1.12% adverse (P99 24.27%); separate counter controls improve mean 1.40% but retain 2.79% adverse P99. Extended composed default-cache points remain 9.28% adverse, large writes 3.18%; independent first-key controls do not erase them. SDK/adapter checks pass 187 cases; an installed wheel verifies 8,230 exact reads, 32 callers, 2 MiB values, mutable ownership and reopen. Native artifacts retain their prior qualification. No window-tail, whole-application, 1,000-conversation SLO or broad SQLite victory is claimed. <!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

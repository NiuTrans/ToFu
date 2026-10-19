# Native JSON materialization and projection replay

This contract owns JSON buffer ownership in `versioned_document.rs` and
`turn_projection_patch.rs`, the compact-head reader in `turn.rs`, and semantic
response validation in `response_json.rs`. Whole-buffer receipt zlib replacement regressed and remains private; `tofudb_receipt_deflate_20261005.json` also attributes >200 ms outliers primarily to authority-mutex waiting, with the competing holder still unproven. Producer-owned receipt eligibility follows the selected-response mutation/lifetime rules in [package guidance](../packages/tofu-db/AGENTS.md); historical rejection and adverse-tail attribution remain in `tofudb_receipt_proof_20261005.json`.
Persisted bounds and patch vocabulary remain defined by [`tofudb_ir_v1.json`](../contracts/tofudb_ir_v1.json); the authority, WAL and
owner-scoped entity keys keep their existing publication boundaries.
Owned model-context composition and exact sizing follow
[context materialization](TOFUDB_CONTEXT_READ.md).

## Bounded document reads

The byte API returns the original JSON bytes after validation. The decoded-value
API returns that one parsed value, logical key and source byte length; consumers
must not validate JSON by building a value and then discard and rebuild it.
Both paths share document decoding, namespace checks, authenticated Blob reads, explicit tenant/owner parameters and the requested output bound.

Inline values reject an insufficient bound before copying their body. Blob
reads first validate reachability, manifest identity and scope, then reject a
logical body exceeding the requested bound before allocating its body buffer
or reading payload chunks. A permitted read still verifies every payload chunk. Over-budget requests do not bypass identity or authorization checks.

## Private projection ownership

Problem: replaying each patch cloned the entire accumulated projection twice;
text append also rebuilt its existing prefix. Long patch chains repeatedly copied unchanged evidence and already accumulated content. Checkpoint/patch
readers parsed the same JSON twice and cloned the resulting objects again.

Decision: reconstruct one privately owned object and consume it through each
patch. Text append reuses its String capacity; the final object moves out.
Borrowed callers retain independent-copy semantics. Wire budget, version and
operation-count checks happen before copying a borrowed projection. Nested
path/type checks still run on the private object, and a failed partial result
never escapes. Patch diffing borrows the input root maps and retains only the actual operations instead of cloning both root documents.

The compact-head reader validates checkpoint attempt/revision, every patch's
attempt and contiguous revisions, accumulated patch bytes, and the final byte
witness before replacing its caller's logical projection. An error retains the
physical descriptor and publishes no partial value. Persisted representation, head rollover/retirement, OCC witnesses and durability are unchanged.

Alternatives considered: a parsed-head cache requires version/owner fencing,
budget accounting and reclamation and leaves cold replay's repeated-copy work;
skipping JSON, identity, revision or byte-witness checks weakens correctness. Moving copies to another thread would retain their CPU and memory costs.

## Measurement boundaries

`projection_head_materialize` is inclusive; `projection_head_checkpoint`,
`projection_head_patch_load` and `projection_head_patch_apply` separate its work.
Checkpoint/patch byte counters count materialized valid JSON before logical
fences; applied patches include successful members of a later-rejected head. They measure work, not acknowledged writes or unique durable projections.
Global totals include background consumers; request work identifies the caller.
Patch counters cover the structural prefix; text-tail reconstruction is included
in the total materialization time and keeps its separate text-page counters.
No counter retains identities or content, and no new resident worker or cache is introduced.

The executable allocation contract replays 64 small Unicode text additions
without copying an unrelated 192-KiB evidence string or replacing a text buffer
with sufficient capacity. Inline/Blob bounds, namespace and Blob scope have explicit regression coverage. Benchmarks and defeated hypotheses live in
`tofudb_projection_reconstruction_20261001.json`.
Finite replay gains do not certify all SQLite workloads or resource metrics;
authority admission, rotation, settlement and memory peaks remain separate gates.

The strict same-source ABBA reconstruction probe uses one seed plus 32 small
patches and 512 KiB of unchanged evidence. Its default-off average read latency is 4.044–4.048 ms before and 2.760–2.761 ms after; process RSS is
487.2–487.7 MB before and 425.2–426.1 MB after. This is a reconstruction case,
not a total database memory certification. The 1000-conversation capacity gate
still fails latency acceptance despite exact recovery and zero errors; its
sampled completed dispatch is predominantly FIFO wait. Dropped profile requests and overlapping RSS ranges remain visible in the evidence.

## Encoded semantic response validation

Problem: the semantic executor deserialized its already encoded response into
`serde_json::Value` only to check validity, then discarded every decoded string,
map and array. This repeated object construction taxes document-heavy reads.

Decision: `response_json::validate` traverses the same serde_json parser with a
non-materializing visitor. It checks all numbers, UTF-8, Unicode escapes,
container syntax, trailing bytes and the same recursion limit. Ordinary keys,
strings, arrays and maps retain no decoded objects. Escaped strings still need
the parser's temporary scratch buffer; this is not an allocation-free promise
for all JSON. Serde's reserved first-map-key markers use the previous Value
validation path, including escaped marker names and markers in nested objects.
Desktop-specific response checks and the public integrity error remain unchanged.

The compiler and validator have opt-in `semantic_transaction_compile` and
`semantic_response_validate` spans. The compilation span covers the shared
`compile_admitted_transaction` path; specialized turn/diagnostic/parallel paths
retain their existing probes. The validation span covers the previous generic
response check, excluding desktop validation and early specialized returns.
These spans are inside dispatch and overlap its existing inclusive execution
spans. They must not be added as independent end-to-end latency. Profiling stays off by default. No content, identities or paths are retained.

Alternatives rejected: skipping response validation weakens the corruption fence. Deserializing `IgnoredAny` accepts numeric overflow, invalid UTF-8,
unpaired Unicode surrogates and excessive nesting that Value rejects in our
pinned dependency. A raw JSON syntax scan shares that compatibility problem.
Caching decoded responses retains owner/version invalidation and memory costs.
The visitor instead removes work from every ordinary response without a cache.

Verification: six response-validator parity tests cover malformed/truncated
JSON, byte mutations, numbers, strings, recursion limits and Serde markers.
Allocation/timing experiments and real C ABI/SDK/three-engine comparisons live
in `tofudb_response_validation_20261002.json`. This optimization does not certify comprehensive SQLite superiority.

The matched default-off SDK ABBA uses 28,600 exact timed readbacks per leg and the same Python SDK. The 4,096-object array case changes from
5.181–5.268 ms to 4.070–4.072 ms (about 21–23% lower mean); case CPU changes
from 1.0755–1.0946 s to 0.8495–0.8519 s for 200 reads. Its validation-only allocation probe removes 16,397 allocations / 2,911,655 requested bytes per
call. This is transient validation work, not a reduction in persistent data or a promise of lower process RSS: post-array RSS remains around 66 MiB.

Small SDK/native reads and the maintained three-engine point/header cohorts
show no consistent universal improvement. Raw SQLite point means remain about
6 microseconds versus TofuDB's 31–40 microseconds in the maintained test. The mixed-cohort escaped-text means slightly regress (5.419–5.440 ms versus
5.474–5.534 ms); a separate fresh-authority escaped-only ABBA reverses direction
(5.468–5.505 ms versus 5.425–5.429 ms). Both cohorts are retained. This is small, context-sensitive behavior, not evidence of an escaped-text victory.
Each document case has only 200 measurements, insufficient for a P99 claim.

Same-candidate off/on/on/off observer trials use sample-every 0/16/16/0; point means are about 22/24/23/22 microseconds. Array means range from
4.053–4.065 ms off to 4.076–4.117 ms on. Profiling is useful diagnostics with
visible observer cost; default-off ABBA alone establishes the performance result.
The separate array profile captures all 210 reads with no dropped samples or
invalid span accounting. Mean native dispatch is 3.280 ms: exclusive `ir_steps`
is 2.952 ms, response validation 0.309 ms and compilation 0.002 ms. These are
sampled wall-time attribution, not CPU measurements. Remaining array cost is
predominantly inside IR execution; a compiler optimization alone cannot remove it.

Two new stages add 256 fixed atomic-counter bytes and 48 bytes to each bounded
profile sample/active request. Measured active-request storage is 5,816 bytes
and retained collector storage is 145,248 bytes for the sampled workloads.
All SDK cases retain 130 process threads. Native correctness is 1,511 passed, 2 ignored, including FUSE crash/fault and embedded lifecycle boundaries.
Measured baseline source is `f7679020505a568ad882c54f11acab8046e2f58e3cff10d05dae1c1560e6e0b8`;
candidate source is `1d04a9008746e4262cb84ad9482fe78cffe11b7b2de215dfb6e84de9a52b7446`.

## Canonical versioned-document projections

Problem: `project_document` parsed the complete document into Value, then
`json!` built another Value tree for its metadata envelope, and serialization
walked that tree again. Large arrays and maps paid repeated allocation and copying inside the IR steps identified by the response-validation profile.

Decision: `canonical_json_bytes::canonicalize` recognizes bytes that exactly
match the current serde_json Value serializer. It traverses the strict parser,
checks ascending unique decoded map keys, and compares each serialized primitive
and structural token with the input. Complete parsing and exact byte comparison
are required before borrowing the original body. Ordinary unescaped map keys
borrow input strings; escaped keys and string decoding can allocate temporary
scratch. Borrowed strings compare serializer writes directly; unborrowed escaped
strings encode into a temporary Serde Vec and compare that contiguous result. A recognition failure always uses the previous Value parser/serializer.
Whitespace, duplicate keys, unsorted maps, noncanonical numeric/string spellings
and reserved Serde markers therefore keep their old canonical output and errors.

The projection encoder retains the previous metadata key order and uses the
same serde serializer for keys, timestamps and versions. It copies the validated
canonical body into one owned response buffer. Inline/Blob reachability, scope,
namespace/identity, value and response bounds remain at their existing owners.
No stored bytes, format versions, receipts, mutation publication, caches, workers
or configured backends change. Byte-returning and decoded-value APIs keep their separate contracts; this changes the encoded projection path only.

Opt-in anchors split `document_value_materialize`, `document_json_canonical`
and `document_project_encode`. `document_json_fallback` is nested within the
canonical stage and measures the complete legacy parse/serialization fallback.
Materialization excludes the preceding entity fetch, document-envelope decode
and identity check. These are wall-time spans nested in IR execution, not separate
end-to-end/CPU totals. Default-off observation avoids clock/counter work.

Alternatives rejected: injecting merely valid raw JSON changes number coercion,
duplicate-key and serialization behavior; assuming every stored value is already
canonical breaks legacy snapshots and migration input. A new persisted flag needs
format/downgrade certification, while a decoded-object cache adds owner/version
fences, retention and eviction. Recognizing canonical bytes removes the duplicate objects without either storage changes or a cache.

Six canonical-output tests compare with the previous parser/serializer, including
byte mutations, recursion, Unicode, numbers and Serde markers. Projection tests
compare exact old envelope bytes with and without keys, exact output bounds,
malformed inputs and correct/wrong Blob scopes. Full native and matched SDK, C ABI, three-engine, allocation and observer receipts are retained in
`tofudb_document_projection_20261002.json`.

The first native gate retained 1,480 passing unit tests, two ignored tests and
one failure because a randomly shaped probe's Metadata occurrence 15 never fired
in 16 replay attempts. The unchanged targeted replay passed, so it did not prove
that this failed full-gate receipt was clean. The fault test now supplies one
fixed test segment ID to both probe and replay through a test-only entry point.
Production still allocates random IDs at the original eligible-batch boundary.
All fired-fault requirements and acknowledged-state recovery checks remain; the initial source/commit, failure log and targeted replays are retained.

The first measured implementation compared every escaped-string serializer fragment directly. It reduced kernel allocations but regressed escaped-text
SDK means from 5.447–5.457 ms to 6.674–6.793 ms (default-off ABBA); kernel means
rose from 2.605–2.613 ms to 4.719–4.722 ms. That source is rejected and retained
in an isolated commit with its matched binaries, successful 1,519-test receipt
and raw reports. The cause was repeated tiny comparison writes. The selected
implementation uses the original Serde Vec serializer for unborrowed strings
and compares one contiguous result. This transient allocation releases before
the next value, never becomes a cache, and preserves the strict canonical check.
The original unbuffered result must not be credited as the selected build.

Matched selected source `dbb32a0c…` versus baseline `1d04a900…` passes 1,519
native tests with two ignored, plus 25 profile-report tests. Each default-off
SDK ABBA leg checks 29,000 exact timed returns; each C ABI leg checks 116,000.
The SDK array means are 4.053/4.129 ms before versus 2.227/2.490 ms after (42.3% lower pooled means; measured process CPU 40.4% lower). Wide-map means
are 6.457/6.583 versus 2.946/3.303 ms (52.1% lower; CPU 49.4% lower). Escaped-key means are 1.439/1.454 versus 0.817/0.883 ms. The mixed-corpus
escaped-text result still rises 7.9%; the dedicated default-off ABBA measures 5.426/5.454 versus 5.469/5.436 ms (pooled +0.23%), and kernel means are
2.587/2.603 versus 2.615/2.617 ms. Retain both cohorts; do not discard the
slow mixed leg or call escaped strings a proven speedup. The initial roughly 23.5% consistent regression is rejected; its allocation win alone was not
sufficient to accept it.

The isolated kernel checks exact previous bytes: array allocation/reallocation
calls fall from 32,801 to one, wide maps from 42,340 to one. These are projection
kernel allocation counts, not whole-process allocations, RSS or peak memory. SDK final RSS remains approximately 69–74 MiB across these legs, with 130
threads in both builds. No stable resident-memory improvement is established.
Sampled profiles report 5,912 active-request bytes and 148,512 retained bytes;
the same two-operation collector previously retained 145,248 bytes, so its
measured increase is 3,264 bytes. Four stages add 96 bytes per active/retained
sample and per retained operation aggregate. Each of two one-in-16 runs reconciles 1,845 requests with zero errors/drops.
Same-binary off/on/on/off small single-caller means are 21/25/23/22 us; observation has measurable cost and stays disabled by default.

Scalar C ABI reads remain approximately 17–18 us; eight-caller warm-overlay candidate means include a slower 47 us leg versus baseline 40–41 us. Small
record or concurrent latency superiority is not established. The maintained
three-engine medium runs measure TofuDB points at 30–31 us, application SQLite
at 231–251 us, and raw SQLite at 6 us. Header-only conversation opens are 41–46 us before and 49–50 us after, versus raw SQLite 18 us. An independent
balanced eight-leg followup with 1,000 warmups and 20,000 timed opens per leg
retains baseline 40/41/42/41 us and candidate 50/42/42/42 us. This path uses the
separate decoded-document contract and shows no reliable improvement; the slower candidate legs remain evidence, not a claimed completed optimization.
The first followup setup mistakenly supplied a foreign owner; the engine denied
it before measurement. Reproduction uses the maintained harness owner without weakening the scope check.

The isolated every-request array profile reconciles 210 `record.get` samples
with zero errors: mean dispatch is 1.306 ms, queue-plus-dispatch 1.312 ms. Exclusive canonical recognition is 0.839 ms, response validation 0.332 ms,
materialization 0.123 ms and envelope encoding 0.003 ms. The first two parser
passes are the next measured hotspots. These are sampled wall spans; they do
not measure CPU, and cannot be subtracted from another cohort's SDK latency.
All-scenario/all-metric SQLite superiority remains unproven. Documentation and
architecture gates retain the baseline findings, with byte-identical logs; they are reported as failing gates, not counted as passes.

## Borrowed-string canonical recognition

Problem: a strict Serde slice parser already checks each unescaped string's
UTF-8/control characters and returns the input slice. Canonical recognition then serializes that same string and scans it for escapes again, including
object keys. The every-request array profile identified canonical recognition
as 0.839 ms of a 1.306 ms mean dispatch, larger than response validation.

Decision: reuse only the parser's borrowed-string proof. The private comparison
checks both token quotes, exact offset/length and input pointer identity before
advancing over a string. No equal text in a different allocation can establish
this proof. The pinned compact serializer writes these unescaped contents unchanged; numeric normalization, key ordering/uniqueness, marker handling,
structural-token comparison, strict parsing and parser end remain unchanged.
Escaped strings/keys retain the same Serde encoding. Legacy JSON still uses
Value conversion on any recognition failure. No persisted flag, format change, cache, new authority, worker or backend change is introduced.

`json_borrowed_strings` and `json_borrowed_string_bytes` count skipped string
serializations and their decoded UTF-8 content bytes, excluding quote bytes.
The private comparison accumulates counts locally and emits two work updates
once per recognition attempt, without per-string TLS/clock probes. A failed attempt may contain a verified prefix before the complete Value fallback;
these counters describe performed work, not whole-body certification. Existing
canonical/fallback wall spans retain their scope. Two work dimensions add
16 bytes to each active/retained profile sample and each operation aggregate;
collection remains opt-in, labels/samples keep their existing bounded lifecycle.

Alternatives: trusting a new persisted bit needs an authoritative admission
proof, format/downgrade certification and validation of raw/imported writes;
a mere bit is not evidence that arbitrary stored bytes are canonical. Assuming
borrowed strings are correct without binding their offset, length and pointer
loses the token proof. Comparing/serializing all string bytes again retains
the measured redundant scan. Thread-local decoded-object caches add identity,
version, retention and cold-read obligations without removing first-read work.

Nine focused tests include exact foreign-allocation/offset/empty-string proof,
all 1,112,030 unescaped Unicode scalars as independent raw UTF-8 values and
map keys (never generated by the serializer under test), escaped/whitespace inputs and the prior
numeric/depth/marker/duplicate-key/byte-mutation oracle. The parser and compact
formatter are dependency-owned; this exhaustive serialization test is required
when their versions or configuration change. Full native, matched CLI/FFI, SDK/C ABI/three-engine/observer and allocation evidence is retained in
`tofudb_borrowed_strings_20261002.json`.

The initial 1,522-test passing build used serializer-generated Unicode inputs;
that fixture could hide a future change to the formatter's escaping rules. Its source, commit, binaries and full passing receipt remain archived. The
selected fixture builds raw quoted UTF-8 directly, then compares with the legacy
Value output for both scalar values and map keys. Production recognition logic
is unchanged by this strengthening; final validation uses the selected source.

Matched source `0225869e…` versus `dbb32a0c…` passes 1,522 native tests with two
ignored and 25 maintained profile-report tests. SDK default-off ABBA checks
29,000 exact timed returns per leg. Array means are 2.249/2.240 ms before and
1.942/1.915 ms after (-14.1%; measured process CPU -13.1%). Wide maps are
2.993/2.953 versus 2.596/2.548 ms (-13.5%; CPU -12.5%). Plain 256 KiB text is
0.545/0.547 versus 0.476/0.441 ms (-16.0%; CPU -16.1%). Scalar SDK means stay
22 us; concurrent 32-caller means include slower candidate legs. Mixed escaped
text rises 1.5%; dedicated ABBA is 5.453/5.546 versus 5.491/5.449 ms. Escaped
keys show no stable improvement, and their kernel candidate includes a slower
332 us leg versus baseline 302–303 us. Preserve all these results; the new proof
benefits unescaped tokens and does not establish every-shape superiority.

Kernel ABBA compares the previous canonical helper with the selected helper,
using exact old envelope bytes. Array means are 858/859 versus 559/562 us, wide maps 1,096/1,099 versus 666/668 us and plain text 142 versus 47 us.
Allocation counts stay unchanged in these borrowed cases. No whole-process allocation, peak RSS or CPU attribution follows from this metadata fixture.
C ABI checks 116,000 exact reads per leg: scalar reads remain approximately
17–18 us; some concurrent candidate legs are slower. The three-engine corpus
retains TofuDB points 27–30 us versus raw SQLite 6 us, and header opens 41–67 us
versus raw SQLite 18 us. The independent eight-leg conversation followup is
baseline 43/42/47/53 us and candidate 42/42/44/45 us; mixed-corpus slower legs
remain recorded. No stable universal small-record/conversation win is claimed.

Each of two one-in-16 profiles reconciles 1,845 sampled requests with zero
drops/errors. The isolated every-request array profile checks 210 record reads,
each skipping exactly 12,289 strings and 60,335 decoded content bytes; observed
work totals equal the corpus-derived totals. Mean dispatch is 0.978 ms and queue-plus-dispatch 0.987 ms. Canonical recognition is 0.515 ms, response
validation 0.328 ms, materialization 0.122 ms and envelope encoding 0.003 ms.
Compared with the preceding same-recipe profile (canonical 0.839 ms), this
removes a substantial scan cost; exclusive wall spans are not CPU and cannot be subtracted from another SDK cohort. Canonical recognition and response
validation remain the next parser hotspots.

Same-binary off/on/on/off scalar means are 21/23/24/21 us; sampling has a cost
and remains disabled by default. Active-request bytes increase 5,912→5,928;
retained bytes for the same two-operation collector increase 148,512→149,056 (+544). No workers, caches, persistent state or profile caps change. Full
all-scenario/all-metric SQLite superiority remains unproved. Documentation and
architecture checks retain existing baseline failures without added findings.

## Validated response provenance

**Problem.** The borrowed-string every-request array profile spent 0.328 ms
reparsing the final response after strict canonical recognition (0.515 ms). The encoded body and its bounded metadata envelope already establish JSON
validity, but an untyped `Vec<u8>` discarded that information.

**Decision.** `canonicalize_with_depth` returns an opaque body with the maximum
container depth only after complete strict recognition. Legacy conversion retains the original Value parser/serializer and produces no certificate.
`DocumentProjection` owns immutable encoded bytes and adds the metadata object
to the depth; lists compose every row's certificate plus their outer array.
The default parser budget remains 127 containers for the complete response,
including envelopes. A valid body at the boundary may still need rejection.

`IrSlots` binds the certificate to the actual produced slot and invalidates it
before every mutable index access. The selected `IrResponse` has no mutable
byte accessor; consuming it into raw bytes discards the certificate. Semantic
execution skips generic validation only for this immutable proven result.
Raw entity reads, literals, missing values, overwrites and serialized receipt
replay retain ordinary validation. Desktop-specific response checks remain
unconditional. No authority, physical format, response bytes, error mapping, deadline, commit or receipt publication rule changes.

**Alternatives.** An operation-name whitelist cannot prove the actual selected
bytes. A persisted validity bit is unsafe across raw/import writes and adds a
format migration. A thread-local last-proof flag can outlive its source slot. Ignoring wrapper depth weakens the existing integrity boundary. Each is
rejected by the producer-owned certificate and its mutation/lifetime tests;
these are design alternatives, not measured failed performance experiments.

**Attribution.** `json_response_proof_reuse` counts actual semantic reuse and
`json_response_proof_bytes` counts the entire projected response. Generic
validation spans remain present on unproven outputs. The two work dimensions
add 16 bytes per active/retained sample and operation aggregate. The existing
64-slot bound gains 64 transient booleans, with no cache, workers, persistent
state or profile-cap growth. Profiling stays disabled by default.

**Evidence.** Frozen builds, full gates, paired measurements and limitations
bind baseline `0225869ecfc927d9820c88ce79d6219f8b5c228a65a5b538eb0b59cd7b1a3224`
to candidate `a27a0178fd65fc6b62ee2e4718e831fc6c52ee2458b250eef569c9a39ae86296`.
The native gate passes 1,530 tests with two existing ignores; focused checks
cover seven proof boundaries, ten canonical tests and 25 report tests. All
builds/tests finish before strict disposable-FUSE SDK/C ABI/three-engine ABBA.
SDK checks 29,000 exact values per leg; C ABI checks 116,000 exact byte replies.

| SDK shape | Baseline mean ms, two legs | Candidate mean ms, two legs |
| --- | --- | --- |
| 256 KiB plain text | 0.442 / 0.445 | 0.401 / 0.403 |
| 4,096-item array | 1.907 / 1.910 | 1.607 / 1.616 |
| Escaped text | 5.476 / 5.538 | 4.157 / 4.064 |
| 4,096-field map | 2.521 / 2.535 | 2.364 / 2.166 |
| Escaped keys | 0.838 / 0.864 | 0.697 / 0.747 |
| 32 small-record callers | 0.401 / 0.414 | 0.452 / 0.413 |

The independent escaped-text ABBA retains 5.441/5.450 versus 4.115/4.075 ms.
Full-envelope 64-row lists are 1.862/1.808 versus 1.524/1.506 ms, a balanced
17.4% reduction. The mixed SDK 32-caller cohort is 6.1% slower; its eight-leg
isolation is baseline 0.394/0.415/0.417/0.433 versus candidate
0.396/0.399/0.411/0.419 ms. The stable regression is not reproduced, but the
original slower cohort stays recorded. C ABI overlay eight-caller balanced latency is also 1.6% slower; this is not universal dominance.

Every-request array profiles each reconcile 210 reads. Baseline dispatch is
0.986 ms, including 0.519 ms canonical recognition and 0.331 ms final validation;
candidate dispatch is 0.668 ms with zero final-validation calls. The work ledger
proves 210 reuses and 26,211,990 complete response bytes (124,819 each). The list
profile independently proves 210 reuses and 24,044,370 bytes (114,497 each).
Both one-in-16 mixed profiles reconcile 1,845 samples without drops/errors.
Active bytes grow 5,928→5,944 and the same two-operation retained collector
149,056→149,600. Off/on/on/off scalar means are 25/23/23/22 us; this noisy cohort
cannot establish zero observer cost, and default-off timing remains separate.

Candidate array canonical recognition is now 0.532 ms, about 80% of dispatch;
materialization is 0.124 ms, about 19%. These are the remaining native hotspots.
The three-engine corpus still has native points 29 us versus raw SQLite 6–7 us,
and header opens 51–57 us versus 18 us. Eight isolated header-open legs stay
42–43 us for both builds. Further parser/representation work is required;
full all-scenario/all-metric superiority remains unproved.

## Canonical blob certificates

**Problem.** After eliminating final response reparsing, canonical recognition
still consumed 0.532 ms, about 80% of the every-request array dispatch. A warm
read repeated that grammar/key/serializer scan even though complete blob materialization already re-established the same scoped content identity.

**Decision.** `BlobReader::materialize_verified` owns collection of every actual
chunk, completes scoped content verification (the original hash or exact current whole-blob dependency evidence below), and returns opaque
`VerifiedBlobBytes`. It cannot seal an arbitrary supplied buffer or a partially
consumed reader's suffix. Bytes have no mutable accessor; consuming the Vec
discards the witness. Manifest/scope checks still precede bounds, payload reads
and allocation. Existing bound messages and source-integrity errors remain.

`canonical_json_cache.rs` accepts only that type. A cold lookup performs the
existing strict canonical recognizer; only success with a depth certificate
populates metadata. A warm lookup binds the currently verified source's tenant,
owner, complete content hash and logical length to that certificate. Every read still verifies the current manifest, chunk contents and complete body
before lookup. The cache never replaces materialization or trusts a persisted
BlobReference by itself. Noncanonical legacy conversion, malformed JSON and
inline values keep the original path; no converted bytes or errors are cached.

Only body grammar/depth is reused. Metadata key/version/timestamp encoding,
response byte bounds, outer object/array depth composition, IR slot invalidation
and semantic validation for unproven results remain per-response operations.
Namespace/key identity checks, ownership, MVCC root selection, publication, receipt replay, deadlines and corruption behavior remain unchanged.

**Lifecycle and accounting.** Each authority owns a lazily populated cache;
read replicas and anonymous metric observers share its Arc. A new authority
open starts empty. The last authority/replica/observer release drops metadata;
no background workers or persistent files are added. Startup derives capacity
from the existing probed/overridden hot block-cache budget divided by 64. Direct/offline authorities remain disabled until that budget is configured.
Conservative index accounting is 4 KiB base plus 512 bytes per entry; two ordered
indexes provide LRU with exactly one recency record per certificate. Repeated
hits cannot grow a stale-stamp queue. Resizing evicts to the new bound, zero
capacity releases roots, and clock overflow drops reconstructible certificates.
Poisoned optional metadata falls back to original recognition and reports unavailable; it cannot convert a source error into success.

**Attribution.** `json_canonical_proof_reuse`, `json_canonical_proof_bytes` and
`json_canonical_proof_miss` measure sampled certificate reuse, source bytes and
required cold scans. Existing canonical/materialization/encoding spans retain
their boundaries. Three work dimensions add 24 bytes per active/retained sample
and operation aggregate. `canonicalJsonProofCache` exposes only availability,
capacity/accounted bytes, record counts, hits, misses and evictions, without
source identities or authority locking. Launch `resourceBudget` includes the derived `canonicalJsonProofCacheBytes`.

**Alternatives and rejected variants.** A persisted valid bit or an operation
whitelist cannot bind current bytes. Looking up by an unverified BlobReference
before reading can conceal a forged logical identity, a scope error, corruption
or a failed source read. Retaining complete canonical bodies creates another
payload cache and increases resident memory. This implementation instead reuses
only small certificates after the completed immutable source witness.

An initial empty-cache trim removed the final records but retained the standard
library's two empty index roots. A layout-equivalent tracked-allocator fixture
measured 1,528 retained bytes after final pops and zero after map replacement.
That candidate was rejected: its partial native gate and successful release
build receive no final-gate or timing credit. The corrected trim explicitly
releases empty maps. Source, logs, binaries and experiment are retained with
the evidence. Earlier fixture-only failures staged a blob without a durable
semantic mutation; their guard remains intact and fixtures use the existing
atomic blob commit. Design rejections are separate from measured regressions.

**Frozen evidence.** Machine-readable results
retain baseline source `a27a0178` and final source `b9cb0454`, matched CLI/loaded
DSO receipts, exact readbacks, balanced sequential cohorts and profile ledgers.
The final native gate passed 1,539 tests, with two existing ignores; focused
checks include complete/partial/hash-failing blob reads, scope/MVCC/cache/depth
boundaries and 25 maintained Python profile-report tests. The rejected initial
source `0d3b0d2f` receives no final gate or timing credit.

Default-off Python SDK ABBA means (baseline/candidate milliseconds):

| Value | Baseline | Candidate | Mean latency reduction |
| --- | ---: | ---: | ---: |
| ASCII text, 256 KiB | 0.4010 | 0.3575 | 10.8% |
| Array, 4,096 items | 1.5975 | 1.0635 | 33.4% |
| Escaped text | 4.0935 | 2.4735 | 39.6% |
| Object, 4,096 keys | 2.1490 | 1.5010 | 30.2% |
| Escaped keys | 0.6965 | 0.3920 | 43.7% |

A second escaped-text ABBA remains 4.0685→2.4775 ms. Every-request array profiles
separately show canonical wall time 0.529295→0.002581 ms and total native dispatch
0.669478→0.143637 ms. The candidate still spends 0.123719 ms (86% of dispatch)
on materialization; client conversion/delivery is outside native spans, so the
native reduction must not be substituted for SDK latency. Exactly 210 responses
retain the existing whole-response proof: one cold canonical scan plus 209
cache reuses, 26,076,303 reused source bytes and no final generic validation.
Only the cold scan visits 12,289 strings/60,335 string bytes. One certificate
and one recency record account for 4,608 bytes; the mixed five-body corpus
accounts for 6,656 bytes. These are conservative cache index estimates, not RSS.

**Limits and unfavorable measurements.** Inline list responses do not use this
blob cache; their ABBA means are 1.4850→1.4925 ms. Scalar/8-caller/32-caller SDK
means are 21.5→21.0, 98.5→98.0 and 404→405 us. Eight additional 32-caller legs
average 410.5→433.5 us (5.6% slower); they are retained as an unresolved adverse
measurement, not dismissed as noise or credited as a win. Native C ABI scalar
32-caller means are 234.356→232.531 us in overlay and 231.232→235.262 us after
materialization; eight-caller legs vary substantially. Inline paths never touch
the new proof mutex. The data does not establish all-concurrency superiority.

The maintained three-engine corpus still reads small points in TofuDB at
29–32 us versus raw SQLite's 6 us, and header opens at 41–67 us versus 18 us.
Eight isolated header-open legs average 44.0→42.5 us; this small difference does
not resolve the gap. The legacy SQLite adapter is a distinct comparison.
Off/on/on/off scalar means are 22/23/23/21 us: sampling is not claimed free.
Sampled active memory is 5,944→5,968 bytes, and the same two-operation collector
149,600→150,416 bytes; both one-in-16 runs reconcile 1,845 samples without drops
or errors. Documentation and architecture gates retain their pre-existing
failures; native correctness passed independently.

**Next bottleneck.** Complete source materialization/hash verification now
dominates the array's native dispatch. Subdivide manifest acquisition, chunk
fetch/verification, full content hashing and buffer collection before selecting
another optimization. Small-record queue/dispatch and concurrent SDK latency
require separate attribution. Cache reuse does not authorize skipping source
integrity, durability or ownership checks. Full all-scenario/all-metric SQLite
superiority remains unproved.

## Verified decoded blob chunks

**Problem.** The post-canonical-cache array profile spends 0.139696 ms in native
dispatch. Nested probes identify 0.069522 ms in decoding, 0.023334 ms in chunk
logical hashing, 0.027599 ms in full scoped content hashing, and 0.003143 ms in
buffer collection. These wall-time spans supersede guesses about the residual
materialization cost; complete bytes and exclusive/inclusive ledgers reconcile.
A codec-only balanced ABBA with 16,000 exact decodes measures fresh/reused Zstd
contexts at 69,840.5/69,319 ns, only 0.747% lower. Context reuse was rejected as
an insufficient root remedy; that experiment is not a database speedup claim.

**Decision.** `blob_decode_cache.rs`, a child owned by `blob.rs`, retains positive
chunk decoding/hash certificates for compressed immutable blocks. A lookup
follows the current backend's actual successful block read and requires the
same encoded `Arc<Vec<u8>>` allocation, BlockId, and expected logical length as
the originally verified decoding. BlockId or a header alone is insufficient.
A replacement allocation misses even for equal bytes; invalid replacement bytes
enter the original header/decompression/hash checks and fail as before.

Only the exact original decoder establishes an entry. The original encoded
Arc and decoded Arc have no mutable alias; caller-owned Vec output clones when
needed and cannot mutate the certificate. Warm reads reuse decoded bytes and
chunk-hash evidence, but still check the current manifest/scope/reference,
source-read result and logical byte count. Complete scoped BlobId verification
uses the independently bounded whole-blob proof below or the original hash. Bounds
precede payload reads. Range reads retain their intersecting-chunk semantics
and do not acquire a whole-blob certificate. Legacy/raw chunks are not retained;
all parser, response-envelope, durability, publication, MVCC and receipt fences
remain unchanged. Optional cache poisoning falls back to original decoding.

**Lifecycle and bounds.** Authorities and their read replicas share one lazy
cache; reopen creates an empty cache. Anonymous observers may keep it alive
until their own release; the last authority/replica/observer drop releases all
pins. Capacity is the existing probed/overridden hot block-cache budget divided
by 16; offline authorities start disabled. Charges conservatively include both
encoded and decoded Vec capacities, 512 bytes per entry and a 4 KiB index base,
even when encoded bytes are also charged to the existing block cache. LRU keeps
exactly one recency record per entry. Oversize entries are not admitted; resize,
zero capacity and clock reset release reconstructible records and empty roots.
In-flight handles can outlive eviction, bounded by the existing caller/worker
lifecycle and one format-bounded 1 MiB chunk per active reader, in addition to
its already bounded response. No workers, persisted cache or format change.

**Anchors.** Seven opt-in stages separate manifest opening, chunk source reading,
decoding, logical hash verification, full content hashing, collection and cache
access. Five work counters expose decoded/collected/full-hashed logical bytes
and reused chunk proof counts/bytes. Default-off probes do not read clocks.
`system.metrics.decodedBlobChunkCache` exports anonymous occupancy/capacity,
hits/misses/evictions and availability; launch `resourceBudget` includes
`decodedBlobChunkCacheBytes`. Metrics never acquire the authority mutex.

**Alternatives.** A global or authority-wide decompressor mutex would serialize
parallel decodes and the measured reuse gain is negligible. Skipping complete
content hashing would weaken the existing scoped manifest-to-body witness.
Looking up by persisted BlockId before the actual read can hide a failed or
changed source. Retaining unbounded whole projected documents adds payload and
metadata duplication; this bounded chunk cache instead reuses a specific pure
decode computation after source acquisition, with explicit memory tradeoffs.

The initial full gate found a separate fixture lifetime failure: the existing
conversation-header comparison initialized two databases from temporary
TempDir expressions, which removed their paths before the first seed commit.
The same unit executable reproduced ENOENT. The fixture now retains both guards
until database teardown. No production commit/error behavior changed; the
failed source/gate and reproduction are retained without final-gate or timing
credit, and the corrected source is rebuilt and gated independently.

A subsequent full gate found an unexercised foreground metadata fault target.
Random foreground checkpoint segment UUIDs alter catalog bytes and shard
layout, unlike the already fixed background fixture ID. The same executable's
focused replay passed, confirming the original result was not a stable complete
matrix. An explicit per-engine, bounded, test-only checkpoint ID sequence now
controls foreground probes/replays; 16 no-fault trace comparisons precede the
matrix. Every target must fire on its first replay and all recovery assertions
remain; retries and skipping unexercised targets are not credited. Production
checkpoint IDs still use `Uuid::new_v4`. The failed full gate and its superseded
binary stay retained without final correctness/timing credit.

**Frozen measured evidence.** Machine-readable results
retain baseline `b9cb0454`, final `43cd6870`, CLI/loaded DSO identities, all raw
cohorts and failed variants. The final gate passes 1,543 tests, two existing
ignores; 25 Python report tests pass. Earlier failed gates are not credited.
Default-off SDK ABBA means (milliseconds):

| Value | Baseline | Candidate | Mean latency reduction |
| --- | ---: | ---: | ---: |
| ASCII text, 256 KiB | 0.3810 | 0.2885 | 24.3% |
| Array, 4,096 items | 1.0880 | 0.9865 | 9.3% |
| Escaped text | 2.4800 | 2.0815 | 16.1% |
| Object, 4,096 keys | 1.5180 | 1.3685 | 9.8% |
| Escaped keys | 0.4120 | 0.3510 | 14.8% |

Escaped-only followup remains 2.4995→2.0780 ms. Native C ABI array means improve
12.1–21.3% across overlay/materialized and 1/8/32 callers, with 18,400 exact
readbacks per leg and independent full-value verification outside timing.
Single-caller overlay is 488.233→401.610 us; materialized is 488.153→429.150 us.
Every-request array native dispatch separately measures 0.139726→0.061903 ms.
Only one of 210 reads decodes/hashes its chunk: 124,767 decoded bytes, 209 chunk
proof reuses and 26,076,303 reused logical bytes. All 210 still hash the whole
scoped body and collect 26,201,070 bytes. Canonical/body-envelope proof ledgers
remain independently reconciled; final generic validation is absent only on
already proven whole responses. Cold chunk decode/hash amortize to
0.000398/0.000126 ms, full content hashing remains 0.028386 ms. Projection encoding
is 0.014825 ms in this cohort versus baseline 0.003371 ms; that adverse/outlier
stage is retained rather than removed from the dispatch mean.

**Memory and unfavorable measurements.** One decoded entry plus one recency
record account for 136,764 bytes; five entries account for 1,031,784 bytes under
the 134,217,728-byte launch capacity. Both mixed SDK legs use 130 threads, as do
both baseline legs. End-of-corpus RSS means are 71,748→74,030 KiB (+2,282 KiB,
about 2.23 MiB); these include client/runtime/allocator effects, not just cache
payload, and are distinct from capacity accounting. Active sampled request
memory is 5,968→6,176 bytes; the same two-operation collector is
150,416→157,488 bytes. Both one-in-16 runs reconcile 1,845 samples without
errors/drops. Scalar off/on/on/off means 22/24/24/19 us do not establish free
profiling.

The mixed scalar SDK cohort is 21→22 us; scalar native C ABI single-caller means
are 17.374→17.507 us (overlay) and 17.596→17.735 us (materialized). Native overlay
32-callers regress 3.9%, and materialized eight-callers regress 6.5%. Eight
additional SDK 32-caller legs average 417.0→418.75 us; neither the initial
mixed-cohort concurrency win nor prior phase's regression proves a universal
concurrency result. Inline lists are 1.5400→1.5005 ms despite not using this
cache, so this difference is not attributed to chunk reuse. Isolated header
opens average 44.75→45.00 us. Small-point three-engine legs remain native
29–37 us versus raw SQLite 6–7 us; header opens remain 42–51 us versus 19–20 us.
All adverse legs stay in the evidence. Full all-scenario/all-metric superiority
is still unproved.

## Verified whole-blob dependencies

**Problem.** After canonical transport proof handoff, the array diagnostic still
hashes 26,201,070 logical bytes over 210 reads: 5,865,699 ns, about 27.93 us per
read. Chunk decode/hash reuse alone cannot remove this separate scope-bound
manifest-to-body computation. Transport measurements are owned by
[embedded response](TOFUDB_EMBEDDED_RESPONSE.md#immutable-canonical-dispatch-results).

**Decision.** `blob_whole_cache.rs` retains only positive complete scoped content
certificates. `BlobReader` opens and validates the current manifest, checks
owner/reference/atomic commit eligibility, applies the caller's size bound,
reads every current chunk successfully and decodes it through the original
validated chunk/cache boundary. Reuse requires the exact immutable manifest Arc,
identity (tenant, owner, BlobId, logical length), and every ordered encoded chunk
Arc/count from an earlier successful complete materialization. The collected
length is checked independently. Only then can the reader certify the actual
collected bytes without repeating the full scoped hash. A miss hashes the entire
collected body with the original scoped hasher before admitting a certificate.
Fresh allocations miss even for equal bytes; valid but changed chunks must
pass the complete hash; malformed chunks/current I/O failures keep their errors.
A persisted BlockId or an operation name never establishes a proof.

Only fresh complete materialization enters this path. Streaming cursors and
range reads retain their original hashing/intersection semantics; partially
consumed readers cannot certify suffixes. Public mutable Vec extraction drops
the byte witness, and retained encoded allocations have no mutable alias.
Below 4 KiB the reader uses its existing hash path without dependency lookup or
admission, to avoid fixed metadata work on small records. Poisoned optional
metadata always falls back to full verification. No format/durability change.

**Lifecycle and accounting.** The existing decoded-chunk cache owns a separate
lazy certificate index, shared by authority replicas and anonymous observers;
reopen starts empty. The same launch probe/override sets its capacity to the
hot block-cache budget divided by 64, in addition to the decoded cache's /16.
Entries charge the manifest and all encoded Vec capacities, source-vector
capacity plus per-source Arc/Vec allocation metadata, 512 bytes per entry,
and a 4 KiB index base. Repeated sources are conservatively charged repeatedly.
Admission never retains an oversized dependency list: a reader drops attempted
pins as soon as they exceed its allowance. Each retained certificate has one
LRU record; resize, zero capacity, eviction and clock rollover release records
and empty roots. In-flight readers can retain a previously admitted certificate
until completion after eviction/resize, bounded by its admission allowance,
existing active-reader lifecycle and format limits. There is no extra decoded
body cache, worker or persistent growth.

**Anchors.** Three default-off stages distinguish lookup, dependency comparison,
and certificate admission. Four work dimensions count complete proof reuse,
reused logical bytes, full-proof misses and successfully read current encoded
bytes (manifest plus chunks). Existing full-hashed/collected-byte counters remain.
These stages add 384 fixed counter bytes; three sampled stage slots plus four
work dimensions add 104 bytes per request/sample/operation aggregate.
`decodedBlobChunkCache.wholeBlob` exposes anonymous availability, occupancy,
lookups, manifest matches, completed reuse and eviction counts. Manifest matches
alone are not successful whole-body proofs. `resourceBudget.wholeBlobProofCacheBytes`
reports the launch capacity; neither observer takes the authority mutex.

**Alternatives.** Omitting full verification without current dependency evidence
would weaken the scoped witness. A BlockId-keyed cache before source reads can
hide current errors/replacements. Persisted validation bits do not prove the
current allocation lineage. Retaining full projected bodies duplicates durable
payload and has a different lifetime. These alternatives are rejected. The
cold path still incurs a full hash, and raw chunks keep their own logical hash.

**Verification and failed attempts.** Tests warm proofs before injecting current
manifest/chunk failures, equal-byte fresh allocations, corrupt chunks, valid
changed bodies, reordered chunks, wrong owner/tenant, mismatched scoped hash,
size bounds and absent atomic commit evidence. Streaming, range, mutable output,
capacity, oversize, LRU pins, clock reset and poison fallbacks are covered.
The first synthetic two-chunk fixture used a short nonfinal chunk; the strict
format parser correctly rejected it before the optimization. The fixture now
uses two full 1 MiB chunks; the rejected source/test/log is retained separately
and earns no final correctness or timing credit. No decoder relaxation or retry.

Measured results and all unfavorable cohorts are retained in
`tofudb_whole_proof_20261003.json`.

**Frozen measurements.** Final source `988b6318` passes 1,551 native tests,
two existing ignores and 25 Python report tests. Matching CLI/loaded DSO
ABBA legs run after builds/tests finish on strict disposable FUSE authorities;
every timed native/SDK reply is exact. In 210 array reads, full-hashed bytes
fall 26,201,070→124,767: one miss, 209 whole-proof reuses (26,076,303 bytes).
Collection remains 26,201,070 bytes; current encoded reads total 1,574,370 bytes.
Sampled dispatch is 50.105→25.575 us. Full-hash spans total 5,830,958→28,371 ns;
lookup/dependency/store total 22,372/8,186/1,063 ns, separately accounted.

| Default-off mean | Baseline | Candidate | Latency reduction |
| --- | ---: | ---: | ---: |
| Native array overlay, one caller | 69.910 us | 42.109 us | 39.8% |
| Native array materialized, one caller | 69.157 us | 42.622 us | 38.4% |
| Native array overlay, eight callers | 78.819 us | 54.683 us | 30.6% |
| Native array materialized, eight callers | 79.586 us | 59.264 us | 25.5% |
| SDK ASCII text, 256 KiB | 0.2435 ms | 0.2015 ms | 17.2% |
| SDK array, 4,096 items | 0.6620 ms | 0.6315 ms | 4.6% |
| SDK escaped text | 0.7380 ms | 0.6690 ms | 9.3% |
| SDK object, 4,096 keys | 0.9775 ms | 0.9605 ms | 1.7% |
| SDK escaped keys | 0.2320 ms | 0.2145 ms | 7.5% |
| SDK float/text, 256 KiB | 0.2405 ms | 0.1955 ms | 18.7% |

**Unfavorable cohorts.** Initial native array overlay32 regresses 6.1%
(271.474→288.084 us); materialized improves 7.3%. Eight additional balanced
legs measure overlay 266.660→261.191 us (2.1% lower), materialized
282.098→251.956 us (10.7% lower). They do not replace the adverse cohort.
Initial native scalar materialized8 regresses 19.1% (45.548→54.262 us);
overlay single/32 and materialized32 also regress 1.8/2.3/1.5%. A separate
eight-leg cohort, eight callers and 50,000 reads per caller per state, performs
6,400,000 exact reads: overlay 45.029→44.229 us; materialized 43.475→45.643 us
(still 5.0% worse). Small values do not use the new whole-proof path; these
source-level differences do not establish their cause. SDK parallel8 regresses
1.5%; numeric boundary stays 22.5 us; single improves 22→21 us. Eight isolated
SDK32 legs regress 423.25→428.75 us (1.3%). Isolated header opens measure
44.75→44.25 us. Every original/adverse/followup leg stays in the evidence.

Six whole-proof entries account for 27,152 bytes (one array entry: 12,153)
under a 33,554,432-byte allowance, alongside 1,294,583 decoded-cache bytes.
Baseline/candidate SDK cohorts both have 130 threads. End-of-corpus RSS means
are 74,486→74,394 KiB (−92 KiB); this whole-process difference, including
Python/runtime/allocator, does not prove free metadata. Active sampled bytes
are 6,400; the same two-operation retained collector is 165,104 bytes.
Off/on/on/off observer results are retained independently.

**Remaining root.** A separate native one-in-16 diagnostic mixes states and
1/8/32 callers (not default-off timing credit), with 1,662 sampled gets per
build. Mean dispatch falls 142.924→113.600 us; exclusive FIFO wait grows
73.853→76.339 us and now dominates. Whole-proof lookup averages 0.431 us,
dependency comparison 0.091 us; aggregate spans do not establish a global
cache-lock regression. Queue/admission/worker delivery needs its own causal
measurement and design. Raw SQLite small points remain 6–7 us versus TofuDB
29–30 us; header opens 19–20 us versus 43–48 us. The SQL Sidecar is a separate,
much slower transport baseline. All-scenario/all-metric superiority is unproved.

## Final IR result ownership

Problem: after all IR steps completed, `result_bytes` cloned the selected
slot's complete response allocation. The original allocation stayed in the
execution frame while receipt policy and outbox preparation ran. A large
response therefore paid another allocation and copy before transport encoding.

Decision: final extraction takes the selected slot allocation and its exact
immutable JSON certificate together. It clears the slot and certificate before
returning the owned response. Steps and predicates have already finished;
receipt insertion and outbox capture continue to use the final response and
transaction. Missing results and IR literals still clone their declared
fallback bytes and remain unproven. Invalid slots keep the original error.
This change applies to the shared IR executor, with unchanged request scope,
admission, validation, commit, replay, watchdog and caller-buffer boundaries.

Alternatives: cloning was safe but duplicated an entire response. Sharing a
mutable response allocation with slots would complicate certificate invalidation
and retain otherwise dead execution buffers. Removing the FFI worker would
lose watchdog isolation; published reads can still wait for pins or source I/O.
Taking ownership after the last step needs neither cache nor worker redesign.

Default-off diagnostics: `ir_result_extract` covers final selection and transfer;
`ir_receipt_response_policy` covers the existing command response classification.
Sample work counters `ir_result_moved_bytes` and `ir_result_copied_bytes`
distinguish transferred results from literal/missing fallback copies. These
are local byte counts without payloads, identities or paths. Inclusive spans
remain inside dispatch and are not additive CPU measurements. Two stage slots
add 256 fixed counter bytes across the global/seven component arrays; two work
slots plus stages add 64 bytes per bounded active/sample aggregate. No pool,
cache, persistent growth or new background worker is introduced.

Evidence: IR ownership measurements.
The allocation transfer specification checks a 1 MiB response's original pointer
and capacity, and rejects reuse of the emptied slot. The existing proof suite
continues to exercise actual result selection, mutation invalidation, missing
results and unproven receipt replay. End-to-end timing requires exact ABI/SDK
readbacks, matched source identities and profiling-disabled balanced legs.

Matched verification passes 1,578 native tests with two existing ignores. CLI,
DSO and debug test CLI identify source
`72d490b11a4f6096ece2349c0ff301afc22da27b76a15bb1d5ead4d3d7fed83c`;
baseline is `e760421e990f984d9fdfa052a9813fbc5db2a783a546941e128d9bdeb1e2f7f9`.
All eight ABI/SDK/observer/publication drivers pass. Exact timed byte readbacks,
concurrent strict writes, replay, fault and lifecycle boundaries remain covered.
Active request storage measures 6,904 bytes, 64 above the matched baseline.

Positive percentages below mean reductions; adverse results are retained.

| ABI array corpus | Storage / callers | Mean latency | Process CPU |
| --- | --- | ---: | ---: |
| Short | materialized / 1 | +21.23% | +19.09% |
| Short | materialized / 32 | -1.44% | +1.40% |
| Short | materialized / 8 | +18.31% | +10.48% |
| Short | overlay / 1 | +8.85% | +7.66% |
| Short | overlay / 32 | +0.30% | +7.54% |
| Short | overlay / 8 | +14.40% | +10.40% |
| Long | materialized / 8 | +6.28% | +5.00% |
| Long | overlay / 8 | -2.98% | -1.96% |

Small native reads show no consistent gain: short latency changes span
-2.88% to +1.84%; long scalar changes are -1.69% materialized and +1.53%
overlay. This does not defeat the worker/queue bottleneck.

The mixed SDK cohort retains single -9.13%, numeric-boundary -20.13%,
float-text -8.68% and array -1.55% mean changes. Its legacy mean field rounds
milliseconds to three places; the comparison instead uses the same sum-derived
throughput. Independent tenfold cohorts retain unrounded means and raw samples:

| Independent SDK corpus | Mean latency reduction | Process CPU reduction |
| --- | ---: | ---: |
| single | -0.06% | -0.49% |
| numeric-boundary | +0.59% | +0.46% |
| float-text-256k | +8.18% | +8.67% |
| array-4096 | -0.90% | -0.61% |

The earlier large scalar/numeric/float SDK regressions do not repeat in these
independent cohorts. This does not establish their cause or discard the mixed
results. The SDK array still has a small adverse mean/CPU result. ABI array
benefits do not promise universal client speedup; response parsing and thread
handoff remain. The deterministic claim is removal of one selected-result
allocation/copy, not a claim of zero overhead or lower process RSS.

Same-mount strict three-engine calibration, candidate mean microseconds:

| Workload | TofuDB embedded | SQLite sidecar | Direct SQLite |
| --- | ---: | ---: | ---: |
| conversation_open | 41.79 | 303.24 | 18.09 |
| record_get_points | 27.32 | 256.44 | 5.90 |

All four balanced calibration legs retain every engine and workload without
errors. P99 coverage is insufficient in this 500-read calibration; no tail
victory is claimed. The direct-SQLite gap remains visible. Profiling-on
record.get queue/dispatch averages are 78.17/10.52 microseconds baseline and
79.85/10.96 candidate; these overlapping, separately sampled wall spans cannot
be subtracted into causal CPU savings. Final extraction averages 0.033
microseconds in the mixed diagnostic and the byte counters confirm transfers.
Observer off/on/on/off controls are retained separately from production gains.

The first compile attempt accidentally extended an unrelated enum expression
through a broad text replacement. It failed before running tests, was corrected
without changing assertions, and its source fingerprint/log remain in evidence.
No failed build is counted as verification or performance evidence. Broader
architecture/documentation gates are compared against their existing failures;
finite measurements do not certify all-scenario, all-metric SQLite dominance.

## Owned semantic payload decoding

Problem: the generic `semantic_executor.rs::payload_object` decoded an owned
`serde_json::Value`, then used `as_object().cloned()`. This recursively allocated
and copied the entire request tree before compilation. Only one paper artifact
compiler had a separate consuming helper; normal writes and replay compilation
paid the duplicate allocation cost.

Decision: match `Value::Object` and transfer its owned map to every generic
compiler, including the former special caller. Keep the exact same parser,
number features, root-object rejection, request digest, owner validation,
idempotency, WAL, fault behavior, worker dispatch and watchdog boundaries.
Specialized raw-payload compilers keep their existing paths. Add the optional
`semantic_payload_decode` span around parse/root extraction and the
`semantic_payload_decoded_bytes` work field for successfully parsed bytes
(including a parsed non-object rejected at the root). These diagnose actual
parse work; they do not count the eliminated clone as runtime work.

Alternatives: preserve the deep clone and tune allocation, replace the parser,
or bypass worker dispatch. Ownership transfer removes the redundant work
without new cache invalidation, parser equivalence or timeout semantics. A
separate process-wide mimalloc preload experiment remains unshipped: CPU often
improves, but latency is mixed and it does not prove a Rust-only DSO allocator
change or its late-load/unload lifetime safety. The CLI already uses mimalloc;
embedded-library allocation policy must be evaluated at its own boundary.

Evidence: frozen builds, controls and adverse cohorts.
Native source is `29add8937ed34ebc825e53e1de7202bc64bc3e481eac0caa141c7eb1b3f88b0e`;
baseline is `72d490b11a4f6096ece2349c0ff301afc22da27b76a15bb1d5ead4d3d7fed83c`.
Debug CLI and release CLI/DSO identities agree with their frozen roles. Full
native verification: 1,578 passed, two existing ignores; response-proof,
canonical-digest, embedded lifecycle and profile boundaries also pass. All
eight core drivers, three additional drivers and four three-engine controls
exit zero. No production database or runtime configuration is changed.

An isolated allocation probe extracts both actual frozen helper bodies, using
the engine's locked serde_json version/features, with diagnostic stubs disabled.
It measures allocator requested bytes, not process RSS or full-engine CPU.
Acceptance and canonical output agree on all 27 inputs, including invalid
roots/UTF-8/depth/numbers, duplicate keys, escaping and byte mutations.

| Shape | Allocations before → after | Requested bytes before → after | Peak requested live bytes before → after |
| --- | ---: | ---: | ---: |
| Small object | 12 → 6 | 1,318 → 659 | 1,318 → 659 |
| 4,096-item array | 32,798 → 16,404 | 5,693,694 → 2,912,319 | 5,562,750 → 2,781,375 |
| 4,096-key map | 42,230 → 21,115 | 6,107,128 → 3,053,564 | 6,107,128 → 3,053,564 |
| Escaped text | 22 → 19 | 918,770 → 721,525 | 459,389 → 459,389 |

Parser-built vector capacity can exceed a clone's exact-length capacity. This
is transient compiler ownership, not a promise of halved retained RSS. Escaped
text peak explicitly does not improve.

Strict same-mount write/replay ABBA, 100 unique acknowledged writes, 100 exact
idempotent replays and 100 exact readbacks per shape/leg. Timed execute_json
includes Python request serialization and engine background CPU; readback
verification is outside timing. Positive percentages mean reduction.

| Shape / operation | Latency reduction | CPU reduction |
| --- | ---: | ---: |
| array / write | +22.68% | +21.68% |
| array / replay | +34.20% | +34.20% |
| escaped / write | +1.29% | -1.47% |
| escaped / replay | -1.00% | -0.82% |

These finite 100-operation legs do not certify P99. Array write/replay benefits
are substantial; escaped-text CPU and replay regressions are retained.

Default-off read controls remain mixed. Long eight-caller native scalar
materialized latency/CPU regress 5.21%/1.46%; long array materialized regress
0.60%/0.45%, while array overlay improves 9.47%/8.71%. All 1/8/32-caller cohorts
and per-leg tails remain in evidence. Generic record.get payloads are small;
this change is not an explanation or defeat of all fixed read overhead.

| SDK case | Latency reduction | CPU reduction |
| --- | ---: | ---: |
| single | +1.61% | +1.41% |
| parallel-8 | -3.60% | -0.34% |
| parallel-32 | -0.57% | -0.58% |
| text-256k | -0.05% | -0.06% |
| array-4096 | -0.62% | -0.56% |
| escaped-text | -0.78% | -0.71% |
| wide-map-4096 | -0.03% | +0.30% |
| escaped-keys | -4.46% | -3.99% |
| float-text-256k | +0.18% | +0.27% |
| numeric-boundary | +0.32% | +0.70% |

SDK end-of-corpus RSS cohort means are 76,226 → 72,942 KiB with unchanged
130-thread counts. This mixed-process measurement does not isolate helper
allocation or certify lower RSS on every shape. Diagnostic observer controls
are separate from default-off performance; no overlapping wall-stage subtraction
is used to claim causal CPU savings.

| Candidate workload mean, µs | TofuDB embedded | SQLite sidecar | Direct SQLite |
| --- | ---: | ---: | ---: |
| conversation_open | 37.00 | 326.83 | 18.12 |
| record_get_points | 25.88 | 255.72 | 5.80 |

Each calibration has 500 reads; tail sample coverage is insufficient. The
native direct-SQLite point gap remains about 4.5×. All-scenario/all-metric
SQLite dominance and complete defeat of the main bottleneck remain unproved.

The allocator experiment uses the same baseline DSO in both roles, changing
only temporary process-wide preload for the timed native harness. It therefore
also affects harness/libc allocations. The official
[mimalloc override mechanism](https://microsoft.github.io/mimalloc/overrides.html)
and exact vendored v3.3.2 source/library hashes are retained. Scalar one-caller
materialized latency improves 4.91%, but 32-caller materialized regresses 1.20%;
array 32-caller materialized regresses 7.12% despite CPU improving 4.43%.
This is insufficient evidence for adopting an embedded allocator. No preload
or allocator change is delivered; all twelve row comparisons are retained.

Resource contract: one stage and one work field add 128 fixed atomic counter
bytes and 32 bytes per active sampled request, retained sample and operation
aggregate. Measured active storage is 6,904 → 6,936 bytes; the same two-label
retained collector is 182,240 → 183,328 bytes. No workers, caches, queues,
handle words, persistent formats or growth are added. Broader gates retain
pre-existing failures and are compared for new findings, not reported as passes.

## Entity point-read attribution and rejected query witnesses

Problem: first point reads intertwine immutable source resolution with key/OCC
witness allocation, atomic byte-budget reservation, and a retained value copy.
Aggregate EntityGet counts do not partition these costs. Removing unnecessary
commit certification from explicitly noncommittable IR queries looked promising,
but fewer operations alone do not prove faster application behavior.

Decision: keep the original point witnesses, transaction-local read reuse and
all writer/query limits. Add launch-time opt-in diagnostic stages:
`entity_point_prepare` covers a non-reused point read after scope/instance/usable
checks; nested `entity_point_resolve` covers overlay/tree resolution; nested
`entity_point_retain` covers witness insertion and optional value retention.
Preparation's exclusive time also contains witness budget reservation and
staged-write lookup. Reused reads return before these stages. Existing
`entity_get`, `entity_read_reuse`, overlay/tree and pin-budget work counters
explain branch selection. `entity_point_retained_bytes` counts only bytes
successfully duplicated into transaction-local value retention; it excludes
returned/source values, key bytes and reuse-result copies. Missing values,
failed reservations and source failures add no retained-byte count. Existing
source-error reservation release and fail-closed authority behavior stay intact.

Inclusive spans nest and must not be added as independent latency or CPU.
The three stages add 384 fixed process/component counter bytes. One work field
and three stages add 80 bytes per sampled request, sample and aggregate; the
collector remains bounded by existing operation/slow-sample limits. Disabled
profiling avoids new clock reads and atomic timing-counter updates. No cache,
queue, worker, persistent format or durable file growth is introduced.

Rejected alternative: a fresh declared IR Query could become noncommittable,
retain the same snapshot/scope lease, and skip point witnesses/value duplicates.
The private prototype rejects mutations, commit and late conversion, checks
registry poison, and preserves range quotas plus writer/preparation OCC. Its
1,585 tests passed with two pre-existing ignores, including five new authority,
source-failure, lease and quota cases. Nevertheless, it is not adopted. It
removed both point budget reserves in the scalar control, yet timing was mixed:
long eight-caller array CPU fell 4.1%/10.4% for materialized/overlay reads, while
scalar CPU increased 2.6%/0.5%. The eight-workload application cohort included
conversation-open latency +19.8%, record points +13.8%, and transcript hydration
+10.7%. All adverse legs are retained. Separate 800,000 exact single-caller
reads also yielded mixed latency; individual scheduling spikes are retained
without attributing a cause or discarding them.

A separate ABBA diagnostic of conversation.get/list found zero transaction-local
read reuse in both builds. This rules out lost reuse as the demonstrated cause
for those controls; it does not explain the regressions. Physical overlay/tree
mix varied as background materialization progressed, so cross-process source
counts alone are not a causal attribution. The rejected prototype and pinned
binaries remain in isolated Git history/evidence. Its read-only mode is not
part of current runtime behavior. Future optimization must separate source,
retention and reservation costs and demonstrate gains with profiling disabled.

Alternatives: omit witnesses on every replica (breaks parallel write preparation)
or every get (breaks OCC/ABA); weaken durability or bypass worker timeout/lifetime
boundaries (changes correctness); retain a prototype solely because allocations
fall (contradicted by the measured application regressions). Evidence includes
frozen identities, semantic tests, all performance legs, exact-result checks,
CPU/tails, three-engine controls and diagnostic observer runs:
`tofudb_query_witness_20261004.json`.

The adopted anchors passed 53 relevant tests (point reuse, IR, profiler,
published query and embedded transport). Separate default-off controls include
800,000 exact native scalar reads, 73,600 array reads at 1/8/32 callers, and ten
SDK shapes. Their mixed results are retained and do not establish a root
optimization: the data algorithm is unchanged. A sampled scalar diagnostic
counts exactly 62 retained-copy bytes per read. Batch conversation.list controls
show source-resolution spans of 0.52–1.07 ms per request versus retention spans
of 0.11–0.17 ms; roots/materialization differ across processes. These perturbed,
inclusive wall measurements direct investigation, not a CPU-saving forecast.
The scalar collector grew 183,328 -> 186,048 bytes and active request accounting
6,936 -> 7,016 bytes. Separate SDK off/16/16/off observer legs measured total
profiling CPU overhead about 20% for the small scalar shape and 29% for numeric
boundaries; this includes pre-existing probes and is not isolated new-anchor
cost. Profiling stays disabled by default; certification timings exclude it.

## Window read ownership and attribution

Problem: document decoding copied namespace/key/inline bytes before a second body copy; public Turn normalization replaced already canonical owned strings.
Decision: the existing format decoder borrows validated fields and inline bytes from its source. Decoded-value readers parse that view; byte/metadata/stream APIs acquire ownership only where it escapes. Public Turns retain normalized status/outcome/cause strings and update existing context-revision slots. Format, identity, owner, Blob reachability/content, byte/depth, execution-epoch, deadline and error checks stay in their existing owners. No cache, worker or persisted format is added.

Request-only opt-in probes separate `document_materialize`, `turn_public`, `turn_snapshot_encode`, `turn_snapshot_window` and `turn_snapshot_attempts`. They retain no identities/content. Five fixed slots add 120 bytes per active sample/operation aggregate; the measured all-request collector grows by 4,320 bytes. Existing resource-derived budgets account for those structure sizes. Inclusive elapsed spans are nonadditive and are not CPU time; default-off controls qualify latency.

`benchmarks/tofudb_point_boundary_measure.py --workload window --history-turns N` reuses the existing fixture, timing and profile owners. It checks every ordered ID/content/actor/time, plus native total, zero-based ordinals and continuation metadata. Default `point` behavior remains. Namespace/fixture and artifact hashes, raw samples, actual reads, warmup, whole-process CPU and lifetime peak RSS accompany each result. Native returns a full public snapshot; raw SQLite returns four-field rows. That ratio is a SQL row floor, not equal complete response work.

`tofudb_window_read_ownership_20261015.json`: 1,997 native tests pass (nine existing ignores), including full ownership/error suites; the public-Turn oracle checks 1,792 valid combinations and malformed inputs. Six retained benchmark tests and 184 neighboring Python tests pass. Twenty-four added default-off arms check 960,000 reads: six balanced blocks all improve window means; aggregate short/long-history means improve 1.21%/1.79%, process CPU 1.15%/1.42%, and averages of per-run P99 improve 4.47%/9.36%. These are bounded local effects, not pooled percentiles or a capacity certificate.
Matched complete profiles attribute ~45→42 microseconds/request to document materialization and ~14.5→13.4 to public normalization. Earlier window cohorts and neighboring/pipeline regressions remain in the evidence. All sixteen strict pipeline arms pass correctness and fail the overall SLO; application phase/delta stream P99 regress 2.26%/1.40%. No stable whole-system, footprint or raw-SQLite victory is established.

Alternatives: skipping validation weakens integrity; another parser duplicates format authority; a parsed cache adds invalidation/lifecycle work. Fewer allocations alone do not admit an optimization: the 2026-10-04 borrowed-document experiment reduced observed scalar allocations 34→30 but had adverse SDK CPU and mixed native timing, so it remains rejected for that population. Current admission relies on the separate combined-path controls and preserves that earlier defeat.

## Exact compiled read plans: defeated alternative

Problem: repeated point reads reparse payloads and rebuild an immutable generic
read IR. Decision: retain the original compiler. A private authority-owned cache
reused only fully compiled generic `record.get` plans with exact tenant/owner and
raw payload equality. Admission, current data, reader quotas, deadline/catalog
checks, blob/JSON verification, IR execution and worker/watchdog stayed intact.
Task-result reads and command IDs retained compilation. Probe-derived capacity
was block-cache bytes / 256; actual plan/payload capacities plus conservative
index metadata were charged, including evicted externally held plans. Empty
indexes released their roots; contention/poison fell back to normal compilation.
Nine cache, twelve IR and two record tests passed; no full release gate is claimed.

Separate profiling proves actual reuse: 200 reads across two writes still see
current values, while compile/decode calls fall from 202 to 3. Eight default-off
SDK and eight native legs verify 5,905,600 exact responses. Native scalar latency
improves 0.62%–7.31%, CPU 3.19%–6.18%; SDK single-small latency/CPU improve
4.51%/4.23%. But unique cold SDK reads regress 8.09%/5.81%, escaped/array CPU
regress 1.56%/2.25%, and eight-caller small latency regresses 0.85%.
Process RSS/HWM and tails remain in raw reports; worker count stays 130.

Alternative: select a cache because compilation disappears or warm points win.
The complete controls defeat that criterion. Observer counts and net gains are
not independent CPU-share equations. No read-plan cache is integrated; publication,
wake and facade construction remain attribution targets. Private code, all legs,
resource proofs, observer runs and artifacts remain in
`tofudb_read_plan_20261004.json`.


## Inline canonical proof reuse: measured seam, rejected admission costs

Problem: current inline reads repeat strict canonical recognition; large arrays
and escaped text amplify that work. Reuse requires the current scoped Entity/MVCC
read, full byte equality and original source/reader/witness checks. An index
fingerprint never establishes a proof.

Decision: retain the original runtime parser. Three private prototypes tested
SHA-indexed exact-byte proofs, cheaper CRC-indexed selective proofs, and delayed
payload retention until a second complete successful parse. Poison, contention,
legacy JSON and malformed bytes preserve the original fallback. All versions
and positive/adverse legs remain in private Git and canonical evidence.

Latest long escaped/array latency falls 45.26%/41.72%, CPU 42.98%/40.11%.
But 2,000 unique cold-array latency rises 2.35% and CPU 4.24%. Those 2,000
first-sighting entries cannot certify any body; with two completed proofs,
the population accounts 2,063,456 bytes. A bounded admission sketch is the next
alternative to test, not delivered behavior or a proven causal explanation.

Experimental inline capacity is block-cache budget /64 /2, additional to the
existing blob capacity. Bounds include actual boxed bytes plus conservative
metadata; eviction precedes allocation. Anonymous metrics expose inline
hits/misses/pending entries/bytes beside existing canonical stages. No workers,
durable formats or persistent growth change. Accounted bytes are not RSS.

Three normal paired SDK/native experiments and two longer SDK replays validate
22,510,400 timed reads. Canonical gates pass 20/21/21 tests; document-neighbor
gates pass 13 per source. Separate observers preserve current values/versions
across writes. No full release gate or universal SQLite victory is claimed.
Canonical evidence and raw receipts.

## Admission bitmap experiment and uncached parser direction

Problem: earlier first-sighting trees retained cold-read metadata. A private
probe-derived lazy two-bit bitmap removed those trees; saturation clears only
admission hints, never completed proofs or durable data. Hints still require
current strict parsing; completed reuse requires full scoped byte equality.
Decision: reject the bitmap runtime as well. Across 9,460,800 exact timed reads,
native arrays improve mean 5.91–62.41% and CPU 44.16–58.81%, proving an internal
JSON seam. But normal SDK cold arrays regress 5.21%/4.63%; longer cold arrays
improve while scalar latency/CPU regress 15.30%/14.18%. All legs remain.
Eliminating tree allocation does not prove the cause of residual regressions.
23 canonical and 13 document tests pass; no full release gate is claimed.
Next alternative: uncached integer-token proof to remove serializer replay
without per-key retention. `tofudb_inline_sketch_20261004.json`.

## Uncached integer proofs: four rejected runtime variants

Problem: inline arrays replay integer serialization after strict parsing.
Four private implementations retain exact legacy bytes, range/depth semantics,
current reads, shared admission/IR, worker ownership and all source checks.
Decision: reject all four; 39,355,200 exact timed reads retain adverse legs.
Typed reparsing regresses SDK arrays 6.13% latency/5.99% CPU. Lexical range
proofs improve arrays, but ordinary shapes show adverse observations. Dynamic
array flags remove scalar conversions yet regress native single-caller arrays
3.55%/3.50%. Compile-time visitors improve long SDK arrays 5.89%/5.41% and cold
arrays 8.51%/7.27%; repeated native scalar CPU still regresses up to 3.78%.
Private integer/byte anchors verify 200,000/578,000 array tokens across two
writes; semantic compilation/payload decoding still run 202 times. The last
variant passes 21 canonical-related and 13 document tests; no full gate claimed.
External hardware/stack observers add no production hooks. Same-process maps
resolve old perf's missing executable mappings. Visible scalar samples favor
queue/reply, atomic/mutex spin and allocation; JSON comparison is smaller.
Whole-process instruction counts rise only 0.12%, CPU task-clock 4.15%; this
cannot identify a particular lock or establish code-layout causation.
Comparison/work-array layout increments and all RSS/HWM/thread rows remain.
Next: investigate delivery/synchronization and allocation using these anchors;
keep authority, pin quotas, watchdogs and late-disposal contracts. Older long
cold-row count/throughput metadata is preserved with explicit corrections.
Implementations, raw legs, counters and symbolized probes.

## Full-history archive selection

Problem. `legacy_messages_full` treated the public `window=0` full-history
sentinel as an empty interval. It decoded sealed pages, then discarded them and
materialized the entire lane. The standalone sealed-window helper had the same
sentinel mistake. A cursor inside the sealed prefix also exposed an unsigned
reverse-range subtraction when the fast path became reachable. The live-tail
materializer stopped after one index page, stranding ranges beyond 1,000 rows.

Decision. Full-history selection starts at zero; positive windows keep their
bounded suffix. A cursor wholly inside the sealed prefix never asks for a live
tail. The response moves owned messages after clipping instead of cloning every
message. Live-tail reconstruction advances explicit index-page cursors, retains
only one page plus the selected message vector, and rejects reversed ranges or
reads beyond the existing 10,000-row full-read budget. Archive selection retains
that full-read ceiling, final response-byte admission and deadline errors. Page
digests, tenant/owner keys, lane-count fences, source pinning and mutation
invalidation remain the read authority. No cache, thread or durable format is
added; memory is bounded by the existing page and response limits.

Alternatives. Keeping the per-document fallback for every full read discards the
archive's purpose. Ignoring the unsealed tail loses acknowledged messages.
Cloning the selected archive messages creates an avoidable second object tree.
Reading an unbounded whole lane for a small tail consumes memory proportional to
history. The exact fallback remains the comparison oracle after archive
invalidation; tests cover sealed-only, partial sealing, clipped cursors, empty
intervals, expired deadlines and index-page boundaries. Performance evidence is
recorded separately from those correctness claims.

The source-pinned retest
retains eight default-off legs and 36,120 exact response checks. Hydration mean
8.290 → 8.292 ms does not establish a speedup; P50 is about 1% worse. The
remaining application-path attribution and archive visibility are unproven.
Native 1,577 tests pass, two ignored; correctness does not certify SQLite parity.

## Shared source bytes and decoded archive messages

Problem. The bounded byte reader built a complete `Value` only to validate it, then discarded it. Archive page hydration parsed those same bytes again. Decision. Byte consumers use the existing streaming response validator, whose acceptance matches `Value`, including recursion and reserved Serde markers. Consumers needing both decoded content and source digests receive one owned JSON tree with its original bytes through `materialize_stored_document_value_and_bytes_bounded`. Archive hydration moves the array, checks its original source type, and retains every digest, byte, message-count and object-shape fence. Scope and Blob verification still precede JSON interpretation; the original byte bound and error mapping remain. Alternatives. Re-encoding the decoded tree before hashing loses the identity of noncanonical source bytes. Accepting any decoded array permits a reserved raw marker object that the original typed array reader rejected. Skipping validation or caching unverified source bytes weakens corruption detection. These are rejected. No cache, thread, queue, stored format or diagnostic slot is added.

Measured in `tofudb_single_parse_archive_20261006.json`: 16 reversed-order legs, 72,240 exact responses; mean window read -9.5%, full hydration -17.4%. Sidebar unchanged; no RSS/space win or SQLite-floor victory. Native 1,579 passed/two ignored; 39 direct original application differential cases passed. Standard pytest collection is blocked by host lxml dependencies.

Rejected exact tree-length counting: 16 legs and 72,240 exact responses proved parity but no speed benefit. Native 1,580 passed/two ignored. Decision: retain Serde encoding; direct counting adds another tree walk. Source-byte counting cannot preserve the encoded-byte bound for noncanonical sources.

## Archive catalog cursor coverage

Problem: a lookahead row became the cursor before truncation, so each ninth conversation was skipped. Decision: resume after the last returned row. Increasing batch size or idle delays cannot repair excluded rows; foreground sealing violates worker ownership.
`tofudb_archive_cursor_20261006.json`: 50/50 diagnostic pages versus 45/50 before; combined native 1,581 passed/two ignored, archive 17 and application 39 passed. Historical pairing checked 72,240 exact responses on the earlier source. Bulk latency essentially unchanged; no causal speed claim. Existing page, worker and publication bounds remain; previously omitted reconstructible pages are now produced.

## Private Turn object ownership

Problem: scoped Turn reads validated, parsed and cloned one JSON tree; legacy-message conversion cloned its private projection and metadata again.
Decision: consume the existing bounded scoped decoded-value reader, move its object into reconstruction, and move private fields into legacy messages. Byte-only decoding also moves its parsed map. Owner/namespace/Blob source, grammar/depth, projection chain, execution-epoch, deadline, fallback and original encoded-byte budgets remain mandatory. No resident cache, worker or format is added.
Alternatives: skipping verification weakens integrity; a parsed cache adds invalidation and memory lifecycles; exact tree-length counting previously failed to improve timing. Keep the existing Serde budget encoding.

`tofudb_turn_owned_materialization_20261006.json`: full native 1,587 passed/two existing ignores, 38 process and 39 original application differential cases passed; 12 complete paired readbacks match byte-for-byte. Default-off small ABBA window/bulk means improve ~16%/~20%; medium ~15%/~6.6%. Adverse paired sidebar and matrix concurrent/swarm means remain visible. Two candidate 1000-active stream P99s ~972/980 ms pass latency, with narrow margins and uncovered process-crash/provider/browser/soak gates. Raw embedded SQLite still leads warm history/hydration; no all-scene, stable capacity or footprint victory.

## Snapshot response construction

Problem: response construction copied private Turn/settings trees before encoding. The earlier borrowed encoder removed those copies. The current producer moves the private trees into one complete Value and retains its immutable canonical/depth evidence through the selected IR result; normalization, ownership, byte, deadline and fallback validation fences remain. The complete decision and alternatives live in [snapshot producer evidence](TOFUDB_EMBEDDED_RESPONSE.md#complete-snapshot-value-evidence). `tofudb_snapshot_response_construction_20261006.json`: 1,589 native/two existing ignores, 38 process and 39 original application cases pass. Exact paired full readbacks match; small window mean ~14–16% lower, medium ~18%. Sidebar/bulk/append/reopen adverse legs are retained. Eight 1000-active runs have zero errors and exact content/events, but several new and old runs exceed1s; causal capacity attribution and stable capacity remain unproven. Raw SQLite floors still lead; no all-scene or footprint victory.

## Event projection byte witness

Problem: full event evidence cloned/re-encoded an unchanged projection already measured for its size fence; storage cloned its private Turn solely for serialization. Decision: freeze the final projection after terminal trace merge, reuse actual encoded length, and serialize the existing Turn map. Text-head and legacy compact-slim evidence remain distinct; continuation, source/owner, byte, replay and durability fences stay. Alternatives: compact terminal evidence loses final fields; a fresh counting traversal retains work; skipping validation weakens integrity. `tofudb_event_projection_witness_20261006.json`: combined 1,596 native/two existing ignores, 38 process and 39 original application cases pass. Two concurrent recovery sources are preserved. Eight 1000-active cohorts have exact content/events and zero errors, but adverse stream/terminal/append legs remain; final stream ~958/969 ms and terminal ~2977/3987 ms do not certify stable/causal whole-engine gains or SQLite superiority. No resident cache, worker or format added.

## Authority-owned parallel Blob compression

Problem: the shared Zstd encoder held one mutex throughout compression and frame construction. Independent private preparations could therefore stall the authority writer. A 1,000-conversation diagnostic observed 5,239.77 ms of aggregate codec wait against 594.87 ms of aggregate compression; these overlapping request spans are attribution evidence, not a throughput ratio.

Decision: `blob_codec.rs` leases reusable level-3 contexts from authority-owned metadata. Compression runs outside the pool mutex. Existing chunk framing, checksums, size bounds, verification, publication and durability remain mandatory. A panic poisons the encoder and later writes fail closed. Contexts have no thread-local lifetime. Leases release on unwind; idle contexts disappear with the last authority/observer owner.

The existing launch probe sets the maximum retained context count to effective CPUs and retained Zstd workspace bytes to memory headroom / 128. Missing observations fall back to one context and 8 MiB. These bounds reclaim idle workspace; they do not limit active legal preparation. `blobCodecPool` exposes created/reused/active/peak/idle contexts, exact Zstd-reported idle workspace bytes, both idle budgets and poison state without taking the authority lock. Active workspace and allocator metadata are explicitly outside this byte measurement. Operation aggregates expose `kind=aggregate`, request/error counts and no fabricated request sequence or outcome; slow samples retain their real sequence/outcome. Codec wait measures pool acquisition, while compression excludes context creation and frame hashing (which remain within the outer Blob encode span). The opt-in request profiler distinguishes `blob_codec_wait` from `blob_codec_compress`; two extra stage slots add 48 bytes per sampled request/aggregate. Sampling remains off by default.

Alternatives: more preparation workers retain the shared serialization point; per-chunk fresh contexts repeat initialization; thread-local caches outlive authority teardown and evade resource accounting. The selected pool adds no worker, durable format, backend or admission queue.

Verification and adverse performance legs are retained in `tofudb_parallel_blob_codec_20261013.json`. The retained pipeline comparisons enabled client stage sampling; their latency is diagnostic, not unprofiled admission. Native full-sampling runs diagnose stages separately. Passing correctness does not certify every performance metric or the whole application.

The client-profiled A-B-B-A window has stream P99 6,670/5,643/5,583/6,055 ms (~11.8% lower candidate mean), all above the 1,000 ms gate. Settle P99 10,189/9,858/9,718/9,718 ms has no consistent paired win and all exceed 5,000 ms. All four preserve 1,000 exact contents/replays, 5,000 ordered frames and zero errors across two SIGKILL boundaries. Candidate terminal snapshots retain 41/37 contexts (~22.1/20.1 MB Zstd workspace); actual measured values in the evidence take precedence over rounded prose. B1 client peak RSS rises, and both candidate elapsed times are worse. Full-sampling wait drops to single-digit aggregate milliseconds, but diagnostic operation counts/lifetimes differ; no ratio is claimed. Remaining FIFO and replay queues require separate evidence-led work. Full native tests: 1,832 pass, eight existing ignores; final codec neighbors: 71 pass.

Final build `a1f7c03a…` was measured separately after extending the lease through frame construction (the original panic-poison boundary) and correcting aggregate labels. Final client-profiled A-B-B-A stream P99 is 5,997/5,824/5,922/6,409 ms (~5.3% lower candidate mean); settle is 9,922/9,475/9,706/10,022 ms. Every leg remains above both latency targets while preserving all 1,000 exact content/replay records and 5,000 ordered frames across both SIGKILL boundaries. Seven final profiler cases and a real 64-conversation full-sampling diagnostic verify corrected aggregate fields; its latency is excluded from formal admission. Complete original reports, all observer metrics and every slow-sample stage are retained in the checksum-linked `.json.gz` archive. These windows support removing shared codec serialization, not a claim of complete performance optimization or stable P99 certification.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

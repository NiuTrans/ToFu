# Request-scope native image preparation

Problem: cold immutable archive admission follows context preparation on the
first native task. Decision: trusted NativeRootConfiguration(image_preparation=True)
queues existing NativeArchiveImage admission on its owning serving loop after
principal/route/repository/pricing checks. Ordinary ROOT only; Flow containers
and already-cancelled tasks retain lazy acquisition. No application startup or
idle work is added. The shared image still seals/checks its pin once, borrowers
await the exact task, and failure stays sticky without fallback or retry.
Shutdown withdraws queued preparation, joins started archive/self-check work and
closes its image before dependency teardown. No model request, account reservation
or billable effect is admitted by preparation; no new pool/queue/worker is added.
Alternatives: bypassing archive verification weakens integrity; prewarming at
application startup taxes unused capability; detached loading loses shutdown
ownership. The option remains false by default pending complete performance
admission. NativeArchiveImage.prepare uses the same loop and existing acquisition
as duplicate; the host retains no task, principal or credential in the cache.
The application benchmark accepts --prepare-native-image and records both the
requested policy and before/after first-operation witnesses. Timing collectors
refuse mixed policy, missing witness or pre-clock admission. Diagnostic spans
remain non-admitting. Twelve alternating cold SQLite ROOT pairs measured median
768.130 ms prepared versus 809.236 ms lazy (0.94920); the prepared maximum was
1,561.710 ms versus 1,084.701 ms and mean CPU 711.296 versus 687.540 ms. All clocks
remain in evidence. This small cohort cannot qualify P99/defaults; the increased
maximum/CPU prevents inferring stable performance from its median improvement.
The cache remains one existing bounded sealed image; this change moves its
acquisition earlier within requested work, without additional persistent growth.
Verification: shared-image/host cancellation and close tests, application owner
preparation/rollback tests, benchmark witness guards and real ROOT requests.

Archive decoding bounds apply before tar metadata admission. Problem: Python's
compressed tar stream expands an input chunk without an output limit before
checking a member header; a high-ratio input can allocate ahead of its declared
member budget. Decision: an internal reader uses zlib's gzip mode with an
explicit decoded-output limit, 64 KiB call-local chunks and a total derived
from existing member/extended-metadata/header/padding/final-record bounds.
It completes the gzip trailer before executable self checks, validating CRC,
size and truncation; rejected reader state remains sticky so a retry cannot
turn a zero remaining limit into unlimited decompression. Pin, immutable
snapshot, exact manifest/member digests, ELF/target/version and sealed-image
checks remain mandatory. Alternatives: larger unrestricted compressed reads
reduce call counts but enlarge expansion; disabling checks weakens admission;
public buffered gzip readers also measured slower and duplicate buffering.
This changes no durable data, resident worker, pool or persistent cache.
Decoded scratch is bounded per accepted loader call and discarded on failure/
completion; malformed-stream tests prove refusal before code execution and
continued refusal without further expansion. Existing full archives, shared
descriptors and cancellation/close behavior remain covered. Diagnostic costs
and rejected reader variants remain in the archive evidence; stronger bounds
do not imply system speed, P99 or default Rust admission.

A final alternating codec diagnostic measured 70.363 ms bounded versus
61.402 ms prior (1.14595). Eight complete native HTTP cold pairs measured
998.979 ms versus 1,098.579 ms (0.90934); common scheduling/storage variation
means the smaller full-request sample does not establish codec speed or P99
admission. All raw clocks, earlier rejected reader variants and the invalid
first macro probe remain recorded; no default-selection verdict is granted.


Direct pinned executable admission also opens its source with `O_NONBLOCK` and
checks the resulting descriptor with `fstat` before reading. Problem: a FIFO
substituted at the configured binary path could block acquisition indefinitely;
joined cancellation could then never finish. Decision: accept only regular
files within the declared byte budget, retaining the streaming growth limit,
exact digest and sealed immutable execution image. Alternatives: checking the
path before opening races replacement; cancelling the await abandons physical
read ownership. Path replacement after open cannot substitute different bytes.
Source and image descriptors close on every refused admission. No cache, worker
or retained data is added. Verify the real no-writer FIFO in a disposable joined
process, path replacement, byte-budget/digest failures and normal sealed-image
lifetime in `tests/agent_core/test_native_executable_source.py`.


Installed executable admission separates one-time distribution validation from
per-application execution validation. Problem: a ROOT request needs the binary,
but archive admission also reads and hashes optional libraries, contracts and
licenses. Decision: `python -m scripts.build_native_runtime --check ARCHIVE
--install-executable /absolute/new/path --archive-sha256 PIN
--application-version VERSION` verifies the complete pinned archive and all
runtime self checks, then publishes only its sealed executable bytes via an
exclusive, fsynced file. It refuses replacement; retain the archive for licenses,
contracts and optional libraries. Its JSON receipt distinguishes archive and
executable digests. Publication does not activate any backend or alter defaults.

Trusted application composition may create `NativeExecutableImage(binary=...,
sha256=receipt_executable_digest, application_version=..., budget=...)` and pass
it to `NativeHost`/`NativeRootConfiguration`. The supplied digest must come from
a trusted installation, not be learned from the current executable path. First
use opens a bounded regular file, copies and hashes its exact bytes into sealed
memory, verifies application/protocol/host identity and runs both runtime gates.
Every process executes that immutable descriptor. Both image kinds share one
loop-owned acquisition, sticky failure, per-borrower byte checks and joined
close. Flow still explicitly requires an archive. A separately pinned pure token
library may be supplied as described below. SDK/archive entry points remain valid.
Alternatives: extracting and executing mutable paths permits replacement races;
skipping the whole archive check at installation loses release validation;
startup prewarming taxes unused capability. No extra pool, queue or persistent
cache is introduced. The installed file is an explicit operator-owned release
artifact; replacement and reclamation stay outside request execution.

Verification: installed-image and shared-image lifecycle tests, full archive
publication/refusal/fault tests, and ordinary durable HTTP text/tool execution.
The HTTP benchmark records the selected image source and executable digest;
source/pin mixtures refuse timing comparison. File admission remains inside
the first-operation clock. Installation is reported separately. The independent memory collector also accepts `--installed-executable` only
for the application entry and freezes the source/digest across workers. Existing
archive memory evidence cannot qualify the installed variant, and default migration
still requires complete timing, memory and backend coverage.

Opt-in stage diagnostics name installed acquisition `native.executable_admission`
and keep its three `native.identity_check` spans. Nested spans overlap and never
supply timing admission; an installed request does not claim archive acquisition.


Pure token-library installation uses the same checked archive authority:
`--check ARCHIVE --install-token-library /absolute/new/path --archive-sha256 PIN
--application-version VERSION`. It verifies the complete archive and token ABI
contract, then exclusively publishes sealed library bytes and returns their digest.
Installation does not load library code. `NativeExecutableImage` accepts the
optional `token_library` and `token_library_sha256` together; enabling token counts
without this pair fails. First use independently seals bounded regular bytes,
checks their digest, loads the pure ABI and verifies embedded vocabulary identity.
The pure library has its own ABI version; application version is checked at
archive installation. This capability grants no Flow authority.

Problem: re-reading the full archive for a pure count repeats distribution work.
Decision: reuse the installer, sealing and joined counter lifecycle for an explicit
library pin. Alternatives: reading a mutable library directly permits replacement
races; learning its digest at load time does not establish installation identity.
No extra cache, worker pool or startup prewarming is introduced.

Application timing, memory and diagnostic collectors accept
`--installed-token-library` with `--installed-executable`. Every worker records
both pins and witnesses lazy first-operation counter loading, absence of Python
encoder initialization in the native arm, and joined library release. Evidence
without that selection cannot qualify the variant. Diagnostic spans include an
explicit operation-clock interval; setup and readback outside it are not request
latency. Overlapping spans must not be summed.


`system_native_installed_token_20261014.json`
records the corrected initial invocation and tool-worker scopes against source
`88e3b4cb83fbd6512f0e385c33a2181375c83269`. The fixed cohort has 212 independent
workers and 1,652 operations: three warm pairs (100 samples plus 20 warmups),
100 cold pairs and three independent stopped-memory pairs. All native workers
load the selected library lazily, leave Python encoders uninitialized and join
library release. Provider work and durable outcomes agree between arms.

For this disposable SQLite, priced/count-limited text fixture with installed
images, worker reuse and worker-future completion, native/Python warm median
ratios are 0.695–0.707 and CPU ratios 0.891–0.914. Cold median is 615 ms versus
775 ms; mean CPU is 502 ms versus 605 ms. Cold P95/P99 ratios are 0.680/0.342;
the Python arm contains large outliers, all retained, so the P99 ratio is a
cohort observation rather than a general tail-latency guarantee. Independent
unique-VM RSS ratios are 0.882–0.891 and PSS ratios 0.875–0.886. Combined timing
and memory qualify this component. The installed library is 6,999,848 bytes;
its explicit one-time publication and lazy host lifetime add no worker pool.

The earlier installed-image cold-P99 rejection, archive CPU rejection and the
partial cohort stopped for inherited-scope correction remain recorded. Twelve
HTTP text/tool cases pass over SQLite, native daemon and native embedded; these
functional checks do not certify other backends' performance. Other workload,
concurrency and domain gates remain required before broad default migration.


Application collectors support `--backend daemon` and `--backend embedded` with
an explicit `--storage-engine /absolute/tofu-db`. Both require current-source
binary identity; embedded additionally validates the published library pair and
freezes `libtofu_db.so`'s digest. Each worker checks health and the ready runtime's
actual backend/transport/PID before and after the workload, then witnesses the
runtime stopped with no child before accepting cleanup. New receipts missing
these observations fail validation. Historical receipts retain their original,
weaker observation scope and cannot qualify a new backend by relabeling it.

Problem: a requested native backend alone does not prove which transport ran.
Decision: reuse the storage runtime's public status and deployment verification,
keep daemon/embedded cohorts separate, and freeze both embedded artifacts.
Alternatives: auto transport selection can silently change the measured boundary;
a daemon binary pin alone leaves embedded code unidentified. Startup/identity
checks stay outside request clocks; live storage selection is never changed.

`system_native_backend_application_20261014.json`
retains separate daemon and embedded cohorts at source `c0e598f847dd9f626a6154360cbee5cc3270bde8`,
each with 212 independent workers and 1,652 operations. Both use the same installed
executable/token-library selection and current-source native storage pair. Cold
median/mean-CPU ratios are 0.848/0.829 for daemon and 0.872/0.834 for embedded;
cold P95/P99 also pass. Separate unique-VM RSS ratios are 0.847–0.900 and
0.877–0.911 respectively, with joined runtime and token-library cleanup.
Warm admission fails: daemon's third P95 ratio is 1.076; embedded has five
median/P95/P99 regressions, including median ratios 1.055 and 1.080. Neither
backend qualifies this candidate or permits default selection.

Subsequent 20-operation warm diagnostics retain only spans inside each request
clock. Embedded native/Python means are 105.8/88.9 ms in those instrumented,
non-alternating samples. Native context preparation and terminal persistence
remain application callbacks; overlapping stage times identify investigation
boundaries, not additive costs or a proven regression cause. Setup/readback
and instrumented clocks cannot replace the uninstrumented admission cohort.

Shared schema preflight diagnostics
compare the exact old/new validation functions over the same frozen application
and native embedded engine. The 1,200-case schema differential preserves results,
identity, diagnostics, exceptions and source immutability. Text requests agree
over SQLite, daemon and embedded. Four diagnostic workers execute 26 operations
each; the last 20 native context spans average 8.801 ms before and 8.203 ms after,
while whole-request means are 98.040/98.570 ms. This does not establish an
end-to-end native gain; the prior backend warm rejections remain in force.
No cache, pool, persistent state or default selection is added by this reduction
of repeated validation inside one call.

Worker invocation input retains the exact bytes produced by the client's
bounded object encoder. Problem: `_Input.write` decoded and encoded these
same payloads again just to add the worker owner/sequence envelope. Decision:
an immutable bytes subtype, minted only after structure, UTF-8, finite-number
and exact-size admission, permits envelope construction without re-encoding the
inner JSON. The subtype carries no owner/task authority or mutable metadata;
first-frame owner/type is checked from the actual bytes. Each receiving view
still enforces its original inner-frame limit. Ordinary bytes keep their full
parse/encode path. Failed input admission or a failed write leaves the initial
frame unconsumed; closed views and joined readiness retain their prior fences.
Alternatives rejected: splicing arbitrary bytes changes NaN/Unicode rejection;
retaining mutable input objects permits post-admission changes. This adds no
cache, process, queue or persistent state and does not admit default selection.

`system_native_worker_frames_20261014.json`
retains twelve fresh Python/native warm workers (1,452 operations) and six
separate before/after native workers (726 operations), each with 100 measured
samples after 20 warmups and a recorded first operation. The direct native
change has median ratios 0.972–0.988, P95 0.922–0.944, P99 1.009–1.046 and
CPU 0.986–0.993. This local improvement does not qualify the full native path:
embedded Python/native P95 ratios are 1.260–1.307 and daemon 1.094–1.181.
Both fixed warm cohorts reject; cold and independent memory are therefore not
collected for this candidate, and prior full-cohort failures remain active.
Twelve installed text/mixed-tool application cases pass across SQLite, daemon
and embedded, with real reuse, callback failure and Stop/close checks. Published
wheel/sdist contain the exact changed runtime modules; isolated wheel framing
also passes. No whole-project or default-selection approval follows.

Terminal projection captures the immutable task identity rather than its owning
terminal policy. Problem: the policy's callback referenced the policy itself,
retaining completed invocation/context owners until cyclic garbage collection.
Decision: preserve the same constructor-time identity and all terminal checks
without that back-reference. Alternatives rejected: disabling or freezing GC
hides retained state; clearing projection callbacks before joined completion
would compromise terminal retries. `test_native_completion.py` verifies prompt
release without GC together with retry, post-commit and cancellation behavior.
This lifecycle correction alone grants no latency or default-selection verdict.

`system_native_terminal_lifetime_20261014.json`
retains GC-only diagnostics and a separate cyclic-object census. The diagnostic
warm cohorts overlap four Python and six native full collections with request
clocks; those pauses span roughly 56–102 ms. The corrected callback's completed
invocation/context owners disappear from the cyclic-object census. Six separate
uninstrumented native workers retain all 726 operations for three alternating
before/after pairs: median ratios 0.979–0.995, P95 0.982–1.022, P99 0.924–0.996,
and CPU 0.959–1.006. These pass the local 5% nonregression bound but do not
resolve prior Python/native backend failures. No cold or memory qualification
is claimed. The release test fails before the fix; 153 direct/neighbor tests and
16 installed HTTP/Stop cases pass afterward. Wheel/sdist retain the exact changed
runtime source and pass membership checks. Whole-project/default admission
remains false.

Joined conversation completion retires its compiled tool catalogs, contract
maps and search text after post-commit observers capture their inputs. Problem:
the terminal hot registry kept these execution-only graphs on both task and
config until eviction, increasing residency and cyclic-GC scans. Decision:
replace those containers and shallow-copy config; borrowed context and detached
child-policy snapshots remain intact. Empty catalogs retain explicit denial;
absent/None catalogs that can select fallback inventory are rejected alternatives.
Early persistence, pending durability, active tasks and transient/inline owners
retain their tool state. Events, Turn/results and public user configuration stay
under their existing retention owners. The existing coalesced heap-trim owner
receives reclamation hints; no new timer, cache, pool or GC policy is introduced.

`system_terminal_tool_retention_20261014.json`
retains twelve fresh Python/native warm workers across embedded and daemon
(1,452 operations). Embedded P95 ratios 1.133–1.273 and two daemon ratios
1.141/1.190 still reject; cold and language-comparison memory remain uncollected.
Separate native before/after cohorts each use six fresh workers (726 operations):
uninstrumented timing has median ratios 0.994–1.010, P95 0.950–1.013, P99
0.822–0.869 and CPU 0.994–1.015. Independent stopped, kernel-confirmed unique-VM
memory gives RSS ratios 0.905–0.945 and PSS 0.901–0.946. These local checks pass
the 5% nonregression bound; they do not replace the failed language gates.
The structural diagnostic retains 64 terminal tasks: per-field config charges
fall from 41.3 MB to 1.30 MB while event replay remains. These charges include
aliases and are not RSS. Verification covers 13 release cases, 330 neighboring
contracts, 16 installed HTTP/Stop cases, 88 sampler checks and eight installed
priced-tree Continue/checkpoint cases. Four journal cases remain capability
skips for authorities without the native checkpoint control port. Wheel/sdist
contain both exact changed runtime modules. Whole-project/default admission
remains false.

Native gateway permission admission validates and serializes the complete
catalog without decoding a schema graph used only to extract permissions.
The public compiler still returns detached JSON-normalized containers. Problem:
constructor admission discarded that full decoded graph, while text-only
settlement rebuilt execution documents for an empty tool declaration. Decision:
prepare those documents only at the first declared, permitted tool occurrence;
empty/unknown-call rounds still parse/publish their declaration, retire previous
prepared state and enforce pending-approval exclusion. Alternatives rejected:
skipping catalog validation changes finite-number/cycle and metadata refusal;
returning early on text leaves old declaration state; a shared mutable catalog
cache adds alias/authority and lifetime risks. No cache or persistent owner is
added. Verify catalog materialization, gateway, prepared/context and installed
text/mixed-tool contracts before claiming performance admission.

`system_native_catalog_materialization_20261014.json`
uses freshly built current-source runtime/token/Flow artifacts and a matching
private daemon/embedded engine after checkpoint-frontier source changes. The
1,200-case compiler oracle preserves 400 outputs and 800 exact refusals. Same-
source diagnostics reduce tracked JSON containers per native request from
6,572 to 5,261 (Python: 708); added diagnostic traversal cannot admit timing.
Six separate native before/after workers (726 operations) give median ratios
0.955–0.966, P95 0.937–0.965, P99 0.933–0.974 and CPU 0.955–0.970, passing
the fixed local nonregression bound. Twelve Python/native warm workers (1,452
operations) still reject P95: embedded 1.108–1.228, daemon 1.112–1.189. Cold
and independent language-comparison memory are therefore not collected for this
candidate. Verification includes 30 direct and 125 neighboring tests, five
actual worker/Stop tests, 20 installed application/recovery passes and four
journal capability skips; wheel/sdist contain the exact gateway source. Prior
failures remain active; no whole-project or default-selection verdict follows.


Native catalog export owns its decoded input graph before projection. Problem:
export decoded the immutable prepared snapshot, projected it, then decoded a
second complete graph solely to establish isolation that the first decode had
already provided. Decision: retain full projection and JSON encoding validation,
return the projection over the privately decoded snapshot, and keep public
compiler exports independently normalized. Each gateway export still reads the
current permission map and creates independent nested containers. Alternatives
rejected: retaining a decoded catalog would add resident state and permission
invalidation; borrowing caller definitions would break mutation isolation;
skipping final encoding would admit non-finite values. No cache, shared schema,
worker or GC policy is added. Verify nested JSON normalization, permission
changes/refusal, repeated export isolation and actual application execution.


`system_native_catalog_export_20261014.json`
checks 1,200 compiler inputs and 1,200 gateway exports against the previous
implementation, including exact failures and independent nested outputs.
Diagnostics reduce JSON-decoded tracked containers from 5,261 to 4,761 per
native request (Python: 708). Six independent native before/after workers
(726 operations) pass the local nonregression bounds: median ratios 0.971–0.997,
CPU 0.969–0.995 and P99 0.937–0.955; P95 is 1.001–1.026, so no P95 gain is
claimed. Twelve separate Python/native warm workers (1,452 operations) reject
admission: embedded P95 is 1.116–1.460, with first-pair P99 1.303; daemon P95
is 1.035–1.220. No cold or language-comparison memory continuation is collected.
The same source-verified runtime/token/Flow and database artifacts serve both
arms; Rust sources are unchanged. Verification includes 32 direct, 125 neighbor
and 20 installed application/recovery passes, four journal capability skips,
and byte-exact gateway source in wheel/sdist. Prior rejections remain recorded;
whole-project and default selection admission are still false.


Result-declaration serialization shares the tool contract's finite-JSON/32 KiB
boundary and avoids normalized containers used only for admission or description
text; clone consumers keep detached outputs and non-string keys keep canonical
normalization. The decision and fallback semantics belong to the
[tool gateway contract](contracts/TOOL_SEARCH_EXECUTION_GATEWAY.md).
`system_native_result_schema_20261014.json`
preserves 2,400 exact clone/render outputs or refusals, 59 initial direct passes,
13 final serializer passes, 60 native neighbors and 184 broader passes. Four
broader failures are inherited: three inventory cases and the nonstream receipt
bytes/mapping mismatch, separately reproduced on the previous frozen source.
Twenty installed application/recovery cases pass, four journal cases lack the
selected authority capability, and wheel/sdist contain all three exact modules.

Same-source diagnostics reduce native JSON decodes from 80 to 62 and tracked
containers from 4,761 to 4,179 per request; the current Python arm has 638.
Six native before/after workers (726 operations) pass local bounds: median ratios
0.985–0.991, CPU 0.975–0.981, P95 0.983–1.026 and P99 0.913–1.003. Six additional
Python before/after workers pass those bounds, with median ratios 0.872–1.014,
CPU 0.923–1.008, P95 0.805–1.007 and P99 0.922–1.006; that variability does not
establish uniform gains. Twelve independent language-comparison warm workers
still reject admission: embedded P95 0.994–1.152 and daemon P95 0.948–1.229,
with daemon first-pair P99 1.307. No cold/memory continuation is collected.
Identical source-verified runtime/token/Flow and storage artifacts are reused;
there is no new resident cache, worker, GC setting or default selection. Prior
failed observations remain active; whole-project/default admission is false.


Native context binding retains only the catalog data its selected observer
consumes. Problem: the built-in visibility projection repeatedly decoded every
argument schema even though it only checked names, and completion decoded the
original tools before replacing them with the final wire projection. Decision:
prepared application execution supplies the callable `NativeToolProjection`;
that exact built-in type binds a frozen name-membership map after validating the
complete catalog's JSON representation. Generic callbacks and subclasses retain
full detached schemas. Rust configuration and the execution gateway keep their
complete argument and permission authority. Context metadata/output and tools
use separate immutable JSON snapshots, so completion decodes only its selected
tool surface. Explicit context tools also avoid constructing a discarded default
projection. Alternatives rejected: removing full validation changes refusal;
pruning generic callbacks changes their contract; sharing mutable schema objects
breaks round isolation. No cache, worker, protocol, GC policy or default selection
is added. Verify name/hidden/foreign membership, mutation isolation, generic and
subclass projection, malformed catalogs, completion, cancellation and installed
application/recovery before using performance measurements.


`system_native_context_binding_20261014.json`
uses newly built runtime/token/Flow and matching database artifacts after the
application pending-tool checkpoint source change. Checks include 53 direct,
128 neighbor, 31 route, five installed worker/Stop and 20 installed application/
recovery passes; four journal cases lack selected-authority capability. All 480
old/new context observations preserve outputs or exact failures. Wheel/sdist
contain the three exact changed modules. Diagnostics reduce native JSON-decoded
tracked containers from 4,179 to 3,464 per request (current Python: 638).

Six native timing workers (726 operations) pass local bounds: median ratios
0.960–1.005, CPU 0.945–0.999, P95 0.958–1.008 and P99 0.951–1.041. Six separate
stopped-tree memory workers pass local bounds with RSS ratios 0.953–1.027 and
PSS 0.952–1.028; no uniform memory or latency gain is claimed. Twelve separate
Python/native warm workers still reject admission: embedded P95 1.064–1.158,
first-pair P99 1.358; daemon P95 0.869–1.375, second-pair P99 1.362. Cold and
language-comparison memory continuation are not collected after warm rejection.
Earlier failures remain active; whole-project/default admission is false.

Separate GC-only diagnostics observe four Python versus seven native full
collections in 100 measured operations. The native seven slowest operations
intersect those collections, whose pauses are 46.7–63.2 ms. A separate stack
probe locates four full-collection triggers inside prepared execution-tool JSON
decoding, two inside gateway catalog export decoding and one at a worker frame.
A triggering allocation does not identify all retained garbage or prove every
uninstrumented outlier's cause. These diagnostic clocks cannot admit performance;
the evidence preserves their raw identities and source stacks separately.


The immutable prepared-tool snapshot experiment is rejected for runtime adoption.
Problem: prepared entry and gateway export repeatedly decode full tool schemas,
and the suspended prepared coroutine retains a detached directory. The tested
candidate retained serialized prepared input and an invocation-owned compiled
catalog, revalidated source identity and permissions, and cleared the additional
snapshot before effect cleanup. Its 2,400 baseline comparisons matched output,
serialized catalog bytes, exceptions, mutation behavior and cleanup; 68 focused,
155 neighboring, 5 installed worker/Stop and 20 actual application/recovery tests
passed (4 checkpoint capability skips). Both SDK formats contained exact sources.

Decision: retain the implementation in source commit
`cedbe33a9468ec23732c29afd2e18fa1803a0c2d` and its frozen evidence in
`system_native_tool_snapshot_20261014.json`; restore the preceding
runtime implementation. JSON-decoded GC-tracked containers fell from 3,464 to
3,281 per operation, but the three uninstrumented native before/after pairs
include a P95 ratio of 1.0915, above the 1.05 nonregression limit. The independent
unique-VM memory pairs include RSS/PSS ratios of 1.0655/1.0660, also failing.
Smaller allocation counts cannot establish tail-latency or memory improvement.

The separate unchanged Python/native comparison rejects embedded warm P95
(1.0787) and daemon warm P95/P99 (up to 1.3551/1.5229). Raw failed cohorts remain
in the evidence; cold and language-comparison memory continuation were not run
after warm rejection. Alternatives rejected: selecting only favorable pairs,
rerunning unchanged code for a passing cohort, relaxing the gate, or changing GC
behavior to suppress observed pauses. Production authority and default selection
are unchanged; whole-project/default admission remains false.

The canonical configuration-header experiment is rejected for runtime adoption.
Problem: the worker adapter re-materializes full messages and execution schemas
to check the initial type/owner after the bounded client has encoded them.
The candidate used an immutable bytes subtype only for exact string-keyed
configuration dictionaries with integer owners, wrote type/owner first, and read
actual immutable header bytes before comparing with the worker owner. Ordinary
bytes and extension/mixed-key inputs retained full parsing. Rust still validated
the complete configuration and its owner. Alternatives rejected: mutable owner
metadata, using an encoding marker as authorization, or changing frame budgets.

Decision: preserve source commit `f9f60ce89b8f6d290fdd1724f343e24825bb26b1`
and `system_native_config_frames_20261014.json`, and restore the
preceding runtime. All 2,400 old/new semantic, size, exception and refusal-state
comparisons matched; top-level header order was the intentional representation
change. The 91 focused, 71 neighboring, 5 installed worker/Stop, 20 application
and 16 actual ROOT process-death/recovery tests passed. Four application tests
skip missing storage capabilities. Two existing architecture-boundary failures
remain; their offending files, rule manifest and test equal the source baseline.
Both SDK distributions contained exact changed sources and no leaked members.

JSON-decoded GC-tracked containers fell from 3,464 to 2,953 per operation and
one full decode disappeared, but two native before/after P95 ratios were 1.2052
and 1.2466, above the original 1.05 limit. The third was 0.8969. Median and CPU
ratios alone cannot override these failures. Independent unique-VM memory pairs
passed (RSS 0.9726–1.0277, PSS 0.9713–1.0288). The failed fixed cohorts remain
recorded; no GC change, default switch or whole-project admission is made.

The separate unchanged Python/native comparison rejects embedded warm P95
(up to 1.0924) and daemon warm P95 (1.0595); cold and language-comparison memory
continuations were not collected after warm rejection. A separate instrumented
cohort observes four Python versus six native full GC collections during the
100 measured operations. These diagnostics are not latency admission and do not
establish the cause of each uninstrumented outlier. No unchanged cohort is rerun
or substituted for a failed one.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

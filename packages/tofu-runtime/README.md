# Tofu native runtime

This is the native runtime foundation for the Rust-primary migration. The root
Cargo workspace builds `tofu-runtime` and the `tofu-native` executable without
Python or an extension-loader boundary. Production agent/provider/HTTP services
are still being migrated; `check-runtime` certifies native job ownership only.

```sh
cargo test --workspace
cargo run --release -p tofu-runtime -- check-runtime
cargo run --release -p tofu-runtime -- check-session
python3 scripts/gen_rust_execution_contract.py --check
```

`executor.rs` owns bounded job/byte admission, startup-before-publication,
call-local result channels, panic containment and joined shutdown. Composition
supplies an explicit `Budget`; job producers declare their captured-payload byte
weight. That accounting excludes worker stacks and allocator overhead, which
require process RSS measurements. The check workload derives CPU concurrency
from `available_parallelism` and declares its small u64 payload weights.

Accepted jobs drain on close. A dropped receiver does not silently cancel work;
agent cancellation and durable settlement belong to the higher execution owner
when that layer is ported. No storage backend is selected by this crate.

Existing Tofu API/event/model contracts remain the domain authorities. Rust
services must implement them with explicit principal/owner context. The Tofu-DB
crate retains its own Cargo lock and certification boundary; root builds exclude
it until a reviewed public runtime dependency is introduced.

`session/` owns live execution identity, monotonic phases, cancellation/deadline
signals, resource binding rollback and exactly-once terminal invariant receipts.
Its registry is composition-owned and receives explicit owner IDs. Snapshot
fields exclude identity; native durations are typed, and remaining deadlines
are signed nanoseconds. Release callbacks run outside the state lock; concurrent
terminalizers await one receipt, while recursive settlement is refused. An
abandoned Rust session releases its resources as cancellation without writing a
durable task verdict. Provider/billing/admission adapters and task/event drivers
remain separate migration steps.

`execution_contract.rs` is derived by the retained generator from the current
Python operational owner, including phase order, terminal labels and closed
resource names. Native builds execute the checked-in Rust definitions without
Python. Six observed Python receipt fixtures guard native settlement semantics;
timing is excluded from those parity fixtures.

`task.rs` now drives an explicit execution callback and freezes one terminal
candidate after resource settlement. Optional durable persistence precedes
terminal event delivery; transient compositions skip storage. Persistence or
delivery failure retains retry debt without rerunning execution or cleanup.
Ports must implement idempotency for ambiguous acknowledgements, keyed by the
explicit owner/task identity. Cancellation drops successful result content and
uses the generated replay terminal vocabulary. Active execution refuses external
terminalization; cancellation is cooperative, not forced callback interruption.
These ports have no production provider, database or SSE adapter yet, so this
is a native execution boundary rather than production application cutover.
Verify `tests/task.rs` for ordering, ownership, deadlines and failure debt.

`sse.rs` frames provider bytes without Python: strict incremental UTF-8, BOM,
CR/LF, multiline data, control fields, bounded events and malformed-input recovery.
`feed_until` delivers issues before each event and stops at the exact consumer
cutoff without decoding its trailer; consumer panic retires the parser. Actual
EOF validates unfinished data while intentional close only discards buffers.
Composition supplies the event byte budget; the diagnostic bound derives from
the canonical Python framer. No background thread, cache or global parser state
is introduced. `tests/sse.rs` replays 315 actual Python fixtures at six chunk
sizes. Issue counts and event values match; native diagnostics retain typed kinds
rather than Python offset strings. Provider completion remains consumer-owned.

`examples/sse_measure.rs` isolates framing work. Three fresh-process alternating
pairs of 10,000 streams at 1/7/65,536-byte chunks measured native wall ratios
0.009/0.036/0.085 on the development host. This excludes HTTP, inference, task
composition, RSS and retained-memory admission and is not application speedup.

`provider.rs` composes SSE framing with Chat-compatible text/reasoning, indexed
tool deltas, raw provider usage and authoritative terminal evidence. The state
vocabulary and failure-priority classifier derive from `stream_result.py`;
`[DONE]` alone never proves completion. Composition supplies aggregate output
and tool budgets, which bound cumulative accepted payload rather than allocator
RSS. Failed parsing, EOF and provider errors cannot publish a successful task.

`tofu-native run-chat-stream OWNER TASK_ID OUTPUT_BYTES TOOL_CALLS` consumes
an already authorized Chat SSE attempt on stdin, settles a transient native
task and emits its terminal JSON on stdout, with nonzero exit on failure.
It requires no Python and composes the migrated owners rather than duplicating
a task driver. It is not an HTTP client or the full agent tool loop.

34 actual Python result fixtures at five chunk sizes certify the supported
Chat subset. The fixture exporter initializes absent usage to `{}` before
finalizing: the Python gateway-limit path currently otherwise raises on absent
usage. Native usage is raw provider usage, excluding billing normalization,
trace metadata and compatibility flag projection. Rich reasoning-details,
full caller reconciliation,
and Anthropic/Responses translations remain migration work;
unsupported shapes fail closed. Model-specific embedded thinking-tag parsing
and gateway error classification are not yet connected to this owner.

`http.rs` now owns an async reqwest/rustls client for one fixed endpoint and
explicit proxy route. Composition supplies pool/connect/header/idle budgets;
there is no ambient proxy, redirect, transport retry or request-wide generation
timeout. One semaphore permit spans the complete response and bounded terminal
drain; EOF drain failures discard the connection without revoking completion.
The monotonic session cancellation signal wakes active network waits directly.
The caller owns the Tokio runtime and resolves credentials/admission first.

`tofu-native run-http-chat CONFIG_BYTES_LIMIT` accepts an explicit attempt JSON
on stdin: `owner_user_id`, `task_id`, `endpoint`, optional `proxy`/`headers`,
canonical `body`, `event_bytes`, `output_bytes`, `tool_calls`, and a `policy`
object containing `connections`, `idle_connections`, `keepalive_ms`,
`connect_ms`, `request_headers_ms`, `pool_wait_ms`, nullable `stream_idle_ms`,
`drain_timeout_ms`, `drain_bytes`, `drain_chunks`. It executes HTTP → stream
result → transient task settlement → terminal JSON entirely in Rust.
This native CLI does not read application routing/credential stores or replace
the published service. Headers/upload currently share the request-header
timeout rather than the Python transport's independent write timeout. Typed
HTTP status is retained without provider error bodies; gateway cooldown/error
classification, custom enterprise trust roots, dynamic proxy policy, HTTP/2
interop certification, callbacks, durable adapters and full protocol/tool policy
remain pending. No end-to-end performance improvement is claimed.

`tool_policy.rs` now owns response-position flood admission and normalized caller
authority. Threshold defaults/ranges, caller bounds and the public flood error
projection derive from the retained Python authorities. Live program callers may
be provisional; execution/replay must require a resolved identity. Caller changes
within a native slot fail closed. Named retransmissions retain one argument body.
`ChatStream::with_tool_limit` and HTTP request `identical_tool_limit` preserve
explicit overrides; the stdin CLI accepts an optional final `IDENTICAL_LIMIT`.

Signature hashing preserves large integers, float/int distinctions, Python's
nonfinite JSON constants, sorted keys and last-wins duplicate keys without
retaining parameter trees. Arrays stream into one hash state; each retained
position/signature uses fixed digests. 2,400 actual Python observations and 26
caller cases guard revisions and occurrence identity, including the generated
error fields. A 10,000-observation unchanged position retains one position.
The current native signature parser rejects depth above 128, rather than granting
unchecked success; this is a migration limitation versus deeper Python input.
Tool schema/execution admission and full caller reconciliation remain pending. Signature equality is certified, not byte equality
with Python's serialized SHA input. Global arbitrary-precision JSON decoding was
rejected because its reserved marker keys can reinterpret ordinary objects; the
guard validates syntax and fingerprints numeric tokens without that feature.

Three fresh-process alternating pairs of 5,000 observations per profile measured
native/Python aggregate wall ratios 0.121/0.341/0.291 for short arguments,
1,000-integer arrays and long Unicode. The rejected per-array-element hashing
version regressed the array profile to 1.401; streaming array hashing removes
that repeated hash startup and child-vector allocation. Raw aggregates and source
identity are retained in `rust_tool_policy_20261009.json`. This
excludes CPU, tails, RSS and provider/task integration performance admission.

`tool_assembly.rs` now owns upstream-index aliases, unindexed continuation,
fresh-ID and name-based occurrence splitting, and retransmission suppression.
`ChatStream::with_tool_names` receives an immutable shared catalog; the HTTP
request carries names extracted once from its canonical tool body, including
namespaced functions. Prefix lookup uses the sorted catalog range instead of scanning every name.
Name joining follows the dispatched catalog; absent a
catalog it uses the canonical no-arguments-started fallback. Invalid indices/IDs
retain malformed evidence while recovering through the unindexed owner. Known
internal transport prefixes are filtered only after flood admission. Empty
provider IDs/names remain faithful diagnostic output for the later schema gate.
Opaque `extra_content` survives assembly and final projection.

160 actual Python result fixtures replayed at five network chunk sizes guard
assembly, output order, fragmentation and malformed counts. A 10,000-frame
retransmission check retains eight occurrences and one upstream-index alias.
Index strings currently accept bounded ASCII decimal text; non-ASCII numeric
glyph compatibility is still pending. Callers retain the native normalized
authority boundary rather than adopting inert metadata from the Python raw
accumulator. Full tool schema/execution admission, provider translations and
application cutover remain separate migration work.

`tool_contract.rs` now compiles request-owned ToolContractV2 documents once and
validates owned JSON arguments in place. It preserves the Python custom rules,
including required-null rejection before defaults, schema property error order,
Unicode character lengths, numeric kinds/bounds, Python enum equality and nested
defaults. Ordered JSON maps preserve declaration order; global arbitrary-precision
JSON decoding remains disabled. Validation codes, version, default retry action
and permission labels derive from the canonical Python contract owner.

`Catalog::admit` requires explicit owner, epoch, resolved caller and authorization.
Missing permission metadata cannot grant execution. The private admitted token
is consumed by a matching request registry, which rechecks authorization at
dispatch and passes explicit owner/epoch/caller to its handler. Denied/revoked
access, foreign scope, handler failures and panics cannot become successful
execution or automatic retries. Handler resources now settle through the per-occurrence native scope; this
registry does not create worker pools.
`tofu-native check-tool-gateway` exercises native validation/default denial and
one explicitly authorized fixture handler, without registering an application
built-in. 780 actual Python argument/result/error fixtures guard the validator.

This is the compiled contract stage, not the full ingestion pipeline: curated
name/argument repairs, contract clamping, call-ID normalization, tool result
projection, production handler adapters and full agent-loop wiring remain pending.
Malformed schemas fail at compilation, and depth above 128 is refused. Native
arguments use finite machine-range JSON; Python-only values and wider integer
decoding still require the shared native JSON boundary.

Three alternating fresh-process pairs of 5,000 validations measured native/Python
aggregate wall ratios 0.057/0.076/0.073 for small arguments, nested arrays and
wide objects. Native input cloning is included; schema compilation is excluded.
Source identity/raw aggregates live in `rust_tool_contract_20261009.json`.
This is not CPU/tail/RSS or application performance admission.

`tool_execution.rs` now wraps every registry dispatch in one tool occurrence
scope, reusing the native ExecutionSession resource owner. `dispatch_scoped`
accepts an explicitly configured identity/deadline and returns result/error,
terminal receipt and independent cancellation/deadline facts. The convenience
dispatch owns a request-registry-local identity and has no configured deadline.
Final authorization and handler lookup precede dispatch-start evidence.

Handlers receive a runtime view with checkpoints, cancellation wakeup,
heartbeat and exception-safe resource binding; they cannot settle that scope
themselves. Cancelled/overdue calls do not enter handlers. Active callbacks must
reach quiescence before cleanup and result adoption; cleanup failure suppresses
success. Failed/panicking callbacks still settle bound resources. Terminalization
freezes stop facts and refuses late rewrites or a second dispatch of the scope.
No detached worker, timer thread, progress store or durable task authority is
introduced. Timeout/cancellation remain cooperative; production process-group
kill/reap, output/progress/artifact adapters and
full handler/agent-loop migration are still pending.

`tests/tool_execution.rs` verifies live Stop with a joined callback thread,
combined stop/failure observations and 10,000 successive dispatches with 10,000
resource releases and zero active scopes. These are lifecycle counts, not CPU,
RSS or throughput admission.

`Scope::with_parent` now links an owner/identity-checked parent execution through
owned stop observers, not a parent session reference. Parent Stop and closing
wake a waiting tool; the earliest parent/child deadline controls checkpoints and
async waits. Parent closing is reported independently from explicit cancellation
and deadline expiry. Settlement intent is published before cleanup callbacks,
so parent disposal may join children without deadlocking their stop wait.
Observers own wake channels/clock only and do not pin parent resources. Parent
terminalization/drop and foreign parent binding are verified alongside the actual
Task abort → tool stop → resource release → aborted terminal event path.

No polling thread, detached timer or cancellation authority is added. Timer/wake
futures live in the handler's awaited operation and retire with it; parent signal
state lives in ExecutionSession. Production composition must use the explicit
parent-linked scope; the standalone convenience dispatch remains unparented.
A 10,000-child-scope check leaves only the parent registered with its original
reference count, then zero registrations after parent settlement. Production
process/output/progress adapters and full agent-loop migration remain pending.

`process_launch.rs` prepares immutable explicit Unix child mappings. It validates
environment/argv, requires an absolute working directory and resolves executable
names from child PATH (relative entries use child cwd; absent PATH uses Unix
`/bin:/usr/bin`). Commands clear ambient environment, disable stdin and pipe both
output streams. Unsupported non-Unix resolution fails closed. Construction is
not permission or sandbox admission; callers must install those policies and
provide bounded output, cancellation and joined reaping before production use.
Real process checks live in `tests/process_launch.rs`. No performance claim or
production handler switch is made by this launch-input increment.

`process_supervisor.rs::execute` runs one declared command behind a dedicated
Linux `tofu-native process-worker REQUEST_FORMAT CONFIG_BYTES` child. The caller supplies the
absolute trusted native executable and an admitted Request: launch mapping,
configuration/output byte caps, wall timeout, termination grace and cancellation
observation interval. It must run on its admitted blocking execution lane;
production permission, path and namespace/cgroup policy are still required.
The internal worker command reads bounded JSON on stdin and writes a private
4-byte big-endian header length, JSON lifecycle header (at most 4096 bytes),
raw stdout then raw stderr. This is not an application API or shell command tool.

The worker exclusively owns Linux subreaper and SIGTERM/SIGCHLD dispositions;
no application-global signal or reaping policy changes. pidfd exit readiness
avoids a polling floor on normal completion. Stop/timeout sends group SIGTERM,
then SIGKILL after the declared grace; normal completion also removes leftover
descendants. Leaders remain unreaped until group termination; escaped orphaned
descendants are killed and reaped until ECHILD. Result metadata records exit
code/signal, cancellation, timeout, total output bytes and truncation separately.
Capture caps never stop draining; drain slices are bounded so output floods
cannot starve Stop. Dropped/unwinding callers request worker Stop and join it.
Exceptional cleanup errors retain ownership and retry rather than abandon it.

The supervisor receives parent-death SIGTERM, but uncatchable worker death/OOM
is not certified containment; production adapters must preserve the external
namespace/cgroup authority. This primitive does not impose child CPU/RSS/process
or filesystem/egress limits, and must not replace the existing production tool
sandbox until those adapters are verified. Linux pidfd/procfs/subreaper support
is required; other OS targets do not expose this module. Native transient tests
verify real child exit, double-forked setsid escape and parent-linked tool Stop
through resource settlement; production command registration remains pending.

`examples/process_measure.rs` measures disposable supervised `/bin/true` launches
and OS CPU/RSS counters. This quantifies native supervisor cost only; it does not
compare equivalent Python isolation or establish application speedup. Resource
accounting and retained measurement live in `docs/RESOURCE_CONTROL.md`.

Native exec boundaries now mark all inherited handles above stderr CLOEXEC
before both supervisor and tool exec, set no_new_privs and disable core dumps.
The std exec-error pipe remains writable until exec; failure therefore remains
an actual spawn failure. Linux 5.11+ can use CLOSE_RANGE_CLOEXEC; older supported
kernels enumerate procfs inside the post-fork child with an 8-KiB fixed buffer.
No parent-side descriptor snapshot or numeric range cap is used. Unexpected
syscall/inventory failures refuse exec. No parent descriptor flags, limits or
privilege policy are changed. `process_boundary::ResourceLimits` declares positive
finite CPU seconds, address-space bytes, output-file bytes and open descriptors;
`Request.limits` installs equal soft/hard ceilings on the tool, leaving the
supervisor able to clean up. `None` is explicit absence for trusted primitive
checks, not unattended admission. Per-process limits are not aggregate tree
memory, disk or PID bounds; no_new_privs does not remove existing capabilities
or provide filesystem/network isolation. Production must still supply those
external policies and explicit resource admission.

The internal request and binary header carry
`tofu.native-process-request/v1` and `tofu.native-process-result/v1`; mismatched
formats fail rather than silently skip limits. The version precedes the numeric
config bound in worker argv, so pre-version workers refuse before executing any
tool request. `tests/process_supervisor.rs`
verifies actual CPU, memory, file-size and descriptor enforcement, high inheritable
FD closure and unchanged parent state. Library tests force proc inventory and
retain std exec failure behavior. These tests do not certify production sandbox
cutover or hard supervisor-death containment.

`agent.rs::Agent::run` now composes transient multi-round execution over the
existing Task driver, compiled owner/epoch tool catalog, authorization and
parent-linked tool scopes. Composition supplies an authorized Provider port and
a Feedback port that owns public model-facing text/error projection. Full
batch IDs/shapes and lossless numeric argument carriers preflight before effects;
compiled defaults/permissions and final authorization still use the existing
gateway. Duplicate/missing IDs, malformed JSON or unsupported numeric carriers
fail explicitly rather than invoke partial batches. Denial and ordinary tool
failures may be projected by Feedback; failed required cleanup always fails the
agent. Stop suppresses late results/continuation and Task alone terminalizes.
Explicit budgets cover rounds, total tool occurrences, retained messages/raw
usage and text feedback; no compaction or silent truncation is invented.
No tool executes when the declared remaining model budget cannot continue it.

`agent_http.rs::HttpProvider` owns immutable request fields/tool names for the
authorized fixed-endpoint transport. It serializes borrowed messages into one
bounded byte buffer, forcing streaming and refusing duplicate message authority.
Parent closing/Stop/deadline wakes silent network waits without detached watcher
threads. Invoke this synchronous composition from the admitted execution lane,
not inside a running async reactor. `tests/agent.rs` exercises real TCP → admitted
native supervised echo process → wire tool feedback → next TCP model round →
one terminal publication, plus denial, invalid batches, budgets and failure/Stop.
`examples/agent_lifecycle_measure.rs` verifies 10,000 transient executions and
zero retained scopes; this is lifetime evidence, not application performance.

This is the transient ordered/text lane. Production parallel tool classification,
approval/progress, multimodal result projection, provider routing/protocol
translations, compaction, pricing and durable round checkpoints are not yet
adapted. Production application and embedded Python services remain their
current owners until those adapters and whole-application gates are verified.

`tool_scheduler.rs::Scheduler` now supplies a composition-owned parallel tool
lane via `Agent.scheduling`. Its explicit budgets declare workers, total in-flight
calls and weighted payload bytes. Whole batches reserve capacity before any
publication; partial admission never starts a subset. The private Rayon pool
starts only for the first classified parallel batch; empty/ordered-only work
starts no workers. Pools persist across batches and close joins stored worker
handles, including partial-start failure. No global Rayon pool is used.

Only a read-permission call whose name is in the supplied immutable classification
set overlaps. Ordered calls run on the already-admitted caller lane by provider
position while independent reads may run on the pool; handlers retain project
leases and authority for cross-task write exclusion. Feedback remains in original
position order. All physical calls and resource cleanup finish before dispatch
returns. Necessary cleanup failure cancels remaining scopes and fails the agent
even when earlier peers are cancelled; tool-local Stop also aborts the parent.
Close refuses new work, drains admitted calls and joins threads; concurrent
closers wait for that join. Callback self-close and same-pool nested dispatch
are refused rather than deadlock. Authorization now explicitly requires Send+Sync
and is still rechecked by the existing registry at final physical dispatch.

Production must supply classification from the canonical tool/ordered-state
policy and derive capacity from resource admission; explicit values have no
hardcoded single-user ceiling. This port does not certify builtin approval,
progress, programmatic-child reconciliation, durable effects or full app cutover.
`tests/tool_scheduler.rs` and `tests/agent.rs` verify actual overlap, sorted
feedback, Stop, capacity refusal and faults. `examples/tool_parallel_measure.rs`
compares four independent synthetic I/O handlers with the ordered native path;
measurements are scoped concurrency/cost evidence, not real tool/app speedup.

The tool scheduler now consumes the private `tofu-rayon-core` path dependency
under `vendor/rayon-core`, based on upstream 1.13.0 with retained MIT/Apache
licenses and `UPSTREAM.json` source/delta hashes. The custom `short_idle_spin()`
option selects four idle rounds for this pool; upstream defaults remain 32.
Partial wake uses the same selected threshold, preserving wake/latch accounting.
A zero-round experiment is retained as rejected tail-latency evidence. Separate
package/links identity allows an external consumer to use upstream Rayon beside
Tofu without workspace patches; publishing the dependency is disabled.

Verify `cargo test -p tofu-runtime`,
`cargo test -p tofu-rayon-core --lib -- --test-threads=1` and the repeated idle/wake
stress case before changing the maintained delta. Root workspace membership is
explicit for both crates; the core's many thread-pool tests run serially to avoid
unrelated test-pool resource pressure. Existing engine workspaces stay separate.
`examples/tool_parallel_measure.rs` accepts an optional wait/empty/cpu profile;
paired source/binary-identical fixtures and separate RSS observations are retained
in `rust_tool_idle_policy_20261009.json`. These costs cover synthetic
native scheduling only, with whole-application migration/admission still pending.

## Experimental request-local token counting

The unadmitted candidate shares short-piece BPE storage across text fields of one
Counter.count call, alongside its existing regex scratch. Only byte offsets and
ranks are retained, never input text or token results. Every unmerged short piece
clears the prior boundaries before reuse; whole-token matches allocate nothing.
Exact capacity growth stays within the 1 KiB crossover; long pieces retain their
independent heap scratch. Success and every error drop all request-local storage.
No encoder cache, worker, cross-request buffer or admission default is added.
Problem: repeated short-piece allocation can erase native computation gains.
Decision: measure reuse of one request-owned vector without changing merge order.
Alternatives: a process-wide scratch pool complicates concurrent ownership and
retains peak requests; always using the heap increases short-piece overhead.
Verify the full-encoder/heterogeneous-field oracle in tests/token_ingress.rs and
the paired examples/token_scratch_measure.rs via benchmarks/token_scratch.py.
Separate allocator diagnostics remain in examples/token_ingress_measure.rs.
The candidate is retained as benchmarks/token_scratch_candidate.patch; production
keeps the original algorithm. Materialize both frozen source snapshots with
benchmarks/token_scratch_prepare.py --reference-commit e9a8d5d52 --output /tmp/tofu-token-scratch-sources.
Build each with its own CARGO_TARGET_DIR and run benchmarks/token_scratch.py with
both binaries and --reference-root/--candidate-root. Never share a Cargo output
directory between variants: a cached example can silently reference the other
source tree. Identical binary hashes refuse the comparison before measurement.
The `rust_token_scratch_20261011.json` preserves
failed tails, A/A calibration and the rejected large cohort. Cold diagnostics
and kernel gains cannot supply full-app performance admission.
The global [admission contract](../../benchmarks/system_performance_contract.json)
also requires cold first-operation clocks from distinct fresh processes; missing
activation evidence cannot be replaced with warm component measurements.

## Native BPE data layout

Problem: the initial two-byte rank search repeatedly hashes keys and follows
vocabulary pointers; short unmerged pieces also allocate boundary vectors.
Decision: derive one encoder-owned 65,536-cell u32 index from the admitted
vocabulary, keep longer rank lookup canonical, and store boundaries for pieces
below 32 bytes inline. Single-byte coverage is verified at admission; a two-byte
piece has exactly one possible merge. The linear merger uses one algorithm over
inline and ordinary Vec storage; larger pieces retain the indexed heap.
Alternatives: request-wide reuse failed prior tails; a manual slice-length rewrite
slowed medium pieces despite identical counts; a second vocabulary changes rank
authority. The added 256 KiB index has the encoder's existing lazy/drop lifetime,
never holds input text and does not alter owner/byte/token admission.
The fixed-size array expresses the complete u16 key space to the compiler.
The measured dynamic-slice variant failed code-piece tails; fixed storage passes.
Merge adaptation retains MIT upstream notices under licenses/tiktoken-rs-0.11.0/.
The `rust_pair_ranks_20261011.json` covers
14 cases, three alternating pairs and 5,000 operations per case/arm. All warm
P50/P95/P99/CPU/RSS guards pass; dense o200k pieces improve at least 10% in
every pair. Separate allocation diagnostics show +4.71%/+2.34% encoder residency.
benchmarks/token_cold_start.py runs identical token_ingress_measure examples
with --cold-only --encoding in 100 fresh workers per encoding/arm, including
first-operation CPU and wall clocks. Both encodings pass the 5% cold guard.
To rebuild the original comparison, archive e9a8d5d52 as reference, copy the
current token_ingress_measure.rs into that snapshot, and build each variant
with its own CARGO_TARGET_DIR. Pass source roots and binaries to both runners.
The two benchmarks/token_pair_*_rejected.patch files reconstruct the rejected
slice and dynamic-index variants from this implementation; their source digests
and failed measurements remain in the evidence. These component checks do not
grant application-default or whole-agent migration admission.

Provider completion rechecks session/client Stop and deadline after the required
text consumer joins. A consumer can publish Stop during the transfer's winning
poll; no response success may escape that publication boundary. Existing
transport/provider failures retain precedence. Verify the deterministic
session/client/deadline/failing-consumer cases in tests/stream_progress.rs.
`html_projection::project` and `project_bounded` now implement the canonical
knowledge HTML text projection entirely in Rust. The iterative tokenizer/arena
preserve entity, whitespace, hidden subtree, heading, table and malformed-input
behavior of the explicitly recorded Python 3.12.13 owner. A subtree text index
avoids repeated walks of deeply nested empty cells. Generated Unicode casing
and context predicates preserve that owner across Rust Unicode upgrades.
`project_bounded` refuses text-workspace expansion without partial output; its
budget is distinct from input/arena memory and process RSS. Verify the generated
oracle and `tests/html_projection.rs`, then the compiled-source differential
driver `benchmarks/knowledge_html/native_check.py`. The optional `html-projection-abi` feature exports synchronous complete-result
sinks, with generated ABI status and lifetime contracts. Explicit Python bindings
seal pinned release bytes and join accepted calls before unloading. Application
default selection and durable-ingest admission remain separate.

> Evidence note: the the former evidence/ artifact snapshots cited here were retired from the tree. The cited JSON artifacts remain recoverable from git history.

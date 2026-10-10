# Automatic resource control

## Native model control resources

One explicit counter and host-account port reuse the one-slot reader, existing
counter worker and caller executor. Host state retains one request, cancellation
signal, lease and at most one frame-bounded frozen usage candidate. No history
mirror, account copy, extra pool, queue, timer service or durable record is added.
Each control action uses declared ACK bytes/time; reserve/dispatch are additionally
clamped by the original execution deadline. Settlement and cleanup join their
physical calls. The debug wire probe caps a proposed 100-token output to 7 and
returns actual 3+2 token usage; the installed SDK uses one actual shared parent
account, and the application probe settles two requests once. These are quota,
representation and lifecycle checks, not RSS/throughput or system admission.

## Native dispatch credential resources

The existing dispatch ACK carries at most its declared frame byte budget of
trusted per-attempt headers. Rust moves the parsed HeaderMap into Request; no
header field, allocation or credential cache is added to Held. Configured headers
are cloned only when no replacement is supplied. Python keeps one frozen header
carrier during the joined callback/ACK, then releases it. A 2026-10-10 Python 3.12
layout probe measured both baseline/candidate policy dictionaries at 296 bytes;
prepare_headers and dispatch_entered are the two added attributes. The one-token
fixture encoded to 37 bytes. These are object/encoding measurements, not RSS or
throughput evidence, and exclude callback/vault memory. No pool, worker, queue,
timer, ledger or durable growth is added. Refresh callbacks share existing
cancellation/joining; only proven pre-intent failures become unsent. Verify actual
rotation, empty replacement, invalid headers, timeout and unknown intent tests.

## Native shared-account checkpoint resources

Explicit shared recovery adds one scalar account object to the existing bounded
snapshot, not another history, ledger, worker, queue or journal. Capture and
restore use the existing account lock and supplied executor, and join before ACK.
A 2026-10-10 encoded fixture with owner 7, root "root", limits 1000 tokens/3 tools,
fractional UTC deadline and cl100k_base/max_tokens/fixture policy adds 376 JSON
bytes including the shared_model_account wrapper. This is encoded metadata cost,
not RSS or throughput evidence; identity/model lengths still consume the existing
snapshot/frame bounds. Existing model settlement and root membership must be
quiescent; pending attempts and active children refuse export. Restored unknown
usage remains fenced. Verify the shared-account core and actual archive/TofuDB
pause/close/reopen tests; default production and active-tree recovery are pending.

## Native selected model authority resources

with_model_admission chooses one supplied authority and does not create an account
mirror, worker, queue, history cache or durable record. The native variant keeps
its lease inline, without an adapter Box. A 2026-10-10 debug metadata probe measured
SelectedAdmission 16 bytes versus its prior Account Arc 8, and SelectedLease/native
Lease both 32 bytes. These sizes exclude the authority and external adapter heap.
One frozen scalar cap survives validation through serialization. External leases
live for one joined counted request; unsent failures settle, while unknown intent
stays fenced at their authority. This is representation/lifecycle evidence, not
RSS, throughput or whole-system admission. Run model_admission_cost with nocapture.

## Native absolute deadline resources

One optional u64 UTC expiry is carried by the existing round owner and borrowed
checkpoint view. It adds at most 40 encoded JSON bytes per retained snapshot
(measured with u64::MAX); ordinary frame/snapshot budgets still apply. No history
copy, timer service, background worker, receipt log or new durable record is added.
Session observation metadata is call-local, and existing cancellation/reader joining
owns shutdown. Wall time is sampled once per explicit absolute-deadline run, then
an inherited monotonic deadline covers every round. This is a representation and
lifecycle measurement, not a throughput, RSS or whole-system admission claim.

## Native canonical context policy resources

One run freezes route/output/tool JSON and an optional fixed deadline; close joins.
Scopes reset after preparation; the caller owns the executor and eight receipts.
One call-local deadline timer retires after joined work; queued work can cancel.
Shared root preparation adds no workers/pools/history mirrors; one joined settlement
signal fences inbox confirmation. Deadline: zero effects; no RSS/speed claim.

## Native context preparation resources

Preparation is explicit and uses the existing single control slot. Configured
round_preparation_bytes bounds each encoded request/reply; the reader uses the
maximum of that admitted bound and the tool-frame bound. The host rejects an
oversized prepared reply before reservation, and Rust validates logical retained
history under retained_bytes before dispatch. Disabled preparation transfers no
history and owns no replacement. Enabled history/reply carriers live only through
the owning callback, reservation and model round; no worker, pool, queue, timer
service or persistent history mirror is added. Callback cancellation joins before
reservation; invalid admitted history settles NotDispatched exactly once.
The application fixture measures JSON history 16,033 -> 102 bytes at round zero,
and 709 -> 709 bytes at round one while retaining tool/reasoning evidence. Native
stdio verifies 16,000-character history transfer and ordinary Stop during partial
200,000-character publication: a complete frame precedes aborted, with zero HTTP.
These encoded-size/lifecycle measurements prove neither peak RSS nor performance
admission. A hard deadline or broken pipe may fail an incomplete publication;
ordinary Stop finishes the owned frame under the original task deadline.

## Native round permit resources

One per-run ledger retains two atomics; the ordered provider lane owns at most
one attached Permit at a time. Ordinary history/result evidence is borrowed; explicit preparation may own one
replacement history. Permit consumption or Drop invokes its settlement callback exactly once;
Drop reports Abandoned, not zero usage. Callback errors/panics fail the required
ModelRoute resource and prevent success certification. Admission construction
owns rollback before returning its Permit. Policy waits can observe parent
cancellation/deadline through Context without receiving settlement authority.
A denied HTTP round opens no connection; native tests also verify second-round
refusal, stopped/failed/panicked providers, NotDispatched on deadline, late usage
settlement despite cancellation and settlement-fault refusal. No new worker,
queue, timer service or retained round ledger list is created. Application policy
still owns finite-credit treatment, aggregate capacity and durable records.
These count/lifecycle observations do not certify application speed or RSS.

## Native terminal persistence resources

The live reader joins and transfers at most its existing bounded unread buffer;
it creates no second live input pump. Optional persistence retains one frozen
candidate in Task, one attempt counter/retry flag and one exclusive AsyncFd
carrier. Each serialized request obeys its explicit frame budget. Exclusive
post-settlement writes support frames above PIPE_BUF without a concurrent writer;
stdout flags restore before ACK waiting and stdin restores before publication.
No thread, pool, unbounded receipt list or model retry is introduced. Native
ACK grace and maximum host-requested attempts bound the persistence exchange.
Python retains one frozen event/receipt pair per invocation and joins its owned
callback and ACK task even after repeated cancellation. Policy callbacks must
quiesce; ACK transport alone has the host shutdown bound. The caller owns durable
idempotency/writer fencing, including uncertain timeout reconciliation.
The verified standalone fixture is 14,358,304 bytes, 34,912 above its predecessor.
Thirteen host unit tests and fourteen archive scenarios observe cleanup and
commit attempts; the retry scenario keeps two model requests/one tool callback
while commit attempts increase from one to two. These count/lifecycle witnesses
are not peak memory, durable database correctness or application performance
admission. The existing host frame/drain/admission budgets remain authoritative.

## Native Responses dialect resources

One explicit Profile enum accompanies the existing provider budget/replay state;
no history copy, pool, queue, timer, account or credential store is added.
The 2026-10-10 debug x86_64 layout probe measured Profile at 1 byte and both
previous (Budget,bool) and new (Budget,bool,Profile) tuples at 24 bytes.
Generated dialect literals have process-lifetime static storage. Existing request
byte limits bound borrowed serialization and host model settlement remains joined.
Codex omits the server output limit by canonical protocol policy: local admission
and actual/unknown usage settlement remain required and cannot imply a hard
provider token cap. Run dialect_validation_keeps_local_output_admission with
nocapture and actual counted stdio tests. These are layout/lifecycle measurements,
not CPU/RSS/latency or default-production admission.

## Native Responses stream resources

A caller supplies item and conservative retained-byte bounds. Function positions,
item-ID aliases/ambiguities, hidden message positions and replay entries have
bounded owners. UTF-8 identity bytes are charged before copying aliases; text,
arguments, protocol identities and snapshots contribute to byte admission.
Conservative charges include replacements over the response lifetime and metadata;
they are not exact allocator RSS. Unknown event telemetry is a single saturating
counter, never an unbounded set of provider labels. No worker, queue, timer, cache,
transport connection or persistent store is introduced by this parser.

Visible prefix proof uses one character counter and fixed SHA-256 state without a
second retained response-text buffer. Terminal text slices borrow response fields;
only a proven missing suffix is projected. Thirty-five Python-derived scenarios
exercise replay, slot routing, duplicate/recycled IDs, errors, usage and phases.
Chunk widths 1/3/7/64/65536 preserve exact Unicode text, tool JSON, replay and ignored
malformed trailers. A 512-character UTF-8 ID fits a 2,500-byte event but its retained
aliases exceed that admission and refuse before a slot is stored. Detached nested
replay snapshots cannot mutate the owner. These state/count/bound observations are
not application speedup or Responses production-route evidence.

## Native structured-output request resources

Schema conversion uses the existing per-round output_config projection and bounded
request writer. It introduces no worker, timer, cache, queue or persisted schema
registry. The canonical descriptor/schema remains caller-owned; the native format
projection belongs to the current request and is released after serialization.
Whole-history borrowing and existing native process/request/retained-byte bounds
remain unchanged. This is not a new model-token or billing policy.

Measurement: three invalid format/configuration cases complete with zero observed
TCP connections and all transport slots available. The real application fixture
sends two model requests with the same schema and effort, executes one registered
file write and persists the returned JSON text in its terminal Turn. Sixteen
Python-derived request fixtures verify supported conversion, including preserved
format and effort. These count/lifecycle checks do not claim application speedup
or that client-side response validation has been implemented.

## Native multimodal feedback resources

Explicit multimodal mode adds no worker, timer, queue or media store. The
application's existing image pipeline owns decode/resize, immutable media and
vision policy; the native host carries already admitted canonical blocks.
Encoded array bytes count toward feedback_bytes, frame_bytes/tool_frame_bytes
and retained_bytes; text retains its previous byte semantics. Exceeding feedback
admission stops before the follow-up model request. Block validation borrows
Rust values; Python lazily caches only two static schema validators. Schemas
ship as generated Python code, not runtime repository-file dependencies.

Anthropic MIME reconciliation decodes at most 1,364 base64 prefix characters
(at most 1,023 decoded bytes), then drops the sample. This is bounded temporary
scratch, not a full image decode or an allocator RSS measurement. Image URL
references are forwarded without local fetching. Native ingress retains its
existing generated per-image token fee; no billing/permission policy is inferred.
Measurements: real registered image inspection completes two model requests and
one settled tool round; undeclared mode and a 16-byte feedback ceiling each
complete one model request, one read/tool round and a durable error outcome.
Tool model-content validation returns the original borrowed blocks without
mutation. These lifecycle/count observations are not application speedup claims.

## Native Anthropic HTTP/request resources

Anthropic execution shares the existing HTTP connection/pool admission, request
byte writer, required text-consumer joining and terminal drain. It introduces no
thread, pool, watcher, timer or retained history cache. Request serialization
borrows canonical history and creates only current message/tool-result-group
projections rather than a full converted history vector. The final request
buffer remains bounded by request_bytes; existing agent retained-byte admission
bounds canonical inputs. The explicit CLI derives protocol byte admission from
its existing output_bytes profile and uses the generated block-index ceiling.
Protocol and canonical accumulation have separate bounded charges; neither is
an allocator RSS claim. Header/route bytes retain the existing bounded,
single-use principal-owned credential carrier.

Measurement: the real TCP model/tool/model test records two requests over one
keep-alive connection, one executed client tool, preserved hosted/signed replay
and all pool slots released. The stdio/application test records two model calls,
one registered file write and one canonical settled tool round before terminal
ACK. Unsupported-request tests reject media/policy without transport effects.
These count/lifecycle checks do not claim HTTP/application speedup; the isolated
stream translation measurements below retain their narrower scope.

## Native Anthropic stream resources

Each stream owns one bounded protocol map, usage accumulator and existing SSE /
Chat owners. Block count and conservative byte charges are explicit composition
inputs; protocol index/name ceilings derive from the retained authority. Event
encoded size is checked before field copies with a counting writer. Initial
block/replay identities, reverse-name metadata and input/text growth are charged;
usage replacement conservatively remains charged over the response lifetime.
This is serialized-payload admission plus bounded metadata, not exact allocator
RSS. Detached snapshots become caller-owned output. No worker, timer, queue,
cache or persistent state is introduced. Native host/HTTP enablement remains
separate from this stream parser.

Measurement: `benchmarks/anthropic_stream_measure.py` and the release example
compare both actual translators on 10,000 one-character server-tool JSON input
fragments, including event JSON parse and final verified replay snapshot. Two
seven-sample runs measured Python medians 118.586 / 160.673 ms and native
7.991 / 8.001 ms. Conservative native protocol charge was 40,495 bytes; this is
not RSS. Raw samples and source/binary hashes accompany each local result.
Python reparses growing input on each delta; native parses at closure/snapshot.
These isolated kernel observations exclude SSE framing, transport, model time,
application queues and billing; they are not whole-application speedup evidence.

## Native tool model-round metadata

The native invocation context adds one optional machine-word round identity;
the host ToolRequest adds one optional integer slot. The scheduler's existing
batch owns the number; no counter, queue, worker, timer or persistent store is
introduced. Wire metadata grows by one scalar `model_round` property per tool
request (16 JSON bytes at round zero). Existing whole-frame admission applies.
A focused scheduler test carries rounds 0/1/9 through ordered and parallel lanes,
while standalone dispatch carries None. The real multi-round fixture verifies
two pipeline writes and final durability after three model requests. This is
metadata/ownership evidence, not an application speedup measurement.

## Native application gateway resources

The optional NativeGateway borrows the application's declared executor and owns
one serialization lock plus count/byte-bounded pending decision proofs. Bounds
are supplied by the composition, not fixed host assumptions. A proof contains
canonical tool name, arguments and caller bytes; capacity is reserved before
policy await and released on denial, failure, execution or close. The existing
application receipt count/TTL remain unchanged, and original decision timestamps
are retained. Cancellation adds at most one scoped watcher per active callback;
it is joined at callback exit and physical pipeline work is always joined.

Measurement: the focused pending-policy test admits one outstanding decision
with a declared one-call/4096-byte budget, rejects the second while policy is
blocked, then closes with zero retained proofs/bytes and rejects the late grant.
The real native/application test performs one registered file write, one settled
tool round and two model requests through the existing event and terminal owners.
These are ownership/count measurements, not RSS or end-to-end speed comparisons.

## Native host approval resources

Optional approval control borrows the existing native reader/control slot and
application callback executor; no pool, process or resident polling loop is added.
The existing native approval Store retains only its configured receipt count and
identity byte quota, TTL and weak parent binding. Host proofs retain detached
canonical name/argument/caller bytes under explicit payload and receipt-count
bounds until exact consumption or invocation disposal; Python container overhead
is additional bounded per-entry work, not charged as allocator/RSS evidence.
Each request carries the native remaining deadline rounded upward to milliseconds.
One scoped host timeout signals the request stop event and joins its callback;
late decisions become denial. Repeated explicit cancellation writes Stop before
joining callback/result delivery. Published write calls wait for result/EOF;
client admission remains charged until host work and native close both quiesce.
The real archive/TCP probe observes one write callback/scratch mutation after one
approval, zero mutations after denial/policy failure/approval Stop, and no second
model request after required policy/tool failure or Stop. A cancelled already
started write may have committed: its one observed effect remains evidence, not
rolled back or silently replayed. All cases observe zero active native runs after
joined return. These are lifecycle/effect counts, not application CPU/RSS or
throughput admission. Old standalone read-only stdio callers retain their declared
cancel/deadline semantics; matching native host clients opt into result joining.

## Native incremental text resources

The optional text path adds no pool, process, timer or second stdin reader.
Each network chunk retains only accepted offset metadata over the existing
bounded text accumulator; adjacent same-channel appends merge without copying
text, while channel changes retain provider order. Each nonempty span charges
at least one admitted output byte; Vec capacity/metadata scale with that bound.
A native fixture with 512 one-byte appends measures 12,288 bytes of unmerged
span storage versus at most 128 capacity bytes after merging to one span.
These are metadata byte counts, not allocator arenas/RSS or application speed.
CLI text uses one existing control slot, UTF-8-safe atomic pipe frames and one
pending host ACK. Native Stop/deadline cancels network reads but waits for an
issued consumer; EOF/fault closes the joined reader. Host projection borrows the
application executor, serializes frames, and joins callback/ACK before releasing
process admission. Incoming text bytes are bounded by declared per-round output
bytes times maximum rounds; every frame remains independently bounded. Close
accounts for bounded text frame metadata and still uses its declared time grace.
Raw execution with no observer retains its allocation-free parser observation
path. Required-consumer failure remains unknown usage when raw provider evidence
cannot establish it; this path grants no refund or automatic retry. Full CPU,
RSS, input-to-paint and application throughput admission remain unmeasured.

## Native application terminal resources

The application native terminal policy creates no pool, process, timer or
registry. It borrows an explicitly supplied lifecycle-owned executor, serializes
one task's terminal callbacks with an owned async lock, and retains two fixed-size
SHA-256 identities plus the existing task reference. Incoming event/receipt bytes
are bounded by the supplied terminal frame budget before decoding. Projection
runs once; commit retries create one physical executor future at a time. Native
cancellation and repeated host cancellation join that future before serialization
or process admission can be released. Projection failure retains bounded candidate
exception context until the policy is disposed; task output remains governed by
existing task budgets and is retained until publication/recovery delivery settles.
The adapter does not charge a second worker pool or retain a full native history
cache. Disposal and heavy-state reclamation remain application composition duties.
Real archive/loopback/application-store tests observe two physical model requests,
one host tool callback and one terminal projection, then zero active native runs.
Those are lifecycle witnesses, not application CPU/RSS/P99 or throughput admission.

## Native route host resources

The v2 native host port retains one chosen route's public metadata/private
headers in bounded JSON bytes. Its default ceiling derives from the existing
model-routing document bound; callers may provide a smaller owned invocation
limit. The existing semantic repository and candidate compiler retain their
current bounds. One credential is resolved once per carrier; no plaintext cache,
Slot registry entry, health record, task, pool or network probe is added.
Consumption atomically detaches the private carrier; native execution uses the
existing client admission/process budget and joined close. The host clears the
owned headers on every exit. Prepared body/inference work and proxy resolution
remain explicit policy ports, not new parallel authorities. An actual archive
probe observes two physical model requests, one read callback, one secret
resolution and zero active native invocations after exit. These are count and
lifecycle witnesses, not allocator/RSS/latency improvement claims. Durable route
accounting and production default selection remain pending.

## Native local-token resources

Encoder initialization is lazy and owned by an explicit Arc, with no permanent
counter worker or prompt-count cache. Counters share an encoder only when their
composition supplies that Arc; repeated native process launches do not share
its dictionaries. Input/text/tool-JSON byte bounds and a maximum 128 JSON depth
bound transient work. BPE token-vector scratch is proportional to bounded UTF-8
text bytes; tool JSON is bounded before encoding. The CLI adds at most one scoped
CPU worker per ordered invocation, with an explicitly supplied stack of at least
256 KiB, and joins it while the existing reactor handles control. CPU/encoder/
stack demand must enter launch admission before production use.
Release diagnostics observe first-use cl100k/o200k wall time about 72/147 ms,
retained requested bytes 14,316,652/28,078,894 and peaks 15,779,779/31,728,409.
After encoder drop the diagnostic process retains 8/0 extra requested bytes;
these measurements include library process bookkeeping, exclude worker stacks,
allocator arenas and RSS, and do not prove application memory admission.
Three alternating fresh-process pairs over each encoding's 1,200 request counts
show CPU ratios 0.0845–0.1132 against the current Python local backend,
including its digest cache and batch implementation. Imports and fixture JSON
parsing are outside timing; native allocation instrumentation remains enabled.
CLI worker startup, transport, route policy, tier selection and durability are
excluded. Python tiktoken already has a Rust BPE core; these ratios measure
request-count composition, not a general BPE speedup or full-system improvement.
The standalone executable grows to 14,323,392 bytes because vocabulary is
embedded; the host's sealed image is shared only within its declared client.
These cold/footprint costs prevent an unmeasured default production promotion.

## Native model-account resources

Bounds explicitly declare active actors, pending attempts and identity bytes;
composition derives these from launch capacity and owned execution quotas. The
account retains bounded actor/attempt maps and scalar consumed/unknown/ordinal
state, never histories or a settled receipt list. Pending attempts retain their
account through Lease until settlement/Drop. Revoke fences future dispatch;
detach and close refuse pending work rather than discarding consumption. Callback
ports must quiesce and use weak back-references when they retain the account.
Observers run without account locks; pending observation prevents admission,
complete snapshots and close. Without an observer, receipt strings are not
constructed. Waiting uses the existing runtime/caller, adding no polling thread,
worker or persistent timer. Cancelled waiting acquires no lease.
A System-allocator diagnostic, including its external lease vector, measures
1/8/64/512 simultaneous actors and attempts at 1,444/2,116/18,268/150,220 live
requested bytes; peaks are 1,448/2,164/18,316/150,268 bytes. All four return to
zero additional requested bytes after drop. These are allocator-requested bytes,
not RSS or allocator arena footprint. Three alternating fresh-process pairs of
20,000 reserve/dispatch/settle/snapshot operations show native CPU ratios
0.0171–0.0178 and wall ratios 0.0171–0.0178 against unchanged canonical Python
bodies. The native diagnostic retains allocation instrumentation. The workload
uses one uncapped actor and excludes tokenizer, HTTP, observers, durable accounts
and application scheduling; it is not production performance admission.

## Native usage observation resources

Observation borrows the provider JSON and scans only generated fixed alias
lists; it copies no usage map or nested payload and creates no retained counter
ledger, thread, queue or cache. Wide-counter/unsupported numeric text errors
retain raw usage and mark observed tokens unknown. The three-field stdio
projection remains subject to the existing frame and FIFO atomicity bounds.
The built fixture is 6,679,344 executable bytes. Three alternating
fresh-process pairs each run 48,180 observations over the same 2,409 captured
inputs after 20 warmup passes. Native wall-time ratios are 0.0773–0.0817 and CPU
ratios 0.0772–0.0817 against unchanged canonical Python function bodies. Import
and JSON parsing are outside timing. This diagnostic does not measure allocation
peak/RSS, provider waiting, billing, account admission or end-to-end application
performance, and does not authorize production cutover.

## Native round control transport resources

Optional round control reuses the existing reactor reader, one pending reply
slot and one host reservation record per active run. It adds no OS thread,
worker pool or retained per-round list. Requests carry message count, not copied
history or invented token counts; raw usage/error evidence uses the existing
frame bound and fails closed when oversized. Ordinary FIFO publication retains PIPE_BUF atomicity; explicitly enabled history
preparation uses exclusive bounded frames as described above. Cancellation/deadline bounds authorization waits; settlement may
ignore cancellation to deliver evidence but retains the native deadline. Host
ACK transport has its own declared shutdown grace. Owned policy callbacks must
quiesce and are joined even after repeated cancellation; accounting failure is
not retried or replaced by zero usage. After native exit, a reserved attempt
without observed settlement receives one Abandoned callback.
The built fixture is 6,670,312 executable bytes (50,680 above the preceding
fixture); sealed image lifetime and active-run limits remain unchanged. Nine
host unit tests and eight real archive/TCP scenarios verify callback joining,
ACK timeout, second-round refusal, settlement failure and cancellation; every
archive run releases admission. These are resource/lifecycle observations,
not peak RSS or application performance admission.

## Native host process resources

The internal host client has explicit maximum in-flight runs, executable bytes,
configuration/frame bytes and shutdown grace. It retains one lazily loaded,
sealed executable image; the verified fixture is 6,619,632 bytes. The image is
shared by native processes for that client and released on joined client close.
Hash/copy work borrows the host executor once; no polling or permanent dedicated
reader thread is added. Per-invocation processes own their stdio buffers. Frame
structure/string checks precede serialization; stream-reader buffering is bounded
by the declared frame limit. Graceful cancellation drains bounded output; expiry
kills and awaits exit. Repeated cancellation cannot detach acquired work.
Callbacks remain caller-owned and must quiesce when cancelled. Budget derivation
and aggregation belong to the existing launch policy, not this transport port.
Eight real archive/TCP scenarios verify native rounds, callback error attribution,
overload, cancellation and concurrent close; all observe zero active runs after
cleanup. These observations do not certify application CPU/RSS/latency gains.

## Native continuous control resources

The stdio invocation owns one Tokio input task on its already declared reactor,
not an additional OS thread. Input and frame limits stay unchanged. Exactly one
reply slot and one pending published-request identity are retained; duplicates,
unissued IDs and invalid frames fail closed instead of buffering future replies.
A fatal frame cancels transport and is projected as a protocol error; a valid
cancel requests parent cancellation. A required Upstream session resource joins
the input task before terminal publication and restores stdin flags. The task
holds only a weak parent, preventing a resource/reader ownership cycle. Shutdown
and rollback both join; an actor panic is cleanup failure. Live subprocess tests
observe cancellation during silent HTTP, reject unissued/invalid/oversize frames,
and verify original shared stdin flags are restored before the terminal line.
No polling loop, background reader thread or unbounded mailbox is introduced.
These are control/lifecycle checks, not application performance measurements.

## Native stdio agent resources

Configuration and tool frames have explicit byte limits. Stdin is exclusively
owned, uses bounded incremental nonblocking reads, and is rebound from a lean
configuration reactor to a runtime whose blocking capacity matches the declared
HTTP connection budget. No detached stdin reader or per-tool worker is created.
One tool is outstanding in the ordered lane; its parent-linked scope waits on
a one-slot reply channel versus cancellation/deadline. Tool requests publish as atomic FIFO
frames no larger than PIPE_BUF with cancellation-aware writable readiness;
stdout flags are restored before waiting for the result. Failed protocol exchanges
settle the task rather than continuing the model loop. Terminal delivery uses
ordinary stdout backpressure after execution cleanup. Stdio cancel frames are
observed during both provider and tool waits; the deadline remains independent.
External host execution and its own cleanup remain host-owned. This native
port does not certify external tool side effects, RSS or application throughput.

## Native I/O occupancy resources

Demand CPU weights range from 1 to 1000 units per worker; launch CPU capacity
is effective CPUs times 1000. Aggregate leases charge workers times their
occupancy, plus declared worker memory and in-flight job bytes. Derived worker
counts are bounded by both remaining occupancy and memory; all arithmetic is
checked. Unknown memory still admits one lean pool only. Weights are estimated
and cannot enforce actual CPU use; production adapters must retain conservative
weights until workload evidence justifies lower ones. CPU work remains 1000.
A synthetic two-CPU/128-MiB fixture compares eight independent 20-ms waiting
handlers at weights 1000 and 250, with three fresh alternating 100-sample pairs
and 20 warmups. Source and raw samples are retained in the native I/O capacity
evidence. Faster batch completion can require more resident worker memory and
CPU overhead; this evidence is not whole-application performance admission.
No production default or dynamic policy is changed by this explicit capability.

## Native aggregate pool allocation resources

One composition-owned allocator caps aggregate declared CPU occupancy by the
probe's effective CPU capacity and weighted bytes by its explicit share of launch
memory headroom. Each demand can reserve a fraction of the remaining share;
allocation charges declared worker weights plus maximum in-flight job bytes.
Pool count is bounded by positive worker admission. Unknown memory admits only
one lean worker/job pool. This is weighted admission, not RSS reservation or
cgroup enforcement. Pool constructors roll back failed reservation; leases
release only after active jobs, borrowed calls and workers have joined. Scheduler
reentrant-close refusal retains its charge; concurrent successful close callers
observe release after join. Allocator close stops new pool admission and does
not detach or forcibly stop existing pools. No queue, timer or worker is added.
Four focused tests include 16 simultaneous constructors, a mixed executor/tool
allocation observing 6144 charged bytes and three workers, and a blocked job
retaining charges through close. All joined closes observe zero charges.
The native CLI also checks zero aggregate charges. These measurements certify
resource lifecycle only, not end-to-end CPU/RSS/throughput or dynamic tuning.

## Native launch capacity resources

`capacity.rs` reads at most 1 MiB per proc/cgroup input and uses no timer, worker
or global cache. Launch composition owns the immutable snapshot and selected
volume. Membership/mount roots use component-aware validation, escaped mount
paths and visible ancestors. CPU uses the minimum online/affinity/quota count;
unreadable/unresolved hierarchy falls back to one. Memory headroom uses the
minimum host available and each finite ancestor limit minus current usage;
missing usage under a finite limit grants no measured headroom. Volume space
uses caller-available blocks. Unknown memory yields one worker/one job; known
insufficient headroom refuses admission. Demand overflow and zero inputs fail
closed. Explicit workload weights determine concurrency and in-flight bytes,
with no fixed upper worker ceiling. Independent compositions must share an
aggregate budget rather than each spending the full snapshot. Existing direct
budgets remain explicit overrides. Live-host evidence records 63 effective
CPUs, 63 configured workers, 15 observed resident workers and 126 completed
synthetic jobs with joined close. These are resource/lifecycle observations,
not CPU/RSS/throughput gains; resident counts depend on arrival timing.

## Native approval receipt resources

Each task-scoped receipt store has explicit count and weighted identity-byte
budgets, a weak parent session and no worker or timer. Expiry is lazy at mint,
inspection and consumption; close clears retained receipts. FIFO eviction and
same-ID replacement invalidate old capabilities. Weighted bytes count two call
ID copies, entry metadata and ticket-index metadata; this is admission weight,
not allocator RSS. Arguments are retained only as a fixed digest. A 10,000-mint
regression observes at most two receipts and 4096 weighted bytes, then zero on
close. Production composition must derive aggregate budgets from the launch
probe and close stores at task settlement. This count measurement does not
certify application CPU, RSS, throughput or performance admission.

## Native tool idle-policy costs

The composition-owned tool pool selects a four-round idle spin window from the
private renamed Rayon-core snapshot. Other pools retain upstream 32-round
defaults; no process-wide setting or environment override changes. Latch/event
counters and scope joining remain the upstream protocol; partial wake resets
to the selected pool threshold. Zero spin was rejected for a measured empty-job
P99 regression. Pool capacity, batch call/byte admission, cancellation, cleanup
priority and joined shutdown are unchanged. The retained path dependency embeds
with the crate and can coexist with upstream Rayon; package/links are distinct.

`evidence/rust_tool_idle_policy_20261009.json` compares identical four-handler
fixtures in three alternating process pairs: waiting work (100 samples/variant),
empty and hash work (1000), each with 20 warmups, plus separate cold RSS processes.
Waiting CPU ratios are 0.70..0.75, empty 0.65..0.66 and hash 0.95..0.96; all measured
latency ratios remain below 1.05 and RSS ratios 0.97..1.01. CPU includes startup
and warmup; counts/zero post-close workers are checked separately. Instrumented
syscall counts diagnose yield/futex behavior only, never substitute for timings.
This is scoped synthetic scheduler cost, not production tool or whole-system
performance admission. The dependency's 103 serial unit tests and runtime's
133 tests cover retained wake/scope and lifecycle behavior.


## Native scoped tool scheduler resources

Composition declares private parallel worker capacity, aggregate in-flight call
and weighted-byte budgets, and immutable independent-read names. Whole batches
reserve both counters before publication; oversize/concurrent overload refuses
without partial execution. Weights include serialized arguments/caller, identity
bytes and fixed scope metadata, not allocator/stack RSS. Empty and ordered-only
batches start no threads; the first parallel batch starts the declared pool,
which persists until joined composition close. Ordered callbacks use the already
admitted caller lane; total calls bound both lanes. Production must derive and
aggregate these capacities from the launch probe, not duplicate an unconstrained
pool for every request. No global Rayon pool, per-batch thread pool or durable
queue is introduced. Caller/worker self-close is refused; concurrent close waits
for active batch guards and thread join. Partial thread startup also joins.
All borrowed jobs, scope disposal and original-position results settle before
feedback; required cleanup failure stops peers and remains fatal despite Stop.

`evidence/rust_parallel_tools_20261009.json` records three 100-sample alternating
process pairs (20 warmups) for four independent 20-ms synthetic I/O handlers,
plus three separate memory pairs. Parallel elapsed time is approximately 0.25
of ordered time, with higher CPU/RSS cost; this is no whole-system admission.
Declared/actual worker counts and zero scopes/workers after close are checked.
Production classification, approval/progress, programmatic reconciliation and
full application gates remain pending.


## Native multi-round agent resources

The transient native agent keeps one admitted working-message array and a raw
usage ledger for its declared execution. Explicit caller budgets bound provider
rounds, total tool occurrences, serialized retained messages/usage and each text
feedback. Serialized size is counted through a writer without constructing a
second JSON buffer; these are logical payload caps, not allocator/RSS admission.
The HTTP adapter borrows messages/template while producing one bounded request
byte buffer; its callable-name catalog is shared across rounds. There is no new
worker pool, retry queue, durable store or retained cache. Tool effects currently
use the ordered lane with existing parent-linked scopes; production parallel
classification/admission remains required before replacing the Python pipeline.

Provider responses and tool scopes reach quiescence before continuation. Parent
Stop/deadline interrupts silent HTTP; live tool Stop waits for handler return
and resource cleanup. Required cleanup failures fail the agent rather than feed
a recoverable tool error. Native Task alone freezes and publishes the terminal.
The synthetic 10,000-agent count fixture executes 20,000 provider rounds, releases
10,000 tool resources, publishes 10,000 terminal events and leaves zero active
scopes. Source identity and counts live in `evidence/rust_agent_loop_20261009.json`;
real TCP/native-process and fault tests are separate. These are lifecycle counts,
not CPU/RSS/tail/throughput or whole-system performance admission. Routing,
compaction, usage pricing, approval/progress and durable round adapters remain
production integration work.


## Native child exec boundary resources

Both native supervisor and tool exec seal all handles above stderr and disable
core dumps/exec privilege gain in their forked children. The parent retains its
original descriptor flags, resource limits and privilege policy. Descriptor
closure uses Linux CLOSE_RANGE_CLOEXEC where available; the older-kernel path
streams actual procfs entries with one 8-KiB stack buffer, skips redundant writes
for already-CLOEXEC handles and never bounds safety by descriptor number. The
std exec-error pipe stays open until exec so failed launches remain failures.
Inventory/syscall failures refuse exec; no parent snapshot, cache, pool, reader
thread or persistent artifact is added. Explicit positive finite ResourceLimits
apply equal CPU/address-space/file-size/descriptor soft/hard ceilings only to
the tool. None remains trusted primitive absence, never unattended admission.
Limits are per process/file, not aggregate tree memory/disk/PIDs; no_new_privs
does not drop existing capabilities or isolate filesystem/network access.

The worker version is an argv preface before its numeric configuration bound;
pre-version peers therefore refuse before reading/executing the tool request.
Versioned request/result envelopes also reject skew. Actual non-root tests verify
inheritable FD 4096 closes even above child NOFILE 32, parent flags remain intact,
direct-worker tool exec still seals handles, and CPU/memory/file-size/descriptor
ceilings actually stop growth. Forced proc inventory tests preserve exec errors.
The source/binary-identified A/B/B/A cold-launch samples and separate VmHWM
processes live in `evidence/rust_process_boundary_20261009.json`; the initial
cost observations remain identified rather than discarded. Compared with a
reference missing this boundary, final median launch cost is 7.8..8.8% higher and
CPU cost 0.8..8.4% higher across three pairs. That reference is not an equivalent
safe implementation, and these costs establish no performance or application
admission. Raw CPU totals include startup and descendants; child RSS is a
maximum, not aggregate simultaneous memory. Creating user/PID namespaces passes
on this host, but both private-proc mount attempts are denied; namespace/chroot,
aggregate cgroup limits, hard supervisor-death containment and production command
policy adapters remain independently unverified.


## Native process supervision resources

One admitted native invocation creates one short-lived Linux supervisor process
and its declared tool process tree. There are no capture reader threads, new
worker pools, detached jobs or persistent caches; the caller's existing physical
job admission must bound concurrent invocations. Configuration and stdout/stderr
payload caps are explicit caller budgets. Configuration serialization is borrowed
and byte-bounded before spawning; capture drains use fixed 16-KiB scratch buffers
and at most 16 reads per observation. Poll descriptors are stack-owned; child PID
parsing uses an 8-KiB streaming buffer rather than a process-count-sized list.
Worker result transport contains a four-byte length, at most 4096 bytes of JSON
metadata, then raw bounded stdout/stderr; it never expands bytes into JSON values.
Parent/worker buffers can overlap, and caps do not include allocator overhead,
process stacks or command memory. Linux child CPU/RSS/process-count, namespace,
filesystem and egress budgets remain the external sandbox composition's job.

pidfd readiness wakes process exit; an explicit request poll interval bounds
cooperative Stop observation without a watcher thread. Timeout and termination
grace remain independent. Leaders stay unreaped until group kill; adopted escaped
descendants are reaped until ECHILD. Callback unwind requests supervisor Stop and
joins its exit before releasing ownership. Cleanup OS errors retain ownership
and retry; they never authorize success or abandonment. Parent-death SIGTERM is
verified with an isolated caller killed by SIGKILL; supervisor SIGKILL/OOM needs
external namespace/cgroup containment and is not yet certified. No production
command adapter or application process is switched by this increment.

Three fresh release measurement processes each supervise 100 `/bin/true` calls;
P50 is 1.57..1.61 ms, P99 1.94..2.01 ms on this host. Three separate one-call
processes record native VmHWM 3504..3712 KiB; child rusage peak is 3540..3644 KiB
(maximum, not summed simultaneous memory). rusage self peak can retain pre-exec
launcher high-water; native VmHWM is recorded separately. CPU counters include
startup and child work. Raw samples, source/binary identity, capacity and parent
loss receipts live in `evidence/rust_process_supervision_20261009.json`. This is
supervision cost, not an equivalent-isolation baseline or whole-system admission.


## Native parent tool stop observers

A parent-linked tool scope owns cancellation/closing channel receivers and a
clock/deadline observation, not a parent ExecutionSession reference or resource
lease. One settlement-intent watch belongs to each session and wakes children
before callbacks may join them. Awaited wake/timer futures introduce no new pool,
watcher thread or detached job. The effective deadline is the earliest declared
parent/child bound; stop facts freeze before child resource cleanup.
A 10,000-child measurement retains the parent's original reference count and
only its own registration, then zero registrations after settlement. Live parent
Stop, deadline, closing/join and abandonment fixtures verify quiescence. These
are lifetime/state counts, not CPU/RSS or throughput admission.


## Native tool execution scopes

Every native handler invocation owns one resource scope registered by its
request composition. Scope identities are local to that owner/registry; explicit
scoped calls carry their configured deadline. Resource callbacks reuse the closed
ExecutionSession labels and limits, and terminal cleanup retires the membership.
There is no new worker pool, background timer or retained invocation cache.
A 10,000-dispatch measurement observes 10,000 calls and releases with zero active
scopes afterward. A joined live-Stop fixture proves resources remain held until
the callback exits. These are lifecycle counts, not throughput, CPU or RSS
admission; physical handler/process budgets remain the composition's owner.


## Native compiled tool contracts

Native execution documents compile into an immutable request-owned catalog;
validation consumes owned argument values and copies defaults only when applied.
Schema property order is retained, and the registry remains frozen to owner/epoch.
There is no per-call validation cache, global registry or worker pool; handler
resource lifetimes belong to the execution service. Three paired processes per
profile execute 5,000 validations for small/nested/wide arguments, with raw wall
aggregates and source identity in `evidence/rust_tool_contract_20261009.json`.
These measurements exclude compilation, cold start, CPU, tails and RSS.
Caller-supplied document/argument admission remains the composition boundary.


## Native tool occurrence assembly resources

The native occurrence table and its upstream-index aliases belong to one
provider attempt and are bounded by the explicit tool-position capacity. Each
alias is created only by an admitted split. Request callable names are immutable
shared catalog data, extracted once from the canonical request rather than
rebuilt for every delta. Opaque tool metadata counts against cumulative output
admission. A 10,000-frame retransmission measurement retains eight occurrences
and one alias; 160 Python fixtures verify assembly at five chunk sizes. These
are state/lifecycle counts, not RSS or application throughput admission.


## Native tool-response admission resources

Each native Chat attempt owns one flood guard bounded by its explicit tool
position capacity, with fixed SHA-256 signatures in position/count maps.
Parameter trees are not retained; transient key sorting is bounded by admitted
output bytes, and arrays use streaming hashing. A 10,000-observation unchanged
position retains one position, while 2,400 Python-oracle observations verify
position retirement and matching order across revisions. Both maps retire with
the attempt, including rejection. Default/range derive from the shared Python
policy (32; 2..4096); explicit native request overrides remain. These are retained
state counts, not RSS or end-to-end throughput certification.


## Native provider HTTP resources

The native runtime HTTP composition accepts explicit capacity-derived pool
budgets; the standalone CLI requires all budgets rather than assuming host
capacity. One endpoint/proxy composition owns one client and a semaphore limiting
active attempts. Idle connections are bounded per that sole endpoint and expire;
the permit spans header wait, streaming and terminal drain. One session-owned
watch signal and a call-local receiver wake cancellation without polling threads.
The caller owns the Tokio runtime; no per-request executor or client is created.
Real TCP verification executes two attempts on one accepted connection and
observes all permits returned after success, cancellation and timeout, including
Stop during silent upstream input. These are lifecycle/resource counts, not
provider throughput or RSS admission. Production probe/default wiring remains
pending; operator-supplied values are explicit. See the native crate guide for
transport gaps and `packages/tofu-runtime/tests/http.rs` for executable checks.


## Problem, decision, alternatives

Users submit work and expect correct results quickly. An independent fixed
resource limit can reject useful work even when the machine has room: a route
pool counts candidate handles, whereas execution pools count running tasks.
Increasing every constant hides leaks and can instead exhaust memory.

The resource catalog in `contracts/resource_policy_v1.json` owns the existing
105 launch defaults and six live resource declarations. Its generated stdlib
projection (`scripts/gen_resource_policy.py`) supplies `runtime_guards.py`;
the cached launch probe and existing capacity formulas still derive defaults
from CPU affinity/cgroups, memory capacity/headroom and volume space. Explicit
operator settings retain their ceilings. No tuning control is added to the
user task flow. Distributed deployments retain external admission policy.

One serving-loop controller adjusts local budgets using measured demand and
pressure for bounded domains. [Scan/heartbeat isolation](DEFENSIVE_PATTERNS.md#background-fan-out-before-demand-submission) protects the serving loop. Root acceptance is demand-driven;
native owners retain usage, fairness and teardown;
there is no second execution registry or lease counter. Storage selection,
provider credentials/quotas, model choice, task quality, context, cost limits,
and durable user state remain under their existing authorities.

Alternatives rejected: raising a single magic limit does not cover dependent
pools; per-task hardware/configuration probes add latency; a second scheduler
creates accounting divergence; automatic provider-quota or task-quality changes
would violate the user's execution constraints.

## Interface and authority

`ResourceController` provides `catalog`, `snapshot`, `plan`, `apply`, `acquire`.
Plans carry a controller identity and revision; stale/foreign plans are rejected.
An owner port declares read/resize callbacks, minimum/maximum, optional native
acquisition and memory cost. `ResourceOwner` requires user and task identity;
a lease delegates release exactly once after successful native release, even
if the controller has stopped. Domain execution paths continue to use their
native leases. Adding a port never copies their accounting.

Snapshots contain desired/effective capacity, used/waiting, completed/errors,
blocked attempts, current authority ceiling, reason and status. Blocked attempts
are cumulative, not a fabricated number of waiting tasks. A native sample
failure is visible as unavailable. Apply orders dependencies before growing
upstream work, and upstream before shrinking dependencies. Partial application
is reported truthfully, blocks upstream growth and retries after a cooldown.
Shutdown cancels the sampler; existing work retains its native release owner.

| Resource | Native owner | Live behavior |
| --- | --- | --- |
| `execution.root` | demand-driven agent executor + entry accounting | accepted jobs prepare workers immediately; resize changes idle thread reuse; root ownership tokens track settlement without refusing admission |
| `execution.child` | Swarm execution accounting | enter ready work immediately; keep explicit owner accounting and cancellation without permit queues |
| `model.requests` | dispatch request accounting | local counts and controller resize cannot block authorized requests; genuine provider failures keep their retry/cancellation policy |
| `model.routes` | ephemeral handle registry | register every request-owned handle without a count ceiling; rollback and disposal remain exact |
| `storage.rpc` | selected native v2 session pool | never exceed current daemon readiness budget; excess sessions close only when idle or released |
| `storage.frames` | selected client's response frame admission | grow byte admission with headroom; preserve occupied bytes and response priority |

These are process budgets, not per-user quotas. Shared owners retain identity
and fairness. Optional owners attach lazily on first normal use. Restarting a
native storage client replaces its port; controller sampling opens no storage
or provider connections. Legacy/SQLite clients do not acquire native RPC ports.

Cataloged launch settings are explicitly `restart` settings, not falsely
advertised as hot-swappable. The six ports above are the first live migration.
Feature-specific controls outside the common launch policy remain with their
current owners until given catalog declarations and safe resize ports. External
daemon/provider ceilings are observed, not raised by the application controller.

## Feedback and safety

The serving loop schedules feedback every five seconds and awaits one serial
sample in its existing asyncio worker pool. Kernel and owner reads never run on
the HTTP loop; cancellation or shutdown cannot schedule another sample or apply
a late adjustment. Only local counters and current process/cgroup/host memory
are read. Launch-probe cache hits use layout inputs without filesystem access;
layout resolution and remote directory scans occur only on a miss or refresh.
The cache retains at most 16 layout observations. Problem: resolving auto layout
before cache lookup stalled the HTTP loop on a network directory. Decision:
cache by layout inputs and offload the whole feedback sample. Alternatives
rejected: disabling pressure checks weakens admission; adding a timer/pool
duplicates lifecycle ownership.
Measurement on the deployment's FUSE volume: 500 warm lookups fell from
570.071 ms / 500 layout resolutions to 1.353 ms / zero resolutions.
With an injected 80 ms counter read, a 5 ms event-loop heartbeat's ten-sample
median fell from 80.099 to 5.131 ms. This is loop-isolation evidence, not model
throughput. The sampler retains one in-flight worker-pool job and a cancellation
fence; repeated ticks cannot accumulate blocked probe jobs. Unknown headroom disables growth. Two
healthy samples of queued or blocked work permit a 25% increase, within the
launch envelope, memory reserve and owner ceiling. Shared provider contention,
downstream waiting and new errors suppress growth. Concurrency is not itself
success: failed completions are excluded from productive throughput.

A growth trial lasts at least 30 seconds; repeated growth cannot overwrite it.
When sustained demand produces at least four completions but no throughput
gain, the previous target is restored and growth backs off for 60 seconds.
For remaining bounded execution domains, memory pressure reduces admission
by 25% of the smaller of active
usage and current capacity, at most once per 15-second
cooldown. Running work drains normally. Occupied response buffers are not
reclaimed to force a smaller budget. Buffer limits consume no idle memory.

Remaining bounded-domain admission pressure comes from current memory
observations; a historical refusal
is diagnostic evidence, never a persistent pressure latch. Refusals increment
the blocked counter. After healthy demand samples, pressure-reduced capacity
returns toward its last healthy ceiling, respecting current headroom,
dependencies and operator limits. Recovery does not enter a speculative
30-second throughput trial. Slow 25% growth from a temporary minimum was
rejected because it needlessly strands useful work on large hosts.

Route handles are execution-owned metadata, not a concurrency envelope.
Registration/disposal carry exact ownership; launch sizing hints and controller
plans cannot refuse registration or drop a handle to manufacture capacity.


## Demand-driven child execution

Problem: session pools, local semaphores, a process permit queue, wave/result
quotas and delivery-worker slots independently parked or rejected authorized
children. Registry overflow cleanup could then abort old productive sessions.

Decision: dependency-ready children use the common demand-driven executor.
There is no local execution semaphore, permit wait queue, wave-size ceiling or
session/result-count admission limit. Owner-addressed accounting records entry
and release; Stop is checked before entry and again after startup observers.
Retry and inherited token/tool/deadline authority remain under their existing
owners. The launch parallel value controls idle thread reuse, and the session
value controls terminal-memory cache retention. Live sessions may exceed it;
capacity cleanup cannot abort them or delete their durable result rows.
Admission does no whole-registry sorting or retirement. Existing throttled
maintenance selects terminal candidates once per sweep, preserving prompt
acceptance when the live registry grows past the cache target.
Recovery reservations exclude duplicate identities, without counting capacity.

Cancellation mirrors and child event delivery start lazily for actual work,
without shared slots. Child events remain FIFO beyond the old queue allowance;
real sink failures and flush deadlines remain visible. Closing a child revokes
publication and detaches uncommitted closures; an entered callback retains its
thread until actual return without denying another child. Worker-start failure
still settles through ordinary invocation cleanup.

Alternatives rejected: raising several unrelated constants retains hidden
bottlenecks; unbounded work on a fixed pool still queues; retiring live sessions
loses user work; dropping child evidence can falsely report successful results.
The task/result carriers and callbacks are reclaimed by their existing owners,
with no new background queue or scheduler. Provider-side constraints remain in
model I/O and are not changed by this migration.

Measurement on this host with 64 synthetic parked child runs: all entered in
20.723 ms despite old pool/local/global/waiter/session values of one. Peak
process RSS rose by 796 KiB, all 64 results settled, and active owner accounting
returned to zero. This measures scheduling/accounting, not provider throughput.
A registry-only measurement with maintenance ports isolated registered 2,000
live sessions in 1.831 ms with a terminal-cache target of one; it includes no
provider or storage work.

## Model requests, route ownership and SSE

Problem: a local request count could hide a healthy authorized provider; a
separate route registry could reject execution before dispatch; SSE quotas
rejected connections or displaced unrelated live conversation streams.

Decision: dispatch records every request reservation without a local capacity
refusal or fan-out reduction. Request-owned route registration is atomic with
no count ceiling; construction failure, cancellation and observer failure keep
the existing rollback/disposal owner. Controllers observe demand but cannot
restore these limits. RPM pressure remains a selection hint with a saturating
score so a busy healthy target cannot overflow into ineligibility. Provider
errors, cooldown, authorized fallback, billing and consumption policy remain
with their existing authorities.

SSE tracks opaque connection tokens by explicit principal locally, releasing
only the exact owned token. It has no shared lease store, count admission,
expiry heartbeat or quota response. New conversations cannot evict other live
streams. Same-client reconnect supersession and stale-generation fencing stay
in the broker. Setup failure closes the subscription, and response/observer
close never cancels accepted agent execution.

Alternatives rejected: raising count defaults leaves nested bottlenecks;
unrelated subscription eviction interrupts legitimate work; pretending real
provider failures succeeded would corrupt accounting; expiring live ownership
can lose exact teardown. Reconstructible generation history remains bounded,
and connection/request metadata is reclaimed at actual close/settlement.

A metadata-only stress measurement registered 4,096 connection tokens under
one principal in 46.714 ms despite an old cap of one; all tokens were unique
and the active count returned to zero after concurrent release. Peak process
RSS rose by 6,988 KiB, including the eight-worker test harness and Futures.
This measures accounting rather than live socket or model throughput.

## HTTP connection and control RPC demand

Problem: the asynchronous model HTTP pool imposed a fixed active connection
ceiling and pool wait deadline even after dispatch stopped limiting requests.
Control RPC separately gated per-socket calls, recent request counts, process
workers and reliable response backlog.

Decision: active model HTTP connections have no local count limit or pool-wait
timeout. Keepalive retention remains separate: its default follows the cached
launch CPU/memory policy via sync-worker capacity, explicit idle overrides
remain, and expiry/close owns reclamation. Connect, upload, TLS/proxy/egress,
provider failures, model Stop and terminal stream drain retain their contracts.

Control RPC uses demand-driven physical workers with copied request context and
explicit socket owner. Count and rate gates cannot deny requests or responses;
only allowlisted, valid requests enter execution. Real deadlines/cancellation
settle the waiter while native Future completion owns physical accounting.
Close retires idle workers and stops new dispatch; already-running read calls
finish without a late response. Correlated responses are FIFO until actual
socket delivery/disconnect, which clears pending reconstructible frames.

Alternatives rejected: an unbounded backlog behind a fixed executor still
queues; bigger connection counts preserve the ceiling; pretending timed-out
threads exited hides residency; disconnecting on count backlog interrupts
useful requests. Existing executor and socket lifecycles own reclamation with
no second scheduler or durable state machine.

On this host, 144 synthetic RPC handlers all entered in 37.230 ms, with a
2,644 KiB increase in process peak RSS and 144 correlated responses. The
measurement includes worker setup/accounting and parked reads, not filesystem
throughput. A real loopback HTTP test holds 48 requests simultaneously above
the former 32-connection limit, then verifies complete responses and cleanup.

## Operations and verification

### Demand-driven root acceptance and workers

Problem: fixed root worker slots and a finite launch queue serialize accepted
I/O work and can reject another task even when existing workers are parked on
model or tool I/O.

Decision: the root executor prepares a worker for every submitted job, reusing
idle workers when available. The launch worker value and live resize control
idle thread reuse; neither gates execution. The handoff deque supports pending
cancellation and honest pre-worker status, but has no capacity rejection.
Surplus threads retire after completing their work. A proven wedged thread
keeps its quarantine accounting and retires when it returns. Shutdown,
duplicate task IDs and actual OS thread-start failure retain typed ownership
rollback; a failed submission cannot later execute.

Alternatives rejected: increasing the queue only postpones rejection; waiting
for the five-second controller sample keeps usable resources idle; running jobs
on the HTTP loop breaks request responsiveness. Root entry uses local exact ownership tokens without capacity slots, memory
refusal, shared-store leases or heartbeat threads. Old inflight settings and
controller resizing cannot restore a root ceiling. Child execution also starts on demand. Model dispatch and SSE also use ownership accounting; other feature gates
remain separate owners and require subsequent migration.


Measurement on this host with a synthetic parked-I/O barrier: 64 tasks all
entered within 12.536 ms with an idle reuse target of one, zero queued jobs at
the barrier and a 796 KiB increase in process peak RSS. All 64 completed and
resident worker threads drained to one. This measures thread scheduling and
cleanup only; real provider payloads and OS thread-start limits still matter.

`benchmarks/conversation_concurrency.py` measures production executor and
durable Turn operations on a disposable native authority, with a barrier
provider to prove simultaneous running tasks, bounded output, SIGKILL/reopen,
RSS and latency. It does not spend tokens or measure real-provider quotas.
Use `--conversations 1000 --restart-while-running --rounds 4 --chunk-bytes 8192`
on the target volume; `--recover-interrupted` instead checks application boot
settlement of every interrupted attempt. Reports include the exact binary
hash and filesystem, and retain failed authorities for diagnosis. Verify with
`tests/resource_control/test_thousand_conversations.py` and the native run;
capacity is conditional on the measured workload and current machine pressure.

Native readiness distinguishes a stuck authority holder from sustained useful
contention. Successful foreground mutex acquisitions advance one authority-local
counter; the watchdog observes it without taking the mutex. Progress resets
the contention window even when every sampled `try_lock` is busy. A held mutex
with no progress still trips the existing ten-second threshold, and poisoning
never clears on progress. A process-global counter was rejected because work
in another authority could mask a real wedge. Raising the threshold would only
delay recovery. Thousand-task verification must sample readiness as well as
completion: correct readback alone does not prove the supervisor will keep a
busy, healthy daemon alive.

`lib.resource_control.resource_snapshot()` exposes credential-free diagnostics.
The existing admin-scoped `/metrics` emits `tofu_resource_{desired,effective,
used,waiting,maximum,blocked,errors,pressure}` with bounded resource/unit labels and
`tofu_resource_policy_revision`. `[Resources]` logs record adjustments and
failure reasons. Diagnostics and sampling contain no user/task IDs or content.
`tofu_task_startup_stage_seconds` separates context construction, registration,
preparation, binding, submission and physical worker entry. Compare it with the
executor's actual queue-wait metric and storage RPC timings before attributing
startup latency to exhausted slots.
New code takes effect through the ordinary serving-runtime startup boundary;
changing source alone does not replace an already running server.

Storage warmup reads one metadata catalog page with no messages or settings.
A generic conversation listing with a caller-side limit was rejected because
it can still hydrate large documents and hit the response-frame bound.

Run `python3 scripts/gen_resource_policy.py --check` and
`pytest -q tests/resource_control` first, then the neighboring executor,
server-loop, dispatch, Swarm, storage and deployment contracts. Tests cover
real queued-work growth, in-flight shrink, failed/stale plans, retry/backoff,
exact release, cancellation, lazy imports, native authority limits and sampler
shutdown. Pool shrink must never lend a returned excess session while older
borrowers still exceed the new budget. Racing dispatch transfers a singleton
reservation before ordinary dispatch and releases cancelled/unsubmitted slots.

A local synthetic measurement (12 independent 40 ms waiting jobs, one initial
worker, sufficient headroom, feedback samples advanced for the experiment)
completed in 0.4821 s at fixed capacity and 0.2417 s after automatic growth to
two workers. This measures queue parallelism, not provider latency. With six
synthetic idle owners, 1,000 feedback ticks averaged 0.149 ms/tick under
tracemalloc; retained traced allocation after collection was 1,664 bytes
(peak 212,848 bytes). Startup/idle creates no resource-controller thread.

Completed native authority holds of at least one second emit an
`authority_hold` diagnostic naming only the operation and elapsed time.
Foreground commands, overlay merge, rotation prepare/install and pack install
share this timer; no user content enters the record. The threshold bounds log
frequency per serialized holder. Readiness measurements retain at most eight
failure examples (component state or probe exception), so a failed load result
can distinguish contention from an unavailable health transport.

Overlay merges begin at 64 entries after every cold open, then measured hold
latency grows or shrinks slices across 1..2,048 entries toward the existing
25 ms target. A fast mount still reaches the full batch; starting at the full
batch was rejected because cold DolphinFS merges held authority for 7.9 s.
Slow failed merges also reduce the next slice, while empty or immediately
rejected work cannot grow it. Rotation write admission is checked before page
materialization, preventing repeated expensive merges that cannot publish.
The per-key durable merge cursor and original commit versions are unchanged.

Merge admission also counts the exact pending bytes of its produced pages,
including the directory/root, against the existing 2 MiB WAL-carried payload
limit. Oversized slices shrink before publication instead of synchronously
fsyncing hundreds of loose files under authority. One indivisible entry keeps
the certified spill fallback so progress is always possible. Underfilled slices
do not justify growing the next target; growth uses the accepted slice size
and actual materialized entries, preserving the host's measured throughput.

On the 60-CPU / roughly 210 GiB DolphinFS host, byte-bounded merge admission
passed 1000 simultaneous attempts with four 8 KiB updates each, two SIGKILL
cycles, exact output comparison for all 1000, and offline verification with
zero missing blocks. The strict-durability run took 171.00 s (35.83 s admission;
stream P99 56.51 s including queueing), with 317 healthy probes, zero observed
continuous authority contention and zero `flushDeferred` spill events.
These are synthetic production-executor/storage measurements using a barrier
provider; they do not certify remote provider quotas or 1000 paid responses.

## Conversation presentation working set

The browser's initial block window follows the actual chat viewport height; the
policy and expansion semantics live in [RENDER_CONTRACT.md](RENDER_CONTRACT.md).
Collapsed round bodies are constructed on disclosure. This bounds reconstructible
DOM work without changing durable history, TurnStore authority, or worker
concurrency. Before the change, sampled six heavy conversations constructed
13,066–32,709 chat nodes and blocked the main thread for 519–864 ms on selection.
Keep browser navigation measurements alongside verification when changing this
working set; cold snapshot time and rendering time must be reported separately.
After viewport sizing and lazy round rendering, the same six conversations
mounted 1,338–2,550 nodes and their longest render tasks were 105–170 ms. Two
repeated selections reached content in 129–135 ms with zero snapshot requests
(previously 553–837 ms). Cold selections still took 1.1–1.8 s including the large
snapshot read and transfer; host load makes these measurements a moving target.


### Lazy read-tool transport working set

A 2026-10-01 read-only measurement on the previous large sidebar sample used the
same owner-scoped `refs`, tail-96 and artifact-hint snapshot. Initial decoded
JSON was 16,999,694 bytes; adding lazy read-tool output reduced it to 7,735,525
bytes (54.5% less), deferring 351 outputs containing 9,132,886 characters.
This measures representation size, not end-to-end latency: the initial storage
read still obtains authoritative projections and other tool families remain
inline. No process-wide snapshot cache or prefetch pool is added. Detail request
and cached output are owned by each mounted disclosure, expire with its block,
and have a 30-second read timeout; collapse/navigation abort pending reads. Full
result rendering retains the existing 120,000-character display bound.

The same frozen full/lazy response pair was parsed in real Chromium: median
JSON parse + generated-schema validation + reference materialization over five
alternating samples fell from 60.2 ms to 30.6 ms (49.2% less). This isolates the
browser decode path and does not claim a 49.2% reduction in navigation latency.


### Collapsed reasoning working set

Completed reasoning above 4,096 characters does not build its rich body until
opened. Each mounted keyed block owns one toggle listener and its latest render
callback; retiring the block removes both. Unchanged opened content is reused,
and closed updates defer the latest translation/projection. There is no global
content cache, prefetch queue, or worker. Streaming still renders immediately.
On one frozen live snapshot, two alternating Chromium runs reduced initial
closed-reasoning descendants from 909 to zero and total chat nodes from 1,432
to 523. Click-to-render-frame fell from 302–311 ms to 109–115 ms; longest tasks
fell from 220–230 ms to 63–64 ms. Snapshot requests were fulfilled with identical
bytes to isolate browser work; these figures exclude network/storage waiting.


### Large snapshot JSON decoding

Problem: the optional accelerated decoder built a large tree, discovered
unsupported floats, discarded it, and parsed the same response with stdlib.
Decision: an 8 KiB probe at each response edge declines explicit float members
before speculative parsing. The probe only selects the original parser;
remaining accelerator shape/number checks and typed errors are unchanged.
It owns at most 16 KiB of temporary slices and adds no cache, pool, or threads.
Alternative rejected: admitting all floating-point values can change original
number semantics, while a complete JSON pre-scan adds work to every large read.
A frozen 30,482,608-byte native snapshot response decoded in 241–261 ms before
and 196–203 ms with the rejection probe over three alternating samples. This
is decoder time only; native history materialization and HTTP rendering remain
separate costs. Exact huge integers, negative zero, quoted numeric source,
Unicode and malformed response behavior are specified by the wire-client tests.


## Request Inspector history working set

Problem: reconstructing and retaining every round's complete message array
made incremental durable histories consume quadratic read-cache space.
Decision: `SnapshotReplayIndex` shares original tails and verified canonical
prefix digests; the selected-round endpoint materializes only its newest
matching snapshot and, for folding, the previous round. The flat wire feed
materializes only the final snapshot. Public replay still returns complete
payloads, with unchanged legacy fields, version scopes and corruption markers.
Alternatives rejected: a larger cache only delays quadratic growth; clipping
payloads loses evidence; a second persistence format adds recovery authority.

The existing cache retains at most 16 entries and an aggregate conservative
unique-object byte estimate. Shared objects across entries may be counted
twice; digest carriers include a native-memory allowance. This estimate is
not an exact process-RSS limit. Oversized histories remain fully readable
without cache admission. Cold reads can still require a large temporary graph.
Write invalidation drops raw, derived and pending-read witnesses, preventing
old in-flight reads/index builds from repinning stale entries. Live/settled
TTLs remain 3/300 seconds; no extra timer, worker or durable store is added.

`TOFU_TASK_INSPECTOR_CACHE_MAX_MIB` is a cataloged restart setting, bounded to
4..65,536 MiB. Personal defaults use the existing launch probe: the smaller
of capacity/256 and available-memory/64, with that same bound. Unknown
capacity/headroom uses 16 MiB; distributed deployments use 256 MiB. This
setting controls reconstructible retention, never execution or user evidence.
The environment inventory also recognizes canonical resource-resolver calls;
existing launch budgets remain cataloged without changing their values.

A controlled append-only 400/800/1,600-round Unicode fixture measured
`tracemalloc` allocations after fixture/import setup. At 1,600 rounds, retained
Python allocations fell from 21,331,462 to 1,302,966 bytes and cold selected
read time from 15.43 to 0.71 seconds; one storage read and exact payload/fold
results were preserved. This excludes source-fixture storage, native digest
allocations, unrelated server work and primary-worker RSS. It establishes the
read-cache scaling improvement, not the cause of a live service's memory growth.
Timings vary with host load; allocation figures describe the integrated owner.
Focused regression owners are `test_snapshot_replay_index.py` and
`test_request_inspector_snapshot_budget.py`, alongside existing replay, wire,
read-cache, query-bound and page-recovery tests.


## Long-history inspector continuation

Problem: a structural prefix capped at 10,000 rows cannot establish the latest
snapshot of any round, because a later event may re-emit an earlier address.
Decision: selected payload and final-wire reads continue chronologically through
semantic event pages under `event-read-guard/v2`, scoped exactly to request
snapshots and tool-wire projections. Each slice checks the exact source stamp
and echoed scope before and after reading. Changed or unavailable guards discard
its result; retained count/cursor equality alone is never a fence.
Unrelated appends preserve admitted continuation. Relevant appends and retention
still require a fresh read; this is not an as-of snapshot lease or a guarantee
of completion while replay dependencies themselves change continuously.

`SnapshotReplayState` is the existing snapshot decoder made incremental. It
retains per-turn/version message baselines and tools definitions across slices;
unselected result records are released immediately. Later matching snapshots
replace selected ones. Frozen v1 split baselines, v2 chronological baselines,
prefix/message-count/tool/version checks and explicit degradation are preserved.
EOF is required for both a payload and a confirmed absence.

A read consumes at most 20,000 returned events or two seconds of page admission
per slice, with a 16 MiB conservative decoded-page estimate. One final page may
cross that byte boundary; native pages separately reject more than 8 MiB.
Storage calls retain finite deadlines. Working dependency state is bounded by
the launch-probed inspector byte budget. Between slices it uses the existing
16-entry aggregate byte cache; active workers leave only weak busy witnesses
there, and mutable replay graphs belong to their bounded reads. Busy witnesses
are not evicted while their worker is active. Same-selection readers retry;
there is no extra worker, timer, durable store or execution authority.

A budget boundary remains a retryable read failure, and a later request resumes
an admitted checkpoint. Invalidation, cache eviction, process loss or a new
source stamp requires a fresh scan from durable events. Memory-budget rejection
never truncates stored evidence or establishes expiry. Oversized ordinary reads
still retain their existing uncached path; long dependency scans may fail their
working budget explicitly. Full payloads are materialized only for the selected
results after the final fence. Source consistency does not certify arbitrary
corruption, demand, originality or net benefit.

Alternatives: lifting the prefix cap retains every historical payload; scanning
only the tail loses baseline/tools prerequisites; trusting the first matching
round misses re-emissions. Streaming the existing decoder preserves one replay
rule while keeping retained results proportional to the selected addresses.
Regression owner: `test_request_inspector_history_continuation.py`. Native
capacity, throughput and full request-list pagination require separate evidence.

A controlled constant-message legacy fixture with 1,000/10,000/20,000 source
snapshots and one selected address measured 5,238 estimated working bytes at
EOF and 4,397 after releasing continuation dependencies in every case.
This excludes source-fixture storage, prefix-outline cache, page buffers and
native allocations; it is not RSS or a throughput result. Growing message
prefixes, distinct turns and tool catalogs still consume the working budget.
Real native coverage includes 10,002 retained events, cross-page v2 message/tool
dependencies, late round re-emission, equal-bounds concurrent retention ABA and
a fresh-process replay. The existing browser regression opens the resulting
latest wire in both 390- and 1440-pixel viewports.


## Optional JavaScript program instances

Problem: full JavaScript must bound host queues, guest CPU/memory and cleanup
without retaining a new idle worker pool. Decision: compile one immutable
637,405-byte reactor on first use, with fuel metering and parallel compilation
disabled; each program owns and closes a fresh Store, promises and receipt map.
The existing tool worker invokes callback ports. Scheduling reserves at most
16 child calls before the guest yields. Source is bounded to 32 KiB. Returns
and model handoffs use canonical artifact/token-budget delivery without a fixed
1 MiB execution rejection. Private bridge packets are
bounded to the instance heap size, scaling with the same launch probe. Normal
execution uses a cumulative 30-second active guest deadline, including bootstrap,
built-ins and return serialization, excluding child tool waits and synchronous
host bridge work. A lazy shared 10-ms epoch ticker interrupts WASM built-ins;
Stores have independent deadlines. The last Store release stops and joins the
ticker, and engine disposal joins it before closing native handles. This adds
at most one thread while programs own Stores and no idle worker. Fuel remains
metered for statistics and explicit deterministic budgets; its default exact-JSON
integer maximum is not the normal execution boundary. Timeout and explicit fuel
failures identify their reason and limit; engine traps are classified separately.
Problem: a fixed 2-billion instruction cutoff rejected ordinary large projections
far below the heap bound. Decision: measure actual guest execution time rather
than engine instruction density. Alternatives rejected: raising the fixed fuel
constant repeats the same size-dependent failure; disabling every execution
boundary allows infinite loops and expensive built-ins to occupy workers forever.

`TOFU_PROGRAM_HEAP_MAX_MIB` uses the launch probe: personal heap grows with
capacity/128 and headroom/64, with a 16..1,024 MiB envelope; unknown probes use
32 MiB. Distributed default is 256 MiB; explicit overrides use the same finite
envelope. Linear memory is independently bounded to twice the heap plus 16 MiB.
There is no persistent instance, continuation, temporary file or child work
owned by this adapter after completion, observation, cancellation or failure.

A fresh-process local measurement on Python 3.12/Wasmtime 49 processed 240 and
2,400 synthetic rows (84,784 and 852,635 input bytes): warm medians over five
runs were 12.94 and 25.23 ms; the first run including compilation took 1,099.8 ms.
Guest linear memory was 1,572,864 and 3,342,336 bytes. Process RSS was 19.4 MB
before import, 34.1 MB after import, 85.3/87.9 MB after both workloads and
87.9 MB after collection; thread count stayed one. RSS includes compilation,
native dependencies and allocator retention, and is distinct from guest memory.
These adapter measurements exclude tools, models and gateway overhead.
After removing fixed return rejection, a 1,200,000-byte UTF-8 return with a
64 MiB guest heap took 27.76 ms median over three warm runs and used 4 MiB
WASM linear memory; this excludes model, tool and artifact delivery overhead.
A 100,641-character string projected through `Object.keys` reproduced the old
fuel failure at 23,592,960 linear-memory bytes, well below its heap limit. With
the elapsed deadline it completed 100,641 keys (794,683 output bytes), consuming
3,828,887,994 instructions and 32,112,640 linear-memory bytes. Five warm local
runs had a 734.9-ms median adapter latency; the last run recorded 647.8-ms active
guest time. Thread count returned to one after every run. This measures local
adapter work, not model latency, and claims no instruction or memory reduction.
Failure diagnostics reuse the existing at-most-16 canonical child receipts and
the program's 1 MiB input-delivery bound; they add no resident cache or worker.
Only failure parses complete inline JSON to summarize its root and first item,
with at most eight 64-character keys and no field values. A 1,000-row,
749,519-byte local input produced a 204-byte diagnostic: median 3.90 ms over
20 warm runs with Python allocation tracing, maximum traced allocation
1,002,312 bytes. This measures diagnostics after imports, excluding models,
tool execution and the engine; parsed objects leave scope immediately.
Alternatives: a resident VM pool adds task-state lifetimes; default parallel
compilation retains an unnecessary compiler thread pool; an unmetered engine
misses long-running built-ins. Verification: `test_javascript_program.py`,
`test_javascript_resources.py`, resource catalog and existing gateway tests.


Program result reuse adds one task-turn index, allocated only after an eligible
post-hook capture. Application and storage-free entries both use the original
`CanonicalReceipt` runtime descriptor with the same launch-probed
`TOFU_TOOL_RESULT_CACHE_CAPACITY` (16..1,024; defaults scale with capacity).
Small JSON arrays/objects use the same index and exact bindings; full delivery adds no model bytes.
At capacity 16, 32 small manifest captures retain 16 entries / 672 source bytes / 21424 charged bytes.
Terminal release clears the index; this is retained-source accounting, not RSS.
Their charged bytes are bounded to capacity times 64 KiB (1..64 MiB),
with each source at most 1 MiB; oldest entries leave the working set first.
The charge includes canonical manifest size, frozen name/argument binding bytes,
and 1 KiB per entry for metadata;
it is a conservative capacity charge rather than an RSS measurement.
Terminal callbacks release the index
and native descriptors; a new Flow turn clears them. There is no new idle
pool, persistent VM, raw model-preview cache or durable user-state deletion.
A focused measurement with capacity 16 and twelve 150,004-byte canonical
inputs retains six entries / 900,024 source bytes (plus charged descriptor
metadata) under its 1,048,576-byte
bound; terminal release leaves no index. This is a retained-text measurement,
not RSS; no new persistence writes or storage reads are added. The public
SDK integration performs one ordinary source call followed by one program
reuse and delivers only `{"total":45}` to the model for that program. It proves
execution behavior, not spontaneous adoption, saved model tokens or latency.
Explicit `tools.keep` computed JSON uses this same bank, byte bound and terminal
release, with no new resident pool. A sorted catalog-name SHA-256 fence adds
64 charged bytes per computed entry; all source coverage remains unknown.
Sixteen keep attempts per program are separate from source calls and reads.
A capacity-16 mixed measurement of twelve 150,000-character payloads plus one
original `[9]` input retains six computed values and one original input:
900,155 source bytes, 909,397 charged bytes under 1,048,576. Python tracemalloc
peak is 2,034,237 bytes, not RSS; terminal release removes the bank. The real
SDK contract executes observation, model selection and a second fresh VM with
one source call total and zero additional source reads. It mocks only model
transport, so it establishes behavior rather than spontaneous adoption or cost.
Partial result delivery reuses that existing bank and adds only host-validated
snapshot metadata, with no new worker, persistent cache, source read or write.
A fixed 64-item preview with a 102,703-byte input and synthetic `input_` selector
adds 202 UTF-8 bytes / 46 cl100k tokens including the derived read expression;
Validated JSON kind adds 19 bytes / 5 tokens to the same encoded metadata;
these are local encoding costs, not provider usage or savings. Complete views add zero bytes.
The stable code-action schema and context guide expose operations and composition
policy without repeating a full manual. Validation, VM bounds and authority stay fixed;
local token counts must be distinguished from actual provider usage and paired quality.
Round aggregate budgeting counts the metadata and recomputes exact visible bytes;
original coverage and bank capacity/lifecycle remain authoritative. JSON classification
decodes at most 1 MiB once at admission, retaining only a kind, never a second tree.
A 101,641-byte row array peaks at 112,686 traced Python bytes; a 1,000,001-byte
scalar array peaks at 4,169,516 bytes (not RSS). Directory/projection never reread bodies.
Failed execution snapshots actual counters before Store closure, without extending VM life.
The private input directory uses the same index, owner and turn lifecycle.
Bindings are bounded by the existing 32 KiB name/argument binding limit,
held as immutable UTF-8 bytes, and excluded from JSON task metadata. Queries
return at most 16 bindings and execute at most 16 times per program; inspection
does not load result bodies. A synthetic 32-capture / capacity-16 measurement
with 30,000-character argument values retains 16 entries, 48 source bytes and
480,608 binding bytes; the charged set is 500,784 bytes under 1,048,576.
One directory page encodes to 482,233 bytes. Python tracemalloc peak is
2,627,177 bytes across capture and query; this is not RSS. No storage reads,
writes, resident workers or durable state are introduced. Verify with
`tests/tools/test_program_result_reuse.py` and its real SDK execution contract.
Each capture has a versioned 38-byte runtime reference charged to the same bound.
The composer reads at most four metadata records without decoding argument bytes,
emits at most 256 content tokens / 4,096 bytes, and adds no resident cache or
source-body read. Initial global-budget admission is atomic. A 100-sample
synthetic measurement at capacities 16/1,024 emitted 199..208 tokens / 622..624
bytes; median generation was 0.60..2.89 ms with tracemalloc enabled. Peak
Python allocation was 19,565 bytes including four reference expressions;
30,000-character arguments remain outside context. This is not RSS. Context history uses existing task token/compaction
lifecycle bounds; snapshot changes do not alter the static schema or guidance.

## Application DOM resources

Problem: completed scope timeouts retained cleanup records, while boot DOM work left observers and orientation retries unowned. Decision: the shared browser scope owns listeners, task IDs and explicit cleanups; completed tasks release their record before delivery, cancellation removes ownership before native cancellation, and disposal fences queued callbacks even when cancellation throws. The application interaction composition owns one autosize frame, one scroll frame, one clipboard-feedback timer, one log-inspection timer/request plus a latest-intent bit, and one composer ResizeObserver. The separate viewport owner bounds orientation retries to two timers and resize work to one frame. No constructor starts a request or optional feature. Existing request transport bounds still apply: disposal fences a log response but does not abort that request.

Measurement: 10,000 timeout/frame cycles deliver 20,000 callbacks and retain zero task records afterward. A 100-event typing/scroll burst schedules two frames and reads composer scrollHeight twice; 100 unchanged observer notifications add no repaint. A 100-event resize/orientation burst retains one frame and two timers, then zero after disposal. These are deterministic resource/DOM-read counts, not RSS or latency claims. Edited text and conversation identity fence paste results; modal-owned off-target drops prevent tab navigation without attaching files to chat. Browser contracts exercise actual autosizing, stable reading scroll, viewport resizing, shortcuts and positive/stale paste responses at 390 and 1180 pixels.

Alternatives rejected: retaining per-timeout cleanup closures grows with tab age; unbounded retries add redundant work; painting every response can overwrite a newer draft. Seven explicit owners improve source context and cleanup behavior; measured gzip-9 main JavaScript grows 402,955 → 404,665 bytes and the complete module graph grows 1,460,032 → 1,461,734 bytes; download reduction is not claimed and no delivery budget is raised. Verification: `tests/application_interactions.test.cjs`, `tests/visual/test_visual_application_interactions.py`, native lifecycle/sync browser contracts and the frontend gate.

## Composer model picker

Problem: a 1,747-line retained toolbar mixed model projection, rendering, search and popup resources; provider rows captured an obsolete preference. Decision: nine typed owners (largest 77 lines) separate catalog joins, display planning, row/fold rendering, render-local search, glyphs, selection contracts and DOM composition. Canonical DTOs stay generated and persistence remains in retained model actions. Search indexes rows once per render, aggregates visible sections in one pass and never interpolates canonical IDs into selectors; Map grouping and explicit section namespaces accept arbitrary canonical IDs without colliding with pending or recent groups. Each open provider popup owns its dismissal listeners and rolls back failed setup. Selection repaint uses complete selection identity and skips unchanged state; current provider preference is read on open, with one actionable option per Provider ID; multiple Offerings remain catalog metadata because preference cannot pin an Offering or Access. Native factory buttons support keyboard selection and fold disclosure, while the existing menu-row factory is shared with ordinary dropdowns. Long service captions truncate within the bounded popup and expose their complete text as a title.

Measurement: a 1,000-row / 1,000-section query visits 1,000 rows, while repeating it visits zero; clearing visits 1,000. A 100-call unchanged selection refresh performs no row class writes. Catalog capabilities union once per model instead of rebuilding/sorting per offering. These count retained work rather than RSS/latency. Detached or superseded rows cannot dispatch actions; disposal removes the popup/listeners and fences retained callbacks. No constructor starts network work or optional features, and no deferred outside-click timer is needed. Alternative rejected: classic fragments preserve ambient globals; row closures freeze service preference; per-section selector scans multiply work; arbitrary source splitting leaves ownership hidden. Verify with `tests/model_picker.test.cjs`, sorting/fold/footer/glyph contracts and shipped picker/startup browser contracts. In the isolated task graph, measured gzip-9 main JavaScript grows 404,665 → 406,094 bytes and the complete graph grows 1,461,734 → 1,462,548; critical CSS grows 1,275,273 → 1,275,583 raw bytes (274,293 → 274,376 gzip). This buys explicit ownership and keyboard controls; delivery remains larger and no budget is raised. Concurrent API/backend edits require a fresh integration snapshot for the browser gate rather than reusing an obsolete engine.

## Native Codex login discovery

Problem: services missed the user's installed Codex environment, while spawning
its runtime for every token read would create needless latency and memory work.
Decision: the native-login adapter has one metadata cache (30 seconds, failures
5 seconds) and one last-verified metadata snapshot (absolute age 120 seconds),
one serialized flight for the process OS credential authority and no bearer
cache. One lifecycle-owned native metadata session avoids repeated shell/cold
startup during metadata revalidation. Its reader and idle reaper are lazy;
there is no import-time process launch. Fresh command metadata is read without
the native-auth lock, so healthy access-file reads do not wait for renewal.
Concurrent recovery callers join one completed probe even when it fails, rather
than queueing repeated native launches. Timeout/unavailability may reuse the
verified source within its absolute age, with live source/config stamps and live
bearer checks; policy rejection/source change revokes it, and failures never
renew its age. Freshness expiry starts one demand-driven revalidation worker
while healthy reads return immediately; clear/exit drain it. Revalidation uses
the warm session's config/read and configRequirements/read, never account/read.
The idle reaper terminates/reaps the process and joins its reader after 60 seconds
without a metadata exchange, or 120 seconds from startup. Demand rotates at
90 seconds to rediscover shell hooks/remote policy before the absolute age limit;
warm success cannot renew that startup policy age. Steady residency is at most
one native process and two adapter threads (reader + reaper); demand revalidation
adds one finite worker. Renewal can temporarily add a second, one-shot process
and reader, serialized against metadata exchanges. No resident pool scales with
request/account count. Unsupported stores retain no optional native session.
Launcher provenance hashes the entire sanitized environment, retaining no raw
values. Known source/config stamps invalidate reuse, and stamps are checked
around policy reads. Native diagnostics name spawn/initialize_sent/
initialize_received/session_reuse at debug level and lock/initialize/config/
requirements/renewal timeouts at warning level, without protocol bodies, paths,
stderr or environment. Pre-response initialize timeout still cannot distinguish
shell setup from native boot: no safe acknowledgement exists at that boundary.
Task preparation retries have three attempts
and a 30-second admission window, with cancellation-aware backoff and no tool
replay, new thread or persistent state. Each native transport owns its reader
and process group. Discovery plus renewal share a 4-second protocol
budget including lock waits, with 1 MiB stdout, 64 KiB per line and
16 queued protocol messages; teardown terminates/reaps children and joins the
reader. Catalogue startup reconciliation already runs in the post-ready worker.
Native Codex's own reconstructible artifacts stay under its lifecycle; Tofu
creates no durable cache or copied token. Account-file replacement/logout is
checked on every read; cached command metadata cannot mask token deletion.
Measurement on this host with Codex 0.160.0: automatic discovery without a
CODEX_HOME override took 1.795 seconds with child peak RSS 180,336 KiB; a following
access-only read took 1 ms. That historical one-shot implementation had zero
idle workers/processes; the reusable transport now retains bounded residency
until idle reclamation. This is a
native-runtime probe cost, not inference throughput or a resource-budget raise.
Additional measurement on this host: native discovery took 0.332, 0.299 and
0.295 seconds across three probes; 50 healthy reads had a 0.325 ms median and
0.426 ms maximum. The protocol budget
remains 4 seconds; extending the timeout would add latency without correcting
lock contention or duplicate recovery launches.
Alternatives rejected: shell-profile text parsing misses wrappers and layering;
per-read runtime launches amplify cost; retaining a second refresh token races
the native credential authority. Behavioral tests cover source selection,
single-flight, live file replacement, renewal and bounded teardown.
Historical metadata-only measurement on this host: 200 synthetic access-file reads with source
stamp verification took 0.932 ms median / 1.085 ms maximum with tracemalloc active;
one initial probe, zero subsequent probes and zero new idle workers. Traced peak
allocation was 1,082,838 bytes, including the existing bounded file reader.
Alternative rejected: indefinitely retaining last-good policy would bypass
revocation; raising every native-command timeout would leave startup on the
critical path and add latency to healthy requests.

New synthetic lifecycle measurement: a delayed initial boot plus four cache
expiries with 16 concurrent metadata callers each used one initialize and five
config/requirements exchanges, with zero account reads. This is subprocess
startup amortization, not token caching or an inference-throughput claim.
Idle reclamation, absolute-age expiry and generation-fenced clear/restart tests
verify that owned processes/readers/reapers exit rather than leak.

## Stage counter contention experiment

Problem: always-on stage timers perform four global atomic writes even when
request sampling is off. Decision: retain the existing fixed counters. A private
probe-sized, lazy component-stripe prototype cuts writes to two, preserves
pre-launch totals and thread-exit counts, and aggregates reporting once. Its
first serving probe bounds slots by CPU/connection capacity; 22,400 counter
bytes per slot persist until process exit (allocator/registry overhead excluded).

Alternatives: original global counters, stripes, and identical CLI/DSO bytes
with stripes disabled only in disposable children. The matched-source variant
regresses array CPU about 6–11% and metrics polling 10%. The same-library
control shows only mixed scalar/array CPU gains up to about 3%; polling still
regresses about 7%. Four JSON-kernel symbols retain sizes/mnemonic sequences
but shift alignment; this does not prove an exclusive code-layout cause.
[Evidence](evidence/tofudb_stage_stripes_20261005.json) retains 14,118,400 exact
reads, 16,000 validated timed metrics polls and 153,600 untimed warming reads,
5 counter / 6 control / 12 FFI focused passes and the later 5-pass counter
control. No full suite, production ABI/setting/metrics change, pool/queue change
or SQLite dominance is claimed. Counter contention is not the main read gap.

## Portable reply experiment under mimalloc

Problem: reply allocation and retirement could still explain the read gap after
the accepted allocator update. Decision: retain standard MPSC replies. The
private alternative uses a unique atomic slot and standard Thread park/unpark,
preserving cancellation, sender disappearance, timeout/retry and receiver
transfer between threads. All old direct futex syscalls were removed; their
wake/filter error boundary was unproved. No immutable waiter shortcut was used.

Identical CLI/DSO bytes switch implementations only in disposable children; an
observer verifies the actual selected backend. Across 7,059,200 exact timed
reads, native single-caller CPU improves about 3% and SDK small-response CPU
about 8%, but native 32-caller means regress 2–5% and SDK concurrent P99 regresses
12–13%. The average per-leg P99 is not a pooled percentile. Both backends use
mimalloc; old System allocation observations are not reused as new evidence.

A separate 3,000-read sampling observer sees recycle fall from 170 to 77 ns,
acquire rise from 75 to 91 ns, queue wait remain around 4.5 us and reply handoff
around 3.7–3.9 us. These mixed durations overlap and are not additive CPU costs.
Reply-object replacement is insufficient; scheduling/parking contention remains
a hypothesis for the concurrent regression, not a uniquely established cause.
[Evidence](evidence/tofudb_portable_reply_20261005.json) retains 12 reply, 12 FFI
per backend and 4 actual-source Loom passes, raw hashes and the private prototype
commit. No full suite, runtime ABI/setting change or SQLite dominance is claimed.
The previously released database is activated: live daemon bytes match the
published CLI, with native TofuDB authority preserved; no restart was performed.

## Exact frame admission and coalesced daemon reads

Problem: every tiny outbound frame reserved 8 MiB before encoding. With a
128 MiB frame budget and 512 probed connections, only 16 simultaneously blocked
writes fit. Decision: count encoded bytes with the same FrameBodySink operations
that emit them, validate/admit before allocating, and allocate the counted size.
The guard lasts through CRC, short/interrupted writes and errors. No limit rises.
A deterministic 32-writer test admits 16/rejects 16 on the old path; the new path
admits all 32 at the same limits and releases every byte/frame afterward.

Length-prefix reads now accept up to four bytes; the admitted body and four-byte
CRC trailer share one bounded read. No following frame is prefetched. Mid-frame
timeout, CRC, truncated-body/trailer error classes and bearer-body clearing stay
intact. Full available frames use two reads instead of four. The separate syscall
observer records 4,013 versus 2,007 recvfrom calls, with other reads unchanged.
Two opt-in anchors measure frame work after initial prefix progress and decode;
they exclude idle first-byte waiting and overlap outside semantic dispatch.
They add 256 fixed counter bytes until process exit. CRC relocation adds at most
four requested heap bytes per live read frame (256 at the unchanged ceiling);
allocator rounding is separate. Pool, connection/frame caps and durable growth
are unchanged. Examples avoid duplicate allocators when the library opts in.

Alternatives: pessimistic maximum reservations starve small responses; a separate
length table risks codec drift; encode-before-admit evades memory admission;
prefetch buffering retains following-frame/bearer bytes and changes accounting.
Private toggles and failure probes were removed from the permanent implementation.
[Evidence](evidence/tofudb_frame_admission_20261005.json) retains 8,595,200 exact
timed reads, separate syscall sampling, all failed setups and adverse rows. Final
means improve about 3–5% on native RPC and 1–4% on SDK cases. Overlay single-array
P99 still worsens 7.4%; no all-tail or SQLite dominance claim. Full native Cargo
passes 1,601 tests (two existing ignores); default examples compile. Publication
checks pass 53 initially and two timeout cases on isolated rerun. Publication and
activation follow the existing lifecycle; backend/identity are preserved.

## Native archive preparation and authority-hold attribution

Problem: archive windows encoded large pages under the foreground mutex, and
`Ok(false)` kept the worker spinning after catch-up. Decision: build each page
on an owner-scoped, accounted preparation replica; commit validates source and
manifest witnesses before publishing private blobs. Only `Ok(true)` continues
catch-up. Read-only replicas alone leave compression inside the hold.
Private payloads use the existing SnapshotPin transaction budget; one page has
at most 128 messages and the existing 64 MiB document bound. Leases end at
commit/abort. Optional request profiling adds 408 bytes of fixed hold counters
and 16 bytes of accepted-page/message counters per loaded native module; timers
use 40 stack bytes (previously 32). New probes read no clock or emit warnings
when disabled. The existing slow-hold warnings retain their threshold.
`requestProfile.authorityHolds` has 17 fixed phase names, completed wall-time
totals/maxima/calls, and approximate process-lifetime snapshots. Nested phases
are not additive or per-authority causal evidence. Archive acceptance counts
accepted background commits, never a durable prefix or per-owner certificate.
[Measurement and alternatives](evidence/tofudb_archive_authority_20261005.json)
retain all default-off paired legs, exact readbacks, fault checks and observers.
Long small-text P99 rose 10.4%; long-run process RSS rose 1.4–6.2%. Large-text
mean/P99/CPU improved; universal SQLite or steady-state dominance remains open.

## Immutable private block publication

Problem: private preparation hashed and copied encoded blocks, then publication
hashed and copied the same immutable bytes again under authority. Decision:
`PreparedBlockSource` binds identity to its borrowed source, and capture mints an
immutable `PreparedBlockPayload`; publication retains its Arc allocation through
the ordinary pending/durable dedup and WAL path. Source mutation after capture
and copy-on-write reader mutation cannot alter the bound payload. Business/OCC
validation still precedes publication; rotation accounting and optional loose
duplicate WAL carrying are preserved. No wire, durability or storage format changes.

Each private block reserves 160 metadata bytes instead of 128, including the
additional 32-byte identity; the existing transaction lease ends at commit/abort.
The optional `prepared_blob_publish` stage adds 128 fixed counter bytes per native
module plus 24 bytes per bounded profile record. It measures private-store locking
and block publication after lease validation, excluding WAL and commit. Disabled
diagnostic probes read no clock and update no new counters. No pool, cache or
persistent-growth limit rises. Existing component labels are classification only;
an archive worker labelled foreground does not establish foreground CPU cost.

Alternatives: accepting caller-supplied IDs loses provenance; skipping verified
dedup or WAL carrying weakens persistence; replacing the captured bytes with a
mutable shared buffer breaks identity. A first test incorrectly expected a cold
loose duplicate to suppress WAL carrying; the test was corrected, policy retained.
[Evidence](evidence/tofudb_private_blob_publication_20261005.json) retains all
paired default-off legs and adverse rows. Long unique-text mean/P99 improve about
5%, CPU 1.8%, process RSS 1.6%; long small-text mean/P99 worsen 1.4%/3.8%, and
read controls worsen 2.2%/3.4%. Shared-host observations are finite cohorts, not
steady-state or universal SQLite dominance. All 198,680 default-off whole-content
checks and three replay/conflict/reopen patterns pass. Native tests pass 1,569
with two existing ignores. Zero cache/proof-reuse counters defeat that attribution.
The separate 1,000-write observer records 1,009 private publications totaling
27.7 ms, while archive publication still reaches 32.7 ms; remaining commit work
needs further attribution. Profiled timings are excluded from production gains.

## Producer-bound receipt policy

Problem: receipt eligibility reparsed the full generated response merely to
exclude root null and root `ok:false`; escaping made this about 0.98 ms per large
response. Decision: only `turn.rs` mints immutable `SettledAppendResponse`, whose
fixed root has attempt/conversationRevision/turn and cannot express either case.
Normal staging validates raw projection inputs first. Eligibility follows only
the actually selected allocation through IrSlots; mutation, replacement,
projection overwrite, extraction, literal/missing/raw results and serialized
replay clear or lack it. It is never a JSON/depth integrity certificate; the
ordinary final validation and all owner, OCC, deadline, receipt and durability
checks remain. No request, response or storage encoding changes.

An IR frame uses 64 additional stack bytes (IrSlots 1,600 -> 1,664); IrResponse
remains 32 bytes. One optional work counter and two diagnostic stage records add
56 bytes per active/sample/aggregate profile record (observed active 7,232 ->
7,288). `receipt_encode` includes validation, compression and assembly;
`receipt_compress` is nested compressor construction/write/finish only. These
add 256 process/component counter bytes per loaded native module. New diagnostics
read no clock or update new counters when disabled. Collector bounds/lifetimes
and all pool, cache, growth and durability limits remain unchanged.

Alternatives: operation-name trust does not follow selected/mutated IR bytes;
unchecked arbitrary-byte flags lose producer provenance; streaming JSON policy
still decodes escaped content. The prior producer-proof experiment was rejected
for large random-content P99 regression before archive-lock repair; it remains
in history. This fresh comparison retains every adverse row rather than replacing
it. [Evidence](evidence/tofudb_receipt_proof_retry_20261005.json) records 198,680
default-off complete checks, three replay/conflict/reopen patterns and all native
tests (1,571 passed, two existing ignores). Long escaped mean/CPU improve about
10%, P99 16%; random mean/P99 remain nearly flat but timed process CPU worsens
3.9%, small-text P99 worsens 6.1%, read mean/P99 worsen 2.2%/1.7%, and escaped RSS
worsens 1.8%. Shared-host process CPU includes generation and concurrent workers;
RSS is process high-water, not steady-state core heap. The separate observer
confirms receipt-policy work near 0.1 us; random-content receipt compression
still costs about 1.58 ms. Immutable response reuse needs investigation beyond
codec replacement. SQLite references, maintenance waits, missing coverage and
failed setups remain in the evidence; no universal performance or activation claim.

## Storage JSON admission shape dispatch

Problem: the client traversed every ordinary JSON scalar through abstract
Mapping/type checks, and mapping materialization repeated them before encoding.
Decision: dispatch exact built-in scalars/containers directly; subclasses and
registered Mapping views retain the original generic checks and ordering.
Non-finite floats still fail before orjson can coerce them, aliased/cyclic
containers are visited once, and unknown values still reach the original
serialization error boundary. Canonical/frame bytes and owner adaptation stay
unchanged. This is shared ingress work, not a new native kernel or query lane.

Two temporary references add 16 bytes of Python validator frame slots and one
adds eight bytes per materializer frame; no new retained pool, cache, thread,
queue, durable state or bound. Timing is observer-only: cProfile/function-edge
and existing native queue/dispatch/reply anchors run separately from credited
default-off measurements. Inclusive wall/CPU counts are not additive; cProfile
overhead does not establish production phase cost.

Alternatives: remove number admission (silently coerces NaN/Infinity), dispatch
subclasses as plain scalars (hides Mapping values), memoize mutable input identity
(stale evidence), or bypass watchdog delivery (changes timeout/lifetime safety).
The comparison driver separately stopped allocating unused command IDs for reads;
writes still allocate once and reuse that ID on classified retries. This is a
measurement correction, excluded from production ingress gains.
[Evidence](evidence/tofudb_point_ingress_20261005.json) preserves old-driver BCCBCBBC
legs, independently corrected-driver controls, exact native/codec checks, Python
source hashes and unchanged native build identity. Point mean/P99 improve about
8.7%/8.6%, CPU 6.8%; 2,048-integer record writes improve about 61%/58%, CPU 62%.
These finite shared-host cohorts do not prove SQLite dominance, active-streaming
capacity, FUSE or steady-state memory. Cross-thread handoff remains material. Embedded client decoding now passes a temporary memoryview of the caller-owned thread-local output to orjson, removing its response-sized bytes copy. A successful parsed tree owns all values before buffer reuse; the stock decoder fallback keeps the original UTF-8 copy boundary. No pool/thread/cache/file or retained bound changes. The temporary view retires after synchronous decoding (an exception traceback can retain its safely owned buffer reference). Python-only allocation traces and end-to-end cohorts, with limitations, are retained in [client response evidence](evidence/tofudb_client_response_view_20261005.json).

Project direct-write recovery uses `TOFU_PROJECT_WRITE_RECOVERY_MAX_MIB`, a
restart-only per-file allowance independent of read/display caps. Personal
admission scales with launch memory capacity, memory headroom and free volume;
its fallback is 4 MiB, distributed fallback 16 MiB, and the inline-evidence
safety ceiling is 1024 MiB. See [project undo](PROJECT_FILE_UNDO.md) for the
encoding envelope, durable-history lifecycle and executable measurements.

## Project undo payload working set

The undo index cache keeps the existing launch-resolved total byte/session
budget. A root may use the available total rather than a fixed quarter, with
LRU eviction across roots. Immutable pre-images and reverse patches stay on
disk and load only after selection. Packing uses at most that byte budget or
one required value during migration, with no resident payload cache or worker.
Packs are reclaimed after the final indexed reference is successfully removed;
durable pending undo records are never aged out to improve performance.

A same-host five-file benchmark using a private copy of the roughly 320 MB
production journal took 29.242 seconds initially and 28.843 seconds on the next
original-tool batch. With packed contents and compact metadata encoding, the
first batch including one-time conversion took 5.909 seconds; the next batch
took 0.927 seconds. The mutable index shrank to about 3.6 MB; original contents
remain durable in immutable packs. These measurements include normal tool
write/undo preparation and file-history work on the same FUSE volume, with a
running application, not just text replacement or a mock storage backend.

Translation idle reuse follows the same probed CPU/RSS root capacity as the
agent pool. Independent jobs and provider calls start on demand; the former
worker/admission and provider-waiter ceilings no longer govern them. The earlier
controlled 96-job study on 63 effective CPUs with 208,790 MiB memory headroom,
using 25-ms simulated provider I/O, took 1.206 s with two workers and 0.059 s with
48 derived workers. That historical comparison measures local scheduling, not
an upstream latency promise or the current demand-driven executor. Actual
provider responses and cancellation remain authoritative.

## Demand-driven desktop, video and paper work

Problem: local desktop/video semaphores, paper document/subscriber quotas and
reader/restore slots rejected otherwise valid work. Desktop active state also
shared the evictable terminal cache, so removing admission alone could lose
status and permit duplicate work. Reader metadata bytes imposed a hidden quota.

Decision: start work on demand and retain active owner/device state until
settlement. Video reservations represent cleanup ownership only. Paper workers
accept separate documents immediately; same-document freshness still coalesces
revisions. Subscribers, cache readers and durable read leases have no count
quota. Reader identities expire after the existing lease lifetime; release and
renewal preserve owner checks. Restores use unique private stages. Chunked reads,
receipt verification, cancellation joins and storage authority remain intact.

Alternatives considered: increasing fixed slots would still reject bursts;
keeping active tasks in the TTL cache would lose lifecycle ownership; dropping
read leases would allow reclamation during downloads. Tests start 144 desktop
workers with a one-entry terminal cache, acquire 256 video ownership tokens,
retain 128 cache/durable readers and stream a 9 MiB artifact with under 4 MiB
peak Python allocation. Active bookkeeping grows with live demand and is
released on settlement; reader metadata grows with unexpired identities only.

## Device execution and transport ownership

Problem: device execution refused beyond four workers, paper builds had another
four-job ceiling, and the replay ledger refused beyond 65,536 IDs. Result/stream
queues blocked task settlement behind fixed item/byte budgets; the browser relay
fell back after its two-job FIFO filled and retained timed-out queue entries.

Decision: device execution and paper builds start on demand while identities
remain known until the run ends. Transport frame limits constrain one poll, not
producer admission. Pending serialized bytes reuse 1/128 of launch-probed
capacity/headroom (1 MiB lean fallback), split 7/8 results and 1/8 streams;
overflow uses private temporary spools. A validated reply acknowledges only its
sent prefix and frees those bytes/files. Actual storage failures are explicit.
Stopping cancels project streams and waits for real execution settlement before
reclaiming transport state. Browser relay jobs have an uncapped live FIFO with
timeout/completion/close cleanup and immediate close notification.

Alternatives considered: larger quotas still refuse demand; evicting old IDs
replays side effects; retaining all payloads in RAM magnifies stalled transport;
spooling every small healthy-path message adds avoidable disk traffic; returning
from shutdown while processes/readers remain alive loses resource ownership.

Measurements on this host: 512 unacknowledged 32 KiB frames held 16,790,418
serialized bytes with 368,437 peak Python allocation bytes in the disk-overflow
path; append took 0.105 s and teardown left zero pending bytes. This measures
local spool throughput, not gateway latency or disk durability. Retaining 70,000
command identities used 7,403,402 peak Python allocation bytes; IDs are released
at run teardown. Coverage includes a real 32-command poll burst, replay, invalid
reply retransmission, spool-write failure, prefix reclamation, 24 concurrent relay
jobs, process cancellation/start failure, pipe-reader joins and child exit status.

## Desktop device resources

Problem: fixed worker clamps underused larger devices, while lifetime replay
retention grew indefinitely. Decision: `lib/desktop_agent/_runtime.py` preserves
demand-driven local admission and the existing private output spool. Negotiated
server delivery credits intersect effective CPUs with available GiB from the
cached launch probe, falling back to one on probe failure. The retired worker
override cannot reject issued commands. Resident output memory uses 1/128 of
capacity/headroom (1 MiB lean fallback), split 7/8 results and 1/8 streams; larger
output spools to private temporary files. Frames and HTTP prefixes stay bounded
independently of pending disk output. Actual storage errors remain visible.
Spool ACK deletes files promptly; shutdown joins owned work before reclaiming
spools. One poll Session closes at run exit. Finite-deadline replay entries retire
only after completion, expiry and acknowledgement, using a monotonic server-clock
watermark and expiry heap; legacy records stay pinned until joined shutdown.
Alternatives rejected: deleting IDs without command expiry permits repeated side
effects; a larger whole HTTP body can exceed request-size limits and block ACK
forever; abandoning workers before spool cleanup loses owned output lifecycle.

Reproduce the offline admission/reclamation measurement with
`python benchmarks/desktop_runtime.py --commands 100000`. The 1s synthetic pickup
TTL and 0.01s clock tick retain at most 101 IDs and finish with 100, exceeding the
old 65,536 lifetime ceiling without restart. Three timing samples completed 100,000 transitions
in 0.308–0.310s (median 323,753/s); a separate tracemalloc run reports residency; this is a kernel measurement, not actual
tool throughput, process RSS, provider performance or multi-client certification.
A loopback HTTP regression verifies two polls reuse one TCP connection.

## Browser request and lease ownership

Problem: local poll concurrency/RPM gates, device/waiter registration and tab
lease quotas refused otherwise valid owned requests. Workflow owner gates,
eight-step/five-item defaults and two-item waves hid available execution
capacity. Login-capture quotas refused independent routes and fixed cooldown
maps could evict still-live pacing state.

Decision: retain exact live HTTP request ownership without quota/TTL admission,
without credential/owner/global rate buckets or inherited protocol cooldowns.
Authentication and the current frame still own identity and protocol validity.
Device/waiter/lease state grows with live demand and leaves through disconnect,
timeout, cancellation and settlement; one lease sweeper handles every timed
lease. Same-device long-poll replacement remains atomic. Workflows expand all
results by default and start independent reads on demand; explicit caller limits,
serial selection, output bytes, deadlines and site-declared pacing remain. Login
capture still deduplicates its exact owner/device/domain and rolls back worker
start failures; it no longer refuses other routes. Route cooldowns expire by
time without count eviction. Remaining affinity cache sizing is reconstructible
metadata, not device admission.

Alternatives considered: larger quotas still reject bursts; silently evicting
leases loses tab/capture ownership; reusing a prior credential protocol failure
skips current authentication and can delay an upgraded client; removing genuine
site pacing can trigger upstream challenges; discarding partial workflow receipts
hides completed reads. Coverage includes 4,096 simultaneous poll leases, 256
devices/leases, 128 async waiters, a 32-read workflow fan-out, concurrent same-owner
workflows, real authentication/protocol failures, rollback and resource cleanup.

A controlled local ownership measurement registered and owner-bound 4,096 live
poll requests in 0.0508 s with 1,173,684 peak Python allocation bytes; release
returned the live registry to zero. This measures ownership bookkeeping, not
network/browser throughput or upstream site latency.

## Extension resources and file-transfer demand

Problem: temporary tabs, captures, debugger sessions, observers and breakpoints
had independent fixed admission counts. Restart recovery read only four tab
IDs. File transfers reserved every advertised maximum and rejected bursts by
owner/process count or cache targets, even when actual storage was available.
Completed metadata TTL could delete a file before its handoff grace expired.

Decision: own browser resources until settlement without count quotas, recover
every exact persisted temporary-tab ID, and retain same-tab CDP coordination,
cancellation, reapers and real Chrome errors. Transfers start without reserving
hypothetical growth; actual bytes retain free-space, order, hash and atomic
commit checks. Eligible old completed staging can be reclaimed by byte/count
cache targets; live and fresh artifacts never cause admission refusal. Expired
completed metadata leaves files under cache TTL/grace, while unfinished/aborted
files still leave through their existing cleanup owner.

Alternatives considered: increasing quotas still rejects demand; forgetting
owned IDs strands tabs after worker restart; evicting active resources loses
claim ownership; reserving every maximum charges bytes not yet written; removing
actual-write checks weakens the durable volume reserve; deleting fresh completed
artifacts makes returned receipts unusable. Tests start twelve held tab creates,
recover eight persisted IDs, own 64 captures/16 debugger sessions/16 observers
and 40 breakpoints, and create 128 maximum-size transfer descriptors without
reserved disk growth. Every burst releases its owned resources; content payloads
and observations keep their existing finite per-resource bounds.

Same-tab concurrent debugger starts coalesce their initialization and retain one
physical CDP holder; failure/cancellation settle that owner without publishing
a late active session or deleting another generation's registration.

The Node worker probe owned 64 captures, 16 debugger sessions, 16 observers and
40 breakpoints, then released every runtime registry in 212 ms with 1,657,232
bytes of net V8 heap growth. Chrome APIs were controlled test seams and the
observer window was 200 ms; this measures worker bookkeeping, not Chromium
renderer memory, page/network latency or a production concurrency guarantee.
Detach deadlines are cancelled after actual completion instead of leaving
five-second timers alive for every reclaimed CDP holder.

## MCP and custom-tool request ownership

Problem: a four-owner MCP registry evicted idle owners or refused requests while
an unrelated transport was live. Credential diagnostics used process slots,
custom client replies had a task-derived waiter quota, and live tool environments
had a 1,024-handle ceiling. Default definition count rejected more than 32 tools.
An unused hour-long disposal helper contradicted real terminal ownership.

Decision: retain owner bridge generations without registry quota/eviction;
independent credential probes start on demand with exact owner/server dedup,
finite actual reads and start-failure rollback. Custom environments and handoffs
remain until their exact terminal owner releases them. Definition counts are
unlimited by default; explicit positive caller limits, schema/result bytes,
approval, visibility, callback identity and actual timeouts remain enforced.
Real same-owner shutdown/startup fencing stays intact while unrelated owners
continue to resolve independently. Retired count settings leave the launch
manifest and its generated projection.

Alternatives considered: larger quotas still refuse demand; dropping an active
bridge loses loop/process cleanup ownership; creating a replacement for a still
stopping same-owner generation weakens lifecycle fencing; removing callback
identity permits cross-task or cross-owner result injection; timer-expiring live
environments makes valid long-running tasks lose their declared tools. Coverage
includes 128 independent bridges, probe overlap/dedup and worker failure, 96
concurrent handoffs with cross-owner rejection, 1,100 live environments, 128
request definitions, real terminal disposal and loop-generation rollback.

Measurement (Python 3.12, this host): minting 128 lazy owner bridges and 1,100
custom environments together took 701.94 ms with a 3,378,481-byte tracemalloc
peak (3,372,084 bytes live before cleanup). Both registries returned to zero
after explicit disposal. No transport loops started; audit-log output was
disabled for this bookkeeping probe. These figures measure Python registry
allocation, not provider processes, external latency or production throughput.

## Translation and VLM work without count admission

Problem: whole-turn, PPTX, paper, attended-send and VLM jobs waited for a shared
worker ceiling or were refused by a finite queue; translation calls encountered
a second active/waiting provider gate. PDF task results could be count-evicted
before a client retrieved them. The same background lane also serves provider
probes, so changing only translation would leave the generic admission flaw.

Decision: carrier demand equals active plus pending jobs, preserving explicit
job/owner identity and round-robin selection only for transient pickup. Worker
counts govern idle reuse; surplus idle carriers retire immediately and reusable
ones follow the configured quiet timeout. Completed input closures and outputs
are released before an idle wait, and metric failures cannot deny admission or
interrupt settlement. Queue counts cannot deny demand.
Actual startup failure rolls back the newly admitted job; duplicate IDs and
shutdown remain genuine errors. Translation provider contexts count exact live
calls and release in finally without a count permit. Actual billing/provider
errors, deadlines, cancellation, validation and committed result ownership stay
at their existing boundaries. Fresh PDF results retain their declared retrieval
TTL. The two retired queue settings leave the launch manifest and projection;
obsolete local saturation exceptions and HTTP refusals are removed.

Alternatives considered: larger finite queues still reject demand; removing
identity would permit callback/cancellation crossover; unlimited permanently
resident threads would keep burst cost after its work ends; dropping terminal
results on count pressure loses successful work; bypassing actual provider or
billing failures would misattribute external limits to local scheduling.

Verification includes 64 simultaneously entered lane jobs, 64 provider calls and
64 whole-PDF tasks, exact owner access, fresh result retrieval, cancellation
notification races, delayed-pickup round-robin selection, worker-start rollback,
actual provider failure and observational metric failure. Controlled pickup
fixtures intentionally delay expansion to expose otherwise transient pending
states; burst tests exercise the production demand-driven implementation.

Measurement (Python 3.12, this host): 64 controlled I/O holders entered together
in 23.17 ms with zero queued jobs, 815,104 bytes of process RSS growth and a
376,600-byte tracemalloc peak (366,306 bytes live). All jobs settled at
35.64 ms; all 64 carriers retired at 66.31 ms without shutdown, leaving zero
active, queued or resident threads and 64 accepted / zero rejected jobs. This
measures local carrier bookkeeping, not PDF rendering memory, upstream inference
latency or provider capacity. Idle-reuse lifetime tests also verify completed
input/output objects are released while their reusable carrier remains alive.

## Knowledge-owner and production recovery admission

Problem: a finite enrichment-owner registry deferred an opted-in corpus and
restart recovery enumerated only its configured owner count. Production restore
also refused a valid saved job when unrelated live carriers filled a registry.

Decision: start independent knowledge owners on demand; reuse counts govern
idle threads only. Preserve same-owner coalescing, stop/revive, durable per-asset
claims, vision authorization and actual startup rollback. Recovery walks ordered
512-row pages with an exclusive after_user_id cursor through the repository,
SQL and native authorities; page size bounds transport without truncating total
owners. Native reads seek the cursor and stream bounded index pages instead of
materializing a tenant-global inventory with a scan ceiling. Production restore
retains exact identity/owner checks but drops registry-count admission refusal.
The retired owner setting leaves the manifest and its generated projection.

Alternatives considered: raising owner counts still omits or refuses demand;
one unbounded inventory response retains all owners and has no transport bound;
restarting same-owner work concurrently loses stop and asset-claim ownership;
dropping owner checks would expose another owner's resumed work. The existing
one-asset-per-owner-turn policy remains a separate scheduling path to audit;
these changes do not establish full within-corpus inference throughput.

Verification covers 64 overlapping independent owners, 1,200 resumed owners,
cursor advancement failure, 128 restored live jobs, duplicate/foreign identity,
real native and SQL owner-index pagination, and idle worker retirement.

Measurement (Python 3.12, this host): 64 controlled owner callbacks entered in
19.66 ms with zero queued owners, 815,104 bytes of RSS growth and a
319,486-byte tracemalloc peak (309,192 bytes live). At 62.5 ms all
64 carriers had retired without shutdown: zero active, queued, retained owners
or resident threads; 64 accepted / zero rejected callbacks. This measures local
owner bookkeeping, excluding paid vision inference and repository asset I/O.
Native inventory tests restore 520 real owner-index entries and traverse every
page, including tenant isolation and cursor validation.

## Durable paper storage and saved reading without count permits

Problem: native and SQL retained/saved-paper handlers still refused work behind
aggregate artifact bytes, 128 records, 512 associations and 16 readers. The native
IR generator still projected retired fields. Browser reading state capped dirty
documents and serialized every document behind one normal request. Removing
admission without changing the old fixed-prefix reclamation scan would leave
expired builds hidden behind the first protected page.

Decision: retain transaction-coupled usage accounting without count/total-byte
admission. Preserve overflow/corruption checks, immutable digests, exact owner,
lease expiry, requested generation and publication rollback. Native/SQL prune
uses a persistent owner cursor, 128-row transport pages and 32-chunk deletion
batches; an unfinished artifact keeps its cursor. Saved-reading writes start
independently by document while same-document state still coalesces and keeps
writer/sequence/revision fences. Released entries survive only their pending
write lifetime; overall close retains its existing deadline. Canonical schemas,
native/application/frontend projections and launch defaults retire the quotas.

Alternatives considered: increasing quotas still refuses demand; evicting pinned
manuscripts loses durable user state; dropping writer identity lets old responses
overwrite a newer window; unbounded reclamation transactions weaken failure and
memory ownership; rescanning only the first page starves later expired builds.
Reader metadata still occupies the existing bounded metadata document; separate
lease-record layout and remaining parser/compile policy stay on the full audit.

Verification includes 256 overlapping native reader leases, 256 saved documents,
1,025 conversation associations, legacy ledger counts beyond old quotas, exact
numeric overflow refusal, every upload/publication/reclamation I/O fault matrix,
recovery after reopening, and a full protected GC page before an expired build.
A locally served real-browser fixture starts 256 saved-reading requests without
local refusal, releases every document entry after settlement, and rejects late
captures after close. The browser service port is controlled; native/SQL tests
separately verify persistent publication and transaction semantics.

Measurement (native debug engine, real local filesystem): registering 256
saved identities took 134.66 ms; the apparent file-length growth after authority
close was 351,122 bytes. Reopening verified every document identity. This
measures durable metadata on this filesystem, excluding PDF bytes, inference,
backup overhead, allocator RSS and production latency guarantees; apparent
length is distinct from physical allocated blocks.

## Classic PDF parsing without local admission slots

Problem: compressed PDFs were refused by an aggregate unfinished count, and a
fixed process pool queued valid independent work behind its worker count. Direct
text/visual/ingest paths shared that refusal; HTTP surfaces synthesized busy/503.
Submission errors could also rerun a potentially published parse inline.

Decision: count exact unfinished lifetimes without admission quotas. Each busy
pooled parse owns one worker executor; independent requests acquire another
process on demand. Launch-probed counts size idle reuse from CPU/memory headroom,
excess idle executors retire after settlement, and quiet reusable ones follow
one generation-fenced timer. Retired count settings and obsolete HTTP refusals
leave their canonical manifest. Actual worker timeout/error, caller deadlines,
parser validation, MuPDF locking, owner state and genuine startup errors stay.
Construction failure before publication alone permits one inline fallback;
ambiguous submission cleanup owns its executor through a real wait/join. A
completed parser's TimeoutError is distinct from expiry of its caller's wait.

Alternatives considered: raising slots still denies/queues demand; removing the
MuPDF lock makes in-process native parsing unsafe; reusing a busy executor merely
moves the queue; rerunning ambiguous work duplicates CPU/native execution;
releasing a timed-out live Future loses payload/process lifecycle ownership.
Direct in-process entrypoints retain the required MuPDF lock and remain a
separate isolation/performance path in the full audit.

Verification includes independent demand beyond idle reuse, exact late Future
settlement, creation/submission failure separation, actual parser TimeoutError,
generation-safe idle retirement and real process cleanup. A compatible isolated
Python launcher loads the installed PyMuPDF against glibc 2.28 without changing
the application environment or switching its storage authority.

Measurement (installed PyMuPDF, eight real one-page parses): all eight workers
entered in 1391.44 ms, held with zero local rejection despite idle reuse = 1.
Parent RSS grew 1,982,464 bytes; aggregate child RSS was 669,216,768
bytes. Results settled at 1397.16 ms and every child PID was reaped at
1459.41 ms, with zero remaining unfinished work or managed pools. The
controlled hold is a test seam; timings include cold interpreter/import startup
and this host's scheduling, not upstream inference or large-PDF throughput.

Provider probes — Problem: fixed per-task thread pools and 4,096-cell admission
made valid matrix work queue or fail before contacting a provider. Decision: requested
cells now use the shared demand executor; launch worker values bound idle reuse
only. Manual and daily matrix construction no longer inject a count sentinel.
Explicit helper slices, provider authorization, attempt/timeout bounds, same-run
publication ordering, cancellation and owner CAS remain. Failed persistence
retains a secret-free terminal snapshot instead of count-evicting fresh results.
Alternatives: a larger quota retains the same failure mode; a global semaphore would
serialize independent provider calls. Physical thread creation failures remain
real errors, and no synthetic success or duplicate inline call is introduced.
Measurement: 24 controlled cells held simultaneously at idle target 1 entered
in 5.61 ms and settled in 7.76 ms; process RSS increased by 270,336 bytes while
held, with 25 additional threads including the calling thread. Thread count
returned to baseline after settlement. This validates demand ownership and
cleanup, not real provider latency or billed-call throughput.


## Host-browser search fallback demand

Problem: the search package's 16-call semaphore silently returned unavailable
for another eligible fallback while the connected host browser could serve it.
Decision: every eligible connected fallback invokes its request-bound provider;
there is no package-wide count admission or waiting queue. The provider retains
owner/device access authority and physical call ownership. Timeout forwarding,
content checks, explicit transport policy and real unavailable/error outcomes
retain their existing behavior. Alternatives: increasing the semaphore postpones
loss, blocking acquisition queues independent work, and bypassing the host
provider would lose browser identity/access enforcement.

Controlled offline measurement: 32 blocked calls with distinct bound identities
remain live while a healthy 33rd completes within two seconds. A transport fault
settles only its own call, followed by another healthy call; all 35 calls settle,
34 succeed and one reports unavailable, with zero count-based skips and no
worker thread left after joined close. Peak provider activity is 33. No new
thread pool, durable artifact or cache is introduced. Provider work retains its
existing synchronous owner until actual return. This measures the package/host
seam, not Chromium tab capacity or real-site throughput. Gates: package browser
fallback/access/deadline/demand tests, standalone wheel smoke and host search
bridge/browser ownership tests.


## Prepared carrier construction by identity

Problem: restoration of every prepared task shared one lock and returned
`database_busy` after 500 ms, so one slow birth blocked unrelated accepted
carriers. Decision: synchronize through the shared WeakLockPool by globally
unique task ID. Each holder/waiter keeps the same lock alive through construction;
independent IDs start immediately and idle keys disappear. Duplicate restores
wait for the actual construction then verify owner, digest and unstarted lifecycle,
returning the same carrier. Same-ID recursion fails as a lifecycle error instead
of deadlocking. Domain proof/CAS, durable cancellation, ordinary factory authority
and actual storage errors retain their existing owners. Alternatives: increasing
the timeout delays global refusal, no lock can replace one resident carrier, and
a permanent ID-to-lock map retains synchronization state after useful work ends.

Controlled measurement: 32 blocked independent constructions retain 32 identity
locks while a healthy 33rd completes within two seconds. Thirty-two duplicate
restores wait beyond the old 500 ms cutoff and construct exactly one carrier;
all successful calls retire to zero lock entries. Actual factory failure releases
the key for retry; foreign owners, changed inputs and started carriers remain
refused. No worker, queue or durable record is added; weak lock/thread-local
state lives only with active construction and waiters. These are controlled
lifecycle measurements, not durable-volume or provider throughput. Gates:
prepared restore demand, weak lock pool, pending recovery, cancellation and
terminal delivery tests.


## Playwright rendering and physical completion

Problem: the synchronous browser worker serialized every fetch/search/PDF/preview
behind a queue, and caller timeouts left queued or running operations alive.
Decision: built-in browser operations share one lazy async browser owner loop;
each admitted call owns its isolated context and starts independently. Blocking
extraction/file helpers use demand workers and remain attached to their call even
when Playwright dispatches the callback separately. Context creation shields the
untransferred result and closes it on cancellation. Timeout and shutdown cancel
once, drain physical helpers and close contexts before acknowledging completion.
The server drains used browser work before dependency teardown without importing
unused search modules. Legacy sync registered handlers keep a separately owned
thread-affine browser/demand worker. Alternatives: larger serial pools retain a
queue, sharing a sync browser across threads violates affinity, and one browser
per normal request multiplies process startup/residency unnecessarily.

Controlled measurement: 32 blocked fetch contexts on one browser/loop allow a
healthy 33rd to complete within two seconds; all 33 contexts close. Runtime tests
cover startup failure fan-out, context-creation cancellation, callback helper drain
and zero calls/threads after shutdown. No durable record or retained result queue
is added. Context/helper state scales with active work and retires on settlement;
the used browser remains warm until joined process shutdown. These are offline
lifecycle measurements; real Chromium availability and whole-browser memory/CPU
must be reported separately, never inferred from fake page APIs. Gates: package
Playwright/runtime/access/deadline tests, preview/PDF policy, host shutdown,
independent wheel import and real-browser smoke where available.

## Terminal observer lifecycle

Problem: a fixed 16-observer quota rejected otherwise valid service adapters,
and callback snapshots could outlive teardown. Decision: registrations have no
count quota; each registration owns a generation and per-thread active-call
counts. External unregister withdraws future claims and joins active callbacks.
Callback-internal unregister withdraws without joining, avoiding mutual callback
waits; service teardown must occur outside callbacks. Re-registration cannot
receive an old snapshot. Required terminal delivery precedes optional observers;
receipt deduplication, transient suppression and exception isolation remain.
Alternative: a larger quota retains arbitrary rejection; observer worker queues
add ownership and shutdown costs without helping these synchronous adapters.
The controlled fixture delivers to 65 observers once, joins a blocked callback,
and leaves zero registrations/invocations after teardown; no worker is added.

## Operator identity configuration demand

Problem: local site credentials, private-host allowlists and the encrypted vault
rejected valid additions at 64/64/128 rows; cookie adapters silently retained
only 256 cookies and declarations refused more than 64 fields. Decision: retain
complete requested collections through existing atomic JSON persistence, without
aggregate count admission. Name uniqueness, required cookies, hostname/strategy
validation, encryption, redaction, permissions and per-value validation remain.
Immutable vault stages share the existing cross-process transaction, preserving
unrelated entries and duplicate-name refusal. No worker or queue is added; these
operator stores retain durable configuration until explicit edit/delete.
Alternatives: larger quotas still reject valid configuration; eviction would
silently delete credentials or reachability grants. The controlled fixture writes
193 sources (73,602 bytes, 0.564 s), 193 hosts (29,102 bytes, 0.165 s) and 257
vault entries (74,838 bytes, 0.430 s), using temporary local files. Tests reload,
update/delete late rows, preserve 257 cookies/declarations, and exercise four
processes retaining 136 immutable stages. These are fixture storage measurements,
not production throughput or peak RSS guarantees.

## Model-routing secret demand and reclamation

Problem: memory, SQL, native writes and migration restore refused the 1,025th
secret. Native lists assumed at most two pages; a fixed active-reference count
and oldest-row prune prefix prevented protecting/reclaiming larger collections.
Decision: retain complete owner/tenant secret sets without count admission.
Native count arithmetic remains checked and listing traverses every bounded
index page with exact count/index agreement. SQL mutation locks the requested
secret identity rather than serializing the owner for quota counting. Required
record validation, encrypted ciphertext, redacted listings, authority revision
CAS, atomic indexes and migration identity/replay checks remain unchanged.
Prune pages past protected references, deleting at most its existing 256-row
maintenance slice; that slice limits mutation work, never stored identities.
No worker or queue is added. Secrets remain durable until explicit delete or
existing inactive/age-qualified reclamation. Alternatives: larger quotas still
reject valid configuration; dropping references risks deleting active secrets.
The SQLite fixture retains/lists 2,105 rows (193,552 JSON bytes, 0.022 s combined
put/list), then protects 1,800 and deletes 256 later rows in 0.004 s. Native
fixtures cover 2,105 restored rows across reopen, late replay/divergence, complete
listing and two cleanup slices. Measurements concern controlled metadata only,
not production throughput, cryptographic work or peak memory.

## Incremental translation backlog ownership

Problem: a process accumulator quota refused independent tasks, while finite
buffers discarded accepted previews and ordinary terminal handoff evicted them.
Decision: independent accumulators start on demand; each owns one lazy anonymous
file containing pending UTF-8 bodies and an ordered key/range index. Identical
pending keys coalesce. Consumed/replaced ranges reclaim when obsolete bytes
exceed live bytes; copying uses 64 KiB scratch space. Empty buffers truncate,
and idle retirement, consumed terminal handoff, explicit close and worker
failure release the file. Failed housekeeping retains accepted ranges for retry.
Normal stamp/replacement drains accepted segments first; explicit cancellation
withdraws pending work and wakes the consumer. No extra worker is introduced.
The active, queue-capacity and preview-count launch knobs are retired. Provider
cost/deadline policy remains explicit; completed originals/translations use the
paired file-backed task cache described below.
Alternatives: larger quotas still refuse/discard demand; resident text deques
scale heap with backlog; dropping pending segments during normal stamp loses
requested enrichment. A controlled 2,085-segment backlog retains 68,321,280
bytes in one anonymous file with 485,894 peak Python allocation bytes and zero
drops; enqueue takes 0.048 s. Drain/terminal leaves zero file bytes/open files.
A 65-blocked-call plus healthy 66th task fixture joins every worker and releases
all registry/file ownership after cancellation. These are synthetic backlog and
lifecycle measurements, not provider throughput, total RSS or restart durability.

## Incremental translation shutdown ownership

Problem: demand accumulators could outlive service teardown; registry removal
alone did not prove a worker had physically returned. Retired-cache handoffs
also borrowed storage synchronously without a shutdown ownership receipt.
Decision: one lifecycle fence withdraws new previews/retired-cache handoffs,
cancels existing accumulators, joins weakly registered physical thread handles,
and waits for active synchronous handoffs. Weak handles retain no task/cache
payloads after worker retirement. Started-then-failed carriers remain joinable;
never-started failures release their handles. Explicit cancellation suppresses
late preview publication and clears reconstructible completed caches.
Production shutdown runs this join off the serving loop after task quiescence,
before browser/provider/push/storage teardown; failure retains those dependencies.
An unused translation module stays unloaded. Callback-internal owner close
refuses self-join before changing lifecycle state; repeated external close joins
existing work. No new worker, timer, pool, capacity gate or durable store is added.
Alternatives: a timeout/registry-only stop can certify teardown while callbacks
still execute; retaining accumulator objects would extend payload lifetime.
Controlled callbacks and synchronous handoffs keep close pending until release;
a deregistered worker remains owned beyond 1.1 s and then physically joins.
After close, worker handles, registered accumulators, synchronous invocations and
spool ownership are zero. These are lifecycle witnesses, not provider latency or
peak RSS measurements. Preview frames and completed-cache retention have their
own demand owners below.

## Translation preview transport work

Problem: every provider chunk copied all completed translations, then serialized
both the full block map and joined narration. Decision: incremental producers
publish only the current block's partial/final preview in `partialByRound`.
The presentation controller owns a null-prototype keyed index, merging changed
keys without copying prior history or mutating borrowed DTOs. An explicit empty
map resets previews; clearing/failing the activity releases it. Legacy whole-turn
`partial` frames still work. Joined narration is derived lazily at presentation;
the existing graph paint scheduler coalesces a burst while retaining every key.
Text and reasoning blocks consume ephemeral previews independently; durable
translations remain preferred and previews never become `translatedMarkdown`.
The plain-text Surface renderer respects the selected display text too.
Alternatives: increasing preview count with cumulative frames makes transport
quadratic; overwriting each received map loses earlier blocks. No new worker,
queue, timer, durable registry or capacity gate is added. These optional push
previews remain reconstructible; authoritative completion/reload uses the Turn.
A controlled 2,000-completed-block fixture reduces a progress payload from
7,249,045 bytes to 125 bytes. Real Chromium against the local app merges four
updates into one subsequent paint, preserves all final previews and leaves the
source TurnState unchanged. Completed cache bodies use the paired file owner
below; empty accumulators retire on demand as described below. Payload size
is not total RSS or throughput.

## Completed translation cache demand

Problem: a 32-default/1,024-maximum preview count suppressed valid later
segments, while paid originals/translations remained resident in task maps.
Source/result thresholds also declined long input and discarded paid output.
Decision: remove preview count and source/result character admission gates, and
the preview-count launch knob. Complete source/result text uses the existing
file-backed pending buffer and paid cache. One lazy anonymous file per task
stores paired UTF-8 bodies; mapping views retain keys/ranges only.
Pair indexes publish after complete writes, preserving prior paid pairs after
partial-write failure. Replaced ranges reclaim when obsolete bytes exceed live
bytes; copying uses 64 KiB scratch. Worker retirement/recreation reuses the same
cache. Failed terminal persistence keeps the cache for retry; successful
terminal handoff or explicit cancellation clears views and closes the empty file.
Done notifications contain status only; consumers hydrate authoritative Turn
facts rather than receiving another complete cache mirror. Provider cost,
deadline/retry policy and source identity validation remain explicit. The
translation engine retains whole-document context, protected markup and its
truncation/model-rotation checks; local 32,000/64,000 character thresholds no
longer decline input or discard completed output. Larger thresholds still
refuse valid text, and arbitrary splitting breaks mixed-language/markup context.
No extra worker, pool, timer, durable store or count quota is introduced.
Alternatives: a larger count gate still suppresses valid previews; uncapped
resident dictionaries grow payload heap; publishing half a pair corrupts source
matching. The controlled 2,049-pair fixture retains 134,283,264 file bytes with
985,444 peak Python allocation bytes, writing in 0.104 s; clear leaves zero file
bytes/open files. A 1,100-preview fixture crosses worker recreation and hands
all exact pairs to the existing terminal commit. These measure resident cache
ownership, not total RSS. Terminal enrichment borrows a pinned file snapshot and
reads only authoritative segment keys as described below; its projection/storage
encoding still follows the existing authority policy.
A controlled long-text fixture retains 129 complete source/result pairs
(280,000/150,000 characters each), totaling
94,170,000 file bytes with 1,485,460 peak Python
allocation during append and 0.1473 s write time. Inputs were
allocated before tracing; this is cache append memory, not total process RSS.
Clearing closes the file. Real SQLite settlement/cold-read tests preserve long
source and translated text under the ordinary owner/source/revision fences.

## Incremental translation idle lifetime

Problem: every accumulator retained an idle OS thread for 300 seconds, and an
arrival during consumer retirement could be refused despite reusable paid cache.
Decision: enqueue the first accepted segment before starting its carrier; a
startup commit signal prevents provider dispatch after partial-start failure.
Consumers retire immediately after draining pending work. Idle retirement owns
no complete-cache copy or storage commit; terminal handoff owns persistence.
Producers atomically replace idle-retired carriers and reuse the task cache.
Terminal/cancel requests fence later submissions, so retirement cannot reopen a
settled task. A failed producer does not withdraw another accepted producer's
carrier. Physical worker handles remain joined through the existing shutdown
owner. No new pool, timer or count-admission queue is introduced.
Alternatives: shortening a timeout retains idle occupancy; blindly retrying any
closed queue would reopen cancelled/terminal owners; starting before handoff
can race an empty consumer; dispatch before startup success can create effects
while reporting failure. The controlled fixture runs 128 independent completed
jobs, then observes zero live idle workers/registry entries and retains paid
cache until explicit cancellation releases its files. Separate fixtures verify
retirement-window replacement, zero model calls after partial-start failure and
continued accepted work after an unrelated producer fault. These are lifetime
counts, not production latency or total RSS guarantees. Terminal complete-map
materialization and project-wide remaining gates still require their owning work.

## Terminal translation snapshot reads

Problem: terminal enrichment decoded every cached source/translation into two
complete dictionaries, even when the authoritative Turn referenced few keys.
Projection merge also duplicated the already private projection twice.
Decision: freeze the small range/count index and duplicate its anonymous-file
handle for the synchronous commit lifetime. Mapping lookups decode only requested
keys. Absolute-position reads isolate the snapshot from append/reclamation seek
cursors; platforms without pread use the task cache lock for cursor isolation.
Append, replacement, cache clear and file reclamation cannot revoke pinned data.
The snapshot closes in finally after success, failure or retry; completed-cache
retention still follows terminal persistence/cancellation. Projection merge makes
one private deep copy and compares it to the untouched original mapping.
Owner predicates, source equality, settled-Turn refusal, revision CAS and storage
retry remain at the existing authority boundary. No worker, timer, body mirror,
capacity gate, transaction split or alternate storage authority is introduced.
Alternatives: retaining complete dictionaries multiplies decoded heap; borrowing
mutable live file ranges breaks source matching across cancellation/reclamation;
splitting authority commits would weaken the existing atomic update contract.
The 2,049-pair fixture retains 134,283,264 file bytes; committing one authoritative
segment decodes exactly two values with 248,195 peak Python allocation bytes in
0.0003 s. Replacement/clear retains old snapshot values until its file handle
closes. These are controlled enrichment-read measurements, not production
latency, total RSS or removal of final projection/storage encoding limits.

## Push connection demand

Problem: local Push connection registration rejected process/owner counts at
64/12 reference defaults and 256/128 hard ceilings, scanning all existing peers
for every new socket. Decision: remove connection quotas, constructor capacity
arguments and their launch overrides. Authenticated sockets register in the
existing weak set without a count scan; subscription/owner filtering and eager
unregister/disconnect remain. Shutdown withdraws new registrations through its
lifecycle fence and reports service closing (503), never capacity throttling.
Health reports demand admission; obsolete connection-capacity gauges and install
knob guidance are removed. No worker, timer, alternate registry or durable state
is introduced by registration. Local event FIFOs have no count/byte/frame
refusal, oldest-frame drop, sustained-pressure disconnect or 64-pong eviction.
One immutable JSON publication is prepared on demand workers and shared across
matching sockets; FIFO references pin segmented anonymous files until delivery
or disconnect. Segment and idle worker reuse hints derive from existing launch
observations and never restrict accepted work. Independent file ranges use
short reservations and cursor-independent I/O where available. Slow encoding
and disk reads preserve control/RPC priority; cancellation retains an active
event and joins every asyncio waiter. Socket close joins privately owned
workers; application shutdown joins the hub owner. Encoding/file/actual socket
failures still end affected delivery explicitly, preserving durable authority.
The sole socket writer sends stored JSON directly instead of parsing and
serializing large event bodies on the serving loop. Event-limit launch knobs
and capacity gauges are retired; health reports owned file counts/bytes.
Alternatives: larger quotas still reject valid sockets; registration-time peer
scans make bursts quadratic; evicting old sockets drops live ownership. The
2,049-same-owner fixture registers in 0.0059 s with 295,332 peak Python allocation
bytes for the registry operation (clients allocated beforehand), then unregisters
to zero. A 1,026-client fixture delivers only to its 513 authenticated matching
owners, drains retained bytes, and releases every subscription/client on close.
These are synthetic registry/lifetime measurements, not socket memory, actual
network throughput. Increasing old quotas still rejects valid events; unbounded
resident payload dictionaries multiply heap. Temporary files retain exact
accepted bytes without a second durable store. Standard-library JSON encoding
avoids oversized backing allocations from concurrent large orjson encodes.
A controlled 129-event/33-client fixture holds 4,257 FIFO references to
135,273,695 file bytes, with 472,825 resident and 97,295,716 peak traced Python
bytes after 0.6193 s preparation. Source bodies were created during tracing.
Peak includes concurrently active inputs/encoding; resident bytes are measured
after every preparation completes. Disconnect/unregister releases all files
and client entries. This is temporary transport residency, not total RSS or a
network throughput guarantee. Tests deliver 4,097 FIFO events and a 9 MiB
Unicode frame, preserve shared publication identity/owner filtering, inject
partial writes, and join repeated cancellation. A real Quart WebSocket receives
1,026 ordered events including the large frame without pressure disconnect.

## Webhook subscription demand

Problem: registration rejected global and owner subscription counts, while the
loaded delivery snapshot independently discarded valid rows beyond its count
budget. Decision: persist owner-bound subscriptions through the existing atomic
JSON authority without count admission, and project all valid persisted rows.
The retired subscription override and capacity health fields are removed.
New opaque subscription IDs use 128 random bits so growing catalogs do not
inherit the old 32-bit collision risk; existing IDs remain readable/deletable.
Filtering, secret redaction, HMAC signatures, owner-scoped list/delete, URL/egress
validation and the cross-process atomic update remain. The transient failure
state cache uses the existing retry-state reuse hint; its eviction is not
subscription admission. Immediate/retry and event-byte policies remain separate
unfinished delivery work. Alternatives: larger quotas still refuse valid rows;
truncating reload silently deactivates subscriptions; evicting rows deletes
operator configuration. Durable rows live until explicit deletion. No new
worker, store, or background lifecycle is introduced by this change.
A controlled 4,098-row fixture stores 1,183,247 JSON
bytes; one late atomic append takes 0.1886 s, with
8,690,442 peak and 2,470,043
resident traced Python allocation during append/cache publication. Rows were
seeded before tracing. Cold reload preserves all rows; HTTP tests cover creation,
owner-only listing, secret redaction, cross-owner refusal and explicit deletion
beyond the old global/owner ceilings. These are fixture measurements, not total
RSS or production throughput.

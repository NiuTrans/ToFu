# Native runtime owner

html_projection.rs/html_tokenizer.rs own pure HTML text projection, with an
iterative arena and subtree text ranges. Generate entities/Unicode/oracles with
gen_native_html_projection.py; never use host Rust Unicode casing as its owner.
Verify html_projection tests and knowledge_html/native_check before profiling.
Kernel clocks do not admit the Python binding or durable ingest path.
html_projection_abi.rs borrows complete UTF-8 output through a synchronous sink;
no result handle survives return. Its generated contract owns status/lifetime.
Verify html_projection_abi, actual sealed binding and archive capability tests.

OwnedLease.finish shares Task's terminal debt fence: identical retries reread
own durable header, stale generation refuses, completed Suspend plus Aborted
alone preserves recovery. Verify unknown-before/after-write, no-history reads and
pause/error/ordinary-abort cases in tests/checkpoint_journal.rs. Callers freeze
status/Suspend before retrying; C ABI owns that volatile candidate binding.
Journal.recover is explicit: trusted Store proves writer-process death, then the
existing CAS advances generation with Keep. It never retries ambiguous delivery
or accepts inflight/terminal/legacy fences. Verify checkpoint-journal fault tests.
checkpoint_writer.rs owns shared Linux boot/PID-namespace/start-tick capture
and pidfd death proof. Missing evidence refuses; elapsed time grants no takeover.
Keep real child SIGKILL/reap, live/foreign identity and recycled-PID checks in
both runtime feature modes; database adapters borrow this owner without copies.
inspect_abandoned_frontier only observes completed/paid-pending dead-writer state;
header/snapshot versions must agree across the read. It grants no lease or recovery
authority. Verify before/after snapshot races and unchanged durable state.
Journal.validate_retained_frontier shares pure header/snapshot checks with active
recovery. It performs no storage I/O or writer-death check and grants no authority;
passive callers must separately prove the authenticated exact prefix. Verify the
NoIo/live-writer journal test and native aggregate replay fault tests.
Journal.handoff requires released completed state or a paid pending frontier
with dead-writer proof and atomic Store authorization. Pending wallet debt is
retained on the retired source; the target starts a new zero-usage invoice.
checkpoint_lineage.rs owns accepted ancestry and retirement validation for
TofuDB and SQL. SQL invokes its pure ABI inside the actual locked transaction;
never acquire a runtime handle or reenter host callbacks from that validator.
It rebinds task/account identities while retaining each fee origin and the original
deadline/limits. Verify transfer conflicts and before/after unknown-delivery fences.
checkpoint_actor_handoff.rs prepares the whole retired set through OwnedLease;
Store defaults to refusal and must commit all targets before returning leases.
checkpoint_actor_accounts.rs verifies canonical full-account moves against the
Python producer. Verify native whole-consumer faults/limits and installed leases.
Priced actor checkpoint composition declares fee-payer origin support before HTTP;
the common snapshot preserves original payer IDs with local executor rounds.
Application actor takeover remains a separate accepted aggregate authority.
checkpoint_goal_context.rs owns bounded Goal event witnesses and accepted
continuation chains through transaction-scoped ReadAuthority reads. Preserve
exact context bytes and budget charging across JSON feature modes; storage
adapters filter identities and bound each document. checkpoint_goal_policy.rs
shares immutable fee mode, wallet holds and prepared-actor birth rules. Claim
and consumption policy live in checkpoint_goal_claim/consume; canonical codec
digests ignore provider map order. SQL transaction callbacks never enter a
handle or publish capabilities before commit. Verify both JSON modes, paired
SQL rollback/lifetime tests and native replay/multi-actor repeated SIGKILL.
checkpoint_actor_fees.rs validates frozen actor quote unions against the shared
account frontier, preserving original producers/payers and refusing root-owned
consumption. native_aggregate_cost_v1 validates non-wallet credits without
relaxing wallet capsules. Neither grants billing/lease authority. Aggregate storage
tests use Python-produced fee/account fixtures and historical-overlap conflicts.

The root Cargo workspace owns this crate and the `tofu-native` entry point.
model_tool_wire validates one visible function subset against the execution
catalog; schemas never relax actual argument/permission validation. Optional
prepared_tool_wire binds monotonic round projections after context preparation;
its read guard holds the exact template through counting/aliases/serialization.
Late visibility requires round control/history, cannot compete with wire_tools,
and never overrides generation or execution rights. Verify prepared_tool_wire
plus actual stdio two-round visibility and injected durable ROOT comparisons.
tool_sibling_control returns declarations on existing round settlement before
callbacks, preserving old standalone defaults. Verify model_tool_wire and stdio.
chat_wire/chat_content borrow Chat history, normalize shared text/context seams
and canonical key order, retaining images/cache boundaries and durable bytes.
Fields/order/Unicode whitespace derive from Python; never edit them separately.
Verify gen_rust_chat_wire_oracle, real HTTP and durable ROOT message parity.
`src/executor.rs` owns demand job publication and joined worker lifetime; it is
not a second durable task, conversation, provider or storage state machine.
Probe budgets size idle reuse; executor job/byte counts and aggregate allocator
observations grant no admission quota. Qualified tool reads use demand scoped
workers; legacy tool count/byte hints grant no admission quota.
reactor.rs owns one current-thread CLI reactor and demand blocking workers;
verify parked jobs, independent healthy work and joined shutdown in reactor.rs.
Never derive blocking admission from socket reuse hints.
HTTP attempts are demand-driven; connection/pool-wait hints grant no HTTP quota.
Verify the concurrent TCP cohort and active_requests lifetime through response,
required consumer join and drain; model admission stays with its own account.
Relative task expiry, Agent round counts and cumulative tool limits are optional; preserve exact nullable checkpoint binding and
finite/zero pre-tool refusal. Per-response tool admission remains independent. Configure uses validate_protocol null semantics; tool
admission retains its Python oracle. Verify Agent/checkpoint/tool-contract tests.

token_rank_counter.rs owns count-only ordinary BPE; generated token_ranks/ data
is pinned to tiktoken-rs 0.11.0 and belongs to the native source identity. Do not
read/edit bulk tables; use gen_native_token_rank_tables.py check mode. Verify
token ingress Python/full-encoder differential and cold/warm/RSS paired evidence.
Forward keys borrow the generator’s packed static bytes; reconstruct and verify
the original upstream asset SHA, plus packed SHA, before encoder admission.
token_merge_heap.rs owns request-local indexed merges above the measured 1 KiB
crossover; preserve lowest-rank/leftmost ties and one entry per byte. Verify both
index widths against synthetic rank ties plus ordinary text/oracle neighbors.
token_pair_ranks.rs derives the complete immutable two-byte index; token_merge_linear.rs
shares merge semantics across inline/Vec boundaries. Verify pair coverage, rank ties,
all request/field oracles, cold costs and the measured 256 KiB encoder residency.

Read `docs/ARCHITECTURE.md`, `docs/MONOREPO.md` and `docs/DEFENSIVE_PATTERNS.md`.
Port domain contracts before replacing consumers; take current Python behavior
as a differential oracle, not a permanent native execution dependency.

Verify `cargo test --workspace`, release `check-runtime`, focused domain parity
and independent performance evidence. Existing `packages/tofu-db/` builds keep
their standalone manifest/lock and admission rules.

`session/` owns live resource settlement, not durable task status. Generate
`execution_contract.rs` with `scripts/gen_rust_execution_contract.py`; verify its
check mode before committing. Run `tests/session.rs` and preserve the captured
Python receipt oracle before extending adapters or terminal publication.

`task.rs` owns frozen terminal candidates and persistence/publication debt via
explicit ports. Replay fields derive from `lib/task_replay/task_replay.py`; verify
`tests/task.rs` before adding production adapters. Port retries require owner/task
idempotency and may not restart provider execution or resource settlement.

`sse.rs` owns provider-neutral byte framing, not completion classification.
Verify actual Python oracle fixtures in `tests/sse.rs`, exact stop boundaries,
malformed-input resynchronization and EOF versus intentional cancellation.
Framing must preserve arbitrary UTF-8/CRLF splits and byte budgets.
Delivered payloads release growth slack; close/discard release owned buffers.
Measure tiny/fragmented/Unicode and multiline/large events with
`benchmarks/sse_framing.rs`; allocation-instrumented clocks never admit speed.

`provider.rs` owns the supported Chat stream subset and explicit output/tool
admission. Native state classification is generated from `stream_result.py`.
Run `tests/provider.rs` and verify `run-chat-stream` before changing adapters;
unsupported provider/tool shapes must not become verified success.
Completion transfers compact owned strings; preserve scalar notice limits and
classification precedence. Verify default/sorted-map provider tests and the real
`provider_finish_measure` example, with separate timing/allocation binaries.

`http.rs` owns fixed-endpoint pools, awaited response lifetime and terminal
drain. `cli_http.rs` is explicit transient composition, not routing authority.
Verify `tests/http.rs` with real TCP, session Stop, pool cancellation and
connection reuse before adding production transport adapters.
Header wait policy has explicit nullable stdio representation; verify
http_header_wait_stdio, the host schema and installed application HTTP policy.
Keep upload bounds separate from response-head waiting and caller Stop/deadline.
The maintained async connector patch is packages/vendor/reqwest; its upstream
archive/file pins live in TOFU_UPSTREAM.json. Verify physical write/flush, TLS,
explicit proxy, H2 cause preservation, HTTP/1 upload Stop and healthy-connection
isolation before changing it. cli_agent::Input scans only appended bytes and
retains that cursor across cancelled frame futures; keep large-input stdio proof.

`tool_policy.rs` owns position-based flood admission and caller normalization.
Run its actual Python oracle and provider/HTTP neighbors; preserve numeric kinds,
large integers, signature revisions, explicit limits and no automatic retry on
flood. `retryable` still permits manual retry; `autoRetryExhausted` fences automation.

`tool_assembly.rs` owns response-local occurrence/index/name reconciliation.
Verify `tests/tool_assembly.rs` against the actual Python oracle, including shared
catalogs, flood policy, ordering and long retransmission state counts.

`tool_contract.rs` owns compiled request contracts and default-deny dispatch
proofs. Verify its actual Python error/default oracle, owner/epoch/caller gates
and authorization revocation. Contract validity must never grant execution;
handler lifecycle belongs to the declared execution owner.

`tool_execution.rs` owns one scoped handler lifecycle over ExecutionSession.
Run `tests/tool_execution.rs` and contract/session neighbors; preserve callback
quiescence, orthogonal stop facts, rollback and success refusal after failed
cleanup. Handlers get a runtime view, never terminalization authority.

Parent-linked tool scopes observe session signals without owning parent leases.
Verify `tests/tool_parent_stop.rs`: parent Stop/deadline, closing-before-join,
abandonment, owner fencing and the actual Task abort/terminal event composition.

`process_launch.rs` owns explicit Unix launch inputs, not authorization or process
quiescence. Verify `tests/process_launch.rs` with real children; preserve child
PATH/cwd resolution, environment isolation and literal argv. Execution adapters
must still provide sandbox, output bounds, cancellation and joined reaping.

`process_supervisor.rs` runs in a dedicated Linux worker, never installing
subreaper or signal policy in the application. Verify real processes in
`tests/process_supervisor.rs`, including escaped descendants, cancellation,
unwind, capture limits and release after joined reaping. Keep reserved leader
PIDs until group termination; ECHILD is required before worker result.
Sandbox/cgroup admission and hard supervisor-death containment remain separate.

`process_boundary.rs` installs child exec policy without mutating the parent.
Verify high inheritable FDs (including above lowered NOFILE), forced old-kernel
proc inventory, preserved spawn errors, unchanged parent policy and actual
CPU/AS/file-size/descriptor limits. Keep post-fork code allocation/lock-free.
Process limits and no_new_privs never certify namespace/chroot/cgroup isolation.

`agent.rs` composes transient ordered/text rounds through the shared Task and
tool-scope owners; `agent_http.rs` supplies bounded borrowed-body HTTP rounds.
Verify `tests/agent.rs` with actual TCP/process feedback, complete-batch argument
preflight, revocation/denial, parent Stop/deadline and fatal cleanup failure.
`tool_policy::lossless_arguments` must reject numeric-kind/value coercion.
Production parallel/approval/progress/compaction/durable adapters remain pending.

`progress.rs` owns native post-tool consecutive control; Agent.progress binds
trusted observations after joined tools. Verify tests/progress.rs, actual Python
oracle check and agent Stop/failure/disabled/budget cases. Never infer evidence
or nonretryable failure from prose. Verify actual stdio/host observer joining;
checkpoint/default-root migration is pending.
agent_checkpoint.rs owns native completed/pending-tool recovery over trusted
load/begin/pending/reconcile/complete ports. Verify agent frontier/fence/budget/streak tests, native
neighbors and MSRV. The schema is native_agent_checkpoint_v1; never restore
inflight/terminal state or invent account/deadline credit. Verify checkpoint stdio
Suspend/resume/fault cases and joined host callbacks; selected embedded application storage is explicit; default promotion is pending.
checkpoint_journal.rs owns versioned lease/frontier transitions over atomic Store
ports; Policy composes Agent checkpoints with explicit host Recovery. Verify
journal CAS/fault/bound tests and actual Agent Suspend/resume. Keep must never
hydrate history; never release on Drop/timeout or load inflight/terminal state.
Paid tools_pending recovery requires dead-writer proof, exact domain observations
and the ordinary progress/complete boundary. Verify batch/usage/limit preservation;
stdio binds explicit pending/reconcile callbacks; verify installed priced SDK
recovery and borrowed PendingView serialization. Reconciliation histories share
the exclusive bounded large-frame writer. Verify bounded spent-tool deltas with
all paid fields frozen and actual application file-batch recovery. See [recovery contract](../../docs/contracts/TOOL_EFFECT_RECOVERY.md).
checkpoint_owned.rs exposes OwnedLease/OwnedPolicy over the same Journal/Lease.
Verify callback lifetimes, shared load/poison/generation flags, actual Agent
pause/terminal retry and native-store reopen; benchmark checkpoint_owned_cost.
The retained Store/Recovery are explicit; no token reattachment, Drop release,
second journal or history cache. Embedded C ABI reuses this owner; default production binding is pending.
Cargo.toml keeps provider-json-order enabled by default. Checkpoint-only engine
embedding disables runtime defaults; verify normal workspace behavior plus
no-default-features journal/agent tests. NativeCheckpointStore rejects a linked
insertion-order Map because the DB canonical codec requires sorted objects.
Policy::terminal_persistence binds an exact owned Store and wraps the existing
Task persistence port. Verify settlement/fence/persistence order, failed/unknown
seal retry, owner/status denial, genuine Suspend vs ordinary abort/cleanup failure,
real Agent loops and native-store reopen. Never reexecute frozen terminal debt;
metadata fence and task commit are ordered separate transactions, not atomic.
`tool_scheduler.rs` owns atomic batch admission, explicit read classification and
joined scoped parallel execution; `Agent.scheduling` selects it at composition.
Verify 32-read overlap with hint 1, ordered writes, parent Stop, required-cleanup
priority, partial startup before any effect,
reentrant refusal and concurrent joined close. Authorization must be Send+Sync.
Never infer safe overlap from read permission alone or use the global Rayon pool.

The private `vendor/rayon-core` dependency retains upstream scope/wake code with
a pool-local short idle-spin option. `UPSTREAM.json` owns provenance/delta hashes;
licenses stay retained. No guides are added inside vendored sources. Verify
native runtime tests, private-core serial tests, idle/wake stress and matched
wait/empty/hash cost profiles before changing this boundary.

`approval.rs` owns trusted exact single-use receipts, not human/Auto/delegate
policy. Agent decisions distinguish Approved, Denied and NotRequired; Denied
never reaches scheduling and preserves bounded owner feedback. Verify
`tests/approval.rs` and agent waiting/Stop tests; preserve final
authorization, parent/epoch binding, TTL, revoke and count/byte bounds. Mint
accepts trusted composition decisions only; never model-controlled authority.

`capacity.rs` owns explicit immutable launch measurements and pure budget
derivation. Verify `tests/capacity.rs`, cgroup ancestors/namespace roots,
headroom failure, overflow and large-host scaling. Capture once per composition;
aggregate pool shares and preserve explicit overrides. No per-request probing.

`capacity_allocation.rs` owns composition-wide pool reservations; private leases
stay in pools until joined close. Verify `tests/capacity_allocation.rs` and
scheduler concurrent-close tests. Preserve atomic observational accounting, unknown
capacity fallback, rollback and no early release during reentrant close.

Demand CPU occupancy is explicit and measured; keep CPU-heavy callers at 1000
units. Verify aggregate CPU/memory charges and joined release in capacity tests;
use `tool_io_capacity_measure` for waiting-work cost comparisons. Read permission
alone never justifies lower occupancy or production default changes.

`cli_agent.rs` composes Linux native rounds with declared read-only stdio tools.
The native-agent stdio schema owns frames; verify `tests/agent_stdio.rs` with
real subprocess/TCP, exact response IDs, EOF/byte limits, silent deadline, one
terminal and write refusal before network effects. Never infer host authority.

`cli_agent_control.rs` owns the one-slot reactor reader and required joined
Upstream lease. Verify silent-provider cancel, unissued/duplicate frame refusal,
weak parent lifetime, original stdin flags before terminal, and actor-fault
cleanup. Never replace it with a detached blocking stdin reader.

Stdio deadline_unix_ms binds original UTC expiry into checkpoints; changed,
added or removed profiles refuse before model/account recovery. start_observed
subtracts pre-entry elapsed time and preserves ordinary terminal settlement.
Verify actual expired/shortened HTTP and pause/resume tests plus session timing.

model_admission.rs exposes counted shared-authority Admission/Lease ports.
with_model_admission selects one authority; with_account retains its inline path.
Verify real TCP cap freezing, paid/unknown settlement, fault/cancel/no-send cases
and metadata cost. Callbacks receive read-only stop/deadline observation, join
and fence unknown intent; SDK/default binding
is pending. Never create a local mirror or recount host-prepared input.

cli_model_control binds model_counter to one host account over the existing
reader; never also construct a native mirror. Verify real wire cap/settlement,
ACK expiry, original deadline, no-effect refusal and SDK cleanup after join.
Shared-account checkpoint restoration/default promotion remain pending.

Standalone delivery belongs to `scripts/build_native_runtime.py` and the native
runtime artifact contract. Verify Rust 1.85.1, archive integrity/failure tests,
installed empty-PATH execution and the native-runtime workflow before promoting
archives. A target triple alone does not certify a distribution/glibc baseline.

`round_admission.rs` owns optional per-round policy ports and required Permit
settlement certification. Verify agent admission/Stop/unwind/usage tests. Raw
Returned/Failed/Abandoned evidence grants no refund or routing authority; keep
owner/task explicit and preserve required ModelRoute cleanup before terminal.

`cli_agent_rounds.rs` binds optional host round decisions and raw settlement ACKs
to the existing control slot. Verify real stdio refusal before HTTP, wrong reply
kind, accounting refusal and cancel-time ACK before terminal cleanup.

`usage.rs` owns observed token lower bounds/completeness, not pricing or budgets.
Aliases derive from lib/cost.py; verify the current Python oracle gate, native
usage tests, u64/Unicode refusal and real round evidence before changes.

`model_account.rs` owns bounded finite model reservations/settlement and one-time
restore. Counted HttpProvider requires a trusted InputCounter and explicit output
field; verify current Python account traces, real TCP caps/refusal, observer
quiescence, wait/Stop and allocator release before production integration.

`token_ingress.rs` owns explicitly selected local tiktoken request estimates;
verify current Python fixtures, exact tool JSON, byte/depth/identity refusal and
owned lazy encoder release. `cli_token_counter.rs` borrows through one joined
CPU worker; verify cold cancel before HTTP. Production resolver selection and
encoder/stack capacity admission remain separate. Versioned license overlays
under licenses/ supply upstream notices omitted from crate packages.

`cli_terminal.rs` owns optional post-settlement ACK transport, not storage.
Verify frozen Task.finish retry, joined reader tail/flags, explicit ACK limits,
large exclusive frames and zero model/tool reexecution before changes.

`provider.rs` borrowed text spans feed the optional awaited `http::TextObserver`.
Verify accepted-prefix/order/UTF-8, consumer failure and Stop joining with
`tests/stream_progress.rs`; default execution must allocate no observation buffer.
`agent_http.rs` rechecks Stop/deadline after transfer joins before success. Verify
consumer-published session/client/deadline Stop and original fault precedence.
`cli_text.rs` uses the existing control slot for text ACKs; verify explicit rejection,
published callback quiescence and original stdin/stdout flags before terminal.

`cli_approval.rs` binds explicit host decisions to the existing approval Store.
Non-read callbacks require exact consumed receipts and joined result/EOF; preserve
standalone read-only deadlines. Verify `tests/approval.rs`, `tool_contract.rs`,
stdio tests and the scratch-only native approval probe before extending policy.

`anthropic.rs` owns provider event translation/replay over existing SSE and Chat
accumulation; it does not authorize routed HTTP requests. Verify the Python
oracle generator/check and `tests/anthropic.rs`, then provider/stream_progress/
HTTP neighbors. Stop mappings and bounds derive from execution_contract. Keep
hosted tools private, signatures/replay order intact, partial JSON unrepaired,
and byte/block admission explicit. The release `anthropic_stream_cost` example
and `benchmarks/anthropic_stream_measure.py` measure only the translation kernel.

`anthropic_request.rs` serializes supported canonical requests while borrowing
history; unsupported features fail before dispatch. Verify its Python request
oracle and actual Messages model/tool/model connection reuse. `http.rs` keeps
one monomorphized physical response owner for Chat/Anthropic; required-consumer
Stop/drain tests apply to both. Explicit CLI provider_protocol comes from the
owned v2 route; do not infer protocol or allow caller routing overrides.

`model_content.rs` borrows/validates declared canonical text/image blocks; shapes
derive from the stdio schema into native constants and packaged Python schemas.
Feedback.model_content is explicit; text-only policies remain default. Verify
model_content/request/token_ingress tests, generator drift and registered
inspect_image archive tests. MIME sniffing uses the generated magic authority
and a bounded base64 prefix, never local URL fetch or full image-sized decode.

`responses.rs` owns bounded provider event interpretation and replay over existing
SSE/Chat accumulation. Verify the Python Responses oracle plus responses/provider/
sse/stream_progress tests. Item/output identities cannot guess across explicit
routing evidence; terminal text/argument snapshots append only proven suffixes.
Native request/HTTP/profile admission remains separate; replay never authorizes
program, hosted-tool or multi-agent effects. Unknown mandatory shapes fail closed.

`responses_request.rs` owns borrowed core request conversion and exact tool aliases.
Verify the current Python request oracle and real TCP model/tool/model tests.
`HttpProvider::with_responses` binds explicit replay support; no model-name inference.
Shared HTTP ownership preserves joined cancellation and bounded provider rejection
metadata. Hosted/program/agent profiles refuse before tool effects; CLI/routed
application admission binds owned route capabilities in native_route.py.

Prepared round requests inherit one CLOCK_MONOTONIC deadline per run. Verify
fixed deadline across rounds, queued/running worker timeout joining, no obsolete
decision after expiry and installed interrupted terminal persistence before ACK.

Optional per-round history preparation uses the existing reservation channel.
Verify exact prepared history, NotDispatched on retained-byte refusal, callback
fault persistence/cause and joined cancellation. Large exclusive frames must
finish ordinary Stop publication under the original deadline; never add a reader
or mirror history. Installed preparation tests preserve tool/reasoning replay.

pricing owns resolved request arithmetic, with no price/tier/account authority.
Verify gen_rust_pricing_oracle.py --check, tests/pricing.rs and the explicit
pricing_measure diagnostic; preserve exact Python rounding and prompt binding.

pricing_usage borrows raw counters and the generated pricing_unicode tables.
Verify raw/split/fee Python oracle parity, malformed/overflow refusal and unchanged
usage completeness; never infer settlement completeness from pricing tokens.

pricing_tiers selects one borrowed normalized card tier from actual total prompt
counts. Verify Python tier oracle, inclusive/duplicate/tail thresholds, one-time
peak scaling, invalid-card refusal and pricing_measure; no price/clock registry.

billing_credits converts canonical component USD into existing micro-credit units
with explicit margin. Verify generated unit/fee oracle, raw-to-credit chain, half
ties and all overflow paths; this grants no wallet or settlement authority.

Optional settle_with_cost carries one post-account fee quote in StreamResult and
existing round settlement. Verify priced stdio/SDK cases, known versus zero usage,
price prompt binding and no-delivery admission before model reservation.

Agent.run_retained/Task.run_captured move admitted failure/Stop history into the
same frozen terminal candidate; success races preserve completed observed data.
Do not copy a history mirror or treat prefix usage as billing/resume authority.
Verify retained Agent/Task, old-artifact refusal, installed Stop and actual
application failure-after-tool tests; capture remains explicitly selected.

cli_agent_worker.rs owns the explicit owner-bound persistent stdio command.
Canonical worker envelopes live in native_agent_stdio_v1.schema.json. Reuse
requires monotonic scope, joined reader/replies, exclusive terminal-input release
and dropped per-task ports. Encoders alone are immutable worker-owned state.
Verify agent_stdio persistent-worker tests before changing readiness/reuse; no
application pool/default routing is admitted by the standalone command.

Feature-gated token_count_abi owns pure C token-count handle lifetimes and atomic
caller-buffer publication. Constants derive from native_token_count_abi_v1.json.
Close revokes lookup and joins accepted calls; no model/store/account authority.
Verify token_count_abi tests and generator guard before changing ABI semantics.

flow_graph.rs owns immutable topology and pending-action cursor transitions;
flow_abi.rs owns scoped monotonic handles and joined call/close lifetime.
Generate vocabulary/layout from native_flow_abi_v1.json; no provider, account,
context or journal is owned here. Verify native Flow differential and v4 tests.
flow_branch.rs owns label matching and containment; Unicode casefold derives
from gen_native_verdict_policy.py. Verify native branch differential/failure
tests and distinct classifier/selected-role Turns in the installed v4 fixture.

usage_aggregate owns pure additive counters for admitted native JSON. Detail
labels/oracle derive from lib.cost through gen_native_usage_aggregate.py; verify
usage_aggregate tests and the Python drift guard. Overflow must refuse without
partial output. aggregate_json checks raw positive integer width before decoding;
verify raw duplicate/opaque/negative-zero cases. Explicit terminal persistence
usage_summary freezes a Value aggregate for successful ACK retries; verify
stdio selection/retry and SDK byte/identity fences before default promotion;
never replace raw usage or grant account/refund authority from this projection.


Terminal persistence retains clone-for-validation after the cross-process move
candidate failed to establish stable gains. Keep exact canonical byte identity
and terminal_validation_measure as diagnostics; native_terminal_delivery covers
real SDK/transient ACK retries only, never durable/application admission.


Explicit completion_reference emits terminal_committed only after persistence
ACK and Task publication. Preserve original full events by default, retained
retry evidence and physical worker scope. Verify stdio reference/abort and SDK
frozen candidate authority before qualification; do not infer storage admission.


retry_reference retains the first full request and only sends metadata after host
retry ACK. Native scalar binding must match the same immutable candidate/status
and checkpoint marker; SDK reuses original bytes and rechecks checkpoint authority.
Verify real stdio, SDK selection matrix and forged/premature/post-acceptance cases.

checkpoint_wallet validates frozen quote totals/account coverage at pending
handoff and retains original invoices in retired headers. A new executor starts
a zero-debt candidate. Verify repeated wallet handoff and malformed evidence;
this module never reprices, writes wallets or grants an execution lease.

`checkpoint_control.rs` is the shared bounded control/capability owner over
`checkpoint_control/runtime.rs`. The optional `checkpoint-storage-abi` feature
binds it to a synchronous host Store; `joined_calls.rs` retains the shared Flow
and checkpoint close lifetime. Private SQL admission lives in its ABI contract.
Backend adapters supply an authenticated,
joined authority and its document ceiling; Journal/OwnedLease retain transitions.
Preserve one-shot response admission and whole-set Goal publication. Verify the
actual native C ABI/daemon suites before admitting a selected SQL implementation.

# Native runtime owner

OwnedLease.finish shares Task's terminal debt fence: identical retries reread
own durable header, stale generation refuses, completed Suspend plus Aborted
alone preserves recovery. Verify unknown-before/after-write, no-history reads and
pause/error/ordinary-abort cases in tests/checkpoint_journal.rs. Callers freeze
status/Suspend before retrying; C ABI owns that volatile candidate binding.

The root Cargo workspace owns this crate and the `tofu-native` entry point.
`src/executor.rs` owns demand job publication and joined worker lifetime; it is
not a second durable task, conversation, provider or storage state machine.
Probe budgets size idle reuse; executor job/byte counts and aggregate allocator
observations grant no admission quota. Qualified tool reads use demand scoped
workers; legacy tool count/byte hints grant no admission quota.

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
explicit ports. Replay fields derive from `lib/task_replay.py`; verify
`tests/task.rs` before adding production adapters. Port retries require owner/task
idempotency and may not restart provider execution or resource settlement.

`sse.rs` owns provider-neutral byte framing, not completion classification.
Verify actual Python oracle fixtures in `tests/sse.rs`, exact stop boundaries,
malformed-input resynchronization and EOF versus intentional cancellation.

`provider.rs` owns the supported Chat stream subset and explicit output/tool
admission. Native state classification is generated from `stream_result.py`.
Run `tests/provider.rs` and verify `run-chat-stream` before changing adapters;
unsupported provider/tool shapes must not become verified success.

`http.rs` owns fixed-endpoint pools, awaited response lifetime and terminal
drain. `cli_http.rs` is explicit transient composition, not routing authority.
Verify `tests/http.rs` with real TCP, session Stop, pool cancellation and
connection reuse before adding production transport adapters.

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
agent_checkpoint.rs owns optional native completed-round recovery over trusted
load/begin/complete ports. Verify agent frontier/fence/budget/streak tests, native
neighbors and MSRV. The schema is native_agent_checkpoint_v1; never restore
inflight/terminal state or invent account/deadline credit. Verify checkpoint stdio
Suspend/resume/fault cases and joined host callbacks; selected embedded application storage is explicit; default promotion is pending.
checkpoint_journal.rs owns versioned lease/frontier transitions over atomic Store
ports; Policy composes Agent checkpoints with explicit host Recovery. Verify
journal CAS/fault/bound tests and actual Agent Suspend/resume. Keep must never
hydrate history; never release on Drop/timeout or load inflight/terminal state.
Selected embedded durable callbacks are explicit; default entry is pending.
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

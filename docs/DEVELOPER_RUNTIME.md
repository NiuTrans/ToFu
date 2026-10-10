# Developer runtime contract

This is the authority for consuming Tofu's agent kernel without the full
application database or full Tofu UI. It defines Python composition, the lightweight
HTTP/SSE sidecar, model-routing setup, transient state, and release artifacts.
The full backend API is documented in
[HEADLESS_API.md](contracts/HEADLESS_API.md).

## Native terminal durability policy

Supply TerminalPolicy plus explicit terminal_persistence configuration to
NativeProcessClient.run (also forwarded by run_native_route). The profile declares
operation_id, ACK grace, maximum attempts and frame bytes. Policy.persist receives
the same PrincipalContext, task, operation and ordinal plus frozen event/receipt
JSON bytes; accessors return detached views. Return accepted only after durable
acknowledgment, retry only for an explicitly idempotent retry, or rejected.
The native Task retains the same terminal candidate across requested retries;
terminal visibility follows accepted persistence. Callback failure raises
NativePersistenceError with cause, and ACK transport failure remains a durability
error. Missing policy fails before launch. A final event that differs from the
accepted candidate or arrives without ACK is refused by the host.
Cancellation joins callbacks and bounded ACK work. Cleanup can still persist an
aborted candidate; late cancellation cannot rewrite an already frozen outcome.
Applications must persist/reconcile candidate debt using the supplied operation
identity after timeout or process loss; this port never reruns models to repair
storage. Frame/attempt/grace limits are explicit, not business success evidence.
Real subprocess tests cover rejection, wrong ACK, timeout, explicit retry and a
10 KiB candidate. Archive probes cover accepted/retried/failed/cancelled host
persistence with joined zero-active-run cleanup. The explicit durable app path
now covers selected storage authority, live progress and required terminal writes;
production-default selection, full protocol coverage and process-loss recovery
remain pending.

## Native Responses routed execution

The verified native archive accepts `provider_protocol: responses` and optional
boolean `responses_reasoning_replay` (default false). Application routes bind these
from owner-authorized `openai_responses` candidates and explicit capability
metadata; request overrides refuse before route consumption. API-key/local identity
credentials retain the existing single-use ownership boundary; OAuth is unsupported.
Verify native-route tests, actual stdio tests and registered Responses write/image
application cases with a verified archive. Explicit multimodal feedback remains
required for tool images. This path uses the existing joined HTTP/text/tool/terminal
owners and does not promote the production execution default.

## Native Responses request dialect

**Problem:** the native core Responses serializer lacked the Codex wire dialect,
so an owner-selected Codex profile could send rejected sampling/output fields.
**Decision:** responses_profile is an explicit compatible/openai/codex stdio field;
compiled routes derive it from canonical inference metadata and refuse request
overrides before consuming credentials. HttpProvider.with_responses_profile and
responses_request::Profile carry that trusted policy through borrowed serialization.
Codex emits empty instructions, the canonical include list and default summary,
and omits temperature/top_p/max_output_tokens. Values derive from the existing
Python RESPONSES_PROFILES authority at build time. Exact reasoning intent remains
with preparation; no model-name default or effort remapping occurs in native code.
Local token admission still validates its output proposal and settles actual/unknown
sent usage; omitted server output limits cannot promise provider-enforced caps.
**Alternatives rejected:** inferring dialect from model names changes generic
providers; accepting caller overrides bypasses routing; pretending a dropped cap
was enforced loses budget evidence. Compatible/openai retain the supported core
serialization; other OpenAI extensions are not newly admitted. OAuth credential
refresh remains unsupported by this route and must use the existing auth owner.

Verify both generators in check mode, 29 Python-derived request fixtures,
responses_request/agent/agent_stdio tests, actual counted TCP requests for all
three profiles, test_native_responses_dialect_installed and native-route
owner/override tests. These establish request and
settlement parity, not production default or whole-system performance admission.

## Native Responses stream boundary

`responses::Translator` consumes decoded Responses events under explicit item/byte
admission and an owned reverse-name map. `responses::Stream` combines it with the
existing SSE framer and canonical accumulator; accepted text spans borrow that
accumulator. Function arguments route by proven item/output identities, and terminal
snapshots can append only an agreeing missing suffix. Opaque and phased replay is
returned through the assistant `_responses_items` sidecar. Bounded provider errors
remain readable through provider_error; returned failure cannot be treated as done.
Unknown mandatory output/item types fail explicitly.

Verify `scripts/gen_rust_responses_oracle.py --check`, execution-contract generation
and `tests/responses.rs` plus provider/SSE/text neighbors. Native compiled routes
admit core Chat/Anthropic/Responses through explicit protocol composition.
Replay capture alone never grants
execution authority for program, hosted tools or native multi-agent items.

## Native JSON Schema output requests

The native Messages adapter accepts canonical response_format.type="json_schema"
with a json_schema object carrying schema and optional name/strict. Schema must be
an object; an explicit strict must be true. No extra descriptor fields are accepted.
The request emits output_config.format={type:"json_schema",schema:...}; name and
strict are negotiation metadata rather than native payload fields. An existing
native format must match. Independent output_config settings and effort priority
remain intact. Chat continues forwarding its canonical response_format.

Verify Python request-oracle drift, anthropic_request tests and the zero-network
invalid-format agent cases. The actual `anthropic_schema` archive/application test
checks both model rounds, registered tool execution and persisted JSON text.
Provider negotiation does not replace application response-schema validation;
non-schema formats remain explicit refusals. Production-default selection is unchanged.

## Native text/image tool feedback

Set stdio configuration `tool_feedback="multimodal"` and construct the same
NativeGateway with `multimodal=True`. The application binding rejects differing
profiles. Callbacks may return a string or a nonempty ordered list of canonical
text/image_url blocks; unknown fields/types and invalid image detail are refused.
The default remains text. Feedback, frame and retained-byte bounds still apply.
Native Feedback policies opt into model_content; existing text policies need no
change. Both Chat and Anthropic preserve image-bearing tool continuation.

Anthropic accepts URL/base64 images in user/tool content and uses the retained
MIME reconciliation semantics. It never fetches URL images itself. Vision policy
and ownership/media retention still belong to the existing application pipeline.
Verify the Python/Rust content contract generator, model_content/request tests
and the real `anthropic_image`, `chat_image`, `image_without_admission` and
`image_feedback_overflow` application cases using a verified native archive.
Audio/video, non-schema output formats and unported provider profiles remain explicit
refusals; production-default execution selection is unchanged.

## Native Anthropic routed execution

`compile_native_route` admits canonical API-key/none Anthropic connections and
uses the existing Messages endpoint/header helpers. Its owned private carrier
sets `provider_protocol="anthropic"`; callers cannot override protocol, headers
or endpoint. Prepare the trusted canonical template with max_tokens. Current
Messages conversion supports text/images, tools, signed thinking/replay, system/developer
instructions, cache markers and ordinary native effort/choice/stop fields.
Unported audio/video/non-schema-format/discovery/compaction and OAuth refuse explicitly.

For direct Rust composition, call `HttpProvider::with_anthropic(Budget)` before
execution; direct transport callers supply an already-authorized Messages body
to `execute_anthropic_with_text`. The same HTTP owner joins required text work.
Verify `tests/anthropic_request.rs`, the real TCP agent test, native-route tests
and the `anthropic_gateway` application test with a verified archive. Production
execution selection is unchanged; this explicit path is not a fallback route.

## Native Anthropic stream boundary

`tofu_runtime::anthropic::Translator` accepts decoded provider events with
explicit block/byte budgets and an owned reverse tool-name map. `Stream` combines
this translator with native SSE framing, shared tool assembly, borrowed text
spans and stream classification. Replay-only state appears in the returned
assistant `_anthropic_content_blocks`; hosted-tool state is never dispatched.
Malformed tool JSON at terminal/EOF and unrepresentable usage fail closed.

Verify the Python-derived oracle with `scripts/gen_rust_anthropic_oracle.py
--check` and `cargo test -p tofu-runtime --test anthropic`. Measure the isolated
kernel by building `--release --example anthropic_stream_cost` and passing that
binary to `benchmarks/anthropic_stream_measure.py --native-probe PATH`.
The routed native CLI supports explicit Chat, Anthropic and core Responses compositions.
The Anthropic request adapter currently supports text/images, client tools, signed
thinking and complete replay carriers; unsupported media/policy refuses before
dispatch. The parser alone grants no provider or tool execution authority.

## Native tool model rounds

Current native `tool_request` frames carry `model_round`, a zero-based integer
from the admitted scheduler batch. ToolRequest exposes it to the callback.
NativeGateway uses it directly; compositions using the current verified binary
can pass `round_source=None`. Older frames require an explicit factual fallback
source. Missing/negative/boolean identity refuses before application tool parsing.
The field is additive in stdio v1 for existing standalone host callbacks.
Verify multi-round registered application execution with the `gateway_rounds`
case in `tests/storage/test_native_task_terminal.py`; tool records must retain
`llmRound` 0/1 independently of repeated provider IDs and tool ordinal numbering.

## Native registered application tools

`run_native_task(..., gateway=NativeGateway(...), callbacks={})` explicitly binds
registered application tools to the native model loop. Supply the same owned
task/executor, prepared tool catalog and permission map, current project/config,
factual model-round source, decision policy for non-read tools, and bounded
pending count/bytes. Native configuration tool names and permissions must match.
The binding rejects competing callback/approval owners and disposes proofs after
the native client joins its callbacks. Parser, pipeline and handler registration
remain existing application owners; do not construct a direct-handler adapter.

Exact current approval receipts are reused without a duplicate human prompt;
current admission/hooks still run. Native text feedback is supported. A rich
multimodal result requires explicit text/image block admission. Verify
with `tests/tasks_pkg/test_native_gateway.py` and the `gateway_write` case in
`tests/storage/test_native_task_terminal.py` using a verified native archive and
disposable application authority. Production-default composition is unchanged.

## Native non-read host tools

`NativeProcessClient.run(..., approval_policy=...)` requires explicit
`approval_control` receipt/payload bounds for non-read tools. The async
`ApprovalPolicy.decide(ApprovalRequest)` returns a canonical receipt authority
or `denied`; it consumes the existing human/auto/delegated policy, never guesses
from a tool name. Detached argument/caller accessors cannot rewrite the proof.
The request stop event and remaining deadline govern owned waiters; all work
must quiesce before the returned future finishes. ToolRequest exposes a consumed
`call_id`, caller and stop event. The admitted callback must enter the existing
tool gateway and preserve its permission, ownership, hooks and result policies.
`run_native_route` and `run_native_task` forward this policy explicitly. A failed
host tool sends `tool_result.failed=true` rather than stranding its native waiter;
the host preserves the original error and drains native failure before releasing
admission. The host opts into `tool_result_join`; use a newly built matching
archive. Source-only approval tests and `tests/support/native_approval_probe.py`
verify exact consumption and real scratch-only effects. Process loss still needs
application effect reconciliation; approval does not authorize a replay.

## Native text progress

`NativeProcessClient.run(..., on_text=...)` opts into native content/reasoning
frames over the existing stdio owner. `TextDelta` carries the principal, task,
monotonic sequence, channel and accepted text. The async consumer returns None;
only then does the host ACK. Callback failure rejects the frame with its original
cause and drains the native error terminal without fabricating user cancellation.
Repeated cancellation joins started callbacks and ACKs. Configuration with
`text_progress=true` but no consumer fails before image acquisition. Bytes derive
from existing output/round budgets; signatures are not public text observations.
`run_native_route` forwards this port; `run_native_task(..., project_text=...)`
uses the explicit application projector and supplied executor. Existing event
coalescing, ownership, recovery and terminal persistence keep their authority.
Observers must use their returned future for all work, with no detached writes.
Native application tests require a newly built matching archive; an older archive
without the optional schema rejects this opt-in rather than silently dropping it.

## Native execution from a selected v2 route

`compile_native_route(repository, boundary, selection, policy=...)` compiles the
canonical primary candidate and freezes bounded private endpoint/header data.
It does not search another protocol, refresh OAuth, import a model, mint a Slot,
probe connectivity or persist credentials. Owner mismatch/unauthorized selection
fails before secret lookup; unsupported primary paths fail before decryption.
Use `run_native_route` with the owned NativeProcessClient, the single-use route,
a configuration without endpoint/headers/template, admitted read callbacks and
a trusted prepare_template callback. That callback must run existing selected
inference/body policy and return generation parameters with the chosen wire
model, exactly one positive output field, no messages/tools/private control
fields. The declared Deployment output cap is applied before invocation.
The result separates the native event from route/model identity metadata. Proxy
selection remains explicit caller policy. Caller cancellation retains the host's
joined cleanup; consumed routes are never replayed. Nonconsumed carriers expose
close. `python -m tests.support.native_route_probe --archive ARCHIVE` uses the
repository test environment and checks real v2 -> sealed native -> loopback HTTP
with synthetic credentials only. This is authority/materialization coverage,
not billing/durable-route settlement or end-to-end performance admission.

## Optional native stdio local token admission

Configure token_admission only when trusted composition has selected the local
tiktoken tier. Its canonical definition supplies encoding, output field,
input/text/tool-JSON/depth bounds, actor/attempt/identity bounds and CPU worker
stack bytes. Optional total_tokens is finite (zero denies); omission is unbounded.
The configured model string is bound exactly. The native driver derives tools
from its compiled catalog, counts frozen messages/tools and writes the lease
output cap into the actual HTTP request. It never silently chooses a vocabulary,
changes the production backend or substitutes zero on counter failure.
This port inherits local text/framing/image estimates; it does not claim exact
server billing or replace the existing multi-tier counter policy. Missing usage
remains unknown. The scoped CLI counter worker keeps cancel observation live and
joins before returning; caller-supplied bounds must include encoder/stack demand.
Verify current Python token fixtures, both encodings, actual stdio HTTP cap and
second-round refusal, cold cancel with zero HTTP, and the real archive host probe.
`scripts/check_rust_token_ingress.py` needs pinned Python tiktoken 0.14.0 and
already fetched Cargo assets; it verifies canonical vocabulary hashes and makes
no vocabulary network request. Version-bound license overlays supplement
notices omitted from upstream crate packages in standalone archives.
Production resolver/model routing, shared persistent native process ownership,
model-specific non-tiktoken counters and durable account adapters remain pending.

## Native counted stdio shared account

Explicit model_control={ack_ms} plus model_counter selects the host account port;
local token_admission cannot be mixed with it. Rust counts
admitted history once and exchanges reserve/dispatch/settle over the existing
single reader. No local Account mirror is created. A frozen output cap reaches
the actual wire, dispatch ACK precedes HTTP, and acknowledged settlement precedes
later rounds or terminal persistence. Reserve/dispatch inherit the execution
and ACK deadline; settlement gets independent ACK grace. SDK callbacks join on
cancel/timeout and never send expired replies. Process loss joins native execution
before pending lease cleanup; lost settlement ACK reuses frozen paid usage.

NativeModelAccountPolicy uses the caller's existing ExecutionBudget and executor,
checks explicit tree/actor owner identity and preserves unknown sent usage. It
creates no counter, account, slot, worker or backend. A live child keeps tree
completeness false until its existing membership owner closes it. Pricing and
transcript publication remain with their current owners. Do not reserve this same
account through RoundPolicy. Explicit run_native_task/run_native_route delegate
model_policy. Default production entry and whole-system performance remain pending.

**Problem:** restarting a completed round with a fresh shared account could return
spent credit. **Decision:** explicit checkpoint_shared_account plus both policies
captures the quiescent invocation-root ExecutionBudget before journal completion;
load restores it before the native ACK and any model admission. The generated
checkpoint schema binds owner/root, original token/tool/deadline limits and exact
encoding/output-field/wire-model policy. Tokens, total/control tool calls, attempt
ordinals and unknown-usage state survive; restoration is one-time into a pristine
account. Pending attempts, active child membership, changed policy and mixed local
accounts refuse. Native never mirrors the host ledger. Existing journal ownership
and joined former-writer quiescence remain required; this is not crash takeover.
**Alternatives rejected:** resetting credits bypasses limits; copying a live parent
while siblings run loses shared-account authority; another ledger splits settlement.
Verify test_shared_account_checkpoint and the explicit real-archive/TofuDB
test_native_shared_account_recovery pause/close/reopen tests, including exhausted
credit and fractional original deadline. Active-tree recovery and default production
promotion remain pending.

Verify native_model_control unit/installed tests, model/account/stdio gates and the
native_model_account real application case with a verified private native archive.

## Native per-attempt credential headers

**Problem:** a fixed configured token cannot refresh between native model rounds.
**Decision:** accepted model dispatch may return a full headers object over the
existing one-slot model_decision. NativeModelAccountPolicy's optional synchronous
prepare_headers(request) runs on the supplied executor before dispatch intent;
callers bind it to their existing owner/destination-authorized credential resolver.
ModelDispatch freezes bytes and redacts repr. SDK/native validate names, values,
case duplicates and generated forbidden transport fields. Rust moves the fresh
headers into the actual request, replacing configured headers without cloning a
stale set. Header metadata never enters usage, model-account snapshots or journal
state. None retains configured headers; an explicit empty object clears them.

Refresh failure/cancellation before intent is proven unsent by the same host
policy. It freezes dispatch_entered before the authority marker; failure/lost ACK
at or after that marker remains unknown. Callback timeout signals cancellation,
joins physical work and preserves the original timeout if the worker cancels.
**Alternatives rejected:** startup-only resolution cannot rotate long-run tokens;
merging headers retains stale credentials; another reader or vault splits authority;
refunding failed dispatch delivery loses unknown paid usage. OAuth route admission
still refuses: its owner-scoped resolver/destination binding and request dialect
composition must be completed before automatic subscription use or default cutover.

Verify native_model_control unit tests, the seven-case actual stdio header matrix,
model_admission direct-header failures and test_native_dispatch_credentials_installed
with an explicitly verified archive. The installed test rotates credentials over
two actual model/tool rounds and preserves typed refresh failure without a second
HTTP call. No live subscription, network credential refresh or performance gain
is claimed by these fixture tests.

## Native shared model admission

**Problem:** counted HTTP was tied to one process-local Account, preventing an
existing shared-tree authority from authorizing the same frozen request.
**Decision:** with_model_admission selects an explicit Admission/Lease port instead
of constructing a native mirror account. The native path keeps its inline lease.
InputCounter runs once on admitted history; one returned output cap is frozen,
validated against the counted proposal and written to the actual wire. Joined
sender intent precedes HTTP. Settlement preserves reported usage, and unknown
intent delivery can never be voided as unsent. Refusal, invalid cap, cancellation
and serialization failure cannot dispatch; known unsent reservations are released.
**Alternatives:** a second account duplicates reservations; counting before prompt
preparation authorizes stale input; rereading a mutable cap opens a validation gap.

Ports are trusted, owner/task-bound and must join/fence unknown delivery.
Callbacks receive only read-only wake/deadline observations, not the parent
session or its resource/terminal authority. They
create no route, tokenizer, pricing default or retry. Actual TCP tests reuse one
shared Account, verify both output fields, paid settlement, observer failure and
follow-up refusal. Fault tests cover lost/panicked dispatch intent and zero HTTP.
Counted stdio and application shared-budget adapters are explicit as described
above; pricing recovery and production default promotion remain pending.

## Native counted HTTP composition

Rust embeddings may wrap HttpProvider with `with_account`, supplying an explicit
Account, owner/task binding, InputCounter and OutputField. InputCounter counts
model input from the immutable borrowed messages and template/tool catalog; no
default count or tokenizer is invented. The declared output field must carry a
positive integer and the other supported output field must be absent. The lease
cap is written into the actual HTTP body without modifying the frozen template.
The native account shares the tree cap, honors trusted actor-local slices,
restores model totals/ordinals once and preserves unknown usage across requests.
A native observer is an acknowledged lifecycle port: failure/panic cannot
certify settlement or start a follow-up request. It does not replace billing,
route policy, child-membership durability or terminal persistence. Do not also
reserve the same token account through a host RoundPolicy. Separate wallet or
routing authorities remain explicit composition concerns. Stdio explicitly selects the native counted provider through token_admission;
registered model counters and shared application account/pricing adapters remain
required before production cutover.
Verify `scripts/check_rust_model_account.py`, native model_account tests and real
TCP output-cap/follow-up refusal. The current source gate executes unchanged
canonical account/lease bodies with their canonical validator/usage functions,
without importing app/pricing side effects or contacting a model.

## Native usage evidence for round policies

Returned RoundSettlement evidence includes raw `usage` and an additive
`usage_observation` object: `tokens` is the observed lower bound, `complete`
records whether both provider counters and required cache evidence are known,
and `error` identifies unsupported numeric representation. An empty usage object
reports zero lower bound with complete=False; this is not evidence of free work.
Overflow and unsupported non-ASCII numeric text report tokens=None, complete=False
and an explicit error. Other malformed fields follow the canonical cleaning
rules. Native counters support u64; pricing/money remains with the cost authority.
Raw usage is preserved, including incomplete and mixed vendor fields. A policy
must not refund unknown usage or substitute history_messages for token counts.
Verify `scripts/check_rust_usage_oracle.py`, native usage tests and the archive
probe's OpenAI-inclusive/Anthropic-residual fixture rounds before changing this
boundary. This evidence port does not yet supply sealed input-token admission.

## Native terminal persistence composition

Construct checkpoint_journal::Policy with Policy::new(lease, recovery, suspend).
Its terminal_persistence(exact_store_arc, existing_persistence_arc) supplies the
Task persistence port; the Store must be the exact instance used for acquisition.
The owned wrapper survives terminal retry after the execution stack is gone.
Settlement/frozen-candidate ownership stays in Task. A failed seal prevents
persistence/publication; successful seal is idempotently reread on retry. A
committed Suspend may keep its completed frontier only for Aborted; ordinary
Stop or failed cleanup seals it. Caller releases a paused lease after joined
terminal handling. No automatic restart redispatch or financial/time recovery
is granted. Fence and task writes are sequential, so interrupted completion
still needs canonical orphan settlement; this API is not a crash-recovery store.
## Native owned checkpoint callbacks

**Problem:** borrowed Journal/Lease lifetimes prevented long-lived application
callbacks from retaining the native transition authority. **Decision:**
checkpoint_journal::OwnedLease::acquire(scope, exact_store_arc, snapshot_bytes,
identity_bytes, token) acquires through Journal and retains its original lease
state. OwnedPolicy::new(lease, explicit_recovery_arc, suspend) implements the
same Agent checkpoint port. Recovery is Send + Sync and retains financial/time
authority; it grants no new budget. terminal_persistence(existing_port_arc)
binds the exact retained Store without accepting a replacement. release remains
an explicit execution-owner action after joined pause/terminal handling.

Borrowed and owned calls share one Arc of token/generation/loading/ready/poison
flags. An immutable callback scope is borrowed; no scope strings or history are
copied for metadata calls. Durable CAS remains the concurrency authority.
One-shot load, stale generation, unknown-delivery poisoning, committed Suspend
and frozen terminal retries retain existing semantics. Drop never releases.
**Alternatives rejected:** leaked/self-referential journals obscure lifecycle;
reattaching tokens can resurrect stale owners; a Python journal duplicates the
transition authority. No worker, timer, authority or history cache is added.
Verify checkpoint_journal/agent tests, native-store reopen and fault neighbors,
MSRV and examples/checkpoint_owned_cost.rs. The benchmark compares metadata-only
acquire/load/release over a memory fixture; it excludes storage I/O and cannot
certify application latency. FFI/semantic application binding and the production
Rust default remain pending.

Local release measurement (2026-10-10, six alternating 10,000-cycle samples)
was 8.417 us median borrowed vs 8.771 us owned for acquire/load/release.
Inline OwnedLease/OwnedPolicy sizes were 120/152 bytes; shared heap metadata and
caller-owned Store/Recovery are additional. This is callback overhead evidence,
not a speedup or a complete resident-memory/production-latency measurement.
## Native TofuDB checkpoint embedding

Enable tofu-db/native-runtime-checkpoints only for explicit native embedding.
NativeCheckpointStore receives the existing Arc<Mutex<AuthorityDatabase>>,
tenant/owner and header/snapshot byte budgets; it opens no paths. Journal uses
this Store unchanged. Host code still owns joined release, terminal seal and
financial/time recovery. This does not select an application storage backend.
Header and snapshot are separate versioned records committed atomically; Keep
updates header only. Blob snapshots use the existing owner-bound codec and GC.
Generic record access is denied. Runtime provider-json-order remains default-on;
the DB dependency opts out. An insertion-order Map makes Store construction fail
before effects: combined provider-order embedding is not supported by this codec. Configured outbox now captures native_checkpoint_outbox_v1 mutations atomically.
NativeCheckpointStore.replay takes an explicit source UUID, cipher and sealed
record for a passive target without an outbox. It verifies scope/digest/schema,
pins expected version and commits state plus one latest replay witness. Current
redelivery is idempotent; direct older/gapped records refuse. NativeCheckpointSink
receives a target, existing EngineLogicalOutboxSink, cipher and source UUID. Its
archive proof grants old-version retry only for the same archive-bound confirmed
target prefix. NativeCheckpointStore.publish_pending uses caller-supplied batch
budgets, fetches without retaining the writer, publishes target/archive and ACKs
source in order. Target/archive failures expose restart evidence. Reopen before
retrying uncertain publication; no receipt is returned before both destinations
are durable. Cross-domain and application routing remain unsupported.
## Native journal store boundary

checkpoint_journal::Journal acquires one scoped versioned lease. Store adapters
must atomically compare the header version and write header plus Replace(snapshot)
with durable acknowledgement; Keep must preserve snapshot without decoding it.
Snapshot reads are pinned to that record version. Lease load is one-shot; begin
requires successful load and persists inflight before any effects. Policy binds
the existing Agent port and an explicit Recovery for pricing/elapsed time.
Release is caller-owned after joined Suspend; seal is caller-owned with terminal
composition. Missing acknowledgements poison the handle; inflight, terminal and
still-owned frontiers refuse acquisition. No timeout or Drop steals ownership.
Adapters must bound input bytes before materialization; identity and snapshot
budgets are explicit composition inputs. Tests use an atomic in-memory store;
this API alone does not enable application crash recovery or switch storage.
## Native embedded checkpoint binding

**Problem:** application callbacks needed the running authority, and a shared
WAL commit could return before fsync. **Decision:** with native-runtime-checkpoints,
Arc<EmbeddedRuntime>.checkpoint_lease(scope, header_bytes, snapshot_bytes,
identity_bytes, token) returns an OwnedLease on that runtime's exact database.
Owner must match its authenticated scope; tenant follows the existing scope
encoding. It opens no authority and creates no worker/driver. Explicit byte
bounds, Recovery, joined pause release and terminal persistence remain caller-owned.
Lease callbacks retain the runtime; drop them before opening a replacement authority.

NativeCheckpointStore captures a generation-bound WAL witness under authority
and waits outside its writer before returning reads/CAS, passive replay/current
retry, pending publication or source ACK. Plain Strict inline-sync is supported;
deferred WAL without a coordinator refuses construction before any lease write.
Latched sync errors refuse reads and retries, preserving the live journal fence.
Runtime close marks closed, joins entered checkpoint Store calls through a shared
read/write lifecycle guard, then retires existing drivers. Closed or disarmed
callbacks refuse before I/O; Drop still never releases the durable lease.
**Alternatives rejected:** cloning/opening a database splits authority; waiting
under its writer blocks commit pipelining; treating commit/visible metadata as
fsync can publish an unconfirmed terminal frontier. Verify real runtime owner/
close/reopen, blocked flush/close joining, native-store WAL fault/replay/ACK tests
and existing checkpoint neighbors. Rust binding is explicit; C ABI/semantic
routing, SQL adapters, crash takeover and production-default execution remain pending.
## Native checkpoint C ABI and standalone SDK

**Problem:** installed hosts lacked callable native journal ports, and output
buffer retries could repeat one-shot load. **Decision:**
tofudb_embed_checkpoint_supported negotiates the explicit native-runtime-checkpoints
feature; tofudb_embed_checkpoint_execute carries create/load/begin/complete/seal/
release/forget under native_checkpoint_control_v1. It reuses the existing embedded
request envelope, owned worker queue, panic containment and watchdog. Ordinary
storage execute cannot enter checkpoint control. Capability IDs are allocated
monotonically within one live handle; the lazy registry retains only live leases
and response bounds, not snapshots, receipts or durable task state.

Before enqueue/effects, capacity must cover admitted snapshot bytes plus generated
headroom. Borrowed preflight parses control metadata without decoding snapshot
history; only the worker materializes completion state. Buffer refusal cannot
consume acquisition or load. SDK EmbeddedRuntime.checkpoint_control requires
explicit capacity/deadline, joins calls before close and never grow-replays a
checkpoint command. Generated constants come from gen_native_checkpoint_control.py.
Native errors preserve the journal code. Missing/old feature refuses explicitly.

Forget/handle close drops volatile capabilities without releasing durable leases;
callers must join capability calls before forget and release only joined eligible
frontiers. Unknown results never authorize automatic retry or token attachment.
**Alternatives rejected:** direct synchronous FFI bypasses worker/watchdog lifecycle;
a second checkpoint database splits authority; generic response retry repeats load;
retaining response/history debt in the registry grows with context. Real C ABI
and loaded-library SDK tests cover same-authority reopen, owner denial, expiry,
small buffers, one-shot load, monotonic IDs and inflight close fences. C ABI is
explicit opt-in; application policy/daemon/SQL binding, terminal debt reconciliation
and production Rust default remain pending.

Local x86_64 debug footprint probe measured registry/entry/lazy-slot inline
sizes 40/16/16 bytes; Arc/BTree allocations, lease and retained runtime are
additional. Forget removed the live entry; no history was retained by registry.
## Native checkpoint host policy

NativeProcessClient.run, run_native_route and run_native_task accept an explicit
checkpoint_policy together with round_policy. Its immutable binding identifies
the owned model/tool/account policy. async load(CheckpointRequest) returns None
or an eligible native checkpoint after acquiring its recovery lease and restoring
host budgets; begin returns None only after durable inflight CAS; complete
returns continue or suspend only after committing the completed frontier.
Requests carry identity, action/round, epoch/binding, cancellation and the current
monotonic deadline; complete also carries native_agent_checkpoint_v1 state.
Observers must cooperate and join their work. The caller owns lease cleanup after
the native run returns. Callbacks never automatically repair inflight/terminal
state. Oversize/invalid/failed callbacks stop before later effects and keep their
original cause after terminal acknowledgement. Native token_admission state restores settled token usage, last attempt ordinal
and usage completeness through the pristine one-time account boundary. Exact
encoding/output-field/total-budget policy must match; missing account state or
removing admission is refused. Unknown usage stays unknown and exhausted budgets
cannot dispatch another request. Host pricing and original deadline authority still require trusted recovery;
native token restoration does not reconstruct either. Stdio preserves a supplied
absolute deadline as specified below.
No storage backend or automatic application recovery is selected by this port.
## Native absolute execution deadline

Optional deadline_unix_ms is the application's original UTC expiry, including
zero for already expired work. run_native_task reads the earliest validated task
or shared-tree deadline without creating another account and can only shorten it.
Stdio clamps deadline_ms by wall-clock remaining time observed once; start_observed
subtracts observation-to-entry delay, then uses the existing monotonic session.
Depleted time creates a cancelled session for ordinary settlement and terminal ACK.
Checkpoint state retains the exact UTC value. Added, changed or removed deadline
profiles refuse before restored model dispatch, and before account restoration.
The host must preserve the original UTC authority across restart; wall-clock
correction across process restarts remains an application clock assumption.
Generic embedded checkpoint Recovery still owns pricing/time authorization.
Verify real agent_stdio expiry/shortened-budget/recovery cases, session observed
budget tests and the application shared-tree deadline tests. Default execution
promotion and whole-system performance remain pending.

## Native checkpoint terminal host policy

When checkpoint_policy and terminal_policy are composed, supply async
seal(CheckpointTerminalRequest). It receives principal/task/epoch/binding, the
immutable TerminalRequest, a native suspended observation and cooperative
cancellation signal. Return accepted only after durably fencing recovery, retry
only for an idempotent reconciliation, or rejected. The host calls seal even if
load failed or acquisition delivery was unknown: the policy must reconcile its
owned scope, preserve uncertain ownership and refuse foreign/active authority.
A proven no-acquisition outcome may accept without a write. Only suspended=True
may preserve the durably completed frontier; normal Done/Error/Aborted seals
terminal. Host cleanup after return still owns joined lease release.

The SDK enables checkpoint_terminal_fence and requires checkpoint_suspended in
terminal requests. Rust sets it only after an accepted complete Suspend and an
Aborted terminal candidate; cleanup Error reports false. The marker and event/
receipt freeze together across retries. Seal runs before TerminalPolicy.persist;
a retry skips task persistence, and accepted seal is reused for later task commit
retries. Attempts/grace remain those of terminal_persistence. Seal receives no
expired execution deadline: terminal durability must still settle interrupted
runs. Cancellation signals and joins the callback before task persistence/ACK.
This ordered pair is not an atomic cross-domain transaction; a crash after the
checkpoint fence requires existing orphan terminal reconciliation. Missing seal
fails before binary acquisition. Checkpoint-only transient composition remains
unchanged. Verify native-process ordering/fault/cancellation tests, real
agent_stdio checkpoint retry cases and the installed native_context application
case; this port does not select a storage backend or promote the default runner.
## Project input persistence seam

Custom ConversationStore implementations expose inject_project_messages(task,
messages, round_num), confirm_project_messages(task, round_num) and
confirm_project_context_consumption(task), in addition to the existing narrow
confirm_project_context_delivery(delivery) command. The application adapter owns
project identity/coordinator checks and acknowledgement semantics; core supplies
the explicit owned task and only calls consumption after verified provider return.
Native settlement invokes the same consumption helper after accounting and
before deferred inbox confirmation, suppressing it on failure/cancellation.
The default adapter skips these operations for _transientRuntime=True without
importing host project/storage modules. Custom hosts implement their own policy;
missing methods never trigger an implicit application-store fallback.
## Canonical gateway progress composition

For run_native_task with NativeGateway, configure no_progress_rounds and/or
nonretryable_failure_rounds explicitly, with round_policy and the existing
feedback_bytes bound. NativeGateway owns the observer; a separate progress_policy
is refused for this composition. Non-gateway native routes keep the explicit
ProgressPolicy port. Gateway results publish only current-batch hashes after
settlement; the native call count must match before resultEvidenceComplete is
true. Locally refused/denied or missing results therefore remain incomplete.
Verification stays unset unless a separate future authoritative policy supplies
it. Failure signatures require every observed call to be non-retryable, using the
shared root normalization at each runner's existing argument boundary. Evidence
is consumed after observation and released on close; budget overflow fails open
for the breaker and never reexecutes or weakens tool settlement.
## Native post-tool progress observations

run_native_task, run_native_route and NativeProcessClient.run accept an explicit
progress_policy together with round_policy. Its frozen ProgressLimits enables
one or both breakers, and async observe(ProgressRequest) returns exactly
worldVersion, evidenceIds, resultEvidenceComplete, verification and
nonretryableFailureSignatures. Observations belong to trusted application result
policy; incomplete result evidence must remain false, and terminal failure
signatures require every call to have failed non-retryably. The request carries
owner/task/round/call count, a cancellation event and the inherited monotonic deadline.
Observers must use their declared executor, cooperate with this signal/deadline,
and join work before return; Stop does not detach callbacks. No credentials or
message history cross this port. Invalid observations stop before another HTTP
round and preserve the original callback failure after terminal acknowledgement.
The application default root still requires separate migration.
## Shared native root context preparation

NativeContextPolicy calls the same prepare_round_context/admit_round_context as
the default root adapter. Explicit project_path/project_enabled configure attachment
policy. Hygiene precedes inbox/checkpoint/project/swarm/evolution/program-input
refresh, then complete prompt admission and execution-allowance notice re-admission.
Only a verified provider_finished return after accounting confirms deferred inbox
messages. RoundSettlement.cancelled reflects host close or cancellation during
settlement; cancelled/failed returns leave messages recoverable. Installed inbox
cases verify actual queued operator input in the model request and one confirmation
while retaining tool feedback and encrypted reasoning. Proactive summaries require
an existing authorized route; native core does not infer server-compaction support.

## Native preparation deadlines

Prepared RoundRequest optionally carries deadline_monotonic_ns, an inherited
local Linux CLOCK_MONOTONIC deadline shared across rounds. It includes publication
and queue time; None retains direct-composition compatibility. NativeContextPolicy
uses the fact for one call-local timeout, checks worker/summary entry and joins
cancellation. Queued executor work cancels without waiting for unrelated work;
running work observes the task abort event. After expiry the host does not send
an old round_decision into the post-settlement terminal carrier. Native deadline
abort and the host TimeoutError cause remain distinct observable outcomes.
Verify running/queued timeout unit tests, fixed-deadline stdio and installed
context-deadline persistence cases; no model/tool/accounting effect may occur.

## Native canonical context policy

Pass NativeContextPolicy as run_native_task's round_policy with the same principal,
owned task, executor and an explicit accounting delegate (reserve/settle only).
The issued route binds metadata, clamped output and configured tool catalog before
native acquisition. No credential headers cross this policy port. Preparation
uses enforce_dispatch_prompt_limit, including required checklist restoration and
existing pressure/compaction behavior. Missing context facts or impossible output
reservation refuse; no window is invented. Semantic L2 requires an initialized
owned pin matching the compiled primary; otherwise it refuses before summary I/O.
Verify native-context isolation/cancellation tests, route-budget/summary neighbors,
generated tool projection and installed context success/refusal application tests.
This policy is a migration adapter; root context injections and default selection
are not changed. It neither authorizes a new summary route nor invents refunds.

## Native per-round history preparation

An optional async `prepare(RoundRequest)` on the supplied round policy enables
round_preparation. Its request.messages contains canonical history; return a list
of canonical message objects. The host validates the reply frame before calling
reserve; Rust validates prepared retained bytes before actual HTTP. The same
round_request/round_decision slot carries history, with no extra reader or loop.
round_preparation_bytes defaults to the host's admitted frame budget and may be
explicitly reduced. Omitted preparation preserves the existing count-only protocol.
A preparation exception publishes an explicit native failure before being raised
as NativeRoundError with its original cause after terminal/exit confirmation.
Invalid native retained history reports NotDispatched; preparation grants no tool,
credential, route, budget or compaction authority. Verify host cancellation/fault
unit tests, actual large-frame stdio/Stop tests and installed Responses preparation
application tests. The existing application policy must supply compaction and
injection semantics before the production entry can adopt this port.

## Native per-round host policy

Pass `round_policy=RoundPolicy` to NativeProcessClient.run to enable the optional
round-control protocol. `reserve(RoundRequest)` asynchronously returns an exact
bool; only True authorizes that round. Requests bind the supplied principal,
task, ordinal and history-message count. They carry no token/price estimate.
`settle(RoundSettlement)` must return None after recording raw Returned, Failed,
Stopped, NotDispatched or Abandoned evidence. Native settlement waits for the
host ACK before later effects. Missing usage remains unknown. These callbacks
are policy ports, not durable billing or model-routing implementation.
Reservation/accounting errors raise NativeRoundError with their original cause;
no callback retry or automatic model fallback occurs. Cancellation joins policy
callbacks before releasing invocation admission. An approved reservation whose
native settlement was not observed receives one Abandoned callback after exit.
A settlement callback already attempted is never attempted again. Callbacks must
quiesce; ACK transport alone is bounded by shutdown_seconds. Without the optional
policy, the previously authorized transient configuration retains its original
behavior. Configuring round_control without a policy fails before launch.

## Native host adapter for application composition

The internal `lib.agent_core.native_process` port invokes an installed native
archive from an async Linux host. Construct NativeProcessClient with the trusted
manifest binary checksum/protocol version, an explicit PrincipalContext and a
Budget supplied by launch policy. Pass `run()` a previously authorized bounded
native configuration, matching admitted read-tool callbacks and an absolute
working directory. ToolRequest carries the same principal and exact task/request
identity. The returned dictionary is the native terminal event; no second model
loop or task terminal is fabricated by the host. Close the client before disposing
its event loop. Callbacks must quiesce on cancellation; callback errors retain
cause under NativeToolError and never become tool evidence or automatic retries.

The adapter is fail-fast at its declared admission bound. It validates and seals
a cached executable image, so source-path replacement does not change launched
bytes. Configuration and tool content never enter subprocess arguments or stderr
logs. Close drains/cancels and joins native execution before releasing admission
and the image. Older Linux Python/libc builds use the kernel memfd/seal UAPI when
stdlib bindings are absent; unsupported kernel/ABI execution fails closed.
`python -m tests.support.native_process_probe --archive ARCHIVE` verifies real
host/native/model/tool integration against synthetic loopback provider fixtures.
This port is not yet selected by AgentRuntime/serve; v2 route compilation,
durable account policy, write approval, durable checkpoints and progress remain
owned integration work. No production backend is silently switched.

## Standalone native archive

Using Python 3.12 for build tooling,
`python scripts/build_native_runtime.py --output OUTPUT_DIRECTORY` builds the
locked native GNU/Linux host target and creates
`tofu-native-VERSION-TARGET.tar.gz`. The archive contains the executable, exact
lockfile, runtime/stdio contracts, license files and a SHA-256 member manifest;
it contains no Python interpreter, Cargo, compiler or application checkout.
`--offline` requires previously cached dependencies. `--target-dir` declares
build-cache placement. Existing output paths are refused, never overwritten.
Archive bytes are deterministic for identical member bytes and metadata.

`--check ARCHIVE` validates inventory, checksums, member/byte bounds and rejects
links/traversal/duplicate paths before extraction. On the matching native host,
it then runs build-info, session settlement and default-deny tool checks from a
fresh temporary directory with an empty command PATH. This verifies installed
execution; checksum integrity does not authenticate a publisher. The metadata
records compiler, application version, crate version, target and dependencies.
The contract is `contracts/native_runtime_artifact_v1.json`.

The native-runtime workflow checks Rust 1.85.1, runtime/private-core tests and
the standalone archive. It uploads a seven-day CI artifact only; it does not
publish a release or change the application backend. Initial local certification
covers GNU/Linux x86_64. Native aarch64 building is supported by the script but
not certified by that observation; the target triple alone does not certify a
minimum glibc or arbitrary distribution. Full production migration remains pending.

## Native Rust stdio entry

Linux `tofu-native run-http-agent CONFIG_FRAME_BYTES` runs supported text/Chat
rounds without a Python model loop. Its contract is
`contracts/native_agent_stdio_v1.schema.json`. Supply one newline-terminated
configure object with explicit owner/task/epoch, endpoint/proxy/headers, request
template, messages, compiled read-tool catalog, HTTP policy and all byte/round/
deadline budgets. The template omits messages and tools; Rust derives request
tools from the catalog and projects each round's history. Stdin is an exclusive
pipe/socket; tool-request stdout must be an exclusive FIFO pipe. Regular-file
stdin is not supported by this readiness-based entry.

The host reads `tool_request`, executes the named declared read tool under its
own authority and returns `tool_result` with the exact request_id and text
content, including empty text. A `cancel` frame during provider or result wait aborts; the
deadline also wakes silent provider/client waits. Invalid versions, extra fields,
wrong IDs, EOF and oversized responses stop further provider rounds. Tool
request frames must fit PIPE_BUF so cancellation cannot expose partial requests.
The final stdout line is the ordinary task terminal event, after resource
settlement. No stderr diagnostics expose credentials or request payloads.

This transient port does not yet replace Python AgentRuntime/serve: full provider
routing, write approval, checkpoint storage, progress, billing and multimodal
policy remain pending. The Rust subprocess tests cover actual stdio/TCP
composition and protocol failures; they do not certify application performance.

## Product boundary

```text
Python caller ── tofu_agent.AgentRuntime ─┐
                                         ├─ transient TaskRuntime
HTTP caller ── tofu-agent sidecar ────────┘          │
                                                    ▼
                                        production agent orchestrator
                                                    │
                                          v2 route / tools / MCP
```

`AgentRuntime` uses the same task and agent execution seams as the full
application. Each invocation carries an explicit `PrincipalContext`, owns one
bounded transient runtime, and uses the canonical
`tofu.model-routing/v2` candidate compiler. It does not initialize full-app
routes, conversations, accounts, billing, scheduler state, or the storage
sidecar. Task/event replay, idempotency, and active runs exist only in bounded
process memory and disappear at restart.

This is a composition mode, not another orchestrator or provider-routing state
machine.

**Ingress affinity initialization — Problem:** ordinary embedded and worker
task creation imported Quart solely to discover that no request existed.
**Decision:** `manager/_registry.py` queries request affinity when Quart is
already loaded. Web requests retain clipped direct keys and reconnectability;
background tasks retain conversation-store inheritance and durable birth
metadata. Explicit principal/owner validation still precedes every task.
**Alternatives considered:** importing the framework on every first task taxes
storage-free composition; skipping affinity for all transient tasks loses real
request routing metadata. Verify `tests/tasks_pkg/test_task_ingress_affinity.py`
and `benchmarks/task_affinity_measure.py`; first-task clocks exclude common
imports, and loaded-web/long-residency checks remain separate boundaries.

## Release artifacts

All public artifacts use the repository [`VERSION`](../VERSION):

| Artifact | Public name | Entry point |
|---|---|---|
| Python runtime | `tofu-agent` | `import tofu_agent`; `tofu-agent` CLI |
| Python remote SDK | `tofu-sdk` | `import tofu_sdk` |
| TypeScript SDK | `@rangehow/tofu-sdk` | `import { Tofu } …` |
| OCI runtime | `ghcr.io/rangehow/tofu-agent:<version>` | port `15001` |

The runtime wheel contains the agent kernel, model-routing compiler, provider
wire adapters, tools/MCP, and a dependency-free static setup page. It excludes
the full application routes, storage implementations, Tofu UI assets, tests, and
runtime data. The OCI `agent` target installs that wheel into a
dependency-only final image.

The OCI build installs the standalone `tofu-repo-kernel` platform wheel into
API, Worker and Headless environments. Rust compiles only in the dependency
builder; runtime images contain `libgcc-s1` and the verified wheel. Image gates
reject compiler/database-server tools and require both `python -m tofu_repo_kernel`
and the application lazy selector to certify native activation. The portable
Python runtime wheel keeps the native component optional and its existing
missing/invalid-component fallback; no runtime compiler or download is added.


工具结果的 capture/composition 只依赖可随 wheel 分发的纯 Python 契约投影，
不得在导入时依赖完整应用的 `lib.storage` 或 checkout 的 `contracts/`。
`contracts/tool_result_query_v1.json` 是唯一源；生成器
`scripts/gen_tool_result_query_contract.py` 产出 `lib/tool_result_query_generated.py`，
由发行成员门禁和 `tests/tools/test_result_contract_distribution.py` 校验。
持久化才进入 owner-scoped storage 边界；轻量运行时的小结果无需存储服务。

## Model-routing ownership

The standalone runtime accepts one complete access envelope:

```json
{
  "model_routing": {
    "contract_version": "tofu.model-routing/v2",
    "revision": 0,
    "creators": [
      {"creator_id": "openai", "name": "OpenAI"}
    ],
    "models": [
      {
        "creator_id": "openai",
        "model_id": "gpt-x",
        "display_name": "GPT X",
        "capabilities": ["text", "thinking"],
        "context_window": 131072,
        "quality_rank": 100
      }
    ],
    "providers": [
      {"provider_id": "gateway-a", "name": "Gateway A", "scope": "owner"}
    ],
    "provider_accesses": [
      {
        "provider_access_id": "gateway-a-access",
        "provider_id": "gateway-a",
        "enabled": true,
        "quota_policy": {"rpm": 60}
      }
    ],
    "connections": [
      {
        "connection_id": "gateway-a-primary",
        "provider_access_id": "gateway-a-access",
        "base_url": "https://models.example/v1",
        "protocol": "openai",
        "enabled": true,
        "priority": 0,
        "extra_headers": {}
      }
    ],
    "credentials": [
      {
        "credential_id": "gateway-a-key",
        "provider_access_id": "gateway-a-access",
        "kind": "api_key",
        "secret_reference": "gateway-a-secret",
        "key_hint": "configured",
        "enabled": true,
        "authorization": {
          "connection_ids": ["gateway-a-primary"],
          "models": [{"creator_id": "openai", "model_id": "gpt-x"}]
        },
        "quota_policy": {}
      }
    ],
    "offerings": [
      {
        "offering_id": "gateway-a-gpt-x",
        "provider_access_id": "gateway-a-access",
        "identity_state": "confirmed",
        "model": {"creator_id": "openai", "model_id": "gpt-x"},
        "enabled": true,
        "capabilities": ["text", "thinking"],
        "context_window": 131072,
        "priority": 0
      }
    ],
    "deployments": [
      {
        "deployment_id": "gateway-a-gpt-x-primary",
        "offering_id": "gateway-a-gpt-x",
        "connection_id": "gateway-a-primary",
        "wire_model_id": "vendor/gpt-x",
        "identity_confidence": "high",
        "probe_status": "unprobed",
        "priority": 0
      }
    ]
  },
  "model": {"creator_id": "openai", "model_id": "gpt-x"},
  "routing": {"preferred_provider_id": "gateway-a"},
  "credential_secrets": {"gateway-a-secret": "sk-..."}
}
```

The aggregate contains only redacted credential metadata and opaque secret
references. `credential_secrets` is a separate map keyed by those references;
it is excluded from `repr`, readiness, capabilities, setup reads, task
snapshots, and results. A non-local enabled Credential must have exactly one
configured secret value. Plain model strings, `model@provider`, provider
blocks, aliases, and request-ID pools are rejected.

The configured `model` is the process default. Each run may override it with
another structured official Model or provider-scoped pending Offering from the
same aggregate and may supply request `routing` policy. Each admitted run gets
a request-owned slot group and keeps it until terminal settlement; a setup
change affects only later runs.

### Authority order

The sidecar resolves configuration in this order:

1. `--model-routing-json` or `--model-routing-file`;
2. `TOFU_AGENT_MODEL_ROUTING`, containing the exact JSON envelope above;
3. the encrypted saved setup document;
4. unconfigured.

`TOFU_AGENT_MODEL_ROUTING_FILE` supplies the CLI file default. CLI/environment
authority makes the setup API read-only until the override is removed and the
process restarts. `.env` values never override already-present process
environment values.

A fresh sidecar may start unconfigured. `GET /health/live` remains 200 while
`GET /health/ready` returns 503 with `setup_required:true`. The bundled
`/setup` page edits the complete v2 aggregate, sends one minimal probe against
the computed primary Deployment, and hot-applies a successful save.

### Encrypted setup storage

| Item | Default / override |
|---|---|
| Model-routing document | `~/.config/tofu-agent/model-routing.json`; `TOFU_AGENT_CONFIG_PATH` overrides it |
| Encryption key | Adjacent `model-routing.json.key`; `TOFU_AGENT_CONFIG_KEY` overrides it |
| Permissions | Owner read/write only (`0600`) |

`XDG_CONFIG_HOME` is honored on Unix and `APPDATA` on Windows. The file
stores public aggregate metadata plus one Fernet-encrypted
`credential_secrets` envelope. File and key writes are atomic; a corrupt file,
missing key, unsupported schema, or decryption failure fails closed. Back up
the document and adjacent key together. Delete removes the document but retains
the key to avoid secret churn on a later save.

## Python composition

```python
from tofu_agent import AgentRuntime, ModelRoutingConfig

access = ModelRoutingConfig.from_mapping(model_routing_envelope)

with AgentRuntime.local(model_routing=access, max_inflight=4) as runtime:
    execution = runtime.start(
        [{"role": "user", "content": "Inspect this project"}],
        routing={"preferred_provider_id": "gateway-a"},
        config={
            "thinking": "high",
            "tools": ["search", "fetch"],
            "project": "/workspace/project",
        },
        request_id="run-owned-by-caller",
        timeout_s=600,
    )

    for event in execution.events(cursor=0):
        consume(event)
    result = execution.result()
```

`AgentRuntime.local()` constructs an explicit personal principal. A server or
enterprise adapter constructs `AgentRuntime(principal=..., model_routing=...)`
with its authenticated owner instead of relying on a module global. A single
request may pass `model_routing=...` to use a complete one-run envelope; it
does not persist or mutate the runtime default.

| Method | Contract |
|---|---|
| `run` / `run_async` | Submit once and wait for a typed terminal `AgentResult`. |
| `stream` / `stream_async` | Submit once and yield native events. |
| `start` | Return an `AgentExecution` immediately. |
| `events(cursor=N)` | Replay from the absolute next sequence, then follow live. |
| `event_page(cursor=N)` | Return one bounded replay page. |
| `snapshot()` | Return the stable `agent.run` projection. |
| `abort()` | Request cooperative cancellation for the owned task. |
| `close()` | Reject new work and perform bounded active-run teardown. |

Admission uses the shared launch hardware budget when `max_inflight` is omitted
or zero; `TOFU_AGENT_MAX_INFLIGHT` or a positive argument supplies an explicit
capacity. The CLI follows the same precedence. `TOFU_RESOURCE_PROFILE=throughput`
scales foreground workers to effective CPU and available memory; the default
`balanced` profile reserves a larger desktop share. Each runtime owns a lazy
dedicated agent executor, closed with the runtime. Async event/result observers
wait on loop notifications and consume no blocked I/O threads. Independent
runtime instances never replace the application's executor.
Overload, configuration, timeout,
closed-runtime, and execution failures retain distinct public exception types.
`abort()` and `close(abort=True)` use the task engine's owned cancellation
boundary: signal both cancellation representations, invoke registered resource
cleanup, and wake client-tool waiters. An expired caller must not leave native
workers consuming shared executor capacity. Cancellation remains cooperative;
the terminal verdict, rather than `status=done` alone, reports its outcome.

### Terminal success and diagnostics

`AgentResult.ok` and `tofu-agent run`'s JSON `ok`/exit status share the canonical
turn verdict. `status=done` only means execution settled, not success. A normal
finish (`stop`, `done`, `completed`, `end_turn`) with no error or conflicting
stream evidence succeeds; missing finish, truncation, cancellation, malformed
streams and exhausted recovery do not. A provider tool handoff is not complete
at this autonomous-run boundary. Failures retain content, tool receipts, usage
and verifier-independent trajectory artifacts; callers must not discard partial
work or rewrite benchmark rewards merely because runtime health failed.

The result optionally carries `stream_state` and `stream_diagnostics`: at most
16 recent per-request parser receipts with trace/round, provider finish and DONE
observations, malformed-frame count, transport bytes, SSE events and output
character/tool counts. These are not summed usage flags and contain no request
bodies, credentials or provider text. They distinguish a positively terminated
empty response from a cut/malformed stream; missing historical receipts cannot
prove which upstream component caused a previous empty response.
ATIF uses explicit current-run transcript provenance;
legacy input-only snapshots rebuild current native tool receipts after the human
request boundary, so historical tools neither suppress nor duplicate this run.

Empty-terminal recovery adds one explicit correction to the preserved history
per attempt and shares a two-recovery whole-task budget. It never rewinds tool
receipts or retries the whole turn after side effects. Cancellation, pending
human interaction and real stream cuts retain their own existing policies.

### Harness compatibility defaults

`tofu-agent run|serve|doctor --harness-profile auto|off|longcat_flash` defaults to
`auto`. Auto defaults to registry-backed full native exposure, preserving explicit
exposure, Tool Search and programmatic-calling settings. LongCat marker recovery
is selected automatically for LongCat model names; other models' text is untouched.
Empty-terminal recovery and workspace safety fixes apply to every model, even off. Python callers can use
`AgentRuntime.local(harness_profile='longcat_flash')` (also supported by the
constructor); each run may override the process default with
`config={'harnessProfile': 'off'}` or `'longcat_flash'`.

The explicit `longcat_flash` profile selects registry-backed full native exposure,
disables Tool Search wrapping, and disables programmatic calling. Common coding
tools (find/search/read/edit/write/command and background command controls) are
therefore direct definitions from the same enabled registry, not copied schemas.
This does not enable disabled capabilities, relax permissions, change execution
scope, or bypass input validation. Existing schema budgets still apply.

LongCat terminal text beginning with `<longcat_tool_call>` or
`<longcat_arg_key>` is classified as invalid protocol for native-tool reissue.
The observed format does **not** establish a safe complete call grammar, typed
argument encoding, or replay identity: this profile never executes XML text,
even apparently complete packets. Quoted examples, code fences and prose are
not normalized; malformed or truncated packets are never guessed. Native calls
in the same response take precedence and suppress text-driven reissue to avoid
duplicating side effects. The shared termination recovery owner controls the
whole-task budget (at most two recovery rounds), not this profile helper.

Desktop-relayed streams propagate cancellation through response-header waiting.
Cancellation closes the pending relay and escapes without trying another device;
this also bounds that wait when the transient compaction budget expires.

## Lightweight HTTP/SSE contract

Start locally with `tofu-agent doctor` and `tofu-agent serve`.

> **Namespace note:** the `tofu-agent` composition mounts the `/api/v1`
> namespace below natively — it is a separate lightweight server, not the
> retired full-backend v1 mount. On the full Tofu backend the same product
> contract is served under `/api/v4` (its `/api/v1` returns 426); see
> [HEADLESS_API.md](contracts/HEADLESS_API.md).

| Method and path | Purpose |
|---|---|
| `GET /setup` | Static model-routing control plane. |
| `GET /api/v1/setup/model-routing` | Redacted active aggregate, source, editability, and storage state. |
| `POST /api/v1/setup/model-routing/test` | Validate the full envelope and probe its computed primary Deployment without saving. |
| `PUT /api/v1/setup/model-routing` | Encrypt, save, and hot-apply the full envelope. |
| `DELETE /api/v1/setup/model-routing` | Remove saved access and unconfigure later runs. |
| `GET /health/live` | Dependency-free liveness. |
| `GET /health/ready` | Redacted readiness and bounded capacity. |
| `GET /api/v1/capabilities` | Installed runtime, model-routing state, events, and limits. |
| `POST /api/v1/agent/run` | Blocking result, SSE (`stream:true`), or HTTP 202 (`async:true`). |
| `GET /api/v1/tasks/{id}` | Current run projection. |
| `GET /api/v1/tasks/{id}/events?cursor=N` | Bounded replay page. |
| `GET /api/v1/tasks/{id}/stream?cursor=N` | Native replay and live SSE. |
| `POST /api/v1/tasks/{id}/abort` | Cooperative abort. |
| `POST /api/v1/tasks/{id}/tools/{call_id}/result` | Resolve one client tool call. |

The sidecar deliberately omits conversations, full-app provider repositories,
accounts, billing, scheduling, files, and feature routes. The setup service
owns one process default aggregate; it is not the full app's owner repository.

A minimal managed-default request is:

```http
POST /api/v1/agent/run
Authorization: Bearer <TOFU_AGENT_TOKEN>
Idempotency-Key: <stable caller key>
Content-Type: application/json

{"messages":[{"role":"user","content":"Hello"}]}
```

`Prefer: respond-async` or `"async":true` returns 202, `Location`, and
`X-Tofu-Task-Id`. `"stream":true` returns `agent.run.chunk` SSE. Every
event has an absolute `id`/`seq`; resume an existing task with
`cursor=last_seq+1` or `Last-Event-ID:last_seq`. Streaming never resubmits a
run after a transport disconnect.

Idempotency is scoped to the runtime principal and bounded by count and TTL.
The same key/body returns the same execution; a different body returns
`409 idempotency_conflict`.

### Authentication

`/`, liveness/readiness, `/setup`, and setup assets are public and contain
no configuration. Every `/api/v1/setup/*`, agent, and task route follows the
same auth policy; setup data routes also reject cross-site browser requests.
With `TOFU_AGENT_TOKEN`, an exact Bearer token is required. Without a token,
automatic mode accepts only loopback bind, peer, and Host authority. The CLI
refuses an exposed tokenless bind unless `--allow-unauthenticated` is explicit;
the OCI image therefore requires a token.

The setup page holds a bearer token only in JavaScript memory. It uses no
cookies, browser storage, or external resources. A remote one-time
`/setup#token=...` fragment is removed from the address bar immediately and
is never sent to the server. Disable setup with `--no-setup` or
`TOFU_AGENT_SETUP_ENABLED=0`.

## State and restart semantics

| Property | Developer runtime | Full application |
|---|---|---|
| Task/event authority | Bounded process memory | Storage sidecar |
| Reconnect | Cursor resume while this process lives | Durable task replay |
| Restart | Runs and idempotency records are lost | Durable domains recover by policy |
| Model access | Encrypted v2 file or explicit envelope | Owner-aware v2 repository |
| Database/full Tofu UI | Never initialized | Declared application lifecycle |
| Accounts/billing | None | Auth and repository boundaries |

Model-access durability does not imply task durability.

## Restart timeline logging

While the browser renders the restart progress bar, the backend attributes
its wall time per stage so a slow restart can be located in one grep:

- `logs/app.log` (old worker, prepare band): approval pending/approved,
  `[Update] in-request frontend preflight passed in Ns`, `[Update]
  Supervisor accepted deferred worker restart in Ns`. When the manager
  cannot be reached or refuses, `[Update] Manager delegation unavailable
  (…); falling back to in-place re-exec` marks the last-resort path, and a
  later `[Update] Restart refused by every bridge; acceptance stamp rolled
  back …` means no restart happened and a retry is safe.
- `logs/server-manager.log` (supervisor + manager, shutdown and boot
  bands): quiescence outcome and wait, `worker pid=N SIGTERM sent`,
  `worker pid=N exited cleanly Ns after SIGTERM` (or the SIGKILL
  escalation warning), sidecar drain/reap, `frontend preflight starting /
  passed in Ns` (a rebuild prints timestamped `Frontend artifact is stale…
  / rebuilt and validated in Ns` lines inline), `worker spawned pid=N
  source=… totalSinceRequest=Ns`, and `worker ready pid=N source=…
  bootTime=Ns` when the readiness probe first passes.
- `logs/server-console.log` (worker image): `production runtime shutdown
  begin / took Ns` plus the per-step shutdown timings, then the usual
  `[boot + Ns] [startup phase M/9] done | name | Ns` lines and the final
  `Boot time: Ns` banner.

UI-originated restarts carry `requestedAt` end to end (update route →
`supervisor_protocol.request_deferred_worker_restart` → supervisor →
`LifecycleManager.restart(requested_at=…)`), so manager stage lines suffix
`totalSinceRequest=Ns`; CLI/automatic recoveries log stage elapsed only.

## Verification and release

Minimum gates for a public runtime change are:

```bash
pytest -q tests/agent_core/test_agent_provider_setup.py tests/tasks_pkg/test_public_agent_runtime.py \
  tests/server/test_headless_agent_server.py
pytest -q tests/tasks_pkg/test_inprocess_facade.py tests/sdk/test_sdk_sse_parser.py
npm run build --prefix clients/typescript
uvx --from build --with 'setuptools==80.10.2' --with 'wheel==0.48.0' \
  pyproject-build --no-isolation
python3 scripts/check_developer_runtime_artifacts.py dist/tofu_agent-*.whl
```

Build from a clean checkout or an isolated setuptools `build_base`; an old
`build/lib` can leak previously packaged application modules despite current
package exclusions. Validate the actual artifact before installation or release.
Stream-terminal changes also run `tests/evaluations/test_harness_stream_end_to_end.py`:
loopback HTTP recovery and exhaustion must preserve each completed tool receipt.

The clean-wheel smoke installs only the wheel outside the repository, checks
that database drivers and full-app assets are absent while setup assets are
present, runs `tofu-agent doctor`, and completes one turn against a controlled
OpenAI-compatible server. Release workflows publish Python/TypeScript/OCI
artifacts from the same version and gate secrets, SBOMs, and image
vulnerabilities before registry publication.

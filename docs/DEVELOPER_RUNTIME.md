# Developer runtime contract

This is the authority for consuming Tofu's agent kernel without the full
application database or full Tofu UI. It defines Python composition, the lightweight
HTTP/SSE sidecar, model-routing setup, transient state, and release artifacts.
The full backend API is documented in
[HEADLESS_API.md](contracts/HEADLESS_API.md).

## Native terminal durability policy

Supply TerminalPolicy plus explicit terminal_persistence configuration to NativeProcessClient.run (also forwarded by run_native_route). The profile declares operation_id, explicit nullable ACK grace, maximum attempts and frame bytes; null delegates waiting to the persistence owner and EOF, never treating exit or late Stop as a commit. Missing/zero grace refuses. Problem: durable-only terminal admission excluded storage-free tasks. Decision: NativeTaskTerminalPolicy and prepared/task entries require explicit `transient=True`, reuse required delivery, reject Turn/wallet/checkpoint/resume authority and fence lifetime changes. Alternatives: implicit task-marker selection weakens durable admission; a second finalizer duplicates settlement. AgentRuntime now accepts trusted NativeAgentConfiguration; CLI run/serve/doctor owns an operator-supplied local archive/profile; default promotion remains pending. Policy.persist receives the same PrincipalContext, task, operation and ordinal plus frozen event/receipt JSON bytes; accessors return detached views. Durable ports accept only after durable acknowledgment; an explicit transient task port accepts after required receiver delivery (no restart durability), retries only idempotently, or rejects. The native Task retains the same terminal candidate across requested retries; terminal visibility follows accepted persistence. Callback failure raises NativePersistenceError with cause, and ACK transport failure remains a durability error. Missing policy fails before launch. A final event that differs from the accepted candidate or arrives without ACK is refused by the host. Cancellation joins callbacks and bounded ACK work. Cleanup can still persist an aborted candidate; late cancellation cannot rewrite an already frozen outcome. Applications must persist/reconcile candidate debt using the supplied operation identity after timeout or process loss; this port never reruns models to repair storage. Frame/attempt/grace limits are explicit, not business success evidence. Real subprocess/archive tests cover bounded persistence retries and joined cleanup. **Problem:** ROOT history also carries private host context metadata. **Decision:** Chat borrows generated fields/order and normalizes text/context seams. **Alternatives rejected:** blacklists leak new fields; stripping history loses context. Verify Python chat-wire oracles, cache/HTTP/ROOT parity; model-specific normalization, default selection, complete protocol coverage and process-loss recovery remain pending.

## Native Responses routed execution

The verified native archive accepts `provider_protocol: responses` and optional
boolean `responses_reasoning_replay` (default false). Application routes bind these
from owner-authorized `openai_responses` candidates and explicit capability
metadata; request overrides refuse before route consumption. API-key/local identity
credentials retain the existing single-use ownership boundary. Codex OAuth requires
the explicit owner/destination resolver described below.
Verify native-route tests, actual stdio tests and registered Responses write/image
application cases with a verified archive. Explicit multimodal feedback remains
required for tool images. This path uses the existing joined HTTP/text/tool/terminal
owners and does not promote the production execution default.

## Native Responses request dialect

**Problem:** the native core Responses serializer lacked the Codex wire dialect, so an owner-selected Codex profile could send rejected sampling/output fields. **Decision:** responses_profile is an explicit compatible/openai/codex stdio field; compiled routes derive it from canonical inference metadata and refuse request overrides before consuming credentials. HttpProvider.with_responses_profile and responses_request::Profile carry that trusted policy through borrowed serialization. Codex emits empty instructions, the canonical include list and default summary, and omits temperature/top_p/max_output_tokens. Values derive from the existing Python RESPONSES_PROFILES authority at build time. Exact reasoning intent remains with preparation; no model-name default or effort remapping occurs in native code. Local token admission still validates its output proposal and settles actual/unknown sent usage; omitted server output limits cannot promise provider-enforced caps. **Alternatives rejected:** inferring dialect from model names changes generic providers; accepting caller overrides bypasses routing; pretending a dropped cap was enforced loses budget evidence. Compatible/openai retain the supported core serialization; other OpenAI extensions are not newly admitted. OAuth credential refresh remains with the explicitly supplied owner/destination auth port.

Verify both generators in check mode, 30 Python-derived request fixtures,
responses_request/agent/agent_stdio tests, actual counted TCP requests for all
three profiles, test_native_responses_dialect_installed and native-route
owner/override tests. These establish request and
settlement parity, not production default or whole-system performance admission.

## Native owner-bound Codex OAuth route

**Problem:** provider-global OAuth files cannot authorize an arbitrary owner, and native routing could not bind live credential resolution to its selected route. **Decision:** compile_native_route accepts an explicit credential_resolver for Codex OAuth only when the authorized candidate selects openai_responses/codex. The issued single-use route binds owner and tenant and carries that port privately; consume/close relinquish its reference. No global token store is selected. run_native_route binds it to NativeModelAccountPolicy for one joined execution scope and releases it afterward. Each NativeCredentialRequest carries the explicit principal, task/attempt, credential ID/private reference, subscription, exact selected endpoint, wire model, stable cache key, private extra headers and cancellation signal. The resolver must validate source ownership, revocation and destination before returning complete headers. Existing owner-aware auth implementations belong behind this port; it is not an authorization bypass or new token vault.

Codex's canonical route_feature binding admits encrypted reasoning replay and cache affinity unless explicitly narrowed. The existing responses_cache_affinity_key helper derives the key from task/conversation identity. A supported cache feature places exactly that key into the borrowed native body; conflicting or disabled cache overrides refuse. Key bytes are bounded by the generated stdio policy. Missing resolver/dialect/account binding or incomplete authorization refuses before native HTTP; invalid owner/tenant or protocol refuses before secret lookup. Login/refresh failure retains its typed SDK cause, settles proven unsent reservation and does not retry model/tool effects. **Alternatives rejected:** calling a global token loader borrows another owner's identity; startup-only credentials expire mid-run; random per-round cache IDs lose affinity; silently selecting a keyed alternative changes the requested subscription.

Verify native-route/account binding unit tests, the 30-case Python request oracle,
cache key refusal tests and test_native_oauth_route_installed against a verified
archive. The four local cases cover rotated headers, body/header key equality,
opaque reasoning continuation, selected owner/tenant/reference/destination, typed login
failure and scope release. They do not contact a real subscription. Production
other subscription dialects and default entry remain pending. The application
factory below assembles the existing owned Codex source.

## Native production Codex credential composition

**Problem:** an arbitrary callback does not bind production refresh to live
routing grants or source account identity. **Decision:**
compile_native_application_route(repository, principal, selection) supplies
NativeCodexCredentialResolver. Each attempt recompiles the same selected offering
through the existing admission boundary before/after refresh. A canonical SHA-256
of all public route semantics except aggregate revision binds identity, endpoint,
context, inference policy and output cap; secret-reference replacement also refuses.
Unrelated revision changes are allowed. Only the first-party Codex destination is
admitted; transport proxy egress remains separate. No alternate route is selected.

The resolver reads existing stored source metadata: explicit owner/tenant/account or the existing shared reference is required. Legacy personal source ownership uses codex_shared_login.local_owner, never a core single-user default. A missing or unreadable source retains credential-unavailable attribution, not inferred expiry. The first authorized source identity is frozen for this invocation; account replacement between rounds refuses. Shared-reference identity is hashed, not cached as raw data. Existing resolve_oauth_request handles refresh/singleflight and emits identity headers with the exact bound affinity key. Its optional expected account check refuses account replacement during token/header materialization. Grant and source are rechecked before dispatch; typed login/source failures remain distinct. **Alternatives rejected:** another refresh implementation risks single-use token races; accepting changed sources redirects existing history; comparing revision alone rejects unrelated configuration updates; a global unowned token fallback crosses ownership. Other stores/providers can supply the existing explicit port; this adapter does not create a new vault or copy tokens into routing state.

Verify test_native_credentials, outbound/refresh retry suites, application route
factory tests and test_native_oauth_production_installed with the verified OAuth
archive. Actual native processes refuse source/owner/revocation failures with zero
proxy CONNECT requests and zero spent/pending tokens. Existing successful two-round
fixtures test the explicit port. No live subscription request was made; production
success/deployment/default entry and other subscription adapters remain unverified.

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
registered tools to Rust. Problem: explicit prepared entry could attach gateway-only documents and reject declared writes. Decision: freeze task execution authority plus gateways, compile missing contracts through the canonical adapter, and preserve existing documents.
Problem: startup visibility froze intent before inbox/context preparation. Decision: prepared entry uses wire_tool_preparation and RoundPreparation after canonical injection/admission; messages and visible functions share the bounded pre-reservation frame. Rust validates the subset and binds one read guard through counting, aliases and serialization. Generation/catalog rights remain frozen. Alternatives: early projection misses injected intent; a second context pass duplicates inbox effects; registry fallback or skipped approval weakens authority.
Model visibility grants no rights. Owned executors join preparation/Stop; explicit
tool_sibling_control settles declarations before effects and qualified reuse.
The binding rejects competing callback/approval owners and disposes proofs after
the native client joins its callbacks. Parser, pipeline and handler registration
remain existing application owners; do not construct a direct-handler adapter.

Exact current approval receipts are reused without a duplicate human prompt;
current admission/hooks still run. Tool rows appear on actual callback entry;
one bounded response queue releases on consumption/close. Rich results need admission. Verify
with `tests/tasks_pkg/test_native_gateway.py` and the `gateway_write` case in
`tests/storage/test_native_task_terminal.py` using a verified native archive and
disposable authority. Exact ROOT wire comparison still gates default selection.

## Native non-read host tools

`NativeProcessClient.run(..., approval_policy=...)` requires explicit `approval_control` receipt/payload bounds for non-read tools. The async `ApprovalPolicy.decide(ApprovalRequest)` returns a canonical receipt authority or `denied`; it consumes the existing human/auto/delegated policy, never guesses from a tool name. Detached argument/caller accessors cannot rewrite the proof. The request stop event and remaining deadline govern owned waiters; all work must quiesce before the returned future finishes. ToolRequest exposes a consumed `call_id`, caller and stop event. The admitted callback must enter the existing tool gateway and preserve its permission, ownership, hooks and result policies. `run_native_route` and `run_native_task` forward this policy explicitly. A failed host tool sends `tool_result.failed=true` rather than stranding its native waiter; the host preserves the original error and drains native failure before releasing admission. The host opts into `tool_result_join`; use a newly built matching archive. Source-only approval tests and `tests/support/native_approval_probe.py` verify exact consumption and real scratch-only effects. Process loss still needs application effect reconciliation; approval does not authorize a replay.

## Native text progress

`NativeProcessClient.run(..., on_text=...)` opts into native content/reasoning frames over the existing stdio owner. `TextDelta` carries the principal, task, monotonic sequence, channel and accepted text. The async consumer returns None; only then does the host ACK. Callback failure rejects the frame with its original cause and drains the native error terminal without fabricating user cancellation. Repeated cancellation joins started callbacks and ACKs. Configuration with `text_progress=true` but no consumer fails before image acquisition. Bytes derive from existing output/round budgets; signatures are not public text observations. `run_native_route` forwards this port; `run_native_task(..., project_text=...)` uses the explicit application projector and supplied executor. Existing event coalescing, ownership, recovery and terminal persistence keep their authority. Observers must use their returned future for all work, with no detached writes. Native application tests require a newly built matching archive; an older archive without the optional schema rejects this opt-in rather than silently dropping it.

## Native execution from a selected v2 route

`compile_native_route(repository, boundary, selection, policy=...)` compiles the canonical primary candidate and freezes bounded private endpoint/header data. It does not search another protocol, refresh OAuth, import a model, mint a Slot, probe connectivity or persist credentials. Owner mismatch/unauthorized selection fails before secret lookup; unsupported primary paths fail before decryption. Use `run_native_route` with the owned NativeProcessClient, the single-use route, a configuration without endpoint/headers/template, admitted read callbacks and a trusted prepare_template callback. That callback must run existing selected inference/body policy and return generation parameters with the chosen wire model, exactly one positive output field, no messages/tools/private control fields. The declared Deployment output cap is applied before invocation. The result separates the native event from route/model identity metadata. Proxy selection remains explicit caller policy. Caller cancellation retains the host's joined cleanup; consumed routes are never replayed. Nonconsumed carriers expose close. `python -m tests.support.native_route_probe --archive ARCHIVE` uses the repository test environment and checks real v2 -> sealed native -> loopback HTTP with synthetic credentials only. This is authority/materialization coverage, not billing/durable-route settlement or end-to-end performance admission.

## Optional native stdio local token admission

Configure token_admission only when trusted composition has selected the local tiktoken tier. Its canonical definition supplies encoding, output field, input/text/tool-JSON/depth bounds, actor/attempt/identity bounds and CPU worker stack bytes. Optional total_tokens is finite (zero denies); omission is unbounded. The configured model string is bound exactly. The native driver derives tools from its compiled catalog, counts frozen messages/tools and writes the lease output cap into the actual HTTP request. It never silently chooses a vocabulary, changes the production backend or substitutes zero on counter failure. This port inherits local text/framing/image estimates; it does not claim exact server billing or replace the existing multi-tier counter policy. Missing usage remains unknown. The scoped CLI counter worker keeps cancel observation live and joins before returning; caller-supplied bounds must include encoder/stack demand. Verify current Python token fixtures, both encodings, actual stdio HTTP cap and second-round refusal, cold cancel with zero HTTP, and the real archive host probe. `scripts/check_rust_token_ingress.py` needs pinned Python tiktoken 0.14.0 and already fetched Cargo assets; it verifies canonical vocabulary hashes and makes no vocabulary network request. Version-bound license overlays supplement notices omitted from upstream crate packages in standalone archives. Production resolver/model routing, shared persistent native process ownership, model-specific non-tiktoken counters and durable account adapters remain pending.

## Native counted stdio shared account

Explicit model_control={ack_ms} plus model_counter selects the host account port;
local token_admission cannot be mixed with it. Rust counts
admitted history once and exchanges reserve/dispatch/settle over the existing
single reader. No local Account mirror is created. A frozen output cap reaches
the actual wire, dispatch ACK precedes HTTP, and acknowledged settlement precedes
later rounds or terminal persistence. Reserve/dispatch inherit the execution
and ACK deadline; explicit `ack_ms: null` adds no callback deadline, while settlement
retains its own owner. Missing/zero ACK policy refuses. SDK callbacks join on cancel/timeout and never send expired replies. Process loss joins native execution
before pending lease cleanup; lost settlement ACK reuses frozen paid usage.

NativeModelAccountPolicy uses the caller's existing ExecutionBudget and executor,
checks explicit tree/actor owner identity and preserves unknown sent usage. It
creates no counter, account, slot, worker or backend. A live child keeps tree
completeness false until its existing membership owner closes it. Pricing and
transcript publication remain with their current owners. Do not reserve this same
account through RoundPolicy. Explicit run_native_task/run_native_route delegate
model_policy. Default production entry and whole-system performance remain pending.

**Problem:** restarting a completed round with a fresh shared account could return spent credit. **Decision:** explicit checkpoint_shared_account plus both policies captures the quiescent invocation-root ExecutionBudget before journal completion; load restores it before the native ACK and any model admission. The generated checkpoint schema binds owner/root, original token/tool/deadline limits and exact encoding/output-field/wire-model policy. Tokens, total/control tool calls, attempt ordinals and unknown-usage state survive; restoration is one-time into a pristine account. Pending attempts, active child membership, changed policy and mixed local accounts refuse. Native never mirrors the host ledger. Existing journal ownership and joined former-writer quiescence remain required; this is not crash takeover. **Alternatives rejected:** resetting credits bypasses limits; copying a live parent while siblings run loses shared-account authority; another ledger splits settlement. Verify test_shared_account_checkpoint and the explicit real-archive/TofuDB test_native_shared_account_recovery pause/close/reopen tests, including exhausted credit and fractional original deadline. Active-tree recovery and default production promotion remain pending.

Verify native_model_control unit/installed tests, model/account/stdio gates and the
native_model_account real application case with a verified private native archive.

## Native per-attempt credential headers

**Problem:** a fixed configured token cannot refresh between native model rounds. **Decision:** accepted model dispatch may return a full headers object over the existing one-slot model_decision. NativeModelAccountPolicy's optional synchronous prepare_headers(request) runs on the supplied executor before dispatch intent; callers bind it to their existing owner/destination-authorized credential resolver. ModelDispatch freezes bytes and redacts repr. SDK/native validate names, values, case duplicates and generated forbidden transport fields. Rust moves the fresh headers into the actual request, replacing configured headers without cloning a stale set. Header metadata never enters usage, model-account snapshots or journal state. None retains configured headers; an explicit empty object clears them.

Refresh failure/cancellation before intent is proven unsent by the same host
policy. It freezes dispatch_entered before the authority marker; failure/lost ACK
at or after that marker remains unknown. Callback timeout signals cancellation,
joins physical work and preserves the original timeout if the worker cancels.
**Alternatives rejected:** startup-only resolution cannot rotate long-run tokens;
merging headers retains stale credentials; another reader or vault splits authority;
refunding failed dispatch delivery loses unknown paid usage. OAuth routes require the explicit owner/destination binding below; production
default cutover remains pending.

Verify native_model_control unit tests, the seven-case actual stdio header matrix,
model_admission direct-header failures and test_native_dispatch_credentials_installed
with an explicitly verified archive. The installed test rotates credentials over
two actual model/tool rounds and preserves typed refresh failure without a second
HTTP call. No live subscription, network credential refresh or performance gain
is claimed by these fixture tests.

## Native shared model admission

**Problem:** counted HTTP was tied to one process-local Account, preventing an existing shared-tree authority from authorizing the same frozen request. **Decision:** with_model_admission selects an explicit Admission/Lease port instead of constructing a native mirror account. The native path keeps its inline lease. InputCounter runs once on admitted history; one returned output cap is frozen, validated against the counted proposal and written to the actual wire. Joined sender intent precedes HTTP. Settlement preserves reported usage, and unknown intent delivery can never be voided as unsent. Refusal, invalid cap, cancellation and serialization failure cannot dispatch; known unsent reservations are released. **Alternatives:** a second account duplicates reservations; counting before prompt preparation authorizes stale input; rereading a mutable cap opens a validation gap.

Ports are trusted, owner/task-bound and must join/fence unknown delivery.
Callbacks receive only read-only wake/deadline observations, not the parent
session or its resource/terminal authority. They
create no route, tokenizer, pricing default or retry. Actual TCP tests reuse one
shared Account, verify both output fields, paid settlement, observer failure and
follow-up refusal. Fault tests cover lost/panicked dispatch intent and zero HTTP.
Counted stdio and application shared-budget adapters are explicit as described
above; pricing recovery and production default promotion remain pending.

## Native counted HTTP composition

Rust embeddings may wrap HttpProvider with `with_account`, supplying an explicit Account, owner/task binding, InputCounter and OutputField. InputCounter counts model input from the immutable borrowed messages and template/tool catalog; no default count or tokenizer is invented. The declared output field must carry a positive integer and the other supported output field must be absent. The lease cap is written into the actual HTTP body without modifying the frozen template. The native account shares the tree cap, honors trusted actor-local slices, restores model totals/ordinals once and preserves unknown usage across requests. A native observer is an acknowledged lifecycle port: failure/panic cannot certify settlement or start a follow-up request. It does not replace billing, route policy, child-membership durability or terminal persistence. Do not also reserve the same token account through a host RoundPolicy. Separate wallet or routing authorities remain explicit composition concerns. Stdio explicitly selects the native counted provider through token_admission; registered model counters and shared application account/pricing adapters remain required before production cutover. Verify `scripts/check_rust_model_account.py`, native model_account tests and real TCP output-cap/follow-up refusal. The current source gate executes unchanged canonical account/lease bodies with their canonical validator/usage functions, without importing app/pricing side effects or contacting a model.

## Native usage evidence for round policies

Returned RoundSettlement evidence includes raw `usage` and an additive `usage_observation` object: `tokens` is the observed lower bound, `complete` records whether both provider counters and required cache evidence are known, and `error` identifies unsupported numeric representation. An empty usage object reports zero lower bound with complete=False; this is not evidence of free work. Overflow and unsupported non-ASCII numeric text report tokens=None, complete=False and an explicit error. Other malformed fields follow the canonical cleaning rules. Native counters support u64; pricing/money remains with the cost authority. Raw usage is preserved, including incomplete and mixed vendor fields. A policy must not refund unknown usage or substitute history_messages for token counts. Verify `scripts/check_rust_usage_oracle.py`, native usage tests and the archive probe's OpenAI-inclusive/Anthropic-residual fixture rounds before changing this boundary. This evidence port does not yet supply sealed input-token admission.

## Native terminal persistence composition

Construct checkpoint_journal::Policy with Policy::new(lease, recovery, suspend). Its terminal_persistence(exact_store_arc, existing_persistence_arc) supplies the Task persistence port; the Store must be the exact instance used for acquisition. The owned wrapper survives terminal retry after the execution stack is gone. Settlement/frozen-candidate ownership stays in Task. A failed seal prevents persistence/publication; successful seal is idempotently reread on retry. A committed Suspend may keep its completed frontier only for Aborted; ordinary Stop or failed cleanup seals it. Caller releases a paused lease after joined terminal handling. No automatic restart redispatch or financial/time recovery is granted. Fence and task writes are sequential, so interrupted completion still needs canonical orphan settlement; this API is not a crash-recovery store.
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

Borrowed and owned calls share one Arc of token/generation/loading/ready/poison flags. An immutable callback scope is borrowed; no scope strings or history are copied for metadata calls. Durable CAS remains the concurrency authority. One-shot load, stale generation, unknown-delivery poisoning, committed Suspend and frozen terminal retries retain existing semantics. Drop never releases. **Alternatives rejected:** leaked/self-referential journals obscure lifecycle; reattaching tokens can resurrect stale owners; a Python journal duplicates the transition authority. No worker, timer, authority or history cache is added. Verify checkpoint_journal/agent tests, native-store reopen and fault neighbors, MSRV and examples/checkpoint_owned_cost.rs. The benchmark compares metadata-only acquire/load/release over a memory fixture; it excludes storage I/O and cannot certify application latency. FFI/semantic application binding and the production Rust default remain pending.

Local release measurement (2026-10-10, six alternating 10,000-cycle samples)
was 8.417 us median borrowed vs 8.771 us owned for acquire/load/release.
Inline OwnedLease/OwnedPolicy sizes were 120/152 bytes; shared heap metadata and
caller-owned Store/Recovery are additional. This is callback overhead evidence,
not a speedup or a complete resident-memory/production-latency measurement.
## Native TofuDB checkpoint embedding

Standard TofuDB builds include native-runtime-checkpoints. Activation still requires explicit native embedding and owned checkpoint admission. NativeCheckpointStore receives the existing Arc<Mutex<AuthorityDatabase>>, tenant/owner and header/snapshot byte budgets; it opens no paths. Journal uses this Store unchanged. Host code still owns joined release, terminal seal and financial/time recovery. This does not select an application storage backend. Header and snapshot are separate versioned records committed atomically; Keep updates header only. Problem: fresh executors cannot adopt a paused journal. Decision: handoff rechecks accepted ancestry in the same owned transaction, retires source, creates target and captures one composite outbox record; only the terminal private source snapshot is reclaimed. Alternative: copy then retire can publish two executable owners. Blob snapshots use the existing owner-bound codec and GC. Generic record access is denied. Runtime provider-json-order remains default-on; the DB dependency opts out. An insertion-order Map makes Store construction fail before effects: combined provider-order embedding is not supported by this codec. Configured outbox now captures native_checkpoint_outbox_v1 mutations atomically. NativeCheckpointStore.replay takes an explicit source UUID, cipher and sealed record for a passive target without an outbox. It verifies scope/digest/schema, pins expected version and commits state plus one latest replay witness. Current redelivery is idempotent; direct older/gapped records refuse. NativeCheckpointSink receives a target, existing EngineLogicalOutboxSink, cipher and source UUID. Its archive proof grants old-version retry only for the same archive-bound confirmed target prefix. NativeCheckpointStore.publish_pending uses caller-supplied batch budgets, fetches without retaining the writer, publishes target/archive and ACKs source in order. Target/archive failures expose restart evidence. Reopen before retrying uncertain publication; no receipt is returned before both destinations are durable. Cross-domain and application routing remain unsupported.
## Native journal store boundary

checkpoint_journal::Journal acquires one scoped versioned lease. Store adapters must atomically compare the header version and write header plus Replace(snapshot) with durable acknowledgement; Keep must preserve snapshot without decoding it. Explicit handoff accepts released completed state or paid pending state with kernel proof of writer death, retains fee origins and original deadline/limits, and rebinds task/account identities plus the target wallet reservation. Ambiguous delivery never retries or falls back; source retirement leaves one small header fence and moves the executable snapshot without adding a resident worker or another store. Snapshot reads are pinned to that record version. Lease load is one-shot; begin requires successful load and persists inflight before any effects. Policy binds the existing Agent port and an explicit Recovery for pricing/elapsed time; [pending tools](contracts/TOOL_EFFECT_RECOVERY.md#native-paid-tool-frontier) additionally require exact domain observations. Release is caller-owned after joined Suspend; seal is caller-owned with terminal composition. Missing acknowledgements poison the handle; inflight, terminal and still-owned frontiers refuse acquisition. No timeout or Drop steals ownership. Adapters must bound input bytes before materialization; identity and snapshot budgets are explicit composition inputs. Tests use an atomic in-memory store; this API alone does not enable application crash recovery or switch storage. A wallet fee checkpoint carries one bounded `wallet_settlement` projection of its current original-task debt, generated by the same billing `project` used by terminal `freeze`. Its schema is embedded in the checkpoint contract and exported by the existing generator. Restore checks owner, wallet, original reservation, round count and exact frozen quotes without repricing or freezing the running invoice; later paid work still belongs in the eventual terminal capsule. Completed handoff drops the old candidate when rebinding executor identity. Pending wallet handoff checks original frozen credits and account coverage, then freezes the source invoice in its existing terminal journal header. Source retirement, invoice, target creation and composite outbox commit together; the new pending target carries a zero-debt candidate for its own actual reservation. Repeated crashes preserve each executor's debt, including zero-cost intermediates. The source header's invoice is durable financial evidence; only its private executable snapshot is reclaimable. No new store, ledger, pool or worker exists. The existing wallet sweeper consumes it through billing.recover and the bounded read-only `invoice` C ABI. The query joins runtime close, waits for the durable WAL witness, and refuses passive replay sources. Existing journals without a retired invoice cannot justify refunds. Task-result capsules retain their normal terminal path; errors never select another storage backend or pricing source. Problem: task-result-only invoices were lost when a paid pending process died. Decision: freeze its canonical capsule at the existing atomic retirement owner. Alternatives: a separate metadata write splits debt from retirement under crashes and checkpoint-only passive replay; freezing a live invoice loses later fees; recalculating prices changes debt; copying the old debt to a new executor charges the wrong reservation. Verify actual ROOT fresh wallet/repeated SIGKILL, janitor lost ACK, native live-writer/passive replay and write/sync-fault tests. One fixed-shape capsule remains per retired wallet executor: actual ROOT fixtures retained 318 bytes for the original invoice and 315 for a zero-debt intermediate. Twenty warmed native invoice queries had medians of 0.22–0.25 ms; these are representation/read measurements, not whole-task or physical-WAL claims. Later work replaces the active candidate, never appends per-round invoices. Synthetic capture medians over 20 warmed samples added 0.42–0.95 ms at 1/32/256 rounds, excluding IO and overall latency.

## Native embedded checkpoint binding

**Problem:** application callbacks needed the running authority, and a shared
WAL commit could return before fsync. **Decision:** with native-runtime-checkpoints,
Arc<EmbeddedRuntime>.checkpoint_lease(scope, header_bytes, snapshot_bytes,
identity_bytes, token) returns an OwnedLease on that runtime's exact database.
Owner must match its authenticated scope; tenant follows the existing scope
encoding. It opens no authority and creates no worker/driver. Explicit byte
bounds, Recovery, joined pause release and terminal persistence remain caller-owned.
Lease callbacks retain the runtime; drop them before opening a replacement authority.

NativeCheckpointStore captures a generation-bound WAL witness under authority and waits outside its writer before returning reads/CAS, passive replay/current retry, pending publication or source ACK. Plain Strict inline-sync is supported; deferred WAL without a coordinator refuses construction before any lease write. Latched sync errors refuse reads and retries, preserving the live journal fence. Runtime close marks closed, joins entered checkpoint Store calls through a shared read/write lifecycle guard, then retires existing drivers. Closed or disarmed callbacks refuse before I/O; Drop still never releases the durable lease. **Alternatives rejected:** cloning/opening a database splits authority; waiting under its writer blocks commit pipelining; treating commit/visible metadata as fsync can publish an unconfirmed terminal frontier. Verify real runtime owner/ close/reopen, blocked flush/close joining, native-store WAL fault/replay/ACK tests and existing checkpoint neighbors. Rust binding is explicit; C ABI/semantic routing, SQL adapters, crash takeover and production-default execution remain pending.
## Native checkpoint C ABI and standalone SDK

**Problem:** installed hosts lacked callable native journal ports, and output
buffer retries could repeat one-shot load. **Decision:**
tofudb_embed_checkpoint_supported negotiates the compiled native-runtime-checkpoints
feature (standard builds include it; --no-default-features omits it); tofudb_embed_checkpoint_execute carries create/load/begin/pending/complete/seal/
release/forget under native_checkpoint_control_v1. It reuses the existing embedded
request envelope, owned worker queue, panic containment and watchdog. Ordinary
storage execute cannot enter checkpoint control. Problem: checkpoint control was coupled to one storage backend. Decision: `tofu-runtime::checkpoint_control` owns bounded admission and volatile capabilities through an explicit authenticated runtime port and authority document ceiling; the TofuDB stack adapter retains its existing authority and joined lifecycle. Alternatives: duplicating control in Python would split journal semantics; wrapping all control in a SQL transaction could publish capabilities before a failed commit. Each authority must acknowledge durable writes before returning leases. The registry adds one document-ceiling word and no worker or store. The private `checkpoint-storage-abi` feature now binds that owner to selected SQLite/PostgreSQL atomic bytes through `lib/storage_authority_sql/native_checkpoint.py`, with explicit tenant/owner, pinned sealed code, per-call deadlines and joined callbacks. `lib/native_binary/__init__.py` supplies the common dependency-free code verifier; agent-facing imports retain the same exception and function identities. Problem: a Python journal or one outer SQL transaction would split transitions or acknowledge before COMMIT. Decision: Rust retains leases; each private SQL CAS commits before returning, exact identity/version checks serialize competing writers, and Keep does not read snapshots. Alternatives: public record access bypasses authority; SQL retries after unknown delivery can duplicate effects. Header/snapshot rows use reserved SQL-specific envelopes, with no implicit TofuDB import. The adapter remains experimental: accepted Turn handoff now locks the actual attempts and Turn, calls the shared Rust lineage/retirement validator inside one SQL transaction, retires the source and creates the target before publishing its lease. ABI version 3 retains the pure single-handoff validator and adds a distinct synchronous Goal transaction entry; neither enters a runtime handle. Problem: a preflight-only check can race worker entry, source reacquisition or supersession. Decision: lock attempt rows before the Turn, then both private checkpoint keys; recheck exact versions, accepted ancestry and pending writer death before any write. Alternatives: Python policy duplicates authority; publishing a lease before COMMIT admits unknown effects. Completed and paid-pending transfer, original invoice retention, rollback and lost acknowledgements are covered by `system_native_sql_checkpoint_lineage_20261016.json`. Whole-Goal claim and consumption now run shared Rust authority inside one joined SQL transaction. A transaction-local callback uses only its entered Session; SQLite never submits a writer job from its writer thread. Rust validates the complete actor set, protected preparations, original debt and fresh parent reservation, then SQL commits all private headers/snapshots, ledger changes and account/event cuts before leases publish. Every inner refusal rolls back; lost commit acknowledgement publishes no capability. Logical outbox/replay uses the existing publisher with ordered transaction proofs and passive target fences; see `system_native_sql_checkpoint_outbox_20261016.json`. Explicit application availability is now owned by the lazy SQL service, described below. `system_native_goal_sql_transaction_20261016.json` records selected-backend checks and resource observations; it grants no performance/default admission. See `system_native_sql_checkpoint_store_20261016.json`. Problem: duplicating process-death evidence for that adapter could weaken takeover fences. Decision: `tofu_runtime::checkpoint_writer` owns the existing Linux boot/PID-namespace/start-tick capture and pidfd proof; TofuDB borrows it directly. Alternatives: per-backend copies drift; wall-clock expiry or missing procfs rows cannot prove death. Shared writer proof retains live/foreign/PID-reuse/SIGKILL checks and selected native integration evidence, without granting SQL availability. Actual shared-owner checks retain source pins, failure corrections and 32-cycle resource observations: close restored thread/fd counts while the loaded debug library remained resident; this is not performance admission. Problem: a private SQL controller cannot serve the application journal without transport, package and lifetime ownership. Decision: generated `application` policy in `native_checkpoint_storage_abi_v1.json` defines `TOFU_STORAGE_NATIVE_CHECKPOINT`, a bounded JSON launch document with exact fields `format`, `library_path`, `sha256`, `binary_bytes`, `tenant_id`, `owner_user_id`, `document_bytes`, `identity_bytes`; format is `tofu.sql-native-checkpoint/v1`. Build with `scripts/build_native_runtime.py --output DIR --checkpoint-storage` (combine `--flow` for Goal); install the declared image with `--check ARCHIVE --install-checkpoint-library ABSOLUTE_PATH --archive-sha256 PIN --application-version VERSION`. Installation verifies the immutable archive and publishes exclusively; the SQL process accepts the installed image pin without importing the execution graph. One explicit scope owns a lazy sealed ABI-3 controller, joined before selected outbox/backend close. The application supplies the same document/identity limits to `NativeGoalRecoveryConfiguration` and its actor checkpoint factory; a larger executor frame ceiling does not override storage admission. Disabled/read-only configurations load no native code. Capability negotiation returns a process UUID; shared client admission binds each control to it, carries explicit owner and absolute deadline, and makes one transport attempt without receipts, retries or stale-binding refresh. All required symbols must exist before availability succeeds. Alternatives rejected: importing archive/execution adapters into Sidecar, allocating one runtime per request, and reusing generic command retries after uncertain commit. `system_native_sql_checkpoint_application_20261016.json` records archive, public recovery, fault and resource checks; this explicit capability grants no default/performance admission. Capability IDs are allocated
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

NativeProcessClient.run, run_native_route and run_native_task accept an explicit checkpoint_policy together with round_policy. Its immutable binding identifies the owned model/tool/account policy. async load(CheckpointRequest) returns None or an eligible native checkpoint after acquiring its recovery lease and restoring host budgets; begin returns None only after durable inflight CAS; complete returns continue or suspend only after committing the completed frontier. Requests carry identity, action/round, epoch/binding, cancellation and the current monotonic deadline; complete also carries native_agent_checkpoint_v1 state. Observers must cooperate and join their work. The caller owns lease cleanup after the native run returns. Callbacks never automatically repair inflight/terminal state. Oversize/invalid/failed callbacks stop before later effects and keep their original cause after terminal acknowledgement. Native token_admission state restores settled token usage, last attempt ordinal and usage completeness through the pristine one-time account boundary. Exact encoding/output-field/total-budget policy must match; missing account state or removing admission is refused. Unknown usage stays unknown and exhausted budgets cannot dispatch another request. Host pricing and original deadline authority still require trusted recovery; native token restoration does not reconstruct either. Stdio preserves a supplied absolute deadline as specified below. No storage backend or automatic application recovery is selected by this port.
## Native absolute execution deadline

Optional deadline_unix_ms is the application's original UTC expiry, including zero for already expired work. run_native_task reads the earliest validated task or shared-tree deadline without creating another account and can only shorten it. Required deadline_ms is null for no relative task expiry or a positive millisecond budget. An absolute deadline still applies when the relative budget is null. Stdio clamps any relative budget by wall-clock remaining time observed once; start_observed subtracts observation-to-entry delay, then uses the existing monotonic session. Depleted time creates a cancelled session for ordinary settlement and terminal ACK. Checkpoint state retains the exact UTC value. Added, changed or removed deadline profiles refuse before restored model dispatch, and before account restoration. The host must preserve the original UTC authority across restart; wall-clock correction across process restarts remains an application clock assumption. Generic embedded checkpoint Recovery still owns pricing/time authorization. Verify real agent_stdio expiry/shortened-budget/recovery cases, session observed budget tests and the application shared-tree deadline tests. Default execution promotion and whole-system performance remain pending.

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

The SDK enables checkpoint_terminal_fence and requires checkpoint_suspended in terminal requests. Rust sets it only after an accepted complete Suspend and an Aborted terminal candidate; cleanup Error reports false. The marker and event/ receipt freeze together across retries. Seal runs before TerminalPolicy.persist; a retry skips task persistence, and accepted seal is reused for later task commit retries. Attempts/grace remain those of terminal_persistence. Seal receives no expired execution deadline: terminal durability must still settle interrupted runs. Cancellation signals and joins the callback before task persistence/ACK. This ordered pair is not an atomic cross-domain transaction; a crash after the checkpoint fence requires existing orphan terminal reconciliation. Missing seal fails before binary acquisition. Checkpoint-only transient composition remains unchanged. Verify native-process ordering/fault/cancellation tests, real agent_stdio checkpoint retry cases and the installed native_context application case; this port does not select a storage backend or promote the default runner.
## Project input persistence seam

Custom ConversationStore implementations expose inject_project_messages(task, messages, round_num), confirm_project_messages(task, round_num) and confirm_project_context_consumption(task), in addition to the existing narrow confirm_project_context_delivery(delivery) command. The application adapter owns project identity/coordinator checks and acknowledgement semantics; core supplies the explicit owned task and only calls consumption after verified provider return. Native settlement invokes the same consumption helper after accounting and before deferred inbox confirmation, suppressing it on failure/cancellation. The default adapter skips these operations for _transientRuntime=True without importing host project/storage modules. Custom hosts implement their own policy; missing methods never trigger an implicit application-store fallback.
## Canonical gateway progress composition

For run_native_task with NativeGateway, configure no_progress_rounds and/or nonretryable_failure_rounds explicitly, with round_policy and the existing feedback_bytes bound. NativeGateway owns the observer; a separate progress_policy is refused for this composition. Non-gateway native routes keep the explicit ProgressPolicy port. Gateway results publish only current-batch hashes after settlement; the native call count must match before resultEvidenceComplete is true. Locally refused/denied or missing results therefore remain incomplete. Verification stays unset unless a separate future authoritative policy supplies it. Failure signatures require every observed call to be non-retryable, using the shared root normalization at each runner's existing argument boundary. Evidence is consumed after observation and released on close; budget overflow fails open for the breaker and never reexecutes or weakens tool settlement.
## Native post-tool progress observations

run_native_task, run_native_route and NativeProcessClient.run accept an explicit progress_policy together with round_policy. Its frozen ProgressLimits enables one or both breakers, and async observe(ProgressRequest) returns exactly worldVersion, evidenceIds, resultEvidenceComplete, verification and nonretryableFailureSignatures. Observations belong to trusted application result policy; incomplete result evidence must remain false, and terminal failure signatures require every call to have failed non-retryably. The request carries owner/task/round/call count, a cancellation event and the inherited monotonic deadline. Observers must use their declared executor, cooperate with this signal/deadline, and join work before return; Stop does not detach callbacks. No credentials or message history cross this port. Invalid observations stop before another HTTP round and preserve the original callback failure after terminal acknowledgement. The application default root still requires separate migration.
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

Pass NativeContextPolicy as run_native_task's round_policy with the same principal, owned task, executor and an explicit accounting delegate (reserve/settle only). The issued route binds metadata, clamped output and configured tool catalog before native acquisition. No credential headers cross this policy port. Preparation uses enforce_dispatch_prompt_limit, including required checklist restoration and existing pressure/compaction behavior. Missing context facts or impossible output reservation refuse; no window is invented. Semantic L2 requires an initialized owned pin matching the compiled primary; otherwise it refuses before summary I/O. Verify native-context isolation/cancellation tests, route-budget/summary neighbors, generated tool projection and installed context success/refusal application tests. This policy is a migration adapter; root context injections and default selection are not changed. It neither authorizes a new summary route nor invents refunds.

## Native per-round history preparation

An optional async `prepare(RoundRequest)` on the supplied round policy enables round_preparation. Its request.messages contains canonical history; return a list of canonical message objects. The host validates the reply frame before calling reserve; Rust validates prepared retained bytes before actual HTTP. The same round_request/round_decision slot carries history, with no extra reader or loop. round_preparation_bytes defaults to the host's admitted frame budget and may be explicitly reduced. Omitted preparation preserves the existing count-only protocol. A preparation exception publishes an explicit native failure before being raised as NativeRoundError with its original cause after terminal/exit confirmation. Invalid native retained history reports NotDispatched; preparation grants no tool, credential, route, budget or compaction authority. Verify host cancellation/fault unit tests, actual large-frame stdio/Stop tests and installed Responses preparation application tests. The existing application policy must supply compaction and injection semantics before the production entry can adopt this port.

## Native per-round host policy

Pass `round_policy=RoundPolicy` to NativeProcessClient.run to enable the optional round-control protocol. `reserve(RoundRequest)` asynchronously returns an exact bool; only True authorizes that round. Requests bind the supplied principal, task, ordinal and history-message count. They carry no token/price estimate. `settle(RoundSettlement)` must return None after recording raw Returned, Failed, Stopped, NotDispatched or Abandoned evidence. Native settlement waits for the host ACK before later effects. Missing usage remains unknown. These callbacks are policy ports, not durable billing or model-routing implementation. Reservation/accounting errors raise NativeRoundError with their original cause; no callback retry or automatic model fallback occurs. Cancellation joins policy callbacks before releasing invocation admission. An approved reservation whose native settlement was not observed receives one Abandoned callback after exit. A settlement callback already attempted is never attempted again. Callbacks must quiesce; ACK transport alone is bounded by shutdown_seconds. Without the optional policy, the previously authorized transient configuration retains its original behavior. Configuring round_control without a policy fails before launch.

## Native host adapter for application composition

Explicit installation and shared installed-executable admission are defined in
[NATIVE_IMAGE_ADMISSION.md](NATIVE_IMAGE_ADMISSION.md). They preserve separate
distribution/executable pins and do not change default selection.

The internal `lib.agent_core.native_process` port invokes an installed native archive from an async Linux host. Construct NativeProcessClient with the trusted manifest binary checksum/protocol version, an explicit PrincipalContext and a Budget supplied by launch policy. Pass `run()` a previously authorized bounded native configuration, matching admitted read-tool callbacks and an absolute working directory. ToolRequest carries the same principal and exact task/request identity. The returned dictionary is the native terminal event; no second model loop or task terminal is fabricated by the host. Close the client before disposing its event loop. Callbacks must quiesce on cancellation; callback errors retain cause under NativeToolError and never become tool evidence or automatic retries.

The adapter is fail-fast at its declared admission bound. It validates and seals a cached executable image, so source-path replacement does not change launched bytes. Configuration and tool content never enter subprocess arguments or stderr logs. Close drains/cancels and joins native execution before releasing admission and the image. Older Linux Python/libc builds use the kernel memfd/seal UAPI when stdlib bindings are absent; unsupported kernel/ABI execution fails closed. `python -m tests.support.native_process_probe --archive ARCHIVE` verifies real host/native/model/tool integration against synthetic loopback provider fixtures. This port is not yet selected by AgentRuntime/serve; v2 route compilation, durable account policy, write approval, durable checkpoints and progress remain owned integration work. No production backend is silently switched.

## Pinned native archive client

`lib/agent_core/native_binary.py` owns regular-file ELF admission, exact digest verification and sealing. `native_process` re-exports the same error types and helper names. Problem: pure installed-library admission imported the full task client and its dataclass construction. Decision: share the existing sealing implementation below that client; installed HTML bindings import this small owner directly and archive inspection only on the archive path. Alternatives: duplicating seal logic risks divergent fault behavior; eager task-client import adds unrelated first-use work. FIFO refusal, path replacement, byte bounds and failure descriptor cleanup retain their original tests.

**Problem:** every host manually verified/extracted an archive and then reopened its binary, adding install-tree lifetime and duplicate inspection work. **Decision:** NativeProcessClient.from_archive(archive=absolute_path, sha256=trusted compressed_archive_digest, application_version=expected_version, principal=..., budget=...) creates a lazy client over the same joined acquisition/image owner. It snapshots bounded compressed bytes privately, checks the supplied pin before parsing, streams member digests and the executable into a sealed image, validates member inventory/version/protocol/target and runs the existing build/session/tool self checks against those exact bytes. Only bounded metadata survives inspection; no member tree is extracted. Acquisition happens after normal configuration admission. Concurrent runs share one image; close joins acquisition/runs before reclaiming it. An archive removed/replaced after acquisition cannot change later executed bytes. Checksums do not authenticate a publisher: the caller supplies a trusted release pin and version; no download, update or default selection occurs.

The generated artifact contract owns member/unpacked/metadata bounds. Tar extended headers are bounded before materialization and their aggregate is bounded; sparse, link, duplicate and traversal members refuse. Self-check stdout/stderr share a byte ceiling, timeout kills/joins the owned child, and invalid checks never start agent work. Cancellation of acquisition joins before disposal. The build tool's verify delegates to this same retained inspector; read-only cross-target checks can omit execution, while runtime clients always verify the host and sealed image. **Alternatives rejected:** verify then reopen/extract trusts mutable paths; retaining all license/member bodies inflates memory; permanent installs add avoidable cleanup; a second verifier splits policy. Verify archive release/loader/host tests and the explicit installed loader fixture, including source removal and failed pin/version. The runtime and contract projection are checked in the developer-runtime wheel. This component enables default composition; AgentRuntime/serve selection and full application/performance admission remain pending.

## Standalone native archive

Using Python 3.12 for build tooling,
`python scripts/build_native_runtime.py --output OUTPUT_DIRECTORY` builds the
locked native GNU/Linux host target and creates
`tofu-native-VERSION-TARGET.tar.gz`. The archive contains the executable, exact
lockfile, runtime/stdio contracts, license files and a SHA-256 member manifest;
it contains no Python interpreter, Cargo, compiler or application checkout.
Direct invocation resolves the repository from the script location, so archive checks also work from another working directory without `PYTHONPATH`. This keeps the shared validator authoritative; a separate CLI validator would duplicate its integrity checks.
`--offline` requires cached dependencies; `--target-dir` declares build-cache placement. Output paths are exclusive and archive bytes deterministic.
Problem: repeated activation paid gzip decode CPU. Decision: `--compression-profile local` emits `-local.tar.gz` with stored blocks; distribution remains the transport profile. The loader checks identical members, pins and gates; the producer bounds final archive bytes before publication.
Alternatives: levels 1/6 did not improve decode; larger local files trade space for CPU, so task/memory admission remains separate. Neither profile selects a production agent loop.

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

**Ingress affinity initialization — Problem:** ordinary embedded and worker task creation imported Quart solely to discover that no request existed. **Decision:** `manager/_registry.py` queries request affinity when Quart is already loaded. Web requests retain clipped direct keys and reconnectability; background tasks retain conversation-store inheritance and durable birth metadata. Explicit principal/owner validation still precedes every task. **Alternatives considered:** importing the framework on every first task taxes storage-free composition; skipping affinity for all transient tasks loses real request routing metadata. Verify `tests/tasks_pkg/test_task_ingress_affinity.py` and `benchmarks/task_affinity_measure.py`; first-task clocks exclude common imports, and loaded-web/long-residency checks remain separate boundaries.

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
`scripts/gen_tool_result_query_contract.py` 产出 `lib/tool_results/tool_result_query_generated.py`，
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
from tofu_agent import AgentRuntime, ModelRoutingConfig, NativeAgentConfiguration
access = ModelRoutingConfig.from_mapping(model_routing_envelope)
with AgentRuntime.local(model_routing=access) as runtime:
    execution = runtime.start([{"role": "user", "content": "Inspect this project"}],
        routing={"preferred_provider_id": "gateway-a"},
        config={"thinking": "high", "tools": ["search", "fetch"], "project": "/workspace/project"},
        request_id="run-owned-by-caller", timeout_s=600)
    for event in execution.events(cursor=0): consume(event)
    result = execution.result()
# Optional Rust composition: borrow an admitted host, running loop, callback
# executor and launch capacity; profile declares HTTP/ACK/model-counter limits.
native = NativeAgentConfiguration(host=host, loop=loop, executor=callbacks,
    configuration=profile, working_directory=workdir, resource_policy=launch_policy,
    pending_calls=pending_calls, pending_bytes=pending_bytes)
runtime = AgentRuntime(principal=principal, model_routing=access, native_execution=native)
try:
    result = await runtime.run_async([{"role": "user", "content": "Inspect this project"}])
finally:
    await runtime.close_async()  # join before caller closes host/loop/callbacks
```

`AgentRuntime.local()` constructs an explicit personal principal. A server or
enterprise adapter constructs `AgentRuntime(principal=..., model_routing=...)`
with its authenticated owner instead of relying on a module global. A single
request may pass `model_routing=...` to use a complete one-run envelope; it
does not persist or mutate the runtime default. Rust uses the same issued route, preparation, tools and account; no Python model fallback. Use async calls on its serving loop; sync callers require another lane. Native results wait for worker/client cleanup. `capabilities.execution.backend` reports the selected engine; CLI `--engine rust --native-archive ARCHIVE --native-profile PROFILE.json` owns its launch-sized callback executor and admitted image; default promotion remains pending.

Optional repository extraction has its own trusted configuration, independent
of the model execution engine. Install `tofu-repo-kernel` 0.4.0 or later with its
stream artifact and either a pinned grammar bundle (0.7+) or a reviewed external directory. For example,
with an existing routing `access`, the application's already-issued
`launch_policy`, and an admitted project:

```python
from tofu_agent import AgentRuntime, NativeRepositoryConfiguration
repository = NativeRepositoryConfiguration.from_launch_policy(
    # Omit grammar_directory to select the installed pinned bundle.
    launch_policy=launch_policy,
    timeout=60.0,
)
with AgentRuntime.local(model_routing=access, native_repository=repository) as runtime:
    result = runtime.run(
        [{"role": "user", "content": "Use repo_map to summarize this project"}],
        config={"tools": ["repo_map"], "project": "/workspace/project"},
    )
```

Path-list, request and per-file output ceilings follow the existing launch policy's `frame_maximum`, without another probe or allocation of those ceilings. Source reads use the canonical source/reference admission bound; file growth between stat and read cannot consume the host-sized frame allowance. Explicit overrides remain available through `NativeRepositoryConfiguration(grammar_directory, limits=(path_bytes, request_bytes, file_frame_bytes, source_read_bytes), timeout=...)`. An omitted/None grammar directory selects the installed pinned bundle; an explicit path keeps trusted external selection. Construction imports no native image/parser package. The runtime owns one lazy sealed executable and each task owns a cancellable root/owner scope in the shared ExecutionSession. Close joins active task cleanup before disposing the image; timeout retains it for a retry. `capabilities.repository_extraction` reports the selected backend and reference default, not readiness or performance certification. Selected failures do not fall back. This configuration is not a wire/CLI option. Full catalog coverage and performance admission remain required before default promotion; installed grammar integrity, plain-directory fallback and launch capacity are implemented. Standalone rounds validate MCP wire without application storage, skip durable tool provenance for transient tasks, and resolve requirement identity before importing durable acceptance; an issued inherited requirement still uses its original owner checks.

| Method | Contract |
|---|---|
| `run` / `run_async` | Submit once and wait for a typed terminal `AgentResult`. |
| `stream` / `stream_async` | Submit once and yield native events. |
| `start` | Return an `AgentExecution` immediately. |
| `events(cursor=N)` | Replay from the absolute next sequence, then follow live. |
| `event_page(cursor=N)` | Return one bounded replay page. |
| `snapshot()` | Return the stable `agent.run` projection. |
| `abort()` | Request cooperative cancellation for the owned task. |
| `close()` / `close_async()` | Reject new work and join bounded teardown; timeout retains borrowed owners for retry on both execution backends. |

SDK project retirement — Problem: a native task could finish while its detached file-history observer was still writing under the caller's project; immediate temporary-directory retirement then raced those writes. Python SDK cleanup also treated the terminal notification as worker completion. Decision: transient history completion runs on the existing task/callback worker, after terminal publication, and both SDK adapters keep admission and borrowed resources until the worker and resource disposal finish. `close` raises `AgentTimeoutError` if its join deadline expires; the runtime rejects new work but retains outstanding owners for a later close retry. Alternatives rejected: sleeping before deletion does not establish completion; suppressing snapshots loses history; detaching a second shutdown worker repeats the lifetime defect. Durable application observers retain their existing post-terminal scheduling. No new worker, pool or persistent store is introduced. A real installed two-task model/repository cycle holds the second history operation, observes timeout with one active SDK task, then releases it and verifies both history jobs and all native children joined before teardown. See failure and verification receipts.

Admission uses the shared launch hardware budget when `max_inflight` is omitted or zero; `TOFU_AGENT_MAX_INFLIGHT` or a positive argument supplies an explicit capacity. The CLI follows the same precedence. `TOFU_RESOURCE_PROFILE=throughput` scales foreground workers to effective CPU and available memory; the default `balanced` profile reserves a larger desktop share. Each runtime owns a lazy dedicated agent executor, closed with the runtime. Async event/result observers wait on loop notifications and consume no blocked I/O threads. Independent runtime instances never replace the application's executor. Overload, configuration, timeout, closed-runtime, and execution failures retain distinct public exception types. `abort()` and `close(abort=True)` use the task engine's owned cancellation boundary: signal both cancellation representations, invoke registered resource cleanup, and wake client-tool waiters. An expired caller must not leave native workers consuming shared executor capacity. Cancellation remains cooperative; the terminal verdict, rather than `status=done` alone, reports its outcome.

### Terminal success and diagnostics

`AgentResult.ok` and `tofu-agent run`'s JSON `ok`/exit status share the canonical
turn verdict. `status=done` only means execution settled, not success. A normal
finish (`stop`, `done`, `completed`, `end_turn`) with no error or conflicting
stream evidence succeeds; missing finish, truncation, cancellation, malformed
streams and exhausted recovery do not. A provider tool handoff is not complete
at this autonomous-run boundary. Failures retain content, tool receipts, usage
and verifier-independent trajectory artifacts; callers must not discard partial
work or rewrite benchmark rewards merely because runtime health failed.

The result optionally carries `stream_state` and `stream_diagnostics`: at most 16 recent per-request parser receipts with trace/round, provider finish and DONE observations, malformed-frame count, transport bytes, SSE events and output character/tool counts. These are not summed usage flags and contain no request bodies, credentials or provider text. They distinguish a positively terminated empty response from a cut/malformed stream; missing historical receipts cannot prove which upstream component caused a previous empty response. ATIF uses explicit current-run transcript provenance; legacy input-only snapshots rebuild current native tool receipts after the human request boundary, so historical tools neither suppress nor duplicate this run.

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

Start locally with `tofu-agent doctor` and `tofu-agent serve`. For Rust, all three commands accept `--engine rust --native-archive ARCHIVE --native-profile PROFILE.json`; the unpriced profile declares SDK HTTP/counter/ACK bounds and cannot supply routes or credentials. CLI joins runtime, host/image and callbacks in that order; SIGINT cancels the active native request. `doctor` validates the local archive and reports `execution.backend`. Missing/invalid native composition fails without Python fallback.

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

Packaged domain readers retain the same `contracts/<name>.json` or YAML
paths as the checkout. `setup.py` copies the canonical machine-readable schemas
byte for byte into wheel assets; the sdist includes those inputs and the build
hook. Guides and schema-generation tools stay outside the wheel. Artifact checks
require startup-critical swarm, paper-build and resource-policy schemas.
Problem: installed task executor imports previously depended on checkout-only
schema paths. Decision: preserve one schema authority and its relative paths at
build time. Alternatives: handwritten fallbacks duplicate wire authority;
patching each eager import leaves later optional domains with the same defect.
Schemas are loaded by their owning domains, not by the packaging layer.

The clean-wheel smoke installs only the wheel outside the repository, checks
that database drivers and full-app assets are absent while setup assets are
present, runs `tofu-agent doctor`, and completes one turn against a controlled
OpenAI-compatible server. Release workflows publish Python/TypeScript/OCI
artifacts from the same version and gate secrets, SBOMs, and image
vulnerabilities before registry publication.

## Native task Stop notification

Problem: a plain threading.Event cannot wake an async native host blocked on a silent provider. Decision: TaskRuntime creates AbortSignal, retaining Event wait/is_set semantics and exposing disposable loop Future subscriptions. The native host owns one subscription and joins its watcher before process teardown. At an already-settled native terminal candidate it detaches notification before projection, so deadline projection cannot masquerade as user cancellation. An initial Stop enters native cancellation through cancel_requested without rewriting the original deadline; active Stop enters the existing joined host cancellation and terminal/account settlement. Durable native tasks require this owned signal before route consumption. Existing externally supplied plain Events remain intact and are refused by native task composition. Alternatives rejected: polling adds idle work; a blocking waiter consumes a worker; replacing supplied Events breaks caller identity; deadline zero changes recovery policy. Actual installed-process tests cover no-request initial Stop and silent HTTP cancellation with canonical terminal ACK and unknown dispatched usage. Default production entry and whole-system performance promotion remain pending.

## Host-owned workspace bindings

**Problem:** automatic browser workspace defaults must not bind an embedded
agent to Tofu's installation or another conversation. **Decision:** SDK,
lightweight server and headless hosts keep the existing explicit config contract:
`projectPath` is the primary root, `projectPaths` includes reference/extra roots,
and `readOnlyPaths` selects read-only references. The `project` shorthand maps to
`projectPath`. Pass these per invocation, derived from the host's own editor,
checkout, mount or request authorization—not a process-wide current project.

```python
request = AgentRequest(
    messages=[{"role": "user", "content": "Review the changes"}],
    config={"projectPath": "/mounted/application",
            "projectPaths": ["/mounted/application", "/mounted/reference"],
            "readOnlyPaths": ["/mounted/reference"]},
)
```

No binding remains deliberate absence; it does not activate the conversation application's private output roots. Relative file reads require a bound root (attachment references stay owner-scoped); missing roots are an explicit empty task policy, never an older conversation cache or launch CWD. Explicit paths are interpreted on the execution host, not the SDK client's machine. Remote bindings retain the existing addressed-device contract. Workspace lists detach at kernel config construction; physical tasks own root policies even for non-Git/transient runs. Children borrow only parent-authorized roots. Instruction loading starts at those roots and never walks server-launch ancestors. Rule guidance does not grant filesystem access. **Alternative rejected:** a kernel-global auto-workspace/default project would couple independent SDK users and require application persistence in the embed. Verify `test_automatic_workspace.py`, scoped-guidance and SDK runtime suites.
### Priced completed-round checkpoints

Problem: a restored native token account retained consumption but omitted the prices/credits already paid before Suspend. Decision: priced shared checkpoints require synchronous fee-capture and prepared-restore ports on the supplied round policy. SDK composition emits checkpoint_fee_projection and checkpoint_fee_executor_origins, so older sealed binaries refuse before model admission instead of accepting an unusable priced checkpoint. The same checkpoint snapshot stores a closed host_fee_projection prefix with optional per-round task_id origins (legacy missing origins bind to the original snapshot task; a 32-byte ID adds 45 encoded bytes per round and no resident workers); raw usage stays only in its existing usage array. NativeTaskCostPolicy validates route, owner/tenant/task, wallet/reservation, exact round prefix, known usage and matching shared consumption. Restore prepares without mutation, restores the original account, then commits the fee projection after checking the entire account (including zero-token attempt ordinals). Future rounds append after that frontier without repeating consumer delivery or current-price lookup. Problem: relabeling all restored quotes loses inherited executor attribution. Decision: retain each origin; alternative: relabeling would recharge inherited work. Rust checks the fee checkpoint profile/header and the generated snapshot shape before resumed HTTP. SDK checks the extended future load frame before durable completion; oversized producer output retains paid task evidence and fails without publishing a checkpoint that cannot be loaded. Existing reader, joining, deadlines and terminal fences remain the owners. Alternatives: token-only restore reprices paid work; another journal splits authority; duplicating usage in fee rows expands snapshots; fee mutation before account admission permits partial recovery. Verify test_native_fee_checkpoint_installed and native-cost projection tests, the Rust checkpoint/stdio neighbors and generated contracts. Actual sealed processes preserve a 31-micro first round while a changed second-round price charges 61, with one tool execution and two HTTP requests. Invalid/missing fees refuse before new HTTP, and an old artifact refuses before any HTTP. These are explicit Suspend/resume ports over a supplied checkpoint policy; selected The selected TofuDB standalone task-result path is verified below; proven-quiescent crash takeover, conversation Continue integration, production default and whole-system performance admission remain separate requirements.

### Selected TofuDB priced recovery admission

Problem: standalone fee tests missed ROOT's null limits and already hydrated Continue prefix, and did not prove the selected authority retained prices or respected terminal acknowledgements across reopen. Decision: standard TofuDB builds include native-runtime-checkpoints while keeping provider-json-order disabled in that dependency; journal creation, takeover and invocation remain explicit. Journal null round/tool limits preserve unlimited execution; finite limits still fence the frontier. --no-default-features retains an engine-only build. The shared installed fixture accepts owned checkpoint/terminal composition for real backend tests. SDK response capacity follows the explicit caller bound within Python buffer representation; allocation failure refuses before journal dispatch without replay, rather than imposing a private 16 MiB quota. Actual NativeCheckpointPolicy and NativeTaskTerminalPolicy commit through one EmbeddedRuntime, join/close it, reopen the same authority and restore old fees before the second HTTP request. Hydrated paid rows/usage must exactly match the journal before shared account restore; commit rechecks the unchanged projection. A lost terminal ACK retries the same candidate and confirms the canonical task-result writer without repeating tools or fees. Alternatives: memory-only proof leaves storage fences untested; a separate test writer bypasses application CAS; closing only the client leaves the authority lease alive; opt-in packaging leaves default engine artifacts unable to provide the native journal. Verify test_native_checkpoint_policy with exact private binary/library/source identity, test_native_fee_checkpoint_installed and actual ROOT test_native_root_recovery. This proves explicit prepared ROOT Suspend/reopen, not public conversation Continue command admission, in-flight takeover or default Rust execution. Explicit recover accepts abandoned new/completed frontiers only after Store proves the recorded Linux boot/PID-view/start identity has exited, using pidfd rather than missing procfs rows or a zombie leader ([Linux process identity](https://man7.org/linux/man-pages/man5/proc_pid_status.5.html), [pidfd exit readiness](https://man7.org/linux/man-pages/man2/pidfd_open.2.html)); live, foreign, inaccessible and legacy identities and passive replay targets refuse; completed publication joins accepted canonical projection events. Structural projection derives checklist state before terminal, so completed tool progress survives SIGKILL. Problem: crash kept a valid completed frontier permanently leased. Decision: proof plus the existing CAS advances generation using Keep; alternatives of expiry-based stealing or replaying inflight work can duplicate effects. Verify system_native_abrupt_recovery_20261011.json. Actual prepared ROOT pause/reopen evidence: system_native_root_recovery_20261011.json.

### Native application generation defaults

Problem: native task composition required a caller-written generation template, so task thinking, sampling and structured-output intent could be lost at entry. Decision: absent an explicit prepare_template callback, run_native_task uses prepare_native_application_template. It resolves the existing task configuration against the issued logical model, compiles intent through the canonical reasoning compiler using the selected inference profile, applies the issued output cap, and emits the wire-model alias without internal fields. A legacy preset that changes the selected model refuses; unsupported profile intent preserves the canonical preparation error. Model attribution is seeded only after successful compilation. Structured-output schema is detached; history is neither built nor copied, and native context admission remains the sole prompt policy owner. Alternatives: hand-built effort fields drift from route capabilities; running the full body builder needlessly normalizes/copies history and cannot own native continuity; silently changing models bypasses the issued route. Explicit custom template callbacks remain supported. This supplies application preparation, not production default selection. Verify canonical generation parity and actual OpenAI/Responses/Anthropic requests over disposable storage authorities.

### Prepared tools in native application composition

Problem: native callers duplicated prepared function schemas and safety flags in a handwritten configure catalog. Decision: compile_native_tool_catalog builds the native declarations from prepared function definitions and canonical task write/ confirmation partitions. Confirmation wins over write; other functions retain the ordinary read classification. Explicit permission subsets remain caller-owned. Duplicate names, foreign permissions and unrepresented control fields refuse rather than silently changing advertised tools. The returned schema graph is caller-independent. NativeGateway derives permissions when absent; run_native_task fills an absent tools field from the gateway's frozen prepared definitions and permissions. It still validates supplied catalogs and uses the same callback, approval, context and terminal owners. No resource limit, backend or execution selection is inferred. Alternatives: a parallel handwritten catalog drifts; ignoring strict/provider fields changes capability semantics; a second registry forks task safety. Verify detached schema/projection parity and real default catalog/template/approval composition for all three supported provider protocols.

### Native HTTP demand admission

Transport no longer turns Policy.connections/pool_wait into an active HTTP semaphore or queue. Positive compatibility hints and idle-connection reuse remain; CLI uses one demand reactor; blocking work starts independently of connections. Agent stdin remains registered to that reactor through configuration and execution. active_requests replaces the retired available_slots diagnostic and spans response, required text-consumer join and terminal drain. Its RAII counter only observes; post-join Stop/deadline refuses success and preserves faults. Provider reservation/timeouts/byte bounds and connection discard/reuse retain their own authority. Verify the real 32-request TCP cohort plus HTTP/Agent/stream-progress cancellation and teardown tests. Whole system performance and default application selection are not inferred from it.
Problem: mandatory request_headers_ms imposed a total wait through upload and response headers when caller policy had no such deadline. Decision: explicit null removes that phase total while retaining connect, stream-idle, caller Stop and task-deadline owners; positive values retain their existing bound, and missing/zero/invalid values refuse. Alternatives: a large sentinel creates a hidden deadline; reusing stream idle interrupts header waits; removing validation turns omission into permission. Independent optional write_ms budgets post-connect physical writes/flushes; [native transport ownership](modules/llm_io.md#native-physical-writes-and-stdio-framing) owns its TLS/proxy/H2 failure and cancellation semantics. Automatic production defaults still require full qualification. Verify http/header-wait stdio tests, the configure schema and test_native_application_http_policy before promotion; `system_native_http_header_wait_20261016.json` records the original pre-header milestone and its then-outstanding qualification gaps.

### Optional native round count

Problem: a mandatory positive round count introduced an unrelated execution
ceiling when application policy left that dimension unbounded. Decision: configure
rounds and checkpoint round_limit accept explicit null; Agent Budget uses
Option<usize>. Null removes the count ceiling, while positive integers retain
pre-tool next-round refusal. Zero remains invalid. Checkpoint restore requires
exact equality of the original optional limit before any effect. Tool count,
context/feedback/output byte admission, caller/model policy, progress and time
limits remain independent. SDK text delivery retains sequence/frame admission;
null rounds keeps close streaming under frame/time bounds; settlement ACKs still join.

Configure uses Catalog.validate_protocol: required null is a value and must match
the declared schema type. Ordinary tool validation retains its Python-compatible
required-null rejection and dispatch authorization. Alternatives: using a huge
integer disguises a limit; treating null as missing cannot express policy;
relaxing tool validation changes handler error/default behavior. Verify eight-round
execution, finite pre-tool refusal, unchanged checkpoint limits, original tool
oracle and installed stdio streamed delivery. Other native execution dimensions,
application configuration/default selection and full performance admission remain
unfinished.

Native application run_native_task installs the canonical context preparation
when round_policy is absent or supplies accounting without prepare. It binds the
issued route/output/tools, resolves project policy through model_config, performs
existing round injection and prompt admission, and joins preparation on cancel.
Explicit preparing policies retain ownership. A missing accounting port reserves
no model resources; model/account and pricing remain their existing owners. The
context adapter closes at native exit without retaining a history mirror.

Native application model_control now defaults to NativeModelAccountPolicy when
no model policy or pricing resolver is supplied. It requires the existing owned
task/tree ExecutionBudget and explicit model_counter configuration; it creates
no account, counter or pool. Missing/foreign accounts and token_admission as a
second authority refuse before route consumption. Priced execution still requires
the resolved pricing owner; explicit model policies remain supported. Parent
limits clamp native output reservation, and known/unknown usage follows existing
settlement without claiming child completion before its owner closes membership.

Application model_counter may omit encoding and output_field while retaining
explicit byte/depth/stack bounds. The default compiler selects the canonical
local tiktoken tier from the issued logical model (not its provider wire alias),
without loading Python BPE, and derives output_field from the final generation
template. Explicit encoding remains supported; mismatched output fields, unknown
keys and invalid schema bounds refuse before process launch. All seven fields
still cross the native wire, and this local tier does not claim exact counting
for every provider. Routing gives preparation detached narrow inputs; no history
copy, new counter, tokenizer cache, worker or account is added.

Native successful completion with NativeContextPolicy publishes the shared final
Request Inspector messages_snapshot (kind=state, roundNum=final) after terminal
projection and round close, before canonical persistence. Model/tool metadata
comes from the existing frozen context binding; message state comes from the
settled task. The shared leaf applies existing wire/media sanitization and keeps
best-effort diagnostic publication. It appends no assistant/history, emits no
second round close/done, and owns no terminal writer. Frozen commit retry does
not repeat projection or snapshot. Explicit preparing policies retain their
presentation ownership. Production root selection remains separate.

Native token ingress uses a count-only ordinary BPE kernel over exact pinned
tiktoken-rs 0.11.0 merge ranks and split patterns. It retains forward ranks, a
derived 256 KiB byte-pair index and pattern, with request-local scratch; decoder,
special-token encoder, unstable-completion index and token-ID output are absent.
Lazy initialization verifies table SHA-256/ordinal/byte coverage before use;
existing owner/input/depth admission and scoped worker joining stay unchanged.
Generated table source lives under src/token_ranks so native build identity
includes its exact bytes. Regenerate/check with gen_native_token_rank_tables.py
--source-dir pointing to the pinned upstream assets directory. This does not
change the selected local-tier policy or claim exact counting for every model.

Native BPE pieces at least 1 KiB use a request-local indexed min-heap. Each byte
start owns at most one heap entry, and a merge updates only its two affected
neighbors. Rank then original byte-start preserves leftmost tie ordering.
Short pieces use 512-byte inline boundaries below 32 bytes, then ordinary Vec.
u32 offsets cover ordinary pieces; usize handles larger representable inputs
without a new input ceiling. Compact scratch reserves at most 20 bytes per
input byte (16-byte node plus 4-byte heap slot), versus 16 bytes in the former
linear boundary representation. It drops at request return; token vocabulary,
owner/input admission, encoder lifetime and scoped worker cleanup are unchanged.

run_native_prepared_task consumes the existing RootLoopRequest after application preparation, plus the owned client/repository/selection/executor and explicit native resource/recovery and pending-receipt bounds. It derives wire identity and history from the owned task/request, builds the canonical tool gateway, and compiles one primary route on the supplied executor under copied call context. Route/preparation overrides and foreign/transient tasks refuse before repository access. Cancellation withdraws only queued acquisition; started reads join, late private routes close, and original cancellation/execution errors survive cleanup. The entry delegates all rounds/accounting/terminal commit to run_native_task, defaults live text on (explicit false remains), and retains supplied projectors. It does not rerun preparation, copy history, create an account/client/pool/store, select a storage backend or change the production root selector.

Native total_tool_calls separates cumulative task tool quota from the positive
per-response tool_calls admission bound. Explicit null means no native total
ceiling, zero allows model prose but refuses any tool effects, and nonnegative
integers retain batch preflight. Omission preserves the legacy cumulative
tool_calls limit for direct callers. Prepared application entry defaults the
new total to null; canonical gateway/shared task budgets still own application
quota. Checkpoints bind nullable tool_limit exactly; changed/missing recovery
policy refuses before another model/tool request. Byte, owner, response and
progress checks remain independent.

Prepared native invocation may receive resource_policy from the existing
lib.resource_control.runtime.launch_policy result. native_resources fills
missing byte/count fields using that issued capacity and the host Budget; it
never probes or changes host resources. Explicit overrides remain. Callers
may use `native_production_profile.compile_native_application_profile` for HTTP, counting and terminal defaults; route binding still supplies epoch/model identity.
`compile_native_transport_budget` shares launch-sized image/stdio bounds with SDK composition; neither compiler grants model/tool authority or production selection.

ApprovalRequest.remaining_seconds is optional. An explicit null remaining_ms
from a task without expiry preserves ordinary approval and joined Stop; finite
remaining time retains timeout denial. Missing time is invalid protocol input.

[Image preparation](NATIVE_IMAGE_ADMISSION.md) defines optional overlap. An explicit NativeArchiveImage may own one pinned native release across
principals. NativeProcessClient.from_shared_archive borrows an independent
sealed-image descriptor on first run, verifies its own binary bound and keeps
its own principal/run state. Close clients and the image owner explicitly;
closing the image prevents future borrows but does not revoke already borrowed
descriptors or running tasks. No client cache, automatic download, startup work
or default application backend selection is introduced.

server.create_app/create_production_app and create_application accept an
explicit native_image. The application exposes tofu_native_host, whose async
client(principal, budget=...) scope returns an independently owned native
client. Quart closes the host; production shutdown joins it before dependencies
close. Register before serving; duplicate registration requires the same image.
Clients are transient and acquisition remains lazy. Explicit native_root=NativeRootConfiguration
binds ordinary ROOT startup to the serving runtime, issued selection, owner-aware repository/pricing and prepared checkpoint factory. Problem: a fresh task ID changes the default epoch. Decision: adopt the trusted checkpoint factory's stable epoch only after exact task-object/owner/executor checks; alternative: per-attempt epochs cannot transfer the executable frontier. The frozen application profile remains unchanged. Verify priced ordinary HTTP Continue/checkpoint-resume and joined journal cleanup.
Optional `NativeRootConfiguration(native_repository=repository)` uses the SDK's
`NativeRepositoryConfiguration`. Each application owns one fresh lazy scan runtime;
only its issued principal/route selects it. Request JSON cannot replace it.
Children must pass parent port/id/owner checks before borrowing that runtime;
their scan/cancellation scopes remain independent. Shutdown detaches admission,
joins repository disposal off-loop, then retires model clients through NativeHost.
Failed disposal keeps its owner for explicit retry.

Problem: shielding an unscheduled coroutine lost joined cleanup on cancellation
(`.done()` raised AttributeError); a worker Task's control exception escaped the
loop even after lifespan caught it. Decision: `_joined` retains one scheduled cleanup
through repeated caller cancellation. Repository cleanup returns failures to
lifespan for propagation after its cleanup; failed scheduling closes the unstarted
coroutine and preserves retry. Alternatives: rescheduling duplicates work;
swallowing failures hides disposal debt; raw worker Tasks duplicate control failure.
NativeHost also carries client and token/Flow worker cleanup failures as results
across Task boundaries, then reports one frozen aggregate after every owner was
attempted. A control failure cannot bypass siblings or escape the serving loop;
failed clients remain owned. Repeated close observes that same aggregate without
repeating cleanup. Failed task scheduling releases its unstarted coroutine;
client scheduling failure still joins accepted siblings and retains the unscheduled owner;
outer host scheduling can be retried while admission stays closed. No worker,
queue or optional-library lifetime is added. Verify `test_native_cleanup_join.py`,
`test_native_application_close.py`, `test_native_host_close.py`, and the active-scan
cancellation/ownership application lifespan tests. `system_native_host_close_20261015.json`.

Problem: recovery lost unknown provider usage and admitted another request. Decision: persist optional usageComplete and restore false before model admission, retaining paid lower bounds even without a token cap. Alternatives: zeroing unknown consumption or trusting request config lose authority; live uncapped semantics remain unchanged. Verify system_native_unknown_usage_20261011.json. Public Stop withdraws cancelled presentation frames while the terminal writer commits settled evidence; Continue appends paid rows after the retained prefix without repricing it. Problem: cumulative fee rows were charged again under a fresh executor id. Decision: native producers stamp taskId; native wallet settlement validates the whole token/quote prefix but folds credits only for fresh rows owned by this executor, in one pass without a prefix copy. Legacy invoice rows may acquire missing taskId only from read_accepted_resume_lineage: explicit owned reads verify the transaction-issued _resumeSource, exact current task/attempt/Turn and distinct terminal source before a detached, fully validated prefix replaces live fields. Caller config grants no lineage; missing/same/foreign, superseded or changed provenance refuses before provider entry and retains native billing authority. Alternatives: charging the visible cumulative total duplicates old fees; discarding old rows loses allowance and audit evidence; repricing changes prior debt. Verify system_native_resume_wallet_20261012.json. Checkpoint-resume creation rematerializes retained tool blocks before pending publication, preserving strict readers. Evidence: system_native_public_continuation_20261011.json. Binding precedes Attempt/task publication inside the existing rollback boundary; global default selection remains unqualified. Problem: manual invocation did not wire ordinary HTTP tasks or settle their issued request resources. Decision: borrow the application runtime and existing session/account owners at the shared boundaries. Alternatives: per-task loops/pools, raw request backend fields and success-before-resource-settlement create competing authority. Verify system_native_application_entry_20261011.json.

NativePreparedInvocation is the explicit synchronous worker port for a prepared durable root task. Supply the application host/serving loop, principal, selection/repository, transport/resource profile, callback executor and policies, then bind it internally as task._nativePreparedInvocation. The root owner keeps preparation and common teardown; the native task owns terminal projection and commit, so Python's loop/post-loop tail is skipped after handoff. Native terminal settles the existing ExecutionSession resources before persistence; failed invariants persist failure and reject the successful candidate, without repeated release. This is not a user configuration field or automatic default. Application native_root assembles this port; counted execution prepares the canonical shared account after ROOT config/resume preparation. Use a distinct callback pool when the worker pool has a fixed thread count. Native terminal failure never triggers whole-turn replay; successful history carries the current-run marker. Real root-entry checks cover three protocols, success/failure/owner Stop and durable tool/history outcomes. `benchmarks/native_agent_root_measure.py` owns warm/cold/CPU/exact-work evidence. Problem: PID sums duplicate vfork address spaces; running kcmp comparisons can race. Decision: `native_root_memory.py` uses separate disposable workers, pidfd-owned pauses, all-thread stop checks and kernel VM identity before RSS/PSS reads. Raw sums remain; denial cannot qualify. Alternative: live heuristic dedup is unproven. Source: [kcmp](https://man7.org/linux/man-pages/man2/kcmp.2.html). Default/recovery and whole-project admission remain pending.

Explicit retain_execution_history adds execution (admitted messages and raw
verified-round usage) to error/aborted terminal events. Prepared application
entry enables it by default, while direct legacy clients omit it. This is
observed model-facing prefix evidence; it grants no checkpoint resume or usage
refund. Terminal projection retains task messages while preserving existing
paid accounting. Old archives reject the new configure field before HTTP.

Native root worker invocation now shares final prose translation and the
post-commit observer/release helper with Python. run_native_task optional
after_terminal runs on the supplied executor only after terminal persistence
was accepted and native work joined; direct callers retain their existing
default ownership when the callback is omitted. The callback creates no new
terminal or fee commit. Observer policies retain their existing feature gates.

Prepared native execution now carries the existing search-result accumulator
through the canonical gateway and reuses the source-footer backstop before
terminal persistence. The footer decorates task display text only; raw model
history/usage and terminal candidate bytes retain their original semantics.

## Explicit persistent native agent worker

**Problem:** ordinary native stdio creates a process and local encoder per task. **Decision:** `tofu-native run-http-agent-worker FRAME_BYTES OWNER` reuses one reactor and lazily owned cl100k/o200k encoders through the existing agent entry. The fixed positive owner is explicit startup authority. The canonical stdio schema's worker definitions specify a separate `tofu.native-agent-worker/v1` envelope: initial `worker_ready` has sequence 0; `worker_run` carries the next sequence and an unchanged configure object; every outgoing callback/terminal is `worker_event`, and every incoming reply/cancel is `worker_control` with the same owner/sequence. Inner frames retain ordinary stdio v1. `worker_shutdown` uses the next sequence and returns `worker_closed` after encoder disposal. Idle EOF closes; partial EOF, foreign owner, unsolicited command and stale scope refuse. Readiness follows actor join, pending/reply quiescence, exclusive terminal input release and destruction of per-task objects. Scope parse failure is sticky; terminal retry cannot turn a stale ACK into current authority. Buffered trailing controls refuse reuse. Ordinary single-task output/protocol remains unchanged. **Alternatives rejected:** reusing task/account/credential state leaks scopes; accepting bare cancels or repeated unscoped ACK IDs targets a later invocation; serializing all application work through one worker caps machine capacity. This is one explicit worker, not an application-wide admission gate. Encoders contain immutable vocabulary only; HTTP client, model account, session, tools, callbacks, task and terminal debt remain per invocation. No takeover or replay is implied by a failed worker. Application NativeHost still uses transient processes; principal/tenant/workspace-owned demand reuse and full priced/counted application warm/cold/stopped-memory gates remain pending. This entry introduces no production selection, process pool, durable store or backend switch. Verification: real stdio tests cover two same-ID tasks with fresh finite accounts, changed headers, terminal ACK scope, encoder reuse and exact close; old ACK and foreign/bare commands refuse before readiness/provider access. The resource measurement and diagnostic limits live in [resource control](RESOURCE_CONTROL.md#explicit-persistent-native-worker-mechanism).

**Mixed native application profiles — Problem:** one actor-checkpoint profile refuses ordinary ROOT execution, preventing a single application from serving both chat and recoverable Flow.
**Decision:** NativeRootConfiguration accepts an explicit full `flow_configuration` and synchronous `flow_checkpoint_factory`. Omission retains the existing shared profile/factory. Both profiles pass the same authority validation and freeze independently; binding selects by the existing trusted Flow boundary, while Goal preflight reads that same Flow profile. Ordinary ROOT retains its own checkpoint policy and no actor-recovery capability. Both kinds borrow the same application host, workers and selected storage; issued route and account owners remain task scoped.
**Alternatives rejected:** stripping actor flags from every ROOT silently changes explicit policy; enabling actor checkpoints on ordinary ROOT weakens ownership; separate applications duplicate runtime ownership.
Verify `test_native_application_profiles.py` and `test_native_application_profiles_installed.py`, including issued-owner refusal, frozen inputs, preflight bounds, real actor checkpoints, following ordinary HTTP ROOT, money and joined teardown. `system_native_application_profiles_20261016.json` records backend scope. This composition does not authorize default or performance promotion.

## Application-owned native worker reuse

**Problem:** the persistent CLI was not owned by durable application invocation. **Decision:** trusted NativeRootConfiguration(worker_reuse=True) enables demand workers on NativeHost through the issued launch policy. Preparation configures lazily on the existing sync executor; actual acquisition binds to the serving loop. Reuse keys include the complete principal (tenant/subject/scopes), resolved cwd plus device/inode, and immutable transport budget. Busy workers never queue other tasks: demand starts another worker. root_maximum sizes total idle reuse only, not execution admission. Cache eviction/host shutdown closes and joins workers before image release; active clients settle through the same shutdown handlers, model account, route and terminal ports as transient execution. The process view translates scoped frames and treats joined worker readiness as invocation EOF/exit; one bounded pump owns physical stdout. Late writes cannot reuse a closed view. Failure/cancelled cleanup retires the worker, never retries the task automatically. Failed startup joins and releases the acquired image. **Alternatives rejected:** owner ID alone crosses tenant/auth scopes; path text alone misses replaced directories; a bounded active pool caps machine capacity; retaining a failed callback/process loses unknown financial or delivery debt. Wrapper overhead is distinct from original inner-frame admission. Rust adds only canonical envelope bytes to transport parsing and still checks the original inner frame size. Native worker version is generated from the canonical schema. `system_native_application_worker_reuse_20261013.json` retains real repeated ROOT work, worker PID/sequence/idle witnesses and exact money/work. The fixture uses disposable SQLite; no live backend switches or deployments. The option remains false by default. Full native-authority warm/cold/stopped-memory performance admission, broader tool/Flow/concurrency coverage and default Rust migration remain pending. Existing failed full warm results are not waived.

Release ownership fence: **Problem:** a retired invocation could clear an idle
worker or release a later invocation on the same process. **Decision:** release
requires object identity with the worker's current invocation before any mutation.
**Alternative rejected:** pool identity or successful readiness alone remains
true for a previous invocation. Repeated/stale release fails without touching
idle membership, process lifetime or current scope. Regression tests cover both
idle and actively reused workers in `test_native_worker_pool.py`; this adds no
pool, cache, resource limit or default-selection admission.

The same current-invocation identity fence protects physical kill. A retained
old process view cannot terminate an idle worker or the next invocation; the
current view keeps its kill/reap behavior. Pool-level physical teardown remains
owned by host shutdown. Tests cover stale kill in both states and current kill.

## Pure native token count ABI

**Problem:** host admission repeatedly enters Python batch executors although the exact count-only Rust encoder already exists. **Decision:** optional `token-count-abi` exports version/open/batch/close over the canonical `contracts/native_token_count_abi_v1.json` contract. Monotonic non-reused handles own lazy encoders; close withdraws new lookup and joins accepted calls before release. UTF-8 text/output spans are caller-owned and live until synchronous return. Batch counts publish only after all input and computation succeeds. Empty input does not initialize vocabulary. No task, price, account, permission, store, prompt cache or worker pool is created. **Alternatives rejected:** raw Box pointers permit stale use/free races; process-global encoder caches hide lifetime; per-call subprocesses repeat initialization and transport costs. The feature stays off by default. Archive packaging, verified Python binding, resource measurements and whole-application performance admission remain pending. The loaded-encoder teardown test proves registry removal and loss of the last encoder reference after close; allocator RSS return is not implied. Actual debug C ABI tests compare 4,004 counts against Python tiktoken across both encodings, including four concurrent callers, malformed UTF-8 and stale-handle refusal. These are correctness checks, not release performance admission.

Reproduce the explicit library check with
`python3 scripts/check_native_token_count_abi.py --library /absolute/path/libtofu_runtime.so`
(after building with `cargo build -p tofu-runtime --lib --features token-count-abi`).
The checker loads trusted local executable code; archive identity verification
remains the packaging owner's responsibility. Graph control uses the separate [Native Flow contract](NATIVE_FLOW_CONTROL.md).

**Packaging decision:** reuse the pinned archive rather than introduce a second release/distribution authority or extract a mutable install tree. Native archives may also include Flow control with `--flow`; combining it with `--token-count` emits one library and both exact ABI contracts. The Flow loading and lifetime contract is [Native Flow control](NATIVE_FLOW_CONTROL.md). Native archives may include the optional token-count extension using `build_native_runtime.py --token-count`. Its member and companion ABI contract are declared by the artifact contract; both must exist and validate together. Legacy archives remain valid without this extension. `seal_token_count_archive` uses the existing bounded private snapshot, manifest checks and sealed memfd, requires an explicit archive digest/version and matching host, and returns an owned descriptor without loading code. Replacing the archive after inspection cannot replace these verified bytes. The eventual binding must join accepted calls before releasing handles, library mappings and this descriptor. Python hot-path selection and complete-application performance admission remain pending.

**Python binding decision:** `NativeTokenCounter` explicitly owns one sealed library mapping and up to two lazy immutable encoder handles. It accepts ordinary UTF-8 text batches with explicitly chosen encoding; no model resolution or fallback is added. Python call leases retain borrowed buffers and the code mapping through exceptions and concurrent close. Close rejects new leases, joins accepted calls, closes native handles, then dlcloses before descriptor release. Teardown failures retain resources and refuse future use. The archive's complete ABI contract must match the generated local contract before loading. Continuous machine-word descriptor/output arrays avoid per-batch ctypes class retention; encoded text remains call-local. Surrogate-containing Python strings are explicitly refused by UTF-8 encoding; application integration must preserve its existing tokenizer compatibility policy before selecting this backend. No global encoder, cache, executor, timer or import-time load is added. Alternatives rejected: GC-only unloading has no join witness; closing the memfd before dlclose permits reused loader paths; serializing the whole batch blocks independent callers. Actual release-archive parity and repeated load/close verify this boundary; application selection, residency measurements and whole-task performance admission remain pending.

Reproduce the installed binding check with
`python3 -m scripts.check_native_token_binding --archive ARCHIVE --sha256 PIN --application-version VERSION --binary-bytes LIMIT`.
For an installed library use `--library LIBRARY --sha256 LIBRARY_PIN
--binary-bytes LIMIT` instead. The checker compares 12,012 counts across both encodings and four callers,
then witnesses zero remaining mappings for the sealed inode after each of three
load/close cycles. This is code-mapping release, not allocator RSS certification.

**Application adapter decision:** `create_application`/`configure_application` accept explicit `native_token_count=True` with an admitted native image. `NativeHost` borrows the archive pin/version/byte bound, or the separately pinned installed pure library described in [image admission](NATIVE_IMAGE_ADMISSION.md), and lazily owns one pure counter. Native client scopes explicitly carry their issuing host into the shared NativeContextPolicy. Route binding and the complete preparation/projection call enter native_token_scope, resetting it on success, failure and cancellation. The task-owned prepared invocation supplies this scope before initial context construction. Gateway approval and execution copy the caller context onto the existing worker lane and restore the lane context afterward. Foreign task owners are refused before using the prepared invocation. Tasks without a prepared native invocation preserve their caller's explicit counter scope. Background tasks therefore use the selected counter without a Quart request context; unrelated contexts retain their own application counter. Unselected applications retain their existing encoder path. Count-only results preserve cache keys, multiplicity, identity receipts, structural/image charges and model confidence. Paired/lone surrogates use tiktoken's UTF-16 surrogatepass/replace repair before UTF-8 encoding. Existing public fallback behavior stays owned by the token façade, never by the native binding. The host closes clients/workers, joins counter initialization and accepted counts on an existing off-loop lane, then retires the image. Never-used counters start no loader, vocabulary or shutdown worker. No new pool, prompt cache or global backend is introduced. Alternatives rejected: process-global selection crosses app lifetimes; binding installation on import taxes idle; sharing mutable application context across unrelated tasks changes ownership. Default activation, resource residency and complete-task performance admission remain pending.

**Background scope correction:** HTTP ROOT comparability found the selected
library was never loaded: a new task's background context did not inherit Quart's
request app context. Request-context parity alone had not proved ROOT use.
Explicit host propagation fixes that gap; application lookup remains for direct
HTTP token endpoints. The initial no-load cohort is invalid for native-token
performance. A background preparation test witnesses native use during prompt
injection, admission and wire projection, with context reset after return.
No backend default or performance claim follows from this wiring correction.

**Native token application admission:** the paired cohort contains 14 fresh workers and 1,504 identical priced/counted HTTP ROOT operations, using the same current native runtime and daemon with only token selection changed. All candidate workers witness loaded/closed libraries. Three timing pairs have P95 ratios 1.325, 1.000 and 1.386, with CPU-mean ratios 0.962, 1.020 and 0.980; native token selection remains explicit and default admission is rejected. Separate stopped-VM PSS ratios are 1.024, 1.016 and 0.997. Memory results do not defeat latency failures. Cold startup, scaling and other backends remain unqualified. The instrumented 26-task growth workers observe 1,285/1,289 storage attempts and nested terminal/context costs; these guide investigation of publication/settlement boundaries without attributing the tail regressions.

**Publication diagnostic decision:** unlabelled storage.v2 aggregates mixed production work with benchmark polling. Diagnostic spans now retain canonical operation names from storage_operations_v1.json, fixed phase labels and no payload/owner/command identifiers; the stable aggregate label is preserved. Terminal/storage profiling observes 26 real ROOT operations. Fixture polling accounts for 233 Attempt reads; terminal-writer context observes 26 each of event publication, result checkpoint, event read, attempt read, scheduler execution lookup and queue depth. Other observed contexts issue 215 event publications and 78 Attempt reads. Context labels are not complete cross-thread causal ownership. Investigate native event publication and terminal transaction composition next; do not optimize fixture polling as product work or remove ownership reads without atomic replacement. Alternatives rejected: arbitrary operation strings can retain caller content; unlabelled totals misidentify the target; profiler cumulative time alone is not pure CPU or P95 causality. Existing token tail failures remain failures.

**Completion observation decision:** original HTTP ROOT clocks include 10 ms Attempt/resource polling. Same-implementation calibration ran six fresh workers and 726 identical operations; P95 ratios were 0.970/0.975/0.886, so this sample does not prove stable symmetric 5% tail sensitivity. Earlier candidate regressions remain recorded and rejected. `native_application_root.py --completion-observer worker-future` instead borrows actual scheduler Futures, verifies route/client/job retirement, freezes clocks, then independently reads durable Attempt/Turn/result and pricing evidence. Poll remains the default; different observer modes refuse timing comparability. The observer never cancels productive work when observation times out; exit joins submissions in progress and accepted Futures, preserving cancellation before retiring metadata. Idle physical carriers and Future notification callbacks are not claimed terminated by job retirement. No product worker, store, timer or execution policy is changed. Alternatives rejected: shortening poll intervals adds measurement reads; replacing durable proof with a callback weakens comparability; mixing observer clocks fabricates a runtime speedup. Python/native future-mode smoke establishes ten matching operations and joining, not performance admission. Future-mode calibration and full-system gates remain pending. Reproduce correctness with the ordinary ROOT CLI's explicit observer option; retain observer metadata in any future timing collector.

**Collector observer boundary:** native_application_root_measure and native_root_memory --entry application accept explicit completion-observer; worker commands, reports and source witnesses preserve it. Timing and memory qualification reject missing/foreign/invalid future-mode observations. Legacy rows remain poll; prebound memory cannot select the application-only future observer. Future calibration ran 726 identical ROOT operations in six workers. P95 ratios 1.022/0.941/1.023 and P99 ratios 1.010/0.990/0.945 still do not prove stable symmetric 5% tail sensitivity. Separate official collector smoke ran 62 joined operations and correctly remained incomplete. Poll remains default; earlier candidates stay withdrawn. Next use larger independently controlled cohorts for sensitivity and full native application admission; no runtime promotion follows from this check.

**Native additive usage projection:** `usage_aggregate::aggregate` projects already-admitted native JSON rounds into a fresh public counter map. Its detail labels and truth fixtures derive from `lib.cost.accumulate_usage_into` through `gen_native_usage_aggregate.py`. It preserves alias keys separately, numeric kind, one level of billing details, scalar/detail collisions and Python's nonfinite-sum skip; booleans, private keys and opaque provider trees do not become counters. Checked u64 sums return CounterOverflow without partial output or input mutation. Finite f64 counters retain their represented numeric meaning. `aggregate_json` additionally accepts borrowed raw round JSON and checks positive integer width before numeric decoding, including when the prior total is a float. It decodes only counter-bearing maps; opaque values retain raw spans. Duplicate keys use their last value, negative integers are ignored and integer -0 stays integer zero. Malformed JSON/non-array roots refuse without partial output. The Value entry cannot recover integers already rounded by a caller. Python supports wider positive integers; those explicitly refuse in the raw entry. Neither entry grants general wire or financial admission. Problem: terminal projection still performs additive usage work in Python. Decision: establish the Rust kernel and actual Python oracle before composing it into terminal transport. Alternatives rejected: normalize-before-add loses separate provider aliases/details; saturating or float-converting wide sums invents evidence; replacing raw usage with a summary weakens replay/accounting. No budget, pricing, wallet, refund, store, cache or worker authority is added. Explicit terminal_persistence.usage_summary selects a native successful-terminal projection over already-admitted Values, frozen once and reused on ACK retry. The profile/counts travel beside raw usage; SDK selection, retry identity and candidate byte bounds include them. The application validates the generated schema and uses counts without Python accumulation. Failed/aborted history has no summary; frozen paid usage is never accumulated again. Missing selected or unsolicited summaries refuse. Old/default profiles retain the existing path. This Value binding cannot recover lexical width lost during provider decoding; the raw entry remains separate. End-to-end performance and default promotion remain pending. Alternatives: recounting raw rows in Python defeats the port; replacing raw rows loses evidence; recomputing on retries repeats projection.

Verify terminal host/projection tests and agent_stdio terminal ACK cases; run
`test_native_terminal_usage_integration.py` with TOFU_NATIVE_USAGE_TEST_BINARY
set to a source-matched local binary for real SDK/TCP/projection retry proof.

**Terminal additive summary application cohort:** three alternating same-native archive on/off pairs completed 726 priced SQLite HTTP ROOT operations in six fresh workers, with exact paid-work signatures and joined retirement. Every selected summary request had already-frozen paid usage, so this fixture does not exercise replacement of Python accumulation. Warm latency/CPU ratios are in `system_native_terminal_usage_cohort_20261013.json`; no stable benefit or default promotion is established. The first cohort is excluded because a collector smoke overlapped its second pair; the fresh cohort ran after both jobs joined. Official selection/witness smoke remains incomplete. Timing collectors freeze selection and physical summary delivery; memory refuses this unqualified variant. Problem: native delivery alone could be mistaken for saved work. Decision: keep explicit selection, distinguish paid frozen requests and investigate terminal copying next. Alternatives: globally enabling summaries adds work to paid ROOT; accepting untagged memory rows conflates variants.

**Terminal validation ownership experiment:** Problem: persistence copies its JSON envelope for existing owned validation, then serializes the original. A move-and-serialize candidate passed exact wire tests and showed component gains, but real SDK/transient delivery does not establish a stable gain. Three alternating pairs per 1 KiB/4 MiB history completed 1,440 native executions in twelve fresh workers, exact history and one model call per operation, with one failed receiver ACK followed by the existing frozen retry and joined exit. Small-history tails and a large-history tail sample worsen; timings do not identify a cause. Decision: restore the exact measured baseline terminal owner; retain all raw ratios and candidate patch. Alternatives: shipping from component gains alone ignores transport/host work; bypassing validation weakens normalization. No cache/worker/store/default is added. The pure byte-identity test and terminal_validation_measure remain diagnostic. The native_terminal_delivery collector uses the real SDK and existing transient receiver, never mocks persistence, and proves model/receiver/history/cleanup counts. It excludes Quart, durable storage, wallet, concurrency and memory qualification. Next investigate repeated full-history ACK requests/final-event delivery while preserving frozen candidate authority. Verify terminal delivery collector guards and real stdio terminal ACK tests before changing that boundary.

**Explicit terminal completion reference:** Problem: after an accepted frozen terminal request, native delivery sent the complete event/history again. Decision: terminal_persistence.completion_reference selects a terminal_committed metadata frame bound to version, owner, task, operation, accepted attempt and outcome. SDK requires explicit selection and accepted ACK, validates the generated schema and exact binding, and returns a fresh view of its existing frozen event bytes. Changed/premature/unsolicited references and selected full-event fallback refuse. Normal and cancellation drains validate references; process exit/outcome and worker sequence/readiness fences remain. Retry requests retain complete raw evidence, existing delivery debt and no model/tool reexecution. Default profiles retain full events; old native schemas refuse the new selection before dispatch. Alternatives: trusting a status-only notice loses candidate identity; retaining a parsed mirror duplicates history; changing all artifacts breaks old callers. Verify SDK short/1 MiB history plus retry, stdio success/reference/abort cases, host forgery checks and generator drift. native_terminal_delivery can select --completion-reference and requires one verified frame per operation. Full application timing, recovery/scaling/memory and default promotion remain pending.

**Terminal reference delivery cohort:** three alternating same-release-binary off/on pairs per 1 KiB/4 MiB history completed 1,440 real native executions in twelve fresh workers, with one model round and one failed receiver ACK followed by frozen retry per operation. Exact history/work and physical reference counts matched; all workers joined. The 4 MiB terminal-tail P95 ratios were 0.8125/0.7540/0.8229, complete-operation P95 0.9705/0.9568/0.9591 and CPU 0.9708/0.9691/0.9596. Small history retains a P95 1.1108 adverse sample. Problem: reduced final bytes alone do not establish universal benefit. Decision: keep explicit selection and `system_native_terminal_reference_cohort_20261013.json`; no global/default admission follows. Alternatives: enabling for all histories ignores small-task tails; treating transient receiver ACK as durable commit misstates coverage. The benchmark compares only matching binary/work/policy in distinct workers and refuses missing selection/reference/cleanup evidence. Next measure paid durable HTTP ROOT and remaining lifecycle/resource gates; investigate full-history ACK retries while retaining frozen raw evidence and authority.

**Durable terminal reference composition:** selected reference delivery now has real priced/counted ordinary HTTP ROOT proof through disposable SQLite and a current-source TofuDB daemon. Each backend completed five Python and five native ROOT operations with identical paid-work signatures, committed Task/Turn/replay readback and joined worker/route/native retirement. Native SDK validated five reference frames per backend. Problem: transient delivery did not prove durable publication. Decision: freeze explicit selection and physical SDK frames in the application collector; `system_native_durable_terminal_reference_20261013.json` retains SQLite incomplete and native small-sample regression verdicts. Alternatives: configuration-only proof misses actual delivery; attributing Python/native ratios to reference selection confuses the experiment; accepting unqualified memory rows conflates variants. These are semantic smokes, not performance/default admission. Cold, memory, concurrency and full application timing remain pending. No deployed authority/backend changes. Next compare same-native selected/unselected durable cohorts under the current-source authority before any default promotion.

**Same-native durable reference cohort:** three alternating off/on pairs completed 726 short-text priced/counted durable HTTP ROOT operations in six fresh workers using one archive and one current-source TofuDB daemon binary. Paid-work signatures, committed Task/Turn/replay state and joined retirement match; selected workers validated 121 reference frames each. P95 ratios were 1.2761/0.9895/1.0500; no stable short durable ROOT gain is established. Problem: Python/native comparison could not isolate the reference change. Decision: retain explicit selection and `system_native_durable_reference_cohort_20261013.json`. Alternatives: promoting from transient long-history gains ignores durable costs; renaming native rows to Python corrupts pricing/provenance. The dedicated comparator shares canonical durable/financial validation, requires actual native arms and checks observer, authority, archive, policy, work, affinity, clocks and fresh worker identity. Instrumented or unfinished rows refuse. Cold, memory, concurrent/recovery and full migration admission remain pending; source-matched build replaced the old mismatched engine without deployment or live backend change. Next reduce long-history ACK duplication, preserving frozen raw candidate/receipt authority.

**Explicit terminal retry reference:** Problem: a failed commit/receiver ACK resends and re-encodes the full frozen history on each terminal retry. Decision: terminal_persistence.retry_reference retains the full first request, then selects a terminal_retry metadata frame only after an explicit host retry ACK. Native persistence binds the same immutable Task candidate address/status and frozen checkpoint disposition; a different candidate or marker refuses. SDK requires selection, saved raw candidate/receipt/summary bytes, retry-ready state and exact owner/task/operation/version/next attempt/outcome. It reuses the same bytes for the existing policy without model/tool replay; checkpoint disposition is checked again. Full retry fallback, premature/stale/foreign references and post-acceptance requests refuse. Normal and cancellation drains share this handler. Final completion reference is independent; defaults retain full requests/events. Only bounded scalar metadata is added to the per-task persistence port, released with that port; no history mirror, worker, store or authority is added. Alternatives: caching another parsed history duplicates context; replaying projection/effects on retry breaks settlement; skipping checkpoint rechecks weakens recovery fences. Verify real stdio retry bytes, SDK short/1 MiB history with both selections, host forgery/checkpoint guards and generator drift. native_terminal_delivery --retry-reference requires actual retry frames; its comparator holds final-reference policy fixed. Timing, durable application/resource gates and default promotion remain pending.

**Terminal retry-reference cohort:** three alternating same-release-binary off/on pairs per 1 KiB/4 MiB history completed 1,440 real native executions in twelve fresh workers. Final completion reference stayed enabled on both sides; only retry_reference changed. Exact history/work, one model call, two receiver calls and one verified final frame per operation match; selected workers additionally verify one retry frame. All workers joined, source/archive identity stayed fixed. For 4 MiB, tail P95 ratios were 0.7122/0.6113/0.6494; complete-operation P95 0.9555/0.9524/0.9511 and CPU 0.9506/0.9493/0.9491. Short history retains adverse tail samples. Problem: protocol semantics alone do not establish a speed gain. Decision: retain explicit selection and `system_native_terminal_retry_reference_cohort_20261013.json`, with no default/application admission. Alternatives: changing both references confounds attribution; treating a transient receiver ACK as a durable commit misstates coverage. Cold, memory, concurrency, durable recovery and full migration gates remain pending. Next verify real selected-authority lost-commit ACK/retry without repeating model/tool effects or replacing the frozen candidate.

**Durable retry-reference confirmation loss:** real SDK/task terminal cases now cover SQLite, current-source TofuDB daemon and its attested same-build embedded library, with full and reference modes. The canonical writer actually commits; Task/Turn/replay readback succeeds before its first successful return is withheld. Host explicitly requests retry, not replay after unknown native transport delivery. Each of six cases retains two model calls, one read-tool call, one projection, two writer calls and one terminal event. Reference mode reuses identical frozen event/receipt byte objects. Business replay content is unchanged; existing checkpoint version/updated/completed bookkeeping may advance on retry. Problem: transient ACK loss did not prove durable effect behavior. Decision: retain the `system_native_durable_retry_reference_20261013.json` and executable storage test, with no default/performance admission. Alternatives: mocking persistence misses commit/readback; asserting whole-row identity confuses bookkeeping with business effects; using an unmatched library splits authority. Verify tests/storage/test_native_terminal_retry_reference.py with explicit TOFU_NATIVE_USAGE_TEST_BINARY and a source-matched TOFUDB_BIN/library pair. Ordinary HTTP ROOT/Flow, paid wallet, concurrent recovery, crash/restart and resource/performance gates remain pending. No live deployment/backend changed.

The installed execution graph uses `lib.agent_core.storage_errors` for typed
port failures and diagnostics. `lib.storage.errors` re-exports these exact
objects for application adapters. Problem: importing the storage facade solely
for error formatting loaded an excluded package in standalone SDK execution.
Decision: keep one standard-library-only contract, enforced by the shared-module
architecture check and installed model/tool smoke. Alternatives: shipping the
storage implementation expands the SDK dependency boundary; duplicate exception
types break catches and error classification.

Event-delivery constants/profiling policy also generate into `lib.agent_core` from `task_event_delivery_v1.json`; storage facades re-export the same values. Request-slot minting no longer initializes the configured operator pool. Its caller already supplies owner-authorized routes; an eventual configured dispatch still initializes and preserves all live request slots, including on failure. Working-fact acquisition imports its storage adapter only after transient and disabled-injection guards. Unbound Evolution tool discovery stays lazy. The opt-in `test_native_model_loop_installed.py` runs two full native SDK rounds from an installed wheel under `-S`, in a fresh directory, verifies every loaded application module belongs to that wheel, and checks joined resource retirement.

## Terminal transcript ownership

**Problem:** terminal task cleanup discarded the storage-free CLI/SDK transcript before trajectory export, leaving no causal call/receipt sequence. **Decision:** transient tasks retain their sole working transcript with their existing invocation owner and bounded retained-run window, alongside tool rounds. Derived caches still retire at settlement; durable conversation attempts still release committed messages. **Alternatives rejected:** reconstruction from display rows loses prompt order and retry provenance; copying a second transcript creates another authority. The controlled CLI SSE test verifies exactly one tool call and receipt through both successful empty-stop recovery and exhausted recovery. <!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->

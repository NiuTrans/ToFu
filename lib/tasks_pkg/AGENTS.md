# Task engine guidance

`context_dependencies.py` binds admitted working-fact context to durable task
dependencies; final request evidence and verified provider return own confirmation.
The domain contract is `docs/contracts/WORKING_FACTS.md`; composer reads stay pure.

## Scope and first reads

This package owns task-turn execution, orchestration of model/tool rounds,
context composition, compaction, event folding, and settlement. Read
`docs/modules/task_engine.md`, `docs/contracts/EVENTS.md`, and
`docs/contracts/TURN_SETTLEMENT.md`.

## Editing rules

- Keep one root run loop and one settlement path. Managers, handlers, and
  compatibility entry points delegate; they do not fork lifecycle state.
  `child_invocation.close_unstarted_child_invocation` rejects unused prepared
  carriers through the existing terminal owner before publication/account close.
  It refuses dispatched tasks; verify prepared graph-child scope/cleanup tests.
- Build model context through `context_composer/` and its explicit budgets.
  Compaction preserves required anchors, tool-call/result pairs, provenance,
  usage accounting, and durable receipts.
- Tool handlers adapt domain tools to the unified gateway. Registration,
  approval, execution, and result settlement retain distinct owners.
  `native_file_evidence.py` binds original batches for ROOT recovery and actor
  preflight; `invocation_effects.py` owns registered-child receipt inspection.
  Its batch read pins a supplied version or an acknowledged source parent and
  rechecks the record after domain queries. Recovered NativeFileToolRecovery uses
  the exact checkpoint storage; verify native Goal file recovery, version races,
  paid/unpaid accounting and real Goal writer SIGKILL before enabling dispatch.
- Persist only through declared task/conversation repository operations after
  the relevant commit boundary. A wake/push hint never substitutes for commit.
- Preserve explicit owner cancellation through model streams, tools,
  subprocesses, child agents, persistence, and final event emission. An
  HTTP/SSE/push observer disconnect never cancels an already-started provider
  dispatch; see `docs/contracts/API_CONTRACT.md` §2.11.
- Bound explicit authorization, retries, context, tool output, server messages,
  and retained stream state. Fault injection and rollback paths remain usable.

Parallel tools and streaming prefetch use the shared demand executor in
`lib/observability.py`. Worker knobs size idle reuse only; prefetch preserves
qualified occurrences and never restores a local count gate.

`requirement_source.py` carries read-only owner Turn/Attempt context through
`child_invocation.py` and frozen assignment reconstruction; final admission
revalidates the source through `acceptance.turns`. No child lifecycle rights.
Verify `tests/artifacts/test_turn_requirements_context.py` and retained authority tests.

Native context admission defaults to NativeContextPolicy and native_context_scope.
native_application binds ordinary ROOT/Flow through explicit application policy before
Attempt binding, borrowing the serving loop/executor/probe policy and issued
selection. Checkpoint factories use the supplied worker after ROOT preparation;
cancellation joins their construction and closes unentered policies.
Explicit flow_configuration/flow_checkpoint_factory separate actor recovery from
ordinary ROOT policy under that same application host. Goal preflight uses the
selected Flow profile; verify native_application_profiles and its installed case.
The optional checkpoint_preparation scope enters before the ordinary factory,
then awaits readiness after its worker returns. NativeGoalHandoff owns whole-set
readiness, Stop subscriptions and joined teardown through prepare_recipients;
it neither starts children nor replaces invocation settlement. Verify
test_native_goal_preparation and actual native Goal handoff under one callback worker.
The child owner's admission_scope defers durable execution admission until that
whole-set ACK; failure precedes native client/model/tool entry. Verify the genuine
HTTP Goal source SIGKILL fixture through accepted takeover and ordinary completion.
Native child model switches use the exact owned catalog and live account actor;
checkpoint_child_routes is checked on the original root, including descendants.
Native terminal settles ExecutionSession resources before commit; failed invariants
persist failure and reject success. Verify native-application HTTP/rollback tests.
Public Stop withdraws cancelled round-end presentation without blocking terminal
ACK; acknowledged native cancellation never re-enters the error finalizer.
Continue fee rows append after the paid prefix while native round identity stays
local. Verify real v4 Stop/Continue/checkpoint_resume across all storage transports.
Resume restores explicit usageComplete=false from the settled projection; paid
counters remain a lower bound and new model dispatch waits for reconciliation.
Verify lost-provider-usage public commands, forged config and joined cleanup.
Native fees retain producer taskId through public carry. A priced wallet resume
validates attribution before process acquisition; failed validation keeps native
billing authority so recovery cannot silently reprice inherited consumption.
Fee checkpoints retain per-round origins separately from checkpoint task identity;
regenerate native_content_contract from the canonical schema. New priced SDK
composition declares executor-origin support before effects; old artifacts refuse.
Actor-local fee rounds also retain original payer roots; sourceRound distinguishes
transferred descendants from inherited local history. Verify three-generation SDK
transport and acknowledged-prefix fee restore. native_goal_handoff.py binds the
whole-account sink to existing registered checkpoint policies; native model
restore verifies acknowledged actor counters without restoring credit twice.
Verify test_native_goal_handoff (unit and loaded C ABI), policy/fee neighbors
and cleanup; pending Goal dispatch requires native_recovery's trusted app policy.
Its optional existing ExecutionJournal binds native commit positions and advances
the shared publication cursor before adoption; verify following ordinary events.
native_tool_projection freezes declared authority and shares orchestration/wire
policy with Python. wire_tools is visibility only; sibling declarations settle
before effects, defer rows until entry, and consume prepared occurrences once.
Native prepared entry projects tools after canonical context injection/admission
on each real/restored round, using the same raw admission tools as Python.
Explicit prepared entry fills missing declared execution contracts through
the canonical adapter, preserving existing authoritative/invalid documents;
gateway-only maps must never suppress a declared tool's argument contract.
Verify native-tool-projection/gateway and actual paired ROOT mixed-order tests.
An absent accounting policy grants no model reservation; explicit preparation
remains caller-owned. Default project policy resolves from the issued route.
Verify generated native tool projection, route-window/output isolation, joined
preparation, unauthorized-summary refusal and installed context application tests.
Use the existing compactor/accounting owners; never create slots or fallback routes.

NativeGatewayProgress owns one current settled batch of hashes; run_native_task
binds it from explicit breaker limits. Verify native-progress/gateway tests and
installed context execution. Match native call counts, withdraw incomplete or
oversized evidence, and release borrowed gateway references on close.
NativeGateway defaults to tool_dispatch.api authorization/withdrawal over one
prepared occurrence and pending receipt. Verify Auto/Manual/denial, owner-bound
confirmation, joined cancel/pressure, unused-grant withdrawal and real native
application tests; policy remains in contracts/TOOL_EXECUTION_POLICY.md.
Project preparation/consumption enters through ConversationStore. The default
adapter owns host project validation; native and Python verified returns share
confirm_consumed_project_context. Verify project-store/native-context tests and
current-coordinator consumption; transient tasks must not activate app storage.
NativeCheckpointPolicy delegates to selected embedded storage, with explicit
financial/time recovery and supplied executor. Verify callback cancellation,
PendingToolRecovery capture/ACK/reconcile order, exact domain metadata and real
Python-writer SIGKILL in test_native_pending_policy; production gateway binding
is explicit via native_file_recovery.py for complete write_file batches. Verify
actual ROOT SIGKILL, domain proofs, spent quota and wallet in
test_native_file_root_recovery.py; mixed/unknown recovery still refuses.
Trusted allow_unreconciled_batches permits live ordinary batches, with exact
ACK binding and unknown recovery refusal; verify production mixed-batch loss.
Verify unknown acquisition, native finish retry and real-library pause/reopen. Release
paused recovery only after NativeTaskTerminalPolicy.accepted; verify actual ROOT
pause/reopen, nullable limits and no tool/credit replay in test_native_root_recovery. Cleanup preserves
primary failure. No Python journal or selected-backend fallback.
Explicit recover_abandoned uses the selected Rust recover port once, with no
create fallback or expiry-based takeover. Verify real ROOT SIGKILL/restart tests;
fresh accepted non-wallet file handoff uses the same journal transaction and
original producer proof. Verify actual v4 Continue and repeated crash; pending
wallet debt remains fenced until source-invoice recovery is admitted.
NativeCheckpointPolicy.handoff_from_accepted reads immutable owned ancestry on
the supplied worker, then asks Rust for atomic handoff. It never falls back to
create/recover or retries ambiguous acquisition. Verify fresh-task wallet handoff.
NativePreparedInvocation adopts the stable epoch issued by a trusted checkpoint
factory only after checking task object, owner and borrowed executor identity.
Verify ordinary HTTP Continue/checkpoint-resume with native journal and wallet.

NativeModelAccountPolicy uses an existing owned tree account and executor.
Native application counter preparation derives omitted encoding/output fields
from the issued logical model/template, preserves explicit bounds, and validates
the generated model_counter schema before launch; never load a Python BPE here.
Enabled unpriced model_control defaults to that policy; missing/foreign accounts
and a second token authority refuse before route consumption.
Verify native_model_control installed parent cap/zero-request refusal, unknown
usage, exact settlement and real native_model_account application persistence.
ModelPolicy owns counted finance; RoundPolicy must not reserve the same account.

SDK native children borrow host/loop/executor, issue their own route using the
supplied repository and preserve tree budget/ingress. Verify actual child usage,
finite unknown-usage refusal and owned cache aliases; priced/durable children
remain a separate migration boundary.

## Verification

Start with the focused task/round/handler/compaction test. Then run event-fold,
turn-settlement, cancellation, and owner-isolation neighbors named in the task
and context domain maps. Use `make test-unit` only after those pass.

NativeModelAccountPolicy optional prepare_pricing follows settled known usage.
Verify resolver failure retains paid accounting, no repeated preparation on
cleanup and exact round cost delivery; resolver/consumer ownership is explicit.

native_pricing owns route-bound price resolution over existing price/FX ports.
Verify native price scope/tenant/absence tests, tier/peak neighbors and installed
route fee delivery; it creates no price table, wallet, refresh or user global.

native_cost_projection owns projection into existing task.apiRounds/cost fields.
Verify unchanged candidate retry, delivery debt, frozen terminal folding and real
Turn readback; no wallet writes, second fee ledger or model replay.

run_native_task optional pricing_resolver composes the existing account and fee
consumer while preserving context preparation/binding/cleanup. Verify duplicate
owner refusal and installed native-cost Turn readback before entry changes.

Explicit transient native terminal/prepared entry reuses storage-free delivery.
Verify real stdio done/error/Stop and lost ACK without storage or model replay;
keep durable lifetime default, reject Turn/wallet/checkpoint/resume authority.
project_native_terminal is the native entry's default task/text/outcome adapter.
Verify actual Turn settlement without manual event commits, malformed result
refusal and streamed thinking retention; the existing writer owns publication.

Native billable terminal projection freezes the existing-wallet settlement before
its retry digest; manager metadata commits the capsule in the ordinary result
CAS. Verify real native publication and fresh-process wallet recovery, no replay.

NativeTaskCostPolicy checkpoint ports reuse canonical native usage and prepare
restoration before account mutation. Verify owned round/route/wallet coverage,
complete account/ordinal identity, installed Suspend/resume and no repricing.

Native completion reuses the final Request Inspector projection after settled
state and round closure, from the existing context route/tools binding. Terminal
projection runs once; commit retry must not repeat snapshots or append history.

Native prepared entry consumes the existing RootLoopRequest and explicit
resource/recovery profile, creates one owned gateway/route, then run_native_task.
Verify native-prepared owner/override/cancel/context/cleanup tests and real three-
protocol RootLoopRequest execution. Never replay preparation or create a client,
pool, store or production fallback here.

`native_http_policy.py` compiles the existing transport owner’s settings for trusted
application composition; verify production HTTP policy, installed v4 and restart tests.

Native resources compile issued launch frame capacity and host transport bounds
before prepared gateway/route acquisition; never use task RSS reserve as a
payload limit or probe per invocation. Verify native-resources overrides/scaling
and real prepared-request tests with missing byte fields.

Native host binds explicit images to application lifespan and transient client
scopes. Production joins it before accounting/event/storage teardown; failed
joining fences dependency shutdown. Verify native-host, app assembly/lifecycle,
server shutdown ordering and real prepared-request tests. Never cache clients
by user or turn scope ownership into another task registry.
Host cleanup carries control failures across Task boundaries as results and
joins accepted siblings after scheduling refusal. Verify test_native_host_close.
Native Flow retirement closes graph admission and joins existing graph borrowers
off-loop before unloading code; verify native Flow lifetime and production Goal
shutdown tests, including a resumed actor waiting on its provider.

NativePreparedInvocation bridges explicit issued task ports from workers into
the existing serving loop and native prepared task. Preserve principal/context,
Stop/join and cancellation type; bounded callback pools cannot own the blocking
worker. Verify native-invocation/root-selection and real application-root tests.
Prepared native catalogs admit canonical decoded_result_schema documentation
while keeping it on the gateway; unknown provider controls still refuse.

`native_parent.py` borrows the canonical live task for detached delegation policy.
Keep exact principal/account/task checks; never serialize ports or read them from
request config. Verify native-parent authority and public production delegation.

Native prepared gateway borrows RootLoopRequest.all_search_results_text for
canonical search classification/budgeting. Terminal projection applies the
existing sources footer once before commit; never scrape arbitrary feedback,
copy the corpus, or mutate raw model history/usage for presentation.

Legacy invoice provenance is recovered only from the checked accepted resume
relation; validate detached exact prefix before assigning it. Keep original quotes
and cumulative consumption. Verify spoof/stale refusal and real legacy v4 wallet
Continue/checkpoint-resume; journal transfer remains independently fenced.

NativeRootConfiguration.worker_reuse is trusted opt-in policy. NativeHost lazily
installs complete-principal/cwd/budget process ownership from issued launch hints;
threaded prepare does no process IO. Verify application worker witnesses and
native host/client/model/terminal/Stop neighbors. Idle reuse never gates demand.

NativeHost optionally owns the pure token-count library under the same admitted
archive pin or separate installed-library pin. Initialization/close share an off-loop lock; new acquisitions refuse
after host close. Verify native_application_adapter and native_host tests before
changing app context propagation or shutdown ordering.
Archive token and Flow factories borrow separate descriptors from native_image's
lazy shared library member. Each ABI still validates/binds and joins its own calls.
Verify native_shared_library and its real installed cases for absent extensions,
concurrent admission, archive removal, binding failure and joined retirement.

Native client scopes carry the issuing token host into NativeContextPolicy;
background route binding/preparation must enter native_token_scope even without
Quart app context. Verify test_native_context background scope and actual ROOT
library-load witnesses; endpoint parity alone does not prove ROOT integration.

The task-owned prepared invocation binds native token scope before initial
context preparation. Gateway approval/execution copy that scope into existing
workers and restore it afterward. Verify invocation owner/fatal exit, gateway
context reuse and real installed HTTP text/tools with Python encoders disabled.
Tasks without a prepared native binding retain their caller's explicit token scope.

`native_production_profile.py` compiles application HTTP/counter/ACK defaults from
existing policy owners; compose_native_application pairs explicit pinned release
with the existing app lifespan. `native_resources.compile_native_transport_budget` also
serves SDK launch. Keep route/account/epoch/checkpoint authority at binding.
Verify production-profile, installed model-control and production crash/readback
tests; no default promotion follows from protocol compatibility alone.

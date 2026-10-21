# Agent core guidance

## Scope

This package defines execution contracts shared by the full application and
`tofu_agent`. Read `docs/modules/task_engine.md` and `docs/contracts/EVENTS.md`.

## Editing rules

- Keep run inputs, outputs, decisions, events, and lifecycle states independent
  of HTTP, persistence backends, and UI projections.
- Event names and payloads come from the canonical event contract. Producers
  emit committed facts; consumers may project them but cannot reinterpret
  settlement.
- Preserve explicit task/run/attempt identity, cancellation, terminal-state
  uniqueness, usage accounting, and typed failure attribution.
- Inject provider, tool, context, clock, and event sinks through declared ports.
  Do not read module-global user/session state.
- `execution_budget.py` owns shared input/output reservations and per-attempt
  settlement ports. Unknown provider usage cannot refund finite authorization. Optional child membership
  observations gate attachment; failed detachment observation still releases membership.
  Optional tool executor observations retain pending state until a return/error observation.
  Optional reserved-slot observations include unentered tools; aggregate headroom
  may narrow output before the final durable dispatch fence. `narrow_limits`
  composes only pristine accounts. Verify `test_composed_budget_limits.py`.
- `task_runtime.py` owns the optional terminal_persist durability port;
  `docs/DEFENSIVE_PATTERNS.md` and `test_research_action_terminal.py` own its
  settlement-before-visibility and pending-repair verification path.
- Keep interfaces usable by transient and durable compositions without making
  the transient runtime depend on repositories.

`progress_ledger.py` owns bounded progress checkpoint serialization; the
shared `lib/agent_loop/agent_loop.py` completed-round restoration ports are documented in
`docs/RUN_CONTINUITY.md` and verified by `test_agent_loop_recovery.py`.

progress_evidence.py owns normalized operation/result hashes, counted result
evidence and nonretryable-operation hashes shared by the Python runner and native
gateway. Verify result-evidence, native-progress, root
adapter, breaker and checkpoint neighbors; it grants no execution authority.
Owned execution status precedes typed-projection classification; successful
source examples cannot become terminal failures. Verify progress-outcome authority.
execution_deadline_unix_ms reads the earliest validated task/shared-tree expiry
without creating a ledger; verify parent/child, zero, invalid and integer bounds.
Native run binding can shorten this original deadline, never extend it.

native_model_control owns counted SDK reserve/dispatch/settle over one reader.
Verify joined cancel/timeout, expired ACK suppression, frozen usage retry and
process-loss cleanup after native join; callbacks cannot create another account.
ExecutionBudget.authorizes_model_owner checks the actual root/actor identities.

native_tool_control owns active request-bound callbacks on that same reader.
Verify native_tool_control and installed native_tools_parallel tests for reverse
replies, identical occurrences, sibling failure, repeated Stop and joined reuse.
model_call_id is correlation only; call_id remains approval authority. A callback
cannot close its own client; transport errors must still join other callbacks.

## Verification

Run `pytest -q tests/agent_core/test_agent_core_boundary.py` plus focused run-contract and
event tests. Add task-engine integration tests when a shared lifecycle surface
changes.

`ExecutionBudget.restore_model_account` restores model totals/attempt ordinals
into pristine accounts once; it grants no execution authority. Verify
`test_model_account_restore.py` before changing recovered admission semantics.
`budget_snapshot.py` validates cumulative Flow receipt observations;
`account_frontier.py` freezes whole budget cuts at effect/membership boundaries;
its packaged schema derives from `gen_execution_account_frontier.py`. Verify
`test_account_frontier.py`, concurrent debt and failed-durability admission fences.
`restore_replay_snapshot` restores spent authorization after caller-owned lineage
validation. Verify `test_budget_snapshot.py`: parallel/multiple-generation totals,
unknown reservations, pristine ownership and retries that cannot rewind spending.
`account_recovery.py` moves paid actor prefixes inside that same account without
adding global consumption. Unbound debt stays explicit in frontier observations;
verify `test_recovered_actor_prefix.py`, failed ACKs and partial-rebind SIGKILL.
account_recovery_batch.py moves the full actor set through one prebound durable
sink; verify test_recovered_actor_batch for exact retries, byte exhaustion,
reentrant admission and unknown ACKs. No lease is exposed before sink success.
validate_actor_recipients reuses that transfer preflight under the budget lock;
it observes the complete pristine set without moving prefixes or publishing.
The prebound atomic sink also fences root/child model and tool admission while
any original actor remains pending; child preparation and cleanup stay available.
verify_recovered_actor_account checks an acknowledged prefix without moving credit;
verify native Goal handoff/model restore tests for changed state and failed ACKs.
Recovered actor tool reconciliation compares the original prefix, preserves prepaid
calls and publishes tools_reconciled only for proven unpaid calls. Verify
test_recovered_actor_tools; native ModelControl passes the loaded checkpoint to
restore policy so pending operation evidence remains available without mutation.
These ports require caller-owned accepted lineage and grant no actor/wallet rights.
`recovered_actor_prefix_tokens` exposes only the acknowledged original actor
spend to external fee policy; clones, foreign owners and failed frontier ACKs
refuse. Verify recovered actor fee transfer alongside the account recovery tests.

`native_process.py` is the internal owned native host transport, not model-routing
or billing authority. Verify native-process unit tests and the explicit archive
probe; preserve principal binding, sealed image lifetime, demand run ownership,
callback quiescence, cancellation-safe spawn and joined process close.

Optional RoundPolicy gates every native HTTP round and acknowledges raw evidence;
verify joined cancellation, exactly-once settlement attempts, ACK bounds and
NativeRoundError cause preservation. Durable accounting policy remains external.
Explicit pending-tool checkpoint callbacks restore paid fees before domain
queries and reuse progress/completion before new reservations. Verify exact
observation order, sticky refusal, joined cancellation and priced installed
pending recovery; never replay tools or reserve the restored provider round.

Optional per-round history preparation uses the existing reservation channel.
wire_tool_preparation returns RoundPreparation with messages and visible tools
in the same certified frame before reservation; reject missing/competing fields.
It changes no execution catalog. Verify transport byte fences and joined Stop.
Verify exact prepared history, NotDispatched on retained-byte refusal, callback
fault persistence/cause and joined cancellation. Large exclusive frames must
finish ordinary Stop publication under the original deadline; never add a reader
or mirror history. Installed preparation tests preserve tool/reasoning replay.

Prepared round requests inherit one CLOCK_MONOTONIC deadline per run. Verify
fixed deadline across rounds, queued/running worker timeout joining, no obsolete
decision after expiry and installed interrupted terminal persistence before ACK.

TerminalPolicy acknowledges frozen native candidates through an explicit durable
port. Verify candidate equality, callback/ACK joining, failure attribution and
explicit retries; never infer persistence from process exit or rerun models.

`worker_executor.py` injects app observations into the standard-library-only
`tofu_workers.executor` lifecycle. Shared ownership rules are verified by worker
executor and server-loop tests; standalone consumers must not import app state.

`push_frames.py` binds Push launch hints to the shared immutable JSON file
owner in `tofu_workers/json_frames.py`, including joined worker close. `push.py` owns authenticated routing,
FIFO references and priority control lanes. Verify `test_push_frame_spool.py`
and the server Push lifecycle/socket tests; no temporary range becomes durable
replay authority or a count/byte admission quota.

native_image owns one explicit loop-bound pinned archive or installed executable
image across principals. Installed images retain complete runtime self checks;
verify installed-image and executable-install tests before changing that boundary.
Clients duplicate sealed descriptors, retain independent identity/run/close and
check their binary bound. Shared close joins acquisition without revoking borrowed
FDs; verify native-shared-image, archive-installed and prepared application tests.

native_artifact owns shared bounded archive inspection and sealed self checks.
The generated artifact contract is authoritative; verify loader/release/installed
fixtures and package membership. from_archive reuses native_process image/close
ownership; never add an install tree, second image owner or automatic download.

abort_signal owns task Event-compatible loop notification, with disposable
subscriptions and no polling workers. Verify set/subscribe races, cross-loop
wakes, native initial Stop, silent HTTP settlement and canonical terminal ACK.

ModelPricing is a bounded settle-only snapshot; generated model_price is its
shape authority. Verify priced installed delivery, prompt/known-usage refusal,
joined callback faults and absent default cost allocation; it grants no wallet.

Priced shared checkpoints require explicit fee ports and emit the generated
checkpoint_fee_projection and checkpoint_fee_executor_origins profiles. Verify pre-admission old-artifact refusal,
identity before restore and future-load byte admission before durable completion.

native_worker_pool.py owns scoped persistent process views and their physical
stdout pump; it never owns task/account/routing decisions. Verify pool scope,
startup/close/failure tests plus installed callback/Stop before changing reuse.
Invocation EOF means joined readiness, not physical process exit; host teardown
must close/join cached workers before releasing the shared image.

native_process._encode mints immutable object-frame bytes only after bounded
JSON/UTF-8 encoding. The worker adapter may envelope these bytes directly;
first-frame identity and original byte limits still apply. Ordinary bytes keep
full decoding/encoding. Verify native_worker_frames plus pool/process tests.

Native worker release validates the exact current Invocation before changing
physical worker ownership. Retired or duplicate releases cannot discard an idle
worker or a later invocation; verify `test_native_worker_pool.py`.

Invocation.kill also requires current invocation identity before physical kill;
verify stale idle/active and current kill/reap in `test_native_worker_pool.py`.

Optional token-count archive members share native_artifact inspection. Verify
release archive extension tests and explicit sealed release-library parity;
legacy runtime archives remain valid without the extension. No library load
or execution occurs during seal_token_count_archive inspection.
`lib/native_binary/__init__.py` owns the exact regular-file/digest/sealed-image boundary
and shared transport errors. native_binary and native_process retain imports;
installed HTML admission need not import task execution or archive inspection.
Verify native_executable_source, archive-loader and actual HTML binding tests.
The optional borrowed generated FFI keeps the same Linux memfd ABI and sealing
checks; native_memfd_cffi tests own fallback cleanup and control failures.

native_token_count owns explicit pinned pure Python/C bindings; constants derive
from gen_native_token_count_abi.py. Verify binding race/fault tests plus actual
release archive parity and repeated unload/reload. Never unload code before all
Python call leases and native handles join, or silently fallback after ABI faults.

Selected terminal usage summaries derive from the stdio schema and use the
shared validate_usage_summary boundary. Freeze selection/bytes with the candidate;
verify native-process retry and actual terminal-usage SDK integration tests.

An installed image may carry a separate pure token-library path and pin; Flow
still requires archive authority. Verify token-library installation/refusal,
sealed-library binding faults and repeated load/close with both encodings.

Pending file reconciliation adds only proven spent tool counts to the exact
restored shared account. Unknown batches mark consumption incomplete; callable
tool observers require their own receipt policy. Verify shared-account checkpoint
refund/cap/identity cases and actual native file ROOT recovery.

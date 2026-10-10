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
- `task_runtime.py` owns the optional terminal_persist durability port;
  `docs/DEFENSIVE_PATTERNS.md` and `test_research_action_terminal.py` own its
  settlement-before-visibility and pending-repair verification path.
- Keep interfaces usable by transient and durable compositions without making
  the transient runtime depend on repositories.

`progress_ledger.py` owns bounded progress checkpoint serialization; the
shared `lib/agent_loop.py` completed-round restoration ports are documented in
`docs/RUN_CONTINUITY.md` and verified by `test_agent_loop_recovery.py`.

progress_evidence.py owns pure visible-result and nonretryable-operation hashes
shared by the Python runner and native gateway. Verify native-progress, root
adapter, breaker and checkpoint neighbors; it grants no execution authority.
execution_deadline_unix_ms reads the earliest validated task/shared-tree expiry
without creating a ledger; verify parent/child, zero, invalid and integer bounds.
Native run binding can shorten this original deadline, never extend it.

native_model_control owns counted SDK reserve/dispatch/settle over one reader.
Verify joined cancel/timeout, expired ACK suppression, frozen usage retry and
process-loss cleanup after native join; callbacks cannot create another account.
ExecutionBudget.authorizes_model_owner checks the actual root/actor identities.

## Verification

Run `pytest -q tests/agent_core/test_agent_core_boundary.py` plus focused run-contract and
event tests. Add task-engine integration tests when a shared lifecycle surface
changes.

`ExecutionBudget.restore_model_account` restores model totals/attempt ordinals
into pristine accounts once; it grants no execution authority. Verify
`test_model_account_restore.py` before changing recovered admission semantics.

`native_process.py` is the internal owned native host transport, not model-routing
or billing authority. Verify native-process unit tests and the explicit archive
probe; preserve principal binding, sealed image lifetime, demand run ownership,
callback quiescence, cancellation-safe spawn and joined process close.

Optional RoundPolicy gates every native HTTP round and acknowledges raw evidence;
verify joined cancellation, exactly-once settlement attempts, ACK bounds and
NativeRoundError cause preservation. Durable accounting policy remains external.

Optional per-round history preparation uses the existing reservation channel.
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

`push_frames.py` owns lazy demand encoding, shared immutable temporary-file
ranges and joined transport-worker close. `push.py` owns authenticated routing,
FIFO references and priority control lanes. Verify `test_push_frame_spool.py`
and the server Push lifecycle/socket tests; no temporary range becomes durable
replay authority or a count/byte admission quota.

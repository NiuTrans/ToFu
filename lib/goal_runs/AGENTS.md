# Chat Goal lifecycle and context

Read `docs/RUN_CONTINUITY.md` and `docs/modules/orchestration_dag.md`.

- `contract.py` owns GoalRun policy/status vocabulary, launch archive schema
  and the permitted worker-state keys. `service.py` owns lifecycle transitions;
  `repository.py` is the owner-bound storage port.
- `worker_context.py` owns the model working set. `context_recovery.py` uses
  the canonical settled-lane cache; the cache never authorizes effect replay.
- `fee_account.py` freezes trusted launch cost/wallet mode; legacy recovery remains
  wallet-only. Verify test_goal_fee_account and full test_native_production_goal.
- `execution_journal.py` freezes chat launches on the existing orchestration
  event stream; `journal_codec.py` owns lossless bounded packets.
  `journal_publication.py` owns its ordinary/native sequence cursor under the
  shared ExecutionJournal lock. Verify test_goal_native_journal plus real C ABI
  handoff followed by ordinary cuts/effects; unknown ACKs never advance/retry.
  `account_journal.py` binds whole shared-budget cuts to that same launch/stream;
  it restores spent prefixes after accepted lineage, never unresolved actor rights.
  `pending_recovery.py` joins typed pending admissions to exact whole-account and
  native actor evidence. Pending seeds require accepted native composition.
  `prepared_recovery.py` joins complete consumed reservations without changing
  preparation into execution. Native claim rechecks the protected header witness;
  verify test_goal_prepared_recovery and real test_goal_native_handoff_sigkill.
  `pending_fee_evidence.py` joins exact private completions and pending quotes to
  that cut; its invoice candidate grants no wallet settlement or actor takeover.
  `pending_claim.py` binds accepted continuation, physical journal positions and
  original fees to one native claim. Only its checked receipt admits pending
  journal preparation with the caller-owned NativeGoalHandoff. Verify
  test_goal_pending_claim and real test_goal_pending_claim_sigkill; lost ACKs
  never retry. `native_recovery.py` composes explicitly configured native Goal
  recovery through the ordinary chat runner; unsupported pending evidence refuses. Recovery I/O bounds come from the selected storage client or an explicit override, never nullable terminal ACK grace; preflight and execution borrow the same resolved storage.
  Verify test_goal_production_recovery with real SIGKILL and selected storage.
  `GoalRecoverySeed.inspect_pending_inputs` applies archived launch limits to
  the common read-only Flow inspector; it grants no child or native capability.
  `lib/orchestration/execution_journal.py` owns effect receipts
  and ambiguity. Replay restores worker inputs, never child lifecycle handles.
- Launch archives use the public child-invocation input-policy projection;
  successful admission binds that immutable policy to subsequent Goal leaves.
  Resource/principal rebind remains mandatory after restart.
- A stored archive is evidence, not admission. Recovery must also bind the
  conversation/attempt and GoalRun lifecycle; no standalone transient fallback.
- `recovery.py` rebinds restart-interrupted Goals through idempotent ordinary
  conversation commands; the old terminal run remains immutable. Source and
  successor attempt bindings, lane-tail/Stop/queue guards precede effect replay.
  Its optional synchronous `before_submit` port acknowledges the common boot
  audit after preflight and before normal command submission; do not move it
  after dispatch or weaken the native claim's exact source cursor.
- Ready-journal publication acknowledges a recovery source before effects;
  unfinished handoff admission proves private accepted config and continuation
  ancestry. Task birth alone never closes election.
- `continuation.py` creates an explicit normal conversation command. Control
  markers and browser gestures do not become a second Goal state machine.

Verify launch/effect work with `tests/goals/test_goal_execution_journal.py`,
`test_goal_journal_retry.py` across semantic authorities,
then `test_goal_worker_context.py` and
`tests/orchestration/test_orchestration_execution_recovery.py`. Lifecycle changes
also run `test_goal_run_service.py` and `test_goal_run_sidecar.py`.
Recovery also runs `test_goal_conversation_recovery.py` and
`test_goal_handoff_recovery.py` across semantic authorities,
plus the continuity, conversation-command and parent-settlement tests.

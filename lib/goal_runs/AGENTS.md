# Chat Goal lifecycle and context

Read `docs/RUN_CONTINUITY.md` and `docs/modules/orchestration_dag.md`.

- `contract.py` owns GoalRun policy/status vocabulary, launch archive schema
  and the permitted worker-state keys. `service.py` owns lifecycle transitions;
  `repository.py` is the owner-bound storage port.
- `worker_context.py` owns the model working set. `context_recovery.py` uses
  the canonical settled-lane cache; the cache never authorizes effect replay.
- `execution_journal.py` freezes chat launches on the existing orchestration
  event stream; `journal_codec.py` owns lossless bounded packets.
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

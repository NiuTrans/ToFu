# Automation guidance

This domain owns versioned automation definitions, trigger evidence, and durable
step transitions. Start at `docs/modules/scheduling_ops.md` and the executable
`contracts/automation_v1.schema.json` contract.

- Definitions are data: no Python, shell, JavaScript, or arbitrary expression
  evaluator enters the condition language. Actions are explicitly registered.
- Repositories require an owner on every operation. One occurrence carries its
  frozen definition and evidence; editing a rule cannot rewrite an active run.
- Published revisions, event subscriptions and canonical clock jobs mutate only
  through `automation.definition.update`; generic definition writes are drafts.
  Calendar plans live in `lib/scheduler/calendar.py`; clock producer admission
  proves the existing queue's live lease rather than adding a timer authority.
  `clock.py` selects UTC slots and returns canonical suspension. `runtime.py`
  installs clock/event/run handlers for worker/all roles; `worker_service.py`
  owns lazy launch-probed concurrency and shutdown before storage closure.
  `contracts/automation_runtime_v1.json` owns timings; no per-rule resident timer.
  Publication deletes obsolete pure clock transport, retaining accepted runs and
  the latest bounded failure. Generic enqueue rejects reserved automation kinds.
- `events.py` owns frozen task-start envelopes and bounded subscription prefixes;
  `turn.attempt.start` atomically produces subscribed transport at physical entry.
  `event_delivery.py` checkpoints bounded indexed fanout in that transport;
  typed completion settles and retires it after the heartbeat joins, preserving
  accepted runs and owned failure diagnostics. Native recovery is primary evidence.
  `presentation.py` formats frozen time; `presentation_index.py` defines owned
  references committed by run advance. `presentation_query.py` reads bounded
  per-attempt pages; output stays in the run and never enters model context.
- Persist an effect intent before dispatch. An unknown external result requires
  reconciliation, never an automatic replay of a non-idempotent action.
  `execution.py` binds frozen action versions to explicit ports and persists each
  transition through the live worker fence. Manual-policy external actions
  suspend before intent; `approval.py` owns immutable reviewed-byte proofs.
  `automation.run.approve` commits the owned approval and canonical wake together;
  claim wake observations stay fixed across heartbeats and protect later deferral.
- Agent and Flow actions use the existing invocation and conversation owners.
  This package must not add a model loop or a second conversation projection.
- Verify contracts, owner isolation, concurrent CAS, lost acknowledgements,
  recovery, and cancellation in `tests/automations/` before UI integration.
- `management.py` owns public DTOs and manual request identity; HTTP handlers
  consume `automation_management_v1.schema.json`, never private record envelopes.

- `projects.py` owns durable immutable-root automation scope bindings and task
  capture. Registration is metadata, never filesystem write authority. Native/SQL
  publication and first admission prove an active binding inside their transaction.

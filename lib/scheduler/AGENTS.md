# Scheduler guidance

## Scope

This package owns timer definitions, polling/claim policy, and bounded scheduled
execution. Read `docs/modules/scheduling_ops.md` and the process-role table in
`lib/process_roles.py`.

## Editing rules

- Timer commands and durable states are owner-scoped semantic storage
  operations. Parse schedules once into an explicit, versioned representation.
- `schedule_create` fires a clock payload (cron/`once:`). `timer_create` is a
  single-shot readiness watcher that injects a continuation. Do not merge the
  schemas or treat timer as a countdown reminder.
- Claiming is atomic and lease-based. Multiple workers, restart, timeout, and
  lost acknowledgement must not duplicate externally visible effects.
- Keep polling, claim, execution, outcome, retry/backoff, and reschedule as
  explicit transitions with observable failure reasons.
- Automation UTC calendar queries live in `calendar.py`; their policy is
  `contracts/automation_v1.schema.json` and their gate is
  `tests/scheduler/test_automation_calendar.py`.
- A new scheduled action delegates to its application service; the scheduler
  does not copy domain policy or bypass authorization.
- Timer waiting uses `timer/_runtime.py`; blocking polls use demand workers.
  Resource hints never refuse independent accepted work. Retire transport state
  after settlement; preserve declared poll budgets, leases and durable evidence.
- Cancellation/shutdown stops new claims, settles or releases owned work, and
  leaves recoverable durable state.

## Verification

Run timer parse/poll/resume/dispatch tests and scheduler process-runner tests.
Add Sidecar, owner-isolation, multi-worker, restart, and action-domain tests for
the affected transition.

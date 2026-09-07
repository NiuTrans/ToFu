# Scheduler guidance

## Scope

This package owns timer definitions, polling/claim policy, and bounded scheduled
execution. Read `docs/modules/scheduling_ops.md` and the process-role table in
`lib/process_roles.py`.

## Editing rules

- Timer commands and durable states are owner-scoped semantic storage
  operations. Parse schedules once into an explicit, versioned representation.
- Claiming is atomic and lease-based. Multiple workers, restart, timeout, and
  lost acknowledgement must not duplicate externally visible effects.
- Keep polling, claim, execution, outcome, retry/backoff, and reschedule as
  explicit transitions with observable failure reasons.
- A new scheduled action delegates to its application service; the scheduler
  does not copy domain policy or bypass authorization.
- Bound polling batches, workers, leases, retries, catch-up work, history, and
  retained results according to the runtime resource profile.
- Cancellation/shutdown stops new claims, settles or releases owned work, and
  leaves recoverable durable state.

## Verification

Run timer parse/poll/resume/dispatch tests and scheduler process-runner tests.
Add Sidecar, owner-isolation, multi-worker, restart, and action-domain tests for
the affected transition.

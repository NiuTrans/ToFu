# Scheduling and autonomous operations

This domain owns timers, scheduled conversation work, proactive dispatch,
daily reports, and bounded optimization. These paths can act without a user
click, so authority, idempotency, and blast-radius controls are part of the
domain contract.

## Ownership

| Concern | Owner |
|---|---|
| Timer contract and CRUD | `lib/scheduler/contract.py`, `timer/_crud.py` |
| Timer polling/notification | `lib/scheduler/timer/` |
| Scheduled task execution | `lib/scheduler/executor/` |
| Conversation dispatch | `lib/scheduler/conversation_dispatch.py` |
| Proactive decisions | `lib/scheduler/proactive.py` |
| Process runner | `lib/scheduler/process_runner.py` |
| Daily report | `lib/daily_report/` |
| Optimizer analysis/proposals | `lib/optimizer/analyzer/`, `proposer.py` |
| Optimizer application policy | `lib/optimizer/applier.py`, `actions/` |
| Durable state | owner-scoped Sidecar scheduler/optimizer operations |
| HTTP adapters | `routes/api_v1/scheduler.py`, `daily_report.py`, `optimizer.py` |

## Timer lifecycle

1. An authenticated owner creates a timer with a validated schedule and action.
2. The Sidecar persists identity, owner, next due time, and revision.
3. Polling claims due work idempotently.
4. The executor invokes a declared action with bounded context and runtime.
5. Outcome is recorded before the next schedule is advanced.
6. Notification/push is a projection of the recorded outcome.

Restart must not lose a durable timer or execute one due occurrence twice.
Process-local wakeups are hints; the Sidecar row is authoritative. Parse or
timezone failures suspend/fail visibly instead of entering a tight poll loop.

## Autonomous authority

Scheduled and proactive work acts only for the owner recorded on the durable
row. It receives no ambient administrator capability. The action catalogue is
an allow-list with explicit input limits; arbitrary code, routes, or tool names
cannot be smuggled through timer payloads.

The scheduler worker itself starts only with an explicit system principal
carrying `scheduler:run`. Personal composition binds that principal to the
personal owner and may install the four personal built-ins plus run the
project/peer sweeps. Distributed composition is ownerless: it still claims
durable task rows and rebuilds a least-privilege principal from each row's
owner, but it does not invent owner 1, install personal built-ins, or perform
global project/peer scans.

The billing-reserve built-in is the sole periodic recovery owner and is policy
reconciled disabled unless multi-user relay billing is active. The scheduler's
durable claim prevents duplicate sweeps; request-worker startup creates no
parallel janitor. Daily-report backfill also uses this worker: it runs every
six hours, queues a startup hint only when yesterday is absent, and has no
second six-hour sleeper. Optimizer and backup built-ins preserve
user-controlled enablement during definition reconciliation.

Personal built-ins have an owner-scoped `system_key`; display names are not
their authority. During reconciliation, an exact-name-and-type row created
before that key existed is adopted in the same locked Sidecar transaction. If
a newer keyed duplicate already exists, the oldest legacy identity is kept and
the reconstructible duplicate is retired before the definition is refreshed,
so one due interval can produce only one built-in execution.

An LLM poll materializes one exact tool-schema epoch and compiles its
`ToolContractV2` execution documents before dispatch. The owner identity and
the same documents reach the shared executor. A missing, malformed, filtered,
or argument-invalid call is returned as a typed non-execution; contract
compilation failure disables tools for that poll instead of falling back to an
ambient registry.

Optimizer analysis is read-only. Proposals are durable, inspectable facts.
Automatic application is limited to registered reversible actions with a
bounded write set and verification. Unknown or high-impact proposals require
human approval and never fall through to generic execution.

Every optimizer run receives a principal with `optimizer:maintain` (an admin
request satisfies that scope) and a positive owner. Sidecar proposal/action
identities, joins, status changes, expiry scans, and indexes are keyed by
`(user_id, id)`. Schema v37 assigns historical personal rows to owner 1 only
during migration; new operation payloads have no owner default. In distributed
mode structured audit evidence is filtered to the run owner and unowned raw
application/error logs are excluded. The current file-backed
`block_search_domain` action is personal-only, so distributed runs stage it for
review instead of mutating deployment-global configuration.

## Daily report

Daily-report readers gather owner-scoped conversation/todo/activity state,
compute bounded aggregates, invoke the shared LLM path when needed, and commit
one idempotent report identity for the period. Generation failures do not erase
the last complete report. UI/API views read the stored report rather than
recomputing it per request. The built-in reconstructs `reports:maintain` from
the durable task owner; ownerless distributed composition installs none.

## Failure semantics

- Invalid schedule/action: reject at creation/update.
- Claim conflict: another worker owns the occurrence; do not execute.
- Action failure: record typed terminal outcome and apply bounded retry policy.
- Process crash: lease/claim expires and a later poll safely resumes.
- Owner/resource missing: terminal non-retryable outcome unless the contract
  declares temporary unavailability.
- Optimizer verification failure: roll back/reject the action and retain the
  proposal plus diagnostics.

## Invariants

- Sidecar state is authoritative; polling memory is a cache.
- Timer, occurrence, and report identities are idempotent.
- Owner/tenant identity is explicit from storage through execution.
- Missing owner or scope fails before a scheduler/optimizer storage call.
- Only registered actions can run autonomously.
- Claims/leases have bounded lifetimes and recovery semantics.
- Retry loops are capped and do not duplicate completed side effects.
- Optimizer writes are allow-listed, reversible, and post-verified.
- Notifications reflect stored outcomes; they do not define completion.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Timer field/schedule | scheduler contract and timer CRUD | parser, Sidecar, resume |
| Poll/claim behavior | timer poll/state modules | multi-worker/idempotency |
| New scheduled action | action catalogue + focused service | owner, input bounds, retry |
| Daily report input | focused reader | owner isolation, period idempotency |
| Optimizer signal | analyzer | proposal determinism |
| Auto-applied action | `actions/`, applier | write set, rollback, verification |

## Test map

```bash
pytest -q tests/test_timer_parse_failure.py \
  tests/test_timer_poll_agent_loop.py tests/test_timer_resume_guardrails.py
pytest -q tests/test_timer_dispatch_outcomes.py \
  tests/test_scheduler_process_runner.py
pytest -q tests/test_daily_report.py tests/test_daily_report_storage.py
pytest -q tests/test_optimizer.py tests/test_optimizer_sidecar_storage.py
```

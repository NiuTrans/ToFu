# Scheduling and autonomous operations

This domain owns timers, scheduled conversation work, proactive dispatch,
daily reports, and bounded optimization. These paths can act without a user
click, so authority, idempotency, and blast-radius controls are contractual.

## Ownership

| Concern | Owner |
|---|---|
| Automation admission, occurrence/step records and worker composition | `lib/automations/`, `runtime.py`, [automation contract](../AUTOMATIONS.md) |
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
| HTTP adapters | `routes/api_v1/automations.py`, `scheduler.py`, `daily_report.py`, `optimizer.py` |
| Browser My Day TODO/stream mutation policy | `frontend/src/features/myday/task-actions.ts` |
| Browser My Day suggestion launcher | `frontend/src/features/myday/quick-action-launcher.ts` |
| Browser My Day owner-scoped read cache | `frontend/src/features/myday/report-cache.ts` |
| Browser My Day digest/reminder lifecycle | `frontend/src/features/myday/background-controller.ts`, composed by `features/background.ts` |
| Browser My Day panel presentation | `frontend/src/runtime/sections/myday.js` (migration boundary) |

Scheduled subprocess output is drained in 8 KiB nonblocking reads, retaining at most 50,000 stdout and 10,000 stderr characters before accumulation. Continuous output still observes cancellation and the deadline, without extra reader threads. POSIX timeout/cancellation terminates the process group even after its leader exits, grants at most two seconds for graceful exit, then kills surviving descendants. `tests/scheduler/test_scheduler_process_runner.py` measures parent heap under a 16 MiB dual-stream flood (<2 MiB peak).

Owned task polling uses one terminal snapshot renderer in `lib/scheduler/executor/_await.py`: all status/wait paths return complete terminal evidence, errors/aborts remain partial output, and unavailable or nonterminal states never imply success. Waits require a handle, respect parent cancellation, return early on completion, and are bounded to 60 seconds; no-ID polling is a dashboard. This replaces length-only/tail-only results that contradicted the tool promise. Executable contract: `tests/scheduler/test_poll_task_results.py`.

## Timer lifecycle

1. An authenticated owner creates a watcher with a validated readiness check and continuation.
2. The Sidecar persists identity, owner, next due time, and revision.
3. Polling claims due work idempotently.
4. The executor invokes a declared action with bounded context and runtime.
5. Outcome is recorded before the next schedule is advanced.
6. Notification/push is a projection of the recorded outcome.

Restart must neither lose a durable timer nor execute one occurrence twice; process-local wakeups are hints, the Sidecar row is authoritative, and parse/timezone failures suspend visibly instead of entering a tight poll loop.

Timer Watchers register every accepted live watcher without process/owner count
admission quotas. `TOFU_TIMER_LIVE_CAP` and `TOFU_TIMER_RESUME_CAP` are retired.
Startup recovery uses the internal active feed with limit=0 (all rows through
native index pages) and starts every eligible definition; declared positive
query limits still select bounded projections. Cross-replica leases and local
duplicate registration retain single ownership. Actual thread-start failure
rolls back local registration/lease and reports startup failure; tool creation
keeps its existing durable cancellation rollback. Native total/active counts
remain exact integrity observations and migration no longer refuses definition
513. Waiting watchers share one lazy event loop; blocking storage/model calls
use demand workers with no active ceiling or retained idle workers. Cancel wakes
waits. Shutdown joins physical calls and releases leases before task quiescence,
HTTP/runtime-store closure; failed joins keep dependencies live. Gates include
`tests/scheduler/test_timer_wait_runtime.py` (32 blocked peers plus healthy work,
stop/cancellation drain) and 512 waiting watchers sharing one thread.

Clock jobs use `schedule_create` (cron/`once:`); readiness waiters use `timer_create` (single-shot continuation, no cron). Schemas remain within 600/450/1000 combined tokens. Timer creation is resident/direct-call under routed exposure, Tool Search and programmatic compaction; other scheduler tools remain searchable. SQL/native admission accepts predicate-only watchers, and native `max_polls=0` preserves unlimited checks. Human-only restart/redeploy waits remain forbidden.

**Problem:** equal command output can hide model recovery or semantic changes; negative agreement does not validate a future positive edge. **Decision:** predicates use zero LLM calls; hybrid/semantic watchers retain verification and require JSON booleans. Cancellation fences model/tool boundaries; the shared no-progress guard stops repeated tool/result loops. **Alternatives rejected:** permanent output caching and promotion after three negative agreements can miss or falsely accept readiness; code-only errors never acquire an ungrounded model fallback.

Committed `ready` is a dispatch obligation: busy/unavailable lanes retry without new checks, poll exhaustion or resume age expiry. Exact conversation command identity deduplicates continuation. Cancelled/foreign-owner calls have no local watcher authority; exceptional loop exit releases the registry/lease for resume. Commands use the shared bounded process runner, explicit owner/project directory; regex readiness requires successful stdout. The 16 MiB flood test retains <2 MiB parent heap and at most 3,500 stdout + 500 stderr characters. No resident worker is added; default 120 polls bound ledger text to about 0.8 MiB, while explicitly unlimited watchers retain growing audit history. Gates: `tests/scheduler/test_timer_readiness_reliability.py`, SQL storage contract and native semantic timer tests.

Conversation-dispatched runs build their task config through
`build_task_config` in `lib/scheduler/_shared.py`: the timer's `tools_config` wins on
conflict, otherwise conversation settings carry through — including the
translation pair (`autoTranslate`, `uiLang`) that every translation trigger
resolves from `task['config']` alone. A scheduled turn therefore translates
exactly like an interactive turn of the same conversation.

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
The personal backup's finite `max_runtime` derives from the launch-probed
recovery-copy budget (30 minutes..6 hours by default, explicit ceiling 24
hours). If retained recovery points make only the transient peak exceed the
copy budget, a fastpath run may hard-link and fully publish the new point
before budget-retiring older verified backups; it never rotates the unique
deep-clean rollback or falls back to a full copy for that exception.

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

One proactive status snapshot feeds both LLM dispatch and audit; its two-message
tail and scheduler/project settings/existence probes never hydrate full transcripts; frozen legacy suffix scans have a 128 KiB work budget before authoritative full decode.

Optimizer analysis is read-only. One run snapshots each eligible bounded log tail once; distributed mode still excludes raw application/error lines and audit rows remain owner-filtered. Each audit/error snapshot streams once into all projections without retaining parsed entries; application lines without a possible signal skip timestamp/regex work, with an 8,192-character fast-path ceiling.
All block-domain post-apply metrics share one application-log pass. Transcript-derived signals select owner-scoped metadata, then use bounded repository hydration; lazy frame failure drops the
whole best-effort signal. Proposals are durable facts. Automatic application is
limited to reversible actions with a bounded verified write set. High-impact or
unknown proposals require human approval and never fall through to execution.
The optional LLM proposer uses request-local strict billing-stop admission: a
recorded 402/quota stop cannot be retried through a stale manual ON or direct
fallback. It also shares the finite optional-enrichment 429 allowance (personal
2, distributed 8, hard ceiling 16); failure leaves the evidence intact with
zero proposals.

Every optimizer run receives a principal with `optimizer:maintain` (an admin
request satisfies that scope) and a positive owner. Sidecar proposal/action
identities, joins, status changes, expiry scans, and indexes are keyed by
`(user_id, id)`. Schema v37 assigns historical personal rows to owner 1 only
during migration; new operation payloads have no owner default. In distributed
mode structured audit evidence is filtered to the run owner and unowned raw
application/error logs are excluded. The current file-backed
`block_search_domain` action is personal-only, so distributed runs stage it for
review instead of mutating deployment-global configuration.

Optimizer package and HTTP discovery do not initialize the analysis pipeline.
The route retains only its narrow storage/action authorities; analyzer,
proposer, applier, and orchestrator modules resolve when an authorized run
starts. Package-level compatibility exports must remain lazy so a storage-only
consumer cannot accidentally initialize autonomous execution.

## Daily report

Daily provider detection: see [model registration](../MODEL_REGISTRATION.md#daily-provider-detection).

Daily-report readers gather owner-scoped state. Historical extraction bounds
candidates before hydration, then derives statistics and its 800-character
transcript once with a 128-turn retained prefix; lazy failure drops the day.
Calendar/day counts send explicit local intervals; storage projects Turn
timestamp scalars, bounds frozen decoding, and returns only distinct counts.
Reports commit idempotently; the durable owner reconstructs `reports:maintain`.
Its reconstructible LLM analysis uses the same strict billing-stop admission;
the same finite actual-429 allowance applies. Explicit scheduler prompts,
timer/proactive polls, and attended Agent calls do not inherit either narrower
policy.

Daily-report route discovery likewise keeps storage readers, cost aggregation,
conversation/todo collectors, LLM generation, and scheduler integration
dormant. The `lib.daily_report` facade resolves each focused owner on the first
authorized request or scheduled invocation while preserving the same explicit
owner and principal through the call.

The browser loads My Day write policy with the lazy feature chunk. Its typed
controller receives the selected report, render/cache seams, and daily API as
explicit ports. TODO toggles and deletes update optimistically and restore the
same item/order on rejection; successful stream cycles adopt the server's
resolved status, and a created task adopts the returned authoritative report.
Quick actions use a separate typed intent controller: it fills the composer
before creating the conversation so project/tool state remains armed, then
applies each declared tool mode through an injected presentation port.
The typed report repository is an owner-scoped, reconstructible read cache:
96 reports of at most 512 KiB plus 24 month overviews of at most 128 KiB bound
estimated storage at 51 MiB. Schema v3 discards the former unscoped/unbounded
cache instead of migrating it across owners. The idle background feature owns
one cache-first digest revalidation and one three-hour reminder timer; its
owner/date ledger retains at most 16 entries and `beforeunload` destroys both
timers. The retained panel now owns presentation only.

## Failure semantics

- Invalid schedule/action: reject at creation/update.
- Claim conflict: another worker owns the occurrence; do not execute.
- Action failure: record typed terminal outcome and apply bounded retry policy.
- Process crash: lease/claim expires and a later poll safely resumes.
- Owner/resource missing: terminal non-retryable outcome unless the contract
  declares temporary unavailability. Optimizer verification failure rolls back/
  rejects the action and retains the proposal plus diagnostics.

## Invariants

- Sidecar state is authoritative; polling memory is a cache. Timer, occurrence,
  and report identities are idempotent. Owner/tenant identity is explicit from
  storage through execution; a missing owner or scope fails before a scheduler/
  optimizer storage call.
- Only registered actions run autonomously. Claims/leases have bounded lifetimes
  and recovery semantics. Retry loops are capped and never duplicate completed
  side effects. Optimizer writes are allow-listed, reversible, and post-verified.
- Notifications reflect stored outcomes; they do not define completion.
- My Day optimistic writes roll back visibly; the browser never invents a
  stream-cycle result or treats its local report cache as authority.
- A My Day quick action prefills before conversation creation; tool modes and
  send-button reconciliation cross explicit UI ports.
- My Day cache keys require a resolved positive owner, cache bytes/entries are
  bounded, and the retained panel cannot recreate its own IndexedDB or timer.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Timer field/schedule | scheduler contract and timer CRUD | parser, Sidecar, resume |
| Poll/claim behavior | timer poll/state modules | multi-worker/idempotency |
| New scheduled action | action catalogue + focused service | owner, input bounds, retry |
| Daily report input | focused reader | owner isolation, period idempotency |
| My Day browser mutation | `features/myday/task-actions.ts` | payload period, optimistic rollback, server status, cache-after-ack |
| My Day quick action | `features/myday/quick-action-launcher.ts` | prefill-before-create, selected item, explicit tool intent |
| My Day cache/background | `features/myday/{report-cache,background-controller}.ts` | owner isolation, byte/entry ceilings, single probe/reminder, teardown |
| Optimizer signal | analyzer | proposal determinism |
| Auto-applied action | `actions/`, applier | write set, rollback, verification |

## Test map
```bash
pytest -q tests/scheduler/test_scheduler_tool_schema.py tests/llm_dispatch/test_timer_parse_failure.py \
  tests/llm_dispatch/test_timer_poll_agent_loop.py tests/scheduler/test_timer_resume_guardrails.py
pytest -q tests/scheduler/test_timer_dispatch_outcomes.py tests/scheduler/test_scheduler_process_runner.py
pytest -q tests/conversation/test_daily_report.py tests/daily_report/test_daily_report_storage.py \
  tests/startup/test_daily_report_startup_boundary.py
pytest -q tests/myday/test_myday_background.py tests/myday/test_myday_task_actions.py
pytest -q tests/optimizer/test_optimizer.py tests/optimizer/test_optimizer_sidecar_storage.py \
  tests/startup/test_optimizer_startup_boundary.py tests/conversation/test_optional_llm_billing_admission.py
```


## Complete scheduled-task admission and dispatch

Problem: owner creation/migration stopped at 100 definitions, and the runtime
read only a 1,000-task prefix. Decision: exact native counts observe integrity;
SQL creation keeps identity locking without quota-only owner locking. Native
count overflow fails before staging documents, claims or indexes. Internal
owner/global list limit=0 reads every matching definition; positive limits are
explicit projections without a separate 2,000-row ceiling. Native traversal
uses bounded index windows in one snapshot and retains every OCC witness;
built-in adoption traverses the complete owner catalog. Migration preserves
its explicit cursor pages. Due claims remain atomic and owner-scoped, so the
complete feed does not duplicate occurrences. Alternatives: larger caps or
restoring only a prefix still strand accepted work; weakening identity/claim
fences introduces duplicate effects. Gates: native scheduler demand/migration
and semantic tests, SQL scheduler contract and due-feed reconciliation tests.

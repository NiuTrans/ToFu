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

Already-cancelled subprocess jobs return typed cancellation without launch; success requires zero exit code without timeout, cancellation or error. Checking only after launch was rejected because termination cannot undo startup effects; cancellation during launch still joins process-tree cleanup. Scheduled subprocess output is drained in 8 KiB nonblocking reads, retaining at most 50,000 stdout and 10,000 stderr characters before accumulation. Continuous output still observes cancellation and the deadline, without extra reader threads. POSIX timeout/cancellation terminates the process group even after its leader exits, grants at most two seconds for graceful exit, then kills surviving descendants. `tests/scheduler/test_scheduler_process_runner.py` measures parent heap under a 16 MiB dual-stream flood (<2 MiB peak).

Owned task polling uses one terminal snapshot renderer in `lib/scheduler/executor/_await.py`: all status/wait paths return complete terminal evidence, errors/aborts remain partial output, and unavailable or nonterminal states never imply success. Waits require a handle, respect parent cancellation, return early on completion or pending turn input, and are bounded by the caller (60 seconds for the retired `await_task`, `POLL_WAIT_CAP_S` for `poll`); no-ID polling is a dashboard. This replaces length-only/tail-only results that contradicted the tool promise. Executable contract: `tests/scheduler/test_poll_task_results.py`.

## Timer lifecycle

1. An authenticated owner creates a watcher with a validated readiness check and continuation.
2. The Sidecar persists identity, owner, next due time, and revision.
3. Polling claims due work idempotently.
4. The executor invokes a declared action with bounded context and runtime.
5. Outcome is recorded before the next schedule is advanced.
6. Notification/push is a projection of the recorded outcome.

Restart must neither lose a durable timer nor execute one occurrence twice; process-local wakeups are hints, the Sidecar row is authoritative, and parse/timezone failures suspend visibly instead of entering a tight poll loop.

Timer Watchers register every accepted live watcher without process/owner count admission quotas. `TOFU_TIMER_LIVE_CAP` and `TOFU_TIMER_RESUME_CAP` are retired. Startup recovery uses the internal active feed with limit=0 (all rows through native index pages) and starts every eligible definition; declared positive query limits still select bounded projections. Cross-replica leases and local duplicate registration retain single ownership. Actual thread-start failure rolls back local registration/lease and reports startup failure; tool creation keeps its existing durable cancellation rollback. Native total/active counts remain exact integrity observations and migration no longer refuses definition 513. Waiting watchers share one lazy event loop; blocking storage/model calls use demand workers with no active ceiling or retained idle workers. Cancel wakes waits. Shutdown joins physical calls and releases leases before task quiescence, HTTP/runtime-store closure; failed joins keep dependencies live. Gates include `tests/scheduler/test_timer_wait_runtime.py` (32 blocked peers plus healthy work, stop/cancellation drain) and 512 waiting watchers sharing one thread.

Clock jobs use `schedule_create` (cron/`once:`); readiness waiters use `timer_create` (single-shot continuation, no cron). Schemas remain within 600/450/1000 combined tokens. Timer creation is resident/direct-call under routed exposure, Tool Search and programmatic compaction; other scheduler tools remain searchable. SQL/native admission accepts predicate-only watchers, and native `max_polls=0` preserves unlimited checks. Human-only restart/redeploy waits remain forbidden. A continuation or `current` agent job targets only a conversation whose turn the creating task owns (`completion_conversation_id`): child invocations, conversation-less tasks and headless requests are not offered `timer_create` and are refused both with a typed error (task engine, "Background command delivery owner").

**Problem:** equal command output can hide model recovery or semantic changes; negative agreement does not validate a future positive edge. **Decision:** predicates use zero LLM calls; hybrid/semantic watchers retain verification and require JSON booleans. Cancellation fences model/tool boundaries; the shared no-progress guard stops repeated tool/result loops. **Alternatives rejected:** permanent output caching and promotion after three negative agreements can miss or falsely accept readiness; code-only errors never acquire an ungrounded model fallback.

**Problem:** a hybrid proactive agent's poll LLM has no tools and its status snapshot never carried the predicate result, so its authoritative verdict was blind and hybrid-to-code promotion depended on chance agreement. **Decision:** code and hybrid polls evaluate the predicate once, before the LLM, and append its verdict, note and fenced output to the status snapshot that the poll LLM, the reconcile and the poll ledger all use (`proactive.predicate_status`). **Alternatives rejected:** a second evaluation for the prompt (two runs can disagree with the reconciled one). Specification: `tests/scheduler/test_proactive_status_budget.py`.

**Problem:** a scheduled agent's `command` was both the poll LLM's standing instruction and the instruction executed when ready. The schema told callers to pass an empty `command` for a pure-code agent, which `execute_proactive_task` cannot run (both storage authorities also reject an empty `command` at creation, with an opaque protocol error), while `command` plus `condition_command` derived `hybrid`: the poll LLM, prompted to prefer acting when unsure, stayed authoritative until enough agreements promoted the predicate, so an agent ran although its decisive predicate exited 1. **Decision:** for `task_type='agent'`, `command` is always the instruction executed when ready. `create_task`, and every `update_task` that would leave an agent's `command` empty or whitespace, raise `ValueError` before storage; `schedule_create` and the `schedule_manage` update return it as `Error:`. `derive_scheduled_condition_kind` makes an agent with a non-empty `condition_command` `code` from creation: the predicate decides every poll with zero LLM calls, a match dispatches `command`, exit 1 waits, and an ambiguous or errored predicate never triggers. `hybrid` is reached only by the existing ambiguity demotion (sustained predicate errors in `reconcile_and_decide`), whose poll LLM reads `command` as its standing instruction. Without a predicate the agent stays `llm`. `update_task` cannot change the predicate; a real `task_type` change re-derives the kind as creation would, and otherwise stored rows keep their kind (no migration). Timers keep `derive_condition_kind`: their check instruction and continuation are separate fields. **Alternatives rejected:** a persisted `check_instruction` column for scheduled tasks adds a field across the SQL and TofuDB storage authorities and their migrations to separate roles the predicate already decides; keeping hybrid-at-creation with an LLM veto still puts two roles in one field and pays an LLM call per poll. Specification: `tests/scheduler/test_scheduled_agent_condition_contract.py`.

**Problem:** a row's `task_type` selects the authority its execution runs with: built-in types (`optimizer`, `provider_probe_daily`, `model_intelligence_daily`, `storage_backup`, `reserve_reclaim`, `daily_report_backfill`) mint system-scoped principals or sweep the whole deployment, and `router_watchdog_review` belongs to the Router Watchdog. `schedule_manage` update forwarded any `updates` object, and `update_task` accepted `task_type`, so a model could turn its own row into a built-in type (or set poll/execution bookkeeping, conversation routing and agent tool settings); `schedule_create` never checked `task_type` server-side; and an agent's `condition_regex` was not validated as a timer's is, so an invalid one kept every code-tier poll ambiguous. **Decision:** `tool_defs` declares `SCHEDULABLE_TASK_TYPES` (command, python, prompt, agent) and `TOOL_UPDATABLE_FIELDS`, and the published schemas are built from them. The tool boundary refuses other create types and other update fields by name; `update_task` lets a row switch only among the schedulable types and applies the code-execution gate to the new type, so built-in and service rows keep the type their owner created whatever the caller. Agents and timers share `validate_condition_regex`. **Alternatives rejected:** an execution-time owner check per built-in handler (each new handler must remember it, and the row is already corrupted); refusing service types in `create_task` (the Router Watchdog legitimately creates its review rows with an owner principal). Specification: `tests/scheduler/test_scheduler_tool_boundary.py`.

Restart resumes durable active timers regardless of creation age, including
`max_polls=0` watchers. Offline elapsed time does not consume actual poll budget
or justify rewriting a timer as expired. `TOFU_TIMER_MAX_AGE_HOURS` is retired
and ignored; cancellation, exhausted actual poll counts, leases and terminal
states still govern execution. Startup restoration carries each timer's owner
and never mutates durable status based on wall-clock age.

Committed `ready` is a dispatch obligation: busy/unavailable lanes retry without new checks, poll exhaustion or resume age expiry. Exact conversation command identity deduplicates continuation.
Committed `ready` is a dispatch obligation: busy/unavailable lanes retry without new checks, poll exhaustion or resume age expiry. A busy target lane is delivered, not deferred: the continuation enters the SAME conversation command service with `injectMode='steer'`, which commits it into the live attempt as a durable injection block the model sees on its next round and falls back to the durable queue when the steer window has closed. `steered` and `queued` therefore settle the watcher exactly like `started`, and the recorded `last_poll_decision` is the delivery receipt rendered by the timer panel. Exact conversation command identity deduplicates continuation.
 Cancelled/foreign-owner calls have no local watcher authority; exceptional loop exit releases the registry/lease for resume. Commands use the shared bounded process runner, explicit owner/project directory; regex readiness requires successful stdout. The 16 MiB flood test retains <2 MiB parent heap and at most 3,500 stdout + 500 stderr characters. No resident worker is added; default 120 polls bound ledger text to about 0.8 MiB, while explicitly unlimited watchers retain growing audit history. Gates: `tests/scheduler/test_timer_readiness_reliability.py`, SQL storage contract and native semantic timer tests.

**Problem:** executor acceptance can precede failure of the timer's triggered receipt, and a browser list request can predate a successful mutation. **Decision:** a ready watcher retires only after the triggered status CAS is acknowledged; failure retains the obligation and replays the same Turn command, without another executor. A lost CAS never announces success. Browser mutations retain their pending row until a new list read follows any older in-flight read. Click containment uses the original event path, so synchronous pending-row replacement cannot turn an internal action into an outside click and close it. **Alternatives rejected:** retiring on executor acceptance strands active rows; reusing a pre-mutation list restores stale actions. These changes add no worker, cache or periodic read.
**Alternatives rejected:** retiring on executor acceptance strands active rows; reusing a pre-mutation list restores stale actions. A second watcher-owned lane for continuations was rejected as a duplicate dispatch path; waiting for the target turn to end was rejected because a long agentic turn never yields and the watcher's one-shot obligation would be spent on retries that re-poll the model.

**Problem:** a readiness watcher never owns its target lane, and the conversation is very often still running when its condition becomes true — a bare `lane_busy` refusal made every trigger a paid no-op. **Decision:** the watcher asks for the steer lane, so `steered`/`queued` are accepted deliveries that retire it; only `retry` (lane unavailable) keeps watching. The trigger route maps `retry` to 409 `timer_delivery_retry` and a settled row to 409 `timer_inactive`, and the panel announces every outcome — delivery lane on success, retry/closure/failure otherwise — instead of rolling the row back silently.

A steer reaches the model only at its attempt's next round boundary. When the attempt ends first, the task finalizer re-queues the undelivered steer as the conversation's next turn (`_salvage_undelivered_steer`) and `record_requeued_continuation` moves the watcher's receipt from `steered` to `queued` (CAS on `triggered`, best-effort; the queued turn is the delivery authority). An already rendered final answer is never reopened to consume a late steer: it would stop being the answer it was rendered as.

**Problem:** a poll still in flight when its watcher settled (operator delivery, check-now delivery, cancel, dispatch failure) committed its verdict afterwards, overwriting the delivery receipt, spending budget and appending a ledger row on a settled row. **Decision:** `timer.poll.commit` advances only an active watcher in both authorities; a settled one returns `{inserted: false, id: null, advanced: false, status}` with no ledger row, poll-ID claim or progress change, while a replayed poll ID still returns its original row. Predicate reconcile writes carry `expected_status: active`. **Alternatives rejected:** a caller-side status re-read narrows but cannot close the window; gating only the progress update would still leave a ledger row and claim on the settled watcher. Contracts: `tests/storage/test_storage_authority_sql_contract.py`, the native `timer_poll_commit_*` tests, the timer script in `tests/tofudb/test_tofudb_differential.py` and `test_timer_continuation_integration.py`.

**Problem:** the panel's only action, 触发, was labelled in most locales as running one check but delivered the continuation unchecked, so a continuation could assert a condition that did not hold. **Decision:** `POST /api/v1/timer/<id>/check` (`check_timer_now`) runs one poll with a background poll's semantics — it spends one poll of `max_polls` (a spent budget retires the watcher as exhausted), writes the same ledger row, and a committed `ready` is delivered without re-checking — and delivers only on `ready`. The committed row, not the call's own verdict, decides: a poll number is one command identity, so a check that loses it to a background poll gets a command conflict and reports the row that poll committed. The response carries `ready`, `decision`, `reason` and, when delivered, `delivery`; delivery refusals reuse the trigger route's 409 codes. The panel makes checking the default action and names an unmet condition with its reason; `/trigger` remains forced delivery, labelled as skipping the check. **Alternatives rejected:** changing `/trigger` in place would silently alter a documented API; waking the watcher loop early yields no verdict for the operator and needs a cross-replica signal. Specifications: `test_timer_check_now.py`, the timer routes in `tests/api/test_api_v1_integration.py` and `tests/frontend/test_frontend_timer_trigger_feedback.py`.

These changes add no worker, cache or periodic read.
 Fault and real-storage specifications live in `test_timer_dispatch_outcomes.py` and `test_timer_continuation_integration.py`; `test_timer_mutation_reconciliation.py` and the visual timer-panel test cover stale responses and failure recovery.

Timer/proactive conversation-dispatched runs retain `build_task_config` in
`lib/scheduler/_shared.py`: the timer's `tools_config` wins on conflict for tool
choices, otherwise conversation settings carry through — including the
translation pair (`autoTranslate`, `uiLang`) that every translation trigger
resolves from `task['config']` alone. The model route, reasoning depth, extra
and read-only roots and Manual write approval (`autoApply`) come from the
conversation through inactive `resolve_conv_config`; its `modelRef` is the
routing authority, so the `model` name is aligned to it, as task registration
does. A `schedule_create` definition's model-authored `tools_config` never
selects the model. Attended capabilities (`autopilot`, `desktopEnabled`,
`humanGuidanceEnabled`) are explicitly off: never granted, and recorded as a
human turn records them. While the conversation is in Plan mode the dispatch
returns `busy` (nothing accepted); a timer keeps its ready verdict and retries
without a new check, and an `await_device` wake stays pending for the device's
next connection, so a continuation can neither escape the read-only plan nor
gain the human guidance Plan requires.

**Problem:** a continuation ran on a different model than its creator, at the
default depth, without the owner's prompt, and rebuilt its model context because
the replay policy witness differed (stored settings had merged the model name
and `modelRef` from different sources; see `resolve_conv_settings`).
**Decision:** `timer_create` and `await_device` capture the creating task's
`CONTINUATION_POLICY_KEYS` server-side (`capture_continuation_policy`): route,
reasoning, `maxTokens`/`temperature`, system prompt and its mode/blocks,
`chatMode`, `delegateMode`, `schedulerEnabled`, `mcpToolExposure`, the
workspace binding (`projectPaths`, `readOnlyPaths` with `projectPath`) and the
owner preference blocks `tools`/`cache`/`responses`/`compaction` (mappings only;
a caller's tool-schema list is never replayed). `continuation_policy` replays
them as the dispatch's `config_overrides`, replacing the route as a unit so a
captured name is never paired with the conversation's `modelRef`. Chat mode is
dropped when its flag expansion would grant a tool (the swarm rule). A
browser-shaped creator and its continuation then share one policy witness.
Rows without a capture (created before it) follow the conversation's route and
still rebuild once. Flow and attended capabilities are never inherited; a
creator with any of them on therefore still rebuilds. **Alternatives
rejected:** conversation settings alone cannot reconstruct the global prompt or
owner preferences; copying the whole parent config would carry attended
capabilities and internal carriers into an unattended turn; equating an absent
witness field with an explicit value was rejected for replay ingress
(`docs/CONTEXT_COMPACTION.md`), so server-built turns state explicit values
instead. Contract: `tests/scheduler/test_timer_continuation_policy.py`.

**Problem:** separate swarm defaults invalidated identical strict/portable replay policy.
**Decision:** only `swarm_autocontinue_reply` uses inactive `resolve_conv_config`, sharing human prompt/route/depth defaults.

Swarm still disables Flow/autopilot/desktop/guidance/scheduling and rejects tool/noncontrol overrides;
chat-mode expansion cannot add tools. Read-only/manual-approval settings, virtual-user provenance,
Stop/parent CAS and budgets remain. Plan returns `start_failed` before admission rather than enable guidance.

**Alternatives rejected:** weaker hashes or unvalidated parent config copying. This wake has no accepted attempt
config witness; nondefault global prompt/tools still differ and rebuild. Contract: `tests/scheduler/test_swarm_continuation_config.py`.

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

The billing-reserve built-in is the sole periodic recovery owner and is policy reconciled disabled unless multi-user relay billing is active. The scheduler's durable claim prevents duplicate sweeps; request-worker startup creates no parallel janitor. Daily-report backfill also uses this worker: it runs every six hours, queues a startup hint only when yesterday is absent, and has no second six-hour sleeper. Optimizer and backup built-ins preserve user-controlled enablement during definition reconciliation. The personal backup's finite `max_runtime` derives from the launch-probed recovery-copy budget (30 minutes..6 hours by default, explicit ceiling 24 hours). If retained recovery points make only the transient peak exceed the copy budget, a fastpath run may hard-link and fully publish the new point before budget-retiring older verified backups; it never rotates the unique deep-clean rollback or falls back to a full copy for that exception.

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

Optimizer analysis is read-only. One run snapshots each eligible bounded log tail once; distributed mode still excludes raw application/error lines and audit rows remain owner-filtered. The one exception to the tail is prompt-cache split evidence (`analyzer/_cache.py`): in personal mode it streams the window's `[CacheRoundRecord]` lines from `app.log` and its plain rotated siblings into per-group counters, because a busy log's 4 MiB tail covers only a few hours (docs/LLM_COST_OPTIMIZATION.md section 6). Each audit/error snapshot streams once into all projections without retaining parsed entries; application lines without a possible signal skip timestamp/regex work, with an 8,192-character fast-path ceiling.
All block-domain post-apply metrics share one application-log pass. Transcript-derived signals select owner-scoped metadata, then use bounded repository hydration; a transcript too large for one response is read back to the signal's cutoff in repository windows, and a scan the authority still cannot serve is recorded on the bundle as unavailable evidence, which the proposer prompt names explicitly (unknown, never a zero count). Proposals are durable facts. Automatic application is
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

Daily-report readers gather owner-scoped state. Historical extraction bounds candidates before hydration, then derives statistics and its 800-character transcript once with a 128-turn retained prefix; lazy failure drops the day. Calendar/day counts send explicit local intervals; storage projects Turn timestamp scalars, bounds frozen decoding, and returns only distinct counts. Reports commit idempotently; the durable owner reconstructs `reports:maintain`. Its reconstructible LLM analysis uses the same strict billing-stop admission; the same finite actual-429 allowance applies. Explicit scheduler prompts, timer/proactive polls, and attended Agent calls do not inherit either narrower policy.

Daily-report route discovery likewise keeps storage readers, cost aggregation,
conversation/todo collectors, LLM generation, and scheduler integration
dormant. The `lib.daily_report` facade resolves each focused owner on the first
authorized request or scheduled invocation while preserving the same explicit
owner and principal through the call. Its dynamic exports have unknown types,
not module-object types, so request adapters retain callable/list contracts
without eagerly importing every domain owner.

The browser loads My Day write policy with the lazy feature chunk. Its typed controller receives the selected report, render/cache seams, and daily API as explicit ports. TODO toggles and deletes update optimistically and restore the same item/order on rejection; successful stream cycles adopt the server's resolved status, and a created task adopts the returned authoritative report. Quick actions use a separate typed intent controller: it fills the composer before creating the conversation so project/tool state remains armed, then applies each declared tool mode through an injected presentation port. The typed report repository is an owner-scoped, reconstructible read cache: 96 reports of at most 512 KiB plus 24 month overviews of at most 128 KiB bound estimated storage at 51 MiB. Schema v3 discards the former unscoped/unbounded cache instead of migrating it across owners. The idle background feature owns one cache-first digest revalidation and one three-hour reminder timer; its owner/date ledger retains at most 16 entries and `beforeunload` destroys both timers. The retained panel now owns presentation only.

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
pytest -q tests/scheduler/test_timer_dispatch_outcomes.py tests/scheduler/test_scheduler_process_runner.py \
  tests/scheduler/test_timer_check_now.py tests/scheduler/test_timer_continuation_policy.py
pytest -q tests/scheduler/test_scheduled_agent_condition_contract.py tests/scheduler/test_scheduler_predicate_promotion.py \
  tests/scheduler/test_proactive_status_budget.py
pytest -q tests/conversation/test_daily_report.py tests/daily_report/test_daily_report_storage.py \
  tests/startup/test_daily_report_startup_boundary.py
pytest -q tests/myday/test_myday_background.py tests/myday/test_myday_task_actions.py
pytest -q tests/optimizer/test_optimizer.py tests/optimizer/test_optimizer_sidecar_storage.py \
  tests/startup/test_optimizer_startup_boundary.py tests/conversation/test_optional_llm_billing_admission.py
```


## Complete scheduled-task admission and dispatch

Problem: owner creation/migration stopped at 100 definitions, and the runtime read only a 1,000-task prefix. Decision: exact native counts observe integrity; SQL creation keeps identity locking without quota-only owner locking. Native count overflow fails before staging documents, claims or indexes. Internal owner/global list limit=0 reads every matching definition; positive limits are explicit projections without a separate 2,000-row ceiling. Native traversal uses bounded index windows in one snapshot and retains every OCC witness; built-in adoption traverses the complete owner catalog. Migration preserves its explicit cursor pages. Due claims remain atomic and owner-scoped, so the complete feed does not duplicate occurrences. Alternatives: larger caps or restoring only a prefix still strand accepted work; weakening identity/claim fences introduces duplicate effects. Gates: native scheduler demand/migration and semantic tests, SQL scheduler contract and due-feed reconciliation tests.

Problem: the 30-second tick ran command, python, prompt, optimizer and proactive-agent tasks inline (only four maintenance types had their own thread), so one slow task delayed every other due task, and because a cron expression matches only the current minute, occurrences that came due while the tick was blocked were lost. Decision: `_dispatch_claimed_task` runs every claimed occurrence on its task's own thread and returns; the tick only lists, claims and dispatches. Runs are single-flight per task id: an occurrence claimed while the same task still runs becomes one pending rerun (further claims coalesce into it) that starts when the run ends, so a task neither overlaps itself nor silently drops due work. Stop joins runs by its deadline and drops pending reruns. Threads follow claimed occurrences; there is no worker pool or concurrency ceiling. Alternatives rejected: a bounded pool reintroduces cross-task waiting; dropping the occurrence while the task runs (the old maintenance policy) loses claimed work; allowing self-overlap lets stateful tasks race themselves; queueing every missed occurrence grows without bound for a fast cron. Gate: `tests/scheduler/test_scheduler_due_tasks_run_off_tick.py`.

## Atomic execution settlement

**Problem:** terminal bookkeeping hydrated every owner schedule, calculated cron presentation and then updated rows selected by an earlier execution witness. **Decision:** `scheduler.task.settle_execution` atomically matches owner, agent type and exact execution task ID. Rust maintains an owner-scoped execution index with exact document verification; legacy owners backfill once in the same transaction. SQL uses an owner/execution partial index and conditional UPDATE. Receipts replay the original result, and stale execution callbacks change zero rows. One index entry per eligible definition plus one native owner marker is retained until membership changes or deletion; no worker, cache or admission quota is added. A 256-definition fixture retained 257 index entries totaling 14,387 encoded key/value bytes; page, blob and receipt overhead are excluded. Settlement first uses the read-only `scheduler.task.execution_lookup` port. A proven absence skips command/receipt creation; matching or uninitialized legacy state enters atomic settlement with its ordinary receipt/outbox lifecycle. An empty native owner is checked against count and created-index witnesses; a nonempty legacy owner never treats a missing execution index as absence. End-to-end performance remains pending. **Alternatives rejected:** hydrating schedules does work unrelated to settlement; an application reverse map loses restart authority; read-then-update permits a later execution to be overwritten. Verify scheduler execution-settlement port, storage receipt/owner tests and native scheduler execution tests, including legacy backfill, OCC conflict and reopen.

# Module Design Doc — Unit 10: Scheduling / Ops (`scheduler/`, `optimizer/`, `daily_report/`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md`). These three
> subsystems all run WORK ON A TIMER with no user in the loop: the scheduler
> fires cron/proactive-agent tasks, the optimizer runs a nightly self-tuning loop
> that can auto-apply config changes, and daily_report generates unattended.
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11. `list_dir`
> overcounts — all numbers are `wc -l`. Every MISCUT/BIG verdict cites competing
> responsibilities or line ranges; size alone is never the argument.
>
> **The analytical payload is AUTONOMOUS-MUTATION SAFETY:** what can each of these
> change WITHOUT human approval, and is that power bounded?

---

## 1. Autonomous-mutation safety — the blast radius, mapped

### 1a. The optimizer — whitelist auto-apply IS real and TIGHT (verified)

The risk: a nightly LLM-driven loop auto-applying config changes with no human
in the loop. **Verdict: the auto-apply power is bounded to a ONE-ENTRY whitelist
with a real TTL-revert path. Everything else is forced to `pending_review`.**
Verified by reading `actions/__init__.py` + `applier.py`:

- **The whitelist exists and is a single dict** — `ACTION_REGISTRY` in
  `actions/__init__.py`. It has **exactly ONE `auto_apply=True` action**:
  `block_search_domain` (add a domain to `server_config.search.skip_domains` for
  N days). Every other action type (12 of them: `adjust_fetch_timeout`,
  `suggest_model_fallback`, `toggle_llm_content_filter`, `extend_cache_ttl`,
  `disable_failing_scheduled_task`, …) is registered `auto_apply=False` with
  `apply=None` — they are SUGGEST-ONLY and always land in `pending_review`.
- **The applier is the sole gate, and it fails closed** — `applier.py`'s
  docstring: "This is the ONLY place allowed to call a handler's `apply()`. Any
  `action_type` not marked `auto_apply=True` is stored as `pending_review` and
  skipped — no exceptions." Read the code: `initial_status = 'applied' if (entry
  and entry.get('auto_apply') and not dry_run) else 'pending_review'`. An unknown
  action_type → `pending_review` ("unknown action_type"). A non-whitelisted one →
  `pending_review` ("not in auto-apply whitelist"). **There is no path to
  auto-apply an action that isn't `block_search_domain`.**
- **The revert path is real and scheduled** — `block_search_domain` has a
  `revert(args)` that removes the domain again (verified: line 121, idempotent,
  audit-logged). `applier.revert_expired_actions()` scans
  `optimizer_action_log` for rows past `expires_at` and calls the handler's
  `revert()`. And it runs FIRST on every `run_once` (`orchestrator.run_once` step
  1, before proposing anything). So a TTL-expired auto-applied change is reverted
  on the next nightly tick. An action with no revert handler is marked `expired`
  without reverting (logged) — never silently left applied past TTL.
- **The blast radius of the one auto-apply action is minimal + reversible** — it
  only appends a domain to the search skip-list (a fetch-quality tuning), TTL-
  bounded (default 7 days), and fully reversible. It touches NO hyperparameter,
  NO model routing, NO DB schema, NO security config — all of which are the
  `_SUGGEST_ONLY` set requiring human approval, explicitly per CLAUDE.md §10.

**The alignment with the project's mandatory-approval rule is exact:** the
`_SUGGEST_ONLY` docstring cites CLAUDE.md §10 (hyperparameters, model routing, DB
schema, security require human approval), and the only auto-applied action is a
reversible, TTL-bounded, non-§10 search-quality tweak. **Auto-mutation is
bounded.**

### 1b. The proactive agent — CAN spawn a full agentic run on a trigger (by design), bounded by poll-gate + caps

The risk: a scheduled `task_type='agent'` task spawning a full tool-enabled,
money-spending agentic run with no human confirmation. **Verdict: YES it can —
that is the feature — but the trigger is gated by an independent per-poll LLM
decision, and the run is bounded by max_executions / expiry / a re-entrancy
guard. It is NOT unbounded, but it IS the highest-autonomy surface in the tree
and deserves the loudest documentation.** Verified by reading `proactive.py`:

- **Two-phase design (read in full):** Phase B (`poll_decision`) = a lightweight
  `capability='cheap'`, `max_tokens=256`, tools-OFF LLM call that answers
  `{"act": true/false}` from a status snapshot. ONLY if `act=true` does Phase C
  (`execute_proactive_task`) fire — which creates a real user message in the
  target conversation and runs a FULL agentic task with ALL tools + SSE, visible
  in the frontend like any user turn. So a full run WITH tools DOES happen on a
  trigger with no human confirm.
- **The gate is the poll decision, and it is deliberately independent** — each
  poll is stateless (no cross-poll history, "saving tokens" + preventing
  drift-toward-act), tools-OFF (`tools_available=False` in the poll prompt), and
  the status snapshot is `fence_untrusted`-wrapped (prompt-injection defense — the
  conv content is fenced as DATA, not instructions). So the DECIDE step can't
  itself act or be hijacked by conversation content into acting.
- **Bounded by three caps** (`should_auto_disable`): `max_executions` (auto-
  disable after N runs), `expires_at` (auto-disable after a deadline), and
  `is_task_executing` (re-entrancy guard — won't spawn a second run while the
  prior one is still running). So a proactive task can't fan out unboundedly or
  run forever.
- **The RESIDUAL risk (documented, not a bug):** within its caps, a proactive
  task DOES spend money + make tool changes autonomously on each `act=true`. The
  human authorizes this ONCE at task-creation (choosing `task_type='agent'` +
  `target_conv_id` + `tools_config` + `max_executions`), not per-run. That is the
  intended contract of a "proactive agent," but it IS a standing grant of
  autonomous spend/mutation — the single most powerful autonomous surface in the
  unit. The mitigations (poll-gate, caps, visible-in-frontend, poll-log audit
  trail in `proactive_poll_log`) bound it; they do not eliminate the standing grant.

### 1c. daily_report — read-mostly, bounded autonomous write

`daily_report` auto-generates a work journal (backfill yesterday at boot + every
6h). Its only autonomous mutation is writing a JSON report file per date + a
cost-cache row + TODO-carryover state — no config, no money, no tool execution.
The `_SUGGEST_ONLY` `disable_failing_scheduled_task` even routes THROUGH the
human-approval gate. Lowest blast radius of the three.

### 1d. Summary — the autonomous-mutation ladder

| Subsystem | Auto-mutates without human approval | Bounded by |
|---|---|---|
| optimizer | ONE action (`block_search_domain`: TTL search-skip) | 1-entry whitelist + TTL-revert + fail-closed applier |
| proactive agent | a FULL tool-enabled agentic run (spend + changes) | per-poll LLM gate (tools-off, fenced) + max_exec + expiry + re-entrancy guard; **human grants once at creation** |
| daily_report | a JSON report file + cost cache + TODO state | date-scoped, no config/money/tools |

No unbounded auto-apply; no unbounded proactive trigger. The proactive agent is
the highest-autonomy surface and is correctly the one with the most gating.

---

## 2. Module inventory (real `wc -l`, size verdict, status, tests)

### 2.1 `scheduler/` (3848 LOC, 8 files)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `timer.py` | 1039 | **BIG** | HOT | `test_timer_poll_agent_loop`, `test_timer_parse_failure`, `test_timer_poll_event_suppression`, `test_timer_resume_guardrails` |
| `manager.py` | 953 | **BIG** | HOT | via scheduler e2e |
| `executor.py` | 703 | **BIG** | HOT | via scheduler e2e |
| `tool_defs.py` | 340 | OK | HOT | via scheduler tool e2e |
| `proactive.py` | 312 | OK | HOT (highest autonomy) | via proactive e2e |
| `_shared.py` | 308 | OK | HOT | `test_timer_*` |
| `cron.py` | 167 | OK | HOT | via cron e2e |
| `__init__.py` | 26 | OK (facade) | — | — |

`timer.py` — **BIG (1039), bundles 2 concerns:** the Timer Watcher lifecycle
(create/cancel/status/resume + the `_deferred_resume` schema-readiness gate) AND
the inline poll-check loop (the `check_command` + poll-LLM-decision + continuation
injection engine — the blocking-timer analog of `proactive.poll_decision`). The
poll-loop engine is a separable concern from the watcher CRUD/persistence. Split
candidate: `timer_poll.py` (the poll engine) + `timer.py` (watcher lifecycle).
Cited by concern.

`manager.py` — **BIG (953), bundles 3 concerns:** the scheduled-task CRUD +
registry, the 30s scheduler heartbeat loop (`_scheduler_loop`), AND the
default-task auto-registration (the Daily Optimizer at 03:30 — line 621; note the
schema-readiness gate lives in `start_scheduler_worker._deferred_resume` per the
`startup-race` memory). The optimizer-registration + the cron loop are separable
from task CRUD. Split candidate; defer (hot, startup-race-sensitive).

`executor.py` — **BIG (703).** Runs a scheduled task by type (command / python /
prompt / agent). The `agent` branch delegates to `proactive.execute_proactive_task`.
Cohesive-ish (one dispatch by task_type) but at threshold; the command/python
sandboxed-exec paths are a separable cluster from the prompt/agent LLM paths.
Classified BIG, defer.

`proactive.py` — OK (312). The poll→decide→execute engine (§1b). Cohesive single
concern; the highest-autonomy code in the unit but well-bounded and self-contained.

`_shared.py` — OK. Shared poll-prompt builder (`build_poll_system_prompt`), the
`fence_untrusted` prompt-injection guard, `parse_json_decision`, and
`inject_and_run_task` (the common load→inject→config→create→run used by both
proactive and timer continuation). A genuine DRY seam — both autonomous triggers
route through it. Correctly extracted.

`cron.py` / `tool_defs.py` — OK. Cron-expression parsing; the schedule_* tool
schemas. Single-concern.

### 2.2 `optimizer/` (1546 + 241 actions LOC, 8 files)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `analyzer.py` | 734 | **BIG** | live (nightly) | `test_optimizer` |
| `proposer.py` | 329 | OK | live | `test_optimizer` |
| `storage.py` | 184 | OK | live | `test_optimizer` |
| `applier.py` | 148 | OK (the gate) | live | `test_optimizer` |
| `orchestrator.py` | 128 | OK | live | `test_optimizer` |
| `actions/block_search_domain.py` | 166 | OK | live (the ONE auto-apply) | `test_optimizer` |
| `actions/__init__.py` | 75 | OK (the whitelist) | live | `test_optimizer` |
| `__init__.py` | 23 | OK (facade) | — | — |

`analyzer.py` — **BIG (734), bundles the evidence GATHERERS** — one function per
signal source (tool-call/error counts, top search domains, IRRELEVANT-dropped
domains, audit-event counts, prior actions, daily-report snippets). It's a lot of
read-only DB/log scraping in one file. Each gatherer is independent; a split by
signal-family is possible. Cohesive (all "build the EvidenceBundle") but the
biggest optimizer file — classified BIG, defer (it's a read-only nightly path,
low risk, low value to split).

`applier.py` (148) + `actions/__init__.py` (75) — OK, and these are the
**safety-critical core** (§1a): the fail-closed apply gate + the one-entry
whitelist. Small, cohesive, heavily-commented — correctly bounded. This is where
the autonomous-mutation safety LIVES and it is tight.

`orchestrator.py` (128) — OK. The `run_once` pipeline (revert-expired → gather →
propose → apply/stage → audit). Clean, each step try/except-isolated + degrades
gracefully. `proposer` (329, the LLM proposal step) and `storage` (184, the
proposal/action-log DB) are both well-bounded.

### 2.3 `daily_report/` (2272 LOC, 9 files) — a REFERENCE decomposition

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `conversations.py` | 550 | OK | live | `test_daily_report` |
| `cost.py` | 542 | OK | live | `test_daily_report` |
| `todos.py` | 410 | OK | live | `test_daily_report` |
| `llm.py` | 247 | OK | live | `test_daily_report` |
| `prompts.py` | 124 | OK | live | — |
| `storage.py` | 118 | OK | live | — |
| `__init__.py` | 117 | OK (facade) | — | — |
| `generator.py` | 88 | OK | live | — |
| `scheduler.py` | 76 | OK | live | — |

**The entire `daily_report/` package is correctly bounded** — this is a
completed, documented decomposition (the 2405-LOC `routes/daily_report.py`
monolith was split into a thin route layer + this 9-module package; the split is
recorded in the `backend-daily-report-decomposition` memory). Each module is one
concern (cost / todos / conversations-extract / llm-analysis / prompts / storage /
generator / scheduler). Nothing miscut. A **sixth reference-quality package**.

---

## 3. Dependencies (in / out)

**Inbound:** `routes/api_v1/scheduler.py` (task CRUD + timer), `routes/api_v1/optimizer.py`
(the REST surface — `run_once`, list/approve/revert proposals), `routes/daily_report.py`
(the thin route layer). `server.py` startup → `scheduler.manager.start_scheduler_worker`
(the 30s heartbeat) + `daily_report.scheduler.start_report_scheduler`.

**Key internal edges:**
- `scheduler.manager` (30s loop) → `executor.run_task` → by type: `proactive.execute_proactive_task`
  (agent) / sandboxed exec (command/python) / `smart_chat` (prompt).
- `scheduler.manager` registers + fires the **Daily Optimizer** task nightly →
  `optimizer.run_once` (in-process). This is the seam that makes the optimizer
  autonomous — it's a scheduled task like any other.
- `proactive` + `timer` → `_shared.inject_and_run_task` → `tasks_pkg.manager`
  (spawns the real agentic task) — the autonomous-run seam.
- `proactive.poll_decision` / `timer` poll → `llm_dispatch.smart_chat`
  (`capability='cheap'`) — the gate LLM.
- `optimizer.analyzer` → `lib/database` + `audit.log` + `daily_report` snippets
  (read-only evidence). `optimizer.applier` → `block_search_domain.apply` →
  `server_config.json` (the one mutation) + `lib.reload_config`.

**Outbound:** `lib/database` (all three persist), `lib/llm_dispatch` (the gate +
proposal + analysis LLM calls), `json_store` (optimizer/report file writes),
`audit_log` (every autonomous mutation is audited). **No back-edges up into
`routes` from the lib packages.**

---

## 4. Invariants (must not be broken by a refactor)

1. **`applier.apply_proposal` is the ONLY caller of an action's `apply()`** and it
   fails closed — a non-`auto_apply=True` action_type is ALWAYS `pending_review`.
   Never widen the auto-apply whitelist to a §10 category (hyperparameter / model
   routing / DB schema / security) without human sign-off.
2. **Every auto-applied action MUST have a reversible `revert()` + a TTL**, and
   `revert_expired_actions` runs FIRST on every `run_once` — so an auto-applied
   change self-reverts at TTL. An action without a revert handler is marked
   `expired` without reverting (never silently left applied).
3. **The proactive/timer poll LLM runs tools-OFF** (`tools_available=False`) and
   fences the status snapshot as untrusted DATA (`fence_untrusted`) — the decide
   step can neither act nor be prompt-injected into acting.
4. **A proactive agent task is bounded** by `max_executions` + `expires_at` +
   the `is_task_executing` re-entrancy guard — it cannot fan out or run forever.
5. **Every autonomous mutation is audit-logged** (`audit_log('optimizer_run_complete'/'optimizer_revert'/'optimizer_action_failed')`,
   `proactive_poll_log` rows) — an unattended change always leaves a trace (the
   project's #1 logging rule applied to the code that runs while nobody watches).
6. **Import-time scheduler threads gate on schema readiness** — the Daily
   Optimizer registration + the janitor sweep poll for their exact table/column
   before first DB access (the `startup-race-import-time-threads-vs-init-db`
   lesson; the optimizer register moved into `_deferred_resume`).
7. **The optimizer degrades gracefully** — every `run_once` step is try/except-
   isolated; a crashed gatherer yields an empty `EvidenceBundle` so the audit
   record still writes. A self-tuning loop must never take down the server.
8. **Schedule caps are §10 config** — poll intervals, the 30s heartbeat, the 6h
   report backfill, `max_executions` defaults require sign-off to change.

---

## 5. Known debt (grounded)

- **`scheduler/timer.py` (1039) bundles watcher-lifecycle + the poll-loop engine**
  (§2.1) — a clean split seam.
- **`scheduler/manager.py` (953) bundles task-CRUD + the cron heartbeat +
  default-task registration** (§2.1) — separable, but startup-race-sensitive.
- **`scheduler/executor.py` (703)** and **`optimizer/analyzer.py` (734)** are BIG
  but cohesive (one dispatch-by-type / one evidence-bundle builder respectively).
- **CLAUDE.md's Unit-10 map is thin** — it names the three packages but not that
  `scheduler/` has 8 modules or that the optimizer's auto-apply is a 1-entry
  whitelist. Minor doc-drift.
- No unbounded auto-mutation found — the two autonomous-mutation surfaces this
  unit was tasked to bound are both gated (§1).

---

## 6. Segmentation verdict (this unit)

**Correctly bounded — leave as-is:**
The ENTIRE `daily_report/` package (a completed reference decomposition);
`optimizer/applier` + `actions/` (the safety-critical whitelist gate — small,
tight), `optimizer/orchestrator`, `proposer`, `storage`, `block_search_domain`;
`scheduler/proactive` (self-contained autonomy engine), `_shared` (the DRY
autonomous-run seam), `cron`, `tool_defs`.

**Miscut — should split (priority order):**
1. **`scheduler/timer.py` (1039) → extract `timer_poll.py`** for the poll-check
   loop engine (check_command + poll-LLM-decision + continuation injection),
   leaving the Timer Watcher CRUD/lifecycle/resume in `timer.py`. Behind
   `test_timer_poll_agent_loop` + `test_timer_resume_guardrails`.
2. **`scheduler/manager.py` (953) → extract the cron heartbeat loop + default-task
   registration** from task CRUD. RISK: startup-race-sensitive (the schema-gate in
   `_deferred_resume` must stay) — defer / do with care.

**Big but optional (defer unless touched):**
`scheduler/executor.py` (703 — command/python-exec vs prompt/agent-LLM branches),
`optimizer/analyzer.py` (734 — split by signal-family; read-only nightly path).

**Do NOT split:** `optimizer/applier.py` + `actions/__init__.py` (the safety gate
is deliberately small and centralized — fragmenting the whitelist would weaken the
"one place that can apply" guarantee), the `daily_report/` modules.

---

## 7. Comparison to Units 1–9 (the running thesis)

- **The two autonomous-mutation surfaces this unit exists to bound are both
  GATED, and the gates are the smallest, tightest code in the subsystem** — the
  optimizer's fail-closed 1-entry whitelist (`applier.py` 148 + `actions/__init__.py`
  75) and the proactive poll-gate (tools-off, fenced). The project put its
  safety-critical logic in small centralized modules, consistent with the
  single-source discipline seen in every prior unit (Unit 7's one table author,
  Unit 8's one egress guard + one cost engine, Unit 9's one AST-enforced boundary).
- **The proactive agent is the highest-autonomy surface in the whole tree** — it
  can spawn a full tool-enabled, money-spending run on a trigger. It is bounded
  (poll-gate + caps + audit trail) but rests on a STANDING human grant at
  task-creation, not per-run approval. That is the correct design for a proactive
  agent, but it's the one place worth re-reading whenever the caps or the poll-gate
  change — flagged as loudly as any structural finding.
- **`daily_report/` is a SIXTH reference-quality package** (with `swarm/`,
  `token_counter/`, `compaction/`, `billing/`, `agent_core/`) — a completed,
  documented monolith-split. The scheduler is the one subsystem here with genuine
  BIG-file debt (`timer`, `manager`, `executor` all >700), a step below the others.

---

*Next unit: Unit 11 (Ingest / media — `pdf_parser/`, `doc_parser`, `file_reader`,
`image_gen`, `transcription`, `paper/`, `translate/`).*

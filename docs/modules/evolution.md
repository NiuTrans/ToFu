# Self-evolution

This domain owns the goal-driven self-evolution engine: any task goal with
verifiable completion criteria (or an explicitly exploratory one) is planned
under measured cost, executed through the task engine, settled against its
criteria, and folded back into the stores that change the next attempt.
Design authority and the R1–R6 root-level program rationale:
[../EVOLUTION_DESIGN.md](../EVOLUTION_DESIGN.md).

The one loop: intake (`evolution_goal_v1`) → resource indexing (history,
project, academic through `index_all`) → plan assembly (measured cost,
self-model predictions, optional policy program) → authorization (objective submission confirms the default run plan; detailed API plans require confirmation) → execution through `run_agent_loop` only → settlement (terminal
listener plus criteria checkers, or explicit human verdict) → learning
(priors, strategies, anti-patterns, gaps, hypotheses) → gate-controlled
mutation (sandbox → holdout regression → confirm → activate → monitor with
auto-rollback). `maintenance.run_maintenance_cycle` is the single heartbeat
running every learning pass in dependency order; there is no second
orchestrator.

Invariants enforced by the store layer, not by callers: exploratory episodes
never mint strategies or capability changes; verdict outcome equals terminal
status; terminal episodes never re-settle; cost priors carry machine-checkable
provenance (`priors_source`), never model prose; hypotheses are admitted only
on measured lift over ≥3-sample groups; mutations need settled-episode
evidence; skill and policy code installs only through the gate and
`lib/skills/`; distillation datasets admit checker-verified verdicts only.
The exploration plane adds its own: observations carry zero learning rights
and never affect verdicts, strategies, mutations or planning; on-demand
reindex only lands untrusted reference links and is receipted by
`reindex_note` observations; frame-change promotion creates draft goals only —
execution still requires the ordinary owner-authorized path. Guardrails are
scoped by plane: thought is free, evidence is bounded, behavior is gated
(see [../EVOLUTION_DESIGN.md](../EVOLUTION_DESIGN.md) exploration loop).

## Ownership

| Concern | Owner |
|---|---|
| Goal-only run workflow, independent review, repair, cancellation and Ask Human | `lib/evolution/runs.py` |
| Durable inline stage birth confirmation, executing task checkpoints/events and one terminal receiver | `runs._launch`, task manager persistence, `test_evolution_durable_stages.py`, [../RUN_CONTINUITY.md](../RUN_CONTINUITY.md) |
| CAS-fenced submission of the original live pending stage | `lib/evolution/stage_dispatch.py`, `tests/evolution/test_evolution_stage_dispatch.py`; real SQLite/TofuDB contention: `test_evolution_durable_dispatch_race.py` |
| Post-hook input archives and one proven unsubmitted cold restoration per frozen stage; unchanged experiment/budget | `lib/evolution/stage_inputs.py`, `lib/evolution/pending_recovery.py`, task manager `prepared.py`, `tests/evolution/test_evolution_pending_recovery.py`; independent process images on SQLite/TofuDB: `test_evolution_cold_process_dispatch.py` |
| Confirmed process-loss and bounded terminal syntax/text-format recovery for frozen read-only reviews; consumption fences | `lib/evolution/review_recovery.py`, `tests/evolution/test_evolution_review_recovery.py`, `tests/evolution/test_evolution_review_format_retry.py` |
| Frozen hypothesis review with separate demand/feasibility/novelty/net-value judgments | `hypothesis_review.py`, `runs.py`, `programs.py`, `programs/hypothesis-assessment.ts`, `test_evolution_hypothesis_review.py` |
| Immutable stage requests, terminal result recovery and owner original-output inspection | `lib/evolution/stage_launch.py`, `lib/evolution/stage_outcomes.py`, `lib/evolution/terminal_checkpoint.py`, `lib/evolution/source_tools.py`, `frontend/src/features/evolution/studio/outcome-reader.ts`, [../STAGE_OUTCOMES.md](../STAGE_OUTCOMES.md) |
| Durable exploration frontier, experiment receipts, archive-backed memory and reaper progression | `lib/evolution/programs.py`, `contracts/evolution_program_v1.schema.json` |
| Reviewed failure diagnosis and candidate frontier admission | `lib/evolution/replanning.py`, `tests/evolution/test_evolution_replanning.py` |
| Executor-authorized problem/scenario/technique hypotheses, frontier adaptation and bounded archive recall | `lib/evolution/exploration_tools.py` |
| Compare two original hypotheses and experiment reviews across loaded pages; explicit original evidence | `frontend/src/features/evolution/programs/hypothesis-comparison.ts`, `testing/programs.test.cjs` |
| Source-bound semantic archive recall, shared-budget child rankings and bounded owner cache | `lib/evolution/semantic_memory.py`, [../EXPLORATION_MEMORY.md](../EXPLORATION_MEMORY.md) |
| Contract loading and typed errors | `lib/evolution/contracts.py` |
| Goal-scoped material shelves and execution-only collection tool | `lib/evolution/library.py` |
| Goal updates, role delivery, acknowledgements | `lib/evolution/coordination.py` |
| On-demand goal spokesperson and follow-up handoff | `lib/evolution/coordinator.py`, `lib/evolution/skills/goal-coordinator/SKILL.md` |
| Owner-scoped records plumbing (keys, bounds, retry) | `lib/evolution/records.py` |
| Goal intake and plan persistence | `lib/evolution/goals.py` |
| Episode lifecycle (open/settle rights) | `lib/evolution/episodes.py` |
| Task-engine bridge (receipt actuals; done ≠ success) | `lib/evolution/bridge.py` |
| Measured cost priors and fallback chain | `lib/evolution/cost_model.py` |
| Resource indexer seam; history/project/academic | `lib/evolution/indexers.py` |
| Deterministic planner; policy slot (R3) | `lib/evolution/planner.py` |
| Strategy templates and anti-patterns | `lib/evolution/strategies.py` |
| Mutation gate and capability-gap queue | `lib/evolution/gate.py` |
| Holdout suite, paired evidence, regression receipts, auto-rollback drill | `lib/evolution/holdout.py`, `lib/evolution/comparison.py` |
| Macro crystallization proposer | `lib/evolution/crystallize.py` |
| Domain plugins (research first; consumed by `runs.py` for `domain='research'` runs) | `lib/evolution/domains.py` |
| Skill forge (R1): author seam, sandbox, install | `lib/evolution/skill_forge.py` |
| Teach channel: owner knowledge → verified skill, episode-evidenced | `lib/evolution/teach.py` |
| `evolution_teach` built-in (binding-scoped exposure) + dispatch handler | `lib/evolution/tool_handler.py`, `lib/tools/registry/_build.py` |
| System awareness snapshot (one producer, every surface) | `lib/evolution/awareness.py` |
| Self-model hypotheses and predictions (R4) | `lib/evolution/self_model.py` |
| Automatic curriculum probes (R2) | `lib/evolution/curriculum.py` |
| Gate thresholds as evolvable policy (R5) | `lib/evolution/gate_policy.py` |
| Verified-trajectory distillation receipts (R6) | `lib/evolution/distill.py` |
| Maintenance heartbeat | `lib/evolution/maintenance.py` |
| Observation ledger (zero-learning-rights noticings, clustering, frame-change promotion) | `lib/evolution/observations.py` |
| Friction miner (struggle without failure → frontier-visible) | `lib/evolution/friction.py` |
| On-demand reindex (actor/settle/freshness triggers, receipt-bound) | `lib/evolution/refetch.py` |
| Evidence-backed experiment reports | `lib/evolution/report.py` |
| Terminal listener (auto settlement) | `lib/evolution/automation.py` |
| `evolution_guidance` context block rendering | `lib/evolution/guidance.py` |
| Criteria checkers (bounded `test_command`) | `lib/evolution/checkers.py` |
| HTTP surface | `routes/api_v1/evolution.py` |
| Primary program notebook, execution studio, goal evidence, capabilities and teaching | `frontend/src/features/evolution.ts`, `frontend/src/features/evolution/` |
| Auto-research workbench page (`#/evolution/research`): direction composer, pipeline console, record archive, production workspace | `frontend/src/features/evolution/research/` |

## Verification

```bash
pytest -q tests/evolution/test_evolution_runs.py tests/evolution/test_evolution_contracts.py tests/evolution/test_evolution_episodes.py \
  tests/evolution/test_evolution_bridge.py tests/evolution/test_evolution_cost_model.py \
  tests/evolution/test_evolution_indexers.py tests/evolution/test_evolution_planner.py \
  tests/evolution/test_evolution_strategies.py tests/evolution/test_evolution_gate.py \
  tests/evolution/test_evolution_checkers.py tests/evolution/test_evolution_automation.py \
  tests/evolution/test_evolution_guidance.py tests/evolution/test_evolution_holdout.py tests/evolution/test_evolution_comparison.py \
  tests/evolution/test_evolution_crystallize.py tests/evolution/test_evolution_domains.py \
  tests/evolution/test_evolution_skill_forge.py tests/evolution/test_evolution_self_model.py \
  tests/evolution/test_evolution_curriculum.py tests/evolution/test_evolution_system.py \
  tests/evolution/test_evolution_end_to_end.py tests/api/test_api_v1_evolution.py \
  tests/evolution/test_evolution_headless_runtime.py tests/evolution/test_evolution_soak.py \
  tests/evolution/test_evolution_observations.py tests/evolution/test_evolution_friction.py \
  tests/evolution/test_evolution_refetch.py tests/evolution/test_evolution_exploration.py
```

Neighbors: `tests/research/test_research_records.py` (records pattern),
`tests/context/test_context_composer.py` (guidance block registry),
`tests/api/test_api_v4_contract.py` (generated meta contract).

## Browser workbench

The primary `#/evolution` notebook creates and starts a durable program with
original acceptance checks, optional project binding, queued experiments and
paged immutable receipts. `programs/controller.ts` owns visible-page reads;
`programs/page.ts` owns stable input forms and explicit commands. See the durable
program section of `docs/EVOLUTION_DESIGN.md` for recovery and memory limits.
The `#/evolution/runs` pixel studio accepts an objective and optional model, then
uses the Headless run lifecycle directly. `workspace.ts` routes between it and
existing detailed evidence pages. `workspace.py` projects bounded run summaries,
owner-selectable model choices, live Ask Human questions, and the unified live
floor: one bounded strip merging non-terminal evolution runs with durable
orchestration runs (goal-mode rows detected through the canonical `chat-goal:`
orchestration id), fail-soft when the orchestration store is unreadable. The
same one-flight read carries the pending gate list (`proposed`/`confirmed`
mutations reduced to presentation fields), and the studio's delivery-table
drawer works those decisions with the shared enable/waiver/reject journey —
receipts render in place, unmeasurable activation needs a second explicit
click. A persistent responsive mission panel shows the goal, actual stage,
failure/recovery guidance, proposed steps, latest recorded work, deliverables,
review evidence and measured usage without requiring a drawer. Missing/null/zero
goal limits mean **No limit**, never a percentage-complete gauge. New general
runs have no default cumulative token, tool, duration or repair ceiling;
explicit limits and legacy authorizations remain enforced. Task links show
terminal receipt status rather than stale execution-stage labels. Preparing a
retry only prefills a new goal form; it never replays side effects or starts
spending. Room-animation pause and execution stop are separate controls.
Review success never claims proven learning. Backend `get_run`/`list_runs` stay
durable-only for authority and eligibility. HTTP detail and `workspace.py` use
explicit `get_run_snapshot`/`list_run_snapshots` presentation reads; terminal
status with `snapshot_pending_reconciliation` never authorizes result journaling
or follow-up spend before the callback settles the run.

Projection authority: `goal-workbench.ts`; controls and expanded inspector:
`studio/goal-command-panel.ts`, `studio/goal-run-inspector.ts`. The room uses
small Canvas2D motion/sprite/dirty-paint modules, not a second task runtime.
Distance-driven gait, fixed foot anchoring, continuous chair transitions and
hidden/idle teardown live in `office-motion.ts`, `office-sprite-geometry.ts`,
`office-pod-motion.ts` and `office-canvas.ts`. Engine selection and performance
measurements: [../EVOLUTION_GAME_ART.md](../EVOLUTION_GAME_ART.md).

Crystallization closes the loop back into authored structure: a tool sequence
recurring in ≥3 winning episodes is proposed as a `flow_template` mutation
carrying a small validated worker→critic→deliverable definition; activation
materializes it into the owner's saved-flow catalogue (Studio flow picker and
Agent Mode choices pick it up with no new surface). Macro recurrence (≥2) and
flow recurrence (≥3) stay distinct signals.

Deferred with rationale: mid-run node editing is rejected (definitions stay
CAS-immutable during a run; forking a new run from an edited copy is the
intervention), a manual pause for durable runs needs a runtime-lifecycle
design of its own rather than a flag on the abort path, and mapping the
fixed executor/reviewer/coordinator vocabulary onto the 13 orchestration
roles is a naming convergence, not a feature — all three are revisited when
run volume justifies them.
Model, resource, artifact and interaction limits live in `EVOLUTION_DESIGN.md`.

Browser checks: `node --test frontend/src/features/evolution/testing/*.test.cjs`,
`pytest -q tests/evolution/test_evolution_studio_browser.py -m visual (TOFU_API_MAJOR=4)`, then the visual
and frontend gates. Refresh/visibility budgets live in the frontend architecture.
The browser-free runtime test uses a real task loop, isolated storage and a
deterministic provider transport. The opt-in live long-run command and its
measurement limits live in `EVOLUTION_DESIGN.md`.

Terminal automation enqueues only owner/task/binding identities and derived
consumption counters after a successful task-result commit. One lazy worker and
`TOFU_EVOLUTION_TERMINAL_QUEUE_CAPACITY` pending receipts (launch-probed personal
4..16, distributed 16, hard maximum 16) bound residency; idle workers retire after
30 seconds. Saturation or shutdown leaves the durable episode awaiting explicit
settlement, never reports a fabricated verdict. Shutdown cancels the active
checker before storage teardown. Criteria execution uses the shared bounded
process runner with an explicit owner, a cumulative 300-second goal deadline,
and capped output. A receipt must match its episode's durable task identity.


### Required execution completion

Managed transient run, coordinator and episode tasks bind an owner/task-scoped
terminal receiver after launch commit. Terminal persistence freezes domain context
before result CAS and acknowledges the receiver before dropping heavy output.
Failed handoffs use maintenance retries; CAS makes duplicate notifications harmless.
Research uses its durable listener. Launch rollback reads the owned run to distinguish
lost ACK from failed commit, then cancels fresh unbound carriers even if episode
settlement fails. Deduped carriers and unknown authority never grant cancellation.
Problem: episode cleanup left costly orphans; rejected: cancel on every save error.
Bound research terminal accounting also folds durable physical model attempts:
retain the larger usage lower bound and require complete journal usage. Problem:
task summaries omit the separate discovery account; rejected: treating missing
usage as free. Storage failures propagate; unbound legacy accounting remains.
Verify bridge and worker-budget tests on both stores.
Verify research-domain tests. Reconciliation covers restart/missed callbacks, preserving
verdict, holdout and learning gates. See `docs/TASK_EXECUTION_MODES.md` for the
shared delivery contract and rejected all-Flow consolidation alternative.

Task activity transport (`frontend/src/features/evolution/task-stream.ts`) uses
the shared API path resolver for deployment prefixes. Each source owns its
callbacks; replacement/disposal detaches them and fences already queued events.
Reconnect has one pending timer, bounded backoff and a monotonic safe cursor.
Malformed records and constructor failure degrade to the studio status poll.
Problem: absolute SSE URLs bypassed transport configuration; late callbacks
could close a replacement source. Decision: share URL policy and make source
ownership explicit. Alternatives: feature-specific prefix parsing duplicates
the deployment policy; polling alone discards the existing live activity feed.

Programme creation accepts optional `research_source={task_id,idea_id}` from an
owned completed discovery. The programme retains a small source identity and
canonical payload digest; its original objective, acceptance and default
scientific frontier remain unchanged. Current executors use
`evolution_explore action=read_research_source` to page the exact selected idea,
survey and gap map. Owner/current-run checks repeat after reads; changed or
unavailable originals refuse without latest-direction fallback. Prior ideas
remain unverified proposals. This seeds the existing long-horizon planner; it
is not automatic continuation of the discovery account or an experiment verdict.
The caller still supplies and starts its authorized programme/budget.
Problem: completed discovery could not seed Programme with source-bound material.
Decision: one original-record reference and digest, using existing read tools.
Alternatives rejected: copying a clipped survey into the objective or inventing
a separate autonomous research loop. No resident cache, worker or new store is
added; the existing programme header gains one bounded reference (representative
fixture <300 encoded bytes). Each page rereads/hashes the complete source payload:
CPU and temporary memory scale with source size; a page grants no replay rights.
Verify `test_evolution_research_source.py` and programme/exploration-tool suites;
source tests use both SQLite and native TofuDB authorities.

The research workbench opens a source-specific Programme intake draft through
the existing creator. Its material reference and editable objective survive
refresh under the current owner/browser scope; unrelated creator drafts remain
separate. The visible note explains that goal/budget apply to a new Programme.
Existing Save plan/Start commands retain activation authority; opening intake
never starts an experiment. Problem: original discovery sources were API-only.
Decision: seed the existing bounded draft store and creator; alternatives
rejected: immediate hidden launch and overwriting an existing draft. Shared UI
factory controls add no new cache/store/worker; source seed metadata remains
within existing draft byte/entry bounds. Verify Programme frontend scenarios
and the live research-to-intake refresh journey.

Programme source reads now use `records.load_completed_idea_source` to validate
selection and metadata in one owned snapshot. Problem: every page loaded the
full record twice, doubling transport/decode/copy work. Decision: shared
completed-selection admission with one read; direction-bound drafting/workspace
callers still compare their expected direction/language. Alternative rejected:
caching mutable originals without revalidation. A 308000-byte survey benchmark
on isolated selected stores used six alternating old/new groups of eight calls:
record.get count fell from 2 to 1; SQLite median 3.30→2.60 ms and native TofuDB
2.98→2.10 ms. This measures source preparation, not end-to-end research speed.
There is no new resident cache, worker or persistent data. Serialization/hash
still scale with the whole source per page. Exact contents/digest remain equal
and source-change refusal remains; verify source/draft/selection suites.

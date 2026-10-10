# Self-evolution package guidance

Design authority: `docs/EVOLUTION_DESIGN.md`; wire authority:
`contracts/evolution_goal_v1.schema.json`, `evolution_plan_v1`,
`evolution_episode_v1` (guards: `tests/evolution/test_evolution_contracts.py`).

- `contracts.py` owns schema loading and the typed errors; nothing persists
  without passing it.
- `programs.py` composes runs into durable exploration frontiers and immutable
  experiment receipts. The original objective/acceptance stay fixed; launch
  intents commit before submission; history stays paged outside the header.
  The serving-loop reaper owns progression, including durable background waits
  and one reviewed join (`test_evolution_background_wait.py`), never a new model
  loop or timer.
- `exploration_tools.py` authorizes current experiment executors against both
  run and program records. Structured problem/scenario/mechanism proposals
  extend the frontier under CAS; archive recall is cursor/chunk bounded. Exact
  original reads use receipt_sources and `test_evolution_original_read.py`.
  Future validation preregistration is immutable under program CAS; default
  synthesis requires its durable record (`test_evolution_preregistration.py`);
  new default validation freezes input/budget and both system generation digests.
  Proposals and source reports carry no verification or learning authority.
- `input_binding.py` derives readonly input plans from the owned frozen protocol;
  admission rechecks executor/project/protocol, never granting command approval.
  Verify `test_evolution_owned_input_binding.py`.
- `scientific_commands.py` freezes owner-bound command originals after hooks and before model budgets; terminal readers close its local capture scope. Verify scientific-command and read-observation archive tests; read-only originals
  never become numerical measurements or grant replay/benchmark authenticity.
  v2 requires direct-parent delivery; v3 separately records authorized frozen
  program-history delivery. source_tools bounds/rechecks it; stage_outcomes archives/restores it.
- `tool_evidence.py` recomputes explicitly precommitted numerical protocols from
  retained command observations; `stage_outcomes.py` preserves late background
  audits in companion joins. Verify `test_evolution_scientific_observations.py`.
- `hypothesis_review.py` binds four independent evidence-axis judgments to the
  immutable run hypothesis; runs own parsing and verdicts, programs own receipts.
  Missing or foreign reviews refuse; unread originals cannot retain positive axes.
- `replanning.py` classifies definitive failures and validates reviewed recovery
  frontiers; original failed checks remain queued. Programs own its CAS; runs
  own diagnosis execution and independent review. Reviewed candidate-generation
  successors keep original controls and immutable failures; programs also exposes
  owned direct-parent data/split reuse facts to independent review, without a validity verdict. Verify
  `test_evolution_validation_generation.py`. `validation_candidate_index.py` owns
  immutable owner/control-scoped failed membership; programs publish roots under
  CAS. Verify `test_evolution_validation_candidate_index.py`.
- `semantic_memory.py` ranks complete archive windows through ordinary read-only
  child invocations, sharing model route and budget. Explicit original-source scopes use owner-bound outcome pages beyond receipt previews; exact quotes bind source kind, digest and offsets;
  the bounded owner cache has no verdict or learning authority. See
  `docs/EXPLORATION_MEMORY.md` and its owning HTTP/contract tests.
- `review_recovery.py` proves local process loss before re-admitting frozen
  read-only reviews. Runs first attempt lossless archived-review projection; unresolved JSON/text-limit failures share a finite allowance,
  archived consumption and the original frozen review; executors never replay. Lost usage remains a lower bound and cannot train costs.
  Executing tasks and living/unknown owners never authorize replay.
- `source_tools.py` reads owner-bound immutable originals. Review-only exposure
  excludes write tools; full acceptance requires bounded coverage receipts from
  successful final provider inputs, frozen with terminal review outcomes.
  Deliverables and saved tool evidence have distinct identities and coverage.
- `executor_delivery.py` binds explicit sole-call answers to the frozen executor launch; ordinary terminal archives and independent review retain acceptance authority. Verify `test_evolution_executor_delivery.py` and the headless runtime gate.
- `review_delivery.py` submits source-backed reviewer judgments through the live
  terminal reply port. Frozen launches authorize submission; ordinary run
  adjudication owns coverage and verdicts. Scientific review also matches the
  sealed executor protocol; drift blocks submission and automatic replay. Preparation never authorizes replay.
- `artifact_evidence.py` owns explicit project-file capture and bounded generation reservations.
  Only current project executors declare; terminal closure retains unavailable captures.
  Streaming fingerprint_path reads share the private-file boundary and retain no
  contents or execution proof (`test_evolution_file_fingerprint.py`). Read receipts
  publish in an immutable chain under artifact-manifest CAS, surviving compaction
  through the existing review source (`test_evolution_fingerprint_receipts.py`).
  Stage outcomes own sealing/review authority; snapshots never imply correctness or value.
- `tool_evidence.py` checks correlated current command observations for frozen
  execution policies (`test_evolution_command_evidence.py`) and captures owned
  background completion snapshots (`test_evolution_background_execution.py`); it streams canonical JSONL of terminal-retained tool messages
  and bounded previews; it cannot recover earlier compaction or upstream clipping.
- `stage_outcomes.py` freezes terminal output, consumption, and exact review
  evidence before receipt progression, including complete owned background
  outputs in the tool-evidence stream. Verified archive recovery never replays
  an executor; chunked owner reads preserve complete output. See `STAGE_OUTCOMES.md`.
- `terminal_checkpoint.py` prepares immutable originals through the existing
  terminal port; the owned task-result CAS commits their frozen context. Only
  authenticated committed capsules and complete digests can materialize outcomes.
- `stage_launch.py` freezes pre-hook prompts/configuration outside the run header;
  explicit owner model changes link immutable pre-allocation successors.
  The run CAS pointer authorizes resumed preparing handoffs; missing live
  executor tasks never authorize side-effect replay. See `RUN_CONTINUITY.md`.
- `stage_inputs.py` archives complete post-hook JSON input; `pending_recovery.py`
  proves each unchanged startup-retired birth before cold restoration. The
  shared task factory skips hooks; runs preserve episode/budgets and own both CAS.
  The shared invocation sink publishes two-bank working contexts and phase
  observations plus owner-bound model/child/tool witnesses; inspection never admits replay (`test_evolution_working_checkpoint.py`).
- `stage_dispatch.py` validates the bound pending birth and resident carrier.
  Runs own submission; admitted tasks never replay. See `RUN_CONTINUITY.md`.
- `run_receipts.py` archives immutable stage history before the run CAS; reads restore the complete legacy projection. Verify `test_evolution_run_receipt_archive.py`; missing archives refuse recovery. `receipt_validation.py` retains bounded schema-success hashes only; every read still verifies original bytes/identity/prefix. Verify `test_evolution_receipt_validation.py`.
- `records.py` owns owner-prefixed keys, size bounds, and acknowledged writes
  with bounded retry on the Sidecar `record.*` operations; no domain policy.
- `goals.py` is intake: goals are admitted under verifiable criteria (or
  explicitly exploratory), plans persist only against an admitted goal.
- `episodes.py` is settlement: opens only against confirmed plans, requires
  verdict/status agreement, restricts exploratory learning rights, and leaves
  a failed settle running instead of faking settlement.
- `bridge.py` projects terminal task-engine runs into receipt actuals
  (`lib.cost.observed_usage_totals` owns cache conventions and usage completeness); it never infers a
  verdict from task status.
- `cost_model.py` aggregates settled episodes into measured cost priors;
  unknown is never zero, the fallback chain is measured_history →
  domain_default → exploratory_flat.
- `indexers.py` owns the resource-indexer seam (`index_all` is the only
  fan-out/merge point): history (settled episodes are the cheapest prior),
  project (bounded fixed-authority GitHub search, fail-soft), and academic
  (wraps `lib/research/literature.py` discovery, never new HTTP policy).
- `planner.py` owns deterministic plan assembly: caller/model proposes
  skeletons, measurable costs always come from the cost model, ranking is
  arithmetic, and every plan persists as `proposed` pending confirmation.
- `strategies.py` distills settled episodes into templates (lineage-bound)
  and anti-patterns (two failures minimum); exploratory episodes contribute
  nothing.
- `gate.py` owns mutation lifecycle (proposed → confirmed → active, with
  rollback restoring the parent chain) and the deduplicated capability-gap
  queue. No evidence, no mutation; confirmation is a distinct act.
- `checkers.py` turns criteria into verdicts; any unknown criterion keeps the
  episode waiting for a human. `test_command` is shell-free, hard-capped.
- `automation.py` installs one fail-soft listener on the task engine's
  post-commit terminal path (`register_terminal_listener`). Its lazy bounded
  receipt queue and start/stop hooks own checker execution; tasks without an
  evolution binding never touch the stores. See `docs/modules/evolution.md`.
- `guidance.py` renders the registered `evolution_guidance` context block
  (see `docs/CONTEXT_INJECTIONS.md`); acquisition lives in the composer,
  rendering here, identity from the composer request.
- `holdout.py` owns the regression probes: `confirm_mutation` runs the
  structural sandbox, `activate_mutation` requires a regression receipt
  (`fail` blocks; `unmeasurable` needs the explicit waiver), and
  `monitor_active_mutations` auto-rolls-back on post-activation degradation.
- `crystallize.py` proposes `macro_tool` mutations from tool sequences that
  recur in winning tasks — recurrence is the evidence, the gate decides.
- `domains.py` adapts existing capabilities (research first) into goal
  payloads and verdicts without rewiring their engines.
- `skill_forge.py` (R1) turns a capability gap into executable skill code:
  the author is an injectable seam (default = one bounded task-engine chat
  turn, never its own loop), the sandbox runs the skill's own tests in a
  plugin-isolated subprocess, and install goes through the gate and
  `lib/skills/` only. Composition (`uses`) requires installed dependencies
  and stays acyclic. Owner-taught `knowledge`/`examples` (the workbench
  Teach form) ground the author; every example becomes a mandatory tested
  behavior — taught knowledge enters only through verification.
- `self_model.py` (R4) is the falsifiable competence model: hypotheses over
  the deterministic feature registry are proposed cheaply (LLM meta-goal or
  caller), data decides admitted/discarded (insufficient data waits), and
  only admitted factors adjust the planner's utility default and failure
  prior. An elegant but non-predictive hypothesis is discarded, not debated.
- `curriculum.py` (R2) admits frontier probe goals (capability gaps,
  data-starved hypotheses, anti-pattern retries) through the ordinary
  intake — deduplicated, capped per run and in total, exploratory rights
  unless machine-checkable criteria are derivable from the retried goals.
- `planner.py` also owns the policy slot (R3): a gate-installed
  `planner-policy-*` skill exporting `score(candidate, context)` governs
  ranking, degrading to the arithmetic rank on any misbehavior. Policy
  fitness is replay-measured in `holdout.py`.
- `gate_policy.py` (R5) makes the gate's own thresholds evolvable through
  ordinary `budget_policy` mutations; `meta_report` is the meta-metric.
- `distill.py` (R6) builds checker-verified trajectory datasets and routes
  distilled models through ordinary routing mutations; the trainer seam is
  explicit, never a hidden no-op.
- `maintenance.py` is the single heartbeat: all learning passes in
  dependency order, fail-soft per step. `report.py` renders the
  evidence-backed experiment report — every number traces to a receipt.
- `teach.py` is the owner-knowledge channel shared by every surface
  (workbench form, API, `TEACH_TOOL_DEFINITION` for chat): forge → sandbox
  → meta-goal episode whose verdict is the sandbox receipt → gate proposal
  with that episode as evidence. Owner say-so is input, never proof.
- `tool_handler.py` binds the `evolution_teach` built-in; the ToolSpec's
  exposure gate (and `lib/tools/routing.py`) shows it only for tasks whose
  config carries an evolution binding — ordinary chat never sees it — and
  the handler re-checks the binding because exposure is prompt policy, not
  authority.
- `awareness.py` is the single snapshot producer; surfaces render system
  state from it, never from ad-hoc store reads.

Durable identities use SHA-256 digests, never `hash()`, so deduplication
survives process restarts. The API surface is `routes/api_v1/evolution.py`.

Editing rules:

- Execution, LLM loops, and event journals belong to the task engine; this
  package never grows a parallel one.
- Learning writes must trace to episode receipts; cost priors carry machine-
  checkable provenance (`priors_source`), never model prose.
- Owner identity stays an explicit parameter through every read/write.

Verify with `pytest -q tests/evolution/test_evolution_contracts.py tests/evolution/test_evolution_episodes.py`,
then the neighboring `tests/research/test_research_records.py` when `records.py`
plumbing changes.

- `programs.py` binds optional prior discovery sources at creation;
  `exploration_tools.read_research_source` serves exact owner/current-task pages.
  Verify `test_evolution_research_source.py`; proposals grant no acceptance or
  budget continuation rights.

- `episodes.EpisodeReadWindow` owns recent settlements and referenced goals
  for one lazy, owner-bound maintenance view. Maintenance passes explicitly receive it; it closes after each cycle.
  Verify `test_evolution_maintenance_window.py` for isolation, freshness, read
  counts and failures; `test_evolution_recent_learning.py` covers temporal
  selection beyond prefix caps, exact parents, paging and restart. Ordinary
  lists retain their existing repository ordering.

- `policy_transitions.py` owns frozen budget-policy preimages and CAS-fenced
  effect/rollback ACKs; `evolution_policy_transition_v1` owns its journal shape.
  Verify `test_evolution_policy_rollback.py`; complete mutation-history folds
  and parent visibility are guarded by `test_evolution_mutation_history.py`.

- `mutation_versions.py` reserves owner/kind versions with storage CAS before
  proposal publication. Its durable head survives publication failure; verify
  `test_evolution_mutation_versions.py` for contention, ACK ambiguity and restart.

# Research execution and workspace

Map: `docs/modules/ingest_media.md`; scientific validity lives in
`docs/RESEARCH_CYCLE.md`, compilation in `docs/PAPER_BUILDS.md`.

- `program.py` normalizes `contracts/research_program_v1.schema.json` and
  derives readiness. `workspace.py` owns revision CAS and server-written
  compilation evidence and completed observations; request bodies cannot erase
  or replace those fields.
- `action.py` admits new experiments only with a saved numeric rule; API and
  worker checks live together, verified by `test_research_rule_admission.py`.
  It runs explicit production actions and captures scientific receipts
  before shared tool-result presentation; verify capture/archive immutability in
  `tests/research/test_research_observation_capture.py`. Compilation enters
  `compilation.py` and the shared `lib/paper/builds/` scheduler; other actions
  retain the guarded agent/tool boundary. Do not restore a model compiler.
- `manuscript.py` owns source scaffolding/export. Explicit source records are
  frozen by `lib/paper/builds/records.py`, not a second TeX implementation.
- `measurements.py` constructs paired reports from measured seed/value records;
  `observation_audit.py` owns their shared statistics. Verify `test_research_measurements.py`.
- `observation_audit.py` verifies computable reported measurements; no model
  verdict establishes numerical consistency. Verify through
  `test_research_observation_audit.py` and `test_research_audit_migration.py`.
- `history.py` owns immutable paged experiment observations and current-scope
  aggregates; the workspace CAS publishes its head. Its verification suite is
  `tests/research/test_research_history.py`.
- `action_observations.py` journals original experiment-tool results before
  model projection on the selected record authority. Its schema and restart/ACK
  guards are in `test_research_action_observations.py`; it never replays effects.
- `action_budget.py` normalizes explicit cumulative policy from the observation
  contract and projects its HTTP schema; verify action and production-start tests.
- `model_accounting.py` owns logical model-call intents and cumulative usage
  checkpoints; verify `test_research_model_accounting.py` before changing paid-call
  admission or response/tool ordering. Meter recovery guards live in
  `test_research_meter_recovery.py`; no record grants replay or budget reset.
- `action_terminal.py` is the opt-in TaskRuntime terminal durability port;
  its retained result serves cold task queries; restart, ACK, pending-repair
  and ownership guards live in
  `test_research_action_terminal.py`. It never reruns execution.
- `request_context.py` retains original action definitions and exact private model request material with
  task-scoped chunk sharing; verify `test_research_request_context.py`. Stored
  context does not grant tool authority, replay or budget reset.
- `model_response.py` acknowledges exact model replies before usage/tool
  progression; verify `test_research_model_response.py`. Stored tool calls
  are untrusted response data and are never replayed by inspection.
- `loop_checkpoint.py` binds completed-round messages, tools, usage and chassis
  state before the next provider dispatch; verify
  `test_research_loop_checkpoint.py`. Restoration grants no execution ownership.
- `history_context.py` pages exact completed history through existing artifacts;
  its contract, context-boundary and local-chain tests guard archival/restoration.
- `action_continuation.py` prepares original policy, workspace and tool epoch
  before a dead-process claim transfer. `action.py` restores that typed context
  in the existing worker; verify `test_research_action_process_continuation.py`.
  `resume_action` registers one worker for an explicit owned resume command;
  scheduler/refusal and dedup guards live in `test_research_resume_entry.py`.
  `action_startup.py` resumes proven-dead eligible actions after readiness,
  through the same command. Verify `test_research_action_startup.py` and the
  fresh-process continuation suite; inspection never grants replay.
- `action_discovery.py` projects owned unfinished actions for cold workbench
  reopening. It reads metadata only and never grants execution; verify
  `test_research_action_discovery.py` and the shipped-page resume browser suite.
- `action_pending.py` owns the reconstructible unfinished lookup. Initial claims
  retain its pointer; acknowledged terminal persistence retires it. Startup
  prioritizes indexed work and reconciles original journals afterward. Verify
  `test_research_pending_index.py`; lookup data grants no execution authority.
- `execution_owner.py` registers one agent executor before paid dispatch and
  fences model/tool/checkpoint progression; verify
  `test_research_execution_owner.py`. Process loss does not itself grant replay.
- `execution_observation.py` retains producer job state/identity before output
  truncation under the program receipt schema; verify
  `test_research_execution_observation.py` and cross-action
  `test_research_pending_lineage.py`; completed-source replay guards live in
  `test_research_settled_replay.py`. It never certifies quiescence.
- `source_index.py` owns immutable consumed-source lookup across archival;
  workspace CAS publishes its root. Verify `test_research_source_index.py`
  and `test_research_source_publication.py` for publication/ACK failures. Full
  paged source consumption/backfill uses `test_research_paged_sources.py`.
- `progress_index.py` prepares run/basis-scoped pending source/job tries;
  `test_research_progress_index.py` guards delta parity, scope, ACK and restart.
  `progress_checkpoint.py` binds index deltas to published receipt heads; verify
  `test_research_progress_publication.py`. Workers use the published checkpoint
  for source/job previews (`test_research_indexed_polls.py`); terminal settlement
  retains complete-history verification.
- `receipt_pages.py` prepares immutable, byte-bounded unfinished receipt pages;
  `test_research_receipt_pages.py` covers scope, integrity, ACK and restart.
  Workspace CAS publication uses its typed preparation port; verify
  `test_research_receipt_publication.py`. Complete chronological reads use
  `workspace.open_run_receipts`; verify `test_research_receipt_stream.py` for
  ordering, snapshot authority and cleanup. `workspace.read_run_execution_state`
  uses published progress indexes with a legacy streaming fallback; verify
  `test_research_execution_reducer.py`. `science_reduction.py` shares complete
  outcome/numeric/audit folds between decisions and workspace inspection; verify
  `test_research_science_stream.py`. Workers continue and page already-paged
  unfinished runs; `test_research_pending_paging.py` verifies restart/replay/CAS.
  `receipt_settlement.py` publishes fully revalidated failed paged runs; verify
  `test_research_paged_failure.py`. Success recomputes full bound receipt_evidence;
  `test_research_paged_success.py` covers atomic science/source publication and
  archival. Workers activate paging for new pending runs; the browser count uses
  full history plus inline tail (`test_research_paged_success_browser.py`).
  `original_science.py` recovers oversized science from bound journal originals;
  `test_research_original_science.py` guards scope, digest, ambiguity and restart.
- `records.py` owns resumable research-run projections; these are separate
  from manuscript build receipts and workspace revision state. Startup restores
  acknowledged checkpoints before task birth and rejects terminal snapshots;
  verify `test_research_startup_checkpoint.py`. Exact snapshot/index ACK and
  lost-response guards live in `test_research_record_ack.py`.

Verify changed boundaries in `tests/research/test_research_compilation.py`, workspace,
action, result-retention (`test_research_result_retention.py`), workspace evidence
guards (`test_research_workspace_evidence_guard.py`) and scientific-validity suites. The real compile/download/failure
browser journey is `tests/research/test_research_compile_browser.py`.

The guarded loop → actual local command → observation/model journals → workspace
CAS → terminal chain is covered on both stores by `test_research_local_action_chain.py`
with controlled provider frames; this is lifecycle evidence, not a scientific result.

- `budget_binding.py` binds model-only policy immutably to an owned acknowledged
  task identity; `contracts/research_model_budget_v1.schema.json` owns its shape.
  Verify `test_research_budget_binding.py`; no binding alone authorizes recovery.

- `model_attempt_journal.py` adapts the shared execution budget observer to
  acknowledged physical send/terminal records; fresh accounts refuse prior
  attempts. Verify `test_research_model_attempt_journal.py`; recovery is separate.

- `model_attempt_journal.summarize_model_attempts` folds bounded receipt pages;
  missing/unknown attempts remain incomplete. Verify `test_research_model_attempt_summary.py`.

- `discovery_owner.py` binds one immutable process-incarnation claim to the
  discovery model policy; inspection never grants takeover. Verify
  `test_research_discovery_owner.py`; action ownership stays basis-scoped.

- `engine.py` activates acknowledged model policy, discovery claim and attempt
  accounting before recipe entry. `test_research_worker_budget.py` covers worker
  wiring, fresh-account recovery refusal and claim loss around intent ACK.

- Manual resume checks model policy and recovers eligible active accounts before
  task birth; public
  `resumable` agrees with that admission. Verify
  `test_research_budget_resume_admission.py` and startup checkpoint tests.

- Startup budget recovery transfers a versioned discovery claim after proven
  process death and complete usage, retaining its predecessor. Verify
  `test_discovery_account_recovery.py` before changing account/owner recovery.

- `can_recover_discovery_execution` is only a read-only eligibility hint; manual
  resume repeats recovery/CAS. Verify `test_discovery_manual_recovery.py`.

- `records.load_idea_source` owns completed selection reads; workspace saves
  retain their task ID. `program.hypothesis_identity_payload` also owns action
  freeze identity. Verify selection-source and local-action-chain tests.

Independent paper/research judge, terminology and podcast work uses production
`owned_results` with a copied caller context per job. No local worker window
or mandatory first-call warm-up may gate these siblings; original budget
ports, caller-owned result order and joined cleanup remain authoritative.

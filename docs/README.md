# Documentation map

This directory describes the system that runs now. Git history is the archive:
completed plans, incident narratives, one-off audits, and superseded designs do
not remain beside current contracts. The machine-readable navigation inventory
is [`catalog.json`](catalog.json). Documentation changes have no automatic
catalog, length, link, prose, or generated-index gate.

## First reads for a model

1. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) for process and dependency
   boundaries.
2. Read the one domain map under [`modules/`](modules/) that owns the change.
3. Read the relevant contract below; contracts override narrative examples.
4. Find the smallest tests named by the domain map before editing.

| Change | Runtime owner | Contract / map |
|---|---|---|
| Development, affected tests, candidate verification and CI gates | `scripts/test_select.py`, `.github/workflows/ci.yml` | [`TESTING_STRATEGY.md`](TESTING_STRATEGY.md#development-and-delivery-loop) |
| Repository layout, first-party packages, versioning, releases, checkout relocation | root, `packages/`, `plugins/` | [`MONOREPO.md`](MONOREPO.md) |
| Rust migration priorities, equivalent-work evidence and regression admission | existing domain owners, `packages/tofu-runtime/`, `packages/tofu-db/` | [`MONOREPO.md`](MONOREPO.md#rust-migration-quality) |
| Embed/package/serve the agent without application storage or the full Tofu UI (includes a small model-routing v2 setup page) | `tofu_agent/`, transient paths in `lib/tasks_pkg/` | [`DEVELOPER_RUNTIME.md`](DEVELOPER_RUNTIME.md), [`HEADLESS_API.md`](contracts/HEADLESS_API.md) |
| App assembly, boot, shutdown | `lib/app/app_assembly.py`, `lib/production_lifecycle.py`, `lib/serving_loop_lifecycle.py` | [`modules/infra_runtime.md`](modules/infra_runtime.md) |
| HTTP, auth, errors | `routes/`, `lib/api_response.py`, `lib/error_envelope/` | [`API_CONTRACT.md`](contracts/API_CONTRACT.md), [`VERSIONING.md`](contracts/VERSIONING.md), [`API_V4_MIGRATION.md`](contracts/API_V4_MIGRATION.md) (cutover complete — v4 mechanism reference), [`modules/auth_providers_billing.md`](modules/auth_providers_billing.md), [`CODEX_ACCOUNT_USAGE.md`](runbooks/CODEX_ACCOUNT_USAGE.md) |
| Accounts, owners, credentials | `lib/identity.py`, `lib/api_keys/`, Sidecar `identity` domain | [`IDENTITY.md`](IDENTITY.md), [`contracts/identity_v1.yaml`](../contracts/identity_v1.yaml) |
| Conversation turns and live state | `contracts/conversation_sync_v3.yaml`, `lib/conversation_sync/`, `routes/conversation_sync_v3.py`, `frontend/src/core/conversation-sync.ts` | [`CONVERSATION_SYNC_V3.md`](contracts/CONVERSATION_SYNC_V3.md) |
| Conversation delete, restore, clone | `contracts/conversation_lifecycle_v1.yaml`, `routes/conversations.py`, `lib/storage_authority_sql/operations_pkg/_conversations.py` | [`API_CONTRACT.md`](contracts/API_CONTRACT.md), [`STORAGE.md`](STORAGE.md) |
| Sidebar conversation search | `lib/conversations/search.py`, `lib/storage_authority_sql/turn_search_query.py`, `frontend/src/sidebar-search-controller.ts` | [`contracts/conversation_search_v2.json`](../contracts/conversation_search_v2.json), [`STORAGE.md`](STORAGE.md) |
| Durable data | `lib/storage/`, `lib/storage_authority_sql/`, `packages/tofu-db/` | [`STORAGE.md`](STORAGE.md), [`STORAGE_TOFUDB.md`](STORAGE_TOFUDB.md), [`STORAGE_OPERATIONS.md`](STORAGE_OPERATIONS.md), [`modules/data_tier.md`](modules/data_tier.md) |
| Entity B+Tree page formats and packing | `packages/tofu-db/src/entity_page_codec.rs`, `entity.rs` | [`TOFUDB_ENTITY_PAGES.md`](TOFUDB_ENTITY_PAGES.md) |
| Compact native search postings and generation IDs | `packages/tofu-db/src/turn_search_projection.rs` | [`TOFUDB_SEARCH_POSTINGS.md`](TOFUDB_SEARCH_POSTINGS.md) |
| Snapshot-certified native query result reuse | `packages/tofu-db/src/query_result_cache.rs`, `authority_query_view.rs` | [`TOFUDB_QUERY_RESULT_CACHE.md`](TOFUDB_QUERY_RESULT_CACHE.md) |
| Long-history native context materialization and SQLite comparison | `packages/tofu-db/src/turn/model_context.rs`, `benchmarks/tofudb_context_read.py` | [`TOFUDB_CONTEXT_READ.md`](TOFUDB_CONTEXT_READ.md) |
| Embedded command dependency waits and carrier lifecycle | `packages/tofu-db/src/embed_command_phases.rs` | [`TOFUDB_EMBEDDED_PHASES.md`](TOFUDB_EMBEDDED_PHASES.md) |
| Native whole-authority local front and asynchronous recovery shadow | `packages/tofu-db/src/fastpath.rs`, `packages/tofu-db/src/online_backup.rs` | [`TOFUDB_FASTPATH.md`](TOFUDB_FASTPATH.md) |
| Task / agent execution | `lib/tasks_pkg/`, `lib/agent_core/`, `lib/swarm/` | [`modules/task_engine.md`](modules/task_engine.md), [`TASK_EXECUTION_MODES.md`](TASK_EXECUTION_MODES.md), [`AGENT_LIFECYCLE.md`](AGENT_LIFECYCLE.md), [`RETAINED_AGENTS.md`](RETAINED_AGENTS.md), [`EVENTS.md`](contracts/EVENTS.md), [`TASK_EVENT_DELIVERY.md`](TASK_EVENT_DELIVERY.md) |
| Orchestration definitions and runs | `lib/orchestration/`, `routes/api_v1/orchestration_*` | [`modules/orchestration_dag.md`](modules/orchestration_dag.md), [`ORCHESTRATION_PRODUCT_SURFACES.md`](ORCHESTRATION_PRODUCT_SURFACES.md) |
| Run interruption recovery and long-horizon continuity | `lib/orchestration/run_recovery.py`, `lib/orchestration/runtime_start_service.py`, `lib/goal_runs/`, `lib/evolution/runs.py` | [`RUN_CONTINUITY.md`](RUN_CONTINUITY.md), [`STAGE_OUTCOMES.md`](STAGE_OUTCOMES.md), [`TASK_EXECUTION_MODES.md`](TASK_EXECUTION_MODES.md) |
| Models, routing, and provider I/O | `lib/model_routing/`, `lib/llm/`, `lib/llm_dispatch/`, `lib/protocols.py`, `lib/local_serve/`, `lib/model_profiles/`, `lib/provider/provider_template_recipes.py` | [`contracts/model_routing_v2.schema.json`](../contracts/model_routing_v2.schema.json), [`modules/llm_io.md`](modules/llm_io.md), [`LLM_RUNTIME_PROFILES.md`](LLM_RUNTIME_PROFILES.md), [`MODEL_REGISTRATION.md`](MODEL_REGISTRATION.md), [`PROVIDER_TEMPLATE_RECIPES.md`](PROVIDER_TEMPLATE_RECIPES.md) |
| Image builds, workspace provisioning, third-party platform adapters | `packages/tofu-environments/` | [`ENVIRONMENT_PROVIDERS.md`](ENVIRONMENT_PROVIDERS.md) |
| Tools and MCP | `lib/tools/`, `lib/mcp/` | [`modules/tools_execution.md`](modules/tools_execution.md), [`TOOL_EXECUTION_POLICY.md`](contracts/TOOL_EXECUTION_POLICY.md), [`TOOL_RESULT_QUERY.md`](contracts/TOOL_RESULT_QUERY.md), [`TOOL_PLUGINS.md`](contracts/TOOL_PLUGINS.md) |
| Project command tracking and undo | `lib/project_mod/` | [`PROJECT_FILE_UNDO.md`](PROJECT_FILE_UNDO.md) |
| Skills | `lib/skills/`, `routes/api_v1/skills.py` | [`modules/skills.md`](modules/skills.md), [`AUTOMATION_SKILL_SOURCES.md`](AUTOMATION_SKILL_SOURCES.md) |
| Browser automation and site adapters | `lib/browser/`, `routes/browser.py`, `browser_extension/` | [`modules/browser_automation.md`](modules/browser_automation.md) |
| Site packs, release-tiered bundles, custom site/provider onboarding | `lib/site_packs/`, `bundles/`, `lib/auth_sources.py`, `lib/provider/provider_template_recipes.py` | [`SITE_PACKS.md`](SITE_PACKS.md) |
| Browser UI | `frontend/src/`; retained runtime under `frontend/src/runtime/sections/`; styles under `frontend/src/styles/` | [`FRONTEND_ARCHITECTURE.md`](FRONTEND_ARCHITECTURE.md), [`RENDER_CONTRACT.md`](RENDER_CONTRACT.md), [Tool-card header](../frontend/src/conversation/README.md#tool-card-header) |
| Design system: tokens, shared controls, gallery | `frontend/src/styles/application/00-design-tokens.css`, `frontend/src/styles/application/30-ui-kit.css`, `frontend/src/ui/`, `designs/ui-gallery/` | [`DESIGN_SYSTEM.md`](DESIGN_SYSTEM.md) |
| VS Code forwarded ports and constrained reverse proxies | `lib/control_rpc.py`, `lib/static_mirror.py`, `frontend/src/api/transport.ts`, retained Project/push/log sections | [`contracts/control_rpc_v1.yaml`](../contracts/control_rpc_v1.yaml), [`PROXY_RUNTIME.md`](PROXY_RUNTIME.md) |
| Project coordination and Git publication | `lib/conversations/`, `lib/project_mod/`, `lib/integration_control.py` | [`modules/conversations_project_brain.md`](modules/conversations_project_brain.md), [`modules/project_integration.md`](modules/project_integration.md), [`GIT_WORKSPACE_GUARD.md`](runbooks/GIT_WORKSPACE_GUARD.md) |
| Project file history, undo, redo | `lib/file_history/`, `lib/project_mod/modifications.py` | [`modules/file_history.md`](modules/file_history.md) |
| Media, papers, and Research Foundry (engine; the workbench is an Evolution page) | `lib/media_attachments.py`, `lib/knowledge/`, `lib/video_analysis/`, `lib/paper/`, `lib/research/`, `lib/motion_video/` | [`contracts/research_program_v1.schema.json`](../contracts/research_program_v1.schema.json), [`modules/ingest_media.md`](modules/ingest_media.md) |
| TeX builds, PDF/SyncTeX receipts and preview scheduling | `lib/paper/builds/`, `routes/api_v1/paper_builds.py` | [`contracts/paper_build_v1.schema.json`](../contracts/paper_build_v1.schema.json), [`modules/ingest_media.md`](modules/ingest_media.md) |
| PID-anchored paper citations (OpenAlex/Crossref search, insert, audit) | `lib/paper/citations.py`, `lib/tasks_pkg/handlers/paper_citations.py`, `lib/tools/paper_citations.py` | [`PAPER_CITATIONS.md`](PAPER_CITATIONS.md) |
| Paper quality gate, review dossier and scaffold (`paper_check`/`paper_review`/`paper_scaffold`) | `lib/paper/checks.py`, `lib/paper/review_dossier.py`, `lib/tools/paper_quality.py`, `lib/tasks_pkg/handlers/paper_quality.py` | [`PAPER_QUALITY.md`](PAPER_QUALITY.md) |
| Long-running deliverable production | `lib/production/` plus capability recipes | [`modules/production.md`](modules/production.md) |
| Chat artifacts, existing-file delivery and artifact/Turn owner review | `lib/acceptance/`, `lib/artifacts/`, Sidecar artifact operations, native `artifact.rs` | [`modules/task_engine.md`](modules/task_engine.md), [`response_file_v1.schema.json`](../contracts/response_file_v1.schema.json), [`ARTIFACT_REVIEW.md`](contracts/ARTIFACT_REVIEW.md) |
| Shared Python worker lifecycle | `tofu_workers/`, `lib/agent_core/worker_executor.py` | [`modules/task_engine.md`](modules/task_engine.md), [`modules/tools_execution.md`](modules/tools_execution.md) |
| Automatic resource budgets and live capacity | `contracts/resource_policy_v1.json`, `runtime_guards.py`, `lib/resource_control/` | [`RESOURCE_CONTROL.md`](RESOURCE_CONTROL.md) |
| Scheduling and operations | `lib/scheduler/`, `lib/daily_report/` | [`modules/scheduling_ops.md`](modules/scheduling_ops.md), [`RELIABILITY_RUNBOOK.md`](runbooks/RELIABILITY_RUNBOOK.md) |
| Desktop devices, remote worktrees, egress | `lib/desktop/`, `lib/desktop_agent/`, `lib/desktop_dist/` | [`modules/remote_execution.md`](modules/remote_execution.md) |
| Goal-driven evolution and spokesperson conversation | `lib/evolution/`, `frontend/src/features/evolution/` | [`modules/evolution.md`](modules/evolution.md), [`EVOLUTION_DESIGN.md`](EVOLUTION_DESIGN.md), [`EXPLORATION_MEMORY.md`](EXPLORATION_MEMORY.md), [`EVOLUTION_GAME_ART.md`](EVOLUTION_GAME_ART.md) |
| Experiments and strategy decisions | `contracts/experiments_v1.schema.json`, `lib/experiments/`, `lib/cost_experiments/cost_experiments.py` | [`modules/experiments.md`](modules/experiments.md) |
| Conversation working facts, source versions and owner corrections | `lib/memory/working_facts/`, common context composer | [`WORKING_FACTS.md`](contracts/WORKING_FACTS.md) |
| Automatic context injections, necessity policy and Request Inspector receipts | `lib/tasks_pkg/context_composer/` | [`CONTEXT_INJECTIONS.md`](CONTEXT_INJECTIONS.md), [`modules/context_engineering.md`](modules/context_engineering.md) |
| Long-agent context, tool-cost controls, and paired Codex evaluation | `lib/tasks_pkg/context_composer/`, `lib/tools/`, `lib/benchmark_contract.py`, `evaluations/{codex_kimi_proxy,long_agent_release}/` | [`LLM_COST_OPTIMIZATION.md`](LLM_COST_OPTIMIZATION.md), [`modules/context_engineering.md`](modules/context_engineering.md), [`CONTEXT_COMPACTION.md`](CONTEXT_COMPACTION.md), [`CONTEXT_BUDGET_POLICY.md`](CONTEXT_BUDGET_POLICY.md), [`modules/tools_execution.md`](modules/tools_execution.md) |
| Benchmark catalog, Terminal-Bench / SWE-bench runs and traceable Tofu–OpenCode comparisons | `evaluations/catalog.py`, `evaluations/swebench/`, `rootless_vm/` | [`../evaluations/swebench/README.md`](../evaluations/swebench/README.md), [`ROOTLESS_VM_SANDBOX.md`](ROOTLESS_VM_SANDBOX.md) |
| Logging and incident diagnosis | `lib/log*.py`, `lib/incident_journal.py` | [`LOGGING.md`](LOGGING.md), [`RELIABILITY_RUNBOOK.md`](runbooks/RELIABILITY_RUNBOOK.md) |

## Authority rules

- Machine-readable contracts are edited first; generated clients and catalogs
  are outputs. Never hand-edit a generated file.
- A concept has one owner. A fallback may degrade a capability, but it may not
  become a second state machine, transport, repository, or error taxonomy.
- New persisted access carries explicit user identity through a repository or
  semantic storage operation. Routes do not contain SQL.
- New frontend behavior is a normal TypeScript module. Retained runtime
  sections may shrink or move to TypeScript; no new section is added.
- Plans live in the active task while work is underway. When the change lands,
  retain only the resulting contract and rationale that is still true.
- Tests specify outcomes and failure semantics. Do not pin incidental source
  text, private function locations, or compatibility behavior that was removed.

## Documentation lifecycle

Keep `catalog.json` useful as navigation. A product change updates its
authority document in the same diff. If a document becomes
historical, delete it after moving any still-valid invariant into the authority
document; do not create an in-repository archive folder.

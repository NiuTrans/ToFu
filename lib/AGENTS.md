# Backend library guidance

## Scope and dependency direction

`lib/` contains application, execution, persistence-client, and operational
modules. Read `docs/ARCHITECTURE.md` and the owning domain map before editing.
Dependencies flow from delivery to application/execution to repository/storage
ports; backend libraries never import routes or browser owners.

## Domain routing

| Concern | Primary owners | First map |
|---|---|---|
| Boot and process lifecycle | `app_*`, `server_*`, `production_lifecycle.py`, `process_roles.py` | `docs/modules/infra_runtime.md` |
| Identity, providers, OAuth, billing | `identity.py`, `api_keys/`, `oauth/`, `billing/`, `llm_dispatch/` | `docs/modules/auth_providers_billing.md` |
| Tasks and agent execution | `tasks_pkg/`, `agent_core/`, `swarm/` | `docs/modules/task_engine.md` |
| Orchestration graphs | `orchestration/`, `orchestration_*.py` | `docs/modules/orchestration_dag.md` |
| Model I/O | `llm/`, `llm_dispatch/`, `model_*`, `pricing/` | `docs/modules/llm_io.md` |
| Tools, MCP, browser | `tools/`, `mcp/`, `browser/`, `project_mod/` | `docs/modules/tools_execution.md` |
| Context and memory | `tasks_pkg/context_composer/`, `tasks_pkg/compaction/`, `memory/`, `token_counter/` | `docs/modules/context_engineering.md` |
| Conversations and project state | `conversation_sync/`, `conversations/`, `project_mod/`, `presence/` | `docs/modules/conversations_project_brain.md` |
| Durable data | `storage/`, `storage_sidecar/` | `docs/modules/data_tier.md` |
| Papers and media | `paper/`, `pdf_parser/`, `translate/`, `motion_video/`, `knowledge/` | `docs/modules/ingest_media.md` |
| Scheduling and production | `scheduler/`, `daily_report/`, `optimizer/`, `production/` | `docs/modules/scheduling_ops.md` |
| Remote execution | `desktop/`, `desktop_agent/`, `desktop_dist/` | `docs/modules/remote_execution.md` |
| Skills and experiments | `skills/`, `experiments/` | `docs/modules/skills.md`, `docs/modules/experiments.md` |

## Editing rules

- Give each module one responsibility, explicit entry points/dependencies, and
  one owner for policy/state. Cross-domain calls use public protocols or
  committed events, never private-module reach-through.
- Carry principal/owner identity explicitly. Application code uses semantic
  repositories; SQL, backend selection, and database paths stay in the storage
  sidecar.
- Preserve typed failures, idempotency, rollback, cancellation, fault-injection
  seams, and user-visible recovery. Do not turn a fallback into a second state
  machine or authority.
- Bound every cache, queue, pool, task, stream, retry, temporary artifact, and
  background lifecycle. Optional heavy dependencies load lazily.

## Verification

Run the smallest tests named by the domain map, then neighboring boundary tests.
Use `python3 scripts/check_architecture.py` for dependency/ownership changes and
the relevant `make test-unit` or `make test-api` gate once focused tests pass.

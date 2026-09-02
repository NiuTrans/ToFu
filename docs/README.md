# Documentation map

This directory describes the system that runs now. Git history is the archive:
completed plans, incident narratives, one-off audits, and superseded designs do
not remain beside current contracts. The exhaustive machine-readable inventory
is [`catalog.json`](catalog.json), enforced by
`python3 scripts/check_documentation.py`.

## First reads for a model

1. Read [`ARCHITECTURE.md`](ARCHITECTURE.md) for process and dependency
   boundaries.
2. Read the one domain map under [`modules/`](modules/) that owns the change.
3. Read the relevant contract below; contracts override narrative examples.
4. Find the smallest tests named by the domain map before editing.

| Change | Runtime owner | Contract / map |
|---|---|---|
| Embed/package/serve the agent without application storage or ChatUI (includes a small Provider setup page) | `tofu_agent/`, transient paths in `lib/tasks_pkg/` | [`DEVELOPER_RUNTIME.md`](DEVELOPER_RUNTIME.md), [`HEADLESS_API.md`](HEADLESS_API.md) |
| App assembly, boot, shutdown | `lib/app_assembly.py`, `lib/production_lifecycle.py`, `lib/serving_loop_lifecycle.py` | [`modules/infra_runtime.md`](modules/infra_runtime.md) |
| HTTP, auth, errors | `routes/`, `lib/api_response.py`, `lib/error_envelope/` | [`API_CONTRACT.md`](API_CONTRACT.md), [`modules/auth_providers_billing.md`](modules/auth_providers_billing.md) |
| Accounts, owners, credentials | `lib/identity.py`, `lib/api_keys/`, Sidecar `identity` domain | [`IDENTITY.md`](IDENTITY.md), [`contracts/identity_v1.yaml`](../contracts/identity_v1.yaml) |
| Conversation turns and live state | `contracts/conversation_sync_v3.yaml`, `lib/conversation_sync/`, `routes/conversation_sync_v3.py`, `frontend/src/core/conversation-sync.ts` | [`CONVERSATION_SYNC_V3.md`](CONVERSATION_SYNC_V3.md) |
| Conversation delete, restore, clone | `contracts/conversation_lifecycle_v1.yaml`, `routes/conversations.py`, `lib/storage_sidecar/operations_pkg/_conversations.py` | [`API_CONTRACT.md`](API_CONTRACT.md), [`STORAGE.md`](STORAGE.md) |
| Durable data | `lib/storage/`, `lib/storage_sidecar/` | [`STORAGE.md`](STORAGE.md), [`modules/data_tier.md`](modules/data_tier.md) |
| Task / agent execution | `lib/tasks_pkg/`, `lib/agent_core/` | [`modules/task_engine.md`](modules/task_engine.md), [`EVENTS.md`](EVENTS.md) |
| Models and provider I/O | `lib/llm/`, `lib/llm_dispatch/`, `lib/model_profiles/`, `lib/provider_template_recipes.py` | [`modules/llm_io.md`](modules/llm_io.md), [`MODEL_REGISTRATION.md`](MODEL_REGISTRATION.md), [`PROVIDER_TEMPLATE_RECIPES.md`](PROVIDER_TEMPLATE_RECIPES.md) |
| Tools and MCP | `lib/tools/`, `lib/mcp/` | [`modules/tools_execution.md`](modules/tools_execution.md), [`TOOL_PLUGINS.md`](TOOL_PLUGINS.md) |
| Skills | `lib/skills/`, `handlers/skills.py`, `routes/api_v1/skills.py` | [`modules/skills.md`](modules/skills.md) |
| Browser automation and site adapters | `lib/browser/`, `routes/browser.py`, `browser_extension/` | [`modules/browser_automation.md`](modules/browser_automation.md) |
| Browser UI | `frontend/src/`; retained runtime under `frontend/src/runtime/sections/`; styles under `frontend/src/styles/` | [`FRONTEND_ARCHITECTURE.md`](FRONTEND_ARCHITECTURE.md), [`RENDER_CONTRACT.md`](RENDER_CONTRACT.md) |
| VS Code forwarded ports and constrained reverse proxies | `lib/control_rpc.py`, `lib/static_mirror.py`, `frontend/src/api/transport.ts`, retained Project/push/log sections | [`contracts/control_rpc_v1.yaml`](../contracts/control_rpc_v1.yaml), [`PROXY_RUNTIME.md`](PROXY_RUNTIME.md) |
| Project coordination and Git publication | `lib/conversations/`, `lib/project_mod/`, `lib/integration_control.py` | [`modules/conversations_project_brain.md`](modules/conversations_project_brain.md), [`modules/project_integration.md`](modules/project_integration.md) |
| Media and knowledge | `lib/paper/`, `lib/motion_video/`, `lib/knowledge/` | [`modules/ingest_media.md`](modules/ingest_media.md) |
| Long-running deliverable production | `lib/production/` plus capability recipes | [`modules/production.md`](modules/production.md) |
| Scheduling and operations | `lib/scheduler/`, `lib/daily_report/` | [`modules/scheduling_ops.md`](modules/scheduling_ops.md), [`RELIABILITY_RUNBOOK.md`](RELIABILITY_RUNBOOK.md) |
| Desktop devices, remote worktrees, egress | `lib/desktop/`, `lib/desktop_agent/`, `lib/desktop_dist/` | [`modules/remote_execution.md`](modules/remote_execution.md) |
| Experiments and strategy decisions | `contracts/experiments_v1.schema.json`, `lib/experiments/`, `lib/cost_experiments.py` | [`modules/experiments.md`](modules/experiments.md) |
| Long-agent context, tool-cost controls, and paired Codex evaluation | `lib/tasks_pkg/context_composer/`, `lib/tools/`, `lib/benchmark_contract.py`, `evaluations/{codex_kimi_proxy,long_agent_release}/` | [`LLM_COST_OPTIMIZATION.md`](LLM_COST_OPTIMIZATION.md), [`modules/context_engineering.md`](modules/context_engineering.md), [`modules/tools_execution.md`](modules/tools_execution.md) |
| Logging and incident diagnosis | `lib/log*.py`, `lib/incident_journal.py` | [`LOGGING.md`](LOGGING.md), [`RELIABILITY_RUNBOOK.md`](RELIABILITY_RUNBOOK.md) |

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

Every Markdown file under `docs/` must appear in `catalog.json`. A product
change updates its authority document in the same diff. If a document becomes
historical, delete it after moving any still-valid invariant into the authority
document; do not create an in-repository archive folder.

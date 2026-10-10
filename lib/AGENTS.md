# Backend library guidance

## Scope and dependency direction

`lib/` contains application, execution, persistence-client, and operational
modules. Read `docs/ARCHITECTURE.md` and the owning domain map before editing.
Dependencies flow from delivery to application/execution to repository/storage
ports; backend libraries never import routes or browser owners.

## Domain routing

| Concern | Primary owners | First map |
|---|---|---|
| Boot and process lifecycle | `app_*`, `server_*`, `production_lifecycle.py`, `process_roles.py`, `compat/`, `self_update/`, `log_clean/` | `docs/modules/infra_runtime.md` |
| Identity, providers, OAuth, billing | `identity.py`, `api_keys/`, `oauth/`, `billing/`, `llm_dispatch/` | `docs/modules/auth_providers_billing.md` |
| API spec and error taxonomy | `openapi/`, `error_envelope/`, `api_response.py` | `docs/contracts/API_CONTRACT.md`, `docs/modules/integrations_api.md` |
| Tasks and agent execution | `tasks_pkg/`, `agent_core/`, `swarm/`, `agent_verdict/`, `goal_runs/`, `key_stats/` | `docs/modules/task_engine.md` |
| Orchestration graphs | `orchestration/`, `orchestration_*.py` | `docs/modules/orchestration_dag.md` |
| Model I/O | `llm/`, `llm_dispatch/`, `local_serve/`, `model_*`, `pricing/`, `llm_sanitize/`, `mt_provider/` | `docs/modules/llm_io.md` |
| Tools, MCP, browser, site packs | `tools/`, `mcp/`, `browser/`, `site_packs/`, `project_mod/`, `repo_map/`, `tool_input_repair/` | `docs/modules/tools_execution.md`, `docs/SITE_PACKS.md` |
| Context and memory | `tasks_pkg/context_composer/`, `tasks_pkg/compaction/`, `memory/`, `token_counter/` | `docs/modules/context_engineering.md` |
| Conversations and project state | `conversation_sync/`, `conversations/`, `project_mod/`, `presence/`, `chat/`, `conv_config/` | `docs/modules/conversations_project_brain.md` |
| Durable data | `storage/`, `storage_sidecar/` | `docs/modules/data_tier.md` |
| Papers and media | `paper/`, `pdf_parser/`, `translate/`, `motion_video/`, `knowledge/`, `doc_parser/`, `video_analysis/`, `transcription/`, `text_lang/` | `docs/modules/ingest_media.md` |
| Deliverable production | `production/`, `longform/`, `slides/`, `design_sys/`, `artifacts/`, `image_gen/` | `docs/modules/production.md` |
| Scheduling and operations | `scheduler/`, `daily_report/`, `optimizer/` | `docs/modules/scheduling_ops.md` |
| External channel integrations | `feishu/` | `docs/modules/integrations_api.md` |
| Remote execution | `desktop/`, `desktop_agent/`, `desktop_dist/`, `desktop_wire/` | `docs/modules/remote_execution.md` |
| Skills and experiments | `skills/`, `experiments/` | `docs/modules/skills.md`, `docs/modules/experiments.md` |
| Proof bundles | `proof/` | `docs/modules/proof.md` |

## Editing rules

- Give each module one responsibility, explicit entry points/dependencies, and
  one owner for policy/state. Cross-domain calls use public protocols or
  committed events, never private-module reach-through.
- Carry principal/owner identity explicitly. Application code uses semantic
  repositories; backend selection stays in the storage client, while SQL and
  database paths stay within the selected storage authority.
- Preserve typed failures, idempotency, rollback, cancellation, fault-injection
  seams, and user-visible recovery. Do not turn a fallback into a second state
  machine or authority.
- Bound every cache, queue, pool, task, stream, retry, temporary artifact, and
  background lifecycle. Optional heavy dependencies load lazily.

## Hidden conventions that local reading will miss

- `lib/__init__.py` executes on EVERY `lib.*` import and historically reads
  configuration at import time. Never add import-time work there; its line
  count and env-read count ratchet downward via `make architecture-check`.
- Module facades are explicit re-exports. Replacing a module object's class
  (`sys.modules[__name__].__class__ = ...`) makes text search miss symbols
  and hides import cost; the one legacy instance (`lib/llm_dispatch/api.py`)
  is frozen by the architecture gate and new ones are rejected.
- Several packages keep a monkeypatch-parity contract: tests patch symbols
  through the package facade, so call sites must resolve through the facade
  rather than deep-importing a private submodule (see
  `lib/mcp/client/_bridge.py`). Before "simplifying" an indirect import,
  check `git grep -n monkeypatch tests/` for the owning suite.
- Cross-process contract modules (sidecar-shared) are marked in their file
  headers and listed in `scripts/check_architecture.py`; they may only import
  the standard library and each other.

## Verification

Run the smallest tests named by the domain map, then neighboring boundary tests.
Use `python3 scripts/check_architecture.py` for dependency/ownership changes and
the relevant `make test-unit` or `make test-api` gate once focused tests pass.

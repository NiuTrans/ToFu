# Tofu (豆腐) — Architecture Panorama

> Canonical, up-to-date layered map of the project. Used as the source for
> `docs/architecture.html` (visual diagram) and whenever an AI assistant
> needs a birds-eye view.
>
> **Last re-scanned:** 2026-07-17 against `lib/`, `routes/`, `static/js/`,
> `server.py`, `routes/__init__.py`.
> **VERSION:** 0.13.0

---

## 1. Five-layer mental model

Tofu maps cleanly onto the same five layers Claude Code popularised
(Entry / Core / Safety / Context / Tools) plus an **Infra** layer that
covers logging, DB, OAuth, cross-DC, etc. and an **Ops** layer for the
nightly optimiser / scheduler.

```
┌─────────────────────────────────────────────────────────────────────┐
│                        ① Entry Layer                                │
│   Web UI (index.html)  │  Feishu Bot  │  Proactive Scheduler        │
│   Browser Extension    │  Desktop Agent│  MCP sub-process (stdio)   │
│   Headless API: /api/v1/* · OpenAI-compat · Anthropic-compat        │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        ② Core Engine                                │
│   routes/chat.py → tasks_pkg.manager.create_task()                  │
│   tasks_pkg.orchestrator.run_task()  ←  main ReAct loop             │
│   tasks_pkg.endpoint.run_endpoint_task()  ←  Planner→Worker→Critic  │
│   tasks_pkg.autopilot  ·  lib/orchestration_engine.py (DAG runs)    │
│   lib/swarm/master.py  ←  multi-agent DAG                           │
│   SSE stream: append_event() → /api/chat/stream/<task_id>           │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│         ③ Safety / Policy  ·  ④ Context Engineering                 │
│  project_mod.config.DANGEROUS_PATTERNS   │  system_context.py        │
│  tasks_pkg.approval (write approval)     │  compaction/ (3-layer)    │
│  oauth/ (Claude/Codex PKCE)              │  memory/ (skills, inject) │
│  proxy.py / rate_limiter.py              │  conv_message_builder.py  │
│  export.py (3-level sanitisation)        │  attachments.py           │
│  agent_core/personal_scope (headless     │  token_counter/ (budget)  │
│    fail-closed)  ·  auth_mode.py         │                           │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        ⑤ Tools & Extensions                         │
│  lib/tools/*.py (definitions)  →  tasks_pkg.tool_dispatch           │
│                                 →  tasks_pkg.executor               │
│                                 →  tasks_pkg.handlers/*.py          │
│  Built-in: project / search / fetch / browser / code_exec /         │
│            image_gen / memory / conversation /                      │
│            human_guidance / meta(plan)                              │
│  External: MCP (mcp/), Swarm agents, Desktop tools                  │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│              ⑥ LLM Dispatch  ·  ⑦ Infra  ·  ⑧ Ops                   │
│  llm_dispatch/ (slot × model × key) + llm/ package (build_body/SSE)  │
│  database/ (PG primary, SQLite fallback, dual schema)               │
│  billing/ (wallet · ledger · pricing) · log.py (5-stream)           │
│  scheduler/ (cron, timer, proactive)                                │
│  optimizer/ (nightly self-tuning loop)                              │
│  cross_dc.py  ·  fs_keepalive.py  ·  compat/  ·  self_update.py     │
└─────────────────────────────────────────────────────────────────────┘
```

> **Plugin note.** The former in-tree **trading** subsystem was extracted to a
> standalone `tofu-trading` package (2026-06). It is no longer part of core; it
> mounts through the `tofu.blueprints` / `tofu.startup` entry-point groups
> discovered by `routes/plugin_registry.py`. Core is trading-agnostic.

---

## 2. Full Mermaid panorama

Paste into any Mermaid-aware renderer (GitHub, Typora, Obsidian).

```mermaid
flowchart TB
  %% ============ ENTRY ============
  subgraph ENTRY["① 入口层 Entry"]
    direction LR
    web[Web UI<br/>index.html + static/js]
    feishu[Feishu Bot<br/>lib/feishu/]
    ext[Browser Extension<br/>browser_extension/]
    desk[Desktop Agent<br/>lib/desktop_agent.py]
    sched_in[Proactive Scheduler<br/>lib/scheduler/proactive.py]
    headless[Headless API<br/>/api/v1/* · OpenAI · Anthropic compat]
    mcp_in[MCP Servers<br/>stdio/SSE subprocess]
  end

  %% ============ ROUTES ============
  subgraph ROUTES["routes/ — top-level BPs + api_v1/ (37 modules)"]
    direction LR
    r_chat[chat.py + chat_queue<br/>chat_human_io · chat_tool_state]
    r_conv[conversations + _search<br/>_compaction · common]
    r_media[paper · translate · upload<br/>browser · desktop · artifacts]
    r_v1a[api_v1: chat · agents · agent_run<br/>conversations · tasks · endpoint]
    r_v1b[api_v1: project · memory · mcp<br/>orchestrations · swarm · scheduler]
    r_v1c[api_v1: auth · users · keys · billing<br/>providers · optimizer · daily_report]
    r_compat[compat_openai · compat_anthropic<br/>api_docs · metrics · legacy_redirects]
    r_plug[(plugin BPs via<br/>tofu.blueprints entry-points)]
  end

  %% ============ CORE ============
  subgraph CORE["② 核心引擎 Core Engine"]
    direction TB
    mgr[tasks_pkg/manager.py<br/>create_task · persist · SSE events]
    orch[tasks_pkg/orchestrator.py<br/>run_task — ReAct loop]
    endp[tasks_pkg/endpoint.py<br/>Planner → Worker → Critic<br/>+ endpoint_prompts · endpoint_review]
    auto[tasks_pkg/autopilot.py<br/>virtual-user self-drive]
    orun[lib/orchestration_engine.py<br/>multi-step DAG runs]
    swarm[lib/swarm/master.py<br/>+ scheduler · agent · planner<br/>streaming DAG, artifact store]
    stream[stream_handler · cache_tracking<br/>llm_fallback]
  end

  %% ============ CONTEXT ============
  subgraph CTX["④ 上下文工程 Context"]
    direction TB
    sysc[system_context.py<br/>+ system_prompt_cc.py]
    comp[compaction/<br/>micro → smart-summary → force]
    mem[memory/<br/>skills + injection + prefetch]
    conv_m[conv_message_builder.py<br/>build_api_messages_from_db]
    att[attachments.py<br/>per-turn file injection]
    msg_store[server_message_store.py<br/>persist_registry · persistence_store]
    tok[token_counter/<br/>context budget]
  end

  %% ============ SAFETY ============
  subgraph SAFE["③ 安全拦截 Safety"]
    direction LR
    approv[tasks_pkg/approval.py<br/>write-approval gate]
    hooks[tasks_pkg/tool_hooks.py<br/>before/after hooks]
    dang[project_mod/config.py<br/>DANGEROUS_PATTERNS]
    pscope[agent_core/personal_scope.py<br/>headless fail-closed]
    oauth_b[oauth/<br/>Claude · Codex · PKCE]
    proxy[proxy.py · rate_limiter.py<br/>auth_mode.py]
    export[export.py<br/>3-level sanitisation]
  end

  %% ============ TOOLS ============
  subgraph TOOLS["⑤ 工具与扩展 Tools"]
    direction TB
    tdef[lib/tools/*.py<br/>definitions]
    tdisp[tasks_pkg/tool_dispatch.py<br/>name → handler routing]
    texec[tasks_pkg/executor.py<br/>+ streaming_tool_executor<br/>+ executor_image]
    thand[tasks_pkg/handlers/<br/>misc · project · search<br/>browser · mcp · memory · code_exec]

    subgraph TFAM["tool families"]
      direction LR
      t1[project_mod/<br/>list/read/grep/write/<br/>apply_diff/run_command]
      t2[tofu_search external pkg<br/>search + fetch · multi-engine + filter]
      t3[browser/<br/>extension-bridge<br/>+ playwright pool]
      t4[image_gen.py<br/>multi-model dispatch]
      t5[mcp/<br/>client · registry · config]
      t6[file_reader.py<br/>pdf_parser/ · doc_parser]
    end
  end

  %% ============ LLM ============
  subgraph LLM["⑥ LLM Dispatch"]
    direction TB
    disp[llm_dispatch/dispatcher.py<br/>slot × model × key]
    api[llm_dispatch/api.py<br/>dispatch_chat · dispatch_stream]
    conf[llm_dispatch/config.py<br/>model aliases · routing]
    disc[llm_dispatch/discovery.py<br/>auto-discover /v1/models]
    client[lib/llm/<br/>body · stream · astream · cache · diagnostics]
    minfo[model_info.py · context_limits.py<br/>_clamp_max_tokens]
    byo[byo_providers · byo_resolve<br/>byo_egress · provider_probe]
  end

  %% ============ INFRA ============
  subgraph INFRA["⑦ 基础设施 Infra"]
    direction LR
    db[database/<br/>PG primary · SQLite fallback<br/>_core_schema (single table source)<br/>_schema_pg · _schema_sqlite · _sql_translate · _wrappers]
    bill[billing/<br/>wallet · ledger · pricing<br/>payments/]
    log[log.py<br/>app · access · error · vendor · audit]
    comp_p[compat/<br/>_platform · openai · anthropic shims]
    xdc[cross_dc.py · fs_keepalive.py<br/>FUSE latency auto-probe]
    boot[bootstrap.py · self_update.py<br/>dep-repair · in-place update]
  end

  %% ============ OPS ============
  subgraph OPS["⑧ Ops & 自进化 Evolution"]
    direction TB
    opt[optimizer/<br/>analyzer · proposer · applier<br/>storage · actions/<br/>nightly LLM-driven tuning]
    sch[scheduler/<br/>cron · timer · proactive<br/>manager · executor]
    daily[lib/daily_report/<br/>My Day · calendar · TODOs]
    audit[audit_log events<br/>→ logs/audit.log]
  end

  %% ============ WIRING ============
  ENTRY --> ROUTES
  ROUTES --> CORE
  CORE --> CTX
  CORE --> SAFE
  CORE --> TOOLS
  TOOLS --> LLM
  CORE --> LLM
  CORE --> INFRA
  INFRA --> bill
  OPS -.observes.-> INFRA
  OPS -.feedback.-> CORE
  ENTRY -.MCP stdio.-> TOOLS
```

---

## 3. Directory-level canonical list

Grounded against the current filesystem (2026-07-06).

### 3.1 Top level

| Path | Role |
|---|---|
| `server.py` | App entry (Quart + Hypercorn) · Flask→Quart shim · middleware · logging bootstrap · auto-TLS for HTTP/2 · auto-delegates to `bootstrap.py` on ImportError |
| `bootstrap.py` | LLM-guided dependency-repair launcher with live browser status page |
| `export.py` | 3-level sanitising export (personal / internal / opensource) — see CLAUDE.md §11 |
| `index.html` | Main SPA |
| `trading.html` | Legacy trading SPA shell (core trading code now lives in the external `tofu-trading` plugin) |
| `healthcheck.py` · `install.{py,sh,ps1}` | Install / health helpers |

### 3.2 `lib/` — core libraries (28 top-level sub-packages + 85 top-level modules)

**Sub-packages** (28 directories under `lib/` carrying an `__init__.py`,
excluding the `tests/` test package). The `tasks_pkg/handlers/` row below is a
*nested* sub-package of `tasks_pkg/`, listed for convenience — it is NOT counted
in the 28.

| Package | Purpose |
|---|---|
| `agent_core/` | **Browsable facade for the reusable agent base** (relocated 2026-06). `__init__.py` PEP-562 lazy facade (`CORE_MEMBERS` maps symbol → module); `task_runtime.py` (real home; `lib/task_runtime.py` shims it); `push.py` (PushHub + `push_event()`; `lib/push.py` shims it); `events.py` (EventType/EventSpec contract); `profiles.py` (capability profiles); `personal_scope.py` (app-personal vs headless fail-closed — see CLAUDE.md §3.7) |
| `artifacts/` | Generated-artifact storage / retrieval |
| `billing/` | Wallet · ledger · pricing · per-user cost accounting (`payments/` sub-pkg) |
| `browser/` | advanced · handlers · queue · dispatch · display · fetch |
| `chat/` | Chat-domain helpers shared by the chat routes |
| `compat/` | Cross-platform shim (`_platform.py`: Linux/macOS/Windows) + OpenAI/Anthropic API-compat adapters (`openai.py`, `anthropic.py`) |
| `conversations/` | Conversation-store domain logic shared by routes |
| `daily_report/` | My-Day / daily-report engine + background scheduler |
| `database/` | `_core` · `_bootstrap` · **`_core_schema`** (single SQLAlchemy-Core source for every table) · `_schema_pg` · `_schema_sqlite` · `_sql_translate` · `_wrappers` |
| `desktop/` | Desktop-agent server-side support (pairs with the top-level `desktop_agent.py` / `desktop_tools.py`) |
| `feishu/` | `_state` · conversation · messaging · pipeline · commands · events · startup |
| `file_history/` | api · store — per-file copy-backup undo |
| `llm/` | `body` · `chat` · `stream` · `astream` · `_sse_core` · `cache` · `anthropic_outbound` · `diagnostics` · `_transport` (split from the former `llm_client.py`) |
| `llm_dispatch/` | api · config · discovery · dispatcher · factory · slot (multi-key × multi-model) |
| `mcp/` | client · registry · config · project_names · types |
| `memory/` | storage · tools · injection · relevance · prefetch |
| `oauth/` | claude · codex · manager · pkce · token_store · outbound |
| `optimizer/` | analyzer · proposer · applier · storage · actions/ (**nightly self-tuning**; REST surface at `routes/api_v1/optimizer.py`) |
| `paper/` | Reading-Mode engine: report_engine · translate_engine · prompts · images · arxiv · tools |
| `pdf_parser/` | core · text · images · math · vlm · postprocess · _common |
| `presence/` | Cross-conversation live presence ("who is working here now") — the Project-Brain peer-status registry alongside the push hub |
| `project_mod/` | `tools` (execute_tool registry) · `run_command` · `read_tools` · `write_tools` · scanner · indexer · modifications · config |
| `scheduler/` | manager · executor · cron · timer · proactive · tool_defs · _shared |
| `swarm/` (16 modules) | master · agent · scheduler · planner · registry · rate_limiter · artifact_store · integration · events · tools · types · messages · result_format · protocol · persistence · snapshot — *(`review`/`synthesis` from earlier revisions no longer exist on disk)* |
| `tasks_pkg/` | **Task orchestration / execution** — see §3.2.1 |
| `tasks_pkg/handlers/` | misc · project · search · browser · mcp · memory · code_exec · _adapter |
| `token_counter/` | Context-window token accounting / budget |
| `tools/` | **Definitions**: project · search · browser · meta · human_guidance · image_gen · code_exec · conversation |
| `translate/` | Translation engine + cache + provider plumbing |

#### 3.2.1 `lib/tasks_pkg/` — execution package (38 modules + 3 sub-packages)

`orchestrator` · `manager` · `endpoint` · `endpoint_prompts` · `endpoint_review` ·
`autopilot` · `entry` · `executor` · `executor_image` · `streaming_tool_executor` ·
`tool_dispatch` · `tool_display` · `tool_hooks` · `cache_tracking` · `llm_fallback` ·
`stream_handler` · `message_builder` · `conv_message_builder` · `server_message_store` ·
`persist_registry` · `persistence_store` · `system_context` · `system_prompt_cc` ·
`model_config` · `attachments` · `approval` · `human_guidance` · `stdin_handler` ·
`auto_translate` · `commit_round` · `event_log` (durable SSE replay) ·
`event_fold` · `activity_sink` · `killed_recovery` (killed-task recovery) ·
`turn_retry` · `wire_fingerprint` · `wire_messages` · `write_breakdown`

Sub-packages: **`compaction/`** (3-layer context compaction, now a package) ·
**`segments/`** (segment-timeline model) · **`handlers/`** (per-tool execution handlers).

#### 3.2.2 `lib/` top-level modules

All **85** `lib/*.py` modules are accounted for (`agent_loop.py` added
2026-07-01; `runtime_state_store.py` / `runtime_paths.py` / `llm_json.py` added
since; verified against disk, zero invented names). The table below names
77 directly; the remaining 8 are documented elsewhere: the six `orchestration*`
modules share the single `orchestration*.py` row, and the `push.py` +
`task_runtime.py` compat shims are described in the `agent_core/` package row
in §3.2.

| Module(s) | Purpose |
|---|---|
| `log.py` · `log_clean.py` | `get_logger` / `log_context` / `log_exception` / `audit_log`; log-retention cleanup |
| `agent_core_manifest.py` | Declares `CORE_MODULES` / `REGISTRY_SEAMS` / `CONCRETE_PLUGIN_MODULES` — source of truth for the core/plugin split |
| `agent_artifacts.py` | `.tofu*` artifact registry (CLAUDE.md §3.6) |
| `agent_inbox.py` · `agent_options.py` · `agent_verdict.py` · `agent_loop.py` | Agent inbox · option resolution · STOP/CONTINUE verdict logic · shared multi-round tool-loop + `AbortSignal` seam (CLAUDE.md §4.6) |
| `orchestration*.py` | `orchestration` · `orchestration_engine` · `orchestration_composer` · `orchestration_endpoint_adapter` · `orchestration_endpoint_runner` · `orchestration_runs` — multi-step DAG orchestration |
| `model_info.py` · `context_limits.py` · `pricing.py` · `cost.py` · `cost_estimator.py` | Per-model caps · context limits · pricing tables · cost accounting |
| `api_response.py` · `request_parser.py` · `error_envelope.py` · `error_fingerprint.py` · `llm_error_format.py` · `llm_errors.py` | Unified JSON responses · typed body parsing · error shaping |
| `http_client.py` · `json_store.py` · `ttl_cache.py` · `idempotency.py` | Shared infra (CLAUDE.md §4.6) |
| `auth_mode.py` · `auth_sources.py` · `api_keys.py` · `rate_limit_api.py` · `rate_limit_store.py` · `rate_limiter.py` · `relay_config.py` | Auth modes · key management · rate limiting · relay config |
| `byo_providers.py` · `byo_resolve.py` · `byo_egress.py` · `provider_balance.py` · `provider_defaults.py` · `provider_probe.py` · `mt_provider.py` | Bring-your-own-provider plumbing |
| `conv_config.py` · `conv_ref.py` · `branch_meta.py` · `feature_registry.py` · `features_store.py` | Conversation config · branch metadata · feature flags |
| `js_bundler.py` · `css_bundler.py` · `openapi.py` | Frontend bundling · OpenAPI spec gen |
| `self_update.py` · `env_compat.py` · `config_dir.py` · `cross_dc.py` · `fs_keepalive.py` · `proxy.py` · `code_server_excludes.py` | Self-update · env/platform · cross-DC FUSE probe · keepalive · proxy · code-server exclude list |
| `runtime_state_store.py` · `runtime_paths.py` | Pluggable runtime-state backend (inproc default / redis opt-in via `TOFU_RUNTIME_STATE_BACKEND` — the scale-out lease/counter substrate seam) · runtime path resolution |
| `llm_json.py` | Robust JSON extraction from LLM output (fence-stripping, lenient parse) |
| `llm_sanitize.py` | Message sanitization (gateway terms, orphan tool calls, role merging) |
| `embeddings.py` · `file_reader.py` · `doc_parser.py` · `image_gen.py` · `pptx_translator.py` · `text_lang.py` · `translate_cache.py` | Embeddings · file/doc reading · image gen · translation helpers |
| `desktop_agent.py` · `desktop_tools.py` | Desktop agent bridge |
| `dispatch_stats.py` · `usage_tracker.py` · `key_stats.py` · `trajectory.py` · `tool_changes.py` · `tool_input_repair.py` · `message_queue.py` · `search_bridge.py` · `protocols.py` · `utils.py` · `_pkg_utils.py` · `version.py` | Stats · usage · trajectory · tool-input repair · message queue · misc helpers |

### 3.3 `routes/` — Blueprints (top-level + `api_v1/`; 362 `@*_bp.route` decorators)

The headless **`/api/v1/*`** surface is the canonical API (see CLAUDE.md §16).
`routes/__init__.py::ALL_BLUEPRINTS` wires the core set; optional feature
plugins (e.g. `tofu-trading`) mount via `routes/plugin_registry.py`
(`tofu.blueprints` / `tofu.startup` entry-point groups).

**Top-level Blueprints (always on):**
`common`, `chat` (+ side-effect modules `chat_queue`, `chat_human_io`,
`chat_tool_state`), `conversations` (+ `conversations_search`,
`conversations_compaction`), `config`, `browser`, `desktop`, `oauth`,
`translate`, `upload`, `artifacts`, `paper`, `push`,
`compat_openai`, `compat_anthropic`, `api_docs`, `metrics`, `legacy_redirects`.

**`routes/api_v1/` — headless surface (37 modules):**
`agents`, `agent_run`, `artifacts`, `auth`, `auth_mode`, `auth_sources`,
`billing`, `browser`, `capabilities`, `chat`, `chat_direct`, `common`,
`config`, `conversations`, `daily_report`, `desktop`, `endpoint`, `folders`,
`keys`, `logs`, `mcp`, `memory`, `oauth`, `optimizer`, `orchestrations`,
`paper`, `project`, `providers`, `scheduler`, `swarm`, `tasks`, `translate`,
`update`, `uploads`, `usage`, `users`, `webhooks`.

> **Trading routes are gone from core** — extracted to the standalone
> `tofu-trading` package; they register only when that plugin is installed.

### 3.4 `static/js/` — Vanilla-JS frontend (decomposed subpackages + bundler)

Every backend HTTP call goes through the unified client `api.js`
(CLAUDE.md §3.2.0). Large monoliths were decomposed (2026-05-28) into
subpackages; the served page is a single content-hashed `bundle-<hash>.js`
built from `_BUNDLE_FILES` in `lib/js_bundler.py` (CLAUDE.md §3.2.1).

| Group | Files |
|---|---|
| **root** | `api.js` · `main.js` · `i18n.js` · `core.js` · `branch.js` · `artifacts.js` · `orchestration.js` · `paper-reader.js` · `task-mode.js` · `project.js` · `translation.js` · `upload.js` · `image-gen.js` · `myday.js` · `context-bar.js` · `conv_view.js` · `info-rail.js` · `mobile_panels.js` · `compaction-viewer.js` · `idb-cache.js` · `push.js` · `preferences.js` · `toolset-apply.js` · `skills.js` · `memory.js` · `optimizer.js` · `scheduler.js` · `timer.js` · `update.js` · `log-clean.js` · `export-images.js` · `relay-admin.js` (`/admin` page) · `settings.js` · `globals.d.ts` (tsc ratchet) |
| **`core/`** | icons · markdown · safe_html · escape_html · conversations · folders · cost · toast · dialog · debug_panel · health_stream_timer · cross_tab_sync · translate_guard · cache_stats · error_envelope |
| **`ui/`** | chat_render · streaming_render · streaming_ui · streaming_swarm_panel · stream_lifecycle · sse_pipeline · `sse_handlers_{io,lifecycle,misc,swarm,tool}` · sse_poll_fallback · swarm_push · tool_rounds · finish_info · conversation_list · edit_message · message_actions · send_button · popups · turn_nav |
| **`main/`** | main_send_pipeline · main_conv_lifecycle · main_regen_continue · main_init_tasks · main_toolbar_ui · main_folders_mobile · main_input_handling · main_translating_bubble |
| **`settings/`** | provider_render · provider_templates · template_actions · model_edit · key_stats · balance · mcp · oauth · auth_sources · access_matrix · auto_setup · branding · core_panel · local_endpoints · other_tabs · save_export · system_prompt_editor · visibility_defaults · chip_input |

### 3.5 `data/`, `logs/`, `docs/` (runtime & docs)

- `data/config/server_config.json` · `features.json` · `profiles/*.json` ·
  `daily_reports/` (**per-project** isolated; no `~/.chatui/` global state)
- `logs/` — app · access · error · vendor · audit
- `docs/` — `ARCHITECTURE.md` (this file) · `architecture.html` /
  `architecture_en.html` (visual companions) · `HEADLESS_API.md` ·
  `api_client.md` · `legacy_api_migration.md` · `EVENTS.md` ·
  `COMPAT_OPENAI.md` · `COMPAT_ANTHROPIC.md` · `CUSTOM_TOOLS.md` ·
  `TOOL_PLUGINS.md` · `CLAUDE_CODE_ALIGNMENT.md` · `SECURITY_AUDIT_REPORT.md` ·
  `DEVELOPMENT_DIRECTION.md` · `agentic-development-experience.md`

---

## 4. Request → Response walk-through

Helps when explaining Tofu in a talk / diagram legend.

1. **Browser** sends `POST /api/chat/send` (atomic: msg + translate + start task).
2. `routes/chat.py::chat_send()` persists the user message, calls
   `_start_task_for_conv()` → `tasks_pkg/conv_message_builder.py::build_api_messages_from_db()`.
3. `manager.create_task()` stores the task in memory + registers conv→task latest.
4. Background thread runs `orchestrator.run_task()`:
   - `system_context._inject_system_contexts()` (system prompt, memory, attachments)
   - per round:
     - `llm_dispatch.api.dispatch_stream()` → `llm_dispatch.dispatcher` picks
       best **slot** (key × model) → `lib.llm.stream_chat()` streams SSE.
     - `stream_handler.analyse_stream_result()` classifies finish reason /
       tool calls / retries; `llm_fallback` swaps model on failure.
     - `tool_dispatch.parse_tool_calls()` + `execute_tool_pipeline()` →
       `executor._execute_tool_one()` → per-family handler.
     - `cache_tracking` tracks prompt-cache breaks.
     - `compaction.run_compaction_pipeline()` compresses old turns when needed.
5. `append_event()` emits SSE to `/api/chat/stream/<task_id>` (polled by frontend).
   Every event is **also** persisted via `tasks_pkg/event_log.append_persistent_event()`
   into the `task_events` table — this is what makes Last-Event-ID resumption
   durable across `cleanup_old_tasks` and server restart. Successive deltas
   are coalesced into one row per ~250 ms window with the LAST event_id.
6. On completion, `persist_task_result()` writes to DB (PG → SQLite fallback),
   `message_queue.dispatch_next_queued()` kicks any queued next message.
7. **Endpoint mode**: same loop wrapped in `endpoint.run_endpoint_task()`
   with Planner / Worker / Critic phases and `MAX_REPLANS=3`.
8. **Swarm mode**: `routes/api_v1/swarm.py` delegates to `swarm/master.py` which
   runs a streaming DAG of specialist agents via `swarm/scheduler.py`.
9. **Headless**: `/api/v1/agent/run` (`agent_run.py`) and the OpenAI/Anthropic
   compat surfaces build a config via `apply_headless_personal_defaults()` so
   operator-personal state (memory, preference profile) fails CLOSED for BYO
   callers (CLAUDE.md §3.7).

---

## 5. Drift-check protocol (for AI assistants)

When re-generating this file or the HTML companion:

```bash
# 1. Inventory
list_dir('lib'); list_dir('routes'); list_dir('routes/api_v1'); list_dir('static/js')
list_dir('lib/tasks_pkg'); list_dir('lib/swarm')
list_dir('static/js/core'); list_dir('static/js/ui'); list_dir('static/js/main'); list_dir('static/js/settings')

# 2. Count route decorators
grep_search(pattern='@\\w+_bp\\.route', path='routes', count_only=True)

# 3. Trace active ALL_BLUEPRINTS
read_files([{path: 'routes/__init__.py'}])

# 4. Confirm trading is plugin-only (no in-tree trading package)
list_dir('lib')  # expect NO trading*/ dirs; mounts via plugin_registry.py
```

If the inventory has changed (new package, new tasks_pkg module, new blueprint),
update both §3 of this file and the matching block in `docs/architecture.html`,
then bump the "Last re-scanned" line at the top.

---

## 6. Messages-as-Rows roadmap (sync foundation)

The conversation store has been migrating away from "single JSONB array,
two writers" toward "individually addressable rows, server-only writes".
The current shape is the bridge layer:

| Phase | Status | What landed |
|---|---|---|
| **0. Persisted SSE events** | ✅ 2026-05-09 | `task_events` table + `tasks_pkg/event_log.py`. `append_event` mirrors every event; SSE stream falls back to the table when the task is gone. Survives `cleanup_old_tasks` and server restart. |
| **1. Comprehensive checkpoint** | ✅ 2026-05-09 | `_sync_partial_to_conversation` is now CAS-retried and writes the full structural payload (toolRounds, modifiedFileList, _memoryPrefetch, gitSha, model). Page-reload mid-stream reconstructs the same UI. |
| **2. Stable per-message IDs** | ✅ 2026-05-09 | `_assign_message_ids()` backfills UUIDs onto every JSONB write site (save_conv, patch_message, partial sync, result sync). New `PATCH /api/conversations/<cid>/messages/by-id/<mid>` endpoint. `routes/translate.py` resolves by id first, then idx, then content. The "msg_idx N out of range" warning class is fixed. |
| **3. Frontend reads via id** | ⏳ mostly done (2026-06-25) | `_patchMessageOnServer`, `_startTranslateTask`, AND now **edit/regenerate** send `msgId` when available (`saveEditAndResend`/`regenerateFromUser` send `truncateToMsgId`; `chat_regenerate` resolves it via `find_message_by_id` and falls back to the index). Branch ops are still index-based (low-value; the parent-message index is validated server-side against the freshly-persisted array). Legacy `msgIdx` paths all still work. |
| **4. Stop frontend writes** | ◑ split (verified 2026-06-25) | **Append primitive = DONE, already live:** the *user-message-creation* path does NOT use a full-array PUT — `sendMessage`→`Api.chat.send`→`chat_send` (`routes/chat.py`) creates the user message server-side via `_append_user_msg_idempotent` + `_persist_conv_messages`. A separate `POST .../messages` endpoint would be redundant with this. **Full-array-PUT elimination = DEFERRED, and NOT benign:** the remaining `syncConversationToServer` PUTs serve edit/regen/branch/post-stream/checkpoint (rewrite/truncate ops, not append), and the `save_conv` guards they pass through (`blocked_msg_regression`, `blocked_stale_checkpoint`, the cross-talk count-jump detector) are **load-bearing safety** added by the 2026-05 sync-hardening to fix real data-loss races. Removing them requires replacing `save_conv` with per-op targeted endpoints everywhere FIRST — a large cutover that is the opposite of low-risk. The cross-talk heuristic stays until the full-array PUT is provably gone. |
| **5. Per-message rows** | 🚧 in progress (2026-06-25) | `messages` becomes its own table `conversation_messages(conv_id, seq, _msgId, role, content, …, meta JSONB)`. Landing **migrator-first**: a one-shot idempotent backfill + DUAL-WRITE/DUAL-READ behind a flag (`TOFU_MESSAGES_ROWS`), GATED on byte-identical `build_search_text` output verified on real data BEFORE flipping reads. NOT a one-pass cutover. |

Phase 0–2 are shipped and reversible. Phase 3 is opt-in per call site
(absence of `_msgId` falls through to the legacy index path). Phase 4–5
require a coordinated backfill + frontend cutover and should ship as
their own PRs once Phase 3 is fully migrated.

---

*Companion files: `docs/architecture.html` / `docs/architecture_en.html`
(visual panoramic diagrams, use for screenshots / slides / social posts),
`CLAUDE.md §1` (terse overview that points here), `README.md` (user-facing
project tour).*

# Module Design Doc — Unit 12: Integrations / API Surface (`routes/`, `compat/`, `feishu/`, `mcp/`, `desktop`, `openapi`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md`). This is
> Layers ① (Entry) + ② (routes/) of the panorama: every way a request enters
> Tofu — the web UI's routes, the headless `/api/v1/*` surface, the OpenAI/
> Anthropic compat adapters, Feishu, MCP, desktop.
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11. `list_dir`
> overcounts — all numbers are `wc -l`. Every MISCUT/BIG verdict cites competing
> responsibilities or line ranges; size alone is never the argument.
>
> **The analytical payload:** (1) are the routes THIN adapters (parse → call a
> `lib/` function → shape response), or has business logic leaked into handlers?
> and (2) do the headless surfaces (`compat/` + `api_v1/agent_run` + `api_v1/chat`)
> route through the SAME orchestrator the interactive chat uses, not a fork?

---

## 1. Same-orchestrator verification (the load-bearing question)

**Verdict: ALL of the headless surfaces route through the SAME
`create_task` + `spawn_task` seam the interactive UI uses. There is NO parallel
orchestrator.** Verified by grepping the task-spawn call in every entry handler:

| Entry surface | Handler | Task spawn |
|---|---|---|
| Web UI | `routes/chat.py` | `create_task` (+ chat_queue `spawn_task`) |
| Native headless | `routes/api_v1/chat.py:chat_completions` | `create_task` + `spawn_task` |
| OpenAI compat | `routes/compat_openai.py:113,154` | `create_task` + `spawn_task` |
| Anthropic compat | `routes/compat_anthropic.py:100,141` | `create_task` + `spawn_task` |
| Agent runtime (BYOM) | `routes/api_v1/agent_run.py:566,671` | `create_task` + `spawn_task` |

Every one of these `create_task(conv, messages, cfg)` → `spawn_task(task)` — the
same `lib/tasks_pkg` entry documented in Unit 1. So tool use, thinking, MCP,
memory, compaction, the fallback chain, admission control, and billing settle are
identical across all surfaces; the adapters differ ONLY in request/response shape.
The docstrings say so explicitly: `api_v1/chat` — "Reuses `create_task` +
`spawn_task` … so every feature available to the UI is reachable from the API
without re-implementation"; `agent_run` — "Everything else (orchestrator,
fallback, retries, tool execution) is shared with `routes.api_v1.chat`."

**The ONE deliberate exception is documented, not a fork:** `api_v1/chat_direct.py`
(279) is a lighter single-shot path that calls `entry.build_chat_config` +
`dispatch` directly, "NONE of the create_task / spawn_task / thread-worker
machinery" (its own docstring). This is an intentional low-overhead lane for a
stateless single completion — it reuses the SAME `entry.build_chat_config` kernel
(so cfg-mapping can't drift), just skips the task registry it doesn't need. Not a
second orchestrator; a documented shortcut sharing the config kernel.

**`compat/` correctly reuses, doesn't fork.** `lib/compat/openai.py` +
`anthropic.py` are PURE translators (no Flask import): `translate_openai_request`
(OpenAI body → Tofu cfg+messages), `build_openai_response` (task → OpenAI body),
`stream_openai_chunks` (task events → OpenAI wire). The route
(`routes/compat_openai.py`) wires them to `create_task`/`spawn_task`. So the
compat surface is a shape-translation shell over the one task engine — exactly
the correct decoupling (mirrors Unit 3's `search_bridge` seam pattern). Bonus:
both `compat/*` reuse `tasks_pkg.segments.deliverable_text` (the narrator-leak fix
from Unit 1's segment epic) so headless callers get the narration-free answer —
the SAME single source, not a re-derivation.

---

## 2. Route thinness — is business logic in handlers?

**Verdict: MOSTLY thin, with the correct exceptions and ONE cluster of genuinely
fat route files.** The pattern is real and enforced, but not universal:

### 2a. The thin adapters (the norm)

`api_v1/chat`, `compat_openai`, `agent_run` are textbook thin: parse body →
(BYO resolve) → `build_chat_config`/`translate_*_request` → `create_task` →
`spawn_task` → shape response. The BUSINESS logic they invoke all lives in `lib/`:
- cfg mapping → `lib.tasks_pkg.entry.build_chat_config` (the SHARED kernel, so UI
  and API can't drift on how knobs land in cfg).
- billing → `lib.billing.request_flow` (reserve/settle — Unit 8).
- admission → `lib.agent_core.admission.controller` (Unit 9).
- BYO resolve + egress → `lib.byo_resolve` + `lib.byo_egress` (Unit 8).
- message transform → `lib.tasks_pkg.conv_message_builder` (runs server-side; the
  route only does a shallow `_validate_messages` reject of malformed input,
  explicitly deferring the real pipeline to lib — see the docstring).

The route handlers hold only HTTP concerns: auth scope, admission 503, the
reserve/settle wiring, the SSE generator, and the response envelope. That is the
correct route-layer residue.

### 2b. The correct exceptions (response-shaping that belongs in routes)

Some large handlers are big because response-shaping IS a route concern and
nothing else calls them (the same judgment the `backend-paper-decomposition`
memory records): `routes/paper.py`'s `export_report` (MD/HTML/PDF rendering with
KaTeX + base64 embedding) and `fetch_arxiv_stream` (SSE generator) — pure response
shaping, no reusable engine. Extracting them would move ~450 lines for no win.

### 2c. The genuinely fat route files (the debt)

`routes/paper.py` (2554), `routes/chat.py` (2256), `routes/conversations.py`
(1865), `routes/config.py` (1123), `routes/upload.py` (947) are all >900 lines.
These are the LARGEST route files and the place where "thin adapter" is most
strained. Two sub-cases:
- **`routes/chat.py` (2256) — mostly thin but sprawling.** It already delegates
  the heavy lifting to `lib.chat.*` (the back-compat aliases at the top —
  `_append_user_msg_idempotent`, `_auto_translate_user`, `_build_tool_history_round`,
  etc. all moved to `lib/chat/messages.py` to break the lib→routes cycle). What
  remains is 2256 lines of route handlers + the SSE snapshot serializer
  (`_dumps_yielding`, an off-loop encode to dodge the GIL-wedge incident). It's
  BIG because it has many endpoints (start/stream/poll/abort/queue), not because
  logic leaked — the logic is in `lib/chat/`.
- **`routes/paper.py` (2554) — thin-route + lib package already done** (the
  `lib/paper/` extraction, Unit 11). The 2554 is 22 route handlers + the two
  response-shapers (§2b). BIG but structurally correct.
- **`routes/conversations.py` (1865), `config.py` (1123), `upload.py` (947)** —
  these are the least-audited. `conversations.py` is CRUD + search + the
  reconcile GET path (Unit 6 mid-migration); `config.py` is the settings surface
  (provider CRUD + toggles); `upload.py` includes `_safe_image_fetch` (an SSRF
  guard that IS security logic — arguably belongs in `lib`, cf. `byo_egress`).
  These are the candidates where a `lib/` extraction (the proven pattern) would
  most help — but they're also heavily-contested (conversations.py) or config-
  surface churn.

### 2d. Enforcement exists

Route thinness on the FRONTEND side is enforced by `test_frontend_api_isolation.py`
(the ratchet from CLAUDE.md §3.2.0 — every JS call must go through `Api.*`). There
is no equivalent backend "no logic in routes" AST gate, so route-fatness is
governed by convention + the decomposition memories, not a test. That's a gap
worth noting (a `test_route_thinness` ratchet counting non-HTTP imports per route
file would pin it), but it is NOT a defect in the current code — the big files are
big for endpoint-count/response-shaping reasons, not leaked logic.

---

## 3. Module inventory (real `wc -l`, size verdict, status, tests)

### 3.1 `routes/` top-level (12,644 LOC, 25 files)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `paper.py` | 2554 | **BIG (route+shapers)** | HOT | `test_paper_*` |
| `chat.py` | 2256 | **BIG (many endpoints)** | HOT | `test_chat_routes_scope_gated`, `test_agent_poll_routes` |
| `conversations.py` | 1865 | **BIG** | HOT | `test_branch_routes` |
| `config.py` | 1123 | **BIG** | HOT | `test_conv_config_route` |
| `upload.py` | 947 | **BIG** | HOT | `test_static_route_offload` |
| `common.py` | 652 | OK (index/bundle) | HOT | `test_static_route_offload` |
| `artifacts.py` | 365 | OK | HOT | `test_artifacts_pdf_export` |
| `compat_openai.py` | 287 | OK (thin adapter) | HOT | `test_compat_openai`, `test_ecosystem_sdk_compat` |
| `push.py` | 218 | OK | HOT | `test_push_fanout` |
| `chat_queue.py` | 213 | OK | HOT | via chat e2e |
| `compat_anthropic.py` | 210 | OK (thin adapter) | HOT | `test_compat_anthropic` |
| `desktop.py` | 206 | OK | live | `test_desktop_install_paths` |
| `metrics.py` | 196 | OK | HOT | `test_usage_and_metrics_routes` |
| `plugin_registry.py` | 194 | OK | HOT | `test_push_hub_and_plugin_registry` |
| `conversations_search.py` | 185 | OK | HOT | via search e2e |
| `browser.py` | 185 | OK | live | via browser e2e |
| `oauth.py` | 184 | OK | live | via oauth e2e |
| `translate.py` | 160 | OK (thin — post-decomp) | HOT | via translate e2e |
| `conversations_compaction.py` | 157 | OK | HOT | via compaction e2e |
| `__init__.py` | 109 | OK (register_all) | HOT | `test_api_v1_integration` |
| `_task_routes.py` | 92 | OK (factory) | HOT | `test_agent_poll_routes` |
| `chat_human_io.py` | 86 | OK | HOT | via human-io e2e |
| `api_docs.py` | 81 | OK | HOT | `test_openapi_spec` |
| `chat_tool_state.py` | 62 | OK | HOT | via tool-state e2e |
| `legacy_redirects.py` | 57 | leaf | HOT | via redirect e2e |

`routes/__init__.py` — OK, notable: `ALL_BLUEPRINTS` + `register_all` is the
single mount point; plugin blueprints join via the `tofu.blueprints` entry-point
group (fail-soft, no-op for a vanilla install) — the same plugin-seam discipline
as tools/providers/schema (Units 3/7/9). It also starts the two schedulers
(daily-report + proactive) with the schema-readiness gate (Unit 10).

### 3.2 `routes/api_v1/` (13,240 LOC, 38 files) — the canonical surface

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `project.py` | 1545 | **BIG** | HOT (Project Brain REST) | `test_api_v1_integration` |
| `daily_report.py` | 753 | **BIG** | live | via daily-report e2e |
| `agent_run.py` | 713 | **BIG (thin façade)** | HOT | `test_api_v1_agent_run` |
| `orchestrations.py` | 688 | **BIG** | live | `test_orchestrations` |
| `mcp.py` | 658 | **BIG** | HOT | `test_mcp_tool_links` |
| `auth.py` | 651 | **BIG** | HOT | `test_chat_routes_scope_gated` |
| `chat.py` | 610 | OK (thin — §2a) | HOT | `test_api_v1_chat_route` |
| `billing.py` | 584 | **BIG** | live | `test_billing_phase2` |
| `agents.py` | 574 | **BIG** | HOT | `test_capabilities_agents_drift` |
| `capabilities.py` | 535 | **BIG** | HOT | `test_capabilities_extensibility` |
| `memory.py` | 517 | **BIG** | HOT | `test_memory_global_server_store` |
| `conversations.py` | 408 | OK | HOT | via conv e2e |
| `users.py` | 392 | OK | HOT | via user e2e |
| `logs.py` | 343 | OK | HOT | `test_logs_clean_route` |
| `providers.py` | 321 | OK | HOT | `test_api_v1_byo_surface_polish` |
| `optimizer.py` | 312 | OK | live | via optimizer e2e |
| `tasks.py` | 301 | OK | HOT | `test_agent_poll_routes` |
| `scheduler.py` | 279 | OK | HOT | via scheduler e2e |
| `chat_direct.py` | 279 | OK (documented lite lane) | HOT | `test_api_v1_chat_route` |
| `update.py` | 274 | OK | live | `test_self_update_tarball` |
| `folders.py` | 266 | OK | HOT | via folders e2e |
| `translate.py` | 261 | OK | HOT | via translate e2e |
| `webhooks.py` | 236 | OK | live | via webhook e2e |
| `artifacts.py` | 214 | OK | HOT | via artifacts e2e |
| `keys.py` | 186 | OK | HOT | `test_api_keys` |
| `auth_sources.py` | 183 | OK | live | `test_auth_sources_xhs` |
| `endpoint.py` | 162 | OK | HOT | `test_endpoint_flow_parity` |
| `browser.py` | 140 | OK | live | via browser e2e |
| `audio.py` | 128 | OK | HOT | `test_audio_transcribe` |
| `auth_mode.py` | 122 | OK | HOT | via auth e2e |
| `oauth.py` | 113 | OK | live | via oauth e2e |
| `__init__.py` | 105 | OK (ALL_V1_BLUEPRINTS) | HOT | `test_api_v1_integration` |
| `swarm.py` | 103 | OK | HOT | `test_swarm_async` |
| `usage.py` | 81 | OK | HOT | `test_usage_and_metrics_routes` |
| `desktop.py` | 50 | leaf | live | via desktop e2e |
| `paper.py` | 48 | leaf (delegates to routes/paper) | HOT | via paper e2e |
| `common.py` | 42 | leaf | HOT | — |
| `config.py` | 33 | leaf | HOT | — |
| `uploads.py` | 30 | leaf | HOT | — |

`project.py` — **BIG (1545), the largest api_v1 file.** It is the WHOLE Project
Brain REST surface (Unit 6): ~40 endpoints (project set/browse/write + charter/
board/feed/peer/status/watch/ready). Each handler is thin (calls a
`lib.conversations.*` pillar function), but there are 40 of them. It's BIG by
endpoint-count, not leaked logic — and it's mid-migration (Unit 6 flagged the
charter-CRUD + status/watch frontend tail in flight here). Document, don't split
now (contested). A natural future split is by pillar (`project_brain_routes.py`
separate from `project_files_routes.py`).

`agent_run.py` — BIG (713) but a THIN façade (§1/§2a): the size is the capability
alias table (`_ALIAS_SETTERS`, `_TOOL_TAG_MAP`, `_build_cfg`) + the full
reserve/admission/settle/dispose choreography + the trajectory flatten. All of it
is HTTP-orchestration; the actual agent work is `create_task`/`spawn_task`. BIG
but correctly bounded for what it is (the headline BYOM endpoint).

`capabilities.py` / `agents.py` — BIG; the capability/agent-catalogue surfaces
(guarded by `test_capabilities_*_drift` — a drift ratchet, good). `mcp.py` (658),
`auth.py` (651), `billing.py` (584), `memory.py` (517) — BIG route surfaces, each
delegating to its `lib/` subsystem; BIG by endpoint-count.

### 3.3 `lib/compat/` (1051 LOC, 4 files) — pure translators

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `anthropic.py` | 340 | OK | HOT | `test_compat_anthropic`, `test_anthropic_outbound` |
| `openai.py` | 335 | OK | HOT | `test_compat_openai` |
| `_platform.py` | 327 | OK | HOT | `test_env_compat` |
| `__init__.py` | 49 | OK (facade) | — | — |

`openai.py`/`anthropic.py` — OK, exemplary: pure functions, no Flask, reuse
`deliverable_text` (§1). `_platform.py` is the cross-platform shim (Linux/macOS/
Windows path/process abstractions) — unrelated to the API adapters but shares the
`compat/` namespace (a mild naming overload: "compat" = both API-compat AND
platform-compat). Cohesive individually.

### 3.4 `lib/mcp/` (4610 LOC, 8 files)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `client.py` | 2413 | **MISCUT** | HOT | `test_mcp_call_health`, `test_mcp_install_nonblocking`, `test_mcp_launcher_resolve` |
| `registry.py` | 1213 | **BIG** | HOT | `test_mcp_catalog_internal_only`, `test_mcp_tool_links` |
| `project_names.py` | 375 | OK | HOT | via mcp e2e |
| `health_probe.py` | 222 | OK | HOT | `test_mcp_health_probe_contract`, `test_mcp_cred_health` |
| `config.py` | 194 | OK | HOT | via mcp e2e |
| `types.py` | 115 | OK | HOT | — |
| `vendored.py` | 45 | OK | live | — |
| `__init__.py` | 33 | OK (facade) | — | — |

`client.py` — **MISCUT (2413), the largest file in the unit.** It bundles: MCP
subprocess lifecycle (spawn/stdio/SSE transport), the background asyncio loop in a
daemon thread, tool discovery + OpenAI-schema translation, tool-call dispatch, the
circuit-breaker/backoff, and credential probing. That's at least 4 concerns
(transport lifecycle / discovery+translate / dispatch / breaker+cred-probe). Same
species as `manager.py`/`_core.py` — a hot core that grew. Split candidate:
`mcp/transport.py` (subprocess+loop) + `mcp/dispatch.py` (call routing+breaker),
leaving discovery+lifecycle in `client.py`. RISK: the async-loop-in-daemon-thread
threading model is subtle; split behind the mcp health/call tests.

`registry.py` — BIG (1213), the MCP server catalogue + install/vendor-copy +
project-name mapping. Cohesive-ish (catalogue management) but large; defer.

### 3.5 `lib/feishu/` (1196 LOC, 8 files) — clean

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `commands.py` | 260 | OK | live | `test_feishu` |
| `conversation.py` | 228 | OK | live | `test_feishu` |
| `events.py` | 179 | OK | live | `test_feishu` |
| `messaging.py` | 133 | OK | live | `test_feishu` |
| `pipeline.py` | 124 | OK | live | `test_feishu` |
| `startup.py` | 119 | OK | live | `test_feishu` |
| `_state.py` | 79 | OK | live | `test_feishu` |
| `__init__.py` | 74 | OK (facade) | — | — |

The `feishu/` package is well-decomposed — the Feishu/Lark bot pipeline cleanly
split into events → pipeline → commands → messaging → conversation, nothing
oversized. Its `pipeline` reaches into the task engine the same way the other
entry surfaces do (it's another entry adapter).

---

## 4. Dependencies (in / out)

**Inbound:** the ASGI app (`server.py`) → `routes.register_all(app)` → mounts
`ALL_BLUEPRINTS` + `ALL_V1_BLUEPRINTS` + plugin blueprints. External SDKs hit
`/v1/*` (compat), integrators hit `/api/v1/*`, the browser hits `/api/*` + `/`.

**Outbound (the whole point — routes call DOWN into lib):**
- All chat/agent entry → `lib.tasks_pkg` (`create_task`/`spawn_task`/`entry`) —
  the single orchestrator (§1).
- `lib.compat.*` (translators) ← the compat routes.
- `lib.billing.request_flow`, `lib.agent_core.admission`, `lib.byo_resolve`,
  `lib.byo_egress` — the cross-cutting HTTP-layer concerns (Units 8/9).
- `lib.conversations.*` ← `api_v1/project.py` (Project Brain, Unit 6).
- `lib.mcp.client` ← `api_v1/mcp.py` + the mcp tool handler (Unit 3).
- `lib.feishu.pipeline` ← the feishu webhook route → `tasks_pkg`.
- `lib.openapi` (`api_meta` decorator) ← every documented route (spec generation).
- `lib.api_response` (`api_ok`/`api_error`/`sse_response`) + `lib.request_parser`
  — the unified envelope + typed body extraction every route uses (the route-layer
  DRY seam).

**No back-edges:** `lib/` never imports `routes/` — the `lib.chat` extraction
(the back-compat aliases in `routes/chat.py`) exists SPECIFICALLY to break the one
historical `lib→routes` cycle. Confirmed the dependency flows strictly routes→lib.

---

## 5. Invariants (must not be broken by a refactor)

1. **Every chat/agent entry surface routes through `create_task`+`spawn_task`**
   (§1) — the ONE orchestrator. A new entry surface MUST reuse it, never fork the
   loop. `chat_direct` is the only exception and shares the `build_chat_config`
   kernel.
2. **cfg mapping goes through `entry.build_chat_config`** — the shared kernel so
   UI, api_v1/chat, and compat can never drift on how knobs land in cfg.
3. **`lib/compat/*` are PURE (no Flask import)** — the route wires them to HTTP.
   Keep the translator/HTTP split.
4. **Headless surfaces fail-closed on personal scope** (`apply_headless_personal_defaults`
   — Units 8/9) — the operator's memory/preferences never ride a BYO call.
5. **`lib/` never imports `routes/`** — the `lib.chat` extraction broke the one
   cycle; don't reintroduce a route import in a lib module.
6. **Every JS→backend call goes through `Api.*`** (`test_frontend_api_isolation`
   ratchet — CLAUDE.md §3.2.0). The count is monotonic-decreasing; CI fails on a
   new raw `fetch('/api/...')`.
7. **A new top-level `static/js/*.js` MUST be added to `_BUNDLE_FILES`** (CLAUDE.md
   §3.2.1) or it silently no-ops in production.
8. **`register_all` mounts plugins fail-soft** via entry-point groups — a vanilla
   install with no plugin is a no-op; a duplicate blueprint name is skipped, logged.
9. **The single abort handler is `routes/chat.py::chat_abort`** — a second
   registration on the same path silently shadows it (Flask routes to the
   first-registered). The api_v1/chat abort is intentionally NOT redefined.
10. **MCP is a process-global singleton with a daemon asyncio loop** — public
    methods are thread-safe; a refactor must preserve the call-from-any-thread
    contract.

---

## 6. Known debt (grounded)

- **`lib/mcp/client.py` (2413) is MISCUT** — 4 concerns (transport / discovery+
  translate / dispatch / breaker+cred-probe) in the largest file of the unit (§3.4).
- **`routes/conversations.py` (1865), `config.py` (1123), `upload.py` (947)** are
  the least-audited fat route files — candidates for a `lib/` extraction (the
  proven pattern), though `upload.py`'s `_safe_image_fetch` SSRF guard arguably
  belongs in `lib` next to `byo_egress`.
- **`routes/api_v1/project.py` (1545)** — BIG by endpoint-count (the whole Project
  Brain REST surface); mid-migration (Unit 6), split-by-pillar deferred.
- **No backend route-thinness AST gate** exists (only the frontend `Api.*`
  ratchet) — route-fatness is convention-governed. A `test_route_thinness` ratchet
  would pin it. Not a defect today, a hardening opportunity.
- **`compat/` namespace overload** — API-compat (openai/anthropic) + platform-compat
  (`_platform.py`) share one package. Cosmetic.
- No parallel orchestrator, no leaked-logic defect in the audited thin adapters —
  the two things this unit was tasked to check are sound.

---

## 7. Segmentation verdict (this unit)

**Correctly bounded — leave as-is:**
The thin adapters (`api_v1/chat`, `compat_openai`, `compat_anthropic`,
`chat_direct`, the small `api_v1/*` files), `lib/compat/*` (pure translators), the
entire `feishu/` package (clean), `routes/__init__` (mount point), `translate.py`
(post-decomposition thin), most of `routes/` and `api_v1/`.

**Miscut — should split:**
1. **`lib/mcp/client.py` (2413) → extract `mcp/transport.py`** (subprocess +
   daemon asyncio loop + stdio/SSE) and **`mcp/dispatch.py`** (call routing +
   circuit-breaker + backoff), leaving discovery + lifecycle in `client.py`.
   Same species as `manager.py`/`_core.py`. Behind `test_mcp_call_health` +
   `test_mcp_install_nonblocking`.

**Big but optional (defer / contested):**
- `routes/api_v1/project.py` (1545) — split-by-pillar, but mid-migration (Unit 6);
  defer until the charter-CRUD/status-watch tail lands.
- `routes/conversations.py` (1865) — `lib/` extraction candidate; contested.
- `routes/config.py` (1123), `upload.py` (947) — `lib/` extraction candidates.
- `routes/chat.py` (2256), `paper.py` (2554) — BIG by endpoint-count + correct
  response-shapers; the logic is already in `lib/`, so a split is low-value.
- `lib/mcp/registry.py` (1213), `api_v1/{daily_report,agent_run,orchestrations,
  auth,agents,capabilities,billing,memory}.py` — BIG by endpoint/table count,
  cohesive; defer.

**Do NOT split:** `lib/compat/*` (pure translators, correct), `feishu/` modules,
the small route files.

**Hardening opportunity (not a split):** add a backend `test_route_thinness`
ratchet (count non-HTTP-concern imports per route file) to pin the thin-adapter
convention the way `test_frontend_api_isolation` pins the frontend.

---

## 8. Comparison to Units 1–11 (closing the survey)

- **The headless surfaces are genuinely unified** — one orchestrator, one cfg
  kernel, pure-translator compat shells. This is the payoff of the whole `lib/`
  extraction program the survey traced: the entry layer is thin because the
  business logic was pulled DOWN into `lib/` (Units 1–11). The `compat/` reuse of
  `deliverable_text` (Unit 1's segment epic) is the clearest proof the surfaces
  share one source, not forks.
- **`feishu/` is an EIGHTH reference-quality package** (with `swarm/`,
  `token_counter/`, `compaction/`, `billing/`, `agent_core/`, `daily_report/`,
  `paper/`) — the well-decomposed norm holds even at the integration edge.
- **The one real miscut, `mcp/client.py`, is the SAME species** seen in Units 1
  (`manager`/`orchestrator`), 2 (`api.py`), 7 (`_core.py`), and 12's route-fat
  files: a hot core that accreted concerns. That consistency IS the survey's
  finding — the codebase is overwhelmingly well-segmented, and its debt clusters
  in a small, identifiable set of "hot cores left behind after extraction."
- **The route layer proves the thin-adapter thesis with one caveat:** the audited
  chat/agent adapters are genuinely thin, but there is NO test enforcing it
  backend-side, so a handful of fat route files (conversations/config/upload) sit
  un-ratcheted. That's the one systemic hardening this unit surfaces.

---

## 9. Survey complete — the cross-unit synthesis (all 12 units)

**Segmentation verdict across the whole backend:** the codebase is
overwhelmingly well-segmented. Eight packages are reference-quality clean splits
(`compaction/`, `token_counter/`, `swarm/`, `billing/`, `agent_core/`,
`daily_report/`, `paper/`, `feishu/`); the plugin/seam discipline
(tools/providers/schema/ConversationStore/activity + the AST boundary gate) is
consistent and, in `agent_core`, test-enforced.

**The genuine debt is a small, consistent set of "hot cores left behind after
extraction"** — files where sibling concerns were extracted AROUND a monolithic
core that stayed:
- `tasks_pkg/manager.py` (3236), `orchestrator.py` (2726), `autopilot.py` (2768) — Unit 1
- `llm_dispatch/api.py` (1869) — Unit 2
- `database/_core.py` (2433) — Unit 7
- `mcp/client.py` (2413) — Unit 12

Plus a handful of concern-boundary splits (`tool_env` misplacement, `oauth/codex`,
`self_update`, `image_gen`, `file_reader`) and BIG-but-cohesive files that should
NOT be split (registries + data tables + shared cores: `_core_schema`,
`agent_core/events`, `_sse_core`, `config` tables, `report_engine`).

**The non-structural findings that matter as much as any split:**
- A live endpoint/autopilot DUAL-implementation (Unit 4) — a half-finished
  strangler-fig migration to finish or park, not a file to split.
- Four correctness invariants that a boundary-respecting refactor could still
  break (Unit 5's compaction gate floor + expand-starvation TTL, Unit 7's
  cross-backend `_CRITICAL_COLUMNS`, Unit 11's paper double-render guard).
- Two trust boundaries verified single-sourced (Unit 8's egress choke-point +
  billing cost engine) and one AST-enforced architectural boundary (Unit 9).

**The stale `docs/refactor_decomposition_proposal.md` is superseded** by this
`docs/modules/` set on every file it names; and the CLAUDE.md §1 map drifted in
most units (missing `_pg_ownership`, the `tofu_search` extraction, the grown
`agent_core/`, the undercounted `database/`) — the panorama should be re-scanned
against these 12 docs.

---

*Survey complete: all 12 units documented in `docs/modules/`.*

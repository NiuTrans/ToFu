# Module Design Doc — Unit 3: Tools & Execution (`tools/`, `handlers/`, `project_mod/`, `browser/`, search/fetch)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md`). This unit
> is Layer ⑥ (Tools): the **definition → dispatch → handler → implementation**
> chain for every tool the LLM can call.
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11. `list_dir`
> overcounts this tree too — all numbers are `wc -l`. Every MISCUT/BIG verdict
> cites competing responsibilities or line ranges; size alone is never the argument.
>
> **Scope boundary vs Unit 1:** Unit 1 already documented `tool_dispatch.py`,
> `executor.py`, `executor_image.py`, `streaming_tool_executor.py`, and the
> `handlers/` package *from the task-engine side* (their size/status verdicts
> live there). This doc does NOT re-litigate those size verdicts. Its job is the
> **seam question** (§2): is the definition/dispatch/handler/implementation
> boundary clean and single-directional across the packages, or are there
> back-edges and duplicated responsibility?

---

## 1. The four-layer tool chain (what lives where)

A tool call travels through four distinct layers, each owned by a different
package. This separation is the whole point of the unit:

```
  ① SCHEMA (what the LLM sees)          lib/tools/*.py  — pure dicts, zero logic
        │  assemble_tool_list(ctx)
        ▼
  ② REGISTRY / DISPATCH (routing)       lib/tools/registry.py  (ToolSpec + assembly)
        │                                lib/tasks_pkg/tool_dispatch.py  (parse→gate→exec)
        │                                lib/tasks_pkg/executor.py  (tool_registry singleton)
        ▼
  ③ HANDLER (per-tool glue)             lib/tasks_pkg/handlers/*.py  — log→exec→meta→finalize
        │
        ▼
  ④ IMPLEMENTATION (does the work)      lib/project_mod/  (file ops, run_command)
                                         tofu_search  (web search + fetch — EXTRACTED, §4)
                                         lib/browser/  (extension automation)
                                         lib/mcp/, lib/memory/, lib/scheduler/ (other impls)
```

- **Schema** (`lib/tools/`) is *declarative* — `PROJECT_TOOL_WRITE_FILE` etc.
  are literal JSON-schema dicts. `registry.py` wraps them in `ToolSpec`s and
  `assemble_tool_list(ctx)` emits the cache-stable ordered list. Confirmed: read
  of `lib/tools/project.py` shows it is 100% dict definitions + the multi-root
  hint helper — no execution logic.
- **Dispatch** is split between `registry.py` (which specs are active +
  cache-stable order) and `tasks_pkg/tool_dispatch.py`+`executor.py` (parse the
  LLM's tool call, gate it, run the handler). The registry's `handler=` field
  wires a spec to its handler so schema+gate+handler ship from ONE package.
- **Handler** (`tasks_pkg/handlers/`) is the thin glue — most bodies collapse to
  `simple_call(...)` (the `_adapter.py` DRY primitive). The handler owns the
  `task` dict, emits meta, calls the implementation.
- **Implementation** is where the actual work happens, and it lives in
  *different packages* — this is exactly where a segmentation problem would hide.

---

## 2. The analytical payload: is the seam clean and single-directional?

**Verdict: the seam is clean and downward-directional, with ONE structural
oddity (`tool_env.py`) and TWO trivial, deliberately-lazy back-edges.** The
"incorrect segmentation" failure mode for tools — a handler re-implementing
schema knowledge, or an implementation package reaching back up into the task
engine — is **largely absent**. Evidence from tracing every cross-package import:

### 2a. Direction ① — schema is imported DOWN by handlers (correct)

Handlers import schema *names* from `lib/tools/`, never the reverse:
- `handlers/project.py:23` → `from lib.tools import PROJECT_TOOL_NAMES, build_project_tool_meta`
- `handlers/misc.py:16` → `from lib.tools import (…)`
- `handlers/browser.py:13` → `from lib.tools import BROWSER_TOOL_NAMES, IMAGE_GEN_TOOL_NAMES`

The schema package (`lib/tools/`) does NOT import handlers — the only
`handlers`/`tasks_pkg` strings inside `lib/tools/*.py` are **doc comments**
(`registry.py:559`, `project.py:441-444`, `todo.py:10-23`), not code imports.
So a handler never re-declares a schema and the schema never knows its handler
exists (they're wired by the `ToolSpec.handler=` field at registration). **This
is the clean version of the definition↔handler boundary.**

### 2b. Direction ② — `project_mod/` does NOT structurally reach back into `tasks_pkg/`

The critical back-edge test ("does the implementation package reach up into the
task engine?"). Result: **essentially no.** Across all 13 `project_mod/` files
there is exactly **ONE runtime code edge**:
- `project_mod/config.py:410` — `from lib.tasks_pkg.manager import _chat_runtime,
  _latest_task_for_conv`, and it is a **lazy in-function import inside a
  try/except that fails OPEN** (a "is this conv still live?" probe used to bound
  the `_conv_roots` eviction cache). The docstring explicitly says it fails open
  so an import cycle never blocks eviction. This is a deliberate, guarded probe,
  not a structural dependency.

The only other `tasks_pkg` mentions in `project_mod/` are **doc-comment
references** (`indexer.py:17,202` and `write_tools.py:85` describe which layer
owns the `task` dict). So `project_mod` is a genuinely standalone file-ops
library that the task engine calls *down* into — the correct direction. It's
reached via `from lib.project_mod import execute_tool` in `handlers/project.py`.

### 2c. Direction ③ — the ONE structural oddity: `lib/tools/tool_env.py`

`tool_env.py` (536 lines) is the exception that proves the rule. It lives in the
*schema* package (`lib/tools/`) but is NOT schema — it's the per-request custom-tool
runtime for headless `/api/v1/agent/run` (mint/dispose a `ToolEnvironment`
carrying caller-supplied schemas AND handlers, scoped to one task). Because it
executes tools, it reaches DOWN into the execution layer:
- `tool_env.py:231` → `from lib.tasks_pkg.executor import _finalize_tool_round`
- `tool_env.py:286` → `from lib.tasks_pkg.manager import append_event`
- `tool_env.py:335` → `from lib.project_mod import execute_standalone_command`

These are all lazy in-function imports, but the placement is the smell: a file
in the *definition* package (`lib/tools/`) imports from the *dispatch* and
*implementation* packages. That's a **layering inversion** — `tool_env.py` is
architecturally a sibling of `handlers/` or `ephemeral.py` (its own docstring
says it "mirrors `lib/llm_dispatch/ephemeral.py`"), not of the schema dicts it
sits next to. Split candidate (§7).

### 2d. Seam summary

| Edge | Direction | Verdict |
|---|---|---|
| handlers → `lib/tools` schema | down (import names) | ✅ clean |
| `lib/tools` schema → handlers | none (doc comments only) | ✅ clean |
| handlers → `project_mod` impl | down (`execute_tool`) | ✅ clean |
| `project_mod` → `tasks_pkg` | 1 lazy fail-open probe (`config.py:410`) | ✅ acceptable |
| `tool_env.py` (in `lib/tools/`) → executor/manager/project_mod | down, but from the wrong package | ⚠️ layering inversion |
| registry `ToolSpec.handler=` → handler | declarative wiring | ✅ clean (the seam's keystone) |

**The seam is single-directional except `tool_env.py`.** No handler
re-implements schema; no implementation package structurally depends on the task
engine. The registry's `handler=` field is the keystone that keeps
schema+gate+handler co-located without coupling the packages.

---

## 3. A correction to the CLAUDE.md map: `search/` and `fetch/` are GONE (extracted)

CLAUDE.md §1 lists `lib/fetch/` and `lib/search/` as in-tree packages. **They do
not exist on disk** (`find lib/fetch lib/search` → "No such file or directory").
They were **extracted to a standalone `tofu_search` package** — the same
extraction pattern as `tofu-trading`. Evidence:
- `handlers/search.py:14` → `from tofu_search import fetch_page_content,
  looks_like_text_asset, perform_web_search`
- `lib/search_bridge.py` (327 lines) is the **host-side bridge**: it installs
  chatui's `dispatch_chat` (LLM filter seam), `lib.browser` (browser fallback
  seam), and `lib.auth_sources` (authenticated-fetch seam) into `tofu_search`
  via `tofu_search.configure()` + `register_*_provider()`.

This is a **clean extraction with a seam**: `tofu_search` is host-agnostic and
`search_bridge.py` fills its three provider seams — architecturally identical to
how `llm_dispatch` fills `tofu_search`'s LLM seam. So the search/fetch
"segmentation" is not miscut; it's been promoted out of the tree entirely. **Any
doc doc that says otherwise (including the CLAUDE.md §1 map) is stale.**

---

## 4. Module inventory (real `wc -l`, size verdict, status, tests)

### 4.1 `lib/tools/` — schema definitions + registry (4525 LOC, 13 files)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `registry.py` | 1218 | **BIG** | HOT | `test_tool_registry`, `test_schema_registry`, `test_core_tool_isolation`, `test_explicit_tools_passthrough` |
| `project.py` | 607 | OK (schema dicts) | HOT | `test_project_tools`, `test_tool_root_pill` |
| `tool_env.py` | 536 | **MISPLACED** | live (headless) | `test_custom_tool_isolation` |
| `conversation.py` | 516 | OK (schema dicts) | HOT | `test_project_feed_read_tool` |
| `browser.py` | 516 | OK (schema dicts) | HOT | via browser e2e |
| `meta.py` | 382 | OK | HOT | `test_tool_registry` |
| `search.py` | 193 | OK (schema dicts) | HOT | via search e2e |
| `todo.py` | 174 | OK | HOT | `test_frontend_tool_completion` |
| `image_gen.py` | 108 | leaf | HOT | — |
| `image_edit.py` | 98 | leaf | HOT | — |
| `human_guidance.py` | 77 | leaf | HOT | — |
| `__init__.py` | 73 | OK (facade) | — | — |
| `code_exec.py` | 27 | leaf | HOT | — |

`registry.py` — **BIG, and it bundles 2 concerns.** (a) The `ToolSpec`/`ToolContext`
dataclasses + `assemble_tool_list` + the built-in spec registrations + plugin
discovery (the declarative assembly engine). (b) The prompt-cache stability
machinery: `_multiroot_sticky` latch (~lines 78-130), the per-conversation
tool-SCHEMA latch (`latch_tool_list`/`tool_list_diverged`/`tool_list_diff`,
~130+), which is a distinct concern (cache-key stability, sibling to Unit 1's
cache invariants). The two share the spec list but the latch machinery could be
`registry_latch.py`. Classified BIG; see §7.

`project.py`/`conversation.py`/`browser.py`/`search.py` — all OK. These are
pure schema-dict files (verified: `project.py` is entirely `PROJECT_TOOL_*`
dicts + the `with_multiroot_hint` helper). Large because tool descriptions are
verbose prose, NOT because of logic. Do not split.

`tool_env.py` — **MISPLACED (see §2c).** Not miscut internally (it's cohesive:
mint/dispose a per-request tool env), but it's in the wrong package.

### 4.2 `lib/tasks_pkg/handlers/` — the glue layer (3085 LOC, 10 files)

Size verdicts for these live in Unit 1 (task_engine.md §3.8); recap: `misc.py`
(716, BIG — hosts the Project-Brain coordination handlers + legacy misc, split
candidate → `handlers/coordination.py`), `search.py` (671), `project.py` (476),
all others OK. `_adapter.py` (195) is the shared `simple_call`/`run_batch_concurrent`
DRY primitive — the reason most handler bodies are one line. `_read_gate.py`
(352) owns the read-before-edit policy (`check_read_before_edit`,
`partition_batch_edits`). **Seam note:** every handler here imports its schema
NAMES from `lib/tools/` (§2a) and its implementation from the impl packages —
it re-declares neither. Correct glue layer.

### 4.3 `lib/project_mod/` — file-ops implementation (9327 LOC, 13 files)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `write_tools.py` | 1482 | **BIG** | HOT | `test_write_tools_root_attribution`, `test_tool_changes` |
| `run_command.py` | 1375 | **BIG** | HOT | `test_run_command_sticky_cwd`, `test_run_command_danger_quoted`, `test_run_command_not_run_meta` |
| `read_tools.py` | 1316 | **BIG** | HOT | `test_project_tools`, `test_recently_accessed_files_string_reads` |
| `tools.py` | 1080 | OK (dispatch façade) | HOT | `test_project_tools` |
| `command_analysis.py` | 874 | OK | live | `test_command_analysis_extraction` |
| `config.py` | 761 | OK | HOT | via project e2e |
| `modifications.py` | 709 | OK | HOT | `test_tool_changes` |
| `scanner.py` | 447 | OK | HOT | via indexer e2e |
| `indexer.py` | 433 | OK | live | via project e2e |
| `gitignore_suggest.py` | 294 | OK | live | — |
| `portable_sandbox.py` | 254 | OK | live | — |
| `config` `__init__.py` | 152 | OK (facade) | — | — |
| `abs_path_guard.py` | 150 | OK | HOT | `test_abs_path_guard` |

`tools.py` — OK. It is now a **pure dispatch façade** (its own docstring says the
tool groups were extracted to `read_tools`/`write_tools`/`run_command` and it
"retains `browse_directory` + `execute_tool` + re-exports"). This is a
*successful* prior split — 80+ lines of it (verified) are `# backward compat`
re-exports, exactly the facade pattern `compaction/__init__` uses. The remaining
logic is `execute_tool` (the `_EXEC_HANDLERS` name→impl registry) + `browse_directory`.

`write_tools.py` / `read_tools.py` / `run_command.py` — **BIG but each is one
cohesive tool family.** `run_command.py` is the largest single concern
(shell exec + process-tree kill + snapshot/diff + destructive-command guards);
`command_analysis.py` (the command classifier) is already extracted from it.
`write_tools.py` bundles write_file/apply_diff/insert_content + the closest-match
fuzzy finder + workspace-root auto-registration. These are BIG-but-right: each
file is one tool family, not multiple unrelated jobs. A finer split (e.g.
`run_command` → exec + guards + snapshot) is possible but low-value; classified
BIG, defer.

### 4.4 `lib/browser/` — extension automation (1756 LOC, 7 files)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `handlers.py` | 560 | OK | live | `test_browser_read_payload`, `test_browser_pdf_download_guard` |
| `queue.py` | 415 | OK | live | `test_browser_queue_ttl`, `test_browser_async_poll` |
| `advanced.py` | 354 | OK | live | via browser e2e |
| `display.py` | 166 | OK | live | — |
| `dispatch.py` | 157 | OK | live | — |
| `fetch.py` | 61 | leaf | live | — |
| `__init__.py` | 43 | OK (facade) | — | — |

All OK — this package is well-bounded. It's reached two ways: directly by
`handlers/browser.py` (the browser tool) and indirectly by `search_bridge.py`'s
`_ChatuiBrowserProvider` (the fetch fallback seam). No back-edges into `tasks_pkg`.

### 4.5 The extracted / bridged implementations (not in-tree)

| Unit | Where | LOC (bridge) | Tests |
|---|---|--:|---|
| web search + fetch | `tofu_search` (external pkg) + `lib/search_bridge.py` | 327 (bridge) | `test_search_bridge_config`, `test_streaming_websearch_delegation`, `test_streaming_fetch_url_delegation`, `test_search_marginalia_deepen` |
| tool-arg repair | `lib/tool_input_repair.py` | 1526 | `test_tool_input_repair`, `test_malformed_tool_args`, `test_paper_tool_args_repair` |

`lib/tool_input_repair.py` (1526) — **BIG**, sits between dispatch and handler:
6 value-repair patterns + a param-key alias layer + structural transforms
(`MultiEdit`→`apply_diffs`, `AskUserQuestion`→`ask_human`). Cohesive (all
schema-driven arg repair) but large; it's the one place tool *schema knowledge*
is legitimately duplicated (it must know param names to alias them) — but that's
its whole purpose, not a leak. Classified BIG, defer.

---

## 5. Invariants (must not be broken by a refactor)

1. **Tool ordering in `assemble_tool_list` is prompt-cache-critical.** The
   registration order (search→fetch→read_files→project|code_exec→browser→
   desktop→image_gen→conv_ref→human_guidance→⟨boundary⟩→memory→scheduler→
   swarm→mcp) is A/B-validated and reproduces the cached prefix byte-for-byte.
2. **The per-conversation tool-SCHEMA latch is load-bearing.** Any byte change
   in the tools array between rounds (a toggle, an MCP re-emit) invalidates the
   whole ~65k-token cached prefix; `registry.py`'s latch + `_multiroot_sticky`
   prevent flapping. Pairs with Unit 1 + Unit 2 cache invariants.
3. **Plugin visibility is fail-closed + per-request.** `assemble_tool_list`
   evaluates a plugin spec only when its `plugin_name` is in the request's
   `enabled_plugins` allow-list (default: no third-party plugins). Guarded by
   `test_core_tool_isolation`.
4. **`ToolSpec.handler=` co-locates schema+gate+handler.** The executor calls
   `sync_spec_handlers(tool_registry)` at startup; late plugins self-sync. Do
   not re-introduce the hardcoded `if feature:` ladder this replaced.
5. **`tool_env` mint/dispose leaves `tool_registry` byte-identical.** Custom
   tool names must match `custom__<ident>` and never collide with a built-in;
   `test_custom_tool_isolation` pins it.
6. **`project_mod/config.py`'s live-task probe fails OPEN.** The one back-edge
   into `tasks_pkg.manager` must never block `_conv_roots` eviction on an import
   cycle — an unbounded cache is worse than a rare mis-eviction.
7. **Read-before-edit gate** (`handlers/_read_gate.py`): apply_diff/insert_content
   are refused unless the target was read earlier; batch variants gate per-path.
8. **Remote callers are absolute-path-restricted** (`abs_path_guard`): agents:run
   / chat-key / compat callers can't read/write outside a registered root.

---

## 6. Known debt (grounded)

- **`tool_env.py` is in the wrong package** (§2c) — the one genuine layering
  inversion in the unit.
- **`registry.py` (1218) bundles the assembly engine with the cache-latch
  machinery** (§4.1) — a clean internal seam.
- **CLAUDE.md §1 lists `lib/fetch/`+`lib/search/` that no longer exist** (§3) —
  a doc-drift bug; they're `tofu_search` + `search_bridge.py` now.
- The `handlers/misc.py` Project-Brain split (from Unit 1) also lands in this
  unit's scope.
- `write_tools`/`read_tools`/`run_command` (each >1300) are BIG-but-cohesive;
  finer splits are low-value.

---

## 7. Segmentation verdict (this unit)

**Correctly bounded — leave as-is:**
All schema-dict files (`tools/project`, `conversation`, `browser`, `search`,
`meta`, `todo`, `image_gen`, `image_edit`, `human_guidance`, `code_exec`);
`project_mod/tools` (a *successful* prior facade split), `command_analysis`,
`config`, `modifications`, `scanner`, `indexer`, `gitignore_suggest`,
`portable_sandbox`, `abs_path_guard`; all of `browser/`; `handlers/_adapter`,
`_read_gate`, and the small handlers; `search_bridge.py` (a clean host-seam bridge).

**Miscut / misplaced — should move or split (priority order):**

1. **Relocate `lib/tools/tool_env.py` → `lib/tasks_pkg/tool_env.py`** (or a new
   `lib/agent_core/` sibling). It is the per-request custom-tool *runtime*, not a
   schema; it imports DOWN into `executor`/`manager`/`project_mod` from the
   *definition* package (§2c). Its own docstring says it mirrors
   `llm_dispatch/ephemeral.py` — put it next to the execution layer it belongs
   to. Low risk (lazy imports already; `test_custom_tool_isolation` guards it).
2. **`registry.py` (1218) → extract `registry_latch.py`** for the tool-SCHEMA
   latch + `_multiroot_sticky` machinery (~lines 78-200 cluster), leaving the
   `ToolSpec`/assembly engine in `registry.py`. Behind `test_tool_registry` +
   `test_schema_registry`.
3. **`handlers/misc.py` → `handlers/coordination.py`** (carried from Unit 1 —
   the charter/board/peer Project-Brain handlers).

**Big but optional (defer unless touched):**
`project_mod/write_tools` (1482), `run_command` (1375), `read_tools` (1316) —
each one cohesive tool family; `tool_input_repair.py` (1526).

**Do NOT split:** the schema-dict files (large only because descriptions are
prose), `project_mod/tools.py` (already the right facade shape).

---

## 8. Comparison to Units 1–2 (the running thesis)

- **The tool seam is the cleanest cross-package boundary documented so far.**
  Unlike Unit 2's `cache_tracking` back-edge (LLM layer → task layer), the
  tools chain flows strictly downward: schema ← handler ← impl, wired by the
  declarative `ToolSpec.handler=` field. The registry pattern (a tool author
  registers a spec once, zero core edits) is *why* the seam stayed clean as
  tools multiplied — it's the structural opposite of the `if feature:` ladder it
  replaced.
- **Extraction-to-external-package is a third split outcome** (beyond
  `compaction/`-clean and `orchestrator`-incomplete): `tofu_search` +
  `search_bridge.py` shows a whole subsystem promoted out of the tree with a
  provider-seam bridge. That's the *most* decoupled outcome and a template for
  future extractions.
- **The one real finding is a misplacement, not a miscut:** `tool_env.py` is
  cohesive but lives in the wrong package. That's a different defect class than
  the `manager.py`/`api.py` "one file, many jobs" miscuts — worth distinguishing
  in the eventual refactor plan.

---

*Next unit: Unit 4 (Orchestration / DAG — `orchestration*.py`, `swarm/`).*

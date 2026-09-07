# Tool Plugins — Multi-Tenant Visibility Isolation

> **Status:** implemented (2026-06).
> **Scope:** makes the process-global `tofu.tools` entry-point plugin
> mechanism safe on a **shared, multi-tenant** server (notably the headless
> `POST /api/v1/agent/run` API). A plugin installed in the process is now
> only **exposed to the model** for a request that explicitly allow-lists it
> — so one caller's installed plugin can no longer pollute another caller's
> tool surface (or, via an imperative tool `description`, their behaviour).

This is the **install-time / process-global** sibling of
[`CUSTOM_TOOLS.md`](CUSTOM_TOOLS.md) (per-request, caller-supplied tools).
Use the table in §7 to pick which mechanism you want.

---

## 1. The problem

A third-party package contributes native tools by declaring a `tofu.tools`
entry point (see `lib/tools/registry/_plugins.py::discover_plugin_specs`):

```toml
[project.entry-points."tofu.tools"]
weather = "my_pkg.weather:register"
```

At **import time**, `discover_plugin_specs()` loads every such entry point into
the same process-global `_TOOL_SPECS` list as the built-ins. That was designed
for a **single-tenant** deployment — "this process is mine, the tools I
installed are the tools I want."

But Tofu also runs as a **shared headless runtime**: many independent callers
hit one `POST /api/v1/agent/run` process. There, process-global is wrong:

* **Every** caller sees **every** installed plugin's tool schema.
* A plugin's `description` is free text the model obeys. A real-world example
  (`liantong_kb`) shipped *"回答任何业务问题前都必须先调用本工具"* ("you MUST call
  this tool before answering any question") — perfectly reasonable for its own
  dedicated deployment, but on a shared server it hijacks **unrelated**
  conversations. This is the concrete "context pollution" bug that motivated
  the work.

| Concern | Per-request isolated *before* this work? | Where it lives |
|---|---|---|
| Built-in tool **gating** (search/project/…) | ✅ yes — `ToolContext` flags | `assemble_tool_list(ctx)` |
| Per-request **custom** tools (`tools=[…]`) | ✅ yes — `ToolEnvironment` | `CUSTOM_TOOLS.md` |
| `tofu.tools` **entry-point plugins** | ❌ **no — process-global, always on** | `discover_plugin_specs()` → `_TOOL_SPECS` |

The fix closes that last row **without** changing how plugins are authored or
installed, and without touching any plugin package.

---

## 2. The design — a per-request visibility allow-list

A plugin spec is still loaded once, process-global. What changes is **whether
`assemble_tool_list` evaluates it for a given request**. Three pieces:

1. **Provenance tags on every spec.** `ToolSpec` gains two fields:
   `source` (`'builtin'` | `'plugin'`) and `plugin_name` (the entry-point
   name, e.g. `'liantong_kb'`). Built-ins keep the defaults
   (`source='builtin'`).

2. **Auto-stamping at discovery.** `discover_plugin_specs()` hands the plugin's
   `register(...)` callback a **wrapper** instead of `register_tool_spec`
   directly. The wrapper stamps `source='plugin'` + `plugin_name=<ep.name>`
   onto each spec via `dataclasses.replace` before registering it. The plugin
   author does **nothing** — they can't forget to tag, and they can't spoof a
   built-in. One entry point may register several specs; they all inherit its
   `plugin_name`.

3. **The gate.** `ToolContext` gains `enabled_plugins: set[str] | None`.
   `assemble_tool_list` skips any `source='plugin'` spec whose `plugin_name`
   isn't allowed. Built-ins are **never** gated.

```python
def plugin_allowed(self, plugin_name: str) -> bool:
    if self.enabled_plugins is None:     # gate fully open (single-tenant)
        return True
    return bool(plugin_name) and plugin_name in self.enabled_plugins
```

The three states of `enabled_plugins`:

| Value | Meaning |
|---|---|
| `None` | **All** plugins visible — single-tenant / legacy "everything I installed is on". |
| `set()` | **No** plugins visible — the safe shared-server default. |
| `{names}` | Only plugins whose `plugin_name` is listed. |

> **Fail-closed by design.** A plugin spec with an empty `plugin_name` (a
> misconfigured plugin that somehow dodged stamping) is treated as **not**
> allow-listed unless the gate is fully open (`None`). We never leak by
> accident.

---

## 3. Resolving the allow-list per request

`resolve_enabled_plugins(cfg)` (`lib/tools/registry/_plugins.py`) is the single
resolution point, called from `lib/tasks_pkg/model_config.py::_assemble_tool_list`
when it builds the `ToolContext`. Resolution order — **first present wins**:

1. **`cfg['plugins']`** — request-scoped. A headless caller sets it via
   `config.plugins` on `/api/v1/agent/run`.
2. **`TOFU_DEFAULT_TOOL_PLUGINS`** env var — deployment-wide default. A
   dedicated single-tenant install (e.g. liantong's `app/` copy) sets this
   once and never passes `plugins` per request.
3. **Neither set → fail-closed**: `set()` → no third-party plugins.

Each level accepts the same vocabulary, normalised by `_parse_plugin_spec`:

| Input | Result |
|---|---|
| `'*'`, `'all'`, `['*']` | `None` (all plugins visible) |
| `'liantong_kb'`, `['a','b']`, `'a, b c'` | a `set` of names |
| `None`, `''`, `[]` | `set()` (none) |

The `'*'` sentinel maps to `None` because that is exactly the `enabled_plugins`
value meaning "allow everything".

---

## 4. Data flow

```
POST /api/v1/agent/run                 lib/tasks_pkg/model_config.py
  body.config.plugins ──► _build_cfg ──► cfg['plugins']
                                              │
                                              ▼
                            resolve_enabled_plugins(cfg)
                              cfg['plugins'] ─► TOFU_DEFAULT_TOOL_PLUGINS ─► set()
                                              │
                                              ▼
                            ToolContext(enabled_plugins=…)
                                              │
                                              ▼
                            assemble_tool_list(ctx)
                              builtin spec  → always evaluated
                              plugin  spec  → only if ctx.plugin_allowed(name)
```

The **caller-supplied `tools=[…]` short-circuit** in `_assemble_tool_list`
(documented in `COMPAT_OPENAI.md`) returns *before* the `ToolContext` is built,
so a request that fully specifies its own tool list bypasses the plugin gate
entirely. Plan Mode is the safety exception: its request policy removes
unproven/mutating schemas and injects the canonical built-in `ask_human`
contract before returning the final executable catalog.

---

## 5. Usage

### 5.1 Shared multi-tenant server (the safe default)

Do nothing. With neither `config.plugins` nor `TOFU_DEFAULT_TOOL_PLUGINS` set,
**no** third-party plugin is exposed to any request. Built-in tools work as
always.

A caller that *wants* a plugin opts in per request:

```jsonc
POST /api/v1/agent/run
{
  "model": "…",
  "messages": [...],
  "config": { "plugins": ["liantong_kb"] }      // only this caller sees it
}
```

### 5.2 Dedicated single-tenant deployment

Set the deployment default once and forget it:

```bash
# expose ALL installed plugins (old behaviour)
export TOFU_DEFAULT_TOOL_PLUGINS='*'

# …or expose a specific set deployment-wide
export TOFU_DEFAULT_TOOL_PLUGINS='liantong_kb,weather'
```

A per-request `config.plugins` still overrides the env default for that one
request.

### 5.3 Discovering what's installed

`available_plugins()` (`lib/tools/registry/_plugins.py`) maps each loaded plugin name →
the spec keys it registered (built-ins excluded). Surface it from ops tooling
or a `/api/v1/capabilities`-style endpoint so a caller knows what names are
valid in `config.plugins`.

---

## 6. What this is — and is NOT

* **It IS** a *visibility* boundary: an un-allow-listed plugin's schema is never
  put in front of the LLM, so the model can't call it and can't be steered by
  its `description`.
* **It is NOT** a security sandbox. A plugin's `register()` and handler code
  still run **in-process** (entry points are operator-trusted install-time
  code, exactly like a pip dependency). If you need to run *untrusted*
  caller-supplied tool logic with isolation, that's the per-request
  `ToolEnvironment` (`CUSTOM_TOOLS.md`) with its `client` / `webhook` /
  `sandbox` backends — not this mechanism.

---

## 7. Plugins vs. custom tools — which do I want?

| | `tofu.tools` **plugin** (this doc) | Per-request **custom tool** (`CUSTOM_TOOLS.md`) |
|---|---|---|
| Who provides it | Operator, at install time (pip + entry point) | The API caller, in the request body |
| Trust | Trusted in-process code | Untrusted — schema-only by default |
| Lifetime | Process-global, gated per request | One task, then disposed |
| Names | Author-chosen | Must match `^custom__…$` |
| Isolation | **Visibility** allow-list (`enabled_plugins`) | Full handler isolation + egress/RCE guards |
| Turn it on | `config.plugins` / `TOFU_DEFAULT_TOOL_PLUGINS` | Send `tools=[…]` |

---

## 8. Files

| File | Change |
|---|---|
| `lib/tools/registry/` | `ToolSpec.source` / `.plugin_name` fields; `ToolContext.enabled_plugins` + `plugin_allowed()`; `assemble_tool_list` visibility gate; auto-stamping wrapper in `discover_plugin_specs`; `resolve_enabled_plugins()`, `_parse_plugin_spec()`, `available_plugins()`; package "Plugin isolation" note |
| `lib/tools/__init__.py` | export `resolve_enabled_plugins`, `available_plugins` |
| `lib/tasks_pkg/model_config.py` | resolve `enabled_plugins` from cfg and pass into `ToolContext` |
| `routes/api_v1/agent_run.py` | `config.plugins` alias → `cfg['plugins']`; docstring + OpenAPI note |
| `tests/test_plugin_isolation.py` | **new** — resolution order, three-state gate, fail-closed empty name, auto-stamping, `available_plugins` |

---

## 9. Guardrails (tests)

`tests/test_plugin_isolation.py` asserts:

* `resolve_enabled_plugins`: cfg wins over env; env default when cfg absent;
  absent-everything is fail-closed `set()`; `'*'` → `None`; empty → `set()`.
* a `source='plugin'` spec is **hidden** under `set()`, **visible** when its
  name is allow-listed or the gate is open (`None`), and **not** unlocked by a
  different plugin's name;
* built-ins are present regardless of the allow-list;
* a plugin spec with a **blank** `plugin_name` is hidden under a non-open gate
  (fail-closed) but visible when fully open;
* `discover_plugin_specs` **auto-stamps** `source`/`plugin_name` onto a spec a
  plugin registers without setting them itself;
* `available_plugins()` lists installed plugins and excludes built-ins.

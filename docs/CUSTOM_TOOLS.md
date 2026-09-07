# Per-Request Custom Tools — Design & Architecture

> **Status:** implemented (2026-06).
> **Scope:** lets a headless `POST /api/v1/agent/run` request supply its own
> tool definitions for the duration of **one task**, with a hard guarantee
> that nothing the request defines persists into process-global state or
> leaks into any other user's task.

---

## 1. The problem

Tofu can run as a headless agent runtime ("you bring the model, we bring the
agent"). The natural next step is "you bring the **tools** too." But the tool
machinery was built single-tenant:

| Concern | Per-request isolated *before* this work? | Where it lives |
|---|---|---|
| Tool **schemas** (what the LLM sees) | ✅ yes | `assemble_tool_list(ctx)` builds a fresh `list[dict]` per task |
| Sub-agent **state** (messages/model) | ✅ yes | `SubAgent.__init__` (`lib/swarm/agent.py`) |
| Tool **handler resolution** (name → executable) | ❌ **no — process-global** | `tool_registry.lookup()` (`lib/tasks_pkg/executor/_execute.py`) |

Both reference subsystems prove the gap:

* **MCP** is a *fully global singleton* — one `get_bridge()`, one
  `mcp_servers.json`, one `_tool_index`, and it even **monkey-patches**
  `tool_registry.lookup`. Copying MCP would make every user share every
  other user's tools. It is the **anti-pattern** for multi-tenant isolation.
* **The swarm** isolates schemas (`scope_tools_for_role`) and per-agent
  state, but still falls back to the **same global** `tool_registry` for
  handler resolution. There is no per-task handler override anywhere.

Meanwhile the **model-routing v2 route-group** path
(`lib/model_routing/dispatch_adapter.py` + `lib/llm_dispatch/ephemeral.py`)
already solved the *lifecycle* half of this exact problem: mint-per-request →
isolate-by-uniqueness → **dispose-on-terminal**, with idempotent disposal, a
bounded handle table, and synchronous cleanup on every error path. That
machinery is registry-agnostic and reused verbatim here.

So the design reduces to **one missing seam**: invert handler resolution to
be per-request, and wrap it in the ephemeral lifecycle.

---

## 2. The central object — `ToolEnvironment`

`lib/tools/tool_env.py` owns a single per-request object that holds **both**
the custom schemas and their handlers. It is created in the route, attached to
the task as `task['_tool_env']`, and disposed when the task reaches a terminal
state. **No process-global registry is ever mutated by a request.**

```
ToolEnvironment
  handle_id          unique id (custom_env_<hex>)
  owner              opaque audit tag (api key id)
  tools              list[_CustomTool]   (name, clean_schema, mode, write, idempotent, config)
  schemas            list[dict]          (clean {type,function} schemas → injected into the LLM tool list)
  write_names        frozenset[str]      (per-env concurrency partition)
  idempotent_names   frozenset[str]      (per-env dedup partition)
  limits             ToolLimits
  resolve(fn_name)   → ToolHandler | None   (the inverted lookup)
```

Each `_CustomTool` carries its own **execution backend mode**, so a single
request can mix client-handoff, webhook, and sandbox tools freely.

---

## 3. The inverted handler lookup (the one core change)

`_execute_tool_one` (`lib/tasks_pkg/executor/_execute.py`) already receives the live
`task` dict. The only behavioural change is to consult the task-local
resolver **before** the global registry:

```python
handler = None
_env = task.get('_tool_env')
if _env is not None:
    handler = _env.resolve(fn_name)      # task-local, request-scoped
if handler is None:
    handler = tool_registry.lookup(fn_name, round_entry)   # built-ins (unchanged)
```

The existing universal try/except safety net then wraps whichever handler was
found, so a misbehaving custom tool returns an error to the LLM instead of
aborting the task — identical to built-in tools.

The **swarm inherits this for free**: `SubAgent._dispatch_tool` threads
`'_tool_env': self.parent_task.get('_tool_env')` onto its `task_proxy`, so a
spawned sub-agent resolves the same per-request tools (subject to its
role-scoped schema allow-list).

---

## 4. The dedup / write-partition fix at dispatch time

`tool_dispatch.py` partitions tools into **write** (run serially for
filesystem-race safety) and **idempotent** (dedup-cached). Those sets were
frozen **once at import** by `_registry_tool_flags()`, so custom tools were
invisible to them — a custom *write* tool would wrongly run in the parallel
pool, and a custom read tool would never dedup.

Fix: compute the partition **per task** at the top of `execute_tool_pipeline`,
unioning the import-time base with the env's declared flags:

```python
def _task_partitions(task):
    env = task.get('_tool_env')
    if env is None:
        return _WRITE_TOOLS, _IDEMPOTENT_TOOLS
    return (_WRITE_TOOLS | env.write_names,
            _IDEMPOTENT_TOOLS | env.idempotent_names)
```

A custom tool declaring `"write": true` therefore runs in the serial write
phase; one declaring `"idempotent": true` is dedup-cached in the existing
per-task `_tool_result_cache`. The safe default (neither) → runs in parallel,
no caching.

---

## 5. Schema injection — reuse the clean path

A new `ToolSpec('custom', _build_custom, phase='capability')` is registered
**last** (after `mcp`), so built-in ordering — which is prompt-cache-critical
— is untouched and custom tools always sit at the tail. `_build_custom(ctx)`
reads the validated clean schemas from `ctx.cfg['_customToolSchemas']` (set by
the route at mint time) and returns them. Zero global mutation; rides the
existing per-task `assemble_tool_list`.

---

## 6. Execution backends — a spectrum, not one choice

`ToolEnvironment` dispatches each call to a backend selected by the tool's
`execution.mode`. All three sit behind the same isolation guarantees.

### 6.1 `client` (default — zero-trust)
The server takes **schemas only**. When the model calls a `client` tool, the
handler emits a `custom_tool_call` SSE event `{callId, toolName, arguments}`
and **blocks** (`request_client_tool_result`, abort-aware, bounded by
`limits.per_call_timeout_s`). The client executes the tool and POSTs the
result back to `POST /api/v1/tasks/{id}/tool_result {call_id, content}`. **The
server never executes user logic** → no RCE, no SSRF, no contamination by
construction. This is the OpenAI/Anthropic function-calling model and the
correct default for a public API.

### 6.2 `webhook` (declarative remote functions)
The tool declares `{url, auth, headers, timeout_s}`. On a call, the server
POSTs `{tool, arguments}` to the URL and returns the response body as the tool
result. Risk shifts from code-exec to **SSRF**, closed by
`lib.byo_egress.validate_egress_url` at **both** mint time and call time
(defeats DNS rebinding) — the same guard ProviderAccess routes use
(`169.254.169.254` / link-local always blocked; private ranges gated by
`TOFU_BYO_BLOCK_PRIVATE`).

### 6.3 `sandbox` (heavy opt-in)
The tool declares a `command`; the server runs it via
`execute_standalone_command` with `TOFU_TOOL_ARGS` = JSON(args) in the
environment. This is untrusted code execution, so it is **disabled by
default** and gated behind the operator env flag
`TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX=1`. When off, a request that declares a
sandbox tool is rejected with a clear error.

---

## 7. Lifecycle — reuse ephemeral disposal verbatim

Mirrors `lib/llm_dispatch/ephemeral.py`:

* `mint_tool_env(tools=…, owner=…, allow_sandbox=…)` validates and builds the
  env, tracked in a bounded `_envs` dict (ceiling `_MAX_TOOL_ENVS = 1024`).
* The route disposes **synchronously** on every validation / billing / spawn
  error path (mirroring the four `dispose_ephemeral_slot` sites in
  `agent_run.py`).
* On successful spawn, a daemon thread runs `dispose_tool_env_after_terminal`
  (polls `task['status']` until terminal, 1-hour ceiling), exactly like
  `dispose_after_terminal` for slots.
* `dispose_tool_env` is **idempotent**.
* Client-result waiters are capped from the launch-time in-flight-task budget
  (32–512 process-wide), and callback IDs/results are bounded before entering
  the registry. This matters because a callback can otherwise retain its full
  request body while the tool thread is waiting to be scheduled.

A custom tool therefore cannot outlive its one task.

---

## 8. Enumerated contamination vectors (and how each is closed)

1. **Schema list** — per-task already (`assemble_tool_list`). ✅
2. **Handler map** — inverted to the per-task `resolve()`; the global
   `tool_registry` never receives a user handler. ✅
3. **Mandatory `custom__` namespace + collision reject** — every custom name
   must match `^custom__[A-Za-z0-9_]{1,56}$`. No built-in or `mcp__` tool uses
   that prefix, so collision is structurally impossible; we additionally
   reject any name appearing in `all_specs()` provides. A user can never
   shadow `write_file` / `run_command`. The prefix also passes the existing
   `parse_tool_calls` name guards. ✅
4. **Concurrency/dedup partitions** — unioned per-task at dispatch time (§4),
   not frozen at import. ✅
5. **`register_tool_spec` / `tool_registry.register`** — reserved for
   operator-trusted startup plugins; an AST isolation test forbids any
   `/api/v1` request module from importing or calling them. ✅
6. **No `lookup` monkey-patching** — the per-request path resolves via
   `task['_tool_env']`, never by mutating the global method (the MCP
   anti-pattern). ✅
7. **Resource exhaustion** — `ToolLimits`: `max_tools`,
   `max_total_schema_bytes`, `per_call_timeout_s`, `max_result_chars`; both the
   handle table and the client-result waiter table are bounded. The callback
   contract enforces a 128-character call ID and 200,000-character result
   before the result becomes retained process state. ✅
8. **SSRF (webhook)** — egress-validated at mint *and* call time. ✅
9. **RCE (sandbox)** — disabled unless the operator opts in. ✅
10. **Ambient authority** — custom tools receive only their own args; they get
    no implicit filesystem / DB / project handles. ✅

---

## 9. Request shape

```jsonc
POST /api/v1/agent/run
{
  "model": {"creator_id":"openai","model_id":"gpt-x"},
  "routing": {"preferred_provider_id":"provider-abc"},
  "messages": [...],
  "tools": [
    { "type": "function",
      "function": { "name": "custom__get_weather", "description": "...",
                    "parameters": { "type": "object", "properties": { ... } } },
      "execution": { "mode": "client" } },                 // default; may be omitted

    { "type": "function",
      "function": { "name": "custom__lookup", "parameters": { ... } },
      "execution": { "mode": "webhook", "url": "https://api.example.com/t",
                     "auth": "Bearer xyz", "timeout_s": 20 },
      "idempotent": true },

    { "type": "function",
      "function": { "name": "custom__fmt", "parameters": { ... } },
      "execution": { "mode": "sandbox", "command": "python3 fmt.py" },
      "write": true }
  ]
}
```

The `execution` / `write` / `idempotent` keys are **stripped** before the
schema reaches the LLM (it sees only `{type, function}`).

Client-handoff result callback:

```jsonc
POST /api/v1/tasks/{task_id}/tool_result
{ "call_id": "ctool_ab12…", "content": "72°F and sunny", "is_error": false }
```

---

## 10. Files

| File | Change |
|---|---|
| `lib/tools/tool_env.py` | **new** — `ToolEnvironment`, `_CustomTool`, `ToolLimits`, three backends, `mint_tool_env` / `dispose_tool_env` / `dispose_tool_env_after_terminal`, client-result registry, validation |
| `lib/tools/registry/_build.py` | `ToolSpec('custom', _build_custom, …)` registered last |
| `lib/tasks_pkg/executor/_execute.py` | per-task resolver consulted before global lookup in `_execute_tool_one` |
| `lib/tasks_pkg/tool_dispatch/_flags.py` | `_task_partitions(task)` — per-task write/idempotent union |
| `lib/swarm/agent.py` | thread `_tool_env` onto the sub-agent `task_proxy` |
| `routes/api_v1/agent_run.py` | parse `tools`, mint env, attach to task, dispose on terminal + every error path |
| `routes/api_v1/tasks.py` | `POST /api/v1/tasks/{id}/tool_result` callback |
| `tests/test_custom_tool_isolation.py` | **new** — isolation guards |

---

## 11. Guardrails (tests)

`tests/test_custom_tool_isolation.py` asserts:

* validation: rejects non-`custom__` names, rejects built-in collisions,
  enforces `max_tools`, strips `execution`/`write`/`idempotent` from the
  LLM-facing schema;
* **the global `tool_registry` is byte-identical before and after** minting +
  disposing an env;
* two concurrent envs each with a `custom__x` tool resolve to **their own**
  handler;
* `_task_partitions` unions env write/idempotent names;
* client-handoff: `resolve_client_tool_result` unblocks a pending
  `request_client_tool_result`;
* AST guard: no `/api/v1` request module imports/calls `register_tool_spec`
  or `tool_registry.register`.

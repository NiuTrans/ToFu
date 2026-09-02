# Tools and execution

This domain defines which tools a task may see, how schemas reach a model, how calls are validated/dispatched, and how results become model context and UI events.
MCP and plugin setup live in [`../TOOL_PLUGINS.md`](../TOOL_PLUGINS.md) and [`../TOOL_SEARCH_EXECUTION_GATEWAY.md`](../TOOL_SEARCH_EXECUTION_GATEWAY.md).

## Ownership

| Concern | Owner |
|---|---|
| Built-in tool specifications | `lib/tools/*.py` |
| Compiled tool contract and result envelope | `lib/tools/contracts.py`, `lib/tools/result_envelope.py` |
| Registry construction and collision policy | `lib/tools/registry/` |
| Per-request visibility and environment | `lib/tools/tool_env.py` |
| Unified execution gateway | `lib/tools/gateway.py` |
| Task/runtime adapters | `lib/tasks_pkg/handlers/` |
| Project file operations and bounded shared tree index | `lib/project_mod/` |
| MCP lifecycle and transport | `lib/mcp/` |
| Browser bridge | `lib/browser/`, browser handler |
| Lazy web-search runtime and host providers | `lib/search_runtime.py`, `lib/search_bridge.py` |
| Tool event vocabulary | [`../EVENTS.md`](../EVENTS.md) and event registry |
| Generated inventory | [`../TOOL_INVENTORY.md`](../TOOL_INVENTORY.md) |

## Execution flow

1. The task policy resolves an explicit visibility allow-list.
2. The registry builds schemas for only those tools and rejects name collisions.
3. The model returns a call with a stable call ID and JSON arguments.
4. The gateway validates the name, visibility, arguments, and capability.
5. A focused handler executes with the authenticated request environment.
6. The gateway normalizes success/failure and timestamps.
7. Settlement emits the canonical tool result and adds exactly one matching model-context result.

Tool schemas describe capability; handlers implement it. A handler may depend on a
domain service, but schemas stay free of task runtime/HTTP and search activates on first valid use.

`ToolContractV2` is the single definition for execution parameters, validation,
model/search metadata, detailed help, permission, idempotency, typed errors, and
programmatic eligibility. Its projections feed provider schemas, discovery, and
execution validation; detailed help and parameters are paid only after discovery.

Registry assembly must produce one v2 document per executable name. Argument
ingestion may apply bounded, auditable syntax/shape repairs, then checks the
result against that request-owned document before dispatch. A present epoch
fails closed on a missing/malformed document. The same final check covers root,
nested `execute_tools`/ToolScript, swarm, and read-only SSE pre-execution calls;
a contract rejection remains `rejected`, never `done`.

Stable gateway schemas are added after registry/model-config assembly, which
also freezes their `ToolContractV2` documents. A request never advertises
`search_tools`/`execute_tools` without its execution document. Names use the
provider-safe ASCII set, preserving `mcp__server-name__tool-name` while rejecting
whitespace, path separators, dots, and Unicode lookalikes. Assembly/compilation
failure clears tool authority and continues text-only. Paper report/Q&A epochs
stamp `degradedReason`: side effects fail closed without aborting main generation.

`None` is an explicit read-compatible adapter state for standalone legacy
callers outside a production-owned tool epoch. It is not equivalent to an
empty map: an empty v2 map rejects every call. Production timer polls and
research-only paper runners validate against the exact post-policy schemas sent
to the model. Full report/QA/deepen runners freeze a two-layer epoch once per
run: bounded wire visibility plus a larger server-owned executable catalog.
Both layers and the gateway documents come from one registry snapshot; a search
hit cannot drift to a different schema or permission epoch. Contract compilation
failure removes the unattended tool surface, and rejection remains `rejected`,
never `done`.

`tools.schemaBudgetTokens` defaults to `0` (uncapped) for every model; names
never install hidden budgets. A positive value is a model-neutral local Tool
Search target, fitted once after programmatic calling, MCP, and multi-agent
shaping. The code floor (`read_files`, `grep_search`, `find_files`, `edit_file`,
`run_command`) and exact `tool_choice` retain full schemas even if the target is
exceeded. Other schemas fit deterministically; any omission keeps
`search_tools` + `execute_tools` live so hidden capabilities remain discoverable.
When local multi-agent routing is active, its control lifecycle is one required
unit: `spawn_agents`, `await_agents`, and `get_agent_result` all survive the budget.
A budget may not advertise launch while hiding settlement or retrieval.

The gateway pair targets 500 tokens; neither target may fail a request. For a
fixed tier, programmatic activation cannot change its bytes: eligible names stay
in the task-owned latch, serial-chain observations stay in telemetry, and
`execute_tools` plus local `spawn_agents` use fixed conditional guidance.
Policy remains task-owned. Compaction removes stable hints and annotations while
preserving JSON-Schema property names and validation. Final preflight isolates
malformed schemas as bounded `tool_schema_rejected` diagnostics; it removes dangling
`tools`/`tool_choice` if none survive. Budget omission is not a broken tool, and
execution always rechecks current visibility and authority after discovery.

Bounded `tool_wire_projection` evidence records ordered final names, tokens, and an
opaque fingerprint without full schemas; names or token counts do not prove byte stability.

`list_dir` is absent from new epochs; its bounded historical reader backs `run_command`
fast paths. Filesystem `grep` segments keep pipeline placement;
directory-only relative `ls` scans 10,000 entries, returns 1,000,
and formats at most 64 KiB. `find <dir> -type f` plus at most one quoted
`-name`/`-iname` uses `fd` then a depth/time/250,000-entry bounded walk and
returns at most 500 paths, including hidden/ignored files. Other operands,
executables, expansions, recursion, expressions, mutation, and pipelines keep
shell semantics. Direct real-`find` segments are planned with a 40-second,
host-compatible timeout before the ancestor/FUSE resource verdict, so that
guard judges the command that actually executes instead of falsely rejecting
its own bounded plan. Other recursive scanners still require a narrow target
or an explicit timeout.
Kill switches are `TOFU_RUN_LS_FASTPATH=0` and `TOFU_RUN_FIND_FASTPATH=0`. A plain Python script/`-m` workload on a network workspace keeps a real child process but may receive an owner/workspace/interpreter-scoped host-local `PYTHONPYCACHEPREFIX`; local/unknown source mounts, non-local cache roots, missing owners, `-c`/isolated/no-site modes, low disk, and existing bytecode policy stay unchanged.
The reconstructible cache is capped by `TOFU_RUN_PYTHON_CACHE_MAX_MIB` plus 100,000 filesystem entries, 64 namespaces, seven-day TTL and 256 MiB free-space reserve. Default `auto` seeds a repeat-heavy module only after it repeats and reuses warm namespaces, avoiding the measured cold-prefix cost for one-shot work; `TOFU_RUN_PYTHON_CACHE=1` forces the experiment and `=0` disables it.

## Visibility and authority

Tool availability is per request. Installed does not mean visible. Multi-user
deployments default to an explicit allow-list; a dedicated operator may expose
more tools through declared configuration. Plugins, MCP servers, request-level
custom tools, and built-ins all cross the same name/visibility gate.

Project and filesystem tools receive a resolved workspace/root capability.
They do not infer authority from the process working directory. Write tools use
the shared path validation, freshness, atomic write, and write-set attribution
boundaries. Read results never grant a subsequent write automatically.

Plan Mode is a stricter request policy layered over this boundary. Initial
wire exposure and the Tool Search catalog remove mutating or unproven schemas;
the final dispatch check independently requires the exact call to be proven
read-only. Unknown and caller-defined tools fail closed, MCP tools require an
explicit `readOnlyHint: true`, and mixed desktop tools expose and accept only
their read branches. This also applies to caller-supplied `tools=[...]`;
unknown schemas are removed and the framework-owned canonical `ask_human`
schema is present so within-turn clarification cannot be disabled accidentally.
The policy owner is `lib/tasks_pkg/plan_mode.py`.

## Side effects and settlement

A tool call is settled exactly once, including cancellation and failure. The
runtime distinguishes transport failure, invalid arguments, unavailable tool,
permission denial, handler failure, and user abort. Repair is limited to
syntax/shape problems and cannot silently change the requested capability.

Retries of side-effecting tools require an idempotency contract. The generic
LLM retry loop must not replay a completed write, webhook, browser action, or
human-guidance resolution.

`execute_tools` receipts key call ID together with canonical arguments, model
round, and world version because Kimi may recycle a positional call ID on later
messages. Exact same-round frames deduplicate, changed calls execute, and a
cached failure replays its failure verdict rather than becoming `done`. The
task-local receipt table evicts oldest entries beyond 256.

The gateway remains protocol-only in durable activity. Its terminal envelope
is decoded from the canonical `toolContent` event field: pre-dispatch validation
failures project as warning-level skipped rows for the attempted child, while an
executed child's own lifecycle row owns its typed V2 code, message, and next
action. The wrapper is removed rather than counted or rendered twice. Legacy
`tool_result` now also carries `toolName`, so live folding can identify protocol
rows before the named `tool_complete` frame arrives.

Large/binary outputs become artifacts or bounded references. They are not
dumped wholesale into the next model request or logs.

`edit_file` inserts always resolve one unique anchor. A supplied `replace_all`
flag is ignored for a unique-anchor insert rather than rejecting it; it never
enables multi-site insertion. Only `replace` gives `replace_all=true` batch semantics.

`tools.resultEnvelope` now ships as `v2`. `ToolResultEnvelopeV2` exposes stable
status, a short summary, at most 64 structured items, cursor, truncation, byte
counts, freshness/evidence ID, and a typed retry hint. The model
sees at most 8,000 tokens for one result and 24,000 tokens for all results in a
round. Overflow is stored through the owner-scoped content-addressed artifact
repository and resumed with range, search, or cursor operations; `read_files` has no exemption. Batched producers supply bounded per-file projection items, and settlement preserves every requested path/status before sharing preview space fairly or removing previews under aggregate pressure.
The request-local sidecar crosses streaming/dedup caches only as bounded metadata and is discarded after envelope materialization; complete legacy text remains only in the owner-scoped artifact. Reconstructible overflow expires after 24 hours by default
(seven-day hard TTL ceiling) and is reclaimed in bounded background batches.
If artifact storage fails, the envelope returns a short honest preview plus a
narrower-rerun hint and no artifact reference or cursor; an inaccessible
reference is never presented as recoverable evidence. Legacy text/file-staging
semantics remain only as an explicit rollback or experiment control. Paper
adapters default to V2; an explicit legacy arm is fingerprint-isolated from
other live tasks and cannot read or write the canonical paper-result cache.

## MCP boundary

MCP lifecycle, discovery, health, and transport live under `lib/mcp/`. The
registry sees normalized tool specifications and the gateway sees normalized
calls; neither depends on an MCP SDK payload. Progressive discovery may defer
schemas, but execution still checks current visibility and server health.

Remote content is untrusted tool output. Resource links, text, images, and
audio preserve their declared types and size bounds.

## Invariants

- One registry for names and schemas; one gateway for execution verdicts.
- Visibility is request-scoped and default-deny.
- Tool call IDs and result pairing survive streaming and continuation.
- Every call settles once on success, failure, timeout, cancellation, or abort.
- Writes use explicit root authority, freshness checks, and atomic operations.
- Handler exceptions become typed tool failures, not successful text.
- Tool results are bounded before model context and logging.
- Artifact references are owner-scoped, expiring, content-addressed, and never
  reveal a database or filesystem path.
- MCP/plugin/custom-tool adapters cannot bypass visibility or egress policy.
- Generated inventory derives from the live registry.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Built-in schema | focused `lib/tools/<domain>.py` | registry and inventory generation |
| Tool contract/result budget | `contracts.py`, `result_envelope.py`, compaction budget | schema compiler, typed error, 8k/24k, cursor tests |
| Handler behavior | focused `lib/tasks_pkg/handlers/` module | gateway settlement and events |
| Tool visibility | `tool_env.py`, registry plugin owner | owner/request isolation |
| Project write/index | `lib/project_mod/` and write gates | root attribution, freshness, rollback, shared resource ceilings |
| MCP server/transport | `lib/mcp/` | lifecycle, health, normalized result |
| Browser action | browser bridge + handler | user scope, queue TTL, auth |

## Test map

```bash
pytest -q tests/test_tool_registry.py tests/test_tool_registry_builtin_name_protection.py
pytest -q tests/test_unified_tool_gateway.py tests/test_tool_settle_all_lanes.py tests/test_tool_call_wire_shape.py
pytest -q tests/test_core_tool_isolation.py tests/test_custom_tool_isolation.py
pytest -q tests/test_write_tools_atomic.py tests/test_write_tools_root_attribution.py
pytest -q tests/test_mcp_v2_protocol.py tests/test_mcp_liveness_probe.py
pytest -q tests/test_long_agent_v2_contracts.py -k 'tool_contract or tool_result or tool_artifact or tool_search'
```

# Tool-dispatch isolation design

## Current boundary

`lib/tasks_pkg/executor/_resolver.py` owns handler resolution in this order:
request-local `ToolEnvironment`, frozen process built-in registry, then MCP
fallback scoped to the task’s explicit owner. Unknown `mcp__` names resolve
through that owner’s bridge; MCP does not monkey-patch global registry lookup.
Built-in specs live in `lib/tools/registry/_builtin_specs.py`. Registry mutation
is allowed only inside audited `registration_window`s and freezes when app
assembly completes.

MCP server configuration is namespaced by owner with one-time legacy migration
in `lib/mcp/config.py`. `lib/mcp/client/_state.py` retains owner bridge generations
without a count quota or unrelated-owner eviction. A retired generation still
performing actual cleanup fences replacement for the same owner. Other owners
continue independently. Process shutdown retires and disconnects every bridge;
only physically stopped generations leave the registry.

Custom tool schemas and handlers belong to the request environment until its
terminal owner disposes it. Default tool counts, environment counts and client
handoff counts have no admission quota. Positive caller-specified definition
limits remain deliberate request policy; schema/result size, approval,
visibility, identity matching and real deadlines remain enforced. See
[CUSTOM_TOOLS.md](CUSTOM_TOOLS.md) and
[RESOURCE_CONTROL.md](RESOURCE_CONTROL.md#mcp-and-custom-tool-request-ownership).

## Problem and decision

Problem: a process-global mutable handler table could make schema ownership
and handler ownership disagree. A finite owner bridge registry then evicted idle
owner state or refused unrelated work while transports were live.

Decision: keep one resolver seam, immutable built-ins, request-local extensions
and explicit owner-scoped MCP state. Count-based admission does not govern
ownership; actual lifecycle completion governs release.

Alternatives considered: global lookup patches lose request/owner context;
increasing a bridge quota still refuses demand; evicting live bridges loses
transport cleanup ownership; replacing a stopping same-owner generation permits
two generations to race. Preserving exact ownership and cleanup avoids each.

## Verification

- `tests/tasks_pkg/test_handler_resolver.py`: resolution order and owner fallback.
- `tests/tasks_pkg/test_registry_freeze.py`: audited registration and frozen fallback.
- `tests/tools/test_tool_builtin_specs_structure.py`: canonical built-in specs.
- `tests/mcp_protocol/test_mcp_owner_isolation.py`: isolation, on-demand owners,
  generation startup rollback and shutdown fencing.
- `tests/mcp_protocol/test_mcp_loop_shutdown.py`: actual drain and loop teardown.
- `tests/tools/test_custom_tool_isolation.py`: request isolation, exact callback
  matching, burst admission and terminal disposal.

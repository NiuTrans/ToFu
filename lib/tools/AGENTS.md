# Tool contract and registry guidance

## Scope

This package owns built-in tool schemas, registry/discovery, execution contracts,
result envelopes, visibility, and tool-search metadata. Task handlers live under
`lib/tasks_pkg/handlers/`; domain implementations keep their own authority.
Read `docs/modules/tools_execution.md`.

## Editing rules

- Define each tool once with a stable name, explicit JSON schema, capability,
  side-effect class, approval requirement, timeout, and result budget.
- Built-in names are protected. Plugin and request-visible registries are
  isolated by owner/request and cannot mutate global catalogs implicitly.
- Tool execution returns the canonical success/error envelope and settles every
  lane exactly once. Preserve cancellation, timeout, partial-output, artifact,
  and retry semantics.
- Enforce path, network, subprocess, browser, and write authority at the owning
  boundary. Model intent or schema validity is not authorization.
- Bound inline output, artifacts, pagination/cursors, search candidates,
  subprocess streams, and retained diagnostics; redact secrets before events or
  persistence.
- `docs/contracts/TOOL_INVENTORY.md` is generated. Change the schema/registry owner and run
  `scripts/gen_tool_inventory.py`.

## Minimal path for a new built-in tool

Registration is decorator-driven and collected by import side effect — there
is no central list to edit, and missing one of the steps below leaves the
tool half-visible (callable but undocumented, or documented but unrouted):

1. Write the handler in the owning `lib/tasks_pkg/handlers/*.py` module and
   decorate it: `@tool_registry.handler('name', category=..., description=...)`
   (or `.special(...)` for synthetic/internal tools like `__code_exec__`).
2. Declare the JSON schema/spec in the registry's spec owner
   (`lib/tools/registry/_builtin_specs.py`) with capability, side-effect
   class, approval requirement, timeout, and result budget.
3. Regenerate the inventory: `python3 scripts/gen_tool_inventory.py`
   (drift-gated by `make contracts-check`).
4. Verify: the focused handler test, then
   `pytest -q tests/tools/test_tool_registry_write_partition_ssot.py
   tests/tools/test_tool_inventory.py`.

## Verification

Run registry and inventory checks, then focused schema/handler tests and the
unified gateway settlement/isolation tests. Add write, MCP, browser, or
long-agent budget tests for the affected capability.

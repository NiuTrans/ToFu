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
- `docs/TOOL_INVENTORY.md` is generated. Change the schema/registry owner and run
  `scripts/gen_tool_inventory.py`.

## Verification

Run registry and inventory checks, then focused schema/handler tests and the
unified gateway settlement/isolation tests. Add write, MCP, browser, or
long-agent budget tests for the affected capability.

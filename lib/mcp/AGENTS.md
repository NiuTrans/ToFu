# MCP guidance

## Scope

This package owns MCP server configuration, client transports, lifecycle,
capability discovery, and normalized tool/resource results. Tool visibility and
execution policy remain in `lib/tools/` and the task gateway.

## Editing rules

- Treat server configuration, environment, transport frames, tool schemas, and
  returned content as untrusted. Validate sizes, types, paths, and protocol
  versions before projection.
- Preserve explicit owner/request visibility and approval. Connecting a server
  does not grant its tools universal or unattended authority.
- Give each connection a bounded start, health, reconnect/backoff, request,
  cancellation, and shutdown lifecycle. Dispose child processes and streams on
  every terminal path.
- Normalize MCP errors/content once; do not expose transport exceptions or raw
  secrets through the unified tool result.
- Bound servers, tools/resources, discovery pages, concurrent requests, output,
  retries, logs, and child-process I/O.
- Keep stdio/network transports interchangeable behind the same behavioral
  contract and injectable in tests.

## Verification

Run focused MCP protocol, lifecycle, liveness, cancellation, normalization,
visibility, and tool-registry tests. Use live external servers only as explicit
opt-in smoke tests.

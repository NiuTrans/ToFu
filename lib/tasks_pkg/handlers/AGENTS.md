# Task tool-handler guidance

## Scope

Handlers adapt unified tool calls to domain services. Tool schemas, visibility,
approval, and result envelopes live in `lib/tools/`; domain policy remains with
the invoked package.

## Editing rules

- Parse the registered schema once, recover explicit owner/task/project context,
  and call one public domain entry point. Do not duplicate validation or policy.
- Enforce approval/capability receipts before side effects and retain exact root,
  browser device, remote device, or network attribution.
- Settle the canonical result exactly once on success, typed failure, timeout,
  cancellation, or partial artifact output.
- Propagate cancellation to subprocess, network, browser, MCP, child-task, and
  storage work. Clean only resources owned by the call.
- Bound input expansion, concurrency, output, artifacts, progress events,
  retries, and diagnostics; redact secrets before results/events.
- Keep display formatting outside execution semantics and never write directly
  to route/frontend state.

## Verification

Run the focused handler/domain tests, then unified gateway, approval, isolation,
settle-all-lanes, wire-shape, and cancellation tests.

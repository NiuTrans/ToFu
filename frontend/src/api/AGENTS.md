# Frontend API client guidance

## Scope

This directory is the typed browser transport boundary. Public wire authority
lives under `contracts/` and in `docs/API_CONTRACT.md`.

## Editing rules

- Files named `generated` are generator output. Change the canonical contract
  and generator, then regenerate; never patch generated TypeScript directly.
- Handwritten transport code owns HTTP/SSE mechanics only: base paths,
  credentials, cancellation, timeouts, content types, and canonical error
  parsing. Domain policy stays in services/features.
- Carry owner/auth context through supported transport mechanisms; never infer
  authority from visible UI state or cached records.
- Preserve abort propagation and terminal stream settlement. Bound response
  buffering and do not expose tokens, headers, or raw sensitive bodies in
  browser diagnostics.

## Verification

Run the matching generator check, `npm run typecheck:modules`, and focused API
contract/transport frontend tests. Add `make test-api` when the server/client
wire boundary changes.

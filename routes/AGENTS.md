# HTTP route guidance

## Scope and first reads

Routes are stateless protocol adapters. Read `docs/API_CONTRACT.md`, the owning
domain map, and the relevant machine-readable contract before editing.

## Editing rules

- Authentication and authorization are decided at the shared middleware
  boundary with default deny. Pass structured principal/owner identity into
  application services explicitly.
- Decode and validate HTTP inputs, call one application command/query, and map
  its typed result. Routes do not own SQL, filesystem persistence, durable
  state, provider policy, retry state machines, or domain transactions.
- Use the canonical response and error-envelope helpers. Do not catch arbitrary
  exceptions and reinterpret them as success or a new error taxonomy.
- Preserve request IDs, cancellation/disconnect behavior, body and upload
  bounds, streaming settlement, idempotency keys, and content types.
- `api_v1/` is a delivery namespace, not permission to duplicate native domain
  behavior. Compatibility routes delegate to named adapters and retain native
  parity.
- Public field/path changes begin in the owning schema and regenerate clients.

## Verification

Run the smallest route/domain tests, then neighboring auth, error-envelope,
storage, and contract-parity tests. Use `make test-api` after focused checks
when public HTTP behavior changes.

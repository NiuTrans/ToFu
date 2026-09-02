# SDK guidance

## Scope

`clients/python/` and `clients/typescript/` are public consumers of the native
API contract. They must remain usable without importing the server application.

## Editing rules

- `contracts/api_v4.yaml` is the wire authority. Files named `generated` are
  regenerated with `scripts/gen_api_v4_contract.py`, never hand-edited.
- Keep both SDKs aligned on paths, authentication, request/response types,
  pagination, streaming, timeouts, cancellation, and typed error envelopes.
- Handwritten clients may add ergonomic transport behavior but may not widen or
  reinterpret the contract silently.
- Preserve package boundaries and supported runtime versions. Do not depend on
  repository-relative data, the application database, or server internals.
- Examples and fixtures use fake credentials and deterministic local endpoints.

## Verification

Run `npm run check:api-v4`, the API-v4 contract tests, the TypeScript package's
`npm test`, and the focused Python SDK tests. Build/package each changed SDK
before changing its published version metadata.

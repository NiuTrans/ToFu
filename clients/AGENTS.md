# SDK guidance

## Scope

`clients/python/` and `clients/typescript/` are public consumers of the native
API contract. They must remain usable without importing the server application.

## Owners

- Python: `tofu_sdk/__init__.py` is the export facade; `_client.py` and
  `async_client.py` assemble the public clients. `_protocol.py` owns wire
  projection and validation; `_sync_transport.py` / `_async_transport.py` own
  requests and lifetimes. `_sse.py` frames bytes, `_sse_events.py` assembles
  events, and `_streaming.py` releases responses. Resource methods live in
  `_tasks.py`, `_agents.py`, `_accounts.py`, and `_async_resources.py`.
- TypeScript: `src/index.ts` exports the facade. `client.ts` assembles clients;
  `protocol.ts`, `transport.ts`, `streaming.ts` / `sse-framer.ts`, and
  `tasks.ts` / `agents.ts` / `accounts.ts` own the corresponding boundaries.
  `types.ts` contains public types. Resource-to-client imports are type-only.
- Runtime versions live in Python `_version.py` and TypeScript `version.ts`;
  keep them aligned with each package's release metadata.

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

`tests/fixtures/sdk/stream_frames.json` is the shared cross-language stream
specification. Python tests use the repository's declared source finder;
TypeScript tests consume built `dist`. Verify installed wheel/npm tarballs
outside the repository so source imports cannot conceal missing package files.

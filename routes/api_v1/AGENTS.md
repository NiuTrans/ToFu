# API v1 route guidance

## Scope

This directory exposes versioned native application capabilities. It inherits
the stateless route boundary from `routes/AGENTS.md`; domain contracts and
application services remain authoritative.

## Editing rules

- Keep each route module aligned with one domain owner and discoverable from the
  versioned registration surface.
- Parse path/query/body/upload data through shared schemas and bounds, resolve
  the authenticated principal, and delegate to one command/query service.
- Preserve stable status codes, canonical envelopes, pagination/cursors,
  idempotency, request IDs, stream events, and content types.
- A versioned route may translate a public shape but cannot copy storage,
  scheduling, provider, orchestration, task, or tool policy.
- New or changed public fields start from the appropriate machine-readable
  contract/OpenAPI owner and regenerate SDK/frontend consumers.
- Capability absence and permission denial remain distinguishable without
  leaking another owner's resource existence.

## Verification

Run the focused `tests/test_api_v1_*` module plus its domain service/Sidecar
tests. Add API contract drift, auth parity, error-envelope, OpenAPI/SDK, and
frontend tests when the public surface changes.

# Test-suite guidance

## Scope and first read

Tests are executable product and architecture specifications. Read `README.md`
and `docs/TESTING_STRATEGY.md`; use the domain map to find the smallest owning
suite.

## Test design

- Assert public behavior or a durable boundary: identity/authorization, typed
  failure, idempotency, rollback, cancellation, cleanup, and resource bounds.
- Do not pin private function locations, exact comments, incidental ordering,
  magic source counts, generated bundles, or compatibility paths already
  removed.
- Unit tests have no network or real model. API tests use controlled storage and
  provider seams. Browser tests cover only outcomes that cheaper layers cannot.
- Tests must be hermetic and parallel-safe under xdist work stealing. Each test
  owns its database, directories, ports, environment, clock/random/provider
  seams, processes, and cleanup.
- Never mutate shipped source or rely on a fixed host-global resource as a lock.
  Fixtures are minimal, synthetic, non-secret, and live under `fixtures/` or
  `support/` only when shared.
- Use `live_llm`, `slow`, `visual`, and other markers honestly. Expensive or
  external tests stay opt-in and cannot be a hidden release prerequisite.

## Verification ladder

Run the changed test directly, then neighboring contract tests, then the domain
gate. Use `make suite-health` for collection/quality changes and broad
`make test-unit`, `make test-api`, `make test-frontend`, or `make test-all` only
after the worktree is stable.

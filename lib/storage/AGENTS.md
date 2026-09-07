# Storage client guidance

## Scope

This package is the application-facing `storage.v1` client and repository
boundary. The dedicated process under `lib/storage_sidecar/` owns databases,
SQL, schema, and transactions. Read `docs/STORAGE.md` and
`docs/modules/data_tier.md`.

## Editing rules

- Expose semantic, backend-neutral operations with explicit owner identity,
  deadlines, idempotency, and typed errors. Do not expose SQL or connection
  objects to application code.
- Preserve framing, authentication, capability negotiation, request/receipt
  correlation, cancellation, supervision, readiness, and write fencing.
- A selected backend failing is an error; the client never silently switches
  authority or opens `data/tofu.db` itself.
- Keep transport queues, in-flight requests, retries, buffers, and child-process
  lifecycles bounded and observable.
- Manifest/catalog changes are declarative and validated against Sidecar
  registrations; do not hardcode operation-specific transaction behavior here.

## Verification

Run `tests/test_storage_process_boundary.py` and focused client/supervision tests,
then the real SQLite Sidecar contract cases that exercise the changed semantic
operation.

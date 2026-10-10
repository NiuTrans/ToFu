# Storage client guidance

## Scope

This package owns the backend-neutral application client and repository
boundary. The configured backend selects native `storage.v2` in
`packages/tofu-db/` or SQL `storage.v1` in `lib/storage_sidecar/`; that dedicated
process owns durable state and transactions. Read `docs/STORAGE.md` and
`docs/modules/data_tier.md`. Backend selection never implies migration.

## Editing rules

- Expose semantic, backend-neutral operations with explicit owner identity,
  deadlines, idempotency, and typed errors. Do not expose SQL or connection
  objects to application code.
- Preserve framing, authentication, capability negotiation, request/receipt
  correlation, cancellation, supervision, readiness, and write fencing.
- A selected backend failing is an error; the client never silently switches
  authority or opens `data/tofu.db` itself.
- `protocol.py` owns finite-number admission and mapping materialization shared
  by framing and native request adaptation. Exact built-ins may use direct
  dispatch; subclasses/views retain generic checks. Verify
  `tests/storage/test_storage_protocol_json.py`.
- Keep transport queues, in-flight requests, retries, buffers, and child-process
  lifecycles bounded and observable.
- Manifest/catalog changes are declarative and validated against the selected
  backend semantic registrations; do not hardcode operation-specific transaction behavior here.

## Verification

Run `tests/storage_sidecar/test_storage_process_boundary.py` and focused client/supervision tests,
then `tests/storage/test_storage_application_contract.py` against both native and SQLite
processes for changed semantic operations. Native wire changes also exercise
`tests/tofudb/test_tofudb_differential.py` with the explicitly selected test binary.

# Storage sidecar guidance

## Scope and authority

This package is the sole owner of application database connections, schema,
SQL, transactions, backend adapters, maintenance, and semantic operation
registration. Read `docs/STORAGE.md` and `docs/modules/data_tier.md`.

## Editing rules

- Add behavior through one semantic operation with declared input/output,
  ownership filter, transaction mode, idempotency, retry class, and receipt.
  Routes and application services never contain its SQL.
- SQLite and PostgreSQL implement the same semantics. Keep dialect details in
  adapters and verify owner filtering occurs before limits/projections.
- `schema.py` is the schema-evolution authority. Migrations are forward,
  serialized, restart-safe, observable, and never an implicit startup rewrite
  outside the declared lifecycle.
- Preserve atomic domain state plus event/outbox writes, conflict fencing,
  rollback, lost-ack recovery, backup/restore integrity, and fail-closed backend
  selection.
- Pools, lanes, busy retries, result sizes, WAL/outbox/projection growth,
  maintenance work, and shutdown are explicitly bounded.
- Offline maintenance is a separate, explicitly invoked boundary. It never races
  a live writer or silently deletes durable user data.

## Verification

Run the smallest operation-domain test, then
`tests/test_storage_sidecar_contract.py` and
`tests/test_storage_process_boundary.py` cases for the changed boundary.
Backend-sensitive changes require the identical PostgreSQL contract lane and
the relevant migration/backup/fault test.

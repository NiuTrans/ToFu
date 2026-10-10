# Storage backend adapter guidance

## Scope

Adapters implement SQLite and PostgreSQL mechanics for the Sidecar semantic
operation catalog. They do not define application-level behavior.

## Editing rules

- Preserve semantic parity across backends: transaction isolation, owner
  filtering, ordering, conflicts, affected-row meaning, receipts, and errors.
- Keep dialect SQL, connection setup, TLS, durability validation, pool behavior,
  retry classification, and backend capability checks inside the adapter.
- SQLite retains one ordered physical writer, caller deadlines and demand-created
  query-only connections. PostgreSQL retains distinct read/write owners with
  direct Psycopg connections. Pool counts size idle reuse, never active admission.
- Classify retryable failures narrowly from documented backend codes. Never
  retry an unknown or non-idempotent outcome as though it were uncommitted.
- `connection_pool.py` owns connection leases, preparation, idle reuse and
  joined close once. `sqlite_read_pool.py` supplies the SQLite reset adapter;
  PostgreSQL supplies its direct factory/reset and SQLSTATE classifier.
  Transactions and dialect code stay with their owners. Verify both backends
  and local search projection before integrating shared lifecycle changes.
- Cancellation, deadlines, progress watchdogs, connection invalidation, and
  shutdown release resources on every path.
- Backend unavailability fails closed; no adapter silently switches authority or
  weakens durability.

## Verification

Run focused adapter tests and the identical semantic Sidecar contract against
SQLite and provisioned PostgreSQL. Add fault, pool-budget, durability, timeout,
and cancellation cases for changed mechanics.

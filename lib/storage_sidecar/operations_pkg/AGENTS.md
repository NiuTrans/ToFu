# Storage operation implementation guidance

## Scope

This directory implements semantic operations declared by the Sidecar catalog.
SQL and transaction code stays here or in backend adapters and is never imported
by application/route code.

## Editing rules

- Implement the declared request/result and transaction mode exactly. Keep
  owner predicates in every read, write, join, subquery, and conflict path.
- Apply authorization/domain filters before ordering, limits, aggregation, or
  returning not-found; do not leak cross-owner existence through counts/errors.
- Use backend-neutral helpers or paired dialect implementations while preserving
  SQLite/PostgreSQL semantics.
- Mutations write domain state and required events/outbox rows atomically.
  Idempotent replay and lost acknowledgement return the original semantic result.
- Avoid unbounded scans, variable lists, result payloads, and write batches.
  Declare indexes/schema changes through the schema owner.
- Preserve fault-injection points and ensure every exception rolls back before
  typed classification.

## Verification

Run the exact Sidecar contract tests for the operation, including owner
isolation, pre-limit filtering, idempotency, conflict, rollback, receipt, and
backend parity. Add migration/index tests for schema changes.

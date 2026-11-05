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

`_papers.py` keeps report metadata validation, owner locks and atomic writes;
`lib.paper.report_metadata` owns the dependency-free usage and recorded-cost
projection shared by merge/accumulate and application recovery. Do not import
pricing services or worker lifecycle into a storage operation. Verify atomic
second-pass, owner-isolation, replay and metadata projection contracts.

`_runs.py` serializes event append, Goal MAX(seq) allocation, status transition
and deletion on the owner/tenant/run row. Lock before reading authority or
deleting events; otherwise concurrent PostgreSQL writers can overwrite terminal
state or leave orphan events. Verify `test_goal_transaction_fences.py` on both
backends and the neighboring orchestration/Goal contracts.

## Verification

Run the exact Sidecar contract tests for the operation, including owner
isolation, pre-limit filtering, idempotency, conflict, rollback, receipt, and
backend parity. Add migration/index tests for schema changes.

## Billing operation owners

`_billing_wallets.py` owns wallet/ledger mutations and reservation fences;
`_billing_redeem.py` and `_billing_payments.py` reuse its atomic wallet application.
`_billing_values.py` supplies scalar validation and raw-document decoding without
IO. `operation_domains/billing.py` only registers these implementations. Native
checkpoint settlement imports the wallet owner directly. Verify billing/payment
contracts on SQLite, PostgreSQL and both native transports, including rollback
and lost-acknowledgement replay; keep fault points in their transaction owners.

## Turn operation module layout

`_turns.py` is a pure re-export facade; implementations live in semantic
slices: `_turns_core.py` (identity/publication/search-row plumbing),
`_turns_read.py` (get/list/delta/sync projections), `_turns_write.py`
(create/update/compact), `_turns_lifecycle.py` (delete/recover/archive),
`_turns_events.py` (attempt-event append/retention), `_turns_branch.py`
(branch create/delete). New code goes to its semantic slice, never the
facade; slices must keep the acyclic import direction
core → events → read → lifecycle → write → branch.

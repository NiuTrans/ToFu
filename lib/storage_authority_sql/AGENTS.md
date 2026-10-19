# SQL storage authority guidance (formerly "storage sidecar")

## Scope and authority

This package (`lib/storage_authority_sql/`) is the SQL **authority** — the
implementation that owns durable state when the SQL `storage.v1` backend is
selected. It is NOT a backend competing with `lib/storage/`; `lib/storage/` is
the client facade that delegates to whichever authority is selected (this SQL
authority, or native TofuDB). It is the sole owner of application database
connections, schema,
SQL, transactions, backend adapters, maintenance, and semantic operation
registration. Read `docs/STORAGE.md` and `docs/modules/data_tier.md`; native migration is
defined in `docs/STORAGE_TOFUDB.md`, operational bounds in `docs/STORAGE_OPERATIONS.md`.

## Editing rules

- Add behavior through one semantic operation with declared input/output,
  ownership filter, transaction mode, idempotency, retry class, and receipt.
  Routes and application services never contain its SQL.
- SQLite and PostgreSQL implement the same semantics. Keep dialect details in
  adapters and verify owner filtering occurs before limits/projections.
  Native billing admission and standalone reserve release share the wallet lock;
  ledger append cannot bypass it. Ledger kinds come from the billing table in
  `contracts/tofudb_ir_v1.json` via `gen_tofudb_ir_contract.py`.
- `schema.py` is the schema-evolution authority. Migrations are forward,
  serialized, restart-safe, observable, and never an implicit startup rewrite
  outside the declared lifecycle.
- Preserve atomic domain state plus event/outbox writes, conflict fencing,
  rollback, lost-ack recovery, backup/restore integrity, and fail-closed backend
  selection.
- `native_checkpoint.py` is the private, explicit SQL adapter for the shared
  Rust journal. `native_checkpoint_rows.py` owns atomic private bytes only.
  `native_checkpoint_handoff.py` locks SQL observations and invokes shared Rust
  validation within one commit; it owns no lease/lineage/billing policy.
  `native_checkpoint_goal.py` enters one joined transaction; its reads adapter
  supplies bounded locked rows to shared Rust claim/consumption policy. Inner
  callbacks use only that Session, never nested Backend jobs; inner refusals
  must raise through rollback before returning an outer rejected response.
  `native_checkpoint_outbox.py` captures ordered IO in that same transaction;
  replay verifies source prefixes and retains passive envelopes/fences. Its new
  codec refuses old readers; never bypass this with raw mutation replay.
  `native_checkpoint_config.py` admits one explicit pinned scope and budgets;
  `native_checkpoint_service.py` lazily owns the controller for one process UUID.
  Server close joins image admission and entered control before backend/outbox
  teardown. Disabled/preview ports load no code; failures and stale bindings do
  not refresh or replay. Verify RPC, full public SQL recovery and joined close;
  availability is a capability, never performance/default promotion.
- Pools, lanes, busy retries, result sizes, WAL/outbox/projection growth,
  maintenance work, and shutdown are explicitly bounded.
- Offline maintenance is a separate, explicitly invoked boundary. It never races
  a live writer or silently deletes durable user data.
- The Tofu-DB SQLite→native offline migration toolkit lives in the
  `migration/` subpackage (`lib/storage_authority_sql/migration/`). It is an
  operator-only, explicitly-invoked boundary — never a startup, wire, or live
  serving path. See `lib/storage_authority_sql/migration/AGENTS.md` for the
  per-module rules. Its generated limits live in
  `migration/generated_tofudb_migration.py`; edit `contracts/tofudb_ir_v1.json`
  and run `scripts/gen_tofudb_ir_contract.py`, never edit the generated module.


## Verification

Run the smallest operation-domain test, then
`tests/storage/test_storage_authority_sql_contract.py` and
`tests/storage_authority_sql/test_storage_process_boundary.py` cases for the changed boundary.
Backend-sensitive changes require the identical PostgreSQL contract lane and
the relevant migration/backup/fault test.

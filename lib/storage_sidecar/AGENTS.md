# Storage sidecar guidance

## Scope and authority

This package is the sole owner of application database connections, schema,
SQL, transactions, backend adapters, maintenance, and semantic operation
registration. Read `docs/STORAGE.md` and `docs/modules/data_tier.md`; native migration is
defined in `docs/STORAGE_TOFUDB.md`, operational bounds in `docs/STORAGE_OPERATIONS.md`.

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
- Tofu-DB migration source/checkpoint/target/import helpers are offline-only.
  The target helper owns the exclusive native child and bounded pipe deadlines. Their generated
  limits live in `generated_tofudb_migration.py`; edit `contracts/tofudb_ir_v1.json`
  and run `scripts/gen_tofudb_ir_contract.py`, never edit the generated module.
- `tofudb_migration_source.blob_size/blob_chunks` own constant-memory metadata
  and bounded incremental TEXT/BLOB reads by exact primary key. Callers supply
  domain byte ceilings and close abandoned iterators; ordinary row limits stay.
- `tofudb_migration_source.digest_key_rows/digest_key_point` read exact 32-byte
  BLOB primary keys, including WITHOUT ROWID receipt tables. SQL seeks raw
  bytes; lowercase hex is only the external cursor identity, never a sort.
- `tofudb_migration_receipt_source.py` audits both global receipt formats
  without assigning owners. Preserve the live duplicate-format refusal even
  for identical payloads; complete source hashes are not native replay proof.
- `tofudb_migration_coverage.py` reports every source table against the plan;
  the operator CLI refuses unmapped nonempty tables before opening a target
  and repeats admission inside the import snapshot. No name-based exclusions.
- `tofudb_migration_mirrors.py` proves declared retired trading rows equal to the
  mapped current plugin authority. Complete column/row equality is mandatory;
  neither a table prefix nor an old migration receipt establishes coverage.
- `tofudb_migration_search.py` admits only the exact engine-owned retired FTS5
  layout after mapping its complete content/config archives. Same-name ordinary
  tables and different virtual definitions are not rebuildable-state evidence.
- `tofudb_migration_retired.py` archives its explicit historical table inventory
  as typed, inactive records. Keep source identity/bytes and checkpoint/catalog
  verification intact; never replay retired rows as current task state.
- `tofudb_migration_metadata.py` declares source-engine metadata admission:
  retain known values as provenance, preserve native identity/schema/counters,
  and reject unknown keys or incomplete source cutover markers before writes.
- `tofudb_migration_retired_large.py` owns large retired row manifests and raw
  parts. Source `cell_metadata/blob_slice` avoid materializing large cells;
  publish verified parts before manifests and never repair a verified prefix.
- `tofudb_migration_turn_payload.py` owns bounded source Turn body reads and
  private upload/readback. Source hashes bind exact chunks; transfer buffers
  have explicit size/hash/offset receipts and an abort/EOF lifecycle.
- `tofudb_migration_projection.py` admits only materialized source Turns before
  binding; `_turn_document` owns private projection-codec decoding for both
  active and trashed Turns. Live patch chains require source settlement/recovery.
- `tofudb_migration_tombstones.py` preserves source deletion markers via native
  claim/delta/retention publication and independent owner catalogs.
- `tofudb_migration_trash.py` completes owner-scoped native staging before active
  domain verification; source prefix reads and pinned catalogs verify every Turn.
- `tofudb_migration_transcript.py` streams frozen message arrays through the
  application archive/projection decoder and packs bounded native pages. Plan v20
  hashes original source bytes and verifies native manifests, physical page
  catalogs and recomputed page hashes; source decoding alone is not certification.


## Verification

Run the smallest operation-domain test, then
`tests/storage/test_storage_sidecar_contract.py` and
`tests/storage_sidecar/test_storage_process_boundary.py` cases for the changed boundary.
Backend-sensitive changes require the identical PostgreSQL contract lane and
the relevant migration/backup/fault test.

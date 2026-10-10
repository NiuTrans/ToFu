# Conversation header subtree ownership

- `recency.rs` owns the per-owner recency directory (derived sidebar index;
  `docs/TOFUDB_RECENCY_DIRECTORY.md`): head-128 newest entries, same-transaction
  maintenance through the `store_header`/create/delete/restore funnels, bounded
  lazy backfill, and every coverage gate falling back to the legacy
  read-all+sort path. Prepared writes retain logical index mutations; index reads
  flush them against the snapshot, commit applies them after business validation.
  Creation count/activity mutations share this deferred queue; counts and
  activity-index reads flush it on the original snapshot. All creation metadata
  applies before recency backfill, including multi-create transactions.
  Verify with `cargo test --lib recency`.
- `migration.rs` imports source logical header state. It also exposes the same
  transaction-local insert-or-match operation for trash staging.
- `trash_migration.rs` owns one pending capsule per owner, its durable seal,
  pinned-source inspection and the daemon readiness fence. Range pins capture
  committed roots; never combine pending header writes and pin extraction and
  assume those writes are in the capsule.
- Shared header/index helpers remain in `../conversation_header.rs`. Turn and
  frozen-page modules own their codecs, physical catalogs and range definitions.
- From `packages/tofu-db/`, start verification with `cargo test --lib
  trash_migration`; neighboring tests are header migration, Turn migration and
  frozen transcripts. Source/resume/process tests live in
  `tests/tofudb/test_tofudb_migration_import.py` at the repository root.

# Module Design Doc — Data Tier

The authoritative behavioural and operational contract is
[`docs/STORAGE.md`](../STORAGE.md). This module note describes
code ownership only and must not redefine backend authority.

Storage optimization starts at the
[SQLite reference and change-review map](../STORAGE_PERFORMANCE_CHARTER.md#sqlite-reference-and-change-review),
which links deployment, lifecycle, query, cache, backup and failure-validation
owners to the existing performance gates.

## Process ownership

| Package | Runs in | Responsibility |
|---|---|---|
| `lib/storage/` | Web, worker, task, plugin parent | Selected `storage.v1` / native `storage.v2` transport, process-wide response-frame admission, semantic client, structured errors, supervision, readiness/write fence, declarative manifest validation |
| `lib/storage_authority_sql/` | Dedicated child process | Personal database paths, project lease, FUSE preflight, drivers, pools, transactions, receipts, semantic operation catalog, SQLite backup/restore/handoff |
| `lib/storage_authority_sql/impact_cache.py` | Landing verifier | Private reconstructible test records and blob fingerprints in an explicit repository cache directory; never an application authority or backend fallback |
| `lib/storage_authority_sql/native_checkpoint_{config,service}.py`, `lib/storage/checkpoint_control.py` | Selected SQL Sidecar / application client | Explicit pinned scope, lazy joined code lifetime and one-attempt instance-bound RPC; no backend switching or generic receipts |
| `lib/storage_authority_sql/native_checkpoint_outbox.py` | Selected SQL Sidecar | Shared Rust checkpoint transaction capture, ordered SQL prefix verification and passive replay; existing outbox owns encryption/publication |
| `lib/storage_authority_sql/adapters/sqlite.py` | Sidecar only | One ordered writer without job-count admission, fair priority lanes, demand-created query-only connections with idle reuse, WAL/full-sync, progress watchdog, result-code retry classification |
| `lib/storage_authority_sql/adapters/postgres.py` | Sidecar only | External TLS PostgreSQL connections, durability/schema validation, Psycopg 3 demand-created connections, isolated read/write lease owners, idle reuse and SQLSTATE retry classification |
| `packages/tofu-db/` | Supervised Rust authority | CONTROL/WAL engine, owner-scoped semantic operations and resource-budgeted `storage.v2` serving; selection and certification are separate ([native contract](../STORAGE_TOFUDB.md)) |
| `lib/storage/storage_metric_policy.py` | Shared dependency-light policy | Launch-probed recent latency sample window for application and Sidecar metrics |
| `lib/agent_core/storage_event_policy.py` | Application process | Launch-derived event residency hints and independent Sidecar frame bounds; batcher uses RAM/file overflow, while task delivery owns its separate scheduling policy |
| `lib/conversations/repository.py` | Application process | Owner-scoped conversation projections; metadata-first lazy transcript scans with recursive frame splitting and, given a time bound, newest-first message windows for one transcript no response can carry; validated interval-count projection |

| `lib/conversations/diagnostic.py` | Application process | Strict owner/revision/rank/byte-cursor validation for bounded diagnostic reads; no projection hydration or physical storage knowledge |
| `lib/storage_authority_sql/operations_pkg/_conversation_diagnostic.py`, `packages/tofu-db/src/conversation_diagnostic.rs` | Selected SQL / native authority | Read-only header/turn/chunk queries, byte slicing and integrity fences; behavioral contract in [diagnostic contract](../contracts/CONVERSATION_DIAGNOSTICS.md) |
| `lib/storage_authority_sql/fastpath.py`, `shipper.py` | Sidecar only | Opt-in measured-local SQLite write front, deployment lineage, capacity preflight, bounded-RPO durable shadow shipping and recovery |
| `lib/storage_authority_sql/storage_capabilities.py` | Sidecar/control plane | Bounded filesystem capability report and pure, conservative adaptive backend/front policy; never moves authority bytes |
| `lib/storage_authority_sql/turn_search_projection.py` | Sidecar only | Transactional dirty-set consumer; local SQLite demand readers share the connection owner with the authority; independent PostgreSQL projection transactions, corruption recovery, generation fencing and paged backfill |
| `lib/storage_authority_sql/operations_pkg/_conversations.py` | Sidecar only | Owner-scoped metadata/full/window/activity projections; SQL paging/scalar timestamps for active Turns; bounded compatibility decoding for frozen archives |
| `lib/storage_authority_sql/archived_message_codec.py`, `offline_compaction_archive_maintenance.py` | Sidecar archive boundary / stopped-server window | Backend-neutral lossless per-message storage codec; bounded owner-resolved consolidation of retired compaction snapshots |
| `lib/storage_authority_sql/task_result_field_codec.py`, `operations_pkg/_records.py`, `offline_task_result_maintenance.py` | Sidecar task-result boundary / stopped-server window | Selective backend-neutral field codec; public hydration, compact projections, and bounded version-neutral historical backfill |
| `lib/storage_authority_sql/logical_outbox.py`, `logical_shadow.py` | Sidecar only, explicit opt-in | Same-transaction durable logical outbox; asynchronous backend-neutral publisher; private checksummed filesystem sink; publisher health and backpressure |
| `lib/storage_authority_sql/logical_replay.py` | Offline verifier / future projection worker | Authenticated bounded mutation replay to SQLite/PostgreSQL with an atomic durable checkpoint, ordered projection digests, deterministic canary sampling, explicit fail-closed cutover/rollback evidence |
| `scripts/storage_deep_clean.py` | Explicit stopped-server window | Lease-owned retention, verified compaction, deferred performance-index installation, integrity/parity gates, atomic publication and rollback retention |
| `lib/storage_authority_sql/migration/tofudb_migration_{source,checkpoint,target,import}.py` | Explicit offline migration owner | Bounded pinned SQLite reads, verified identity-bound resume publication, and operator domain import; limits generated from `contracts/tofudb_ir_v1.json`; rollback, remaining domains, and cutover stay separate |
| `lib/storage_authority_sql/migrate.py` | One-shot distributed migration Job | Advisory-lock serialization and forward-only PostgreSQL schema migration; never imported as application startup authority |
| `lib/database_panel/` | Application process | Settings → 数据库 panel: engine status/switch (`.env` + restart), optional native/SQLite Fast Path configuration and actual activation, independent native search-index placement, path discovery, dual-path backup mirror; contract in [STORAGE.md](../STORAGE.md#settings-database-panel) |

## Dependency direction

```text
Repository / Service / Plugin
          |
          v
lib.storage.StorageClient
    |-- selected native v2 --> packages/tofu-db semantic executor
    `-- selected SQL v1 ----> lib.storage_authority_sql.operations
                               |-- SQLite adapter
                               `-- PostgreSQL adapter
```

Application code may depend on `lib.storage`; `lib.storage` must never import a
driver or know a database path. Sidecar code may depend on the shared protocol,
error, and manifest vocabulary. Driver-bearing adapter modules must never be
imported into the application process.

## Semantic catalog

The initial vertical slice contains schema version, versioned records,
naturally deduplicated task events, declarative plugin manifests/rows, health,
metrics, integrity, and backup operations. Domain repositories add named
operations; they do not add SQL fields or connection callbacks to the wire.

Every critical write has one transaction. Non-reconstructible effects also
have one command receipt; natural-key events and version-witnessed snapshot
checkpoints deliberately bypass the receipt table because the authority row
itself resolves identical replay and rejects stale divergent replay. Clean
no-write results are not receipts. Both adapters execute the same catalog
contract and return the same wire shapes and error codes.

Generic record metadata reads share SQL/native implementations in `_records.py`
and `versioned_document.rs`; the wire contract is in [STORAGE.md](../STORAGE.md#retention-and-size-bounds).

`task_results.checkpoint` has an additive guarded-v1 mode for independently
deployed task managers. The response echoes the exact contract only after the
Sidecar has atomically taken the conversation lifecycle and task-key locks and
enforced parent/owner/status/tombstone fences. The manager may cache the
returned version and remove its two compatibility queries only after that
echo; old clients and old Sidecars continue through the unguarded contract.
Running diagnostics use a short maintenance-lane deadline, while task birth
and terminal diagnostics retain bounded user-lane retries.

Birth witness — Problem: the durable-at-birth row of every new chat task read
`task_results.replay_get` before its checkpoint, one more storage round trip
inside task registration, although `create_task` had just minted the uuid4 task
id. Decision: a birth checkpoint for an id minted in this process
(`fresh_birth`) expects version 0 without the witness read. Both authorities
create version 1 only when no row exists and expect 0, and report
`database_conflict` otherwise; the existing conflict handler then reads the
witness (fences and abort tombstones included) and retries. The parent
`conversation.get` stays until the guard is echoed, and a prepared (restorable)
task id, which may already carry its durable birth, keeps the read.
Alternatives rejected: skipping both compatibility queries before the guard echo
(an unguarded Sidecar would then write under a deleted parent); a process-wide
guard-capability cache (a rolled-back Sidecar would inherit a stale verdict).
Spec: `tests/tasks_pkg/test_birth_checkpoint_witness.py`.

An independent additive cache-settings-v1 echo may join the staged positive
`cachePrefixHWM` and `lastTurnCacheRead` facts to that same guarded transaction.
HWM merges by maximum. A new checkpoint applies last-read LWW, while an
identical ambiguous replay preserves a different value already written by a
newer task. Without the exact echo, the manager retains the legacy per-fact
settings RMW; no cache fact depends on synchronized process-local state.

Turn-event persistence compares canonical projections at this semantic
boundary. An equal full projection, or a slim text update that leaves the full
projection equal, advances the same CAS revision and durable event sequence
without assigning the unchanged large `projection_json` value. SQLite and
PostgreSQL share that branch; process-lifetime per-event metrics count the
skipped assignments and canonical stored bytes.

## Schema evolution boundary

`lib/storage_authority_sql/schema.py` is the single schema definition and upgrade
sequence for both adapters. Application modules never import cursors, driver
errors, SQL translation, transaction helpers, or migration internals. Offline
transfer and maintenance commands may open a driver only from their explicit
CLI boundary; `tests/storage_authority_sql/test_storage_process_boundary.py` keeps that exception
small and prevents it from becoming a runtime access path.

Problem: without ripgrep, indexed startup scans included offline scripts and refused a valid Web process. Decision: Git, lexical and filesystem scans use the same runtime roots; the separate strict inventory still audits offline tools. Alternative: requiring ripgrep hides the scope defect and makes storage startup depend on an optional accelerator. Verify all Git/ripgrep availability combinations and real production restart.

Plugins contribute declarative manifests. They cannot execute schema callbacks
or receive a connection object. Manifest row writes use version-witnessed
`put`/`delete` or one bounded same-table `batch`; legacy import can only scan
the exact identifiers declared in the manifest and remains read-only.

## Storage contract generation

Problem: the database contract generator mixed file reads, compatibility guards,
native/Python/C projections and publication. Repeated reads within one invocation
could make Python and Rust artifacts use different revisions of the same input.

Decision: `scripts/tofudb_contract_codegen/` owns the authored generator.
`ContractInputs` captures each JSON document on its first read and releases the
per-invocation cache when generation ends. There is no process-wide cache or
claim of an atomic snapshot across different files. `validation.py` composes
the existing domain guards in order; their compatibility literals, rejection
behavior and authority remain unchanged. Named native, search, paper and Python
projections share those inputs. `generation.OUTPUT_PATHS` is the single output
inventory. The CLI publishes only after the complete map has been built, retaining
sequential writes and a read-only drift check.

Alternatives considered: a global memoization cache could leak old contracts into
later invocations; splitting the large functions without consolidating reads
would preserve the revision mismatch. Generating through a generic schema engine
would replace existing admission and formatting semantics without a need.

`test_contract_generator_inputs.py` covers one-read consistency, fresh subsequent
invocations, failure before publication and read-only drift checks.
`test_native_search_volume_contract.py` covers cross-language search geometry.
Run `python3 scripts/gen_tofudb_ir_contract.py` and its `--check` mode in an
isolated checkout; refactoring must preserve all generated output bytes.

## Verification


`scripts/tofudb_perf_compare.py` compares disposable authorities through their
client contracts, not raw database primitives. Its `tofudb_workloads/` package
owns the ordered workload definitions with explicit invocation inputs and
execution ports; adapters and measurement/report policy remain in the script.
Legacy engine startup is
sequential and scopes PostgreSQL DSN-file and other launch environment overrides
to the child launch, restoring the caller environment even on failure. SQLite
must not inherit a PostgreSQL test DSN or attach through a caller connection file;
each engine retains its own runtime/client without replacing the global runtime.
Native daemon stderr logs live in one private temporary directory per engine:
`stop()` deletes it after a clean run and keeps it, naming it on stderr, after a
launch failure, an unclean daemon exit or a reported workload error
(`retain_logs()`); it never globs or deletes logs it did not create.
`tests/tofudb/test_tofudb_perf_compare.py` covers both engine orders, inherited settings,
and failure cleanup offline; PostgreSQL measurements require an explicitly
provisioned disposable database, never a production authority.
- `tests/storage_authority_sql/test_storage_process_boundary.py` checks the client/route driver and
  path boundary.
- `tests/storage/test_storage_authority_sql_contract.py` launches the real SQLite sidecar and
  covers authentication, preflight, receipts, conflicts, natural idempotency,
  plugin manifests, backup, integrity, and crash fencing.

- `tests/tofudb/test_tofudb_migration_import.py` covers offline SQLite → Tofu-DB
  tenant-user/conversation-header/credential/provider/billing/record import, verified-prefix resume, identity binding,
  exact record JSON/version/time, 2,005-row paged enumeration, commit-before-checkpoint process loss, bounded memory, and child cleanup.
- The identical contract suite must run against PostgreSQL in a provisioned CI
  job and on each supported platform.
- Release load, soak, fault, FUSE, restore, and handoff gates are defined only
  in the authoritative storage document.

The backend-neutral error envelope lives in `lib.agent_core.storage_errors`;
`lib.storage.errors` re-exports the same objects. This keeps transient execution
independent of storage initialization while preserving adapter catches and wire
classification. Verify storage error diagnostics and the core persistence boundary.

# Module Design Doc — Data Tier

The authoritative behavioural and operational contract is
[`docs/STORAGE.md`](../STORAGE.md). This module note describes
code ownership only and must not redefine backend authority.

## Process ownership

| Package | Runs in | Responsibility |
|---|---|---|
| `lib/storage/` | Web, worker, task, plugin parent | `storage.v1` framing, semantic client, structured errors, supervision, readiness/write fence, declarative manifest validation |
| `lib/storage_sidecar/` | Dedicated child process | Personal database paths, project lease, FUSE preflight, drivers, pools, transactions, receipts, semantic operation catalog, SQLite backup/restore/handoff |
| `lib/storage_sidecar/adapters/sqlite.py` | Sidecar only | One writer connection, fair priority lanes, query-only pool, WAL/full-sync, progress watchdog, result-code retry classification |
| `lib/storage_sidecar/adapters/postgres.py` | Sidecar only | External TLS PostgreSQL connections, durability/schema validation, Psycopg 3 isolated read/write pools, connection budget, SQLSTATE retry classification |
| `lib/storage_sidecar/fastpath.py`, `shipper.py` | Sidecar only | Opt-in measured-local SQLite write front, deployment lineage, capacity preflight, bounded-RPO durable shadow shipping and recovery |
| `lib/storage_sidecar/storage_capabilities.py` | Sidecar/control plane | Bounded filesystem capability report and pure, conservative adaptive backend/front policy; never moves authority bytes |
| `lib/storage_sidecar/turn_search_projection.py` | Sidecar only | Transactional dirty-set consumer; independent local SQLite/shared PostgreSQL conversation-search projection; corruption recovery, generation fencing, bounded backfill |
| `lib/storage_sidecar/logical_outbox.py`, `logical_shadow.py` | Sidecar only, explicit opt-in | Same-transaction bounded logical outbox; asynchronous backend-neutral publisher; private checksummed filesystem sink; publisher health and backpressure |
| `lib/storage_sidecar/logical_replay.py` | Offline verifier / future projection worker | Authenticated bounded mutation replay to SQLite/PostgreSQL with an atomic durable checkpoint, ordered projection digests, deterministic canary sampling, explicit fail-closed cutover/rollback evidence |
| `scripts/storage_deep_clean.py` | Explicit stopped-server window | Lease-owned retention, verified compaction, deferred performance-index installation, integrity/parity gates, atomic publication and rollback retention |
| `lib/storage_sidecar/migrate.py` | One-shot distributed migration Job | Advisory-lock serialization and forward-only PostgreSQL schema migration; never imported as application startup authority |

## Dependency direction

```text
Repository / Service / Plugin
          |
          v
lib.storage.StorageClient  -- storage.v1 -->  lib.storage_sidecar.operations
                                                |              |
                                                v              v
                                         SQLite adapter   PostgreSQL adapter
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

## Schema evolution boundary

`lib/storage_sidecar/schema.py` is the single schema definition and upgrade
sequence for both adapters. Application modules never import cursors, driver
errors, SQL translation, transaction helpers, or migration internals. Offline
transfer and maintenance commands may open a driver only from their explicit
CLI boundary; `tests/test_storage_process_boundary.py` keeps that exception
small and prevents it from becoming a runtime access path.

Plugins contribute declarative manifests. They cannot execute schema callbacks
or receive a connection object. Manifest row writes use version-witnessed
`put`/`delete` or one bounded same-table `batch`; legacy import can only scan
the exact identifiers declared in the manifest and remains read-only.

## Verification

- `tests/test_storage_process_boundary.py` checks the client/route driver and
  path boundary.
- `tests/test_storage_sidecar_contract.py` launches the real SQLite sidecar and
  covers authentication, preflight, receipts, conflicts, natural idempotency,
  plugin manifests, backup, integrity, and crash fencing.
- The identical contract suite must run against PostgreSQL in a provisioned CI
  job and on each supported platform.
- Release load, soak, fault, FUSE, restore, and handoff gates are defined only
  in the authoritative storage document.

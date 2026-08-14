# Module Design Doc — Data Tier

The authoritative behavioural and operational contract is
[`docs/STORAGE_REDESIGN.md`](../STORAGE_REDESIGN.md). This module note describes
code ownership only and must not redefine backend authority.

## Process ownership

| Package | Runs in | Responsibility |
|---|---|---|
| `lib/storage/` | Web, worker, task, plugin parent | `storage.v1` framing, semantic client, structured errors, supervision, readiness/write fence, declarative manifest validation |
| `lib/storage_sidecar/` | Dedicated child process | Database paths, project lease, FUSE preflight, drivers, pools, transactions, receipts, semantic operation catalog, backup/restore/handoff |
| `lib/storage_sidecar/adapters/sqlite.py` | Sidecar only | One writer connection, fair priority lanes, query-only pool, WAL/full-sync, progress watchdog, result-code retry classification |
| `lib/storage_sidecar/adapters/postgres.py` | Sidecar only | Project-local cluster lifecycle, checksum/durability verification, isolated read/write pools, 80% connection budget, SQLSTATE retry classification |
| `lib/database/` | Migration compatibility surface | Existing repositories being moved to named operations; it is not an allowed long-term connection owner |

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

Every critical write has one transaction and one command receipt. Natural-key
event writes deliberately bypass the receipt table. Both adapters execute the
same catalog contract and return the same wire shapes and error codes.

## Migration boundary

`lib/database` still identifies migration work, not a second permanent access
path. A domain is complete only when its services no longer import
`get_thread_db`, cursors, driver errors, SQL translation, or transaction
helpers. Static checks are tightened as each domain lands; production cutover
is blocked until the legacy allowlist is empty.

Plugin migration replaces `tofu.schema` callbacks with manifest discovery.
The compatibility callback registry must be removed before the production
gate opens.

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

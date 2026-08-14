# Storage architecture (`storage.v1`)

> This document is the single source of truth for durable storage. Other
> documents must link here rather than restating backend authority or failure
> behaviour.

> **Implementation status (2026-08-14): not approved for production cutover.**
> The isolated `storage.v1` runtime, both backend adapters, command receipts,
> declarative plugin API, supervision, and maintenance commands are present and
> contract-tested. The existing application domains have not all crossed that
> boundary yet: the CI migration inventory currently contains at most 58
> production Python files referencing `get_thread_db`, seven legacy driver-
> importing modules, and five `tofu.schema` references. Those ratchets may only
> decrease. Application startup must remain on the existing path until they
> reach zero and the release gates in section 11 pass; do not run the Sidecar
> concurrently against the legacy live authority.

Completed domain slices include rate limiting, orchestration, durable Swarm
session state, research artifacts, optimizer repositories, log aggregates,
paper reports/translations/library reads, title healing, podcast state,
renderable chat artifacts, tenant-user accounts, and the persistent daily-cost
cache. The daily report's live conversation scan, the artifact backfill
route's conversation read, and the durable Swarm panel snapshot still belong
to the unmigrated conversation-message CAS domain; naming them here avoids
mistaking a migrated cache/session table for a completed business domain.

## 1. Non-negotiable contract

- `Storage Sidecar` is the only process allowed to load SQLite/PostgreSQL
  drivers, derive database paths, open connections, or own transactions.
- Web, task, worker, and plugin processes call versioned semantic operations
  through `StorageClient`. RPC never accepts SQL, paths, connection handles, or
  transaction handles.
- SQLite and PostgreSQL are equal, supported enterprise backends. SQLite is the
  default. PostgreSQL activates only with `TOFU_DB_BACKEND=postgres`.
- An invalid selector, failed preflight, unavailable selected backend, or failed
  schema/integrity check prevents readiness. There is no backend fallback.
- Databases, PostgreSQL clusters, backups, migration state, leases, and logs
  stay under the project directory. Runtime port and authentication token move
  only through the parent/child control channel and environment; they are not
  persisted or printed in argv/logs.
- The supported single-instance target is 1,000 authenticated online users and
  200 continuously active tasks/streams on Linux, macOS, and Windows. Linux
  additionally requires certification on representative enterprise FUSE.

## 2. Process and protocol boundary

The parent starts `python -m lib.storage_sidecar`, passes a freshly generated
48-byte-class token in the child environment, and reads the random loopback
port from the one-line stdout control envelope. The child listens only on
`127.0.0.1`. Messages use a four-byte network-order length followed by bounded
`orjson`; the fixed protocol identifier is `storage.v1`.

The public client surface is:

```python
query(operation, payload, deadline)
command(operation, payload, command_id, priority, deadline)
health()
metrics()
maintenance(operation, payload, deadline)
```

Every request has a correlation ID, absolute deadline, protocol identifier,
and authentication token. Operation names are validated. The semantic catalog
is compiled into the sidecar; unknown names fail with
`database_protocol_error`. Arbitrary SQL has no wire representation.

The parent reports ready only after the sidecar has acquired the project lease,
completed filesystem/backend/schema/read-write preflight, bound its listener,
and passed a fresh `health()` handshake. If the child exits, readiness is
revoked immediately and new writes are fenced. A supervisor may restart and
re-handshake the same configured backend; it must not choose another backend.

## 3. Backend selection and project layout

`TOFU_DB_BACKEND` accepts exactly `sqlite` or `postgres`; absence means
`sqlite`. Aliases such as `pg`, historical authority markers, or availability
probes never override it.

```text
<project>/
  data/
    tofu.db                         # SQLite authority when selected
    pgdata/                         # PostgreSQL cluster when selected
    backups/                        # verified SQLite/PG backups
    .storage-sidecar.lock           # process-held lease lock
    .storage-sidecar-lease.json     # auditable owner stamp
    storage-handoff-audit.jsonl     # explicit handoff/recovery audit
  logs/
    storage-postgresql.log
```

Test-only project-root overrides require the explicit
`TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE=1` authority. Database paths never arrive
from RPC payloads.

## 4. SQLite transaction model

- Exactly one physical read-write connection owns all primary-file writes.
- The default read pool contains 16 physical `query_only` connections. A read
  cannot upgrade to a write transaction.
- The writer uses weighted fair service (user 8, event 2, maintenance 1) so
  critical user writes lead without starving event or maintenance work.
- Pool/writer acquisition is bounded at two seconds. Connections rotate after
  60 seconds idle or 15 minutes total lifetime.
- WAL, `synchronous=FULL`, foreign keys, and a 4,096-page auto-checkpoint remain
  enabled. FUSE is not "fixed" by weakening durability.
- Every command is one `BEGIN IMMEDIATE` transaction. The default five-second
  watchdog uses SQLite's progress handler. Maintenance work must use explicit,
  longer deadlines and bounded units.
- Classification uses numeric SQLite result codes. Rollback occurs before all
  retry decisions. No layer parses `"database is locked"` or other localized
  exception text.
- Regenerable events use natural uniqueness such as `(task_id, sequence)` and
  may be grouped behind a maximum 300 ms / 500 item durability window. Terminal
  events must commit before outward acknowledgement.

## 5. PostgreSQL transaction model

- The sidecar initializes, starts, connects to, and stops the project-local
  `data/pgdata` cluster. Data checksums are mandatory.
- `fsync=on`, `synchronous_commit=on`, and `full_page_writes=on` are verified at
  every start. Failure is fatal.
- Read and write connections use isolated pools (defaults 32 and 16). The
  combined allocation is automatically reduced so application pools remain at
  or below 80% of the server's `max_connections`, leaving admin/recovery
  headroom.
- Pool acquisition, idle/max lifetime, transaction watchdog, rollback-before-
  retry, and semantic operation contracts match SQLite.
- PostgreSQL retry/error decisions use SQLSTATE and exception classes, never
  message text.

## 6. Filesystem/FUSE preflight

Before opening for service, the sidecar verifies under the project data root:

- minimum free space;
- complete writes, file `fsync`, directory `fsync`, and atomic replace;
- exclusive file locking and project lease acquisition;
- latency below the configured safety ceiling;
- SQLite WAL close/reopen recovery plus `integrity_check`, or PostgreSQL
  checksums/durability settings plus non-recovery primary state.

An enterprise FUSE certification run additionally injects latency, short I/O,
disk-full, process kill, WAL damage, and reconnect faults. A mount that cannot
prove the required semantics is rejected with a concrete diagnostic.

## 7. Exactly-once commands and errors

Critical commands require `command_id`. Within the same transaction as the
business mutation, `storage_command_receipts` records command ID, operation,
canonical request SHA-256, a response capped at 64 KiB, and commit time.

- same ID + same operation/payload: return the stored response;
- same ID + different operation/payload: `database_conflict`;
- failed/rolled-back command: no receipt and no business mutation.

Regenerable event commands use natural unique keys and do not grow the receipt
table.

Every failure is sanitized and includes `code`, `retryable`,
`retry_after_ms`, and `operation_id`. Stable codes are:

- `database_busy`
- `database_unavailable`
- `database_timeout`
- `database_conflict`
- `database_integrity`
- `database_protocol_error`
- `database_internal`
- `plugin_storage_incompatible`

HTTP maps busy/unavailable/timeout to 503, conflict to 409, and integrity,
protocol, internal, or plugin compatibility failure to 500. Logs contain
operation/correlation IDs and classifications, not SQL, parameters, tokens, or
sensitive payloads.

## 8. Plugins

Plugins use a declarative manifest with namespace, monotonically increasing
version, tables, typed columns, primary keys, indexes/unique constraints, and
named operations. Supported operation actions are validated `get`, `list`,
`put`, and `delete`; write operations require command receipts.

The sidecar validates identifiers, types, action/kind parity, migration order,
append-only compatibility, document constraints, uniqueness, and backend
compatibility. Incompatibility returns `plugin_storage_incompatible` and the
plugin remains disabled. `tofu.schema` connection callbacks, arbitrary plugin
SQL, and initialization callbacks are legacy interfaces to remove before the
production gate opens.

## 9. Operations

The protected project-local command is:

```bash
python scripts/storagectl.py [--backend sqlite|postgres] COMMAND
```

Commands are `preflight`, `status`, `backup`, `restore`, `handoff`, and
`integrity-check`. Backups are created and verified under `data/backups`.
Restore accepts only a project backup, requires `--confirm`, runs offline under
the exclusive lease, and preserves the previous authority as a recoverable
project-local backup. Handoff requires the old sidecar to be stopped and the
lease acquirable; forced recovery is explicit and audited.

## 10. Migration and one-window cutover

Development may migrate domains in stages, but production has one 30–60 minute
maintenance cutover and no durable dual-write period:

1. Freeze the current validation baseline and make direct driver/path/SQL/
   transaction access a CI failure.
2. Land `storage.v1`, supervision, errors, receipts, SQLite scheduling, and the
   PostgreSQL adapter.
3. Move each Repository/Service to named `StorageClient` operations and run the
   identical contract suite against both backends. Remove `get_thread_db`, raw
   cursors, in-process pools, and SQL translation from business processes.
4. Convert all plugin storage to manifests and remove `tofu.schema` callbacks.
5. During the maintenance window: stop ingress/workers, drain writes, create
   and verify a project-local backup, run expansion-only schema migration,
   start the selected sidecar, compare counts and critical digests, start the
   app, perform read/write smoke tests, then reopen traffic.

If acceptance fails, stop the sidecar and restore the pre-window entry point.
Because the migration is expansion-only and there is no double write, the
original authority remains usable; use the verified backup if integrity checks
require it.

Cross-host movement is stop-old, prove lease release, start-new. There is no
automatic cross-host failover and no runtime backend switching.

## 11. Release gates

Both backends run the same tests for atomicity, isolation, rollback, retry,
idempotency, duplicate acknowledgements, plugin manifests, migrations,
backup/restore, and every error code. Fault injection covers sidecar kill,
disconnect, disk full, FUSE delay/short I/O, WAL corruption, long read
snapshots, timeout, duplicate request, and lost acknowledgement.

Each OS/backend pair requires a 60-minute benchmark and 24-hour soak; Linux
SQLite and PostgreSQL repeat the full run on representative enterprise FUSE.
At 1,000 authenticated connections and 200 active tasks/streams:

- zero loss or duplicate commit of non-regenerable data;
- zero writer-lane timeout, connection leak, or unclassified database 500;
- regenerable persistence lag no more than 300 ms;
- storage read p95 <= 100 ms and p99 <= 250 ms;
- critical write acknowledgement p95 <= 200 ms and p99 <= 500 ms;
- bounded queues/connections/file descriptors/RSS that return after load.

### 11.1 Repeatable Linux FUSE certification

The checked-in harness refuses a non-FUSE Linux project path unless the
operator explicitly supplies `--allow-non-fuse`. Its default run is the
required 60-minute, 200-stream load against both backends:

```bash
python scripts/storage_certify.py --backend both --workers 200 \
  --duration-seconds 3600
```

It writes the mount identity, latency distributions, event durability lag,
writer/RPC/process bounds, receipt replay result, and integrity result to
`data/storage-certification/load-*/summary.json`. Results remain project-local
and the tool never weakens durability settings.

On 2026-08-14 the current project path was identified as `fuse.bgfuse`
(`beegfs-fuse`). A 10-second gate-calibration run with two seconds of warm-up,
200 streams, a 50 ms operation interval, about 200 regenerable events/second,
and both real backends passed every automated threshold. After the expanded
paper-title and daily-cost semantic operations landed, the same profile passed
again at
`data/storage-certification/load-20260814T113923404690Z/summary.json`.
SQLite read p95/p99 was 45.838/50.903 ms, critical-write p95/p99 was
52.592/78.262 ms, and maximum event persistence lag was 187.389 ms;
PostgreSQL measured 70.981/78.140 ms, 77.473/80.173 ms, and 205.621 ms.
Both reported zero errors, writer timeouts, RPC rejections, or batch failures.
The associated backend-neutral functional/adapter/FUSE-fault suite passed 152
tests in 405.80 seconds on the same mount. This is valid short-run FUSE
evidence, not a substitute for the still-required 60-minute benchmark or
24-hour soak.

After the artifact and tenant-user slices landed, the current code was checked
again on the same BeeGFS mount. SQLite passed the 200-worker profile in
`data/storage-certification/load-20260814T121543184481Z/summary.json` with
read p95/p99 59.209/62.749 ms, critical-write p95/p99 71.371/85.725 ms,
and maximum event lag 220.981 ms. The PostgreSQL leg in that combined run
recorded one shared-host scheduling spike (read p99 266.879 ms and maximum
event lag 933.664 ms) while another four-worker full test run and Sidecar were
active, so that combined summary correctly remains failed. An immediate
PostgreSQL-only repeat at
`data/storage-certification/load-20260814T121709405151Z/summary.json` passed:
read p95/p99 68.902/70.968 ms, critical-write p95/p99 72.895/74.253 ms,
maximum event lag 222.461 ms, and zero errors, retries, timeouts, or RPC
rejections. The current FUSE fault-injection suite also passed 5/5. Both the
failed sample and the clean repeat are retained because a short calibration
run cannot establish tail-latency stability under unrelated host contention.

An exploratory 800-event/second SQLite overload was intentionally kept out of
the standard profile: repeated short runs showed FUSE variance and sometimes
exceeded the 300 ms event window. No capacity claim is made for that overload.

Production cutover also requires a real restore drill, integrity check, and
old/new-node handoff drill. Until every gate passes, the new architecture is
not certified for production traffic.

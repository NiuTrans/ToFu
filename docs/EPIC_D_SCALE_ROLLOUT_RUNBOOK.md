# Distributed deployment rollout runbook

This is the operator companion to
[`STORAGE.md`](STORAGE.md). It applies only to the
Kubernetes topology. The standalone installer and Docker Compose remain the
personal SQLite topology and never provision PostgreSQL or Redis.

## Preconditions

- One external, highly available PostgreSQL primary with TLS, backups, PITR,
  monitoring, and credential rotation owned by the platform.
- One external, highly available Redis service with TLS. Redis is ephemeral
  coordination only; it is not a task, event, or conversation authority.
- Absolute secret-file mounts containing a PostgreSQL DSN with
  `sslmode=verify-full` and a `rediss://` URL.
- A unique, stable replica ID per Pod.
- A migration Job built from the same release as the application images.
- A verified personal SQLite snapshot and a tested restore path before the
  first data migration.

Self-running `initdb`, `pg_ctl`, a project-local `pgdata`, or an unencrypted
Redis endpoint is outside the supported distributed topology.

## Configuration contract

Every distributed Pod sets:

```text
TOFU_DEPLOYMENT_MODE=distributed
TOFU_DISTRIBUTED_PREVIEW_MODE=read-only
TOFU_PROCESS_ROLE=api|worker|scheduler
TOFU_POSTGRES_DSN_FILE=/run/secrets/tofu/postgres-dsn
TOFU_REDIS_URL_FILE=/run/secrets/tofu/redis-url
TOFU_REPLICA_ID=<stable-pod-id>
```

The process role maps to owners in `lib/process_roles.py`:

| Role | Owned work |
|---|---|
| `api` | frontend, request/catalog services, network configuration |
| `worker` | task recovery and task/background execution |
| `scheduler` | timed jobs and event retention/reclamation |
| `all` | transitional single-replica contract testing only |

Removed variables `TOFU_DB_BACKEND`, `TOFU_REQUIRE_PG`, and
`TOFU_REPLICA_RING` are fatal. Direct DSN values and project-local PostgreSQL
management flags are not part of the public contract.

## Preview safety boundary

The chart currently validates the distributed process, external-service, and
Storage Sidecar wiring; it is not yet a claim that chat execution can scale
horizontally. Its defaults are one API, one worker, and one scheduler replica,
with both API and worker HPAs disabled. Do not override those replica counts or
enable an HPA until the durable chat worker is registered in the production
composition root and the Pod-kill takeover, stale-fence rejection, and random
load-balancing acceptance gates pass. The chart values schema rejects those
overrides while this preview boundary is active; clearing it requires an
explicit chart/schema release change, not an operator-side `--set`. A shared
PostgreSQL database alone does not satisfy that boundary.

`TOFU_DISTRIBUTED_PREVIEW_MODE=read-only` is a mandatory technical latch, not
an advisory label. Application startup rejects distributed configuration
without it; HTTP mutations and WebSocket handshakes are refused before route
execution; the Sidecar rejects every command; and worker, scheduler, recovery,
and optional background owners remain stopped. The one-shot migration Job is
the sole write path because it connects directly under its advisory schema
lock. Removing this latch requires the durable execution and failure-injection
acceptance gates in this runbook plus an explicit release change.

## Rollout sequence

1. Render and validate `deploy/helm/tofu` with both image digests. The chart
   must pass `helm lint` and `scripts/check_helm_render.py`; it deliberately
   refuses an omitted digest and never creates the external-services Secret.
2. Build both digest-pinned, non-root image targets. Verify the `api` image has
   no Playwright, compiler, or PostgreSQL server binaries; verify the `worker`
   image contains the declared browser runtime.
3. Run `python -m lib.storage_sidecar.migrate` as the chart's one-shot hook Job. The Job takes
   the PostgreSQL advisory migration lock and advances the schema. Application
   Pods only validate the exact schema version and execute no startup DDL.
4. Start the default one API replica, one worker, and one scheduler with the
   Service selecting only API Pods. Each Pod must have exactly one local
   Storage Sidecar and the private memory-backed connection handoff. Require all
   three fixed probes:
   `/health/startup`, `/health/ready`, and `/health/live`.
5. Exercise storage-operation parity and one read-only user journey against
   PostgreSQL. Confirm Redis loss makes new admission unavailable while
   accepted PostgreSQL-backed state remains queryable.
6. During the approved stop-write window, snapshot SQLite, import tables,
   correct sequences, compare row counts/content checksums, and run a read-only
   smoke against PostgreSQL. Open writes only after every check passes.
7. After the preview safety boundary is cleared, scale API replicas behind
   random load balancing and verify no sticky session is required for queries
   and durable SSE replay. This is an acceptance gate, not a supported default
   rollout step while the chart remains in preview.
8. Scale workers only after the release's durable claim/heartbeat/fencing
   acceptance suite proves Pod-kill takeover and stale-writer rejection.
9. Keep the original SQLite database read-only until a full PostgreSQL restore
   exercise and the post-cutover observation window both succeed.

## SQLite to PostgreSQL stop-write import

The importer is plan-only by default and does not connect to PostgreSQL:

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source data/tofu.db \
  --postgres-dsn-file /run/secrets/tofu/postgres-dsn \
  --report data/sqlite-to-postgres.report.json
```

Run the copy only inside the approved stop-write window, after the Sidecar and
Web process are stopped:

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source data/tofu.db \
  --postgres-dsn-file /run/secrets/tofu/postgres-dsn \
  --report data/sqlite-to-postgres.report.json \
  --execute --source-quiesced --confirm-empty-target
```

The DSN has no command-line/string alternative: it must come from an absolute,
bounded secret file and must require `sslmode=verify-full`. The importer holds
the SQLite project lease, opens one query-only transaction, and takes the
PostgreSQL schema-migration lock followed by its data-import advisory lock. It
refuses a target whose schema version differs from the release or whose
business tables contain any row.

Full mode dynamically compares the source and target table sets before writing.
An old SQLite table without a current PostgreSQL schema owner is a hard blocker,
not silently dropped data. Copying is table-wise and batch-bounded; publication
requires exact row counts plus the order-independent, duplicate-sensitive
XOR/SUM SHA-256 digest for every table and correction of every PostgreSQL-owned
sequence. The final PostgreSQL commit is one transaction, so an interrupted or
failed import rolls back to the required empty target.

`--table NAME` is only a contract/smoke aid. Its report status is always
`partial_verified`, `cutover_ready` is always false, and that target must be
discarded before a full run. Only a committed full copy can produce
`status=verified` and `cutover_ready=true`. Reports are fsynced and atomically
renamed, contain the secret-file path but never the DSN, and record only an
exception type on failure because driver messages may contain connection data.

The repository's default/unit test path uses fake PostgreSQL targets and makes
no network connection. Before production cutover, the same release still must
pass the PostgreSQL integration matrix, a read-only application smoke, and the
restore exercise; the unit-tested CLI contract alone is not a migration drill.

## Probe semantics

- `/health/live` proves only that the process/event loop can answer. It never
  probes storage and must not cause dependency failures to restart every Pod.
- `/health/startup` remains 503 until the lifecycle and Sidecar are ready.
- `/health/ready` becomes 503 during startup, shutdown, or Sidecar loss so the
  Service stops routing new traffic.
- Authenticated diagnostics expose dependency detail; public probes contain
  only lifecycle state, process role, and a storage-ready boolean.

## Failure policy

- PostgreSQL unavailable or schema-mismatched: startup/readiness fail closed.
- Redis unavailable: new distributed admission fails with 503 and
  `Retry-After`; no fallback to per-process caps is allowed.
- A Redis wake hint may be lost. Consumers recover through PostgreSQL cursors
  and must never treat Pub/Sub delivery as a commit.
- A migration-lock conflict leaves the Job unsuccessful; application Pods do
  not race it by attempting their own DDL.
- PostgreSQL backups and PITR are platform operations. The application backup
  operation intentionally refuses that backend.

## Rollback boundary

Before PostgreSQL writes open, rollback means keeping SQLite read-only and
returning traffic to the verified personal release. Once PostgreSQL accepts
new writes, SQLite is no longer current: use forward repair or a separately
verified reverse export. Never point two writable authorities at the same
release, and never delete the last verified backup to create migration space.

Record image digests, schema version, migration receipt, validation hashes,
probe results, and the human approval for opening writes in the deployment
change record.

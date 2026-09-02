# Storage authority

Responsibility: the current durable-data boundary. This document describes the
running system; Git history contains migration plans and past measurements.

## One authority

The Storage Sidecar is the only process that loads a database driver, resolves
a database path or DSN, opens transactions, runs schema work, or executes SQL.
Application code calls named semantic operations through `StorageClient`:

```text
route / application service / worker
  -> owner-scoped repository or semantic operation
  -> StorageClient query | command
  -> authenticated storage.v1 RPC
  -> operation registry
  -> one Sidecar transaction
  -> SQLite or PostgreSQL adapter
```

There is no in-process database fallback and no runtime store switching. A
missing or unhealthy selected authority revokes readiness.

## Owners and entry points

| Concern | Owner |
|---|---|
| Client API, deadlines, supervision | `lib/storage/` |
| RPC server and operation dispatch | `lib/storage_sidecar/server.py`, `operation_registry.py` |
| Semantic operation domains | `lib/storage_sidecar/operation_domains/` |
| Transaction implementations | `lib/storage_sidecar/operations_pkg/` |
| Durable schema and migrations | `lib/storage_sidecar/schema.py` |
| SQLite/PostgreSQL adaptation | `lib/storage_sidecar/adapters/` |
| Deployment/preflight | `lib/storage_sidecar/config.py`, `preflight.py` |
| Operator CLI | `scripts/storagectl.py`, `python -m lib.storage_sidecar.migrate` |

The current machine versions are `storage.v1`, schema version 40, and operation
registry version 21. Code constants are authoritative; update this sentence in
the same change when either version advances.

## Identity and dependency rules

- Auth resolves identity once at the request boundary. Every durable
  user-owned operation below it receives a positive `owner_user_id` (some
  established repository method parameters remain named `user_id`; their
  value is always the numeric owner, never an account subject).
- Routes contain no SQL and do not derive storage paths.
- Application services speak domain vocabulary; operation handlers translate
  that vocabulary into backend-neutral SQL authored inside the Sidecar.
- SQLite and PostgreSQL expose the same operation/result/error behavior.
- Resource IDs are never treated as proof of ownership. Reads and writes join
  or filter by the explicit owner.
- Personal mode may currently compose owner `1`, but repositories and schemas
  must not encode “one user exists” as a core invariant.

Account and credential storage is defined by
[`contracts/identity_v1.yaml`](../contracts/identity_v1.yaml). `tenant_users.id`
is the opaque account subject; its unique `owner_user_id` is allocated by
`storage_identity_sequences`. `auth_credentials` is the only bearer authority
and stores a one-way token digest. Credential administration filters by exact
owner and tenant, while authentication atomically updates `last_used_at` and
rejects credentials bound to inactive accounts.

## Commands, queries, and idempotency

`query(operation, payload)` is read-only. `command(operation, payload,
command_id)` is one transaction. Operations with non-reconstructible effects
require a command receipt written in that same transaction.

- same command ID + same operation and canonical payload returns the committed
  response;
- same command ID + different operation or payload is `database_conflict`;
- a rollback writes neither receipt nor business mutation;
- a read-only refusal or empty lease claim is not receipt-cached;
- maintenance commands use the low-priority writer lane and bounded batches.

Command receipts remain capped at 64 KiB on both backends. Responses above
that legacy JSON size get one private zlib level-1 attempt only when their
decoded form is at most 4 MiB; incompressible or larger responses still reject
and roll back atomically. The versioned decoder bounds output before parsing,
fails corrupt/unknown envelopes as ``database_integrity``, and continues to
read every legacy plain-JSON receipt byte-for-byte.

RPC accepts named operations and JSON-compatible payloads only. SQL, paths,
connections, and transaction handles have no wire representation.

Declarative plugin manifests may expose `get`, bounded `list`, `put`,
`delete`, and same-physical-table `batch` operations. A batch is one Sidecar
command transaction, rejects duplicate keys, and carries per-row version
witnesses for optimistic concurrency. Prefix/cursor traversal remains bounded
by the manifest's `limit_max`; plugin code never supplies an expression or SQL
fragment. The startup-only `legacy_scan` action is further restricted to the
exact table, column, and ordering identifiers validated from the immutable
manifest. It exists solely for verified cutovers from tables already owned by
the authority; it is read-only and cannot delete or mutate the legacy source.

## Conversation data

`storage_conversations` is the active header/settings index.
`storage_conversation_turns` is the sole active transcript authority.
`messages_json` and aggregate `search_text` on the header are frozen empty
placeholders; no runtime writes a conversation-sized message document.

The metadata-only `conversation.list` query accepts an optional bounded
`project_path`. The Sidecar combines it with the required owner predicate
before ordering and limiting, then applies `settings_keys` projection before
the result crosses the RPC boundary. Project consumers retain only the small
`projectPath` witness needed to fail closed during a mixed-version rollout.

At rest, a versioned private projection codec interns only tool-segment
``input`` / ``result.content`` values that are exactly equal to the uniquely
identified ``toolRounds`` copy. Every semantic read hydrates those references
before mutation, search extraction, or return, so the public turn/API contract
is unchanged; divergent, ambiguous, partial, and future shapes remain verbatim.
Malformed or unknown codec references fail closed as ``database_integrity``.
Existing rows are converted only by their next ordinary write—there is no
startup scan or rewrite competing for the authority writer.

Conversation search is an eventually consistent, rebuildable projection. A
turn or lifecycle mutation writes only one small version-token dirty marker to
`storage_projection_outbox` in the authority transaction. Search-text
extraction, historical scans, and projection I/O never run inside the user
transaction or the authority writer. An acknowledgement deletes only the
exact token the worker read; a concurrent mutation replaces that token and
therefore cannot be lost behind a stale acknowledgement.

SQLite materializes `storage_search_conversations` and
`storage_search_turns` in a private host-local `turn-search-v1.sqlite3`, keyed
by a hash of the authority data root. The file is disposable, quick-checked on
open, discarded and rebuilt on corruption, and bounded by
`TOFU_TURN_SEARCH_PROJECTION_MAX_MIB` (personal default: 2% of observed free
space, clamped to 128 MiB..4 GiB). Search failure degrades only search; it does
not revoke authority readiness or roll back a user write. PostgreSQL uses the
same tables in the shared database but writes them through independent,
separately committed maintenance transactions.

The Sidecar owns the projection worker. It drains current dirty entities
first, then begins historical backfill after the default 60-second startup
quiet period. Authority reads use bounded 8-row/2-MiB pages through the read
pool; source projections whose text representation could exceed that budget
after worst-case UTF-8 encoding and the private codec's bounded 2× hydration
are omitted explicitly instead of being loaded. Global cursors include owner,
conversation, and turn identity. Conversation rebuilds use generation tokens,
so old rows remain searchable until the new generation is complete and a
concurrent mutation forces another pass. The compatibility
`turn.search.backfill` command now schedules one rebuild marker and returns
immediately. Legacy `storage_turn_search` and header `search_text` are not read
authorities.

This split follows the same durable-authority/projection boundary visible in
the vendored Codex state runtime: Codex keeps state, logs, queues, and
paginated thread history in separate SQLite databases, and explicitly refuses
to retrofit a full `VACUUM` during startup because maintenance would contend
with foreground writers (`codex/codex-rs/state/src/sqlite.rs`). Tofu goes one
step further for a network-mounted authority by placing rebuildable search on
a host-local database and rejecting automatic page relocation before it
reaches the authority writer.

Header deletion/restore/clone is defined by
[`contracts/conversation_lifecycle_v1.yaml`](../contracts/conversation_lifecycle_v1.yaml):

- delete atomically moves the header and normalized turn graph to
  `storage_conversation_trash*`, removes executable state, and makes every
  active query/write observe “missing” immediately;
- restore atomically rebuilds active rows without attempts or live latches;
- clone atomically creates a new terminal graph and remaps executable
  identities; the browser never uploads a message array;
- maintenance permanently purges trash after 30 days in bounded oldest-first
  batches.

Conversation Sync v3 owns turn commands, snapshots, and replay; see
[CONVERSATION_SYNC_V3.md](CONVERSATION_SYNC_V3.md).

## Transaction model

### SQLite

- One physical read-write connection serializes all writes; read-only pooled
  connections cannot upgrade to writers.
- Active RPC handlers stay hard-capped. The accept loop may wait at most 100 ms
  for one slot so a short scheduling burst drains through the bounded kernel
  backlog; sustained saturation receives a classified retryable rejection.
- The writer uses weighted user/event/maintenance lanes.
- Backlogged compatible writes share group commits, while each logical job
  keeps its own deadline, savepoint, result, and rollback behavior. The sole
  cross-domain writer receives one launch-probed, bounded 8..64 MiB personal
  page cache (`TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB`; explicit ceiling
  256 MiB); readers retain SQLite's lean cache.
- Commands use `BEGIN IMMEDIATE`, bounded acquisition, and progress-handler
  deadlines. Rollback precedes error classification. Diagnostics publish the
  current `begin` / `execute` / `commit` / `rollback` / `post_commit` phase.
- A statement stalled 15 seconds past its deadline is interrupted. Because
  SQLite cannot interrupt a kernel-blocked commit/fsync, a writer still stuck
  60 seconds past its deadline hard-exits only the Sidecar; the supervisor
  restarts it and WAL recovery preserves the committed prefix. Both bounds are
  explicit and fail configuration if the hard bound is not greater.
- WAL, `synchronous=FULL`, foreign keys, and bounded checkpoints remain on.
- Personal startup probes the filesystem that actually contains `data/` and
  reserves 1% of its capacity, clamped to 256 MiB..2 GiB. The Sidecar refuses
  startup below that floor; `TOFU_STORAGE_MIN_FREE_BYTES` is the explicit
  operator override.
- FUSE is accepted only after lock, fsync, atomic-replace, WAL-recovery, and
  latency preflight succeeds; durability is never weakened to accommodate a
  mount.
- Before reclamation reaches the writer, the adapter checks the observed
  authority topology. Network, generic userspace, and unknown filesystems
  return `offline_required` without executing any SQLite statement: even one
  `incremental_vacuum(1)` can be an uninterruptible kernel/filesystem call on
  BeeGFS/NFS/FUSE. Known host-local block, memory, and container-overlay
  authorities may enter the bounded online path.
- Eligible reclamation checks its wall budget between
  `incremental_vacuum(1)` units.
  A freelist of at least 1,048,576 pages that also occupies 25% of the file is
  classified as bulk compaction: online page moves stop for that process and
  the response points to the explicit offline deep-clean workflow.
  Transactional retention operations are registered once with kind
  `maintenance` and dispatched through the same backend-neutral operation
  catalog as commands; they run at maintenance writer priority and never
  accumulate command receipts. Task-event streaming and structural tiers use
  six-hour and 30-day horizons respectively. By default, each tier deletes at
  most 16 separately committed pages of 25 rows; a remaining backlog is revisited in
  30 seconds, while a drained backlog is probed only every five minutes. The
  mutually exclusive, age-only partial indexes make both an empty probe and
  `ORDER BY created_at_ms LIMIT` range-bounded without scanning the other tier.
  Streaming backlog drains before the longer-lived structural
  tier, so one maintenance cycle cannot stack both workloads. An established
  SQLite authority that still has the v1
  `(stream_kind, event_type, created_at_ms)` index uses a compatibility path:
  it seeks at most 64 distinct event types and deletes one exact-type page per
  transaction, never issuing the unsafe tier-wide scan/sort. Streaming type
  discovery is interleaved with those exact-type deletes and stops as soon as
  one non-empty page is found, so a backlog page does not enumerate every type
  while holding the sole writer. Once typed streaming backlog is empty, the
  same compatibility path self-heals rows written before event type/kind
  columns existed. It selects blank rows through the indexed streaming cutoff,
  with the ordinary 25-row commit limit plus a separate 4 MiB stored-payload
  materialization budget. Decodable streaming rows past six hours are deleted;
  structural rows retain their payload and receive recovered metadata. Opaque
  rows remain structural and receive only an internal progress marker so one
  malformed oldest row cannot starve later rows. It keeps growth bounded before
  the explicit offline v2 index window; abnormal type cardinality or an
  authority with neither index disables the optional sweep explicitly. All
  online retention and reclamation work
  shares a process-lifetime circuit:
  if any bounded maintenance unit trips the writer watchdog, optional
  housekeeping stops instead of repeatedly starving user/event writes;
  restart performs one fresh probe.
- Best-effort log aggregates persist counts separately from their TTL sweep.
  Failed flushes back off exponentially to five minutes; TTL deletion runs in
  the maintenance lane and removes at most 500 indexed rows per transaction,
  so observability cannot form a retry storm behind user writes.

### PostgreSQL

- The adapter connects to an externally managed primary with verified TLS.
  The application never runs database-server binaries or stores PG data.
- Read/write pools are isolated and capped below the server connection budget.
- `fsync`, synchronous commit, full-page writes, primary status, schema version,
  and transaction deadlines are verified.
- Application startup validates only. `python -m
  lib.storage_sidecar.migrate` is the one schema migration entry point and
  publishes a new version only after the migration transaction commits.
- Distributed worker claims use adapter-owned `FOR UPDATE SKIP LOCKED` row
  selection. SQLite executes the same operation inside its serialized writer
  transaction; backend-specific lock syntax never enters application code.
- Backup/PITR, HA, and autovacuum belong to the platform.

### Adaptive topology and transactional logical shadow

`lib/storage_sidecar/storage_capabilities.py` is the single vocabulary for
storage topology and primitive capabilities. One bounded probe reports the
mount/filesystem class, persistence confidence, free bytes, private-file
creation, file and directory fsync, atomic replace, exclusive locking, and a
SQLite WAL recovery round trip. Startup preflight exposes the topology fields
in health alongside its existing durability results. Known network and generic
FUSE filesystems never become a local SQLite authority merely because a
single-process WAL round trip or latency benchmark succeeds; unknown evidence
falls back instead of being guessed. Paths under the system temporary or
`XDG_RUNTIME_DIR` lifecycle remain ephemeral even when their backing device is
a persistent local filesystem.

The pure adaptive planner may automatically retain direct SQLite or select an
external client/server adapter when that preserves the declared durability
contract. A verified, decisively faster local SQLite front is only a
recommendation until bounded-RPO consent is already present: directory
permissions, free space, or an apparent local disk are not consent. Every plan
names the selected and recommended strategies, durability contract, reason
code, evidence, and whether one user decision is required.

Schema 39 adds `storage_logical_outbox`. When explicitly enabled, the RPC
dispatcher runs the semantic handler and inserts its canonical logical record
inside the same SQLite/PostgreSQL transaction; a command receipt is inserted
after that callback in the same transaction. The encrypted body carries the
validated request/response plus the exact bounded DML mutation program that
the handler executed, including row-count witnesses. A clean refusal or a
natural-idempotency retry that changes no row emits nothing.
Oversize records roll back the domain mutation, and a full pending-byte budget
returns retryable `database_busy` rather than acknowledging an unrecorded
change. The sequence counter and pending-byte counter are reconciled against
the table before serving. Request and response documents are sealed before the
outbox insert with the deployment's authenticated secret envelope, bound to
event ID, tenant, and numeric owner. The table and segment contain ciphertext,
codec/key identity, and non-secret routing metadata—not a second plaintext copy
of credential/provider inputs.

After commit, `lib/storage_sidecar/logical_outbox.py` wakes one bounded
publisher. It reads a small page, releases the database connection, appends and
fsyncs through the `LogicalCommitSink` boundary, then performs a tiny ordered
ack transaction. Network/FUSE file I/O therefore never runs while the SQLite
writer or a PostgreSQL transaction is held. A crash after file fsync but before
ack is safe: retrying the same sequence and event ID compares the canonical
record and acknowledges it as a duplicate; a divergent reuse, gap, checksum,
or lineage fault poisons the publisher and fails closed.

The built-in sink is `lib/storage_sidecar/logical_shadow.py`. Its private,
single-writer segments contain a checksummed lineage header and length-prefixed
canonical semantic records with explicit tenant/user identity, command
identity, schema/operation-registry contract, and sequence. Append fsyncs before
returning; rotation uses atomic rename plus directory fsync; recovery truncates
only an incomplete tail of the sole open segment. It supports local block and
POSIX network/FUSE mounts whose permission, locking, fsync, and atomic-replace
contracts hold. `LogicalCommitSink` is the adapter boundary for an immutable
object-store implementation; no native cloud SDK or credential policy is
silently selected by this repository. An object store mounted without POSIX
semantics must use such an adapter, not masquerade as a directory.

Activation is intentionally low-mind but never implicit durability consent:

- `TOFU_STORAGE_LOGICAL_SHADOW=off` is the default and changes no write path;
- `auto` activates SQLite only with its measured/consented local write front,
  or when an absolute sink directory is explicitly supplied. PostgreSQL auto
  requires that explicit shared directory;
- `required` refuses startup when the configured topology cannot meet the
  contract. Distributed PostgreSQL requires an explicit shared directory;
- every API/worker replica captures into the shared PostgreSQL outbox, while
  only `scheduler`/`all` publishes. The sink writer lock makes another
  publisher a healthy standby rather than a second writer;
- `TOFU_STORAGE_LOGICAL_SHADOW_DIR` selects an absolute sink. Otherwise an
  eligible personal SQLite deployment uses `data/logical-commits`;
- `TOFU_STORAGE_LOGICAL_ACCESS=owner` creates mode-0700/0600 state. An
  explicitly shared service group may select `group`, which requires a
  world-inaccessible group-rwx directory and group-rw files; existing modes
  are validated and never broadened implicitly. Payloads remain encrypted in
  either mode;
- `TOFU_STORAGE_LOGICAL_OUTBOX_MAX_MIB`,
  `TOFU_STORAGE_LOGICAL_SHADOW_MAX_MIB`,
  `TOFU_STORAGE_LOGICAL_SEGMENT_MIB`,
  `TOFU_STORAGE_LOGICAL_RECORD_MAX_MIB`, and
  `TOFU_STORAGE_LOGICAL_PUBLISH_BATCH` are hard bounded. Defaults derive the
  outbox from the launch-time log budget and keep every batch/record/segment
  finite. History is never deleted merely to regain capacity.

Logical payload encryption reuses `TOFU_SECRET_ENCRYPTION_KEY`; distributed
replicas must receive the same key. Personal mode uses the existing private
mode-0600 deployment key. Back up that key independently with the same controls
as other encrypted provider state: losing it does not corrupt the authority
database, but makes logical replay impossible; replacing it while unpublished
outbox/history exists fails key-identity validation rather than guessing.

The database remains authoritative in all three modes. `logical_replay.py`
provides a concrete SQLite/PostgreSQL `BackendReplayTarget`: it authenticates
the encrypted record, requires the exact schema contract, applies its bounded
DML program, verifies every affected-row witness, and advances
`storage_logical_replay_checkpoints` in that same target transaction. Offline
shadow reads resume from that cursor in bounded pages rather than loading the
history into memory. The module also provides constant-memory ordered
projection digests, stable canary selection, and a fail-closed cutover
decision. Promotion must
advance database → shadow → canary reads → logical authority one stage at a
time and requires an explicit operator request, zero outbox backlog, equal
source/sink/replay cursors, identical source/target projection digests, a
minimum verified sample, a ready publisher, and a verified rollback
checkpoint. No runtime code performs that promotion yet. This is deliberate:
zero remote-wait commit latency and zero loss on destruction of the local
device are mutually incompatible; the existing fast-path bounded-RPO consent
continues to own that choice.

## Durable worker jobs

`storage_worker_jobs` is the shared claim authority for accepted distributed
work. `worker_job.enqueue` is owner-scoped and semantically idempotent;
`worker_job.claim_next` increments both the attempt and fencing token while
granting a 60-second lease. A claim must list the task kinds for which that
worker has a complete handler; omission never means "claim everything". A live
worker heartbeats every 20 seconds and may advance its replay cursor
monotonically.

`turn.attempt.dispatch_worker` is the only distributed conversation dispatch
boundary. In one database transaction it binds the current accepted attempt to
a deterministic task ID, enqueues one `conversation-attempt` job, and captures
the running-state sync event. Its payload is limited to durable
conversation/turn/attempt references and the explicit `PrincipalContext`; it
does not copy a conversation projection or request-local provider handles.
Lost-ACK retries resolve the same job, while a legacy `@dispatching` or other
executor binding fails closed instead of creating a second authority.

Every heartbeat, cancellation poll, and terminal settlement proves
`task_id + claim_owner + fencing_token` against an unexpired lease. Once a new
worker reclaims an expired row, the former token cannot write a terminal state.
Cancellation is durable: queued work becomes terminal immediately, while
running work must observe and acknowledge the command. Redis may wake claimers
but is never consulted for job state or correctness.

`lib/durable_worker.py` supplies the synchronous claim/heartbeat/cancel/terminal
runner ports, but registers no production task kinds. A kind is excluded from
the claim filter until its registration names durable event replay, terminal
billing/admission settlement, cooperative cancellation, and fencing for every
externally visible side effect. These declarations are an executable startup
gate, not proof by themselves.

The storage/runner foundation is intentionally not yet selected by the chat
composition root. Worker autoscaling stays disabled until a conversation
handler reconstructs the executor from durable references, all four safety
properties pass crash-injection E2E, and API admission/cancellation use this
authority end to end. The current in-process path continues to use the
one-shot attempt dispatch claim and must not be advertised as cross-Pod
takeover.

## Layout and secrets

Personal SQLite state lives under the project data root:

```text
data/
  tofu.db
  backups/
  logical-commits/       # only when logical shadow is enabled and eligible
  .storage-sidecar.lock
  .storage-sidecar-lease.json
  storage-handoff-audit.jsonl
```

The SQLite search projection is intentionally outside this durable tree under
the configured host-local temporary projection root. Backups, handoff, and
logical replay never copy it; the transactional dirty set and authority turns
are sufficient to reconstruct it.

The child Sidecar receives its random loopback endpoint and token through the
owned startup control channel. Its existing parent watcher also owns one
close-on-exec pipe from the application process; EOF releases the Sidecar and
project lease on parent death or in-place image replacement, including when the
numeric PID is unchanged. The pipe is bounded to one per child and adds no
watcher thread. Distributed Pods exchange startup metadata in a mode-0600
Pod-local memory file and remain independently managed. DSN/Redis credentials
come from mounted secret files and never appear in argv, repr, logs, or RPC
payloads.

## Retention and size bounds

- Streaming task frames: 6 hours.
- Structural request-inspector events: 30 days.
- Settled attempt replay events: 1 day in personal mode, 7 days in distributed
  mode; the permanent turn projection is unaffected.
- Conversation sync replay rows: 7 days; expired cursors require a snapshot.
- Recoverable conversation trash: 30 days.
- Tool-result artifacts: owner-scoped, content-addressed reconstructible data;
  each write declares an expiry, reads/searches reject the wrong owner and
  expired rows, and maintenance prunes expired rows in bounded batches.
- Non-terminal attempt-event payload: maximum 4 MiB.
- Storage RPC frame: maximum 64 MiB; personal-mode active RPCs adapt from 2..12
  using effective CPU and memory, while distributed mode defaults to 64.
- Transcript analytics use the owner-scoped conversation repository's
  metadata-first scan and hydrate at most four conversations per RPC. A frame
  limit response recursively splits that batch; one oversize conversation
  fails loudly instead of producing an incomplete report.
- Local conversation-search projection: launch-probed personal budget of
  128 MiB..4 GiB; two read connections, one independent writer, 8-row/2-MiB
  authority scan pages, and no authority-writer backfill work.

Retention runs in separately committed pages and reports whether backlog
remains. Permanent turn projections and active headers are never retention
targets. Command receipts and backups follow their dedicated operational
policies.

Task-event payloads use a private at-rest codec on both backends. Canonical
JSON below 64 KiB stays byte-identical; a larger event gets one deterministic
zlib level-1 attempt and stores the envelope only when it is smaller. Reads
restore the original semantic event before replay, and natural-key duplicate
checks compare the canonical decoded bytes. Stored and decoded forms are each
bounded by the 64 MiB RPC ceiling. Unsupported, truncated, corrupt, trailing,
or length-mismatched envelopes fail as `database_integrity`; existing plain
JSON rows remain readable without migration.

`tool_result_artifacts` stores only bounded tool overflow, not durable user
state or transcript authority. Application code uses the artifact repository
or `tool_result_artifact.put/read/search/prune` semantic operations with an
explicit `user_id`; references expose a CAS digest, cursor, media type, size,
and expiry but never a SQLite row or disk path. Rewriting the same content is
idempotent and cannot shorten its existing lifetime.

## Failure contract

Storage errors expose a stable code, retryability, retry delay, and operation
ID without SQL or sensitive parameters. Core codes are:

- `database_not_found`, `database_conflict`, `database_forbidden`,
  `database_busy`;
- `database_unavailable`, `database_timeout`;
- `database_integrity`, `database_protocol_error`, `database_internal`;
- `conversation_authority_conflict`, `storage_payload_too_large`;
- `plugin_storage_incompatible`.

Busy/unavailable/timeout may be retried only where the operation contract says
the write is idempotent. Integrity/protocol faults fail closed. A legacy
message-array write receives `conversation_authority_conflict`; clients must
use turn commands rather than GET/rebase/retry.

## Operations and verification

Use `scripts/storagectl.py` for preflight, status, baseline, backup, restore,
handoff, and integrity checks. Classic SQLite backup is page-wise. When the
measured-local fastpath is active, backup instead asks the sole checkpoint
owner for a deadline-bounded stable shadow generation: acknowledged commits at
the checkpoint become one standalone database image, same-filesystem targets
pin it with a hard link, and a separately mounted target receives one sequential
copy. Commits accepted during that image copy remain in the next WAL/backup.
Both paths remain capacity-checked, fully integrity-verified, checksummed,
fsynced, and atomically published; checksum reads release their scanned page
cache best-effort. PostgreSQL backup/PITR is requested through the deployment
platform. The distributed scheduler neither
registers nor executes the legacy application-managed database backup tasks.
Online and offline SQLite backup paths share one artifact policy: a job
manifest identifies each temporary copy, expired artifacts are reclaimed only
after their recorded process is dead, and publication retains two verified
recovery points by default. `TOFU_STORAGE_SQLITE_BACKUP_RETENTION`,
`TOFU_STORAGE_SQLITE_BACKUP_TEMP_TTL_SECONDS`, and
`TOFU_STORAGE_SQLITE_BACKUP_RESERVE_BYTES` may tune those bounded personal-mode
budgets. Before allocating a temporary copy, admission also requires the
estimated copy plus retained same-volume verified backups and deep-clean
rollbacks to fit `TOFU_STORAGE_RECOVERY_COPY_BUDGET_MIB`. The personal default
is half of the launch-probed data-volume capacity, clamped to 4..512 GiB; an
unknown probe falls back to 64 GiB, distributed mode defaults to 1 TiB, and an
explicit override remains hard-capped at 8 TiB. A backup directory mounted on
a different device owns a separate footprint and does not charge local
rollback points. Successful backup results expose retained, projected, limit,
and same-volume rollback bytes. Budget refusal creates or deletes nothing; the
operator can use an independent backup volume or an explicit bounded override.
One Sidecar accepts at most one full backup at a time; a concurrent request
receives a retryable `database_busy` result.

`serverctl.py doctor` derives freshness only from canonical
`storage-sqlite-*.sqlite3` artifacts whose Sidecar checksum manifest is present
and structurally complete. The retired `data/db_snapshots/` owner is never
treated as current recovery health; its published and interrupted logical byte
totals are reported separately for operator-controlled retirement after an
independent canonical backup. Diagnostics never delete those legacy artifacts.

Large bounded RPC frames can leave freed allocator arenas resident after the
handler thread exits. At the transition to zero active RPCs, the Sidecar checks
its process RSS no more than once per cooldown and calls `malloc_trim(0)` only
above `TOFU_STORAGE_IDLE_TRIM_RSS_MIB`. The personal default derives from the
same launch-time memory probe as the SQLite writer cache; distributed mode has
a separate 1 GiB default. `system.metrics.rpc` exposes cumulative
`idle_trim_attempts`, `idle_trim_successes`, `idle_trim_reclaimed_bytes`, and
the last before/after RSS values. The trim runs while new handler admission is
briefly fenced, so it cannot race an active storage transaction; unsupported
allocators fail open without changing storage semantics.

Verified-copy deep clean has a separate rollback lifecycle. After a new
authority passes integrity, row-parity and publication gates, the newest
`tofu.db.pre-compact-<stamp>` is preserved and older copies are reduced to one
by default (`TOFU_STORAGE_SQLITE_ROLLBACK_RETENTION`, hard ceiling four). The
tool proves its second-stamped candidate, rollback, WAL and SHM names absent
before the first mutation; cleanup is enabled only after that ownership gate,
so an existing file or dangling link is never overwritten or unlinked. The
read-only `--analyze` report includes logical and allocated bytes, age, excess
count, and an exact retirement command for every retained copy. The same
bounded shallow report totals verified SQLite backups and root-level files of
at least 1 GiB whose lifecycle belongs to migration/cutover owner sign-off;
those operator-managed files receive no automatic deletion command. Removing the
last local rollback is never automatic: after observing the replacement
healthy, stop Tofu and run
`python3 scripts/storage_deep_clean.py --retire-rollback <basename> --confirm`.
The command accepts no path or glob, holds the project lease, rejects links or
WAL/SHM companions, quick-checks the current authority, fsyncs the deletion,
and verifies the postcondition.

For an authority created before bounded attempt frames/retention, first run the
read-only `python3 scripts/storage_deep_clean.py --analyze`. Its
`compaction_plan` reports live/source bytes, required reserve, available space,
the bulk-compaction verdict, and missing deferred indexes. The `tables` section
measures exact encoded payload bytes and real row counts rather than multiplying
a sparse `max(rowid)` by a sample average; it also exposes rowid holes and a
64-group exact `storage_events` stream/type breakdown. These logical payload
totals exclude SQLite page/index overhead. All table measurements share one
60-second SQL progress deadline: a slow authority returns explicit
`analysis_budget_exhausted` partial results instead of continuing an unbounded
diagnostic scan. Physical compaction is intentionally offline and explicit:
`python3 scripts/storage_deep_clean.py --offline --confirm`. It acquires the
project lease, deletes only expired transport rows, verifies a compact copy,
and retains the pre-clean authority for operator-controlled rollback. Never run
the offline mode while Tofu is serving, and verify free space before the
maintenance window. While that lease is held, the lifecycle manager reports
`maintenance` with the safe owner label/PID, queues an explicit start request
without consuming the crash-loop budget, and starts Tofu automatically after
the OS lock is released. The lease stamp is diagnostic only; the OS lock stays
authoritative, so a stale `running` stamp never blocks startup. The verified
compact copy also receives every missing deferred performance index and
retires superseded indexes before building their replacement, so their pages
can be reused; established authorities never build such indexes implicitly
during startup. When `--analyze` reports only missing/obsolete deferred indexes
and no physical compaction need, use
`python3 scripts/storage_deep_clean.py --offline --no-vacuum --confirm`: it
performs current transport retention and the in-place index transition under
the same lease without allocating a second authority copy. It deliberately
skips task-event type recovery/codec backfill and does not delete frozen legacy
transport rows because either would create a large freelist without returning
physical space. If a personal disk cannot hold both the compact copy and the
old authority, first make and verify an independent backup, then use
`python3 scripts/storage_deep_clean.py --offline --low-space --confirm`. That
mode returns free pages in bounded in-place batches and verifies integrity plus
authority row counts, but intentionally cannot retain an on-volume rollback
copy.

The verified-copy and low-space modes make one rowid-keyset pass over current
`storage_events` task streams before reclaim. Explicit event types receive the
canonical 6-hour streaming or 30-day structural TTL. Blank v21 migration rows
recover `event_type` and `event_kind` from a valid top-level JSON object, after
which the same TTL applies; malformed or type-less rows remain in the longer
structural class. Retained plain payloads of at least 64 KiB receive the private
task-event codec, including already-typed historical rows. Project feed/status
streams never enter this pass. Selection is capped at 4,096 rows and 64 MiB of
stored payload, writes commit separately with a WAL checkpoint, and a repeat
pass is write-free once the cohort is classified/compressed. The report exposes
scan/write pages, TTL counts and bytes, recovered/opaque/invalid rows,
compression savings, batch maxima and retained blank rows.

The verified-copy and low-space physical modes also retire expired rows from
the frozen pre-Sidecar `attempt_events` and `task_events` tables. Legacy
attempt cleanup requires both age eligibility and a newer sequence, so the
newest diagnostic frame for every attempt always survives. Legacy task frames
use the same canonical 6-hour streaming and 30-day structural lifetimes as the
Sidecar event authority. `task_results` remains untouched. Deletes commit in
pages of at most 900 rows and 128 MiB of encoded payload, and truncate the WAL
between pages; an individual over-budget row fails closed. The report includes
deleted rows/payload bytes, batch maxima and retained-row counts for operator
verification.

For SQLite on a high-tail-latency FUSE/network volume, see
[`TRB-fastpath.md`](TRB-fastpath.md). The local-front mode preserves
`synchronous=FULL` on the serving database and activates only after filesystem,
WAL-recovery, capacity, lineage, and measured-speedup checks. It deliberately
has a bounded RPO if the local disk itself is lost; enabling it is an operator
durability decision, not an automatic response to a slow mount. First
activation uses source-fingerprinted, fsynced copy checkpoints and resumes only
owned partial bytes whose classic database/WAL identity is unchanged. The
Sidecar reports bounded startup progress to a renewable stall watchdog inside
one immutable topology-aware hard deadline; the ASGI lifespan shares that
deadline plus a fixed reserve, so no outer layer repeatedly kills valid work.
Completed 256 MiB copy ranges are fsynced before their page-cache pages are
released best-effort. The first verified shadow reuses the immutable classic
database through a same-filesystem hard link; later generations copy the stable
post-checkpoint database image sequentially and capture concurrent commits in
the new WAL prefix. Thus a large authority neither needs a second durable
database-sized allocation on first activation nor restarts page-wise backup
work whenever the live front receives a write. Full-copy rebases are triggered
by an adaptive WAL budget instead of a fixed 64 MiB: one eighth of the current
authority (at least 64 MiB), capped by one percent of launch-time free disk and
8 GiB. The local and durable WAL copies therefore remain explicitly bounded
without repeatedly rewriting a large authority under ordinary write traffic.

The storage metrics surface exports rolling commit p50/p95/max, queue depth by
lane, current writer phase, stall interrupts, group-commit counts, writer-cache
bytes, linked SQLite runtime version, fast-path activation, durable ship lag,
last-ship age, and logical capture/publisher state, pending records/bytes,
cursor, duplicate retries, and failures. Required mode revokes readiness on a
degraded, blocked, or poisoned publisher. A healthy
deployment should alert on nonzero stall-interrupt growth, persistent user-lane
queueing, p95 commit latency approaching the command deadline, or advancing
ship lag/age. It should also alert before the logical outbox or sink reaches its
byte ceiling; a full sink intentionally backpressures rather than discarding
durable history.

For a storage change, verify in this order:

1. operation unit/contract test on SQLite;
2. wrong-owner, rollback, idempotency, and timeout behavior;
3. PostgreSQL adapter contract when enabled;
4. schema migration from the previous version;
5. affected HTTP/application contract;
6. storage boundary and documentation gates;
7. representative FUSE fault/load certification for transaction changes.

Never preserve an old runtime path as rollback. Recovery restores verified data
and restarts the single authority; it does not activate a second repository.

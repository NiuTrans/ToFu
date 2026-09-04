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

The current machine versions are `storage.v1`, schema version 53, and operation
registry version 33. Code constants are authoritative; update this sentence in
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

Schema 52 stores new permanent receipts in `storage_command_receipts_v2`: a
domain-separated 32-byte SHA-256 command key, the operation name, the canonical
request SHA-256 as 32 binary bytes, the bounded response, and its commit time.
SQLite stores the fixed primary key once with `WITHOUT ROWID`; PostgreSQL uses
the same logical `BYTEA` columns on its ordinary primary-key table. One indexed
`UNION ALL` probes both formats: zero rows executes, one exact row replays, a
request mismatch is `database_conflict`, and a duplicate is `database_integrity`.
Migration creates an empty table without scanning, rewriting, or deleting legacy
rows; new writes use v2 and old rows remain replayable indefinitely. There is no
time-based pruning: exact permanent replay for arbitrary IDs and a finite set
cannot both be guaranteed. Avoiding receipts for natural-idempotent/read-only
operations plus the fixed-width identity reduces growth without weakening ACKs.

RPC accepts named operations and JSON-compatible payloads only. SQL, paths,
connections, and transaction handles have no wire representation.

Recent-project history uses the owner-scoped `project.recent.touch_many`
command when a project selection carries more than one root. One receipted
transaction accepts at most 32 non-empty paths of at most 4,096 characters,
deduplicates them, locks/updates them in stable path order for PostgreSQL, and
returns only `{touched}`. A lost-ACK replay therefore cannot increment usage
counts twice, owner scopes cannot mix, and a multi-root selection pays one RPC
and one receipt instead of one of each per root.

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
For Turn-native conversations, `messages_json` is empty and new header
`search_text` values are empty; pre-C246 headers may retain a rebuildable copy
until physical reclaim. Pre-Turn imports keep frozen, potentially non-empty
`messages_json` for compatibility. Runtime writes never replace either shape
with a conversation-sized message or aggregate-search document.

Owner-scoped `conversation.get` exposes mutually explicit full, metadata-only,
and bounded message projections. A `message_window` is limited to 1..500 and
may carry a non-negative exclusive `before_sequence`; a cursor without a
window is invalid. Active Turn transcripts page through the backend-neutral
adapter contract. Frozen pre-Turn JSON archives scan only a requested first or
last page when its estimated and observed Python work stays within 128 KiB of
code units; middle pages, count/shape ambiguity, or a larger suffix/prefix use
the authoritative full decoder and then slice. Every get/list shape omits the
header search corpus in SQL and returns an empty compatibility placeholder.

The metadata-only `conversation.list` query accepts optional bounded
`project_path` and `title_contains` filters. The Sidecar combines them with the
required owner predicate before ordering and limiting, then applies
`settings_keys` projection before the result crosses the RPC boundary. Title
matching preserves Python `str.lower()` substring semantics across SQLite and
PostgreSQL: the authority scans keyset pages of at most 512 lightweight
`id/title/updated_at` rows, stops once the requested result bound is met, and
projects full metadata only for matches. Thus Unicode casing and literal `%`
or `_` retain the caller's established behavior without returning the whole
catalog. An explicit empty `settings_keys` whitelist projects SQL constant `{}`
instead of reading or decoding the stored settings document. Project consumers
retain only the small `projectPath` witness needed to fail closed during a
mixed-version rollout.

`conversation.activity_dates` accepts an owner, bounded candidate filters, and
an explicit strictly increasing millisecond-boundary vector. It counts each
conversation at most once per `[start,end)` interval. Turn-native rows select
only the typed JSON `timestamp` scalar plus their row-time fallback in batches
of 64; PostgreSQL keeps JSON scalar types rather than coercing numeric zero to
text. Frozen pre-Turn archives load at most four stored documents per query and
decode one at a time, preserving malformed-data failure and missing-timestamp
fallback. Only the interval counts cross the storage frame.

At rest, a versioned private projection codec interns only tool-segment
``input`` / ``result.content`` values that are exactly equal to the uniquely
identified ``toolRounds`` copy. Semantic reads hydrate references before use,
so the public contract is unchanged; divergent/future shapes remain verbatim
and malformed references fail closed as ``database_integrity``. Turn-native
rows convert only on their next write; no startup rewrite competes for the
authority. Current compaction archives use the same codec when created; explicit
offline deep-clean backfills them and frozen pre-Turn arrays before giving each
message of at least 64 KiB one zlib level-1 attempt. Only a smaller result gets
a backend-neutral JSON envelope; array/message boundaries remain visible, so
budget-admitted reads hydrate only selected envelopes. Plain/projection-only
archives remain readable; 64 MiB limits and canonical round trips gate writes.

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
immediately. Legacy `storage_turn_search` and header `search_text` are neither
read authorities nor runtime inputs; physical cleanup retires verified copies.

This split follows the durable-authority/projection boundary in the vendored
Codex state runtime: state, logs, queues, and paginated history use separate
SQLite databases, and startup refuses a full `VACUUM` that would contend with
foreground writers (`codex/codex-rs/state/src/sqlite.rs`). For network-mounted
authority, Tofu additionally keeps rebuildable search host-local and rejects
automatic page relocation before it reaches the authority writer.

Header deletion/restore/clone is defined by
[`contracts/conversation_lifecycle_v1.yaml`](../contracts/conversation_lifecycle_v1.yaml):

- delete atomically moves the header and normalized turn graph to
  `storage_conversation_trash*`, removes executable state, and makes every
  active query/write observe “missing” immediately;
- restore atomically rebuilds active rows without attempts or live latches;
- clone accepts settled or generating sources and atomically freezes the latest
  durable projection into a terminal graph with remapped executable identities;
  live turns become interrupted, non-terminal tools become aborted, source
  execution continues, later source updates cannot enter, and no browser message array is uploaded;
- maintenance permanently purges trash after 30 days in bounded oldest-first
  batches.

Conversation Sync v3 owns turn commands, snapshots, and replay; see
[CONVERSATION_SYNC_V3.md](CONVERSATION_SYNC_V3.md).

Schema 51 removes the second durable encoding of every new `attempt.event`
change. `storage_attempt_events` already owns the exact AttemptEvent document,
so `storage_conversation_changes` stores `{}` plus its nullable
`(attempt_id, attempt_sequence)` reference; one owner/conversation/turn-fenced
LEFT JOIN reconstructs the unchanged public ConversationChange. NULL identifies
historical self-contained rows and requires no JSON backfill. A missing or
mismatched reference fails storage integrity. The partial reference index keeps
retention probes proportional to candidate attempts rather than the replay log.
Stopped-server tooling probes for the reference discriminator before building
retention SQL, so it can safely inspect or clean a schema-50 authority before
startup migration; once the schema-51 column exists, the reference fence is
mandatory.

## Transaction model

### SQLite

- One physical read-write connection serializes all writes; read-only pooled
  connections cannot upgrade to writers.
- Active RPC handlers stay hard-capped. The accept loop may wait at most 100 ms
  for one slot so a short scheduling burst drains through the bounded kernel
  backlog; sustained saturation receives a classified retryable rejection.
- The writer uses weighted user/event/maintenance lanes.
- The three lanes share one launch-probed waiting-job ceiling
  (`TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY`; personal 8..64, distributed
  128, hard ceiling 1,024). Saturation fails before retaining the operation
  with retryable `database_busy`. An acquisition timeout removes a job that is
  still queued, releasing its decoded request/closure immediately; a job
  already drained into a batch remains fenced by its cancellation bit.
- Backlogged compatible writes share group commits, while each logical job
  keeps its own deadline, savepoint, result, and rollback behavior. The sole
  cross-domain writer receives one launch-probed, bounded 8..64 MiB personal
  page cache (`TOFU_STORAGE_SQLITE_WRITER_CACHE_MIB`; explicit ceiling
  256 MiB); readers retain SQLite's lean cache.
- Each backend owns one reconstructible, revision-keyed live Turn projection
  cache (`TOFU_STORAGE_TURN_PROJECTION_CACHE_MIB`). Probe failure uses 16 MiB,
  the 8 GiB reference host uses 32 MiB, adaptive personal mode stops at
  128 MiB, distributed mode uses 256 MiB, and explicit values stop at 1 GiB.
  Entries are additionally capped at 256 and expire after ten idle minutes;
  charged bytes are three times canonical stored bytes to cover hydrated
  Python containers. Revision/owner/conversation/Turn/attempt mismatch,
  terminal settlement, backend close, or LRU pressure discards an entry and
  safely reloads durable authority.
- Durable task-event producers derive both waiting-object and serialized-byte
  ceilings from the same launch-probed writer budget. Probe failure resolves
  to 256 objects / 64 MiB, the 8 GiB reference profile to 512 / 64 MiB, and
  distributed mode to 4,096 / 512 MiB; explicit overrides remain hard-capped
  at 8,192 objects / 1,024 MiB. One claimed batch may exist beyond that waiting
  queue, but retains at most 500 events and 60 MiB, leaving protocol headroom
  below the 64 MiB `storage.v1` frame.
- Event serialization and frame size are validated before queue admission;
  item or byte saturation fails immediately with retryable `database_busy`.
  A confirmation/flush timeout identity-removes work not yet claimed and
  releases its payload before the natural-key direct fallback, preventing a
  later redundant batch request. Already-claimed work keeps the existing
  ambiguous-commit contract and is safely deduplicated by `(task_id,
  sequence)`. The 1 ms idle gather window still waits for commit before
  visibility and preserves concurrent-stream amortization.
- Commands use `BEGIN IMMEDIATE`, bounded acquisition, and progress-handler
  deadlines. Rollback precedes error classification. Diagnostics publish the
  current `begin` / `execute` / `commit` / `rollback` / `post_commit` phase.
- Writer commit latency and application event-persist latency retain only a
  recent sample window derived from that same waiting-job budget: 2,048 on
  probe failure, 4,096 on the 8 GiB reference profile, and 32,768 in
  distributed mode/hard maximum. Metric snapshots copy under their short
  observation lock and sort outside it, so a scrape cannot stall commits.
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
  most 16 separately committed pages of 25 rows; a remaining backlog is
  revisited in 30 seconds, while a drained backlog is probed only every five
  minutes. The
  mutually exclusive, age-only partial indexes make both an empty probe and
  `ORDER BY created_at_ms LIMIT` range-bounded without scanning the other tier.
  Streaming backlog drains before the longer-lived structural
  tier, so one maintenance cycle cannot stack both workloads. An established
  SQLite authority missing the v2 age-leading partial index does **not** use
  the v1 `(stream_kind, event_type, created_at_ms)` compatibility index while
  serving. A deleted-row limit cannot bound how long that legacy search holds
  SQLite's sole writer on a multi-GiB event table. The semantic prune therefore
  returns `legacy_index_offline_required` before any scan, the maintenance
  owner disables that tier for the process, and the explicit offline
  deep-clean/index workflow below owns legacy classification and transition.
  An authority with neither index similarly returns `missing_index`. All
  online retention and reclamation work shares a process-lifetime circuit:
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

## Durable turn-source queue

`storage_queue_items` is the owner-scoped authority for human, Goal,
peer, and workflow turn sources waiting behind a conversation task. Dequeue
leases rather than deletes a source; only accepted Turn/attempt creation may
finalize it. Normal 60-second maintenance calls
`queue.conversations.list_all` with the additive
`tofu.queue.reap-probe/v1` selector. One read-pool grouped query returns the
existing oldest-first conversation list plus whether any non-autopilot lease
is strictly expired. The application enters the `queue.reap` writer
transaction only after that exact capability echo reports useful repair, then
uses the same list for bounded dispatch. A clean tick is therefore one read
RPC and no writer admission, mutation, or receipt. Old Sidecars return the
legacy bare list and retain the former writer repair; process-start recovery
always force-reclaims predecessor leases before dispatch. Lease taking and the
live-task guard remain the race authority, so read-first maintenance cannot
create a second consumer.

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

## Durable swarm recovery

Swarm startup recovery reads only sessions in the ordinary `running` or
`terminated` lifecycle states. A legacy resumable row without a positive
owner is default-denied and moved by the dedicated
`swarm.session.quarantine_ownerless` command to
`quarantined:ownerless`. The command re-checks the durable owner inside its
transaction, so a concurrent repair wins. Quarantine preserves the session and
all child checkpoints as evidence while excluding their potentially large
message histories at the recovery query boundary; it neither invents personal
owner `1` nor repeats the same invalid work and startup error on every boot.

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
  mode; the permanent turn projection is unaffected. A retained Conversation
  Sync reference temporarily extends the exact AttemptEvent source through the
  sync replay window. Sync pruning releases that fence, and the next bounded
  attempt prune reclaims the stream. Sync pruning deletes up to 256 composite
  keys per statement rather than paying one statement per change. Explicit
  turn deletion/compaction instead
  expires the affected conversation-change prefix before deleting its events,
  so an old cursor requests a snapshot rather than resolving a dangling row.
- Per-generation-attempt timing traces are durable user state, not replay
  transport. Schema 46 owns the bounded document in
  `storage_generation_attempts.timing_trace_json`; settlement freezes server
  spans and user-visible phase history there and mirrors the terminal snapshot
  into the current `projection.timingTrace`. Owner-scoped
  `turn.perception.record` updates only the small attempt document under the
  terminal-event lock, so it causes no large Turn rewrite, conversation revision,
  sync row, or command-receipt growth. Event pruning and reclaim never delete
  that snapshot, and a later attempt cannot overwrite an older task's evidence.
  One trace is capped at 256 spans, 128 gaps, 128 prompt rows, 64 client receipts,
  and 96 KiB with explicit dropped counters.
- Schema 47 adds the partial
  `(conversation_id, created_at DESC, attempt_id DESC)` discovery index only for
  attempts with a non-empty task ID. `turn.timing_trace.list` selects no trace
  JSON or content, filters by the joined Turn owner before `LIMIT`, and returns
  at most 100 compact rows plus `has_more`. Thus Request Inspector paging is
  proportional to the requested conversation page rather than a global
  `task_results` scan; index growth is exactly one derived entry per
  externally addressable generation attempt.
- Conversation sync replay rows: 7 days; expired cursors require a snapshot.

Generic cold chat replay uses compact semantic reads, not raw records.
`task_results.replay_get` owner-filters before projection; its private per-field
codec leaves outer lifecycle facts visible and selectively hydrates metadata/error
plus explicitly requested terminal content/thinking, never segments/tool rounds.
`event.bounds` returns exact cursors without event bodies, which still come from
bounded `event.list` pages. Routes own neither JSON decoding nor SQL;
missing/foreign task results are indistinguishable, and no codec envelope crosses
a public read boundary.

Task-result writes remain receipt-free, version-witnessed snapshots. The
additive `tofu.task-results.checkpoint.guard/v1` mode first takes the same
owner-qualified conversation lifecycle lock used by delete/purge, validates
the parent in that transaction, then takes the task-result key lock and
enforces task ownership, terminal regression, abort-tombstone preservation,
identical replay, and CAS. A missing parent, foreign key collision, or proven
status fence returns one indistinguishable `owned:false` result without
mutating authority. Only an exact guard echo lets a new manager cache the
returned version and collapse later checkpoints from three RPCs to one; old
managers retain the unguarded operation, and a new manager talking to an old
Sidecar retains both compatibility reads. Reconstructible `running` snapshots
get one 500 ms maintenance-priority writer admission. Pending birth and
terminal snapshots keep user priority and five bounded attempts; admission or
CAS pressure is never reported as an ownership fence.

The independent additive
`tofu.task-results.checkpoint.cache-settings/v1` capability accepts at most the
two positive bounded facts carried in the task snapshot and commits them to
the owner-qualified conversation settings in that same transaction. The
prefix HWM is a monotonic maximum; last-turn cache read is LWW for a new task
write. On identical replay after an ambiguous acknowledgement, the operation
may repair/increase HWM but preserves a different last-read value because a
newer task may already own it. The response echoes authoritative values and a
commit bit. A manager clears its staged candidates only after the exact guard
and cache capability echoes; old Sidecars fall back to the serialized per-fact
settings operations. This fusion adds no schema or command receipt.

- Sidecar rate-limit events: every row carries its originating 1-second..7-day
  window as an exact expiry; each later check removes at most 256 globally
  expired rows through the age index, so one-shot identities are reclaimed
  without a table scan. Schema v43 resets only legacy rows that had no TTL.
- Recoverable conversation trash: 30 days.
- Tool-result artifacts: owner-scoped, content-addressed reconstructible data;
  each write declares an expiry, reads/searches reject the wrong owner and
  expired rows, and maintenance prunes expired rows in bounded batches.
- Non-terminal attempt-event payload: maximum 4 MiB.
- Storage RPC frame: maximum 64 MiB; personal-mode active RPCs adapt from 2..12
  using effective CPU and memory, while distributed mode defaults to 64. A
  separate serialized-frame budget is resolved independently per Sidecar and
  client process: 128..512 MiB in personal mode (128 MiB at the 8 GiB
  reference and on probe failure) and 1 GiB in distributed mode, with an
  explicit 128 MiB..8 GiB range.
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

Provider raw archives are a separate durable, owner-scoped authority in
`storage_raw_archives`; they do not inherit task-event TTL. The provider
transport captures the final protocol-specific request body and raw response
bytes, excludes headers, applies the shared secret redactor, and compresses
request and response independently. All rows for one generation Attempt share
a 16 MiB compressed ceiling. The global ceiling is 1% of launch-probed free
space capped at 4 GiB (256 MiB when the probe is unknown), and each commit also
preserves `TOFU_STORAGE_MIN_FREE_BYTES`. Saturation inserts metadata with
`integrity=partial` and `truncationReason=quota_exhausted`; it never evicts an
older archive. Reads are owner/task scoped and return at most 1 MiB per chunk.
Explicit Turn/conversation deletion removes owned archives; maintenance has no
TTL or implicit archive-prune operation. SQLite and PostgreSQL use the same
semantic operations and byte accounting.

`tool_result_artifacts` stores only bounded tool overflow, not durable user
state or transcript authority. Application code uses the artifact repository
or `tool_result_artifact.put/read/search/prune` semantic operations with an
explicit `user_id`; references expose a CAS digest, cursor, media type, size,
and expiry but never a SQLite row or disk path. Rewriting the same content is
idempotent and cannot shorten its existing lifetime.

## Failure contract

Storage errors expose a stable code, retryability, retry delay, and operation
ID without SQL or sensitive parameters. A capacity response produced before
the server reads or dispatches a request may additionally carry the literal
boolean `request_not_dispatched=true`; absence, false, strings, transport EOF,
timeouts, and backend errors all mean execution state is unknown. Core codes
are:

- `database_not_found`, `database_conflict`, `database_forbidden`,
  `database_busy`;
- `database_unavailable`, `database_timeout`;
- `database_integrity`, `database_protocol_error`, `database_internal`;
- `conversation_authority_conflict`, `storage_payload_too_large`;
- `plugin_storage_incompatible`.

Busy/unavailable/timeout may be retried only where the operation contract says
the write is idempotent. The synchronous client grants commands its existing
three-attempt ceiling only when every failed attempt carries the literal
pre-dispatch proof; it assigns a new request ID, preserves the command ID and
payload, obeys `retry_after_ms`, and otherwise raises after the first ambiguous
failure. This lets a short Sidecar handler-capacity burst settle without ever
replaying a possibly executed mutation. Local client counters and Prometheus
expose retries and retry-bound exhaustion. Integrity/protocol faults fail
closed. A legacy message-array write receives
`conversation_authority_conflict`; clients must use turn commands rather than
GET/rebase/retry.

## Operations and verification

`paper.library.identity` is an owner-scoped query with an additive
`max_text_chars` projection. Omission preserves the rolling full-text result;
zero returns title/arXiv identity plus the authoritative text length without
copying the text, and positive values use a backend-portable `substr` before
the result crosses storage.v1. The hard maximum is the Q&A request-source
budget of 1,000,000 characters. Report title/Insight consumers request zero,
Podcast fallback requests 40,000, a hash-only report start requests 120,000
only after live/cache fast paths miss, and a cold hash-only Q&A start requests
at most 1,000,000. A caller may set `include_text_length=false` only beside
`max_text_chars=0`; this existence projection selects no source and does not
evaluate SQL `length(parsed_text)`. Repeat Q&A starts retain a launch-probed
600-second TTL/LRU working set keyed by explicit `(user_id, paper_hash)`; each
hit repeats that owner-existence lookup. Deletion or another tenant's matching
hash therefore cannot revive cached text, while the content hash itself is the
source revision. The owner predicate precedes ordering and limit; ordinary
bounded projections still report `parsed_text_length` so truncation remains
observable.

Use `scripts/storagectl.py` for preflight, status, baseline, backup, restore,
handoff, and integrity checks. Classic SQLite backup is page-wise. When the
measured-local fastpath is active, backup instead asks the sole checkpoint
owner for a deadline-bounded stable shadow generation: acknowledged commits at
the checkpoint become one standalone database image, same-filesystem targets
pin it with a hard link (on same-directory-link filesystems such as BeeGFS, by
linking beside the source and atomically renaming the second name across
directories), and a separately mounted target receives one sequential copy. Commits accepted during that image copy remain in the next WAL/backup.
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

Fastpath may refresh a verified recovery point when the pre-publication peak
exceeds that copy budget but the post-rotation set fits. This exception is
zero-copy only: capacity planning selects older verified backups, the shipper
must hard-link the checkpointed shadow on the same filesystem, and the new
image must pass integrity, checksum, fsync, atomic publication, and manifest
durability before any selected old backup is retired. Cross-device copy
fallback is refused in this mode. The newest point is always preserved,
deep-clean rollback artifacts are never rotation candidates, and even the
newest point plus retained rollback must fit the hard budget. Successful
results expose both peak/projected bytes and whether budget rotation occurred;
configured backup retention remains the target when the total recovery
footprint allows it.

Fastpath replacement images fsync a resumable prefix every 256 MiB. Its private
state binds the authority UUID, prior shadow generation, and bounded source
fingerprint; timeout, shutdown, or process loss therefore redoes less than one
checkpoint instead of the whole database. A changed source invalidates the
prefix and restarts from zero. The concurrent WAL prefix is always recopied,
and the previously published snapshot/WAL pair remains untouched until the new
image and WAL prefix are both ready for publication. A resumed image retains
the first checkpoint's `recovery_point_at`: canonical backup manifests carry
that boundary, and `serverctl.py doctor` measures freshness from it rather than
the later artifact mtime. `snapshot_progress_bytes`,
`snapshot_resume_count`, and `snapshot_resumed_bytes` expose this lifecycle. The
backup deadline used by the scheduler, startup cutovers, and maintenance CLI
is derived once from the same recovery-copy budget (30 minutes..6 hours); an
explicit `TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS` remains bounded to 24
hours. Hypercorn's outer lifespan deadline is the larger of the Sidecar seed
and full-backup deadlines plus a fixed 60-second reserve, so it cannot cancel a
valid startup backup before the storage operation's own bounded deadline.
Before checkpointing, the shadow volume must fit the complete image plus
the current WAL and reserve; publication rechecks the pair after concurrent WAL
growth. Only the validated, actually allocated durable resume prefix counts as
reusable capacity, and a short WAL read refuses publication.

`serverctl.py doctor` derives freshness only from canonical
`storage-sqlite-*.sqlite3` artifacts whose Sidecar checksum manifest is present
and structurally complete. Retired `data/db_snapshots/` artifacts never count
as current recovery health; their published/interrupted logical and allocated
totals remain explicit. Published, ambiguous, fresh, or live-owner files always
require operator control. Before capacity admission, the next canonical backup
scans at most 256 entries and removes only an expired, dead-PID temporary with the exact retired timestamp/PID/UUID name plus safe regular companions.

Serialized frame allocation has two independent bounds. The four-byte fixed
header remains bounded by active RPC count; the weighted budget below counts
JSON body bytes. The decoder reserves the declared request bytes before
allocating and receives into one exact `bytearray`; the old fragmented
chunk-list plus `join` retained a second full
copy at the join boundary. In a frozen local benchmark of a 46 MiB frame split
into 64 KiB receives (seven untraced timing trials), the retained path reduced
traced Python peak allocation from 92.09 to 46.00 MiB and median receive time
from 30.31 to 22.27 ms. Before response encoding, each handler reserves the
maximum 64 MiB frame, then releases it after the loopback send. Both directions
share `TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB`; completed responses use their own
FIFO ahead of new request bodies so resource pressure does not preferentially
discard already-committed command results. Waits are bounded to five seconds.
Every application/worker process independently resolves the same numeric
profile for its shared `StorageClient` response decoder. After reading the
four-byte header, it reserves the declared body through receive and JSON decode,
then releases before returning the semantic result to its caller. Command
responses use the priority FIFO; if the client-side wait expires, a read may use
the existing retry loop, while a command remains a single ambiguous attempt.
The budget is per process rather than a host-wide aggregate.
On the 8 GiB reference profile this changes the serialized-buffer envelope
from the slot-only 8 × 64 = 512 MiB to 128 MiB; the measured high-capacity
launch profile resolves from 12 × 64 = 768 MiB to 512 MiB, and distributed mode
from 4 GiB to 1 GiB. These are enforced admitted frame-body envelopes; one
response serialization attempt can still exceed 64 MiB before `encode_frame`
rejects it, so operation-level result limits remain authoritative. They are not
claims about the decoded Python result graph, SQLite pages, kernel socket
buffers, or a deployed RSS saving. `system.metrics.rpc` and Prometheus expose
current/capacity/peak reservations, wait/rejection/admitted totals, and
observed request/response byte totals and maxima. Client-process Prometheus
series expose the same response fields. The shared admission hot path (acquire, observe,
release) measured 1.969 µs best / 2.016 µs median over five 500,000-iteration
loops; that is about 0.14% of the previously measured 1.41 ms median physical
commit acknowledgement, not an end-to-end query-latency claim. It adds one
condition object and integer counters per process, with no worker thread.

Large bounded RPC frames can leave freed allocator arenas resident after the
handler thread exits. At the transition to zero active RPCs, the Sidecar checks
its process RSS no more than once per cooldown and calls `malloc_trim(0)` only
above `TOFU_STORAGE_IDLE_TRIM_RSS_MIB`. The personal default derives from the
same launch-time memory probe as the SQLite writer cache and is capped at
384 MiB so a high-memory host does not retain hundreds of MiB of already-free
arenas; distributed mode has a separate 1 GiB default. Personal mode checks at
most once per 60 seconds, while distributed mode retains 300 seconds. A frozen
13-trim busy-generation window (2026-08-29 00:04..01:04) had a 826.6 MiB median
pre-trim RSS, 304.6 MiB median post-trim RSS, 439.5 MiB median reclaimed, and a
920.6 MiB maximum. At equal allocation rate, the shorter policy projects about
409 MiB median pre-trim RSS; this is a pre-deployment projection, not a measured
saving. `system.metrics.rpc` and Prometheus expose attempts, successes,
cumulative reclaimed bytes, last before/after RSS, cumulative/last trim
duration, RPC active/waiting/capacity/rejections, and process RSS. The trim runs
while new handler admission is briefly fenced, so it cannot race an active
storage transaction; unsupported allocators fail open without changing storage
semantics. Explicit cooldowns remain bounded to 30..3,600 seconds.

`turn.event.record` compares the decoded canonical projection with the current
turn row inside its existing transaction. When a full-fold frame is equal, its
UPDATE omits `projection_json`; when a slim frame leaves both cumulative text
fields equal, it likewise omits the SQLite `json_set` / PostgreSQL `jsonb_set`
assignment. The projection-revision CAS, status and settlement changes,
attempt-event append, optional carried task event, conversation revision,
ordered sync change, and terminal search projection remain mandatory. Thus an
empty patch still advances every replay cursor exactly as before, but does not
dirty an unchanged large value. `system.metrics.attempt_events.by_type` exposes
`projection_blob_write_skips` and
`projection_blob_write_skipped_bytes`; the byte counter is the canonical
stored value excluded from the assignment, not a claim about physical WAL
bytes. In a read-only active-authority audit, 6,887 of 13,878 boot-window
frames had empty patches and carried 3,923,713,036 bytes of conservative
projection evidence; 6,111 known full-fold frames accounted for
3,893,916,098 bytes. Actual WAL reduction must be measured after deployment.

Full-fold application commands use that same versioned projection-patch
primitive instead of transporting the cumulative Turn twice (once as the
command projection and again inside its event envelope). The Sidecar locks the
attempt/Turn, requires `baseRevision` to equal the stored revision and
`targetRevision = base + 1`, normalizes the public stable-segment base, applies
the patch copy-on-write, and then owns the full-row encode plus canonical replay
patch. A malformed path/version fails closed; a stale base refreshes and
rebuilds the application event once, while repeated contention is rejected.
Each live task serializes this lane with one local lock and retains exactly one
last-applied public projection/revision, so after the first authority read a
long turn does not re-download its whole projection for every structural event.
Pure coalesced progress still performs the small attempt-status read because it
does not enter a write transaction; stale executors therefore keep the exact
visibility fence. The baseline is reconstructible and joins terminal heavy
state release. On the read-only 955-tool Turn from `mtdx825fjmhmx5`, the stable
projection is 6,843,583 bytes; an appended `tool_start` patch is 301 bytes, and
the old duplicated command shape is 13,687,938 bytes versus 695 bytes for the
new command (-99.99492%). Patch construction measured 0.338 ms median locally.
These are serialization/transport proxies, not deployed WAL, RSS, API-billing,
or end-to-end latency measurements; the Sidecar still rewrites a changed full
projection row.

The Sidecar write lane also reuses the exact-revision public projection after
the first event. A cache hit selects only Turn metadata, applies the validated
incoming patch copy-on-write, and reuses that patch for replay instead of
reading/decoding the projection BLOB, stabilizing every segment again, and
diffing the two full documents. The authenticated application marks a target as
stable only after the shared normalizer; missing private evidence conservatively
marks it for one repair at the next structural patch. Slim text or status
changes do the same. Rollback or an external revision advance produces a miss,
never a stale read. On the same read-only
955-tool Turn, the encoded BLOB is 5,235,567 bytes and its measured retained
Python baseline is 10,583,620 bytes (2.021x), so the cache charges 15,706,701
bytes (3x). A local old-vs-hit simulation that kept the still-required full
encode changed median processing from 51.985 to 26.407 ms (-49.20%, 1.97x)
and incremental traced peak from 70.269 to 8.635 MiB (-87.71%), while avoiding
one 5,235,567-byte database value read per hit. These are local CPU/allocation
proxies; they do not include the durable-write change below.

Changed live structural frames use a vertically isolated checkpoint plus a
bounded durable patch head. `storage_turn_projection_checkpoints` owns one full,
owner/conversation/attempt/revision-fenced baseline; while it is active, the hot
`storage_conversation_turns` row holds `{}` plus checkpoint/materialized
revisions and exact patch count/bytes. The patches are not duplicated: they are
the already-atomic `storage_attempt_events` replay payloads. A head is limited to
64 patches and 1 MiB of encoded patches. Crossing either limit, or requiring
stable-segment repair, writes one new checkpoint and clears the head. A live
inline projection above 64 KiB is externalized once on its next event. Terminal
settlement, restart recovery, direct edit/branch/compaction writes, and
recoverable deletion materialize the complete projection, clear all head
metadata, and delete the checkpoint. Activity-date reads join the checkpoint
and fold a head only when present; clone and public Turn reads use the same
fail-closed projection loader. Event retention excludes any attempt still
referenced by a checkpoint or head. Restart recovery budgets each checkpoint
from its declared encoded bytes with a conservative hydration multiplier,
without selecting every candidate checkpoint BLOB before choosing the bounded
transaction chunk.

Every revision after externalization remains reconstructible even when its
projection is byte-identical: the first such revision starts a head with the
already-carried empty patch instead of advancing beyond the revision-fenced
checkpoint. This keeps unchanged status/heartbeat frames on the same exact
chain invariant as structural changes without rewriting the checkpoint BLOB.

Schema 48 introduced the bounded head counters; schema 49 added the checkpoint
revision discriminator, while the latest table catalogue creates the checkpoint
table empty. Neither migration decodes or backfills historical projection
values. Fresh authorities fold the bounded event range through the existing
`(attempt_id, sequence)` primary key. Authorities that already applied the
published schema-48 projection-chain index may retain it; schema 49 does not
rebuild or require that index. Metrics expose checkpoint materializations and
bytes, one-time inline bytes released, BLOB skips, and deferred BLOB bytes.
Schema 50 repairs the exact schema-49 one-revision/no-head cohort by advancing
the checkpoint row and Turn discriminator together. It matches owner,
conversation, attempt, and revision metadata and neither selects nor rewrites
the checkpoint JSON; any other malformed shape remains a fail-closed integrity
error.
Replay `projectionBytes` remains event evidence: it is exact when a checkpoint
or materialized target is encoded, text-only for a slim frame, and conservative
baseline-plus-patch evidence for a deferred structural target. It is never
physical-row or WAL byte accounting.

On the same read-only 955-tool projection, a local temporary-SQLite
per-transaction proxy compared a 5,235,877-byte full target write with a
414-byte exact patch / 519-byte event. Median transaction time was
33.300 ms versus 0.284 ms (-99.15%, 117.35x); WAL was 5,294,232 bytes / 1,285
frames versus 12,392 bytes / 3 frames (-99.77%); incremental full-target encode
peak was 9,037,773 bytes versus 2,706 bytes (-99.97%). The bounded rollover and
terminal paths still pay one full materialization. These figures are local
storage/encoding proxies, not deployed RSS, disk, API-billing, or end-to-end
latency savings.

`turn.image.get` is a read-only compatibility query for historical inline Turn
images. It requires explicit owner, conversation, Turn, positive projection
revision, and an image index below the 20-item ceiling. Missing/foreign or
non-terminal rows return no result; a revision mismatch returns only the
current revision, and a match returns one bounded encoded image plus declared
MIME instead of exporting the full projection over RPC. The conversation-sync
application boundary then strictly decodes at most 8 MiB and sniffs the true
PNG/JPEG/GIF/WebP MIME before HTTP delivery. The operation never rewrites the
Turn, advances a cursor, or makes a compatibility URL into storage authority.
Reference snapshots prefer an existing canonical `/api/images/` upload URL and
replace any stale revision-fenced preview request-locally; only inline-only
historical images use `turn.image.get`.

Verified-copy deep clean has a separate rollback lifecycle. After a new
authority passes integrity, row-parity and publication gates, the newest
`tofu.db.pre-compact-<stamp>` is preserved and older copies are reduced to one
by default (`TOFU_STORAGE_SQLITE_ROLLBACK_RETENTION`, hard ceiling four). The
tool proves its second-stamped candidate, rollback, WAL and SHM names absent
before the first mutation; cleanup is enabled only after that ownership gate,
so an existing file or dangling link is never overwritten or unlinked. The
read-only `--analyze` report includes logical and allocated bytes, age, excess
count, and an exact retirement command for every retained copy. The same
bounded report totals verified SQLite backups, root files of at least 1 GiB,
and one level (256 entries per directory) of known retired `db_snapshots`,
`pg_backups`, and `retired_migration_artifacts-*` owners. Operator-owned rows
receive no deletion command. The last local rollback is never automatic: after observing the replacement
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
a sparse `max(rowid)` by a sample average; it exposes rowid holes, exact large
Turn-codec source candidates, and a 64-group event breakdown. The same canonical
selectors used by offline deletion report exact settled Sidecar
`storage_attempt_events`, legacy `attempt_events`, and streaming/structural
`task_events` rows plus encoded bytes for the requested `--ttl-days`; invalid
horizons fail before opening the authority. These logical
payload totals exclude SQLite page/index overhead. All measurements share one
60-second SQL progress deadline: a slow authority returns explicit
`analysis_budget_exhausted` partial results instead of continuing an unbounded
diagnostic scan. `offline_compaction_recommended` describes existing freelist
pressure only; `offline_maintenance` combines that signal with exact expired
transport, rebuildable header-search, conditional legacy-mirror payload,
deferred indexes, copy capacity, and one stopped-server command. A blocked
verified copy returns no command rather than choosing no-rollback low-space. Physical
compaction is intentionally offline and explicit:
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
structural class. Retained `round_usage` rows reuse the canonical persistence
projector: every private `_wire_*` usage graph is removed while unknown public
fields remain forward-compatible, including recovered blank rows and payloads
below the codec threshold. Other payloads use the private codec at 64 KiB; project feed/status streams never enter this pass.
Selection is capped at 4,096 rows and 64 MiB; writes checkpoint separately and
a repeat pass is write-free. The report exposes TTL/type/projection/codec rows,
decoded projection input/output/removed bytes, invalid rows and batch maxima.

New durable `messages_snapshot` rows use private `snapshotDeltaVersion=2`.
The persistence-only projector keeps one chronological message baseline per
`(task, turn)`, so a request identical to the preceding post-tool state stores
an empty tail instead of repeating it in a separate kind chain. Live SSE and
Request Inspector payloads remain full; server-side rebuild removes the marker
and restores kind-specific rows exactly. Unmarked v1 deltas keep their frozen
`(task, turn, kind)` interpretation, exact v1 rows may seed a following v2
baseline, and unsupported versions report degradation. Projector state remains
bounded to 64 active tasks and is released at terminal events. On 1,564 frames
read from the active fastpath authority, exact replay plus the production codec
projected 32,728,847 stored bytes to 22,281,348 (31.92%) and 56 resident v1
baseline chains to 32 v2 chains. This bounds future growth only; historical
rows retain the structural TTL/offline reclaim lifecycle above.

Those physical modes make metadata-first keyset passes over inactive inline Turn
projections, frozen headers, compaction archives, and large task-result records.
Turn rows require inactive checkpoint/head columns before reusing the production codec; header canonical proof may also clear its rebuildable search copy.
Current compaction archives adopt the per-message codec only when smaller; the
exact retired generic shape migrates only after unique active/trash ownership and
full public target agreement. Large task-result documents reuse the runtime
32 KiB per-field codec while outer owner/status/experiment facts remain visible.
Every pass proves canonical public equality and strictly smaller stored bytes;
malformed, conflicting, ambiguous, or over-64-MiB evidence stays intact.
Selection caps at 64 rows/64 MiB with per-page WAL checkpoints; `--no-vacuum` skips every physical rewrite.

The verified-copy and low-space physical modes also retire expired rows from
the frozen pre-Sidecar `attempt_events` and `task_events` tables. Legacy
attempt cleanup requires both age eligibility and a newer sequence, so the
newest diagnostic frame for every attempt always survives. Legacy task frames
use the same canonical 6-hour streaming and 30-day structural lifetimes as the
Sidecar event authority. Task results are never deleted. Deletes commit in
pages of at most 900 rows and 128 MiB of encoded payload, and truncate the WAL
between pages; an individual over-budget row fails closed. The report includes
deleted rows/payload bytes, batch maxima and retained-row counts for operator
verification.

The read-only report also inventories the frozen pre-Sidecar conversation
mirror family: `conversations`, `conversation_messages`, and
`conversation_turns`. These tables are not runtime authorities, but they are
never removed by the ordinary command above. When the report offers the exact
command, an operator may add `--retire-legacy-conversation-mirrors` to an
offline physical-reclaim pass. Retirement is owner-scoped and per
conversation: the frozen message array, the ordered row mirror reconstructed
from its lossless `meta` plus versioned translation overlay, and the current
`storage_conversations.messages_json` archive must have identical canonical
JSON. A missing current header, ambiguous legacy global ID, malformed or
unequal witness, legacy Turn foreign key, document above 64 MiB, or deletion
cohort above the 128 MiB payload budget retains that conversation unchanged.
Selection is capped at 64
conversations and every successful batch commits and truncates the WAL
separately. The current Sidecar archive is never modified, and integrity plus
active-authority row parity still gate publication. Here, "never modified"
describes the mirror-retirement proof/deletion itself; the independent
lossless archive-codec pass above may change only its private physical
representation. `--no-vacuum` rejects the
flag because deleting mirrors without returning disk space has no user-visible
value; low-space mode requires the same explicit independent-backup contract as
its other in-place mutations.

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
by an adaptive WAL budget instead of a fixed 64 MiB: one quarter of the current
authority (at least 64 MiB), capped per WAL by two percent of launch-time free
disk and 16 GiB. Shipper construction rechecks both the local-front and durable
shadow filesystems and takes the smaller two-percent ceiling; probe failure
keeps the already-bounded launch value. The two WAL copies therefore remain
inside a four-percent hard envelope while halving database-sized rewrite
frequency versus the prior one-eighth/eight-GiB policy. A rebase starts at
15/16 of that budget, reserving one sixteenth for commits that race the raw
checkpoint. Each physical commit observes the local WAL. Only once it reaches
the hard watermark does the fair writer reject later jobs before `BEGIN` with
retryable `database_busy`; the shipper's raw checkpoint bypasses the fence,
truncates the WAL, and reopens admission. One
already-started, deadline- and request-bounded commit segment remains atomic,
so this is an admission watermark rather than a byte-exact filesystem quota.
The fence also remains active when pre-checkpoint or publication capacity is
unavailable, preventing an unbounded local recovery tail while reads and the
previous durable recovery point remain available. Publication still repeats
the complete image-plus-tail capacity check. Sidecar and Prometheus metrics
expose complete generations, physical database/WAL copy bytes, current copy
progress, proactive rebase trigger, hard WAL budget/headroom, pressure
state/activations/rejections, and the resolved limits so write amplification
and write pressure stay measurable.
A failed runtime WAL-size observation fails closed and has its own counter; it
cannot silently report zero bytes and reopen admission.

After backend startup, the process-held project lease publishes a
credential-free `tofu.storage-locator/v1` record: backend identity, the active
SQLite authority path, configured path, fastpath flag, and shadow directory.
It never contains the private RPC token, DSN, or password. Read-only offline
diagnostics resolve this locator first, retain a bounded `/proc` open-file
compatibility path for pre-locator processes, then require matching local and
shadow authority UUIDs. If a shadow exists but no unique front is provable,
they refuse the classic file instead of presenting stale state as current.

The storage metrics surface exports rolling commit p50/p95/max, queue depth by
lane, current writer phase, stall interrupts, group-commit counts, writer-cache
bytes, per-attempt-event accepted/rejected payload budgets and unchanged
projection-write skips, linked SQLite runtime version, fast-path activation,
durable ship lag, last-ship age, and logical capture/publisher state, pending
records/bytes, cursor, duplicate retries, and failures. Fastpath also exports
rebase/pressure state, local WAL bytes, remaining admission headroom, and both
writer- and shipper-side pressure refusals. Required mode revokes
readiness on a degraded, blocked, or poisoned publisher. A healthy
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

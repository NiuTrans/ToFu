# Storage authority

Responsibility: the current durable-data boundary. This document describes the
running system; Git history contains migration plans and past measurements. Local port admission and recovery follow [proxy runtime](PROXY_RUNTIME.md#server-connection-reclamation-and-local-port-recovery).

## One authority

Foreground native RPC admission is FIFO at both the Python session pool and
the Rust authority mutex. Only the head native waiter polls the mutex; others
park and retain deadline/cancellation checks. Health-lane lock acquisition,
session handshake and pool acquisition share the attempt deadline. A request whose budget expired before dispatch reports an explicit
timeout without dispatch. Existing receipt/retry authority and frame budgets remain unchanged. This prevents a large pool from turning
slow volume I/O into a mutex polling storm; adding connections alone does not. Session lifecycle: [native sessions](STORAGE_NATIVE_SESSIONS.md).
Retryable command backpressure uses the existing aggregate deadline with capped
exponential backoff and stable receipt identity. Sixteen fast retries are not a
second, shorter timeout; persistent overload still returns a classified error. `StorageClient.request_timeout_seconds` and its native counterpart expose the already configured per-request timeout without opening a session; embedded transport inherits the native value. Native Goal recovery reads this policy when no explicit I/O override is supplied, independently of terminal ACK waiting.

Boot recovery freezes its attempt creation cutoff and drains bounded
`turn.recover` transactions until no eligible work remains. Positive progress
and a strictly decreasing remainder are required; failure is retryable and explicit. A fixed round cap previously stranded attempts beyond 512 even
when each transaction succeeded. New live tasks and excluded registry tasks
stay outside the sweep. Recovery settles interruptions and never starts paid
model work. Verification: `tests/turn_lifecycle/test_large_restart_recovery.py`
and the native concurrency benchmark's `--recover-interrupted` mode.

Application calls use `StorageClient`; one selected authority owns durable state:

```text
route / application service / worker
  -> owner-scoped repository or semantic operation
  -> StorageClient query | command            (lib/storage/ — client facade)
  -> selected authority (exclusive process lease)
     -> native storage.v2 -> Rust TofuDB      (packages/tofu-db/ — native authority)
     OR storage.v1 -> lib/storage_authority_sql/ -> SQLite / PostgreSQL  (SQL authority)
```

`TOFU_PERSONAL_STORAGE_BACKEND=tofudb` selects the native authority. Preserve
that selection and its existing data: a failure is repaired within the selected
backend, never by silently opening an old SQLite file or creating another store.
The native direction remains supported; SQL backends remain separate explicit deployment choices.
Drivers, physical paths, schema work and SQL stay inside the selected implementation; there is no in-process fallback or runtime store switching. A missing or unhealthy selected authority revokes readiness.
Linux lifecycle-managed source deployments retain the selected SQL Sidecar in
the same manager-owned resident host used for native daemons. API detach only
closes its client; the authority and its crash budget remain alive. SQL keeps
the authenticated v1 connection handoff, native keeps v2, and the distributed
preview fence is unchanged. See [resident storage lifecycle](NATIVE_STORAGE_LIFECYCLE.md).
See [native storage](STORAGE_TOFUDB.md) for its launch and certification contract.
With the native authority selected, `TOFUDB_TRANSPORT` (default `auto`)
chooses how the application reaches the engine: `auto` resolves at launch
from the authority volume class — `embedded` (same engine in-process, no
loopback socket) everywhere except observed network mounts, where the
engine's Normal durability tier requires the supervised `tofu-db serve`
daemon. `daemon`/`embedded` remain explicit overrides; this chooses transport, never authority/backend. See [native storage](STORAGE_TOFUDB.md) for its contract and honest limits.
Embedded requests share the protocol's 64 MiB aggregate limit only for declared streamed commands; other operations retain the single-frame cap. The FFI reserves encoded bytes against its launch-probed queue budget; oversized commands use the same authenticated semantic admission as streams.
Framed writes submit length, body and CRC together using three borrowed I/O slices; partial writes and interruptions preserve exact offsets, admission and secret-buffer lifetime. This avoids split-write delayed-ACK stalls without widening peer deadlines or allocating another frame copy; zero progress, overrun and transport errors never report completion.

The SQL Sidecar releases serialized-frame admission after sending a response, then half-closes its write side and reads at most one byte to observe client EOF under the existing five-second socket timeout. This keeps queued trailing reply bytes alive through normal client receipt. It admits no additional request, does not extend RPC deadlines, and retains exact active-handler accounting without a count quota. Client frame validation and command receipts remain the acknowledgement authority.

**Managed native engine identity.** Problem: checking a mutable checkout's source
on every daemon recycle can reject the same engine already admitted by the
running application and exhaust recovery into permanent fencing. Decision:
the supervisor records its first admitted daemon path and full SHA-256, and
recovery rechecks those exact bytes plus the declared content pin. Checkout edits
and resolver environment changes do not select an upgrade during recovery;
replaced bytes refuse even with a new pin. A new application process performs
the original source-freshness admission. Alternatives rejected: dropping content
verification accepts artifact drift; comparing recovery against current disk
sources treats unrelated development as a failed authority upgrade. The witness
is one path/digest pair in memory, with no copied engine or persistent state.
Verify `tests/storage/test_storage_supervisor_restart_identity.py`.

**Managed-child liveness.** Problem: waiting only for process exit leaves a
live but non-serving authority ready indefinitely. Decision: bounded health
probes (`TOFU_STORAGE_LIVENESS_*`, `lib/storage/supervisor.py`), matching the
external monitor; at least three consecutive failures spanning
`TOFU_STORAGE_LIVENESS_UNRESPONSIVE_SECONDS` (default 120 s) trigger bounded
owner-channel EOF → SIGTERM → SIGKILL. Exit fences readiness and the restart
loop replaces the child. Alternatives rejected: RPC errors alone do not trigger
recovery; bounded engine shutdown alone cannot recover a child wedged elsewhere.

Problem: failure counts alone confuse starvation with a wedge and cause
cold-start storms. Decision: `_host_overload_detail` and
`TOFU_STORAGE_LIVENESS_OVERLOAD_*` suppress recycling while the cgroup memory
working set (usage minus inactive file cache) is at least 92% or loadavg1 is
at least 2× effective CPUs, resetting the streak.
Normal termination resumes once pressure clears. Suppression is bounded:
`TOFU_STORAGE_LIVENESS_OVERLOAD_MAX_SUPPRESS` (default 60 probes) forces one
recycle, with exponential `TOFU_STORAGE_LIVENESS_OVERLOAD_RECYCLE_COOLDOWN_SECONDS`
(default 300 s, cap 1800 s); any served probe clears the cooldown and the
suppression count. Observation failure falls back to healthy. Restart backoff
caps at 60 s. Alternatives rejected: unlimited tolerance leaves wedges
unrecovered; a larger failure count instead of overload attribution still
storms under sustained pressure; `/proc` process states cannot reliably
separate starvation and wedges. The time budget complements attribution, it
does not replace it: once page cache stopped counting as overload, a ~4.5 s
count-only verdict would kill engines that recover by themselves within a
minute (2026-10-26), and wedge recovery stays bounded at about two minutes
([STORAGE_OPERATIONS.md](STORAGE_OPERATIONS.md#admitted-engines-start-refusals-and-liveness)).

Daemon, embedded and external monitors share `_liveness_probe_config`; one slow
probe never fences readiness. Recovery remains distinct: managed termination,
embedded fencing/full in-process restart, or external detach/reattach. Daemon
and external monitors also share the unresponsiveness budget and overload
attribution (`_LivenessStreak`); an external attachment detaches at once when
its published PID/start identity has exited, and one without such an identity
(a SQL co-container) keeps the plain consecutive-failure verdict. Embedded
probes use one worker per handle (probe deadline + 2 s join); FFI wedges fence
through the driver crash callback because engine state is unknown.
The runtime restart loop is bounded by a sliding-window breaker
(`TOFU_STORAGE_RESTART_CRASH_LIMIT`, default 5;
`TOFU_STORAGE_RESTART_CRASH_WINDOW_SECONDS`, default 600): sustained
crash/startup-failure storms enter a terminal `degraded` state that
stops spawning, keeps writes fenced, surfaces in `status()`, and logs
CRITICAL once plus a periodic reminder. The fast first restart is
unchanged; leaving `degraded` requires a process restart.
`auto_restart=False` runtimes and attachments to an engine another process
owns never trip the breaker; a deterministic `StorageStartRefused` reports
`refused` and is rechecked every 30 s without counting as a crash.

Problem: a bounded restart can still drop task frames and produce avoidable
503s. Decision: `StorageRuntime.client()` waits on readiness while a restart
worker lives, bounded by `_FENCE_RESTART_WAIT_CAP_S` (default 30 s); degraded,
refused, stopping and never-started states still fail fast. The storage.v2 client retries
`database_unavailable` before the first response byte with the same
(tenant, owner, command_id) identity and bounded
`_COMMAND_MIN_RETRY_BUDGET_S`. A receipt-backed command replays its recorded
outcome and never applies twice; a receipt-free command is re-executed, so its
operation must be idempotent under re-execution. Provider ingress
retries persist→push inline (`_DELIVERY_MAX_ATTEMPTS`, backoff capped by
`_DELIVERY_RETRY_MAX_DELAY_S`); flush completion does not abort a retry, but stale
attempts, aborts and detach do. Alternatives rejected: slower liveness recovery
prolongs wedges; a separate replay queue duplicates ordering machinery; immediate
user-visible failure drops frames during otherwise recoverable restart windows.

Problem: `turn.delete` and `turn.branch.delete` were receipt-free, yet
re-executing a committed delete finds no rows. During a storage stall a delete
committed, its ACK was lost, and the same-ID replay answered
`database_not_found`; native echoes that code as the message, so the browser
toasted `database_not_found` for a delete that had succeeded. Decision: both
deletes are receipt-backed.
Callers name one delete intent with a SHA-256 identity (conversation plus sorted
turn IDs; conversation, parent and lane), within the 200-character receipt and
256-byte wire command-ID bounds, and `turn.delete` sends its IDs in that sorted
order so a reordered repeat replays instead of conflicting. Both engines mint
turn IDs as fresh UUIDs and never reissue a lane ID (a UUID in SQL; on native a
digest of a branch command fenced by the parent's strictly increasing
projection revision), so a receipt cannot absorb a later delete. Growth: each
delete retains one receipt. A SQLite measurement stored 541 B for a two-turn
delete, 697 B for four turns, and 6,363 B for a branch delete whose response
carries its 5,983 B parent Turn; the deleted rows themselves are removed.
Alternatives rejected: answering an absent turn as already deleted also hides
wrong IDs and needs new native and SQL semantics; refusing every ambiguous
receipt-free replay in the client discards the re-execution idempotency other
turn commands rely on and turns recoverable stalls into user-visible failures;
the former joined-ID identity exceeded those bounds for larger deletes. Specs:
the lost-ACK delete replay cases in
`tests/storage/test_storage_authority_sql_contract.py` and
`tests/tofudb/test_tofudb_differential.py`.

Problem: a storage-layer failure was classified as `server_busy`, whose hint
blames local admission capacity (task slots, memory headroom). A fenced
authority — whose only recovery is a process restart — then read as "server
busy, retry shortly" and stayed unrecovered for hours (2026-10-22: a degraded
sidecar served `database_unavailable` for 9.5 h under capacity wording).
Decision: `database_busy` / `database_unavailable` classify as
`storage_unavailable`; `server_busy` keeps its capacity meaning (memory-pressure
admission, event-delivery backpressure). The storage hint names the storage
layer and the restart recovery, and never points at keys or models. A degraded
runtime raises a non-retryable error whose text names the terminal state and
the required process restart, so the UI stops offering a futile Retry.
Alternatives rejected: keeping one kind cannot describe two recoveries; a new
kind per storage code multiplies the public taxonomy without changing any
recovery path; hint-only edits leave the machine-readable kind lying about the
subsystem.

Problem: the deployed engine and its content pin can drift apart, and the
supervisor then refuses to launch it — a deterministic refusal that the crash
breaker counts as repeated failed starts until the runtime latches `degraded`
(2026-10-22). Decision: `scripts/deploy_tofudb_engine.py` is the only writer of
`packages/tofu-db/target/certified/`; it installs the artifact by rename,
writes the `<binary>.sha256` sidecar the supervisor verifies, and records
provenance in `build.json`. Hand copies desynchronize artifact and pin (and
must never truncate the mmap'ed `libtofu_db.so` in place), and the supervisor
fails closed rather than launching unverified bytes. While the authority runs,
the managed-child monitor re-checks the deployed artifact against that pin
(`StorageSupervisor.check_verified_artifact`, every 60 probes): the check reads
a bounded head+tail sample and re-verifies the full file only when the sample
changed, so an out-of-band replacement is reported as CRITICAL instead of
surviving until the next application restart needs the pin (restarts of the
running engine keep its admitted private copy). Alternatives rejected: silently
re-pinning at start would trust whatever is on disk; a runtime backend switch
would invent a second authority; skipping the pin check removes the only drift
detector.

Problem: a native engine crash yields an exit signal, an uptime and — until the
host keeps a core — no faulting stack. Decision: engine stderr is mirrored
through `tofu.storage.engine` (`storage child pid=… stderr:`), so a panic or
engine log line reaches `logs/app.log`; the supervisor records exit
code/signal/uptime for every crash; a collected core is inspected read-only
with `python3 scripts/inspect_native_crash.py --core <path> --executable
packages/tofu-db/target/certified/tofu-db`. Alternatives rejected: attaching a
debugger to a live authority mutates the process under investigation; enabling
core dumps globally on a shared host is a host-policy decision this repository
does not own.

Supervision logs spawn identity, ready handshake, probe failures, termination,
exit code/signal/uptime, restart scheduling and degraded transitions via business
prefixes `tofu.storage.supervisor` / `tofu.storage.runtime` (`_BIZ_PREFIXES`).
A lifecycle-bound `stderr=PIPE` drainer mirrors engine JSON errors/critical and
all integrity fences to `tofu.storage.engine`, forwards raw lines to inherited
stderr, truncates at 16 KiB and checks respawn leaks; the pipe always drains.

### Conversation diagnostics

The inspector follows configured SQLite authority or the TofuDB loopback API,
never a stale fallback. Explicit `--db` is an offline override. The read-only,
source-fenced chunk protocol, budgets, overrides and tests are owned by
[CONVERSATION_DIAGNOSTICS.md](contracts/CONVERSATION_DIAGNOSTICS.md).

Flow phase commit replay retains only the transmitted suffix (or the last phase
on final revalidation) plus its owner/attempt/live-child fence. Durable
acknowledged history is not copied into every pending commit. Lost-ACK retries must still match the unacknowledged output
and terminal metadata exactly; no discarded evidence, owner-fence relaxation or
increased storage frame limit is needed to reduce this quadratic copy overhead.
A synthetic 128-phase/20-tool-row measurement reduced copied phase rows from 8,256 to 128,
copy-loop time from 1.37 s to 0.019 s, and peak traced allocation from 2.36 MB to
5.9 KB; this isolates replay-copy overhead, not end-to-end execution latency.

## Settings database panel

Settings → 数据库 (`lib/database_panel/`, `routes/api_v1/database_panel.py`,
`frontend/src/features/settings/database.ts`) is the user-facing surface of
this authority. Its guarantees:


The panel presents engine identity/health together, then filesystem/free-space
facts and three independent native disclosures for Fast Path, required search
index, and backup. Summary rows are full keyboard/touch targets; editable drafts
and exact selectable/copyable paths remain inside their own disclosure. Lock and
pending-restart evidence wraps rather than being clipped. The immediate-Apply
note distinguishes this section from the modal's global Save/Cancel. Theme-token
surfaces and gap-owned sections replace conflicting per-ID margin overrides;
more shadowed nested cards and permanently expanded path walls were rejected.
Browser specifications in `tests/visual/test_database_projection_settings.py`
cover three themes, both languages, 320–1440px widths, native keyboard toggles,
long authority/lock paths and read-only refresh preserving drafts/open folds.

- A successful dual-path backup publishes the durable `lastBackup` witness
  after its stable-directory mirror and ledger complete, for both native
  directory snapshots and file snapshots. Snapshot or mirror failure records
  a failed witness; an HTTP observer timeout alone cannot settle the backup.
  This shared publication avoids a completed native copy appearing absent in
  the panel; backend-specific success returns are insufficient evidence.
- The panel has no performance benchmark or idle benchmark scheduler. Engine comparison and workload acceptance remain explicit offline harnesses (see `STORAGE_PERFORMANCE_CHARTER.md`), not engine-selection scores in Settings.
- Backend switching writes `TOFU_PERSONAL_STORAGE_BACKEND` to the project `.env` and takes effect through the existing approval-gated restart — the no-runtime-switching rule above is unchanged. sqlite→tofudb may first run
  the certified offline import (`tofudb_migration_import`) against a
  verified online backup artifact; the import target is a fresh or
  checkpoint-bound authority, never one holding user content. Each backend
  keeps its own data; a switch deletes nothing.
- **Both engines have optional Fast Path, off by default.** SQLite uses capability-gated `TOFU_STORAGE_FASTPATH*`; native uses explicit `TOFU_DB_FASTPATH_DIR` ([native lifecycle](TOFUDB_FASTPATH.md)). The panel distinguishes saved next-launch configuration from actual activation and pending restart. Native local commits are Strict; remote shadow snapshots are asynchronous, so local-volume loss can lose the unreplicated tail. Native activation/fencing locks disable and relocation; exit requires offline verified export, not flipping a checkbox.
- **TofuDB's reconstructible search projection is required and independent of Fast Path.** It has its own placement controls; changing the index never relocates the database authority. Active/pending/default paths and reset semantics follow [Database path settings](contracts/API_CONTRACT.md#database-path-settings). Before initial status resolves, the panel renders a stable loading structure with disabled actions, never a generic engine's fake state; refresh invalidates editable state and preserves only the loading structure, not stale running-state claims. Failed status disables mutations and offers retry. Normal entry performs no disk capability scan or engine artifact verification; runtime reads, placement discovery and mandatory launch admission are independent.
- Daemon and embedded launchers share `lib/storage_authority_sql/projection_paths.py`. `TOFUDB_SEARCH_PROJECTION_DIR` overrides persisted `tofudb_search_projection_dir`; custom bases receive an authority-path namespace, preventing shared-disk collisions. No authority bytes are moved or deleted.
- Preferences are durable: hold the shared thread/advisory-process JSON lock across strict reread, field-preserving merge, private same-directory temporary write, flush → file fsync → replace → parent-directory fsync; create missing directories privately and sync parent entries. Pre-replacement failures preserve old bytes/absence. Post-replacement fsync failure raises but leaves new bytes visible; never roll back an uncertain commit or change the live index. Reread/retry reconciles it. Problem: shared `update_json_atomic(strict=True)` enforces parsing, not fsync success. Decision: reuse its locks with this local durability boundary, rather than weaken guarantees or alter all callers. Advisory coordination requires filesystem locking support.
- Dual-path backup mirrors the authority-owned `system.backup` artifact and manifest, pruning only previously copied files. Disposable-index candidates may be ephemeral; backup candidates must exclude known ephemeral storage.
**Decision: separate authority acceleration from index placement.** Problem: generic static switches appeared before status, then disappeared for TofuDB while the same picker became an index picker; native Fast Path existed but lacked a panel entry. Keep independent optional Fast Path, required search index and verified backup sections with explicit configured/active/pending witnesses. Reject equating search with Fast Path, claiming an absent runtime witness is active, and silently disabling a fenced native authority. Search candidates may be ephemeral because the index is reconstructible; authority/front and backup candidates may not.

## Owners and entry points

| Concern | Owner |
|---|---|
| Client API, deadlines, supervision | `lib/storage/` |
| Native RPC client and semantic adaptation | `lib/storage/v2_client.py`, `lib/storage/v2_semantics.py` |
| Native engine, transactions and recovery | `packages/tofu-db/src/` |
| SQL RPC server and operation dispatch | `lib/storage_authority_sql/server.py`, `operation_registry.py` |
| Semantic operation domains | `lib/storage_authority_sql/operation_domains/` |
| Transaction implementations | `lib/storage_authority_sql/operations_pkg/` |
| Durable schema and migrations | `lib/storage_authority_sql/schema.py` |
| SQLite/PostgreSQL adaptation | `lib/storage_authority_sql/adapters/` |
| Deployment/preflight | `lib/storage_authority_sql/config.py`, `preflight.py` |
| Operator CLI | `scripts/storagectl.py`, `python -m lib.storage_authority_sql.migrate`, `python -m lib.storage_authority_sql.migration.tofudb_migration_import` |

Protocol, schema and registry versions come from their code constants and the
machine-readable operation catalog, not a separately maintained prose version:
`lib/storage_authority_sql/schema.py`, `lib/storage_authority_sql/operation_domains/`,
`contracts/storage_operations_v1.json`, and the native v2 wire contract.

The native import CLI checks every source table against the declared mapping
plan before opening its target, then repeats this gate in the import snapshot.
Any unmapped populated table refuses operator import; table names never imply
safe deletion. Run `python -m lib.storage_authority_sql.migration.tofudb_migration_coverage
--source /absolute/source.db` for the JSON inventory (exit 2 means incomplete).
Empty unknown tables remain visible. This is table coverage only: field fidelity,
managed namespaces, replay state, target verification and cutover certification
remain separate requirements. The Python domain-import API defaults to partial
coverage for isolated adapter certification and cannot certify full-source cutover.

## Synthetic continuation admission

`turn.create_pair` accepts the opt-in `require_parent_not_stopped` fence
(default false). Under the same owner/conversation lock used by terminal event
recording, creation requires the supplied parent to remain the lane tail and
its authoritative status to be `completed` or `failed`. A missing, changed,
live, cancelled, aborted, or interrupted tail rejects with
`turn_lane_advanced` before any new pair is committed. Failed tails remain
eligible for bounded diagnostic work. The scheduler and conversation command
service forward this fence; other callers retain their existing semantics.
An already committed command still replays idempotently before admission checks.

## Child archive ownership

`invocation.register_child` validates an owned real conversation and parent task
birth (or an exact bound generation attempt), then stores immutable user,
conversation, parent-task and child-task IDs in the existing `task_results`
record. Natural replay returns the same identity; rebinding and generic record
forgery/deletion are rejected. Registration failure disposes the runtime child
before dispatch. Headless/transient parents do not access storage.

Compaction archives keep the real `conversation_id`; `archive_scope` defaults to
empty for root archives. A nonempty child scope requires exact registered birth
evidence, never just a prefix. Create/get/list/update/prune/delete filter scope
before projection or limits. Each scope uses the existing retention count and
payload budgets. Conversation trash retains all scopes for restore; permanent
conversation purge removes every scope. Birth identity remains a small immutable
task record so an old child ID cannot be rebound after archive deletion.

The semantic differential gate preserves raw returned clocks until comparison,
then checks their exact presence and JSON type before ignoring clock values.
Boolean/number mismatches fail. Only declared server-identity fields may map to
stable placeholders; user content and arbitrary UUID text remain byte-sensitive.
Shape-only probes require successful responses from both authorities. A context
history digest may be normalized only after independent recomputation from that
owner's durable Turn headers; unverified proofs remain exact comparison data.

## Model continuation checkpoints

`lib/storage/model_continuation.py` owns `model_continuation.get/put/delete`.
These owner-scoped semantic operations retain one derived working-model view per
existing conversation lane; SQL uses `storage_records`, while native TofuDB
uses an owner-scoped continuation document. They do not edit Turn history.
The checkpoint has no TTL and survives worker restart/cache eviction. It is
prepared once before completed or truncated-task settlement when a complete
assistant text boundary exists, not on every model round. Pending tool calls,
failed/aborted execution and unsupported recovery modes do not publish a prefix.
`turn.event.record` publishes the bounded candidate in the same transaction as
terminal projection, attempt, replay carrier and conversation revision, using
the authoritative settled Turn witness and ordinal. Storage write failures roll
back the entire terminal command; preparation failures/invalid candidates settle
with an explicit `modelContinuationStatus` in the terminal attempt event and
`model_continuation_status` in the command result, without deleting history or
an existing checkpoint. Queue draining and terminal delivery follow this commit.
Standalone `put` remains an explicit settled-import compatibility seam, not the
live task publication path.
Each value is capped at 8 MiB and 16,384 model messages. A proof checkpoint
carries one terminal witness and has no Turn-count bound; only legacy per-Turn
witness lists keep their 4,096-witness cap (see
[long-horizon context reads](TOFUDB_CONTEXT_READ.md#streamed-proof-walks-without-a-turn-ceiling)).
Conversation deletion/purge and Turn deletion remove the derived continuation.
The selected authority validates ownership and the settled frontier witness;
authority-derived ordinal CAS rejects stale writers and equal-frontier changes.
These live-CAS commands keep no duplicate payload in command receipt history.
Restore still validates the complete prefix, policy and media against authority.
Failure is observable and leaves the full transcript intact; subsequent prompt
admission must safely bound a cold reconstruction.

Continuation persistence and get/put/delete execute natively on both authority
families. Tofu-DB serves all three operations through the shared
versioned-document codec and owner-bound record layout (see
[Tofu-DB storage](STORAGE_TOFUDB.md)), and the `model.continuation.v1`
migration adapter covers certified source replay.

### Native Supervisor selection (storage.v2 direct, no Python sidecar in the hot path)

Native backend selection, engine guarantees, bounded domain migration and release
certification are defined in [Tofu-DB storage](STORAGE_TOFUDB.md). Selecting the
backend is separate from proving complete source migration and safe cutover.

## Identity and dependency rules

- Auth resolves identity once at the request boundary. Every durable
  user-owned operation below it receives a positive `owner_user_id` (some
  established repository method parameters remain named `user_id`; their
  value is always the numeric owner, never an account subject).
- Routes contain no SQL and do not derive storage paths.
- Static startup scanning rejects drivers, DB helpers, SQL, DB-shaped handles and argument-bearing connection factories. Problem: generic no-argument HTTP/domain `connect()` methods falsely blocked boot. Decision: treat them as domain verbs and invalidate old caches, retaining evidence-based fences instead of a file exemption. This lexical guard cannot prove arbitrary dynamically aliased database ownership.
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
rejects credentials bound to inactive accounts. Owner+tenant credential counts
observe integrity without an admission quota. Native lists traverse all index
windows in one snapshot and retain exact count agreement; SQL lists keep owner
and tenant predicates before ordering. Bootstrap emptiness remains atomic,
revoked tombstones remain identifiable without granting authority, and secret
hashes never enter public projections. A per-record response bound is not an
aggregate list cap. Gates include the credential demand, ownership/authentication
and semantic differential scripts.

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

`turn.event.record` requires a command receipt when its typed `terminal` field
is `true`. The operation-domain declaration `receipt_when_true='terminal'`
projects to `receiptWhenTrue` in the JSON catalog and the native metadata;
SQL and native execution select the same ordinary atomic receipt boundary.
Missing, false or non-boolean flags do not select it; receipt selection does
not coerce input types.

Problem: a terminal commit settles the Attempt. Re-executing after losing its
ACK previously returned `applied: false`, so a successful completion became a
stale-attempt failure. Decision: replay the exact committed response for the
same command identity and canonical payload, including the carried event and
model-continuation result. A different payload still conflicts; rollback and
owner fences remain shared with other receipted commands. This covers transport
retries of the same command, not an unrelated later command with a new identity.
Alternatives rejected: assuming every settled attempt proves this request
committed accepts foreign/changed completion evidence; receipting every stream
frame adds unnecessary retained writes. Nonterminal events and their batches
retain their current CAS and durable-row reconciliation.

The lost-ACK regression runs through both application authorities; native tests
also verify reopen, unchanged WAL on replay and changed-payload/owner refusal.
Retention cost is one ordinary command receipt per accepted terminal intent,
under the existing receipt lifecycle; see
[storage operations](STORAGE_OPERATIONS.md#terminal-event-receipts).

Knowledge source/asset files precede metadata publication. `KnowledgePublicationAttempt`
records storage-client entry monotonically across same-receipt retries; failure afterward
may follow a commit. Only failure before every entry permits candidate deletion.
The guarded boundary includes index preparation, source naming/writing and metadata
conversion/encoding after returned visual ownership. Cleanup attempts every returned candidate,
preserves the original error and records secondary cleanup failures.
Unknown outcomes retain files; a later retry refusal cannot authorize their deletion. Successful duplicate/vanished results retain their existing cleanup semantics.
See [candidate retention](STORAGE_OPERATIONS.md#knowledge-publication-candidates).
On `O_DIRECTORY` platforms, knowledge publication fsyncs changed file directories
and ancestors through the existing data-root anchor, children first and once per
call, before metadata entry. File fsync remains strict; directory failure rolls
back new candidates without dispatch. Other platforms retain file fsync/rename only.

Command receipt encoding and admission derive from `contracts/tofudb_ir_v1.json` through
`generated_storage_receipts.py`: stored and decoded responses have no private
byte quota on either backend. Responses above 64 KiB get one private zlib level-1
attempt when their length fits the existing u32 compressed header; larger
responses retain plain JSON. Both paths retain the smaller representation.
The versioned decoder verifies exact declared output before parsing, fails
corrupt/unknown envelopes as `database_integrity`, and reads legacy plain JSON
and compressed receipts. Older 64 KiB readers cannot read newly accepted large
receipts; a binary downgrade requires a compatible reader or an upgrade backup.



`turn.steer.commit` stores only bounded injection/Turn identities and the committed
conversation revision, plus one atomic `conversation.activity` snapshot invalidation.
Neither the full Turn nor its growing injection history enters the receipt. Replay
returns the same receipt without repeating mutation or advancing the sync head.
The lifecycle boundary hydrates the current owner-scoped public Turn only after the
command service has published the live inbox wakeup. A failed post-commit read
still returns accepted steering without `latestTurn`; the committed invalidation
repairs presentation without inviting a duplicate human submission.

RPC accepts named operations and JSON-compatible payloads only. SQL, paths,
connections, and transaction handles have no wire representation.

storage.v2 response frames (including chunked streams) charge the same
process-level `TOFU_STORAGE_RPC_INFLIGHT_MAX_MIB` RAM buffer threshold as v1.
Excess receive bodies use private anonymous temporary files instead of waiting
or rejecting, with checksums/decoding before file and mapping cleanup. Exact
leases track RAM/file ownership; already-encoded writes charge actual body
length. Native Rust frame/input byte admission is an independent boundary.
Native codec exceptions on the v2 wire or embedded boundary
(`ValueError`/`TypeError`) are contained as `database_protocol_error`
with the session retired and RPC metrics recorded, and the v2 request
path rejects non-finite floats exactly like v1. Client retry loops emit
at most one WARNING per operation per 30 s.
`event_batcher.close(timeout)` fails
still-queued events with retryable `database_unavailable` on timeout
instead of leaving producers hanging; the drain-first normal path is
unchanged. Storage warm-up runs one lifecycle-owned demand executor and
merges only the same client/operation/payload across passes; independent
reads never wait behind a worker count window. Completed merge entries retire.

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
For Turn-native conversations, `messages_json` and aggregate `search_text` on
the header are frozen empty placeholders. A conversation imported before the
Turn cutover instead keeps its frozen, potentially non-empty `messages_json`
as the compatibility transcript until an explicit verified migration; runtime
writes never replace either shape with a conversation-sized message document.

Owner-scoped `conversation.get` exposes mutually explicit full, metadata-only,
and bounded message projections. A `message_window` is limited to 1..500 and
may carry a non-negative exclusive `before_sequence`; a cursor without a
window is invalid. Active Turn transcripts page through the backend-neutral
adapter contract. Frozen pre-Turn JSON archives scan only a requested first or
last page when its estimated and observed Python work stays within 128 KiB of
code units; middle pages, count/shape ambiguity, or a larger suffix/prefix use
the authoritative full decoder and then slice. Windowed reads never select or
return the unrelated aggregate `search_text` corpus.

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
identified ``toolRounds`` copy. Every semantic read hydrates those references
before mutation, search extraction, or return, so the public turn/API contract
is unchanged; divergent, ambiguous, partial, and future shapes remain verbatim.
Malformed or unknown codec references fail closed as ``database_integrity``.
Turn-native rows are converted only by their next ordinary write—there is no
startup scan or rewrite competing for the authority writer. Frozen pre-Turn
conversation archives remain readable in their plain form. An explicit
physical offline deep-clean may apply the sequence form of the same codec
message by message, but only after an exact canonical round-trip and only when
the stored document becomes smaller; repeated passes are write-free.

Conversation search is an eventually consistent, rebuildable projection. A
turn or lifecycle mutation writes only one small version-token dirty marker to
`storage_projection_outbox` in the authority transaction. Search-text
extraction, historical scans, and projection I/O never run inside the user
transaction or the authority writer. An acknowledgement deletes only the
exact token the worker read; a concurrent mutation replaces that token and
therefore cannot be lost behind a stale acknowledgement.

The search-v2 authority is [`contracts/conversation_search_v2.json`](../contracts/conversation_search_v2.json).
SQLite materializes `storage_search_conversations`, compatibility
`storage_search_turns`, and field-preserving `storage_search_chunks` in a private
host-local `turn-search-v1.sqlite3` (disposable format 2), keyed
by a hash of the authority data root. The file is disposable, quick-checked on
open, and discarded and rebuilt on corruption. The local SQL projection has
no corpus-byte admission quota; `TOFU_TURN_SEARCH_PROJECTION_MAX_MIB` is retired.
Indexed rows follow owner-scoped source/generation deletion and rebuild lifecycles. Search failure degrades only search; it does
not revoke authority readiness or roll back a user write. PostgreSQL uses the
same tables in the shared database but writes them through independent,
separately committed maintenance transactions.

The Sidecar owns the projection worker. It drains a bounded dirty batch and,
after the default 60-second startup delay, advances historical backfill even
while dirty writes continue. Each conversation job reads one 8-row/2-MB source
page per invocation; backfill visits one header at a time. Settled main-lane
Turns, frozen pre-Turn messages, retained compaction archives (including admitted
compressed messages), and titles contribute. Original wording, content,
translation, and thinking remain separate 4096-character chunks with 512-character
overlap, removing the former 10-KB text-head cutoff. Plain Turn reads select
public text rather than transferring giant tool payloads. Checkpoint/patch heads
use the canonical hydration owner. Individual oversized/corrupt sources leave
persistent partial-coverage evidence; source-generation races retry instead of
being acknowledged as permanent skips. Counters are conservative diagnostics,
not exactly-once per-source accounting. Reset clears them and restarts backfill.

Format upgrades rebuild only the disposable index. Generation tokens preserve
replay and retire old rows after completion; multi-page rebuilding is eventually
consistent, not an atomic whole-conversation snapshot. Local growth is rejected
before commit with database/WAL reserve; capacity failure rolls back the projection
transaction, never the authority write. Search-status reads use a worker snapshot
rather than borrowing the projection writer.

Queries return owner-bound keyset continuations, including empty partial pages,
without the old 5000-conversation scan cutoff. Phrase/title/ID results precede
cross-Turn all-word results, ordered by recency and ID within each phase. SQLite
trigram acceleration has a 500000-VM-step ceiling; exhaustion switches to a
resumable portable scan instead of restarting the expensive query. Portable
queries inspect at most 256 headers and 512 chunks per page, in 16-chunk batches;
word bits and the intra-conversation chunk key survive continuation. Only bounded
snippets and one owner-scoped metadata batch cross the application boundary;
search never hydrates transcripts or whole settings documents for visible hits.
This projection/indexing path applies to SQLite/PostgreSQL; live PostgreSQL
execution remains a separate opt-in verification lane.
The pre-authority Rust projection builds invisible generations and atomically
publishes headers, text visibility and the owner partial-source counter. Public
text fields use 4096-character chunks with 512-character overlap; per-chunk Bloom
summaries reject negatives before text reads. Pages admit at most 256 headers
and 512 chunks, carry keys/word bits in an 8192-byte source/owner/query-bound
cursor, and restart pending matches when their generation changes. Original user
wording wins consistently across accelerated and portable scans. Source pages
admit eight Turn/archive rows and 2000000 materialized bytes; oversized archives
remain explicitly partial. Owner budgets include staged/active headers, titles
and chunks, including conversations with no text. Generated native limits come
from the search contract through `scripts/gen_tofudb_ir_contract.py`.
The Rust daemon can opt into a dedicated absolute persistent projection directory outside the authority. Its sole maintenance worker first enqueues at most 16 inline backfill headers (128 KiB of source values), then drains at most 16 token-valued dirty conversations through the pending durable backfill cursor. This avoids rebuilding the initial dirty corpus twice while preserving complete explicit later backfills and mutations on either side of the cursor. Source snapshots end within the round; the worker reads its 8-row/2000000-byte Turn/archive pages from one batch-scoped pinned read replica (the snapshot read lane), so a slow-mount source page never holds the foreground authority mutex against a writer, and acknowledges only exact observed tokens through a brief writer acquisition. The combined Turn/archive source read is bounded to 1,024 pages; exhausting that budget atomically publishes explicit partial coverage and acknowledges the observed tokens, so unchanged oversized sources do not retry forever or block later conversations. New source mutations can rebuild and clear partial coverage. Each invalidation gets a fresh persisted 128-bit token, preventing stale acknowledgements across marker deletion/recreation; old 8-byte tokens remain readable. Disposable format upgrades bind the source authority UUID and persist a pending marker until source backfill scheduling commits. Repeated work reuses an already published matching generation, while a changed source resets only that owner’s projection ranges. `conversation.search` uses a separate deadline-bounded projection mutex, returns a retryable capability error when unavailable, and never acquires or fails the foreground authority. Capacity is 2% of launch-observed free space clamped to 128 MiB..4 GiB per owner. `turn.search.backfill` remains a scheduler and legacy aggregate search fields remain non-authoritative. The separate `backfill-activity-index` operator command is explicit existing-authority maintenance: it holds the normal authority lease, persists a building cursor, materializes at most 256 source headers/16 MiB per transaction, and requires repeated invocations until `complete=true`; foreground mutations maintain membership while building, and neither daemon open nor startup performs this retrofit scan.

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

- delete atomically moves the header and normalized turn graph to `storage_conversation_trash*`, removes executable state, and makes every
  active query/write observe “missing” immediately;
- restore atomically rebuilds active rows without attempts or live latches;
- clone atomically creates a new terminal graph and remaps executable
  identities; the browser never uploads a message array;
- maintenance permanently purges trash after 30 days in bounded oldest-first batches.

Tofu-DB compiles delete, restore, purge, and clone through bounded lifecycle primitives. Delete pins recoverable header/Turn/tombstone ranges and detaches active indexes, attempts, and sync replay without walking transcript rows. Restore mounts those ranges, overlays a normalized header, advances revision plus a conversation execution epoch, repairs the exact owner count, and removes trash metadata/pin in one receipt-backed commit. Turns from older epochs project as inert: attempt/run latches disappear, live status becomes interrupted, and runtime/tool presentation is terminalized; post-restore Turns inherit the new epoch. Clone reads one OCC snapshot capped at 2,000 Turns, 8 MiB of projection, and 1,000 archives, atomically creates a new header/lane graph/index/identity-claim set, remaps Turn/task/archive/parent identities from the command, and copies no attempts or executable latches. Archive transcript, summary, and receipt documents are separately blob-capable: summary updates never rewrite transcript payload, and same-owner clone reuses immutable content-addressed blocks. Live source projections are terminalized while source execution remains unchanged. Identity claims remain fenced until bounded tombstone pruning or purge, and legacy randomly keyed attempts also require an active header. Explicit backup/GC traversal follows persisted stream segments and owner-bound versioned-document, command-receipt, and logical-outbox blob graphs from active or pinned Entity leaves without transaction history; normal open and foreground delete/restore operations never scan values. Payload-segment compaction and certification remain authority gates; any lifecycle action whose foreground cost or temporary bytes scale with a 90 GB transcript fails closed.

Conversation Sync v3 owns turn commands, snapshots, and replay; see
[CONVERSATION_SYNC_V3.md](contracts/CONVERSATION_SYNC_V3.md).

## Transaction model

### SQLite

- One physical read-write connection serializes all writes; read-only pooled
  connections cannot upgrade to writers.
- Validated RPC handlers start on demand; no local handler-count permit
  refuses an independent request. Framing/authentication and real worker-start
  failures keep their authority.
- The ordered SQLite writer accepts waiting jobs without a count quota.
  `TOFU_STORAGE_SQLITE_WRITER_QUEUE_CAPACITY` is a legacy sizing hint for
  shared RAM/metric policies. Writer acquisition uses the caller deadline,
  without a separate priority-lane timeout. Expiry removes a still-queued
  operation; a job already drained into a batch keeps its cancellation fence.
- Query-only SQLite connections are created on demand. Configured read counts
  size idle reuse; all borrowed/connecting readers remain owned until returned
  or retired. Close rejects new leases and joins existing ones before closing
  caches, idle handles and the authority anchor. Age/idle rotation and rollback
  poisoning retire handles without reducing future reader eligibility.
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
- Durable event batching retains frozen canonical payloads through ACK.
  Launch-probed bytes size RAM/file representation; excess bodies use owned
  anonymous files, with no pending item/aggregate-byte admission quota.
  Batches still compose at most 500 events / 60 MiB below the storage.v1 frame.
- Event serialization and individual frame bounds are validated before
  acceptance. Confirmation/flush expiry removes only unclaimed work and closes
  its representation. A claimed event's producer awaits its batch's outcome,
  bounded by the batch RPC's own deadline (or the worker's exit), so a row is
  never re-sent while its batch is in flight; after a settled failure the one
  `event.append` fallback
  relies on natural-key deduplication by `(task_id, sequence)`. The 1 ms gather
  window preserves concurrent-stream amortization and commit-before-visibility.
- Commands use `BEGIN IMMEDIATE`, caller-bounded waits, and progress-handler
  deadlines. Rollback precedes error classification. A COMMIT failure may replay only with a durable receipt or a still-active transaction observed before successful rollback; absent transaction evidence means unknown delivery. Batch failures retain the exact uncertain segment: its receipt-free jobs fail once, while receipt-backed and later unexecuted jobs retain service. Raw callbacks have no implicit replay.
  Diagnostics publish the current `begin` / `execute` / `commit` / `rollback` / `post_commit` phase. Problem: rollback after a completed COMMIT cannot undo its effects. Decision: carry receipt authority and inspect transaction state before rollback. Alternatives: unconditional batch fallback can duplicate mutations; disabling all retries would also discard proven rollback and durable-receipt recovery. See [SQLite transaction semantics](https://www.sqlite.org/lang_transaction.html) and `test_sqlite_commit_delivery.py`.
  **Batched failures wait for their segment.** Problem: a group-commit member
  that failed inside its savepoint (for example a `record.put` version
  conflict against an earlier member's still-uncommitted write) was answered
  before the segment committed. Its caller could read the pre-segment state
  through the query pool (an evolution reconcile then re-raised the conflict
  as unexplained), and if the segment rolled back the conflict was spurious.
  Decision: a failed member's error is delivered with its segment's COMMIT;
  when the segment rolls back, the member stays pending and the batch retry or
  per-job fallback re-evaluates it against durable state (its savepoint left no
  effects). Alternatives rejected: re-reading on the client after a conflict
  only narrows the window and leaves the spurious-conflict case; disabling
  group commit for CAS writes reintroduces one fsync per write.
  A failed ROLLBACK on the sole writer is a writer-fatal event: it logs
  ERROR, fences the writer (new submissions and queued jobs fail fast with
  retryable `database_unavailable` "poisoned"), counts
  `writer.rollback_failures`, and hard-exits through the same watchdog
  semantics so the supervisor restarts the Sidecar — a poisoned writer
  never silently serves another job. A failed rollback on a read-pool slot
  closes and discards the slot (WARNING) and the pool replenishes it,
  mirroring the PostgreSQL `broken=True` rule. If the writer thread does
  not exit within the close join budget (e.g. FUSE-wedged), `close()`
  skips the cross-thread connection close and logs a warning rather than
  running a wedged thread onto a closed handle. `SQLITE_FULL` maps to
  non-retryable `database_unavailable`: a full volume does not heal inside
  an RPC deadline and no longer burns backoff retries.
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
- FUSE is accepted only after lock, fsync, atomic-replace, and WAL-recovery
  preflight succeeds; durability is never weakened to accommodate a mount.
  Preflight latency is reported as `fsync_ms` and never refuses startup: a
  slow or loaded mount that passes these checks is still durable ("Hardware
  never limits work", [RESOURCE_CONTROL.md](RESOURCE_CONTROL.md)), and the
  statement deadlines and writer watchdog above bound individual stalls. A
  startup latency threshold (formerly `TOFU_STORAGE_PREFLIGHT_MAX_MS`, now
  ignored) is rejected: any threshold refuses all storage on a busier host,
  and a default-off switch gets turned back on.
- Before reclamation reaches the writer, the adapter checks the observed
  authority topology. Network, generic userspace, and unknown filesystems
  return `offline_required` without executing any SQLite statement: even one
  `incremental_vacuum(1)` can be an uninterruptible kernel/filesystem call on
  BeeGFS/NFS/FUSE. Known host-local block, memory, and container-overlay
  authorities may enter the bounded online path.
- Eligible reclamation checks its wall budget between `incremental_vacuum(1)` units. A freelist of at least 1,048,576 pages that also occupies 25% of the file is classified as bulk compaction: online page moves stop for that process and the response points to the explicit offline deep-clean workflow. Transactional retention operations are registered once with kind `maintenance` and dispatched through the same backend-neutral operation catalog as commands; they run at maintenance writer priority and never accumulate command receipts. Task-event streaming and structural tiers use six-hour and 30-day horizons respectively. By default, each tier deletes at most 16 separately committed pages of 25 rows; a remaining backlog is revisited in 30 seconds, while a drained backlog is probed only every five minutes. The mutually exclusive, age-only partial indexes make both an empty probe and `ORDER BY created_at_ms LIMIT` range-bounded without scanning the other tier. Streaming backlog drains before the longer-lived structural tier, so one maintenance cycle cannot stack both workloads. An established SQLite authority missing the v2 age-leading partial index does **not** use the v1 `(stream_kind, event_type, created_at_ms)` compatibility index while serving. A deleted-row limit cannot bound how long that legacy search holds SQLite's sole writer on a multi-GiB event table. The semantic prune therefore returns `legacy_index_offline_required` before any scan, the maintenance owner disables that tier for the process, and the explicit offline deep-clean/index workflow below owns legacy classification and transition. An authority with neither index similarly returns `missing_index`. All online retention and reclamation work shares a process-lifetime circuit: if any bounded maintenance unit trips the writer watchdog, optional housekeeping stops instead of repeatedly starving user/event writes; restart performs one fresh probe.
- Best-effort log aggregates persist counts separately from their TTL sweep.
  Failed flushes back off exponentially to five minutes; TTL deletion runs in
  the maintenance lane and removes at most 500 indexed rows per transaction,
  so observability cannot form a retry storm behind user writes.

### PostgreSQL

- The adapter connects to an externally managed primary with verified TLS.
  The application never runs database-server binaries or stores PG data.
- Read/write connection owners stay isolated and create Psycopg connections
  on demand. Configured sizes retain idle handles, never cap active borrowers;
  startup records server max_connections without deriving a local 80% permit
  budget. Real server refusal and connection errors keep SQLSTATE classification.
  Close fences both owners, joins borrowers/preparers and releases caches after
  connection retirement. SQLite and PostgreSQL share one lease lifecycle owner.
- Pooled JSON/JSONB loaders mark decoded strings so shared codecs never parse them twice; `"true"`, `"null"` and JSON-shaped strings retain their type. Other structured results keep their existing representation and projection byte-evidence checks. Loader configuration is connection-local and covers text/binary results.
- `fsync`, synchronous commit, full-page writes, primary status, schema version,
  and transaction deadlines are verified.
- Application startup validates only. `python -m
  lib.storage_authority_sql.migrate` is the one schema migration entry point and
  publishes a new version only after the migration transaction commits.
- Distributed worker claims use adapter-owned `FOR UPDATE SKIP LOCKED` row
  selection. SQLite executes the same operation inside its serialized writer
  transaction; backend-specific lock syntax never enters application code.
- Transaction phase is tracked, and a failure in the COMMIT phase (lost
  ack) is retried only for receipt-backed commands, whose advisory lock
  plus receipt probe makes replay safe; receipt-free commands (including
  maintenance) are never re-executed and surface the ambiguous classified
  error. Begin/execute-phase retries are unchanged. SQLSTATE 25P03 maps to
  retryable `database_timeout`.
- `idle_in_transaction_session_timeout` is set per transaction via
  `set_config(..., is_local=true)` from the operation's declared budget
  (the same source as `statement_timeout`, up to 300 s); the 5 s pool
  default covers only connections without a declared budget. A pool
  `release` failure closes the connection instead of leaking it.
- Backup/PITR, HA, and autovacuum belong to the platform.

### Adaptive topology and transactional logical shadow

`lib/storage_authority_sql/storage_capabilities.py` is the single vocabulary for
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

After commit, `lib/storage_authority_sql/logical_outbox.py` wakes one bounded
publisher. It reads a small page, releases the database connection, appends and
fsyncs through the `LogicalCommitSink` boundary, then performs a tiny ordered
ack transaction. Network/FUSE file I/O therefore never runs while the SQLite
writer or a PostgreSQL transaction is held. A crash after file fsync but before
ack is safe: retrying the same sequence and event ID compares the canonical
record and acknowledges it as a duplicate; a divergent reuse, gap, checksum,
or lineage fault poisons the publisher and fails closed.

The built-in sink is `lib/storage_authority_sql/logical_shadow.py`. Its private,
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
- `TOFU_STORAGE_LOGICAL_SEGMENT_MIB` selects a positive rotation target;
  `TOFU_STORAGE_LOGICAL_PUBLISH_BATCH` selects a positive cursor page size.
  Neither has a fixed upper ceiling. The former OUTBOX_MAX_MIB,
  SHADOW_MAX_MIB and RECORD_MAX_MIB environment knobs are retired.
  Record, outbox and total shadow sizes do not grant or deny business writes.
  A large record owns its segment. History remains durable until its declared
  publication/ACK lifecycle; it is never deleted to regain capacity.

Problem: record/backlog quotas rolled back valid business writes, and shadow
quotas stranded their publication. Decision: capture every replayable mutation
and preserve complete encrypted records, using segment rotation as a work hint.
Alternatives: increasing separate ceilings leaves the next refusal; truncating
mutations or deleting history loses recovery evidence. SQL mutation prefixes,
transaction atomicity, frame lengths/checksums, sequence lineage, file ownership,
permissions, fsync and exact ACK checks remain authoritative. Reads check actual
remaining file bytes before allocating a claimed frame; real I/O/allocation
failures stay visible and cannot ACK unpublished records. A complete frame with
an intact checksum and a damaged length is corruption, even at the open tail;
recovery never truncates it as an unfinished append.
Verification: 44 capture/shadow/replay/config checks retain a 9 MiB record,
a complete 4,097-mutation replay into a fresh SQLite authority, exact ACK and
legacy-hint retirement, alongside corruption/permissions/crash-tail behavior.
A synthetic 9,437,184-byte body produces a 9,437,527-byte frame / 9,437,684-byte
file; fsynced append took 115.50 ms with 37,750,533 bytes traced peak, excluding
input construction, and reopen retained exact content. This measures the shadow
writer, not encryption, database throughput or model work.

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

Fast-path lineage manifests distinguish absence from failure. A missing file
may permit initial discovery; an unreadable or malformed manifest, a non-file
path or a symlink aborts authority selection and offline fallback. Never treat
corruption as permission to open the older classic database. Reads are capped
at 64 KiB so malformed lineage metadata cannot allocate unbounded memory.
The existing authority and shadow remain untouched for explicit recovery.

SQLite backup retention requires a complete publication manifest identifying
its artifact, size, checksum and successful integrity check. Online and
offline backup target connections both install a deadline-driven progress
handler and a busy timeout, so the post-copy `PRAGMA integrity_check`
cannot wedge past its deadline while holding `_backup_lock`, an RPC slot,
and a read-pool slot; an expired verification aborts with
`database_timeout` and releases all three, and the abort path closes the
target connection (including `-wal`/`-shm` sidecars) before artifact
cleanup. Missing, malformed
or symlinked manifests and uncommitted copies never displace verified recovery
points and are never automatically retired. These regular files still count
against the recovery-copy disk budget; ambiguous bytes cannot justify a
capacity reservation by assuming they will be deleted. Manifest inspection is
bounded to 16 KiB and does not hash database contents; restore retains the full
checksum verification. Pruning refuses to run unless its new recovery point
has valid publication metadata.

Fast-path lineage manifests distinguish absence from failure. A missing file
may permit initial discovery; an unreadable or malformed manifest, a non-file
path or a symlink aborts authority selection and offline fallback. Never treat
corruption as permission to open the older classic database. Reads are capped
at 64 KiB so malformed lineage metadata cannot allocate unbounded memory.
The existing authority and shadow remain untouched for explicit recovery.

## Durable turn-source queue

`storage_queue_items` is the owner-scoped authority for human, Goal,
peer, and workflow turn sources waiting behind a conversation task. Dequeue
leases rather than deletes a source; only accepted Turn/attempt creation may
finalize it.
leases rather than deletes a source; only accepted Turn/attempt creation may
finalize it.

**Problem:** a queued Turn pair commits its input Turn plus a `pending` output
Turn whose attempt carries `queueBinding`, long before any executor task owns
that attempt. The transcript therefore has exactly one explanation for the
output Turn: the queue row. The native `turn.sync.snapshot` step answered
`queueItems: []` unconditionally (`queue_items: Vec::new()` in
`turn.rs::sync_snapshot_with_proof`) while the SQL projection and
`queue.list` both served the row, so the browser hid the queued output Turn
(by its binding) and drew nothing in its place — a header-only ghost bubble —
and every timing receipt for that attempt was rejected for having no task
identity.

**Decision:** `turn.sync.snapshot` composes `queueItems` in the same read
transaction through `queue::snapshot_items`, sharing the `queue.list`
projection body so the two surfaces cannot disagree, and the item is the
contract's `ConversationQueueItem` (no `userId`/`payload`/`config`; the
transcript text prefers the embedded user message). The browser additionally
derives "this Turn is queued" from the attempt's durable `queueBinding`, so a
lagging queue read degrades to an unpositioned chip instead of an empty Turn.

**Alternatives rejected:** a browser-side transcript refresh on every queue
change adds a second read authority to explain one commit; leaving the
snapshot empty and rendering a synthetic "waiting" block invents state the
server never confirmed; bounding the row read behind a capability probe would
make the queue row disappear for the browser that needs it most.
 Normal 60-second maintenance calls
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
create a second consumer. Failure-path lease release uses a fresh command
identity for each dispatch, so a later lease cannot replay an older release
receipt. This does not change the stable queue identity used for accepted
Turn creation and lost-ack recovery.

`queue.injection.ack` is a receipt-backed owner-scoped command accepting
`{conv_id, user_id, queue_ids}` with at most 256 IDs of 256 characters (one
inbox drain). It returns `{removed, removedQueueIds}`. One queue lock and
transaction select only those IDs, delete unlinked non-human peer messages or
explicit background-command completion workflow rows, and renumber once.
Human, unrelated, cross-owner and any input/output/attempt-linked rows are
untouched. The transaction appends one `turn.upsert` replay change containing
only actual `removedQueueIds` and empty Turn/attempt upserts; no-op retries
publish no change. Command receipts recover lost acknowledgements; new calls
are naturally idempotent. The facade makes one command RPC and no queue list
queries per bounded drain; errors remain best-effort and do not cancel pairs.

## Durable worker jobs

`storage_worker_jobs` is the shared claim authority for accepted distributed
work. Owner-scoped `worker_job.enqueue` is semantically idempotent. Claims list
only complete handler kinds, increment attempt/fence and default to a 60-second
lease with 20-second heartbeats and a monotonic replay cursor. Omission never
means “claim everything”. Every heartbeat, cancellation poll and settlement
proves task/holder/token and an unexpired lease; expiry revokes the old fence.
Redis may wake claimers but never decides state or correctness.

`worker_job.claim_horizon` is the read-only half of a claim. For the same
explicit kinds and the same authority clocks it answers `claimableInMs`: 0 when
`claim_next` would claim now (a due queued/suspended job, an expired
current-epoch lease, or a lease from another authority lifecycle), the delay to
the earliest availability or current-epoch lease expiry, or null when no such
job exists. Native TofuDB serves it on the snapshot query lane from the same
bounded lease ranges and per-kind priority summary a claim reads, without the
writer mutex or a WAL barrier; SQL runs the claim's indexed probes in a read
transaction without row locks or a writer job. It grants nothing:
`claim_next` stays the only claim and re-validates under the writer, so a stale
answer costs at most one empty exclusive claim, never a second holder.

`turn.attempt.dispatch_worker` atomically binds the accepted Attempt to a
deterministic task, enqueues one `conversation-attempt` job and captures the
pending sync event. Payloads carry durable conversation/Turn/Attempt references
and an explicit `PrincipalContext`, never projections or request-local provider
handles. Lost acknowledgements resolve the same job; legacy `@dispatching` or
another executor binding fails closed instead of creating a second authority.

Durably bound Attempts require `worker_claim` at `turn.attempt.start`,
`turn.event.record` (including every batch entry), `turn.visible.sync`,
`task_results.checkpoint` and standalone task `event.append{,_batch}`, as defined by
[worker publication authority](../contracts/worker_job_lifecycle_v1.json).
The stored binding selects the authority; an omitted proof cannot bypass it.
Publication validates the current holder/token, user, kind, Attempt and
backend-owned lease clock/epoch in the same transaction. SQL holds the job row
until commit; native TofuDB retains its exact OCC dependency. A refusal raises
`database_conflict` and rolls back Turn changes, task events, replay and
settlement, including a Turn batch's previously applied entries. Standalone
replay batches retain independent sibling results: a stale row returns
`worker_claim_stale` and is never acknowledged or pushed. Flow phases borrow
their immutable root Attempt's claim. Cancellation refuses start; an entered
Attempt's current holder may still drain evidence and checkpoints. Without
durable `startedAt`, only terminal cleanup is permitted. Lost/expired holders
cannot drain. Legacy in-process bindings
need no additional job read and accept no worker proof. The context's
`publication_claim()` provides explicit evidence, never an effect capability.
**Problem:** refusing all nonterminal writes after Stop stranded an already
accepted event at the FIFO head, preventing the terminal behind it from
settling. **Decision:** cancellation withdraws new execution while current
ownership retains its duty to commit received evidence. **Alternative rejected:**
discarding or bypassing the FIFO head loses accounting/effect evidence; allowing
an expired claim to drain would defeat fencing. Backend entry evidence, not a
caller flag, selects the cancellation drain rule.

**Effect admission:** the common tool owner requires the initial `tool_effect`
`entered` event's exact durable ACK before handler entry. That frame has no
reconciliation reference and therefore requires an uncancelled claim inside
the publication transaction. Turn wrappers (including terminal and batched
frames), Flow phases and standalone replay enforce the same rule. Later
reconciliation references and returned/unknown evidence may drain. Treating
every entered Attempt write as drainage would allow a new effect after Stop;
checking cancellation only in the caller leaves a race with its durable commit.
This closes root tool admission only; it does not qualify provider or child
admission, or turn a receipt into permission to replay an unknown effect.

For `conversation-attempt`, queued cancellation keeps the existing job claimable
until its domain handler commits the terminal Turn; transport cancellation alone
cannot orphan a pending Attempt. `abort_attempt` commits this control intent
before local signalling and never invents a terminal writer on registry miss.
Internal cancellation carries its cause and provenance in the versioned
conversation cancellation envelope within the existing reason field. Both
local and remote carriers consume the first acknowledged cause; later Stop
requests cannot overwrite it. Oversized control messages are refused by the
existing wire contract, never truncated into misleading attribution.
`conversation_sync/worker_execution.py` reconstructs the common executor from
accepted state and joins it in the claim owner's thread. An entered Attempt or
unconfirmed physical/account settlement is not replayable from a lease alone.
Legacy `turn.recover` and `task_results.recover_running` exclude every
`conversation-attempt:` binding, including Flow phases and expired leases.
The native restart indexes omit these bindings; old index entries are verified
against canonical identities and removed without changing execution state.
Problem: API startup previously treated another process's live task as an
orphan. Decision: recovery follows durable execution ownership. Alternative
rejected: excluding only currently live leases would misclassify unknown
effects after a lease expires. Ordinary legacy recovery remains enabled.
Production kind registration, effect/crash recovery and independent process
composition remain unqualified; see the [architecture migration](ARCHITECTURE.md).

**Problem:** process-local commit callbacks strand an idle execution process when a
producer commits elsewhere or exits before delivering its acknowledgement.
**Decision:** [worker transport control](../contracts/worker_control_v1.json)
provides an authenticated, receipt-free authority watch. SQLite/PostgreSQL stage
queue/cancellation hints in the operation's session and publish at the adapter's
COMMIT boundary, including ambiguous delivery. Native TofuDB publishes from the
committed authority transaction. A cursor read precedes the queue probe; the next
wait compares its epoch and independent queue/cancel generations before sleeping.
This closes the subscribe/probe race without polling the job table. Pending waits
retain only cancellation IDs received during that wait; a response gap refreshes
active claim proofs. Restart changes the epoch. Shutdown wakes and joins waiters.
**Alternative rejected:** a caller-side wake after RPC success loses committed
work after producer death; a timer scanning an empty queue adds recurring storage
work and command receipts without fixing ownership.

`lib/durable_worker/service.py` owns demand execution, read-only claim horizons,
lease supervision and joined shutdown once. The automation service supplies its
existing policy. `StorageWorkerWakeSource` is the cross-process transport port;
its deadlines/retry cadence reuse the existing worker heartbeat/failure contracts.
Queue changes wake the coordinator only; cancellation wakes matching monitors,
which re-read authority rather than treating a notification as permission. No
worker ceiling, new timing knob, notification journal or second task registry is
introduced. Production conversation registration remains separately qualified.

[Worker lifecycle](../contracts/worker_job_lifecycle_v1.json) owns the states.
Live-fenced `worker_job.defer` sets `suspended`, releases the lease and indexes
availability. Resume advances the fence. Exact replay cannot extend the wait;
migration rebuilds the same queue and priority summary. Other queued kinds
settle cancellation immediately; running work acknowledges it. Suspended cancellation wakes
domain cleanup and remains nonterminal while external effects are unknown. PostgreSQL
locks the current job through cancel/defer/progress publication; claimers skip it.
Cancellation racing deferral forces immediate availability unless the domain
already acknowledged that sequence; acknowledged cleanup can wait without a busy loop.
The native application adapter omits owner injection for this strict
lease-fenced request; session authority remains authenticated.

`lib/durable_worker/` registers no production kinds. Claimable kinds must prove
durable replay, billing/admission settlement, cooperative cancellation and effect
fences; declarations alone are not proof. Domain cleanup/accounting settles before
worker completion; unconfirmed cleanup leaves the job reclaimable after lease
expiry. Suspension retains no executor/heartbeat thread; pending heartbeats are
joined, and cleanup discovered during teardown remains lease-supervised.

The chat composition root still uses in-process one-shot Attempt dispatch.
Worker autoscaling remains disabled until durable executor reconstruction,
crash-injection E2E for all four properties, and API admission/cancellation use this
authority end to end; cross-Pod takeover is not yet supported.

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

### Orphaned sidecar lease recovery (2026-09-21)

**Problem.** A UI-triggered restart deadlocked the server: the manager's 12s
SIGKILL escalation fired before the worker finished its graceful shutdown
sequence (the storage-sidecar stop is the last step), orphaning the native
Tofu-DB authority. The lease stamp's `owner_kind: "tofudb"` was not a known
kind to `server_manager.read_storage_lease_status`, so the holder classified
as `unknown`, skipped the bounded sidecar drain grace, and every subsequent
start failed closed with "refusing to start a second storage authority" until
manual intervention.

**Decision.** Three layered defenses in `server_manager.py`, all keyed off the
OS flock as sole lease authority:

1. Classification: the stamp kind `tofudb` normalizes to `storage_sidecar`,
   and `_legacy_storage_lease_owner` recognizes the `tofu-db serve` command
   line, so the native authority gets the same 10s drain grace as the Python
   sidecar.
2. Orphan reaping: when the drain grace expires and the holder is still a
   verified same-host process whose command line still matches a storage
   sidecar (PID-reuse guard), the manager sends SIGTERM, waits up to 15s for
   the lease to clear, then SIGKILLs as the final backstop. Unknown, foreign,
   and offline-maintenance holders remain fail-closed.
3. Escalation budget: the manager's default terminate timeout rose from 12s
   and now mirrors the worker's configured graceful-shutdown hard budget
   (`TOFU_SHUTDOWN_HARD_SECS` from the spawn environment or project .env,
   default 30s) plus a fixed 5s margin, so SIGKILL stays the outer backstop
   to the worker's own deadline instead of pre-empting it mid-sequence.

**Alternatives considered.** Relying solely on the close-on-exec EOF watcher
was the status quo and demonstrably insufficient once the worker is killed
mid-shutdown. Reaping any lease holder regardless of kind was rejected: an
unverifiable or foreign authority must block start, never be signalled.
Shrinking the worker shutdown sequence to fit 12s was rejected: the sequence
is already individually bounded, and the sidecar stop must remain last so
background producers never observe a storage outage.

## Retention and size bounds

- Streaming task frames: 6 hours.
- Structural request-inspector events: 30 days.
- Settled attempt replay events: 1 day in personal mode, 7 days in distributed
  mode; the permanent turn projection is unaffected.
- Per-generation-attempt timing traces are durable user state, not replay
  transport. Schema 46 owns the bounded document in
  `storage_generation_attempts.timing_trace_json`; settlement freezes server
  spans and user-visible phase history there and mirrors the terminal snapshot
  into the current `projection.timingTrace`. Owner-scoped
  `turn.perception.record` updates only the small attempt document under the terminal-event
  lock. Tofu-DB's sole effectless command IR step still commits its owner-scoped OCC mutation
  without Turn/revision/sync/receipt/outbox growth or a generic exemption. Event pruning never deletes
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
  externally addressable generation attempt. Native Turn authority also keeps one compact deletion directory per Turn: at most 64 Attempts and 64 KiB, with a 16 KiB versioned tombstone containing every retained Attempt identity. Creation and lifecycle changes update it in the same OCC transaction. Turn and branch deletion use the directory to retire all historical Attempt/event and timing/dispatch state without reading blob-capable Attempt payloads; claims remain fenced for seven days. Trash capsules retain and restore only the inert directory/count identity evidence, not Attempt documents or events, so later deletion/purge releases every claim without reviving work.
- Conversation sync replay rows: 7 days; expired cursors require a snapshot.

Cold chat replay uses owner-scoped `task_results.replay_get` for status/clocks
(with terminal content/thinking opt-in), `event.bounds` for count/min/max, and
bounded `event.list` pages for ordered bodies; raw records are not exposed.

`event.list(payload_types)` selects bodies only for those types (at most 64
strings of 1–128 characters); other rows retain sequence/time/type without body
decoding/transfer. Omission preserves full replay; empty lists select metadata.
SQLite/PostgreSQL keep identical task ownership and filter/page limits.

`event.latest(metadata_only:true)` returns final sequence/time/type without body
(default: full replay). After 200,000 detail rows, trace readers use it plus a
type-filtered terminal read and report partial `event-limit` coverage. Routes own
neither JSON decoding nor SQL; missing/foreign task results are indistinguishable.

`record.list(include_value:false)` returns only key/version/updated_at_ms with
unchanged prefix/cursor/order/scope/page bounds. SQL omits the value column;
native reads only the document header, not JSON/blob content. Default/true keeps
full reads; nonboolean flags fail protocol admission. Repositories use this to
check reconstructible roster versions without hydrating dialogue.

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
get one maintenance-priority writer admission. Pending birth and
terminal snapshots keep user priority and five bounded attempts; admission or
CAS pressure is never reported as an ownership fence.

Problem: a constant checkpoint deadline below the authority's queue tail
refused best-effort checkpoints under congestion, and the refusals repeated
(`lib/storage/deadline_policy.py`). Decision: nonterminal checkpoints share
one budget built from observed service time inside fixed bounds, 1-4 s for
the write and 0.25-2 s for a preflight read. No hardware probe is read, and an
observation only lengthens a budget above its fixed floor, so a slow host is
never refused work that the fixed floor admits ("Hardware never limits work",
`docs/RESOURCE_CONTROL.md`). Tests that assert the budget drop prior samples
through `reset_storage_service_observations()` and pin the fixed floor.
Alternatives rejected: a fixed constant brings the congestion refusals back;
comparing against a recomputed policy answer let the asserted bound move
with samples left by earlier tests (1 s idle, 4 s under load).

The additive `tofu.task-results.checkpoint.cache-settings/v1` capability commits at most two bounded facts to owner-qualified conversation settings with the task snapshot; no new schema, wire fields or command receipt.
- `cache_prefix_hwm` is positive and monotonic-max; `last_turn_cache_read` is nonnegative and LWW on a new task write. Observed zero/floor reads replace older warm baselines; missing/null/invalid usage omits the candidate.
- Owner-local caches distinguish absence from zero. Identical replay after ambiguous acknowledgement may repair/increase HWM but repairs last-read only if absent/invalid, never replacing a present zero or a newer value.
- Responses echo authoritative values and a commit bit. The manager clears candidates only after exact guard/capability echoes; running checkpoints retain the read candidate until terminal task-result publication. Children/inline/transients never publish conversation cache facts.
- Old Sidecars and legacy unscoped callers retain serialized best-effort per-fact compatibility; a protocol validation error never downgrades to an out-of-transaction write. Existing v1 peers rejecting zero require coordinated upgrade.

**Problem:** zero conflated with absence resurrected stale warm predecessors.
**Decision:** keep task-result publication as the sole scoped durability authority.
**Alternative rejected:** scope teardown writes bypass parent/status/CAS fences after failed staging and break atomic publication.
Process-local completed predecessors and the 30-second owner cache are not distributed ordering: a crash before terminal publication preserves the previous durable baseline; concurrent root turns/replicas observe last committed publication, not logical turn-start order. A distributed turn-order fence requires a separate contract change.

- Sidecar rate-limit events: every row carries its originating 1-second..7-day
  window as an exact expiry; each later check removes at most 256 globally
  expired rows through the age index, so one-shot identities are reclaimed
  without a table scan. Schema v43 resets only legacy rows that had no TTL.
- Raw provider archives: each attempt stores at most 16 MiB of compressed
  request/response blobs (`storage_raw_archives.stored_bytes`) against a 4 GiB
  global budget; over-budget puts degrade to bounded `partial` placeholder
  rows instead of failing the generation. The live total is the
  `storage_meta.raw_archive_stored_bytes` counter — bootstrapped once from a
  `SUM(stored_bytes)` scan under the budget lock, charged on insert, and
  debited by every archive delete path (conversation delete, Turn row-set
  retirement) in the same transaction — so a `raw_archive.put` never scans
  the archive table, and its post-insert metadata is assembled from the
  values just written rather than a blob read-back on the single writer.
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
- Local SQL conversation-search projection: demand-created query-only readers
  through the shared SQLite connection owner, launch-derived idle reuse, one
  independent projection writer, 8-row/2-MiB authority scan pages and no
  authority-writer backfill work. Close joins connecting/borrowed readers.
  Cache growth follows the indexed source corpus without a global byte quota;
  physical I/O failure retains explicit diagnosis and durable source authority.

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

`tool_result_artifacts` 存放有界工具证据（正文及 canonical manifest），不是永久用户状态或 transcript authority；网页长文本按 UTF-8 边界分为 4 MiB part，由 manifest 跨 part 签名续读；配额拒绝时显式 `retentionCoverage=unavailable`。
应用仅经 repository / `tool_result_artifact.put/read/search/prune` 访问，显式传 `user_id`；
`contracts/tool_result_query_v1.json` 统一声明 typed manifest、read/search/JSON Pointer、签名游标及资源上限。
引用暴露 CAS 摘要而非磁盘路径；每次 child 访问重验 owner/expiry，未获取/未保留的范围不得宣称完整零命中。
source、retention、view coverage 与 executionStatus 独立；图片原始 bytes 继续归既有 attachment authority。
同内容重写不缩短已有有效期；到期/缺失/超额明确不可用，绝不通过重执行伪造历史证据。

最终宿主执行结论或 source coverage 改变时，结果预算入口重新发布对应的
canonical manifest，继续引用原始证据；原 manifest 保持不可变。发布失败会
撤下过时的模型可见引用并明确报告 retention 不可用。已一致的结果复用不写存储。
SQLite / TofuDB 的固定样本均验证：16,800 字节原文只增加一个 286–289 字节的
序列化 manifest 正文，未复制原文；新记录仍受既有 owner、配额和到期策略约束。
语义与备选方案见 [结果预算契约](CONTEXT_BUDGET_POLICY.md#round-result-evidence)，
写入次数、体积及重启读回由 `tests/tools/test_canonical_envelope_verdict_backends.py` 验证。

**Spill-origin persistence (decision 2026-09-22).** An oversized tool result is replaced at dispatch time by a v2 envelope whose `artifactRef` is a bare content digest — by construction it carries NO provenance. The in-task `_artifactProvenance` map (source round + tool display) lets a same-turn `read_tool_artifact`/`search_tool_artifact` row label itself `R54 compacted · web_search: …`, but that map dies with its turn, so a cross-turn read-back fell back to the opaque digest label. Problem: recover the origin after the source turn. Decision: mirror the tiny origin record through the GENERIC `record.put/get` store (namespace `tool_result_artifact_origin`, keyed by content digest, value carrying `user_id` + `expiresAtMs`); the read boundary (`artifact_provenance`) consults the in-task map first, then the durable record when an owner is resolvable. Alternatives considered: (a) a dedicated `tool_result_artifact_origin.*` operation pair + owner-keyed table was rejected — provenance is a reconstructible advisory read-model, and `record.*` already owns the dual-backend-certified "small JSON by key" contract on both TofuDB and the SQL sidecar, so a parallel table would re-implement that subset (and force an IR `logicalTables` bump + new semantic-executor layout + differential scripts) for zero behavioural gain; (b) embedding the origin inside the artifact body's envelope was rejected — it pollutes the evidence the model reads back and breaks the content-digest dedup (two turns spilling identical content must share one artifact row). The origin record is overwrite-on-respill (latest source wins) and lazily expires with its artifact's TTL; a store hiccup degrades to the in-task map only and never fails a tool settle. TODO(enterprise): `storage_records` has no `user_id` column, so owner isolation is carried inside the value and enforced at the read boundary (the `task_results` pattern); a second real user requires keeping that filter and moving to an owner-keyed store.

**Model-facing semantic alias (decision 2026-09-22).** Problem: the sparse model projection of a spilled result quoted the raw `tool-result:<sha256>` pointer — 76 chars of random hex that tokenizes poorly and carries no meaning, and every `read_tool_artifact`/`search_tool_artifact` call had to re-quote it. Decision: storage keeps the bare digest as its ONLY key (CAS dedup, manifest children, signed cursor binding, and compaction anchors all depend on that shape), while the model-visible envelope projection carries a deterministic alias `tr:<slug>:<digest-prefix>` (semantic slug from the tool display, 8-hex digest prefix, schema stays within the 96-char ceiling). The alias is minted at settle time and persisted through the generic `record.*` store (namespace `tool_result_artifact_alias`, owner + lazy expiry in the value, same TODO(enterprise) caveat as the origin record); read/search handlers and the display relabel resolve it back to the digest BEFORE any storage call, so cursors keep binding the digest and a wrong alias fails loud (`artifact_unavailable`) instead of silently reading neighbouring evidence. Determinism keeps re-spills idempotent and replay/dedup projections stable; a same-slug same-prefix collision with different content lengthens the suffix (8/16/32/64; every claim is a version-guarded `record.put`, foreign or concurrently moved rows are never overwritten), so a wrong-content mapping is structurally impossible rather than merely unlikely. Mint/claim failures degrade to the digest projection, never failing a settle. Alternatives considered: (a) a truncated digest as the storage key was rejected — a collision would silently return the wrong evidence, strictly worse than a typed error, and would rewrite both backends' validators; (b) a monotonic short counter (`tr:7`) was rejected — it needs atomic counter state, is unstable across turns, breaks loop-breaker projection identity, and is no more memorable; (c) a bare semantic slug without hex was rejected — same-tool spills collide and a hallucinated slug cannot be distinguished from a wrong one; (d) re-encoding the digest (base32/base64) was rejected — ~20% shorter but still meaningless to BPE and to the model.

**Alias claim cost (decision 2026-10-28).** Problem: the claim sat on the settle path as get → put → read-after-write get, and the per-result and round-aggregate budgets minted the same alias twice, so one spilled result cost seven alias round trips plus the artifact put before the model could see it (`benchmarks/tool_dispatch_overhead.py`: 9 synchronous non-event storage operations per spilled `search_files`). Decision: a fresh alias is one `record.put` with `expected_version: 0` (both authorities treat an absent row as version 0); on conflict the row is read once and only our own (refresh) or an expired row is replaced at exactly the observed version, otherwise the suffix lengthens; a re-mint in the same task reuses its own claim while that claim keeps at least half its TTL. The same benchmark measures 4 operations per spilled result. Alternatives considered: (a) keeping read-before-write and dropping only the verify still costs two round trips and needs the verify to close the race; (b) minting asynchronously after settle was rejected because the collision check decides the alias text the model sees. Expected CAS conflicts (cross-turn identical re-spills) appear in the SQLite sidecar's writer log at WARNING.

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
- `database_invalid_payload`;
- `conversation_authority_conflict`, `storage_payload_too_large`;
- `plugin_storage_incompatible`.

**Search input classification (decision 2026-11-01).** Problem: both
authorities answered a rejected `conversation.search` cursor with
`database_protocol_error`, which the HTTP boundary answers as a server fault
([API_CONTRACT.md](contracts/API_CONTRACT.md) §2.3), so a browser holding a
stale or damaged continuation got a 500. Decision: a search cursor that is over
`cursorBytes`, undecodable, or bound to another authority, owner or query fails
as `database_invalid_payload`, the storage code answered as the caller's typed
400. A query over `queryCharacters`/`queryTerms` or containing NUL and a
non-boolean `page` flag (`contracts/conversation_search_v2.json`) stay
`database_protocol_error`: the application boundary
(`lib/conversations/search.py`) rejects those before dispatch, so storage sees
them only from a broken caller. Both authorities agree, pinned on both lanes by
`test_search_v2_pages_preserve_title_sources_and_long_message_tail` in
`tests/tofudb/test_tofudb_differential.py`. Alternatives rejected: making query
or page faults `database_invalid_payload` would report the calling
application's validation bug as the end user's mistake; leaving cursors a
protocol error turns an ordinary stale continuation into a server failure.

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

**`deadline_elapsed` classification (decision 2026-09-19).** The native daemon emits `deadline_elapsed` with the wire `retryable=false` because that single flag also gates command replay; both server variants are nevertheless replay-safe for an identical operation — the admission variant is a pre-dispatch rejection by construction, the execution variant aborts the transaction pre-commit, and committed commands always leave a durable `(tenant, owner, command_id)` receipt whose replay returns the recorded outcome. The storage.v2 client therefore classifies retry eligibility by code for `database_timeout`: reads replay freely, commands replay with the preserved command ID inside the same bounded aggregate budget; a connection-level `database_unavailable` without dispatch proof stays fail-fast. Turn-critical writers (`deliver_invocation_event`, Flow chat turn commit) add one bounded backoff layer (`lib/storage/retry.py`) on top so a sub-minute authority brownout degrades to a wait instead of a fatal turn error — the 2026-09-19 incident killed a healthy 15-round conversation at round 16 from a single transient `deadline_elapsed` under cgroup memory pressure. Alternatives considered: (a) marking admission `deadline_elapsed` retryable on the wire was rejected — it conflates the two server variants behind one flag and requires a daemon rebuild for a client-side policy; (b) the 2026-09-18 rejection of client read retry (`docs/STORAGE_TOFUDB.md`, "would only queue more full scans") predates the server-side mid-operation deadline budgets that now bound every scan, so the amplification it feared no longer exists; (c) leaving each caller to invent its own classification (the `lib/evolution/records.py` precedent) was rejected — the policy belongs at one client chokepoint.

**Availability retry spacing (decision 2026-09-22).** The flat
`retry_after_ms` hint (50–100 ms) is tuned for a healthy authority's
millisecond blips; against a cgroup-memory-starved authority every retry
re-pays the full execution window, and a fleet of caller threads
re-attempting at hint spacing is the storm that keeps the authority starved
(the supervisor was withholding recycling while 120 sync threads retry-spun
the pool). The storage.v2 client therefore keeps the hint for the FIRST
availability retry — a healthy blip pays nothing — and spaces consecutive
`database_timeout`/`database_busy`/`database_unavailable` retries
exponentially (0.5 s doubling, 8 s cap, jittered), but only when the
remaining budget affords the wait (4x); small-budget callers keep the hint.
Problem: millisecond retries amplify authority pressure. Decision: classified
pre-dispatch resource failures use congestion backoff, preserving command
identity and budget. Authority busy retains fast WAL retries. The native client
has no pool-size or long-request admission refusal; its pool size controls idle
reuse ([session lifecycle](STORAGE_NATIVE_SESSIONS.md)). Alternatives rejected:
shrinking deadlines churns sessions and discards block-cache warming.

**Writer budgets follow the observed authority service time (decision
2026-10-22).** The native authority admits foreground work through one FIFO
queue in front of one exclusive authority mutex and charges that queue wait
against the caller's deadline, so a writer's budget has to cover the
authority's end-to-end service time, queue wait included. The nonterminal
task checkpoint carried a fixed 0.5 s budget for its whole read/write
sequence, and the measured client-side latency of that operation against the
live authority put its p98.8 at exactly 500 ms: roughly one percent of
checkpoints were refused with `deadline_elapsed` — reported by the client as
`database_timeout` — and the refusal repeated instead of clearing, because the
availability backoff only engages for a caller whose aggregate budget can
afford the wait (a 0.5 s per-attempt budget leaves 1.0 s, below the 4x
patience rule), so every attempt of one episode re-entered the same
congestion. The ghost then migrates from writer to writer: `credential.touch`
(2026-10-14, 53 refusals in one day, later eliminated) was followed by
`task_results.checkpoint` (48 in five days, growing to 21 on 2026-10-22).
`lib/storage/deadline_policy.py` is now the one owner of that budget: it
records the authority's observed service time per operation (bounded recent
window, pooled fallback for a cold operation) and answers how long a writer
may ask the authority to take; the checkpoint's read and its write each get
their own budget, and the total remains bounded. The estimate is the
observed tail scaled by a factor, clamped into the caller's floor and ceiling,
so the budget tracks the authority in use and returns to it when the
authority gets faster. Measurement against the live authority (2026-10-22,
10 h uptime, 321k calls): the authority itself is healthy — 400 serial
foreground writes measured p99 32 ms, a 64-way concurrent burst p99 104 ms,
no refusals, and `system.health` reports no authority wedge — while the
daemon's own counters show the queue, not the work, is the cost
(`authority_wait` 469k acquisitions / 9,671 s against `command_execute`
217k calls / 1,618 s). Alternatives considered: (a) raising the constant was
rejected — it re-breaks on the next slower authority and never scales with
the host; (b) a per-writer constant was rejected — the ghost migrates from
writer to writer, which is what this class of fix has done for a week;
(c) a priority lane in the native authority was deferred — the storage.v2
wire carries no priority field, so the client's `priority` argument never
reaches the daemon (the SQL sidecar honors `user`/`event`/`maintenance`
through one fair priority writer), and the native admission is a single FIFO
queue, so a turn-critical writer cannot yet overtake a bulk one; that gap
needs an engine change and is not a client-side decision. The authority's
long foreground-holding phases (overlay drain 16.1 s, catalog publication
2.5 s) stay above the checkpoint's 4 s ceiling, so a checkpoint that lands
inside one still fails — the ceiling, not the budget, is the remaining
bound.

**Event appends follow the writer budget and never race their own batch
(decision 2026-11-01).** Problem: the standalone event path kept per-writer
constants. `event.append_batch` asked for 2 s per attempt, and a producer
waited 5 s for its batched row before sending the same row again as
`event.append` with a 5 s deadline. Under congestion the batch's attempts
were refused (`deadline_elapsed`) while the batch was still in flight, so
every producer that gave up queued a second copy of its row beside the
retrying batch, adding load exactly when the authority's queue is the
bottleneck (2026-10-31: at least ten `event.append` refusals and an 11-minute
event barrier on one task). Decision: the batch RPC, a producer's wait for
dispatch, the flush barrier and the `event.append` fallback all take their
budget from `storage_write_deadline`, with each path's existing budget as the
floor (the batcher's 2 s, the event log's 5 s), so observed service time only
raises them. A row still queued when the wait expires is withdrawn and takes
the single natural-key fallback, as before. A dispatched row's producer awaits
the batch outcome, bounded by the batch RPC's own deadline or the worker's
exit, and falls back only after that batch settled with a failure; the
persistence wait still retries transient failures. Alternatives considered:
(a) keeping the in-flight fallback: natural-key deduplication keeps it
correct, but it doubles queue load under congestion; (b) reconciling through
an `event.list` read: one more request in the same congested queue, and it
cannot see a row whose batch has not committed yet; (c) the policy's 1 s
default floor: it lowered this path's cold-start budget, and a cold 200-event
batch on a loaded test host expired before it reached the writer.
Measurement (`benchmarks/event_append_congestion.py`; FIFO authority with
foreign 750 ms writers through the same client, four CPU-bound threads, both
trees on local disk, medians of three alternating fresh runs): with four
foreign writers and a warm policy, 8 lanes × 5 events took 58 → 10 event RPCs,
18 → 0 refusals, 2.58 → 1.00 sends per durable row, 37 → 0 concurrent
duplicate sends and p50/p95 latency 8.6/9.3 s → 7.2/7.6 s; with no prior
observations the first batch's three attempts are still refused at the 2 s
floor and its rows fall back only after it settles (63 → 18 RPCs, 45 → 0
duplicates). With eight foreign writers (queue wait above both old
deadlines) the old path made no row durable within 180 s (120 refused event
RPCs, 32 persistence retries); the new path committed all 12 rows with 6
batch RPCs.

**Request-oversize classification (decision 2026-09-22).** Both storage.v2
transports enforce the per-operation request payload bound (8 MiB minus
frame slack; only `artifact.create`, `task_results.checkpoint`, and
`tool_result_artifact.put` may stream up to 64 MiB) by rejecting the request
before any byte is dispatched. Semantic budgets can legitimately brush that
bound — the model-continuation candidate alone may reach 8 MiB and rides
inside the terminal `turn.event.record` request next to the projection
patch. The rejection is deterministic and caller-correctable, so both
clients classify it as `storage_payload_too_large` (the v1 sidecar
payload-cap code): the socket client keeps the provably aligned pooled
session instead of retiring it, and the embedded client must not map the
engine's request-size admission to retryable `database_unavailable` (that
would re-issue an undeliverable request until the deadline). Before this
decision the socket path surfaced the rejection as `database_protocol_error`
("codec failure"), a shape `lib/turn_lifecycle.py::_is_frame_overflow_error`
does not match, so the designed degradation — slim settlement with the
continuation marked `frame_over_budget`, then a cooperative abort when even
text no longer fits — never engaged and the raw error failed the turn.
Alternatives considered: (a) lowering the semantic continuation budget below
the wire bound was rejected as the primary fix — the terminal projection
patch is not bounded by the candidate budget, so overflow stays possible and
must have one correct classification wherever it fires; (b) widening the
turn fence's string matching was rejected — the v1 taxonomy already owns
this exact class, and one boundary failure must have one code on every
backend.

## Cold-authority warm-up

**Problem (observed 2026-09-16/18).** Cold native entity reads on DolphinFS FUSE cost tens of seconds
(thousands of ~25 ms file opens) against a 5 s client deadline. Abandoned calls discarded warming
progress, sustaining a timeout storm; one uninterrupted ~37 s scan restored millisecond reads.
**Decision.** `lib/storage/warmup.py` runs representative replay-safe reads of conversation, record,
task-results, timer and credential families in a **post-ready**, lifecycle-owned keeper thread.
`TOFU_STORAGE_KEEP_WARM_INTERVAL_SECONDS` defaults to 300 s; zero disables periodic warming, not
that initial pass. `TOFU_STORAGE_WARMUP_DEADLINE_SECONDS` (180 s) and
`TOFU_STORAGE_WARMUP_BOOT_BUDGET_SECONDS` (600 s) retain per-op and initial-pass bounds.
Independent probes use the shared demand executor: launch capacity sizes idle reuse, never
an active read quota. Cross-pass merging applies only to the same client, operation and canonical
payload; completed entries retire. Genuine unstarted worker failure is a failed probe, and does
not prevent the other reads from starting. A timed-out shutdown join retains the keeper handle;
`lib/server/server_shutdown.py` stops the keeper before the storage authority; once the keeper exits,
shutdown cancels unstarted probes and retains physical ownership of running reads
until they finish. Warming is fail-soft; durable recovery stays fail-closed. A larger fixed pool
was rejected because it would retain a count queue and stale ownership semantics.
**Alternatives rejected:** eager native index loading needs engine certification (deferred, not excluded);
relocating the authority changes durability; raising request deadlines charges users repeatedly for cold reads.
**Measurement:** 32 independent probes rendezvous with idle reuse hint 1 in 11.70 ms,
with zero workers left after joined shutdown (`test_storage_warmup_executor.py`).
`packages/tofu-db/src/cold_read_tests.rs` gates VFS io-ops; `maintenance_reopen` in
`scripts/tofudb_perf_compare.py` measures cold/warm latency ([charter](STORAGE_PERFORMANCE_CHARTER.md)).

## Restart readiness and recovery

**Problem:** one measured recovery phase took 86.4 s and reported 7,088 Project Brain reconciliations;
the old retry policy slept 49.5 s across six attempts on a deterministic protocol failure. These are
old-path observations, not a claimed production speedup.
**Decision:** `lib/server/server_assembly.py::_init_database` keeps task/turn settlement and active work-item
reconciliation behind readiness. Only typed `retryable=True` failures retry within a bound; permanent
protocol/schema/programming faults log step, elapsed time and code, then fail instead of publishing ready.
Optional pending-resume/background-command enrichment retries only typed transient failures; cache
warming starts post-ready. `project_brain_startup.recover_active_work_items` filters snapshot status
before task reads: older native snapshots include terminal history, not just active work. Terminal rows
must not incur task replay/no-op writes every boot; genuine active rows still settle durably.
**Alternatives rejected:** swallowed recovery errors leave active orphans behind a healthy UI; blanket
retries cannot repair deterministic errors; dropping history/switching authority weakens user durability.
The SQL orphan probe reads only root task status with each backend's JSON operator and returns an existence bit; nested status text is not recovery authority. Problem: textual LIKE is invalid for PostgreSQL JSONB and can match nested objects. Decision: use typed root-field extraction; casting JSONB to text preserves false positives. Specs: `tests/storage/test_task_recovery_probe.py`, `tests/server/test_server_startup_recovery.py`, `tests/storage/test_storage_warmup.py`,
`tests/storage/test_storage_warmup_executor.py`, `tests/project_mod/test_project_brain_recovery_pagination.py`,
`tests/storage/test_boot_recovery_probe.py`.

Native Project Brain recovery — **Problem:** the 2026-10-20 startup spent 29.078 s
paging project recovery snapshots; checking active work materialized large terminal
history documents. **Decision:** `project_brain_recovery.rs` maintains one 40-byte
summary per project in the generated `project_brain_recovery_v1` namespace. The
summary binds its active-work bit to SHA-256 of the exact stored document header,
including its version/blob identity. Project mutation publishes both in the same
transaction; retirement removes both. Only a matching negative summary skips full
hydration. Missing, malformed or stale summaries use the original complete read;
active work still passes the existing durable startup reconciliation.

Legacy summaries are filled on the existing maintenance worker, at most 1,000
documents and one historical projection per round. It reads a pinned replica
outside the writer lock, then rechecks the exact header and owner cursor before
publishing. Summary and cursor commit atomically through maintenance writes and do
not generate logical-outbox business events. There is one bounded cursor per
registered tenant/owner, no new worker or full-history cache. Queries stay read-only;
the first upgraded restart can still pay legacy hydration until backfill completes.
**Alternatives rejected:** skipping all terminal-looking projects without a version
fence could hide newly active work; rewriting every old document on startup repeats
the delay; deferring active settlement beyond readiness admits orphaned work.
Verification: `project_brain` lifecycle/relink/outbox tests and recovery-summary
stale-source, cancellation, cross-owner and commit-fault tests.

Project-document recovery walk — **Problem:** on 2026-10-29 one restart spent
82.6 s of a 101.5 s storage-recovery phase in 24 recovery pages that returned one
active project. Older generations had left one `project_brain_by_updated_desc` row
per historical save, so a few summary-less inactive projects recurred on every
index page. Deduplication was page-local and a slow hydration ends a page at the
one-second yield, so the same projects were hydrated again on almost every page.
The backfill walked the same index, and its permanent completion marker never
revisited a project whose summary was missing or stale after its pass.
**Decision:** the restart walk and the backfill iterate the digest-keyed
`project_brain_documents` namespace: one row per project, whose scanned envelope is
the exact header its summary must match. The continuation is the last scanned
project (`ownerUserId`, `projectKey`), so settling or updating a project never moves
it across the cursor, and a complete walk hydrates each summary-less project at most
once and a negatively summarized project never. Update-index rows are not read; a
digest that differs from the envelope's recorded key fails closed. The backfill
keeps its source fence and one-projection rounds but uses a new cursor
(`backfill:documents`), so every project left without a valid summary gets one
pass; the completing round deletes the superseded `backfill` cursor in the same
transaction. A legacy project hydrated by one restart therefore gets a background
summary on the existing worker, and later restarts skip it once that is published. The SQL authority pages its one
row per project in primary-key order with the same cursor shape.
**Alternatives rejected:** a cross-page seen-set would have to travel in the
client-held cursor, grows with the project count and needs tamper protection;
skipping index rows whose timestamp differs from the document would hide a project
whose current row is missing; publishing summaries from the recovery read would put
writes back on its writer-free replica lane; raising the yield target only shortens
the page count on fast mounts. Measurement (`restart_walk_hydrates_each_project_at_most_once_despite_legacy_index_rows`,
5 projects, 1,500 repeated legacy rows, hot caches disabled): a walk that yields
after each hydration went from 1,204 hydrations, 1,204 pages, 301 returns of the one
active project and 23,268 VFS reads to 4 hydrations, 5 pages, 1 return and 41 reads;
without yields, 8 hydrations and 86 reads became 4 and 29. This is a deterministic
work count, not a production timing.

## Operations and verification

Operational tooling, backup/recovery, maintenance, projection checkpoints and
verification requirements are defined in [Storage operations](STORAGE_OPERATIONS.md).


Embedded FFI parser admission uses an independent budget derived from the same
launch memory-headroom observation; socket-frame ceilings retain their transport
scope. The canonical policy is `resourceProbe.embeddedRequestMemory` in
`contracts/storage_v2.json`. Its reservation lifecycle, measured residency and
rejected alternatives are recorded in [worker delivery](TOFUDB_WORKER_DELIVERY.md#independent-embedded-parser-admission-budget).

SQL RPC delivery — Problem: a process semaphore waited 100 ms and refused
connections above a configured count, even with free host resources. Decision:
each accepted socket starts an owned handler immediately; the legacy capacity
knob sizes only the kernel-clamped backlog hint. Active/waiting capacity is zero
(unlimited), with exact active/peak metrics. Real worker-creation failure proves
no dispatch and returns classified retryable unavailability; a worker already
published keeps cleanup ownership even if its start call raises afterwards.
Heap trim runs outside the acceptance/accounting lock and rechecks idle state.
Linux backlog sizing observes live `somaxconn` at construction (this host: 4,096
versus Python's compile-time 128); unavailable observations use the portable
platform fallback. The accept loop never waits for a count permit.
Authentication, preview-write denial, receipts, deadlines, half-close/EOF waits,
frame spilling and selected backend remain. Alternatives: larger caps retain
refusal; inline dispatch strands the accept loop; premature socket close risks
truncating a durable acknowledgement.
Measurement: 32 real authenticated RPCs at legacy hint 1 entered together
and completed in 60.00 ms with zero admission wait/refusal, returning handler
accounting to zero. Socket/worker cleanup, startup failure before/after
publication and a blocked heap trim have dedicated lifecycle checks. This is
acceptance/lifecycle evidence, not database throughput certification.

Command receipt demand admission: **Problem:** shared 4 MiB stored/decoded
receipt quotas refused otherwise supported domain commands and rolled back their
mutations. **Decision:** complete responses reuse existing exact command identity,
compression and native authenticated overflow blobs without private byte quotas.
The retained compressed codec has a u32 decoded length; responses outside that
representation use its existing plain encoding. Decoders grow from actual source
bytes and retain exact length, codec, compressed-stream completion and request
conflict checks. **Alternatives considered:** larger quotas postpone refusal;
truncated receipts break exactly-once replay; domain exceptions create different
receipt authorities. Tests exercise complete large compressible/incompressible
receipts, actual commits/replays, changed-request conflicts, corrupt envelopes,
offline restore and domain/outbox rollback faults. No worker or queue is added;
receipt growth follows retained command results and existing lifecycle ownership.

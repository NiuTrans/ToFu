# Native storage across application restarts

## Problem and decision

Stopping the native database with every Web worker discards its caches and
repeats authority open, WAL recovery and projection open on each application
restart. A production observation before this change spent 111.267 seconds
opening the authority and 135.326 seconds in application storage recovery.
Those are separate phases, not a controlled performance baseline for this change.

For a Linux lifecycle-managed source checkout whose configured native transport
resolves to `daemon`, `server_manager.LifecycleManager` now owns a persistent
`serverctl_pkg.native_storage_host` process. That host reuses `StorageRuntime`
and `StorageSupervisor`; it owns the native child, its close-on-exec stdin lease,
crash budget, readiness handshake and graceful shutdown. Restarting the Web
worker closes only that worker's authenticated clients. Storage caches and
background engine ownership survive. The same host now retains an explicitly
selected SQL Sidecar across Web restarts, publishing its existing authenticated
v1 handoff; native transport keeps the v2 identity contract. Explicit embedded
transport and explicitly supplied external connection files retain their owners.

This supersedes the worker-only lifetime decision in
`STORAGE_TOFUDB.md` (“Worker restarts re-open the authority in-process”).
The backend, data-layout resolver and durability selection are unchanged.
No migration, fastpath activation or weaker durability is implicit.

## Identity and readiness

`contracts/native_storage_connection_v2.json` owns the private v2 connection
document. The host publishes it atomically with mode 0600 only after a native
health handshake. Attachment validates protocol, backend, owner/tenant, project/data root,
engine content identity and PID/start identity before authenticated health.
The active projection/fastpath descriptors accompany that same generation.
An application cannot rebuild an external owner's executable during attachment;
direct launches still prepare before starting their own database.

Problem: reattaching after transient health failures reverified the executable
on disk, although deployment preparation can replace it while the admitted
native process still serves. This rejected that same process and exhausted the
application's recovery budget into permanent fencing. Decision: initial native
attachment retains full source/content admission; subsequent attachments compare
the private connection document's engine digest with that original admission,
alongside scope, project/data root, PID/start identity and authenticated health.
They do not reopen the deployment artifact. Managed child launches still verify
the exact admitted bytes and pin, and a changed service engine still requires a
drained replacement. Alternatives rejected: adopting the latest disk digest
silently upgrades a live application; disabling the crash breaker hides repeated
failures; checking deployment files during reconnect confuses disk preparation
with running-process identity. The existing in-memory admission is reused;
there is no additional persistent state or retention.

The host's own relaunches run a private copy of the admitted bytes, so a staged
deployment never refuses them; an attachment detaches at once when the
published process exits but keeps a live, starved engine through the
unresponsiveness budget; attachments are never latched by the restart breaker;
and deterministic refusals report `refused` instead of crash pressure. The
decisions and the private-copy retention are in
[STORAGE_OPERATIONS.md](STORAGE_OPERATIONS.md#admitted-engines-start-refusals-and-liveness).

The project-local service record is reconstructible control metadata. A separate
OS lease covers host creation through final native shutdown. The manager passes
that lease descriptor to the child before publishing its PID/start identity.
A missing or corrupt record cannot certify that a held service lease is free.
The record is never a substitute for the actual storage authority lease.

## Restart, upgrade and failures

| Event | Ownership and outcome |
|---|---|
| Ordinary worker restart/crash | Keep the same host and native PID; reconnect and run the existing application recovery gates. |
| Manager executable reload | Same manager PID/start identity retains the host and database. |
| Actual manager death | Host observes lost PID/start identity, joins startup/restart work and drains native storage. A successor waits for the old storage lease, then restarts the same generation for the preserved worker; when the generation changed it drains that worker as a planned restart, whose replacement starts the current host first. |
| Native crash | Existing runtime fences readiness, applies its crash budget, starts the admitted engine from its private copy (whatever a deployment did to the configured path) and publishes a new token/PID. Clients detach on the exited PID and reattach through their existing recovery owner. |
| Starved but live engine | Neither monitor gives it up before `TOFU_STORAGE_LIVENESS_UNRESPONSIVE_SECONDS` of continuous failure (longer under host overload); clients keep their attachment and resume when it answers. |
| Repeated native failures | Keep the terminal failure; automatic worker retries cannot reset the database crash budget. Explicit restart may replace the failed host. Attached clients are never latched themselves and reattach once a host publishes a healthy engine. |
| Unexpected host exit under a live manager | Latch a visible failure; do not respawn a new runtime with an empty crash budget on every reconciliation. Explicit restart may recover it. |
| Engine, scope, storage configuration or host lifecycle code changes | Prepare and validate while the old application serves; drain the worker before replacing its service. A live worker cannot adopt a different engine silently. |
| Full stop/offline maintenance | Stop worker, then host and native child. Maintenance still needs the real storage OS lease. |

The manager excludes a resident authority from orphan reaping only after proving
same-host manager/host PID identities and the authority's parent relationship.
Native crash recovery may coexist with a live Web lock only when that Web
process names the same project and private connection channel owned by the
verified host. Foreign Web owners and maintenance leases remain fenced.

Alternatives rejected: leaving an unowned native child breaks shutdown and
maintenance; reusing the SQL v1 wire for native storage mismatches protocols;
skipping recovery/readiness gates can report unusable data as ready; silently
adopting a new engine under a live worker breaks configuration ownership.

Problem (2026-11-05): a signalled Supervisor's host drained on manager EOF.
Its successor found the preserved worker alive, but the storage generation had
changed since that host started (host lifecycle code and generated storage
semantics). It refused to start another generation under the worker and asked
for an explicit restart that nothing performed; the worker retried attachment
for 1 h 40 min. Decision: when the departed host is gone, the authority lease is
free, its record names this host and a manager that is provably gone, and the
live worker attaches through that host's private connection file, the successor
drains the worker with the planned-restart path
(`stop(keep_desired_running=True)`). The next reconcile's planned recovery
replaces the record with the current generation, then spawns the worker. An
unchanged generation still restarts the host for the same worker. Failed
hosts, foreign hosts, another live manager and unbound workers keep the
explicit-restart boundary; the planned exit neither consumes nor resets a crash
budget. Alternatives rejected: starting the changed host under the live worker
adopts another generation silently (the worker also refuses a different engine
digest and stays fenced); keeping a host alive across manager death for
adoption needs a new handoff channel and an adoption timeout; waiting for an
operator leaves the outage unbounded. Verify
`test_successor_manager_restores_storage_for_the_preserved_worker` (a real
manager death with both generations).

The replacement identity is `native_storage_host.prepare_plan`: the admitted
engine path and digest, the search projection directory, the storage,
data-root and placement environment (`TOFU_STORAGE_*`, `TOFUDB_*`, `TOFU_DB_*`,
the data-root keys, `TOFU_LOCAL_DIR`, `XDG_STATE_HOME`), and the bytes of every
module the host runs while launching the native daemon: its lifecycle owners and
the automatic fastpath planner (`native_fastpath_auto.py`, `local_placement.py`,
`startup_budget.py`, `generated_v2_semantics.py`) and the search projection
launch preparation (`projection_paths.py`), whose capacity decision chooses the
directory each daemon launch receives. Problem (2026-10-27): the
planner runs in the resident host at every daemon launch, but its modules and
placement roots were outside the identity. A restart after changing only them
kept the old host, and a later daemon relaunch could import new planner code
under the old supervisor. Decision: any module or setting the host acts on at
daemon launch belongs in the identity; `tests/server/test_native_storage_host_plan.py`
varies each one. Alternatives rejected: hashing the whole repository replaces
the host, and pays a full authority reopen, on every application-only change;
importing the planner eagerly at host start removes skew within one host
lifetime but still leaves a stale host serving after the files change.

The manager's process lifecycle is shared through
`serverctl_pkg/resident_service.py`: PID/start identity, inherited exclusive
lease, private record publication and verified shutdown have one owner.
`NativeStorageService` supplies storage preparation, connection-file identity
and authority-lease recognition. The common lifecycle's source bytes also
participate in host replacement. This preserves the existing record format,
paths, crash budget and manager-exec behavior while making the same physical
ownership rules available to the execution host. A second independent PID/stop
implementation was rejected because it could disagree about physical release.

The historical native host/module and private-record names remain compatible
while the policy follows the selected backend. SQL generation preparation hashes
its storage implementation, authority configuration and configured secret-file
contents; only digests enter the private plan. It neither selects another store
nor enables the distributed preview. SQLite and PostgreSQL process tests attach,
write, detach, kill the owned SQL child and reconnect while the same storage host
retains the authority and rotates its transport token.

Headless execution uses the same resident owner and the common application
BootRunner/shutdown. Its generation covers Python application sources, the
planned interpreter's installed packages and launch policy; it excludes frontend
artifacts and reveals no secret values. Preparation
and application construction reject source changes. Lazy imports reuse
`PinnedImportSource`, retaining its documented limits for late package imports
and non-Python assets rather than copying a second application tree. Generation
discovery follows every importable package in those roots, including standalone
clients and worker packages; a separate package allowlist omitted valid owners.
The private execution record replaces one fixed file;
temporary preparation files are removed, and no source archive or history
journal is retained. A preparation measured 7,614 importable Python files in
7.842 seconds and a 336-byte compact plan. This is an inventory measurement,
not a throughput claim.

Problem: installed packages are executable source too, but the generation hashed
only checkout sources. Reinstalling a stale provider package (or upgrading any
dependency without a source change) therefore left the old host, with the old
modules already imported, serving across restarts. Decision: the plan also
records the runtime replica's environment fingerprint of the planned interpreter
(`describe_environment` plus `fingerprint` in `serverctl_pkg/runtime_replica.py`),
so installs, upgrades and removals replace the host at the next restart while
bytecode writes do not; a layout that cannot be described records none.
Alternatives rejected: hashing every installed file reads the whole environment
(10 GB here) per preparation; a second distribution-metadata scan would duplicate
the replica's install detector; requiring a full stop after installs keeps the
stale host as the default outcome. Measured on this FUSE host: 0.15 s to describe
the interpreter plus 0.46 s to fingerprint it per preparation.
Verify `tests/server/test_execution_plan.py`.

The personal Linux manager now composes the selected storage, a headless execution
host and an API host. An automatic native transport resolves to the shared daemon;
an explicitly embedded transport or separately assigned role retains its declared
topology. Both generations prepare before any old process drains. An unchanged
execution host survives API restart; storage replacement joins execution first,
and full stop joins execution before storage. No configured backend is substituted.

Problem: a healthy API can hide a dead executor. Decision: execution host exits
consume the manager's existing windowed failure budget once per service identity.
Recovery replaces only that host and only with the same prepared source/configuration
generation. API liveness remains visible while readiness reports the execution
outage. Code changes or an exhausted budget require the existing explicit restart.
Alternative rejected: giving each replacement a fresh budget permits an endless
crash loop; retiring the API on execution failure needlessly interrupts clients.
The bounded recovery record replaces one current value, not a history of tasks.
Verify `test_execution_composition.py` and `test_execution_application.py`;
the latter boots the common application against a real SQL authority while the
HTTP port is occupied, joins execution, and proves storage remains alive.
Restart activity protection now reads durable root jobs as well as local
carriers. Storage errors do not certify idle or consume an ordinary restart
approval; explicit force retains the existing approval boundary.

The resident protocol also passes an inherited manager-control pipe. Only the
manager owns its writer; its descriptor remains inheritable across manager exec
and is excluded from child `Popen` descriptors. The private record retains its
descriptor, device and inode so an exec'd manager can verify the same endpoint
and close only its own writer after physical host exit. Hosts verify the read
endpoint and treat EOF as withdrawal of manager ownership. This needs no new
polling schedule and works on the running kernel where `pidfd_open` returns
`ENOSYS`. It changes neither task authority nor the storage connection format.
The existing post-Popen PID publication handshake remains shared and bounded.

Draining the native daemon at host replacement or full stop gets
`restart_budget.NATIVE_DAEMON_STOP_SECONDS` (90 s); the manager's host stop
budget derives from it. Problem (2026-10-27): the host gave the daemon a fixed
25 s. On the FUSE authority a clean drain measured up to 13.4 s of maintenance
stop plus 17.6 s of closing checkpoint, so three of that day's seven stops were
SIGKILLed inside the checkpoint and the next open replayed the WAL for
51-102 s. Decision: the budget covers a measured slow drain with headroom; the
kill remains the backstop for a wedged daemon. Alternatives rejected: skipping
the closing checkpoint moves the same replay onto every open; keeping 25 s and
accepting replay costs more restart time than it saves and kills the authority
mid-checkpoint.

## History growth and maintenance admission

Problem: paged history manifests can retain more sources than fit in one leaf,
but compaction previously rejected retained counts above the leaf capacity.
Automatic maintenance passes the total history count minus its current source
slice, so normal history growth could terminate the engine repeatedly and
exhaust its restart budget.

Decision: both authority and engine compaction accept every positive retention
count. The physical page encoder still enforces its page size, and automatic
pruning still preserves the overlay replay suffix and bounded source slice.
No durable data is discarded to satisfy a count limit; this changes no storage
format, retention policy, or resident worker count. Alternatives rejected:
raising the leaf constant postpones failure and confuses physical layout with
logical retention; clamping the replay-protected suffix can lose recovery data;
increasing restart attempts repeats the same deterministic failure.
Regression tests exercise counts beyond one leaf and `usize::MAX` through both
entry points, zero rejection, continued writes and reopen. Existing paged-history
and compaction fault tests cover physical publication and recovery.

## Verification and measurement

`tests/server/test_native_storage_service.py` exercises real native clients and
manager transitions on disposable data. It verifies exact conversation recovery,
three detach/attach cycles with unchanged PID, native SIGKILL/token rotation,
manager exec retention, manager-only SIGKILL cleanup, warm worker restarts,
configuration replacement and full-stop lease release. Use a source-certified
`TOFU_TEST_TOFUDB_BIN`; tests never build an engine or open production storage.
`tests/storage/test_native_storage_connection.py` covers rejected identities,
failed-handshake client cleanup, reconnection after deployment artifact changes,
and cancellation winning a late handshake. The resident-service tests also
replace the executable by rename on disposable storage, then verify runtime
recovery, unchanged native PID and preserved conversation data, and that an
engine SIGKILL after such a deployment relaunches the admitted bytes and the
application reattaches to them.

One local disposable run measured native attachment at 0.656, 0.584 and 0.535
seconds, and complete minimal-worker restarts at 3.32 and 4.59 seconds. These
are component/minimal-application measurements, not production outage claims.
The full application retains task recovery, import, worker and readiness gates;
its separately measured timing must not be inferred from attachment latency.
After removing the synchronous optional login probe, two complete application
restarts measured 6.549 and 7.073 seconds from process spawn to ready, and
14.410 and 15.990 seconds from request to ready (including preparation while
the previous application still serves). A separate observation during concurrent
native tests measured an 11.870-second readiness outage and 28.884 seconds from
request to ready. These observations establish no production SLO or zero-downtime
claim; load and shutdown work affect the full chain.
The first activation, real manager death and engine upgrades still open storage.

Persistence and retention are specified in `STORAGE_OPERATIONS.md`.

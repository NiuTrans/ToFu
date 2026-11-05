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
background engine ownership survive. Explicit embedded transport, SQL storage
and explicitly supplied external connection files retain their existing owners.

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
| Actual manager death | Host observes lost PID/start identity, joins startup/restart work and drains native storage. A successor waits for the old storage lease before recovery. |
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

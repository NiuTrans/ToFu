# Fast-path storage authority — operator troubleshooting

The SQLite write front may run on a measured-local filesystem while the
durable truth stays on the persistent data dir as a continuously-shipped
shadow (`data/fastpath-shadow/`). See `lib/storage_sidecar/fastpath.py` for
the design invariants. SQLite's own [WAL documentation](https://sqlite.org/wal.html)
also treats a network filesystem as outside WAL's supported topology; Tofu's
single-host preflight reduces risk but does not turn network fsync tail latency
into local storage.

## Knobs

| Env | Values | Default | Meaning |
| --- | --- | --- | --- |
| `TOFU_STORAGE_FASTPATH` | `off` / `auto` / `required` | `off` (opt-in since 2026-08-20) | `off`: never relocate. `auto`: probe and relocate on a measured win. `required`: refuse to boot unless relocation activates. |
| `TOFU_STORAGE_FASTPATH_DIR` | path | unset | Explicit persistent-local candidate. Skips the same-device short-circuit; the measured benchmark still gates activation. |
| `TOFU_STORAGE_FASTPATH_MIN_SPEEDUP` | 0–1000 | `3.0` | Minimum (data-dir fsync median ÷ candidate fsync median) to activate. The benchmark is recorded either way. |
| `TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S` | 30–3600 seconds | `900` | Immutable hard limit for a fastpath boot. Valid progress renews only the 30-second stall watchdog; it never extends this limit. Hypercorn reserves another 60 seconds for later required startup phases. |

Activation is fail-closed: any failed probe (permissions, WAL semantics,
free space, insufficient measured win) leaves the authority on the data dir
exactly as before. Check `system.metrics` → `fastpath` for the verdict and
the measured numbers.

`server.py`'s dotenv phase applies an explicit fastpath choice before the
Sidecar boots; the code default stays `off`. Auto discovery may reuse a
surviving deployment-keyed temporary front for compatibility, but it never
creates or restores an absent implicit `/tmp` front. Container recreation
would otherwise turn every cold launch into a database-sized copy. New
activation therefore requires a measured persistent-local candidate or an
explicit `TOFU_STORAGE_FASTPATH_DIR`.

If a durable shadow exists but no front can be selected, startup fails closed
instead of opening the stale pre-fastpath `data/tofu.db`. To leave fastpath,
stop Tofu and run:

```bash
python3 scripts/storagectl.py retire-fastpath --confirm
```

The offline command first prefers the uniquely verified surviving local front,
including its unshipped crash tail; only a genuinely lost front falls back to
the durable shadow. It copies and WAL-checkpoints that source, validates
integrity and authority identity, atomically publishes it as `data/tofu.db`,
and moves both the former classic image and complete shadow under
`data/backups/` for rollback. It deletes no durable user state. Set
`TOFU_STORAGE_FASTPATH=off` before the next start; remove the retained rollback
artifacts only after application-level verification.

First activation copies the current classic database and any WAL byte for byte.
Before deciding and again immediately before the copy, the Sidecar requires the
full source size plus a reserve equal to 5% of the source, clamped to 1..8 GiB
(and never less than the general 2 GiB floor). An insufficient candidate is
rejected before authority bytes move, so a large database cannot fill the OS
volume mid-copy. The private temporary copy is fsynced and checkpointed every
256 MiB. A killed process resumes from the last durable offset only when the
classic database/WAL device, inode, size, mtime, ctime, and bounded content
witness are unchanged; otherwise it deletes only its private seed artifacts
and starts again. Files up to 1 MiB are hashed fully; larger files hash 32
deterministic 16 KiB ranges, so identity checks never add another full scan of
a multi-GiB authority. The classic authority is never modified. An interruption
after atomic install but before lineage-manifest publication is also recoverable
and is never treated as a foreign front.

During the copy, the child emits credential-free phase and byte counters on its
private parent control pipe. Advancing counters renew a 30-second stall
watchdog; opaque fsync/replay phases may emit an explicit heartbeat. Neither
can renew the hard timeout above. A large first activation therefore remains
bounded, visible in logs, and resumable instead of repeatedly truncating the
same temporary file at a fixed readiness deadline.

Each 256 MiB checkpoint also advises the kernel to release only the completed,
fsynced source and destination ranges from page cache. The hint is best-effort
on platforms that support `POSIX_FADV_DONTNEED`; unsupported filesystems keep
identical copy and durability behavior. This bounds one-shot migration cache
pressure without evicting uncopied ranges or weakening fsync.

## Shadow snapshot I/O budget

For a newly seeded front, a one-use durable provenance record fingerprints the
classic source and the installed local image. If both remain byte-identical,
the shipper publishes shadow generation 1 by hard-linking the immutable
classic database on the same durable filesystem and copying only a non-empty
classic WAL. This consumes no second database-sized allocation and performs no
database-sized network write. Failure or unsupported hard links fall back to
the ordinary snapshot path.

A later rebase first completes the shipper-owned `TRUNCATE` checkpoint. Normal
WAL-mode commits then append to a new WAL while the database image stays
unchanged because no other component may checkpoint it. The shipper performs
one bounded sequential image copy, rejects publication if the source inode,
size, or mtime changes, then pairs it with the complete concurrent WAL prefix.
This avoids SQLite backup's page-at-a-time destination writes and restart-on-
source-change behavior. On the 87.6 GiB live authority, that old path repeatedly
returned to roughly 646 MiB under ordinary commits and therefore could not
finish; the new path never restarts the image copy for a concurrent commit.
The live replacement published an 87,387,230,208-byte generation in about
eight minutes while the API stayed ready. A read-only postcondition matched
259 deterministic 4 KiB ranges against the stable local image and verified the
SQLite byte count, schema version 40, and authority UUID. This is operational
evidence, not a substitute for the explicit full integrity workflow.

Because every rebase still writes one complete database image, its budget is
scale-aware. The effective hard WAL budget is one quarter of the authority size
with a 64 MiB floor, capped by a launch-time resource default equal to two
percent of free disk and never more than 16 GiB. The local WAL and its durable
mirror therefore stay within a four-percent hard envelope. At
shipper construction the local-front and durable-shadow filesystems are both
rechecked and the smaller two-percent ceiling wins; an unavailable secondary
probe keeps the bounded launch value. On the 82 GiB live authority the resolved
threshold rises from 8 to 16 GiB. A frozen
pre-change log window contained 19 complete 82-GiB generations (1.56 TiB of
sequential database-image writes); under equal WAL churn the doubled threshold
projects roughly ten generations and 0.7–0.8 TiB fewer full-image writes. This
is policy evidence, not a post-deployment saving claim. Operators may lower the
ceiling with `TOFU_STORAGE_FASTPATH_WAL_REBASE_MAX_MIB` when recovery-tail or
disk headroom matters more than rewrite frequency.

The shipper proactively starts a rebase at 15/16 of the resolved WAL budget,
reserving one sixteenth (1 GiB on the 16-GiB live budget) for commits that race
the checkpoint. The full budget remains a hard admission watermark, not a
byte-exact file quota. Every physical commit observes the local WAL. Only at or
above that hard watermark are later jobs refused before `BEGIN` with retryable
`database_busy`; the raw shipper checkpoint bypasses the fence and clears it
after truncation. One request-
bounded transaction or at-most-64-job group-commit segment already in flight
remains atomic and can cross the watermark (segments use a soft 0.25-second
split and the existing hard transaction deadline). This closes both the
long image-copy window and the
pre-checkpoint capacity-failure window without cancelling a resumable copy or
holding the writer for the full eight-minute image transfer. Publication still
repeats the full snapshot-plus-tail capacity check and refuses an unsafe
generation. Persistent pressure means capacity or shadow progress needs
operator attention; reads and the prior durable recovery point remain usable.
WAL-size observation is fail-closed: a non-`ENOENT` stat failure keeps the
fence engaged and increments an explicit failure counter until a trustworthy
observation succeeds. The added hot local `stat` cost measured 870 ns/physical
commit (500,000 loops, best of five, 2026-08-29), about 0.06% of the previously
measured 1.41 ms median physical acknowledgement; group commit amortizes that
cost across logical jobs, and the change adds no thread, queue, or cache.

The canonical nightly SQLite backup uses the same checkpoint owner instead of
starting a second live page-wise backup. It forces one stable shadow generation
under the ship lock, then pins that standalone image into `data/backups/` with
a hard link on the same filesystem (or one sequential copy across devices).
Full integrity and SHA-256 verification remain mandatory, and the scanned hash
pages are released from cache best-effort. This preserves the existing
single-file restore contract while removing destination-page write
amplification and source-change restarts. A backup deadline also bounds waiting
for an in-flight ship pass and the cross-device copy.

Every startup scans at most 64 exact shipper-private temporary names and
unlinks one only when the PID encoded in that name is no longer alive. This
reclaims interrupted snapshot copies in bounded work; live-owner artifacts,
directories, symlinks' targets, and unrecognized names are retained. Reclaimed
file and byte counts are exported in the shipper metrics.

## Recommended activation sequence for a large authority

1. Keep `TOFU_STORAGE_FASTPATH=off` and run the read-only
   `python3 scripts/storage_deep_clean.py --analyze`.
2. Schedule downtime, stop Tofu, and run
   `python3 scripts/storage_deep_clean.py --offline --confirm`. This compacts
   the classic source first, verifies it, installs deferred performance
   indexes, atomically publishes it, and retains the old file for rollback.
3. Start once on the classic authority and verify health, integrity, row parity,
   and representative conversations. Retain the rollback file until that
   verification is complete.
4. Choose an explicit local SSD directory with enough capacity, accept the
   local-disk-loss RPO below, set `TOFU_STORAGE_FASTPATH=auto` and
   `TOFU_STORAGE_FASTPATH_DIR`, then restart. Confirm a measured activation,
   commit p95/max, and shadow ship lag before declaring the window complete.

Do not explicitly point the front at RAM/tmpfs for a durable authority. A
deployment-scoped persistent local SSD gives the latency benefit without
making every ordinary reboot look like local-disk loss.

Auto candidate dirs are namespaced per data dir
(e.g. `/tmp/tofu-fastpath-<uid>-<data-dir-hash>`), so two deployments on one
host never share a front. A front whose local manifest names another
deployment's shadow dir — or none — is quarantined at boot as
`tofu.db.foreign-<timestamp>*` beside the front and never served
(2026-08-20 incident: production adopted a certification test's front from
the previously-unkeyed shared /tmp dir and served its 3 test conversations
while the real authority sat untouched).

## Metrics that matter

- `fastpath.benchmark.*` — the boot-time measured fsync medians.
- `fastpath.shipper.ship_lag_bytes` / `last_ship_age_s` — the RPO window:
  how much committed data is not yet on the durable dir.
- `fastpath.shipper.snapshots`, `snapshot_database_bytes_copied`, and
  `snapshot_wal_bytes_copied` — completed generations and the process-lifetime
  physical chunks written during the current Sidecar lifetime.
- `fastpath.shipper.snapshot_progress_bytes` / `wal_rebase_trigger_bytes` —
  durable current-copy progress and the proactive full-rebase trigger.
- `fastpath.shipper.wal_rebase_budget_bytes` / `wal_write_pressure_bytes` —
  the resolved hard admission budget.
- `fastpath.shipper.rebase_active`,
  `write_pressure_active`,
  `local_wal_bytes`, and `wal_write_headroom_bytes` — the live copy/admission
  state and remaining WAL headroom.
- `fastpath.shipper.write_pressure_activations` /
  `write_pressure_rejections`, `write_pressure_observation_failures`, and
  writer `write_admission_rejections` — how often the bounded fence engages,
  how much write work it refuses, and whether its resource meter failed closed.
- `fastpath.shipper.stale_artifacts_reclaimed` /
  `stale_artifact_bytes_reclaimed` — bounded cleanup of dead-owner private
  snapshot attempts during this Sidecar lifetime.
- `writer.commit_latency` — p50/p95/max commit latency histogram; compare
  against `benchmark.data_dir_median_fsync_ms` to verify the win is real.
- Prometheus mirrors these with the `tofu_storage_fastpath_*` generation,
  copied-byte, progress, budget, pressure, local-WAL/headroom, lag, and age
  series; alert on trend, not one sample.

## Crash semantics

- Process crash, local disk SURVIVES → zero loss: boot reconciliation keeps
  the front authoritative and the shipper forward-ships the crash tail.
- Local disk LOST (container rescheduled, tmpfs wiped) → bounded loss: the
  shadow holds every commit except the unshipped tail (≤ the ship lag above).
  Boot restores snapshot + WAL prefix automatically.
- Graceful stop ships the tail on the way down, so a planned restart with a
  surviving shadow loses nothing even if the front is wiped.

## Split-brain guard

If the local front and the shadow disagree on `authority_uuid`, boot logs
CRITICAL and falls back to the classic `data/tofu.db`. This means two
authority lineages diverged — do NOT just delete files blindly. Decide which
lineage has the newer truth (compare shadow `manifest.json` `updated_at`
against the front's mtime), then either wipe the front dir (keep shadow) or
wipe `data/fastpath-shadow/` (keep front) and restart.

## First-activation window

Until shadow generation 1 is published, the durable recovery point remains the
pre-fastpath classic `data/tofu.db`; a local-disk loss in that brief window
reverts to the classic file's state at seeding time. Verified hard-link
publication normally closes the window with metadata I/O only. If provenance
is missing/changed or the filesystem rejects hard links, the sequential rebase
runs on the shipper thread without blocking readiness; the window remains
logged (WARNING) and visible as `shipper.snapshots == 0`.

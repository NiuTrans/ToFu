# DB Durability Rollout Runbook (local-primary PG + Tier B PITR)
# DB Durability Rollout Runbook (local-primary PG + Tier B PITR)

> **⛔ WITHDRAWN 2026-08-06 (owner final ruling, epic pt_4d321fb8f1c2400c permanently closed):**
> "不要使用除了项目以外的路径来解决这个问题，/tmp这些路径不准用来部署db，会丢的。以后都不许想这个。"
> The `/tmp`-based local-primary migration below is **dead — do NOT execute any step of it**.
> The DB stays on the project's own directory (the legacy FUSE pgdata). The code
> machinery is preserved but INERT (opt-in only, default OFF; see
> `lib/database/_pg_seed.py`). This document is kept only as the record of an
> explored-and-rejected design.


> **Audience:** the operator rolling out the FUSE→local-disk database migration on a
> real cluster. Design rationale lives in `JOURNAL.md` §§7a–7i; this file is the
> step-by-step operational procedure and the safety stops.
>
> **One-sentence why:** the live PostgreSQL cluster must not run off the DolphinFS
> FUSE mount (WAL/locking over a network FS is unsupported and has corrupted us
> before). This moves the live cluster to local xfs (`/tmp` on this box) and uses
> DolphinFS only as a backup/replication target, with a seconds-RPO PITR recovery
> path for a fresh container.

---

## 0. Pre-flight — confirm the current state

```bash
# Which pgdata is the live cluster on? (expect the FUSE /mnt path pre-rollout)
ps -eo pid,cmd | grep '[p]ostgres -D'

# What does the resolver choose RIGHT NOW? (with no opt-in envs set, expect the
# populated legacy FUSE pgdata — the gate holds until a verified seed)
python -c "from lib.database.db_paths import resolve_pgdata_dir; from lib.runtime_paths import data_root; print(resolve_pgdata_dir(data_root()))"

# Confirm the local target volume is real local disk, NOT the volatile overlay
df -T /tmp        # expect a real block device (e.g. /dev/md0p1 xfs), not overlay
mountpoint -q /tmp && echo "/tmp is a distinct volume (good)"
```

**The split auto-engages on network mounts and the seed is DEFAULT-ON**
(since 2026-08-05, owner directive — plain `python server.py` must Just Work):
the first boot that finds the local pgdata unpopulated runs the one-time seed
automatically. `TOFU_DB_SEED_LOCAL=0` is the escape hatch to defer it.

## Environment variables (all default OFF / safe)

| Var | Default | Effect |
|---|---|---|
| `TOFU_DB_LOCAL_SPLIT` | auto (`/mnt/`→on) | Force the local-primary split on/off. Auto-engages only when the data root is a network mount. |
| `TOFU_DB_LOCAL_ROOT` | `/tmp/tofu` | Parent of the live local `pgdata`. Must be durable-across-process-restart local disk. |
| `TOFU_DB_BACKUP_ROOT` | `<data>/pg_backups` | DolphinFS durability target (dumps + `wal/` + `base/`). |
| `TOFU_DB_SEED_LOCAL` | `1` (since 2026-08-05) | **Default-on**: the one-time seed migration fires automatically on any plain start (heavy: full dump+restore before serving). Set `0` to defer. |
| `TOFU_DB_TIER_B` | `0` | **Opt-in** WAL archiving + base backups + PITR cold-start (seconds-RPO). |
| `TOFU_DB_BASEBACKUP_INTERVAL_H` | `24` | `pg_basebackup -X stream` cadence. |
| `TOFU_DB_WAL_ARCHIVE_TIMEOUT` | `30` | Per-segment archive hard timeout (FUSE-stall guard). |
| `TOFU_DB_RESTORE_DIVERGENCE_WARN_S` | `21600` (6h) | Channel-divergence CRITICAL threshold. |
| `TOFU_DB_STRICT_PG` | (unset) | Refuse to start (vs SQLite-fallback) if a recoverable cluster/backup exists but won't restore. Recommended ON in prod. |

---

## 1. Rollout order (do NOT reorder — the safety depends on it)

### Step 1 — Seed the local primary (one-time, automatic on a plain restart)
```bash
# Nothing to export — the seed is default-on. Just start the server normally:
python server.py        # (or the usual restart script)
# (leave TOFU_DB_TIER_B unset for now — seed via a fresh live dump first)
```
To DEFER the seed on a given boot instead: `TOFU_DB_SEED_LOCAL=0`.
On this boot, `_ensure_pg_running` Step -1 will:
1. Ensure the legacy cluster is **up** (start it if down — do NOT let it fall back
   to the stale nightly just because it wasn't running).
2. Take a **fresh live `pg_dumpall`** of legacy (zero data-loss window).
3. `initdb` the local `pgdata` under `$TOFU_DB_LOCAL_ROOT` and restore the dump.
4. **Verify** the restored `conversations` count equals the source; on any
   mismatch it QUARANTINES the half-built local dir and stays on legacy.
5. **Flip in the SAME boot (2026-08-05)**: stop legacy, retarget local to the
   pinned port (`TOFU_PG_PORT`), start it, and continue the boot serving LOCAL.
   The old two-restart dance left an unbounded staleness window (writes between
   the two boots would never reach local) — the flip is atomic precisely so that
   window is zero. Only the server's own boot migrates (`TOFU_SERVER_PROCESS`
   marker set by server.py); side-process imports just attach.

Watch:
```bash
grep -aE '\[DB-Seed\]|\[DB-Flip\]|\[DB-Migrate\]' logs/app.log | tail -30
# expect: "[DB-Seed] SUCCESS — local seeded + verified …" then
#         "[DB-Flip] SUCCESS — local primary serving on :15439 …"
```

### Step 2 — Verify the flip right after that one boot  ⚠️ the gate
Do NOT skip this. The boot that seeded also flipped — confirm the live cluster
is the local one and carries every row:
```bash
PGGSSENCMODE=disable psql -h 127.0.0.1 -p 15439 -U "$USER" -d tofu -tAc \
  'SHOW data_directory'                        # expect /tmp/tofu/pgdata
PGGSSENCMODE=disable psql -h 127.0.0.1 -p 15439 -U "$USER" -d tofu -tAc \
  'SELECT count(*) FROM conversations'         # == the seed's verified count
ps -eo pid,cmd | grep '[p]ostgres -D'          # live postgres -D /tmp/tofu/pgdata
```
If `data_directory` is still the FUSE path, the migration did not flip — look
for `[DB-Flip]` / `[DB-Migrate]` CRITICALs (a failure marker
`/tmp/tofu/.seed_failed` then bounds retries to one attempt per 6h; delete it
to retry on the next boot after fixing the cause).

### Step 3 — Stale-reseed safety net (nothing to do)
If a boot ever finds the local copy POPULATED BUT STALE (legacy written more
recently — e.g. a rollback followed by new writes), the migrator parks the
stale copy aside and re-seeds automatically. Rollback remains
`TOFU_DB_LOCAL_SPLIT=0` + restart; rolling forward again is then just
re-enabling the split (the reseed re-syncs). 

### Step 4 — Enable Tier B (seconds-RPO)
Only after local is the confirmed primary:
```bash
export TOFU_DB_TIER_B=1
# restart
```
Now:
- The managed `postgresql.conf` block on the **local primary** gains
  `archive_mode=on` + `archive_command` (never written to legacy — verified by
  the pre-flip guard). Confirm:
  ```bash
  grep -A1 'archive_mode' /tmp/tofu/pgdata/postgresql.conf
  ```
- The **PostgreSQL Base Backup** scheduled task starts producing
  `$TOFU_DB_BACKUP_ROOT/base/<ts>/` via `pg_basebackup -X stream`.
- Completed WAL segments stream to `$TOFU_DB_BACKUP_ROOT/wal/`. Confirm growth:
  ```bash
  ls -t $TOFU_DB_BACKUP_ROOT/wal/ | head; ls $TOFU_DB_BACKUP_ROOT/base/
  ```

### Step 5 — Validate the PITR cold-start (optional but recommended, off-prod)
On a scratch copy / staging container: with a base + a WAL tail present and an
empty local `pgdata`, a cold start restores via PITR and recovers to the WAL
tail (not just the base). Watch for `[DB-PITR] PITR recovery SUCCESS — replayed
to WAL tail`.

---

## 2. Reading the signals

- **`[DB-Seed] SUCCESS … Next boot will resolve … to local`** — seed done; flip on next restart.
- **`[db_paths] … staying on legacy FUSE pgdata until the seed migration populates local`** — the gate is holding; local not yet seeded. Expected pre-seed and after a failed/quarantined seed.
- **`[WAL-Archive] FAILED to archive … returning non-zero so PG retains+retries`** — a single archive attempt failed (likely a FUSE stall). PG keeps the segment and retries; not fatal.
- **`[WAL-Archive] archiving has fallen behind (N consecutive fails …)`** — the DolphinFS archive target is stalled; RPO is degrading toward the last base backup and local `pg_wal` is growing. Investigate the mount; the live cluster is unaffected.
- **`db_restore_channel_divergence` CRITICAL** (audit + error.log) — at a cold-start restore, the Tier A dump end and Tier B WAL-tail end diverged by more than the threshold. **One durability channel is broken** (a stalled WAL archive, or a dead nightly-dump job). The restore still proceeds with the NEWER channel, but you must find and fix the stale channel:
  ```bash
  grep -a 'db_restore_channel_divergence' logs/audit.log | tail
  # tier_a_end vs tier_b_end tell you which channel is stale (older).
  ```

---

## 3. ⛔ STOP — do NOT retire the legacy FUSE cluster yet

The seed **preserves** the legacy cluster; nothing in the code deletes or retires
it. Retirement (removing `/mnt/.../data/pgdata`) is a MANUAL, IRREVERSIBLE step
that is explicitly gated on ALL of:

1. A seeded local cluster **verified row-equal** to the source (Step 2), and
2. The primary confirmed running on local across at least one real restart (Step 3), and
3. Tier B archiving confirmed healthy (base backups landing, WAL streaming, no
   sustained `archiving has fallen behind` CRITICALs), and
4. **Explicit owner sign-off.**

Until then: keep legacy on FUSE as the fallback. If anything goes wrong, force
`TOFU_DB_LOCAL_SPLIT=0` and restart — resolution returns to the intact legacy
cluster (the seed never fires with the split off; `TOFU_DB_SEED_LOCAL=0` also
defers it).

## 4. Rollback (at any point before retirement)
```bash
export TOFU_DB_LOCAL_SPLIT=0      # force legacy primary (the seed never fires
                                  # with the split off — no other env needed)
unset TOFU_DB_TIER_B
# restart → resolve_pgdata_dir returns the legacy FUSE pgdata; you are back to
# the pre-rollout state. The seeded /tmp/tofu/pgdata is harmless (ignored).
# (Without the split override, the DEFAULT-ON seed would simply re-verify the
# already-populated local and no-op.)
```

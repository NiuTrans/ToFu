"""PostgreSQL local-primary SEED migration (dump legacy → seed local pgdata).

Extracted from lib/database/_bootstrap.py (2026-07-11, Decoupling D sub-cut 2,
§10 signed off). The verify-gated opt-in seed pipeline: major-version compat
check, live `pg_dumpall`, conv-row count (verify gate), bring legacy up for the
seed, and _seed_local_pgdata_from_legacy itself. NO module globals (no shared-
mutable hazard). Its 12 call-outs into core (start/verify/restore/
backup helpers, some re-exported from _pg_ownership) resolve LAZILY via in-body
import to avoid an import cycle. _bootstrap re-imports the 5 seed fns
(explicit facade) so `_bootstrap.<name>` / `b._<name>` keep resolving for
_core.py + the db_seed test suite.
"""

import os  # noqa: F401
import shutil  # noqa: F401
import subprocess  # noqa: F401
import time  # noqa: F401

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)


# ── Lazy shims: core/backup helpers the seed calls (resolved at call time). ──
def _boot_stop_pg_quietly(*a, **k):
    from lib.database._bootstrap import _boot_stop_pg_quietly as _f
    return _f(*a, **k)

def _bootstrap_pg(*a, **k):
    from lib.database._bootstrap import _bootstrap_pg as _f
    return _f(*a, **k)

def _find_pg_binary(*a, **k):
    from lib.database._bootstrap import _find_pg_binary as _f
    return _f(*a, **k)

def _latest_pg_backup(*a, **k):
    from lib.database._bootstrap import _latest_pg_backup as _f
    return _f(*a, **k)

def _pg_binaries_present(*a, **k):
    from lib.database._bootstrap import _pg_binaries_present as _f
    return _f(*a, **k)

def _quarantine_corrupt_pgdata(*a, **k):
    from lib.database._bootstrap import _quarantine_corrupt_pgdata as _f
    return _f(*a, **k)

def _read_our_pg_port(*a, **k):
    from lib.database._bootstrap import _read_our_pg_port as _f
    return _f(*a, **k)

def _recover_via_pitr(*a, **k):
    from lib.database._bootstrap import _recover_via_pitr as _f
    return _f(*a, **k)

def _select_restore_channel(*a, **k):
    from lib.database._bootstrap import _select_restore_channel as _f
    return _f(*a, **k)

def _tier_b_enabled(*a, **k):
    from lib.database._bootstrap import _tier_b_enabled as _f
    return _f(*a, **k)

def _verify_pg_after_start(*a, **k):
    from lib.database._bootstrap import _verify_pg_after_start as _f
    return _f(*a, **k)

def _verify_pg_data_directory(*a, **k):
    from lib.database._bootstrap import _verify_pg_data_directory as _f
    return _f(*a, **k)


def _pgdata_major_compatible(pgdata) -> bool:
    """Return False when the pgdata major version can't be served by the local
    postgres binary (caller must then bail to SQLite); True to proceed.

    Extracted verbatim from ``_ensure_pg_running`` Step 3b.  If the pgdata
    directory was created by a different PG major than the installed binary
    (common after an export/copy across machines), any start attempt FATALs on
    a config-param mismatch (e.g. PG 18's ``autovacuum_worker_slots`` under a
    PG 17 binary) and the scheduler retry-storms on "connection refused".  We
    detect that here so the caller falls back cleanly.

    Returns
    -------
    bool
        False  → incompatible major OR no postgres binary to check against;
                 the caller must ``return None``.
        True   → no PG_VERSION file, unreadable version, a matching major, or
                 a non-fatal probe error — proceed with the normal flow.
    """
    pg_version_file = os.path.join(pgdata, 'PG_VERSION')
    if not os.path.isfile(pg_version_file):
        return True
    try:
        with open(pg_version_file) as _vf:
            pgdata_major = _vf.read().strip().split('.')[0]
    except Exception as _e:
        logger.debug('[DB] Could not read PG_VERSION from %s: %s', pgdata, _e)
        pgdata_major = None
    if not pgdata_major:
        return True
    # Query the locally-installed postgres binary for its major.
    try:
        _postgres_bin = _find_pg_binary('postgres')
        _ver_out = subprocess.run(
            [_postgres_bin, '--version'],
            capture_output=True, text=True, timeout=5
        )
        if _ver_out.returncode == 0:
            # Output is like "postgres (PostgreSQL) 17.2"
            _bin_major = _ver_out.stdout.strip().split()[-1].split('.')[0]
            if _bin_major != pgdata_major:
                logger.error(
                    '[DB] pgdata major=%s but local postgres binary major=%s '
                    '— REFUSING to start (would FATAL with config-param errors). '
                    'Falling back to SQLite. To recover: move %s aside (e.g. '
                    '`mv pgdata pgdata.bak`) so a fresh pgdata is initdb\'d, '
                    'OR install matching PG version, OR set TOFU_DB_BACKEND=sqlite.',
                    pgdata_major, _bin_major, pgdata)
                return False
            logger.debug('[DB] pgdata major (%s) matches local binary', pgdata_major)
    except FileNotFoundError:
        # No postgres binary on host — caller (_core) already bailed
        # earlier via _pg_binaries_present(), so this shouldn't fire,
        # but guard anyway.
        logger.info('[DB] No postgres binary to version-check pgdata against')
        return False
    except Exception as _e:
        logger.debug('[DB] Could not run postgres --version: %s', _e)
        # Non-fatal — let normal flow try to start PG; it'll fail
        # with a clearer log if incompatible.
    return True


def _dump_live_cluster(pg_host, pg_port, pg_user, out_path):
    """Take a fresh, consistent, lock-free online ``pg_dumpall`` of a LIVE PG.

    A logical dump of a running PostgreSQL is a consistent online backup with
    ZERO data-loss window (unlike restoring a stale nightly artifact) and no
    Tier-B machinery. Used as the PREFERRED seed source for the one-time
    local-primary migration.

    Returns:
        True on a non-empty dump written to *out_path*, else False.
    """
    pg_dumpall = _find_pg_binary('pg_dumpall')
    if not shutil.which(pg_dumpall) and not os.path.isfile(pg_dumpall):
        logger.warning('[DB-Seed] pg_dumpall not found — cannot take a live dump')
        return False
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, 'w') as fh:
            proc = subprocess.run(
                [pg_dumpall, '-h', pg_host, '-p', str(pg_port), '-U', pg_user,
                 '--clean', '--if-exists'],
                stdout=fh, stderr=subprocess.PIPE, text=True,
                env={**os.environ, 'PGCONNECT_TIMEOUT': '10',
                     'PGGSSENCMODE': 'disable'},
                timeout=int(getenv_compat('TOFU_DB_SEED_DUMP_TIMEOUT', default='1800')),
            )
        if proc.returncode != 0:
            logger.warning('[DB-Seed] live pg_dumpall failed (rc=%d): %s',
                           proc.returncode, (proc.stderr or '').strip()[:300])
            return False
        if os.path.getsize(out_path) == 0:
            logger.warning('[DB-Seed] live pg_dumpall produced an empty file')
            return False
        logger.info('[DB-Seed] Live dump written to %s (%.1f MB)',
                    out_path, os.path.getsize(out_path) / 1e6)
        return True
    except Exception as e:
        logger.error('[DB-Seed] live pg_dumpall raised: %s', e, exc_info=True)
        return False


def _count_convs(pg_host, pg_port, pg_user, pg_dbname):
    """Row count of the conversations table, or None if unreadable.

    Used as the seed verification sentinel: the restored local cluster must
    report a count consistent with the source before local is declared canonical.
    """
    psql_bin = _find_pg_binary('psql')
    if not shutil.which(psql_bin) and not os.path.isfile(psql_bin):
        return None
    try:
        proc = subprocess.run(
            [psql_bin, '-h', pg_host, '-p', str(pg_port), '-U', pg_user,
             '-d', pg_dbname, '-tAc', 'SELECT count(*) FROM conversations'],
            capture_output=True, text=True,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '10', 'PGGSSENCMODE': 'disable'},
            timeout=30,
        )
        if proc.returncode != 0:
            logger.debug('[DB-Seed] conv-count query failed: %s',
                         (proc.stderr or '').strip()[:200])
            return None
        return int((proc.stdout or '0').strip() or '0')
    except Exception as e:
        logger.debug('[DB-Seed] conv-count probe raised: %s', e)
        return None


def _ensure_legacy_up_for_seed(legacy_pgdata, base_dir, pg_user):
    """Ensure the LEGACY cluster is running so the seed can take a FRESH dump.

    The seed runs at Step -1, before the normal start path, so on a clean/PARK
    restart the legacy PG is DOWN. Reuse it if already up; otherwise start it via
    the owned-PG ``pg_ctl start`` path (same mechanism the normal boot uses).

    Returns:
        The legacy port (int) when the cluster is confirmed up, else None (only
        then may the caller fall back to the nightly dump).
    """
    port = _read_our_pg_port(legacy_pgdata)
    if port is None:
        logger.warning('[DB-Seed] legacy postgresql.conf has no port — cannot '
                       'start legacy for a fresh dump')
        return None
    # Already up?
    try:
        r = subprocess.run(
            [_find_pg_binary('pg_isready'), '-h', '127.0.0.1', '-p', str(port),
             '-d', 'template1'],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and _verify_pg_data_directory('127.0.0.1', port,
                                                            legacy_pgdata, pg_user):
            logger.info('[DB-Seed] legacy cluster already up on :%d (reusing for dump)', port)
            return port
    except Exception as e:
        logger.debug('[DB-Seed] legacy pg_isready probe failed: %s', e)
    # Down → start it via pg_ctl (do NOT fall back to nightly just because it
    # wasn't running yet — that is the exact bug this fixes).
    log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
    try:
        start = subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', legacy_pgdata, '-l', log_path,
             'start', '-w', '-t', '30'],
            capture_output=True, text=True, timeout=45)
        if start.returncode == 0 and _verify_pg_after_start(port, legacy_pgdata,
                                                            pg_user, total_wait_s=12):
            logger.info('[DB-Seed] started legacy cluster on :%d for a fresh dump', port)
            return port
        logger.warning('[DB-Seed] could not start legacy cluster for dump (rc=%d): %s',
                       start.returncode, (start.stderr or '').strip()[:200])
    except Exception as e:
        logger.warning('[DB-Seed] starting legacy cluster for dump raised: %s', e)
    return None


def _seed_local_pgdata_from_legacy(local_pgdata, legacy_pgdata, base_dir,
                                   pg_port, pg_user, pg_password, pg_dbname):
    """One-time migration: populate an empty LOCAL pgdata from the legacy source.

    This is what makes the local-primary flip SAFE: it runs at most once, BEFORE
    ``_ensure_pg_running`` picks a cluster, and only actually populates local
    after a verified restore. Its properties (owner-mandated):

      1. Seed source = a FRESH live ``pg_dumpall`` of the legacy cluster. Because
         this runs at Step -1 (before the normal start path), the legacy cluster
         is first STARTED if it is down (via _ensure_legacy_up_for_seed); the
         nightly dump is used ONLY when legacy genuinely cannot be started — NOT
         merely because it wasn't up yet. Opt-in only (TOFU_DB_SEED_LOCAL=1);
         the default-on /tmp seed was withdrawn 2026-08-06 (owner: no DB
         deployment outside the project directory).
      2. Idempotent on ``pgdata_is_populated(local)`` — if local already looks
         initialized, skip entirely (never re-restore over newer local data).
      3. Verify-before-canonical — after restoring into local, confirm the
         restored cluster starts and its ``conversations`` count is consistent
         with the source. On ANY failure: QUARANTINE the half-restored local dir
         so it can never satisfy the gate, leave legacy canonical, log CRITICAL.

    Returns:
        True iff local is now a verified, populated cluster (gate will flip to
        it on the next boot); False on skip / failure (legacy stays canonical).
    """
    from lib.database.db_paths import pgdata_is_populated

    # WITHDRAWN (owner directive 2026-08-06, epic pt_4d321fb8f1c2400c closed):
    # "不要使用除了项目以外的路径来解决这个问题，/tmp这些路径不准用来部署db，
    # 会丢的。以后都不许想这个。" The local-primary seed targets /tmp — exactly
    # the forbidden class. The 2026-08-05 default-on flip is REVERTED: the seed
    # is inert unless TOFU_DB_SEED_LOCAL=1 is set explicitly (nobody will), so
    # a plain `python server.py` NEVER deploys the DB outside the project dir.
    # The machinery stays only as documentation of the explored-and-rejected
    # path; do not re-enable without an owner decision on a project-local root.
    if getenv_compat('TOFU_DB_SEED_LOCAL', default='0').lower() not in ('1', 'true', 'yes'):
        logger.debug('[DB-Seed] not opted in (TOFU_DB_SEED_LOCAL!=1) — skipping '
                     'local seed; staying on legacy FUSE pgdata (the /tmp-local '
                     'seed was withdrawn 2026-08-06)')
        return False

    # ── Property 2: idempotent — never touch an already-populated local ──
    if pgdata_is_populated(local_pgdata):
        logger.debug('[DB-Seed] local pgdata %s already populated — no-op', local_pgdata)
        return True
    if not _pg_binaries_present():
        logger.warning('[DB-Seed] PG binaries absent — cannot seed local; '
                       'staying on legacy')
        return False

    from lib.log import audit_log
    audit_log('db_seed_local_start', local=local_pgdata, legacy=legacy_pgdata)

    # ── Property 1: prefer a FRESH live dump; fall back to latest nightly ──
    # CRITICAL ordering: the seed runs at Step -1, BEFORE the normal start path,
    # so on a clean/PARK restart the legacy cluster is DOWN here. We must bring
    # it UP ourselves before dumping — otherwise a mere "not yet running" would
    # silently degrade to the stale nightly and defeat the zero-loss guarantee.
    # Only a legacy cluster that genuinely CANNOT be started falls back to nightly.
    staged = os.path.join(base_dir, 'data', 'pg_backup.sql')
    src = 'live'
    legacy_port = _ensure_legacy_up_for_seed(legacy_pgdata, base_dir, pg_user)
    live_ok = False
    if legacy_port is not None:
        live_ok = _dump_live_cluster('127.0.0.1', legacy_port, pg_user, staged)
    if not live_ok:
        latest = _latest_pg_backup(base_dir)
        if not latest:
            logger.critical('[DB-Seed] legacy cluster could not be started AND no '
                            'nightly dump to fall back to — cannot seed local; '
                            'legacy stays canonical.')
            audit_log('db_seed_local_failed', reason='no_source')
            return False
        try:
            shutil.copy2(latest, staged)
            src = 'nightly:%s' % os.path.basename(latest)
            logger.warning('[DB-Seed] live cluster unreachable — falling back to '
                           'latest nightly dump %s (may lose since-dump rows)', latest)
        except OSError as e:
            logger.critical('[DB-Seed] could not stage nightly dump: %s', e)
            audit_log('db_seed_local_failed', reason='stage_failed')
            return False

    # Source conversation count (verification target). Best-effort; None → skip
    # the numeric equality check but still require the restored cluster to start.
    src_convs = _count_convs('127.0.0.1', legacy_port, pg_user, pg_dbname) \
        if legacy_port is not None else None

    # ── §3a channel selection: when Tier B is on, restore via the NEWER of the
    # two durability channels. tier_b → PITR (base + WAL replay, seconds-RPO);
    # tier_a (or Tier B off) → the fresh-dump/initdb path below. This is the
    # real cold-start restore path, driven by the selector — not a standalone fn.
    if _tier_b_enabled():
        channel, _end = _select_restore_channel(base_dir)
        if channel == 'tier_b':
            logger.info('[DB-Seed] §3a selector chose Tier B (WAL tail newest) — '
                        'restoring via PITR')
            result = _recover_via_pitr(local_pgdata, base_dir, pg_port,
                                       pg_user, pg_password, pg_dbname)
            if not result:
                logger.critical('[DB-Seed] PITR restore FAILED — quarantined; '
                                'legacy stays canonical.')
                audit_log('db_seed_local_failed', reason='pitr_failed')
                return False
            local_port = result['PG_PORT']
            local_convs = _count_convs('127.0.0.1', local_port, pg_user, pg_dbname)
            if local_convs is None or (src_convs is not None and local_convs < src_convs):
                # PITR should recover >= the dump's rows (WAL tail is newer); a
                # SHORTFALL means the replay didn't reach the tail → not canonical.
                logger.critical('[DB-Seed] PITR VERIFY FAILED — local convs=%s < '
                                'source=%s (WAL tail not fully replayed); '
                                'quarantining, legacy stays canonical.',
                                local_convs, src_convs)
                _boot_stop_pg_quietly(local_pgdata)
                _quarantine_corrupt_pgdata(local_pgdata)
                audit_log('db_seed_local_failed', reason='pitr_verify', source='tier_b')
                return False
            logger.warning('[DB-Seed] SUCCESS via PITR — local recovered to WAL tail '
                           '(convs=%s). Legacy PRESERVED at %s.', local_convs, legacy_pgdata)
            audit_log('db_seed_local_success', local_convs=local_convs, source='tier_b')
            _boot_stop_pg_quietly(local_pgdata)
            return True

    # ── initdb + restore INTO local (reuses the well-tested bootstrap path) ──
    logger.info('[DB-Seed] Seeding local pgdata=%s from %s source', local_pgdata, src)
    result = _bootstrap_pg(local_pgdata, base_dir, '127.0.0.1', pg_port,
                           pg_user, pg_password, pg_dbname)
    if not result:
        logger.critical('[DB-Seed] initdb+restore into local FAILED — quarantining '
                        'the half-built local dir; legacy stays canonical.')
        _quarantine_corrupt_pgdata(local_pgdata)
        audit_log('db_seed_local_failed', reason='bootstrap_failed', source=src)
        return False

    # ── Property 3: verify BEFORE declaring local canonical ──
    local_port = result['PG_PORT']
    local_convs = _count_convs('127.0.0.1', local_port, pg_user, pg_dbname)
    ok = local_convs is not None
    if ok and src_convs is not None:
        ok = local_convs == src_convs
    if not ok:
        logger.critical('[DB-Seed] VERIFY FAILED — restored local convs=%s vs '
                        'source=%s. Quarantining local so it cannot satisfy the '
                        'gate; legacy stays canonical.', local_convs, src_convs)
        _boot_stop_pg_quietly(local_pgdata)
        _quarantine_corrupt_pgdata(local_pgdata)
        audit_log('db_seed_local_failed', reason='verify_mismatch',
                  local_convs=local_convs, src_convs=src_convs, source=src)
        return False

    logger.warning('[DB-Seed] SUCCESS — local seeded + verified (convs=%s, source=%s). '
                   'Next boot will resolve pgdata to local. Legacy PRESERVED at %s '
                   '(not deleted — retire only after operator sign-off).',
                   local_convs, src, legacy_pgdata)
    audit_log('db_seed_local_success', local_convs=local_convs, source=src)
    # Stop the just-seeded local PG; _ensure_pg_running (next boot) starts it
    # through the normal owned-PG path. Leaving it running here would collide
    # with this boot's legacy cluster on the same DSN.
    _boot_stop_pg_quietly(local_pgdata)
    return True


# ══════════════════════════════════════════════════════════════════════
#  Atomic single-boot migration (seed → verify → flip) — 2026-08-05
# ══════════════════════════════════════════════════════════════════════

def _local_seed_is_stale(local_pgdata, legacy_pgdata) -> bool:
    """True when the legacy cluster was written MORE RECENTLY than the local
    seed — i.e. the local copy is behind and must be re-seeded before it may
    serve. Compares ``global/pg_control`` mtimes (a running or cleanly-stopped
    cluster checkpoints it; free, no cluster start needed). Unreadable/missing
    files → not stale (the populated-ness gates handle those cases).
    """
    try:
        lm = os.path.getmtime(os.path.join(local_pgdata, 'global', 'pg_control'))
        gm = os.path.getmtime(os.path.join(legacy_pgdata, 'global', 'pg_control'))
    except OSError as e:
        logger.debug('[DB-Migrate] pg_control mtime unreadable (treated as not stale): %s', e)
        return False
    return gm > lm


def _set_pgdata_port(pgdata, port) -> None:
    """Rewrite the ``port = N`` line in postgresql.conf (append if absent)."""
    conf = os.path.join(pgdata, 'postgresql.conf')
    with open(conf) as f:
        lines = f.readlines()
    wrote = False
    with open(conf, 'w') as f:
        for line in lines:
            s = line.strip()
            if not wrote and s.startswith('port') and '=' in s and not s.startswith('#'):
                f.write(f'port = {port}\n')
                wrote = True
            else:
                f.write(line)
    if not wrote:
        with open(conf, 'a') as f:
            f.write(f'\nport = {port}\n')


def _start_pg_cluster(pgdata, base_dir):
    """``pg_ctl start`` a cluster; returns the CompletedProcess."""
    log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
    return subprocess.run(
        [_find_pg_binary('pg_ctl'), '-D', pgdata, '-l', log_path,
         'start', '-w', '-t', '60'],
        capture_output=True, text=True, timeout=90)


def _flip_local_into_service(local_pgdata, legacy_pgdata, base_dir,
                             pinned_port, pg_user, pg_dbname) -> bool:
    """Swap the serving cluster: stop legacy, start the verified local seed on
    the pinned port. Legacy's DATA is never touched (dump-source only).

    The port swap is required because the deployment pins the PG port in the
    server env (TOFU_PG_PORT) — an explicit port makes the bootstrap treat
    :pinned as the managed target, so the local cluster must answer THERE or
    the flip would silently keep serving legacy (mechanism gap found
    2026-08-05). Legacy is stopped only AFTER the seed passed verification,
    keeping the no-service window to seconds. On any failure legacy is
    restarted and stays canonical.
    """
    from lib.log import audit_log
    logger.warning('[DB-Flip] stopping legacy cluster (%s) to free :%d for the '
                   'local primary', legacy_pgdata, pinned_port)
    _boot_stop_pg_quietly(legacy_pgdata)
    try:
        _set_pgdata_port(local_pgdata, pinned_port)
    except Exception as e:
        logger.error('[DB-Flip] retarget local port to :%d failed: %s — '
                     'restarting legacy', pinned_port, e)
        _start_pg_cluster(legacy_pgdata, base_dir)
        return False
    start = _start_pg_cluster(local_pgdata, base_dir)
    ok = (getattr(start, 'returncode', 1) == 0
          and _verify_pg_after_start(pinned_port, local_pgdata, pg_user,
                                     total_wait_s=15))
    if not ok:
        logger.critical('[DB-Flip] local primary failed to start on :%d — '
                        'restarting legacy; migration aborted (legacy canonical)',
                        pinned_port)
        audit_log('db_seed_flip_failed', port=pinned_port)
        _boot_stop_pg_quietly(local_pgdata)
        _start_pg_cluster(legacy_pgdata, base_dir)
        return False
    logger.warning('[DB-Flip] SUCCESS — local primary serving on :%d; legacy '
                   'stopped and PRESERVED at %s', pinned_port, legacy_pgdata)
    audit_log('db_seed_flip_success', port=pinned_port)
    return True


def _seed_failure_marker(local_root):
    return os.path.join(local_root, '.seed_failed')


def _mark_seed_failure(local_root) -> None:
    """Stamp the failure cooldown marker (creating the root dir if needed —
    on a failed first seed the local root may not exist yet)."""
    try:
        os.makedirs(local_root, exist_ok=True)
        with open(_seed_failure_marker(local_root), 'w') as fh:
            fh.write(str(time.time()))
    except OSError as e:
        logger.debug('[DB-Migrate] could not write failure marker: %s', e)


def _seed_failure_fresh(local_root, cooldown_s=6 * 3600) -> bool:
    """True when a previous migration failed within the cooldown — bounds the
    cost of a DETERMINISTIC failure to one dump+restore attempt per cooldown
    instead of one per boot (measured 2026-08-05: one attempt = a 46 GB dump)."""
    try:
        age = time.time() - os.path.getmtime(_seed_failure_marker(local_root))
        return age < cooldown_s
    except OSError as e:
        logger.debug('[DB-Migrate] no failure marker readable (no cooldown): %s', e)
        return False


def _migrate_local_primary_if_due(pgdata, base_dir, pg_port, pg_user,
                                  pg_password, pg_dbname):
    """Server-boot migration driver: seed the local primary and flip ATOMICALLY
    in the same boot. Returns the pgdata the boot should proceed with.

    Gates (all must hold):
      * ``TOFU_SERVER_PROCESS=1`` — only the server's own boot may migrate:
        the flip stops/starts clusters. A side process (probe, tooling, a bare
        ``import lib.database``) must never fire this — measured 2026-08-05:
        two bare imports each burned a full 46 GB dump cycle. The marker is
        set in-process by server.py at its top; it is NOT a user-facing knob.
      * split engaged AND legacy populated (a fresh install has nothing to
        migrate) AND (local unpopulated OR stale vs legacy).
      * ``TOFU_DB_SEED_LOCAL=1`` must be set explicitly — the migration was
        withdrawn as default-on on 2026-08-06 (owner directive: /tmp-class
        external paths are forbidden for the DB).

    Atomicity: the flip happens in the SAME boot as a verified seed. The old
    two-restart dance left an unbounded staleness window (writes between the
    seeding boot and the flipping boot would be absent from local) — fatal
    under the default-on directive, where the two boots can be days apart.

    Failure semantics: any failure leaves legacy canonical (restarted if it
    was stopped) and drops a cooldown marker so a deterministic failure costs
    one attempt per 6 h, not one per boot.
    """
    from lib.database.db_paths import (
        local_data_split_enabled, legacy_pgdata_dir, pgdata_is_populated)

    if getenv_compat('TOFU_SERVER_PROCESS', default='') != '1':
        return pgdata
    data_dir = os.path.join(base_dir, 'data')
    legacy = legacy_pgdata_dir(data_dir)
    if not local_data_split_enabled(data_dir):
        return pgdata
    if not pgdata_is_populated(legacy):
        return pgdata
    local_root = getenv_compat('TOFU_DB_LOCAL_ROOT', default='').strip() \
        or '/tmp/tofu'
    local = os.path.join(os.path.abspath(local_root), 'pgdata')
    if getenv_compat('TOFU_DB_SEED_LOCAL', default='0').lower() not in ('1', 'true', 'yes'):
        # Default path — the /tmp-local migration was withdrawn 2026-08-06
        # (owner: DB never leaves the project dir). Debug level: nothing wrong.
        logger.debug('[DB-Migrate] not opted in — staying on the resolved pgdata')
        return pgdata
    if pgdata_is_populated(local) and not _local_seed_is_stale(local, legacy):
        return pgdata  # already migrated and fresh — normal post-flip boot
    if _seed_failure_fresh(local_root):
        logger.warning('[DB-Migrate] last migration attempt failed recently — '
                       'cooling down (one attempt per 6h); staying on legacy')
        return pgdata

    # Stale local: park it aside so the seed sees an empty target (initdb
    # refuses a non-empty dir, and the stale copy must never serve anyway).
    if pgdata_is_populated(local):
        parked = '%s.stale.%s' % (local, time.strftime('%Y%m%d_%H%M%S'))
        try:
            os.rename(local, parked)
            logger.warning('[DB-Migrate] local seed is stale vs legacy — parked '
                           'aside at %s for a fresh re-seed', parked)
        except OSError as e:
            logger.error('[DB-Migrate] could not park stale local %s: %s — '
                         'staying on the resolved pgdata', local, e)
            return pgdata

    try:
        pinned = int((getenv_compat('TOFU_PG_PORT', default='') or '').strip()
                     or pg_port)
    except (TypeError, ValueError) as e:
        logger.debug('[DB-Migrate] TOFU_PG_PORT unparsable, using default port %s: %s',
                     pg_port, e)
        pinned = pg_port

    ok = _seed_local_pgdata_from_legacy(
        local, legacy, base_dir, pinned, pg_user, pg_password, pg_dbname)
    if not ok:
        _mark_seed_failure(local_root)
        return legacy
    if not _flip_local_into_service(local, legacy, base_dir, pinned,
                                    pg_user, pg_dbname):
        _mark_seed_failure(local_root)
        return legacy
    try:
        os.remove(_seed_failure_marker(local_root))
    except OSError as e:
        logger.debug('[DB-Migrate] could not clear failure marker (harmless): %s', e)
    return local

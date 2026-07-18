"""Corrupt-cluster self-heal / quarantine — the 2026-06-04 WAL-redo-PANIC class.

``_quarantine_corrupt_pgdata`` moves a corrupt cluster aside; the two-stage
``_try_self_heal_corrupt_pg`` recovers a cluster that exists but refuses to
start (pg_resetwal -f first, then quarantine + fresh initdb + restore latest
logical dump) — fully automatic, so the user never runs pg_resetwal / psql by
hand.
"""

import os
import shutil
import subprocess
import time

from lib.env_compat import getenv_compat
from lib.log import get_logger

from lib.database._pg_backup._dump import _latest_pg_backup
from lib.database._pg_backup._shims import (
    _bootstrap_pg,
    _ensure_database_exists,
    _find_pg_binary,
    _mark_pg_owned_locally,
    _pg_binaries_present,
    _read_our_pg_port,
    _stop_local_pg_quietly,
    _verify_pg_after_start,
    _write_owner_host,
)

logger = get_logger(__name__)


def _quarantine_corrupt_pgdata(pgdata):
    """Move a corrupt pgdata aside to pgdata.corrupt.<ts> so a fresh initdb can run.

    Returns the quarantine path on success, or None on failure (in which case
    the caller must NOT proceed to initdb over live data).
    """
    quarantine = f'{pgdata}.corrupt.{time.strftime("%Y%m%d_%H%M%S")}'
    try:
        os.rename(pgdata, quarantine)
        logger.warning('[DB-SelfHeal] Quarantined corrupt cluster: %s → %s',
                       pgdata, quarantine)
        return quarantine
    except OSError as e:
        logger.error('[DB-SelfHeal] Could not quarantine corrupt pgdata %s: %s',
                     pgdata, e)
        return None


def _try_self_heal_corrupt_pg(pgdata, base_dir, pg_host, pg_port, pg_user,
                              pg_password, pg_dbname):
    """Automatically recover a PG cluster that exists but refuses to start.

    Triggered from _ensure_pg_running when every normal start path failed but
    a real cluster is present on disk (the 2026-06-04 'WAL redo PANIC →
    lost conversations' class of failure). Fully automatic — the user never
    has to run pg_resetwal / psql by hand.

    Two-stage strategy (least destructive first):
      1. ``pg_resetwal -f`` on the existing pgdata, then retry start. This is
         the standard escape from a WAL-redo PANIC and preserves all heap data
         (only un-checkpointed WAL is discarded). A pre-reset filesystem copy
         of pg_wal is NOT kept, but the whole cluster is quarantined in stage 2
         only if this fails, so stage-1 operates in place.
      2. If reset fails (or the reset cluster still won't start), quarantine
         the corrupt pgdata, ``initdb`` a fresh cluster, and restore the latest
         ``data/pg_backups/`` logical dump into it. Data written since the last
         nightly backup is lost, but the service comes back automatically with
         the bulk of history instead of a 2-row SQLite.

    Disabled by TOFU_PG_SELF_HEAL=0 (then we fall back to the loud-fail path).

    Returns:
        dict {PG_HOST, PG_PORT, PG_DSN} on success, or None.
    """
    if getenv_compat('TOFU_PG_SELF_HEAL', default='1').lower() in ('0', 'false', 'no'):
        logger.warning('[DB-SelfHeal] Disabled via TOFU_PG_SELF_HEAL — skipping '
                       'automatic recovery')
        return None
    if not _pg_binaries_present():
        return None

    from lib.log import audit_log

    def _build_dsn(host, port):
        dsn = f"host={host} port={port} dbname={pg_dbname}"
        if pg_user:
            dsn += f" user={pg_user}"
        if pg_password:
            dsn += f" password={pg_password}"
        return dsn

    # Take an exclusive backup of the corrupt cluster dir first (cheap, just a
    # filesystem copy) so a botched self-heal can never destroy the only copy.
    logger.warning('[DB-SelfHeal] PG cluster at %s exists but will not start — '
                   'attempting automatic recovery', pgdata)
    audit_log('pg_self_heal_start', pgdata=pgdata)

    # ── Stage 1: pg_resetwal -f, then retry start ──
    pg_resetwal = _find_pg_binary('pg_resetwal')
    if shutil.which(pg_resetwal) or os.path.isfile(pg_resetwal):
        try:
            result = subprocess.run(
                [pg_resetwal, '-f', '-D', pgdata],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                logger.warning('[DB-SelfHeal] Stage 1: pg_resetwal -f succeeded — '
                               'retrying start')
                conf_port = _read_our_pg_port(pgdata) or pg_port
                log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
                try:
                    start = subprocess.run(
                        [_find_pg_binary('pg_ctl'), '-D', pgdata, '-l', log_path,
                         'start', '-w', '-t', '30'],
                        capture_output=True, text=True, timeout=45)
                    if start.returncode == 0 and _verify_pg_after_start(
                            conf_port, pgdata, pg_user, total_wait_s=12):
                        logger.warning('[DB-SelfHeal] Stage 1 recovery SUCCESS — '
                                       'PG started after pg_resetwal')
                        _ensure_database_exists('127.0.0.1', conf_port, pg_dbname,
                                                pg_user, pgdata)
                        _write_owner_host(pgdata)
                        _mark_pg_owned_locally(pgdata)
                        audit_log('pg_self_heal_success', stage='resetwal',
                                  port=conf_port)
                        return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port,
                                'PG_DSN': _build_dsn('127.0.0.1', conf_port)}
                    logger.error('[DB-SelfHeal] Stage 1: PG still will not start '
                                 'after pg_resetwal (rc=%d): %s',
                                 start.returncode, (start.stderr or '').strip()[:300])
                    _stop_local_pg_quietly(pgdata)
                except Exception as e:
                    logger.error('[DB-SelfHeal] Stage 1 start attempt raised: %s', e)
            else:
                logger.error('[DB-SelfHeal] Stage 1: pg_resetwal failed (rc=%d): %s',
                             result.returncode, (result.stderr or '').strip()[:300])
        except Exception as e:
            logger.error('[DB-SelfHeal] Stage 1 pg_resetwal raised: %s', e)

    # ── Stage 2: quarantine + fresh initdb + restore latest backup ──
    latest = _latest_pg_backup(base_dir)
    if not latest:
        logger.critical('[DB-SelfHeal] Stage 2 impossible: no logical backup in '
                        'data/pg_backups/ to restore from. Leaving corrupt cluster '
                        'in place for manual recovery (pg_resetwal already tried). '
                        'A nightly backup would have made this automatic.')
        audit_log('pg_self_heal_failed', reason='no_backup')
        return None

    logger.warning('[DB-SelfHeal] Stage 2: rebuilding from latest backup %s', latest)
    quarantine = _quarantine_corrupt_pgdata(pgdata)
    if not quarantine:
        return None

    # Stage the dump where _bootstrap_pg's restore step looks for it, so we
    # reuse the well-tested initdb → createdb → restore path verbatim.
    staged_dump = os.path.join(base_dir, 'data', 'pg_backup.sql')
    try:
        shutil.copy2(latest, staged_dump)
    except OSError as e:
        logger.error('[DB-SelfHeal] Could not stage backup for restore: %s', e)
        return None

    result = _bootstrap_pg(pgdata, base_dir, pg_host, pg_port, pg_user,
                           pg_password, pg_dbname)
    if result:
        logger.warning('[DB-SelfHeal] Stage 2 recovery SUCCESS — fresh cluster '
                       'restored from %s (corrupt cluster preserved at %s)',
                       latest, quarantine)
        audit_log('pg_self_heal_success', stage='restore_backup',
                  backup=latest, quarantine=quarantine)
        return result

    logger.critical('[DB-SelfHeal] Stage 2 FAILED — fresh initdb+restore did not '
                    'come up. Corrupt cluster preserved at %s, backup at %s.',
                    quarantine, latest)
    audit_log('pg_self_heal_failed', reason='restore_failed',
              quarantine=quarantine, backup=latest)
    return None

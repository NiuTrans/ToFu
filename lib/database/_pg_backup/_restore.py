"""Restore/recovery: §3a newest-wins channel selector + cold-start PITR replay.

``_select_restore_channel`` picks the channel with the NEWER recoverable end
(Tier A latest dump vs Tier B last archived WAL segment) — never by tier.
``_recover_via_pitr`` lays the latest ``-X stream`` base into the target and
replays archived WAL to the last segment (the seconds-RPO tail), then promotes.
"""

import os
import shutil
import sys
import subprocess

from lib.env_compat import getenv_compat
from lib.log import get_logger

from lib.database._pg_backup._basebackup import (
    _latest_base_backup,
    _tier_b_wal_end_ts,
)
from lib.database._pg_backup._dump import _tier_a_dump_end_ts
from lib.database._pg_backup._selfheal import _quarantine_corrupt_pgdata
from lib.database._pg_backup._shims import (
    _ensure_database_exists,
    _ensure_managed_pg_config,
    _find_pg_binary,
    _mark_pg_owned_locally,
    _read_our_pg_port,
    _verify_pg_after_start,
    _write_owner_host,
)

logger = get_logger(__name__)


def _restore_divergence_warn_s():
    raw = getenv_compat('TOFU_DB_RESTORE_DIVERGENCE_WARN_S', default='21600')
    try:
        return min(max(int(raw), 60), 30 * 24 * 3600)
    except (TypeError, ValueError):
        logger.warning('[DB-Restore] invalid divergence threshold %r; using 21600s',
                       raw)
        return 21600


def _select_restore_channel(base_dir):
    """§3a: pick the channel with the NEWER recoverable end (never by tier).

    Compares Tier A (latest logical dump completion) vs Tier B (last archived
    WAL segment). Restores the NEWER. When the two ends diverge by more than
    ``TOFU_DB_RESTORE_DIVERGENCE_WARN_S`` (default 6h) logs CRITICAL + audit —
    a large gap means one durability channel is BROKEN and must surface; the
    restore still proceeds with the newer channel.

    Returns:
        ('tier_b', wal_end_ts) | ('tier_a', dump_end_ts) | (None, None).
    """
    a_ts = _tier_a_dump_end_ts(base_dir)
    b_ts = _tier_b_wal_end_ts(base_dir)
    if a_ts is None and b_ts is None:
        return None, None

    # Divergence guard (only meaningful when BOTH channels exist).
    if a_ts is not None and b_ts is not None:
        warn_s = _restore_divergence_warn_s()
        gap = abs(a_ts - b_ts)
        if gap > warn_s:
            chosen = 'tier_b' if b_ts >= a_ts else 'tier_a'
            logger.critical('[DB-Restore] durability channel DIVERGENCE: Tier A end=%.0f '
                            'vs Tier B end=%.0f (gap=%.0fs > %ds) — one channel is '
                            'likely BROKEN. Restoring the NEWER (%s); investigate the '
                            'stale channel.', a_ts, b_ts, gap, warn_s, chosen)
            try:
                from lib.log import audit_log
                audit_log('db_restore_channel_divergence', tier_a_end=a_ts,
                          tier_b_end=b_ts, chosen=chosen, gap_s=round(gap))
            except Exception as _ae:
                logger.debug('[DB-Restore] divergence audit failed: %s', _ae)

    if b_ts is not None and (a_ts is None or b_ts >= a_ts):
        return 'tier_b', b_ts
    return 'tier_a', a_ts


def _recover_via_pitr(local_pgdata, base_dir, pg_port, pg_user, pg_password, pg_dbname):
    """Tier B cold-start: restore latest base + replay archived WAL (PITR).

    Lays the latest ``-X stream`` base into ``local_pgdata``, writes
    ``recovery.signal`` + a ``restore_command`` pointing at our WAL-restore shim
    + ``recovery_target_timeline='latest'``, then starts PG so it replays the
    archived WAL to the last segment (the seconds-RPO tail) and promotes.

    Returns dict {PG_HOST,PG_PORT,PG_DSN} on a verified recovery, else None.
    """
    base = _latest_base_backup(base_dir)
    if not base:
        logger.warning('[DB-PITR] no base backup present — cannot PITR')
        return None
    if os.path.exists(local_pgdata) and os.listdir(local_pgdata):
        logger.error('[DB-PITR] target %s not empty — refusing to overlay', local_pgdata)
        return None
    try:
        os.makedirs(os.path.dirname(local_pgdata), exist_ok=True)
        shutil.copytree(base, local_pgdata)
    except Exception as e:
        logger.error('[DB-PITR] could not lay down base %s: %s', base, e)
        return None
    # recovery config: replay archived WAL via our restore shim.
    restore_cmd = ("%s -m lib.database.wal_archive restore %%f %%p"
                   % (sys.executable or 'python3'))
    try:
        with open(os.path.join(local_pgdata, 'postgresql.auto.conf'), 'a') as f:
            f.write("\nrestore_command = '%s'\n" % restore_cmd)
            f.write("recovery_target_timeline = 'latest'\n")
        open(os.path.join(local_pgdata, 'recovery.signal'), 'w').close()
    except OSError as e:
        logger.error('[DB-PITR] could not write recovery config: %s', e)
        return None
    # Apply managed tuning to the restored base (do NOT re-initdb — the base IS
    # the cluster), then start PG so it replays archived WAL and promotes.
    _ensure_managed_pg_config(local_pgdata)
    conf_port = _read_our_pg_port(local_pgdata) or pg_port
    log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
    logger.info('[DB-PITR] Restoring base %s + replaying archived WAL into %s (port=%d)',
                base, local_pgdata, conf_port)
    try:
        start = subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', local_pgdata, '-l', log_path,
             'start', '-w', '-t', '60'],
            capture_output=True, text=True, timeout=90)
        if start.returncode == 0 and _verify_pg_after_start(conf_port, local_pgdata,
                                                            pg_user, total_wait_s=20):
            _ensure_database_exists('127.0.0.1', conf_port, pg_dbname, pg_user, local_pgdata)
            _write_owner_host(local_pgdata)
            _mark_pg_owned_locally(local_pgdata)
            dsn = f"host=127.0.0.1 port={conf_port} dbname={pg_dbname}"
            if pg_user:
                dsn += f" user={pg_user}"
            logger.warning('[DB-PITR] PITR recovery SUCCESS — replayed to WAL tail on :%d',
                           conf_port)
            return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port, 'PG_DSN': dsn}
        logger.critical('[DB-PITR] PITR start/replay FAILED (rc=%d): %s — quarantining',
                        start.returncode, (start.stderr or '').strip()[:300])
    except Exception as e:
        logger.critical('[DB-PITR] PITR start raised: %s — quarantining', e)
    _quarantine_corrupt_pgdata(local_pgdata)
    return None

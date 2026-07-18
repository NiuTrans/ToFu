"""Database creation + SQL-dump restore for a freshly-bootstrapped PG.

``_ensure_database_exists`` runs ``createdb`` if the target DB is missing (only
after verifying the PG is truly ours). ``_restore_from_sql_dump_if_present``
feeds a ``data/pg_backup.sql`` dump (left by export.py) into a freshly-initdb'd
cluster on first boot, with a data-loss guard against clobbering a populated
target.

Extracted from the monolithic ``_bootstrap.py`` (facade-preserving split).
"""

import os
import shutil
import subprocess

from lib.log import get_logger

from lib.database._pg_ownership import _find_pg_binary, _get_username
from lib.database._bootstrap._verify import _verify_pg_data_directory

logger = get_logger(__name__)


def _ensure_database_exists(host, port, pg_dbname, pg_user, pgdata):
    """Run ``createdb`` if the target database doesn't exist yet."""
    if not _verify_pg_data_directory(host, port, pgdata, pg_user):
        logger.error('[DB] REFUSING to createdb on %s:%d — it is NOT our PG instance '
                     '(data_directory mismatch). This prevents data leakage.',
                     host, port)
        return

    db_user = pg_user or _get_username()
    createdb_bin = _find_pg_binary('createdb')
    # Try the given host first; if 'localhost' DNS fails (macOS quirk),
    # retry with 127.0.0.1 as fallback.
    hosts_to_try = [host]
    if host == 'localhost':
        hosts_to_try.append('127.0.0.1')
    elif host == '127.0.0.1':
        hosts_to_try.append('localhost')
    for _h in hosts_to_try:
        try:
            result = subprocess.run(
                [createdb_bin, '-h', _h, '-p', str(port),
                 '-U', db_user, pg_dbname],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                if 'already exists' in result.stderr:
                    logger.debug('[DB] Database "%s" already exists on %s:%d',
                                pg_dbname, _h, port)
                    return
                elif 'could not translate host name' in result.stderr and _h != hosts_to_try[-1]:
                    logger.debug('[DB] createdb DNS failed for %s, retrying with %s', _h, hosts_to_try[-1])
                    continue
                else:
                    logger.warning('[DB] createdb on %s:%d failed: %s',
                                  _h, port, result.stderr.strip())
            else:
                logger.info('[DB] Created missing database "%s" on %s:%d',
                           pg_dbname, _h, port)
            return
        except FileNotFoundError:
            logger.debug('[DB] createdb binary not found (looked for: %s) — skipping', createdb_bin)
            return
        except Exception as e:
            logger.warning('[DB] createdb check failed: %s', e)
            return


def _restore_from_sql_dump_if_present(base_dir, pg_port, pg_user, pg_dbname):
    """If ``data/pg_backup.sql`` exists (left by export.py), restore it.

    The dump was produced by ``pg_dumpall --clean --if-exists`` so it's
    safe to apply to a freshly-initdb'd cluster that only has the default
    ``template1`` / ``postgres`` / ``$USER`` databases.

    After a successful restore the dump file is DELETED so we never
    restore the same snapshot twice (which would clobber any new data
    written by the user on the destination after the first boot).

    Silent no-op if the dump is missing, empty, or ``psql`` is unavailable.
    """
    dump_path = os.path.join(base_dir, 'data', 'pg_backup.sql')
    if not os.path.isfile(dump_path):
        return
    try:
        size = os.path.getsize(dump_path)
    except OSError as e:
        logger.warning('[DB] Could not stat pg_backup.sql: %s — skipping restore', e)
        return
    if size == 0:
        logger.info('[DB] pg_backup.sql is empty — removing and skipping restore')
        try:
            os.remove(dump_path)
        except OSError as _e:
            logger.debug('[DB] Could not remove empty dump: %s', _e)
        return

    psql_bin = _find_pg_binary('psql')
    if not shutil.which(psql_bin) and not os.path.isfile(psql_bin):
        logger.warning('[DB] psql not found — cannot restore %s '
                       '(destination will come up with an empty DB). '
                       'Install PostgreSQL client to enable auto-restore.',
                       dump_path)
        return

    # ⚠️ DATA-LOSS GUARD (2026-06-28 incident hardening): this dump is a
    # ``pg_dumpall --clean --if-exists`` — applying it DROPs and recreates
    # EVERY database in the dump. That is safe ONLY against a freshly-initdb'd
    # cluster (the intended export→first-boot flow). If the target already
    # holds real conversations (e.g. self-heal Stage 2 restored over a cluster
    # that actually had data, or a stale dump was left in place), a blind
    # restore would silently replace newer data with the snapshot. Refuse to
    # clobber a populated target: quarantine the dump aside instead of
    # applying it, and log loudly so an operator can decide.
    try:
        probe = subprocess.run(
            [psql_bin, '-h', '127.0.0.1', '-p', str(pg_port), '-U', pg_user,
             '-d', pg_dbname, '-tAc',
             "SELECT count(*) FROM conversations"],
            capture_output=True, text=True,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '10', 'PGGSSENCMODE': 'disable'},
            timeout=30,
        )
        existing_convs = int((probe.stdout or '0').strip() or '0') if probe.returncode == 0 else 0
    except Exception as e:
        # Table absent / DB empty / probe failed → treat as a clean target
        # (the normal first-boot case). Don't block the intended restore.
        logger.debug('[DB] restore pre-check probe failed (assuming empty target): %s', e)
        existing_convs = 0

    if existing_convs > 0:
        quarantine = dump_path + '.skipped-nonempty-target'
        logger.critical(
            '[DB] REFUSING to apply %s: target DB %r already has %d '
            'conversations. A --clean restore would DROP and replace them '
            '(potential data loss). Moving the dump aside to %s; apply it '
            'manually if you are SURE. Set TOFU_FORCE_DUMP_RESTORE=1 to '
            'override.',
            dump_path, pg_dbname, existing_convs, quarantine)
        if os.environ.get('TOFU_FORCE_DUMP_RESTORE') != '1':
            try:
                os.replace(dump_path, quarantine)
            except OSError as e:
                logger.error('[DB] Could not quarantine dump %s: %s', dump_path, e)
            return
        logger.warning('[DB] TOFU_FORCE_DUMP_RESTORE=1 — applying restore over '
                       'a populated DB at operator request')

    logger.info('[DB] Restoring data from %s (%.1f MB) — this may take a moment…',
                dump_path, size / (1024 * 1024))
    try:
        # Connect to the postgres admin DB; pg_dumpall --clean expects
        # to be able to DROP the target databases before recreating them.
        # -v ON_ERROR_STOP=1 makes a partial restore fail loudly instead
        # of leaving a half-restored DB.
        result = subprocess.run(
            [psql_bin, '-h', '127.0.0.1', '-p', str(pg_port), '-U', pg_user,
             '-d', 'postgres', '-v', 'ON_ERROR_STOP=1', '-q', '-f', dump_path],
            capture_output=True, text=True,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '10', 'PGGSSENCMODE': 'disable'},
            # No timeout — large dumps can take minutes on FUSE.
        )
    except Exception as e:
        logger.error('[DB] psql restore invocation failed: %s', e, exc_info=True)
        return

    if result.returncode != 0:
        # Leave the dump file in place so the user can retry manually.
        logger.error('[DB] Restore from %s FAILED (rc=%d). Dump preserved for '
                     'manual retry. stderr=%.1000s',
                     dump_path, result.returncode, (result.stderr or '').strip())
        return

    logger.info('[DB] Restore from %s completed successfully', dump_path)
    try:
        os.remove(dump_path)
        logger.info('[DB] Removed %s (restore complete, one-shot)', dump_path)
    except OSError as e:
        logger.warning('[DB] Could not remove restored dump %s: %s', dump_path, e)

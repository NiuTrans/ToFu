"""PostgreSQL verification probes — identity, health, binary presence.

Probes used across the bootstrap flow to answer "is the PG on host:port truly
OURS, alive, and serving?" without trusting a bare TCP-alive postmaster.

Extracted from the monolithic ``_bootstrap.py`` (facade-preserving split).
"""

import os
import shutil
import time

from lib.log import get_logger

from lib.database._pg_ownership import _find_pg_binary, _get_username

logger = get_logger(__name__)


def _verify_pg_data_directory(host, port, pgdata, pg_user):
    """Check that the PG on host:port uses OUR pgdata directory."""
    import subprocess
    db_user = pg_user or _get_username()
    psql_bin = _find_pg_binary('psql')
    try:
        result = subprocess.run(
            [psql_bin, '-h', host, '-p', str(port), '-U', db_user,
             '-d', 'template1', '-t', '-A',
             '-c', 'SHOW data_directory;'],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '5', 'PGGSSENCMODE': 'disable'}
        )
        if result.returncode == 0:
            remote_pgdata = result.stdout.strip()
            our_pgdata = os.path.realpath(pgdata)
            remote_real = os.path.realpath(remote_pgdata) if remote_pgdata else ''
            if remote_real and remote_real != our_pgdata:
                logger.warning(
                    '[DB] data_directory mismatch: PG on %s:%d uses %s, '
                    'but ours is %s', host, port, remote_pgdata, pgdata)
                return False
            logger.debug('[DB] data_directory verified: PG on %s:%d → %s', host, port, remote_pgdata)
            return True
        else:
            logger.debug('[DB] Could not verify data_directory on %s:%d: %s',
                        host, port, result.stderr.strip()[:200])
            return False  # fail-safe: cannot verify → refuse to match
    except FileNotFoundError:
        logger.debug('[DB] psql binary not found — cannot verify data_directory')
        return False  # fail-safe: no psql → refuse to match
    except Exception as e:
        logger.debug('[DB] data_directory check failed on %s:%d: %s', host, port, e)
        return False  # fail-safe: error → refuse to match


def _pg_has_database(host, port, dbname, pg_user):
    """Check if a PostgreSQL instance has a specific database."""
    import subprocess
    db_user = pg_user or _get_username()
    psql_bin = _find_pg_binary('psql')
    try:
        result = subprocess.run(
            [psql_bin, '-h', host, '-p', str(port), '-U', db_user,
             '-d', 'template1', '-t', '-A',
             '-c', f"SELECT 1 FROM pg_database WHERE datname = '{dbname}';"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, 'PGCONNECT_TIMEOUT': '5', 'PGGSSENCMODE': 'disable'}
        )
        if result.returncode == 0:
            has_it = result.stdout.strip() == '1'
            logger.debug('[DB] Database "%s" on %s:%d: %s',
                        dbname, host, port, 'exists' if has_it else 'NOT FOUND')
            return has_it
        else:
            logger.debug('[DB] Could not check database existence on %s:%d: %s',
                        host, port, result.stderr.strip()[:200])
            return True
    except Exception as e:
        logger.debug('[DB] Database existence check failed on %s:%d: %s', host, port, e)
        return True


def _pg_real_connect_ok(host, port, pg_user, pg_dbname, timeout_s=5):
    """Probe a PG host with a *real* connection, not just pg_isready.

    pg_isready returns OK as soon as postmaster accepts a TCP connection,
    even if the backend process that actually services queries is hung
    (common with "half-alive" containers on shared FUSE storage where
    the postmaster's FUSE-bound disk I/O is unreachable). A real
    psycopg2.connect() is what the app uses, so it's what we probe.

    Returns True if a fresh connection + trivial SELECT succeeds.
    """
    try:
        import psycopg2
    except ImportError:
        logger.debug('[DB] psycopg2 not importable — cannot do real-connect probe')
        return False
    db_user = pg_user or _get_username()
    dsn = f"host={host} port={port} dbname={pg_dbname or 'template1'} user={db_user}"
    try:
        conn = psycopg2.connect(
            dsn,
            connect_timeout=timeout_s,
            application_name='tofu-probe',
            gssencmode='disable',
        )
    except Exception as e:
        logger.debug('[DB] Real-connect probe to %s:%d failed: %s', host, port, e)
        return False
    try:
        cur = conn.cursor()
        cur.execute('SELECT 1')
        cur.fetchone()
        cur.close()
        return True
    except Exception as e:
        logger.debug('[DB] Real-connect probe query to %s:%d failed: %s', host, port, e)
        return False
    finally:
        try:
            conn.close()
        except Exception as _e:
            logger.debug('[DB] Real-connect probe close failed: %s', _e)


def _verify_pg_after_start(pg_port, pgdata, pg_user, total_wait_s=12):
    """Verify the PG we just started is truly ours and stays alive.

    pg_ctl start can succeed (rc=0) and yet the postmaster shuts itself
    down moments later. Three failure modes we must detect here:

    1. WAL recovery PANIC (e.g. "invalid resource manager ID in
       checkpoint record"). pg_ctl returns 0 because the postmaster
       process itself launched fine; the startup sub-process aborts
       seconds later and the postmaster shuts down.
    2. Concurrent-start race: another host's postmaster wrote a
       different PID to postmaster.pid AFTER our pg_ctl rc=0 but BEFORE
       we noticed. Our postmaster will discover this within ~60s and
       perform an "immediate shutdown because data directory lock file
       is invalid".
    3. data_directory mismatch: rare, but if a port collision dance
       lands us on someone else's PG, we should not declare success.

    Approach: poll over ~total_wait_s. At each tick verify (a) postmaster.pid
    still references a live local PG process, AND (b) a real psycopg2
    connect+SELECT 1 succeeds, AND (c) data_directory matches our pgdata.
    Require two consecutive successful checks before declaring victory.

    Returns True if PG is healthy, False otherwise. On failure the caller
    is expected to NOT take ownership and to fall back / retry.
    """
    deadline = time.monotonic() + total_wait_s
    consecutive_ok = 0
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    last_err = None
    while time.monotonic() < deadline:
        # Check 1 — pidfile + PID alive locally
        try:
            with open(pidfile) as _f:
                pid_str = _f.readline().strip()
            pid = int(pid_str)
        except FileNotFoundError as _e_audit:
            logger.debug('[_bootstrap] _verify_pg_after_start caught %s: %s', type(_e_audit).__name__, _e_audit)
            last_err = 'postmaster.pid disappeared'
            consecutive_ok = 0
            time.sleep(0.5)
            continue
        except (OSError, ValueError) as e:
            # OSError: permission/filesystem; ValueError: int(pid_str) on garbage.
            logger.debug('[DB:bootstrap] postmaster.pid unreadable: %s', e)
            last_err = f'postmaster.pid unreadable: {e}'
            consecutive_ok = 0
            time.sleep(0.5)
            continue
        try:
            from lib.compat import is_process_alive
            if not is_process_alive(pid):
                last_err = f'postmaster PID {pid} not alive (likely PANIC during recovery)'
                consecutive_ok = 0
                time.sleep(0.5)
                continue
        except ImportError as e:
            logger.debug('[DB] is_process_alive unavailable for verify: %s', e)
        # Check 2 — real psycopg2 connect + trivial query
        if not _pg_real_connect_ok('127.0.0.1', pg_port, pg_user, None, timeout_s=3):
            last_err = 'real psycopg2 SELECT 1 failed'
            consecutive_ok = 0
            time.sleep(1.0)
            continue
        # Check 3 — data_directory matches ours
        try:
            if not _verify_pg_data_directory('127.0.0.1', pg_port, pgdata, pg_user):
                last_err = 'data_directory mismatch (someone else\'s PG)'
                consecutive_ok = 0
                time.sleep(1.0)
                continue
        except Exception as e:
            logger.debug('[DB] _verify_pg_data_directory raised during verify: %s', e)
            last_err = f'data_directory probe raised: {e}'
            consecutive_ok = 0
            time.sleep(1.0)
            continue
        consecutive_ok += 1
        if consecutive_ok >= 2:
            return True
        time.sleep(0.5)
    logger.error('[DB] Post-start verification FAILED after %.1fs: %s',
                 total_wait_s, last_err)
    return False


def _pg_binaries_present():
    """Quick check: is pg_ctl available at all on this host?

    Returns True only if the core PG binaries are discoverable. This lets
    us bail out of the whole bootstrap flow early with a friendly
    "fallback to SQLite" message, instead of emitting a string of ERROR
    logs as we probe ports, scan directories, and finally try pg_ctl.
    """
    # _find_pg_binary returns the bare name as a fallback — but that only
    # works as a launch argument if PATH has the real binary. So we also
    # verify with shutil.which() that SOMETHING is there.
    pg_ctl = _find_pg_binary('pg_ctl')
    if os.path.isabs(pg_ctl) and os.path.isfile(pg_ctl):
        return True
    # Bare name — check PATH
    return shutil.which(pg_ctl) is not None

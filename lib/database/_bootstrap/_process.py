"""PostgreSQL process helpers — port read, quiet stop, port scan, shutdown.

Low-level helpers that read our configured port, stop a locally-started PG
(best-effort, used to undo a failed start), scan a port range for a PG that
owns our pgdata, and the public shutdown ``_stop_pg``.

Extracted from the monolithic ``_bootstrap.py`` (facade-preserving split).
"""

import os
import subprocess

from lib.log import get_logger

from lib.database._pg_ownership import _find_pg_binary, stop_heartbeat, _release_startup_lock
from lib.database._bootstrap._verify import _verify_pg_data_directory

logger = get_logger(__name__)


def _read_our_pg_port(pgdata):
    """Read the port from OUR postgresql.conf, if it exists."""
    conf_path = os.path.join(pgdata, 'postgresql.conf')
    if not os.path.isfile(conf_path):
        return None
    try:
        port = None
        with open(conf_path) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith('port') and '=' in stripped:
                    if stripped.startswith('#'):
                        continue
                    val = stripped.split('=', 1)[1].strip().split('#')[0].strip()
                    port = int(val)
        return port
    except Exception as e:
        logger.debug('[DB] Could not parse port from postgresql.conf: %s', e)
        return None


def _stop_local_pg_quietly(pgdata):
    """Best-effort pg_ctl stop -m fast, used to undo a failed start."""
    try:
        subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, 'stop', '-m', 'fast', '-w', '-t', '10'],
            capture_output=True, text=True, timeout=15
        )
        logger.info('[DB] Stopped local PG after failed post-start verification')
    except Exception as e:
        logger.debug('[DB] Quiet stop after failed verify raised: %s', e)


def _boot_stop_pg_quietly(pgdata):
    """Best-effort ``pg_ctl stop -m fast`` for a pgdata we just started."""
    try:
        subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, 'stop', '-m', 'fast', '-w', '-t', '20'],
            capture_output=True, text=True, timeout=30)
    except Exception as e:
        logger.debug('[DB-Seed] quiet stop of %s failed: %s', pgdata, e)


def _scan_for_our_pg(host, port_range, pgdata, pg_user):
    """Scan a range of ports for a PG instance that owns our pgdata."""
    for port in port_range:
        try:
            result = subprocess.run(
                [_find_pg_binary('pg_isready'), '-h', host, '-p', str(port), '-d', 'template1'],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode != 0:
                continue
            if _verify_pg_data_directory(host, port, pgdata, pg_user):
                logger.info('[DB] Found our PG on %s:%d (port scan recovery)', host, port)
                return port
        except Exception as e:
            logger.debug('[DB] Port scan probe %d failed: %s', port, e)
            continue
    return None


def _stop_pg(pgdata):
    """Stop PostgreSQL server on shutdown."""
    # Stop the heartbeat first so a peer host that starts up during
    # the pg_ctl stop window sees "no heartbeat" and takes over cleanly.
    stop_heartbeat(pgdata)
    if os.path.isdir(pgdata):
        try:
            subprocess.run(
                [_find_pg_binary('pg_ctl'), '-D', pgdata, 'stop', '-m', 'fast'],
                capture_output=True, text=True, timeout=30
            )
            logger.info('[DB] PostgreSQL stopped')
        except Exception as e:
            logger.warning('[DB] Error stopping PostgreSQL: %s', e)
    # Always release the cross-host startup lock on shutdown, regardless
    # of whether pg_ctl stop succeeded — a peer host is better off taking
    # over a potentially-stuck PG than being locked out forever.
    _release_startup_lock()

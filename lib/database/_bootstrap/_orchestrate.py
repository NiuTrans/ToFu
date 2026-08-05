"""Top-level bootstrap orchestrators — initdb, explicit-target, ensure-running.

The big flow drivers:

* ``_bootstrap_pg`` — initdb a brand-new pgdata, configure, start, verify,
  createdb, restore-from-dump, take ownership.
* ``_try_explicit_pg_target`` — handle an env-set (``TOFU_PG_HOST`` /
  ``TOFU_PG_PORT``) user-managed PG target.
* ``_ensure_pg_running`` — the full ensure-accessible orchestration: seed
  migration, binary check, explicit target, port reuse/scan, cross-host owner
  handoff, stale-pidfile cleanup, local start + self-heal.

Extracted from the monolithic ``_bootstrap.py`` (facade-preserving split).
"""

import os
import subprocess
import time

from lib.compat import IS_MACOS, IS_WINDOWS
from lib.env_compat import getenv_compat
from lib.log import get_logger

from lib.database._pg_ownership import (
    _find_pg_binary, _get_username, _find_free_port,
    _try_acquire_startup_lock, _release_startup_lock,
    _write_owner_host, _mark_pg_owned_locally,
    _read_pg_host_from_pidfile, _get_local_ip,
    _pidfile_pid_is_live_local_postgres,
    _pg_already_running_on_another_machine, _heartbeat_is_fresh,
    _HEARTBEAT_TTL_S, _verify_flock_support_or_warn, _clear_heartbeat,
    _fix_unix_socket_conf,
)
from lib.database._pg_seed import (
    _pgdata_major_compatible,
)
from lib.database._pg_backup import _try_self_heal_corrupt_pg

from lib.database._bootstrap._config import (
    _ensure_managed_pg_config, _restart_local_pg,
)
from lib.database._bootstrap._process import (
    _read_our_pg_port, _scan_for_our_pg, _stop_local_pg_quietly,
)
from lib.database._bootstrap._verify import (
    _verify_pg_after_start, _verify_pg_data_directory,
    _pg_binaries_present, _pg_real_connect_ok,
)
from lib.database._bootstrap._database import (
    _ensure_database_exists, _restore_from_sql_dump_if_present,
)

logger = get_logger(__name__)


def _bootstrap_pg(pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname):
    """Bootstrap a brand-new PostgreSQL data directory and start the server.

    Returns:
        dict with updated PG_HOST, PG_PORT, PG_DSN on success, or None on failure.
    """
    logger.info('[DB] Bootstrapping new PostgreSQL data directory at %s ...', pgdata)

    os.makedirs(os.path.dirname(pgdata), exist_ok=True)
    os.makedirs(os.path.join(base_dir, 'logs'), exist_ok=True)

    # initdb refuses a non-empty target dir. Earlier steps (the flock probe and
    # the cross-host startup lock) place tofu-owned marker files INSIDE pgdata
    # (.tofu_pg_start.lock, .tofu_flock_probe) before we get here, which would
    # make initdb fail with "directory not empty". Strip ONLY our own .tofu*
    # artifacts (never anything else — a genuinely populated cluster never
    # reaches this function because the caller gates on pgdata_is_populated).
    if os.path.isdir(pgdata):
        try:
            for _name in os.listdir(pgdata):
                if _name.startswith('.tofu'):
                    _p = os.path.join(pgdata, _name)
                    try:
                        os.remove(_p)
                    except OSError as _e:
                        logger.debug('[DB] Could not remove pre-initdb artifact %s: %s', _p, _e)
        except OSError as _e:
            logger.debug('[DB] Could not scan pgdata for pre-initdb artifacts: %s', _e)

    # initdb
    initdb_bin = _find_pg_binary('initdb')
    try:
        result = subprocess.run(
            [initdb_bin, '-D', pgdata, '--encoding=UTF8', '--locale=C',
             '--auth=trust', '--username=' + (pg_user or _get_username())],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            logger.error('[DB] initdb failed: %s', result.stderr)
            return None
        logger.info('[DB] initdb completed successfully')
    except FileNotFoundError:
        hint = 'conda install postgresql'
        if IS_MACOS:
            hint = 'brew install postgresql@18, or conda install postgresql'
        elif IS_WINDOWS:
            hint = 'install PostgreSQL and add PG bin/ to PATH'
        logger.error('[DB] initdb not found (looked for: %s) — install PostgreSQL '
                     '(e.g. %s)', initdb_bin, hint)
        return None
    except Exception as e:
        logger.error('[DB] initdb failed: %s', e, exc_info=True)
        return None

    # Pick a free port and configure
    free_port = _find_free_port(start=pg_port)
    conf_path = os.path.join(pgdata, 'postgresql.conf')
    try:
        with open(conf_path, 'a') as f:
            f.write('\n# ── Tofu auto-bootstrap overrides ──\n')
            f.write(f'port = {free_port}\n')
            f.write("listen_addresses = '*'\n")
            f.write("unix_socket_directories = ''\n")
        logger.info('[DB] Configured PG port=%d in postgresql.conf', free_port)
    except Exception as e:
        logger.error('[DB] Cannot write postgresql.conf: %s', e)
        return None

    # Apply the managed tuning block (max_connections / durability / WAL).
    # Written BEFORE the first start, so no restart is needed here.
    _ensure_managed_pg_config(pgdata)

    # Start PG — but first acquire the cross-host startup lock so we
    # don't race another tofu host that shares this pgdata.
    if not _try_acquire_startup_lock(pgdata):
        logger.warning('[DB] Skipping initdb-time pg_ctl start: another host '
                       'holds the cross-host startup lock. Falling back.')
        return None
    log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
    pg_ctl_bin = _find_pg_binary('pg_ctl')
    try:
        start_cmd = [pg_ctl_bin, '-D', pgdata, '-l', log_path, 'start']
        if IS_WINDOWS:
            # On Windows, pg_ctl start needs -w (wait) to be reliable
            start_cmd.insert(-1, '-w')
        result = subprocess.run(
            start_cmd,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            logger.error('[DB] pg_ctl start failed: %s', result.stderr)
            _release_startup_lock()
            return None
        logger.info('[DB] PostgreSQL started on port %d', free_port)
    except FileNotFoundError:
        hint = 'conda install postgresql'
        if IS_MACOS:
            hint = 'brew install postgresql@18, or conda install postgresql'
        elif IS_WINDOWS:
            hint = 'install PostgreSQL and add PG bin/ to PATH'
        logger.error('[DB] pg_ctl not found (looked for: %s) — install PostgreSQL '
                     '(e.g. %s)', pg_ctl_bin, hint)
        _release_startup_lock()
        return None
    except Exception as e:
        logger.error('[DB] pg_ctl start failed: %s', e, exc_info=True)
        _release_startup_lock()
        return None

    # Post-start verification: if the postmaster PANIC-shuts within a
    # few seconds (WAL corruption, concurrent-start race), fail fast.
    if not _verify_pg_after_start(free_port, pgdata, pg_user, total_wait_s=12):
        logger.error('[DB] Freshly initdb\'d PG failed post-start verification — '
                     'stopping it and aborting bootstrap. See logs/postgresql.log.')
        _stop_local_pg_quietly(pgdata)
        _release_startup_lock()
        return None

    time.sleep(1)

    # Create the database
    db_user = pg_user or _get_username()
    createdb_bin = _find_pg_binary('createdb')
    # Use 127.0.0.1 instead of 'localhost' — on macOS, DNS resolution of
    # 'localhost' can fail when network is misconfigured (e.g. iPhone tethering,
    # VPN) with: "could not translate host name 'localhost' to address".
    for _createdb_host in ('127.0.0.1', 'localhost'):
        try:
            result = subprocess.run(
                [createdb_bin, '-h', _createdb_host, '-p', str(free_port),
                 '-U', db_user, pg_dbname],
                capture_output=True, text=True, timeout=15
            )
            if result.returncode != 0:
                if 'already exists' in result.stderr:
                    logger.info('[DB] Database "%s" already exists', pg_dbname)
                    break  # success
                elif ('could not translate host name' in result.stderr
                      and _createdb_host == '127.0.0.1'):
                    # Shouldn't happen with 127.0.0.1, but just in case
                    continue
                else:
                    logger.error('[DB] createdb failed: %s', result.stderr)
                    return None
            else:
                logger.info('[DB] Created database "%s"', pg_dbname)
                break  # success
        except Exception as e:
            logger.error('[DB] createdb failed: %s', e, exc_info=True)
            return None

    # ── Restore from pg_backup.sql if export.py left one behind ──
    # export.py's personal-mode export NEVER raw-copies pgdata/ (hot-copy
    # across FUSE causes TOAST chunk corruption). Instead it does a
    # pg_dumpall → data/pg_backup.sql. On the destination's first boot,
    # we've just finished initdb + createdb above — so this is exactly
    # the moment to feed the dump into psql. On success we delete the
    # dump file so subsequent boots skip straight through.
    _restore_from_sql_dump_if_present(base_dir, free_port, db_user, pg_dbname)

    # Build DSN
    dsn = f"host=127.0.0.1 port={free_port} dbname={pg_dbname}"
    if pg_user:
        dsn += f" user={pg_user}"
    if pg_password:
        dsn += f" password={pg_password}"
    _write_owner_host(pgdata)
    _mark_pg_owned_locally(pgdata)
    logger.info('[DB] Bootstrap complete — DSN: host=127.0.0.1 port=%d dbname=%s',
                free_port, pg_dbname)
    return {'PG_HOST': '127.0.0.1', 'PG_PORT': free_port, 'PG_DSN': dsn}


def _try_explicit_pg_target(pgdata, base_dir, pg_host, pg_port, build_dsn):
    """Step 1 of ``_ensure_pg_running``: handle an explicit env-set PG target.

    When ``TOFU_PG_HOST`` names a remote host, OR ``TOFU_PG_PORT`` is set (even
    for localhost), the user manages PG externally — connect directly rather
    than bootstrap.  A LOCAL explicit port that names OUR OWN pgdata is special:
    an unreachable target then means "our local PG is currently down" (e.g. the
    Restart button stopped it and the new server raced ahead), so we fall
    through to the local start path instead of failing to SQLite.

    Returns
    -------
    (handled: bool, result: dict | None)
        handled=True  → the caller must ``return result`` (result is the PG
                        info dict on success, or None on a hard failure).
        handled=False → no explicit target, OR an explicit-LOCAL-ours target is
                        down → the caller falls through to Step 2 (local start).
    """
    explicit_host = getenv_compat('TOFU_PG_HOST')
    explicit_port = getenv_compat('TOFU_PG_PORT', default=None)
    is_explicit_external = (
        (explicit_host and explicit_host not in ('localhost', '127.0.0.1', '::1'))
        or explicit_port is not None  # any explicit port = user-managed PG
    )
    if not is_explicit_external:
        return False, None

    target_host = explicit_host or pg_host
    target_port = int(explicit_port) if explicit_port else pg_port
    # A local explicit port (e.g. TOFU_PG_PORT pointing at 127.0.0.1)
    # almost always names OUR OWN pgdata started by a previous server.py —
    # NOT a truly external/unmanaged PG. If that cluster is OURS and we
    # have the binaries to manage it, an unreachable target just means
    # "our local PG is currently down" (e.g. the user clicked Restart,
    # which stops PG, and the new server raced ahead of it). In that case
    # we must START it ourselves rather than give up and fall back to a
    # near-empty SQLite. Only a genuinely external target (remote host, or
    # a local port whose pgdata isn't ours) is strictly connect-or-fail.
    target_is_local = target_host in ('localhost', '127.0.0.1', '::1')
    target_is_ours = (
        target_is_local
        and _read_our_pg_port(pgdata) == target_port
        and _pg_binaries_present()
    )
    logger.info('[DB] Using explicit PG target from env: %s:%d (manageable_local=%s)',
                target_host, target_port, target_is_ours)
    # Try psycopg2 directly (no pg_isready binary needed — works in CI)
    try:
        import psycopg2
        test_dsn = build_dsn(target_host, target_port)
        conn = psycopg2.connect(test_dsn, connect_timeout=5)
        conn.close()
        logger.info('[DB] Explicit PG target %s:%d is reachable', target_host, target_port)
        # Manage our own cluster's tuning so the connection / WAL settings
        # stay in sync (otherwise PG keeps its initdb defaults — e.g.
        # max_connections=200 — below the app-side TOFU_DB_MAX_CONNS
        # ceiling, producing 'too many clients' FATALs).
        if target_is_local and _read_our_pg_port(pgdata) == target_port:
            _mark_pg_owned_locally(pgdata)
            if _ensure_managed_pg_config(pgdata):
                _restart_local_pg(pgdata, base_dir)
        return True, {'PG_HOST': target_host, 'PG_PORT': target_port,
                      'PG_DSN': test_dsn}
    except ImportError:
        logger.error('[DB] psycopg2 not installed — cannot connect to explicit PG')
        return True, None
    except Exception as e:
        if target_is_ours:
            logger.warning('[DB] Explicit local PG target %s:%d is down (%s) — '
                           'it names OUR pgdata, so attempting to START it '
                           'locally instead of falling back to SQLite.',
                           target_host, target_port, e)
            # Fall through to the local start/bootstrap path (Step 2+).
            return False, None
        logger.error('[DB] Explicit PG target %s:%d not reachable: %s',
                     target_host, target_port, e)
        return True, None


def _ensure_pg_running(pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname):
    """Ensure PostgreSQL is accessible. Start locally or discover remote instance.

    Returns:
        dict with PG_HOST, PG_PORT, PG_DSN on success, or None on failure.
    """
    def _build_dsn(host, port):
        dsn = f"host={host} port={port} dbname={pg_dbname}"
        if pg_user:
            dsn += f" user={pg_user}"
        if pg_password:
            dsn += f" password={pg_password}"
        return dsn

    # ── Step -1: local-primary migration (seed + atomic same-boot flip) ──
    # Server-boot only (the migrator gates on TOFU_SERVER_PROCESS): when the
    # local-primary split is engaged and the local pgdata is unpopulated or
    # stale, this seeds it from legacy and flips THIS boot to serve local —
    # no two-restart window. On skip/failure the returned pgdata is unchanged
    # (legacy stays canonical) and serving continues below. Idempotent +
    # verify-gated + cooldown-marked; see _pg_seed._migrate_local_primary_if_due.
    try:
        from lib.database._pg_seed import _migrate_local_primary_if_due
        pgdata = _migrate_local_primary_if_due(
            pgdata, base_dir, pg_port, pg_user, pg_password, pg_dbname)
    except Exception as _se:
        logger.error('[DB-Seed] migration hook raised (continuing on %s): %s',
                     pgdata, _se, exc_info=True)

    # ── Step 0: Early bail if PG binaries are simply not installed ──
    # Unless the user has explicitly set TOFU_PG_HOST to a remote, there's
    # no point probing anything — we can't start, query, or verify PG.
    # This turns a noisy "ERROR: pg_ctl not found" trace into a single
    # friendly INFO line, and the caller seamlessly falls back to SQLite.
    _explicit_host = getenv_compat('TOFU_PG_HOST')
    _explicit_remote = (_explicit_host
                        and _explicit_host not in ('localhost', '127.0.0.1', '::1'))
    if not _explicit_remote and not _pg_binaries_present():
        logger.info(
            '[DB] PostgreSQL client binaries (pg_ctl, initdb, psql) not found '
            'on this host — SKIPPING PG bootstrap and falling back to SQLite. '
            'This is normal when PG is not installed. '
            'To enable PG (better concurrency for 100+ users): '
            'conda install -c conda-forge postgresql>=18'
        )
        return None

    # ── Step 1: Explicit host/port override (see _try_explicit_pg_target) ──
    # An env-set remote host or any explicit port = user-managed PG: connect
    # directly. handled=True → return its result; handled=False → fall through
    # to the local start path (no explicit target, or our-own-local PG is down).
    _handled, _explicit_result = _try_explicit_pg_target(
        pgdata, base_dir, pg_host, pg_port, _build_dsn)
    if _handled:
        return _explicit_result

    # ── Step 2: Read OUR port from OUR postgresql.conf ──
    our_port = _read_our_pg_port(pgdata)
    if our_port is not None:
        pg_port = our_port
        logger.info('[DB] Read port=%d from our postgresql.conf', our_port)

        try:
            # Use 127.0.0.1 — 'localhost' DNS can fail on macOS with certain
            # network configs (iPhone tethering, VPN, etc.)
            _local = '127.0.0.1'
            result = subprocess.run(
                [_find_pg_binary('pg_isready'), '-h', _local, '-p', str(pg_port), '-d', 'template1'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                is_ours = _verify_pg_data_directory(_local, pg_port, pgdata, pg_user)
                if is_ours:
                    logger.info('[DB] PostgreSQL already running on %s:%d (verified ours)', _local, pg_port)
                    _ensure_database_exists(_local, pg_port, pg_dbname, pg_user, pgdata)
                    # Already-running local PG on our pgdata — almost
                    # certainly started by a previous server.py on this
                    # host. Take ownership so shutdown_pool stops it.
                    _mark_pg_owned_locally(pgdata)
                    # Re-apply managed tuning; restart only if it changed a
                    # restart-only setting (max_connections / wal_level).
                    if _ensure_managed_pg_config(pgdata):
                        _restart_local_pg(pgdata, base_dir)
                    return {'PG_HOST': _local, 'PG_PORT': pg_port,
                            'PG_DSN': _build_dsn(_local, pg_port)}
                else:
                    # NOT our PG — NEVER reuse another project's PG just because
                    # it has a database with the same name. This prevents cross-project
                    # data leakage and PID-file duel crashes.
                    logger.warning(
                        '[DB] PG on %s:%d is NOT ours (data_directory mismatch) '
                        '— REFUSING to reuse. Scanning nearby ports for our PG.',
                        _local, pg_port)
                    found_our_port = _scan_for_our_pg(_local, range(15432, 15440), pgdata, pg_user)
                    if found_our_port:
                        _ensure_database_exists(_local, found_our_port, pg_dbname, pg_user, pgdata)
                        _mark_pg_owned_locally(pgdata)
                        return {'PG_HOST': _local, 'PG_PORT': found_our_port,
                                'PG_DSN': _build_dsn(_local, found_our_port)}
        except Exception as _e:
            logger.debug('[DB] pg_isready localhost:%d check failed: %s', pg_port, _e)

    # ── Step 2b: Scan nearby ports for our PG by data_directory ──
    # Only match by data_directory verification — NEVER by database name alone.
    # This prevents cross-project data leakage when exported copies share the
    # same database name but must use independent PG instances.
    _local = '127.0.0.1'
    found_our_port = _scan_for_our_pg(_local, range(15432, 15440), pgdata, pg_user)
    if found_our_port:
        _ensure_database_exists(_local, found_our_port, pg_dbname, pg_user, pgdata)
        _mark_pg_owned_locally(pgdata)
        return {'PG_HOST': _local, 'PG_PORT': found_our_port,
                'PG_DSN': _build_dsn(_local, found_our_port)}

    # ── Step 3: Check if another machine owns the pgdata ──
    #
    # Defer to the remote ONLY if a fresh tofu heartbeat proves another
    # tofu process is actively using that PG right now. A bare TCP-alive
    # postmaster is not enough: it could be the stale tail of a previous,
    # unclean exit on another host — in which case we must take over
    # rather than route every DB call across a dying link (see
    # .tofu/memories/pg-cross-host-heartbeat-takeover.md).
    is_remote, remote_host = _pg_already_running_on_another_machine(pgdata, pg_port)
    if is_remote and remote_host:
        fresh, hb_info = _heartbeat_is_fresh(pgdata)
        if fresh:
            remote_ok = _pg_real_connect_ok(remote_host, pg_port, pg_user, pg_dbname, timeout_s=5)
            if remote_ok:
                logger.info('[DB] PostgreSQL is running on remote machine %s '
                            '(heartbeat fresh, age=%.1fs, pid=%s) — connecting as client',
                            remote_host, hb_info.get('age_s', -1) if hb_info else -1,
                            hb_info.get('pid') if hb_info else None)
                _ensure_database_exists(remote_host, pg_port, pg_dbname, pg_user, pgdata)
                return {'PG_HOST': remote_host, 'PG_PORT': pg_port,
                        'PG_DSN': _build_dsn(remote_host, pg_port)}
            logger.warning('[DB] Heartbeat was fresh but real-connect to %s:%d failed — '
                          'treating as dead and taking over locally', remote_host, pg_port)
        else:
            if hb_info is None:
                logger.info('[DB] Remote PG owner %s present but no tofu heartbeat file '
                            '— previous owner exited uncleanly; taking over locally',
                            remote_host)
            else:
                logger.info('[DB] Remote PG owner %s has a STALE heartbeat '
                            '(age=%.1fs > ttl=%ds, last_pid=%s) — previous owner is gone; '
                            'taking over locally',
                            remote_host, hb_info.get('age_s', -1),
                            _HEARTBEAT_TTL_S, hb_info.get('pid'))

    # ── Step 3b: pgdata ↔ binary major-version sanity check ──
    # A pgdata created by a different PG major than the installed binary
    # FATALs on start (config-param mismatch) → scheduler retry-storm. Detect
    # it (see _pgdata_major_compatible) and fall back to SQLite cleanly.
    if not _pgdata_major_compatible(pgdata):
        return None

    # ── Step 4/5: Start PG locally or bootstrap ──
    # Before any local start/takeover, verify the pgdata mount truly enforces
    # advisory locks. A silent-no-op filesystem makes the cross-host startup
    # interlock useless (two hosts both "acquire" and double-start → WAL
    # corruption). Warn loudly, or refuse entirely if TOFU_PG_REQUIRE_FLOCK.
    if not _verify_flock_support_or_warn(pgdata):
        return None

    # Gate initdb on whether pgdata is a POPULATED cluster (has PG_VERSION /
    # postgresql.conf), NOT on mere directory existence. Earlier steps in this
    # function create the directory as a side-effect of placing their lock/probe
    # files — _verify_flock_support_or_warn()'s _probe_flock_enforced and
    # _try_acquire_startup_lock both os.makedirs(pgdata). On a first-ever boot of
    # a RESOLVED path that didn't exist yet (the local-disk split's
    # $TOFU_DB_LOCAL_ROOT/pgdata is the common case), that left an EMPTY dir here,
    # so `not os.path.isdir(pgdata)` was False → initdb was skipped → we fell
    # through to `pg_ctl start` on an empty dir → "is not a database cluster
    # directory" → silent SQLite fallback (the exact reported bug). initdb
    # accepts an existing EMPTY dir, and _bootstrap_pg strips any leftover tofu
    # lock/probe artifacts first, so bootstrapping here is safe and idempotent.
    from lib.database.db_paths import pgdata_is_populated as _pgdata_populated
    if not _pgdata_populated(pgdata):
        logger.info('[DB] pgdata %s is not an initialized cluster — '
                    'bootstrapping new PostgreSQL instance', pgdata)
        result = _bootstrap_pg(pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname)
        if not result:
            logger.error('[DB] Bootstrap failed — refusing to connect to '
                         'default 127.0.0.1:%d (may be another project)', pg_port)
        return result

    # Clean up stale pidfile
    #
    # Container-switch scenario: a user uses web-based VS Code and moves
    # between containers, so the machine IP changes but only ONE container
    # is live at any time. The `.pg_owner_host` marker from the previous
    # container will point at an IP that no longer runs PG. Treat such a
    # marker as stale — probe reachability first before deferring to it.
    #
    # Rule:
    #   - Remote host reachable on PG port → concurrent multi-host scenario,
    #     defer to remote (preserves the original cross-machine safety net).
    #   - Remote host NOT reachable → previous owner is dead (container gone
    #     or machine switched), auto-heal by removing stale markers and
    #     starting PG locally. This makes container switches a no-op.
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    if os.path.exists(pidfile):
        owner_host = _read_pg_host_from_pidfile(pgdata)
        local_ip = _get_local_ip()
        # IP-independent guard FIRST: if the pidfile PID is a live local
        # postgres, this host already owns the cluster. _get_local_ip() can
        # flap (container IP reassignment), which previously caused a host to
        # see its OWN postmaster as "remote", delete the pidfile, and start a
        # SECOND postmaster on the same pgdata → pg_subtrans corruption.
        # Reuse the running instance instead of taking over.
        if _pidfile_pid_is_live_local_postgres(pgdata):
            conf_port = _read_our_pg_port(pgdata) or pg_port
            if _verify_pg_data_directory('127.0.0.1', conf_port, pgdata, pg_user):
                logger.warning('[DB] Step 4: postmaster.pid is a LIVE local postgres '
                               '(owner_host marker=%s, local_ip=%s) — reusing it '
                               'instead of taking over (IP-flap safe).',
                               owner_host, local_ip)
                _ensure_database_exists('127.0.0.1', conf_port, pg_dbname, pg_user, pgdata)
                _write_owner_host(pgdata)
                _mark_pg_owned_locally(pgdata)
                return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port,
                        'PG_DSN': _build_dsn('127.0.0.1', conf_port)}
            logger.warning('[DB] Step 4: pidfile PID is live postgres but data_directory '
                           'verify on 127.0.0.1:%d failed — proceeding with caution.', conf_port)
        if owner_host and owner_host not in (local_ip, 'localhost', '127.0.0.1'):
            # Heartbeat is the authoritative signal: only defer if another
            # tofu is actively running there. Bare TCP-alive postmaster
            # is not enough (an unclean exit can leave it answering for
            # hours).
            fresh, hb_info = _heartbeat_is_fresh(pgdata)
            remote_alive = fresh and _pg_real_connect_ok(
                owner_host, pg_port, pg_user, pg_dbname, timeout_s=5)
            if remote_alive:
                logger.warning('[DB] Step 4 safety net: postmaster.pid belongs to '
                               'remote host %s (we are %s) and tofu heartbeat is '
                               'fresh (age=%.1fs, pid=%s) — refusing to delete. '
                               'Connecting to remote host.',
                               owner_host, local_ip,
                               hb_info.get('age_s', -1) if hb_info else -1,
                               hb_info.get('pid') if hb_info else None)
                _ensure_database_exists(owner_host, pg_port, pg_dbname, pg_user, pgdata)
                return {'PG_HOST': owner_host, 'PG_PORT': pg_port,
                        'PG_DSN': _build_dsn(owner_host, pg_port)}
            # Stale or missing heartbeat — previous owner is gone (unclean
            # exit, container switched, machine rebooted). Auto-heal: remove
            # ownership markers and proceed to start PG locally. Data
            # files are untouched.
            if hb_info is None:
                logger.warning('[DB] Step 4 auto-heal: previous owner %s exited '
                               'uncleanly (no heartbeat file) — taking over locally.',
                               owner_host)
            elif not fresh:
                logger.warning('[DB] Step 4 auto-heal: previous owner %s heartbeat is '
                               'stale (age=%.1fs > ttl=%ds, last_pid=%s) — '
                               'taking over locally.',
                               owner_host, hb_info.get('age_s', -1),
                               _HEARTBEAT_TTL_S, hb_info.get('pid'))
            else:
                logger.warning('[DB] Step 4 auto-heal: previous owner %s heartbeat '
                               'fresh but PG unreachable — taking over locally.',
                               owner_host)
            owner_file = os.path.join(pgdata, '.pg_owner_host')
            try:
                if os.path.exists(owner_file):
                    os.remove(owner_file)
                    logger.info('[DB] Removed stale .pg_owner_host (was %s)', owner_host)
            except Exception as _e:
                logger.warning('[DB] Could not remove stale .pg_owner_host: %s', _e)
            _clear_heartbeat(pgdata)
        else:
            logger.warning('[DB] Removing stale postmaster.pid before starting PG '
                          '(owner: %s, us: %s)', owner_host, local_ip)
        # Cross-host HARD interlock — the real anti-corruption barrier.
        #
        # We are about to delete a postmaster.pid and start our own
        # postmaster. Removing the pidfile defeats PostgreSQL's OWN
        # single-postmaster guard, so THIS is the catastrophic step: if
        # every IP/PID/heartbeat heuristic above was wrong and the "dead"
        # owner is actually a LIVE peer on another host, deleting its
        # pidfile and starting a second postmaster on the shared FUSE
        # pgdata corrupts WAL / pg_subtrans. A live peer holds this flock
        # for its entire lifetime, so we acquire it BEFORE the deletion:
        # if another host holds it, two postmasters physically cannot
        # coexist — we refuse to take over and fall back. (The later
        # pg_ctl-start acquisition is idempotent — a no-op once held here.)
        if not _try_acquire_startup_lock(pgdata):
            logger.warning('[DB] Refusing to remove postmaster.pid / take over: '
                           'another host holds the cross-host startup lock on '
                           'pgdata=%s — a live peer owns this PG. Falling back '
                           '(SQLite / retry next cycle).', pgdata)
            return None
        try:
            os.remove(pidfile)
        except FileNotFoundError:
            # Already gone (race with another cleanup path) — fine.
            logger.debug('[DB] postmaster.pid already removed')
        except PermissionError as e:
            if IS_WINDOWS:
                logger.error('[DB] Cannot remove stale pidfile (file locked by another process '
                             '— PG may still be running): %s', e)
            else:
                logger.error('[DB] Cannot remove stale pidfile: %s', e)
            _release_startup_lock()
            return None
        except Exception as e:
            logger.error('[DB] Cannot remove stale pidfile: %s', e)
            _release_startup_lock()
            return None

    _fix_unix_socket_conf(pgdata)

    # Check if configured port is taken (possibly by our own orphaned PG)
    conf_port = _read_our_pg_port(pgdata) or pg_port
    try:
        check = subprocess.run(
            [_find_pg_binary('pg_isready'), '-h', '127.0.0.1', '-p', str(conf_port), '-d', 'template1'],
            capture_output=True, text=True, timeout=3
        )
        if check.returncode == 0:
            # PG is already responding on our port — check if it's ours
            if _verify_pg_data_directory('127.0.0.1', conf_port, pgdata, pg_user):
                logger.info('[DB] PG already running on 127.0.0.1:%d (our data_directory) '
                           '— reusing after pidfile cleanup', conf_port)
                _ensure_database_exists('127.0.0.1', conf_port, pg_dbname, pg_user, pgdata)
                _write_owner_host(pgdata)
                _mark_pg_owned_locally(pgdata)
                if _ensure_managed_pg_config(pgdata):
                    _restart_local_pg(pgdata, base_dir)
                return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port,
                        'PG_DSN': _build_dsn('127.0.0.1', conf_port)}
            # Not ours — reassign to a different port
            free_port = _find_free_port(start=conf_port + 1)
            if free_port is None:
                logger.error('[DB] No free port found — cannot start PG')
                _release_startup_lock()
                return None
            logger.info('[DB] Port %d is occupied by another PG — reassigning to %d',
                       conf_port, free_port)
            _conf_path = os.path.join(pgdata, 'postgresql.conf')
            try:
                with open(_conf_path) as _f:
                    _lines = _f.readlines()
                with open(_conf_path, 'w') as _f:
                    for _line in _lines:
                        _s = _line.strip()
                        if _s.startswith('port') and '=' in _s and not _s.startswith('#'):
                            _f.write(f'port = {free_port}\n')
                        else:
                            _f.write(_line)
                pg_port = free_port
                logger.info('[DB] Updated postgresql.conf: port = %d', free_port)
            except Exception as _e:
                logger.error('[DB] Failed to update postgresql.conf port: %s', _e)
                _release_startup_lock()
                return None
    except Exception as _e:
        logger.debug('[DB] Port availability check failed: %s', _e)

    logger.info('[DB] Starting PostgreSQL server from %s ...', pgdata)
    # Cross-host startup lock — prevents two tofu hosts on the same
    # FUSE-mounted pgdata from racing into pg_ctl start at the same time
    # (which corrupts WAL with mutual-PID-eviction).
    if not _try_acquire_startup_lock(pgdata):
        logger.warning('[DB] Another tofu host is currently starting/owning PG '
                       'on this pgdata — skipping our pg_ctl start. Caller will '
                       'fall back to SQLite (or retry next cycle).')
        return None
    try:
        log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
        result = subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, '-l', log_path, 'start'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            logger.info('[DB] PostgreSQL started successfully on this machine')
            # Verify it stays up — pg_ctl rc=0 does NOT mean recovery
            # succeeded. If WAL is corrupted or another host's pidfile
            # races us, the postmaster will shut itself down within
            # seconds. Catch that here instead of letting the scheduler
            # storm-retry.
            if not _verify_pg_after_start(pg_port, pgdata, pg_user, total_wait_s=12):
                logger.error('[DB] PG started (rc=0) but failed post-start '
                             'verification — likely WAL corruption or concurrent '
                             'start by another host. Stopping local PG and '
                             'attempting automatic self-heal. See logs/postgresql.log.')
                _stop_local_pg_quietly(pgdata)
                # Automatic corruption recovery (pg_resetwal → restore-from-backup)
                # BEFORE giving up. We still hold the cross-host startup lock, so
                # no peer can race us during recovery. Released after either way.
                healed = _try_self_heal_corrupt_pg(
                    pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname)
                _release_startup_lock()
                return healed
            _ensure_database_exists('127.0.0.1', pg_port, pg_dbname, pg_user, pgdata)
            _write_owner_host(pgdata)
            _mark_pg_owned_locally(pgdata)
            return {'PG_HOST': '127.0.0.1', 'PG_PORT': pg_port,
                    'PG_DSN': _build_dsn('127.0.0.1', pg_port)}
        else:
            logger.error('[DB] Failed to start PostgreSQL: %s', result.stderr)
            _release_startup_lock()
            return None
    except FileNotFoundError as e:
        # pg_ctl / initdb binary not present — PostgreSQL is simply not
        # installed on this host. This is a normal "PG not available →
        # fallback to SQLite" path, NOT a bug. Log at INFO level so it's
        # clear the system is intentionally degrading.
        logger.info('[DB] PostgreSQL binaries not found on this host (%s). '
                    'This is normal — tofu will automatically use SQLite. '
                    'To enable PG (better concurrency): '
                    '  conda install -c conda-forge postgresql>=18',
                    e)
        _release_startup_lock()
        return None
    except Exception as e:
        logger.error('[DB] Failed to start PostgreSQL: %s', e, exc_info=True)
        _release_startup_lock()
        return None

"""PostgreSQL server management — auto-bootstrap, start, stop, remote discovery.

Extracted from _core.py for modularity. Called from _core at import time.
Cross-platform: works on Linux, macOS, and Windows.
"""

import getpass
import os
import shutil
import subprocess
import time

from lib.compat import IS_LINUX, IS_MACOS, IS_WINDOWS
from lib.log import get_logger

logger = get_logger(__name__)


def _find_pg_binary(name):
    """Locate a PostgreSQL binary by name, cross-platform.

    Uses ``shutil.which()`` which respects PATH on all platforms.
    On Windows, also checks common PostgreSQL install locations.

    Args:
        name: Binary name without extension (e.g. 'pg_ctl', 'initdb').

    Returns:
        Full path to the binary, or *name* itself if not found
        (so subprocess will raise FileNotFoundError with a clear message).
    """
    found = shutil.which(name)
    if found:
        return found
    # On macOS, try common Homebrew / MacPorts / Conda locations
    if IS_MACOS:
        mac_paths = [
            # Homebrew (Apple Silicon)
            '/opt/homebrew/bin',
            '/opt/homebrew/opt/postgresql/bin',
            # Homebrew (Intel)
            '/usr/local/bin',
            '/usr/local/opt/postgresql/bin',
            # MacPorts
            '/opt/local/bin',
            # Postgres.app
            '/Applications/Postgres.app/Contents/Versions/latest/bin',
        ]
        # Also check all Homebrew-versioned postgresql formulae
        for prefix in ['/opt/homebrew/opt', '/usr/local/opt']:
            for pg_ver in range(18, 12, -1):
                mac_paths.append(os.path.join(prefix, f'postgresql@{pg_ver}', 'bin'))
        # Check Conda envs — the user's active conda env and base
        conda_prefix = os.environ.get('CONDA_PREFIX', '')
        if conda_prefix:
            mac_paths.insert(0, os.path.join(conda_prefix, 'bin'))
        conda_base = os.environ.get('CONDA_PREFIX_1', '')  # base env when sub-env is active
        if conda_base:
            mac_paths.append(os.path.join(conda_base, 'bin'))
        for d in mac_paths:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate):
                logger.info('[DB] Found %s at %s', name, candidate)
                return candidate
    # On Windows, try common PostgreSQL install paths
    if IS_WINDOWS:
        for pg_ver in range(18, 12, -1):
            candidate = os.path.join(
                os.environ.get('ProgramFiles', r'C:\Program Files'),
                'PostgreSQL', str(pg_ver), 'bin', f'{name}.exe'
            )
            if os.path.isfile(candidate):
                logger.info('[DB] Found %s at %s', name, candidate)
                return candidate
    # Return bare name — subprocess will raise FileNotFoundError
    return name


def _get_username(fallback='postgres'):
    """Get OS username cross-platform (Linux USER, Windows USERNAME)."""
    try:
        return getpass.getuser()
    except Exception:
        return fallback


def _read_pg_host_from_pidfile(pgdata):
    """Read the PG owner host from .pg_owner_host on shared FUSE storage."""
    owner_file = os.path.join(pgdata, '.pg_owner_host')
    try:
        if os.path.exists(owner_file):
            with open(owner_file) as f:
                host = f.read().strip()
            if host:
                return host
    except Exception as e:
        logger.debug('[DB] Could not read .pg_owner_host: %s', e)
    return None


def _get_local_ip():
    """Get this machine's IP address (non-loopback)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as _e:
        logger.debug('[DB] UDP socket IP detection failed: %s', _e)
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception as _e2:
        logger.debug('[DB] gethostbyname fallback also failed: %s — returning 127.0.0.1', _e2)
        return '127.0.0.1'


def _write_owner_host(pgdata):
    """Write our IP to .pg_owner_host so other machines know where to connect."""
    owner_file = os.path.join(pgdata, '.pg_owner_host')
    try:
        ip = _get_local_ip()
        with open(owner_file, 'w') as f:
            f.write(ip)
        logger.info('[DB] Wrote PG owner host: %s', ip)
    except Exception as e:
        logger.warning('[DB] Could not write .pg_owner_host: %s', e)


def _pg_already_running_on_another_machine(pgdata, pg_port):
    """Check if another machine owns the PG data directory.

    Returns:
        (True, host_ip) if another machine has PG running on this pgdata,
        (False, None) otherwise.
    """
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    if not os.path.exists(pidfile):
        logger.debug('[DB] No postmaster.pid — PG not running')
        return False, None

    try:
        with open(pidfile) as f:
            lines = f.readlines()
        if len(lines) < 2:
            logger.debug('[DB] postmaster.pid too short (%d lines) — treating as absent', len(lines))
            return False, None
        pid = int(lines[0].strip())
    except Exception as e:
        logger.warning('[DB] Cannot parse postmaster.pid: %s', e)
        return False, None

    owner_host = _read_pg_host_from_pidfile(pgdata)
    local_ip = _get_local_ip()
    is_remote_owner = (
        owner_host is not None
        and owner_host not in (local_ip, 'localhost', '127.0.0.1')
    )

    logger.info('[DB] postmaster.pid: PID=%d, owner_host=%s, local_ip=%s, is_remote=%s',
                pid, owner_host, local_ip, is_remote_owner)

    if is_remote_owner:
        reachable = False
        try:
            result = subprocess.run(
                [_find_pg_binary('pg_isready'), '-h', owner_host, '-p', str(pg_port)],
                capture_output=True, text=True, timeout=5
            )
            reachable = (result.returncode == 0)
        except Exception as e:
            logger.debug('[DB] pg_isready to %s failed: %s', owner_host, e)
        logger.info('[DB] PG owned by remote host %s (reachable=%s) — deferring to it', owner_host, reachable)
        return True, owner_host

    try:
        from lib.compat import is_process_alive, is_process_named
        if not is_process_alive(pid):
            raise ProcessLookupError(f'PID {pid} not alive')
        try:
            if is_process_named(pid, 'postgres'):
                logger.debug('[DB] PID %d is local postgres — already running', pid)
                return False, None
            else:
                logger.info('[DB] PID %d exists locally but is not postgres — stale pidfile', pid)
                return False, None
        except Exception as e:
            logger.warning('[DB] Cannot check PID %d command: %s — assuming stale', pid, e)
            return False, None
    except ProcessLookupError:
        logger.info('[DB] PID %d not found locally, owner=%s (us) — stale pidfile', pid, owner_host or 'unknown')
        return False, None
    except PermissionError:
        logger.info('[DB] Cannot signal PID %d (PermissionError) — assuming local PG running', pid)
        return False, None


def _find_free_port(start=15432, end=15500):
    """Find an available TCP port in [start, end) for PostgreSQL."""
    import socket
    for port in range(start, end):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            result = s.connect_ex(('127.0.0.1', port))
            s.close()
            if result != 0:
                return port
        except Exception:
            return port
    logger.warning('[DB] No free port found in %d–%d, falling back to %d', start, end, start)
    return start


def _fix_unix_socket_conf(pgdata):
    """Patch postgresql.conf to disable Unix sockets if needed.

    Disables Unix sockets on:
      - FUSE filesystems (Linux: /mnt/ paths) — FUSE doesn't support AF_UNIX
      - Windows — Unix domain sockets are only partially supported
    On macOS with local disk, Unix sockets are fine — skip patching.
    """
    # Decide if we need to disable unix sockets
    if IS_WINDOWS:
        reason = 'Windows (Unix sockets not reliably supported)'
    elif IS_LINUX and pgdata.startswith('/mnt/'):
        reason = 'FUSE filesystem does not support Unix sockets'
    else:
        # macOS and Linux on local disk — Unix sockets are fine
        return

    conf_path = os.path.join(pgdata, 'postgresql.conf')
    if not os.path.isfile(conf_path):
        return
    try:
        with open(conf_path) as f:
            content = f.read()
        if "unix_socket_directories = ''" in content:
            return
        import re
        new_content, count = re.subn(
            r"unix_socket_directories\s*=\s*'[^']*'",
            "unix_socket_directories = ''",
            content
        )
        if count > 0:
            with open(conf_path, 'w') as f:
                f.write(new_content)
            logger.info('[DB] Patched postgresql.conf: disabled unix_socket_directories (%s)', reason)
    except Exception as e:
        logger.warning('[DB] Could not patch unix_socket_directories in postgresql.conf: %s', e)


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


def _verify_pg_data_directory(host, port, pgdata, pg_user):
    """Check that the PG on host:port uses OUR pgdata directory."""
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
            return True
    except FileNotFoundError:
        logger.debug('[DB] psql binary not found — skipping data_directory verification')
        return True
    except Exception as e:
        logger.debug('[DB] data_directory check failed on %s:%d: %s', host, port, e)
        return True


def _pg_has_database(host, port, dbname, pg_user):
    """Check if a PostgreSQL instance has a specific database."""
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


def _scan_for_our_pg(host, port_range, pgdata, pg_user):
    """Scan a range of ports for a PG instance that owns our pgdata."""
    for port in port_range:
        try:
            result = subprocess.run(
                [_find_pg_binary('pg_isready'), '-h', host, '-p', str(port)],
                capture_output=True, text=True, timeout=2
            )
            if result.returncode != 0:
                continue
            if _verify_pg_data_directory(host, port, pgdata, pg_user):
                logger.info('[DB] Found our PG on %s:%d (port scan recovery)', host, port)
                return port
        except Exception:
            continue
    return None


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


def _bootstrap_pg(pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname):
    """Bootstrap a brand-new PostgreSQL data directory and start the server.

    Returns:
        dict with updated PG_HOST, PG_PORT, PG_DSN on success, or None on failure.
    """
    logger.info('[DB] Bootstrapping new PostgreSQL data directory at %s ...', pgdata)

    os.makedirs(os.path.dirname(pgdata), exist_ok=True)
    os.makedirs(os.path.join(base_dir, 'logs'), exist_ok=True)

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
            f.write('\n# ── ChatUI auto-bootstrap overrides ──\n')
            f.write(f'port = {free_port}\n')
            f.write("listen_addresses = '*'\n")
            f.write("unix_socket_directories = ''\n")
        logger.info('[DB] Configured PG port=%d in postgresql.conf', free_port)
    except Exception as e:
        logger.error('[DB] Cannot write postgresql.conf: %s', e)
        return None

    # Start PG
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
        return None
    except Exception as e:
        logger.error('[DB] pg_ctl start failed: %s', e, exc_info=True)
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

    # Build DSN
    dsn = f"host=127.0.0.1 port={free_port} dbname={pg_dbname}"
    if pg_user:
        dsn += f" user={pg_user}"
    if pg_password:
        dsn += f" password={pg_password}"
    _write_owner_host(pgdata)
    logger.info('[DB] Bootstrap complete — DSN: host=127.0.0.1 port=%d dbname=%s',
                free_port, pg_dbname)
    return {'PG_HOST': '127.0.0.1', 'PG_PORT': free_port, 'PG_DSN': dsn}


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

    # ── Step 1: Explicit host/port override ──
    # When CHATUI_PG_HOST is set to a remote host, OR when CHATUI_PG_PORT is
    # explicitly set (even with localhost), skip local bootstrap and connect
    # directly.  This covers CI service containers, Docker Compose, managed PG,
    # or any external instance the user wants to use.
    explicit_host = os.environ.get('CHATUI_PG_HOST')
    explicit_port = os.environ.get('CHATUI_PG_PORT')
    is_explicit_external = (
        (explicit_host and explicit_host not in ('localhost', '127.0.0.1', '::1'))
        or explicit_port is not None  # any explicit port = user-managed PG
    )
    if is_explicit_external:
        target_host = explicit_host or pg_host
        target_port = int(explicit_port) if explicit_port else pg_port
        logger.info('[DB] Using explicit PG target from env: %s:%d', target_host, target_port)
        # Try psycopg2 directly (no pg_isready binary needed — works in CI)
        try:
            import psycopg2
            test_dsn = _build_dsn(target_host, target_port)
            conn = psycopg2.connect(test_dsn, connect_timeout=5)
            conn.close()
            logger.info('[DB] Explicit PG target %s:%d is reachable', target_host, target_port)
            return {'PG_HOST': target_host, 'PG_PORT': target_port,
                    'PG_DSN': test_dsn}
        except ImportError:
            logger.error('[DB] psycopg2 not installed — cannot connect to explicit PG')
            return None
        except Exception as e:
            logger.error('[DB] Explicit PG target %s:%d not reachable: %s',
                        target_host, target_port, e)
            return None

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
                [_find_pg_binary('pg_isready'), '-h', _local, '-p', str(pg_port)],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                is_ours = _verify_pg_data_directory(_local, pg_port, pgdata, pg_user)
                has_our_db = False
                if is_ours:
                    logger.info('[DB] PostgreSQL already running on %s:%d (verified ours)', _local, pg_port)
                    has_our_db = True
                else:
                    has_our_db = _pg_has_database(_local, pg_port, pg_dbname, pg_user)
                    if has_our_db:
                        logger.info('[DB] PG on %s:%d is another project\'s instance, '
                                   'but it has our database "%s" — reusing it', _local, pg_port, pg_dbname)
                    else:
                        logger.warning(
                            '[DB] PG on %s:%d is NOT ours and lacks database "%s" '
                            '— scanning nearby ports for our PG', _local, pg_port, pg_dbname)
                        found_our_port = _scan_for_our_pg(_local, range(15432, 15440), pgdata, pg_user)
                        if found_our_port:
                            _ensure_database_exists(_local, found_our_port, pg_dbname, pg_user, pgdata)
                            return {'PG_HOST': _local, 'PG_PORT': found_our_port,
                                    'PG_DSN': _build_dsn(_local, found_our_port)}
                if has_our_db:
                    _ensure_database_exists(_local, pg_port, pg_dbname, pg_user, pgdata)
                    return {'PG_HOST': _local, 'PG_PORT': pg_port,
                            'PG_DSN': _build_dsn(_local, pg_port)}
        except Exception as _e:
            logger.debug('[DB] pg_isready localhost:%d check failed: %s', pg_port, _e)

    # ── Step 2b: Scan nearby ports for our database ──
    _scan_range = range(15432, 15440)
    _local = '127.0.0.1'
    for _try_port in _scan_range:
        try:
            _chk = subprocess.run(
                [_find_pg_binary('pg_isready'), '-h', _local, '-p', str(_try_port)],
                capture_output=True, text=True, timeout=2
            )
            if _chk.returncode != 0:
                continue
            if _pg_has_database(_local, _try_port, pg_dbname, pg_user):
                logger.info('[DB] Found accessible PG with database "%s" on %s:%d '
                           '(port scan fallback)', pg_dbname, _local, _try_port)
                return {'PG_HOST': _local, 'PG_PORT': _try_port,
                        'PG_DSN': _build_dsn(_local, _try_port)}
        except Exception:
            continue

    # ── Step 3: Check if another machine owns the pgdata ──
    is_remote, remote_host = _pg_already_running_on_another_machine(pgdata, pg_port)
    if is_remote and remote_host:
        remote_ok = False
        try:
            _check = subprocess.run(
                [_find_pg_binary('pg_isready'), '-h', remote_host, '-p', str(pg_port)],
                capture_output=True, text=True, timeout=5
            )
            remote_ok = (_check.returncode == 0)
        except Exception as _e:
            logger.debug('[DB] pg_isready to remote %s:%d failed: %s', remote_host, pg_port, _e)
        if remote_ok:
            logger.info('[DB] PostgreSQL is running on remote machine %s — connecting as client', remote_host)
            _ensure_database_exists(remote_host, pg_port, pg_dbname, pg_user, pgdata)
            return {'PG_HOST': remote_host, 'PG_PORT': pg_port,
                    'PG_DSN': _build_dsn(remote_host, pg_port)}
        else:
            logger.warning('[DB] Remote PG owner %s is NOT reachable on port %d — '
                          'will try to start PG locally (Step 4)', remote_host, pg_port)

    # ── Step 4/5: Start PG locally or bootstrap ──
    if not os.path.isdir(pgdata):
        logger.info('[DB] No pgdata directory — bootstrapping new PostgreSQL instance')
        result = _bootstrap_pg(pgdata, base_dir, pg_host, pg_port, pg_user, pg_password, pg_dbname)
        if not result:
            logger.error('[DB] Bootstrap failed — refusing to connect to '
                         'default 127.0.0.1:%d (may be another project)', pg_port)
        return result

    # Clean up stale pidfile
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    if os.path.exists(pidfile):
        owner_host = _read_pg_host_from_pidfile(pgdata)
        local_ip = _get_local_ip()
        if owner_host and owner_host not in (local_ip, 'localhost', '127.0.0.1'):
            logger.warning('[DB] Step 4 safety net: postmaster.pid belongs to remote host %s '
                          '(we are %s) — refusing to delete. Connecting to remote host.',
                          owner_host, local_ip)
            _ensure_database_exists(owner_host, pg_port, pg_dbname, pg_user, pgdata)
            return {'PG_HOST': owner_host, 'PG_PORT': pg_port,
                    'PG_DSN': _build_dsn(owner_host, pg_port)}
        logger.warning('[DB] Removing stale postmaster.pid before starting PG '
                      '(owner: %s, us: %s)', owner_host, local_ip)
        try:
            os.remove(pidfile)
        except PermissionError as e:
            if IS_WINDOWS:
                logger.error('[DB] Cannot remove stale pidfile (file locked by another process '
                             '— PG may still be running): %s', e)
            else:
                logger.error('[DB] Cannot remove stale pidfile: %s', e)
            return None
        except Exception as e:
            logger.error('[DB] Cannot remove stale pidfile: %s', e)
            return None

    _fix_unix_socket_conf(pgdata)

    # Check if configured port is taken (possibly by our own orphaned PG)
    conf_port = _read_our_pg_port(pgdata) or pg_port
    try:
        check = subprocess.run(
            [_find_pg_binary('pg_isready'), '-h', '127.0.0.1', '-p', str(conf_port)],
            capture_output=True, text=True, timeout=3
        )
        if check.returncode == 0:
            # PG is already responding on our port — check if it's ours
            if _verify_pg_data_directory('127.0.0.1', conf_port, pgdata, pg_user):
                logger.info('[DB] PG already running on 127.0.0.1:%d (our data_directory) '
                           '— reusing after pidfile cleanup', conf_port)
                _ensure_database_exists('127.0.0.1', conf_port, pg_dbname, pg_user, pgdata)
                _write_owner_host(pgdata)
                return {'PG_HOST': '127.0.0.1', 'PG_PORT': conf_port,
                        'PG_DSN': _build_dsn('127.0.0.1', conf_port)}
            # Not ours — reassign to a different port
            free_port = _find_free_port(start=conf_port + 1)
            if free_port is None:
                logger.error('[DB] No free port found — cannot start PG')
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
                return None
    except Exception as _e:
        logger.debug('[DB] Port availability check failed: %s', _e)

    logger.info('[DB] Starting PostgreSQL server from %s ...', pgdata)
    try:
        log_path = os.path.join(base_dir, 'logs', 'postgresql.log')
        result = subprocess.run(
            [_find_pg_binary('pg_ctl'), '-D', pgdata, '-l', log_path, 'start'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            logger.info('[DB] PostgreSQL started successfully on this machine')
            time.sleep(1)
            _ensure_database_exists('127.0.0.1', pg_port, pg_dbname, pg_user, pgdata)
            _write_owner_host(pgdata)
            return {'PG_HOST': '127.0.0.1', 'PG_PORT': pg_port,
                    'PG_DSN': _build_dsn('127.0.0.1', pg_port)}
        else:
            logger.error('[DB] Failed to start PostgreSQL: %s', result.stderr)
            return None
    except Exception as e:
        logger.error('[DB] Failed to start PostgreSQL: %s', e, exc_info=True)
        return None


def _stop_pg(pgdata):
    """Stop PostgreSQL server on shutdown."""
    if os.path.isdir(pgdata):
        try:
            subprocess.run(
                [_find_pg_binary('pg_ctl'), '-D', pgdata, 'stop', '-m', 'fast'],
                capture_output=True, text=True, timeout=30
            )
            logger.info('[DB] PostgreSQL stopped')
        except Exception as e:
            logger.warning('[DB] Error stopping PostgreSQL: %s', e)

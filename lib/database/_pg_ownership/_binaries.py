"""Binary discovery, pidfile / remote-owner probes, port + conf helpers.

Holds no mutable module global (nothing to keep ``global``-local here) — the
grouping is by cohesion: PostgreSQL binary discovery, the pidfile liveness
ground-truth, and the multi-host remote-owner decision
``_pg_already_running_on_another_machine`` that stitches the self-heal +
identity + heartbeat pieces together.

The two core call-outs (``_audit``, ``_pg_real_connect_ok``) are resolved
LAZILY (in-body import) to avoid an import cycle with ``_bootstrap``. They are
NOT re-exported by the facade (they belong to core; re-exporting would shadow
the real ones).

Patch-safety: every cross-submodule / test-patchable helper
(``_get_local_ip``, ``_get_host_identity``, ``_owner_is_self``,
``_read_pg_host_from_pidfile``, ``_pidfile_pid_is_live_local_postgres``,
``_pg_real_connect_ok``, ``_heal_if_copied``, ``_heal_if_standalone_remote_owner``)
is resolved through the ``lib.database._pg_ownership`` facade at call time.
"""

import getpass
import os
import shutil

from lib.compat import IS_LINUX, IS_MACOS, IS_WINDOWS
from lib.log import get_logger

logger = get_logger(__name__)


def _audit(*a, **k):
    from lib.database._bootstrap import _audit as _core_audit
    return _core_audit(*a, **k)


def _pg_real_connect_ok(*a, **k):
    from lib.database._bootstrap import _pg_real_connect_ok as _f
    return _f(*a, **k)


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
    except Exception as e:
        logger.debug('[DB] getuser() failed, using fallback %s: %s', fallback, e)
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


def _pidfile_pid_is_live_local_postgres(pgdata):
    """Return True if postmaster.pid names a PID that is a live local postgres.

    This is the IP-independent ground truth for "is OUR machine already
    running PG on this pgdata". The `.pg_owner_host` marker is derived from
    `_get_local_ip()`, which flaps when the container's IP is reassigned
    (cloud-IDE network changes) — making a host mistake its OWN postmaster
    for a remote one. A PID liveness + name check does not depend on the IP,
    so we use it as a hard guard before deleting the pidfile or starting a
    second postmaster (concurrent access to one pgdata corrupts pg_subtrans).

    Returns False if the pidfile is absent/unparseable, the PID is dead, or
    the PID belongs to a non-postgres process (genuinely stale pidfile).
    """
    pidfile = os.path.join(pgdata, 'postmaster.pid')
    try:
        with open(pidfile) as f:
            pid = int(f.readline().strip())
    except (FileNotFoundError, ValueError) as e:
        logger.debug('[DB] pidfile liveness: cannot read PID from %s: %s', pidfile, e)
        return False
    except OSError as e:
        logger.debug('[DB] pidfile liveness: stat/read error on %s: %s', pidfile, e)
        return False
    try:
        from lib.compat import is_process_alive, is_process_named
        if not is_process_alive(pid):
            return False
        try:
            named = is_process_named(pid, 'postgres')
        except Exception as e:
            # Can't introspect the name (no /proc perms etc.) — be SAFE and
            # assume it IS our live postgres rather than risk a double-start.
            logger.warning('[DB] pidfile liveness: PID %d alive but name check '
                           'failed (%s) — assuming live postgres to avoid double-start', pid, e)
            return True
        if named:
            logger.info('[DB] pidfile liveness: PID %d is a LIVE local postgres '
                        '— this host already owns pgdata=%s', pid, pgdata)
            return True
        logger.info('[DB] pidfile liveness: PID %d alive but not postgres — stale pidfile', pid)
        return False
    except Exception as e:
        logger.warning('[DB] pidfile liveness check failed (%s) — assuming live '
                       'postgres to avoid double-start', e)
        return True


def _pg_already_running_on_another_machine(pgdata, pg_port):
    """Check if another machine owns the PG data directory.

    Returns:
        (True, host_ip) if another machine has PG running on this pgdata,
        (False, None) otherwise.
    """
    import lib.database._pg_ownership as _pkg
    # Copy/move self-heal: if this pgdata was copied here from another path,
    # every inherited marker (owner_host, heartbeat, pidfile) belongs to the
    # ORIGINAL instance. Never defer to it — that is the "silently connect to
    # the source machine's PG" trap. Clear the markers and report no remote.
    if _pkg._heal_if_copied(pgdata):
        return False, None

    # Standalone single-machine copy: an inherited remote-owner marker (same
    # FUSE abs-path, different container/host) must not make us defer. Clear it
    # and own PG locally. No-op unless TOFU_PG_STANDALONE is set.
    if _pkg._heal_if_standalone_remote_owner(pgdata):
        return False, None

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

    # IP-independent identity check FIRST: the stable .pg_owner_id marker
    # (machine-id / hostname) does not flap when the container IP is
    # reassigned, unlike .pg_owner_host. If it says this pgdata is ours, we
    # own it — no matter what IP the (possibly stale) .pg_owner_host records.
    owner_self = _pkg._owner_is_self(pgdata)
    if owner_self is True:
        logger.info('[DB] .pg_owner_id matches this host (id=%s) — pgdata is OURS '
                    '(ignoring any IP flap in .pg_owner_host)', _pkg._get_host_identity())
        return False, None

    owner_host = _pkg._read_pg_host_from_pidfile(pgdata)
    local_ip = _pkg._get_local_ip()
    is_remote_owner = (
        owner_host is not None
        and owner_host not in (local_ip, 'localhost', '127.0.0.1')
    )
    # A DIFFERENT-host identity marker is authoritative proof of remoteness
    # even if the flapping IPs happen to coincide.
    if owner_self is False:
        is_remote_owner = True

    logger.info('[DB] postmaster.pid: PID=%d, owner_host=%s, local_ip=%s, owner_self=%s, is_remote=%s',
                pid, owner_host, local_ip, owner_self, is_remote_owner)

    # IP-independent ground truth: if the pidfile PID is a live local
    # postgres, THIS host already owns pgdata — regardless of what the
    # `.pg_owner_host` IP marker says. _get_local_ip() flaps when the
    # container IP is reassigned, which previously made a host mistake its
    # OWN postmaster for a remote one, delete the pidfile, and start a
    # SECOND postmaster on the same pgdata → pg_subtrans corruption. Trust
    # the PID over the IP.
    if is_remote_owner and _pkg._pidfile_pid_is_live_local_postgres(pgdata):
        logger.warning('[DB] postmaster.pid PID=%d is a LIVE local postgres but '
                       'owner_host=%s != local_ip=%s — IP flap detected. Treating '
                       'as OURS (not remote) to avoid a double-start.',
                       pid, owner_host, local_ip)
        return False, None

    if is_remote_owner:
        # Use a real psycopg2 connect probe — pg_isready can give false
        # positives on "half-alive" containers (TCP accept works but real
        # queries hang) which is exactly the container-switch scenario on
        # shared FUSE storage.
        reachable = _pkg._pg_real_connect_ok(owner_host, pg_port, None, None, timeout_s=5)
        logger.info('[DB] PG owned by remote host %s (real_connect=%s) — deferring to it', owner_host, reachable)
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
        except Exception as e:
            logger.debug('[DB] Port %d probe error (assuming free): %s', port, e)
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

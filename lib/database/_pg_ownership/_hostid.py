"""Host identity / local-IP / owner-host markers.

Owns the ``_HOST_IDENTITY_CACHE`` module global (rebound by
``_get_host_identity``) and ``_OWNER_ID_FILE``. ``_get_local_ip`` lives here
too: it is the flap-prone IP probe that ``_get_host_identity`` deliberately
does NOT depend on, plus the payload source for heartbeat / owner-host writes.

The identity cache is set on the ``lib.database._pg_ownership`` facade (its
canonical home) so a test that resets ``pg_ownership._HOST_IDENTITY_CACHE``
between cases is honoured. Sibling submodules resolve ``_get_local_ip`` /
``_get_host_identity`` / ``_owner_is_self`` through the facade too.
"""

import os

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)


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


_HOST_IDENTITY_CACHE = None
_OWNER_ID_FILE = '.pg_owner_id'


def _get_host_identity():
    """Return a STABLE per-host identity that does NOT flap when the container
    IP is reassigned (unlike ``_get_local_ip()``).

    ``_get_local_ip()`` uses a UDP-trick to 8.8.8.8 and returns whatever the
    container's current IP is. Cloud-IDE / FUSE deployments reassign that IP
    while the host stays the same, which made a server mistake its OWN pgdata
    for a remote one ("connection ... timeout expired" / split-brain). The
    machine-id (or hostname) stays constant across an IP flap but DIFFERS
    between genuinely different containers/hosts — exactly the semantics we
    want for "is this pgdata owned by THIS machine?".

    Order: ``TOFU_HOST_ID`` env override → ``/etc/machine-id`` →
    ``/var/lib/dbus/machine-id`` → ``socket.gethostname()``. Cached per process
    (on the facade module).
    """
    import lib.database._pg_ownership as _pkg
    if _pkg._HOST_IDENTITY_CACHE:
        return _pkg._HOST_IDENTITY_CACHE
    ident = (getenv_compat('TOFU_HOST_ID', default='') or '').strip()
    if not ident:
        for p in ('/etc/machine-id', '/var/lib/dbus/machine-id'):
            try:
                with open(p) as f:
                    ident = f.read().strip()
                if ident:
                    break
            except OSError as e:
                logger.debug('[DB] host-identity: could not read %s: %s', p, e)
    if not ident:
        try:
            import socket
            ident = socket.gethostname().strip()
        except Exception as e:
            logger.debug('[DB] host-identity: gethostname failed: %s', e)
            ident = ''
    _pkg._HOST_IDENTITY_CACHE = ident or 'unknown-host'
    return _pkg._HOST_IDENTITY_CACHE


def _owner_is_self(pgdata):
    """IP-independent ownership check using the stable ``.pg_owner_id`` marker.

    Returns:
        True  — the stored host-identity equals ours (we own this pgdata,
                regardless of any IP flap recorded in ``.pg_owner_host``).
        False — the stored identity is a DIFFERENT host.
        None  — no ``.pg_owner_id`` marker (legacy pgdata or never written);
                caller must fall back to the IP / live-PID heuristics.
    """
    import lib.database._pg_ownership as _pkg
    id_file = os.path.join(pgdata, _OWNER_ID_FILE)
    try:
        if os.path.exists(id_file):
            with open(id_file) as f:
                stored = f.read().strip()
            if stored:
                return stored == _pkg._get_host_identity()
    except OSError as e:
        logger.debug('[DB] Could not read %s: %s', _OWNER_ID_FILE, e)
    return None


def _write_owner_host(pgdata):
    """Write our IP to .pg_owner_host so other machines know where to connect,
    plus a stable host-identity to .pg_owner_id for IP-flap-proof self-check."""
    import lib.database._pg_ownership as _pkg
    owner_file = os.path.join(pgdata, '.pg_owner_host')
    try:
        ip = _pkg._get_local_ip()
        with open(owner_file, 'w') as f:
            f.write(ip)
        logger.info('[DB] Wrote PG owner host: %s (id=%s)', ip, _pkg._get_host_identity())
    except Exception as e:
        logger.warning('[DB] Could not write .pg_owner_host: %s', e)
    id_file = os.path.join(pgdata, _OWNER_ID_FILE)
    try:
        with open(id_file, 'w') as f:
            f.write(_pkg._get_host_identity())
    except Exception as e:
        logger.warning('[DB] Could not write %s: %s', _OWNER_ID_FILE, e)

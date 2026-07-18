"""Process-ownership flag + standalone-mode self-heal.

Owns the ``_PG_STARTED_BY_US`` module global (rebound by
``_mark_pg_owned_locally``, read by ``is_pg_owned_locally``). Both accessors of
that flag live HERE, and the canonical copy is kept on the
``lib.database._pg_ownership`` facade so ``pg_ownership._PG_STARTED_BY_US`` and
the two accessors always agree.

``_standalone_mode`` / ``_heal_if_standalone_remote_owner`` complement the
copy-detector in ``_identity``: on shared FUSE storage every container sees
pgdata at the SAME absolute path, so copy-detect can't fire — yet the
``.pg_owner_host`` may still point at a stale peer. The explicit
``TOFU_PG_STANDALONE`` flag resolves that ambiguity in favour of "own it
locally".

Patch-safety: cross-submodule helpers (``_owner_is_self``,
``_read_pg_host_from_pidfile``, ``_get_local_ip``,
``_pidfile_pid_is_live_local_postgres``, ``_clear_ownership_markers``,
``_clear_heartbeat``, ``_write_instance_stamp``, ``_start_heartbeat_thread``,
``_canonical_pgdata_path``) are resolved through the
``lib.database._pg_ownership`` facade at call time so tests that patch these
names on the package take effect.
"""

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)


_PG_STARTED_BY_US = False


def _standalone_mode():
    """True when this deployment is a standalone single-machine copy.

    Set ``TOFU_PG_STANDALONE=1`` (``export.py`` seeds it into every exported
    ``.env``). In this mode we NEVER defer to a remote PG owner recorded in an
    inherited pgdata: such an ``.pg_owner_host`` / heartbeat comes from the
    machine the copy was made on — or a previous container sharing the same
    FUSE-mounted absolute path — NOT a live failover peer. We clear the
    inherited markers and own PG locally instead of routing every DB call
    across a dead cross-host link (the "connection ... timeout expired" crash).

    This deliberately disables same-path multi-host failover, which standalone
    deployments don't use. Same-path failover deployments must leave
    ``TOFU_PG_STANDALONE`` unset to keep the heartbeat handoff in
    ``_pg_already_running_on_another_machine`` Step 3.
    """
    return getenv_compat('TOFU_PG_STANDALONE', default='').strip().lower() in (
        '1', 'true', 'yes', 'on')


def _heal_if_standalone_remote_owner(pgdata):
    """In standalone mode, drop an inherited REMOTE-owner marker so we never
    defer to another machine's PG.

    Complements ``_heal_if_copied``: that one heals when the pgdata was copied
    to a DIFFERENT absolute path. On shared FUSE storage every container sees
    the pgdata at the SAME absolute path, so the stamp matches and copy-detect
    can't fire — yet the ``.pg_owner_host`` still points at a stale peer. The
    explicit ``TOFU_PG_STANDALONE`` flag resolves that ambiguity in favour of
    "own it locally".

    Returns True when an inherited remote marker was cleared (caller should
    treat the directory as freshly owned locally), False otherwise.
    """
    import lib.database._pg_ownership as _pkg
    if not _standalone_mode():
        return False
    # Stable-identity guard: if .pg_owner_id says this pgdata is ours, an IP
    # flap (owner_host != local_ip) is NOT an inherited remote marker — it's
    # our own pgdata under a new container IP. Never clear in that case.
    if _pkg._owner_is_self(pgdata) is True:
        return False
    owner_host = _pkg._read_pg_host_from_pidfile(pgdata)
    if not owner_host:
        return False
    local_ip = _pkg._get_local_ip()
    if owner_host in (local_ip, 'localhost', '127.0.0.1'):
        return False  # owner is this host — nothing inherited to heal
    # Owner is remote. IP-independent safety: if our pidfile PID is a LIVE
    # local postgres, THIS host already owns pgdata (the .pg_owner_host IP can
    # be stale after a container-IP flap) — never clobber our own postmaster.
    if _pkg._pidfile_pid_is_live_local_postgres(pgdata):
        return False
    logger.warning('[DB] TOFU_PG_STANDALONE set and pgdata carries a REMOTE '
                   'owner marker (owner_host=%s, local_ip=%s) — inherited from '
                   'another machine/container, not a failover peer. Clearing it '
                   'and owning PG locally.', owner_host, local_ip)
    try:
        from lib.log import audit_log as _audit
        _audit('pg_standalone_heal_remote_owner',
               owner_host=owner_host, local_ip=local_ip,
               pgdata=_pkg._canonical_pgdata_path(pgdata))
    except Exception as e:
        logger.debug('[DB] audit_log for standalone heal failed: %s', e)
    _pkg._clear_ownership_markers(pgdata, remove_pidfile=False,
                                  reason='standalone remote-owner marker')
    _pkg._clear_heartbeat(pgdata)
    return True


def _mark_pg_owned_locally(pgdata=None):
    """Record that this process is responsible for the local PG.

    When ``pgdata`` is provided, also starts the heartbeat refresher so
    other hosts (sharing the same FUSE-mounted pgdata) can tell that a
    tofu process is actively using this PG.
    """
    import lib.database._pg_ownership as _pkg
    _pkg._PG_STARTED_BY_US = True
    if pgdata:
        _pkg._write_instance_stamp(pgdata)
        _pkg._start_heartbeat_thread(pgdata)


def is_pg_owned_locally():
    """Return True if this process started / took over a local PG server."""
    import lib.database._pg_ownership as _pkg
    return _pkg._PG_STARTED_BY_US

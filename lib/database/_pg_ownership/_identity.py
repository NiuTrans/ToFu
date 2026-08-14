"""Instance-identity stamp — the copy/move detector.

Owns the ``_INSTANCE_ID_FILE`` name. No mutable module global lives here (the
stamp lives on disk), so there is no ``global`` rebind to keep local; the
grouping is by cohesion.

Ownership markers (``.pg_owner_host``, ``.tofu_heartbeat``, ``postmaster.pid``)
live INSIDE pgdata, which is exactly the directory people copy. When the whole
project is copied to a NEW path the markers come along and the fresh instance
trusts them — silently routing every DB call back to the ORIGINAL machine's PG
via FUSE cross-machine discovery. The discriminator is the pgdata's own
ABSOLUTE PATH: legitimate cross-host sharing happens at the SAME mount path; a
copy lands at a DIFFERENT path. We stamp the canonical path into
``.pg_instance_id`` whenever we take local ownership; a later mismatch means
the directory was copied → ignore inherited markers and take over locally.

Patch-safety: cross-submodule constants / helpers (``_OWNER_ID_FILE``,
``_HEARTBEAT_FILE``, ``_clear_heartbeat``) are resolved through the
``lib.database._pg_ownership`` facade.
"""

import os
import time

from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)


_INSTANCE_ID_FILE = '.pg_instance_id'


def _instance_id_path(pgdata):
    return os.path.join(pgdata, _INSTANCE_ID_FILE)


def _canonical_pgdata_path(pgdata):
    """Return a stable canonical key for a pgdata location.

    Uses ``os.path.realpath`` (resolves symlinks + ``..``) so the same
    physical directory always produces the same string regardless of how
    it was addressed. Returns the input unchanged on error.
    """
    try:
        return os.path.realpath(pgdata)
    except OSError as e:
        logger.debug('[DB] realpath(%s) failed: %s', pgdata, e)
        return pgdata


def _read_instance_stamp(pgdata):
    """Return the parsed `.pg_instance_id` dict, or None if absent/invalid."""
    path = _instance_id_path(pgdata)
    try:
        data = read_json(path, strict=True)
        if isinstance(data, dict) and data.get('path'):
            return data
        logger.debug('[DB] instance stamp at %s malformed: %r', path, data)
        return None
    except (OSError, RuntimeError) as e:
        logger.debug('[DB] Could not read instance stamp at %s: %s', path, e)
        return None


def _write_instance_stamp(pgdata):
    """Stamp this pgdata with its current canonical path + a fresh id.

    Idempotent for the path: if a stamp already exists for the SAME
    canonical path, the existing id/created are preserved (we only rewrite
    when the path differs or no stamp exists). Best-effort — failures are
    logged at debug and never abort startup.
    """
    import uuid
    canon = _canonical_pgdata_path(pgdata)
    existing = _read_instance_stamp(pgdata)
    if existing and _canonical_pgdata_path(existing.get('path', '')) == canon:
        return  # already stamped for this path — keep stable id
    payload = {
        'path': canon,
        'id': (existing or {}).get('id') or uuid.uuid4().hex,
        'created': (existing or {}).get('created') or time.time(),
        'restamped': time.time() if existing else None,
    }
    path = _instance_id_path(pgdata)
    try:
        write_json_atomic(path, payload, indent=None)
        logger.info('[DB] Stamped pgdata instance identity: path=%s id=%s',
                    canon, payload['id'])
    except OSError as e:
        logger.debug('[DB] Could not write instance stamp to %s: %s', path, e)


def _pgdata_was_copied(pgdata):
    """Return (was_copied, stamped_path) for this pgdata.

    ``was_copied`` is True only when a stamp EXISTS and its recorded
    canonical path differs from the current canonical path — i.e. the
    directory was copied or moved here from elsewhere. A missing stamp
    (legacy pgdata predating this mechanism, or a brand-new initdb) returns
    False so existing same-path multi-host behaviour is untouched; the stamp
    is written lazily the next time we take local ownership.
    """
    stamp = _read_instance_stamp(pgdata)
    if not stamp:
        return False, None
    stamped = _canonical_pgdata_path(stamp.get('path', ''))
    current = _canonical_pgdata_path(pgdata)
    if stamped and stamped != current:
        return True, stamped
    return False, stamped


def _clear_ownership_markers(pgdata, *, remove_pidfile=True, reason=''):
    """Remove the machine-specific ownership markers from a pgdata.

    Clears `.pg_owner_host`, the tofu heartbeat, and (optionally)
    `postmaster.pid` / `postmaster.opts`. The DATA files are never touched.
    Used by the copy/move self-heal and by the `reset-ownership` admin
    command. Best-effort; each failure is logged at warning level.
    """
    import lib.database._pg_ownership as _pkg
    suffix = f' ({reason})' if reason else ''
    removed = []
    targets = ['.pg_owner_host', _pkg._OWNER_ID_FILE, _pkg._HEARTBEAT_FILE]
    if remove_pidfile:
        targets += ['postmaster.pid', 'postmaster.opts']
    for name in targets:
        p = os.path.join(pgdata, name)
        try:
            if os.path.exists(p):
                os.remove(p)
                removed.append(name)
        except OSError as e:
            logger.warning('[DB] Could not remove ownership marker %s%s: %s',
                           name, suffix, e)
    if removed:
        logger.info('[DB] Cleared ownership markers%s: %s', suffix, ', '.join(removed))
    return removed


def _heal_if_copied(pgdata):
    """If this pgdata was copied/moved here, drop inherited ownership markers.

    Returns True when a copy was detected and markers were cleared (the
    caller should treat the directory as freshly-owned-locally), False
    otherwise. The instance stamp is re-written for the new path by the
    subsequent ``_mark_pg_owned_locally`` call.
    """
    import lib.database._pg_ownership as _pkg
    was_copied, stamped = _pgdata_was_copied(pgdata)
    if not was_copied:
        return False
    logger.warning('[DB] pgdata was COPIED/MOVED here (stamped path=%s, '
                   'current=%s) — ignoring inherited ownership markers and '
                   'taking over locally. This prevents silently connecting to '
                   "the original machine's PostgreSQL.",
                   stamped, _canonical_pgdata_path(pgdata))
    try:
        from lib.log import audit_log as _audit
        _audit('pg_copied_pgdata_self_heal',
               stamped_path=stamped,
               current_path=_canonical_pgdata_path(pgdata))
    except Exception as e:
        logger.debug('[DB] audit_log for copy self-heal failed: %s', e)
    _pkg._clear_ownership_markers(pgdata, remove_pidfile=False, reason='copied pgdata')
    _pkg._clear_heartbeat(pgdata)
    return True

"""Cross-host ownership for auxiliary SQLite stores.

The canonical ``tofu.db`` authority is guarded by :mod:`sqlite_owner`.  A
feature-owned SQLite file needs the same fail-closed protection, but one
process may legitimately own several such stores.  This module therefore
keeps an ownership heartbeat per resolved database path instead of creating a
second, ad-hoc locking convention in application code.
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
import threading
import time

from lib.log import get_logger
from lib.database import sqlite_owner as _owner


logger = get_logger(__name__)


_state_lock = threading.RLock()
_states: dict[str, dict] = {}


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def _paths(db_path: str) -> tuple[Path, Path]:
    path = Path(db_path).resolve()
    stem = f'.{path.name}.tofu-owner'
    return path.parent / stem, path.parent / f'{stem}.lock'


def _assert_write_role(db_path: str, purpose: str) -> None:
    # Test databases are isolated and short-lived.  An explicit guard setting
    # still lets owner tests exercise the production role boundary.
    if ('PYTEST_CURRENT_TEST' in os.environ
            and os.environ.get('TOFU_SQLITE_OWNER_GUARD') is None):
        return
    if _owner.write_role_authorized():
        return
    raise _owner.SQLiteOwnershipError(
        f'auxiliary SQLite write for {purpose!r} is restricted to the server '
        f'or explicit maintenance authority: {Path(db_path).resolve()}')


def _record(db_path: str, existing: dict | None) -> dict:
    now = time.time()
    members = {}
    if existing and existing.get('host') == _owner._host_id():
        old_members = existing.get('members')
        if isinstance(old_members, dict):
            members = {
                str(key): value for key, value in old_members.items()
                if isinstance(value, dict)
                and now - float(value.get('ts', 0)) <= _owner.TTL_S
            }
    members[_owner._instance_id] = {'pid': os.getpid(), 'ts': now}
    return {
        'version': 1,
        'host': _owner._host_id(),
        'updated_at': now,
        'db': str(Path(db_path).resolve()),
        'members': members,
    }


def _claim_or_refresh(db_path: str) -> dict:
    marker, lock_dir = _paths(db_path)
    with _owner._marker_lock(lock_dir):
        existing, mtime, error = _owner._read(marker)
        if error is not None and _owner._fresh(mtime):
            raise _owner.SQLiteOwnershipError(
                'fresh auxiliary SQLite owner marker is unreadable; '
                f'refusing write mode: {error}')
        if (existing and _owner._fresh(mtime)
                and existing.get('host') not in (None, '', _owner._host_id())):
            raise _owner.SQLiteOwnershipError(
                'auxiliary SQLite authority is owned by '
                f'host={existing.get("host")!r}; marker={marker}')
        record = _record(db_path, existing)
        _owner._atomic_write(marker, record)
        return record


def _heartbeat(db_path: str, stop: threading.Event) -> None:
    while not stop.wait(_owner.REFRESH_S):
        try:
            record = _claim_or_refresh(db_path)
            with _state_lock:
                state = _states.get(db_path)
                if state is None:
                    return
                state['claim'] = record
                state['last_verified'] = time.time()
        except Exception as exc:
            with _state_lock:
                state = _states.get(db_path)
                if state is not None:
                    state['lost'] = str(exc)
            logger.critical(
                '[DB] Auxiliary SQLite ownership LOST for %s: %s',
                db_path, exc)
            return


def assert_store_owner(db_path: str | os.PathLike, *, purpose: str) -> None:
    """Authorize and revalidate one auxiliary-store write acquisition."""
    purpose = str(purpose or '').strip()
    if not purpose:
        raise ValueError('auxiliary SQLite write purpose must not be empty')
    resolved = str(Path(db_path).resolve())
    _assert_write_role(resolved, purpose)
    if not _truthy(os.environ.get('TOFU_SQLITE_OWNER_GUARD'), True):
        return

    with _state_lock:
        state = _states.get(resolved)
        if state and state.get('lost'):
            raise _owner.SQLiteOwnershipError(
                f'auxiliary SQLite ownership lost: {state["lost"]}')
        if state is None:
            stop = threading.Event()
            state = {
                'claim': None,
                'lost': '',
                'last_verified': 0.0,
                'stop': stop,
                'thread': None,
            }
            _states[resolved] = state
        # Claim while holding the registry lock. This path runs once per store
        # and prevents two first-use threads from starting duplicate heartbeat
        # threads for the same authority.
        if state['claim'] is None:
            record = _claim_or_refresh(resolved)
            state['claim'] = record
            state['last_verified'] = time.time()
            thread = threading.Thread(
                target=_heartbeat, args=(resolved, state['stop']), daemon=True,
                name=f'sqlite-store-owner-{Path(resolved).stem[:24]}')
            state['thread'] = thread
            thread.start()
            logger.info(
                '[DB] Auxiliary SQLite ownership claimed: '
                'host=%s pid=%d marker=%s',
                _owner._host_id(), os.getpid(), _paths(resolved)[0])
            return

    if time.time() - float(state['last_verified']) < 5.0:
        return
    marker, _lock_dir = _paths(resolved)
    existing, mtime, error = _owner._read(marker)
    if (error is not None or not existing or not _owner._fresh(mtime)
            or existing.get('host') != _owner._host_id()
            or existing.get('db') != resolved):
        try:
            record = _claim_or_refresh(resolved)
        except Exception as exc:
            with _state_lock:
                state['lost'] = str(exc)
            raise _owner.SQLiteOwnershipError(
                f'auxiliary SQLite ownership validation failed: {exc}') from exc
        with _state_lock:
            state['claim'] = record
    with _state_lock:
        state['last_verified'] = time.time()


def release_store_owners() -> None:
    """Release every auxiliary claim held by this process."""
    with _state_lock:
        states = list(_states.items())
        _states.clear()
    for _path, state in states:
        state['stop'].set()
    for _path, state in states:
        thread = state.get('thread')
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
    for db_path, _state in states:
        marker, lock_dir = _paths(db_path)
        try:
            with _owner._marker_lock(lock_dir, timeout_s=1.0):
                existing, _mtime, _error = _owner._read(marker)
                if not existing or existing.get('host') != _owner._host_id():
                    continue
                members = existing.get('members')
                if not isinstance(members, dict):
                    members = {}
                members.pop(_owner._instance_id, None)
                now = time.time()
                members = {
                    key: value for key, value in members.items()
                    if isinstance(value, dict)
                    and now - float(value.get('ts', 0)) <= _owner.TTL_S
                }
                if members:
                    existing['members'] = members
                    existing['updated_at'] = max(
                        float(value.get('ts', 0)) for value in members.values())
                    _owner._atomic_write(marker, existing)
                else:
                    marker.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning(
                '[DB] Could not release auxiliary SQLite owner %s: %s',
                db_path, exc)


atexit.register(release_store_owners)


__all__ = ['assert_store_owner', 'release_store_owners']

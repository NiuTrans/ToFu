"""Process-wide guard against raw opens of authoritative SQLite files.

Static checks cover first-party code and wrapper SQL guards cover callers that
use :mod:`lib.database`. Dynamically loaded Python plugins could still call
``sqlite3.connect(DB_PATH)`` and create an uncoordinated writer/read snapshot.
The server installs this narrow interposer before plugin discovery. The
canonical database and every auxiliary store registered by the data layer are
protected, while unrelated plugin-private SQLite files remain fully usable.
Data-layer connection factories enter the explicit capability scope below.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import threading
from urllib.parse import unquote

from lib.log import get_logger


logger = get_logger(__name__)

class SQLiteDriverBoundaryError(RuntimeError):
    """A caller attempted to open canonical SQLite outside the data layer."""


_lock = threading.RLock()
_local = threading.local()
_installed = False
_protected_paths: set[str] = set()
_original_connect = sqlite3.connect


def _database_path(database, *, uri: bool) -> str | None:
    try:
        value = os.fspath(database)
    except TypeError as exc:
        logger.debug('[DB] SQLite database argument is not path-like: %s', exc)
        return None
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    value = str(value)
    if value == ':memory:' or value.startswith('file::memory:'):
        return None
    if uri or value.startswith('file:'):
        if not value.startswith('file:'):
            return None
        value = unquote(value[5:].split('?', 1)[0])
        if not value or value == ':memory:':
            return None
    try:
        return str(Path(value).resolve())
    except OSError as exc:
        logger.debug('[DB] SQLite path resolution failed; using absolute path: %s',
                     exc)
        return os.path.abspath(value)


def _guarded_connect(database, *args, **kwargs):
    path = _database_path(database, uri=bool(kwargs.get('uri', False)))
    with _lock:
        protected = path in _protected_paths if path else False
    if (protected
            and not bool(getattr(_local, 'allow_depth', 0))):
        raise SQLiteDriverBoundaryError(
            'raw sqlite3.connect() to a registered Tofu database is denied; '
            'use the lib.database data layer pool/repository or reviewed '
            'tooling facade')
    return _original_connect(database, *args, **kwargs)


def install_sqlite_driver_guard(canonical_path: str | os.PathLike) -> bool:
    """Protect one exact authority path; return whether installation was new."""
    global _installed
    resolved = str(Path(canonical_path).resolve())
    with _lock:
        _protected_paths.add(resolved)
        if _installed:
            return False
        sqlite3.connect = _guarded_connect
        # ``sqlite3.dbapi2`` is normally the same module, but assign explicitly
        # for interpreters that expose it as a distinct module object.
        sqlite3.dbapi2.connect = _guarded_connect
        _installed = True
    return True


def register_sqlite_driver_authority(path: str | os.PathLike) -> str:
    """Register an auxiliary data-layer-owned path without opening it."""
    resolved = str(Path(path).resolve())
    with _lock:
        _protected_paths.add(resolved)
    return resolved


@contextmanager
def allow_sqlite_driver_connection(purpose: str):
    """Grant the current thread one reviewed data-layer open capability."""
    purpose = str(purpose or '').strip()
    if not purpose:
        raise ValueError('SQLite driver connection purpose must not be empty')
    depth = int(getattr(_local, 'allow_depth', 0) or 0)
    _local.allow_depth = depth + 1
    try:
        yield
    finally:
        _local.allow_depth = depth


def uninstall_sqlite_driver_guard_for_tests() -> None:
    """Restore stdlib state; test-only because production is fail-closed."""
    global _installed
    with _lock:
        sqlite3.connect = _original_connect
        sqlite3.dbapi2.connect = _original_connect
        _protected_paths.clear()
        _installed = False


__all__ = [
    'SQLiteDriverBoundaryError', 'allow_sqlite_driver_connection',
    'install_sqlite_driver_guard', 'register_sqlite_driver_authority',
    'uninstall_sqlite_driver_guard_for_tests',
]

"""Side-effect-free SQLite connection/transaction lane for offline tools.

This leaf module can be loaded directly by file path, so a SQLite-only
maintenance command does not trigger application backend discovery or start a
PostgreSQL cluster.  It still centralizes driver construction, canonical-write
authorization, cross-host ownership checks, ``BEGIN IMMEDIATE``, rollback and
bounded busy retries under ``lib/database``.
"""

from __future__ import annotations

from contextlib import nullcontext
import importlib.util
from pathlib import Path
import sqlite3
import sys
import time
from typing import Callable, TypeVar

from lib.log import get_logger


logger = get_logger(__name__)
from urllib.parse import quote


_T = TypeVar('_T')
_OWNER_LEAF_NAME = '_tofu_sqlite_owner_tooling_leaf'


def _load_owner_module():
    # Reuse the package instance when the application already imported it;
    # otherwise load only the leaf to keep an offline/help command side-effect
    # free with respect to backend discovery.
    existing = sys.modules.get('lib.database.sqlite_owner')
    if existing is not None:
        return existing
    existing = sys.modules.get(_OWNER_LEAF_NAME)
    if existing is not None:
        return existing
    path = Path(__file__).with_name('sqlite_owner.py')
    spec = importlib.util.spec_from_file_location(_OWNER_LEAF_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - install damage
        raise RuntimeError(f'cannot load SQLite ownership module: {path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[_OWNER_LEAF_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_OWNER_LEAF_NAME, None)
        raise
    return module


_owner = _load_owner_module()


def open_sqlite_tool_connection(path: str | Path, *, writable: bool):
    """Open a bounded raw handle owned by the data-layer tooling facade."""
    resolved = Path(path).resolve()
    mode = 'rw' if writable else 'ro'
    uri = f'file:{quote(str(resolved))}?mode={mode}'
    driver_guard = sys.modules.get('lib.database.sqlite_driver_guard')
    capability = (driver_guard.allow_sqlite_driver_connection(
        'reviewed SQLite maintenance tool connection')
        if driver_guard is not None else nullcontext())
    with capability:
        conn = sqlite3.connect(
            uri, uri=True, timeout=30, isolation_level=None,
            check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    if not writable:
        conn.execute('PRAGMA query_only=ON')
    return conn


def open_sqlite_candidate_connection(
    path: str | Path,
    *,
    canonical_path: str | Path,
    writable: bool,
):
    """Open an explicitly non-canonical migration candidate.

    Offline snapshot builders need SQLite's native bulk-load controls, but
    must never accidentally attach those relaxed durability settings to the
    live authority.  Requiring the canonical path at every call keeps that
    safety decision in the data layer and makes a path mix-up fail closed.
    """
    resolved = Path(path).resolve()
    canonical = Path(canonical_path).resolve()
    if resolved == canonical:
        raise RuntimeError(
            'refusing candidate connection to canonical SQLite authority: '
            f'{resolved}')
    mode = 'rw' if writable else 'ro'
    uri = f'file:{quote(str(resolved))}?mode={mode}'
    driver_guard = sys.modules.get('lib.database.sqlite_driver_guard')
    capability = (driver_guard.allow_sqlite_driver_connection(
        'reviewed non-canonical SQLite migration candidate')
        if driver_guard is not None else nullcontext())
    with capability:
        conn = sqlite3.connect(
            uri, uri=True, timeout=30, isolation_level=None,
            check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    if not writable:
        conn.execute('PRAGMA query_only=ON')
    return conn


def run_sqlite_tool_write(
    conn,
    *,
    db_path: str | Path,
    canonical_path: str | Path,
    purpose: str,
    operation: Callable[[sqlite3.Connection], _T],
    retries: int = 5,
) -> _T:
    """Execute one retryable maintenance batch in an owned transaction.

    ``operation`` is replayed only after SQLite proves the transaction never
    acquired its writer slot (``locked``/``busy``). Exact-old-value CAS remains
    the caller's responsibility for rows selected on a separate read handle.
    """
    if not callable(operation):
        raise TypeError('SQLite tooling operation must be callable')
    purpose = str(purpose or '').strip()
    if not purpose:
        raise ValueError('SQLite tooling purpose must not be empty')
    retries = max(1, min(int(retries), 20))
    if bool(getattr(conn, 'in_transaction', False)):
        raise RuntimeError(
            'SQLite tooling write received an already-active transaction')

    resolved = str(Path(db_path).resolve())
    canonical = str(Path(canonical_path).resolve())
    with _owner.maintenance_write_authority(purpose):
        for attempt in range(retries):
            # Revalidate before every physical writer acquisition so a resumed
            # process observes a cross-host takeover before mutating a page.
            _owner.assert_owner(resolved, canonical)
            try:
                conn.execute('BEGIN IMMEDIATE')
                result = operation(conn)
                conn.commit()
                return result
            except sqlite3.OperationalError as exc:
                try:
                    conn.rollback()
                except sqlite3.Error as rollback_exc:
                    logger.debug('[DB] tooling retry rollback failed: %s',
                                 rollback_exc)
                text = str(exc).lower()
                if ('locked' not in text and 'busy' not in text):
                    raise
                if attempt + 1 >= retries:
                    raise
                time.sleep(min(0.05 * (2 ** attempt), 0.8))
            except BaseException:
                try:
                    conn.rollback()
                except sqlite3.Error as rollback_exc:
                    logger.debug('[DB] tooling failure rollback failed: %s',
                                 rollback_exc)
                raise
    raise RuntimeError('unreachable SQLite tooling retry state')


__all__ = [
    'open_sqlite_candidate_connection',
    'open_sqlite_tool_connection',
    'run_sqlite_tool_write',
]

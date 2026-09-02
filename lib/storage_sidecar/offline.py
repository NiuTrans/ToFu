"""Read-only access to a stopped or concurrently WAL-backed SQLite authority.

This module exists only for offline diagnostics. Runtime application code must
use semantic Sidecar operations; diagnostics may open the SQLite file directly
because the live Sidecar token and port are intentionally process-private.
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
from urllib.parse import quote


def open_readonly_sqlite_authority(path: str | Path) -> sqlite3.Connection:
    """Open an existing SQLite authority with driver-level write denial."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f'SQLite authority does not exist: {resolved}')
    uri = f'file:{quote(str(resolved))}?mode=ro'
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=30,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=30000')
    connection.execute('PRAGMA query_only=ON')
    return connection


__all__ = ['open_readonly_sqlite_authority']

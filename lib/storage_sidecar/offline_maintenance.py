"""Explicit offline storage connections for migration and recovery tools.

Runtime application code never imports this module. A canonical SQLite write
requires a live :class:`ProjectLease`; candidate databases must be distinct
from the declared authority. PostgreSQL connections are always explicitly
addressed and never trigger application backend discovery.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, TypeVar
from urllib.parse import quote

from lib.log import get_logger
from lib.storage_sidecar.offline import open_readonly_sqlite_authority
from lib.storage_sidecar.preflight import ProjectLease


logger = get_logger(__name__)
_T = TypeVar('_T')


def open_sqlite_tool_connection(
    path: str | Path,
    *,
    writable: bool,
    lease: ProjectLease | None = None,
) -> sqlite3.Connection:
    """Open one authority connection; writes require its acquired lease."""
    if not writable:
        return open_readonly_sqlite_authority(path)
    if lease is None:
        raise RuntimeError('Writable SQLite tooling requires ProjectLease')
    resolved = lease.require_authority(path)
    uri = f'file:{quote(str(resolved))}?mode=rw'
    connection = sqlite3.connect(
        uri, uri=True, timeout=30, isolation_level=None,
        check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=30000')
    return connection


def open_sqlite_candidate_connection(
    path: str | Path,
    *,
    canonical_path: str | Path,
    writable: bool,
) -> sqlite3.Connection:
    """Open a migration candidate that is provably not the live authority."""
    resolved = Path(path).resolve()
    canonical = Path(canonical_path).resolve()
    if resolved == canonical:
        raise RuntimeError(
            f'refusing candidate connection to SQLite authority: {resolved}')
    mode = 'rw' if writable else 'ro'
    uri = f'file:{quote(str(resolved))}?mode={mode}'
    connection = sqlite3.connect(
        uri, uri=True, timeout=30, isolation_level=None,
        check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=30000')
    if not writable:
        connection.execute('PRAGMA query_only=ON')
    return connection


def run_sqlite_tool_write(
    connection: sqlite3.Connection,
    *,
    db_path: str | Path,
    lease: ProjectLease,
    purpose: str,
    operation: Callable[[sqlite3.Connection], _T],
    retries: int = 5,
) -> _T:
    """Run one bounded canonical mutation while retaining lease authority."""
    lease.require_authority(db_path)
    if not callable(operation):
        raise TypeError('SQLite tooling operation must be callable')
    if not str(purpose or '').strip():
        raise ValueError('SQLite tooling purpose must not be empty')
    if bool(getattr(connection, 'in_transaction', False)):
        raise RuntimeError('SQLite tooling received an active transaction')
    retries = max(1, min(int(retries), 20))
    for attempt in range(retries):
        lease.require_authority(db_path)
        try:
            connection.execute('BEGIN IMMEDIATE')
            result = operation(connection)
            connection.commit()
            return result
        except sqlite3.OperationalError as exc:
            connection.rollback()
            message = str(exc).lower()
            if ('locked' not in message and 'busy' not in message) \
                    or attempt + 1 >= retries:
                raise
            time.sleep(min(0.05 * (2 ** attempt), 0.8))
        except BaseException:
            connection.rollback()
            raise
    raise RuntimeError('unreachable SQLite tooling retry state')


@contextmanager
def write_transaction(connection: sqlite3.Connection):
    """Own one transaction on a disposable non-authority candidate."""
    if bool(getattr(connection, 'in_transaction', False)):
        raise RuntimeError('SQLite candidate already has an active transaction')
    connection.execute('BEGIN IMMEDIATE')
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def sqlite_schema_names(
    connection: sqlite3.Connection,
    object_type: str,
) -> set[str]:
    """Return names for one constrained SQLite schema-object class."""
    normalized_type = str(object_type).strip().lower()
    if normalized_type not in {'table', 'index'}:
        raise ValueError('SQLite schema object type must be table or index')
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type=?",
            (normalized_type,),
        )
    }


def sqlite_index_exists(
    connection: sqlite3.Connection,
    index_name: str,
) -> bool:
    """Check one index without giving operator scripts a raw SQL seam."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='index' AND name=?",
        (str(index_name),),
    ).fetchone()
    return row is not None


def checkpoint_sqlite_wal(connection: sqlite3.Connection) -> None:
    """Bound an offline writer's WAL after one committed maintenance page."""
    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')


def prepare_sqlite_compaction_candidate(
    connection: sqlite3.Connection,
) -> None:
    """Make a candidate self-contained before its main file is published."""
    mode = connection.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
    if str(mode).lower() != 'delete':
        raise RuntimeError('SQLite compaction candidate rejected DELETE mode')
    connection.execute('PRAGMA synchronous=FULL')


def _quote_identifier(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def drop_sqlite_index(
    connection: sqlite3.Connection,
    index_name: str,
) -> None:
    """Execute one index retirement inside the caller-owned transaction."""
    connection.execute(f'DROP INDEX {_quote_identifier(index_name)}')


def create_portable_archive_table(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[Mapping[str, Any]],
    *,
    derived_types: frozenset[str],
    type_map: Mapping[str, str],
) -> None:
    primary_key = [str(column['name']) for column in columns if column['pk']]
    definitions: list[str] = []
    for column in columns:
        source_type = str(column['udt_name'])
        if source_type in derived_types:
            continue
        definition = (
            f'{_quote_identifier(column["name"])} '
            f'{type_map.get(source_type, "TEXT")}')
        if not column['nullable']:
            definition += ' NOT NULL'
        definitions.append(definition)
    if primary_key:
        definitions.append(
            'PRIMARY KEY ('
            + ', '.join(_quote_identifier(name) for name in primary_key)
            + ')')
    connection.execute(
        f'CREATE TABLE {_quote_identifier(table)} ('
        + ', '.join(definitions) + ')')


def add_portable_archive_column(
    connection: sqlite3.Connection,
    table: str,
    column: Mapping[str, Any],
    *,
    type_map: Mapping[str, str],
) -> None:
    connection.execute(
        f'ALTER TABLE {_quote_identifier(table)} ADD COLUMN '
        f'{_quote_identifier(column["name"])} '
        f'{type_map.get(str(column["udt_name"]), "TEXT")}')


def suspend_nonunique_indexes(
    connection: sqlite3.Connection,
) -> list[tuple[str, str]]:
    indexes = [tuple(row) for row in connection.execute("""
        SELECT name, sql FROM sqlite_master
        WHERE type='index' AND sql IS NOT NULL
          AND upper(ltrim(sql)) NOT LIKE 'CREATE UNIQUE INDEX%'
        ORDER BY name
    """).fetchall()]
    with write_transaction(connection):
        for name, _ddl in indexes:
            connection.execute(f'DROP INDEX {_quote_identifier(name)}')
    return indexes


def restore_index(connection: sqlite3.Connection, ddl: str) -> None:
    with write_transaction(connection):
        connection.execute(ddl)


def open_postgres_tool_connection(dsn=None, **kwargs):
    """Open one explicitly configured PostgreSQL migration connection."""
    import psycopg2

    return psycopg2.connect(dsn, **kwargs) if dsn is not None \
        else psycopg2.connect(**kwargs)


def release_postgres_tool_snapshot(connection) -> None:
    connection.rollback()


__all__ = [
    'add_portable_archive_column',
    'checkpoint_sqlite_wal',
    'create_portable_archive_table',
    'drop_sqlite_index',
    'open_postgres_tool_connection',
    'open_sqlite_candidate_connection',
    'open_sqlite_tool_connection',
    'prepare_sqlite_compaction_candidate',
    'release_postgres_tool_snapshot',
    'restore_index',
    'run_sqlite_tool_write',
    'sqlite_index_exists',
    'sqlite_schema_names',
    'suspend_nonunique_indexes',
    'write_transaction',
]

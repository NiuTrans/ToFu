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
from lib.task_event_contract import (
    STRUCTURAL_EVENT_TYPES,
    TASK_EVENT_STREAMING_RETENTION_MS,
    TASK_EVENT_STRUCTURAL_RETENTION_MS,
)


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


def sqlite_conversation_change_references_available(
    connection: sqlite3.Connection,
) -> bool:
    """Return whether an offline authority can contain schema-51 references.

    Recovery tooling may run before the application has upgraded a schema-50
    authority. Missing tables or columns mean there cannot be compact replay
    references to protect; other SQLite failures remain fail-closed.
    """
    columns = {
        str(row[1])
        for row in connection.execute(
            'PRAGMA table_info("storage_conversation_changes")'
        )
    }
    return {'attempt_id', 'attempt_sequence'} <= columns


def sqlite_transport_retention_candidate_queries(
    *,
    attempt_cutoff_ms: int,
    now_ms: int,
    aggregate: bool,
    last_settled_ms: int = -1,
    last_attempt_id: str = '',
    protect_conversation_change_references: bool = True,
) -> dict[str, dict[str, object]]:
    """Build canonical Sidecar/legacy retention queries for inspect/delete."""
    structural_types = tuple(sorted(STRUCTURAL_EVENT_TYPES))
    placeholders = ','.join('?' for _ in structural_types)
    if aggregate:
        attempt_projection = (
            'count(*), COALESCE(sum('
            'length(CAST(old.payload AS BLOB))), 0)')
        task_projection = (
            'count(*), COALESCE(sum('
            'length(CAST(payload AS BLOB))), 0)')
        attempt_suffix = ''
        task_suffix = ''
    else:
        attempt_projection = (
            'old.rowid, length(CAST(old.payload AS BLOB))')
        task_projection = 'rowid, length(CAST(payload AS BLOB))'
        attempt_suffix = ' ORDER BY old.created_at, old.rowid LIMIT ?'
        task_suffix = ' ORDER BY ts_ms, rowid LIMIT ?'

    reference_guard = ''
    if protect_conversation_change_references:
        reference_guard = (
            'AND NOT EXISTS (SELECT 1 '
            'FROM storage_conversation_changes AS changes '
            'WHERE changes.attempt_id=attempts.attempt_id '
            'AND changes.attempt_sequence IS NOT NULL) '
        )
    if aggregate:
        sidecar_sql = (
            'SELECT count(*), COALESCE(sum('
            'length(CAST(events.payload_json AS BLOB))), 0) '
            'FROM storage_attempt_events AS events '
            'JOIN storage_generation_attempts AS attempts '
            'ON attempts.attempt_id = events.attempt_id '
            "WHERE attempts.status NOT IN ('pending','running') "
            'AND attempts.settled_at IS NOT NULL '
            'AND attempts.settled_at < ? '
            + reference_guard
        )
        sidecar_params = (int(attempt_cutoff_ms),)
    else:
        sidecar_sql = (
            'SELECT attempt_id, settled_at '
            'FROM storage_generation_attempts AS attempts '
            "WHERE status NOT IN ('pending','running') "
            'AND settled_at IS NOT NULL AND settled_at < ? '
            + reference_guard
            + 'AND (settled_at > ? OR '
            '(settled_at = ? AND attempt_id > ?)) '
            'ORDER BY settled_at, attempt_id LIMIT 64'
        )
        sidecar_params = (
            int(attempt_cutoff_ms),
            int(last_settled_ms),
            int(last_settled_ms),
            str(last_attempt_id),
        )

    sidecar_required_tables = (
        'storage_attempt_events',
        'storage_generation_attempts',
    )
    if protect_conversation_change_references:
        sidecar_required_tables += ('storage_conversation_changes',)

    return {
        'storage_attempt_events': {
            'table': 'storage_attempt_events',
            'required_tables': sidecar_required_tables,
            'sql': sidecar_sql,
            'params': sidecar_params,
        },
        'attempt_events': {
            'table': 'attempt_events',
            'sql': (
                f'SELECT {attempt_projection} FROM attempt_events AS old '
                'WHERE old.created_at < ? AND EXISTS ('
                'SELECT 1 FROM attempt_events AS newer '
                'WHERE newer.attempt_id = old.attempt_id '
                'AND newer.seq > old.seq)'
                + attempt_suffix
            ),
            'params': (int(attempt_cutoff_ms),),
        },
        'task_events_streaming': {
            'table': 'task_events',
            'sql': (
                f'SELECT {task_projection} FROM task_events '
                f'WHERE ts_ms < ? AND type NOT IN ({placeholders})'
                + task_suffix
            ),
            'params': (
                int(now_ms) - TASK_EVENT_STREAMING_RETENTION_MS,
                *structural_types,
            ),
        },
        'task_events_structural': {
            'table': 'task_events',
            'sql': (
                f'SELECT {task_projection} FROM task_events '
                f'WHERE ts_ms < ? AND type IN ({placeholders})'
                + task_suffix
            ),
            'params': (
                int(now_ms) - TASK_EVENT_STRUCTURAL_RETENTION_MS,
                *structural_types,
            ),
        },
    }


def measure_sqlite_transport_retention(
    connection: sqlite3.Connection,
    *,
    existing_tables: set[str],
    ttl_days: float,
    now_ms: int,
    deadline_at: float,
) -> dict:
    """Measure exact expired payload selected by the offline delete pass."""
    attempt_cutoff_ms = int(now_ms - ttl_days * 86_400_000)
    references_available = sqlite_conversation_change_references_available(
        connection
    )
    queries = sqlite_transport_retention_candidate_queries(
        attempt_cutoff_ms=attempt_cutoff_ms,
        now_ms=now_ms,
        aggregate=True,
        protect_conversation_change_references=references_available,
    )
    sources: dict[str, dict] = {}
    complete = True
    timed_out = False
    for source, spec in queries.items():
        required_tables = {
            str(table) for table in spec.get(
                'required_tables', (spec['table'],))
        }
        if not required_tables <= existing_tables:
            sources[source] = {
                'measurement': 'not_applicable',
                'row_count': 0,
                'payload_bytes': 0,
            }
            continue
        if timed_out or time.monotonic() >= deadline_at:
            complete = False
            timed_out = True
            sources[source] = {
                'error': 'analysis_budget_exhausted',
                'measurement': 'not_completed',
            }
            continue
        try:
            row = connection.execute(
                str(spec['sql']), tuple(spec['params'])).fetchone()
            sources[source] = {
                'measurement': 'exact_expired_payload',
                'row_count': max(0, int(row[0] or 0)),
                'payload_bytes': max(0, int(row[1] or 0)),
            }
        except sqlite3.OperationalError as exc:
            interrupted = (
                'interrupted' in str(exc).lower()
                and time.monotonic() >= deadline_at
            )
            if interrupted:
                complete = False
                timed_out = True
                sources[source] = {
                    'error': 'analysis_budget_exhausted',
                    'measurement': 'not_completed',
                }
            else:
                complete = False
                sources[source] = {
                    'error': str(exc),
                    'measurement': 'not_completed',
                }
        except sqlite3.Error as exc:
            complete = False
            sources[source] = {
                'error': str(exc),
                'measurement': 'not_completed',
            }

    completed = [
        measurement for measurement in sources.values()
        if measurement.get('measurement') == 'exact_expired_payload'
    ]
    return {
        'measurement': 'exact_expired_payload',
        'measurement_complete': complete,
        'timed_out': timed_out,
        'ttl_days': float(ttl_days),
        'candidate_rows': sum(
            int(measurement['row_count']) for measurement in completed),
        'candidate_payload_bytes': sum(
            int(measurement['payload_bytes']) for measurement in completed),
        'completed_sources': len(completed),
        'sources': sources,
    }


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
    'measure_sqlite_transport_retention',
    'sqlite_index_exists',
    'sqlite_transport_retention_candidate_queries',
    'sqlite_schema_names',
    'suspend_nonunique_indexes',
    'write_transaction',
]

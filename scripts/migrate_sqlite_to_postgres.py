#!/usr/bin/env python3
"""Copy a stopped SQLite authority into empty external PostgreSQL storage.

The default invocation is plan-only and never opens a PostgreSQL connection.
Execution requires ``--execute --source-quiesced --confirm-empty-target``. The
source is held under the project lease and read from one explicit read-only
SQLite transaction. PostgreSQL must already contain the exact current Sidecar
schema and no business rows; schema creation remains the migration Job's job.

Full migrations copy every dynamically discovered table, correct owned
sequences, and compare row counts plus the shared order-independent,
duplicate-sensitive digest before committing. A selected-table run is useful
for contract rehearsal but is permanently ineligible for cutover.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Callable, Iterable, Iterator, Protocol, Sequence
from urllib.parse import parse_qs, urlsplit

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from scripts import migrate_pg_to_sqlite as _reverse_migrator
except ModuleNotFoundError as exc:  # direct ``python scripts/...`` execution
    if exc.name != 'scripts':
        raise
    import migrate_pg_to_sqlite as _reverse_migrator

from lib.storage_sidecar import offline_maintenance as _SQLITE_TOOLING
from lib.storage_sidecar.schema import SCHEMA_VERSION


_MIGRATION_LOCK_NAMESPACE = 'tofu.storage'
_MIGRATION_LOCK_KEY = 'sqlite-to-postgres'
_SCHEMA_LOCK_KEY = 'schema-migration'
_META_TABLE = 'storage_meta'
_META_COLUMNS = ('meta_key', 'meta_value')
_MAX_SECRET_BYTES = 16 * 1024
_MAX_BATCH_ROWS = 100_000


class MigrationRefused(RuntimeError):
    """A fail-closed precondition prevented a target mutation."""


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    name: str
    postgres_type: str


@dataclass(frozen=True, slots=True)
class MigrationOptions:
    source: Path
    dsn_secret_file: Path
    report: Path
    batch_rows: int = 1000
    tables: tuple[str, ...] = ()
    source_quiesced: bool = False
    connect_timeout: int = 10


class MigrationTarget(Protocol):
    def acquire_migration_lock(self) -> None: ...

    def validate_schema_version(self) -> int: ...

    def table_names(self) -> list[str]: ...

    def column_specs(self, table: str) -> list[ColumnSpec]: ...

    def row_count(self, table: str) -> int: ...

    def metadata_rows(self) -> list[tuple[str, str]]: ...

    def prepare_metadata_import(self) -> None: ...

    def insert_batch(
        self,
        table: str,
        columns: Sequence[ColumnSpec],
        rows: Sequence[Sequence[Any]],
    ) -> None: ...

    def iter_batches(
        self,
        table: str,
        columns: Sequence[ColumnSpec],
        batch_rows: int,
    ) -> Iterator[list[tuple[Any, ...]]]: ...

    def reset_owned_sequences(self, tables: Sequence[str]) -> list[dict]: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def close(self) -> None: ...


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _quote_sqlite_identifier(value: str) -> str:
    return _reverse_migrator._quote_ident(value)


def _dsn_uses_verified_tls(dsn: str) -> bool:
    if dsn.startswith(('postgres://', 'postgresql://')):
        values = parse_qs(urlsplit(dsn).query).get('sslmode', ())
        return bool(values and values[-1].lower() == 'verify-full')
    match = re.search(
        r'(?:^|\s)sslmode\s*=\s*["\']?([^\s"\']+)',
        dsn,
        flags=re.IGNORECASE,
    )
    return bool(match and match.group(1).lower() == 'verify-full')


def read_dsn_secret(path: str | os.PathLike[str]) -> str:
    """Read a TLS-verified DSN only from one absolute bounded secret file."""
    secret_path = Path(path)
    if not secret_path.is_absolute():
        raise MigrationRefused('PostgreSQL DSN secret file must be absolute')
    try:
        metadata = secret_path.stat()
    except OSError as exc:
        raise MigrationRefused('PostgreSQL DSN secret file is unreadable') from exc
    if not secret_path.is_file() or not 0 < metadata.st_size <= _MAX_SECRET_BYTES:
        raise MigrationRefused(
            'PostgreSQL DSN secret file must contain 1..16384 bytes')
    try:
        value = secret_path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError) as exc:
        raise MigrationRefused(
            'PostgreSQL DSN secret file is not readable UTF-8') from exc
    if not value or '\x00' in value:
        raise MigrationRefused('PostgreSQL DSN secret file is invalid')
    if not _dsn_uses_verified_tls(value):
        raise MigrationRefused(
            'external PostgreSQL DSN requires sslmode=verify-full')
    return value


def _report_contains_secret(value: Any, secret: str) -> bool:
    if isinstance(value, dict):
        return any(_report_contains_secret(item, secret) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_report_contains_secret(item, secret) for item in value)
    return bool(secret and secret in str(value))


def write_report(path: Path, report: dict[str, Any], *, dsn: str) -> None:
    """Atomically publish a durable report after proving it contains no DSN."""
    if _report_contains_secret(report, dsn):
        raise RuntimeError('refusing to write a migration report containing a DSN')
    _reverse_migrator._write_report(path, report)


class SQLiteSnapshot:
    """One explicitly pinned, query-only SQLite transaction."""

    def __init__(self, connection, path: Path) -> None:
        self.connection = connection
        self.path = path
        self._released = False
        self.data_version_at_start = int(
            connection.execute('PRAGMA data_version').fetchone()[0])

    def schema_version(self) -> int:
        row = self.connection.execute(
            'SELECT meta_value FROM storage_meta WHERE meta_key=?',
            ('schema_version',),
        ).fetchone()
        if row is None:
            raise MigrationRefused('SQLite storage schema version is missing')
        try:
            return int(row[0])
        except (TypeError, ValueError) as exc:
            raise MigrationRefused(
                'SQLite storage schema version is invalid') from exc

    def table_names(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [str(row[0]) for row in rows]

    def column_names(self, table: str) -> list[str]:
        rows = self.connection.execute(
            f'PRAGMA table_info({_quote_sqlite_identifier(table)})').fetchall()
        return [str(row[1]) for row in rows]

    def iter_batches(
        self,
        table: str,
        columns: Sequence[str],
        batch_rows: int,
    ) -> Iterator[list[tuple[Any, ...]]]:
        selected = ', '.join(_quote_sqlite_identifier(name) for name in columns)
        cursor = self.connection.execute(
            f'SELECT {selected} FROM {_quote_sqlite_identifier(table)}')
        while True:
            rows = cursor.fetchmany(batch_rows)
            if not rows:
                return
            yield [tuple(row) for row in rows]

    def release_and_current_data_version(self) -> int:
        """End the pinned snapshot before checking for another writer's commit."""
        if not self._released:
            self.connection.rollback()
            self._released = True
        return int(self.connection.execute('PRAGMA data_version').fetchone()[0])


@contextmanager
def open_sqlite_snapshot(path: Path) -> Iterator[SQLiteSnapshot]:
    """Open the source with mode=ro/query_only and pin one read snapshot."""
    connection = _SQLITE_TOOLING.open_sqlite_tool_connection(path, writable=False)
    try:
        query_only = int(connection.execute('PRAGMA query_only').fetchone()[0])
        if query_only != 1:
            raise MigrationRefused('SQLite source did not enter query-only mode')
        connection.execute('BEGIN')
        # The first catalog read establishes the snapshot before target work.
        connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
        ).fetchall()
        yield SQLiteSnapshot(connection, path)
    finally:
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()


@contextmanager
def _project_lease(data_dir: Path):
    from lib.storage_sidecar.preflight import ProjectLease

    lease = ProjectLease(
        data_dir,
        owner_kind='offline_maintenance',
        owner_label='SQLite to PostgreSQL migration',
    )
    lease.acquire()
    try:
        yield
    finally:
        lease.release()


def _canonical_json(value: Any) -> str:
    if isinstance(value, (bytes, bytearray, memoryview)):
        value = bytes(value).decode('utf-8')
    if isinstance(value, str):
        value = json.loads(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )


def canonicalize_row(
    row: Sequence[Any], columns: Sequence[ColumnSpec],
) -> tuple[Any, ...]:
    if len(row) != len(columns):
        raise MigrationRefused('row width does not match target column contract')
    values: list[Any] = []
    for value, column in zip(row, columns):
        pg_type = column.postgres_type.lower()
        if value is not None and pg_type in {'json', 'jsonb'}:
            value = _canonical_json(value)
        elif value is not None and pg_type in {'bool', 'boolean'}:
            if value not in (False, True, 0, 1):
                raise MigrationRefused(
                    f'SQLite boolean value is invalid for column {column.name}')
            value = int(bool(value))
        elif value is not None and pg_type == 'bytea':
            value = bytes(value)
        values.append(value)
    return tuple(values)


def digest_batches(
    batches: Iterable[Sequence[Sequence[Any]]],
    columns: Sequence[ColumnSpec],
) -> _reverse_migrator.RowDigest:
    digest = _reverse_migrator.RowDigest()
    for batch in batches:
        for row in batch:
            digest.add(canonicalize_row(row, columns))
    return digest


def sequence_setval_state(maximum: Any) -> tuple[int, bool]:
    """Return a safe PostgreSQL setval(value, is_called) state."""
    maximum_value = int(maximum) if maximum is not None else None
    if maximum_value is None or maximum_value < 1:
        return 1, False
    return maximum_value, True


class PsycopgMigrationTarget:
    """Thin offline adapter that reuses Sidecar session/schema authorities."""

    def __init__(self, dsn: str, connect_timeout: int) -> None:
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        from psycopg.types.json import Json, Jsonb
        from lib.storage_sidecar.adapters.postgres import PostgresSession

        self._psycopg = psycopg
        self._sql = sql
        self._json = Json
        self._jsonb = Jsonb
        self.connection = psycopg.connect(
            dsn,
            autocommit=False,
            row_factory=dict_row,
            application_name='tofu-sqlite-to-postgres-migration',
            connect_timeout=connect_timeout,
        )
        self.session = PostgresSession(self.connection)

    def acquire_migration_lock(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute('SET LOCAL statement_timeout = 0')
            cursor.execute('SET LOCAL idle_in_transaction_session_timeout = 0')
            cursor.execute('SET LOCAL lock_timeout = 5000')
        # Match the one-shot schema Job's lock first, then own the data import.
        self.session.lock_key(_MIGRATION_LOCK_NAMESPACE, _SCHEMA_LOCK_KEY)
        self.session.lock_key(_MIGRATION_LOCK_NAMESPACE, _MIGRATION_LOCK_KEY)

    def validate_schema_version(self) -> int:
        from lib.storage_sidecar.schema import validate_schema_version

        return validate_schema_version(self.session)

    def table_names(self) -> list[str]:
        rows = self.session.fetch_all(
            "SELECT tablename AS name FROM pg_tables "
            "WHERE schemaname='public' ORDER BY tablename")
        return [str(row['name']) for row in rows]

    def column_specs(self, table: str) -> list[ColumnSpec]:
        rows = self.session.fetch_all(
            'SELECT column_name, udt_name FROM information_schema.columns '
            'WHERE table_schema=? AND table_name=? ORDER BY ordinal_position',
            ('public', table),
        )
        return [
            ColumnSpec(str(row['column_name']), str(row['udt_name']))
            for row in rows
        ]

    def _one_identifier(self, template: str, table: str):
        statement = self._sql.SQL(template).format(self._sql.Identifier(table))
        with self.connection.cursor() as cursor:
            cursor.execute(statement)
            return cursor.fetchone()

    def row_count(self, table: str) -> int:
        row = self._one_identifier('SELECT COUNT(*) AS count FROM {}', table)
        return int(row['count'])

    def metadata_rows(self) -> list[tuple[str, str]]:
        rows = self.session.fetch_all(
            'SELECT meta_key, meta_value FROM storage_meta ORDER BY meta_key')
        return [(str(row['meta_key']), str(row['meta_value'])) for row in rows]

    def prepare_metadata_import(self) -> None:
        self.session.execute('DELETE FROM storage_meta')

    def _adapt_value(self, value: Any, column: ColumnSpec) -> Any:
        if value is None:
            return None
        pg_type = column.postgres_type.lower()
        if pg_type in {'json', 'jsonb'}:
            parsed = json.loads(_canonical_json(value))
            return self._jsonb(parsed) if pg_type == 'jsonb' else self._json(parsed)
        if pg_type in {'bool', 'boolean'}:
            return bool(value)
        if pg_type == 'bytea':
            return bytes(value)
        return value

    def insert_batch(
        self,
        table: str,
        columns: Sequence[ColumnSpec],
        rows: Sequence[Sequence[Any]],
    ) -> None:
        if not rows:
            return
        identifiers = self._sql.SQL(', ').join(
            self._sql.Identifier(column.name) for column in columns)
        placeholders = self._sql.SQL(', ').join(
            self._sql.Placeholder() for _column in columns)
        statement = self._sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
            self._sql.Identifier(table), identifiers, placeholders)
        adapted = [
            tuple(self._adapt_value(value, column)
                  for value, column in zip(row, columns))
            for row in rows
        ]
        with self.connection.cursor() as cursor:
            cursor.executemany(statement, adapted)

    def iter_batches(
        self,
        table: str,
        columns: Sequence[ColumnSpec],
        batch_rows: int,
    ) -> Iterator[list[tuple[Any, ...]]]:
        identifiers = self._sql.SQL(', ').join(
            self._sql.Identifier(column.name) for column in columns)
        statement = self._sql.SQL('SELECT {} FROM {}').format(
            identifiers, self._sql.Identifier(table))
        with self.connection.cursor() as cursor:
            cursor.execute(statement)
            while True:
                rows = cursor.fetchmany(batch_rows)
                if not rows:
                    return
                yield [
                    tuple(row[column.name] for column in columns)
                    for row in rows
                ]

    def reset_owned_sequences(self, tables: Sequence[str]) -> list[dict]:
        if not tables:
            return []
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name, column_name, "
                "pg_get_serial_sequence(format('%%I.%%I', table_schema, table_name), "
                "column_name) AS sequence_name "
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name=ANY(%s) "
                "ORDER BY table_name, ordinal_position",
                (list(tables),),
            )
            sequence_rows = [dict(row) for row in cursor.fetchall()]
        corrected: list[dict] = []
        for row in sequence_rows:
            sequence_name = row.get('sequence_name')
            if not sequence_name:
                continue
            statement = self._sql.SQL('SELECT MAX({}) AS maximum FROM {}').format(
                self._sql.Identifier(row['column_name']),
                self._sql.Identifier(row['table_name']),
            )
            with self.connection.cursor() as cursor:
                cursor.execute(statement)
                maximum = cursor.fetchone()['maximum']
                next_value, is_called = sequence_setval_state(maximum)
                cursor.execute(
                    'SELECT setval(%s::regclass, %s, %s)',
                    (sequence_name, next_value, is_called),
                )
            corrected.append({
                'table': str(row['table_name']),
                'column': str(row['column_name']),
                'maximum': maximum,
            })
        return corrected

    def commit(self) -> None:
        self.connection.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.connection.close()


def _connect_target(dsn: str, connect_timeout: int) -> MigrationTarget:
    return PsycopgMigrationTarget(dsn, connect_timeout)


def _validate_source_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MigrationRefused('SQLite source is unavailable') from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MigrationRefused('SQLite source must be a regular non-symlink file')
    if metadata.st_size <= 0:
        raise MigrationRefused('SQLite source is empty')


def _require_project_data_path(path: Path, label: str) -> None:
    data_root = (_PROJECT_ROOT / 'data').resolve()
    try:
        path.resolve().relative_to(data_root)
    except (OSError, ValueError) as exc:
        raise MigrationRefused(
            f'{label} must stay inside project data directory') from exc


def _ordered_tables(tables: Iterable[str]) -> list[str]:
    unique = set(tables)
    ordered = sorted(unique - {_META_TABLE})
    return ([_META_TABLE] if _META_TABLE in unique else []) + ordered


def _base_report(options: MigrationOptions) -> dict[str, Any]:
    return {
        'version': 1,
        'status': 'running',
        'cutover_ready': False,
        'cutover_reason': 'migration_not_complete',
        'started_at': _utc_now(),
        'source': {
            'backend': 'sqlite',
            'path': str(options.source),
            'operator_declared_quiesced': bool(options.source_quiesced),
        },
        'target': {
            'backend': 'postgres',
            'dsn_secret_file': str(options.dsn_secret_file),
            'expected_schema_version': SCHEMA_VERSION,
        },
        'selected_tables': list(options.tables) if options.tables else 'all',
        'batch_rows': options.batch_rows,
        'tables': {},
    }


def _preflight_tables(
    source: SQLiteSnapshot,
    target: MigrationTarget,
    requested: Sequence[str],
    report: dict[str, Any],
) -> tuple[list[str], bool]:
    source_tables = set(source.table_names())
    target_tables = set(target.table_names())
    source_only = sorted(source_tables - target_tables)
    target_only = sorted(target_tables - source_tables)
    report['table_mapping'] = {
        'source_only': source_only,
        'target_only': target_only,
    }
    if requested:
        selected = sorted(set(requested))
        missing_source = sorted(set(selected) - source_tables)
        missing_target = sorted(set(selected) - target_tables)
        if missing_source or missing_target:
            report['table_mapping']['selected_missing_source'] = missing_source
            report['table_mapping']['selected_missing_target'] = missing_target
            raise MigrationRefused('selected migration table is unavailable')
        return _ordered_tables(selected), False
    if source_only or target_only:
        raise MigrationRefused(
            'full migration requires identical source and target table sets')
    return _ordered_tables(source_tables), True


def _validate_empty_target(
    target: MigrationTarget,
    target_tables: Sequence[str],
    report: dict[str, Any],
) -> None:
    metadata = target.metadata_rows()
    expected_metadata = [('schema_version', str(SCHEMA_VERSION))]
    report['target_initial_metadata'] = metadata
    nonempty = {}
    for table in target_tables:
        if table == _META_TABLE:
            continue
        count = target.row_count(table)
        if count:
            nonempty[table] = count
    report['target_nonempty_tables'] = nonempty
    if metadata != expected_metadata:
        raise MigrationRefused(
            'target storage_meta must contain only the current schema version')
    if nonempty:
        raise MigrationRefused('target PostgreSQL business tables are not empty')


def run_migration(
    options: MigrationOptions,
    *,
    dsn: str,
    target_factory: Callable[[str, int], MigrationTarget] = _connect_target,
    lease_factory: Callable[[Path], Any] = _project_lease,
    source_factory: Callable[[Path], Any] = open_sqlite_snapshot,
) -> dict[str, Any]:
    """Execute one migration using injectable offline source/target seams."""
    if not options.source_quiesced:
        raise MigrationRefused(
            'execution requires an explicit stopped-write source assertion')
    if not 1 <= options.batch_rows <= _MAX_BATCH_ROWS:
        raise MigrationRefused('batch_rows must be between 1 and 100000')
    _validate_source_file(options.source)
    report = _base_report(options)
    write_report(options.report, report, dsn=dsn)
    target: MigrationTarget | None = None
    try:
        with lease_factory(options.source.parent):
            report['source']['project_lease'] = 'acquired'
            with source_factory(options.source) as source:
                source_version = source.schema_version()
                report['source']['schema_version'] = source_version
                report['source']['data_version_at_start'] = (
                    source.data_version_at_start)
                if source_version != SCHEMA_VERSION:
                    raise MigrationRefused(
                        'SQLite source schema does not match the current version')

                target = target_factory(dsn, options.connect_timeout)
                target.acquire_migration_lock()
                report['target']['advisory_locks'] = [
                    f'{_MIGRATION_LOCK_NAMESPACE}:{_SCHEMA_LOCK_KEY}',
                    f'{_MIGRATION_LOCK_NAMESPACE}:{_MIGRATION_LOCK_KEY}',
                ]
                target_version = target.validate_schema_version()
                report['target']['schema_version'] = target_version
                if target_version != SCHEMA_VERSION:
                    raise MigrationRefused(
                        'PostgreSQL target schema does not match the current version')

                target_tables = target.table_names()
                selected, is_full = _preflight_tables(
                    source, target, options.tables, report)
                report['is_full_migration'] = is_full
                _validate_empty_target(target, target_tables, report)
                write_report(options.report, report, dsn=dsn)

                for table in selected:
                    source_names = source.column_names(table)
                    target_columns = target.column_specs(table)
                    target_names = [column.name for column in target_columns]
                    if set(source_names) != set(target_names):
                        report['tables'][table] = {
                            'status': 'column_mismatch',
                            'source_columns': source_names,
                            'target_columns': target_names,
                        }
                        write_report(options.report, report, dsn=dsn)
                        raise MigrationRefused(
                            'source and target columns differ for migration table')
                    if table == _META_TABLE:
                        if tuple(target_names) != _META_COLUMNS:
                            raise MigrationRefused(
                                'storage_meta column contract is unexpected')
                        target.prepare_metadata_import()

                    source_digest = _reverse_migrator.RowDigest()
                    batches = 0
                    rows = 0
                    for batch in source.iter_batches(
                            table, target_names, options.batch_rows):
                        for row in batch:
                            source_digest.add(
                                canonicalize_row(row, target_columns))
                        target.insert_batch(table, target_columns, batch)
                        batches += 1
                        rows += len(batch)

                    target_digest = digest_batches(
                        target.iter_batches(
                            table, target_columns, options.batch_rows),
                        target_columns,
                    )
                    equal = source_digest.signature() == target_digest.signature()
                    report['tables'][table] = {
                        'status': 'verified' if equal else 'mismatch',
                        'columns': target_names,
                        'source': source_digest.as_dict(),
                        'target': target_digest.as_dict(),
                        'batches': batches,
                        'rows_inserted': rows,
                    }
                    write_report(options.report, report, dsn=dsn)
                    if not equal:
                        raise MigrationRefused(
                            'row count or duplicate-sensitive digest mismatch')

                report['sequence_corrections'] = target.reset_owned_sequences(selected)
                source_data_version_at_end = (
                    source.release_and_current_data_version())
                report['source']['data_version_after_snapshot_release'] = (
                    source_data_version_at_end)
                if source_data_version_at_end != source.data_version_at_start:
                    raise MigrationRefused(
                        'SQLite source changed during the stopped-write migration')

                target.commit()
                report['target_committed_at'] = _utc_now()
                report['completed_at'] = _utc_now()
                if is_full:
                    report['status'] = 'verified'
                    report['cutover_ready'] = True
                    report['cutover_reason'] = (
                        'full_copy_committed_with_schema_count_digest_and_sequence_parity')
                else:
                    report['status'] = 'partial_verified'
                    report['cutover_ready'] = False
                    report['cutover_reason'] = 'selected_tables_are_never_cutover_ready'
                write_report(options.report, report, dsn=dsn)
                return report
    except BaseException as exc:
        if target is not None:
            try:
                target.rollback()
            except BaseException:
                pass
        report['status'] = 'failed'
        report['cutover_ready'] = False
        report['cutover_reason'] = 'migration_failed'
        report['failed_at'] = _utc_now()
        # Driver messages can contain connection metadata. Record type only.
        report['error'] = {'type': type(exc).__name__}
        try:
            write_report(options.report, report, dsn=dsn)
        except BaseException:
            pass
        raise
    finally:
        if target is not None:
            target.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default='data/tofu.db')
    parser.add_argument('--postgres-dsn-file', required=True)
    parser.add_argument(
        '--report', default='data/sqlite-to-postgres.report.json')
    parser.add_argument('--batch-rows', type=int, default=1000)
    parser.add_argument('--connect-timeout', type=int, default=10)
    parser.add_argument('--table', dest='tables', action='append', default=[])
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--source-quiesced', action='store_true')
    parser.add_argument('--confirm-empty-target', action='store_true')
    return parser


def _safe_summary(report: dict[str, Any], report_path: Path) -> dict[str, Any]:
    return {
        'status': report['status'],
        'cutoverReady': bool(report.get('cutover_ready')),
        'tables': len(report.get('tables', {})),
        'report': str(report_path),
    }


def main(
    argv: Iterable[str] | None = None,
    *,
    target_factory: Callable[[str, int], MigrationTarget] | None = None,
    lease_factory: Callable[[Path], Any] | None = None,
    source_factory: Callable[[Path], Any] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    raw_source = Path(args.source)
    source_candidate = (
        raw_source if raw_source.is_absolute() else Path.cwd() / raw_source)
    report_path = Path(args.report).resolve()
    dsn_file = Path(args.postgres_dsn_file)
    try:
        # Validate the caller-provided path before resolve() can hide a symlink.
        _validate_source_file(source_candidate)
        source = source_candidate.resolve()
        _require_project_data_path(source, 'source')
        _require_project_data_path(report_path, 'report')
        canonical_source = (_PROJECT_ROOT / 'data' / 'tofu.db').resolve()
        if source != canonical_source:
            raise MigrationRefused(
                'source must be the canonical stopped SQLite authority')
        if source == report_path:
            raise MigrationRefused('report path must differ from SQLite source')
        if (not 1 <= args.batch_rows <= _MAX_BATCH_ROWS
                or not 1 <= args.connect_timeout <= 120):
            raise MigrationRefused('batch/connect bounds are invalid')
        dsn = read_dsn_secret(dsn_file)
        options = MigrationOptions(
            source=source,
            dsn_secret_file=dsn_file.resolve(),
            report=report_path,
            batch_rows=args.batch_rows,
            tables=tuple(args.tables),
            source_quiesced=bool(args.source_quiesced),
            connect_timeout=args.connect_timeout,
        )
        if not args.execute:
            planned = _base_report(options)
            planned.update({
                'status': 'planned',
                'cutover_ready': False,
                'cutover_reason': 'execution_confirmation_missing',
            })
            write_report(report_path, planned, dsn=dsn)
            print(json.dumps(
                _safe_summary(planned, report_path), sort_keys=True))
            return 0
        if not args.source_quiesced or not args.confirm_empty_target:
            raise MigrationRefused(
                'execution requires --source-quiesced and '
                '--confirm-empty-target')
        report = run_migration(
            options,
            dsn=dsn,
            target_factory=target_factory or _connect_target,
            lease_factory=lease_factory or _project_lease,
            source_factory=source_factory or open_sqlite_snapshot,
        )
        print(json.dumps(_safe_summary(report, report_path), sort_keys=True))
        return 0
    except BaseException as exc:
        print(json.dumps({
            'status': 'failed',
            'cutoverReady': False,
            'errorType': type(exc).__name__,
            'report': str(report_path),
        }, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())


__all__ = [
    'ColumnSpec',
    'MigrationOptions',
    'MigrationRefused',
    'PsycopgMigrationTarget',
    'SQLiteSnapshot',
    'canonicalize_row',
    'digest_batches',
    'main',
    'open_sqlite_snapshot',
    'read_dsn_secret',
    'run_migration',
    'sequence_setval_state',
    'write_report',
]

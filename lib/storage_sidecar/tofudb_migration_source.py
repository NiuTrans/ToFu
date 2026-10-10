"""Pinned, bounded SQLite source reads for explicit Tofu-DB migration.

The operator owns the stopped-write lease and source/target binding. This
module owns SQL and driver admission: one query-only snapshot, declared
primary-key pagination, a byte ceiling enforced by SQLite before Python row
materialization, and detection of another connection's committed changes.
Domain importers remain responsible for bounded decoding and exact target
enumeration; a source iterator is never a target row-count oracle.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any

from lib.storage_sidecar.generated_tofudb_migration import (
    MAX_KEY_COLUMNS,
    MAX_SOURCE_BATCH_ROWS,
    MAX_SOURCE_COLUMNS,
    MAX_SOURCE_CHUNK_BYTES,
    MAX_SOURCE_ROW_BYTES,
    MAX_SOURCE_TABLES,
    MAX_SOURCE_WIDE_ROW_BYTES,
)
from lib.storage_sidecar.offline import open_readonly_sqlite_authority


class MigrationSourceError(ValueError):
    """Source shape, snapshot stability or materialization budget was rejected."""


class _DefaultCellBytes:
    """sqlite3.Blob subset for default-materialized cells of added columns.

    ALTER TABLE ADD COLUMN serves the schema DEFAULT at the SQL layer for rows
    that predate the alteration, while the incremental blob handle sees the
    physically absent field as NULL. ``_open_blob`` proves the SQL-visible
    TEXT/BLOB type before falling back to this exact-PK materialization; the
    default literal is schema-bounded, so constant-memory admission is intact.
    """

    def __init__(self, value: Any):
        self._data = value.encode('utf-8') if isinstance(value, str) else bytes(value)
        self._pos = 0

    def __len__(self) -> int:
        return len(self._data)

    def __enter__(self) -> '_DefaultCellBytes':
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def seek(self, offset: int, origin: int = 0) -> None:
        if origin == 0:
            self._pos = offset
        elif origin == 1:
            self._pos += offset
        else:
            self._pos = len(self._data) + offset

    def read(self, size: int = -1) -> bytes:
        end = len(self._data) if size < 0 else min(len(self._data), self._pos + size)
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk

    def close(self) -> None:
        self._pos = 0


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value or '\x00' in value or len(value) > 256:
        raise MigrationSourceError('invalid migration SQL identifier')
    return '"' + value.replace('"', '""') + '"'


def _row_bytes(row: Sequence[Any]) -> int:
    return sum(len(value.encode('utf-8')) if isinstance(value, str)
               else len(value) if isinstance(value, bytes) else 8 for value in row)


class SQLiteMigrationSource:
    """One stable source snapshot, yielding at most one admitted row at a time."""

    def __init__(self, path: Path, *, maximum_row_bytes: int = MAX_SOURCE_ROW_BYTES):
        # The circuit breaker may widen up to the frame-coherent wide bound for
        # domains whose migration lane legitimately carries >8 MiB documents
        # (task results, compaction archives). Domain document builders keep
        # their own semantic caps; this limit only guards materialization.
        if (type(maximum_row_bytes) is not int
                or not 1 <= maximum_row_bytes <= MAX_SOURCE_WIDE_ROW_BYTES):
            raise MigrationSourceError('invalid migration row byte limit')
        path = Path(path)
        if not path.is_absolute():
            raise MigrationSourceError('migration source requires an absolute path')
        connection = open_readonly_sqlite_authority(path)
        self._connection = connection
        self._maximum_row_bytes = maximum_row_bytes
        self._closed = False
        try:
            # SQLite checks its own row allocation, rather than discovering an
            # oversized value after a fetchmany() has already filled memory.
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,
                                maximum_row_bytes + MAX_SOURCE_COLUMNS * 16)
            connection.execute(f'PRAGMA cache_size=-{MAX_SOURCE_ROW_BYTES // 1024}')
            connection.execute('PRAGMA mmap_size=0')
            connection.execute('PRAGMA temp_store=FILE')
            self._data_version = connection.execute('PRAGMA data_version').fetchone()[0]
            connection.execute('BEGIN')
            cursor = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT GLOB 'sqlite_*' ORDER BY name LIMIT ?",
                (MAX_SOURCE_TABLES + 1,),
            )
            tables = []
            for row in cursor:
                if len(tables) == MAX_SOURCE_TABLES:
                    raise MigrationSourceError('migration source table bound exceeded')
                table = row[0]
                _identifier(table)
                tables.append(table)
            self._tables = tuple(tables)
        except BaseException:
            connection.close()
            self._closed = True
            raise

    def __enter__(self) -> SQLiteMigrationSource:
        return self

    def __exit__(self, exception_type: Any, *_: Any) -> None:
        self.close(verify_unchanged=exception_type is None)

    def close(self, *, verify_unchanged: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._connection.rollback()
            if (verify_unchanged and self._connection.execute('PRAGMA data_version').fetchone()[0]
                    != self._data_version):
                raise MigrationSourceError('source changed during the pinned migration snapshot')
        finally:
            self._connection.close()

    @property
    def tables(self) -> tuple[str, ...]:
        return self._tables

    def _columns(self, table: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if self._closed:
            raise MigrationSourceError('migration source is closed')
        if table not in self._tables:
            raise MigrationSourceError('migration source table is unavailable')
        cursor = self._connection.execute(f'PRAGMA table_info({_identifier(table)})')
        names: list[str] = []
        keys: list[tuple[int, str]] = []
        for row in cursor:
            if len(names) == MAX_SOURCE_COLUMNS:
                raise MigrationSourceError('migration source column bound exceeded')
            _identifier(row['name'])
            names.append(row['name'])
            if row['pk']:
                keys.append((row['pk'], row['name']))
        return tuple(names), tuple(name for _, name in sorted(keys))

    def table_columns(self, table: str) -> tuple[str, ...]:
        """Return the bounded declared column catalog for source admission."""
        return self._columns(table)[0]

    def table_definition(self, table: str) -> dict[str, str]:
        """Read bounded SQLite engine provenance, never infer it from a name."""
        self._columns(table)
        row = self._connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None or not isinstance(row[0], str) or _row_bytes(row) > self._maximum_row_bytes:
            raise MigrationSourceError('invalid migration source table definition')
        cursor = self._connection.execute(f'PRAGMA table_list({_identifier(table)})')
        try:
            entries = cursor.fetchmany(2)
        finally:
            cursor.close()
        if len(entries) != 1 or entries[0]['schema'] != 'main':
            raise MigrationSourceError('ambiguous migration source table definition')
        return {'sql': row[0], 'kind': entries[0]['type']}

    def table_has_rows(self, table: str) -> bool:
        """Probe catalog presence without materializing any source body."""
        if self._closed:
            raise MigrationSourceError('migration source is closed')
        if table not in self._tables:
            raise MigrationSourceError('migration source table is unavailable')
        try:
            return self._connection.execute(
                f'SELECT 1 FROM {_identifier(table)} LIMIT 1').fetchone() is not None
        except sqlite3.Error:
            raise MigrationSourceError('SQLite source presence probe failed') from None

    def raw_archive_usage(self, owner_id: int, group: str | None = None):
        """Stream exact archive totals; SQLite spill work stays in temp files."""
        if self._closed or type(owner_id) is not int or owner_id <= 0:
            raise MigrationSourceError('invalid raw archive aggregate scope')
        if group not in (None, 'conversation_id', 'attempt_id'):
            raise MigrationSourceError('invalid raw archive aggregate grouping')
        if 'storage_raw_archives' not in self._tables:
            if group is None:
                yield {'count': 0, 'stored_bytes': 0}
            return
        fields = (group + ',') if group else ''
        sql = ('SELECT ' + fields + 'COUNT(*) AS count, '
               'COALESCE(SUM(stored_bytes),0) AS stored_bytes '
               'FROM storage_raw_archives WHERE user_id=?')
        if group:
            sql += ' GROUP BY ' + group + ' ORDER BY ' + group
        cursor = None
        try:
            cursor = self._connection.execute(sql, (owner_id,))
            for row in cursor:
                if any(type(row[key]) is not int or row[key] < 0
                       for key in ('count', 'stored_bytes')):
                    raise MigrationSourceError('raw archive aggregate exceeds integer bounds')
                yield dict(row)
        except sqlite3.Error:
            raise MigrationSourceError('SQLite raw archive aggregate read failed') from None
        finally:
            if cursor is not None:
                cursor.close()

    def billing_aggregate(self, user_id: str, ref_id: str | None = None) -> dict[str, int]:
        """Constant-size ledger evidence using the source's user/ref indexes."""
        if self._closed or not isinstance(user_id, str) or not user_id:
            raise MigrationSourceError('invalid billing aggregate scope')
        if 'billing_ledger' not in self._tables:
            return {'count': 0, 'total_micro': 0} if ref_id is None else {
                'count': 0, 'held_micro': 0, 'last_reserve_ts': 0}
        if ref_id is None:
            sql = ('SELECT COUNT(*) AS count, COALESCE(SUM(amount_micro),0) AS total_micro '
                   'FROM billing_ledger WHERE user_id=?')
            parameters = (user_id,)
        else:
            if not isinstance(ref_id, str) or not ref_id:
                raise MigrationSourceError('invalid billing reserve scope')
            sql = ("SELECT COUNT(*) AS count, -COALESCE(SUM(amount_micro),0) AS held_micro, "
                   "COALESCE(MAX(CASE WHEN kind='reserve' THEN ts ELSE 0 END),0) AS last_reserve_ts "
                   "FROM billing_ledger WHERE user_id=? AND ref_id=? AND ref_type='reserve' "
                   "AND kind IN ('reserve','reserve_release')")
            parameters = (user_id, ref_id)
        try:
            plan = list(self._connection.execute('EXPLAIN QUERY PLAN ' + sql, parameters))
            if (not any('SEARCH ' in row['detail'].upper() for row in plan)
                    or any('TEMP B-TREE' in row['detail'].upper() for row in plan)):
                raise MigrationSourceError('billing aggregate requires a source index')
            row = self._connection.execute(sql, parameters).fetchone()
            if any(type(value) is not int for value in row):
                raise MigrationSourceError('billing aggregate is outside integer bounds')
            return dict(row)
        except sqlite3.Error:
            raise MigrationSourceError('SQLite billing aggregate read failed') from None

    def point(
        self, table: str, columns: Sequence[str], key_columns: Sequence[str],
        key: Sequence[str | int],
    ) -> dict[str, Any] | None:
        """One indexed primary-key lookup for independent target enumeration."""
        declared, primary = self._columns(table)
        if (tuple(key_columns) != primary or not 1 <= len(primary) <= MAX_KEY_COLUMNS
                or len(key) != len(primary) or not columns
                or len(set(columns)) != len(columns)
                or any(column not in declared for column in columns)
                or any(not isinstance(value, str) and type(value) is not int for value in key)
                or _row_bytes(key) > self._maximum_row_bytes):
            raise MigrationSourceError('migration lookup requires the exact declared primary key')
        selected = ','.join(_identifier(column) for column in columns)
        where = ' AND '.join(f'{_identifier(column)}=?' for column in primary)
        sql = f'SELECT {selected} FROM {_identifier(table)} WHERE {where} LIMIT 1'
        try:
            row = self._connection.execute(sql, tuple(key)).fetchone()
            if row is None:
                return None
            if _row_bytes(row) > self._maximum_row_bytes:
                raise MigrationSourceError('migration source row exceeds its byte bound')
            return {column: row[column] for column in columns}
        except sqlite3.Error:
            raise MigrationSourceError('SQLite migration source lookup failed') from None

    @staticmethod
    def _digest_key(value: str) -> bytes:
        if (not isinstance(value, str) or len(value) != 64
                or any(character not in '0123456789abcdef' for character in value)):
            raise MigrationSourceError('invalid SHA-256 migration key')
        return bytes.fromhex(value)

    def _digest_key_columns(self, table, columns, key_column):
        declared, primary = self._columns(table)
        if (primary != (key_column,) or not columns or key_column not in columns
                or len(set(columns)) != len(columns)
                or any(column not in declared for column in columns)):
            raise MigrationSourceError('digest scan requires the exact declared primary key')
        return ','.join(_identifier(column) for column in columns)

    def _digest_key_row(self, row, columns, key_column):
        if _row_bytes(row) + 32 > self._maximum_row_bytes:
            raise MigrationSourceError('migration source row exceeds its byte bound')
        key = row[key_column]
        if not isinstance(key, bytes) or len(key) != 32:
            raise MigrationSourceError('migration source key is not a SHA-256 BLOB')
        return {column: key.hex() if column == key_column else row[column]
                for column in columns}

    def digest_key_point(self, table: str, columns: Sequence[str], key_column: str,
                         key: str) -> dict[str, Any] | None:
        """Lookup one 32-byte BLOB PK; expose its canonical hex checkpoint identity."""
        selected = self._digest_key_columns(table, columns, key_column)
        raw_key = self._digest_key(key)
        sql = (f'SELECT {selected} FROM {_identifier(table)} '
               f'WHERE {_identifier(key_column)}=? LIMIT 1')
        try:
            row = self._connection.execute(sql, (raw_key,)).fetchone()
            return None if row is None else self._digest_key_row(row, columns, key_column)
        except sqlite3.Error:
            raise MigrationSourceError('SQLite migration digest lookup failed') from None

    def digest_key_rows(self, table: str, columns: Sequence[str], key_column: str, *,
                        after: str | None = None,
                        batch_rows: int = MAX_SOURCE_BATCH_ROWS) -> Iterator[dict[str, Any]]:
        """Stream 32-byte BLOB primary keys in native index order, without HEX sorting.

        SQL compares raw bytes; only the returned identity becomes lowercase hex.
        Each cursor owns at most one bounded page and closes on abandonment. This
        also supports WITHOUT ROWID receipt tables, which cannot use blobopen.
        """
        columns = tuple(columns)
        selected = self._digest_key_columns(table, columns, key_column)
        if type(batch_rows) is not int or not 1 <= batch_rows <= MAX_SOURCE_BATCH_ROWS:
            raise MigrationSourceError('invalid digest scan batch size')
        previous = None if after is None else self._digest_key(after)
        while True:
            if self._closed:
                raise MigrationSourceError('migration source is closed')
            where = '' if previous is None else f' WHERE {_identifier(key_column)}> ?'
            sql = (f'SELECT {selected} FROM {_identifier(table)}{where} '
                   f'ORDER BY {_identifier(key_column)} LIMIT ?')
            parameters = (batch_rows,) if previous is None else (previous, batch_rows)
            cursor = None
            try:
                for step in self._connection.execute('EXPLAIN QUERY PLAN ' + sql, parameters):
                    if 'TEMP B-TREE' in step['detail'].upper():
                        raise MigrationSourceError('digest key order requires an unbounded sort')
                cursor = self._connection.execute(sql, parameters)
                count = 0
                for row in cursor:
                    document = self._digest_key_row(row, columns, key_column)
                    current = row[key_column]
                    if previous is not None and current <= previous:
                        raise MigrationSourceError('migration digest key did not advance')
                    previous = current
                    count += 1
                    yield document
                if count < batch_rows:
                    return
            except sqlite3.Error:
                raise MigrationSourceError('SQLite migration digest scan failed') from None
            finally:
                if cursor is not None:
                    cursor.close()

    @contextmanager
    def _open_blob(self, table, column, key_columns, key):
        """One exact-PK read-only handle shared by size and streaming reads."""
        declared, primary = self._columns(table)
        if (column not in declared or tuple(key_columns) != primary
                or not 1 <= len(primary) <= MAX_KEY_COLUMNS or len(key) != len(primary)
                or any(not isinstance(value, str) and type(value) is not int for value in key)
                or _row_bytes(key) > self._maximum_row_bytes):
            raise MigrationSourceError('blob lookup requires the exact declared primary key')
        rowid = next((name for name in ('rowid', '_rowid_', 'oid') if name not in declared), None)
        if rowid is None:
            raise MigrationSourceError('migration blob requires an unshadowed SQLite rowid')
        where = ' AND '.join(f'{_identifier(name)}=?' for name in primary)
        sql = (f'SELECT {rowid} AS blob_rowid, typeof({_identifier(column)}) AS value_type '
               f'FROM {_identifier(table)} WHERE {where} LIMIT 1')
        try:
            found = self._connection.execute(sql, tuple(key)).fetchone()
            if found is None:
                raise MigrationSourceError('migration blob source row is missing')
            if type(found['blob_rowid']) is not int or found['value_type'] not in {'text', 'blob'}:
                raise MigrationSourceError('migration blob source value is not TEXT/BLOB')
            try:
                handle: Any = self._connection.blobopen(
                    table, column, found['blob_rowid'], readonly=True)
            except sqlite3.Error as blob_error:
                if 'cannot open value of type null' not in str(blob_error):
                    raise
                # The SQL layer proved TEXT/BLOB above, so a physically absent
                # (pre-ALTER) field means the cell is the added column's schema
                # DEFAULT; materialize that exact value through one bounded
                # exact-PK read instead of the incremental handle.
                row = self._connection.execute(
                    f'SELECT {_identifier(column)} FROM {_identifier(table)} '
                    f'WHERE {where} LIMIT 1', tuple(key)).fetchone()
                if row is None or not isinstance(row[0], (str, bytes)):
                    raise MigrationSourceError(
                        'migration blob default materialization mismatch') from None
                handle = _DefaultCellBytes(row[0])
            with handle:
                yield handle
        except sqlite3.Error:
            raise MigrationSourceError('SQLite migration blob read failed') from None

    def cell_metadata(self, table: str, columns: Sequence[str], key_columns: Sequence[str],
                      key: Sequence[str | int]) -> list[dict[str, Any]]:
        """Inspect all cell types/scalars without loading TEXT/BLOB bodies."""
        declared, primary = self._columns(table)
        if (tuple(key_columns) != primary or not primary or len(key) != len(primary)
                or not columns or len(set(columns)) != len(columns)
                or any(column not in declared for column in columns)
                or any(not isinstance(value, str) and type(value) is not int for value in key)
                or _row_bytes(key) > self._maximum_row_bytes):
            raise MigrationSourceError('cell metadata requires the exact declared primary key')
        selected = []
        for index, column in enumerate(columns):
            quoted = _identifier(column)
            selected.extend((f'typeof({quoted}) AS t{index}',
                             f"CASE WHEN typeof({quoted}) IN ('text','blob') "
                             f' THEN NULL ELSE {quoted} END AS v{index}'))
        where = ' AND '.join(f'{_identifier(column)}=?' for column in primary)
        try:
            row = self._connection.execute(
                f'SELECT {",".join(selected)} FROM {_identifier(table)} WHERE {where} LIMIT 1',
                tuple(key)).fetchone()
            if row is None:
                raise MigrationSourceError('cell metadata source row is missing')
            result = []
            for index, column in enumerate(columns):
                kind = row[f't{index}']
                if kind in ('text', 'blob'):
                    result.append({'type': kind, 'bytes': self.blob_size(table, column, primary, key)})
                elif kind in ('integer', 'real', 'null'):
                    result.append({'type': kind, 'value': row[f'v{index}']})
                else:
                    raise MigrationSourceError('unsupported SQLite cell type')
            return result
        except sqlite3.Error:
            raise MigrationSourceError('SQLite migration cell metadata failed') from None

    def blob_slice(self, table: str, column: str, key_columns: Sequence[str],
                   key: Sequence[str | int], *, offset: int, size: int,
                   maximum_bytes: int) -> bytes:
        """Seek one bounded exact-PK slice for independent archive-part verification."""
        if (type(offset) is not int or offset < 0 or type(size) is not int
                or not 0 <= size <= min(MAX_SOURCE_CHUNK_BYTES, self._maximum_row_bytes)
                or type(maximum_bytes) is not int or not 0 <= maximum_bytes < 2**63):
            raise MigrationSourceError('invalid migration blob slice budget')
        with self._open_blob(table, column, key_columns, key) as blob:
            if len(blob) > maximum_bytes or offset + size > len(blob):
                raise MigrationSourceError('migration blob slice exceeds its domain byte budget')
            blob.seek(offset)
            value = blob.read(size)
            if len(value) != size:
                raise MigrationSourceError('migration blob slice was incomplete')
            return value

    def blob_size(self, table: str, column: str, key_columns: Sequence[str],
                  key: Sequence[str | int]) -> int:
        """Read encoded byte length without materializing even oversized TEXT.

        SQL length(TEXT) can decode the entire value and trip row admission;
        SQLite's incremental handle exposes its byte count in constant memory.
        This metadata read grants no permission to exceed a domain read budget.
        """
        with self._open_blob(table, column, key_columns, key) as blob:
            return len(blob)

    def blob_chunks(
        self, table: str, column: str, key_columns: Sequence[str],
        key: Sequence[str | int], *, maximum_bytes: int,
        chunk_bytes: int = MAX_SOURCE_CHUNK_BYTES,
    ) -> Iterator[bytes]:
        """Stream exact-PK TEXT/BLOB within explicit total and chunk budgets.

        Close abandoned iterators to release their single read-only handle.
        Ordinary row admission and source snapshot/change checks stay intact.
        """
        with self._open_blob(table, column, key_columns, key) as blob:
            if (type(maximum_bytes) is not int or not 0 <= maximum_bytes < 2**63
                    or type(chunk_bytes) is not int
                    or not 1 <= chunk_bytes <= min(MAX_SOURCE_CHUNK_BYTES, self._maximum_row_bytes)):
                raise MigrationSourceError('invalid migration blob byte budget')
            remaining = len(blob)
            if remaining > maximum_bytes:
                raise MigrationSourceError('migration blob exceeds its domain byte budget')
            while remaining:
                chunk = blob.read(min(chunk_bytes, remaining))
                if not chunk or len(chunk) > remaining:
                    raise MigrationSourceError('migration blob read was incomplete')
                remaining -= len(chunk)
                yield chunk

    def rows(
        self, table: str, columns: Sequence[str], key_columns: Sequence[str], *,
        after: Sequence[str | int] | None = None, batch_rows: int = MAX_SOURCE_BATCH_ROWS,
        key_prefix: Sequence[str | int] | None = None,
    ) -> Iterator[dict[str, Any]]:
        declared, primary = self._columns(table)
        columns, key_columns = tuple(columns), tuple(key_columns)
        if (not columns or len(set(columns)) != len(columns)
                or any(name not in declared for name in columns)
                or not 1 <= len(key_columns) <= MAX_KEY_COLUMNS
                or key_columns != primary or any(key not in columns for key in key_columns)
                or type(batch_rows) is not int or not 1 <= batch_rows <= MAX_SOURCE_BATCH_ROWS):
            raise MigrationSourceError('migration scan requires the exact declared primary key')
        if after is not None and (
                len(after) != len(key_columns)
                or any(not isinstance(value, str) and type(value) is not int for value in after)
                or any(type(value) is int and not -(2**63) <= value <= 2**63 - 1 for value in after)
                or _row_bytes(after) > self._maximum_row_bytes):
            raise MigrationSourceError('invalid migration resume key')
        prefix = () if key_prefix is None else tuple(key_prefix)
        if key_prefix is not None and (
                not 1 <= len(prefix) <= len(key_columns)
                or any(not isinstance(value, str) and type(value) is not int for value in prefix)
                or any(type(value) is int and not -(2**63) <= value <= 2**63 - 1 for value in prefix)
                or _row_bytes(prefix) > self._maximum_row_bytes
                or (after is not None and tuple(after[:len(prefix)]) != prefix)):
            raise MigrationSourceError('invalid migration primary key prefix')
        last = None if after is None else tuple(after)
        selected = ','.join(_identifier(column) for column in columns)
        ordered = ','.join(_identifier(column) for column in key_columns)
        placeholders = ','.join('?' for _ in key_columns)
        while True:
            clauses = [f'{_identifier(column)}=?' for column in key_columns[:len(prefix)]]
            if last is not None:
                clauses.append(f'({ordered}) > ({placeholders})')
            where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
            parameters = (*prefix, batch_rows) if last is None else (*prefix, *last, batch_rows)
            sql = (f'SELECT {selected} FROM {_identifier(table)}{where} '
                   f'ORDER BY {ordered} LIMIT ?')
            try:
                plan = self._connection.execute('EXPLAIN QUERY PLAN ' + sql, parameters)
                for step in plan:
                    if 'TEMP B-TREE' in step['detail'].upper():
                        raise MigrationSourceError('migration key order requires an unbounded sort')
                cursor = self._connection.execute(sql, parameters)
                count = 0
                for row in cursor:
                    if _row_bytes(row) > self._maximum_row_bytes:
                        raise MigrationSourceError('migration source row exceeds its byte bound')
                    current = tuple(row[key] for key in key_columns)
                    if (current == last or any(not isinstance(value, str) and type(value) is not int
                                               for value in current)):
                        raise MigrationSourceError('migration primary key is nullable or unsupported')
                    count += 1
                    last = current
                    yield {column: row[column] for column in columns}
            except sqlite3.DataError:
                raise MigrationSourceError('migration source row exceeds SQLite admission') from None
            except sqlite3.Error:
                raise MigrationSourceError('SQLite migration source read failed') from None
            if count < batch_rows:
                return

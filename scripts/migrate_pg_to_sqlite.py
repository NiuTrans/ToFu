#!/usr/bin/env python3
"""Snapshot PostgreSQL into a new, verified SQLite authority file.

The source is opened REPEATABLE READ + READ ONLY and is never mutated.  The
target path must not exist: an interrupted migration is discarded and rerun,
while the PG authority remains untouched.  Every portable source column is
copied, including rows from retired plugin tables that a fresh install no
longer creates.  PostgreSQL-only derived columns (currently ``tsvector``) are
rebuilt using SQLite's native index and recorded explicitly in the report.

Acceptance is table-local and fail-closed: source/target row counts plus two
independent commutative 256-bit accumulators must match before the report can
say ``verified``.  The target is then closed, reopened read-only, checksummed a
second time, and subjected to ``foreign_key_check`` + ``integrity_check``.

Typical full migration (writes only the NEW file and report)::

    python scripts/migrate_pg_to_sqlite.py \
      --postgres-dsn-file /run/secrets/tofu-postgres-dsn \
      --target data/tofu.db.pg-migration \
      --report data/tofu.db.pg-migration.report.json

A bounded live smoke can select representative tables::

    python scripts/migrate_pg_to_sqlite.py --target data/tofu-smoke.db \
      --table users --table error_resolutions --table trading_sim_sessions
"""

from __future__ import annotations

import argparse
import ctypes
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import time
from typing import Iterable, Sequence
from urllib.parse import parse_qs, urlsplit

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from lib.storage_sidecar import offline_maintenance as _PG_TOOLING
from lib.storage_sidecar import offline_maintenance as _SQLITE_TOOLING


_MODULUS = 1 << 256
_DERIVED_PG_TYPES = frozenset({'tsvector'})
_MALLOC_TRIM_UNSET = object()
_malloc_trim_fn = _MALLOC_TRIM_UNSET
_MAX_SECRET_BYTES = 16 * 1024
_TYPE_MAP = {
    'int2': 'INTEGER',
    'int4': 'INTEGER',
    'int8': 'INTEGER',
    'bool': 'INTEGER',
    'float4': 'REAL',
    'float8': 'REAL',
    'bytea': 'BLOB',
    'json': 'TEXT',
    'jsonb': 'TEXT',
    'text': 'TEXT',
    'varchar': 'TEXT',
    'bpchar': 'TEXT',
    'timestamptz': 'TEXT',
    'timestamp': 'TEXT',
    'date': 'TEXT',
    'uuid': 'TEXT',
}


@contextmanager
def _migration_lock(target: str):
    """Prevent two full-copy processes from saturating one installation."""
    lock_path = _PROJECT_ROOT / 'data' / '.pg_to_sqlite_migration.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('a+', encoding='utf-8')
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, BlockingIOError, OSError) as exc:
            raise RuntimeError(
                'another PostgreSQL→SQLite migration is already running; '
                f'refusing concurrent copy (lock={lock_path})') from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            'pid': os.getpid(),
            'target': str(target),
            'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        }, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _require_project_data_path(path: Path, label: str) -> None:
    data_root = (_PROJECT_ROOT / 'data').resolve()
    try:
        path.resolve().relative_to(data_root)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f'{label} must stay inside the project data directory '
            f'{data_root}: {path}') from exc


def _sqlite_type(udt_name: str) -> str:
    """Map the small set of live PG types to stable SQLite affinities."""
    try:
        return _TYPE_MAP[udt_name]
    except KeyError as exc:
        raise RuntimeError(
            f'unsupported PostgreSQL type {udt_name!r}; refusing a lossy copy') from exc


def _convert_value(value, udt_name: str):
    """Convert a PG value into the exact Python type SQLite will read back."""
    if value is None:
        return None
    affinity = _sqlite_type(udt_name)
    if affinity == 'INTEGER':
        return int(value)
    if affinity == 'REAL':
        return float(value)
    if affinity == 'BLOB':
        return bytes(value)
    if isinstance(value, (dt.datetime, dt.date)):
        # ISO is lossless for the one live timestamptz column and is directly
        # comparable after SQLite returns it as TEXT.
        return value.isoformat(sep=' ') if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, str):
        return value
    # JSON adapters are registered to return raw text, but keep this defensive
    # path for driver/version drift.
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'))
    return str(value)


def _encode_value(value) -> bytes:
    if value is None:
        return b'N'
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        payload = str(value).encode('ascii')
        tag = b'I'
    elif isinstance(value, float):
        # Binary IEEE-754 representation makes NaN/inf and negative zero
        # deterministic; both drivers expose the value as Python float.
        payload = struct.pack('!d', value)
        tag = b'F'
    elif isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        tag = b'B'
    else:
        payload = str(value).encode('utf-8', errors='surrogatepass')
        tag = b'T'
    return tag + len(payload).to_bytes(8, 'big') + payload


class RowDigest:
    """Order-independent, duplicate-sensitive table checksum.

    XOR catches bit differences cheaply; modular SUM preserves multiplicity
    (two equal rows cancel under XOR but not SUM).  Combined with the exact row
    count, accidental equality is computationally negligible while avoiding a
    huge ORDER BY on the 11-million-row task_events table.
    """

    __slots__ = ('count', 'xor256', 'sum256', 'canonical_bytes')

    def __init__(self):
        self.count = 0
        self.xor256 = 0
        self.sum256 = 0
        self.canonical_bytes = 0

    def add(self, row: Sequence) -> None:
        encoded = b''.join(_encode_value(v) for v in row)
        number = int.from_bytes(hashlib.sha256(encoded).digest(), 'big')
        self.count += 1
        self.xor256 ^= number
        self.sum256 = (self.sum256 + number) % _MODULUS
        self.canonical_bytes += len(encoded)

    def as_dict(self) -> dict:
        return {
            'rows': self.count,
            'xor_sha256': f'{self.xor256:064x}',
            'sum_sha256': f'{self.sum256:064x}',
            'canonical_bytes': self.canonical_bytes,
        }

    def signature(self) -> tuple[int, int, int]:
        return self.count, self.xor256, self.sum256


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


def _read_dsn_secret(path: str | os.PathLike[str]) -> str:
    """Read the external PostgreSQL authority from one bounded secret file."""
    secret_path = Path(path)
    if not secret_path.is_absolute():
        raise RuntimeError(
            'PostgreSQL DSN secret file must be an absolute path')
    try:
        metadata = secret_path.stat()
    except OSError as exc:
        raise RuntimeError('PostgreSQL DSN secret file is unreadable') from exc
    if not secret_path.is_file() or not 0 < metadata.st_size <= _MAX_SECRET_BYTES:
        raise RuntimeError(
            'PostgreSQL DSN secret file must contain 1..16384 bytes')
    try:
        value = secret_path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            'PostgreSQL DSN secret file is not readable UTF-8') from exc
    if not value or '\x00' in value:
        raise RuntimeError('PostgreSQL DSN secret file is invalid')
    if not _dsn_uses_verified_tls(value):
        raise RuntimeError(
            'external PostgreSQL DSN requires sslmode=verify-full')
    return value


def _source_quiescence_state(conn, *, exclude_pids=()) -> dict:
    """Observe fresh-session policy and peer clients before forcing self RO."""
    previous = conn.autocommit
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute('SHOW default_transaction_read_only')
            read_only = str(cur.fetchone()[0]).strip().lower() == 'on'
            cur.execute('SELECT pg_backend_pid()')
            own_pid = int(cur.fetchone()[0])
            excluded = {own_pid, *(int(pid) for pid in exclude_pids)}
            cur.execute("""
                SELECT pid, application_name, state
                  FROM pg_stat_activity
                 WHERE datname=current_database()
                   AND backend_type='client backend'
                 ORDER BY pid
            """)
            peers = [tuple(row) for row in cur.fetchall()
                     if int(row[0]) not in excluded]
            return {
                'default_transaction_read_only': read_only,
                'backend_pid': own_pid,
                'other_client_sessions': len(peers),
                'peer_sample': peers[:10],
            }
    finally:
        conn.autocommit = previous


def _probe_source_quiescence(dsn: str, *, exclude_pids=()) -> dict:
    """Recheck source policy and peers on a fresh completion connection."""
    probe = _PG_TOOLING.open_postgres_tool_connection(
        dsn, connect_timeout=10,
        application_name='tofu-pg-to-sqlite-quiescence-check')
    try:
        return _source_quiescence_state(probe, exclude_pids=exclude_pids)
    finally:
        probe.close()


def _release_source_snapshot(conn) -> str:
    """End a consumed source snapshot without letting cleanup mask parity.

    Once every source cursor has reached EOF and its target digest matches,
    the snapshot has served its purpose.  Release it before index rebuilds and
    cross-reopen verification; those destination-only stages can take hours
    and must not retain PostgreSQL vacuum horizons.  A server restart after
    the final fetch already released the backend, so rollback/close errors at
    this boundary are cleanup diagnostics rather than data-copy failures.
    """
    if getattr(conn, 'closed', True):
        return 'already_closed'
    outcome = 'rolled_back'
    try:
        _PG_TOOLING.release_postgres_tool_snapshot(conn)
    except Exception as exc:
        outcome = f'already_disconnected:{type(exc).__name__}'
    try:
        if not getattr(conn, 'closed', True):
            conn.close()
    except Exception as exc:
        if outcome == 'rolled_back':
            outcome = f'close_failed:{type(exc).__name__}'
    return outcome


def _probe_source_quiescence_at_end(dsn: str, *, exclude_pids=(),
                                    required: bool) -> dict:
    """Probe final PG policy, failing hard only for a cutover assertion."""
    try:
        return _probe_source_quiescence(dsn, exclude_pids=exclude_pids)
    except Exception as exc:
        if required:
            raise
        # Online snapshot parity is already proven from the original
        # REPEATABLE READ transaction. A later PG outage is operational
        # evidence worth recording, but cannot make that byte proof false.
        return {
            'default_transaction_read_only': None,
            'backend_pid': None,
            'other_client_sessions': None,
            'peer_sample': [],
            'probe_error': f'{type(exc).__name__}: {str(exc).splitlines()[0]}',
        }


def _connect_source(args, dsn: str):
    from psycopg2 import extras

    conn = _PG_TOOLING.open_postgres_tool_connection(
        dsn,
        connect_timeout=args.connect_timeout,
        application_name='tofu-pg-to-sqlite',
    )
    source_label = {'dsn': '[redacted]'}

    # Observe the policy before set_session(readonly=True) changes this
    # connection. A read-only migration connection does not prove the serving
    # application was quiesced; cutover readiness requires the fresh-session
    # default itself to be read-only at both ends of the copy.
    source_quiescence = _source_quiescence_state(conn)

    # Keep JSON/JSONB in PostgreSQL's canonical textual representation.  The
    # SQLite schema stores these columns as TEXT and the checksum then compares
    # exact bytes rather than Python dict insertion order.
    extras.register_default_json(conn, loads=lambda raw: raw)
    extras.register_default_jsonb(conn, loads=lambda raw: raw)
    conn.set_session(isolation_level='REPEATABLE READ', readonly=True,
                     autocommit=False)
    # The consistency snapshot intentionally spans source streaming *and*
    # destination verification.  Large target scans can leave PG momentarily
    # idle in transaction for longer than the application's safety timeout;
    # disable it only for this read-only migration transaction.
    with conn.cursor() as cur:
        cur.execute('SET LOCAL idle_in_transaction_session_timeout = 0')
        cur.execute('SET LOCAL statement_timeout = 0')
    return conn, source_label, source_quiescence


def _migration_verdict(*, is_full: bool, source_quiesced: bool,
                       read_only_at_start: bool,
                       read_only_at_end: bool,
                       peer_sessions_at_start: int = 0,
                       peer_sessions_at_end: int = 0) -> tuple[str, bool, str]:
    """Return report status/cutover flag without conflating snapshot parity.

    Row digests on a REPEATABLE READ snapshot prove conversion fidelity. They
    do not prove the live source stopped changing while a multi-hour copy ran.
    Only an explicit maintenance-window assertion plus a server-enforced
    read-only default at both boundaries may produce a cutover-ready report.
    """
    if not is_full:
        return 'partial_verified', False, 'only_selected_tables_were_copied'
    if not source_quiesced:
        return ('snapshot_verified', False,
                'source_writes_were_not_declared_quiesced')
    if not read_only_at_start or not read_only_at_end:
        raise RuntimeError(
            '--source-quiesced requires PostgreSQL '
            'default_transaction_read_only=on for fresh sessions at both '
            'the start and end of migration')
    if peer_sessions_at_start or peer_sessions_at_end:
        raise RuntimeError(
            '--source-quiesced requires zero other PostgreSQL client '
            'sessions at both migration boundaries; observed '
            f'start={peer_sessions_at_start}, end={peer_sessions_at_end}')
    return 'verified', True, 'source_quiesced_and_server_default_read_only'


def _initialize_target(target: Path) -> None:
    """Create the current Sidecar SQLite schema in a disposable child.

    The Sidecar schema is the storage authority.  Bootstrapping it directly
    avoids legacy backend discovery and all retired PostgreSQL configuration.
    The child boundary still returns imported schema/native memory before the
    parent starts a potentially multi-hour copy.
    """
    if target.exists():
        raise FileExistsError(
            f'target already exists: {target}; choose a new path (never overwrite)')
    target.parent.mkdir(parents=True, exist_ok=True)
    child_env = os.environ.copy()
    for name in tuple(child_env):
        if name.startswith(('TOFU_PG_', 'TOFU_DB_', 'CHATUI_DB_')):
            child_env.pop(name, None)
    for name in (
        'TOFU_REQUIRE_PG', 'TOFU_STORAGE_MODE', 'CHATUI_STORAGE_MODE',
        'TOFU_DISTRIBUTED_PREVIEW_MODE',
        'TOFU_POSTGRES_DSN_FILE', 'TOFU_REDIS_URL_FILE', 'TOFU_REPLICA_ID',
    ):
        child_env.pop(name, None)
    child_env['TOFU_DEPLOYMENT_MODE'] = 'personal'
    child_env['TOFU_PROCESS_ROLE'] = 'all'
    # Optional schema plugins can transitively import numerical/ONNX stacks.
    # Their default is one native worker per visible CPU (100+ threads here),
    # even though schema initialization performs no inference or matrix work.
    # Keep the one-shot installer lightweight without changing server defaults.
    for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS',
                 'ORT_NUM_THREADS', 'TOFU_ONNX_THREADS'):
        child_env[name] = '1'
    old_pythonpath = child_env.get('PYTHONPATH', '')
    child_env['PYTHONPATH'] = str(_PROJECT_ROOT) + (
        os.pathsep + old_pythonpath if old_pythonpath else '')
    child_code = """
import sqlite3
import sys
from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from lib.storage_sidecar.schema import initialize_schema, validate_schema_version

target = sys.argv[1]
connection = sqlite3.connect(target, isolation_level=None)
connection.row_factory = sqlite3.Row
try:
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('BEGIN IMMEDIATE')
    initialize_schema(SQLiteSession(connection))
    connection.commit()
    validate_schema_version(SQLiteSession(connection))
except BaseException:
    connection.rollback()
    raise
finally:
    connection.close()
"""
    try:
        result = subprocess.run(
            [sys.executable, '-c', child_code, str(target)], cwd=str(_PROJECT_ROOT),
            env=child_env, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('SQLite schema bootstrap timed out after 600s') from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip()[-4000:]
        raise RuntimeError(
            f'SQLite schema bootstrap failed rc={result.returncode}: {detail}')
    if not target.is_file():
        raise RuntimeError('SQLite schema bootstrap returned without target file')
    check = _SQLITE_TOOLING.open_sqlite_candidate_connection(
        target,
        canonical_path=_PROJECT_ROOT / 'data' / 'tofu.db',
        writable=False)
    try:
        integrity = check.execute('PRAGMA quick_check').fetchone()[0]
        table_count = int(check.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
    finally:
        check.close()
    if integrity != 'ok' or table_count == 0:
        raise RuntimeError(
            f'SQLite schema bootstrap validation failed: '
            f'quick_check={integrity!r}, tables={table_count}')


def _source_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema='public' AND table_type='BASE TABLE'
             ORDER BY table_name
        """)
        return [row[0] for row in cur.fetchall()]


def _source_columns(conn, table: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT column_name, data_type, udt_name, is_nullable,
                   column_default, ordinal_position
              FROM information_schema.columns
             WHERE table_schema='public' AND table_name=%s
             ORDER BY ordinal_position
        """, (table,))
        columns = [
            {
                'name': row[0], 'data_type': row[1], 'udt_name': row[2],
                'nullable': row[3] == 'YES', 'default': row[4],
                'ordinal': int(row[5]),
            }
            for row in cur.fetchall()
        ]
        cur.execute("""
            SELECT kcu.column_name
              FROM information_schema.table_constraints tc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name=tc.constraint_name
               AND kcu.constraint_schema=tc.constraint_schema
             WHERE tc.table_schema='public' AND tc.table_name=%s
               AND tc.constraint_type='PRIMARY KEY'
             ORDER BY kcu.ordinal_position
        """, (table,))
        primary_key = [row[0] for row in cur.fetchall()]
    for column in columns:
        column['pk'] = column['name'] in primary_key
    return columns


def _target_columns(db, table: str) -> list[str]:
    rows = db.execute(f'PRAGMA table_info({_quote_ident(table)})').fetchall()
    return [row[1] for row in rows]


def _create_archive_table(db, table: str, columns: list[dict]) -> None:
    """Create a compatibility table for retired plugin/PG-only history."""
    _SQLITE_TOOLING.create_portable_archive_table(
        db,
        table,
        columns,
        derived_types=_DERIVED_PG_TYPES,
        type_map=_TYPE_MAP,
    )


def _ensure_portable_columns(db, table: str, columns: list[dict]) -> list[dict]:
    """Ensure all non-derived source columns have a place in the target."""
    target_columns = set(_target_columns(db, table))
    if not target_columns:
        _create_archive_table(db, table, columns)
        target_columns = set(_target_columns(db, table))
    for column in columns:
        if column['udt_name'] in _DERIVED_PG_TYPES:
            continue
        if column['name'] not in target_columns:
            # Preserve historical columns removed from the current runtime
            # schema.  Nullable is intentional: this is an archive extension,
            # and current code no longer writes it.
            _SQLITE_TOOLING.add_portable_archive_column(
                db, table, column, type_map=_TYPE_MAP)
            target_columns.add(column['name'])
    return [c for c in columns
            if c['udt_name'] not in _DERIVED_PG_TYPES
            and c['name'] in target_columns]


def _recommended_fetch_rows(source, table: str, requested: int,
                            batch_bytes: int) -> int:
    with source.cursor() as cur:
        cur.execute("""
            SELECT pg_total_relation_size(c.oid), GREATEST(c.reltuples::bigint, 1)
              FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
             WHERE n.nspname='public' AND c.relname=%s
        """, (table,))
        row = cur.fetchone()
    if not row:
        return requested
    average = max(1, int(row[0]) // int(row[1]))
    # pg_total_relation_size reflects compressed/toasted storage. Fetching a
    # batch detoasts every JSON value, so 64 "1 MiB" stored conversation rows
    # can actually occupy many GiB in libpq/Python. Apply a second row-count
    # ceiling for wide relations; the small extra commits are far cheaper than
    # an OOM during installation.
    if average >= 512 * 1024:
        wide_cap = 8
    elif average >= 64 * 1024:
        wide_cap = 64
    else:
        wide_cap = requested
    return max(1, min(requested, wide_cap, batch_bytes // average))


def _copy_table(source, target, table: str, columns: list[dict],
                batch_rows: int, batch_bytes: int) -> tuple[RowDigest, dict]:
    from psycopg2 import sql

    names = [column['name'] for column in columns]
    select_sql = sql.SQL('SELECT {} FROM {}').format(
        sql.SQL(', ').join(sql.Identifier(name) for name in names),
        sql.Identifier(table),
    )
    insert_sql = (
        f'INSERT INTO {_quote_ident(table)} ('
        + ', '.join(_quote_ident(name) for name in names)
        + ') VALUES (' + ','.join('?' for _ in names) + ')')
    fetch_rows = _recommended_fetch_rows(source, table, batch_rows, batch_bytes)
    digest = RowDigest()
    started = time.monotonic()
    batches = 0
    commits = 0
    checkpoints = 0
    checkpoint_at = max(batch_bytes * 4, 64 * 1024 * 1024)
    last_checkpoint_bytes = 0
    with _SQLITE_TOOLING.write_transaction(target):
        target.execute(f'DELETE FROM {_quote_ident(table)}')

    cursor_name = 'tofu_migrate_' + hashlib.sha1(table.encode()).hexdigest()[:16]
    with source.cursor(name=cursor_name) as cur:
        cur.itersize = fetch_rows
        cur.execute(select_sql)
        while True:
            rows = cur.fetchmany(fetch_rows)
            if not rows:
                break
            # Stream converted tuples into sqlite3.executemany instead of
            # materialising a second wide-row list. On toasted conversation
            # payloads, the PG relation-size estimate can be much smaller than
            # the detoasted JSON text; keeping raw + converted batches at once
            # caused multi-GiB migration RSS despite a 64 MiB nominal cap.
            def _converted_rows():
                for row in rows:
                    converted = tuple(
                        _convert_value(value, column['udt_name'])
                        for value, column in zip(row, columns))
                    digest.add(converted)
                    yield converted

            with _SQLITE_TOOLING.write_transaction(target):
                target.executemany(insert_sql, _converted_rows())
            # Drop both the raw batch and the closure that captured it before
            # asking libc to return free arenas. Merely calling malloc_trim
            # while ``rows`` is still referenced cannot lower the peak/RSS.
            del _converted_rows
            del rows
            batches += 1
            # The destination is a disposable new file, so table-level
            # atomicity buys nothing: an interrupted run is discarded.  A
            # commit per bounded batch prevents one 10+ GiB transaction and
            # keeps SQLite's single writer slot short-lived.
            commits += 1
            if digest.canonical_bytes - last_checkpoint_bytes >= checkpoint_at:
                target.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                checkpoints += 1
                last_checkpoint_bytes = digest.canonical_bytes
            if batches % 25 == 0:
                print(f'  {table}: {digest.count:,} rows, '
                      f'{digest.canonical_bytes / (1024 ** 2):.1f} MiB canonical',
                      flush=True)
            # libpq detoasts wide JSON values into native allocations. Python
            # releases each batch, but glibc can retain those arenas for the
            # rest of a multi-hour migration (observed 12–14 GiB RSS). Return
            # free heap pages after wide batches; unsupported libc/platforms
            # simply skip the optional trim.
            if fetch_rows <= 64:
                _trim_process_heap()
    return digest, {
        'duration_s': round(time.monotonic() - started, 3),
        'fetch_rows': fetch_rows,
        'batches': batches,
        'commits': commits,
        'checkpoints': checkpoints,
    }


def _trim_process_heap() -> bool:
    """Best-effort release of freed glibc arenas after detoasted wide rows."""
    global _malloc_trim_fn
    try:
        if _malloc_trim_fn is _MALLOC_TRIM_UNSET:
            trim = ctypes.CDLL(None).malloc_trim
            trim.argtypes = [ctypes.c_size_t]
            trim.restype = ctypes.c_int
            _malloc_trim_fn = trim
        if _malloc_trim_fn is None:
            return False
        return bool(_malloc_trim_fn(0))
    except (AttributeError, OSError, TypeError, ValueError):
        _malloc_trim_fn = None
        return False


def _recommended_target_fetch_rows(db, table: str, expected_rows: int | None,
                                   requested: int, batch_bytes: int) -> int:
    """Bound SQLite verification batches using the *actual target* size.

    Source ``pg_total_relation_size`` is a useful copy hint but TOAST makes it
    underestimate the Python strings returned for wide JSON rows. The target
    has already materialized those strings, so SQLite's dbstat page total is a
    much better verification-scan estimate. Fail closed to 64 rows if dbstat
    is unavailable: verification is allowed to take longer, never to consume
    tens of GiB merely because ``fetchmany(10000)`` met a wide table.
    """
    average = None
    if expected_rows and expected_rows > 0:
        try:
            row = db.execute(
                'SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name=?',
                (table,),
            ).fetchone()
            stored_bytes = int(row[0]) if row else 0
            if stored_bytes > 0:
                average = max(1, stored_bytes // expected_rows)
        except Exception:
            average = None
    if average is None:
        return max(1, min(requested, 64))
    if average >= 512 * 1024:
        wide_cap = 8
    elif average >= 64 * 1024:
        wide_cap = 64
    else:
        wide_cap = requested
    return max(1, min(requested, wide_cap, batch_bytes // average))


def _digest_target(db, table: str, columns: list[dict],
                   fetch_rows: int = 10_000, *, expected_rows: int | None = None,
                   batch_bytes: int = 64 * 1024 * 1024) -> RowDigest:
    names = [column['name'] for column in columns]
    query = ('SELECT ' + ', '.join(_quote_ident(name) for name in names)
             + f' FROM {_quote_ident(table)}')
    digest = RowDigest()
    fetch_rows = _recommended_target_fetch_rows(
        db, table, expected_rows, fetch_rows, batch_bytes)
    cur = db.execute(query)
    while True:
        rows = cur.fetchmany(fetch_rows)
        if not rows:
            break
        for row in rows:
            # SQLite affinities already return the canonical Python types.
            digest.add(tuple(row))
    return digest


def _rebuild_derived(db) -> None:
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if 'conversations_fts' in tables and 'conversations' in tables:
        with _SQLITE_TOOLING.write_transaction(db):
            db.execute('DELETE FROM conversations_fts')
            db.execute("""
                INSERT INTO conversations_fts(rowid, search_text)
                SELECT rowid, search_text FROM conversations
                 WHERE search_text IS NOT NULL AND search_text <> ''
            """)


def _verify_reopened_target(target_path: Path,
                            table_columns: dict[str, list[dict]],
                            report: dict, batch_bytes: int) -> str:
    """Reopen the finished file read-only and verify every table from disk."""
    reopened = _SQLITE_TOOLING.open_sqlite_candidate_connection(
        target_path,
        canonical_path=_PROJECT_ROOT / 'data' / 'tofu.db',
        writable=False)
    try:
        total = len(table_columns)
        for position, (table, columns) in enumerate(
                table_columns.items(), start=1):
            started = time.monotonic()
            expected = report['tables'][table]['source']
            digest = _digest_target(
                reopened, table, columns,
                expected_rows=int(expected['rows']),
                batch_bytes=batch_bytes)
            got = digest.as_dict()
            if (got['rows'], got['xor_sha256'], got['sum_sha256']) != (
                    expected['rows'], expected['xor_sha256'], expected['sum_sha256']):
                raise RuntimeError(
                    f'cross-reopen verification failed for table {table}')
            print(
                f'  reopen {position}/{total}: {table} '
                f'({time.monotonic() - started:.1f}s)',
                flush=True,
            )
        reopen_integrity = reopened.execute(
            'PRAGMA integrity_check').fetchone()[0]
        if reopen_integrity != 'ok':
            raise RuntimeError(
                f'cross-reopen integrity_check failed: {reopen_integrity}')
        return 'ok'
    finally:
        reopened.close()


def _suspend_nonunique_indexes(db) -> list[tuple[str, str]]:
    """Drop rebuildable secondary indexes during the bulk load.

    Primary-key/UNIQUE autoindexes remain active, so uniqueness is still an
    online invariant.  Rebuilding ordinary indexes once after loading avoids
    millions of random B-tree updates on the shared project filesystem.
    """
    return _SQLITE_TOOLING.suspend_nonunique_indexes(db)


def _restore_indexes(db, indexes: list[tuple[str, str]]) -> None:
    """Rebuild disposable secondary indexes with bounded WAL and progress.

    A single transaction across every index can leave tens of GiB in the WAL
    and provides no useful atomicity: any failed migration candidate is
    discarded wholesale.  Commit and checkpoint each index so a small-host
    installation needs space for only the largest individual index, not all
    indexes combined.
    """
    total = len(indexes)
    for position, (name, ddl) in enumerate(indexes, start=1):
        started = time.monotonic()
        _SQLITE_TOOLING.restore_index(db, ddl)
        checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint and int(checkpoint[0]) != 0:
            raise RuntimeError(
                f'index checkpoint remained busy after {name}: {checkpoint}')
        # Index creation can leave a large pager cache plus freed native sorter
        # arenas resident.  The candidate is the only connection at this point,
        # so releasing both after every index is safe and bounds cumulative RSS
        # to the largest individual index rather than the whole index set.
        db.execute('PRAGMA shrink_memory')
        _trim_process_heap()
        print(
            f'  index {position}/{total}: {name} '
            f'({time.monotonic() - started:.1f}s)',
            flush=True,
        )


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.tmp-{os.getpid()}')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Some network filesystems reject directory fsync. The report itself
        # has still been file-fsynced and atomically renamed.
        pass


def _fsync_database(path: Path) -> None:
    """Make the verified, checkpointed destination durable before sign-off."""
    with path.open('rb') as handle:
        os.fsync(handle.fileno())
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _remove_closed_sqlite_sidecars(path: Path) -> list[str]:
    """Remove WAL-derived files only after every candidate handle is closed.

    A read-only WAL reopen commonly leaves a 32 KiB ``-shm`` file even when
    the WAL is empty.  SHM is derived coordination state, but the cutover gate
    deliberately rejects every non-empty sidecar.  Refuse to remove anything
    if WAL still contains bytes; that could discard committed data.  With an
    empty/absent WAL and no open handles, both files are safely regenerable.
    """
    wal = Path(str(path) + '-wal')
    shm = Path(str(path) + '-shm')
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError(
            f'candidate retains a non-empty WAL after close: {wal}')
    removed = []
    for sidecar in (wal, shm):
        try:
            sidecar.unlink()
            removed.append(sidecar.name)
        except FileNotFoundError:
            pass
    if removed:
        try:
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass
    return removed


def migrate(args) -> dict:
    target_path = Path(args.target).resolve()
    report_path = Path(args.report or (str(target_path) + '.report.json')).resolve()
    if report_path == target_path:
        raise ValueError('report path must differ from target database path')
    _require_project_data_path(target_path, 'target')
    _require_project_data_path(report_path, 'report')

    # Index rebuilds over 10M+ event rows can exceed small-host RAM when
    # temp_store=MEMORY. Force SQLite spill files beside the candidate: bounded
    # memory, maximum project-path permissions, and no /tmp durability breach.
    target_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ['SQLITE_TMPDIR'] = str(target_path.parent)

    dsn = _read_dsn_secret(args.postgres_dsn_file)
    _initialize_target(target_path)
    source, source_label, source_quiescence_at_start = _connect_source(args, dsn)
    source_dsn = source.dsn
    source_read_only_at_start = source_quiescence_at_start[
        'default_transaction_read_only']
    if args.source_quiesced and (
            not source_read_only_at_start
            or source_quiescence_at_start['other_client_sessions']):
        source.close()
        raise RuntimeError(
            '--source-quiesced was requested but PostgreSQL is not isolated: '
            'fresh sessions must be default_transaction_read_only=on and '
            'there must be zero other client sessions; refusing to create a '
            'misleading cutover report')
    report = {
        'version': 1,
        'status': 'running',
        'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'source': source_label,
        'source_quiescence': {
            'operator_declared': bool(args.source_quiesced),
            'default_transaction_read_only_at_start':
                source_read_only_at_start,
            'other_client_sessions_at_start':
                source_quiescence_at_start['other_client_sessions'],
            'peer_sample_at_start':
                source_quiescence_at_start['peer_sample'],
        },
        'target': str(target_path),
        'selected_tables': args.tables or 'all',
        'tables': {},
    }
    _write_report(report_path, report)

    target = _SQLITE_TOOLING.open_sqlite_candidate_connection(
        target_path,
        canonical_path=_PROJECT_ROOT / 'data' / 'tofu.db',
        writable=True)
    target.execute('PRAGMA foreign_keys=OFF')
    target.execute('PRAGMA journal_mode=WAL')
    target.execute('PRAGMA synchronous=OFF')
    target.execute('PRAGMA wal_autocheckpoint=0')
    target.execute('PRAGMA busy_timeout=1000')
    target.execute('PRAGMA locking_mode=EXCLUSIVE')
    target.execute('PRAGMA temp_store=FILE')
    target.execute('PRAGMA cache_size=-262144')
    target.execute('PRAGMA mmap_size=0')
    target.execute('PRAGMA threads=1')
    suspended_indexes = _suspend_nonunique_indexes(target)
    report['bulk_load'] = {
        'suspended_nonunique_indexes': len(suspended_indexes),
        'batch_rows_cap': args.batch_rows,
        'batch_bytes_mib': args.batch_bytes_mib,
        'temp_store': 'FILE',
        'mmap_size': 0,
        'sqlite_threads': 1,
        'sqlite_tmpdir': str(target_path.parent),
    }
    _write_report(report_path, report)

    table_columns: dict[str, list[dict]] = {}
    try:
        with source.cursor() as cur:
            cur.execute('SELECT txid_current_snapshot(), version()')
            snapshot, pg_version = cur.fetchone()
        report['source_snapshot'] = snapshot
        report['source_version'] = pg_version

        all_tables = _source_tables(source)
        selected = sorted(set(args.tables)) if args.tables else all_tables
        missing = sorted(set(selected) - set(all_tables))
        if missing:
            raise RuntimeError(f'source tables do not exist: {missing}')

        for index, table in enumerate(selected, 1):
            print(f'[{index}/{len(selected)}] copying {table}', flush=True)
            source_columns = _source_columns(source, table)
            with _SQLITE_TOOLING.write_transaction(target):
                portable = _ensure_portable_columns(
                    target, table, source_columns)
            table_columns[table] = portable
            skipped = [c['name'] for c in source_columns if c not in portable]
            source_digest, metrics = _copy_table(
                source, target, table, portable,
                args.batch_rows, args.batch_bytes_mib * 1024 * 1024)
            target_digest = _digest_target(
                target, table, portable,
                expected_rows=source_digest.count,
                batch_bytes=args.batch_bytes_mib * 1024 * 1024)
            equal = source_digest.signature() == target_digest.signature()
            table_report = {
                'status': 'verified' if equal else 'mismatch',
                'columns': [c['name'] for c in portable],
                'skipped_derived_columns': skipped,
                'source': source_digest.as_dict(),
                'target': target_digest.as_dict(),
                **metrics,
            }
            report['tables'][table] = table_report
            _write_report(report_path, report)
            if not equal:
                raise RuntimeError(f'row verification failed for table {table}')
            target.execute('PRAGMA wal_checkpoint(TRUNCATE)')

        report['source_snapshot_release'] = _release_source_snapshot(source)
        _write_report(report_path, report)

        _rebuild_derived(target)
        print(f'rebuilding {len(suspended_indexes)} secondary indexes', flush=True)
        _restore_indexes(target, suspended_indexes)
        target.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        foreign_key_errors = [tuple(row) for row in target.execute(
            'PRAGMA foreign_key_check').fetchall()]
        integrity = target.execute('PRAGMA integrity_check').fetchone()[0]
        is_full_migration = not args.tables
        if foreign_key_errors and is_full_migration:
            raise RuntimeError(
                f'foreign_key_check reported {len(foreign_key_errors)} violation(s)')
        if integrity != 'ok':
            raise RuntimeError(f'SQLite integrity_check failed: {integrity}')
        target.close()
        target = None
        _fsync_database(target_path)

        # Cross-reopen verification: no trust in connection-local/WAL state.
        _verify_reopened_target(
            target_path, table_columns, report,
            args.batch_bytes_mib * 1024 * 1024)
        removed_sidecars = _remove_closed_sqlite_sidecars(target_path)
        _fsync_database(target_path)

        source_quiescence_at_end = _probe_source_quiescence_at_end(
            source_dsn,
            required=bool(args.source_quiesced))
        source_read_only_at_end = source_quiescence_at_end[
            'default_transaction_read_only']
        status, cutover_ready, cutover_reason = _migration_verdict(
            is_full=is_full_migration,
            source_quiesced=args.source_quiesced,
            read_only_at_start=source_read_only_at_start,
            read_only_at_end=source_read_only_at_end,
            peer_sessions_at_start=source_quiescence_at_start[
                'other_client_sessions'],
            peer_sessions_at_end=(source_quiescence_at_end[
                'other_client_sessions'] or 0),
        )
        report['status'] = status
        report['cutover_ready'] = cutover_ready
        report['cutover_reason'] = cutover_reason
        report['source_quiescence'][
            'default_transaction_read_only_at_end'] = source_read_only_at_end
        report['source_quiescence']['other_client_sessions_at_end'] = (
            source_quiescence_at_end['other_client_sessions'])
        report['source_quiescence']['peer_sample_at_end'] = (
            source_quiescence_at_end['peer_sample'])
        if source_quiescence_at_end.get('probe_error'):
            report['source_quiescence']['end_probe_error'] = (
                source_quiescence_at_end['probe_error'])
        report['completed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        report['foreign_key_check'] = (
            'ok' if not foreign_key_errors else {
                'status': 'not_enforced_for_partial_migration',
                'violations': len(foreign_key_errors),
                'sample': foreign_key_errors[:20],
            })
        report['integrity_check'] = 'ok'
        report['cross_reopen_check'] = 'ok'
        report['sidecar_check'] = 'ok'
        report['removed_sidecars'] = removed_sidecars
        target_stat = target_path.stat()
        report['target_size_bytes'] = target_stat.st_size
        report['target_mtime_ns'] = target_stat.st_mtime_ns
        _write_report(report_path, report)
        return report
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = f'{type(exc).__name__}: {exc}'
        report['failed_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        _write_report(report_path, report)
        raise
    finally:
        _release_source_snapshot(source)
        if target is not None:
            target.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', required=True,
                        help='new SQLite file; must not already exist')
    parser.add_argument('--report', default='',
                        help='JSON verification report (default: TARGET.report.json)')
    parser.add_argument(
        '--postgres-dsn-file',
        default=os.environ.get('TOFU_POSTGRES_DSN_FILE', ''),
        help='absolute TLS-verified external PostgreSQL DSN secret file; '
             'defaults to TOFU_POSTGRES_DSN_FILE')
    parser.add_argument('--connect-timeout', type=int, default=10)
    parser.add_argument('--batch-rows', type=int, default=10_000)
    parser.add_argument('--batch-bytes-mib', type=int, default=64)
    parser.add_argument('--table', dest='tables', action='append', default=[],
                        help='copy only this table (repeatable; smoke/testing only)')
    parser.add_argument(
        '--source-quiesced', action='store_true',
        help='maintenance-window assertion: all application writers are '
             'stopped; also requires PostgreSQL fresh sessions to have '
             'default_transaction_read_only=on at start and completion')
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.batch_rows < 1 or args.batch_bytes_mib < 1:
        raise SystemExit('batch sizes must be positive')
    if not args.postgres_dsn_file:
        raise SystemExit(
            '--postgres-dsn-file or TOFU_POSTGRES_DSN_FILE is required')
    with _migration_lock(args.target):
        report = migrate(args)
    print(json.dumps({
        'status': report['status'],
        'target': report['target'],
        'tables': len(report['tables']),
        'report': str(Path(args.report or (args.target + '.report.json')).resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())

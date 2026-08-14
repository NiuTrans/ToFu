"""Managed project-local PostgreSQL backend with isolated read/write pools."""

from __future__ import annotations

from dataclasses import dataclass
import glob
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
import time
from typing import Any
from datetime import datetime, timezone

import orjson
import psycopg2
from psycopg2 import errors as pg_errors
from psycopg2.extras import RealDictCursor
from psycopg2 import sql as pg_sql

from lib.storage.errors import StorageError
from lib.log import get_logger
from lib.storage_sidecar.adapters.base import Backend, Operation
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.preflight import run_filesystem_preflight
from lib.storage_sidecar.schema import initialize_schema


logger = get_logger('tofu.storage.sidecar.postgres')


def _find_pg_binary(name: str) -> str:
    candidates: list[str] = []
    found = shutil.which(name)
    if found:
        candidates.append(found)
    for root in filter(None, {
        str(Path(sys.executable).resolve().parent),
        os.environ.get('CONDA_PREFIX', ''),
    }):
        candidates.extend((str(Path(root) / name), str(Path(root) / 'bin' / name)))
    for pattern in (
        f'/usr/lib/postgresql/*/bin/{name}',
        f'/opt/homebrew/opt/postgresql*/bin/{name}',
        f'/usr/local/opt/postgresql*/bin/{name}',
    ):
        candidates.extend(sorted(glob.glob(pattern), reverse=True))
    for candidate in candidates:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise StorageError(
        'database_unavailable', f'PostgreSQL binary {name} is unavailable')


class _ManagedPostgres:
    def __init__(self, config: SidecarConfig) -> None:
        self.config = config
        configured_port = os.environ.get('TOFU_STORAGE_PG_PORT')
        if configured_port:
            self.port = int(configured_port)
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(('127.0.0.1', 0))
                self.port = int(probe.getsockname()[1])
        if not 1024 <= self.port <= 65535:
            raise RuntimeError('TOFU_STORAGE_PG_PORT is invalid')
        self.user = 'tofu_storage'
        self.database = 'tofu'
        self._started_here = False

    def _base_dsn(self, database: str) -> str:
        return (
            f'host=127.0.0.1 port={self.port} dbname={database} '
            f'user={self.user} connect_timeout=2 application_name=tofu-storage-sidecar'
        )

    @property
    def dsn(self) -> str:
        return self._base_dsn(self.database)

    def _initialize(self) -> None:
        initdb = _find_pg_binary('initdb')
        self.config.pgdata.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [initdb, '-D', str(self.config.pgdata), '--data-checksums',
             '--encoding=UTF8', '--auth=trust', f'--username={self.user}'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            raise StorageError(
                'database_unavailable', 'PostgreSQL cluster initialization failed')

    def _running_port(self) -> int | None:
        pid_file = self.config.pgdata / 'postmaster.pid'
        try:
            lines = pid_file.read_text(encoding='utf-8').splitlines()
            pid = int(lines[0])
            port = int(lines[3])
            os.kill(pid, 0)
            return port
        except (OSError, ValueError, IndexError) as exc:
            logger.debug('PostgreSQL pid/port probe unavailable: %s',
                         type(exc).__name__)
            return None

    def start(self) -> None:
        if not (self.config.pgdata / 'PG_VERSION').is_file():
            self._initialize()
        running_port = self._running_port()
        if running_port is not None:
            self.port = running_port
            # Do not claim or stop a PID from a stale postmaster.pid until the
            # server proves that it actually owns this exact project pgdata.
            adopted = True
        else:
            adopted = False
            pg_ctl = _find_pg_binary('pg_ctl')
            log_path = self.config.logs_dir / 'storage-postgresql.log'
            options = ' '.join((
                '-h 127.0.0.1', f'-p {self.port}',
                '-c fsync=on', '-c synchronous_commit=on',
                '-c full_page_writes=on', '-c max_connections=96',
                '-c idle_in_transaction_session_timeout=5000',
                '-c log_statement=none', '-c log_min_error_statement=error',
            ))
            result = subprocess.run(
                [pg_ctl, '-D', str(self.config.pgdata), '-l', str(log_path),
                 '-o', options, '-w', '-t', '30', 'start'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=40,
                check=False,
            )
            if result.returncode != 0:
                raise StorageError(
                    'database_unavailable', 'PostgreSQL failed to start')
            self._started_here = True
        admin = psycopg2.connect(self._base_dsn('postgres'))
        try:
            admin.autocommit = True
            with admin.cursor() as cursor:
                cursor.execute('SHOW data_directory')
                actual_data_dir = Path(cursor.fetchone()[0]).resolve()
                cursor.execute('SHOW port')
                actual_port = int(cursor.fetchone()[0])
                if (actual_data_dir != self.config.pgdata.resolve()
                        or actual_port != self.port):
                    raise StorageError(
                        'database_unavailable',
                        'PostgreSQL process identity does not match the project cluster')
                # The project lease proves no peer sidecar owns this verified
                # cluster. A postmaster surviving a killed Sidecar is adopted
                # and stopped when the new owner exits.
                if adopted:
                    self._started_here = True
                cursor.execute('SELECT 1 FROM pg_database WHERE datname = %s', (self.database,))
                if cursor.fetchone() is None:
                    cursor.execute(
                        pg_sql.SQL('CREATE DATABASE {}').format(
                            pg_sql.Identifier(self.database)))
        finally:
            admin.close()

    def stop(self) -> None:
        if not self._started_here:
            return
        try:
            subprocess.run(
                [_find_pg_binary('pg_ctl'), '-D', str(self.config.pgdata),
                 '-w', '-t', '30', 'stop', '-m', 'fast'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=40,
                check=False,
            )
        finally:
            self._started_here = False


class PostgresSession:
    backend = 'postgres'

    def __init__(self, connection) -> None:
        self.connection = connection

    def lock_key(self, namespace: str, key: str) -> None:
        # Two-key advisory locks serialize one semantic bucket without
        # coupling the operation catalog to PostgreSQL connection objects.
        self.fetch_one(
            'SELECT pg_advisory_xact_lock(hashtext(?), hashtext(?)) AS locked',
            (namespace, key),
        )

    @staticmethod
    def _sql(value: str) -> str:
        # Catalog SQL is authored internally with the SQLite-compatible
        # placeholder.  Plugins cannot submit text, so this replacement never
        # processes user-controlled SQL or string literals containing '?'.
        # psycopg's paramstyle also treats a literal percent as formatting;
        # double it before introducing its ``%s`` placeholders.
        return value.replace('%', '%%').replace('?', '%s')

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(self._sql(sql), params)
            return max(0, int(cursor.rowcount))

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()):
        with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(self._sql(sql), params)
            row = cursor.fetchone()
            return dict(row) if row is not None else None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()):
        with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(self._sql(sql), params)
            return [dict(row) for row in cursor.fetchall()]


@dataclass(slots=True)
class _PgSlot:
    connection: Any
    created_at: float
    last_used_at: float


class _PgPool:
    def __init__(self, dsn: str, size: int, config: SidecarConfig) -> None:
        self.dsn = dsn
        self.size = size
        self.config = config
        self.queue: queue.Queue[_PgSlot] = queue.Queue(size)
        self.rotations = 0
        now = time.monotonic()
        try:
            for _ in range(size):
                self.queue.put(_PgSlot(self._connect(), now, now))
        except BaseException:
            self.close()
            raise

    def _connect(self):
        connection = psycopg2.connect(self.dsn)
        connection.autocommit = False
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION application_name = 'tofu-storage-sidecar'")
            cursor.execute('SET SESSION idle_in_transaction_session_timeout = 5000')
        connection.commit()
        return connection

    def acquire(self, deadline_at: float) -> _PgSlot:
        timeout = max(0.0, min(
            self.config.acquire_timeout_s, deadline_at - time.monotonic()))
        try:
            slot = self.queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise StorageError(
                'database_timeout', 'PostgreSQL pool acquisition timed out', True, 25,
            ) from exc
        now = time.monotonic()
        if (slot.connection.closed
                or now - slot.created_at >= self.config.max_lifetime_s
                or now - slot.last_used_at >= self.config.idle_lifetime_s):
            try:
                slot.connection.close()
            finally:
                slot = _PgSlot(self._connect(), now, now)
                self.rotations += 1
        return slot

    def release(self, slot: _PgSlot, *, broken: bool = False) -> None:
        if broken or slot.connection.closed:
            try:
                slot.connection.close()
            finally:
                now = time.monotonic()
                slot = _PgSlot(self._connect(), now, now)
        slot.last_used_at = time.monotonic()
        self.queue.put(slot)

    def close(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait().connection.close()


def _map_postgres_error(exc: BaseException) -> StorageError:
    if isinstance(exc, StorageError):
        return exc
    if not isinstance(exc, psycopg2.Error):
        return StorageError('database_internal', 'Storage operation failed')
    state = exc.pgcode or ''
    if state in {'40001', '40P01', '55P03'}:
        return StorageError('database_busy', 'PostgreSQL transaction is busy', True, 25)
    if state == '57014':
        return StorageError('database_timeout', 'PostgreSQL transaction timed out', True, 25)
    if state.startswith('08') or isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
        return StorageError('database_unavailable', 'PostgreSQL is unavailable', True, 100)
    if state in {'23505', '23P01'}:
        return StorageError('database_conflict', 'PostgreSQL uniqueness conflict')
    if state.startswith('23') or isinstance(exc, pg_errors.DataException):
        return StorageError('database_integrity', 'PostgreSQL integrity constraint failed')
    return StorageError('database_internal', 'PostgreSQL operation failed')


class PostgresBackend(Backend):
    name = 'postgres'

    def __init__(self, config: SidecarConfig) -> None:
        self.config = config
        self._manager = _ManagedPostgres(config)
        self._read_pool: _PgPool | None = None
        self._write_pool: _PgPool | None = None
        self._closed = False
        self._preflight: dict[str, Any] = {}
        self._metrics = {'queries': 0, 'commands': 0, 'retries': 0, 'failures': 0}

    def _transaction(
        self,
        pool: _PgPool,
        operation: Operation,
        deadline_at: float,
        *,
        readonly: bool,
        retries: int,
    ) -> Any:
        attempt = 0
        while True:
            slot = pool.acquire(deadline_at)
            broken = False
            retrying = False
            try:
                remaining_ms = max(1, int(min(
                    self.config.transaction_timeout_s,
                    deadline_at - time.monotonic(),
                ) * 1000))
                with slot.connection.cursor() as cursor:
                    # psycopg2 opens a transaction before the first statement
                    # when autocommit is disabled. Sending BEGIN explicitly
                    # therefore emits PostgreSQL's "already a transaction"
                    # warning on every RPC. Read transactions only need their
                    # access mode declared before catalog work begins.
                    if readonly:
                        cursor.execute('SET TRANSACTION READ ONLY')
                    cursor.execute('SET LOCAL statement_timeout = %s', (remaining_ms,))
                    cursor.execute('SET LOCAL lock_timeout = %s',
                                   (min(2000, remaining_ms),))
                result = operation(PostgresSession(slot.connection))
                if readonly:
                    slot.connection.rollback()
                else:
                    slot.connection.commit()
                return result
            except BaseException as exc:
                if not isinstance(exc, (StorageError, psycopg2.Error)):
                    # Never log SQL or parameters here.  The exception class is
                    # enough to diagnose catalog bugs without exposing data.
                    logger.error(
                        'unclassified PostgreSQL semantic failure type=%s',
                        type(exc).__name__)
                try:
                    slot.connection.rollback()
                except psycopg2.Error as rollback_error:
                    broken = True
                    logger.debug('PostgreSQL rollback failed: %s',
                                 type(rollback_error).__name__)
                mapped = _map_postgres_error(exc)
                if (mapped.retryable and attempt < retries
                        and time.monotonic() + 0.02 < deadline_at):
                    attempt += 1
                    self._metrics['retries'] += 1
                    retrying = True
                else:
                    self._metrics['failures'] += 1
                    raise mapped from exc
            finally:
                try:
                    pool.release(slot, broken=broken)
                except psycopg2.Error as release_error:
                    logger.warning(
                        'PostgreSQL pool slot replacement failed: %s',
                        type(release_error).__name__)
            if retrying:
                time.sleep(0.01 * (2 ** attempt))
                continue

    def start(self) -> dict[str, Any]:
        report = run_filesystem_preflight(self.config.data_dir)
        self._manager.start()
        probe = psycopg2.connect(self._manager.dsn)
        try:
            probe.autocommit = True
            with probe.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    'SELECT current_setting(\'fsync\') AS fsync, '
                    'current_setting(\'synchronous_commit\') AS synchronous_commit, '
                    'current_setting(\'full_page_writes\') AS full_page_writes, '
                    'current_setting(\'data_checksums\') AS data_checksums, '
                    'current_setting(\'max_connections\')::int AS max_connections, '
                    'pg_is_in_recovery() AS in_recovery')
                settings = dict(cursor.fetchone())
            if (settings['fsync'] != 'on'
                    or settings['synchronous_commit'] not in {'on', 'remote_apply'}
                    or settings['full_page_writes'] != 'on'
                    or settings['data_checksums'] != 'on'
                    or settings['in_recovery']):
                raise StorageError(
                    'database_unavailable', 'PostgreSQL durability preflight failed')
            budget = max(2, int(settings['max_connections'] * 0.8) - 2)
            requested = self.config.read_pool_size + self.config.write_pool_size
            if requested > budget:
                write_size = max(1, min(
                    self.config.write_pool_size,
                    round(budget * self.config.write_pool_size / requested),
                ))
                read_size = max(1, budget - write_size)
            else:
                read_size = self.config.read_pool_size
                write_size = self.config.write_pool_size
            # Initialize the logical schema before filling either pool.
            probe.autocommit = False
            startup_session = PostgresSession(probe)
            initialize_schema(startup_session)
            startup_session.execute(
                'INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?) '
                'ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value',
                ('__startup_read_write_probe__', 'ok'),
            )
            startup_probe = startup_session.fetch_one(
                'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
                ('__startup_read_write_probe__',),
            )
            if startup_probe is None or startup_probe['meta_value'] != 'ok':
                raise StorageError(
                    'database_integrity', 'PostgreSQL read/write startup probe failed')
            startup_session.execute(
                'DELETE FROM storage_meta WHERE meta_key = ?',
                ('__startup_read_write_probe__',),
            )
            probe.commit()
        except BaseException:
            probe.rollback()
            probe.close()
            self._manager.stop()
            raise
        probe.close()
        try:
            self._read_pool = _PgPool(self._manager.dsn, read_size, self.config)
            self._write_pool = _PgPool(self._manager.dsn, write_size, self.config)
        except BaseException:
            if self._read_pool:
                self._read_pool.close()
            self._manager.stop()
            raise
        self._preflight = {
            **report.as_dict(),
            'durability': {
                key: settings[key] for key in (
                    'fsync', 'synchronous_commit', 'full_page_writes', 'data_checksums')
            },
            'read_pool_capacity': read_size,
            'write_pool_capacity': write_size,
            'connection_budget_80pct': budget,
        }
        return self.health()

    def query(self, operation: Operation, deadline_at: float) -> Any:
        if self._read_pool is None:
            raise StorageError('database_unavailable', 'PostgreSQL read pool is not ready')
        result = self._transaction(
            self._read_pool, operation, deadline_at, readonly=True, retries=1)
        self._metrics['queries'] += 1
        return result

    def command(
        self,
        operation_name: str,
        payload_digest: str,
        command_id: str | None,
        priority: str,
        operation: Operation,
        deadline_at: float,
        *,
        receipt_required: bool,
    ) -> Any:
        del priority  # PostgreSQL uses its isolated write pool, not SQLite lanes.
        if receipt_required and (
                not isinstance(command_id, str)
                or not command_id
                or len(command_id) > 200):
            raise StorageError(
                'database_protocol_error', 'A valid command_id is required')
        if self._write_pool is None:
            raise StorageError('database_unavailable', 'PostgreSQL write pool is not ready')

        def transactional(session: PostgresSession) -> Any:
            if receipt_required:
                # Serialize identical command IDs before the receipt probe.
                # This closes the concurrent-first-delivery race where two
                # transactions both observe "no receipt" and one later loses
                # the receipt PK insert after repeating the business mutation.
                session.fetch_one(
                    'SELECT pg_advisory_xact_lock(hashtext(?)) AS locked',
                    (command_id,),
                )
                receipt = session.fetch_one(
                    'SELECT operation, request_digest, response_json '
                    'FROM storage_command_receipts WHERE command_id = ? FOR UPDATE',
                    (command_id,),
                )
                if receipt is not None:
                    if (receipt['operation'] != operation_name
                            or receipt['request_digest'] != payload_digest):
                        raise StorageError(
                            'database_conflict', 'command_id was reused for a different request')
                    return orjson.loads(bytes(receipt['response_json']))
            response = operation(session)
            if receipt_required:
                encoded = orjson.dumps(response, option=orjson.OPT_SORT_KEYS)
                if len(encoded) > 64 * 1024:
                    raise StorageError(
                        'database_protocol_error', 'Command response is too large for a receipt')
                session.execute(
                    'INSERT INTO storage_command_receipts('
                    'command_id, operation, request_digest, response_json, committed_at_ms) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (command_id, operation_name, payload_digest, encoded,
                     int(time.time() * 1000)),
                )
            return response

        result = self._transaction(
            self._write_pool, transactional, deadline_at, readonly=False, retries=3)
        self._metrics['commands'] += 1
        return result

    def health(self) -> dict[str, Any]:
        return {
            'ready': not self._closed and self._read_pool is not None
                     and self._write_pool is not None,
            'backend': self.name,
            'protocol': 'storage.v1',
            'preflight': self._preflight,
        }

    def metrics(self) -> dict[str, Any]:
        return {
            'backend': self.name,
            **self._metrics,
            'read_pool_available': self._read_pool.queue.qsize() if self._read_pool else 0,
            'write_pool_available': self._write_pool.queue.qsize() if self._write_pool else 0,
            'read_pool_rotations': self._read_pool.rotations if self._read_pool else 0,
            'write_pool_rotations': self._write_pool.rotations if self._write_pool else 0,
        }

    def integrity_check(self, deadline_at: float) -> dict[str, Any]:
        return self.query(
            lambda session: {
                'ok': bool(session.fetch_one(
                    'SELECT NOT pg_is_in_recovery() AS ok')['ok']),
                'checksums': 'on',
            },
            deadline_at,
        )

    def backup(self, deadline_at: float) -> dict[str, Any]:
        backups = self.config.data_dir / 'backups'
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        target = backups / f'storage-postgres-{stamp}'
        if target.exists():
            raise StorageError('database_conflict', 'Backup target already exists')
        timeout = max(1, int(deadline_at - time.monotonic()))
        result = subprocess.run(
            [_find_pg_binary('pg_basebackup'), '-D', str(target),
             '-d', self._manager.dsn, '-X', 'stream', '-c', 'fast',
             '--checkpoint=fast', '--no-password'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            if target.exists():
                shutil.rmtree(target)
            raise StorageError(
                'database_unavailable', 'PostgreSQL base backup failed', True, 100)
        return {
            'ok': True,
            'backup': str(target.relative_to(self.config.project_root)),
            'bytes': sum(path.stat().st_size for path in target.rglob('*') if path.is_file()),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._read_pool:
            self._read_pool.close()
            self._read_pool = None
        if self._write_pool:
            self._write_pool.close()
            self._write_pool = None
        self._manager.stop()


__all__ = ['PostgresBackend', 'PostgresSession']

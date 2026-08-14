"""SQLite enterprise backend: one writer, fair priority queue, read-only pool."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import queue
import sqlite3
import threading
import time
from typing import Any
from datetime import datetime, timezone
import os

import orjson

from lib.storage.errors import StorageError
from lib.log import get_logger
from lib.storage_sidecar.adapters.base import Backend, Operation
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.preflight import run_filesystem_preflight
from lib.storage_sidecar.schema import initialize_schema


_SQLITE_BUSY = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
_SQLITE_UNAVAILABLE = {sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_FULL}
_SQLITE_INTEGRITY = {sqlite3.SQLITE_CONSTRAINT, sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
_PRIORITY_CYCLE = ('user',) * 8 + ('event',) * 2 + ('maintenance',)
logger = get_logger('tofu.storage.sidecar.sqlite')


class SQLiteSession:
    backend = 'sqlite'

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def lock_key(self, namespace: str, key: str) -> None:
        # The backend's single physical writer already serializes every key.
        del namespace, key

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        cursor = self.connection.execute(sql, params)
        return max(0, int(cursor.rowcount))

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()):
        row = self.connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()):
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]


@dataclass(slots=True)
class _ReadSlot:
    connection: sqlite3.Connection
    created_at: float
    last_used_at: float


@dataclass(slots=True)
class _WriteJob:
    operation: Operation
    deadline_at: float
    priority: str
    started: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None
    cancelled: bool = False


class _FairWriter:
    def __init__(self, connection: sqlite3.Connection, transaction_timeout_s: float) -> None:
        self._connection = connection
        self._transaction_timeout_s = transaction_timeout_s
        self._queues = {name: deque() for name in {'user', 'event', 'maintenance'}}
        self._condition = threading.Condition()
        self._stop = False
        self._cycle_index = 0
        self._thread = threading.Thread(
            target=self._run, name='storage-sqlite-writer', daemon=True)
        self.metrics = {
            'submitted': 0, 'completed': 0, 'failed': 0, 'timed_out': 0,
            'max_queue_depth': 0, 'transaction_retries': 0,
        }
        self._thread.start()

    def _take(self) -> _WriteJob | None:
        with self._condition:
            while not self._stop and not any(self._queues.values()):
                self._condition.wait()
            if self._stop and not any(self._queues.values()):
                return None
            for _ in range(len(_PRIORITY_CYCLE)):
                name = _PRIORITY_CYCLE[self._cycle_index]
                self._cycle_index = (self._cycle_index + 1) % len(_PRIORITY_CYCLE)
                if self._queues[name]:
                    return self._queues[name].popleft()
            return next(q.popleft() for q in self._queues.values() if q)

    def _run(self) -> None:
        while True:
            job = self._take()
            if job is None:
                return
            if job.cancelled or time.monotonic() >= job.deadline_at:
                job.error = StorageError(
                    'database_timeout', 'Storage writer acquisition timed out', True, 25)
                if not job.cancelled:
                    self.metrics['timed_out'] += 1
                job.started.set()
                job.done.set()
                continue
            job.started.set()
            try:
                job.result = self._transaction(job)
                self.metrics['completed'] += 1
            except Exception as exc:
                job.error = exc
                self.metrics['failed'] += 1
                logger.debug('SQLite writer job failed: %s', type(exc).__name__)
            finally:
                job.done.set()

    def _transaction(self, job: _WriteJob) -> Any:
        attempts = 0
        while True:
            transaction_deadline = min(
                job.deadline_at, time.monotonic() + self._transaction_timeout_s)
            self._connection.set_progress_handler(
                lambda: 1 if time.monotonic() >= transaction_deadline else 0,
                1000,
            )
            try:
                self._connection.execute('BEGIN IMMEDIATE')
                result = job.operation(SQLiteSession(self._connection))
                if time.monotonic() >= transaction_deadline:
                    raise StorageError(
                        'database_timeout', 'Storage transaction exceeded its watchdog',
                        True, 25)
                self._connection.commit()
                return result
            except BaseException as exc:
                # Rollback precedes classification and every possible retry.
                try:
                    self._connection.rollback()
                except sqlite3.Error as rollback_error:
                    logger.debug('SQLite rollback after failed transaction failed: %s',
                                 type(rollback_error).__name__)
                mapped = _map_sqlite_error(exc, transaction_deadline)
                if (mapped.retryable and mapped.code in {'database_busy', 'database_unavailable'}
                        and attempts < 3 and time.monotonic() + 0.02 < job.deadline_at):
                    attempts += 1
                    self.metrics['transaction_retries'] += 1
                    time.sleep(0.01 * (2 ** attempts))
                    continue
                raise mapped from exc
            finally:
                self._connection.set_progress_handler(None, 0)

    def submit(self, operation: Operation, priority: str, deadline_at: float) -> Any:
        if priority not in self._queues:
            raise StorageError('database_protocol_error', 'Invalid storage priority')
        job = _WriteJob(operation=operation, deadline_at=deadline_at, priority=priority)
        with self._condition:
            if self._stop:
                raise StorageError(
                    'database_unavailable', 'Storage writer is stopping', True, 100)
            self._queues[priority].append(job)
            depth = sum(len(items) for items in self._queues.values())
            self.metrics['submitted'] += 1
            self.metrics['max_queue_depth'] = max(self.metrics['max_queue_depth'], depth)
            self._condition.notify()
        acquire_wait = max(0.0, min(2.0, deadline_at - time.monotonic()))
        if not job.started.wait(acquire_wait):
            job.cancelled = True
            self.metrics['timed_out'] += 1
            raise StorageError(
                'database_timeout', 'Storage writer acquisition timed out', True, 25)
        remaining = max(0.0, deadline_at - time.monotonic())
        if not job.done.wait(remaining):
            raise StorageError(
                'database_timeout', 'Storage command deadline expired', True, 25)
        if job.error is not None:
            raise job.error
        return job.result

    def close(self) -> None:
        with self._condition:
            self._stop = True
            self._condition.notify_all()
        self._thread.join(timeout=10)
        self._connection.close()


def _map_sqlite_error(exc: BaseException, deadline_at: float) -> StorageError:
    if isinstance(exc, StorageError):
        return exc
    if not isinstance(exc, sqlite3.Error):
        return StorageError('database_internal', 'Storage operation failed')
    raw_code = getattr(exc, 'sqlite_errorcode', 0) or 0
    code = int(raw_code) & 0xFF
    if code == sqlite3.SQLITE_INTERRUPT and time.monotonic() >= deadline_at:
        return StorageError('database_timeout', 'Storage transaction timed out', True, 25)
    if code in _SQLITE_BUSY:
        return StorageError('database_busy', 'Storage writer is busy', True, 25)
    if code in _SQLITE_UNAVAILABLE:
        return StorageError('database_unavailable', 'SQLite storage is unavailable', True, 100)
    if code in _SQLITE_INTEGRITY:
        return StorageError('database_integrity', 'SQLite integrity constraint failed')
    return StorageError('database_internal', 'SQLite operation failed')


class SQLiteBackend(Backend):
    name = 'sqlite'

    def __init__(self, config: SidecarConfig) -> None:
        self.config = config
        self._writer: _FairWriter | None = None
        self._read_pool: queue.Queue[_ReadSlot] = queue.Queue(config.read_pool_size)
        self._closed = False
        self._preflight: dict[str, Any] = {}
        self._metrics = {'queries': 0, 'query_failures': 0, 'pool_rotations': 0}

    def _connect(self, *, query_only: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.config.sqlite_path,
            timeout=self.config.acquire_timeout_s,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=2000')
        connection.execute('PRAGMA foreign_keys=ON')
        if query_only:
            connection.execute('PRAGMA query_only=ON')
        else:
            mode = connection.execute('PRAGMA journal_mode=WAL').fetchone()[0]
            if str(mode).lower() != 'wal':
                connection.close()
                raise StorageError(
                    'database_unavailable', 'SQLite WAL mode is unavailable')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA wal_autocheckpoint=4096')
        return connection

    def _scratch_recovery_preflight(self) -> None:
        path = self.config.data_dir / '.storage-wal-preflight.sqlite3'
        for suffix in ('', '-wal', '-shm'):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        try:
            connection = sqlite3.connect(path, isolation_level=None)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('CREATE TABLE durability_probe(value TEXT NOT NULL)')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT INTO durability_probe(value) VALUES (?)', ('committed',))
            connection.commit()
            connection.close()
            reopened = sqlite3.connect(path)
            row = reopened.execute('SELECT value FROM durability_probe').fetchone()
            integrity = reopened.execute('PRAGMA integrity_check').fetchone()[0]
            reopened.close()
            if row != ('committed',) or integrity != 'ok':
                raise StorageError(
                    'database_integrity', 'SQLite WAL recovery preflight failed')
        finally:
            for suffix in ('', '-wal', '-shm'):
                path.with_name(path.name + suffix).unlink(missing_ok=True)

    def start(self) -> dict[str, Any]:
        report = run_filesystem_preflight(self.config.data_dir)
        self._scratch_recovery_preflight()
        writer_connection = self._connect(query_only=False)
        try:
            writer_connection.execute('BEGIN IMMEDIATE')
            startup_session = SQLiteSession(writer_connection)
            initialize_schema(startup_session)
            startup_session.execute(
                'INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?) '
                'ON CONFLICT(meta_key) DO UPDATE SET meta_value = excluded.meta_value',
                ('__startup_read_write_probe__', 'ok'),
            )
            probe = startup_session.fetch_one(
                'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
                ('__startup_read_write_probe__',),
            )
            if probe is None or probe['meta_value'] != 'ok':
                raise StorageError(
                    'database_integrity', 'SQLite read/write startup probe failed')
            startup_session.execute(
                'DELETE FROM storage_meta WHERE meta_key = ?',
                ('__startup_read_write_probe__',),
            )
            # Do not run PRAGMA integrity_check in the boot-critical path.  It
            # scans the entire authority and can exceed the supervisor's
            # bounded startup window by minutes once tofu.db reaches tens of
            # gigabytes.  Filesystem durability, scratch WAL recovery, schema
            # initialization, and the real transactional round-trip above are
            # the bounded readiness gates.  The exhaustive scan remains
            # available through ``system.integrity_check`` / storage_certify.
            writer_connection.commit()
        except BaseException:
            writer_connection.rollback()
            writer_connection.close()
            raise
        now = time.monotonic()
        try:
            for _ in range(self.config.read_pool_size):
                self._read_pool.put(_ReadSlot(self._connect(query_only=True), now, now))
        except BaseException:
            while not self._read_pool.empty():
                self._read_pool.get_nowait().connection.close()
            writer_connection.close()
            raise
        self._writer = _FairWriter(
            writer_connection, self.config.transaction_timeout_s)
        self._preflight = report.as_dict()
        return self.health()

    def _acquire_read(self, deadline_at: float) -> _ReadSlot:
        timeout = max(0.0, min(
            self.config.acquire_timeout_s, deadline_at - time.monotonic()))
        try:
            slot = self._read_pool.get(timeout=timeout)
        except queue.Empty as exc:
            raise StorageError(
                'database_timeout', 'Storage read pool acquisition timed out', True, 25,
            ) from exc
        now = time.monotonic()
        if (now - slot.created_at >= self.config.max_lifetime_s
                or now - slot.last_used_at >= self.config.idle_lifetime_s):
            slot.connection.close()
            slot = _ReadSlot(self._connect(query_only=True), now, now)
            self._metrics['pool_rotations'] += 1
        return slot

    def query(self, operation: Operation, deadline_at: float) -> Any:
        slot = self._acquire_read(deadline_at)
        slot.connection.set_progress_handler(
            lambda: 1 if time.monotonic() >= deadline_at else 0, 1000)
        try:
            slot.connection.execute('BEGIN')
            result = operation(SQLiteSession(slot.connection))
            slot.connection.rollback()
            self._metrics['queries'] += 1
            return result
        except BaseException as exc:
            try:
                slot.connection.rollback()
            except sqlite3.Error as rollback_error:
                logger.debug('SQLite read rollback failed: %s',
                             type(rollback_error).__name__)
            self._metrics['query_failures'] += 1
            raise _map_sqlite_error(exc, deadline_at) from exc
        finally:
            slot.connection.set_progress_handler(None, 0)
            slot.last_used_at = time.monotonic()
            self._read_pool.put(slot)

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
        if receipt_required and (
                not isinstance(command_id, str)
                or not command_id
                or len(command_id) > 200):
            raise StorageError(
                'database_protocol_error', 'A valid command_id is required')

        def transactional(session: SQLiteSession) -> Any:
            if receipt_required:
                receipt = session.fetch_one(
                    'SELECT operation, request_digest, response_json '
                    'FROM storage_command_receipts WHERE command_id = ?',
                    (command_id,),
                )
                if receipt is not None:
                    if (receipt['operation'] != operation_name
                            or receipt['request_digest'] != payload_digest):
                        raise StorageError(
                            'database_conflict', 'command_id was reused for a different request')
                    return orjson.loads(receipt['response_json'])
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

        if self._writer is None:
            raise StorageError('database_unavailable', 'SQLite writer is not ready', True, 100)
        return self._writer.submit(transactional, priority, deadline_at)

    def health(self) -> dict[str, Any]:
        return {
            'ready': not self._closed and self._writer is not None,
            'backend': self.name,
            'protocol': 'storage.v1',
            'preflight': self._preflight,
        }

    def metrics(self) -> dict[str, Any]:
        writer = dict(self._writer.metrics) if self._writer else {}
        return {
            'backend': self.name,
            'read_pool_size': self._read_pool.qsize(),
            'read_pool_capacity': self.config.read_pool_size,
            'queries': dict(self._metrics),
            'writer': writer,
        }

    def integrity_check(self, deadline_at: float) -> dict[str, Any]:
        def check(session: SQLiteSession):
            row = session.fetch_one('PRAGMA integrity_check')
            # PRAGMA result column name is driver-defined; use the sole value.
            value = next(iter(row.values())) if row else ''
            return {'ok': value == 'ok', 'result': value}

        return self.query(check, deadline_at)

    def backup(self, deadline_at: float) -> dict[str, Any]:
        backups = self.config.data_dir / 'backups'
        backups.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        target = backups / f'storage-sqlite-{stamp}.sqlite3'
        if target.exists():
            raise StorageError('database_conflict', 'Backup target already exists')
        slot = self._acquire_read(deadline_at)
        destination = None
        try:
            destination = sqlite3.connect(target, isolation_level=None)
            slot.connection.backup(destination, pages=1024)
            result = destination.execute('PRAGMA integrity_check').fetchone()[0]
            if result != 'ok':
                raise StorageError('database_integrity', 'SQLite backup verification failed')
            destination.close()
            destination = None
            with target.open('rb') as stream:
                os.fsync(stream.fileno())
            return {
                'ok': True,
                'backup': str(target.relative_to(self.config.project_root)),
                'bytes': target.stat().st_size,
            }
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            if destination is not None:
                destination.close()
            slot.last_used_at = time.monotonic()
            self._read_pool.put(slot)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        while not self._read_pool.empty():
            self._read_pool.get_nowait().connection.close()


__all__ = ['SQLiteBackend', 'SQLiteSession']

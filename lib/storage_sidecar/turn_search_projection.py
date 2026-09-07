"""Independent, rebuildable materialization for conversation turn search.

Responsibility: consume transactional dirty-set markers from the durable
authority and materialize bounded search fragments without holding the
authority writer. SQLite targets a host-local disposable database; PostgreSQL
targets shared projection tables through its independent transaction pool.

Entry point: :class:`TurnSearchProjectionRuntime`. Dependencies: the backend
transaction contract and turn-search text projector. No route or application
module opens either database.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import os
from pathlib import Path
import queue
import sqlite3
import stat
import threading
import time
from typing import Any, Protocol

import orjson

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.base import Backend, Operation, Session
from lib.storage_sidecar.projection_codec import (
    STORAGE_PROJECTION_MAX_HYDRATION_RATIO,
)


logger = get_logger('tofu.storage.sidecar.turn_search_projection')

PROJECTION_NAME = 'turn_search.v1'
LOCAL_FORMAT_VERSION = 1
LOCAL_DATABASE_NAME = 'turn-search-v1.sqlite3'
_DIRTY_BATCH = 16
_BACKFILL_ROWS = 8
_BACKFILL_BYTES = 2_000_000
# ``length(TEXT)`` is characters on both supported authorities. Four bytes per
# Unicode scalar is the conservative UTF-8 ceiling. A private projection codec
# may then restore at most one exact segment copy per unique tool round, so its
# one-to-one invariant contributes the explicit hydration ratio below. This
# probe therefore guarantees the decoded source still fits the 2 MiB page
# budget before deriving a bounded 10 KiB search fragment.
_SOURCE_TEXT_MAX_UNITS = (
    _BACKFILL_BYTES // (4 * STORAGE_PROJECTION_MAX_HYDRATION_RATIO)
)
_IDLE_POLL_SECONDS = 2.0
_RETRY_SECONDS = 0.25


def _field(row: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Read a mapping field without requiring the optional ``dict.get`` API."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


class _ProjectionTarget(Protocol):
    def start(self) -> None: ...
    def close(self) -> None: ...
    def apply_turn(
        self, identity: Mapping[str, Any], snapshot: Mapping[str, Any] | None,
        generation_token: str,
    ) -> None: ...
    def begin_conversation(
        self, identity: Mapping[str, Any], header: Mapping[str, Any] | None,
        generation_token: str,
    ) -> None: ...
    def apply_conversation_page(
        self, rows: list[Mapping[str, Any]], generation_token: str,
    ) -> None: ...
    def finalize_conversation(
        self, identity: Mapping[str, Any], generation_token: str,
    ) -> None: ...
    def reset(self) -> None: ...
    def backfill_state(self) -> tuple[str, bool]: ...
    def set_backfill_state(self, cursor: str, complete: bool) -> None: ...
    def status(self) -> dict[str, Any]: ...


class _LocalSession:
    """Small Session implementation over the disposable projection DB."""

    backend = 'sqlite'

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def lock_key(self, namespace: str, key: str) -> None:
        del namespace, key

    def index_exists(self, index_name: str) -> bool:
        return self.fetch_one(
            "SELECT 1 AS present FROM sqlite_schema "
            "WHERE type='index' AND name=?", (index_name,)) is not None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        cursor = self.connection.execute(sql, params)
        return max(0, int(cursor.rowcount))

    def fetch_one(self, sql: str, params: tuple[Any, ...] = ()):
        row = self.connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetch_all(self, sql: str, params: tuple[Any, ...] = ()):
        return [
            dict(row)
            for row in self.connection.execute(sql, params).fetchall()
        ]

    def fetch_one_for_update_skip_locked(
        self, sql: str, params: tuple[Any, ...] = (),
    ):
        return self.fetch_one(sql, params)


_LOCAL_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS projection_meta ("
    "meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS storage_search_conversations ("
    "id TEXT NOT NULL,user_id INTEGER NOT NULL,updated_at_ms INTEGER NOT NULL,"
    "generation_token TEXT NOT NULL DEFAULT '',PRIMARY KEY(user_id,id))",
    "CREATE INDEX IF NOT EXISTS idx_storage_search_conversations_order "
    "ON storage_search_conversations(user_id,updated_at_ms,id)",
    "CREATE TABLE IF NOT EXISTS storage_search_turns ("
    "conversation_id TEXT NOT NULL,user_id INTEGER NOT NULL,turn_id TEXT NOT NULL,"
    "lane_id TEXT NOT NULL,ordinal INTEGER NOT NULL,search_text TEXT NOT NULL,"
    "projection_revision INTEGER NOT NULL,updated_at INTEGER NOT NULL,"
    "generation_token TEXT NOT NULL DEFAULT '',"
    "PRIMARY KEY(user_id,conversation_id,turn_id))",
    "CREATE INDEX IF NOT EXISTS idx_storage_search_turns_owner_order "
    "ON storage_search_turns(user_id,conversation_id,lane_id,ordinal)",
)


def _projection_values(
    snapshot: Mapping[str, Any], generation_token: str,
) -> tuple[Any, ...]:
    from lib.storage_sidecar.operations_pkg._common import _load
    from lib.storage_sidecar.operations_pkg._turns import _turn_search_text

    projection = _load(_field(snapshot, 'projection_json')) or {}
    if not isinstance(projection, Mapping):
        projection = {}
    search_text = _turn_search_text(
        str(_field(snapshot, 'actor') or ''), projection)
    return (
        str(snapshot['conversation_id']),
        int(snapshot['user_id']),
        str(snapshot['turn_id']),
        str(_field(snapshot, 'lane_id') or 'main'),
        int(_field(snapshot, 'ordinal') or 0),
        search_text,
        int(_field(snapshot, 'projection_revision') or 0),
        int(_field(snapshot, 'updated_at') or 0),
        generation_token,
    )


def _upsert_header(
    session: Session, header: Mapping[str, Any], generation_token: str,
) -> None:
    session.execute(
        "INSERT INTO storage_search_conversations("
        "id,user_id,updated_at_ms,generation_token) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id,id) DO UPDATE SET "
        "updated_at_ms=excluded.updated_at_ms,"
        "generation_token=excluded.generation_token",
        (
            str(header['id']),
            int(header['user_id']),
            int(_field(header, 'updated_at_ms') or 0),
            generation_token,
        ),
    )


def _apply_turn_to_session(
    session: Session,
    identity: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    generation_token: str,
) -> None:
    user_id = int(identity['user_id'])
    turn_id = str(identity['entity_key'])
    if snapshot is None or (
        str(_field(snapshot, 'lane_id') or 'main') != 'main'
        or str(_field(snapshot, 'status') or '') in {'pending', 'running'}
    ):
        session.execute(
            "DELETE FROM storage_search_turns WHERE user_id=? AND turn_id=?",
            (user_id, turn_id),
        )
        return
    _upsert_header(session, {
        'id': snapshot['conversation_id'],
        'user_id': snapshot['user_id'],
        'updated_at_ms': _field(snapshot, 'conversation_updated_at_ms') or 0,
    }, generation_token)
    session.execute(
        "INSERT INTO storage_search_turns("
        "conversation_id,user_id,turn_id,lane_id,ordinal,search_text,"
        "projection_revision,updated_at,generation_token) "
        "VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id,conversation_id,turn_id) DO UPDATE SET "
        "lane_id=excluded.lane_id,ordinal=excluded.ordinal,"
        "search_text=excluded.search_text,"
        "projection_revision=excluded.projection_revision,"
        "updated_at=excluded.updated_at,"
        "generation_token=excluded.generation_token",
        _projection_values(snapshot, generation_token),
    )


def _begin_conversation_in_session(
    session: Session,
    identity: Mapping[str, Any],
    header: Mapping[str, Any] | None,
    generation_token: str,
) -> None:
    user_id = int(identity['user_id'])
    conversation_id = str(identity['entity_key'])
    if header is None:
        session.execute(
            "DELETE FROM storage_search_turns "
            "WHERE user_id=? AND conversation_id=?",
            (user_id, conversation_id),
        )
        session.execute(
            "DELETE FROM storage_search_conversations "
            "WHERE user_id=? AND id=?",
            (user_id, conversation_id),
        )
        return
    _upsert_header(session, header, generation_token)


def _apply_page_in_session(
    session: Session, rows: list[Mapping[str, Any]], generation_token: str,
) -> None:
    for row in rows:
        _apply_turn_to_session(
            session,
            {'user_id': row['user_id'], 'entity_key': row['turn_id']},
            row,
            generation_token,
        )


def _finalize_conversation_in_session(
    session: Session,
    identity: Mapping[str, Any],
    generation_token: str,
) -> None:
    session.execute(
        "DELETE FROM storage_search_turns "
        "WHERE user_id=? AND conversation_id=? AND generation_token<>?",
        (
            int(identity['user_id']),
            str(identity['entity_key']),
            generation_token,
        ),
    )


class LocalSQLiteTurnSearchTarget:
    """Host-local projection with corruption recovery and a hard byte budget."""

    def __init__(self, directory: Path, max_bytes: int) -> None:
        self.directory = Path(directory)
        self.path = self.directory / LOCAL_DATABASE_NAME
        self.max_bytes = max(1, int(max_bytes))
        self._writer: sqlite3.Connection | None = None
        self._read_pool: queue.Queue[sqlite3.Connection] = queue.Queue(2)
        self._lock = threading.Lock()
        self._state = 'starting'
        self._last_error = ''
        self._writes = 0

    def _prepare_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise StorageError(
                'database_unavailable', 'Search projection directory is unsafe')
        if os.name != 'nt':
            info = self.directory.stat()
            if info.st_uid != os.getuid():
                raise StorageError(
                    'database_unavailable',
                    'Search projection directory has a foreign owner')
            if stat.S_IMODE(info.st_mode) & 0o077:
                self.directory.chmod(0o700)

    def _connect(self, *, query_only: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=2.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA busy_timeout=2000')
            if query_only:
                connection.execute('PRAGMA query_only=ON')
            else:
                connection.execute('PRAGMA journal_mode=WAL')
                # This store is disposable and replayable. NORMAL prevents a
                # host-cache fsync from entering the authority latency budget.
                connection.execute('PRAGMA synchronous=NORMAL')
                connection.execute('PRAGMA wal_autocheckpoint=1000')
            return connection
        except BaseException:
            connection.close()
            raise

    def _discard_corrupt(self) -> None:
        # This database has no authority value. Keeping timestamped corrupt
        # copies made repeated recoveries a process-lifetime disk leak and
        # could double the declared projection budget. Preserve the diagnosis
        # in status/logs and reclaim every byte before rebuilding.
        for suffix in ('', '-wal', '-shm'):
            source = self.path.with_name(self.path.name + suffix)
            source.unlink(missing_ok=True)
        self._last_error = 'corrupt_projection_rebuilt'
        logger.warning(
            '[turn-search] discarded corrupt disposable projection at %s',
            self.path,
        )

    def _open_writer(self) -> sqlite3.Connection:
        try:
            connection = self._connect(query_only=False)
            result = connection.execute('PRAGMA quick_check').fetchone()[0]
            if result != 'ok':
                raise sqlite3.DatabaseError('projection quick_check failed')
            return connection
        except sqlite3.DatabaseError:
            try:
                connection.close()
            except (UnboundLocalError, sqlite3.Error):
                pass
            self._discard_corrupt()
            return self._connect(query_only=False)

    def start(self) -> None:
        self._prepare_directory()
        self._writer = self._open_writer()
        session = _LocalSession(self._writer)
        self._writer.execute('BEGIN IMMEDIATE')
        try:
            for statement in _LOCAL_SCHEMA:
                session.execute(statement)
            version = session.fetch_one(
                "SELECT meta_value FROM projection_meta WHERE meta_key=?",
                ('format_version',),
            )
            if version is not None and int(version['meta_value']) != LOCAL_FORMAT_VERSION:
                session.execute('DELETE FROM storage_search_turns')
                session.execute('DELETE FROM storage_search_conversations')
                session.execute('DELETE FROM projection_meta')
            session.execute(
                "INSERT INTO projection_meta(meta_key,meta_value) VALUES (?,?) "
                "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
                ('format_version', str(LOCAL_FORMAT_VERSION)),
            )
            self._writer.commit()
        except BaseException:
            self._writer.rollback()
            self._writer.close()
            self._writer = None
            raise
        try:
            for _ in range(2):
                self._read_pool.put(self._connect(query_only=True))
        except BaseException:
            while True:
                try:
                    self._read_pool.get_nowait().close()
                except queue.Empty:
                    break
            self._writer.close()
            self._writer = None
            self._state = 'failed'
            raise
        self._state = 'warming'

    def _database_bytes(self) -> int:
        total = 0
        for suffix in ('', '-wal', '-shm'):
            path = self.path.with_name(self.path.name + suffix)
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def _write(self, callback: Callable[[Session], None]) -> None:
        with self._lock:
            if self._writer is None or self._state == 'closed':
                raise StorageError(
                    'database_unavailable', 'Search projection is unavailable')
            if self._database_bytes() > self.max_bytes:
                self._state = 'capacity_exceeded'
                raise StorageError(
                    'database_unavailable',
                    'Conversation search projection reached its resource budget')
            self._writer.execute('BEGIN IMMEDIATE')
            try:
                callback(_LocalSession(self._writer))
                self._writer.commit()
                self._writes += 1
            except BaseException:
                self._writer.rollback()
                raise

    def query(self, operation: Operation, deadline_at: float) -> Any:
        if self._state == 'closed':
            raise StorageError(
                'database_unavailable', 'Search projection is unavailable')
        if self._state == 'capacity_exceeded':
            raise StorageError(
                'database_unavailable',
                'Conversation search projection reached its resource budget')
        timeout = max(0.0, min(2.0, deadline_at - time.monotonic()))
        try:
            connection = self._read_pool.get(timeout=timeout)
        except queue.Empty as exc:
            raise StorageError(
                'database_timeout', 'Search projection read timed out', True, 25,
            ) from exc
        connection.set_progress_handler(
            lambda: 1 if time.monotonic() >= deadline_at else 0, 1000)
        try:
            connection.execute('BEGIN')
            result = operation(_LocalSession(connection))
            connection.rollback()
            return result
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            raise StorageError(
                'database_timeout' if time.monotonic() >= deadline_at
                else 'database_unavailable',
                'Conversation search projection query failed',
                time.monotonic() >= deadline_at,
                25,
            ) from exc
        finally:
            connection.set_progress_handler(None, 0)
            if self._state == 'closed':
                connection.close()
            else:
                self._read_pool.put(connection)

    def apply_turn(
        self, identity: Mapping[str, Any], snapshot: Mapping[str, Any] | None,
        generation_token: str,
    ) -> None:
        self._write(lambda session: _apply_turn_to_session(
            session, identity, snapshot, generation_token))

    def begin_conversation(
        self, identity: Mapping[str, Any], header: Mapping[str, Any] | None,
        generation_token: str,
    ) -> None:
        self._write(lambda session: _begin_conversation_in_session(
            session, identity, header, generation_token))

    def apply_conversation_page(
        self, rows: list[Mapping[str, Any]], generation_token: str,
    ) -> None:
        self._write(lambda session: _apply_page_in_session(
            session, rows, generation_token))

    def finalize_conversation(
        self, identity: Mapping[str, Any], generation_token: str,
    ) -> None:
        self._write(lambda session: _finalize_conversation_in_session(
            session, identity, generation_token))

    def reset(self) -> None:
        def clear(session: Session) -> None:
            session.execute('DELETE FROM storage_search_turns')
            session.execute('DELETE FROM storage_search_conversations')
            session.execute(
                "DELETE FROM projection_meta WHERE meta_key IN (?,?)",
                ('backfill_cursor', 'backfill_complete'),
            )
        self._write(clear)
        self._state = 'warming'

    def backfill_state(self) -> tuple[str, bool]:
        if self._writer is None:
            return '', False
        with self._lock:
            session = _LocalSession(self._writer)
            cursor = session.fetch_one(
                "SELECT meta_value FROM projection_meta WHERE meta_key=?",
                ('backfill_cursor',),
            )
            complete = session.fetch_one(
                "SELECT meta_value FROM projection_meta WHERE meta_key=?",
                ('backfill_complete',),
            )
        return (
            str(cursor['meta_value']) if cursor else '',
            bool(complete and complete['meta_value'] == '1'),
        )

    def set_backfill_state(self, cursor: str, complete: bool) -> None:
        def update(session: Session) -> None:
            for key, value in (
                ('backfill_cursor', cursor),
                ('backfill_complete', '1' if complete else '0'),
            ):
                session.execute(
                    "INSERT INTO projection_meta(meta_key,meta_value) "
                    "VALUES (?,?) ON CONFLICT(meta_key) DO UPDATE SET "
                    "meta_value=excluded.meta_value",
                    (key, value),
                )
        self._write(update)
        if complete:
            self._state = 'ready'

    def status(self) -> dict[str, Any]:
        cursor, complete = self.backfill_state()
        return {
            'state': self._state,
            'backend': 'local-sqlite',
            'database_bytes': self._database_bytes(),
            'max_bytes': self.max_bytes,
            'backfill_complete': complete,
            'backfill_cursor': cursor,
            'writes': self._writes,
            'last_error': self._last_error,
        }

    def close(self) -> None:
        self._state = 'closed'
        while True:
            try:
                self._read_pool.get_nowait().close()
            except queue.Empty:
                break
        with self._lock:
            if self._writer is not None:
                self._writer.close()
                self._writer = None


class AuthorityTurnSearchTarget:
    """Shared PostgreSQL projection tables, written outside user transactions."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self._state = 'warming'
        self._writes = 0
        self._last_error = ''

    def start(self) -> None:
        return None

    def _query(self, name: str, callback: Operation) -> Any:
        return self.backend.query(name, callback, time.monotonic() + 5.0)

    def _command(self, name: str, callback: Operation) -> Any:
        digest = hashlib.sha256(name.encode('utf-8')).hexdigest()
        result = self.backend.command(
            name, digest, None, 'maintenance', callback,
            time.monotonic() + 5.0, receipt_required=False)
        self._writes += 1
        return result

    def apply_turn(
        self, identity: Mapping[str, Any], snapshot: Mapping[str, Any] | None,
        generation_token: str,
    ) -> None:
        self._command('projection.turn_search.apply_turn', lambda session:
                      _apply_turn_to_session(
                          session, identity, snapshot, generation_token))

    def begin_conversation(
        self, identity: Mapping[str, Any], header: Mapping[str, Any] | None,
        generation_token: str,
    ) -> None:
        self._command('projection.turn_search.begin_conversation', lambda session:
                      _begin_conversation_in_session(
                          session, identity, header, generation_token))

    def apply_conversation_page(
        self, rows: list[Mapping[str, Any]], generation_token: str,
    ) -> None:
        self._command('projection.turn_search.apply_page', lambda session:
                      _apply_page_in_session(session, rows, generation_token))

    def finalize_conversation(
        self, identity: Mapping[str, Any], generation_token: str,
    ) -> None:
        self._command('projection.turn_search.finalize_conversation', lambda session:
                      _finalize_conversation_in_session(
                          session, identity, generation_token))

    def reset(self) -> None:
        def clear(session: Session) -> None:
            session.execute('DELETE FROM storage_search_turns')
            session.execute('DELETE FROM storage_search_conversations')
            session.execute(
                "DELETE FROM storage_meta WHERE meta_key IN (?,?)",
                ('turn_search_backfill_cursor_v1',
                 'turn_search_backfill_complete_v1'),
            )
        self._command('projection.turn_search.reset', clear)
        self._state = 'warming'

    def backfill_state(self) -> tuple[str, bool]:
        def read(session: Session):
            rows = session.fetch_all(
                "SELECT meta_key,meta_value FROM storage_meta "
                "WHERE meta_key IN (?,?)",
                ('turn_search_backfill_cursor_v1',
                 'turn_search_backfill_complete_v1'),
            )
            values = {str(row['meta_key']): str(row['meta_value']) for row in rows}
            return (
                values.get('turn_search_backfill_cursor_v1', ''),
                values.get('turn_search_backfill_complete_v1') == '1',
            )
        return self._query('projection.turn_search.backfill_state', read)

    def set_backfill_state(self, cursor: str, complete: bool) -> None:
        def update(session: Session) -> None:
            for key, value in (
                ('turn_search_backfill_cursor_v1', cursor),
                ('turn_search_backfill_complete_v1', '1' if complete else '0'),
            ):
                session.execute(
                    "INSERT INTO storage_meta(meta_key,meta_value) VALUES (?,?) "
                    "ON CONFLICT(meta_key) DO UPDATE SET "
                    "meta_value=excluded.meta_value",
                    (key, value),
                )
        self._command('projection.turn_search.backfill_checkpoint', update)
        if complete:
            self._state = 'ready'

    def status(self) -> dict[str, Any]:
        try:
            cursor, complete = self.backfill_state()
        except StorageError:
            cursor, complete = '', False
        return {
            'state': self._state,
            'backend': 'postgres-projection-tables',
            'backfill_complete': complete,
            'backfill_cursor': cursor,
            'writes': self._writes,
            'last_error': self._last_error,
        }

    def close(self) -> None:
        self._state = 'closed'


class TurnSearchProjectionRuntime:
    """Bounded worker coordinating authority reads and projection writes."""

    def __init__(
        self,
        backend: Backend,
        target: _ProjectionTarget,
        *,
        backfill_delay_s: float,
    ) -> None:
        self.backend = backend
        self.target = target
        self.backfill_delay_s = max(0.0, float(backfill_delay_s))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._target_close_lock = threading.Lock()
        self._target_closed = False
        self._backfill_after = 0.0
        self._metrics = {
            'dirty_applied': 0,
            'dirty_acked': 0,
            'backfill_scanned': 0,
            'backfill_indexed': 0,
            'oversize_skipped': 0,
            'failures': 0,
        }
        self._last_error = ''

    def start(self) -> None:
        self.target.start()
        self._backfill_after = time.monotonic() + self.backfill_delay_s
        self._thread = threading.Thread(
            target=self._run,
            name='turn-search-projection',
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            self._close_target_once()
            raise

    def wake(self) -> None:
        self._wake.set()

    def query(self, operation: Operation, deadline_at: float) -> Any:
        query = getattr(self.target, 'query', None)
        if query is None:
            return self.backend.query(
                'conversation.search.authority_projection',
                operation,
                deadline_at,
            )
        return query(operation, deadline_at)

    def _authority_query(self, name: str, callback: Operation) -> Any:
        return self.backend.query(name, callback, time.monotonic() + 5.0)

    def _authority_command(self, name: str, callback: Operation) -> Any:
        digest = hashlib.sha256(name.encode('utf-8')).hexdigest()
        return self.backend.command(
            name,
            digest,
            None,
            'maintenance',
            callback,
            time.monotonic() + 5.0,
            receipt_required=False,
        )

    def _list_dirty(self) -> list[Mapping[str, Any]]:
        return self._authority_query(
            'projection.turn_search.outbox_list',
            lambda session: session.fetch_all(
                "SELECT projection_name,entity_kind,user_id,entity_key,"
                "version_token,enqueued_at_ms FROM storage_projection_outbox "
                "WHERE projection_name=? ORDER BY enqueued_at_ms,entity_kind,"
                "user_id,entity_key LIMIT ?",
                (PROJECTION_NAME, _DIRTY_BATCH),
            ),
        )

    def _ack(self, dirty: Mapping[str, Any]) -> bool:
        changed = self._authority_command(
            'projection.turn_search.outbox_ack',
            lambda session: session.execute(
                "DELETE FROM storage_projection_outbox "
                "WHERE projection_name=? AND entity_kind=? AND user_id=? "
                "AND entity_key=? AND version_token=?",
                (
                    PROJECTION_NAME,
                    str(dirty['entity_kind']),
                    int(dirty['user_id']),
                    str(dirty['entity_key']),
                    str(dirty['version_token']),
                ),
            ),
        )
        acked = bool(changed)
        if acked:
            self._metrics['dirty_acked'] += 1
        return acked

    def _turn_snapshot(
        self, dirty: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        source = self._authority_query(
            'projection.turn_search.turn_source_size',
            lambda session: session.fetch_one(
                "SELECT length(projection_json) AS projection_units "
                "FROM storage_conversation_turns WHERE user_id=? AND turn_id=?",
                (int(dirty['user_id']), str(dirty['entity_key'])),
            ),
        )
        if source is None:
            return None
        if int(_field(source, 'projection_units') or 0) \
                > _SOURCE_TEXT_MAX_UNITS:
            self._metrics['oversize_skipped'] += 1
            return None
        return self._authority_query(
            'projection.turn_search.turn_snapshot',
            lambda session: session.fetch_one(
                "SELECT t.*,c.updated_at_ms AS conversation_updated_at_ms "
                "FROM storage_conversation_turns AS t "
                "JOIN storage_conversations AS c "
                "ON c.id=t.conversation_id AND c.user_id=t.user_id "
                "WHERE t.user_id=? AND t.turn_id=?",
                (int(dirty['user_id']), str(dirty['entity_key'])),
            ),
        )

    def _conversation_header(
        self, dirty: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        return self._authority_query(
            'projection.turn_search.conversation_header',
            lambda session: session.fetch_one(
                "SELECT id,user_id,updated_at_ms FROM storage_conversations "
                "WHERE user_id=? AND id=?",
                (int(dirty['user_id']), str(dirty['entity_key'])),
            ),
        )

    @staticmethod
    def _materialize_candidates(
        session: Session,
        *,
        candidates: list[Mapping[str, Any]],
        cursor: str,
    ) -> dict[str, Any]:
        rows: list[Mapping[str, Any]] = []
        total_bytes = 0
        next_cursor = cursor
        skipped = 0
        remaining = len(candidates) > _BACKFILL_ROWS
        for candidate in candidates[:_BACKFILL_ROWS]:
            candidate_cursor = str(candidate['candidate_cursor'])
            if int(_field(candidate, 'projection_units') or 0) \
                    > _SOURCE_TEXT_MAX_UNITS:
                skipped += 1
                next_cursor = candidate_cursor
                continue
            turn_id = str(candidate['turn_id'])
            row = session.fetch_one(
                "SELECT t.*,c.updated_at_ms AS conversation_updated_at_ms "
                "FROM storage_conversation_turns AS t "
                "JOIN storage_conversations AS c "
                "ON c.id=t.conversation_id AND c.user_id=t.user_id "
                "WHERE t.user_id=? AND t.conversation_id=? AND t.turn_id=?",
                (
                    int(candidate['user_id']),
                    str(candidate['conversation_id']),
                    turn_id,
                ),
            )
            if row is None:
                next_cursor = candidate_cursor
                continue
            raw = _field(row, 'projection_json')
            size = len(raw) if isinstance(raw, bytes) else len(
                str(raw or '').encode('utf-8'))
            if rows and total_bytes + size > _BACKFILL_BYTES:
                remaining = True
                break
            rows.append(row)
            total_bytes += size
            next_cursor = candidate_cursor
        return {
            'rows': rows,
            'cursor': next_cursor,
            'remaining': remaining,
            'bytes': total_bytes,
            'skipped': skipped,
        }

    def _conversation_page(
        self, dirty: Mapping[str, Any], cursor: str,
    ) -> dict[str, Any]:
        return self._authority_query(
            'projection.turn_search.conversation_page',
            lambda session: self._materialize_candidates(
                session,
                cursor=cursor,
                candidates=session.fetch_all(
                    "SELECT t.user_id,t.conversation_id,t.turn_id,"
                    "length(t.projection_json) AS projection_units,"
                    "t.turn_id AS candidate_cursor "
                    "FROM storage_conversation_turns AS t "
                    "WHERE t.user_id=? AND t.conversation_id=? "
                    "AND t.lane_id='main' "
                    "AND t.status NOT IN ('pending','running') "
                    "AND t.turn_id>? ORDER BY t.turn_id LIMIT ?",
                    (
                        int(dirty['user_id']),
                        str(dirty['entity_key']),
                        cursor,
                        _BACKFILL_ROWS + 1,
                    ),
                ),
            ),
        )

    @staticmethod
    def _decode_backfill_cursor(cursor: str) -> tuple[int, str, str]:
        try:
            value = orjson.loads(cursor)
            if (isinstance(value, list) and len(value) == 3
                    and isinstance(value[0], int)
                    and isinstance(value[1], str)
                    and isinstance(value[2], str)):
                return max(0, value[0]), value[1], value[2]
        except (TypeError, ValueError, orjson.JSONDecodeError):
            pass
        # Old v1 preview cursors contained only a turn id. Replaying from the
        # beginning is safe because every target write is an idempotent UPSERT.
        return 0, '', ''

    @staticmethod
    def _encode_backfill_cursor(row: Mapping[str, Any]) -> str:
        return orjson.dumps([
            int(row['user_id']),
            str(row['conversation_id']),
            str(row['turn_id']),
        ]).decode('utf-8')

    def _backfill_page(self, cursor: str) -> dict[str, Any]:
        cursor_user_id, cursor_conversation_id, cursor_turn_id = (
            self._decode_backfill_cursor(cursor))

        def read(session: Session) -> dict[str, Any]:
            candidates = session.fetch_all(
                "SELECT t.user_id,t.conversation_id,t.turn_id,"
                "length(t.projection_json) AS projection_units "
                "FROM storage_conversation_turns AS t "
                "WHERE t.lane_id='main' "
                "AND t.status NOT IN ('pending','running') AND ("
                "t.user_id>? OR (t.user_id=? AND t.conversation_id>?) OR "
                "(t.user_id=? AND t.conversation_id=? AND t.turn_id>?)) "
                "ORDER BY t.user_id,t.conversation_id,t.turn_id LIMIT ?",
                (
                    cursor_user_id,
                    cursor_user_id,
                    cursor_conversation_id,
                    cursor_user_id,
                    cursor_conversation_id,
                    cursor_turn_id,
                    _BACKFILL_ROWS + 1,
                ),
            )
            decorated = [
                {**dict(row), 'candidate_cursor': self._encode_backfill_cursor(row)}
                for row in candidates
            ]
            return self._materialize_candidates(
                session, candidates=decorated, cursor=cursor)

        return self._authority_query(
            'projection.turn_search.backfill_page',
            read,
        )

    def _process_turn(self, dirty: Mapping[str, Any]) -> None:
        snapshot = self._turn_snapshot(dirty)
        self.target.apply_turn(
            dirty, snapshot, str(dirty['version_token']))
        self._metrics['dirty_applied'] += 1
        self._ack(dirty)

    def _process_conversation(self, dirty: Mapping[str, Any]) -> None:
        token = str(dirty['version_token'])
        header = self._conversation_header(dirty)
        self.target.begin_conversation(dirty, header, token)
        if header is not None:
            cursor = ''
            while not self._stop.is_set():
                page = self._conversation_page(dirty, cursor)
                rows = list(page['rows'])
                if rows:
                    self.target.apply_conversation_page(rows, token)
                self._metrics['oversize_skipped'] += int(
                    page.get('skipped') or 0)
                next_cursor = str(page['cursor'])
                if not page['remaining']:
                    break
                if next_cursor == cursor:
                    raise StorageError(
                        'database_internal',
                        'Conversation search projection cursor stalled')
                cursor = next_cursor
                self._stop.wait(0.01)
            self.target.finalize_conversation(dirty, token)
        self._metrics['dirty_applied'] += 1
        self._ack(dirty)

    def _process_rebuild(self, dirty: Mapping[str, Any]) -> None:
        self.target.reset()
        self.target.set_backfill_state('', False)
        self._backfill_after = time.monotonic()
        self._metrics['dirty_applied'] += 1
        self._ack(dirty)

    def _drain_dirty(self) -> bool:
        rows = self._list_dirty()
        for dirty in rows:
            kind = str(dirty['entity_kind'])
            if kind == 'turn':
                self._process_turn(dirty)
            elif kind == 'conversation':
                self._process_conversation(dirty)
            elif kind == 'rebuild':
                self._process_rebuild(dirty)
        return bool(rows)

    def _advance_backfill(self) -> bool:
        cursor, complete = self.target.backfill_state()
        if complete:
            return False
        page = self._backfill_page(cursor)
        rows = list(page['rows'])
        indexed = 0
        for row in rows:
            try:
                self.target.apply_turn(
                    {'user_id': row['user_id'], 'entity_key': row['turn_id']},
                    row,
                    'backfill-v1',
                )
                indexed += 1
            except StorageError as exc:
                if exc.code == 'database_unavailable':
                    raise
                logger.warning(
                    '[turn-search] skipped malformed historical turn=%s: %s',
                    str(_field(row, 'turn_id') or '')[:36], exc.message)
        next_cursor = str(page['cursor'])
        done = not bool(page['remaining'])
        if not done and next_cursor == cursor:
            raise StorageError(
                'database_internal', 'Turn-search backfill cursor stalled')
        self.target.set_backfill_state(next_cursor, done)
        self._metrics['backfill_scanned'] += len(rows)
        self._metrics['backfill_scanned'] += int(page.get('skipped') or 0)
        self._metrics['backfill_indexed'] += indexed
        self._metrics['oversize_skipped'] += int(page.get('skipped') or 0)
        return not done

    def _close_target_once(self) -> None:
        with self._target_close_lock:
            if self._target_closed:
                return
            self.target.close()
            self._target_closed = True

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                did_work = False
                try:
                    did_work = self._drain_dirty()
                    if (
                        not did_work
                        and time.monotonic() >= self._backfill_after
                    ):
                        did_work = self._advance_backfill()
                    self._last_error = ''
                except StorageError as exc:
                    self._metrics['failures'] += 1
                    self._last_error = f'{exc.code}:{exc.message}'
                    if exc.code == 'database_unavailable' and (
                        'resource budget' in exc.message
                    ):
                        logger.error(
                            '[turn-search] projection paused: %s', exc.message)
                        self._stop.wait(_IDLE_POLL_SECONDS)
                    else:
                        logger.warning(
                            '[turn-search] projection retryable failure: %s',
                            exc.message)
                        self._stop.wait(_RETRY_SECONDS)
                except BaseException as exc:
                    self._metrics['failures'] += 1
                    self._last_error = type(exc).__name__
                    logger.exception('[turn-search] projection worker failed')
                    self._stop.wait(_RETRY_SECONDS)
                if did_work:
                    self._stop.wait(0.01)
                    continue
                self._wake.wait(_IDLE_POLL_SECONDS)
                self._wake.clear()
        finally:
            # The worker owns target shutdown. If an authority read is stuck in
            # a network filesystem call, the caller may stop waiting after its
            # bounded join, but it must not close local SQLite handles beneath
            # work that can still resume later.
            self._close_target_once()

    def status(self) -> dict[str, Any]:
        return {
            **self.target.status(),
            'worker_alive': bool(self._thread and self._thread.is_alive()),
            'metrics': dict(self._metrics),
            'last_worker_error': self._last_error,
        }

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is None:
            self._close_target_once()
            return
        thread.join(timeout=5.0)
        if thread.is_alive():
            self._last_error = 'shutdown_waiting_for_authority_io'
            logger.warning(
                '[turn-search] projection shutdown left one bounded daemon '
                'worker waiting for authority I/O; target close is deferred')
            return
        self._thread = None


__all__ = [
    'AuthorityTurnSearchTarget',
    'LocalSQLiteTurnSearchTarget',
    'PROJECTION_NAME',
    'TurnSearchProjectionRuntime',
]

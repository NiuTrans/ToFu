"""SQLite repository for the deterministic Git integration control plane.

The integration service owns Git decisions; this module owns its durable
state.  No caller receives a connection or supplies SQL.  Schema lifecycle,
driver construction, cross-host authority, writer reservation, retry,
rollback, compare-and-set transitions, and bounded event retention all live
behind this semantic boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import TypeVar

from lib.log import get_logger
from lib.database.sqlite_store_owner import assert_store_owner


logger = get_logger(__name__)
_T = TypeVar('_T')
_SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = 15_000
_RETRIES = 6
_schema_lock = threading.RLock()
_schema_ready: dict[str, tuple[int, int]] = {}
_writer_locks: dict[str, threading.RLock] = {}

_SCHEMA_STATEMENTS = (
    '''CREATE TABLE IF NOT EXISTS integration_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )''',
    '''CREATE TABLE IF NOT EXISTS integration_workspaces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_root TEXT NOT NULL,
        task_id TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        workspace_path TEXT NOT NULL,
        managed INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'running',
        base_sha TEXT NOT NULL DEFAULT '',
        checkpoint_sha TEXT NOT NULL DEFAULT '',
        candidate_sha TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(project_root, task_id)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_integration_ready
       ON integration_workspaces(state, updated_at)''',
    '''CREATE TABLE IF NOT EXISTS integration_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_root TEXT NOT NULL,
        task_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        detail TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_integration_events_project
       ON integration_events(project_root, id DESC)''',
)


class IntegrationStateError(RuntimeError):
    """A rejected durable-state transition."""


def _resolved(path: str | os.PathLike) -> str:
    resolved = str(Path(path).resolve())
    driver_guard = sys.modules.get('lib.database.sqlite_driver_guard')
    if driver_guard is not None:
        driver_guard.register_sqlite_driver_authority(resolved)
    return resolved


def register_store(path: str | os.PathLike) -> str:
    """Register the control authority before dynamic code can raw-open it."""
    return _resolved(path)


def _signature(path: str) -> tuple[int, int]:
    stat = os.stat(path)
    return int(stat.st_dev), int(stat.st_ino)


def _writer_lock(path: str) -> threading.RLock:
    with _schema_lock:
        return _writer_locks.setdefault(path, threading.RLock())


def _connect(path: str, *, create: bool) -> sqlite3.Connection:
    target = Path(path)
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
        database = path
        uri = False
    else:
        database = target.as_uri() + '?mode=ro'
        uri = True
    driver_guard = sys.modules.get('lib.database.sqlite_driver_guard')
    capability = (driver_guard.allow_sqlite_driver_connection(
        'integration control repository connection')
        if driver_guard is not None else nullcontext())
    with capability:
        conn = sqlite3.connect(
            database, uri=uri, timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f'PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _is_busy(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        'locked' in text or 'busy' in text)


def _begin_write(
    conn: sqlite3.Connection,
    path: str,
    *,
    purpose: str,
    operation: Callable[[sqlite3.Connection], _T],
) -> _T:
    for attempt in range(_RETRIES):
        assert_store_owner(path, purpose=purpose)
        acquired = False
        try:
            conn.execute('BEGIN IMMEDIATE')
            acquired = True
            result = operation(conn)
            conn.commit()
            return result
        except BaseException as exc:
            try:
                conn.rollback()
            except sqlite3.Error as rollback_exc:
                logger.debug(
                    '[IntegrationControl] rollback after write failure failed: %s',
                    rollback_exc)
            if acquired or not _is_busy(exc) or attempt + 1 >= _RETRIES:
                raise
            time.sleep(min(0.05 * (2 ** attempt), 0.8))
    raise RuntimeError('unreachable integration-control SQLite retry state')


def _schema_is_current(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT value FROM integration_meta WHERE key='schema_version'"
        ).fetchone()
        return bool(row and int(row['value']) == _SCHEMA_VERSION)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.debug('[IntegrationControl] schema probe needs migration: %s', exc)
        return False


def _set_wal(conn: sqlite3.Connection, path: str) -> None:
    for attempt in range(_RETRIES):
        assert_store_owner(
            path, purpose='integration control schema journal mode')
        try:
            conn.execute('PRAGMA journal_mode=WAL').fetchone()
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc) or attempt + 1 >= _RETRIES:
                raise
            time.sleep(min(0.05 * (2 ** attempt), 0.8))


def _ensure_schema(conn: sqlite3.Connection, path: str) -> None:
    try:
        signature = _signature(path)
    except FileNotFoundError as exc:
        logger.debug('[IntegrationControl] schema file not created yet: %s', exc)
        signature = (-1, -1)
    with _schema_lock:
        if _schema_ready.get(path) == signature and signature != (-1, -1):
            return
        if _schema_is_current(conn):
            _schema_ready[path] = _signature(path)
            return
        _set_wal(conn, path)

        def migrate(db: sqlite3.Connection) -> None:
            for statement in _SCHEMA_STATEMENTS:
                db.execute(statement)
            db.execute('''
                INSERT INTO integration_meta(key, value)
                VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            ''', (str(_SCHEMA_VERSION),))

        _begin_write(
            conn, path, purpose='integration control schema migration',
            operation=migrate)
        _schema_ready[path] = _signature(path)


def _read(
    db_path: str | os.PathLike,
    operation: Callable[[sqlite3.Connection], _T],
    *,
    default: _T,
) -> _T:
    path = _resolved(db_path)
    if not Path(path).is_file():
        return default
    conn = _connect(path, create=False)
    try:
        if not _schema_is_current(conn):
            return default
        return operation(conn)
    finally:
        conn.close()


def _write(
    db_path: str | os.PathLike,
    *,
    purpose: str,
    operation: Callable[[sqlite3.Connection], _T],
) -> _T:
    path = _resolved(db_path)
    with _writer_lock(path):
        conn = _connect(path, create=True)
        try:
            _ensure_schema(conn, path)
            return _begin_write(
                conn, path, purpose=purpose, operation=operation)
        finally:
            conn.close()


def _event(
    db: sqlite3.Connection,
    project_root: str,
    task_id: str,
    kind: str,
    message: str,
    detail: str,
    now: float,
) -> None:
    db.execute(
        'INSERT INTO integration_events '
        '(project_root, task_id, kind, message, detail, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        (project_root, task_id, kind, message[:500], detail[:4000], now),
    )
    db.execute(
        'DELETE FROM integration_events WHERE project_root=? AND id NOT IN '
        '(SELECT id FROM integration_events WHERE project_root=? '
        ' ORDER BY id DESC LIMIT 300)',
        (project_root, project_root),
    )


def initialize_store(db_path: str | os.PathLike) -> None:
    """Upgrade/create the control store during authorized server bootstrap."""
    _write(
        db_path, purpose='initialize integration control store',
        operation=lambda _db: None)


def _row(db: sqlite3.Connection, root: str, task_id: str) -> sqlite3.Row:
    row = db.execute(
        'SELECT * FROM integration_workspaces '
        'WHERE project_root=? AND task_id=?', (root, task_id),
    ).fetchone()
    if row is None:
        raise IntegrationStateError(f'Unknown integration task: {task_id}')
    return row


def register_workspace(
    db_path: str | os.PathLike,
    *,
    project_root: str,
    task_id: str,
    title: str,
    workspace_path: str,
    managed: bool,
    base_sha: str,
    now: float,
) -> None:
    def register(db: sqlite3.Connection) -> None:
        existing = db.execute(
            'SELECT state FROM integration_workspaces '
            'WHERE project_root=? AND task_id=?',
            (project_root, task_id),
        ).fetchone()
        if existing and existing['state'] in {'ready', 'integrating'}:
            raise IntegrationStateError(
                'This task already has an immutable checkpoint in the '
                'integration queue')
        db.execute(
            'INSERT INTO integration_workspaces '
            '(project_root, task_id, title, workspace_path, managed, state, '
            ' base_sha, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(project_root, task_id) DO UPDATE SET '
            ' title=excluded.title, workspace_path=excluded.workspace_path, '
            ' managed=excluded.managed, base_sha=excluded.base_sha, '
            " checkpoint_sha='', candidate_sha='', state='running', error='', "
            ' updated_at=excluded.updated_at',
            (project_root, task_id, title, workspace_path, int(managed),
             'running', base_sha, now, now),
        )
        _event(db, project_root, task_id, 'registered',
               'Writer workspace registered', workspace_path, now)

    _write(db_path, purpose='register integration workspace', operation=register)


def get_workspace(
    db_path: str | os.PathLike, project_root: str, task_id: str
) -> dict:
    result = _read(
        db_path,
        lambda db: dict(_row(db, project_root, task_id)),
        default=None,
    )
    if result is None:
        raise IntegrationStateError(f'Unknown integration task: {task_id}')
    return result


def save_checkpoint(
    db_path: str | os.PathLike,
    *,
    project_root: str,
    task_id: str,
    checkpoint_sha: str,
    now: float,
) -> None:
    def save(db: sqlite3.Connection) -> None:
        row = _row(db, project_root, task_id)
        if row['state'] in {'ready', 'integrating'}:
            raise IntegrationStateError(
                'The submitted checkpoint is immutable while it is in the '
                'integration queue')
        db.execute(
            "UPDATE integration_workspaces SET checkpoint_sha=?, "
            "state='checkpointed', error='', updated_at=? "
            'WHERE id=?', (checkpoint_sha, now, row['id']))
        _event(db, project_root, task_id, 'checkpointed',
               f'Checkpoint {checkpoint_sha[:12]} captured without staging '
               'the workspace', '', now)

    _write(db_path, purpose='save integration checkpoint', operation=save)


def submit_checkpoint(
    db_path: str | os.PathLike,
    *,
    project_root: str,
    task_id: str,
    now: float,
) -> None:
    def submit(db: sqlite3.Connection) -> None:
        row = _row(db, project_root, task_id)
        if not row['checkpoint_sha']:
            raise IntegrationStateError('Checkpoint the workspace before submitting')
        if row['state'] == 'integrating':
            raise IntegrationStateError('The checkpoint is already integrating')
        db.execute(
            "UPDATE integration_workspaces SET state='ready', error='', "
            'updated_at=? WHERE id=?', (now, row['id']))
        _event(db, project_root, task_id, 'submitted',
               'Checkpoint entered the deterministic integration queue', '', now)

    _write(db_path, purpose='submit integration checkpoint', operation=submit)


def retry_checkpoint(
    db_path: str | os.PathLike,
    *,
    project_root: str,
    task_id: str,
    now: float,
) -> None:
    def retry(db: sqlite3.Connection) -> None:
        row = _row(db, project_root, task_id)
        if row['state'] not in {'quarantined', 'failed'}:
            raise IntegrationStateError(
                'Only quarantined or failed checkpoints can be retried')
        if not row['checkpoint_sha']:
            raise IntegrationStateError('Checkpoint the workspace before retrying')
        db.execute(
            "UPDATE integration_workspaces SET state='ready', error='', "
            'updated_at=? WHERE id=?', (now, row['id']))
        _event(db, project_root, task_id, 'retried',
               'Quarantined checkpoint returned to the queue', '', now)

    _write(db_path, purpose='retry integration checkpoint', operation=retry)


def claim_next(db_path: str | os.PathLike, *, now: float) -> dict | None:
    def claim(db: sqlite3.Connection) -> dict | None:
        db.execute(
            "UPDATE integration_workspaces SET state='ready', "
            "error='Recovered an interrupted integration', updated_at=? "
            "WHERE state='integrating' AND updated_at < ?",
            (now, now - 660),
        )
        row = db.execute(
            "SELECT * FROM integration_workspaces WHERE state='ready' "
            'AND NOT EXISTS (SELECT 1 FROM integration_workspaces active '
            ' WHERE active.project_root=integration_workspaces.project_root '
            " AND active.state='integrating') "
            'ORDER BY updated_at ASC, id ASC LIMIT 1',
        ).fetchone()
        if row is None:
            return None
        changed = db.execute(
            "UPDATE integration_workspaces SET state='integrating', updated_at=? "
            "WHERE id=? AND state='ready'", (now, row['id']),
        ).rowcount
        if changed != 1:
            return None
        fresh = db.execute(
            'SELECT * FROM integration_workspaces WHERE id=?',
            (row['id'],),
        ).fetchone()
        return dict(fresh) if fresh is not None else None

    return _write(db_path, purpose='claim integration checkpoint', operation=claim)


def get_integrating(db_path: str | os.PathLike, row_id: int) -> dict | None:
    return _read(
        db_path,
        lambda db: (
            dict(row) if (row := db.execute(
                "SELECT * FROM integration_workspaces "
                "WHERE id=? AND state='integrating'", (int(row_id),)
            ).fetchone()) is not None else None
        ),
        default=None,
    )


def quarantine(
    db_path: str | os.PathLike,
    *,
    row_id: int,
    project_root: str,
    task_id: str,
    reason: str,
    now: float,
) -> bool:
    def mark(db: sqlite3.Connection) -> bool:
        changed = db.execute(
            "UPDATE integration_workspaces SET state='quarantined', error=?, "
            "updated_at=? WHERE id=? AND state='integrating'",
            (reason[:4000], now, int(row_id)),
        ).rowcount
        if changed == 1:
            _event(db, project_root, task_id, 'quarantined',
                   'Checkpoint needs attention', reason, now)
        return changed == 1

    return _write(db_path, purpose='quarantine integration checkpoint', operation=mark)


def requeue(
    db_path: str | os.PathLike,
    *,
    row_id: int,
    error: str,
    now: float,
) -> bool:
    return _write(
        db_path,
        purpose='requeue integration checkpoint',
        operation=lambda db: db.execute(
            "UPDATE integration_workspaces SET state='ready', error=?, "
            "updated_at=? WHERE id=? AND state='integrating'",
            (error[:4000], now, int(row_id)),
        ).rowcount == 1,
    )


def mark_merged(
    db_path: str | os.PathLike,
    *,
    row_id: int,
    project_root: str,
    task_id: str,
    candidate_sha: str,
    now: float,
) -> bool:
    def mark(db: sqlite3.Connection) -> bool:
        changed = db.execute(
            "UPDATE integration_workspaces SET state='merged', candidate_sha=?, "
            "error='', updated_at=? WHERE id=? AND state='integrating'",
            (candidate_sha, now, int(row_id)),
        ).rowcount
        if changed == 1:
            _event(db, project_root, task_id, 'merged',
                   f'Checkpoint integrated into candidate {candidate_sha[:12]}',
                   '', now)
        return changed == 1

    return _write(db_path, purpose='complete integration checkpoint', operation=mark)


def mark_failed(
    db_path: str | os.PathLike,
    *,
    row_id: int,
    project_root: str,
    task_id: str,
    error: str,
    now: float,
) -> bool:
    def fail(db: sqlite3.Connection) -> bool:
        changed = db.execute(
            "UPDATE integration_workspaces SET state='failed', error=?, "
            "updated_at=? WHERE id=? AND state='integrating'",
            (error[:4000], now, int(row_id)),
        ).rowcount
        if changed == 1:
            _event(db, project_root, task_id, 'failed',
                   'Integration worker failed', error, now)
        return changed == 1

    return _write(db_path, purpose='fail integration checkpoint', operation=fail)


def record_event(
    db_path: str | os.PathLike,
    *,
    project_root: str,
    task_id: str,
    kind: str,
    message: str,
    detail: str,
    now: float,
) -> None:
    _write(
        db_path,
        purpose=f'record integration event {kind}',
        operation=lambda db: _event(
            db, project_root, task_id, kind, message, detail, now),
    )


def status_rows(
    db_path: str | os.PathLike, project_root: str
) -> tuple[list[dict], list[dict]]:
    def load(db: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
        rows = db.execute(
            'SELECT * FROM integration_workspaces WHERE project_root=? '
            'ORDER BY updated_at DESC', (project_root,),
        ).fetchall()
        events = db.execute(
            'SELECT * FROM integration_events WHERE project_root=? '
            'ORDER BY id DESC LIMIT 30', (project_root,),
        ).fetchall()
        return [dict(row) for row in rows], [dict(row) for row in events]

    return _read(db_path, load, default=([], []))


__all__ = [
    'IntegrationStateError', 'claim_next', 'get_integrating', 'get_workspace',
    'initialize_store',
    'mark_failed', 'mark_merged', 'quarantine', 'record_event',
    'register_store', 'register_workspace', 'requeue', 'retry_checkpoint',
    'save_checkpoint', 'status_rows', 'submit_checkpoint',
]

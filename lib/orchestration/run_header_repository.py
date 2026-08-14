"""Database repository for durable orchestration run headers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.log import get_logger
from lib.orchestration.run_status import (
    INITIAL_RUN_STATUS,
    TERMINAL_RUN_STATUSES,
    is_run_status,
    is_terminal_run_status,
)
from lib.orchestration.run_store_codec import (
    encode_run_json,
    row_to_run_header,
)
from lib.orchestration.run_repository_call import (
    run_store_attempt,
    run_store_require,
)
from lib.orchestration.run_store_port import OrchestrationRunStoreError


logger = get_logger(__name__)


class OrchestrationRunHeaderRepository:
    """Own header SQL and lifecycle fencing, independent of event replay."""

    def __init__(self, database: Callable[[], Any], clock: Callable[[], int]):
        self._database = database
        self._clock = clock

    def create(self, run_id: str, *, definition: dict,
               input_text: str = '', orch_id: str = '', name: str = '',
               created_by: str = '') -> bool:
        if not run_id:
            return False
        db = self._database()
        if db is None:
            return False
        now = self._clock()
        def write():
            from lib.database import db_execute_with_retry
            db_execute_with_retry(
                db,
                'INSERT INTO orchestration_runs (id, orch_id, name, '
                'definition, input, status, created_by, created_at, '
                'updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (run_id, orch_id or '', name or '',
                 encode_run_json(definition or {}), input_text or '',
                 INITIAL_RUN_STATUS, created_by or '', now, now),
            )
            logger.debug('[OrchRuns] created run id=%s orch=%s',
                         run_id, orch_id)
            return True

        return run_store_attempt(
            f'create_run({run_id})', write, fallback=False)

    def update_status(self, run_id: str, status: str, *,
                      final: str | None = None,
                      error: dict | str | None = None) -> bool:
        """Write a lifecycle transition behind an immutable terminal fence."""
        if not run_id or not is_run_status(status):
            if status:
                logger.warning(
                    '[OrchRuns] rejected unknown run status %r', status)
            return False
        db = self._database()
        if db is None:
            return False
        now = self._clock()
        def write():
            from lib.database import db_execute_with_retry
            error_text = '' if error is None else (
                error if isinstance(error, str) else encode_run_json(error))
            assignments = ['status=?']
            params: list = [status]
            if final is not None:
                assignments.append('final=?')
                params.append(final)
            if error is not None:
                assignments.append('error=?')
                params.append(error_text)
            assignments.append('updated_at=?')
            params.append(now)
            if is_terminal_run_status(status):
                assignments.append(
                    'finished_at=CASE WHEN COALESCE(finished_at, 0)=0 '
                    'THEN ? ELSE finished_at END')
                params.append(now)
            else:
                assignments.append('finished_at=0')
            terminal = tuple(sorted(TERMINAL_RUN_STATUSES))
            placeholders = ','.join('?' for _ in terminal)
            params.extend([run_id, *terminal, status])
            cursor = db_execute_with_retry(
                db,
                'UPDATE orchestration_runs SET ' + ', '.join(assignments)
                + ' WHERE id=? AND (status NOT IN (' + placeholders
                + ') OR status=?)',
                tuple(params),
                return_cursor=True,
            )
            changed = bool(cursor is not None and cursor.rowcount > 0)
            if changed:
                logger.debug('[OrchRuns] run %s → %s', run_id, status)
            else:
                logger.debug(
                    '[OrchRuns] terminal transition fenced run=%s target=%s',
                    run_id, status,
                )
            return changed

        return run_store_attempt(
            f'update_status({run_id})', write, fallback=False)

    def retire_interrupted(self, error: dict | str) -> int | None:
        db = self._database()
        if db is None:
            return None
        now = self._clock()
        terminal = tuple(sorted(TERMINAL_RUN_STATUSES))
        placeholders = ','.join('?' for _ in terminal)
        def write():
            from lib.database import db_execute_with_retry
            error_text = error if isinstance(error, str) \
                else encode_run_json(error)
            cursor = db_execute_with_retry(
                db,
                "UPDATE orchestration_runs SET status='error', final='', "
                'error=?, updated_at=?, finished_at=CASE '
                'WHEN COALESCE(finished_at, 0)=0 THEN ? ELSE finished_at END '
                'WHERE status NOT IN (' + placeholders + ')',
                (error_text, now, now, *terminal),
                return_cursor=True,
            )
            retired = max(0, int(cursor.rowcount)) \
                if cursor is not None else 0
            if retired:
                logger.warning(
                    '[OrchRuns] retired %d interrupted run(s)', retired)
            return retired

        return run_store_attempt(
            'retire_interrupted_runs', write, fallback=None)

    def get(self, run_id: str) -> dict | None:
        if not run_id:
            return None
        db = self._database()
        if db is None:
            raise OrchestrationRunStoreError(
                'orchestration run store unavailable')
        row = run_store_require(
            f'get_run({run_id})',
            f'failed to read orchestration run {run_id}',
            lambda: db.execute(
                'SELECT id, orch_id, name, definition, input, status, final, '
                'error, created_by, created_at, updated_at, finished_at '
                'FROM orchestration_runs WHERE id=?',
                (run_id,),
            ).fetchone(),
        )
        return row_to_run_header(row, include_definition=True) if row else None

    def list(self, *, status: str = '', orch_id: str = '',
             limit: int = 50) -> list[dict]:
        if status and not is_run_status(status):
            return []
        db = self._database()
        if db is None:
            raise OrchestrationRunStoreError(
                'orchestration run store unavailable')
        limit = max(1, min(int(limit or 50), 200))
        where, params = [], []
        if status:
            where.append('status=?')
            params.append(status)
        if orch_id:
            where.append('orch_id=?')
            params.append(orch_id)
        clause = (' WHERE ' + ' AND '.join(where)) if where else ''
        rows = run_store_require(
            'list_runs',
            'failed to list orchestration runs',
            lambda: db.execute(
                'SELECT id, orch_id, name, status, final, error, created_by, '
                'created_at, updated_at, finished_at FROM orchestration_runs'
                + clause + ' ORDER BY created_at DESC, id DESC LIMIT '
                + str(limit),
                tuple(params),
            ).fetchall(),
        )
        return [
            row_to_run_header(row, include_definition=False) for row in rows
        ]

__all__ = ['OrchestrationRunHeaderRepository']

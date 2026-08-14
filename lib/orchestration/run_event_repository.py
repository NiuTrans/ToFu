"""Database repository for append-only durable orchestration run events."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.orchestration.run_repository_call import (
    run_store_attempt,
    run_store_require,
)
from lib.orchestration.run_store_codec import (
    decode_run_json,
    encode_run_json,
)
from lib.orchestration.run_store_port import (
    ORCHESTRATION_RUN_EVENT_PAGE_LIMIT,
    OrchestrationRunStoreError,
    RunEventPage,
)
from lib.task_replay import TASK_REPLAY_EVENT_SEQUENCE_FIELD

class OrchestrationRunEventRepository:
    """Own append, cursor-boundary replay and event cleanup SQL."""

    def __init__(self, database: Callable[[], Any], clock: Callable[[], int]):
        self._database = database
        self._clock = clock

    def append(self, run_id: str, seq: int, event: dict) -> bool:
        if not run_id or seq is None:
            return False
        db = self._database()
        if db is None:
            return False
        def write():
            from lib.database import db_execute_with_retry
            db_execute_with_retry(
                db,
                'INSERT OR IGNORE INTO orchestration_run_events '
                '(run_id, seq, type, node_id, payload, ts) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (run_id, int(seq), str(event.get('type') or ''),
                 str(event.get('node_id') or ''), encode_run_json(event),
                 self._clock()),
            )
            return True

        return run_store_attempt(
            f'append_event({run_id}/{seq})', write, fallback=False)

    def page(self, run_id: str, cursor: int = 0) -> RunEventPage:
        """Read rows and their authoritative next-cursor in one statement."""
        if not run_id:
            return [], 0, False
        db = self._database()
        if db is None:
            raise OrchestrationRunStoreError(
                'orchestration run store unavailable')
        requested = max(0, int(cursor or 0))
        def read():
            rows = db.execute(
                'WITH boundary AS ('
                ' SELECT COALESCE(MAX(seq) + 1, 0) AS next_cursor'
                ' FROM orchestration_run_events WHERE run_id=?'
                ') SELECT e.seq, e.payload, b.next_cursor'
                ' FROM boundary b LEFT JOIN orchestration_run_events e'
                ' ON e.run_id=? AND e.seq>=CASE WHEN ? < b.next_cursor'
                ' THEN ? ELSE b.next_cursor END ORDER BY e.seq ASC LIMIT ?',
                (run_id, run_id, requested, requested,
                 ORCHESTRATION_RUN_EVENT_PAGE_LIMIT),
            ).fetchall()
            return rows

        rows = run_store_require(
            f'get_event_page({run_id})',
            f'failed to replay orchestration run {run_id}',
            read,
        )
        boundary = int(rows[0]['next_cursor'] or 0) if rows else 0
        events = []
        for row in rows:
            if row['seq'] is None:
                continue
            event = decode_run_json(row['payload'], None, strict=True)
            if not isinstance(event, dict):
                raise OrchestrationRunStoreError(
                    'durable orchestration event is not an object')
            event.setdefault(TASK_REPLAY_EVENT_SEQUENCE_FIELD, row['seq'])
            events.append(event)
        next_cursor = boundary
        if len(events) >= ORCHESTRATION_RUN_EVENT_PAGE_LIMIT:
            next_cursor = min(
                boundary,
                int(events[-1][TASK_REPLAY_EVENT_SEQUENCE_FIELD]) + 1,
            )
        return RunEventPage(
            events=events,
            next_cursor=next_cursor,
            cursor_reset=requested > boundary,
            caught_up=next_cursor >= boundary,
        )

__all__ = ['OrchestrationRunEventRepository']

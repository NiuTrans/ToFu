"""Atomic durable event plus nonterminal run-header projection."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.orchestration.run_repository_call import run_store_attempt
from lib.orchestration.run_status import (
    TERMINAL_RUN_STATUSES,
    is_run_status,
    is_terminal_run_status,
)
from lib.orchestration.run_store_codec import encode_run_json


class OrchestrationRunProjectionRepository:
    """Commit one replay fact and its implied header state atomically."""

    def __init__(self, database: Callable[[], Any], clock: Callable[[], int]):
        self._database = database
        self._clock = clock

    def project(
        self,
        run_id: str,
        seq: int,
        event: dict,
        status: str = '',
    ) -> bool:
        if (not run_id or seq is None or not isinstance(event, dict)
                or (status and (not is_run_status(status)
                                or is_terminal_run_status(status)))):
            return False
        db = self._database()
        if db is None:
            return False
        now = self._clock()
        event_payload = encode_run_json(event)

        def write() -> bool:
            from lib.database import write_transaction
            with write_transaction(
                db, label=f'orchestration projection {run_id}/{seq}',
            ):
                inserted = db.execute(
                    'INSERT OR IGNORE INTO orchestration_run_events '
                    '(run_id, seq, type, node_id, payload, ts) '
                    'VALUES (?, ?, ?, ?, ?, ?)',
                    (run_id, int(seq), str(event.get('type') or ''),
                     str(event.get('node_id') or ''),
                     event_payload, now),
                )
                if inserted is None or inserted.rowcount <= 0:
                    existing = db.execute(
                        'SELECT payload FROM orchestration_run_events '
                        'WHERE run_id=? AND seq=?',
                        (run_id, int(seq)),
                    ).fetchone()
                    if isinstance(existing, (tuple, list)):
                        existing_payload = existing[0]
                    else:
                        existing_payload = (
                            existing['payload'] if existing is not None
                            else None
                        )
                    if existing_payload == event_payload:
                        return True
                    raise RuntimeError(
                        'durable event sequence rejected conflicting payload')
                terminal = tuple(sorted(TERMINAL_RUN_STATUSES))
                placeholders = ','.join('?' for _ in terminal)
                if status:
                    cursor = db.execute(
                        'UPDATE orchestration_runs SET status=?, '
                        'updated_at=?, finished_at=0 WHERE id=? AND status '
                        'NOT IN (' + placeholders + ')',
                        (status, now, run_id, *terminal),
                    )
                else:
                    cursor = db.execute(
                        'UPDATE orchestration_runs SET updated_at=? '
                        'WHERE id=? AND status NOT IN ('
                        + placeholders + ')',
                        (now, run_id, *terminal),
                    )
                if cursor is None or cursor.rowcount <= 0:
                    raise RuntimeError(
                        'durable run header rejected event projection')
            return True

        return run_store_attempt(
            f'project_event({run_id}/{seq}, status={status or "unchanged"})',
            write,
            fallback=False,
        )


__all__ = ['OrchestrationRunProjectionRepository']

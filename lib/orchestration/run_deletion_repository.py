"""Atomic aggregate deletion for durable orchestration runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.orchestration.run_repository_call import run_store_attempt


class OrchestrationRunDeletionRepository:
    """Own the cross-table transaction for deleting one durable run.

    Header lifecycle and event replay remain separate repositories. Deletion
    is the aggregate boundary where both tables must commit or roll back as a
    unit, so its SQL and transaction ownership live together here.
    """

    def __init__(self, database: Callable[[], Any]):
        self._database = database

    def delete(self, run_id: str) -> bool:
        if not run_id:
            return False
        db = self._database()
        if db is None:
            return False
        def write():
            from lib.database import write_transaction
            with write_transaction(
                db,
                label='delete durable orchestration run aggregate',
            ):
                db.execute(
                    'DELETE FROM orchestration_run_events WHERE run_id=?',
                    (run_id,),
                )
                header = db.execute(
                    'DELETE FROM orchestration_runs WHERE id=?',
                    (run_id,),
                )
                return bool(getattr(header, 'rowcount', 0) or 0)

        return run_store_attempt(
            f'atomic delete_run({run_id})', write, fallback=False)


__all__ = ['OrchestrationRunDeletionRepository']

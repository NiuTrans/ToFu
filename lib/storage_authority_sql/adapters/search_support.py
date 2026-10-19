"""Search dialect support inside the adapter boundary; no corpus caches."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import time

from lib.storage_authority_sql.adapters.base import Session

ACCELERATOR_VM_STEPS = 500_000


class AcceleratorBudgetExceeded(Exception):
    """Retry via resumable chunk scanning, not the same expensive SQL."""


def prepare_search_session(session: Session) -> None:
    if session.backend == 'sqlite':
        session.connection.create_function(  # type: ignore[attr-defined]
            'tofu_search_lower', 1, lambda value: str(value or '').lower(),
            deterministic=True,
        )


@contextmanager
def accelerator_budget(session: Session):
    """Bound optional FTS work, preserving the enclosing adapter deadline."""
    connection = session.connection  # type: ignore[attr-defined]
    deadline = getattr(session, 'search_deadline_at', float('inf'))
    steps = 0
    exhausted = False

    def progress():
        nonlocal steps, exhausted
        steps += 1000
        exhausted = steps >= ACCELERATOR_VM_STEPS
        return int(exhausted or time.monotonic() >= deadline)

    connection.set_progress_handler(progress, 1000)
    try:
        yield
    except sqlite3.OperationalError as exc:
        if exhausted and 'interrupted' in str(exc) and time.monotonic() < deadline:
            raise AcceleratorBudgetExceeded() from exc
        raise
    finally:
        connection.set_progress_handler(
            (lambda: int(time.monotonic() >= deadline)) if deadline != float('inf') else None, 1000)

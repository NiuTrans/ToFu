"""Bounded SQLite maintenance operations owned by the data layer."""

from __future__ import annotations

import time

from lib.log import get_logger


logger = get_logger(__name__)


def incremental_vacuum(db, *, max_pages: int, min_free_pages: int,
                       budget_ms: int, monotonic=time.monotonic) -> int:
    """Return at most ``max_pages`` free pages within a wall-time budget.

    ``PRAGMA incremental_vacuum`` does not open a durable user transaction and
    the shipped SQLite build commonly releases one tail page per call.  This
    operation therefore holds the data layer's writer lane once around the
    bounded raw loop.  Raw access is intentionally encapsulated here so
    application subsystems cannot bypass writer ownership.
    """
    max_pages = max(0, int(max_pages))
    min_free_pages = max(0, int(min_free_pages))
    budget_ms = max(0, int(budget_ms))
    if max_pages == 0 or budget_ms == 0:
        return 0

    from lib.database import _core
    if _core._BACKEND != 'sqlite':
        return 0

    mode_row = db.execute('PRAGMA auto_vacuum').fetchone()
    if not mode_row or int(mode_row[0]) != 2:
        return 0
    before_row = db.execute('PRAGMA freelist_count').fetchone()
    before = int(before_row[0]) if before_row else 0
    if before < min_free_pages:
        return 0

    pages = min(before, max_pages)
    deadline = monotonic() + budget_ms / 1000.0
    after = before
    db._acquire_write_lane('bounded SQLite incremental vacuum')
    try:
        raw = db.raw
        while before - after < pages and monotonic() < deadline:
            remaining = pages - (before - after)
            raw.execute(f'PRAGMA incremental_vacuum({remaining})')
            next_after = int(
                raw.execute('PRAGMA freelist_count').fetchone()[0])
            if next_after >= after:
                break
            after = next_after
    finally:
        # These maintenance pragmas do not represent caller-owned state.
        db._dirty = False
        db._release_write_lane_if_transaction_ended()

    reclaimed = max(0, before - after)
    if reclaimed:
        logger.info(
            '[DB] SQLite incremental vacuum returned %d page(s) '
            '(freelist %d -> %d, budget=%dms)',
            reclaimed, before, after, budget_ms)
    return reclaimed


__all__ = ['incremental_vacuum']

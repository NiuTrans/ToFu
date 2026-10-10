"""Content-free compaction stage timings; no retained state or new events.

The pipeline owns stage boundaries. This observer logs slow/failing work at
INFO and short successful work at DEBUG, never intercepting execution errors.
"""
from contextlib import contextmanager
import time

from lib.log import get_logger

logger = get_logger(__name__)


@contextmanager
def compaction_stage_timing(stage: str, task: dict | None, round_num: int):
    started_at = time.monotonic()
    status = 'error'
    try:
        yield
        status = 'done'
    finally:
        elapsed_ms = max(0.0, time.monotonic() - started_at) * 1000
        try:
            emit = logger.info if elapsed_ms >= 1000 or status != 'done' else logger.debug
            emit('[CompactionTiming] task=%s round=%s stage=%s status=%s elapsed_ms=%.1f',
                 str(task.get('id') or '?')[:8] if task else '?',
                 round_num, stage, status, elapsed_ms)
        except Exception as e:
            # This diagnostic must not mask a compaction/cancellation error.
            logger.debug('[CompactionTiming] emit dropped: %s', e)

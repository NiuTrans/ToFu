"""lib/timeutil.py — Shared time helpers used across the lib/ layer.

Small, dependency-free time utilities that multiple modules need. Kept in a
dedicated module (rather than lib/utils.py) so the helper has a clear,
narrowly-scoped home and no import-cycle risk.
"""

import time

__all__ = ['now_ms']


def now_ms() -> int:
    """Return the current wall-clock time in integer milliseconds since the epoch."""
    return int(time.time() * 1000)

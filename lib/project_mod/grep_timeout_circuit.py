"""Owner-scoped cooldown for directory grep scans proven too slow.

Responsibility: remember only that one ``(owner, target, include)`` scan timed
out, then reject equivalent live directory walks for a short bounded window.
The module never caches paths, patterns, file contents, or search results.

Entry points are :func:`check`, :func:`record_timeout`, and
:func:`record_success`. ``read_tools.tool_grep`` owns policy placement: an
available tree index is always tried before this circuit, and a narrower path
or different include glob is a different key. Dependencies are deliberately
limited to the standard library so this resource guard stays usable from every
project-tool execution path.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass


_DEFAULT_COOLDOWN_SECONDS = 300.0
_MAX_COOLDOWN_SECONDS = 900.0
_DEFAULT_ENTRY_CAPACITY = 256
_MAX_ENTRY_CAPACITY = 1024


@dataclass(frozen=True)
class CircuitDecision:
    """Evidence returned for one fast rejection."""

    remaining_seconds: float
    rejection_count: int

    @property
    def should_log(self) -> bool:
        """Log first/power-of-two rejections without unbounded log growth."""
        count = self.rejection_count
        return count == 1 or count & (count - 1) == 0


@dataclass
class _CircuitEntry:
    blocked_until: float
    rejection_count: int = 0


_lock = threading.RLock()
_entries: OrderedDict[str, _CircuitEntry] = OrderedDict()


def _env_float(name: str, default: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, '') or default)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(maximum, value))


def _env_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, '') or default)
    except (TypeError, ValueError):
        return default
    return max(1, min(maximum, value))


def cooldown_seconds() -> float:
    """Configured cooldown; zero is the explicit circuit kill switch."""
    return _env_float(
        'TOFU_GREP_TIMEOUT_COOLDOWN_S',
        _DEFAULT_COOLDOWN_SECONDS,
        _MAX_COOLDOWN_SECONDS,
    )


def entry_capacity() -> int:
    """Process-wide retained-state capacity with a non-bypassable ceiling."""
    return _env_int(
        'TOFU_GREP_TIMEOUT_CIRCUIT_ENTRIES',
        _DEFAULT_ENTRY_CAPACITY,
        _MAX_ENTRY_CAPACITY,
    )


def _key(owner_user_id, target: str, include: str | None) -> str | None:
    """Return a fixed-width key, or None when no explicit owner was supplied."""
    if owner_user_id is None or str(owner_user_id).strip() == '':
        return None
    normalized_target = os.path.normcase(os.path.abspath(target))
    material = '\0'.join((str(owner_user_id), normalized_target, str(include or '')))
    return hashlib.sha256(material.encode('utf-8', 'surrogateescape')).hexdigest()


def _prune(now: float) -> None:
    expired = [key for key, entry in _entries.items()
               if entry.blocked_until <= now]
    for key in expired:
        _entries.pop(key, None)
    capacity = entry_capacity()
    while len(_entries) > capacity:
        _entries.popitem(last=False)


def check(owner_user_id, target: str, include: str | None = None,
          *, now: float | None = None) -> CircuitDecision | None:
    """Return cooldown evidence for an equivalent live scan, else None.

    Missing owner identity intentionally disables the shared state. Production
    dispatch passes the authenticated task owner; legacy/direct callers retain
    their historical behavior rather than sharing an anonymous circuit.
    """
    key = _key(owner_user_id, target, include)
    if key is None or cooldown_seconds() <= 0:
        return None
    observed_at = time.monotonic() if now is None else now
    with _lock:
        _prune(observed_at)
        entry = _entries.get(key)
        if entry is None:
            return None
        entry.rejection_count += 1
        _entries.move_to_end(key)
        return CircuitDecision(
            remaining_seconds=max(0.0, entry.blocked_until - observed_at),
            rejection_count=entry.rejection_count,
        )


def record_timeout(owner_user_id, target: str, include: str | None = None,
                   *, now: float | None = None) -> None:
    """Open or extend the bounded cooldown after a real backend timeout."""
    key = _key(owner_user_id, target, include)
    cooldown = cooldown_seconds()
    if key is None or cooldown <= 0:
        return
    observed_at = time.monotonic() if now is None else now
    with _lock:
        _prune(observed_at)
        _entries[key] = _CircuitEntry(blocked_until=observed_at + cooldown)
        _entries.move_to_end(key)
        _prune(observed_at)


def record_success(owner_user_id, target: str, include: str | None = None) -> None:
    """Clear old slow-root evidence after an equivalent search succeeds."""
    key = _key(owner_user_id, target, include)
    if key is None:
        return
    with _lock:
        _entries.pop(key, None)


def snapshot(*, now: float | None = None) -> dict[str, int | float]:
    """Return content-free resource evidence for tests and diagnostics."""
    observed_at = time.monotonic() if now is None else now
    with _lock:
        _prune(observed_at)
        return {
            'activeEntries': len(_entries),
            'entryCapacity': entry_capacity(),
            'cooldownSeconds': cooldown_seconds(),
            'fastRejections': sum(
                entry.rejection_count for entry in _entries.values()),
        }


def _reset_for_tests() -> None:
    with _lock:
        _entries.clear()

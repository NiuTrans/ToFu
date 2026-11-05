"""Pricing state revision: one monotonic stamp for every runtime price input.

Responsibility: count each change to the live inputs that cost arithmetic
reads — the published exchange/default rates (``_refresh``), the provider
override registry (``_provider``) and the global model / legacy tier tables
(``_tables``) — so a pure cost memo (``lib.cost.compute_api_rounds_cost``) can
prove that a settled round's cached cost is still current without pricing it
again. Readers take no lock.

Protocol: a writer publishes its new data first and bumps afterwards; a
reader reads the revision before reading any data and stores its result
under that revision. A reader that races a writer therefore stores the old
revision and recomputes on its next call; it can never keep a stale price.
Pricing rows are replaced, never edited in place; ``RevisionedTable`` stamps
every top-level write of a shared table, including test patches. The peak
schedule is time, not state: cost memos also key on the multiplier in force.

Dependencies: standard library only.
"""

from __future__ import annotations

import threading

_revision_lock = threading.Lock()
_revision = 0


def pricing_revision() -> int:
    """Return the current pricing revision (read it before reading prices)."""
    return _revision


def bump_pricing_revision() -> int:
    """Record that pricing inputs changed (call after publishing them)."""
    global _revision
    with _revision_lock:
        _revision += 1
        return _revision


class RevisionedTable(dict):
    """A shared pricing table whose every top-level write bumps the revision."""

    __slots__ = ()

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        bump_pricing_revision()

    def __delitem__(self, key):
        dict.__delitem__(self, key)
        bump_pricing_revision()

    def __ior__(self, other):
        dict.update(self, other)
        bump_pricing_revision()
        return self

    def clear(self):
        dict.clear(self)
        bump_pricing_revision()

    def pop(self, *args):
        value = dict.pop(self, *args)
        bump_pricing_revision()
        return value

    def popitem(self):
        item = dict.popitem(self)
        bump_pricing_revision()
        return item

    def setdefault(self, key, default=None):
        value = dict.setdefault(self, key, default)
        bump_pricing_revision()
        return value

    def update(self, *args, **kwargs):
        dict.update(self, *args, **kwargs)
        bump_pricing_revision()

    def __reduce_ex__(self, _protocol):
        return dict, (dict(self),)


__all__ = ['RevisionedTable', 'bump_pricing_revision', 'pricing_revision']

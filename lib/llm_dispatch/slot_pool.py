"""lib/llm_dispatch/slot_pool.py — The dispatcher's live slots and their selection scopes.

Responsibility
--------------
Hold every live :class:`~lib.llm_dispatch.slot.Slot` (configured and
request-scoped) in publication order, indexed by dispatcher ``provider_id`` so
a selection scans only the slots its execution context may use:

* a provider-pinned context (``provider_pin``) sees ``scope(pin)``: its own
  request-scoped route group, or one configured provider's slots;
* an unpinned context sees only configured slots. A request-scoped slot
  belongs to the route group that minted it (its credentials and lifetime are
  that request's) and never serves another caller's unpinned dispatch.

The pool also indexes the credentials of live slots per key-health namespace
(``Slot.key_stats_provider_id()``, owner + Provider): the last-resort
siblings ``lib.key_stats`` reads for every key admission. Several slots share
one credential, so each credential is counted by its live slots and leaves
the index when the last of them is withdrawn.

Entry points
------------
``scope(pin)``, ``by_provider(provider_id)``, ``key_stats_key_names(namespace)``,
``publish(slots)``, ``withdraw(slots)`` and ``replace_configured(slots)``. The
dispatcher holds its lock for every mutation and for scope reads that
selection must see consistently. ``scope``/``by_provider`` return live lists:
copy them before using them outside that lock. ``key_stats_key_names`` returns
an immutable value that is replaced, never edited, so it may be read without
the lock. ``provider_id`` is the index key; it is never changed on a pooled
slot.

An ordered occurrence map owns membership; an identity index locates every
occurrence of a retiring slot without scanning other requests. List-style reads
and mutations remain available. Generic mutations rebuild the indexes; request
publication/withdrawal update only the affected entries and provider scopes.
Iteration snapshots membership so diagnostics can safely outlive the caller's
lock. Dependencies: standard collections and Slot attributes only.
"""

from __future__ import annotations

from collections.abc import Iterable, MutableSequence

__all__ = ['SlotPool']


class _KeyHealthCredentials:
    """Credential names of live slots per key-health namespace, with counts.

    Each slot is counted under the identity it had when it entered the pool,
    so withdrawing it reverses exactly what publishing it added.
    """

    __slots__ = ('_counts', '_names', '_identity')

    def __init__(self):
        self._counts: dict[str, dict[str, int]] = {}
        self._names: dict[str, tuple[str, ...]] = {}
        self._identity: dict[int, tuple[str, str]] = {}

    def names(self, namespace: str) -> tuple[str, ...]:
        return self._names.get(namespace, ())

    def compact(self) -> None:
        """Release retired dictionary capacity at request-pool quiescence."""
        self._identity = dict(self._identity)
        self._counts = {namespace: dict(counts) for namespace, counts in self._counts.items()}
        self._names = dict(self._names)

    def add(self, slots: Iterable) -> None:
        """Count *slots*; their identities are read before anything changes."""
        counted = [
            (slot, self._identity.get(id(slot))
             or (slot.key_stats_provider_id(), slot.key_stats_key_name()))
            for slot in slots]
        changed: set[str] = set()
        for slot, identity in counted:
            self._identity[id(slot)] = identity
            self._count(identity, 1, changed)
        self._publish(changed)

    def remove(self, occurrences: list) -> None:
        """Uncount every removed occurrence, then forget those slots."""
        changed: set[str] = set()
        for slot in occurrences:
            self._count(self._identity[id(slot)], -1, changed)
        for slot in occurrences:
            self._identity.pop(id(slot), None)
        self._publish(changed)

    def _count(self, identity: tuple[str, str], step: int, changed: set[str]) -> None:
        namespace, name = identity
        if not name:
            return
        counts = self._counts.setdefault(namespace, {})
        before = counts.get(name, 0)
        after = before + step
        if after:
            counts[name] = after
        else:
            del counts[name]
        if not before or not after:
            changed.add(namespace)

    def _publish(self, changed: set[str]) -> None:
        for namespace in changed:
            counts = self._counts.get(namespace)
            if counts:
                self._names[namespace] = tuple(sorted(counts))
            else:
                self._counts.pop(namespace, None)
                self._names.pop(namespace, None)


class SlotPool(MutableSequence):
    """All live slots, plus per-provider, configured-only and key-health views."""

    def __init__(self, slots: Iterable = ()):
        self._replace(slots)

    def _replace(self, slots: Iterable) -> None:
        entries = dict(enumerate(slots))
        positions: dict[int, list[int]] = {}
        by_provider: dict[str, list] = {}
        configured: list = []
        for position, slot in entries.items():
            positions.setdefault(id(slot), []).append(position)
            by_provider.setdefault(slot.provider_id, []).append(slot)
            if not slot.request_scoped:
                configured.append(slot)
        key_health = _KeyHealthCredentials()
        key_health.add(entries.values())
        self._entries = entries
        self._positions = positions
        self._next_position = len(entries)
        self._by_provider = by_provider
        self._configured = configured
        self._key_health = key_health

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self.copy())

    def __reversed__(self):
        return reversed(self.copy())

    def __getitem__(self, index):
        return self.copy()[index]

    def __eq__(self, other):
        if isinstance(other, SlotPool):
            other = other.copy()
        return self.copy() == other

    def __repr__(self):
        return repr(self.copy())

    def copy(self) -> list:
        return list(self._entries.values())

    def index(self, value, *bounds):
        return self.copy().index(value, *bounds)

    def __add__(self, other):
        return self.copy() + (other.copy() if isinstance(other, SlotPool) else other)

    def __radd__(self, other):
        return other + self.copy()

    def __mul__(self, count):
        return self.copy() * count

    __rmul__ = __mul__

    # ── Views ──

    def scope(self, pin: str | None) -> list:
        """Slots selectable by a context pinned to *pin*, or by an unpinned one."""
        if pin:
            return self._by_provider.get(pin, [])
        return self._configured

    def by_provider(self, provider_id: str) -> list:
        """Every slot whose dispatcher ``provider_id`` equals *provider_id*."""
        return self._by_provider.get(provider_id, [])

    def key_stats_key_names(self, namespace: str) -> tuple[str, ...]:
        """Sorted ``key_stats_key_name()`` of live slots in one key-health namespace."""
        return self._key_health.names(namespace)

    # ── Bulk operations (caller holds the dispatcher lock) ──

    def publish(self, slots: Iterable) -> None:
        """Append *slots* in order and index them."""
        slots = list(slots)
        self._key_health.add(slots)
        for slot in slots:
            position = self._next_position
            self._next_position += 1
            self._entries[position] = slot
            self._positions.setdefault(id(slot), []).append(position)
            self._by_provider.setdefault(slot.provider_id, []).append(slot)
            if not slot.request_scoped:
                self._configured.append(slot)

    def withdraw(self, slots: Iterable) -> set[int]:
        """Remove all occurrences of these objects without walking other requests.

        Returns the ``id`` of every slot that was present. Order of the
        remaining slots is unchanged. Views are replaced rather than edited,
        so a copy taken earlier stays intact.
        """
        targets = {id(slot): slot for slot in slots}
        if not targets:
            return set()
        dropped = []
        removed = set()
        for slot_id in targets:
            positions = self._positions.pop(slot_id, ())
            if positions:
                removed.add(slot_id)
                dropped.extend(self._entries.pop(position) for position in positions)
        if not dropped:
            return set()
        gone = [targets[slot_id] for slot_id in removed]
        for provider_id in {slot.provider_id for slot in gone}:
            bucket = [slot for slot in self._by_provider.get(provider_id, ())
                      if id(slot) not in removed]
            if bucket:
                self._by_provider[provider_id] = bucket
            else:
                self._by_provider.pop(provider_id, None)
        if any(not slot.request_scoped for slot in gone):
            self._configured = [slot for slot in self._configured
                                if id(slot) not in removed]
        self._key_health.remove(dropped)
        if len(self._entries) == len(self._configured):
            # The last request retired. Reclaim empty hash-table capacity
            # without re-reading the surviving slots' credential identities.
            self._entries = dict(self._entries)
            self._positions = dict(self._positions)
            self._by_provider = dict(self._by_provider)
            self._key_health.compact()
        return removed

    def replace_configured(self, slots: Iterable) -> None:
        """Swap the configured slots; live request-scoped slots stay, in order."""
        request_scoped = [slot for slot in self if slot.request_scoped]
        self._replace(list(slots) + request_scoped)

    # ── Generic sequence mutations keep the indexes exact ──

    def append(self, slot) -> None:
        self._replace(self.copy() + [slot])

    def extend(self, slots) -> None:
        self._replace(self.copy() + list(slots))

    def insert(self, index, slot) -> None:
        slots = self.copy()
        slots.insert(index, slot)
        self._replace(slots)

    def remove(self, slot) -> None:
        slots = self.copy()
        slots.remove(slot)
        self._replace(slots)

    def pop(self, index=-1):
        slots = self.copy()
        slot = slots.pop(index)
        self._replace(slots)
        return slot

    def clear(self) -> None:
        self._replace(())

    def sort(self, *args, **kwargs) -> None:
        slots = self.copy()
        slots.sort(*args, **kwargs)
        self._replace(slots)

    def reverse(self) -> None:
        self._replace(reversed(self.copy()))

    def __setitem__(self, index, value) -> None:
        slots = self.copy()
        slots[index] = value
        self._replace(slots)

    def __delitem__(self, index) -> None:
        slots = self.copy()
        del slots[index]
        self._replace(slots)

    def __iadd__(self, slots):
        self.extend(slots)
        return self

    def __imul__(self, count):
        self._replace(self.copy() * count)
        return self

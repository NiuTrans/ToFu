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

The pool is a ``list`` so existing readers (``list(dispatcher.slots)``,
iteration, equality) keep working. Generic list mutations rebuild the index;
the dispatcher's own bulk operations update it in one pass. Dependencies: Slot
attributes only.
"""

from __future__ import annotations

from collections.abc import Iterable

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


class SlotPool(list):
    """All live slots, plus per-provider, configured-only and key-health views."""

    def __init__(self, slots: Iterable = ()):
        super().__init__(slots)
        self._reindex()

    def _reindex(self) -> None:
        by_provider: dict[str, list] = {}
        configured: list = []
        for slot in list.__iter__(self):
            by_provider.setdefault(slot.provider_id, []).append(slot)
            if not slot.request_scoped:
                configured.append(slot)
        key_health = _KeyHealthCredentials()
        key_health.add(list.__iter__(self))
        self._by_provider = by_provider
        self._configured = configured
        self._key_health = key_health

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
        super().extend(slots)
        for slot in slots:
            self._by_provider.setdefault(slot.provider_id, []).append(slot)
            if not slot.request_scoped:
                self._configured.append(slot)

    def withdraw(self, slots: Iterable) -> set[int]:
        """Remove exactly these objects (by identity) in one pass.

        Returns the ``id`` of every slot that was present. Order of the
        remaining slots is unchanged. Views are replaced rather than edited,
        so a copy taken earlier stays intact.
        """
        targets = {id(slot): slot for slot in slots}
        if not targets:
            return set()
        kept = []
        dropped = []
        for slot in list.__iter__(self):
            if id(slot) in targets:
                dropped.append(slot)
            else:
                kept.append(slot)
        if not dropped:
            return set()
        removed = {id(slot) for slot in dropped}
        super().__setitem__(slice(None), kept)
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
        return removed

    def replace_configured(self, slots: Iterable) -> None:
        """Swap the configured slots; live request-scoped slots stay, in order."""
        request_scoped = [slot for slot in list.__iter__(self) if slot.request_scoped]
        super().__setitem__(slice(None), list(slots) + request_scoped)
        self._reindex()

    # ── Generic list mutations keep the index exact ──

    def append(self, slot) -> None:
        super().append(slot)
        self._reindex()

    def extend(self, slots) -> None:
        super().extend(slots)
        self._reindex()

    def insert(self, index, slot) -> None:
        super().insert(index, slot)
        self._reindex()

    def remove(self, slot) -> None:
        super().remove(slot)
        self._reindex()

    def pop(self, index=-1):
        slot = super().pop(index)
        self._reindex()
        return slot

    def clear(self) -> None:
        super().clear()
        self._reindex()

    def sort(self, *args, **kwargs) -> None:
        super().sort(*args, **kwargs)
        self._reindex()

    def reverse(self) -> None:
        super().reverse()
        self._reindex()

    def __setitem__(self, index, value) -> None:
        super().__setitem__(index, value)
        self._reindex()

    def __delitem__(self, index) -> None:
        super().__delitem__(index)
        self._reindex()

    def __iadd__(self, slots):
        super().__iadd__(slots)
        self._reindex()
        return self

    def __imul__(self, count):
        super().__imul__(count)
        self._reindex()
        return self

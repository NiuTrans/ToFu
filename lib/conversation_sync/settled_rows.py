"""Settled Turn-projection rows: deep-immutable history shared by reference.

Responsibility: one representation for projection history rows that never
change again — completed tool rounds, priced API rounds, closed timing status
entries and folded activity entries. ``settle`` returns a deep-immutable twin
of a JSON tree. ``SettledDict`` / ``SettledList`` serialize byte-identically to
``dict`` / ``list``, but every in-place mutator raises ``SettledRowMutation``.
Projection capture therefore shares settled rows instead of copying them, and
pure projection steps memoize per row identity in the row's own memo slot
(``memo_get`` / ``memo_put``), so a memo entry lives exactly as long as its row.

Copy semantics stay those of plain containers: ``dict(row)`` / ``copy.copy``
give a mutable shallow copy whose nested values remain settled;
``copy.deepcopy``, pickling and generic rebuilders that construct through the
row's own type (``dataclasses.asdict``, ``type(row)(items)``) return mutable
plain containers. A writer that
legitimately changes a settled row replaces it copy-on-write.

A long row list is derived once per row: ``derive_rows`` keeps each settled
row's derived output while the row keeps its identity, and a detached copy
shares those outputs and charges them from that memo without visiting them.
An event frame is copied and charged in one visit
(``detach_frame_sharing_settled``); its root and atoms are measured when it is
enqueued.

Entry points: ``settle``, ``is_settled``, ``settled_retained_bytes``,
``detach_sharing_settled``, ``detach_frame_sharing_settled``, ``derive_rows``,
``memo_get`` / ``memo_put``.

Dependencies: standard library only. This is a sidecar-shared contract module
(``scripts/check_architecture.py``): the storage sidecar imports projection
helpers that recognize settled rows, although it never receives one.
"""

from __future__ import annotations

import copy
import json
import operator
import sys
from itertools import islice
from typing import Any

__all__ = [
    'DerivedRows', 'FrameCharge', 'SettledDict', 'SettledList', 'SettledRowMutation',
    'UnsettleableValue', 'compact_json_bytes', 'derive_rows',
    'detach_frame_sharing_settled', 'detach_sharing_settled', 'interchangeable',
    'is_settled', 'json_array_text', 'memo_get',
    'memo_put', 'settle', 'settle_items', 'settle_or_none', 'settle_unless_same',
    'settled_retained_bytes',
]

# Exactly the atomic types ``lib.utils.fast_deepcopy`` shares without copying.
_ATOMIC_TYPES = frozenset({type(None), bool, int, float, complex, str, bytes})
# Value subclasses (for example a canonical tool-result ``str``) are immutable
# values too; their runtime attributes never reach JSON.
_IMMUTABLE_VALUE_BASES = (str, int, float, complex, bytes)
# The detached-graph owner allowance charged by ``lib.utils`` accounting.
_DETACHED_GRAPH_OVERHEAD_BYTES = 1024
_MISSING = object()


class SettledRowMutation(TypeError):
    """A settled row was changed in place; replace it copy-on-write instead."""


class UnsettleableValue(TypeError):
    """The value holds a mutable non-JSON object, so it cannot settle."""


def _refuse(self, *_args, **_kwargs):
    raise SettledRowMutation(
        f'settled {type(self).__name__} rows are immutable; replace the row '
        'copy-on-write (dict(row) / list(row)) instead of editing it in place')


class SettledDict(dict):
    """A ``dict`` that serializes like one and refuses every mutation."""

    __slots__ = ('_memo',)

    def __new__(cls, *args, **kwargs):
        if args or kwargs:
            # Generic rebuilders (dataclasses.asdict, type(row)(items)) copy
            # through the row's type; a copy is a plain mutable container.
            return dict(*args, **kwargs)
        return dict.__new__(cls)

    def __init__(self, *args, **kwargs):
        if args or kwargs:
            _refuse(self)
        self._memo = None

    __setitem__ = __delitem__ = __ior__ = _refuse
    clear = pop = popitem = setdefault = update = _refuse

    def __copy__(self):
        return dict(self)

    def __deepcopy__(self, memo):
        result: dict = {}
        memo[id(self)] = result
        for key, value in dict.items(self):
            result[copy.deepcopy(key, memo)] = copy.deepcopy(value, memo)
        return result

    def __reduce_ex__(self, _protocol):
        return dict, (dict(self),)


class SettledList(list):
    """A ``list`` that serializes like one and refuses every mutation."""

    __slots__ = ('_memo',)

    def __new__(cls, *args):
        if args:
            return list(*args)
        return list.__new__(cls)

    def __init__(self, *args):
        if args:
            _refuse(self)
        self._memo = None

    __setitem__ = __delitem__ = __iadd__ = __imul__ = _refuse
    append = extend = insert = pop = remove = clear = sort = reverse = _refuse

    def __copy__(self):
        return list(self)

    def __deepcopy__(self, memo):
        result: list = []
        memo[id(self)] = result
        for value in self:
            result.append(copy.deepcopy(value, memo))
        return result

    def __reduce_ex__(self, _protocol):
        return list, (list(self),)


_SETTLED_TYPES = (SettledDict, SettledList)
_dict_setitem = dict.__setitem__
_list_append = list.append


def is_settled(value: Any) -> bool:
    """True for a settled container (exact type check, no subclass magic)."""
    kind = type(value)
    return kind is SettledDict or kind is SettledList


def _settle(value, memo):
    kind = type(value)
    if kind in _ATOMIC_TYPES or kind is SettledDict or kind is SettledList:
        return value
    identity = id(value)
    settled = memo.get(identity)
    if settled is not None:
        return settled
    if kind is dict:
        # Incremental insertion gives the same table allocation as the
        # incremental detached copies this twin replaces.
        result = SettledDict()
        memo[identity] = result
        for key, child in value.items():
            if type(key) not in _ATOMIC_TYPES and not isinstance(
                    key, _IMMUTABLE_VALUE_BASES):
                raise UnsettleableValue(f'unsettleable key {type(key).__name__}')
            _dict_setitem(result, key, _settle(child, memo))
        return result
    if kind is list:
        result = SettledList()
        memo[identity] = result
        for child in value:
            _list_append(result, _settle(child, memo))
        return result
    if kind is tuple:
        children = tuple(_settle(child, memo) for child in value)
        result = value if all(
            new is old for new, old in zip(children, value)) else children
        memo[identity] = result
        return result
    if isinstance(value, _IMMUTABLE_VALUE_BASES):
        return value
    raise UnsettleableValue(f'unsettleable value {kind.__name__}')


def settle(value: Any) -> Any:
    """Return a deep-immutable twin; settled subtrees and atoms are shared.

    Raises ``UnsettleableValue`` when the tree holds a mutable object that is
    not a builtin JSON container (a custom mapping, a set, an arbitrary
    object): such a row cannot be proven immutable and stays live.
    """
    return _settle(value, {})


def settle_or_none(value: Any) -> Any:
    """``settle`` for opportunistic producers: ``None`` when unsettleable."""
    try:
        return settle(value)
    except UnsettleableValue:
        return None


def settle_items(values: Any, *, previous: Any = ()) -> Any:
    """Return a new plain list of settled items (unsettleable items stay live).

    A rebuilt list reuses the settled item at the same position of
    ``previous`` when it is interchangeable with the new item, so unchanged
    rows keep their identity (and their memoized results) across rebuilds.
    """
    if not isinstance(values, list):
        return values
    if not isinstance(previous, list):
        previous = ()
    settled_items = []
    for index, item in enumerate(values):
        prior = previous[index] if index < len(previous) else None
        if is_settled(prior) and interchangeable(prior, item):
            settled_items.append(prior)
            continue
        settled = settle_or_none(item)
        settled_items.append(item if settled is None else settled)
    return settled_items


_DICT_KINDS = (dict, SettledDict)
_LIST_KINDS = (list, SettledList)


def interchangeable(left: Any, right: Any) -> bool:
    """True when either value can replace the other with no observable change.

    Settled and plain containers are interchangeable when their keys (in
    order) and items are; atoms must have the same exact type and value, and
    floats the same sign and NaN-ness. Anything else must be the same object.
    """
    if left is right:
        return True
    kind = type(left)
    other = type(right)
    if kind in _DICT_KINDS and other in _DICT_KINDS:
        if len(left) != len(right):
            return False
        for (left_key, left_value), (right_key, right_value) in zip(
                dict.items(left), dict.items(right)):
            if not (interchangeable(left_key, right_key)
                    and interchangeable(left_value, right_value)):
                return False
        return True
    if (kind in _LIST_KINDS and other in _LIST_KINDS) or (
            kind is tuple and other is tuple):
        return len(left) == len(right) and all(
            interchangeable(a, b) for a, b in zip(left, right))
    if kind is not other or kind not in _ATOMIC_TYPES:
        return False
    if kind is float:
        return repr(left) == repr(right)
    return left == right


def settle_unless_same(row: Any, result: Any) -> Any:
    """Return ``row`` when a pure step left it interchangeable, else settle."""
    return row if interchangeable(result, row) else settle(result)


def memo_get(row: Any, key: Any, default: Any = None) -> Any:
    """Return a memoized pure result for a settled row, or ``default``."""
    memo = row._memo
    if memo is None:
        return default
    value = memo.get(key, _MISSING)
    if value is _MISSING:
        return default
    return row if value is _SAME else value


def memo_put(row: Any, key: Any, value: Any) -> Any:
    """Memoize ``value`` for ``row``; deterministic values make races benign."""
    memo = row._memo
    if memo is None:
        memo = {}
        row._memo = memo
    memo[key] = _SAME if value is row else value
    return value


# A memo result equal to its own row is stored by marker, not as a cycle.
_SAME = object()


def settled_retained_bytes(row: Any) -> int:
    """Charge every object reachable from ``row`` once, computed once per row.

    This is the identity walk of ``lib.utils.retained_graph_bytes`` restricted
    to one settled row and without the per-graph owner allowance.
    """
    charge = memo_get(row, _RETAINED_KEY)
    if charge is not None:
        return charge
    pending = [row]
    seen = set()
    charge = 0
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        charge += sys.getsizeof(value)
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return memo_put(row, _RETAINED_KEY, charge)


_RETAINED_KEY = ('retained-bytes',)
_JSON_BYTES_KEY = ('compact-json-bytes',)


def _encoded_bytes(value) -> int:
    return len(json.dumps(
        value, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8'))


def compact_json_bytes(document: Any, *, row_fields: tuple = ()) -> int:
    """Exact compact UTF-8 JSON size of ``document``, reusing settled rows.

    Equals ``len(json.dumps(document, ensure_ascii=False,
    separators=(',', ':')).encode('utf-8'))``. JSON composes by
    concatenation: a row list encodes as ``[`` + rows joined by ``,`` + ``]``.
    Settled rows of the named top-level lists are measured once each; the
    document is encoded once with only its other rows, and every settled row
    adds its size plus the separator it needs. Without settled rows this is
    one encoding of the whole document.
    """
    settled_by_field = {}
    for field in row_fields:
        rows = document.get(field)
        if type(rows) in _LIST_KINDS:
            settled = [row for row in rows if type(row) is SettledDict]
            if settled:
                settled_by_field[field] = settled
    if not settled_by_field:
        return _encoded_bytes(document)
    reduced = dict(document)
    total = 0
    for field, settled in settled_by_field.items():
        plain = [row for row in document[field] if type(row) is not SettledDict]
        reduced[field] = plain
        total += len(settled) if plain else len(settled) - 1
        for row in settled:
            size = memo_get(row, _JSON_BYTES_KEY)
            if size is None:
                size = memo_put(row, _JSON_BYTES_KEY, _encoded_bytes(row))
            total += size
    return total + _encoded_bytes(reduced)


class DerivedRows:
    """One row list's derived rows, reused while its settled inputs keep their identity.

    ``inputs`` are the rows of the last derivation, held so their identities
    stay valid, and ``outputs`` their derived rows. A position whose input and
    output are both settled is reused by the next derivation and shared by
    detached copies; ``live`` lists every other position (a row that can still
    change in place), which is derived and copied again each time. ``shared``
    holds the reused outputs' ids, ``charge`` the standalone retained size of
    each distinct one and ``weight`` the caller's per-row measure summed over
    reused positions. ``distinct`` is false when one output fills several
    reused positions; removing such a position then rebuilds the memo.
    """

    __slots__ = ('inputs', 'outputs', 'live', 'shared', 'charge', 'weight', 'distinct')

    def __init__(self, inputs, outputs, live, shared, charge, weight, distinct):
        self.inputs = inputs
        self.outputs = outputs
        self.live = live
        self.shared = shared
        self.charge = charge
        self.weight = weight
        self.distinct = distinct

    def __reduce_ex__(self, _protocol):
        # Identities do not survive copying or pickling; neither does the memo.
        return type(None), ()


def derive_rows(rows, derive, previous: DerivedRows | None = None, *, weigh=None):
    """Return ``(derived, memo)``; ``derived`` is a new list equal to ``derive(rows)``.

    ``derive(items)`` returns the derived rows of a list, position by
    position, and must be pure for settled rows. Only live positions and rows
    that are new or replaced since ``previous`` (this list's last memo) are
    derived again; rows dropped from the front of a bounded window keep the
    remaining positions reusable. ``weigh(row)`` is an optional additive
    measure summed over reused outputs into ``memo.weight``. Passing
    ``{id(derived): memo}`` to ``detach_sharing_settled`` shares and charges
    the reused outputs without visiting them.
    """
    rows = list(rows)
    base = _rebase(rows, previous)
    if base is None:
        derived = list(derive(rows))
        return derived, _fold_derived(
            rows, derived, range(len(rows)), frozenset(), 0, 0, True, weigh)
    offset, changed = base
    live = previous.live
    kept = len(previous.inputs) - offset
    if not offset and not changed and kept == len(rows):
        derived = list(previous.outputs)
        if live:
            for position, output in zip(live, derive([rows[index] for index in live])):
                derived[position] = output
        return derived, previous
    live_positions = set(live)
    removed = [previous.outputs[index] for index in range(offset)
               if index not in live_positions]
    removed += [previous.outputs[offset + position] for position in changed
                if offset + position not in live_positions]
    if removed and not previous.distinct:
        derived = list(derive(rows))
        return derived, _fold_derived(
            rows, derived, range(len(rows)), frozenset(), 0, 0, True, weigh)
    shared, charge, weight = previous.shared, previous.charge, previous.weight
    if removed:
        shared = shared.difference(map(id, removed))
        charge -= sum(map(settled_retained_bytes, removed))
        if weigh is not None:
            weight -= sum(map(weigh, removed))
    positions = sorted({index - offset for index in live if index >= offset}.union(
        changed, range(kept, len(rows))))
    derived = list(islice(previous.outputs, offset, None)) + rows[kept:]
    for position, output in zip(positions, derive([rows[index] for index in positions])):
        derived[position] = output
    return derived, _fold_derived(
        rows, derived, positions, shared, charge, weight, previous.distinct, weigh)


def _rebase(rows, previous):
    """``(offset, changed)`` while ``previous`` still describes ``rows``, else None.

    ``offset`` rows left the front of the list; ``changed`` lists the
    positions of ``rows`` that hold another object than ``previous`` held.
    """
    if previous is None:
        return None
    inputs = previous.inputs
    offset = 0
    if rows and inputs and rows[0] is not inputs[0]:
        first = rows[0]
        offset = next((index for index in range(1, len(inputs))
                       if inputs[index] is first), 0)
        if not offset:
            return None
    if len(rows) < len(inputs) - offset:
        return None
    if all(map(operator.is_, rows, islice(inputs, offset, None))):
        return offset, ()
    same = map(operator.is_, rows, islice(inputs, offset, None))
    return offset, [position for position, unchanged in enumerate(same) if not unchanged]


def _fold_derived(rows, derived, positions, shared, charge, weight, distinct, weigh):
    """Memo of ``rows``: freshly derived ``positions`` join the reused ones."""
    added = set()
    live = []
    for position in positions:
        output = derived[position]
        if is_settled(output) and is_settled(rows[position]):
            identity = id(output)
            if identity in shared or identity in added:
                distinct = False
            else:
                added.add(identity)
                charge += settled_retained_bytes(output)
            if weigh is not None:
                weight += weigh(output)
        else:
            live.append(position)
    return DerivedRows(tuple(rows), tuple(derived), tuple(live), shared.union(added),
                       charge, weight, distinct)


_JSON_TEXT_KEY = ('json-text',)


def json_array_text(rows: Any) -> str:
    """``json.dumps(rows, ensure_ascii=False)``, encoding each settled row once.

    With the default separators a JSON array is ``[`` + the items' own
    encodings joined by ``, `` + ``]``. A settled row keeps its text in its
    memo slot, so a growing row list re-encodes only its live rows.
    """
    if type(rows) not in _LIST_KINDS:
        return json.dumps(rows, ensure_ascii=False)
    parts = []
    for row in rows:
        if type(row) is SettledDict:
            text = memo_get(row, _JSON_TEXT_KEY)
            if text is None:
                text = memo_put(row, _JSON_TEXT_KEY, json.dumps(row, ensure_ascii=False))
        else:
            text = json.dumps(row, ensure_ascii=False)
        parts.append(text)
    return '[' + ', '.join(parts) + ']'


def _keep_alive_during_copy(value, memo):
    try:
        memo[id(memo)].append(value)
    except KeyError:
        memo[id(memo)] = [value]


class _SharingCopy:
    """Detach a graph, sharing settled rows and charging each one once.

    For graphs without settled rows the JSON and charge are exactly those of
    ``lib.utils.fast_deepcopy_with_retained_bytes``: builtin dicts/lists are
    copied inline, atoms and immutable value subclasses are shared and
    charged once by identity, and any other type falls back to
    ``copy.deepcopy`` followed by a re-measure of the reachable result. A
    settled row is a shared leaf whose standalone charge
    (``settled_retained_bytes``) is added once per graph. A ``derive_rows``
    list named in ``derived_rows`` shares its reused outputs and adds their
    memoized charge without visiting them.
    """

    def __init__(self, measure: bool, derived_rows=None, *, defer_atoms=False):
        self.measure = measure
        self.memo: dict = {}
        self.seen: set = set()
        self.retained_bytes = _DETACHED_GRAPH_OVERHEAD_BYTES
        self.used_fallback = False
        self.derived_rows = derived_rows
        # Atoms whose size is read later: serializing a frame (orjson) before
        # it is enqueued attaches a cached UTF-8 form to non-ASCII text.
        self.atoms = [] if defer_atoms else None

    def _charge_atom(self, value):
        if self.atoms is None:
            self.retained_bytes += sys.getsizeof(value)
        else:
            self.atoms.append(value)

    def copy_frame(self, frame):
        """Copy a dict root; its own size and direct items are charged later."""
        result = {}
        memo = self.memo
        memo[id(frame)] = result
        _keep_alive_during_copy(frame, memo)
        copy_value = self.copy
        for key, child in frame.items():
            if type(key) not in _ATOMIC_TYPES:
                key = copy_value(key)
            kind = type(child)
            if not (kind in _ATOMIC_TYPES or kind is SettledDict or kind is SettledList
                    or isinstance(child, _IMMUTABLE_VALUE_BASES)):
                child = copy_value(child)
            result[key] = child
        return result

    def _copy_derived_rows(self, value, derived):
        memo = self.memo
        # Appending item by item allocates exactly as the row-by-row copy does.
        result = [row for row in value]
        memo[id(value)] = result
        _keep_alive_during_copy(value, memo)
        if self.measure:
            self.seen.add(id(result))
            self.seen |= derived.shared
            self.retained_bytes += derived.charge + sys.getsizeof(result)
        for position in derived.live:
            result[position] = self.copy(value[position])
        return result

    def copy(self, value):
        kind = type(value)
        identity = id(value)
        seen = self.seen
        if kind in _ATOMIC_TYPES:
            if self.measure and identity not in seen:
                seen.add(identity)
                self._charge_atom(value)
            return value
        if kind is SettledDict or kind is SettledList:
            if self.measure and identity not in seen:
                seen.add(identity)
                self.retained_bytes += settled_retained_bytes(value)
            return value
        if isinstance(value, _IMMUTABLE_VALUE_BASES):
            # A value subclass (canonical tool-result text) is immutable and
            # serializes as its base value. A generic deepcopy would charge an
            # equal-sized twin and then re-walk the whole graph.
            if self.measure and identity not in seen:
                seen.add(identity)
                self._charge_atom(value)
            return value
        memo = self.memo
        if identity in memo:
            return memo[identity]
        getsize = sys.getsizeof
        charge = 0
        measure = self.measure
        atoms = self.atoms
        if kind is dict:
            result = {}
            memo[identity] = result
            _keep_alive_during_copy(value, memo)
            if measure:
                seen.add(id(result))
            for key, child in value.items():
                if type(key) not in _ATOMIC_TYPES:
                    key = self.copy(key)
                elif measure and id(key) not in seen:
                    seen.add(id(key))
                    if atoms is None:
                        charge += getsize(key)
                    else:
                        atoms.append(key)
                if type(child) not in _ATOMIC_TYPES:
                    child = self.copy(child)
                elif measure and id(child) not in seen:
                    seen.add(id(child))
                    if atoms is None:
                        charge += getsize(child)
                    else:
                        atoms.append(child)
                result[key] = child
            if measure:
                self.retained_bytes += charge + getsize(result)
            return result
        if kind is list:
            derived = self.derived_rows.get(identity) if self.derived_rows else None
            if derived is not None and (not measure or seen.isdisjoint(derived.shared)):
                return self._copy_derived_rows(value, derived)
            result = []
            memo[identity] = result
            _keep_alive_during_copy(value, memo)
            if measure:
                seen.add(id(result))
            append = result.append
            for child in value:
                child_kind = type(child)
                if child_kind is SettledDict:
                    # Shared settled rows dominate long histories.
                    if measure and id(child) not in seen:
                        seen.add(id(child))
                        charge += settled_retained_bytes(child)
                elif child_kind not in _ATOMIC_TYPES:
                    child = self.copy(child)
                elif measure and id(child) not in seen:
                    seen.add(id(child))
                    if atoms is None:
                        charge += getsize(child)
                    else:
                        atoms.append(child)
                append(child)
            if measure:
                self.retained_bytes += charge + getsize(result)
            return result
        self.used_fallback = True
        return copy.deepcopy(value, memo)


def _charge_unseen(values, seen) -> int:
    """Charge what ``values`` reach and ``seen`` lacks, adding it to ``seen``.

    The identity walk of ``retained_graph_bytes``, stopping at settled rows,
    which count their standalone size once.
    """
    pending = list(values)
    total = 0
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        kind = type(item)
        if kind is SettledDict or kind is SettledList:
            total += settled_retained_bytes(item)
            continue
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return total


def _retained_bytes_sharing_settled(value) -> int:
    """``retained_graph_bytes`` that stops at settled rows (charged once)."""
    return _DETACHED_GRAPH_OVERHEAD_BYTES + _charge_unseen((value,), set())


def detach_sharing_settled(value: Any, *, measure: bool = True, derived_rows=None):
    """Return ``(detached, retained_bytes)`` sharing settled rows by reference.

    Unsettled structure is copied exactly as ``fast_deepcopy`` copies it.
    ``retained_bytes`` is zero when ``measure`` is false. ``derived_rows``
    maps ``id(rows)`` of ``derive_rows`` lists inside ``value`` to their memo;
    their reused outputs are shared and charged without being visited, with
    the charge the row-by-row copy computes.
    """
    owner = _SharingCopy(measure, derived_rows)
    result = owner.copy(value)
    if not measure:
        return result, 0
    if owner.used_fallback:
        return result, _retained_bytes_sharing_settled(result)
    return result, owner.retained_bytes


class FrameCharge:
    """The retained charge of one detached event frame, completed at enqueue.

    The copy charges the containers below the frame root while visiting them
    and keeps the atoms it met. ``complete`` measures those atoms, then the
    root and its direct keys and values as they are at that point, so fields
    the producer stamps on the root after detachment (event sequence,
    envelope identity, execution clock) and text that serialization grew are
    charged exactly as a fresh walk of the final frame charges them, without
    walking it again.
    """

    __slots__ = ('_frame', '_owner')

    def __init__(self, frame, owner):
        self._frame = frame
        self._owner = owner

    def complete(self, *beside) -> tuple[int, int]:
        """Return ``(frame_bytes, total_bytes)``; call once, when enqueuing.

        ``frame_bytes`` charges the frame alone. ``total_bytes`` also charges
        ``beside`` once by identity against it, as ``retained_graph_bytes(
        frame, *beside)`` does; settled rows count their standalone size once.
        """
        owner, self._owner = self._owner, None
        if owner.used_fallback:
            # A generic deepcopy hook may merge keys or alias arbitrary
            # objects: charge the final reachable frame instead.
            owner.seen = set()
            owner.retained_bytes = _DETACHED_GRAPH_OVERHEAD_BYTES
            owner.atoms = ()
        frame_bytes = (owner.retained_bytes + sum(map(sys.getsizeof, owner.atoms))
                       + _charge_unseen((self._frame,), owner.seen))
        return frame_bytes, frame_bytes + _charge_unseen(beside, owner.seen)


def detach_frame_sharing_settled(frame: dict) -> tuple[dict, FrameCharge]:
    """Detach an event frame like ``detach_sharing_settled`` in one measured visit.

    Returns ``(detached, charge)``; ``charge.complete(*beside)`` yields its
    retained bytes once the producer has finished stamping the frame root.
    """
    owner = _SharingCopy(True, defer_atoms=True)
    detached = owner.copy_frame(frame)
    return detached, FrameCharge(detached, owner)

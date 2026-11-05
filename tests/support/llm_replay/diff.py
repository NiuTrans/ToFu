"""Field-level diff of two normalized replay requests.

Drift diagnostics only: the replayer still decides identity with the
wholesale digest from ``normalize.py``; this module explains a mismatch.
Differences are reported in document order as JSON paths
(``$.tools[3:read_files].function.description``) with the recorded and
current values.  List elements are aligned by content, so one inserted tool
or message is reported once instead of shifting every later index; inside a
changed run, elements that carry a name (``function.name`` or ``name``) are
paired by that name and the name is shown next to the index.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from typing import Any

__all__ = ['ABSENT', 'Difference', 'describe_difference',
           'request_differences']


class _Absent:
    def __repr__(self) -> str:
        return '<absent>'


#: Marks the side of a difference on which a key or list element is missing.
ABSENT: Any = _Absent()

_IDENTIFIER = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


@dataclass(frozen=True)
class Difference:
    path: str
    recorded: Any
    current: Any


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def _key_segment(key: str) -> str:
    if _IDENTIFIER.match(key):
        return f'.{key}'
    return f'[{json.dumps(key, ensure_ascii=False)}]'


def _element_name(value) -> str | None:
    if not isinstance(value, dict):
        return None
    function = value.get('function')
    if isinstance(function, dict) and isinstance(function.get('name'), str):
        return function['name']
    name = value.get('name')
    return name if isinstance(name, str) else None


def _index_segment(index: int, value) -> str:
    name = _element_name(value)
    return f'[{index}]' if name is None else f'[{index}:{name}]'


def _pair_changed_run(recorded: list, current: list) -> list[tuple]:
    """Pair one changed run: by name first, unnamed elements by position."""
    unmatched = list(range(len(current)))
    by_name: dict[str, list[int]] = {}
    for offset in unmatched:
        name = _element_name(current[offset])
        if name is not None:
            by_name.setdefault(name, []).append(offset)
    pairs: list[tuple] = []
    leftovers: list[int] = []
    for offset, value in enumerate(recorded):
        name = _element_name(value)
        candidates = by_name.get(name) if name is not None else None
        if candidates:
            partner = candidates.pop(0)
            unmatched.remove(partner)
            pairs.append((offset, partner))
        else:
            leftovers.append(offset)
    unnamed = [offset for offset in unmatched
               if _element_name(current[offset]) is None]
    for offset in leftovers:
        if _element_name(recorded[offset]) is None and unnamed:
            partner = unnamed.pop(0)
            unmatched.remove(partner)
            pairs.append((offset, partner))
        else:
            pairs.append((offset, None))
    pairs.extend((None, offset) for offset in unmatched)
    return sorted(pairs, key=lambda pair: (
        pair[0] if pair[1] is None else pair[1], pair[1] is None))


def _diff_lists(recorded: list, current: list, path: str,
                out: list[Difference]) -> None:
    matcher = difflib.SequenceMatcher(
        None, [_canonical(item) for item in recorded],
        [_canonical(item) for item in current], autojunk=False)
    for tag, r1, r2, c1, c2 in matcher.get_opcodes():
        if tag == 'equal':
            continue
        for left, right in _pair_changed_run(recorded[r1:r2],
                                             current[c1:c2]):
            if right is None:
                value = recorded[r1 + left]
                out.append(Difference(
                    path + _index_segment(r1 + left, value), value, ABSENT))
            elif left is None:
                value = current[c1 + right]
                out.append(Difference(
                    path + _index_segment(c1 + right, value), ABSENT, value))
            else:
                value = current[c1 + right]
                _diff(recorded[r1 + left], value,
                      path + _index_segment(c1 + right, value), out)


def _diff(recorded, current, path: str, out: list[Difference]) -> None:
    if isinstance(recorded, dict) and isinstance(current, dict):
        for key, value in recorded.items():
            segment = path + _key_segment(str(key))
            if key in current:
                _diff(value, current[key], segment, out)
            else:
                out.append(Difference(segment, value, ABSENT))
        for key, value in current.items():
            if key not in recorded:
                out.append(Difference(path + _key_segment(str(key)),
                                      ABSENT, value))
        return
    if isinstance(recorded, list) and isinstance(current, list):
        _diff_lists(recorded, current, path, out)
        return
    if type(recorded) is not type(current) or recorded != current:
        out.append(Difference(path, recorded, current))


def request_differences(recorded, current) -> list[Difference]:
    """Every leaf-level difference between two normalized requests."""
    out: list[Difference] = []
    _diff(recorded, current, '$', out)
    return out


def _common_prefix_length(left: str, right: str) -> int:
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return index


def _excerpt(value, *, start: int, width: int) -> str:
    if value is ABSENT:
        return '<absent>'
    if not isinstance(value, str):
        rendered = _canonical(value)
        return rendered if len(rendered) <= width else rendered[:width] + '…'
    window = json.dumps(value[start:start + width], ensure_ascii=False)
    prefix = '…' if start > 0 else ''
    suffix = '…' if start + width < len(value) else ''
    return prefix + window + suffix


def describe_difference(difference: Difference, *, width: int = 160) -> str:
    """One reviewable line: the path plus recorded and current excerpts.

    Two strings are excerpted from shortly before their first differing
    character, so a one-word edit deep inside a long prompt stays visible.
    """
    recorded, current = difference.recorded, difference.current
    start = 0
    if isinstance(recorded, str) and isinstance(current, str):
        start = max(0, _common_prefix_length(recorded, current) - width // 4)
    return (f'{difference.path}: recorded '
            f'{_excerpt(recorded, start=start, width=width)}, current '
            f'{_excerpt(current, start=start, width=width)}')

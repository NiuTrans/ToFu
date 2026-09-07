"""Schema-guided object-array salvage — last-resort recovery for an
array-of-objects arg (e.g. read_files ``reads``) whose stringified value is so
structurally broken that even ``repair_json`` cannot parse it.

Because the tool schema declares the item's exact keys and each key's type, we
don't INFER structure from the (broken) punctuation — we anchor on the known
key tokens and read each value BY ITS DECLARED TYPE. This is the "automaton"
that reconstructs meaningless outer/record punctuation.

Free-text item keys whose values can legitimately contain quotes, colons,
braces and newlines: scanning for "the next key" inside such a value
mis-splits records, and these belong to DESTRUCTIVE edit tools
(apply_diffs / insert_contents) where a wrong salvage would silently corrupt
a code edit. For any item schema containing one of these, salvage is REFUSED
and the call falls through to an honest model retry.
"""

from __future__ import annotations

import re
from typing import Any

from lib.log import get_logger

from lib.tool_input_repair._schema import _array_item_schema

logger = get_logger(__name__)


_FREE_TEXT_ITEM_KEYS = frozenset({
    'search', 'replace', 'content', 'anchor', 'new_string', 'old_string',
    'new_content', 'body', 'text', 'code',
})

_SALVAGE_MISSING = object()  # private "no value parsed" sentinel


def _parse_value_fragment(frag: str, json_type: str) -> Any:
    """Read ONE value out of the text between two key tokens, by declared type.

    A ``string`` value is read by its quote structure (first balanced
    ``"..."``), so embedded ``: , { }`` inside the value never mis-split a
    record; an ``integer`` / ``number`` / ``boolean`` is read as the obvious
    literal run. Returns ``_SALVAGE_MISSING`` when nothing plausible is found.
    """
    if json_type == 'string':
        m = re.search(r'"((?:[^"\\]|\\.)*)"', frag)
        if m:
            return m.group(1).replace('\\"', '"').replace('\\\\', '\\')
        # Unterminated quote — take up to the next structural delimiter.
        stripped = frag.strip().lstrip('"')
        cut = re.split(r'["\],}]', stripped, maxsplit=1)[0].strip()
        return cut if cut else _SALVAGE_MISSING
    if json_type in ('integer', 'number'):
        m = re.search(r'-?\d+(?:\.\d+)?', frag)
        if not m:
            return _SALVAGE_MISSING
        raw = m.group(0)
        try:
            return int(raw) if json_type == 'integer' and '.' not in raw else float(raw)
        except (ValueError, TypeError) as e:
            logger.debug('[ToolRepair] numeric fragment %r not coercible (%s) — '
                         'salvaging as missing', raw, e)
            return _SALVAGE_MISSING
    if json_type == 'boolean':
        low = frag.lower()
        t, f = low.find('true'), low.find('false')
        if t == -1 and f == -1:
            return _SALVAGE_MISSING
        return t != -1 and (f == -1 or t < f)
    return _SALVAGE_MISSING


def _salvage_object_array(
    raw: str, item_types: dict[str, str], required: set[str],
) -> list[dict[str, Any]] | None:
    """Reconstruct a list-of-objects from a structurally-broken string using
    the declared item schema as anchors. Returns ``None`` when it can't be
    done safely (no key tokens, or no record carries every required key).
    Never raises.
    """
    key_re = re.compile(
        r'"(' + '|'.join(re.escape(k) for k in item_types) + r')"\s*:?'
    )
    matches = list(key_re.finditer(raw))
    if not matches:
        return None
    records: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    for idx, m in enumerate(matches):
        k = m.group(1)
        val_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        val = _parse_value_fragment(raw[m.end():val_end], item_types[k])
        if val is _SALVAGE_MISSING:
            continue
        if k in cur:  # a key reappears → new record boundary
            records.append(cur)
            cur = {}
        cur[k] = val
    if cur:
        records.append(cur)
    # Guardrail: every kept record must carry all required item keys, else the
    # split is untrustworthy — refuse and let the model re-emit.
    records = [r for r in records if required.issubset(r.keys())]
    return records or None


def _try_schema_array_salvage(tool_name: str, key: str, value: str) -> list[dict[str, Any]] | None:
    """Gate + run object-array salvage for one array-typed arg. Returns the
    salvaged list, or ``None`` to leave the value untouched (honest retry).
    """
    item = _array_item_schema(tool_name, key)
    if item is None:
        return None
    item_types, required = item
    if _FREE_TEXT_ITEM_KEYS & item_types.keys():
        # Destructive / free-text editor — never auto-salvage.
        return None
    if '"' not in value:  # no key tokens possible
        return None
    try:
        return _salvage_object_array(value, item_types, required)
    except Exception as e:  # pragma: no cover — defensive
        logger.debug('[ToolRepair] array salvage failed for %s.%s: %s', tool_name, key, e)
        return None

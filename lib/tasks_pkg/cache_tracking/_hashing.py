"""Hashing & diffing helpers for cache-break detection.

Pure functions (no shared state): they turn system prompt / tools / message
prefixes into stable hashes and diff two hash snapshots to name the EXACT
tool or message-field that changed between rounds.
"""

from __future__ import annotations

import hashlib
import json

import orjson

from lib.log import get_logger

logger = get_logger(__name__)


# Alphabetical order preserves the historical culprit ordering from
# ``sorted(set(old_row) | set(new_row))`` while giving every message a compact,
# fixed-width representation. ``None`` means the field was absent; every
# present value is a process-local integer fingerprint.
_PREFIX_FIELD_NAMES = (
    'content',
    'reasoning_content',
    'reasoning_details',
    'role',
    'thinking_signature',
    'tool_call_id',
    'tool_calls',
)
_PREFIX_FIELD_WIDTH = len(_PREFIX_FIELD_NAMES)
_REASONING_DETAILS_JSON_OPTIONS = (
    orjson.OPT_NON_STR_KEYS | orjson.OPT_SORT_KEYS
)
PrefixFieldRow = tuple[int | None, ...]


def _md5(text: str) -> str:
    """Fast hash for comparison (not security)."""
    return hashlib.md5(text.encode('utf-8', errors='replace')).hexdigest()[:16]


def _hash_system_prompt(messages: list) -> str:
    """Hash the system message content."""
    for msg in messages:
        if msg.get('role') == 'system':
            content = msg.get('content', '')
            if isinstance(content, list):
                parts = [
                    b.get('text', '') for b in content
                    if isinstance(b, dict) and b.get('type') == 'text'
                ]
                return _md5(''.join(parts))
            return _md5(str(content))
    return ''


def _hash_tools(tools: list | None) -> str:
    """Hash the tool definitions (aggregate)."""
    if not tools:
        return ''
    try:
        return _md5(json.dumps(tools, sort_keys=True, ensure_ascii=False))
    except (TypeError, ValueError) as e:
        logger.debug('[CacheTracking] Tool definitions not JSON-serializable, using str: %s', e)
        return _md5(str(tools))


def _hash_tools_per_tool(tools: list | None) -> dict[str, str]:
    """Hash each tool individually for per-tool diff reporting.

    Returns dict of {tool_name: hash} so we can report WHICH tool(s)
    changed when a tools hash mismatch is detected.
    """
    if not tools:
        return {}
    result = {}
    for tool in tools:
        fn = tool.get('function', {})
        name = fn.get('name', 'unknown')
        try:
            h = _md5(json.dumps(tool, sort_keys=True, ensure_ascii=False))
        except (TypeError, ValueError) as _e_audit:
            logger.debug('[cache_tracking] _hash_tools_per_tool caught %s: %s', type(_e_audit).__name__, _e_audit)
            h = _md5(str(tool))
        result[name] = h
    return result


def _diff_tool_hashes(
    old_hashes: dict[str, str],
    new_hashes: dict[str, str],
) -> list[str]:
    """Return list of tool names that changed, were added, or removed."""
    changes = []
    all_names = set(old_hashes) | set(new_hashes)
    for name in sorted(all_names):
        old_h = old_hashes.get(name)
        new_h = new_hashes.get(name)
        if old_h is None:
            changes.append(f'+{name}')
        elif new_h is None:
            changes.append(f'-{name}')
        elif old_h != new_h:
            changes.append(f'~{name}')
    return changes


def _reasoning_details_hash(value: object) -> int:
    """Return a compact process-local fingerprint for one reasoning payload."""
    try:
        return hash(orjson.dumps(
            value, option=_REASONING_DETAILS_JSON_OPTIONS))
    except (TypeError, ValueError) as exc:
        logger.debug(
            '[CacheTrack] reasoning_details not JSON-serialisable (%s) — '
            'hashing str() form', exc)
        return hash(str(value))


def _hash_prefix_fields(
    messages: list,
    prefix_count: int,
) -> list[PrefixFieldRow]:
    """Build compact per-message, per-field cache-prefix fingerprints.

    Returns one fixed-width tuple per message in ``messages[:prefix_count]``.
    Tuple positions are defined by ``_PREFIX_FIELD_NAMES`` and contain only a
    process-local integer fingerprint or ``None``. CacheState is neither
    persisted nor shared across processes, so Python's keyed runtime hash keeps
    the same 64-bit comparison envelope as the former truncated MD5 while
    avoiding UTF-8 copies, hexadecimal strings, and one dict per message.

    The tuple covers each wire-affecting FIELD
    (``role`` / ``content`` / ``tool_calls`` / ``tool_call_id`` /
    ``reasoning_content`` / ``thinking_signature`` / ``reasoning_details``)
    individually. ``_diff_prefix_fields`` still names the EXACT
    ``(message_index, field)`` that changed between two rounds — the same
    way ``_diff_tool_hashes`` names the exact tool. This turns the old
    terminal "silent prefix byte change (guess)" into a concrete culprit.
    """
    if prefix_count <= 0 or not messages:
        return []
    out: list[PrefixFieldRow] = []
    for msg in messages[:prefix_count]:
        content = msg.get('content', '')
        content_hash = None
        if isinstance(content, list):
            content_hash = hash(tuple(
                block.get('text', '') or block.get('type', '')
                for block in content
                if isinstance(block, dict)
            ))
        elif isinstance(content, str):
            content_hash = hash(content)

        tcs = msg.get('tool_calls') or ()
        tool_calls_hash = None
        if tcs:
            _tp = []
            for tc in tcs:
                if isinstance(tc, dict):
                    fn = tc.get('function') or {}
                    _tp.append(tc.get('id', ''))
                    if isinstance(fn, dict):
                        _tp.append(fn.get('name', ''))
                        _tp.append(fn.get('arguments', ''))
            tool_calls_hash = hash(tuple(_tp))

        reasoning_content = msg.get('reasoning_content')
        rd = msg.get('reasoning_details')
        thinking_signature = msg.get('thinking_signature')
        tool_call_id = msg.get('tool_call_id')
        out.append((
            content_hash,
            hash(str(reasoning_content)) if reasoning_content else None,
            _reasoning_details_hash(rd) if rd else None,
            hash(msg.get('role', '')),
            hash(str(thinking_signature)) if thinking_signature else None,
            hash(str(tool_call_id)) if tool_call_id else None,
            tool_calls_hash,
        ))
    return out


def _is_current_prefix_field_snapshot(snapshot: list) -> bool:
    """Whether a baseline uses the current packed process-local format.

    A live code reload can leave one legacy dict baseline in memory. Treat it
    as incomparable for one round and replace it instead of reporting every
    field as mutated merely because the representation changed.
    """
    if not snapshot:
        return True
    first = snapshot[0]
    return isinstance(first, tuple) and len(first) == _PREFIX_FIELD_WIDTH


def _prefix_field_row_values(row: object) -> tuple:
    """Read packed rows and legacy dict rows for diagnostic compatibility."""
    if isinstance(row, tuple) and len(row) == _PREFIX_FIELD_WIDTH:
        return row
    if isinstance(row, dict):
        return tuple(row.get(field) for field in _PREFIX_FIELD_NAMES)
    return (None,) * _PREFIX_FIELD_WIDTH


def _diff_prefix_fields(old: list, new: list, max_report: int = 6) -> list:
    """Name the exact ``msg[i].field`` entries that differ between two
    per-message field-hash lists (from ``_hash_prefix_fields``).

    Only the overlapping index range is compared field-by-field; a length
    change of the compared prefix is reported as a separate ``len A->B``
    token. Capped at ``max_report`` culprits so the cause string stays
    readable (an extra ``…`` marks truncation).
    """
    changes: list[str] = []
    n = min(len(old), len(new))
    for i in range(n):
        old_values = _prefix_field_row_values(old[i])
        new_values = _prefix_field_row_values(new[i])
        for field_index, field in enumerate(_PREFIX_FIELD_NAMES):
            if old_values[field_index] != new_values[field_index]:
                changes.append(f'msg[{i}].{field}')
                if len(changes) >= max_report:
                    changes.append('…')
                    return changes
    if len(old) != len(new):
        changes.append(f'len {len(old)}\u2192{len(new)}')
    return changes

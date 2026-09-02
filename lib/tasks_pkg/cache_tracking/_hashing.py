"""Hashing & diffing helpers for cache-break detection.

Pure functions (no shared state): they turn system prompt / tools / message
prefixes into stable hashes and diff two hash snapshots to name the EXACT
tool or message-field that changed between rounds.
"""

from __future__ import annotations

import hashlib
import json

from lib.log import get_logger

logger = get_logger(__name__)


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


def _hash_prefix_content(messages: list, prefix_count: int) -> str:
    """Hash the content of messages in the cache prefix.

    This is NOT used for cache break detection (to avoid false positives
    from micro-compact). It's used for diagnostic mutation detection:
    if this hash changes between rounds without a compaction event,
    something is silently mutating messages in the cached prefix.

    Covers the fields that ACTUALLY land on the wire and therefore affect
    the Anthropic prefix-byte match — not just ``content`` text. A turn's
    ``tool_calls`` (name + arguments + id), ``reasoning_content``,
    ``reasoning_details`` and ``thinking_signature`` are all serialized into
    the request body by ``build_body``; a per-round change in any of them is a
    real cache miss. The earlier text-only hash was BLIND to those, so a
    tool_call / argument / signature mutation produced a real miss with NO
    ``PREFIX MUTATION DETECTED`` log line (it got mislabeled ``server_side``).
    Block ORDER is preserved by appending in sequence, so a reorder also
    changes the hash.
    """
    if prefix_count <= 0 or not messages:
        return ''
    parts = []
    for msg in messages[:prefix_count]:
        parts.append(msg.get('role', ''))
        content = msg.get('content', '')
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    # text blocks → text; non-text (image/tool_result) → type
                    parts.append(block.get('text', '') or block.get('type', ''))
        elif isinstance(content, str):
            parts.append(content)
        # Tool calls: name + arguments + id, in order (wire-affecting).
        for tc in msg.get('tool_calls') or ():
            if isinstance(tc, dict):
                parts.append(tc.get('id', ''))
                fn = tc.get('function') or {}
                if isinstance(fn, dict):
                    parts.append(fn.get('name', ''))
                    parts.append(fn.get('arguments', ''))
        if msg.get('tool_call_id'):
            parts.append(str(msg.get('tool_call_id')))
        # Replayed signed-thinking blocks (Claude) are part of the body.
        if msg.get('reasoning_content'):
            parts.append(str(msg.get('reasoning_content')))
        if msg.get('thinking_signature'):
            parts.append(str(msg.get('thinking_signature')))
        rd = msg.get('reasoning_details')
        if rd:
            try:
                parts.append(json.dumps(rd, sort_keys=True, ensure_ascii=False))
            except (TypeError, ValueError):
                parts.append(str(rd))
    return _md5(''.join(parts))


def _hash_prefix_fields(messages: list, prefix_count: int) -> list[dict]:
    """Per-message, per-field hashes of the cache prefix.

    Companion to ``_hash_prefix_content`` (which rolls the WHOLE prefix into
    one hash). This returns a list — one dict per message in
    ``messages[:prefix_count]`` — mapping each wire-affecting FIELD
    (``role`` / ``content`` / ``tool_calls`` / ``tool_call_id`` /
    ``reasoning_content`` / ``thinking_signature`` / ``reasoning_details``)
    to its individual hash. ``_diff_prefix_fields`` then names the EXACT
    ``(message_index, field)`` that changed between two rounds — the same
    way ``_diff_tool_hashes`` names the exact tool. This turns the old
    terminal "silent prefix byte change (guess)" into a concrete culprit.
    """
    if prefix_count <= 0 or not messages:
        return []
    out: list[dict] = []
    for msg in messages[:prefix_count]:
        fh: dict[str, str] = {'role': _md5(msg.get('role', ''))}
        content = msg.get('content', '')
        if isinstance(content, list):
            _cp = []
            for block in content:
                if isinstance(block, dict):
                    _cp.append(block.get('text', '') or block.get('type', ''))
            fh['content'] = _md5('\x1f'.join(_cp))
        elif isinstance(content, str):
            fh['content'] = _md5(content)
        tcs = msg.get('tool_calls') or ()
        if tcs:
            _tp = []
            for tc in tcs:
                if isinstance(tc, dict):
                    fn = tc.get('function') or {}
                    _tp.append(tc.get('id', ''))
                    if isinstance(fn, dict):
                        _tp.append(fn.get('name', ''))
                        _tp.append(fn.get('arguments', ''))
            fh['tool_calls'] = _md5('\x1f'.join(_tp))
        if msg.get('tool_call_id'):
            fh['tool_call_id'] = _md5(str(msg.get('tool_call_id')))
        if msg.get('reasoning_content'):
            fh['reasoning_content'] = _md5(str(msg.get('reasoning_content')))
        if msg.get('thinking_signature'):
            fh['thinking_signature'] = _md5(str(msg.get('thinking_signature')))
        rd = msg.get('reasoning_details')
        if rd:
            try:
                fh['reasoning_details'] = _md5(
                    json.dumps(rd, sort_keys=True, ensure_ascii=False))
            except (TypeError, ValueError) as e:
                logger.debug('[CacheTrack] reasoning_details not JSON-serialisable '
                             '(%s) — hashing str() form', e)
                fh['reasoning_details'] = _md5(str(rd))
        out.append(fh)
    return out


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
        o = old[i] or {}
        nw = new[i] or {}
        for field in sorted(set(o) | set(nw)):
            if o.get(field) != nw.get(field):
                changes.append(f'msg[{i}].{field}')
                if len(changes) >= max_report:
                    changes.append('…')
                    return changes
    if len(old) != len(new):
        changes.append(f'len {len(old)}\u2192{len(new)}')
    return changes

"""Pure retirement of superseded managed snapshots at a compaction boundary.

Entry point: compact_context_snapshots. The registry and renderer's canonical
hashes own identity; durable history and ordinary cache-preserving appends are
unchanged. Invalid or incomplete later carriers cannot retire earlier policy.
"""
from __future__ import annotations

from ._render import _managed_block_hashes
from ._sections import remove_managed_section
from .registry import INJECTION_REGISTRY


def compact_context_snapshots(messages: list[dict]) -> list[dict]:
    """Return a detached list, replacing only verified obsolete tail sections.

    Preserve current snapshots verbatim, system policy, non-text payloads,
    unregistered/ambiguous carriers and ordinary authored lookalike text.
    Mixed carriers retain their independently current sibling sections.
    Pass-local witnesses scale with input and are discarded on return.
    """
    witnesses = []
    latest = {}
    for index, message in enumerate(messages):
        rows = message.get('_contextBlocks')
        if (message.get('role') != 'user' or not message.get('_contextComposer')
                or message.get('_containsHumanSteer') or not isinstance(rows, list)):
            witnesses.append({})
            continue
        content = message.get('content')
        text_only = isinstance(content, str) or (
            isinstance(content, list) and bool(content) and all(
                isinstance(part, dict) and part.get('type') == 'text'
                and isinstance(part.get('text'), str) for part in content))
        actual = _managed_block_hashes([message]) if text_only else {}
        valid = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                continue
            key = row['id']
            spec = INJECTION_REGISTRY.get(key)
            if spec is None or spec.lane != 'block' or spec.placement == 'system':
                continue
            verified = bool(actual.get(key)) and actual[key] == row.get('hash')
            valid[key] = verified if key not in valid else False
            latest[key] = (index, valid[key])
        # Section withdrawal reconstructs siblings: only fully witnessed text
        # carriers may be edited. Never discard an unknown sibling or media.
        editable = bool(valid) and len(valid) == len(rows) and all(valid.values())
        witnesses.append(valid if editable else {})

    result = []
    for index, (message, valid) in enumerate(zip(messages, witnesses)):
        for key in valid:
            newest, verified = latest[key]
            if verified and newest > index:
                message = remove_managed_section(message, key)
                if message is None:
                    break
        if message is not None:
            result.append(message)
    return result

"""Python port of Qwen Code's 2026-08-11 keyword scoring reference.

This is an evaluation adapter, not application code. It mirrors the weights,
stop-word handling and small cancel/delete alias group in Qwen Code commit
062a6132b966ab6a342a0c0e45fe316cc7bd32fe.
"""

from __future__ import annotations

import re
from typing import Any


_STOP = {
    'a', 'an', 'and', 'are', 'at', 'be', 'can', 'could', 'did', 'do',
    'does', 'for', 'from', 'how', 'i', 'in', 'is', 'it', 'me', 'my', 'of',
    'on', 'or', 'please', 'should', 'that', 'the', 'these', 'this', 'those',
    'to', 'was', 'were', 'what', 'which', 'with', 'would', 'you',
}
_ACTION_ALIASES = {
    'cancel': ('cancel', 'delete', 'remove', 'stop', 'clear'),
    'clear': ('clear', 'delete', 'remove', 'cancel', 'stop'),
    'delete': ('delete', 'remove', 'cancel', 'stop', 'clear'),
    'remove': ('remove', 'delete', 'cancel', 'stop', 'clear'),
    'stop': ('stop', 'cancel', 'delete', 'remove', 'clear'),
}


def _tokenize(query: str) -> list[str]:
    out = []
    for raw in str(query or '').lower().split():
        normalized = re.sub(
            r'^[^\w.+#-]+|[^\w.+#-]+$', '', raw, flags=re.UNICODE)
        if len(normalized) >= 2 and normalized not in _STOP:
            out.append(normalized)
    return out


def _schema(tool: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    fn = tool.get('function') or tool
    return (str(fn.get('name') or ''), str(fn.get('description') or ''),
            fn.get('parameters') or {})


def qwen_keyword_search(catalog, query, *, limit=5,
                        namespace_by_name=None, search_text_by_name=None,
                        **_ignored):
    hints = search_text_by_name or {}
    terms = _tokenize(str(query or ''))
    scored = []
    for tool in catalog or []:
        if not isinstance(tool, dict):
            continue
        name, description, params = _schema(tool)
        lowered = name.lower()
        desc = description.lower()
        hint_parts = str(hints.get(name) or '').lower().split()
        is_mcp = name.startswith('mcp__')
        total = 0
        for term in terms:
            variants = _ACTION_ALIASES.get(term, (term,))
            name_score = 0
            for variant in variants:
                if (lowered == variant or lowered.endswith('_' + variant)
                        or lowered.endswith('.' + variant)):
                    name_score = max(name_score, 12 if is_mcp else 10)
                elif variant in lowered:
                    name_score = max(name_score, 6 if is_mcp else 5)
            total += name_score
            if any(part in variants for part in hint_parts):
                total += 4
            if any(variant in desc for variant in variants):
                total += 2
            if term in _ACTION_ALIASES and any(
                    variant != term and (
                        variant in lowered or variant in hint_parts)
                    for variant in variants):
                total += 6
        if total > 0:
            scored.append((-total, name, description, params))
    scored.sort()
    items = [{
        'name': name,
        'namespace': str((namespace_by_name or {}).get(name) or 'general'),
        'description': description,
        'arguments_schema': params,
        'score': -negative,
    } for negative, name, description, params in scored[:int(limit)]]
    return {'status': 'ok', 'query': str(query or ''), 'items': items,
            'total': len(scored), 'next_cursor': None}

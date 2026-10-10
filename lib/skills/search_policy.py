"""Pure bounded query recovery shared by online discovery; no network or state.

Keep semantic registry matches unless a single-word query only matches a longer
word prefix (Frida/Friday). Recovery reuses only words from the explicit query.
"""
from __future__ import annotations

import re
import unicodedata


def words(value: str) -> list[str]:
    return re.findall(r'[a-z0-9]+|[\u3400-\u9fff]+',
                      unicodedata.normalize('NFKC', value).lower())


def fallback_queries(query: str) -> list[str]:
    """At most two anchors, only for multi-concept queries (four+ words)."""
    tokens = list(dict.fromkeys(token for token in words(query) if len(token) >= 3))
    if len(tokens) < 4:
        return []
    return [tokens[0], tokens[len(tokens) // 2]]


def prefix_only_match(query: str, candidate: dict) -> bool:
    tokens = words(query)
    if len(tokens) != 1:
        return False
    term = tokens[0]
    fields = ' '.join(str(candidate.get(key) or '') for key in
                      ('catalog_id', 'name', 'description', 'tags'))
    candidate_words = words(fields)
    return term not in candidate_words and any(
        word.startswith(term) and word != term for word in candidate_words)

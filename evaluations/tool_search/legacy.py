"""Frozen pre-2026-08-11 lexical retriever used as the control arm."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any


_WORD_RE = re.compile(r'[a-z0-9_./:-]+|[\u3400-\u9fff]', re.I)


def _terms(value: Any) -> list[str]:
    raw = _WORD_RE.findall(str(value or '').lower())
    out = []
    for word in raw:
        out.append(word)
        out.extend(part for part in re.split(r'[_./:-]+', word)
                   if part and part != word)
    return out


def legacy_search_enabled_catalog(catalog, query, *, limit=8,
                                  namespace_by_name=None, **_ignored):
    """The former name/description/property BM25 behaviour, for A/B replay."""
    query_text = str(query or '').strip()
    namespace_map = namespace_by_name or {}
    docs = []
    for position, tool in enumerate(catalog or []):
        fn = tool.get('function') if isinstance(tool, dict) else None
        if not isinstance(fn, dict) or not fn.get('name'):
            continue
        name = str(fn['name'])
        params = fn.get('parameters') or {}
        props = params.get('properties') or {}
        ns = str(namespace_map.get(name) or 'general').lower()
        text = ' '.join((name, ns, str(fn.get('description') or ''),
                         ' '.join(props)))
        docs.append((position, name, fn, ns, _terms(text)))
    qterms = _terms(query_text)
    frequencies = Counter()
    for *_prefix, terms in docs:
        frequencies.update(set(terms))
    average = sum(len(row[-1]) for row in docs) / max(1, len(docs))
    scored = []
    for position, name, fn, ns, terms in docs:
        counts = Counter(terms)
        score = 0.0
        for term in qterms:
            freq = counts.get(term, 0)
            if not freq:
                continue
            df = frequencies.get(term, 0)
            inverse = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
            denom = freq + 1.2 * (
                0.25 + 0.75 * len(terms) / max(average, 1))
            score += inverse * (freq * 2.2) / denom
        lowered = query_text.lower()
        if lowered == name.lower():
            score += 100
        elif lowered in name.lower():
            score += 12
        if score > 0:
            scored.append((-score, position, name, fn, ns))
    scored.sort()
    items = [{
        'name': name, 'namespace': ns,
        'description': str(fn.get('description') or ''),
        'arguments_schema': fn.get('parameters') or {},
        'score': round(-negative, 6),
    } for negative, _position, name, fn, ns in scored[:int(limit)]]
    return {'status': 'ok', 'query': query_text, 'items': items,
            'total': len(scored), 'next_cursor': None}

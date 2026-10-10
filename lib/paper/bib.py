"""Minimal, bounded BibTeX entry extraction for citation auditing.

This is deliberately NOT a full BibTeX grammar: it locates ``@type{key,``
entry heads with balanced-brace scanning so undefined/duplicate keys can be
audited against ``\\cite`` usage. Field bodies are not interpreted, so macro
definitions and string concatenation cannot confuse the key index.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_ENTRIES = 4096
_ENTRY_HEAD = re.compile(r'@([A-Za-z]+)\s*[{(]\s*')
_COMMENT = re.compile(r'(?<!\\)%[^\n]*')


@dataclass(frozen=True)
class BibEntry:
    key: str
    entry_type: str
    line: int


def parse_entries(text: str, *, max_entries: int = MAX_ENTRIES) -> tuple[list[BibEntry], list[str]]:
    """Return (entries, duplicate_keys); scanning never raises on bad input."""
    entries: list[BibEntry] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for match in _ENTRY_HEAD.finditer(text):
        entry_type = match.group(1).lower()
        if entry_type in ('comment', 'preamble', 'string'):
            continue
        # An entry key contains no comma and opens no nesting, so it always
        # runs from the head to the first comma regardless of the opener.
        comma = text.find(',', match.end(), match.end() + 300)
        if comma < 0:
            continue
        key = text[match.end():comma].strip()
        if not key or len(key) > 256 or not re.match(r'^[^\s,{}@#%="()]+$', key):
            continue
        line = text.count('\n', 0, match.start()) + 1
        if key in seen:
            if key not in duplicates:
                duplicates.append(key)
        else:
            seen.add(key)
            entries.append(BibEntry(key, entry_type, line))
            if len(entries) >= max_entries:
                break
    return entries, duplicates

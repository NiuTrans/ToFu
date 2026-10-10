"""Visibility of the clock fact in the current working messages.

The renderer uses this read-only check before appending ``current_date``.
Only owned clock sections and top-level system/developer facts are witnesses;
user/tool text, quoted XML and fenced examples never establish the clock.
No conversation cache or second time source is maintained here.
"""
from __future__ import annotations

from datetime import date
import re
from typing import Any

from ._sections import _strip_to_body

_OWNED_SECTION = re.compile(
    r'<!-- tofu-context:([^:\n]+):start -->.*?'
    r'<!-- tofu-context:\1:end -->', re.DOTALL)
_CLOCK_FACT = re.compile(r'Current date: (\d{4}-\d{2}-\d{2})')
_XML_EXCERPT = re.compile(r'<([A-Za-z][\w:-]*)\b[^>]*>.*?</\1\s*>', re.DOTALL)


def _clock_fact(text: str) -> str:
    match = _CLOCK_FACT.fullmatch(text.strip())
    if match is None:
        return ''
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return ''
    return match.group(0)


def _policy_date(text: str) -> str:
    # Policy may quote examples/attachments. Only an unindented, standalone
    # fact outside XML excerpts and Markdown fences supplies the actual date.
    text = _XML_EXCERPT.sub('', text)
    latest, fence = '', ''
    for line in text.splitlines():
        stripped = line.lstrip()
        marker = re.match(r'(`{3,}|~{3,})', stripped)
        if marker:
            run = marker.group(1)
            if not fence:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence):
                fence = ''
            continue
        if not fence and line == line.lstrip():
            fact = _clock_fact(line)
            if fact:
                latest = fact
    return latest


def visible_clock_date(messages: list[dict[str, Any]]) -> str:
    """Return the latest trusted complete clock fact actually still visible."""
    latest = ''
    for message in messages:
        if not isinstance(message, dict):
            continue
        owned = bool(message.get('_contextComposer'))
        policy = message.get('role') in ('system', 'developer')
        if not (owned or policy):
            continue
        blocks = message.get('_contextBlocks')
        clock_carrier = (owned and (not isinstance(blocks, list) or any(
            isinstance(block, dict) and block.get('id') == 'current_date'
            for block in blocks)))
        if not (clock_carrier or policy):
            continue
        content = message.get('content')
        parts = content if isinstance(content, list) else [content]
        for part in parts:
            text = part.get('text') if isinstance(part, dict) else part
            if not isinstance(text, str):
                continue
            # Managed clock sections are trusted only on owned carriers.
            if clock_carrier:
                # Match outer owned sections first. A clock example inside a
                # memory/project section stays evidence even on replay that
                # preserved ownership but omitted per-block metadata.
                for match in _OWNED_SECTION.finditer(text):
                    if match.group(1) == 'current_date':
                        fact = _clock_fact(_strip_to_body(match.group(0)))
                        if fact:
                            latest = fact
            if policy:
                fact = _policy_date(text)
                if fact:
                    latest = fact
    return latest

"""Shared length-labelled text framing for model-only result projections.

Entry: text_section, compact_text_field, load_text_field. Callers admit the
producer shape and retain canonical values before model budgeting. Body text
is opaque; no parsing or clipping.
Length counts Unicode codepoints, so embedded section headings stay body data.
"""
from __future__ import annotations

import json
import re

from lib.tools.result_projection_json import dump_projection_json
from lib.tools.result_surfaces_generated import CONTRACT

_CONTROL_CHARACTERS = re.compile('[' + ''.join(
    re.escape(chr(code)) for code in range(32)
    if chr(code) not in CONTRACT['textProjection']['allowedControlCharacters']) + ']')


def text_section(field: str, body: str) -> str:
    return f'{field} (text; {len(body)} chars):\n{body}'


def compact_text_field(value: dict, field: str, *, original: str | None = None) -> str:
    """Render one opaque string only when the complete view uses fewer bytes."""
    if original is None:
        original = dump_projection_json(value)
    body = value.get(field)
    if type(body) is not str or _CONTROL_CHARACTERS.search(body):
        return original
    if not any(char in body for char in ('"', '\\', '\t', '\n', '\r')):
        return original
    metadata = {key: item for key, item in value.items() if key != field}
    try:
        candidate = dump_projection_json(metadata) + '\n' + text_section(field, body)
        return candidate if len(candidate.encode('utf-8')) < len(original.encode('utf-8')) else original
    except (ValueError, TypeError, RecursionError, OverflowError):
        return original


def load_text_field(text: str, field: str) -> dict | None:
    """Read an exact framed view, never a clipped or ambiguous body.

    Only model observation/error readers use this display decoder. Canonical
    capture and decoded program values must continue to use their source owner.
    """
    metadata, separator, tail = text.partition('\n')
    label, newline, body = tail.partition('\n')
    match = re.fullmatch(re.escape(field) + r' \(text; ([0-9]+) chars\):', label)
    if not separator or not newline or match is None:
        return None
    try:
        if int(match.group(1)) != len(body):
            return None
        value = json.loads(metadata)
    except (ValueError, TypeError, RecursionError):
        return None
    return {**value, field: body} if isinstance(value, dict) and field not in value else None

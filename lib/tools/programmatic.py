"""Production contracts for OpenAI Programmatic Tool Calling (PTC).

The ordinary tool runtime returns canonical text.  PTC JavaScript needs a
predictable JSON object, so every opted-in tool exposes the same lossless text
envelope.  Eligibility is separately and explicitly declared on ``ToolSpec``;
it must never be inferred from the broader retry/dedup partition.
"""

from __future__ import annotations

import json
from typing import Any


# Hard application-side ceilings.  The hosted V8 runtime owns its own runtime
# timeout; these limits bound the client-owned calls and bytes that Tofu feeds
# back into one program, plus protocol continuations after program_output.
PROGRAMMATIC_MAX_CALLS = 16
PROGRAMMATIC_MAX_CONCURRENT_CALLS = 8
PROGRAMMATIC_MAX_OUTPUT_BYTES = 1_048_576
PROGRAMMATIC_MAX_CONTINUATIONS = 4


def programmatic_output_schema() -> dict[str, Any]:
    """Return a fresh exact schema for Tofu's canonical text envelope."""
    return {
        'type': 'object',
        'properties': {
            'content': {'type': 'string'},
            'truncated': {'type': 'boolean'},
        },
        'required': ['content', 'truncated'],
        'additionalProperties': False,
    }


def eligible_programmatic_tool_names() -> set[str]:
    """Return explicitly reviewed built-in tool names.

    Third-party plugins remain direct-only until the plugin trust/approval
    boundary has an equally explicit PTC review mechanism.
    """
    from lib.tools import all_specs

    names: set[str] = set()
    for spec in all_specs():
        if spec.source == 'builtin':
            # Fail closed if a registry edit misspells or over-declares a name.
            names.update(spec.programmatic_tools.intersection(spec.provides))
    return names


def truncate_utf8(value: str, max_bytes: int) -> tuple[str, bool]:
    """Clamp *value* without splitting a UTF-8 code point."""
    raw = value.encode('utf-8')
    if len(raw) <= max(0, max_bytes):
        return value, False
    if max_bytes <= 0:
        return '', True
    return raw[:max_bytes].decode('utf-8', errors='ignore'), True


def encode_programmatic_output(content: Any, *, max_bytes: int | None = None
                               ) -> tuple[str, int, bool]:
    """Encode one tool result as the exact PTC JSON envelope.

    Returns ``(json_text, consumed_utf8_bytes, truncated)`` so the caller can
    maintain a cumulative per-program byte budget without reparsing JSON.
    """
    text = (content if isinstance(content, str)
            else json.dumps(content if content is not None else '',
                            ensure_ascii=False))
    truncated = False
    if max_bytes is not None:
        text, truncated = truncate_utf8(text, max_bytes)
    consumed = len(text.encode('utf-8'))
    return (json.dumps({'content': text, 'truncated': truncated},
                       ensure_ascii=False), consumed, truncated)


__all__ = [
    'PROGRAMMATIC_MAX_CALLS',
    'PROGRAMMATIC_MAX_CONCURRENT_CALLS',
    'PROGRAMMATIC_MAX_OUTPUT_BYTES',
    'PROGRAMMATIC_MAX_CONTINUATIONS',
    'eligible_programmatic_tool_names',
    'encode_programmatic_output',
    'programmatic_output_schema',
    'truncate_utf8',
]

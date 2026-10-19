"""Bounded MCP wire validation shared by transient SDK and durable storage.

Validates and detaches complete declarations; never grants tool authority or
opens storage. The conversation repository owns persistence and identity.
"""
from __future__ import annotations

import copy
import json

from lib.agent_core.tool_errors import MCPDeclarationError

MAX_DECLARATION_BYTES = 256 * 1024
MAX_DECLARATIONS = 128


def validate_definitions(definitions: list[dict]) -> list[dict]:
    """Detach a complete wire or reject it; never silently truncate schemas."""
    if not isinstance(definitions, list) or len(definitions) > MAX_DECLARATIONS:
        raise MCPDeclarationError('MCP declaration count exceeds the bounded slot')
    names = set()
    for tool in definitions:
        function = tool.get('function') if isinstance(tool, dict) else None
        name = function.get('name') if isinstance(function, dict) else None
        if (not isinstance(tool, dict) or tool.get('type') != 'function'
                or not isinstance(name, str) or not name.startswith('mcp__')
                or name in names):
            raise MCPDeclarationError('Malformed or duplicate MCP declaration')
        names.add(name)
    try:
        size = 0
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False,
                                   separators=(',', ':'))
        for part in encoder.iterencode(definitions):
            if size + len(part) > MAX_DECLARATION_BYTES:
                raise MCPDeclarationError('MCP declaration bytes exceed the bounded slot')
            size += len(part.encode('utf-8'))
            if size > MAX_DECLARATION_BYTES:
                raise MCPDeclarationError('MCP declaration bytes exceed the bounded slot')
    except (TypeError, ValueError, RecursionError) as exc:
        raise MCPDeclarationError('MCP declarations are not JSON') from exc
    return copy.deepcopy(definitions)

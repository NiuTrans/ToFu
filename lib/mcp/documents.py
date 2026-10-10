"""Bounded JSON documents at the MCP result boundary.

Human-readable tool output remains a string. Application consumers use
decode_document to recover the complete structured document, or an exact JSON
text result from older servers. Errors and truncated documents never parse as
successful receipts. Transport and owner authorization remain with the caller.
"""
from __future__ import annotations

import json
import math

from .types import MCP_MAX_DOCUMENT_BYTES, MCP_MAX_DOCUMENT_DEPTH, MCP_MAX_DOCUMENT_NODES


class MCPDocumentError(ValueError):
    """A complete, bounded JSON result could not be recovered."""


def encode_document(value, *, max_bytes=MCP_MAX_DOCUMENT_BYTES):
    """Validate before serialization so nested/large SDK values stay bounded."""
    nodes = 0
    string_bytes = 0

    def visit(item, depth):
        nonlocal nodes, string_bytes
        nodes += 1
        if nodes > MCP_MAX_DOCUMENT_NODES or depth > MCP_MAX_DOCUMENT_DEPTH:
            raise MCPDocumentError('MCP document exceeds its structure limit')
        if isinstance(item, str):
            if len(item) > max_bytes:
                raise MCPDocumentError('MCP document exceeds its byte limit')
            try:
                string_bytes += len(item.encode('utf-8'))
            except UnicodeError as exc:
                raise MCPDocumentError('MCP document contains invalid text') from exc
            if string_bytes > max_bytes:
                raise MCPDocumentError('MCP document exceeds its byte limit')
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise MCPDocumentError('MCP document keys must be strings')
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif item is None or isinstance(item, bool):
            string_bytes += 5
        elif isinstance(item, int):
            string_bytes += item.bit_length() * 30103 // 100000 + 2
        elif isinstance(item, float) and math.isfinite(item):
            string_bytes += 24
        else:
            raise MCPDocumentError('MCP document contains a non-JSON value')
        if string_bytes > max_bytes:
            raise MCPDocumentError('MCP document exceeds its byte limit')

    visit(value, 0)
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise MCPDocumentError('MCP document is invalid JSON') from exc
    if len(text.encode('utf-8')) > max_bytes:
        raise MCPDocumentError('MCP document exceeds its byte limit')
    return text


def structured_document(result):
    """Return the SDK payload across v1/v2 field spellings, without copying it."""
    for name in ('structured_content', 'structuredContent'):
        value = result.get(name) if isinstance(result, dict) else getattr(result, name, None)
        if value is not None:
            return value
    return None


def decode_document(result):
    """Prefer typed content; legacy compatibility accepts exact JSON only."""
    if getattr(result, 'is_error', False):
        raise MCPDocumentError('MCP tool returned an error')
    if getattr(result, 'document_error', None):
        raise MCPDocumentError(result.document_error)
    raw = getattr(result, 'structured_json', None)
    if raw is None:
        raw = getattr(result, 'document_text', None)
    if raw is None:
        raw = str(result)
    if len(raw) > MCP_MAX_DOCUMENT_BYTES:
        raise MCPDocumentError('MCP document exceeds its byte limit')
    try:
        if len(raw.encode('utf-8')) > MCP_MAX_DOCUMENT_BYTES:
            raise MCPDocumentError('MCP document exceeds its byte limit')
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise MCPDocumentError('MCP tool did not return a complete JSON document') from exc
    encode_document(value)
    return value

"""Progressive disclosure for large MCP tool catalogs.

Shipping every connected MCP schema on every model round is both expensive
and noisy: a few servers can contribute hundreds of functions and hundreds of
kilobytes of JSON.  This module keeps the on-wire surface stable at three
small meta tools once the catalog crosses a configurable threshold:

* ``search_mcp_tools`` discovers relevant enabled tools and returns their
  exact input schemas.
* ``call_mcp_read_tool`` invokes only tools explicitly annotated read-only.
* ``call_mcp_write_tool`` invokes only tools not annotated read-only, keeping
  the existing serial-dispatch and human-approval safety boundary intact.

Small catalogs stay inline to avoid adding a discovery round.  Operators can
override the automatic choice with ``mcpToolExposure=inline|progressive`` in
request config or ``TOFU_MCP_TOOL_EXPOSURE``.  The automatic inline ceiling is
``mcpInlineToolLimit`` / ``TOFU_MCP_INLINE_TOOL_LIMIT`` (default 16).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from lib.log import get_logger
from lib.mcp.types import parse_namespaced_name

logger = get_logger(__name__)


MCP_SEARCH_TOOL_NAME = 'search_mcp_tools'
MCP_READ_TOOL_NAME = 'call_mcp_read_tool'
MCP_WRITE_TOOL_NAME = 'call_mcp_write_tool'
MCP_PROGRESSIVE_TOOL_NAMES = frozenset({
    MCP_SEARCH_TOOL_NAME,
    MCP_READ_TOOL_NAME,
    MCP_WRITE_TOOL_NAME,
})

_DEFAULT_INLINE_TOOL_LIMIT = 16
_MAX_SEARCH_RESULTS = 8


MCP_PROGRESSIVE_TOOL_DEFS = [
    {
        'type': 'function',
        'function': {
            'name': MCP_SEARCH_TOOL_NAME,
            'description': (
                'Search the connected MCP tool catalog before using an MCP '
                'capability. Returns exact tool names, descriptions, input '
                'schemas, safety class, and the correct call wrapper.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': (
                            'Capability or action to find, such as "search '
                            'GitHub issues" or "edit an Overleaf document".'
                        ),
                    },
                    'server': {
                        'type': 'string',
                        'description': 'Optional exact MCP server name.',
                    },
                    'limit': {
                        'type': 'integer',
                        'minimum': 1,
                        'maximum': _MAX_SEARCH_RESULTS,
                        'default': 5,
                    },
                },
                'required': ['query'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': MCP_READ_TOOL_NAME,
            'description': (
                'Call an MCP tool that search_mcp_tools marked read_only=true. '
                'Use the exact namespaced name and arguments matching its '
                'returned input_schema.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {
                        'type': 'string',
                        'description': 'Exact mcp__server__tool name.',
                    },
                    'arguments': {
                        'type': 'object',
                        'description': (
                            'JSON object whose arguments match the discovered '
                            'input schema. Pass an object directly; do not '
                            'JSON-encode it as a string.'
                        ),
                    },
                },
                'required': ['name', 'arguments'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': MCP_WRITE_TOOL_NAME,
            'description': (
                'Call an MCP tool that search_mcp_tools marked read_only=false. '
                'This path is serialized and may require user approval. Use '
                'the exact namespaced name and schema-matching arguments.'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'name': {
                        'type': 'string',
                        'description': 'Exact mcp__server__tool name.',
                    },
                    'arguments': {
                        'type': 'object',
                        'description': (
                            'JSON object whose arguments match the discovered '
                            'input schema. Pass an object directly; do not '
                            'JSON-encode it as a string.'
                        ),
                    },
                },
                'required': ['name', 'arguments'],
                'additionalProperties': False,
            },
        },
    },
]


def _coerce_inline_limit(cfg: dict[str, Any]) -> int:
    """Resolve the automatic inline-catalog ceiling with safe bounds."""
    raw = cfg.get('mcpInlineToolLimit')
    if raw is None:
        raw = os.environ.get('TOFU_MCP_INLINE_TOOL_LIMIT',
                             str(_DEFAULT_INLINE_TOOL_LIMIT))
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        logger.debug('[MCP:Progressive] invalid inline limit %r (%s); using %d',
                     raw, e, _DEFAULT_INLINE_TOOL_LIMIT)
        value = _DEFAULT_INLINE_TOOL_LIMIT
    return max(0, min(256, value))


def use_progressive_mcp(cfg: dict[str, Any], tool_count: int) -> bool:
    """Return whether this request should expose the compact MCP meta tools.

    ``auto`` is the default and switches only when the number of enabled MCP
    tools exceeds the inline ceiling.  Invalid modes fail open to ``auto`` so
    a typo cannot silently remove MCP capabilities.
    """
    mode = cfg.get('mcpToolExposure')
    if mode is None:
        mode = os.environ.get('TOFU_MCP_TOOL_EXPOSURE', 'auto')
    mode = str(mode).strip().lower()
    if mode in ('progressive', 'search', 'deferred'):
        return True
    if mode in ('inline', 'all', 'full'):
        return False
    if mode not in ('', 'auto'):
        logger.warning('[MCP:Progressive] unknown exposure mode=%r; using auto',
                       mode)
    return int(tool_count or 0) > _coerce_inline_limit(cfg)


def _query_terms(query: str) -> list[str]:
    """Tokenize a multilingual search query without external dependencies."""
    query = (query or '').strip().lower()
    if not query:
        return []
    terms = re.findall(r'[\w.-]+', query, flags=re.UNICODE)
    return list(dict.fromkeys([query, *terms]))


def search_mcp_catalog(bridge, query: str, *, server: str = '',
                       limit: int = 5) -> str:
    """Search enabled MCP definitions and return bounded schema-rich JSON."""
    terms = _query_terms(query)
    if not terms:
        return json.dumps({
            'error': 'query must not be empty',
            'matches': [],
        }, ensure_ascii=False)

    server_filter = (server or '').strip().lower()
    try:
        result_limit = max(1, min(_MAX_SEARCH_RESULTS, int(limit)))
    except (TypeError, ValueError) as e:
        logger.debug('[MCP:Progressive] invalid search limit %r (%s); using 5',
                     limit, e)
        result_limit = 5

    safety = bridge.get_tool_safety()
    ranked: list[tuple[int, str, dict[str, Any], str]] = []
    for tool_def in bridge.get_openai_tool_defs():
        fn = tool_def.get('function') or {}
        name = str(fn.get('name') or '')
        parsed = parse_namespaced_name(name)
        if not name or parsed is None:
            continue
        server_name, short_name = parsed
        if server_filter and server_name.lower() != server_filter:
            continue

        description = str(fn.get('description') or '')
        name_text = f'{name} {short_name}'.lower()
        desc_text = description.lower()
        score = 0
        full_query = terms[0]
        if full_query == name.lower() or full_query == short_name.lower():
            score += 200
        if full_query in name_text:
            score += 80
        elif full_query in desc_text:
            score += 30
        for term in terms[1:]:
            if term in name_text:
                score += 20
            elif term in desc_text:
                score += 6
        if score <= 0:
            continue
        ranked.append((score, name, fn, server_name))

    ranked.sort(key=lambda row: (-row[0], row[1]))
    matches = []
    for score, name, fn, server_name in ranked[:result_limit]:
        read_only = bool(safety.get(name, False))
        matches.append({
            'name': name,
            'server': server_name,
            'description': fn.get('description') or '',
            'input_schema': fn.get('parameters') or {
                'type': 'object', 'properties': {},
            },
            'read_only': read_only,
            'invoke_with': (MCP_READ_TOOL_NAME if read_only
                            else MCP_WRITE_TOOL_NAME),
            'relevance_score': score,
        })

    return json.dumps({
        'query': query,
        'server': server or None,
        'matches': matches,
        'instruction': (
            'Call invoke_with using {"name": match.name, "arguments": {...}}. '
            'Arguments must match input_schema exactly.'
        ),
    }, ensure_ascii=False, separators=(',', ':'))


def call_progressive_mcp(bridge, wrapper_name: str,
                         namespaced_name: str, arguments: Any) -> str:
    """Validate a progressive wrapper's safety class, then call the MCP tool."""
    name = str(namespaced_name or '').strip()
    if parse_namespaced_name(name) is None:
        return ('MCP tool error: invalid namespaced name. Run search_mcp_tools '
                'and use the exact mcp__server__tool value it returns.')
    if not isinstance(arguments, dict):
        return 'MCP tool error: arguments must be a JSON object.'

    info = bridge.get_tool_info(name)
    if info is None:
        return ('MCP tool error: tool is unavailable or unknown. Run '
                'search_mcp_tools again to refresh the enabled catalog.')
    read_only = bool(info.get('read_only_hint', False))
    if wrapper_name == MCP_READ_TOOL_NAME and not read_only:
        return (f'MCP tool error: {name} is not annotated read-only; use '
                f'{MCP_WRITE_TOOL_NAME} so serialization and approval apply.')
    if wrapper_name == MCP_WRITE_TOOL_NAME and read_only:
        return (f'MCP tool error: {name} is read-only; use '
                f'{MCP_READ_TOOL_NAME} for the parallel safe path.')
    return bridge.call_tool(name, arguments)

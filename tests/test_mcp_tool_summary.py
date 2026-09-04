"""Compact MCP inventory used by first-screen and Settings projections."""

from __future__ import annotations

import json
import threading

import pytest


pytestmark = pytest.mark.unit


def _bridge(*, servers, tools, disabled=None):
    from lib.mcp.client._bridge import MCPBridge

    bridge = MCPBridge.__new__(MCPBridge)
    bridge._lock = threading.Lock()
    bridge._servers = {name: object() for name in servers}
    bridge._configs = {
        name: {'disabled_tools': list((disabled or {}).get(name, ()))}
        for name in servers
    }
    bridge._tool_index = {
        f'mcp__{server}__{name}': {
            'server_name': server,
            'tool_name': name,
            # Large fields prove the summary path does not need to understand
            # or reproduce schema-bearing inventory rows.
            'description': 'description-' + ('x' * 256),
            'openai_def': {
                'type': 'function',
                'function': {
                    'name': f'mcp__{server}__{name}',
                    'parameters': {'type': 'object', 'description': 'y' * 512},
                },
            },
        }
        for server, name in tools
    }
    return bridge


def test_enabled_summary_is_sorted_exact_and_omits_non_live_or_zero_servers():
    bridge = _bridge(
        servers=['zeta', 'empty', 'alpha'],
        tools=[
            ('zeta', 'two'), ('alpha', 'disabled'), ('ghost', 'stale'),
            ('zeta', 'one'), ('alpha', 'enabled'),
        ],
        disabled={'alpha': ['disabled', 'no-longer-discovered']},
    )

    assert bridge.get_enabled_tool_summary() == {
        'servers': [
            {'name': 'alpha', 'count': 1},
            {'name': 'zeta', 'count': 2},
        ],
        'total': 3,
    }


def test_240_tool_schema_inventory_collapses_below_one_percent_on_the_wire():
    tools = [
        (f'server-{server_index}', f'tool-{tool_index:03d}')
        for server_index in range(6)
        for tool_index in range(40)
    ]
    bridge = _bridge(
        servers=[f'server-{index}' for index in range(6)],
        tools=tools,
    )
    summary = bridge.get_enabled_tool_summary()
    full_rows = list(bridge._tool_index.values())
    compact_bytes = len(json.dumps(
        summary, sort_keys=True, separators=(',', ':')).encode())
    schema_bytes = len(json.dumps(
        full_rows, sort_keys=True, separators=(',', ':')).encode())

    assert summary['total'] == 240
    assert compact_bytes < 512
    assert compact_bytes * 100 < schema_bytes, (
        f'compact MCP summary grew beyond 1% of schema inventory: '
        f'{compact_bytes} vs {schema_bytes} bytes')


def test_optional_route_projection_cannot_fail_the_owning_operation():
    from routes.api_v1.mcp import _enabled_tool_summary

    class BrokenSummaryBridge:
        def get_enabled_tool_summary(self):
            raise RuntimeError('fault-injected summary failure')

    assert _enabled_tool_summary(BrokenSummaryBridge()) == {
        'servers': [],
        'total': 0,
    }

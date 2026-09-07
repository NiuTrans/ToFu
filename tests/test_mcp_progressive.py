"""Cost and safety regression tests for progressive MCP disclosure."""

from __future__ import annotations

import json

import pytest

from lib.mcp.progressive import (
    MCP_PROGRESSIVE_TOOL_DEFS,
    MCP_READ_TOOL_NAME,
    MCP_SEARCH_TOOL_NAME,
    MCP_WRITE_TOOL_NAME,
    call_progressive_mcp,
    search_mcp_catalog,
    use_progressive_mcp,
)

pytestmark = pytest.mark.unit


def _tool(name: str, *, description: str = '', read_only: bool = False):
    definition = {
        'type': 'function',
        'function': {
            'name': name,
            'description': description,
            'parameters': {
                'type': 'object',
                'properties': {'query': {'type': 'string'}},
            },
        },
    }
    return definition, read_only


class _Bridge:
    def __init__(self, tools):
        self._defs = [row[0] for row in tools]
        self._safety = {
            row[0]['function']['name']: row[1] for row in tools
        }
        self.calls = []

    def get_openai_tool_defs(self):
        return self._defs

    def get_tool_safety(self):
        return self._safety

    def get_tool_info(self, name):
        for definition in self._defs:
            fn = definition['function']
            if fn['name'] == name:
                return {
                    'server_name': name.split('__')[1],
                    'tool_name': name.split('__')[-1],
                    'read_only_hint': self._safety[name],
                }
        return None

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps({'ok': True, 'name': name})


def test_large_catalog_defaults_to_three_stable_meta_tools():
    assert use_progressive_mcp({}, 17) is True
    assert use_progressive_mcp({}, 16) is False
    assert [t['function']['name'] for t in MCP_PROGRESSIVE_TOOL_DEFS] == [
        MCP_SEARCH_TOOL_NAME, MCP_READ_TOOL_NAME, MCP_WRITE_TOOL_NAME]


def test_meta_tool_contract_explains_discovery_arguments_and_safety():
    by_name = {
        definition['function']['name']: definition['function']
        for definition in MCP_PROGRESSIVE_TOOL_DEFS
    }
    search = by_name[MCP_SEARCH_TOOL_NAME]
    read = by_name[MCP_READ_TOOL_NAME]
    write = by_name[MCP_WRITE_TOOL_NAME]

    assert 'before' in search['description'].lower()
    assert 'exact' in search['description'].lower()
    assert 'read_only=true' in read['description']
    assert 'read_only=false' in write['description']
    assert 'approval' in write['description'].lower()
    for wrapper in (read, write):
        schema = wrapper['parameters']
        assert schema['required'] == ['name', 'arguments']
        assert schema['additionalProperties'] is False
        assert schema['properties']['arguments']['type'] == 'object'
        assert 'JSON object' in schema['properties']['arguments']['description']


def test_inline_override_keeps_small_or_large_catalog_inline():
    assert use_progressive_mcp({'mcpToolExposure': 'inline'}, 240) is False
    assert use_progressive_mcp({'mcpToolExposure': 'progressive'}, 1) is True


def test_240_tool_catalog_schema_bytes_collapse_by_more_than_95_percent():
    tools = [
        _tool(
            f'mcp__server{i % 5}__operation_{i}',
            description=('Detailed remote capability and usage guidance. ' * 20),
        )[0]
        for i in range(240)
    ]
    inline_bytes = len(json.dumps(tools, ensure_ascii=False))
    progressive_bytes = len(json.dumps(
        MCP_PROGRESSIVE_TOOL_DEFS, ensure_ascii=False))
    assert progressive_bytes < inline_bytes * 0.05


def test_search_returns_exact_schema_safety_and_wrapper():
    bridge = _Bridge([
        _tool('mcp__github__search_issues',
              description='Search GitHub issues', read_only=True),
        _tool('mcp__github__create_issue',
              description='Create a GitHub issue', read_only=False),
    ])
    result = json.loads(search_mcp_catalog(bridge, 'search issues'))
    match = result['matches'][0]
    assert match['name'] == 'mcp__github__search_issues'
    assert match['read_only'] is True
    assert match['invoke_with'] == MCP_READ_TOOL_NAME
    assert match['input_schema']['properties']['query']['type'] == 'string'


def test_read_write_wrappers_enforce_mcp_safety_annotation():
    bridge = _Bridge([
        _tool('mcp__docs__read_page', read_only=True),
        _tool('mcp__docs__edit_page', read_only=False),
    ])

    denied_write = call_progressive_mcp(
        bridge, MCP_WRITE_TOOL_NAME, 'mcp__docs__read_page', {})
    denied_read = call_progressive_mcp(
        bridge, MCP_READ_TOOL_NAME, 'mcp__docs__edit_page', {})
    assert 'is read-only' in denied_write
    assert 'not annotated read-only' in denied_read
    assert bridge.calls == []

    call_progressive_mcp(
        bridge, MCP_READ_TOOL_NAME, 'mcp__docs__read_page', {'id': 1})
    call_progressive_mcp(
        bridge, MCP_WRITE_TOOL_NAME, 'mcp__docs__edit_page', {'id': 1})
    assert bridge.calls == [
        ('mcp__docs__read_page', {'id': 1}),
        ('mcp__docs__edit_page', {'id': 1}),
    ]

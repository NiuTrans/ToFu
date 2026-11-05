"""MCP tool names reach providers inside their function-name contract.

Problem: the namespaced name ``mcp__{server}__{tool}`` copied the server's raw
tool name. Kingsoft Docs (金山文档) names 574 of its 630 tools with dots
(``sheet.get_range_data``), and Anthropic and OpenAI both reject any function
name outside ``^[a-zA-Z0-9_-]{1,64}$`` — one such definition 400s the whole
request, and a model call using the dotted name is dropped as malformed.
Decision: the namespaced name is provider-safe (unsafe characters become ``_``,
an over-long tool part is shortened with a digest), while the index keeps the
server's raw tool name for dispatch, per-tool disabling and display search.
"""

import asyncio
import re
from types import SimpleNamespace

import pytest

from lib.mcp.client._bridge import MCPBridge, _MCPServerHandle
from lib.mcp.types import (
    PROVIDER_TOOL_NAME_MAX,
    make_namespaced_name,
    namespaced_tool_names,
)

pytestmark = pytest.mark.unit

PROVIDER_NAME = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


class _Tool:
    def __init__(self, name):
        self.name = name
        self.description = f'{name} description'
        self.inputSchema = {'type': 'object', 'properties': {}}
        self.annotations = None


def test_dotted_tool_name_becomes_provider_safe():
    name = make_namespaced_name('kdocs', 'sheet.get_range_data')
    assert name == 'mcp__kdocs__sheet_get_range_data'
    assert PROVIDER_NAME.fullmatch(name)


@pytest.mark.parametrize(('server', 'tool', 'expected'), [
    ('srv', 'read_doc', 'mcp__srv__read_doc'),
    ('hope', 'hope_login', 'mcp__hope__login'),
    ('amap-maps', 'maps-geo', 'mcp__amap-maps__maps-geo'),
])
def test_names_already_inside_the_contract_are_unchanged(server, tool, expected):
    assert make_namespaced_name(server, tool) == expected


def test_overlong_tool_part_is_shortened_with_a_stable_distinct_digest():
    long_a = 'x' * 70 + '.a'
    long_b = 'x' * 70 + '.b'
    name_a = make_namespaced_name('srv', long_a)
    assert len(name_a) == PROVIDER_TOOL_NAME_MAX
    assert PROVIDER_NAME.fullmatch(name_a)
    assert name_a.startswith('mcp__srv__')
    assert name_a == make_namespaced_name('srv', long_a)
    assert name_a != make_namespaced_name('srv', long_b)


def test_sanitizing_collision_keeps_the_exact_name_and_suffixes_the_rewrite():
    names = namespaced_tool_names('srv', ['a_b', 'a.b'])
    assert names['a_b'] == 'mcp__srv__a_b'
    assert names['a.b'] != names['a_b']
    assert names['a.b'].startswith('mcp__srv__a_b_')
    assert all(PROVIDER_NAME.fullmatch(value) for value in names.values())
    # Input order never changes which spelling keeps the plain name.
    assert namespaced_tool_names('srv', ['a.b', 'a_b']) == names


def test_bridge_index_definition_and_raw_name_agree():
    bridge = MCPBridge()
    handle = _MCPServerHandle('kdocs', {})
    bridge._replace_server_catalog(
        'kdocs', handle, [_Tool('sheet.get_range_data'), _Tool('read_file')])

    assert set(bridge._tool_index) == {
        'mcp__kdocs__sheet_get_range_data', 'mcp__kdocs__read_file'}
    info = bridge._tool_index['mcp__kdocs__sheet_get_range_data']
    assert info['tool_name'] == 'sheet.get_range_data'
    assert info['namespaced_name'] == 'mcp__kdocs__sheet_get_range_data'
    assert info['openai_def']['function']['name'] == info['namespaced_name']
    for definition in bridge.get_openai_tool_defs():
        assert PROVIDER_NAME.fullmatch(definition['function']['name'])


def _connected_bridge(configs=None):
    bridge = MCPBridge()
    handle = _MCPServerHandle('kdocs', {})
    seen = []

    async def call_tool(name, arguments=None, read_timeout_seconds=None):
        seen.append(name)
        return SimpleNamespace(
            content=[SimpleNamespace(type='text', text='ok')],
            structuredContent=None, isError=False)

    handle.session = SimpleNamespace(call_tool=call_tool)
    bridge._servers['kdocs'] = handle
    bridge._configs = dict(configs or {})
    bridge._replace_server_catalog(
        'kdocs', handle, [_Tool('sheet.get_range_data')])
    bridge._run_async = lambda coro, timeout=None, **_kwargs: asyncio.run(coro)
    return bridge, seen


def test_call_dispatches_the_servers_raw_tool_name():
    bridge, seen = _connected_bridge()
    assert bridge.call_tool('mcp__kdocs__sheet_get_range_data', {}) == 'ok'
    assert seen == ['sheet.get_range_data']


def test_disabling_the_raw_name_refuses_the_provider_safe_call():
    bridge, seen = _connected_bridge(
        {'kdocs': {'disabled_tools': ['sheet.get_range_data']}})
    with pytest.raises(ValueError, match='disabled by user'):
        bridge.call_tool('mcp__kdocs__sheet_get_range_data', {})
    assert seen == []

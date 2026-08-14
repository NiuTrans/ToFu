"""Pre-request MCP catalog search, cache, sticky state, and workflows."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lib.mcp.client._bridge import MCPBridge, _MCPServerHandle
from lib.mcp.tool_search import (
    build_catalog_index,
    canonical_schema_hash,
    invalidate_server_catalog,
    record_mcp_tool_used,
    select_active_mcp_tools,
)


pytestmark = pytest.mark.unit


def _definition(server, name, description=''):
    return {
        'type': 'function',
        'function': {
            'name': f'mcp__{server}__{name}',
            'description': f'[MCP:{server}] {description or name}',
            'parameters': {'type': 'object', 'properties': {}},
        },
    }


def _row(server, name, *, description='', meta=None, version='v1'):
    definition = _definition(server, name, description)
    return {
        'server_id': server, 'tool_name': name,
        'namespaced_name': f'mcp__{server}__{name}',
        'openai_def': definition, 'meta': meta or {},
        'schema_hash': canonical_schema_hash(
            definition['function']['parameters']),
        'catalog_version': version,
    }


def _names(definitions):
    return [tool['function']['name'] for tool in definitions]


def _large_snapshot():
    rows = [
        _row('xuecheng', 'prepare_doc_edit', description='prepare document edit',
             meta={'bundle': 'edit_document', 'intents': ['编辑学城文档'],
                   'aliases': ['学城', 'Xuecheng'], 'risk': 'read'}),
        _row('xuecheng', 'update_doc', description='update edit document',
             meta={'bundle': 'edit_document',
                   'requires': ['prepare_doc_edit'],
                   'intents': ['编辑学城文档'], 'risk': 'write'}),
        _row('hope', 'search', description='search hope courses',
             meta={'aliases': ['hope'], 'risk': 'read'}),
        _row('hope', 'login', description='login hope',
             meta={'risk': 'write'}),
    ]
    rows.extend(_row('hope', f'tool_{i}', description=f'utility {i}',
                     meta={'risk': 'read'}) for i in range(8))
    return rows


def test_catalog_index_is_content_addressed_and_private_meta_never_leaks():
    snapshot = _large_snapshot()
    first = build_catalog_index(snapshot)
    second = build_catalog_index(list(reversed(snapshot)))
    assert first is second
    assert first.fingerprint == second.fingerprint
    assert all('_meta' not in row['openai_def'] and 'meta' not in row['openai_def']
               for row in snapshot)

    invalidate_server_catalog('xuecheng')
    rebuilt = build_catalog_index(snapshot)
    assert rebuilt is not first
    assert rebuilt.fingerprint == first.fingerprint


def test_selection_is_stable_bounded_and_expands_workflow_dependencies():
    snapshot = _large_snapshot()
    first = select_active_mcp_tools(
        snapshot, task_id='mcp-edit-task', query='请编辑学城文档', limit=4)
    second = select_active_mcp_tools(
        snapshot, task_id='mcp-edit-task', query='请编辑学城文档', limit=4)
    assert first == second
    names = _names(first)
    assert 'mcp__xuecheng__update_doc' in names
    assert names.index('mcp__xuecheng__prepare_doc_edit') \
        < names.index('mcp__xuecheng__update_doc')
    # Bundle members consume base slots; hard requirements may exceed them.
    assert len(names) <= 5


def test_dependency_moves_before_owner_when_both_were_already_selected():
    snapshot = [
        _row('xuecheng', 'prepare_doc_edit', description='load editable source',
             meta={'bundle': 'edit_document', 'risk': 'read'}),
        _row('xuecheng', 'update_doc', description='update document',
             meta={'bundle': 'edit_document',
                   'requires': ['prepare_doc_edit'],
                   'intents': ['编辑学城文档'], 'risk': 'write'}),
    ]
    snapshot.extend(
        _row('hope', f'unrelated_{i}', description=f'unrelated tool {i}')
        for i in range(8))
    selected = select_active_mcp_tools(
        snapshot, task_id='mcp-dependency-order-task',
        query='编辑学城文档', limit=4)
    names = _names(selected)
    assert names.index('mcp__xuecheng__prepare_doc_edit') \
        < names.index('mcp__xuecheng__update_doc')


def test_used_tool_survives_intent_change_but_unrelated_tools_can_rotate():
    snapshot = _large_snapshot()
    initial = select_active_mcp_tools(
        snapshot, task_id='mcp-sticky-task', query='hope search', limit=4)
    used = 'mcp__hope__search'
    assert used in _names(initial)
    record_mcp_tool_used('mcp-sticky-task', used)

    changed = select_active_mcp_tools(
        snapshot, task_id='mcp-sticky-task', query='编辑学城文档', limit=4)
    changed_names = _names(changed)
    assert changed_names[0] == used
    assert 'mcp__xuecheng__update_doc' in changed_names
    assert 'mcp__xuecheng__prepare_doc_edit' in changed_names


def test_marking_an_active_tool_used_does_not_reorder_the_wire_schema():
    snapshot = _large_snapshot()
    initial = select_active_mcp_tools(
        snapshot, task_id='mcp-order-task', query='hope utility', limit=4)
    initial_names = _names(initial)
    assert len(initial_names) >= 2
    record_mcp_tool_used('mcp-order-task', initial_names[-1])
    after = select_active_mcp_tools(
        snapshot, task_id='mcp-order-task', query='hope utility', limit=4)
    assert _names(after) == initial_names


def test_ambiguous_prompt_gets_deterministic_four_tool_starter_set():
    snapshot = _large_snapshot()
    selected = select_active_mcp_tools(
        snapshot, task_id='mcp-fallback-task', query='完全不匹配的意图',
        limit=8)
    assert len(selected) == 4
    assert selected == select_active_mcp_tools(
        snapshot, task_id='mcp-fallback-task', query='完全不匹配的意图',
        limit=8)


class _FakeTool:
    def __init__(self, name, *, meta=None):
        self.name = name
        self.description = name
        self.inputSchema = {'type': 'object', 'properties': {}}
        self.meta = meta or {}
        self.annotations = SimpleNamespace(readOnlyHint=True)


def test_bridge_snapshot_uses_server_version_hash_and_refresh_notification():
    bridge = MCPBridge()
    handle = _MCPServerHandle('xuecheng-mcp', {})
    old_tool = _FakeTool('read_doc', meta={'aliases': ['学城']})
    assert bridge._replace_server_catalog(
        'xuecheng-mcp', handle, [old_tool], catalog_version='catalog-1')
    assert not bridge._replace_server_catalog(
        'xuecheng-mcp', handle, [old_tool], catalog_version='catalog-1')
    snapshot = bridge.get_tool_catalog_snapshot()
    assert snapshot[0]['catalog_version'] == 'catalog-1'
    assert snapshot[0]['schema_hash'].startswith('sha256:')
    assert snapshot[0]['meta']['aliases'] == ['学城']
    assert 'meta' not in snapshot[0]['openai_def']

    new_tool = _FakeTool('update_doc', meta={
        'requires': ['read_doc'], 'risk': 'write'})

    class _Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[old_tool, new_tool],
                                   catalogVersion='catalog-2')

    handle.session = _Session()
    handle.tools_list_changed = True
    asyncio.run(bridge._handle_server_message(
        handle, {'method': 'notifications/tools/list_changed'}))
    refreshed = bridge.get_tool_catalog_snapshot()
    assert {row['tool_name'] for row in refreshed} == {
        'read_doc', 'update_doc'}
    assert {row['catalog_version'] for row in refreshed} == {'catalog-2'}

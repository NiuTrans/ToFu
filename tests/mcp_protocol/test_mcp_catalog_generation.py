"""The MCP catalog is frozen per exact catalog generation, not copied per round."""
import contextlib
import copy
import json
import sys
from types import SimpleNamespace

import pytest

from lib.mcp import tool_search
from lib.tools.registry import _build

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_caches():
    tool_search.clear_tool_search_caches()
    yield
    tool_search.clear_tool_search_caches()


def _tool(name, description=''):
    return SimpleNamespace(
        name=name, description=description or f'{name} records',
        inputSchema={'type': 'object', 'required': ['q'],
                     'properties': {'q': {'type': 'string', 'description': 'query'},
                                    'limit': {'type': 'integer', 'minimum': 1}}},
        annotations=None, meta=None)


def _connected_bridge(monkeypatch, catalogs):
    import lib.mcp
    from lib.mcp.client._bridge import MCPBridge, _MCPServerHandle
    bridge = MCPBridge(owner_user_id=7)
    bridge._started = True
    for server, tools in catalogs.items():
        handle = _MCPServerHandle(server, {})
        handle.session = object()
        bridge._servers[server] = handle
        assert bridge._replace_server_catalog(server, handle, tools)
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: bridge)
    return bridge


def _containers(value, found=None):
    """Identity of every dict/list reachable from *value*."""
    found = set() if found is None else found
    if isinstance(value, dict):
        found.add(id(value))
        for item in value.values():
            _containers(item, found)
    elif isinstance(value, (list, tuple)):
        found.add(id(value))
        for item in value:
            _containers(item, found)
    return found


@contextlib.contextmanager
def _catalog_deep_copies(bridge):
    """Record every top-level ``copy.deepcopy`` over the bridge's own definitions.

    A profiler sees the call however ``deepcopy`` was imported.
    """
    calls = []
    code = copy.deepcopy.__code__

    def profile(frame, event, arg):
        if event != 'call' or frame.f_code is not code or frame.f_locals.get('memo') is not None:
            return
        internal = {id(info['openai_def']) for info in bridge._tool_index.values()}
        value = frame.f_locals.get('x')
        items = value if isinstance(value, (list, tuple)) else [value]
        if any(id(item) in internal for item in items):
            calls.append(len(items))

    sys.setprofile(profile)
    try:
        yield calls
    finally:
        sys.setprofile(None)


def _root_task():
    cfg = {'userId': 7, 'model': 'test', 'mcpToolExposure': 'auto',
           'memoryEnabled': False, 'schedulerEnabled': False,
           'preferencesEnabled': False, 'plugins': []}
    task = {'id': 'root-generation', 'convId': 'durable', '_userId': 7,
            'messages': [{'role': 'user', 'content': 'read the docs'}], 'config': cfg}
    mcfg = dict(project_path='', project_enabled=False, search_mode='off',
                search_enabled=False, fetch_enabled=False, code_exec_enabled=False,
                browser_enabled=False, desktop_enabled=False, image_gen_enabled=False)
    return task, cfg, mcfg


def _mcp_catalog(task):
    return [tool for tool in task['_executable_tool_catalog']
            if tool['function']['name'].startswith('mcp__')]


def test_catalog_generation_advances_exactly_when_the_visible_catalog_changes(monkeypatch):
    bridge = _connected_bridge(monkeypatch, {
        'docs': [_tool('read'), _tool('search')], 'mail': [_tool('send')]})
    generation, frozen = bridge.get_frozen_openai_tool_defs()
    assert bridge.get_frozen_openai_tool_defs() == (generation, frozen)
    assert bridge.get_frozen_openai_tool_defs()[1] is frozen

    handle = bridge._servers['docs']
    # A reconnect that rediscovers the same catalog is not a change.
    assert not bridge._replace_server_catalog('docs', handle, [_tool('search'), _tool('read')])
    assert bridge.get_frozen_openai_tool_defs()[0] == generation
    # A changed tool list is.
    assert bridge._replace_server_catalog('docs', handle, [_tool('read'), _tool('search', 'v2')])
    changed, frozen_changed = bridge.get_frozen_openai_tool_defs()
    assert changed > generation
    assert [tool['function']['description'] for tool in frozen_changed
            if tool['function']['name'] == 'mcp__docs__search'] == ['[MCP:docs] v2']
    # A grant change is; repeating the same grant is not.
    bridge.set_disabled_tools('docs', ['read'])
    granted, frozen_granted = bridge.get_frozen_openai_tool_defs()
    assert granted > changed
    assert 'mcp__docs__read' not in {tool['function']['name'] for tool in frozen_granted}
    bridge.set_disabled_tools('docs', ['read'])
    assert bridge.get_frozen_openai_tool_defs()[0] == granted
    # A disconnect that removes tools is.
    monkeypatch.setattr(bridge, '_run_async_with_timeout',
                        lambda coroutine, timeout: coroutine.close())
    assert bridge._disconnect_one('mail')
    disconnected, frozen_disconnected = bridge.get_frozen_openai_tool_defs()
    assert disconnected > granted
    assert {tool['function']['name'] for tool in frozen_disconnected} == {'mcp__docs__search'}


def test_assembled_catalog_equals_a_fresh_copy_and_never_aliases_bridge_state(monkeypatch):
    bridge = _connected_bridge(monkeypatch, {
        'docs': [_tool('read'), _tool('search')], 'mail': [_tool('send')]})
    ctx = SimpleNamespace(cfg={'mcpToolExposure': 'inline'}, tid='t', task_id='t',
                          conv_id='', owner_user_id=7, messages=[])
    wire = _build._build_mcp(ctx)
    expected = copy.deepcopy(bridge.get_openai_tool_defs())
    assert ctx.cfg['_mcpAllowedToolCatalog'] == expected
    assert wire == expected
    internal = set().union(*(_containers(info['openai_def'])
                             for info in bridge._tool_index.values()))
    assert not _containers(ctx.cfg['_mcpAllowedToolCatalog']) & internal


def test_unchanged_mcp_round_performs_no_catalog_deep_copy(monkeypatch):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import (
        assemble_round_tools, refresh_root_mcp_authority)
    bridge = _connected_bridge(monkeypatch, {
        'docs': [_tool('read'), _tool('search')], 'mail': [_tool('send')]})
    task, cfg, mcfg = _root_task()
    # The first assembly also freezes the conversation's wire declarations.
    assemble_round_tools(cfg, task, mcfg)
    baseline = json.dumps(_mcp_catalog(task))
    assert {tool['function']['name'] for tool in _mcp_catalog(task)} == {
        'mcp__docs__read', 'mcp__docs__search', 'mcp__mail__send'}
    pristine = copy.deepcopy(bridge.get_frozen_openai_tool_defs()[1])

    for _ in range(3):
        with _catalog_deep_copies(bridge) as copies:
            refresh_root_mcp_authority(task)
        assert copies == []
        assert json.dumps(_mcp_catalog(task)) == baseline
    assert list(bridge.get_frozen_openai_tool_defs()[1]) == list(pristine)

    bridge.set_disabled_tools('docs', ['search'])
    with _catalog_deep_copies(bridge) as copies:
        refresh_root_mcp_authority(task)
    assert len(copies) == 1
    assert {tool['function']['name'] for tool in _mcp_catalog(task)} == {
        'mcp__docs__read', 'mcp__mail__send'}

    bridge.set_disabled_tools('docs', [])
    with _catalog_deep_copies(bridge) as copies:
        refresh_root_mcp_authority(task)
        refresh_root_mcp_authority(task)
    assert len(copies) == 1
    assert json.dumps(_mcp_catalog(task)) == baseline

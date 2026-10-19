"""MCP delegate exposure mode: zero schemas on the orchestrator's wire.

``mcpToolExposure='delegate'`` keeps every MCP schema out of the main
agent's context while the full allowed catalog stays the execution
authority — reachable through execute_tools/search_tools and inherited by
spawn_agents sub-agents (whose ``all_tools`` is the parent executable
catalog, not the wire subset). The mode writes no selection freeze, so an
explicit mid-conversation switch back to 'auto' is never poisoned.
"""
from types import SimpleNamespace

import pytest

from lib.mcp.tool_search import (
    frozen_wire_tool_names,
    mcp_selection_scope_id,
    select_active_mcp_tools,
)

pytestmark = pytest.mark.unit


def _names(tools):
    return [str((t.get('function') or {}).get('name') or '') for t in tools]


def _snapshot(count=5):
    return [
        {
            'openai_def': {'type': 'function', 'function': {
                'name': f'mcp__docs__tool{i}',
                'description': f'docs tool {i}',
                'parameters': {'type': 'object', 'properties': {}}}},
            'namespaced_name': f'mcp__docs__tool{i}',
            'meta': {},
        }
        for i in range(count)
    ]


def _bridge(snapshot):
    class _Bridge:
        connected = True
        server_count = 1

        @staticmethod
        def get_openai_tool_defs():
            return [row['openai_def'] for row in snapshot]

        @staticmethod
        def get_tool_catalog_snapshot():
            return snapshot

    return _Bridge()


def _ctx(cfg_extra, conv_id='delegate-conv', task_id='delegate-task'):
    cfg = {'mcpEnabled': True, 'mcpActiveToolLimit': 3}
    cfg.update(cfg_extra)
    return SimpleNamespace(
        cfg=cfg, tid=task_id, task_id=task_id,
        messages=[{'role': 'user', 'content': 'do docs work'}],
        conv_id=conv_id, owner_user_id=7), cfg


def test_delegate_mode_returns_empty_wire_retains_authority(monkeypatch):
    import lib.mcp as mcp_module
    from lib.tools.registry import _build

    snapshot = _snapshot()
    monkeypatch.setattr(mcp_module, 'get_bridge', lambda owner: _bridge(snapshot))
    ctx, cfg = _ctx({'mcpToolExposure': 'delegate'})

    wire = _build._build_mcp(ctx)

    assert wire == []
    # Authority upper bound retained: execute_tools and sub-agent inheritance
    # still see the complete catalog.
    assert _names(cfg['_mcpAllowedToolCatalog']) == [
        f'mcp__docs__tool{i}' for i in range(5)]
    assert cfg['_mcpActiveToolNames'] == []


def test_delegate_mode_writes_no_selection_freeze(monkeypatch):
    import lib.mcp as mcp_module
    from lib.tools.registry import _build

    snapshot = _snapshot()
    monkeypatch.setattr(mcp_module, 'get_bridge', lambda owner: _bridge(snapshot))
    ctx, _cfg = _ctx({'mcpToolExposure': 'delegate'})
    _build._build_mcp(ctx)

    scope = mcp_selection_scope_id(
        task_id='delegate-task', conv_id='delegate-conv', owner_user_id=7)
    _names_frozen, frozen = frozen_wire_tool_names(scope)
    assert not frozen, 'delegate mode must not write freeze state'

    # …so a later explicit switch to auto still gets a real selection.
    selected = select_active_mcp_tools(
        snapshot, task_id='delegate-task', selection_scope_id=scope,
        query='docs tool', limit=3)
    assert 0 < len(selected) <= 3


def test_delegate_delta_advertises_catalog_and_delegation(monkeypatch):
    """Without any freeze, delegate mode renders every connected tool as a
    name/hint row plus explicit spawn_agents delegation guidance."""
    import lib.mcp as mcp_module
    from lib.tasks_pkg.context_composer import ComposeRequest
    from lib.tasks_pkg.context_composer import _providers as providers

    snapshot = _snapshot()
    monkeypatch.setattr(mcp_module, 'get_bridge', lambda owner: _bridge(snapshot))
    providers._reset_tail_transitions_for_tests()

    task = {'id': 'delegate-delta-task',
            'config': {'mcpToolExposure': 'delegate'}}
    blocks = providers.collect_context_blocks(
        [{'role': 'user', 'content': 'request'}],
        ComposeRequest(
            conv_id='delegate-delta-conv', user_id=7, task=task,
            has_real_tools=True,
            tool_names=frozenset(
                {'search_tools', 'execute_tools', 'spawn_agents'})),
    )
    block = next(b for b in blocks if b.id == 'mcp_tools_delta')
    assert '<available_mcp_tools>' in block.content
    for i in range(5):
        assert f'mcp__docs__tool{i}' in block.content
    assert 'spawn_agents' in block.content
    assert 'delegate exposure mode' in block.content
    assert 'input_schema' not in block.content


def test_delegate_delta_stays_silent_without_tools(monkeypatch):
    """Delegate mode with a disconnected/empty bridge renders no block."""
    import lib.mcp as mcp_module
    from lib.tasks_pkg.context_composer import ComposeRequest
    from lib.tasks_pkg.context_composer import _providers as providers

    class _EmptyBridge:
        connected = True

        @staticmethod
        def get_openai_tool_defs():
            return []

    monkeypatch.setattr(mcp_module, 'get_bridge', lambda owner: _EmptyBridge())
    providers._reset_tail_transitions_for_tests()

    blocks = providers.collect_context_blocks(
        [{'role': 'user', 'content': 'request'}],
        ComposeRequest(
            conv_id='delegate-empty-conv', user_id=7,
            task={'id': 'delegate-empty-task',
                  'config': {'mcpToolExposure': 'delegate'}},
            has_real_tools=True,
            tool_names=frozenset({'search_tools', 'execute_tools'})),
    )
    block = next(b for b in blocks if b.id == 'mcp_tools_delta')
    assert not block.content


def test_auto_mode_unaffected_by_delegate_branch(monkeypatch):
    import lib.mcp as mcp_module
    from lib.tools.registry import _build

    snapshot = _snapshot()
    monkeypatch.setattr(mcp_module, 'get_bridge', lambda owner: _bridge(snapshot))
    ctx, cfg = _ctx({}, conv_id='auto-conv', task_id='auto-task')
    wire = _build._build_mcp(ctx)
    assert 0 < len(wire) <= 3
    assert len(cfg['_mcpAllowedToolCatalog']) == 5


def test_agent_options_accepts_delegate_exposure():
    from lib.agent_loop.agent_options import TofuOptions
    opts = TofuOptions.from_cfg({'mcpToolExposure': 'delegate'})
    assert opts.mcp_tool_exposure == 'delegate'
    assert TofuOptions.from_cfg({}).mcp_tool_exposure == 'auto'
    # Unknown values round-trip (no enum coercion at this layer) and degrade
    # to the auto preselection inside _build_mcp's exposure dispatch.
    assert TofuOptions.from_cfg(
        {'mcpToolExposure': 'not-a-mode'}).mcp_tool_exposure == 'not-a-mode'
    schema = TofuOptions.openapi_schema()
    assert 'delegate' in schema['properties']['mcpToolExposure']['enum']


def test_conv_config_round_trips_delegate_exposure():
    from lib.conv_config._resolve import resolve_conv_config
    cfg = resolve_conv_config(
        {'mcpToolExposure': 'delegate'}, {}, is_active=True)
    assert cfg['mcpToolExposure'] == 'delegate'
    cfg = resolve_conv_config({}, {}, is_active=True)
    assert cfg['mcpToolExposure'] == 'auto'


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))

"""Actual MCP handlers retain effect uncertainty in common executor settlement."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.result_envelope import tool_result_error
from tests.mcp_protocol.test_mcp_call_health import _make_bridge_with_server
from tests.mcp_protocol.test_mcp_recovery_classification import protocol_error
from tests.tasks_pkg.test_tool_outcome_finality import observations, task_for

pytestmark = pytest.mark.unit


def assembled_mcp_documents(bridge, underlying, *, progressive=False, exposure=None):
    """Use production assembly, including its live MCP declaration metadata."""
    from lib.tasks_pkg.model_config import _assemble_tool_list

    bridge._started = True
    bridge._tool_index[underlying]['openai_def'] = {
        'type': 'function', 'function': {
            'name': underlying, 'description': 'Exercise one declared MCP operation.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    }
    cfg = {
        'userId': 1, 'model': 'test-model', 'plugins': [],
        'memoryEnabled': False, 'schedulerEnabled': False, 'preferencesEnabled': False,
        '_storageFreeRuntime': True,
        'mcpToolExposure': exposure or ('progressive' if progressive else 'inline'),
    }
    _assemble_tool_list(
        cfg, '', False, 'mcp-recovery-assembly', 'off', False,
        False, False, False, False, messages=[], conv_id='')
    documents = cfg['_toolContractDocumentsByName']
    assert underlying in documents, 'fixture did not assemble the live MCP declaration'
    return documents


@pytest.mark.parametrize('read_only', [True, False, None])
@pytest.mark.parametrize('exposure', ['inline', 'progressive', 'delegate'])
def test_live_mcp_contract_preserves_read_and_write_recovery(
        read_only, exposure, monkeypatch):
    bridge, name = _make_bridge_with_server()
    bridge._tool_index[name]['read_only_hint'] = read_only
    monkeypatch.setattr('lib.mcp.get_bridge', lambda _owner: bridge)
    document = assembled_mcp_documents(bridge, name, exposure=exposure)[name]
    assert document['permission'] == ('read' if read_only is True else 'write')
    assert document['idempotency'] == (
        'read_only' if read_only is True else 'non_idempotent')
    assert document['effectRecovery'] == (
        'read_only' if read_only is True else 'reconcile')


@pytest.mark.parametrize('metadata', ['missing', 'truthy', 'schema_mismatch', 'duplicate'])
def test_unproven_live_metadata_never_compiles_read_recovery(metadata, monkeypatch):
    from copy import deepcopy

    bridge, name = _make_bridge_with_server()
    bridge._tool_index[name]['read_only_hint'] = True
    monkeypatch.setattr('lib.mcp.get_bridge', lambda _owner: bridge)
    original = bridge.get_tool_catalog_snapshot

    def snapshot():
        rows = deepcopy(original())
        if metadata == 'missing':
            rows[0].pop('read_only_hint')
        elif metadata == 'truthy':
            rows[0]['read_only_hint'] = 'true'
        elif metadata == 'schema_mismatch':
            rows[0]['openai_def']['function']['parameters'] = {
                'type': 'object', 'properties': {'other': {'type': 'string'}}}
        else:
            rows.append(deepcopy(rows[0]))
        return 'test-metadata-generation', rows

    monkeypatch.setattr(bridge, 'get_tool_catalog_projection', snapshot)
    document = assembled_mcp_documents(bridge, name)[name]
    assert document['permission'] == 'write'
    assert document['effectRecovery'] == 'reconcile'


@pytest.mark.parametrize('unavailable', ['disabled', 'disconnected', 'metadata_failure'])
def test_reused_assembly_clears_mcp_read_recovery(unavailable, monkeypatch):
    from lib.tools.registry._build import _build_mcp

    bridge, name = _make_bridge_with_server()
    bridge._tool_index[name]['read_only_hint'] = True
    monkeypatch.setattr('lib.mcp.get_bridge', lambda _owner: bridge)
    assembled_mcp_documents(bridge, name)
    ctx = SimpleNamespace(
        cfg={'mcpToolExposure': 'inline', '_storageFreeRuntime': True},
        owner_user_id=1, tid='reused-mcp-assembly', task_id='', conv_id='')
    assert _build_mcp(ctx)
    assert ctx.cfg['_mcpToolReadOnlyByName'] == {name: True}

    if unavailable == 'disabled':
        ctx.cfg['mcpEnabled'] = False
    elif unavailable == 'disconnected':
        bridge._started = False
    else:
        def fail():
            raise OSError('catalog metadata unavailable')
        monkeypatch.setattr(bridge, 'get_tool_catalog_search_text_projection', fail)

    assert _build_mcp(ctx) == []
    assert ctx.cfg['_mcpToolReadOnlyByName'] == {}
    assert ctx.cfg['_mcpAllowedToolCatalog'] == []


@pytest.mark.parametrize('progressive', [False, True])
@pytest.mark.parametrize('failure', ['lost_write_reply', 'read_timeout'])
def test_mcp_transport_failure_reaches_common_effect_settlement(
        failure, progressive, observations, monkeypatch):
    from lib.tasks_pkg.handlers.mcp import handle_mcp_tool, handle_progressive_mcp_tool

    bridge, underlying = _make_bridge_with_server()
    read = failure == 'read_timeout'
    bridge._tool_index[underlying].update(
        read_only_hint=read, schema_hash='original-declaration')
    calls, reconnects = [], []

    def fail(coro, timeout=None, **kwargs):
        coro.close()
        calls.append(underlying)
        if read:
            raise protocol_error(-32001, "Request 'tools/call' timed out")
        raise ConnectionResetError('reply lost after remote publication')

    def reconnect(server):
        reconnects.append(server)
        return bridge._servers[server]

    monkeypatch.setattr(bridge, '_run_async', fail)
    monkeypatch.setattr(bridge, '_reconnect_server', reconnect)
    monkeypatch.setattr('lib.mcp.get_bridge', lambda _owner: bridge)
    name = ('call_mcp_read_tool' if read else 'call_mcp_write_tool') if progressive else underlying
    arguments = {'name': underlying, 'arguments': {}} if progressive else {}
    task = task_for(
        handle_progressive_mcp_tool if progressive else handle_mcp_tool,
        name=name, permission='read' if read else 'write')
    task['_toolContractDocumentsByName'] = assembled_mcp_documents(
        bridge, underlying, progressive=progressive)
    _, row, _ = _build_tool_round_entry(
        name, arguments, 'owned-call', json.dumps(arguments), 1, False)
    task['toolRounds'] = [row]
    call = {'id': 'owned-call', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)}}
    messages, captured = [], []
    execute_tool_pipeline(
        task, [(call, name, 'owned-call', arguments, 1, row, None)],
        cfg={'autoApply': True}, project_path=None, project_enabled=False,
        tool_list=[], messages=messages, all_search_results_text=[],
        round_num=0, model='test-model',
        internal_result_sink=SimpleNamespace(
            capture=lambda call_id, value: captured.append(value)))

    error = tool_result_error(messages[0]['content'])
    assert error is not None, 'MCP adapter erased operational failure metadata'
    assert error.code == ('tool_execution_timeout' if read else 'tool_effect_unknown')
    assert error.retryable is read
    assert calls == [underlying] and reconnects == []
    assert row['executionEffect']['state'] == 'unknown'
    if not read:
        assert row['executionEffect']['operationId'] in error.message
        assert 'Do not repeat' in error.next_action
    assert row['status'] == 'error' and captured[0].execution_status == 'error'
    assert [event['outcome'] for event in observations[1]] == ['error']
    assert not task.get('_activeToolContexts')

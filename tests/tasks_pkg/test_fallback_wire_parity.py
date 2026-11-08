"""Configured fallback and switch-back match an ordinary destination request."""
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

import lib.tasks_pkg.llm_fallback._requests as requests
from lib.llm.cache_affinity import cache_affinity_key
from lib.llm.tool_wire import project_tool_wire, provider_tool_name
import lib.context_telemetry as context_telemetry
import lib.tasks_pkg.orchestrator._round_request_prep as round_request_prep
from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
from lib.tasks_pkg.orchestrator._round_request_prep import build_round_request

pytestmark = pytest.mark.unit


def _schema(name):
    return {'type': 'function', 'function': {
        'name': name, 'description': f'{name} tool.',
        'parameters': {'type': 'object', 'properties': {}}}}


def _names(tools):
    return [provider_tool_name(tool) for tool in tools or ()]


def _request(task, model, messages, tools, *, round_num=3):
    round_state = SimpleNamespace(model=model, preset=model, thinking_enabled=True)
    _tools, body = build_round_request(
        task, round_state, messages, tools, round_num=round_num,
        tid=task['id'][:8], thinking_depth='max', temperature=1.0,
        max_tokens=4096, response_format=None)
    return body


def _wire(body):
    projected = deepcopy(body)
    project_tool_wire(projected, api_protocol='openai')
    return projected['tools']


def test_fallback_preserves_destination_wire_and_request_identity(monkeypatch):
    tools = [_schema(name) for name in (
        'read_files', 'search_files', 'find_files', 'edit_file', 'run_command',
        'poll', 'kill', *('deferred_' + str(index) for index in range(14)))]
    task = {
        'id': 'fallback-parity-task', 'convId': 'fallback-parity-conv',
        '_userId': 7, '_turnId': 'turn-7', '_attemptId': 'attempt-7',
        'config': {'tools': {'toolSearch': 'local'}},
        '_executable_tool_catalog': deepcopy(tools),
        '_toolDiscoveryPolicyByName': {
            tool['function']['name']: ('eager' if index < 7 else 'searchable')
            for index, tool in enumerate(tools)},
        '_toolSearchCatalogSize': len(tools), '_toolSearchableCount': 14,
        'events': [], 'events_lock': threading.Lock(),
    }
    monkeypatch.setattr(round_request_prep, 'emit_messages_snapshot_event',
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(context_telemetry, 'capture_round_context',
                        lambda *args, **kwargs: None)
    messages = [{'role': 'user', 'content': 'Continue the investigation.'}]
    source = _request(task, 'claude-opus-5.5', messages, tools)
    source['_thinking_depth'] = 'max'
    source.update({'_force_local_tool_search': True,
                   '_force_local_programmatic': True,
                   '_subscription_404_absorbed': 1})
    authority = deepcopy(task['_executable_tool_catalog'])
    ordinary = _request(task, 'kimi-k3.1-pro', messages, tools)
    ordinary['_thinking_depth'] = 'max'
    monkeypatch.setattr(
        'lib.tasks_pkg.context_composer.turn_requirements.refresh_turn_requirements',
        lambda *args, **kwargs: None)

    fallback, _ = requests.prepare_fallback_body(
        task, source, 'claude-opus-5.5', 'kimi-k3.1-pro', 4096, tools,
        messages, round_num=3)

    assert json.dumps(_wire(fallback)) == json.dumps(_wire(ordinary))
    assert fallback['_conv_id'] == ordinary['_conv_id']
    assert cache_affinity_key(fallback) == cache_affinity_key(ordinary)
    assert fallback['_thinking_depth'] == 'max'
    assert fallback['_raw_archive_context'] == {
        **ordinary['_raw_archive_context'], 'model': 'kimi-k3.1-pro'}
    assert fallback['_tool_discovery_policy_by_name'] == ordinary['_tool_discovery_policy_by_name']
    assert not {'_force_local_tool_search', '_force_local_programmatic',
                '_subscription_404_absorbed'} & fallback.keys()
    assert task['_executable_tool_catalog'] == authority



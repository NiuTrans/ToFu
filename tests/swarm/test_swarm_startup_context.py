"""Delegated startup projects schemas before prompt admission, preserving authority."""
from copy import deepcopy

import pytest
from tests.swarm.test_swarm_invocation import (
    runtime as runtime, agent_for, reply, scripted_provider, TOOL,
)
from lib.tools.gateway import tool_schema_tokens

pytestmark = pytest.mark.unit


def final_local_tools(body):
    # The transport fixture captures pre-projection input. Exercise the real
    # shared provider boundary before asserting the schema sent to a model.
    from lib.llm.tool_wire import project_tool_wire
    body = deepcopy(body)
    project_tool_wire(body, api_protocol='openai', base_url='https://fixture.invalid')
    return body['tools']


def test_large_delegated_catalog_starts_without_summarizing_tool_definitions(runtime, monkeypatch):
    tools = [deepcopy(TOOL)] + [{'type': 'function', 'function': {
        'name': f'fixture_deferred_{index}', 'description': 'documentation ' * 2200,
        'parameters': {'type': 'object', 'properties': {}},
    }} for index in range(44)]
    policies = {tool['function']['name']: 'searchable' for tool in tools[1:]}
    runtime.parent['config'].update({
        '_toolDiscoveryPolicyByName': {**policies, 'forbidden_parent_tool': 'eager'},
        '_executableToolNamespaceByName': {name: 'fixture' for name in policies},
        '_requiredDirectToolNames': ['forbidden_parent_tool'],
    })
    original = deepcopy(tools)
    scripted_provider(monkeypatch, runtime, [reply('Verified startup')])
    result = agent_for(runtime, tools=tools).run()
    assert result.status == 'completed', result.error_message
    assert len(runtime.calls) == 1
    child = runtime.children[0]
    visible = final_local_tools(runtime.calls[0])
    names = {tool['function']['name'] for tool in visible}
    assert TOOL['function']['name'] in names
    assert {'search_tools', 'execute_tools'} <= names
    assert not names.intersection(policies)
    assert 'forbidden_parent_tool' not in names
    assert tool_schema_tokens(tools, model='gpt-4o') > 80640
    assert tool_schema_tokens(visible, model='gpt-4o') < 10000
    assert tool_schema_tokens(child['_tool_schema'], model='gpt-4o') < 10000
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names
    assert set(policies) <= _known_tool_names(child)
    assert 'forbidden_parent_tool' not in _known_tool_names(child)
    assert all(child['_executableToolNamespaceByName'][name] == 'fixture' for name in policies)
    assert tools == original


@pytest.mark.parametrize('mode', ['off', 'native', 'local'])
def test_explicit_discovery_policy_and_required_tools_survive_child_startup(runtime, monkeypatch, mode):
    tools = [deepcopy(TOOL)] + [{'type': 'function', 'function': {
        'name': f'fixture_extra_{index}', 'parameters': {'type': 'object'},
    }} for index in range(30)]
    runtime.parent['config'].update({
        'tools': {'toolSearch': mode},
        '_toolDiscoveryPolicyByName': {tool['function']['name']: 'searchable' for tool in tools},
        '_requiredDirectToolNames': [TOOL['function']['name']],
    })
    scripted_provider(monkeypatch, runtime, [reply()])
    result = agent_for(runtime, tools=tools).run()
    assert result.status == 'completed', result.error_message
    child = runtime.children[0]
    assert child['_toolSearchMode'] == mode
    names = {tool['function']['name'] for tool in final_local_tools(runtime.calls[0])}
    assert TOOL['function']['name'] in names
    if mode != 'local':
        assert {tool['function']['name'] for tool in runtime.calls[0]['tools']} == {
            tool['function']['name'] for tool in tools}


@pytest.mark.parametrize('archived', [False, True])
def test_parent_initial_visibility_survives_detached_child_startup(runtime, monkeypatch, archived):
    from lib.tasks_pkg.child_invocation import snapshot_conversation_child_policy
    tools = [deepcopy(TOOL)] + [{'type': 'function', 'function': {
        'name': f'mcp__fixture__hidden_{index}', 'description': 'hidden schema ' * 500,
        'parameters': {'type': 'object'},
    }} for index in range(32)]
    runtime.parent['_tool_schema'] = [TOOL]
    runtime.parent['config']['_executableToolCatalog'] = tools
    runtime.parent['config']['tools'] = {'toolSearch': 'auto', 'programmaticCalling': 'off'}
    if archived:
        snapshot = snapshot_conversation_child_policy(runtime.parent, model='fixture-model', tools=tools)
        runtime.parent['config'].update(snapshot['conversation_tool_policy'])
        del runtime.parent['_tool_schema']
    scripted_provider(monkeypatch, runtime, [reply()])
    result = agent_for(runtime, tools=tools).run()
    assert result.status == 'completed', result.error_message
    child = runtime.children[0]
    assert child['_toolSearchMode'] == 'auto'
    assert child['config']['tools']['programmaticCalling'] == 'off'
    names = {tool['function']['name'] for tool in final_local_tools(runtime.calls[0])}
    assert not any(name.startswith('mcp__fixture__hidden_') for name in names)
    assert {'search_tools', 'execute_tools', TOOL['function']['name']} <= names
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names
    assert all(tool['function']['name'] in _known_tool_names(child) for tool in tools)

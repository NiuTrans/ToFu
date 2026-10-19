"""Coordinator restrictions are authority checks, not prompt-only suggestions."""
from copy import deepcopy
from types import SimpleNamespace
import threading

import pytest

from lib.tasks_pkg.coordinator_policy import (
    COORDINATOR_TOOLS, coordinator_call_rejection, coordinator_scope, filter_coordinator_tools,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def coordinator(monkeypatch):
    from lib.conversations import project_coordination
    state = {'projectPath': '/project', 'conversationId': 'coordinator',
             'availability': 'available', 'revision': 1}
    def repository(owner):
        assert owner == 7
        return SimpleNamespace(read=lambda path: deepcopy(state))
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', repository)
    task = {'id': 'coordinator-task', 'convId': 'coordinator', '_userId': 7,
            'config': {'projectPath': '/project'}, 'abort_event': threading.Event()}
    return task, state


def schema(name):
    return {'type': 'function', 'function': {'name': name, 'description': name,
            'parameters': {'type': 'object', 'properties': {}}}}


@pytest.mark.parametrize('name', [
    'read_files', 'grep_search', 'find_files', 'write_file', 'edit_file', 'run_command',
    'browser_open', 'execute_program', 'spawn_agents', 'manage_agents', 'mcp__unsafe',
    'new_future_tool', 'publish_workspace',
])
def test_concrete_work_is_rejected_even_when_unadvertised_or_approved(coordinator, name):
    task, _ = coordinator
    assert 'dispatch-only' in coordinator_call_rejection(task, name, {})
    assert filter_coordinator_tools(task, [schema(name)]) == []
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    task['_customToolHandlers'] = {name: lambda *args: pytest.fail('forbidden effect')}
    result = _execute_tool_one(task, {}, name, 'tc', {}, 0, {}, task['config'], '/project', True)
    assert result[0] == 'tc' and 'dispatch-only' in result[1] and result[2] is False
    from lib.tasks_pkg.tool_dispatch.api import authorize_prepared_tool_call
    parsed = ({}, name, 'tc', {}, 1, {}, None)
    assert authorize_prepared_tool_call(task, parsed, configuration=task['config'],
                                        project_path='/project', round_num=1, model='model-a') == 'denied'


def test_allowed_coordination_and_batch_transport(coordinator):
    task, _ = coordinator
    for name in COORDINATOR_TOOLS - {'execute_tools'}:
        assert coordinator_call_rejection(task, name, {}) is None
    calls = {'calls': [{'name': 'get_project_assignment', 'arguments': {'assignment_id': 'a'}}]}
    assert coordinator_call_rejection(task, 'execute_tools', calls) is None
    calls['calls'].append({'name': 'run_command', 'arguments': {'command': 'echo unsafe'}})
    assert coordinator_call_rejection(task, 'execute_tools', calls)
    assert coordinator_call_rejection(task, 'execute_tools', {'program': 'return 1'})


def test_binding_is_owner_scoped_and_restriction_cannot_drop_mid_turn(coordinator):
    task, binding = coordinator
    assert coordinator_scope(task)
    binding['conversationId'] = ''
    assert coordinator_call_rejection(task, 'run_command', {})
    ordinary = {'id': 'worker-task', 'convId': 'worker', '_userId': 7,
                'config': {'projectPath': '/project'}}
    assert coordinator_call_rejection(ordinary, 'run_command', {}) is None
    assert filter_coordinator_tools(ordinary, [schema('run_command')]) == [schema('run_command')]


def test_unknown_binding_fails_closed_without_human_approval(coordinator, monkeypatch):
    task, _ = coordinator
    from lib.conversations import project_coordination
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: (_ for _ in ()).throw(OSError('storage down')))
    assert 'fail closed' in coordinator_call_rejection(task, 'run_command', {})
    with pytest.raises(OSError):
        filter_coordinator_tools(task, [schema('run_command')])


def test_coordinator_does_not_create_a_filesystem_workspace(coordinator, monkeypatch):
    task, _ = coordinator
    from lib.tasks_pkg import workspace_delivery
    monkeypatch.setattr(workspace_delivery, 'execution_repositories',
                        lambda task: pytest.fail('workspace inspection is not needed'))
    assert workspace_delivery.bind_worker_workspace(task) is None


def test_streaming_prefetch_cannot_bypass_policy(coordinator):
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    task, _ = coordinator
    executor = StreamingToolAccumulator.__new__(StreamingToolAccumulator)
    executor._task = task
    executor._project_path = '/project'
    # Use the concrete prefetch entry point before it reaches file/cache owners.
    result = executor._execute_one('read_files', {'path': 'private.txt'})
    assert 'dispatch-only' in str(result)


def test_catalog_and_gateway_program_mode_stay_restricted(coordinator, monkeypatch):
    task, _ = coordinator
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as assembly
    cfg = task['config']
    task['messages'] = []
    cfg['tools'] = {'programmaticCalling': 'auto'}
    def assemble(*args, **kwargs):
        tools = [schema('get_conversation'), schema('create_conversation'), schema('run_command'),
                 schema('execute_program'), schema('manage_agents')]
        cfg['_executableToolCatalog'] = tools
        cfg['_toolContractDocumentsByName'] = {t['function']['name']: {} for t in tools}
        return tools, True
    monkeypatch.setattr(assembly, '_assemble_tool_list', assemble)
    tools, real = assembly.assemble_round_tools(cfg, task, {
        'tid': task['id'], 'project_enabled': True, 'project_path': '/project',
        'search_mode': 'auto', 'search_enabled': False, 'fetch_enabled': False,
        'code_exec_enabled': True, 'browser_enabled': True, 'desktop_enabled': False,
        'image_gen_enabled': False, 'model': 'model-a'})
    assert real

    assert cfg['tools']['programmaticCalling'] == 'off'
    assert {t['function']['name'] for t in tools} == {'get_conversation', 'create_conversation'}
    assert {t['function']['name'] for t in cfg['_executableToolCatalog']} == {
        'get_conversation', 'create_conversation'}
    assert set(cfg['_toolContractDocumentsByName']) == {'get_conversation', 'create_conversation'}
    assembly.attach_gateway_contract_documents(task, 'auto')
    assert 'execute_program' not in cfg['_toolContractDocumentsByName']


@pytest.mark.parametrize('name,args', [
    ('run_command', {'command': 'echo blocked'}),
    ('mcp__project__write_file', {'path': 'blocked.txt'}),
    ('execute_program', {'code': 'return await tools.invoke("run_command", {})'}),
    ('execute_tools', {'calls': [{'name': 'write_file', 'arguments': {'path': 'blocked.txt'}}]}),
])
def test_real_pipeline_rejects_before_approval_hooks_and_dispatch(coordinator, monkeypatch, name, args):
    from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    task, _ = coordinator
    task.update({key: value for key, value in _mk_task().items() if key not in task})
    task['_attended'] = True
    task['config']['autoApply'] = False
    parsed = _mk_tc('blocked-call', name, 0, args=args)
    task['toolRounds'] = [parsed[5]]
    def forbidden(*args, **kwargs):
        pytest.fail('Coordinator must reject before asking approval or invoking an effect')
    monkeypatch.setattr(pipeline, '_approval_stops_tool_call', forbidden)
    monkeypatch.setattr(pipeline, '_execute_tool_one', forbidden)
    messages = []
    pipeline.execute_tool_pipeline(task, [parsed], task['config'], '/project', True,
                                   [schema(name)], messages, [], 0, 'test-model')
    assert len(messages) == 1 and messages[0]['tool_call_id'] == 'blocked-call'
    assert 'dispatch-only' in messages[0]['content']
    assert parsed[5]['status'] == 'rejected'


@pytest.mark.parametrize('latch', [None, 'task', 'replay'])
@pytest.mark.parametrize('bound', [True, False])
def test_provider_execution_lanes_follow_role_not_settings_or_replay(coordinator, latch, bound):
    from lib.llm.responses_outbound import openai_body_to_responses
    from lib.llm.tool_wire import project_tool_wire
    from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
    from lib.tasks_pkg.tool_orchestration_policy import resolve_tool_orchestration
    task, _ = coordinator
    if not bound:
        task['convId'] = 'ordinary-worker'
    catalog = [schema('get_conversation'), schema('create_conversation')]
    messages = [{'role': 'user', 'content': 'Review independent modules in parallel'}]
    task['config'].update(tools={'programmaticCalling': 'on', 'programmaticExposure': 'code_api'},
                          orchestration={'multiAgent': 'auto'})
    if latch:
        decision = resolve_tool_orchestration(
            requested_programmatic='on', requested_programmatic_exposure='code_api',
            requested_multi_agent='read_only', messages=messages, tools=catalog,
            round_num=1, model='gpt-5.6-sol')
        if latch == 'task':
            task['_toolOrchestrationPromptLatch'] = decision
        else:
            task['config']['_modelReplayToolLatch'] = decision
    for round_num in (1, 2):
        body = {'model': 'gpt-5.6-sol', 'messages': messages, 'tools': deepcopy(catalog),
                '_responses_feature_profile': 'openai'}
        attach_tool_request_metadata(task, body, messages, catalog, model=body['model'],
                                     round_num=round_num, tid=task['id'])
        projection = project_tool_wire(body, api_protocol='responses', responses_profile='openai')
        outbound, _ = openai_body_to_responses(body)
        if bound:
            assert body['_programmatic_tool_calling'] == 'off'
            assert body['_multi_agent_mode'] == 'disabled'
            assert body['_programmatic_exposure'] == 'additive'
            assert projection.multi_agent_backend == projection.ptc_backend == 'off'
            assert 'multi_agent' not in outbound
            assert 'execute_program' not in {tool.get('name') for tool in outbound.get('tools', [])}
            assert task['_ptc_local'] is None
            assert task['_toolOrchestrationPromptLatch']['multiAgent'] == 'disabled'
        else:
            assert body['_programmatic_tool_calling'] == 'on'
            assert projection.multi_agent_backend == 'native_openai'
            assert outbound['multi_agent']['enabled'] is True

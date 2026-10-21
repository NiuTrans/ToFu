"""Coordinator visibility is role-bound, but never widens executable authority."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS
from tests.tasks_pkg.test_coordinator_policy import coordinator as coordinator

pytestmark = pytest.mark.unit


def names(tools):
    return {(tool.get('function') or tool).get('name') for tool in tools or []}


def assemble(task, *, expected_available=True, **config):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools

    cfg = task['config']
    cfg.update(_turnOwnerUserId=task['_userId'], memoryEnabled=False,
               mcpEnabled=False, plugins=[])
    cfg.update(config)
    task.setdefault('messages', [{'role': 'user', 'content': '继续'}])
    tools, available = assemble_round_tools(cfg, task, {
        'tid': task['id'], 'project_enabled': True, 'project_path': '/project',
        'search_mode': 'off', 'search_enabled': False, 'fetch_enabled': False,
        'code_exec_enabled': True, 'browser_enabled': False,
        'desktop_enabled': False, 'image_gen_enabled': False, 'model': 'gpt-5.6-sol',
    })
    assert available is expected_available
    return tools


def wire(task, tools, *, round_num=1, protocol='responses'):
    from lib.llm.tool_wire import project_tool_wire
    from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata

    body = {'model': 'gpt-5.6-sol', 'messages': task['messages'], 'tools': deepcopy(tools)}
    attach_tool_request_metadata(task, body, task['messages'], tools,
                                 model=body['model'], round_num=round_num, tid=task['id'])
    project_tool_wire(body, api_protocol=protocol, responses_profile='openai')
    return body


def test_default_routed_wire_exposes_core_on_new_turns_and_retries(coordinator):
    from lib.context_experiment_flags import normalize_context_experiment_flags
    from lib.llm.responses_outbound import openai_body_to_responses
    from lib.tools.routing import _reset_sticky_exposure_for_tests

    original, _ = coordinator
    expected = COORDINATOR_TOOLS - {'search_tools', 'execute_tools'}
    assert normalize_context_experiment_flags(original['config'])['tools']['nativeExposure'] == 'routed'
    for turn, text in enumerate(('开始', '继续', 'Looks good')):
        # No earlier discovery, routing cache or task policy is required.
        _reset_sticky_exposure_for_tests()
        task = {**original, 'id': f'coordinator-turn-{turn}', 'config': deepcopy(original['config']),
                'messages': [{'role': 'user', 'content': text}]}
        tools = assemble(task)
        assert names(task['_executable_tool_catalog']) == expected
        assert names(tools) == expected
        assert expected <= names(task['_tool_schema'])
        for round_num in (1, 2):
            for protocol in ('responses', 'chat_completions'):
                body = wire(task, tools, round_num=round_num, protocol=protocol)
                assert expected <= names(body['tools'])
                assert names(body['tools']) <= COORDINATOR_TOOLS
                assert body['_programmatic_tool_calling'] == 'off'
                if protocol == 'responses':
                    outbound, _ = openai_body_to_responses(body)
                    functions = {item['name']: item for item in outbound['tools']
                                 if item['type'] == 'function'}
                    assert expected <= functions.keys()
                    assert not any(item.get('defer_loading') for item in functions.values())
                    assert 'multi_agent' not in outbound


@pytest.mark.parametrize('search_mode', ['off', 'local', 'native'])
def test_discovery_budget_and_replayed_visibility_cannot_hide_role_tools(coordinator, search_mode):
    task, _ = coordinator
    tools = assemble(task, tools={'toolSearch': search_mode, 'schemaBudgetTokens': 1},
                     _toolInitialVisibleNames=['get_conversation'])
    expected = names(task['_executable_tool_catalog'])
    assert expected == COORDINATOR_TOOLS - {'search_tools', 'execute_tools'}
    assert expected <= names(task['_tool_schema'])
    assert expected == set(task['_requiredDirectToolNames'])
    assert set(task['_toolDiscoveryPolicyByName'].values()) == {'eager'}
    body = wire(task, tools)
    assert expected <= names(body['tools'])
    assert names(body['tools']) <= COORDINATOR_TOOLS


@pytest.mark.parametrize('selection', [
    {'toolSpecDenylist': ['coordinator', 'project_integration']},
    {'toolSpecAllowlist': ['read_files', 'conv_ref']},
])
def test_explicit_spec_selection_remains_the_capability_ceiling(coordinator, selection):
    from lib.tools.registry import all_specs

    task, _ = coordinator
    tools = assemble(task, **selection)
    absent = set().union(*(spec.provides for spec in all_specs()
                          if spec.key in {'coordinator', 'project_integration'}))
    body = wire(task, tools)
    assert {'list_conversations', 'get_conversation'} <= names(body['tools'])
    for surface in (tools, task['_executable_tool_catalog'], body['tools']):
        assert not names(surface) & absent
    assert not set(task['_toolContractDocumentsByName']) & absent
    assert not set(task['_requiredDirectToolNames']) & absent


@pytest.mark.parametrize('only_coordinator', [False, True])
def test_plan_mode_filters_hidden_catalog_before_role_promotion(coordinator, only_coordinator):
    task, _ = coordinator
    config = {'planMode': True}
    if only_coordinator:
        # The router initially exposes zero tools from this family. Plan Mode
        # must still filter its executable catalog before role visibility wins.
        config['toolSpecAllowlist'] = ['coordinator']
    tools = assemble(task, **config)
    body = wire(task, tools)
    forbidden = {'create_conversation', 'dispatch_project_assignment', 'delegate_project_work',
                 'review_project_work', 'cancel_project_work', 'finish_project_request',
                 'report_to_user', 'send_project_message', 'consider_project_message', 'todo_write'}
    assert {'get_project_assignment', 'get_project_workers', 'await_project_workers'} <= names(body['tools'])
    for surface in (tools, task['_executable_tool_catalog'], body['tools']):
        assert not names(surface) & forbidden
    assert not set(task['_requiredDirectToolNames']) & forbidden


@pytest.mark.parametrize('case', ['ordinary', 'unbound', 'foreign_owner', 'storage_free', 'child'])
def test_ordinary_or_ineligible_invocations_gain_no_coordinator_core(coordinator, monkeypatch, case):
    from lib.conversations import project_coordination
    from lib.tools.registry import all_specs
    from tests.tasks_pkg.test_coordinator_policy import schema

    task, binding = coordinator
    core = next(spec.provides for spec in all_specs() if spec.key == 'coordinator')
    task['config']['coordinator'] = True  # Request text/settings cannot select a role.
    if case == 'ordinary':
        task['convId'] = 'ordinary'
    elif case == 'unbound':
        binding.update(availability='unbound', conversationId='')
    elif case == 'foreign_owner':
        from types import SimpleNamespace
        task['_userId'] = 8
        def repository(owner):
            assert owner == 8
            return SimpleNamespace(read=lambda path: {'availability': 'unbound', 'conversationId': ''})
        monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', repository)
    elif case == 'storage_free':
        task['_transientRuntime'] = True
        task['config']['_storageFreeRuntime'] = True
    else:
        task['_parentTaskId'] = 'parent'

        from lib.tasks_pkg.invocation import InvocationPolicy
        task['_invocationPolicy'] = InvocationPolicy.child('parent')
        task['_invocationTools'] = [schema('get_conversation')]
    tools = assemble(task)
    assert not names(tools) & core
    assert not names(task['_executable_tool_catalog']) & core
    assert not set(task['_requiredDirectToolNames']) & core


def test_binding_loss_does_not_promote_tools_in_a_new_task(coordinator):
    task, binding = coordinator
    tools = assemble(task)
    assert 'delegate_project_work' in names(tools)
    binding.update(availability='unbound', conversationId='')
    fresh = {'id': 'later-task', 'convId': task['convId'], '_userId': 7,
             'config': {'projectPath': '/project'}}
    tools = assemble(fresh)
    assert 'delegate_project_work' not in names(tools)
    assert 'delegate_project_work' not in names(fresh['_executable_tool_catalog'])


def test_schema_guidance_allows_only_scoped_coordinator_evidence(coordinator):
    task, _ = coordinator
    tools = {tool['function']['name']: tool['function'] for tool in assemble(task)}
    for name in ('list_conversations', 'get_conversation'):
        guidance = tools[name]['description']
        assert 'bound project Coordinator' in guidance
        assert 'same-owner, same-project' in guidance
        assert 'current authorized request' in guidance
        assert 'user explicitly' in guidance
        assert 'Otherwise do not browse history proactively' in guidance
    assert 'delegate_project_work' in tools['create_conversation']['description']
    assert 'without a separate per-worker authorization' in tools['create_conversation']['description']

    worker_guidance = tools['get_project_workers']['description']
    assert 'registered Worker execution assignments' in worker_guidance
    assert 'list_conversations with scope=project' in worker_guidance
    assert 'empty conversation shells' in worker_guidance

@pytest.mark.parametrize('failure', ['empty_selection', 'binding_unavailable'])
def test_role_visibility_never_restores_empty_or_failed_authority(coordinator, monkeypatch, failure):
    from lib.conversations import project_coordination

    task, _ = coordinator
    config = {}
    if failure == 'empty_selection':
        config['toolSpecAllowlist'] = []
    else:
        def unavailable(owner):
            raise OSError('binding unavailable')
        monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', unavailable)
    assert not assemble(task, expected_available=False, **config)
    assert not task['_executable_tool_catalog']
    assert not task['_requiredDirectToolNames']

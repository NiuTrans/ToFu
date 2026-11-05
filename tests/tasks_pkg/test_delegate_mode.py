"""Delegate Mode: the root orchestrates; delegated workers keep ordinary authority."""
from copy import deepcopy
import threading
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.delegate_mode import (
    DELEGATE_ROOT_TOOLS,
    DELEGATION_VIEW_KEY,
    delegate_call_rejection,
    delegate_mode_active,
    delegate_mode_enabled,
    delegation_config_view,
    delegation_policy_view,
    delegation_tool_catalog,
)
from tests.goals import test_goal_invocation_integration as goal_fixture
from tests.swarm.test_retained_assignment_authority import authority as authority  # noqa: F401
from tests.swarm.test_retained_assignment_authority import tool
from tests.tasks_pkg.test_coordinator_policy import coordinator as coordinator  # noqa: F401

runtime = goal_fixture.runtime
pytestmark = pytest.mark.unit


def names(tools):
    return {(item.get('function') or item).get('name') for item in tools or []}


@pytest.fixture
def make_root(monkeypatch):
    """Ordinary roots: the project is coordinated by a different conversation."""
    from lib.conversations import project_coordination
    binding = {'projectPath': '/project', 'conversationId': 'another-conversation',
               'availability': 'available', 'revision': 1}
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: SimpleNamespace(read=lambda path: deepcopy(binding)))
    return lambda: {'id': 'delegate-root', 'convId': 'delegate-conversation', '_userId': 7,
                    'config': {'projectPath': '/project'}, 'abort_event': threading.Event()}


@pytest.fixture
def root(make_root):
    return make_root()


def assemble(task, **config):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    cfg = task['config']
    cfg.update(_turnOwnerUserId=task['_userId'], memoryEnabled=False,
               mcpEnabled=False, plugins=[])
    cfg.update(config)
    task.setdefault('messages', [{'role': 'user', 'content': 'Audit every module'}])
    tools, available = assemble_round_tools(cfg, task, {
        'tid': task['id'], 'project_enabled': True, 'project_path': '/project',
        'search_mode': 'off', 'search_enabled': False, 'fetch_enabled': False,
        'code_exec_enabled': True, 'browser_enabled': False,
        'desktop_enabled': False, 'image_gen_enabled': False, 'model': 'gpt-5.6-sol',
    })
    assert available is True
    return tools


def delegating(task, catalog=()):
    """A task whose assembly already narrowed it (dispatch-level tests)."""
    task[DELEGATION_VIEW_KEY] = {
        'task': {'_executable_tool_catalog': list(catalog)}, 'config': {}}
    return task


def test_mode_flag_is_a_strict_boolean():
    assert delegate_mode_enabled({'delegateMode': True})
    for value in (False, None, 'true', 1, 'yes'):
        assert not delegate_mode_enabled({'delegateMode': value})
    assert not delegate_mode_enabled(None)


@pytest.mark.parametrize('owner', [
    {'planMode': True}, {'activeFlow': 'flow-1'}, {'autopilot': True},
    {'autopilotEnabled': True},
])
def test_another_loop_owner_clears_delegate_mode(owner):
    from lib.tasks_pkg.plan_mode import (
        normalize_interaction_mode_conversation_settings,
        normalize_interaction_mode_runtime_config,
    )
    assert normalize_interaction_mode_runtime_config(
        {'delegateMode': True, **owner})['delegateMode'] is False
    settings_owner = {'autopilotEnabled': True} if 'autopilot' in owner else owner
    assert normalize_interaction_mode_conversation_settings(
        {'delegateMode': True, **settings_owner})['delegateMode'] is False


def test_delegate_mode_replaces_direct_image_generation():
    from lib.tasks_pkg.plan_mode import (
        normalize_interaction_mode_conversation_settings,
        normalize_interaction_mode_runtime_config,
    )
    runtime = normalize_interaction_mode_runtime_config(
        {'delegateMode': True, 'imageGenMode': True})
    stored = normalize_interaction_mode_conversation_settings(
        {'delegateMode': True, 'imageGenMode': True})
    assert runtime['delegateMode'] is True and runtime['imageGenMode'] is False
    assert stored['delegateMode'] is True and stored['imageGenMode'] is False


def test_delegate_mode_alone_survives_normalization():
    from lib.tasks_pkg.plan_mode import (
        interaction_mode_generated_turn_identity,
        normalize_interaction_mode_conversation_settings,
        normalize_interaction_mode_runtime_config,
    )
    assert normalize_interaction_mode_runtime_config({'delegateMode': True})['delegateMode']
    assert normalize_interaction_mode_conversation_settings({'delegateMode': True})['delegateMode']
    assert interaction_mode_generated_turn_identity({'delegateMode': True}) == ('assistant', 'reply')


def test_conversation_choice_resolves_and_persists():
    from lib.conv_config import resolve_conv_config, resolve_conv_settings
    assert resolve_conv_config(conv_settings={'delegateMode': True},
                               is_active=False)['delegateMode'] is True
    # The live toolbar choice of the active conversation wins over storage.
    assert resolve_conv_config(conv_settings={'delegateMode': True},
                               overrides={'delegateMode': False},
                               is_active=True)['delegateMode'] is False
    assert resolve_conv_config(conv_settings={'delegateMode': True, 'planMode': True},
                               is_active=False)['delegateMode'] is False
    assert resolve_conv_settings({'delegateMode': True})['delegateMode'] is True
    assert resolve_conv_settings({})['delegateMode'] is False


def test_assembly_narrows_the_root_and_keeps_the_ordinary_view_for_workers(root, make_root):
    standard = make_root()
    assemble(standard)
    tools = assemble(root, delegateMode=True)

    assert delegate_mode_active(root)
    root_names = names(root['_executable_tool_catalog'])
    assert 'spawn_agents' in root_names and root_names <= DELEGATE_ROOT_TOOLS
    assert names(tools) <= DELEGATE_ROOT_TOOLS
    assert names(root['_tool_schema']) <= DELEGATE_ROOT_TOOLS
    assert set(root['_toolContractDocumentsByName']) <= DELEGATE_ROOT_TOOLS

    # Workers receive exactly what an ordinary root would have delegated.
    delegated = delegation_tool_catalog(root)
    assert names(delegated) == names(standard['_executable_tool_catalog'])
    assert {'read_files', 'run_command', 'write_file'} <= names(delegated)
    view = delegation_policy_view(root)
    assert view['_toolInitialVisibleNames'] == standard['_toolInitialVisibleNames']
    assert names(view['config']['_executableToolCatalog']) == names(delegated)
    assert DELEGATION_VIEW_KEY not in view
    assert view['id'] == root['id'] and view['_userId'] == root['_userId']
    # The user's experiment settings are not rewritten for workers.
    assert root['config'].get('tools') == standard['config'].get('tools')


def test_reassembly_recomputes_the_surface_from_the_current_choice(root):
    assemble(root, delegateMode=True)
    assert delegate_mode_active(root)
    tools = assemble(root, delegateMode=False)
    assert not delegate_mode_active(root)
    assert {'read_files', 'run_command'} <= names(root['_executable_tool_catalog'])
    assert 'read_files' in names(tools)


def test_a_catalog_that_cannot_delegate_keeps_the_ordinary_surface(root):
    explicit = [tool('read_files'), tool('write_file')]
    assemble(root, delegateMode=True, _explicitToolSchemas=deepcopy(explicit))
    assert not delegate_mode_active(root)
    assert names(root['_executable_tool_catalog']) == {'read_files', 'write_file'}


def test_coordinator_role_wins_over_delegate_mode(coordinator):  # noqa: F811
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS
    task, _ = coordinator
    assemble(task, delegateMode=True)
    assert not delegate_mode_active(task)
    assert names(task['_executable_tool_catalog']) <= COORDINATOR_TOOLS


@pytest.mark.parametrize('name', [
    'read_files', 'grep_search', 'write_file', 'run_command', 'web_search', 'fetch_url',
    'execute_program', 'mcp__github__get_issue', 'browser_open', 'new_future_tool',
])
def test_concrete_work_is_rejected_on_every_execution_entry(root, name):
    task = delegating(root)
    assert 'Delegate mode' in delegate_call_rejection(task, name, {})
    from lib.tasks_pkg.executor._execute import _execute_tool_one
    task['_customToolHandlers'] = {name: lambda *args: pytest.fail('forbidden effect')}
    result = _execute_tool_one(task, {}, name, 'tc', {}, 0, {}, task['config'], '/project', True)
    assert result[0] == 'tc' and 'Delegate mode' in result[1] and result[2] is False
    from lib.tasks_pkg.tool_dispatch.api import authorize_prepared_tool_call
    assert authorize_prepared_tool_call(
        task, ({}, name, 'tc', {}, 1, {}, None), configuration=task['config'],
        project_path='/project', round_num=1, model='model-a') == 'denied'


def test_orchestration_tools_and_pure_batches_are_allowed(root):
    task = delegating(root)
    for name in DELEGATE_ROOT_TOOLS - {'execute_tools'}:
        assert delegate_call_rejection(task, name, {}) is None
    batch = {'calls': [{'name': 'poll', 'arguments': {}},
                       {'name': 'get_agent_result', 'arguments': {}}]}
    assert delegate_call_rejection(task, 'execute_tools', batch) is None


@pytest.mark.parametrize('arguments', [
    {'calls': [{'name': 'read_files', 'arguments': {}}]},
    {'calls': [{'name': 'poll'}, {'name': 'run_command'}]},
    {'calls': [{'name': 'execute_tools', 'arguments': {}}]},
    {'calls': [], 'program': 'return 1'},
    {'program': 'return await tools.read_files({})'},
    {},
])
def test_batches_that_reach_concrete_work_are_rejected(root, arguments):
    assert 'Delegate mode' in delegate_call_rejection(delegating(root), 'execute_tools', arguments)


def test_tasks_outside_delegate_mode_are_never_restricted(root):
    assert delegate_call_rejection(root, 'read_files', {}) is None
    root[DELEGATION_VIEW_KEY] = {}
    assert delegate_call_rejection(root, 'run_command', {}) is None


def test_real_pipeline_rejects_before_approval_and_dispatch(root, monkeypatch):
    from tests.tools.test_tool_settle_all_lanes import _mk_task, _mk_tc
    from lib.tasks_pkg.tool_dispatch import _pipeline as pipeline
    task = delegating(root)
    task.update({key: value for key, value in _mk_task().items() if key not in task})
    task['_attended'] = True
    task['config']['autoApply'] = False
    parsed = _mk_tc('blocked-call', 'write_file', 0, args={'path': 'a.txt', 'content': 'x'})
    task['toolRounds'] = [parsed[5]]

    def forbidden(*args, **kwargs):
        pytest.fail('Delegate mode must reject before approval or effects')
    monkeypatch.setattr(pipeline, '_approval_stops_tool_call', forbidden)
    monkeypatch.setattr(pipeline, '_execute_tool_one', forbidden)
    messages = []
    pipeline.execute_tool_pipeline(task, [parsed], task['config'], '/project', True,
                                   [tool('write_file')], messages, [], 0, 'test-model')
    assert len(messages) == 1 and messages[0]['tool_call_id'] == 'blocked-call'
    assert 'Delegate mode' in messages[0]['content']
    assert parsed[5]['status'] == 'rejected'


@pytest.mark.parametrize('protocol', ['responses', 'chat_completions'])
def test_requests_keep_local_swarm_and_never_run_programs(root, protocol):
    from lib.llm.responses_outbound import openai_body_to_responses
    from lib.llm.tool_wire import project_tool_wire
    from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
    tools = assemble(root, delegateMode=True,
                     tools={'programmaticCalling': 'on', 'programmaticExposure': 'code_api'},
                     orchestration={'multiAgent': 'auto'})
    for round_num in (1, 2):
        body = {'model': 'gpt-5.6-sol', 'messages': root['messages'], 'tools': deepcopy(tools)}
        attach_tool_request_metadata(root, body, root['messages'], tools, model=body['model'],
                                     round_num=round_num, tid=root['id'])
        project_tool_wire(body, api_protocol=protocol, responses_profile='openai')
        assert body['_programmatic_tool_calling'] == 'off'
        assert body['_multi_agent_mode'] == 'full'
        assert body['_resolved_multi_agent_backend'] == 'local_swarm'
        assert 'spawn_agents' in names(body['tools'])
        assert names(body['tools']) <= DELEGATE_ROOT_TOOLS
        if protocol == 'responses':
            outbound, _ = openai_body_to_responses(body)
            assert 'multi_agent' not in outbound


def test_composer_swaps_parallel_guidance_for_the_delegate_contract(root):
    from lib.tasks_pkg.context_composer._models import ComposeRequest
    from lib.tasks_pkg.context_composer._providers import collect_context_blocks
    tools = assemble(root, delegateMode=True)
    messages = [{'role': 'user', 'content': 'Audit every module'}]
    blocks = {block.id: block for block in collect_context_blocks(messages, ComposeRequest(
        task=root, has_real_tools=True, tool_names=names(tools)))}
    assert '<delegate_mode>' in blocks['delegate_mode'].content
    assert blocks['delegate_mode'].placement == 'system'
    assert not blocks['delegate_mode'].suppressed_reason
    assert blocks['parallel_execution'].content == ''
    assert blocks['parallel_execution'].placement == 'system'
    assert blocks['parallel_execution'].suppressed_reason == 'superseded_by_delegate_mode'
    assert blocks['programmatic_execution'].content == ''

    ordinary = dict(root)
    ordinary.pop(DELEGATION_VIEW_KEY)
    off = {block.id: block for block in collect_context_blocks(messages, ComposeRequest(
        task=ordinary, has_real_tools=True, tool_names=names(tools)))}
    assert off['delegate_mode'].content == ''
    assert off['delegate_mode'].suppressed_reason == 'delegate_mode_off'
    assert off['parallel_execution'].placement == 'system'


def test_worker_capabilities_stay_visible_to_the_delegating_root(root, monkeypatch):
    import lib.mcp as mcp_module
    from lib.tasks_pkg.context_composer import ComposeRequest
    from lib.tasks_pkg.context_composer import _providers as providers
    from tests.mcp_protocol.test_mcp_delegate_exposure import _bridge, _snapshot
    snapshot = _snapshot(3)
    monkeypatch.setattr(mcp_module, 'get_bridge', lambda owner: _bridge(snapshot))
    providers._reset_tail_transitions_for_tests()
    # Workers may hold run_command and one of the connected MCP tools.
    delegating(root, [tool('spawn_agents'), tool('run_command'),
                      tool('mcp__docs__tool1')])
    root['_executable_tool_catalog'] = [tool('spawn_agents')]
    blocks = {block.id: block for block in providers.collect_context_blocks(
        [{'role': 'user', 'content': 'Audit the docs'}],
        ComposeRequest(conv_id='delegate-conversation', user_id=7, task=root,
                       has_real_tools=True, tool_names=frozenset({'spawn_agents'})))}
    assert blocks['credential_vault'].suppressed_reason != 'run_command_not_enabled'
    delta = blocks['mcp_tools_delta'].content
    assert 'mcp__docs__tool1' in delta and 'tools allowlist' in delta
    # Only tools a worker can actually receive are advertised.
    assert 'mcp__docs__tool0' not in delta and 'mcp__docs__tool2' not in delta

    ordinary = dict(root)
    ordinary.pop(DELEGATION_VIEW_KEY)
    blocks = {block.id: block for block in providers.collect_context_blocks(
        [{'role': 'user', 'content': 'Audit the docs'}],
        ComposeRequest(conv_id='delegate-conversation', user_id=7, task=ordinary,
                       has_real_tools=True, tool_names=frozenset({'spawn_agents'})))}
    assert blocks['credential_vault'].suppressed_reason == 'run_command_not_enabled'


def test_delegate_contract_names_the_spawn_features_it_relies_on():
    from lib.swarm.tools import SPAWN_AGENTS_TOOL
    from lib.tasks_pkg.delegate_mode import delegate_mode_prompt_block
    block = delegate_mode_prompt_block()
    item = SPAWN_AGENTS_TOOL['function']['parameters']['properties']['agents']['items']
    for field in ('templates', 'params', 'output_schema'):
        assert f'`{field}`' in block
    for field in ('template', 'params', 'output_schema'):
        assert field in item['properties']
    assert 'templates' in SPAWN_AGENTS_TOOL['function']['parameters']['properties']


def test_spawn_and_retained_handlers_delegate_the_captured_catalog(runtime, monkeypatch):
    from lib.tasks_pkg.handlers.misc import _agents
    from lib.tasks_pkg.handlers import retained_agents
    import lib.swarm.integration as integration
    import lib.swarm.retained as retained
    seen = []
    monkeypatch.setattr(integration, 'execute_swarm_tool',
                        lambda *a, **kw: seen.append(kw['all_tools']) or '{}')
    monkeypatch.setattr(retained, 'execute_manage_agents',
                        lambda arguments, **kw: seen.append(kw['all_tools']) or '{}')
    full = [tool('read_files'), tool('run_command'), tool('spawn_agents')]
    runtime.parent['_executable_tool_catalog'] = [tool('spawn_agents')]
    delegating(runtime.parent, full)
    entry = {'query': 'Spawn agents', 'toolName': 'spawn_agents'}
    _agents._handle_swarm_tool(runtime.parent, {}, 'spawn_agents', 'spawn', {'agents': []}, 1,
                               dict(entry), runtime.parent['config'], '', False,
                               all_tools=[tool('spawn_agents')])
    retained_agents._handle_manage_agents(
        runtime.parent, {}, 'manage_agents', 'manage', {'action': 'list'}, 1,
        dict(entry, toolName='manage_agents'), runtime.parent['config'], '', False,
        all_tools=[tool('spawn_agents')])
    assert [names(catalog) for catalog in seen] == [names(full), names(full)]


def test_worker_assignment_policy_comes_from_the_delegation_view(authority):  # noqa: F811
    from lib.swarm.master import bind_assignment_contexts
    from lib.swarm.protocol import SubTaskSpec
    parent = deepcopy(authority.current)
    parent['_toolInitialVisibleNames'] = ['spawn_agents']
    parent['config']['_toolInitialVisibleNames'] = ['spawn_agents']
    full = [tool('read_files'), tool('write_file')]
    parent[DELEGATION_VIEW_KEY] = {
        'task': {'_executable_tool_catalog': full,
                 '_toolInitialVisibleNames': ['read_files', 'write_file']},
        'config': {'_toolInitialVisibleNames': ['read_files', 'write_file']}}

    def visible(task):
        templates = bind_assignment_contexts(
            [SubTaskSpec(id='worker', role='general', objective='Read the files')],
            parent_task=task, parent_config=task['config'],
            all_tools=delegation_tool_catalog(task), model='current-model',
            project_path='/current-project', thinking_enabled=False, validate_only=True)
        return set(templates['current-model']['config']['_toolInitialVisibleNames'])

    assert visible(parent) == {'read_files', 'write_file'}
    narrowed_only = deepcopy(parent)
    narrowed_only.pop(DELEGATION_VIEW_KEY)
    narrowed_only['_executable_tool_catalog'] = full
    assert visible(narrowed_only) == {'spawn_agents'}


def test_config_view_overrides_only_captured_policy_fields(root):
    delegating(root)
    root[DELEGATION_VIEW_KEY]['config'] = {'_executableToolCatalog': [tool('read_files')]}
    merged = delegation_config_view(root, {'model': 'm', '_executableToolCatalog': []})
    assert merged == {'model': 'm', '_executableToolCatalog': [tool('read_files')]}
    assert delegation_config_view({'config': {}}, {'model': 'm'}) == {'model': 'm'}


def test_terminal_release_drops_the_captured_view():
    from lib.tasks_pkg.manager._persist import _release_terminal_tool_state
    from tests.tasks_pkg.test_terminal_tool_release import task_with_tools
    task = task_with_tools()
    delegating(task, task['_executable_tool_catalog'])
    assert _release_terminal_tool_state(task) > 0
    assert task[DELEGATION_VIEW_KEY] == {}
    assert not delegate_mode_active(task)

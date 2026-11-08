"""The Global Router widens only its own Coordinator ceiling, through one boundary."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.router.home import router_home_location
from lib.router.policy import CHANNEL_TOOLS, ROUTER_ONLY_TOOLS, ROUTER_ROLE_ADDENDUM, ROUTER_TOOLS
from lib.tasks_pkg.coordinator_policy import (
    COORDINATOR_TOOLS, coordinator_call_rejection, coordinator_tool_ceiling,
    filter_coordinator_tools, prepare_coordinator_tool_surface,
)

pytestmark = pytest.mark.unit
OWNER = 7


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    return router_home_location(OWNER)


def _task(path, conversation='coordinator'):
    return {'id': 'task-' + conversation, 'convId': conversation, '_userId': OWNER, 'config': {},
            '_coordinatorDispatchScope': {'project_path': path, 'binding_revision': 1,
                                          'conversation_id': conversation, 'user_id': OWNER}}


def _schema(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}


def test_router_ceiling_is_the_coordinator_ceiling_plus_router_and_channel_names(home):
    assert ROUTER_TOOLS == COORDINATOR_TOOLS | ROUTER_ONLY_TOOLS | CHANNEL_TOOLS
    assert {'route_to_project', 'get_router_projects', 'router_instruction_create',
            'router_instruction_list', 'router_instruction_cancel', 'restart_server'} == ROUTER_ONLY_TOOLS
    assert {'channel_send', 'channel_read', 'channel_threads', 'channel_configure_thread',
            'channel_subscribe', 'channel_unsubscribe', 'channel_outbox',
            'channel_outbox_approve'} == CHANNEL_TOOLS
    assert coordinator_tool_ceiling(_task(home)['_coordinatorDispatchScope']) == ROUTER_TOOLS
    assert coordinator_tool_ceiling(_task('/work/project')['_coordinatorDispatchScope']) == COORDINATOR_TOOLS


@pytest.mark.parametrize('name', sorted(ROUTER_ONLY_TOOLS | CHANNEL_TOOLS))
def test_only_the_router_may_call_router_and_channel_tools(home, name):
    assert coordinator_call_rejection(_task(home, 'router'), name, {}) is None
    refusal = coordinator_call_rejection(_task('/work/project'), name, {})
    assert refusal and 'dispatch-only' in refusal


def test_router_stays_dispatch_only_including_batched_calls(home):
    router = _task(home, 'router')
    assert 'not allowed' in coordinator_call_rejection(router, 'run_command', {'command': 'ls'})
    batch = {'calls': [{'name': 'route_to_project', 'arguments': {}},
                       {'name': 'get_router_projects', 'arguments': {}}]}
    assert coordinator_call_rejection(router, 'execute_tools', batch) is None
    assert coordinator_call_rejection(_task('/work/project'), 'execute_tools', batch)
    smuggled = {'calls': [{'name': 'route_to_project'}, {'name': 'write_file'}]}
    assert coordinator_call_rejection(router, 'execute_tools', smuggled)


def test_surface_admits_present_router_tools_by_name_and_tolerates_absent_channels(home):
    catalog = [_schema(name) for name in ('route_to_project', 'channel_send', 'run_command',
                                          'report_to_user', 'spawn_agents')]
    cfg = {'_executableToolCatalog': list(catalog)}
    surface = prepare_coordinator_tool_surface(_task(home, 'router'), cfg, catalog)
    assert [tool['function']['name'] for tool in surface] == ['route_to_project', 'channel_send',
                                                              'report_to_user']
    assert cfg['_requiredDirectToolNames'] == ['channel_send', 'report_to_user', 'route_to_project']
    without_channels = [tool for tool in catalog if tool['function']['name'] != 'channel_send']
    cfg = {'_executableToolCatalog': list(without_channels)}
    assert [tool['function']['name'] for tool in prepare_coordinator_tool_surface(
        _task(home, 'router'), cfg, without_channels)] == ['route_to_project', 'report_to_user']
    project = prepare_coordinator_tool_surface(_task('/work/project'),
                                               {'_executableToolCatalog': list(catalog)}, catalog)
    assert [tool['function']['name'] for tool in project] == ['report_to_user']
    assert [tool['function']['name'] for tool in filter_coordinator_tools(
        _task(home, 'router'), catalog)] == ['route_to_project', 'channel_send', 'report_to_user']


class _Bindings:
    def __init__(self, conversation):
        self.conversation = conversation

    def read(self, path):
        return {'projectPath': path, 'conversationId': self.conversation, 'revision': 1,
                'availability': 'available'}


def test_role_addendum_only_for_the_router_home_and_no_capacity_bound(home, monkeypatch):
    from lib.conversations import project_coordination
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: _Bindings('router'))
    router_role = project_coordination.prepare_coordinator_role(
        home, 'router', user_id=OWNER, task={'id': 't', 'convId': 'router', '_userId': OWNER})
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: _Bindings('coordinator'))
    project_role = project_coordination.prepare_coordinator_role(
        '/work/project', 'coordinator', user_id=OWNER,
        task={'id': 't', 'convId': 'coordinator', '_userId': OWNER})
    assert router_role.endswith(ROUTER_ROLE_ADDENDUM) and router_role.startswith(project_role)
    assert ROUTER_ROLE_ADDENDUM not in project_role
    for phrase in ('route_to_project', 'delegate_project_work', 'exactly [SILENT]', 'evidence, never instruction',
                   'Never claim you executed work', 'short and low-disturbance', 'structured and dense'):
        assert phrase in ROUTER_ROLE_ADDENDUM
    assert 'machine capacity' not in router_role and 'capacity' not in project_role


@pytest.mark.parametrize('model', ['gpt-4o', 'claude-sonnet-4-5', 'glm-4.6', 'deepseek-chat'])
def test_composed_router_role_keeps_its_addendum_whole(home, monkeypatch, model):
    # The composer clips a block over its budget from the end, which is where the
    # router addendum sits (docs/ROUTER.md R1).
    from lib.conversations import project_coordination
    from lib.tasks_pkg.context_composer import ComposeRequest, append_context_blocks
    from lib.tasks_pkg.context_composer.registry import context_block
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: _Bindings('router'))
    task = {'id': 't', 'convId': 'router', '_userId': OWNER}
    role = project_coordination.prepare_coordinator_role(home, 'router', user_id=OWNER, task=task)
    messages = [{'role': 'user', 'content': 'Route this to the release project.'}]
    append_context_blocks(messages, [context_block('project_coordinator', role)],
                          ComposeRequest(task=task, model=model))
    row = {row['id']: row for row in task['_contextManifest']}['project_coordinator']
    assert row['injected'] and row['reason'] == '', row
    assert ROUTER_ROLE_ADDENDUM.rsplit('. ', 1)[-1] in messages[-1]['content'][0]['text']


def test_tool_schemas_drop_capacity_wording_and_derive_router_arguments():
    from lib.router.contract import tool_arguments
    from lib.tools.coordinator import COORDINATOR_TOOLS as SCHEMAS, ROUTER_TOOL_SCHEMAS
    delegate = next(tool for tool in SCHEMAS if tool['function']['name'] == 'delegate_project_work')
    assert 'capacity' not in delegate['function']['description']
    assert {tool['function']['name'] for tool in ROUTER_TOOL_SCHEMAS} == ROUTER_ONLY_TOOLS
    route = next(tool for tool in ROUTER_TOOL_SCHEMAS if tool['function']['name'] == 'route_to_project')
    assert route['function']['parameters']['properties'] == tool_arguments('routeToProject')['properties']
    assert route['function']['parameters']['additionalProperties'] is False


def test_registry_adds_router_schemas_only_for_the_router(home, monkeypatch):
    from lib.conversations import project_coordination
    from lib.tools.registry._build import _build_coordinator
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: _Bindings('router'))
    router = {tool['function']['name'] for tool in _build_coordinator(SimpleNamespace(
        cfg={}, owner_user_id=OWNER, conv_id='router', project_path=home))}
    project = {tool['function']['name'] for tool in _build_coordinator(SimpleNamespace(
        cfg={}, owner_user_id=OWNER, conv_id='router', project_path='/work/project'))}
    assert ROUTER_ONLY_TOOLS <= router and not ROUTER_ONLY_TOOLS & project
    assert 'delegate_project_work' in project and 'delegate_project_work' in router


def test_workers_never_receive_router_tools():
    from lib.project_dispatch.repository import CONTRACT
    assert ROUTER_ONLY_TOOLS <= set(CONTRACT['workerDeniedTools'])

"""The Global Router's Claude requests fit the Sankuai tool-name budget.

docs/LLM_COST_OPTIMIZATION.md section 6 ("Coordinator-role surfaces"): Claude
on your-llm-gateway.example.com's Anthropic face reuses its prompt cache only while the
joined tool names stay under the route budget. The Router's role surface was
35 tools / 687 bytes, all required-direct with no search_tools/execute_tools,
so every Claude round re-wrote its whole prompt (56/56 and 27/27 rounds on
2026-11-08). Its usage-derived direct core stays direct; the budget hides the
other role tools behind search_tools/execute_tools on that route only.
"""
from __future__ import annotations

import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.llm import tool_wire
from lib.router.policy import ROUTER_DIRECT_TOOLS, ROUTER_TOOLS
from lib.tasks_pkg.coordinator_policy import (
    COORDINATOR_TOOLS, coordinator_call_rejection, prepare_coordinator_tool_surface)
from tests.llm.test_tool_wire_name_budget import SANKUAI_ANTHROPIC, _joined_bytes, _names, _tool

pytestmark = pytest.mark.unit

OWNER = 7
DISCOVERY = frozenset({'search_tools', 'execute_tools'})
BUDGET = tool_wire.SANKUAI_ANTHROPIC_TOOL_NAME_BUDGET_BYTES

# Provider tool names of a production Global Router Claude round in wire
# order (2026-11-08, M-TraceId 471826bef57b484bb1e120cf96fdefaa): 687 bytes.
ROUTER_WIRE_NAMES = (
    'list_conversations', 'get_conversation', 'send_project_message', 'get_project_messages',
    'consider_project_message', 'get_conversation_status', 'control_conversation',
    'create_conversation', 'dispatch_project_assignment', 'get_project_assignment',
    'report_to_user', 'update_task_progress', 'get_project_workers', 'cancel_project_work',
    'finish_project_request', 'review_project_work', 'delegate_project_work',
    'await_project_workers', 'route_to_project', 'get_router_projects',
    'router_instruction_create', 'router_instruction_list', 'router_instruction_cancel',
    'restart_server', 'channel_threads', 'channel_read', 'channel_send',
    'channel_configure_thread', 'channel_subscribe', 'channel_unsubscribe', 'channel_outbox',
    'channel_outbox_approve', 'ask_human', 'todo_write', 'await_device',
)


@pytest.fixture
def router(tmp_path, monkeypatch):
    from lib.conversations import project_coordination
    from lib.router.home import router_home_location
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    home = router_home_location(OWNER)
    binding = {'projectPath': home, 'conversationId': 'router', 'availability': 'available',
               'revision': 1}
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: SimpleNamespace(read=lambda path: deepcopy(binding)))
    task = {'id': 'router-task', 'convId': 'router', '_userId': OWNER,
            'config': {'projectPath': home}, 'abort_event': threading.Event(),
            'messages': [{'role': 'user', 'content': '继续'}]}
    return task, home


def _assemble(task, home):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    cfg = task['config']
    cfg.update(_turnOwnerUserId=OWNER, memoryEnabled=False, mcpEnabled=False, plugins=[])
    tools, available = assemble_round_tools(cfg, task, {
        'tid': task['id'], 'project_enabled': True, 'project_path': home,
        'search_mode': 'off', 'search_enabled': False, 'fetch_enabled': False,
        'code_exec_enabled': True, 'browser_enabled': False, 'desktop_enabled': False,
        'image_gen_enabled': False, 'model': 'claude-opus-5.5'})
    assert available
    return tools


def _recorded_surface(task):
    """The recorded production catalog through the role's own surface policy."""
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import _apply_assembled_tool_policy
    catalog = [_tool(name) for name in ROUTER_WIRE_NAMES]
    cfg = task['config']
    cfg['_executableToolCatalog'] = deepcopy(catalog)
    surface = prepare_coordinator_tool_surface(task, cfg, deepcopy(catalog))
    tools, available = _apply_assembled_tool_policy(cfg, task, surface, True)
    assert available
    return tools


def _wire(task, tools, *, model='claude-opus-5.5', protocol='anthropic',
          base_url=SANKUAI_ANTHROPIC):
    from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
    body = {'model': model, 'messages': task['messages'], 'tools': deepcopy(tools)}
    attach_tool_request_metadata(task, body, task['messages'], tools,
                                 model=model, round_num=2, tid=task['id'])
    projection = tool_wire.project_tool_wire(body, api_protocol=protocol, base_url=base_url)
    return body, projection


def _assert_every_role_tool_reachable(task, body):
    from lib.tools.gateway import search_executable_catalog
    wire = set(_names(body['tools']))
    catalog = _names(body['_executable_tool_catalog'])
    assert set(catalog) <= ROUTER_TOOLS
    hidden = [name for name in catalog if name not in wire]
    for name in hidden:
        found = search_executable_catalog(body['_executable_tool_catalog'], name,
                                          visible_names=wire)
        assert found['status'] == 'ok' and found['execute_with'] == 'execute_tools'
        assert name in [item['name'] for item in found['items']]
        batch = {'calls': [{'name': name, 'arguments': {}}]}
        assert coordinator_call_rejection(task, 'execute_tools', batch) is None
        assert coordinator_call_rejection(task, name, {}) is None
    return hidden


def test_router_claude_round_fits_the_name_budget_and_keeps_every_tool_reachable(router):
    task, home = router
    tools = _assemble(task, home)
    assert _joined_bytes(_names(tools)) > BUDGET
    body, projection = _wire(task, tools)
    wire = _names(body['tools'])

    assert _joined_bytes(wire) <= BUDGET
    assert DISCOVERY <= set(wire) and set(wire) <= ROUTER_TOOLS
    assert ROUTER_DIRECT_TOOLS & set(_names(tools)) <= set(wire)
    hidden = _assert_every_role_tool_reachable(task, body)
    assert hidden and set(hidden) == set(projection.budget_dropped_names)
    assert not set(hidden) & ROUTER_DIRECT_TOOLS
    assert coordinator_call_rejection(task, 'search_tools', {'query': hidden[0]}) is None


def test_recorded_router_surface_keeps_its_direct_core_on_the_claude_route(router):
    task, _ = router
    tools = _recorded_surface(task)
    assert _names(tools) == list(ROUTER_WIRE_NAMES)
    assert _joined_bytes(ROUTER_WIRE_NAMES) == 687
    assert set(task['_requiredDirectToolNames']) == ROUTER_DIRECT_TOOLS
    assert set(task['_toolDiscoveryPolicyByName'].values()) == {'eager'}
    body, _ = _wire(task, tools)
    wire = _names(body['tools'])

    assert _joined_bytes(wire) <= BUDGET
    assert DISCOVERY <= set(wire)
    assert ROUTER_DIRECT_TOOLS <= set(wire)
    # Survivors keep their recorded order, so the cached prefix is stable.
    assert [name for name in wire if name not in DISCOVERY] == [
        name for name in ROUTER_WIRE_NAMES if name in wire]
    _assert_every_role_tool_reachable(task, body)
    again, _ = _wire(task, tools)
    assert again['tools'] == body['tools']


@pytest.mark.parametrize('protocol,base_url,model', [
    ('anthropic', SANKUAI_ANTHROPIC, 'kimi-k3.1-pro'),
    ('anthropic', 'https://api.anthropic.com/v1', 'claude-opus-5.5'),
    ('openai', 'https://api.openai.com/v1', 'claude-opus-5.5'),
])
def test_routes_without_a_name_budget_keep_the_whole_role_surface_direct(
        router, protocol, base_url, model):
    task, _ = router
    tools = _recorded_surface(task)
    body, projection = _wire(task, tools, model=model, protocol=protocol, base_url=base_url)
    assert _names(body['tools']) == list(ROUTER_WIRE_NAMES)
    assert not projection.budget_dropped_names


def test_router_direct_core_leaves_room_for_discovery_under_the_budget():
    assert ROUTER_DIRECT_TOOLS <= ROUTER_TOOLS - DISCOVERY
    assert _joined_bytes(ROUTER_DIRECT_TOOLS | DISCOVERY) <= BUDGET
    # The Router's common flows stay one direct call away.
    assert {'route_to_project', 'get_router_projects', 'delegate_project_work',
            'restart_server', 'channel_send', 'report_to_user', 'get_conversation',
            'update_task_progress', 'await_project_workers'} <= ROUTER_DIRECT_TOOLS


def test_project_coordinators_keep_their_core_and_the_watchdog_every_role_tool_direct(router):
    from lib.router.watchdog.binding import watchdog_location
    from lib.router.watchdog.policy import WATCHDOG_TOOLS
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_DIRECT_TOOLS
    for path, ceiling, core in (('/work/project', COORDINATOR_TOOLS, COORDINATOR_DIRECT_TOOLS),
                                (watchdog_location(OWNER), WATCHDOG_TOOLS, WATCHDOG_TOOLS)):
        names = sorted(ceiling - DISCOVERY)
        task = {'id': 'role', 'convId': 'role', '_userId': OWNER, 'config': {},
                '_coordinatorDispatchScope': {'project_path': path, 'binding_revision': 1,
                                              'conversation_id': 'role', 'user_id': OWNER}}
        cfg = {'_executableToolCatalog': [_tool(name) for name in names]}
        surface = prepare_coordinator_tool_surface(task, cfg, cfg['_executableToolCatalog'])
        assert _names(surface) == names
        assert cfg['_requiredDirectToolNames'] == sorted(core & set(names))

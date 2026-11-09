"""Every Coordinator-family surface fits the Sankuai Claude tool-name budget.

docs/LLM_COST_OPTIMIZATION.md section 6 ("Coordinator-role surfaces"): Claude on
your-llm-gateway.example.com's Anthropic face reuses its prompt cache only while the joined
tool names stay within the route budget. A Project Coordinator's human Turn
carried all 23 role tools (456 bytes), every one required-direct, so once the
artifact readers joined its ceiling each such Claude round would re-write its
whole prompt. Each role now keeps a usage-derived direct core (the Project
Coordinator's COORDINATOR_DIRECT_TOOLS, the Global Router's ROUTER_DIRECT_TOOLS,
every Watchdog tool); that route's budget hides the other role tools behind
search_tools/execute_tools, and every other route sends the whole surface.

Each surface goes through its real assembly, or the role policy over its whole
ceiling, and the final wire projection, so a ceiling or core change that pushes
a role over the budget fails here.
"""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.llm import tool_wire
from lib.project_dispatch.repository import CONTRACT
from lib.router.policy import ROUTER_DIRECT_TOOLS, ROUTER_TOOLS
from lib.router.watchdog.policy import WATCHDOG_TOOLS
from lib.tasks_pkg.coordinator_policy import (
    COORDINATOR_DIRECT_TOOLS, COORDINATOR_TOOLS, coordinator_call_rejection,
    prepare_coordinator_tool_surface)
from tests.llm.test_tool_wire_name_budget import SANKUAI_ANTHROPIC, _joined_bytes, _names, _tool
from tests.router.test_router_tool_name_budget import _assemble as _assemble_router, _wire
from tests.router.test_router_tool_name_budget import router as router
from tests.tasks_pkg.test_coordinator_policy import coordinator as coordinator
from tests.tasks_pkg.test_coordinator_tool_surface import assemble

pytestmark = pytest.mark.unit

OWNER = 7
BUDGET = tool_wire.SANKUAI_ANTHROPIC_TOOL_NAME_BUDGET_BYTES
DISCOVERY = frozenset({'search_tools', 'execute_tools'})
READERS = ('read_tool_artifact', 'search_tool_artifact')


def _assert_hidden_tools_reachable(task, body, ceiling):
    """Hidden role tools stay in the catalog, found by search, admitted by exact name."""
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names
    from lib.tools.gateway import search_executable_catalog
    wire = set(_names(body['tools']))
    catalog = _names(body['_executable_tool_catalog'])
    assert set(catalog) <= ceiling
    hidden = [name for name in catalog if name not in wire]
    for name in hidden:
        found = search_executable_catalog(body['_executable_tool_catalog'], name,
                                          visible_names=wire)
        assert found['status'] == 'ok' and found['execute_with'] == 'execute_tools'
        assert name in [item['name'] for item in found['items']]
        assert coordinator_call_rejection(task, 'execute_tools',
                                          {'calls': [{'name': name, 'arguments': {}}]}) is None
        assert coordinator_call_rejection(task, name, {}) is None
        assert name in _known_tool_names(task)
    return hidden


def _role_task(path, conversation):
    return {'id': 'task-' + conversation, 'convId': conversation, '_userId': OWNER, 'config': {},
            'abort_event': threading.Event(), 'messages': [{'role': 'user', 'content': '继续'}],
            '_coordinatorDispatchScope': {'project_path': path, 'binding_revision': 1,
                                          'conversation_id': conversation, 'user_id': OWNER}}


def _ceiling_surface(task, ceiling):
    """The role policy over a catalog holding every tool of the role's ceiling."""
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import _apply_assembled_tool_policy
    catalog = [_tool(name) for name in sorted(ceiling - DISCOVERY)]
    cfg = task['config']
    cfg['_executableToolCatalog'] = deepcopy(catalog)
    surface = prepare_coordinator_tool_surface(task, cfg, deepcopy(catalog))
    tools, available = _apply_assembled_tool_policy(cfg, task, surface, True)
    assert available
    return tools


@pytest.fixture
def homes(tmp_path, monkeypatch):
    from lib.router.home import router_home_location
    from lib.router.watchdog.binding import watchdog_location
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    return router_home_location(OWNER), watchdog_location(OWNER)


@pytest.fixture
def router_with_channels(router, monkeypatch):
    """The production Router surface: every channel tool of a configured account."""
    from lib.channels.tools import CHANNEL_TOOLS
    monkeypatch.setattr('lib.channels.tools.visible_tools',
                        lambda owner, conversation: list(CHANNEL_TOOLS))
    return router


@pytest.fixture
def watchdog(homes, monkeypatch):
    from lib.conversations import project_coordination
    _, path = homes
    binding = {'projectPath': path, 'conversationId': 'watchdog', 'availability': 'available',
               'revision': 1}
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: SimpleNamespace(read=lambda p: deepcopy(binding)))
    task = {'id': 'watchdog-task', 'convId': 'watchdog', '_userId': OWNER,
            'config': {'projectPath': path}, 'abort_event': threading.Event(),
            'messages': [{'role': 'user', 'content': '继续'}]}
    return task, path


def test_project_coordinator_human_turn_fits_the_budget_and_keeps_every_tool_reachable(coordinator):
    task, _ = coordinator
    tools = assemble(task)
    assert set(_names(tools)) == COORDINATOR_TOOLS - DISCOVERY
    assert _joined_bytes(_names(tools)) == 456
    body, projection = _wire(task, tools)
    wire = _names(body['tools'])

    assert _joined_bytes(wire) <= BUDGET
    assert DISCOVERY <= set(wire) and set(wire) <= COORDINATOR_TOOLS
    assert set(task['_requiredDirectToolNames']) == COORDINATOR_DIRECT_TOOLS
    assert COORDINATOR_DIRECT_TOOLS <= set(wire)
    hidden = _assert_hidden_tools_reachable(task, body, COORDINATOR_TOOLS)
    # The fit hides the longest names outside the core, which are its rarest tools,
    # while the partial-result recovery stays direct.
    assert set(hidden) == set(projection.budget_dropped_names) == {
        'dispatch_project_assignment', 'create_conversation'}
    assert set(READERS) <= set(wire)
    # Survivors keep their assembled order, so the cached prefix is stable.
    assert [name for name in wire if name not in DISCOVERY] == [
        name for name in _names(tools) if name in wire]
    again, _ = _wire(task, tools)
    assert again['tools'] == body['tools']


@pytest.mark.parametrize('protocol,base_url,model', [
    ('anthropic', SANKUAI_ANTHROPIC, 'kimi-k3.1-pro'),
    ('anthropic', 'https://api.anthropic.com/v1', 'claude-opus-5.5'),
    ('openai', 'https://api.openai.com/v1', 'claude-opus-5.5'),
])
def test_project_coordinator_routes_without_a_name_budget_keep_the_whole_surface_direct(
        coordinator, protocol, base_url, model):
    task, _ = coordinator
    tools = assemble(task)
    body, projection = _wire(task, tools, model=model, protocol=protocol, base_url=base_url)
    assert _names(body['tools']) == _names(tools)
    assert not projection.budget_dropped_names


def test_project_coordinator_core_leaves_room_for_discovery_and_keeps_continuations_direct():
    assert COORDINATOR_DIRECT_TOOLS <= COORDINATOR_TOOLS - DISCOVERY
    assert _joined_bytes(COORDINATOR_DIRECT_TOOLS | DISCOVERY) <= BUDGET
    # The usage order ends at the budget: the next tool would not fit.
    assert _joined_bytes(
        COORDINATOR_DIRECT_TOOLS | DISCOVERY | {'dispatch_project_assignment'}) > BUDGET
    # A continuation grant fits the budget whole, so it keeps every tool required-direct.
    assert set(CONTRACT['continuationTools']) - DISCOVERY <= COORDINATOR_DIRECT_TOOLS
    # The readers are a partial result's only recovery.
    assert set(READERS) <= COORDINATOR_DIRECT_TOOLS


@pytest.mark.parametrize('role', ['project', 'router', 'router_review', 'watchdog'])
def test_every_coordinator_role_ceiling_fits_the_claude_name_budget(homes, monkeypatch, role):
    from lib.channels.assistant import REVIEW_TOOLS
    home, watchdog_path = homes
    path, ceiling = {'project': ('/work/project', COORDINATOR_TOOLS),
                     'router': (home, ROUTER_TOOLS), 'router_review': (home, REVIEW_TOOLS),
                     'watchdog': (watchdog_path, WATCHDOG_TOOLS)}[role]
    if role == 'router_review':
        monkeypatch.setattr('lib.channels.assistant.review_tool_ceiling',
                            lambda task, scope: REVIEW_TOOLS)
    task = _role_task(path, role)
    tools = _ceiling_surface(task, ceiling)
    assert set(_names(tools)) == ceiling - DISCOVERY
    required = set(task['_requiredDirectToolNames'])
    body, _ = _wire(task, tools)
    wire = _names(body['tools'])

    assert _joined_bytes(wire) <= BUDGET, (role, _joined_bytes(wire), wire)
    assert required <= set(wire)
    _assert_hidden_tools_reachable(task, body, ceiling)
    if _joined_bytes(_names(tools)) <= BUDGET:
        # A role under the budget is sent whole, with no discovery pair added.
        assert wire == _names(tools)


def test_watchdog_keeps_every_role_tool_required_direct_under_the_budget(watchdog):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    task, path = watchdog
    cfg = task['config']
    cfg.update(_turnOwnerUserId=OWNER, memoryEnabled=False, mcpEnabled=False, plugins=[])
    tools, available = assemble_round_tools(cfg, task, {
        'tid': task['id'], 'project_enabled': True, 'project_path': path,
        'search_mode': 'off', 'search_enabled': False, 'fetch_enabled': False,
        'code_exec_enabled': True, 'browser_enabled': False, 'desktop_enabled': False,
        'image_gen_enabled': False, 'model': 'claude-opus-5.5'})
    assert available and set(_names(tools)) == WATCHDOG_TOOLS - DISCOVERY
    assert set(task['_requiredDirectToolNames']) == WATCHDOG_TOOLS - DISCOVERY
    body, projection = _wire(task, tools)
    assert _names(body['tools']) == _names(tools)
    assert _joined_bytes(_names(body['tools'])) <= BUDGET
    assert not projection.budget_dropped_names


def test_production_router_surface_fits_with_its_channel_tools_and_readers(router_with_channels):
    task, home = router_with_channels
    tools = _assemble_router(task, home)
    assert set(_names(tools)) == ROUTER_TOOLS - DISCOVERY
    body, _ = _wire(task, tools)
    wire = _names(body['tools'])
    assert _joined_bytes(wire) <= BUDGET
    assert ROUTER_DIRECT_TOOLS <= set(wire)
    _assert_hidden_tools_reachable(task, body, ROUTER_TOOLS)


def _assert_exact_name_calls_admitted(task, calls):
    """A model call by exact name parses as dispatchable and passes the role policy."""
    from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
    task.update(model='claude-opus-5.5', events=[], events_lock=threading.Lock(),
                toolRounds=[], aborted=False)
    for name, arguments in calls.items():
        call = {'id': 'call-' + name, 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)}}
        parsed, _ = parse_tool_calls({'content': '', 'tool_calls': [call]}, task, round_num=0,
                                     tool_round_num=0, project_enabled=True)
        assert [(row[1], row[6]) for row in parsed] == [(name, None)]
        assert parsed[0][5].get('status') != 'rejected'
        assert coordinator_call_rejection(task, name, arguments) is None
        batch = {'calls': [{'name': name, 'arguments': arguments}]}
        assert coordinator_call_rejection(task, 'execute_tools', batch) is None


def test_the_routers_hidden_readers_answer_the_partial_result_hint_by_exact_name(
        router_with_channels):
    """A partial envelope names its reader; hidden from the wire, an exact-name call still runs."""
    task, home = router_with_channels
    tools = _assemble_router(task, home)
    body, _ = _wire(task, tools)
    assert not set(READERS) & set(_names(body['tools']))
    reference = 'tr:read-a-conversation:0c7e3b9f'
    _assert_exact_name_calls_admitted(task, {
        'read_tool_artifact': {'artifact_ref': reference, 'cursor': '0'},
        'search_tool_artifact': {'artifact_ref': reference, 'query': 'checkpointSha'}})


def test_a_hidden_project_coordinator_tool_answers_its_exact_name(coordinator):
    task, _ = coordinator
    tools = assemble(task)
    body, _ = _wire(task, tools)
    assert 'dispatch_project_assignment' not in _names(body['tools'])
    _assert_exact_name_calls_admitted(task, {'dispatch_project_assignment': {
        'assignment_id': '8d6b344c-31a7-57d4-acf2-a8015738cecf', 'expected_revision': 1}})

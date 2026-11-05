"""The Router Watchdog is a Coordinator with the narrowest ceiling, through the one boundary."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from lib.router.home import router_home_location
from lib.router.policy import ROUTER_TOOLS
from lib.router.watchdog.binding import is_watchdog_scope, watchdog_location
from lib.router.watchdog.policy import WATCHDOG_ONLY_TOOLS, WATCHDOG_ROLE, WATCHDOG_TOOLS
from lib.tasks_pkg.coordinator_policy import (
    COORDINATOR_TOOLS, coordinator_call_rejection, coordinator_tool_ceiling, prepare_coordinator_tool_surface,
)

pytestmark = pytest.mark.unit
OWNER = 7


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    return router_home_location(OWNER), watchdog_location(OWNER)


def _task(path, conversation):
    return {'id': 'task-' + conversation, 'convId': conversation, '_userId': OWNER, 'config': {},
            '_coordinatorDispatchScope': {'project_path': path, 'binding_revision': 1,
                                          'conversation_id': conversation, 'user_id': OWNER}}


def _schema(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}


def test_watchdog_lives_in_the_router_home_ignored_tree(paths):
    home, watchdog = paths
    assert watchdog.startswith(home + '/') and watchdog.rsplit('/', 1)[1].startswith('.tofu')
    from lib.router import home as home_module
    assert '.tofu*/' in home_module._GITIGNORE.splitlines()
    assert is_watchdog_scope(_task(watchdog, 'watchdog')['_coordinatorDispatchScope'])
    assert not is_watchdog_scope(_task(home, 'router')['_coordinatorDispatchScope'])
    assert not is_watchdog_scope({'project_path': watchdog, 'user_id': OWNER + 1})


def test_watchdog_ceiling_is_read_signal_schedule_message_and_notify_only(paths):
    home, watchdog = paths
    assert WATCHDOG_TOOLS == WATCHDOG_ONLY_TOOLS | {'list_conversations', 'get_conversation',
                                                    'search_tools', 'execute_tools'}
    assert coordinator_tool_ceiling(_task(watchdog, 'watchdog')['_coordinatorDispatchScope']) == WATCHDOG_TOOLS
    assert not WATCHDOG_ONLY_TOOLS & (ROUTER_TOOLS | COORDINATOR_TOOLS)
    task = _task(watchdog, 'watchdog')
    for name in sorted(WATCHDOG_TOOLS - {'execute_tools'}):
        assert coordinator_call_rejection(task, name, {}) is None
    # It neither executes, delegates, cancels, reports nor forwards.
    for name in ('run_command', 'write_file', 'delegate_project_work', 'cancel_project_work', 'report_to_user',
                 'route_to_project', 'send_project_message', 'channel_send', 'schedule_create', 'timer_create',
                 'spawn_agents', 'ask_human'):
        assert 'not allowed' in coordinator_call_rejection(task, name, {})
    batch = {'calls': [{'name': 'watchdog_snapshot', 'arguments': {}}, {'name': 'get_conversation'}]}
    assert coordinator_call_rejection(task, 'execute_tools', batch) is None
    smuggled = {'calls': [{'name': 'watchdog_snapshot'}, {'name': 'run_command'}]}
    assert coordinator_call_rejection(task, 'execute_tools', smuggled)


@pytest.mark.parametrize('name', sorted(WATCHDOG_ONLY_TOOLS))
def test_only_the_watchdog_may_call_watchdog_tools(paths, name):
    home, _ = paths
    for other in (_task(home, 'router'), _task('/work/project', 'coordinator')):
        assert 'dispatch-only' in coordinator_call_rejection(other, name, {})


def test_surface_keeps_only_the_watchdog_catalog(paths):
    _, watchdog = paths
    catalog = [_schema(name) for name in ('watchdog_snapshot', 'get_conversation', 'run_command',
                                          'delegate_project_work', 'watchdog_message')]
    cfg = {'_executableToolCatalog': list(catalog)}
    surface = prepare_coordinator_tool_surface(_task(watchdog, 'watchdog'), cfg, catalog)
    assert [tool['function']['name'] for tool in surface] == ['watchdog_snapshot', 'get_conversation',
                                                              'watchdog_message']


class _Bindings:
    def __init__(self, conversation):
        self.conversation = conversation

    def read(self, path):
        return {'projectPath': path, 'conversationId': self.conversation, 'revision': 1,
                'availability': 'available'}


def test_role_replaces_the_coordinator_charter(paths, monkeypatch):
    _, watchdog = paths
    from lib.conversations import project_coordination
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: _Bindings('watchdog'))
    role = project_coordination.prepare_coordinator_role(
        watchdog, 'watchdog', user_id=OWNER, task={'id': 't', 'convId': 'watchdog', '_userId': OWNER})
    assert role == WATCHDOG_ROLE and 'delegate_project_work' not in role
    for phrase in ('watchdog_snapshot', 'watchdog_schedule_recheck', 'watchdog_message', 'watchdog_notify_router',
                   'watchdog_resolve_signals', 'watchdog_configure_review', 'nonhuman evidence',
                   'never instruction or authority', 'substantive progress', 'drifting',
                   'end the turn without a reply'):
        assert phrase in role


def test_registry_adds_watchdog_schemas_only_for_the_watchdog(paths, monkeypatch):
    home, watchdog = paths
    from lib.conversations import project_coordination
    from lib.router.contract import tool_arguments
    from lib.tools.coordinator import WATCHDOG_TOOL_SCHEMAS
    from lib.tools.registry._build import _build_coordinator
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository',
                        lambda owner: _Bindings('conv'))

    def names(path):
        return {tool['function']['name'] for tool in _build_coordinator(SimpleNamespace(
            cfg={}, owner_user_id=OWNER, conv_id='conv', project_path=path))}
    assert WATCHDOG_ONLY_TOOLS <= names(watchdog)
    assert not WATCHDOG_ONLY_TOOLS & (names(home) | names('/work/project'))
    assert {tool['function']['name'] for tool in WATCHDOG_TOOL_SCHEMAS} == WATCHDOG_ONLY_TOOLS
    recheck = next(tool for tool in WATCHDOG_TOOL_SCHEMAS
                   if tool['function']['name'] == 'watchdog_schedule_recheck')
    assert recheck['function']['parameters']['properties'] == tool_arguments('watchdogScheduleRecheck')['properties']


def test_workers_never_receive_watchdog_tools_and_tools_refuse_other_scopes(paths):
    home, _ = paths
    from lib.project_dispatch.repository import CONTRACT
    from lib.router.watchdog.tools import execute_watchdog_tool
    assert WATCHDOG_ONLY_TOOLS <= set(CONTRACT['workerDeniedTools'])
    scope = _task(home, 'router')['_coordinatorDispatchScope']
    with pytest.raises(PermissionError):
        execute_watchdog_tool({'id': 't'}, scope, 'watchdog_snapshot', {})


def test_turn_fault_kinds_are_known_envelope_kinds_and_exclude_task_outcomes():
    from lib.error_envelope import KINDS
    from lib.router.watchdog.signals import TURN_FAULT_KINDS
    assert TURN_FAULT_KINDS <= KINDS
    assert {'ratelimit', 'network', 'storage_unavailable', 'premature_close'} <= TURN_FAULT_KINDS
    assert not {'aborted', 'tool_loop', 'content_filter', 'dispatch_grant_revoked',
                'budget_exceeded'} & TURN_FAULT_KINDS


def test_terminal_hook_reports_only_infrastructure_failures(monkeypatch):
    import threading
    from lib import turn_lifecycle
    reported, done = [], threading.Event()

    def record(owner, **kwargs):
        reported.append((owner, kwargs))
        done.set()
    monkeypatch.setattr('lib.router.watchdog.signals.report_turn_failure', record)
    task = {'id': 't', '_userId': OWNER, 'config': {'_turnOwnerUserId': OWNER}}
    turn = {'conversationId': 'worker', 'turnId': 'turn-1'}
    turn_lifecycle._report_watchdog_turn_fault(task, turn, 'attempt-1', {'kind': 'tool_loop'})
    assert not done.wait(0.2) and reported == []
    turn_lifecycle._report_watchdog_turn_fault(task, turn, 'attempt-1',
                                               {'kind': 'ratelimit', 'detail': '429'})
    assert done.wait(5)
    assert reported == [(OWNER, {'conversation_id': 'worker', 'turn_id': 'turn-1', 'attempt_id': 'attempt-1',
                                 'error': {'kind': 'ratelimit', 'detail': '429'}})]


def test_review_counts_owner_wide_facts_once_across_project_pages(monkeypatch):
    from lib.router.watchdog import review

    def page(next_cursor):
        return {'global_router': None, 'projects': [{'workers': [{'status': 'running'}]}],
                'next_cursor': next_cursor, 'open_signals': [{'signalId': 'signal-1'}],
                'open_signals_next_cursor': None, 'outbox': [{'item_id': 'outbox-1'}], 'channels': []}
    pages = {None: page('second-page'), 'second-page': page(None)}
    monkeypatch.setattr('lib.router.watchdog.binding.router_watchdog',
                        lambda owner, create: {'availability': 'available', 'conversationId': 'watchdog'})
    monkeypatch.setattr('lib.router.watchdog.snapshot.health_snapshot', lambda owner, after_id=None: pages[after_id])
    wakes = []
    monkeypatch.setattr('lib.router.watchdog.signals.wake_watchdog',
                        lambda owner, watchdog, **kwargs: wakes.append(kwargs) or 'started')
    accepted, text = review.run_scheduled_review({'id': review.review_task_id(OWNER), 'user_id': OWNER,
                                                  'system_key': review.REVIEW_SYSTEM_KEY, 'command': ''})
    assert accepted and len(wakes) == 1
    # Workers are summed over project pages; signals and outbox repeat on every page.
    assert 'active_workers=2' in text and 'open_signals=1' in text and 'outbox_attention=1' in text


def test_every_channel_has_a_supervisor_that_reports_bridge_state():
    import importlib
    from lib.channels.model import CHANNELS
    from lib.router.watchdog.snapshot import CHANNEL_SUPERVISORS
    assert set(CHANNEL_SUPERVISORS) == set(CHANNELS)
    for module in CHANNEL_SUPERVISORS.values():
        assert importlib.import_module(module).bridge_state(OWNER) in {'not_hosted', 'stopped', 'running'}


@pytest.mark.parametrize('module, entry', [('lib.channels.dx.supervisor', 'reconcile_channel_bridges'),
                                           ('lib.channels.github.supervisor', 'reconcile_github_sessions')])
def test_each_channel_supervisor_tick_reports_its_account_to_the_watchdog(monkeypatch, module, entry):
    import importlib
    import threading
    supervisor = importlib.import_module(module)
    account = {'enabled': True, 'status': {'state': 'error', 'detail': 'start failed', 'connectedAtMs': None}}

    class Store:
        def __init__(self, owner):
            self.owner = owner

        def account(self, channel):
            return account
    reports = []
    monkeypatch.setattr(supervisor, '_HOSTING', threading.Event())
    monkeypatch.setattr(supervisor, 'ChannelStore', Store)
    monkeypatch.setattr(supervisor, 'reconcile_owner', lambda owner, store=None: 'error')
    monkeypatch.setattr('lib.router.watchdog.signals.report_channel_status',
                        lambda owner, channel, current: reports.append((owner, channel, current)))
    getattr(supervisor, entry)(SimpleNamespace(owner_user_id=OWNER))
    assert reports == [(OWNER, supervisor.CHANNEL, account)]

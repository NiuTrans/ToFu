"""Project Router reports relay to the Global Router as passive notices (R3)."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.router.home import router_home_location
from lib.router.relay import relay_command_id, relay_project_report
from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
from tests.project_mod.test_coordinator_reports import reports as reports  # noqa: F401  fixture

pytestmark = pytest.mark.unit
REPORT = {'projectPath': '/work/project', 'coordinatorId': 'coordinator-project', 'turnId': 'report-turn',
          'kind': 'delivery', 'title': 'Parser fix merged',
          'message': 'Fixed the null pointer; regression test added and green.'}


@pytest.fixture
def router(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    state = SimpleNamespace(binding={'availability': 'available', 'conversationId': 'router-conv',
                                     'revision': 1, 'projectPath': router_home_location(7)},
                            calls=[])
    monkeypatch.setattr('lib.router.binding.global_router',
                        lambda owner, *, create, client=None: deepcopy(state.binding))

    def dispatch(conversation, message, tools, **kwargs):
        state.calls.append({'conversation': conversation, 'message': deepcopy(message), **kwargs})
        return ScheduledTurnDispatch('started', 'router-task')
    state.dispatch = dispatch
    return state


def test_notice_inlines_the_chain_of_recall_and_wakes_with_a_peer_initiator(router):
    assert relay_project_report(7, REPORT, dispatch=router.dispatch) == 'started'
    [call] = router.calls
    assert call['conversation'] == 'router-conv' and call['user_id'] == 7
    assert call['inject_mode'] == 'steer'
    message = call['message']
    assert message['_initiator'] == 'peer'
    assert message['origin'] == {'initiator': 'peer', 'sourceConversationId': 'coordinator-project'}
    for fact in ('/work/project', 'coordinator-project', 'kind=delivery', 'report-turn',
                 'Parser fix merged', REPORT['message'], 'nonhuman'):
        assert fact in message['content']
    assert call['command_id'] == relay_command_id(7, 'coordinator-project', 'report-turn')


def test_relay_is_idempotent_per_report_turn(router):
    relay_project_report(7, REPORT, dispatch=router.dispatch)
    relay_project_report(7, REPORT, dispatch=router.dispatch)
    relay_project_report(7, {**REPORT, 'turnId': 'later-report'}, dispatch=router.dispatch)
    first, replay, later = (call['command_id'] for call in router.calls)
    assert first == replay != later


@pytest.mark.parametrize('case', ['home-report', 'no-router', 'router-own-report'])
def test_nothing_is_relayed_without_a_distinct_available_router(router, case):
    report = dict(REPORT)
    if case == 'home-report':
        report['projectPath'] = router_home_location(7)
    elif case == 'no-router':
        router.binding = {'availability': 'unbound', 'conversationId': '', 'revision': 0}
    else:
        report['coordinatorId'] = 'router-conv'
    assert relay_project_report(7, report, dispatch=router.dispatch) is None
    assert not router.calls


def test_publication_relays_each_report_turn_and_survives_relay_failure(reports):  # noqa: F811
    r = reports
    relayed = []

    def relay(owner, report, *, client):
        relayed.append((owner, deepcopy(report)))
        if len(relayed) == 1:
            raise RuntimeError('router unavailable')
    repo = r.factory()
    repo.relay = relay
    sent = repo.publish(r.scope, r.request)
    assert sent['published'] is True
    assert relayed == [(7, {'projectPath': '/project', 'coordinatorId': 'coordinator',
                            'turnId': sent['turnId'], 'kind': 'decision', 'title': 'Decision needed',
                            'message': 'Choose the migration window.'})]
    replay = repo.publish(r.scope, r.request)
    assert replay['turnId'] == sent['turnId'] and relayed[1][1]['turnId'] == sent['turnId']
    from lib.conversations.coordinator_reports import POLICY
    assert not [key for (namespace, key) in r.store.rows if namespace == POLICY['pendingNamespace']]


def test_recovered_publication_relays_through_the_same_path(reports):  # noqa: F811
    from lib.conversations.coordinator_reports import recover_pending_reports
    r = reports
    relayed = []
    repo = r.factory()
    key, intent = repo._retain_intent(r.scope, r.request)
    assert intent is not None
    factory = lambda owner, path: r.factory(owner, path)  # noqa: E731

    def build(owner, path):
        instance = factory(owner, path)
        instance.relay = lambda owner, report, *, client: relayed.append(report['turnId'])
        return instance
    assert recover_pending_reports(client=r.store, repository_factory=build) == 1
    assert len(relayed) == 1


def test_default_relay_reads_through_the_reports_storage_port(reports, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    r = reports
    sent = r.factory().publish(r.scope, {**r.request, 'idempotency_key': 'default-port'})
    assert sent['published'] is True

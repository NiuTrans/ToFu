"""Router Watchdog over a real storage authority (docs/ROUTER.md R7).

Provisioning, durable idempotent fault signals and their wake, the owner-scoped
health snapshot, interventions, and the owner-controlled scheduler review.
Wakes go through a recording dispatcher except where the real turn command and
task start are the subject; no model runs.
"""
from __future__ import annotations

from datetime import datetime
import os
from types import SimpleNamespace

import pytest

from tests.support.semantic_authorities import semantic_authority as semantic_authority

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = [pytest.mark.unit,
              pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)]

RATE_LIMIT = {'kind': 'ratelimit', 'detail': '429 Too Many Requests', 'hint': 'Wait for the window.'}


@pytest.fixture
def world(semantic_authority, monkeypatch, tmp_path):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.router.binding import global_router
    from lib.router.watchdog.binding import router_watchdog
    from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
    client, owner, _engine = semantic_authority
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.get_storage_client', client)
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(tmp_path / 'router'))
    router = global_router(owner, create=True)
    watchdog = router_watchdog(owner, create=True)
    project = tmp_path / 'alpha'
    project.mkdir()
    coordinator = ProjectCoordinationRepository(owner).create(
        str(project), folder_id='folder', expected_revision=0, command_id='alpha')
    for conversation_id, settings in (
            ('worker-alpha', {'projectPath': str(project), '_coordinatorSource': coordinator['conversationId']}),
            ('plain-chat', {})):
        client(write=True).command('conversation.create', {
            'conv_id': conversation_id, 'user_id': owner, 'title': conversation_id, 'settings': settings,
        }, 'create-' + conversation_id)
    woken = []

    def dispatch(conversation_id, message, tools_config, **kwargs):
        woken.append((conversation_id, message, kwargs))
        return ScheduledTurnDispatch('started', task_id=f'wake-{len(woken)}')
    # A native authority serves exactly one owner and refuses another owner's
    # owned rows outright (authority_scope_mismatch); SQLite hosts several.
    return SimpleNamespace(client=client, owner=owner, router=router, watchdog=watchdog,
                           project=str(project), coordinator=coordinator, worker='worker-alpha',
                           woken=woken, dispatch=dispatch, tmp=tmp_path, native=_engine is not None)


def _scope(world):
    return {'user_id': world.owner, 'project_path': world.watchdog['projectPath'],
            'conversation_id': world.watchdog['conversationId'], 'binding_revision': world.watchdog['revision']}


def _fail(world, conversation_id, turn, error=RATE_LIMIT, **kwargs):
    from lib.router.watchdog.signals import report_turn_failure
    return report_turn_failure(world.owner, conversation_id=conversation_id, turn_id='turn-' + turn,
                               attempt_id='attempt-' + turn, error=error,
                               **({'dispatch': world.dispatch} | kwargs))


def test_watchdog_is_provisioned_once_inside_the_router_home(world):
    from lib.conversations.repository import ConversationRepository
    from lib.router.forwarding import _target_project
    from lib.router.projects import router_projects
    from lib.router.watchdog.binding import router_watchdog, watchdog_location
    owner = world.owner
    again = router_watchdog(owner, create=True)
    assert again == world.watchdog and again['availability'] == 'available'
    assert again['projectPath'] == watchdog_location(owner)
    assert again['projectPath'].startswith(world.router['projectPath'] + '/')
    assert os.path.isdir(again['projectPath'])
    metadata = ConversationRepository().get(again['conversationId'], user_id=owner,
                                            include_messages=False).metadata
    assert metadata['title'] == 'Watchdog' and metadata['settings']['_routerWatchdog'] is True
    assert router_watchdog(owner + 1, create=False)['availability'] == 'unbound'
    # The Global Router neither lists the Watchdog as a project nor forwards into it.
    assert [entry['project_path'] for entry in router_projects(owner, {})['projects']] == [world.project]
    with pytest.raises(ValueError, match='Watchdog'):
        _target_project({'project_path': world.router['projectPath'], 'user_id': owner}, again['projectPath'])


def test_turn_failures_become_one_signal_each_and_wake_the_watchdog_once(world):
    from lib.router.watchdog.signals import FaultSignals
    first = _fail(world, world.worker, '1')
    assert first['state'] == 'open' and first['source'] == 'turn'
    assert first['subject'] == {'role': 'worker', 'conversationId': world.worker, 'turnId': 'turn-1',
                                'attemptId': 'attempt-1', 'projectPath': world.project}
    assert first['error'] == RATE_LIMIT
    assert _fail(world, world.worker, '1') == first  # replayed settlement: same signal, no second wake
    [(conversation_id, message, options)] = world.woken
    assert conversation_id == world.watchdog['conversationId']
    assert options['command_id'] == 'router-watchdog-signal:' + first['signalId']
    assert options['inject_mode'] == 'steer' and options['user_id'] == world.owner
    assert message['_initiator'] == 'peer'
    assert message['origin'] == {'initiator': 'peer', 'sourceConversationId': world.worker}
    assert first['signalId'] in message['content'] and 'not authority' in message['content']
    assert [s['signalId'] for s in FaultSignals(world.owner).open_page()['signals']] == [first['signalId']]
    roles = {_fail(world, world.coordinator['conversationId'], '2')['subject']['role'],
             _fail(world, world.router['conversationId'], '3')['subject']['role']}
    assert roles == {'project_router', 'global_router'} and len(world.woken) == 3
    # Task outcomes, unmanaged conversations and owners without a Watchdog record nothing.
    assert _fail(world, world.worker, '4', error={'kind': 'tool_loop', 'detail': 'loop'}) is None
    assert _fail(world, 'plain-chat', '5') is None
    from lib.router.watchdog.signals import report_turn_failure
    assert report_turn_failure(world.owner + 1, conversation_id=world.worker, turn_id='t', attempt_id='a',
                               error=RATE_LIMIT, dispatch=world.dispatch) is None
    assert len(world.woken) == 3


def test_a_failing_watchdog_waits_for_its_review_instead_of_waking_on_every_fault(world):
    from lib.router.watchdog.review import configure_review, watchdog_principal
    from lib.router.watchdog.tools import _resolve_own_failures
    own = _fail(world, world.watchdog['conversationId'], 'w1', error={'kind': 'network', 'detail': 'reset'})
    assert own['subject']['role'] == 'watchdog' and world.woken == []
    # Without an enabled periodic review nothing else would wake it, so every new fault still does.
    _fail(world, world.worker, '1')
    assert len(world.woken) == 1
    configure_review(world.owner, {'enabled': True}, principal=watchdog_principal(world.owner))
    assert _fail(world, world.worker, '2')['state'] == 'open' and len(world.woken) == 1
    assert _resolve_own_failures(world.owner, world.watchdog['conversationId']) == [own['signalId']]
    _fail(world, world.worker, '3')
    assert len(world.woken) == 2


def test_recovery_outbox_and_channel_faults_are_one_signal_per_episode(world):
    from lib.channels.model import OutboxItem
    from lib.router.watchdog.signals import (report_channel_status, report_outbox_fault,
                                             report_recovery_failure)
    from lib.storage.errors import StorageError
    owner, dispatch = world.owner, world.dispatch
    assignment = {'id': 'assignment-1', 'request': {'recipientId': world.worker}, 'grant': {'revision': 3}}
    recovery = report_recovery_failure(owner, world.project, StorageError('storage_unavailable', 'down at 12:00'),
                                       assignment=assignment, dispatch=dispatch)
    assert recovery['subject'] == {'role': 'dispatch', 'projectPath': world.project,
                                   'assignmentId': 'assignment-1', 'conversationId': world.worker}
    assert report_recovery_failure(owner, world.project, StorageError('storage_unavailable', 'down at 12:01'),
                                   assignment=assignment, dispatch=dispatch)['signalId'] == recovery['signalId']
    assert report_recovery_failure(owner, world.project, PermissionError('rebound'),
                                   assignment=assignment, dispatch=dispatch) is None
    boots = {report_recovery_failure(owner, world.project, RuntimeError('scope'), episode=episode,
                                     dispatch=dispatch)['signalId'] for episode in (1, 1, 2)}
    assert len(boots) == 2

    def item(state, attempt):
        return OutboxItem(id='outbox-1', thread='dx:gid:500', text='hi', idempotency_key='k', state=state,
                          origin={}, created_at_ms=1, updated_at_ms=1, attempt_at_ms=attempt, error='timeout')
    unknown = report_outbox_fault(owner, item('unknown', 10), dispatch=dispatch)
    assert unknown['subject'] == {'role': 'outbox', 'outboxItemId': 'outbox-1', 'thread': 'dx:gid:500',
                                  'channel': 'dx'}
    assert report_outbox_fault(owner, item('unknown', 20), dispatch=dispatch)['signalId'] == unknown['signalId']
    assert report_outbox_fault(owner, item('failed', 30), dispatch=dispatch)['signalId'] != unknown['signalId']
    assert report_outbox_fault(owner, item('sent', 40), dispatch=dispatch) is None

    def account(state, *, updated, connected, enabled=True, detail='bridge start failed: E'):
        return {'enabled': enabled, 'status': {'state': state, 'detail': detail,
                                               'updatedAtMs': updated, 'connectedAtMs': connected}}
    channel = report_channel_status(owner, 'dx', account('error', updated=5, connected=1), dispatch=dispatch)
    assert channel['subject'] == {'role': 'channel', 'channel': 'dx'} and channel['error']['kind'] == 'channel_error'
    # The supervisor restarts a failing bridge every tick with a fresh time and detail;
    # the episode is the state since the last connection.
    assert report_channel_status(owner, 'dx', account('error', updated=9, connected=1, detail='bridge exited (1)'),
                                 dispatch=dispatch)['signalId'] == channel['signalId']
    assert report_channel_status(owner, 'dx', account('error', updated=60, connected=50),
                                 dispatch=dispatch)['signalId'] != channel['signalId']
    assert report_channel_status(owner, 'dx', account('needs_login', updated=65, connected=50),
                                 dispatch=dispatch)['error']['kind'] == 'channel_needs_login'
    assert report_channel_status(owner, 'dx', account('connected', updated=70, connected=70),
                                 dispatch=dispatch) is None
    assert report_channel_status(owner, 'dx', account('error', updated=80, connected=70, enabled=False),
                                 dispatch=dispatch) is None
    assert len(world.woken) == 8


def test_compute_stalls_are_one_signal_per_failure_streak_and_close_on_recovery(world):
    from lib.router.watchdog.signals import FaultSignals, report_compute_stall, resolve_compute_stall
    owner = world.owner
    stall = dict(run_id='run-1', operation_id='job-1', project_path=world.project,
                 kind='compute_observation_failed', dispatch=world.dispatch)
    first = report_compute_stall(owner, failure='ModuleNotFoundError', since=100.0,
                                 detail="ModuleNotFoundError: No module named 'lib.runtime_paths'", **stall)
    assert first['source'] == 'compute' and first['state'] == 'open'
    assert first['subject'] == {'role': 'compute', 'runId': 'run-1', 'operationId': 'job-1',
                                'projectPath': world.project}
    assert first['error']['kind'] == 'compute_observation_failed'
    assert 'lib.runtime_paths' in first['error']['detail'] and 'never resubmitted' in first['error']['hint']
    # The reaper re-reads every pass: the same class in the same streak is the same signal and wake.
    assert report_compute_stall(owner, failure='ModuleNotFoundError', since=100.0, detail='other text',
                                **stall)['signalId'] == first['signalId']
    [(conversation_id, message, _options)] = world.woken
    assert conversation_id == world.watchdog['conversationId']
    assert 'run=run-1' in message['content'] and 'operation=job-1' in message['content']
    other = report_compute_stall(owner, failure='PermissionError', since=100.0, detail='denied', **stall)
    assert other['signalId'] != first['signalId'] and len(world.woken) == 2
    assert resolve_compute_stall(owner, first['signalId'], 'The status read succeeded again.') is True
    assert FaultSignals(owner).get(first['signalId'])['state'] == 'resolved'
    # A resolved signal never reopens; a later streak is a new signal.
    assert report_compute_stall(owner, failure='ModuleNotFoundError', since=100.0, detail='x',
                                **stall)['state'] == 'resolved'
    later = report_compute_stall(owner, failure='ModuleNotFoundError', since=200.0, detail='x', **stall)
    assert later['state'] == 'open' and later['signalId'] != first['signalId'] and len(world.woken) == 3
    assert {s['signalId'] for s in FaultSignals(owner).open_page()['signals']} == {other['signalId'],
                                                                                    later['signalId']}
    # Never raises into the reporting owner.
    assert resolve_compute_stall(owner, 'unknown-signal', 'x') is False
    assert report_compute_stall(owner, failure='', since=1.0, detail='', **stall) is None


def test_snapshot_is_owner_scoped(world):
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.router.watchdog.signals import FaultSignals
    from lib.router.watchdog.snapshot import health_snapshot, needs_review
    other = world.owner + 1
    beta = world.tmp / 'beta'
    beta.mkdir()
    if not world.native:
        ProjectCoordinationRepository(other).create(str(beta), folder_id='folder', expected_revision=0,
                                                    command_id='beta')
    foreign, _ = FaultSignals(other).record('turn', ['foreign'], {'role': 'worker', 'conversationId': 'w'},
                                            {'kind': 'network', 'detail': ''})
    own = _fail(world, world.coordinator['conversationId'], '1')
    snapshot = health_snapshot(world.owner)
    assert snapshot['global_router']['conversation_id'] == world.router['conversationId']
    assert [entry['project_path'] for entry in snapshot['projects']] == [world.project]
    assert snapshot['projects'][0]['open_signal_ids'] == [own['signalId']]
    assert snapshot['projects'][0]['workers'] == []
    assert [signal['signalId'] for signal in snapshot['open_signals']] == [own['signalId']]
    assert snapshot['outbox'] == [] and snapshot['channels'] == []
    assert needs_review(snapshot)['open_signals'] == 1
    theirs = health_snapshot(other)
    assert theirs['global_router'] is None
    assert [entry['project_path'] for entry in theirs['projects']] == ([] if world.native else [str(beta)])
    assert [signal['signalId'] for signal in theirs['open_signals']] == [foreign['signalId']]


def test_resolving_closes_signals_and_never_reopens_them(world):
    from lib.router.watchdog.signals import FaultSignals
    signal = _fail(world, world.worker, '1')
    [resolved] = FaultSignals(world.owner).resolve([signal['signalId']], 'Provider recovered; Worker resumed.')
    assert resolved['state'] == 'resolved' and resolved['resolution'] == 'Provider recovered; Worker resumed.'
    assert FaultSignals(world.owner).open_page()['signals'] == []
    assert _fail(world, world.worker, '1')['state'] == 'resolved' and len(world.woken) == 1
    with pytest.raises(ValueError):
        FaultSignals(world.owner).resolve(['unknown-signal'], 'x')


def test_message_reaches_a_router_or_worker_of_any_project_as_nonhuman_context(world):
    from lib.conversations.project_messages import ProjectMailbox, _watchdog_sender
    from lib.router.watchdog.tools import execute_watchdog_tool
    arguments = {'conversation_id': world.worker, 'kind': 'steer', 'idempotency_key': 'loop-1',
                 'content': 'You retried the same failing test 9 times; read the error and change approach.'}
    sent = execute_watchdog_tool({'id': 'watchdog-task'}, _scope(world), 'watchdog_message', arguments)
    assert sent['recipient_role'] == 'worker' and sent['project_path'] == world.project
    assert sent['execution_started'] is False
    head, _ = ProjectMailbox(world.owner, world.project, world.worker).head()
    [message] = head['messages']
    assert message['sender'] == world.watchdog['conversationId'] and message['kind'] == 'steer'
    assert message['content'].startswith('[From the owner\u2019s Router Watchdog')
    assert execute_watchdog_tool({'id': 'watchdog-task'}, _scope(world), 'watchdog_message',
                                 arguments)['message']['id'] == message['id']
    # Delivery admits the owner's bound Watchdog as a sender in every project, nobody else.
    assert _watchdog_sender(world.owner, world.watchdog['conversationId'])
    assert not _watchdog_sender(world.owner, world.coordinator['conversationId'])
    assert not _watchdog_sender(world.owner + 1, world.watchdog['conversationId'])
    for target in ('plain-chat', world.watchdog['conversationId']):
        with pytest.raises(PermissionError):
            execute_watchdog_tool({'id': 't'}, _scope(world), 'watchdog_message',
                                  {**arguments, 'conversation_id': target, 'idempotency_key': target})


def test_router_notice_is_a_peer_input_refused_while_the_router_is_failing(world):
    from lib.router.watchdog.tools import _notify_router
    arguments = {'message': 'Every Worker hits 429 since 12:00; the owner may need another key.',
                 'idempotency_key': 'quota-1'}
    result = _notify_router({'id': 't'}, _scope(world), arguments, dispatch=world.dispatch)
    assert result == {'router_conversation_id': world.router['conversationId'], 'disposition': 'started',
                      'execution_started': False}
    _notify_router({'id': 't'}, _scope(world), arguments, dispatch=world.dispatch)
    (first_conversation, message, first), (_, _, second) = world.woken
    assert first_conversation == world.router['conversationId']
    assert first['command_id'].startswith('router-watchdog-notice:') and first['command_id'] == second['command_id']
    assert message['origin'] == {'initiator': 'peer', 'sourceConversationId': world.watchdog['conversationId']}
    assert 'not the owner\u2019s words' in message['content'] and arguments['message'] in message['content']
    with pytest.raises(ValueError, match='Unknown'):
        _notify_router({'id': 't'}, _scope(world), {**arguments, 'signal_ids': ['nope']}, dispatch=world.dispatch)
    _fail(world, world.router['conversationId'], 'r1')
    with pytest.raises(PermissionError, match='recovered'):
        _notify_router({'id': 't'}, _scope(world), {**arguments, 'idempotency_key': 'quota-2'},
                       dispatch=world.dispatch)


def test_periodic_review_is_one_owner_controlled_scheduler_task(world):
    from lib.identity import PrincipalContext
    from lib.router.watchdog.review import (DEFAULT_REVIEW_SCHEDULE, configure_review, review_state,
                                            review_task_id, watchdog_principal)
    from lib.scheduler.manager import get_scheduler
    owner = world.owner
    assert review_state(owner) == {'enabled': False, 'schedule': DEFAULT_REVIEW_SCHEDULE, 'taskId': None}
    principal = watchdog_principal(owner)
    enabled = configure_review(owner, {'enabled': True}, principal=principal)
    assert enabled == {'enabled': True, 'schedule': '7,37 * * * *', 'taskId': review_task_id(owner)}
    row = get_scheduler().get_task(enabled['taskId'], user_id=owner)
    assert row['task_type'] == 'router_watchdog_review' and row['system_key'] == 'router-watchdog-review'
    faster = configure_review(owner, {'enabled': True, 'schedule': '*/15 * * * *'}, principal=principal)
    assert faster == {**enabled, 'schedule': '*/15 * * * *'}
    assert configure_review(owner, {'enabled': False}, principal=principal) == {**faster, 'enabled': False}
    rows = [task for task in get_scheduler().list_tasks(user_id=owner, include_disabled=True)
            if task['task_type'] == 'router_watchdog_review']
    assert [task['id'] for task in rows] == [enabled['taskId']]
    for schedule in ('once:2099-01-01 00:00', 'every minute'):
        with pytest.raises(ValueError):
            configure_review(owner, {'enabled': True, 'schedule': schedule}, principal=principal)
    with pytest.raises(PermissionError):
        configure_review(owner, {'enabled': True}, principal=PrincipalContext.user(
            subject_id='no-scope', owner_user_id=owner, scopes=set()))
    if world.native:
        from lib.storage.errors import StorageError
        with pytest.raises(StorageError, match='authority_scope_mismatch'):
            review_state(owner + 1)
    else:
        assert review_state(owner + 1)['taskId'] is None


class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2030, 1, 1, 12, 0, 30)


def test_scheduled_reviews_skip_idle_snapshots_and_rechecks_always_wake(world, monkeypatch):
    from lib.router.watchdog import review
    from lib.router.watchdog.signals import FaultSignals
    from lib.scheduler.manager import get_scheduler
    owner, scheduler = world.owner, get_scheduler()
    monkeypatch.setattr(review, 'datetime', _FrozenClock)
    wakes = []
    monkeypatch.setattr('lib.router.watchdog.signals.wake_watchdog',
                        lambda owner, watchdog, **kwargs: wakes.append(kwargs) or 'started')
    principal = review.watchdog_principal(owner)
    periodic = scheduler.get_task(review.configure_review(owner, {'enabled': True}, principal=principal)['taskId'],
                                  user_id=owner)
    success, text = scheduler._execute_task(periodic)
    assert success and text.startswith('Nothing to review') and wakes == []
    signal = _fail(world, world.worker, '1')
    wakes.clear()  # the signal's own wake goes through the same port
    assert scheduler._execute_task(periodic)[0] and scheduler._execute_task(periodic)[0]
    first, replay = wakes
    assert first['initiator'] == 'proactive' and first['origin'] == {'scheduledTaskId': periodic['id']}
    assert first['command_id'] == replay['command_id'] and 'open signals=1' in first['content']
    arguments = {'after_minutes': 10, 'reason': 'Provider 429s: confirm the Workers resumed.',
                 'idempotency_key': 'recheck-1'}
    armed = review.schedule_recheck(owner, world.watchdog['conversationId'], arguments, principal=principal,
                                    now=datetime(2030, 1, 1, 12, 0, 30))
    assert armed['schedule'] == 'once:2030-01-01 12:10' and armed['enabled'] and armed['reason'] == arguments['reason']
    assert review.schedule_recheck(owner, world.watchdog['conversationId'], arguments, principal=principal) == armed
    with pytest.raises(ValueError):
        review.schedule_recheck(owner, world.watchdog['conversationId'], {**arguments, 'reason': 'other'},
                                principal=principal)
    FaultSignals(owner).resolve([signal['signalId']], 'Recovered.')
    success, _ = scheduler._execute_task(scheduler.get_task(armed['task_id'], user_id=owner))
    assert success and len(wakes) == 3 and arguments['reason'] in wakes[-1]['content']


def test_review_tool_acts_only_on_the_owners_own_message(world, monkeypatch):
    from lib import turn_lifecycle as turns
    from lib.identity import PrincipalContext
    from lib.router.watchdog.review import review_state
    from lib.router.watchdog.tools import execute_watchdog_tool
    from lib.turn_initiation import INITIATOR_PEER, stamp_initiator
    owner, conversation = world.owner, world.watchdog['conversationId']

    def turn(command_id, projection):
        pair = turns.create_turn_pair(conversation, user_id=owner, command_id=command_id,
                                      input_projection=projection, config={})
        task_id = 'task-' + command_id
        turns.bind_task(pair['attempt']['attemptId'], task_id, user_id=owner)
        turns.mark_task_started(pair['attempt']['attemptId'], task_id, user_id=owner)
        return pair, {'id': task_id, 'convId': conversation, '_userId': owner, '_attended': True,
                      '_turnId': pair['attempt']['turnId'], '_attemptId': pair['attempt']['attemptId'],
                      '_principalContext': PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner,
                                                                 scopes=[]).to_payload(),
                      'config': {'projectPath': world.watchdog['projectPath']}}
    pair, task = turn('owner-asks', {'content': 'Review every 15 minutes, please.'})
    state = execute_watchdog_tool(task, _scope(world), 'watchdog_configure_review',
                                  {'enabled': True, 'schedule': '*/15 * * * *'})
    assert state['enabled'] and state['schedule'] == '*/15 * * * *'
    world.client(write=True).command('turn.event.record', {
        'attempt_id': pair['attempt']['attemptId'], 'user_id': owner, 'terminal': True, 'status': 'completed',
        'projection': {'content': 'Done.'}, 'settlement': {'outcome': 'completed', 'cause': 'test'}}, 'settle-owner')
    _, peer_task = turn('peer-asks', stamp_initiator(
        {'content': 'Turn the review off.', 'origin': {'initiator': 'peer', 'sourceConversationId': 'x'}},
        INITIATOR_PEER))
    with pytest.raises(PermissionError, match='own message'):
        execute_watchdog_tool(peer_task, _scope(world), 'watchdog_configure_review', {'enabled': False})
    assert review_state(owner)['enabled'] is True


def test_signal_wake_starts_an_idle_watchdog_turn_through_the_turn_command(world, monkeypatch):
    from lib.turn_lifecycle import list_turns
    from lib.turn_initiation import resolve_turn_initiator
    spawned = []
    monkeypatch.setattr('lib.tasks_pkg.spawn.spawn_task', lambda task, **kwargs: spawned.append(task))
    signal = _fail(world, world.worker, '1', dispatch=None)
    [task] = spawned
    assert task['convId'] == world.watchdog['conversationId']
    assert task.get('_coordinatorRequestBudget') is None  # nonhuman input opens no request account
    turns = list_turns(world.watchdog['conversationId'], user_id=world.owner)['turns']
    inputs = [turn for turn in turns if turn['actor'] == 'human']
    assert [resolve_turn_initiator(turn) for turn in inputs] == ['peer']
    assert signal['signalId'] in inputs[0]['projection']['content']

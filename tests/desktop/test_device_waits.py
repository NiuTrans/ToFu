"""Durable device waits (docs/modules/remote_execution.md, "Waiting for an offline device").

A conversation that needs an offline device registers one wait with
``await_device``; the record survives restarts in portable record CAS. The poll
route observes each device's online transition once per connection epoch and
wakes every pending wait naming that device through the ordinary turn command,
exactly once per wait, owner-matched, with the registering turn's own execution
choices. Owners list and cancel waits; deleting the conversation cancels them.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from lib.desktop import bridge as db
from lib.desktop import device_waits as dw
from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
from lib.storage.errors import StorageError
from tests.channels.conftest import RecordClient
from tests.storage.test_storage_authority_sql_contract import storage as storage  # noqa: F401

pytestmark = pytest.mark.unit
OWNER, OTHER = 7, 8
REASON = 'Run ~/bin/backup.sh on the laptop and report the archive size.'


@pytest.fixture(autouse=True)
def _clean_bridge(monkeypatch):
    monkeypatch.setattr(db, '_last_poll', [0.0])
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()
    yield
    with db.command_queue_lock:
        db.command_queue.clear()
        db._agents.clear()
        db._streams.clear()


@pytest.fixture
def records():
    return RecordClient()


class Dispatch:
    """Records wake calls; ``disposition`` is the outcome the turn command reports."""

    def __init__(self, disposition='started'):
        self.calls = []
        self.disposition = disposition

    def __call__(self, conversation_id, message, tools_config, **kwargs):
        self.calls.append({'conversation_id': conversation_id, 'message': copy.deepcopy(message),
                           'tools_config': copy.deepcopy(tools_config), **copy.deepcopy(kwargs)})
        return ScheduledTurnDispatch(self.disposition,
                                     task_id='wake-task' if self.disposition == 'started' else '')


@pytest.fixture
def dispatch():
    return Dispatch()


def _task(conversation='conv-a', owner=OWNER, **config):
    return {'id': f'task-{conversation}', 'convId': conversation, '_userId': owner,
            '_turnId': f'turn-{conversation}',
            'config': {'desktopEnabled': True, 'codeExecEnabled': True, 'projectPath': '/work',
                       'searchMode': 'off', **config}}


def _connect(agent_id='laptop-1', owner=OWNER, name='Laptop'):
    return db.register_agent(agent_id, {'name': name, 'platform': 'linux'}, user_id=str(owner))


def _offline(agent_id='laptop-1'):
    with db.command_queue_lock:
        db._agents[agent_id]['last_seen'] -= db._CONNECTED_WINDOW_S + 1


def _known_offline(agent_id='laptop-1', owner=OWNER, name='Laptop'):
    _connect(agent_id, owner, name)
    _offline(agent_id)


def _await(records, dispatch, task=None, device='Laptop', reason=REASON):
    return dw.await_device(task or _task(), {'device': device, 'reason': reason},
                           client=records, dispatch=dispatch)


def _waits(records, owner=OWNER):
    return dw.DeviceWaits(owner, client=records)


def _keys(records, namespace):
    return sorted(key for (space, key) in records.rows if space == dw.NAMESPACES[namespace])


def _only_wait(records, owner=OWNER):
    [wait] = _waits(records, owner).page(include_closed=True)['waits']
    return _waits(records, owner).read(wait['waitId'])


# ── connection epochs at the registration/online transition ───────────────

def test_registration_reports_each_online_transition_once():
    first = _connect()
    assert first['agent_id'] == 'laptop-1' and first['user_id'] == str(OWNER)
    assert first['connection_epoch'] == 1 and first['name'] == 'Laptop'
    assert _connect() is None, 'a heartbeat of a live connection is not a new epoch'
    _offline()
    again = _connect()
    assert again['connection_epoch'] == 2 and again['connected_at'] >= first['connected_at']
    assert _connect(owner=OTHER)['connection_epoch'] == 3, 'another owner is a new connection'
    assert db.online_connection('laptop-1', str(OTHER))['connection_epoch'] == 3
    assert db.online_connection('laptop-1', str(OWNER)) is None
    public = db.list_agents(user_id=str(OTHER))[0]
    assert 'connection_epoch' not in public and 'connected_at' not in public


# ── registration ──────────────────────────────────────────────────────────

def test_offline_device_registers_one_durable_wait_per_conversation(records, dispatch):
    _known_offline()
    text = _await(records, dispatch)
    assert 'Registered wait' in text and 'End your turn now' in text
    wait = _only_wait(records)
    assert wait['state'] == 'pending' and wait['conversationId'] == 'conv-a'
    assert wait['device'] == {'selector': 'Laptop', 'agentId': 'laptop-1', 'name': 'Laptop',
                              'key': dw.device_key('id', 'laptop-1')}
    assert wait['reason'] == REASON and wait['source'] == {'taskId': 'task-conv-a',
                                                          'turnId': 'turn-conv-a'}
    assert wait['wake'] == {
        'toolsConfig': {'projectPath': '/work', 'searchMode': 'off', 'codeExecEnabled': True},
        'overrides': {'desktopEnabled': True, 'schedulerEnabled': False}}
    again = _await(records, dispatch, device='laptop-1', reason='something else')
    assert 'already registered' in again and wait['waitId'] in again and REASON in again
    assert len(_keys(records, 'waits')) == 1 and len(_keys(records, 'pending')) == 1
    _await(records, dispatch, task=_task('conv-b'))
    assert len(_keys(records, 'waits')) == 2 and len(_keys(records, 'pending')) == 2
    assert dispatch.calls == []


def test_online_device_registers_nothing(records, dispatch):
    _connect()
    text = _await(records, dispatch)
    assert 'is online now' in text and 'no wait was registered' in text
    assert records.rows == {} and dispatch.calls == []


def test_ambiguous_names_and_blank_arguments_are_refused(records, dispatch):
    _known_offline('laptop-1', name='Laptop')
    _known_offline('laptop-2', name='laptop')
    refused = _await(records, dispatch)
    assert 'ambiguous_device' in refused and 'laptop-1' in refused and 'laptop-2' in refused
    assert 'invalid_arguments' in _await(records, dispatch, device='  ')
    assert 'invalid_arguments' in dw.await_device(_task(), {'device': 'Laptop'}, client=records)
    assert records.rows == {}


def test_project_dispatch_turns_and_turnless_tasks_cannot_wait(records, dispatch):
    from lib.project_dispatch.admission import KEY
    from lib.tasks_pkg.conversation_delivery import deferred_completion_refusal
    worker = _task(**{KEY: {'assignmentId': 'assignment-1'}})
    refused = _await(records, dispatch, task=worker)
    assert 'device_wait_outside_grant' in refused and 'Report the device dependency' in refused
    headless = {**_task(), '_noSavedConversation': True}
    assert 'No wait was registered' in deferred_completion_refusal(headless, 'await_device', {})
    conversationless = {**_task(), 'convId': ''}
    assert 'No wait was registered' in deferred_completion_refusal(conversationless, 'await_device', {})
    assert deferred_completion_refusal(_task(), 'await_device', {}) is None
    assert records.rows == {}


def test_a_child_invocation_is_told_to_report_the_dependency():
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.tasks_pkg.conversation_delivery import deferred_completion_refusal
    from lib.tasks_pkg.manager import discard_task
    child = create_child_invocation(
        {'id': 'parent-task', 'convId': 'parent-conv', '_userId': OWNER, '_transientRuntime': True,
         'config': {'memoryEnabled': False, 'searchMode': 'off'}},
        model='fixture-model', tools=[], objective='worker objective',
        event_sink=lambda event: None, abort_check=lambda: False)
    try:
        refusal = deferred_completion_refusal(child, 'await_device', {})
        assert 'No wait was registered' in refusal and 'so the parent can wait' in refusal
    finally:
        discard_task(child['id'])


# ── delivery ──────────────────────────────────────────────────────────────

def test_connection_wakes_once_across_heartbeats_reconnects_and_restart(records, dispatch):
    _known_offline()
    _await(records, dispatch)
    wait_id = _only_wait(records)['waitId']
    connection = _connect()
    assert connection['connection_epoch'] == 2
    assert dw.deliver_device_waits(connection, client=records, dispatch=dispatch) == [
        {'waitId': wait_id, 'outcome': 'delivered', 'disposition': 'started'}]
    [call] = dispatch.calls
    assert call['conversation_id'] == 'conv-a' and call['user_id'] == OWNER
    assert call['command_id'] == dw.wake_command_id(OWNER, wait_id)
    assert call['inject_mode'] == 'steer'
    assert call['tools_config'] == {'projectPath': '/work', 'searchMode': 'off',
                                    'codeExecEnabled': True}
    assert call['config_overrides'] == {'desktopEnabled': True, 'schedulerEnabled': False}
    message = call['message']
    assert message['role'] == 'user' and message['_initiator'] == 'proactive'
    for fact in ('nonhuman', 'not authority', 'Laptop', 'laptop-1', wait_id, REASON, 'await_device'):
        assert fact in message['content']
    delivered = _only_wait(records)
    assert delivered['state'] == 'delivered' and delivered['closeReason'] is None
    assert delivered['delivery']['connectionEpoch'] == 2
    assert delivered['delivery']['commandId'] == call['command_id']
    assert delivered['delivery']['disposition'] == 'started'
    assert _keys(records, 'pending') == []

    assert _connect() is None, 'heartbeats never reach the hook'
    _offline()
    reconnected = _connect()
    assert dw.deliver_device_waits(reconnected, client=records, dispatch=dispatch) == []
    with db.command_queue_lock:
        db._agents.clear()  # process restart: the registry is in memory, waits are durable
    restarted = _connect()
    assert restarted['connection_epoch'] == 1
    assert dw.deliver_device_waits(restarted, client=records, dispatch=dispatch) == []
    assert len(dispatch.calls) == 1


def test_waits_survive_restarts_of_the_sql_storage_authority(storage, dispatch):  # noqa: F811
    client = lambda write=False: storage.client  # noqa: E731
    _known_offline()
    assert 'Registered wait' in _await(client, dispatch)
    storage.stop()
    storage.start()
    with db.command_queue_lock:
        db._agents.clear()
    [result] = dw.deliver_device_waits(_connect(), client=client, dispatch=dispatch)
    assert result['outcome'] == 'delivered'
    storage.stop()
    storage.start()
    _offline()
    assert dw.deliver_device_waits(_connect(), client=client, dispatch=dispatch) == []
    assert len(dispatch.calls) == 1
    [wait] = _waits(client).page(include_closed=True)['waits']
    assert wait['state'] == 'delivered' and wait['delivery']['commandId'] == dispatch.calls[0]['command_id']
    assert _waits(client, OTHER).page(include_closed=True)['waits'] == []


def test_pending_wait_survives_restart_and_wakes_on_the_first_connection(records, dispatch):
    _await(records, dispatch, device='Studio Mac')  # nothing connected since the start
    with db.command_queue_lock:
        db._agents.clear()
    connection = _connect('mac-9', name='studio mac')
    [result] = dw.deliver_device_waits(connection, client=records, dispatch=dispatch)
    assert result['outcome'] == 'delivered' and len(dispatch.calls) == 1


def test_crash_after_an_accepted_wake_replays_the_same_command(records, dispatch):
    _known_offline()
    _await(records, dispatch)
    failures = []

    def lose_the_delivery_record(payload):
        if payload['value'].get('state') == 'delivered' and not failures:
            failures.append(payload['key'])
            raise StorageError('database_unavailable', 'process died before the CAS')
    records.fail_put = lose_the_delivery_record
    assert dw.deliver_device_waits(_connect(), client=records, dispatch=dispatch) == []
    assert _only_wait(records)['state'] == 'pending' and len(_keys(records, 'pending')) == 1
    _offline()
    [result] = dw.deliver_device_waits(_connect(), client=records, dispatch=dispatch)
    assert result['outcome'] == 'delivered'
    first, replay = dispatch.calls
    assert first['command_id'] == replay['command_id'], 'the turn command deduplicates the replay'


def test_unknown_selector_matches_the_device_id_or_name_when_it_connects(records, dispatch):
    by_name = _await(records, dispatch, device='Studio Mac')
    assert 'has connected since the server started' in by_name
    _await(records, dispatch, task=_task('conv-b'), device='mac-10')
    assert {wait['device']['agentId'] for wait in _waits(records).page()['waits']} == {None}
    [named] = dw.deliver_device_waits(_connect('mac-9', name='STUDIO MAC'), client=records,
                                      dispatch=dispatch)
    [addressed] = dw.deliver_device_waits(_connect('mac-10', name='Other'), client=records,
                                          dispatch=dispatch)
    assert named['outcome'] == addressed['outcome'] == 'delivered'
    assert [call['conversation_id'] for call in dispatch.calls] == ['conv-a', 'conv-b']


@pytest.mark.parametrize('disposition, state, reason, pointer_kept', [
    ('steered', 'delivered', None, False),
    ('queued', 'delivered', None, False),
    ('busy', 'pending', None, True),
    ('target_missing', 'cancelled', 'conversation_missing', False),
    ('start_failed', 'failed', 'start_failed', False),
])
def test_every_turn_command_outcome_settles_or_keeps_the_wait(records, disposition, state, reason,
                                                              pointer_kept):
    _known_offline()
    _await(records, Dispatch())
    dw.deliver_device_waits(_connect(), client=records, dispatch=Dispatch(disposition))
    wait = _only_wait(records)
    assert wait['state'] == state and wait['closeReason'] == reason
    assert bool(_keys(records, 'pending')) is pointer_kept
    if state == 'delivered':
        assert wait['delivery']['disposition'] == disposition


def test_owner_isolation_of_waits_devices_and_wakes(records, dispatch):
    _known_offline()
    _await(records, dispatch)
    wait_id = _only_wait(records)['waitId']
    # Another owner's device, even under the same agent ID and name, wakes nothing of ours.
    assert dw.deliver_device_waits(_connect(owner=OTHER), client=records, dispatch=dispatch) == []
    assert dispatch.calls == [] and _only_wait(records)['state'] == 'pending'
    other = _waits(records, OTHER)
    assert other.page(include_closed=True) == {'waits': [], 'nextCursor': None}
    with pytest.raises(dw.DeviceWaitNotFound):
        other.read(wait_id)
    with pytest.raises(dw.DeviceWaitNotFound):
        other.cancel(wait_id)
    # Another owner's online device does not make ours look online.
    assert 'Registered wait' in _await(records, dispatch, task=_task('conv-b'))
    [first, second] = dw.deliver_device_waits(_connect(owner=OWNER), client=records,
                                              dispatch=dispatch)
    assert first['outcome'] == second['outcome'] == 'delivered'
    assert {call['user_id'] for call in dispatch.calls} == {OWNER}


def test_owner_cancellation_and_conversation_deletion_stop_the_wake(records, dispatch):
    _known_offline()
    _await(records, dispatch)
    _await(records, dispatch, task=_task('conv-b'))
    waits = _waits(records)
    first, second = (wait['waitId'] for wait in waits.page()['waits'])
    cancelled = waits.cancel(first)
    assert cancelled['state'] == 'cancelled' and cancelled['closeReason'] == 'owner_cancelled'
    assert waits.cancel(first) == cancelled, 'cancellation is idempotent'
    assert waits.cancel_conversation('conv-b', reason='conversation_deleted') == [second]
    assert waits.cancel_conversation('conv-b', reason='conversation_deleted') == []
    assert _keys(records, 'pending') == []
    assert dw.deliver_device_waits(_connect(), client=records, dispatch=dispatch) == []
    assert dispatch.calls == []
    assert waits.page()['waits'] == []
    assert {wait['waitId']: (wait['state'], wait['closeReason'])
            for wait in waits.page(include_closed=True)['waits']} == {
        first: ('cancelled', 'owner_cancelled'), second: ('cancelled', 'conversation_deleted')}
    with pytest.raises(dw.DeviceWaitNotFound):
        waits.cancel('dw-unknown')


def test_the_pending_view_follows_the_pending_index_not_the_history(records, dispatch, monkeypatch):
    monkeypatch.setattr(dw, '_page_rows', lambda: 2)
    _known_offline()
    for conversation in ('conv-1', 'conv-2', 'conv-3'):
        _await(records, dispatch, task=_task(conversation))
    dw.deliver_device_waits(_connect(), client=records, dispatch=dispatch)
    _offline()
    for conversation in ('conv-4', 'conv-5', 'conv-6'):
        _await(records, dispatch, task=_task(conversation))
    waits = _waits(records)
    first = waits.page()
    assert [wait['conversationId'] for wait in first['waits']] == ['conv-4', 'conv-5']
    second = waits.page(cursor=first['nextCursor'])
    assert [wait['conversationId'] for wait in second['waits']] == ['conv-6']
    assert second['nextCursor'] is None
    history, cursor = [], None
    while True:
        page = waits.page(cursor=cursor, include_closed=True)
        history += [(wait['conversationId'], wait['state']) for wait in page['waits']]
        if (cursor := page['nextCursor']) is None:
            break
    # Wait IDs order by registration millisecond; waits of one millisecond tie randomly.
    assert sorted(history) == [('conv-1', 'delivered'), ('conv-2', 'delivered'),
                               ('conv-3', 'delivered'), ('conv-4', 'pending'),
                               ('conv-5', 'pending'), ('conv-6', 'pending')]


def test_a_delivered_wait_is_not_cancelled_and_a_new_wait_can_follow_it(records, dispatch):
    _known_offline()
    _await(records, dispatch)
    first = _only_wait(records)['waitId']
    dw.deliver_device_waits(_connect(), client=records, dispatch=dispatch)
    assert _waits(records).cancel(first)['state'] == 'delivered'
    _offline()
    assert 'Registered wait' in _await(records, dispatch)
    assert len(_waits(records).page()['waits']) == 1


def test_device_connecting_during_registration_is_woken_immediately(records, dispatch, monkeypatch):
    _known_offline()
    register = dw.DeviceWaits.register

    def connect_meanwhile(self, **kwargs):
        result = register(self, **kwargs)
        _connect()  # its own delivery ran before the pointer existed in the race
        return result
    monkeypatch.setattr(dw.DeviceWaits, 'register', connect_meanwhile)
    text = _await(records, dispatch)
    assert 'connected while the wait was being registered' in text
    [call] = dispatch.calls
    assert call['conversation_id'] == 'conv-a'
    assert _only_wait(records)['state'] == 'delivered'


def test_a_connection_between_the_pointer_and_record_writes_loses_no_wait(records, dispatch,
                                                                          monkeypatch):
    _known_offline()
    put = dw.DeviceWaits._put
    raced = []

    def connect_before_the_record_exists(self, namespace, key, value, version):
        if namespace == dw.NAMESPACES['waits'] and not raced:
            raced.extend(dw.deliver_device_waits(_connect(), client=records, dispatch=dispatch))
        return put(self, namespace, key, value, version)
    monkeypatch.setattr(dw.DeviceWaits, '_put', connect_before_the_record_exists)
    text = _await(records, dispatch)
    assert [result['outcome'] for result in raced] == ['incomplete']
    assert 'connected while the wait was being registered' in text
    [call] = dispatch.calls
    assert call['conversation_id'] == 'conv-a' and _only_wait(records)['state'] == 'delivered'


def test_an_interrupted_registration_is_completed_by_the_next_call(records, dispatch, monkeypatch):
    _known_offline()
    put = dw.DeviceWaits._put

    def crash_before_the_record(self, namespace, key, value, version):
        if namespace == dw.NAMESPACES['waits']:
            raise StorageError('database_unavailable', 'process died after the pointer')
        return put(self, namespace, key, value, version)
    with monkeypatch.context() as patch:
        patch.setattr(dw.DeviceWaits, '_put', crash_before_the_record)
        with pytest.raises(StorageError):
            _await(records, dispatch)
    assert _keys(records, 'waits') == [] and len(_keys(records, 'pending')) == 1
    _offline()
    assert [r['outcome'] for r in dw.deliver_device_waits(_connect(), client=records,
                                                          dispatch=dispatch)] == ['incomplete']
    assert len(_keys(records, 'pending')) == 1 and dispatch.calls == []
    _offline()
    assert 'Registered wait' in _await(records, dispatch)
    [result] = dw.deliver_device_waits(_connect(), client=records, dispatch=dispatch)
    assert result['outcome'] == 'delivered' and len(dispatch.calls) == 1


def test_one_failing_wait_does_not_block_the_others(records):
    _known_offline()
    _await(records, Dispatch())
    _await(records, Dispatch(), task=_task('conv-b'))

    def flaky(conversation_id, message, tools_config, **kwargs):
        if conversation_id == 'conv-a':
            raise StorageError('database_unavailable', 'conversation store restarting')
        return ScheduledTurnDispatch('started', task_id='t')
    results = dw.deliver_device_waits(_connect(), client=records, dispatch=flaky)
    assert [result['outcome'] for result in results] == ['delivered']
    states = {wait['conversationId']: wait['state']
              for wait in _waits(records).page(include_closed=True)['waits']}
    assert states == {'conv-a': 'pending', 'conv-b': 'delivered'}


def test_unparsable_bridge_owner_wakes_nothing(records, dispatch):
    assert dw.deliver_device_waits({'agent_id': 'a', 'user_id': '', 'name': '',
                                    'connection_epoch': 1, 'connected_at': 1.0},
                                   client=records, dispatch=dispatch) == []


# ── what the wake carries ─────────────────────────────────────────────────

def test_wake_keeps_the_registering_turn_choices_and_nothing_private(monkeypatch):
    from lib.project_dispatch.admission import KEY
    wake = dw.wake_capture({
        'desktopEnabled': True, 'codeExecEnabled': True, 'projectPath': '/work', 'model': 'm',
        'schedulerEnabled': True, 'autopilot': True, 'humanGuidanceEnabled': True,
        'tools': [{'type': 'function', 'function': {'name': 'caller_tool'}}],
        KEY: {'assignmentId': 'a'}, '_turnPrincipalTenantId': 'tenant', '_attended': True})
    # A list-valued ``tools`` is a caller's schema grant and is never replayed.
    assert wake == {'toolsConfig': {'projectPath': '/work', 'codeExecEnabled': True, 'model': 'm',
                                    'schedulerEnabled': True},
                    'overrides': {'desktopEnabled': True, 'schedulerEnabled': True}}
    preference = dw.wake_capture({'model': 'm', 'tools': {'toolSearch': 'auto'}})
    assert preference['toolsConfig'] == {'model': 'm', 'tools': {'toolSearch': 'auto'}}
    assert dw.wake_capture({'desktopEnabled': 'yes'})['overrides']['desktopEnabled'] is False
    assert 'project_remote' not in dw.wake_capture({'project_remote': {'agent_id': 'a', 'root': 'r'}})['overrides']
    monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
    remote = dw.wake_capture({'project_remote': {'agent_id': 'a', 'root': 'r'}})
    assert remote['overrides']['project_remote'] == {'agent_id': 'a', 'root': 'r'}


def test_a_wake_runs_as_the_registering_turn_like_a_timer_continuation(records, dispatch):
    from lib.scheduler._shared import CONTINUATION_POLICY_KEYS, continuation_policy
    route = {'model': 'route-model', 'modelRef': {'model_id': 'route-model', 'provider_id': 'p'},
             'preferredProviderId': 'p', 'thinkingDepth': 'high', 'thinkingEnabled': True,
             'systemPrompt': 'Coordinator prompt'}
    _known_offline()
    _await(records, dispatch, task=_task(**route))
    captured = _only_wait(records)['wake']['toolsConfig']
    assert {key: captured[key] for key in CONTINUATION_POLICY_KEYS if key in captured} == route
    dw.deliver_device_waits(_connect(), client=records, dispatch=dispatch)
    [call] = dispatch.calls
    assert call['config_overrides'] == {**continuation_policy(captured),
                                        'desktopEnabled': True, 'schedulerEnabled': False}
    assert call['config_overrides']['model'] == 'route-model'
    assert call['config_overrides']['routing'] == {}, 'the route is replaced as one unit'


def _recording_turn_command(monkeypatch, settings):
    from lib.scheduler import conversation_dispatch
    payloads = []

    class Commands:
        def create_turn(self, conversation, owner, payload, *, request_started_at):
            payloads.append((conversation, owner, copy.deepcopy(payload)))
            return SimpleNamespace(value={'attempt': {'taskId': 'wake-task'}})

    class Storage:
        def query(self, operation, payload):
            assert operation == 'conversation.get' and payload['user_id'] == OWNER
            return {'metadata': {'settings': settings}}
    monkeypatch.setattr(conversation_dispatch, 'conversation_turn_commands', Commands())
    monkeypatch.setattr(conversation_dispatch, 'get_storage_client', lambda: Storage())
    return payloads


def test_real_turn_dispatch_receives_one_nonhuman_steerable_wake(records, monkeypatch):
    payloads = _recording_turn_command(monkeypatch, {'model': 'conversation-model',
                                                     'projectPath': '/settings-root'})
    _known_offline()
    _await(records, Dispatch())
    wait_id = _only_wait(records)['waitId']
    [result] = dw.deliver_device_waits(_connect(), client=records)
    assert result == {'waitId': wait_id, 'outcome': 'delivered', 'disposition': 'started'}
    [(conversation, owner, payload)] = payloads
    assert (conversation, owner) == ('conv-a', OWNER)
    assert payload['commandId'] == dw.wake_command_id(OWNER, wait_id)
    assert payload['injectMode'] == 'steer' and payload['kind'] == 'scheduled_reply'
    assert payload['inputTurn']['_initiator'] == 'proactive'
    config = payload['config']
    assert config['model'] == 'conversation-model', 'the conversation keeps choosing its model'
    assert config['projectPath'] == '/work' and config['codeExecEnabled'] is True
    assert config['desktopEnabled'] is True and config['schedulerEnabled'] is False
    assert not [key for key in config if key.startswith('_')]


def test_a_wake_into_a_plan_mode_conversation_stays_pending(records, monkeypatch):
    """Plan is read-only and runs with human guidance, which a wake never gains."""
    payloads = _recording_turn_command(monkeypatch, {'model': 'conversation-model',
                                                     'planMode': True})
    _known_offline()
    _await(records, Dispatch())
    wait_id = _only_wait(records)['waitId']
    [result] = dw.deliver_device_waits(_connect(), client=records)
    assert result == {'waitId': wait_id, 'outcome': 'pending', 'disposition': 'busy'}
    assert payloads == [] and _only_wait(records)['state'] == 'pending'


def test_a_wake_into_a_worker_conversation_could_not_carry_its_grant(monkeypatch):
    """Why project Workers are denied await_device (workerDeniedTools).

    The ordinary turn command admits a nonhuman wake into an idle Worker
    conversation, but discards the assignment grant carrier: the turn would run
    as an ordinary owner turn outside the grant's tool ceiling and workspace.
    """
    import lib.conversation_sync.command_service as commands
    from lib.conversation_sync.execution_identity import restore_execution_identity
    from lib.project_dispatch.admission import KEY, is_dispatch_task
    from lib.scheduler import conversation_dispatch
    captured = []

    class Accepted(Exception):
        pass

    def accept(*args, **kwargs):
        captured.append(kwargs['config'])
        raise Accepted
    monkeypatch.setattr(commands, 'create_turn_pair', accept)
    service = commands.ConversationTurnCommandService(
        build_user_message=lambda message, *args: message,
        was_aborted_after=lambda *args: False,
        start_task=lambda *args, **kwargs: pytest.fail('no executor before durable admission'))
    _recording_turn_command(monkeypatch, {})
    monkeypatch.setattr(conversation_dispatch, 'conversation_turn_commands', service)
    grant = {'assignmentId': 'assignment-1', 'owner': OWNER, 'recipientId': 'worker-conv'}
    with pytest.raises(Accepted):
        conversation_dispatch.dispatch_scheduled_turn(
            'worker-conv', {'role': 'user', 'content': 'device online', '_initiator': 'proactive'},
            {}, user_id=OWNER, command_id='device-wait:probe', inject_mode='steer',
            config_overrides={KEY: grant, 'desktopEnabled': True})
    [config] = captured
    assert KEY not in config and not is_dispatch_task({'config': config})
    principal, _ = restore_execution_identity({**config, '_turnOwnerUserId': OWNER})
    assert principal.kind == 'user' and principal.require_owner() == OWNER


# ── tool surface, policy and hints ────────────────────────────────────────

def _context(**overrides):
    from lib.tools.registry._spec import ToolContext
    fields = dict(cfg={}, task_id='task', project_path='', project_enabled=False, search_mode='off',
                  search_enabled=False, fetch_enabled=False, code_exec_enabled=False,
                  browser_enabled=False, desktop_enabled=False, owner_user_id=OWNER, conv_id='conv-a')
    fields.update(overrides)
    return ToolContext(**fields)


def test_tool_is_offered_where_device_tools_are_and_to_routers(monkeypatch):
    from lib.tools.registry._build import _build_device_wait
    names = lambda ctx: [tool['function']['name'] for tool in _build_device_wait(ctx)]  # noqa: E731
    assert names(_context(desktop_enabled=True)) == ['await_device']
    assert names(_context()) == []
    router = _context()
    router.executable_tool_catalog.append({'type': 'function',
                                           'function': {'name': 'delegate_project_work'}})
    assert names(router) == ['await_device']
    assert names(_context(desktop_enabled=True, no_saved_conversation=True)) == []
    assert names(_context(desktop_enabled=True, completion_delivery=False)) == []
    assert names(_context(desktop_enabled=True, owner_user_id=0)) == []
    assert names(_context(desktop_enabled=True, cfg={'_storageFreeRuntime': True})) == []
    remote = {'project_remote': {'agent_id': 'laptop-1', 'root': 'src'}}
    assert names(_context(cfg=remote)) == []
    monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
    assert names(_context(cfg=remote)) == ['await_device']


def test_spec_policy_ceilings_and_approval_metadata():
    from lib.project_dispatch.repository import CONTRACT as DISPATCH
    from lib.router.policy import ROUTER_TOOLS
    from lib.tasks_pkg.coordinator_policy import COORDINATOR_TOOLS
    from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS
    from lib.tools.registry import all_specs
    [spec] = [spec for spec in all_specs() if 'await_device' in spec.provides]
    assert spec.key == 'device_wait' and spec.phase == 'capability'
    assert 'await_device' in spec.write_tools and 'await_device' in spec.idempotent_tools
    assert 'await_device' in COORDINATOR_TOOLS and 'await_device' in ROUTER_TOOLS
    assert 'await_device' in DISPATCH['workerDeniedTools']
    assert 'await_device' not in DISPATCH['continuationTools']
    meta = {}
    _APPROVAL_META_ENRICHERS['await_device'](meta, {'device': 'Laptop', 'reason': REASON})
    assert [row['label'] for row in meta['riskFields']] == ['Device', 'Reason']


def test_offline_device_errors_name_await_device_only_when_the_task_can_wait(monkeypatch):
    import lib.desktop
    from lib.project_dispatch.admission import KEY
    from lib.tasks_pkg.handlers.misc import _agents
    from lib.tasks_pkg.handlers.project import _remote_error_text
    task = _task()
    assert 'call await_device' in dw.offline_hint(task)
    assert "'laptop-1' is offline" in dw.offline_hint(task, 'laptop-1')
    assert dw.offline_hint({**task, 'convId': ''}) == ''
    assert dw.offline_hint(_task(**{KEY: {'assignmentId': 'a'}})) == ''
    monkeypatch.setattr(lib.desktop, 'send_desktop_command',
                        lambda *args, **kwargs: (None, 'Desktop command cancelled'))
    assert 'call await_device' in _agents._run_desktop('desktop_list_files', {},
                                                       user_id=str(OWNER), task=task)
    assert _agents._run_desktop('desktop_list_files', {}, user_id=str(OWNER)) == (
        'Desktop Agent Error: Desktop command cancelled')
    remote = {'root': 'src', 'agent_id': 'laptop-1'}
    assert "'laptop-1' is offline" in _remote_error_text(task, remote, 'not online')
    _connect()
    assert dw.offline_hint(task) == dw.offline_hint(task, 'laptop-1') == ''
    assert _remote_error_text(task, remote, 'boom') == 'Error: remote worktree src: boom'


# ── poll route hook ───────────────────────────────────────────────────────

@pytest.mark.api
def test_poll_route_delivers_once_per_connection_epoch(flask_client, monkeypatch):
    from lib.bridge_auth import process_agent_token
    monkeypatch.setattr(db, 'POLL_WAIT_TIMEOUT', 0.1)
    seen = []
    monkeypatch.setattr(dw, 'deliver_device_waits', lambda connection: seen.append(connection) or [])

    def poll():
        response = flask_client.post('/api/desktop/poll', json={
            'results': [], 'agent': {'agent_id': 'agent-A', 'name': 'mac'},
        }, headers={'X-Bridge-Secret': process_agent_token()})
        assert response.status_code == 200
    poll()
    poll()
    assert [connection['connection_epoch'] for connection in seen] == [1]
    _offline('agent-A')
    poll()
    assert [connection['connection_epoch'] for connection in seen] == [1, 2]
    assert seen[0]['agent_id'] == 'agent-A' and seen[0]['name'] == 'mac'

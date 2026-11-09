"""Coordinators inspect live sessions and use the same commands as their owner."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.identity import PrincipalContext
from lib.conversation_sync.execution_identity import capture_execution_identity
from tests.project_mod.test_coordinator_workers import runtime as worker_runtime, base_runtime, context

__all__ = ('worker_runtime', 'base_runtime', 'context')

pytestmark = pytest.mark.unit


@pytest.fixture
def sessions(monkeypatch):
    from lib.conversations import session_control
    from lib.conversations import repository, project_coordination
    from lib.project_dispatch import delegation
    from lib.router import policy
    from lib.router.watchdog import binding
    from lib.conversation_sync import runtime
    from lib import turn_lifecycle

    principal = PrincipalContext.user(subject_id='owner', owner_user_id=7, tenant_id='tenant')
    scope = dict(user_id=7, project_path='/project', conversation_id='coordinator', binding_revision=1)
    turn = dict(conversationId='session', turnId='answer', currentAttemptId='original',
                actor='assistant', status='interrupted', projectionRevision=4,
                settlement={'cause': 'process_exit', 'resumeOptions': [
                    {'operation': 'continue', 'anchor': {'type': 'output_tail'}}]})
    attempt = dict(conversationId='session', turnId='answer', attemptId='original',
                   status='interrupted', config=capture_execution_identity(
                       {'model': 'target-model', 'thinkingEnabled': True}, 7, principal))
    state = SimpleNamespace(scope=scope, turn=turn, attempts={'original': attempt}, calls=[],
                            metadata={'id': 'session', 'settings': {'projectPath': '/project'}},
                            principal=principal, source_error=None, global_router=False)
    state.task = {'id': 'coordinator-task', 'convId': 'coordinator', '_userId': 7,
                  '_principalContext': principal.to_payload(), 'config': {'projectPath': '/project'}}
    monkeypatch.setattr(policy, 'is_router_scope', lambda scope: state.global_router)
    monkeypatch.setattr(binding, 'is_watchdog_scope', lambda scope: False)
    monkeypatch.setattr(repository, 'ConversationRepository', lambda *args, **kwargs: SimpleNamespace(
        get=lambda conv, **kw: deepcopy(state.metadata) if conv == 'session' and kw['user_id'] == 7 else None))
    monkeypatch.setattr(turn_lifecycle, 'get_turn', lambda conv, turn, **kw: deepcopy(state.turn))
    monkeypatch.setattr(turn_lifecycle, 'get_attempt', lambda attempt, **kw: deepcopy(state.attempts[attempt]))
    monkeypatch.setattr(project_coordination, 'ProjectCoordinationRepository', lambda owner, **kwargs: SimpleNamespace(
        read=lambda project: {'availability': 'available', 'conversationId': 'coordinator', 'revision': 1}))

    def source(*args, **kwargs):
        if state.source_error:
            raise PermissionError(state.source_error)
        return state.principal, {'conversationId': 'coordinator', 'turnId': 'human'}, {}
    monkeypatch.setattr(delegation, 'source_for_task', source)

    class Commands:
        def create_attempt(self, conv, turn, owner, body, **kwargs):
            state.calls.append((conv, turn, owner, deepcopy(body), kwargs))
            return SimpleNamespace(value={'conversationId': conv, 'turn': deepcopy(state.turn),
                'attempt': {'attemptId': 'resumed', 'status': 'pending'}, 'idempotentReplay': False})

        def abort_attempt(self, attempt, owner):
            state.calls.append(('stop', attempt, owner))
            return SimpleNamespace(value={'attemptId': attempt, 'status': 'interrupted'})

    monkeypatch.setattr(runtime, 'conversation_turn_commands', Commands())
    state.args = {'conversation_id': 'session', 'turn_id': 'answer', 'attempt_id': 'original',
                  'action': 'continue', 'expected_revision': 4, 'idempotency_key': 'resume-once'}
    state.control = lambda **changes: session_control.control_session(
        state.task, state.scope, {**state.args, **changes})
    return state


def test_continue_uses_target_config_and_shared_command_with_stable_identity(sessions):
    s = sessions
    first = s.control()
    s.control()
    conv, turn, owner, body, kwargs = s.calls[0]
    assert (conv, turn, owner) == ('session', 'answer', 7)
    assert body['operation'] == 'continue' and body['expectedProjectionRevision'] == 4
    assert body['config'] == {'model': 'target-model', 'thinkingEnabled': True}
    assert kwargs['principal'] == s.principal
    assert body['commandId'] == s.calls[1][3]['commandId']
    assert first['attempt']['attemptId'] == 'resumed'


def test_lost_ack_replay_uses_original_attempt_config(sessions):
    s = sessions
    first = s.control()
    s.turn['currentAttemptId'] = 'resumed'
    s.attempts['resumed'] = {**s.attempts['original'], 'attemptId': 'resumed',
        'commandId': first['command_id'], 'config': {'model': 'changed-after-acceptance'}}
    s.control()
    assert s.calls[0][3] == s.calls[1][3]


def test_resume_anchor_key_order_does_not_change_command_identity(sessions):
    first = sessions.control(resume_anchor={'type': 'replay_only', 'contentChars': 0})
    second = sessions.control(resume_anchor={'contentChars': 0, 'type': 'replay_only'})
    assert first['command_id'] == second['command_id']


@pytest.mark.parametrize('violation', ['foreign', 'project', 'tenant', 'read_only', 'source', 'attempt'])
def test_session_control_refuses_before_mutation(sessions, violation):
    s = sessions
    if violation == 'foreign': s.metadata = None
    if violation == 'project': s.metadata['settings']['projectPath'] = '/other'
    if violation == 'tenant':
        s.principal = PrincipalContext.user(subject_id='other', owner_user_id=7, tenant_id='other')
    if violation == 'read_only': s.metadata['settings']['readOnly'] = True
    if violation == 'source': s.source_error = 'Synthetic input cannot authorize session control'
    if violation == 'attempt': s.attempts['original']['conversationId'] = 'other'
    with pytest.raises((PermissionError, ValueError)):
        s.control()
    assert not s.calls


def test_global_router_can_control_owned_session_in_another_project(sessions):
    sessions.global_router = True
    sessions.metadata['settings']['projectPath'] = '/other'
    sessions.control()
    assert len(sessions.calls) == 1


def test_stop_names_exact_attempt_and_does_not_require_streaming_revision(sessions):
    sessions.turn['projectionRevision'] = 99
    sessions.attempts['original']['status'] = 'running'
    sessions.control(action='stop', expected_revision=None)
    assert sessions.calls == [('stop', 'original', 7)]


def test_repeated_stop_of_terminal_attempt_cannot_withdraw_later_work(sessions):
    result = sessions.control(action='stop')
    assert result['receipt']['alreadyTerminal'] is True
    assert not sessions.calls


def test_stale_stop_never_stops_a_successor(sessions):
    sessions.turn['currentAttemptId'] = 'newer'
    with pytest.raises((ValueError, PermissionError)):
        sessions.control(action='stop')
    assert not sessions.calls


def test_status_projects_authoritative_state_without_private_config(sessions, monkeypatch):
    from lib.conversations.session_control import inspect_session
    from lib.conversation_sync import service
    from lib import message_queue, turn_lifecycle
    from lib.turn_lifecycle import LifecycleNotFound
    page = {'syncSeq': 25, 'totalTurns': 1, 'hasMore': False, 'nextBeforeOrdinal': None,
            'turns': [{**sessions.turn, 'ordinal': 3}], 'attempts': [sessions.attempts['original']]}
    pages = []

    def turn_page(conv, owner, **kwargs):
        pages.append((conv, owner, kwargs))
        return deepcopy(page)
    monkeypatch.setattr(service, 'ConversationSyncService', lambda repository: SimpleNamespace(
        turn_page=turn_page))
    queued = [{'queueId': 'q1', 'position': 0, 'kind': 'real', 'text': 'Then deploy'}]
    monkeypatch.setattr(message_queue, 'get_queue', lambda conv, *, user_id: deepcopy(queued))
    result = inspect_session(sessions.scope, {'conversation_id': 'session'})
    assert [(conv, owner, kwargs['lane_id'], kwargs['before_ordinal']) for conv, owner, kwargs in pages] == [
        ('session', 7, 'main', None)]
    assert result['turns'][0]['status'] == 'interrupted'
    assert result['turns'][0]['settlement']['resumeOptions'][0]['operation'] == 'continue'
    assert result['window'] == {'lane_id': 'main', 'total_turns': 1, 'next_before_ordinal': None}
    assert result['queue_items'] == queued
    assert 'config' not in result['attempts'][0]
    assert not sessions.calls
    focused = inspect_session(sessions.scope, {'conversation_id': 'session', 'turn_id': 'answer'})
    assert focused['turns'] == result['turns'] and focused['attempts'] == result['attempts']
    assert 'window' not in focused and len(pages) == 1

    def get_turn(conv, turn_id, **kwargs):
        if turn_id != 'answer':
            raise LifecycleNotFound('Turn not found')
        return deepcopy(sessions.turn)
    monkeypatch.setattr(turn_lifecycle, 'get_turn', get_turn)
    with pytest.raises(LifecycleNotFound):
        inspect_session(sessions.scope, {'conversation_id': 'session', 'turn_id': 'missing'})
    for cursor in ({'before_ordinal': -1}, {'before_ordinal': True}, {'lane_id': ''}):
        with pytest.raises(ValueError):
            inspect_session(sessions.scope, {'conversation_id': 'session', **cursor})


@pytest.mark.parametrize('revoked', [False, True])
def test_dispatched_session_retains_exact_grant(worker_runtime, monkeypatch, revoked):
    from lib.project_dispatch.admission import TrustedDispatch
    from lib import turn_lifecycle
    from lib.conversation_sync import runtime

    r = worker_runtime
    worker = r.call('delegate_project_work', **r.args)
    assert 'error' not in worker, worker
    row = r.c.repo.read(worker['assignment_id'])
    trusted = TrustedDispatch(r.c.repo.admission(row['id']))
    attempt_id = row['execution']['attemptId']
    turn_id = row['execution']['turnId']
    r.attempts[attempt_id].update(status='interrupted', config=trusted.capture(
        {}, 7, trusted.principal, worker['conversation_id']))
    r.turns[turn_id].update(currentAttemptId=attempt_id, projectionRevision=3, status='interrupted')
    monkeypatch.setattr(turn_lifecycle, 'get_turn', r.c.repo.turn_get)
    monkeypatch.setattr(turn_lifecycle, 'get_attempt', r.c.repo.attempt_get)
    captured = []

    class Commands:
        def create_attempt(self, conv, turn, owner, body, **kwargs):
            admission = kwargs['trusted_dispatch']
            assert isinstance(admission, TrustedDispatch)
            assert admission.checked()['id'] == row['id']
            assert kwargs['principal'].kind == 'system'
            assert body['config'] == {}
            captured.append(admission.binding)
            return SimpleNamespace(value={'turn': r.turns[turn_id], 'attempt': r.attempts[attempt_id]})

    monkeypatch.setattr(runtime, 'conversation_turn_commands', Commands())
    if revoked:
        r.c.repo.revoke(row['id'], row['grant']['revision'])
    result = r.call('control_conversation', conversation_id=worker['conversation_id'],
        turn_id=turn_id, attempt_id=attempt_id, action='continue', expected_revision=3,
        idempotency_key='resume-worker')
    assert ('error' in result) is revoked, result
    assert len(captured) == (0 if revoked else 1)


def test_follow_up_refuses_an_ordinary_session_before_any_command(sessions):
    from lib.project_dispatch.service import FollowUpRefused
    s = sessions
    with pytest.raises(ValueError, match='instruction'):
        s.control(action='follow_up')
    with pytest.raises(FollowUpRefused, match='Worker session a Coordinator dispatched'):
        s.control(action='follow_up', instruction='Also summarize the result.')
    assert not s.calls


def test_the_owner_delegation_never_follows_up(sessions):
    from lib.router.owner_delegation import delegated_conversation_control
    s = sessions
    s.global_router = True
    with pytest.raises(ValueError, match='follow_up'):
        delegated_conversation_control(s.task, s.scope, {
            **s.args, 'action': 'follow_up', 'instruction': 'Also summarize the result.',
            'owner_delegation': {'reason': 'A standing instruction', 'instruction_id': 'ri-1',
                                 'event_id': 'event-1'}}, control=lambda *a, **kw: pytest.fail('ran'))
    assert not s.calls


def _ended_worker(r, monkeypatch):
    """A delegated Worker whose bound Turn completed, read through the real repository."""
    from lib.project_dispatch.admission import TrustedDispatch
    from lib import turn_lifecycle
    worker = r.call('delegate_project_work', **r.args)
    row = r.c.repo.read(worker['assignment_id'])
    execution = row['execution']
    trusted = TrustedDispatch(r.c.repo.admission(row['id']))
    r.attempts[execution['attemptId']].update(status='completed', config=trusted.capture(
        {}, 7, trusted.principal, worker['conversation_id']))
    r.turns[execution['turnId']].update(currentAttemptId=execution['attemptId'], projectionRevision=3,
                                        status='completed')
    monkeypatch.setattr(turn_lifecycle, 'get_turn', r.c.repo.turn_get)
    monkeypatch.setattr(turn_lifecycle, 'get_attempt', r.c.repo.attempt_get)
    monkeypatch.setattr(r.c.repo, 'workspace_get', lambda *args, **kwargs: None)
    return worker, row


def test_follow_up_of_an_ended_worker_creates_its_next_turn_never_an_attempt(worker_runtime, monkeypatch):
    from lib.conversation_sync import runtime
    from lib.project_dispatch.admission import FOLLOW_UP_KEY
    r = worker_runtime
    worker, row = _ended_worker(r, monkeypatch)
    execution = row['execution']
    created = []

    class Commands:
        def create_turn(self, conv, owner, body, **kwargs):
            trusted = kwargs['trusted_dispatch']
            created.append((conv, deepcopy(body), kwargs['principal'], trusted.follows,
                            trusted.capture({}, owner, kwargs['principal'], conv)))
            r.turns['follow-up-turn'] = {'conversationId': conv, 'turnId': 'follow-up-turn',
                                         'actor': 'assistant', 'projectionRevision': 1}
            r.attempts['follow-up-attempt'] = {'conversationId': conv, 'turnId': 'follow-up-turn',
                'attemptId': 'follow-up-attempt', 'taskId': '', 'status': 'pending',
                'commandId': body['commandId']}
            trusted.continue_execution('follow-up-turn', 'follow-up-attempt')
            return SimpleNamespace(value={'turn': r.turns['follow-up-turn'],
                                          'attempt': r.attempts['follow-up-attempt']})

        def create_attempt(self, *args, **kwargs):
            pytest.fail('A follow-up never regenerates or resumes the ended Turn')

    r.service.commands = Commands()
    monkeypatch.setattr(runtime, 'conversation_turn_commands', Commands())
    args = dict(conversation_id=worker['conversation_id'], turn_id=execution['turnId'],
                attempt_id=execution['attemptId'], action='follow_up', expected_revision=3,
                instruction='Also add the bot when a Router group is created.', idempotency_key='next')
    result = r.call('control_conversation', **args)
    assert 'error' not in result, result
    assert result['assignment_id'] == row['id'] and result['turn']['turnId'] == 'follow-up-turn'
    [(conv, body, principal, follows, config)] = created
    assert conv == worker['conversation_id'] and principal.kind == 'system'
    assert body['inputActor'] == 'virtual_user' and body['config'] == {}
    assert body['parentTurnId'] == execution['turnId'] and body['requireParentIsLaneTail'] is True
    assert args['instruction'] in body['inputTurn']['content']
    assert body['inputTurn']['_projectAssignmentId'] == row['id']
    assert follows['execution'] == execution
    assert config[FOLLOW_UP_KEY] == {'attemptId': execution['attemptId'], 'turnId': execution['turnId'],
                                     'taskId': r.attempts[execution['attemptId']]['taskId'],
                                     'status': 'completed'}
    assert r.c.repo.read(row['id'])['execution']['attemptId'] == 'follow-up-attempt'
    # A lost ACK replays the launched follow-up; a changed instruction cannot start another.
    r.attempts['follow-up-attempt']['taskId'] = 'conversation-attempt:follow-up-attempt'
    replay = r.call('control_conversation', **args)
    assert replay['idempotent_replay'] and replay['attempt']['attemptId'] == 'follow-up-attempt', replay
    changed = r.call('control_conversation', **{**args, 'instruction': 'Do something else.'})
    assert changed['code'] == 'follow_up_refused' and changed['execution_started'] is False, changed
    assert len(created) == 1


def test_follow_up_refuses_another_coordinators_worker(worker_runtime, monkeypatch):
    r = worker_runtime
    worker, row = _ended_worker(r, monkeypatch)
    r.c.conversations.rows['other-coordinator'] = {'user_id': 7, 'title': 'other',
                                                   'settings': {'projectPath': '/project'}}
    monkeypatch.setattr(r.c.repo, 'read', lambda assignment_id: {
        **row, 'request': {**row['request'], 'coordinatorId': 'other-coordinator'}})
    refused = r.call('control_conversation', conversation_id=worker['conversation_id'],
                     turn_id=row['execution']['turnId'], attempt_id=row['execution']['attemptId'],
                     action='follow_up', expected_revision=3, instruction='Also do this.',
                     idempotency_key='foreign')
    assert 'another Coordinator' in refused['error'], refused

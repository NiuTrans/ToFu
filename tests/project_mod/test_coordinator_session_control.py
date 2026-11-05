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
    snapshot = {'conversationRevision': 12, 'syncSeq': 25, 'turns': [sessions.turn],
                'attempts': [sessions.attempts['original']], 'queueItems': [],
                'activeAttemptPhase': {'phase': 'waiting_for_human'}}
    monkeypatch.setattr(service, 'ConversationSyncService', lambda repository: SimpleNamespace(
        snapshot=lambda conv, owner, **kw: deepcopy(snapshot)))
    result = inspect_session(sessions.scope, {'conversation_id': 'session'})
    assert result['conversation_revision'] == 12
    assert result['turns'][0]['status'] == 'interrupted'
    assert result['turns'][0]['settlement']['resumeOptions'][0]['operation'] == 'continue'
    assert result['active_attempt_phase'] == snapshot['activeAttemptPhase']
    assert 'config' not in result['attempts'][0]
    assert not sessions.calls
    focused = inspect_session(sessions.scope, {'conversation_id': 'session', 'turn_id': 'answer'})
    assert focused['turns'] == result['turns']
    from lib.turn_lifecycle import LifecycleNotFound
    with pytest.raises(LifecycleNotFound):
        inspect_session(sessions.scope, {'conversation_id': 'session', 'turn_id': 'missing'})


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

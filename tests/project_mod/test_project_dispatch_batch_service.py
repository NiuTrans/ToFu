"""Human-approved fan-out uses existing independent turn commands, no providers."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.identity import PrincipalContext
from lib.project_dispatch import admission
from lib.project_dispatch.repository import record_stop
from lib.project_dispatch.service import ProjectDispatchService
from lib.turn_lifecycle import LifecycleConflict
from tests.project_mod.test_project_dispatch_repository import context as context

pytestmark = pytest.mark.unit


@pytest.fixture
def batch_runtime(context, monkeypatch):
    c = context
    c.conversations.rows['second'] = deepcopy(c.conversations.rows['recipient'])
    body = {'projectPath': '/project', 'coordinatorId': 'coordinator',
            'authorizationSource': deepcopy(c.request['authorizationSource']),
            'objective': 'Optimize Tofu with independent reliability and performance evidence.',
            'budget': {'maxTokens': 200, 'maxModelRequests': 4, 'deadline': 2000},
            'workers': [{key: deepcopy(c.request[key]) for key in ('recipientId', 'brief', 'model', 'budget')},
                        {**{key: deepcopy(c.request[key]) for key in ('brief', 'model', 'budget')},
                         'recipientId': 'second'}]}
    state = SimpleNamespace(c=c, body=body, calls=[], births={}, turns={}, attempts={},
                            busy=set(), lost=set(), after=None)
    monkeypatch.setattr(admission, 'repository_for', lambda binding: c.repo)

    class Commands:
        def create_turn(self, conv, owner, request, **kwargs):
            assert owner == 7 and kwargs['principal'].kind == 'system'
            assert kwargs['principal'].tenant_id == 'tenant-a'
            assert request['inputActor'] == 'virtual_user'
            assert request['inputTurn']['_initiator'] == 'brain'
            # All grants, not just the first, must be committed before birth.
            assert len(c.repo.list()) == len(body['workers'])
            trusted = kwargs['trusted_dispatch']
            trusted.checked()
            state.calls.append((conv, request['commandId']))
            if conv in state.busy:
                raise LifecycleConflict('superseded_by_human', 'Human input has priority')
            command = request['commandId']
            if command not in state.births:
                turn_id, attempt_id = 'turn-' + conv, 'attempt-' + conv
                trusted.bind_execution(turn_id, attempt_id)
                state.births[command] = conv
                state.turns[turn_id] = {'turnId': turn_id, 'conversationId': conv,
                    'actor': 'assistant', 'projectionRevision': 1, 'projection': {'content': ''}}
                state.attempts[attempt_id] = {'attemptId': attempt_id, 'turnId': turn_id,
                    'conversationId': conv, 'taskId': 'task-' + conv, 'status': 'running'}
            if state.after:
                state.after(conv)
            if conv in state.lost:
                state.lost.remove(conv)
                raise TimeoutError('ACK lost after executor birth')
            return SimpleNamespace(value={})

    state.service = ProjectDispatchService(c.repo, commands=Commands(),
        turn_get=lambda conv, turn, **kw: deepcopy(state.turns.get(turn)),
        attempt_get=lambda attempt, **kw: deepcopy(state.attempts.get(attempt)))
    return state


def test_batch_fans_out_without_waiting_for_worker_results(batch_runtime):
    r = batch_runtime
    result = r.service.authorize_batch_and_dispatch(r.body, principal=r.c.principal, command_id='fan-out')
    assert [row['status'] for row in result['dispatches']] == ['accepted', 'accepted']
    assert len(r.births) == 2
    assert [row['execution']['status'] for row in result['assignments']] == ['running', 'running']
    assert all(not row['canonicalDelivery'] and row['review'] is None for row in result['assignments'])
    assert all(r.body['objective'] in row['request']['brief'] for row in result['assignments'])
    r.service.dispatch_batch(result['batchId'], principal=r.c.principal)
    assert len(r.births) == 2 and len({command for _, command in r.calls}) == 2


def test_busy_human_lane_does_not_starve_other_approved_worker(batch_runtime):
    r = batch_runtime; r.busy.add('recipient')
    result = r.service.authorize_batch_and_dispatch(r.body, principal=r.c.principal, command_id='busy')
    assert [row['status'] for row in result['dispatches']] == ['deferred', 'accepted']
    assert result['assignments'][0]['execution'] is None
    r.busy.clear()
    r.service.dispatch_batch(result['batchId'], principal=r.c.principal)
    assert len(r.births) == 2


def test_lost_launch_ack_keeps_original_batch_and_never_buys_second_execution(batch_runtime):
    r = batch_runtime; r.lost.add('recipient')
    result = r.service.authorize_batch_and_dispatch(r.body, principal=r.c.principal, command_id='lost')
    assert [row['status'] for row in result['dispatches']] == ['unknown', 'accepted']
    assert result['dispatches'][0]['reason'] == 'dispatch_unconfirmed'
    assert result['assignments'][0]['execution']['taskId'] == 'task-recipient'
    assert len(r.births) == 2
    receipt = r.service.batch_receipt(principal=r.c.principal, command_id='lost')
    assert 'dispatches' not in receipt  # No invented persisted acceptance ACK.
    assert receipt['batchId'] == result['batchId']
    replay = r.service.authorize_batch_and_dispatch(r.body, principal=r.c.principal, command_id='lost')
    assert replay['batchId'] == result['batchId'] and len(r.births) == 2
    assert len(r.c.repo.list()) == 2


def test_bad_last_worker_mints_nothing_and_launches_nothing(batch_runtime):
    r = batch_runtime
    r.body['workers'][-1]['recipientId'] = 'missing'
    with pytest.raises(ValueError):
        r.service.authorize_batch_and_dispatch(r.body, principal=r.c.principal, command_id='invalid')
    assert not r.c.client.rows and not r.calls


def test_all_rework_preflights_precede_atomic_authorization(batch_runtime, monkeypatch):
    r = batch_runtime
    r.body['workers'][-1]['reworkOf'] = {'assignmentId': 'old', 'grantRevision': 1, 'resultRevision': 2}
    monkeypatch.setattr(r.service, 'view', lambda *a, **kw: {'grantRevision': 1, 'review': None})
    with pytest.raises(ValueError, match='rework'):
        r.service.authorize_batch_and_dispatch(r.body, principal=r.c.principal, command_id='rework')
    assert not r.c.client.rows and not r.calls


def test_get_reconciliation_is_read_only_even_after_stop_and_expiry(batch_runtime):
    r = batch_runtime
    result = r.service.authorize_batch_and_dispatch(r.body, principal=r.c.principal, command_id='read')
    record_stop(7, 'coordinator', 1001, client=r.c.client)
    r.c.now[0] = 3000
    writes, calls = r.c.client.puts, len(r.calls)
    receipt = r.service.batch_receipt(principal=r.c.principal, command_id='read')
    assert receipt['batchId'] == result['batchId']
    assert r.c.client.puts == writes and len(r.calls) == calls
    assert r.service.batch_receipt(principal=r.c.principal, command_id='missing') is None
    assert r.c.client.puts == writes and len(r.calls) == calls


def test_stop_between_births_prevents_second_worker_but_preserves_evidence(batch_runtime):
    r = batch_runtime
    r.after = lambda conv: record_stop(7, 'coordinator', 1001, client=r.c.client)
    result = r.service.authorize_batch_and_dispatch(r.body, principal=r.c.principal, command_id='stop')
    assert [row['status'] for row in result['dispatches']] == ['accepted', 'unknown']
    assert len(r.births) == 1 and len(r.c.repo.list()) == 2
    assert result['assignments'][1]['execution'] is None
    r.service.dispatch_batch(result['batchId'], principal=r.c.principal)
    assert len(r.births) == 1


@pytest.mark.parametrize('change', ['owner', 'tenant', 'system', 'scope'])
def test_other_principal_cannot_dispatch_or_read_batch(batch_runtime, change):
    r = batch_runtime
    result = r.service.authorize_batch_and_dispatch(r.body, principal=r.c.principal, command_id='private')
    principal = (PrincipalContext.system(subject_id='system', owner_user_id=7,
                 tenant_id='tenant-a', scopes={'chat'}) if change == 'system' else
                 PrincipalContext.user(subject_id='other', owner_user_id=8 if change == 'owner' else 7,
                 tenant_id='other' if change == 'tenant' else 'tenant-a',
                 scopes=set() if change == 'scope' else {'chat'}))
    count = len(r.calls)
    with pytest.raises(PermissionError):
        r.service.dispatch_batch(result['batchId'], principal=principal)
    with pytest.raises(PermissionError):
        r.service.view_batch(result['batchId'], principal=principal)
    if change == 'tenant':
        assert r.service.batch_receipt(principal=principal, command_id='private') is None
    else:
        with pytest.raises(PermissionError):
            r.service.batch_receipt(principal=principal, command_id='private')
    assert len(r.calls) == count

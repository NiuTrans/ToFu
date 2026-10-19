"""Scope-removal specs: fake CAS, real passive originals, no network/models."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from threading import Barrier
from types import SimpleNamespace
import pytest
from lib.conversations.project_messages import ProjectMailbox
from lib.identity import PrincipalContext
from lib.project_dispatch.repository import LIMITS as DISPATCH_LIMITS, NAMESPACE, ProjectDispatchError, record_stop
from lib.project_dispatch.scope_changes import LIMITS, ProjectScopeChanges, schema, validate
from lib.storage.errors import StorageError
from tests.project_mod import test_project_dispatch_repository as context_owner

context_fixture = context_owner.context
pytestmark = pytest.mark.unit

@pytest.fixture
def context(context_fixture):
    c = context_fixture
    c.attempts, c.cancel_calls = {}, []
    c.fail_cancels, c.cancel_ack_lost, c.fail_mail = set(), set(), set()
    c.signal_only = False
    c.repo.attempt_get = lambda identity, *, user_id: deepcopy(c.attempts.get(identity))
    def cancel(identity, owner):
        assert owner == 7
        c.cancel_calls.append(identity)
        if identity in c.fail_cancels:
            raise RuntimeError('injected cancellation unavailable')
        if not c.signal_only:
            c.attempts[identity]['status'] = 'interrupted'
        if identity in c.cancel_ack_lost:
            c.cancel_ack_lost.remove(identity)
            raise RuntimeError('lost ACK after terminal commit')
        return SimpleNamespace(value={'attemptId': identity, 'status': 'abort_signaled'})
    def mailbox(owner, project, recipient):
        if recipient in c.fail_mail:
            raise RuntimeError('injected mailbox unavailable')
        return ProjectMailbox(owner, project, recipient, client=c.client)
    c.app = ProjectScopeChanges(c.repo, commands=SimpleNamespace(abort_project_attempt=cancel), mailbox_factory=mailbox, publication_lock=lambda project: nullcontext())
    return c

def assignment(c, name, *, recipient='recipient', bound=True):
    c.conversations.rows.setdefault(recipient, {'user_id': 7, 'settings': {'projectPath': '/project'}})
    a = c.repo.authorize({**deepcopy(c.request), 'recipientId': recipient}, c.principal, name)
    if bound:
        a = c.repo.bind_execution(a['id'], 'turn-' + name, 'attempt-' + name)
        c.attempts['attempt-' + name] = {'user_id': 7, 'attemptId': 'attempt-' + name, 'turnId': 'turn-' + name, 'conversationId': recipient, 'status': 'running'}
    return a

def request(c, *rows, kind='shrink'):
    return {'projectPath': '/project', 'coordinatorId': 'coordinator', 'authorizationSource': deepcopy(c.request['authorizationSource']), 'kind': kind, 'note': 'Remove only these grants', 'targets': [{'assignmentId': a['id'], 'grantRevision': a['grant']['revision']} for a in rows]}

def create(c, body, command='change'):
    return c.app.create(body, principal=c.principal, command_id=command)


def test_atomic_revocation_originals_results_and_unrelated_task_untouched(context):
    c = context
    a, b, unrelated = assignment(c, 'a'), assignment(c, 'b', recipient='worker-b'), assignment(c, 'unrelated')
    c.repo.record_review(a['id'], 'accept', 'retain completed evidence', 2, 1)
    before = {r['id']: deepcopy(r) for r in c.repo.list()}
    observed, command = [], c.client.command
    def inspect(operation, args, identity):
        if args['namespace'] == NAMESPACE and args['value'].get('scopeChanges'):
            selected = [r for r in args['value']['assignments'] if r['id'] in (a['id'], b['id'])]
            observed.append(all(r['grant']['revoked'] for r in selected))
        return command(operation, args, identity)
    c.client.command = inspect
    view = create(c, request(c, a, b))
    assert observed and all(observed)
    assert c.repo.read(unrelated['id']) == unrelated
    assert set(c.cancel_calls) == {'attempt-a', 'attempt-b'}
    assert c.attempts['attempt-unrelated']['status'] == 'running'
    for original in (a, b):
        expected = before[original['id']]
        expected['grant'].update(revoked=True, revision=2)
        assert c.repo.read(original['id']) == expected
    assert view['authorityRevoked'] and view['stopConfirmed']
    assert all(n['messageEvidence']['state'] == 'submitted' and n['messageEvidence']['decision'] is None for n in view['notifications'])
    validate(view, 'view')
    mailbox = ProjectMailbox(7, '/project', 'recipient', client=c.client)
    original = mailbox.load()[0]['messages'][0]
    mailbox.confirm([original['id']], 'task', 1, turn_id='turn-a', attempt_id='attempt-a')
    mailbox.consider({'message_id': original['id'], 'expected_revision': 2, 'decision': 'accepted', 'note': 'Observed'}, 'task')
    refreshed = c.app.view(view['changeId'], principal=c.principal)
    n = next(n for n in refreshed['notifications'] if n['recipientId'] == 'recipient')
    assert n['messageEvidence'] == {'id': original['id'], 'revision': 3, 'state': 'input_confirmed', 'decision': 'accepted'}
    assert mailbox.load()[0]['messages'][0]['content'] == original['content']

def test_worker_genuine_human_source(context):
    c = context
    a = assignment(c, 'a')
    body = request(c, a)
    body['authorizationSource']['conversationId'] = 'recipient'
    c.turn['conversationId'] = 'recipient'
    view = create(c, body)
    assert [n['recipientId'] for n in view['notifications']] == ['coordinator']
    assert view['notifications'][0]['messageEvidence']['decision'] is None

@pytest.mark.parametrize('mutation', [
    lambda c, b: c.turn.update(actor='virtual_user'),
    lambda c, b: c.turn['projection'].update(_peerMessage=True, _peerHuman=True),
    lambda c, b: c.turn['projection'].update(_timer=True),
    lambda c, b: c.turn['projection'].update(_brainDispatch=True),
    lambda c, b: c.turn.update(projectionRevision=4),
    lambda c, b: c.turn.update(user_id=True),
    lambda c, b: c.conversations.rows['recipient'].update(user_id=True),
    lambda c, b: c.conversations.rows['recipient']['settings'].update(archived=True),
    lambda c, b: c.conversations.rows['recipient']['settings'].update(projectPath='/other'),
    lambda c, b: c.binding.value.update(conversationId='recipient'),
    lambda c, b: b.update(projectPath='/project/'),
    lambda c, b: b['targets'][0].update(grantRevision=2),
    lambda c, b: b['targets'][0].update(grantRevision=True),
    lambda c, b: b['authorizationSource'].update(projectionRevision=3.0),
])
def test_invalid_or_stale_authority_has_no_partial_effect(context, mutation):
    c = context
    a = assignment(c, 'a')
    body = request(c, a)
    mutation(c, body)
    before = deepcopy(c.client.rows)
    with pytest.raises((ProjectDispatchError, PermissionError)):
        create(c, body)
    assert c.client.rows == before and not c.cancel_calls

@pytest.mark.parametrize('mode', ['owner', 'tenant', 'system', 'scope'])
def test_principal_boundary(context, mode):
    c = context
    a = assignment(c, 'a')
    c.principal = (PrincipalContext.system(subject_id='system', owner_user_id=7, scopes=['chat']) if mode == 'system' else PrincipalContext.user(subject_id='user:7', owner_user_id=8 if mode == 'owner' else 7, tenant_id='other' if mode == 'tenant' else 'tenant-a', scopes=[] if mode == 'scope' else ['chat']))
    with pytest.raises((ProjectDispatchError, PermissionError)):
        create(c, request(c, a))
    if mode != 'tenant':
        with pytest.raises(PermissionError):
            c.app.list(principal=c.principal)
    assert not c.repo.read(a['id'])['grant']['revoked']

def test_stale_second_pin_and_incomplete_stop_are_all_or_none(context):
    c = context
    a, b = assignment(c, 'a'), assignment(c, 'b')
    body = request(c, a, b)
    body['targets'][1]['grantRevision'] = 2
    with pytest.raises(ProjectDispatchError):
        create(c, body)
    with pytest.raises(ProjectDispatchError, match='all current'):
        create(c, request(c, a, kind='stop_project'))
    assert not c.repo.read(a['id'])['grant']['revoked']
    assert not c.repo.scope_changes() and not c.cancel_calls
    assert create(c, request(c, a, b, kind='stop_project'))['stopConfirmed']

def test_partial_cancel_failure_is_durable_and_reconcilable(context):
    c = context
    a, b = assignment(c, 'a'), assignment(c, 'b', recipient='worker-b')
    c.fail_cancels.add('attempt-b')
    view = create(c, request(c, a, b))
    assert [r['cancellation']['state'] for r in view['revocations']] == ['terminal', 'failed']
    assert not view['stopConfirmed']
    c.fail_cancels.clear()
    assert c.app.retry(view['changeId'], principal=c.principal)['stopConfirmed']
    assert c.cancel_calls == ['attempt-a', 'attempt-b', 'attempt-b']
    assert all(r['grant']['revision'] == 2 and r['grant']['revoked'] for r in c.repo.list())
    assert len(ProjectMailbox(7, '/project', 'recipient', client=c.client).load()[0]['messages']) == 1

def test_signal_is_not_stop_and_reads_are_passive(context):
    c = context
    c.signal_only = True
    a = assignment(c, 'a')
    view = create(c, request(c, a))
    assert view['revocations'][0]['cancellation']['state'] == 'signaled' and not view['stopConfirmed']
    puts, calls = c.client.puts, len(c.cancel_calls)
    assert c.app.list(principal=c.principal) == [view]
    assert c.client.puts == puts and len(c.cancel_calls) == calls
    c.app.retry(view['changeId'], principal=c.principal)
    assert len(c.cancel_calls) == calls
    c.attempts['attempt-a']['status'] = 'interrupted'
    assert c.app.retry(view['changeId'], principal=c.principal)['stopConfirmed']

def test_ack_loss_source_archive_and_rebound_replay_preserves_evidence(context):
    c = context
    a = assignment(c, 'a')
    body = request(c, a)
    c.client.uncertain_once = True
    c.cancel_ack_lost.add('attempt-a')
    view = create(c, body)
    assert not view['stopConfirmed']
    c.turn['projectionRevision'] = 99
    c.binding.value['conversationId'] = 'other'
    c.conversations.rows['recipient']['settings']['archived'] = True
    replay = create(c, body)
    assert replay['changeId'] == view['changeId'] and replay['stopConfirmed']
    assert c.cancel_calls == ['attempt-a'] and c.repo.read(a['id'])['grant']['revision'] == 2
    body['note'] = 'changed command'
    with pytest.raises(ProjectDispatchError, match='reused'):
        create(c, body)

def test_notification_failure_rebound_and_ack_replay(context):
    c = context
    a = assignment(c, 'a', bound=False)
    c.fail_mail.add('recipient')
    view = create(c, request(c, a))
    assert view['notifications'][0]['state'] == 'failed'
    c.fail_mail.clear()
    c.conversations.rows['recipient']['settings']['projectPath'] = '/other'
    assert c.app.retry(view['changeId'], principal=c.principal)['notifications'][0]['state'] == 'failed'
    c.conversations.rows['recipient']['settings']['projectPath'] = '/project'
    c.client.uncertain_once = True
    view = c.app.retry(view['changeId'], principal=c.principal)
    assert view['notifications'][0]['state'] == 'submitted'
    assert view['notifications'][0]['messageEvidence']['decision'] is None
    c.app.retry(view['changeId'], principal=c.principal)
    assert len(ProjectMailbox(7, '/project', 'recipient', client=c.client).load()[0]['messages']) == 1
    assert not c.cancel_calls

@pytest.mark.parametrize('field,value', [('user_id', True), ('turnId', 'wrong'), ('conversationId', 'coordinator'), ('status', 'unknown')])
def test_attempt_identity_failure_keeps_revocation_but_no_stop_lie(context, field, value):
    c = context
    a = assignment(c, 'a')
    c.attempts['attempt-a'][field] = value
    view = create(c, request(c, a))
    assert view['authorityRevoked'] and not view['stopConfirmed']
    assert view['revocations'][0]['cancellation']['state'] == 'failed' and not c.cancel_calls

def test_existing_target_stop_and_completed_result_never_overwritten(context):
    c = context
    a = assignment(c, 'a')
    c.attempts['attempt-a']['status'] = 'completed'
    c.repo.record_review(a['id'], 'accept', 'result retained', 4, 1)
    record_stop(7, 'recipient', 1100, client=c.client)
    before = c.repo.read(a['id'])
    view = create(c, request(c, a))
    assert view['stopConfirmed'] and not c.cancel_calls
    assert c.repo.read(a['id'])['execution'] == before['execution']
    assert c.repo.read(a['id'])['review'] == before['review']
    with pytest.raises(ProjectDispatchError):
        c.repo.validate(c.repo.admission(a['id']))

def test_count_bytes_and_cas_bounds_have_no_partial_revoke(context, monkeypatch):
    c = context
    a = assignment(c, 'a')
    body = request(c, a)
    c.client.forced_conflicts = DISPATCH_LIMITS['casAttempts']
    with pytest.raises(ProjectDispatchError, match='Concurrent/uncertain'):
        create(c, body)
    monkeypatch.setitem(LIMITS, 'changesPerProject', 0)
    with pytest.raises(ProjectDispatchError, match='capacity'):
        create(c, body)
    monkeypatch.setitem(LIMITS, 'changesPerProject', 128)
    monkeypatch.setitem(LIMITS, 'maxChangeBytes', 1)
    with pytest.raises(ProjectDispatchError, match='byte capacity'):
        create(c, body)
    monkeypatch.setitem(LIMITS, 'maxChangeBytes', 262144)
    body['targets'] *= 129
    with pytest.raises(ProjectDispatchError):
        create(c, body)
    assert not c.repo.read(a['id'])['grant']['revoked'] and not c.repo.scope_changes() and not c.cancel_calls
    assert schema('request')['additionalProperties'] is False

def test_concurrent_identical_command_has_one_atomic_original(context):
    c = context
    a = assignment(c, 'a', bound=False)
    body = request(c, a)
    c.client.reads, c.client.first_reads = 0, Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: c.repo.revoke_scope_change(body, principal=c.principal, command_id='same'), range(2)))
    assert results[0] == results[1] and len(c.repo.scope_changes()) == 1
    assert c.repo.read(a['id'])['grant']['revision'] == 2 and not c.cancel_calls

def test_extension_record_byte_bound_preserves_evidence(context, monkeypatch):
    c = context
    a = assignment(c, 'a', bound=False)
    before = deepcopy(c.client.rows)
    monkeypatch.setitem(DISPATCH_LIMITS, 'maxRecordBytes', 1024)
    with pytest.raises((ProjectDispatchError, StorageError)):
        create(c, request(c, a))
    assert c.client.rows == before and not c.cancel_calls

def test_tenant_read_retry_and_receipt_integrity(context):
    c = context
    a = assignment(c, 'a')
    view = create(c, request(c, a))
    other = PrincipalContext.user(subject_id='other', owner_user_id=7, tenant_id='other', scopes=['chat'])
    assert c.app.list(principal=other) == []
    with pytest.raises(PermissionError):
        c.app.retry(view['changeId'], principal=other)
    with pytest.raises(ProjectDispatchError):
        c.repo.record_scope_change_outcome(view['changeId'], 'revocations', a['id'], {'state': 'unbound', 'status': '', 'error': ''})
    c.client.rows[(NAMESPACE, c.repo.key)]['value']['scopeChanges'][0]['owner'] = True
    with pytest.raises(StorageError):
        c.repo.scope_changes()


@pytest.mark.parametrize('field,value', [('profile', {'writePaths': ['/**']}),
                                         ('budget', {'maxModelRequests': 999}), ('reworkOf', {})])
def test_scope_removal_dto_never_expands_or_reworks(context, field, value):
    c = context
    a = assignment(c, 'a')
    body = request(c, a)
    body[field] = value
    before = deepcopy(c.client.rows)
    with pytest.raises(ProjectDispatchError):
        create(c, body)
    assert c.client.rows == before and not c.cancel_calls


def test_nonworker_source_cannot_be_promoted_by_human_api_identity(context):
    c = context
    a = assignment(c, 'a')
    c.conversations.rows['stranger'] = {'user_id': 7, 'settings': {'projectPath': '/project'}}
    c.turn['conversationId'] = 'stranger'
    body = request(c, a)
    body['authorizationSource']['conversationId'] = 'stranger'
    with pytest.raises(ProjectDispatchError, match='owned worker'):
        create(c, body)
    assert not c.repo.read(a['id'])['grant']['revoked']


def test_cas_retry_revalidates_exact_source_revision(context):
    c = context
    a, b = assignment(c, 'a'), assignment(c, 'b')
    command = c.client.command
    def stale(operation, args, identity):
        if args['namespace'] == NAMESPACE and args['value'].get('scopeChanges'):
            c.turn['projectionRevision'] = 4
            raise StorageError('database_conflict', 'source changed while CAS raced')
        return command(operation, args, identity)
    c.client.command = stale
    with pytest.raises(ProjectDispatchError, match='source changed'):
        create(c, request(c, a, b))
    assert not c.repo.scope_changes() and not c.cancel_calls
    assert all(not r['grant']['revoked'] for r in c.repo.list())


def test_cancellation_receipt_store_failure_reconciles_exact_existing_attempt(context):
    c = context
    a = assignment(c, 'a')
    command = c.client.command
    def missing_ack(operation, args, identity):
        changes = args['value'].get('scopeChanges', []) if args['namespace'] == NAMESPACE else []
        if changes and changes[0]['revocations'][0]['cancellation']['state'] == 'terminal':
            raise StorageError('database_unavailable', 'receipt store unavailable')
        return command(operation, args, identity)
    c.client.command = missing_ack
    with pytest.raises(StorageError):
        create(c, request(c, a))
    change = c.repo.scope_changes()[0]
    assert c.repo.read(a['id'])['grant']['revoked']
    assert change['revocations'][0]['cancellation']['state'] == 'pending'
    assert c.cancel_calls == ['attempt-a']
    c.client.command = command
    assert c.app.retry(change['changeId'], principal=c.principal)['stopConfirmed']
    assert c.cancel_calls == ['attempt-a']


def test_actual_cancel_owner_uses_targeted_withdrawal_not_conversation_stop(monkeypatch):
    from lib.conversation_sync import command_service
    calls = []
    def abort(attempt_id, **kwargs):
        calls.append((attempt_id, kwargs))
        return {'attemptId': attempt_id, 'status': 'abort_signaled'}
    monkeypatch.setattr(command_service, 'abort_attempt', abort)
    from lib.conversation_sync.runtime import conversation_turn_commands
    result = conversation_turn_commands.abort_project_attempt('exact', 7)
    assert result.value == {'attemptId': 'exact', 'status': 'abort_signaled'}
    assert calls == [('exact', {'user_id': 7, 'withdraw_dispatch': False})]


def test_command_lookup_and_replay_do_not_renew_scope(context):
    c = context
    a = assignment(c, 'a', bound=False)
    view = create(c, request(c, a), command='human-removal')
    assert c.app.list(principal=c.principal, command_id='human-removal') == [view]
    assert c.app.list(principal=c.principal, command_id='other') == []
    created = c.repo.read(a['id'])['grant']['createdAt']
    c.now[0] += 100
    assert create(c, request(c, a), command='human-removal')['changeId'] == view['changeId']
    assert c.repo.read(a['id'])['grant']['createdAt'] == created


@pytest.mark.parametrize('corruption', ['terminal', 'unbound', 'revision'])
def test_corrupt_receipts_fail_closed_not_ui_stop_truth(context, corruption):
    c = context
    a = assignment(c, 'a')
    view = create(c, request(c, a))
    revocation = c.client.rows[(NAMESPACE, c.repo.key)]['value']['scopeChanges'][0]['revocations'][0]
    if corruption == 'revision':
        revocation['revokedRevision'] = 2.0
    else:
        revocation['cancellation'].update(state=corruption, status='running')
    with pytest.raises(StorageError):
        c.app.view(view['changeId'], principal=c.principal)


def test_default_application_cancel_port_uses_existing_runtime_owner(context, monkeypatch):
    from lib.conversation_sync import command_service
    c = context
    a = assignment(c, 'a')
    c.app.commands = None
    calls = []
    def abort(attempt_id, **kwargs):
        calls.append((attempt_id, kwargs))
        c.attempts[attempt_id]['status'] = 'interrupted'
        return {'attemptId': attempt_id, 'status': 'abort_signaled'}
    monkeypatch.setattr(command_service, 'abort_attempt', abort)
    assert create(c, request(c, a))['stopConfirmed']
    assert calls == [('attempt-a', {'user_id': 7, 'withdraw_dispatch': False})]

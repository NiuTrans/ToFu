"""Portable dispatch authority tests: fake CAS storage, no native engine/tasks."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Lock
from types import SimpleNamespace
import uuid

import pytest

from lib.identity import PrincipalContext
from lib.project_dispatch.repository import (
    LIMITS, NAMESPACE, ProjectDispatchError, ProjectDispatchRepository,
)
from lib.storage.errors import StorageError


pytestmark = pytest.mark.unit

class RecordClient:
    def __init__(self):
        self.rows = {}
        self.receipts = {}
        self.lock = Lock()
        self.first_reads = None
        self.reads = 0
        self.conflicts = 0
        self.puts = 0
        self.forced_conflicts = 0
        self.uncertain_once = False

    def __call__(self, *, write=False):
        return self

    def query(self, operation, args):
        assert operation == 'record.get'
        with self.lock:
            result = deepcopy(self.rows.get((args['namespace'], args['key'])))
            self.reads += 1
            barrier = self.first_reads if self.reads <= 2 else None
        if barrier:
            barrier.wait(timeout=5)
        return result

    def command(self, operation, args, command_id):
        assert operation == 'record.put'
        with self.lock:
            if command_id in self.receipts:
                assert self.receipts[command_id] == args
                return
            key = (args['namespace'], args['key'])
            version = self.rows.get(key, {}).get('version', 0)
            if self.forced_conflicts or args['expected_version'] != version:
                if self.forced_conflicts:
                    self.forced_conflicts -= 1
                self.conflicts += 1
                raise StorageError('database_conflict', 'stale record')
            self.rows[key] = {'value': deepcopy(args['value']), 'version': version + 1}
            self.receipts[command_id] = deepcopy(args)
            self.puts += 1
            if self.uncertain_once:
                self.uncertain_once = False
                raise StorageError('database_timeout', 'response lost after commit', retryable=True)


class Conversations:
    def __init__(self):
        self.rows = {
            c: {'user_id': 7, 'settings': {'projectPath': '/project'}}
            for c in ('coordinator', 'recipient')
        }
        self.snapshot = False
        self.calls = []

    def get(self, conversation_id, *, user_id, include_messages):
        self.calls.append((conversation_id, user_id, include_messages))
        row = self.rows.get(conversation_id)
        if row is None or row['user_id'] != user_id:
            return None
        return SimpleNamespace(metadata=deepcopy(row)) if self.snapshot else deepcopy(row)


class Binding:
    def __init__(self):
        self.value = {'projectPath': '/project', 'conversationId': 'coordinator',
                      'availability': 'available', 'revision': 1}

    def read(self, project):
        assert project == '/project'
        return deepcopy(self.value)


@pytest.fixture
def context():
    client, conversations, binding = RecordClient(), Conversations(), Binding()
    turn = {'conversationId': 'coordinator', 'turnId': 'human-turn', 'actor': 'human',
            'projectionRevision': 3, 'projection': {'role': 'user', 'content': 'Please consult'}}
    turn_calls = []

    def turn_get(conversation_id, turn_id, *, user_id):
        turn_calls.append((conversation_id, turn_id, user_id))
        return deepcopy(turn)

    principal = PrincipalContext.user(subject_id='user:7', owner_user_id=7,
                                      tenant_id='tenant-a', scopes=['chat'])
    request = {'projectPath': '/project', 'coordinatorId': 'coordinator',
               'recipientId': 'recipient', 'authorizationSource': {
                   'conversationId': 'coordinator', 'turnId': 'human-turn', 'projectionRevision': 3},
               'brief': 'Consult only, no tools', 'model': 'model-a',
               'budget': {'maxTokens': 100, 'maxModelRequests': 2, 'deadline': 2000}}
    now = [1000]

    def repository(owner=7, project='/project'):
        return ProjectDispatchRepository(owner, project, client=client, storage_version=1,
                                         conversations=conversations, turn_get=turn_get,
                                         binding_repository=binding, clock=lambda: now[0])

    return SimpleNamespace(client=client, conversations=conversations, binding=binding,
                           turn=turn, principal=principal, request=request, now=now,
                           turn_calls=turn_calls, repository=repository, repo=repository())


def authorize(c, command='command'):
    return c.repo.authorize(c.request, c.principal, command)


def test_authorize_admission_and_owned_metadata_ports(context):
    c = context
    c.conversations.snapshot = True
    a = authorize(c)
    assert set(a) == {'id', 'request', 'grant', 'execution', 'review', 'modelAttempts'}
    assert a['grant'] == {'bindingRevision': 1, 'lifecycleVersion': 1, 'subject': 'user:7', 'tenant': 'tenant-a', 'owner': 7,
                          'revision': 1, 'revoked': False, 'createdAt': 1000}
    assert a['execution'] is None and a['review'] is None and a['modelAttempts'] == []
    assert c.repo.validate(c.repo.admission(a['id'])) == a
    assert all(owner == 7 and not messages for _, owner, messages in c.conversations.calls)
    assert c.turn_calls == [('coordinator', 'human-turn', 7)] * 2
    assert next(iter(c.client.rows))[0] == 'project.dispatch.v1'


def test_deep_copies_and_replay_do_not_mutate_or_regrant(context):
    c = context
    a = authorize(c)
    initial = deepcopy(a)
    a['request']['brief'] = 'bad'
    a['grant']['revision'] = 44
    c.request['brief'] = 'changed'
    assert c.repo.read(initial['id']) == initial
    with pytest.raises(ProjectDispatchError, match='reused'):
        authorize(c)
    c.request = deepcopy(initial['request'])
    c.now[0] = 3000
    assert authorize(c) == initial  # replay evidence, not new live authority
    rows = c.repo.list()
    rows[0]['request']['brief'] = 'bad'
    assert c.repo.read(initial['id']) == initial
    assert c.client.puts == 1
    assert c.repository().authorize(c.request, c.principal, 'command')['id'] == initial['id']


@pytest.mark.parametrize('change', ['subject', 'tenant'])
def test_replay_rejects_changed_granter(context, change):
    c = context
    authorize(c)
    c.principal = PrincipalContext.user(subject_id='other' if change == 'subject' else 'user:7',
                                        owner_user_id=7,
                                        tenant_id='other' if change == 'tenant' else 'tenant-a',
                                        scopes=['chat'])
    with pytest.raises(ProjectDispatchError, match='granter'):
        authorize(c)


@pytest.mark.parametrize('mode', ['owner', 'system', 'scope'])
def test_principal_boundary(context, mode):
    c = context
    principal = (PrincipalContext.system(subject_id='system', owner_user_id=7, scopes=['chat'])
                 if mode == 'system' else PrincipalContext.user(
                     subject_id='user', owner_user_id=8 if mode == 'owner' else 7,
                     scopes=[] if mode == 'scope' else ['chat']))
    with pytest.raises((ProjectDispatchError, PermissionError)):
        c.repo.authorize(c.request, principal, 'command')
    assert not c.client.rows


@pytest.mark.parametrize('field,value', [
    ('owner', 8), ('tenant', 'tenant-b'), ('projectPath', '/project/sub'),
    ('recipientId', 'coordinator'), ('revision', 2), ('version', 2),
    ('extra', 'unexpected'), ('owner', True),
])
def test_full_admission_and_scope_validation(context, field, value):
    c = context
    a = authorize(c)
    admission = c.repo.admission(a['id'])
    admission[field] = value
    with pytest.raises(ProjectDispatchError):
        c.repo.validate(admission)
    with pytest.raises(ProjectDispatchError, match='not found'):
        c.repository(owner=8).read(a['id'])
    with pytest.raises(ProjectDispatchError, match='not found'):
        c.repository(project='/project/sub').read(a['id'])


@pytest.mark.parametrize('mutation', [
    lambda r: r.update(unexpected=True),
    lambda r: r['budget'].update(maxTokens=True),
    lambda r: r['budget'].update(maxModelRequests=65),
    lambda r: r['budget'].update(deadline=float('nan')),
    lambda r: r['budget'].update(deadline=float('inf')),
    lambda r: r['authorizationSource'].update(projectionRevision=0),
    lambda r: r.update(brief='x' * 4097),
])
def test_request_schema_rejects_invalid_input(context, mutation):
    mutation(context.request)
    with pytest.raises(ProjectDispatchError):
        authorize(context)
    assert context.client.puts == 0


@pytest.mark.parametrize('change', ['archive', 'readonly', 'project', 'owner', 'missing',
                                   'rebind', 'binding-unavailable', 'expiry', 'revision',
                                   'actor', 'source-conversation', 'turn-id'])
def test_live_authority_rechecks(context, change):
    c = context
    a = authorize(c)
    admission = c.repo.admission(a['id'])
    target = c.conversations.rows['recipient']
    if change == 'archive':
        target['settings']['archived'] = True
    elif change == 'readonly':
        target['settings']['readOnly'] = True
    elif change == 'project':
        target['settings']['projectPath'] = '/project/sub'
    elif change == 'owner':
        target['user_id'] = 8
    elif change == 'missing':
        del c.conversations.rows['coordinator']
    elif change == 'rebind':
        c.binding.value['conversationId'] = 'recipient'
    elif change == 'binding-unavailable':
        c.binding.value['availability'] = 'unavailable'
    elif change == 'expiry':
        c.now[0] = 2000
    elif change == 'revision':
        c.turn['projectionRevision'] += 1
    elif change == 'actor':
        c.turn['actor'] = 'virtual_user'
    elif change == 'source-conversation':
        c.turn['conversationId'] = 'other'
    else:
        c.turn['turnId'] = 'other'
    with pytest.raises(ProjectDispatchError):
        c.repo.validate(admission)
    with pytest.raises(ProjectDispatchError):
        c.repo.bind_execution(a['id'], 'run', 'attempt')
    assert c.repo.read(a['id']) == a


@pytest.mark.parametrize('projection', [
    None, {'_initiator': 'peer'}, {'_initiator': []}, {'_initiator': 'unknown'},
    {'origin': {'blockId': 'origin', 'initiator': 'timer'}},
    {'origin': {'blockId': 'origin', 'initiator': 'human', 'sourceConversationId': 'other'}},
    {'_peerMessage': True, '_peerHuman': True}, {'_brainDispatch': True},
    {'_isVirtualUser': True}, {'_autopilotRunId': 'auto'}, {'_timer': True},
    {'_isCompactionSummary': True}, {'role': 'assistant'},
])
def test_api_human_principal_cannot_launder_synthetic_source(context, projection):
    c = context
    c.turn['projection'] = projection
    with pytest.raises(ProjectDispatchError):
        authorize(c)
    assert c.client.puts == 0


@pytest.mark.parametrize('change', ['self', 'source', 'noncanonical'])
def test_exact_project_and_coordinator_source(context, change):
    c = context
    if change == 'self':
        c.request['recipientId'] = 'coordinator'
    elif change == 'source':
        c.request['authorizationSource']['conversationId'] = 'recipient'
    else:
        c.request['projectPath'] = '/project/'
    with pytest.raises(ProjectDispatchError):
        authorize(c)


def test_project_constructor_uses_canonical_namespace(context):
    assert context.repository(project='/project/').key == context.repo.key
    assert context.repository(owner=8).key != context.repo.key
    assert context.repository(project='/project/sub').key != context.repo.key


def test_concurrent_cas_preserves_both_assignments(context):
    c = context
    c.client.first_reads = Barrier(2)
    def write(command):
        return c.repository().authorize(c.request, c.principal, command)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(write, ('one', 'two')))
    assert first['id'] != second['id']
    assert {a['id'] for a in c.repo.list()} == {first['id'], second['id']}
    assert c.client.conflicts == 1 and c.client.puts == 2


def test_concurrent_identical_authorization_has_one_original(context):
    c = context
    c.client.first_reads = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: c.repository().authorize(
            c.request, c.principal, 'same'), range(2)))
    assert results[0] == results[1]
    assert len(c.repo.list()) == 1 and c.client.puts == 1


def test_uncertain_commit_is_reconciled_and_retries_are_bounded(context):
    c = context
    c.client.uncertain_once = True
    a = authorize(c)
    assert c.repo.list() == [a] and c.client.puts == 1
    c.client.forced_conflicts = LIMITS['casAttempts']
    with pytest.raises(ProjectDispatchError, match='Concurrent/uncertain'):
        authorize(c, 'another')
    assert c.client.conflicts == LIMITS['casAttempts']
    assert c.repo.list() == [a]


def test_capacity_has_no_eviction_and_replay_still_works(context):
    c = context
    a = authorize(c)
    row = c.client.rows[(NAMESPACE, c.repo.key)]
    for index in range(LIMITS['assignmentsPerProject'] - 1):
        other = deepcopy(a)
        other['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, f'capacity-fixture:{index}'))
        row['value']['assignments'].append(other)
    before = deepcopy(row)
    with pytest.raises(ProjectDispatchError, match='capacity'):
        authorize(c, 'overflow')
    assert row == before and len(c.repo.list()) == 128
    assert authorize(c) == a and c.client.puts == 1


def test_immutable_execution_binding_revoke_and_stale_revision(context):
    c = context
    a = authorize(c)
    association = {'turnId': 'execution-turn', 'attemptId': 'execution-attempt'}
    bound = c.repo.bind_execution(a['id'], **{'turn_id': 'execution-turn', 'attempt_id': 'execution-attempt'})
    assert bound['execution'] == association
    puts = c.client.puts
    assert c.repo.bind_execution(a['id'], 'execution-turn', 'execution-attempt') == bound
    assert c.client.puts == puts
    with pytest.raises(ProjectDispatchError, match='immutable'):
        c.repo.bind_execution(a['id'], 'execution-turn', 'another-attempt')
    admission = c.repo.admission(a['id'])
    with pytest.raises(ProjectDispatchError, match='revision'):
        c.repo.revoke(a['id'], 2)
    assert c.repo.revoke(a['id'], 1) == association
    assert c.repo.revoke(a['id'], 1) == association
    assert c.repo.read(a['id'])['grant']['revision'] == 2
    assert c.repo.read(a['id'])['grant']['revoked'] is True
    with pytest.raises(ProjectDispatchError, match='revoked'):
        c.repo.validate(admission)
    with pytest.raises(ProjectDispatchError, match='revoked'):
        c.repo.validate(c.repo.admission(a['id']))
    assert c.repo.bind_execution(a['id'], 'execution-turn', 'execution-attempt')['execution'] == association
    assert authorize(c)['grant']['revoked'] is True  # replay never renews authority


def test_unbound_revocation_returns_no_association(context):
    c = context
    a = authorize(c)
    assert c.repo.revoke(a['id'], 1) is None
    with pytest.raises(ProjectDispatchError):
        c.repo.bind_execution(a['id'], 'turn', 'attempt')


@pytest.mark.parametrize('decision', ['accept', 'rework', 'reject'])
def test_immutable_report_review_is_not_delivery(context, decision):
    c = context
    a = authorize(c)
    with pytest.raises(ProjectDispatchError, match='association'):
        c.repo.record_review(a['id'], decision, 'checked', 4, 1)
    bound = c.repo.bind_execution(a['id'], 'run', 'attempt')
    reviewed = c.repo.record_review(a['id'], decision, 'checked', 4, 1)
    assert reviewed['review'] == {'decision': decision, 'note': 'checked', 'resultRevision': 4}
    assert reviewed['execution'] == bound['execution']
    assert reviewed['grant'] == bound['grant']
    assert 'delivered' not in reviewed and 'status' not in reviewed
    puts = c.client.puts
    assert c.repo.record_review(a['id'], decision, 'checked', 4, 1) == reviewed
    assert c.client.puts == puts
    with pytest.raises(ProjectDispatchError, match='immutable'):
        c.repo.record_review(a['id'], decision, 'checked', 5, 1)


@pytest.mark.parametrize('decision,note,result_revision,expected_revision', [
    ('approve', '', 1, 1), ('accept', 'x' * 2049, 1, 1), ('accept', '', True, 1),
    ('accept', '', 0, 1), ('accept', '', 1, True), ('accept', '', 1, 2),
])
def test_review_input_and_revision_fencing(context, decision, note, result_revision, expected_revision):
    c = context
    a = authorize(c)
    c.repo.bind_execution(a['id'], 'run', 'attempt')
    with pytest.raises(ProjectDispatchError):
        c.repo.record_review(a['id'], decision, note, result_revision, expected_revision)


def model_attempt(identity, phase='dispatch_intent'):
    return {'root_task_id': identity, 'attempt_task_id': identity, 'attempt_ordinal': 1,
            'reserved_tokens': 10, 'phase': phase, 'reported_tokens': 0,
            'usage_complete': phase != 'dispatch_intent', 'account_observed_tokens': 0,
            'account_nonce': 'a' * 32}


def test_trusted_mutation_preserves_other_assignments_and_authority(context):
    c = context
    a, b = authorize(c, 'one'), authorize(c, 'two')
    def ledger(assignment):
        assignment['modelAttempts'].append(model_attempt('model-attempt'))
    updated = c.repo.mutate(a['id'], ledger)
    assert updated['modelAttempts'] == [model_attempt('model-attempt')]
    assert c.repo.read(b['id']) == b
    updated['modelAttempts'].clear()
    assert c.repo.read(a['id'])['modelAttempts']
    with pytest.raises(ProjectDispatchError, match='authority'):
        c.repo.mutate(a['id'], lambda value: value['grant'].update(revoked=True))
    with pytest.raises(ProjectDispatchError):
        c.repo.mutate(a['id'], lambda value: value.update(modelAttempts={}))


def test_byte_cap_is_enforced_without_partial_write(context):
    c = context
    a = authorize(c)
    before = deepcopy(c.client.rows)
    def oversized(assignment):
        assignment['modelAttempts'] = [{'diagnostic': 'x' * LIMITS['maxRecordBytes']}]
    with pytest.raises(ProjectDispatchError, match='byte capacity'):
        c.repo.mutate(a['id'], oversized)
    assert c.client.rows == before and c.repo.read(a['id']) == a


@pytest.mark.parametrize('corruption', ['binding', 'duplicate', 'request', 'tenant', 'execution', 'ledger'])
def test_corrupt_storage_fails_closed(context, corruption):
    c = context
    a = authorize(c)
    value = c.client.rows[(NAMESPACE, c.repo.key)]['value']
    if corruption == 'binding':
        value['binding']['owner'] = 8
    elif corruption == 'duplicate':
        value['assignments'].append(deepcopy(a))
    elif corruption == 'request':
        value['assignments'][0]['request']['budget']['maxModelRequests'] = 100
    elif corruption == 'tenant':
        value['assignments'][0]['grant']['tenant'] = ''
    elif corruption == 'execution':
        value['assignments'][0]['execution'] = {'turnId': 'run'}
    else:
        value['assignments'][0]['modelAttempts'] = {}
    with pytest.raises(StorageError) as caught:
        c.repo.read(a['id'])
    assert caught.value.code == 'database_integrity'


def test_nonretryable_storage_error_is_not_hidden(context):
    c = context
    def command(*args):
        raise StorageError('database_forbidden', 'denied')
    c.client.command = command
    with pytest.raises(StorageError) as caught:
        authorize(c)
    assert caught.value.code == 'database_forbidden' and c.client.reads == 1


@pytest.mark.parametrize('change', ['projection', 'turn-marker', 'source-owner', 'origin'])
def test_source_provenance_changes_deny_existing_admission(context, change):
    c = context
    a = authorize(c)
    admission = c.repo.admission(a['id'])
    if change == 'projection':
        c.turn['projection']['_initiator'] = 'operator'
    elif change == 'turn-marker':
        c.turn['_peerMessage'] = True
    elif change == 'source-owner':
        c.turn['user_id'] = 8
    else:
        c.turn['projection']['origin'] = {'initiator': 'brain', 'blockId': 'origin'}
    with pytest.raises(ProjectDispatchError):
        c.repo.validate(admission)
    assert c.repo.read(a['id']) == a


@pytest.mark.parametrize('metadata', [None, {'settings': []}, {'settings': None},
                                     {'settings': {'projectPath': '/project'}, 'archived': True}])
def test_malformed_or_archived_metadata_fails_gracefully(context, metadata):
    c = context
    c.conversations.get = lambda *args, **kwargs: metadata
    with pytest.raises(ProjectDispatchError, match='unavailable'):
        authorize(c)


def test_concurrent_internal_ledger_mutations_rebase_without_lost_rows(context):
    c = context
    a, b = authorize(c, 'one'), authorize(c, 'two')
    c.client.reads = 0
    c.client.first_reads = Barrier(2)
    def mutate(assignment_id):
        def update(assignment):
            assignment['modelAttempts'].append(model_attempt(assignment_id, 'settled'))
        return c.repository().mutate(assignment_id, update)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(mutate, (a['id'], b['id'])))
    assert c.client.conflicts == 1
    for result in results:
        assert c.repo.read(result['id'])['modelAttempts'] == result['modelAttempts']
    assert len(c.repo.list()) == 2


def test_coordinator_binding_aba_never_restores_old_paid_authority(context):
    c = context
    row = authorize(c)
    admission = c.repo.admission(row['id'])
    c.binding.value.update(conversationId='another-coordinator', revision=2)
    with pytest.raises(ProjectDispatchError, match='binding'):
        c.repo.validate(admission)
    c.binding.value.update(conversationId='coordinator', revision=3)
    with pytest.raises(ProjectDispatchError, match='binding'):
        c.repo.validate(admission)
    assert authorize(c) == row  # uncertain ACK replay keeps original epoch
    assert c.repo.read(row['id'])['execution'] is None
    fresh = authorize(c, 'new-human-command')
    assert fresh['grant']['bindingRevision'] == 3
    assert c.repo.validate(c.repo.admission(fresh['id'])) == fresh


@pytest.mark.parametrize('revision', [True, 1.0, 0, None, '1'])
def test_malformed_coordinator_binding_revision_cannot_authorize(context, revision):
    c = context
    c.binding.value['revision'] = revision
    with pytest.raises(ProjectDispatchError, match='binding'):
        authorize(c)
    assert c.repo.list() == []


def test_legacy_grant_without_epoch_is_readable_but_cannot_gain_authority(context):
    c = context
    row = authorize(c)
    record = c.client.rows[(NAMESPACE, c.repo.key)]['value']
    del record['assignments'][0]['grant']['bindingRevision']
    assert c.repo.read(row['id'])['request'] == row['request']
    with pytest.raises(ProjectDispatchError, match='fresh human authorization'):
        c.repo.validate(c.repo.admission(row['id']))
    assert authorize(c)['grant'].get('bindingRevision') is None

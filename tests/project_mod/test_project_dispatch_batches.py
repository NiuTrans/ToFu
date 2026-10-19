"""Atomic explicit-human batches over existing portable CAS; no task/provider effects."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
import json
import uuid

from jsonschema import Draft202012Validator
import pytest

from lib.identity import PrincipalContext
from lib.project_dispatch.repository import (
    CONTRACT, LIMITS, NAMESPACE, ProjectDispatchError, ProjectDispatchRepository,
    batch_requests, record_stop,
)
from lib.storage.errors import StorageError
from tests.project_mod.test_project_dispatch_repository import context as context, model_attempt

from tests.storage.test_storage_authority_sql_contract import storage as storage
pytestmark = pytest.mark.unit


@pytest.fixture
def batch_context(context):
    c = context
    c.batch_request = {key: deepcopy(c.request[key]) for key in (
        'projectPath', 'coordinatorId', 'authorizationSource')}
    c.batch_request.update(objective='Investigate the original objective without publishing',
                           budget={'maxTokens': 200, 'maxModelRequests': 4, 'deadline': 2000},
                           workers=[])
    for index in range(2):
        recipient = f'worker-{index}'
        c.conversations.rows[recipient] = {'user_id': 7, 'settings': {'projectPath': '/project'}}
        c.batch_request['workers'].append({
            'recipientId': recipient, 'brief': f'Check part {index}', 'model': 'model-a',
            'budget': {'maxTokens': 100, 'maxModelRequests': 2, 'deadline': 2000}})
    return c


def authorize(c, command='batch-command', repo=None):
    return (repo or c.repo).authorize_batch(c.batch_request, principal=c.principal, command_id=command)


def record(c):
    return c.client.rows[(NAMESPACE, c.repo.key)]['value']


def test_atomic_manifest_derivation_and_no_execution_effects(batch_context):
    c = batch_context
    request, conversations = deepcopy(c.batch_request), deepcopy(c.conversations.rows)
    batch = authorize(c)
    assert set(batch) == {'id', 'request', 'assignmentIds'}
    assert batch['id'] == c.repo.batch_identity('batch-command')
    assert batch['assignmentIds'] == [str(uuid.uuid5(uuid.UUID(batch['id']), f'worker:{i}'))
                                       for i in range(2)]
    assert batch['request'] == request and c.batch_request == request
    children = batch_requests(request)
    rows = c.repo.list()
    assert [r['request'] for r in rows] == children
    assert children[0]['brief'] == ('Original batch objective:\n' + request['objective']
                                   + '\n\nWorker responsibility:\nCheck part 0')
    assert [r['id'] for r in rows] == batch['assignmentIds']
    assert rows[0]['grant'] == rows[1]['grant'] == {
        'subject': 'user:7', 'tenant': 'tenant-a', 'owner': 7, 'bindingRevision': 1,
        'lifecycleVersion': 1, 'revision': 1, 'revoked': False, 'createdAt': 1000}
    assert all(r['execution'] is None and r['review'] is None and r['modelAttempts'] == []
               and 'publication' not in r and 'toolAttempts' not in r for r in rows)
    assert c.client.puts == 1 and len(c.client.rows) == 1
    assert c.conversations.rows == conversations
    assert all(owner == 7 and messages is False for _, owner, messages in c.conversations.calls)
    assert c.repo.find_batch('batch-command') == c.repo.read_batch(batch['id']) == batch
    assert c.repo.find_batch('absent') is None
    with pytest.raises(ProjectDispatchError, match='not found'):
        c.repo.read_batch('absent')
    for r in rows:
        assert c.repo.validate(c.repo.admission(r['id'])) == r


def test_reads_and_returned_requests_are_isolated_and_read_only(batch_context):
    c = batch_context
    batch = authorize(c)
    initial = deepcopy(batch)
    batch['request']['objective'] = 'changed'
    batch['assignmentIds'].clear()
    read = c.repo.read_batch(initial['id'])
    read['request']['workers'][0]['budget']['maxTokens'] = 123
    c.repo.find_batch('batch-command')['assignmentIds'].clear()
    children = batch_requests(c.batch_request)
    children[0]['authorizationSource']['turnId'] = 'changed'
    assert c.repo.find_batch('batch-command') == initial
    assert c.client.puts == 1
    for repo in (c.repository(owner=8), c.repository(project='/other')):
        assert repo.find_batch('batch-command') is None
        with pytest.raises(ProjectDispatchError, match='not found'):
            repo.read_batch(initial['id'])


@pytest.mark.parametrize('change', ['missing', 'owner', 'project', 'archive', 'read-only',
                                   'profile', 'dependency', 'rework', 'expired'])
def test_bad_last_worker_admits_nothing_and_preserves_existing_rows(batch_context, change):
    c = batch_context
    prior = c.repo.authorize(c.request, c.principal, 'existing')
    last = c.batch_request['workers'][-1]
    metadata = c.conversations.rows[last['recipientId']]
    if change == 'missing': del c.conversations.rows[last['recipientId']]
    elif change == 'owner': metadata['user_id'] = 8
    elif change == 'project': metadata['settings']['projectPath'] = '/other'
    elif change == 'archive': metadata['settings']['archived'] = True
    elif change == 'read-only': metadata['settings']['readOnly'] = True
    elif change == 'profile': last['profile'] = {'mode': 'files', 'tools': ['unknown'],
                                               'readPaths': [], 'writePaths': [], 'maxToolCalls': 1}
    elif change == 'dependency': last['dependencies'] = [{'assignmentId': prior['id'],
                                                          'grantRevision': 1, 'resultRevision': 1}]
    elif change == 'rework': last['reworkOf'] = {'assignmentId': prior['id'], 'grantRevision': 1,
                                               'resultRevision': 1}
    else: last['budget']['deadline'] = 999
    before = deepcopy(c.client.rows)
    with pytest.raises(ProjectDispatchError): authorize(c)
    assert c.client.rows == before and c.client.puts == 1 and c.repo.list() == [prior]
    assert c.repo.find_batch('batch-command') is None


@pytest.mark.parametrize('change', ['duplicate', 'coordinator', 'source', 'project', 'empty', 'nine',
                                   'tokens', 'requests', 'deadline', 'bool', 'nan', 'inf',
                                   'objective-empty', 'objective-long', 'derived-brief', 'extra',
                                   'caller-id', 'child-source', 'child-objective'])
def test_batch_shape_and_aggregate_limits_fail_before_write(batch_context, change):
    c = batch_context
    request = c.batch_request
    if change == 'duplicate': request['workers'][1]['recipientId'] = 'worker-0'
    elif change == 'coordinator': request['workers'][1]['recipientId'] = 'coordinator'
    elif change == 'source': request['authorizationSource']['conversationId'] = 'worker-0'
    elif change == 'project': request['projectPath'] = '/project/'
    elif change == 'empty': request['workers'] = []
    elif change == 'nine': request['workers'] *= 5
    elif change in ('tokens', 'requests'):
        request['budget']['maxTokens' if change == 'tokens' else 'maxModelRequests'] -= 1
    elif change == 'deadline': request['workers'][1]['budget']['deadline'] += 1
    elif change == 'bool': request['workers'][1]['budget']['maxTokens'] = True
    elif change in ('nan', 'inf'): request['budget']['deadline'] = float(change)
    elif change == 'objective-empty': request['objective'] = ''
    elif change == 'objective-long': request['objective'] = 'x' * 4097
    elif change == 'derived-brief': request['objective'] = 'x' * 4096
    elif change == 'extra': request['automaticReview'] = True
    elif change == 'caller-id': request['workers'][0]['id'] = 'caller-picked'
    elif change == 'child-source': request['workers'][0]['authorizationSource'] = request['authorizationSource']
    else: request['workers'][0]['objective'] = 'different goal'
    with pytest.raises(ProjectDispatchError): authorize(c)
    assert not c.client.rows and c.client.puts == 0


@pytest.mark.parametrize('count', [1, 8])
def test_minimum_and_maximum_batch_sizes(batch_context, count):
    c = batch_context
    worker = deepcopy(c.batch_request['workers'][0])
    c.batch_request['workers'] = [{**deepcopy(worker), 'recipientId': f'worker-{i}'}
                                  for i in range(count)]
    for w in c.batch_request['workers']:
        c.conversations.rows[w['recipientId']] = {'user_id': 7, 'settings': {'projectPath': '/project'}}
    c.batch_request['budget'].update(maxTokens=count * 100, maxModelRequests=count * 2)
    batch = authorize(c)
    assert len(batch['assignmentIds']) == len(c.repo.list()) == count
    assert c.client.puts == 1


@pytest.mark.parametrize('key_mode', ['same', 'different'])
def test_concurrent_batches_rebase_atomically(batch_context, key_mode):
    c = batch_context
    c.client.first_reads = Barrier(2)
    commands = ('same', 'same' if key_mode == 'same' else 'different')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda key: authorize(c, key, c.repository()), commands))
    if key_mode == 'same':
        assert results[0] == results[1]
        assert len(c.repo.list()) == 2 and c.client.puts == 1
    else:
        assert results[0]['id'] != results[1]['id']
        assert len(c.repo.list()) == 4 and c.client.puts == 2
        assert c.client.conflicts == 1
    for command, batch in zip(commands, results):
        assert c.repo.find_batch(command) == batch
        assert all(c.repo.read(member)['request'] == child for member, child in
                   zip(batch['assignmentIds'], batch_requests(batch['request'])))


def test_lost_ack_exact_replay_fresh_repository_never_renews(batch_context):
    c = batch_context
    c.client.uncertain_once = True
    batch = authorize(c)
    assert c.client.puts == 1 and len(c.repo.list()) == 2
    c.repo.revoke(batch['assignmentIds'][0], 1)
    c.now[0] = 3000
    c.binding.value.update(conversationId='new-coordinator', revision=2)
    c.turn['projectionRevision'] = 4
    del c.conversations.rows['worker-1']
    record_stop(7, 'coordinator', 3001, client=c.client)
    before = deepcopy(c.client.rows)
    assert authorize(c, repo=c.repository()) == batch
    assert c.repository().find_batch('batch-command') == batch
    assert c.repository().read_batch(batch['id']) == batch
    assert c.client.rows == before
    assert c.repo.read(batch['assignmentIds'][0])['grant']['revoked'] is True
    assert c.repo.read(batch['assignmentIds'][1])['grant']['bindingRevision'] == 1
    for member in batch['assignmentIds']:
        with pytest.raises(ProjectDispatchError): c.repo.validate(c.repo.admission(member))


@pytest.mark.parametrize('change', ['objective', 'source', 'order', 'brief', 'budget', 'subject', 'tenant'])
def test_changed_request_or_principal_conflicts_not_renewed(batch_context, change):
    c = batch_context
    batch = authorize(c)
    before = deepcopy(c.client.rows)
    if change == 'objective': c.batch_request['objective'] += ' changed'
    elif change == 'source': c.batch_request['authorizationSource']['projectionRevision'] += 1
    elif change == 'order': c.batch_request['workers'].reverse()
    elif change == 'brief': c.batch_request['workers'][1]['brief'] += ' changed'
    elif change == 'budget': c.batch_request['budget']['maxTokens'] += 1
    else:
        c.principal = PrincipalContext.user(subject_id='other' if change == 'subject' else 'user:7',
                                            owner_user_id=7,
                                            tenant_id='other' if change == 'tenant' else 'tenant-a',
                                            scopes=['chat'])
    with pytest.raises(ProjectDispatchError, match='reused'): authorize(c)
    assert c.client.rows == before and c.repo.read_batch(batch['id']) == batch


@pytest.mark.parametrize('mode', ['owner', 'system', 'scope', 'synthetic'])
def test_batch_explicit_human_admission_only(batch_context, mode):
    c = batch_context
    if mode == 'synthetic': c.turn['projection']['_brainDispatch'] = True
    elif mode == 'system': c.principal = PrincipalContext.system(subject_id='system', owner_user_id=7, scopes=['chat'])
    else:
        c.principal = PrincipalContext.user(subject_id='user:7', owner_user_id=8 if mode == 'owner' else 7,
                                            scopes=[] if mode == 'scope' else ['chat'])
    with pytest.raises((ProjectDispatchError, PermissionError)): authorize(c)
    assert c.client.puts == 0


@pytest.mark.parametrize('recipient', ['coordinator', 'worker-1'])
def test_stop_checked_on_new_batch_admission_but_not_legacy_single(batch_context, recipient):
    c = batch_context
    record_stop(7, recipient, c.now[0], client=c.client)
    before = deepcopy(c.client.rows)
    with pytest.raises(ProjectDispatchError, match='Stop'): authorize(c)
    assert c.client.rows == before and c.repo.list() == []

    single = c.repo.authorize(c.request, c.principal, 'legacy-single')
    assert single['grant']['createdAt'] == c.now[0]
    # A genuinely later human authorization remains possible under existing Stop semantics.
    c.now[0] += 1
    assert len(authorize(c)['assignmentIds']) == 2


@pytest.mark.parametrize('phase', ['during-workers', 'after-workers'])
def test_rebind_during_admission_cannot_commit_mixed_epochs(batch_context, phase):
    c = batch_context
    base = c.binding.read
    count = [0]
    def binding(project):
        count[0] += 1
        if count[0] == (2 if phase == 'during-workers' else 3):
            c.binding.value['revision'] += 1
        return base(project)
    c.binding.read = binding
    with pytest.raises(ProjectDispatchError, match='binding'): authorize(c)
    assert c.client.puts == 0


def test_batch_rebind_aba_preserves_old_manifest_but_new_command_pins_new_epoch(batch_context):
    c = batch_context
    first = authorize(c)
    c.binding.value.update(conversationId='other', revision=2)
    with pytest.raises(ProjectDispatchError): authorize(c, 'new')
    c.binding.value.update(conversationId='coordinator', revision=3)
    second = authorize(c, 'new')
    assert first != second
    assert authorize(c) == first
    for member in first['assignmentIds']:
        with pytest.raises(ProjectDispatchError, match='binding'): c.repo.validate(c.repo.admission(member))
    assert all(c.repo.read(member)['grant']['bindingRevision'] == 3 for member in second['assignmentIds'])


def test_assignment_capacity_reserves_every_child_or_none_and_replay_works(batch_context):
    c = batch_context
    batch = authorize(c)
    value = record(c)
    seed = c.repo.read(batch['assignmentIds'][0])
    for index in range(LIMITS['assignmentsPerProject'] - 3):
        row = deepcopy(seed)
        row['id'] = str(uuid.uuid5(uuid.NAMESPACE_URL, f'batch-capacity:{index}'))
        value['assignments'].append(row)
    before = deepcopy(c.client.rows)
    with pytest.raises(ProjectDispatchError, match='capacity'): authorize(c, 'overflow')
    assert c.client.rows == before and len(c.repo.list()) == 127
    assert authorize(c) == batch
    c.batch_request['workers'].pop()
    assert len(authorize(c, 'last-slot')['assignmentIds']) == 1
    assert len(c.repo.list()) == 128


def test_batch_capacity_is_independently_bounded_without_eviction(batch_context):
    c = batch_context
    c.batch_request['workers'].pop()
    for index in range(LIMITS['batchesPerProject']):
        authorize(c, f'batch-{index}')
    assert len(record(c)['batches']) == len(c.repo.list()) == 128
    before = deepcopy(c.client.rows)
    with pytest.raises(ProjectDispatchError, match='batch capacity'): authorize(c, 'overflow')
    assert c.client.rows == before
    assert c.repo.find_batch('batch-0') == authorize(c, 'batch-0')


def test_byte_capacity_and_exhausted_cas_never_partially_persist(batch_context, monkeypatch):
    c = batch_context
    batch = authorize(c)
    before = deepcopy(c.client.rows)
    size = len(json.dumps(record(c), ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    monkeypatch.setitem(LIMITS, 'maxRecordBytes', size + 1)
    with pytest.raises(ProjectDispatchError, match='byte capacity'): authorize(c, 'too-large')
    assert c.client.rows == before and c.repo.find_batch('too-large') is None
    monkeypatch.setitem(LIMITS, 'maxRecordBytes', 4194304)
    c.client.forced_conflicts = LIMITS['casAttempts']
    with pytest.raises(ProjectDispatchError, match='Concurrent/uncertain'): authorize(c, 'conflicting')
    assert c.client.rows == before and c.client.conflicts == LIMITS['casAttempts']
    assert authorize(c) == batch


@pytest.mark.parametrize('change', ['manifest-extra', 'objective', 'common-source', 'worker-budget',
                                   'members-short', 'member-duplicate', 'member-dangling', 'member-order',
                                   'batch-duplicate', 'batch-bad-id', 'request', 'subject', 'tenant',
                                   'epoch', 'epoch-absent', 'createdAt', 'lifecycle-absent', 'batches-type'])
def test_batch_integrity_rejects_corruption_on_every_read(batch_context, change):
    c = batch_context
    batch = authorize(c)
    value = record(c)
    manifest = value['batches'][0]
    last = value['assignments'][-1]
    if change == 'manifest-extra': manifest['automatic'] = True
    elif change == 'objective': manifest['request']['objective'] += 'changed'
    elif change == 'common-source': manifest['request']['authorizationSource']['projectionRevision'] += 1
    elif change == 'worker-budget': manifest['request']['workers'][1]['budget']['maxTokens'] -= 1
    elif change == 'members-short': manifest['assignmentIds'].pop()
    elif change == 'member-duplicate': manifest['assignmentIds'][1] = manifest['assignmentIds'][0]
    elif change == 'member-dangling': value['assignments'].pop()
    elif change == 'member-order': manifest['assignmentIds'].reverse()
    elif change == 'batch-duplicate': value['batches'].append(deepcopy(manifest))
    elif change == 'batch-bad-id': manifest['id'] = 'not-a-uuid'
    elif change == 'request': last['request']['brief'] += ' changed'
    elif change in ('subject', 'tenant'): last['grant'][change] = 'other'
    elif change == 'epoch': last['grant']['bindingRevision'] += 1
    elif change == 'epoch-absent': del last['grant']['bindingRevision']
    elif change == 'createdAt': last['grant']['createdAt'] += 1
    elif change == 'lifecycle-absent': del last['grant']['lifecycleVersion']
    else: value['batches'] = {}
    for read in (c.repo.list, lambda: c.repo.read_batch(batch['id']),
                 lambda: c.repo.find_batch('batch-command')):
        with pytest.raises(StorageError) as error: read()
        assert error.value.code == 'database_integrity'
    assert c.client.puts == 1


@pytest.mark.parametrize('field', ['id', 'request', 'grant', 'execution', 'review', 'publication'])
def test_existing_mutate_cannot_change_member_authority(batch_context, field):
    c = batch_context
    batch = authorize(c)
    before = deepcopy(c.client.rows)
    def edit(row):
        if field == 'id': row['id'] = 'other'
        elif field == 'request': row['request']['brief'] = 'different objective'
        elif field == 'grant': row['grant']['bindingRevision'] = 3
        elif field == 'execution': row['execution'] = {'turnId': 't', 'attemptId': 'a'}
        elif field == 'review': row['review'] = {'decision': 'accept', 'note': '', 'resultRevision': 1}
        else: row['publication'] = {'unsafe': True}
    with pytest.raises(ProjectDispatchError, match='authority'): c.repo.mutate(batch['assignmentIds'][0], edit)
    assert c.client.rows == before and c.repo.read_batch(batch['id']) == batch


def test_normal_member_lifecycle_keeps_manifest_and_other_member_unchanged(batch_context):
    c = batch_context
    batch = authorize(c)
    first, last = batch['assignmentIds']
    unchanged = c.repo.read(last)
    c.repo.mutate(first, lambda row: row['modelAttempts'].append(model_attempt('model-attempt', 'settled')))
    c.repo.bind_execution(first, 'turn', 'attempt')
    c.repo.record_review(first, 'rework', 'human review', 2, 1)
    c.repo.revoke(first, 1)
    assert c.repo.read_batch(batch['id']) == batch == authorize(c)
    assert c.repo.read(last) == unchanged
    assert c.repo.read(first)['grant']['revision'] == 2
    assert c.repo.read(first)['review']['decision'] == 'rework'
    assert 'publication' not in c.repo.read(first)
    assert len(c.repo.list()) == 2  # review never creates automatic rework


def test_legacy_record_and_single_grant_domain_are_preserved(batch_context):
    c = batch_context
    single = c.repo.authorize(c.request, c.principal, 'shared-command')
    assert 'batches' not in record(c)
    batch = authorize(c, 'shared-command')
    assert single['id'] != batch['id'] and single['id'] not in batch['assignmentIds']
    assert c.repo.read(single['id']) == single
    assert c.repo.authorize(c.request, c.principal, 'shared-command') == single
    assert c.client.puts == 2


def test_contract_batch_dtos_are_strict_and_reference_existing_children(batch_context):
    c = batch_context
    Draft202012Validator.check_schema(CONTRACT)
    batch = authorize(c)
    def valid(name, value):
        return Draft202012Validator({**CONTRACT, '$ref': '#/$defs/' + name}).is_valid(value)
    assert valid('batch', batch)
    assert valid('batchRetry', {'projectPath': '/project', 'batchId': batch['id']})
    assert not valid('batchRetry', {'projectPath': '/project', 'batchId': batch['id'], 'renew': True})
    for status in ('accepted', 'deferred', 'aborted', 'unknown'):
        result = {'assignmentId': batch['assignmentIds'][0], 'status': status,
                  'reason': 'dispatch_unconfirmed' if status == 'unknown' else ''}
        assert valid('batchDispatchResult', result)
        assert not valid('batchDispatchResult', {**result, 'providerError': 'secret'})
        assert not valid('batchDispatchResult', {**result, 'reason': '' if status == 'unknown' else 'dispatch_unconfirmed'})
    for field, definition in CONTRACT['$defs']['batchWorker']['properties'].items():
        assert definition == {'$ref': '#/$defs/request/properties/' + field}


def test_concurrent_same_key_changed_objective_has_one_winner(batch_context):
    c = batch_context
    c.client.first_reads = Barrier(2)
    requests = [deepcopy(c.batch_request), deepcopy(c.batch_request)]
    requests[1]['objective'] = 'Changed objective'
    def admit(request):
        try:
            return c.repository().authorize_batch(request, principal=c.principal, command_id='same')
        except ProjectDispatchError as error:
            return error
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(admit, requests))
    winners = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    losers = [outcome for outcome in outcomes if isinstance(outcome, ProjectDispatchError)]
    assert len(winners) == len(losers) == 1
    assert 'reused' in str(losers[0])
    assert c.repo.find_batch('same') == winners[0]
    assert len(c.repo.list()) == 2 and c.client.puts == 1


def test_real_portable_storage_batch_cas_and_lost_ack(batch_context, storage):
    c = batch_context
    class LostAck:
        def __init__(self): self.lost, self.puts = False, 0
        def __call__(self, *, write=False): return self
        def query(self, operation, args): return storage.client.query(operation, args)
        def command(self, operation, args, command_id):
            result = storage.client.command(operation, args, command_id)
            self.puts += 1
            if not self.lost:
                self.lost = True
                raise StorageError('database_timeout', 'batch commit ACK lost', retryable=True)
            return result
    port = LostAck()
    def repo():
        return ProjectDispatchRepository(7, '/project', client=port, storage_version=1,
                                         conversations=c.conversations, turn_get=c.repo.turn_get,
                                         binding_repository=c.binding, clock=lambda: c.now[0])
    r = repo()
    bad = deepcopy(c.batch_request)
    bad['workers'][-1]['recipientId'] = 'absent'
    with pytest.raises(ProjectDispatchError): r.authorize_batch(bad, principal=c.principal, command_id='bad')
    assert port.puts == 0 and r.find_batch('bad') is None
    first = r.authorize_batch(c.batch_request, principal=c.principal, command_id='portable')
    durable = storage.client.query('record.get', {'namespace': NAMESPACE, 'key': r.key})
    assert durable['version'] == port.puts == 1
    assert durable['value']['batches'] == [first]
    assert len(durable['value']['assignments']) == 2
    fresh = repo()
    assert fresh.find_batch('portable') == fresh.read_batch(first['id']) == first
    assert fresh.authorize_batch(c.batch_request, principal=c.principal, command_id='portable') == first
    with pytest.raises(StorageError) as stale:
        storage.client.command('record.put', {'namespace': NAMESPACE, 'key': r.key,
                               'value': durable['value'], 'expected_version': 0}, 'stale-batch-cas')
    assert stale.value.code == 'database_conflict'
    fresh.revoke(first['assignmentIds'][0], 1)
    c.now[0] = 3000
    c.binding.value['revision'] = 2
    assert repo().authorize_batch(c.batch_request, principal=c.principal, command_id='portable') == first
    assert repo().read(first['assignmentIds'][0])['grant']['revoked'] is True
    assert port.puts == 2


def test_optional_profile_dependencies_and_rework_use_exact_existing_pins(batch_context):
    c = batch_context
    prior = c.repo.authorize(c.request, c.principal, 'prerequisite')
    c.repo.bind_execution(prior['id'], 'result-turn', 'result-attempt')
    source_get = c.repo.turn_get
    c.repo.turn_get = lambda conv, turn, **kw: (
        {'turnId': turn, 'conversationId': 'recipient', 'actor': 'assistant',
         'projectionRevision': 5, 'projection': {'content': 'Result'}}
        if turn == 'result-turn' else source_get(conv, turn, **kw))
    c.repo.attempt_get = lambda attempt, **kw: {
        'attemptId': attempt, 'turnId': 'result-turn', 'conversationId': 'recipient',
        'taskId': 'result-task', 'status': 'completed'}
    reference = {'assignmentId': prior['id'], 'grantRevision': 1, 'resultRevision': 5}
    c.batch_request['workers'][0]['dependencies'] = [deepcopy(reference)]
    c.batch_request['workers'][0]['profile'] = {
        'mode': 'files', 'tools': ['read_files'], 'readPaths': ['src'],
        'writePaths': [], 'maxToolCalls': 1}
    batch = authorize(c)
    first = c.repo.read(batch['assignmentIds'][0])
    assert first['request']['profile'] == c.batch_request['workers'][0]['profile']
    assert c.repo.dependency_states(first)[0]['status'] == 'waiting_review'
    assert first['execution'] is None and 'publication' not in first
    c.repo.record_review(prior['id'], 'accept', 'Approved', 5, 1)
    assert c.repo.dependency_states(first)[0]['status'] == 'ready'
    c.repo.validate(c.repo.admission(first['id']))
    # Rework is a fresh explicit child with separate identity, never an automatic
    # consequence of review. It preserves its predecessor pin and normal fences.
    rejected = c.repo.authorize(c.request, c.principal, 'reviewed-for-rework')
    c.repo.bind_execution(rejected['id'], 'rework-result-turn', 'rework-result-attempt')
    c.repo.record_review(rejected['id'], 'rework', 'Revise', 5, 1)
    rework_pin = {**reference, 'assignmentId': rejected['id']}
    c.batch_request['workers'][0].pop('dependencies')
    c.batch_request['workers'][1]['reworkOf'] = deepcopy(rework_pin)
    rework = authorize(c, 'explicit-rework')
    assert c.repo.read(rework['assignmentIds'][1])['request']['reworkOf'] == rework_pin
    assert c.repo.read_batch(batch['id']) == batch
    assert len(c.repo.list()) == 6

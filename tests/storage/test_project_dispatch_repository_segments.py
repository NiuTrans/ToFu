"""Real domain cutover, growth and point-access contracts on each authority."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
import uuid

import pytest

from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from lib.project_dispatch.repository import ProjectDispatchRepository, NAMESPACE
from lib.project_dispatch.segmented import SegmentedDispatch, FORMAT, key
from tests.project_mod.test_project_dispatch_repository import context, model_attempt

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.fixture
def owned(context, semantic_authority):
    factory, owner, engine = semantic_authority
    c = context
    for row in c.conversations.rows.values():
        row['user_id'] = owner
    repo = ProjectDispatchRepository(owner, '/project', client=factory, conversations=c.conversations,
        turn_get=c.repo.turn_get, binding_repository=c.binding, clock=lambda: c.now[0])
    principal = PrincipalContext.user(subject_id=f'user:{owner}', owner_user_id=owner, tenant_id='tenant-a', scopes=['chat'])
    return SimpleNamespace(repo=repo, store=SegmentedDispatch(repo), request=c.request, principal=principal,
                           factory=factory, engine=engine)


def authorize(c, command='one'):
    return c.repo.authorize(c.request, c.principal, command)


def test_first_mutation_cuts_over_and_queries_never_write(owned):
    c = owned
    assert c.repo.list() == [] and c.store.head() is None
    one = authorize(c)
    marker = c.factory().query('record.get', {'namespace': NAMESPACE, 'key': c.repo.key})
    assert marker['value']['format'] == FORMAT and marker['value']['original'] is None
    head = c.store.head()
    assert head['value']['state']['phase'] == 'ready'
    assert c.repo.read(one['id']) == one and c.repo.list() == [one]
    assert authorize(c) == one and c.store.head() == head
    assert c.repo.find_command('missing') is None and c.store.head() == head
    # An old in-flight v1 writer cannot replace the new authority.
    with pytest.raises(StorageError) as error:
        c.factory(write=True).command('record.put', {'namespace': NAMESPACE, 'key': c.repo.key,
            'expected_version': 0, 'value': {'binding': c.repo.binding, 'assignments': []}}, uuid.uuid4().hex)
    assert error.value.code == 'database_conflict'


def test_existing_evidence_migrates_exactly_and_old_repository_fails_closed(owned):
    c = owned; c.repo.storage_version = 1
    before = [authorize(c, str(i)) for i in range(3)]
    c.repo.bind_execution(before[0]['id'], 'turn', 'attempt')
    c.repo.mutate(before[0]['id'], lambda row: row['modelAttempts'].append(model_attempt('physical-task')))
    original = c.repo._load()[0]
    c.repo.storage_version = 2
    assert c.repo.list() == original['assignments'] and c.store.head() is None
    final = authorize(c, 'new')
    assert c.repo.list() == original['assignments'] + [final]
    c.repo.storage_version = 1
    with pytest.raises(StorageError):
        c.repo.list()
    c.repo.storage_version = 2
    if c.engine:
        c.engine.restart_cycle()
        assert c.repo.list() == original['assignments'] + [final]


@pytest.mark.parametrize('fault', ['freeze', 'copy', 'compact', 'ready'])
def test_migration_resumes_after_each_lost_ack_without_duplicate_grants(owned, monkeypatch, fault):
    c = owned; c.repo.storage_version = 1
    old = authorize(c, 'old'); c.repo.storage_version = 2
    real = c.repo.client; fired = []
    class Interrupted:
        def query(self, *args, **kwargs): return real().query(*args, **kwargs)
        def command(self, operation, payload, command_id):
            result = real(write=True).command(operation, payload, command_id)
            phase = (payload.get('head') or {}).get('phase')
            point = ('freeze' if operation == 'record.put' and payload['value'].get('original') is not None
                     else 'compact' if operation == 'record.put' else 'ready' if phase == 'ready' else 'copy')
            if point == fault and not fired:
                fired.append(point)
                raise OSError('process lost after acknowledged durable write')
            return result
    c.repo.client = lambda **kw: Interrupted()
    with pytest.raises(OSError): authorize(c, 'new')
    assert fired == [fault]
    # Restart the application-level repository. Reads remain passive in every
    # intermediate phase, including after the frozen transport copy is gone.
    c.repo.client = real
    assert c.repo.list() == [old]
    new = authorize(c, 'new')
    assert c.repo.list() == [old, new]
    assert c.store.head()['value']['state']['phase'] == 'ready'


def test_project_exceeds_legacy_capacity_and_point_work_is_history_independent(owned):
    c = owned
    rows = [authorize(c, str(i)) for i in range(160)]
    assert len(c.repo.list()) == 160
    real = c.repo.client; queries = []
    class Counted:
        def query(self, operation, payload, **kw):
            queries.append((operation, payload))
            return real().query(operation, payload, **kw)
        def command(self, *args, **kw): return real(write=True).command(*args, **kw)
    c.repo.client = lambda **kw: Counted()
    assert c.repo.read(rows[0]['id']) == rows[0]
    c.repo.bind_execution(rows[0]['id'], 'turn', 'attempt')
    c.repo.mutate(rows[0]['id'], lambda row: row['modelAttempts'].append(model_attempt('physical-task')))
    assert not any(operation == 'project.dispatch.list' or operation == 'record.get' and payload['namespace'] == NAMESPACE
                   for operation, payload in queries)
    assert sum(operation.startswith('project.dispatch.') for operation, _ in queries) <= 12
    assert c.repo.find_command('159')['id'] == rows[-1]['id']


def test_missing_history_index_is_an_integrity_failure_not_silent_evidence_loss(owned):
    c = owned; row = authorize(c)
    head = c.store.head(); state = head['value']['state']
    c.store.command(head['version'], state, [{'collection': 'indexes', 'id': 'assignments:order:00000000000000000001',
        'expected_version': 1, 'delete': True}])
    with pytest.raises(StorageError) as error: c.repo.list()
    assert error.value.code == 'database_integrity'
    assert c.repo.read(row['id']) == row


def test_scope_withdrawal_retains_its_full_128_target_atomic_boundary(owned):
    c = owned
    rows = [authorize(c, str(i)) for i in range(128)]
    request = {'projectPath': c.repo.project, 'coordinatorId': 'coordinator',
        'authorizationSource': c.request['authorizationSource'], 'kind': 'shrink', 'note': 'Withdraw this scope',
        'targets': [{'assignmentId': row['id'], 'grantRevision': 1} for row in rows]}
    receipt = c.repo.revoke_scope_change(request, principal=c.principal, command_id='withdraw-all')
    assert len(receipt['revocations']) == 128
    assert all(row['grant']['revoked'] and row['grant']['revision'] == 2 for row in c.repo.list())
    assert c.repo.scope_changes() == [receipt]


def test_racing_first_mutations_preserve_both_grants_and_one_cutover(owned):
    c = owned
    barrier = threading.Barrier(2)
    real = c.repo.client
    local = threading.local()
    class Racing:
        def query(self, *args, **kwargs): return real().query(*args, **kwargs)
        def command(self, operation, payload, command_id):
            if operation == 'record.put' and not getattr(local, 'waited', False):
                local.waited = True
                barrier.wait(timeout=10)
            return real(write=True).command(operation, payload, command_id)
    c.repo.client = lambda **kw: Racing()
    with ThreadPoolExecutor(max_workers=2) as workers:
        rows = list(workers.map(lambda command: authorize(c, command), ['left', 'right']))
    assert {row['id'] for row in c.repo.list()} == {row['id'] for row in rows}
    assert c.store.head()['value']['state']['counts']['assignments'] == 2
    assert authorize(c, 'left') == rows[0]


def test_batch_replay_and_dependency_pins_survive_selected_reads(owned):
    from lib.project_dispatch.repository import ProjectDispatchError
    c = owned
    request = {name: deepcopy(c.request[name]) for name in ('projectPath', 'coordinatorId', 'authorizationSource')}
    request.update(objective='Review two independent results', budget={'maxTokens': 200, 'maxModelRequests': 4, 'deadline': 2000}, workers=[])
    for index in range(2):
        recipient = f'batch-worker-{index}'
        c.repo.conversations.rows[recipient] = {'user_id': c.repo.owner, 'settings': {'projectPath': '/project'}}
        request['workers'].append({'recipientId': recipient, 'brief': 'Inspect this part', 'model': 'model-a', 'budget': c.request['budget']})
    manifest = c.repo.authorize_batch(request, principal=c.principal, command_id='batch')
    prior = c.repo.read(manifest['assignmentIds'][0])
    c.repo.bind_execution(prior['id'], 'result-turn', 'result-attempt')
    source_turn = c.repo.turn_get
    result = {'turnId': 'result-turn', 'conversationId': prior['request']['recipientId'],
              'actor': 'assistant', 'projectionRevision': 5, 'projection': {'content': 'Result'}}
    c.repo.turn_get = lambda conv, turn, **kw: deepcopy(result) if turn == 'result-turn' else source_turn(conv, turn, **kw)
    c.repo.attempt_get = lambda ident, **kw: {'attemptId': ident, 'turnId': 'result-turn',
        'conversationId': prior['request']['recipientId'], 'taskId': 'physical-result', 'status': 'completed'}
    c.repo.record_review(prior['id'], 'accept', 'Checked exact result', 5, 1)
    child = c.repo.authorize({**c.request, 'dependencies': [{'assignmentId': prior['id'],
        'grantRevision': 1, 'resultRevision': 5}]}, c.principal, 'dependent')
    assert c.repo.validate(c.repo.admission(child['id'])) == child
    assert c.repo.read_batch(manifest['id']) == manifest
    assert c.repo.authorize_batch(request, principal=c.principal, command_id='batch') == manifest
    result['projectionRevision'] = 6
    with pytest.raises(ProjectDispatchError, match='not ready'):
        c.repo.validate(c.repo.admission(child['id']))

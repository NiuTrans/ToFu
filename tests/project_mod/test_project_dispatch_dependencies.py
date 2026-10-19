"""Bounded dependency admission/deferral/live fences; no network/native backend."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.identity import PrincipalContext
from lib.project_dispatch import admission
from lib.project_dispatch.repository import (
    NAMESPACE, ProjectDispatchError, record_stop,
)
from lib.project_dispatch.service import ProjectDispatchService
from lib.storage.errors import StorageError
from tests.project_mod.test_project_dispatch_repository import context as context

pytestmark = pytest.mark.unit


@pytest.fixture
def dependency_context(context, monkeypatch):
    c = context
    c.turns, c.attempts, c.workspaces = {}, {}, {}
    c.evidence_reads = []
    source_get = c.repo.turn_get
    def turns(conv, turn, **kw):
        c.evidence_reads.append(('turn', turn))
        return deepcopy(c.turns[turn]) if turn in c.turns else source_get(conv, turn, **kw)
    def attempts(identity, **kw):
        c.evidence_reads.append(('attempt', identity))
        return deepcopy(c.attempts.get(identity))
    c.repo.turn_get, c.repo.attempt_get = turns, attempts
    c.repo.workspace_get = lambda project, task, **kw: deepcopy(c.workspaces.get(task))
    c.births = []
    def create_turn(conv, owner, payload, **kw):
        trusted = kw['trusted_dispatch']
        trusted.checked()
        if payload['commandId'] not in c.births:
            c.births.append(payload['commandId'])
        trusted.bind_execution('dependent-turn', 'dependent-attempt')
        return SimpleNamespace(value={})
    c.service = ProjectDispatchService(c.repo, commands=SimpleNamespace(create_turn=create_turn),
                                      turn_get=turns, attempt_get=attempts)
    monkeypatch.setattr(admission, 'repository_for', lambda _: c.repo)
    return c


def completed(c, command='prerequisite', references=None, files=False, reviewed=False):
    request = deepcopy(c.request)
    recipient = 'recipient-' + command
    c.conversations.rows[recipient] = {'user_id': c.repo.owner, 'settings': {'projectPath': '/project'}}
    request['recipientId'] = recipient
    if references is not None:
        request['dependencies'] = references
    if files:
        request['profile'] = {'mode': 'files', 'tools': ['read_files'],
                              'readPaths': ['src'], 'writePaths': [], 'maxToolCalls': 1}
    row = c.repo.authorize(request, c.principal, command)
    tid, aid, task = 'turn-' + command, 'attempt-' + command, 'task-' + command
    c.repo.bind_execution(row['id'], tid, aid)
    c.turns[tid] = {'turnId': tid, 'conversationId': recipient, 'actor': 'assistant',
                    'projectionRevision': 5, 'projection': {'content': 'Result'}}
    c.attempts[aid] = {'attemptId': aid, 'turnId': tid, 'conversationId': recipient,
                       'taskId': task, 'status': 'completed'}
    if files:
        c.workspaces[task] = {'project_root': '/project', 'task_id': task, 'user_id': c.repo.owner,
                             'checkpoint_sha': 'exact-checkpoint',
                             'origin': {admission.KEY: c.repo.admission(row['id']),
                                        'delivery': {'state': 'blocked', 'checkpointSha': 'exact-checkpoint'}}}
    if reviewed:
        c.repo.record_review(row['id'], 'accept', 'Checked', 5, 1)
    return c.repo.read(row['id'])


def reference(row, **overrides):
    result = {'assignmentId': row['id'], 'grantRevision': 1, 'resultRevision': 5}
    if row['request'].get('profile', {}).get('mode') == 'files':
        result['checkpointSha'] = 'exact-checkpoint'
    return {**result, **overrides}


def dependent(c, refs, command='dependent'):
    return c.repo.authorize({**deepcopy(c.request), 'dependencies': refs}, c.principal, command)


def state(c, row):
    return c.service.view(row['id'], principal=c.principal)['dependencyStates'][0]


def test_waiting_accept_same_grant_dispatch_and_no_spend(dependency_context):
    c = dependency_context
    prior = completed(c)
    row = dependent(c, [reference(prior)])
    before = c.client.puts
    view = c.service.dispatch(row['id'], principal=c.principal)
    assert view['dispatch'] == 'deferred'
    assert view['dependencyStates'][0]['status'] == 'waiting_review'
    assert c.births == [] and c.client.puts == before
    assert c.repo.read(row['id'])['execution'] is None
    assert c.repo.read(row['id'])['modelAttempts'] == []
    with pytest.raises(ProjectDispatchError, match='not ready'):
        c.repo.validate(c.repo.admission(row['id']))
    c.service.review(prior['id'], principal=c.principal, decision='accept', note='Checked',
                     result_revision=5, expected_revision=1)
    replay = dependent(c, [reference(prior)])
    assert replay['id'] == row['id'] and replay['grant'] == row['grant']
    assert c.service.dispatch(row['id'], principal=c.principal)['dispatch'] == 'accepted'
    assert c.births == ['project-dispatch:' + row['id']]
    assert c.repo.read(row['id'])['grant']['revision'] == 1


def test_file_held_publication_then_delivered(dependency_context):
    c = dependency_context
    prior = completed(c, files=True, reviewed=True)
    row = dependent(c, [reference(prior)])
    assert state(c, row)['status'] == 'waiting_delivery'
    assert c.service.dispatch_authorized(c.repo.admission(row['id']))['dispatch'] == 'deferred'
    c.repo.request_publication(prior['id'], principal=c.principal, expected_revision=1,
                               result_revision=5, expected_checkpoint_sha='exact-checkpoint')
    assert state(c, row)['status'] == 'waiting_delivery'
    c.workspaces['task-prerequisite']['origin']['delivery']['state'] = 'delivered'
    assert state(c, row)['status'] == 'ready'
    assert c.repo.validate(c.repo.admission(row['id']))['id'] == row['id']
    assert c.service.dispatch(row['id'], principal=c.principal)['dispatch'] == 'accepted'


@pytest.mark.parametrize('change,reason', [
    ('revision', 'result_changed'), ('running', 'result_changed'), ('revoked', 'revoked'),
    ('stop', 'stopped'), ('source', 'scope_changed'), ('archived', 'scope_changed'),
    ('binding', 'scope_changed'), ('rework', 'review_rejected'), ('reject', 'review_rejected'),
    ('task', 'result_changed'), ('owner', 'result_changed'),
])
def test_live_prerequisite_changes_fail_closed(dependency_context, change, reason):
    c = dependency_context
    prior = completed(c)
    row = dependent(c, [reference(prior)])
    c.repo.record_review(prior['id'], change if change in ('rework', 'reject') else 'accept', 'Checked', 5, 1)
    if change == 'revision': c.turns['turn-prerequisite']['projectionRevision'] = 6
    if change == 'running': c.attempts['attempt-prerequisite']['status'] = 'running'
    if change == 'revoked': c.repo.revoke(prior['id'], 1)
    if change == 'stop': record_stop(7, prior['request']['recipientId'], 1001, client=c.client)
    if change == 'source': c.turn['projectionRevision'] = 4
    if change == 'archived': c.conversations.rows[prior['request']['recipientId']]['settings']['archived'] = True
    if change == 'binding': c.binding.value['availability'] = 'unavailable'
    if change == 'task': c.attempts['attempt-prerequisite']['taskId'] = ''
    if change == 'owner': c.turns['turn-prerequisite']['user_id'] = 8
    assert state(c, row)['status'] == 'invalid'
    assert state(c, row)['reason'] == reason
    with pytest.raises(ProjectDispatchError): c.repo.validate(c.repo.admission(row['id']))
    if change not in ('source', 'binding'):
        assert c.service.dispatch(row['id'], principal=c.principal)['dispatch'] == 'deferred'
    assert c.births == []


@pytest.mark.parametrize('change', ['missing_sha', 'consult_sha', 'stale_grant', 'stale_result',
                                    'duplicate_id', 'self', 'dangling', 'tenant', 'unfinished', 'extra', 'cap'])
def test_bad_dependency_grants_are_not_written(dependency_context, change):
    c = dependency_context
    prior = completed(c, files=change == 'missing_sha', reviewed=True)
    ref = reference(prior)
    refs = [ref]
    if change == 'missing_sha': ref.pop('checkpointSha')
    if change == 'consult_sha': ref['checkpointSha'] = 'forbidden'
    if change == 'stale_grant': ref['grantRevision'] = 2
    if change == 'stale_result': ref['resultRevision'] = 6
    if change == 'duplicate_id': refs.append(reference(prior, resultRevision=6))
    if change == 'self': ref['assignmentId'] = c.repo.command_identity('dependent')
    if change == 'dangling': ref['assignmentId'] = 'absent'
    if change == 'tenant':
        c.principal = PrincipalContext.user(subject_id='user:7', owner_user_id=7, tenant_id='other', scopes=['chat'])
    if change == 'unfinished': c.attempts['attempt-prerequisite']['status'] = 'running'
    if change == 'extra': ref['mutable'] = True
    if change == 'cap': refs = [reference(prior, resultRevision=n) for n in range(1, 10)]
    before = c.client.puts
    with pytest.raises(ProjectDispatchError): dependent(c, refs)
    assert c.client.puts == before and len(c.repo.list()) == 1


@pytest.mark.parametrize('change', ['self', 'forward', 'cycle', 'dangling', 'duplicate', 'malformed'])
def test_persisted_graph_corruption_rejected(dependency_context, change):
    c = dependency_context
    prior = completed(c, reviewed=True)
    row = dependent(c, [reference(prior)])
    value = c.client.rows[(NAMESPACE, c.repo.key)]['value']
    if change in ('self', 'forward', 'cycle'):
        value['assignments'][0]['request']['dependencies'] = [reference(prior if change == 'self' else row)]
    if change == 'dangling': value['assignments'][1]['request']['dependencies'][0]['assignmentId'] = 'absent'
    if change == 'duplicate': value['assignments'][1]['request']['dependencies'].append(reference(prior, resultRevision=6))
    if change == 'malformed': value['assignments'][1]['request']['dependencies'][0]['grantRevision'] = 0
    with pytest.raises(StorageError): c.repo.list()
    assert c.repo.dependency_states(row)[0]['status'] == 'unknown'
    with pytest.raises(StorageError): c.service.dispatch(row['id'], principal=c.principal)
    assert c.births == []


def test_transitive_fences_and_single_snapshot_memo(dependency_context):
    c = dependency_context
    first = completed(c, 'first', reviewed=True)
    middle = completed(c, 'middle', [reference(first)], reviewed=True)
    row = dependent(c, [reference(first), reference(middle)])
    c.evidence_reads.clear()
    reads = c.client.reads
    assert all(item['status'] == 'ready' for item in c.repo.dependency_states(row))
    assert c.evidence_reads.count(('attempt', 'attempt-first')) == 1
    assert c.evidence_reads.count(('turn', 'turn-first')) == 1
    assert c.client.reads - reads == 5  # snapshot + two Stop watermarks per prerequisite
    c.repo.revoke(first['id'], 1)
    assert all(item['status'] == 'invalid' for item in c.repo.dependency_states(row))
    with pytest.raises(ProjectDispatchError): c.repo.validate(c.repo.admission(row['id']))


def test_deadline_history_retained_but_own_deadline_enforced(dependency_context):
    c = dependency_context
    prior = completed(c, reviewed=True)
    c.now[0] = 2001
    c.request['budget']['deadline'] = 3000
    row = dependent(c, [reference(prior)])
    assert state(c, row)['status'] == 'ready'
    c.now[0] = 3001
    with pytest.raises(ProjectDispatchError, match='expired'):
        c.service.dispatch(row['id'], principal=c.principal)
    assert c.births == []


@pytest.mark.parametrize('own_change', ['stop', 'expired', 'revoked'])
def test_own_authority_checked_before_waiting_deferral(dependency_context, own_change):
    c = dependency_context
    prior = completed(c)
    row = dependent(c, [reference(prior)])
    if own_change == 'stop': record_stop(7, 'recipient', 1001, client=c.client)
    if own_change == 'expired': c.now[0] = 2001
    if own_change == 'revoked': c.repo.revoke(row['id'], 1)
    with pytest.raises(ProjectDispatchError): c.service.dispatch(row['id'], principal=c.principal)
    assert c.births == []


def test_unknown_evidence_defers_and_storage_projection_never_ready(dependency_context, monkeypatch):
    c = dependency_context
    prior = completed(c, reviewed=True)
    row = dependent(c, [reference(prior)])
    c.repo.attempt_get = lambda *a, **kw: (_ for _ in ()).throw(StorageError('database_timeout', 'unavailable'))
    assert state(c, row)['status'] == 'unknown'
    assert c.service.dispatch(row['id'], principal=c.principal)['dispatch'] == 'deferred'
    assert c.births == []
    monkeypatch.setattr(c.repo, '_load', lambda: (_ for _ in ()).throw(StorageError('database_timeout', 'unavailable')))
    assert c.repo.dependency_states(row)[0]['status'] == 'unknown'


@pytest.mark.parametrize('race', ['revoke', 'read_error'])
def test_ready_to_birth_race_fails_closed(dependency_context, monkeypatch, race):
    c = dependency_context
    prior = completed(c, reviewed=True)
    row = dependent(c, [reference(prior)])
    original = c.repo.dependency_states
    fired = []
    def states(*args, **kw):
        result = original(*args, **kw)
        if not fired:
            fired.append(True)
            if race == 'revoke': c.repo.revoke(prior['id'], 1)
            else: c.repo.attempt_get = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('storage failed'))
        return result
    monkeypatch.setattr(c.repo, 'dependency_states', states)
    with pytest.raises(ProjectDispatchError): c.service.dispatch(row['id'], principal=c.principal)
    assert c.births == [] and c.repo.read(row['id'])['execution'] is None


def test_cas_retry_lost_ack_preserves_grant_and_request(dependency_context):
    c = dependency_context
    prior = completed(c)
    c.client.forced_conflicts = 2
    c.client.uncertain_once = True
    row = dependent(c, [reference(prior)])
    assert c.client.conflicts == 2
    c.repo.revoke(prior['id'], 1)
    assert dependent(c, [reference(prior)]) == row  # durable ACK replay, not regrant
    assert state(c, row)['reason'] == 'revoked'
    assert len(c.repo.list()) == 2


def test_legacy_no_dependency_projection_unchanged(dependency_context):
    c = dependency_context
    row = c.repo.authorize(c.request, c.principal, 'legacy')
    assert 'dependencyStates' not in c.service.view(row['id'], principal=c.principal)
    assert c.repo.validate(c.repo.admission(row['id'])) == row


@pytest.mark.parametrize('change,expected', [
    ('checkpoint', 'invalid'), ('receipt_checkpoint', 'invalid'), ('publication_checkpoint', 'invalid'),
    ('carrier', 'invalid'), ('discarded', 'invalid'), ('bool_owner', 'unknown'),
    ('workspace_error', 'unknown'), ('missing_workspace', 'unknown'),
    ('no_publication', 'waiting_delivery'), ('no_receipt_sha', 'waiting_delivery'),
    ('usage', 'unknown'), ('tool', 'unknown'),
])
def test_file_readiness_requires_exact_settled_delivery_evidence(dependency_context, change, expected):
    c = dependency_context
    prior = completed(c, files=True, reviewed=True)
    row = dependent(c, [reference(prior)])
    c.repo.request_publication(prior['id'], principal=c.principal, expected_revision=1,
                               result_revision=5, expected_checkpoint_sha='exact-checkpoint')
    workspace = c.workspaces['task-prerequisite']
    workspace['origin']['delivery']['state'] = 'delivered'
    assert state(c, row)['status'] == 'ready'
    if change == 'checkpoint': workspace['checkpoint_sha'] = 'changed'
    if change == 'receipt_checkpoint': workspace['origin']['delivery']['checkpointSha'] = 'changed'
    if change == 'publication_checkpoint':
        c.client.rows[(NAMESPACE, c.repo.key)]['value']['assignments'][0]['publication']['checkpointSha'] = 'changed'
    if change == 'carrier': workspace['origin'][admission.KEY]['revision'] = 2
    if change == 'discarded': workspace['state'] = 'discarded'
    if change == 'bool_owner': workspace['user_id'] = True
    if change == 'workspace_error':
        c.repo.workspace_get = lambda *a, **kw: (_ for _ in ()).throw(StorageError('database_timeout', 'unavailable'))
    if change == 'missing_workspace': c.workspaces.clear()
    if change == 'no_publication': c.client.rows[(NAMESPACE, c.repo.key)]['value']['assignments'][0].pop('publication')
    if change == 'no_receipt_sha': workspace['origin']['delivery'].pop('checkpointSha')
    if change == 'usage':
        c.repo.mutate(prior['id'], lambda a: a['modelAttempts'].append({
            'root_task_id': 'task-prerequisite', 'attempt_ordinal': 1, 'attempt_task_id': 'physical',
            'reserved_tokens': 10, 'phase': 'dispatch_intent', 'reported_tokens': 0,
            'usage_complete': False, 'account_nonce': '0123456789abcdef0123456789abcdef', 'account_observed_tokens': 0}))
    if change == 'tool':
        c.repo.mutate(prior['id'], lambda a: a.update(toolAttempts=[{
            'root_task_id': 'task-prerequisite', 'tool_ordinal': 1, 'task_id': 'task-prerequisite',
            'tool_call_id': 'tool', 'tool_name': 'read_files', 'phase': 'intent', 'account_nonce': '0123456789abcdef0123456789abcdef'}]))
    assert state(c, row)['status'] == expected
    with pytest.raises(ProjectDispatchError): c.repo.validate(c.repo.admission(row['id']))
    assert c.service.dispatch(row['id'], principal=c.principal)['dispatch'] == 'deferred'
    assert not c.births


def test_consult_revoke_uses_publication_lock(dependency_context):
    from contextlib import contextmanager
    c = dependency_context
    prior = completed(c, reviewed=True)
    events = []
    @contextmanager
    def lock(project):
        assert project == '/project'
        events.append('locked')
        yield
        events.append('unlocked')
    original = c.repo.revoke
    def revoke(*a, **kw):
        assert events == ['locked']
        events.append('revoked')
        return original(*a, **kw)
    c.repo.revoke = revoke
    c.service.publication_lock = lock
    c.service.commands.abort_attempt = lambda *a: events.append('cancelled')
    c.service.revoke(prior['id'], principal=c.principal, expected_revision=1)
    assert events == ['locked', 'revoked', 'cancelled', 'unlocked']


def test_physical_task_and_publication_recheck_transitive_dependencies(dependency_context):
    from lib.llm.llm_errors import AbortedError
    c = dependency_context
    prior = completed(c, reviewed=True)
    row = dependent(c, [reference(prior)])
    c.repo.bind_execution(row['id'], 'dependent-turn', 'dependent-attempt')
    c.repo.revoke(prior['id'], 1)
    binding = c.repo.admission(row['id'])
    task = {'config': {admission.KEY: binding}}
    with pytest.raises(AbortedError): admission.validate_task(task, require_account=False)
    with pytest.raises(ProjectDispatchError): c.repo.validate_publication(binding, 'dependent-task')
    assert c.repo.read(row['id'])['modelAttempts'] == []


def test_eight_edges_and_128_node_graph_bound(dependency_context):
    c = dependency_context
    prior = [completed(c, 'prior-' + str(n), reviewed=True) for n in range(8)]
    row = dependent(c, [reference(item) for item in prior])
    assert len(c.repo.dependency_states(row)) == 8
    # Construct a valid bounded persisted chain without quadratic authorization.
    value = c.client.rows[(NAMESPACE, c.repo.key)]['value']
    template = deepcopy(prior[0])
    chain = [template]
    for n in range(1, 128):
        item = deepcopy(template)
        item['id'] = 'bounded-' + str(n)
        item['request']['dependencies'] = [reference(chain[-1])]
        chain.append(item)
    value['assignments'] = deepcopy(chain)
    assert len(c.repo.list()) == 128
    assert c.repo.dependency_states(chain[-1])[0]['status'] == 'ready'
    overflow = deepcopy(template)
    overflow['id'] = 'overflow'
    value['assignments'].append(overflow)
    with pytest.raises(StorageError): c.repo.list()
    assert c.repo.dependency_states(chain[-1])[0]['status'] == 'unknown'


def test_transitive_stop_and_result_changes(dependency_context):
    c = dependency_context
    first = completed(c, 'first', reviewed=True)
    middle = completed(c, 'middle', [reference(first)], reviewed=True)
    row = dependent(c, [reference(middle)])
    c.turns['turn-first']['projectionRevision'] = 6
    assert state(c, row)['reason'] == 'result_changed'
    c.turns['turn-first']['projectionRevision'] = 5
    record_stop(7, first['request']['recipientId'], 1001, client=c.client)
    assert state(c, row)['reason'] == 'stopped'


@pytest.mark.parametrize('change', ['stop', 'deadline'])
def test_lane_busy_race_never_skips_own_authority(dependency_context, change):
    from lib.turn_lifecycle import LifecycleConflict
    c = dependency_context
    prior = completed(c, reviewed=True)
    row = dependent(c, [reference(prior)])
    def busy(*a, **kw):
        if change == 'stop': record_stop(7, 'recipient', 1001, client=c.client)
        else: c.now[0] = 2001
        raise LifecycleConflict('lane_busy', 'Already occupied')
    c.service.commands.create_turn = busy
    with pytest.raises(ProjectDispatchError): c.service.dispatch(row['id'], principal=c.principal)
    assert c.births == [] and c.repo.read(row['id'])['execution'] is None


def test_file_view_reuses_injected_repository_workspace_port(dependency_context):
    c = dependency_context
    prior = completed(c, files=True)
    view = c.service.view(prior['id'], principal=c.principal)
    assert view['delivery']['checkpointSha'] == 'exact-checkpoint'
    assert view['delivery']['status'] == 'blocked'
    accepted = c.service.review(prior['id'], principal=c.principal, decision='accept',
                                note='Checked', result_revision=5, expected_revision=1)
    assert accepted['review']['decision'] == 'accept'
    assert accepted['canonicalDelivery'] is False


def test_grant_cas_conflict_rechecks_prerequisite_revocation(dependency_context):
    c = dependency_context
    prior = completed(c, reviewed=True)
    original = c.client.command
    raced = []
    def command(operation, args, command_id):
        if args['namespace'] == NAMESPACE and not raced and len(args['value']['assignments']) == 2:
            raced.append(True)
            c.repo.revoke(prior['id'], 1)
        return original(operation, args, command_id)
    c.client.command = command
    with pytest.raises(ProjectDispatchError, match='prerequisite'):
        dependent(c, [reference(prior)])
    assert c.client.conflicts == 1
    assert len(c.repo.list()) == 1
    assert c.repo.read(prior['id'])['grant']['revoked'] is True
    assert c.births == []


@pytest.fixture
def owner_one_context(dependency_context, monkeypatch):
    c = dependency_context
    previous = c.repo
    c.repo = c.repository(owner=1)
    c.repo.turn_get, c.repo.attempt_get = previous.turn_get, previous.attempt_get
    c.repo.workspace_get = previous.workspace_get
    c.service.repository = c.repo
    c.principal = PrincipalContext.user(subject_id='user:1', owner_user_id=1,
                                        tenant_id='tenant-a', scopes=['chat'])
    for metadata in c.conversations.rows.values():
        metadata['user_id'] = 1

    def conversation_get(conversation_id, *, user_id, include_messages):
        # This port is already owner-scoped; metadata may omit owner fields.
        assert user_id == 1 and not include_messages
        c.conversations.calls.append((conversation_id, user_id, include_messages))
        return deepcopy(c.conversations.rows.get(conversation_id))

    monkeypatch.setattr(c.conversations, 'get', conversation_get)
    return c


def prerequisite_owner_evidence(c, prior):
    evidence = {
        'conversation': c.conversations.rows[prior['request']['recipientId']],
        'source': c.turn,
        'result': c.turns['turn-prerequisite'],
        'attempt': c.attempts['attempt-prerequisite'],
    }
    for item in evidence.values():
        item.update(user_id=1, owner_user_id=1)
    return evidence


@pytest.mark.parametrize('evidence_kind', ['conversation', 'source', 'result', 'attempt'])
@pytest.mark.parametrize('owner_field', ['user_id', 'owner_user_id'])
@pytest.mark.parametrize('bad_owner', [True, 1.0], ids=['bool', 'float'])
def test_owner_one_rejects_untyped_evidence(owner_one_context, evidence_kind, owner_field, bad_owner):
    c = owner_one_context
    prior = completed(c, files=True, reviewed=True)
    evidence = prerequisite_owner_evidence(c, prior)
    c.repo.request_publication(prior['id'], principal=c.principal, expected_revision=1,
                               result_revision=5, expected_checkpoint_sha='exact-checkpoint')
    c.workspaces['task-prerequisite']['origin']['delivery']['state'] = 'delivered'
    binding = c.repo.admission(prior['id'])
    assert c.repo.validate_publication(binding, 'task-prerequisite')['id'] == prior['id']
    row = dependent(c, [reference(prior)])
    assert state(c, row)['status'] == 'ready'
    evidence[evidence_kind][owner_field] = bad_owner
    before = c.client.puts
    projected = state(c, row)
    assert projected['status'] == 'invalid'
    assert projected['reason'] == ('scope_changed' if evidence_kind in ('conversation', 'source')
                                   else 'result_changed')
    with pytest.raises(ProjectDispatchError):
        dependent(c, [reference(prior)], command='new-dependent')
    with pytest.raises(ProjectDispatchError):
        c.repo.validate(c.repo.admission(row['id']))
    with pytest.raises(ProjectDispatchError):
        c.repo.validate_publication(binding, 'task-prerequisite')
    if evidence_kind == 'source':
        with pytest.raises(ProjectDispatchError):
            c.service.dispatch(row['id'], principal=c.principal)
    else:
        assert c.service.dispatch(row['id'], principal=c.principal)['dispatch'] == 'deferred'
    assert c.client.puts == before and c.births == []
    assert c.repo.read(row['id'])['execution'] is None


def test_owner_one_preserves_omitted_evidence_owners(owner_one_context):
    c = owner_one_context
    prior = completed(c, files=True, reviewed=True)
    evidence = prerequisite_owner_evidence(c, prior)
    c.repo.request_publication(prior['id'], principal=c.principal, expected_revision=1,
                               result_revision=5, expected_checkpoint_sha='exact-checkpoint')
    c.workspaces['task-prerequisite']['origin']['delivery']['state'] = 'delivered'
    row = dependent(c, [reference(prior)])
    assert state(c, row)['status'] == 'ready'
    for item in (*evidence.values(), *c.conversations.rows.values()):
        item.pop('user_id', None)
        item.pop('owner_user_id', None)

    binding = c.repo.admission(prior['id'])
    assert c.repo.validate_publication(binding, 'task-prerequisite')['id'] == prior['id']
    assert state(c, row)['status'] == 'ready'
    assert c.repo.validate(c.repo.admission(row['id'])) == row
    assert dependent(c, [reference(prior)], command='new-dependent')['grant']['owner'] == 1
    assert c.service.dispatch(row['id'], principal=c.principal)['dispatch'] == 'accepted'
    assert c.births == ['project-dispatch:' + row['id']]


@pytest.mark.parametrize('phase,terminal', [
    ('future_phase', False), ('not_entered', True),
    ('executor_returned', True), ('executor_exception', True),
])
def test_tool_phase_allowlist_fences_dependencies_and_publication(dependency_context, phase, terminal):
    c = dependency_context
    prior = completed(c, files=True, reviewed=True)
    c.repo.request_publication(prior['id'], principal=c.principal, expected_revision=1,
                               result_revision=5, expected_checkpoint_sha='exact-checkpoint')
    c.workspaces['task-prerequisite']['origin']['delivery']['state'] = 'delivered'
    row = dependent(c, [reference(prior)])
    assert state(c, row)['status'] == 'ready'
    c.repo.mutate(prior['id'], lambda a: a.update(toolAttempts=[{
        'root_task_id': 'task-prerequisite', 'tool_ordinal': 1, 'task_id': 'task-prerequisite',
        'tool_call_id': 'tool', 'tool_name': 'read_files', 'phase': phase,
        'account_nonce': '0123456789abcdef0123456789abcdef'}]))
    before = c.client.puts
    binding = c.repo.admission(prior['id'])
    if terminal:
        assert state(c, row)['status'] == 'ready'
        assert c.repo.validate(c.repo.admission(row['id']))['id'] == row['id']
        assert c.repo.validate_publication(binding, 'task-prerequisite')['id'] == prior['id']
        assert dependent(c, [reference(prior)], command='new-dependent')['grant']['revision'] == 1
        assert c.service.dispatch(row['id'], principal=c.principal)['dispatch'] == 'accepted'
        assert c.births == ['project-dispatch:' + row['id']]
    else:
        assert state(c, row) == {'reference': reference(prior), 'status': 'unknown',
                                'reason': 'evidence_unavailable'}
        with pytest.raises(ProjectDispatchError):
            dependent(c, [reference(prior)], command='new-dependent')
        with pytest.raises(ProjectDispatchError, match='not ready'):
            c.repo.validate(c.repo.admission(row['id']))
        with pytest.raises(ProjectDispatchError, match='Unconfirmed tool execution'):
            c.repo.validate_publication(binding, 'task-prerequisite')
        assert c.service.dispatch(row['id'], principal=c.principal)['dispatch'] == 'deferred'
        assert c.client.puts == before and c.births == []
        assert c.repo.read(row['id'])['execution'] is None


@pytest.mark.parametrize('bad_revision', [True, 1.0], ids=['bool', 'float'])
def test_publication_rejects_untyped_result_revision(owner_one_context, bad_revision):
    c = owner_one_context
    prior = completed(c, files=True)
    c.turns['turn-prerequisite']['projectionRevision'] = 1
    c.repo.record_review(prior['id'], 'accept', 'Checked', 1, 1)
    c.repo.request_publication(prior['id'], principal=c.principal, expected_revision=1,
                               result_revision=1, expected_checkpoint_sha='exact-checkpoint')
    binding = c.repo.admission(prior['id'])
    assert c.repo.validate_publication(binding, 'task-prerequisite')['id'] == prior['id']
    c.turns['turn-prerequisite']['projectionRevision'] = bad_revision
    before = c.client.puts
    with pytest.raises(ProjectDispatchError, match='Publication execution/result identity changed'):
        c.repo.validate_publication(binding, 'task-prerequisite')
    assert c.client.puts == before and c.births == []


def test_binding_aba_invalidates_old_result_pins_and_new_grant_cannot_borrow_them(dependency_context):
    c = dependency_context
    prior = completed(c, reviewed=True)
    row = dependent(c, [reference(prior)])
    assert c.repo.dependency_states(row)[0]['status'] == 'ready'
    c.binding.value.update(conversationId='another-coordinator', revision=2)
    c.binding.value.update(conversationId='coordinator', revision=3)
    state = c.repo.dependency_states(row)[0]
    assert (state['status'], state['reason']) == ('invalid', 'scope_changed')
    with pytest.raises(ValueError):
        dependent(c, [reference(prior)], command='fresh-after-aba')
    assert c.repo.read(row['id'])['execution'] is None

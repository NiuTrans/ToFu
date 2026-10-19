"""Post-CAS review and post-delivery ACK wake reuse bounded existing grants."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.project_dispatch import recovery
from lib.project_dispatch.repository import record_stop
from lib.project_mod import delivery
from lib.storage.errors import StorageError
from tests.project_mod.test_project_dispatch_dependencies import (
    dependency_context as dependency_context, completed, dependent, reference,
)
from tests.project_mod.test_project_dispatch_repository import context as context

pytestmark = pytest.mark.unit


@pytest.fixture
def wake_context(dependency_context, monkeypatch):
    c = dependency_context
    monkeypatch.setattr(recovery, 'ProjectDispatchRepository', lambda owner, project: c.repo)
    monkeypatch.setattr(recovery, 'ProjectDispatchService', lambda repo: c.service)
    monkeypatch.setattr('lib.conversation_sync.pending_abort.was_pending_abort_after', lambda *a: False)
    return c


def accept(c, row, decision='accept'):
    return c.service.review(row['id'], principal=c.principal, decision=decision,
                            note='Checked', result_revision=5, expected_revision=1)


def test_review_immediately_retries_other_lane_and_not_unrelated_grants(wake_context):
    c = wake_context
    prior = completed(c)
    row = dependent(c, [reference(prior)])
    unrelated = c.repo.authorize(c.request, c.principal, 'unrelated')
    assert prior['request']['recipientId'] != row['request']['recipientId']
    accept(c, prior)
    assert c.births == ['project-dispatch:' + row['id']]
    bound = c.repo.read(row['id'])
    assert bound['grant'] == row['grant'] and bound['request'] == row['request']
    assert bound['execution'] is not None and bound['modelAttempts'] == []
    assert c.repo.read(unrelated['id'])['execution'] is None
    for _ in range(2): accept(c, prior)
    assert len(c.births) == 1 and c.repo.read(row['id'])['execution'] == bound['execution']


@pytest.mark.parametrize('decision', ['reject', 'rework'])
def test_nonaccept_review_does_not_notify(wake_context, monkeypatch, decision):
    c = wake_context
    prior = completed(c)
    dependent(c, [reference(prior)])
    monkeypatch.setattr(recovery, 'wake_project_dependents', lambda *a, **kw: pytest.fail('Not accepted'))
    accept(c, prior, decision)
    assert not c.births


def test_failed_review_cas_never_notifies_but_durable_ack_replay_does(wake_context, monkeypatch):
    c = wake_context
    prior = completed(c)
    row = dependent(c, [reference(prior)])
    original = c.repo.record_review
    def failure(*a, **kw):
        raise StorageError('database_timeout', 'Uncertain review ACK', True)
    monkeypatch.setattr(c.repo, 'record_review', failure)
    with pytest.raises(StorageError): accept(c, prior)
    assert not c.births
    monkeypatch.setattr(c.repo, 'record_review', original)
    c.client.uncertain_once = True
    accept(c, prior)
    assert c.births == ['project-dispatch:' + row['id']]


def test_transitive_completed_middle_does_not_hide_newly_ready_dependents(wake_context):
    c = wake_context
    first = completed(c, 'first', reviewed=True)
    middle = completed(c, 'middle', [reference(first)], reviewed=True)
    row = dependent(c, [reference(middle)])
    c.attempts['attempt-first']['status'] = 'running'
    assert c.service.dispatch(row['id'], principal=c.principal)['dispatch'] == 'deferred'
    c.attempts['attempt-first']['status'] = 'completed'
    accept(c, first)
    assert c.births == ['project-dispatch:' + row['id']]


@pytest.mark.parametrize('fence', ['stop', 'human', 'scope', 'revoked'])
def test_wake_preserves_recipient_stop_human_priority_and_scope(wake_context, fence):
    from lib.turn_lifecycle import LifecycleConflict
    c = wake_context
    prior = completed(c)
    row = dependent(c, [reference(prior)])
    if fence == 'stop': record_stop(7, 'recipient', 1001, client=c.client)
    if fence == 'scope': c.conversations.rows['recipient']['settings']['projectPath'] = '/elsewhere'
    if fence == 'revoked': c.repo.revoke(row['id'], 1)
    if fence == 'human':
        def queued_human(*a, **kw):
            raise LifecycleConflict('superseded_by_human', 'Human is queued')
        c.service.commands.create_turn = queued_human
    accept(c, prior)
    assert not c.births and c.repo.read(row['id'])['execution'] is None


def test_lost_wake_recovers_existing_grant_at_startup(wake_context, monkeypatch):
    c = wake_context
    prior = completed(c)
    row = dependent(c, [reference(prior)])
    original = c.repo.list
    monkeypatch.setattr(c.repo, 'list', lambda: (_ for _ in ()).throw(RuntimeError('Lost notification')))
    accept(c, prior)
    assert not c.births and c.repo.read(prior['id'])['review']['decision'] == 'accept'
    monkeypatch.setattr(c.repo, 'list', original)
    value = next(iter(c.client.rows.values()))['value']
    client = SimpleNamespace(query=lambda op, payload, **kw: [
        {'key': c.repo.key, 'value': deepcopy(value)}] if not payload['after_key'] else [])
    def resume(owner, project, **kw):
        return recovery.recover_project_dispatch(owner, project, service=c.service,
            was_aborted_after=lambda *a: False, clock=c.repo.clock, **kw)
    assert recovery.recover_persisted_dispatch(client=client,
        repository_factory=lambda owner, project: c.repo, recovery=resume) == 1
    assert c.births == ['project-dispatch:' + row['id']]


@pytest.fixture
def delivery_context(wake_context, monkeypatch):
    c = wake_context
    prior = completed(c, files=True, reviewed=True)
    row = dependent(c, [reference(prior)])
    workspace = c.workspaces['task-prerequisite']
    workspace.update(state='merged', workspace_path='/source', baseline_sha='base',
                     checkpoint_sha='exact-checkpoint', candidate_sha='candidate')
    workspace['origin']['delivery'].update(taskOutcome='completed', state='pending',
        phase='adoption', targetRef='refs/heads/main', conversationId='recipient-prerequisite')
    c.service.publication_lock = lambda project: nullcontext()
    c.service.publication_wake = lambda: None
    monkeypatch.setattr(delivery.state, 'get_workspace', lambda *a, **kw: deepcopy(workspace))
    def save(**kw):
        workspace['origin'].update(deepcopy(kw['patch']))
        return deepcopy(workspace['origin'])
    monkeypatch.setattr(delivery.state, 'set_workspace_meta', save)
    from pathlib import Path
    locked = []
    @contextmanager
    def lock(*a, **kw):
        locked.append(True)
        try: yield
        finally: locked.pop()
    control = SimpleNamespace(_repo_root=lambda p: Path('/project'), _repo_lock=lock,
        _now=c.repo.clock, _push=lambda *a, **kw: None,
        task_publication_checkpoint=lambda row: row['checkpoint_sha'],
        _git=lambda root, args: SimpleNamespace(returncode=0, stdout='refs/heads/main', stderr=''),
        _rev=lambda *a: 'exact-checkpoint', _porcelain=lambda *a: {'total': 0})
    monkeypatch.setattr(delivery, '_control', lambda: control)
    monkeypatch.setattr('lib.project_mod.workspace_lease.workspace_write_lease', lock)
    monkeypatch.setattr(delivery, '_publication_hold', lambda *a: None)
    original = recovery.wake_project_dependents
    def wake(*a, **kw):
        assert not locked, 'Executor birth under publication/workspace exclusion'
        assert workspace['origin']['delivery']['state'] == 'delivered'
        return original(*a, **kw)
    monkeypatch.setattr(recovery, 'wake_project_dependents', wake)
    c.prior, c.dependent, c.workspace, c.control = prior, row, workspace, control
    return c


def publish(c):
    return c.service.publish(c.prior['id'], principal=c.principal, expected_revision=1,
                            result_revision=5, expected_checkpoint_sha='exact-checkpoint')


def test_publish_candidate_is_not_delivery_and_exact_ack_wakes_once(delivery_context):
    c = delivery_context
    publish(c)
    assert not c.births
    assert c.repo.dependency_states(c.dependent)[0]['status'] == 'waiting_delivery'
    result = delivery.deliver_registered_workspace('/project', 'task-prerequisite', user_id=7)
    assert result['state'] == 'delivered'
    assert c.births == ['project-dispatch:' + c.dependent['id']]
    bound = c.repo.read(c.dependent['id'])['execution']
    delivery.deliver_registered_workspace('/project', 'task-prerequisite', user_id=7)
    assert len(c.births) == 1 and c.repo.read(c.dependent['id'])['execution'] == bound


def test_receipt_ack_loss_no_birth_until_durable_reconciliation(delivery_context, monkeypatch):
    c = delivery_context
    publish(c)
    original = delivery.state.set_workspace_meta
    def lose_ack(**kw):
        original(**kw)
        if kw['patch']['delivery']['state'] == 'delivered':
            raise StorageError('database_timeout', 'Delivered committed, ACK lost', True)
        return deepcopy(c.workspace)
    monkeypatch.setattr(delivery.state, 'set_workspace_meta', lose_ack)
    with pytest.raises(StorageError):
        delivery.deliver_registered_workspace('/project', 'task-prerequisite', user_id=7)
    assert not c.births
    monkeypatch.setattr(delivery.state, 'set_workspace_meta', original)
    delivery.deliver_registered_workspace('/project', 'task-prerequisite', user_id=7)
    assert c.births == ['project-dispatch:' + c.dependent['id']]


@pytest.mark.parametrize('changed', ['checkpoint', 'task', 'publication'])
def test_wrong_exact_delivery_evidence_cannot_wake(delivery_context, changed):
    c = delivery_context
    if changed != 'publication': publish(c)
    if changed == 'checkpoint': c.workspace['checkpoint_sha'] = 'different'
    if changed == 'task': c.attempts['attempt-prerequisite']['taskId'] = 'different-task'
    delivery.deliver_registered_workspace('/project', 'task-prerequisite', user_id=7)
    assert not c.births


def test_no_change_receiver_wakes_only_after_ack_and_outside_lock(delivery_context, monkeypatch):
    c = delivery_context
    publish(c)
    c.workspace.update(state='checkpointed')
    c.workspace['origin'].update(executionMode='isolated-task')
    c.workspace['origin']['delivery'].update(phase='checkpoint')
    monkeypatch.setattr('lib.tasks_pkg.background_commands.task_has_running_commands', lambda **kw: False)
    monkeypatch.setattr('lib.swarm.integration.task_has_running_agents', lambda **kw: False)
    monkeypatch.setattr('lib.project_mod.task_delta_delivery.reclaim_delivered_workspace', lambda row: True)
    def no_change(row, receipt):
        with c.control._repo_lock('/project'):
            return delivery._save(row, receipt, state='delivered', reason='no_changes')
    monkeypatch.setattr(delivery, '_deliver_task_delta', no_change)
    result = delivery._settle_registered(deepcopy(c.workspace), delivery._receipt(c.workspace))
    assert result['state'] == 'delivered'
    assert c.births == ['project-dispatch:' + c.dependent['id']]


from tests.storage.test_integration_delivery import repo as repo_fixture, automatic_settle
from tests.project_mod.test_project_dispatch_file_lifecycle import file_context
repo = repo_fixture


@pytest.mark.parametrize('crash_before_ack', [False, True])
def test_real_git_delivery_wakes_other_lane_only_after_receipt_ack(context, repo, monkeypatch,
                                                                 crash_before_ack):
    from pathlib import Path
    from lib.project_dispatch import admission
    from lib.project_dispatch.service import ProjectDispatchService
    from lib.tasks_pkg.workspace_delivery import bind_worker_workspace
    from lib import integration_state_repository as state
    f = file_context(context, repo, monkeypatch)
    row = f.repository.authorize(f.request, f.principal, 'file-wake')
    f.repository.bind_execution(row['id'], 'worker-turn', 'worker-attempt')
    trusted = admission.TrustedDispatch(f.repository.admission(row['id']))
    cfg = trusted.capture({}, f.owner, trusted.principal, 'recipient')
    task = {'id': 'automatic', 'convId': 'recipient', 'config': cfg, '_userId': f.owner,
            '_principalContext': trusted.principal.to_payload(), 'status': 'running'}
    bind_worker_workspace(task)
    (Path(task['config']['projectPath']) / 'allowed').mkdir()
    (Path(task['config']['projectPath']) / 'allowed' / 'result.txt').write_text('delivered bytes\n')
    f.attempt['status'] = 'completed'
    f.output.update(projectionRevision=2)
    held = automatic_settle(repo)
    assert held['state'] == 'blocked'
    f.c.conversations.rows['other-lane'] = deepcopy(f.c.conversations.rows['recipient'])
    request = deepcopy(f.request)
    request.pop('profile')
    request.update(recipientId='other-lane', brief='Consume delivered result in another lane',
        dependencies=[{'assignmentId': row['id'], 'grantRevision': 1,
                       'resultRevision': 2, 'checkpointSha': held['checkpointSha']}])
    dependent_row = f.repository.authorize(request, f.principal, 'consume-file')
    births = []
    def create(conversation_id, owner, payload, *, principal, trusted_dispatch, **kwargs):
        durable = state.find_workspace(str(f.root), 'automatic', user_id=f.owner)['origin']['delivery']
        assert durable['state'] == 'delivered'
        assert (f.root / 'allowed' / 'result.txt').read_text() == 'delivered bytes\n'
        assert conversation_id == 'other-lane' and owner == f.owner
        if payload['commandId'] not in births: births.append(payload['commandId'])
        trusted_dispatch.bind_execution('dependent-turn', 'dependent-attempt')
        return SimpleNamespace(value={})
    service = ProjectDispatchService(f.repository, commands=SimpleNamespace(create_turn=create),
        turn_get=f.turns, attempt_get=lambda *a, **kw: deepcopy(f.attempt),
        workspace_get=state.find_workspace, publication_lock=lambda root: nullcontext(),
        publication_wake=lambda: None)
    monkeypatch.setattr(recovery, 'ProjectDispatchRepository', lambda owner, project: f.repository)
    monkeypatch.setattr(recovery, 'ProjectDispatchService', lambda repo: service)
    monkeypatch.setattr('lib.conversation_sync.pending_abort.was_pending_abort_after', lambda *a: False)
    service.review(row['id'], principal=f.principal, decision='accept', note='Checked bytes',
                   result_revision=2, expected_revision=1)
    assert not births
    service.publish(row['id'], principal=f.principal, expected_revision=1,
                    result_revision=2, expected_checkpoint_sha=held['checkpointSha'])
    assert not births
    original = delivery._save
    if crash_before_ack:
        def crash(row, receipt, **updates):
            if updates.get('state') == 'delivered':
                raise RuntimeError('Injected pre-ACK crash after byte publication')
            return original(row, receipt, **updates)
        monkeypatch.setattr(delivery, '_save', crash)
        delivery.resume_pending_workspace_deliveries()
        assert not births
        assert (f.root / 'allowed' / 'result.txt').read_text() == 'delivered bytes\n'
        monkeypatch.setattr(delivery, '_save', original)
    delivery.resume_pending_workspace_deliveries()
    assert births == ['project-dispatch:' + dependent_row['id']]
    bound = f.repository.read(dependent_row['id'])
    assert bound['request'] == dependent_row['request'] and bound['grant'] == dependent_row['grant']
    assert bound['execution'] is not None and not bound['modelAttempts']
    delivery.deliver_registered_workspace(str(f.root), 'automatic', user_id=f.owner)
    assert len(births) == 1 and f.repository.read(dependent_row['id'])['execution'] == bound['execution']

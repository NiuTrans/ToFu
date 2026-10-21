"""Automatic Worker tools preserve existing result and rework graph admission."""
import pytest

from tests.project_mod.test_coordinator_continuation import run, runtime, base_runtime, context, delegate, end

pytestmark = pytest.mark.unit


def completed(r):
    worker = delegate(r); end(r, worker)
    workspace = r.workspaces[r.attempts[worker['execution']['attemptId']]['taskId']]
    workspace.update(checkpoint_sha='a' * 40, state='integrated')
    workspace['origin']['delivery']['checkpointSha'] = workspace['checkpoint_sha']
    return worker, {'assignmentId': worker['id'], 'grantRevision': worker['grant']['revision'],
        'resultRevision': 1, 'checkpointSha': workspace['checkpoint_sha']}


def test_automatic_dependent_waits_for_review_and_publication_then_reuses_its_grant(run):
    r = run; first, pin = completed(r)
    args = {**r.args, 'idempotency_key': 'dependent', 'dependencies': [pin]}
    pending = r.call('delegate_project_work', **args)
    assert pending['dispatch'] == 'deferred', pending
    row = r.c.repo.read(pending['assignment_id'])
    assert row['request']['dependencies'] == [pin] and row['execution'] is None
    assert len(r.calls) == 1
    r.service.publication_wake = lambda: None
    review = r.call('review_project_work', assignment_id=first['id'], result_revision=1,
        decision='accept', note='Verified exact code', checkpoint_sha=pin['checkpointSha'])
    assert review['canonicalDelivery'], review
    ready = r.call('delegate_project_work', **args)
    assert ready['dispatch'] == 'accepted' and ready['assignment_id'] == pending['assignment_id'], ready
    assert len(r.created) == len(r.calls) == 2
    assert r.call('delegate_project_work', **args)['assignment_id'] == pending['assignment_id']
    assert len(r.calls) == 2


@pytest.mark.parametrize('violation', ['result', 'checkpoint', 'grant'])
def test_stale_dependency_pin_never_starts_worker(run, violation):
    r = run; _, pin = completed(r)
    if violation == 'result': pin['resultRevision'] += 1
    if violation == 'grant': pin['grantRevision'] += 1
    if violation == 'checkpoint': pin['checkpointSha'] = 'b' * 40
    denied = r.call('delegate_project_work', **{**r.args, 'idempotency_key': 'bad', 'dependencies': [pin]})
    assert 'error' in denied and len(r.calls) == 1
    assert len(r.c.repo.list()) == 1


def test_automatic_rework_records_exact_link_and_replays_without_duplicate_birth(run):
    r = run; first, pin = completed(r); pin.pop('checkpointSha')
    args = {**r.args, 'idempotency_key': 'repair', 'rework_of': pin}
    assert 'error' in r.call('delegate_project_work', **args)
    assert len(r.calls) == 1
    reviewed = r.call('review_project_work', assignment_id=first['id'], result_revision=1,
                     decision='rework', note='Repair the missing acceptance check')
    assert reviewed['review']['decision'] == 'rework'
    repair = r.call('delegate_project_work', **args)
    assert repair['dispatch'] == 'accepted', repair
    assert r.c.repo.read(repair['assignment_id'])['request']['reworkOf'] == pin
    assert r.call('delegate_project_work', **args) == repair and len(r.calls) == 2

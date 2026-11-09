"""Accepted Worker checkpoints must not become permanently unpublishable.

A completed Worker Turn can gain a later projection revision without new code
(for example a background command completion injected after the review). The
pinned publication then no longer matches the current result and the receiver
holds it. The hold must name that cause and the source Coordinator must be able
to re-affirm the exact same checkpoint at the current result revision.
"""
import pytest

from lib.project_dispatch.repository import ProjectDispatchError
from tests.project_mod.test_coordinator_continuation import run as run, runtime, base_runtime, context, delegate, end
from tests.project_mod.test_coordinator_dependency_links import completed

pytestmark = pytest.mark.unit


def _accepted_then_drifted(r):
    first, pin = completed(r)
    r.service.publication_wake = lambda: None
    review = r.call('review_project_work', assignment_id=first['id'], result_revision=1,
                    decision='accept', note='Verified exact code', checkpoint_sha=pin['checkpointSha'])
    assert 'error' not in review, review
    assert review['canonical_delivery'], review
    r.turns[first['execution']['turnId']]['projectionRevision'] = 3
    return first, pin


def test_result_drift_after_acceptance_names_its_cause(run):
    from lib.project_dispatch.publication import publication_reason
    r = run; first, pin = _accepted_then_drifted(r)
    binding = r.c.repo.admission(first['id'])
    task_id = first['execution']['taskId']
    with pytest.raises(ProjectDispatchError, match='result revision changed'):
        r.c.repo.validate_publication(binding, task_id)
    workspace = r.workspaces[task_id]
    row = {**workspace, 'task_id': task_id, 'user_id': binding['owner'],
           'project_root': binding['projectPath'], 'origin': {**workspace['origin'], 'pinned': True}}
    from lib.project_dispatch import admission
    row['origin'][admission.KEY] = binding
    assert publication_reason(row, {}) == 'project_publication_result_changed'


def test_coordinator_reaffirms_same_checkpoint_at_current_result(run):
    r = run; first, pin = _accepted_then_drifted(r)
    task_id = first['execution']['taskId']
    woken = []
    r.service.publication_wake = lambda: woken.append(True)
    again = r.call('review_project_work', assignment_id=first['id'], result_revision=3,
                   decision='accept', note='Re-verified after late output', checkpoint_sha=pin['checkpointSha'])
    assert 'error' not in again, again
    row = r.c.repo.read(first['id'])
    assert row['review']['resultRevision'] == row['publication']['resultRevision'] == 3
    assert row['publication']['checkpointSha'] == pin['checkpointSha']
    assert r.c.repo.validate_publication(r.c.repo.admission(first['id']), task_id)['id'] == first['id']
    assert woken


@pytest.mark.parametrize('change', ['checkpoint', 'decision', 'stale_revision', 'no_checkpoint'])
def test_reaffirmation_never_changes_what_was_accepted(run, change):
    r = run; first, pin = _accepted_then_drifted(r)
    arguments = {'assignment_id': first['id'], 'result_revision': 3, 'decision': 'accept',
                 'note': 'Re-verified', 'checkpoint_sha': pin['checkpointSha']}
    if change == 'checkpoint':
        arguments['checkpoint_sha'] = 'b' * 40
    elif change == 'decision':
        arguments.update(decision='rework'); arguments.pop('checkpoint_sha')
    elif change == 'stale_revision':
        arguments['result_revision'] = 1; arguments['note'] = 'different'
    else:
        arguments.pop('checkpoint_sha')
    assert 'error' in r.call('review_project_work', **arguments)
    row = r.c.repo.read(first['id'])
    assert row['review']['resultRevision'] == row['publication']['resultRevision'] == 1


@pytest.mark.parametrize('checkpoint', [True, False])
def test_checkpoint_pin_retires_that_workers_held_receipt(run, checkpoint):
    r = run; first, pin = completed(r)
    r.service.publication_wake = lambda: None
    released = []
    r.service.publication_release = (
        lambda project, task_id, *, user_id: released.append((project, task_id, user_id)))
    arguments = {'assignment_id': first['id'], 'result_revision': 1, 'decision': 'accept',
                 'note': 'Verified exact code'}
    if checkpoint:
        arguments['checkpoint_sha'] = pin['checkpointSha']
    assert 'error' not in r.call('review_project_work', **arguments)
    task_id = first['execution']['taskId']
    assert released == ([(r.c.repo.project, task_id, r.c.repo.owner)] if checkpoint else [])


def test_a_publication_hold_names_its_hidden_cause_once_per_cause(monkeypatch, caplog):
    import logging
    from types import SimpleNamespace
    from lib import integration_control
    from lib.project_dispatch import admission, publication

    refusal = ['Project publication requires explicit human authorization']
    binding = {'owner': 1, 'projectPath': '/project', 'assignmentId': 'assignment',
               'recipientId': 'worker', 'revision': 1}

    class Repository:
        def validate_publication(self, binding, task_id):
            if refusal[0]:
                raise ProjectDispatchError(refusal[0])
            return {'id': 'assignment', 'publication': {'checkpointSha': 'c' * 40},
                    'grant': {'owner': 1, 'revision': 1, 'revoked': False},
                    'request': {'projectPath': '/project', 'recipientId': 'worker',
                                'profile': {'mode': 'project', 'writePaths': ['.']}}}

    monkeypatch.setattr(publication, '_LOGGED_HOLD_CAUSES', {})
    monkeypatch.setattr(admission, 'repository_for', lambda binding: Repository())
    monkeypatch.setattr(integration_control, '_git', lambda *a, **k: SimpleNamespace(stdout=''))
    row = {'task_id': 'task', 'user_id': 1, 'project_root': '/project', 'checkpoint_sha': 'c' * 40,
           'origin': {admission.KEY: binding, 'sourceSnapshot': 'b' * 40}}
    reason = 'project_publication_authority_missing'
    # The receipt already carries this hold, e.g. written by an earlier process.
    held = {'state': 'blocked', 'reason': reason}
    with caplog.at_level(logging.WARNING, logger=publication.__name__):
        assert publication.publication_reason(row, held) == reason
        # The receiver's later retries of the same cause stay quiet.
        assert publication.publication_reason(row, held) == reason
        refusal[0] = 'Grant withdrawn by durable conversation Stop'
        assert publication.publication_reason(row, held) == reason
        refusal[0] = ''
        assert publication.publication_reason(row, held) == ''
        # A hold that clears and returns names its cause again.
        refusal[0] = 'Grant withdrawn by durable conversation Stop'
        assert publication.publication_reason(row, held) == reason
    causes = [record.getMessage() for record in caplog.records]
    assert len(causes) == 3, causes
    assert 'requires explicit human authorization' in causes[0]
    assert all('durable conversation Stop' in cause for cause in causes[1:])

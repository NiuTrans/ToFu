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
    assert review['canonicalDelivery'], review
    r.turns[first['execution']['turnId']]['projectionRevision'] = 3
    return first, pin


def test_result_drift_after_acceptance_names_its_cause(run):
    from lib.project_dispatch.publication import publication_reason
    r = run; first, pin = _accepted_then_drifted(r)
    binding = r.c.repo.admission(first['id'])
    task_id = r.attempts[first['execution']['attemptId']]['taskId']
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
    task_id = r.attempts[first['execution']['attemptId']]['taskId']
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

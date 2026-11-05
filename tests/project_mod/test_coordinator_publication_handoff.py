"""Human steering retains publication authority for already delegated work."""
import pytest

from lib.project_dispatch.admission import KEY
from tests.project_mod.test_coordinator_continuation import (
    run as run, runtime as runtime, base_runtime as base_runtime, context as context,
    delegate, end,
)
from tests.project_mod.test_coordinator_worker_handoff import next_human_attempt

pytestmark = pytest.mark.unit


def completed_checkpoint(r):
    row = delegate(r)
    end(r, row, delivered=False)
    task_id = r.attempts[row['execution']['attemptId']]['taskId']
    binding = r.c.repo.admission(row['id'])
    r.workspaces[task_id] = {
        'task_id': task_id, 'user_id': r.c.repo.owner, 'project_root': r.c.repo.project,
        'checkpoint_sha': 'a' * 40, 'state': 'completed', 'origin': {KEY: binding},
    }
    return row, task_id, binding


def review(r, row):
    return r.call('review_project_work', assignment_id=row['id'], result_revision=1,
                  decision='accept', note='Verified original checkpoint', checkpoint_sha='a' * 40)


@pytest.mark.parametrize('new_input', [False, True])
def test_resumed_coordinator_review_can_publish_original_checkpoint(run, new_input):
    r = run
    row, task_id, binding = completed_checkpoint(r)
    next_human_attempt(r, new_input=new_input)
    result = review(r, row)
    assert 'error' not in result, result
    validated = r.c.repo.validate_publication(binding, task_id)
    assert validated['publication']['checkpointSha'] == 'a' * 40
    assert validated['publication']['subject'] == 'coordinator:' + row['request']['delegation']['taskId']
    assert validated['request'] == row['request'] and validated['grant'] == row['grant']
    assert len(r.created) == len(r.calls) == 1


def test_authorized_repeat_review_repairs_preexisting_misattributed_publication(run):
    r = run
    row, task_id, binding = completed_checkpoint(r)
    assert 'error' not in review(r, row)
    next_human_attempt(r, new_input=True)

    def retain_old_marker(value):
        # Reproduce the durable receipt left by the old review implementation.
        retained = r.c.repo._find(value, row['id'])
        retained['publication']['subject'] = 'coordinator:' + r.task['id']

    r.c.repo._change(retain_old_marker)
    prior = r.c.repo.read(row['id'])
    assert 'error' not in review(r, row)
    validated = r.c.repo.validate_publication(binding, task_id)
    assert validated['publication'] == {
        **prior['publication'], 'subject': 'coordinator:' + row['request']['delegation']['taskId']}
    assert validated['review'] == prior['review']
    assert validated['request'] == prior['request'] and validated['grant'] == prior['grant']
    r.c.repo.revoke(row['id'], row['grant']['revision'])
    assert 'error' in review(r, row)

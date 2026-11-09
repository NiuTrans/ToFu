"""Human steering retains publication authority for already delegated work."""
from copy import deepcopy

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
    task_id = row['execution']['taskId']
    binding = r.c.repo.admission(row['id'])
    r.workspaces[task_id] = {
        'task_id': task_id, 'user_id': r.c.repo.owner, 'project_root': r.c.repo.project,
        'checkpoint_sha': 'a' * 40, 'state': 'completed',
        'origin': {KEY: binding, 'delivery': {'state': 'delivered', 'taskOutcome': 'completed'}},
    }
    return row, task_id, binding


def review(r, row):
    return r.call('review_project_work', assignment_id=row['id'], result_revision=1,
                  decision='accept', note='Verified original checkpoint', checkpoint_sha='a' * 40)


def test_alias_project_keeps_the_coordinator_and_reviews_the_physical_workspace(run, tmp_path, monkeypatch):
    from lib.conversations import coordinator_workers
    from lib.project_dispatch.admission import require_isolated_worker_root

    r = run
    physical = tmp_path / 'physical'
    physical.mkdir()
    project = tmp_path / 'project'
    project.symlink_to(physical, target_is_directory=True)
    project_key = str(project)
    r.c.binding.value['projectPath'] = project_key
    monkeypatch.setattr(r.c.binding, 'read', lambda path: deepcopy(r.c.binding.value))
    r.c.request['projectPath'] = project_key
    for conversation in r.c.conversations.rows.values():
        conversation['settings']['projectPath'] = project_key
    r.task['config']['projectPath'] = project_key
    r.attempts['coordinator-attempt']['config']['projectPath'] = project_key
    r.task.pop('_coordinatorDispatchScope', None)
    previous = r.c.repo
    r.c.repo = type(previous)(previous.owner, project_key, client=previous.client,
        conversations=previous.conversations, binding_repository=r.c.binding,
        turn_get=previous.turn_get, attempt_get=previous.attempt_get, clock=previous.clock,
        storage_version=previous.storage_version)
    r.service.repository = r.c.repo
    r.service.publication_wake = lambda: None
    monkeypatch.setattr(coordinator_workers, 'require_isolated_worker_root', require_isolated_worker_root)

    row, task_id, binding = completed_checkpoint(r)
    r.workspaces[task_id]['project_root'] = str(physical)
    def workspace_get(root, identity, *, user_id):
        assert root == str(physical) and user_id == r.c.repo.owner
        return deepcopy(r.workspaces.get(identity))
    r.c.repo.workspace_get = workspace_get
    result = review(r, row)
    assert 'error' not in result, result
    validated = r.c.repo.validate_publication(binding, task_id)
    assert validated['request']['projectPath'] == project_key
    assert validated['grant'] == row['grant']
    assert validated['publication']['checkpointSha'] == 'a' * 40
    assert r.task['convId'] == 'coordinator' and len(r.created) == len(r.calls) == 1
    assert project.is_symlink() and (physical / '.git').is_dir()


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

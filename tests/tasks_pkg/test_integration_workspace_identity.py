"""Integration tools address physical registrations, while Checkers address work items."""
import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.project_mod import delivery
from tests.storage.test_integration_delivery import (
    automatic_workspace, git, repo as repo,
)

pytestmark = pytest.mark.unit


def call_tool(repo, monkeypatch, work, operation, args):
    from lib.conversations import project_brain
    from lib.tasks_pkg.handlers.misc import _brain

    root, _, owner = repo
    task = {'id': 'automatic', 'convId': 'conv', '_userId': owner,
            '_workspaceDelivery': {'repositories': [{
                'canonicalRoot': str(root), 'workspacePath': str(work),
                'workId': 'automatic', 'kind': 'owned',
                'ownerTaskId': 'automatic', 'acknowledged': True,
            }]}}
    checkers = []
    monkeypatch.setattr(project_brain, 'ensure_work_item', lambda *a, **k: 'pw_logical')
    monkeypatch.setattr(project_brain, 'run_all_enabled_checkers',
                        lambda path, **kwargs: checkers.append((path, kwargs)) or [])
    monkeypatch.setattr(_brain, 'simple_call',
                        lambda task, name, arguments, *a, executor, **k: executor(name, arguments))
    result = _brain._handle_integration_tool(
        task, None, operation, 'call', args, 1, {}, {}, str(work), True)
    return result, checkers


def test_checkpoint_uses_physical_task_and_canonical_repository(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('owned task\n')
    before = git(root, 'rev-parse', 'HEAD')
    result, checkers = call_tool(repo, monkeypatch, work, 'integration_checkpoint',
                                 {'note': 'verified', 'task_id': 'pw_logical'})
    assert result.startswith('Checkpoint saved for automatic:'), result
    assert checkers == []
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    assert git(root, 'show', row['checkpoint_sha'] + ':task.txt') == 'owned task'
    assert git(root, 'rev-parse', 'HEAD') == before
    assert not (root / 'task.txt').exists()


def test_automatic_submit_keeps_candidate_queue_for_explicit_workspaces(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('owned task\n')
    before = git(root, 'rev-parse', 'HEAD')
    result, checkers = call_tool(repo, monkeypatch, work, 'integration_submit',
                                 {'summary': 'completed and verified'})
    assert 'automatic task delivery' in result, result
    assert checkers == []
    assert state.get_workspace(str(root), 'automatic', user_id=owner)['state'] == 'checkpointed'
    assert not ic.process_ready_once()
    assert git(root, 'rev-parse', 'HEAD') == before
    delivery.settle_task_workspace(str(root), 'automatic', user_id=owner, outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    receipt = state.get_workspace(str(root), 'automatic', user_id=owner)['origin']['delivery']
    assert receipt['state'] == 'delivered', receipt
    assert (root / 'task.txt').read_text() == 'owned task\n'


def test_explicit_submit_preserves_candidate_queue(repo, monkeypatch):
    root, work, owner = repo
    delivery.bind_task_workspace(str(work), 'automatic', user_id=owner, conversation_id='conv')
    (work / 'task.txt').write_text('explicit candidate\n')
    before = git(root, 'rev-parse', 'HEAD')
    result, _ = call_tool(repo, monkeypatch, work, 'integration_submit',
                          {'summary': 'explicit candidate submission'})
    assert result.startswith('Submitted automatic for HUMAN review'), result
    assert state.get_workspace(str(root), 'automatic', user_id=owner)['state'] == 'ready'
    assert git(root, 'rev-parse', 'HEAD') == before
    assert not (root / 'task.txt').exists()

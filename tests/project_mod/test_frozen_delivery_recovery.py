"""Frozen task checkpoints recover independently of mutable source admission."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import threading

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.project_mod import delivery
from lib.project_mod.workspace_lease import workspace_file_write_lease, workspace_write_lease
from tests.storage.test_integration_delivery import (
    automatic_settle, automatic_workspace, git, repo as repo,
)

pytestmark = pytest.mark.unit


@contextmanager
def held_source(work, *, metadata=False):
    entered, release = threading.Event(), threading.Event()

    def hold():
        admission = (workspace_write_lease(str(work)) if metadata else
                     workspace_file_write_lease(str(work / 'later.txt')))
        with admission:
            entered.set()
            assert release.wait(15)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(hold)
        try:
            assert entered.wait(5)
            yield
        finally:
            release.set()
            future.result(timeout=5)


def queue_completion(root, monkeypatch, owner):
    with monkeypatch.context() as patch:
        patch.setattr(delivery, '_settle_registered', lambda row, receipt: receipt)
        return delivery.settle_task_workspace(str(root), 'automatic', user_id=owner, outcome='completed')


def receipt(root):
    return state.get_workspace(str(root), 'automatic', user_id=1)['origin']['delivery']


def test_frozen_checkpoint_recovers_while_source_is_busy(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('captured task\n')
    queue_completion(root, monkeypatch, owner)
    ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    delivery._save(row, delivery._receipt(row), checkpointSha=row['checkpoint_sha'])
    (work / 'later.txt').write_text('uncaptured source work\n')
    with held_source(work):
        assert delivery.resume_pending_workspace_deliveries()
        result = receipt(root)
        assert result['state'] == 'delivered', result
        assert (root / 'task.txt').read_text() == 'captured task\n'
        assert not (root / 'later.txt').exists()
    assert (work / 'later.txt').read_text() == 'uncaptured source work\n'


@pytest.mark.parametrize('metadata', [False, True])
def test_source_contention_preserves_frozen_conflict_evidence(repo, monkeypatch, metadata):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('task version\n')
    (root / 'base.txt').write_text('canonical version\n')
    first = automatic_settle(repo)
    assert first['state'] == 'blocked'
    assert first['retry']['condition'] == 'conflict_inputs_changed'
    before = git(root, 'rev-parse', 'HEAD')
    with held_source(work, metadata=metadata):
        assert not delivery.resume_pending_workspace_deliveries()
        assert receipt(root) == first
    assert git(root, 'rev-parse', 'HEAD') == before


def test_failed_release_validation_no_longer_prevents_workspace_delivery(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('task\n')
    queue_completion(root, monkeypatch, owner)
    ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    from lib.project_mod.delivery_retry import retry_evidence
    source = ic._alternate_index_checkpoint(root, ic._rev(root, 'HEAD'), 'automatic')
    retry = retry_evidence('validation_inputs_changed', '0' * 64,
                           row['origin']['sourceSnapshot'], row['checkpoint_sha'], source)
    delivery._save(row, delivery._receipt(row), state='failed', checkpointSha=row['checkpoint_sha'],
                   reason='Gate failed: legacy verification', retry=retry)
    with held_source(work):
        assert delivery.resume_pending_workspace_deliveries()
    assert receipt(root)['state'] == 'delivered', (receipt(root).get('reason'), receipt(root).get('taskOutcome'), receipt(root).get('phase'))
    assert (root / 'task.txt').read_text() == 'task\n'


def test_initial_capture_still_requires_source_writer_admission(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('not captured\n')
    queue_completion(root, monkeypatch, owner)
    with held_source(work):
        delivery.resume_pending_workspace_deliveries()
        assert receipt(root)['reason'] == 'workspace_busy'
        assert state.get_workspace(str(root), 'automatic', user_id=owner)['checkpoint_sha'] == ''
    assert not (root / 'task.txt').exists()


def test_recovery_restores_conflict_cause_overwritten_by_busy_receipt(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('task version\n')
    (root / 'base.txt').write_text('canonical version\n')
    first = automatic_settle(repo)
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    delivery._save(row, first, state='blocked', phase='checkpoint', reason='workspace_busy')
    assert delivery.resume_pending_workspace_deliveries()
    restored = receipt(root)
    assert restored['retry'] == first['retry']
    assert restored['phase'] == 'workspace_adoption'
    assert restored['state'] == 'blocked'
    assert restored['reason'].startswith('task_delta_conflict:')
    assert not delivery.resume_pending_workspace_deliveries()


@pytest.mark.parametrize('kind', ['commands', 'agents'])
def test_frozen_checkpoint_still_waits_for_live_descendants(repo, monkeypatch, kind):
    from lib.tasks_pkg import background_commands
    from lib.swarm import integration

    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('task\n')
    queue_completion(root, monkeypatch, owner)
    ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    delivery._save(row, delivery._receipt(row), checkpointSha=row['checkpoint_sha'])
    if kind == 'commands':
        monkeypatch.setattr(background_commands, 'task_has_running_commands', lambda **k: True)
    else:
        monkeypatch.setattr(integration, 'task_has_running_agents', lambda **k: True)
    with held_source(work):
        delivery.resume_pending_workspace_deliveries()
        result = receipt(root)
        assert result['reason'] == ('task_background_commands_running' if kind == 'commands'
                                    else 'task_child_agents_running'), result
    assert not (root / 'task.txt').exists()

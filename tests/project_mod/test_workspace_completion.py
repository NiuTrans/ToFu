"""Completed isolated work reaches the configured Git checkpoint and retires safely."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from lib import integration_control as ic, integration_state_repository as state
from lib.project_mod import delivery, task_delta_delivery
from lib.project_mod.workspace_lease import WorkspaceBusyError, workspace_file_write_lease
from lib.tasks_pkg import workspace_delivery
from tests.storage.test_integration_delivery import automatic_workspace, git, repo as repo

pytestmark = pytest.mark.unit


def finish(root):
    return delivery.settle_task_workspace(str(root), 'automatic', user_id=1, outcome='completed')


def test_delivered_clone_is_reclaimed_with_checkpoint_recovery(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('finished task\n')
    result = finish(root)
    assert result['state'] == 'delivered', result
    assert not work.exists()
    assert git(root, 'show', result['checkpointSha'] + ':task.txt') == 'finished task'
    assert (root / 'task.txt').read_text() == 'finished task\n'
    assert finish(root) == result
    assert state.get_workspace(str(root), 'automatic', user_id=owner)['checkpoint_sha'] == result['checkpointSha']


def test_source_cleanup_retries_lost_ack_without_reapplying_delivery(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('finished task\n')
    original = state.set_workspace_meta

    def lose_ack(**kwargs):
        if 'sourceRetirement' in kwargs.get('patch', {}):
            raise RuntimeError('cleanup ACK unavailable')
        return original(**kwargs)

    monkeypatch.setattr(state, 'set_workspace_meta', lose_ack)
    assert finish(root)['state'] == 'delivered'
    assert not work.exists()
    monkeypatch.setattr(state, 'set_workspace_meta', original)
    monkeypatch.setattr(delivery, '_deliver_task_delta', lambda *a: pytest.fail('replayed delivered bytes'))
    delivery.resume_pending_workspace_deliveries()
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    assert row['origin']['sourceRetirement'] == 'reclaimed'


@pytest.mark.parametrize('mode', ['enabled', 'disabled', 'failing_gate'])
def test_recovered_delivery_commits_after_the_original_turn_has_finished(repo, monkeypatch, mode):
    root, _, owner = repo
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'false' if mode == 'disabled' else 'true')
    if mode == 'failing_gate':
        git(root, 'config', '--local', 'tofu.linearCheckpointTestCommand', 'python3 -c "raise SystemExit(1)"')
    before = git(root, 'rev-parse', 'HEAD')
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('finished task\n')
    original = delivery._deliver_task_delta
    monkeypatch.setattr(delivery, '_deliver_task_delta', lambda row, receipt: delivery._save(
        row, receipt, state='blocked', phase='workspace_adoption', reason='workspace_busy'))
    assert finish(root)['state'] == 'blocked'
    monkeypatch.setattr(delivery, '_deliver_task_delta', original)
    assert delivery.resume_pending_workspace_deliveries()
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    if mode == 'enabled':
        assert git(root, 'show', 'HEAD:task.txt') == 'finished task'
        assert git(root, 'status', '--porcelain') == ''
        assert row['origin']['deliveryCheckpoint']['repositories'][0]['stableUpdated']
    else:
        assert git(root, 'rev-parse', 'HEAD') == before
        assert (root / 'task.txt').read_text() == 'finished task\n'
        if mode == 'disabled':
            assert row['origin']['deliveryCheckpoint']['status'] == 'disabled'
        else:
            assert not row['origin']['deliveryCheckpoint']['repositories'][0]['stableUpdated']
    head = git(root, 'rev-parse', 'HEAD')
    assert not delivery.resume_pending_workspace_deliveries()
    assert git(root, 'rev-parse', 'HEAD') == head


def test_busy_source_cleanup_retries_after_writer_retires(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('finished task\n')
    original = task_delta_delivery.reclaim_delivered_workspace
    monkeypatch.setattr(task_delta_delivery, 'reclaim_delivered_workspace',
                        lambda row: (_ for _ in ()).throw(RuntimeError('temporary retirement failure')))
    assert finish(root)['state'] == 'delivered'
    assert work.exists()
    monkeypatch.setattr(task_delta_delivery, 'reclaim_delivered_workspace', original)
    delivery.resume_pending_workspace_deliveries()
    assert not work.exists()
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    assert row['origin']['sourceRetirement'] == 'reclaimed'


@pytest.mark.parametrize('late', ['file', 'ignored', 'index'])
def test_reclamation_preserves_uncaptured_work(repo, monkeypatch, late):
    root, _, _ = repo
    (root / '.gitignore').write_text('private-output\n')
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('finished task\n')
    original = delivery._deliver_task_delta

    def publish(*args):
        result = original(*args)
        if late == 'index':
            (work / 'base.txt').write_text('staged only\n')
            git(work, 'add', 'base.txt')
            (work / 'base.txt').write_text('base\n')
        else:
            (work / ('private-output' if late == 'ignored' else 'later.txt')).write_text('keep me\n')
        return result

    monkeypatch.setattr(delivery, '_deliver_task_delta', publish)
    assert finish(root)['state'] == 'delivered'
    assert work.exists()
    if late == 'index':
        assert git(work, 'show', ':base.txt') == 'staged only'
    else:
        assert (work / ('private-output' if late == 'ignored' else 'later.txt')).read_text() == 'keep me\n'


def test_unrelated_file_writer_progresses_during_delivery_capture(repo, monkeypatch):
    root, _, _ = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('finished task\n')
    entered, release = threading.Event(), threading.Event()
    original = ic._alternate_index_checkpoint

    def capture(checkout, *args, **kwargs):
        if Path(checkout) == root:
            entered.set()
            assert release.wait(15)
        return original(checkout, *args, **kwargs)

    monkeypatch.setattr(ic, '_alternate_index_checkpoint', capture)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(finish, root)
        try:
            assert entered.wait(15)
            with pytest.raises(WorkspaceBusyError):
                with workspace_file_write_lease(root / 'task.txt', timeout=0):
                    pytest.fail('overlapping writer was admitted')
            with workspace_file_write_lease(root / 'other.txt', timeout=0):
                (root / 'other.txt').write_text('peer work\n')
        finally:
            release.set()
        assert result.result(timeout=20)['state'] == 'delivered'
    assert (root / 'other.txt').read_text() == 'peer work\n'


def test_completed_delivery_reaches_opted_in_checkpoint_through_commit_round(repo, monkeypatch):
    from lib import file_history as history
    import lib.project_mod as project_mod
    from lib.tasks_pkg.commit_round import _commit, _derive

    root, _, _ = repo
    git(root, 'config', '--local', 'tofu.linearCheckpoint', 'true')
    work = automatic_workspace(repo, monkeypatch)
    (work / 'task.txt').write_text('finished task\n')
    task = {'id': 'automatic', 'convId': 'conv', '_userId': 1, 'status': 'done',
            'finishReason': 'stop', 'config': {'projectPath': str(root)}, 'toolRounds': []}
    events = []
    monkeypatch.setattr(_derive, 'derive_round_modified_files', lambda *a, **kw: ([], 0, False))
    monkeypatch.setattr(history, 'is_enabled', lambda: False)
    monkeypatch.setattr(project_mod, 'get_modifications', lambda *a, **kw: [])
    monkeypatch.setattr(_commit, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(_commit, '_patch_turn_projection_with_file_list', lambda *a, **kw: None)
    _commit._run_commit_round_async(task, str(root), None, False)
    event = next(event for event in events if event['type'] == 'round_committed')
    assert event['workspaceDelivery']['repositories'][0]['deliveryStatus'] == 'delivered', event
    assert git(root, 'show', 'HEAD:task.txt') == 'finished task'
    assert git(root, 'status', '--porcelain') == ''
    assert event['linearGitCheckpoint']['repositories'][0]['stableUpdated']


@pytest.mark.parametrize('status,mode', [('pending', 'working_tree'), ('blocked', 'working_tree'),
                                       ('preserved', 'working_tree'), ('delivered', 'committed')])
def test_unconfirmed_or_committed_delivery_does_not_borrow_shared_checkpoint(repo, status, mode):
    root, work, _ = repo
    receipt = {'repositories': [{'canonicalRoot': str(root), 'workspacePath': str(work),
                                 'deliveryStatus': status, 'deliveryPublicationMode': mode}]}
    assert workspace_delivery.checkpoint_roots_without_workspaces([str(root), str(work)], receipt) == []

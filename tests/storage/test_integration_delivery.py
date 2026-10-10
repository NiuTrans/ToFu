"""Registered task checkpoints are not delivered until canonical adoption."""
import subprocess
import pytest

from lib import integration_control as ic
from lib.project_mod import delivery
from lib import integration_state_repository as state

pytestmark = pytest.mark.unit


def git(root, *args):
    cp = subprocess.run(['git', '-C', str(root), *args], text=True,
                        capture_output=True, timeout=20)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    from lib.storage import StorageSupervisor
    from lib.storage.runtime import StorageRuntime
    from lib.storage.service import install_runtime_for_test
    runtime = StorageRuntime(supervisor=StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60), auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    owner = 1
    monkeypatch.setattr(ic, '_start_or_wake_worker', lambda: None)
    monkeypatch.setattr(ic, '_push', lambda *a, **k: None)
    monkeypatch.setattr(delivery, '_canonical_in_use', lambda *a: False)
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', '')
    root = tmp_path / 'canonical'
    root.mkdir()
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', 'Delivery test')
    git(root, 'config', 'user.email', 'delivery@example.invalid')
    (root / 'base.txt').write_text('base\n')
    git(root, 'add', 'base.txt')
    git(root, 'commit', '-m', 'base')
    work = tmp_path / 'work'
    git(root, 'worktree', 'add', '-b', 'feature', str(work))
    delivery._RECOVERY_CURSOR = 0
    try:
        yield root, work, owner
    finally:
        install_runtime_for_test(None)


def settle(repo, outcome='completed'):
    root, work, owner = repo
    bound = delivery.bind_task_workspace(str(work), 'task', user_id=owner,
                                         conversation_id='conv')
    assert bound['canonicalRoot'] == str(root)
    (work / 'feature.txt').write_text('finished\n')
    receipt = delivery.settle_task_workspace(str(work), 'task', user_id=owner,
                                             outcome=outcome)
    delivery.resume_pending_workspace_deliveries()
    return state.get_workspace(str(root), 'task', user_id=owner)['origin']['delivery']


def test_delivery_not_candidate_merge_and_idempotent_adoption(repo):
    root, work, owner = repo
    before = git(root, 'rev-parse', 'HEAD')
    receipt = settle(repo)
    assert receipt['state'] == 'pending'
    assert ic.process_ready_once()
    assert git(root, 'rev-parse', 'HEAD') == before
    assert not (root / 'feature.txt').exists()
    assert delivery.resume_pending_workspace_deliveries()
    row = state.get_workspace(str(root), 'task', user_id=owner)
    assert row['origin']['delivery']['state'] == 'delivered'
    assert (root / 'feature.txt').read_text() == 'finished\n'
    head = git(root, 'rev-parse', 'HEAD')
    assert not delivery.resume_pending_workspace_deliveries()
    assert git(root, 'rev-parse', 'HEAD') == head
    # Retain the source worktree for explicit cleanup, never silently remove it.
    assert work.is_dir()


@pytest.mark.parametrize('outcome', ['failed', 'cancelled', 'stopped'])
def test_failed_tasks_checkpoint_without_publish_or_later_outcome_upgrade(repo, outcome):
    root, work, owner = repo
    before = git(root, 'rev-parse', 'HEAD')
    assert settle(repo, outcome)['state'] == 'preserved'
    row = state.get_workspace(str(root), 'task', user_id=owner)
    assert row['state'] == 'checkpointed'
    assert git(root, 'show', row['checkpoint_sha'] + ':feature.txt') == 'finished'
    assert not ic.process_ready_once()
    assert delivery.settle_task_workspace(str(work), 'task', user_id=owner,
                                          outcome='completed')['state'] == 'preserved'
    assert git(root, 'rev-parse', 'HEAD') == before


def test_dirty_checkout_blocks_then_resumes_without_losing_user_bytes(repo):
    root, work, owner = repo
    settle(repo)
    assert ic.process_ready_once()
    (root / 'base.txt').write_text('user edit\n')
    receipt = delivery.deliver_registered_workspace(str(root), 'task', user_id=owner)
    assert receipt['state'] == 'blocked' and receipt['reason'] == 'canonical_dirty'
    assert (root / 'base.txt').read_text() == 'user edit\n'
    git(root, 'add', 'base.txt')
    git(root, 'commit', '-m', 'user work')
    delivery.resume_pending_workspace_deliveries()
    assert (root / 'base.txt').read_text() == 'user edit\n'
    assert (root / 'feature.txt').exists()
    assert state.get_workspace(str(root), 'task', user_id=owner)['origin']['delivery']['state'] == 'delivered'


def test_target_branch_change_never_publishes_to_different_branch(repo):
    root, work, owner = repo
    settle(repo)
    assert ic.process_ready_once()
    git(root, 'symbolic-ref', 'HEAD', 'refs/heads/other')
    # Point the new branch at the identical tree: cleanliness isn't sufficient.
    git(root, 'update-ref', 'refs/heads/other', 'refs/heads/main')
    receipt = delivery.deliver_registered_workspace(str(root), 'task', user_id=owner)
    assert receipt['reason'] == 'target_branch_changed'
    assert not (root / 'feature.txt').exists()


def test_crash_after_adoption_before_receipt_is_reconciled(repo, monkeypatch):
    root, work, owner = repo
    settle(repo)
    assert ic.process_ready_once()
    original = delivery._save
    def crash(row, receipt, **updates):
        if updates.get('state') == 'delivered':
            raise RuntimeError('simulated post-git crash')
        return original(row, receipt, **updates)
    monkeypatch.setattr(delivery, '_save', crash)
    with pytest.raises(RuntimeError, match='post-git'):
        delivery.deliver_registered_workspace(str(root), 'task', user_id=owner)
    head = git(root, 'rev-parse', 'HEAD')
    monkeypatch.setattr(delivery, '_save', original)
    delivery.resume_pending_workspace_deliveries()
    assert git(root, 'rev-parse', 'HEAD') == head
    assert state.get_workspace(str(root), 'task', user_id=owner)['origin']['delivery']['state'] == 'delivered'


def test_intent_storage_failure_prevents_checkpoint_and_submission(repo, monkeypatch):
    root, work, owner = repo
    delivery.bind_task_workspace(str(work), 'task', user_id=owner)
    (work / 'feature.txt').write_text('retain\n')
    def fail(**kwargs):
        raise RuntimeError('storage unavailable')
    monkeypatch.setattr(state, 'set_workspace_meta', fail)
    with pytest.raises(RuntimeError, match='storage unavailable'):
        delivery.settle_task_workspace(str(work), 'task', user_id=owner, outcome='completed')
    assert state.get_workspace(str(root), 'task', user_id=owner)['state'] == 'running'
    assert (work / 'feature.txt').read_text() == 'retain\n'


def test_recovery_page_is_readonly_bounded_and_keeps_owner(repo):
    root, work, owner = repo
    delivery.bind_task_workspace(str(work), 'task', user_id=owner)
    page = state.delivery_page(0, 1)
    assert len(page['rows']) == 1
    assert page['rows'][0]['user_id'] == owner
    assert not page['hasMore']
    assert state.delivery_page(page['rows'][0]['id'], 1)['rows'] == []


def test_runtime_paths_refused_before_checkpoint_ref_and_index_untouched(repo):
    root, work, owner = repo
    delivery.bind_task_workspace(str(work), 'task', user_id=owner)
    (work / 'node_modules').mkdir()
    (work / 'node_modules' / 'runtime.bin').write_bytes(b'not source')
    before = git(work, 'ls-files', '--stage')
    receipt = delivery.settle_task_workspace(str(work), 'task', user_id=owner, outcome='completed')
    assert receipt['state'] == 'pending'
    delivery.resume_pending_workspace_deliveries()
    receipt = state.get_workspace(str(root), 'task', user_id=owner)['origin']['delivery']
    assert receipt['state'] == 'failed'
    assert 'forbidden' in receipt['reason']
    assert git(work, 'ls-files', '--stage') == before
    assert state.get_workspace(str(root), 'task', user_id=owner)['checkpoint_sha'] == ''


def test_concurrent_task_cannot_claim_same_unfinished_workspace(repo):
    root, work, owner = repo
    delivery.bind_task_workspace(str(work), 'task', user_id=owner)
    with pytest.raises(ic.IntegrationError, match='another work item'):
        delivery.bind_task_workspace(str(work), 'second', user_id=owner)


def test_recovery_submits_after_intent_before_queue_crash(repo, monkeypatch):
    canonical, workspace, _owner = repo
    delivery.bind_task_workspace(workspace, 'crash-intent', user_id=1)
    (workspace / 'change.txt').write_text('durable task work\n')
    delivery.settle_task_workspace(workspace, 'crash-intent', user_id=1,
                                 outcome='completed')
    row = state.get_workspace(str(canonical), 'crash-intent', user_id=1)
    original = ic.submit_workspace
    monkeypatch.setattr(ic, 'submit_workspace', lambda *a, **k: (_ for _ in ()).throw(SystemExit('crash')))
    with pytest.raises(SystemExit):
        delivery._settle_registered(row, delivery._receipt(row))
    assert state.get_workspace(str(canonical), 'crash-intent', user_id=1)['state'] == 'checkpointed'
    monkeypatch.setattr(ic, 'submit_workspace', original)
    monkeypatch.setattr(delivery, '_RECOVERY_CURSOR', 0)
    delivery.resume_pending_workspace_deliveries()
    assert state.get_workspace(str(canonical), 'crash-intent', user_id=1)['state'] == 'ready'
    assert ic.process_ready_once()
    assert delivery.deliver_registered_workspace(canonical, 'crash-intent', user_id=1)['state'] == 'delivered'


def test_cancelled_revokes_queue_and_stale_success(repo):
    canonical, workspace, _owner = repo
    delivery.bind_task_workspace(workspace, 'revoked', user_id=1)
    (workspace / 'change.txt').write_text('not authorized\n')
    delivery.settle_task_workspace(workspace, 'revoked', user_id=1, outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    assert state.get_workspace(str(canonical), 'revoked', user_id=1)['state'] == 'ready'
    receipt = delivery.settle_task_workspace(workspace, 'revoked', user_id=1, outcome='cancelled')
    assert receipt['state'] == 'pending'
    # Revocation removes the queue before asynchronous preservation runs.
    assert not ic.process_ready_once()
    delivery.resume_pending_workspace_deliveries()
    assert delivery._receipt(state.get_workspace(str(canonical), 'revoked', user_id=1))['state'] == 'preserved'
    assert delivery.settle_task_workspace(workspace, 'revoked', user_id=1, outcome='completed')['taskOutcome'] == 'cancelled'
    state.set_workspace_meta(project_root=str(canonical), task_id='revoked', user_id=1,
        patch={'delivery': {'state': 'pending', 'taskOutcome': 'completed'}}, now=99)
    assert delivery._receipt(state.get_workspace(str(canonical), 'revoked', user_id=1))['taskOutcome'] == 'cancelled'
    assert not (canonical / 'change.txt').exists()


def test_transient_checkpoint_failure_remains_recoverable(repo, monkeypatch):
    canonical, workspace, _owner = repo
    delivery.bind_task_workspace(workspace, 'retry', user_id=1)
    (workspace / 'retry.txt').write_text('retry me\n')
    original = ic.checkpoint_workspace
    monkeypatch.setattr(ic, 'checkpoint_workspace', lambda *a, **k: (_ for _ in ()).throw(OSError('temporary store outage')))
    receipt = delivery.settle_task_workspace(workspace, 'retry', user_id=1, outcome='failed')

    assert receipt['state'] == 'pending'
    delivery.resume_pending_workspace_deliveries()
    receipt = delivery._receipt(state.get_workspace(str(canonical), 'retry', user_id=1))
    assert receipt['state'] == 'blocked'
    monkeypatch.setattr(ic, 'checkpoint_workspace', original)
    monkeypatch.setattr(delivery, '_RECOVERY_CURSOR', 0)
    delivery.resume_pending_workspace_deliveries()
    assert delivery._receipt(state.get_workspace(str(canonical), 'retry', user_id=1))['state'] == 'preserved'


def test_ignored_file_never_overwritten_by_adoption(repo):
    canonical, workspace, _owner = repo
    (canonical / '.git' / 'info' / 'exclude').write_text('private.txt\n')
    (canonical / 'private.txt').write_text('local private bytes\n')
    delivery.bind_task_workspace(workspace, 'ignored', user_id=1)
    (workspace / 'private.txt').write_text('task bytes\n')
    git(workspace, 'add', '-f', 'private.txt')
    git(workspace, 'commit', '-qm', 'track formerly ignored path')
    delivery.settle_task_workspace(workspace, 'ignored', user_id=1, outcome='completed')

    delivery.resume_pending_workspace_deliveries()
    assert ic.process_ready_once()
    before = git(canonical, 'rev-parse', 'HEAD')
    assert delivery.deliver_registered_workspace(canonical, 'ignored', user_id=1)['state'] == 'blocked'
    assert (canonical / 'private.txt').read_text() == 'local private bytes\n'
    assert git(canonical, 'rev-parse', 'HEAD') == before


def test_post_adoption_crash_then_user_edit_is_not_clean_delivery(repo, monkeypatch):
    canonical, workspace, _owner = repo
    delivery.bind_task_workspace(workspace, 'dirty-crash', user_id=1)
    (workspace / 'change.txt').write_text('task\n')
    delivery.settle_task_workspace(workspace, 'dirty-crash', user_id=1, outcome='completed')

    delivery.resume_pending_workspace_deliveries()
    assert ic.process_ready_once()
    original = delivery._save
    def crash(row, receipt, **changes):
        if changes.get('state') == 'delivered':
            raise SystemExit('crash')
        return original(row, receipt, **changes)
    monkeypatch.setattr(delivery, '_save', crash)
    with pytest.raises(SystemExit):
        delivery.deliver_registered_workspace(canonical, 'dirty-crash', user_id=1)
    monkeypatch.setattr(delivery, '_save', original)
    (canonical / 'change.txt').write_text('new user edit\n')
    receipt = delivery.deliver_registered_workspace(canonical, 'dirty-crash', user_id=1)
    assert receipt['reason'] == 'canonical_dirty'
    assert (canonical / 'change.txt').read_text() == 'new user edit\n'


def test_simultaneous_cross_owner_claim_has_one_winner(repo):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    canonical, workspace, _owner = repo
    gate = Barrier(2)
    def bind(owner):
        gate.wait(timeout=10)
        try:
            delivery.bind_task_workspace(workspace, f'owner-{owner}', user_id=owner)
            return 'bound'
        except Exception:
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(bind, [1, 2])) == ['bound', 'refused']


def test_git_subprocess_inherits_workspace_lease(repo, monkeypatch):
    from lib.project_mod.workspace_lease import workspace_write_lease, workspace_lease_fds
    canonical, _workspace, _owner = repo
    original = ic.subprocess.run
    observed = []
    with workspace_write_lease(canonical):
        expected = workspace_lease_fds()
        def run(*args, **kwargs):
            observed.append(kwargs.get('pass_fds'))
            return original(*args, **kwargs)
        monkeypatch.setattr(ic.subprocess, 'run', run)
        assert ic._git(canonical, ['status', '--short']).returncode == 0
    assert expected and observed == [expected]


@pytest.mark.parametrize('live_status', ['running', 'done'])
def test_working_registration_with_live_carrier_never_reads_cold_verdict(repo, monkeypatch, live_status):
    from lib import storage
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    canonical, workspace, owner = repo
    delivery.bind_task_workspace(workspace, 'live', user_id=owner)
    monkeypatch.setattr(chat_task_runtime, 'get', lambda _task: {'status': live_status})
    def unexpected_read():
        pytest.fail('A live carrier still owns terminal intent persistence')
    monkeypatch.setattr(storage, 'get_storage_client', unexpected_read)
    assert not delivery.resume_pending_workspace_deliveries()
    row = state.get_workspace(str(canonical), 'live', user_id=owner)
    assert row['state'] == 'running'
    assert delivery._receipt(row)['state'] == 'working'


@pytest.mark.parametrize('verdict, expected', [
    ({'status': 'done', 'metadata': {'finishReason': 'stop',
                                   'streamState': 'provider_finished'}}, 'completed'),
    ({'status': 'done'}, 'failed'),
    ({'status': 'done', 'metadata': {'finishReason': 'length'}}, 'failed'),
    ({'status': 'cancelled', 'metadata': {'aborted': True}}, 'cancelled'),
    (None, 'failed'),
])
def test_working_registration_recovers_owner_scoped_durable_verdict(repo, monkeypatch, verdict, expected):
    from types import SimpleNamespace
    from lib import storage
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    canonical, workspace, owner = repo
    delivery.bind_task_workspace(workspace, 'orphan', user_id=owner)
    (workspace / 'orphan.txt').write_text('retained after restart\n')
    monkeypatch.setattr(chat_task_runtime, 'get', lambda _task: None)
    queries = []
    def query(name, payload):
        queries.append((name, payload))
        return verdict
    monkeypatch.setattr(storage, 'get_storage_client', lambda: SimpleNamespace(query=query))
    assert delivery.resume_pending_workspace_deliveries()
    assert queries == [('task_results.replay_get', {
        'key': 'orphan', 'user_id': owner,
        'include_terminal_payload': False, 'include_metadata': True,
    })]
    row = state.get_workspace(str(canonical), 'orphan', user_id=owner)
    assert delivery._receipt(row)['taskOutcome'] == expected
    assert delivery._receipt(row)['state'] == 'pending'
    # Recovery records intent in one tick, captures work in the next.
    assert delivery.resume_pending_workspace_deliveries()
    row = state.get_workspace(str(canonical), 'orphan', user_id=owner)
    assert row['state'] == ('ready' if expected == 'completed' else 'checkpointed')
    assert git(canonical, 'show', row['checkpoint_sha'] + ':orphan.txt') == 'retained after restart'
    assert not (canonical / 'orphan.txt').exists()


def test_cold_verdict_failure_keeps_retry_debt_and_recovers_other_owner(repo, monkeypatch):
    from types import SimpleNamespace
    from lib import storage
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    canonical, workspace, owner = repo
    delivery.bind_task_workspace(workspace, 'unavailable', user_id=owner)
    other = workspace.parent / 'other-owner'
    git(canonical, 'worktree', 'add', '-b', 'other-owner', str(other))
    delivery.bind_task_workspace(other, 'recoverable', user_id=owner + 1)
    monkeypatch.setattr(chat_task_runtime, 'get', lambda _task: None)
    def query(_name, payload):
        if payload['user_id'] == owner:
            raise OSError('temporary replay outage')
        return {'status': 'done', 'finishReason': 'stop', 'streamState': 'provider_finished'}
    monkeypatch.setattr(storage, 'get_storage_client', lambda: SimpleNamespace(query=query))
    assert delivery.resume_pending_workspace_deliveries()
    assert delivery._receipt(state.get_workspace(
        str(canonical), 'unavailable', user_id=owner))['state'] == 'working'
    assert delivery._receipt(state.get_workspace(
        str(canonical), 'recoverable', user_id=owner + 1))['taskOutcome'] == 'completed'


def test_cancel_linearized_before_adoption_prevents_git_publication(repo, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from lib.project_mod.workspace_lease import workspace_write_lease
    from contextlib import contextmanager
    canonical, workspace, owner = repo
    settle(repo)
    assert ic.process_ready_once()
    before = git(canonical, 'rev-parse', 'HEAD')
    revocation_in_lock, release_revocation, adoption_has_lease = Event(), Event(), Event()
    original_save = delivery._save
    def save(row, receipt, **updates):
        if updates.get('taskOutcome') == 'cancelled':
            revocation_in_lock.set()
            assert release_revocation.wait(10)
        return original_save(row, receipt, **updates)
    monkeypatch.setattr(delivery, '_save', save)
    @contextmanager
    def observed_lease(*args, **kwargs):
        with workspace_write_lease(*args, **kwargs) as lease:
            adoption_has_lease.set()
            yield lease
    monkeypatch.setattr('lib.project_mod.workspace_lease.workspace_write_lease', observed_lease)
    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled = pool.submit(delivery.settle_task_workspace, workspace, 'task',
                                user_id=owner, outcome='cancelled')
        try:
            assert revocation_in_lock.wait(10)
            adopted = pool.submit(delivery.deliver_registered_workspace, canonical, 'task', user_id=owner)
            assert adoption_has_lease.wait(10)
        finally:
            release_revocation.set()
        assert cancelled.result(timeout=10)['taskOutcome'] == 'cancelled'
        assert adopted.result(timeout=10)['taskOutcome'] == 'cancelled'
    assert git(canonical, 'rev-parse', 'HEAD') == before
    assert not (canonical / 'feature.txt').exists()


def test_automatic_task_workspace_freezes_input_without_mutating_canonical(repo, tmp_path, monkeypatch):
    root, _work, owner = repo
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    before = git(root, 'rev-parse', 'HEAD')
    (root / 'base.txt').write_text('borrowed dirty context\n')
    git(root, 'add', 'base.txt')
    (root / 'untracked.txt').write_text('borrowed untracked\n')
    index = git(root, 'ls-files', '--stage')
    status = git(root, 'status', '--porcelain')
    created = ic.create_task_workspace(str(root), 'auto', user_id=owner)
    from pathlib import Path
    isolated = Path(created['workspacePath'])
    assert isolated != root
    assert (isolated / 'base.txt').read_text() == 'borrowed dirty context\n'
    assert (isolated / 'untracked.txt').read_text() == 'borrowed untracked\n'
    assert git(root, 'rev-parse', 'HEAD') == before
    assert git(root, 'ls-files', '--stage') == index
    assert git(root, 'status', '--porcelain') == status
    assert ic.create_task_workspace(str(root), 'auto', user_id=owner) == created
    delivery.bind_task_workspace(str(isolated), 'auto', user_id=owner)
    (isolated / 'task.txt').write_text('only owned delta\n')
    ic.checkpoint_workspace(str(root), 'auto', user_id=owner)
    row = state.get_workspace(str(root), 'auto', user_id=owner)
    publication = ic.task_publication_checkpoint(row)
    assert git(root, 'show', publication + ':base.txt') == 'base'
    assert git(root, 'show', publication + ':task.txt') == 'only owned delta'
    assert 'untracked.txt' not in git(root, 'ls-tree', '--name-only', publication)
    assert git(root, 'status', '--porcelain') == status


def test_noop_task_does_not_publish_borrowed_dirty_input(repo, tmp_path, monkeypatch):
    root, _work, owner = repo
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    (root / 'base.txt').write_text('belongs to user\n')
    before = git(root, 'rev-parse', 'HEAD')
    created = ic.create_task_workspace(str(root), 'noop', user_id=owner)
    delivery.bind_task_workspace(created['workspacePath'], 'noop', user_id=owner)
    delivery.settle_task_workspace(created['workspacePath'], 'noop', user_id=owner,
                                    outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    row = state.get_workspace(str(root), 'noop', user_id=owner)
    assert row['origin']['delivery']['state'] == 'delivered'
    assert row['origin']['delivery']['phase'] == 'no_changes'
    assert git(root, 'rev-parse', 'HEAD') == before
    assert (root / 'base.txt').read_text() == 'belongs to user\n'
    assert not ic.process_ready_once()


def test_task_depending_on_borrowed_context_is_preserved_not_published(repo, tmp_path, monkeypatch):
    from pathlib import Path
    root, _work, owner = repo
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    (root / 'base.txt').write_text('borrowed edit\n')
    before = git(root, 'rev-parse', 'HEAD')
    created = ic.create_task_workspace(str(root), 'dependent', user_id=owner)
    work = Path(created['workspacePath'])
    (work / 'base.txt').write_text('task extends borrowed edit\n')
    ic.checkpoint_workspace(str(root), 'dependent', user_id=owner)
    row = state.get_workspace(str(root), 'dependent', user_id=owner)
    with pytest.raises(ic.IntegrationError, match='delta conflicts'):
        ic.task_publication_checkpoint(row)
    assert git(root, 'rev-parse', 'HEAD') == before
    assert git(root, 'show', row['checkpoint_sha'] + ':base.txt') == 'task extends borrowed edit'
    assert (root / 'base.txt').read_text() == 'borrowed edit\n'


def test_task_registration_failure_cleans_only_created_checkout_and_retries(repo, tmp_path, monkeypatch):
    from pathlib import Path
    root, _work, owner = repo
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    before = git(root, 'rev-parse', 'HEAD')
    original = state.register_workspace
    paths = []
    def failed(**kwargs):
        paths.append(kwargs['workspace_path'])
        raise RuntimeError('injected registration failure')
    monkeypatch.setattr(state, 'register_workspace', failed)
    with pytest.raises(RuntimeError, match='injected registration'):
        ic.create_task_workspace(str(root), 'retry-admission', user_id=owner)
    assert not Path(paths[0]).exists()
    assert git(root, 'rev-parse', 'HEAD') == before
    monkeypatch.setattr(state, 'register_workspace', original)
    created = ic.create_task_workspace(str(root), 'retry-admission', user_id=owner)
    assert Path(created['workspacePath']).is_dir()


def test_lost_registration_ack_preserves_owned_workspace(repo, tmp_path, monkeypatch):
    from pathlib import Path
    root, _work, owner = repo
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    original = state.register_workspace
    def lost_ack(**kwargs):
        original(**kwargs)
        raise RuntimeError('registration acknowledgment lost')
    monkeypatch.setattr(state, 'register_workspace', lost_ack)
    with pytest.raises(RuntimeError, match='acknowledgment lost'):
        ic.create_task_workspace(str(root), 'lost-ack', user_id=owner)
    row = state.get_workspace(str(root), 'lost-ack', user_id=owner)
    assert Path(row['workspace_path']).exists()
    created = ic.create_task_workspace(str(root), 'lost-ack', user_id=owner)
    assert created['workspacePath'] == row['workspace_path']


def test_task_cancelled_before_snapshot_creates_no_workspace(repo, tmp_path, monkeypatch):
    root, _work, owner = repo
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(tmp_path / 'managed'))
    with pytest.raises(Exception, match='cancelled'):
        ic.create_task_workspace(str(root), 'cancelled-admission', user_id=owner,
                                  task={'aborted': True})
    assert state.find_workspace(str(root), 'cancelled-admission', user_id=owner) is None


def automatic_workspace(repo, monkeypatch, task_id='automatic'):
    root, _legacy_work, owner = repo
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(root.parent / 'managed'))
    created = ic.create_task_workspace(str(root), task_id, user_id=owner, conversation_id='conv')
    bound = delivery.bind_task_workspace(str(root), task_id, user_id=owner, conversation_id='conv')
    assert bound['workspacePath'] == created['workspacePath']
    from pathlib import Path
    return Path(created['workspacePath'])



def automatic_settle(repo, task_id='automatic', outcome='completed'):
    root, _work, owner = repo
    delivery.settle_task_workspace(str(root), task_id, user_id=owner, outcome=outcome)
    delivery.resume_pending_workspace_deliveries()
    return state.get_workspace(str(root), task_id, user_id=owner)['origin']['delivery']



def test_automatic_source_snapshot_preserves_dirty_context_and_real_index(repo, monkeypatch):
    root, _work, _owner = repo
    (root / 'base.txt').write_text('staged user edit\n')
    git(root, 'add', 'base.txt')
    (root / 'base.txt').write_text('unstaged user edit\n')
    (root / 'untracked.txt').write_text('user context\n')
    before_head = git(root, 'rev-parse', 'HEAD')
    before_index = (root / '.git' / 'index').read_bytes()
    work = automatic_workspace(repo, monkeypatch)
    assert (work / 'base.txt').read_text() == 'unstaged user edit\n'
    assert (work / 'untracked.txt').read_text() == 'user context\n'
    assert git(root, 'rev-parse', 'HEAD') == before_head
    assert (root / '.git' / 'index').read_bytes() == before_index
    assert (root / 'base.txt').read_text() == 'unstaged user edit\n'



def test_task_delta_delivery_preserves_source_wip_and_concurrent_unrelated_files(repo, monkeypatch):
    root, _work, owner = repo
    (root / 'base.txt').write_text('existing user work\n')
    (root / 'staged.txt').write_text('staged user work\n')
    git(root, 'add', 'staged.txt')
    before_index = (root / '.git' / 'index').read_bytes()
    before_head = git(root, 'rev-parse', 'HEAD')
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('existing user work\ntask addition\n')
    (root / 'another.txt').write_text('concurrent user change\n')
    receipt = automatic_settle(repo)
    assert receipt['state'] == 'delivered', receipt
    assert (root / 'base.txt').read_text() == 'existing user work\ntask addition\n'
    assert (root / 'another.txt').read_text() == 'concurrent user change\n'
    assert (root / 'staged.txt').read_text() == 'staged user work\n'
    assert (root / '.git' / 'index').read_bytes() == before_index
    assert git(root, 'rev-parse', 'HEAD') == before_head
    assert state.get_workspace(str(root), 'automatic', user_id=owner)['state'] == 'checkpointed'
    assert automatic_settle(repo)['state'] == 'delivered'
    assert (root / 'base.txt').read_text().count('task addition') == 1



def test_two_automatic_tasks_do_not_share_writer_locks_and_deliver_disjoint_changes(repo, monkeypatch):
    from lib.project_mod.workspace_lease import workspace_write_lease
    root, _work, _owner = repo
    first = automatic_workspace(repo, monkeypatch, 'first')
    second = automatic_workspace(repo, monkeypatch, 'second')
    assert first != second
    with workspace_write_lease(first), workspace_write_lease(second):
        (first / 'first.txt').write_text('first task\n')
        (second / 'second.txt').write_text('second task\n')
    for task_id in ('first', 'second'):
        delivery.settle_task_workspace(str(root), task_id, user_id=repo[2], outcome='completed')
    delivery.resume_pending_workspace_deliveries()
    for task_id in ('first', 'second'):
        assert state.get_workspace(str(root), task_id, user_id=repo[2])['origin']['delivery']['state'] == 'delivered'
    assert (root / 'first.txt').read_text() == 'first task\n'
    assert (root / 'second.txt').read_text() == 'second task\n'



def test_task_delta_conflict_preserves_entire_canonical_tree_and_task_checkpoint(repo, monkeypatch):
    root, _work, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('task replacement\n')
    (work / 'new.txt').write_text('must not be partially applied\n')
    (root / 'base.txt').write_text('concurrent replacement\n')
    receipt = automatic_settle(repo)
    assert receipt['state'] == 'blocked', receipt
    assert 'task_delta_conflict' in receipt['reason']
    assert (root / 'base.txt').read_text() == 'concurrent replacement\n'
    assert not (root / 'new.txt').exists()
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    assert git(root, 'show', row['checkpoint_sha'] + ':base.txt') == 'task replacement'



@pytest.mark.parametrize('outcome', ['failed', 'cancelled', 'stopped'])
def test_automatic_unsuccessful_task_is_preserved_without_publication(repo, monkeypatch, outcome):
    root, _work, _owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('unfinished\n')
    receipt = automatic_settle(repo, outcome=outcome)
    assert receipt['state'] == 'preserved'
    assert (root / 'base.txt').read_text() == 'base\n'
    assert (work / 'base.txt').read_text() == 'unfinished\n'



def test_task_delta_gate_runs_without_canonical_writer_lease_and_checks_exact_result(repo, monkeypatch):
    import threading
    from lib.project_mod.workspace_lease import workspace_write_lease
    root, _work, _owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'new.txt').write_text('task delta\n')
    checked = []

    def gate(checkout, source, target, configured):
        assert git(root, 'show', target + ':new.txt') == 'task delta'
        def probe():
            with workspace_write_lease(root):
                checked.append('canonical available')
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(5)
        assert not thread.is_alive()
        return True, ''

    monkeypatch.setattr(ic, '_gate_commands', gate)
    assert automatic_settle(repo)['state'] == 'delivered'
    assert checked == ['canonical available']



@pytest.mark.parametrize('change', ['file', 'branch', 'revocation'])
def test_task_delta_revalidates_after_gate_before_publication(repo, monkeypatch, change):
    root, _work, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'new.txt').write_text('task delta\n')

    def gate(*args):
        if change == 'file':
            (root / 'base.txt').write_text('changed during gate\n')
        elif change == 'branch':
            git(root, 'symbolic-ref', 'HEAD', 'refs/heads/changed')
        else:
            delivery.settle_task_workspace(str(root), 'automatic', user_id=owner, outcome='cancelled')
        return True, ''

    monkeypatch.setattr(ic, '_gate_commands', gate)
    receipt = automatic_settle(repo)
    assert receipt['state'] in {'blocked', 'pending', 'preserved'}, receipt
    assert not (root / 'new.txt').exists()
    if change == 'revocation':
        assert receipt['taskOutcome'] == 'cancelled'



def test_task_delta_recovers_lost_final_receipt_without_reapplying(repo, monkeypatch):
    root, _work, _owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').write_text('base\nappend once\n')
    original = delivery._save
    failed = []

    def fail_once(row, receipt, **updates):
        if updates.get('state') == 'delivered' and not failed:
            failed.append(True)
            raise RuntimeError('receipt unavailable after publication')
        return original(row, receipt, **updates)

    monkeypatch.setattr(delivery, '_save', fail_once)
    first = automatic_settle(repo)
    assert first['state'] == 'blocked', first
    assert first['targetSha'] and first['checkpointSha']
    (root / 'unrelated.txt').write_text('written after patch\n')
    delivery.resume_pending_workspace_deliveries()
    receipt = state.get_workspace(str(root), 'automatic', user_id=repo[2])['origin']['delivery']
    assert receipt['state'] == 'delivered', receipt
    assert (root / 'base.txt').read_text() == 'base\nappend once\n'
    assert (root / 'unrelated.txt').read_text() == 'written after patch\n'



def test_no_change_automatic_task_reclaims_clone_and_keeps_terminal_receipt(repo, monkeypatch):
    work = automatic_workspace(repo, monkeypatch)
    receipt = automatic_settle(repo)
    assert receipt['state'] == 'delivered'
    assert not work.exists()
    assert automatic_settle(repo)['state'] == 'delivered'



def test_no_change_task_preserves_ignored_user_artifacts(repo, monkeypatch):
    root, _work, _owner = repo
    (root / '.gitignore').write_text('private-artifact\n')
    work = automatic_workspace(repo, monkeypatch)
    (work / 'private-artifact').write_text('durable user output\n')
    assert automatic_settle(repo)['state'] == 'delivered'
    assert (work / 'private-artifact').read_text() == 'durable user output\n'



def test_source_snapshot_does_not_contend_with_canonical_writer_lease(repo, monkeypatch):
    from lib.project_mod.workspace_lease import workspace_write_lease
    import threading
    root, _work, _owner = repo
    ready, release = threading.Event(), threading.Event()
    def existing_writer():
        with workspace_write_lease(root):
            ready.set()
            assert release.wait(10)
    thread = threading.Thread(target=existing_writer)
    thread.start()
    try:
        assert ready.wait(5)
        work = automatic_workspace(repo, monkeypatch)
        assert (work / 'base.txt').read_text() == 'base\n'
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()



def test_source_snapshot_retries_changed_bytes_and_refuses_moved_head(repo, monkeypatch):
    root, _work, owner = repo
    original = ic._alternate_index_checkpoint
    calls = []
    def capture(checkout, parent, task_id):
        snapshot = original(checkout, parent, task_id)
        calls.append(snapshot)
        if len(calls) == 1:
            (root / 'base.txt').write_text('changed during first capture\n')
        return snapshot
    monkeypatch.setattr(ic, '_alternate_index_checkpoint', capture)
    work = automatic_workspace(repo, monkeypatch)
    assert len(calls) == 4
    assert (work / 'base.txt').read_text() == 'changed during first capture\n'

    def moved_branch(checkout, parent, task_id):
        snapshot = original(checkout, parent, task_id)
        git(root, 'symbolic-ref', 'HEAD', 'refs/heads/new-branch')
        return snapshot
    monkeypatch.setattr(ic, '_alternate_index_checkpoint', moved_branch)
    with pytest.raises(ic.IntegrationError, match='HEAD/branch changed'):
        ic.create_task_workspace(str(root), 'another-task', user_id=owner)



def test_running_background_command_fences_capture_and_clone_reclamation(repo, monkeypatch):
    from lib.tasks_pkg import background_commands
    root, _work, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    active = [True]
    monkeypatch.setattr(background_commands, 'task_has_running_commands',
                        lambda **identity: active[0])
    receipt = automatic_settle(repo)
    assert receipt['reason'] == 'task_background_commands_running'
    assert work.exists()
    (work / 'late.txt').write_text('background result\n')
    active[0] = False
    delivery.resume_pending_workspace_deliveries()
    receipt = state.get_workspace(str(root), 'automatic', user_id=owner)['origin']['delivery']
    assert receipt['state'] == 'delivered'
    assert (root / 'late.txt').read_text() == 'background result\n'



def test_lazy_workspace_runtime_keeps_python_prefix_private_and_reuses_node_packages(repo, monkeypatch):
    import sys
    from pathlib import Path
    from lib.tasks_pkg import workspace_delivery
    root, _work, owner = repo
    (root / '.gitignore').write_text('.venv/\nnode_modules/\n')
    source_env = root / '.venv'
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(source_env)],
                   check=True, capture_output=True, timeout=60)
    source_python = source_env / 'bin/python'
    packages = Path(subprocess.check_output([str(source_python), '-c',
        'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip())
    (packages / 'borrowed_package.py').write_text('VALUE = "source packages"\n')
    package = root / 'node_modules' / 'sample'
    package.mkdir(parents=True)
    (package / 'package.json').write_text('{"name":"sample"}\n')
    work = automatic_workspace(repo, monkeypatch)
    assert not (work / '.venv').exists()
    task = {'id': 'automatic', '_userId': owner, 'convId': 'conv', '_workspaceDelivery': {
        'repositories': [{'canonicalRoot': str(root), 'workspacePath': str(work), 'workId': 'automatic'}]}}
    workspace_delivery.prepare_worker_command_runtime(task)
    output = subprocess.check_output([str(work / '.venv/bin/python'), '-c',
        'import sys,borrowed_package; print(sys.prefix); print(borrowed_package.VALUE)'], text=True)
    assert output.splitlines() == [str(work / '.venv'), 'source packages']
    assert (work / 'node_modules/sample').is_symlink()
    assert (work / 'node_modules').is_dir() and not (work / 'node_modules').is_symlink()
    assert git(work, 'status', '--porcelain') == ''
    assert automatic_settle(repo)['state'] == 'delivered'
    assert not work.exists()
    assert (package / 'package.json').exists()
    assert (packages / 'borrowed_package.py').exists()



def test_automatic_workspace_operation_measurement(repo, monkeypatch):
    import time
    root, _work, _owner = repo
    for index in range(1000):
        (root / f'source-{index}.txt').write_text(f'{index:04d}' + 'x' * 1020)
    started = time.perf_counter()
    work = automatic_workspace(repo, monkeypatch)
    created = time.perf_counter()
    assert automatic_settle(repo)['state'] == 'delivered'
    completed = time.perf_counter()
    assert not work.exists()
    print(f'workspace-measurement files=1000 bytes=1024000 creation_s={created-started:.3f} '
          f'no_change_settlement_s={completed-created:.3f} retained_clones=0')



def test_unborn_source_branch_can_execute_and_receive_task_delta_without_creating_head(repo, monkeypatch):
    root, _work, _owner = repo
    git(root, 'symbolic-ref', 'HEAD', 'refs/heads/unborn')
    index_before = (root / '.git/index').read_bytes()
    work = automatic_workspace(repo, monkeypatch)
    assert (work / 'base.txt').read_text() == 'base\n'
    (work / 'new.txt').write_text('new repository task\n')
    receipt = automatic_settle(repo)
    assert receipt['state'] == 'delivered', receipt
    assert (root / 'new.txt').read_text() == 'new repository task\n'
    assert ic._rev(root, 'HEAD') == ''
    assert (root / '.git/index').read_bytes() == index_before



@pytest.mark.parametrize('modify_after_verification', [False, True])
def test_successful_workspace_verifier_is_replayed_on_exact_publication_tree(
        repo, monkeypatch, modify_after_verification):
    import sys
    from lib.tasks_pkg import workspace_delivery
    root, _work, owner = repo
    (root / '.gitignore').write_text('__pycache__/\n')
    work = automatic_workspace(repo, monkeypatch)
    package = work / 'package'
    package.mkdir()
    (package / 'subject.py').write_text('VALUE = 1\n')
    tests = package / 'tests'
    tests.mkdir()
    (tests / 'test_subject.py').write_text(
        'import unittest, subject\nclass SubjectTest(unittest.TestCase):\n'
        '    def test_value(self): self.assertEqual(subject.VALUE, 1)\n')
    command = __import__('shlex').join([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'])
    cp = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
                        cwd=package, capture_output=True, text=True)
    assert cp.returncode == 0, cp.stderr
    task = {'id': 'automatic', 'convId': 'conv', '_userId': owner,
            'status': 'done', 'finishReason': 'stop', 'config': {'projectPath': str(work)},
            '_workspaceDelivery': {'repositories': [{
                'canonicalRoot': str(root), 'workspacePath': str(work), 'workId': 'automatic'}]}}
    workspace_delivery.record_worker_verification(task, {'command': command, 'working_dir': 'package'},
                                                  str(work), {'exitCode': 0})
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    assert row['origin']['_workspaceDelivery']['verification']['directory'] == 'package'
    if modify_after_verification:
        (package / 'subject.py').write_text('VALUE = 2\n')
    assert workspace_delivery.persist_terminal_workspace_intent(task) is True
    delivery.resume_pending_workspace_deliveries()
    result = state.get_workspace(str(root), 'automatic', user_id=owner)['origin']['delivery']
    if modify_after_verification:
        assert result['state'] == 'failed', result
        assert not (root / 'package/subject.py').exists()
    else:
        assert result['state'] == 'delivered', result
        assert (root / 'package/subject.py').read_text() == 'VALUE = 1\n'



def test_live_child_agents_fence_worktree_publication_until_their_owner_settles(repo, monkeypatch):
    from types import SimpleNamespace
    from lib.swarm.integration import _state
    root, _work, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    session = SimpleNamespace(task_id='automatic', user_id=owner, is_terminated=False)
    monkeypatch.setitem(_state._active_sessions, 'private-workspace-test-session', session)
    receipt = automatic_settle(repo)
    assert receipt['reason'] == 'task_child_agents_running'
    assert work.exists()
    (work / 'delegated.txt').write_text('child result\n')
    session.is_terminated = True
    delivery.resume_pending_workspace_deliveries()
    receipt = state.get_workspace(str(root), 'automatic', user_id=owner)['origin']['delivery']
    assert receipt['state'] == 'delivered', receipt
    assert (root / 'delegated.txt').read_text() == 'child result\n'



def test_task_delta_binary_assets_and_deletions_are_published_together(repo, monkeypatch):
    root, _work, _owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'base.txt').unlink()
    content = bytes(range(256)) * 8
    (work / 'asset.bin').write_bytes(content)
    assert automatic_settle(repo)['state'] == 'delivered'
    assert not (root / 'base.txt').exists()
    assert (root / 'asset.bin').read_bytes() == content



def test_runtime_preparation_strips_ambient_secrets_and_keeps_remote_venv_boundary(repo, monkeypatch):
    import sys
    from lib.project_mod.workspace_runtime import prepare_workspace_runtime
    root, _work, _owner = repo
    (root / '.gitignore').write_text('.venv/\n')
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(root / '.venv')],
                   check=True, capture_output=True, timeout=60)
    work = automatic_workspace(repo, monkeypatch)
    monkeypatch.setenv('LLM_API_KEY', 'private-fixture-secret')
    original = subprocess.run
    seen = []
    def probe(*args, **kwargs):
        if str(args[0][0]).startswith(str(root / '.venv')) or str(args[0][0]).startswith(str(work / '.venv')):
            assert 'LLM_API_KEY' not in kwargs['env']
            seen.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', probe)
    prepare_workspace_runtime(str(root), str(work), restricted=True)
    assert len(seen) == 3
    outside = root.parent / 'external-env'
    original([sys.executable, '-m', 'venv', '--without-pip', str(outside)],
             check=True, capture_output=True, timeout=60)
    other_source, other_work = root.parent / 'other-source', root.parent / 'other-work'
    other_source.mkdir()
    other_work.mkdir()
    (other_source / '.venv').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='authorized environment root'):
        prepare_workspace_runtime(str(other_source), str(other_work), restricted=True)



def test_no_source_change_does_not_delete_new_task_environment_packages(repo, monkeypatch):
    import sys
    from pathlib import Path
    from lib.project_mod.workspace_runtime import prepare_workspace_runtime
    root, _work, _owner = repo
    (root / '.gitignore').write_text('.venv/\n')
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(root / '.venv')],
                   check=True, capture_output=True, timeout=60)
    work = automatic_workspace(repo, monkeypatch)
    prepare_workspace_runtime(str(root), str(work))
    packages = Path(subprocess.check_output([str(work / '.venv/bin/python'), '-c',
        'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip())
    (packages / 'newly_installed.py').write_text('VALUE = "user installation"\n')
    assert automatic_settle(repo)['state'] == 'delivered'
    assert work.exists()
    assert (packages / 'newly_installed.py').read_text() == 'VALUE = "user installation"\n'



def test_terminal_task_capture_refreshes_an_earlier_explicit_checkpoint(repo, monkeypatch):
    root, _work, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'early.txt').write_text('early task change\n')
    early = ic.checkpoint_workspace(str(root), 'automatic', user_id=owner)['checkpointSha']
    (work / 'late.txt').write_text('latest completed task change\n')
    receipt = automatic_settle(repo)
    assert receipt['state'] == 'delivered', receipt
    assert receipt['checkpointSha'] != early
    assert (root / 'early.txt').exists() and (root / 'late.txt').exists()

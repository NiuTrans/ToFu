"""Task checkouts on a local volume stay reconstructible from Git, like lanes.

Every test owns a canonical repository, a SQLite integration authority, a data
root and a "local" placement root under tmp_path. Task checkouts default to the
data root, so the fixture opts in with ``TOFU_LOCAL_WORKSPACES=on`` as an
operator would; the volume decision is replaced so that root is selected, and
nothing touches the developer's home.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.project_mod import delivery
from lib.tasks_pkg import workspace_delivery
from serverctl_pkg import local_placement

pytestmark = pytest.mark.unit

OWNER = 7


def git(cwd, *args, check=True):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(['git', '-C', str(cwd), *args], env=env, text=True,
                            capture_output=True, timeout=60)
    if check:
        assert result.returncode == 0, (args, result.stderr)
    return result.stdout.strip()


def worktrees(root) -> dict[str, list[str]]:
    """Registered worktree path -> its porcelain attribute lines."""
    listed = {}
    for block in git(root, 'worktree', 'list', '--porcelain').split('\n\n'):
        lines = block.splitlines()
        listed[lines[0][len('worktree '):]] = lines[1:]
    return listed


def locked(root, checkout) -> bool:
    return any(line == 'locked' or line.startswith('locked ')
               for line in worktrees(root)[str(checkout)])


@pytest.fixture
def layout(tmp_path, monkeypatch):
    from lib.storage import StorageSupervisor
    from lib.storage.runtime import StorageRuntime
    from lib.storage.service import install_runtime_for_test
    runtime = StorageRuntime(supervisor=StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60), auto_restart=False)
    install_runtime_for_test(runtime)
    runtime.start()
    data, local = tmp_path / 'data', tmp_path / 'local-state'
    placed = local / 'workspaces'
    monkeypatch.setattr(ic, 'data_root', lambda: str(data))
    monkeypatch.setattr(ic, '_PLACED_WORKSPACE_ROOTS', {})
    monkeypatch.setattr(ic, '_start_or_wake_worker', lambda: None)
    monkeypatch.setattr(ic, '_push', lambda *args, **kwargs: None)
    monkeypatch.setattr(delivery, '_canonical_in_use', lambda *args: False)
    for name in ('TOFU_INTEGRATION_WORKSPACE_DIR', 'XDG_STATE_HOME'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TOFU_LOCAL_WORKSPACES', 'on')
    monkeypatch.setenv('TOFU_LOCAL_DIR', str(local))
    monkeypatch.setenv('TOFU_INTEGRATION_TEST_CMD', '')

    def choose(source, purpose, **_kwargs):
        # A temporary directory is never a placement root, so only the volume
        # verdict is stubbed; the operator's switch still decides.
        mode = local_placement.placement_mode(purpose)
        if mode == 'off':
            return local_placement.Placement(False, purpose, mode, 'disabled')
        return local_placement.Placement(True, purpose, mode, 'selected', root=str(placed))

    monkeypatch.setattr(local_placement, 'choose_local_root', choose)
    root = tmp_path / 'canonical'
    root.mkdir()
    git(root, 'init', '-q', '-b', 'main')
    git(root, 'config', 'user.name', 'Local checkout test')
    git(root, 'config', 'user.email', 'local@example.invalid')
    (root / '.gitignore').write_text('.tofu/\nbuild/\n')
    (root / 'base.txt').write_text('base\n')
    git(root, 'add', '-A')
    git(root, 'commit', '-qm', 'base')
    try:
        yield {'root': root, 'data': data, 'placed': placed, 'tmp': tmp_path}
    finally:
        install_runtime_for_test(None)


def create(layout, task_id='worker') -> Path:
    created = ic.create_task_workspace(str(layout['root']), task_id, user_id=OWNER,
                                       conversation_id='conversation')
    return Path(created['workspacePath'])


def admin(checkout: Path) -> Path:
    return Path((checkout / '.git').read_text().split('gitdir:', 1)[1].strip())


def test_saving_work_in_progress_leaves_head_and_index_byte_identical(layout, monkeypatch):
    root = layout['root']
    checkout = create(layout)
    assert checkout.is_relative_to(layout['placed'])
    (checkout / 'base.txt').write_text('staged\n')
    git(checkout, 'add', 'base.txt')
    (checkout / 'base.txt').write_text('staged, then edited\n')
    (checkout / 'new.txt').write_text('untracked work\n')
    (checkout / 'build').mkdir()
    (checkout / 'build' / 'output.bin').write_bytes(b'ignored output')
    index, head_file = admin(checkout) / 'index', admin(checkout) / 'HEAD'
    head = git(checkout, 'rev-parse', 'HEAD')
    before = (index.read_bytes(), index.stat().st_mtime_ns, head_file.read_bytes())

    wip = ic.save_workspace_wip(str(root), 'worker', user_id=OWNER)

    assert (index.read_bytes(), index.stat().st_mtime_ns, head_file.read_bytes()) == before
    assert git(root, 'rev-parse', ic._wip_ref(OWNER, 'worker')) == wip
    assert git(root, 'rev-parse', wip + '^') == head
    assert git(root, 'show', f'{wip}:base.txt') == 'staged, then edited'
    assert git(root, 'show', f'{wip}:new.txt') == 'untracked work'
    assert 'build/output.bin' not in git(root, 'ls-tree', '-r', '--name-only', wip)
    assert 'Tofu-Checkpoint: none' in git(root, 'cat-file', 'commit', wip)

    # A checkout on the data volume persists with it and is not saved.
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(layout['tmp'] / 'managed'))
    on_data_volume = create(layout, 'data-volume')
    (on_data_volume / 'new.txt').write_text('work\n')
    assert ic.save_workspace_wip(str(root), 'data-volume', user_id=OWNER) == ''
    assert not git(root, 'rev-parse', '--verify', '--quiet', ic._wip_ref(OWNER, 'data-volume'),
                   check=False)


def test_the_restart_pause_and_round_settlement_save_work_in_progress(layout):
    root = layout['root']
    checkout = create(layout)
    delivery.bind_task_workspace(str(root), 'worker', user_id=OWNER, conversation_id='c')
    (checkout / 'paused.txt').write_text('edit before the restart\n')

    receipt = delivery.settle_task_workspace(str(root), 'worker', user_id=OWNER,
                                             outcome='interrupted')

    assert (receipt['state'], receipt['taskOutcome']) == ('working', 'interrupted')
    assert not git(root, 'rev-parse', '--verify', '--quiet', ic._checkpoint_ref(OWNER, 'worker'),
                   check=False), 'a pause still owes no capture'
    paused = git(root, 'rev-parse', ic._wip_ref(OWNER, 'worker'))
    assert git(root, 'show', f'{paused}:paused.txt') == 'edit before the restart'

    (checkout / 'paused.txt').write_text('edit of the resumed attempt\n')
    task = {'id': 'worker', '_userId': OWNER, '_workspaceDelivery': {'repositories': [
        {'canonicalRoot': str(root), 'workspacePath': str(checkout), 'workId': 'worker'}]}}
    fenced = dict(task, _workspaceDeliveryOwnershipRejected=True)
    assert workspace_delivery.save_worker_workspaces(fenced) == []
    assert git(root, 'rev-parse', ic._wip_ref(OWNER, 'worker')) == paused

    saved = workspace_delivery.save_worker_workspaces(task)

    assert saved == [{'checkout': str(checkout),
                      'wip': git(root, 'rev-parse', ic._wip_ref(OWNER, 'worker'))}]
    assert git(root, 'show', f"{saved[0]['wip']}:paused.txt") == 'edit of the resumed attempt'


def test_a_save_that_finishes_after_a_newer_one_is_dropped(layout, monkeypatch):
    root = layout['root']
    checkout = create(layout)
    from lib.project_mod import lanes
    original = lanes.capture_working_tree
    newer = []

    def capture_then_lose_the_race(path):
        captured = original(path)
        if not newer:
            newer.append('')
            (checkout / 'base.txt').write_text('newer\n')
            newer[0] = ic.save_workspace_wip(str(root), 'worker', user_id=OWNER)
        return captured

    monkeypatch.setattr(lanes, 'capture_working_tree', capture_then_lose_the_race)

    assert ic.save_workspace_wip(str(root), 'worker', user_id=OWNER) == ''
    assert newer[0] and git(root, 'rev-parse', ic._wip_ref(OWNER, 'worker')) == newer[0]
    assert git(root, 'show', f'{newer[0]}:base.txt') == 'newer'


def test_a_deleted_checkout_is_restored_at_its_registered_path_with_an_event(layout, monkeypatch):
    root = layout['root']
    git(root, 'config', '--local', 'tofu.workspaceMode', 'isolated')
    # Managed-repository shared data is linked into worker checkouts, never versioned.
    (root / 'datasets').mkdir()
    (root / 'datasets' / 'corpus.bin').write_bytes(b'shared data')
    with open(root / '.git' / 'info' / 'exclude', 'a', encoding='utf-8') as exclude:
        exclude.write('/datasets\n')
    (root / '.git' / 'tofu-managed.json').write_text(
        json.dumps({'version': 1, 'shared': ['datasets'], 'skipped': []}), encoding='ascii')
    monkeypatch.setattr('lib.agent_core.events.emit_phase', lambda *args, **kwargs: None)
    first = {'id': 'restored-worker', 'convId': 'conversation', '_userId': OWNER,
             'status': 'running', 'config': {'projectPath': str(root)}}
    resumed = dict(first, config={'projectPath': str(root)})
    try:
        workspace_delivery.bind_worker_workspace(first)
        checkout = Path(first['config']['projectPath'])
        assert checkout.is_relative_to(layout['placed'])
        head = git(checkout, 'rev-parse', 'HEAD')
        (checkout / 'base.txt').write_text('edited\n')
        (checkout / 'new.txt').write_text('untracked work\n')
        (checkout / 'build').mkdir()
        (checkout / 'build' / 'output.bin').write_bytes(b'ignored output')
        bound = first['_workspaceDelivery']['repositories'][0]
        state.set_workspace_meta(user_id=OWNER, project_root=str(root), task_id='restored-worker',
                                 patch={'_workspaceDelivery': dict(
                                     bound, runtimePrepared=True, runtimePaths=['.venv'])},
                                 now=1.0)
        [saved] = workspace_delivery.save_worker_workspaces(first)
        shutil.rmtree(checkout)

        workspace_delivery.bind_worker_workspace(resumed)

        assert resumed['config']['projectPath'] == str(checkout)
        assert (checkout / 'base.txt').read_text() == 'edited\n'
        assert (checkout / 'new.txt').read_text() == 'untracked work\n'
        assert not (checkout / 'build').exists()
        assert os.readlink(checkout / 'datasets') == str(root / 'datasets')
        assert git(checkout, 'rev-parse', 'HEAD') == head
        assert git(checkout, 'diff', '--name-only') == 'base.txt'
        assert git(checkout, 'diff', '--cached', '--name-only') == ''
        assert git(checkout, 'ls-files', '--others', '--exclude-standard') == 'new.txt'
        assert locked(root, checkout)
        row = resumed['_workspaceDelivery']['repositories'][0]
        assert 'runtimePrepared' not in row and 'runtimePaths' not in row
        durable = state.get_workspace(str(root), 'restored-worker', user_id=OWNER)
        assert 'runtimePrepared' not in durable['origin']['_workspaceDelivery']
        _rows, events = state.status_rows(str(root), user_id=OWNER)
        [event] = [item for item in events if item['kind'] == 'workspace_restored']
        assert event['task_id'] == 'restored-worker' and saved['wip'] in event['detail']
        [advice] = resumed['_projectOverlapAdvisories']
        assert '[Workspace restored]' in advice['value']
        assert saved['wip'][:12] in advice['value'] and str(checkout) in advice['value']
    finally:
        from lib import project_mod
        project_mod.config.clear_conv_state('restored-worker')


def test_restoration_takes_the_newest_of_checkpoint_and_saved_work(layout):
    root = layout['root']
    checkout = create(layout)
    (checkout / 'base.txt').write_text('saved\n')
    ic.save_workspace_wip(str(root), 'worker', user_id=OWNER)
    (checkout / 'base.txt').write_text('checkpointed\n')
    checkpoint = ic.checkpoint_workspace(str(root), 'worker', user_id=OWNER)['checkpointSha']
    base = state.get_workspace(str(root), 'worker', user_id=OWNER)['base_sha']
    shutil.rmtree(checkout)

    receipt = ic.restore_task_workspace(str(root), 'worker', user_id=OWNER)

    assert (receipt['source'], receipt['sourceSha'], receipt['head']) == (
        'checkpoint', checkpoint, base)
    assert (checkout / 'base.txt').read_text() == 'checkpointed\n'
    assert git(checkout, 'diff', '--name-only') == 'base.txt'

    (checkout / 'base.txt').write_text('saved after the checkpoint\n')
    wip = ic.save_workspace_wip(str(root), 'worker', user_id=OWNER)
    shutil.rmtree(checkout)

    receipt = ic.restore_task_workspace(str(root), 'worker', user_id=OWNER)

    assert (receipt['source'], receipt['sourceSha'], receipt['head']) == ('wip', wip, base)
    assert (checkout / 'base.txt').read_text() == 'saved after the checkpoint\n'


def test_restoration_without_saved_work_rebuilds_the_task_input(layout):
    root = layout['root']
    (root / 'context.txt').write_text('uncommitted context the task started from\n')
    checkout = create(layout)
    snapshot = git(root, 'rev-parse', ic._input_ref(OWNER, 'worker'))
    shutil.rmtree(checkout)

    receipt = ic.restore_task_workspace(str(root), 'worker', user_id=OWNER)

    assert (receipt['source'], receipt['sourceSha'], receipt['head']) == (
        'input', snapshot, snapshot)
    assert (checkout / 'context.txt').read_text() == 'uncommitted context the task started from\n'
    assert git(checkout, 'status', '--porcelain') == ''
    assert ic.restore_task_workspace(str(root), 'worker', user_id=OWNER) is None


def test_commits_made_after_the_last_save_stay_reachable_when_the_checkout_is_lost(layout):
    root = layout['root']
    checkout = create(layout)
    wip = ic.save_workspace_wip(str(root), 'worker', user_id=OWNER)
    (checkout / 'base.txt').write_text('committed in the checkout\n')
    git(checkout, 'commit', '-qam', 'worker commit')
    committed = git(checkout, 'rev-parse', 'HEAD')
    shutil.rmtree(checkout)

    receipt = ic.restore_task_workspace(str(root), 'worker', user_id=OWNER)

    assert (receipt['sourceSha'], receipt['lostHead']) == (wip, committed)
    assert git(root, 'rev-parse', ic._lost_head_ref(OWNER, 'worker')) == committed


def test_a_locked_local_checkout_survives_a_prune_from_a_host_that_cannot_see_it(
        layout, monkeypatch):
    root, hidden = layout['root'], layout['tmp'] / 'not-visible-here'
    checkout = create(layout)
    assert locked(root, checkout)

    checkout.rename(hidden)
    git(root, 'worktree', 'prune', '--expire=now')
    assert str(checkout) in worktrees(root)
    hidden.rename(checkout)
    assert git(checkout, 'rev-parse', '--show-toplevel') == str(checkout)

    # Retirement still removes it, lock and registration included.
    ic._remove_controlled_worktree(root, checkout)
    assert not checkout.exists() and str(checkout) not in worktrees(root)

    # The lock is what protected it: an unlocked data-volume checkout is pruned.
    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(layout['tmp'] / 'managed'))
    on_data_volume = create(layout, 'data-volume')
    assert not locked(root, on_data_volume)
    on_data_volume.rename(hidden)
    git(root, 'worktree', 'prune', '--expire=now')
    assert str(on_data_volume) not in worktrees(root)


def test_a_checkout_the_local_volume_cannot_hold_goes_to_the_data_root(layout, monkeypatch):
    root = layout['root']
    (root / 'large.bin').write_bytes(b'\0' * 1_000_000)
    git(root, 'add', 'large.bin')
    git(root, 'commit', '-qm', 'large')
    asked = []

    def no_room(path, required_bytes=0, *, required_inodes=0, **_kwargs):
        asked.append((Path(path), required_bytes, required_inodes))
        return False

    monkeypatch.setattr(local_placement, 'has_free_space', no_room)

    checkout = create(layout)

    assert checkout.is_relative_to(layout['data'] / 'integration' / 'worktrees')
    assert not locked(root, checkout)
    [(path, required_bytes, required_inodes)] = asked
    assert path.is_relative_to(layout['placed'])
    assert required_bytes >= 1_000_000 + len('base\n') and required_inodes >= 3
    # Scratch checkouts (gates, merges, repair proposals) fall back the same way.
    assert ic._checkout_root(root, 'HEAD') == layout['data'] / 'integration' / 'worktrees'


def test_restoration_refuses_a_volume_that_cannot_hold_the_checkout(layout, monkeypatch):
    root = layout['root']
    checkout = create(layout)
    shutil.rmtree(checkout)
    monkeypatch.setattr(local_placement, 'has_free_space', lambda *args, **kwargs: False)

    with pytest.raises(ic.IntegrationError, match='cannot hold the restored checkout'):
        ic.restore_task_workspace(str(root), 'worker', user_id=OWNER)
    assert not checkout.exists()


def test_discard_saves_the_work_and_removes_the_registered_local_checkout(layout, monkeypatch):
    root = layout['root']
    checkout = create(layout)
    (checkout / 'base.txt').write_text('discarded edit\n')
    (checkout / 'new.txt').write_text('discarded untracked file\n')
    # Placement changed after creation: today's root is elsewhere, the row still names this one.
    later = layout['tmp'] / 'later-local-root'
    monkeypatch.setattr(ic, '_PLACED_WORKSPACE_ROOTS', {})
    monkeypatch.setattr(local_placement, 'choose_local_root', lambda source, purpose, **_kwargs:
                        local_placement.Placement(True, purpose, 'on', 'selected', root=str(later)))
    assert not ic._workspace_root().is_relative_to(layout['placed'])
    admitted, original = [], ic._destination_lock

    @contextlib.contextmanager
    def recorded(destination, **kwargs):
        admitted.append(Path(destination))
        with original(destination, **kwargs):
            yield

    monkeypatch.setattr(ic, '_destination_lock', recorded)

    assert ic.discard_workspace(str(root), 'worker', user_id=OWNER)['state'] == 'discarded'

    assert admitted == [checkout]
    assert not checkout.exists() and str(checkout) not in worktrees(root)
    wip = git(root, 'rev-parse', ic._wip_ref(OWNER, 'worker'))
    assert git(root, 'show', f'{wip}:base.txt') == 'discarded edit'
    assert git(root, 'show', f'{wip}:new.txt') == 'discarded untracked file'
    assert state.get_workspace(str(root), 'worker', user_id=OWNER)['state'] == 'discarded'
    _rows, events = state.status_rows(str(root), user_id=OWNER)
    assert [item['detail'] for item in events if item['kind'] == 'checkout_removed'] != []


def test_discard_keeps_checkouts_git_cannot_rebuild_or_that_an_attempt_works_in(
        layout, monkeypatch):
    root = layout['root']
    history = create(layout, 'with-history')
    (history / '.tofu' / 'file-history').mkdir(parents=True)
    (history / '.tofu' / 'file-history' / 'snapshots.jsonl').write_text('{"id": "undo"}\n')

    ic.discard_workspace(str(root), 'with-history', user_id=OWNER)

    assert (history / '.tofu' / 'file-history' / 'snapshots.jsonl').is_file()
    assert locked(root, history)

    working = create(layout, 'working')
    delivery.bind_task_workspace(str(root), 'working', user_id=OWNER, conversation_id='c')
    (working / 'base.txt').write_text('edit of the running attempt\n')

    ic.discard_workspace(str(root), 'working', user_id=OWNER)

    assert (working / 'base.txt').read_text() == 'edit of the running attempt\n'
    receipt = delivery.settle_task_workspace(str(root), 'working', user_id=OWNER,
                                             outcome='completed')
    assert receipt['state'] == 'preserved'
    assert not working.exists() and str(working) not in worktrees(root)
    wip = git(root, 'rev-parse', ic._wip_ref(OWNER, 'working'))
    assert git(root, 'show', f'{wip}:base.txt') == 'edit of the running attempt'

    monkeypatch.setenv('TOFU_INTEGRATION_WORKSPACE_DIR', str(layout['tmp'] / 'managed'))
    on_data_volume = create(layout, 'data-volume')
    ic.discard_workspace(str(root), 'data-volume', user_id=OWNER)
    assert on_data_volume.is_dir(), 'a data-volume checkout keeps its directory'


def test_reclaiming_a_delivered_local_checkout_removes_its_lock_and_registration(layout):
    root = layout['root']
    checkout = create(layout)
    delivery.bind_task_workspace(str(root), 'worker', user_id=OWNER, conversation_id='c')
    assert locked(root, checkout)

    delivery.settle_task_workspace(str(root), 'worker', user_id=OWNER, outcome='completed')
    delivery.resume_pending_workspace_deliveries()

    row = state.get_workspace(str(root), 'worker', user_id=OWNER)
    assert row['origin']['delivery']['state'] == 'delivered'
    assert row['origin']['sourceRetirement'] == 'reclaimed'
    assert not checkout.exists() and str(checkout) not in worktrees(root)

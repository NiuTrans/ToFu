"""Mixed effects use one physical order and protect Git/lease administration."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from lib.project_mod import workspace_lease as leases

pytestmark = pytest.mark.unit


@pytest.fixture
def linked(tmp_path):
    common = tmp_path / 'git-administration'
    common.mkdir()
    checkouts = []
    for name in ('a', 'z', 'zz'):
        root = tmp_path / name
        root.mkdir()
        admin = common / 'worktrees' / name
        admin.mkdir(parents=True)
        (admin / 'commondir').write_text('../..')
        (root / '.git').write_text('gitdir: ' + str(admin))
        (root / 'source').write_text('before')
        (admin / 'index').write_text('index')
        checkouts.append(root)
    return common, checkouts


@contextmanager
def process_holder(api, target):
    source = ('import sys; from lib.project_mod import workspace_lease as m; '
              'cm=getattr(m,sys.argv[1])(sys.argv[2]); cm.__enter__(); '
              'print("ready",flush=True); sys.stdin.read()')
    child = subprocess.Popen([sys.executable, '-c', source, api, str(target)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        yield
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()


def clean():
    assert not leases._registry
    assert not leases._active_descriptors


@pytest.mark.parametrize('reverse', [False, True])
def test_external_git_index_file_and_metadata_effect_exclude_each_other(linked, reverse):
    common, (a, _, _) = linked
    index = common / 'worktrees' / 'a' / 'index'
    api, target = ('workspace_file_write_lease', index) if reverse else ('workspace_git_metadata_lease', a)
    with process_holder(api, target):
        contender = leases.workspace_git_metadata_lease if reverse else leases.workspace_file_write_lease
        with pytest.raises(leases.WorkspaceBusyError):
            with contender(a if reverse else index):
                pytest.fail('raw Git administration edit bypassed metadata ownership')
        with leases.workspace_file_write_lease(a / 'source'):
            pass  # The same metadata holder never excludes ordinary source Edit.
    clean()


def test_git_common_directory_lock_storage_is_fenced(linked):
    common, (a, _, _) = linked
    with process_holder('workspace_file_write_lease', a / 'source'):
        with pytest.raises(leases.WorkspaceBusyError):
            with leases.workspace_tree_write_lease(common / 'tofu-workspace-leases'):
                pytest.fail('cooperative removal could split Git checkout lock inodes')
    clean()


@pytest.mark.parametrize('offset', ['self', 'ancestor', 'descendant'])
def test_internal_neutral_namespace_cannot_be_destructively_admitted(tmp_path, monkeypatch, offset):
    neutral = tmp_path / 'neutral'
    neutral.mkdir(mode=0o700)
    monkeypatch.setattr(leases, '_tree_lock_namespace', lambda: neutral)
    target = {'self': neutral, 'ancestor': tmp_path, 'descendant': neutral / 'lock'}[offset]
    with pytest.raises(leases.WorkspaceBusyError, match='Internal workspace lease namespace'):
        with leases.workspace_tree_write_lease(target):
            pytest.fail('neutral lock namespace could be retired')
    with pytest.raises(leases.WorkspaceBusyError, match='Internal workspace lease namespace'):
        with leases.workspace_file_write_lease(neutral / 'lock'):
            pytest.fail('neutral lock inode could be rebound')
    clean()


def test_metadata_order_is_independent_of_linked_checkout_alias(linked):
    _, (a, _, zz) = linked
    assert leases.lease_claim_order(('metadata', a)) == leases.lease_claim_order(('metadata', zz))


def run_competing_transactions(left, right):
    start = threading.Barrier(2)
    errors, entered = [], []

    def run(claims, name):
        try:
            start.wait(timeout=5)
            with leases.workspace_claims_lease(claims, timeout=2):
                entered.append(name)
                time.sleep(.01)  # Keep a real conflicting set live for the peer.
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=run, args=(left, 'left')),
               threading.Thread(target=run, args=(right, 'right'))]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(5)
    assert all(not worker.is_alive() for worker in workers)
    assert not errors
    assert set(entered) == {'left', 'right'}
    clean()


def test_mixed_metadata_and_root_aliases_do_not_invert_physical_admission(linked):
    _, (a, z, zz) = linked
    run_competing_transactions([('root', z), ('metadata', zz)],
                               [('metadata', a), ('root', z)])


def test_mixed_destructive_tree_and_peer_root_do_not_deadlock(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    for root in (a, b):
        (root / '.git').mkdir(parents=True)
    run_competing_transactions([('tree', a), ('root', b)],
                               [('root', a), ('tree', b)])


def test_stronger_combined_claim_preserves_reentry_without_promotion(linked):
    _, (a, _, _) = linked
    with leases.workspace_claims_lease([('metadata', a), ('root', a), ('file', a / 'source')]):
        with leases.workspace_write_lease(a):
            with leases.workspace_file_write_lease(a / 'source'):
                assert leases.workspace_lease_fds()
    clean()


@pytest.mark.parametrize('api', ['single', 'batch'])
@pytest.mark.parametrize('pointer', ['marker', 'commondir'])
def test_raw_git_topology_changes_do_not_split_file_exclusion(linked, api, pointer):
    common, (a, _, _) = linked
    target = a / '.git' if pointer == 'marker' else common / 'worktrees' / 'a' / 'commondir'
    factory = (lambda: leases.workspace_file_write_lease(target)) if api == 'single' else (
        lambda: leases.workspace_claims_lease([('file', target)]))
    with pytest.raises(leases.WorkspaceBusyError, match='identity metadata cannot be rebound'):
        with factory():
            pytest.fail('raw pointer edit could change the live same-file lock namespace')
    clean()


def test_nonstandard_native_administration_pointer_is_not_an_ordinary_file(tmp_path):
    admin = tmp_path / 'custom-admin'
    admin.mkdir()
    (admin / 'HEAD').write_text('ref: refs/heads/main\n')
    (admin / 'commondir').write_text('../common')
    with pytest.raises(leases.WorkspaceBusyError, match='identity metadata cannot be rebound'):
        with leases.workspace_file_write_lease(admin / 'commondir'):
            pytest.fail('nonstandard Git administration bypassed topology admission')
    clean()


def test_controlled_retirement_retains_linked_checkout_with_active_raw_index(linked, tmp_path, monkeypatch):
    from lib import integration_control
    common, (a, _, _) = linked
    monkeypatch.setattr(integration_control, '_workspace_root', lambda: tmp_path)
    monkeypatch.setattr(integration_control, '_git', lambda *args, **kwargs:
                        pytest.fail('retirement bypassed active Git administration'))
    with process_holder('workspace_file_write_lease', common / 'worktrees' / 'a' / 'index'):
        integration_control._remove_controlled_worktree(a, a)
        assert (a / 'source').read_text() == 'before'
    clean()

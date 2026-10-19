"""Precise effect isolation and destructive real-path fencing contracts."""
import os
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest
from lib.project_mod import workspace_lease as leases

pytestmark = pytest.mark.unit


@pytest.fixture
def checkouts(tmp_path):
    parent = tmp_path / 'parent'
    child = parent / 'nested'
    external = tmp_path / 'external' / 'linked'
    for root in (parent, child, external):
        root.mkdir(parents=True)
        (root / '.git').mkdir()
        (root / 'a').write_text('a')
    # Linked administrative directory is physically under the parent's .git.
    admin = parent / '.git' / 'worktrees' / 'linked'
    admin.mkdir(parents=True)
    (admin / 'commondir').write_text('../..')
    (external / '.git').rmdir()
    (external / '.git').write_text('gitdir: ' + str(admin))
    return parent, child, external


def in_thread(call):
    errors = []
    def run():
        try:
            call()
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=run)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert not errors


@contextmanager
def process_holder(api, target):
    code = ('import sys; from lib.project_mod import workspace_lease as m; '
            'cm=getattr(m,sys.argv[1])(sys.argv[2]); cm.__enter__(); '
            'print("ready",flush=True); sys.stdin.read()')
    child = subprocess.Popen([sys.executable, '-c', code, api, str(target)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        yield
    finally:
        child.stdin.close()
        child.wait(timeout=5)
        child.stdout.close()


@pytest.mark.parametrize('holder', ['workspace_write_lease', 'workspace_read_lease',
                                    'workspace_git_metadata_lease'])
def test_parent_checkout_operations_overlap_nested_edit(checkouts, holder):
    parent, child, _ = checkouts
    with process_holder(holder, parent):
        with leases.workspace_file_write_lease(child / 'a'):
            assert leases.workspace_lease_fds()
    assert not leases._registry
    assert not leases._active_descriptors


@pytest.mark.parametrize('holder', ['workspace_write_lease', 'workspace_read_lease'])
def test_own_checkout_full_scope_still_excludes_edit(checkouts, holder):
    _, child, _ = checkouts
    with process_holder(holder, child):
        with pytest.raises(leases.WorkspaceBusyError):
            with leases.workspace_file_write_lease(child / 'a'):
                pytest.fail('same checkout transformation/snapshot weakened')


def test_common_metadata_serializes_linked_but_not_files(checkouts):
    parent, _, external = checkouts
    with process_holder('workspace_git_metadata_lease', parent):
        with leases.workspace_file_write_lease(parent / 'a'):
            pass
        with leases.workspace_file_write_lease(external / 'a'):
            pass
        with pytest.raises(leases.WorkspaceBusyError):
            with leases.workspace_git_metadata_lease(external):
                pytest.fail('common metadata did not serialize')
    assert not leases._registry


@pytest.mark.parametrize('holder', ['workspace_write_lease', 'workspace_read_lease',
                                    'workspace_git_metadata_lease'])
@pytest.mark.parametrize('combined', [False, True])
@pytest.mark.parametrize('reverse', [False, True])
def test_checkout_scope_does_not_serialize_linked_worktrees(checkouts, holder, combined, reverse):
    parent, _, external = checkouts
    held, target = (external, parent) if reverse else (parent, external)
    with process_holder(holder, held):
        admission = (leases.workspace_claims_lease([('root', target)]) if combined
                     else leases.workspace_write_lease(target))
        with admission:
            (target / 'a').write_text('independent edit')
        with leases.workspace_read_lease(target):
            assert (target / 'a').read_text() == 'independent edit'
    assert not leases._registry
    assert not leases._active_descriptors


@pytest.mark.parametrize('holder', ['workspace_write_lease', 'workspace_read_lease',
                                    'workspace_git_metadata_lease'])
@pytest.mark.parametrize('combined', [False, True])
def test_own_checkout_writer_still_excludes_snapshots_and_metadata(checkouts, holder, combined):
    parent, _, _ = checkouts
    with process_holder(holder, parent):
        admission = (leases.workspace_claims_lease([('root', parent)]) if combined
                     else leases.workspace_write_lease(parent))
        with pytest.raises(leases.WorkspaceBusyError):
            with admission:
                pytest.fail('same checkout exclusion was lost')
    assert not leases._registry
    assert not leases._active_descriptors


@pytest.mark.parametrize('target_kind', ['checkout', 'external-parent', 'admin-parent'])
@pytest.mark.parametrize('reverse', [False, True])
def test_destructive_targets_fence_external_linked_descendants(checkouts, target_kind, reverse):
    parent, _, external = checkouts
    target = {'checkout': external, 'external-parent': external.parent,
              'admin-parent': parent}[target_kind]
    holder_api = 'workspace_file_write_lease' if reverse else 'workspace_tree_write_lease'
    holder_target = external / 'a' if reverse else target
    with process_holder(holder_api, holder_target):
        api = leases.workspace_tree_write_lease if reverse else leases.workspace_file_write_lease
        contender = target if reverse else external / 'a'
        with pytest.raises(leases.WorkspaceBusyError):
            with api(contender):
                pytest.fail('real destructive descendant ignored')
    assert not leases._registry
    assert not leases._active_descriptors


def test_disjoint_tree_target_overlaps_and_held_descriptors_survive(checkouts, tmp_path):
    _, child, _ = checkouts
    with leases.workspace_tree_write_lease(tmp_path / 'unrelated'):
        fds = leases.workspace_lease_fds()
        in_thread(lambda: _enter_edit(child / 'a'))
        assert leases.workspace_lease_fds() == fds
        for fd in fds:
            os.fstat(fd)
    assert not leases._registry


def _enter_edit(path):
    with leases.workspace_file_write_lease(path):
        assert leases.workspace_lease_fds()


@pytest.mark.parametrize('api', [leases.workspace_git_metadata_lease,
                                leases.workspace_tree_write_lease])
def test_new_scope_pre_cancel_and_exception_cleanup(checkouts, api):
    parent, _, _ = checkouts
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(leases.WorkspaceLeaseCancelled):
        with api(parent, cancel_event=cancel):
            pytest.fail('cancelled admission entered')
    with pytest.raises(RuntimeError):
        with api(parent):
            raise RuntimeError('body fault')
    assert not leases._registry
    assert not leases._active_descriptors


def test_all_claim_scopes_order_and_canonicalize(checkouts, tmp_path):
    parent, child, _ = checkouts
    alias = tmp_path / 'alias'
    alias.symlink_to(child)
    for scope in ('root', 'file', 'metadata', 'tree'):
        assert leases.lease_claim_order((scope, child)) == leases.lease_claim_order((scope, alias))
    claims = [('file', child / 'a'), ('root', child), ('tree', parent), ('metadata', child)]
    assert sorted(claims, key=leases.lease_claim_order)[0] == ('tree', parent)


def test_tree_outer_reentry_and_root_parent_to_tree_child(checkouts):
    parent, child, _ = checkouts
    with leases.workspace_tree_write_lease(parent):
        with leases.workspace_write_lease(child):
            with leases.workspace_file_write_lease(child / 'a'):
                pass
    with leases.workspace_write_lease(parent):
        with leases.workspace_tree_write_lease(child):
            pass
        # Shared lifecycle barriers cannot be promoted by dropping a live flock.
        before = leases.workspace_lease_fds()
        with pytest.raises(leases.WorkspaceBusyError):
            with leases.workspace_tree_write_lease(parent):
                pytest.fail('same-target SH promotion dropped live protection')
        assert leases.workspace_lease_fds() == before
    assert not leases._registry


def test_tree_target_rebound_during_wait_fails_closed(checkouts, tmp_path, monkeypatch):
    _, _, external = checkouts
    target = external.parent
    waiting = threading.Event()
    original = leases._try_acquire
    def observed(*args):
        result = original(*args)
        if result not in ('enter', 'acquire'):
            waiting.set()
        return result
    monkeypatch.setattr(leases, '_try_acquire', observed)
    errors = []
    def wait():
        try:
            with pytest.raises(leases.WorkspaceBusyError, match='Tree target changed'):
                with leases.workspace_tree_write_lease(target, timeout=5):
                    pytest.fail('rebound non-Git destructive target entered')
        except BaseException as error:
            errors.append(error)
    with leases.workspace_file_write_lease(external / 'a'):
        worker = threading.Thread(target=wait)
        worker.start()
        assert waiting.wait(5)
        target.rename(tmp_path / 'old-external')
        target.mkdir()
    worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert not leases._registry


def test_tree_namespace_is_independent_of_tmpdir(checkouts, tmp_path):
    _, child, _ = checkouts
    environment = dict(os.environ, TMPDIR=str(tmp_path))
    with leases.workspace_tree_write_lease(child):
        code = ('import sys; from lib.project_mod.workspace_lease import '
                'workspace_file_write_lease, WorkspaceBusyError; '
                '\ntry:\n with workspace_file_write_lease(sys.argv[1]): sys.exit(9)'
                '\nexcept WorkspaceBusyError: pass')
        result = subprocess.run([sys.executable, '-c', code, str(child / 'a')],
                                env=environment, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr

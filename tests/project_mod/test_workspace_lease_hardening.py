"""Regression boundaries for binding, publication and cross-wrapper admission."""
import hashlib
import os
import subprocess
import sys
import threading

import pytest

from lib.project_mod import workspace_lease as leases

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / 'repo'
    root.mkdir()
    (root / '.git').mkdir()
    (root / 'a').write_text('a')
    (root / 'b').write_text('b')
    return root


def clean():
    assert not leases._registry
    assert not leases._active_descriptors
    assert leases.workspace_lease_fds() == ()


def worker(call):
    errors = []

    def run():
        try:
            call()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, errors


def join(thread, errors):
    thread.join(5)
    assert not thread.is_alive(), 'admission did not reach quiescence'
    assert not errors


APIS = [leases.workspace_write_lease, leases.workspace_read_lease,
        leases.workspace_file_write_lease, leases.workspace_runtime_setup_lease]


@pytest.mark.parametrize('api', APIS)
@pytest.mark.parametrize('switch', ['common', 'gitdir', 'checkout-inode', 'common-inode'])
def test_complete_binding_switch_during_local_wait(repo, tmp_path, monkeypatch, api, switch):
    old_git = tmp_path / 'git-one'
    (repo / '.git').rename(old_git)
    common = tmp_path / 'common'
    common.mkdir()
    (old_git / 'commondir').write_text(str(common))
    marker = repo / '.git'
    marker.write_text('gitdir: ' + str(old_git))
    replacement = tmp_path / 'replacement'
    replacement.mkdir()
    if switch == 'gitdir':
        (replacement / 'commondir').write_text(str(common))
    waiting = threading.Event()
    original = leases._try_acquire

    def observed(*args):
        outcome = original(*args)
        if outcome not in ('enter', 'acquire'):
            waiting.set()
        return outcome

    monkeypatch.setattr(leases, '_try_acquire', observed)

    def wait():
        with pytest.raises(leases.WorkspaceBusyError, match='identity changed'):
            with api(repo / 'a', timeout=5):
                pytest.fail('metadata switched but operation entered')

    with leases.workspace_write_lease(repo):
        thread, errors = worker(wait)
        assert waiting.wait(5)
        if switch == 'common':
            (old_git / 'commondir').write_text(str(replacement))
        elif switch == 'gitdir':
            marker.write_text('gitdir: ' + str(replacement))
        elif switch == 'common-inode':
            common.rename(tmp_path / 'old-common')
            common.mkdir()
        else:
            repo.rename(tmp_path / 'old-checkout')
            repo.mkdir()
            marker.write_text('gitdir: ' + str(old_git))
            (repo / 'a').write_text('replacement')
    join(thread, errors)
    clean()
    with api(repo / 'a'):
        pass
    clean()


@pytest.mark.parametrize('shape', ['dangling-symlink', 'directory'])
def test_invalid_common_pointer_never_admits_a_new_namespace(repo, tmp_path, shape):
    pointer = repo / '.git' / 'commondir'
    if shape == 'dangling-symlink':
        pointer.symlink_to(tmp_path / 'missing-common-pointer')
    else:
        pointer.mkdir()
    with pytest.raises(leases.WorkspaceBusyError, match='Invalid Git common-directory pointer'):
        with leases.workspace_write_lease(repo):
            pytest.fail('Invalid metadata must not create a competing namespace')
    clean()


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='Requires POSIX FIFO metadata')
@pytest.mark.parametrize('api', APIS)
def test_special_git_marker_is_not_a_non_git_exemption(repo, api):
    (repo / '.git').rename(repo / 'old-git')
    os.mkfifo(repo / '.git')
    with pytest.raises(leases.WorkspaceBusyError, match='Invalid Git workspace marker type'):
        with api(repo / 'a'):
            pytest.fail('An existing special marker must not waive exclusion')
    clean()


def test_common_switch_during_physical_wait(repo, tmp_path, monkeypatch):
    import fcntl
    code = ('import sys; from lib.project_mod.workspace_lease import workspace_write_lease; '
            'cm=workspace_write_lease(sys.argv[1]); cm.__enter__(); '
            'print("ready", flush=True); sys.stdin.read(); cm.__exit__(None,None,None)')
    child = subprocess.Popen([sys.executable, '-c', code, str(repo)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    attempted = threading.Event()
    original = fcntl.flock

    def observed(*args):
        try:
            return original(*args)
        except BlockingIOError:
            attempted.set()
            raise

    monkeypatch.setattr(fcntl, 'flock', observed)

    def wait():
        with pytest.raises(leases.WorkspaceBusyError, match='identity changed'):
            with leases.workspace_file_write_lease(repo / 'a', timeout=5):
                pytest.fail('physical admission used stale common dir')

    try:
        assert child.stdout.readline().strip() == 'ready'
        thread, errors = worker(wait)
        assert attempted.wait(5)
        replacement = tmp_path / 'other-common'
        replacement.mkdir()
        (repo / '.git' / 'commondir').write_text(str(replacement))
        child.stdin.close()
        child.wait(timeout=5)
        join(thread, errors)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if not child.stdin.closed:
            child.stdin.close()
        child.stdout.close()
    clean()


@pytest.mark.parametrize('mode', ['upgrade', 'reader', 'file'])
@pytest.mark.parametrize('fault_type', [RuntimeError, KeyboardInterrupt])
def test_existing_fd_publication_fault_restores_outer_claim(repo, monkeypatch, mode, fault_type):
    outer = (leases.workspace_file_write_lease(repo / 'a') if mode == 'file'
             else leases.workspace_read_lease(repo))
    inner = (leases.workspace_file_write_lease(repo / 'b') if mode == 'file'
             else leases.workspace_write_lease(repo) if mode == 'upgrade'
             else leases.workspace_read_lease(repo))
    original = leases._publish_hold

    def fault(*args):
        original(*args)
        raise fault_type('after publication')

    with outer:
        fds = leases.workspace_lease_fds()
        before = {key: {name: (dict(value) if isinstance(value, dict) else value)
                        for name, value in entry.items() if name != 'cond'}
                  for key, entry in leases._registry.items()}
        with monkeypatch.context() as patch:
            patch.setattr(leases, '_publish_hold', fault)
            with pytest.raises(fault_type, match='after publication'):
                with inner:
                    pytest.fail('fault ignored')
        assert leases.workspace_lease_fds() == fds
        after = {key: {name: (dict(value) if isinstance(value, dict) else value)
                       for name, value in entry.items() if name != 'cond'}
                 for key, entry in leases._registry.items()}
        assert after == before
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    clean()
    with leases.workspace_write_lease(repo):
        pass
    clean()


@pytest.mark.parametrize('nested_api', [leases.workspace_write_lease,
                                       leases.workspace_file_write_lease])
def test_nested_checkout_fences_parent_tree_and_preserves_sibling_overlap(repo, nested_api):
    child = repo / 'aaa-child'
    sibling = repo / 'bbb-child'
    for root in (child, sibling):
        root.mkdir()
        (root / '.git').mkdir()
        (root / 'a').write_text('a')

    with nested_api(child / 'a'):
        def parent():
            with pytest.raises(leases.WorkspaceBusyError):
                with leases.workspace_tree_write_lease(repo):
                    pytest.fail('parent destructive tree ignored nested child')
            with nested_api(sibling / 'a'):
                assert leases.workspace_lease_fds()
        thread, errors = worker(parent)
        join(thread, errors)
        code = ('import sys; from lib.project_mod.workspace_lease import '
                'workspace_tree_write_lease, WorkspaceBusyError; '
                '\ntry:\n with workspace_tree_write_lease(sys.argv[1]): sys.exit(9)'
                '\nexcept WorkspaceBusyError: pass')
        result = subprocess.run([sys.executable, '-c', code, str(repo)],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
    with leases.workspace_tree_write_lease(repo):
        def nested():
            with pytest.raises(leases.WorkspaceBusyError):
                with nested_api(child / 'a'):
                    pytest.fail('nested child ignored parent whole root')
        thread, errors = worker(nested)
        join(thread, errors)
    clean()


def test_shared_order_groups_checkout_not_raw_target_path(tmp_path):
    a, b = tmp_path / 'A', tmp_path / 'B'
    for root in (a, b):
        root.mkdir()
        (root / '.git').mkdir()
    nested = b / 'aaa-child'
    nested.mkdir()
    (nested / '.git').mkdir()
    targets = [str(nested / 'a'), str(b / 'z')]
    claims = [('file', target) for target in targets]
    assert sorted(claims) == claims  # Raw target order has the opposite direction.
    assert sorted(claims, key=leases.lease_claim_order) == [claims[1], claims[0]]
    root_claim = ('root', str(b))
    assert sorted([claims[0], root_claim], key=leases.lease_claim_order)[0] == root_claim
    alias = tmp_path / 'alias'
    alias.symlink_to(b, target_is_directory=True)
    assert leases.lease_claim_order(('file', alias / 'z')) == leases.lease_claim_order(claims[1])


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'directory-symlink'])
def test_unsafe_lock_metadata_is_not_modified_or_leaked(repo, tmp_path, kind):
    victim = tmp_path / 'victim'
    victim.write_text('untouched')
    lock_dir = repo / '.git' / 'tofu-workspace-leases'
    if kind == 'directory-symlink':
        outside = tmp_path / 'outside'
        outside.mkdir()
        lock_dir.symlink_to(outside, target_is_directory=True)
    else:
        lock_dir.mkdir()
        lock = lock_dir / (hashlib.sha256(os.fsencode(str(repo))).hexdigest() + '.lock')
        if kind == 'symlink':
            lock.symlink_to(victim)
        else:
            os.link(victim, lock)
    with pytest.raises(leases.WorkspaceBusyError):
        with leases.workspace_write_lease(repo):
            pytest.fail('unsafe lock accepted')
    assert victim.read_text() == 'untouched'
    if kind == 'directory-symlink':
        assert not list(outside.iterdir())
    clean()


def test_hardlinked_target_refuses_ambiguous_file_identity(repo):
    os.link(repo / 'a', repo / 'alias')
    with pytest.raises(leases.WorkspaceBusyError, match='Hardlinked'):
        with leases.workspace_file_write_lease(repo / 'a'):
            pytest.fail('hardlink alias split target locks')
    clean()


def test_nested_cancellation_releases_ancestor_and_pending_child(repo, monkeypatch):
    child = repo / 'nested'
    child.mkdir()
    (child / '.git').mkdir()
    (child / 'a').write_text('a')
    cancellation = threading.Event()
    waiting = threading.Event()
    original = leases._try_acquire

    def observed(*args):
        result = original(*args)
        if result not in ('enter', 'acquire'):
            waiting.set()
        return result

    monkeypatch.setattr(leases, '_try_acquire', observed)

    def wait():
        with pytest.raises(leases.WorkspaceLeaseCancelled):
            with leases.workspace_file_write_lease(child / 'a', timeout=5,
                                                   cancel_event=cancellation):
                pytest.fail('cancelled operation entered')

    with leases.workspace_write_lease(child):
        before = leases.workspace_lease_fds()
        thread, errors = worker(wait)
        assert waiting.wait(5)
        cancellation.set()
        join(thread, errors)
        assert leases.workspace_lease_fds() == before
        assert leases._active_descriptors == set(before)
    clean()


def test_batch_edit_uses_shared_checkout_order(repo, monkeypatch):
    from contextlib import contextmanager
    from lib.project_mod.write_tools import _ops
    child = repo / 'aaa-child'
    child.mkdir()
    (child / '.git').mkdir()
    targets = [str(child / 'a'), str(repo / 'z')]
    observed = []

    @contextmanager
    def admitted(path, **kwargs):
        observed.append(path)
        yield

    monkeypatch.setattr(leases, 'workspace_file_write_lease', admitted)
    monkeypatch.setattr(_ops, '_enforce_not_readonly', lambda *args, **kwargs: None)
    monkeypatch.setattr(_ops, '_resolve_base', lambda base, path, **kwargs: (base, path))

    @_ops._with_workspace_writer
    def operation(base, edits):
        return 'done'

    assert operation(str(repo), [{'path': target} for target in targets]) == 'done'
    assert observed == sorted(targets, key=lambda path: leases.lease_claim_order(('file', path)))
    assert observed == targets[::-1]
    clean()


@pytest.mark.parametrize('pointer', ['gitdir', 'commondir'])
def test_file_lease_refuses_metadata_rebinding(repo, tmp_path, pointer):
    if pointer == 'gitdir':
        actual = tmp_path / 'actual-git'
        (repo / '.git').rename(actual)
        target = repo / '.git'
        target.write_text('gitdir: ' + str(actual))
    else:
        target = repo / '.git' / 'commondir'
        target.write_text(str(repo / '.git'))
    before = target.read_bytes()
    with pytest.raises(leases.WorkspaceBusyError, match='cannot be rebound'):
        with leases.workspace_file_write_lease(target):
            pytest.fail('metadata rebinding was admitted by file scope')
    assert target.read_bytes() == before
    clean()


def test_lock_directory_rebinding_between_check_and_open_fails_closed(repo, tmp_path, monkeypatch):
    lock_dir = repo / '.git' / 'tofu-workspace-leases'
    outside = tmp_path / 'outside'
    outside.mkdir()
    original = leases.os.open
    switched = False

    def redirected(path, flags, *args, **kwargs):
        nonlocal switched
        if path == lock_dir and not switched:
            switched = True
            lock_dir.rename(repo / '.git' / 'old-lock-directory')
            lock_dir.symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(leases.os, 'open', redirected)
    with pytest.raises(leases.WorkspaceBusyError):
        with leases.workspace_write_lease(repo):
            pytest.fail('directory race followed symlink')
    assert switched
    assert not list(outside.iterdir())
    clean()


@pytest.mark.parametrize('mode', ['initial', 'upgrade', 'file'])
def test_cancellation_after_publication_rolls_back_transaction(repo, monkeypatch, mode):
    from contextlib import nullcontext
    cancelled = threading.Event()
    original = leases._publish_hold

    def stop(*args):
        original(*args)
        cancelled.set()

    outer = (leases.workspace_read_lease(repo) if mode == 'upgrade'
             else leases.workspace_file_write_lease(repo / 'a') if mode == 'file'
             else nullcontext())
    api = leases.workspace_file_write_lease if mode == 'file' else leases.workspace_write_lease
    with outer:
        before = leases.workspace_lease_fds()
        with monkeypatch.context() as patch:
            patch.setattr(leases, '_publish_hold', stop)
            with pytest.raises(leases.WorkspaceLeaseCancelled):
                with api(repo / 'b', cancel_event=cancelled):
                    pytest.fail('cancellation after publication entered body')
        assert leases.workspace_lease_fds() == before
        assert leases._active_descriptors == set(before)
    clean()


def test_topology_discovery_visits_each_ancestor_once(repo, monkeypatch):
    observed = []
    original = leases._binding_at_root
    def inspect(root):
        observed.append(str(root))
        return original(root)
    monkeypatch.setattr(leases, '_binding_at_root', inspect)
    chain = leases._checkout_chain(repo / 'a')
    assert chain[-1][0] == str(repo)
    assert len(observed) == len(set(observed)), 'Do not repeat full prefix walks per discovered root'
    clean()


@pytest.mark.parametrize('api', APIS)
def test_new_nested_marker_during_physical_admission_is_revalidated(repo, monkeypatch, api):
    nested = repo / 'new-child'
    nested.mkdir()
    target = nested / 'a'
    target.write_text('a')
    original = leases._acquire_process_lock
    inserted = False
    def acquire(*args, **kwargs):
        nonlocal inserted
        result = original(*args, **kwargs)
        if not inserted:
            (nested / '.git').mkdir()
            inserted = True
        return result
    monkeypatch.setattr(leases, '_acquire_process_lock', acquire)
    with pytest.raises(leases.WorkspaceBusyError, match='identity changed'):
        with api(target):
            pytest.fail('Known-root optimization must not miss a new nested binding')
    assert inserted
    clean()


def test_non_directory_candidate_keeps_enclosing_checkout_identity(repo):
    expected = leases._checkout_identity(repo)
    assert leases._checkout_identity(repo / 'a' / 'impossible-child') == expected
    assert leases._checkout_chain(repo / 'a' / 'impossible-child')[-1][:2] == expected
    clean()

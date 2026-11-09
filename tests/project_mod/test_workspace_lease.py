"""Executable specification for checkout admission and descriptor lifetime."""
import hashlib
import os
import subprocess
import sys
import threading

import pytest

from lib.project_mod import workspace_lease as lease_mod
from lib.project_mod.workspace_lease import (
    WorkspaceBusyError, WorkspaceLeaseCancelled,
    workspace_lease_fds, workspace_read_lease,
    workspace_write_lease,
)

pytestmark = pytest.mark.unit


def _lock_name(root):
    return hashlib.sha256(os.fsencode(str(root))).hexdigest() + '.lock'


def _repo(path):
    path.mkdir()
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    subprocess.run(['git', '-C', str(path), 'config', 'user.email', 'test@example.invalid'], check=True)
    subprocess.run(['git', '-C', str(path), 'config', 'user.name', 'Test'], check=True)
    (path / 'file.txt').write_text('initial\n')
    subprocess.run(['git', '-C', str(path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(path), 'commit', '-qm', 'initial'], check=True)
    return path


def _thread(call):
    errors = []

    def run():
        try:
            call()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    return worker, errors


def _join(worker, errors):
    worker.join(5)
    assert not worker.is_alive(), 'lease worker did not reach quiescence'
    assert not errors


def _assert_closed(fds):
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def _assert_clean():
    assert workspace_lease_fds() == ()
    assert not lease_mod._registry
    assert not lease_mod._registry_roots
    assert not lease_mod._active_descriptors


class _ObservedCancellation:
    """Signals an actual wait probe; no elapsed-time assertions."""

    def __init__(self):
        self.observed = threading.Event()
        self.cancelled = threading.Event()
        self.probes = 0

    def is_set(self):
        self.probes += 1
        if self.probes >= 3:  # before identity, local gate, then waiting retry
            self.observed.set()
        return self.cancelled.is_set()


class TestWorkspaceWriterLease:
    def test_reentrant_and_independent_worktrees(self, tmp_path):
        repo = _repo(tmp_path / 'repo')
        sibling = tmp_path / 'sibling'
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-b', 'sibling', str(sibling)], check=True)
        alias = tmp_path / 'alias'
        alias.symlink_to(repo, target_is_directory=True)
        with workspace_write_lease(repo):
            fds = workspace_lease_fds()
            with workspace_write_lease(alias / 'file.txt'):
                assert set(fds).issubset(workspace_lease_fds())

            def other_thread():
                with pytest.raises(WorkspaceBusyError, match=r'(Workspace|Git common-directory metadata) write is active'):
                    with workspace_write_lease(repo):
                        pytest.fail('same checkout overlapped')
                with lease_mod.workspace_file_write_lease(sibling / 'file.txt'):
                    assert workspace_lease_fds()  # Independent source files remain usable.
                with workspace_write_lease(sibling):
                    assert workspace_lease_fds()

            worker, errors = _thread(other_thread)
            _join(worker, errors)
        _assert_closed(fds)
        _assert_clean()
        with workspace_write_lease(sibling):
            assert workspace_lease_fds()
        expected = set()  # Each key's lock file goes with its last release.
        actual = {path.name for path in (repo / '.git' / 'tofu-workspace-leases').glob('*.lock')}
        assert actual == expected
        _assert_clean()

    def test_rebound_checkout_is_refused_while_its_root_is_held(self, tmp_path):
        first, second = _repo(tmp_path / 'first'), _repo(tmp_path / 'second')
        checkout = tmp_path / 'checkout'
        checkout.mkdir()
        marker = checkout / '.git'
        marker.write_text(f'gitdir: {first / ".git"}\n')
        with workspace_write_lease(checkout):
            marker.write_text(f'gitdir: {second / ".git"}\n')
            with pytest.raises(WorkspaceBusyError, match='rebound during an active lease'):
                with workspace_write_lease(checkout):
                    pass
        # Last departure retires the old identity; the new binding is admitted.
        with workspace_write_lease(checkout):
            assert (os.path.realpath(checkout), os.path.realpath(second / '.git')) in (
                lease_mod._registry_roots.values())
        _assert_clean()

    def test_exception_and_process_exit_release(self, tmp_path):
        repo = _repo(tmp_path / 'repo')
        with pytest.raises(RuntimeError, match='failure'):
            with workspace_write_lease(repo):
                raise RuntimeError('failure')
        code = (
            'import sys; from lib.project_mod.workspace_lease import workspace_write_lease; '
            'cm=workspace_write_lease(sys.argv[1]); cm.__enter__(); '
            'print("ready", flush=True); sys.stdin.read()')
        child = subprocess.Popen([sys.executable, '-c', code, str(repo)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == 'ready'
            with pytest.raises(WorkspaceBusyError, match='holder type unknown'):
                with workspace_write_lease(repo):
                    pytest.fail('cross-process holder overlapped')
            child.kill()
            child.wait(timeout=5)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()
        with workspace_write_lease(repo):
            assert workspace_lease_fds()
        _assert_clean()

    def test_lock_symlink_fails_closed(self, tmp_path):
        import hashlib
        repo = _repo(tmp_path / 'repo')
        lock_dir = repo / '.git' / 'tofu-workspace-leases'
        lock_dir.mkdir()
        lock = lock_dir / (hashlib.sha256(os.fsencode(str(repo))).hexdigest() + '.lock')
        victim = tmp_path / 'victim'
        victim.write_text('keep')
        lock.symlink_to(victim)
        with pytest.raises(WorkspaceBusyError):
            with workspace_write_lease(repo):
                pytest.fail('unsafe lock metadata accepted')
        assert victim.read_text() == 'keep'
        _assert_clean()

    def test_inherited_fd_retains_exclusion_after_parent_release(self, tmp_path):
        repo = _repo(tmp_path / 'repo')
        with workspace_write_lease(repo):
            fds = workspace_lease_fds()
            child = subprocess.Popen(
                [sys.executable, '-c', 'import sys; print("ready", flush=True); sys.stdin.read()'],
                pass_fds=fds, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == 'ready'
        _assert_closed(fds)
        lock = repo / '.git' / 'tofu-workspace-leases' / _lock_name(repo)
        try:
            assert lock.exists(), 'release unlinked a lock file an inherited descriptor holds'
            with pytest.raises(WorkspaceBusyError, match='holder type unknown'):
                with workspace_write_lease(repo):
                    pytest.fail('inherited child descriptor stopped excluding')
        finally:
            child.stdin.close()
            child.wait(timeout=5)
            child.stdout.close()
        with workspace_write_lease(repo):
            assert workspace_lease_fds()
        assert not lock.exists()
        _assert_clean()

    def test_descriptor_validation_failure_closes_fd(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path / 'repo')
        opened = []
        original_open = os.open

        def track_open(*args, **kwargs):
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def fail_stat(fd):
            raise OSError('injected fstat failure')

        with monkeypatch.context() as patch:
            patch.setattr(lease_mod.os, 'open', track_open)
            patch.setattr(lease_mod.os, 'fstat', fail_stat)
            with pytest.raises(WorkspaceBusyError, match='injected fstat failure'):
                with workspace_write_lease(repo):
                    pytest.fail('invalid descriptor admitted')
        assert opened
        _assert_closed(opened)
        _assert_clean()


class TestWorkspaceAdmissionWait:
    @pytest.mark.parametrize('mode', [workspace_read_lease, workspace_write_lease])
    @pytest.mark.parametrize('timeout', [5, None, 5000])
    def test_waiter_acquires_after_writer_release(self, tmp_path, mode, timeout):
        repo = _repo(tmp_path / 'repo')
        cancel = _ObservedCancellation()
        acquired = threading.Event()

        def wait_for_lease():
            with mode(repo, timeout=timeout, cancel_event=cancel):
                acquired.set()

        with workspace_write_lease(repo):
            worker, errors = _thread(wait_for_lease)
            assert cancel.observed.wait(5)
            assert not acquired.is_set()
        _join(worker, errors)
        assert acquired.is_set()
        _assert_clean()

    @pytest.mark.parametrize('mode', [workspace_read_lease, workspace_write_lease])
    @pytest.mark.parametrize('timeout', [5, None, 5000])
    def test_waiter_cancels_without_entering_or_leaking(self, tmp_path, mode, timeout):
        repo = _repo(tmp_path / 'repo')
        cancel = _ObservedCancellation()
        acquired = threading.Event()

        def wait_for_lease():
            with pytest.raises(WorkspaceLeaseCancelled, match='admission cancelled'):
                with mode(repo, timeout=timeout, cancel_event=cancel):
                    acquired.set()

        with workspace_write_lease(repo):
            fds = workspace_lease_fds()
            worker, errors = _thread(wait_for_lease)
            try:
                assert cancel.observed.wait(5)
            finally:
                cancel.cancelled.set()
                _join(worker, errors)
            assert not acquired.is_set()
            assert workspace_lease_fds() == fds
            assert lease_mod._active_descriptors == set(fds)
        _assert_closed(fds)
        _assert_clean()

    def test_process_waiter_cancels_and_closes_pending_fd(self, tmp_path, monkeypatch):
        import fcntl
        repo = _repo(tmp_path / 'repo')
        code = (
            'import sys; from lib.project_mod.workspace_lease import workspace_write_lease; '
            'cm=workspace_write_lease(sys.argv[1]); cm.__enter__(); '
            'print("ready", flush=True); sys.stdin.read()')
        child = subprocess.Popen([sys.executable, '-c', code, str(repo)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        cancel, attempted = threading.Event(), threading.Event()
        original_flock = fcntl.flock

        def probe_flock(*args):
            try:
                return original_flock(*args)
            except BlockingIOError:
                attempted.set()
                raise

        def waiter():
            with pytest.raises(WorkspaceLeaseCancelled):
                with workspace_write_lease(repo, timeout=None, cancel_event=cancel):
                    pytest.fail('cancelled process waiter entered')

        try:
            assert child.stdout.readline().strip() == 'ready'
            monkeypatch.setattr(fcntl, 'flock', probe_flock)
            worker, errors = _thread(waiter)
            try:
                assert attempted.wait(5)
            finally:
                cancel.set()
                _join(worker, errors)
            _assert_clean()
        finally:
            child.stdin.close()
            child.wait(timeout=5)
            child.stdout.close()
        with workspace_write_lease(repo):
            assert workspace_lease_fds()
        _assert_clean()

    @pytest.mark.parametrize('timeout', [-1, float('inf'), float('nan')])
    def test_invalid_timeout_rejected_before_acquisition(self, tmp_path, timeout):
        with pytest.raises(ValueError, match='nonnegative and finite'):
            with workspace_write_lease(tmp_path, timeout=timeout):
                pytest.fail('invalid admission timeout accepted')
        _assert_clean()

    def test_non_git_cancelled_before_body(self, tmp_path):
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(WorkspaceLeaseCancelled):
            with workspace_write_lease(tmp_path, cancel_event=cancel):
                pytest.fail('cancelled non-Git operation entered')
        _assert_clean()


class TestWorkspaceReadLease:
    def test_two_readers_block_writer_with_accurate_type(self, tmp_path):
        repo = _repo(tmp_path / 'repo')
        ready, release = threading.Event(), threading.Event()

        def reader():
            with workspace_read_lease(repo):
                ready.set()
                assert release.wait(5)

        with workspace_read_lease(repo):
            worker, errors = _thread(reader)
            try:
                assert ready.wait(5)
                with pytest.raises(WorkspaceBusyError, match=r'(Workspace|Git common-directory metadata) read is active'):
                    with workspace_write_lease(repo):
                        pytest.fail('writer ignored another explicit reader')
            finally:
                release.set()
                _join(worker, errors)
        with workspace_write_lease(repo):
            assert workspace_lease_fds()
        _assert_clean()

    def test_reader_blocked_by_writer(self, tmp_path):
        repo = _repo(tmp_path / 'repo')
        with workspace_write_lease(repo):
            def reader():
                with pytest.raises(WorkspaceBusyError, match=r'(Workspace|Git common-directory metadata) write is active'):
                    with workspace_read_lease(repo):
                        pytest.fail('reader overlapped a writer')
            worker, errors = _thread(reader)
            _join(worker, errors)
        _assert_clean()

    def test_writer_read_reentry_preserves_exclusion(self, tmp_path):
        repo = _repo(tmp_path / 'repo')
        with workspace_write_lease(repo):
            with workspace_read_lease(repo):
                with workspace_write_lease(repo):
                    assert workspace_lease_fds()
            def writer():
                with pytest.raises(WorkspaceBusyError, match=r'(Workspace|Git common-directory metadata) write is active'):
                    with workspace_write_lease(repo):
                        pytest.fail('nested read downgraded writer')
            worker, errors = _thread(writer)
            _join(worker, errors)
        _assert_clean()

    def test_sole_reader_upgrades_and_restores_outer_read(self, tmp_path):
        repo = _repo(tmp_path / 'repo')
        with workspace_read_lease(repo):
            fds = workspace_lease_fds()
            with workspace_write_lease(repo):
                assert workspace_lease_fds() == fds
                with workspace_read_lease(repo):
                    assert workspace_lease_fds() == fds
            def writer():
                with pytest.raises(WorkspaceBusyError, match=r'(Workspace|Git common-directory metadata) read is active'):
                    with workspace_write_lease(repo):
                        pytest.fail('upgrade lost outer reader')
            worker, errors = _thread(writer)
            _join(worker, errors)
        _assert_closed(fds)
        _assert_clean()

    def test_each_shared_reader_inherits_live_fd_after_first_reader_exits(self, tmp_path):
        repo = _repo(tmp_path / 'repo')
        ready, parent_released = threading.Event(), threading.Event()
        children, inherited = [], []

        def second_reader():
            with workspace_read_lease(repo):
                inherited.append(workspace_lease_fds())
                ready.set()
                assert parent_released.wait(5)
                assert workspace_lease_fds() == inherited[0]
                for fd in workspace_lease_fds():
                    os.fstat(fd)
                child = subprocess.Popen(
                    [sys.executable, '-c', 'import sys; print("ready", flush=True); sys.stdin.read()'],
                    pass_fds=workspace_lease_fds(), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, text=True)
                children.append(child)
                assert child.stdout.readline().strip() == 'ready'

        with workspace_read_lease(repo):
            fds = workspace_lease_fds()
            worker, errors = _thread(second_reader)
            assert ready.wait(5)
            assert inherited == [fds]
        parent_released.set()
        try:
            _join(worker, errors)
            _assert_closed(fds)
            with pytest.raises(WorkspaceBusyError, match='holder type unknown'):
                with workspace_write_lease(repo):
                    pytest.fail('second reader child lost inherited exclusion')
        finally:
            parent_released.set()
            for child in children:
                child.stdin.close()
                child.wait(timeout=5)
                child.stdout.close()
        with workspace_write_lease(repo):
            assert workspace_lease_fds()
        _assert_clean()

    @pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires POSIX fork cleanup')
    @pytest.mark.parametrize('mode', [workspace_read_lease, workspace_write_lease])
    def test_fork_cleanup_does_not_close_reused_fd(self, tmp_path, mode):
        repo = _repo(tmp_path / 'repo')
        read_fd, write_fd = os.pipe()
        child_pid = None
        try:
            with mode(repo):
                fds = workspace_lease_fds()
                child_pid = os.fork()
                if child_pid == 0:
                    os.close(read_fd)
                    try:
                        _assert_clean()
                        _assert_closed(fds)
                        reused_fd = os.open(os.devnull, os.O_RDONLY)
                        if reused_fd != fds[0]:
                            os.dup2(reused_fd, fds[0])
                            os.close(reused_fd)
                            reused_fd = fds[0]
                    except BaseException as error:
                        os.write(write_fd, ('error:' + repr(error)).encode())
                        os._exit(1)
                else:
                    assert workspace_lease_fds() == fds
            if child_pid == 0:
                # Fork reset the inherited context's FD and Condition. Its old
                # descriptor number now belongs to a new child-owned file.
                try:
                    os.fstat(reused_fd)
                    os.close(reused_fd)
                    os.write(write_fd, b'clean')
                    os.close(write_fd)
                    os._exit(0)
                except BaseException as error:
                    os.write(write_fd, ('error:' + repr(error)).encode())
                    os._exit(1)
            os.close(write_fd)
            write_fd = None
            assert os.read(read_fd, 4096) == b'clean'
            _, status = os.waitpid(child_pid, 0)
            child_pid = None
            assert os.waitstatus_to_exitcode(status) == 0
            _assert_closed(fds)
            with workspace_write_lease(repo):
                assert workspace_lease_fds()
            _assert_clean()
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
            if child_pid:
                os.waitpid(child_pid, 0)



def _hold_in_child(script, *args):
    """A child process that runs `script`, prints ready and waits for stdin."""
    child = subprocess.Popen([sys.executable, '-c', script + '; print("ready", flush=True); sys.stdin.read()',
                              *map(str, args)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == 'ready'
    return child


def _release_child(child):
    child.stdin.close()
    child.wait(timeout=5)
    child.stdout.close()


class TestLockFileReclamation:
    """Lock files are reclaimed with the lease lifecycle (2026-11-08: 2.45M
    never-unlinked files had accumulated in the per-user tree namespace)."""

    @pytest.fixture(autouse=True)
    def private_tree_namespace(self, tmp_path, monkeypatch):
        namespace = tmp_path / 'tree'
        namespace.mkdir()
        monkeypatch.setattr(lease_mod, '_tree_lock_namespace', lambda: namespace)
        return namespace

    def test_an_acquirer_whose_file_was_reclaimed_after_its_open_locks_the_recreated_file(
            self, tmp_path, monkeypatch):
        repo = _repo(tmp_path / 'repo')
        root = os.path.realpath(repo)
        original_open = lease_mod._open_lock
        reclaimed = []

        def open_then_reclaim(common, opened_root):
            fd, fcntl = original_open(common, opened_root)
            if opened_root == root and not reclaimed:
                # A concurrent release's reclamation removes the name between
                # this open and its flock.
                os.unlink(common / 'tofu-workspace-leases' / _lock_name(opened_root))
                reclaimed.append(os.fstat(fd).st_ino)
            return fd, fcntl

        monkeypatch.setattr(lease_mod, '_open_lock', open_then_reclaim)
        with workspace_write_lease(repo):
            assert reclaimed
            for fd in workspace_lease_fds():
                assert os.fstat(fd).st_nlink == 1, 'a lease holds a lock file reclamation removed'
        _assert_clean()

    def test_an_acquirer_never_locks_a_reclaimed_file_another_process_replaced(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path / 'repo')
        root = os.path.realpath(repo)
        original_open = lease_mod._open_lock
        children = []

        def open_then_replace(common, opened_root):
            fd, fcntl = original_open(common, opened_root)
            if opened_root == root and not children:
                os.unlink(common / 'tofu-workspace-leases' / _lock_name(opened_root))
                children.append(_hold_in_child(
                    'import sys; from lib.project_mod.workspace_lease import workspace_write_lease; '
                    'cm=workspace_write_lease(sys.argv[1]); cm.__enter__()', repo))
            return fd, fcntl

        monkeypatch.setattr(lease_mod, '_open_lock', open_then_replace)
        try:
            with pytest.raises(WorkspaceBusyError):
                with workspace_write_lease(repo):
                    pytest.fail('admitted on a reclaimed file while another process holds its replacement')
            assert children
        finally:
            for child in children:
                _release_child(child)
        _assert_clean()

    def test_sweep_reclaims_only_unheld_regular_lock_files(self, tmp_path):
        repo = _repo(tmp_path / 'repo')
        common = repo / '.git'
        lock_dir = common / 'tofu-workspace-leases'
        lock_dir.mkdir()
        stale = [lock_dir / f'{index:064x}.lock' for index in range(3)]
        for path in stale:
            path.touch()
        shared = lock_dir / ('a' * 64 + '.lock')
        shared.touch()
        victim = tmp_path / 'victim'
        victim.write_text('keep')
        (lock_dir / ('b' * 64 + '.lock')).symlink_to(victim)
        os.link(victim, lock_dir / ('c' * 64 + '.lock'))
        child = _hold_in_child(
            'import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR); fcntl.flock(fd, fcntl.LOCK_SH)', shared)
        try:
            with workspace_write_lease(repo):
                own = lock_dir / _lock_name(repo)
                assert lease_mod.reclaim_unheld_lock_files(common) == 3
                assert sorted(path.name for path in lock_dir.iterdir()) == sorted(
                    [shared.name, own.name, 'b' * 64 + '.lock', 'c' * 64 + '.lock'])
                assert victim.read_text() == 'keep'
        finally:
            _release_child(child)
        assert lease_mod.reclaim_unheld_lock_files(common) == 1, 'the exited holder left its file unheld'
        assert sorted(path.name for path in lock_dir.iterdir()) == ['b' * 64 + '.lock', 'c' * 64 + '.lock']
        assert victim.read_text() == 'keep'
        _assert_clean()

    def test_the_boot_sweep_reclaims_the_tree_namespace(self, private_tree_namespace):
        lock_dir = private_tree_namespace / 'tofu-workspace-leases'
        lock_dir.mkdir()
        (lock_dir / ('d' * 64 + '.lock')).touch()
        lease_mod.start_lock_file_reclamation().join(timeout=30)
        assert not list(lock_dir.iterdir())

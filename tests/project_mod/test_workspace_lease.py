"""Executable specification for checkout admission and descriptor lifetime."""
import hashlib
import os
import subprocess
import sys
import threading

import pytest

from lib.project_mod import workspace_lease as lease_mod
from lib.project_mod.workspace_lease import (
    WorkspaceBusyError, WorkspaceLeaseCancelled, workspace_lease_fds,
    workspace_read_lease, workspace_write_lease,
)

pytestmark = pytest.mark.unit


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
        expected = {hashlib.sha256(os.fsencode(str(path))).hexdigest() + '.lock'
                    for path in (repo, sibling, sibling / 'file.txt')}
        actual = {path.name for path in (repo / '.git' / 'tofu-workspace-leases').glob('*.lock')}
        assert actual == expected
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
        try:
            with pytest.raises(WorkspaceBusyError, match='holder type unknown'):
                with workspace_write_lease(repo):
                    pytest.fail('inherited child descriptor stopped excluding')
        finally:
            child.stdin.close()
            child.wait(timeout=5)
            child.stdout.close()
        with workspace_write_lease(repo):
            assert workspace_lease_fds()
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


def test_user_data_write_does_not_contend_with_source_lease_but_git_still_does(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0.05')
    repo = _repo(tmp_path / 'repo')
    from lib.project_mod.write_tools import _ops
    import lib.project_mod.run_command as commands
    data = repo / 'data'
    data.mkdir()
    ready, release = threading.Event(), threading.Event()
    def source_writer():
        with workspace_write_lease(repo):
            ready.set()
            assert release.wait(10)
    thread = threading.Thread(target=source_writer)
    thread.start()
    try:
        assert ready.wait(5)
        result = _ops.tool_write_file(str(data), 'user.txt', 'durable user data\n')
        assert result['ok'] is True
        assert (data / 'user.txt').read_text() == 'durable user data\n'
        result = commands.tool_run_command(str(data), 'cp user.txt copied.txt')
        assert '[exit code: 0]' in result
        for command in ('git add -- user.txt', 'GIT_PAGER=cat git add -- user.txt'):
            result = commands.tool_run_command(str(data), command)
            assert '[workspace-busy]' in result
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()

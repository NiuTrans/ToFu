"""Lock-file I/O must not hold unrelated admission hostage or weaken fork safety."""
import contextlib
import os

import subprocess
import sys
from pathlib import Path
import threading
import time

import pytest

from lib.project_mod import workspace_lease as leases

pytestmark = pytest.mark.unit


@pytest.fixture
def roots(tmp_path):
    result = []
    for name in ('slow', 'independent'):
        root = tmp_path / name
        (root / '.git').mkdir(parents=True)
        (root / 'file').write_text(name)
        result.append(root)
    return result


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


def finish(thread, errors):
    thread.join(5)
    assert not thread.is_alive(), 'owned operation did not settle'
    assert not errors


def clean():
    assert leases._opening_descriptors == 0
    assert not leases._registry
    assert not leases._active_descriptors


def stalled_open(monkeypatch, root, descriptors=None):
    entered, release = threading.Event(), threading.Event()
    original = leases._open_lock
    def open_lock(common, key):
        value = original(common, key)
        if key == str(root):
            if descriptors is not None:
                descriptors.append(value[0])
            entered.set()
            assert release.wait(5)
        return value
    monkeypatch.setattr(leases, '_open_lock', open_lock)
    return entered, release


@pytest.mark.parametrize('sample', range(3))
def test_unrelated_admission_finishes_before_slow_lockfile_io(monkeypatch, roots, sample):
    slow, independent = roots
    entered, release = stalled_open(monkeypatch, slow)
    done = threading.Event()
    def admit(root, completed=None):
        with leases.workspace_file_write_lease(root / 'file', timeout=3):
            if completed:
                completed.set()
    # Block the checkout barrier's physical open, rather than a target-file open.
    # A whole-root claim selects the same key the helper holds.
    def hold_slow():
        with leases.workspace_write_lease(slow, timeout=3):
            pass
    first, first_errors = worker(hold_slow)
    second = None
    started = time.monotonic()
    try:
        assert entered.wait(3)
        start_other = time.monotonic()
        second, second_errors = worker(lambda: admit(independent, done))
        assert done.wait(1), 'unrelated edit waited behind slow lock-file filesystem I/O'
        other_ms = (time.monotonic() - start_other) * 1000
        assert not release.is_set()
        time.sleep(max(0, 0.2 - (time.monotonic() - started)))
    finally:
        release.set()
        finish(first, first_errors)
        if second:
            finish(second, second_errors)
    print(f'lock_io_sample={sample} held_ms={(time.monotonic()-started)*1000:.3f} independent_ms={other_ms:.3f}')
    clean()


def _fork_probe(monkeypatch, roots, interrupt_wait=False):
    slow, _ = roots
    descriptors = []
    entered, release = stalled_open(monkeypatch, slow, descriptors)
    barrier_wait = threading.Event()
    registry_progress = threading.Event()

    parent_release = threading.Event()
    admitted = threading.Event()
    original_wait = leases._registry_condition.wait
    interrupted = []
    def observe_wait(*args, **kwargs):
        barrier_wait.set()
        if interrupt_wait and not interrupted:
            interrupted.append(True)
            raise KeyboardInterrupt('interrupted at-fork barrier')
        return original_wait(*args, **kwargs)
    monkeypatch.setattr(leases._registry_condition, 'wait', observe_wait)
    def hold():
        with leases.workspace_write_lease(slow, timeout=3):
            admitted.set()
            assert parent_release.wait(5)
    first, errors = worker(hold)
    def unblock():
        try:
            assert barrier_wait.wait(3)
            # Fork's wait must release the registry, not wait with it held.
            leases.workspace_lease_fds()
            registry_progress.set()
        finally:
            release.set()
    releaser = None
    pid = None
    try:
        assert entered.wait(3)
        releaser, release_errors = worker(unblock)
        pid = os.fork()
        if pid == 0:
            okay = (leases._opening_descriptors == 0 and not leases._registry
                    and not leases._active_descriptors)
            try:
                os.fstat(descriptors[0])
                okay = False
            except OSError:
                pass  # This owned FD must be closed in the child, not the parent.
            # The fork barrier must bind a new usable condition to its new lock.
            with leases._registry_condition:
                leases._registry_condition.notify_all()
            os._exit(0 if okay else 9)
        waited, status = os.waitpid(pid, 0)
        assert waited == pid and os.waitstatus_to_exitcode(status) == 0
        assert registry_progress.is_set()
        assert admitted.wait(3)

        os.fstat(descriptors[0])
        # Child close-only cleanup must not release the parent's exclusion.
        with pytest.raises(leases.WorkspaceBusyError):
            with leases.workspace_write_lease(slow):
                pytest.fail('child cleanup unlocked parent admission')
    finally:
        release.set()
        parent_release.set()
        finish(first, errors)
        if releaser:
            finish(releaser, release_errors)
    clean()


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='POSIX fork descriptor ownership')
@pytest.mark.parametrize('interrupted', [False, True])
def test_fork_waits_for_open_registration_without_blocking_registry(roots, interrupted):
    process = subprocess.Popen(
        [sys.executable, '-m', 'tests.project_mod.test_workspace_lock_io_isolation',
         '--fork-probe', str(roots[0]), str(int(interrupted))],
        cwd=Path(__file__).resolve().parents[2], start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stdout + stderr
    finally:
        if process.poll() is None:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()


@pytest.mark.parametrize('fault', ['open', 'io_timer', 'reservation_timer',
                                  'registration', 'registration_after', 'cancel'])
def test_open_handoff_faults_retire_reservation_and_descriptor(monkeypatch, roots, fault):
    slow, _ = roots
    original_open = leases._open_lock
    original_timer = leases.timing_phase

    original_registry = leases._timed_registry
    returned = []
    cancellation = threading.Event()
    def open_lock(common, key):
        if fault == 'open':
            raise KeyboardInterrupt('open interrupted')
        value = original_open(common, key)
        returned.append(value[0])
        if fault == 'cancel':
            cancellation.set()
        return value
    @contextlib.contextmanager
    def timer(name, **kwargs):
        with original_timer(name, **kwargs):
            yield
        if fault == 'io_timer' and name == 'workspace_lock_io':
            raise RuntimeError('timer failed after opening')

    @contextlib.contextmanager
    def registry():
        with original_registry():
            yield
        if fault == 'reservation_timer' and leases._opening_descriptors:
            raise RuntimeError('registry finalizer failed after reserving')
    class RefuseRegistration(set):
        def add(self, value):
            if fault == 'registration_after':
                super().add(value)
            raise RuntimeError('descriptor registration failed')
    with monkeypatch.context() as patch:
        patch.setattr(leases, '_open_lock', open_lock)
        patch.setattr(leases, 'timing_phase', timer)

        patch.setattr(leases, '_timed_registry', registry)
        if fault in ('registration', 'registration_after'):
            patch.setattr(leases, '_active_descriptors', RefuseRegistration())
        expected = (KeyboardInterrupt if fault == 'open' else
                    leases.WorkspaceLeaseCancelled if fault == 'cancel' else RuntimeError)
        with pytest.raises(expected):
            with leases.workspace_write_lease(slow, cancel_event=cancellation):
                pytest.fail('failed admission entered protected work')
    for fd in returned:
        with pytest.raises(OSError):
            os.fstat(fd)
    clean()
    with leases.workspace_write_lease(slow):
        pass
    clean()


def test_directory_close_failure_does_not_lose_new_lock_descriptor(monkeypatch, roots):
    import stat
    root, _ = roots
    original_open, original_close = os.open, os.close
    lock_fds = []
    failed = []
    def capture_open(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if str(path).endswith('.lock'):
            lock_fds.append(fd)
        return fd
    def close(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        original_close(fd)
        if is_directory and not failed:
            failed.append(True)
            raise RuntimeError('directory close failed after lock open')
    with monkeypatch.context() as patch:
        patch.setattr(leases.os, 'open', capture_open)
        patch.setattr(leases.os, 'close', close)
        with pytest.raises(RuntimeError, match='directory close failed'):
            with leases.workspace_write_lease(root):
                pytest.fail('failed open entered protected work')
    assert lock_fds
    for fd in lock_fds:
        with pytest.raises(OSError):
            os.fstat(fd)
    clean()


if __name__ == '__main__':
    assert sys.argv[1] == '--fork-probe'
    with pytest.MonkeyPatch.context() as patch:
        _fork_probe(patch, [Path(sys.argv[2]), None], bool(int(sys.argv[3])))


def test_interrupted_reservation_cleanup_retires_before_propagating(monkeypatch, roots):
    slow, _ = roots
    original_lock, original_open = leases._registry_lock, leases._open_lock
    armed, interrupted, descriptors = [], [], []
    error = KeyboardInterrupt('reservation cleanup acquire interrupted')
    class InterruptedLock:
        def acquire(self, *args, **kwargs):
            if armed and not interrupted:
                interrupted.append(True)
                raise error
            return original_lock.acquire(*args, **kwargs)
        def release(self):
            return original_lock.release()
        def __enter__(self):
            self.acquire()
            return self
        def __exit__(self, *args):
            self.release()
    def opened(common, key):
        value = original_open(common, key)
        if key == str(slow):
            descriptors.append(value[0])
            armed.append(True)
        return value
    with monkeypatch.context() as patch:
        patch.setattr(leases, '_registry_lock', InterruptedLock())
        patch.setattr(leases, '_open_lock', opened)
        with pytest.raises(KeyboardInterrupt) as caught:
            with leases.workspace_write_lease(slow):
                pytest.fail('interruption was silently lost')
        assert caught.value is error
        assert interrupted
        clean()
        with leases.workspace_write_lease(slow):
            pass
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)
    clean()

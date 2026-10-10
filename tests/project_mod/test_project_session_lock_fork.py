"""Forked children must neither inherit journal ownership nor retain its lock."""
import os
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.skipif(not hasattr(os, 'fork'), reason='POSIX fork boundary')]


@pytest.mark.parametrize('scenario', ['owner-thread', 'other-thread', 'descriptor-reuse',
    pytest.param('registry-budget', marks=pytest.mark.skipif(
        not os.path.isdir('/proc/self/fd'), reason='Linux descriptor budget probe'))])
def test_fork_does_not_transfer_session_ownership(tmp_path, scenario):
    program = r'''
import os, sys, threading
from lib.project_mod import session_lock as locks
locks._wait_seconds = lambda: 0
session, scenario = sys.argv[1:]
def wait_success(pid):
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, status
if scenario == 'owner-thread':
    with locks.locked_session(session):
        pid = os.fork()
        if pid == 0:
            try:
                with locks.locked_session(session):
                    os._exit(9)  # A child must contend, not inherit reentrancy.
            except locks.SessionLockError:
                os._exit(0)
        wait_success(pid)
elif scenario == 'descriptor-reuse':
    with locks.locked_session(session):
        pid = os.fork()
        if pid == 0:
            child_fd = os.open(os.devnull, os.O_RDONLY)
    if pid == 0:
        try:
            os.fstat(child_fd)
            os.close(child_fd)
            os._exit(0)
        except OSError:
            os._exit(10)
    wait_success(pid)
elif scenario == 'registry-budget':
    before = len(os.listdir('/proc/self/fd'))
    for _ in range(32):
        with locks.locked_session(session):
            assert len(locks._active_descriptors) == 1
            with locks.locked_session(session):
                assert len(locks._active_descriptors) == 1
        assert not locks._active_descriptors
    try:
        with locks.locked_session(session):
            raise RuntimeError('injected interruption')
    except RuntimeError:
        pass
    assert not locks._active_descriptors
    assert len(os.listdir('/proc/self/fd')) == before
else:
    acquired, release = threading.Event(), threading.Event()
    def owner():
        with locks.locked_session(session):
            acquired.set()
            assert release.wait(10)
    thread = threading.Thread(target=owner)
    thread.start()
    assert acquired.wait(10)
    read_end, write_end = os.pipe()
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(ready_read)
        os.write(ready_write, b'r')
        os.close(ready_write)
        os.close(write_end)
        os.read(read_end, 1)  # Stay alive while the parent releases and reacquires.
        os.close(read_end)
        os._exit(0)
    os.close(read_end)
    os.close(ready_write)
    try:
        assert os.read(ready_read, 1) == b'r'
        os.close(ready_read)
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        with locks.locked_session(session):
            pass  # The child's inherited descriptor must not retain the lock.
    finally:
        release.set()
        thread.join(10)
        os.write(write_end, b'x')
        os.close(write_end)
        wait_success(pid)
print('fork ownership isolated')
'''
    result = subprocess.run([sys.executable, '-c', program, str(tmp_path), scenario],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'fork ownership isolated' in result.stdout

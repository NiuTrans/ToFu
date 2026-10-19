"""Cancellation must retire inherited leases even when the session leader exits."""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import select
import shlex
import signal
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from lib.project_mod.run_command import _kill_process_tree, _signal_process_tree_termination, tool_run_command
from lib.project_mod.workspace_lease import workspace_write_lease

pytestmark = [pytest.mark.unit, pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux process/FD observation')]

CHILD_CODE = '''import os, signal, time
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print('CHILD_READY', os.getpid(), flush=True)
    time.sleep(30)
    os._exit(0)
print('PARENT_READY', pid, flush=True)
time.sleep(30)
'''


def _running(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_bytes().rsplit(b')', 1)[-1].split()[0] not in (b'Z', b'X')
    except FileNotFoundError:
        return False


@contextmanager
def _reap_owned_descendants():
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) or libc.prctl(36, 1, 0, 0, 0):
        pytest.skip('Cannot establish test-only descendant reaping')
    children = []
    try:
        yield children
    finally:
        for pid in children:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # This test's exact descendant already exited.
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass  # Its owning parent reaped it before adoption.
        libc.prctl(36, previous.value, 0, 0, 0)


def _spawn(children):
    from lib.project_mod.workspace_lease import workspace_lease_fds
    proc = subprocess.Popen([sys.executable, '-c', CHILD_CODE], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, pass_fds=workspace_lease_fds())
    proc._tofu_owned_pgid = proc.pid
    output = b''
    deadline = time.monotonic() + 5
    while b'CHILD_READY ' not in output or b'PARENT_READY ' not in output or output.count(b'\n') < 2:
        if not select.select([proc.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise AssertionError('Owned test child did not become ready')
        output += os.read(proc.stdout.fileno(), 4096)
    lines = output.decode().splitlines()
    child = int(next(line.split()[1] for line in lines if line.startswith('CHILD_READY')))
    children.append(child)
    return proc, child


@pytest.mark.parametrize('leader_already_reaped', [False, True])
def test_group_retirement_closes_inherited_lease_after_leader_exits(tmp_path, leader_already_reaped):
    (tmp_path / '.git').mkdir()
    with _reap_owned_descendants() as children:
        with workspace_write_lease(tmp_path):
            proc, child = _spawn(children)
            try:
                if leader_already_reaped:
                    _signal_process_tree_termination(proc)
                    proc.wait(timeout=3)
                _kill_process_tree(proc)
                assert proc.poll() is not None
                assert not _running(child), 'Leader exit alone must not release supervised lifetime'
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # This exact owned process group is already quiescent.
                proc.wait(timeout=3)
                proc.stdout.close()
                proc.stderr.close()
        with workspace_write_lease(tmp_path):
            pass


def test_live_abort_is_not_complete_until_descendant_lease_closes(tmp_path):
    (tmp_path / '.git').mkdir()
    task = {'id': 'owned-abort-test', 'aborted': False}
    with _reap_owned_descendants() as children:
        text = []
        def on_chunk(_stream, chunk):
            text.append(chunk)
            for line in ''.join(text).splitlines():
                if line.startswith('CHILD_READY '):
                    pid = int(line.split()[1])
                    if pid not in children:
                        children.append(pid)
                    task['aborted'] = True
        command = 'touch admitted.txt && ' + shlex.join([sys.executable, '-c', CHILD_CODE])
        output = tool_run_command(str(tmp_path), command, task=task, on_chunk=on_chunk)
        assert '[Command aborted by user]' in output
        assert children and not _running(children[0])
        assert '_subprocess_pid' not in task and '_subprocess_pgid' not in task
        with workspace_write_lease(tmp_path):
            pass


def test_reattached_group_retirement_waits_for_descendants(tmp_path):
    from lib.tasks_pkg.background_commands import _kill_record_tree, _pid_starttime
    (tmp_path / '.git').mkdir()
    with _reap_owned_descendants() as children:
        with workspace_write_lease(tmp_path):
            proc, child = _spawn(children)
            record = {'pid': proc.pid, 'pgid': proc.pid, 'pid_starttime': _pid_starttime(proc.pid)}
            try:
                _kill_record_tree(record)
                proc.wait(timeout=3)
                assert not _running(child)
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass  # This exact owned process group is already quiescent.
                proc.wait(timeout=3)
                proc.stdout.close()
                proc.stderr.close()
        with workspace_write_lease(tmp_path):
            pass


@pytest.mark.parametrize('interactive', [False, True])
@pytest.mark.parametrize('close_output', [False, True])
def test_success_waits_for_writer_descendants_after_leader_exit(tmp_path, interactive, close_output):
    import threading
    from lib.project_mod.workspace_lease import WorkspaceBusyError
    (tmp_path / '.git').mkdir()
    ready = tmp_path / 'descendant.pid'
    release = tmp_path / 'release-owned-descendant'
    code = '''import os, pathlib, sys, time
if os.fork() == 0:
    ready = pathlib.Path(sys.argv[1])
    pending = ready.with_suffix('.pending')
    pending.write_text(str(os.getpid()))
    pending.replace(ready)
    if sys.argv[3] == 'True':
        os.close(1)
        os.close(2)
    while not pathlib.Path(sys.argv[2]).exists():
        time.sleep(0.01)
    os._exit(0)
os._exit(0)
'''
    task = {'id': 'owned-success-test', 'aborted': False}
    groups, output, failures = [], [], []
    def on_spawn(_start_ms, _deadline_ms):
        groups.append(task['_subprocess_pgid'])
    def run():
        try:
            command = 'touch admitted.txt && ' + shlex.join([sys.executable, '-c', code, str(ready), str(release), str(close_output)])
            output.append(tool_run_command(str(tmp_path), command, task=task, on_spawn=on_spawn,
                stdin_callback=(lambda _prompt: None) if interactive else None))
        except BaseException as error:
            failures.append(error)
    with _reap_owned_descendants() as children:
        worker = threading.Thread(target=run)
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert ready.exists() and groups
            children.append(int(ready.read_text()))
            while _running(groups[0]) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert not _running(groups[0]), 'The direct session leader should have exited'
            assert worker.is_alive(), 'Success must await admitted descendants, not orphan their lease'
            with pytest.raises(WorkspaceBusyError):
                with workspace_write_lease(tmp_path):
                    pytest.fail('An admitted inheriting writer still owns this checkout')
            release.touch()
            worker.join(5)
            assert not worker.is_alive() and not failures
            assert output and '[exit code: 0]' in output[0]
            with workspace_write_lease(tmp_path):
                pass
        finally:
            release.touch()
            task['aborted'] = True
            worker.join(5)
            if worker.is_alive() and groups:
                os.killpg(groups[0], signal.SIGKILL)
                worker.join(3)


def test_stale_reattach_record_is_never_signal_authority():
    from lib.tasks_pkg.background_commands import _kill_record_tree
    with patch('lib.tasks_pkg.background_commands._record_pid_alive', return_value=False), patch('os.killpg') as kill:
        _kill_record_tree({'pid': 12345, 'pgid': 12345, 'pid_starttime': 'stale'})
        kill.assert_not_called()


@pytest.mark.parametrize('token', [None, '', 123, True, 'bogus-start'])
def test_missing_or_invalid_linux_recovered_birth_token_cannot_signal(token):
    from lib.tasks_pkg.background_commands import _kill_record_tree
    with patch('lib.tasks_pkg.background_commands._record_pid_alive', return_value=True), patch('os.killpg') as kill:
        assert _kill_record_tree({'pid': 12345, 'pgid': 12345, 'pid_starttime': token}) is False
        kill.assert_not_called()


def test_current_process_group_is_refused():
    from lib.project_mod.process_group import retire_process_group
    with patch('os.killpg') as kill, pytest.raises(ValueError):
        retire_process_group(os.getpgrp())
    kill.assert_not_called()

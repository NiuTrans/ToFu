"""Cancellation must retire inherited leases even when the session leader exits.

A finished command's result is never replaced by its group's retirement:
members left running are stopped and reported, and a group that outlives
SIGKILL becomes a typed warning while its record stays for the boot sweep.

Stop, timeout and kill also retire the groups a command's still-linked
descendants created (GNU ``timeout``, ``setsid``), which a signal to the
command's own group never reaches.
"""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import re
import shlex
import shutil
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
    try:
        while b'CHILD_READY ' not in output or b'PARENT_READY ' not in output or output.count(b'\n') < 2:
            # Wait for the child's own readiness, never a host-speed deadline:
            # EOF means it exited first, and the per-test hang guard bounds a hang.
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                raise AssertionError('Owned test child exited before becoming ready')
            output += chunk
    except BaseException:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
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


LINGERING = {
    'responds_to_term': 'sleep 300 & echo "child=$!"; echo main-done',
    'ignores_term': '(trap "" TERM; exec sleep 300) & echo "child=$!"; echo main-done',
}


def _shell_pid(output, name):
    return int(re.search(rf'^{name}=(\d+)$', output, re.M).group(1))


@pytest.mark.parametrize('interactive', [False, True])
@pytest.mark.parametrize('member', sorted(LINGERING))
def test_finished_command_keeps_its_result_and_stops_what_it_left_running(
        tmp_path, monkeypatch, interactive, member):
    from lib.background_command_projection import command_result_status
    from lib.project_mod import foreground_command_records as records
    monkeypatch.setattr(records, '_records_root', lambda: str(tmp_path / 'records'))
    with _reap_owned_descendants() as children:
        output = tool_run_command(
            str(tmp_path), LINGERING[member],
            stdin_callback=(lambda prompt: None) if interactive else None)
        child = _shell_pid(output, 'child')
        children.append(child)
        assert re.search(r'^main-done$', output, re.M)
        assert command_result_status(output) == {
            'exitCode': 0, 'timedOut': False, 'interrupted': False}
        assert output.rstrip().endswith(
            '\n[warning process-group-lingering-stopped: the command exited but '
            'left processes running in its process group; they were stopped '
            '(SIGTERM, then SIGKILL). Start long-lived processes with '
            'run_command background=true.]\n[exit code: 0]')
        assert not _running(child), 'A finished command must not leak its group members'
    assert not list((tmp_path / 'records').glob('*.json'))


@pytest.mark.parametrize('interactive', [False, True])
def test_unconfirmed_retirement_keeps_the_result_and_the_record(
        tmp_path, monkeypatch, interactive):
    """A member blocked in an uninterruptible wait (D state on a stalled network
    filesystem) stays observable after SIGKILL until that wait ends; the group
    observation seam models it."""
    from lib.background_command_projection import command_result_status
    from lib.project_mod import foreground_command_records as records, process_group
    monkeypatch.setattr(records, '_records_root', lambda: str(tmp_path / 'records'))
    stuck = set()
    observe = process_group.group_has_live_members
    monkeypatch.setattr(process_group, 'group_has_live_members',
                        lambda pgid: pgid in stuck or observe(pgid))
    task = {'aborted': False}
    seen = []

    def on_chunk(_stream, text):
        seen.append(text)
        received = ''.join(seen)
        match = re.search(r'^pgid=(\d+)$', received, re.M)
        if match and re.search(r'^part1$', received, re.M) and not stuck:
            stuck.add(int(match.group(1)))
            task['_cmd_interrupt'] = {'source': 'watchdog', 'ts': time.time(),
                                      'note': 'no output for 1822s'}

    with _reap_owned_descendants() as children:
        output = tool_run_command(
            str(tmp_path), 'echo "pgid=$$"; echo part1; sleep 300', task=task,
            on_chunk=on_chunk,
            stdin_callback=(lambda prompt: None) if interactive else None)
        pgid = _shell_pid(output, 'pgid')
        children.append(pgid)
        assert re.search(r'^part1$', output, re.M), 'collected output must survive'
        status = command_result_status(output)
        assert status['interrupted'] is True and status['exitCode'] == -1
        warning = output.index(
            f"[warning process-group-retirement-incomplete: processes of this "
            f"command's process group {pgid} were still running after SIGTERM and SIGKILL")
        assert warning < output.index('[Command interrupted by stall-watchdog: no output for 1822s')
        assert f'pgrep -a -g {pgid}' in output
        assert 'Error executing command' not in output
        assert 'Error during interactive execution' not in output
        assert not _running(pgid), 'SIGKILL still reached the group'
    retained = list((tmp_path / 'records').glob(f'*-{pgid}.json'))
    assert len(retained) == 1, 'an unconfirmed group stays recorded for the boot sweep'


def test_current_process_group_is_refused():
    from lib.project_mod.process_group import retire_process_group
    with patch('os.killpg') as kill, pytest.raises(ValueError):
        retire_process_group(os.getpgrp())
    kill.assert_not_called()


def test_an_escaped_group_cannot_name_the_current_group():
    from lib.project_mod.process_group import retire_process_group
    with patch('os.killpg') as kill, pytest.raises(ValueError):
        retire_process_group(2 ** 22 + 7, escaped_groups=[os.getpgrp()])
    kill.assert_not_called()


ESCAPED = {
    # GNU timeout moves itself and its child into a new process group.
    'timeout': ("timeout 300 sh -c 'echo \"inner=$$\"; exec sleep 300' & "
                'echo "outer=$!"; wait'),
    # A background setsid starts its own session and group without forking.
    'setsid': ("setsid sh -c 'echo \"inner=$$\"; exec sleep 300' & "
               'echo "outer=$!"; wait'),
    # A TERM-ignoring child keeps timeout waiting; only SIGKILL ends both.
    'timeout_ignoring_term': (
        "timeout 300 sh -c 'trap \"\" TERM; echo \"inner=$$\"; "
        "while :; do sleep 1; done' & echo \"outer=$!\"; wait"),
}


def _require_escape_tools():
    if shutil.which('timeout') is None or shutil.which('setsid') is None:
        pytest.skip('GNU timeout and util-linux setsid are required')


def _escaped_pids(text):
    found = [re.search(rf'^{name}=(\d+)$', text, re.M) for name in ('outer', 'inner')]
    return [int(match.group(1)) for match in found] if all(found) else None


def _spawn_escaped(shape):
    proc = subprocess.Popen(['sh', '-c', ESCAPED[shape]], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    proc._tofu_owned_pgid = proc.pid
    output = b''
    try:
        while _escaped_pids(output.decode()) is None:
            # Readiness, never a host-speed deadline; the hang guard bounds a hang.
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                raise AssertionError('Escaping test command exited before becoming ready')
            output += chunk
    except BaseException:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    return proc, _escaped_pids(output.decode())


def _close_spawned(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # This exact owned process group is already quiescent.
    proc.wait(timeout=3)
    proc.stdout.close()
    proc.stderr.close()


@pytest.mark.parametrize('interactive', [False, True])
@pytest.mark.parametrize('shape', sorted(ESCAPED))
def test_stop_retires_groups_the_command_created(tmp_path, monkeypatch, shape, interactive):
    """The kill tool and Stop set ``aborted``; nothing the command started survives."""
    from lib.project_mod import foreground_command_records as records
    _require_escape_tools()
    monkeypatch.setattr(records, '_records_root', lambda: str(tmp_path / 'records'))
    task = {'id': f'escaped-{shape}', 'aborted': False}
    seen, pids = [], []

    def on_chunk(_stream, text):
        seen.append(text)
        found = _escaped_pids(''.join(seen))
        if found and not pids:
            pids.extend(found)
            task['aborted'] = True

    with _reap_owned_descendants() as children:
        output = tool_run_command(
            str(tmp_path), ESCAPED[shape], task=task, on_chunk=on_chunk,
            stdin_callback=(lambda prompt: None) if interactive else None)
        children.extend(dict.fromkeys(pids))
        assert '[Command aborted by user]' in output
        assert 'warning process-group' not in output
        assert pids and [pid for pid in pids if _running(pid)] == [], (
            'a process that left the command group outlived Stop')
    assert not list((tmp_path / 'records').glob('*.json'))


@pytest.mark.parametrize('shape', sorted(ESCAPED))
def test_cancel_signal_reaches_groups_before_their_parent_exits(shape):
    """The cancel callback's TERM ends the shell, re-parenting what it started,
    before retirement inspects the tree: both must still reach those groups."""
    _require_escape_tools()
    with _reap_owned_descendants() as children:
        proc, pids = _spawn_escaped(shape)
        children.extend(dict.fromkeys(pids))
        try:
            _signal_process_tree_termination(proc)
            _kill_process_tree(proc)
            assert proc.poll() is not None
            assert [pid for pid in pids if _running(pid)] == []
        finally:
            _close_spawned(proc)


@pytest.mark.parametrize('shape', sorted(ESCAPED))
def test_reattached_retirement_reaches_groups_the_command_created(shape):
    from lib.tasks_pkg.background_commands import _kill_record_tree, _pid_starttime
    _require_escape_tools()
    with _reap_owned_descendants() as children:
        proc, pids = _spawn_escaped(shape)
        children.extend(dict.fromkeys(pids))
        record = {'pid': proc.pid, 'pgid': proc.pid, 'pid_starttime': _pid_starttime(proc.pid)}
        try:
            assert _kill_record_tree(record) is True
            proc.wait(timeout=3)
            assert [pid for pid in pids if _running(pid)] == []
        finally:
            _close_spawned(proc)


def test_discovery_finds_created_groups_and_trusts_no_changed_leader(monkeypatch):
    from lib.project_mod import process_group
    _require_escape_tools()
    with _reap_owned_descendants() as children:
        proc, pids = _spawn_escaped('timeout')
        children.extend(dict.fromkeys(pids))
        try:
            outer, _inner = pids
            # timeout leads its new group; its child shares it; nothing else is ours.
            assert process_group.escaped_descendant_groups(proc.pid, proc.pid) == [outer]
            real = process_group.read_process_fields
            births = iter([b'1', b'2'])

            def changed_leader(pid):
                fields = list(real(pid))
                fields[19] = next(births)
                return fields

            monkeypatch.setattr(process_group, 'read_process_fields', changed_leader)
            assert process_group.escaped_descendant_groups(proc.pid, proc.pid) == [], (
                'a leader whose identity changed during discovery links no subtree')
        finally:
            _close_spawned(proc)


def test_kill_tool_retires_what_a_background_command_started(monkeypatch, tmp_path):
    """The ``kill`` tool on a handed-off background command reaches the group
    that GNU ``timeout`` created, not only the command's own group."""
    from lib import agent_inbox
    from lib.tasks_pkg import background_commands
    from lib.tasks_pkg.handlers import _background_command as background
    _require_escape_tools()
    monkeypatch.setattr(background, 'AUTO_BACKGROUND_AFTER_S', 0.05)
    monkeypatch.setattr(background, '_queue_completion', lambda **_kwargs: True)
    monkeypatch.setattr(background_commands, '_registry', {})
    outer_file, inner_file = tmp_path / 'outer', tmp_path / 'inner'
    command = (f"timeout 300 sh -c 'echo $$ > \"{inner_file}\"; exec sleep 300' & "
               f'echo $! > "{outer_file}"; wait')
    task = {'id': 'escaped-kill', 'convId': 'conv-escaped', '_userId': 7,
            'aborted': False, 'toolRounds': []}
    agent_inbox.reset_for_test()
    try:
        with _reap_owned_descendants() as children:
            result = background.run_with_steer_handoff(
                task=task, config={}, command=command,
                execute=lambda command_task: tool_run_command(
                    str(tmp_path), command, task=command_task))
            command_id = result.split(background.BACKGROUND_COMMAND_MARKER, 1)[1]
            command_id = command_id.split('.', 1)[0].strip()
            # Readiness, never a host-speed deadline; the hang guard bounds a hang.
            while not all(path.exists() and path.read_text().strip()
                          for path in (outer_file, inner_file)):
                time.sleep(0.02)
            pids = [int(outer_file.read_text()), int(inner_file.read_text())]
            children.extend(pids)
            assert background_commands.kill_background_command(command_id, user_id=7) is True
            settled = background_commands.check_background_command(
                command_id, user_id=7, wait_s=30)
            assert settled['status'] == 'completed'
            assert '[Command aborted by user]' in settled['result']
            assert [pid for pid in pids if _running(pid)] == [], (
                'the kill tool left what the command started running')
    finally:
        agent_inbox.reset_for_test()

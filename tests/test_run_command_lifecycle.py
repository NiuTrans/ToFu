"""Focused executable specs for ``run_command`` subprocess lifecycle.

Covers the accepted-plan process-group contract:

* process-group/grandchild termination and reap (both the ``_kill_process_tree``
  primitive and the live abort path),
* cancellation-before-spawn short-circuits without spawning a subprocess,
* a scoped cancel callback composed from the parts run_command will wire
  together (context + ``_kill_process_tree``) terminates the whole group
  exactly once.

The cancellation-before-spawn case is written against the TARGET contract and
currently FAILS against the still-polling/legacy-PID-field implementation; that
failure is a production gap, not a defect in this spec. The rest must pass.
"""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.unit


# ── Process helpers ────────────────────────────────────────────────────
def _proc_state(pid: int) -> str:
    try:
        with open(f'/proc/{pid}/stat', encoding='ascii') as handle:
            data = handle.read()
        return data.split(') ', 1)[1].split()[0]
    except (OSError, IndexError):
        return ''


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pid_not_running(pid: int, timeout: float = 5.0) -> bool:
    """True once the pid is gone or has become a zombie (terminated)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        if _proc_state(pid) == 'Z':
            return True
        time.sleep(0.02)
    return False


def _wait_for_file(path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f'marker file not written within {timeout}s: {path}')


def _spawn_parent_and_grandchild():
    """Spawn a session-leading parent that spawns a 300s ``sleep`` grandchild.

    Returns ``(proc, child_pid)``; the parent reports the grandchild pid on
    stdout before sleeping itself.
    """
    code = (
        "import subprocess, time\n"
        "child = subprocess.Popen(['sleep', '300'])\n"
        "print('CHILD %d' % child.pid, flush=True)\n"
        "time.sleep(300)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, '-c', code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child_pid = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and child_pid is None:
        line = proc.stdout.readline()
        if not line:
            break
        if line.startswith('CHILD '):
            child_pid = int(line.split()[1])
    if child_pid is None:
        proc.kill()
        proc.wait()
        raise AssertionError('grandchild pid was not reported')
    return proc, child_pid


def _cleanup_group(proc, child_pid):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait()
        except OSError:
            pass
    elif child_pid is not None and _pid_exists(child_pid) and _proc_state(child_pid) != 'Z':
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass


# ── Process-group / grandchild termination and reap ────────────────────
def test_kill_process_tree_terminates_grandchild_and_reaps():
    from lib.project_mod.run_command import _kill_process_tree

    proc, child_pid = _spawn_parent_and_grandchild()
    try:
        _kill_process_tree(proc)
        assert proc.returncode is not None, (
            'direct child must be reaped by _kill_process_tree')
        assert _pid_not_running(child_pid), (
            f'grandchild {child_pid} still running after group kill')
    finally:
        _cleanup_group(proc, child_pid)


def test_run_command_abort_kills_grandchild_and_clears_pid_fields(tmp_path):
    from lib.project_mod.run_command import tool_run_command

    marker = tmp_path / 'grandchild.pid'
    # The background `sleep` stays in the shell's process group because this is
    # a non-interactive sh (no job control), so a group kill reaches both.
    command = f"sleep 300 & echo $! > {shlex.quote(str(marker))}; wait"
    task = {'aborted': False}
    grandchild_pid: list[int] = []

    def _abort():
        _wait_for_file(marker)
        grandchild_pid.append(int(marker.read_text().strip()))
        task['aborted'] = True

    thread = threading.Thread(target=_abort, daemon=True)
    thread.start()
    try:
        out = tool_run_command(str(tmp_path), command, task=task)
    finally:
        thread.join(timeout=5)

    assert '[Command aborted by user]' in out
    assert '_subprocess_pid' not in task, 'legacy PID field must be cleaned up'
    assert '_subprocess_pgid' not in task, 'legacy PGID field must be cleaned up'
    assert grandchild_pid, 'abort thread never observed the grandchild pid'
    assert _pid_not_running(grandchild_pid[0]), (
        f'grandchild {grandchild_pid[0]} still running after abort')


def test_context_cancel_callback_composition_kills_process_group():
    """The accepted plan's seam, composed from parts that already exist:
    run_command will register a cancel callback that calls
    ``_kill_process_tree``; ``cancel_task_contexts`` must invoke it exactly
    once and terminate the whole group (direct child + grandchild) and reap
    the direct child."""
    from lib.project_mod.run_command import _kill_process_tree
    from lib.tasks_pkg.tool_runtime import (
        ToolExecutionContext,
        cancel_task_contexts,
    )

    proc, child_pid = _spawn_parent_and_grandchild()
    try:
        task = {'id': 'composition', 'aborted': False}
        from lib.tasks_pkg.tool_runtime import context_for_task
        task['_userId'] = 1
        ctx = context_for_task(
            task,
            round_num=1,
            tool_call_id='c',
            tool_name='run_command',
            round_entry={},
        )
        killed: list[int] = []

        def kill():
            killed.append(1)
            _kill_process_tree(proc)

        ctx.register_cancel_callback(kill)
        # Mirror cancel_task: signal first, then fan out to scoped contexts.
        task['aborted'] = True
        ctx.abort_event.set()

        count, errors = cancel_task_contexts(task)
        assert count == 1
        assert errors == ()
        assert killed == [1]
        assert proc.returncode is not None
        assert _pid_not_running(child_pid)
    finally:
        _cleanup_group(proc, child_pid)


# ── Cancellation before spawn (accepted-plan contract) ────────────────
def test_run_command_cancel_before_spawn_does_not_spawn(tmp_path, monkeypatch):
    """A task already aborted must not spawn a subprocess at all.

    This documents the production gap: the current runner still Popen()s and
    only kills on the first poll tick (~0.2s) instead of short-circuiting
    before spawn. The target behavior is a no-spawn aborted result.
    """
    import lib.project_mod.run_command as rc

    calls: list[tuple] = []

    def boom(*args, **kwargs):
        calls.append(args)
        raise AssertionError('run_command must not spawn after cancellation')

    monkeypatch.setattr(rc.subprocess, 'Popen', boom)
    out = rc.tool_run_command(str(tmp_path), 'echo ok', task={'aborted': True})

    assert calls == [], (
        f'Popen was called despite pre-existing cancellation: {out!r}')
    assert '[Command aborted by user]' in out

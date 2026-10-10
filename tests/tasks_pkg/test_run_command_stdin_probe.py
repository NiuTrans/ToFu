"""A stuck procfs sensor cannot stop command control or publish late input."""
import builtins
import os
import shlex
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('reason', ['stop', 'timeout', 'interrupt', 'finish'])
def test_blocked_syscall_read_does_not_block_command_control(tmp_path, monkeypatch, reason):
    from lib.project_mod import run_command as rc
    from lib.compat import HAS_PROCFS
    if not HAS_PROCFS:
        pytest.skip('Linux procfs boundary')
    from lib.project_mod import stdin_probe
    pool = stdin_probe.ProbePool(1)
    monkeypatch.setattr(stdin_probe, '_default_pool', lambda: pool)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    task = {'aborted': False}
    result = {}
    class BlockedSyscall:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self):
            entered.set()
            assert release.wait(15), 'test must release its own sensor'
            return '0 0x0 0x0 0x0 0x0 0x0 0x0'
    def proc_open(path, *args, **kwargs):
        if str(path).startswith('/proc/') and str(path).endswith('/syscall'):
            return BlockedSyscall()
        return builtins.open(path, *args, **kwargs)
    monkeypatch.setattr(rc, 'open', proc_open, raising=False)
    callbacks = []
    chunks = []
    def run():
        try:
            result['text'] = rc.tool_run_command(
                str(tmp_path),
                'printf LIVE1; sleep 0.7; printf LIVE2' if reason == 'finish' else 'sleep 30',
                timeout=1 if reason == 'timeout' else None,
                task=task, stdin_callback=lambda hint: callbacks.append(hint),
                on_chunk=lambda stream, text: chunks.append((stream, text)))
        finally:
            finished.set()
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(5)
        pid = task['_subprocess_pid']
        if reason == 'stop':
            task['aborted'] = True
        elif reason == 'interrupt':
            task['_cmd_interrupt'] = {'pid': pid, 'source': 'user', 'ts': time.time(), 'note': ''}
        assert finished.wait(3), 'procfs read prevented command control from returning'
        assert not release.is_set(), 'control must finish while the sensor is still blocked'
        assert not callbacks
        assert '_subprocess_pid' not in task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        expected = {'stop': 'Command aborted', 'timeout': 'timed out',
                    'interrupt': 'Command interrupted', 'finish': '[exit code: 0]'}[reason]
        assert expected.lower() in result['text'].lower()
        if reason == 'finish':
            live_output = ''.join(text for stream, text in chunks if stream == 'stdout')
            assert live_output == 'LIVE1LIVE2'
    finally:
        release.set()
        worker.join(7)
        assert not worker.is_alive()
        # The sensor owns no task callback; after release its late answer
        # must remain fenced even though it says that stdin was being read.
        deadline = time.monotonic() + 2
        while pool.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.active == 0, 'test must retire its physical sensor'
        assert not callbacks


def test_real_interactive_input_still_reaches_owned_child(tmp_path):
    from lib.project_mod.run_command import tool_run_command
    from lib.compat import HAS_PROCFS
    if not HAS_PROCFS:
        pytest.skip('Linux procfs boundary')
    replies = []
    def answer(hint):
        replies.append(hint)
        return 'VALUE7'
    command = shlex.quote(sys.executable) + ' -c ' + shlex.quote("print('READ=' + input(), flush=True)")
    text = tool_run_command(str(tmp_path), command, timeout=5, stdin_callback=answer)
    assert 'READ=VALUE7' in text
    assert len(replies) == 1


def test_closed_blocked_sensor_keeps_capacity_and_fences_late_result():
    from lib.project_mod.stdin_probe import ProbePool, StdinProbe
    pool = ProbePool(1)
    entered, release = threading.Event(), threading.Event()
    admitted = []
    def blocked(pid, inode, *, stop_event):
        entered.set()
        assert release.wait(5)
        return (pid, 'late-input')
    first = StdinProbe(blocked, 1, 2, pool=pool)
    later = [StdinProbe(lambda *a, **k: admitted.append(True), 3, 4, pool=pool)
             for _ in range(8)]
    try:
        assert first.poll() is None and entered.wait(2)
        first.close()
        for probe in later:
            assert probe.poll() is None
            probe.close()
        assert pool.active == 1
        assert not admitted
        release.set()
        deadline = time.monotonic() + 2
        while pool.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.active == 0
        assert first.poll() is None
        assert all(probe.poll() is None for probe in later)
        assert not admitted
    finally:
        release.set()
        first.close()
        for probe in later: probe.close()
        deadline = time.monotonic() + 2
        while pool.active and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pool.active == 0


def test_noninteractive_stdin_reader_still_receives_eof(tmp_path):
    from lib.project_mod.run_command import tool_run_command
    calls = []
    out = tool_run_command(str(tmp_path), 'sort', timeout=5,
                           stdin_callback=lambda hint: calls.append(hint))
    assert '[exit code: 0]' in out
    assert not calls


@pytest.mark.parametrize('boundary', ['construction', 'launch'])
def test_optional_sensor_failure_preserves_command_output_and_cleanup(tmp_path, monkeypatch, boundary):
    from lib.project_mod import stdin_probe
    from lib.project_mod.run_command import tool_run_command
    def unavailable(*args, **kwargs):
        raise RuntimeError('owned sensor admission unavailable')
    if boundary == 'construction':
        monkeypatch.setattr(stdin_probe.StdinProbe, '__init__', unavailable)
    else:
        monkeypatch.setattr(stdin_probe.ProbePool, 'launch', unavailable)
    task = {'aborted': False}
    out = tool_run_command(str(tmp_path), 'echo retained-output && sleep 0.3', timeout=3,
                           task=task, stdin_callback=lambda hint: None)
    assert 'retained-output' in out and '[exit code: 0]' in out
    assert '_subprocess_pid' not in task

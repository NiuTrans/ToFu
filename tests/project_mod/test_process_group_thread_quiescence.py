"""An exited leader with live native threads cannot prove group quiescence."""
import os
import platform
import signal
import subprocess
import sys
import time

import pytest

from lib.project_mod.process_group import (
    group_has_live_members, read_process_fields, retire_process_group,
)

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not sys.platform.startswith('linux') or platform.machine() not in ('x86_64', 'AMD64'),
    reason='Linux x86-64 thread-exit syscall fixture')]


@pytest.fixture
def thread_only_descendant(tmp_path):
    trace = tmp_path / 'descendant.pid'
    child = (
        'import ctypes,os,signal,threading,time\n'
        'signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
        'threading.Thread(target=lambda:time.sleep(30),daemon=True).start()\n'
        'ctypes.CDLL(None).syscall(60,0)\n'  # SYS_exit retires only this thread.
    )
    launcher = (
        'import subprocess,sys\nfrom pathlib import Path\n'
        'child=subprocess.Popen([sys.executable,"-c",sys.argv[1]])\n'
        'Path(sys.argv[2]).write_text(str(child.pid))\n'
    )
    process = subprocess.Popen([sys.executable, '-c', launcher, child, str(trace)],
                               start_new_session=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    pid = None
    try:
        process.wait(timeout=5)
        pid = int(trace.read_text())
        deadline = time.monotonic() + 5
        while True:
            fields = read_process_fields(pid)
            if fields[0] == b'Z' and int(fields[17]) > 1:
                break
            assert time.monotonic() < deadline, 'fixture did not retain a live thread'
            time.sleep(.01)
        yield process, pid
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        if pid is not None:
            deadline = time.monotonic() + 5
            while True:
                try:
                    fields = read_process_fields(pid)
                except FileNotFoundError:
                    break
                if fields[0] in (b'Z', b'X') and int(fields[17]) == 1:
                    break
                assert time.monotonic() < deadline, 'fixture descendant did not retire'
                time.sleep(.01)


def test_zombie_leader_with_live_threads_remains_a_live_group(thread_only_descendant):
    process, _pid = thread_only_descendant
    assert group_has_live_members(process.pid)


def test_empty_group_is_settled_without_a_proc_scan(monkeypatch):
    """Completion checks every command's group; an empty one costs one signal probe."""
    from lib.project_mod import process_group
    process = subprocess.Popen(['true'], start_new_session=True)
    process.wait(timeout=5)
    scans = []
    real_path = process_group.Path
    monkeypatch.setattr(process_group, 'Path',
                        lambda *parts: scans.append(parts) or real_path(*parts))
    assert group_has_live_members(process.pid) is False
    assert scans == []
    live = subprocess.Popen(['sleep', '30'], start_new_session=True)
    try:
        assert group_has_live_members(live.pid) is True
        assert scans  # A populated group is still observed member by member.
    finally:
        live.kill()
        live.wait(timeout=5)


def test_retirement_kills_threads_even_after_direct_leader_is_reaped(thread_only_descendant):
    process, pid = thread_only_descendant
    retire_process_group(process.pid, wait_leader=process.wait,
                         grace_seconds=.02, kill_seconds=2)
    try:
        fields = read_process_fields(pid)
    except FileNotFoundError:
        return
    assert fields[0] in (b'Z', b'X') and int(fields[17]) == 1

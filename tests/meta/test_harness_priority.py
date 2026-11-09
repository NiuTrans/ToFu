"""Pytest processes yield CPU and I/O to the live server and its TofuDB.

Owner decision 2026-11-08 (docs/TESTING_STRATEGY.md, "Test processes yield to
the live server"): importing ``tests/conftest.py`` first moves every thread of
each pytest controller and xdist worker to nice 19 and the idle I/O class
(``tests/support/process_priority.py``) before workers, engines or browsers
start; they inherit both. It addresses only the calling process's own
threads, so a pytest launched from the live server never lowers the server, its
execution host or production TofuDB.

An unprivileged process can never lower a nice value again, so every process
this suite starts already runs at nice 19 and only the I/O class can show what
changed: the specs start their subjects at best-effort level 4, which needs no
privilege, and record the CPU calls to prove which threads they address.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import xml.etree.ElementTree as ElementTree

import psutil
import pytest

from tests.meta.test_configured_slot_switch_isolation import (
    _standalone_session_environment,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not sys.platform.startswith('linux'),
                       reason='per-thread CPU and I/O priority is Linux behavior'),
]

_ROOT = Path(__file__).resolve().parents[2]
_IN_SESSION_CASE = ('tests/meta/test_harness_priority.py::'
                    'test_this_pytest_process_runs_every_thread_at_the_lowest_priority')

_SUBJECT = r'''
import json, os, subprocess, sys, threading
import psutil
from tests.support import process_priority

def priority(tid):
    return [os.getpriority(os.PRIO_PROCESS, tid), int(psutil.Process(tid).ionice().ioclass)]

waiting = [sys.executable, '-c', 'import sys; sys.stdin.read()']
release = threading.Event()
earlier_thread = threading.Thread(target=release.wait)
earlier_thread.start()
earlier_child = subprocess.Popen(waiting, stdin=subprocess.PIPE)
calls = []
setpriority = os.setpriority
def recording_setpriority(which, who, value):
    calls.append([which, who, value])
    setpriority(which, who, value)
os.setpriority = recording_setpriority
process_priority.lower_own_priority()
os.setpriority = setpriority
own_threads = sorted(int(tid) for tid in os.listdir('/proc/self/task'))
later_thread = threading.Thread(target=release.wait)
later_thread.start()
later_child = subprocess.Popen(waiting, stdin=subprocess.PIPE)
report = {
    'own_threads': own_threads,
    'threads': [priority(tid) for tid in [*own_threads, later_thread.native_id]],
    'earlier_child': priority(earlier_child.pid),
    'later_child': priority(later_child.pid),
    'calls': calls,
}
release.set()
for child in (earlier_child, later_child):
    child.communicate(b'')
print(json.dumps(report))
'''


def _lowest() -> list[int]:
    return [19, int(psutil.IOPRIO_CLASS_IDLE)]


def _priority(tid: int) -> list[int]:
    """Nice value and I/O class of one thread; a process ID names its main thread."""
    return [os.getpriority(os.PRIO_PROCESS, tid), int(psutil.Process(tid).ionice().ioclass)]


def _thread_priorities(pid: int) -> dict[int, list[int]]:
    priorities = {}
    for tid in map(int, os.listdir(f'/proc/{pid}/task')):
        with contextlib.suppress(ProcessLookupError, psutil.NoSuchProcess):
            priorities[tid] = _priority(tid)
    return priorities


@contextlib.contextmanager
def _this_thread_at_best_effort_level_4():
    """Children this thread starts begin above the idle class; no privilege needed."""
    thread = psutil.Process(threading.get_native_id())
    previous = thread.ionice()
    thread.ionice(psutil.IOPRIO_CLASS_BE, 4)
    try:
        yield thread.pid
    finally:
        thread.ionice(previous.ioclass, previous.value
                      if previous.ioclass == psutil.IOPRIO_CLASS_BE else None)


def test_this_pytest_process_runs_every_thread_at_the_lowest_priority():
    threads = _thread_priorities(os.getpid())
    assert threads and all(value == _lowest() for value in threads.values()), threads
    if os.environ.get('PYTEST_XDIST_WORKER'):
        # The controller started this worker after lowering itself.
        controller = _thread_priorities(os.getppid())
        assert all(value == _lowest() for value in controller.values()), controller


def test_lowering_reaches_every_own_thread_and_later_children_only():
    with _this_thread_at_best_effort_level_4() as launcher:
        completed = subprocess.run(
            [sys.executable, '-c', _SUBJECT], cwd=_ROOT,
            capture_output=True, text=True, timeout=120)
        launcher_after = _priority(launcher)

    assert completed.returncode == 0, completed.stderr[-4000:]
    report = json.loads(completed.stdout)
    best_effort = [19, int(psutil.IOPRIO_CLASS_BE)]
    # A thread that existed before the call, one created after it and a child
    # started after it all run at the lowest priority.
    assert report['threads'] == [_lowest()] * len(report['threads'])
    assert report['later_child'] == _lowest()
    # Neither the process that launched it nor a child it started earlier changed.
    assert launcher_after == best_effort
    assert report['earlier_child'] == best_effort
    # CPU priority addresses each own thread, never a process group or user.
    assert sorted(report['calls']) == sorted(
        [os.PRIO_PROCESS, tid, 19] for tid in report['own_threads'])


def test_a_pytest_session_lowers_its_controller_and_workers(tmp_path):
    report_path = tmp_path / 'session.xml'
    with _this_thread_at_best_effort_level_4():
        completed = subprocess.run(
            [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', '-n', '1',
             '-q', '--timeout=240', f'--junitxml={report_path}', _IN_SESSION_CASE],
            cwd=_ROOT, env=_standalone_session_environment(),
            capture_output=True, text=True, timeout=280)

    output = completed.stdout[-6000:] + completed.stderr[-2000:]
    assert completed.returncode == 0, output
    cases = list(ElementTree.parse(report_path).getroot().iter('testcase'))
    assert len(cases) == 1, output
    assert [child.tag for child in cases[0]
            if child.tag in {'failure', 'error', 'skipped'}] == [], output

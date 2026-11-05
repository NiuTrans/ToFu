"""A Supervisor re-exec inherits children that nothing else will ever reap.

``os.exec*`` keeps the PID and every child but discards the previous image's
Popen objects. These specs replay the ``/reload`` handoff with a real exec into
``supervisor.py`` and check exactly which inherited children get reaped.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

import server_manager

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not sys.platform.startswith('linux'), reason='Linux /proc child lists')]

ROOT = Path(__file__).resolve().parents[2]

# The image before the exec: an exited orphan, an exited worker generation the
# persisted manager state still records, and a live child.
_PREVIOUS_IMAGE = r'''
import json, os, sys, time
from pathlib import Path
supervisor, state_path, children_path, state_version, port = sys.argv[1:6]

def stat_fields(pid):
    raw = Path(f'/proc/{pid}/stat').read_bytes()
    return raw[raw.rfind(b')') + 2:].split()

def child(code):
    pid = os.fork()
    if pid == 0:
        try:
            if code is None:
                time.sleep(60)
        finally:
            os._exit(code or 0)
    return pid

orphan, worker, live = child(3), child(5), child(None)
for pid in (orphan, worker):
    while stat_fields(pid)[0] != b'Z':
        time.sleep(0.01)
Path(state_path).write_text(json.dumps({
    'version': int(state_version), 'desired': 'running', 'observed': 'running',
    'port': int(port), 'serverEnv': {'PORT': port},
    'worker': {'pid': worker, 'processStartTime': int(stat_fields(worker)[19]),
               'spawnedAt': time.time()},
}))
Path(children_path).write_text(
    json.dumps({'orphan': orphan, 'worker': worker, 'live': live}))
os.execv(sys.executable, [sys.executable, supervisor])
'''


def _state(pid):
    try:
        raw = Path(f'/proc/{pid}/stat').read_bytes()
    except FileNotFoundError:
        return None
    return raw[raw.rfind(b')') + 2:].split()[0].decode()


def _wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _recorded_exit(state_path):
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        return {}
    return state.get('lastWorkerProcessExit') or {}


def test_reexec_reaps_inherited_children_after_settling_the_worker_exit(tmp_path):
    project = tmp_path / 'project'
    (project / 'data').mkdir(parents=True)
    (project / 'logs').mkdir()
    (project / 'server.py').write_text('raise SystemExit(0)\n')
    (project / 'stop.sh').write_text('#!/bin/sh\nexit 0\n')
    state_path = project / 'data' / 'server-manager-state.json'
    children_path = tmp_path / 'children.json'
    log_path = tmp_path / 'supervisor.log'
    environment = {
        key: value for key, value in os.environ.items()
        if key not in ('PORT', '_TOFU_RUNTIME_PORT')}
    environment.update({
        'TOFU_SUPERVISOR_PROJECTS': str(project),
        'TOFU_SUPERVISOR_HOST': '127.0.0.1',
        'TOFU_SUPERVISOR_PORT': '0',
        'TOFU_SUPERVISOR_OWNER_PID': str(os.getpid()),
        'TOFU_MANAGER_INTERVAL': '2',
        'TOFU_DATA_DIR': str(project),
    })
    # Bound but not listening: no other test takes the worker port, and the
    # manager sees no listener conflict on it.
    with socket.socket() as reserved, log_path.open('wb') as log:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
        supervisor = subprocess.Popen(
            [sys.executable, '-c', _PREVIOUS_IMAGE, str(ROOT / 'supervisor.py'),
             str(state_path), str(children_path),
             str(server_manager.STATE_VERSION), str(port)],
            cwd=project, env=environment,
            stdout=subprocess.DEVNULL, stderr=log)
        children = {}

        def log_tail():
            return log_path.read_text(errors='replace')[-3000:]

        try:
            assert _wait_for(children_path.exists), log_tail()
            children = json.loads(children_path.read_text())

            assert _wait_for(lambda: _state(children['orphan']) is None), (
                'the new image never reaped an exited orphan of the old one\n'
                + log_tail())
            assert _state(children['live']) not in (None, 'Z')

            # The recorded worker keeps its zombie until its exit is observed.
            assert _wait_for(lambda: _recorded_exit(state_path)), log_tail()
            evidence = _recorded_exit(state_path)
            assert evidence['pid'] == children['worker']
            assert evidence['exitCode'] == 5
            assert _wait_for(lambda: _state(children['worker']) is None), log_tail()

            # An inherited child that exits after the handoff is reaped too.
            os.kill(children['live'], signal.SIGKILL)
            assert _wait_for(lambda: _state(children['live']) is None), log_tail()
        finally:
            live = children.get('live')
            if live and _state(live) not in (None, 'Z'):
                os.kill(live, signal.SIGKILL)
            supervisor.send_signal(signal.SIGTERM)
            try:
                supervisor.wait(timeout=15)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=5)


# Two managers in one re-exec'd process: the first one's tick must not reap the
# exited worker that the second one has yet to observe.
_TWO_MANAGERS_BEFORE = r'''
import json, os, sys, time
from pathlib import Path
root, first, second, state_version, port, result = sys.argv[1:7]

def stat_fields(pid):
    raw = Path(f'/proc/{pid}/stat').read_bytes()
    return raw[raw.rfind(b')') + 2:].split()

def child(code):
    pid = os.fork()
    if pid == 0:
        os._exit(code)
    return pid

orphan, pending = child(3), child(4)
for pid in (orphan, pending):
    while stat_fields(pid)[0] != b'Z':
        time.sleep(0.01)
for project, desired, worker in (
        (first, 'stopped', {}),
        (second, 'running', {'pid': pending, 'spawnedAt': time.time(),
                             'processStartTime': int(stat_fields(pending)[19])})):
    Path(project, 'data', 'server-manager-state.json').write_text(json.dumps({
        'version': int(state_version), 'desired': desired, 'port': int(port),
        'serverEnv': {'PORT': port, 'TOFU_DATA_DIR': project}, 'worker': worker}))
os.execv(sys.executable, [sys.executable, '-c', sys.argv[7], root, first, second,
                          result, str(orphan), str(pending)])
'''

_TWO_MANAGERS_AFTER = r'''
import json, sys
from pathlib import Path
root, first, second, result, orphan, pending = sys.argv[1:7]
sys.path.insert(0, root)
import server_manager

def state(pid):
    try:
        raw = Path(f'/proc/{pid}/stat').read_bytes()
    except FileNotFoundError:
        return None
    return raw[raw.rfind(b')') + 2:].split()[0].decode()

managers = [server_manager.LifecycleManager(project) for project in (first, second)]
managers[0].reconcile()
after_first = {'orphan': state(orphan), 'pending': state(pending)}
managers[1].reconcile()
Path(result).write_text(json.dumps({
    'afterFirst': after_first, 'afterSecond': state(pending),
    'evidence': managers[1].status()['lastWorkerProcessExit']}))
'''


def test_one_managers_tick_leaves_another_managers_exited_worker_pending(tmp_path):
    projects = []
    for name in ('first', 'second'):
        project = tmp_path / name
        (project / 'data').mkdir(parents=True)
        (project / 'logs').mkdir()
        (project / 'server.py').write_text('raise SystemExit(0)\n')
        projects.append(str(project))
    result_path = tmp_path / 'result.json'
    environment = {
        key: value for key, value in os.environ.items()
        if key not in ('PORT', '_TOFU_RUNTIME_PORT')}
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
        completed = subprocess.run(
            [sys.executable, '-c', _TWO_MANAGERS_BEFORE, str(ROOT), *projects,
             str(server_manager.STATE_VERSION), str(port), str(result_path),
             _TWO_MANAGERS_AFTER],
            cwd=tmp_path, env=environment, capture_output=True, text=True,
            timeout=120)
    assert completed.returncode == 0, completed.stderr[-3000:]
    result = json.loads(result_path.read_text())
    assert result['afterFirst'] == {'orphan': None, 'pending': 'Z'}
    assert result['evidence']['exitCode'] == 4
    assert result['afterSecond'] is None


def _forked(code=None):
    pid = os.fork()
    if pid == 0:
        try:
            if code is None:
                time.sleep(30)
        finally:
            os._exit(code or 0)
    return pid


def _settle(pid):
    if _state(pid) not in (None, 'Z'):
        os.kill(pid, signal.SIGKILL)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


def test_inherited_reaping_spares_live_pending_reused_and_foreign_pids():
    orphan, pending, reused, live = _forked(3), _forked(4), _forked(6), _forked()
    # This zombie is a grandchild: only its own parent may reap it.
    parent = subprocess.Popen(
        ['sh', '-c', '(exit 0) & echo $!; exec sleep 30'],
        stdout=subprocess.PIPE, text=True)
    owned = None
    try:
        grandchild = int(parent.stdout.readline())
        assert _wait_for(lambda: all(
            _state(pid) == 'Z' for pid in (orphan, pending, reused, grandchild)))
        identities = dict(server_manager.child_process_identities())
        inherited = [(pid, identities[pid]) for pid in (orphan, pending, live)]
        # Captured with another start time: the PID now names a new process.
        stale = (reused, identities[reused] + 1)
        foreign = (grandchild, server_manager.proc_start_ticks(grandchild))
        # Started after the capture: its Popen owns that exit status.
        owned = subprocess.Popen([sys.executable, '-c', 'raise SystemExit(9)'])
        assert _wait_for(lambda: _state(owned.pid) == 'Z')

        remaining = server_manager.reap_inherited_children(
            [*inherited, stale, foreign], pending_worker_pids={pending})

        assert _state(orphan) is None
        assert _state(pending) == 'Z', 'a pending worker awaits its observation'
        assert _state(reused) == 'Z'
        assert _state(live) not in (None, 'Z')
        assert _state(grandchild) == 'Z'
        assert sorted(remaining) == sorted(inherited[1:])
        assert owned.wait(timeout=5) == 9
    finally:
        for pid in (orphan, pending, reused, live):
            _settle(pid)
        parent.kill()
        parent.wait(timeout=5)
        parent.stdout.close()
        if owned is not None and owned.poll() is None:
            owned.kill()
            owned.wait(timeout=5)

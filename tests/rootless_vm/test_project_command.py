"""Strict project launch shares the existing command lifecycle and teardown."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from rootless_vm.project_command import ProjectCommandSpec, run_project_command
from rootless_vm.qemu_launcher import _sanitized_environment
from rootless_vm import pidfd
from rootless_vm import pdeath_exec, qemu_launcher

pytestmark = pytest.mark.unit


@pytest.fixture
def jailed_shell(host_tmp_path):
    jail = host_tmp_path / 'jail'
    jail.mkdir(mode=0o700)
    sources = [Path(shutil.which(name)).resolve() for name in ('bash', 'setsid', 'sleep')]
    dependencies = subprocess.run(['ldd', *map(str, sources)], capture_output=True,
                                  text=True, timeout=10, check=True,
                                  env=_sanitized_environment())
    for line in dependencies.stdout.splitlines():
        fields = line.split()
        candidate = (fields[2] if len(fields) >= 3 and fields[1] == '=>'
                     else fields[0] if fields else '')
        if candidate.startswith('/') and Path(candidate).is_file():
            sources.append(Path(candidate))
    pins = {}
    for source in sources:
        target = jail / str(source).lstrip('/')
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o555)
        pins[str(source)] = hashlib.sha256(target.read_bytes()).hexdigest()
    (jail / 'tmp').mkdir()
    (jail / 'experiment').mkdir()
    spec = ProjectCommandSpec(
        jail=jail, argv=(str(sources[0]), '-c', 'echo completed'),
        toolchain=tuple(pins.items()), deadline_epoch=time.time() + 20,
        timeout_seconds=5, memory_bytes=256 * 1024**2, file_bytes=1024**2,
        file_descriptors=64, cpu_seconds=5)
    return spec


def test_expired_original_deadline_cannot_start(jailed_shell):
    with pytest.raises(TimeoutError, match='original command deadline'):
        run_project_command(replace(jailed_shell, deadline_epoch=time.time() - 1))


def test_changed_pinned_runtime_refuses_command(jailed_shell):
    executable = jailed_shell.jail / jailed_shell.argv[0].lstrip('/')
    executable.chmod(0o700)
    executable.write_bytes(b'changed')
    with pytest.raises(ValueError, match='digest changed'):
        run_project_command(jailed_shell)


def test_symlinked_runtime_refuses_command(jailed_shell):
    executable = jailed_shell.jail / jailed_shell.argv[0].lstrip('/')
    executable.unlink()
    executable.symlink_to(sys.executable)
    with pytest.raises(OSError):
        run_project_command(jailed_shell)


def test_unavailable_namespace_has_no_host_fallback(jailed_shell, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda _name: None)
    with pytest.raises(RuntimeError, match='supervision is unavailable'):
        run_project_command(jailed_shell)
    assert not (jailed_shell.jail / 'host_fallback_was_run').exists()


@pytest.mark.parametrize('field,value', [
    ('timeout_seconds', float('inf')), ('deadline_epoch', float('nan')),
    ('memory_bytes', 0), ('file_descriptors', True), ('argv', ('../bin/bash',)),
])
def test_invalid_scope_never_launches(jailed_shell, field, value):
    with pytest.raises((TypeError, ValueError)):
        run_project_command(replace(jailed_shell, **{field: value}))


def test_cancel_before_spawn(jailed_shell):
    result = run_project_command(jailed_shell, task={'aborted': True})
    assert 'aborted' in result.lower()


def test_pidfd_cannot_signal_a_reused_numeric_identity():
    child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
    descriptor = None
    try:
        descriptor = pidfd.open_process(child.pid)
        child.terminate()
        child.wait(timeout=3)
        with pytest.raises(OSError) as failure:
            pidfd.send_signal(descriptor, signal.SIGTERM)
        assert failure.value.errno == errno.ESRCH
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)
        if descriptor is not None:
            os.close(descriptor)


def test_unknown_pidfd_abi_refuses_without_pid_signal_fallback(monkeypatch):
    monkeypatch.delattr(os, 'pidfd_open', raising=False)
    monkeypatch.delattr(signal, 'pidfd_send_signal', raising=False)
    monkeypatch.setattr(pidfd.platform, 'machine', lambda: 'unrecognized')
    with pytest.raises(OSError) as failure:
        pidfd.require_process_handles()
    assert failure.value.errno == errno.ENOSYS


@pytest.mark.parametrize('identity', [(200, 1, 10), (100, 1, 9)])
def test_namespace_bootstrap_rejects_foreign_or_recycled_supervisor(monkeypatch, identity):
    monkeypatch.setattr(qemu_launcher.os, 'getppid', lambda: 0)
    monkeypatch.setattr(qemu_launcher, '_host_parent', lambda _fd: 100)
    monkeypatch.setattr(qemu_launcher, '_host_parent_identity', lambda _fd, _pid: (1, 10))
    with pytest.raises(RuntimeError, match='foreign supervisor'):
        qemu_launcher.arm_parent_death_signal(
            0, death_signal=signal.SIGKILL, descriptor_directory=3,
            supervisor_identity=identity)


def test_supervisor_bootstrap_rejects_adoption_before_first_instruction(monkeypatch):
    monkeypatch.setattr(pdeath_exec.os, 'getppid', lambda: 200)
    monkeypatch.setattr(pdeath_exec.os, 'execve', lambda *_a: pytest.fail('must not exec'))
    assert pdeath_exec.main(['--project-supervisor', '100', '/usr/bin/unshare']) == 143


def _children(pid):
    try:
        return [int(value) for value in Path(
            f'/proc/{pid}/task/{pid}/children').read_text().split()]
    except FileNotFoundError:
        return []


def _read_spec(payload):
    raw = json.loads(Path(payload).read_text())
    raw['jail'] = Path(raw['jail'])
    raw['argv'] = tuple(raw['argv'])
    raw['toolchain'] = tuple(tuple(pin) for pin in raw['toolchain'])
    return ProjectCommandSpec(**raw)


def _supervise_case(case, payload):
    """An expendable subreaper owns all children, including crash orphans."""
    if case == 'owner_death':
        return _supervise_owner_death(payload)
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    spec = _read_spec(payload)
    command = "trap '' TERM; /usr/bin/setsid /usr/bin/sleep 30 & echo ready; wait"
    if case == 'exit':
        command = "/usr/bin/setsid /usr/bin/sleep 30 & echo ready; exit 0"
    elif case == 'write':
        command = "printf project_result > /experiment/result.txt; echo ready"
    elif case == 'file_limit':
        command = "echo ready; printf '%1048577s' x > /experiment/bounded-output"
    spec = replace(spec, argv=(spec.argv[0], '-c', command),
                   timeout_seconds=0.8 if case == 'timeout' else 5)
    task = {'aborted': False}
    owned = []
    handles = {}
    ready = False

    class Context:
        cancellation_requested = False
        callback = None

        def register_cancel_callback(self, callback):
            self.callback = callback
            return lambda: setattr(self, 'callback', None)

    context = Context()

    def on_chunk(_stream, text):
        nonlocal ready
        if 'ready' not in text or ready:
            return
        ready = True
        frontier = _children(task['_subprocess_pid'])
        while frontier:
            pid = frontier.pop()
            owned.append(pid)
            handles[pid] = pidfd.open_process(pid)
            frontier.extend(_children(pid))
        if case == 'cancel':
            task['aborted'] = True
        elif case == 'context':
            context.cancellation_requested = True
            context.callback()
        elif case == 'interrupt':
            task['_cmd_interrupt'] = {'source': 'user'}
        elif case == 'parent_death':
            os.kill(task['_subprocess_pid'], signal.SIGKILL)

    result = ''
    reaped = []
    try:
        result = run_project_command(spec, task=task, runtime_context=context,
                                     on_chunk=on_chunk)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid:
                reaped.append(pid)
            else:
                time.sleep(0.02)

        remaining = [pid for pid in owned if Path(f'/proc/{pid}').exists()]
        assert ready, result
        if case not in ('exit', 'write', 'file_limit'):
            assert len(owned) >= 2, 'test did not create the detached namespace child'
        assert not remaining, f'unreaped namespace processes: {remaining}'
        assert '_subprocess_pid' not in task and '_subprocess_pgid' not in task
        assert context.callback is None
        if case == 'cancel' or case == 'context':
            assert 'aborted' in result.lower(), result
        elif case == 'interrupt':
            assert 'interrupted' in result.lower(), result
            assert not task['aborted']
        elif case == 'timeout':
            assert 'timed out' in result.lower(), result
        elif case in ('exit', 'write'):
            assert '[exit code: 0]' in result, result
            if case == 'write':
                assert (spec.jail / 'experiment/result.txt').read_text() == 'project_result'
        elif case == 'file_limit':
            size = (spec.jail / 'experiment/bounded-output').stat().st_size
            assert 0 < size <= spec.file_bytes
            assert '[exit code: 0]' not in result
        print(json.dumps({'case': case, 'owned': owned, 'reaped_orphans': reaped,
                          'all_physically_reaped': True}))
    finally:
        # Only this disposable supervisor's children can reach this cleanup.
        for descriptor in handles.values():
            try:
                pidfd.send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(descriptor)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if not pid:
                time.sleep(0.02)

def _controller(payload):
    task = {}
    spec = _read_spec(payload)
    spec = replace(spec, argv=(spec.argv[0], '-c',
                   "trap '' TERM; /usr/bin/setsid /usr/bin/sleep 30 & echo ready; wait"))

    def notify(_stream, text):
        if 'ready' in text:
            owned = [task['_subprocess_pid']]
            frontier = _children(owned[0])
            while frontier:
                pid = frontier.pop()
                owned.append(pid)
                frontier.extend(_children(pid))
            print(json.dumps({'owned': owned}), flush=True)

    run_project_command(spec, task=task, on_chunk=notify)


def _supervise_owner_death(payload):
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    child = subprocess.Popen(
        [sys.executable, '-c',
         'from tests.rootless_vm.test_project_command import _controller; '
         '_controller(__import__("sys").argv[1])', payload],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    handles = {}
    reaped = []
    try:
        assert select.select([child.stdout], [], [], 5)[0], 'controller did not start'
        line = child.stdout.readline()
        assert line.startswith('{'), child.stderr.read(2048)
        owned = json.loads(line)['owned']
        assert len(owned) >= 3, owned
        for pid in owned:
            handles[pid] = pidfd.open_process(pid)
        child.kill()
        child.wait(timeout=3)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid:
                reaped.append(pid)
            else:
                time.sleep(0.02)
        assert all(not Path(f'/proc/{pid}').exists() for pid in owned), owned
        print(json.dumps({'case': 'owner_death', 'owned': owned,
                          'reaped_orphans': reaped, 'all_physically_reaped': True}))
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=3)
        for descriptor in handles.values():
            try:
                pidfd.send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(descriptor)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if not pid:
                time.sleep(0.02)
        child.stdout.close()
        child.stderr.close()


@pytest.mark.parametrize('case', ['cancel', 'context', 'interrupt', 'timeout',
                                'exit', 'parent_death', 'owner_death', 'write', 'file_limit'])
def test_real_namespace_command_teardown(jailed_shell, case, host_tmp_path):
    probe = subprocess.run(['unshare', '--user', '--map-root-user', '--pid',
                            '--net', '--ipc', '--uts', '--fork', '/usr/bin/true'],
                           capture_output=True, timeout=5)
    if probe.returncode:
        pytest.skip('host does not permit required rootless namespaces')
    payload = host_tmp_path / 'spec.json'
    payload.write_text(json.dumps({**jailed_shell.__dict__, 'jail': str(jailed_shell.jail)}))
    worker = subprocess.run(
        [sys.executable, '-c',
         'from tests.rootless_vm.test_project_command import _supervise_case; '
         '_supervise_case(__import__("sys").argv[1],__import__("sys").argv[2])',
         case, str(payload)], cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=15)
    assert worker.returncode == 0, worker.stderr[-2500:] + worker.stdout[-1000:]
    assert json.loads(worker.stdout.splitlines()[-1])['all_physically_reaped']
    print(worker.stdout.splitlines()[-1])

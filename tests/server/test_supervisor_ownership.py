"""A Supervisor retires its worker only when its owner is really gone.

``git merge``/``checkout`` rewrite a changed file by ``unlink()`` and then
``open(O_CREAT|O_EXCL)``, so a live Supervisor polling its own source can see
``supervisor.py`` missing for a moment during any merge into its checkout.
That window is not ownership loss. A deleted or moved checkout, and a declared
owner process that exited or whose PID now names another process, still are.

The end-to-end specs run a real ``supervisor.py`` copied into a disposable
checkout (every other entry is a symlink to this repository) that adopts a
real lock-holding worker.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

import server_manager
import supervisor

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not sys.platform.startswith('linux'), reason='Linux /proc process identity')]

ROOT = Path(__file__).resolve().parents[2]

_WORKER = r'''import fcntl, os, signal, socket, sys, time
lock = open(os.path.join('data', '.server.lock'), 'w+')
fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
lock.write('%d@%s\n' % (os.getpid(), socket.gethostname()))
lock.flush()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(1)
'''


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _source(tmp_path):
    source = tmp_path / 'checkout' / 'supervisor.py'
    source.parent.mkdir()
    source.write_text('# owner\n')
    return source


def _state(pid):
    try:
        raw = Path(f'/proc/{pid}/stat').read_bytes()
    except FileNotFoundError:
        return None
    return raw[raw.rfind(b')') + 2:].split()[0].decode()


def _zombie():
    """An exited child that this process deliberately leaves unreaped."""
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    deadline = time.monotonic() + 10
    while _state(child.pid) != 'Z':
        assert time.monotonic() < deadline, 'child never exited'
        time.sleep(0.01)
    return child


def _source_grace():
    return server_manager.SUPERVISOR_SOURCE_MISSING_GRACE_SECONDS


def _checkout_grace():
    return server_manager.SUPERVISOR_CHECKOUT_MISSING_GRACE_SECONDS


def test_transiently_missing_or_replaced_source_is_not_ownership_loss(tmp_path):
    source = _source(tmp_path)
    clock = _Clock()
    ownership = server_manager.SupervisorOwnership(str(source), clock=clock)
    assert ownership.lost() == ''
    # git checkout_entry: unlink the old file, then create the new one.
    source.unlink()
    assert ownership.lost() == ''
    clock.now += _source_grace() - 0.5
    assert ownership.lost() == ''
    source.write_text('# merged generation\n')
    assert ownership.lost() == ''
    # The window restarts after the file is seen again.
    source.unlink()
    clock.now += 0.5
    assert ownership.lost() == ''
    clock.now += _source_grace() - 1.0
    assert ownership.lost() == ''


@pytest.mark.parametrize('removal', ['file', 'checkout', 'moved', 'recreated'])
def test_source_missing_past_its_grace_is_ownership_loss(tmp_path, removal):
    source = _source(tmp_path)
    clock = _Clock()
    ownership = server_manager.SupervisorOwnership(str(source), clock=clock)
    if removal == 'file':
        source.unlink()
    elif removal == 'checkout':
        shutil.rmtree(source.parent)
    else:
        source.parent.rename(tmp_path / 'moved-checkout')
    if removal == 'recreated':
        # A manager state write (mkdir parents=True) after the move recreates
        # the old path as a different, empty directory.
        (source.parent / 'data').mkdir(parents=True)
    assert ownership.lost() == ''
    # No git operation removes or replaces the checkout root, so that needs
    # only a short confirmation; a missing file in place gets the full grace.
    clock.now += _checkout_grace()
    reason = ownership.lost()
    if removal == 'file':
        assert reason == ''
        clock.now += _source_grace() - _checkout_grace()
        reason = ownership.lost()
        assert 'checkout directory still exists' in reason
    elif removal == 'recreated':
        assert 'checkout directory was replaced' in reason
    else:
        assert 'checkout directory is gone' in reason
    assert 'ownership sentinel disappeared' in reason
    assert str(source) in reason


def test_unverifiable_source_neither_confirms_nor_clears_loss(
        tmp_path, monkeypatch):
    source = _source(tmp_path)
    clock = _Clock()
    ownership = server_manager.SupervisorOwnership(str(source), clock=clock)
    source.unlink()
    assert ownership.lost() == ''
    real_stat = os.stat

    def failing_stat(path, *args, **kwargs):
        if str(path) == str(source):
            raise OSError(5, 'Input/output error')
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(server_manager.os, 'stat', failing_stat)
    clock.now += _source_grace()
    assert ownership.lost() == ''
    monkeypatch.undo()
    assert 'ownership sentinel disappeared' in ownership.lost()


def test_owner_that_exited_unreaped_or_whose_pid_was_reused_is_gone(
        tmp_path, monkeypatch):
    source = _source(tmp_path)
    live = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    zombie = _zombie()
    try:
        assert server_manager.SupervisorOwnership(
            str(source), owner_pid=live.pid).lost() == ''
        assert 'owner process disappeared' in server_manager.SupervisorOwnership(
            str(source), owner_pid=zombie.pid).lost()

        ownership = server_manager.SupervisorOwnership(
            str(source), owner_pid=live.pid)
        real_ticks = server_manager.proc_start_ticks
        monkeypatch.setattr(
            server_manager, 'proc_start_ticks',
            lambda pid: real_ticks(pid) + 1 if pid == live.pid else real_ticks(pid))
        reason = ownership.lost()
        assert f'pid={live.pid}' in reason
        assert 'different process' in reason
    finally:
        live.kill()
        live.wait(timeout=5)
        zombie.wait(timeout=5)


# ── a real supervisor.py over a disposable checkout ────────────────────


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _checkout(tmp_path):
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    for entry in ROOT.iterdir():
        if entry.name.startswith('.') or entry.name in (
                'supervisor.py', 'data', 'logs'):
            continue
        (checkout / entry.name).symlink_to(
            entry, target_is_directory=entry.is_dir())
    shutil.copy2(ROOT / 'supervisor.py', checkout / 'supervisor.py')
    return checkout


def _environment(**extra):
    environment = {
        key: value for key, value in os.environ.items()
        if key not in ('PORT', '_TOFU_RUNTIME_PORT', 'TOFU_TESTING')
        and not key.startswith('PYTEST_')}
    environment.update({key: str(value) for key, value in extra.items()})
    return environment


def _wait_for(predicate, timeout):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


class _Deployment:
    """A disposable checkout whose real Supervisor adopted a real worker."""

    def __init__(self, tmp_path, *, owner_pid):
        self.tmp_path = tmp_path
        self.checkout = _checkout(tmp_path)
        self.project = tmp_path / 'project'
        (self.project / 'data').mkdir(parents=True)
        (self.project / 'logs').mkdir()
        (self.project / 'server.py').write_text(_WORKER)
        (self.project / 'stop.sh').write_text('#!/bin/sh\nexit 0\n')
        self.state_path = self.project / 'data' / 'server-manager-state.json'
        self.log_path = tmp_path / 'supervisor.log'
        self.port = _free_port()
        self.worker = subprocess.Popen(
            [sys.executable, 'server.py'], cwd=self.project,
            env=_environment(), stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        lock = self.project / 'data' / '.server.lock'
        assert _wait_for(
            lambda: lock.is_file()
            and lock.read_text().startswith(f'{self.worker.pid}@'), 20)
        with self.log_path.open('wb') as log:
            self.supervisor = subprocess.Popen(
                [sys.executable, str(self.checkout / 'supervisor.py')],
                cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=log,
                env=_environment(
                    TOFU_SUPERVISOR_PROJECTS=self.project,
                    TOFU_SUPERVISOR_HOST='127.0.0.1',
                    TOFU_SUPERVISOR_PORT=self.port,
                    TOFU_SUPERVISOR_OWNER_PID=owner_pid,
                    TOFU_MANAGER_INTERVAL='0.5',
                    TOFU_DATA_DIR=self.project,
                    TOFU_HEARTBEAT_DIR=tmp_path / 'heartbeat',
                    PORT=_free_port(),
                ))

    def log(self):
        try:
            return self.log_path.read_text(errors='replace')
        except OSError:
            return ''

    def state(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def health(self):
        try:
            with urllib.request.urlopen(
                    f'http://127.0.0.1:{self.port}/health', timeout=1) as response:
                return json.loads(response.read())
        except OSError:
            return {}

    def wait_adopted(self):
        assert _wait_for(lambda: self.health().get('ok') is True, 30), self.log()
        assert _wait_for(
            lambda: (self.state().get('desired') == 'running'
                     and (self.state().get('worker') or {}).get('pid')
                     == self.worker.pid), 30), self.log()

    def close(self):
        for process in (self.supervisor, self.worker):
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.fixture()
def deployment(tmp_path):
    created = []

    def make(owner_pid=None):
        created.append(_Deployment(tmp_path, owner_pid=owner_pid or os.getpid()))
        return created[-1]

    yield make
    for item in created:
        item.close()


def test_live_supervisor_keeps_its_worker_across_a_git_style_rewrite(deployment):
    live = deployment()
    live.wait_adopted()
    source = live.checkout / 'supervisor.py'
    rewritten = source.read_bytes() + b'\n# merged generation\n'
    source.unlink()
    # Several Supervisor polls observe the gap (a slow FUSE/git rewrite).
    time.sleep(1.5)
    descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(rewritten)
    time.sleep(1.0)

    assert live.supervisor.poll() is None, live.log()[-3000:]
    assert live.worker.poll() is None, live.log()[-3000:]
    assert live.state()['desired'] == 'running'
    assert 'ownership sentinel missing' in live.log()
    assert 'ownership sentinel reappeared' in live.log()


def test_deleted_checkout_still_retires_supervisor_and_worker(deployment):
    live = deployment()
    live.wait_adopted()
    shutil.rmtree(live.checkout)

    assert _wait_for(
        lambda: live.supervisor.poll() is not None, 20), live.log()[-3000:]
    assert live.worker.wait(timeout=15) == 0
    assert live.state()['desired'] == 'stopped'
    assert 'ownership sentinel disappeared' in live.log()


def test_moved_checkout_retires_even_after_its_path_is_recreated(tmp_path):
    """The Supervisor's own project is its checkout. After a move, the
    manager's next state write recreates the old path (mkdir parents=True);
    that empty directory is not the checkout and must not stretch the
    confirmation to the in-place file grace."""
    checkout = _checkout(tmp_path)
    (checkout / 'data').mkdir()
    (checkout / 'logs').mkdir()
    (checkout / 'data' / 'server-manager-state.json').write_text(
        json.dumps({'version': server_manager.STATE_VERSION,
                    'desired': 'stopped'}))
    log_path = tmp_path / 'supervisor.log'
    port = _free_port()
    with log_path.open('wb') as log:
        process = subprocess.Popen(
            [sys.executable, str(checkout / 'supervisor.py')],
            cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=log,
            env=_environment(
                TOFU_SUPERVISOR_PROJECTS=checkout,
                TOFU_SUPERVISOR_HOST='127.0.0.1',
                TOFU_SUPERVISOR_PORT=port,
                TOFU_SUPERVISOR_OWNER_PID=os.getpid(),
                TOFU_MANAGER_INTERVAL='0.2',
                TOFU_DATA_DIR=checkout,
                TOFU_HEARTBEAT_DIR=tmp_path / 'heartbeat',
                PORT=_free_port(),
            ))
    try:
        def healthy():
            try:
                with urllib.request.urlopen(
                        f'http://127.0.0.1:{port}/health', timeout=1) as response:
                    return json.loads(response.read()).get('ok') is True
            except OSError:
                return False

        assert _wait_for(healthy, 30), log_path.read_text(errors='replace')
        checkout.rename(tmp_path / 'moved-checkout')
        assert _wait_for(lambda: process.poll() is not None, 5), (
            log_path.read_text(errors='replace')[-3000:])
        assert 'ownership sentinel disappeared' in log_path.read_text(
            errors='replace')
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def test_unreaped_exited_owner_retires_supervisor_and_worker(deployment):
    owner = _zombie()
    try:
        live = deployment(owner_pid=owner.pid)
        assert _wait_for(lambda: live.supervisor.poll() is not None, 30), (
            live.log()[-3000:])
        assert live.worker.wait(timeout=15) == 0
        assert live.state()['desired'] == 'stopped'
        assert 'owner process disappeared' in live.log()
    finally:
        owner.wait(timeout=5)


def test_supervisor_build_server_declares_its_checkout_and_owner(
        tmp_path, monkeypatch):
    project = tmp_path / 'proj'
    (project / 'data').mkdir(parents=True)
    (project / 'server.py').write_text('import time; time.sleep(300)\n')
    (project / 'stop.sh').write_text('#!/bin/sh\nexit 0\n')
    monkeypatch.setenv(supervisor.ENV_HOST, '127.0.0.1')
    monkeypatch.setenv(supervisor.ENV_PORT, '0')
    monkeypatch.setenv(supervisor.ENV_PROJECTS, os.path.realpath(project))
    monkeypatch.setenv(supervisor.ENV_OWNER_PID, str(os.getpid()))
    httpd = supervisor.build_server()
    try:
        assert httpd.ownership.source_file == os.path.realpath(supervisor.__file__)
        assert httpd.ownership.owner_pid == os.getpid()
        httpd.service_actions()
    finally:
        httpd.server_close()

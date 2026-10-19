"""Real agent polling, owned resolution, TeX compilation and artifact transfer.

The HTTP seam is in-process; server cache and device project are separate
directories. The production polling loop, bridge, compiler and scheduler run.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lib.desktop import bridge
from lib.paper.builds import service
from lib.paper.builds.errors import BuildError
from tests.support import paper_builds as fixtures
from tests.support import desktop_paper

pytestmark = pytest.mark.unit
scheduler = fixtures.scheduler
device = desktop_paper.desktop_paper_device



def payload(**extra):
    return {'source': 'local', 'conv_id': 'owned-conversation', 'path': 'paper/main.tex',
            'request_id': 'edit-1', 'subscriber_id': 'reader', 'line': 3, **extra}


def wait_terminal(scheduler, task_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        task = scheduler.production.get(task_id)
        if task['status'] in ('done', 'error', 'aborted'):
            return task
        time.sleep(.05)
    raise AssertionError('Paper task did not settle')


def test_agent_build_crosses_the_real_poll_loop_and_preserves_immutable_location(device, scheduler, monkeypatch):
    if not shutil.which('latexmk') and not (Path(__file__).resolve().parents[2] / '.texlive/bin').is_dir():
        pytest.skip('TeX toolchain unavailable')
    monkeypatch.setattr(service, 'owned_settings', lambda *_: {'projectPath': 'remote:device:project'})
    def no_host(*args, **kwargs):
        raise AssertionError('Remote sources must not resolve on the server')
    monkeypatch.setattr(service, 'bind_host_document', no_host)
    accepted = service.start_preview(payload(), owner_user_id=7, scheduler=scheduler)
    task = wait_terminal(scheduler, accepted['task_id'])
    assert task['status'] == 'done', task.get('error')
    result = task['result']
    receipt = result['receipt']
    assert receipt['target'] == 'desktop' and receipt['version_verified']
    assert receipt['owner_user_id'] == 7 and receipt['document_id'] == accepted['document_id']
    assert result['location']['build_id'] == receipt['build_id'] and result['location']['page'] == 1
    unique = {command['id']: command['type'] for command in device.commands}
    assert list(unique.values()) == ['paper_resolve', 'paper_build']
    assert all(str(device.root) not in json.dumps(command) for command in device.commands)
    assert bridge._streams == {} and bridge.command_queue == {}
    with scheduler.cache.lease(7, receipt['document_id'], receipt['build_id']) as build:
        import fitz
        with fitz.open(build.directory / 'document.pdf') as pdf:
            assert 'A version from the desktop.' in pdf[0].get_text()
    (device.paper / 'main.tex').write_text('Later uncompiled source')
    assert service.locate_build(receipt['document_id'], receipt['build_id'],
        {'file': 'main.tex', 'line': 3}, owner_user_id=7, scheduler=scheduler) == result['location']
    assert all(len(frame['data'].encode()) <= 262144 for frame in device.frames)


def test_desktop_binding_rejects_foreign_owner_unowned_root_and_path_escape(device, scheduler, monkeypatch):
    monkeypatch.setattr(service, 'owned_settings', lambda *_: {'projectPath': 'remote:device:project'})
    with pytest.raises(BuildError, match='different bridge user'):
        service.start_preview(payload(), owner_user_id=8, scheduler=scheduler)
    with pytest.raises(BuildError, match='outside this conversation'):
        service.start_preview(payload(project_root='remote:device:unknown'), owner_user_id=7, scheduler=scheduler)
    with pytest.raises(BuildError, match='escapes share root'):
        service.start_preview(payload(path='../main.tex'), owner_user_id=7, scheduler=scheduler)
    assert not any(command['type'] == 'paper_build' for command in device.commands)


def test_desktop_build_requires_a_versioned_capability(device, scheduler, monkeypatch):
    from lib.paper.builds import desktop
    monkeypatch.setattr(service, 'owned_settings', lambda *_: {'projectPath': 'remote:device:project'})
    for capability in (None, True, 2):
        monkeypatch.setattr(desktop, 'online_agents', lambda: [
            {'agent_id': 'device', 'user_id': '7', 'capabilities': {'paper_build': capability}}])
        with pytest.raises(BuildError, match='Update the Desktop Agent'):
            service.start_preview(payload(), owner_user_id=7, scheduler=scheduler)
    assert not device.commands


def test_device_paper_resolution_and_demand_execution_do_not_load_server_stack(device):
    script = '''import sys, threading
from lib.desktop_agent._paper import PaperBuildWorker, resolve_document
resolve_document({'schema_version':1,'root':'project','path':'paper/main.tex'})
worker=PaperBuildWorker(threading.Event(), lambda: True)
for index in range(32):
    assert worker.admit(str(index))
for index in range(32):
    worker.release(str(index))
for name in ('quart','sqlite3','lib.storage','lib.production.runtime','lib.tasks_pkg','fitz','pymupdf'):
    assert name not in sys.modules, name
worker.close()
'''
    completed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process cancellation fixture')
def test_device_disconnect_cancels_work_and_reclaims_its_temporary_directory(device, monkeypatch, tmp_path):
    from lib.desktop_agent._paper import PaperBuildWorker
    from lib.paper.builds import compiler
    pidfile = tmp_path / 'disconnected.pid'
    executable = tmp_path / 'disconnected-compiler'
    executable.write_text('#!/usr/bin/env python3\nimport os,time\n'
                          f'open({str(pidfile)!r},"w").write(str(os.getpid()))\ntime.sleep(60)\n')
    executable.chmod(0o755)
    monkeypatch.setattr(compiler, 'prepare_toolchain', lambda *_, **__: (str(executable), 'pdflatex', 'held', dict(os.environ)))
    connected = [True]
    monkeypatch.setenv('TOFU_PAPER_BUILD_WORKERS', '1')
    worker = PaperBuildWorker(threading.Event(), lambda: connected[0])
    build_id = 'd' * 32
    assert worker.admit(build_id)
    assert worker.admit('e' * 32), 'independent device builds must start on demand'
    worker.release('e' * 32)
    errors, frames = [], []
    def execute():
        try:
            worker.execute(build_id, {'schema_version': 1, 'root': 'project', 'project_path': 'paper',
                'main_file': 'main.tex', 'edited_file': 'main.tex', 'owner_user_id': 7,
                'document_id': 'a' * 64, 'build_id': build_id}, lambda *frame: frames.append(frame))
        except BuildError as exc:
            errors.append(exc.code)
    thread = threading.Thread(target=execute)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert pidfile.exists()
        connected[0] = False
        thread.join(timeout=5)
        assert not thread.is_alive() and errors == ['cancelled'] and not frames
        assert not worker.pending and list(device.temporary.iterdir()) == []
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
    finally:
        connected[0] = False
        worker.close()
        thread.join(timeout=5)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process cancellation fixture')
def test_detaching_the_last_viewer_cancels_device_process_and_releases_transport(device, scheduler, monkeypatch, tmp_path):
    from lib.paper.builds import compiler
    monkeypatch.setattr(service, 'owned_settings', lambda *_: {'projectPath': 'remote:device:project'})
    pidfile = tmp_path / 'compiler.pid'
    executable = tmp_path / 'held-compiler'
    executable.write_text('#!/usr/bin/env python3\nimport os,time\n'
                          f'open({str(pidfile)!r},"w").write(str(os.getpid()))\ntime.sleep(60)\n')
    executable.chmod(0o755)
    monkeypatch.setattr(compiler, 'prepare_toolchain', lambda *_, **__: (str(executable), 'pdflatex', 'held', dict(os.environ)))
    accepted = service.start_preview(payload(), owner_user_id=7, scheduler=scheduler)
    deadline = time.monotonic() + 5
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert pidfile.exists(), 'device compiler did not start'
    pid = int(pidfile.read_text())
    assert scheduler.detach(accepted['task_id'], owner_user_id=7, subscriber_id='reader')
    assert wait_terminal(scheduler, accepted['task_id'])['status'] == 'aborted'
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(.02)
    else:
        import psutil
        raise AssertionError(f'Device compiler survived preview cancellation: status={psutil.Process(pid).status()}, commands={[row["type"] for row in device.commands]}')
    assert any(command['type'] == 'paper_build_cancel' for command in device.commands)
    assert not bridge._streams
    assert not scheduler.cache.latest(7, accepted['document_id'])


def test_desktop_compilation_lifetime_is_connection_and_cancel_owned(monkeypatch):
    from lib.desktop_agent import _paper
    stopped=__import__('threading').Event();connected=[True]
    cancel=_paper._Cancellation(stopped,lambda:connected[0])
    monkeypatch.setattr(time, 'monotonic', lambda: 10**12)
    assert not cancel.is_set()
    connected[0]=False
    assert cancel.is_set()
    connected[0]=True;cancel.local.set()
    assert cancel.is_set()

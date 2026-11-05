"""Real process retention, manager-death wake and physical execution drain."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from serverctl_pkg.execution_service import ExecutionService
from serverctl_pkg.resident_service import manager_control_matches, process_matches

pytestmark = [
    pytest.mark.api,
    pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux process identity required'),
]


class ProbeService(ExecutionService):
    host_module = 'tests.support.execution_host_probe'


def test_control_requires_the_exact_pipe_and_endpoint_direction():
    reader, writer = os.pipe()
    other_reader, other_writer = os.pipe()
    try:
        info = os.fstat(writer)
        record = {'managerControl': {'fd': writer, 'device': info.st_dev, 'inode': info.st_ino}}
        assert manager_control_matches(record, reader, write=False)
        assert manager_control_matches(record, writer, write=True)
        assert not manager_control_matches(record, reader, write=True)
        assert not manager_control_matches(record, other_reader, write=False)
    finally:
        for descriptor in (reader, writer, other_reader, other_writer):
            os.close(descriptor)


def wait_for(predicate):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError('resident process did not reach the expected state')


def environment(project):
    return {**os.environ, 'TOFU_PROJECT_PATH': str(project),
            'PYTHONPATH': str(Path(__file__).resolve().parents[2])}


def test_execution_survives_reattach_and_stop_waits_for_physical_exit(tmp_path):
    service = ProbeService(str(tmp_path), sys.executable)
    plan = {'application': 'controlled-execution'}
    record = {}
    try:
        service.ensure(plan, environment(tmp_path))
        record = wait_for(lambda: (value if (value := service.record()).get('state') == 'ready' else None))
        assert (tmp_path / 'entered').exists()
        service.ensure(plan, environment(tmp_path))
        assert service.record()['pid'] == record['pid']
        with ThreadPoolExecutor() as pool:
            stopped = pool.submit(service.stop, 30)
            try:
                wait_for(lambda: (tmp_path / 'draining').exists())
                assert not stopped.done()
                assert process_matches(record['pid'], record['processStartTime'])
            finally:
                if service.host_matches(service.record()):
                    os.kill(record['pid'], signal.SIGUSR1)
            stopped.result(30)
        assert (tmp_path / 'exited').exists()
        assert not service.state_path.exists()
    finally:
        remaining = service.record()
        if service.host_matches(remaining):
            os.kill(remaining['pid'], signal.SIGUSR1)
            service.stop(30)


def test_manager_exec_retains_execution_and_exit_waits_for_physical_drain(tmp_path):
    program = """import os, signal, sys
from tests.server.test_execution_service import ProbeService, environment
from pathlib import Path
service = ProbeService(sys.argv[1], sys.executable)
service.ensure({'application': 'controlled-execution'}, environment(sys.argv[1]))
if len(sys.argv) == 2:
    os.execv(sys.executable, [sys.executable, __file__, sys.argv[1], 'retained'])
(Path(sys.argv[1]) / 'manager-exec').touch()
signal.pause()
"""
    launcher = tmp_path / 'manager.py'
    launcher.write_text(program)
    manager = subprocess.Popen([sys.executable, str(launcher), str(tmp_path)],
                               env=environment(tmp_path), start_new_session=True)
    service = ProbeService(str(tmp_path), sys.executable)
    record = {}
    try:
        record = wait_for(lambda: (value if (value := service.record()).get('state') == 'ready' else None))
        wait_for(lambda: (tmp_path / 'manager-exec').exists())
        assert not (tmp_path / 'draining').exists()
        manager.kill()
        manager.wait(30)
        wait_for(lambda: (tmp_path / 'draining').exists())
        assert service.record()['state'] != 'stopped'
        with pytest.raises(RuntimeError, match='drained'):
            service.ensure({'application': 'controlled-execution'}, environment(tmp_path))
        os.kill(record['pid'], signal.SIGUSR1)
        wait_for(lambda: not process_matches(record['pid'], record['processStartTime']))
        assert service.record()['state'] == 'stopped'
        assert (tmp_path / 'exited').exists()
        service.stop(30)
    finally:
        if manager.poll() is None:
            manager.kill()
            manager.wait(30)
        remaining = service.record()
        if service.host_matches(remaining):
            os.kill(remaining['pid'], signal.SIGUSR1)
            service.stop(30)

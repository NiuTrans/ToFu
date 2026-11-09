"""A slow execution drain names its cause and never leaves the server down.

2026-11-06 14:10 restart: the execution host's ``process background services``
join outlived its 10 s budget. ``asyncio.wait_for`` raised a bare
``TimeoutError``, the host recorded ``failed`` with the error text
"TimeoutError: ", and the manager refused the whole restart ("execution
shutdown failed: TimeoutError: " at 14:11:13). No API host was started until
a manual restart at 14:52:37.
"""
import asyncio
import logging
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from serverctl_pkg.execution_service import ExecutionService
from serverctl_pkg.resident_service import process_matches

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(not sys.platform.startswith('linux'),
                                 reason='Linux process identity required')]


def test_background_stop_timeout_names_the_owner_still_stopping(monkeypatch):
    from lib.server import server_background_services as services
    from lib.server import server_shutdown

    app = SimpleNamespace(extensions={'tofu_registered_background_services': True})

    def slow_stop(app, *, timeout):
        app.extensions['tofu_background_service_stopping'] = 'proactive scheduler'
        time.sleep(1.0)
        return 0

    monkeypatch.setattr(services, 'stop_registered_background_services', slow_stop)
    monkeypatch.setattr(server_shutdown, 'PROCESS_BACKGROUND_STOP_SECONDS', 0.2)

    with pytest.raises(TimeoutError, match='still stopping: proactive scheduler'):
        asyncio.run(server_shutdown._stop_process_background_services(
            app, logging.getLogger('test-shutdown')))


class FailedCloseService(ExecutionService):
    host_module = 'tests.support.execution_host_failed_close_probe'


def _environment(project):
    return {**os.environ, 'TOFU_PROJECT_PATH': str(project),
            'PYTHONPATH': str(Path(__file__).resolve().parents[2])}


def _ready(service):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        record = service.record()
        if record.get('state') == 'ready':
            return record
        time.sleep(0.02)
    raise AssertionError('execution host did not become ready')


@pytest.mark.api
def test_execution_host_whose_close_failed_is_still_replaced(tmp_path):
    service = FailedCloseService(str(tmp_path), sys.executable)
    plan = {'application': 'failed-close'}
    try:
        service.ensure(plan, _environment(tmp_path))
        first = _ready(service)

        # The host exits with its close failed. An explicit restart retires it
        # instead of leaving the whole server down.
        retired = service.stop(30, retire_failed_close=True)
        assert 'process background services did not stop' in retired

        assert (tmp_path / f'close-failed-{first["pid"]}').exists()
        assert not process_matches(first['pid'], first['processStartTime'])
        assert not service.state_path.exists()
        # A successor starts: one slow drain no longer leaves the server down.
        service.ensure(plan, _environment(tmp_path))
        second = _ready(service)
        assert second['pid'] != first['pid']
    finally:
        if service.host_matches(service.record()):
            service.stop(30, retire_failed_close=True)

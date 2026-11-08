"""A real headless production application borrows storage and owns no HTTP port."""
import os
from pathlib import Path
import socket
import sys
import time

import pytest

from serverctl_pkg.execution_service import ExecutionService
from serverctl_pkg.native_storage_service import NativeStorageService
from serverctl_pkg.restart_budget import NATIVE_SERVICE_PREPARATION_SECONDS

pytestmark = pytest.mark.api
ROOT = Path(__file__).resolve().parents[2]


def test_real_application_boot_and_shutdown_preserve_resident_storage(tmp_path):
    (tmp_path / 'contracts').mkdir()
    (tmp_path / 'contracts/native_storage_connection_v2.json').symlink_to(
        ROOT / 'contracts/native_storage_connection_v2.json')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('TOFU_', 'TOFUDB_')) and key != 'LD_LIBRARY_PATH'}
    environment.update(PYTHONPATH=str(ROOT), TOFU_PROJECT_PATH=str(tmp_path),
        TOFU_DATA_DIR=str(tmp_path / 'data'), TOFU_DEPLOYMENT_MODE='personal',
        TOFU_PERSONAL_STORAGE_BACKEND='sqlite')
    storage = NativeStorageService(str(tmp_path), sys.executable)
    execution = ExecutionService(str(tmp_path), sys.executable)
    occupied = socket.socket()
    occupied.bind(('127.0.0.1', 0))
    occupied.listen()
    try:
        plan = storage.prepare(environment)
        environment['TOFU_STORAGE_CONNECTION_FILE'] = storage.ensure(plan, environment)
        environment['TOFU_PROJECT_PATH'] = str(ROOT)
        environment['PORT'] = str(occupied.getsockname()[1])
        execution_plan = execution.prepare(environment)
        execution.ensure(execution_plan, environment)
        deadline = time.monotonic() + NATIVE_SERVICE_PREPARATION_SECONDS
        while time.monotonic() < deadline:
            record = execution.record()
            if record.get('state') in {'ready', 'failed'}:
                break
            time.sleep(.05)
        assert record.get('state') == 'ready', record
        from lib.tasks_pkg.command_control_transport import call, CommandControlUnavailable
        endpoint = record['commandControl']
        assert endpoint['serviceId'] == record['serviceId']
        assert call(endpoint, 'interrupt', 'absent-task', user_id=1) is None
        with pytest.raises(CommandControlUnavailable):
            call({**endpoint, 'serviceId': 'previous-incarnation'},
                 'interrupt', 'absent-task', user_id=1)
        assert not (tmp_path / 'data/server.lock').exists()
        storage_pid = storage.record()['pid']
        execution.stop()
        assert not execution.state_path.exists()
        assert storage.record()['pid'] == storage_pid
        assert storage.host_matches(storage.record())
    finally:
        try:
            execution.stop()
        finally:
            storage.stop()
            occupied.close()

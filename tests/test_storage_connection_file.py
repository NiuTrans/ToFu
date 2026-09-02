"""Independent co-container Storage Sidecar handoff contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from lib.storage.connection_file import (
    read_connection_file,
    remove_connection_file,
    write_connection_file,
)
from lib.storage.protocol import PROTOCOL_VERSION
from lib.storage.supervisor import StorageSupervisor


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_connection_file_is_private_atomic_and_token_owned(tmp_path):
    path = tmp_path / 'storage.json'
    token = 'a' * 48
    write_connection_file(
        path, host='127.0.0.1', port=32123, token=token, backend='postgres')

    assert path.stat().st_mode & 0o777 == 0o600
    assert read_connection_file(path) == {
        'format': 'tofu.storage-connection/v1',
        'protocol': PROTOCOL_VERSION,
        'host': '127.0.0.1',
        'port': 32123,
        'token': token,
        'backend': 'postgres',
    }
    assert not list(tmp_path.glob('.storage.json.tmp-*'))
    assert remove_connection_file(path, token='b' * 48) is False
    assert path.exists()
    assert remove_connection_file(path, token=token) is True
    assert not path.exists()


def test_connection_file_rejects_broad_mode_and_symlink(tmp_path):
    path = tmp_path / 'storage.json'
    document = {
        'format': 'tofu.storage-connection/v1',
        'protocol': PROTOCOL_VERSION,
        'host': '127.0.0.1',
        'port': 32123,
        'token': 'a' * 48,
        'backend': 'sqlite',
    }
    path.write_text(json.dumps(document), encoding='utf-8')
    path.chmod(0o640)
    with pytest.raises(RuntimeError, match='permissions'):
        read_connection_file(path)

    target = tmp_path / 'target.json'
    target.write_text(json.dumps(document), encoding='utf-8')
    target.chmod(0o600)
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(RuntimeError, match='regular file'):
        read_connection_file(path)


def test_supervisor_attaches_without_owning_external_sidecar_process(tmp_path):
    connection_file = tmp_path / 'run' / 'storage.json'
    connection_file.parent.mkdir()
    project = tmp_path / 'project'
    (project / 'data').mkdir(parents=True)
    (project / 'logs').mkdir()
    environment = os.environ.copy()
    environment.update({
        'TOFU_DEPLOYMENT_MODE': 'personal',
        'TOFU_PROCESS_ROLE': 'all',
        'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE': '1',
        'TOFU_STORAGE_PROJECT_ROOT': str(project),
        'TOFU_STORAGE_TEST_BACKEND': 'sqlite',
        'TOFU_STORAGE_CONNECTION_FILE': str(connection_file),
    })
    environment.pop('TOFU_STORAGE_TOKEN', None)
    environment.pop('TOFU_STORAGE_PARENT_PID', None)
    process = subprocess.Popen(
        [sys.executable, '-m', 'lib.storage_sidecar'],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    supervisor = None
    crashed = threading.Event()
    try:
        assert process.stdout is not None
        ready = json.loads(process.stdout.readline())
        assert ready['type'] == 'storage.ready'
        supervisor = StorageSupervisor(
            backend='sqlite',
            connection_file=connection_file,
            startup_timeout=5.0,
            on_crash=lambda _code: crashed.set(),
        )
        assert supervisor.start().health()['ready'] is True
        assert supervisor.status()['pid'] is None
        supervisor.stop()
        assert process.poll() is None

        assert supervisor.start().health()['ready'] is True
        process.terminate()
        process.wait(timeout=5)
        assert crashed.wait(4.0)
        assert supervisor.wait_until_unready(timeout=1.0)
    finally:
        if supervisor is not None:
            supervisor.stop()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        if process.returncode not in {0, -15}:
            assert process.stderr is not None
            pytest.fail(process.stderr.read())

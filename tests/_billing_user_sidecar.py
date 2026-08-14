"""Project-local storage.v1 runtime for tenant-user integration tests."""

from __future__ import annotations

import os
from pathlib import Path
import uuid

import pytest


@pytest.fixture(scope='module', autouse=True)
def billing_user_storage():
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test

    project_root = (
        Path(__file__).resolve().parents[1]
        / 'data'
        / 'storage-certification'
        / 'billing-user-tests'
        / f'{os.getpid()}-{uuid.uuid4().hex}'
    )
    project_root.mkdir(parents=True, exist_ok=True)
    runtime = StorageRuntime(
        StorageSupervisor(
            project_root=project_root, backend='sqlite', startup_timeout=20),
        auto_restart=False,
    )
    install_runtime_for_test(runtime)
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.stop()
        install_runtime_for_test(None)

"""Isolated Storage Sidecar authority for credential-focused test modules."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet
import pytest


@pytest.fixture(scope='module', autouse=True)
def credential_storage(tmp_path_factory):
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test

    project_root = tmp_path_factory.mktemp('credential-sidecar')
    previous_secret_key = os.environ.get('TOFU_SECRET_ENCRYPTION_KEY')
    os.environ['TOFU_SECRET_ENCRYPTION_KEY'] = Fernet.generate_key().decode(
        'ascii')
    from lib.secret_envelope import reset_secret_envelope_for_test
    reset_secret_envelope_for_test()
    runtime = StorageRuntime(
        StorageSupervisor(
            project_root=project_root,
            backend='sqlite',
            startup_timeout=60,
        ),
        auto_restart=False,
    )
    install_runtime_for_test(runtime)
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.stop()
        install_runtime_for_test(None)
        if previous_secret_key is None:
            os.environ.pop('TOFU_SECRET_ENCRYPTION_KEY', None)
        else:
            os.environ['TOFU_SECRET_ENCRYPTION_KEY'] = previous_secret_key
        reset_secret_envelope_for_test()


__all__ = ['credential_storage']

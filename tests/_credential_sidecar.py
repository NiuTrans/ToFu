"""Isolated Storage Sidecar authority for credential-focused test modules."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet
import pytest

from tests.support.sidecar_fixtures import module_declares_plugin


_PLUGIN_NAME = 'tests._credential_sidecar'


@pytest.fixture(scope='module', autouse=True)
def credential_storage(request, tmp_path_factory):
    """Install a credential Sidecar only for modules that declared it.

    pytest registers ``pytest_plugins`` process-wide, so an autouse fixture
    would otherwise run for every subsequently collected module and stop the
    previous module's global runtime (fencing unrelated tests).
    """
    if not module_declares_plugin(request, _PLUGIN_NAME):
        yield None
        return

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

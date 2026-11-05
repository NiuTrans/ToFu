"""The evolution task fixture never runs OWNER's tasks on another owner's authority.

``start_storage()`` leaves the shared test authority ready between modules. In a
native lane it is TofuDB bound to the personal owner, and that authority refuses
every owned write for another owner with ``authority_scope_mismatch``.
"""
import os

import pytest

from tests.evolution.test_evolution_runs import OWNER, runtime as runtime

pytestmark = pytest.mark.unit


@pytest.fixture
def personal_native_authority(tmp_path, monkeypatch):
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage import service
    from lib.storage.service import install_runtime_for_test

    if not os.environ.get('TOFUDB_BIN'):
        pytest.skip('Set TOFUDB_BIN to a source-matched native engine')
    monkeypatch.delenv('TOFUDB_OWNER_ID', raising=False)
    authority = StorageRuntime(
        StorageSupervisor(project_root=tmp_path, backend='tofudb', startup_timeout=60),
        auto_restart=False,
    )
    install_runtime_for_test(authority)
    try:
        authority.start()
        yield authority
    finally:
        with service._lock:
            still_installed = service._runtime is authority
        if still_installed:
            install_runtime_for_test(None)
        else:
            authority.stop()


def test_task_fixture_replaces_a_ready_native_authority_of_another_owner(
        personal_native_authority, runtime):
    from lib.storage.service import storage_runtime

    runs, _, tasks = runtime
    assert storage_runtime() is not personal_native_authority
    run = runs.submit_run({'objective': 'Explain why binary search is logarithmic.'},
                          user_id=OWNER, idempotency_key='owner-scoped-authority')
    assert run['status'] == 'executing'
    assert len(tasks) == 1

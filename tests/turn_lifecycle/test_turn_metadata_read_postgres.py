"""Run owned Turn metadata and growing requirement scenarios on PostgreSQL.

The shared public scenarios retain their assertions. A function-owned cluster
uses a private Unix socket; its SQL Sidecar joins before the cluster is reaped.
No existing database, external DSN or application runtime is borrowed.
"""
import pytest

from tests.artifacts.test_turn_requirement_read_cost import (
    test_current_requirements_do_not_materialize_turn_projection,
)
from tests.artifacts.test_turn_review import subject as subject
from tests.turn_lifecycle.test_task_event_delivery import turn_service as turn_service
from tests.turn_lifecycle.test_turn_metadata_read import (
    test_metadata_matches_default_owned_turn_without_growing_body,
)

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.api

__all__ = [
    'test_metadata_matches_default_owned_turn_without_growing_body',
    'test_current_requirements_do_not_materialize_turn_projection',
]


@pytest.fixture
def storage(postgres_cluster, tmp_path, monkeypatch):
    from lib.storage import StorageRuntime, StorageSupervisor
    from lib.storage.service import install_runtime_for_test

    dsn_file = tmp_path / 'postgres-dsn'
    with dsn_file.open('x', encoding='utf-8') as output:
        dsn_file.chmod(0o600)
        output.write(postgres_cluster)
    monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', str(dsn_file))
    monkeypatch.setenv('TOFUDB_OWNER_ID', '1')
    monkeypatch.setenv('TOFU_STORAGE_PG_READ_POOL', '2')
    monkeypatch.setenv('TOFU_STORAGE_PG_WRITE_POOL', '2')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='postgres', connection_file='',
        startup_timeout=60)
    runtime = StorageRuntime(supervisor, auto_restart=False)
    try:
        install_runtime_for_test(runtime)
        runtime.start()
        assert runtime.status()['backend'] == 'postgres'
        yield supervisor
    finally:
        try:
            install_runtime_for_test(None)
        finally:
            runtime.stop()
        assert runtime.status()['state'] == 'stopped'

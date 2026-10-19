"""Native wallet admission retains its transaction boundary with parallel SQL writers."""
from tests.storage.test_storage_application_contract import application_storage_owner_id as application_storage_owner_id
import pytest

from tests.storage.test_native_billing_admission import (
    test_admitted_hold_survives_missing_task_metadata_and_only_exact_settlement_releases,
    test_lost_admission_ack_stays_protected_and_retry_never_adds_another_entry,
    test_racing_refund_and_admission_have_one_winner,
)

pytest_plugins = ('tests.support.postgres_cluster',)
pytestmark = pytest.mark.api

# Reuse the same public wallet scenarios under a genuinely concurrent authority.
__all__ = [
    'test_admitted_hold_survives_missing_task_metadata_and_only_exact_settlement_releases',
    'test_lost_admission_ack_stays_protected_and_retry_never_adds_another_entry',
    'test_racing_refund_and_admission_have_one_winner',
]


@pytest.fixture
def application_storage(postgres_cluster, tmp_path, monkeypatch):
    from lib.storage import StorageRuntime, StorageSupervisor, service

    dsn_file = tmp_path / 'postgres-dsn'
    dsn_file.write_text(postgres_cluster)
    dsn_file.chmod(0o600)
    monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', str(dsn_file))
    monkeypatch.setenv('TOFU_STORAGE_PG_READ_POOL', '2')
    monkeypatch.setenv('TOFU_STORAGE_PG_WRITE_POOL', '2')
    runtime = StorageRuntime(StorageSupervisor(
        project_root=tmp_path, backend='postgres', startup_timeout=60),
        auto_restart=False)
    monkeypatch.setattr(service, 'storage_runtime', lambda: runtime)
    try:
        runtime.start()
        yield runtime.client()
    finally:
        runtime.stop()

"""The selected SQL authority outlives API attachments through the common host."""
import os
from pathlib import Path
import signal
import sys
from types import SimpleNamespace

import pytest

from serverctl_pkg.native_storage_service import NativeStorageService

pytestmark = pytest.mark.unit
pytest_plugins = ('tests.support.postgres_cluster',)
ROOT = Path(__file__).resolve().parents[2]


class PostgresTestService(NativeStorageService):
    host_module = 'tests.support.sql_storage_host_probe'

    def host_matches(self, record):
        from serverctl_pkg.resident_service import host_matches
        return host_matches(record, host_module=self.host_module)


def test_sql_plan_preserves_the_selected_backend_without_native_admission(monkeypatch):
    from serverctl_pkg.native_storage_host import prepare_plan
    monkeypatch.setenv('TOFU_PERSONAL_STORAGE_BACKEND', 'sqlite')
    supervisor = SimpleNamespace(_configured_backend=lambda: 'sqlite')
    plan = prepare_plan(supervisor)
    assert plan['backend'] == 'sqlite'
    assert 'binary' not in plan
    assert len(plan['configurationSha256']) == 64


def test_sql_connection_export_requires_a_current_owned_generation():
    from lib.storage.supervisor import StorageSupervisor
    supervisor = StorageSupervisor(backend='sqlite', connection_file='')
    with pytest.raises(RuntimeError, match='ready'):
        supervisor.connection_document(service_id='test-service')


@pytest.mark.parametrize('backend', ['sqlite', 'postgres'])
def test_sql_authority_survives_two_client_lifetimes(tmp_path, request, backend):
    from lib.storage.supervisor import StorageSupervisor
    from tests.server.test_native_storage_service import read_connection, wait_for
    (tmp_path / 'contracts').mkdir()
    (tmp_path / 'contracts/native_storage_connection_v2.json').symlink_to(
        ROOT / 'contracts/native_storage_connection_v2.json')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('TOFU_', 'TOFUDB_'))}
    environment.update({
        'PYTHONPATH': str(ROOT), 'TOFU_PROJECT_PATH': str(tmp_path),
        'TOFU_DATA_DIR': str(tmp_path / 'data'),
        'TOFU_DEPLOYMENT_MODE': 'personal', 'TOFU_PERSONAL_STORAGE_BACKEND': 'sqlite',
    })
    if backend == 'postgres':
        dsn_file = tmp_path / 'postgres-dsn'
        dsn_file.write_text(str(request.getfixturevalue('postgres_cluster')))
        dsn_file.chmod(0o600)
        environment.update({
            'TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE': '1',
            'TOFU_STORAGE_TEST_BACKEND': backend,
            'TOFU_STORAGE_TEST_POSTGRES_DSN_FILE': str(dsn_file),
        })
    service_class = PostgresTestService if backend == 'postgres' else NativeStorageService
    service = service_class(str(tmp_path), sys.executable)
    plan = service.prepare(environment)
    assert plan and plan['backend'] == backend
    try:
        connection_path = service.ensure(plan, environment)
        first = wait_for(lambda: read_connection(service))
        assert first['backend'] == backend
        host_pid = service.record()['pid']
        for index in range(2):
            client_owner = StorageSupervisor(project_root=tmp_path, backend=backend,
                                             connection_file=connection_path)
            try:
                client = client_owner.start()
                if index == 0:
                    client.command('conversation.create', {
                        'conv_id': 'resident-sql', 'user_id': 1,
                        'title': 'SQL retained', 'settings': {},
                    }, 'resident-sql-create')
                else:
                    result = client.query('conversation.get', {'conv_id': 'resident-sql', 'user_id': 1})
                    assert result['metadata']['title'] == 'SQL retained'
            finally:
                client_owner.stop()
            assert read_connection(service) == first
            assert service.ensure(plan, environment) == connection_path
            if index == 0:
                child_pid = wait_for(lambda: service.record().get('nativePid'))
                os.kill(child_pid, signal.SIGKILL)
                first = wait_for(lambda: (
                    document if (document := read_connection(service))
                    and document['token'] != first['token'] else None))
                assert service.record()['pid'] == host_pid
    finally:
        service.stop()
    assert not service.connection_path.exists()

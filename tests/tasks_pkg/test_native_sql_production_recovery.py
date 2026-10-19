"""Full public application restart on explicitly selected native SQL journals.

Reuses the production file/Goal recovery contracts and owns every SQL scope,
installed image pin, process and provider. No storage or execution mocks.
"""
import json
import os
from pathlib import Path
import subprocess
from dataclasses import replace

import pytest

pytestmark = [pytest.mark.api, pytest.mark.slow]
BACKENDS = ['sqlite'] + (['postgres'] if os.environ.get('TOFU_STORAGE_TEST_POSTGRES') == '1' else [])


@pytest.fixture(params=BACKENDS)
def sql_application(request, tmp_path, monkeypatch):
    image = os.environ.get('TOFU_CHECKPOINT_SQL_TEST_LIBRARY')
    if not image:
        pytest.skip('explicit installed and pinned checkpoint image required')
    from lib.storage_authority_sql.generated_checkpoint_storage_contract import SPEC
    configuration = dict(format=SPEC['application']['configuration_format'], library_path=image,
        sha256=os.environ['TOFU_CHECKPOINT_SQL_TEST_SHA256'], binary_bytes=Path(image).stat().st_size,
        tenant_id=1, owner_user_id=1, document_bytes=8 << 20, identity_bytes=4096)
    monkeypatch.setenv(SPEC['application']['environment'], json.dumps(configuration))
    # The private cluster is sized for this fixture's explicit concurrent demand.
    monkeypatch.setenv('TOFU_STORAGE_PG_READ_POOL', '2')
    monkeypatch.setenv('TOFU_STORAGE_PG_WRITE_POOL', '2')
    if request.param == 'postgres':
        from tests.support.postgres_isolation import isolated_postgres_dsn
        source = Path(os.environ['TOFU_STORAGE_TEST_POSTGRES_DSN_FILE'])
        with isolated_postgres_dsn(source, tmp_path) as dsn:
            monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', str(dsn))
            # Retain the application's PostgreSQL cutover backup requirement.
            # Initialize only this private schema, then take an actual dump
            # before publishing the confirmation consumed by production boot.
            import psycopg
            from lib.storage_authority_sql.adapters.postgres import PostgresBackend
            from tests.storage_authority_sql.test_sqlite_read_pool import _config
            preparation = tmp_path/'database-preparation'
            preparation.mkdir()
            backend = PostgresBackend(replace(_config(preparation), backend='postgres', postgres_dsn=dsn.read_text()))
            try:
                backend.start()
            finally:
                backend.close()
            with psycopg.connect(dsn.read_text()) as connection:
                schema = connection.execute('SELECT current_schema()').fetchone()[0]
            backup = tmp_path/'before-project-cutover.pgdump'
            subprocess.run(['pg_dump', '--format=custom', '--schema='+schema, '--file='+str(backup),
                            '--dbname='+dsn.read_text()], check=True, capture_output=True, timeout=30)
            assert backup.stat().st_size > 0
            monkeypatch.setenv('TOFU_PROJECT_BRAIN_CUTOVER_BACKUP_CONFIRMED', '1')
            yield request.param
    else:
        yield request.param


@pytest.mark.parametrize('revise_accepted', [False, True])
def test_public_file_recovery_uses_selected_sql(tmp_path, sql_application, revise_accepted):
    from tests.tasks_pkg.test_native_production_recovery import (
        test_pending_file_survives_full_service_loss_and_public_continue as recover)
    recover(tmp_path, revise_accepted, storage_backend=sql_application)


def test_public_goal_recovery_uses_selected_sql(tmp_path, sql_application):
    from tests.tasks_pkg.test_native_production_goal import (
        test_public_goal_recovers_published_actor_after_full_service_loss as recover)
    recover(tmp_path, 'daemon', 'strict', 'application', storage_backend=sql_application)

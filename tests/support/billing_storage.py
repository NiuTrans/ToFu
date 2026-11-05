"""Real billing authorities: SQLite, both native transports and opt-in PostgreSQL.

Reuses application storage lifecycle and its explicit owner; PostgreSQL receives
one disposable schema per test. No configured application authority is opened.
"""

import os
from pathlib import Path

import pytest

from tests.storage import test_storage_application_contract as storage_contract


@pytest.fixture(params=['sqlite', 'tofudb',
    pytest.param('tofudb_embedded', marks=pytest.mark.serial),
    pytest.param('postgres', marks=pytest.mark.skipif(
        os.environ.get('TOFU_STORAGE_TEST_POSTGRES') != '1', reason='PostgreSQL lane is opt-in'))])
def application_storage(request, tmp_path, monkeypatch, application_storage_owner_id):
    fixture = storage_contract.application_storage.__wrapped__
    if request.param == 'postgres':
        from tests.support.postgres_isolation import isolated_postgres_dsn
        source = Path(os.environ['TOFU_STORAGE_TEST_POSTGRES_DSN_FILE'])
        with isolated_postgres_dsn(source, tmp_path) as dsn:
            monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', str(dsn))
            yield from fixture(request, tmp_path, monkeypatch, application_storage_owner_id)
    else:
        yield from fixture(request, tmp_path, monkeypatch, application_storage_owner_id)


@pytest.fixture
def application_storage_owner_id():
    return 2


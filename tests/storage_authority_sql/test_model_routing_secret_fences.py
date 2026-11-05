"""Secret cleanup fences current aggregate commits and concurrent refreshes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
import threading
import time
import uuid

import pytest

from lib.storage_authority_sql.operations_pkg import _model_routing
from tests.model_routing.test_secret_lifecycle import referenced_document
from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient
from tests.storage_authority_sql.test_sqlite_read_pool import _config
from tests.support.postgres_cluster import owned_postgres_cluster

pytestmark = pytest.mark.unit
OWNER = {'owner_user_id': 7, 'tenant_id': 'workspace'}
REFERENCE = 'shared-reference'


@pytest.fixture(params=['sqlite', 'postgres'])
def sql_backend(request, tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_FASTPATH', 'off')
    config = _config(tmp_path)
    with ExitStack() as resources:
        if request.param == 'postgres':
            from lib.storage_authority_sql.adapters.postgres import PostgresBackend
            dsn = resources.enter_context(owned_postgres_cluster())
            backend = PostgresBackend(replace(config, backend='postgres', postgres_dsn=dsn))
        else:
            from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
            backend = SQLiteBackend(config)
        backend.start()
        resources.callback(backend.close)
        yield backend


def command(backend, operation, payload):
    return TransactionClient(backend).command(operation, payload, str(uuid.uuid4()))


def direct(backend, operation):
    return backend.command('test.secret_fence', '', None, 'user', operation,
                           time.monotonic() + 20, receipt_required=False)


def secret_payload(*, updated_at=1.0, reference=REFERENCE):
    return {**OWNER, 'secret_reference': reference, 'ciphertext': 'fixture-ciphertext',
            'key_hint': 'fixture', 'updated_at': updated_at}


@pytest.mark.parametrize('cleanup', ['delete', 'prune'])
def test_cleanup_waits_for_inflight_reference_commit(sql_backend, cleanup):
    command(sql_backend, 'model_routing.secret.put', secret_payload())
    entered, release, submitted = threading.Event(), threading.Event(), threading.Event()

    def commit(session):
        _model_routing._model_routing_commit(session, {
            **OWNER, 'expected_revision': 0, 'document': referenced_document(REFERENCE),
            'updated_at': 2.0,
        })
        entered.set()
        assert release.wait(10)

    def clean():
        submitted.set()
        payload = {'secret_reference': REFERENCE} if cleanup == 'delete' else {
            'active_secret_references': [], 'updated_before': 100.0}
        return command(sql_backend, 'model_routing.secret.' + cleanup, {**OWNER, **payload})

    with ThreadPoolExecutor(max_workers=3) as executor:
        committing = executor.submit(direct, sql_backend, commit)
        assert entered.wait(5)
        cleaning = executor.submit(clean)
        try:
            assert submitted.wait(5)
            with pytest.raises(TimeoutError):
                cleaning.result(timeout=0.1)
            if sql_backend.name == 'postgres':
                # An independent owner must progress while this aggregate is held.
                executor.submit(command, sql_backend, 'model_routing.secret.put',
                                {**secret_payload(), 'owner_user_id': 8}).result(timeout=5)
        finally:
            release.set()
        committing.result(timeout=10)
        result = cleaning.result(timeout=10)
    assert result['deleted' if cleanup == 'delete' else 'count'] == 0
    assert TransactionClient(sql_backend).query('model_routing.secret.get', {
        **OWNER, 'secret_reference': REFERENCE})['ciphertext'] == 'fixture-ciphertext'


@pytest.mark.parametrize('sql_backend', ['postgres'], indirect=True)
def test_prune_rechecks_timestamp_after_concurrent_refresh(sql_backend, monkeypatch):
    from lib.storage_authority_sql.adapters.postgres import PostgresSession

    command(sql_backend, 'model_routing.secret.put', secret_payload())
    entered, release, selected = threading.Event(), threading.Event(), threading.Event()
    fetch_all = PostgresSession.fetch_all

    def observed_page(session, sql, parameters=()):
        rows = fetch_all(session, sql, parameters)
        if sql.startswith('SELECT secret_reference, updated_at FROM storage_model_routing_secrets'):
            assert rows[0]['updated_at'] == 1.0
            selected.set()
        return rows

    monkeypatch.setattr(PostgresSession, 'fetch_all', observed_page)

    def refresh(session):
        _model_routing._model_routing_secret_put(session, secret_payload(updated_at=200.0))
        entered.set()
        assert release.wait(10)

    with ThreadPoolExecutor(max_workers=3) as executor:
        refreshing = executor.submit(direct, sql_backend, refresh)
        assert entered.wait(5)
        cleaning = executor.submit(command, sql_backend, 'model_routing.secret.prune', {
            **OWNER, 'active_secret_references': [], 'updated_before': 100.0,
        })
        try:
            assert selected.wait(5)
            # Cleanup cannot serialize independent writes of the same owner.
            executor.submit(command, sql_backend, 'model_routing.secret.put',
                            secret_payload(reference='independent', updated_at=200.0)).result(timeout=5)
        finally:
            release.set()
        refreshing.result(timeout=10)
        assert cleaning.result(timeout=10)['count'] == 0
    assert TransactionClient(sql_backend).query('model_routing.secret.get', {
        **OWNER, 'secret_reference': REFERENCE})['updated_at'] == 200.0

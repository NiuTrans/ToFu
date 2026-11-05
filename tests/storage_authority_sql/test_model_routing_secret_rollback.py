"""Cleanup remains atomic when failure follows a candidate deletion."""
import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.operations_pkg import _model_routing
from tests.storage_authority_sql.test_model_routing_secret_fences import (
    sql_backend as sql_backend, command, direct, secret_payload, OWNER,
)
from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('cleanup', ['delete', 'prune'])
def test_cleanup_failure_rolls_back_every_deleted_secret(sql_backend, cleanup):
    for reference in ('first', 'second'):
        command(sql_backend, 'model_routing.secret.put', secret_payload(reference=reference))

    def fail_after_delete(session):
        if cleanup == 'delete':
            result = _model_routing._model_routing_secret_delete(
                session, {**OWNER, 'secret_reference': 'first'})
            assert result['deleted'] is True
        else:
            result = _model_routing._model_routing_secret_prune(session, {
                **OWNER, 'active_secret_references': [], 'updated_before': 100.0,
            })
            assert result['count'] == 2
        raise StorageError('database_integrity', 'injected cleanup transaction failure')

    with pytest.raises(StorageError, match='injected cleanup transaction failure'):
        direct(sql_backend, fail_after_delete)
    client = TransactionClient(sql_backend)
    for reference in ('first', 'second'):
        assert client.query('model_routing.secret.get', {
            **OWNER, 'secret_reference': reference})['ciphertext'] == 'fixture-ciphertext'

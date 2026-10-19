"""Secret counts do not admit demand; cleanup reaches beyond active prefixes."""
import json
import sqlite3

import pytest

from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
from lib.storage_authority_sql.adapters.sqlite import SQLiteSession
from lib.storage_authority_sql.operations_pkg._model_routing import (
    _model_routing_secret_put, _model_routing_secret_get,
    _model_routing_secret_list, _model_routing_secret_prune,
)
from lib.storage_authority_sql.schema import initialize_schema

pytestmark = pytest.mark.unit


def test_memory_secret_demand_preserves_late_update_and_owner_scope():
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary(7, 'workspace')
    for index in range(2105):
        repository.put_secret(boundary, 'fixture-value', secret_reference=f'secret-{index:04}')
    repository.put_secret(boundary, 'replacement', secret_reference='secret-2104')
    assert repository.resolve_secret(boundary, 'secret-2104') == 'replacement'
    foreign = OwnerBoundary(8, 'workspace')
    repository.put_secret(foreign, 'foreign', secret_reference='secret-2104')
    assert repository.resolve_secret(foreign, 'secret-2104') == 'foreign'
    assert repository.resolve_secret(boundary, 'secret-2104') == 'replacement'


def test_sql_secret_demand_and_cleanup_preserve_full_active_set():
    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    try:
        initialize_schema(session)
        boundary = {'owner_user_id': 7, 'tenant_id': 'workspace'}
        def put(index, **overrides):
            return _model_routing_secret_put(session, dict(boundary,
                secret_reference=f'secret-{index:04}', ciphertext='fixture-sealed',
                key_hint='hint', updated_at=float(index + 1), **overrides))
        for index in range(2105):
            put(index)
        rows = _model_routing_secret_list(session, boundary)
        assert len(rows) == 2105
        assert rows[-1]['secret_reference'] == 'secret-2104'
        assert 'ciphertext' not in json.dumps(rows)
        foreign = dict(boundary, owner_user_id=8)
        _model_routing_secret_put(session, dict(foreign, secret_reference='secret-2104',
            ciphertext='foreign-sealed', key_hint='foreign', updated_at=1.0))
        assert len(_model_routing_secret_list(session, foreign)) == 1
        active = [f'secret-{index:04}' for index in range(1800)]
        prune = dict(boundary, active_secret_references=active, updated_before=3000.0)
        first = _model_routing_secret_prune(session, prune)
        assert first['count'] == 256
        assert first['removed'][0] == 'secret-1800'
        second = _model_routing_secret_prune(session, prune)
        assert second['count'] == 49
        assert second['removed'][-1] == 'secret-2104'
        assert _model_routing_secret_prune(session, prune)['count'] == 0
        assert len(_model_routing_secret_list(session, boundary)) == 1800
        assert _model_routing_secret_get(session, dict(foreign, secret_reference='secret-2104'))['ciphertext'] == 'foreign-sealed'
    finally:
        connection.close()

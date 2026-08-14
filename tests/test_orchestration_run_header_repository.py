"""Durable-run header query stability contracts."""

from __future__ import annotations

import pytest

from lib.orchestration.run_header_repository import (
    OrchestrationRunHeaderRepository,
)
from lib.orchestration.run_store_port import OrchestrationRunStoreError


pytestmark = pytest.mark.unit


class _Database:
    def __init__(self):
        self.sql = ''
        self.params = ()

    def execute(self, sql, params):
        self.sql = sql
        self.params = params
        return self

    def fetchall(self):
        return []


def test_run_list_has_a_stable_tie_breaker_and_bounded_filters():
    database = _Database()
    repository = OrchestrationRunHeaderRepository(
        lambda: database, lambda: 1)

    assert repository.list(
        status='running', orch_id='flow-1', limit=999) == []
    assert 'WHERE status=? AND orch_id=?' in database.sql
    assert 'ORDER BY created_at DESC, id DESC LIMIT 200' in database.sql
    assert database.params == ('running', 'flow-1')


class _CorruptHeaderDatabase:
    def execute(self, _sql, _params):
        return self

    def fetchone(self):
        return {
            'id': 'bad', 'orch_id': '', 'name': '',
            'definition': '{broken', 'input': '', 'status': 'running',
            'final': '', 'error': '', 'created_by': '',
            'created_at': 0, 'updated_at': 0, 'finished_at': 0,
        }


def test_corrupt_definition_is_not_mislabeled_as_a_successful_read():
    repository = OrchestrationRunHeaderRepository(
        lambda: _CorruptHeaderDatabase(), lambda: 1)

    with pytest.raises(OrchestrationRunStoreError, match='decode'):
        repository.get('bad')

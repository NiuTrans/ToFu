"""Durable event replay integrity contracts."""

from __future__ import annotations

import json
import sqlite3

import pytest

from lib.orchestration.run_event_repository import (
    OrchestrationRunEventRepository,
)
from lib.orchestration.database_run_store import (
    DatabaseOrchestrationRunStore,
)
from lib.orchestration.run_store_port import (
    ORCHESTRATION_RUN_EVENT_PAGE_LIMIT,
    OrchestrationRunStoreError,
)


pytestmark = pytest.mark.unit


class _ReplayDatabase:
    def __init__(self, payload):
        self.payload = payload

    def execute(self, _sql, _params):
        return self

    def fetchall(self):
        return [{'seq': 0, 'payload': self.payload, 'next_cursor': 1}]


@pytest.mark.parametrize('payload', ['{broken', '[]', 'null'])
def test_corrupt_event_never_advances_replay_as_if_it_were_valid(payload):
    repository = OrchestrationRunEventRepository(
        lambda: _ReplayDatabase(payload), lambda: 1)

    with pytest.raises(OrchestrationRunStoreError):
        repository.page('run', 0)


def test_repository_bounds_pages_and_advances_by_last_delivered_sequence():
    database = sqlite3.connect(':memory:')
    database.row_factory = sqlite3.Row
    database.execute(
        'CREATE TABLE orchestration_run_events ('
        'run_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL)'
    )
    database.executemany(
        'INSERT INTO orchestration_run_events (run_id, seq, payload) '
        'VALUES (?, ?, ?)',
        [
            ('run', sequence, json.dumps({'type': 'step_delta'}))
            for sequence in range(ORCHESTRATION_RUN_EVENT_PAGE_LIMIT + 1)
        ],
    )
    repository = OrchestrationRunEventRepository(
        lambda: database, lambda: 1)

    first_page = repository.page('run', 0)
    first, next_cursor, reset = first_page
    assert len(first) == ORCHESTRATION_RUN_EVENT_PAGE_LIMIT
    assert next_cursor == ORCHESTRATION_RUN_EVENT_PAGE_LIMIT
    assert reset is False
    assert first_page.caught_up is False

    final_page = repository.page('run', next_cursor)
    final, boundary, reset = final_page
    assert len(final) == 1
    assert boundary == ORCHESTRATION_RUN_EVENT_PAGE_LIMIT + 1
    assert reset is False
    assert final_page.caught_up is True

    store = DatabaseOrchestrationRunStore(lambda: database, lambda: 1)
    all_events = store.get_events('run', 0)
    assert len(all_events) == ORCHESTRATION_RUN_EVENT_PAGE_LIMIT + 1
    assert all_events[-1]['seq'] == ORCHESTRATION_RUN_EVENT_PAGE_LIMIT

    database.execute(
        'DELETE FROM orchestration_run_events WHERE run_id=? AND seq=?',
        ('run', ORCHESTRATION_RUN_EVENT_PAGE_LIMIT),
    )
    exact_page = repository.page('run', 0)
    assert len(exact_page.events) == ORCHESTRATION_RUN_EVENT_PAGE_LIMIT
    assert exact_page.caught_up is True

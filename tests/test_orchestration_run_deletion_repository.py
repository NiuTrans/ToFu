"""Atomic durable-run aggregate deletion coverage."""

from __future__ import annotations

import sqlite3

import pytest

from lib.orchestration.database_run_store import (
    DatabaseOrchestrationRunStore,
)


pytestmark = pytest.mark.unit


def _database() -> sqlite3.Connection:
    database = sqlite3.connect(':memory:')
    database.execute(
        'CREATE TABLE orchestration_runs (id TEXT PRIMARY KEY)')
    database.execute(
        'CREATE TABLE orchestration_run_events ('
        'run_id TEXT NOT NULL, seq INTEGER NOT NULL)')
    database.execute('INSERT INTO orchestration_runs (id) VALUES (?)', ('run',))
    database.execute(
        'INSERT INTO orchestration_run_events (run_id, seq) VALUES (?, ?)',
        ('run', 0),
    )
    database.commit()
    return database


def _count(database: sqlite3.Connection, table: str) -> int:
    return int(database.execute(
        f'SELECT COUNT(*) FROM {table}').fetchone()[0])


def test_database_store_deletes_header_and_events_as_one_aggregate():
    database = _database()
    store = DatabaseOrchestrationRunStore(lambda: database)

    assert store.delete_run('run') is True
    assert _count(database, 'orchestration_runs') == 0
    assert _count(database, 'orchestration_run_events') == 0


def test_header_delete_failure_rolls_back_event_deletion():
    database = _database()
    database.execute(
        "CREATE TRIGGER reject_run_delete BEFORE DELETE ON "
        "orchestration_runs BEGIN SELECT RAISE(ABORT, 'injected'); END")
    database.commit()
    store = DatabaseOrchestrationRunStore(lambda: database)

    assert store.delete_run('run') is False
    assert _count(database, 'orchestration_runs') == 1
    assert _count(database, 'orchestration_run_events') == 1

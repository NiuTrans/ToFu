"""Atomic durable event/run-header repository contracts."""

from __future__ import annotations

import sqlite3

import pytest

from lib.orchestration.run_projection_repository import (
    OrchestrationRunProjectionRepository,
)


pytestmark = pytest.mark.unit


def _database(*, broken_header: bool = False) -> sqlite3.Connection:
    db = sqlite3.connect(':memory:')
    header_columns = (
        'id TEXT PRIMARY KEY, status TEXT NOT NULL'
        if broken_header else
        'id TEXT PRIMARY KEY, status TEXT NOT NULL, '
        'updated_at INTEGER NOT NULL, finished_at INTEGER NOT NULL'
    )
    db.execute(f'CREATE TABLE orchestration_runs ({header_columns})')
    db.execute(
        'CREATE TABLE orchestration_run_events ('
        'run_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL, '
        'node_id TEXT NOT NULL, payload TEXT NOT NULL, ts INTEGER NOT NULL, '
        'PRIMARY KEY (run_id, seq))'
    )
    return db


def _seed_run(db: sqlite3.Connection, status: str = 'pending') -> None:
    columns = {
        row[1] for row in db.execute(
            'PRAGMA table_info(orchestration_runs)').fetchall()
    }
    if 'updated_at' in columns:
        db.execute(
            'INSERT INTO orchestration_runs '
            '(id, status, updated_at, finished_at) VALUES (?, ?, 1, 0)',
            ('run-1', status),
        )
    else:
        db.execute(
            'INSERT INTO orchestration_runs (id, status) VALUES (?, ?)',
            ('run-1', status),
        )
    db.commit()


def test_event_and_nonterminal_header_commit_in_one_projection():
    db = _database()
    _seed_run(db)
    repository = OrchestrationRunProjectionRepository(lambda: db, lambda: 42)

    assert repository.project(
        'run-1', 3, {'type': 'human_request'}, 'paused') is True

    event = db.execute(
        'SELECT seq, type, ts FROM orchestration_run_events').fetchone()
    header = db.execute(
        'SELECT status, updated_at, finished_at FROM orchestration_runs'
    ).fetchone()
    assert event == (3, 'human_request', 42)
    assert header == ('paused', 42, 0)


def test_every_event_projection_is_fenced_by_the_active_header():
    db = _database()
    _seed_run(db, 'done')
    repository = OrchestrationRunProjectionRepository(lambda: db, lambda: 42)

    assert repository.project(
        'run-1', 4, {'type': 'step_trace'}) is False
    assert db.execute(
        'SELECT COUNT(*) FROM orchestration_run_events').fetchone()[0] == 0


def test_header_write_failure_rolls_back_the_event_insert():
    db = _database(broken_header=True)
    _seed_run(db)
    repository = OrchestrationRunProjectionRepository(lambda: db, lambda: 42)

    assert repository.project(
        'run-1', 5, {'type': 'human_request'}, 'paused') is False
    assert db.execute(
        'SELECT COUNT(*) FROM orchestration_run_events').fetchone()[0] == 0
    assert db.execute(
        'SELECT status FROM orchestration_runs').fetchone()[0] == 'pending'


def test_projection_rejects_terminal_or_unknown_status_before_writing():
    db = _database()
    _seed_run(db)
    repository = OrchestrationRunProjectionRepository(lambda: db, lambda: 42)

    assert repository.project(
        'run-1', 1, {'type': 'flow_complete'}, 'done') is False
    assert repository.project(
        'run-1', 2, {'type': 'flow_start'}, 'future') is False
    assert db.execute(
        'SELECT COUNT(*) FROM orchestration_run_events').fetchone()[0] == 0


def test_identical_retry_does_not_reapply_an_old_header_transition():
    db = _database()
    _seed_run(db)
    repository = OrchestrationRunProjectionRepository(lambda: db, lambda: 42)
    event = {'type': 'human_request', 'request_id': 'gate-1'}

    assert repository.project('run-1', 6, event, 'paused') is True
    db.execute(
        'UPDATE orchestration_runs SET status=?, updated_at=? WHERE id=?',
        ('running', 50, 'run-1'),
    )
    db.commit()

    assert repository.project('run-1', 6, event, 'paused') is True
    assert (
        db.execute(
            'SELECT status, updated_at FROM orchestration_runs').fetchone()
        == ('running', 50)
    )


def test_conflicting_retry_is_rejected_without_header_side_effects():
    db = _database()
    _seed_run(db)
    repository = OrchestrationRunProjectionRepository(lambda: db, lambda: 42)

    assert repository.project(
        'run-1', 7, {'type': 'step_trace', 'output': 'first'}) is True
    assert repository.project(
        'run-1', 7, {'type': 'step_trace', 'output': 'other'},
        'paused',
    ) is False

    payload = db.execute(
        'SELECT payload FROM orchestration_run_events '
        'WHERE run_id=? AND seq=?', ('run-1', 7),
    ).fetchone()[0]
    assert 'first' in payload
    assert db.execute(
        'SELECT status FROM orchestration_runs').fetchone()[0] == 'pending'

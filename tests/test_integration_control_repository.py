"""Transaction/concurrency contracts for integration-control storage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from lib.database import integration_control_repository as repository


pytestmark = pytest.mark.unit


def _ready(path: Path, task_id: str, *, now: float) -> None:
    repository.register_workspace(
        path, project_root='/project', task_id=task_id, title=task_id,
        workspace_path=f'/workspace/{task_id}', managed=False,
        base_sha='a' * 40, now=now)
    repository.save_checkpoint(
        path, project_root='/project', task_id=task_id,
        checkpoint_sha=(task_id[0] * 40), now=now + 0.1)
    repository.submit_checkpoint(
        path, project_root='/project', task_id=task_id, now=now + 0.2)


def test_concurrent_claim_allows_one_integrating_task_per_project(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'control.sqlite3'
    _ready(path, 'alpha', now=1.0)
    _ready(path, 'beta', now=2.0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(
            lambda index: repository.claim_next(path, now=10.0 + index / 100),
            range(8)))

    winners = [row for row in claimed if row is not None]
    assert len(winners) == 1
    rows, _events = repository.status_rows(path, '/project')
    assert sum(row['state'] == 'integrating' for row in rows) == 1
    assert sum(row['state'] == 'ready' for row in rows) == 1


def test_register_rolls_back_workspace_when_event_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / 'control.sqlite3'

    def fail_event(*_args, **_kwargs):
        raise RuntimeError('injected event failure')

    monkeypatch.setattr(repository, '_event', fail_event)
    with pytest.raises(RuntimeError, match='injected event failure'):
        repository.register_workspace(
            path, project_root='/project', task_id='rollback', title='',
            workspace_path='/workspace/rollback', managed=False,
            base_sha='a' * 40, now=1.0)

    rows, events = repository.status_rows(path, '/project')
    assert rows == []
    assert events == []


def test_terminal_transition_is_compare_and_set(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'control.sqlite3'
    _ready(path, 'alpha', now=1.0)
    rows, _events = repository.status_rows(path, '/project')
    row = rows[0]

    assert repository.mark_merged(
        path, row_id=row['id'], project_root='/project', task_id='alpha',
        candidate_sha='c' * 40, now=3.0) is False
    rows, events = repository.status_rows(path, '/project')
    assert rows[0]['state'] == 'ready'
    assert all(event['kind'] != 'merged' for event in events)


def test_pre_repository_schema_is_upgraded_without_losing_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'control.sqlite3'
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE integration_workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_root TEXT NOT NULL,
            task_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            workspace_path TEXT NOT NULL,
            managed INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'running',
            base_sha TEXT NOT NULL DEFAULT '',
            checkpoint_sha TEXT NOT NULL DEFAULT '',
            candidate_sha TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(project_root, task_id)
        );
        CREATE TABLE integration_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_root TEXT NOT NULL,
            task_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
        INSERT INTO integration_workspaces(
            project_root, task_id, workspace_path, created_at, updated_at
        ) VALUES('/project', 'legacy', '/workspace/legacy', 1, 1);
    ''')
    db.commit()
    db.close()

    repository.initialize_store(path)

    rows, _events = repository.status_rows(path, '/project')
    assert [row['task_id'] for row in rows] == ['legacy']

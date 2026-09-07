"""Transaction/concurrency contracts for integration-control storage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from lib import integration_state_repository as repository
from lib.storage import StorageSupervisor
from lib.storage.errors import StorageError
from lib.storage.runtime import StorageRuntime
from lib.storage.service import install_runtime_for_test


pytestmark = pytest.mark.unit


@pytest.fixture
def sidecar_factory(tmp_path: Path):
    def start() -> None:
        supervisor = StorageSupervisor(
            project_root=tmp_path, backend='sqlite', startup_timeout=60)
        runtime = StorageRuntime(supervisor=supervisor, auto_restart=False)
        install_runtime_for_test(runtime)
        runtime.start()

    try:
        yield tmp_path, start
    finally:
        install_runtime_for_test(None)


def _ready(task_id: str, *, now: float, user_id: int = 1) -> None:
    repository.register_workspace(
        user_id=user_id, project_root='/project', task_id=task_id, title=task_id,
        workspace_path=f'/workspace/{task_id}', managed=False,
        base_sha='a' * 40, now=now)
    repository.save_checkpoint(
        user_id=user_id, project_root='/project', task_id=task_id,
        checkpoint_sha=(task_id[0] * 40), now=now + 0.1)
    repository.submit_checkpoint(
        user_id=user_id, project_root='/project', task_id=task_id, now=now + 0.2)


def test_concurrent_claim_allows_one_integrating_task_per_project(
    sidecar_factory,
) -> None:
    _root, start = sidecar_factory
    start()
    _ready('alpha', now=1.0)
    _ready('beta', now=2.0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(
            lambda index: repository.claim_next(now=10.0 + index / 100),
            range(8)))

    winners = [row for row in claimed if row is not None]
    assert len(winners) == 1
    rows, _events = repository.status_rows('/project', user_id=1)
    assert sum(row['state'] == 'integrating' for row in rows) == 1
    assert sum(row['state'] == 'ready' for row in rows) == 1


def test_register_rolls_back_workspace_when_event_write_fails(
    sidecar_factory, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('TOFU_STORAGE_ENABLE_FAULT_INJECTION', '1')
    monkeypatch.setenv(
        'TOFU_STORAGE_FAULT_ONCE', 'integration.after_workspace_mutation')
    _root, start = sidecar_factory
    start()

    with pytest.raises(StorageError) as captured:
        repository.register_workspace(
            user_id=1, project_root='/project', task_id='rollback', title='',
            workspace_path='/workspace/rollback', managed=False,
            base_sha='a' * 40, now=1.0)
    assert captured.value.code == 'database_internal'

    rows, events = repository.status_rows('/project', user_id=1)
    assert rows == []
    assert events == []


def test_terminal_transition_is_compare_and_set(
    sidecar_factory,
) -> None:
    _root, start = sidecar_factory
    start()
    _ready('alpha', now=1.0)
    rows, _events = repository.status_rows('/project', user_id=1)
    row = rows[0]

    assert repository.mark_merged(
        row_id=row['id'], candidate_sha='c' * 40, now=3.0) is False
    rows, events = repository.status_rows('/project', user_id=1)
    assert rows[0]['state'] == 'ready'
    assert all(event['kind'] != 'merged' for event in events)


def test_pre_repository_schema_is_upgraded_without_losing_rows(
    sidecar_factory,
) -> None:
    root, start = sidecar_factory
    data = root / 'data'
    data.mkdir()
    authority = data / 'tofu.db'
    db = sqlite3.connect(authority)
    db.executescript('''
        CREATE TABLE storage_meta (
            meta_key TEXT PRIMARY KEY,
            meta_value TEXT NOT NULL
        );
        INSERT INTO storage_meta(meta_key, meta_value)
        VALUES('schema_version', '2');
        CREATE TABLE integration_workspaces (
            id BIGINT PRIMARY KEY,
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
            id BIGINT PRIMARY KEY,
            project_root TEXT NOT NULL,
            task_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        );
        INSERT INTO integration_workspaces(
            id, project_root, task_id, workspace_path, created_at, updated_at
        ) VALUES(1, '/project', 'legacy', '/workspace/legacy', 1, 1);
    ''')
    db.commit()
    db.close()

    start()
    repository.initialize_store()

    rows, _events = repository.status_rows('/project', user_id=1)
    assert [row['task_id'] for row in rows] == ['legacy']


def test_peek_ready_reads_without_claiming_or_sweeping(
    sidecar_factory,
) -> None:
    _root, start = sidecar_factory
    start()
    assert repository.peek_ready() is None
    _ready('alpha', now=1.0)

    peeked = repository.peek_ready()
    assert peeked is not None and peeked['task_id'] == 'alpha'
    # The probe is read-only: the row stays ready and claimable afterwards.
    rows, _events = repository.status_rows('/project', user_id=1)
    assert rows[0]['state'] == 'ready'

    claimed = repository.claim_next(now=10.0)
    assert claimed is not None and claimed['task_id'] == 'alpha'
    assert repository.peek_ready() is None
    # The worker supplies its clock: stale recovery becomes visible through
    # the read pool without running the mutating claim sweep on every poll.
    stale = repository.peek_ready(now=671.0)
    assert stale is not None and stale['task_id'] == 'alpha'


def test_receipt_replay_does_not_duplicate_registration_event(
    sidecar_factory,
) -> None:
    _root, start = sidecar_factory
    start()
    arguments = {
        'user_id': 1, 'project_root': '/project', 'task_id': 'same',
        'title': 'Same',
        'workspace_path': '/workspace/same', 'managed': False,
        'base_sha': 'a' * 40, 'now': 1.0,
    }
    repository.register_workspace(**arguments)
    repository.register_workspace(**arguments)

    rows, events = repository.status_rows('/project', user_id=1)
    assert [row['task_id'] for row in rows] == ['same']
    assert [event['kind'] for event in events] == ['registered']


def test_same_project_and_task_id_are_isolated_by_owner(sidecar_factory) -> None:
    _root, start = sidecar_factory
    start()
    for user_id in (1, 2):
        repository.register_workspace(
            user_id=user_id,
            project_root='/shared-project',
            task_id='same-task',
            title=f'owner-{user_id}',
            workspace_path=f'/workspace/owner-{user_id}',
            managed=False,
            base_sha='a' * 40,
            now=float(user_id),
        )

    owner_one, events_one = repository.status_rows(
        '/shared-project', user_id=1)
    owner_two, events_two = repository.status_rows(
        '/shared-project', user_id=2)

    assert [row['title'] for row in owner_one] == ['owner-1']
    assert [row['title'] for row in owner_two] == ['owner-2']
    assert {event['user_id'] for event in events_one} == {1}
    assert {event['user_id'] for event in events_two} == {2}

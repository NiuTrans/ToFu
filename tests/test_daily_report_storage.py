"""Durability and concurrency contracts for daily-report JSON storage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest

from lib.daily_report import storage
from lib.daily_report import scheduler
from lib.identity import PrincipalContext

pytestmark = pytest.mark.unit
OWNER_USER_ID = 41


@pytest.fixture
def report_store(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, '_REPORTS_DIR', str(tmp_path))
    monkeypatch.setattr(
        storage, '_invalidate_calendar',
        lambda _date, *, owner_user_id: None)
    return tmp_path


@pytest.mark.parametrize('date_str', [
    '../secrets',
    '2026-08-13/../../secrets',
    '2026-8-13',
    '2026-02-30',
    '',
])
def test_report_path_rejects_noncanonical_or_traversing_dates(
        report_store, date_str):
    with pytest.raises(ValueError):
        storage._report_path(date_str, owner_user_id=OWNER_USER_ID)


def test_failed_atomic_replace_preserves_old_report_and_raises(report_store):
    date_str = '2026-08-13'
    storage._save_report(
        date_str, {'streams': [{'id': 'old'}]},
        owner_user_id=OWNER_USER_ID)
    path = report_store / str(OWNER_USER_ID) / f'{date_str}.json'
    before = path.read_bytes()

    with mock.patch('lib.json_store.os.replace',
                    side_effect=OSError('injected disk failure')):
        with pytest.raises(OSError, match='injected disk failure'):
            storage._save_report(
                date_str, {'streams': [{'id': 'new'}]},
                owner_user_id=OWNER_USER_ID)

    assert path.read_bytes() == before
    assert storage._load_report(
        date_str, owner_user_id=OWNER_USER_ID)['streams'][0]['id'] == 'old'
    assert not list(report_store.rglob('.jsonstore-*.tmp'))


def test_concurrent_report_mutations_do_not_lose_updates(report_store):
    date_str = '2026-08-13'
    storage._save_report(
        date_str, {'streams': [], 'tomorrow': []},
        owner_user_id=OWNER_USER_ID)

    def add_todo(index):
        def mutate(report):
            report['tomorrow'].append({
                'id': f'todo-{index}', 'text': str(index), 'done': False,
                '_manual': True,
            })
            return report

        storage._update_report(
            date_str, mutate, owner_user_id=OWNER_USER_ID)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_todo, range(32)))

    stored = storage._load_report(
        date_str, owner_user_id=OWNER_USER_ID)
    assert {item['id'] for item in stored['tomorrow']} == {
        f'todo-{index}' for index in range(32)
    }


def test_generated_commit_merges_latest_manual_state(report_store):
    """Edits made after analysis began survive its eventual commit."""
    date_str = '2026-08-13'
    analysis_result = {
        'ok': True,
        'streams': [{
            'id': 'stream-new', 'title': 'Ship parser',
            'status': 'in_progress', 'conv_ids': ['conv-1'],
        }],
        'tomorrow': [],
        'tasks': [],
    }

    # This edit represents user activity that happens while the LLM is still
    # working with its already-created ``analysis_result``.
    storage._save_report(date_str, {
        'streams': [{
            'id': 'stream-old', 'title': 'Old title', 'status': 'blocked',
            'conv_ids': ['conv-1'], '_manual': True,
        }],
        'tomorrow': [{
            'id': 'todo-manual', 'text': 'Write release notes',
            'done': False, '_manual': True,
        }],
        'tasks': [],
    }, owner_user_id=OWNER_USER_ID)

    stored = storage._save_generated_report(
        date_str, analysis_result, owner_user_id=OWNER_USER_ID)

    assert stored['streams'][0]['status'] == 'blocked'
    assert stored['streams'][0]['_manual'] is True
    assert [item['id'] for item in stored['tomorrow']] == ['todo-manual']
    # Storage must not add metadata/manual state back into the caller's object.
    assert analysis_result['streams'][0]['status'] == 'in_progress'
    assert analysis_result['tomorrow'] == []
    assert 'date' not in analysis_result


def test_mutation_refuses_to_replace_corrupt_existing_report(report_store):
    date_str = '2026-08-13'
    path = report_store / str(OWNER_USER_ID) / f'{date_str}.json'
    path.parent.mkdir(parents=True)
    corrupt = b'{"streams": ['
    path.write_bytes(corrupt)

    with pytest.raises(storage.JsonStoreReadError):
        storage._update_report(
            date_str, lambda _report: {'streams': []},
            owner_user_id=OWNER_USER_ID)

    assert path.read_bytes() == corrupt


def test_report_storage_and_jobs_are_owner_scoped(report_store):
    date_str = '2026-08-13'
    other_owner = OWNER_USER_ID + 1
    storage._save_report(
        date_str, {'streams': [{'id': 'owner-a'}]},
        owner_user_id=OWNER_USER_ID)
    storage._save_report(
        date_str, {'streams': [{'id': 'owner-b'}]},
        owner_user_id=other_owner)

    assert storage._load_report(
        date_str, owner_user_id=OWNER_USER_ID)['streams'][0]['id'] == 'owner-a'
    assert storage._load_report(
        date_str, owner_user_id=other_owner)['streams'][0]['id'] == 'owner-b'

    storage._update_job(
        date_str, 'generating', owner_user_id=OWNER_USER_ID)
    assert storage._get_job(
        date_str, owner_user_id=OWNER_USER_ID)['status'] == 'generating'
    assert storage._get_job(date_str, owner_user_id=other_owner) is None


def test_report_storage_requires_explicit_positive_owner(report_store):
    with pytest.raises(TypeError):
        storage._load_report('2026-08-13')
    with pytest.raises(ValueError, match='positive user_id'):
        storage._load_report('2026-08-13', owner_user_id=0)


def test_scheduler_requires_scoped_owning_principal():
    principal = PrincipalContext.system(
        subject_id='daily-report-test',
        owner_user_id=OWNER_USER_ID,
        scopes={'reports:maintain'},
    )
    assert scheduler._scheduler_owner(principal) == OWNER_USER_ID

    with pytest.raises(PermissionError, match='lacks scope'):
        scheduler._scheduler_owner(PrincipalContext.system(
            subject_id='unscoped-test',
            owner_user_id=OWNER_USER_ID,
            scopes=set(),
        ))
    with pytest.raises(PermissionError, match='owning user'):
        scheduler._scheduler_owner(PrincipalContext.system(
            subject_id='ownerless-test',
            scopes={'reports:maintain'},
        ))


def test_route_threads_authenticated_owner_to_report_storage():
    import asyncio

    from quart import Quart, g
    from routes.api_v1 import daily_report as daily_report_routes

    async def scenario():
        app = Quart(__name__)
        principal = PrincipalContext.user(
            subject_id='daily-report-route-test',
            owner_user_id=OWNER_USER_ID,
            scopes={'reports:read'},
        )
        inherited = mock.Mock(return_value=[])
        load = mock.Mock(return_value={'streams': [], 'tasks': []})
        handler = daily_report_routes.get_cached_report.__wrapped__
        async with app.test_request_context(
                '/api/v1/daily-report/2026-08-13'):
            g.principal_context = principal
            with mock.patch.object(
                    daily_report_routes, '_get_today_inherited_todos',
                    inherited), mock.patch.object(
                        daily_report_routes, '_load_report', load):
                response, status = await handler('2026-08-13')

        assert status == 200
        assert (await response.get_json())['ok'] is True
        inherited.assert_called_once_with(
            '2026-08-13', owner_user_id=OWNER_USER_ID)
        load.assert_called_once_with(
            '2026-08-13', owner_user_id=OWNER_USER_ID)

    asyncio.run(scenario())


@pytest.mark.parametrize('date_str', ['../../outside', '2026-02-30'])
def test_todo_route_rejects_invalid_date_before_storage(date_str):
    import asyncio
    from quart import Quart
    from routes.api_v1 import daily_report as daily_report_routes

    async def scenario():
        app = Quart(__name__)
        # Unwrap require_auth and _db_safe: this test targets the handler's
        # input boundary; auth has separate integration coverage.
        handler = daily_report_routes.toggle_tomorrow_todo.__wrapped__.__wrapped__
        async with app.test_request_context(
                '/api/v1/daily-report/todo-toggle', method='PATCH', json={
                    'date': date_str, 'todo_id': 'todo-1', 'done': True,
                }):
            with mock.patch.object(
                    daily_report_routes, '_update_report',
                    side_effect=AssertionError('storage must not be reached')):
                response, status = await handler()
        assert status == 400
        assert (await response.get_json())['error']

    asyncio.run(scenario())

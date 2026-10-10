"""Startup recovery visits every bounded page without skipping owner scopes."""

import json
import sqlite3

import pytest

from lib.storage_sidecar.operations_pkg._project_brain import (
    _project_brain_recovery_snapshot,
)
from lib.storage.protocol import StorageError


pytestmark = pytest.mark.unit


class Session:
    def __init__(self, connection):
        self.connection = connection

    def fetch_all(self, sql, params=()):
        return self.connection.execute(sql, params).fetchall()


@pytest.fixture
def recovery_database():
    with sqlite3.connect(':memory:') as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            'CREATE TABLE storage_project_brain_projects ('
            'owner_user_id INTEGER, project_key TEXT, projection_json TEXT, '
            'updated_at_ms INTEGER, PRIMARY KEY(owner_user_id, project_key))')
        yield connection


@pytest.mark.parametrize('count', [1000, 1001, 2001])
def test_pages_include_inactive_projects_and_continue_after_settlement(
        recovery_database, count):
    db = recovery_database
    db.executemany(
        'INSERT INTO storage_project_brain_projects VALUES (?, ?, ?, ?)',
        [(1 + i // 1000, f'/project-{i:04}', json.dumps({'workItems': [
            {'id': str(i), 'taskId': str(i), 'status': 'active'}]}), 50)
         for i in range(count)])
    seen = []
    payload = {}
    while True:
        page = _project_brain_recovery_snapshot(Session(db), payload)
        assert len(page['projects']) <= 1000
        if not seen:
            assert page['capped'] is (count > 1000)
        for project in page['projects']:
            identity = (project['ownerUserId'], project['projectKey'])
            seen.append(identity)
            # Real settlement moves this row ahead of the page cursor.
            db.execute(
                'UPDATE storage_project_brain_projects SET updated_at_ms=100, '
                'projection_json=? WHERE owner_user_id=? AND project_key=?',
                (json.dumps({'workItems': []}), *identity))
        if not page['capped']:
            break
        payload = {'cursor': page['nextCursor']}
    assert len(seen) == len(set(seen)) == count


def test_recovery_deduplicates_acknowledged_work_without_aliasing_owners(monkeypatch):
    from lib.conversations import project_brain_startup as startup
    from lib.tasks_pkg import workspace_delivery as delivery

    queries, finishes, intents = [], [], []
    scopes = [(7, '/project-a'), (7, '/project-a'),
              (8, '/project-a'), (7, '/project-b')]

    class Client:
        def maintenance(self, operation, payload, **kwargs):
            return {'capped': False, 'projects': [
                {'ownerUserId': owner, 'projectKey': project, 'workItems': [
                    {'id': 'work', 'taskId': 'task', 'status': 'active'}]}
                for owner, project in scopes]}

        def query(self, operation, payload):
            queries.append(payload)
            return {'status': 'done', 'finishReason': 'stop'}

        def command(self, operation, payload, command_id):
            finishes.append((payload['owner_user_id'], payload['project_key'],
                             payload['work_id']))

    monkeypatch.setattr(startup, 'get_storage_client', lambda **kw: Client())
    monkeypatch.setattr(delivery, 'settle_task_workspaces',
                        lambda task, root: intents.append((task['_userId'], root)))

    assert startup.recover_active_work_items() == 3
    assert len(queries) == 3
    assert finishes == [(7, '/project-a', 'work'), (8, '/project-a', 'work'),
                        (7, '/project-b', 'work')]
    assert intents == [(7, '/project-a'), (8, '/project-a'), (7, '/project-b')]


def test_empty_active_page_still_has_continuation(recovery_database):
    db = recovery_database
    db.executemany(
        'INSERT INTO storage_project_brain_projects VALUES (1, ?, ?, 50)',
        [(f'/project-{i:04}', json.dumps({'workItems': []})) for i in range(1001)])
    first = _project_brain_recovery_snapshot(Session(db), {})
    assert first['projects'] == []
    assert first['capped'] is True
    last = _project_brain_recovery_snapshot(Session(db), {'cursor': first['nextCursor']})
    assert last == {'projects': [], 'capped': False}


@pytest.mark.parametrize('cursor', [[], {}, {'updatedAt': -1}, {
    'updatedAt': True, 'ownerUserId': 1, 'projectKey': '/project'}])
def test_invalid_cursor_fails_closed(recovery_database, cursor):
    with pytest.raises(StorageError):
        _project_brain_recovery_snapshot(Session(recovery_database), {'cursor': cursor})


def test_startup_continues_empty_pages_and_preserves_task_owner(monkeypatch):
    from lib.conversations import project_brain_startup as startup

    cursor = {'updatedAt': 50, 'ownerUserId': 7, 'projectKey': '/earlier'}
    requests, commands = [], []

    class Client:
        def maintenance(self, operation, payload, **kwargs):
            assert operation == 'project_brain.recovery.snapshot'
            requests.append(payload)
            if len(requests) == 1:
                return {'projects': [], 'capped': True, 'nextCursor': cursor}
            return {'projects': [{'ownerUserId': 7, 'projectKey': '/later',
                                 'workItems': [{'id': 'work', 'taskId': 'task', 'status': 'active'}]}],
                    'capped': False}

        def query(self, operation, payload):
            assert payload['user_id'] == 7
            return {'status': 'done', 'finishReason': 'stop'}

        def command(self, operation, payload, command_id):
            commands.append(payload)

    monkeypatch.setattr(startup, 'get_storage_client', lambda **kwargs: Client())
    assert startup.recover_active_work_items() == 1
    assert requests == [{}, {'cursor': cursor}]
    assert commands[0]['status'] == 'completed'
    assert commands[0]['owner_user_id'] == 7
    assert commands[0]['project_key'] == '/later'


@pytest.mark.parametrize('repeat', [False, True])
def test_startup_rejects_missing_or_repeated_continuation(monkeypatch, repeat):
    from lib.conversations import project_brain_startup as startup

    class Client:
        def maintenance(self, *args, **kwargs):
            result = {'projects': [], 'capped': True}
            if repeat:
                result['nextCursor'] = {
                    'updatedAt': 50, 'ownerUserId': 7, 'projectKey': '/same'}
            return result

    monkeypatch.setattr(startup, 'get_storage_client', lambda **kwargs: Client())
    with pytest.raises(RuntimeError, match='advancing cursor'):
        startup.recover_active_work_items()



def test_startup_ignores_terminal_history_from_older_native_snapshot(monkeypatch):
    from lib.conversations import project_brain_startup as startup

    queries, commands = [], []

    class Client:
        def maintenance(self, *args, **kwargs):
            history = [{'id': str(i), 'taskId': str(i), 'status': 'completed'}
                       for i in range(7088)]
            return {'projects': [{'ownerUserId': 7, 'projectKey': '/project',
                                  'workItems': history + [
                                      {'id': 'live', 'taskId': 'task', 'status': 'active'}]}],
                    'capped': False}

        def query(self, operation, payload):
            queries.append(payload)
            return {'status': 'done'}

        def command(self, operation, payload, command_id):
            commands.append(payload)

    monkeypatch.setattr(startup, 'get_storage_client', lambda **kwargs: Client())
    assert startup.recover_active_work_items() == 1
    assert len(queries) == len(commands) == 1
    assert queries[0]['key'] == 'task'
    assert commands[0]['work_id'] == 'live'


@pytest.mark.parametrize('failure', ['receipt', 'exception'])
def test_startup_preserves_active_work_until_intent_acknowledged(monkeypatch, failure):
    from lib.conversations import project_brain_startup as startup
    from lib.tasks_pkg import workspace_delivery as delivery

    active = {'pending': 'task-pending', 'healthy': 'task-healthy'}
    finishes, attempts = [], []
    unavailable = True

    class Client:
        def maintenance(self, operation, payload, **kwargs):
            return {'projects': [{'ownerUserId': 7, 'projectKey': '/project',
                                  'workItems': [{'id': work, 'taskId': task, 'status': 'active'}
                                                for work, task in active.items()]}],
                    'capped': False}

        def query(self, operation, payload):
            assert payload['user_id'] == 7
            return {'status': 'done', 'finishReason': 'stop'}

        def command(self, operation, payload, command_id):
            assert operation == 'project_brain.work.finish'
            finishes.append(payload)
            del active[payload['work_id']]

    def settle(task, root):
        attempts.append(task['id'])
        assert task['_userId'] == 7
        assert root == '/project'
        if task['id'] == 'task-pending' and unavailable:
            if failure == 'exception':
                raise RuntimeError('intent store unavailable')
            return {'errors': [{'detail': 'intent store unavailable'}]}
        return {'repositories': [{'status': 'intent_recorded'}]}

    monkeypatch.setattr(startup, 'get_storage_client', lambda **kwargs: Client())
    monkeypatch.setattr(delivery, 'settle_task_workspaces', settle)
    assert startup.recover_active_work_items() == 1
    assert active == {'pending': 'task-pending'}
    assert [item['work_id'] for item in finishes] == ['healthy']
    unavailable = False
    assert startup.recover_active_work_items() == 1
    assert active == {}
    assert attempts == ['task-pending', 'task-healthy', 'task-pending']
    assert finishes[-1]['status'] == 'completed'
    assert startup.recover_active_work_items() == 0


@pytest.mark.parametrize('stored,expected', [

    ({'status': 'done', 'metadata': {'finishReason': 'stop'}}, 'completed'),
    ({'status': 'done', 'metadata': {'finishReason': 'length'}}, 'failed'),
    ({'status': 'done', 'metadata': {'finishReason': 'stop', 'streamState': 'premature_close'}}, 'failed'),
    ({'status': 'done', 'metadata': {'finishReason': 'stop', 'streamState': 'unknown'}}, 'failed'),
    ({'status': 'done'}, 'failed'),
    ({'status': 'done', 'finishReason': 'length'}, 'failed'),
    ({'status': 'done', 'finishReason': 'premature_close'}, 'failed'),
    ({'status': 'done', 'finishReason': 'stop', 'streamState': 'premature_close'}, 'failed'),
    ({'status': 'done', 'finishReason': 'stop', 'streamState': 'unknown'}, 'failed'),
    ({'status': 'failed', 'finishReason': 'stop'}, 'failed'),
    ({'status': 'done', 'finishReason': 'stop'}, 'completed'),
    ({'status': 'done', 'finishReason': 'stop', 'aborted': True}, 'cancelled'),
])
def test_startup_does_not_synthesize_publication_authority(monkeypatch, stored, expected):
    from lib.conversations import project_brain_startup as startup
    from lib.tasks_pkg import workspace_delivery as delivery

    outcomes, finishes = [], []

    class Client:
        def maintenance(self, *args, **kwargs):
            return {'projects': [{'ownerUserId': 7, 'projectKey': '/project',
                                  'workItems': [{'id': 'work', 'taskId': 'task', 'status': 'active'}]}],
                    'capped': False}

        def query(self, operation, payload):
            assert payload['include_metadata'] is True
            assert payload['include_terminal_payload'] is False
            return dict(stored, config={'readOnlyPaths': ['/readonly']})

        def command(self, operation, payload, command_id):
            finishes.append(payload)

    def settle(task, root):
        assert task['config']['readOnlyPaths'] == ['/readonly']
        evidence = dict(stored.get('metadata') or {}, **stored)
        assert task.get('finishReason') == evidence.get('finishReason')
        assert task.get('streamState') == evidence.get('streamState')
        outcomes.append(delivery.task_outcome(task))
        return {'repositories': [{'status': 'intent_recorded'}]}

    monkeypatch.setattr(startup, 'get_storage_client', lambda **kwargs: Client())
    monkeypatch.setattr(delivery, 'settle_task_workspaces', settle)
    assert startup.recover_active_work_items() == 1
    assert outcomes == [expected]
    assert finishes[0]['status'] == expected

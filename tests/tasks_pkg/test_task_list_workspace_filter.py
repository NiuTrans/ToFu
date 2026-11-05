"""Owned workspace lookup filters before slicing, so old long tasks stay visible."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
from quart import Quart, g, request
import pytest
from lib.agent_core.task_runtime import TaskRuntime
from lib.api_keys import local_admin_context
from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4

pytestmark = pytest.mark.unit


def task_app(tasks):
    app = Quart(__name__)
    app.config['TESTING'] = True

    @app.before_request
    async def grant():
        context = local_admin_context()
        context.owner_user_id = int(request.headers.get('X-Test-User', '1'))
        g.auth_ctx = context
        g.rate_decision = None

    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(
        tasks.api_v1_tasks_bp, name='workspace_tasks_v4')))
    return app


def test_exact_metadata_filters_are_owned_and_precede_limit(monkeypatch):
    import routes.api_v1.tasks as tasks
    runtime = TaskRuntime('research-action')
    def create(identifier, owner, direction, lang):
        task = runtime.create(user_id=owner, task_id=identifier,
            meta={'direction':direction,'lang':lang,'action':'analyze'})
        runtime.mark_running(identifier)
        return task
    create('old-target',1,'Original long task','en')
    for n in range(6): create('other-'+str(n),1,'Different task','en')
    create('wrong-language',1,'Original long task','zh')
    create('foreign-target',2,'Original long task','en')
    monkeypatch.setattr(tasks,'_registries',lambda:{'research-action':runtime})
    app = task_app(tasks)
    async def query():
        path='/api/v4/tasks?kind=research-action&status=running&direction=Original%20long%20task&lang=en&limit=1'
        response=await app.test_client().get(path);assert response.status_code==200
        data=(await response.get_json())['data']
        assert data['total']==1 and [row['id'] for row in data['tasks']]==['old-target']
        response=await app.test_client().get(path,headers={'X-Test-User':'2'})
        assert [row['id'] for row in (await response.get_json())['data']['tasks']]==['foreign-target']
    try: asyncio.run(query())
    finally:
        for owner in (1,2):
            for task in runtime.snapshot_owned(user_id=owner):
                runtime.finish(task['id'],result=None);runtime.remove_owned(task['id'],user_id=owner)


@pytest.mark.parametrize('limit', [1, 4, 50])
def test_list_projects_only_the_owned_page_in_stable_timestamp_order(monkeypatch, limit):
    import routes.api_v1.tasks as tasks

    def row(identity, stamp, kind, owner=1, **extra):
        return {'id': identity, 'created_at': stamp, 'finished_at': 20,
                'kind': kind, 'status': 'done', '_userId': owner,
                'meta': {'topic': identity}, 'artifact_quality': None, **extra}

    research = [row('old', None, 'research-action'),
                row('first-tie', 7, 'research-action'),
                row('second-tie', 7, 'research-action', status='error',
                    error={'kind': 'provider_error', 'message': 'Unavailable'}),
                row('zero', 0, 'research-action'),
                row('foreign', 30, 'research-action', owner=2)]
    motion = [row('third-tie', 7, 'motion-video',
                  artifact_quality={'degraded': True, 'reason': 'Quality finding'}),
              row('newest', 9, 'motion-video'), row('negative', -1, '')]
    snapshots = [Mock(side_effect=lambda *, user_id, rows=rows:
                      [task for task in rows if task['_userId'] == user_id])
                 for rows in (research, motion)]
    failed = Mock(side_effect=RuntimeError('Optional runtime unavailable'))
    monkeypatch.setattr(tasks, '_registries', lambda: {
        'research-action': SimpleNamespace(snapshot_owned=snapshots[0]),
        'optional': SimpleNamespace(snapshot_owned=failed),
        'motion-video': SimpleNamespace(snapshot_owned=snapshots[1]),
    })
    project = tasks.project_work_unit
    projected = []

    def record_projection(task, **kwargs):
        projected.append(task['id'])
        return project(task, **kwargs)

    monkeypatch.setattr(tasks, 'project_work_unit', record_projection)
    app = task_app(tasks)

    async def query():
        response = await app.test_client().get(f'/api/v4/tasks?limit={limit}')
        assert response.status_code == 200
        return (await response.get_json())['data']

    payload = asyncio.run(query())
    expected = ['newest', 'first-tie', 'second-tie', 'third-tie',
                'old', 'zero', 'negative'][:limit]
    assert payload['total'] == 7
    assert [item['id'] for item in payload['tasks']] == expected
    assert sorted(projected) == sorted(expected)
    original = {task['id']: task for task in research + motion}
    for item in payload['tasks']:
        source = original[item['id']]
        assert item['created_at'] == source['created_at']
        assert item['artifact_quality'] == source['artifact_quality']
        assert item['kind'] == (source['kind'] or 'motion-video')
        unit = project(source, events=[], compact=True)
        assert item.get('workUnit') == unit
    for snapshot in [*snapshots, failed]:
        snapshot.assert_called_once_with(user_id=1)


def test_selected_task_projection_failure_is_not_hidden(monkeypatch):
    import routes.api_v1.tasks as tasks

    runtime = SimpleNamespace(snapshot_owned=lambda **kwargs: [
        {'id': 'selected', 'kind': 'motion-video', 'created_at': 1}])
    monkeypatch.setattr(tasks, '_registries', lambda: {'motion-video': runtime})
    monkeypatch.setattr(tasks, 'project_work_unit', Mock(side_effect=RuntimeError('Invalid selected projection')))
    app = task_app(tasks)

    async def query():
        with pytest.raises(RuntimeError, match='Invalid selected projection'):
            await app.test_client().get('/api/v4/tasks?limit=1')

    asyncio.run(query())


@pytest.mark.parametrize('stamps,expected', [
    ([None, 0, 1.5], ['task-2', 'task-0', 'task-1']),
    (['20', '3', '10'], ['task-1', 'task-0', 'task-2']),
])
def test_list_preserves_original_timestamp_values_and_comparison(monkeypatch, stamps, expected):
    import routes.api_v1.tasks as tasks
    rows = [{'id': f'task-{n}', 'kind': 'chat', 'created_at': stamp}
            for n, stamp in enumerate(stamps)]
    runtime = SimpleNamespace(snapshot_owned=lambda **kwargs: rows)
    monkeypatch.setattr(tasks, '_registries', lambda: {'chat': runtime})
    app = task_app(tasks)

    async def query():
        response = await app.test_client().get('/api/v4/tasks?limit=2')
        assert response.status_code == 200
        return (await response.get_json())['data']

    payload = asyncio.run(query())
    assert payload['total'] == 3
    assert [item['id'] for item in payload['tasks']] == expected[:2]
    assert [item['created_at'] for item in payload['tasks']] == [
        next(row['created_at'] for row in rows if row['id'] == identity)
        for identity in expected[:2]]


def test_incomparable_timestamps_still_propagate_invalid_registry_data(monkeypatch):
    import routes.api_v1.tasks as tasks
    runtime = SimpleNamespace(snapshot_owned=lambda **kwargs: [
        {'id': 'numeric', 'created_at': 1}, {'id': 'text', 'created_at': '2'}])
    monkeypatch.setattr(tasks, '_registries', lambda: {'chat': runtime})
    app = task_app(tasks)

    async def query():
        with pytest.raises(TypeError):
            await app.test_client().get('/api/v4/tasks?limit=1')

    asyncio.run(query())

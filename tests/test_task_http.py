"""HTTP contract tests shared by live and durable task replay routes."""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path

import pytest

import routes.task_http as task_http
from routes._task_routes import register_task_routes


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


class _Runtime:
    kind = 'factory-contract'

    def __init__(self):
        self.poll_cursors = []
        self.aborted = []

    def poll(self, task_id, *, cursor=0):
        self.poll_cursors.append((task_id, cursor))
        return {
            'format': 'tofu.task-replay/v1',
            'ok': True,
            'events': [],
            'next_cursor': cursor,
            'status': 'running',
            'done': False,
            'cursor': {'requested': cursor, 'next': cursor, 'reset': False},
        }

    def abort(self, task_id):
        self.aborted.append(task_id)
        return True

    def get(self, _task_id):
        return None


def _app():
    from quart import Quart

    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {
            **Quart.default_config, 'PROVIDE_AUTOMATIC_OPTIONS': True,
        }
    return Quart(__name__)


def test_task_replay_query_schema_and_parser_are_one_contract():
    assert task_http.task_replay_parameters() == [{
        'name': 'cursor',
        'in': 'query',
        'schema': {'type': 'integer', 'minimum': 0, 'default': 0},
        'description': 'Producer-owned next event sequence.',
    }]
    assert task_http.task_replay_cursor({'cursor': '12'}) == 12
    assert task_http.task_replay_cursor({'cursor': '-4'}) == 0
    assert task_http.task_replay_cursor({'cursor': 'invalid'}) == 0


def test_task_replay_response_uses_protocol_owned_status(monkeypatch):
    calls = []
    monkeypatch.setattr(
        task_http,
        'api_payload',
        lambda payload, status: calls.append((payload, status)) or
        (payload, status),
    )
    success = {
        'format': 'tofu.task-replay/v1',
        'ok': True,
        'events': [],
        'done': False,
    }
    missing = {
        'format': 'tofu.task-replay/v1',
        'ok': False,
        'events': [],
        'done': True,
        'error': 'not_found',
    }
    assert task_http.task_replay_response(success) == (success, 200)
    assert task_http.task_replay_response(missing) == (missing, 404)
    assert calls == [(success, 200), (missing, 404)]


def test_live_and_durable_adapters_delegate_to_shared_task_http():
    generic = (ROOT / 'routes/_task_routes.py').read_text()
    generic_api = (ROOT / 'routes/api_v1/tasks.py').read_text()
    durable = (ROOT / 'routes/api_v1/'
               'orchestration_task_http.py').read_text()
    runtime = (ROOT / 'routes/api_v1/'
               'orchestration_runtime_routes.py').read_text()
    mutation = (ROOT / 'routes/api_v1/'
                'orchestration_mutation_routes.py').read_text()

    assert generic.count('task_replay_cursor(request.args)') == 1
    assert generic.count('task_replay_response(resp)') == 1
    assert generic.count('parameters=task_replay_parameters()') == 1
    assert generic_api.count('task_replay_cursor(request.args)') == 2
    assert generic_api.count('parameters=_TASK_REPLAY_PARAMETERS') == 2
    assert 'int(request.args.get(\'cursor\')' not in generic_api
    assert 'safe_replay_cursor' not in generic + generic_api + durable
    assert 'task_replay_http_status' not in generic + generic_api + durable
    assert durable.count('durable_replay_parameters = task_replay_parameters') == 1
    assert durable.count('durable_replay_cursor = task_replay_cursor') == 1
    assert durable.count('return task_replay_response(payload)') == 1
    assert runtime.count('route_decorators=(require_auth,)') == 1
    assert mutation.count('route_decorators=(require_auth,)') == 1


def test_task_route_consumers_depend_on_shared_capability_ports():
    generic = (ROOT / 'routes/_task_routes.py').read_text()
    runtime = (ROOT / 'routes/api_v1/'
               'orchestration_runtime_routes.py').read_text()
    mutation_routes = (ROOT / 'routes/api_v1/'
                       'orchestration_mutation_routes.py').read_text()
    mutation_operations = (ROOT / 'lib/orchestration/'
                           'mutation_operations.py').read_text()
    ports = (ROOT / 'lib/task_runtime_ports.py').read_text()

    assert 'runtime: TaskRouteRuntimePort' in generic
    assert 'runtime: TaskRouteRuntimePort' in runtime
    assert 'runtime: TaskRouteRuntimePort' in mutation_routes
    assert 'runtime: TaskAbortRuntimePort' in mutation_operations
    assert 'class TaskRouteRuntimePort(' in ports
    assert 'class TaskAbortRuntimePort(Protocol)' in ports
    assert 'from lib.task_runtime import TaskRuntime' not in (
        runtime + mutation_routes
    )


def test_task_route_factory_preserves_default_poll_and_abort_behavior():
    from quart import Blueprint
    from lib.openapi import build_spec

    app = _app()
    blueprint = Blueprint('task_factory_contract', __name__)
    runtime = _Runtime()
    register_task_routes(
        blueprint, runtime, url_prefix='/factory', tags=('factory',),
    )
    app.register_blueprint(blueprint)

    async def exercise():
        client = app.test_client()
        replay = await client.get('/factory/poll/task-1?cursor=-9')
        aborted = await client.post('/factory/abort/task-1')
        return replay, aborted

    replay, aborted = asyncio.run(exercise())
    assert replay.status_code == 200
    assert aborted.status_code == 200
    assert runtime.poll_cursors == [('task-1', 0)]
    assert runtime.aborted == ['task-1']

    paths = build_spec(app)['paths']
    poll = paths['/factory/poll/{task_id}']['get']
    parameters = {item['name']: item for item in poll['parameters']}
    assert poll['tags'] == ['factory']
    assert parameters['cursor']['schema'] == {
        'type': 'integer', 'minimum': 0, 'default': 0,
    }


def test_task_route_factory_accepts_shared_response_contracts():
    from quart import Blueprint
    from lib.openapi import build_spec

    app = _app()
    blueprint = Blueprint('task_factory_responses', __name__)
    runtime = _Runtime()
    poll_responses = {'200': {'description': 'Shared poll response'}}
    abort_responses = {'200': {'description': 'Shared abort response'}}
    register_task_routes(
        blueprint,
        runtime,
        url_prefix='/contracted',
        poll_responses=poll_responses,
        abort_responses=abort_responses,
    )
    app.register_blueprint(blueprint)

    paths = build_spec(app)['paths']
    assert paths['/contracted/poll/{task_id}']['get']['responses'] == \
        poll_responses
    assert paths['/contracted/abort/{task_id}']['post']['responses'] == \
        abort_responses


def test_task_route_factory_applies_injected_decorators_before_runtime():
    from quart import Blueprint
    from lib.api_response import api_unauthorized

    app = _app()
    blueprint = Blueprint('task_factory_guarded', __name__)
    runtime = _Runtime()

    def reject(handler):
        @functools.wraps(handler)
        def guarded(*_args, **_kwargs):
            return api_unauthorized('guarded')
        return guarded

    register_task_routes(
        blueprint,
        runtime,
        url_prefix='/guarded',
        route_decorators=(reject,),
    )
    app.register_blueprint(blueprint)

    async def exercise():
        client = app.test_client()
        return (
            await client.get('/guarded/poll/task-1'),
            await client.post('/guarded/abort/task-1'),
        )

    replay, aborted = asyncio.run(exercise())
    assert replay.status_code == aborted.status_code == 401
    assert runtime.poll_cursors == []
    assert runtime.aborted == []


def test_generic_task_replay_routes_publish_the_shared_cursor_schema():
    from lib.openapi import build_spec
    from routes.api_v1.tasks import api_v1_tasks_bp

    app = _app()
    app.register_blueprint(api_v1_tasks_bp)
    paths = build_spec(app)['paths']
    expected = task_http.task_replay_parameters()[0]
    for path in (
        '/api/v1/tasks/{task_id}/events',
        '/api/v1/tasks/{task_id}/stream',
    ):
        parameters = {
            item['name']: item for item in paths[path]['get']['parameters']
        }
        assert parameters['cursor'] == expected

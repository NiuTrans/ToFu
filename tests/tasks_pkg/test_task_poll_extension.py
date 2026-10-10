"""Task event HTTP reads retain plugin and injected poll implementations."""
import pytest

from lib.agent_core.task_runtime import TaskRuntime
from tests.tasks_pkg.test_task_quality_axis import _run, _tasks_app

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('subclass_override', [False, True])
def test_task_events_preserve_legacy_poll_override(monkeypatch, subclass_override):
    observed = []
    payload = {'format': 'tofu.task-replay/v1', 'ok': True,
               'events': [{'type': 'progress', 'seq': 7}], 'next_cursor': 8,
               'status': 'running', 'done': False,
               'cursor': {'requested': 7, 'next': 8, 'reset': False}}
    class ExtensionRuntime(TaskRuntime):
        def poll(self, task_id, cursor=0):
            observed.append((task_id, cursor))
            return payload
    runtime = (ExtensionRuntime if subclass_override else TaskRuntime)(
        'extension-poll', push_channel='')
    task = runtime.create(user_id=1)
    if not subclass_override:
        def injected_poll(task_id, cursor=0):
            observed.append((task_id, cursor))
            return payload
        monkeypatch.setattr(runtime, 'poll', injected_poll)
    app, module = _tasks_app(runtime)
    monkeypatch.setattr(module, '_registries', lambda: {'extension-poll': runtime})
    async def request_page():
        response = await app.test_client().get(
            f'/api/v1/tasks/{task["id"]}/events?cursor=7')
        assert response.status_code == 200
        body = await response.get_json()
        assert body['events'] == payload['events']
        assert body['next_cursor'] == 8 and body['caught_up'] is True
    _run(request_page())
    assert observed == [(task['id'], 7)]

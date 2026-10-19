"""Explicit durable task reads through real SQLite/native authorities."""
import asyncio

import pytest

from tests.storage.test_storage_application_contract import (
    application_storage, application_storage_owner_id,
)
from tests.api_v4.test_durable_chat_replay import _seed_task, _tasks_app
from tests.support.envelope import v1_get_json

pytestmark = pytest.mark.api


@pytest.mark.usefixtures('application_storage')
def test_explicit_view_reads_committed_authority(monkeypatch):
    import routes.api_v1.tasks as tasks_mod
    from lib.tasks_pkg.manager._persist import build_result_meta
    from lib.agent_core.run_contract import project_agent_result
    from tofu_agent.models import AgentResult
    metadata = build_result_meta({'finishReason': 'stop', 'streamState': 'empty_response'})
    task_id, _ = _seed_task(user_id=1, event_sequences=(0, 2), metadata_overrides=metadata)
    app = _tasks_app(monkeypatch)
    hot = {'id': task_id, 'kind': 'chat', 'status': 'running', 'content': 'memory only'}
    monkeypatch.setattr(tasks_mod, '_find_task', lambda key, user_id: (None, hot, False))

    async def exercise():
        client = app.test_client()
        response = await client.get(f'/api/v4/tasks/{task_id}?view=durable')
        assert response.status_code == 200
        return await v1_get_json(response)

    result = asyncio.run(exercise())
    assert result['readSource'] == 'durable'
    assert result['status'] == 'done'
    assert result['content'] == 'durable answer'
    assert result['streamState'] == 'empty_response'
    assert not AgentResult.from_payload(project_agent_result(result, model='model-cold')).ok
    assert hot['status'] == 'running'

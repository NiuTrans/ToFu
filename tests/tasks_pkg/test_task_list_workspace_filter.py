"""Owned workspace lookup filters before slicing, so old long tasks stay visible."""
import asyncio
from quart import Quart, g, request
import pytest
from lib.agent_core.task_runtime import TaskRuntime
from lib.api_keys import local_admin_context
from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4

pytestmark = pytest.mark.unit


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
    app=Quart(__name__);app.config['TESTING']=True
    @app.before_request
    async def grant():
        context=local_admin_context();context.owner_user_id=int(request.headers.get('X-Test-User','1'))
        g.auth_ctx=context;g.rate_decision=None
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(tasks.api_v1_tasks_bp,name='workspace_tasks_v4')))
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

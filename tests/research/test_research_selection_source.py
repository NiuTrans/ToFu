"""Saved discovery identity is owner-validated and part of experiment basis."""
import asyncio
from copy import deepcopy
import json
import uuid
import pytest
from lib.research import records, workspace, program, action
from lib.storage import StorageSupervisor
from lib.storage.errors import StorageError
from lib.turn_lifecycle import LifecycleNotFound
pytestmark = pytest.mark.unit
DIRECTION = 'Frozen original selection'


@pytest.fixture(params=['sqlite','tofudb'])
def storage(tmp_path,monkeypatch,request):
    monkeypatch.setenv('TOFUDB_OWNER_ID','12')
    supervisor = StorageSupervisor(project_root=tmp_path / 'storage',backend=request.param,startup_timeout=60)
    supervisor.start()
    monkeypatch.setattr(workspace,'_storage',lambda **kwargs:supervisor.client)
    try:
        yield supervisor, request.param
    finally:
        supervisor.stop()


def source(client, monkeypatch, task_id='original', owner=12, ideas=('idea-1',)):
    monkeypatch.setattr(records,'_storage',lambda **kwargs:client)
    records.persist_record({'task_id':task_id,'user_id':owner,'direction':DIRECTION,
        'lang':'en','status':'done','result':{'accepted':[{'id':idea} for idea in ideas]}})


def test_saved_source_survives_legacy_update_without_field(storage,monkeypatch):
    supervisor, _ = storage
    source(supervisor.client,monkeypatch)
    draft = workspace.empty_workspace(DIRECTION)
    draft.update(selected_idea_id='idea-1',selected_idea_source_task_id='original')
    saved = workspace.save_workspace(DIRECTION,'en',draft,user_id=12,expected_revision=0)
    assert saved['selected_idea_source_task_id'] == 'original'
    legacy = deepcopy(saved);legacy.pop('selected_idea_source_task_id')
    updated = workspace.save_workspace(DIRECTION,'en',legacy,user_id=12,expected_revision=saved['revision'])
    assert updated['selected_idea_source_task_id'] == 'original'


# Another owner's record is indistinguishable from none; an owner-bound native
# authority may refuse the foreign workspace read first.
@pytest.mark.parametrize('owner,idea,refusal',[(13,'idea-1',(LifecycleNotFound,StorageError)),
                                               (12,'missing',ValueError)])
def test_foreign_or_missing_selection_cannot_publish(storage,monkeypatch,owner,idea,refusal):
    supervisor, _ = storage
    source(supervisor.client,monkeypatch)
    draft = workspace.empty_workspace(DIRECTION)
    draft.update(selected_idea_id=idea,selected_idea_source_task_id='original')
    with pytest.raises(refusal):
        workspace.save_workspace(DIRECTION,'en',draft,user_id=owner,expected_revision=0)
    assert workspace.load_workspace(DIRECTION,'en',user_id=12)['revision'] == 0


def test_source_change_invalidates_basis_but_empty_legacy_keeps_hash():
    base = workspace.empty_workspace(DIRECTION);base['hypothesis'] = 'Measured hypothesis'
    legacy = program.dependency_digests(base)
    omitted = deepcopy(base);omitted.pop('selected_idea_source_task_id')
    assert program.dependency_digests(omitted) == legacy
    base.update(selected_idea_id='idea-1',selected_idea_source_task_id='original')
    bound = program.dependency_digests(base)
    assert bound['protocol_digest'] == legacy['protocol_digest']
    assert bound['hypothesis_digest'] != legacy['hypothesis_digest']
    assert action._frozen_hypothesis_identity(base) == bound['hypothesis_digest']
    base['selected_idea_source_task_id'] = 'different-original'
    assert program.dependency_digests(base)['hypothesis_digest'] != bound['hypothesis_digest']


READY_PROTOCOL = {'decision_rule':{'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2},
    'random_seeds':[1,2,3],'dataset':'held-out','primary_metric':'accuracy','baseline':'reference'}


def _routes(owner):
    """The real workspace and task-start routes on both wire majors, bound to one owner."""
    from quart import Quart, g
    from lib.api_keys import AuthContext
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from routes.api_v1.research import api_v1_research_bp
    from routes.api_v1.tasks import api_v1_tasks_bp
    app = Quart(__name__)
    app.config['TESTING'] = True
    @app.before_request
    async def principal():
        g.auth_ctx = AuthContext(key_id='fixture',owner_user_id=owner,scopes=frozenset({'admin'}))
        g.rate_decision = None
    for blueprint in (api_v1_research_bp, api_v1_tasks_bp):
        app.register_blueprint(blueprint)
        app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(blueprint,name=blueprint.name+'_v4')))
    return app


def _call(app, method, path, body):
    async def go():
        response = await getattr(app.test_client(),method)(path,json=body)
        return response.status_code, await response.get_json(force=True)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


def _bound_workspace(app, prefix):
    """Owner selects idea-1 of the saved source and freezes an admissible protocol."""
    draft = workspace.empty_workspace(DIRECTION)
    draft.update(selected_idea_id='idea-1',selected_idea_source_task_id='original',protocol=READY_PROTOCOL)
    status, body = _call(app,'put',f'{prefix}/research/workspace',
        {'direction':DIRECTION,'lang':'en','expected_revision':0,'workspace':draft})
    assert status == 200, body
    saved = workspace.load_workspace(DIRECTION,'en',user_id=12)
    assert saved['selected_idea_source_task_id'] == 'original'
    action.require_experiment_decision_rule(saved)
    return saved


def _refuse_job_creation(monkeypatch):
    created = []
    def refuse(*args, **kwargs):
        created.append(args[:1])
        raise AssertionError('a refused research-action start reached job creation')
    monkeypatch.setattr('lib.research.action_runtime._production.claim_task',refuse)
    return created


def _start_and_reselect(app, prefix, saved):
    started = _call(app,'post',f'{prefix}/tasks/start',{'kind':'research-action','direction':DIRECTION,
        'lang':'en','action':'experiment','expected_revision':saved['revision'],'confirm_external_writes':True})
    reselected = _call(app,'put',f'{prefix}/research/workspace',{'direction':DIRECTION,'lang':'en',
        'expected_revision':saved['revision'],'workspace':dict(saved,selected_idea_id='idea-2')})
    return started, reselected


@pytest.mark.parametrize('prefix',['/api/v1','/api/v4'])
def test_deleted_selection_source_is_not_found_on_start_and_put(storage,monkeypatch,prefix):
    supervisor, _ = storage
    source(supervisor.client,monkeypatch,ideas=('idea-1','idea-2'))
    app = _routes(12)
    saved = _bound_workspace(app,prefix)
    key = '12:original'
    row = supervisor.client.query('record.get',{'namespace':records.NAMESPACE,'key':key})
    supervisor.client.command('record.delete',{'namespace':records.NAMESPACE,'key':key,
        'expected_version':row['version']},f'test-delete:{uuid.uuid4().hex}')
    assert not supervisor.client.query('record.get',{'namespace':records.NAMESPACE,'key':key})
    created = _refuse_job_creation(monkeypatch)
    for status, body in _start_and_reselect(app,prefix,saved):
        assert status == 404, body
        if prefix == '/api/v1':
            assert body['ok'] is False
            message = body['error']
        else:
            assert (body['status'],body['code']) == (404,'not_found')
            message = body['detail']
        assert 'research source not found' in message.lower()
    assert created == []
    assert workspace.load_workspace(DIRECTION,'en',user_id=12)['revision'] == saved['revision']


def test_unreadable_selection_source_stays_a_redacted_server_fault(storage,monkeypatch):
    supervisor, _ = storage
    source(supervisor.client,monkeypatch,ideas=('idea-1','idea-2'))
    app = _routes(12)
    saved = _bound_workspace(app,'/api/v4')
    # The record exists, but its stored identity no longer matches its key.
    records._put(records.NAMESPACE,'12:original',{'task_id':'secret-corrupt-identity'})
    created = _refuse_job_creation(monkeypatch)
    for status, body in _start_and_reselect(app,'/api/v4',saved):
        assert status == 500, body
        assert body['code'] == 'internal_error'
        assert 'secret-corrupt-identity' not in json.dumps(body)
    assert created == []
    assert workspace.load_workspace(DIRECTION,'en',user_id=12)['revision'] == saved['revision']

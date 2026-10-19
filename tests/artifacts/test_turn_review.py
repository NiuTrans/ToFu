"""Ordinary task acceptance shares generations, evidence and CAS on both stores."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.turn_lifecycle.test_task_event_delivery import storage as storage, turn_service as turn_service
from tests.turn_lifecycle.test_turn_event_carried_task_event import _running_task
from lib.acceptance import turns
from lib.acceptance.review import ReviewConflict
from lib.artifacts import core
from lib.identity import PrincipalContext
from lib.storage import StorageError

pytestmark = pytest.mark.unit
PRINCIPAL = PrincipalContext.user(subject_id='reviewer', owner_user_id=1)


@pytest.fixture
def subject(storage, turn_service, monkeypatch):
    monkeypatch.setattr(turns, 'get_storage_client', lambda write=False: storage.client)
    created, task = _running_task(turn_service, storage, 'turn-review', 'turn-review-task')
    task.update(convId='turn-review', _turnId=created['turn']['turnId'])
    turn_service.record_task_event(task, {'type': 'delta', 'content': 'seed'})
    artifact = core.create_artifact(conv_id='turn-review', content='# Report', format='markdown',
        source='write_file', source_ref={'path': 'report.md'})
    return SimpleNamespace(turn_id=created['turn']['turnId'], task=task, artifact=artifact, service=turn_service,
                           input_id=created['turn']['parentTurnId'])


def read(subject, principal=PRINCIPAL):
    return turns.read_review('turn-review', subject.turn_id, principal=principal)


def slots(subject):
    return [{'id': 'report', 'label': 'Report', 'artifactId': subject.artifact['id']}]


def configure(subject, previous=None, **changes):
    view = previous or read(subject)
    return turns.update_review('turn-review', subject.turn_id, {
        'action': 'requirements', 'requirementsRevision': view['requirementsRevision'],
        'objective': 'Deliver a checked report', 'criteria': [
            {'id': 'answer', 'kind': 'requirement', 'text': 'Explain the result'},
            {'id': 'report', 'kind': 'requirement', 'text': 'Report totals are correct'},
        ], 'outputs': slots(subject), **changes}, principal=PRINCIPAL)


def decide(subject, index=0, selected=None, previous=None, **changes):
    view = previous or read(subject)
    criterion = view['criteria'][index]
    selected = selected if selected is not None else [view['outputs'][index]['id']]
    return turns.update_review('turn-review', subject.turn_id, {
        'action': 'decision', 'requirementsRevision': view['requirementsRevision'],
        'decisionsRevision': view['decisionsRevision'], 'criterionId': criterion['id'],
        'criterionDigest': criterion['digest'], 'verdict': 'pass', 'note': 'Compared to the source',
        'outputBindings': [{'id': row['id'], 'version': row['version']} for row in view['outputs'] if row['id'] in selected],
        **changes}, principal=PRINCIPAL)


def finish(subject):
    subject.task.update(status='done', finishReason='stop')
    subject.service.record_task_event(subject.task, {'type': 'done', 'finishReason': 'stop'})


def test_completed_execution_and_all_criteria_do_not_imply_output_coverage(subject, storage):
    finish(subject)
    assert read(subject)['status'] == 'unconfigured'
    configure(subject)
    decide(subject, selected=['answer'])
    partial = decide(subject, index=1, selected=['answer'])
    assert partial['status'] == 'pending'
    assert [row['covered'] for row in partial['outputs']] == [True, False]
    accepted = decide(subject, index=1)
    assert accepted['status'] == 'accepted'
    storage.stop(); storage.start()
    assert read(subject) == accepted


def test_running_turn_cannot_be_accepted_and_final_answer_needs_review(subject):
    configure(subject); decide(subject); before = decide(subject, index=1)
    assert all(row['state'] == 'pass' for row in before['criteria'])
    assert before['status'] == 'pending'
    finish(subject)
    changed = read(subject)
    assert [row['state'] for row in changed['criteria']] == ['stale', 'pass']
    assert decide(subject)['status'] == 'accepted'


def test_answer_mutation_and_aba_preserve_independent_file_judgment(subject):
    configure(subject); decide(subject); decide(subject, index=1)
    for content in ['changed answer', 'seed']:
        subject.task['content'] = content
        subject.service.record_task_event(subject.task, {'type': 'delta', 'content': content})
        assert [row['state'] for row in read(subject)['criteria']] == ['stale', 'pass']


def test_file_version_and_slot_readdition_never_resurrect_old_pass(subject):
    finish(subject); configure(subject); decide(subject); decide(subject, index=1)
    old = slots(subject)
    new = core.create_artifact(conv_id='turn-review', content='# Revised report', format='markdown',
        source='write_file', source_ref={'path': 'report.md'})
    changed = configure(subject, outputs=[{**old[0], 'artifactId': new['id']}])
    assert [row['state'] for row in changed['criteria']] == ['pass', 'stale']
    binding = changed['criteria'][1]['evidence']['outputBindings'][0]
    assert binding['artifactId'] == subject.artifact['id']
    assert binding['kind'] == 'artifact' and binding['label'] == 'Report'
    assert configure(subject, outputs=old)['criteria'][1]['state'] == 'stale'
    decide(subject, index=1)
    configure(subject, outputs=[])
    assert configure(subject, outputs=old)['criteria'][1]['state'] == 'stale'


def test_adding_output_keeps_independent_judgments_but_requires_coverage(subject):
    finish(subject); configure(subject); decide(subject); decide(subject, index=1)
    second = core.create_artifact(conv_id='turn-review', content='Data', format='markdown', source='write_file')
    changed = configure(subject, outputs=slots(subject)+[{'id':'data','label':'Data','artifactId':second['id']}])
    assert changed['status'] == 'pending'
    assert [row['state'] for row in changed['criteria']] == ['pass', 'pass']
    assert [row['covered'] for row in changed['outputs']] == [True, True, False]
    assert decide(subject, index=1, selected=['report','data'])['status'] == 'accepted'


def test_global_constraint_must_cover_every_output(subject):
    finish(subject)
    criterion = {'id':'privacy','kind':'constraint','text':'No personal data'}
    configure(subject, criteria=[criterion])
    with pytest.raises(ValueError, match='every declared output'):
        decide(subject, selected=['answer'])
    assert decide(subject, selected=['answer','report'])['status'] == 'accepted'
    second = core.create_artifact(conv_id='turn-review', content='Data', format='markdown', source='write_file')
    changed = configure(subject, criteria=[criterion], outputs=slots(subject)+[{'id':'data','label':'Data','artifactId':second['id']}])
    assert changed['criteria'][0]['state'] == 'stale'
    assert changed['status'] == 'pending'


def test_requirement_generation_and_constraint_scoping_are_shared(subject):
    finish(subject); old=configure(subject); decide(subject); decide(subject,index=1)
    criteria=[{key:row[key] for key in ('id','kind','text')} for row in old['criteria']]
    criteria[0]['text']='Explain uncertainty'
    assert [row['state'] for row in configure(subject,criteria=criteria)['criteria']]==['stale','pass']
    assert configure(subject)['criteria'][0]['state']=='stale'
    decide(subject)
    assert all(row['state']=='stale' for row in configure(subject,objective='Different purpose')['criteria'])


def test_deleted_output_retains_evidence_and_fails_closed(subject, storage):
    finish(subject); configure(subject); decide(subject); decide(subject,index=1)
    core.delete_artifact(subject.artifact['id'])
    view=read(subject)
    assert [row['state'] for row in view['criteria']]==['pass','stale']
    assert view['outputs'][1]['version'] is None
    assert view['criteria'][1]['evidence']['note']=='Compared to the source'
    storage.stop();storage.start()
    assert read(subject)==view


def test_owner_tenant_conversation_and_actor_boundaries(subject, turn_service):
    configure(subject)
    with pytest.raises(turns.LifecycleNotFound):
        read(subject, PrincipalContext.user(subject_id='foreign',owner_user_id=8))
    assert read(subject, PrincipalContext.user(subject_id='reviewer',owner_user_id=1,tenant_id='other'))['status']=='unconfigured'
    with pytest.raises(turns.LifecycleNotFound):
        turns.read_review('turn-review',subject.input_id,principal=PRINCIPAL)
    with pytest.raises(turns.LifecycleNotFound):
        turns.read_review('another-conversation',subject.turn_id,principal=PRINCIPAL)
    created,_ = _running_task(turn_service,None,'another-conversation','another-task')
    other=core.create_artifact(conv_id='another-conversation',content='Other',format='markdown',source='write_file')
    with pytest.raises(core.ArtifactNotFoundError):
        configure(subject,outputs=[{'id':'other','label':'Other','artifactId':other['id']}])
    view=read(subject)
    command={'action':'requirements','requirementsRevision':view['requirementsRevision'],'objective':'system',
             'criteria':[],'outputs':[]}
    with pytest.raises(PermissionError):
        turns.update_review('turn-review',subject.turn_id,command,
            principal=PrincipalContext.system(subject_id='synthetic',owner_user_id=1,scopes=()))


def test_old_tabs_wrong_versions_and_duplicate_slots_fail(subject):
    before=configure(subject); decide(subject)
    with pytest.raises(ReviewConflict):decide(subject,previous=before)
    with pytest.raises(ReviewConflict):decide(subject,outputBindings=[{'id':'answer','version':'0'*64}])
    configure(subject,objective='Changed')
    with pytest.raises(ReviewConflict):configure(subject,previous=before)
    with pytest.raises(ValueError):configure(subject,outputs=slots(subject)*2)
    with pytest.raises(ValueError):configure(subject,outputs=[{**slots(subject)[0],'id':'answer'}])
    with pytest.raises(ValueError):decide(subject,note=' ')


def test_requirement_and_answer_changes_racing_decision_are_stale(subject,monkeypatch):
    finish(subject); configure(subject); decide(subject)
    original=turns.TurnReview.put
    def interleave(self,kind,value,revision):
        if kind=='decisions':configure(subject,objective='Changed during review')
        return original(self,kind,value,revision)
    monkeypatch.setattr(turns.TurnReview,'put',interleave)
    result=decide(subject,index=1)
    assert all(row['state']=='stale' for row in result['criteria'])
    assert result['status']=='pending'


def test_lost_ack_readback_and_uncommitted_failure(subject,storage,monkeypatch):
    original=storage.client.command
    def lost(*args,**kwargs):
        original(*args,**kwargs)
        raise StorageError('database_timeout','lost ACK')
    monkeypatch.setattr(storage.client,'command',lost)
    assert configure(subject)['requirementsRevision']==1
    def failed(*args,**kwargs):raise StorageError('database_unavailable','not committed')
    monkeypatch.setattr(storage.client,'command',failed)
    with pytest.raises(StorageError):configure(subject,objective='Not saved')
    assert read(subject)['objective']=='Deliver a checked report'


def test_bound_fact_correction_invalidates_only_dependent_judgment(subject,monkeypatch,storage):
    from lib.memory.working_facts import repository,service
    monkeypatch.setattr(repository,'get_storage_client',lambda write=False:storage.client)
    finish(subject); configure(subject)
    fact=service.record_fact('turn-review',{'action':'record','revision':0,'text':'# Report','kind':'observation',
        'sources':[{'kind':'artifact','id':subject.artifact['id'],'version':subject.artifact['content_sha256'],'quote':'# Report'}]},
        principal=PRINCIPAL,task_id=subject.task['id'])['facts'][0]
    decide(subject); accepted=decide(subject,index=1,factBindings=[{'id':fact['id'],'revision':fact['revision']}])
    assert accepted['status']=='accepted'
    view=service.query_facts('turn-review',principal=PRINCIPAL)
    service.owner_decision('turn-review',{'action':'correct','revision':view['revision'],'id':fact['id'],
        'text':'Corrected total','note':'Compared source'},principal=PRINCIPAL)
    changed=read(subject)
    assert [row['state'] for row in changed['criteria']]==['pass','stale']
    assert changed['criteria'][1]['evidence']['factBindings'][0]['text']=='# Report'


def test_http_scope_conflict_and_contract(subject):
    from quart import Quart,g,request
    from lib.api_v4 import attach_v4_envelope,copy_blueprint_to_v4
    from routes.api_v1.turn_review import api_v1_turn_review_bp
    from lib.acceptance.contract import validate
    app=Quart(__name__)
    from lib.http_transport.error_handlers import register_http_error_handlers
    register_http_error_handlers(app)
    app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_turn_review_bp,name='turn_review_test_v4')))
    @app.before_request
    def identity():
        owner=request.headers.get('X-Test-Owner')
        if owner:
            scopes=request.headers.get('X-Test-Scopes','chat').split(',')
            g.auth_ctx=SimpleNamespace(is_authenticated=True,owner_user_id=int(owner),key_id='test',name='test',
                tenant_id=None,scopes=scopes,has_scope=lambda scope:scope in scopes)
            g.principal_context=PrincipalContext.user(subject_id='reviewer',owner_user_id=int(owner))
    async def exercise():
        client=app.test_client();url='/api/v4/conversations/turn-review/turns/'+subject.turn_id+'/review'
        headers={'X-Test-Owner':'1'}
        assert (await client.get(url)).status_code==401
        assert (await client.get(url,headers={'X-Test-Owner':'8'})).status_code==404
        assert (await client.get(url,headers={**headers,'X-Test-Scopes':'tasks'})).status_code==403
        response=await client.get(url,headers=headers)
        assert response.status_code==200
        validate((await response.get_json())['data'],'TurnReviewView')
        command={'action':'requirements','requirementsRevision':0,'objective':'Review','criteria':[], 'outputs':[]}
        assert (await client.post(url,headers=headers,json=command)).status_code==200
        assert (await client.post(url,headers=headers,json=command)).status_code==409
        command['extra']='forbidden'
        assert (await client.post(url,headers=headers,json=command)).status_code==400
    asyncio.run(exercise())


def test_retry_attempt_cannot_inherit_acceptance_of_identical_answer(subject):
    finish(subject); configure(subject); decide(subject); decide(subject,index=1)
    turn=subject.service.get_turn('turn-review',subject.turn_id,user_id=1)
    retry=subject.service.create_attempt('turn-review',subject.turn_id,command_id='retry-review',operation='regenerate',
        expected_projection_revision=turn['projectionRevision'],config={'model':'fixture'},user_id=1)
    attempt=retry['attempt']['attemptId']
    subject.service.bind_task(attempt,'retry-review-task',user_id=1)
    subject.task.update(_attemptId=attempt,id='retry-review-task',status='running',content='seed')
    subject.service.record_task_event(subject.task,{'type':'delta','content':'seed'})
    finish(subject)
    assert [row['state'] for row in read(subject)['criteria']]==['stale','pass']


def test_output_change_racing_decision_is_stale(subject,monkeypatch):
    configure(subject)
    original=turns.TurnReview.put
    def interleave(self,kind,value,revision):
        if kind=='decisions':
            subject.task['content']='Changed while reviewing'
            subject.service.record_task_event(subject.task,{'type':'delta','content':subject.task['content']})
        return original(self,kind,value,revision)
    monkeypatch.setattr(turns.TurnReview,'put',interleave)
    assert decide(subject)['criteria'][0]['state']=='stale'


def test_requirements_read_avoids_decisions_artifact_and_fact_reads(subject,monkeypatch,storage):
    configure(subject)
    original=storage.client.query; calls=[]
    def observe(operation,payload,*args,**kwargs):
        calls.append((operation,payload))
        return original(operation,payload,*args,**kwargs)
    monkeypatch.setattr(storage.client,'query',observe)
    value=turns.read_requirements('turn-review',subject.turn_id,principal=PRINCIPAL)
    assert value['objective']=='Deliver a checked report'
    assert value['outputs']==slots(subject)
    assert len([row for row in calls if row[0]=='record.get'])==1
    assert not any(operation.startswith('artifact.') for operation,_ in calls)


def test_current_failure_keeps_task_unaccepted(subject):
    finish(subject); configure(subject); decide(subject)
    assert decide(subject,index=1,verdict='fail')['status']=='changes_requested'


@pytest.mark.parametrize('output_count', [1, 32])
def test_review_cost_follows_declared_outputs_without_reading_bodies(subject, storage, monkeypatch, tmp_path, output_count):
    import json
    import statistics
    import time
    from collections import Counter
    output_slots=slots(subject)
    for index in range(1, output_count):
        artifact=core.create_artifact(conv_id='turn-review',content=f'Report {index}',format='markdown',source='write_file')
        output_slots.append({'id':f'output_{index}','label':f'Output {index}','artifactId':artifact['id']})
    configure(subject,outputs=output_slots,criteria=[
        {'id':f'requirement_{index}','kind':'requirement','text':f'Check {index}'} for index in range(16)])
    calls=Counter(); original=storage.client.query
    def observe(operation,payload,*args,**kwargs):
        calls[operation]+=1
        if operation=='record.get':
            assert payload['namespace']=='turn_review_v1'
        assert operation not in {'artifact.content','artifact.read','blob.get'}
        return original(operation,payload,*args,**kwargs)
    monkeypatch.setattr(storage.client,'query',observe)
    read(subject); calls.clear()
    timings=[]
    for _ in range(10):
        start=time.perf_counter(); view=read(subject); timings.append((time.perf_counter()-start)*1000)
    assert calls['turn.get']==10
    assert calls['record.get']==20
    assert len(view['outputs'])==output_count+1
    (tmp_path/'turn-review-measurement.json').write_text(json.dumps({
        'artifactOutputs':output_count,'criteria':16,'reads':10,'median_ms':statistics.median(timings),
        'response_bytes':len(json.dumps(view,ensure_ascii=False,separators=(',',':')).encode()),
        'queries':dict(calls)},indent=2))


def test_maximum_declared_scope_still_has_room_for_the_mandatory_answer(subject):
    from lib.acceptance.contract import contract
    count=contract()['$defs']['TurnRequirementsCommand']['properties']['outputs']['maxItems']
    output_slots=slots(subject)
    for index in range(1,count):
        artifact=core.create_artifact(conv_id='turn-review',content=f'Boundary report {index}',format='markdown',source='write_file')
        output_slots.append({'id':f'output_{index}','label':f'Output {index}','artifactId':artifact['id']})
    finish(subject)
    configured=configure(subject,outputs=output_slots,criteria=[{'id':'complete','kind':'constraint','text':'Every deliverable checked'}])
    assert len(configured['outputs'])==count+1
    assert decide(subject,selected=[row['id'] for row in configured['outputs']])['status']=='accepted'
    with pytest.raises(ValueError):configure(subject,outputs=output_slots+[output_slots[0]])
    assert read(subject)['status']=='accepted'


def test_deleted_target_hides_review_without_erasing_owner_evidence(subject):
    finish(subject); configure(subject); decide(subject); decide(subject, index=1)
    repository = turns.TurnReview('turn-review', subject.turn_id, PRINCIPAL)
    retained = repository.read('decisions')
    subject.service.delete_turns('turn-review', [subject.turn_id], user_id=1)
    with pytest.raises(turns.LifecycleNotFound):
        read(subject)
    with pytest.raises(turns.LifecycleNotFound):
        turns.read_requirements('turn-review', subject.turn_id, principal=PRINCIPAL)
    assert repository.read('decisions') == retained

"""Owned final-delivery intent, fault retention and independent frozen review."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.evolution import artifact_delivery as delivery, artifact_evidence as artifacts
from lib.evolution.contracts import EvolutionStateError, EvolutionContractError
from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
from lib.tasks_pkg.tool_delivery import consume_delivery
from tests.evolution.test_evolution_runs import runtime as task_runtime, OWNER, finish, review
from tests.evolution.test_evolution_artifact_evidence import project_run, read_artifacts

runtime = task_runtime
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def no_reindex(monkeypatch):
    monkeypatch.setattr('lib.evolution.maintenance.reindex_maintenance',lambda **kw:[])


@pytest.fixture
def persisted_runtime(monkeypatch,request):
    """No synthetic record.* seam: durable run, manifest, parts and result owners."""
    from lib.storage import storage_status
    if not storage_status().get('ready'):
        request.getfixturevalue('chat_sidecar')
    from tests.conftest import _assert_isolated_storage
    _assert_isolated_storage('explicit artifact delivery persistence')
    from lib.evolution import runs
    from lib.tasks_pkg import spawn
    tasks=[]
    monkeypatch.setattr(runs,'resolve_run_model',lambda requested,**kw:requested)
    monkeypatch.setattr(spawn,'spawn_task',lambda task:tasks.append(task))
    monkeypatch.setattr(runs,'default_task_loader',lambda tid,user_id:
        next((t for t in tasks if t['id']==tid and t['_userId']==user_id),None))
    yield runs,None,tasks
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.tasks_pkg.manager._registry import discard_task
    for task in tasks:
        task['_executionSession'].settle(ExecutionPhase.CANCELLED,cause='test_cleanup')
        discard_task(task['id'],'')


def request(runtime, tmp_path, **extra):
    run, task=project_run(runtime,tmp_path)
    (tmp_path/'answer.json').write_text('{"result":"完整原件 🧪"}')
    args={'paths':['answer.json'],'delivery':'Prepared answer.json; checks passed. Demand remains unknown.',**extra}
    message={'role':'assistant','content':'','tool_calls':[{'id':'delivery-call',
        'type':'function','function':{'name':'evolution_artifact','arguments':json.dumps(args,ensure_ascii=False)}}]}
    task['_compact_messages']=[message]
    return run,task,args,message


def source_request(runtime, tmp_path, content='{"result":"exact原件 🧪e\u0301"}\r\n'):
    run, task = project_run(runtime, tmp_path)
    (tmp_path/'answer.json').write_bytes(content.encode('utf-8'))
    snapshot = artifacts.declare(task, {'path':'answer.json'})
    args = {'delivery_source':{key:snapshot[key] for key in ('artifact_id','content_sha256')}}
    message = {'role':'assistant','content':'','tool_calls':[{'id':'delivery-call',
        'type':'function','function':{'name':'evolution_artifact','arguments':json.dumps(args)}}]}
    task['_compact_messages'] = [message]
    return run,task,args,message,content


def acknowledge(task,args,message,result):
    messages=task['_compact_messages']
    messages.append({'role':'tool','tool_call_id':'delivery-call','content':json.dumps(result)})
    task['_tool_call_id_receipts']={'delivery-call':{'name':'evolution_artifact','status':'done'}}
    provider=ProviderStreamResult(message,'tool_calls',{'input_tokens':10,'output_tokens':5},
        ProviderStreamState.PROVIDER_FINISHED,provider_finish_reason='tool_calls',saw_finish_reason=True)
    state=SimpleNamespace(last_stream_result=provider,last_finish_reason='tool_calls',assistant_msg=message)
    return consume_delivery(task,state,round_num=0,message=message,messages=messages,note=None),state


def test_complete_delivery_closes_generations_and_independent_review_still_reads_original(runtime,tmp_path):
    run,task,args,message=request(runtime,tmp_path)
    result=delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    manifest=artifacts.manifest_for(task,OWNER)
    assert manifest['delivery_intent']['content']==args['delivery'] and result['acceptance']=='pending independent review'
    (tmp_path/'answer.json').write_text('unreviewed later edit')
    assert acknowledge(task,args,message,result)[0]
    task['messages']=task['_compact_messages']
    with pytest.raises(EvolutionStateError,match='closed'):artifacts.declare(task,{'path':'answer.json'})
    reviewing=finish(runtime,task,args['delivery'])
    assert reviewing['status']=='reviewing' and len(runtime[2])==2
    rows=[json.loads(line) for line in read_artifacts(runtime[2][-1],reviewing).splitlines()]
    assert rows[0]['content']=='{"result":"完整原件 🧪"}'
    assert finish(runtime,runtime[2][-1],review())['status']=='completed'


def test_prepared_intent_without_live_ack_cannot_complete_or_replay(runtime,tmp_path):
    run,task,args,_=request(runtime,tmp_path)
    delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    task.pop('_explicitToolDeliveryRequest')
    before=deepcopy(runtime[0].get_run(run['run_id'],user_id=OWNER))
    assert runtime[0].advance_run(run['run_id'],user_id=OWNER)==before
    assert len(runtime[2])==1 and task['content']==''


def test_identical_retry_acknowledges_frozen_bytes_changed_identity_refuses(runtime,tmp_path):
    _,task,args,message=request(runtime,tmp_path)
    first=delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    task.pop('_explicitToolDeliveryRequest');(tmp_path/'answer.json').unlink()
    assert delivery.prepare(task,args,call_id='delivery-call',round_num=0)==first
    task.pop('_explicitToolDeliveryRequest')
    changed={**args,'delivery':'changed claim'};message['tool_calls'][0]['function']['arguments']=json.dumps(changed)
    with pytest.raises(EvolutionStateError,match='another delivery identity'):
        delivery.prepare(task,changed,call_id='delivery-call',round_num=0)


def test_partial_capture_retains_success_without_closing_or_binding_delivery(runtime,tmp_path):
    run,task,args,_=request(runtime,tmp_path,paths=['answer.json','missing.json'])
    result=delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    manifest,_=artifacts._manifest(run,task,OWNER)
    assert result['complete'] is False and len(result['results'])==2
    assert not manifest['closed'] and len(manifest['snapshots'])==1
    assert '_explicitToolDeliveryRequest' not in task


def test_sibling_tool_refused_before_capture_or_storage_write(runtime,tmp_path):
    _,task,args,message=request(runtime,tmp_path)
    message['tool_calls'].append(deepcopy(message['tool_calls'][0]))
    writes=runtime[1].writes
    with pytest.raises(ValueError,match='sole tool call'):
        delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    assert runtime[1].writes==writes


@pytest.mark.parametrize('content',['','   ','x'*24001])
def test_reply_bounds_refuse_before_writes(runtime,tmp_path,content):
    _,task,args,_=request(runtime,tmp_path,delivery=content);writes=runtime[1].writes
    with pytest.raises(EvolutionContractError):delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    assert runtime[1].writes==writes


def test_old_incomplete_generation_cannot_be_erased_by_a_new_complete_selection(runtime,tmp_path,monkeypatch):
    run,task,args,_=request(runtime,tmp_path)
    original=artifacts._content
    with monkeypatch.context() as patch:
        patch.setattr(artifacts,'_content',lambda *a:(_ for _ in ()).throw(EvolutionStateError('injected part loss')))
        with pytest.raises(EvolutionStateError):artifacts.declare(task,{'path':'answer.json'})
    (tmp_path/'answer.json').write_text('new complete generation')
    with pytest.raises(EvolutionStateError,match='every declared generation'):
        delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    manifest,_=artifacts._manifest(run,task,OWNER)
    assert [s['state'] for s in manifest['snapshots']]==['collecting','sealed'] and not manifest['closed']


def test_cancel_after_prepare_refuses_completion(runtime,tmp_path):
    run,task,args,message=request(runtime,tmp_path)
    result=delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    runtime[0].cancel_run(run['run_id'],user_id=OWNER)
    assert not acknowledge(task,args,message,result)[0]


def test_declaring_delivery_without_live_dispatch_refuses(runtime,tmp_path):
    _,task,args,_=request(runtime,tmp_path)
    with pytest.raises(EvolutionStateError,match='live tool handler'):artifacts.declare(task,args)


def test_intent_write_ack_loss_retains_frozen_sources_without_authorizing_completion(runtime,tmp_path,monkeypatch):
    from lib.evolution import records
    from lib.storage.errors import StorageError
    run,task,args,_=request(runtime,tmp_path)
    original=records.put_record
    def lose_ack(namespace,key,value,**kw):
        result=original(namespace,key,value,**kw)
        if namespace==artifacts.MANIFESTS and value.get('delivery_intent'):
            raise StorageError('database_locked','injected intent acknowledgement loss')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(records,'put_record',lose_ack)
        with pytest.raises(StorageError):delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    assert artifacts.manifest_for(task,OWNER)['closed']
    assert '_explicitToolDeliveryRequest' not in task
    assert runtime[0].advance_run(run['run_id'],user_id=OWNER)['status']=='executing'
    assert len(runtime[2])==1
    assert delivery.prepare(task,args,call_id='delivery-call',round_num=0)['delivery_prepared']


def test_corrupt_frozen_part_refuses_completion_after_successful_handler_ack(runtime,tmp_path):
    _,task,args,message=request(runtime,tmp_path)
    result=delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    snapshot=artifacts.manifest_for(task,OWNER)['snapshots'][0]
    key=(artifacts.PARTS,artifacts._part_key(snapshot['artifact_id'],0,OWNER))
    runtime[1].rows[key]['text']='corrupt'
    assert not acknowledge(task,args,message,result)[0]


@pytest.mark.parametrize('terminal_fault',['none','lost_receiver_ack'])
@pytest.mark.parametrize('delivery_kind',['authored_text','artifact_source'])
def test_real_registered_pipeline_root_close_and_owned_terminal_commit(persisted_runtime,tmp_path,monkeypatch,terminal_fault,delivery_kind):
    """Controlled provider payload; actual registered tool, root and native result CAS."""
    from lib.agent_loop import run_agent_loop, AbortSignal
    from lib.tasks_pkg.orchestrator._root_agent_loop import RootLoopRequest, _RootLoopHooks
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.tasks_pkg.orchestrator._post_loop import finalize_after_loop
    from lib.storage import get_storage_client
    from lib.evolution import stage_outcomes
    from lib.tasks_pkg import manager
    runtime=persisted_runtime
    if delivery_kind == 'artifact_source':
        artifact_content = json.dumps({'result':'完整原件 🧪e\u0301\r\n' * 4000}, ensure_ascii=False)
        run,task,args,message,expected = source_request(runtime,tmp_path,artifact_content)
    else:
        run,task,args,message=request(runtime,tmp_path)
        expected = args['delivery']
        artifact_content = '{"result":"完整原件 🧪"}'
    task['status']='running'
    original_messages=deepcopy(task['messages'])
    messages=task['messages'];task.pop('_compact_messages')
    tools=artifacts.build(SimpleNamespace(cfg=task['config'],project_enabled=True))
    state=RoundState(model='controlled',preset='default',thinking_enabled=False)
    # Earlier tool calls can make tool ordinals differ from model-round ordinals.
    state.tool_round_num=3
    provider=ProviderStreamResult(message,'tool_calls',{'input_tokens':10,'output_tokens':5},
        ProviderStreamState.PROVIDER_FINISHED,provider_finish_reason='tool_calls',saw_finish_reason=True)
    request_value=RootLoopRequest(task=task,state=state,messages=messages,tool_list=tools,
        all_search_results_text=[],cfg=task['config'],tid=task['id'][:8],thinking_depth=None,
        temperature=None,max_tokens=1000,response_format=None,project_path=str(tmp_path),
        project_enabled=True,search_enabled=False)
    hooks=_RootLoopHooks(request_value)
    hooks.round_context={'stream_accumulator':SimpleNamespace(announced_tc_map={})}
    calls=[];checkpoints=[];events=[]
    original_append=manager.append_event
    def observe_event(t,e):
        events.append(deepcopy(e));return original_append(t,e)
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._round_checkpoint.append_event',observe_event)
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._post_loop.append_event',observe_event)
    task['_invocationCheckpointSink']=lambda t,m,n:checkpoints.append(n)
    def model_round(body):
        calls.append(body)
        assert len(calls)==1,'Explicit delivery must avoid another model request'
        state.assistant_msg=message;state.last_stream_result=provider
        state.last_finish_reason='tool_calls';state.last_usage=provider.usage
        state.accumulated_usage=dict(provider.usage);task['usage']=dict(provider.usage)
        return provider
    def execute(round_num,tool_calls):
        # Root prelude installs the same assistant carrier before the gateway.
        messages.append(message)
        task['_compact_messages']=messages
        return hooks.execute_tools(round_num,tool_calls)
    outcome=run_agent_loop(abort=AbortSignal(lambda:task['aborted']),round_tools=tools,
        dispatch=lambda n,t:model_round(n),execute_tools=execute,after_tools=hooks.after_tools,
        on_round_end=hooks.close_round)
    # Shared root records tool admission at round 0, then settled round 1.
    assert outcome.exit_reason=='explicit_tool_delivery' and len(calls)==1 and checkpoints==[0,1]
    assert task['content']==expected and state.last_stream_result is provider
    assert task['toolRounds'][-1]['llmRound']==0
    assert task['toolRounds'][-1]['status']=='done'
    assert artifacts.manifest_for(task,OWNER)['delivery_intent']['round_num']==0
    from lib.tasks_pkg.manager import _terminal
    def lost_receiver(t):raise RuntimeError('injected process loss after owned terminal commit')
    with monkeypatch.context() as patch:
        if terminal_fault=='lost_receiver_ack':patch.setattr(_terminal,'notify_terminal_listeners',lost_receiver)
        finalize_after_loop(task,cfg=task['config'],tid=task['id'][:8],model='controlled',
        preset='default',thinking_depth=None,thinking_enabled=False,temperature=None,
        max_tokens=1000,messages=messages,original_messages=original_messages,tool_list=tools,
        assistant_msg=state.assistant_msg,round_num=0,accumulated_usage=state.accumulated_usage,
        api_rounds=[],last_finish_reason='tool_calls',last_usage=provider.usage,
        tool_call_happened=True,all_search_results_text=[],project_path=str(tmp_path),
        project_enabled=True,loop_exit_reason=outcome.exit_reason,abort_detected_phase=None,
        last_stream_result=provider)
    row=get_storage_client().query('task_results.replay_get',{'key':task['id'],'user_id':OWNER,'include_metadata':True})
    assert row['status']=='done'
    assert task['finishReason']=='tool_calls' and task['streamState']=='provider_finished'
    assert messages[-1]['content']==expected
    assert [(e['roundNum'],e['reason']) for e in events if e['type']=='round_end']==[(0,'tools')]
    if terminal_fault=='lost_receiver_ack':
        assert stage_outcomes.read_metadata(run['run_id'],task['id'],user_id=OWNER) is None
        assert runtime[0].get_run(run['run_id'],user_id=OWNER)['status']=='executing'
        with monkeypatch.context() as patch:
            patch.setattr(runtime[0],'default_task_loader',lambda *a,**kw:None)
            runtime[0].advance_run(run['run_id'],user_id=OWNER)
    current=runtime[0].get_run(run['run_id'],user_id=OWNER)
    assert current['status']=='reviewing' and len(runtime[2])==2
    meta=stage_outcomes.read_metadata(run['run_id'],task['id'],user_id=OWNER)
    assert meta['finish_reason']=='tool_calls' and meta['artifact_evidence']['complete']
    assert meta['actuals']['tokens']==15 and meta['actuals_complete']
    assert row['metadata']['terminalContext']['outcome']==meta
    offset = 0
    chunks = []
    while True:
        page = stage_outcomes.page(run['run_id'],task['id'],user_id=OWNER,offset=offset)
        chunks.append(page['content'])
        if page['next_offset'] is None:
            break
        offset = page['next_offset']
    assert ''.join(chunks).encode('utf-8') == expected.encode('utf-8')
    assert json.loads(read_artifacts(runtime[2][-1],current))['content']==artifact_content
    if terminal_fault=='lost_receiver_ack':
        from lib.tasks_pkg.manager import _persist
        assert _persist.persist_task_result(task,_defer_heavy_release=True)
        assert len(runtime[2])==2 and len(calls)==1
        assert stage_outcomes.read_metadata(run['run_id'],task['id'],user_id=OWNER)==meta


def test_source_delivery_uses_sealed_bytes_with_compact_intent_after_live_file_changes(runtime,tmp_path):
    _,task,args,message,content = source_request(runtime,tmp_path,'\ufeff'+('原件 🧪e\u0301\r\n' * 6000))
    assert len(content) > 24000
    (tmp_path/'answer.json').unlink()
    result = delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    intent = artifacts.manifest_for(task,OWNER)['delivery_intent']
    assert 'content' not in intent and intent['delivery_source'] == args['delivery_source']
    assert len(json.dumps(intent)) < 1024
    assert result['delivery_chars'] == len(content)
    assert acknowledge(task,args,message,result)[0]
    assert task['content'].encode('utf-8') == content.encode('utf-8')


@pytest.mark.parametrize('invalid',['digest','foreign_artifact','foreign_task','path','paths','delivery','binary','blank','collecting'])
def test_invalid_source_delivery_refuses_before_intent_or_live_binding(runtime,tmp_path,invalid):
    content = '   \r\n' if invalid == 'blank' else 'complete text'
    run,task,args,message,_ = source_request(runtime,tmp_path,content)
    if invalid == 'digest':
        args['delivery_source']['content_sha256'] = '0' * 64
    elif invalid == 'foreign_artifact':
        args['delivery_source']['artifact_id'] = '0' * 64
    elif invalid == 'foreign_task':
        _,foreign = project_run(runtime,tmp_path)
        snapshot = artifacts.declare(foreign, {'path':'answer.json'})
        args['delivery_source'] = {key:snapshot[key] for key in ('artifact_id','content_sha256')}
    elif invalid in ('path','paths','delivery'):
        args[invalid] = ['answer.json'] if invalid == 'paths' else 'answer.json'
    elif invalid == 'binary':
        (tmp_path/'binary.bin').write_bytes(b'\xff\x00')
        snapshot = artifacts.declare(task, {'path':'binary.bin'})
        args['delivery_source'] = {key:snapshot[key] for key in ('artifact_id','content_sha256')}
    elif invalid == 'collecting':
        runtime[1].rows[(artifacts.MANIFESTS,artifacts._key(task['id'],OWNER))]['snapshots'][0]['state'] = 'collecting'
    message['tool_calls'][0]['function']['arguments'] = json.dumps(args)
    writes = runtime[1].writes
    with pytest.raises((EvolutionContractError,EvolutionStateError)):
        delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    manifest,_ = artifacts._manifest(run,task,OWNER)
    assert not manifest['closed'] and 'delivery_intent' not in manifest
    assert '_explicitToolDeliveryRequest' not in task
    assert runtime[1].writes == writes


def test_source_retry_is_frozen_and_intent_alone_never_completes(runtime,tmp_path):
    run,task,args,message,content = source_request(runtime,tmp_path)
    prepared = delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    task.pop('_explicitToolDeliveryRequest')
    before = deepcopy(runtime[0].get_run(run['run_id'],user_id=OWNER))
    (tmp_path/'answer.json').write_text('later unreviewed replacement')
    assert runtime[0].advance_run(run['run_id'],user_id=OWNER) == before
    assert len(runtime[2]) == 1 and not task['content']
    retried = delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    assert retried == prepared
    assert acknowledge(task,args,message,retried)[0]
    assert task['content'] == content


@pytest.mark.parametrize('fault',['cancel','deadline','stale_task','unknown_usage','live_consumption','corrupt_part','intent_changed'])
def test_source_ack_cannot_ignore_cancellation_consumption_or_corruption(runtime,tmp_path,fault):
    run,task,args,message,_ = source_request(runtime,tmp_path)
    prepared = delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    if fault == 'cancel':
        runtime[0].cancel_run(run['run_id'],user_id=OWNER)
    elif fault == 'deadline':
        task['config']['executionLimits']['deadline'] = 1
    elif fault == 'stale_task':
        current,version = runtime[0]._read(run['run_id'],OWNER)
        current['task_id'] = 'successor-executor'
        runtime[0]._save(current,version,OWNER)
    elif fault == 'unknown_usage':
        task['_executionActualsComplete'] = False
    elif fault == 'live_consumption':
        from lib.agent_core.execution_budget import model_budget_admission
        lease = model_budget_admission(task)(input_tokens=1,max_output_tokens=1)
    elif fault == 'corrupt_part':
        snapshot = artifacts.manifest_for(task,OWNER)['snapshots'][0]
        runtime[1].rows[(artifacts.PARTS,artifacts._part_key(snapshot['artifact_id'],0,OWNER))]['text'] = 'corrupt'
    else:
        key = (artifacts.MANIFESTS,artifacts._key(task['id'],OWNER))
        runtime[1].rows[key]['delivery_intent']['arguments_sha256'] = '0' * 64
    assert not acknowledge(task,args,message,prepared)[0]
    assert not task['content']
    if fault == 'live_consumption':
        lease.settle(unsent=True)


def test_source_delivery_preserves_the_existing_full_file_byte_bound(runtime,tmp_path):
    content = 'A' * artifacts.MAX_FILE_BYTES
    _,task,args,message,_ = source_request(runtime,tmp_path,content)
    prepared = delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    assert acknowledge(task,args,message,prepared)[0]
    assert task['content'].encode() == content.encode()
    assert 'content' not in artifacts.manifest_for(task,OWNER)['delivery_intent']


def test_provider_tool_schema_has_a_self_contained_source_reference(runtime,tmp_path):
    import jsonschema
    _,task,args,_,_ = source_request(runtime,tmp_path)
    tools = artifacts.build(SimpleNamespace(cfg=task['config'],project_enabled=True))
    parameters = tools[0]['function']['parameters']
    jsonschema.Draft202012Validator(parameters).validate(args)
    assert '$ref' not in parameters['properties']['delivery_source']


def test_source_delivery_retains_prior_incomplete_generations(runtime,tmp_path,monkeypatch):
    run,task = project_run(runtime,tmp_path)
    (tmp_path/'answer.json').write_text('incomplete generation')
    with monkeypatch.context() as patch:
        patch.setattr(artifacts,'_content',lambda *a:(_ for _ in ()).throw(EvolutionStateError('injected part loss')))
        with pytest.raises(EvolutionStateError):
            artifacts.declare(task, {'path':'answer.json'})
    (tmp_path/'answer.json').write_text('complete later generation')
    snapshot = artifacts.declare(task, {'path':'answer.json'})
    args = {'delivery_source':{key:snapshot[key] for key in ('artifact_id','content_sha256')}}
    task['_compact_messages'] = [{'role':'assistant','tool_calls':[{'id':'delivery-call',
        'type':'function','function':{'name':'evolution_artifact','arguments':json.dumps(args)}}]}]
    with pytest.raises(EvolutionStateError,match='every declared generation'):
        delivery.prepare(task,args,call_id='delivery-call',round_num=0)
    manifest,_ = artifacts._manifest(run,task,OWNER)
    assert not manifest['closed'] and 'delivery_intent' not in manifest

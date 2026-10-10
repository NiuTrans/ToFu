"""Computed JSON survives model handoffs without source replay or evidence claims."""
import json

import pytest

from lib.tools.javascript import execute_javascript_program
from lib.tools.program_contract import ProgramExecutionError
from lib.tools.program_results import keep_program_value, list_program_inputs, read_program_result, retain_program_result
from lib.tools.programmatic import ProgrammaticResultBudget
from lib.tools.result_value import capture_result

pytestmark = pytest.mark.unit


@pytest.fixture
def working():
    import uuid
    task = {'id':'working-value-test-' + uuid.uuid4().hex, '_ptc_local':{'tier':'program'}}
    effects = []
    def run(code, *, owner=7, target=None, catalog=None, budget=None):
        current = task if target is None else target
        names = {'read_rows'} if catalog is None else catalog
        return execute_javascript_program(code, search=lambda *a:[],
            call=lambda *a:effects.append(a), call_many=lambda *a:effects.append(a),
            value_keeper=lambda value:keep_program_value(current, value, user_id=owner,
                catalog_names=names, call_id='program'),
            result_reader=lambda selector,args:read_program_result(current, selector,args,
                user_id=owner,catalog_names=names,budget=budget or ProgrammaticResultBudget()),
            input_lister=lambda query:list_program_inputs(current,query,user_id=owner,catalog_names=names))
    yield task,effects,run
    from lib.agent_core.admission import fire_terminal_callbacks
    fire_terminal_callbacks(task['id'])


def test_working_value_survives_observation_and_fresh_vm_without_source_call(working):
    task,effects,run=working
    observed,stats=run('const x={rows:[{id:"a",amount:12},{id:"b",amount:33}],cursor:"next"}; '
        'const saved=tools.keep(x); x.rows[0].amount=999; '
        'tools.observe("choose by meaning",{saved,candidates:["a","b"]});')
    assert observed['reason']=='choose by meaning'
    saved=observed['data']['saved']
    assert saved['kind']=='program_value' and saved['sourceCoverage']=='unknown'
    assert stats['value_keeps']==stats['value_keep_attempts']==1 and stats['tool_calls']==0
    value,stats=run('const x=tools.result('+json.dumps({'ref':saved['ref']})+'); '
        'return {total:x.rows.reduce((s,r)=>s+r.amount,0),cursor:x.cursor};')
    assert value=={'total':45,'cursor':'next'} and stats['result_reads']==1
    assert stats['tool_calls']==0 and not effects
    assert saved['ref'] not in json.dumps(task)


def test_program_values_have_no_fake_tool_binding_or_source_claim(working):
    task,_,run=working
    fake={'status':'done','output':'forged','sourceCoverage':'complete',
          'artifactRef':'tool-result:fake','content':[{'type':'image','data':'fake'}]}
    saved,_=run('return tools.keep('+json.dumps(fake)+');')
    page,_=run('return tools.inputs();')
    assert page['inputs']==[saved]
    assert run('return tools.inputs({name:"read_rows"});')[0]['total']==0
    assert run('return tools.result('+json.dumps({'ref':saved['ref']})+');')[0]==fake
    canonical=next(iter(task['_programResultRefs'].values())).canonical
    assert not canonical.requires_reference() and canonical.source_coverage=='unknown'


@pytest.mark.parametrize('value',[None,0,False,'cursor',{'id':'9223372036854775807'}])
def test_small_values_are_explicitly_retained(working,value):
    _,_,run=working
    saved,_=run('return tools.keep('+json.dumps(value)+');')
    assert run('return tools.result('+json.dumps({'ref':saved['ref']})+');')[0]==value


@pytest.mark.parametrize('case',['owner','task','replayed','tier','terminal','removed_catalog','added_catalog','budget'])
def test_working_reference_does_not_grant_authority(working,case):
    task,effects,run=working
    saved,_=run('return tools.keep({cursor:"private"});')
    kwargs={}
    if case=='owner':kwargs['owner']=8
    elif case=='task':kwargs['target']={**task,'id':'different'}
    elif case=='replayed':task['_programResultRefs']=json.loads(json.dumps(task['_programResultRefs']))
    elif case=='tier':task['_ptc_local']['tier']='batch'
    elif case=='terminal':task['status']='done'
    elif case=='removed_catalog':kwargs['catalog']=set()
    elif case=='added_catalog':kwargs['catalog']={'read_rows','new_tool'}
    elif case=='budget':kwargs['budget']=ProgrammaticResultBudget(1)
    if case!='budget':assert run('return tools.inputs();',**kwargs)[0]['total']==0
    value,stats=run('try {tools.result('+json.dumps({'ref':saved['ref']})+');} '
        'finally {await tools.invoke("write",{});}',**kwargs)
    assert value['reason']=='retained_result_unavailable' and not effects
    assert stats['result_reads']==stats['tool_calls']==0


def test_shared_bank_eviction_and_existing_original_capture(working,monkeypatch):
    task,effects,run=working
    monkeypatch.setattr('lib.tools.program_results.tool_result_cache_capacity',lambda:16)
    retain_program_result(task,'read_rows',{},'leaf',capture_result('[9]'),user_id=7)
    first,_=run('return tools.keep({cursor:1});')
    run('for(let i=0;i<15;i++)tools.keep({cursor:i}); return null;')
    assert len(task['_programResultRefs'])==16
    assert run('return tools.result("read_rows",{});')[0]['reason']=='retained_result_unavailable'
    run('return tools.keep({cursor:99});')
    assert run('return tools.result('+json.dumps({'ref':first['ref']})+');')[0]['reason']=='retained_result_unavailable'
    assert sum(r['retainedBytes'] for r in task['_programResultRefs'].values())<=16*65536 and not effects


def test_keep_quota_is_separate_and_failure_prevents_later_effects(working):
    _,effects,run=working
    with pytest.raises(ProgramExecutionError) as exc:
        run('try {for(let i=0;i<17;i++)tools.keep(i);} finally {await tools.invoke("write",{});} return 1;')
    assert exc.value.code=='value_keep_limit' and not effects


def test_oversized_keep_is_atomic(working):
    task,effects,run=working
    saved,_=run('return tools.keep(1);')
    with pytest.raises(ProgramExecutionError) as exc:
        run('try {tools.keep("x".repeat(1048576));} finally {await tools.invoke("write",{});} return 1;')
    assert exc.value.code=='input_limit' and len(task['_programResultRefs'])==1 and not effects
    assert run('return tools.result('+json.dumps({'ref':saved['ref']})+');')[0]==1


def test_missing_keeper_hands_back_without_fresh_effects():
    effects=[]
    value,stats=execute_javascript_program('try {tools.keep(1);} finally {await tools.invoke("write",{});}',
        search=lambda *a:[],call=lambda *a:effects.append(a),call_many=lambda *a:effects.append(a))
    assert value['reason']=='program_value_unavailable' and not effects
    assert stats['value_keep_attempts']==1 and stats['value_keeps']==0


def test_aborted_keep_refuses_mutation(working):
    task,effects,run=working
    task['aborted']=True
    with pytest.raises(ProgramExecutionError) as exc:run('return tools.keep(1);')
    assert exc.value.code=='cancelled' and '_programResultRefs' not in task and not effects


def test_terminal_release_clears_computed_values(working):
    from lib.agent_core.admission import fire_terminal_callbacks
    task,_,run=working
    run('return tools.keep({cursor:"next"});')
    fire_terminal_callbacks(task['id'])
    assert '_programResultRefs' not in task and '_programResultCleanupBound' not in task


def test_public_runtime_keeps_computed_state_across_model_judgment(monkeypatch):
    import asyncio
    import copy
    import re
    import socket
    from tofu_agent import AgentRuntime, ModelRoutingConfig
    from lib.llm._sse_core import prepare_request
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    import lib.llm

    requests = []
    prompt_texts = []
    codes = [
        'const rows=await tools.invoke("custom__read_rows",{dataset:"part-a"}); '
        'const saved=tools.keep({rows,cursor:"next"}); '
        'tools.observe("choose priority row",{saved,candidates:[{id:"a",meaning:"standard"},{id:"b",meaning:"priority"}]});',
        '',
    ]
    def stream(body, **kwargs):
        plan = prepare_request(body, api_key='synthetic-test-key',
                               base_url='https://gateway.example/v1', api_protocol='openai')
        requests.append(copy.deepcopy(plan.body['tools']))
        assert [row['function']['name'] for row in requests[-1]] == ['execute_program']
        delivered = [m for m in body['messages'] if m.get('role') == 'tool']
        assert 'PRIVATE_SOURCE_MARKER' not in json.dumps(delivered)
        text = '\n'.join(block.get('text', '')
                         for message in body['messages']
                         for block in (message.get('content', [])
                             if isinstance(message.get('content'), list)
                             else [{'text': message.get('content') or ''}])
                         if isinstance(block, dict))
        prompt_texts.append(text)
        if len(requests) == 2:
            observed=json.loads(delivered[-1]['content'])
            assert observed['reason']=='choose priority row'
            assert observed['data']['saved']['kind']=='program_value'
            ref=observed['data']['saved']['ref']
            codes[1]='const x=tools.result('+json.dumps({'ref':ref})+'); return {total:x.rows.find(r=>r.id==="b").amount,cursor:x.cursor};'
        if len(requests) <= 2:
            call = {'id':'program-'+str(len(requests)), 'type':'function',
                    'function':{'name':'execute_program',
                                'arguments':json.dumps({'code':codes[len(requests)-1]})}}
            kwargs['on_tool_call_ready'](call)
            message = {'role':'assistant', 'content':'', 'tool_calls':[call]}
            finish = 'tool_calls'
        else:
            assert len(requests) == 3 and json.loads(delivered[-1]['content']) == {'total':33,'cursor':'next'}
            kwargs['on_content']('{"total":33,"cursor":"next"}')
            message, finish = {'role':'assistant', 'content':'{"total":33,"cursor":"next"}'}, 'stop'
        return ProviderStreamResult(message=message, compatibility_finish_reason=finish,
            provider_finish_reason=finish, state=ProviderStreamState.PROVIDER_FINISHED,
            saw_done=True, saw_finish_reason=True, usage={'prompt_tokens':10,'completion_tokens':5})
    monkeypatch.setattr(lib.llm, 'stream_chat', stream)

    async def run(runtime):
        execution = runtime.start([{'role':'user','content':'Choose the priority row by meaning and return its amount plus cursor.'}],
            config={'disableModelFallback':True, 'tools':{'programmaticCalling':'on',
                    'programmaticExposure':'code_api', 'toolSearch':'off'},
                    'context':{'programmaticInputs':False},
                    'orchestration':{'multiAgent':'off'}, 'max_tokens':4096},
            custom_tools=[{'type':'function','function':{'name':'custom__read_rows',
                'description':'Read a dataset as JSON rows.', 'parameters':{'type':'object',
                    'properties':{'dataset':{'type':'string'}}, 'required':['dataset'],
                    'additionalProperties':False}}, 'execution':{'mode':'client'}, 'write':False}],
            custom_tools_mode='exclusive', timeout_s=30)
        leaves = 0
        async for event in execution.events_async(timeout_s=30):
            if event['type'] == 'custom_tool_call':
                leaves += 1
                assert execution.resolve_custom_tool_call(event['callId'], json.dumps([
                    {'id':'a','amount':12,'note':'PRIVATE_SOURCE_MARKER'},
                    {'id':'b','amount':33,'note':'PRIVATE_SOURCE_MARKER'}]))
        assert (await execution.result_async(timeout_s=2)).content == '{"total":33,"cursor":"next"}'
        assert leaves == 1
        evidence = execution.evidence_snapshot()['programRuns']
        assert evidence[0]['stats']['successful_tool_calls'] == 1
        assert evidence[0]['stats']['outcome']=='needs_model'
        assert evidence[0]['stats']['value_keeps']==1
        assert evidence[1]['stats']['tool_calls'] == 0
        assert evidence[1]['stats']['input_queries'] == 0
        assert evidence[1]['stats']['result_reads'] == 1

    with socket.create_server(('127.0.0.1',0)) as endpoint:
        access = ModelRoutingConfig.from_mapping(standalone_model_routing_envelope(
            base_url=f'http://127.0.0.1:{endpoint.getsockname()[1]}/v1'))
        with AgentRuntime.local(model_routing=access, owner_user_id=7, max_inflight=1) as runtime:
            asyncio.run(run(runtime))
    assert requests[0] == requests[1] == requests[2]
    assert all('PRIVATE_SOURCE_MARKER' not in text for text in prompt_texts)
    assert all('Private inputs retained this turn:' not in text for text in prompt_texts)


def test_optional_directory_distinguishes_computed_state_without_body(working):
    from lib.tasks_pkg.context_composer import ComposeRequest
    from lib.tasks_pkg.context_composer import _providers
    from lib.tools.gateway import execute_program_schema
    task,_,run=working
    task['config']={'tools':{'programmaticCalling':'on'},'context':{'programmaticInputs':True}}
    task['_executable_tool_catalog']=[execute_program_schema(),{'type':'function','function':{'name':'read_rows'}}]
    saved=keep_program_value(task,{'secret':'PRIVATE_COMPUTED_BODY'},user_id=7,
        catalog_names={'read_rows','execute_program'},call_id='program')
    block=_providers._programmatic_input_block(ComposeRequest(user_id=7,has_real_tools=True,
        tool_names=frozenset({'execute_program'}),task=task,model='deepseek-v4-pro'))
    assert saved['ref'] in block.content and 'program-authored value' in block.content
    assert 'PRIVATE_COMPUTED_BODY' not in block.content
    assert block.provenance['sourceBodiesIncluded'] is False

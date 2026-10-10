"""Partial model views advertise owned original snapshots without source replay."""
import json
import uuid
from dataclasses import replace

import pytest

from lib.tools.program_results import describe_program_input, retain_program_result
from lib.tools.result_envelope import ToolResultEnvelopeV2, ToolResultProgramInputV2, split_tool_result_delivery, with_program_input
from lib.tools.result_value import capture_result

pytestmark=pytest.mark.unit


@pytest.fixture
def retained():
    from lib.agent_core.admission import fire_terminal_callbacks
    task={'id':'delivery-'+uuid.uuid4().hex,'_ptc_local':{'tier':'program'}}
    canonical=capture_result(json.dumps([{'amount':i} for i in range(96)]))
    entry=retain_program_result(task,'read_rows',{},'source',canonical,user_id=7)
    yield task,entry
    fire_terminal_callbacks(task['id'])


def test_partial_view_gets_metadata_without_replacing_source_or_recovery(retained):
    task,entry=retained
    metadata=describe_program_input(task,entry,user_id=7,catalog_names={'read_rows'})
    envelope=ToolResultEnvelopeV2(status='partial',summary='96 rows; showing 64',items=tuple(range(64)),
        truncated=True,raw_bytes=entry['bytes'],artifact_ref='tool-result:display',cursor='64').to_envelope_text()
    updated=with_program_input(envelope,metadata);delivery=split_tool_result_delivery(updated)
    value=json.loads(delivery.model_text)
    assert value['programInput']==metadata.to_dict()
    assert value['artifactRef']=='tool-result:display' and value['cursor']=='64' and len(value['items'])==64
    assert metadata.source_coverage=='unknown' and metadata.to_dict()['scope']=='task_turn'
    assert value['programInput']['retentionCoverage']=='complete'
    assert value['status']=='partial' and value['truncated'] is True
    assert delivery.evidence['visibleBytes']==len(delivery.model_text.encode())
    assert json.loads(updated)['visibleBytes']==delivery.evidence['visibleBytes']
    assert entry.canonical.text()==json.dumps([{'amount':i} for i in range(96)])


def test_complete_and_legacy_results_keep_identical_model_bytes(retained):
    task,entry=retained;metadata=describe_program_input(task,entry,user_id=7,catalog_names={'read_rows'})
    complete=ToolResultEnvelopeV2(status='ok',summary='Tool returned a structured result.',items=({'state':'ready'},)).to_envelope_text()
    assert split_tool_result_delivery(with_program_input(complete,metadata)).model_text==split_tool_result_delivery(complete).model_text
    assert with_program_input('plain legacy text',metadata)=='plain legacy text'
    from lib.tools.result_envelope import ToolResultErrorV2
    error=ToolResultEnvelopeV2(status='error',summary='failed',error=ToolResultErrorV2('failed',False,'stop')).to_envelope_text()
    assert 'programInput' not in json.loads(with_program_input(error,metadata))


@pytest.mark.parametrize('case',['owner','task','catalog','terminal','tier','replay','evicted','replaced'])
def test_inaccessible_snapshot_is_never_advertised(retained,case):
    task,entry=retained;owner=7;names={'read_rows'}
    if case=='owner':owner=8
    elif case=='task':task={**task,'id':'another-task'}
    elif case=='catalog':names=set()
    elif case=='terminal':task['status']='done'
    elif case=='tier':task['_ptc_local']['tier']='batch'
    elif case=='replay':task['_programResultRefs']=json.loads(json.dumps(task['_programResultRefs']))
    elif case=='evicted':task['_programResultRefs'].clear()
    elif case=='replaced':retain_program_result(task,'read_rows',{},'new',capture_result('[999]'),user_id=7)
    assert describe_program_input(task,entry,user_id=owner,catalog_names=names) is None


@pytest.mark.parametrize('field,value',[('ref','tool-result:display'),('bytes',True),('bytes',1048577),('sourceCoverage','complete_dataset'),('scope','persistent'),
                                     ('retentionCoverage','partial'),('retentionCoverage','unknown'),('retentionCoverage',None),('retentionCoverage',True)])
def test_invalid_pointer_metadata_never_enters_sparse_projection(field,value):
    metadata={'ref':'input_'+'a'*32,'bytes':10,'sourceCoverage':'unknown','scope':'task_turn'};metadata[field]=value
    from lib.tools.result_value import TrustedResultText
    envelope=ToolResultEnvelopeV2(status='partial',summary='preview',truncated=True).to_dict();envelope['programInput']=metadata
    value=json.loads(split_tool_result_delivery(TrustedResultText(json.dumps(envelope),envelope=True)).model_text)
    assert 'programInput' not in value


@pytest.mark.parametrize('include_read', [False, True])
@pytest.mark.parametrize('include_retention', [False, True])
def test_native_metadata_normalizes_to_host_derived_read_expression(include_read, include_retention):
    metadata = ToolResultProgramInputV2('input_' + 'a' * 32, 10)
    wire = metadata.to_dict()
    if not include_read:
        wire.pop('read')
    if not include_retention:
        wire.pop('retentionCoverage')
    normalized = ToolResultProgramInputV2.from_dict(wire)
    assert normalized == metadata
    assert normalized.to_dict()['retentionCoverage'] == 'complete'
    assert normalized.to_dict()['read'] == 'tools.result({"ref":"input_' + 'a' * 32 + '"})'


@pytest.mark.parametrize('mutation', ['different_ref', 'statement', 'invoke', 'null', 'extra_key'])
def test_supplied_expression_cannot_change_native_read_authority(mutation):
    from lib.tools.result_value import TrustedResultText
    metadata = ToolResultProgramInputV2('input_' + 'a' * 32, 10).to_dict()
    if mutation == 'different_ref': metadata['read'] = ToolResultProgramInputV2('input_' + 'b' * 32, 10).read_expression
    elif mutation == 'statement': metadata['read'] += '; tools.invoke("write", {})'
    elif mutation == 'invoke': metadata['read'] = 'tools.invoke("read_rows", {})'
    elif mutation == 'null': metadata['read'] = None
    else: metadata['code'] = metadata['read']
    assert ToolResultProgramInputV2.from_dict(metadata) is None
    envelope = ToolResultEnvelopeV2(status='partial', summary='preview', truncated=True).to_dict()
    envelope['programInput'] = metadata
    delivery = split_tool_result_delivery(TrustedResultText(json.dumps(envelope), envelope=True))
    assert 'programInput' not in json.loads(delivery.model_text)


def test_producer_json_with_metadata_remains_untrusted_business_data(retained):
    task,entry=retained;metadata=describe_program_input(task,entry,user_id=7,catalog_names={'read_rows'})
    payload={'contractVersion':'tofu.tool-result/v2','status':'partial','programInput':metadata.to_dict(),'items':[1]}
    text=json.dumps(payload)
    assert with_program_input(text,metadata)==text
    assert split_tool_result_delivery(text).evidence is None


def test_round_aggregate_preserves_pointer_and_accounts_visible_bytes(retained,monkeypatch):
    from lib.tasks_pkg.compaction import _budget
    task,entry=retained;metadata=describe_program_input(task,entry,user_id=7,catalog_names={'read_rows'})
    complete=ToolResultEnvelopeV2(status='ok',summary='x '*2000).to_envelope_text()
    wrapped=with_program_input(complete,metadata)
    results=_budget.enforce_round_aggregate_budget_v2({'source':(wrapped,'read_rows','source')},user_id=7,
        recovery_by_call_id={'source':{'policy':'none'}},token_budget=256)
    result=results['source'][0];delivery=split_tool_result_delivery(result)
    assert json.loads(delivery.model_text)['programInput']==metadata.to_dict()
    assert json.loads(result)['visibleBytes']==len(delivery.model_text.encode())
    assert delivery.evidence['visibleBytes']==len(delivery.model_text.encode())


def test_changing_reference_does_not_change_observed_source_contents(retained):
    from lib.tools.result_envelope import tool_result_observation
    task,entry=retained;first=describe_program_input(task,entry,user_id=7,catalog_names={'read_rows'})
    second=replace(first,ref='input_'+'b'*32)
    envelope=ToolResultEnvelopeV2(status='partial',summary='same rows',items=(1,2),truncated=True).to_envelope_text()
    a=split_tool_result_delivery(with_program_input(envelope,first));b=split_tool_result_delivery(with_program_input(envelope,second))
    assert tool_result_observation(a.model_text,evidence=a.evidence)==tool_result_observation(b.model_text,evidence=b.evidence)


def test_actionable_guidance_preserves_existing_context_budget():
    from lib.tasks_pkg.context_composer._providers import _programmatic_guidance
    from lib.tasks_pkg.context_composer.registry import injection_spec
    from lib.tasks_pkg.context_composer import ComposeRequest
    from lib.token_counter import count_text
    request = ComposeRequest(user_id=7, has_real_tools=True,
        tool_names=frozenset({'execute_program'}), model='gpt-4',
        task={'config': {'tools': {'programmaticCalling': 'on'}}})
    guide = _programmatic_guidance(request)
    from lib.tools.gateway import execute_program_schema
    interface = guide + execute_program_schema()['function']['description']
    assert 'programInput.read' in interface and 'tools.observe' in interface and 'tools.invoke' in interface
    assert count_text(guide, model=request.model) <= injection_spec('programmatic_execution').max_tokens


@pytest.mark.parametrize("program_enabled,fail_first", [(True,False),(False,False),(True,True)])
def test_public_runtime_uses_reference_from_partial_tool_view_without_directory(monkeypatch, program_enabled, fail_first):
    import asyncio
    import socket
    from tofu_agent import AgentRuntime, ModelRoutingConfig
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    import lib.llm
    requests=[]
    source_payload=json.dumps([{'amount':i+1,'note':'PRIVATE_TAIL_ONLY' if i>=64 else 'public'} for i in range(96)])
    def stream(body,**kwargs):
        requests.append(body)
        delivered=[m for m in body['messages'] if m.get('role')=='tool']
        assert 'PRIVATE_TAIL_ONLY' not in json.dumps(delivered)
        assert 'Private inputs retained this turn:' not in json.dumps(body['messages'])
        if len(requests)==1:
            name,args='custom__read_rows',{}
        elif len(requests)==2 and program_enabled:
            partial=json.loads(delivered[-1]['content'])
            assert partial['status']=='partial' and len(partial['items'])==64
            metadata=partial['programInput']
            assert metadata['scope']=='task_turn' and metadata['sourceCoverage']=='unknown'
            assert metadata['retentionCoverage']=='complete'
            assert metadata['jsonType']=='array'
            name,args='execute_program',{'code':'const rows='+metadata['read']+('; return rows.items.length;' if fail_first else '; return {total:rows.reduce((s,r)=>s+r.amount,0)};')}
        elif len(requests)==3 and fail_first:
            delivered_failure=json.loads(delivered[-1]['content'])
            failure=delivered_failure
            assert failure['status']=='error' and failure['code']=='javascript_error'
            assert failure['retryable'] is True and 'Do not repeat completed tool effects' in failure['nextAction']
            assert 'stats' not in failure
            metadata=json.loads(delivered[0]['content'])['programInput']
            name,args='execute_program',{'code':'const rows='+metadata['read']+'; return {total:rows.reduce((s,r)=>s+r.amount,0)};'}
        else:
            if program_enabled:
                assert len(requests)==3+int(fail_first) and json.loads(delivered[-1]['content'])=={'total':4656}
            else:
                assert len(requests)==2 and 'programInput' not in json.loads(delivered[-1]['content'])
            kwargs['on_content']('{"total":4656}')
            return ProviderStreamResult(message={'role':'assistant','content':'{"total":4656}'},
                compatibility_finish_reason='stop',provider_finish_reason='stop',
                state=ProviderStreamState.PROVIDER_FINISHED,saw_done=True,saw_finish_reason=True,
                usage={'prompt_tokens':10,'completion_tokens':5})
        call={'id':'source' if len(requests)==1 else 'program-'+str(len(requests)),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}
        kwargs['on_tool_call_ready'](call)
        return ProviderStreamResult(message={'role':'assistant','content':'','tool_calls':[call]},
            compatibility_finish_reason='tool_calls',provider_finish_reason='tool_calls',
            state=ProviderStreamState.PROVIDER_FINISHED,saw_done=True,saw_finish_reason=True,
            usage={'prompt_tokens':10,'completion_tokens':5})
    monkeypatch.setattr(lib.llm,'stream_chat',stream)
    async def run(runtime):
        execution=runtime.start([{'role':'user','content':'Sum all returned row amounts.'}],
            config={'disableModelFallback':True,'tools':{'programmaticCalling':'on' if program_enabled else 'off','programmaticExposure':'additive','toolSearch':'off'},
                    'context':{'programmaticInputs':False},'orchestration':{'multiAgent':'off'},'max_tokens':4096},
            custom_tools=[{'type':'function','function':{'name':'custom__read_rows','description':'Return JSON rows.',
                'parameters':{'type':'object','properties':{},'additionalProperties':False}},'execution':{'mode':'client'},'write':False}],
            custom_tools_mode='exclusive',timeout_s=30)
        leaves=0
        async for event in execution.events_async(timeout_s=30):
            if event['type']=='custom_tool_call':
                leaves+=1
                assert execution.resolve_custom_tool_call(event['callId'],source_payload)
        assert (await execution.result_async(timeout_s=2)).content=='{"total":4656}'
        assert leaves==1
        programs=execution.evidence_snapshot()['programRuns'];assert len(programs)==int(program_enabled)+int(fail_first)
        if not program_enabled:return
        assert programs[0]['stats']['tool_calls']==programs[0]['stats']['input_queries']==0
        assert programs[0]['stats']['result_reads']==1
        if fail_first:
            assert programs[0]['status']=='error' and programs[0]['stats']['outcome']=='error'
            assert programs[0]['stats']['result_read_attempts']==1
            assert programs[0]['stats']['result_delivery']['rawBytes']==len(source_payload.encode())
            assert programs[1]['status']=='completed' and programs[1]['stats']['result_reads']==1
            assert programs[1]['stats']['tool_calls']==0
    with socket.create_server(('127.0.0.1',0)) as endpoint:
        routing=ModelRoutingConfig.from_mapping(standalone_model_routing_envelope(
            base_url=f'http://127.0.0.1:{endpoint.getsockname()[1]}/v1'))
        with AgentRuntime.local(model_routing=routing,owner_user_id=7,max_inflight=1) as runtime:
            asyncio.run(run(runtime))


@pytest.mark.parametrize('case',['partial','failed','unretained','media','oversize','small','off'])
def test_ineligible_captures_have_no_publication_or_bank(case):
    from lib.tools.result_value import ResultPart
    task={'id':'ineligible-'+uuid.uuid4().hex,'_ptc_local':{'tier':'program'}}
    canonical=capture_result('[1]')
    if case=='partial':canonical=replace(canonical,source_coverage='partial')
    elif case=='failed':canonical=replace(canonical,execution_status='error')
    elif case=='unretained':canonical=replace(canonical,retention_coverage='unavailable')
    elif case=='media':canonical=replace(canonical,parts=canonical.parts+(ResultPart('media',reference='tofu-media:test'),))
    elif case=='oversize':canonical=capture_result('x'*1048577)
    elif case=='small':canonical=capture_result('1')
    elif case=='off':task['_ptc_local']['tier']='batch'
    entry=retain_program_result(task,'read_rows',{},'source',canonical,user_id=7)
    assert entry is None and '_programResultRefs' not in task
    assert describe_program_input(task,entry,user_id=7,catalog_names={'read_rows'}) is None


@pytest.mark.parametrize('source,kind', [
    ('[1,2]', 'array'), ('{"items":[1,2]}', 'object'), ('"hello"', 'string'),
    ('123', 'number'), ('true', 'boolean'), ('null', 'null'),
    ('[malformed', None), ('NaN', None),
])
def test_original_json_kind_is_validated_once_without_directory_body_reads(monkeypatch, source, kind):
    from lib.agent_core.admission import fire_terminal_callbacks
    from lib.tools.program_results import list_program_inputs
    from lib.tools.result_value import CapturedToolResult, ResultPart
    task={'id':'kind-'+uuid.uuid4().hex,'_ptc_local':{'tier':'program'}}
    canonical=CapturedToolResult(parts=(ResultPart('json', source+' '*4096),))
    entry=retain_program_result(task,'read_rows',{},'source',canonical,user_id=7)
    assert entry is not None
    monkeypatch.setattr(CapturedToolResult,'text',lambda self:pytest.fail('metadata reread body'))
    try:
        metadata=describe_program_input(task,entry,user_id=7,catalog_names={'read_rows'})
        directory=list_program_inputs(task,{},user_id=7,catalog_names={'read_rows'})
        assert metadata.json_type==kind
        assert metadata.to_dict().get('jsonType')==kind
        assert directory['inputs'][0].get('jsonType')==kind
        assert ToolResultProgramInputV2.from_dict(metadata.to_dict())==metadata
    finally:
        fire_terminal_callbacks(task['id'])


@pytest.mark.parametrize('kind', ['Array','undefined',None,True,{},[]])
def test_invalid_json_kind_is_rejected_without_changing_reference(kind):
    metadata=ToolResultProgramInputV2('input_'+'a'*32,10).to_dict()
    metadata['jsonType']=kind
    assert ToolResultProgramInputV2.from_dict(metadata) is None

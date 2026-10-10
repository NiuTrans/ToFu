"""Adaptive pack identity, observed navigation, source proof and public SDK flow."""
import asyncio
import json
import socket
from pathlib import Path

import pytest

from evaluations.programmatic_pilot.runner import paired_report, preflight, run_task
from evaluations.programmatic_pilot.workloads import canonical, digest, oracle, read_world, workload_pack

pytestmark = pytest.mark.unit


def test_adaptive_selection_preserves_frozen_baseline_and_controls():
    baseline=workload_pack()
    assert digest([w.__dict__ for w in baseline])=='5bc305d67c463e731d95de64febfef9f518c0e5596e51659d1d7ac033ef4668b'
    tasks=workload_pack('adaptive')
    assert {w.family for w in tasks}=={'observed_dependency','semantic_computation','semantic_judgment','small_result'}
    assert tasks[-2:]==baseline[-2:]
    assert [w.fingerprint for w in tasks]==[w.fingerprint for w in workload_pack('adaptive')]
    for w in tasks:
        assert oracle(w,w.expected_json)
        assert 'tools.keep' not in w.instruction and 'execute_program' not in w.instruction
    with pytest.raises(ValueError):workload_pack('unregistered')


def test_navigation_resolves_returned_names_and_fields_with_complete_pagination():
    task=workload_pack('adaptive')[0]
    entry=json.loads(read_world(task,'custom__read_dataset',{'dataset':'entry'}))
    descriptor=json.loads(read_world(task,'custom__read_dataset',{'dataset':entry['descriptorDataset']}))
    records=[]
    for offset in range(0,120,24):
        records+=json.loads(read_world(task,'custom__read_dataset',{'dataset':descriptor['datasetName'],'offset':offset,'limit':24}))
    selected=[row for row in records if row[descriptor['statusField']]==descriptor['requiredStatus']]
    answer={'count':len(selected),'total':sum(row[descriptor['amountField']] for row in selected)}
    assert oracle(task,canonical(answer)) and len(records)==96
    assert descriptor['datasetName'] not in task.instruction
    assert descriptor['amountField'] not in task.instruction


def test_semantic_join_requires_posted_payments_for_selected_agreement():
    task=workload_pack('adaptive')[1];world=json.loads(task.world_json)
    selected=[r for r in world['payments'] if r['agreementId']=='beta' and r['status']=='posted']
    assert oracle(task,canonical({'id':'beta','count':len(selected),'total':sum(r['amount'] for r in selected)}))
    wrong=[r for r in world['payments'] if r['agreementId']=='gamma' and r['status']=='posted']
    assert not oracle(task,canonical({'id':'gamma','count':len(wrong),'total':sum(r['amount'] for r in wrong)}))
    all_beta=[r for r in world['payments'] if r['agreementId']=='beta']
    assert not oracle(task,canonical({'id':'beta','count':len(all_beta),'total':sum(r['amount'] for r in all_beta)}))


def test_selected_pack_enters_preflight_and_cannot_mix_reports():
    root=Path(__file__).resolve().parents[2]
    manifest=preflight(root,thinking='medium',trials=1,timeout_s=180,pack='adaptive')
    assert manifest['taskPack']=='adaptive'
    assert manifest['taskPackSha256']==digest([w.__dict__ for w in workload_pack('adaptive')])
    assert manifest['infrastructureRetries']==0
    rows=[{'taskId':w.task_id,'arm':arm,'trial':0,'oraclePassed':True,
           'programCount':0,'wallMs':100,'costUsd':None}
          for w in workload_pack('adaptive') for arm in ('direct','programmatic')]
    report=paired_report(rows,trials=1,pack='adaptive')
    assert report['allOraclesPass'] and report['taskPack']=='adaptive'
    with pytest.raises(ValueError):paired_report(rows,trials=1)
    with pytest.raises(ValueError):paired_report(rows[:-1],trials=1,pack='adaptive')


@pytest.mark.parametrize('index',[0,1])
def test_exact_adaptive_answer_without_complete_reads_does_not_pass(tmp_path,index):
    from types import SimpleNamespace
    task=workload_pack('adaptive')[index]
    class Execution:
        async def events_async(self,**kwargs):
            yield {'type':'custom_tool_call','callId':'partial','toolName':'custom__read_dataset',
                   'arguments':{'dataset':next(iter(json.loads(task.world_json)))}}
        def resolve_custom_tool_call(self,*args,**kwargs):return True
        async def result_async(self,**kwargs):return SimpleNamespace(status='done',finish_reason='stop',content=task.expected_json)
        def evidence_snapshot(self):return {'status':'done','apiRounds':[],'programRuns':[]}
    class Runtime:
        def start(self,*args,**kwargs):return Execution()
    record=asyncio.run(run_task(Runtime(),task,'programmatic',0,tmp_path,'medium',30,None))
    assert record['runnerFailure'] is None and not record['requiredReadsComplete'] and not record['oraclePassed']


@pytest.mark.parametrize('index',[0,1])
def test_public_runtime_executes_observed_navigation_or_semantic_handoff(tmp_path,monkeypatch,index):
    from tofu_agent import AgentRuntime, ModelRoutingConfig
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    from tests.support.standalone_model_routing import standalone_model_routing_envelope
    import lib.llm
    task=workload_pack('adaptive')[index];requests=[]
    navigation=(
        'const e=await tools.invoke("custom__read_dataset",{dataset:"entry"}); '
        'const d=await tools.invoke("custom__read_dataset",{dataset:e.descriptorDataset}); '
        'const r=await tools.invoke("custom__read_dataset",{dataset:d.datasetName}); '
        'const selected=r.filter(x=>x[d.statusField]===d.requiredStatus); '
        'return {count:selected.length,total:selected.reduce((s,x)=>s+x[d.amountField],0)};')
    first=(
        'const agreements=await tools.invoke("custom__read_dataset",{dataset:"agreements"}); '
        'const rows=await tools.invoke("custom__read_dataset",{dataset:"payments"}); '
        'const totals={}; for(const r of rows)if(r.status==="posted") { '
        'const t=totals[r.agreementId]??(totals[r.agreementId]={count:0,total:0}); t.count++;t.total+=r.amount;} '
        'const saved=tools.keep(totals); tools.observe("choose renewal clause",{saved,agreements});')
    def stream(body,**kwargs):
        requests.append(body)
        delivered=[m for m in body['messages'] if m.get('role')=='tool']
        assert 'Historical supporting detail.' not in canonical(delivered)
        assert 'Supporting record detail.' not in canonical(delivered)
        if len(requests)==1:code=navigation if index==0 else first
        elif index==1 and len(requests)==2:
            observed=json.loads(delivered[-1]['content']);assert observed['reason']=='choose renewal clause'
            code='const t=tools.result('+canonical({'ref':observed['data']['saved']['ref']})+'); return {id:"beta",...t.beta};'
        else:
            assert json.loads(delivered[-1]['content'])==json.loads(task.expected_json)
            kwargs['on_content'](task.expected_json)
            return ProviderStreamResult(message={'role':'assistant','content':task.expected_json},
                compatibility_finish_reason='stop',provider_finish_reason='stop',
                state=ProviderStreamState.PROVIDER_FINISHED,saw_done=True,saw_finish_reason=True,
                usage={'prompt_tokens':10,'completion_tokens':5})
        call={'id':'program-'+str(len(requests)),'type':'function','function':{'name':'execute_program','arguments':canonical({'code':code})}}
        kwargs['on_tool_call_ready'](call)
        return ProviderStreamResult(message={'role':'assistant','content':'','tool_calls':[call]},
            compatibility_finish_reason='tool_calls',provider_finish_reason='tool_calls',
            state=ProviderStreamState.PROVIDER_FINISHED,saw_done=True,saw_finish_reason=True,
            usage={'prompt_tokens':10,'completion_tokens':5})
    monkeypatch.setattr(lib.llm,'stream_chat',stream)
    with socket.create_server(('127.0.0.1',0)) as endpoint:
        routing=ModelRoutingConfig.from_mapping(standalone_model_routing_envelope(
            base_url=f'http://127.0.0.1:{endpoint.getsockname()[1]}/v1'))
        with AgentRuntime.local(model_routing=routing,owner_user_id=7,max_inflight=1) as runtime:
            record=asyncio.run(run_task(runtime,task,'programmatic',0,tmp_path,'medium',30,None))
    assert record['oraclePassed'] and record['requiredReadsComplete']
    assert record['leafCallCount']==(3 if index==0 else 2)
    assert record['programCount']==(1 if index==0 else 2)
    if index==1:
        assert record['programStats'][0]['value_keeps']==1
        assert record['programStats'][1]['result_reads']==1
        assert record['programStats'][1]['tool_calls']==0


@pytest.mark.parametrize('transport',['sdk','native_api'])
def test_both_clis_select_adaptive_pack_without_live_dispatch(capsys,transport):
    from evaluations.programmatic_pilot import runner,native_api
    entry=runner.main if transport=='sdk' else native_api.main
    assert entry(['--pack','adaptive'])==0
    manifest=json.loads(capsys.readouterr().out)
    assert manifest['taskPack']=='adaptive'
    assert manifest['taskPackSha256']==digest([w.__dict__ for w in workload_pack('adaptive')])

"""Failed live-probe observations retain costs and bound cancellation to their run."""
import json
from copy import deepcopy
import pytest
from evaluations.evolution import soak
from tests.evolution.test_evolution_soak import _run

pytestmark = pytest.mark.unit


def rig(cleanup='confirmed'):
    clock=[0.0];calls=[];reads=0;cancelled=False
    def request(method,path,body,key):
        nonlocal reads,cancelled
        calls.append((method,path,key))
        if path.endswith('/cancel'):
            cancelled=True
            if cleanup=='unavailable':raise soak.urllib.error.URLError('cancel unavailable')
            value=_run('cancelled',tokens=35);value['actuals']['tool_calls']=5
            if cleanup=='foreign':value.update(run_id='foreign',actuals={'tokens':999,'tool_calls':999})
            if cleanup=='wrong_status':value['status']='executing'
            return {'run':value}
        if method=='POST':return {'run':_run('executing',tokens=7)}
        if cancelled:
            value=_run('cancelled',tokens=35);value['actuals']['tool_calls']=5
            if cleanup=='foreign_readback':value.update(run_id='foreign',actuals={'tokens':999,'tool_calls':999})
            return {'run':value}
        reads+=1
        value=_run('reviewing',tokens=31 if reads==1 else 29)
        value['actuals']['tool_calls']=4 if reads==1 else 3
        return {'run':value}
    def observe():
        return soak.observe_run(request,objective='Measure this objective',key='same',poll_seconds=1,
            timeout_seconds=20,max_tokens=100,now=lambda:clock[0],
            sleep=lambda sec:clock.__setitem__(0,clock[0]+sec))
    return request,observe,calls,clock


def test_corrupt_counter_retains_observed_cost_and_confirms_owned_cancellation():
    request,observe,calls,clock=rig()
    with pytest.raises(soak.ProbeFailure,match='tokens counter decreased') as failure:observe()
    receipt=failure.value.receipt
    assert receipt['status']=='probe_error' and not receipt['complete']
    assert receipt['run_id']=='run-probe' and receipt['producer_status']=='cancelled'
    assert receipt['tokens']==35 and receipt['tool_calls']==5
    assert receipt['provider_usage_unknown'] and receipt['cancellation_confirmed']
    assert receipt['admitted_max_tokens']==100
    assert calls.count(('POST','/api/v4/evolution/runs/run-probe/cancel',''))==1
    assert calls[-1][:2]==('GET','/api/v4/evolution/runs/run-probe')


@pytest.mark.parametrize('cleanup',['unavailable','foreign','wrong_status','foreign_readback'])
def test_unconfirmed_cleanup_keeps_cost_lower_bound_and_never_cancels_foreign_run(cleanup):
    request,observe,calls,clock=rig(cleanup)
    with pytest.raises(soak.ProbeFailure) as failure:observe()
    receipt=failure.value.receipt
    assert not receipt['cancellation_confirmed'] and receipt['provider_usage_unknown']
    assert receipt['tokens']==(31 if cleanup in ('unavailable','foreign') else 35)
    assert receipt['tokens']!=999 and receipt['run_id']=='run-probe'
    assert [path for method,path,key in calls if path.endswith('/cancel')]==['/api/v4/evolution/runs/run-probe/cancel']
    assert receipt['cleanup_error']


def test_terminal_receipt_failure_keeps_cost_without_cancelling_completed_work():
    value=_run('completed',tokens=53,receipts=[{'task_id':'same'},{'task_id':'same'}]);calls=[]
    def request(method,path,body,key):calls.append((method,path));return {'run':deepcopy(value)}
    with pytest.raises(soak.ProbeFailure,match='Duplicate stage receipts') as failure:
        soak.observe_run(request,objective='Verify original work',key='same',poll_seconds=1,timeout_seconds=20)
    receipt=failure.value.receipt
    assert receipt['producer_status']=='completed' and receipt['tokens']==53
    assert not receipt['complete'] and receipt['provider_usage_unknown']
    assert not any(path.endswith('/cancel') for method,path in calls)


def test_cli_failed_probe_writes_known_cost_and_stops_submissions(monkeypatch,tmp_path,capsys):
    request,observe,calls,clock=rig()
    monkeypatch.setattr(soak,'_request',lambda base,token,method,path,body=None,*,idempotency_key='':request(method,path,body,idempotency_key))
    output=tmp_path/'failed.jsonl'
    code=soak.main(['--live','--runs','3','--poll-seconds','1','--max-total-tokens','100',
        '--reserve-tokens-per-run','0','--output',str(output)],now=lambda:clock[0],
        sleep=lambda sec:clock.__setitem__(0,clock[0]+sec))
    rows=[json.loads(line) for line in output.read_text().splitlines()]
    summary=json.loads(capsys.readouterr().out)
    assert code==1 and len(rows)==1 and rows[0]['harness_version']==7
    assert rows[0]['tokens']==summary['tokens']==35 and summary['tokens_are_lower_bound']
    assert summary['unknown_usage_runs']==1 and summary['completed']==0
    assert summary['p50_tokens_per_completed_goal'] is None
    assert len([call for call in calls if call[1]=='/api/v4/evolution/runs'])==2


@pytest.mark.parametrize('invalid',[True,-1,1.5,float('inf'),None])
def test_malformed_accounting_never_becomes_zero_or_a_success(invalid):
    calls=[]
    def request(method,path,body,key):
        calls.append((method,path))
        value=_run('executing',tokens=19)
        if path.endswith('/cancel'):value['status']='cancelled'
        elif method=='GET':value['status']='cancelled'
        else:value['actuals']['tool_calls']=invalid
        return {'run':value}
    with pytest.raises(soak.ProbeFailure) as failure:
        soak.observe_run(request,objective='Observe',key='same',poll_seconds=1,timeout_seconds=20)
    assert failure.value.receipt['tokens']==19
    assert failure.value.receipt['provider_usage_unknown'] and not failure.value.receipt['complete']
    assert failure.value.receipt['cancellation_confirmed']


def test_confirmed_cleanup_and_unknown_usage_are_independent_facts():
    base,observe,calls,clock=rig()
    def request(method,path,body,key):
        response=base(method,path,body,key)
        if response['run']['status']=='cancelled':response['run']['actuals']={'tokens':None,'tool_calls':True}
        return response
    with pytest.raises(soak.ProbeFailure) as failure:
        soak.observe_run(request,objective='Observe original cost',key='same',poll_seconds=1,
            timeout_seconds=20,now=lambda:clock[0],sleep=lambda sec:clock.__setitem__(0,clock[0]+sec))
    receipt=failure.value.receipt
    assert receipt['cancellation_confirmed'] and receipt['provider_usage_unknown']
    assert receipt['tokens']==31 and receipt['tool_calls']==4
    assert receipt['producer_status']=='cancelled'


@pytest.mark.parametrize('accounting',[None,{}, {'tokens':23}])
def test_missing_accounting_is_unknown_instead_of_zero(accounting):
    def request(method,path,body,key):
        value=_run('cancelled' if method=='GET' or path.endswith('/cancel') else 'executing',tokens=23)
        if method=='POST' and path=='/api/v4/evolution/runs':value['actuals']=accounting
        return {'run':value}
    with pytest.raises(soak.ProbeFailure) as failure:
        soak.observe_run(request,objective='Observe',key='same',poll_seconds=1,timeout_seconds=20)
    assert failure.value.receipt['provider_usage_unknown'] and not failure.value.receipt['complete']


def test_one_invalid_counter_does_not_discard_another_valid_observation():
    def request(method,path,body,key):
        value=_run('cancelled' if method=='GET' or path.endswith('/cancel') else 'executing',tokens=23)
        if method=='POST' and path=='/api/v4/evolution/runs':value['actuals']={'tokens':None,'tool_calls':8}
        return {'run':value}
    with pytest.raises(soak.ProbeFailure) as failure:
        soak.observe_run(request,objective='Observe',key='same',poll_seconds=1,timeout_seconds=20)
    assert failure.value.receipt['tokens']==23 and failure.value.receipt['tool_calls']==8
    assert failure.value.receipt['provider_usage_unknown']

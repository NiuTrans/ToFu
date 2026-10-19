"""HTTP ROOT performance admission cannot erase paid work or unfinished cleanup."""
import copy
import pytest
from benchmarks.native_application_root import compare

pytestmark=pytest.mark.unit


@pytest.fixture
def observations():
    quote={'currency':'USD','model':'alpha','provider':'provider-a',
           'rates':{'input':5.,'output':10.},'selectedPromptTokens':3}
    usage={'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}
    evidence={'request':{'model':'alpha-wire','messages':[{'role':'user','content':'question'}]},
        'content':'answer','usage':usage,'api_rounds':[{'round':1,'model':'alpha','usage':usage,
        'cost':{'costCny':.0003,'pricingSnapshot':quote,'pricingSource':'resolved_price'}}],
        'budget_consumption':[5,0,True],'task_status':'done','attempt_status':'completed',
        'turn_status':'completed','replay_status':'done','route_retired':True,'worker_retired':True}
    rows=[{'arm':arm,'evidence':copy.deepcopy(evidence),'joined_cleanup':True,'archive_sha256':'a'*64}
          for arm in ('python','native')]
    rows[1]['evidence']['api_rounds'][0]['cost']['pricingSource']='native_resolved_price'
    return rows


def test_comparison_retains_request_usage_quote_money_and_consumption(observations):
    assert compare(observations)['measurement_permitted'] is True
    assert compare(observations)['performance_admitted'] is False
    assert compare(observations)['default_selection_admitted'] is False


@pytest.mark.parametrize('field',['request','usage','cost','quote','budget'])
def test_changed_paid_work_never_permits_timing(observations,field):
    e=observations[1]['evidence']
    if field=='request':e['request']['messages'][0]['content']='different'
    elif field=='usage':e['usage']['prompt_tokens']=4
    elif field=='cost':e['api_rounds'][0]['cost']['costCny']=.0004
    elif field=='quote':e['api_rounds'][0]['cost']['pricingSnapshot']['rates']['input']=6.
    else:e['budget_consumption'][0]=6
    assert compare(observations)['measurement_permitted'] is False


@pytest.mark.parametrize('field',['task_status','worker_retired','route_retired','budget','price','archive','cleanup'])
def test_unfinished_or_unproven_observations_refuse(observations,field):
    e=observations[1]['evidence']
    if field=='task_status':e[field]='failed'
    elif field in {'worker_retired','route_retired'}:e[field]=False
    elif field=='budget':e['budget_consumption'][2]=False
    elif field=='price':e['api_rounds'][0]['cost']['pricingSource']='default_estimate'
    elif field=='archive':observations[1]['archive_sha256']='b'*64
    else:observations[1]['joined_cleanup']=False
    with pytest.raises(ValueError):compare(observations)


def test_telemetry_is_retained_in_receipts_but_does_not_change_work(observations):
    observations[0]['evidence']['usage']['stream_elapsed_ms']=99
    observations[1]['evidence']['api_rounds'][0]['usage']['_transport']='native'
    assert compare(observations)['measurement_permitted'] is True
    assert observations[0]['evidence']['usage']['stream_elapsed_ms']==99


def cohort_rows(observations):
    from benchmarks.native_application_root import comparable_work,digest
    rows=copy.deepcopy(observations)
    for index,row in enumerate(rows):
        row.update(work_sha256=digest(comparable_work(row)),mode='warm',backend='sqlite',scenario='text',
            process_identity=f'{index}:fixture',profiler_enabled=False,tracemalloc_enabled=False,warmups=20,
            first_operation={'wall_ns':1000000,'cpu_ns':1000000},
            raw=[{'wall_ns':1000000,'cpu_ns':1000000} for _ in range(100)])
    return rows


def test_timing_collector_never_infers_missing_cold_or_memory(observations):
    from benchmarks.native_application_root_measure import timing_summary
    result=timing_summary({'warm':cohort_rows(observations),'cold':[]})
    assert 'cold samples incomplete' in result['issues']
    assert 'memory pairs incomplete' in result['issues']
    assert result['whole_project_admitted'] is result['default_selection_admitted'] is False


def test_timing_collector_refuses_forged_work_signature(observations):
    from benchmarks.native_application_root_measure import timing_summary
    rows=cohort_rows(observations);rows[1]['work_sha256']='foreign'
    with pytest.raises(ValueError,match='signature differs'):timing_summary({'warm':rows,'cold':[]})


def test_timing_collector_refuses_changed_money_even_with_recomputed_signature(observations):
    from benchmarks.native_application_root_measure import timing_summary
    from benchmarks.native_application_root import comparable_work,digest
    rows=cohort_rows(observations);rows[1]['evidence']['api_rounds'][0]['cost']['costCny']=.0004
    rows[1]['work_sha256']=digest(comparable_work(rows[1]))
    with pytest.raises(ValueError,match='different paid work'):timing_summary({'warm':rows,'cold':[]})


def test_stage_capture_preserves_identity_and_exception_without_retaining_payload():
    from benchmarks.native_application_stages import Stages
    stages=Stages();value=object();failure=KeyboardInterrupt('stop')
    assert stages.sync('return',lambda argument:argument)(value) is value
    def raise_same():raise failure
    with pytest.raises(KeyboardInterrupt) as caught:stages.sync('raise',raise_same)()
    assert caught.value is failure
    rows=stages.snapshot();assert len(rows)==2
    assert {row['outcome'] for row in rows}=={'returned','raised'}
    assert all(set(row)=={'name','start_ns','wall_ns','thread_cpu_ns','outcome'} for row in rows)
    assert all(row['wall_ns']>=0 and row['thread_cpu_ns']>=0 for row in rows)


def test_async_stage_capture_joins_cancellation_and_records_no_thread_cpu():
    import asyncio
    from benchmarks.native_application_stages import Stages
    async def run():
        stages=Stages();failure=asyncio.CancelledError()
        async def stopped():raise failure
        with pytest.raises(asyncio.CancelledError) as caught:
            await stages.asynchronous('cancel',stopped)()
        assert caught.value is failure
        assert stages.snapshot()[0]['outcome']=='raised'
        assert stages.snapshot()[0]['thread_cpu_ns'] is None
    asyncio.run(run())


def test_stage_diagnostics_cannot_be_admitted_as_timing(observations):
    from benchmarks.native_application_root_measure import timing_summary
    rows=cohort_rows(observations);rows[0]['diagnostic_stages_enabled']=True
    with pytest.raises(ValueError,match='cannot grant timing'):timing_summary({'warm':rows,'cold':[]})


def test_storage_diagnostics_cannot_grant_timing_without_stage_marker(observations):
    from benchmarks.native_application_root_measure import timing_summary
    rows=cohort_rows(observations)
    rows[0]['diagnostic_storage_metrics']={'writer':{'commit_latency':{}}}
    with pytest.raises(ValueError,match='cannot grant timing'):
        timing_summary({'warm':rows,'cold':[]})


def test_diagnostic_sidecar_launch_preserves_owned_process_channels():
    from unittest.mock import patch
    import subprocess
    from benchmarks.native_sidecar_stages import diagnostic_sidecar_launch
    command=['python','-m','lib.storage_authority_sql','owned-argument']
    channels={'stdin':object(),'stdout':object(),'stderr':object(),'env':{'fixture':'value'},'close_fds':True}
    process=object()
    with patch.object(subprocess,'Popen',return_value=process) as original:
        with diagnostic_sidecar_launch():
            assert subprocess.Popen(command,**channels) is process
            changed=original.call_args
            assert changed.args[0]==['python','-m','benchmarks.native_sidecar_stages','owned-argument']
            assert changed.kwargs==channels
            foreign=['python','-m','unrelated.module']
            assert subprocess.Popen(foreign,**channels) is process
            assert original.call_args.args[0] is foreign
        assert subprocess.Popen is original
    assert command==['python','-m','lib.storage_authority_sql','owned-argument']


def test_sidecar_diagnostics_preserve_callback_result_and_failure(monkeypatch):
    from contextlib import ExitStack
    from lib.storage_authority_sql import server
    from benchmarks.native_sidecar_stages import install_sidecar_stages
    value=object();failure=KeyboardInterrupt('fixture')
    def resolve(operation,kind,payload):
        def callback(session):
            assert session is value
            if payload['fail']:raise failure
            return value
        return True,callback,3.0
    monkeypatch.setattr(server,'resolve_operation_contract',resolve)
    with ExitStack() as stack:
        stages=install_sidecar_stages(stack)
        receipt,callback,timeout=server.resolve_operation_contract('fixture.operation','command',{'fail':False})
        assert receipt is True and timeout==3.0 and callback(value) is value
        _,callback,_=server.resolve_operation_contract('fixture.operation','command',{'fail':True})
        with pytest.raises(KeyboardInterrupt) as caught:callback(value)
        assert caught.value is failure
        assert all(set(row)=={'name','start_ns','wall_ns','thread_cpu_ns','outcome'} for row in stages.snapshot())
    assert server.resolve_operation_contract is resolve


@pytest.mark.parametrize('mismatch',['backend','engine','missing'])
def test_http_comparison_refuses_mixed_storage_authority(observations,mismatch):
    authority={'backend':'tofudb','transport':'daemon','engine_sha256':'a'*64}
    observations[0]['storage_authority']=dict(authority)
    observations[1]['storage_authority']=dict(authority)
    if mismatch=='backend':observations[1]['storage_authority']['backend']='sqlite'
    elif mismatch=='engine':observations[1]['storage_authority']['engine_sha256']='b'*64
    else:observations[1].pop('storage_authority')
    with pytest.raises(ValueError,match='identical storage authority'):compare(observations)


def test_native_authority_comparison_requires_exact_pin(observations):
    for row in observations:row['storage_authority']={'backend':'tofudb','transport':'daemon'}
    with pytest.raises(ValueError,match='exact engine pin'):compare(observations)
    for row in observations:row['storage_authority']['engine_sha256']='a'*64
    assert compare(observations)['measurement_permitted'] is True


@pytest.mark.parametrize('authority',[None,{'backend':'sqlite','transport':'sidecar'},
    {'backend':'tofudb','transport':'daemon','engine_sha256':'b'*64}])
def test_native_timing_cohort_refuses_missing_or_changed_authority(observations,authority):
    from benchmarks.native_application_root_measure import timing_summary
    rows=cohort_rows(observations)
    expected={'backend':'tofudb','transport':'daemon','engine_sha256':'a'*64}
    for row in rows:
        row['backend']='daemon'
        row['storage_authority']=dict(expected)
    rows[0]['storage_authority']=authority
    with pytest.raises(ValueError,match='storage authority differs'):
        timing_summary({'warm':rows,'cold':[],'backend':'daemon','storage_authority':expected})


def test_native_timing_cohort_keeps_memory_and_cold_incomplete(observations):
    from benchmarks.native_application_root_measure import timing_summary
    rows=cohort_rows(observations)
    expected={'backend':'tofudb','transport':'daemon','engine_sha256':'a'*64}
    for row in rows:row.update(backend='daemon',storage_authority=dict(expected))
    result=timing_summary({'warm':rows,'cold':[],'backend':'daemon','storage_authority':expected})
    assert 'cold samples incomplete' in result['issues']
    assert 'memory pairs incomplete' in result['issues']
    assert not any('fixture' in issue for issue in result['issues'])


def test_daemon_fixture_requires_explicit_engine_before_resolution(tmp_path):
    from benchmarks.native_application_authority import authority_witness
    with pytest.raises(ValueError,match='explicit storage engine'):
        authority_witness('daemon',None,tmp_path)


@pytest.mark.parametrize('action',['reserve','dispatch','settle','secret-action'])
def test_model_stage_preserves_callback_authority_and_never_records_payload(action):
    import asyncio
    from benchmarks.native_application_stages import Stages
    async def run():
        stages=Stages();owner=object();process=object();result=object()
        payload={'action':action,'credential':'private-not-for-diagnostics'}
        failure=asyncio.CancelledError()
        async def callback(actual_owner,actual_payload,actual_process,*,closing):
            assert actual_owner is owner and actual_payload is payload and actual_process is process
            if closing:raise failure
            return result
        wrapped=stages.model_control(callback)
        assert await wrapped(owner,payload,process,closing=False) is result
        with pytest.raises(asyncio.CancelledError) as caught:
            await wrapped(owner,payload,process,closing=True)
        assert caught.value is failure
        rows=stages.snapshot();label=action if action in ('reserve','dispatch','settle') else 'invalid'
        assert [row['name'] for row in rows]==['native.model_control.'+label]*2
        assert [row['outcome'] for row in rows]==['returned','raised']
        assert all(row['thread_cpu_ns'] is None for row in rows)
        assert 'private-not-for-diagnostics' not in repr(rows) and 'secret-action' not in repr(rows)
    asyncio.run(run())


@pytest.mark.parametrize('value',[None,False,1,'true'])
def test_timing_refuses_missing_or_changed_reuse_policy(observations,value):
    from benchmarks.native_application_root_measure import timing_summary
    rows=cohort_rows(observations)
    for row in rows:row['worker_reuse']=True
    if value is not None:rows[0]['worker_reuse']=value
    else:rows[0].pop('worker_reuse')
    with pytest.raises(ValueError,match='reuse policy'):
        timing_summary(dict(warm=rows,cold=[],worker_reuse=True))


@pytest.mark.parametrize('field',['physical_workers','idle_workers','pids','invocations','all_ready'])
def test_timing_requires_physical_reuse_receipt(observations,field):
    from benchmarks.native_application_root_measure import timing_summary
    rows=cohort_rows(observations)
    for row in rows:row['worker_reuse']=True
    witness=dict(physical_workers=1,idle_workers=1,pids=[123],invocations=121,all_ready=True)
    rows[1]['worker_reuse_witness']=witness
    assert timing_summary(dict(warm=rows,cold=[],worker_reuse=True))['default_selection_admitted'] is False
    witness.pop(field)
    with pytest.raises(ValueError,match='reuse witness'):
        timing_summary(dict(warm=rows,cold=[],worker_reuse=True))


def test_storage_v2_stage_preserves_arguments_failures_and_redacts_operation():
    from benchmarks.native_application_stages import Stages
    stages=Stages();owner=object();payload=object();result=object();failure=OSError('failed')
    def call(actual_owner,operation,actual_payload,**kwargs):
        assert actual_owner is owner and actual_payload is payload
        if kwargs['timeout']==1:raise failure
        return result
    wrapped=stages.storage_v2_attempt(call)
    assert wrapped(owner,'private-operation',payload,timeout=2,command_id='private-command') is result
    with pytest.raises(OSError) as caught:wrapped(owner,'private-operation',payload,timeout=1)
    assert caught.value is failure
    assert [row['name'] for row in stages.snapshot()]==['storage.v2.attempt']*2
    assert [row['outcome'] for row in stages.snapshot()]==['returned','raised']
    assert 'private-' not in repr(stages.snapshot())


def test_terminal_diagnostic_wraps_actual_issued_projector_once():
    from benchmarks.native_application_stages import Stages
    stages=Stages();result=object();task=object();request=object()
    class Owner:pass
    owner=Owner()
    def project(actual_task,actual_request):
        assert actual_task is task and actual_request is request
        return result
    def initialize(actual_owner,*,projector):
        assert actual_owner is owner
        owner.project=projector
    assert stages.terminal_initializer(initialize)(owner,projector=project) is None
    assert owner.project(task,request) is result
    assert [row['name'] for row in stages.snapshot()]==['native.terminal_projection']


def test_usage_summary_selection_cannot_change_between_arms(observations):
    observations[1]['native_usage_summary']=True
    with pytest.raises(ValueError,match='summary selection'):compare(observations)


def test_selected_usage_summary_requires_physical_terminal_delivery(observations):
    for row in observations:row.update(native_usage_summary=True,mode='warm',warmups=1,raw=[{}])
    with pytest.raises(ValueError,match='summary witness'):compare(observations)
    observations[1]['usage_summary_witness']={'requests':3,'paid_frozen_requests':3}
    assert compare(observations)['measurement_permitted'] is True
    observations[1]['usage_summary_witness']['requests']=2
    with pytest.raises(ValueError,match='summary witness'):compare(observations)


def test_timing_collector_refuses_unrequested_native_summary():
    from benchmarks.native_application_root_measure import timing_summary
    with pytest.raises(ValueError,match='summary selection'):
        timing_summary({'warm':[{'native_usage_summary':True}],'cold':[]})


def test_image_preparation_witness_refuses_preclock_or_mixed_policy():
    from benchmarks.native_application_authority import check_image_preparation
    row=dict(arm='native',native_image_preparation=True,
        image_preparation_witness={'before_first_operation':False,'after_first_operation':True})
    check_image_preparation(row,True)
    with pytest.raises(ValueError,match='policy'):
        check_image_preparation(row,False)
    row['image_preparation_witness']['before_first_operation']=True
    with pytest.raises(ValueError,match='before operation'):
        check_image_preparation(row,True)
    row['image_preparation_witness']['before_first_operation']=False
    row['image_preparation_witness']['after_first_operation']=False
    with pytest.raises(ValueError,match='witness'):
        check_image_preparation(row,True)



def test_terminal_reference_selection_and_actual_frames_are_required(observations):
    observations[1]['native_terminal_reference']=True
    with pytest.raises(ValueError,match='reference selection'):compare(observations)
    for row in observations:row['native_terminal_reference']=True
    with pytest.raises(ValueError,match='reference witness'):compare(observations)
    observations[1].update(terminal_reference_frames=1,mode='cold')
    assert compare(observations)['measurement_permitted'] is True
    observations[1]['terminal_reference_frames']=True
    with pytest.raises(ValueError,match='reference witness'):compare(observations)


def test_timing_collector_refuses_unrequested_terminal_reference():
    from benchmarks.native_application_root_measure import timing_summary
    with pytest.raises(ValueError,match='reference selection'):
        timing_summary({'warm':[{'native_terminal_reference':True}],'cold':[]})


def native_reference_pair(observations):
    rows=cohort_rows(observations)
    for row in rows:
        row.update(arm='native',worker_reuse=False,clock_scope='same terminal clock',affinity=[0],
            storage_authority={'backend':'tofudb','transport':'daemon','engine_sha256':'c'*64})
        row['evidence']['api_rounds'][0]['cost']['pricingSource']='native_resolved_price'
    rows[0].update(native_terminal_reference=False,terminal_reference_frames=0)
    rows[1].update(native_terminal_reference=True,terminal_reference_frames=121)
    return rows


def test_same_native_reference_comparison_keeps_actual_arms_and_no_default_admission(observations):
    from benchmarks.native_application_root import compare_native_reference_pair
    rows=native_reference_pair(observations)
    result=compare_native_reference_pair(*rows)
    assert result['measurement_permitted'] and result['ratios']['cpu_mean']==1
    assert not result['performance_admitted'] and not result['default_selection_admitted']
    assert [r['arm'] for r in rows]==['native','native']


@pytest.mark.parametrize('field,value',[('native_terminal_reference',False),('terminal_reference_frames',0),
    ('process_identity','0:fixture'),('profiler_enabled',True),('archive_sha256','d'*64),
    ('completion_observer','worker-future'),('clock_scope','different'),('affinity',[1])])
def test_native_reference_pair_refuses_uncontrolled_comparison(observations,field,value):
    from benchmarks.native_application_root import compare_native_reference_pair
    rows=native_reference_pair(observations);rows[1][field]=value
    with pytest.raises(ValueError):compare_native_reference_pair(*rows)


def test_native_reference_pair_refuses_changed_paid_work_with_recomputed_hash(observations):
    from benchmarks.native_application_root import compare_native_reference_pair,comparable_work,digest
    rows=native_reference_pair(observations)
    rows[1]['evidence']['api_rounds'][0]['cost']['costCny']=.1
    rows[1]['work_sha256']=digest(comparable_work(rows[1]))
    with pytest.raises(ValueError):compare_native_reference_pair(*rows)

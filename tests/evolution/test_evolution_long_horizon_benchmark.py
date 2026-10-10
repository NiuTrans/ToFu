"""Controlled stability results cannot imply omitted recovery or research coverage."""
from copy import deepcopy
import pytest
from benchmarks.evolution_long_horizon import validate_report,run_proof

pytestmark=pytest.mark.unit


def report():
    return {'schema_version':1,'scope':'controlled_native_evolution_repair_and_storage_crash',
        'status':'passed','attempts':2,'receipts':4,'verified_originals':2,
        'requested_crashes':[1],'storage_crashes':[{'after_attempt':1,'exit_code':-9,'run_readback':'exact'}],
        'measurements':[{'attempt':i,'receipts':i*2,'run_header_bytes':100+i,'stored_run_header_bytes':100+i,'stored_receipts':i*2,'archived_receipts':0,'elapsed_ms':1.5} for i in (1,2)],
        'actual_model_calls':0,'executor_replay_tested':False,'research_quality_tested':False}


def test_complete_controlled_scope_is_accepted_without_promoting_quality():
    measured=report();before=deepcopy(measured)
    assert validate_report(measured) and measured==before


@pytest.mark.parametrize('gap',['missing_crash','graceful_exit','unknown_readback','missing_original',
    'missing_receipt','missing_measurement','bad_sequence','nonfinite_time','broader_scope',
    'claimed_replay','claimed_quality','real_models','malformed_crash','malformed_request','unbounded_header','missing_archived_receipts'])
def test_coverage_gaps_never_report_controlled_success(gap):
    measured=report()
    if gap=='missing_crash':measured['storage_crashes']=[]
    elif gap=='graceful_exit':measured['storage_crashes'][0]['exit_code']=0
    elif gap=='unknown_readback':measured['storage_crashes'][0]['run_readback']='unknown'
    elif gap=='missing_original':measured['verified_originals']=1
    elif gap=='missing_receipt':measured['receipts']=3
    elif gap=='missing_measurement':measured['measurements'].pop()
    elif gap=='bad_sequence':measured['measurements'][1]['attempt']=1
    elif gap=='nonfinite_time':measured['measurements'][0]['elapsed_ms']=float('nan')
    elif gap=='broader_scope':measured['scope']='autonomous_research'
    elif gap=='claimed_replay':measured['executor_replay_tested']=True
    elif gap=='claimed_quality':measured['research_quality_tested']=True
    elif gap=='real_models':measured['actual_model_calls']=1
    elif gap=='malformed_crash':measured['storage_crashes']=[None]
    elif gap=='malformed_request':measured['requested_crashes']=[{}]
    elif gap=='unbounded_header':measured['measurements'][0]['stored_receipts']=1000
    else:measured['measurements'][0]['archived_receipts']=32
    assert not validate_report(measured)


@pytest.mark.parametrize('attempts,crashes,repetitions',[(0,[],1),(2,[2],1),(3,[2,1],1),(3,[1,1],1),(2,[],0)])
def test_invalid_dimensions_refuse_before_storage_allocation(attempts,crashes,repetitions):
    with pytest.raises(ValueError):run_proof(attempts,crashes,repetitions)


def test_private_benchmark_binds_fixture_owner_and_restores_caller_scope_after_failure(monkeypatch):
    import os
    from benchmarks import evolution_long_horizon as benchmark
    seen=[]
    class Runtime:
        def __init__(self,supervisor,**kwargs):pass
        def start(self):
            seen.append(int(os.environ['TOFUDB_OWNER_ID']))
            raise RuntimeError('controlled private startup failure')
        def stop(self):seen.append('stopped')
    monkeypatch.setenv('TOFUDB_OWNER_ID','987')
    monkeypatch.setattr(benchmark,'StorageSupervisor',lambda **kwargs:object())
    monkeypatch.setattr(benchmark,'StorageRuntime',Runtime)
    monkeypatch.setattr(benchmark,'install_runtime_for_test',lambda value:None)
    with pytest.raises(RuntimeError,match='controlled private startup failure'):
        benchmark.run_proof(2,[1],2)
    assert seen==[benchmark.OWNER,'stopped']
    assert os.environ['TOFUDB_OWNER_ID']=='987'

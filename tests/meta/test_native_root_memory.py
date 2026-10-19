"""Paused memory evidence cannot turn running/invalid/incomplete rows into admission."""
import copy
import pytest
from benchmarks.native_root_memory import summary

pytestmark=pytest.mark.unit


def rows():
    values=[]
    for pair in range(3):
        for arm in ('python','native') if pair%2==0 else ('native','python'):
            peak=lambda metric:dict(all_threads_stopped=True,kernel_vm_comparison=True,**{metric:1000})
            values.append(dict(arm=arm,process_identity=f'{pair}-{arm}',work_sha256='same',
                joined_cleanup=True,joined_memory_observer=True,memory_snapshots=30,
                raw=[{} for _ in range(100)],warmups=20,timing_admitted=False,
                memory_scope='stopped kernel-confirmed unique address spaces',
                peak_snapshots={metric:peak(metric) for metric in ('unique_vm_rss_bytes','unique_vm_pss_bytes')}))
    return values


def test_memory_component_cannot_admit_timing_or_project_default():
    result=summary(rows(),100,20)
    assert result['verdict']=='qualified_memory_component'
    assert not result['performance_admitted'] and not result['default_selection_admitted']


@pytest.mark.parametrize('change',['running','uncertified','bad_metric','wrong_scope','identity','work'])
def test_invalid_memory_observation_never_supplies_ratios(change):
    data=rows();peak=data[0]['peak_snapshots']['unique_vm_rss_bytes']
    if change=='running':peak['all_threads_stopped']=False
    elif change=='uncertified':peak['kernel_vm_comparison']=False
    elif change=='bad_metric':peak['unique_vm_rss_bytes']=float('nan')
    elif change=='wrong_scope':data[0]['memory_scope']='live process sum'
    elif change=='identity':data[1]['process_identity']=data[0]['process_identity']
    else:data[1]['work_sha256']='different'
    result=summary(data,100,20)
    assert result['verdict']=='invalid' and not result['ratios']


@pytest.mark.parametrize('change',['pairs','samples','warmups','cleanup'])
def test_incomplete_memory_never_qualifies(change):
    data=rows()
    if change=='pairs':data=data[:4]
    elif change=='samples':data[0]['raw']=data[0]['raw'][:99]
    elif change=='warmups':data[0]['warmups']=19
    else:data[0]['joined_memory_observer']=False
    # A smaller caller request cannot lower canonical cohort requirements.
    assert summary(data,5,2)['verdict']=='incomplete'


@pytest.mark.parametrize('metric',['unique_vm_rss_bytes','unique_vm_pss_bytes'])
def test_unique_vm_memory_regression_is_retained(metric):
    data=rows();data[1]['peak_snapshots'][metric][metric]=1200
    assert summary(data,100,20)['verdict']=='regression'


@pytest.mark.parametrize('authority',[None,{'backend':'sqlite','transport':'sidecar'},
    {'backend':'tofudb','transport':'daemon','engine_sha256':'b'*64}])
def test_native_memory_refuses_missing_or_foreign_authority(authority):
    data=rows();expected={'backend':'tofudb','transport':'daemon','engine_sha256':'a'*64}
    for row in data:row['storage_authority']=dict(expected)
    data[0]['storage_authority']=authority
    result=summary(data,100,20,storage_authority=expected)
    assert result['verdict']=='invalid' and result['ratios']=={}


def test_native_memory_qualifies_only_its_memory_component():
    data=rows();expected={'backend':'tofudb','transport':'daemon','engine_sha256':'a'*64}
    for row in data:row['storage_authority']=dict(expected)
    result=summary(data,100,20,storage_authority=expected)
    assert result['verdict']=='qualified_memory_component'
    assert result['performance_admitted'] is result['default_selection_admitted'] is False


@pytest.mark.parametrize('change',['missing_policy','changed_policy','missing_witness','wrong_count'])
def test_memory_refuses_unproven_application_worker_reuse(change):
    data=rows()
    for row in data:
        row.update(worker_reuse=True,mode='memory')
        if row['arm']=='native':
            row['worker_reuse_witness']=dict(physical_workers=1,idle_workers=1,pids=[123],invocations=121,all_ready=True)
    assert summary(data,100,20,worker_reuse=True)['verdict']=='qualified_memory_component'
    if change=='missing_policy':data[0].pop('worker_reuse')
    elif change=='changed_policy':data[0]['worker_reuse']=False
    elif change=='missing_witness':data[1].pop('worker_reuse_witness')
    else:data[1]['worker_reuse_witness']['invocations']=120
    result=summary(data,100,20,worker_reuse=True)
    assert result['verdict']=='invalid' and result['ratios']=={}


@pytest.mark.parametrize('fault', ['source', 'missing_pin', 'changed_pin', 'foreign_selection'])
def test_installed_memory_requires_frozen_source_and_exact_pins(fault):
    data = rows()
    for row in data: row.update(image_source='installed_executable', executable_sha256='a'*64)
    selected = 'installed_executable'
    if fault == 'source': data[-1]['image_source'] = 'archive'
    elif fault == 'missing_pin': data[-1].pop('executable_sha256')
    elif fault == 'changed_pin': data[-1]['executable_sha256'] = 'b'*64
    else: selected = 'archive'
    result = summary(data, 100, 20, image_source=selected)
    assert result['verdict'] == 'invalid' and not result['ratios']


def test_explicit_installed_memory_qualifies_only_its_separate_component():
    data = rows()
    for row in data: row.update(image_source='installed_executable', executable_sha256='a'*64)
    result = summary(data, 100, 20, image_source='installed_executable')
    assert result['verdict'] == 'qualified_memory_component'
    assert result['performance_admitted'] is result['default_selection_admitted'] is False


@pytest.mark.parametrize('metadata', [{'image_source': []}, {'executable_sha256': []}])
def test_malformed_image_metadata_is_invalid_without_ratios(metadata):
    data = rows()
    data[0].update(metadata)
    result = summary(data, 100, 20)
    assert result['verdict'] == 'invalid' and not result['ratios']

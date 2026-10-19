"""Root observations cannot bypass comparability, cold or memory guards."""
import copy
import pytest
from benchmarks.native_agent_root_measure import summarize

pytestmark=pytest.mark.unit


def report():
    result=dict(backend='sqlite',scenario='text',warm=[],memory=[],cold=[])
    ordinal=0
    def row(arm,mode):
        nonlocal ordinal
        ordinal+=1
        clock=dict(wall_ns=1000 if arm=='python' else 800,cpu_ns=1000 if arm=='python' else 800)
        return dict(arm=arm,mode=mode,backend='sqlite',scenario='text',
            process_identity=str(ordinal),work_sha256='same-work',joined_cleanup=True,
            profiler_enabled=False,tracemalloc_enabled=False,
            first_operation=clock,raw=[] if mode=='cold' else [copy.copy(clock) for _ in range(100)],
            warmups=0 if mode=='cold' else 20,rss_samples=100 if mode=='memory' else 0,
            peak_process_tree_rss_bytes=1000 if mode=='memory' else None,
            retained_task_bytes=1000,sealed_image_bytes=15000 if arm=='native' else 0)
    for mode,count in (('warm',3),('memory',3),('cold',100)):
        for pair in range(count):
            for arm in ('python','native') if pair%2==0 else ('native','python'):
                result[mode].append(row(arm,mode))
    return result


def test_complete_root_component_never_promotes_project_default():
    verdict=summarize(report())
    assert verdict['verdict']=='qualified_component'
    assert not verdict['whole_project_admitted'] and not verdict['default_selection_admitted']


@pytest.mark.parametrize('change',['unequal_work','reuse','bad_clock','cold_warmups','changed_fixture','bad_memory','paired_arms','profiler','tracemalloc'])
def test_invalid_observation_cannot_supply_ratios(change):
    data=report()
    if change=='unequal_work':data['warm'][1]['work_sha256']='different'
    elif change=='reuse':data['cold'][0]['process_identity']=data['warm'][0]['process_identity']
    elif change=='bad_clock':data['warm'][0]['raw'][0]['cpu_ns']=float('nan')
    elif change=='cold_warmups':data['cold'][0]['warmups']=20
    elif change=='changed_fixture':data['cold'][0]['backend']='embedded'
    elif change=='bad_memory':data['memory'][0]['peak_process_tree_rss_bytes']=-1
    elif change=='paired_arms':data['warm'][1]['arm']='python'
    elif change=='profiler':data['warm'][0]['profiler_enabled']=True
    else:data['cold'][0]['tracemalloc_enabled']=True
    verdict=summarize(data)
    assert verdict['verdict']=='invalid' and not verdict['ratios']


@pytest.mark.parametrize('change',['cold','warm','memory','warmups','cleanup'])
def test_missing_coverage_never_qualifies(change):
    data=report()
    if change=='cold':data['cold']=data['cold'][:198]
    elif change=='warm':data['warm'][0]['raw']=data['warm'][0]['raw'][:99]
    elif change=='memory':data['memory']=[]
    elif change=='warmups':data['warm'][0]['warmups']=19
    else:data['memory'][0]['joined_cleanup']=False
    assert summarize(data)['verdict']=='incomplete'


@pytest.mark.parametrize('change',['p99','cpu','rss','retained','cold'])
def test_each_regression_refuses_component_qualification(change):
    data=report()
    if change=='p99':
        for row in data['warm'][1]['raw'][-2:]:row['wall_ns']=2000
    elif change=='cpu':
        for row in data['warm'][1]['raw']:row['cpu_ns']=1200
    elif change=='rss':data['memory'][1]['peak_process_tree_rss_bytes']=1200
    elif change=='retained':data['memory'][1]['retained_task_bytes']=1200
    else:
        for row in data['cold']:
            if row['arm']=='native':row['first_operation']['wall_ns']=1200
    assert summarize(data)['verdict']=='regression'


def test_gain_in_only_one_pair_is_not_reproducible_improvement():
    data=report()
    for row in data['warm'][2:]:
        if row['arm']=='native':
            for sample in row['raw']:sample['wall_ns']=990
    assert summarize(data)['verdict']=='no_gain'

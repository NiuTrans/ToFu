"""Terminal delivery evidence must cover physical work, history and cleanup."""
import copy
import pytest
from benchmarks.native_terminal_delivery import validate_observation
pytestmark=pytest.mark.unit


def row():
    return dict(scope='native SDK transient terminal delivery',joined_cleanup=True,
        operations=3,samples=2,retry=True,provider_calls=3,terminal_receipts=3,
        receiver_calls=6,exact_history_verified=True,
        raw=[dict(wall_ns=2,tail_wall_ns=1,cpu_ns=1)]*2)


def test_complete_terminal_delivery_evidence():validate_observation(row())


@pytest.mark.parametrize('field,value',[('joined_cleanup',False),('provider_calls',2),
    ('terminal_receipts',2),('receiver_calls',3),('exact_history_verified',False),('raw',[])])
def test_partial_delivery_or_changed_work_refuses(field,value):
    data=row();data[field]=value
    with pytest.raises(ValueError):validate_observation(data)


@pytest.mark.parametrize('key',['wall_ns','tail_wall_ns','cpu_ns'])
def test_invalid_delivery_clock_refuses(key):
    data=copy.deepcopy(row());data['raw'][0][key]=0
    with pytest.raises(ValueError):validate_observation(data)


def test_reference_selection_requires_one_actual_frame_per_operation():
    data=row();data['completion_reference']=True
    with pytest.raises(ValueError,match='reference witness'):validate_observation(data)
    data['reference_frames']=3;validate_observation(data)
    data['completion_reference']=False
    with pytest.raises(ValueError,match='reference witness'):validate_observation(data)


@pytest.mark.parametrize('value',[None,1,'true'])
def test_reference_selection_requires_a_boolean(value):
    data=row();data['completion_reference']=value
    with pytest.raises(ValueError,match='reference witness'):validate_observation(data)



def pair():
    baseline=row();baseline.update(completion_reference=False,reference_frames=0,
        history_bytes=1024,warmups=1,work_sha256='a'*64,binary_sha256='b'*64,process_identity='1:first')
    candidate=copy.deepcopy(baseline);candidate.update(completion_reference=True,reference_frames=3,process_identity='2:second')
    return baseline,candidate


def test_same_binary_reference_pair_does_not_admit_defaults():
    from benchmarks.native_terminal_delivery import compare_reference_pair
    result=compare_reference_pair(*pair())
    assert result['ratios']['cpu_mean']==1
    assert not result['performance_admitted'] and not result['default_selection_admitted']


@pytest.mark.parametrize('field,value',[('history_bytes',2048),('retry',False),
    ('binary_sha256','c'*64),('work_sha256','c'*64),('process_identity','1:first'),
    ('completion_reference',False),('process_identity',None),('process_identity','')])
def test_reference_pair_refuses_changed_work_selection_or_reused_worker(field,value):
    from benchmarks.native_terminal_delivery import compare_reference_pair
    before,after=pair();after[field]=value
    with pytest.raises(ValueError):compare_reference_pair(before,after)


def test_retry_reference_witness_requires_actual_retry_frames():
    data=row();data.update(retry_reference=True,retry_reference_frames=3)
    validate_observation(data)
    data['retry_reference_frames']=0
    with pytest.raises(ValueError,match='retry reference witness'):validate_observation(data)


def test_retry_reference_pair_freezes_the_final_reference_policy():
    from benchmarks.native_terminal_delivery import compare_retry_reference_pair
    before,after=pair()
    before.update(completion_reference=True,reference_frames=3,retry_reference=False,retry_reference_frames=0)
    after.update(retry_reference=True,retry_reference_frames=3)
    assert compare_retry_reference_pair(before,after)['ratios']['cpu_mean']==1
    before.update(completion_reference=False,reference_frames=0)
    with pytest.raises(ValueError,match='other terminal reference policy'):compare_retry_reference_pair(before,after)

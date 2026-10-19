"""Collectors freeze one completion boundary before timing or memory admission."""
import pytest
from benchmarks.native_application_authority import check_completion_observer
from benchmarks.native_application_root_measure import timing_summary
from benchmarks.native_root_memory import summary
from tests.meta.test_native_root_memory import rows

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('actual',['poll',None,[], 'invalid'])
def test_timing_rejects_missing_foreign_or_invalid_worker_future_observation(actual):
    report={'completion_observer':'worker-future','warm':[{'completion_observer':actual}],'cold':[]}
    with pytest.raises(ValueError,match='completion observer'):
        timing_summary(report)


def test_legacy_rows_remain_poll_and_never_gain_future_admission():
    check_completion_observer({},'poll')
    with pytest.raises(ValueError,match='changed completion observer'):
        check_completion_observer({},'worker-future')


def test_memory_observer_is_bound_across_all_pairs():
    data=rows()
    for row in data:row['completion_observer']='worker-future'
    assert summary(data,100,20,completion_observer='worker-future')['verdict']=='qualified_memory_component'
    data[-1]['completion_observer']='poll'
    rejected=summary(data,100,20,completion_observer='worker-future')
    assert rejected['verdict']=='invalid' and rejected['ratios']=={}


def test_memory_does_not_default_missing_worker_future_witness_to_success():
    result=summary(rows(),100,20,completion_observer='worker-future')
    assert result['verdict']=='invalid' and not result['ratios']



def test_legacy_summary_caller_cannot_silently_accept_new_observer():
    data=rows()
    for row in data:row['completion_observer']='worker-future'
    rejected=summary(data,100,20)
    assert rejected['verdict']=='invalid' and rejected['ratios']=={}


def test_memory_never_admits_an_unqualified_terminal_summary_variant():
    data=rows()
    for row in data:row['native_usage_summary']=True
    rejected=summary(data,100,20)
    assert rejected['verdict']=='invalid' and rejected['ratios']=={}


@pytest.mark.parametrize('actual',[None,0,1,'true',[]])
def test_terminal_summary_policy_requires_exact_boolean_selection(actual):
    from benchmarks.native_application_authority import check_usage_summary
    with pytest.raises(ValueError,match='summary selection'):
        check_usage_summary({'native_usage_summary':actual},False)


def test_memory_refuses_unqualified_terminal_reference_selection():
    data=rows()
    for row in data:row['native_terminal_reference']=True
    result=summary(data,100,20)
    assert result['verdict']=='invalid' and not result['ratios']


@pytest.mark.parametrize('selection,actual', [
    ('installed_executable', 'archive'), ('archive', 'installed_executable'),
    ('installed_executable', None), ('invalid', 'invalid')])
def test_timing_freezes_executable_admission_source(selection, actual):
    report = dict(image_source=selection, warm=[dict(image_source=actual)], cold=[])
    with pytest.raises(ValueError, match='executable admission source'):
        timing_summary(report)


def test_memory_does_not_borrow_archive_admission_for_installed_files():
    data = rows()
    for row in data: row['image_source'] = 'installed_executable'
    result = summary(data, 100, 20)
    assert result['verdict'] == 'invalid' and not result['ratios']


@pytest.mark.parametrize('pin', [None, '', [], 'g'*64])
def test_installed_timing_requires_actual_executable_pin(pin):
    report = dict(image_source='installed_executable',
        warm=[dict(image_source='installed_executable', executable_sha256=pin)], cold=[])
    with pytest.raises(ValueError, match='installed executable pin'):
        timing_summary(report)


@pytest.mark.parametrize('change', ['selection','before','missing','fallback','unclosed','pin'])
def test_token_library_requires_actual_in_clock_native_counts_and_joined_close(change):
    from benchmarks.native_application_authority import check_token_library
    row=dict(arm='native',native_token_count=True,token_library_sha256='a'*64,
        token_library_witness=dict(before_first_operation=False,after_first_operation=True,
            python_encoders_before=[],python_encoders_after=[],encodings=['cl100k_base'],closed=True))
    check_token_library(row,True)
    if change=='selection':row['native_token_count']=False
    elif change=='before':row['token_library_witness']['before_first_operation']=True
    elif change=='missing':row['token_library_witness']['after_first_operation']=False
    elif change=='fallback':row['token_library_witness']['python_encoders_after']=['cl100k_base']
    elif change=='unclosed':row['token_library_witness']['closed']=False
    else:row['token_library_sha256']='bad'
    with pytest.raises(ValueError):check_token_library(row,True)


def test_legacy_memory_cannot_admit_new_native_count_selection():
    data=rows()
    for row in data:row['native_token_count']=True
    result=summary(data,100,20)
    assert result['verdict']=='invalid' and not result['ratios']

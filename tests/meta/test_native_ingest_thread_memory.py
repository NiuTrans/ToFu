"""A producer group and its descendants need exact shared-memory/wait witnesses."""
import copy
import pytest

from benchmarks.knowledge_html.native_ingest_measure import qualify_memory

pytestmark = pytest.mark.unit


def receipts():
    def trace(pid,tgid,parent,born,exiting,joined,peak,*,code=0,exec_event=None,ticks='456',wait_peak=20000):
        return dict(pid=pid,tgid=tgid,creator_tgid=parent,start_ticks=ticks,
            born_event=born,exit_event=exiting,joined_event=joined,joined=True,
            execs=[] if exec_event is None else [dict(event=exec_event)],
            returncode=code,exit_event_status=9 if code==-9 else 0,
            wait4_peak_rss_bytes=wait_peak,
            memory=dict(status=dict(Pid=pid,Tgid=tgid,PPid=100 if tgid==101 else 101,
                                     Threads=2 if pid==102 else 1,VmHWM=peak),
                        exit_smaps=dict(Rss=peak//2),peak_rss_estimate=peak))
    producer=trace(101,101,100,1,10,11,4096,code=-9,exec_event=2)
    thread=trace(102,101,101,7,8,9,4096,code=-9)
    leaf=trace(201,201,101,3,5,6,3072,exec_event=4,wait_peak=8192)
    worker=dict(case='cancellation',mode='memory',pid=100,process_identity='boot:100:123',
        clock_records_materialized_after_memory_counters=True,
        child_memory_receipts=[dict(pid=101,returncode=-9,peak_rss_bytes=20000)],
        producer_receipts=[dict(producer_pid=101,producer_identity='boot:101:456',producer_returncode=-9,
            producer_child_memory_receipts=[dict(pid=201,returncode=0,peak_rss_bytes=8192)])],
        process_image_counters=dict(Pid=100,Tgid=100,VmHWM=8192),sealed_html_image_bytes=1024,
        retained_bytes=0,retention_baseline_rss_bytes=8192,post_cleanup_rss_bytes=4096)
    observer=dict(observation_valid=True,all_tracees_joined=True,root_returncode=0,
        child_memory_complete=True,root_pid=100,boot_id='boot',allow_threaded_children=True,
        tracees=[dict(pid=100,tgid=100,start_ticks='123'),producer,thread,leaf],
        child_process_groups=[dict(tgid=101,tids=[101,102],complete=True,peak_rss_estimate=4096),
                              dict(tgid=201,tids=[201],complete=True,peak_rss_estimate=3072)])
    return worker,observer


def test_thread_group_is_counted_once_and_descendant_overlap_is_included():
    worker,observer=receipts()
    result=qualify_memory(worker,observer)
    assert result['peak_rss_bytes']==8192+4096+3072+1024
    assert 'shared threads count once' in result['memory_scope']


@pytest.mark.parametrize('fault',['missing_wait','omitted_thread','wrong_peak','wrong_identity',
    'wrong_parent','wrong_exit','thread_exec','unjoined','overlapping_sibling','unobserved_group',
    'born_before_parent','late_descendant'])
def test_unqualified_thread_or_descendant_receipt_refuses(fault):
    worker,observer=receipts()
    producer,thread,leaf=observer['tracees'][1:]
    if fault=='missing_wait':worker['producer_receipts'][0]['producer_child_memory_receipts']=[]
    elif fault=='omitted_thread':observer['child_process_groups'][0]['tids']=[101]
    elif fault=='wrong_peak':observer['child_process_groups'][0]['peak_rss_estimate']=8192
    elif fault=='wrong_identity':producer['start_ticks']='457'
    elif fault=='wrong_parent':leaf['creator_tgid']=100
    elif fault=='wrong_exit':worker['child_memory_receipts'][0]['returncode']=0
    elif fault=='thread_exec':thread['execs']=[dict(event=7)]
    elif fault=='unjoined':thread['joined']=False
    elif fault=='unobserved_group':observer['child_process_groups'].pop()
    elif fault=='born_before_parent':leaf['born_event']=1
    elif fault=='late_descendant':leaf['joined_event']=12
    elif fault=='overlapping_sibling':
        second=copy.deepcopy(leaf);second.update(pid=202,tgid=202,creator_tgid=100,
                                               born_event=3,exit_event=12,joined_event=13)
        second['memory']['status'].update(Pid=202,Tgid=202,PPid=100)
        observer['tracees'].append(second)
        observer['child_process_groups'].append(dict(tgid=202,tids=[202],complete=True,peak_rss_estimate=3072))
        worker['child_memory_receipts'].append(dict(pid=202,returncode=0,peak_rss_bytes=8192))
    with pytest.raises(AssertionError):qualify_memory(worker,observer)

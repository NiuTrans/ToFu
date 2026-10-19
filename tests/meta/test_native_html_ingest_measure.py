"""Whole-upload evidence refuses unknown children, partial clocks and false use."""
import copy
import sys

import pytest

from benchmarks.knowledge_html.native_ingest_measure import qualify_memory, validate_worker
from benchmarks.knowledge_html.native_ingest_worker import CPU_SCOPE, fixture_inputs
from benchmarks.knowledge_html.native_ingest_provenance import CLOCK_SCOPE

pytestmark = pytest.mark.unit


def memory_receipts():
    worker = dict(mode='memory', clock_records_materialized_after_memory_counters=True,
        pid=100, process_identity='boot:100:123',
        child_memory_receipts=[dict(pid=101,returncode=0,peak_rss_bytes=4096)],
        process_image_counters=dict(Pid=100,Tgid=100,VmHWM=8192),
        sealed_html_image_bytes=1024, retained_bytes=0,
        retention_baseline_rss_bytes=8192, post_cleanup_rss_bytes=4096)
    observer = dict(observation_valid=True, all_tracees_joined=True,
        root_returncode=0, child_memory_complete=True, root_pid=100, boot_id='boot',
        tracees=[dict(pid=100,tgid=100,start_ticks='123'),
            dict(pid=101,tgid=101,creator_tgid=100,joined=True,execs=[{}],returncode=0,
                exit_event_status=0,wait4_peak_rss_bytes=4096,
                memory=dict(status=dict(Pid=101,PPid=100,Threads=1,VmHWM=3072),
                    exit_smaps=dict(Rss=2048),peak_rss_estimate=3072))])
    return worker, observer


def test_memory_uses_post_exec_peak_and_includes_sealed_bytes():
    worker, observer = memory_receipts()
    metrics = qualify_memory(worker, observer)
    assert metrics['peak_rss_bytes'] == 8192+3072+1024
    assert metrics['retained_bytes'] == 0


@pytest.mark.parametrize('fault', ['missing', 'unknown', 'unjoined', 'wrong_identity',
                                  'wrong_wait', 'multiple_exec', 'threads', 'early_records'])
def test_memory_missing_exit_or_unsupported_topology_refuses(fault):
    worker, observer = memory_receipts()
    child = observer['tracees'][1]
    if fault == 'missing': worker['child_memory_receipts'] = []
    elif fault == 'unknown':
        extra = copy.deepcopy(child); extra['pid'] = extra['tgid'] = 102
        observer['tracees'].append(extra)
    elif fault == 'unjoined': child['joined'] = False
    elif fault == 'wrong_identity': observer['tracees'][0]['start_ticks'] = '122'
    elif fault == 'wrong_wait': child['wait4_peak_rss_bytes'] += 4096
    elif fault == 'multiple_exec': child['execs'].append({})
    elif fault == 'threads': child['memory']['status']['Threads'] = 2
    elif fault == 'early_records': worker['clock_records_materialized_after_memory_counters'] = False
    with pytest.raises(AssertionError): qualify_memory(worker, observer)


def cold_row():
    return dict(variant='candidate',case='pdf',mode='cold',process_identity='boot:100:123',
        clock_scope=CLOCK_SCOPE,database_provenance={'source_sha256': 'a'*64},
        pid=100,python=sys.version,affinity=[2,3],joined_cleanup=True,cpu_scope=CPU_SCOPE,
        profiler_enabled=False,tracemalloc_enabled=False,warmup_operations=0,
        raw_clocks=[dict(wall_ns=9000,worker_cpu_ns=6000,reaped_children_cpu_ns=1000,cpu_ns=7000)],
        wall_samples_ms=[.009],cpu_samples_ms=[.007],html_profile=None,projection_calls=0)


@pytest.mark.parametrize('fault', ['reused', 'cpu', 'instrumented', 'pdf_load', 'partial', 'affinity', 'scope', 'provenance'])
def test_worker_requires_fresh_exact_uninstrumented_work(fault):
    row = cold_row()
    config = dict(variant='candidate',case='pdf',mode='cold',samples=100,
                  database_provenance=copy.deepcopy(row['database_provenance']))
    validate_worker(row,config,set(),[2,3])
    seen = set()
    if fault == 'reused': seen.add(row['process_identity'])
    elif fault == 'cpu': row['raw_clocks'][0]['reaped_children_cpu_ns'] = 0
    elif fault == 'instrumented': row['profiler_enabled'] = True
    elif fault == 'pdf_load': row['html_profile'] = {'loaded': True}
    elif fault == 'partial': row['raw_clocks'] = []
    elif fault == 'affinity': row['affinity'] = [2]
    elif fault == 'scope': row['clock_scope'] = 'tofu.native-ingest-clock/v1'
    elif fault == 'provenance': row['database_provenance']['source_sha256'] = 'b'*64
    with pytest.raises(AssertionError): validate_worker(row,config,seen,[2,3])


def test_fixture_revisions_and_members_force_actual_ingestion():
    samples = [fixture_inputs(case,b'%PDF-fixed',index)
               for case in ('html','pdf','batch','bounded_memory') for index in (0,1)]
    for left,right in zip(samples[::2],samples[1::2]):
        assert len({raw for raw,_ in left+right}) == len(left+right)

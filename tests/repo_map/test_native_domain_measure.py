"""Repository performance evidence refuses fallback and preserves owned cleanup."""
from copy import deepcopy
import subprocess

import pytest

from benchmarks.repository_index import native_domain_worker as worker
from benchmarks.repository_index.native_domain_measure import _assert_observation
from lib.repo_map import _native_rank, rank
from tests.repo_map.test_native_rank import repository
from lib.tasks_pkg.manager.runtime import chat_task_runtime

pytestmark = pytest.mark.unit


def test_benchmark_rejects_computation_fallback_without_changing_normal_policy(monkeypatch):
    class FailedKernel:
        def solve(self, *_args): raise ValueError('fixture computation failed')
    monkeypatch.setattr(_native_rank, 'available_kernel', lambda: FailedKernel())
    repo=repository();expected=rank.sumbd(repo);original=rank._fallback_scores
    with worker.require_weighted_ranking(), pytest.raises(RuntimeError, match='weighted PageRank'):
        rank.sumbd(repo)
    assert rank._fallback_scores is original and rank.sumbd(repo)==expected


def test_failed_operation_removes_its_own_task(tmp_path, monkeypatch):
    work=worker.Workload(tmp_path,'python_extraction');work.start();task_id=work.task['id']
    def failed(*_args,**_kwargs):raise LookupError('tool fixture failed')
    monkeypatch.setattr(work.port,'invoke',failed)
    try:
        with pytest.raises(LookupError,match='tool fixture failed'):work.operation(0)
        assert work.task is None and work.runtime.task_count==0
        assert chat_task_runtime.get(task_id) is None
    finally:work.close()


def test_cleanup_failure_does_not_replace_tool_failure(tmp_path, monkeypatch):
    work=worker.Workload(tmp_path,'python_extraction');work.start();stop=work.stop
    def failed(*_args,**_kwargs):raise LookupError('primary tool failure')
    def cleanup(*_args,**_kwargs):raise OSError('cleanup fixture failed')
    monkeypatch.setattr(work.port,'invoke',failed)
    try:
        with monkeypatch.context() as scoped:
            scoped.setattr(work,'stop',cleanup)
            with pytest.raises(LookupError,match='primary tool failure') as caught:work.operation(0)
            assert any('cleanup failed: OSError' in note for note in caught.value.__notes__)
    finally:
        stop(True);work.close()


def test_clock_accounts_for_the_reaped_discovery_child_cpu():
    def child(_index):
        return subprocess.run(['/bin/sh', '-c',
            'i=0; while [ "$i" -lt 20000 ]; do i=$((i+1)); done'],
            check=True, capture_output=True, timeout=10).returncode
    result, clocks = worker._clock(child, 0)
    assert result == 0
    assert clocks['reaped_children_cpu_ns'] > 1_000_000
    assert clocks['cpu_ns'] > clocks['worker_cpu_ns']
    assert clocks['cpu_ns'] == clocks['worker_cpu_ns'] + clocks['reaped_children_cpu_ns']


def test_discovery_executable_receipt_observes_same_path_replacement(tmp_path, monkeypatch):
    executable=tmp_path/'git';executable.write_bytes(b'first!');executable.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path))
    before=worker.discovery_executables()
    executable.write_bytes(b'second')
    after=worker.discovery_executables()
    assert before['rg'] is None and after['rg'] is None
    assert before['git']['path']==after['git']['path']==str(executable)
    assert before['git']['sha256']!=after['git']['sha256']


@pytest.mark.parametrize('defect',['reused_process','not_joined','wrong_backend','raw_clock','warm_cold','wrong_affinity',
                                  'cpu_scope','child_cpu_missing','child_cpu_negative','discovery_changed',
                                  'memory_boundary','memory_record_count','memory_storage'])
def test_observation_guard_refuses_incomparable_or_unjoined_receipts(defect):
    row=dict(variant='candidate',case='ranking',mode='cold',process_identity='boot:123:456',
        joined_cleanup=True,audit_queue_settled=True,weighted_ranking_required=True,native_backend_selected=True,
        loaded_packages={'fixture':'1'},affinity=[1,2],profiler_enabled=False,tracemalloc_enabled=False,
        cpu_scope=worker.CPU_SCOPE,memory_separate_process=False,warmup_operations=0,
        discovery_executables={'git':{'sha256':'fixture'},'rg':None},
        raw_clocks=[dict(wall_ns=1000000,cpu_ns=500000,worker_cpu_ns=300000,reaped_children_cpu_ns=200000)],
        wall_samples_ms=[1.0],cpu_samples_ms=[.5])
    mode, samples = 'cold', 100
    if defect.startswith('memory_'):
        mode, samples = 'memory', 1
        row.update(mode=mode, memory_separate_process=True, warmup_operations=20,
                   clock_records_materialized_after_memory_counters=True,
                   clock_record_journal=dict(storage='unlinked_temporary_file', records=2, bytes=200, sha256='0'*64))
    good=deepcopy(row);seen=set()
    executables=deepcopy(row['discovery_executables'])
    _assert_observation(good,'candidate','ranking',mode,samples,seen,{'fixture':'1'},[1,2],executables)
    if defect!='reused_process':seen.clear()
    if defect=='not_joined':row['joined_cleanup']=False
    elif defect=='wrong_backend':row['native_backend_selected']=False
    elif defect=='raw_clock':row['raw_clocks'][0]['wall_ns']=2000000
    elif defect=='warm_cold':row['warmup_operations']=20
    elif defect=='wrong_affinity':row['affinity']=[1]
    elif defect=='cpu_scope':row['cpu_scope']='parent_only'
    elif defect=='child_cpu_missing':row['raw_clocks'][0]['reaped_children_cpu_ns']=0
    elif defect=='child_cpu_negative':row['raw_clocks'][0]['reaped_children_cpu_ns']=-1
    elif defect=='discovery_changed':row['discovery_executables']['rg']={'sha256':'different'}
    elif defect=='memory_boundary':row['clock_records_materialized_after_memory_counters']=False
    elif defect=='memory_record_count':row['clock_record_journal']['records']=1
    elif defect=='memory_storage':row['clock_record_journal']['storage']='in_memory_list'
    with pytest.raises(AssertionError):
        _assert_observation(row,'candidate','ranking',mode,samples,seen,{'fixture':'1'},[1,2],executables)


def test_discovery_cold_cpu_retains_costly_samples_in_its_mean():
    from benchmarks.repository_index.native_discovery_measure import aggregate_cold
    values = [100.0, 1.0, 1.0]
    assert aggregate_cold(values, 'cpu_ms_per_operation', None) == 34.0
    assert aggregate_cold(values, 'p50_ms', None) == 1.0

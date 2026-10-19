"""Real shared/fork memory identity and owned pause cleanup on observation faults."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest

from benchmarks.process_vm_memory import LinuxMemoryKernel,StoppedTree,VmSamplingError

pytestmark=pytest.mark.api


@pytest.fixture
def probe(tmp_path):
    compiler=shutil.which('cc')
    if not compiler:pytest.skip('C compiler required for disposable VM lifecycle probe')
    executable=tmp_path/'probe'
    source=Path(__file__).resolve().parents[1]/'fixtures/process_vm_memory/probe.c'
    subprocess.run([compiler,'-O2',str(source),'-o',str(executable)],check=True,timeout=20)
    return executable


def run_probe(probe,mode,operation):
    import psutil
    process=subprocess.Popen([str(probe),mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        ids=json.loads(process.stdout.readline());assert ids['parent']==process.pid
        operation(psutil.Process(process.pid),ids)
        assert psutil.Process(process.pid).status()!='stopped'
        assert psutil.Process(ids['child']).status()!='stopped'
    finally:
        process.stdin.write('x');process.stdin.flush();process.wait(timeout=3)
        process.stdin.close();process.stdout.close()
    assert process.returncode==0


@pytest.mark.parametrize('mode,groups',[('shared',1),('fork',2)])
def test_stopped_kernel_identity_distinguishes_shared_vm_from_fork(probe,mode,groups):
    def operation(root,ids):
        snapshot=StoppedTree(root).snapshot()
        assert snapshot['all_threads_stopped'] and snapshot['kernel_vm_comparison']
        assert snapshot['vm_groups']==groups and len(snapshot['members'])==2
        if mode=='shared':
            assert snapshot['raw_process_rss_bytes']>=snapshot['unique_vm_rss_bytes']*1.8
            assert snapshot['raw_process_pss_bytes']>=snapshot['unique_vm_pss_bytes']*1.8
        else:assert snapshot['raw_process_rss_bytes']==snapshot['unique_vm_rss_bytes']
    run_probe(probe,mode,operation)


def test_comparison_failure_resumes_all_owned_stops_and_closes_pidfds(probe):
    class Denied(LinuxMemoryKernel):
        def same_vm(self,*args):raise PermissionError('fault injected kernel comparison denial')
    def operation(root,ids):
        before=len(list(Path('/proc/self/fd').iterdir()))
        with pytest.raises(PermissionError):StoppedTree(root,kernel=Denied()).snapshot()
        assert len(list(Path('/proc/self/fd').iterdir()))==before
    run_probe(probe,'shared',operation)


def test_sampler_refuses_a_process_not_owned_as_direct_child():
    import psutil
    with pytest.raises(VmSamplingError):StoppedTree(psutil.Process(os.getpid()))


def test_failed_worker_cleanup_binds_and_joins_its_whole_disposable_tree(probe):
    import psutil
    import select
    from benchmarks.native_root_memory import join_failed_worker
    kernel=LinuxMemoryKernel()
    process=subprocess.Popen([str(probe),'hold'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    child_fd=None
    try:
        ids=json.loads(process.stdout.readline());child_fd=kernel.open(ids['child'])
        process.stdin.close()
        join_failed_worker(process,grace_seconds=.01)
        assert process.returncode is not None
        assert select.select([child_fd],[],[],0)[0]==[child_fd]
        assert psutil.Process(os.getpid()).status()!='stopped'
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=3)
        if child_fd is not None:os.close(child_fd)
        process.stdout.close()

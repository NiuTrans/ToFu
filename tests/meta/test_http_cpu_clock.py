"""Remote CPU samples witness child work without charging parent work."""
import os
import subprocess
import sys
import time
import pytest
from benchmarks.http_cpu_clock import OwnedProcessCpuClock

pytestmark=pytest.mark.unit


@pytest.mark.skipif(not sys.platform.startswith('linux'),reason='remote process CPU clock is Linux-specific')
def test_owned_child_cpu_clock_is_live_and_separate():
    executable=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    code='import sys,time\nprint("ready",flush=True)\nfor line in sys.stdin:\n start=time.process_time()\n while time.process_time()-start < .04: pass\n print("worked",flush=True)\n'
    process=subprocess.Popen([executable,'-u','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip()=='ready'
        clock=OwnedProcessCpuClock(process.pid);before=clock.read_ns()
        start=time.process_time()
        while time.process_time()-start<.03:pass
        idle=clock.read_ns()-before
        before=clock.read_ns();process.stdin.write('work\n');process.stdin.flush()
        assert process.stdout.readline().strip()=='worked'
        consumed=clock.read_ns()-before
        assert consumed>=30_000_000 and idle<consumed
    finally:
        process.stdin.close();process.wait(timeout=10);process.stdout.close()
    assert process.returncode==0


@pytest.mark.parametrize('pid',[0,-1,True,'1'])
def test_clock_rejects_unowned_pid_shapes(pid):
    with pytest.raises(ValueError):OwnedProcessCpuClock(pid)

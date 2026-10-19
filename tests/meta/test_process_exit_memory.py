"""Real exit snapshots, original wait ownership and failed-observation cleanup."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

pytestmark=[pytest.mark.api,pytest.mark.skipif(sys.platform!='linux',reason='Linux ptrace memory observation')]
ROOT=Path(__file__).resolve().parents[2]


@pytest.fixture
def probe(tmp_path):
    compiler=shutil.which('cc')
    if not compiler:pytest.skip('C compiler required for owned memory probe')
    target=tmp_path/'probe'
    subprocess.run([compiler,'-O2','-pthread',str(ROOT/'tests/fixtures/process_exit_memory/probe.c'),'-o',str(target)],check=True,timeout=30)
    return target


def observe(tmp_path,probe,mode,timeout=10,allow_threads=False):
    receipt=tmp_path/(mode+'.json')
    launcher=shlex.split(os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
    run=subprocess.run([*launcher,str(ROOT/'benchmarks/process_exit_memory.py'),'--receipt',str(receipt),
                        '--timeout',str(timeout),*(['--allow-threaded-children'] if allow_threads else []),
                        '--',str(probe),mode],capture_output=True,text=True,timeout=30)
    assert receipt.exists(),run.stdout+run.stderr
    return run,json.loads(receipt.read_text())


@pytest.mark.parametrize('mode,groups', [('threaded',1),('nested-threaded',2),('kill-threaded',1)])
def test_threaded_child_peaks_keep_parent_waits_and_count_shared_threads_once(tmp_path,probe,mode,groups):
    run,report=observe(tmp_path,probe,mode,allow_threads=True)
    assert run.returncode==0,run.stdout+run.stderr+json.dumps(report)
    assert report['observation_valid'] and report['all_tracees_joined'] and report['child_memory_complete']
    assert report['allow_threaded_children'] is True
    assert len(report['child_process_groups'])==groups
    tracees={row['pid']:row for row in report['tracees']}
    assert any(len(group['tids'])>1 for group in report['child_process_groups'])
    for group in report['child_process_groups']:
        assert group['complete']
        rows=[tracees[tid] for tid in group['tids']]
        assert group['peak_rss_estimate']==max(row['memory']['peak_rss_estimate'] for row in rows)
        assert 32*1024*1024<=group['peak_rss_estimate']<96*1024*1024
        assert all(row['joined'] and row['tgid']==group['tgid'] for row in rows)
        assert all(row['memory']['status']['Pid']==row['pid'] for row in rows)
        assert all(row['born_event']<row['exit_event']<row['joined_event'] for row in rows)
        leader=tracees[group['tgid']]
        assert all(row['joined_event']<=leader['joined_event'] for row in rows)
        assert len(leader['execs'])==1
        assert leader['returncode']==(-9 if mode=='kill-threaded' else 7)
    assert 'joined 1 children' in run.stdout


def test_threaded_observation_timeout_still_kills_and_joins_every_owned_tid(tmp_path,probe):
    run,report=observe(tmp_path,probe,'unsupported-threads',timeout=.2,allow_threads=True)
    assert run.returncode!=0 and not report['observation_valid']
    assert 'TimeoutError' in report['error'] and report['all_tracees_joined']
    assert all(row['joined'] for row in report['tracees'])
    for row in report['tracees']:
        with pytest.raises(ProcessLookupError):os.kill(row['pid'],0)


@pytest.mark.parametrize('mode,count',[('short',1),('parallel',4)])
def test_freed_child_peak_excludes_parent_history_and_parent_keeps_wait_ownership(tmp_path,probe,mode,count):
    run,report=observe(tmp_path,probe,mode)
    assert run.returncode==0,run.stdout+run.stderr
    assert report['observation_valid'] and report['all_tracees_joined'] and report['child_memory_complete']
    children=[row for row in report['tracees'] if row['tgid']!=report['root_pid']]
    assert len(children)==count
    for row in children:
        assert row['returncode']==7 and row['joined'] and row['creator_tgid']==report['root_pid']
        memory=row['memory']
        assert memory['status']['PPid']==report['root_pid']
        assert 32*1024*1024<=memory['peak_rss_estimate']<96*1024*1024
        assert memory['exit_smaps']['Rss']<16*1024*1024
        assert row['wait4_peak_rss_bytes']>=128*1024*1024
    assert f'joined {count} children' in run.stdout


@pytest.mark.parametrize('mode',['signal','threads'])
def test_root_signals_and_threads_remain_observable(tmp_path,probe,mode):
    run,report=observe(tmp_path,probe,mode)
    assert run.returncode==0 and report['all_tracees_joined'],run.stdout+run.stderr
    assert all(row['joined'] for row in report['tracees'])
    if mode=='signal':assert 'signal delivered' in run.stdout
    else:assert len(report['tracees'])>=2


@pytest.mark.parametrize('mode',['timeout','reexec','unsupported-threads'])
def test_refused_observation_kills_and_reaps_its_owned_tree(tmp_path,probe,mode):
    run,report=observe(tmp_path,probe,mode,timeout=.2 if mode=='timeout' else 10)
    assert run.returncode!=0 and not report['observation_valid']
    assert report['all_tracees_joined'] and report['cleanup_joined_pids']
    assert all(row['joined'] for row in report['tracees'])
    expected='TimeoutError' if mode=='timeout' else 'multiple child execs' if mode=='reexec' else 'child threads'
    assert expected in report['error']
    for row in report['tracees']:
        with pytest.raises(ProcessLookupError):os.kill(row['pid'],0)


@pytest.mark.parametrize('threaded',[False,True])
def test_snapshot_failure_preserves_error_and_joins_children(tmp_path,probe,threaded):
    script = '''
import json, sys
from benchmarks import process_exit_memory as observer
def denied(pid, **kwargs):
    raise PermissionError('injected smaps denial')
observer.snapshot = denied
try:
    threaded = sys.argv[2] == 'true'
    observer.observe([sys.argv[1], 'threaded' if threaded else 'parallel'], timeout_seconds=10,
                     allow_threaded_children=threaded)
except observer.ObservationError as error:
    print(json.dumps(error.report))
else:
    raise AssertionError('missing observation was admitted')
'''
    launcher=shlex.split(os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
    run=subprocess.run([*launcher,'-c',script,str(probe),str(threaded).lower()],cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stdout+run.stderr
    report=json.loads(run.stdout)
    assert not report['observation_valid'] and report['all_tracees_joined']
    assert 'PermissionError: injected smaps denial' in report['error']
    assert all(row['joined'] for row in report['tracees'])
    for row in report['tracees']:
        with pytest.raises(ProcessLookupError):os.kill(row['pid'],0)


@pytest.mark.parametrize('mode',['parallel','threaded'])
def test_observer_death_kills_tracees_and_outer_owner_can_reap(tmp_path,probe,mode):
    # A dedicated outer subreaper owns orphan cleanup; never leave test zombies
    # under the host's PID 1 or alter the pytest process's child ownership.
    script = '''
import json, os, signal, subprocess, sys, time
from pathlib import Path
from benchmarks import process_exit_memory as memory
assert memory.lib.prctl(memory.SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
inner = """
import json, sys, time
from pathlib import Path
from benchmarks import process_exit_memory as memory
def paused(pid, **kwargs):
    Path(sys.argv[2]).write_text(json.dumps({'tid':pid,'tgid':memory.status(pid)['Tgid']}))
    time.sleep(20)
memory.snapshot = paused
memory.observe([sys.argv[1], sys.argv[3]], allow_threaded_children=sys.argv[3]=='threaded')
"""
ready = Path(sys.argv[2])
child = subprocess.Popen([sys.argv[3], '-c', inner, sys.argv[1], str(ready), sys.argv[4]])
reaped = []
try:
    deadline = time.monotonic()+10
    while not ready.exists():
        assert child.poll() is None and time.monotonic()<deadline
        time.sleep(.01)
    child.kill()
    assert child.wait(timeout=5) == -signal.SIGKILL
    deadline = time.monotonic()+5
    while True:
        try: pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError: break
        assert time.monotonic()<deadline, 'tracee survived observer death'
        if pid:
            reaped.append(pid)
            assert os.WIFSIGNALED(status) and os.WTERMSIG(status)==signal.SIGKILL
        else: time.sleep(.01)
    observed = json.loads(ready.read_text())
    assert observed['tgid'] in reaped and len(reaped)>=2
    try: os.kill(observed['tid'], 0)
    except ProcessLookupError: pass
    else: raise AssertionError('observed thread survived observer death')
finally:
    if child.poll() is None: child.kill(); child.wait()
    # If a broken EXITKILL path survives, clean every now-adopted direct child.
    children = Path(f'/proc/self/task/{os.getpid()}/children').read_text().split()
    for pid in children:
        try: os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError: pass  # Adopted child exited before this kill.
    while True:
        try: os.waitpid(-1, 0)
        except ChildProcessError: break
print(json.dumps({'reaped': reaped}))
'''
    launcher=shlex.split(os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable))
    if len(launcher)!=1:pytest.skip('probe requires one executable launcher')
    run=subprocess.run([*launcher,'-c',script,str(probe),str(tmp_path/'ready'),launcher[0],mode],
                       cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stdout+run.stderr
    assert len(json.loads(run.stdout)['reaped'])>=2

"""Read observations reuse command output and lifecycle without scientific grants."""
import shlex
import shutil
import sys
from pathlib import Path
import pytest

from lib.project_mod.file_read_probe import run_observed_process
from lib.project_mod.run_command import _get_cmd_env, run_command_process

pytestmark = pytest.mark.unit


@pytest.fixture
def tracer():
    binary = shutil.which('strace')
    if not binary: pytest.skip('Optional Linux strace adapter is unavailable')
    return binary


def test_real_measurements_and_streaming_survive_separate_read_observation(tmp_path, tracer):
    script = tmp_path/'measure.py'
    shutil.copy2(Path(__file__).parents[1]/'fixtures/evolution/paired_search_measurement.py',script)
    command = [sys.executable, str(script)];chunks = []
    report = run_observed_process(tmp_path,['measure.py'],command,timeout_seconds=5,
        tracer=tracer,child_env=_get_cmd_env(str(tmp_path)),on_chunk=lambda stream,text:chunks.append((stream,text)))
    assert report['returncode'] == 0 and report['trace_transport_complete']
    from lib.research.action import _observed_tool_payload
    payload, state = _observed_tool_payload({'toolName':'run_command','toolArgs':{'command':shlex.join(command)}},report['command_result'])
    assert state == 'done' and payload['scientific_outcome'] == 'refuted'
    assert report['inputs'][0]['read_bytes'] > 0
    assert report['inputs'][0]['initial_sha256'] == payload['scientific_observations']['comparison']['method']['system_digest']
    assert any(stream == 'stdout' and 'scientific_observations' in text for stream,text in chunks)
    assert 'file-read-probe' not in report['command_result']


def test_shared_stop_ends_observed_command_and_clears_process_ownership(tmp_path, tracer):
    (tmp_path/'data').write_bytes(b'bytes');task = {'id':'observed-probe','aborted':False}
    def stop(*args): task['aborted'] = True
    report = run_observed_process(tmp_path,['data'],[sys.executable,'-S','-c','import time;time.sleep(30)'],
        timeout_seconds=5,tracer=tracer,task=task,child_env=_get_cmd_env(str(tmp_path)),on_spawn=stop)
    assert report['returncode'] != 0 and not report['trace_transport_complete']
    assert '_subprocess_pid' not in task


def test_invalid_guard_refuses_before_process_allocation(tmp_path):
    with pytest.raises(ValueError, match='guard must be callable'):
        run_command_process([sys.executable,'-c',"open('marker','w').write('bad')"],
            command='guard validation',timeout=1,base=str(tmp_path),child_env=_get_cmd_env(str(tmp_path)),process_guard=3)
    assert not (tmp_path/'marker').exists()


def test_guard_failure_reports_allocated_failure_and_releases_ownership(tmp_path):
    task = {'id':'guard-failure','aborted':False}
    def fail(process): raise RuntimeError('fault during guard installation')
    result = run_command_process([sys.executable,'-S','-c','import time;time.sleep(30)'],
        command='guard fault',timeout=1,base=str(tmp_path),task=task,child_env=_get_cmd_env(str(tmp_path)),process_guard=fail)
    assert 'failed after allocation' in result and '[exit code: -1]' in result
    assert '_subprocess_pid' not in task


def test_stop_during_input_hash_never_allocates_command(tmp_path, tracer, monkeypatch):
    import threading
    from lib.project_mod import file_read_probe
    (tmp_path/'data').write_bytes(b'x'*2000000)
    task = {'aborted':False, 'abort_event':threading.Event()}
    original = file_read_probe.os.read
    def stop(handle, count):
        block = original(handle,count);task['abort_event'].set();return block
    monkeypatch.setattr(file_read_probe.os, 'read', stop)
    with pytest.raises(RuntimeError,match='not allocated'):
        run_observed_process(tmp_path,['data'],[sys.executable,'-c',"open('marker','w').write('bad')"],
            timeout_seconds=5,tracer=tracer,task=task,child_env=_get_cmd_env(str(tmp_path)))
    assert not (tmp_path/'marker').exists()


def test_expired_frozen_deadline_never_allocates_observed_command(tmp_path, tracer):
    (tmp_path/'data').write_bytes(b'bytes')
    with pytest.raises(RuntimeError,match='deadline exhausted'):
        run_observed_process(tmp_path,['data'],[sys.executable,'-c',"open('marker','w').write('bad')"],
            timeout_seconds=None,tracer=tracer,deadline_epoch=1,child_env=_get_cmd_env(str(tmp_path)))
    assert not (tmp_path/'marker').exists()


def test_namespace_supervisor_keeps_its_original_cleanup_port(monkeypatch):
    from lib.project_mod import run_command
    class Process:
        pid = 123
        def poll(self): return None
        def wait(self, timeout): return 0
        def _file_read_cleanup(self, process): pytest.fail('Namespace cleanup was bypassed')
    signals = []
    monkeypatch.setattr(run_command, '_signal_namespace_child', lambda proc: signals.append(proc.pid) or True)
    run_command._kill_process_tree(Process(), namespace_supervisor=True)
    assert signals == [123]


def test_directory_alias_swap_exposes_limit_of_read_path_and_initial_hash(tmp_path, tracer):
    (tmp_path/'source').mkdir();(tmp_path/'source/data').write_bytes(b'original')
    code = "import os;os.rename('source','saved');os.mkdir('source');open('source/data','wb').write(b'substitute');open('source/data','rb').read();os.unlink('source/data');os.rmdir('source');os.rename('saved','source')"
    report = run_observed_process(tmp_path,['source/data'],[sys.executable,'-S','-c',code],
        timeout_seconds=5,tracer=tracer,child_env=_get_cmd_env(str(tmp_path)))
    assert report['returncode'] == 0
    assert report['inputs'][0]['stable_through_probe'] is True
    assert 'do not attest exact loaded bytes' in report['coverage']


@pytest.mark.parametrize('boundary', ['hash', 'admission'])
def test_preparation_timeout_never_allocates_command(tmp_path, tracer, monkeypatch, boundary):
    from types import SimpleNamespace
    from lib.project_mod import file_read_probe
    (tmp_path/'data').write_bytes(b'bytes')
    clock = [0.0]
    monkeypatch.setattr(file_read_probe, 'time', SimpleNamespace(
        monotonic=lambda:clock[0], time=lambda:100.0))
    read = file_read_probe.os.read
    def expired_read(handle, count):
        block = read(handle, count)
        clock[0] = 6.0
        return block
    def expired_admission(): clock[0] = 6.0
    if boundary == 'hash': monkeypatch.setattr(file_read_probe.os, 'read', expired_read)
    with pytest.raises(RuntimeError, match='timeout exhausted.*not allocated'):
        run_observed_process(tmp_path,['data'],[sys.executable,'-c',"open('marker','w').write('bad')"],
            timeout_seconds=5,tracer=tracer,child_env=_get_cmd_env(str(tmp_path)),
            admission_check=expired_admission if boundary == 'admission' else None)
    assert not (tmp_path/'marker').exists()


@pytest.mark.parametrize('budget, remaining', [(5, 3), (None, None)])
def test_preparation_debits_process_budget_without_capping_unbounded_calls(tmp_path, tracer, monkeypatch, budget, remaining):
    from types import SimpleNamespace
    from lib.project_mod import file_read_probe, run_command
    (tmp_path/'data').write_bytes(b'bytes')
    clock = [0.0]; captured = []
    monkeypatch.setattr(file_read_probe, 'time', SimpleNamespace(
        monotonic=lambda:clock[0], time=lambda:100.0))
    def admission(): clock[0] = 2.0
    def process(args, **kwargs):
        captured.append((kwargs['timeout'], kwargs['deadline_epoch']))
        return 'ok\n[exit code: 0]'
    monkeypatch.setattr(run_command, 'run_command_process', process)
    report = run_observed_process(tmp_path,['data'],[sys.executable,'-c','pass'],
        timeout_seconds=budget,tracer=tracer,child_env=_get_cmd_env(str(tmp_path)),
        admission_check=admission,deadline_epoch=200.0)
    assert captured == [(remaining, 200.0)]
    assert report['returncode'] == 0


def test_slow_trace_decoder_is_quiescent_before_evidence_returns(tmp_path, tracer, monkeypatch):
    import os
    import threading
    import time
    from types import SimpleNamespace
    from lib.project_mod import file_read_probe, run_command
    (tmp_path/'data').write_bytes(b'bytes')
    entered = threading.Event(); workers = []
    def thread_factory(*args, **kwargs):
        worker = threading.Thread(*args, **kwargs);workers.append(worker);return worker
    monkeypatch.setattr(file_read_probe, 'threading', SimpleNamespace(
        Thread=thread_factory, Event=threading.Event))
    decode = file_read_probe.decoded_path
    def slow_decode(value):
        entered.set()
        time.sleep(3.3)
        return decode(value)
    monkeypatch.setattr(file_read_probe, 'decoded_path', slow_decode)
    def process(args, **kwargs):
        fifo = args[args.index('-o')+1]
        with open(fifo, 'wb', buffering=0) as stream:
            stream.write(os.fsencode(f'1 read(3<{tmp_path}/data>, "", 5) = 5\n'))
        assert entered.wait(2), 'Decoder did not enter the injected delay'
        return 'ok\n[exit code: 0]'
    monkeypatch.setattr(run_command, 'run_command_process', process)
    try:
        report = run_observed_process(tmp_path,['data'],[sys.executable,'-c','pass'],
            timeout_seconds=5,tracer=tracer,child_env=_get_cmd_env(str(tmp_path)))
        assert all(not worker.is_alive() for worker in workers), 'Result escaped while trace state was mutable'
        assert not report['trace_transport_complete']
        assert report['inputs'][0]['read_bytes'] == 5
    finally:
        for worker in workers: worker.join(timeout=5)

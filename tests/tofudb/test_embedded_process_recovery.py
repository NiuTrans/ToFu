"""The recovery supervisor reaps real child processes on receipt failures."""
import importlib.util
from pathlib import Path
import signal
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def supervisor(monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'benchmarks/embedded_process_recovery.py'
    spec = importlib.util.spec_from_file_location('embedded_recovery_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    processes = []
    real_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, 'Popen', capture)
    return module, processes


@pytest.mark.parametrize('receipt,expected_error', [
    ("{'pid':os.getpid(),'phase':'running','exact_conversations':3,'exact_event_rows':6}", None),
    ("{'pid':os.getpid(),'phase':'running','exact_conversations':2,'exact_event_rows':6}", AssertionError),
    ("{'pid':os.getpid(),'phase':'verify','exact_conversations':3,'exact_event_rows':6}", AssertionError),
    ("'not a receipt object'", TypeError),
])
def test_receipt_success_or_failure_always_kills_and_reaps_owned_child(supervisor, tmp_path, receipt, expected_error):
    module, processes = supervisor
    code = f'import os,json,sys;print(json.dumps({receipt}),flush=True);sys.stdin.read(1)'
    with (tmp_path / 'child.log').open('w') as log:
        if expected_error:
            with pytest.raises(expected_error):
                module.run_child([sys.executable, '-c', code], log, 'running', 3, 6, timeout=5)
        else:
            result = module.run_child([sys.executable, '-c', code], log, 'running', 3, 6, timeout=5)
            assert result['exact_conversations'] == 3
    assert len(processes) == 1
    assert processes[0].poll() == -signal.SIGKILL
    assert processes[0].stdin.closed and processes[0].stdout.closed


@pytest.mark.parametrize('prefix', ['', '{'])
def test_missing_or_partial_receipt_times_out_and_reaps_the_child(supervisor, tmp_path, prefix):
    module, processes = supervisor
    with (tmp_path / 'child.log').open('w') as log:
        with pytest.raises(TimeoutError):
            module.run_child([sys.executable, '-c', f'import sys;sys.stdout.write({prefix!r});sys.stdout.flush();sys.stdin.read(1)'],
                log, 'running', 3, 6, timeout=0.1)
    assert processes[0].poll() == -signal.SIGKILL

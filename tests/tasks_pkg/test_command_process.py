"""Internal structured launch preserves command cancellation and failure cleanup."""
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from lib.project_mod.run_command import run_command_process

pytestmark = pytest.mark.unit


def _run(argv, tmp_path, **kwargs):
    return run_command_process(argv, command='controlled process', timeout=5,
                               base=str(tmp_path), child_env={'PATH': '/usr/bin:/bin'},
                               **kwargs)


def test_structured_arguments_cannot_become_host_shell(tmp_path):
    marker = tmp_path / 'injected'
    literal = f'$(touch {marker}); echo expanded'
    result = _run((sys.executable, '-c', 'import sys; print(sys.argv[1])', literal), tmp_path)
    assert literal in result
    assert not marker.exists()
    assert '[exit code: 0]' in result


def test_context_cancel_before_spawn_never_starts_process(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, 'Popen', lambda *_a, **_kw: pytest.fail('must not spawn'))
    result = _run((sys.executable, '-c', 'print("started")'), tmp_path,
                  runtime_context=SimpleNamespace(cancellation_requested=True))
    assert 'aborted' in result.lower()


def test_cancel_during_spawn_survives_callback_registration(tmp_path):
    class Context:
        cancellation_requested = False

        def register_cancel_callback(self, _callback):
            return lambda: None

    context = Context()
    result = _run((sys.executable, '-c', 'import time;time.sleep(30)'), tmp_path,
                  runtime_context=context,
                  on_spawn=lambda *_args: setattr(context, 'cancellation_requested', True))
    assert 'aborted' in result.lower()


def test_cancel_registration_failure_reaps_started_process(tmp_path, monkeypatch):
    processes = []
    real_popen = subprocess.Popen

    def track(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    class Context:
        cancellation_requested = False

        def register_cancel_callback(self, _callback):
            raise RuntimeError('controlled registration failure')

    monkeypatch.setattr(subprocess, 'Popen', track)
    task = {}
    try:
        result = _run((sys.executable, '-c', 'import time;time.sleep(30)'), tmp_path,
                      runtime_context=Context(), task=task)
        assert 'controlled registration failure' in result
        assert processes[0].poll() is not None
        assert '_subprocess_pid' not in task and '_subprocess_pgid' not in task
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


def test_original_deadline_survives_slow_spawn_observer(tmp_path):
    deadline = time.time() + 0.1
    observed_deadlines = []

    def slow_observer(_start, deadline_ms):
        observed_deadlines.append(deadline_ms)
        time.sleep(0.2)

    result = _run((sys.executable, '-c', 'import time;time.sleep(30)'), tmp_path,
                  deadline_epoch=deadline, on_spawn=slow_observer)
    assert 'timed out' in result.lower()
    assert observed_deadlines == [deadline * 1000.0]

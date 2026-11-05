"""A stall window renewed by kernel-observed execution, never a wall clock."""
import os
import sys
import threading
import time

import pytest

from lib.project_mod import activity_supervised_process as supervised
from lib.project_mod.activity_supervised_process import (
    ProcessCancelled, ProcessRetirementUnproven, ProcessStalled, run_activity_supervised)
from lib.project_mod.tool_admission import Cancellation

pytestmark = pytest.mark.unit

SLOW = 'import time\nfor _ in range(50):\n    time.sleep(0.03)\n'


def run(tmp_path, script, *, stall=0.5, cancel=None):
    return run_activity_supervised([sys.executable, '-c', script], cwd=tmp_path,
                                   env=os.environ.copy(), stall_seconds=stall,
                                   cancellation=cancel)


@pytest.fixture(params=['procfs', 'portable'])
def activity_source(request, monkeypatch):
    if request.param == 'procfs' and not sys.platform.startswith('linux'):
        pytest.skip('procfs activity is Linux-only')
    monkeypatch.setattr(supervised, '_PROCFS', request.param == 'procfs')
    return request.param


def test_slow_progress_outlives_the_window_and_returns_complete_output(tmp_path, activity_source):
    started = time.monotonic()
    result = run(tmp_path, SLOW + "print('x' * 200000)\nimport sys\nsys.stderr.write('done')")
    assert time.monotonic() - started > 1.0  # two stall windows
    assert result.returncode == 0
    assert result.stdout == 'x' * 200000 + '\n'
    assert result.stderr == 'done'


def test_blocked_process_is_retired_after_one_window(tmp_path, activity_source):
    marker = tmp_path / 'pid'
    started = time.monotonic()
    with pytest.raises(ProcessStalled, match='0.5s'):
        run(tmp_path, f"import os, time\nopen({str(marker)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)")
    assert time.monotonic() - started < 0.5 + 10
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_progressing_descendant_renews_its_waiting_parent(tmp_path, activity_source):
    result = run(tmp_path, f"import subprocess, sys\nsubprocess.run([sys.executable, '-c', {SLOW!r}], check=True)")
    assert result.returncode == 0


def test_nonzero_exit_is_a_result_not_an_error(tmp_path):
    result = run(tmp_path, "import sys\nsys.stderr.write('fatal: refused')\nsys.exit(128)")
    assert (result.returncode, result.stderr) == (128, 'fatal: refused')


def test_cancellation_retires_promptly(tmp_path):
    task = {'aborted': False}
    timer = threading.Timer(0.3, lambda: task.update(aborted=True))
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(ProcessCancelled):
            run(tmp_path, 'import time; time.sleep(60)', stall=60, cancel=Cancellation(task))
    finally:
        timer.join()
    assert time.monotonic() - started < 10


@pytest.mark.parametrize('stall', [0, -1, float('inf'), float('nan')])
def test_invalid_window_refuses_before_launch(tmp_path, stall):
    marker = tmp_path / 'launched'
    with pytest.raises(ValueError, match='positive'):
        run(tmp_path, f"open({str(marker)!r}, 'w')", stall=stall)
    assert not marker.exists()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX escaped helper')
def test_escaped_pipe_holder_cannot_hang_teardown(tmp_path):
    import signal
    marker = tmp_path / 'escaped.pid'
    script = f"""
import subprocess, sys
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
open({str(marker)!r}, 'w').write(str(child.pid))
"""
    started = time.monotonic()
    try:
        with pytest.raises(ProcessRetirementUnproven, match='pipe'):
            run(tmp_path, script, stall=60)
        assert time.monotonic() - started < 10
    finally:
        if marker.exists():
            os.killpg(int(marker.read_text()), signal.SIGKILL)

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


def test_each_renewal_is_reported_to_the_owner_as_liveness(tmp_path, activity_source):
    """An owner judged by its own silence (a delegating Coordinator) publishes the
    same kernel evidence that renews the window, observed on its waiting thread."""
    waiter, renewals = threading.get_ident(), []
    result = run_activity_supervised(
        [sys.executable, '-c', SLOW], cwd=tmp_path, env=os.environ.copy(), stall_seconds=0.5,
        on_activity=lambda: renewals.append(threading.get_ident()))
    assert result.returncode == 0
    assert renewals and set(renewals) == {waiter}


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


def test_supervised_stdin_reaches_a_child_that_reads_late(tmp_path, activity_source):
    """A batched check's whole input is stdin from a file, so a child that reads
    late, or reads gigabytes, never blocks the waiting thread."""
    payload = os.urandom(1024 * 1024) * 3
    result = run_activity_supervised(
        [sys.executable, '-c',
         'import hashlib, sys, time\ntime.sleep(0.6)\n'
         "print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"],
        cwd=tmp_path, env=os.environ.copy(), stall_seconds=0.5, input=payload)
    import hashlib
    assert result.returncode == 0 and result.stdout.strip() == hashlib.sha256(payload).hexdigest()


def test_an_exchange_answers_request_after_request_and_retires_on_close(tmp_path, activity_source):
    """One long-lived checker answers many requests; the window runs only while an
    answer is awaited, so an idle checker is not stalled."""
    from lib.project_mod.activity_supervised_process import ActivitySupervisedExchange
    from tests.support.volume_git import volume_git
    import shutil
    import subprocess
    real = shutil.which('git')
    script, _ = volume_git(tmp_path, 'slow', 'check-ignore', seconds=2.5)
    root = tmp_path / 'folder'
    (root / 'data').mkdir(parents=True)
    (root / 'src').mkdir()
    (root / 'data' / 'f').write_text('x')
    (root / '.gitignore').write_text('data/\n!keep.log\n')
    (root / 'keep.log').write_text('x')
    git_dir = tmp_path / 'scratch'
    subprocess.run([real, 'init', '-q', '--bare', str(git_dir)], check=True, capture_output=True)
    subprocess.run([real, '--git-dir', str(git_dir), 'config', 'core.bare', 'false'], check=True)
    command = [str(script), '-c', 'core.excludesFile=/dev/null', '--git-dir', str(git_dir),
               '--work-tree', str(root), 'check-ignore', '--stdin', '-z', '--non-matching',
               '--verbose']
    answers, advances = [], []
    with ActivitySupervisedExchange(command, cwd=root, env={**os.environ, 'GIT_FLUSH': '1'},
                                    stall_seconds=0.5, on_activity=lambda: advances.append(1)) as check:
        for path in ('src', 'data', 'data/f', 'keep.log', 'missing'):
            answers.append((path, check.exchange(path.encode() + b'\0', 4)))
        time.sleep(1.2)  # longer than one window between requests: idle, not stalled
        answers.append(('again', check.exchange(b'src\0', 4)))
    # A path an ignore rule matches is hidden, except through a negated rule (!keep.log).
    assert [(path, bool(source) and not pattern.startswith(b'!'))
            for path, (source, _line, pattern, _p) in answers] == [
        ('src', False), ('data', True), ('data/f', True), ('keep.log', False),
        ('missing', False), ('again', False)]
    assert answers[3][1][2] == b'!keep.log'
    assert advances, 'a slow answer renews the window'


def test_an_exchange_ends_an_unresponsive_checker_after_one_window(tmp_path, activity_source):
    import signal
    from lib.project_mod.activity_supervised_process import ActivitySupervisedExchange
    marker = tmp_path / 'hung.pid'
    with ActivitySupervisedExchange(
            [sys.executable, '-c', f"import os, sys, time\n"
                                   f"sys.stdin.read(1)\n"
                                   f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
                                   "time.sleep(60)"],
            cwd=tmp_path, env=os.environ.copy(), stall_seconds=0.5) as check:
        started = time.monotonic()
        with pytest.raises(ProcessStalled, match='0.5s'):
            check.exchange(b'path\0', 4)
        assert time.monotonic() - started < 10
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_an_exchange_reports_a_checker_that_exited_instead_of_waiting_forever(
        tmp_path, activity_source):
    from lib.project_mod.activity_supervised_process import ActivitySupervisedExchange
    with ActivitySupervisedExchange([sys.executable, '-c', "import sys\nsys.stderr.write('fatal')\nsys.exit(2)"],
                                    cwd=tmp_path, env=os.environ.copy(), stall_seconds=5) as check:
        with pytest.raises(RuntimeError, match='fatal'):
            check.exchange(b'path\0', 4)

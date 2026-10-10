"""Native TeX process-tree resource budgets and retirement, without a cloud service."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import psutil
import pytest

from lib.paper.builds.budget import compile_memory_bytes
from lib.paper.builds.contract import limit
from lib.paper.builds.errors import BuildError
from lib.paper.builds.process import CommandUsage, run_command

pytestmark = pytest.mark.unit
MIB = 1024 * 1024
ROOT = Path(__file__).resolve().parents[2]


def environment():
    return {**os.environ, 'TOFU_PAPER_BUILD_COMPILE_MAX_MIB': '256'}


def command(tmp_path, script, **kwargs):
    return run_command([sys.executable, '-c', script], cwd=tmp_path,
        environment=environment(), log=tmp_path / 'command.log', timeout=kwargs.pop('timeout', 8),
        log_bytes=kwargs.pop('log_bytes', limit('log_bytes')), **kwargs)


def assert_retired(pid):
    try:
        process = psutil.Process(pid)
        assert not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        pass


def test_command_observes_tree_usage_and_allows_artifacts_larger_than_its_log(tmp_path):
    usage = command(tmp_path, "from pathlib import Path; Path('artifact.pdf').write_bytes(b'x' * 2097152)")
    assert usage.returncode == 0
    assert 0 < usage.peak_memory_bytes <= 256 * MIB
    assert 1 <= usage.peak_processes <= limit('compile_processes')
    assert (tmp_path / 'artifact.pdf').stat().st_size == 2 * MIB
    assert not list(tmp_path.glob('.paper-command-*'))


@pytest.mark.skipif(os.name != 'posix', reason='POSIX aggregate RSS and native allocation fixture')
def test_command_native_address_limit_refuses_allocation_before_rss_growth(tmp_path):
    usage = command(tmp_path, """
import mmap
try:
    value = mmap.mmap(-1, 1024 * 1024 * 1024)
except OSError:
    print('allocation refused')
else:
    raise AssertionError('native address-space limit missing')
""")
    assert usage.returncode == 0
    assert (tmp_path / 'command.log').read_text().strip() == 'allocation refused'
    assert usage.peak_memory_bytes < 256 * MIB


def test_command_aggregate_memory_includes_compiler_children(tmp_path):
    child = "import time; data=[bytearray(b'x'*4194304) for _ in range(40)]; time.sleep(30)"
    script = f"""
import subprocess, sys, time
from pathlib import Path
for number in range(2):
    child = subprocess.Popen([sys.executable, '-c', {child!r}])
    Path('child-' + str(number)).write_text(str(child.pid))
time.sleep(30)
"""
    usage = CommandUsage()
    with pytest.raises(BuildError) as failure:
        command(tmp_path, script, usage=usage)
    assert failure.value.code == 'compile_limit'
    assert usage.peak_memory_bytes > 256 * MIB
    for marker in tmp_path.glob('child-*'):
        assert_retired(int(marker.read_text()))
    assert not list(tmp_path.glob('.paper-command-*'))


@pytest.mark.skipif(os.name != 'posix', reason='POSIX shell-free sleep process fixture')
def test_command_process_count_is_bounded_independently_of_memory(tmp_path):
    script = """
import subprocess, time
from pathlib import Path
for number in range(20):
    child = subprocess.Popen(['/bin/sleep', '30'])
    Path('child-' + str(number)).write_text(str(child.pid))
time.sleep(30)
"""
    usage = CommandUsage()
    with pytest.raises(BuildError) as failure:
        command(tmp_path, script, usage=usage)
    assert failure.value.code == 'compile_limit'
    assert usage.peak_processes > limit('compile_processes')
    for marker in tmp_path.glob('child-*'):
        assert_retired(int(marker.read_text()))


@pytest.mark.parametrize('failure', ['cancel', 'timeout'])
def test_command_cancellation_and_deadline_retire_its_native_child(tmp_path, monkeypatch, failure):
    script = """
import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path('child.pid').write_text(str(child.pid))
time.sleep(30)
"""
    cancel = threading.Event()
    marker = tmp_path / 'child.pid'
    stop = threading.Event()
    watcher = None
    if failure == 'timeout':
        from types import SimpleNamespace
        from lib.paper.builds import process as commands
        real_clock = time.monotonic
        # Expire only once the native child exists. Slow executable startup
        # cannot accidentally turn this into a before-start timeout test.
        monkeypatch.setattr(commands, 'time', SimpleNamespace(
            monotonic=lambda: real_clock() + (20 if marker.exists() else 0)))
    else:
        def after_start():
            while not stop.wait(0.01):
                if marker.exists():
                    cancel.set()
                    return
        watcher = threading.Thread(target=after_start)
        watcher.start()
    try:
        with pytest.raises(BuildError) as caught:
            command(tmp_path, script, cancel=cancel)
    finally:
        stop.set()
        if watcher:
            watcher.join(timeout=2)
    assert caught.value.code == ('cancelled' if failure == 'cancel' else 'compile_timeout')
    assert_retired(int(marker.read_text()))
    assert not list(tmp_path.glob('.paper-command-*'))


@pytest.mark.skipif(os.name != 'posix', reason='POSIX process session ownership')
def test_successful_command_cannot_leave_a_background_child(tmp_path):
    usage = command(tmp_path, """
import subprocess
from pathlib import Path
child = subprocess.Popen(['/bin/sleep', '30'])
Path('child.pid').write_text(str(child.pid))
""")
    assert usage.returncode == 0
    assert_retired(int((tmp_path / 'child.pid').read_text()))


@pytest.mark.skipif(os.name != 'posix', reason='POSIX parent-death process group ownership')
def test_supervisor_crash_retires_the_compiler_session(tmp_path):
    script = """
from pathlib import Path
import os, sys
from lib.paper.builds.process import run_command
root = Path(sys.argv[1])
run_command([sys.executable, '-c', 'import os, time; from pathlib import Path; Path("child.pid").write_text(str(os.getpid())); time.sleep(30)'],
    cwd=root, environment=dict(os.environ), log=root/'command.log', timeout=20, log_bytes=1048576)
"""
    process = subprocess.Popen([sys.executable, '-c', script, str(tmp_path)], cwd=ROOT,
        env=environment(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = None
    try:
        deadline = time.monotonic() + 8
        while not (tmp_path / 'child.pid').exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.02)
        child_pid = int((tmp_path / 'child.pid').read_text())
        worker = psutil.Process(child_pid).parent()
        assert worker is not None
        process.kill()
        process.wait(timeout=5)
        gone, alive = psutil.wait_procs([worker, psutil.Process(child_pid)], timeout=3)
        for child in alive:
            assert child.status() == psutil.STATUS_ZOMBIE
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if child_pid is not None:
            try:
                psutil.Process(child_pid).kill()
            except psutil.NoSuchProcess:
                pass


def test_compile_budget_cannot_be_disabled_or_exceed_its_contract_ceiling():
    assert compile_memory_bytes({'TOFU_PAPER_BUILD_COMPILE_MAX_MIB': '999999'}) == limit('compile_memory_bytes')
    assert compile_memory_bytes({'TOFU_PAPER_BUILD_COMPILE_MAX_MIB': '1'}) == 256 * MIB


def test_refused_worker_arguments_cannot_reclaim_unadmitted_files(tmp_path):
    from lib.paper.builds.process_worker import main
    ready = tmp_path / 'ready'
    ready.write_text('Preserved caller data')
    result = main(['0', '1', '4096', str(os.getpid()), str(ready),
                   str(tmp_path / 'log'), str(tmp_path), sys.executable])
    assert result == 125
    assert ready.read_text() == 'Preserved caller data'


@pytest.mark.skipif(os.name != 'posix', reason='POSIX executable version-probe fixture')
@pytest.mark.parametrize('failure', ['output', 'cancel'])
def test_toolchain_version_probe_is_bounded_cancellable_and_leaves_no_log(tmp_path, monkeypatch, failure):
    from lib.paper.builds import toolchain
    fake = tmp_path / 'pdflatex'
    body = "import os; os.write(1, b'x' * 65536)" if failure == 'output' else 'import time; time.sleep(30)'
    fake.write_text('#!' + sys.executable + '\n' + body + '\n')
    fake.chmod(0o700)
    (tmp_path / 'main.tex').write_text(r'\documentclass{article}')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setattr(toolchain, 'toolchain_environment', lambda *_, **__: environment())
    monkeypatch.setattr(toolchain.shutil, 'which', lambda *_a, **_kw: str(fake))
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set) if failure == 'cancel' else None
    if timer:
        timer.start()
    try:
        with pytest.raises(BuildError) as caught:
            toolchain.prepare_toolchain(tmp_path, 'main.tex', scratch, cancel=cancel)
    finally:
        if timer:
            timer.cancel()
            timer.join()
    assert caught.value.code == ('cancelled' if failure == 'cancel' else 'output_limit')
    assert not list(scratch.iterdir())


@pytest.mark.parametrize('launcher', ['desktop/launcher.py', 'desktop/agent_launcher.py'])
def test_frozen_command_dispatch_works_without_console_or_application_startup(tmp_path, launcher):
    ready = tmp_path / 'ipc/ready'
    ready.parent.mkdir()
    ready.write_bytes(b'1')
    native = [sys.executable, '-c', "from pathlib import Path; Path('native.txt').write_text('ran')"]
    script = """
import os, runpy, sys
root, launcher, target = sys.argv[1:4]
sys.path.insert(0, root)
sys.frozen = True
sys.stdin = sys.stdout = sys.stderr = None
sys.argv = [launcher, '--paper-command-worker', str(256*1024*1024), '5', '1048576',
            str(os.getppid()), target+'/ipc/ready', target+'/command.log', target, *sys.argv[4:]]
runpy.run_path(root+'/'+launcher, run_name='__main__')
"""
    result = subprocess.run([sys.executable, '-c', script, str(ROOT), launcher, str(tmp_path), *native],
        cwd=tmp_path, env=environment(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'native.txt').read_text() == 'ran'
    assert not (tmp_path / 'data').exists()
    assert not ready.parent.exists()

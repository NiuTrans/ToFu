"""Supervise one bounded TeX command, including its descendants and output.

run_command owns admission, RSS/count observation, cancellation and retirement.
The caller owns the log file and optional aggregate-output check. Native limits
run in a fresh worker, never a preexec callback in the threaded application.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from .budget import compile_memory_bytes
from .contract import limit
from .errors import BuildError, check_cancelled
from .process_worker import WORKER_FLAG


@dataclass
class CommandUsage:
    peak_memory_bytes: int = 0
    peak_processes: int = 0
    returncode: int | None = None


def _command(command, cwd, memory, seconds, file_bytes, ready, log):
    mode = [WORKER_FLAG] if getattr(sys, 'frozen', False) else ['-m', 'lib.paper.builds.process_worker']
    return [sys.executable, *mode, str(memory), str(seconds), str(file_bytes),
            str(os.getpid()), str(ready), str(log), str(cwd), *command]


def run_command(command: list[str], *, cwd: Path, environment: dict[str, str],
                log: Path, timeout: float, log_bytes: int, cancel=None,
                inspect_output=None, usage: CommandUsage | None = None,
                file_bytes: int | None = None) -> CommandUsage:
    """A finite command result; no output bytes are accumulated in Python RAM."""
    import psutil
    check_cancelled(cancel)
    seconds = min(max(0, timeout), limit('compile_timeout_seconds'))
    if seconds <= 0:
        raise BuildError('compile_timeout', 'Paper compilation timed out')
    memory = compile_memory_bytes(environment)
    maximum_processes = limit('compile_processes')
    result = usage if usage is not None else CommandUsage()
    file_bytes = min(file_bytes or limit('build_output_bytes'), limit('build_output_bytes'))
    deadline = time.monotonic() + seconds
    process = None
    job = None
    observed = {}
    temporary = tempfile.TemporaryDirectory(prefix='.paper-command-', dir=log.parent)
    ready = (Path(temporary.name) / 'ready').resolve()
    try:
        with log.open('wb'):
            process = subprocess.Popen(_command(command, cwd.resolve(), memory, seconds, file_bytes, ready, log.resolve()),
                cwd=Path(__file__).resolve().parents[3], env=environment,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=os.name != 'nt')
            if os.name == 'nt':
                from .windows_job import WindowsJob
                job = WindowsJob(process, memory, maximum_processes)
            ready.write_bytes(b'1')
            parent = psutil.Process(process.pid)
            next_output_check = 0.0
            while True:
                check_cancelled(cancel)
                if time.monotonic() >= deadline:
                    raise BuildError('compile_timeout', 'Paper compilation timed out')
                rss = count = 0
                try:
                    observed = {pid: child for pid, child in observed.items()
                                if child.is_running() and child.status() != psutil.STATUS_ZOMBIE}
                    children = [parent, *parent.children(recursive=True)]
                    for child in children:
                        try:
                            rss += child.memory_info().rss
                            count += 1
                            observed[child.pid] = child
                        except psutil.NoSuchProcess:
                            pass
                except psutil.NoSuchProcess:
                    pass
                result.peak_memory_bytes = max(result.peak_memory_bytes, rss)
                result.peak_processes = max(result.peak_processes, count)
                if rss > memory or count > maximum_processes:
                    raise BuildError('compile_limit', 'Paper compilation exceeded its memory or process budget')
                if log.stat().st_size > log_bytes:
                    raise BuildError('output_limit', 'Compiler log exceeds the byte limit')
                result.returncode = process.poll()
                if inspect_output is not None and (time.monotonic() >= next_output_check or result.returncode is not None):
                    inspect_output()
                    next_output_check = time.monotonic() + 0.1
                if result.returncode is not None:
                    break
                try:
                    process.wait(timeout=0.02)
                except subprocess.TimeoutExpired:
                    pass
            check_cancelled(cancel)
            if result.returncode in (124, -getattr(signal, 'SIGKILL', 9)):
                raise BuildError('compile_timeout', 'Paper compilation exceeded its native deadline')
            if result.returncode == 125:
                raise BuildError('compile_unavailable', 'Cannot start the bounded paper compiler')
            if result.returncode != 0 and log.stat().st_size >= log_bytes:
                raise BuildError('output_limit', 'Compiler log exhausted its byte limit')
            if result.returncode == 128 + getattr(signal, 'SIGXFSZ', 25):
                raise BuildError('output_limit', 'Paper compilation exceeded its native file byte limit')
            if result.returncode in (128 + getattr(signal, 'SIGXCPU', 24),
                                      128 + getattr(signal, 'SIGKILL', 9)):
                raise BuildError('compile_limit', 'Paper compilation exceeded its native resource budget')
            return result
    except psutil.Error as exc:
        raise BuildError('compile_unavailable', 'Cannot observe the paper compiler process budget') from exc
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    ready.with_name('stop').touch()
                except FileNotFoundError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass  # Native ownership is the fallback if cooperative retirement stalls.
            if job is not None:
                job.close()
            elif os.name != 'nt':
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            for child in observed.values():
                try:
                    child.kill()  # psutil verifies identity before signalling.
                except psutil.NoSuchProcess:
                    pass
            process.wait(timeout=5)
            psutil.wait_procs(list(observed.values()), timeout=1)
        temporary.cleanup()
        if log.is_file() and log.stat().st_size > log_bytes:
            with log.open('r+b') as output:
                output.truncate(log_bytes)

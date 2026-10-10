"""Finite native command entry for TeX and its version probe.

The parent admits this worker before a private one-byte start marker. POSIX limits
are inherited by the command; its whole session dies if the parent disappears.
Windows admission and parent lifetime belong to the parent's Job Object.
No application, HTTP, storage or optional PDF parser is loaded here.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .contract import limit

WORKER_FLAG = '--paper-command-worker'


def _adopt_orphans() -> None:
    if sys.platform == 'linux':
        import ctypes
        library = ctypes.CDLL(None, use_errno=True)
        # Keep grandchildren waitable even if latexmk exits before them.
        if library.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), 'Cannot own compiler descendants')


def _retire(process, observed) -> None:
    import psutil
    for child in psutil.Process().children(recursive=True):
        observed[child.pid] = child
    for child in observed.values():
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    if process is not None:
        process.wait(timeout=5)
    if sys.platform == 'linux':
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    time.sleep(0.01)
            except ChildProcessError:
                break


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    if len(args) < 8:
        return 125
    process = None
    observed = {}
    ready = None
    admitted = False
    try:
        memory, seconds, file_bytes, parent_pid = int(args[0]), float(args[1]), int(args[2]), int(args[3])
        ready, log = Path(args[4]), Path(args[5])
        working_directory, command = args[6], args[7:]
        if not (0 < memory <= limit('compile_memory_bytes') and 0 < seconds <= limit('compile_timeout_seconds')
                and 0 < file_bytes <= limit('build_output_bytes') and parent_pid > 0):
            return 125
        deadline = time.monotonic() + seconds
        while not ready.exists():
            if time.monotonic() >= deadline or (os.name != 'nt' and os.getppid() != parent_pid):
                return 125
            time.sleep(0.01)
        with ready.open('rb') as admission:
            if admission.read(2) != b'1':
                return 125
        admitted = True
        _adopt_orphans()
        if os.name != 'nt':
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_AS, (memory * 2, memory * 2))
            resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(seconds), math.ceil(seconds)))
            resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
        # Explicit files also work in windowed frozen executables, whose
        # standard stream objects and descriptors may be absent.
        with log.open('ab', buffering=0) as output:
            process = subprocess.Popen(command, cwd=working_directory, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT)
        import psutil
        owner = psutil.Process()
        while process.poll() is None:
            if (time.monotonic() >= deadline or ready.with_name('stop').exists()
                    or (os.name != 'nt' and os.getppid() != parent_pid)):
                return 124
            observed = {pid: child for pid, child in observed.items() if child.is_running()}
            for child in owner.children(recursive=True):
                observed[child.pid] = child
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        return process.returncode if process.returncode >= 0 else 128 - process.returncode
    except (OSError, ValueError, subprocess.SubprocessError):
        return 125
    finally:
        if process is not None:
            _retire(process, observed)
        if admitted:
            # Exact private IPC files only; the build and its log remain owned
            # by the caller, including after a supervisor crash.
            ready.unlink(missing_ok=True)
            ready.with_name('stop').unlink(missing_ok=True)
            try:
                ready.parent.rmdir()
            except OSError:
                pass
        if process is not None and os.name != 'nt' and os.getppid() != parent_pid:
            os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == '__main__':
    raise SystemExit(main())

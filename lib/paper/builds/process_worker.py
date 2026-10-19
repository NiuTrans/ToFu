"""Joined command supervisor for TeX probes and desktop shell execution.

The parent admits this worker before a private one-byte start marker. POSIX limits
are inherited by the command; its whole session dies if the parent disappears.
Windows admission and parent lifetime belong to the parent's Job Object.
No application, HTTP, storage or optional PDF parser is loaded here.
"""
from __future__ import annotations

import math
import json
import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


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


def desktop_worker_command(command, *, cwd, ready, stdout, stderr, status,
                           parent_pid, parent_birth):
    """The single source/frozen argv layout for desktop-owned supervision."""
    mode = [WORKER_FLAG] if getattr(sys, 'frozen', False) else ['-m', __name__]
    return [sys.executable, *mode, '--desktop-streams', str(stderr), str(status),
            repr(parent_birth), '0', '0', '0', str(parent_pid), str(ready),
            str(stdout), str(cwd), *command]


def load_desktop_status(path):
    """Validate the private completion record before caller settlement."""
    status = json.loads(Path(path).read_text(encoding='utf-8'))
    if (not isinstance(status, dict) or set(status) != {'returncode', 'error', 'stopped'}
            or type(status.get('returncode')) is not int
            or status.get('error') is not None and not isinstance(status['error'], str)
            or type(status.get('stopped')) is not bool):
        raise ValueError('Invalid desktop command completion record')
    return status


def _owner_alive(parent_pid, parent_birth):
    if os.name != 'nt':
        return os.getppid() == parent_pid
    import psutil
    try:
        owner = psutil.Process(parent_pid)
        return (owner.is_running() and owner.status() != psutil.STATUS_ZOMBIE
                and (parent_birth is None or owner.create_time() == parent_birth))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def main(arguments: list[str] | None = None) -> int:
    args = sys.argv[1:] if arguments is None else arguments
    stream_paths = None
    parent_birth = None
    if args[:1] == ['--desktop-streams']:
        if len(args) < 12:
            return 125
        stream_paths = (Path(args[1]), Path(args[2]))
        try:
            parent_birth = float(args[3])
        except (ValueError, TypeError):
            return 125
        if not math.isfinite(parent_birth) or parent_birth <= 0:
            return 125
        args = args[4:]
    if len(args) < 8:
        return 125
    process = None
    observed = {}
    ready = None
    admitted = False
    failure = None
    stopped = False
    desktop_scope_verified = False
    try:
        memory, seconds, file_bytes, parent_pid = int(args[0]), float(args[1]), int(args[2]), int(args[3])
        ready, log = Path(args[4]), Path(args[5])
        working_directory, command = args[6], args[7:]
        if not (memory >= 0 and math.isfinite(seconds) and seconds >= 0
                and file_bytes >= 0 and parent_pid > 0):
            return 125
        deadline = time.monotonic() + seconds if seconds else None
        if stream_paths and (log.parent != ready.parent
                or any(path.parent != ready.parent for path in stream_paths)
                or not ready.parent.name.startswith('tofu-desktop-command-')
                or (ready.name, log.name, stream_paths[0].name, stream_paths[1].name)
                   != ('ready', 'stdout', 'stderr', 'status.json')):
            return 125
        desktop_scope_verified = stream_paths is not None
        if not _owner_alive(parent_pid, parent_birth):
            return 125
        while not ready.exists():
            if ready.with_name('stop').exists():
                stopped = True
                return 124
            if (deadline is not None and time.monotonic() >= deadline) or (not _owner_alive(parent_pid, parent_birth)):
                return 125
            time.sleep(0.01)
        with ready.open('rb') as admission:
            if admission.read(2) != b'1':
                return 125
        if not _owner_alive(parent_pid, parent_birth):
            return 125
        if ready.with_name('stop').exists():
            stopped = True
            return 124
        admitted = True
        import psutil
        _adopt_orphans()
        if os.name != 'nt':
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            if memory:
                resource.setrlimit(resource.RLIMIT_AS, (memory * 2, memory * 2))
            if seconds:
                resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(seconds), math.ceil(seconds)))
            if file_bytes:
                resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
        # Explicit files also work in windowed frozen executables, whose
        # standard stream objects and descriptors may be absent.
        if not _owner_alive(parent_pid, parent_birth):
            return 125
        if ready.with_name('stop').exists():
            stopped = True
            return 124
        with contextlib.ExitStack() as streams:
            output = streams.enter_context(log.open('ab', buffering=0))
            errors = (streams.enter_context(stream_paths[0].open('ab', buffering=0))
                      if stream_paths else subprocess.STDOUT)
            process = subprocess.Popen(command, cwd=working_directory, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=errors)
        owner = psutil.Process()
        while process.poll() is None:
            if ((deadline is not None and time.monotonic() >= deadline) or ready.with_name('stop').exists()
                    or (not _owner_alive(parent_pid, parent_birth))):
                stopped = True
                return 124
            observed = {pid: child for pid, child in observed.items() if child.is_running()}
            for child in owner.children(recursive=True):
                observed[child.pid] = child
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        return process.returncode if process.returncode >= 0 else 128 - process.returncode
    except (OSError, ValueError, ImportError, subprocess.SubprocessError) as error:
        failure = f'{type(error).__name__}: {error}'
        return 125
    finally:
        if process is not None:
            _retire(process, observed)
        if desktop_scope_verified:
            status = {'returncode': process.returncode if process is not None else 125,
                      'error': failure, 'stopped': stopped}
            stream_paths[1].write_text(json.dumps(status), encoding='utf-8')
        if admitted:
            # Exact private IPC files only; the build and its log remain owned
            # by the caller, including after a supervisor crash.
            ready.unlink(missing_ok=True)
            ready.with_name('stop').unlink(missing_ok=True)
            try:
                ready.parent.rmdir()
            except OSError:
                pass
        if desktop_scope_verified and not _owner_alive(parent_pid, parent_birth):
            for owned in (log, *stream_paths, ready, ready.with_name('stop')):
                owned.unlink(missing_ok=True)
            try:
                ready.parent.rmdir()
            except OSError:
                pass
        if process is not None and os.name != 'nt' and not _owner_alive(parent_pid, parent_birth):
            os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == '__main__':
    raise SystemExit(main())

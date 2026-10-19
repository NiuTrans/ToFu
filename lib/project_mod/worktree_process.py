"""Controlled worktree materialization, not general command execution.

Entry: run_worktree_add. The integration owner supplies sanitized environment,
lease FDs and an unowned destination. Git file-count progress renews an idle
budget; output noise cannot extend it. One bounded reader drains the child,
and teardown retires the process group before the caller can clean up.
"""
from __future__ import annotations

import os
import re
import math
import subprocess
import sys
import threading
import time
from pathlib import Path

from lib.log import get_logger

logger = get_logger(__name__)

_PROGRESS = re.compile(rb'(?:Updating|Checking out) files:\s*\d+%\s*\((\d+)/(\d+)\)')
_OUTPUT_LIMIT = 8192


class WorktreeRetirementUnproven(RuntimeError):
    """Caller must preserve the destination: a writer may still be alive."""


def run_worktree_add(argv: list[str], *, cwd: Path, env: dict[str, str],
                     pass_fds: tuple[int, ...] = (), cancellation=None,
                     idle_timeout: float = 90.0) -> subprocess.CompletedProcess[str]:
    """A healthy large checkout may exceed 90s; a stalled checkout may not.

    LC_ALL fixes Git's progress grammar. Only increasing completed-file counts
    with an unchanged positive total renew the deadline. Hooks must be disabled
    by the caller. No command is replayed after a timeout or ambiguous write.
    """
    if not math.isfinite(idle_timeout) or idle_timeout <= 0:
        raise ValueError('Worktree idle timeout must be positive')
    process = subprocess.Popen(
        argv, cwd=str(cwd), env={**env, 'LC_ALL': 'C'},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=(os.name == 'posix'), pass_fds=pass_fds,
    )
    lock = threading.Lock()
    tail = bytearray()
    last_progress = time.monotonic()
    completed = 0
    total = None
    reader_error = []

    stop_reader = threading.Event()
    eof = threading.Event()

    def drain():
        nonlocal last_progress, completed, total
        carry = b''
        try:
            while not stop_reader.is_set():
                try:
                    chunk = os.read(process.stdout.fileno(), 4096)
                except BlockingIOError:
                    stop_reader.wait(0.01)
                    continue
                if not chunk:
                    eof.set()
                    return
                data = carry + chunk
                with lock:
                    tail.extend(chunk)
                    del tail[:-_OUTPUT_LIMIT]
                    for match in _PROGRESS.finditer(data):
                        count, size = map(int, match.groups())
                        if size > 0 and 0 < count <= size:
                            if total is None:
                                total = size
                            if size == total and count > completed:
                                completed = count
                                last_progress = time.monotonic()
                carry = data[-256:]
        except Exception as error:
            reader_error.append(error)

    reader = threading.Thread(target=drain, name='git-worktree-output', daemon=True)
    try:
        os.set_blocking(process.stdout.fileno(), False)
        reader.start()
        while True:
            if cancellation is not None and cancellation.is_set():
                raise RuntimeError('Task cancelled during workspace materialization')
            with lock:
                idle = time.monotonic() - last_progress
                detail = bytes(tail).decode('utf-8', errors='replace')
            if reader_error:
                raise RuntimeError('Git worktree output reader failed') from reader_error[0]
            if idle >= idle_timeout:
                raise RuntimeError(
                    f'Git worktree materialization stalled for {idle_timeout:g}s '
                    f'(files {completed}/{total or "unknown"}): {detail[-1000:]}')
            try:
                returncode = process.wait(timeout=min(0.2, idle_timeout - idle))
                break
            except subprocess.TimeoutExpired:
                continue
    finally:
        primary_error = sys.exc_info()[1]
        retirement_error = None
        try:
            # Retire helpers too: subprocess.run(timeout=...) kills only the leader.
            if os.name == 'posix':
                from lib.project_mod.process_group import retire_process_group
                retire_process_group(process.pid,
                    wait_leader=lambda seconds: process.wait(timeout=seconds))
            else:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
                if primary_error is not None:
                    raise WorktreeRetirementUnproven('Interrupted non-POSIX descendants are unproven')
        except Exception as error:
            logger.debug('run_worktree_add failure handled: %s', type(error).__name__)
            retirement_error = error
        finally:
            if reader.ident is not None:
                reader.join(timeout=0.2)
                stop_reader.set()
                reader.join(timeout=0.5)
            process.stdout.close()
        if retirement_error is not None or (reader.ident is not None and not eof.is_set()):
            # An escaped pipe holder is not in our owned group. Do not hang or
            # destroy its destination based on the leader's exit alone.
            raise WorktreeRetirementUnproven(
                f'{primary_error or "Worktree exited"}; writer retirement unproven: '
                f'{retirement_error or "output pipe remains held"}') from primary_error or retirement_error
    if reader_error:
        raise RuntimeError('Git worktree output reader failed') from reader_error[0]
    detail = bytes(tail).decode('utf-8', errors='replace')
    return subprocess.CompletedProcess(argv, returncode, stdout='', stderr=detail)

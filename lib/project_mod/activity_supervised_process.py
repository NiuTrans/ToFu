"""Run one Git command whose only deadline is a no-progress window.

Entry: run_activity_supervised. Whole-tree Git snapshot commands (``add -A``,
``diff``, ``ls-files``, ``read-tree``, ``write-tree``) print no progress, and
their duration follows the tree size and the volume's latency. A wall-clock
deadline therefore refuses healthy work on a slow volume. Instead the owner
declares a fixed stall window, renewed whenever the kernel shows that some
thread of the child's process tree executed (CPU time, page faults or context
switches changed). A tree blocked on an unresponsive volume shows no change
and is retired through ``process_group``; owner cancellation is observed while
waiting. Like ``subprocess.run`` it returns complete text stdout/stderr. A
process that keeps executing without progress (a busy loop) is not detected;
cancellation still retires it. Dependencies: procfs on Linux, psutil elsewhere.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from pathlib import Path

from lib.log import get_logger

logger = get_logger(__name__)

_PROCFS = sys.platform.startswith('linux')
_POLL_SECONDS = 0.1
_SAMPLE_SECONDS = 1.0
_DRAIN_SECONDS = 2.0


class ProcessStalled(RuntimeError):
    """No thread of the process tree executed for the whole stall window."""


class ProcessCancelled(RuntimeError):
    """The owner cancelled; the process tree was retired."""


class ProcessRetirementUnproven(RuntimeError):
    """A process-tree member or output-pipe holder may still be alive."""


def _procfs_activity(pid: int) -> tuple:
    from lib.project_mod.process_group import descendant_pids
    sample = []
    for member in (pid, *descendant_pids(pid)):
        try:
            with os.scandir(f'/proc/{member}/task') as entries:
                threads = sorted(entry.name for entry in entries if entry.name.isdecimal())
        except (FileNotFoundError, ProcessLookupError):
            continue  # The member exited between discovery and observation.
        for thread in threads:
            task = Path(f'/proc/{member}/task/{thread}')
            try:
                # minflt, cminflt, majflt, cmajflt, utime, stime
                counters = (task / 'stat').read_bytes().rsplit(b')', 1)[-1].split()[7:13]
                switches = [line for line in (task / 'status').read_bytes().splitlines()
                            if b'ctxt_switches' in line]
            except (FileNotFoundError, ProcessLookupError):
                continue  # The thread exited between discovery and observation.
            sample.append((member, thread, *counters, *switches))
    return tuple(sample)


def _portable_activity(pid: int) -> tuple:
    import psutil
    try:
        leader = psutil.Process(pid)
        members = [leader, *leader.children(recursive=True)]
    except psutil.Error:
        return ()
    sample = []
    for member in members:
        try:
            with member.oneshot():
                times = member.cpu_times()
                sample.append((member.pid, times.user, times.system,
                               *member.num_ctx_switches()))
        except psutil.Error:
            continue  # The member exited between discovery and observation.
    return tuple(sample)


def process_tree_activity(pid: int) -> tuple:
    """Comparable execution evidence for ``pid`` and its descendants."""
    return _procfs_activity(pid) if _PROCFS else _portable_activity(pid)


def _await_exit(process: subprocess.Popen, stall_seconds: float, cancellation):
    """Return output, or None when the leader exited but its pipes stay open."""
    observed = process_tree_activity(process.pid)
    renewed = sampled = time.monotonic()
    while True:
        try:
            return process.communicate(timeout=_POLL_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        if process.poll() is not None:
            return None
        if cancellation is not None and cancellation.is_set():
            raise ProcessCancelled('Cancelled by its owner')
        now = time.monotonic()
        if now - sampled < _SAMPLE_SECONDS:
            continue
        sample, sampled = process_tree_activity(process.pid), now
        if sample != observed:
            observed, renewed = sample, now
        elif now - renewed >= stall_seconds:
            raise ProcessStalled(
                f'No thread of the process tree executed for {stall_seconds:g}s')


def _retire(process: subprocess.Popen, output, primary: BaseException | None):
    """Retire the owned group, then drain within a bound; never hang teardown."""
    unproven = None
    try:
        if os.name == 'posix':
            from lib.project_mod.process_group import retire_process_group
            retire_process_group(process.pid,
                wait_leader=lambda seconds: process.wait(timeout=seconds))
        elif process.poll() is None:
            process.kill()
            process.wait(timeout=_DRAIN_SECONDS)
    except Exception as error:
        logger.debug('activity-supervised retirement failed: %s', type(error).__name__)
        unproven = error
    if output is None:
        try:
            output = process.communicate(timeout=_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            for stream in (process.stdout, process.stderr):
                stream.close()
            unproven = unproven or 'output pipe remains held'
            output = ('', '')
    if unproven is not None:
        raise ProcessRetirementUnproven(
            f'{primary or "Process exited"}; retirement unproven: {unproven}') from primary
    return output


def run_activity_supervised(argv: list[str], *, cwd, env: dict[str, str],
                            stall_seconds: float, pass_fds: tuple[int, ...] = (),
                            cancellation=None) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` with no total deadline; only a stall or Stop ends it early."""
    if not math.isfinite(stall_seconds) or stall_seconds <= 0:
        raise ValueError('Stall window must be positive and finite')
    process = subprocess.Popen(
        argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=(os.name == 'posix'), pass_fds=pass_fds,
    )
    output = primary = None
    try:
        output = _await_exit(process, stall_seconds, cancellation)
    except BaseException as error:
        primary = error
        raise
    finally:
        output = _retire(process, output, primary)
    stdout, stderr = output
    return subprocess.CompletedProcess(argv, process.returncode, stdout=stdout, stderr=stderr)

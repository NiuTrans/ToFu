"""Run one Git command whose only deadline is a no-progress window.

Entry: run_activity_supervised. Whole-tree Git snapshot commands (``add -A``,
``diff``, ``ls-files``, ``read-tree``, ``write-tree``) print no progress, and
their duration follows the tree size and the volume's latency. A wall-clock
deadline therefore refuses healthy work on a slow volume. Instead the owner
declares a fixed stall window, renewed whenever the kernel shows that some
thread of the child's process tree executed (CPU time, page faults or context
switches changed). A tree blocked on an unresponsive volume shows no change
and is retired through ``process_group``; owner cancellation is observed while
waiting. Each renewal can be reported to the owner (``on_activity``), whose own
liveness may be judged by silence while it waits (a delegating Coordinator).
Like ``subprocess.run`` it returns complete text stdout/stderr. A
process that keeps executing without progress (a busy loop) is not detected;
cancellation still retires it. ``ActivitySupervisedExchange`` applies the same
window to a long-lived child that answers one request at a time (``git
check-ignore --stdin``): the window runs only while an answer is awaited.
Dependencies: procfs on Linux, psutil elsewhere.
"""
from __future__ import annotations

import math
import os
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lib.log import get_logger

logger = get_logger(__name__)

_PROCFS = sys.platform.startswith('linux')
_POLL_SECONDS = 0.1
# How often activity evidence is read; worktree materialization shares it.
ACTIVITY_SAMPLE_SECONDS = 1.0
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


class _StallWindow:
    """Renewed whenever the tree is observed executing; raises once it lapses or on Stop."""

    def __init__(self, pid: int, stall_seconds: float, cancellation, on_activity):
        self._pid, self._stall_seconds = pid, stall_seconds
        self._cancellation, self._on_activity = cancellation, on_activity
        self._observed = process_tree_activity(pid)
        self._renewed = self._sampled = time.monotonic()

    def check(self) -> None:
        if self._cancellation is not None and self._cancellation.is_set():
            raise ProcessCancelled('Cancelled by its owner')
        now = time.monotonic()
        if now - self._sampled < ACTIVITY_SAMPLE_SECONDS:
            return
        sample, self._sampled = process_tree_activity(self._pid), now
        if sample != self._observed:
            self._observed, self._renewed = sample, now
            if self._on_activity is not None:
                self._on_activity()
        elif now - self._renewed >= self._stall_seconds:
            raise ProcessStalled(
                f'No thread of the process tree executed for {self._stall_seconds:g}s')


def _await_exit(process: subprocess.Popen, stall_seconds: float, cancellation, on_activity):
    """Return output, or None when the leader exited but its pipes stay open."""
    window = _StallWindow(process.pid, stall_seconds, cancellation, on_activity)
    while True:
        try:
            return process.communicate(timeout=_POLL_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        if process.poll() is not None:
            return None
        window.check()


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
                            cancellation=None, binary: bool = False,
                            on_activity=None, input: bytes | None = None,
                            ) -> subprocess.CompletedProcess:
    """Run ``argv`` with no total deadline; only a stall or Stop ends it early.

    ``binary`` captures raw bytes so binary patch/diff payloads are never
    rewritten by text-mode universal-newline decoding. ``on_activity`` runs on
    this waiting thread whenever the tree is observed executing (each renewal).
    ``input`` is the child's whole stdin, read from an anonymous temporary file
    so a child that reads slowly never blocks this waiting thread.
    """
    if not math.isfinite(stall_seconds) or stall_seconds <= 0:
        raise ValueError('Stall window must be positive and finite')
    stdin = subprocess.DEVNULL
    if input is not None:
        stdin = tempfile.TemporaryFile()
        stdin.write(input)
        stdin.seek(0)
    try:
        process = subprocess.Popen(
            argv, cwd=str(cwd), env=env, stdin=stdin,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=not binary,
            start_new_session=(os.name == 'posix'), pass_fds=pass_fds,
        )
    finally:
        if input is not None:
            stdin.close()
    output = primary = None
    try:
        output = _await_exit(process, stall_seconds, cancellation, on_activity)
    except BaseException as error:
        primary = error
        raise
    finally:
        output = _retire(process, output, primary)
    stdout, stderr = output
    return subprocess.CompletedProcess(argv, process.returncode, stdout=stdout, stderr=stderr)


class ActivitySupervisedExchange:
    """One long-lived child answering one request at a time on its stdout.

    ``exchange`` writes a request and returns its answer: the next ``fields``
    NUL-terminated fields. The stall window runs only while an answer is
    awaited, so a child idle between requests is never stalled; Stop, a stall
    or ``close`` retire its process group. Use as a context manager.
    """

    def __init__(self, argv: list[str], *, cwd, env: dict[str, str], stall_seconds: float,
                 cancellation=None, on_activity=None):
        if not math.isfinite(stall_seconds) or stall_seconds <= 0:
            raise ValueError('Stall window must be positive and finite')
        self._stall_seconds, self._cancellation = stall_seconds, cancellation
        self._on_activity = on_activity
        # A file, not a pipe: unread diagnostics can never block the child.
        self._stderr = tempfile.TemporaryFile()
        try:
            self._process = subprocess.Popen(
                argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self._stderr, start_new_session=(os.name == 'posix'))
        except BaseException:
            self._stderr.close()
            raise
        self._pending = b''

    def __enter__(self) -> 'ActivitySupervisedExchange':
        return self

    def __exit__(self, kind, error, traceback) -> None:
        self.close(error)

    def _exited(self) -> RuntimeError:
        self._process.wait()
        self._stderr.seek(0)
        detail = os.fsdecode(self._stderr.read()).strip()[:600]
        return RuntimeError(f'exited with status {self._process.returncode} before answering'
                            + (f': {detail}' if detail else ''))

    def exchange(self, request: bytes, fields: int) -> list[bytes]:
        try:
            self._process.stdin.write(request)
            self._process.stdin.flush()
        except BrokenPipeError:
            raise self._exited() from None
        window = _StallWindow(self._process.pid, self._stall_seconds,
                              self._cancellation, self._on_activity)
        stream = self._process.stdout.fileno()
        while self._pending.count(b'\0') < fields:
            readable, _, _ = select.select([stream], [], [], _POLL_SECONDS)
            if not readable:
                window.check()
                continue
            chunk = os.read(stream, 1 << 16)
            if not chunk:
                raise self._exited()
            self._pending += chunk
        *answer, self._pending = self._pending.split(b'\0', fields)
        return answer

    def close(self, primary: BaseException | None = None) -> None:
        """End the child: EOF lets it exit; its group is retired either way."""
        # Detached first: retirement drains through communicate(), which must
        # not flush a closed stdin.
        stdin, self._process.stdin = self._process.stdin, None
        try:
            stdin.close()
        except BrokenPipeError:
            pass
        try:
            _retire(self._process, None, primary)
        finally:
            self._stderr.close()

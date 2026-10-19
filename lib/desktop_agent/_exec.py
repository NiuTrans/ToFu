"""
Desktop Agent — local command execution handler.

Contains ``_SHELL_META_RE`` and ``cmd_run_local`` (run a shell command on
the local machine, avoiding ``shell=True`` when possible). Both synchronous and
streamed execution use the existing independent command supervisor; it owns
descendant retirement after caller death. The caller owns live stream projection
and exact explicit timeouts.
"""

import os
import math
import tempfile
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)


# Shell metacharacters that require shell=True for correct behaviour
_SHELL_META_RE = re.compile(r'[|&;<>()$`\\"\'\ \n*?\[#~]')


def _build_args(command):
    """Build the argv for *command* without shell=True.

    Security: avoids shell=True when the command is a simple executable
    invocation (no pipes, redirects, globs, etc.).  When shell features
    *are* needed, the command is passed as a single argument to an
    explicit ``['/bin/sh', '-c', ...]`` invocation so that the
    argument vector is never ambiguously parsed.
    """
    needs_shell = bool(_SHELL_META_RE.search(command))
    if needs_shell:
        # Use explicit shell invocation instead of shell=True so
        # that *command* is a single, unambiguous argument to sh.
        from lib.compat import get_shell_args
        return get_shell_args(command)
    # Simple command — split into argv list, no shell involved.
    # On Windows, use posix=False so that backslash paths and
    # double-quote quoting are handled correctly.
    from lib.compat import IS_WINDOWS
    return shlex.split(command, posix=not IS_WINDOWS)


def command_timeout(value):
    """None is completion-owned; explicit finite nonnegative seconds are exact."""
    if value is None:
        return None
    try:
        timeout = float(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError('Command timeout must be nonnegative and finite or None') from error
    if not math.isfinite(timeout) or timeout < 0 or isinstance(value, bool):
        raise ValueError('Command timeout must be nonnegative and finite or None')
    return timeout


def start_local_run(cmd_id, params, on_chunk, on_exit, *, cancel=None):
    """Stream an exec-authorized desktop command with its owner's cancellation."""
    command = params.get('command', '')
    if not isinstance(command, str) or not command.strip():
        return 'Empty or invalid command'
    try:
        timeout = command_timeout(params.get('timeout'))
    except ValueError as error:
        return str(error)
    cwd = params.get('cwd')
    start_streamed_command(command, os.path.expanduser(cwd) if cwd else None,
                           timeout, on_chunk, on_exit, cancel=cancel)
    return None


def cmd_run_local(params):
    """Run a shell command on the local machine."""
    command = params.get('command', '')
    if not isinstance(command, str) or not command.strip():
        return {'error': 'Empty or invalid command'}
    cwd = params.get('cwd')
    try:
        timeout = command_timeout(params.get('timeout'))
    except ValueError as error:
        return {'error': str(error)}

    resolved_cwd = os.path.expanduser(cwd) if cwd else None

    settled = threading.Event()
    outcomes = []
    start_streamed_command(command, resolved_cwd, timeout, lambda *_: None,
                           lambda outcome: (outcomes.append(outcome), settled.set()))
    settled.wait()
    outcome = outcomes[0]
    if outcome.get('timed_out'):
        outcome['error'] = f'Command timed out after {timeout}s'
    return outcome


# ══════════════════════════════════════════════════════════
#  Streamed execution (docs/modules/remote_execution.md)
# ══════════════════════════════════════════════════════════

# Per-stream capture cap; mirrors cmd_run_local's 100k stdout ceiling.
_MAX_STREAM_CHARS = 100_000


def _kill_tree(proc):
    """Kill *proc* and its whole child tree. Returns True if children died.

    psutil is a soft dependency of the agent (same pattern as _gui.py);
    without it we can only kill the direct process. Windows note: strict
    tree semantics would use a Job Object — psutil.children(recursive=True)
    covers the practical tree on all platforms.
    """
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is None:
        try:
            proc.kill()
        except Exception as e:
            logger.debug('[Exec] proc.kill fallback failed: %s', e)
        return False
    killed = False
    try:
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.kill()
                killed = True
            except psutil.NoSuchProcess as _e:
                logger.debug('kill tree: process gone (%s)', _e)
                pass
        try:
            parent.kill()
        except psutil.NoSuchProcess as _e:
            logger.debug('kill tree: process gone (%s)', _e)
            pass
        # Popen alone reaps the direct child and preserves its exit status.
        _gone, alive = psutil.wait_procs(children, timeout=3)
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess as _e:
                logger.debug('kill tree: process gone (%s)', _e)
                pass
    except Exception as e:
        logger.warning('[Exec] tree kill failed for pid=%s: %s', proc.pid, e)
        try:
            proc.kill()
        except Exception as _e:
            logger.debug('kill tree: failed (%s)', _e)
            pass
    return killed


class _StreamedProcess:
    """Run a command with live output chunks + process-tree kill on timeout.

    Readers tail private stdout/stderr files from the independent supervisor,
    so chunks flow before command completion and work in frozen GUI builds.
    Everything runs on daemon threads; ``start`` never blocks.
    """

    def __init__(self, command, cwd, timeout, on_chunk, on_exit, *, cancel=None):
        self._command = command
        self._cwd = cwd
        self._timeout = command_timeout(timeout)
        self._on_chunk = on_chunk
        self._on_exit = on_exit
        self._cancel = cancel
        self._acc = {'stdout': [], 'stderr': []}
        self._total = {'stdout': 0, 'stderr': 0}
        self._truncated = False
        self._lock = threading.Lock()
        self._readers = []
        self._proc = None
        self._temporary = None
        self._job = None
        self._handles = []
        self._output_wait = threading.Event()

    def _close_execution(self):
        failures = []
        for handle in self._handles:
            try:
                handle.close()
            except OSError as error:
                failures.append(str(error))
        if self._job is not None:
            try:
                self._job.close()
            except OSError as error:
                failures.append(str(error))
            self._job = None
        if self._temporary is not None:
            try:
                self._temporary.cleanup()
            except OSError as error:
                failures.append(str(error))
            self._temporary = None
        return '; '.join(failures)

    def start(self):
        try:
            from lib.paper.builds.process_worker import desktop_worker_command
            import psutil
            args = _build_args(self._command)
            self._temporary = tempfile.TemporaryDirectory(prefix='tofu-desktop-command-')
            directory = Path(self._temporary.name)
            self._ready = directory / 'ready'
            self._status = directory / 'status.json'
            self._stop = directory / 'stop'
            output_paths = [directory / name for name in ('stdout', 'stderr')]
            for path in output_paths:
                path.touch()
            worker = desktop_worker_command(args, cwd=self._cwd or Path.cwd(),
                ready=self._ready, stdout=output_paths[0], stderr=output_paths[1],
                status=self._status, parent_pid=os.getpid(),
                parent_birth=psutil.Process().create_time())
            self._proc = subprocess.Popen(worker, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=Path(__file__).resolve().parents[2], start_new_session=os.name != 'nt')
            if os.name == 'nt':
                from lib.paper.builds.windows_job import WindowsJob
                self._job = WindowsJob(self._proc)
            # Admission follows OS lifetime binding; the worker cannot start early.
            self._ready.write_bytes(b'1')
            for stream, path in zip(('stdout', 'stderr'), output_paths):
                handle = path.open('rb', buffering=0)
                self._handles.append(handle)
                thread = threading.Thread(target=self._reader, args=(stream, handle), daemon=True)
                thread.start()
                self._readers.append(thread)
            threading.Thread(target=self._waiter, daemon=True).start()
        except BaseException as error:
            if self._proc is not None:
                _kill_tree(self._proc)
                self._proc.wait()
            for thread in self._readers:
                thread.join()
            self._close_execution()
            if not isinstance(error, Exception):
                raise
            self._on_exit({'error': str(error)})

    def _reader(self, stream, fh):
        fd = fh.fileno()
        try:
            while True:
                try:
                    data = os.read(fd, 4096)
                except OSError as _e:
                    logger.debug('reader: unreadable (%s)', _e)
                    break
                if not data:
                    if self._proc.poll() is not None:
                        break
                    self._output_wait.wait(.01)
                    continue
                text = data.decode('utf-8', errors='replace')
                with self._lock:
                    room = _MAX_STREAM_CHARS - self._total[stream]
                    if room > 0:
                        keep = text[:room]
                        self._total[stream] += len(keep)
                        self._acc[stream].append(keep)
                    else:
                        self._truncated = True
                        keep = ''
                if keep:
                    try:
                        self._on_chunk(stream, keep)
                    except Exception as e:
                        logger.debug('[Exec] on_chunk callback failed: %s', e)
        finally:
            try:
                fh.close()
            except Exception as _e:
                logger.debug('reader: failed (%s)', _e)
                pass

    def _waiter(self):
        timed_out = cancelled = killed_tree = False
        deadline = (None if self._timeout is None
                    else time.monotonic() + self._timeout)
        while self._proc.poll() is None:
            cancelled = self._cancel is not None and self._cancel.is_set()
            timed_out = deadline is not None and time.monotonic() >= deadline
            if cancelled or timed_out:
                self._stop.write_bytes(b'1')
                self._proc.wait()
                killed_tree = True
                break
            try:
                self._proc.wait(timeout=(.2 if deadline is None else
                    min(.2, max(.001, deadline - time.monotonic()))))
            except subprocess.TimeoutExpired:
                pass
        for thread in self._readers:
            thread.join()
        status = {}
        try:
            from lib.paper.builds.process_worker import load_desktop_status
            status = load_desktop_status(self._status)
        except (OSError, ValueError):
            status = {'error': 'Desktop command supervisor retired without a final status'}
        with self._lock:
            outcome = {
                'stdout': ''.join(self._acc['stdout']),
                'stderr': ''.join(self._acc['stderr']),
                'exit_code': status.get('returncode', self._proc.returncode),
                'timed_out': timed_out,
                'cancelled': cancelled,
                'killed_tree': killed_tree,
                'truncated': self._truncated,
            }
        if status.get('error'):
            outcome['error'] = status['error']
        cleanup_error = self._close_execution()
        if cleanup_error:
            outcome['error'] = 'Desktop command cleanup failed: ' + cleanup_error
        self._on_exit(outcome)


def start_streamed_command(command, cwd, timeout, on_chunk, on_exit, *, cancel=None):
    """Start *command* in the background, streaming output chunks.

    ``on_chunk(stream, data)`` fires per pipe-read chunk ('stdout'/'stderr');
    ``on_exit(outcome)`` fires exactly once with the final capped result
    (``exit_code`` / ``timed_out`` / ``cancelled`` / ``killed_tree`` / ``truncated``) or
    ``{'error': ...}`` when the process could not be started.
    """
    try:
        process = _StreamedProcess(command, cwd, timeout, on_chunk, on_exit, cancel=cancel)
    except ValueError as error:
        on_exit({'error': str(error)})
        return
    process.start()

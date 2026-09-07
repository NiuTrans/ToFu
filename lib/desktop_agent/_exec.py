"""
Desktop Agent — local command execution handler.

Contains ``_SHELL_META_RE`` and ``cmd_run_local`` (run a shell command on
the local machine, avoiding ``shell=True`` when possible).
"""

import os
import re
import shlex
import subprocess
import threading

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


def cmd_run_local(params):
    """Run a shell command on the local machine."""
    command = params.get('command', '')
    if not isinstance(command, str) or not command.strip():
        return {'error': 'Empty or invalid command'}
    cwd = params.get('cwd')
    timeout = params.get('timeout', 30)

    resolved_cwd = os.path.expanduser(cwd) if cwd else None

    try:
        args = _build_args(command)
        result = subprocess.run(
            args, shell=False,
            capture_output=True, text=True,
            timeout=timeout,
            cwd=resolved_cwd,
        )
        return {
            'stdout': result.stdout[:100_000],
            'stderr': result.stderr[:20_000],
            'exit_code': result.returncode,
        }
    except subprocess.TimeoutExpired:
        logger.warning('cmd_run_local timed out: cmd=%s timeout=%ds', command[:120], timeout, exc_info=True)
        return {'error': f'Command timed out after {timeout}s'}
    except Exception as e:
        logger.warning('cmd_run_local failed for cmd=%s: %s', command[:120], e, exc_info=True)
        return {'error': str(e)}


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
        _gone, alive = psutil.wait_procs(children + [parent], timeout=3)
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

    Readers use ``os.read`` on the raw pipe fds (returns whatever is
    available, so chunks flow BEFORE the pipe fills or the process exits).
    Everything runs on daemon threads; ``start`` never blocks.
    """

    def __init__(self, command, cwd, timeout, on_chunk, on_exit):
        self._command = command
        self._cwd = cwd
        self._timeout = timeout
        self._on_chunk = on_chunk
        self._on_exit = on_exit
        self._acc = {'stdout': [], 'stderr': []}
        self._total = {'stdout': 0, 'stderr': 0}
        self._truncated = False
        self._lock = threading.Lock()
        self._readers = []
        self._proc = None

    def start(self):
        try:
            args = _build_args(self._command)
            self._proc = subprocess.Popen(
                args, shell=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=self._cwd,
            )
        except Exception as e:
            logger.warning('[Exec] streamed start failed for cmd=%s: %s',
                           self._command[:120], e)
            self._on_exit({'error': str(e)})
            return
        for stream, fh in (('stdout', self._proc.stdout),
                           ('stderr', self._proc.stderr)):
            t = threading.Thread(target=self._reader, args=(stream, fh),
                                 daemon=True)
            t.start()
            self._readers.append(t)
        threading.Thread(target=self._waiter, daemon=True).start()

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
                    break
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
        timed_out = False
        killed_tree = False
        try:
            self._proc.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            killed_tree = _kill_tree(self._proc)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning('[Exec] process %s still alive after tree kill',
                               self._proc.pid)
        for t in self._readers:
            t.join(timeout=5)
        with self._lock:
            outcome = {
                'stdout': ''.join(self._acc['stdout']),
                'stderr': ''.join(self._acc['stderr']),
                'exit_code': (self._proc.returncode
                              if self._proc.returncode is not None else -9),
                'timed_out': timed_out,
                'killed_tree': killed_tree,
                'truncated': self._truncated,
            }
        self._on_exit(outcome)


def start_streamed_command(command, cwd, timeout, on_chunk, on_exit):
    """Start *command* in the background, streaming output chunks.

    ``on_chunk(stream, data)`` fires per pipe-read chunk ('stdout'/'stderr');
    ``on_exit(outcome)`` fires exactly once with the final capped result
    (``exit_code`` / ``timed_out`` / ``killed_tree`` / ``truncated``) or
    ``{'error': ...}`` when the process could not be started.
    """
    _StreamedProcess(command, cwd, timeout, on_chunk, on_exit).start()

"""lib/self_update/_git.py — git executable resolution and raw runners.

``_git_exe`` (cached PATH/Windows probe), ``_run_git`` (subprocess in the
project root), ``git_available`` (is this a checkout?) and ``_head_sha``.
"""

from __future__ import annotations

import os
import subprocess
import threading
from typing import Optional

from lib.self_update._config import _ROOT

from lib.log import get_logger

logger = get_logger(__name__)

_git_exe_cache: Optional[str] = None


def _git_exe() -> str:
    """Resolve the git executable, falling back to common install dirs.

    On Windows the server is often launched from a context that lacks
    Git's bin/ on PATH (e.g. a double-clicked launcher), so a bare
    ``'git'`` would raise FileNotFoundError and make the UI wrongly
    report "not a git checkout". We therefore probe PATH via
    ``shutil.which`` first, then the standard Windows install locations.
    Result is cached for the process lifetime. Returns ``'git'`` as a
    last resort so the OS still gets a chance to resolve it.
    """
    global _git_exe_cache
    if _git_exe_cache:
        return _git_exe_cache

    import shutil
    found = shutil.which('git')
    if found:
        _git_exe_cache = found
        return found

    candidates = []
    if os.name == 'nt':
        for base in (os.environ.get('ProgramFiles', r'C:\Program Files'),
                     os.environ.get('ProgramFiles(x86)',
                                    r'C:\Program Files (x86)'),
                     os.environ.get('LocalAppData', '')):
            if base:
                candidates.append(os.path.join(base, 'Git', 'cmd', 'git.exe'))
                candidates.append(os.path.join(base, 'Git', 'bin', 'git.exe'))
    for c in candidates:
        if c and os.path.isfile(c):
            _git_exe_cache = c
            logger.info('[Update] Resolved git via fallback: %s', c)
            return c

    _git_exe_cache = 'git'
    return 'git'


def _run_git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a git command in the project root, capturing output.

    Raises FileNotFoundError if git is not installed (caller handles it).
    """
    return subprocess.run(
        [_git_exe(), *args],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


import re as _re

# git --progress writes phase lines to stderr, updating in place with '\r':
#   "Receiving objects:  73% (1234/1690), 5.23 MiB | 1.20 MiB/s"
#   "Resolving deltas:  40% (100/250)"
# We surface the phase name + percent so the UI can show a determinate bar
# instead of an opaque spinner during a long fetch on a slow network.
_GIT_PROGRESS_RE = _re.compile(r'^(?P<phase>[A-Za-z][A-Za-z ]+?):\s+(?P<pct>\d+)%')


def _run_git_streaming(args: list[str], timeout: int = 30,
                       on_progress=None) -> subprocess.CompletedProcess:
    """Run a git command with ``--progress``, forwarding live progress.

    Git emits progress on stderr, redrawing the current line with ``\\r``.
    We read stderr incrementally, parse ``<phase>: <pct>%`` frames, and
    invoke ``on_progress(phase, pct, detail)`` so the caller can render a
    determinate bar. stdout is captured whole (git prints the pull summary
    there). Returns a ``CompletedProcess`` mirroring ``_run_git`` so callers
    are interchangeable. Never lets a progress-callback exception surface.

    Args:
        args: git arguments (``--progress`` is injected if absent).
        timeout: overall wall-clock ceiling in seconds.
        on_progress: optional ``fn(phase: str, pct: int, detail: str)``.
    """
    argv = [_git_exe(), *args]
    if '--progress' not in argv:
        argv.append('--progress')
    proc = subprocess.Popen(
        argv, cwd=_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )

    stderr_lines: list[str] = []

    def _pump_stderr():
        buf = ''
        stream = proc.stderr
        if stream is None:
            return
        while True:
            ch = stream.read(1)
            if not ch:
                break
            if ch in ('\r', '\n'):
                line = buf.strip()
                buf = ''
                if not line:
                    continue
                stderr_lines.append(line)
                if on_progress:
                    m = _GIT_PROGRESS_RE.match(line)
                    if m:
                        try:
                            on_progress(m.group('phase').strip(),
                                        int(m.group('pct')), line)
                        except Exception as e:
                            logger.debug('[Update] git progress cb failed: %s', e)
            else:
                buf += ch
        if buf.strip():
            stderr_lines.append(buf.strip())

    t = threading.Thread(target=_pump_stderr, daemon=True)
    t.start()
    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    t.join(timeout=2)
    return subprocess.CompletedProcess(
        argv, proc.returncode, stdout=stdout or '',
        stderr='\n'.join(stderr_lines))


def git_available() -> bool:
    """True if this is a git checkout and git is on PATH."""
    if not os.path.isdir(os.path.join(_ROOT, '.git')):
        return False
    try:
        cp = _run_git(['rev-parse', '--git-dir'])
        return cp.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] git not available: %s', e)
        return False



def _head_sha() -> Optional[str]:
    """Current HEAD commit SHA, or None if it can't be read."""
    try:
        cp = _run_git(['rev-parse', 'HEAD'])
        if cp.returncode == 0:
            return cp.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] rev-parse HEAD failed: %s', e)
    return None


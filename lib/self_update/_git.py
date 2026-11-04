"""lib/self_update/_git.py — git executable resolution and raw runners.

``_git_exe`` (cached PATH/Windows probe), ``_run_git`` (subprocess in the
project root), ``git_available`` (is this a checkout?), ``_head_sha`` and
``observe_repository`` (both answers from one git process), and
``resolve_update_upstream`` / ``_remote_url`` (where updates come from).
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Optional

from lib.self_update._config import (
    _FALLBACK_BRANCH,
    _FALLBACK_REMOTE,
    _ROOT,
    update_source_overrides,
)

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
        candidates = [found]
    else:
        candidates = []

    # A PATH entry can exist yet fail at load time (for example GLIBC ABI).
    # Preserve PATH ordering, but select only an executable that actually runs.
    for directory in os.get_exec_path():
        candidate = shutil.which('git', path=directory)
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    if os.name == 'nt':
        for base in (os.environ.get('ProgramFiles', r'C:\Program Files'),
                     os.environ.get('ProgramFiles(x86)',
                                    r'C:\Program Files (x86)'),
                     os.environ.get('LocalAppData', '')):
            if base:
                candidates.append(os.path.join(base, 'Git', 'cmd', 'git.exe'))
                candidates.append(os.path.join(base, 'Git', 'bin', 'git.exe'))
    for candidate in dict.fromkeys(candidates):
        try:
            probe = subprocess.run(
                [candidate, '--version'], capture_output=True, text=True,
                timeout=3,
            )
            if probe.returncode == 0 and probe.stdout.startswith('git version '):
                _git_exe_cache = candidate
                return candidate
            logger.warning('[Update] Git candidate unusable: %s (exit %s)',
                           candidate, probe.returncode)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning('[Update] Git candidate unusable: %s (%s)',
                           candidate, exc)

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


@dataclass(frozen=True)
class RepositoryObservation:
    """``git rev-parse --git-dir`` and ``git rev-parse HEAD`` in one process.

    ``discovered`` is what ``rev-parse --git-dir`` reports (repository
    discovery succeeded); ``head`` is what ``_head_sha`` reports (the full
    HEAD commit, None when unborn or unresolvable).
    """

    discovered: bool
    head: Optional[str]


def observe_repository() -> RepositoryObservation:
    """Observe the project checkout and its HEAD with a single git process.

    ``rev-parse`` sets up the repository before parsing arguments, so it
    prints the git directory whenever discovery succeeds and exits 128 with
    no output when it fails. ``HEAD`` is then resolved exactly as
    ``git rev-parse HEAD`` would (exit 0 prints the commit; an unborn or
    ambiguous HEAD exits non-zero after the git directory line).
    """
    try:
        cp = _run_git(['rev-parse', '--git-dir', 'HEAD'])
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] git not available: %s', e)
        return RepositoryObservation(discovered=False, head=None)
    lines = cp.stdout.splitlines()
    head = lines[1].strip() if cp.returncode == 0 and len(lines) > 1 else None
    return RepositoryObservation(discovered=bool(lines), head=head or None)


def git_available(observation: Optional[RepositoryObservation] = None) -> bool:
    """True if this is a git checkout and git is on PATH.

    A caller that already holds this request's ``observe_repository`` result
    passes it instead of spawning another ``rev-parse``.
    """
    if not os.path.isdir(os.path.join(_ROOT, '.git')):
        return False
    if observation is not None:
        return observation.discovered
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


@dataclass(frozen=True)
class UpdateUpstream:
    """The remote and branch an update is checked against and pulled from."""

    remote: str
    branch: str


def _current_branch_upstream() -> Optional[UpdateUpstream]:
    """``branch.<current>.remote`` / ``branch.<current>.merge``, or None.

    None when HEAD is detached, the branch tracks nothing, or git fails.
    """
    try:
        head = _run_git(['symbolic-ref', '--quiet', 'HEAD'])
        ref = head.stdout.strip()
        if head.returncode != 0 or not ref.startswith('refs/heads/'):
            return None
        branch = ref.removeprefix('refs/heads/')
        remote = _run_git(['config', '--get', f'branch.{branch}.remote'])
        merge = _run_git(['config', '--get', f'branch.{branch}.merge'])
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] upstream lookup failed: %s', e)
        return None
    remote_name = remote.stdout.strip() if remote.returncode == 0 else ''
    merge_ref = merge.stdout.strip() if merge.returncode == 0 else ''
    if not remote_name or not merge_ref:
        return None
    return UpdateUpstream(remote=remote_name,
                          branch=merge_ref.removeprefix('refs/heads/'))


def resolve_update_upstream() -> UpdateUpstream:
    """The remote/branch both the update check and the apply follow.

    ``TOFU_UPDATE_REMOTE`` / ``TOFU_UPDATE_BRANCH`` win when set. Otherwise
    the current branch's upstream decides, so a clone follows the channel it
    was cloned from (an internal mirror's ``master`` as much as GitHub's
    ``main``). ``origin``/``main`` stands in only when HEAD is detached or
    the branch tracks nothing.
    """
    overrides = update_source_overrides()
    if overrides.remote and overrides.branch:
        return UpdateUpstream(remote=overrides.remote, branch=overrides.branch)
    tracked = _current_branch_upstream() or UpdateUpstream(
        remote=_FALLBACK_REMOTE, branch=_FALLBACK_BRANCH)
    return UpdateUpstream(remote=overrides.remote or tracked.remote,
                          branch=overrides.branch or tracked.branch)


def _remote_url(remote: str) -> Optional[str]:
    """URL git fetches ``remote`` from, or None when git cannot say.

    ``ls-remote --get-url`` applies ``url.<base>.insteadOf`` rewrites, echoes
    a remote given as a URL unchanged, and never contacts the remote.
    """
    try:
        cp = _run_git(['ls-remote', '--get-url', remote])
    except (FileNotFoundError, subprocess.SubprocessError) as e:
        logger.warning('[Update] remote URL lookup failed: %s', e)
        return None
    if cp.returncode != 0:
        return None
    return cp.stdout.strip() or None


"""Resolve a runnable Git for internal repository operations.

Entry point: git_executable. Probe PATH candidates before any repository command;
never retry a failed write or change the process environment. Cache successful
selection by PATH and loader environment; failed probes remain retryable.
"""
from __future__ import annotations

from functools import lru_cache
import os
import shutil
import subprocess


def git_executable() -> str:
    return _resolve(os.environ.get('PATH', os.defpath),
                    os.environ.get('LD_LIBRARY_PATH', ''),
                    os.environ.get('LD_PRELOAD', ''))


@lru_cache(maxsize=16)
def _resolve(path: str, library_path: str, preload: str) -> str:
    failures = []
    seen = set()
    for directory in [*path.split(os.pathsep), *os.defpath.split(os.pathsep)]:
        # Internal operations must not select a cwd-dependent executable.
        if not os.path.isabs(directory):
            continue
        candidate = shutil.which('git', path=directory)
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            result = subprocess.run([candidate, '--version'], capture_output=True,
                                    text=True, timeout=5, check=False)
            if result.returncode == 0 and result.stdout.startswith('git version '):
                return candidate
            detail = (result.stderr or result.stdout).strip()[:300]
        except (OSError, subprocess.TimeoutExpired) as error:
            detail = str(error)[:300]
        failures.append(f'{candidate}: {detail}')
    raise RuntimeError('No runnable Git executable: ' + ('; '.join(failures)[:1000]
                       or 'Git is not installed on PATH'))

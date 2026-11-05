"""Resolve a runnable Git and the environment of every model- and harness-run Git.

Entry points: git_executable, without_automatic_git_maintenance and
GIT_STALL_SECONDS, the liveness bound of supervised harness Git. Probe PATH
candidates before any repository command; never retry a failed write or change
the process environment. Cache successful selection by PATH and loader
environment; failed probes remain retryable. Explicit repository maintenance is
scripts/git_maintenance.py (docs/runbooks/GIT_WORKSPACE_GUARD.md).
"""
from __future__ import annotations

from functools import lru_cache
import os
import shutil
import subprocess

# Metadata-only commands identify their inputs explicitly. Starting their child
# at the filesystem root avoids cwd resolution on the project's mounted tree.
GIT_METADATA_CWD = os.path.abspath(os.sep)

# GIT_CONFIG_PARAMETERS outranks repository configuration and legacy Git
# binaries read it too; an explicit ``git -c`` in the command still wins.
AUTOMATIC_GIT_MAINTENANCE_OFF = ("'gc.auto=0' 'maintenance.auto=false' "
                                 "'gc.autoDetach=false' 'maintenance.autoDetach=false'")

# Fixed-class liveness bound (contracts/resource_policy_v1.json): constant,
# never derived from hardware, tree size or command. On the shared checkout
# volume a Git command waits as long as the volume does, whether it reads one
# ref or hashes the whole tree, so harness Git that admission depends on runs
# under lib/project_mod/activity_supervised_process.py with no total deadline.
# The window renews whenever any thread of the Git process tree executes: it
# ends only a command blocked on an unresponsive volume, never a slow one.
GIT_STALL_SECONDS = 90.0


def without_automatic_git_maintenance(environment: dict[str, str]) -> dict[str, str]:
    """Return a copy in which no Git command starts automatic maintenance.

    Commit, merge, rebase, am and fetch otherwise start ``gc --auto``. Detached,
    it escapes the command's process group with inherited workspace leases; in
    the foreground it is unbounded hidden work inside the command, rerun by
    every later writer while Git cannot bring its object counts below the
    threshold. Explicit opt-ins (``git -c gc.auto=N``) stay in the foreground.
    """
    result = dict(environment)
    parameters = result.get('GIT_CONFIG_PARAMETERS', '').strip()
    if not parameters.endswith(AUTOMATIC_GIT_MAINTENANCE_OFF):
        result['GIT_CONFIG_PARAMETERS'] = (
            parameters + ' ' + AUTOMATIC_GIT_MAINTENANCE_OFF).strip()
    return result


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
                                    text=True, timeout=5, check=False, cwd=GIT_METADATA_CWD)
            if result.returncode == 0 and result.stdout.startswith('git version '):
                return candidate
            detail = (result.stderr or result.stdout).strip()[:300]
        except (OSError, subprocess.TimeoutExpired) as error:
            detail = str(error)[:300]
        failures.append(f'{candidate}: {detail}')
    raise RuntimeError('No runnable Git executable: ' + ('; '.join(failures)[:1000]
                       or 'Git is not installed on PATH'))

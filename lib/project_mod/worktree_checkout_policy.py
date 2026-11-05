"""Per-invocation checkout concurrency for fresh integration worktrees.

Entry: checkout_worker_config. Reads Git's effective destination configuration
and the shared cached launch CPU snapshot; never writes configuration. Memory
never narrows the worker count. The integration owner retains hook,
cancellation, lease and process lifecycles.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from lib.git_runtime import git_executable
from runtime_guards import probe_system_resources


def checkout_worker_config(cwd: Path, *, env: dict[str, str]) -> list[str]:
    """Default to every allowed core; leave explicit Git settings intact.

    Query the new destination after registration so worktree-specific config and
    conditional includes have the same context as checkout. Even explicit zero,
    negative, empty or invalid values belong to Git, not our policy parser.
    A failed config read adds no override; actual checkout retains Git's errors.
    Small-tree parallelism thresholds remain Git/user policy. Old Git safely
    ignores this configuration key rather than requiring a failed-write retry.
    """
    try:
        configured = subprocess.run(
            [git_executable(), 'config', '--get', 'checkout.workers'],
            cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2.0, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if configured.returncode != 1:  # 1 alone means a successful absent-key read.
        return []

    workers = 1
    try:
        cpu = probe_system_resources().effective_cpu_count
        if type(cpu) is int and cpu > 0:
            workers = cpu
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        # Optimization failure must not prevent an otherwise valid checkout.
        workers = 1
    return ['-c', f'checkout.workers={workers}']

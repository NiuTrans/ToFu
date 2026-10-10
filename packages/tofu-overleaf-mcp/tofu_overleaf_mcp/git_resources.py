"""Launch observation and native limits for disposable Overleaf Git workers.

The standalone MCP package owns this budget; it never imports the Tofu host.
GitOperations probes once. Explicit overrides remain inside contract ceilings.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
import shutil
import tempfile

import psutil

from .writes import contract

MIB = 1024 * 1024


@dataclass(frozen=True)
class GitBudget:
    memory_bytes: int
    workspace_bytes: int
    cpu_count: int
    memory_capacity: int
    memory_available: int
    disk_free: int

    def report(self):
        return asdict(self)


def probe_budget(environment=None):
    environment = os.environ if environment is None else environment
    limits = contract()['x-resource-limits']
    cpu = 1
    capacity = available = 0
    free = None
    try:
        memory = psutil.virtual_memory()
        capacity, available = memory.total, memory.available
        cpu = max(1, len(psutil.Process().cpu_affinity()))
        # Rootless containers normally expose their own cgroup at this mount.
        # An unavailable observation must reduce, never enlarge, the fallback.
        cgroup = Path('/sys/fs/cgroup')
        if (cgroup / 'memory.max').is_file():
            maximum = (cgroup / 'memory.max').read_text().strip()
            if maximum != 'max':
                capacity = min(capacity, int(maximum))
                available = min(available, max(0, int(maximum) - int((cgroup / 'memory.current').read_text())))
        if (cgroup / 'cpu.max').is_file():
            quota, period = (cgroup / 'cpu.max').read_text().split()
            if quota != 'max':
                cpu = min(cpu, max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError, AttributeError, psutil.Error):
        capacity = available = 0
        cpu = 1
    try:
        free = shutil.disk_usage(tempfile.gettempdir()).free
    except OSError:
        pass
    memory = max(128 * MIB, min(256 * MIB, capacity // 32, available // 8))
    disk = min(limits['overleaf_git_workspace_bytes'], free // 64) if free is not None else 64 * MIB
    for key, minimum, maximum in [('MEMORY', 128 * MIB, limits['overleaf_git_memory_bytes']),
                                  ('WORKSPACE', MIB, limits['overleaf_git_workspace_bytes'])]:
        value = environment.get('TOFU_OVERLEAF_GIT_' + key + '_MIB')
        if value is not None:
            try:
                parsed = int(value) * MIB
            except (ValueError, TypeError) as exc:
                raise ValueError('git_budget_invalid') from exc
            if not minimum <= parsed <= maximum:
                raise ValueError('git_budget_invalid')
            if key == 'MEMORY':
                memory = parsed
            else:
                disk = parsed
    return GitBudget(memory, disk, cpu, capacity, available, free if free is not None else -1)


def apply_native_limits(memory_bytes, seconds, file_bytes):
    """Allocation/file bounds supplement the parent's aggregate tree monitor."""
    if os.name == 'nt':
        # The parent owns the Windows Job Object before accepting a worker.
        return
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes * 2, memory_bytes * 2))
    cpu = max(1, math.ceil(seconds))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    if hasattr(os, 'sched_setaffinity'):
        os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})

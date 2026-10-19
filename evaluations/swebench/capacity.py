"""Offline hardware allocation for symmetric rootless benchmark pairs.

Entry point: plan_rootless_pair. The caller supplies pinned task resource
requirements and already-owned VM reservations; the canonical launch probe is
the hardware authority. This planner never starts/stops a process or a model.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json

from runtime_guards import SystemResourceSnapshot


def plan_rootless_pair(
    snapshot: SystemResourceSnapshot,
    task_resources: Mapping[str, Mapping[str, int]],
    *,
    attempts_per_task: int = 1,
    existing_vm_cpus: int = 0,
    existing_vm_memory_mb: int = 0,
    host_memory_reserve_mb: int = 2048,
    state_disk_free_mb: int | None = None,
    per_vm_disk_bound_mb: int | None = None,
    host_disk_reserve_mb: int = 8192,
) -> dict:
    """Choose the largest equal width that fits every possible active roster.

    CPU and memory use independent largest-task sums, so randomized task order
    cannot exceed either allocation. Containers keep their benchmark resource
    requests; each QEMU guest needs at least two CPUs for its boot/services.
    Missing memory evidence cannot justify a large speculative VM allocation.
    """
    if type(attempts_per_task) is not int or not 1 <= attempts_per_task <= 100:
        raise ValueError('attempts_per_task must be between 1 and 100')
    counts = (existing_vm_cpus, existing_vm_memory_mb, host_memory_reserve_mb)
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError('existing allocations and reserve must be nonnegative integers')
    if not task_resources or snapshot.effective_memory_available_mb is None:
        raise ValueError('task resources and observed memory headroom are required')
    if (state_disk_free_mb is None) != (per_vm_disk_bound_mb is None):
        raise ValueError('state disk headroom and per-VM bound must be supplied together')
    if state_disk_free_mb is not None and any(
            type(value) is not int or value <= 0
            for value in (state_disk_free_mb, per_vm_disk_bound_mb, host_disk_reserve_mb)):
        raise ValueError('disk headroom, per-VM bound and reserve must be positive integers')
    rows = []
    for name, resource in sorted(task_resources.items()):
        cpu, memory = resource.get('cpus'), resource.get('memory_mb')
        if not name or any(type(v) is not int or v <= 0 for v in (cpu, memory)):
            raise ValueError('each task requires a name, positive CPUs and memory')
        rows.append({'task': name, 'cpus': max(2, cpu), 'memory_mb': max(512, memory)})
    cpu_budget = snapshot.effective_cpu_count - existing_vm_cpus
    memory_budget = snapshot.effective_memory_available_mb - existing_vm_memory_mb - host_memory_reserve_mb
    cpu_order = sorted((row['cpus'] for row in rows for _ in range(attempts_per_task)), reverse=True)
    memory_order = sorted((row['memory_mb'] for row in rows for _ in range(attempts_per_task)), reverse=True)
    concurrency = cpu = memory = 0
    for width in range(1, len(cpu_order) + 1):
        candidate_cpu, candidate_memory = sum(cpu_order[:width]), sum(memory_order[:width])
        disk_fits = (state_disk_free_mb is None or
                     width * 2 * per_vm_disk_bound_mb <= state_disk_free_mb - host_disk_reserve_mb)
        if candidate_cpu * 2 > cpu_budget or candidate_memory * 2 > memory_budget or not disk_fits:
            break
        concurrency, cpu, memory = width, candidate_cpu, candidate_memory
    if not concurrency:
        raise ValueError('available hardware cannot hold one trial per arm')
    return {
        'schema': 'tofu.rootless-pair-capacity/v1',
        'probe': snapshot.as_dict(), 'taskCount': len(rows), 'attemptsPerTask': attempts_per_task,
        'taskResourcesSha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
        'existingVmCpus': existing_vm_cpus, 'existingVmMemoryMb': existing_vm_memory_mb,
        'hostMemoryReserveMb': host_memory_reserve_mb,
        'concurrencyPerArm': concurrency, 'totalConcurrentTrials': concurrency * 2,
        'stateDiskFreeMb': state_disk_free_mb, 'perVmDiskBoundMb': per_vm_disk_bound_mb,
        'hostDiskReserveMb': host_disk_reserve_mb if state_disk_free_mb is not None else None,
        'worstCasePairDiskMb': concurrency * 2 * per_vm_disk_bound_mb if per_vm_disk_bound_mb else None,
        'worstCasePairCpus': cpu * 2, 'worstCasePairMemoryMb': memory * 2,
    }

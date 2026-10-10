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
    existing_vm_cpus: int = 0,
    existing_vm_memory_mb: int = 0,
    host_memory_reserve_mb: int = 2048,
) -> dict:
    """Choose the largest equal width that fits every possible active roster.

    CPU and memory use independent largest-task sums, so randomized task order
    cannot exceed either allocation. Containers keep their benchmark resource
    requests; each QEMU guest needs at least two CPUs for its boot/services.
    Missing memory evidence cannot justify a large speculative VM allocation.
    """
    counts = (existing_vm_cpus, existing_vm_memory_mb, host_memory_reserve_mb)
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError('existing allocations and reserve must be nonnegative integers')
    if not task_resources or snapshot.effective_memory_available_mb is None:
        raise ValueError('task resources and observed memory headroom are required')
    rows = []
    for name, resource in sorted(task_resources.items()):
        cpu, memory = resource.get('cpus'), resource.get('memory_mb')
        if not name or any(type(v) is not int or v <= 0 for v in (cpu, memory)):
            raise ValueError('each task requires a name, positive CPUs and memory')
        rows.append({'task': name, 'cpus': max(2, cpu), 'memory_mb': max(512, memory)})
    cpu_budget = snapshot.effective_cpu_count - existing_vm_cpus
    memory_budget = snapshot.effective_memory_available_mb - existing_vm_memory_mb - host_memory_reserve_mb
    cpu_order = sorted((row['cpus'] for row in rows), reverse=True)
    memory_order = sorted((row['memory_mb'] for row in rows), reverse=True)
    concurrency = cpu = memory = 0
    for width in range(1, len(rows) + 1):
        candidate_cpu, candidate_memory = sum(cpu_order[:width]), sum(memory_order[:width])
        if candidate_cpu * 2 > cpu_budget or candidate_memory * 2 > memory_budget:
            break
        concurrency, cpu, memory = width, candidate_cpu, candidate_memory
    if not concurrency:
        raise ValueError('available hardware cannot hold one trial per arm')
    return {
        'schema': 'tofu.rootless-pair-capacity/v1',
        'probe': snapshot.as_dict(), 'taskCount': len(rows),
        'taskResourcesSha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest(),
        'existingVmCpus': existing_vm_cpus, 'existingVmMemoryMb': existing_vm_memory_mb,
        'hostMemoryReserveMb': host_memory_reserve_mb,
        'concurrencyPerArm': concurrency, 'totalConcurrentTrials': concurrency * 2,
        'worstCasePairCpus': cpu * 2, 'worstCasePairMemoryMb': memory * 2,
    }

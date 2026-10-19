"""Benchmark width follows both task requirements and available hardware."""
from dataclasses import replace

import pytest

from evaluations.swebench.capacity import plan_rootless_pair
from runtime_guards import SystemResourceSnapshot

pytestmark = pytest.mark.unit


def _snapshot():
    return SystemResourceSnapshot(60, 60, 60, 60, 215040, 179200, 215040,
                                  0, 215040, 179200, 500000, 400000)


def _tasks():
    return {str(i): {'cpus': 4 if i < 3 else 1,
                    'memory_mb': 8192 if i < 8 else 4096 if i < 21 else 2048}
            for i in range(89)}


def test_full_dataset_width_uses_all_cpu_and_accounts_for_existing_vms():
    plan = plan_rootless_pair(_snapshot(), _tasks())
    assert plan['concurrencyPerArm'] == 12
    assert plan['worstCasePairCpus'] == 60
    occupied = plan_rootless_pair(_snapshot(), _tasks(), existing_vm_cpus=8,
                                  existing_vm_memory_mb=20 * 1024)
    assert occupied['concurrencyPerArm'] == 10
    assert occupied['worstCasePairCpus'] + 8 == 60
    assert occupied['taskCount'] == 89


def test_constrained_memory_reduces_width_without_dropping_tasks():
    snapshot = replace(_snapshot(), effective_memory_available_mb=20 * 1024)
    plan = plan_rootless_pair(snapshot, _tasks())
    assert plan['concurrencyPerArm'] == 1
    assert plan['taskCount'] == 89
    assert plan['taskResourcesSha256'] == plan_rootless_pair(_snapshot(), _tasks())['taskResourcesSha256']


def test_unknown_or_exhausted_hardware_cannot_schedule_speculative_work():
    for snapshot in (replace(_snapshot(), effective_memory_available_mb=None),
                     replace(_snapshot(), effective_cpu_count=2)):
        with pytest.raises(ValueError):
            plan_rootless_pair(snapshot, _tasks())


def test_local_disk_headroom_bounds_width_after_cache_placement():
    plan = plan_rootless_pair(_snapshot(), _tasks(), state_disk_free_mb=80*1024,
                             per_vm_disk_bound_mb=22*1024)
    assert plan['concurrencyPerArm'] == 1
    assert plan['worstCasePairDiskMb'] == 44*1024
    larger = plan_rootless_pair(_snapshot(), _tasks(), state_disk_free_mb=600*1024,
                               per_vm_disk_bound_mb=22*1024)
    assert larger['concurrencyPerArm'] == 12
    with pytest.raises(ValueError, match='together'):
        plan_rootless_pair(_snapshot(), _tasks(), state_disk_free_mb=1)


def test_repeated_heavy_tasks_can_share_the_active_roster():
    plan = plan_rootless_pair(_snapshot(), _tasks(), attempts_per_task=5)
    assert plan['concurrencyPerArm'] == 7
    assert plan['worstCasePairCpus'] == 56
    assert plan['attemptsPerTask'] == 5

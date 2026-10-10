"""Program heap budgets scale from observed capacity, preserving overrides."""
import pytest
import runtime_guards as guards

pytestmark = pytest.mark.unit


def snapshot(capacity=None, available=None):
    return guards.SystemResourceSnapshot(
        host_cpu_count=8, affinity_cpu_count=8, cgroup_cpu_count=None,
        effective_cpu_count=8, host_memory_total_mb=capacity,
        host_memory_available_mb=available, cgroup_memory_limit_mb=None,
        cgroup_memory_current_mb=None, effective_memory_capacity_mb=capacity,
        effective_memory_available_mb=available, disk_total_mb=None,
        disk_free_mb=None)


def resolve(environment=None, capacity=None, available=None):
    return guards.resolve_resource_budget('TOFU_PROGRAM_HEAP_MAX_MIB',
        environment or {}, minimum=16, maximum=1024,
        snapshot=snapshot(capacity, available))


def test_heap_scales_up_and_down_with_capacity_and_headroom():
    lean = resolve(capacity=2048, available=1024)
    reference = resolve(capacity=8192, available=6144)
    workstation = resolve(capacity=65536, available=49152)
    assert 16 <= lean < reference < workstation <= 1024
    assert resolve(capacity=65536, available=1024) == lean


def test_probe_failure_is_lean_and_distributed_default_is_explicit():
    assert resolve() == 32
    assert resolve({'TOFU_DEPLOYMENT_MODE':'distributed'}) == 256


@pytest.mark.parametrize('value,expected', [('128',128),('1',16),('999999',1024)])
def test_explicit_override_is_preserved_with_finite_bounds(value, expected):
    assert resolve({'TOFU_PROGRAM_HEAP_MAX_MIB':value}) == expected


@pytest.mark.parametrize('value', ['broken','0','-3',''])
def test_malformed_override_falls_back_to_probed_capacity(value):
    assert resolve({'TOFU_PROGRAM_HEAP_MAX_MIB':value},8192,6144) == resolve(capacity=8192,available=6144)

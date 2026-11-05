"""The program heap limit is a fixed engine constant, preserving overrides."""
import pytest
import runtime_guards as guards

pytestmark = pytest.mark.unit

# wasm32 engine maximum; owner decision 2026-10-24: hardware never sizes it.
_FIXED_HEAP_MIB = 1024


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


@pytest.mark.parametrize('capacity,available', [
    (None, None), (2048, 1024), (8192, 6144), (65536, 49152), (65536, 1024)])
@pytest.mark.parametrize('mode', ['personal', 'distributed'])
def test_heap_is_identical_on_every_host_and_mode(capacity, available, mode):
    assert resolve({'TOFU_DEPLOYMENT_MODE': mode}, capacity, available) == _FIXED_HEAP_MIB


@pytest.mark.parametrize('value,expected', [('128',128),('1',16),('999999',1024)])
def test_explicit_override_is_preserved_with_finite_bounds(value, expected):
    assert resolve({'TOFU_PROGRAM_HEAP_MAX_MIB':value}) == expected


@pytest.mark.parametrize('value', ['broken','0','-3',''])
def test_malformed_override_falls_back_to_the_fixed_heap(value):
    assert resolve({'TOFU_PROGRAM_HEAP_MAX_MIB':value},8192,6144) == _FIXED_HEAP_MIB

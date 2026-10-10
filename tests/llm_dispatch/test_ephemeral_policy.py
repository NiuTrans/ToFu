"""Route residency scales with launch capacity, including pending executions."""
import pytest
import runtime_guards
from lib.llm_dispatch.ephemeral_policy import (
    MAX_REQUEST_ROUTE_SLOTS, resolve_ephemeral_slot_capacity,
)
from tests.deploy.test_deployment_configuration import _resource_snapshot

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('cpus,memory_mb', [(2, 4096), (8, 8192), (64, 131072)])
def test_route_budget_covers_launch_execution_capacity(monkeypatch, cpus, memory_mb):
    snapshot = _resource_snapshot(
        cpus=cpus, capacity_mb=memory_mb, available_mb=memory_mb * 3 // 4)
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda env: snapshot)
    environment = {}
    runtime_guards.install_runtime_resource_defaults(environment, snapshot=snapshot)
    capacity = resolve_ephemeral_slot_capacity(environment)
    roots = int(environment['TOFU_AGENT_WORKERS'])
    children = int(environment['TOFU_SWARM_MAX_PARALLEL']) * int(environment['TOFU_SWARM_SESSION_CAPACITY'])
    assert capacity >= (roots + children) * MAX_REQUEST_ROUTE_SLOTS
    small = _resource_snapshot(cpus=1, capacity_mb=2048, available_mb=1024)
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda env: small)
    assert capacity > resolve_ephemeral_slot_capacity({})


def test_explicit_budgets_require_no_additional_probe(monkeypatch):
    def unexpected_probe(*args, **kwargs):
        raise AssertionError('launch budgets must not re-probe the host')
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', unexpected_probe)
    environment = {
        'TOFU_AGENT_WORKERS': '32', 'TOFU_MAX_INFLIGHT_TASKS': '32',
        'TOFU_SYNC_WORKERS': '16', 'TOFU_SWARM_GLOBAL_WORKERS': '16',
        'TOFU_AGENT_QUEUE_CAPACITY': '1024',
    }
    capacity = resolve_ephemeral_slot_capacity(environment)
    assert capacity >= (32 + 1024 + 16 + 16) * MAX_REQUEST_ROUTE_SLOTS
    assert resolve_ephemeral_slot_capacity({'TOFU_EPHEMERAL_SLOT_CAPACITY': '4096'}) == 4096
    assert resolve_ephemeral_slot_capacity({'TOFU_EPHEMERAL_SLOT_CAPACITY': '1'}) == MAX_REQUEST_ROUTE_SLOTS
    assert resolve_ephemeral_slot_capacity({'TOFU_EPHEMERAL_SLOT_CAPACITY': '999999999999'}) < 999999999999


@pytest.mark.parametrize('value', ['', 'invalid', '0', '-1'])
def test_invalid_override_uses_finite_launch_budget(monkeypatch, value):
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda env: _resource_snapshot())
    assert resolve_ephemeral_slot_capacity({'TOFU_EPHEMERAL_SLOT_CAPACITY': value}) == resolve_ephemeral_slot_capacity({})

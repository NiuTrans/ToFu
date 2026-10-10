"""Resource policy behavior: productive demand, drainage, failure and ownership."""
from dataclasses import replace
import threading

import pytest

from lib.resource_control import (ResourceController, ResourceConflict, ResourceOwner,
                                  ResourcePort, ResourceSample)

pytestmark = pytest.mark.unit


class Owner:
    def __init__(self, capacity=2, **sample):
        self.value = ResourceSample(capacity, **sample)
        self.applied = []
        self.fail = False

    def resize(self, target):
        self.applied.append(target)
        if self.fail:
            raise RuntimeError('injected resize failure')
        self.value = replace(self.value, capacity=target)

    def port(self, **kw):
        return ResourcePort(lambda: self.value, self.resize, 1, 32, **kw)


def test_growth_requires_sustained_demand_and_headroom():
    now = [0.]
    controller = ResourceController(clock=lambda: now[0])
    owner = Owner(waiting=3)
    controller.bind('execution.root', owner.port(growth_cost_bytes=100))
    for t, headroom in [(0, None), (5, None), (10, 50), (15, 200)]:
        now[0] = t
        controller.tick(headroom_bytes=headroom)
        assert owner.value.capacity == (3 if t == 15 else 2)
    # An unresolved throughput trial cannot repeatedly grow every cooldown.
    now[0] = 35
    controller.tick(headroom_bytes=200)
    assert owner.value.capacity == 3


def test_pressure_drains_but_keeps_active_work_and_buffers():
    controller = ResourceController()
    owner = Owner(capacity=8, used=8, waiting=2)
    buffer = Owner(capacity=16, used=15)
    controller.bind('execution.root', owner.port())
    controller.bind('storage.frames', buffer.port())
    state = controller.tick(memory_pressure=True)['resources']
    assert state['execution.root']['effective'] == 6
    assert state['execution.root']['used'] == 8
    assert state['execution.root']['status'] == 'draining'
    assert state['storage.frames']['effective'] == 16


def test_dependency_order_and_failure_are_truthful():
    controller = ResourceController()
    order = []
    root, dependency = Owner(), Owner()
    for key, value in [('execution.root', root), ('model.requests', dependency)]:
        def resize(target, key=key, value=value):
            order.append(key)
            value.resize(target)
        port = value.port()
        port.resize = resize
        controller.bind(key, port)
    dependency.fail = True
    result = controller.apply(controller.plan({'execution.root': 3, 'model.requests': 3}))['resources']
    assert order == ['model.requests']
    assert result['model.requests']['status'] == 'failed'
    assert result['execution.root']['status'] == 'blocked'
    assert result['execution.root']['desired'] == 3
    assert result['execution.root']['effective'] == 2
    dependency.fail = False
    controller.apply(controller.plan({'execution.root': 4, 'model.requests': 4}))
    assert order[-2:] == ['model.requests', 'execution.root']
    controller.apply(controller.plan({'execution.root': 2, 'model.requests': 2}))
    assert order[-2:] == ['execution.root', 'model.requests']


def test_stale_and_foreign_plans_cannot_apply():
    controller, other = ResourceController(), ResourceController()
    owner = Owner()
    controller.bind('execution.root', owner.port())
    plan = controller.plan({'execution.root': 3})
    controller.apply(plan)
    with pytest.raises(ResourceConflict):
        controller.apply(plan)
    with pytest.raises(ResourceConflict):
        other.apply(plan)
    with pytest.raises(ValueError):
        controller.plan({'execution.root': 33})


def test_stagnation_rolls_back_and_backs_off():
    now = [0.]
    controller = ResourceController(clock=lambda: now[0])
    owner = Owner(waiting=2)
    controller.bind('execution.root', owner.port())
    controller.tick()
    now[0] = 5
    owner.value = replace(owner.value, completed=10)
    controller.tick()
    assert owner.value.capacity == 3
    now[0] = 35
    owner.value = replace(owner.value, completed=20)
    controller.tick()
    assert owner.value.capacity == 2
    for t in [55, 60, 90]:
        now[0] = t
        controller.tick()
    assert owner.value.capacity == 2
    now[0] = 96
    controller.tick()
    now[0] = 101
    controller.tick()
    assert owner.value.capacity == 3


def test_provider_pressure_stops_growth_without_rewriting_capacity():
    controller = ResourceController()
    root, provider = Owner(waiting=2), Owner(pressure=True)
    controller.bind('execution.root', root.port())
    controller.bind('model.requests', provider.port())
    for _ in range(3):
        controller.tick()
    assert not root.applied and not provider.applied


def test_owned_lease_releases_once_even_after_controller_shutdown():
    from lib.swarm.execution_gate import SwarmExecutionAccounting
    from lib.resource_control import install_controller, uninstall_controller
    from lib.resource_control.adapters import bind_swarm
    controller = ResourceController()
    controller.launch_policy = {'automatic': True, 'child_maximum': 2, 'task_bytes': 64}
    gate = SwarmExecutionAccounting(capacity=2, waiter_capacity=10)
    install_controller(controller)
    try:
        bind_swarm(gate)
        lease = controller.acquire('execution.child', ResourceOwner(7, 'task-1'))
        assert gate.snapshot()['active'] == 1
        uninstall_controller(controller)
        threads = [threading.Thread(target=lease.release) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert gate.snapshot()['active'] == 0
        assert lease.release() is False
    finally:
        uninstall_controller(controller)


@pytest.mark.parametrize('user,task', [(None,'a'),('1','a'),(True,'a'),(0,'a'),(1,'')])
def test_owner_is_explicit(user, task):
    with pytest.raises(ValueError):
        ResourceOwner(user, task)


def test_transient_resize_failure_retries_after_backoff():
    now = [0.]
    controller = ResourceController(clock=lambda: now[0])
    owner = Owner()
    controller.bind('execution.root', owner.port())
    owner.fail = True
    controller.apply(controller.plan({'execution.root': 4}))
    owner.fail = False
    controller.tick()
    assert owner.value.capacity == 2
    now[0] = 16
    assert controller.tick()['resources']['execution.root']['status'] == 'applied'
    assert owner.value.capacity == 4


def test_failures_are_not_counted_as_productive_throughput():
    now = [0.]
    controller = ResourceController(clock=lambda: now[0])
    owner = Owner(waiting=2)
    controller.bind('execution.root', owner.port())
    controller.tick()
    now[0] = 5
    owner.value = replace(owner.value, completed=100, errors=100)
    controller.tick()
    assert owner.value.capacity == 2


def test_pressure_stops_new_work_even_when_launch_envelope_is_large():
    controller = ResourceController()
    owner = Owner(capacity=32, used=4)
    controller.bind('execution.child', owner.port())
    result = controller.tick(memory_pressure=True)['resources']['execution.child']
    assert result['effective'] == 3 and result['used'] == 4
    assert result['status'] == 'draining'


@pytest.mark.parametrize('headroom,expected', [(None, 3), (200, 5), (2100, 24)])
def test_pressure_recovery_restores_healthy_envelope_with_measured_headroom(headroom, expected):
    now = [0.]
    controller = ResourceController(clock=lambda: now[0])
    owner = Owner(capacity=24, used=4, waiting=20)
    controller.bind('execution.root', owner.port(growth_cost_bytes=100))
    controller.tick(memory_pressure=True, headroom_bytes=0)
    assert owner.value.capacity == 3
    assert owner.value.used == 4
    for t in (15, 20):
        now[0] = t
        state = controller.tick(headroom_bytes=headroom)['resources']['execution.root']
    assert owner.value.capacity == expected
    assert owner.value.used == 4
    if expected > 3:
        assert state['reason'] == 'pressure_recovered'


def test_pressure_recovery_respects_external_ceiling_and_dependency_pressure():
    now = [0.]
    controller = ResourceController(clock=lambda: now[0])
    root = Owner(capacity=24, used=4, waiting=20)
    downstream = Owner(capacity=8, pressure=True)
    controller.bind('execution.root', root.port(growth_cost_bytes=100))
    controller.bind('model.requests', downstream.port(automatic=False))
    controller.tick(memory_pressure=True, headroom_bytes=0)
    for t in (15, 20):
        now[0] = t
        controller.tick(headroom_bytes=10000)
    assert root.value.capacity == 3
    downstream.value = replace(downstream.value, pressure=False)
    root.value = replace(root.value, ceiling=6)
    for t in (25, 30):
        now[0] = t
        controller.tick(headroom_bytes=10000)
    assert root.value.capacity == 6


def test_sustained_pressure_never_restores_capacity():
    now = [0.]
    controller = ResourceController(clock=lambda: now[0])
    root = Owner(capacity=24, used=4, waiting=20)
    controller.bind('execution.root', root.port(growth_cost_bytes=100))
    for t in (0, 15, 30, 45):
        now[0] = t
        controller.tick(memory_pressure=True, headroom_bytes=10000)
    assert root.value.capacity == 1
    assert root.value.used == 4
    for t in (60, 65):
        now[0] = t
        controller.tick(headroom_bytes=10000)
    assert root.value.capacity == 24

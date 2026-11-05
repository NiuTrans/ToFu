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


def test_growth_requires_sustained_demand():
    now = [0.]
    controller = ResourceController(clock=lambda: now[0])
    owner = Owner(waiting=3)
    controller.bind('execution.root', owner.port())
    controller.tick()
    assert owner.value.capacity == 2
    now[0] = 5
    controller.tick()
    assert owner.value.capacity == 3
    # An unresolved throughput trial cannot repeatedly grow every cooldown.
    now[0] = 25
    controller.tick()
    assert owner.value.capacity == 3


def test_tick_has_no_memory_input_and_never_shrinks_saturated_capacity():
    import inspect
    assert list(inspect.signature(ResourceController.tick).parameters) == ['self']
    now = [0.]
    controller = ResourceController(clock=lambda: now[0])
    owners = {
        'execution.root': Owner(capacity=8, used=8),
        'execution.child': Owner(capacity=32, used=40),
        'storage.rpc': Owner(capacity=4, used=4),
        'storage.frames': Owner(capacity=16, used=16),
    }
    initial = {key: owner.value.capacity for key, owner in owners.items()}
    for key, owner in owners.items():
        controller.bind(key, owner.port())
    for t in range(0, 600, 5):
        now[0] = t
        controller.tick()
    for key, owner in owners.items():
        assert owner.applied == [], key
        assert owner.value.capacity == initial[key], key


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
    controller.launch_policy = {'automatic': True, 'child_maximum': 2}
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


def test_growth_respects_external_ceiling_and_dependency_pressure():
    now = [0.]
    controller = ResourceController(clock=lambda: now[0])
    root = Owner(capacity=8, used=8, waiting=20)
    downstream = Owner(capacity=8, pressure=True)
    controller.bind('execution.root', root.port())
    controller.bind('model.requests', downstream.port(automatic=False))
    for t in (0, 5, 10):
        now[0] = t
        controller.tick()
    assert root.value.capacity == 8
    downstream.value = replace(downstream.value, pressure=False)
    root.value = replace(root.value, ceiling=9)
    for t in (15, 20):
        now[0] = t
        controller.tick()
    assert root.value.capacity == 9
    assert root.value.used == 8

"""Lazy ports over execution/dispatch/storage owners; never a second ledger.

The serving runtime binds root execution once. Optional domains bind on their
first normal use, without loading providers or opening storage for sampling.
"""
from __future__ import annotations

import sys

from . import current_controller
from .contract import ResourcePort, ResourceSample


def _sample(values: dict) -> ResourceSample:
    return ResourceSample(**{key: value for key, value in values.items()
                             if key in ResourceSample.__dataclass_fields__})


def bind_root(controller, runtime, admission) -> None:
    initial = runtime.agent_executor.resource_snapshot()['capacity']
    admission_capacity = admission.resource_snapshot()['capacity']
    initial = min(initial, admission_capacity or initial)
    policy = controller.launch_policy

    def read():
        worker = runtime.agent_executor.resource_snapshot()
        admitted = admission.resource_snapshot()
        worker['capacity'] = min(worker['capacity'], admitted['capacity'] or worker['capacity'])
        worker['used'] = max(worker['used'], admitted['used'])
        worker['blocked'] = admitted['blocked']
        worker['pressure'] = admitted['pressure']
        return _sample(worker)

    def resize(target):
        # Refuse new roots first on shrink; prepare workers first on growth.
        if target < read().capacity:
            if admission_capacity:
                admission.resize(target)
            runtime.agent_executor.resize(target)
        else:
            runtime.agent_executor.resize(target)
            if admission_capacity:
                admission.resize(target)

    controller.bind('execution.root', ResourcePort(
        read, resize, 1, max(initial, policy['root_maximum']),
        automatic=False, growth_cost_bytes=0), identity=runtime)


def bind_swarm(gate) -> None:
    controller = current_controller()
    if (controller is None or not controller.launch_policy['automatic']
            or controller.is_bound('execution.child', gate)):
        return
    def make_port():
        policy = controller.launch_policy
        initial = gate.capacity or policy['child_maximum']
        # The full session envelope preserves formerly uncapped useful work.
        gate.resize(initial)

        def read():
            value = gate.snapshot()
            return ResourceSample(max(1, value['active']), value['active'], value['waiting'],
                                  completed=max(0, value['accepted'] - value['active']
                                                - value['waiting'] - value['cancelled']))

        def acquire(owner, **kwargs):
            gate.acquire(owner_key=owner.owner_user_id, **kwargs)
            return lambda: gate.release(owner.owner_user_id)

        return ResourcePort(
            read, gate.resize, 1, max(initial, policy['child_maximum']), acquire=acquire,
            automatic=False, growth_cost_bytes=0)
    controller.bind_lazy('execution.child', gate, make_port)


def bind_dispatcher(dispatcher) -> None:
    controller = current_controller()
    if controller is None or controller.is_bound('model.requests', dispatcher):
        return
    controller.bind_lazy('model.requests', dispatcher, lambda: ResourcePort(
        lambda: _sample(dispatcher.resource_snapshot()),
        dispatcher.resize_resource_capacity, 1,
        max(1, controller.launch_policy['root_maximum'] + controller.launch_policy['child_maximum']), automatic=False))


def bind_ephemeral() -> None:
    controller = current_controller()
    if controller is None or controller.is_bound('model.routes'):
        return
    from lib.llm_dispatch.ephemeral import count_ephemeral_slots
    controller.bind_lazy('model.routes', controller, lambda: ResourcePort(
        lambda: ResourceSample(max(1, count_ephemeral_slots()), used=count_ephemeral_slots()),
        lambda target: None, 1, sys.maxsize,
        automatic=False))


def bind_storage(client) -> None:
    controller = current_controller()
    if controller is None:
        return
    # SQLite/legacy clients are not made into native RPC clients by this port.
    # Only the selected backend's advertised live capability may be bound.
    if hasattr(client, 'resize_resource_capacity') and not controller.is_bound('storage.rpc', client):
        sample = _sample(client.resource_snapshot())
        controller.bind('storage.rpc', ResourcePort(
            lambda: _sample(client.resource_snapshot()), client.resize_resource_capacity,
            1, max(sample.capacity, sample.ceiling or sample.capacity),
            automatic=controller.launch_policy['automatic'],
            growth_cost_bytes=64 * 1024), identity=client)
    if hasattr(client, 'resource_frame_admission') and not controller.is_bound('storage.frames', client):
        frames = client.resource_frame_admission()

        def read():
            value = frames.metrics()
            return ResourceSample(
                value['frame_bytes_capacity'],
                value.get('frame_buffer_memory_bytes', value['frame_bytes_inflight']),
                value['frame_admission_waiting'])

        capacity = read().capacity
        controller.bind('storage.frames', ResourcePort(
            read, frames.resize, capacity, max(capacity, controller.launch_policy['frame_maximum']),
            growth_cost_bytes=1, automatic=controller.launch_policy['automatic']), identity=client)

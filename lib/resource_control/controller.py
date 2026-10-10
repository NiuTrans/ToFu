"""Versioned resource plans and bounded feedback; adapters own every counter.

No provider calls, database access, timers or environment mutation happen here.
Adapters are sampled outside the catalog lock; one apply lock serializes bind,
resize and shutdown. Partial application remains visible and blocks dependents.
"""
from __future__ import annotations

from dataclasses import asdict
import copy
import math
import threading
import time
import uuid

from .contract import (
    ResourceLease, ResourceOwner, ResourcePlan, ResourcePort, ResourceSample,
    ResourceState, catalog_document,
)


class ResourceConflict(ValueError):
    pass


class ResourceController:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        document = catalog_document()
        self._specs = {item['id']: item for item in document['resources']}
        self._policy = document['control']
        self._states: dict[str, ResourceState] = {}
        self._identities: dict[str, object] = {}
        self._lock = threading.RLock()
        self._apply_lock = threading.RLock()
        self._revision = 0
        self._id = uuid.uuid4().hex
        self._closed = False

    def catalog(self) -> dict:
        return copy.deepcopy(catalog_document())

    def bind(self, resource_id: str, port: ResourcePort, *, identity=None) -> None:
        if resource_id not in self._specs:
            raise ValueError(f'undeclared resource: {resource_id}')
        with self._apply_lock:
            with self._lock:
                if self._closed:
                    return
                if identity is not None and self._identities.get(resource_id) is identity:
                    return
            sample = port.read()
            if not 0 <= port.minimum <= sample.capacity <= port.maximum:
                raise ValueError(f'invalid resource envelope: {resource_id}')
            with self._lock:
                self._states[resource_id] = ResourceState(
                    port, sample.capacity, sample.capacity, sample=sample)
                self._identities[resource_id] = identity
                self._revision += 1

    def bind_lazy(self, resource_id: str, identity, make_port) -> None:
        """Serialize first-use owner initialization with policy application."""
        with self._apply_lock:
            if self._closed or self.is_bound(resource_id, identity):
                return
            self.bind(resource_id, make_port(), identity=identity)

    def is_bound(self, resource_id: str, identity=None) -> bool:
        with self._lock:
            return (not self._closed and resource_id in self._states
                    and (identity is None or self._identities.get(resource_id) is identity))

    def sample(self) -> dict[str, ResourceSample]:
        with self._apply_lock:
            if self._closed:
                return {}
            samples = {}
            for resource_id, state in tuple(self._states.items()):
                try:
                    sample = state.port.read()
                    if min(sample.capacity, sample.used, sample.waiting) < 0:
                        raise ValueError('negative resource sample')
                except Exception as exc:
                    with self._lock:
                        state.status = 'unavailable'
                        state.reason = type(exc).__name__
                    continue
                with self._lock:
                    state.sample = sample
                    state.effective = sample.capacity
                    if state.status not in {'failed', 'blocked'}:
                        state.status = ('draining' if sample.used > sample.capacity
                                        else 'applied' if state.desired == sample.capacity
                                        else 'pending')
                samples[resource_id] = sample
            return samples

    def snapshot(self) -> dict:
        self.sample()
        with self._lock:
            return {'contract_version': 'tofu.resource-policy/v1',
                    'revision': self._revision, 'closed': self._closed, 'active': not self._closed,
                    'resources': {key: {
                        'unit': self._specs[key]['unit'],
                        'scope': self._specs[key]['scope'],
                        'desired': state.desired, 'effective': state.effective,
                        'minimum': state.port.minimum, 'maximum': state.port.maximum,
                        'automatic': state.port.automatic, 'status': state.status,
                        'reason': state.reason, **asdict(state.sample),
                    } for key, state in self._states.items()}}

    def capacity(self, resource_id: str, fallback: int) -> int:
        with self._lock:
            state = self._states.get(resource_id)
            return state.effective if state and not self._closed else fallback

    def plan(self, changes: dict[str, int], *, revision: int | None = None,
             reason: str = 'policy') -> ResourcePlan:
        with self._lock:
            if self._closed or (revision is not None and revision != self._revision):
                raise ResourceConflict('resource policy revision is no longer current')
            for resource_id, target in changes.items():
                state = self._states.get(resource_id)
                if state is None:
                    raise ValueError(f'resource is not active: {resource_id}')
                ceiling = state.port.maximum
                if state.sample.ceiling is not None:
                    ceiling = min(ceiling, state.sample.ceiling)
                if (isinstance(target, bool) or not isinstance(target, int)
                        or not state.port.minimum <= target <= ceiling):
                    raise ValueError(f'resource target outside capacity: {resource_id}')
            return ResourcePlan(self._revision, tuple(sorted(changes.items())),
                                reason, self._id)

    def apply(self, plan: ResourcePlan) -> dict:
        with self._apply_lock:
            if plan.controller_id != self._id:
                raise ResourceConflict('resource plan belongs to another controller')
            self.plan(dict(plan.changes), revision=plan.revision, reason=plan.reason)
            changes = dict(plan.changes)
            # Stop new upstream work before shrinking dependencies; prepare
            # dependencies before allowing upstream growth. Neither kills work.
            def depth(key):
                return 1 + max((depth(dep) for dep in self._specs[key]['dependencies']), default=0)
            shrinking = sorted((k for k in changes if changes[k] < self._states[k].effective),
                               key=depth, reverse=True)
            growing = sorted((k for k in changes if k not in shrinking), key=depth)
            failed = set()
            with self._lock:
                self._revision += 1
                for key, target in changes.items():
                    self._states[key].desired = target
                    self._states[key].status = 'pending'
            for key in shrinking + growing:
                state = self._states[key]
                if key in growing and any(
                        dep in failed or (dep in self._states and self._states[dep].status
                                          in {'failed', 'blocked', 'unavailable'})
                        for dep in self._specs[key]['dependencies']):
                    state.status, state.reason = 'blocked', 'dependency_unavailable'
                    failed.add(key)
                    continue
                try:
                    state.port.resize(changes[key])
                    acknowledged = state.port.read()
                    if acknowledged.capacity != changes[key]:
                        raise RuntimeError('resource owner did not acknowledge target')
                except Exception as exc:
                    with self._lock:
                        state.status, state.reason = 'failed', type(exc).__name__
                        state.backoff_until = self._clock() + self._policy['cooldown_seconds']
                    failed.add(key)
                    continue
                with self._lock:
                    state.sample = acknowledged
                    state.effective = acknowledged.capacity
                    state.reason = plan.reason
                    state.status = ('draining' if acknowledged.used > acknowledged.capacity
                                    else 'applied')
                    state.last_change = self._clock()
                    state.healthy_samples = 0
            return self.snapshot()

    def acquire(self, resource_id: str, owner: ResourceOwner, **kwargs) -> ResourceLease:
        if not isinstance(owner, ResourceOwner):
            raise ValueError('resource owner must be explicit')
        with self._lock:
            if self._closed:
                raise ResourceConflict('resource controller stopped')
            port = self._states[resource_id].port
        if port.acquire is None:
            raise ValueError('resource is acquired through its execution owner')
        release = port.acquire(owner, **kwargs)
        return ResourceLease(resource_id, owner, release)

    def tick(self, *, memory_pressure: bool = False,
             headroom_bytes: int | None = None, allow_growth: bool = True) -> dict:
        with self._apply_lock:
            samples = self.sample()
            now = self._clock()
            changes = {}
            reasons = []
            available = headroom_bytes
            for key, sample in samples.items():
                state = self._states[key]
                previous = state.previous_sample
                elapsed = now - state.previous_time
                successful = max(0, sample.completed - sample.errors)
                previous_successful = max(0, previous.completed - previous.errors) if previous else 0
                completed = max(0, successful - previous_successful) if previous else 0
                errors_increased = bool(previous and sample.errors > previous.errors)
                if previous and elapsed > 0 and completed:
                    rate = completed / elapsed
                    state.completion_rate = (rate if not state.completion_rate
                                             else state.completion_rate * .75 + rate * .25)
                blocked = max(0, sample.blocked - previous.blocked) if previous else 0
                state.previous_sample, state.previous_time = sample, now
                if not state.port.automatic or state.status == 'unavailable':
                    continue
                if state.status in {'failed', 'blocked'}:
                    ceiling = min(state.port.maximum, sample.ceiling or state.port.maximum)
                    if (now >= state.backoff_until and
                            (state.desired <= sample.capacity or (not memory_pressure and allow_growth)) and
                            state.port.minimum <= state.desired <= ceiling):
                        changes[key] = state.desired
                        reasons.append('resize_retry')
                    continue
                # An external provider constraint freezes upstream growth. It
                # never rewrites credentials, quotas, task quality or budgets.
                downstream_busy = any(
                    dep in self._states and (self._states[dep].status in
                    {'failed', 'blocked', 'unavailable'} or
                    (dep in samples and (samples[dep].pressure or samples[dep].waiting)))
                    for dep in self._specs[key]['dependencies'])
                pressured = memory_pressure
                demand = sample.waiting > 0 or blocked > 0
                if pressured:
                    state.trial = None
                    state.healthy_samples = 0
                    # Buffers containing completed responses retain their
                    # drain capacity; only execution admission is reduced.
                    if self._specs[key]['kind'] == 'execution':
                        target = max(state.port.minimum, int(min(sample.capacity, sample.used)
                                                             * self._policy['pressure_fraction']))
                        if target < sample.capacity and now - state.last_change >= self._policy['cooldown_seconds']:
                            # A temporary squeeze is not a new throughput
                            # baseline. Keep the last healthy envelope across
                            # repeated shrink steps so recovery does not take
                            # dozens of slow growth trials on a large host.
                            state.pressure_capacity = max(
                                state.pressure_capacity or 0, sample.capacity)
                            changes[key] = target
                            reasons.append('memory_pressure')
                    continue
                if state.trial and now - state.trial[3] >= self._policy['evaluation_seconds']:
                    before, baseline_rate, start_completed, started = state.trial
                    trial_completed = successful - start_completed
                    state.trial = None
                    if (demand and trial_completed >= 4 and baseline_rate > 0
                            and trial_completed / (now - started) <= baseline_rate):
                        changes[key] = max(state.port.minimum, min(before, state.port.maximum,
                                                                  sample.ceiling or state.port.maximum))
                        state.backoff_until = now + self._policy['stagnation_backoff_seconds']
                        reasons.append('no_completion_gain')
                        continue
                if (state.trial or now < state.backoff_until
                        or not allow_growth or errors_increased or sample.pressure or downstream_busy or not demand
                        or now - state.last_change < self._policy['cooldown_seconds']):
                    state.healthy_samples = 0
                    continue
                state.healthy_samples += 1
                if state.healthy_samples < self._policy['healthy_samples_before_growth']:
                    continue
                ceiling = min(state.port.maximum, sample.ceiling or state.port.maximum)
                recovery = (self._policy['restore_capacity_after_pressure']
                            and state.pressure_capacity is not None)
                recovery_ceiling = min(ceiling, state.pressure_capacity or ceiling)
                if recovery and sample.capacity >= recovery_ceiling:
                    state.pressure_capacity = None
                    recovery = False
                increment = (recovery_ceiling - sample.capacity if recovery
                             else max(1, math.ceil(sample.capacity * self._policy['growth_fraction'])))
                if state.port.growth_cost_bytes:
                    if available is None:
                        continue
                    increment = min(increment, available // state.port.growth_cost_bytes)
                target = min(ceiling, sample.capacity + increment)
                if target > sample.capacity:
                    if available is not None:
                        available -= (target - sample.capacity) * state.port.growth_cost_bytes
                    # Restored capacity pays the same memory/dependency cost,
                    # without a new speculative throughput trial. Retire its
                    # saved ceiling only after the next native owner sample.
                    if not recovery:
                        state.trial = (sample.capacity, state.completion_rate, successful, now)
                    changes[key] = target
                    reasons.append('pressure_recovered' if recovery else 'queued_work')
            if changes:
                return self.apply(self.plan(changes, reason=','.join(sorted(set(reasons)))))
            return self.snapshot()

    def close(self) -> None:
        with self._apply_lock, self._lock:
            self._closed = True
            self._revision += 1
            # Leases retain their original owner release callback. Stopping
            # the sampler does not close pools or revoke productive work.
            self._states.clear()
            self._identities.clear()

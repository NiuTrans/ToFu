"""The delivery contention benchmark measures the production coordinator honestly.

``benchmarks/task_event_delivery_contention.py`` is evidence only while its
counting lock really is the coordinator's state lock, every sample proves exact
per-lane commit-then-publish order, and its counts describe the scheduler: a
paced bursty workload never retires a parked pump before its idle deadline,
never broadcasts to parked pumps and needs at most one pump job per lane.
Its backlog mode reports a barrier only after the stub authority committed
every row once, in order, each frame published after its commit, and its
transaction counts are the barrier's persist calls.
Counts only; clocks stay host observations.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def benchmark():
    spec = importlib.util.spec_from_file_location(
        'task_event_delivery_contention_measure',
        ROOT / 'benchmarks' / 'task_event_delivery_contention.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_paced_lanes_reuse_parked_pumps_without_broadcasts(benchmark):
    from lib.tasks_pkg import event_delivery
    sample = benchmark.run_sample(event_delivery, lanes=4, events_per_lane=40, persist_ms=1,
                                  think_ms=2, burst_every=20, burst_pause_ms=30,
                                  cpu_threads=0, idle_reuse=8)
    assert sample['events'] == 160
    assert sample['lock_acquisitions_per_event'] > 0
    assert sample['pump_exits_during_workload'] == 0
    assert 1 <= sample['pump_jobs'] <= 4
    assert sample['notify_all_per_event'] == 0


def test_a_queued_backlog_awaits_at_most_its_head_and_one_batch(benchmark):
    sample = benchmark.run_backlog_sample(events=12, authority_ms=0, cpu_threads=0)
    assert sample['events'] == 12
    transactions = sample['record_transactions'] + sample['batch_transactions']
    assert transactions == sample['persist_calls'] <= 2
    assert sample['max_entries_per_transaction'] >= 6


def test_a_lost_backlog_row_refuses_to_report(benchmark, monkeypatch):
    original = benchmark.SlowTurnAuthority.command

    def lose_a_row(self, operation, payload, *args, **kwargs):
        result = original(self, operation, payload, *args, **kwargs)
        with self._lock:
            self.rows.pop()
        return result

    monkeypatch.setattr(benchmark.SlowTurnAuthority, 'command', lose_a_row)
    with pytest.raises(AssertionError, match='backlog lost'):
        benchmark.run_backlog_sample(events=6, authority_ms=0, cpu_threads=0)


def test_lost_or_reordered_delivery_refuses_to_report(benchmark, monkeypatch):
    from lib.tasks_pkg import event_delivery
    original = event_delivery.EventDeliveryCoordinator.submit

    def drop_publication(self, lane, sequence, retained_bytes, persist, publish, **kwargs):
        return original(self, lane, sequence, retained_bytes, persist,
                        (lambda: None) if sequence == 3 else publish, **kwargs)

    monkeypatch.setattr(event_delivery.EventDeliveryCoordinator, 'submit', drop_publication)
    with pytest.raises(AssertionError, match='delivery lost'):
        benchmark.run_sample(event_delivery, lanes=1, events_per_lane=5, persist_ms=0,
                             think_ms=0, burst_every=0, burst_pause_ms=0,
                             cpu_threads=0, idle_reuse=1)

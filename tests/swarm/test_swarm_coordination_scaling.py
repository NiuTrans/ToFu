"""Swarm coordination work per settled agent stays independent of sibling count.

Executable specification for two scaling boundaries:

* ``StreamingScheduler`` readiness is a reverse-dependency index with an unmet
  count, so one completion reads only its own dependents (dependency semantics,
  admission order, cancellation and cycle rejection unchanged);
* ``snapshot_receipts`` reads the session's shared conversation scope once per
  snapshot instead of once per agent.

Decision and measurement: "Swarm settlement scaling" in docs/modules/task_engine.md.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from lib.swarm.planner import AgentDependencyCycleError
from lib.swarm.protocol import SubAgentResult, SubTaskSpec
from lib.swarm.scheduler import StreamingScheduler

pytestmark = pytest.mark.unit


class _CountingSpec(SubTaskSpec):
    """Counts dependency reads: a rescan of every pending spec shows up here."""

    reads = 0

    @property
    def depends_on(self):
        type(self).reads += 1
        return self._depends_on

    @depends_on.setter
    def depends_on(self, value):
        self._depends_on = value


def _scheduler(order=None, release=None):
    def factory(spec):
        def run():
            if release is not None:
                release.wait(5)
            if order is not None:
                order.append(spec.id)
            return SubAgentResult(status='completed', final_answer=spec.id)
        return SimpleNamespace(messages=[], run=run)
    return StreamingScheduler(agent_factory=factory, default_retries=0)


def _drain(scheduler):
    results = [spec.id for spec, _ in scheduler.iter_completions(poll_interval=0.01)]
    scheduler.shutdown(require_quiescence=True)
    return results


def test_chain_readiness_reads_each_dependency_list_a_bounded_number_of_times():
    n = 300
    _CountingSpec.reads = 0
    specs = [_CountingSpec(id=f'a{i}', role='coder', objective=f'step {i}',
                           depends_on=[f'a{i - 1}'] if i else []) for i in range(n)]
    order: list[str] = []
    scheduler = _scheduler(order)
    scheduler.add_specs(specs)
    assert len(_drain(scheduler)) == n
    assert order == [f'a{i}' for i in range(n)]
    # Rescanning pending specs on each completion reads ~n*n/2 lists.
    assert _CountingSpec.reads < 20 * n


def test_fan_in_waits_for_every_prerequisite_and_sees_their_results():
    order: list[str] = []
    scheduler = _scheduler(order)
    roots = [SubTaskSpec(id=f'r{i}', role='coder', objective=f'root {i}') for i in range(5)]
    sink = SubTaskSpec(id='sink', role='coder', objective='merge',
                       depends_on=['r0', 'r1', 'r1', 'r2', 'r3', 'r4'])
    scheduler.add_specs(roots + [sink])
    assert sorted(_drain(scheduler)) == sorted(['r0', 'r1', 'r2', 'r3', 'r4', 'sink'])
    assert order[-1] == 'sink'


def test_unknown_dependency_blocks_until_a_later_wave_settles_it():
    scheduler = _scheduler()
    scheduler.add_specs([SubTaskSpec(id='late', role='coder', objective='needs x',
                                     depends_on=['x'])])
    assert scheduler.pending_count == 1 and scheduler.running_count == 0
    scheduler.add_specs([SubTaskSpec(id='x', role='coder', objective='makes x')])
    assert sorted(_drain(scheduler)) == ['late', 'x']


def test_dependents_released_together_are_submitted_in_insertion_order():
    scheduler = _scheduler()
    submitted: list[str] = []

    def submit_inline(fn, spec):
        submitted.append(spec.id)
        fn(spec)

    scheduler._pool.submit = submit_inline
    scheduler.add_specs([SubTaskSpec(id='root', role='coder', objective='root')]
                        + [SubTaskSpec(id=i, role='coder', objective=f'dep {i}',
                                       depends_on=['root']) for i in ('d3', 'd1', 'd2')])
    assert submitted == ['root', 'd3', 'd1', 'd2']
    assert scheduler.is_idle
    scheduler.shutdown(wait=False)


def test_cancel_pending_drops_blocked_work_and_a_later_completion_starts_nothing():
    release = threading.Event()
    scheduler = _scheduler(release=release)
    scheduler.add_specs([SubTaskSpec(id='root', role='coder', objective='root'),
                         SubTaskSpec(id='child', role='coder', objective='child',
                                     depends_on=['root'])])
    cancelled = scheduler.cancel_pending()
    assert [s.id for s in cancelled] == ['child']
    release.set()
    assert _drain(scheduler) == ['root']
    assert scheduler.pending_count == 0


def test_cycles_are_still_rejected_before_admission():
    scheduler = _scheduler()
    with pytest.raises(AgentDependencyCycleError):
        scheduler.add_specs([
            SubTaskSpec(id='a', role='coder', objective='a', depends_on=['b']),
            SubTaskSpec(id='b', role='coder', objective='b', depends_on=['a']),
        ])
    assert scheduler.pending_count == 0
    scheduler.shutdown(wait=False)


def test_receipt_snapshot_reads_conversation_scope_once_for_all_agents(monkeypatch):
    from lib.swarm import message_receipts, run_messages

    reads = []

    def get(self, conversation_id, *, user_id, include_messages):
        reads.append(conversation_id)
        return SimpleNamespace(metadata={'settings': {'projectPath': ''}})

    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get', get)
    monkeypatch.setattr(run_messages.RunMessageRepository, 'load',
                        lambda self: ({'binding': self.binding, 'closed': False, 'messages': []}, 0))
    specs = [SubTaskSpec(id=f'a{i}', role='coder', objective=f'o{i}') for i in range(50)]
    session = SimpleNamespace(
        task_id='run-1', user_id=7, conv_id='conv-1', inbox_key='conv-1', _agents={},
        _parent_task_proxy={'id': 'run-1', 'convId': 'conv-1', '_userId': 7, 'config': {}})
    agents = [{'id': s.id, 'status': 'running'} for s in specs]
    snapshot = message_receipts.snapshot_receipts(session, agents, specs)
    assert snapshot['messageReceiptsAvailable'] is True
    assert len(snapshot['messageReceiptsVersions']) == 51
    assert reads == ['conv-1']


def test_receipt_snapshot_scope_failure_still_fails_every_target(monkeypatch):
    from lib.swarm import message_receipts, run_messages

    reads = []

    def get(self, conversation_id, *, user_id, include_messages):
        reads.append(conversation_id)
        return SimpleNamespace(metadata={'settings': {'archived': True}})

    loads = []
    monkeypatch.setattr('lib.conversations.repository.ConversationRepository.get', get)
    monkeypatch.setattr(run_messages.RunMessageRepository, 'load',
                        lambda self: loads.append(1))
    failures = []
    monkeypatch.setattr(message_receipts, '_report_snapshot_failure',
                        lambda run_id, signature, failed, total: failures.append((failed, total)))
    specs = [SubTaskSpec(id=f'a{i}', role='coder', objective=f'o{i}') for i in range(5)]
    session = SimpleNamespace(
        task_id='run-1', user_id=7, conv_id='conv-1', inbox_key='conv-1', _agents={},
        _parent_task_proxy={'id': 'run-1', 'convId': 'conv-1', '_userId': 7, 'config': {}})
    snapshot = message_receipts.snapshot_receipts(
        session, [{'id': s.id, 'status': 'running'} for s in specs], specs)
    assert snapshot['messageReceiptsAvailable'] is False
    assert snapshot['messageReceiptsVersions'] == {}
    assert failures == [(6, 6)] and reads == ['conv-1'] and loads == []

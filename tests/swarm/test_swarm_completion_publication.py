"""Canonical completion survives presentation and concurrent result delivery."""
import threading
import uuid

import pytest

from lib import agent_inbox
from lib.swarm import master as module
from lib.swarm.master import MasterOrchestrator
from lib.swarm.types import SubAgentResult, SubTaskSpec

pytestmark = pytest.mark.unit


@pytest.fixture
def master(monkeypatch):
    key = 'completion-' + uuid.uuid4().hex
    spec = SubTaskSpec(id='child', role='researcher', objective='SECRET objective')
    m = MasterOrchestrator(key, key, [spec], user_id=1)
    monkeypatch.setattr(m, '_persist_agent_snapshot', lambda **kw: None)
    yield m
    agent_inbox.drain(key)


@pytest.mark.parametrize('delivery', ['await', 'get', 'inbox'])
def test_delivery_before_enqueue_has_no_late_twin(master, monkeypatch, delivery):
    entered, release = threading.Event(), threading.Event()
    original = module.format_swarm_update

    def blocked_format(**kw):
        entered.set()
        assert release.wait(3)
        return original(**kw)

    monkeypatch.setattr(module, 'format_swarm_update', blocked_format)
    result = SubAgentResult(status='completed', final_answer='evidence')
    thread = threading.Thread(target=master._on_agent_complete_callback,
                              args=(master.specs[0], result))
    thread.start()
    try:
        assert entered.wait(3)
        if delivery == 'await':
            assert master.await_agents(ids=['child'])['completed']
        elif delivery == 'get':
            assert master.get_agent_result('child')['final_answer'] == 'evidence'
        else:
            master.mark_results_delivered(['child', 'unknown'])
        assert master._delivered_result_ids == {'child'}
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert not agent_inbox.drain(master.inbox_key)
    master._on_agent_complete_callback(master.specs[0], result)
    assert len(master._results) == 1
    assert not agent_inbox.drain(master.inbox_key)


def test_raising_sinks_cannot_lose_completion(master, monkeypatch):
    calls = []

    def snapshot(**kw):
        calls.append('snapshot')
        raise RuntimeError('snapshot sink')

    def progress(event):
        # A reentrant sink must not deadlock on the result lock.
        assert master.get_agent_result('child')['status'] == 'completed'
        calls.append('progress')
        raise RuntimeError('UI sink')

    monkeypatch.setattr(master, '_persist_agent_snapshot', snapshot)
    master.on_progress = progress
    master._on_agent_complete_callback(
        master.specs[0], SubAgentResult(status='completed', final_answer='evidence'))
    assert calls == ['snapshot', 'progress']
    assert master._completion_event.is_set()
    assert master.await_agents(ids=['child'])['completed'][0]['result'] == 'evidence'
    assert not agent_inbox.drain(master.inbox_key)


def test_driver_reports_cancelled_stall_without_prompt_leak(master, caplog):
    import logging
    caplog.set_level(logging.INFO, logger='lib.swarm.master')
    class Scheduler:
        running_count = 0
        pending_count = 0
        def started_at_map(self):
            return {}
        def iter_completions(self):
            master._stop_stalled = True
            master._on_agent_complete_callback(
                master.specs[0], SubAgentResult(status='cancelled'))
            return iter(())
        def shutdown(self):
            pass

    frames = []
    master.on_progress = frames.append
    master._scheduler = Scheduler()
    master._start_driver()
    master._driver_thread.join(3)
    assert master.is_terminated
    terminal = [f for f in frames if f.get('phase') == 'complete'][0]
    assert terminal['outcome'] == 'stalled'
    assert terminal['outcomeCounts'] == dict(
        completed=0, failed=0, cancelled=1, stalled=0, unknown=0)
    assert terminal['reasonCodes'] == ['liveness_stall', 'agent_cancelled']
    assert '✅' not in terminal['content']
    assert 'SECRET objective' not in caplog.text
    record = next(r for r in caplog.records
                  if getattr(r, 'tofu_event_name', '') == 'swarm.driver_done')
    assert record.tofu_event_fields['outcome_counts'] == terminal['outcomeCounts']
    assert record.tofu_event_fields['reason_codes'] == terminal['reasonCodes']

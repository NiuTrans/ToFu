"""Stop crosses the real handler/await boundary without waiting for drain."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from lib.swarm.master import MasterOrchestrator
from lib.swarm.scheduler import StreamingScheduler
from lib.swarm.types import SubTaskSpec, SubAgentResult

pytestmark = pytest.mark.unit


def test_handler_stop_interrupts_real_await(monkeypatch):
    from lib.tasks_pkg.handlers.misc import _agents
    from lib.swarm.integration import _tools

    spec = SubTaskSpec(id='child', role='researcher', objective='work')
    master = MasterOrchestrator('parent', 'conv', [spec], user_id=1)
    task = {'id': 'parent', 'convId': 'conv', '_userId': 1, 'status': 'running'}
    monkeypatch.setattr(_tools, '_get_session', lambda key: master)
    monkeypatch.setattr(_tools, 'add_session_alias', lambda *a: None)
    monkeypatch.setattr(_tools, '_cleanup_stale_sessions', lambda: None)
    # Bypass only UI settlement, not the handler's executor or integration.
    def simple_call(task, name, args, *a, **kw):
        return kw['executor'](name, args)
    monkeypatch.setattr(_agents, 'simple_call', simple_call)
    timer = threading.Timer(0.1, lambda: task.update(aborted=True))
    start = time.monotonic()
    timer.start()
    try:
        raw = _agents._handle_swarm_tool(
            task, {}, 'await_agents', 'tc', {'mode': 'all', 'timeout_seconds': 60},
            1, {}, {}, '', False)
    finally:
        timer.cancel()
    result = json.loads(raw)
    assert time.monotonic() - start < 2
    assert result['cancelled'] and not result['timed_out']
    assert master._aborted
    assert not master._terminated  # no driver/drain/finalize required


def test_master_abort_wakes_wait_and_cancels_pending():
    spec = SubTaskSpec(id='child', role='researcher', objective='work')
    master = MasterOrchestrator('parent', 'conv', [spec], user_id=1)
    cancelled = []
    master._scheduler = SimpleNamespace(cancel_pending=lambda: cancelled.append(True))
    timer = threading.Timer(0.1, master.abort)
    timer.start()
    start = time.monotonic()
    try:
        result = master.await_agents(mode='all', timeout_seconds=60)
    finally:
        timer.cancel()
    assert time.monotonic() - start < 2
    assert result['cancelled'] and cancelled


@pytest.mark.parametrize('known_ids', [True, False])
def test_stop_preempts_await_fast_paths(known_ids):
    spec = SubTaskSpec(id='child', role='researcher', objective='work')
    master = MasterOrchestrator('parent', 'conv', [spec], user_id=1)
    # Exercise both cached no-progress and unknown-ID immediate returns.
    ids = ['child'] if known_ids else ['unknown']
    master._await_timeout_progress[('all', tuple(ids))] = frozenset()
    result = master.await_agents(
        ids=ids, mode='all', timeout_seconds=60, abort_check=lambda: True)
    assert result['cancelled'] and not result['timed_out']
    assert master._aborted


def test_cancelled_result_never_retried_or_replaced():
    spec = SubTaskSpec(id='child', role='researcher', objective='work')
    result = SubAgentResult(status='cancelled', total_tokens=123, rounds_used=4,
                            final_answer='partial')
    calls = []
    scheduler = StreamingScheduler(
        agent_factory=lambda s: SimpleNamespace(run=lambda: (calls.append(s.id) or result)),
        default_retries=1)
    try:
        scheduler._run_one(spec)
        assert scheduler._completed[spec.id][1] is result
        assert calls == ['child']
        assert result.total_tokens == 123 and result.rounds_used == 4
        assert 'Previous attempt' not in spec.context
    finally:
        scheduler.shutdown()


def test_spawn_parent_predicate_detaches_after_normal_turn(monkeypatch):
    from lib.swarm.integration import _tools
    captured = {}
    def capture_master(**kwargs):
        captured.update(kwargs)
        raise RuntimeError('stop before launching workers')
    monkeypatch.setattr(_tools, 'MasterOrchestrator', capture_master)
    monkeypatch.setattr(_tools, '_get_session', lambda key: None)
    monkeypatch.setattr(_tools, '_cleanup_stale_sessions', lambda: None)
    monkeypatch.setattr('lib.agent_inbox.untombstone', lambda key: None)
    task = {'id': 'detach-test', 'convId': 'detach-conv', '_userId': 1,
            'status': 'running'}
    _tools.execute_swarm_tool('spawn_agents', {'agents': [
        {'id': 'child', 'role': 'researcher', 'objective': 'work'}]}, task,
        abort_check=lambda: task['status'] != 'running')
    check = captured['abort_check']
    assert not check()
    task['status'] = 'done'
    assert not check()  # background children survive; no ended-turn predicate
    task['aborted'] = True
    assert check()





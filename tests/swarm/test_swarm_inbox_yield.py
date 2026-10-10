"""Cooperative await yields leave inbox delivery and child evidence untouched."""
import json
import uuid

import pytest

from lib import agent_inbox
from lib.swarm.master import MasterOrchestrator
from lib.swarm.types import SubTaskSpec, SubAgentResult

pytestmark = pytest.mark.unit


@pytest.fixture
def wait_context(monkeypatch):
    from lib.tasks_pkg.handlers.misc import _agents
    from lib.swarm.integration import _tools

    key = 'await-inbox-' + uuid.uuid4().hex
    specs = [SubTaskSpec(id=i, role='researcher', objective='work')
             for i in ('done', 'child')]
    master = MasterOrchestrator(key, key, specs, user_id=1)
    master._results_by_id['done'] = (
        specs[0], SubAgentResult(status='completed', final_answer='durable evidence'))
    task = {'id': key, 'convId': key, '_userId': 1, 'status': 'running'}
    monkeypatch.setattr(_tools, '_get_session', lambda key: master)
    monkeypatch.setattr(_tools, 'add_session_alias', lambda *a: None)
    monkeypatch.setattr(_tools, '_cleanup_stale_sessions', lambda: None)
    monkeypatch.setattr(_agents, 'simple_call',
                        lambda task, name, args, *a, **kw: kw['executor'](name, args))

    def invoke():
        return json.loads(_agents._handle_swarm_tool(
            task, {}, 'await_agents', 'tc', {'mode': 'all', 'timeout_seconds': 60},
            1, {}, {}, '', False))

    yield key, master, task, invoke
    agent_inbox.drain(key)


@pytest.mark.parametrize('mode', ['user-steer', 'background-command'])
@pytest.mark.parametrize('during_wait', [False, True])
def test_handler_yields_without_consuming_input_or_results(wait_context, mode, during_wait):
    key, master, task, invoke = wait_context
    waits = []

    def enqueue():
        agent_inbox.enqueue(key, 'ready', priority='next', mode=mode)

    if during_wait:
        # Inject exactly inside the event wait: no scheduling/sleep dependence,
        # and specifically no completion-event notification to carry the input.
        class ArrivalEvent:
            def wait(self, timeout):
                waits.append(timeout)
                enqueue()
            def clear(self):
                pass
        master._completion_event = ArrivalEvent()
    else:
        enqueue()
        master._await_timeout_progress[('all', ('child', 'done'))] = frozenset({'done'})

    result = invoke()
    assert result['status'] == 'ok'
    assert result['yield_reason'] == 'pending_input'
    assert result['completed'] == []
    assert result['still_running'] == ['child']
    from lib.tasks_pkg.handlers.misc._agents import _build_await_post_build
    meta = {}
    _build_await_post_build()(meta, result, {})
    assert meta['awaitYieldReason'] == 'pending_input'
    assert meta['badge'] == 'yielded · pending input'
    assert not result['timed_out'] and not result.get('cancelled')
    assert not master._aborted and not master._terminated
    assert agent_inbox.has_pending(key, modes=[mode])
    assert master._delivered_result_ids == set()
    assert master._results_by_id['done'][1].final_answer == 'durable evidence'
    if during_wait:
        assert waits == [0.1]
    agent_inbox.drain(key)
    reread = master.await_agents(ids=['done'])
    assert reread['completed'][0]['agent_id'] == 'done'


@pytest.mark.parametrize('mode', ['peer-msg', 'swarm-update', 'unrelated'])
def test_unrelated_modes_do_not_yield_or_spin(wait_context, mode):
    key, master, task, invoke = wait_context
    agent_inbox.enqueue(key, 'unrelated', priority='next', mode=mode)
    master._await_timeout_progress[('all', ('child', 'done'))] = frozenset({'done'})
    result = invoke()
    assert result['wait_suppressed'] and result['timed_out']
    assert 'yield_reason' not in result
    assert agent_inbox.has_pending(key, modes=[mode])


def test_badge_counts_full_swarm_roster_on_narrow_await():
    """A narrow ids=['arch'] await must not render '0/1 done' — the badge
    counts the whole swarm roster (swarm_status) so the header can't be
    mistaken for a 1-agent state."""
    from lib.tasks_pkg.handlers.misc._agents import _build_await_post_build
    payload = {
        'status': 'ok',
        'awaited_ids': ['arch'],
        'completed': [],
        'still_running': ['arch'],
        'also_running': ['res-cn', 'res-us'],
        'swarm_status': [
            {'agent_id': 'arch', 'status': 'running'},
            {'agent_id': 'res-us', 'status': 'running'},
            {'agent_id': 'res-cn', 'status': 'running'},
        ],
        'mode': 'any',
        'timed_out': True,
    }
    meta = {}
    _build_await_post_build()(meta, payload, {})
    assert meta['awaitTimedOut'] is True
    assert meta['badge'] == 'timed out · 0/3 done'
    assert meta['awaitStillRunning'] == ['arch']


def test_badge_falls_back_to_awaited_count_without_roster():
    """Older payloads (no swarm_status) keep the awaited-only count."""
    from lib.tasks_pkg.handlers.misc._agents import _build_await_post_build
    payload = {
        'status': 'ok', 'completed': [], 'still_running': ['arch'],
        'mode': 'any', 'timed_out': True,
    }
    meta = {}
    _build_await_post_build()(meta, payload, {})
    assert meta['badge'] == 'timed out · 0/1 done'


def test_stop_has_priority_over_pending_input(wait_context):
    key, master, task, invoke = wait_context
    agent_inbox.enqueue(key, 'ready', priority='next', mode='background-command')
    task['aborted'] = True
    result = invoke()
    assert result['cancelled'] and not result['timed_out']
    assert 'yield_reason' not in result
    assert agent_inbox.has_pending(key)


def test_stop_arriving_in_predicate_has_priority(wait_context):
    key, master, task, invoke = wait_context
    stopped = False
    def pending():
        nonlocal stopped
        stopped = True
        return True
    result = master.await_agents(mode='all', pending_input_check=pending,
                                 abort_check=lambda: stopped)
    assert result['cancelled'] and 'yield_reason' not in result


def test_agent_completion_inside_predicate_returns_real_results(wait_context):
    key, master, task, invoke = wait_context
    def pending():
        master._results_by_id['child'] = (
            SubTaskSpec(id='child', role='researcher', objective='work'),
            SubAgentResult(status='completed', final_answer='finished concurrently'))
        return True
    result = master.await_agents(mode='all', pending_input_check=pending)
    assert 'yield_reason' not in result
    assert {item['agent_id'] for item in result['completed']} == {'done', 'child'}
    assert result['still_running'] == []


def test_headless_omits_inbox_policy(wait_context):
    key, master, task, invoke = wait_context
    task.pop('convId')
    agent_inbox.enqueue(key, 'ready', priority='next', mode='background-command')
    master._await_timeout_progress[('all', ('child', 'done'))] = frozenset({'done'})
    assert invoke()['wait_suppressed']
    assert 'yield_reason' not in master.await_agents(mode='all', timeout_seconds=0)

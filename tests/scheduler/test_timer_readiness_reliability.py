"""Readiness evidence, immutable handoff and durable dispatch obligations."""
from __future__ import annotations

import asyncio
import json
import tracemalloc
import pytest

from lib.scheduler._shared import evaluate_predicate, parse_json_decision
from lib.scheduler.timer import _loop, _poll

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('value', ['false', 'true', 1, 0, None, [], {}])
def test_only_json_booleans_can_trigger(value):
    with pytest.raises(TypeError):
        parse_json_decision(json.dumps({'ready': value}))


@pytest.mark.parametrize('text', ['[]', '{}', 'null', 'true'])
def test_missing_or_nonobject_verdict_is_not_a_decision(text):
    with pytest.raises(TypeError):
        parse_json_decision(text)


@pytest.mark.parametrize('command', [
    'printf READY >&2', 'printf READY; exit 2', 'printf READY; exit 1',
])
def test_regex_never_promotes_stderr_or_failed_evidence(command):
    assert evaluate_predicate(command, 'READY').matched is not True


def test_predicate_runs_in_explicit_project(tmp_path):
    (tmp_path / 'ready').write_text('ready')
    result = evaluate_predicate('test -f ready', owner_user_id=7,
                                project_path=str(tmp_path))
    assert result.matched is True


def test_predicate_output_is_bounded_before_capture():
    tracemalloc.start()
    try:
        result = evaluate_predicate("python3 -c 'import sys; sys.stdout.write(\"x\" * 16777216)'", 'READY')
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.matched is False
    assert len(result.output) <= 4010
    assert peak < 2_000_000, peak


@pytest.mark.parametrize('first', [None, '{"ready":"false"}', '{"ready":false}'])
def test_unchanged_evidence_does_not_hide_recovery_or_new_verdict(monkeypatch, first):
    import lib.scheduler.timer as timer
    import lib.llm_dispatch as llm
    row = dict(id='tmr_recover', user_id=7, status='active', check_instruction='Check readiness',
               check_command='printf static', tools_config={}, condition_kind='llm', poll_count=0)
    monkeypatch.setattr(timer, '_get_timer_row', lambda *a, **k: row)
    monkeypatch.setattr(_poll, '_build_poll_tools', lambda *a, **k: None)
    responses = iter([first, '{"ready":true,"reason":"verified"}'])
    monkeypatch.setattr(llm, 'smart_chat', lambda *a, **k: (next(responses), {'total_tokens': 10}))
    assert timer.poll_timer(row['id'], user_id=7)[0] is False
    row['poll_count'] += 1
    second = timer.poll_timer(row['id'], user_id=7)
    assert second[0] is True and second[3] is False


def _install_loop(monkeypatch, row):
    threads = []
    from lib.scheduler.timer._runtime import TimerWatcherHandle, TimerWatcherRuntime
    runtime = TimerWatcherRuntime()
    class Handle(TimerWatcherHandle):
        def __init__(self, **kwargs):
            super().__init__(runtime=runtime, **kwargs)
            threads.append(self)
        def start(self):
            pass  # The test installs all verdict hooks before running the body.
        def target(self):
            try:
                asyncio.run(self._run())
            finally:
                runtime.shutdown()
    monkeypatch.setattr(_loop, 'TimerWatcherHandle', Handle)
    monkeypatch.setattr(_loop, '_get_timer_row', lambda *a, **k: dict(row))
    monkeypatch.setattr(_loop, 'claim_watcher_lease', lambda *a: 'test-lease')
    releases = []
    monkeypatch.setattr(_loop, 'release_watcher_lease', lambda *a: releases.append(a))
    sleeps = iter([True, True, False])
    async def sleep(*args):
        return next(sleeps)
    monkeypatch.setattr(_loop, '_sleep_with_lease', sleep)
    assert _loop.start_timer_loop(row['id'], user_id=7)
    return threads[0], releases


@pytest.mark.parametrize('resume_ready', [False, True])
def test_ready_survives_busy_lane_and_final_poll_budget(monkeypatch, resume_ready):
    row = dict(id='tmr_final', user_id=7, status='active', poll_interval=10, max_polls=1,
               poll_count=1 if resume_ready else 0,
               last_poll_decision='ready' if resume_ready else 'wait')
    thread, releases = _install_loop(monkeypatch, row)
    polls, dispatches, exhausted = [], [], []
    def poll(*a, **k):
        polls.append(1)
        return True, 'verified', 25, False, False, '', 'cheap', [], ''
    def record(*a, **k):
        row.update(poll_count=1, last_poll_decision='ready')
    def dispatch(current):
        dispatches.append(current)
        return 'task_started' if len(dispatches) == 2 else None
    monkeypatch.setattr(_loop, 'poll_timer', poll)
    monkeypatch.setattr(_loop, '_record_poll', record)
    monkeypatch.setattr(_loop, '_execute_continuation', dispatch)
    monkeypatch.setattr(_loop, '_mark_exhausted', lambda *a, **k: exhausted.append(1))
    thread.target()
    assert len(dispatches) == 2
    assert len(polls) == (0 if resume_ready else 1)
    assert not exhausted
    assert row['id'] not in _loop._active_timers
    assert releases == [(row['id'], 'test-lease')]


def test_storage_fault_cleans_registry_and_releases_lease(monkeypatch):
    row = dict(id='tmr_fault', user_id=7, status='active', poll_interval=10, max_polls=1)
    thread, releases = _install_loop(monkeypatch, row)
    def unavailable(*a, **k):
        raise RuntimeError('storage offline')
    monkeypatch.setattr(_loop, '_get_timer_row', unavailable)
    thread.target()
    assert row['id'] not in _loop._active_timers
    assert releases == [(row['id'], 'test-lease')]


def test_creation_receipt_crosses_normal_tool_result_metadata():
    from lib.scheduler.executor._timer import TimerCreationResult
    from lib.tasks_pkg.handlers.misc._agents import _scheduler_post_build
    receipt = {'timerId': 'tmr_receipt', 'background': True}
    result = TimerCreationResult('watcher created', receipt)
    meta = {}
    _scheduler_post_build(meta, result, {})
    assert str(result) == 'watcher created'
    assert meta['timerReceipt'] == receipt
    assert meta['timerReceipt'] is not receipt


def test_foreign_cancel_does_not_stop_local_watcher(monkeypatch):
    from lib.scheduler.timer import _crud
    class Client:
        def command(self, *a):
            return {'changed': False}
    monkeypatch.setattr(_crud, '_timer_client', lambda **k: Client())
    sentinel = object()
    _loop._active_timers['tmr_foreign'] = sentinel
    try:
        assert _crud.cancel_timer('tmr_foreign', user_id=9) is False
        assert _loop._active_timers['tmr_foreign'] is sentinel
    finally:
        _loop._active_timers.pop('tmr_foreign', None)


def test_cancelled_snapshot_cannot_start_continuation(monkeypatch):
    import lib.scheduler.conversation_dispatch as dispatcher
    monkeypatch.setattr(_loop, '_get_timer_row', lambda *a, **k: {'status': 'cancelled'})
    monkeypatch.setattr(dispatcher, 'dispatch_scheduled_turn',
                        lambda **k: pytest.fail('cancelled watcher dispatched'))
    assert _loop._execute_continuation({'id': 'tmr_cancel', 'user_id': 7}) is None


def test_explicit_search_off_grants_no_search_tools():
    tools = _poll._build_poll_tools({'searchMode': 'off'}, owner_user_id=7)
    names = {tool['function']['name'] for tool in tools}
    assert 'web_search' not in names and 'fetch_url' not in names
    assert 'read_files' in names


def test_sparse_creation_preserves_verifier_scope_and_translation(monkeypatch):
    from lib.scheduler.executor._timer import _execute_timer_create
    import lib.scheduler.timer as timer
    captured = {}
    def create(**kwargs):
        captured.update(kwargs)
        return dict(id='tmr_scope', condition_kind='llm', poll_interval=60, max_polls=120)
    monkeypatch.setattr(timer, 'create_timer', create)
    monkeypatch.setattr(timer, 'start_timer_loop', lambda *a, **k: True)
    config = {'autoTranslate': True, 'uiLang': 'zh', 'browserClientId': 'owned-browser'}
    result = _execute_timer_create({'_source_conv_id':'conv', '_user_id':7,
        '_parent_task':{'config':config}, 'check_instruction':'Verify CI',
        'continuation_message':'Inspect result'})
    assert not result.startswith('Error')
    assert captured['tools_config'] == config
    assert 'searchMode' not in captured['tools_config']


@pytest.mark.parametrize('values', [
    {'continuation_message':'   '}, {'check_instruction':'   '},
    {'max_polls':-1}, {'condition_regex':'['},
])
def test_invalid_definition_never_reaches_storage(monkeypatch, values):
    from lib.scheduler.timer import _crud
    monkeypatch.setattr(_crud, '_timer_client', lambda **k: pytest.fail('invalid definition persisted'))
    arguments = dict(user_id=7, conv_id='conv', check_instruction='Check', continuation_message='Continue')
    arguments.update(values)
    with pytest.raises(ValueError):
        _crud.create_timer(**arguments)


def test_model_tool_loop_stops_when_evidence_does_not_advance(monkeypatch):
    import lib.scheduler.timer as timer
    import lib.llm_dispatch as llm
    row = dict(id='tmr_repeat', user_id=7, status='active', check_instruction='Check readiness',
               check_command='', tools_config={}, condition_kind='llm', poll_count=0)
    monkeypatch.setattr(timer, '_get_timer_row', lambda *a, **k: row)
    tool = {'type':'function', 'function':{'name':'read_files','parameters':{'type':'object'}}}
    monkeypatch.setattr(_poll, '_build_poll_tools', lambda *a, **k: [tool])
    calls = []
    def repeat(*a, **k):
        calls.append(1)
        return None, {'total_tokens':10, '_tool_calls':[
            {'id':str(len(calls)), 'function':{'name':'read_files','arguments':'{}'}}]}
    monkeypatch.setattr(llm, 'smart_chat', repeat)
    monkeypatch.setattr(timer, '_execute_poll_tool', lambda *a, **k: ('still pending',0.01,False))
    result = timer.poll_timer(row['id'], user_id=7)
    assert not result[0] and result[4]
    assert 'no_progress' in result[1]
    assert len(calls) <= 4


def test_cancel_during_poll_prevents_remaining_tool_calls(monkeypatch):
    import lib.scheduler.timer as timer
    import lib.llm_dispatch as llm
    row = dict(id='tmr_stop', user_id=7, status='active', check_instruction='Check readiness',
               check_command='', tools_config={}, condition_kind='llm', poll_count=0)
    monkeypatch.setattr(timer, '_get_timer_row', lambda *a, **k: row)
    def cancel(*a, **k):
        row['status'] = 'cancelled'
        return None, {'total_tokens':10, '_tool_calls':[
            {'id':'call','function':{'name':'read_files','arguments':'{}'}}]}
    monkeypatch.setattr(llm, 'smart_chat', cancel)
    monkeypatch.setattr(timer, '_execute_poll_tool', lambda *a, **k: pytest.fail('tool ran after cancellation'))
    result = timer.poll_timer(row['id'], user_id=7)
    assert not result[0] and result[4]


def test_creation_receipt_survives_normal_handler_settlement(monkeypatch):
    import orjson
    from lib.scheduler.executor._timer import TimerCreationResult
    from lib.tasks_pkg.handlers.misc import _agents
    receipt = {'timerId':'tmr_settled', 'background':True, 'conditionKind':'code',
        'checkInstruction':'', 'conditionCommand':'test -f ready', 'checkCommand':'',
        'pollInterval':60, 'maxPolls':120}
    monkeypatch.setattr(_agents, 'execute_scheduler_tool',
        lambda *a, **k: TimerCreationResult('watcher created', receipt))
    task = {'id':'task_receipt', '_userId':7, 'convId':'conv_receipt',
            'config':{}, '_suppressEvents':True}
    round_entry = {'roundNum':1, 'toolCallId':'call_receipt', 'toolName':'timer_create',
                   'query':'Wait for the readiness file'}
    _agents._handle_scheduler_tool(task, {}, 'timer_create', 'call_receipt', {},
                                  1, round_entry, {}, '', False)
    restored = orjson.loads(orjson.dumps(round_entry))
    assert restored['status'] == 'done'
    assert restored['results'][0]['timerReceipt'] == receipt


def test_resume_does_not_age_expire_a_ready_dispatch_obligation(monkeypatch):
    import lib.scheduler.timer as timer
    row = dict(id='tmr_old_ready', user_id=7, status='active', poll_interval=60,
               max_polls=1, poll_count=1, last_poll_decision='ready',
               created_at='2000-01-01T00:00:00')
    class Client:
        def query(self, *a):
            return [row]
    monkeypatch.setattr(_loop, '_timer_client', lambda **k: Client())
    monkeypatch.setattr('lib.scheduler.timer._poll._mark_expired', lambda *a, **k: pytest.fail('ready obligation expired'))
    resumed = []
    monkeypatch.setattr(timer, 'start_timer_loop', lambda tid, **k: resumed.append(tid) or True)
    assert timer.resume_active_timers() == 1
    assert resumed == [row['id']]


def test_poll_dispatch_carries_owner_and_observes_cancellation(monkeypatch):
    import lib.scheduler.timer as timer
    import lib.llm_dispatch as llm
    row = dict(id='tmr_owned', user_id=41, status='active',
               check_instruction='Check readiness', tools_config={},
               condition_kind='llm', poll_count=0)
    monkeypatch.setattr(timer, '_get_timer_row', lambda *a, **k: row)
    monkeypatch.setattr(_poll, '_build_poll_tools', lambda *a, **k: None)
    def dispatch(messages, **kwargs):
        assert kwargs['owner_user_id'] == 41
        assert kwargs['abort_check']() is False
        row['status'] = 'cancelled'
        assert kwargs['abort_check']() is True
        return '{"ready":false}', {'total_tokens': 1}
    monkeypatch.setattr(llm, 'smart_chat', dispatch)
    assert timer.poll_timer(row['id'], user_id=41)[0] is False

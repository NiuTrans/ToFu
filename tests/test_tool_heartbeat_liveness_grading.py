"""tests/test_tool_heartbeat_liveness_grading.py — heartbeat evidence grading.

WHY (measured incident, 2026-07-31): task 96c56840 hung 2.5h inside a
``run_command`` whose ``grep -rn … ../`` crawled an entire FUSE parent dir.
The stuck-task reaper never fired because the long-tool heartbeat
(``_emit_tool_heartbeat``) refreshed BOTH reaper liveness clocks every 15s —
a self-generated tick that proves "the dispatcher thread is alive", NOT
"the tool is producing". The user's own SSE page kept the zombie looking
healthy: 1050 events in 6202s, almost all self-ticks.

The owner-ratified fix is EVIDENCE GRADING:

  * Human-wait serial tools (``ask_human`` / ``await_task(action=wait)`` /
    ``timer_create`` — the ``_SERIAL_BLOCKING_TOOLS`` table, exemption
    ratified 2026-07-25) keep the old behaviour byte-for-byte: their
    heartbeat ticks refresh ``_dispatch_heartbeat`` and bump
    ``_t_last_event``. A human may legitimately answer days later.
  * Every OTHER tool's heartbeat tick is marked ``_selfTick: True`` on the
    wire event and neither refreshes ``_dispatch_heartbeat`` nor bumps
    ``_t_last_event`` (``append_event`` skips the bump for marked events).
    Real liveness for ordinary tools comes from REAL progress events:
    stdout chunks (``_make_run_command_progress_cb``), tool results,
    deltas, retry phases.
  * Consequence: a silent >30min ordinary tool IS reaped (wedged by
    definition; the frontend stalled card surfaces it earlier), while a
    command that keeps printing stays alive on its own output.

Both directions are pinned here against the REAL reaper:
a fake-hung non-exempt tool with the heartbeat ticking is reaped; a
producing command and a human-wait round are spared.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_tool_heartbeat_liveness_grading.py -v
"""

import threading
import time

import pytest

pytestmark = pytest.mark.unit


def _mk_task(**over):
    t = {
        'id': 'grade-task-1',
        'convId': 'cv-grade-1',
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        'events': [],
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
    }
    t.update(over)
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    with tasks_lock:
        tasks[t['id']] = t
    return t


def _mk_item(fn, status='searching', fn_args=None, tc_id='tc-1'):
    """One parallel_items 7-tuple: (tc, fn_name, tc_id, fn_args, rn, round_entry, parse_err)."""
    round_entry = {'toolCallId': tc_id, 'toolName': fn, 'status': status}
    return (None, fn, tc_id, fn_args if fn_args is not None else {}, 0, round_entry, None)


@pytest.fixture()
def captured_events(monkeypatch):
    """Capture the heartbeat's emitted events (append_event facade stub)."""
    import lib.tasks_pkg.tool_dispatch._heartbeat as tool_dispatch
    events = []
    monkeypatch.setattr(tool_dispatch, 'append_event',
                        lambda task, ev: events.append(ev))
    return events


@pytest.fixture()
def no_db_persist(monkeypatch):
    """Keep the REAL append_event but stub the durable event-log write."""
    monkeypatch.setattr('lib.tasks_pkg.event_log.append_persistent_event',
                        lambda *a, **k: None, raising=True)


# ═════════════════════════════════════════════════════════════════════
#  1. Non-exempt heartbeat: self-tick marker + NO clock refresh
# ═════════════════════════════════════════════════════════════════════

def test_nonexempt_heartbeat_marks_selftick_and_skips_clock(captured_events):
    """web_search is NOT in the human-wait table: its heartbeat tick must be
    marked ``_selfTick`` (transport only) and must NOT refresh
    ``_dispatch_heartbeat`` — otherwise a hung ordinary tool is reap-proof."""
    from lib.tasks_pkg.tool_dispatch._heartbeat import _emit_tool_heartbeat
    task = _mk_task()
    n = _emit_tool_heartbeat(task, [_mk_item('web_search')], time.time() - 12)
    assert n == 1
    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev['type'] == 'tool_progress'
    assert ev.get('_selfTick') is True, (
        'a non-exempt heartbeat tick must declare itself a self-tick — '
        'it proves the dispatcher is alive, not the tool')
    assert task['_dispatch_heartbeat'] == 0.0, (
        'a non-exempt heartbeat refreshed the reaper dispatch clock — '
        'this is the exact immunity that kept zombie 96c56840 alive 2.5h')


def test_nonexempt_heartbeat_tick_does_not_bump_event_clock(no_db_persist):
    """END-TO-END through the REAL append_event: a marked tick must not bump
    ``_t_last_event`` either (both reaper clocks must go stale for a hung
    ordinary tool)."""
    from lib.tasks_pkg.tool_dispatch._heartbeat import _emit_tool_heartbeat
    task = _mk_task()
    stale = time.time() - 2400
    task['_t_last_event'] = stale
    _emit_tool_heartbeat(task, [_mk_item('run_command')], time.time() - 12)
    assert task['_t_last_event'] == stale, (
        'a heartbeat self-tick bumped _t_last_event via the real append_event')


# ═════════════════════════════════════════════════════════════════════
#  2. Exempt human-wait class: ratified immunity preserved byte-for-byte
# ═════════════════════════════════════════════════════════════════════

def test_exempt_ask_human_keeps_clock_refresh_and_unmarked(captured_events):
    """ANCHOR (ratified 2026-07-25): ask_human may wait days — its heartbeat
    still refreshes ``_dispatch_heartbeat`` and its tick stays unmarked."""
    from lib.tasks_pkg.tool_dispatch._heartbeat import _emit_tool_heartbeat
    task = _mk_task()
    t0 = time.time() - 12
    n = _emit_tool_heartbeat(task, [_mk_item('ask_human')], t0)
    assert n == 1
    assert captured_events[0].get('_selfTick') is not True
    assert task['_dispatch_heartbeat'] >= t0


def test_exempt_await_task_wait_vs_status(captured_events):
    """await_task is exempt ONLY when action='wait' (the match callable) —
    a plain status/list call is an ordinary tool and must NOT be protected."""
    from lib.tasks_pkg.tool_dispatch._heartbeat import _emit_tool_heartbeat
    task = _mk_task()
    _emit_tool_heartbeat(task, [_mk_item('await_task', fn_args={'action': 'wait'})],
                         time.time() - 5)
    assert task['_dispatch_heartbeat'] > 0, 'await_task(wait) lost its exemption'
    assert captured_events[0].get('_selfTick') is not True

    task2 = _mk_task()
    _emit_tool_heartbeat(task2, [_mk_item('await_task', fn_args={'action': 'status'})],
                         time.time() - 5)
    assert task2['_dispatch_heartbeat'] == 0.0, (
        'await_task(status) is not a human wait and must not be protected')
    assert captured_events[1].get('_selfTick') is True


def test_mixed_round_human_wait_keeps_round_alive(captured_events):
    """A round containing a LIVE human-wait tool is alive by design: the
    exempt member's tick stays unmarked (bumps the event clock), the hung
    ordinary member's tick is marked."""
    from lib.tasks_pkg.tool_dispatch._heartbeat import _emit_tool_heartbeat
    task = _mk_task()
    items = [_mk_item('ask_human', tc_id='tc-h'),
             _mk_item('web_search', tc_id='tc-w')]
    _emit_tool_heartbeat(task, items, time.time() - 5)
    by_tc = {ev['toolCallId']: ev for ev in captured_events}
    assert by_tc['tc-h'].get('_selfTick') is not True
    assert by_tc['tc-w'].get('_selfTick') is True


# ═════════════════════════════════════════════════════════════════════
#  3. append_event grading seam
# ═════════════════════════════════════════════════════════════════════

def test_append_event_grading_marked_vs_unmarked(no_db_persist):
    """The REAL append_event: an event carrying ``_selfTick`` must not bump
    ``_t_last_event``; any other event bumps it (deltas, chunks, results)."""
    from lib.tasks_pkg.manager import append_event
    task = _mk_task()
    task['_t_last_event'] = 123.0
    append_event(task, {'type': 'tool_progress', '_selfTick': True,
                        'roundNum': 0, 'toolCallId': 'tc-1'})
    assert task['_t_last_event'] == 123.0, 'self-tick bumped the event clock'
    append_event(task, {'type': 'tool_progress', 'roundNum': 0,
                        'toolCallId': 'tc-1', 'stream': 'stdout', 'chunk': 'x'})
    assert task['_t_last_event'] > 123.0, 'a real progress event must bump the clock'


def test_run_command_stdout_chunk_is_real_evidence(no_db_persist):
    """``_make_run_command_progress_cb`` (real stdout) emits UNMARKED
    tool_progress — a command that keeps printing proves its own life."""
    from lib.tasks_pkg.handlers.code_exec import _make_run_command_progress_cb
    task = _mk_task()
    task['_t_last_event'] = 0.0
    round_entry = {'toolCallId': 'tc-rc', 'toolName': 'run_command', 'status': 'executing'}
    cb = _make_run_command_progress_cb(task, 0, round_entry, 'grep -rn x .')
    cb('stdout', 'some real output line\n')
    cb.flush()
    prog = [e for e in task['events'] if e.get('type') == 'tool_progress']
    assert prog, 'no progress event emitted for real stdout'
    assert all(e.get('_selfTick') is not True for e in prog), (
        'real stdout chunks must NOT be marked as self-ticks')
    assert task['_t_last_event'] > 0.0, (
        'real stdout did not bump the event clock — a producing command '
        'would be reaped at 30 min (false reap of a healthy long command)')


# ═════════════════════════════════════════════════════════════════════
#  4. Reaper end-to-end — both directions
# ═════════════════════════════════════════════════════════════════════

def _reaper_harness(monkeypatch, task):
    import lib.tasks_pkg.manager._maintenance as _maintenance
    from tests.support.chat_tasks import chat_task_registry
    monkeypatch.setattr(_maintenance, '_stuck_task_max_silent_secs',
                        lambda: 1800, raising=True)
    monkeypatch.setattr(_maintenance, '_finalize_reaped_stuck_task',
                        lambda t: None, raising=True)
    monkeypatch.setitem(chat_task_registry, task['id'], task)
    return _maintenance


def test_reaper_reaps_hung_nonexempt_tool_with_heartbeat_ticking(monkeypatch, no_db_persist):
    """THE incident shape: a task wedged 40min inside run_command while the
    heartbeat ticks every 15s. With grading, the ticks prove nothing — the
    reaper must fire."""
    from lib.tasks_pkg.tool_dispatch._heartbeat import _emit_tool_heartbeat
    now = time.time()
    task = _mk_task(created_at=now - 2400,
                    _t_last_event=now - 2400,
                    _dispatch_heartbeat=now - 2400)
    _maintenance = _reaper_harness(monkeypatch, task)
    # The heartbeat ticks (non-exempt tool) — then the reaper runs.
    _emit_tool_heartbeat(task, [_mk_item('run_command')], now - 2400)
    reaped = _maintenance.reap_stuck_running_tasks()
    assert reaped == 1, (
        'a task silent 40min with only heartbeat self-ticks was NOT reaped — '
        'the self-licking immunity is still in place')
    assert task.get('_abort_reason') == 'stuck_no_progress'


def test_reaper_spares_exempt_human_wait(monkeypatch, no_db_persist):
    """ANCHOR: the same doubly-stale task, but the in-flight tool is
    ask_human — the ratified human-wait exemption must spare it."""
    from lib.tasks_pkg.tool_dispatch._heartbeat import _emit_tool_heartbeat
    now = time.time()
    task = _mk_task(created_at=now - 2400,
                    _t_last_event=now - 2400,
                    _dispatch_heartbeat=now - 2400)
    _maintenance = _reaper_harness(monkeypatch, task)
    _emit_tool_heartbeat(task, [_mk_item('ask_human')], now - 2400)
    reaped = _maintenance.reap_stuck_running_tasks()
    assert reaped == 0, 'a live human-wait round was reaped — ratified exemption broken'


def test_reaper_spares_producing_command(monkeypatch, no_db_persist):
    """The healthy-long-command direction (pt_9f5a51ba's two 1846s tasks):
    stdout chunks keep ``_t_last_event`` fresh — the reaper stands down."""
    from lib.tasks_pkg.handlers.code_exec import _make_run_command_progress_cb
    now = time.time()
    task = _mk_task(created_at=now - 2400,
                    _t_last_event=now - 2400,
                    _dispatch_heartbeat=now - 2400)
    _maintenance = _reaper_harness(monkeypatch, task)
    round_entry = {'toolCallId': 'tc-rc', 'toolName': 'run_command', 'status': 'executing'}
    cb = _make_run_command_progress_cb(task, 0, round_entry, 'pytest -x')
    cb('stdout', '=== 412 passed ===\n')
    cb.flush()
    reaped = _maintenance.reap_stuck_running_tasks()
    assert reaped == 0, (
        'a command still producing output was reaped — false reap of a '
        'healthy long command (the pt_9f5a51ba regression shape)')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

"""tests/test_tool_heartbeat.py — the long-running-tool heartbeat (item 3).

A single blocking tool (a slow web_search on dead hosts, a hung MCP call, a
stalled browser action) emits NO delta while it runs. Without a heartbeat the
SSE stream goes silent — a buffering proxy idle-times-out AND both reaper
liveness clocks (``_t_last_event`` / ``_dispatch_heartbeat``) go stale, so the
generalized reaper would FALSE-REAP a tool that is genuinely alive but slow.

``_emit_tool_heartbeat`` (lib/tasks_pkg/tool_dispatch.py) fixes that: while the
parallel-tool wait blocks, a daemon ticker calls it every
``TOOL_HEARTBEAT_INTERVAL`` seconds. Each tick (a) refreshes
``_dispatch_heartbeat`` and (b) emits a ``tool_progress`` per still-in-flight
round — which bumps ``_t_last_event`` via ``append_event`` and keeps the stream
non-silent so the UI shows "Searching… (Ns)".

These tests drive the module-level tick helper directly against a synthetic
task + parallel_items, capturing emitted events via a stubbed ``append_event``.
The NEUTER-BITE sibling proves the tick is load-bearing: a round that has
already settled (status='done') must NOT be pinged, so a heartbeat fired after
settle emits nothing (mirrors the finally-stop guard).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_tool_heartbeat.py -v
"""

import threading
import time

import pytest

pytestmark = pytest.mark.unit


def _mk_task(**over):
    t = {
        'id': 'hb-task-1',
        'convId': 'cv-hb-1',
        'status': 'running',
        'aborted': False,
        'events': [],
        'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0,
    }
    t.update(over)
    return t


def _mk_items(status='searching', fn='web_search'):
    """One parallel_items 7-tuple: (tc, fn_name, tc_id, fn_args, rn, round_entry, parse_err)."""
    round_entry = {'toolCallId': 'tc-1', 'toolName': fn, 'status': status}
    return [(None, fn, 'tc-1', {}, 0, round_entry, None)]


@pytest.fixture()
def captured_events(monkeypatch):
    """Capture every append_event(task, event) the tick emits."""
    from lib.tasks_pkg import tool_dispatch
    events = []
    monkeypatch.setattr(tool_dispatch, 'append_event',
                        lambda task, ev: events.append(ev))
    return events


def test_heartbeat_emits_progress_and_refreshes_clocks(captured_events):
    from lib.tasks_pkg.tool_dispatch import _emit_tool_heartbeat
    task = _mk_task()
    items = _mk_items(status='searching')
    t0 = time.time() - 12  # pretend the tool has been running 12s

    n = _emit_tool_heartbeat(task, items, t0)

    assert n == 1, 'an in-flight round must get one progress ping'
    assert len(captured_events) == 1
    ev = captured_events[0]
    assert ev['type'] == 'tool_progress'
    assert ev['toolCallId'] == 'tc-1'
    assert ev['elapsed'] >= 12
    assert '(' in ev['detail'] and 's)' in ev['detail']  # "Searching… (12s)"
    # The positive-liveness clock was refreshed to ~now.
    assert task['_dispatch_heartbeat'] >= t0 + 10


def test_heartbeat_skips_settled_round(captured_events):
    """NEUTER-BITE: a round that already finalized (status='done') must NOT be
    pinged — this is what prevents a heartbeat racing past settle from
    resurrecting a completed round. If the status gate were removed, this would
    emit a spurious progress event for a done round."""
    from lib.tasks_pkg.tool_dispatch import _emit_tool_heartbeat
    task = _mk_task()
    items = _mk_items(status='done')  # already settled
    n = _emit_tool_heartbeat(task, items, time.time() - 5)
    assert n == 0, 'a settled round must not receive a heartbeat ping'
    assert captured_events == []


def test_heartbeat_noop_when_aborted(captured_events):
    """An aborted task emits no progress (but still refreshes the clock so the
    reaper sees the abort winding down, not a wedge)."""
    from lib.tasks_pkg.tool_dispatch import _emit_tool_heartbeat
    task = _mk_task(aborted=True)
    items = _mk_items(status='searching')
    n = _emit_tool_heartbeat(task, items, time.time() - 5)
    assert n == 0
    assert captured_events == []


def test_heartbeat_thread_ticks_then_stops(captured_events):
    """Integration: the daemon ticker fires while blocking, then stops cleanly
    on stop.set(). Uses a tiny interval so the test is fast."""
    import os
    from lib.tasks_pkg.tool_dispatch import _start_tool_heartbeat
    os.environ['TOOL_HEARTBEAT_INTERVAL'] = '2'  # clamped floor is 2s
    try:
        task = _mk_task()
        items = _mk_items(status='searching')
        stop, thread = _start_tool_heartbeat(task, items, 'hb-task-1')
        # Wait long enough for at least one tick (interval=2s).
        time.sleep(2.5)
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive(), 'ticker thread must stop on stop.set()'
        assert len(captured_events) >= 1, 'at least one heartbeat tick expected'
        assert all(e['type'] == 'tool_progress' for e in captured_events)
    finally:
        os.environ.pop('TOOL_HEARTBEAT_INTERVAL', None)

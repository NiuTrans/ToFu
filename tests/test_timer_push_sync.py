"""Regression guards for push-first timer projection synchronization."""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[1]
TIMER_JS = pathlib.Path(runtime_section_path('timer.js'))


def test_timer_invalidation_is_identifier_free(monkeypatch):
    calls = []
    import lib.agent_core.push as push

    monkeypatch.setattr(push, 'push_event',
                        lambda channel, task_id, payload: calls.append(
                            (channel, task_id, payload)))
    from lib.scheduler.timer._notify import notify_timer_changed

    notify_timer_changed('created')
    assert calls == [('timer', '*', {
        'type': 'timer_changed',
        'change': 'created',
    })]


def test_timer_invalidation_never_breaks_durable_write(monkeypatch):
    import lib.agent_core.push as push
    from lib.scheduler.timer._notify import notify_timer_changed

    def broken_push(*_args, **_kwargs):
        raise RuntimeError('socket unavailable')

    monkeypatch.setattr(push, 'push_event', broken_push)
    # The notification seam is explicitly best-effort: callers invoke it only
    # after commit, and a transport failure must not change their result.
    notify_timer_changed('triggered')


def test_timer_frontend_uses_push_first_deduplicated_reconciliation():
    src = TIMER_JS.read_text()

    assert 'pushSubscribe("timer", "*"' in src
    assert 'frame.type !== "timer_changed"' in src
    assert 'frame.change === "progress" && !_timerPanelOpen' in src
    assert '_timerListInFlight.request.then(() => _fetchTimerList(false))' in src
    assert 'Api.timer.list(summaryOnly)' in src
    assert 'await _fetchTimerList(true)' in src
    assert 'await _fetchTimerList(false)' in src
    assert '!!data.has_timers' in src
    assert '_timerRefreshAfterFlight = true' in src
    assert 'document.visibilityState === "hidden"' in src
    assert 'typeof pushIsConnected !== "function" || !pushIsConnected()' in src

    interval = src[src.index('function _startTimerPolling()'):
                   src.index('async function _refreshTimerBadge()')]
    assert interval.count('_refreshTimerPanel()') == 1
    assert interval.count('_refreshTimerBadge()') == 1
    assert interval.index('_refreshTimerPanel()') < interval.index(
        '_refreshTimerBadge()')


def test_summary_flight_is_promoted_when_panel_opens():
    """A badge summary in flight must never paint the full panel as empty."""
    source = TIMER_JS.read_text()
    setup = r'''
const calls = [];
const resolvers = [];
global.document = {
  visibilityState: 'visible',
  addEventListener() {},
  getElementById() { return null; },
};
global.window = global;
global.setInterval = () => 1;
global.setTimeout = () => 1;
global.Api = { timer: { list(summaryOnly) {
  calls.push(summaryOnly);
  return new Promise((resolve) => resolvers.push(resolve));
}}};
'''
    driver = r'''
(async () => {
  const summary = _fetchTimerList(true);
  const full = _fetchTimerList(false);
  await Promise.resolve();
  resolvers[0]({ ok: true, has_timers: true, active_count: 0 });
  await summary;
  await Promise.resolve();
  resolvers[1]({ ok: true, timers: [{ id: 't1' }], active_count: 0 });
  const result = await full;
  process.stdout.write(JSON.stringify({ calls, result }));
})().catch((error) => { console.error(error); process.exit(1); });
'''
    proc = subprocess.run(
        ['node'], input=setup + '\n' + source + '\n' + driver, text=True,
        capture_output=True, timeout=20, check=False)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result['calls'] == [True, False]
    assert result['result']['timers'] == [{'id': 't1'}]


def test_every_timer_projection_writer_emits_after_durable_write():
    cases = [
        (ROOT / 'lib/scheduler/timer/_crud.py', "notify_timer_changed('created')"),
        (ROOT / 'lib/scheduler/timer/_crud.py', "notify_timer_changed('cancelled')"),
        (ROOT / 'lib/scheduler/timer/_poll.py', "notify_timer_changed('progress')"),
        (ROOT / 'lib/scheduler/timer/_poll.py', "notify_timer_changed('exhausted')"),
        (ROOT / 'lib/scheduler/timer/_poll.py', "notify_timer_changed('expired')"),
        (ROOT / 'lib/scheduler/timer/_poll.py', "notify_timer_changed('orphaned')"),
        (ROOT / 'lib/scheduler/timer/_loop.py', "notify_timer_changed('triggered')"),
        (ROOT / 'lib/scheduler/executor/_timer.py', "notify_timer_changed('triggered')"),
    ]
    for path, needle in cases:
        src = path.read_text()
        pos = src.index(needle)
        boundary = max(
            src.rfind('db_execute_with_retry(', 0, pos),
            src.rfind('write_transaction(', 0, pos),
        )
        assert boundary >= 0, (
            f'{path.name}: {needle} must follow a data-layer-owned write')
        assert pos - boundary < 1200, (
            f'{path.name}: notification drifted from durable write')

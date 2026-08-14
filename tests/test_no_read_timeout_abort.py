"""tests/test_no_read_timeout_abort.py — the "no timeouts, I stop it myself" contract.

WHY
---
Owner ruling (2026-07-29): an LLM request must have NO first-byte timeout and
NO read timeout. "Unless it crashes, what is there that can't be waited for?
If I can't wait, I will naturally pause it myself."

Removing those timeouts is only SAFE because of two things that had to be
built at the same time, and this suite pins both:

  1. **Abort reaches a silent socket.** ``abort_check`` was only ever
     evaluated INSIDE the SSE line loop (``for line in resp.iter_lines()``).
     On a zero-byte hang that loop never iterates, so a Stop was never
     observed — the 300s read timeout was the only thing that ever ended
     such an attempt. Measured pre-fix with the watchdog disabled: Stop
     pressed at t=1.0s, request kept hanging (6.2s, bounded only by the
     fake's own guard). ``StreamIdleWatchdog`` now polls the SAME predicate
     every ABORT_POLL_INTERVAL and closes the response.

  2. **Idle heartbeat survives the first byte.** The old watchdog DISARMED
     on the first SSE line (``notify_first_byte``). That was fine while a
     300s read timeout existed, because a mid-stream stall got interrupted
     and retried, which produced events. With no read timeout, a
     post-first-byte silence is unbounded and emits nothing — so BOTH
     reaper liveness clocks (``_t_last_event`` / ``_dispatch_heartbeat``)
     go stale and ``reap_stuck_running_tasks`` force-fails the task at 30
     min with a "terminated as wedged" error bubble. The reaper would have
     become the new timeout, killing exactly the long waits we just made
     legal. ``notify_activity`` now RESETS the idle clock instead of
     disarming, and ``_on_waiting`` bumps ``_dispatch_heartbeat``.

The design rule: **aliveness is proven by BEATING, never by not-timing-out.**
A beating-but-slow task is never reaped; a genuinely dead worker emits no
beats and is still reaped, so the reaper keeps its real job.

Run:  pytest tests/test_no_read_timeout_abort.py -m unit
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ci_serial: the reaper-clock assertions measure heartbeat vs wall-clock
# deltas with <5s budgets — under the CI parallel lane's CPU starvation the
# measured gap blew past 30s (7a4c727 unit leg) while passing everywhere
# uncontended. The serial lane runs it alone with a 600s timeout.
# (Module-level mark is additive to the per-class unit marks.)
pytestmark = [pytest.mark.unit, pytest.mark.ci_serial]


# ═════════════════════════════════════════════════════════════════════
#  A. No timeout constants / no time-based kill remain
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNoTimeoutsRemain:
    def test_ttft_timeout_constant_is_gone(self):
        """The first-byte KILL must not exist in any form — not even as a
        disabled-by-default constant someone could re-enable."""
        import lib.llm._transport as tp
        assert not hasattr(tp, 'TTFT_TIMEOUT')
        assert not hasattr(tp, 'FirstByteWatchdog')

    def test_first_byte_timeout_error_is_gone(self):
        import lib.llm_errors as le
        assert not hasattr(le, 'FirstByteTimeoutError')
        import lib.llm as llm
        assert not hasattr(llm, 'FirstByteTimeoutError')

    def test_sync_post_passes_no_read_timeout(self, monkeypatch):
        """The requests POST must send ``timeout=(connect, None)``. Pinning
        the ACTUAL kwarg, not the source text — a source scan would pass on
        a comment saying 'no read timeout'."""
        captured = {}

        class _Resp:
            headers = {}
            status_code = 200
            encoding = None

            def iter_lines(self, decode_unicode=False):
                yield 'data: {"id":"x","choices":[{"delta":{"content":"hi"},"index":0}]}'
                yield 'data: [DONE]'

            def close(self):
                pass

        class _Sess:
            def post(self, *a, **k):
                captured.update(k)
                return _Resp()

        monkeypatch.setattr('lib.llm.stream.get_sync_session', lambda: _Sess())
        from lib.llm.stream import _stream_chat_once
        _stream_chat_once({'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
                          api_key='sk-x', base_url='http://fake.local/v1')
        assert 'timeout' in captured, 'timeout kwarg not passed at all'
        _connect, _read = captured['timeout']
        assert _read is None, f'read timeout must be None, got {_read!r}'
        assert _connect > 0, 'connect timeout must stay bounded (crash detection)'

    def test_async_client_has_no_read_timeout(self):
        """httpx read timeout must be None on the pooled client."""
        import lib.llm._transport as tp
        tp.reset_pools_for_test()
        try:
            async def _mk():
                return tp.get_async_client(None)
            client = asyncio.new_event_loop().run_until_complete(_mk())
            assert client.timeout.read is None, \
                f'httpx read timeout must be None, got {client.timeout.read!r}'
            assert client.timeout.connect and client.timeout.connect > 0
        finally:
            tp.reset_pools_for_test()

    def test_non_stream_chat_defaults_to_no_read_timeout(self):
        import inspect
        from lib.llm import chat
        assert inspect.signature(chat).parameters['timeout'].default is None

    def test_dispatch_chat_has_no_total_budget(self):
        """The shared-deadline budget that truncated slow non-streaming calls
        must be gone (it silently produced incomplete translations)."""
        import inspect
        from lib.llm_dispatch.api import dispatch_chat
        src = inspect.getsource(dispatch_chat)
        assert '_deadline' not in src, 'dispatch_chat still enforces a deadline'
        assert '_total_budget' not in src


# ═════════════════════════════════════════════════════════════════════
#  B. StreamIdleWatchdog unit — abort poll + idle beats, no kill
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestStreamIdleWatchdogUnit:
    def test_abort_poll_fires_on_abort(self):
        from lib.llm._transport import StreamIdleWatchdog
        flag = {'v': False}
        fired = []
        wd = StreamIdleWatchdog(abort_check=lambda: flag['v'],
                                on_abort=lambda: fired.append(1))
        wd.start()
        time.sleep(0.2)
        assert fired == [], 'abort fired without the flag being set'
        flag['v'] = True
        time.sleep(1.2)
        assert fired == [1]
        assert wd.aborted is True
        wd.cancel()

    def test_never_kills_on_time_alone(self):
        """No abort, no activity — the watchdog must NEVER end the attempt.
        This is the whole point: a wait is not a failure."""
        from lib.llm._transport import StreamIdleWatchdog
        beats = []
        wd = StreamIdleWatchdog(heartbeat_interval=0.05,
                                on_beat=lambda idle: beats.append(idle),
                                abort_check=lambda: False)
        wd.start()
        time.sleep(0.5)
        assert wd.aborted is False, 'watchdog aborted a healthy silent wait'
        assert len(beats) >= 3, 'idle beats did not fire'
        wd.cancel()

    def test_activity_resets_idle_clock_but_keeps_watching(self):
        """notify_activity must NOT disarm: after activity stops, beats
        resume. The old notify_first_byte() killed the watchdog for good,
        which is what would have let the reaper eat a mid-stream stall."""
        from lib.llm._transport import StreamIdleWatchdog
        beats = []
        wd = StreamIdleWatchdog(heartbeat_interval=0.10,
                                on_beat=lambda idle: beats.append(idle),
                                abort_check=lambda: False)
        wd.start()
        # Keep it "active" for ~0.3s — no beat should fire.
        for _ in range(6):
            wd.notify_activity()
            time.sleep(0.05)
        assert beats == [], f'beat fired while stream was active: {beats}'
        # Now go silent — beats must RESUME (proves no disarm).
        time.sleep(0.45)
        wd.cancel()
        assert len(beats) >= 2, \
            'beats did not resume after activity stopped — watchdog disarmed'

    def test_abort_still_polled_after_activity(self):
        """Abort must remain live for the WHOLE attempt, including after the
        first byte — a mid-stream stall is just as unbounded now."""
        from lib.llm._transport import StreamIdleWatchdog
        flag = {'v': False}
        fired = []
        wd = StreamIdleWatchdog(abort_check=lambda: flag['v'],
                                on_abort=lambda: fired.append(1))
        wd.start()
        wd.notify_activity()
        time.sleep(0.1)
        flag['v'] = True
        time.sleep(1.2)
        assert fired == [1], 'abort not honored after stream activity'
        wd.cancel()

    def test_beat_exception_does_not_kill_the_watchdog(self):
        from lib.llm._transport import StreamIdleWatchdog
        flag = {'v': False}
        fired = []

        def _bad_beat(_idle):
            raise RuntimeError('boom')

        wd = StreamIdleWatchdog(heartbeat_interval=0.04, on_beat=_bad_beat,
                                abort_check=lambda: flag['v'],
                                on_abort=lambda: fired.append(1))
        wd.start()
        time.sleep(0.2)
        flag['v'] = True
        time.sleep(1.2)
        assert fired == [1], 'a raising on_beat took the abort poll down'
        wd.cancel()


# ═════════════════════════════════════════════════════════════════════
#  C. Sync transport — Stop during a zero-byte hang returns FAST
#     (the measured headline number)
# ═════════════════════════════════════════════════════════════════════

class _WedgedResp:
    """200 OK then ZERO body bytes — the incident shape. The 15s guard
    stands in for 'no read timeout': without the abort poll the test must
    FAIL (slow), not hang the suite forever."""

    def __init__(self):
        self.headers = {}
        self.status_code = 200
        self.encoding = None
        self._closed = threading.Event()

    def iter_lines(self, decode_unicode=False):
        self._closed.wait(timeout=15)
        raise ValueError('I/O operation on closed file')
        yield  # pragma: no cover

    def close(self):
        self._closed.set()


class _StallThenFinishResp:
    """Delivers one byte, STALLS, then finishes — the post-first-byte
    silence that has no read timeout to interrupt it any more."""

    def __init__(self, stall=0.6):
        self.headers = {}
        self.status_code = 200
        self.encoding = None
        self._stall = stall
        self._closed = threading.Event()

    def iter_lines(self, decode_unicode=False):
        yield 'data: {"id":"x","choices":[{"delta":{"content":"hi"},"index":0}]}'
        if self._closed.wait(timeout=self._stall):
            raise ValueError('I/O operation on closed file')
        yield 'data: [DONE]'

    def close(self):
        self._closed.set()


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def post(self, *a, **k):
        return self._resp


@pytest.mark.unit
class TestSyncAbortDuringSilence:
    def test_stop_during_zero_byte_hang_returns_within_a_second(self, monkeypatch):
        """THE headline behaviour. Pre-fix (measured): Stop at t=1.0s, the
        request kept hanging 6.2s. Post-fix it must unblock ~immediately."""
        from lib.llm_errors import AbortedError
        monkeypatch.setattr('lib.llm.stream.get_sync_session',
                            lambda: _FakeSession(_WedgedResp()))
        monkeypatch.setattr('lib.llm._transport.ABORT_POLL_INTERVAL', 0.05)
        monkeypatch.setattr('lib.llm._transport.IDLE_HEARTBEAT_S', 0.05)

        aborted = {'v': False}
        threading.Timer(0.3, lambda: aborted.__setitem__('v', True)).start()

        from lib.llm.stream import _stream_chat_once
        t0 = time.monotonic()
        with pytest.raises(AbortedError):
            _stream_chat_once(
                {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
                abort_check=lambda: aborted['v'],
                api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')
        elapsed = time.monotonic() - t0
        # Stop landed at 0.3s; must return well before the fake's 15s guard.
        assert elapsed < 2.0, (
            f'Stop was not honored promptly during a zero-byte hang '
            f'({elapsed:.1f}s) — the abort poll is not load-bearing')

    def test_silent_wait_is_NOT_killed_and_keeps_beating(self, monkeypatch):
        """No abort → the attempt must NOT be ended by any timer, and beats
        must keep coming so the reaper's clocks stay fresh."""
        monkeypatch.setattr('lib.llm._transport.IDLE_HEARTBEAT_S', 0.05)
        monkeypatch.setattr('lib.llm._transport.ABORT_POLL_INTERVAL', 0.05)
        resp = _StallThenFinishResp(stall=0.5)
        monkeypatch.setattr('lib.llm.stream.get_sync_session',
                            lambda: _FakeSession(resp))
        beats = []
        from lib.llm.stream import _stream_chat_once
        msg, finish, usage = _stream_chat_once(
            {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
            abort_check=lambda: False,
            on_first_byte_wait=beats.append,
            api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')
        # The stream completed despite a 0.5s mid-stream stall — nothing killed it.
        assert 'hi' in (msg.get('content') or '')
        # And the stall produced beats AFTER the first byte (the old watchdog
        # disarmed here and would produce none).
        assert beats, 'no idle beat during the post-first-byte stall'

    def test_fast_stream_produces_no_beats(self, monkeypatch):
        class _FastResp:
            headers = {}
            status_code = 200
            encoding = None

            def iter_lines(self, decode_unicode=False):
                yield 'data: {"id":"x","choices":[{"delta":{"content":"hi"},"index":0}]}'
                yield 'data: [DONE]'

            def close(self):
                pass

        monkeypatch.setattr('lib.llm._transport.IDLE_HEARTBEAT_S', 5.0)
        monkeypatch.setattr('lib.llm.stream.get_sync_session',
                            lambda: _FakeSession(_FastResp()))
        beats = []
        from lib.llm.stream import _stream_chat_once
        msg, _f, _u = _stream_chat_once(
            {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
            on_first_byte_wait=beats.append,
            api_key='sk-x', base_url='http://fake.local/v1')
        assert 'hi' in (msg.get('content') or '')
        assert beats == [], 'a fast stream must not emit idle beats'


# ═════════════════════════════════════════════════════════════════════
#  D. Async transport — same two properties
# ═════════════════════════════════════════════════════════════════════

class _AsyncWedgedResp:
    def __init__(self):
        self.headers = {}
        self.status_code = 200
        self._closed = asyncio.Event()

    async def aclose(self):
        self._closed.set()

    async def aiter_lines(self):
        await asyncio.wait_for(self._closed.wait(), timeout=15)
        raise RuntimeError('stream closed')
        yield  # pragma: no cover


class _AsyncStreamCM:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeAsyncClient:
    def __init__(self, resp):
        self._resp = resp
        self.is_closed = False

    def stream(self, *a, **k):
        return _AsyncStreamCM(self._resp)


@pytest.mark.unit
class TestAsyncAbortDuringSilence:
    def test_stop_during_zero_byte_hang_returns_fast(self, monkeypatch):
        from lib.llm_errors import AbortedError

        async def _main():
            resp = _AsyncWedgedResp()
            monkeypatch.setattr('lib.llm.astream.get_async_client',
                                lambda _p: _FakeAsyncClient(resp))
            monkeypatch.setattr('lib.llm._transport.ABORT_POLL_INTERVAL', 0.05)
            monkeypatch.setattr('lib.llm._transport.IDLE_HEARTBEAT_S', 0.05)
            aborted = {'v': False}

            async def _press_stop():
                await asyncio.sleep(0.3)
                aborted['v'] = True

            asyncio.ensure_future(_press_stop())
            from lib.llm.astream import _async_stream_chat_once
            t0 = time.monotonic()
            with pytest.raises(AbortedError):
                await _async_stream_chat_once(
                    {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
                    abort_check=lambda: aborted['v'],
                    api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')
            elapsed = time.monotonic() - t0
            assert elapsed < 2.0, (
                f'async Stop not honored promptly ({elapsed:.1f}s)')

        asyncio.new_event_loop().run_until_complete(_main())

    def test_async_silent_wait_beats_and_is_not_killed(self, monkeypatch):
        class _AsyncStallResp:
            headers = {}
            status_code = 200

            async def aclose(self):
                pass

            async def aiter_lines(self):
                yield 'data: {"id":"x","choices":[{"delta":{"content":"hi"},"index":0}]}'
                await asyncio.sleep(0.4)          # post-first-byte stall
                yield 'data: [DONE]'

        async def _main():
            monkeypatch.setattr('lib.llm.astream.get_async_client',
                                lambda _p: _FakeAsyncClient(_AsyncStallResp()))
            monkeypatch.setattr('lib.llm._transport.IDLE_HEARTBEAT_S', 0.05)
            monkeypatch.setattr('lib.llm._transport.ABORT_POLL_INTERVAL', 0.05)
            beats = []
            from lib.llm.astream import _async_stream_chat_once
            msg, _f, _u = await _async_stream_chat_once(
                {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
                abort_check=lambda: False,
                on_first_byte_wait=beats.append,
                api_key='sk-x', base_url='http://fake.local/v1', log_prefix='[t]')
            assert 'hi' in (msg.get('content') or '')
            assert beats, 'no idle beat during the async post-first-byte stall'

        asyncio.new_event_loop().run_until_complete(_main())


# ═════════════════════════════════════════════════════════════════════
#  E. ★ The reaper must NOT eat a task that is legitimately waiting
#     (criterion 4 — the load-bearing consequence of removing timeouts)
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestReaperCannotEatAWaitingTask:
    def _mk_task(self):
        return {
            'id': 'task-wait-1', 'convId': 'cv-wait-1',
            'content': '', 'thinking': '', 'config': {'model': 'kimi-k3'},
            'events': [], 'toolRounds': [],
            'content_lock': threading.Lock(), 'events_lock': threading.Lock(),
        }

    def _drive_one_beat(self, task, elapsed):
        """Drive the REAL stream_llm_response, capture its on_waiting, fire
        one beat at *elapsed* seconds of idleness."""
        from types import SimpleNamespace
        import lib.tasks_pkg.manager as _mgr
        captured = {}

        def _fake_dispatch(body, **kwargs):
            captured['on_waiting'] = kwargs.get('on_waiting')
            return ({'role': 'assistant', 'content': 'ok',
                     'reasoning_content': ''}, 'stop', {})

        _orig = _mgr.dispatch_stream
        _mgr.dispatch_stream = _fake_dispatch
        try:
            _mgr.stream_llm_response(
                task, {'model': 'kimi-k3',
                       'messages': [{'role': 'user', 'content': 'go'}]},
                tag='R1')
        finally:
            _mgr.dispatch_stream = _orig
        assert captured.get('on_waiting'), 'on_waiting not wired into dispatch'
        slot = SimpleNamespace(model='kimi-k3', key_name='k0',
                               cooldown_reason='', cooldown_until=0.0,
                               last_error_msg='', consecutive_errors=0)
        captured['on_waiting'](elapsed=elapsed, slot=slot)

    def test_waiting_heartbeat_refreshes_the_reaper_dispatch_clock(self):
        """``_on_waiting`` MUST bump ``_dispatch_heartbeat``. Without it,
        both reaper clocks go stale during a long silence and the task is
        force-failed at 30 min — the reaper becoming the new timeout."""
        task = self._mk_task()
        task['_dispatch_heartbeat'] = time.time() - 5000
        before = task['_dispatch_heartbeat']
        self._drive_one_beat(task, elapsed=1800.0)
        assert task['_dispatch_heartbeat'] > before, \
            '_dispatch_heartbeat was not refreshed by the waiting heartbeat'
        assert time.time() - task['_dispatch_heartbeat'] < 5

    def test_task_waiting_40min_with_beats_is_NOT_reaped(self, monkeypatch):
        """END-TO-END of criterion 4 against the REAL reaper: a task that has
        been waiting 40 minutes but is still BEATING must survive, while the
        threshold is only 30 minutes."""
        from lib.tasks_pkg.manager import _maintenance, _registry

        monkeypatch.setattr(_maintenance, '_stuck_task_max_silent_secs',
                            lambda: 1800, raising=True)
        monkeypatch.setattr(_maintenance, '_finalize_reaped_stuck_task',
                            lambda t: None, raising=True)

        now = time.time()
        task = self._mk_task()
        task['status'] = 'running'
        task['aborted'] = False
        task['created_at'] = now - 2400          # 40 min old
        task['_t_last_event'] = now - 2400       # no DELTA for 40 min
        task['_dispatch_heartbeat'] = now - 2400 # stale BEFORE the beat

        fake = {task['id']: task}
        monkeypatch.setattr(_registry, 'tasks', fake, raising=True)
        monkeypatch.setattr(_registry, 'tasks_lock', threading.Lock(), raising=True)
        monkeypatch.setattr(_maintenance, 'tasks', fake, raising=True)
        monkeypatch.setattr(_maintenance, 'tasks_lock', threading.Lock(), raising=True)

        # Precondition: with BOTH clocks stale the reaper WOULD take it.
        assert _maintenance.reap_stuck_running_tasks() == 1, \
            'fixture wrong: a doubly-stale task must be reapable'

        # Now the real thing: the idle heartbeat fires, then the reaper runs.
        task['status'] = 'running'
        task['aborted'] = False
        task['_abort_reason'] = ''
        self._drive_one_beat(task, elapsed=2400.0)

        reaped = _maintenance.reap_stuck_running_tasks()
        assert reaped == 0, (
            'a task waiting 40 min but still BEATING was reaped — the reaper '
            'has become the new 30-minute timeout')
        assert task.get('_abort_reason') != 'stuck_no_progress'

    def test_dead_worker_with_no_beats_is_STILL_reaped(self, monkeypatch):
        """Complement / NEUTER of the above: the reaper must keep its REAL
        job. A worker that emits nothing at all (thread died) is still
        force-failed — otherwise we'd have traded a false-kill for a
        permanent zombie."""
        from lib.tasks_pkg.manager import _maintenance, _registry

        monkeypatch.setattr(_maintenance, '_stuck_task_max_silent_secs',
                            lambda: 1800, raising=True)
        monkeypatch.setattr(_maintenance, '_finalize_reaped_stuck_task',
                            lambda t: None, raising=True)
        now = time.time()
        task = self._mk_task()
        task.update({'status': 'running', 'aborted': False,
                     'created_at': now - 2400,
                     '_t_last_event': now - 2400,
                     '_dispatch_heartbeat': now - 2400})
        fake = {task['id']: task}
        monkeypatch.setattr(_registry, 'tasks', fake, raising=True)
        monkeypatch.setattr(_registry, 'tasks_lock', threading.Lock(), raising=True)
        monkeypatch.setattr(_maintenance, 'tasks', fake, raising=True)
        monkeypatch.setattr(_maintenance, 'tasks_lock', threading.Lock(), raising=True)

        assert _maintenance.reap_stuck_running_tasks() == 1
        assert task['_abort_reason'] == 'stuck_no_progress'


# ═════════════════════════════════════════════════════════════════════
#  F. The beat LABEL must stay honest
#     A mid-stream stall must not claim "no first byte yet" — the user can
#     see text already on screen, so that wording is a visible lie.
# ═════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBeatLabelHonesty:
    def _beats_for(self, prefill):
        """Drive the real _on_waiting with the task's content pre-set to
        *prefill*, and return the emitted phase events."""
        from types import SimpleNamespace
        import lib.tasks_pkg.manager as _mgr

        task = {'id': 'task-lbl', 'convId': 'cv-lbl',
                'content': '', 'thinking': '', 'config': {},
                'events': [], 'toolRounds': [],
                'content_lock': threading.Lock(),
                'events_lock': threading.Lock()}
        captured = {}

        def _fake_dispatch(body, **kwargs):
            captured['on_waiting'] = kwargs.get('on_waiting')
            # Simulate the round having produced text BEFORE the beat fires.
            task['content'] = prefill
            return ({'role': 'assistant', 'content': prefill,
                     'reasoning_content': ''}, 'stop', {})

        _orig = _mgr.dispatch_stream
        _mgr.dispatch_stream = _fake_dispatch
        try:
            _mgr.stream_llm_response(
                task, {'model': 'kimi-k3',
                       'messages': [{'role': 'user', 'content': 'go'}]},
                tag='R1')
        finally:
            _mgr.dispatch_stream = _orig
        slot = SimpleNamespace(model='kimi-k3', key_name='k0',
                               cooldown_reason='', cooldown_until=0.0,
                               last_error_msg='', consecutive_errors=0)
        captured['on_waiting'](elapsed=60.0, slot=slot)
        return [e for e in task['events'] if e.get('type') == 'phase'
                and e.get('phase') == 'retrying']

    def test_pre_first_byte_says_waiting_first_byte(self):
        evs = self._beats_for('')          # nothing streamed
        assert evs and evs[-1]['detailKey'] == 'stream.phase.waitingFirstByte'

    def test_mid_stream_stall_does_not_claim_no_first_byte(self):
        evs = self._beats_for('partial answer so far')
        assert evs, 'no beat emitted'
        ev = evs[-1]
        assert ev['detailKey'] == 'stream.phase.stalledMidStream', (
            'a mid-stream stall still reports the pre-first-byte label — '
            'the user can see text on screen, so that wording is a lie')
        assert 'first byte' not in ev['detail'].lower()

    def test_stall_i18n_keys_shipped_zh_and_en(self):
        locale_root = os.path.join(
            ROOT, 'frontend', 'src', 'i18n', 'locales')
        required = ('stream.phase.stalledMidStream',
                    'stream.phase.stalledMidStreamReason')
        retired = 'stream.retryReason.firstByteTimeout'
        for language in ('zh', 'en'):
            with open(os.path.join(locale_root, f'{language}.json'),
                      encoding='utf-8') as f:
                messages = json.load(f)
            for key in required:
                assert messages.get(key), (
                    f'{key} missing from the shipped {language} locale')
            # The dead first-byte-timeout reason key must be gone with its
            # error in every lazily loaded Vite locale chunk.
            assert retired not in messages


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

"""Regression tests for the GATEWAY-OUTAGE cap in the streaming dispatch loop.

Root cause this guards against: during a TOTAL upstream gateway outage every
slot on every key returns 502/503/504 (``openresty`` "502 Bad Gateway"). Per the
project's ``gateway-5xx-treated-as-429`` convention those map to
``RateLimitError`` and the streaming dispatch loop rotates slots FOREVER
(``_MAX_429_CYCLES = 0``). With the whole upstream down, that pins the worker
thread in a 0.3s poll loop and floods the log — the process stays alive while
the frontend can no longer be served ("backend alive, frontend dead").

The fix bounds a *gateway-5xx streak* with a wall-clock budget while leaving
genuine per-key 429 contention uncapped (a sibling key will free up). This
suite pins:

  1. ``RateLimitError.is_gateway`` is set for 502/503/504 (classifier) and NOT
     for a real 429 / quota 429.
  2. ``_StreamRetryState`` streak bookkeeping: a gateway 5xx opens the streak,
     a real 429 (or success) clears it, and the budget predicate honours the
     disable sentinel (<= 0).
  3. ``dispatch_stream`` RAISES once the gateway-5xx streak exceeds the budget
     (so the worker thread is freed) — the exact fix.
  4. ``dispatch_stream`` does NOT cap a real-429 storm even across a long
     wall-clock span (genuine contention must still rotate forever).

Run:  pytest tests/test_dispatch_gateway_outage_cap.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_slot(model='qwen-plus', key='k0'):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-1234', model=model,
                capabilities={'text'})


class _EndlessDispatcher:
    """Always hands out a fresh usable slot — models an outage where slots
    EXIST (capable) but every stream attempt fails. Lets us exercise the
    infinite-rotation loop without exhausting a queued list."""

    def __init__(self, model='qwen-plus'):
        self._model = model
        self.picks = 0
        self.slots = []

    def pick_and_reserve(self, **kwargs):
        self.picks += 1
        s = _make_slot(model=self._model, key='k%d' % (self.picks % 3))
        s.record_request()
        return s

    def has_capable_slots(self, *a, **kw):
        return True

    def summarize_slots(self, *a, **kw):
        return 'endless-fake'


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', lambda *_a, **_k: None)


class _FakeClock:
    """Monotonic clock that advances a fixed step on every call so a bounded
    number of loop iterations spans an arbitrarily long wall-clock window."""

    def __init__(self, step=1000.0):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


# ══════════════════════════════════════════════════════════
#  1. Classification: is_gateway
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestGatewayClassification:
    def test_502_sets_is_gateway(self):
        from lib.llm_errors import RateLimitError, _classify_http_error
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(502, '502 Bad Gateway', 'm', '[t]')
        assert ei.value.is_gateway is True
        assert ei.value.is_quota is False

    def test_503_504_set_is_gateway(self):
        from lib.llm_errors import RateLimitError, _classify_http_error
        for code in (503, 504):
            with pytest.raises(RateLimitError) as ei:
                _classify_http_error(code, 'upstream down', 'm', '[t]')
            assert ei.value.is_gateway is True

    def test_real_429_is_not_gateway(self):
        from lib.llm_errors import RateLimitError, _classify_http_error
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(429, 'rate limit exceeded', 'm', '[t]')
        assert ei.value.is_gateway is False

    def test_quota_429_is_not_gateway(self):
        from lib.llm_errors import RateLimitError, _classify_http_error
        with pytest.raises(RateLimitError) as ei:
            _classify_http_error(429, 'insufficient_quota', 'm', '[t]')
        assert ei.value.is_gateway is False
        assert ei.value.is_quota is True


# ══════════════════════════════════════════════════════════
#  2. _StreamRetryState streak bookkeeping (pure unit)
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestStreakBookkeeping:
    def test_gateway_opens_streak_real_429_clears(self, monkeypatch):
        from lib.llm_dispatch import api
        # Each monotonic() call advances 1000s, so the delta between recording
        # the streak start and the next exceeded-check (one step) is > budget.
        clock = _FakeClock(step=1000.0)
        monkeypatch.setattr(api.time, 'monotonic', clock)

        st = api._StreamRetryState()
        assert st.gateway_outage_exceeded(120) is False  # no streak yet

        st.note_free_429(is_gateway=True)   # opens streak
        assert st._gateway_streak_start is not None
        # advance clock past budget → exceeded
        assert st.gateway_outage_exceeded(50) is True

        st.note_free_429(is_gateway=False)  # a REAL 429 clears the streak
        assert st._gateway_streak_start is None
        assert st.gateway_outage_exceeded(50) is False

    def test_success_clears_streak(self, monkeypatch):
        from lib.llm_dispatch import api
        monkeypatch.setattr(api.time, 'monotonic', _FakeClock(step=10.0))
        st = api._StreamRetryState()
        st.note_free_429(is_gateway=True)
        assert st._gateway_streak_start is not None
        st.note_success()
        assert st._gateway_streak_start is None

    def test_budget_zero_disables_cap(self, monkeypatch):
        from lib.llm_dispatch import api
        monkeypatch.setattr(api.time, 'monotonic', _FakeClock(step=1e9))
        st = api._StreamRetryState()
        st.note_free_429(is_gateway=True)
        assert st.gateway_outage_exceeded(0) is False
        assert st.gateway_outage_exceeded(-1) is False


# ══════════════════════════════════════════════════════════
#  3. dispatch_stream: gateway outage RAISES (frees the thread)
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestGatewayOutageRaises:
    def test_total_gateway_outage_gives_up(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        disp = _EndlessDispatcher()
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        # Advancing clock so a handful of iterations spans > the budget.
        monkeypatch.setattr(api.time, 'monotonic', _FakeClock(step=1000.0))
        monkeypatch.setattr(api, '_GATEWAY_OUTAGE_BUDGET_S', 120.0)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)

        def _all_gateway_502(body, **kwargs):
            raise RateLimitError('502 Bad Gateway', is_gateway=True,
                                 reason='HTTP 502')

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _all_gateway_502)

        with pytest.raises(RateLimitError):
            api.dispatch_stream([{'role': 'user', 'content': 'hi'}],
                                log_prefix='[t]', max_retries=3)
        # Bounded — it did NOT spin forever (a few cycles, then gave up).
        assert disp.picks < 50


# ══════════════════════════════════════════════════════════
#  4. dispatch_stream: real 429 storm is NOT capped
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRealRateLimitNotCapped:
    def test_long_real_429_storm_then_success(self, monkeypatch):
        """A genuine per-key 429 contention run must keep rotating even across
        a long wall-clock window (the gateway cap must NOT fire for real 429s).
        """
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError

        disp = _EndlessDispatcher()
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        # Each iteration advances 1000s — 30 real-429 cycles = 30000s wall
        # clock, far beyond the 120s gateway budget. If the cap wrongly
        # counted real 429s this would raise; it must succeed instead.
        monkeypatch.setattr(api.time, 'monotonic', _FakeClock(step=1000.0))
        monkeypatch.setattr(api, '_GATEWAY_OUTAGE_BUDGET_S', 120.0)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)

        calls = {'n': 0}

        def _real_429_then_ok(body, **kwargs):
            calls['n'] += 1
            if calls['n'] <= 30:
                raise RateLimitError('rate limit exceeded')  # is_gateway=False
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _real_429_then_ok)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]', max_retries=1)
        assert msg == 'ok'
        assert calls['n'] == 31


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
